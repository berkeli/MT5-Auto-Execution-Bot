import logging
from collections import defaultdict
from datetime import UTC, datetime

import MetaTrader5 as mt5

from bot.config.constants import BOT_VERSION, MARKET_ORDER_TYPES, AssetClass
from bot.config.settings import Settings
from bot.db.sqlite import SQLiteDB
from bot.db.tp_outcomes_writer import TPOutcomesWriter
from bot.mt5.client import MT5Client
from bot.mt5.types import PositionInfo
from bot.tp.asset_config import get_config
from bot.tp.default_strategy import DefaultTPStrategy
from bot.tp.outcome import TPOutcome
from bot.tp.strategy import TPResult, TPStrategy
from bot.trading.lot_calculator import price_distance_to_money
from bot.trading.symbol_mapper import db_symbol_from_mt5, detect_asset_class

logger = logging.getLogger(__name__)


def _seconds_since(iso_str: str | None) -> float | None:
    if not iso_str:
        return None
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(iso_str)).total_seconds()
    except ValueError:
        return None


def _resolve_risk_percent(config: Settings, mt5_symbol: str) -> float | None:
    rp = config.lot_sizing.risk_percent
    if isinstance(rp, (int, float)):
        return float(rp)
    if isinstance(rp, dict):
        if mt5_symbol in rp:
            return float(rp[mt5_symbol])
        if "default" in rp:
            return float(rp["default"])
    return None


class TPEngine:
    def __init__(
        self,
        strategy: TPStrategy | None = None,
        outcomes_writer: TPOutcomesWriter | None = None,
    ) -> None:
        self._strategy: TPStrategy = strategy or DefaultTPStrategy()
        self._outcomes_writer = outcomes_writer

    async def run_cycle(
        self,
        mt5_client: MT5Client,
        sqlite: SQLiteDB,
        config: Settings,
        crypto_only: bool = False,
        server_tp_signals: set[int] | None = None,
    ) -> None:
        mt5_pos_map = {p.ticket: p for p in mt5_client.positions_get()}
        sqlite_rows = await sqlite.get_filled_positions()

        # Skipped/manual signals are off-limits to the TP engine — skip and manual
        # both mean "stop managing this signal", so never trail or close their positions.
        unmanaged_sids = set(await sqlite.get_signal_actions())

        # Sample MFE/MAE for every open position regardless of crypto_only — keeps
        # excursion history complete through spread hours.
        excursions = await self._sample_excursions(sqlite_rows, mt5_pos_map, mt5_client, sqlite)

        by_signal: dict[int, list] = defaultdict(list)
        for row in sqlite_rows:
            if row["mt5_ticket"] not in mt5_pos_map:
                continue
            if row["signal_id"] in unmanaged_sids:
                continue
            # Instant entry: the sender's own take profit is already sitting on the
            # broker as a hard TP, so this trade runs to that price or its stop. Trailing
            # it would bank a fraction of a target the channel set deliberately.
            if row["order_type"] in MARKET_ORDER_TYPES:
                continue
            # SL stripped for spread-hour protection — leave it untouched (the sync
            # cycle owns stripping/restoring). Trailing would otherwise re-arm an SL.
            if row["sl_stripped"]:
                continue
            if crypto_only:
                pos = mt5_pos_map[row["mt5_ticket"]]
                db_sym = db_symbol_from_mt5(pos.symbol, config)
                if detect_asset_class(db_sym) != AssetClass.CRYPTO:
                    continue
            by_signal[row["signal_id"]].append(row)

        mt5_account_login: int | None = None
        if by_signal and self._outcomes_writer is not None:
            acct = mt5_client.account_info()
            if acct is not None:
                mt5_account_login = acct.login

        for signal_id, rows in by_signal.items():
            try:
                await self._process_group(
                    signal_id,
                    rows,
                    mt5_pos_map,
                    mt5_client,
                    sqlite,
                    config,
                    mt5_account_login,
                    excursions,
                    server_tp_signals or set(),
                )
            except Exception:
                logger.error("TPEngine: unhandled error signal=%d", signal_id, exc_info=True)

    async def _sample_excursions(
        self,
        sqlite_rows: list,
        mt5_pos_map: dict[int, PositionInfo],
        mt5_client: MT5Client,
        sqlite: SQLiteDB,
    ) -> dict[int, tuple[float, float]]:
        """Ratchet each open position's max favourable / adverse price excursion
        (distance from entry, always >= 0) and persist it. Returns ticket -> (mfe, mae)."""
        ticks: dict[str, object] = {}
        out: dict[int, tuple[float, float]] = {}
        for row in sqlite_rows:
            pos = mt5_pos_map.get(row["mt5_ticket"])
            if pos is None:
                continue
            if pos.symbol not in ticks:
                ticks[pos.symbol] = mt5_client.symbol_info_tick(pos.symbol)
            tick = ticks[pos.symbol]
            if tick is None:
                continue
            if pos.type == 0:  # long
                fav, adv = tick.bid - pos.price_open, pos.price_open - tick.bid
            else:  # short
                fav, adv = pos.price_open - tick.ask, tick.ask - pos.price_open
            prev_mfe = row["mfe_price"] or 0.0
            prev_mae = row["mae_price"] or 0.0
            mfe = max(prev_mfe, fav, 0.0)
            mae = max(prev_mae, adv, 0.0)
            out[pos.ticket] = (mfe, mae)
            if mfe != prev_mfe or mae != prev_mae:
                await sqlite.update_excursion(pos.ticket, mfe, mae)
        return out

    async def _process_group(
        self,
        signal_id: int,
        rows: list,
        mt5_pos_map: dict[int, PositionInfo],
        mt5_client: MT5Client,
        sqlite: SQLiteDB,
        config: Settings,
        mt5_account_login: int | None = None,
        excursions: dict[int, tuple[float, float]] | None = None,
        server_tp_signals: set[int] | None = None,
    ) -> None:
        trailing_rows = [r for r in rows if r["is_trailing"]]
        non_trailing_rows = [r for r in rows if not r["is_trailing"]]

        # Resolve asset class from the first position's MT5 symbol
        first_pos = mt5_pos_map[rows[0]["mt5_ticket"]]
        db_sym = db_symbol_from_mt5(first_pos.symbol, config)
        asset_class = detect_asset_class(db_sym)
        signal_type = rows[0]["signal_type"] or "standard"
        asset_cfg = get_config(asset_class, signal_type, config, instrument=db_sym)

        # 1-1: fixed TP, full close, trailing disabled — never enter the trailing path
        # even if a stray is_trailing=1 row exists.
        if trailing_rows and signal_type != "1-1":
            trailing_positions = [mt5_pos_map[r["mt5_ticket"]] for r in trailing_rows]
            result = await self._strategy.update_trailing(
                signal_id, trailing_positions, asset_cfg, mt5_client, sqlite
            )
            for err in result.errors:
                logger.error("TPEngine trail signal=%d: %s", signal_id, err)

        if non_trailing_rows:
            positions = [mt5_pos_map[r["mt5_ticket"]] for r in non_trailing_rows]
            if self._triggered(
                signal_id, positions, asset_cfg, mt5_client, config, server_tp_signals
            ):
                result = await self._strategy.execute(
                    signal_id, positions, asset_cfg, mt5_client, sqlite
                )
                for err in result.errors:
                    logger.error("TPEngine execute signal=%d: %s", signal_id, err)
                if result.closed_tickets or result.trailed_tickets:
                    await sqlite.mark_signal_tp_fired(signal_id)
                    await self._cancel_pending_for_signal(signal_id, mt5_client, sqlite)
                if self._outcomes_writer is not None and result.snapshot is not None:
                    await self._record_outcome(
                        signal_id,
                        rows,
                        non_trailing_rows,
                        first_pos,
                        db_sym,
                        asset_class,
                        signal_type,
                        asset_cfg,
                        result,
                        sqlite,
                        config,
                        mt5_account_login,
                        mt5_client,
                        excursions or {},
                    )

    def _triggered(
        self,
        signal_id: int,
        positions: list[PositionInfo],
        asset_cfg,
        mt5_client: MT5Client,
        config: Settings,
        server_tp_signals: set[int] | None,
    ) -> bool:
        """Follow-server mode swaps the local profit threshold for the signal service's
        own auto-TP call (signals.status 'profit' + closed_reason 'automatic', surfaced by
        the sync cycle) so a user exits when the channel says TP regardless of how their
        broker's fills and prices differ. Nothing past the trigger changes — partial close,
        trailing, and the pending-limit teardown run exactly as in threshold mode."""
        if config.tp_config.follow_server_tp:
            if signal_id not in (server_tp_signals or ()):
                return False
            logger.info("TP triggered by server: signal=%d positions=%d", signal_id, len(positions))
            return True
        return self._strategy.should_trigger(positions, asset_cfg, mt5_client)

    async def _record_outcome(
        self,
        signal_id: int,
        all_rows: list,
        non_trailing_rows: list,
        first_pos: PositionInfo,
        db_sym: str,
        asset_class: AssetClass,
        signal_type: str,
        asset_cfg,
        result: TPResult,
        sqlite: SQLiteDB,
        config: Settings,
        mt5_account_login: int | None,
        mt5_client: MT5Client,
        excursions: dict[int, tuple[float, float]],
    ) -> None:
        snapshot = result.snapshot
        if snapshot is None:
            return
        try:
            summary = await sqlite.get_signal_summary(signal_id)
            row0 = all_rows[0]
            stop_loss = row0["db_stop_loss"]
            direction = "long" if first_pos.type == 0 else "short"
            risk_per_limit = (
                abs(snapshot.avg_entry_price - float(stop_loss)) if stop_loss is not None else None
            )

            sym_info = mt5_client.symbol_info(first_pos.symbol)
            risk_money = (
                price_distance_to_money(sym_info, risk_per_limit, snapshot.total_volume)
                if sym_info is not None and risk_per_limit
                else None
            )
            r_multiple = snapshot.realized_pnl / risk_money if risk_money else None

            sig_excursions = [
                excursions[r["mt5_ticket"]] for r in all_rows if r["mt5_ticket"] in excursions
            ]
            mfe_price = max((e[0] for e in sig_excursions), default=None)
            mae_price = max((e[1] for e in sig_excursions), default=None)
            mfe_r = mfe_price / risk_per_limit if mfe_price is not None and risk_per_limit else None
            mae_r = mae_price / risk_per_limit if mae_price is not None and risk_per_limit else None

            newest_row = max(non_trailing_rows, key=lambda r: r["mt5_ticket"])

            # One trigger row per fill depth: a failed close re-qualifies every
            # cycle and would otherwise re-write this row every ~1s.
            level_seq = (
                newest_row["sequence_number"] if newest_row["sequence_number"] is not None else -1
            )
            if not await sqlite.mark_trigger_recorded(signal_id, mt5_account_login or 0, level_seq):
                return

            acct = mt5_client.account_info()

            outcome = TPOutcome(
                signal_id=signal_id,
                mt5_account=mt5_account_login or 0,
                channel_id=row0["channel_id"],
                signal_type=signal_type,
                asset_class=asset_class.value,
                symbol=first_pos.symbol,
                direction=direction,
                total_limits=summary["total"],
                limits_filled=summary["filled"] + summary["closed"],
                limits_pending=summary["pending"],
                limits_cancelled=summary["cancelled"],
                avg_entry_price=snapshot.avg_entry_price,
                tp_trigger_price=snapshot.tp_trigger_price,
                stop_loss=float(stop_loss) if stop_loss is not None else None,
                threshold_value=float(asset_cfg.profit_threshold),
                threshold_unit=asset_cfg.threshold_unit,
                move_at_trigger=snapshot.move_at_trigger,
                realized_pnl=snapshot.realized_pnl,
                others_pnl=snapshot.others_pnl,
                total_volume=snapshot.total_volume,
                partial_close_pct=int(snapshot.partial_close_pct),
                trailing_started=snapshot.trailing_started,
                risk_per_limit=risk_per_limit,
                r_multiple=r_multiple,
                risk_percent_cfg=_resolve_risk_percent(config, first_pos.symbol),
                bot_version=BOT_VERSION,
                tp_strategy="follow_server" if config.tp_config.follow_server_tp else "default",
                notes={"non_trailing_count": len(non_trailing_rows)},
                stage="trigger",
                mfe_price=mfe_price,
                mfe_r=mfe_r,
                mae_price=mae_price,
                mae_r=mae_r,
                level_sequence=newest_row["sequence_number"],
                total_levels=summary["total"],
                seconds_to_trigger=_seconds_since(newest_row["filled_at"]),
                symbol_normalized=db_sym,
                account_equity=acct.equity if acct else None,
                account_balance=acct.balance if acct else None,
            )
            await self._outcomes_writer.record(outcome)
        except Exception:
            logger.error("TP outcome assembly failed signal=%d", signal_id, exc_info=True)

    async def _cancel_pending_for_signal(
        self, signal_id: int, mt5_client: MT5Client, sqlite: SQLiteDB
    ) -> None:
        pending = await sqlite.get_pending_by_signal(signal_id)
        if not pending:
            return
        now_iso = datetime.now(UTC).isoformat()
        for row in pending:
            ticket = row["mt5_ticket"]
            res = mt5_client.cancel_pending_order(ticket)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                await sqlite.mark_cancelled(ticket, now_iso, spread=False)
                logger.info("TP fired signal=%d — cancelled pending ticket=%d", signal_id, ticket)
            else:
                retcode = res.retcode if res else "None"
                logger.warning("TP cancel pending ticket=%d failed retcode=%s", ticket, retcode)
