import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch
import threading
import time
import struct
import json

from rss_cache import DiskFeedCache
from rss_data import Feed
from modules.rss_renderer import PreparedThumbnail


class UnifiedCacheTests(unittest.TestCase):
    def test_thumbnail_write_rejects_nonprepared_before_traversing_payload(self):
        class Untrusted:
            @property
            def frames(self):
                raise AssertionError('unbounded payload traversed')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                DiskFeedCache(directory).put_thumbnail('x', Untrusted(), 100)

    def test_shared_byte_ceiling_includes_temporary_and_preserves_large_old_entry(self):
        import os
        tile = PreparedThumbnail((bytes(768),), (100,))
        with tempfile.TemporaryDirectory() as directory:
            cache = DiskFeedCache(directory, max_bytes=1700)
            cache.put_thumbnail('one', tile, 10)
            cache.put_thumbnail('two', tile, 10)
            original = os.replace
            totals = []
            def observe(source, dest):
                totals.append(sum(p.stat().st_size for p in Path(directory).iterdir()))
                return original(source, dest)
            with patch('rss_cache.os.replace', side_effect=observe):
                cache.put('feed', Feed('x' * 200, ()), 10)
                cache.put_thumbnail('two', tile, 11)
            self.assertTrue(totals)
            self.assertLessEqual(max(totals), 1700)
            self.assertIsNone(cache.get_thumbnail('one'))
            self.assertEqual(cache.get_thumbnail('two'), (tile, 11))
        with tempfile.TemporaryDirectory() as directory:
            cache = DiskFeedCache(directory, max_bytes=1000)
            cache.put_thumbnail('one', tile, 10)
            self.assertFalse(cache.put_thumbnail('one', tile, 11))
            self.assertEqual(cache.get_thumbnail('one'), (tile, 10))

    def test_corrupt_wire_and_write_boundary_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = DiskFeedCache(directory)
            tile = PreparedThumbnail((bytes(768),), (Fraction(100, 3),))
            cache.put_thumbnail('a', tile, 10)
            path = next(Path(directory).glob('*.thumb'))
            for wire in (b'RSSTH001' + struct.pack('!dII', 10, 100, 0) + bytes(768),
                         b'RSSTH001' + struct.pack('!dII', 10, 60001, 1) + bytes(768),
                         b'RSSTH001' + struct.pack('!d', float('nan')) + bytes(776),
                         b'RSSTH001' + struct.pack('!d', 10) + bytes(776 * 301)):
                path.write_bytes(wire)
                self.assertIsNone(cache.get_thumbnail('a'))
            cache.put('feed', Feed('title', ()), 10)
            path = next(Path(directory).glob('*.json'))
            payload = json.loads(path.read_bytes())
            payload['saved'] = 10 ** 1000
            path.write_text(json.dumps(payload))
            self.assertIsNone(cache.get('feed'), 'corrupt numeric record must not escape')
            class Untrusted:
                frames = (bytes(768),) * 301
                durations_ms = (100,) * 301
            with self.assertRaises(ValueError):
                cache.put_thumbnail('bad', Untrusted(), 10)

    def test_combined_lru_reservation_corruption_and_scoped_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = DiskFeedCache(directory, max_bytes=2000, max_entries=2)
            self.assertTrue(hasattr(cache, 'put_thumbnail'), 'unified thumbnail storage absent')
            tile = PreparedThumbnail((bytes(768),), (Fraction(100, 3),))
            feed = Feed('News', ())
            cache.put('feed', feed, 100)
            self.assertTrue(cache.put_thumbnail('image', tile, 100))
            self.assertEqual(cache.get_thumbnail('image'), (tile, 100))
            cache.put('second', feed, 100)
            self.assertIsNone(cache.get('feed'))
            self.assertEqual(cache.get_thumbnail('image'), (tile, 100))
            # Old + temporary payload must fit, without deleting old success.
            with patch('rss_cache.os.replace', side_effect=OSError('disk')):
                with self.assertRaises(OSError):
                    cache.put_thumbnail('image', tile, 101)
            self.assertEqual(cache.get_thumbnail('image'), (tile, 100))
            self.assertLessEqual(sum(p.stat().st_size for p in Path(directory).iterdir()), 2000)
            self.assertFalse(list(Path(directory).glob('*.tmp')))
            for p in Path(directory).glob('*.thumb'):
                p.write_bytes(b'corrupt')
            self.assertIsNone(cache.get_thumbnail('image'))
            untouched = Path(directory) / 'unrelated.txt'
            untouched.write_text('keep')
            cache.clear()
            self.assertIsNone(cache.get('second'))
            self.assertEqual(untouched.read_text(), 'keep')


class PreparedServiceTests(unittest.TestCase):
    def test_prepared_admit_is_physical_admission_and_seen_stays_bounded(self):
        from rss_service import PreparedFeedService, PreparedSnapshot
        now = [1000.]
        class Client:
            def fetch(self, url):
                return f'<rss><channel><item><guid>{now[0]}</guid><title>story</title></item></channel></rss>'.encode()
        service = PreparedFeedService(('https://example.com/feed',), client=Client(), clock=lambda: now[0])
        for _ in range(3):
            service.refresh_due()
            selected = service.admit()
            self.assertIsInstance(selected, PreparedSnapshot)
            self.assertEqual(service.peek().items, ())
            self.assertLessEqual(len(service._rotation.seen), 1)
            now[0] += 1800
        service.close()

    def test_preparation_failure_isolated_and_invalid_options_rejected(self):
        from rss_service import PreparedFeedService
        from modules.rss_renderer import prepare_story
        class Client:
            def fetch(self, url):
                return b'<rss><channel><item><title>one</title></item><item><title>two</title></item></channel></rss>'
        service = PreparedFeedService(('https://example.com/feed',), client=Client())
        def failing(story, *args, **kwargs):
            if story.title == 'one':
                raise OSError('font disappeared')
            return prepare_story(story, *args, **kwargs)
        with patch('modules.rss_renderer.prepare_story', side_effect=failing):
            service.refresh_due()
        self.assertEqual([i.story.title for i in service.peek().items], ['two'])
        self.assertIn('prepar', service.snapshots()[0].warning.lower())
        with self.assertRaises(ValueError):
            service.configure(service.feeds, options='invalid')
        service.close()

    def test_worker_close_cancels_preparation_and_prepares_only_current_pass(self):
        from rss_service import PreparedFeedService
        from modules.rss_renderer import prepare_story
        entered, release = threading.Event(), threading.Event()
        calls = []
        class Client:
            def fetch(self, url):
                return ('<rss><channel>' + ''.join(f'<item><guid>{i}</guid><title>{i}</title><enclosure type="image/png" url="https://example.com/{i}.png"/></item>' for i in range(200)) + '</channel></rss>').encode()
        def blocked(story, *args, **kwargs):
            calls.append(story.title)
            entered.set()
            release.wait(2)
            return prepare_story(story, *args, **kwargs)
        service = PreparedFeedService(('https://example.com/feed',), client=Client(), count=1)
        with patch('modules.rss_renderer.prepare_story', side_effect=blocked), \
                patch('rss_thumbnails.ThumbnailPreparer.prepare', side_effect=AssertionError('story image download')):
            service.start()
            try:
                self.assertTrue(entered.wait(1))
                closer = threading.Thread(target=service.close, kwargs={'timeout': 2})
                closer.start()
            finally:
                release.set()
            closer.join(3)
        self.assertFalse(service.running)
        self.assertEqual(len(calls), 1)
        self.assertEqual(service.peek().stories, ())

    def test_ready_selection_no_io_preview_cursor_expiry_and_reflow(self):
        import rss_service as api
        self.assertTrue(hasattr(api, 'PreparedFeedService'), 'ready lifecycle absent')
        from modules.rss_renderer import duration_seconds, image_frames
        from rss_thumbnails import ThumbnailResult
        now = [1000.]
        class Client:
            def fetch(self, url):
                return b'<rss><channel><item><guid>one</guid><title>one</title><enclosure type="image/png" url="https://example.com/a.png"/></item><item><guid>two</guid><title>two</title></item></channel></rss>'
        with tempfile.TemporaryDirectory() as directory:
            service = api.PreparedFeedService(('https://example.com/feed',), client=Client(),
                cache=DiskFeedCache(directory), clock=lambda: now[0], count=1)
            self.assertFalse(service.running)
            with patch('rss_thumbnails.ThumbnailPreparer.prepare', return_value=ThumbnailResult(warning='rejected')) as decode:
                service.refresh_due()
                self.assertEqual(decode.call_count, 0, 'story images are never downloaded; site banner only')
            first = service.peek()
            self.assertEqual(first.items[0].story.keys, ('id:one',))
            self.assertEqual(first.items[0].source, 'https://example.com/feed')
            from rss_banners import site_banner
            self.assertEqual(first.stories[0].banner, site_banner('https://example.com/feed', ''))
            self.assertIsNone(first.stories[0].thumbnail)
            self.assertEqual(first.stories[0].warnings['headline_available_height'], 7)
            self.assertEqual(first.duration, duration_seconds(first.stories))
            with patch('builtins.open', side_effect=AssertionError('disk')), patch('modules.rss_renderer._font', side_effect=AssertionError('font')), patch.object(service.client, 'fetch', side_effect=AssertionError('network')):
                for _ in range(3):
                    self.assertEqual(service.peek(), first)
                    list(image_frames(first.stories))
                self.assertEqual(service.admit(), first)
                self.assertEqual(service.admit().stories, ())
            service.refresh_due()
            self.assertEqual(service.peek().items[0].story.keys, ('id:two',))
            now[0] += 86400
            self.assertEqual(service.peek().stories, ())
            self.assertEqual(service.admit().stories, ())
            service.close()

    def test_preparation_barriers_reconfigure_reset_close_cannot_publish(self):
        from rss_service import PreparedFeedService
        from modules.rss_renderer import prepare_story
        class Client:
            def fetch(self, url):
                return b'<rss><channel><item><guid>one</guid><title>one</title><enclosure type="image/png" url="https://example.com/a.png"/></item></channel></rss>'
        for action in ('configure', 'reset', 'close'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                cache = DiskFeedCache(directory)
                service = PreparedFeedService(('https://example.com/feed',), client=Client(), cache=cache)
                entered, release = threading.Event(), threading.Event()
                def blocked(story, *args, **kwargs):
                    entered.set()
                    release.wait(2)
                    return prepare_story(story, *args, **kwargs)
                with patch('modules.rss_renderer.prepare_story', side_effect=blocked):
                    worker = threading.Thread(target=service.refresh_due)
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(1))
                        generation = service.peek().generation
                        if action == 'configure':
                            service.configure(('https://example.com/other',))
                        elif action == 'reset':
                            self.assertTrue(hasattr(service, 'reset'), 'scoped reset absent')
                            service.reset()
                        else:
                            service.close()
                        self.assertGreater(service.peek().generation, generation)
                    finally:
                        release.set()
                        worker.join(2)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(service.peek().stories, ())
                    self.assertIsNone(cache.get_thumbnail('https://example.com/a.png'))
                    if action == 'reset':
                        self.assertIsNone(cache.get('https://example.com/feed'))
                service.close()

    def test_feed_fetch_after_reset_cannot_restore_cleared_cache(self):
        from rss_service import PreparedFeedService
        entered, release = threading.Event(), threading.Event()
        class Client:
            def fetch(self, url):
                entered.set()
                release.wait(2)
                return b'<rss><channel><item><title>one</title></item></channel></rss>'
        with tempfile.TemporaryDirectory() as directory:
            cache = DiskFeedCache(directory)
            service = PreparedFeedService(('https://example.com/feed',), client=Client(), cache=cache)
            worker = threading.Thread(target=service.refresh_due)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(hasattr(service, 'reset'), 'reset absent')
                service.reset()
            finally:
                release.set()
                worker.join(2)
            self.assertIsNone(cache.get('https://example.com/feed'))
            self.assertEqual(service.peek().stories, ())
            service.close()

    def test_refresh_identical_content_updates_expiry_and_restart_resets_seen(self):
        from rss_service import PreparedFeedService
        now, calls = [1000.], []
        class Client:
            def fetch(self, url):
                calls.append(url)
                return b'<rss><channel><item><guid>one</guid><title>one</title></item><item><guid>two</guid><title>two</title></item></channel></rss>'
        service = PreparedFeedService(('https://example.com/feed',), client=Client(), clock=lambda: now[0], count=1)
        service.refresh_due()
        now[0] += 1800
        service.refresh_due()
        self.assertEqual(service.peek().items[0].last_success, now[0])
        self.assertEqual(len(calls), 2)
        service.admit()
        service.refresh_due()
        self.assertEqual(service.peek().items[0].story.keys, ('id:two',))
        service.close()
        service.start()
        try:
            deadline = time.monotonic() + 1
            while not service.peek().items and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertEqual(service.peek().items[0].story.keys, ('id:one',))
        finally:
            service.close()
        self.assertFalse(service.running)
        self.assertEqual(service.peek().items, ())

    def test_admission_during_refresh_preparation_never_replays_old_selection(self):
        from rss_service import PreparedFeedService
        from modules.rss_renderer import prepare_story
        now = [1000.]
        class Client:
            def fetch(self, url):
                return b'<rss><channel><item><guid>one</guid><title>one</title></item><item><guid>two</guid><title>two</title></item></channel></rss>'
        service = PreparedFeedService(('https://example.com/feed',), client=Client(), count=1, clock=lambda: now[0])
        service.refresh_due()
        now[0] += 1800
        entered, release = threading.Event(), threading.Event()
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(2)
            return prepare_story(*args, **kwargs)
        with patch('modules.rss_renderer.prepare_story', side_effect=blocked):
            worker = threading.Thread(target=service.refresh_due)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(service.admit().items[0].story.keys, ('id:one',))
            finally:
                release.set()
                worker.join(2)
        self.assertEqual(service.peek().items, (), 'old selection republished after admission')
        service.refresh_due()
        self.assertEqual(service.peek().items[0].story.keys, ('id:two',))
        service.close()
