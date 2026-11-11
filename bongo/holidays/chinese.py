"""The Chinese lunisolar calendar, worked out from the Sun and Moon for Beijing time (UTC+8):
months begin on the day of the new moon; month 11 holds the winter solstice; in a year of 13
months, the first month without a major solar term is the leap month."""
from __future__ import annotations

import datetime as dt
from functools import lru_cache

from . import sky

CST = dt.timezone(dt.timedelta(hours=8))


def _day(when: dt.datetime) -> dt.date:
    return when.astimezone(CST).date()


def _month_start_on_or_before(day: dt.date) -> dt.date:
    """The new-moon day starting the lunar month that contains ``day``."""
    t = dt.datetime.combine(day - dt.timedelta(days=31), dt.time(), tzinfo=CST).astimezone(sky.UTC)
    start = None
    while True:
        nm = sky.new_moon_after(t)
        if _day(nm) > day:
            return start
        start, t = _day(nm), nm + dt.timedelta(days=20)


def _solstice(year: int) -> dt.date:
    return _day(sky.sun_at(270.0, dt.datetime(year, 12, 1, tzinfo=sky.UTC)))


@lru_cache(maxsize=64)
def months(year: int) -> tuple[tuple[int, bool, dt.date], ...]:
    """(month number, is leap, first day) for the lunar months from month 11 of the previous
    Chinese year up to (not including) month 11 of this one."""
    a, b = _month_start_on_or_before(_solstice(year - 1)), _month_start_on_or_before(_solstice(year))
    starts, t = [a], dt.datetime.combine(a, dt.time(12), tzinfo=CST).astimezone(sky.UTC)
    while True:
        nm = sky.new_moon_after(t + dt.timedelta(days=1))
        if _day(nm) >= b:
            break
        starts.append(_day(nm))
        t = nm
    leap_year = len(starts) == 13
    out, number, leap_used = [], 11, False
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else b
        is_leap = False
        if leap_year and not leap_used and i > 0 and not _has_major_term(start, end):
            is_leap = leap_used = True
        if not is_leap and i > 0:
            number = number % 12 + 1
        out.append((number, is_leap, start))
    return tuple(out)


def _has_major_term(start: dt.date, end: dt.date) -> bool:
    """Does the Sun reach a multiple of 30 degrees (a zhongqi) during [start, end) in Beijing?"""
    t0 = dt.datetime.combine(start, dt.time(), tzinfo=CST).astimezone(sky.UTC)
    lon = sky.sun_longitude(t0)
    target = (int(lon // 30) + 1) * 30.0 % 360
    return _day(sky.sun_at(target, t0)) < end


def to_date(year: int, month: int, day: int) -> dt.date:
    """Gregorian date of ``day`` of (non-leap) lunar ``month`` in the Chinese year beginning in ``year``."""
    if month >= 11:
        table, want = months(year + 1), month
    else:
        table, want = months(year), month
    for number, is_leap, start in table:
        if number == want and not is_leap:
            return start + dt.timedelta(days=day - 1)
    raise ValueError(f"no month {month} in Chinese year {year}")


def qingming(year: int) -> dt.date:
    """Tomb-sweeping day: the Sun at 15 degrees, early April."""
    return _day(sky.sun_at(15.0, dt.datetime(year, 3, 25, tzinfo=sky.UTC)))
