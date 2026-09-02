from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import MetaTrader5 as mt5
import pytest

from bot.config.settings import ExcludedChannelAssetConfig, SymbolSuffixRule
from bot.core.sync_cycle import SyncCycle, _feed_for_symbol
from bot.trading.lot_calculator import LotCalculator
from tests.conftest import (
    make_account_info,
    make_order_info,
    make_order_result,
    make_position,
    make_symbol_info,
    make_tick,
)


def _make_supabase_row(
    limit_id=1,
    signal_id=1,
    instrument="EURUSD",
    signal_status="active",
    total_limits=1,
    take_profit=None,
    hit_time=None,
) -> dict:
    return {
        "limit_id": limit_id,
        "signal_id": signal_id,
        "instrument": instrument,
        "direction": "long",
        "stop_loss": 1.08500,
        "price_level": 1.09100,
        "signal_type": "standard",
        "signal_status": signal_status,
        "channel_id": None,
        "sequence_number": 1,
        "total_limits": total_limits,
        "take_profit": take_profit,
        "hit_time": hit_time,
    }


def _mock_supabase(
    signals=None,
    live_prices=None,
    news_mode=None,
    vol_guard=None,
    hit_limit_ids=None,
    profit_limit_ids=None,
    breakeven_limit_ids=None,
):
    sb = AsyncMock()
    sb.fetch_signal_sets.return_value = (
        signals or [],
        set(hit_limit_ids or []),
        dict(profit_limit_ids or {}),
        dict(breakeven_limit_ids or {}),
    )
    sb.fetch_live_prices.return_value = live_prices or {}
    sb.fetch_signal_statuses.return_value = {}
    sb.fetch_sync_state.return_value = (news_mode, vol_guard, 1)
    sb.fetch_feed_health.return_value = {}
    return sb


def _mock_scheduler(cancel_pending=False):
    sched = MagicMock()
    sched.should_cancel_pending.return_value = cancel_pending
    sched.should_block_placement.return_value = False
    sched.is_risky_disabled.return_value = False
    # should_cancel_pending IS is_sl_strip_window on the real scheduler, so it tracks
    # the same flag here. Pinning it matters: left as a bare MagicMock it is truthy,
    # which silently puts every test inside the spread spike — SLs stripped, breakeven
    # force-exits deferred.
    sched.is_sl_strip_window.return_value = cancel_pending
    return sched


def _risky_row(signal_id, direction, price_level):
    return {
        "signal_id": signal_id,
        "signal_type": "risky",
        "direction": direction,
        "price_level": price_level,
    }


def test_risky_sl_map_none_when_no_custom_sl() -> None:
    from tests.conftest import make_settings

    cfg = make_settings()  # risky.stop_loss defaults to None
    rows = [_risky_row(1, "long", 3300.0)]
    assert SyncCycle()._risky_sl_map(rows, cfg) == {}


def test_risky_sl_map_deepest_limit() -> None:
    from tests.conftest import make_settings

    cfg = make_settings()
    cfg.tp_config.risky.stop_loss = 5.0
    rows = [
        _risky_row(1, "long", 3320.0),
        _risky_row(1, "long", 3300.0),  # deepest (lowest) for a long
        _risky_row(2, "short", 3380.0),
        _risky_row(2, "short", 3400.0),  # deepest (highest) for a short
        {"signal_id": 3, "signal_type": "standard", "direction": "long", "price_level": 3300.0},
    ]
    out = SyncCycle()._risky_sl_map(rows, cfg)
    assert out == {1: 3295.0, 2: 3405.0}  # 3300-5 (long), 3400+5 (short); standard ignored


# ---------------------------------------------------------------------------
# Idempotency: already-tracked limits are not re-placed
# ---------------------------------------------------------------------------


async def test_idempotency_known_limit_not_replaced(sqlite_db, mock_mt5, sample_config) -> None:
    # Pre-populate SQLite with limit_id=1
    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=1,
        mt5_ticket=1001,
        order_type="buy_limit",
        lot_size=0.1,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
    )

    supabase = _mock_supabase(signals=[_make_supabase_row(limit_id=1)])
    scheduler = _mock_scheduler()

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.placed == 0
    mock_mt5.order_send.assert_not_called()


async def test_idempotency_second_run_is_noop(sqlite_db, mock_mt5, sample_config) -> None:
    # Two consecutive runs with the same single known limit → placed=0 both times
    await sqlite_db.insert_order(
        limit_id=2,
        signal_id=1,
        mt5_ticket=1002,
        order_type="buy_limit",
        lot_size=0.1,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
    )
    supabase = _mock_supabase(signals=[_make_supabase_row(limit_id=2)])
    scheduler = _mock_scheduler()
    cycle = SyncCycle()

    r1 = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)
    r2 = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert r1.placed == 0
    assert r2.placed == 0


# ---------------------------------------------------------------------------
# Re-placement guard: a limit that already filled on our end is never re-placed
# ---------------------------------------------------------------------------


async def test_filled_then_closed_limit_not_replaced(sqlite_db, mock_mt5, sample_config) -> None:
    # Limit filled on our broker, TP'd, and closed → SQLite row is 'closed'. The TM
    # never marked the limit hit, so Supabase still lists it pending. It must NOT be
    # placed a second time (the dangerous loop).
    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=1,
        mt5_ticket=9001,
        order_type="buy_limit",
        lot_size=0.1,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
    )
    await sqlite_db.mark_filled(9001, "2026-01-01T00:01:00+00:00")
    await sqlite_db.mark_closed(9001, 12.50)

    supabase = _mock_supabase(signals=[_make_supabase_row(limit_id=1)])
    scheduler = _mock_scheduler()

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.placed == 0
    mock_mt5.order_send.assert_not_called()
    assert 1 in cycle._logged_already_filled


async def test_edited_limit_same_price_new_id_not_replaced(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # A TM message edit rebuilds the signal's limit rows with fresh IDENTITY ids. A level
    # we already filled+closed (limit_id=1, price 1.09100) reappears under a new limit_id=2
    # at the same price. The limit_id guard misses it, but the (signal_id, price) guard must
    # still block re-entry.
    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=1,
        mt5_ticket=9201,
        order_type="buy_limit",
        lot_size=0.1,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
        feed_price=1.09100,
    )
    await sqlite_db.mark_filled(9201, "2026-01-01T00:01:00+00:00")
    await sqlite_db.mark_closed(9201, 12.50)

    # New limit_id, same signal + same price_level (1.09100 per _make_supabase_row).
    supabase = _mock_supabase(signals=[_make_supabase_row(limit_id=2)])
    scheduler = _mock_scheduler()

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.placed == 0
    mock_mt5.order_send.assert_not_called()
    assert 2 in cycle._logged_already_filled


async def test_cancelled_limit_still_replaceable(sqlite_db, mock_mt5, sample_config) -> None:
    # A never-filled limit that was cancelled (e.g. spread hour / offset drift) must
    # still re-place — the guard only blocks limits that actually filled.
    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=1,
        mt5_ticket=9101,
        order_type="buy_limit",
        lot_size=0.1,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
    )
    await sqlite_db.mark_cancelled(9101, "2026-01-01T00:01:00+00:00", spread=True)

    mock_mt5.account_info.return_value = make_account_info()
    mock_mt5.order_send.return_value = make_order_result(ticket=9102)
    mock_mt5.order_get_by_ticket.return_value = None
    row = _make_supabase_row(limit_id=1)
    row["price_level"] = 1.09950  # within proximity of mid and below ask → valid buy_limit
    supabase = _mock_supabase(signals=[row])
    supabase.fetch_signal_status.return_value = "active"
    scheduler = _mock_scheduler()

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.placed == 1
    mock_mt5.order_send.assert_called_once()
    assert 1 not in cycle._logged_already_filled


async def test_replaced_limit_reuses_filled_sibling_lot(sqlite_db, mock_mt5, sample_config) -> None:
    # A signal with a filled sibling re-places its remaining limit using the sibling's
    # stored lot, not a fresh calc: the Supabase fetch drops hit limits (l.status='hit'),
    # so recomputing would split the size across fewer survivors and oversize the order.
    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=1,
        mt5_ticket=9101,
        order_type="buy_limit",
        lot_size=0.33,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
    )
    await sqlite_db.mark_filled(9101, "2026-01-01T00:01:00+00:00")

    mock_mt5.account_info.return_value = make_account_info()
    mock_mt5.positions_get.return_value = [make_position(ticket=9101, volume=0.33)]
    mock_mt5.order_send.return_value = make_order_result(ticket=9102)
    mock_mt5.order_get_by_ticket.return_value = None
    row = _make_supabase_row(limit_id=2)  # new pending sibling of the same signal
    row["price_level"] = 1.09950
    supabase = _mock_supabase(signals=[row])
    supabase.fetch_signal_status.return_value = "active"
    scheduler = _mock_scheduler()

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.placed == 1
    assert mock_mt5.order_send.call_args.args[0].volume == 0.33


# ---------------------------------------------------------------------------
# Instant entry: signals whose single limit is born 'hit' at the market price
# ---------------------------------------------------------------------------


def _make_instant_row(limit_id=1, signal_id=1, age_seconds=0.0, **overrides) -> dict:
    row = _make_supabase_row(limit_id=limit_id, signal_id=signal_id, signal_status="hit")
    row.update(
        price_level=1.10000,  # the entry the TM recorded; mock tick mid is 1.10001
        stop_loss=1.09500,
        take_profit=1.10500,
        signal_type="pa",
        hit_time=datetime.now(UTC) - timedelta(seconds=age_seconds),
    )
    row.update(overrides)
    return row


def _instant_supabase(rows) -> AsyncMock:
    sb = _mock_supabase(signals=rows, hit_limit_ids=[r["limit_id"] for r in rows])
    sb.fetch_signal_status.return_value = "hit"
    return sb


async def test_instant_signal_enters_at_market_with_fixed_tp(
    sqlite_db, mock_mt5, sample_config
) -> None:
    mock_mt5.account_info.return_value = make_account_info()
    mock_mt5.order_send.return_value = make_order_result(ticket=7001)
    mock_mt5.resolve_filling.return_value = mt5.ORDER_FILLING_IOC

    cycle = SyncCycle()
    result = await cycle.run(
        _instant_supabase([_make_instant_row()]),
        sqlite_db,
        mock_mt5,
        sample_config,
        _mock_scheduler(),
    )

    assert result.placed == 1
    request = mock_mt5.order_send.call_args.args[0]
    assert request.action == mt5.TRADE_ACTION_DEAL
    assert request.type == mt5.ORDER_TYPE_BUY
    assert request.price == 1.10002  # ask — a long pays the offer
    assert request.sl == 1.09500
    assert request.tp == 1.10500  # the sender's price rides on the broker

    row = await sqlite_db.get_order_by_ticket(7001)
    assert row["order_type"] == "buy_market"


async def test_instant_signal_skipped_when_stale(sqlite_db, mock_mt5, sample_config) -> None:
    # A restart hours later must not open a position on a signal that is still live in
    # the DB — the market has long since left the price the TM recorded.
    mock_mt5.account_info.return_value = make_account_info()

    cycle = SyncCycle()
    result = await cycle.run(
        _instant_supabase([_make_instant_row(age_seconds=3600)]),
        sqlite_db,
        mock_mt5,
        sample_config,
        _mock_scheduler(),
    )

    assert result.placed == 0
    mock_mt5.order_send.assert_not_called()
    assert 1 in cycle._logged_stale_instant


async def test_instant_signal_waits_for_hit_time(sqlite_db, mock_mt5, sample_config) -> None:
    # The TM writes the limit row a beat before it marks it hit. Entering off a NULL
    # hit_time would mean entering blind to how old the recorded price is.
    mock_mt5.account_info.return_value = make_account_info()

    cycle = SyncCycle()
    result = await cycle.run(
        _instant_supabase([_make_instant_row(hit_time=None)]),
        sqlite_db,
        mock_mt5,
        sample_config,
        _mock_scheduler(),
    )

    assert result.placed == 0
    mock_mt5.order_send.assert_not_called()
    assert cycle._logged_stale_instant == set()  # not stale, just not ready yet


@pytest.mark.parametrize("instant, expected_placed", [(True, 0), (False, 1)])
async def test_instant_signal_uses_half_the_proximity_band(
    sqlite_db, mock_mt5, sample_config, instant, expected_placed
) -> None:
    # 10 pips from the market: inside the 15-pip forex proximity a resting ladder is
    # armed at, but outside the halved band an instant entry gets — at that distance
    # we would be taking a materially different trade than the one posted.
    mock_mt5.account_info.return_value = make_account_info()
    mock_mt5.order_send.return_value = make_order_result(ticket=7001)
    mock_mt5.order_get_by_ticket.return_value = None
    row = _make_instant_row(price_level=1.09901)
    if not instant:
        row["take_profit"] = None

    cycle = SyncCycle()
    result = await cycle.run(
        _instant_supabase([row]), sqlite_db, mock_mt5, sample_config, _mock_scheduler()
    )

    assert result.placed == expected_placed


async def test_instant_signal_skipped_when_market_outside_band(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # Price has already run past the take profit: the position would open only to close
    # on the next tick. Proximity still passes, so only the band check catches this.
    mock_mt5.account_info.return_value = make_account_info()

    cycle = SyncCycle()
    result = await cycle.run(
        _instant_supabase([_make_instant_row(take_profit=1.10001)]),
        sqlite_db,
        mock_mt5,
        sample_config,
        _mock_scheduler(),
    )

    assert result.placed == 0
    assert result.skipped == 1
    mock_mt5.order_send.assert_not_called()


async def test_instant_signal_not_re_entered_after_close(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # The TM leaves the signal live until its own TP/SL fires, so the row keeps coming
    # back. Once our position is closed it must never be re-entered.
    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=1,
        mt5_ticket=7001,
        order_type="buy_market",
        lot_size=0.1,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.09500,
        signal_type="pa",
    )
    await sqlite_db.mark_filled(7001, "2026-01-01T00:00:01+00:00")
    await sqlite_db.mark_closed(7001, 25.0)
    mock_mt5.account_info.return_value = make_account_info()

    cycle = SyncCycle()
    result = await cycle.run(
        _instant_supabase([_make_instant_row()]),
        sqlite_db,
        mock_mt5,
        sample_config,
        _mock_scheduler(),
    )

    assert result.placed == 0
    mock_mt5.order_send.assert_not_called()


async def test_instant_signal_blocked_by_disabled_channel(
    sqlite_db, mock_mt5, sample_config
) -> None:
    sample_config.disabled_channels = ["1536971699201773608"]
    mock_mt5.account_info.return_value = make_account_info()

    cycle = SyncCycle()
    result = await cycle.run(
        _instant_supabase([_make_instant_row(channel_id=1536971699201773608)]),
        sqlite_db,
        mock_mt5,
        sample_config,
        _mock_scheduler(),
    )

    assert result.placed == 0
    mock_mt5.order_send.assert_not_called()


async def test_instant_signal_kept_out_of_watch_list(sqlite_db, mock_mt5, sample_config) -> None:
    # An instant entry is never waiting on a level, so it must not sit in the
    # dashboard's "Closest Signals" view alongside genuine resting ladders.
    mock_mt5.account_info.return_value = make_account_info()
    mock_mt5.order_send.return_value = make_order_result(ticket=7001)

    cycle = SyncCycle()
    await cycle.run(
        _instant_supabase([_make_instant_row(), _make_supabase_row(limit_id=2, signal_id=2)]),
        sqlite_db,
        mock_mt5,
        sample_config,
        _mock_scheduler(),
    )

    assert [r["limit_id"] for r in cycle.last_supabase_rows] == [2]


async def test_instant_signal_is_sized_for_two_entries(sqlite_db, mock_mt5, sample_config) -> None:
    # The sender may average a second entry in at any time, so the budget is split
    # across both up front. Sizing against the one visible entry would double the
    # signal's risk the moment the second arrived.
    mock_mt5.account_info.return_value = make_account_info()
    mock_mt5.order_send.return_value = make_order_result(ticket=7001)
    mock_mt5.resolve_filling.return_value = mt5.ORDER_FILLING_IOC

    cycle = SyncCycle()
    await cycle.run(
        _instant_supabase([_make_instant_row()]),
        sqlite_db,
        mock_mt5,
        sample_config,
        _mock_scheduler(),
    )

    calc = LotCalculator(mock_mt5, sample_config)
    placed = mock_mt5.order_send.call_args.args[0].volume
    assert placed == calc.calculate(1.09500, [1.10000], "EURUSD", "pa", ladder_size=2)
    assert placed < calc.calculate(1.09500, [1.10000], "EURUSD", "pa", ladder_size=1)


async def test_added_instant_entry_placed_at_an_already_filled_price(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # A second entry averaged in via the TM's `add` reply arrives as a new limit on
    # the same signal, most likely at the price the first one filled at. The
    # already-filled-price guard exists for TM edits re-minting limit ids, which
    # instant signals never do — so it must not swallow a genuine second fill.
    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=1,
        mt5_ticket=7001,
        order_type="buy_market",
        lot_size=0.1,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.09500,
        signal_type="pa",
        feed_price=1.10000,
    )
    await sqlite_db.mark_filled(7001, "2026-01-01T00:00:01+00:00")
    mock_mt5.account_info.return_value = make_account_info()
    mock_mt5.order_send.return_value = make_order_result(ticket=7002)
    mock_mt5.resolve_filling.return_value = mt5.ORDER_FILLING_IOC

    added = _make_instant_row(limit_id=2, sequence_number=2, total_limits=2)

    cycle = SyncCycle()
    result = await cycle.run(
        _instant_supabase([_make_instant_row(), added]),
        sqlite_db,
        mock_mt5,
        sample_config,
        _mock_scheduler(),
    )

    assert result.placed == 1
    request = mock_mt5.order_send.call_args.args[0]
    assert request.action == mt5.TRADE_ACTION_DEAL
    # Reuses the filled sibling's lot, so the pair lands on the budget the first
    # entry was sized against.
    assert request.volume == 0.1


async def test_tp_fired_signal_limit_not_replaced(sqlite_db, mock_mt5, sample_config) -> None:
    # Our TP engine fired on signal 1 (durably marked). A new limit on that signal still
    # shows active in Supabase (TM/DB lag), but must NOT be re-placed.
    await sqlite_db.mark_signal_tp_fired(1)

    mock_mt5.account_info.return_value = make_account_info()
    supabase = _mock_supabase(signals=[_make_supabase_row(limit_id=7, signal_id=1)])
    scheduler = _mock_scheduler()

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.placed == 0
    mock_mt5.order_send.assert_not_called()
    assert 7 in cycle._logged_already_filled


async def test_tp_fired_signal_pending_sibling_cancelled(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # A still-pending sibling on a TP-fired signal (e.g. the TP engine's cancel failed)
    # is cancelled by the sync cycle's safety net, even though Supabase lists it active.
    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=1,
        mt5_ticket=4001,
        order_type="buy_limit",
        lot_size=0.1,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
    )
    await sqlite_db.mark_signal_tp_fired(1)
    mock_mt5.cancel_pending_order.return_value = make_order_result(ticket=4001)

    supabase = _mock_supabase(signals=[_make_supabase_row(limit_id=1, signal_id=1)])
    scheduler = _mock_scheduler()

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.cancelled == 1
    mock_mt5.cancel_pending_order.assert_called_once_with(4001)
    assert len(await sqlite_db.get_pending_orders()) == 0


# ---------------------------------------------------------------------------
# Spread hour: pending orders are cancelled, placement is skipped
# ---------------------------------------------------------------------------


async def test_spread_hour_cancels_pending(sqlite_db, mock_mt5, sample_config) -> None:
    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=1,
        mt5_ticket=2001,
        order_type="buy_limit",
        lot_size=0.1,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
    )
    mock_mt5.cancel_pending_order.return_value = make_order_result(ticket=2001)

    supabase = _mock_supabase(signals=[_make_supabase_row(limit_id=1)])
    scheduler = _mock_scheduler(cancel_pending=True)

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.cancelled == 1
    mock_mt5.cancel_pending_order.assert_called_once_with(2001)

    # Verify SQLite row is now spread_cancelled
    rows = await sqlite_db.get_pending_orders()
    assert len(rows) == 0


async def test_spread_hour_skips_new_placements(sqlite_db, mock_mt5, sample_config) -> None:
    # No pending in SQLite, one new limit from Supabase, but spread hour active → no placement
    supabase = _mock_supabase(signals=[_make_supabase_row(limit_id=99)])
    scheduler = _mock_scheduler(cancel_pending=True)

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.placed == 0
    mock_mt5.order_send.assert_not_called()


# ---------------------------------------------------------------------------
# Proximity gate uses the feed frame (not the broker frame) for offset symbols
# ---------------------------------------------------------------------------


async def test_proximity_uses_feed_mid_for_offset_symbol(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # SPX500USD limit at the feed price 4590.5; the feed mid is right on it (within the
    # 20-pt index proximity), but the BROKER mid is 4650.5 — 60 pts away. Comparing the
    # feed price to the broker mid (the old bug) would skip this as "outside proximity".
    # With the fix it passes proximity and proceeds to offset (which fails here, with no
    # mocked history → an error, not a proximity skip), proving the gate used the feed mid.
    mock_mt5.symbol_info.return_value = make_symbol_info(name="US500", digits=1, point=0.1)
    mock_mt5.symbol_info_tick.return_value = make_tick(
        bid=4650.0, ask=4651.0, time=int(datetime.now(UTC).timestamp())
    )
    mock_mt5.account_info.return_value = make_account_info()

    row = _make_supabase_row(limit_id=50, instrument="SPX500USD")
    row["stop_loss"] = 4585.0
    row["price_level"] = 4590.5
    supabase = _mock_supabase(
        signals=[row],
        live_prices={
            "SPX500USD": {
                "bid": 4590.0,
                "ask": 4591.0,
                "feed": "oanda",
                "updated_at": datetime.now(UTC),
            }
        },
    )
    supabase.fetch_signal_status.return_value = "active"
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.skipped == 0  # not proximity-rejected
    assert result.errors == 1  # reached offset compute, which had no broker history
    mock_mt5.order_send.assert_not_called()


# ---------------------------------------------------------------------------
# Offset drift: drifted pending orders are cancelled for re-placement
# ---------------------------------------------------------------------------


async def test_offset_drift_cancels_pending(sqlite_db, mock_mt5, sample_config) -> None:
    # Insert pending order for SPX500USD with offset_at_placement=10.0
    await sqlite_db.insert_order(
        limit_id=10,
        signal_id=1,
        mt5_ticket=3001,
        order_type="buy_limit",
        lot_size=0.01,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=4000.0,
        signal_type="standard",
        feed_price=4500.0,
        mt5_price=4510.0,
        offset=10.0,
    )
    mock_mt5.cancel_pending_order.return_value = make_order_result(ticket=3001)
    # US500 symbol info (mapped from SPX500USD)
    mock_mt5.symbol_info.return_value = make_symbol_info(name="US500", digits=1, point=0.1)

    row = _make_supabase_row(limit_id=10, instrument="SPX500USD")
    row["stop_loss"] = 4000.0
    row["price_level"] = 4510.0
    supabase = _mock_supabase(
        signals=[row],
        live_prices={
            "SPX500USD": {
                "bid": 4590.0,
                "ask": 4591.0,
                "feed": "oanda",
                "updated_at": datetime.now(UTC),
            }
        },
    )
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    # Patch OffsetCalculator on this instance to simulate large drift
    cycle._offset_calc.get_offset = MagicMock(return_value=90.0)
    cycle._offset_calc.check_drift = MagicMock(return_value=True)

    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.cancelled == 1
    mock_mt5.cancel_pending_order.assert_called_once_with(3001)

    rows = await sqlite_db.get_pending_orders()
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# Proximity drift: a placed pending that walks outside proximity is cancelled
# ---------------------------------------------------------------------------


async def test_proximity_drift_cancels_far_pending(sqlite_db, mock_mt5, sample_config) -> None:
    # Known EURUSD pending sitting ~90 pips from the broker mid (1.10001) — well outside
    # the 15-pip forex proximity. SL matches the DB so the SL-change loop stays out of it.
    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=1,
        mt5_ticket=2001,
        order_type="buy_limit",
        lot_size=0.1,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
    )
    mock_mt5.cancel_pending_order.return_value = make_order_result(ticket=2001)

    supabase = _mock_supabase(signals=[_make_supabase_row(limit_id=1)])  # price_level 1.09100
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.cancelled == 1
    mock_mt5.cancel_pending_order.assert_called_once_with(2001)
    assert len(await sqlite_db.get_pending_orders()) == 0


# ---------------------------------------------------------------------------
# Ladder resize: a TM edit that changes total_limits re-places the survivors
# ---------------------------------------------------------------------------


async def _insert_near_pending(sqlite_db, limit_id, ticket, ladder_size, lot=0.05):
    # Sits ~1 pip off the mock mid so proximity drift leaves it alone.
    await sqlite_db.insert_order(
        limit_id=limit_id,
        signal_id=1,
        mt5_ticket=ticket,
        order_type="buy_limit",
        lot_size=lot,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
        ladder_size=ladder_size,
    )


async def test_ladder_growth_cancels_stale_sized_pendings(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # Ladder edited 4 -> 5: the four orders placed at total/4 are cancelled so
    # they re-place at total/5 next cycle.
    for i, ticket in enumerate((2001, 2002, 2003, 2004), start=1):
        await _insert_near_pending(sqlite_db, i, ticket, ladder_size=4)
    mock_mt5.cancel_pending_order.side_effect = lambda t: make_order_result(ticket=t)

    rows = []
    for i in range(1, 5):
        r = _make_supabase_row(limit_id=i, total_limits=5)
        r["price_level"] = 1.10010
        rows.append(r)
    supabase = _mock_supabase(signals=rows)

    cycle = SyncCycle()
    result = await cycle.run(
        supabase, sqlite_db, mock_mt5, sample_config, _mock_scheduler(cancel_pending=False)
    )

    assert result.cancelled == 4
    assert len(await sqlite_db.get_pending_orders()) == 0


async def test_unchanged_ladder_leaves_pendings_alone(sqlite_db, mock_mt5, sample_config) -> None:
    await _insert_near_pending(sqlite_db, 1, 2001, ladder_size=4)
    row = _make_supabase_row(limit_id=1, total_limits=4)
    row["price_level"] = 1.10010

    cycle = SyncCycle()
    result = await cycle.run(
        _mock_supabase(signals=[row]),
        sqlite_db,
        mock_mt5,
        sample_config,
        _mock_scheduler(cancel_pending=False),
    )

    assert result.cancelled == 0
    mock_mt5.cancel_pending_order.assert_not_called()
    assert len(await sqlite_db.get_pending_orders()) == 1


async def test_null_ladder_size_is_not_replaced(sqlite_db, mock_mt5, sample_config) -> None:
    # Rows placed before the ladder_size column existed must survive the upgrade
    # rather than all being cancelled on the first cycle after the update.
    await _insert_near_pending(sqlite_db, 1, 2001, ladder_size=None)
    row = _make_supabase_row(limit_id=1, total_limits=5)
    row["price_level"] = 1.10010

    cycle = SyncCycle()
    result = await cycle.run(
        _mock_supabase(signals=[row]),
        sqlite_db,
        mock_mt5,
        sample_config,
        _mock_scheduler(cancel_pending=False),
    )

    assert result.cancelled == 0
    mock_mt5.cancel_pending_order.assert_not_called()
    assert len(await sqlite_db.get_pending_orders()) == 1


async def test_ladder_growth_resizes_every_limit_to_new_split(
    sqlite_db, mock_mt5, sample_config
) -> None:
    """The whole point: a 0.2 total_lot gold ladder edited 4 -> 5 ends up with
    five orders at 0.04, not four at 0.05 next to one at 0.04."""
    from bot.config.settings import LotExceptionConfig

    sample_config.lot_sizing.mode = "total_lot"
    sample_config.lot_sizing.exceptions = [
        LotExceptionConfig(symbol="XAUUSD", mode="total_lot", value=0.2)
    ]
    mock_mt5.symbol_info.return_value = make_symbol_info(
        name="XAUUSD", digits=2, point=0.01, volume_step=0.01, volume_min=0.01
    )
    mock_mt5.symbol_info_tick.return_value = make_tick(symbol="XAUUSD", bid=4304.0, ask=4304.5)
    mock_mt5.account_info.return_value = make_account_info()
    mock_mt5.order_get_by_ticket.return_value = None
    mock_mt5.cancel_pending_order.side_effect = lambda t: make_order_result(ticket=t)

    def _gold_rows(n_rows, total_limits):
        # Longs a few dollars under the mid: inside the $25 metals proximity and
        # below the ask, so every limit actually places.
        out = []
        for i in range(1, n_rows + 1):
            r = _make_supabase_row(limit_id=i, instrument="XAUUSD", total_limits=total_limits)
            r["stop_loss"], r["price_level"] = 4280.0, 4290.0 + i
            out.append(r)
        return out

    scheduler = _mock_scheduler(cancel_pending=False)
    cycle = SyncCycle()

    # Cycle 1 — the original 4-limit ladder goes on at 0.2/4.
    tickets = iter(range(7001, 7099))
    mock_mt5.order_send.side_effect = lambda *_: make_order_result(ticket=next(tickets))
    sb = _mock_supabase(signals=_gold_rows(4, 4))
    sb.fetch_signal_status.return_value = "active"
    await cycle.run(sb, sqlite_db, mock_mt5, sample_config, scheduler)

    placed = await sqlite_db.get_pending_orders()
    assert len(placed) == 4
    assert {r["lot_size"] for r in placed} == {0.05}

    # The TM edit lands: total_limits 4 -> 5, with only the new level inserted.
    # The rev moves because the TM's triggers bump it on every signals/limits
    # write — without that the cycle would keep serving the cached signal set.
    sb = _mock_supabase(signals=_gold_rows(5, 5))
    sb.fetch_signal_status.return_value = "active"
    sb.fetch_sync_state.return_value = (None, None, 2)

    # Cycle 2 — the four stale-sized orders are pulled, the new 5th goes on at 0.2/5.
    await cycle.run(sb, sqlite_db, mock_mt5, sample_config, scheduler)
    # Cycle 3 — the four re-place against the new ladder size.
    await cycle.run(sb, sqlite_db, mock_mt5, sample_config, scheduler)

    final = await sqlite_db.get_pending_orders()
    assert len(final) == 5
    assert {r["lot_size"] for r in final} == {0.04}
    assert {r["ladder_size"] for r in final} == {5}
    assert sum(r["lot_size"] for r in final) == pytest.approx(0.2, abs=1e-9)

    # Cycle 4 — converged: re-placing must not re-trigger itself into a churn loop.
    settled = await cycle.run(sb, sqlite_db, mock_mt5, sample_config, scheduler)
    assert (settled.placed, settled.cancelled) == (0, 0)


async def test_ladder_resize_spares_signal_with_fill(sqlite_db, mock_mt5, sample_config) -> None:
    # A mid-trade ladder is never disturbed: its lot is pinned to the filled
    # sibling, so re-placing could not resize it anyway.
    await _insert_near_pending(sqlite_db, 1, 2001, ladder_size=4)
    await _insert_near_pending(sqlite_db, 2, 2002, ladder_size=4)
    await sqlite_db.mark_filled(2002, "2026-01-01T00:01:00+00:00")

    rows = []
    for i in (1, 2):
        r = _make_supabase_row(limit_id=i, total_limits=5)
        r["price_level"] = 1.10010
        rows.append(r)

    cycle = SyncCycle()
    result = await cycle.run(
        _mock_supabase(signals=rows),
        sqlite_db,
        mock_mt5,
        sample_config,
        _mock_scheduler(cancel_pending=False),
    )

    assert result.cancelled == 0
    mock_mt5.cancel_pending_order.assert_not_called()


async def test_proximity_drift_keeps_near_pending(sqlite_db, mock_mt5, sample_config) -> None:
    # Same setup but the limit sits ~1 pip from the broker mid — inside proximity, so it
    # must be left alone (not cancelled, not re-placed).
    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=1,
        mt5_ticket=2001,
        order_type="buy_limit",
        lot_size=0.1,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
    )
    row = _make_supabase_row(limit_id=1)
    row["price_level"] = 1.10010  # ~0.9 pips from mid 1.10001
    supabase = _mock_supabase(signals=[row])
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.cancelled == 0
    mock_mt5.cancel_pending_order.assert_not_called()
    assert len(await sqlite_db.get_pending_orders()) == 1


async def test_proximity_drift_keeps_pending_inside_hysteresis_band(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # ~18 pips from mid 1.10001: past the 15-pip placement threshold but inside the
    # 1.5x (22.5-pip) cancel distance, so the pending stays. Without the band, price
    # fluctuating around 15 pips would cancel and re-place this order every cycle.
    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=1,
        mt5_ticket=2001,
        order_type="buy_limit",
        lot_size=0.1,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
    )
    row = _make_supabase_row(limit_id=1)
    row["price_level"] = 1.09820
    supabase = _mock_supabase(signals=[row])
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.cancelled == 0
    mock_mt5.cancel_pending_order.assert_not_called()
    assert len(await sqlite_db.get_pending_orders()) == 1


async def test_proximity_drift_cancels_pending_beyond_hysteresis_band(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # ~25 pips out — past the 22.5-pip cancel distance, so the band doesn't save it.
    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=1,
        mt5_ticket=2001,
        order_type="buy_limit",
        lot_size=0.1,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
    )
    mock_mt5.cancel_pending_order.return_value = make_order_result(ticket=2001)
    row = _make_supabase_row(limit_id=1)
    row["price_level"] = 1.09750
    supabase = _mock_supabase(signals=[row])
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.cancelled == 1
    assert len(await sqlite_db.get_pending_orders()) == 0


async def test_proximity_drift_keeps_whole_ladder_when_closest_near(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # Two-limit signal: one limit ~0.9 pips from mid (inside proximity), one ~90 pips out.
    # The ladder is evaluated as one unit — since the closest limit is near (same min-distance
    # rule placement uses), neither limit is cancelled. Cancelling only the far one would fight
    # placement and churn the whole ladder every cycle.
    for lid, ticket in ((1, 2001), (2, 2002)):
        await sqlite_db.insert_order(
            limit_id=lid,
            signal_id=1,
            mt5_ticket=ticket,
            order_type="buy_limit",
            lot_size=0.1,
            placed_at="2026-01-01T00:00:00+00:00",
            db_stop_loss=1.08500,
            signal_type="standard",
        )
    near = _make_supabase_row(limit_id=1)
    near["price_level"] = 1.10010  # ~0.9 pips from mid 1.10001
    far = _make_supabase_row(limit_id=2)  # price_level 1.09100, ~90 pips out
    supabase = _mock_supabase(signals=[near, far])
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.cancelled == 0
    mock_mt5.cancel_pending_order.assert_not_called()
    assert len(await sqlite_db.get_pending_orders()) == 2


async def test_proximity_drift_cancels_whole_ladder_when_all_far(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # Both limits sit well outside proximity — the whole ladder is cancelled together.
    for lid, ticket in ((1, 2001), (2, 2002)):
        await sqlite_db.insert_order(
            limit_id=lid,
            signal_id=1,
            mt5_ticket=ticket,
            order_type="buy_limit",
            lot_size=0.1,
            placed_at="2026-01-01T00:00:00+00:00",
            db_stop_loss=1.08500,
            signal_type="standard",
        )
    mock_mt5.cancel_pending_order.side_effect = lambda t: make_order_result(ticket=t)
    a = _make_supabase_row(limit_id=1)  # 1.09100, ~90 pips out
    b = _make_supabase_row(limit_id=2)
    b["price_level"] = 1.09000  # ~100 pips out
    supabase = _mock_supabase(signals=[a, b])
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.cancelled == 2
    assert len(await sqlite_db.get_pending_orders()) == 0


async def test_proximity_drift_spares_signal_with_fills(sqlite_db, mock_mt5, sample_config) -> None:
    # A far pending on a signal that already has a filled sibling is left alone — its
    # ladder is mid-trade, same guard offset drift uses.
    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=1,
        mt5_ticket=2001,
        order_type="buy_limit",
        lot_size=0.1,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
    )
    await sqlite_db.insert_order(
        limit_id=2,
        signal_id=1,
        mt5_ticket=2002,
        order_type="buy_limit",
        lot_size=0.1,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
    )
    await sqlite_db.mark_filled_and_set_position_ticket(2002, 5002, "2026-01-01T00:01:00+00:00")

    supabase = _mock_supabase(
        signals=[_make_supabase_row(limit_id=1), _make_supabase_row(limit_id=2)]
    )
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.cancelled == 0
    mock_mt5.cancel_pending_order.assert_not_called()


async def test_placement_skips_limit_past_current_price(sqlite_db, mock_mt5, sample_config) -> None:
    # New EURUSD long limit whose price is at/above current ask — would have to
    # be a buy_stop. Should be skipped, not placed as a stop.
    row = _make_supabase_row(limit_id=50, signal_id=5)
    row["price_level"] = 1.10001  # mid; adj_price = mid + spread > ask
    row["stop_loss"] = 1.09500
    mock_mt5.account_info.return_value = make_account_info()
    supabase = _mock_supabase(signals=[row])
    scheduler = _mock_scheduler()

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.placed == 0
    assert result.skipped == 1
    assert result.errors == 0
    mock_mt5.order_send.assert_not_called()


async def test_offset_drift_skipped_when_signal_marked_hit(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # Same setup as test_offset_drift_cancels_pending, but signal_status='hit'.
    # The remaining pending limit must NOT be cancelled — re-placing it at a
    # fresh offset would leave it inconsistent with the already-hit limit.
    await sqlite_db.insert_order(
        limit_id=10,
        signal_id=1,
        mt5_ticket=3001,
        order_type="buy_limit",
        lot_size=0.01,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=4000.0,
        signal_type="standard",
        feed_price=4500.0,
        mt5_price=4510.0,
        offset=10.0,
    )
    mock_mt5.symbol_info.return_value = make_symbol_info(name="US500", digits=1, point=0.1)

    row = _make_supabase_row(limit_id=10, instrument="SPX500USD", signal_status="hit")
    row["stop_loss"] = 4000.0
    row["price_level"] = 4510.0
    supabase = _mock_supabase(
        signals=[row],
        live_prices={
            "SPX500USD": {
                "bid": 4590.0,
                "ask": 4591.0,
                "feed": "oanda",
                "updated_at": datetime.now(UTC),
            }
        },
    )
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    cycle._offset_calc.get_offset = MagicMock(return_value=90.0)
    cycle._offset_calc.check_drift = MagicMock(return_value=True)

    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.cancelled == 0
    mock_mt5.cancel_pending_order.assert_not_called()

    rows = await sqlite_db.get_pending_orders()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Stale-pending sweep: hit limits are held, genuinely-gone limits are cancelled
# ---------------------------------------------------------------------------


async def test_stale_pending_kept_when_limit_marked_hit(sqlite_db, mock_mt5, sample_config) -> None:
    # The TM marked the limit 'hit', so it drops out of the pending Supabase
    # query — but it's still in hit_limit_ids (signal alive). Our pending order
    # must be held, not stale-cancelled: usually a sub-pip price mismatch.
    await sqlite_db.insert_order(
        limit_id=10,
        signal_id=1,
        mt5_ticket=3001,
        order_type="buy_limit",
        lot_size=0.01,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=4000.0,
        signal_type="standard",
    )
    supabase = _mock_supabase(signals=[], hit_limit_ids={10})
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.cancelled == 0
    mock_mt5.cancel_pending_order.assert_not_called()
    rows = await sqlite_db.get_pending_orders()
    assert len(rows) == 1


async def test_stale_pending_cancelled_when_signal_gone(sqlite_db, mock_mt5, sample_config) -> None:
    # Limit is gone from Supabase and NOT in hit_limit_ids (signal cancelled /
    # closed) → the pending order is still stale-cancelled.
    await sqlite_db.insert_order(
        limit_id=10,
        signal_id=1,
        mt5_ticket=3001,
        order_type="buy_limit",
        lot_size=0.01,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=4000.0,
        signal_type="standard",
    )
    mock_mt5.cancel_pending_order.return_value = make_order_result(ticket=3001)
    supabase = _mock_supabase(signals=[], hit_limit_ids=set())
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.cancelled == 1
    mock_mt5.cancel_pending_order.assert_called_once_with(3001)
    rows = await sqlite_db.get_pending_orders()
    assert len(rows) == 0


async def test_stale_pending_kept_when_signal_profit_marked_and_position_held(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # The TM marked the signal 'profit', so its still-pending limit drops out of
    # the active Supabase query. We still hold a filled position for the signal,
    # so the remaining pending limit is held until our own TP engine closes out.
    await sqlite_db.insert_order(
        limit_id=10,
        signal_id=1,
        mt5_ticket=3001,
        order_type="buy_limit",
        lot_size=0.01,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=4000.0,
        signal_type="standard",
    )
    # A second limit on the same signal that already filled (open position).
    await sqlite_db.insert_order(
        limit_id=11,
        signal_id=1,
        mt5_ticket=3002,
        order_type="buy_limit",
        lot_size=0.01,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=4000.0,
        signal_type="standard",
    )
    await sqlite_db.mark_filled(3002, "2026-01-01T00:01:00+00:00")

    supabase = _mock_supabase(signals=[], profit_limit_ids={10: 1})
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.cancelled == 0
    mock_mt5.cancel_pending_order.assert_not_called()
    rows = await sqlite_db.get_pending_orders()
    assert len(rows) == 1


async def test_stale_pending_cancelled_when_profit_signal_has_no_position(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # Signal is 'profit'-marked but we hold no filled position for it — our own TP
    # engine is not running on it, so the leftover pending limit is stale-cancelled.
    await sqlite_db.insert_order(
        limit_id=10,
        signal_id=1,
        mt5_ticket=3001,
        order_type="buy_limit",
        lot_size=0.01,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=4000.0,
        signal_type="standard",
    )
    mock_mt5.cancel_pending_order.return_value = make_order_result(ticket=3001)
    supabase = _mock_supabase(signals=[], profit_limit_ids={10: 1})
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.cancelled == 1
    mock_mt5.cancel_pending_order.assert_called_once_with(3001)
    rows = await sqlite_db.get_pending_orders()
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# Crypto exemption from spread-hour and news-mode gates
# ---------------------------------------------------------------------------


async def test_spread_hour_skips_crypto_cancellation(sqlite_db, mock_mt5, sample_config) -> None:
    # Two pendings: one BTCUSDT (crypto) and one EURUSD. Spread hour fires.
    # Only the EURUSD order should be cancelled; BTCUSDT survives.
    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=1,
        mt5_ticket=4001,
        order_type="buy_limit",
        lot_size=0.10,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08,
        signal_type="standard",
        symbol="EURUSD",
    )
    await sqlite_db.insert_order(
        limit_id=2,
        signal_id=2,
        mt5_ticket=4002,
        order_type="buy_limit",
        lot_size=0.10,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=60000.0,
        signal_type="standard",
        symbol="BTCUSD",  # MT5 symbol; maps from BTCUSDT
    )
    mock_mt5.cancel_pending_order.return_value = make_order_result(ticket=4001)

    eur_row = _make_supabase_row(limit_id=1, signal_id=1, instrument="EURUSD")
    btc_row = _make_supabase_row(limit_id=2, signal_id=2, instrument="BTCUSDT")
    btc_row["stop_loss"] = 60000.0  # match SQLite to avoid the SL-change cancel path
    supabase = _mock_supabase(signals=[eur_row, btc_row])
    scheduler = _mock_scheduler(cancel_pending=True)

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.cancelled == 1
    mock_mt5.cancel_pending_order.assert_called_once_with(4001)
    pending = await sqlite_db.get_pending_orders()
    assert {r["mt5_ticket"] for r in pending} == {4002}


async def test_spread_hour_24h_stock_exempt_but_normal_stock_cancelled(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # 24h stocks carry the broker -24 suffix → exempt like crypto. A normal stock
    # (listed bare, in stock_no_suffix) is cancelled when the gate fires.
    sample_config.stock_no_suffix = ["AAPL.NAS"]  # AAPL listed bare → non-24h stock
    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=1,
        mt5_ticket=5001,
        order_type="buy_limit",
        lot_size=0.10,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=200.0,
        signal_type="standard",
        symbol="TSLA.NAS-24",
    )
    await sqlite_db.insert_order(
        limit_id=2,
        signal_id=2,
        mt5_ticket=5002,
        order_type="buy_limit",
        lot_size=0.10,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=150.0,
        signal_type="standard",
        symbol="AAPL.NAS",
    )
    mock_mt5.cancel_pending_order.return_value = make_order_result(ticket=5002)

    tsla = _make_supabase_row(limit_id=1, signal_id=1, instrument="TSLA.NAS")
    tsla["stop_loss"] = 200.0  # match SQLite to avoid the SL-change cancel path
    aapl = _make_supabase_row(limit_id=2, signal_id=2, instrument="AAPL.NAS")
    aapl["stop_loss"] = 150.0
    supabase = _mock_supabase(signals=[tsla, aapl])
    scheduler = _mock_scheduler(cancel_pending=True)

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.cancelled == 1
    mock_mt5.cancel_pending_order.assert_called_once_with(5002)
    pending = await sqlite_db.get_pending_orders()
    assert {r["mt5_ticket"] for r in pending} == {5001}


# ---------------------------------------------------------------------------
# Per-symbol news gate: cancel only pendings whose instrument is under news
# ---------------------------------------------------------------------------


async def test_news_cancels_only_matching_symbol(sqlite_db, mock_mt5, sample_config) -> None:
    # USD news active: EURUSD pending is cancelled, GBPAUD pending survives.
    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=1,
        mt5_ticket=7001,
        order_type="buy_limit",
        lot_size=0.10,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
        symbol="EURUSD",
    )
    await sqlite_db.insert_order(
        limit_id=2,
        signal_id=2,
        mt5_ticket=7002,
        order_type="buy_limit",
        lot_size=0.10,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
        symbol="GBPAUD",
    )
    mock_mt5.cancel_pending_order.return_value = make_order_result(ticket=7001)

    eur = _make_supabase_row(limit_id=1, signal_id=1, instrument="EURUSD")
    gbp = _make_supabase_row(limit_id=2, signal_id=2, instrument="GBPAUD")
    gbp["price_level"] = 1.10010  # near the mock mid so proximity drift leaves it alone
    supabase = _mock_supabase(signals=[eur, gbp], news_mode="USD")
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.cancelled == 1
    mock_mt5.cancel_pending_order.assert_called_once_with(7001)
    pending = await sqlite_db.get_pending_orders()
    assert {r["mt5_ticket"] for r in pending} == {7002}


async def test_news_all_cancels_every_pending(sqlite_db, mock_mt5, sample_config) -> None:
    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=1,
        mt5_ticket=7101,
        order_type="buy_limit",
        lot_size=0.10,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
        symbol="EURUSD",
    )
    await sqlite_db.insert_order(
        limit_id=2,
        signal_id=2,
        mt5_ticket=7102,
        order_type="buy_limit",
        lot_size=0.10,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
        symbol="GBPAUD",
    )
    mock_mt5.cancel_pending_order.side_effect = [
        make_order_result(ticket=7101),
        make_order_result(ticket=7102),
    ]

    eur = _make_supabase_row(limit_id=1, signal_id=1, instrument="EURUSD")
    gbp = _make_supabase_row(limit_id=2, signal_id=2, instrument="GBPAUD")
    supabase = _mock_supabase(signals=[eur, gbp], news_mode="ALL")
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.cancelled == 2
    pending = await sqlite_db.get_pending_orders()
    assert pending == []


async def test_news_crypto_pending_exempt(sqlite_db, mock_mt5, sample_config) -> None:
    # BTCUSDT pending survives even under ALL news (crypto is 24/7, exempt).
    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=1,
        mt5_ticket=7201,
        order_type="buy_limit",
        lot_size=0.10,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=60000.0,
        signal_type="standard",
        symbol="BTCUSD",
    )
    btc = _make_supabase_row(limit_id=1, signal_id=1, instrument="BTCUSDT")
    btc["stop_loss"] = 60000.0
    supabase = _mock_supabase(signals=[btc], news_mode="ALL")
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.cancelled == 0
    mock_mt5.cancel_pending_order.assert_not_called()
    pending = await sqlite_db.get_pending_orders()
    assert {r["mt5_ticket"] for r in pending} == {7201}


# ---------------------------------------------------------------------------
# News force-exit: close filled positions whose instrument is under news
# ---------------------------------------------------------------------------


async def _insert_filled(sqlite_db, *, mt5_ticket, signal_id, symbol, db_stop_loss=1.08500):
    await sqlite_db.insert_order(
        limit_id=mt5_ticket,
        signal_id=signal_id,
        mt5_ticket=mt5_ticket,
        order_type="buy_limit",
        lot_size=0.10,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=db_stop_loss,
        signal_type="standard",
        symbol=symbol,
    )
    await sqlite_db.mark_filled(mt5_ticket, "2026-01-01T00:01:00+00:00")


async def test_news_force_exits_matching_filled_position(
    sqlite_db, mock_mt5, sample_config
) -> None:
    await _insert_filled(sqlite_db, mt5_ticket=8001, signal_id=1, symbol="EURUSD")
    mock_mt5.positions_get.return_value = [make_position(ticket=8001, symbol="EURUSD")]
    mock_mt5.close_position.return_value = make_order_result(ticket=8001)

    supabase = _mock_supabase(signals=[], news_mode="USD")
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    mock_mt5.close_position.assert_called_once()
    assert mock_mt5.close_position.call_args.kwargs["comment"] == "force_news"
    assert await sqlite_db.get_filled_positions() == []


async def test_news_does_not_exit_unrelated_filled_position(
    sqlite_db, mock_mt5, sample_config
) -> None:
    await _insert_filled(sqlite_db, mt5_ticket=8101, signal_id=1, symbol="GBPAUD")
    mock_mt5.positions_get.return_value = [make_position(ticket=8101, symbol="GBPAUD")]

    supabase = _mock_supabase(signals=[], news_mode="USD")
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    mock_mt5.close_position.assert_not_called()
    assert {r["mt5_ticket"] for r in await sqlite_db.get_filled_positions()} == {8101}


async def test_news_does_not_exit_crypto_position(sqlite_db, mock_mt5, sample_config) -> None:
    # Crypto stays live through news, mirroring the placement-gate exemption.
    sample_config.symbol_map = {"BTCUSDT": "BTCUSD"}  # reverse-maps for asset-class detection
    await _insert_filled(
        sqlite_db, mt5_ticket=8201, signal_id=1, symbol="BTCUSD", db_stop_loss=60000.0
    )
    mock_mt5.positions_get.return_value = [make_position(ticket=8201, symbol="BTCUSD")]

    supabase = _mock_supabase(signals=[], news_mode="ALL")
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    mock_mt5.close_position.assert_not_called()
    assert {r["mt5_ticket"] for r in await sqlite_db.get_filled_positions()} == {8201}


# ---------------------------------------------------------------------------
# Volatility guard: vol_guard tokens gate trades exactly like news, but only
# when the user has enabled the feature (config.volatility_guard).
# ---------------------------------------------------------------------------


async def test_vol_guard_gates_like_news_when_enabled(sqlite_db, mock_mt5, sample_config) -> None:
    sample_config.volatility_guard = True
    await _insert_filled(sqlite_db, mt5_ticket=8301, signal_id=1, symbol="EURUSD")
    mock_mt5.positions_get.return_value = [make_position(ticket=8301, symbol="EURUSD")]
    mock_mt5.close_position.return_value = make_order_result(ticket=8301)

    # vol_guard tokens are per-pair (the volatility monitor writes the full pair).
    supabase = _mock_supabase(signals=[], news_mode=None, vol_guard="EURUSD")
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    mock_mt5.close_position.assert_called_once()
    assert await sqlite_db.get_filled_positions() == []


async def test_vol_guard_ignored_when_disabled(sqlite_db, mock_mt5, sample_config) -> None:
    # Default off: the vol_guard column is read but never acted on.
    assert sample_config.volatility_guard is False
    await _insert_filled(sqlite_db, mt5_ticket=8401, signal_id=1, symbol="EURUSD")
    mock_mt5.positions_get.return_value = [make_position(ticket=8401, symbol="EURUSD")]

    supabase = _mock_supabase(signals=[], news_mode=None, vol_guard="ALL")
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    mock_mt5.close_position.assert_not_called()
    assert {r["mt5_ticket"] for r in await sqlite_db.get_filled_positions()} == {8401}


async def test_vol_guard_is_per_pair(sqlite_db, mock_mt5, sample_config) -> None:
    # A per-pair token gates only that pair — USDJPY rides out EURUSD volatility
    # even though both share USD.
    sample_config.volatility_guard = True
    await _insert_filled(sqlite_db, mt5_ticket=8501, signal_id=1, symbol="USDJPY")
    mock_mt5.positions_get.return_value = [make_position(ticket=8501, symbol="USDJPY")]

    supabase = _mock_supabase(signals=[], news_mode=None, vol_guard="EURUSD")
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    mock_mt5.close_position.assert_not_called()
    assert {r["mt5_ticket"] for r in await sqlite_db.get_filled_positions()} == {8501}


async def test_vol_guard_closes_crypto_position(sqlite_db, mock_mt5, sample_config) -> None:
    # Crypto is exempt from news (24/7 markets don't share its liquidity events) but
    # not from volatility, which is measured straight off the price.
    sample_config.volatility_guard = True
    sample_config.symbol_map = {"BTCUSDT": "BTCUSD"}
    await _insert_filled(
        sqlite_db, mt5_ticket=8601, signal_id=1, symbol="BTCUSD", db_stop_loss=60000.0
    )
    mock_mt5.positions_get.return_value = [make_position(ticket=8601, symbol="BTCUSD")]
    mock_mt5.close_position.return_value = make_order_result(ticket=8601)

    supabase = _mock_supabase(signals=[], news_mode=None, vol_guard="BTCUSDT")
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    mock_mt5.close_position.assert_called_once()
    assert mock_mt5.close_position.call_args.kwargs["comment"] == "force_vol"
    assert await sqlite_db.get_filled_positions() == []


async def test_news_still_exempts_crypto_while_vol_guard_enabled(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # Splitting the token sets must not leak the news gate onto crypto.
    sample_config.volatility_guard = True
    sample_config.symbol_map = {"BTCUSDT": "BTCUSD"}
    await _insert_filled(
        sqlite_db, mt5_ticket=8701, signal_id=1, symbol="BTCUSD", db_stop_loss=60000.0
    )
    mock_mt5.positions_get.return_value = [make_position(ticket=8701, symbol="BTCUSD")]

    supabase = _mock_supabase(signals=[], news_mode="ALL", vol_guard=None)
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    mock_mt5.close_position.assert_not_called()
    assert {r["mt5_ticket"] for r in await sqlite_db.get_filled_positions()} == {8701}


async def test_vol_guard_matches_broker_suffixed_symbol(sqlite_db, mock_mt5, sample_config) -> None:
    # TM writes the DB symbol (EURUSD); the position carries the broker's suffixed
    # symbol (EURUSDm). The gate reverse-maps before matching, so the two still meet.
    sample_config.volatility_guard = True
    sample_config.symbol_suffixes = [
        SymbolSuffixRule(suffix="m", asset_classes=["forex", "forex_jpy"])
    ]
    await _insert_filled(sqlite_db, mt5_ticket=8801, signal_id=1, symbol="EURUSDm")
    mock_mt5.positions_get.return_value = [make_position(ticket=8801, symbol="EURUSDm")]
    mock_mt5.close_position.return_value = make_order_result(ticket=8801)

    supabase = _mock_supabase(signals=[], news_mode=None, vol_guard="EURUSD")
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    mock_mt5.close_position.assert_called_once()
    assert await sqlite_db.get_filled_positions() == []


# ---------------------------------------------------------------------------
# Profit-weekend force-exit: flatten profit-marked signals before the weekend
# ---------------------------------------------------------------------------


async def test_profit_marked_position_closed_in_weekend_window(
    sqlite_db, mock_mt5, sample_config
) -> None:
    await _insert_filled(sqlite_db, mt5_ticket=9001, signal_id=1, symbol="USDCAD")
    mock_mt5.positions_get.return_value = [make_position(ticket=9001, symbol="USDCAD")]
    mock_mt5.close_position.return_value = make_order_result(ticket=9001)

    supabase = _mock_supabase(signals=[])
    supabase.fetch_signal_statuses.return_value = {1: {"status": "profit"}}
    scheduler = _mock_scheduler(cancel_pending=False)
    scheduler.is_weekend_window.return_value = True

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    mock_mt5.close_position.assert_called_once()
    assert mock_mt5.close_position.call_args.kwargs["comment"] == "force_profit_weekend"
    assert await sqlite_db.get_filled_positions() == []


async def test_profit_marked_position_kept_open_on_weekday(
    sqlite_db, mock_mt5, sample_config
) -> None:
    await _insert_filled(sqlite_db, mt5_ticket=9101, signal_id=1, symbol="USDCAD")
    mock_mt5.positions_get.return_value = [make_position(ticket=9101, symbol="USDCAD")]

    supabase = _mock_supabase(signals=[])
    supabase.fetch_signal_statuses.return_value = {1: {"status": "profit"}}
    scheduler = _mock_scheduler(cancel_pending=False)
    scheduler.is_weekend_window.return_value = False

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    mock_mt5.close_position.assert_not_called()
    assert {r["mt5_ticket"] for r in await sqlite_db.get_filled_positions()} == {9101}


async def test_profit_marked_crypto_kept_open_in_weekend_window(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # Crypto trades 24/7 through the weekend, mirroring the gate exemptions.
    sample_config.symbol_map = {"BTCUSDT": "BTCUSD"}  # reverse-maps for asset-class detection
    await _insert_filled(
        sqlite_db, mt5_ticket=9201, signal_id=1, symbol="BTCUSD", db_stop_loss=60000.0
    )
    mock_mt5.positions_get.return_value = [make_position(ticket=9201, symbol="BTCUSD")]

    supabase = _mock_supabase(signals=[])
    supabase.fetch_signal_statuses.return_value = {1: {"status": "profit"}}
    scheduler = _mock_scheduler(cancel_pending=False)
    scheduler.is_weekend_window.return_value = True

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    mock_mt5.close_position.assert_not_called()
    assert {r["mt5_ticket"] for r in await sqlite_db.get_filled_positions()} == {9201}


# ---------------------------------------------------------------------------
# Manual profit force-exit: a TM-marked manual 'profit' closes immediately
# (like breakeven), while an auto-TP 'profit' stays open for our TP engine.
# ---------------------------------------------------------------------------


async def test_manual_profit_position_closed_on_weekday(sqlite_db, mock_mt5, sample_config) -> None:
    await _insert_filled(sqlite_db, mt5_ticket=9301, signal_id=1, symbol="USDCAD")
    mock_mt5.positions_get.return_value = [make_position(ticket=9301, symbol="USDCAD")]
    mock_mt5.close_position.return_value = make_order_result(ticket=9301)

    supabase = _mock_supabase(signals=[])
    supabase.fetch_signal_statuses.return_value = {
        1: {"status": "profit", "closed_reason": "manual"}
    }
    scheduler = _mock_scheduler(cancel_pending=False)
    scheduler.is_weekend_window.return_value = False

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    mock_mt5.close_position.assert_called_once()
    assert mock_mt5.close_position.call_args.kwargs["comment"] == "force_profit"
    assert await sqlite_db.get_filled_positions() == []


async def test_auto_tp_profit_position_kept_open_on_weekday(
    sqlite_db, mock_mt5, sample_config
) -> None:
    await _insert_filled(sqlite_db, mt5_ticket=9401, signal_id=1, symbol="USDCAD")
    mock_mt5.positions_get.return_value = [make_position(ticket=9401, symbol="USDCAD")]

    supabase = _mock_supabase(signals=[])
    supabase.fetch_signal_statuses.return_value = {
        1: {"status": "profit", "closed_reason": "automatic"}
    }
    scheduler = _mock_scheduler(cancel_pending=False)
    scheduler.is_weekend_window.return_value = False

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    mock_mt5.close_position.assert_not_called()
    assert {r["mt5_ticket"] for r in await sqlite_db.get_filled_positions()} == {9401}


async def test_server_tp_signals_holds_auto_tp_only(sqlite_db, mock_mt5, sample_config) -> None:
    # The TP engine's follow-server trigger set, taken off the force-exit status
    # snapshot (no extra egress). Manual 'profit' is force-closed outright, so it must
    # never also arrive as a TP trigger.
    await _insert_filled(sqlite_db, mt5_ticket=9501, signal_id=1, symbol="USDCAD")
    await _insert_filled(sqlite_db, mt5_ticket=9502, signal_id=2, symbol="EURUSD")
    mock_mt5.positions_get.return_value = [
        make_position(ticket=9501, symbol="USDCAD"),
        make_position(ticket=9502, symbol="EURUSD"),
    ]
    mock_mt5.close_position.return_value = make_order_result(ticket=9502)

    supabase = _mock_supabase(signals=[])
    supabase.fetch_signal_statuses.return_value = {
        1: {"status": "profit", "closed_reason": "automatic"},
        2: {"status": "profit", "closed_reason": "manual"},
        3: {"status": "profit", "closed_reason": "automatic"},  # not held locally
    }
    scheduler = _mock_scheduler(cancel_pending=False)
    scheduler.is_weekend_window.return_value = False

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert cycle.server_tp_signals == {1}


# ---------------------------------------------------------------------------
# Cancel force-exit: 'near_miss' and 'manual' cancels close immediately on any
# day / asset class; 'expiry' stays gated to the weekend/crypto window because
# the TM rolls a weekday-expired hit signal over instead of truly closing it.
# ---------------------------------------------------------------------------


async def test_near_miss_cancel_closes_position_on_weekday(
    sqlite_db, mock_mt5, sample_config
) -> None:
    await _insert_filled(sqlite_db, mt5_ticket=9501, signal_id=1, symbol="USDCAD")
    mock_mt5.positions_get.return_value = [make_position(ticket=9501, symbol="USDCAD")]
    mock_mt5.close_position.return_value = make_order_result(ticket=9501)

    supabase = _mock_supabase(signals=[])
    supabase.fetch_signal_statuses.return_value = {
        1: {"status": "cancelled", "closed_reason": "near_miss"}
    }
    scheduler = _mock_scheduler(cancel_pending=False)
    scheduler.is_weekend_window.return_value = False

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    mock_mt5.close_position.assert_called_once()
    assert mock_mt5.close_position.call_args.kwargs["comment"] == "force_cancelled"
    assert await sqlite_db.get_filled_positions() == []


@pytest.mark.parametrize(
    "closed_reason",
    ["manual", "news:EUR", "spread_hour", "late_market"],
)
async def test_void_cancel_closes_position_on_weekday(
    sqlite_db, mock_mt5, sample_config, closed_reason
) -> None:
    # Any void/false-trigger cancel (operator !cancel, news window, spread hour, late
    # market) closes on any day — only 'expiry' stays gated.
    await _insert_filled(sqlite_db, mt5_ticket=9601, signal_id=1, symbol="USDCAD")
    mock_mt5.positions_get.return_value = [make_position(ticket=9601, symbol="USDCAD")]
    mock_mt5.close_position.return_value = make_order_result(ticket=9601)

    supabase = _mock_supabase(signals=[])
    supabase.fetch_signal_statuses.return_value = {
        1: {"status": "cancelled", "closed_reason": closed_reason}
    }
    scheduler = _mock_scheduler(cancel_pending=False)
    scheduler.is_weekend_window.return_value = False

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    mock_mt5.close_position.assert_called_once()
    assert await sqlite_db.get_filled_positions() == []


async def test_expiry_cancel_keeps_position_open_on_weekday(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # A weekday expiry cancellation (TM rolls the hit signal over) holds the position.
    await _insert_filled(sqlite_db, mt5_ticket=9701, signal_id=1, symbol="USDCAD")
    mock_mt5.positions_get.return_value = [make_position(ticket=9701, symbol="USDCAD")]

    supabase = _mock_supabase(signals=[])
    supabase.fetch_signal_statuses.return_value = {
        1: {"status": "cancelled", "closed_reason": "expiry"}
    }
    scheduler = _mock_scheduler(cancel_pending=False)
    scheduler.is_weekend_window.return_value = False

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    mock_mt5.close_position.assert_not_called()
    assert {r["mt5_ticket"] for r in await sqlite_db.get_filled_positions()} == {9701}


async def test_forced_exit_cancels_remaining_pending_limits(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # A breakeven force-exit must tear down the signal's still-pending limits in the same
    # pass, even while the signal-sets fetch still lists that limit as active (its cache
    # trails the status cache). Otherwise the pending could fill after the signal is dead.
    await _insert_filled(sqlite_db, mt5_ticket=9801, signal_id=1, symbol="EURUSD")
    await sqlite_db.insert_order(
        limit_id=9802,
        signal_id=1,
        mt5_ticket=9802,
        order_type="buy_limit",
        lot_size=0.10,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
        symbol="EURUSD",
    )
    mock_mt5.positions_get.return_value = [make_position(ticket=9801, symbol="EURUSD")]
    mock_mt5.close_position.return_value = make_order_result(ticket=9801)
    mock_mt5.cancel_pending_order.return_value = make_order_result(ticket=9802)

    # Signal-sets fetch still shows the pending limit active — so the stale-pending sweep
    # leaves it alone and only the force-exit path can cancel it.
    supabase = _mock_supabase(signals=[_make_supabase_row(limit_id=9802, signal_id=1)])
    supabase.fetch_signal_statuses.return_value = {1: {"status": "breakeven"}}
    scheduler = _mock_scheduler(cancel_pending=False)
    scheduler.is_weekend_window.return_value = False

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    mock_mt5.close_position.assert_called_once()
    assert mock_mt5.close_position.call_args.kwargs["comment"] == "force_breakeven"
    mock_mt5.cancel_pending_order.assert_called_once_with(9802)
    assert await sqlite_db.get_filled_positions() == []
    assert await sqlite_db.get_pending_orders() == []


async def _run_breakeven_exit(
    sqlite_db, mock_mt5, sample_config, *, spike: bool, retain_server_limits: bool = False
) -> SyncCycle:
    await _insert_filled(sqlite_db, mt5_ticket=9811, signal_id=1, symbol="EURUSD")
    if retain_server_limits:
        await sqlite_db.insert_order(
            limit_id=9812,
            signal_id=1,
            mt5_ticket=9812,
            order_type="buy_limit",
            lot_size=0.10,
            placed_at="2026-01-01T00:00:00+00:00",
            db_stop_loss=1.08500,
            signal_type="standard",
            symbol="EURUSD",
        )
    mock_mt5.positions_get.return_value = [make_position(ticket=9811, symbol="EURUSD")]
    mock_mt5.close_position.return_value = make_order_result(ticket=9811)

    supabase = _mock_supabase(
        signals=[] if retain_server_limits else [_make_supabase_row(limit_id=9812, signal_id=1)],
        breakeven_limit_ids={9812: 1} if retain_server_limits else None,
    )
    supabase.fetch_signal_statuses.return_value = {1: {"status": "breakeven"}}
    scheduler = _mock_scheduler(cancel_pending=spike)
    scheduler.is_weekend_window.return_value = False

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)
    return cycle


async def test_breakeven_force_exit_deferred_inside_the_spread_spike(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # We strip SLs across this window so a spread blowout can't stop us out; closing on
    # a breakeven that arrived inside it books the exact price the stripping dodges.
    cycle = await _run_breakeven_exit(sqlite_db, mock_mt5, sample_config, spike=True)

    mock_mt5.close_position.assert_not_called()
    # The status is deliberately not recorded, so the exit is still pending and fires on
    # the first cycle after the window rather than being swallowed.
    assert cycle._last_signal_status.get(1) != "breakeven"


async def test_breakeven_force_exit_fires_outside_the_spike(
    sqlite_db, mock_mt5, sample_config
) -> None:
    await _run_breakeven_exit(sqlite_db, mock_mt5, sample_config, spike=False)

    mock_mt5.close_position.assert_called_once()
    assert mock_mt5.close_position.call_args.kwargs["comment"] == "force_breakeven"


async def test_server_breakeven_is_ignored_when_follow_be_disabled(
    sqlite_db, mock_mt5, sample_config
) -> None:
    sample_config.tp_config.follow_server_tp = True
    sample_config.tp_config.follow_server_be = False
    cycle = await _run_breakeven_exit(
        sqlite_db, mock_mt5, sample_config, spike=False, retain_server_limits=True
    )

    mock_mt5.close_position.assert_not_called()
    assert {r["mt5_ticket"] for r in await sqlite_db.get_filled_positions()} == {9811}
    assert {r["mt5_ticket"] for r in await sqlite_db.get_pending_orders()} == {9812}
    assert cycle._last_signal_status.get(1) != "breakeven"


async def test_breakeven_force_exit_not_deferred_for_crypto(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # Crypto books stay tight through the spike, so it has nothing to wait for.
    await _insert_filled(sqlite_db, mt5_ticket=9821, signal_id=1, symbol="BTCUSDT")
    mock_mt5.positions_get.return_value = [make_position(ticket=9821, symbol="BTCUSDT")]
    mock_mt5.close_position.return_value = make_order_result(ticket=9821)

    supabase = _mock_supabase(signals=[])
    supabase.fetch_signal_statuses.return_value = {1: {"status": "breakeven"}}
    scheduler = _mock_scheduler(cancel_pending=True)
    scheduler.is_weekend_window.return_value = False

    await SyncCycle().run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    mock_mt5.close_position.assert_called_once()


# ---------------------------------------------------------------------------
# Offset drift interval throttle: skip re-evaluation within 30 min window
# ---------------------------------------------------------------------------


async def test_drift_skipped_when_sibling_already_filled(
    sqlite_db, mock_mt5, sample_config
) -> None:
    """A pending limit on a signal whose sibling already filled must not be cancelled
    by offset drift — once a limit has hit, the remaining pendings should hold their
    placement instead of being yanked further from the existing entry."""
    # Sibling limit on the same signal — already filled into a position
    await sqlite_db.insert_order(
        limit_id=20,
        signal_id=7,
        mt5_ticket=6000,
        order_type="buy_limit",
        lot_size=0.01,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=4000.0,
        signal_type="standard",
        feed_price=4500.0,
        mt5_price=4510.0,
        offset=10.0,
    )
    await sqlite_db.mark_filled(6000, "2026-01-01T00:01:00+00:00")

    # The pending limit we want to keep
    await sqlite_db.insert_order(
        limit_id=21,
        signal_id=7,
        mt5_ticket=6001,
        order_type="buy_limit",
        lot_size=0.01,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=4000.0,
        signal_type="standard",
        feed_price=4500.0,
        mt5_price=4510.0,
        offset=10.0,
    )
    mock_mt5.cancel_pending_order.return_value = make_order_result(ticket=6001)
    mock_mt5.symbol_info.return_value = make_symbol_info(name="US500", digits=1, point=0.1)

    pending_row = _make_supabase_row(limit_id=21, signal_id=7, instrument="SPX500USD")
    pending_row["stop_loss"] = 4000.0
    pending_row["price_level"] = 4510.0
    filled_row = _make_supabase_row(limit_id=20, signal_id=7, instrument="SPX500USD")
    filled_row["stop_loss"] = 4000.0
    filled_row["price_level"] = 4510.0
    supabase = _mock_supabase(
        signals=[filled_row, pending_row],
        live_prices={
            "SPX500USD": {
                "bid": 4590.0,
                "ask": 4591.0,
                "feed": "oanda",
                "updated_at": datetime.now(UTC),
            }
        },
    )
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    cycle._offset_calc.get_offset = MagicMock(return_value=90.0)
    cycle._offset_calc.check_drift = MagicMock(return_value=True)

    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.cancelled == 0
    # Drift check should be skipped entirely — get_offset should not even be invoked
    # on a signal that has fills, regardless of throttle state.
    cycle._offset_calc.get_offset.assert_not_called()
    pending = await sqlite_db.get_pending_orders()
    assert {r["mt5_ticket"] for r in pending} == {6001}


async def test_drift_check_skipped_within_interval(sqlite_db, mock_mt5, sample_config) -> None:
    from datetime import UTC, datetime, timedelta

    await sqlite_db.insert_order(
        limit_id=10,
        signal_id=1,
        mt5_ticket=5001,
        order_type="buy_limit",
        lot_size=0.01,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=4000.0,
        signal_type="standard",
        feed_price=4500.0,
        mt5_price=4510.0,
        offset=10.0,
    )
    # Mark a recent offset check (5 minutes ago), within the default 30-min throttle
    recent = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    await sqlite_db.update_last_offset_check(5001, recent)

    mock_mt5.cancel_pending_order.return_value = make_order_result(ticket=5001)
    mock_mt5.symbol_info.return_value = make_symbol_info(name="US500", digits=1, point=0.1)

    row = _make_supabase_row(limit_id=10, instrument="SPX500USD")
    row["stop_loss"] = 4000.0
    row["price_level"] = 4590.5  # on the feed mid, so proximity drift stays out of it
    supabase = _mock_supabase(
        signals=[row],
        live_prices={
            "SPX500USD": {
                "bid": 4590.0,
                "ask": 4591.0,
                "feed": "oanda",
                "updated_at": datetime.now(UTC),
            }
        },
    )
    scheduler = _mock_scheduler(cancel_pending=False)

    cycle = SyncCycle()
    # Even with large drift configured, the throttle should prevent cancellation
    cycle._offset_calc.get_offset = MagicMock(return_value=90.0)
    cycle._offset_calc.check_drift = MagicMock(return_value=True)

    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.cancelled == 0
    cycle._offset_calc.get_offset.assert_not_called()
    rows = await sqlite_db.get_pending_orders()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Multi-broker symbol availability: catalogue-based skip + symbol_select
# ---------------------------------------------------------------------------


async def test_unmapped_symbol_skipped_and_logged_once(sqlite_db, mock_mt5, sample_config) -> None:
    # GCZ26_CFD isn't in the broker catalogue → skip cleanly, no order, no select call,
    # and the skip is logged exactly once across cycles.
    mock_mt5.symbols_get.return_value = frozenset({"EURUSD", "US500"})

    supabase = _mock_supabase(signals=[_make_supabase_row(limit_id=70, instrument="GCZ26_CFD")])
    scheduler = _mock_scheduler()

    cycle = SyncCycle()
    r1 = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)
    r2 = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert r1.placed == 0 and r2.placed == 0
    mock_mt5.order_send.assert_not_called()
    assert "GCZ26_CFD" in cycle._logged_unmapped
    # Never selected (it doesn't exist on the broker)
    for call in mock_mt5.symbol_select.call_args_list:
        assert call.args[0] != "GCZ26_CFD"


async def test_catalogued_symbol_is_selected(sqlite_db, mock_mt5, sample_config) -> None:
    # EURUSD is in the catalogue → it gets selected into MarketWatch before use.
    mock_mt5.symbols_get.return_value = frozenset({"EURUSD"})

    supabase = _mock_supabase(signals=[_make_supabase_row(limit_id=71, instrument="EURUSD")])
    scheduler = _mock_scheduler()

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    mock_mt5.symbol_select.assert_any_call("EURUSD")
    assert "EURUSD" not in cycle._logged_unmapped


# ---------------------------------------------------------------------------
# Spread-hour SL strip / restore
# ---------------------------------------------------------------------------


def _strip_scheduler(in_window: bool) -> MagicMock:
    sched = MagicMock()
    sched.is_sl_strip_window.return_value = in_window
    return sched


async def test_sl_strip_removes_sl_in_window(sqlite_db, mock_mt5, sample_config) -> None:
    await _insert_filled(sqlite_db, mt5_ticket=3001, signal_id=1, symbol="EURUSD")
    pos = make_position(ticket=3001, sl=1.08500)
    mock_mt5.modify_position_sl.return_value = make_order_result(ticket=3001)

    cycle = SyncCycle()
    await cycle._manage_spread_hour_sls(
        sqlite_db, mock_mt5, [pos], _strip_scheduler(True), sample_config, set()
    )

    mock_mt5.modify_position_sl.assert_called_once_with(3001, "EURUSD", 0.0)
    row = await sqlite_db.get_order_by_ticket(3001)
    assert row["sl_stripped"] == 1
    assert row["last_known_mt5_sl"] == 1.08500  # pre-strip SL persisted for restore


async def test_sl_strip_exempts_crypto(sqlite_db, mock_mt5, sample_config) -> None:
    await _insert_filled(sqlite_db, mt5_ticket=3002, signal_id=1, symbol="BTCUSD")
    pos = make_position(ticket=3002, symbol="BTCUSD", sl=60000.0)

    cycle = SyncCycle()
    await cycle._manage_spread_hour_sls(
        sqlite_db, mock_mt5, [pos], _strip_scheduler(True), sample_config, set()
    )

    mock_mt5.modify_position_sl.assert_not_called()
    row = await sqlite_db.get_order_by_ticket(3002)
    assert row["sl_stripped"] == 0


async def test_sl_strip_idempotent_when_already_stripped(
    sqlite_db, mock_mt5, sample_config
) -> None:
    await _insert_filled(sqlite_db, mt5_ticket=3005, signal_id=1, symbol="EURUSD")
    await sqlite_db.set_sl_stripped(3005, 1)
    pos = make_position(ticket=3005, sl=0.0)

    cycle = SyncCycle()
    await cycle._manage_spread_hour_sls(
        sqlite_db, mock_mt5, [pos], _strip_scheduler(True), sample_config, set()
    )

    mock_mt5.modify_position_sl.assert_not_called()


async def test_sl_restore_resets_sl_after_window(sqlite_db, mock_mt5, sample_config) -> None:
    await _insert_filled(sqlite_db, mt5_ticket=3003, signal_id=1, symbol="EURUSD")
    await sqlite_db.update_sl(3003, 1.08500)  # pre-strip SL in last_known_mt5_sl
    await sqlite_db.set_sl_stripped(3003, 1)
    pos = make_position(ticket=3003, sl=0.0)
    mock_mt5.symbol_info_tick.return_value = make_tick(bid=1.10000, ask=1.10002)
    mock_mt5.modify_position_sl.return_value = make_order_result(ticket=3003)

    cycle = SyncCycle()
    await cycle._manage_spread_hour_sls(
        sqlite_db, mock_mt5, [pos], _strip_scheduler(False), sample_config, set()
    )

    mock_mt5.modify_position_sl.assert_called_once_with(3003, "EURUSD", 1.08500)
    mock_mt5.close_position.assert_not_called()
    row = await sqlite_db.get_order_by_ticket(3003)
    assert row["sl_stripped"] == 0


async def test_sl_restore_closes_when_price_past_stop(sqlite_db, mock_mt5, sample_config) -> None:
    await _insert_filled(sqlite_db, mt5_ticket=3004, signal_id=1, symbol="EURUSD")
    await sqlite_db.update_sl(3004, 1.08500)
    await sqlite_db.set_sl_stripped(3004, 1)
    pos = make_position(ticket=3004, sl=0.0, profit=-50.0)
    # bid below the stored stop → price moved past it while unprotected
    mock_mt5.symbol_info_tick.return_value = make_tick(bid=1.08000, ask=1.08002)
    mock_mt5.close_position.return_value = make_order_result(ticket=3004)

    cycle = SyncCycle()
    await cycle._manage_spread_hour_sls(
        sqlite_db, mock_mt5, [pos], _strip_scheduler(False), sample_config, set()
    )

    mock_mt5.close_position.assert_called_once()
    mock_mt5.modify_position_sl.assert_not_called()
    row = await sqlite_db.get_order_by_ticket(3004)
    assert row["status"] == "closed"


# ---------------------------------------------------------------------------
# Per-signal user overrides: skip (pull + never place) and manual (orphan)
# ---------------------------------------------------------------------------


async def test_skip_cancels_pending_and_closes_fills(sqlite_db, mock_mt5, sample_config) -> None:
    # Skipped signal with one pending order and one filled position: pull both.
    await sqlite_db.insert_order(
        limit_id=5001,
        signal_id=1,
        mt5_ticket=5001,
        order_type="buy_limit",
        lot_size=0.1,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
        symbol="EURUSD",
    )
    await _insert_filled(sqlite_db, mt5_ticket=5002, signal_id=1, symbol="EURUSD")
    await sqlite_db.set_signal_action(1, "skip")

    mock_mt5.orders_get.return_value = [make_order_info(ticket=5001)]
    mock_mt5.positions_get.return_value = [make_position(ticket=5002, symbol="EURUSD")]
    mock_mt5.cancel_pending_order.return_value = make_order_result(ticket=5001)
    mock_mt5.close_position.return_value = make_order_result(ticket=5002)

    supabase = _mock_supabase(signals=[])
    scheduler = _mock_scheduler()

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    mock_mt5.cancel_pending_order.assert_called_once_with(5001)
    mock_mt5.close_position.assert_called_once()
    assert mock_mt5.close_position.call_args.kwargs["comment"] == "skip"
    assert await sqlite_db.get_pending_orders() == []
    assert await sqlite_db.get_filled_positions() == []


async def test_skip_blocks_new_placement(sqlite_db, mock_mt5, sample_config) -> None:
    # A live signal the user skipped must never place, even with a fresh limit.
    await sqlite_db.set_signal_action(1, "skip")

    supabase = _mock_supabase(signals=[_make_supabase_row(limit_id=1, signal_id=1)])
    scheduler = _mock_scheduler()

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.placed == 0
    mock_mt5.order_send.assert_not_called()
    assert await sqlite_db.get_pending_orders() == []


async def test_manual_orphans_pending_and_skips_management(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # Manually-handled signal: pending stays put, fills are not force-exited even
    # under news that would otherwise close the position.
    await sqlite_db.insert_order(
        limit_id=6001,
        signal_id=1,
        mt5_ticket=6001,
        order_type="buy_limit",
        lot_size=0.1,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
        symbol="EURUSD",
    )
    await _insert_filled(sqlite_db, mt5_ticket=6002, signal_id=1, symbol="EURUSD")
    await sqlite_db.set_signal_action(1, "manual")

    mock_mt5.orders_get.return_value = [make_order_info(ticket=6001)]
    mock_mt5.positions_get.return_value = [make_position(ticket=6002, symbol="EURUSD")]

    supabase = _mock_supabase(signals=[], news_mode="USD")
    scheduler = _mock_scheduler()

    cycle = SyncCycle()
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    mock_mt5.cancel_pending_order.assert_not_called()
    mock_mt5.close_position.assert_not_called()
    assert {r["mt5_ticket"] for r in await sqlite_db.get_pending_orders()} == {6001}
    assert {r["mt5_ticket"] for r in await sqlite_db.get_filled_positions()} == {6002}


async def test_clear_signal_action_resumes_management(sqlite_db) -> None:
    await sqlite_db.set_signal_action(1, "manual")
    assert await sqlite_db.get_signal_actions() == {1: "manual"}
    await sqlite_db.clear_signal_action(1)
    assert await sqlite_db.get_signal_actions() == {}


# ---------------------------------------------------------------------------
# Channel + asset-class exclusions: drop signals before placement
# ---------------------------------------------------------------------------


async def test_excluded_channel_asset_blocks_placement(sqlite_db, mock_mt5, sample_config) -> None:
    # Exclude forex signals from channel 123 → a EURUSD limit on that channel never places.
    sample_config.excluded_channel_assets = [
        ExcludedChannelAssetConfig(channel="123", asset_class="forex")
    ]
    mock_mt5.account_info.return_value = make_account_info()
    row = _make_supabase_row(limit_id=1, instrument="EURUSD")
    row["channel_id"] = 123
    row["price_level"] = 1.09950
    supabase = _mock_supabase(signals=[row])
    scheduler = _mock_scheduler()

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.placed == 0
    mock_mt5.order_send.assert_not_called()
    assert 1 in cycle._logged_excluded


async def test_excluded_channel_asset_ignores_other_asset(
    sqlite_db, mock_mt5, sample_config
) -> None:
    # The same channel rule targets indices; a forex EURUSD limit is unaffected and places.
    sample_config.excluded_channel_assets = [
        ExcludedChannelAssetConfig(channel="123", asset_class="indices")
    ]
    mock_mt5.account_info.return_value = make_account_info()
    mock_mt5.order_send.return_value = make_order_result(ticket=2002)
    mock_mt5.order_get_by_ticket.return_value = None
    row = _make_supabase_row(limit_id=1, instrument="EURUSD")
    row["channel_id"] = 123
    row["price_level"] = 1.09950
    supabase = _mock_supabase(signals=[row])
    supabase.fetch_signal_status.return_value = "active"
    scheduler = _mock_scheduler()

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.placed == 1
    mock_mt5.order_send.assert_called_once()


async def test_excluded_asset_wildcard_channel(sqlite_db, mock_mt5, sample_config) -> None:
    # Channel left as "all": every forex signal is excluded regardless of channel.
    sample_config.excluded_channel_assets = [ExcludedChannelAssetConfig(asset_class="forex")]
    mock_mt5.account_info.return_value = make_account_info()
    row = _make_supabase_row(limit_id=1, instrument="EURUSD")
    row["channel_id"] = 999
    row["price_level"] = 1.09950
    supabase = _mock_supabase(signals=[row])
    scheduler = _mock_scheduler()

    cycle = SyncCycle()
    result = await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert result.placed == 0
    mock_mt5.order_send.assert_not_called()


# ---------------------------------------------------------------------------
# Rev-gated egress caches: the signals_rev watermark drives Supabase refetching
# ---------------------------------------------------------------------------


async def test_unchanged_rev_reuses_signal_set_cache(sqlite_db, mock_mt5, sample_config) -> None:
    supabase = _mock_supabase(signals=[_make_supabase_row(limit_id=1)])
    scheduler = _mock_scheduler()
    cycle = SyncCycle()

    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert supabase.fetch_sync_state.await_count == 2
    assert supabase.fetch_signal_sets.await_count == 1


async def test_rev_change_refetches_and_drops_status_cache(
    sqlite_db, mock_mt5, sample_config
) -> None:
    supabase = _mock_supabase(signals=[_make_supabase_row(limit_id=1)])
    supabase.fetch_sync_state.side_effect = [(None, None, 1), (None, None, 2)]
    scheduler = _mock_scheduler()
    cycle = SyncCycle()

    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)
    cycle._status_cache = {1: {"status": "active", "closed_reason": None}}
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert supabase.fetch_signal_sets.await_count == 2
    assert cycle._status_cache is None  # no filled positions, so nothing re-primed it


async def test_poll_failure_forces_signal_set_refetch(sqlite_db, mock_mt5, sample_config) -> None:
    # A failed sync-state poll leaves freshness unverifiable: the set cache is
    # dropped so the next read refetches (or fails and skips placement).
    supabase = _mock_supabase(signals=[_make_supabase_row(limit_id=1)])
    scheduler = _mock_scheduler()
    cycle = SyncCycle()

    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)
    supabase.fetch_sync_state.side_effect = Exception("pooler down")
    await cycle.run(supabase, sqlite_db, mock_mt5, sample_config, scheduler)

    assert supabase.fetch_signal_sets.await_count == 2


async def test_signal_set_max_age_legacy_vs_watermark() -> None:
    # Legacy DB (no watermark): the old 5s interval drives refetching.
    cycle = SyncCycle()
    supabase = _mock_supabase()
    t0 = 1000.0
    await cycle._fetch_signal_sets_cached(supabase, set(), t0)
    await cycle._fetch_signal_sets_cached(supabase, set(), t0 + 3)
    assert supabase.fetch_signal_sets.await_count == 1
    await cycle._fetch_signal_sets_cached(supabase, set(), t0 + 6)
    assert supabase.fetch_signal_sets.await_count == 2

    # Watermark present: no interval refetch until the 60s safety net lapses.
    cycle = SyncCycle()
    cycle._signals_rev = 7
    supabase = _mock_supabase()
    await cycle._fetch_signal_sets_cached(supabase, set(), t0)
    await cycle._fetch_signal_sets_cached(supabase, set(), t0 + 30)
    assert supabase.fetch_signal_sets.await_count == 1
    await cycle._fetch_signal_sets_cached(supabase, set(), t0 + 61)
    assert supabase.fetch_signal_sets.await_count == 2


def test_feed_for_symbol_reads_real_feed_not_asset_class(sample_config) -> None:
    config = sample_config.model_copy(
        update={"offset_instruments": ["USOILSPOT", "BTCUSDT", "SPX500USD"]}
    )
    live_prices = {
        # Oil is exness-fed; an asset-class guess would call this "oanda" and watch
        # the wrong feed's health.
        "USOILSPOT": {"feed": "exness"},
        "BTCUSDT": {"feed": "binance"},
        "SPX500USD": {"feed": "oanda"},
    }
    assert _feed_for_symbol("USOILSPOT", config, live_prices) == "exness"
    assert _feed_for_symbol("BTCUSDT", config, live_prices) == "binance"
    assert _feed_for_symbol("SPX500USD", config, live_prices) == "oanda"
    # Non-offset symbols are priced by the broker directly.
    assert _feed_for_symbol("EURUSD", config, live_prices) == "icmarkets"
    # No row written yet — no feed claim to make; proximity reports the real reason.
    assert _feed_for_symbol("SPX500USD", config, {}) is None
