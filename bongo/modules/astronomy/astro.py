"""Compact analytic astronomy: moon phase (Meeus 47/49, truncated) and sun (NOAA). Stdlib only.
Every function takes aware UTC datetimes (naive ones are treated as UTC)."""
from __future__ import annotations

import datetime as dt
from math import acos, asin, cos, degrees, floor, radians, sin, tan

SYNODIC = 29.530588853
UTC = dt.timezone.utc
_EPOCH = dt.datetime(2000, 1, 1, 12, tzinfo=UTC)  # JD 2451545.0
NAMES = ("New Moon", "Waxing Crescent", "First Quarter", "Waxing Gibbous",
         "Full Moon", "Waning Gibbous", "Last Quarter", "Waning Crescent")


def _utc(t: dt.datetime) -> dt.datetime:
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def _days(t: dt.datetime) -> float:
    return (_utc(t) - _EPOCH).total_seconds() / 86400  # days since J2000


def _sin(deg: float) -> float:
    return sin(radians(deg))


def moon_phase(t: dt.datetime) -> tuple[float, float, bool]:
    """(age in days 0..29.53, illuminated fraction 0..1, waxing)."""
    T = _days(t) / 36525
    D = 297.8501921 + 445267.1114034 * T - 0.0018819 * T * T
    M = 357.5291092 + 35999.0502909 * T
    Mp = 134.9633964 + 477198.8675055 * T + 0.0087414 * T * T
    e = (D + 6.289 * _sin(Mp) - 2.100 * _sin(M) + 1.274 * _sin(2 * D - Mp)
         + 0.658 * _sin(2 * D) + 0.214 * _sin(2 * Mp) + 0.110 * _sin(D)) % 360  # elongation
    return e / 360 * SYNODIC, (1 - cos(radians(e))) / 2, e < 180


# (new, full, power of E, dM, dM', dF) and (quarter, E, dM, dM', dF): Meeus table 49.A, truncated
_NF = ((-.40720, -.40614, 0, 0, 1, 0), (.17241, .17302, 1, 1, 0, 0), (.01608, .01614, 0, 0, 2, 0),
       (.01039, .01043, 0, 0, 0, 2), (.00739, .00734, 1, -1, 1, 0), (-.00514, -.00515, 1, 1, 1, 0),
       (.00208, .00209, 2, 2, 0, 0), (-.00111, -.00111, 0, 0, 1, -2), (-.00057, -.00057, 0, 0, 1, 2))
_Q = ((-.62801, 0, 0, 1, 0), (.17172, 1, 1, 0, 0), (-.01183, 1, 1, 1, 0), (.00862, 0, 0, 2, 0),
      (.00804, 0, 0, 0, 2), (.00454, 1, -1, 1, 0), (.00204, 2, 2, 0, 0), (-.0018, 0, 0, 1, -2),
      (-.0007, 0, 0, 1, 2))


def _phase_time(k: float) -> dt.datetime:
    """Moment of the phase with Meeus index k (integer = new, +.25 first q, +.5 full, +.75 last q)."""
    T = k / 1236.85
    jde = 2451550.09766 + SYNODIC * k + 0.00015437 * T * T
    E = 1 - 0.002516 * T - 0.0000074 * T * T
    M, Mp, F = 2.5534 + 29.1053567 * k, 201.5643 + 385.81693528 * k, 160.7108 + 390.67050284 * k
    kind = round(k % 1 * 4)
    s = 0.0
    if kind in (0, 2):
        for new, full, p, a, b, c in _NF:
            s += (new if kind == 0 else full) * E ** p * _sin(a * M + b * Mp + c * F)
    else:
        for co, p, a, b, c in _Q:
            s += co * E ** p * _sin(a * M + b * Mp + c * F)
        w = (0.00306 - 0.00038 * E * cos(radians(M)) + 0.00026 * cos(radians(Mp))
             - 0.00002 * cos(radians(Mp - M)) + 0.00002 * cos(radians(Mp + M)) + 0.00002 * cos(radians(2 * F)))
        s += w if kind == 1 else -w
    return _EPOCH + dt.timedelta(days=jde - 2451545.0 + s - 69 / 86400)  # JDE -> UT (dT ~ 69 s)


def next_phases(t: dt.datetime) -> dict[str, dt.datetime]:
    """The next new / first_quarter / full / last_quarter moments after ``t``."""
    t = _utc(t)
    k0 = floor(_days(t) / 36525 * 100 * 12.3685)
    out: dict[str, dt.datetime] = {}
    for i in range(-4, 12):
        k = k0 + i / 4
        when = _phase_time(k)
        name = ("new", "first_quarter", "full", "last_quarter")[round(k % 1 * 4) % 4]
        if when > t and name not in out:
            out[name] = when
    return dict(sorted(out.items(), key=lambda kv: kv[1]))


def phase_name(fraction: float, waxing: bool) -> str:
    if fraction < 0.02:
        return NAMES[0]
    if fraction > 0.98:
        return NAMES[4]
    if abs(fraction - 0.5) < 0.04:
        return NAMES[2] if waxing else NAMES[6]
    if fraction < 0.5:
        return NAMES[1] if waxing else NAMES[7]
    return NAMES[3] if waxing else NAMES[5]


def _sun(t: dt.datetime) -> tuple[float, float]:
    """(declination deg, equation of time minutes) - NOAA."""
    T = _days(t) / 36525
    L0 = (280.46646 + T * (36000.76983 + T * 0.0003032)) % 360
    M = 357.52911 + T * (35999.05029 - 0.0001537 * T)
    e = 0.016708634 - T * (0.000042037 + 0.0000001267 * T)
    C = (_sin(M) * (1.914602 - T * (0.004817 + 0.000014 * T)) + _sin(2 * M) * (0.019993 - 0.000101 * T)
         + _sin(3 * M) * 0.000289)
    om = 125.04 - 1934.136 * T
    lam = L0 + C - 0.00569 - 0.00478 * _sin(om)
    eps = 23.439291 - 0.0130042 * T + 0.00256 * cos(radians(om))
    decl = degrees(asin(_sin(eps) * _sin(lam)))
    y = tan(radians(eps / 2)) ** 2
    eq = 4 * degrees(y * _sin(2 * L0) - 2 * e * _sin(M) + 4 * e * y * _sin(M) * cos(radians(2 * L0))
                     - 0.5 * y * y * _sin(4 * L0) - 1.25 * e * e * _sin(2 * M))
    return decl, eq


def sun_times(date: dt.date, lat: float, lon: float):
    """(sunrise, sunset) as aware UTC datetimes for the solar day ``date`` at ``lon``; None in polar day/night."""
    midnight = dt.datetime(date.year, date.month, date.day, tzinfo=UTC)
    noon = midnight + dt.timedelta(minutes=720 - 4 * lon)
    rise = set_ = noon
    for _ in range(2):  # refine: evaluate declination at the estimated events
        d1, q1 = _sun(rise)
        d2, q2 = _sun(set_)
        cosh = [(cos(radians(90.833)) / (cos(radians(lat)) * cos(radians(d))) - tan(radians(lat)) * tan(radians(d)))
                for d in (d1, d2)]
        if abs(cosh[0]) > 1 or abs(cosh[1]) > 1:
            return None
        ha1, ha2 = degrees(acos(cosh[0])), degrees(acos(cosh[1]))
        rise = midnight + dt.timedelta(minutes=720 - 4 * (lon + ha1) - q1)
        set_ = midnight + dt.timedelta(minutes=720 - 4 * (lon - ha2) - q2)
    return rise, set_


def sun_heights(t: dt.datetime, lat: float, lon: float) -> tuple[float, float]:
    """(the sun's height above the horizon now, the highest it gets that day) in degrees, no refraction. The
    second is worked out from where the sun is now, so it glides through the year with no step at midnight."""
    t = _utc(t)
    decl, eq = _sun(t)
    minutes = t.hour * 60 + t.minute + t.second / 60 + t.microsecond / 6e7
    ha = (minutes + eq + 4 * lon) / 4 - 180
    s = _sin(lat) * _sin(decl) + cos(radians(lat)) * cos(radians(decl)) * cos(radians(ha))
    return degrees(asin(max(-1.0, min(1.0, s)))), 90 - abs(lat - decl)


def sun_elevation(t: dt.datetime, lat: float, lon: float) -> float:
    """Sun's height above the horizon in degrees (no refraction)."""
    return sun_heights(t, lat, lon)[0]
