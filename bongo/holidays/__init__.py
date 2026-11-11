"""Holidays, offline, in packs (shared by Countdown and Calendar), so a module can ask for "the
next Diwali" without anyone typing in a date.

Each holiday belongs to a pack and works its dates out from that calendar's own rules:

- Seasons, UK, US and Christian dates are Gregorian rules (``rules.py``), Easter included.
- Jewish dates are exact Hebrew-calendar arithmetic (``hebrew.py``); each begins at sunset the
  evening before the date.
- Islamic dates use the tabular calendar (``hijri.py``): marked approximate, as the real day
  follows the new moon being sighted and can be a day either side.
- Hindu and Sikh dates follow the North Indian almanac rules from the Sun and Moon (``hindu.py``):
  marked approximate, as regional calendars and traditions differ.
- Chinese festivals come from the lunisolar calendar for Beijing (``chinese.py``).

``next_date`` also accepts a rule spec (``"12-25"``, ``"last-mon-05"``, ``"easter-2"``) for your own
dates. Everything is arithmetic: no network, no data files that run out.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache
from typing import Callable, NamedTuple

from . import chinese, hebrew, hijri, hindu
from .rules import date_in_year, easter, orthodox_easter  # noqa: F401  (easter is part of the API)


class Holiday(NamedTuple):
    label: str
    pack: str
    dates: Callable[[int], list]  # year -> the holiday's dates in that year (usually one)
    icon: str                     # name in bongo.icons
    approx: bool = False


class Pack(NamedTuple):
    label: str
    note: str = ""


PACKS = {
    "seasons": Pack("Seasons and occasions"),
    "uk": Pack("UK"),
    "us": Pack("US"),
    "christian": Pack("Christian"),
    "jewish": Pack("Jewish", "Each begins at sunset the evening before."),
    "islamic": Pack("Islamic", "Approximate: the day follows the moon being sighted and can be a day either side."),
    "hindu_sikh": Pack("Hindu and Sikh", "Approximate: North Indian almanac rules; regional calendars can differ by a day."),
    "chinese": Pack("Chinese", "Lunar calendar for Beijing time."),
}


def _rule(spec):
    return lambda y: [date_in_year(spec, y)]


def _heb(month, day):
    return lambda y: [hebrew.to_date(hebrew.year_of(y, month), month, day)]


def _hij(month, day):
    return lambda y: hijri.dates_in(y, month, day)


def _chn(month, day):
    return lambda y: [chinese.to_date(y, month, day)]


def _hin(month, tithi, when, shift=0):
    def dates(y):
        d = hindu.festival(y, month, tithi, when)
        return [d + dt.timedelta(days=shift)] if d else []
    return dates


def _sank(sign):
    return lambda y: [hindu.sankranti(y, sign)]


H = Holiday
RULES: dict[str, Holiday] = {
    # Seasons and occasions
    "new_year": H("New Year's Day", "seasons", _rule("01-01"), "fireworks"),
    "valentines": H("Valentine's Day", "seasons", _rule("02-14"), "heart"),
    "fathers_day": H("Father's Day", "seasons", _rule("3rd-sun-06"), "tie"),
    "halloween": H("Halloween", "seasons", _rule("10-31"), "pumpkin"),
    "new_years_eve": H("New Year's Eve", "seasons", _rule("12-31"), "fireworks"),
    # UK
    "mothers_day_uk": H("Mothering Sunday", "uk", _rule("easter-21"), "flower"),
    "st_patricks_day": H("St Patrick's Day", "uk", _rule("03-17"), "shamrock"),
    "may_bank_holiday_uk": H("Early May bank holiday", "uk", _rule("1st-mon-05"), "flower"),
    "spring_bank_holiday_uk": H("Spring bank holiday", "uk", _rule("last-mon-05"), "sun"),
    "summer_bank_holiday_uk": H("Summer bank holiday", "uk", _rule("last-mon-08"), "sun"),
    "bonfire_night": H("Bonfire Night", "uk", _rule("11-05"), "bonfire"),
    "remembrance_sunday_uk": H("Remembrance Sunday", "uk", _rule("2nd-sun-11"), "poppy"),
    "christmas_bank_holiday_uk": H("Christmas Day bank holiday", "uk", _rule("12-25!sub"), "tree"),
    "boxing_day_bank_holiday_uk": H("Boxing Day bank holiday", "uk", _rule("12-26!sub"), "gift"),
    "new_year_bank_holiday_uk": H("New Year's Day bank holiday", "uk", _rule("01-01!sub"), "fireworks"),
    # US
    "mlk_day_us": H("Martin Luther King Jr. Day", "us", _rule("3rd-mon-01"), "star"),
    "presidents_day_us": H("Presidents' Day", "us", _rule("3rd-mon-02"), "flag"),
    "mothers_day_us": H("Mother's Day", "us", _rule("2nd-sun-05"), "flower"),
    "memorial_day_us": H("Memorial Day", "us", _rule("last-mon-05"), "flag"),
    "independence_day_us": H("Independence Day", "us", _rule("07-04"), "fireworks"),
    "labor_day_us": H("Labor Day", "us", _rule("1st-mon-09"), "flag"),
    "thanksgiving_us": H("Thanksgiving", "us", _rule("4th-thu-11"), "turkey"),
    # Christian
    "epiphany": H("Epiphany", "christian", _rule("01-06"), "star"),
    "shrove_tuesday": H("Shrove Tuesday (Pancake Day)", "christian", _rule("easter-47"), "pancake"),
    "ash_wednesday": H("Ash Wednesday", "christian", _rule("easter-46"), "cross"),
    "palm_sunday": H("Palm Sunday", "christian", _rule("easter-7"), "palm"),
    "good_friday": H("Good Friday", "christian", _rule("easter-2"), "cross"),
    "easter": H("Easter Sunday", "christian", _rule("easter"), "egg"),
    "easter_monday": H("Easter Monday", "christian", _rule("easter+1"), "egg"),
    "ascension": H("Ascension Day", "christian", _rule("easter+39"), "cross"),
    "pentecost": H("Pentecost", "christian", _rule("easter+49"), "dove"),
    "orthodox_easter": H("Orthodox Easter", "christian", _rule("orthodox_easter"), "egg"),
    "christmas_eve": H("Christmas Eve", "christian", _rule("12-24"), "star"),
    "christmas": H("Christmas", "christian", _rule("12-25"), "tree"),
    "boxing_day": H("Boxing Day", "christian", _rule("12-26"), "gift"),
    # Jewish
    "purim": H("Purim", "jewish", lambda y: [hebrew.purim(y)], "mask"),
    "passover": H("Passover", "jewish", _heb(hebrew.NISAN, 15), "wine"),
    "shavuot": H("Shavuot", "jewish", _heb(hebrew.SIVAN, 6), "wheat"),
    "rosh_hashanah": H("Rosh Hashanah", "jewish", _heb(hebrew.TISHRI, 1), "apple"),
    "yom_kippur": H("Yom Kippur", "jewish", _heb(hebrew.TISHRI, 10), "candle"),
    "sukkot": H("Sukkot", "jewish", _heb(hebrew.TISHRI, 15), "palm"),
    "simchat_torah": H("Simchat Torah", "jewish", _heb(hebrew.TISHRI, 23), "scroll"),
    "hanukkah": H("Hanukkah", "jewish", _heb(hebrew.KISLEV, 25), "menorah"),
    # Islamic
    "ramadan": H("Ramadan begins", "islamic", _hij(hijri.RAMADAN, 1), "crescent", True),
    "laylat_al_qadr": H("Laylat al-Qadr", "islamic", _hij(hijri.RAMADAN, 27), "star", True),
    "eid_al_fitr": H("Eid al-Fitr", "islamic", _hij(hijri.SHAWWAL, 1), "crescent", True),
    "eid_al_adha": H("Eid al-Adha", "islamic", _hij(hijri.DHU_AL_HIJJAH, 10), "crescent", True),
    "islamic_new_year": H("Islamic New Year", "islamic", _hij(hijri.MUHARRAM, 1), "crescent", True),
    "ashura": H("Ashura", "islamic", _hij(hijri.MUHARRAM, 10), "crescent", True),
    "mawlid": H("Mawlid an-Nabi", "islamic", _hij(hijri.RABI_I, 12), "lantern", True),
    # Hindu and Sikh
    "makar_sankranti": H("Makar Sankranti", "hindu_sikh", _sank(9), "kite", True),
    "maha_shivaratri": H("Maha Shivaratri", "hindu_sikh", _hin("magha", 29, "midnight"), "moon", True),
    "holi": H("Holi", "hindu_sikh", _hin("phalguna", 15, "evening", shift=1), "colours", True),
    "vaisakhi": H("Vaisakhi", "hindu_sikh", _sank(0), "wheat", True),
    "raksha_bandhan": H("Raksha Bandhan", "hindu_sikh", _hin("shravana", 15, "sunrise"), "rakhi", True),
    "janmashtami": H("Janmashtami", "hindu_sikh", _hin("shravana", 23, "sunrise"), "flute", True),
    "ganesh_chaturthi": H("Ganesh Chaturthi", "hindu_sikh", _hin("bhadrapada", 4, "midday"), "flower", True),
    "navratri": H("Navratri begins", "hindu_sikh", _hin("ashvin", 1, "sunrise"), "lamp", True),
    "dussehra": H("Dussehra", "hindu_sikh", _hin("ashvin", 10, "afternoon"), "bow", True),
    "diwali": H("Diwali", "hindu_sikh", _hin("ashvin", 30, "evening"), "lamp", True),
    "bandi_chhor_divas": H("Bandi Chhor Divas", "hindu_sikh", _hin("ashvin", 30, "evening"), "lamp", True),
    "guru_nanak_gurpurab": H("Guru Nanak Gurpurab", "hindu_sikh", _hin("kartika", 15, "sunrise"), "lamp", True),
    # Chinese
    "lunar_new_year": H("Lunar New Year", "chinese", _chn(1, 1), "lantern"),
    "lantern_festival": H("Lantern Festival", "chinese", _chn(1, 15), "lantern"),
    "qingming": H("Qingming", "chinese", lambda y: [chinese.qingming(y)], "willow"),
    "dragon_boat": H("Dragon Boat Festival", "chinese", _chn(5, 5), "boat"),
    "mid_autumn": H("Mid-Autumn Festival", "chinese", _chn(8, 15), "mooncake"),
}
del H


@lru_cache(maxsize=1024)
def _dates(rule_id: str, year: int) -> tuple:
    return tuple(sorted(RULES[rule_id].dates(year)))


def next_date(rule_id_or_spec: str, after: dt.date) -> dt.date:
    """First occurrence of a holiday id or rule spec on or after ``after``."""
    if rule_id_or_spec in RULES:
        for year in range(after.year, after.year + 3):
            for day in _dates(rule_id_or_spec, year):
                if day >= after:
                    return day
    else:
        for year in (after.year, after.year + 1):
            if (day := date_in_year(rule_id_or_spec, year)) >= after:
                return day
    raise ValueError(f"no occurrence of {rule_id_or_spec!r} after {after}")


def label(rule_id: str) -> str:
    try:
        return RULES[rule_id].label
    except KeyError:
        raise ValueError(f"unknown holiday: {rule_id!r}") from None


def by_pack() -> dict[str, list[str]]:
    """Holiday ids grouped by pack, in pack order and then the order of the year."""
    out = {pack: [] for pack in PACKS}
    for rule_id, h in RULES.items():
        out[h.pack].append(rule_id)
    return out


def choice_groups() -> dict[str, str]:
    """id -> the heading a dashboard list groups it under: its pack, saying once when the pack's
    dates are approximate ("Islamic (dates approximate)") rather than on every holiday."""
    loose = {h.pack for h in RULES.values() if h.approx}
    return {rule_id: PACKS[h.pack].label + (" (dates approximate)" if h.pack in loose else "")
            for rule_id, h in RULES.items()}


def choice_labels() -> dict[str, str]:
    """id -> the holiday's name, for the dashboard."""
    return {rule_id: h.label for rule_id, h in RULES.items()}


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
