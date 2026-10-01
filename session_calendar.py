#!/usr/bin/env python3
"""Nugget's session calendar: when is spot gold actually tradeable?

Why this file exists (challenger round 2, item 1): the PAXGUSDT proxy feed
trades 24/7. A naive hourly cron therefore gets a real-looking price while the
gold market is CLOSED, and would feed those bars into the promotion gate. This
module is the single authority on whether a given instant is tradeable.

Spot gold (XAUUSD) trading week, anchored to US Eastern wall clock:
    open  : Sunday   17:00 ET
    close : Friday   17:00 ET
    break : daily    17:00-18:00 ET   (the CME/spot maintenance window)

Because it is anchored to ET, the UTC and WIB anchors SHIFT with US DST:
    17:00 ET = 21:00 UTC (EDT, ~Mar-Nov)  ->  04:00 WIB next day
    17:00 ET = 22:00 UTC (EST, ~Nov-Mar)  ->  05:00 WIB next day
WIB has no DST, so those local times jump twice a year with no code change.
We therefore never hardcode UTC hours - we always convert through ET.

Broker feeds vary the exact break minute and holiday observances. Treat the
daily break as nominal; the holiday table below is for FULL closures and
early closes of the main venues.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
ET = ZoneInfo("America/New_York")   # authoritative DST handling
WIB = ZoneInfo("Asia/Jakarta")

OPEN_HOUR_ET = 17      # Sunday open / Friday close / daily break start
BREAK_HOURS = 1        # daily maintenance window length, in hours

# Full closures (main venues). Observed-date approximations; a broker may differ
# by a day around the weekend. Keys are (month, day) for fixed feasts plus an
# explicit set of computed dates refreshed per year by `_holidays_for`.
FIXED_HOLIDAYS = {
    (1, 1),    # New Year's Day
    (7, 4),    # Independence Day
    (12, 25),  # Christmas Day
    (12, 24),  # Christmas Eve (venue closure in recent years)
}

# Early closes: 13:00 ET instead of 17:00 ET (dictated by convention; broker feeds vary)
EARLY_CLOSE_MD = {
    (7, 3),    # Independence Day eve
    (11, 28),  # day after Thanksgiving (varies)
    (12, 24),  # Christmas Eve
    (12, 31),  # New Year's Eve
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th `weekday` (Mon=0) of `month`."""
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))
    return d


def _holidays_for(year: int) -> set[date]:
    """Computed movable feasts for a year, merged with the fixed ones."""
    out = set()
    for md in FIXED_HOLIDAYS:
        out.add(date(year, md[0], md[1]))
    # Good Friday (2 days before Easter Sunday) - gold venues are closed
    out.add(_easter(year) - timedelta(days=2))
    # Juneteenth now observed by US venues
    out.add(date(year, 6, 19))
    # Market holidays observed on the nearest weekday
    for md in ((1, 1), (6, 19), (7, 4), (12, 25)):
        d = date(year, md[0], md[1])
        if d.weekday() == 5:            # Sat -> previous Fri
            d -= timedelta(days=1)
        elif d.weekday() == 6:          # Sun -> following Mon
            d += timedelta(days=1)
        out.add(d)
    return out


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


_HOLIDAY_CACHE: dict[int, set[date]] = {}


def holidays(year: int) -> set[date]:
    if year not in _HOLIDAY_CACHE:
        _HOLIDAY_CACHE[year] = _holidays_for(year)
    return _HOLIDAY_CACHE[year]


def session_state(dt: datetime) -> str:
    """Classify an instant. Returns one of:

        open            - tradeable
        closed_weekend  - Sat, or Sun before open, or Fri after close
        closed_break    - the daily 17:00-18:00 ET maintenance window
        closed_holiday  - full venue closure
        closed_early    - after an early close (13:00 ET)
    """
    if dt.tzinfo is None:
        raise ValueError("session_state requires a timezone-aware datetime")
    et = dt.astimezone(ET)

    # holiday closure runs on the ET calendar day
    if et.date() in holidays(et.year):
        return "closed_holiday"

    wd = et.weekday()          # Mon=0 .. Sun=6
    h = et.hour + et.minute / 60.0

    if wd == 5:                # Saturday: closed all day
        return "closed_weekend"
    if wd == 6:                # Sunday: closed until 17:00 ET
        return "open" if h >= OPEN_HOUR_ET else "closed_weekend"
    if wd == 4:                # Friday: closes at 17:00 ET
        if h >= OPEN_HOUR_ET:
            return "closed_weekend"
        if (et.month, et.day) in EARLY_CLOSE_MD and h >= 13:
            return "closed_early"
        return "open" if h >= 1 else "closed_break"

    # Mon-Thu
    if et.month == 12 and et.day == 31:      # New Year's Eve early close
        return "closed_early" if h >= 13 else "open"
    if OPEN_HOUR_ET <= h < OPEN_HOUR_ET + BREAK_HOURS:
        return "closed_break"
    return "open"


def is_open(dt: datetime) -> bool:
    """True only when the spot gold market is actually tradeable."""
    return session_state(dt) == "open"


def open_instant(d: date) -> datetime:
    """The UTC instant at which `d` opens (17:00 ET on that date)."""
    return datetime.combine(d, time(OPEN_HOUR_ET, 0), tzinfo=ET).astimezone(UTC)


def expected_hourly_slots(start_utc: datetime, end_utc: datetime) -> list[datetime]:
    """Every top-of-hour slot in [start, end) that falls inside the session.

    This is THE reference the gap watchdog diffs the ledger against: a slot that
    was expected but has no row is a real gap, not a quiet success.
    """
    out = []
    cur = start_utc.replace(minute=0, second=0, microsecond=0)
    if cur < start_utc:
        cur += timedelta(hours=1)
    while cur < end_utc:
        if is_open(cur):
            out.append(cur)
        cur += timedelta(hours=1)
    return out


def describe(dt: datetime) -> str:
    """Human-readable line, UTC + WIB, for logs and reports."""
    st = session_state(dt)
    return (f"{dt.astimezone(UTC):%Y-%m-%d %H:%M} UTC / "
            f"{dt.astimezone(WIB):%Y-%m-%d %H:%M} WIB  ->  {st}")


if __name__ == "__main__":
    print("=== spot-gold session calendar self-test ===\n")

    # 1. The exact anchors, in ET and their UTC equivalents across DST.
    print("anchor check (open hour in UTC, summer vs winter):")
    for label, d in (("summer (EDT)", date(2026, 7, 19)),
                     ("winter (EST)", date(2026, 1, 18))):
        o = open_instant(d)
        print(f"  {label:<14} {d} 17:00 ET -> {o:%Y-%m-%d %H:%M} UTC "
              f"/ {o.astimezone(WIB):%Y-%m-%d %H:%M} WIB")

    # 2. weekday / weekend / break classification
    print("\nclassification checks:")
    cases = [
        ("Mon midday open",        datetime(2026, 9, 30, 12, 0, tzinfo=UTC)),
        ("daily break (21 UTC)",   datetime(2026, 9, 30, 21, 30, tzinfo=UTC)),
        ("Fri after close",        datetime(2026, 10, 2, 22, 0, tzinfo=UTC)),
        ("Saturday",               datetime(2026, 10, 3, 12, 0, tzinfo=UTC)),
        ("Sunday before open",     datetime(2026, 10, 4, 12, 0, tzinfo=UTC)),
        ("Sunday after open",      datetime(2026, 10, 4, 22, 0, tzinfo=UTC)),
        ("Christmas Day",          datetime(2026, 12, 25, 12, 0, tzinfo=UTC)),
    ]
    for label, dt in cases:
        print(f"  {label:<22} {describe(dt)}")

    # 3. weekend share, to compare against the measured contamination
    print("\nweekend/closed share over a real week (hourly):")
    start = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    slots = expected_hourly_slots(start, start + timedelta(days=7))
    total = 7 * 24
    print(f"  {len(slots)}/{total} hourly slots open "
          f"= {len(slots)/total*100:.1f}% (implies ~{(1-len(slots)/total)*100:.1f}% closed)")

    # 4. DST boundary: the WIB hour must jump
    print("\nDST boundary (open hour in WIB across the Nov switch):")
    for d in (date(2026, 10, 25), date(2026, 11, 8)):
        o = open_instant(d).astimezone(WIB)
        print(f"  Sun {d} open -> {o:%Y-%m-%d %H:%M} WIB")

    print("\nALL CALENDAR CHECKS DONE")
