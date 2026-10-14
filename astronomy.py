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


def _utc(value):
    if not isinstance(value, dt.datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('An aware instant is required')
    return value.astimezone(UTC)


@dataclass(frozen=True, order=True)
class VisibilityWindow:
    """End-exclusive continuous local eligibility, always stored on UTC timeline."""
    start: dt.datetime
    end: dt.datetime

    def __post_init__(self):
        object.__setattr__(self, 'start', _utc(self.start))
        object.__setattr__(self, 'end', _utc(self.end))
        if self.end - self.start < dt.timedelta(minutes=10):
            raise ValueError('At least ten continuous minutes of visibility required')


@dataclass(frozen=True)
class AstronomyEvent:
    """Internal normalized record; adapters must supply a shared canonical identity.

    `time` is the phenomenon's instant, not an invented duration. Windows are
    separately calculated opportunities to observe its object on the local date.
    No provider trust, label mapping or cache policy is implied by this record.
    """
    identity: str
    kind: str
    title: str
    time: dt.datetime
    location: Location
    windows: tuple
    source: str

    def __post_init__(self):
        for value in (self.identity, self.kind, self.title, self.source):
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ValueError('Bounded event identity, type, title and source required')
        object.__setattr__(self, 'time', _utc(self.time))
        if not isinstance(self.location, Location):
            raise ValueError('Location eligibility required')
        if (type(self.windows) is not tuple or not self.windows or len(self.windows) > 48
                or any(not isinstance(w, VisibilityWindow) for w in self.windows)):
            raise ValueError('Immutable bounded visibility windows required')
        object.__setattr__(self, 'windows', tuple(sorted(set(self.windows))))


def event_horizon(now, location, days=90):
    """Exclusive midnight after N local civil dates, including today's date."""
    if type(days) is not int or not 1 <= days <= 366:
        raise ValueError('Horizon must be 1 through 366 local dates')
    day = _utc(now).astimezone(ZoneInfo(location.timezone)).date()
    return local_day_bounds(day + dt.timedelta(days=days), location.timezone)[0]


def event_eligible(event, now):
    return any(w.end > _utc(now) for w in event.windows)


def select_events(events, now, location, *, horizon_days=90, limit=5):
    """Nearest eligible records on today's/future local dates; no rotation cursor.

    Exact canonical identities deduplicate deterministically, preferring the
    earlier instant then lexical source identity. Adapters must normalize identities first;
    unrelated events are never merged merely because their dates are close.
    """
    now = _utc(now)
    start = local_day_bounds(now.astimezone(ZoneInfo(location.timezone)).date(), location.timezone)[0]
    end = event_horizon(now, location, horizon_days)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('Event card limit must be 1 through 50')
    candidates = (e for e in events if e.location == location and start <= e.time < end
                  and event_eligible(e, now))
    ordered = sorted(candidates, key=lambda e: (e.time, e.identity, e.source, e.kind, e.title, e.windows))
    unique = {}
    for event in ordered:
        unique.setdefault(event.identity, event)
    return tuple(unique.values())[:limit]


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

    def moon_visibility(self, day, location, stop_event=None):
        """Moon >10 degrees and Sun <-18 degrees on a local civil date.

        Apparent topocentric centres, no atmospheric/terrain corrections. Five
        minute initial samples resolve the ten-minute minimum, then Skyfield
        refines crossings. Grazing contacts still need independent validation.
        """
        check_cancel(stop_event)
        start, end = local_day_bounds(day, location.timezone)
        if not 2000 <= day.year <= 2100:
            raise ValueError('Supported dates are 2000 through 2100')
        observer = self.ephemeris['earth'] + wgs84.latlon(location.latitude, location.longitude)
        def visible(t):
            at = observer.at(t)
            moon = at.observe(self.ephemeris['moon']).apparent().altaz()[0].degrees
            sun = at.observe(self.ephemeris['sun']).apparent().altaz()[0].degrees
            return (moon > 10) & (sun < -18)
        visible.step_days = 5 / (24 * 60)
        times, states = _search(self.timescale, start, end, visible, stop_event)
        beginning = start if visible(self.timescale.from_datetime(start)) else None
        windows = []
        for time, state in zip(times, states):
            check_cancel(stop_event)
            moment = time.utc_datetime()
            if state:
                beginning = moment
            elif beginning is not None:
                if moment - beginning >= dt.timedelta(minutes=10):
                    windows.append(VisibilityWindow(beginning, moment))
                beginning = None
        if beginning is not None and end - beginning >= dt.timedelta(minutes=10):
            windows.append(VisibilityWindow(beginning, end))
        check_cancel(stop_event)
        return tuple(windows)

    def lunar_events(self, now, location, *, horizon_days=90, stop_event=None):
        """Local new/full phase candidates filtered by local-date observability.

        Quarter phases belong to current-Moon's next-phase detail, not the
        roadmap's lunar event list. New Moon normally fails darkness/altitude;
        never relax visibility to manufacture an observable event.
        """
        check_cancel(stop_event)
        now = _utc(now)
        zone = ZoneInfo(location.timezone)
        start = local_day_bounds(now.astimezone(zone).date(), location.timezone)[0]
        end = event_horizon(now, location, horizon_days)
        if not 2000 <= now.year <= 2100 or end > dt.datetime(2101, 1, 1, tzinfo=UTC):
            raise ValueError('Supported dates are 2000 through 2100')
        events = []
        # Bound each numerical search, and check cancellation between chunks.
        while start < end:
            check_cancel(stop_event)
            until = min(end, start + dt.timedelta(days=30))
            times, phases = _search(self.timescale, start, until,
                                    almanac.moon_phases(self.ephemeris), stop_event)
            for time, phase in zip(times, phases):
                check_cancel(stop_event)
                moment = time.utc_datetime()
                if phase not in (0, 2) or not start <= moment < until:
                    continue
                windows = self.moon_visibility(moment.astimezone(zone).date(), location, stop_event)
                if windows:
                    kind = 'new_moon' if phase == 0 else 'full_moon'
                    events.append(AstronomyEvent(
                        f'moon:{kind}:{moment.date().isoformat()}', kind,
                        almanac.MOON_PHASES[int(phase)], moment, location, windows,
                        'NASA/JPL DE440s / Skyfield 1.54; offline bundled time tables'))
            start = until
        check_cancel(stop_event)
        return select_events(events, now, location, horizon_days=horizon_days, limit=50)

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
