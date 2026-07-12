"""US equity market calendar: full holidays and 1:00pm early closes.

Dates come from the NYSE Group's official holiday announcements (press
releases covering 2025-2028, ir.theice.com) and are hardcoded through 2027.
Deterministic weekday rules (Labor Day = first Monday of September, etc.)
fill in the dates the announcements list by name.

Design rule: when the table runs out, DEGRADE LOUDLY. Past
`CALENDAR_THROUGH`, `is_trading_day` falls back to weekday-only (the old
behavior) and prints a one-time warning — a bot that silently treats
Thanksgiving as a trading day just wastes API calls, but one that silently
treats a half-day as a full day holds positions two hours past the close.
Extend the tables from the next NYSE announcement once a year.
"""

from __future__ import annotations

import sys
from datetime import date
from datetime import time as time_of_day

REGULAR_OPEN = time_of_day(9, 30)
REGULAR_CLOSE = time_of_day(16, 0)
EARLY_CLOSE = time_of_day(13, 0)

# Full-day closures (observed dates).
HOLIDAYS: frozenset[date] = frozenset({
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # Martin Luther King Jr. Day
    date(2026, 2, 16),   # Washington's Birthday
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (July 4 is a Saturday; observed Friday)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
    # 2027
    date(2027, 1, 1),    # New Year's Day
    date(2027, 1, 18),   # Martin Luther King Jr. Day
    date(2027, 2, 15),   # Washington's Birthday
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),   # Memorial Day
    date(2027, 6, 18),   # Juneteenth (June 19 is a Saturday; observed Friday)
    date(2027, 7, 5),    # Independence Day (July 4 is a Sunday; observed Monday)
    date(2027, 9, 6),    # Labor Day
    date(2027, 11, 25),  # Thanksgiving
    date(2027, 12, 24),  # Christmas (Dec 25 is a Saturday; observed Friday)
})

# 1:00pm ET closes.
EARLY_CLOSES: frozenset[date] = frozenset({
    date(2026, 11, 27),  # day after Thanksgiving
    date(2026, 12, 24),  # Christmas Eve
    date(2027, 11, 26),  # day after Thanksgiving
    # No Christmas Eve early close in 2027: Dec 24 is the observed holiday.
})

CALENDAR_THROUGH = date(2027, 12, 31)

_warned_past_coverage = False


def _check_coverage(d: date) -> None:
    global _warned_past_coverage
    if d > CALENDAR_THROUGH and not _warned_past_coverage:
        _warned_past_coverage = True
        print(
            f"[market_calendar] WARNING: {d} is past the hardcoded calendar "
            f"(through {CALENDAR_THROUGH}). Falling back to weekday-only logic — "
            f"holidays and early closes are NOT known. Update "
            f"code_base/market_calendar.py from the next NYSE announcement.",
            file=sys.stderr, flush=True,
        )


def is_trading_day(d: date) -> bool:
    """Weekday and not a full-day holiday."""
    _check_coverage(d)
    return d.weekday() < 5 and d not in HOLIDAYS


def market_close_time(d: date) -> time_of_day:
    """Today's closing bell: 16:00, or 13:00 on early-close days."""
    _check_coverage(d)
    return EARLY_CLOSE if d in EARLY_CLOSES else REGULAR_CLOSE


def _smoke_test() -> int:
    cases = [
        (date(2026, 7, 10), True, REGULAR_CLOSE),    # ordinary Friday
        (date(2026, 7, 11), False, REGULAR_CLOSE),   # Saturday
        (date(2026, 7, 3), False, REGULAR_CLOSE),    # observed July 4th
        (date(2026, 11, 26), False, REGULAR_CLOSE),  # Thanksgiving
        (date(2026, 11, 27), True, EARLY_CLOSE),     # half-day
        (date(2026, 12, 24), True, EARLY_CLOSE),     # Christmas Eve half-day
        (date(2027, 12, 24), False, REGULAR_CLOSE),  # observed Christmas 2027
        (date(2027, 11, 26), True, EARLY_CLOSE),     # 2027 half-day
    ]
    failures = 0
    for d, want_open, want_close in cases:
        got_open, got_close = is_trading_day(d), market_close_time(d)
        ok = got_open == want_open and got_close == want_close
        print(f"{'PASS' if ok else 'FAIL'} {d} trading={got_open} close={got_close}")
        failures += 0 if ok else 1
    print(f"\n{'All cases passed.' if not failures else f'{failures} failure(s)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_smoke_test())
