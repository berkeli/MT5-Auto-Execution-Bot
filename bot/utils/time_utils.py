from datetime import UTC, date, datetime, time, timedelta

import pytz

from bot.config.settings import SpreadHourConfig

# Risky positions must be *flat before* a disabled window opens, not closed at its edge:
# the window exists to dodge a scheduled event, and closing on the boundary races it. So
# the gate opens a minute early — placement blocked, pendings pulled, filled positions
# swept — while the close edge stays exact, so risky re-arms exactly when configured.
_RISKY_EXIT_LEAD = timedelta(minutes=1)

_DAY_MAP = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def to_est(dt: datetime) -> datetime:
    est = pytz.timezone("US/Eastern")
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(est)


def _parse_time(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _risky_window(s: str) -> tuple[time, time]:
    """Parse "HH:MM-HH:MM", opening _RISKY_EXIT_LEAD early (see the constant)."""
    start, end = s.split("-")
    return _shift(_parse_time(start), _RISKY_EXIT_LEAD), _parse_time(end)


def _shift(t: time, delta: timedelta) -> time:
    """Move a wall-clock time back by delta, wrapping past midnight."""
    return (datetime.combine(date(2000, 1, 1), t) - delta).time()


class MarketScheduler:
    def __init__(self, config: SpreadHourConfig, risky_windows: list[str] | None = None) -> None:
        self._tz = pytz.timezone(config.timezone)
        self._start = _parse_time(config.daily_start)
        self._stock_start = _parse_time(config.stock_daily_start)
        self._strip_start = _parse_time(config.sl_strip_start)
        self._stock_strip_start = _parse_time(config.sl_strip_stock_start)
        self._end = _parse_time(config.daily_end)
        self._weekend_start = _DAY_MAP[config.weekend_start_day.lower()]
        self._weekend_end = _DAY_MAP[config.weekend_end_day.lower()]
        # UTC windows during which 'risky' signals are disabled entirely.
        self._risky_windows = [_risky_window(w) for w in (risky_windows or [])]

    def _in_session(self, start: time, now: datetime | None) -> bool:
        now_local = (now or datetime.now(UTC)).astimezone(self._tz)
        t = now_local.time()
        day = now_local.weekday()  # 0=Mon ... 4=Fri, 5=Sat, 6=Sun

        # Weekend closure: Fri start through Sun 18:00 (continuous)
        if day == self._weekend_start and t >= start:
            return True
        if self._weekend_start < day < self._weekend_end:  # all day Saturday
            return True
        if day == self._weekend_end and t < self._end:
            return True

        # Daily window Mon–Thu
        if day < self._weekend_start and start <= t < self._end:
            return True

        return False

    def is_spread_hour(self, now: datetime | None = None, stock: bool = False) -> bool:
        return self._in_session(self._stock_start if stock else self._start, now)

    def is_sl_strip_window(self, now: datetime | None = None, stock: bool = False) -> bool:
        """Window in which filled positions' stop-losses are stripped (spread-spike
        protection). Same shape as is_spread_hour but opens ~5 min before the spike."""
        return self._in_session(self._stock_strip_start if stock else self._strip_start, now)

    def should_cancel_pending(self, now: datetime | None = None, stock: bool = False) -> bool:
        """Pendings survive late market (daily_start..sl_strip_start) — prices are still
        tradable there, so a ladder that was already working keeps working and may fill.
        They're pulled only at the spike, where a fill would be at a blown spread."""
        return self.is_sl_strip_window(now, stock)

    def should_block_placement(self, now: datetime | None = None, stock: bool = False) -> bool:
        """Late market is exactly the "no new exposure" window, so this one opens at
        daily_start — the earlier of the two boundaries."""
        return self.is_spread_hour(now, stock)

    def is_risky_disabled(self, now: datetime | None = None) -> bool:
        """True inside any configured UTC window where 'risky' signals are disabled.
        Windows are compared in UTC; one that ends at/before it starts is treated as
        wrapping past midnight."""
        if not self._risky_windows:
            return False
        t = (now or datetime.now(UTC)).astimezone(pytz.utc).time()
        for start, end in self._risky_windows:
            if start <= end:
                if start <= t < end:
                    return True
            elif t >= start or t < end:
                return True
        return False

    def is_weekend_window(self, now: datetime | None = None) -> bool:
        """Forex weekend window: Friday >=15:55 EST through Sunday <18:00 EST.

        Independent of is_spread_hour because the spread-hour daily window
        (e.g. Mon-Thu 15:55-18:00) is not a 'weekend' for signal-expiry purposes.
        """
        now_local = (now or datetime.now(UTC)).astimezone(self._tz)
        t = now_local.time()
        day = now_local.weekday()
        fri_cutoff = time(15, 55)
        sun_open = time(18, 0)
        if day == 4 and t >= fri_cutoff:
            return True
        if day == 5:
            return True
        if day == 6 and t < sun_open:
            return True
        return False
