import logging
from datetime import UTC, datetime

import aiosqlite

from bot.db.queries import (
    CLEAR_HISTORY,
    CLEAR_SIGNAL_FINALIZED,
    CLEAR_SIGNAL_TP_FIRED,
    CLEAR_TRIGGER_RECORDED,
    CREATE_ORDER_MAPPINGS,
    CREATE_SIGNAL_ACTIONS,
    CREATE_SIGNAL_FINALIZED,
    CREATE_SIGNAL_TP_FIRED,
    CREATE_TRIGGER_RECORDED,
    DELETE_CLAIMED_ORDER,
    DELETE_SIGNAL_ACTION,
    GET_ALL_ACTIVE,
    GET_CLAIMED_BY_SIGNAL_LIMIT,
    GET_CLAIMED_ORDERS,
    GET_FILLED_LIMIT_IDS,
    GET_FILLED_POSITIONS,
    GET_FILLED_SIGNAL_PRICES,
    GET_ORDER_BY_TICKET,
    GET_ORDER_HISTORY,
    GET_PENDING_BY_SIGNAL,
    GET_PENDING_ORDERS,
    GET_SETTLED_UNFINALIZED_SIGNALS,
    GET_SIGNAL_ACTIONS,
    GET_SIGNAL_FILLED_LOTS,
    GET_SIGNAL_FINAL_AGGREGATE,
    GET_SIGNALS_WITH_FILLS,
    GET_TP_FIRED_SIGNALS,
    GET_TRAILING_POSITIONS,
    GET_USER_STATS,
    INSERT_CLAIMED_ORDER,
    INSERT_ORDER,
    MARK_CANCELLED,
    MARK_CLOSED,
    MARK_FILLED,
    MARK_SIGNAL_FINALIZED,
    MARK_SIGNAL_TP_FIRED,
    MARK_TRIGGER_RECORDED,
    PROMOTE_CLAIMED_TO_PENDING,
    SET_EXIT_SLIPPAGE,
    SET_SIGNAL_ACTION,
    SET_SL_STRIPPED,
    SET_TRAILING,
    SIGNAL_SUMMARY,
    UPDATE_DB_STOP_LOSS,
    UPDATE_EXCURSION,
    UPDATE_LAST_OFFSET_CHECK,
    UPDATE_SL,
    UPDATE_TICKET,
)

logger = logging.getLogger(__name__)


class SQLiteDB:
    def __init__(self, path: str = "orders.db") -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def _reconnect(self) -> None:
        """Re-establish the aiosqlite connection after an internal thread death."""
        logger.warning("Reconnecting to SQLite (internal thread died)")
        try:
            if self._db:
                await self._db.close()
        except Exception:
            pass
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")

    async def init_schema(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute(CREATE_ORDER_MAPPINGS)
        await self._db.execute(CREATE_SIGNAL_FINALIZED)
        await self._db.execute(CREATE_SIGNAL_TP_FIRED)
        await self._db.execute(CREATE_SIGNAL_ACTIONS)
        await self._db.execute(CREATE_TRIGGER_RECORDED)
        await self._db.commit()
        # Migrations for existing databases
        for col in (
            "symbol TEXT",
            "realized_pnl REAL",
            "channel_id INTEGER",
            "sequence_number INTEGER",
            "mfe_price REAL NOT NULL DEFAULT 0",
            "mae_price REAL NOT NULL DEFAULT 0",
            "sl_stripped INTEGER NOT NULL DEFAULT 0",
            "fill_price REAL",
            "exit_slippage_points REAL",
            "close_reason TEXT",
        ):
            name = col.split()[0]
            try:
                await self._db.execute(f"SELECT {name} FROM order_mappings LIMIT 0")
            except Exception:
                await self._db.execute(f"ALTER TABLE order_mappings ADD COLUMN {col}")
                await self._db.commit()
                logger.info("Migration: added %s column", name)
        # is_scalp (bool) -> signal_type (text). Backfill is_scalp=1 -> 'scalp'.
        async with self._db.execute("PRAGMA table_info(order_mappings)") as cur:
            cols = await cur.fetchall()
        names = {c["name"] for c in cols}
        if "signal_type" not in names:
            await self._db.execute(
                "ALTER TABLE order_mappings ADD COLUMN signal_type TEXT NOT NULL DEFAULT 'standard'"
            )
            if "is_scalp" in names:
                await self._db.execute(
                    "UPDATE order_mappings SET signal_type = 'scalp' WHERE is_scalp = 1"
                )
            await self._db.commit()
            logger.info("Migration: added signal_type column (backfilled from is_scalp)")
        if "is_scalp" in names:
            try:
                await self._db.execute("ALTER TABLE order_mappings DROP COLUMN is_scalp")
                await self._db.commit()
                logger.info("Migration: dropped is_scalp column")
            except Exception:
                logger.warning(
                    "Migration: could not DROP is_scalp (older SQLite). "
                    "Column left in place; code no longer reads it.",
                    exc_info=True,
                )
        logger.info("SQLite schema ready: %s", self._path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def insert_order(
        self,
        limit_id: int,
        signal_id: int,
        mt5_ticket: int,
        order_type: str,
        lot_size: float,
        placed_at: str,
        db_stop_loss: float,
        signal_type: str,
        feed_price: float | None = None,
        mt5_price: float | None = None,
        offset: float | None = None,
        symbol: str | None = None,
        channel_id: int | None = None,
        sequence_number: int | None = None,
    ) -> None:
        await self._db.execute(
            "DELETE FROM order_mappings WHERE limit_id = ? AND status NOT IN ('pending', 'filled')",
            (limit_id,),
        )
        await self._db.execute(
            INSERT_ORDER,
            (
                limit_id,
                signal_id,
                mt5_ticket,
                order_type,
                lot_size,
                placed_at,
                db_stop_loss,
                signal_type,
                feed_price,
                mt5_price,
                offset,
                symbol,
                channel_id,
                sequence_number,
            ),
        )
        await self._db.commit()

    async def mark_filled(
        self, mt5_ticket: int, filled_at: str, fill_price: float | None = None
    ) -> None:
        await self._db.execute(MARK_FILLED, (filled_at, fill_price, mt5_ticket))
        await self._db.commit()

    async def mark_filled_and_set_position_ticket(
        self,
        order_ticket: int,
        position_ticket: int,
        filled_at: str,
        fill_price: float | None = None,
    ) -> None:
        """Atomically mark filled and update the ticket in one transaction."""
        await self._db.execute(MARK_FILLED, (filled_at, fill_price, order_ticket))
        if position_ticket != order_ticket:
            await self._db.execute(UPDATE_TICKET, (position_ticket, order_ticket))
        await self._db.commit()

    async def mark_cancelled(
        self, mt5_ticket: int, cancelled_at: str, spread: bool = False
    ) -> None:
        status = "spread_cancelled" if spread else "cancelled"
        await self._db.execute(MARK_CANCELLED, (status, cancelled_at, mt5_ticket))
        await self._db.commit()

    async def mark_closed(
        self,
        mt5_ticket: int,
        realized_pnl: float | None = None,
        close_reason: str | None = None,
    ) -> None:
        await self._db.execute(MARK_CLOSED, (realized_pnl, close_reason, mt5_ticket))
        await self._db.commit()

    async def set_trailing(self, mt5_ticket: int, is_trailing: int = 1) -> None:
        await self._db.execute(SET_TRAILING, (is_trailing, mt5_ticket))
        await self._db.commit()

    async def set_sl_stripped(self, mt5_ticket: int, stripped: int) -> None:
        await self._db.execute(SET_SL_STRIPPED, (stripped, mt5_ticket))
        await self._db.commit()

    async def get_pending_orders(self) -> list[aiosqlite.Row]:
        async with self._db.execute(GET_PENDING_ORDERS) as cursor:
            return await cursor.fetchall()

    async def get_order_by_ticket(self, mt5_ticket: int) -> aiosqlite.Row | None:
        async with self._db.execute(GET_ORDER_BY_TICKET, (mt5_ticket,)) as cursor:
            return await cursor.fetchone()

    async def get_filled_positions(self) -> list[aiosqlite.Row]:
        async with self._db.execute(GET_FILLED_POSITIONS) as cursor:
            return await cursor.fetchall()

    async def get_trailing_positions(self) -> list[aiosqlite.Row]:
        async with self._db.execute(GET_TRAILING_POSITIONS) as cursor:
            return await cursor.fetchall()

    async def get_all_active(self) -> list[aiosqlite.Row]:
        async with self._db.execute(GET_ALL_ACTIVE) as cursor:
            return await cursor.fetchall()

    async def update_sl(self, mt5_ticket: int, sl: float) -> None:
        await self._db.execute(UPDATE_SL, (sl, mt5_ticket))
        await self._db.commit()

    async def update_excursion(self, mt5_ticket: int, mfe_price: float, mae_price: float) -> None:
        await self._db.execute(UPDATE_EXCURSION, (mfe_price, mae_price, mt5_ticket))
        await self._db.commit()

    async def set_exit_slippage(self, mt5_ticket: int, points: float) -> None:
        await self._db.execute(SET_EXIT_SLIPPAGE, (points, mt5_ticket))
        await self._db.commit()

    async def mark_trigger_recorded(
        self, signal_id: int, mt5_account: int, level_sequence: int
    ) -> bool:
        """Claim the trigger-outcome write for one fill depth. Returns True only if
        this call inserted the row — the caller that loses the claim skips the write."""
        cursor = await self._db.execute(
            MARK_TRIGGER_RECORDED,
            (signal_id, mt5_account, level_sequence, datetime.now(UTC).isoformat()),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def get_settled_unfinalized_signals(self) -> list[int]:
        async with self._db.execute(GET_SETTLED_UNFINALIZED_SIGNALS) as cursor:
            rows = await cursor.fetchall()
        return [row["signal_id"] for row in rows]

    async def get_signal_final_aggregate(self, signal_id: int) -> aiosqlite.Row | None:
        async with self._db.execute(GET_SIGNAL_FINAL_AGGREGATE, (signal_id,)) as cursor:
            return await cursor.fetchone()

    async def mark_signal_finalized(self, signal_id: int, finalized_at: str) -> bool:
        """Record a signal as finalized. Returns True only if this call inserted the row
        (i.e. it was not already finalized) — used to guard the single final-outcome write."""
        cursor = await self._db.execute(MARK_SIGNAL_FINALIZED, (signal_id, finalized_at))
        await self._db.commit()
        return cursor.rowcount > 0

    async def mark_signal_tp_fired(self, signal_id: int) -> None:
        """Record that our own TP engine has fired on this signal. Idempotent — once set,
        the placement loop never re-places any of the signal's limits."""
        await self._db.execute(MARK_SIGNAL_TP_FIRED, (signal_id, datetime.now(UTC).isoformat()))
        await self._db.commit()

    async def get_tp_fired_signals(self) -> set[int]:
        async with self._db.execute(GET_TP_FIRED_SIGNALS) as cursor:
            rows = await cursor.fetchall()
        return {row["signal_id"] for row in rows}

    async def set_signal_action(self, signal_id: int, action: str) -> None:
        """Record a user override ('skip' or 'manual') for a signal. Reversible —
        clear_signal_action hands the signal back to normal bot management."""
        await self._db.execute(
            SET_SIGNAL_ACTION, (signal_id, action, datetime.now(UTC).isoformat())
        )
        await self._db.commit()

    async def clear_signal_action(self, signal_id: int) -> None:
        await self._db.execute(DELETE_SIGNAL_ACTION, (signal_id,))
        await self._db.commit()

    async def get_signal_actions(self) -> dict[int, str]:
        async with self._db.execute(GET_SIGNAL_ACTIONS) as cursor:
            rows = await cursor.fetchall()
        return {row["signal_id"]: row["action"] for row in rows}

    async def update_ticket(self, old_ticket: int, new_ticket: int) -> bool:
        """Update the ticket on a filled row. Returns True if a row was actually updated."""
        cursor = await self._db.execute(UPDATE_TICKET, (new_ticket, old_ticket))
        await self._db.commit()
        return cursor.rowcount > 0

    async def insert_claimed_order(
        self,
        limit_id: int,
        signal_id: int,
        order_type: str,
        lot_size: float,
        placed_at: str,
        db_stop_loss: float,
        signal_type: str,
        feed_price: float | None = None,
        mt5_price: float | None = None,
        offset: float | None = None,
        symbol: str | None = None,
        channel_id: int | None = None,
        sequence_number: int | None = None,
    ) -> None:
        """Pre-write a claim row before order_send(). Uses -limit_id as placeholder ticket."""
        await self._db.execute(
            "DELETE FROM order_mappings WHERE limit_id = ? AND status NOT IN ('pending', 'filled')",
            (limit_id,),
        )
        await self._db.execute(
            INSERT_CLAIMED_ORDER,
            (
                limit_id,
                signal_id,
                -limit_id,
                order_type,
                lot_size,
                placed_at,
                db_stop_loss,
                signal_type,
                feed_price,
                mt5_price,
                offset,
                symbol,
                channel_id,
                sequence_number,
            ),
        )
        await self._db.commit()

    async def promote_claimed_to_pending(self, limit_id: int, mt5_ticket: int) -> None:
        """Promote a claimed row to pending after successful order_send()."""
        await self._db.execute(PROMOTE_CLAIMED_TO_PENDING, (mt5_ticket, limit_id))
        await self._db.commit()

    async def delete_claimed_order(self, limit_id: int) -> None:
        """Remove a claim row after a failed order_send()."""
        await self._db.execute(DELETE_CLAIMED_ORDER, (limit_id,))
        await self._db.commit()

    async def get_claimed_orders(self) -> list[aiosqlite.Row]:
        async with self._db.execute(GET_CLAIMED_ORDERS) as cursor:
            return await cursor.fetchall()

    async def get_claimed_by_signal_limit(
        self, signal_id: int, limit_id: int
    ) -> aiosqlite.Row | None:
        async with self._db.execute(GET_CLAIMED_BY_SIGNAL_LIMIT, (signal_id, limit_id)) as cursor:
            return await cursor.fetchone()

    async def get_pending_by_signal(self, signal_id: int) -> list[aiosqlite.Row]:
        async with self._db.execute(GET_PENDING_BY_SIGNAL, (signal_id,)) as cursor:
            return await cursor.fetchall()

    async def get_filled_limit_ids(self) -> set[int]:
        async with self._db.execute(GET_FILLED_LIMIT_IDS) as cursor:
            rows = await cursor.fetchall()
        return {row["limit_id"] for row in rows}

    async def get_filled_signal_prices(self) -> dict[int, list[float]]:
        """signal_id -> list of DB price levels we have already filled/closed. Lets the
        placement guard reject a re-issued level whose limit_id changed on a TM edit."""
        async with self._db.execute(GET_FILLED_SIGNAL_PRICES) as cursor:
            rows = await cursor.fetchall()
        out: dict[int, list[float]] = {}
        for row in rows:
            out.setdefault(row["signal_id"], []).append(row["feed_price_at_placement"])
        return out

    async def get_signals_with_fills(self) -> set[int]:
        async with self._db.execute(GET_SIGNALS_WITH_FILLS) as cursor:
            rows = await cursor.fetchall()
        return {row["signal_id"] for row in rows}

    async def get_signal_filled_lots(self) -> dict[int, float]:
        async with self._db.execute(GET_SIGNAL_FILLED_LOTS) as cursor:
            rows = await cursor.fetchall()
        return {row["signal_id"]: row["lot_size"] for row in rows}

    async def get_order_history(self, from_date: str, to_date: str) -> list[aiosqlite.Row]:
        async with self._db.execute(GET_ORDER_HISTORY, (from_date, to_date)) as cursor:
            return await cursor.fetchall()

    async def clear_history(self) -> int:
        """Reset the account to 'new': delete every closed/cancelled trade row and the
        finalize guard. Open and pending orders are kept. Returns the rows deleted."""
        cursor = await self._db.execute(CLEAR_HISTORY)
        await self._db.execute(CLEAR_SIGNAL_FINALIZED)
        await self._db.execute(CLEAR_SIGNAL_TP_FIRED)
        await self._db.execute(CLEAR_TRIGGER_RECORDED)
        await self._db.commit()
        return cursor.rowcount

    async def update_db_stop_loss(
        self, mt5_ticket: int, new_db_sl: float, new_mt5_sl: float
    ) -> None:
        await self._db.execute(UPDATE_DB_STOP_LOSS, (new_db_sl, new_mt5_sl, mt5_ticket))
        await self._db.commit()

    async def update_last_offset_check(self, mt5_ticket: int, checked_at: str) -> None:
        await self._db.execute(UPDATE_LAST_OFFSET_CHECK, (checked_at, mt5_ticket))
        await self._db.commit()

    async def get_user_stats(self) -> dict[str, float]:
        async with self._db.execute(GET_USER_STATS) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return {"total_trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}
        return {
            "total_trades": row["total_trades"] or 0,
            "wins": row["wins"] or 0,
            "losses": row["losses"] or 0,
            "total_pnl": float(row["total_pnl"] or 0.0),
        }

    async def get_signal_summary(self, signal_id: int) -> dict[str, int]:
        async with self._db.execute(SIGNAL_SUMMARY, (signal_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return {"total": 0, "filled": 0, "pending": 0, "cancelled": 0, "closed": 0}
        return {
            "total": row["total"] or 0,
            "filled": row["filled"] or 0,
            "pending": row["pending"] or 0,
            "cancelled": row["cancelled"] or 0,
            "closed": row["closed"] or 0,
        }
