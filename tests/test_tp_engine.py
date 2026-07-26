from unittest.mock import AsyncMock

import pytest

from bot.tp.asset_config import AssetClassConfig
from bot.tp.default_strategy import DefaultTPStrategy
from bot.tp.engine import TPEngine
from tests.conftest import (
    make_account_info,
    make_order_result,
    make_position,
    make_symbol_info,
    make_tick,
)


def _dollars_cfg(threshold=4.0, trail=2.0, pct=50) -> AssetClassConfig:
    return AssetClassConfig(
        profit_threshold=threshold,
        threshold_unit="dollars",
        partial_close_percent=pct,
        trailing_distance=trail,
    )


def _pips_cfg(threshold=7.0, trail=3.0, pct=50) -> AssetClassConfig:
    return AssetClassConfig(
        profit_threshold=threshold,
        threshold_unit="pips",
        partial_close_percent=pct,
        trailing_distance=trail,
    )


# ---------------------------------------------------------------------------
# should_trigger — dollars mode
# ---------------------------------------------------------------------------


def test_trigger_dollars_newest_above_threshold(mock_mt5) -> None:
    # price_open=1.0, bid=5.5, move=4.5 >= threshold=4.0 → True (no others)
    pos = make_position(ticket=1, price_open=1.0, type=0, profit=0.0)
    mock_mt5.symbol_info_tick.return_value = make_tick(bid=5.5, ask=5.502)

    strategy = DefaultTPStrategy()
    assert strategy.should_trigger([pos], _dollars_cfg(threshold=4.0), mock_mt5) is True


def test_trigger_dollars_newest_below_threshold(mock_mt5) -> None:
    pos = make_position(ticket=1, price_open=1.0, type=0, profit=0.0)
    mock_mt5.symbol_info_tick.return_value = make_tick(bid=4.0, ask=4.002)  # move=3.0 < 4.0

    strategy = DefaultTPStrategy()
    assert strategy.should_trigger([pos], _dollars_cfg(threshold=4.0), mock_mt5) is False


def test_trigger_dollars_others_negative_blocks(mock_mt5) -> None:
    # newest (ticket=2, highest) in profit, others (ticket=1) losing → no trigger
    pos1 = make_position(ticket=1, price_open=1.0, type=0, profit=-50.0)
    pos2 = make_position(ticket=2, price_open=1.0, type=0, profit=0.0)
    mock_mt5.symbol_info_tick.return_value = make_tick(bid=6.0, ask=6.002)  # move=5.0 >= 4.0

    strategy = DefaultTPStrategy()
    assert strategy.should_trigger([pos1, pos2], _dollars_cfg(threshold=4.0), mock_mt5) is False


def test_trigger_dollars_others_nonnegative_passes(mock_mt5) -> None:
    pos1 = make_position(ticket=1, price_open=1.0, type=0, profit=0.0)  # others sum = 0
    pos2 = make_position(ticket=2, price_open=1.0, type=0, profit=0.0)
    mock_mt5.symbol_info_tick.return_value = make_tick(bid=6.0, ask=6.002)

    strategy = DefaultTPStrategy()
    assert strategy.should_trigger([pos1, pos2], _dollars_cfg(threshold=4.0), mock_mt5) is True


# ---------------------------------------------------------------------------
# should_trigger — pips mode
# ---------------------------------------------------------------------------


def test_trigger_pips_mode(mock_mt5) -> None:
    # price_open=1.09000, bid=1.09800 → move=0.008, pip_sz=0.0001 → 80 pips >= 7 → True
    pos = make_position(ticket=1, price_open=1.09000, type=0)
    mock_mt5.symbol_info_tick.return_value = make_tick(bid=1.09800, ask=1.09802)
    mock_mt5.symbol_info.return_value = make_symbol_info(digits=5, point=0.00001)

    strategy = DefaultTPStrategy()
    assert strategy.should_trigger([pos], _pips_cfg(threshold=7.0), mock_mt5) is True


def test_trigger_pips_mode_below_threshold(mock_mt5) -> None:
    # move=0.0003 → 3 pips < 7 → False
    pos = make_position(ticket=1, price_open=1.09000, type=0)
    mock_mt5.symbol_info_tick.return_value = make_tick(bid=1.09003, ask=1.09005)
    mock_mt5.symbol_info.return_value = make_symbol_info(digits=5, point=0.00001)

    strategy = DefaultTPStrategy()
    assert strategy.should_trigger([pos], _pips_cfg(threshold=7.0), mock_mt5) is False


# ---------------------------------------------------------------------------
# execute — partial_close_percent variants
# ---------------------------------------------------------------------------


async def test_execute_pct_zero_sets_trailing(sqlite_db, mock_mt5) -> None:
    # pct=0 → set_trailing, no close
    pos = make_position(ticket=1001)
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
    await sqlite_db.mark_filled(1001, "2026-01-01T00:01:00+00:00")

    strategy = DefaultTPStrategy()
    result = await strategy.execute(1, [pos], _dollars_cfg(pct=0), mock_mt5, sqlite_db)

    assert 1001 in result.trailed_tickets
    assert result.closed_tickets == []
    mock_mt5.close_position.assert_not_called()

    rows = await sqlite_db.get_trailing_positions()
    assert any(r["mt5_ticket"] == 1001 for r in rows)


async def test_execute_pct_100_closes_all(sqlite_db, mock_mt5) -> None:
    # pct=100 → close newest fully, no trailing
    pos = make_position(ticket=1001)
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
    await sqlite_db.mark_filled(1001, "2026-01-01T00:01:00+00:00")
    mock_mt5.close_position.return_value = make_order_result(ticket=1001)

    strategy = DefaultTPStrategy()
    result = await strategy.execute(1, [pos], _dollars_cfg(pct=100), mock_mt5, sqlite_db)

    assert 1001 in result.closed_tickets
    assert result.trailed_tickets == []

    rows = await sqlite_db.get_all_active()
    assert all(r["status"] != "filled" for r in rows)


async def test_execute_pct_50_partial_close(sqlite_db, mock_mt5) -> None:
    # pct=50 → partial close (close vol = volume * 0.5)
    pos = make_position(ticket=1001, volume=0.2)
    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=1,
        mt5_ticket=1001,
        order_type="buy_limit",
        lot_size=0.2,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=1.08500,
        signal_type="standard",
    )
    await sqlite_db.mark_filled(1001, "2026-01-01T00:01:00+00:00")
    mock_mt5.close_position.return_value = make_order_result(ticket=1001)

    strategy = DefaultTPStrategy()
    result = await strategy.execute(1, [pos], _dollars_cfg(pct=50), mock_mt5, sqlite_db)

    assert 1001 in result.closed_tickets
    call_args = mock_mt5.close_position.call_args
    assert call_args.kwargs["volume"] == pytest.approx(0.1, abs=1e-6)  # 50% of 0.2


async def test_execute_closes_earlier_positions_first(sqlite_db, mock_mt5) -> None:
    # Two positions: ticket=1001 (older) and ticket=1002 (newest)
    # Earlier (1001) should be fully closed; newest (1002) is partial-closed or trailing
    pos1 = make_position(ticket=1001, volume=0.1)
    pos2 = make_position(ticket=1002, volume=0.1)
    for ticket, lid in [(1001, 1), (1002, 2)]:
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
        await sqlite_db.mark_filled(ticket, "2026-01-01T00:01:00+00:00")
    mock_mt5.close_position.return_value = make_order_result()

    strategy = DefaultTPStrategy()
    result = await strategy.execute(1, [pos1, pos2], _dollars_cfg(pct=50), mock_mt5, sqlite_db)

    # Earlier position (1001) closed; newest (1002) partial-closed
    assert 1001 in result.closed_tickets
    assert 1002 in result.closed_tickets
    assert mock_mt5.close_position.call_count == 2


# ---------------------------------------------------------------------------
# TPEngine.run_cycle — trigger-row enrichment (r_multiple, MFE/MAE, level)
# ---------------------------------------------------------------------------


async def test_failing_close_writes_one_trigger_row(sqlite_db, mock_mt5, sample_config) -> None:
    # A close that keeps failing re-qualifies the signal every cycle; the
    # trigger_recorded guard must still yield exactly one outcome row.
    pos = make_position(
        ticket=1001, symbol="XAUUSD", price_open=4459.0, volume=0.3, type=0, profit=11.4
    )
    mock_mt5.positions_get.return_value = [pos]
    mock_mt5.symbol_info_tick.return_value = make_tick(symbol="XAUUSD", bid=4465.0, ask=4465.2)
    mock_mt5.symbol_info.return_value = make_symbol_info(
        name="XAUUSD", digits=2, point=0.01, trade_tick_value=1.0, trade_tick_size=1.0
    )
    mock_mt5.account_info.return_value = make_account_info()
    mock_mt5.close_position.return_value = make_order_result(ticket=1001, retcode=10013)

    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=7,
        mt5_ticket=1001,
        order_type="buy_limit",
        lot_size=0.3,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=4440.0,
        signal_type="standard",
        mt5_price=4459.0,
        symbol="XAUUSD",
        sequence_number=3,
    )
    await sqlite_db.mark_filled(1001, "2026-01-01T00:01:00+00:00")

    writer = AsyncMock()
    engine = TPEngine(outcomes_writer=writer)
    await engine.run_cycle(mock_mt5, sqlite_db, sample_config)
    await engine.run_cycle(mock_mt5, sqlite_db, sample_config)
    await engine.run_cycle(mock_mt5, sqlite_db, sample_config)

    writer.record.assert_awaited_once()


async def test_run_cycle_records_trigger_outcome(sqlite_db, mock_mt5, sample_config) -> None:
    # XAUUSD long: entry 4459, bid 4465 → move 6 >= metals threshold 4.0 → triggers.
    pos = make_position(
        ticket=1001, symbol="XAUUSD", price_open=4459.0, volume=0.3, type=0, profit=11.4
    )
    mock_mt5.positions_get.return_value = [pos]
    mock_mt5.symbol_info_tick.return_value = make_tick(symbol="XAUUSD", bid=4465.0, ask=4465.2)
    mock_mt5.symbol_info.return_value = make_symbol_info(
        name="XAUUSD", digits=2, point=0.01, trade_tick_value=1.0, trade_tick_size=1.0
    )
    mock_mt5.account_info.return_value = make_account_info()
    mock_mt5.close_position.return_value = make_order_result(ticket=1001)

    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=7,
        mt5_ticket=1001,
        order_type="buy_limit",
        lot_size=0.3,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=4440.0,
        signal_type="standard",
        mt5_price=4459.0,
        symbol="XAUUSD",
        sequence_number=3,
    )
    await sqlite_db.mark_filled(1001, "2026-01-01T00:01:00+00:00")

    writer = AsyncMock()
    engine = TPEngine(outcomes_writer=writer)
    await engine.run_cycle(mock_mt5, sqlite_db, sample_config)

    writer.record.assert_awaited_once()
    outcome = writer.record.await_args.args[0]
    assert outcome.stage == "trigger"
    # risk_money = |4459-4440| * 0.3 = 5.7 → r = 11.4 / 5.7 = 2.0
    assert outcome.r_multiple == pytest.approx(2.0)
    assert outcome.mfe_price == pytest.approx(6.0)  # 4465 - 4459
    assert outcome.mfe_r == pytest.approx(6.0 / 19.0)
    assert outcome.level_sequence == 3
    assert outcome.seconds_to_trigger is not None

    # Excursion persisted to SQLite for the open position.
    rows = await sqlite_db.get_all_active()
    trailing_row = next(r for r in rows if r["mt5_ticket"] == 1001)
    assert trailing_row["mfe_price"] == pytest.approx(6.0)

    # The signal is durably marked TP-fired so its siblings never re-place.
    assert 7 in await sqlite_db.get_tp_fired_signals()


# ---------------------------------------------------------------------------
# follow_server_tp — the signal service's auto-TP call replaces the threshold
# ---------------------------------------------------------------------------


async def _insert_below_threshold_gold(sqlite_db, mock_mt5) -> None:
    # XAUUSD long: entry 4459, bid 4460 → move 1.0, well under the metals threshold 4.0.
    pos = make_position(
        ticket=1001, symbol="XAUUSD", price_open=4459.0, volume=0.3, type=0, profit=2.0
    )
    mock_mt5.positions_get.return_value = [pos]
    mock_mt5.symbol_info_tick.return_value = make_tick(symbol="XAUUSD", bid=4460.0, ask=4460.2)
    mock_mt5.symbol_info.return_value = make_symbol_info(
        name="XAUUSD", digits=2, point=0.01, volume_step=0.01, volume_min=0.01
    )
    mock_mt5.account_info.return_value = make_account_info()
    mock_mt5.close_position.return_value = make_order_result(ticket=1001)

    await sqlite_db.insert_order(
        limit_id=1,
        signal_id=7,
        mt5_ticket=1001,
        order_type="buy_limit",
        lot_size=0.3,
        placed_at="2026-01-01T00:00:00+00:00",
        db_stop_loss=4440.0,
        signal_type="standard",
        mt5_price=4459.0,
        symbol="XAUUSD",
        sequence_number=1,
    )
    await sqlite_db.mark_filled(1001, "2026-01-01T00:01:00+00:00")


async def test_follow_server_tp_fires_below_local_threshold(
    sqlite_db, mock_mt5, sample_config
) -> None:
    await _insert_below_threshold_gold(sqlite_db, mock_mt5)
    sample_config.tp_config.follow_server_tp = True

    engine = TPEngine()
    # Server hasn't called TP yet — the local threshold no longer applies either way.
    await engine.run_cycle(mock_mt5, sqlite_db, sample_config, server_tp_signals=set())
    mock_mt5.close_position.assert_not_called()

    await engine.run_cycle(mock_mt5, sqlite_db, sample_config, server_tp_signals={7})

    # Partial close (50% of 0.3) then trail the remainder — same execution as a
    # threshold trigger, and the signal is marked so its siblings never re-place.
    mock_mt5.close_position.assert_called_once()
    assert mock_mt5.close_position.call_args.kwargs["volume"] == pytest.approx(0.15)
    assert [r["mt5_ticket"] for r in await sqlite_db.get_trailing_positions()] == [1001]
    assert 7 in await sqlite_db.get_tp_fired_signals()


async def test_server_tp_ignored_when_follow_disabled(sqlite_db, mock_mt5, sample_config) -> None:
    await _insert_below_threshold_gold(sqlite_db, mock_mt5)

    engine = TPEngine()
    await engine.run_cycle(mock_mt5, sqlite_db, sample_config, server_tp_signals={7})

    mock_mt5.close_position.assert_not_called()


async def test_follow_server_tp_tags_outcome_strategy(sqlite_db, mock_mt5, sample_config) -> None:
    await _insert_below_threshold_gold(sqlite_db, mock_mt5)
    sample_config.tp_config.follow_server_tp = True

    writer = AsyncMock()
    engine = TPEngine(outcomes_writer=writer)
    await engine.run_cycle(mock_mt5, sqlite_db, sample_config, server_tp_signals={7})

    writer.record.assert_awaited_once()
    assert writer.record.await_args.args[0].tp_strategy == "follow_server"
