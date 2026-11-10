"""Offline holiday date rules, so a countdown can target "the next X" without a fixed date.

Spec grammar: "MM-DD" | "easter[+N|-N]" | "<1st|2nd|3rd|4th|5th|last>-<mon..sun>-MM" | "MM-DD!sub"
("!sub" is the UK bank-holiday substitution). Pure arithmetic: no network, no dependencies.
"""
import datetime as dt
import re
from typing import NamedTuple

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ORDINALS = {"1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5, "last": -1}


class Rule(NamedTuple):
    spec: str
    label: str


RULES = {k: Rule(*v) for k, v in {
    "new_year": ("01-01", "New Year's Day"),
    "valentines": ("02-14", "Valentine's Day"),
    "mothers_day_uk": ("easter-21", "Mothering Sunday"),
    "easter": ("easter", "Easter Sunday"),
    "good_friday": ("easter-2", "Good Friday"),
    "easter_monday": ("easter+1", "Easter Monday"),
    "may_bank_holiday_uk": ("1st-mon-05", "Early May bank holiday"),
    "spring_bank_holiday_uk": ("last-mon-05", "Spring bank holiday"),
    "summer_bank_holiday_uk": ("last-mon-08", "Summer bank holiday"),
    "halloween": ("10-31", "Halloween"),
    "bonfire_night": ("11-05", "Bonfire Night"),
    "christmas": ("12-25", "Christmas"),
    "boxing_day": ("12-26", "Boxing Day"),
    "new_years_eve": ("12-31", "New Year's Eve"),
    "mothers_day_us": ("2nd-sun-05", "Mother's Day"),
    "independence_day_us": ("07-04", "Independence Day"),
    "thanksgiving_us": ("4th-thu-11", "Thanksgiving"),
    "christmas_bank_holiday_uk": ("12-25!sub", "Christmas Day bank holiday"),
    "boxing_day_bank_holiday_uk": ("12-26!sub", "Boxing Day bank holiday"),
    "new_year_bank_holiday_uk": ("01-01!sub", "New Year's Day bank holiday"),
}.items()}


def easter(year):
    """Western Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (a + 11 * h + 22 * ((32 + 2 * e + 2 * i - h - k) % 7)) // 451
    month, day = divmod(h + (32 + 2 * e + 2 * i - h - k) % 7 - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


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


def _date_in_year(spec, year):
    sub = spec.endswith("!sub")
    s = spec[:-4] if sub else spec
    try:
        if m := re.fullmatch(r"easter(?:([+-])(\d+))?", s):
            day = easter(year) + dt.timedelta(days=int(m[1] + m[2]) if m[1] else 0)
        elif m := re.fullmatch(r"(1st|2nd|3rd|4th|5th|last)-(%s)-(\d\d)" % "|".join(DAYS), s):
            day = _nth_weekday(year, ORDINALS[m[1]], DAYS.index(m[2]), int(m[3]))
        elif m := re.fullmatch(r"(\d\d)-(\d\d)", s):
            day = dt.date(year, int(m[1]), int(m[2]))
        else:
            raise ValueError
    except ValueError as e:
        raise ValueError(f"bad holiday spec: {spec!r}") from e
    return _substitute(day) if sub else day


def next_date(rule_id_or_spec, after):
    """First occurrence of a rule id or spec on or after ``after``."""
    spec = RULES[rule_id_or_spec].spec if rule_id_or_spec in RULES else rule_id_or_spec
    for year in (after.year, after.year + 1):
        if (day := _date_in_year(spec, year)) >= after:
            return day
    raise ValueError(f"no occurrence of {rule_id_or_spec!r} after {after}")


def label(rule_id):
    try:
        return RULES[rule_id].label
    except KeyError:
        raise ValueError(f"unknown holiday: {rule_id!r}") from None


def resolve(target, now):
    """``next:<id|spec>`` -> (midnight of next occurrence, label); anything else is an ISO datetime."""
    if not target.startswith("next:"):
        d = dt.datetime.fromisoformat(target)  # same rules as countdown.common.parse
        if now.tzinfo is None:
            return d.replace(tzinfo=None), None
        return (d.replace(tzinfo=now.tzinfo) if d.tzinfo is None else d), None
    key = target[5:].strip().lower()
    day = next_date(key, now.date())
    return dt.datetime.combine(day, dt.time(), tzinfo=now.tzinfo), RULES[key].label if key in RULES else key
