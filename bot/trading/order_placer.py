import logging
import time
from datetime import UTC, datetime
from typing import Literal

import MetaTrader5 as mt5

from bot.config.constants import MAGIC_NUMBER
from bot.db.sqlite import SQLiteDB
from bot.db.supabase import SupabaseDB
from bot.mt5.client import MT5Client
from bot.mt5.types import OrderRequest

logger = logging.getLogger(__name__)

_ACTIVE_SIGNAL_STATUSES = frozenset({"active", "hit"})

# Throttle repeated "market closed" notices so a closed session doesn't spam the
# log every sync cycle. Per-limit, monotonic seconds.
_MARKET_CLOSED_LOG_INTERVAL = 300.0

PlacementOutcome = Literal["placed", "skipped", "error"]


class OrderPlacer:
    def __init__(self) -> None:
        self._market_closed_log_ts: dict[int, float] = {}

    async def place_order(
        self,
        signal_id: int,
        limit_id: int,
        direction: str,
        db_stop_loss: float,
        db_price: float,
        signal_type: str,
        mt5_symbol: str,
        lot: float,
        offset: float | None,
        mt5_client: MT5Client,
        sqlite: SQLiteDB,
        supabase: SupabaseDB,
        channel_id: int | None = None,
        sequence_number: int | None = None,
        ladder_size: int | None = None,
    ) -> PlacementOutcome:
        tick = mt5_client.symbol_info_tick(mt5_symbol)
        if tick is None:
            logger.error("No tick for %s (signal=%d limit=%d)", mt5_symbol, signal_id, limit_id)
            return "error"

        info = mt5_client.symbol_info(mt5_symbol)
        if info is None:
            logger.error("No symbol_info for %s", mt5_symbol)
            return "error"

        spread = tick.ask - tick.bid
        base_price = db_price + (offset or 0.0)

        # Limit-only placement: never place stop orders. If the adjusted price is
        # already at/past current market, the signal-provider treats price having
        # crossed the level as a "hit" and would retract the limit anyway — placing
        # a stop here would just get cancelled by the next stale-pending sync.
        if direction == "long":
            adj_price = round(base_price + spread, info.digits)
            adj_sl = round(db_stop_loss + (offset or 0.0) - spread, info.digits)
            if adj_price >= tick.ask:
                logger.info(
                    "Skipping signal=%d limit=%d %s: buy_limit %.5f at/above ask %.5f — price already past",
                    signal_id,
                    limit_id,
                    mt5_symbol,
                    adj_price,
                    tick.ask,
                )
                return "skipped"
            order_type = mt5.ORDER_TYPE_BUY_LIMIT
            order_type_str = "buy_limit"
        else:
            adj_price = round(base_price - spread, info.digits)
            adj_sl = round(db_stop_loss + (offset or 0.0) + spread, info.digits)
            if adj_price <= tick.bid:
                logger.info(
                    "Skipping signal=%d limit=%d %s: sell_limit %.5f at/below bid %.5f — price already past",
                    signal_id,
                    limit_id,
                    mt5_symbol,
                    adj_price,
                    tick.bid,
                )
                return "skipped"
            order_type = mt5.ORDER_TYPE_SELL_LIMIT
            order_type_str = "sell_limit"

        comment = f"s{signal_id}_l{limit_id}"[:32]
        placed_at = datetime.now(UTC).isoformat()

        # Pre-write claim so a crash between order_send and SQLite commit is recoverable
        await sqlite.insert_claimed_order(
            limit_id=limit_id,
            signal_id=signal_id,
            order_type=order_type_str,
            lot_size=lot,
            placed_at=placed_at,
            db_stop_loss=db_stop_loss,
            signal_type=signal_type,
            feed_price=db_price,
            mt5_price=adj_price,
            offset=offset,
            symbol=mt5_symbol,
            channel_id=channel_id,
            sequence_number=sequence_number,
            ladder_size=ladder_size,
        )

        # Pre-send status recheck — abort if signal was cancelled since this cycle began
        status = await supabase.fetch_signal_status(signal_id)
        if status not in _ACTIVE_SIGNAL_STATUSES:
            logger.warning(
                "Pre-send abort: signal=%d status=%r — placement cancelled", signal_id, status
            )
            await sqlite.delete_claimed_order(limit_id)
            return "error"

        request = OrderRequest(
            action=mt5.TRADE_ACTION_PENDING,
            symbol=mt5_symbol,
            volume=lot,
            type=order_type,
            price=adj_price,
            sl=adj_sl,
            magic=MAGIC_NUMBER,
            comment=comment,
            type_filling=mt5.ORDER_FILLING_RETURN,  # resting limit order
        )

        result = mt5_client.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            self._log_send_failure(result, signal_id, limit_id)
            await sqlite.delete_claimed_order(limit_id)
            return "error"

        await sqlite.promote_claimed_to_pending(limit_id, result.ticket)

        # Verify broker accepted the exact SL/price (silent broker adjustments surface here)
        placed = mt5_client.order_get_by_ticket(result.ticket)
        if placed is not None:
            if abs(placed.sl - adj_sl) > info.point:
                logger.warning(
                    "Placement SL mismatch: ticket=%d requested=%.5f broker=%.5f",
                    result.ticket,
                    adj_sl,
                    placed.sl,
                )
            if abs(placed.price_open - adj_price) > info.point:
                logger.warning(
                    "Placement price mismatch: ticket=%d requested=%.5f broker=%.5f",
                    result.ticket,
                    adj_price,
                    placed.price_open,
                )
        else:
            logger.warning("Placement readback: order ticket=%d not found", result.ticket)

        logger.info(
            "Placed %s ticket=%d signal=%d limit=%d price=%.5f lot=%.2f",
            order_type_str,
            result.ticket,
            signal_id,
            limit_id,
            adj_price,
            lot,
        )
        return "placed"

    async def place_market_order(
        self,
        signal_id: int,
        limit_id: int,
        direction: str,
        db_stop_loss: float,
        db_take_profit: float,
        db_price: float,
        signal_type: str,
        mt5_symbol: str,
        lot: float,
        offset: float | None,
        mt5_client: MT5Client,
        sqlite: SQLiteDB,
        supabase: SupabaseDB,
        channel_id: int | None = None,
        sequence_number: int | None = None,
    ) -> PlacementOutcome:
        """Enter an instant-entry signal at the market, carrying the sender's fixed take
        profit onto the broker as a hard TP.

        Both stops are exact frame conversions of the feed price: `+ offset` puts them in
        the broker's mid frame, which sits half a spread inside the bid (long) / ask
        (short) the broker actually triggers on — the same half-spread cushion the resting
        SL of a limit order gets from its full-spread shift.
        """
        tick = mt5_client.symbol_info_tick(mt5_symbol)
        if tick is None:
            logger.error("No tick for %s (signal=%d limit=%d)", mt5_symbol, signal_id, limit_id)
            return "error"

        info = mt5_client.symbol_info(mt5_symbol)
        if info is None:
            logger.error("No symbol_info for %s", mt5_symbol)
            return "error"

        shift = offset or 0.0
        adj_sl = round(db_stop_loss + shift, info.digits)
        adj_tp = round(db_take_profit + shift, info.digits)
        if direction == "long":
            entry, order_type, order_type_str = tick.ask, mt5.ORDER_TYPE_BUY, "buy_market"
        else:
            entry, order_type, order_type_str = tick.bid, mt5.ORDER_TYPE_SELL, "sell_market"

        # Price already outside the signal's own SL↔TP band: the position would open only
        # to close on the next tick. The TM applies the same test before it records the
        # entry, but price keeps moving in the seconds before we see the row.
        low, high = sorted((adj_sl, adj_tp))
        if not low < entry < high:
            logger.info(
                "Skipping signal=%d limit=%d %s: market %.5f outside SL %.5f / TP %.5f",
                signal_id,
                limit_id,
                mt5_symbol,
                entry,
                adj_sl,
                adj_tp,
            )
            return "skipped"

        placed_at = datetime.now(UTC).isoformat()
        await sqlite.insert_claimed_order(
            limit_id=limit_id,
            signal_id=signal_id,
            order_type=order_type_str,
            lot_size=lot,
            placed_at=placed_at,
            db_stop_loss=db_stop_loss,
            signal_type=signal_type,
            feed_price=db_price,
            mt5_price=entry,
            offset=offset,
            symbol=mt5_symbol,
            channel_id=channel_id,
            sequence_number=sequence_number,
            ladder_size=1,
        )

        status = await supabase.fetch_signal_status(signal_id)
        if status not in _ACTIVE_SIGNAL_STATUSES:
            logger.warning(
                "Pre-send abort: signal=%d status=%r — market entry cancelled", signal_id, status
            )
            await sqlite.delete_claimed_order(limit_id)
            return "error"

        request = OrderRequest(
            action=mt5.TRADE_ACTION_DEAL,
            symbol=mt5_symbol,
            volume=lot,
            type=order_type,
            price=entry,
            sl=adj_sl,
            tp=adj_tp,
            magic=MAGIC_NUMBER,
            comment=f"s{signal_id}_l{limit_id}"[:32],
            type_filling=mt5_client.resolve_filling(mt5_symbol),
        )

        result = mt5_client.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            self._log_send_failure(result, signal_id, limit_id)
            await sqlite.delete_claimed_order(limit_id)
            return "error"

        # Left 'pending' for the fill sweep later in this same cycle: the deal already
        # executed, but detect_fills is what reconciles our ticket to the position's.
        await sqlite.promote_claimed_to_pending(limit_id, result.ticket)
        logger.info(
            "Entered %s ticket=%d signal=%d limit=%d price=%.5f lot=%.2f sl=%.5f tp=%.5f",
            order_type_str,
            result.ticket,
            signal_id,
            limit_id,
            result.price,
            lot,
            adj_sl,
            adj_tp,
        )
        return "placed"

    def _log_send_failure(self, result, signal_id: int, limit_id: int) -> None:
        if result is not None and result.retcode == mt5.TRADE_RETCODE_MARKET_CLOSED:
            # Market closed — retries when the session reopens. Expected, not a hard
            # error; log at most once per interval per limit instead of every cycle.
            now = time.monotonic()
            if now - self._market_closed_log_ts.get(limit_id, 0.0) >= _MARKET_CLOSED_LOG_INTERVAL:
                self._market_closed_log_ts[limit_id] = now
                logger.warning(
                    "Placement deferred (market closed): signal=%d limit=%d", signal_id, limit_id
                )
            return
        logger.error(
            "Placement failed: signal=%d limit=%d retcode=%s",
            signal_id,
            limit_id,
            result.retcode if result else "None",
        )
