"""Offline Moon calculations. No downloads, provider access or host registration.

Skyfield apparent geocentric ecliptic elongation defines major phases;
illumination is Sun-Moon-observer geometry, not a cosine-of-age estimate.
Rise/set uses a topocentric apparent upper limb with standard 34' refraction
and mean 16' lunar radius (level sea horizon; terrain/weather not modelled).
"""
import datetime as dt
import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from skyfield import almanac
from skyfield.api import load, load_file, wgs84

UTC = dt.timezone.utc
DATA = Path(__file__).resolve().parent / 'resources' / 'astronomy'
EPHEMERIS_SHA256 = 'c1c7feeab882263fc493a9d5a5b2ddd71b54826cdf65d8d17a76126b260a49f2'
PHASE_NAMES = ('New Moon', 'Waxing Crescent', 'First Quarter', 'Waxing Gibbous',
               'Full Moon', 'Waning Gibbous', 'Last Quarter', 'Waning Crescent')


class Cancelled(Exception):
    """Cooperative calculation cancellation; caller should yield the playlist."""


def check_cancel(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise Cancelled()


def local_day_bounds(day, timezone):
    """UTC instants enclosing a local civil date (not necessarily 24 hours)."""
    zone = ZoneInfo(timezone)
    return tuple(dt.datetime.combine(d, dt.time(), zone).astimezone(UTC)
                 for d in (day, day + dt.timedelta(days=1)))


def _search(ts, start, end, function, stop_event):
    def guarded(t):
        check_cancel(stop_event)
        result = function(t)
        check_cancel(stop_event)
        return result
    guarded.step_days = function.step_days
    result = almanac.find_discrete(ts.from_datetime(start), ts.from_datetime(end), guarded)
    check_cancel(stop_event)
    return result


@dataclass(frozen=True)
class Location:
    latitude: float
    longitude: float
    timezone: str
    label: str
    provenance: str

    def __post_init__(self):
        for value, limit in ((self.latitude, 90), (self.longitude, 180)):
            if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > limit:
                raise ValueError('Invalid location coordinates')
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
            raise ValueError('Explicit IANA timezone required') from exc
        if any(not isinstance(v, str) or not v.strip() or len(v) > 200
               for v in (self.label, self.provenance)):
            raise ValueError('Bounded place label and provenance required')


@dataclass(frozen=True)
class Phase:
    name: str
    time: dt.datetime


@dataclass(frozen=True)
class Moon:
    time: dt.datetime
    location: Location
    phase_degrees: float
    phase_name: str
    illumination: float
    rises: tuple
    sets: tuple
    horizon_state: str
    next_phase: Phase
    source: dict


class Astronomy:
    """Load one release-pinned file; callers own context-manager lifetime."""
    def __init__(self, path=DATA / 'de440s.bsp'):
        path = Path(path)
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('Ephemeris exceeds active-file budget')
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != EPHEMERIS_SHA256:
            raise ValueError('Ephemeris checksum mismatch')
        self.ephemeris = load_file(str(path))
        self.timescale = load.timescale(builtin=True)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.ephemeris.close()

    def moon(self, now, location, stop_event=None):
        check_cancel(stop_event)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('An aware instant is required')
        now = now.astimezone(UTC)
        if not 2000 <= now.year <= 2100:
            raise ValueError('Supported dates are 2000 through 2100')
        zone = ZoneInfo(location.timezone)
        local = now.astimezone(zone)
        start, end = local_day_bounds(local.date(), location.timezone)
        ts, eph = self.timescale, self.ephemeris
        t = ts.from_datetime(now)
        angle = float(almanac.moon_phase(eph, t).degrees)
        illumination = float(eph['earth'].at(t).observe(eph['moon']).apparent().fraction_illuminated(eph['sun']))
        above = almanac.risings_and_settings(eph, eph['moon'],
                                             wgs84.latlon(location.latitude, location.longitude),
                                             radius_degrees=16 / 60)
        # A finer sampling cadence also bounds each cancellation interval.
        above.step_days = 1 / 24
        times, states = _search(ts, start, end, above, stop_event)
        rises = tuple(t.astimezone(zone) for t, state in zip(times, states) if state)
        sets = tuple(t.astimezone(zone) for t, state in zip(times, states) if not state)
        state = 'crossings' if len(times) else ('always_above' if above(ts.from_datetime(start)) else 'always_below')
        times, phases = _search(ts, now, now + dt.timedelta(days=9), almanac.moon_phases(eph), stop_event)
        next_phase = Phase(almanac.MOON_PHASES[int(phases[0])], times[0].astimezone(zone))
        return Moon(local, location, angle, PHASE_NAMES[int((angle + 22.5) // 45) % 8],
                    illumination, rises, sets, state, next_phase,
                    {'ephemeris': 'DE440s', 'checksum_verified': True,
                     'sha256': EPHEMERIS_SHA256, 'skyfield': '1.54',
                     'supported_years': [2000, 2100],
                     'rise_set_accuracy': 'one-minute target; standard horizon, not a guarantee',
                     'timescale': 'Skyfield 1.54 bundled offline tables; future UTC/UT1 uncertain'})
