"""Hindu and Sikh festivals from the Sun and Moon, using the rules North Indian almanacs (Drik
Panchang) apply, for New Delhi. Regional calendars differ (and some festivals are kept on a
different day by different traditions), so these are marked approximate.

- Lunar months are amanta (new moon to new moon), named after the sidereal sign the Sun enters
  during the month (Lahiri ayanamsa): the month the Sun enters Aries in is Chaitra, and so on. A
  month the Sun enters no sign in is an extra (adhika) month, and festivals skip it.
- A tithi (lunar day) is a 12-degree step of the Moon ahead of the Sun. A festival is held on
  the day its tithi is in effect at the moment that festival is reckoned at (sunrise, midday,
  the evening or midnight), the first such day if two qualify. Some festivals are fixed by one
  tradition's rule where traditions differ (Janmashtami by the lunar day at sunrise, as ISKCON
  and Drik date it), and eclipses can move a festival a day (Holi 2026 in North India).
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache

from . import sky

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DELHI = (28.6139, 77.2090)
MONTHS = ("chaitra", "vaishakha", "jyeshtha", "ashadha", "shravana", "bhadrapada", "ashvin", "kartika",
          "margashirsha", "pausha", "magha", "phalguna")


@lru_cache(maxsize=32)
def lunar_months(year: int) -> tuple[tuple[str, dt.datetime, dt.datetime], ...]:
    """(name, start, end) of the regular (non-adhika) lunar months overlapping ``year``."""
    t = sky.new_moon_after(dt.datetime(year - 1, 12, 1, tzinfo=sky.UTC))
    out = []
    while t.year <= year:
        end = sky.new_moon_after(t + dt.timedelta(days=1))
        sign = int(sky.sidereal_sun(t) // 30)
        entry = sky.sun_enters_sign((sign + 1) % 12, t)
        if entry < end:  # the Sun changes sign in this month: it is named after that sign
            out.append((MONTHS[(sign + 1) % 12], t, end))
        t = end
    return tuple(out)


def _moment(day: dt.date, when: str) -> dt.datetime:
    rise, set_ = sky.sunrise_sunset(day, *DELHI, IST)
    return {"sunrise": rise, "midday": rise + (set_ - rise) / 2,
            "afternoon": rise + (set_ - rise) * 0.7,     # aparahna, the fourth fifth of daylight
            "evening": set_ + dt.timedelta(minutes=30),  # pradosh, just after sunset
            "midnight": dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), tzinfo=IST)}[when]


def festival(year: int, month: str, tithi: int, when: str) -> dt.date | None:
    """The day in ``year`` that lunar day ``tithi`` of ``month`` is in effect at ``when``."""
    for name, start, end in lunar_months(year):
        if name != month:
            continue
        # a minute early, so lunar day 1 (which begins at the new moon itself) isn't missed
        begins = sky.elongation_after((tithi - 1) * 12.0, start - dt.timedelta(minutes=1))
        if begins >= end:
            continue
        ends = sky.elongation_after(tithi * 12.0 % 360, begins + dt.timedelta(hours=1))
        day = begins.astimezone(IST).date() - dt.timedelta(days=1)
        while day <= ends.astimezone(IST).date():
            if begins <= _moment(day, when) < ends and day.year == year:
                return day
            day += dt.timedelta(days=1)
        # no moment of that kind fell inside the tithi (a short tithi): the day it began
        first = begins.astimezone(IST).date()
        if first.year == year:
            return first
    return None


def sankranti(year: int, sign: int) -> dt.date:
    """The day the Sun enters a sidereal sign, counted from sunrise to sunrise: after sunset it
    is kept the next day (Makar Sankranti, Vaisakhi)."""
    t = sky.sun_enters_sign(sign, dt.datetime(year, 1, 1, tzinfo=sky.UTC)).astimezone(IST)
    _, set_ = sky.sunrise_sunset(t.date(), *DELHI, IST)
    return t.date() + dt.timedelta(days=1) if t > set_ else t.date()
