"""Date helpers: NJ business days (attorney review), MOD-IV date parsing."""
from __future__ import annotations

from datetime import date, datetime, timedelta


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def nj_state_holidays(year: int) -> set[date]:
    """New Jersey state holidays (N.J.S.A. 36:1-1.1), weekend dates moved to observed weekday.

    Election Day is the Tuesday after the first Monday in November.
    """
    first_monday_nov = _nth_weekday(year, 11, 0, 1)
    days = {
        _observed(date(year, 1, 1)),                    # New Year's Day
        _nth_weekday(year, 1, 0, 3),                    # MLK Day
        _observed(date(year, 2, 12)),                   # Lincoln's Birthday
        _nth_weekday(year, 2, 0, 3),                    # Presidents' Day
        _easter(year) - timedelta(days=2),              # Good Friday
        _last_weekday(year, 5, 0),                      # Memorial Day
        _observed(date(year, 6, 19)),                   # Juneteenth
        _observed(date(year, 7, 4)),                    # Independence Day
        _nth_weekday(year, 9, 0, 1),                    # Labor Day
        _nth_weekday(year, 10, 0, 2),                   # Columbus Day
        first_monday_nov + timedelta(days=1),           # Election Day
        _observed(date(year, 11, 11)),                  # Veterans Day
        _nth_weekday(year, 11, 3, 4),                   # Thanksgiving
        _observed(date(year, 12, 25)),                  # Christmas
    }
    return days


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in nj_state_holidays(d.year)


def nj_business_days_after(start: date, days: int = 3) -> date:
    """Last day of an N-business-day period that begins the day after ``start``.

    Attorney review: the 3 business days run from delivery of the fully executed contract;
    the period ends at the end of the returned date.
    """
    d, counted = start, 0
    while counted < days:
        d += timedelta(days=1)
        if is_business_day(d):
            counted += 1
    return d


def parse_modiv_date(value: str | None) -> date | None:
    """MOD-IV DEED_DATE is YYMMDD. Two-digit years: 00-(current+1) -> 2000s, else 1900s."""
    if not value:
        return None
    s = str(value).strip()
    if len(s) != 6 or not s.isdigit():
        return None
    yy, mm, dd = int(s[:2]), int(s[2:4]), int(s[4:])
    century = 2000 if yy <= (date.today().year % 100) + 1 else 1900
    try:
        return date(century + yy, mm, dd)
    except ValueError:
        return None


def parse_us_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%B %d, %Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def years_between(earlier: date | None, later: date | None = None) -> float | None:
    if earlier is None:
        return None
    later = later or date.today()
    return round((later - earlier).days / 365.25, 2)
