async def test_history_query_groups_by_signal(sqlite_db) -> None:
    # Signal 1: two fills, one winning ($+5.00), one losing ($-2.00) → net +3 → 1 win
    await sqlite_db.insert_order(
        limit_id=11,
        signal_id=1,
        mt5_ticket=10001,
        order_type="buy_limit",
        lot_size=0.10,
        placed_at="2026-06-01T10:00:00+00:00",
        db_stop_loss=1.0,
        signal_type="standard",
        symbol="EURUSD",
    )
    await sqlite_db.insert_order(
        limit_id=12,
        signal_id=1,
        mt5_ticket=10002,
        order_type="buy_limit",
        lot_size=0.10,
        placed_at="2026-06-01T10:00:00+00:00",
        db_stop_loss=1.0,
        signal_type="standard",
        symbol="EURUSD",
    )
    await sqlite_db.mark_filled(10001, "2026-06-01T11:00:00+00:00")
    await sqlite_db.mark_filled(10002, "2026-06-01T11:00:00+00:00")
    await sqlite_db.mark_closed(10001, 5.0)
    await sqlite_db.mark_closed(10002, -2.0)

    # Signal 2: single fill, losing → 1 loss
    await sqlite_db.insert_order(
        limit_id=21,
        signal_id=2,
        mt5_ticket=20001,
        order_type="sell_limit",
        lot_size=0.05,
        placed_at="2026-06-02T10:00:00+00:00",
        db_stop_loss=2.0,
        signal_type="scalp",
        symbol="GBPUSD",
    )
    await sqlite_db.mark_filled(20001, "2026-06-02T11:00:00+00:00")
    await sqlite_db.mark_closed(20001, -4.0)

    # Signal 3: pure cancellation, no fill → not a win or loss
    await sqlite_db.insert_order(
        limit_id=31,
        signal_id=3,
        mt5_ticket=30001,
        order_type="buy_limit",
        lot_size=0.10,
        placed_at="2026-06-03T10:00:00+00:00",
        db_stop_loss=1.0,
        signal_type="standard",
        symbol="USDCAD",
    )
    await sqlite_db.mark_cancelled(30001, "2026-06-03T11:00:00+00:00")

    rows = await sqlite_db.get_order_history("2020-01-01T00:00:00", "2099-12-31T23:59:59")

    by_signal = {r["signal_id"]: r for r in rows}
    assert set(by_signal) == {1, 2, 3}
    # Signal 1 net: +5 - 2 = +3.0
    assert abs(by_signal[1]["total_pnl"] - 3.0) < 1e-9
    assert by_signal[1]["closed_count"] == 2
    # Signal 2 net: -4.0
    assert abs(by_signal[2]["total_pnl"] - (-4.0)) < 1e-9
    assert by_signal[2]["closed_count"] == 1
    # Signal 3: pure cancel
    assert by_signal[3]["closed_count"] == 0
    assert by_signal[3]["cancelled_count"] == 1
    assert all(r["breakeven_count"] == 0 for r in rows)


async def test_breakeven_close_is_flagged_and_excluded_from_win_loss(sqlite_db) -> None:
    # Signal 1: a TM breakeven flatten that landed slightly negative on spread.
    # Signal 2: an ordinary loss of the same size.
    for limit_id, signal_id, ticket, reason in ((11, 1, 10001, "breakeven"), (21, 2, 20001, None)):
        await sqlite_db.insert_order(
            limit_id=limit_id,
            signal_id=signal_id,
            mt5_ticket=ticket,
            order_type="buy_limit",
            lot_size=0.10,
            placed_at="2026-06-01T10:00:00+00:00",
            db_stop_loss=1.0,
            signal_type="standard",
            symbol="EURUSD",
        )
        await sqlite_db.mark_filled(ticket, "2026-06-01T11:00:00+00:00")
        await sqlite_db.mark_closed(ticket, -1.5, reason)

    rows = await sqlite_db.get_order_history("2020-01-01T00:00:00", "2099-12-31T23:59:59")
    by_signal = {r["signal_id"]: r for r in rows}
    assert by_signal[1]["breakeven_count"] == 1
    assert by_signal[2]["breakeven_count"] == 0

    # The breakeven's P&L still counts toward the balance, but not toward the record.
    stats = await sqlite_db.get_user_stats()
    assert stats["total_trades"] == 2
    assert stats["wins"] == 0
    assert stats["losses"] == 1
    assert abs(stats["total_pnl"] - (-3.0)) < 1e-9
