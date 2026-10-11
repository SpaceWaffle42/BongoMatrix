"""Opt-in feed worker/data service. Not registered with any renderer or scheduler.

Call start/close from the eventual host lifecycle, snapshots/select from consumers.
refresh_due is synchronous for worker/testing use only, never a rendering callback.
"""
from dataclasses import dataclass, replace
import threading
import time
from copy import copy
from rss_data import Feed, Rotation, filter_words, parse_feed
from rss_transport import SafeFeedClient, checked_url

REFRESH_SECONDS = 1800
EXPIRY_SECONDS = 86400
MAX_FEEDS = 20


@dataclass(frozen=True)
class FeedSnapshot:
    url: str
    feed: Feed | None = None
    last_success: float | None = None
    status: str = 'empty'
    warning: str = ''


class FeedService:
    def __init__(self, feeds=(), *, client=None, cache=None, clock=time.time):
        self.client = client if client is not None else SafeFeedClient()
        self.cache = cache
        self.clock = clock
        self._lock = threading.RLock()
        self._cache_gate = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._stop, self._wake = threading.Event(), threading.Event()
        self._thread = None
        self._generation = 0
        self._states, self._due = {}, {}
        self._rotation = Rotation()
        self.configure(feeds)

    def configure(self, feeds, *, include=(), exclude=(), count=5):
        if not isinstance(feeds, (list, tuple)) or len(feeds) > MAX_FEEDS:
            raise ValueError('At most 20 feed URLs required')
        for url in feeds:
            checked_url(url)
        include, exclude = filter_words(include), filter_words(exclude)
        if type(count) is not int or not 1 <= count <= 50:
            raise ValueError('Story count must be 1..50')
        feeds = tuple(dict.fromkeys(feeds))
        restored = {}
        if self.cache is not None:
            for url in feeds:
                try:
                    record = self.cache.get(url)
                    if record is not None:
                        feed, saved = record
                        restored[url] = FeedSnapshot(url, feed, saved, 'stale', 'Showing cache; awaiting refresh')
                except (OSError, ValueError):
                    pass
        with self._lock:
            self._generation += 1
            if (getattr(self, 'feeds', None), getattr(self, 'include', None), getattr(self, 'exclude', None)) != (feeds, include, exclude):
                self._rotation = Rotation()
            self.feeds, self.include, self.exclude, self.count = feeds, include, exclude, count
            self._states = {url: self._states.get(url, restored.get(url, FeedSnapshot(url))) for url in feeds}
            self._due = {url: self._due.get(url, 0) for url in feeds}
            self._wake.set()

    def snapshots(self):
        with self._lock:
            now = self.clock()
            result = []
            for state in self._states.values():
                if state.last_success is not None and (now - state.last_success >= EXPIRY_SECONDS or now < state.last_success):
                    state = replace(state, feed=None, status='expired', warning='Cache expired; module will skip')
                result.append(state)
            return tuple(result)

    def select(self):
        with self._lock:
            return self._rotation.select(tuple(s.feed.stories if s.feed else () for s in self.snapshots()),
                                         count=self.count, include=self.include, exclude=self.exclude, material=self.feeds)

    def peek_stories(self):
        """Inspect the next normalized pass without consuming physical rotation."""
        with self._lock:
            rotation = copy(self._rotation)
            rotation.seen = self._rotation.seen.copy()
            return rotation.select(tuple(s.feed.stories if s.feed else () for s in self.snapshots()),
                                   count=self.count, include=self.include, exclude=self.exclude, material=self.feeds)

    def refresh_due(self):
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                generation = self._generation
                urls = tuple(url for url in self.feeds if self._due[url] <= self.clock())
            for url in urls:
                if self._stop.is_set():
                    break
                try:
                    feed = parse_feed(self.client.fetch(url), url)
                    updated = FeedSnapshot(url, feed, self.clock(), 'fresh')
                except Exception:
                    # Each untrusted source is isolated; never leak raw URL/error details.
                    with self._lock:
                        old = self._states.get(url, FeedSnapshot(url))
                    updated = replace(old, status='stale' if old.feed else 'error',
                                      warning='Could not update feed; showing last successful cache' if old.feed else 'Feed rejected or unavailable')
                with self._cache_gate:
                    with self._lock:
                        if generation != self._generation or self._stop.is_set():
                            break
                    if updated.status == 'fresh' and self.cache is not None:
                        try:
                            if not self.cache.put(url, updated.feed, updated.last_success):
                                updated = replace(updated, warning='Disk cache capacity reached; using memory')
                        except (OSError, ValueError):
                            updated = replace(updated, warning='Disk cache unavailable; using memory')
                    with self._lock:
                        if generation != self._generation or self._stop.is_set():
                            break
                        self._states[url] = updated
                        self._due[url] = self.clock() + REFRESH_SECONDS
        finally:
            self._refresh_lock.release()

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            def run():
                while not self._stop.is_set():
                    self._wake.clear()
                    self.refresh_due()
                    with self._lock:
                        delay = max(0.01, min(self._due.values(), default=self.clock() + REFRESH_SECONDS) - self.clock())
                    self._wake.wait(min(delay, REFRESH_SECONDS))
            self._thread = threading.Thread(target=run, name='rss-refresh', daemon=True)
            self._thread.start()

    def close(self, timeout=12):
        with self._lock:
            self._generation += 1
            self._stop.set()
            self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self.running:
                raise TimeoutError('Feed worker did not stop within budget')


@dataclass(frozen=True)
class PreparedItem:
    source: str
    story: object
    last_success: float
    prepared: object
    warning: str = ''


@dataclass(frozen=True)
class PreparedSnapshot:
    generation: int
    items: tuple = ()

    @property
    def stories(self):
        return tuple(item.prepared for item in self.items)

    @property
    def duration(self):
        from modules.rss_renderer import duration_seconds
        return duration_seconds(self.stories)


class PreparedFeedService(FeedService):
    """Explicit worker lifecycle; peek/admit are prepared-only consumer APIs.

    Only admit consumes rotation. A consumed pass becomes unavailable until the
    worker prepares the next one. At most count stories and one media hint per
    story are prepared, never every story in every feed. No import-time work.
    """
    def __init__(self, feeds=(), *, options=None, count=5, **kwargs):
        from rss_config import RenderOptions
        from rss_thumbnails import ThumbnailPreparer
        if options is not None and not isinstance(options, RenderOptions):
            raise ValueError('Expected validated RSS render options')
        self.options = options if options is not None else RenderOptions()
        self._preparer = ThumbnailPreparer()
        self._prepare_lock = threading.Lock()
        self._ready = ()
        self._cancel = threading.Event()
        super().__init__(feeds, **kwargs)
        self.configure(feeds, count=count)

    def configure(self, feeds, *, include=(), exclude=(), count=5, options=None):
        from rss_config import RenderOptions
        if options is not None and not isinstance(options, RenderOptions):
            raise ValueError('Expected validated RSS render options')
        with self._cache_gate:
            super().configure(feeds, include=include, exclude=exclude, count=count)
            with self._lock:
                if options is not None:
                    self.options = options
                self._cancel.set()
                self._cancel = threading.Event()
                self._ready = ()

    def reset(self):
        """Clear RSS cache/seen while retaining configuration; host owns defaults."""
        with self._cache_gate:
            with self._lock:
                self._generation += 1
                self._cancel.set()
                self._cancel = threading.Event()
                self._ready = ()
                self._rotation = Rotation()
                self._states = {url: FeedSnapshot(url) for url in self.feeds}
                self._due = dict.fromkeys(self.feeds, 0)
            if self.cache is not None:
                self.cache.clear()
            self._wake.set()

    def close(self, timeout=12):
        with self._lock:
            self._cancel.set()
            self._ready = ()
        super().close(timeout)

    def start(self):
        with self._lock:
            if self.running:
                return
            self._generation += 1
            self._cancel.set()
            self._cancel = threading.Event()
            self._rotation = Rotation()
            self._ready = ()
            super().start()

    def peek(self):
        with self._lock:
            now = self.clock()
            return PreparedSnapshot(self._generation, tuple(item for item in self._ready
                if 0 <= now - item.last_success < EXPIRY_SECONDS))

    def admit(self):
        with self._lock:
            result = self.peek()
            if result.items:
                self._rotation.material = (self.feeds, self.include, self.exclude)
                self._rotation.seen.intersection_update(key for state in self.snapshots() if state.feed
                    for story in state.feed.stories for key in story.keys)
                self._rotation.seen.update(key for item in result.items for key in item.story.keys)
                self._cancel.set()
                self._cancel = threading.Event()
                self._ready = ()
                self._wake.set()
            return result

    def refresh_due(self):
        from modules.rss_renderer import prepare_story
        if not self._prepare_lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                generation, cancel = self._generation, self._cancel
            super().refresh_due()
            with self._lock:
                if generation != self._generation or cancel.is_set() or self._stop.is_set():
                    return
                options = self.options
                selected = self.peek_stories()
                sources = self.snapshots()
                selected_sources = tuple(next(s for s in sources if s.feed and story in s.feed.stories)
                                         for story in selected)
                if (tuple(item.story for item in self._ready) == selected and
                        tuple(item.last_success for item in self._ready) == tuple(s.last_success for s in selected_sources)):
                    return
            items = []
            for story, source in zip(selected, selected_sources):
                if self._stop.is_set() or cancel.is_set():
                    break
                thumbnail, warning = None, ''
                if story.media:
                    url = story.media[0]
                    try:
                        cached = self.cache.get_thumbnail(url) if self.cache else None
                        if cached and 0 <= self.clock() - cached[1] < EXPIRY_SECONDS:
                            thumbnail = cached[0]
                        else:
                            result = self._preparer.prepare(url, cancel)
                            thumbnail, warning = result.thumbnail, result.warning
                            with self._cache_gate:
                                with self._lock:
                                    if generation != self._generation or cancel.is_set():
                                        return
                                if thumbnail and self.cache:
                                    self.cache.put_thumbnail(url, thumbnail, self.clock())
                    except Exception:
                        warning = 'Thumbnail unavailable; using text only'
                try:
                    prepared = prepare_story(story, options, thumbnail=thumbnail)
                except Exception:
                    with self._lock:
                        if generation == self._generation and not cancel.is_set():
                            state = self._states[source.url]
                            self._states[source.url] = replace(state, warning='Could not prepare story; module may skip')
                    continue
                items.append(PreparedItem(source.url, story, source.last_success, prepared, warning))
            with self._lock:
                if generation == self._generation and not self._stop.is_set() and not cancel.is_set():
                    self._ready = tuple(items)
        finally:
            self._prepare_lock.release()