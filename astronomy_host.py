"""Two bounded application-owned preparation lanes; never calculate in renderers."""
import copy
import datetime as dt
import json
import threading
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo
from astronomy_config import DEFAULTS, location_values, validate


class PreparationBusy(RuntimeError):
    """A different live consumer owns the bounded preparation slot."""


@dataclass(frozen=True)
class Snapshot:
    generation: int = 0
    status: str = 'disabled'
    cards: tuple = ()
    location: tuple = ()
    prepared_at: dt.datetime | None = None
    duration: float = 0
    error: str = ''
    refresh_at: dt.datetime | None = None


def _refresh_at(cards, location, now):
    """Bound ready/empty reuse; compute civil boundaries on the UTC timeline."""
    zone = ZoneInfo(location[2])
    tomorrow = now.astimezone(zone).date() + dt.timedelta(days=1)
    deadlines = [now + dt.timedelta(minutes=5),
                 dt.datetime.combine(tomorrow, dt.time(), zone).astimezone(dt.timezone.utc)]
    for card in cards:
        if getattr(card, 'refresh_at', None) is not None:
            deadlines.append(card.refresh_at)
        event = getattr(card, 'event', None)
        if event is not None:
            deadlines.extend(t for window in event.windows for t in (window.start, window.end) if t > now)
            if event.time > now:
                deadlines.append(event.time)
    return min(deadlines)


def _prepare(settings, location, now, stop):
    # All kernel/checksum/font I/O and numerical work lives on the lane worker.
    from astronomy import Astronomy, Location, check_cancel
    from modules.astronomy_renderer import MoonOptions, EventOptions, prepare_pass
    check_cancel(stop)
    common = {key: settings[key] for key in ('title_font', 'detail_font', 'frame_delay', 'background', 'text_colour')}
    moon = MoonOptions(**common, hold_seconds=settings['moon_hold_seconds'], moon_colour=settings['moon_colour'])
    events = EventOptions(**common, hold_seconds=settings['event_hold_seconds'], accent=settings['accent'], countdown_font=settings['countdown_font'])
    with Astronomy() as engine:
        check_cancel(stop)
        return prepare_pass(engine, now, Location(*location), current_moon=settings['current_moon'],
            lunar_events=settings['lunar_events'], horizon_days=settings['horizon_days'],
            event_limit=settings['event_limit'], options=moon, event_options=events, stop_event=stop)


class PreparationLane:
    """One reusable worker, one replaceable job; old generations cannot publish."""
    def __init__(self, preparer, utc_clock):
        self._prepare, self._clock = preparer, utc_clock
        self._condition = threading.Condition()
        self._thread = None
        self._closed = False
        self._busy = False
        self._job = None
        self._key = None
        self._cancel = threading.Event()
        self._snapshot = Snapshot()
        self._owners = {}

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def peek(self):
        with self._condition:
            return self._snapshot

    def fresh(self):
        with self._condition:
            snapshot = self._snapshot
            return (snapshot.prepared_at is not None and snapshot.refresh_at is not None
                    and snapshot.prepared_at <= self._clock() < snapshot.refresh_at)

    def request(self, settings, location, enabled=True, force=False, owner=None, waiting=None):
        key = (json.dumps(settings, sort_keys=True), location, enabled)
        claim = (owner, waiting)
        with self._condition:
            if key == self._key and self._snapshot.status in ('ready', 'empty') and not self.fresh():
                force = True
            now = time.monotonic()
            self._owners = {o: expiry for o, expiry in self._owners.items() if expiry > now}
            if key != self._key or force:
                if any(o[0] != owner for o in self._owners):
                    raise PreparationBusy('Astronomy preparation is busy.')
                self._owners.clear()
            if owner is not None:
                if claim not in self._owners and len(self._owners) >= 64:
                    raise PreparationBusy('Astronomy preparation is busy.')
                self._owners[claim] = float('inf') if waiting is not None else now + 30
            if self._closed or (key == self._key and not force):
                if waiting is None and self._snapshot.status != 'preparing':
                    self._owners.pop(claim, None)
                return self._snapshot
            self._key = key
            self._cancel.set()
            self._cancel = threading.Event()
            generation = self._snapshot.generation + 1
            active = enabled and (settings['current_moon'] or settings['lunar_events'])
            status = 'preparing' if active and location else 'location_required' if active else 'disabled'
            self._snapshot = Snapshot(generation=generation, status=status, location=location or ())
            if waiting is None and status != 'preparing':
                self._owners.pop(claim, None)
            self._job = (generation, copy.deepcopy(settings), location, self._cancel) if status == 'preparing' else None
            if self._job and not self.running:
                self._thread = threading.Thread(target=self._work, name='astronomy-prepare', daemon=True)
                self._thread.start()
            self._condition.notify_all()
            return self._snapshot

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._job is not None)
                if self._closed:
                    return
                generation, settings, location, stop = self._job
                self._job = None
                self._busy = True
            now = self._clock()
            try:
                cards = self._prepare(settings, location, now, stop)
                from modules.astronomy_renderer import duration_seconds
                snapshot = Snapshot(generation, 'ready' if cards else 'empty', cards,
                                    location, now, duration_seconds(cards, now=now),
                                    refresh_at=_refresh_at(cards, location, now))
            except Exception:
                # No exception internals or filesystem paths exposed to clients.
                snapshot = Snapshot(generation=generation, status='error', location=location,
                                    error='Offline preparation failed; check bundled data and supported date range.')
            with self._condition:
                if not self._closed and not stop.is_set() and generation == self._snapshot.generation:
                    self._snapshot = snapshot
                self._busy = False
                self._condition.notify_all()

    def wait(self, stop_event, timeout=30, generation=None, owner=None):
        try:
            return self._wait(stop_event, timeout, generation)
        finally:
            with self._condition:
                if generation is None or generation == self._snapshot.generation:
                    self._owners.pop(owner, None)
                    if not self._owners and self._snapshot.status == 'preparing':
                        self._cancel.set()
                        self._key = None
                        self._job = None
                        self._snapshot = Snapshot(generation=self._snapshot.generation + 1)
                        self._condition.notify_all()

    def _wait(self, stop_event, timeout, generation):
        deadline = time.monotonic() + timeout
        with self._condition:
            generation = self._snapshot.generation if generation is None else generation
            while not stop_event.is_set() and not self._closed:
                if self._snapshot.generation != generation:
                    return None
                if self._snapshot.status != 'preparing':
                    return self._snapshot
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(min(.05, remaining))
            return None

    def wait_idle(self, timeout):
        with self._condition:
            return self._condition.wait_for(lambda: not self._busy and self._job is None, timeout)

    def close(self):
        with self._condition:
            self._closed = True
            self._cancel.set()
            self._job = None
            self._snapshot = Snapshot(generation=self._snapshot.generation + 1)
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=2)


class AstronomyHost:
    def __init__(self, *, preparer=_prepare, utc_clock=None):
        clock = utc_clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.saved = PreparationLane(preparer, clock)
        self.draft = PreparationLane(preparer, clock)
        self._lock = threading.RLock()
        self._revision = -1
        self._saved_key = None

    def _inputs(self, config):
        settings = config.get('astronomy', DEFAULTS)
        validate(settings)
        location = location_values(dict(config, astronomy=settings))
        return settings, location

    def sync(self, config):
        settings, location = self._inputs(config)
        with self._lock:
            if config['revision'] < self._revision:
                return
            self._revision = config['revision']
            enabled = any(r['module'] == 'astronomy' and r['enabled'] for r in config['playlist'])
            self._saved_key = (copy.deepcopy(settings), location)
            self.saved.request(settings, location, enabled)

    def prepare_saved_pass(self, config, stop_event):
        settings, location = self._inputs(config)
        with self._lock:
            if config['revision'] < self._revision:
                return None
            self.sync(config)
            initial = self.saved.request(settings, location, force=True)
        return self.saved.wait(stop_event, generation=initial.generation)

    def _preview_lane(self, config, owner=None, waiting=None):
        settings, location = self._inputs(config)
        with self._lock:
            if self._saved_key == (settings, location) and self.saved.fresh():
                return self.saved
            self.draft.request(settings, location, owner=owner, waiting=waiting)
            return self.draft

    def preview(self, config, owner=None):
        with self._lock:
            return self._preview_lane(config, owner).peek()

    def wait_preview(self, config, stop_event, owner=None):
        owner = stop_event if owner is None else owner
        with self._lock:
            lane = self._preview_lane(config, owner, waiting=stop_event)
            if lane is self.saved:
                return None if stop_event.is_set() else lane.peek()
            generation = lane.peek().generation
        return lane.wait(stop_event, generation=generation, owner=(owner, stop_event))

    def close(self):
        self.saved.close()
        self.draft.close()
