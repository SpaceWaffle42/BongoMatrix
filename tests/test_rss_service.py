import importlib
import importlib.util
import threading
import tempfile
import inspect
import unittest


def feed(title):
    return f'<rss><channel><title>News</title><item><title>{title}</title><guid>{title}</guid></item></channel></rss>'.encode()


class ServiceTests(unittest.TestCase):
    def test_refresh_isolated_stale_expiry_and_nonblocking_snapshot(self):
        self.assertIsNotNone(importlib.util.find_spec('rss_service'), 'feed service absent')
        api = importlib.import_module('rss_service')
        now, calls, fail = [1000.], [], set()
        entered, release = threading.Event(), threading.Event()
        class Client:
            def fetch(self, url):
                calls.append(url)
                if url in fail:
                    raise ValueError('raw secret detail')
                if url.endswith('/blocked'):
                    entered.set()
                    release.wait(2)
                return feed(url.rsplit('/', 1)[-1])
        urls = ('https://example.com/a', 'https://example.com/b')
        service = api.FeedService(urls, client=Client(), clock=lambda: now[0])
        self.assertEqual(service.select(), ())
        service.refresh_due()
        self.assertEqual(len(service.select()), 2)
        self.assertEqual([s.status for s in service.snapshots()], ['fresh', 'fresh'])
        service.refresh_due()
        self.assertEqual(len(calls), 2)
        now[0] += 1800
        fail.add(urls[0])
        service.refresh_due()
        self.assertEqual([s.status for s in service.snapshots()], ['stale', 'fresh'])
        self.assertEqual(service.snapshots()[0].last_success, 1000.)
        self.assertNotIn('raw secret', service.snapshots()[0].warning)
        now[0] = 87400.
        service.refresh_due()
        self.assertEqual([s.status for s in service.snapshots()], ['expired', 'fresh'])
        self.assertEqual([s.title for s in service.select()], ['b'])
        service.configure(('https://example.com/blocked',))
        service.start()
        self.assertTrue(entered.wait(1))
        # Snapshot and selection never wait on outbound network.
        self.assertEqual(service.select(), ())
        self.assertEqual(service.snapshots()[0].status, 'empty')
        release.set()
        service.close()
        self.assertFalse(service.running)
        for invalid in (['https://example.com/'] * 21, ['file:///etc/passwd'], 'https://example.com/'):
            with self.assertRaises(ValueError):
                api.FeedService(invalid, client=Client())

    def test_persistent_last_success_restart_seen_reset_and_cache_failure(self):
        from rss_service import FeedService
        from rss_cache import DiskFeedCache
        self.assertIn('cache', inspect.signature(FeedService).parameters, 'service cache integration absent')
        url = 'https://example.com/news'
        class Client:
            def fetch(self, url):
                return feed('hello')
        class Offline:
            def fetch(self, url):
                raise OSError('offline')
        with tempfile.TemporaryDirectory() as directory:
            cache = DiskFeedCache(directory)
            first = FeedService((url,), client=Client(), cache=cache, clock=lambda: 1000.)
            first.refresh_due()
            selected = first.select()
            restarted = FeedService((url,), client=Offline(), cache=cache, clock=lambda: 2000.)
            self.assertEqual(restarted.select(), selected)
            self.assertEqual(restarted.snapshots()[0].status, 'stale')
            restarted.refresh_due()
            self.assertEqual(restarted.snapshots()[0].last_success, 1000.)
            expired = FeedService((url,), client=Offline(), cache=cache, clock=lambda: 87400.)
            self.assertEqual(expired.select(), ())
            self.assertEqual(expired.snapshots()[0].status, 'expired')
            class BrokenCache:
                def get(self, url):
                    raise OSError('disk failure')
                def put(self, *args):
                    raise OSError('disk failure')
            healthy = FeedService((url,), client=Client(), cache=BrokenCache(), clock=lambda: 1000.)
            healthy.refresh_due()
            self.assertEqual(healthy.select(), selected)
            self.assertEqual(healthy.snapshots()[0].status, 'fresh')
            self.assertIn('cache', healthy.snapshots()[0].warning.lower())

    def test_material_reconfiguration_resets_without_intermediate_selection(self):
        from rss_service import FeedService
        class Client:
            def fetch(self, url):
                return b'<rss><channel><item><title>one</title></item><item><title>two</title></item></channel></rss>'
        urls = ('https://example.com/news',)
        service = FeedService(urls, client=Client())
        service.configure(urls, count=1)
        service.refresh_due()
        self.assertEqual(service.select()[0].title, 'one')
        service.configure(urls, count=1, include=('missing',))
        service.configure(urls, count=1)
        self.assertEqual(service.select()[0].title, 'one')
        service.configure(urls, count=1)
        self.assertEqual(service.select()[0].title, 'two')