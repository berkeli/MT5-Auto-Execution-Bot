import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from bot.config.constants import BOT_VERSION
from bot.config.settings import Settings, load_config
from bot.core.dashboard_cache import DashboardCache
from bot.core.reconciler import Reconciler
from bot.core.sync_cycle import SyncCycle
from bot.db.sqlite import SQLiteDB
from bot.db.supabase import SupabaseDB
from bot.mt5.client import MT5Client
from bot.mt5.connection import MT5Connection
from bot.tp.engine import TPEngine
from bot.trading import approx_lot
from bot.trading.symbol_mapper import is_symbol_available, map_symbol
from bot.update.installer import UpdateInstaller
from bot.utils.time_utils import MarketScheduler

_RETCODE_DONE = 10009  # mt5.TRADE_RETCODE_DONE
_MARGIN_MODE_HEDGING = 2  # mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
_USER_SNAPSHOT_INTERVAL = 300.0  # 5 min between user-table upserts
_DISCONNECTED_RETRY_INTERVAL = 2.0  # short re-check while MT5 is disconnected
_UPDATE_CHECK_INTERVAL = 3600.0  # 1h between release-manifest checks

logger = logging.getLogger(__name__)


class Engine:
    def __init__(
        self,
        mt5_client: MT5Client,
        mt5_connection: MT5Connection,
        supabase: SupabaseDB,
        sqlite: SQLiteDB,
        config: Settings,
        tp_engine: TPEngine,
        tp_finalizer: Any | None = None,
        license_validator: Any | None = None,
        update_checker: Any | None = None,
    ) -> None:
        self._mt5 = mt5_client
        self._mt5_conn = mt5_connection
        self._supabase = supabase
        self._sqlite = sqlite
        self._config = config
        self._tp = tp_engine
        self._tp_finalizer = tp_finalizer
        self._license = license_validator
        self._update_checker = update_checker
        self._update_installer = UpdateInstaller()
        self._scheduler = MarketScheduler(
            config.spread_hour, config.tp_config.risky.disabled_windows
        )
        self._sync_cycle = SyncCycle()
        self._reconciler = Reconciler()
        self._trading_active = True
        self._running = False
        self._shutdown_callback: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: list[asyncio.Task] = []
        # FastAPI app; set externally via create_app(engine) in main.py
        self.app: Any | None = None
        self.api_ready = asyncio.Event()
        self.dashboard_cache = DashboardCache()
        # Broker symbol catalogue, refreshed on the engine thread for the Settings
        # symbol-mapping picker. The API reads this list; it never calls MT5 directly.
        self.broker_symbols: list[str] = []
        # DB instruments that have a live signal but whose mapped MT5 symbol the broker
        # doesn't carry. Recomputed each dashboard cycle; surfaced in Settings so users
        # can add a manual mapping. Falls off the list once a mapping resolves.
        self.not_found_symbols: list[str] = []
        # Broker SymbolInfo for the approximate-lot calculator's target instruments,
        # refreshed each dashboard cycle so the API can size lots without touching MT5.
        self.lot_specs: dict = {}
        # SSE consumers read from this queue
        self.status_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        # License teardown state
        self._license_expired: bool = False
        self.shutdown_reason: str | None = None
        # "Armed" flag: True once the license has validated and not yet been confirmed
        # rejected. It both arms teardown (a flip to a confirmed rejection flattens
        # positions) and gates new placement (the bot keeps trading through transient
        # ERRORs / sub-threshold rejections, and only stops once the rejection is
        # confirmed). A license that is invalid from startup never arms, so it never
        # trades. Set to the real post-validate state at startup (run_forever).
        self._last_license_valid: bool = True
        # True after MT5 has been initialized; gates Supabase/sync/tp startup.
        self._engine_started: bool = False
        # monotonic timestamp of last successful user-snapshot upsert
        self._last_user_snapshot: float = 0.0
        # last logged sync summary; suppress repeat lines when nothing changes
        self._last_sync_summary: tuple[int, int, int, int, int] | None = None
        # Self-update progress state, surfaced via the status broadcast.
        self._update_in_progress: bool = False
        self._update_progress: int = 0
        self._update_error: str | None = None
        # Set once a terminal shutdown begins, surfaced via the status broadcast so the
        # UI can show a closing screen before the process exits and the SSE drops.
        self._shutting_down: bool = False
        # Last Supabase pool-creation failure, surfaced as the Database status. None
        # once the pool is open or before a connection attempt (idle).
        self._supabase_error: str | None = None
        # (pending, open, trailing, mt5_connected) from the last status build; lets the
        # sync update-progress callback rebuild status without re-querying SQLite.
        self._last_counts: tuple[int, int, int, bool] = (0, 0, 0, False)
        self._last_trade_allowed = True

    def start(self) -> None:
        self._trading_active = True
        logger.info("Trading active")

    def stop(self) -> None:
        self._trading_active = False
        logger.info("Trading paused (position management continues)")

    def set_shutdown_callback(self, callback) -> None:
        self._shutdown_callback = callback

    def shutdown(self) -> None:
        """Signal the engine to stop all tasks and exit. Safe to call from any thread."""
        if self._shutting_down:
            return
        self._shutting_down = True
        self._trading_active = False
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._do_shutdown()))
        elif self._shutdown_callback is not None:
            self._shutdown_callback()

    async def _do_shutdown(self) -> None:
        """Emit a final 'shutting down' status so the UI can show a closing screen, give
        the SSE a moment to flush it to the browser, then cancel tasks and exit."""
        try:
            self.status_queue.put_nowait(self._status_snapshot())
        except asyncio.QueueFull:
            pass
        await asyncio.sleep(0.4)
        self._cancel_tasks()
        if self._shutdown_callback is not None:
            self._shutdown_callback()

    def _cancel_tasks(self) -> None:
        for task in self._tasks:
            task.cancel()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def sqlite(self) -> SQLiteDB:
        return self._sqlite

    @property
    def config(self) -> Settings:
        return self._config

    async def check_updates_now(self) -> None:
        """Run an on-demand update check and push the result to the UI."""
        if self._update_checker is None:
            return
        await self._update_checker.check()
        await self._broadcast_status()

    @property
    def trading_active(self) -> bool:
        return self._trading_active

    async def run_forever(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._running = True
        try:
            # Start API server first so the frontend can connect before init logs
            api_task = None
            if self.app is not None:
                api_task = asyncio.create_task(self._serve_api(), name="api_server")
                try:
                    await asyncio.wait_for(self.api_ready.wait(), timeout=15.0)
                except TimeoutError:
                    if api_task.done() and api_task.exception():
                        logger.critical("API server failed to start: %s", api_task.exception())
                    else:
                        logger.critical("API server failed to start within 15s")
                    return

            await self._sqlite.init_schema()

            # Wait until a license key is configured before launching MT5 or
            # opening the Supabase pool. This keeps the UI responsive while the
            # user enters their credentials and prevents signal data from being
            # fetched without an active license.
            await self._wait_for_license_config()
            if self._shutting_down:
                return

            if not await self._wait_for_mt5_init():
                return

            # Hedging is a hard requirement: on a netting account fill detection and
            # reconciliation are unsafe, so refuse to trade. Keep the API alive so the
            # UI can show why, but never start the trading loops.
            if not self._check_account_mode():
                self.shutdown_reason = "netting_account"
                await self._broadcast_status()
                if api_task is not None:
                    self._tasks = [api_task]
                    await asyncio.gather(api_task, return_exceptions=True)
                return

            try:
                await self._supabase.create_pool()
                self._supabase_error = None
            except Exception as e:
                logger.critical("Supabase connection failed", exc_info=True)
                self._supabase_error = str(e) or "connection failed"
                await self._broadcast_status()
                return

            await self._reconciler.reconcile(self._mt5, self._sqlite)

            if self._license is not None:
                acct = self._mt5.account_info()
                mt5_account = acct.login if acct else 0
                await self._license.validate(self._config.license_key, mt5_account)
                # Sync _last_license_valid to the real post-validate state so a failed
                # startup validate does not falsely trigger teardown on the first sync cycle.
                self._last_license_valid = self._license.license_valid

            self._engine_started = True

            tasks = [
                asyncio.create_task(self._sync_loop(), name="sync_loop"),
                asyncio.create_task(self._tp_loop(), name="tp_loop"),
                asyncio.create_task(self._reconcile_loop(), name="reconcile_loop"),
            ]
            if self._license is not None:
                tasks.append(
                    asyncio.create_task(
                        self._license.heartbeat_loop(
                            self._config.polling.license_heartbeat_seconds
                        ),
                        name="license_heartbeat",
                    )
                )
            if self._update_checker is not None:
                tasks.append(asyncio.create_task(self._update_loop(), name="update_loop"))
            if api_task is not None:
                tasks.append(api_task)
            self._tasks = tasks

            # return_exceptions keeps one task's crash from masking the others, but it
            # also swallows the exception — surface it so a loop that dies (not just a
            # shutdown CancelledError) is visible in the log instead of going silent.
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for task, result in zip(tasks, results, strict=True):
                if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                    logger.critical("Task %s exited unexpectedly", task.get_name(), exc_info=result)
        finally:
            self._running = False
            try:
                await self._supabase.close()
            except Exception:
                pass
            try:
                await self._sqlite.close()
            except Exception:
                pass
            try:
                self._mt5_conn.shutdown()
            except Exception:
                pass

    def _check_account_mode(self) -> bool:
        """Fill detection and reconciliation depend on hedging mode (position.identifier
        == originating order ticket), which doesn't hold on netting/exchange accounts.
        Returns False to block trading when the account is positively netting; True when
        it's hedging or the account can't be read (transient — don't block on that)."""
        acct = self._mt5.account_info()
        if acct is None:
            return True
        company = acct.company or "broker"
        if acct.margin_mode != _MARGIN_MODE_HEDGING:
            logger.critical(
                "Account %s (%s, %s) is in netting mode (margin_mode=%d) — this bot requires a "
                "hedging account; refusing to trade. Switch to a hedging MT5 account to continue.",
                acct.login,
                company,
                acct.server,
                acct.margin_mode,
            )
            return False
        logger.info("Connected to %s (%s), hedging mode OK", company, acct.server)
        return True

    async def _wait_for_mt5_init(self) -> bool:
        """Initialize MT5 with retry. On failure (e.g. wrong terminal path),
        keep retrying while reloading config so the user can fix the path via
        Settings without restarting the bot. Returns True once initialized,
        False if the engine is shut down before MT5 comes up."""
        logged_failure = False
        while self._running and not self._shutting_down:
            self._mt5_conn.set_terminal_path(self._config.mt5_terminal_path)
            if self._mt5_conn.initialize():
                return True
            if not logged_failure:
                logger.warning(
                    "MT5 initialization failed (path=%r, error=%s) — will retry; "
                    "fix the path in Settings → MT5 terminal path",
                    self._config.mt5_terminal_path or "<auto-detect>",
                    self._mt5_conn.last_error,
                )
                logged_failure = True
            await self._broadcast_status()
            await asyncio.sleep(3.0)
            new_config = load_config()
            if new_config is not None:
                self._config = new_config
                self._scheduler = MarketScheduler(
                    new_config.spread_hour, new_config.tp_config.risky.disabled_windows
                )
        return False

    async def _wait_for_license_config(self) -> None:
        """Poll config.json until license_key is set. Broadcasts status so the UI
        can show the engine is awaiting credentials."""
        if self._config.license_key:
            return
        logger.info("Waiting for license key in config.json — engine paused")
        while not self._config.license_key and not self._shutting_down:
            await self._broadcast_status()
            await asyncio.sleep(2.0)
            new_config = load_config()
            if new_config is not None:
                self._config = new_config
                self._scheduler = MarketScheduler(
                    new_config.spread_hour, new_config.tp_config.risky.disabled_windows
                )
        logger.info("License key configured — proceeding with engine startup")

    async def _sync_loop(self) -> None:
        while True:
            if self._license_expired:
                logger.info("Sync loop halted: license expired")
                return

            # Detect a license flip from valid → confirmed-rejected and run teardown.
            if self._license is not None:
                self._last_license_valid, should_teardown = self._teardown_decision(
                    self._last_license_valid,
                    self._license.license_valid,
                    self._license.confirmed_rejected,
                )
                if should_teardown:
                    await self._license_teardown()
                    return

            try:
                new_config = load_config()
                if new_config:
                    old_license_key = self._config.license_key
                    self._config = new_config
                    self._scheduler = MarketScheduler(
                        new_config.spread_hour, new_config.tp_config.risky.disabled_windows
                    )
                    if (
                        self._license is not None
                        and new_config.license_key
                        and new_config.license_key != old_license_key
                    ):
                        acct = self._mt5.account_info()
                        mt5_account = acct.login if acct else 0
                        await self._license.validate(new_config.license_key, mt5_account)
                        self._last_license_valid = self._license.license_valid

                # Skip the cycle while MT5 is disconnected — no point driving order
                # placement/management against a dead IPC pipe or a terminal with no
                # broker link. ensure_connected() re-inits a crashed terminal and rides
                # out a transient broker-link drop (e.g. the account briefly logged in
                # elsewhere) without re-initializing, then we resume on the next tick.
                if not self._mt5.ensure_connected():
                    await self._broadcast_status()
                    await asyncio.sleep(_DISCONNECTED_RETRY_INTERVAL)
                    continue

                # Keep placing while the license is valid OR only transiently/
                # not-yet-confirmed unhealthy (armed). A never-valid license (invalid
                # from startup) is unarmed and never places; a confirmed rejection
                # disarms via teardown above before this line is reached.
                placement_active = self._trading_active and (
                    self._license is None or self._last_license_valid
                )

                result = await self._sync_cycle.run(
                    self._supabase,
                    self._sqlite,
                    self._mt5,
                    self._config,
                    self._scheduler,
                    placement_active=placement_active,
                )
                # Dedupe on actionable counts only — skipped jitters with price and
                # shouldn't re-trigger an otherwise-identical line every cycle.
                key = (
                    result.placed,
                    result.cancelled,
                    result.filled,
                    result.new_trailing,
                    result.errors,
                )
                if any(key) and key != self._last_sync_summary:
                    logger.info(
                        "Sync: placed=%d cancelled=%d filled=%d trailing=%d errors=%d skipped=%d",
                        *key,
                        result.skipped,
                    )
                self._last_sync_summary = key
                await self._broadcast_status()
                await self._update_dashboard()
                await self._maybe_upsert_user_snapshot()
            except Exception:
                logger.error("sync_loop error", exc_info=True)

            await asyncio.sleep(await self._active_interval())

    async def _tp_loop(self) -> None:
        while True:
            try:
                config = self._config
                # The sync loop owns reconnection; here just skip while disconnected
                # so the TP engine and finalizer don't run against a dead pipe.
                if not self._mt5.ensure_connected():
                    await asyncio.sleep(_DISCONNECTED_RETRY_INTERVAL)
                    continue
                if not config.disable_auto_tp:
                    # Signals the TM has auto-TP'd, kept current by the sync loop off its
                    # existing status snapshot — the trigger when follow_server_tp is on.
                    server_tp = self._sync_cycle.server_tp_signals
                    await self._tp.run_cycle(
                        self._mt5,
                        self._sqlite,
                        config,
                        crypto_only=self._scheduler.is_spread_hour(),
                        server_tp_signals=server_tp,
                    )
                if self._tp_finalizer is not None:
                    await self._tp_finalizer.sweep(self._mt5, self._sqlite, config)
            except Exception:
                logger.error("tp_loop error", exc_info=True)

            await asyncio.sleep(await self._tp_interval())

    async def _serve_api(self) -> None:
        try:
            import uvicorn

            cfg = uvicorn.Config(
                self.app,
                host="127.0.0.1",
                port=8500 + self._config.instance_id,
                log_level="error",
                log_config=None,
                loop="none",
            )
            server = uvicorn.Server(cfg)
            await server.serve()
        except Exception:
            logger.error("API server crashed", exc_info=True)

    async def _reconcile_loop(self) -> None:
        """Orphan sweep every 60s; full reconcile every 2h."""
        ORPHAN_INTERVAL = 60.0
        FULL_RECONCILE_INTERVAL = 2 * 3600.0
        last_full = time.monotonic()

        while True:
            await asyncio.sleep(ORPHAN_INTERVAL)
            try:
                swept = await self._reconciler.reconcile_orphans(self._mt5, self._sqlite)
                if swept:
                    logger.info("Orphan sweep: %d processed", swept)
            except Exception:
                logger.error("Orphan sweep failed", exc_info=True)

            now = time.monotonic()
            if now - last_full >= FULL_RECONCILE_INTERVAL:
                try:
                    await self._reconciler.reconcile(self._mt5, self._sqlite)
                except Exception:
                    logger.error("Periodic reconcile failed", exc_info=True)
                last_full = now

    async def _update_loop(self) -> None:
        """Poll the release manifest so the UI can offer 'Update and restart' when a
        newer build ships. Non-fatal — the checker swallows its own errors."""
        await asyncio.sleep(30.0)
        while True:
            await self._update_checker.check()
            await self._broadcast_status()
            await asyncio.sleep(_UPDATE_CHECK_INTERVAL)

    def start_update(self) -> None:
        """Kick off download + self-replace from the API handler (engine event loop)."""
        if self._update_in_progress or not self._update_checker.info.available:
            return
        asyncio.create_task(self._run_update(), name="run_update")

    async def _run_update(self) -> None:
        self._update_in_progress = True
        self._update_progress = 0
        self._update_error = None
        await self._broadcast_status()
        try:
            info = self._update_checker.info

            def on_progress(pct: int) -> None:
                # Only emit on a whole-percent change so a large download can't flood
                # the bounded status queue; drop the tick if the queue is momentarily full.
                if pct == self._update_progress:
                    return
                self._update_progress = pct
                try:
                    self.status_queue.put_nowait(self._status_snapshot())
                except asyncio.QueueFull:
                    pass

            staged = await self._update_installer.download(info, on_progress)
            self._update_installer.apply_and_restart(staged)
            self.shutdown()
        except Exception as e:
            logger.error("Self-update failed", exc_info=True)
            self._update_error = str(e)
            self._update_in_progress = False
            await self._broadcast_status()

    @staticmethod
    def _teardown_decision(
        armed: bool, license_valid: bool, confirmed_rejected: bool
    ) -> tuple[bool, bool]:
        """Returns (new_armed, should_teardown). Teardown fires only on a flip from
        valid → sustained rejection: the license must have been valid (armed) and the
        validator must report a confirmed rejection (N consecutive INVALID/EXPIRED
        results). A single anomalous rejection or a transient ERROR leaves the armed
        state untouched, so neither can flatten a paying user's positions."""
        if license_valid:
            return True, False
        if armed and confirmed_rejected:
            return False, True
        return armed, False

    async def _license_teardown(self) -> None:
        """Cancel all pending orders and close all positions after license expiry."""
        self._license_expired = True
        self.shutdown_reason = "license_expired"
        logger.error("License expired — cancelling all pending orders and closing all positions")

        try:
            now_iso = datetime.now(UTC).isoformat()

            pending = await self._sqlite.get_pending_orders()
            for row in pending:
                ticket = row["mt5_ticket"]
                res = self._mt5.cancel_pending_order(ticket)
                if res and res.retcode == _RETCODE_DONE:
                    await self._sqlite.mark_cancelled(ticket, now_iso, spread=False)
                    logger.info("License teardown: cancelled pending ticket=%d", ticket)
                else:
                    retcode = res.retcode if res else "None"
                    logger.error(
                        "License teardown: cancel failed ticket=%d retcode=%s", ticket, retcode
                    )

            mt5_positions = {p.ticket: p for p in self._mt5.positions_get()}
            filled = await self._sqlite.get_filled_positions()
            for row in filled:
                ticket = row["mt5_ticket"]
                pos = mt5_positions.get(ticket)
                if pos is None:
                    continue
                res = self._mt5.close_position(
                    ticket=pos.ticket,
                    symbol=pos.symbol,
                    volume=pos.volume,
                    position_type=pos.type,
                    comment="license_expired",
                )
                if res and res.retcode == _RETCODE_DONE:
                    realized_pnl = self._mt5.get_position_realized_pnl(ticket)
                    if realized_pnl is None:
                        realized_pnl = pos.profit
                    await self._sqlite.mark_closed(ticket, realized_pnl)
                    logger.info("License teardown: closed position ticket=%d", ticket)
                else:
                    retcode = res.retcode if res else "None"
                    logger.error(
                        "License teardown: close failed ticket=%d retcode=%s", ticket, retcode
                    )
        except Exception:
            logger.error("License teardown encountered an error", exc_info=True)

    async def _update_dashboard(self) -> None:
        try:
            acct = self._mt5.account_info()
            positions = self._mt5.positions_get()
            orders = self._mt5.orders_get()
            active = await self._sqlite.get_all_active()
            signal_actions = await self._sqlite.get_signal_actions()
            # symbols_get() is cached in the client; refresh the API-facing copy cheaply.
            catalogue = self._mt5.symbols_get()
            self.broker_symbols = sorted(catalogue)
            self._refresh_lot_specs(catalogue)
            rows = self._sync_cycle.last_supabase_rows
            if catalogue and rows:
                self.not_found_symbols = sorted(
                    {
                        r["instrument"]
                        for r in rows
                        if not is_symbol_available(r["instrument"], catalogue, self._config)
                    }
                )
            elif catalogue:
                self.not_found_symbols = []
            self.dashboard_cache.update(
                acct,
                positions,
                orders,
                active,
                self._mt5,
                supabase_rows=self._sync_cycle.last_supabase_rows,
                live_prices=self._sync_cycle.last_live_prices,
                pending_limit_ids=self._sync_cycle.last_sqlite_pending_limit_ids,
                config=self._config,
                broker_symbols=catalogue,
                signal_actions=signal_actions,
            )
        except Exception:
            logger.error("Dashboard cache update failed", exc_info=True)

    def _refresh_lot_specs(self, catalogue: frozenset[str]) -> None:
        """Cache SymbolInfo for the approximate-lot target instruments the broker
        carries. symbol_info is cached in the client, so this is near-free after the
        first cycle and keeps MT5 off the API request path."""
        specs: dict = {}
        for db in approx_lot.target_db_symbols():
            mt5_sym = map_symbol(db, self._config)
            if mt5_sym not in catalogue:
                continue
            info = self._mt5.symbol_info(mt5_sym)
            if info is not None:
                specs[mt5_sym] = info
        self.lot_specs = specs

    async def _maybe_upsert_user_snapshot(self) -> None:
        """Push a per-user account snapshot to Supabase every 5 min for leaderboard
        and TP-optimization analysis. Non-fatal; logged on failure and skipped if
        prerequisites are missing."""
        if not self._config.license_key:
            return
        if not self._supabase.is_connected:
            return
        now = time.monotonic()
        if now - self._last_user_snapshot < _USER_SNAPSHOT_INTERVAL:
            return

        acct = self._mt5.account_info()
        if acct is None:
            return

        stats = await self._sqlite.get_user_stats()
        decided = stats["wins"] + stats["losses"]
        win_rate = (stats["wins"] / decided * 100.0) if decided else 0.0
        open_positions_count = len(await self._sqlite.get_filled_positions())

        await self._supabase.upsert_user_snapshot(
            license_key=self._config.license_key,
            mt5_account=int(acct.login),
            balance=float(acct.balance),
            equity=float(acct.equity),
            currency=acct.currency,
            leverage=int(acct.leverage),
            open_positions_count=open_positions_count,
            total_realized_pnl=stats["total_pnl"],
            total_trades=int(stats["total_trades"]),
            wins=int(stats["wins"]),
            losses=int(stats["losses"]),
            win_rate=round(win_rate, 2),
            bot_version=BOT_VERSION,
        )
        self._last_user_snapshot = now

    async def _loop_interval(self, active_seconds: float) -> float:
        """Sleep for the sync/TP loops: fast while orders are active, slow when
        idle, and throttled during spread hour when nothing needs 1s reactivity."""
        has_active = bool(await self._sqlite.get_all_active())
        if self._scheduler.is_spread_hour():
            return 30.0 if has_active else 60.0
        if has_active:
            return active_seconds
        return float(self._config.polling.supabase_interval_seconds)

    async def _active_interval(self) -> float:
        return await self._loop_interval(float(self._config.polling.tp_active_interval_seconds))

    async def _tp_interval(self) -> float:
        return await self._loop_interval(float(self._config.polling.tp_trailing_interval_seconds))

    async def _broadcast_status(self) -> None:
        active = await self._sqlite.get_all_active()
        pending_count = sum(1 for r in active if r["status"] == "pending")
        open_count = sum(1 for r in active if r["status"] == "filled")
        trailing_count = sum(1 for r in active if r["status"] == "filled" and r["is_trailing"])
        mt5_connected = self._mt5.ensure_connected()
        self._last_counts = (pending_count, open_count, trailing_count, mt5_connected)
        self._last_trade_allowed = self._mt5.trade_allowed() if mt5_connected else True
        try:
            self.status_queue.put_nowait(self._status_snapshot())
        except asyncio.QueueFull:
            pass

    def _status_snapshot(self) -> dict:
        """Build the status dict from the last cached counts. Sync so the update
        progress callback can refresh it without re-querying SQLite."""
        pending_count, open_count, trailing_count, mt5_connected = self._last_counts
        info = self._update_checker.info if self._update_checker is not None else None
        return {
            "engine_running": self._running,
            "trading_active": self._trading_active,
            "license_valid": getattr(self._license, "license_valid", True),
            "license_status": getattr(getattr(self._license, "status", None), "value", "valid"),
            "license_message": getattr(self._license, "message", ""),
            "mt5_connected": mt5_connected,
            "mt5_error": None if mt5_connected else self._mt5_conn.last_error,
            "supabase_connected": self._supabase.is_connected,
            "supabase_error": self._supabase_error,
            "pending_count": pending_count,
            "open_count": open_count,
            "trailing_count": trailing_count,
            "bot_version": BOT_VERSION,
            "shutdown_reason": self.shutdown_reason,
            "shutting_down": self._shutting_down,
            "update_available": info.available if info else False,
            "update_version": info.version if info else None,
            "update_notes": info.notes if info else "",
            "update_in_progress": self._update_in_progress,
            "update_progress": self._update_progress,
            "update_error": self._update_error,
            # Gate out the weekend so the banner reflects only the genuine daily
            # spread-hour window — is_spread_hour() stays true all weekend (the
            # trading gates rely on that), which the "Market closed" banner covers.
            "spread_hour_active": self._scheduler.is_spread_hour()
            and not self._scheduler.is_weekend_window(),
            # True only once SLs are actually being stripped (spread-hour proper); the
            # earlier daily_start..sl_strip_start slice is the "late-market" window where
            # only pending orders are cancelled. Lets the UI split the two banners.
            "sl_strip_active": self._scheduler.is_sl_strip_window()
            and not self._scheduler.is_weekend_window(),
            "market_closed": self._scheduler.is_weekend_window(),
            "algo_trading_disabled": mt5_connected and not self._last_trade_allowed,
            "symbol_count": len(self.broker_symbols),
        }
