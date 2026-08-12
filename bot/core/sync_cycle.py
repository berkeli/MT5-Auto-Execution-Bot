import json
import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import MetaTrader5 as mt5

from bot.config.constants import AssetClass
from bot.config.settings import Settings
from bot.db.sqlite import SQLiteDB
from bot.db.supabase import SupabaseDB
from bot.mt5.client import MT5Client
from bot.trading.fill_detector import FillDetector
from bot.trading.lot_calculator import LotCalculator
from bot.trading.offset_calculator import OffsetCalculator
from bot.trading.order_canceller import OrderCanceller
from bot.trading.order_placer import OrderPlacer
from bot.trading.symbol_mapper import (
    db_symbol_from_mt5,
    detect_asset_class,
    instrument_under_news,
    map_symbol,
    needs_offset,
    offset_drift_threshold,
    parse_news_symbols,
    pip_size,
    proximity_threshold,
)
from bot.utils.time_utils import MarketScheduler

logger = logging.getLogger(__name__)

_UNAVAILABLE_COOLDOWN = 300.0  # seconds before retrying a "not in terminal" symbol

# Fallback max-ages for the rev-gated Supabase caches. With the signals_rev watermark
# present, refetches are change-driven and these only cover a missed trigger bump;
# against a legacy DB (no watermark) the legacy values reproduce the old interval polling.
_SIGNAL_SETS_MAX_AGE = 60.0
_SIGNAL_SETS_MAX_AGE_LEGACY = 5.0
_STATUS_MAX_AGE = 30.0
_STATUS_MAX_AGE_LEGACY = 2.0


# Hysteresis band on the proximity gate: a limit is placed at the threshold but only
# cancelled once price has walked 1.5x it away. Without the gap, price fluctuating
# across the threshold cancels and re-places the same ladder every cycle — churn the
# broker sees as order spam.
_PROXIMITY_EXIT_MULTIPLIER = 1.5

# Instant-entry signals (a limit born 'hit' at the market price the TM saw, plus a fixed
# take profit) are taken at market rather than rested as a limit, so both placement gates
# are about how far reality has moved since the TM looked:
#   - hit_time older than this and we're no longer entering the trade that was posted —
#     a restart or a stalled cycle must not open a position on an hours-old signal.
#   - price further than half the instrument's proximity threshold from the recorded
#     entry and we'd be taking a materially different trade.
_INSTANT_MAX_AGE_SECONDS = 60.0
_INSTANT_PROXIMITY_MULTIPLIER = 0.5


def _is_instant(row) -> bool:
    return row["take_profit"] is not None


def _within_proximity(
    limit_prices: list[float],
    mid: float,
    asset_class: AssetClass,
    info,
    prox,
    db_sym: str = "",
    multiplier: float = 1.0,
) -> bool:
    threshold = proximity_threshold(asset_class, info, prox, db_sym)
    if threshold is None:
        return True
    return min(abs(p - mid) for p in limit_prices) <= threshold * multiplier


@dataclass
class SyncResult:
    placed: int = 0
    cancelled: int = 0
    filled: int = 0
    new_trailing: int = 0
    errors: int = 0
    skipped: int = 0  # proximity-filtered limits + past-price placement skips


_FORCE_EXIT_STATUSES = frozenset({"cancelled", "breakeven"})
# 'expiry' is the only cancel reason that keeps a weekday non-crypto position open:
# the TM rolls a hit signal's expiry forward rather than truly closing it, so those
# cancels are gated to the weekend/crypto window. Every other cancel reason
# (near_miss, manual, news:*, spread_hour, late_market, risky_window) means the signal
# was voided or falsely triggered, so we force-close on any day and asset class.
_ROLLOVER_CANCEL_REASONS = frozenset({"expiry"})
_SL_FAIL_MAX = 5
_FORCE_EXIT_MAX_ATTEMPTS = 5


@dataclass
class _CycleContext:
    """Per-cycle snapshot shared by the placement and maintenance phases.

    Built once per run() after the Supabase fetch succeeds; every extracted
    phase reads the same snapshot instead of re-deriving it.
    """

    config: Settings
    scheduler: MarketScheduler
    now: datetime  # wall clock for gate decisions
    cache_now: float  # monotonic clock for egress-guard cache ages
    unmanaged_sids: set[int]  # user 'skip' + 'manual' signals — never touched
    filled_sids: set[int]  # signals we hold a filled position on
    supabase_rows: list  # active pending limits (exclusions applied)
    hit_limit_ids: set[int]  # TM-marked 'hit' limits on live signals
    profit_held_limit_ids: set[int]  # pending limits spared on profit-marked signals
    supabase_by_limit: dict[int, dict]
    supabase_limit_ids: set[int]
    sqlite_limit_ids: set[int]
    sqlite_pending: list  # local pending orders, shrunk as phases cancel
    news_symbols: frozenset[str]
    vol_symbols: frozenset[str]
    risky_disabled: bool
    risky_sl_by_signal: dict[int, float]
    tp_fired_signals: set[int]
    offset_needed: set[str] = field(default_factory=set)
    live_prices: dict = field(default_factory=dict)
    # Tickets already cancelled by a maintenance loop this cycle, so a later
    # loop over the same pending snapshot doesn't re-cancel a gone order.
    repriced_tickets: set[int] = field(default_factory=set)

    def instr_of(self, row) -> str:
        lid = row["limit_id"]
        if lid in self.supabase_by_limit:
            return self.supabase_by_limit[lid]["instrument"]
        return db_symbol_from_mt5(row["symbol"] or "", self.config)

    def is_blocked(self, instr: str, signal_type: str = "standard") -> bool:
        if signal_type == "risky" and self.risky_disabled:
            return True
        if _gated_by_news_or_vol(instr, self.news_symbols, self.vol_symbols, self.config):
            return True
        if _gate_exempt(instr, self.config):
            return False
        is_stock = detect_asset_class(instr) == AssetClass.STOCKS
        return self.scheduler.should_cancel_pending(self.now, stock=is_stock)

    def row_blocked(self, row) -> bool:
        return self.is_blocked(self.instr_of(row), row["signal_type"])


def _persist_stock_no_suffix(db_symbol: str, config: Settings) -> None:
    try:
        config_path = Path("config.json")
        data = json.loads(config_path.read_text())
        no_suffix = data.get("stock_no_suffix", [])
        if db_symbol not in no_suffix:
            no_suffix.append(db_symbol)
            data["stock_no_suffix"] = no_suffix
            config_path.write_text(json.dumps(data, indent=2))
            config.stock_no_suffix = no_suffix
            logger.info("Persisted %s to stock_no_suffix in config.json", db_symbol)
    except Exception:
        logger.error("Failed to persist stock_no_suffix", exc_info=True)


def _gate_exempt(instr: str, config: Settings) -> bool:
    """Crypto and 24h stocks (broker -24 suffix) trade around the clock, so they're
    exempt from the spread-hour and news-mode gates."""
    ac = detect_asset_class(instr)
    if ac == AssetClass.CRYPTO:
        return True
    return (
        ac == AssetClass.STOCKS
        and bool(config.stock_suffix)
        and map_symbol(instr, config).endswith(config.stock_suffix)
    )


def _gated_by_news_or_vol(
    instr: str, news_symbols: frozenset[str], vol_symbols: frozenset[str], config: Settings
) -> bool:
    """True when the volatility guard or news mode gates this instrument.

    The 24/7 exemption is news-specific: crypto and 24h stocks don't share the
    liquidity events news mode guards against. Volatility is measured from price,
    so a move that already happened applies to them like any other class — the
    vol tokens are checked before the exemption, news tokens after."""
    if instrument_under_news(instr, vol_symbols):
        return True
    return not _gate_exempt(instr, config) and instrument_under_news(instr, news_symbols)


def _feed_for_symbol(db_sym: str, config: Settings, live_prices: dict) -> str | None:
    """The TM feed serving this symbol, read off the live_prices row TM wrote for it.
    Inferring it from asset class instead got oil wrong (exness-fed, not oanda), so the
    staleness gate watched the wrong feed's health. None when TM has written no row —
    the proximity gate reports that case with the accurate reason."""
    if not needs_offset(db_sym, config):
        return "icmarkets"
    row = live_prices.get(db_sym)
    return row["feed"] if row is not None else None


class SyncCycle:
    def __init__(self) -> None:
        self._placer = OrderPlacer()
        self._canceller = OrderCanceller()
        self._fill_detector = FillDetector()
        self._offset_calc = OffsetCalculator()
        # mt5_symbol -> monotonic timestamp after which we retry the tick lookup
        self._unavailable_until: dict[str, float] = {}
        # signal_id -> last known status (for forced exit transition detection)
        self._last_signal_status: dict[int, str] = {}
        # limit_ids whose exclusion has already been logged (logged once per lifetime)
        self._logged_excluded: set[int] = set()
        # mt5_symbols the broker doesn't offer at all (logged once per lifetime)
        self._logged_unmapped: set[str] = set()
        # signal_ids whose proximity rejection has been logged (logged once per lifetime)
        self._logged_proximity: set[int] = set()
        # limit_ids skipped from re-placement because they already filled on our end
        # (logged once per lifetime — flags a DB desync without spamming each cycle)
        self._logged_already_filled: set[int] = set()
        # signal_ids skipped by the limit-count gate (logged once per lifetime)
        self._logged_limit_skips: set[int] = set()
        # limit_ids of instant entries that went stale before we could take them
        # (logged once per lifetime — the row stays in the fetch until the TM closes it)
        self._logged_stale_instant: set[int] = set()
        # SL sync consecutive failure tracking
        self._sl_fail_count: dict[int, int] = {}  # ticket -> consecutive fail count
        self._sl_fail_target: dict[int, float] = {}  # ticket -> last failed target sl
        # Force-exit consecutive failure tracking
        self._force_exit_fail_count: dict[int, int] = {}  # mt5_ticket -> consecutive fail count
        # News force-exit consecutive failure tracking (separate from status force-exit)
        self._news_exit_fail_count: dict[int, int] = {}  # mt5_ticket -> consecutive fail count
        # Risky-window force-exit consecutive failure tracking
        self._risky_exit_fail_count: dict[int, int] = {}  # mt5_ticket -> consecutive fail count
        # Profit-weekend force-exit consecutive failure tracking (separate from the above)
        self._profit_weekend_fail_count: dict[int, int] = {}  # mt5_ticket -> consecutive fail count
        # Spread-hour SL strip/restore consecutive failure tracking (log suppression)
        self._sl_strip_fail_count: dict[int, int] = {}  # mt5_ticket -> consecutive fail count
        self._sl_restore_fail_count: dict[int, int] = {}  # mt5_ticket -> consecutive fail count
        self._last_force_exit_status: dict[
            int, str
        ] = {}  # signal_id -> force-exit status at first detection
        # Tickets closed during the current cycle. The exit routines share one
        # filled-positions snapshot per cycle, so without this a position closed
        # by an earlier routine would still look open to a later one.
        self._closed_tickets: set[int] = set()
        self._feed_health_failed: bool = False
        # Egress-guard caches: the active-signal set and force-exit statuses change
        # only when the TM writes signals/limits, so instead of re-pulling them on
        # short intervals the cycle polls one tiny sync-state row (gates + the
        # signals_rev watermark) and drops these caches when the rev moves. The
        # max-age constants below are only a safety net for a missed bump; while
        # the watermark is unavailable (legacy DB) the legacy intervals drive
        # refetching exactly as before.
        self._signals_rev: int | None = None
        self._signal_sets_cache: tuple[list, set[int], dict[int, int]] | None = None
        self._signal_sets_cache_at: float = 0.0
        self._signal_sets_cache_sids: set[int] = set()
        self._gates_cache: tuple[str | None, str | None] | None = None
        self._stale_feeds_cache: set[str] = set()
        self._feed_health_cache_at: float = 0.0
        self._live_prices_cache: dict = {}
        self._live_prices_cache_at: float = 0.0
        self._live_prices_requested: set[str] = set()
        # Force-exit status snapshot for filled signals (egress guard, same shape as the
        # caches above): dropped on a rev change, and refetched immediately when a newly
        # filled signal appears so a force-exit directive is never missed.
        self._status_cache: dict[int, dict] | None = None
        self._status_cache_at: float = 0.0
        self._status_requested: set[int] = set()
        # DB instruments whose feed has gone dark and already been warned about — cleared
        # when the feed recovers so the warning fires once per dark episode, not per cycle.
        self._logged_dark_feeds: set[str] = set()
        # Signals the TM has auto-TP'd, read by the TP engine when the user enables
        # tp_config.follow_server_tp. Derived from the force-exit status snapshot, so it
        # costs no extra egress; preserved across a Supabase blip like that snapshot is.
        self.server_tp_signals: set[int] = set()
        # Snapshots read by DashboardCache to render unplaced "watching" signals.
        # Preserved across Supabase outages so the UI doesn't blank out.
        self.last_supabase_rows: list | None = None
        self.last_live_prices: dict = {}
        self.last_sqlite_pending_limit_ids: set[int] = set()

    def _risky_sl_map(self, supabase_rows: list, config: Settings) -> dict[int, float]:
        """Custom shared stop-loss price per risky signal, or {} when no custom SL is
        configured. Measured from the signal's deepest limit (lowest for longs, highest
        for shorts) so every limit shares one SL. Computed from the still-pending limits
        in the active fetch — the common case where a risky signal is placed as a set."""
        dist = config.tp_config.risky.stop_loss
        if dist is None:
            return {}
        by_sig: dict[int, list] = defaultdict(list)
        for r in supabase_rows:
            if (r["signal_type"] or "") == "risky":
                by_sig[r["signal_id"]].append(r)
        out: dict[int, float] = {}
        for sid, rows in by_sig.items():
            prices = [float(r["price_level"]) for r in rows]
            if rows[0]["direction"] == "long":
                out[sid] = min(prices) - dist
            else:
                out[sid] = max(prices) + dist
        return out

    def _apply_exclusions(self, rows: list, config: Settings) -> list:
        excluded_syms = {s.upper() for s in config.excluded_symbols}
        disabled_types = set(config.disabled_signal_types)
        disabled_channels = set(config.disabled_channels)

        filtered = []
        for r in rows:
            sym = r["instrument"].upper()
            stype = r["signal_type"]
            chan = None if r["channel_id"] is None else str(r["channel_id"])
            reason = None
            if stype in disabled_types:
                reason = f"signal type '{stype}' disabled"
            elif chan is not None and chan in disabled_channels:
                reason = f"channel {chan} disabled"
            elif sym in excluded_syms:
                reason = "symbol excluded"
            else:
                for rule in config.excluded_trades:
                    if rule.symbol.upper() == sym and rule.signal_type in ("all", stype):
                        reason = f"excluded trade {rule.symbol}/{rule.signal_type}"
                        break
                if reason is None and config.excluded_channel_assets:
                    asset = detect_asset_class(sym).value
                    for rule in config.excluded_channel_assets:
                        chan_ok = rule.channel in ("", "all") or chan == rule.channel
                        asset_ok = rule.asset_class in ("", "all") or rule.asset_class == asset
                        if chan_ok and asset_ok:
                            reason = (
                                f"excluded channel/asset {rule.channel or 'all'}/"
                                f"{rule.asset_class or 'all'}"
                            )
                            break

            if reason is None:
                filtered.append(r)
                continue
            lid = r["limit_id"]
            if lid not in self._logged_excluded:
                logger.info(
                    "Excluded: signal_id=%d limit_id=%d symbol=%s — %s",
                    r["signal_id"],
                    lid,
                    r["instrument"],
                    reason,
                )
                self._logged_excluded.add(lid)
        return filtered

    async def run(
        self,
        supabase: SupabaseDB,
        sqlite: SQLiteDB,
        mt5_client: MT5Client,
        config: Settings,
        scheduler: MarketScheduler,
        placement_active: bool = True,
    ) -> SyncResult:
        result = SyncResult()
        self._closed_tickets.clear()

        # User per-signal overrides. 'skip' signals are pulled (pending cancelled,
        # fills closed) and never placed; 'manual' signals are orphaned — the bot
        # stops placing, cancelling, and managing them. Both gate every loop below.
        actions = await sqlite.get_signal_actions()
        skipped_sids = {sid for sid, a in actions.items() if a == "skip"}
        manual_sids = {sid for sid, a in actions.items() if a == "manual"}
        unmanaged_sids = skipped_sids | manual_sids

        # Locally-held filled signals scope the 'profit' branch of the fetch below: we
        # only ever keep profit rows for signals we still hold a position on, so the query
        # pulls just those instead of every historical profit signal (that unbounded set
        # was the pooler-egress leak). Cheap local SQLite read, so it runs every cycle.
        sqlite_active = await sqlite.get_all_active()
        filled_sids = {r["signal_id"] for r in sqlite_active if r["status"] == "filled"}

        cache_now = time.monotonic()
        # Tiny per-cycle poll: news/vol gates + the signals_rev watermark. Runs before
        # the cached fetches below because a rev change is what invalidates them.
        await self._poll_sync_state(supabase)
        signal_sets = await self._fetch_signal_sets_cached(supabase, filled_sids, cache_now)

        supabase_rows = None
        hit_limit_ids: set[int] = set()
        profit_limit_signal: dict[int, int] = {}
        if signal_sets is not None:
            supabase_rows, hit_limit_ids, profit_limit_signal = signal_sets
            supabase_rows = self._apply_exclusions(supabase_rows, config)

            # Skipped/manual signals are dropped from the working pending set so no
            # gate, stale-cancel, SL-change, or drift loop touches them. Skipped
            # pending is cancelled by _apply_skips; manual pending is left orphaned.
            sqlite_pending = [
                r
                for r in sqlite_active
                if r["status"] == "pending" and r["signal_id"] not in unmanaged_sids
            ]

            supabase_by_limit = {r["limit_id"]: r for r in supabase_rows}
            supabase_limit_ids = set(supabase_by_limit)
            sqlite_limit_ids = {r["limit_id"] for r in sqlite_active}

            # hit_limit_ids: limits the TM marked 'hit' while their signal is still live.
            # Their local pending order is held by the stale-pending sweep below — the
            # feed reached the level but our broker hasn't filled (sub-pip mismatch).
            #
            # profit_held_limit_ids: still-pending limits on a 'profit'-marked signal we
            # still hold a filled position for. The TM marking 'profit' drops the signal
            # out of the active set, but we keep its remaining entries live until our own
            # TP engine closes the trade — once we're flat the signal leaves filled_sids
            # and the limits fall back to normal stale-cancellation. Both sets come from
            # the single fetch_signal_sets round-trip above (its 'profit' branch is already
            # scoped to filled_sids, so this filter is exact rather than a pruning step).
            profit_held_limit_ids = {
                lid for lid, sid in profit_limit_signal.items() if sid in filled_sids
            }

            # Per-symbol spread-hour / news-mode gate. Crypto and 24h stocks are exempt
            # (24/7 markets). Stocks use an earlier cutoff because they close at 16:00 EST
            # — the cancel must land before then or MT5 rejects it once the session is
            # shut; everything else uses the standard daily_start.
            now = datetime.now(UTC)

            # news_mode comes from the per-cycle sync-state poll regardless of
            # placement_active so force-exits still fire while placement is paused.
            news_symbols, vol_symbols = self._gate_symbols(config)

            # 'risky' signals are disabled entirely inside their UTC windows (no
            # crypto/24h exemption — the gate is signal-type based, not instrument based).
            risky_disabled = scheduler.is_risky_disabled(now)
            # Custom shared SL per risky signal (empty when no custom SL configured).
            risky_sl_by_signal = self._risky_sl_map(supabase_rows, config)

            # Signals our own TP engine has already fired on. Once we TP a signal we
            # tear down its remaining limits and must never re-enter them, even while
            # Supabase still shows the signal active (the TM/DB can lag, or the user may
            # run a tighter TP than the channel). Durable, so a restart can't re-place.
            tp_fired_signals = await sqlite.get_tp_fired_signals()

            ctx = _CycleContext(
                config=config,
                scheduler=scheduler,
                now=now,
                cache_now=cache_now,
                unmanaged_sids=unmanaged_sids,
                filled_sids=filled_sids,
                supabase_rows=supabase_rows,
                hit_limit_ids=hit_limit_ids,
                profit_held_limit_ids=profit_held_limit_ids,
                supabase_by_limit=supabase_by_limit,
                supabase_limit_ids=supabase_limit_ids,
                sqlite_limit_ids=sqlite_limit_ids,
                sqlite_pending=sqlite_pending,
                news_symbols=news_symbols,
                vol_symbols=vol_symbols,
                risky_disabled=risky_disabled,
                risky_sl_by_signal=risky_sl_by_signal,
                tp_fired_signals=tp_fired_signals,
            )

            if placement_active:
                await self._cancel_gate_blocked_pending(ctx, sqlite, mt5_client, result)

            # Feed prices for every offset symbol in the active signal set — used for
            # placement, drift checks, SL sync, and the dashboard's "Closest Signals" view.
            ctx.offset_needed = {
                r["instrument"] for r in supabase_rows if needs_offset(r["instrument"], config)
            }
            ctx.live_prices = await self._fetch_live_prices_cached(
                supabase, ctx.offset_needed, config, cache_now
            )
            live_prices = ctx.live_prices

            if placement_active:
                # Signals whose ladder is mid-trade must not be disturbed by a
                # re-placement: bot has a local fill, OR Supabase signal_status is
                # 'hit' (the TM has marked at least one limit hit, even if MT5
                # hasn't filled yet). Shared by the resize and drift checks below;
                # placement only ever adds pendings, so it can't change this set.
                blocked_from_drift = await sqlite.get_signals_with_fills() | {
                    r["signal_id"] for r in supabase_rows if r.get("signal_status") == "hit"
                }

                await self._cancel_tp_fired_pending(ctx, sqlite, mt5_client, result)
                await self._cancel_stale_pending(ctx, sqlite, mt5_client, result)
                await self._cancel_sl_changed_pending(ctx, sqlite, mt5_client, result)
                await self._cancel_resized_pending(
                    ctx, blocked_from_drift, sqlite, mt5_client, result
                )

                new_limit_ids = await self._select_new_limits(ctx, sqlite)
                stale_feeds = await self._fetch_stale_feeds_cached(supabase, config, cache_now)

                if new_limit_ids:
                    sym_ticks, sym_infos, unavailable_mt5 = self._resolve_symbols(
                        ctx, mt5_client, new_limit_ids, result
                    )
                    stale_instruments = self._stale_offset_instruments(ctx, new_limit_ids, result)
                    new_by_signal: dict[int, list[int]] = defaultdict(list)
                    for lid in new_limit_ids:
                        new_by_signal[supabase_by_limit[lid]["signal_id"]].append(lid)
                    approved_signals = self._approve_signals(
                        ctx,
                        new_by_signal,
                        stale_feeds,
                        unavailable_mt5,
                        stale_instruments,
                        sym_ticks,
                        sym_infos,
                        result,
                    )
                    signal_lots = await self._compute_signal_lots(
                        ctx, sqlite, mt5_client, approved_signals, new_by_signal
                    )
                    await self._place_approved(
                        ctx,
                        new_limit_ids,
                        approved_signals,
                        signal_lots,
                        supabase,
                        sqlite,
                        mt5_client,
                        result,
                    )

                await self._check_offset_drift(ctx, blocked_from_drift, sqlite, mt5_client, result)
                await self._check_proximity_drift(
                    ctx, blocked_from_drift, sqlite, mt5_client, result
                )

        # Always detect fills (runs even when placement_active=False or Supabase down)
        mt5_orders = mt5_client.orders_get()
        mt5_positions = mt5_client.positions_get()
        await self._process_fills(sqlite, mt5_client, mt5_orders, mt5_positions, result)

        # One filled-positions snapshot shared by every maintenance/exit routine
        # below (a single SQLite read per cycle). self._closed_tickets tracks
        # positions closed mid-cycle so a later routine never double-closes.
        filled_rows = await sqlite.get_filled_positions()

        # Skip: pull every order/position on a user-skipped signal (cancel pending,
        # close fills). Runs even on a Supabase outage — it's driven by local state.
        await self._apply_skips(
            skipped_sids, sqlite, mt5_client, mt5_positions, mt5_orders, result, filled_rows
        )

        # Mark positions closed if they disappeared from MT5 externally
        await self._check_external_closes(sqlite, mt5_client, mt5_positions, filled_rows)

        # Disable-auto-TP: the user owns every exit, so once a signal's filled
        # positions are all closed, clear its remaining pending limits.
        if config.disable_auto_tp:
            await self._cancel_pending_after_close(
                sqlite, mt5_client, unmanaged_sids, result, filled_rows
            )

        # Strip/restore SLs around spread hour. Runs unconditionally (even on a
        # Supabase outage) so a stripped position always gets its SL back.
        await self._manage_spread_hour_sls(
            sqlite, mt5_client, mt5_positions, scheduler, config, unmanaged_sids, filled_rows
        )

        # Force-close any filled 'risky' position while a risky-disabled window is active.
        # Runs unconditionally (even on a Supabase outage) so no risky trade survives the
        # window; pending risky limits are cancelled by the placement-phase gate above.
        await self._check_risky_window_exits(
            sqlite, mt5_client, mt5_positions, scheduler, unmanaged_sids, filled_rows
        )

        if supabase_rows is not None:
            # Build signal-level lookup from supabase rows for SL sync and forced exit
            supabase_by_signal: dict[int, dict] = {}
            for row in supabase_rows:
                sid = row["signal_id"]
                if sid not in supabase_by_signal:
                    supabase_by_signal[sid] = row

            await self._sync_filled_sls(
                sqlite,
                mt5_client,
                mt5_positions,
                supabase_by_signal,
                config,
                live_prices,
                unmanaged_sids,
                filled_rows,
            )
            await self._check_forced_exits(
                supabase,
                sqlite,
                mt5_client,
                mt5_positions,
                scheduler,
                config,
                unmanaged_sids,
                filled_rows,
            )
            await self._check_news_exits(
                news_symbols,
                vol_symbols,
                sqlite,
                mt5_client,
                mt5_positions,
                config,
                unmanaged_sids,
                filled_rows,
            )
            await self._check_profit_weekend_exits(
                supabase,
                sqlite,
                mt5_client,
                mt5_positions,
                scheduler,
                config,
                unmanaged_sids,
                filled_rows,
            )

            # Snapshot for the dashboard's "Closest Signals" view. Re-query pending
            # so newly-placed orders from this cycle appear as placed=True. Instant
            # entries are left out: they are never waiting on a level, so they'd sit
            # in the watch list at zero distance for the life of the trade — the
            # position itself is what the user should see.
            sqlite_pending_now = await sqlite.get_pending_orders()
            self.last_supabase_rows = [r for r in supabase_rows if not _is_instant(r)]
            self.last_live_prices = live_prices
            self.last_sqlite_pending_limit_ids = {r["limit_id"] for r in sqlite_pending_now}

        return result

    async def _process_fills(
        self,
        sqlite: SQLiteDB,
        mt5_client: MT5Client,
        mt5_orders: list,
        mt5_positions: list,
        result: SyncResult,
    ) -> None:
        """Detect fills and partial-close remainders, updating local order state.

        A partial close replaces the MT5 position ticket; the remainder is
        re-tracked as a synthetic 'remainder' order with trailing enabled.
        """
        current_pending = await sqlite.get_pending_orders()
        fills = self._fill_detector.detect_fills(mt5_orders, mt5_positions, current_pending)
        for fill in fills:
            await sqlite.mark_filled_and_set_position_ticket(
                fill.mt5_ticket, fill.position_ticket, fill.filled_at, fill.fill_price
            )
            result.filled += 1
            logger.info("Fill: order=%d pos=%d", fill.mt5_ticket, fill.position_ticket)

        now_iso = datetime.now(UTC).isoformat()
        pos_by_ticket = {p.ticket: p for p in mt5_positions}
        new_tickets = await self._fill_detector.detect_partial_close_tickets(
            mt5_client, sqlite, mt5_positions
        )
        for evt in new_tickets:
            closed_pnl = mt5_client.get_position_realized_pnl(evt.original_ticket)
            await sqlite.mark_closed(evt.original_ticket, closed_pnl)
            remainder_pos = pos_by_ticket.get(evt.new_ticket)
            await sqlite.insert_order(
                limit_id=-evt.new_ticket,
                signal_id=evt.signal_id,
                mt5_ticket=evt.new_ticket,
                order_type="remainder",
                lot_size=0.0,
                placed_at=now_iso,
                db_stop_loss=0.0,
                signal_type=evt.signal_type,
                symbol=remainder_pos.symbol if remainder_pos else None,
            )
            await sqlite.mark_filled(evt.new_ticket, now_iso)
            await sqlite.set_trailing(evt.new_ticket)
            result.new_trailing += 1
            logger.info(
                "Partial close remainder: new_ticket=%d signal=%d (original=%d closed pnl=%s)",
                evt.new_ticket,
                evt.signal_id,
                evt.original_ticket,
                f"{closed_pnl:.2f}" if closed_pnl is not None else "?",
            )

    async def _select_new_limits(self, ctx: _CycleContext, sqlite: SQLiteDB) -> set[int]:
        """Limits present in Supabase but not yet tracked locally, after every
        placement guard: user skip/manual, the limit-count gate, already-filled
        (by limit_id and by (signal_id, price)), TP-fired signals, and the
        spread-hour / news / risky gates."""
        new_limit_ids = ctx.supabase_limit_ids - ctx.sqlite_limit_ids
        by_limit = ctx.supabase_by_limit

        # User-skipped and manually-handled signals never place — drop their
        # limits before any placement work.
        new_limit_ids = {
            lid for lid in new_limit_ids if by_limit[lid]["signal_id"] not in ctx.unmanaged_sids
        }

        # Limit-count gate: signals with too many limits have negative
        # expectancy (win size compresses as the level count grows), so they
        # are skipped at placement. Existing pendings/fills are untouched.
        skip_at = ctx.config.lot_sizing.skip_limits_at
        if skip_at > 0:
            too_many = {
                lid for lid in new_limit_ids if (by_limit[lid].get("total_limits") or 0) >= skip_at
            }
            for lid in too_many:
                sid = by_limit[lid]["signal_id"]
                if sid not in self._logged_limit_skips:
                    self._logged_limit_skips.add(sid)
                    logger.info(
                        "Skipping signal=%d (%s): %d limits >= skip_limits_at=%d",
                        sid,
                        by_limit[lid]["instrument"],
                        by_limit[lid].get("total_limits") or 0,
                        skip_at,
                    )
            new_limit_ids -= too_many

        # Never re-place a limit that has already filled on our end. A limit can
        # fill on our broker while the TM still shows it pending (sub-pip mismatch,
        # or the TM bot was down); once our position TPs/closes its SQLite row goes
        # to 'closed', drops out of get_all_active(), and would otherwise reappear
        # here as a "new" limit — re-entering the exact same level on a loop. The
        # 'closed' row is the durable, restart-safe marker that the limit hit.
        already_filled = new_limit_ids & await sqlite.get_filled_limit_ids()
        new_limit_ids -= already_filled
        for lid in already_filled:
            if lid in self._logged_already_filled:
                continue
            self._logged_already_filled.add(lid)
            row = by_limit[lid]
            logger.warning(
                "Skipping re-placement of limit_id=%d signal_id=%d (%s) — already "
                "filled on our end but still active in DB (TM/DB out of sync)",
                lid,
                row["signal_id"],
                row["instrument"],
            )

        # Same guard, by (signal_id, price_level): a TM message edit rebuilds the
        # signal's limit rows with fresh IDENTITY ids, so a level we already filled
        # reappears under a new limit_id and slips past the limit_id check above.
        # Never re-enter a (signal_id, price) we've already filled/closed.
        filled_prices = await sqlite.get_filled_signal_prices()
        if filled_prices:
            refilled = {
                lid
                for lid in new_limit_ids
                if any(
                    math.isclose(float(by_limit[lid]["price_level"]), p, rel_tol=1e-9)
                    for p in filled_prices.get(by_limit[lid]["signal_id"], ())
                )
            }
            new_limit_ids -= refilled
            for lid in refilled:
                if lid in self._logged_already_filled:
                    continue
                self._logged_already_filled.add(lid)
                row = by_limit[lid]
                logger.warning(
                    "Skipping re-placement of limit_id=%d signal_id=%d (%s) — a limit at "
                    "price %.5f already filled on our end (TM regenerated limit_id on edit)",
                    lid,
                    row["signal_id"],
                    row["instrument"],
                    float(row["price_level"]),
                )

        # Never re-place a limit on a signal our own TP engine has fired on.
        # The local cancel + this guard together mean a TP'd signal's siblings
        # stay down regardless of how long the TM/DB takes to mark it profit.
        tp_blocked = {
            lid for lid in new_limit_ids if by_limit[lid]["signal_id"] in ctx.tp_fired_signals
        }
        new_limit_ids -= tp_blocked
        for lid in tp_blocked:
            if lid in self._logged_already_filled:
                continue
            self._logged_already_filled.add(lid)
            row = by_limit[lid]
            logger.warning(
                "Skipping re-placement of limit_id=%d signal_id=%d (%s) — our TP "
                "engine already fired on this signal; not re-entering",
                lid,
                row["signal_id"],
                row["instrument"],
            )

        # An instant entry is only the trade that was posted for as long as the market
        # is still roughly where the TM saw it. Past that the row keeps coming back in
        # the fetch (its signal stays live until TP/SL), so it must be dropped on age.
        # A NULL hit_time is the sub-second gap between the TM inserting the limit and
        # marking it hit: not stale, just not ready, so it waits for the next cycle.
        instant_ages = {
            lid: (
                None
                if by_limit[lid]["hit_time"] is None
                else (ctx.now - by_limit[lid]["hit_time"]).total_seconds()
            )
            for lid in new_limit_ids
            if _is_instant(by_limit[lid])
        }
        new_limit_ids -= {
            lid
            for lid, age in instant_ages.items()
            if age is None or age > _INSTANT_MAX_AGE_SECONDS
        }
        for lid, age in instant_ages.items():
            if age is None or age <= _INSTANT_MAX_AGE_SECONDS:
                continue
            if lid in self._logged_stale_instant:
                continue
            self._logged_stale_instant.add(lid)
            row = by_limit[lid]
            logger.info(
                "Skipping instant entry limit_id=%d signal_id=%d (%s) — recorded %.0fs ago, "
                "past the %.0fs entry window",
                lid,
                row["signal_id"],
                row["instrument"],
                age,
                _INSTANT_MAX_AGE_SECONDS,
            )

        # Drop blocked symbols (spread hour / weekend / news mode) from the
        # pre-check phase — otherwise tick/proximity/live-price-staleness checks
        # fire (and spam WARN logs) for symbols the placement loop would skip.
        return {
            lid
            for lid in new_limit_ids
            if not ctx.is_blocked(by_limit[lid]["instrument"], by_limit[lid]["signal_type"])
        }

    def _resolve_symbols(
        self,
        ctx: _CycleContext,
        mt5_client: MT5Client,
        new_limit_ids: set[int],
        result: SyncResult,
    ) -> tuple[dict, dict, set[str]]:
        """Resolve each new limit's MT5 symbol against the broker's catalogue and
        fetch its tick/info.

        Symbols the broker doesn't carry (e.g. GCZ26_CFD) are skipped cleanly and
        logged once for their lifetime — no retry churn. Catalogued-but-hidden
        symbols are selected into MarketWatch first. Symbols whose tick failed
        recently are cooldown-suppressed (no MT5 call, no log); on first failure /
        after cooldown we warn once. Returns (sym_ticks, sym_infos,
        unavailable_mt5) keyed by MT5 symbol.
        """
        config = ctx.config
        unique_syms: dict[str, str] = {}
        for lid in new_limit_ids:
            db_sym = ctx.supabase_by_limit[lid]["instrument"]
            unique_syms[map_symbol(db_sym, config)] = db_sym

        sym_ticks: dict = {}
        sym_infos: dict = {}
        newly_unavailable: set[str] = set()
        broker_symbols = mt5_client.symbols_get()
        now_mono = time.monotonic()
        for mt5_sym in list(unique_syms):
            if now_mono < self._unavailable_until.get(mt5_sym, 0.0):
                sym_ticks[mt5_sym] = None
                sym_infos[mt5_sym] = None
                continue

            # Catalogue check (skipped if symbols_get is unavailable this cycle).
            if broker_symbols and mt5_sym not in broker_symbols:
                base_sym = (
                    mt5_sym[: -len(config.stock_suffix)]
                    if config.stock_suffix and mt5_sym.endswith(config.stock_suffix)
                    else None
                )
                if base_sym and base_sym in broker_symbols:
                    # Broker lists the bare stock symbol, not the suffixed one.
                    db_sym = unique_syms[mt5_sym]
                    logger.info(
                        "Stock suffix fallback: %s not in catalogue, using %s",
                        mt5_sym,
                        base_sym,
                    )
                    _persist_stock_no_suffix(db_sym, config)
                    unique_syms[base_sym] = db_sym
                    del unique_syms[mt5_sym]
                    mt5_sym = base_sym
                else:
                    # Instrument simply isn't offered by this broker — skip.
                    sym_ticks[mt5_sym] = None
                    sym_infos[mt5_sym] = None
                    if mt5_sym not in self._logged_unmapped:
                        logger.info(
                            "Symbol %s not offered by this broker — skipping "
                            "(map it under Settings if it exists by another name)",
                            mt5_sym,
                        )
                        self._logged_unmapped.add(mt5_sym)
                    continue

            # Catalogued (or catalogue unknown) — ensure it's in MarketWatch.
            mt5_client.symbol_select(mt5_sym)
            tick = mt5_client.symbol_info_tick(mt5_sym)

            sym_ticks[mt5_sym] = tick
            sym_infos[mt5_sym] = mt5_client.symbol_info(mt5_sym) if tick is not None else None
            if tick is None:
                self._unavailable_until[mt5_sym] = now_mono + _UNAVAILABLE_COOLDOWN
                newly_unavailable.add(mt5_sym)
            else:
                self._unavailable_until.pop(mt5_sym, None)

        # Log and count errors only for symbols newly detected as unavailable.
        # Cooldown-suppressed symbols are skipped silently this cycle.
        unavailable_mt5: set[str] = set()
        for mt5_sym in unique_syms:
            if sym_ticks[mt5_sym] is None:
                unavailable_mt5.add(mt5_sym)
                if mt5_sym in newly_unavailable:
                    count = sum(
                        1
                        for lid in new_limit_ids
                        if map_symbol(ctx.supabase_by_limit[lid]["instrument"], config) == mt5_sym
                    )
                    logger.warning(
                        "Symbol not in terminal: %s — skipping %d limit(s) (retrying in %.0fs)",
                        mt5_sym,
                        count,
                        _UNAVAILABLE_COOLDOWN,
                    )
                    result.errors += count

        return sym_ticks, sym_infos, unavailable_mt5

    def _stale_offset_instruments(
        self, ctx: _CycleContext, new_limit_ids: set[int], result: SyncResult
    ) -> set[str]:
        """Offset instruments whose feed has gone fully dark (log once per episode).

        An old updated_at is normal for an idle feed; only a feed beyond the
        dead-feed bound is skipped — the offset itself is anchored to updated_at,
        not to "now".
        """
        stale_instruments: set[str] = set()
        now_utc = datetime.now(UTC)
        for instrument in ctx.offset_needed:
            live_row = ctx.live_prices.get(instrument)
            if live_row is None:
                continue
            age = (now_utc - live_row["updated_at"]).total_seconds()
            if age > ctx.config.feed_max_staleness_seconds:
                count = sum(
                    1
                    for lid in new_limit_ids
                    if ctx.supabase_by_limit[lid]["instrument"] == instrument
                )
                if count:
                    if instrument not in self._logged_dark_feeds:
                        logger.warning(
                            "Live price dark for %s (%.0fs old) — skipping %d limit(s)",
                            instrument,
                            age,
                            count,
                        )
                        self._logged_dark_feeds.add(instrument)
                    result.errors += count
                stale_instruments.add(instrument)
            else:
                self._logged_dark_feeds.discard(instrument)
        # Drop feeds no longer in the active set so a re-listed signal warns
        # afresh and the set stays bounded.
        self._logged_dark_feeds &= ctx.offset_needed
        return stale_instruments

    def _approve_signals(
        self,
        ctx: _CycleContext,
        new_by_signal: dict[int, list[int]],
        stale_feeds: set[str],
        unavailable_mt5: set[str],
        stale_instruments: set[str],
        sym_ticks: dict,
        sym_infos: dict,
        result: SyncResult,
    ) -> set[int]:
        """Approve signals whose ladder passes availability, feed-health, and
        proximity checks. Approval is per signal, not per limit — a ladder is
        placed or skipped as one unit."""
        config = ctx.config
        approved_signals: set[int] = set()
        for sig_id, lids in new_by_signal.items():
            row0 = ctx.supabase_by_limit[lids[0]]
            db_sym = row0["instrument"]
            mt5_sym = map_symbol(db_sym, config)
            if mt5_sym in unavailable_mt5:
                continue  # errors already counted by _resolve_symbols
            if needs_offset(db_sym, config) and db_sym in stale_instruments:
                continue  # errors already counted by _stale_offset_instruments
            feed = _feed_for_symbol(db_sym, config, ctx.live_prices)
            if feed is not None and feed in stale_feeds:
                result.skipped += len(lids)
                logger.info(
                    "Signal %d (%s): feed=%s is stale — skipping %d limit(s)",
                    sig_id,
                    db_sym,
                    feed,
                    len(lids),
                )
                continue
            tick = sym_ticks.get(mt5_sym)
            info = sym_infos.get(mt5_sym)
            if tick is None or info is None:
                result.errors += len(lids)
                logger.warning(
                    "Signal %d (%s): tick/info unavailable — skipping %d limit(s)",
                    sig_id,
                    db_sym,
                    len(lids),
                )
                continue
            # Proximity is a feed-frame question: the limit prices are feed
            # prices, so compare against the feed mid for offset symbols. Using
            # the broker mid here would be off by the whole offset.
            if needs_offset(db_sym, config):
                live_row = ctx.live_prices.get(db_sym)
                if live_row is None:
                    result.errors += len(lids)
                    logger.warning(
                        "Signal %d (%s): no live price for proximity — skipping %d limit(s)",
                        sig_id,
                        db_sym,
                        len(lids),
                    )
                    continue
                mid = (float(live_row["bid"]) + float(live_row["ask"])) / 2
            else:
                mid = (tick.bid + tick.ask) / 2
            new_prices = [float(ctx.supabase_by_limit[lid]["price_level"]) for lid in lids]
            # An instant entry's price_level is the fill the TM recorded, not a level to
            # wait for, so the band is halved: we're measuring how far price has already
            # slipped from that entry, not how soon a resting limit might be reached.
            multiplier = _INSTANT_PROXIMITY_MULTIPLIER if _is_instant(row0) else 1.0
            if _within_proximity(
                new_prices,
                mid,
                detect_asset_class(db_sym),
                info,
                config.proximity,
                db_sym,
                multiplier,
            ):
                approved_signals.add(sig_id)
                self._logged_proximity.discard(sig_id)
            else:
                result.skipped += len(lids)
                if sig_id not in self._logged_proximity:
                    logger.info(
                        "Signal %d (%s): all limits outside proximity — skipping %d order(s)",
                        sig_id,
                        db_sym,
                        len(lids),
                    )
                    self._logged_proximity.add(sig_id)
        return approved_signals

    async def _compute_signal_lots(
        self,
        ctx: _CycleContext,
        sqlite: SQLiteDB,
        mt5_client: MT5Client,
        approved_signals: set[int],
        new_by_signal: dict[int, list[int]],
    ) -> dict[int, float]:
        """Compute lot once per approved signal (not once per limit).

        The ladder split uses the signal's declared total_limits, so a limit
        placed before the TM has inserted the rest of its ladder is still sized
        against the full ladder. If the signal already has filled siblings, reuse
        their placement lot rather than recomputing: the risk_percent inputs
        (balance, and the SL distances of the limits still visible after the fetch
        drops hit ones) drift, and a re-placed limit should match the size its
        siblings went on at.
        """
        by_signal: dict[int, list] = defaultdict(list)
        for row in ctx.supabase_rows:
            by_signal[row["signal_id"]].append(row)
        lot_calc = LotCalculator(mt5_client, ctx.config)

        filled_lots = await sqlite.get_signal_filled_lots()
        signal_lots: dict[int, float] = {}
        for sig_id in approved_signals:
            prior_lot = filled_lots.get(sig_id)
            if prior_lot is not None:
                signal_lots[sig_id] = prior_lot
                logger.info(
                    "Signal %d: reusing filled-sibling lot=%.2f for %d re-placed limit(s)",
                    sig_id,
                    prior_lot,
                    len(new_by_signal[sig_id]),
                )
                continue
            lids = new_by_signal[sig_id]
            row0 = ctx.supabase_by_limit[lids[0]]
            all_prices = [float(r["price_level"]) for r in by_signal[sig_id]]
            mt5_sym = map_symbol(row0["instrument"], ctx.config)
            risky_sl = ctx.risky_sl_by_signal.get(sig_id)
            sl_for_lot = risky_sl if risky_sl is not None else float(row0["stop_loss"])
            signal_lots[sig_id] = lot_calc.calculate(
                sl_for_lot,
                all_prices,
                mt5_sym,
                row0["signal_type"] or "standard",
                channel_id=row0["channel_id"],
                ladder_size=row0["total_limits"],
            )
        return signal_lots

    async def _place_approved(
        self,
        ctx: _CycleContext,
        new_limit_ids: set[int],
        approved_signals: set[int],
        signal_lots: dict[int, float],
        supabase: SupabaseDB,
        sqlite: SQLiteDB,
        mt5_client: MT5Client,
        result: SyncResult,
    ) -> None:
        """Place every new limit belonging to an approved signal."""
        config = ctx.config
        for lid in new_limit_ids:
            row = ctx.supabase_by_limit[lid]
            sig_id = row["signal_id"]
            if sig_id not in approved_signals:
                continue

            db_sym = row["instrument"]
            if ctx.is_blocked(db_sym, row["signal_type"]):
                continue
            mt5_sym = map_symbol(db_sym, config)
            lot = signal_lots[sig_id]

            offset: float | None = None
            if needs_offset(db_sym, config):
                live_row = ctx.live_prices.get(db_sym)
                if live_row is None:
                    logger.warning("No live price for %s, skipping limit=%d", db_sym, lid)
                    result.errors += 1
                    continue
                offset = self._offset_calc.get_offset(
                    mt5_sym,
                    live_row,
                    mt5_client,
                    config.feed_max_staleness_seconds,
                    config.offset_recompute_interval_seconds,
                )
                if offset is None:
                    result.errors += 1
                    continue

            risky_sl = ctx.risky_sl_by_signal.get(sig_id)
            db_sl = risky_sl if risky_sl is not None else float(row["stop_loss"])

            if _is_instant(row):
                outcome = await self._placer.place_market_order(
                    signal_id=sig_id,
                    limit_id=lid,
                    direction=row["direction"],
                    db_stop_loss=db_sl,
                    db_take_profit=float(row["take_profit"]),
                    db_price=float(row["price_level"]),
                    signal_type=row["signal_type"] or "standard",
                    mt5_symbol=mt5_sym,
                    lot=lot,
                    offset=offset,
                    mt5_client=mt5_client,
                    sqlite=sqlite,
                    supabase=supabase,
                    channel_id=row["channel_id"],
                    sequence_number=row["sequence_number"],
                )
            else:
                outcome = await self._placer.place_order(
                    signal_id=sig_id,
                    limit_id=lid,
                    direction=row["direction"],
                    db_stop_loss=db_sl,
                    db_price=float(row["price_level"]),
                    signal_type=row["signal_type"] or "standard",
                    mt5_symbol=mt5_sym,
                    lot=lot,
                    offset=offset,
                    mt5_client=mt5_client,
                    sqlite=sqlite,
                    supabase=supabase,
                    channel_id=row["channel_id"],
                    sequence_number=row["sequence_number"],
                    ladder_size=row["total_limits"],
                )
            if outcome == "placed":
                result.placed += 1
            elif outcome == "skipped":
                result.skipped += 1
            else:
                result.errors += 1

    async def _check_offset_drift(
        self,
        ctx: _CycleContext,
        blocked_from_drift: set[int],
        sqlite: SQLiteDB,
        mt5_client: MT5Client,
        result: SyncResult,
    ) -> None:
        """Cancel pending orders whose broker-vs-feed offset has drifted so they
        re-place next cycle at a fresh offset.

        Skipped entirely for signals whose other limits have already hit —
        re-placing the survivors at a fresh offset leaves the already-filled (or
        TM-marked-hit) limits at the old offset, producing inconsistent entries
        across the same signal.
        """
        config = ctx.config
        now_drift = datetime.now(UTC)
        drift_interval = config.offset_drift_check_interval_seconds
        for row in ctx.sqlite_pending:
            if row["mt5_ticket"] in ctx.repriced_tickets:
                continue
            if row["offset_at_placement"] is None:
                continue
            if row["limit_id"] not in ctx.supabase_by_limit:
                continue
            if row["signal_id"] in blocked_from_drift:
                continue
            last_check = row["last_offset_check"]
            if last_check:
                try:
                    prev = datetime.fromisoformat(last_check)
                    if (now_drift - prev).total_seconds() < drift_interval:
                        continue
                except ValueError:
                    pass
            instrument = ctx.supabase_by_limit[row["limit_id"]]["instrument"]
            mt5_symbol = map_symbol(instrument, config)
            live_row = ctx.live_prices.get(instrument)
            if live_row is None:
                continue
            current_offset = self._offset_calc.get_offset(
                mt5_symbol,
                live_row,
                mt5_client,
                config.feed_max_staleness_seconds,
                config.offset_recompute_interval_seconds,
            )
            if current_offset is None:
                continue
            await sqlite.update_last_offset_check(row["mt5_ticket"], now_drift.isoformat())
            threshold = offset_drift_threshold(
                detect_asset_class(instrument), config.offset_drift, instrument
            )
            if self._offset_calc.check_drift(
                current_offset, float(row["offset_at_placement"]), threshold
            ):
                logger.info(
                    "Offset drift: %s ticket=%d, cancelling for re-placement",
                    instrument,
                    row["mt5_ticket"],
                )
                ok = await self._canceller.cancel_order(
                    row["mt5_ticket"], mt5_client, sqlite, spread=False
                )
                result.cancelled += ok
                result.errors += not ok
                ctx.repriced_tickets.add(row["mt5_ticket"])

    async def _check_proximity_drift(
        self,
        ctx: _CycleContext,
        blocked_from_drift: set[int],
        sqlite: SQLiteDB,
        mt5_client: MT5Client,
        result: SyncResult,
    ) -> None:
        """Cancel active pendings that have walked outside their proximity threshold.

        Proximity is otherwise only a placement gate, so a limit placed while near
        price — or before the threshold was tightened — would sit forever as the
        market moves away. Cancelled here it re-enters the placement proximity
        check next cycle, re-placing only if price returns. Signals with fills (or
        DB-hit) are spared, exactly as offset drift does: their ladder is mid-trade
        and must not be disturbed.

        The cancel distance is _PROXIMITY_EXIT_MULTIPLIER x the placement threshold,
        never the threshold itself: price hovering right at it would otherwise
        cancel and re-place the same ladder on alternating cycles.

        Evaluated per signal as one unit — same min-distance rule placement uses
        (_within_proximity over all the signal's limit prices). A ladder is either
        in proximity (keep every limit) or out (cancel every limit) together. A
        per-limit check here would fight placement: placement arms the whole ladder
        whenever its closest limit is near, so cancelling only the farther limits
        leaves the closest pending, then the closest drifting out re-arms the whole
        ladder next cycle — an endless place-7/cancel-6 churn.
        """
        config = ctx.config
        drift_by_signal: dict[int, list] = defaultdict(list)
        for row in ctx.sqlite_pending:
            if row["mt5_ticket"] in ctx.repriced_tickets:
                continue
            if row["limit_id"] not in ctx.supabase_by_limit:
                continue
            if row["signal_id"] in blocked_from_drift:
                continue
            drift_by_signal[row["signal_id"]].append(row)

        for sig_rows in drift_by_signal.values():
            db_sym = ctx.supabase_by_limit[sig_rows[0]["limit_id"]]["instrument"]
            mt5_sym = map_symbol(db_sym, config)
            info = mt5_client.symbol_info(mt5_sym)
            if info is None:
                continue
            if needs_offset(db_sym, config):
                live_row = ctx.live_prices.get(db_sym)
                if live_row is None:
                    continue
                mid = (float(live_row["bid"]) + float(live_row["ask"])) / 2
            else:
                tick = mt5_client.symbol_info_tick(mt5_sym)
                if tick is None:
                    continue
                mid = (tick.bid + tick.ask) / 2
            prices = [float(ctx.supabase_by_limit[r["limit_id"]]["price_level"]) for r in sig_rows]
            if _within_proximity(
                prices,
                mid,
                detect_asset_class(db_sym),
                info,
                config.proximity,
                db_sym,
                _PROXIMITY_EXIT_MULTIPLIER,
            ):
                continue
            logger.info(
                "Proximity drift: %s signal=%d (mid %.5f) — cancelling %d limit(s)",
                db_sym,
                sig_rows[0]["signal_id"],
                mid,
                len(sig_rows),
            )
            for row in sig_rows:
                ok = await self._canceller.cancel_order(
                    row["mt5_ticket"], mt5_client, sqlite, spread=False
                )
                result.cancelled += ok
                result.errors += not ok
                ctx.repriced_tickets.add(row["mt5_ticket"])

    async def _fetch_signal_sets_cached(
        self, supabase: SupabaseDB, filled_sids: set[int], cache_now: float
    ) -> tuple[list, set[int], dict[int, int]] | None:
        """Active-signal fetch behind the rev-gated egress cache.

        The sync-state poll drops the cache whenever the signals_rev watermark
        moves, so a refetch here means the set actually changed; the max-age is
        only a safety net (and the legacy interval when the watermark is absent).
        A change in filled_sids also invalidates the cache so a freshly-filled
        'profit' signal's remaining limits are spared immediately. On a fetch
        failure returns None — the caller skips the placement phase and runs
        fill detection only.
        """
        max_age = (
            _SIGNAL_SETS_MAX_AGE if self._signals_rev is not None else _SIGNAL_SETS_MAX_AGE_LEGACY
        )
        signal_sets = self._signal_sets_cache
        if (
            signal_sets is not None
            and filled_sids == self._signal_sets_cache_sids
            and (cache_now - self._signal_sets_cache_at) < max_age
        ):
            return signal_sets
        try:
            signal_sets = await supabase.fetch_signal_sets(list(filled_sids))
        except Exception:
            logger.error(
                "Supabase fetch failed — skipping placement phase, running fill detection only",
                exc_info=True,
            )
            return None
        self._signal_sets_cache = signal_sets
        self._signal_sets_cache_at = cache_now
        self._signal_sets_cache_sids = filled_sids
        return signal_sets

    async def _poll_sync_state(self, supabase: SupabaseDB) -> None:
        """One tiny bot_mode_status row per cycle: the news/vol gate tokens plus the
        signals_rev watermark, which the TM-side triggers bump on every signals/limits
        write. A rev change drops the signal-set and status caches so the next read
        refetches immediately — that change-driven refetch is what lets the heavy
        queries run long fallback max-ages instead of re-pulling every few seconds.
        A failed poll leaves signal-set freshness unverifiable, so that cache is
        dropped too (the refetch either succeeds or skips the placement phase, the
        pre-watermark failure behavior); the status cache is kept — force-exits
        deliberately ride out a pooler blip on the last-known snapshot.
        """
        try:
            news_mode, vol_guard, rev = await supabase.fetch_sync_state()
        except Exception:
            logger.error("Failed to fetch sync state", exc_info=True)
            self._signal_sets_cache = None
            return
        self._gates_cache = (news_mode, vol_guard)
        if rev != self._signals_rev:
            self._signals_rev = rev
            self._signal_sets_cache = None
            self._status_cache = None

    def _gate_symbols(self, config: Settings) -> tuple[frozenset[str], frozenset[str]]:
        """(news, volatility) gate tokens from the last sync-state poll.

        news_mode is per-symbol: a comma-separated list of currency/asset tokens
        (or 'ALL'), NULL when there's no news. The volatility guard (vol_guard)
        shares the same token format and matching, but its tokens are whole DB
        instruments (e.g. 'EURUSD', 'NAS100USD', 'BTCUSDT') plus 'ALL' for gold.
        They stay in separate sets because the 24/7 exemption applies to news
        only — see _gated_by_news_or_vol. Empty when the user hasn't opted in.
        """
        if self._gates_cache is None:
            return frozenset(), frozenset()
        news_mode_raw, vol_guard_raw = self._gates_cache
        vol = parse_news_symbols(vol_guard_raw) if config.volatility_guard else frozenset()
        return parse_news_symbols(news_mode_raw), vol

    async def _fetch_live_prices_cached(
        self, supabase: SupabaseDB, offset_needed: set[str], config: Settings, cache_now: float
    ) -> dict:
        """Feed prices for the offset symbols, behind an egress-guard cache.

        Refetch only when the interval lapses OR a newly-appeared offset symbol
        isn't cached yet (so a brand-new signal is priced immediately, no added
        latency). "Missing" is measured against the last requested set, not the
        returned rows: an offset symbol the feed never publishes has no row but
        was still asked for, so it must not force a refetch every cycle. A fetch
        failure reuses the last snapshot.
        """
        if not offset_needed:
            return {}
        missing = offset_needed - self._live_prices_requested
        if missing or (
            (cache_now - self._live_prices_cache_at) >= config.polling.live_price_interval_seconds
        ):
            try:
                self._live_prices_cache = await supabase.fetch_live_prices(list(offset_needed))
                self._live_prices_cache_at = cache_now
                self._live_prices_requested = set(offset_needed)
            except Exception:
                logger.error("Live prices fetch failed", exc_info=True)
        return self._live_prices_cache

    async def _fetch_stale_feeds_cached(
        self, supabase: SupabaseDB, config: Settings, cache_now: float
    ) -> set[str]:
        """TM feeds currently marked down, behind an egress-guard cache.

        Feed health flips only on feed degradation, so it's cached and refreshed
        on feed_health_interval_seconds rather than every cycle. After the first
        fetch failure the check is disabled for the process lifetime.
        """
        if self._feed_health_failed:
            return set()
        if (cache_now - self._feed_health_cache_at) >= config.polling.feed_health_interval_seconds:
            try:
                feed_health = await supabase.fetch_feed_health()
                self._stale_feeds_cache = {
                    feed for feed, status in feed_health.items() if status == "down"
                }
                self._feed_health_cache_at = cache_now
                if self._stale_feeds_cache:
                    logger.warning("Stale feeds detected: %s", self._stale_feeds_cache)
            except Exception:
                logger.warning("feed_health unavailable — skipping feed staleness checks")
                self._feed_health_failed = True
                return set()
        return self._stale_feeds_cache

    async def _cancel_tp_fired_pending(
        self, ctx: _CycleContext, sqlite: SQLiteDB, mt5_client: MT5Client, result: SyncResult
    ) -> None:
        """Cancel any still-pending order on a TP-fired signal.

        Safety net: the TP engine cancels these the moment it fires; this
        re-cancels if that order send failed, so a TP'd signal can never leave a
        live entry behind.
        """
        if not ctx.tp_fired_signals:
            return
        for row in ctx.sqlite_pending:
            if row["signal_id"] not in ctx.tp_fired_signals:
                continue
            ok = await self._canceller.cancel_order(
                row["mt5_ticket"], mt5_client, sqlite, spread=False
            )
            result.cancelled += ok
            result.errors += not ok
        ctx.sqlite_pending = [
            r for r in ctx.sqlite_pending if r["signal_id"] not in ctx.tp_fired_signals
        ]

    async def _cancel_stale_pending(
        self, ctx: _CycleContext, sqlite: SQLiteDB, mt5_client: MT5Client, result: SyncResult
    ) -> None:
        """Cancel pending orders whose limit is gone from Supabase.

        A limit the TM marked 'hit' on a still-live signal is spared: hold the
        order so a sub-pip price mismatch still fills. Genuine cancels/closes drop
        the signal out of hit_limit_ids, so those orders are still cancelled.
        Pending limits on a 'profit'-marked signal we still hold a position for
        are likewise spared, so the remaining entries keep filling until our own
        TP engine closes the trade (profit_held_limit_ids).
        """
        for row in ctx.sqlite_pending:
            lid = row["limit_id"]
            if (
                lid in ctx.supabase_limit_ids
                or lid in ctx.hit_limit_ids
                or lid in ctx.profit_held_limit_ids
            ):
                continue
            ok = await self._canceller.cancel_order(
                row["mt5_ticket"], mt5_client, sqlite, spread=False
            )
            result.cancelled += ok
            result.errors += not ok

    async def _cancel_sl_changed_pending(
        self, ctx: _CycleContext, sqlite: SQLiteDB, mt5_client: MT5Client, result: SyncResult
    ) -> None:
        """Cancel pending orders whose signal SL changed (re-places next cycle).

        For a risky signal with a custom SL the reference is the recomputed
        deepest-limit SL, not the DB stop-loss (which we deliberately override).
        """
        for row in ctx.sqlite_pending:
            lid = row["limit_id"]
            if lid not in ctx.supabase_by_limit:
                continue
            stored_sl = row["db_stop_loss"]
            if stored_sl is None:
                continue
            risky_sl = ctx.risky_sl_by_signal.get(row["signal_id"])
            current_sl = (
                risky_sl if risky_sl is not None else float(ctx.supabase_by_limit[lid]["stop_loss"])
            )
            mt5_sym = map_symbol(ctx.supabase_by_limit[lid]["instrument"], ctx.config)
            sym = mt5_client.symbol_info(mt5_sym)
            if sym is None:
                continue
            pip_sz = pip_size(sym)
            if pip_sz > 0 and abs(current_sl - stored_sl) >= pip_sz:
                logger.info(
                    "SL change on pending: ticket=%d sl %.5f -> %.5f — cancelling for re-placement",
                    row["mt5_ticket"],
                    stored_sl,
                    current_sl,
                )
                ok = await self._canceller.cancel_order(
                    row["mt5_ticket"], mt5_client, sqlite, spread=False
                )
                result.cancelled += ok
                result.errors += not ok
                ctx.repriced_tickets.add(row["mt5_ticket"])

    async def _cancel_resized_pending(
        self,
        ctx: _CycleContext,
        blocked_from_drift: set[int],
        sqlite: SQLiteDB,
        mt5_client: MT5Client,
        result: SyncResult,
    ) -> None:
        """Cancel pendings whose signal's ladder changed size (re-places next cycle).

        A TM message edit rewrites signals.total_limits to the edited level count
        and inserts only the genuinely new levels, so a 4-limit ladder edited to 5
        leaves four orders sized at total/4 sitting next to one at total/5. Both
        ladder-splitting modes divide by that count, so the survivors have to be
        re-placed against the new one for the signal to hold its budget.

        Keyed on the placement-time ladder_size rather than on a recomputed lot:
        the risk_percent inputs (balance, SL distances) drift on their own, and
        re-placing a live ladder because the balance ticked over a volume step is
        churn nobody asked for. Signals with a fill (or DB-hit) are spared as they
        are everywhere else — their lot is pinned to the filled sibling, so
        re-placing could not resize them anyway. Rows placed before the column
        existed carry NULL and are left alone, so updating the bot doesn't
        re-place every open ladder on the first cycle.
        """
        for row in ctx.sqlite_pending:
            if row["mt5_ticket"] in ctx.repriced_tickets:
                continue
            sup = ctx.supabase_by_limit.get(row["limit_id"])
            if sup is None or row["signal_id"] in blocked_from_drift:
                continue
            placed, current = row["ladder_size"], sup["total_limits"]
            if placed is None or current is None or placed == current:
                continue
            logger.info(
                "Ladder resize: signal=%d ticket=%d limits %d -> %d — cancelling for re-placement",
                row["signal_id"],
                row["mt5_ticket"],
                placed,
                current,
            )
            ok = await self._canceller.cancel_order(
                row["mt5_ticket"], mt5_client, sqlite, spread=False
            )
            result.cancelled += ok
            result.errors += not ok
            ctx.repriced_tickets.add(row["mt5_ticket"])

    async def _cancel_gate_blocked_pending(
        self, ctx: _CycleContext, sqlite: SQLiteDB, mt5_client: MT5Client, result: SyncResult
    ) -> None:
        """Cancel pending orders blocked by the spread-hour / news / risky gates.

        Blocked rows are dropped from ctx.sqlite_pending so downstream loops
        (stale-pending, SL change, offset drift) skip the just-cancelled orders.
        ctx.sqlite_limit_ids is intentionally NOT refreshed: the cancelled limits
        should still be treated as 'known' (not as new placement candidates).
        """
        kept: list = []
        blocked_rows: list = []
        for row in ctx.sqlite_pending:
            (blocked_rows if ctx.row_blocked(row) else kept).append(row)
        for row in blocked_rows:
            ok = await self._canceller.cancel_order(
                row["mt5_ticket"], mt5_client, sqlite, spread=True
            )
            result.cancelled += ok
            result.errors += not ok
        if blocked_rows:
            logger.info("Spread/news/risky gate cancelled %d pending order(s)", len(blocked_rows))
            ctx.sqlite_pending = kept

    async def _sync_filled_sls(
        self,
        sqlite: SQLiteDB,
        mt5_client: MT5Client,
        mt5_positions: list,
        supabase_by_signal: dict[int, dict],
        config: Settings,
        live_prices: dict,
        unmanaged_sids: set[int],
        filled_rows: list | None = None,
    ) -> None:
        filled = filled_rows if filled_rows is not None else await sqlite.get_filled_positions()
        if not filled:
            return

        pos_by_ticket = {p.ticket: p for p in mt5_positions}

        for row in filled:
            if row["mt5_ticket"] in self._closed_tickets:
                continue
            if row["signal_id"] in unmanaged_sids:
                continue
            if row["is_trailing"]:
                continue
            # SL deliberately removed for spread-hour protection — don't re-apply it
            # here even if the signal's DB stop-loss changed mid-window.
            if row["sl_stripped"]:
                continue
            # Risky signal on a user-defined custom SL: the placed SL is derived from the
            # deepest limit, not the DB stop-loss, so never sync it back to the DB value.
            if row["signal_type"] == "risky" and config.tp_config.risky.stop_loss is not None:
                continue

            ticket = row["mt5_ticket"]
            if ticket not in pos_by_ticket:
                continue

            signal_id = row["signal_id"]
            sig = supabase_by_signal.get(signal_id)
            if sig is None:
                continue

            stored_db_sl = row["db_stop_loss"]
            if stored_db_sl is None:
                continue

            current_db_sl = float(sig["stop_loss"])
            instrument = sig["instrument"]
            mt5_sym = map_symbol(instrument, config)

            sym = mt5_client.symbol_info(mt5_sym)
            if sym is None:
                continue
            pip_sz = pip_size(sym)
            if pip_sz <= 0:
                continue

            if abs(current_db_sl - stored_db_sl) < pip_sz:
                continue

            offset = 0.0
            if needs_offset(instrument, config):
                live_row = live_prices.get(instrument)
                if live_row is not None:
                    current_offset = self._offset_calc.get_offset(
                        mt5_sym,
                        live_row,
                        mt5_client,
                        config.feed_max_staleness_seconds,
                        config.offset_recompute_interval_seconds,
                    )
                    offset = (
                        current_offset
                        if current_offset is not None
                        else (row["offset_at_placement"] or 0.0)
                    )
                else:
                    offset = row["offset_at_placement"] or 0.0

            tick = mt5_client.symbol_info_tick(mt5_sym)
            if tick is None:
                continue
            spread = tick.ask - tick.bid
            direction = sig["direction"]
            if direction == "long":
                mt5_sl = round(current_db_sl + offset - spread, sym.digits)
            else:
                mt5_sl = round(current_db_sl + offset + spread, sym.digits)

            pos = pos_by_ticket[ticket]
            res = mt5_client.modify_position_sl(ticket, pos.symbol, mt5_sl)
            if res and res.retcode == mt5.TRADE_RETCODE_NO_CHANGES:
                await sqlite.update_db_stop_loss(ticket, current_db_sl, mt5_sl)
                self._sl_fail_count.pop(ticket, None)
                self._sl_fail_target.pop(ticket, None)
                continue
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                await sqlite.update_db_stop_loss(ticket, current_db_sl, mt5_sl)
                self._sl_fail_count.pop(ticket, None)
                self._sl_fail_target.pop(ticket, None)
                logger.info(
                    "SL sync ticket=%d signal=%d: db_sl %.5f -> %.5f, mt5_sl=%.5f",
                    ticket,
                    signal_id,
                    stored_db_sl,
                    current_db_sl,
                    mt5_sl,
                )
            else:
                retcode = res.retcode if res else "None"
                logger.warning("SL sync failed ticket=%d retcode=%s", ticket, retcode)
                if self._sl_fail_target.get(ticket) != mt5_sl:
                    self._sl_fail_count[ticket] = 0
                    self._sl_fail_target[ticket] = mt5_sl
                count = self._sl_fail_count.get(ticket, 0) + 1
                self._sl_fail_count[ticket] = count
                if count == _SL_FAIL_MAX:
                    logger.error(
                        "Persistent SL sync failure: ticket=%d target_sl=%.5f retcode=%s",
                        ticket,
                        mt5_sl,
                        retcode,
                    )

    async def _manage_spread_hour_sls(
        self,
        sqlite: SQLiteDB,
        mt5_client: MT5Client,
        mt5_positions: list,
        scheduler: MarketScheduler,
        config: Settings,
        unmanaged_sids: set[int],
        filled_rows: list | None = None,
    ) -> None:
        """Strip the stop-loss from every hit position ~5 min before spread hour so a
        spread-driven spike can't stop it out, then restore it once the window ends.
        On restore, if price has genuinely moved past the stop, close at market (a
        bigger but rare loss). Crypto and 24h (-suffix) instruments are exempt — their
        markets stay liquid through these windows. The sl_stripped flag persists in
        SQLite so a restart mid-window never leaves a position unprotected and the TP
        loop / SL-sync both skip a stripped position."""
        filled = filled_rows if filled_rows is not None else await sqlite.get_filled_positions()
        if not filled:
            return

        pos_by_ticket = {p.ticket: p for p in mt5_positions}

        for row in filled:
            if row["mt5_ticket"] in self._closed_tickets:
                continue
            if row["signal_id"] in unmanaged_sids:
                continue
            ticket = row["mt5_ticket"]
            pos = pos_by_ticket.get(ticket)
            if pos is None:
                continue
            instr = db_symbol_from_mt5(row["symbol"] or pos.symbol, config)
            if _gate_exempt(instr, config):
                continue
            is_stock = detect_asset_class(instr) == AssetClass.STOCKS
            in_window = scheduler.is_sl_strip_window(stock=is_stock)
            stripped = bool(row["sl_stripped"])

            if in_window and not stripped and pos.sl != 0.0:
                await sqlite.update_sl(ticket, pos.sl)  # remember pre-strip SL for restore
                res = mt5_client.modify_position_sl(ticket, pos.symbol, 0.0)
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    await sqlite.set_sl_stripped(ticket, 1)
                    self._sl_strip_fail_count.pop(ticket, None)
                    logger.info(
                        "Spread-hour SL strip: ticket=%d signal=%d symbol=%s prev_sl=%.5f",
                        ticket,
                        row["signal_id"],
                        pos.symbol,
                        pos.sl,
                    )
                else:
                    self._log_sl_action_fail("strip", ticket, res, self._sl_strip_fail_count)
            elif not in_window and stripped:
                await self._restore_spread_hour_sl(row, pos, mt5_client, sqlite)

    async def _restore_spread_hour_sl(
        self, row, pos, mt5_client: MT5Client, sqlite: SQLiteDB
    ) -> None:
        ticket = row["mt5_ticket"]
        target = row["last_known_mt5_sl"]
        if target is None or target == 0.0:
            # No stored SL to restore — clear the flag so we stop retrying.
            await sqlite.set_sl_stripped(ticket, 0)
            return

        tick = mt5_client.symbol_info_tick(pos.symbol)
        if tick is None:
            return  # market likely closed (e.g. stock overnight) — retry next cycle

        breached = (pos.type == 0 and tick.bid <= target) or (pos.type != 0 and tick.ask >= target)
        if breached:
            res = mt5_client.close_position(
                ticket=pos.ticket,
                symbol=pos.symbol,
                volume=pos.volume,
                position_type=pos.type,
                comment="spread_sl_restore",
            )
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                realized_pnl = mt5_client.get_position_realized_pnl(ticket)
                if realized_pnl is None:
                    realized_pnl = pos.profit
                await sqlite.mark_closed(ticket, realized_pnl)
                self._closed_tickets.add(ticket)
                self._sl_restore_fail_count.pop(ticket, None)
                logger.warning(
                    "Spread-hour SL restore: price past stop — closed ticket=%d signal=%d "
                    "sl=%.5f pnl=%s",
                    ticket,
                    row["signal_id"],
                    target,
                    f"{realized_pnl:.2f}" if realized_pnl is not None else "?",
                )
            else:
                self._log_sl_action_fail("restore-close", ticket, res, self._sl_restore_fail_count)
            return

        res = mt5_client.modify_position_sl(ticket, pos.symbol, target)
        if res and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_NO_CHANGES):
            await sqlite.set_sl_stripped(ticket, 0)
            self._sl_restore_fail_count.pop(ticket, None)
            logger.info(
                "Spread-hour SL restore: ticket=%d signal=%d symbol=%s sl=%.5f",
                ticket,
                row["signal_id"],
                pos.symbol,
                target,
            )
        else:
            self._log_sl_action_fail("restore", ticket, res, self._sl_restore_fail_count)

    def _log_sl_action_fail(self, action: str, ticket: int, res, counter: dict[int, int]) -> None:
        """Log a strip/restore failure once, then again at the cap, then suppress —
        a stock whose market is shut overnight would otherwise spam every cycle."""
        retcode = res.retcode if res else "None"
        count = counter.get(ticket, 0) + 1
        counter[ticket] = count
        if count == 1:
            logger.warning(
                "Spread-hour SL %s failed: ticket=%d retcode=%s", action, ticket, retcode
            )
        elif count == _SL_FAIL_MAX:
            logger.error(
                "Spread-hour SL %s persistently failing: ticket=%d retcode=%s "
                "(suppressing further logs)",
                action,
                ticket,
                retcode,
            )

    async def _filled_statuses(
        self, supabase: SupabaseDB, filled_sids: set[int]
    ) -> dict[int, dict]:
        """Force-exit statuses for the filled set, behind the rev-gated egress cache:
        the sync-state poll drops the cache when the signals_rev watermark moves (a
        status flip is a signals write), so the max-age is only a safety net — or the
        legacy interval when the watermark is absent. A signal not yet in the cached
        set forces an immediate refetch — so a fresh fill or a post-restart set is
        never gated — and a fetch failure reuses the last snapshot so force-exits
        survive a brief pooler blip."""
        max_age = _STATUS_MAX_AGE if self._signals_rev is not None else _STATUS_MAX_AGE_LEGACY
        cache_now = time.monotonic()
        if (
            self._status_cache is None
            or bool(filled_sids - self._status_requested)
            or (cache_now - self._status_cache_at) >= max_age
        ):
            try:
                self._status_cache = await supabase.fetch_signal_statuses(list(filled_sids))
                self._status_cache_at = cache_now
                self._status_requested = set(filled_sids)
            except Exception:
                logger.error("Failed to fetch signal statuses", exc_info=True)
                if self._status_cache is None:
                    self._status_cache = {}
        return self._status_cache

    async def _close_position_tracked(
        self,
        pos,
        row: dict,
        sqlite: SQLiteDB,
        mt5_client: MT5Client,
        *,
        comment: str,
        label: str,
        fail_counts: dict[int, int] | None,
        close_reason: str | None = None,
    ) -> str:
        """Stop trailing and market-close one tracked position.

        Shared by every force-exit sweep (status, news, risky window, profit
        weekend, user skip). Trailing is stopped first so the TP loop can't
        ratchet the SL mid-close. On success the realized P&L is recorded
        (falling back to the live position profit) and the SQLite row is marked
        closed. Consecutive failures are counted per ticket in fail_counts and
        capped at _FORCE_EXIT_MAX_ATTEMPTS; pass fail_counts=None to retry
        forever (user-skip semantics). Returns "closed", "failed", or "capped".
        """
        ticket = row["mt5_ticket"]
        if ticket in self._closed_tickets:
            return "closed"
        if fail_counts is not None and fail_counts.get(ticket, 0) >= _FORCE_EXIT_MAX_ATTEMPTS:
            return "capped"

        await sqlite.set_trailing(ticket, 0)
        res = mt5_client.close_position(
            ticket=pos.ticket,
            symbol=pos.symbol,
            volume=pos.volume,
            position_type=pos.type,
            comment=comment,
        )
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            realized_pnl = mt5_client.get_position_realized_pnl(ticket)
            if realized_pnl is None:
                realized_pnl = pos.profit
            await sqlite.mark_closed(ticket, realized_pnl, close_reason)
            self._closed_tickets.add(ticket)
            if fail_counts is not None:
                fail_counts.pop(ticket, None)
            logger.info(
                "%s: closed ticket=%d signal=%d symbol=%s pnl=%.2f",
                label,
                ticket,
                row["signal_id"],
                pos.symbol,
                realized_pnl,
            )
            return "closed"

        retcode = res.retcode if res else "None"
        logger.error("%s close failed ticket=%d retcode=%s", label, ticket, retcode)
        if fail_counts is not None:
            new_count = fail_counts.get(ticket, 0) + 1
            fail_counts[ticket] = new_count
            if new_count == _FORCE_EXIT_MAX_ATTEMPTS:
                logger.error(
                    "%s abandoned: ticket=%d signal=%d after %d attempts — "
                    "manual intervention required",
                    label,
                    ticket,
                    row["signal_id"],
                    new_count,
                )
        return "failed"

    async def _check_forced_exits(
        self,
        supabase: SupabaseDB,
        sqlite: SQLiteDB,
        mt5_client: MT5Client,
        mt5_positions: list,
        scheduler: MarketScheduler,
        config: Settings,
        unmanaged_sids: set[int],
        filled_rows: list | None = None,
    ) -> None:
        if filled_rows is None:
            filled_rows = await sqlite.get_filled_positions()
        filled_sids = {r["signal_id"] for r in filled_rows} - unmanaged_sids
        if not filled_sids:
            self._last_signal_status.clear()
            self.server_tp_signals = set()
            return

        status_map = await self._filled_statuses(supabase, filled_sids)

        # Auto-TP'd signals for the TP engine's follow-server mode. Manual 'profit' is
        # excluded: the force-exit pass below closes those outright, whole position.
        self.server_tp_signals = {
            sid
            for sid in filled_sids
            if (entry := status_map.get(sid)) is not None
            and entry["status"] == "profit"
            and entry.get("closed_reason") != "manual"
        }

        pos_by_ticket = {p.ticket: p for p in mt5_positions}
        all_filled_rows = filled_rows

        for signal_id in filled_sids:
            entry = status_map.get(signal_id)
            if entry is None:
                continue
            current = entry["status"]
            closed_reason = entry.get("closed_reason")

            previous = self._last_signal_status.get(signal_id)

            # A manual 'profit' (TM reply / slash command, closed_reason "manual") closes
            # our positions like breakeven; an auto-TP 'profit' (closed_reason "automatic")
            # leaves them open for our own TP engine to manage.
            manual_profit = current == "profit" and closed_reason == "manual"

            if current == "profit" and not manual_profit and previous != "profit":
                logger.info(
                    "Signal %d auto-TP-marked by TM — keeping positions; TP engine continues",
                    signal_id,
                )

            if current not in _FORCE_EXIT_STATUSES and not manual_profit:
                self._last_signal_status[signal_id] = current
                self._last_force_exit_status.pop(signal_id, None)
                continue
            if previous == current:
                continue

            signal_rows = [r for r in all_filled_rows if r["signal_id"] == signal_id]

            # Cancelled status: only force-close if the cancellation is happening within
            # the forex weekend window (Fri >=15:55 EST through Sun <18:00 EST), because
            # weekday cancellations on a hit signal are expected (TM extends expiry to the
            # next day) and positions should stay open. Crypto stays tradable through the
            # weekend, but a 'cancelled' TM status is the only directive we have for it,
            # so crypto positions always close on cancellation regardless of day/time.
            # 'breakeven' and manual 'profit' statuses close unconditionally.
            # Cancellations force-close every position regardless of day or asset class
            # unless the reason is 'expiry' (a rolled-over hit signal). near_miss, manual,
            # news:*, spread_hour, late_market and risky_window all mean the signal was voided
            # or falsely triggered, so our fill must go. The remaining pending limits drop out of the
            # active fetch and are cancelled by the stale-pending sweep.
            gate_reason = current
            if current == "cancelled" and closed_reason in _ROLLOVER_CANCEL_REASONS:
                is_weekend = scheduler.is_weekend_window()
                if not is_weekend:
                    crypto_rows = [
                        r
                        for r in signal_rows
                        if detect_asset_class(db_symbol_from_mt5(r["symbol"] or "", config))
                        == AssetClass.CRYPTO
                    ]
                    if not crypto_rows:
                        self._last_signal_status[signal_id] = current
                        self._last_force_exit_status.pop(signal_id, None)
                        logger.info(
                            "Signal %d cancelled on weekday — keeping non-crypto positions open",
                            signal_id,
                        )
                        continue
                    signal_rows = crypto_rows
                    gate_reason = "cancelled_crypto"
                else:
                    gate_reason = "cancelled_weekend"
            elif current == "cancelled":
                gate_reason = f"cancelled_{closed_reason}"

            logger.warning(
                "Forced exit: signal %d status %r -> %r closed_reason=%r reason=%s — closing %d position(s)",
                signal_id,
                previous,
                current,
                closed_reason,
                gate_reason,
                len(signal_rows),
            )

            # Clear per-ticket fail counts on a new or resumed force-exit trigger
            if self._last_force_exit_status.get(signal_id) != current:
                self._last_force_exit_status[signal_id] = current
                for row in signal_rows:
                    self._force_exit_fail_count.pop(row["mt5_ticket"], None)

            all_handled = True
            for row in signal_rows:
                pos = pos_by_ticket.get(row["mt5_ticket"])
                if pos is None:
                    continue
                outcome = await self._close_position_tracked(
                    pos,
                    row,
                    sqlite,
                    mt5_client,
                    comment=f"force_{current}",
                    label="Forced exit",
                    fail_counts=self._force_exit_fail_count,
                    close_reason="breakeven" if current == "breakeven" else None,
                )
                if outcome == "failed":
                    all_handled = False

            # Tear down the signal's remaining pending limits in the same pass, keyed on
            # the status read that triggered the close — don't wait for the stale-pending
            # sweep, whose signal-sets cache can trail by up to 60s. During volatility one
            # of those entries could otherwise fill after the signal is already dead.
            for prow in await sqlite.get_pending_by_signal(signal_id):
                ok = await self._canceller.cancel_order(
                    prow["mt5_ticket"], mt5_client, sqlite, spread=False
                )
                if not ok:
                    all_handled = False

            if all_handled:
                self._last_signal_status[signal_id] = current
            # else: leave previous status so next cycle retries

        self._last_signal_status = {
            sid: st for sid, st in self._last_signal_status.items() if sid in filled_sids
        }

    async def _check_news_exits(
        self,
        news_symbols: frozenset[str],
        vol_symbols: frozenset[str],
        sqlite: SQLiteDB,
        mt5_client: MT5Client,
        mt5_positions: list,
        config: Settings,
        unmanaged_sids: set[int],
        filled_rows: list | None = None,
    ) -> None:
        """Force-close filled positions whose instrument is under active news or the
        volatility guard. Mirrors the manual-cancel / breakeven force-exit: stop
        trailing, close, mark closed. Gating matches the placement gate, 24/7
        exemption included. Idempotent — once a position is closed it's gone from
        MT5 and skipped next cycle."""
        if not news_symbols and not vol_symbols:
            return
        filled = filled_rows if filled_rows is not None else await sqlite.get_filled_positions()
        if not filled:
            return

        pos_by_ticket = {p.ticket: p for p in mt5_positions}

        for row in filled:
            if row["signal_id"] in unmanaged_sids:
                continue
            pos = pos_by_ticket.get(row["mt5_ticket"])
            if pos is None:
                continue
            instr = db_symbol_from_mt5(row["symbol"] or "", config)
            if not _gated_by_news_or_vol(instr, news_symbols, vol_symbols, config):
                continue
            vol = instrument_under_news(instr, vol_symbols)
            await self._close_position_tracked(
                pos,
                row,
                sqlite,
                mt5_client,
                comment="force_vol" if vol else "force_news",
                label="Volatility exit" if vol else "News exit",
                fail_counts=self._news_exit_fail_count,
            )

    async def _check_risky_window_exits(
        self,
        sqlite: SQLiteDB,
        mt5_client: MT5Client,
        mt5_positions: list,
        scheduler: MarketScheduler,
        unmanaged_sids: set[int],
        filled_rows: list | None = None,
    ) -> None:
        """Force-close every filled 'risky' position while a risky-disabled window is
        active — no risky trade may stay open through the window. Mirrors the news
        force-exit (stop trailing, close, mark closed) with capped retries. Idempotent:
        a closed position is gone from MT5 and skipped next cycle."""
        if not scheduler.is_risky_disabled():
            return
        filled = filled_rows if filled_rows is not None else await sqlite.get_filled_positions()
        if not filled:
            return

        pos_by_ticket = {p.ticket: p for p in mt5_positions}

        for row in filled:
            if row["signal_id"] in unmanaged_sids:
                continue
            if (row["signal_type"] or "") != "risky":
                continue
            pos = pos_by_ticket.get(row["mt5_ticket"])
            if pos is None:
                continue
            await self._close_position_tracked(
                pos,
                row,
                sqlite,
                mt5_client,
                comment="force_risky_window",
                label="Risky-window exit",
                fail_counts=self._risky_exit_fail_count,
            )

    async def _check_profit_weekend_exits(
        self,
        supabase: SupabaseDB,
        sqlite: SQLiteDB,
        mt5_client: MT5Client,
        mt5_positions: list,
        scheduler: MarketScheduler,
        config: Settings,
        unmanaged_sids: set[int],
        filled_rows: list | None = None,
    ) -> None:
        """Flatten profit-marked signals before the forex weekend. A 'profit' status keeps
        positions open for the TP engine during the week (see _check_forced_exits), but we
        don't carry them across the weekend gap. Once the weekend window opens we close every
        non-crypto filled position on a profit-marked signal at market — the same flatten an
        expired ('cancelled') hit trade gets. Crypto trades through the weekend and is exempt.
        Idempotent: closed positions are gone from MT5 and skipped next cycle; fail counts cap
        retries so a shut market (Fri after close) doesn't spam the log all weekend."""
        if not scheduler.is_weekend_window():
            self._profit_weekend_fail_count.clear()
            return
        if filled_rows is None:
            filled_rows = await sqlite.get_filled_positions()
        filled_sids = {r["signal_id"] for r in filled_rows}
        if not filled_sids:
            return

        status_map = await self._filled_statuses(supabase, filled_sids)
        pos_by_ticket = {p.ticket: p for p in mt5_positions}

        for row in filled_rows:
            if row["signal_id"] in unmanaged_sids:
                continue
            pos = pos_by_ticket.get(row["mt5_ticket"])
            if pos is None:
                continue
            entry = status_map.get(row["signal_id"])
            if entry is None or entry["status"] != "profit":
                continue
            instr = db_symbol_from_mt5(row["symbol"] or "", config)
            if detect_asset_class(instr) == AssetClass.CRYPTO:
                continue
            await self._close_position_tracked(
                pos,
                row,
                sqlite,
                mt5_client,
                comment="force_profit_weekend",
                label="Profit-weekend exit",
                fail_counts=self._profit_weekend_fail_count,
            )

    async def _apply_skips(
        self,
        skipped_sids: set[int],
        sqlite: SQLiteDB,
        mt5_client: MT5Client,
        mt5_positions: list,
        mt5_orders: list,
        result: SyncResult,
        filled_rows: list | None = None,
    ) -> None:
        """Pull every order/position on a user-skipped signal: cancel its pending
        orders and market-close its open positions. Idempotent — a cancelled/closed
        row drops out of the queries below, so re-running has no further effect. A
        failed close (e.g. market shut) leaves the position 'filled' and retries
        next cycle."""
        if not skipped_sids:
            return

        order_tickets = {o.ticket for o in mt5_orders}
        for row in await sqlite.get_pending_orders():
            if row["signal_id"] not in skipped_sids:
                continue
            if row["mt5_ticket"] not in order_tickets:
                continue
            ok = await self._canceller.cancel_order(
                row["mt5_ticket"], mt5_client, sqlite, spread=False
            )
            result.cancelled += ok
            result.errors += not ok

        if filled_rows is None:
            filled_rows = await sqlite.get_filled_positions()
        pos_by_ticket = {p.ticket: p for p in mt5_positions}
        for row in filled_rows:
            if row["signal_id"] not in skipped_sids:
                continue
            pos = pos_by_ticket.get(row["mt5_ticket"])
            if pos is None:
                continue
            # fail_counts=None: user skips retry forever until the close lands.
            outcome = await self._close_position_tracked(
                pos, row, sqlite, mt5_client, comment="skip", label="Skip", fail_counts=None
            )
            if outcome == "failed":
                result.errors += 1

    async def _cancel_pending_after_close(
        self,
        sqlite: SQLiteDB,
        mt5_client: MT5Client,
        unmanaged_sids: set[int],
        result: SyncResult,
        filled_rows: list | None = None,
    ) -> None:
        """When auto-TP is disabled the bot doesn't manage exits, so a signal whose
        filled positions are all closed (user-TP'd or stopped out) should have its
        remaining pending limits cancelled — otherwise it keeps entering after the
        user has closed the trade. Only signals that have actually filled are touched;
        an untouched signal with no fills still places its limits normally."""
        pending = await sqlite.get_pending_orders()
        if not pending:
            return
        if filled_rows is None:
            filled_rows = await sqlite.get_filled_positions()
        open_sids = {
            row["signal_id"] for row in filled_rows if row["mt5_ticket"] not in self._closed_tickets
        }
        filled_ever = await sqlite.get_signals_with_fills()
        for row in pending:
            sid = row["signal_id"]
            if sid in unmanaged_sids or sid in open_sids or sid not in filled_ever:
                continue
            ok = await self._canceller.cancel_order(
                row["mt5_ticket"], mt5_client, sqlite, spread=False
            )
            result.cancelled += ok
            result.errors += not ok
            if ok:
                logger.info(
                    "Disable-auto-TP: cancelled orphaned limit ticket=%d signal=%d after close",
                    row["mt5_ticket"],
                    sid,
                )

    async def _check_external_closes(
        self,
        sqlite: SQLiteDB,
        mt5_client: MT5Client,
        mt5_positions: list,
        filled_rows: list | None = None,
    ) -> None:
        """Detect positions no longer in MT5 and mark them closed."""
        filled = filled_rows if filled_rows is not None else await sqlite.get_filled_positions()
        if not filled:
            return

        pos_by_ticket = {p.ticket: p for p in mt5_positions}

        for row in filled:
            ticket = row["mt5_ticket"]
            if ticket in self._closed_tickets:
                continue
            if ticket not in pos_by_ticket:
                realized_pnl = mt5_client.get_position_realized_pnl(ticket)
                await sqlite.mark_closed(ticket, realized_pnl)
                if row["is_trailing"]:
                    logger.info(
                        "Trailing stop hit: ticket=%d signal=%d symbol=%s pnl=%s",
                        ticket,
                        row["signal_id"],
                        row["symbol"] or "?",
                        f"{realized_pnl:.2f}" if realized_pnl is not None else "?",
                    )
                else:
                    logger.info(
                        "External close: ticket=%d signal=%d pnl=%s",
                        ticket,
                        row["signal_id"],
                        f"{realized_pnl:.2f}" if realized_pnl is not None else "?",
                    )
