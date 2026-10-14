"""Offline holiday packs: every date is calculated, never downloaded.

Pure arithmetic so the panel and the dashboard agree without network access:
- Western/Orthodox Easter: Gregorian and Julian computus.
- Hebrew calendar: exact arithmetic calendar (molad, postponements).
- Islamic months: astronomical new moon, Umm al-Qura-style start (conjunction
  before Mecca sunset and the Moon setting after the Sun); local sighting can
  differ by a day, so every date can be edited per year.
- Hindu/Buddhist/Sikh/Chinese dates: lunar phases and solar ingresses from
  truncated Meeus series (better than a minute of arc), with the usual tithi
  rules evaluated in India/China civil time. Regional panchangs occasionally
  differ by a day; edits cover that.
Supported years: 1950-2150.
"""
import datetime as dt
import math
from functools import lru_cache

YEARS = range(1950, 2151)
_J2000 = 2451545.0
_RAD = math.pi / 180


def _jd(moment):
    return moment.timestamp() / 86400 + 2440587.5


def _moment(jd):
    return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(days=jd - 2440587.5)


def _centuries(jd):
    return (jd + 69.0 / 86400 - _J2000) / 36525  # TT ~ UTC + 69 s


def sun_longitude(jd):
    t = _centuries(jd)
    m = (357.52911 + 35999.05029 * t - 0.0001537 * t * t) * _RAD
    centre = ((1.914602 - 0.004817 * t - 0.000014 * t * t) * math.sin(m)
              + (0.019993 - 0.000101 * t) * math.sin(2 * m) + 0.000289 * math.sin(3 * m))
    omega = (125.04 - 1934.136 * t) * _RAD
    return (280.46646 + 36000.76983 * t + 0.0003032 * t * t + centre
            - 0.00569 - 0.00478 * math.sin(omega)) % 360


# Meeus table 47.A (D, M, M', F, coefficient in 1e-6 degrees)
_MOON = (
    (0, 0, 1, 0, 6288774), (2, 0, -1, 0, 1274027), (2, 0, 0, 0, 658314), (0, 0, 2, 0, 213618),
    (0, 1, 0, 0, -185116), (0, 0, 0, 2, -114332), (2, 0, -2, 0, 58793), (2, -1, -1, 0, 57066),
    (2, 0, 1, 0, 53322), (2, -1, 0, 0, 45758), (0, 1, -1, 0, -40923), (1, 0, 0, 0, -34720),
    (0, 1, 1, 0, -30383), (2, 0, 0, -2, 15327), (0, 0, 1, 2, -12528), (0, 0, 1, -2, 10980),
    (4, 0, -1, 0, 10675), (0, 0, 3, 0, 10034), (4, 0, -2, 0, 8548), (2, 1, -1, 0, -7888),
    (2, 1, 0, 0, -6766), (1, 0, -1, 0, -5163), (1, 1, 0, 0, 4987), (2, -1, 1, 0, 4036),
    (2, 0, 2, 0, 3994), (4, 0, 0, 0, 3861), (2, 0, -3, 0, 3665), (0, 1, -2, 0, -2689),
    (2, 0, -1, 2, -2602), (2, -1, -2, 0, 2390), (1, 0, 1, 0, -2348), (2, -2, 0, 0, 2236),
    (0, 1, 2, 0, -2120), (0, 2, 0, 0, -2069), (2, -2, -1, 0, 2048), (2, 0, 1, -2, -1773),
    (2, 0, 0, 2, -1595), (4, -1, -1, 0, 1215), (0, 0, 2, 2, -1110), (3, 0, -1, 0, -892),
    (2, 1, 1, 0, -810), (4, -1, -2, 0, 759), (0, 2, -1, 0, -713), (2, 2, -1, 0, -700),
    (2, 1, -2, 0, 691), (2, -1, 0, -2, 596), (4, 0, 1, 0, 549), (0, 0, 4, 0, 537),
    (4, -1, 0, 0, 520), (1, 0, -2, 0, -487), (2, 1, 0, -2, -399), (0, 0, 2, -2, -381),
    (1, 1, 1, 0, 351), (3, 0, -2, 0, -340), (4, 0, -3, 0, 330), (2, -1, 2, 0, 327),
    (0, 2, 1, 0, -323), (1, 1, -1, 0, 299), (2, 0, 3, 0, 294),
)


def moon_longitude(jd):
    t = _centuries(jd)
    lp = 218.3164477 + 481267.88123421 * t - 0.0015786 * t * t + t ** 3 / 538841
    d = (297.8501921 + 445267.1114034 * t - 0.0018819 * t * t + t ** 3 / 545868) * _RAD
    m = (357.5291092 + 35999.0502909 * t - 0.0001536 * t * t) * _RAD
    mp = (134.9633964 + 477198.8675055 * t + 0.0087414 * t * t + t ** 3 / 69699) * _RAD
    f = (93.2720950 + 483202.0175233 * t - 0.0036539 * t * t) * _RAD
    e = 1 - 0.002516 * t - 0.0000074 * t * t
    total = 0.0
    for cd, cm, cmp, cf, coefficient in _MOON:
        total += coefficient * e ** abs(cm) * math.sin(cd * d + cm * m + cmp * mp + cf * f)
    a1 = (119.75 + 131.849 * t) * _RAD
    a2 = (53.09 + 479264.290 * t) * _RAD
    total += 3958 * math.sin(a1) + 1962 * math.sin(lp * _RAD - f) + 318 * math.sin(a2)
    return (lp + total / 1e6) % 360


def elongation(jd):
    return (moon_longitude(jd) - sun_longitude(jd)) % 360


def _solve(function, target, guess, rate):
    jd = guess
    for _ in range(60):
        error = (function(jd) - target + 180) % 360 - 180
        if abs(error) < 1e-7:
            break
        jd -= error / rate
    return jd


def phase_after(jd, angle):
    """First instant at/after jd when the Moon-Sun elongation equals angle."""
    ahead = (angle - elongation(jd)) % 360
    result = _solve(elongation, angle, jd + ahead / 12.19, 12.19)
    if result < jd - 1e-6:
        result = _solve(elongation, angle, result + 29.53, 12.19)
    return result


def phase_before(jd, angle):
    result = phase_after(jd - 30.6, angle)
    while True:
        following = phase_after(result + 1, angle)
        if following > jd:
            return result
        result = following


def ayanamsa(jd):
    return 23.853 + (jd - _J2000) / 365.25 * 0.0139698  # Lahiri


def sidereal_sun(jd):
    return (sun_longitude(jd) - ayanamsa(jd)) % 360


def solar_ingress(year, angle, *, sidereal=False, month=1):
    function = sidereal_sun if sidereal else sun_longitude
    guess = _jd(dt.datetime(year, month, 1, tzinfo=dt.timezone.utc))
    ahead = (angle - function(guess)) % 360
    return _solve(function, angle, guess + ahead / 0.9856, 0.9856)


def _civil(jd, offset_hours):
    return (_moment(jd) + dt.timedelta(hours=offset_hours)).date()


IST, CST = 5.5, 8.0


def delhi_sunrise_hour(day):
    """Approximate sunrise (IST hours) at New Delhi; within ~2 minutes."""
    n = day.timetuple().tm_yday
    gamma = 2 * math.pi / 365 * (n - 1)
    equation = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                         - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    declination = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
                   - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma))
    latitude = 28.61 * _RAD
    cos_hour = (math.cos(90.833 * _RAD) / (math.cos(latitude) * math.cos(declination))
                - math.tan(latitude) * math.tan(declination))
    minutes = 720 - 4 * (77.21 + math.degrees(math.acos(cos_hour))) - equation
    return minutes / 60 + IST


SUNRISE = 'sunrise'


def _first_local_time_in(start_jd, end_jd, offset, hour):
    """Date of the first local clock `hour` (or Delhi sunrise) inside [start, end)."""
    day = _civil(start_jd, offset)
    for _ in range(3):
        clock = delhi_sunrise_hour(day) if hour == SUNRISE else hour
        moment = dt.datetime.combine(day, dt.time()) + dt.timedelta(hours=clock - offset)
        jd = _jd(moment.replace(tzinfo=dt.timezone.utc))
        if start_jd <= jd < end_jd:
            return day
        day += dt.timedelta(days=1)
    return None


def _tithi(month_start, number):
    """Start/end instants of tithi `number` (1-30) after a new moon."""
    return (phase_after(month_start - 1e-4, (number - 1) * 12 % 360) if number > 1 else month_start,
            phase_after(month_start + (number - 1) * 0.9, number * 12 % 360))


def hindu_month_start(year, sidereal_angle, month):
    """New moon beginning the amanta month containing the sidereal ingress."""
    ingress = solar_ingress(year, sidereal_angle, sidereal=True, month=month)
    return phase_before(ingress, 0)


def _tithi_day(month_start, number, hour, fallback_offset=0):
    start, end = _tithi(month_start, number)
    day = _first_local_time_in(start, end, IST, hour)
    return day or _civil(start, IST) + dt.timedelta(days=fallback_offset)


# ---- Gregorian / Julian computus ---------------------------------------------------------------

def easter(year):
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * l) // 433
    month = (h + l - 7 * m + 90) // 25
    return dt.date(year, month, (h + l - 7 * m + 33 * month + 19) % 32)


def _julian_to_gregorian(year, month, day):
    a = (14 - month) // 12
    y, m = year + 4800 - a, month + 12 * a - 3
    jdn = day + (153 * m + 2) // 5 + 365 * y + y // 4 - 32083
    return dt.date.fromordinal(jdn - 1721425)


def orthodox_easter(year):
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month, day = divmod(d + e + 114, 31)
    return _julian_to_gregorian(year, month, day + 1)


# ---- Hebrew calendar (exact) -------------------------------------------------------------------

def _hebrew_leap(year):
    return (7 * year + 1) % 19 < 7


def _hebrew_elapsed(year):
    months = (235 * year - 234) // 19
    parts = 12084 + 13753 * months
    day = months * 29 + parts // 25920
    return day + 1 if (3 * (day + 1)) % 7 < 3 else day


def _hebrew_delay(year):
    last, present, following = (_hebrew_elapsed(year + offset) for offset in (-1, 0, 1))
    if following - present == 356:
        return 2
    if present - last == 382:
        return 1
    return 0


_HEBREW_EPOCH = dt.date(1, 1, 1).toordinal() - 1373427 + 1  # R.D. -1373427 (Tishrei 1, AM 1)


def _hebrew_new_year(year):
    return -1373427 + _hebrew_elapsed(year) + _hebrew_delay(year)


def _hebrew_year_days(year):
    return _hebrew_new_year(year + 1) - _hebrew_new_year(year)


def _hebrew_month_days(year, month):
    if month in (2, 4, 6, 10, 13):
        return 29
    if month == 12 and not _hebrew_leap(year):
        return 29
    if month == 8 and _hebrew_year_days(year) % 10 != 5:
        return 29
    if month == 9 and _hebrew_year_days(year) % 10 == 3:
        return 29
    return 30


def hebrew_date(year, month, day):
    """Gregorian date for a Hebrew date. Months: 1 Nisan ... 7 Tishrei ... 12 Adar (13 Adar II)."""
    last = 13 if _hebrew_leap(year) else 12
    rata = _hebrew_new_year(year) + day - 1
    if month < 7:
        rata += sum(_hebrew_month_days(year, m) for m in range(7, last + 1))
        rata += sum(_hebrew_month_days(year, m) for m in range(1, month))
    else:
        rata += sum(_hebrew_month_days(year, m) for m in range(7, month))
    return dt.date.fromordinal(rata)


def _hebrew_in(gregorian_year, month, day):
    """The occurrence of a Hebrew date within a Gregorian year."""
    for year in (gregorian_year + 3760, gregorian_year + 3761):
        if month == 12 and _hebrew_leap(year):
            month_used = 13  # Purim is kept in Adar II during leap years
        else:
            month_used = month
        result = hebrew_date(year, month_used, day)
        if result.year == gregorian_year:
            return result
    return None


# ---- Islamic months ----------------------------------------------------------------------------

def _islamic_tabular(year, month, day):
    return dt.date.fromordinal(227014 + day + 29 * (month - 1) + (6 * month - 1) // 11
                               + (year - 1) * 354 + (3 + 11 * year) // 30)


def islamic_month_start(year, month):
    estimate = _islamic_tabular(year, month, 1)
    noon = _jd(dt.datetime.combine(estimate, dt.time(12), dt.timezone.utc))
    conjunction = phase_before(noon + 2, 0)
    if conjunction < noon - 4:
        conjunction = phase_after(noon - 4, 0)
    day = _civil(conjunction, 3)  # Mecca
    sunset = _jd(dt.datetime.combine(day, dt.time(15, 30), dt.timezone.utc))
    # The Moon sets after the Sun when it is east of the Sun by a few degrees at sunset.
    visible = conjunction < sunset and elongation(sunset) < 180
    return day + dt.timedelta(days=1 if visible else 2)


def _islamic_in(gregorian_year, month, day):
    """Every occurrence in the Gregorian year (the Hijri year is ~11 days shorter, so
    a holiday occasionally falls twice in one year, e.g. Eid al-Fitr 2030)."""
    guess = round((gregorian_year - 622) * 33 / 32)
    found = []
    for year in range(guess - 2, guess + 3):
        start = islamic_month_start(year, month) + dt.timedelta(days=day - 1)
        if start.year == gregorian_year:
            found.append(start)
    return tuple(sorted(found)) or None


# ---- Chinese ---------------------------------------------------------------------------------

def lunar_new_year(year):
    solstice = solar_ingress(year - 1, 270, month=12)
    first = phase_after(solstice, 0)
    second = phase_after(first + 1, 0)
    # A leap 11th/12th month (rare) would push it a month later; the second
    # new moon after the solstice is right for 1950-2150 except edited years.
    return _civil(second, CST)


# ---- packs -----------------------------------------------------------------------------------

def _fixed(month, day):
    return lambda year: dt.date(year, month, day)


def _nth_weekday(month, weekday, n):
    def compute(year):
        first = dt.date(year, month, 1)
        return first + dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    return compute


def _offset(base, days):
    return lambda year: base(year) + dt.timedelta(days=days)


def _diwali(year):
    return _tithi_day(hindu_month_start(year, 210, 10), 30, 18.5)  # Amavasya ending Ashvin


def _diwali_real(year):
    start = hindu_month_start(year, 210, 10)  # Kartika begins at this new moon
    previous_full = phase_before(start, 180)
    begin = phase_after(previous_full, 348)
    return _first_local_time_in(begin, start, IST, 18.5) or _civil(begin, IST)


def _holi(year):
    """Colours day after Holika Dahan (Phalguna Purnima evening, after Bhadra).

    Bhadra (Vishti) covers the first half of Purnima. When it lasts past
    midnight after the first Purnima evening and Purnima still runs into the
    next afternoon, the bonfire moves to that next day.
    """
    month_start = hindu_month_start(year, 330, 2)
    begin, end = phase_after(month_start + 10, 168), phase_after(month_start + 12, 180)
    bhadra_end = phase_after(begin, 174)
    dahan = _first_local_time_in(begin, end, IST, 18.5) or _civil(begin, IST)
    at = lambda day, hour: _jd(dt.datetime.combine(day, dt.time(), dt.timezone.utc) + dt.timedelta(hours=hour - IST))
    following = dahan + dt.timedelta(days=1)
    if bhadra_end >= at(following, 0) and end >= at(following, 15):
        dahan = following
    return dahan + dt.timedelta(days=1)


def _navaratri(year):
    month_start = hindu_month_start(year, 180, 9)
    end = phase_after(month_start + 0.5, 12)
    return _first_local_time_in(month_start, end, IST, SUNRISE) or _civil(month_start, IST)


def _dussehra(year):
    month_start = hindu_month_start(year, 180, 9)
    begin, end = phase_after(month_start + 7, 108), phase_after(month_start + 8, 120)
    # Dashami at the start of the afternoon (aparahna), else the day it holds at sunrise.
    return (_first_local_time_in(begin, end, IST, 13.25) or _first_local_time_in(begin, end, IST, SUNRISE)
            or _civil(begin, IST))


def _full_moon_day(year, sidereal_angle, month, hour=SUNRISE):
    month_start = hindu_month_start(year, sidereal_angle, month)
    begin, end = phase_after(month_start + 12, 168), phase_after(month_start + 13, 180)
    return _first_local_time_in(begin, end, IST, hour) or _civil(end, IST)


def _vesak(year):
    return _civil(phase_after(hindu_month_start(year, 30, 4) + 1, 180), IST)


def _vaisakhi(year):
    return _civil(solar_ingress(year, 0, sidereal=True, month=4), IST)


def _guru_nanak(year):
    return _full_moon_day(year, 210, 10)


def _qingming(year):
    return _civil(solar_ingress(year, 15, month=3), CST)


H = dict  # holiday definition: id, name, icon, compute, note
PACKS = {
    'general': dict(name='General', note='Fixed civil days.', holidays=(
        H(id='new_year', name="New Year's Day", icon='firework', compute=_fixed(1, 1)),
        H(id='valentines', name="Valentine's Day", icon='heart', compute=_fixed(2, 14)),
        H(id='halloween', name='Halloween', icon='pumpkin', compute=_fixed(10, 31)),
        H(id='new_years_eve', name="New Year's Eve", icon='firework', compute=_fixed(12, 31)),
    )),
    'christian': dict(name='Christianity', note='Western (Gregorian) dates; Easter by the computus.', holidays=(
        H(id='epiphany', name='Epiphany', icon='star', compute=_fixed(1, 6)),
        H(id='ash_wednesday', name='Ash Wednesday', icon='cross', compute=_offset(easter, -46)),
        H(id='palm_sunday', name='Palm Sunday', icon='cross', compute=_offset(easter, -7)),
        H(id='good_friday', name='Good Friday', icon='cross', compute=_offset(easter, -2)),
        H(id='easter', name='Easter Sunday', icon='egg', compute=easter),
        H(id='ascension', name='Ascension Day', icon='cross', compute=_offset(easter, 39)),
        H(id='pentecost', name='Pentecost', icon='dove', compute=_offset(easter, 49)),
        H(id='all_saints', name="All Saints' Day", icon='cross', compute=_fixed(11, 1)),
        H(id='christmas_eve', name='Christmas Eve', icon='tree', compute=_fixed(12, 24)),
        H(id='christmas', name='Christmas Day', icon='tree', compute=_fixed(12, 25)),
    )),
    'orthodox': dict(name='Orthodox Christianity', note='Julian-calendar feasts shown on Gregorian dates.', holidays=(
        H(id='orthodox_christmas', name='Orthodox Christmas', icon='tree', compute=_fixed(1, 7)),
        H(id='orthodox_good_friday', name='Orthodox Good Friday', icon='cross', compute=_offset(orthodox_easter, -2)),
        H(id='orthodox_easter', name='Orthodox Easter', icon='egg', compute=orthodox_easter),
    )),
    'islam': dict(name='Islam', note='Calculated from the new moon (Umm al-Qura style). Local moon sighting can move a date by a day; edit it if your community differs. Days begin at sunset the evening before.', holidays=(
        H(id='islamic_new_year', name='Islamic New Year', icon='crescent', compute=lambda y: _islamic_in(y, 1, 1)),
        H(id='ashura', name='Ashura', icon='crescent', compute=lambda y: _islamic_in(y, 1, 10)),
        H(id='mawlid', name='Mawlid an-Nabi', icon='crescent', compute=lambda y: _islamic_in(y, 3, 12)),
        H(id='ramadan', name='Ramadan begins', icon='lantern', compute=lambda y: _islamic_in(y, 9, 1)),
        H(id='laylat_al_qadr', name='Laylat al-Qadr', icon='crescent', compute=lambda y: _islamic_in(y, 9, 27)),
        H(id='eid_al_fitr', name='Eid al-Fitr', icon='crescent', compute=lambda y: _islamic_in(y, 10, 1)),
        H(id='eid_al_adha', name='Eid al-Adha', icon='crescent', compute=lambda y: _islamic_in(y, 12, 10)),
    )),
    'judaism': dict(name='Judaism', note='Exact Hebrew calendar. Each day begins at sunset the evening before the date shown.', holidays=(
        H(id='purim', name='Purim', icon='star_of_david', compute=lambda y: _hebrew_in(y, 12, 14)),
        H(id='passover', name='Passover', icon='star_of_david', compute=lambda y: _hebrew_in(y, 1, 15)),
        H(id='shavuot', name='Shavuot', icon='star_of_david', compute=lambda y: _hebrew_in(y, 3, 6)),
        H(id='rosh_hashanah', name='Rosh Hashanah', icon='shofar', compute=lambda y: _hebrew_in(y, 7, 1)),
        H(id='yom_kippur', name='Yom Kippur', icon='star_of_david', compute=lambda y: _hebrew_in(y, 7, 10)),
        H(id='sukkot', name='Sukkot', icon='star_of_david', compute=lambda y: _hebrew_in(y, 7, 15)),
        H(id='hanukkah', name='Hanukkah', icon='menorah', compute=lambda y: _hebrew_in(y, 9, 25)),
    )),
    'hinduism': dict(name='Hinduism', note='Lunar dates for India (Lahiri). Regional calendars can differ by a day; edit if needed.', holidays=(
        H(id='holi', name='Holi', icon='holi', compute=_holi),
        H(id='navaratri', name='Navaratri begins', icon='diya', compute=_navaratri),
        H(id='dussehra', name='Dussehra', icon='diya', compute=_dussehra),
        H(id='diwali', name='Diwali', icon='diya', compute=_diwali_real),
    )),
    'buddhism': dict(name='Buddhism', note='Vesak on the Vaishakha full moon (dates vary by country; edit if needed).', holidays=(
        H(id='vesak', name='Vesak', icon='lotus', compute=_vesak),
    )),
    'sikhism': dict(name='Sikhism', note='Vaisakhi by the solar calendar; Gurpurab on the Kartik full moon.', holidays=(
        H(id='vaisakhi', name='Vaisakhi', icon='khanda', compute=_vaisakhi),
        H(id='guru_nanak', name='Guru Nanak Gurpurab', icon='khanda', compute=_guru_nanak),
    )),
    'chinese': dict(name='Chinese festivals', note='Chinese lunisolar calendar (China time).', holidays=(
        H(id='lunar_new_year', name='Lunar New Year', icon='lantern', compute=lunar_new_year),
        H(id='lantern_festival', name='Lantern Festival', icon='lantern', compute=_offset(lunar_new_year, 14)),
        H(id='qingming', name='Qingming', icon='leaf', compute=_qingming),
    )),
}
HOLIDAYS = {f"{pack}.{item['id']}": dict(item, pack=pack) for pack, data in PACKS.items() for item in data['holidays']}
# Pixel-art icons that holidays use, in addition to the original event types.
HOLIDAY_ICONS = ('tree', 'pumpkin', 'egg', 'cross', 'dove', 'star', 'heart', 'firework', 'crescent',
                 'lantern', 'star_of_david', 'menorah', 'shofar', 'diya', 'holi', 'lotus', 'khanda', 'leaf')
_UNKNOWN_ICONS = {item['icon'] for item in HOLIDAYS.values()} - set(HOLIDAY_ICONS)
if _UNKNOWN_ICONS:
    raise RuntimeError(f"holiday packs use unknown icons: {sorted(_UNKNOWN_ICONS)}")


@lru_cache(maxsize=4096)
def calculated_date(key, year):
    if year not in YEARS:
        return None
    try:
        return HOLIDAYS[key]['compute'](year)
    except (ValueError, OverflowError):
        return None


def calculated_dates(key, year):
    value = calculated_date(key, year)
    return () if value is None else value if isinstance(value, tuple) else (value,)


def holiday_dates(key, year, overrides=None):
    """Edited date for this year if any (replaces the calculation), else calculated dates."""
    edited = ((overrides or {}).get(key) or {}).get(str(year))
    return (dt.date.fromisoformat(edited),) if edited else calculated_dates(key, year)


def pack_events(settings, years):
    """Countdown events for enabled packs (all-day, local calendar)."""
    hidden = set(settings.get('hidden_holidays', ()))
    overrides = settings.get('holiday_dates', {})
    events = []
    for pack, enabled in settings.get('packs', {}).items():
        if not enabled or pack not in PACKS:
            continue
        for item in PACKS[pack]['holidays']:
            key = f'{pack}.{item["id"]}'
            if key in hidden:
                continue
            for year in years:
                for index, date in enumerate(holiday_dates(key, year, overrides)):
                    events.append(dict(id=f'{pack}-{item["id"]}-{year}-{index}', title=item['name'],
                                       start=date.isoformat(), end=None, all_day=True, annual=False,
                                       enabled=True, now_seconds=900, accent=None, icon=item['icon'],
                                       holiday=key))
    return events


CATALOGUE_YEARS = 12  # editor shows last year plus the next ten


def catalogue(years=None):
    """JSON for the dashboard calendar: packs, holidays and calculated dates."""
    if years is None:
        now = dt.date.today().year
        years = range(now - 1, now - 1 + CATALOGUE_YEARS)
    return {pack: dict(name=data['name'], note=data['note'], holidays=[
        dict(key=f'{pack}.{item["id"]}', name=item['name'], icon=item['icon'],
             dates={str(year): [date.isoformat() for date in calculated_dates(f'{pack}.{item["id"]}', year)]
                    for year in years if calculated_dates(f'{pack}.{item["id"]}', year)})
        for item in data['holidays']]) for pack, data in PACKS.items()}
