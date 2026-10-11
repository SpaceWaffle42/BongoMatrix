"""Application-owned RSS resources; no import-time network or worker startup."""
import copy
import json
from pathlib import Path
import threading
from datetime import datetime, timezone

from rss_config import DEFAULTS, render_options, validate
from rss_data import parse_feed
from rss_service import PreparedFeedService
from rss_cache import DiskFeedCache
from rss_transport import SafeFeedClient


class RSSHost:
    def __init__(self, cache_directory):
        self.service = PreparedFeedService(cache=DiskFeedCache(cache_directory))
        # One reusable draft lane per application, never one thread per request.
        # Share the same cache object/lock and global 50 MB ceiling.
        self.draft_service = PreparedFeedService(cache=self.service.cache)
        self._draft_settings = None
        self._settings = None
        self._lock = threading.RLock()
        self.presets = json.loads(Path(__file__).with_name('rss_presets.json').read_text())

    def sync(self, config):
        settings = config.get('rss', DEFAULTS)
        validate(settings)
        enabled = any(row.get('module') == 'rss' and row.get('enabled') for row in config['playlist'])
        with self._lock:
            if settings != self._settings:
                self.service.configure(settings['feeds'], include=settings['include'],
                    exclude=settings['exclude'], count=settings['count'], options=render_options(settings))
                self._settings = copy.deepcopy(settings)
            if enabled:
                self.service.start()
            elif self.service.running:
                self.service.close()

    def close(self):
        with self._lock:
            self.service.close()
            self.draft_service.close()

    def preview(self, settings):
        validate(settings)
        with self._lock:
            snapshot = self.service.peek()
            if settings == self._settings and snapshot.items:
                return snapshot
            if settings != self._draft_settings:
                self.draft_service.configure(settings['feeds'], include=settings['include'],
                    exclude=settings['exclude'], count=settings['count'], options=render_options(settings))
                self._draft_settings = copy.deepcopy(settings)
            self.draft_service.start()
            return self.draft_service.peek()

    def status(self):
        snapshot = self.service.peek()
        return {'running': self.service.running, 'generation': snapshot.generation,
                'duration_seconds': snapshot.duration, 'ready_count': len(snapshot.items),
                'explanation': '' if snapshot.items else 'No prepared eligible stories; module will skip. Test feeds or check filters.',
                'feeds': [{'url': s.url, 'title': s.feed.title if s.feed else '',
                           'status': s.status, 'last_success': s.last_success,
                           'warning': s.warning} for s in self.service.snapshots()],
                'presets': self.presets}

    def test_feed(self, url):
        feed = parse_feed(SafeFeedClient().fetch(url), url)
        newest = max((story.published for story in feed.stories if story.published is not None), default=None)
        return {'title': feed.title, 'count': len(feed.stories),
                'newest': datetime.fromtimestamp(newest, timezone.utc).isoformat() if newest is not None else None,
                'image_available': any(story.media for story in feed.stories),
                'image_availability_kind': 'feed-hints-not-decoded', 'warnings': list(feed.warnings)}

    def reset(self):
        with self._lock:
            # Invalidate both writers before clearing their shared cache. Never
            # join a network worker on this route: reset wakes the physical lane
            # with its saved sources, and an idle disabled lane stays stopped.
            with self.service._cache_gate, self.draft_service._cache_gate:
                self.draft_service.configure(())
                self.service.reset()
                self.draft_service.reset()
                self._draft_settings = None
        return {'defaults': copy.deepcopy(DEFAULTS), 'saved': False, 'cache_cleared': True}
