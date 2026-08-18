"""Late market (spread_hour.daily_start .. sl_strip_start) blocks *new* orders and nothing
else: prices are still tradable, so working ladders stay live and filled positions stay
managed — a server TP in that hour has to exit then, not sit until daily_end. Only the
spike proper (sl_strip_start .. daily_end) tears pendings down, strips SLs and stands the
TP engine down. Risky windows are the exception that proves it: those close positions
outright, a minute *before* the window, however active the trade.

Window arithmetic lives in test_time_utils; these pin the wiring that consumes it."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytz

from bot.config.settings import SpreadHourConfig
from bot.core.engine import Engine
from bot.core.sync_cycle import SyncCycle, _CycleContext
from bot.utils.time_utils import MarketScheduler
from tests.conftest import make_settings


class _StopLoop(Exception):
    pass


def _engine(*, spike: bool, has_active: bool = True) -> tuple[Engine, dict]:
    engine = Engine.__new__(Engine)
    engine._config = make_settings()
    engine._mt5 = SimpleNamespace(ensure_connected=lambda: True)
    engine._sqlite = SimpleNamespace(
        get_all_active=AsyncMock(return_value=[object()] if has_active else [])
    )
    # is_spread_hour stays True throughout: it is the placement block, and both windows
    # are open during the spike, so keying off it would never see the difference.
    engine._scheduler = SimpleNamespace(
        is_spread_hour=lambda: True, is_sl_strip_window=lambda: spike
    )
    engine._sync_cycle = SimpleNamespace(server_tp_signals={3790})
    engine._tp_finalizer = None
    kwargs: dict = {}
    engine._tp = SimpleNamespace(
        run_cycle=AsyncMock(side_effect=lambda *a, **kw: kwargs.update(kw))
    )
    return engine, kwargs


async def _one_pass(engine: Engine) -> None:
    """_tp_loop is a while True; break out on the sleep that ends the first iteration."""
    with patch("bot.core.engine.asyncio.sleep", side_effect=_StopLoop):
        with pytest.raises(_StopLoop):
            await engine._tp_loop()


@pytest.mark.asyncio
async def test_tp_engine_manages_every_symbol_through_late_market() -> None:
    engine, kwargs = _engine(spike=False)
    await _one_pass(engine)
    assert kwargs["crypto_only"] is False
    assert kwargs["server_tp_signals"] == {3790}


@pytest.mark.asyncio
async def test_tp_engine_goes_crypto_only_once_the_spike_starts() -> None:
    engine, kwargs = _engine(spike=True)
    await _one_pass(engine)
    assert kwargs["crypto_only"] is True


@pytest.mark.asyncio
async def test_late_market_keeps_full_loop_cadence() -> None:
    engine, _ = _engine(spike=False)
    assert await engine._tp_interval() == 2.0
    assert await engine._active_interval() == 1.0


@pytest.mark.asyncio
async def test_spread_spike_throttles_the_loops() -> None:
    engine, _ = _engine(spike=True)
    assert await engine._tp_interval() == 30.0
    assert await _engine(spike=True, has_active=False)[0]._tp_interval() == 60.0


# ---------------------------------------------------------------------------
# The gate splits in two: placement stops at daily_start, teardown at the spike
# ---------------------------------------------------------------------------

_EST = pytz.timezone("US/Eastern")


def _ctx(
    when: datetime, *, risky_disabled: bool = False, filled_sids: set[int] | None = None
) -> _CycleContext:
    """A context carrying a real scheduler, so the gates resolve real windows."""
    return _CycleContext(
        config=make_settings(),
        scheduler=MarketScheduler(SpreadHourConfig()),
        now=_EST.localize(when),
        cache_now=0.0,
        unmanaged_sids=set(),
        filled_sids=filled_sids or set(),
        supabase_rows=[],
        hit_limit_ids=set(),
        profit_held_limit_ids={},
        supabase_by_limit={},
        supabase_limit_ids=set(),
        sqlite_limit_ids=set(),
        sqlite_pending=[],
        news_symbols=frozenset(),
        vol_symbols=frozenset(),
        risky_disabled=risky_disabled,
        risky_sl_by_signal={},
        tp_fired_signals=set(),
    )


def test_late_market_blocks_placement_but_keeps_working_ladders() -> None:
    # Monday 16:00 EST — past daily_start (15:55), before sl_strip_start (16:55).
    ctx = _ctx(datetime(2026, 3, 9, 16, 0))
    assert ctx.is_blocked("XAUUSD") is True
    assert ctx.cancel_blocked("XAUUSD") is False


def test_spread_spike_pulls_the_pendings() -> None:
    ctx = _ctx(datetime(2026, 3, 9, 17, 0))
    assert ctx.is_blocked("XAUUSD") is True
    assert ctx.cancel_blocked("XAUUSD") is True


def test_normal_hours_gate_nothing() -> None:
    ctx = _ctx(datetime(2026, 3, 9, 11, 0))
    assert ctx.is_blocked("XAUUSD") is False
    assert ctx.cancel_blocked("XAUUSD") is False


def test_24h_markets_are_exempt_from_the_teardown() -> None:
    # Crypto and -24 stocks trade through the spike, so neither boundary touches them.
    # (Session-hours stocks keep their own 15:40 cutoff — see test_time_utils.)
    ctx = _ctx(datetime(2026, 3, 9, 17, 0))
    assert ctx.cancel_blocked("BTCUSDT") is False
    assert ctx.cancel_blocked("AMD.NAS") is False


def test_news_still_cancels_on_contact_in_late_market() -> None:
    # The narrower window applies only to the clock; news/vol/risky tear down whenever.
    ctx = _ctx(datetime(2026, 3, 9, 11, 0))
    ctx.news_symbols = frozenset({"USD"})
    assert ctx.cancel_blocked("XAUUSD") is True
    assert (
        _ctx(datetime(2026, 3, 9, 11, 0), risky_disabled=True).cancel_blocked("XAUUSD", "risky")
        is True
    )


# ---------------------------------------------------------------------------
# Friday: an untouched ladder doesn't get carried into the 48h gap
# ---------------------------------------------------------------------------


def _pending(signal_id: int, symbol: str = "XAUUSD") -> dict:
    return {"limit_id": signal_id, "signal_id": signal_id, "signal_type": "toll", "symbol": symbol}


def test_friday_late_market_pulls_a_ladder_with_no_fill() -> None:
    # Friday 16:00 EST — a weekday at this hour keeps its pendings; Friday does not.
    assert _ctx(datetime(2026, 3, 6, 16, 0)).row_cancel_blocked(_pending(1)) is True
    assert _ctx(datetime(2026, 3, 9, 16, 0)).row_cancel_blocked(_pending(1)) is False


def test_friday_late_market_keeps_a_working_signals_limits() -> None:
    # Signal 1 is filled and hasn't TP'd — its remaining limits may still average in.
    ctx = _ctx(datetime(2026, 3, 6, 16, 0), filled_sids={1})
    assert ctx.row_cancel_blocked(_pending(1)) is False
    assert ctx.row_cancel_blocked(_pending(2)) is True


def test_friday_teardown_still_spares_crypto() -> None:
    ctx = _ctx(datetime(2026, 3, 6, 16, 0))
    assert ctx.row_cancel_blocked(_pending(1, "BTCUSDT")) is False


def test_friday_working_signal_still_loses_its_limits_at_the_spike() -> None:
    # The reprieve is late market only — 16:55 pulls everything either way.
    ctx = _ctx(datetime(2026, 3, 6, 17, 0), filled_sids={1})
    assert ctx.row_cancel_blocked(_pending(1)) is True


# ---------------------------------------------------------------------------
# Risky windows close active trades outright (the lead itself is in test_time_utils)
# ---------------------------------------------------------------------------


def _filled(ticket: int, signal_type: str) -> dict:
    return {"mt5_ticket": ticket, "signal_id": ticket, "signal_type": signal_type}


@pytest.mark.asyncio
async def test_risky_window_closes_an_active_position() -> None:
    scheduler = MagicMock()
    scheduler.is_risky_disabled.return_value = True
    cycle = SyncCycle()
    positions = [SimpleNamespace(ticket=1, profit=250.0), SimpleNamespace(ticket=2, profit=10.0)]
    with patch.object(SyncCycle, "_close_position_tracked", AsyncMock(return_value="closed")) as c:
        await cycle._check_risky_window_exits(
            MagicMock(),
            MagicMock(),
            positions,
            scheduler,
            set(),
            filled_rows=[_filled(1, "risky"), _filled(2, "standard")],
        )
    # The deeply-profitable risky position goes; the standard one beside it stays.
    assert [call.args[0].ticket for call in c.call_args_list] == [1]
    assert c.call_args_list[0].kwargs["comment"] == "force_risky_window"


@pytest.mark.asyncio
async def test_risky_sweep_is_idle_outside_the_window() -> None:
    scheduler = MagicMock()
    scheduler.is_risky_disabled.return_value = False
    with patch.object(SyncCycle, "_close_position_tracked", AsyncMock()) as c:
        await SyncCycle()._check_risky_window_exits(
            MagicMock(),
            MagicMock(),
            [SimpleNamespace(ticket=1, profit=0.0)],
            scheduler,
            set(),
            filled_rows=[_filled(1, "risky")],
        )
    c.assert_not_called()
