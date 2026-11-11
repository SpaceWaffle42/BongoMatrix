"""The tabular Islamic calendar (the 30-year arithmetic cycle). Real observance follows the moon
being sighted (or a national calendar such as Umm al-Qura), so a date can be a day out either
way; holidays from here are marked approximate. A day begins at sunset the evening before."""
from __future__ import annotations

import datetime as dt

EPOCH = 227015  # R.D. of 1 Muharram AH 1 (16 July 622, Julian)
MUHARRAM, RABI_I, RAMADAN, SHAWWAL, DHU_AL_HIJJAH = 1, 3, 9, 10, 12


def to_date(year: int, month: int, day: int) -> dt.date:
    rd = day + 29 * (month - 1) + (6 * month - 1) // 11 + (year - 1) * 354 + (3 + 11 * year) // 30 + EPOCH - 1
    return dt.date.fromordinal(rd)


def dates_in(gregorian: int, month: int, day: int) -> list[dt.date]:
    """Every Gregorian date in ``gregorian`` that is ``day`` ``month`` in some Islamic year: the
    Islamic year is 11 days shorter, so now and then a holiday falls twice in one year."""
    guess = int((gregorian - 622) * 33 / 32)
    found = [to_date(y, month, day) for y in range(guess - 1, guess + 3)]
    return [d for d in found if d.year == gregorian]
