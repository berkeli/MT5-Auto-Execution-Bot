from datetime import datetime

import pytz

from bot.config.settings import SpreadHourConfig
from bot.utils.time_utils import MarketScheduler


def _est(year, month, day, hour, minute) -> datetime:
    tz = pytz.timezone("US/Eastern")
    return tz.localize(datetime(year, month, day, hour, minute))


def _utc(hour, minute) -> datetime:
    return pytz.utc.localize(datetime(2026, 7, 6, hour, minute))


def _scheduler() -> MarketScheduler:
    return MarketScheduler(SpreadHourConfig())


def _risky_scheduler() -> MarketScheduler:
    return MarketScheduler(SpreadHourConfig(), ["21:55-23:10", "00:55-02:00", "11:55-14:00"])


def test_is_weekend_window_friday_before_cutoff() -> None:
    # Friday Mar 6, 2026 at 15:54 EST — just before the cutoff
    assert _scheduler().is_weekend_window(_est(2026, 3, 6, 15, 54)) is False


def test_is_weekend_window_friday_at_cutoff() -> None:
    assert _scheduler().is_weekend_window(_est(2026, 3, 6, 15, 55)) is True


def test_is_weekend_window_friday_evening() -> None:
    assert _scheduler().is_weekend_window(_est(2026, 3, 6, 22, 0)) is True


def test_is_weekend_window_saturday_noon() -> None:
    assert _scheduler().is_weekend_window(_est(2026, 3, 7, 12, 0)) is True


def test_is_weekend_window_sunday_before_open() -> None:
    assert _scheduler().is_weekend_window(_est(2026, 3, 8, 17, 59)) is True


def test_is_weekend_window_sunday_at_open() -> None:
    assert _scheduler().is_weekend_window(_est(2026, 3, 8, 18, 0)) is False


def test_is_weekend_window_monday_morning() -> None:
    assert _scheduler().is_weekend_window(_est(2026, 3, 9, 9, 0)) is False


def test_is_risky_disabled_inside_each_window() -> None:
    sch = _risky_scheduler()
    assert sch.is_risky_disabled(_utc(22, 0)) is True  # 21:55-23:10
    assert sch.is_risky_disabled(_utc(1, 0)) is True  # 00:55-02:00
    assert sch.is_risky_disabled(_utc(12, 0)) is True  # 11:55-14:00


def test_is_risky_disabled_boundaries() -> None:
    # The gate opens one minute early (_RISKY_EXIT_LEAD) so the force-close sweep lands
    # before the window rather than on its edge; the close edge stays exact.
    sch = _risky_scheduler()
    assert sch.is_risky_disabled(_utc(21, 53)) is False  # outside the lead
    assert sch.is_risky_disabled(_utc(21, 54)) is True  # lead — must be flat by 21:55
    assert sch.is_risky_disabled(_utc(21, 55)) is True  # configured start
    assert sch.is_risky_disabled(_utc(23, 9)) is True  # no lead at the close edge
    assert sch.is_risky_disabled(_utc(23, 10)) is False  # end exclusive


def test_risky_lead_wraps_past_midnight() -> None:
    sch = MarketScheduler(SpreadHourConfig(), risky_windows=["00:00-01:00"])
    assert sch.is_risky_disabled(_utc(23, 59)) is True  # lead crosses into the day before
    assert sch.is_risky_disabled(_utc(23, 58)) is False
    assert sch.is_risky_disabled(_utc(0, 30)) is True
    assert sch.is_risky_disabled(_utc(1, 0)) is False


def test_is_risky_disabled_outside_all_windows() -> None:
    sch = _risky_scheduler()
    assert sch.is_risky_disabled(_utc(15, 0)) is False
    assert sch.is_risky_disabled(_utc(3, 0)) is False


def test_is_risky_disabled_no_windows_configured() -> None:
    # A scheduler built without risky windows never disables.
    assert _scheduler().is_risky_disabled(_utc(22, 0)) is False


def test_is_weekend_window_thursday() -> None:
    # Thursday is never a weekend window even at 17:00 (which IS spread-hour)
    assert _scheduler().is_weekend_window(_est(2026, 3, 5, 17, 0)) is False


# ---------------------------------------------------------------------------
# Per-asset spread-hour cutoff: stocks cancel earlier (15:45) than the default (15:55)
# ---------------------------------------------------------------------------


def test_stock_spread_hour_starts_at_stock_cutoff() -> None:
    # Monday 15:40 EST — stocks are in their window; everything else isn't yet
    s = _scheduler()
    assert s.is_spread_hour(_est(2026, 3, 9, 15, 40), stock=True) is True
    assert s.is_spread_hour(_est(2026, 3, 9, 15, 40), stock=False) is False


def test_stock_spread_hour_before_cutoff() -> None:
    assert _scheduler().is_spread_hour(_est(2026, 3, 9, 15, 39), stock=True) is False


def test_default_spread_hour_at_default_cutoff() -> None:
    # 15:55 EST — default forex window opens; stocks have been in theirs since 15:40
    s = _scheduler()
    assert s.is_spread_hour(_est(2026, 3, 9, 15, 54), stock=False) is False
    assert s.is_spread_hour(_est(2026, 3, 9, 15, 55), stock=False) is True
    assert s.is_spread_hour(_est(2026, 3, 9, 15, 55), stock=True) is True


def test_stock_friday_weekend_starts_at_stock_cutoff() -> None:
    # Friday 15:40 EST — the stock weekend closure opens early; default waits for 15:55
    s = _scheduler()
    assert s.is_spread_hour(_est(2026, 3, 6, 15, 40), stock=True) is True
    assert s.is_spread_hour(_est(2026, 3, 6, 15, 40), stock=False) is False


# ---------------------------------------------------------------------------
# SL strip window: opens ~5 min before the spread spike, closes at daily_end
# ---------------------------------------------------------------------------


def test_pendings_survive_late_market_and_are_pulled_at_the_spike() -> None:
    # Placement stops at daily_start; teardown waits for the spike, so a ladder already
    # working keeps working (and may legitimately fill) through late market.
    s = _scheduler()
    late, spike = _est(2026, 3, 9, 16, 0), _est(2026, 3, 9, 17, 0)
    assert s.should_block_placement(late) is True
    assert s.should_cancel_pending(late) is False
    assert s.should_block_placement(spike) is True
    assert s.should_cancel_pending(spike) is True


def test_stock_pending_teardown_keeps_its_own_cutoff() -> None:
    # stock_daily_start and sl_strip_stock_start are both 15:40 — stocks shut at 16:00
    # and the broker rejects changes past it, so the split buys them no extra time.
    s = _scheduler()
    assert s.should_cancel_pending(_est(2026, 3, 9, 15, 39), stock=True) is False
    assert s.should_cancel_pending(_est(2026, 3, 9, 15, 40), stock=True) is True


def test_friday_pendings_are_pulled_before_the_weekend() -> None:
    s = _scheduler()
    assert s.should_cancel_pending(_est(2026, 3, 6, 16, 0)) is False  # Friday late market
    assert s.should_cancel_pending(_est(2026, 3, 6, 17, 0)) is True  # spike into the close
    assert s.should_cancel_pending(_est(2026, 3, 7, 12, 0)) is True  # Saturday


def test_sl_strip_window_forex_opens_at_1655() -> None:
    s = _scheduler()
    # 16:54 — not yet; 16:55 — stripped (forex)
    assert s.is_sl_strip_window(_est(2026, 3, 9, 16, 54), stock=False) is False
    assert s.is_sl_strip_window(_est(2026, 3, 9, 16, 55), stock=False) is True


def test_sl_strip_window_stock_opens_at_1540() -> None:
    s = _scheduler()
    # Stocks strip 20 min before their 16:00 close, while the session is still open so
    # the broker accepts the SL modification; forex isn't stripped yet
    assert s.is_sl_strip_window(_est(2026, 3, 9, 15, 39), stock=True) is False
    assert s.is_sl_strip_window(_est(2026, 3, 9, 15, 40), stock=True) is True
    assert s.is_sl_strip_window(_est(2026, 3, 9, 15, 40), stock=False) is False


def test_sl_strip_window_closes_at_daily_end() -> None:
    s = _scheduler()
    assert s.is_sl_strip_window(_est(2026, 3, 9, 17, 59), stock=False) is True
    assert s.is_sl_strip_window(_est(2026, 3, 9, 18, 0), stock=False) is False


def test_sl_strip_window_spans_weekend() -> None:
    s = _scheduler()
    # Friday after the strip open through Sunday before the 18:00 reopen
    assert s.is_sl_strip_window(_est(2026, 3, 6, 17, 0), stock=False) is True
    assert s.is_sl_strip_window(_est(2026, 3, 7, 12, 0), stock=False) is True
    assert s.is_sl_strip_window(_est(2026, 3, 8, 17, 59), stock=False) is True
    assert s.is_sl_strip_window(_est(2026, 3, 8, 18, 0), stock=False) is False
