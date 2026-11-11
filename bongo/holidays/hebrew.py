"""The Hebrew calendar, exactly: it is fixed arithmetic (Dershowitz & Reingold, Calendrical
Calculations). Months count from Nisan = 1; Tishri = 7, Kislev = 9, Adar = 12, Adar II = 13.
A Jewish holiday begins at sunset the evening before the date given here."""
from __future__ import annotations

import datetime as dt

EPOCH = -1373427  # R.D. (= Python ordinal) of 1 Tishri AM 1
NISAN, IYYAR, SIVAN, TAMMUZ, AV, ELUL, TISHRI, MARHESHVAN, KISLEV, TEVET, SHEVAT, ADAR, ADAR_II = range(1, 14)


def leap(year: int) -> bool:
    return (7 * year + 1) % 19 < 7


def _elapsed(year: int) -> int:
    months = (235 * year - 234) // 19
    parts = 12084 + 13753 * months
    day = 29 * months + parts // 25920
    return day + 1 if (3 * (day + 1)) % 7 < 3 else day


def _correction(year: int) -> int:
    ny0, ny1, ny2 = _elapsed(year - 1), _elapsed(year), _elapsed(year + 1)
    if ny2 - ny1 == 356:
        return 2
    return 1 if ny1 - ny0 == 382 else 0


def new_year(year: int) -> int:
    return EPOCH + _elapsed(year) + _correction(year)


def _month_days(month: int, year: int) -> int:
    length = new_year(year + 1) - new_year(year)
    if month in (IYYAR, TAMMUZ, ELUL, TEVET, ADAR_II) or (month == ADAR and not leap(year)) \
            or (month == MARHESHVAN and length % 10 != 5) or (month == KISLEV and length % 10 == 3):
        return 29
    return 30


def to_date(year: int, month: int, day: int) -> dt.date:
    """Gregorian date of a Hebrew date."""
    last = ADAR_II if leap(year) else ADAR
    if month < TISHRI:
        days = sum(_month_days(m, year) for m in range(TISHRI, last + 1)) + sum(_month_days(m, year) for m in range(NISAN, month))
    else:
        days = sum(_month_days(m, year) for m in range(TISHRI, month))
    return dt.date.fromordinal(new_year(year) + days + day - 1)


def year_of(gregorian: int, month: int) -> int:
    """The Hebrew year in which ``month`` falls inside Gregorian year ``gregorian``: the year turns
    at Tishri, so Tishri to Kislev are this autumn's new year, the rest the year begun last autumn."""
    return gregorian + 3761 if TISHRI <= month <= KISLEV else gregorian + 3760


def purim(gregorian: int) -> dt.date:
    year = gregorian + 3760
    return to_date(year, ADAR_II if leap(year) else ADAR, 14)
