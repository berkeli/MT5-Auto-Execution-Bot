import asyncio
import logging

import asyncpg

from bot.db.queries import (
    FETCH_FEED_HEALTH,
    FETCH_LIVE_PRICES,
    FETCH_SIGNAL_SETS,
    FETCH_SIGNAL_SETS_LEGACY,
    FETCH_SIGNAL_STATUS,
    FETCH_SIGNAL_STATUSES,
    FETCH_SYNC_STATE,
    FETCH_SYNC_STATE_LEGACY,
    UPSERT_USER_SNAPSHOT,
)

logger = logging.getLogger(__name__)

# Supabase session-mode poolers have a tight per-tenant connection limit
# (typically 15). Keep our pool small so a quick bot restart doesn't trip
# EMAXCONNSESSION while the pooler is still recycling stale sessions.
_POOL_MIN_SIZE = 1
_POOL_MAX_SIZE = 3
_POOL_RETRY_ATTEMPTS = 4
_POOL_RETRY_DELAY = 8.0
# Recycle idle connections before the Supabase pooler reaps them (it kills idle
# server sessions in ~1-2 min). A connection asyncpg still believes is live but whose
# socket the pooler/NAT silently dropped is the classic source of a hung query.
# Kept above the 30s idle sync cadence so the once-per-cycle sync-state poll reuses
# the connection instead of paying a fresh TLS handshake (~5KB egress) every cycle.
_POOL_INACTIVE_LIFETIME = 55.0
# Hard bound on acquiring a connection (waiting for a free slot AND establishing a new
# one). command_timeout bounds query execution; together they guarantee no pool call can
# wedge forever, so a dropped socket surfaces as a caught error the sync loop retries
# instead of silently freezing every DB-backed loop until a manual restart.
_ACQUIRE_TIMEOUT = 15.0


class SupabaseDB:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        self._sync_state_legacy = False
        self._signal_sets_legacy = False

    @property
    def is_connected(self) -> bool:
        return self._pool is not None

    async def create_pool(self) -> None:
        last_error: Exception | None = None
        for attempt in range(1, _POOL_RETRY_ATTEMPTS + 1):
            try:
                self._pool = await asyncpg.create_pool(
                    self._dsn,
                    min_size=_POOL_MIN_SIZE,
                    max_size=_POOL_MAX_SIZE,
                    command_timeout=10,
                    max_inactive_connection_lifetime=_POOL_INACTIVE_LIFETIME,
                    ssl="require",
                    # statement_cache_size=0 disables client-side prepared statements
                    # so the same code works with Supabase's transaction-mode pooler
                    # (port 6543), which doesn't keep server-side state between calls.
                    # Cost in session mode is negligible (sub-ms per query).
                    statement_cache_size=0,
                )
                logger.info("Supabase pool created")
                return
            except asyncpg.exceptions.InternalServerError as e:
                # Pooler is over its session limit — old connections from a previous
                # bot run will be reaped within ~30s, so back off and try again.
                if "EMAXCONNSESSION" not in str(e):
                    raise
                last_error = e
                if attempt < _POOL_RETRY_ATTEMPTS:
                    logger.warning(
                        "Supabase pooler at capacity (attempt %d/%d) — retrying in %.0fs",
                        attempt,
                        _POOL_RETRY_ATTEMPTS,
                        _POOL_RETRY_DELAY,
                    )
                    await asyncio.sleep(_POOL_RETRY_DELAY)
        assert last_error is not None
        raise last_error

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            logger.info("Supabase pool closed")

    def _acquire(self):
        # Every query goes through here so the acquire timeout is impossible to forget.
        return self._pool.acquire(timeout=_ACQUIRE_TIMEOUT)

    async def fetch_signal_sets(
        self, held_signal_ids: list[int]
    ) -> tuple[list[asyncpg.Record], set[int], dict[int, int], dict[int, int]]:
        """The active-signal sets in one round-trip (egress guard): the
        active+pending rows to place, the TM-marked 'hit' limit ids to spare from
        stale-cancel, and {limit_id: signal_id} maps for held 'profit' and 'breakeven'
        signals. The final-status branch is scoped to held_signal_ids because every
        other historical row would be discarded downstream.

        Instant-entry limits ride in the placeable set despite being 'hit': the TM marks
        them hit the moment it records the market price it saw, so 'pending' is a state
        they never occupy. The sync cycle tells them apart by take_profit and enters at
        market instead of resting a limit.
        """
        async with self._acquire() as conn:
            rows = await self._fetch_sets(conn, held_signal_ids)
        active = [
            r
            for r in rows
            if r["signal_status"] in ("active", "hit")
            and (r["limit_status"] == "pending" or r["take_profit"] is not None)
        ]
        hit_limit_ids = {
            r["limit_id"]
            for r in rows
            if r["signal_status"] in ("active", "hit") and r["limit_status"] == "hit"
        }
        profit_limit_signal = {
            r["limit_id"]: r["signal_id"] for r in rows if r["signal_status"] == "profit"
        }
        breakeven_limit_signal = {
            r["limit_id"]: r["signal_id"] for r in rows if r["signal_status"] == "breakeven"
        }
        return active, hit_limit_ids, profit_limit_signal, breakeven_limit_signal

    async def _fetch_sets(self, conn, held_signal_ids: list[int]) -> list[asyncpg.Record]:
        if self._signal_sets_legacy:
            return await conn.fetch(FETCH_SIGNAL_SETS_LEGACY, held_signal_ids)
        try:
            return await conn.fetch(FETCH_SIGNAL_SETS, held_signal_ids)
        except asyncpg.exceptions.UndefinedColumnError:
            # DB not yet migrated (TM restart pending) — fall back for the process
            # lifetime rather than failing every sync cycle on the missing column.
            self._signal_sets_legacy = True
            logger.warning("signals.take_profit missing — instant-entry signals disabled")
            return await conn.fetch(FETCH_SIGNAL_SETS_LEGACY, held_signal_ids)

    async def fetch_live_prices(self, symbols: list[str]) -> dict[str, asyncpg.Record]:
        async with self._acquire() as conn:
            rows = await conn.fetch(FETCH_LIVE_PRICES, symbols)
        return {row["symbol"]: row for row in rows}

    async def fetch_signal_status(self, signal_id: int) -> str | None:
        async with self._acquire() as conn:
            return await conn.fetchval(FETCH_SIGNAL_STATUS, signal_id)

    async def fetch_signal_statuses(self, signal_ids: list[int]) -> dict[int, dict]:
        async with self._acquire() as conn:
            rows = await conn.fetch(FETCH_SIGNAL_STATUSES, signal_ids)
        return {
            row["id"]: {"status": row["status"], "closed_reason": row["closed_reason"]}
            for row in rows
        }

    async def fetch_sync_state(self) -> tuple[str | None, str | None, int | None]:
        """Return (news_mode, vol_guard, signals_rev) from the single bot_mode_status
        row. The gates are comma-separated token lists (or 'ALL'), NULL when off.
        signals_rev is the TM-side write watermark; None when the DB predates the
        column, which drops the sync cycle back to interval-driven fetching."""
        async with self._acquire() as conn:
            if self._sync_state_legacy:
                row = await conn.fetchrow(FETCH_SYNC_STATE_LEGACY)
            else:
                try:
                    row = await conn.fetchrow(FETCH_SYNC_STATE)
                    if row is None:
                        return None, None, None
                    return row["news_mode"], row["vol_guard"], row["signals_rev"]
                except asyncpg.exceptions.UndefinedColumnError:
                    # DB not yet migrated (TM restart pending) — don't retry the
                    # missing column every second for the process lifetime.
                    self._sync_state_legacy = True
                    logger.warning(
                        "bot_mode_status.signals_rev missing — interval-polling fallback"
                    )
                    row = await conn.fetchrow(FETCH_SYNC_STATE_LEGACY)
        if row is None:
            return None, None, None
        return row["news_mode"], row["vol_guard"], None

    async def fetch_feed_health(self) -> dict[str, str]:
        """Return {feed_name: status} from the feed_health table."""
        async with self._acquire() as conn:
            rows = await conn.fetch(FETCH_FEED_HEALTH)
        return {row["feed"]: row["status"] for row in rows}

    async def upsert_user_snapshot(
        self,
        license_key: str,
        mt5_account: int,
        balance: float,
        equity: float,
        currency: str,
        leverage: int,
        open_positions_count: int,
        total_realized_pnl: float,
        total_trades: int,
        wins: int,
        losses: int,
        win_rate: float,
        bot_version: str,
    ) -> None:
        if self._pool is None:
            return
        try:
            async with self._acquire() as conn:
                await conn.execute(
                    UPSERT_USER_SNAPSHOT,
                    license_key,
                    mt5_account,
                    balance,
                    equity,
                    currency,
                    leverage,
                    open_positions_count,
                    total_realized_pnl,
                    total_trades,
                    wins,
                    losses,
                    win_rate,
                    bot_version,
                )
        except Exception:
            logger.error("User snapshot upsert failed account=%d", mt5_account, exc_info=True)
