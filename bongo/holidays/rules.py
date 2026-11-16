"""Gregorian date rules, written as short specs (your own Calendar dates use them too):

    "MM-DD" | "easter[+N|-N]" | "orthodox_easter[+N|-N]" | "<1st|2nd|3rd|4th|5th|last>-<mon..sun>-MM"
    | "MM-DD!sub"   ("!sub": the UK bank-holiday move off a weekend)
"""
from __future__ import annotations

import calendar
import datetime as dt
import re

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ORDINALS = {"1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5, "last": -1}


def easter(year: int) -> dt.date:
    """Western Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (a + 11 * h + 22 * ((32 + 2 * e + 2 * i - h - k) % 7)) // 451
    month, day = divmod(h + (32 + 2 * e + 2 * i - h - k) % 7 - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def orthodox_easter(year: int) -> dt.date:
    """Eastern Orthodox Easter: the Julian computus, given as a Gregorian date."""
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month, day = divmod(d + e + 114, 31)
    julian = dt.date(year, month, day + 1)
    return julian + dt.timedelta(days=year // 100 - year // 400 - 2)  # Julian -> Gregorian (13 days now)


def _nth_weekday(year, n, wd, month):
    if n > 0:
        first = dt.date(year, month, 1)
        day = first + dt.timedelta(days=(wd - first.weekday()) % 7 + 7 * (n - 1))
    else:
        last = dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)
        day = last - dt.timedelta(days=(last.weekday() - wd) % 7)
    if day.month != month:
        raise ValueError(f"no such weekday in month: {n}-{DAYS[wd]}-{month:02d} {year}")
    return day


def _substitute(day):
    """UK bank-holiday rule: Sat/Sun moves to Monday; Christmas/Boxing Day on a Sunday move to Tuesday."""
    if day.weekday() < 5:
        return day
    if day.weekday() == 6 and (day.month, day.day) in ((12, 25), (12, 26)):
        return day + dt.timedelta(days=2)
    return day + dt.timedelta(days=7 - day.weekday())


def date_in_year(spec: str, year: int) -> dt.date:
    sub = spec.endswith("!sub")
    s = spec[:-4] if sub else spec
    try:
        if m := re.fullmatch(r"(orthodox_easter|easter)(?:([+-])(\d+))?", s):
            base = easter(year) if m[1] == "easter" else orthodox_easter(year)
            day = base + dt.timedelta(days=int(m[2] + m[3]) if m[2] else 0)
        elif m := re.fullmatch(r"(1st|2nd|3rd|4th|5th|last)-(%s)-(\d\d)" % "|".join(DAYS), s):
            day = _nth_weekday(year, ORDINALS[m[1]], DAYS.index(m[2]), int(m[3]))
        elif m := re.fullmatch(r"(\d\d)-(\d\d)", s):
            month, mday = int(m[1]), int(m[2])
            if (month, mday) == (2, 29) and not calendar.isleap(year):
                mday = 28                     # a leap-day birthday is kept on 28 Feb in other years
            day = dt.date(year, month, mday)
        else:
            raise ValueError
    except ValueError as e:
        raise ValueError(f"bad holiday spec: {spec!r}") from e
    return _substitute(day) if sub else day
