"""Just enough astronomy for lunar and lunisolar calendars: where the Sun and Moon are, when the
Moon is new, when the Sun reaches a longitude, and the Hindu tithi. Pure arithmetic, offline.

Sun: Meeus, Astronomical Algorithms ch. 25 (about 0.01 degrees). Moon: Meeus ch. 47, the main
periodic terms (a few arc-seconds). Times are UTC datetimes; the theory runs on Terrestrial
Time, about 70 s ahead of UTC this century, which is far below what a calendar day needs.
"""
from __future__ import annotations

import datetime as dt
import math

UTC = dt.timezone.utc
J2000 = dt.datetime(2000, 1, 1, 12, tzinfo=UTC)
DELTA_T = dt.timedelta(seconds=70)

# Meeus table 47.A, longitude: multiples of D, M, M', F and the coefficient in 1e-6 degrees
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


def _t(when: dt.datetime) -> float:
    """Julian centuries of Terrestrial Time since J2000."""
    return ((when + DELTA_T) - J2000).total_seconds() / 86400 / 36525


def _nutation(t: float) -> float:
    return -0.00478 * math.sin(math.radians(125.04 - 1934.136 * t))


def sun_longitude(when: dt.datetime) -> float:
    """Apparent ecliptic longitude of the Sun, degrees [0, 360)."""
    t = _t(when)
    l0 = 280.46646 + 36000.76983 * t + 0.0003032 * t * t
    m = math.radians(357.52911 + 35999.05029 * t - 0.0001537 * t * t)
    c = ((1.914602 - 0.004817 * t - 0.000014 * t * t) * math.sin(m) + (0.019993 - 0.000101 * t) * math.sin(2 * m)
         + 0.000289 * math.sin(3 * m))
    return (l0 + c - 0.00569 + _nutation(t)) % 360


def moon_longitude(when: dt.datetime) -> float:
    """Apparent ecliptic longitude of the Moon, degrees [0, 360)."""
    t = _t(when)
    lp = 218.3164477 + 481267.88123421 * t - 0.0015786 * t ** 2 + t ** 3 / 538841 - t ** 4 / 65194000
    d = 297.8501921 + 445267.1114034 * t - 0.0018819 * t ** 2 + t ** 3 / 545868 - t ** 4 / 113065000
    m = 357.5291092 + 35999.0502909 * t - 0.0001536 * t ** 2 + t ** 3 / 24490000
    mp = 134.9633964 + 477198.8675055 * t + 0.0087414 * t ** 2 + t ** 3 / 69699 - t ** 4 / 14712000
    f = 93.2720950 + 483202.0175233 * t - 0.0036539 * t ** 2 - t ** 3 / 3526000 + t ** 4 / 863310000
    e = 1 - 0.002516 * t - 0.0000074 * t * t
    total = 0.0
    for kd, km, kmp, kf, coeff in _MOON:
        term = coeff * math.sin(math.radians(kd * d + km * m + kmp * mp + kf * f))
        total += term * (e ** abs(km))
    a1, a2 = 119.75 + 131.849 * t, 53.09 + 479264.290 * t
    total += 3958 * math.sin(math.radians(a1)) + 1962 * math.sin(math.radians(lp - f)) + 318 * math.sin(math.radians(a2))
    return (lp + total / 1e6 + _nutation(t)) % 360


def elongation(when: dt.datetime) -> float:
    """Moon minus Sun, degrees [0, 360): 0 at new moon, 180 at full."""
    return (moon_longitude(when) - sun_longitude(when)) % 360


def _crossing(angle, target: float, start: dt.datetime, rate: float) -> dt.datetime:
    """First time on or after ``start`` that ``angle`` (rising ``rate`` degrees a day) passes ``target``."""
    ahead = (target - angle(start)) % 360
    t = start + dt.timedelta(days=ahead / rate)
    for _ in range(8):  # Newton steps on the wrapped difference
        miss = (angle(t) - target + 180) % 360 - 180
        if abs(miss) < 1e-6:
            break
        t -= dt.timedelta(days=miss / rate)
    return t


def new_moon_after(start: dt.datetime) -> dt.datetime:
    return _crossing(elongation, 0.0, start, 12.19)


def elongation_after(target: float, start: dt.datetime) -> dt.datetime:
    """When the Moon is next ``target`` degrees ahead of the Sun (tithi boundaries are 12-degree steps)."""
    return _crossing(elongation, target, start, 12.19)


def sun_at(longitude: float, start: dt.datetime) -> dt.datetime:
    """When the Sun next reaches an apparent (tropical) longitude."""
    return _crossing(sun_longitude, longitude, start, 0.9856)


def ayanamsa(when: dt.datetime) -> float:
    """Lahiri (Chitrapaksha) ayanamsa, the offset of the sidereal zodiac used by Indian almanacs."""
    return 23.85306 + 0.0139697 * ((when - J2000).total_seconds() / 86400 / 365.25)


def sidereal_sun(when: dt.datetime) -> float:
    return (sun_longitude(when) - ayanamsa(when)) % 360


def sun_enters_sign(sign: int, start: dt.datetime) -> dt.datetime:
    """Sankranti: when the Sun next enters sidereal sign ``sign`` (0 = Aries ... 11 = Pisces)."""
    return _crossing(sidereal_sun, sign * 30.0, start, 0.9856)


def tithi(when: dt.datetime) -> int:
    """The lunar day in effect, 1-30: 1-15 the bright half (15 = full moon), 16-30 the dark (30 = new)."""
    return int(elongation(when) // 12) + 1


def sunrise_sunset(day: dt.date, lat: float, lon: float, tz: dt.tzinfo) -> tuple[dt.datetime, dt.datetime]:
    """Local sunrise and sunset (NOAA's simple formula, a minute or two out: plenty here)."""
    n = day.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eq = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g) - 0.014615 * math.cos(2 * g)
                   - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    lat_r = math.radians(lat)
    ha = math.degrees(math.acos(math.cos(math.radians(90.833)) / (math.cos(lat_r) * math.cos(decl))
                                - math.tan(lat_r) * math.tan(decl)))
    noon = dt.datetime.combine(day, dt.time(), tzinfo=UTC) + dt.timedelta(minutes=720 - 4 * lon - eq)
    rise, set_ = noon - dt.timedelta(minutes=4 * ha), noon + dt.timedelta(minutes=4 * ha)
    return rise.astimezone(tz), set_.astimezone(tz)
