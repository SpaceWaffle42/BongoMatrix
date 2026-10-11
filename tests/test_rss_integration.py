"""RSS host contracts, tested without external network."""
import unittest
import copy
import tempfile
from pathlib import Path
from rss_service import PreparedFeedService, FeedService


class ConfigIntegrationTests(unittest.TestCase):
    def test_predecessor_capacity_and_byte_budget_are_nondestructive(self):
        import json
        from config_store import ConfigStore, DEFAULT_CONFIG, CONFIG_SNAPSHOT_LIMIT
        encode = lambda v: json.dumps(v, sort_keys=True, separators=(',', ':')).encode()
        for count in (255, 256):
            for spare in (None, 0, 300, 800):
                with self.subTest(count=count, spare=spare), tempfile.TemporaryDirectory() as directory:
                    old = copy.deepcopy(DEFAULT_CONFIG)
                    old.pop('rss')
                    old.pop('event_countdown')
                    rows = [dict(module='clock_lava', duration=i+1, enabled=False) for i in range(count)]
                    old['playlist'] = rows
                    if spare is not None:
                        old['device']['name'] += ' ' * (CONFIG_SNAPSHOT_LIMIT-spare-len(encode(old)))
                    path = Path(directory) / 'config.json'
                    path.write_bytes(encode(old))
                    before = path.read_bytes()
                    store = ConfigStore(path)
                    current = store.get()
                    self.assertIn('rss', current)
                    self.assertEqual(current['playlist'][:count], rows)
                    self.assertLessEqual(len(current['playlist']), 256)
                    self.assertEqual(path.read_bytes(), before)
                    saved = store.update({})
                    self.assertLessEqual(path.stat().st_size, CONFIG_SNAPSHOT_LIMIT)
                    self.assertEqual(ConfigStore(path).get(), saved)

    def test_defaults_save_validation_and_removed_row_reopen(self):
        from config_store import ConfigStore
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / 'config.json')
            config = store.get()
            self.assertIn('rss', config)
            self.assertEqual(config['rss']['feeds'], ['https://feeds.bbci.co.uk/news/rss.xml'])
            self.assertEqual(config['rss']['refresh_seconds'], 1800)
            self.assertEqual(config['rss']['count'], 5)
            self.assertIsInstance(config['rss']['frame_delay'], (int, float))
            self.assertEqual([r['enabled'] for r in config['playlist'] if r['module'] == 'rss'], [False])
            store.update({'rss': {'count': 2, 'include': ['news']}})
            before = store.path.read_bytes()
            for value in (True, 0, 51, '5'):
                with self.assertRaises(ValueError):
                    store.update({'rss': {'count': value}})
                self.assertEqual(store.path.read_bytes(), before)
            store.update({'playlist': [r for r in config['playlist'] if r['module'] != 'rss']})
            reopened = ConfigStore(store.path).get()
            self.assertEqual(reopened['rss']['count'], 2)
            self.assertNotIn('rss', [r['module'] for r in reopened['playlist']])


class WebIntegrationTests(unittest.TestCase):
    def test_reset_cancels_draft_decode_without_enabling_disabled_physical(self):
        import threading
        from unittest.mock import patch
        from rss_thumbnails import ThumbnailResult
        from modules.rss_renderer import PreparedThumbnail
        f = self.fixture
        f.pair_browser()
        host = f.app.extensions['rss_host']
        before = f.store.path.read_bytes()
        settings = copy.deepcopy(f.store.get()['rss'])
        settings['feeds'] = ['https://example.com/draft']
        image = 'https://example.com/obsolete.png'
        entered, release, finished, returned = (threading.Event() for _ in range(4))
        stops, responses = [], []
        draft = host.draft_service
        refresh = draft.refresh_due
        def observed_refresh():
            refresh()
            finished.set()
        def decode(url, stop):
            stops.append(stop)
            entered.set()
            release.wait(5)
            return ThumbnailResult(PreparedThumbnail((bytes(768),), (100,)))
        def reset():
            try:
                responses.append(f.paired_request('post', '/api/rss/reset', json={}))
            finally:
                returned.set()
        wire = f'<rss><channel><item><title>obsolete</title><enclosure type="image/png" url="{image}"/></item></channel></rss>'.encode()
        with patch.object(draft.client, 'fetch', return_value=wire) as fetch, patch.object(draft._preparer, 'prepare', side_effect=decode), patch.object(draft, 'refresh_due', side_effect=observed_refresh):
            host.preview(settings)
            request = threading.Thread(target=reset)
            try:
                self.assertTrue(entered.wait(1))
                self.assertIsNotNone(draft.cache.get(settings['feeds'][0]))
                host.service._rotation.seen.add('id:old')
                request.start()
                self.assertTrue(returned.wait(1), 'reset route waited for draft decoder')
                self.assertEqual(responses[0].status_code, 200)
                self.assertTrue(stops[0].is_set())
                self.assertFalse(host.service.running)
                self.assertFalse(host.service._rotation.seen)
                self.assertEqual(draft.feeds, ())
                self.assertEqual(host.service.peek().items, ())
                release.set()
                self.assertTrue(finished.wait(1))
                self.assertEqual(draft.peek().items, ())
                self.assertIsNone(draft.cache.get(settings['feeds'][0]))
                self.assertIsNone(draft.cache.get_thumbnail(image))
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(f.store.path.read_bytes(), before)
            finally:
                release.set()
                if request.ident is not None:
                    request.join(2)
                host.close()

    def test_reset_during_fetch_returns_without_network_and_resumes_saved_physical(self):
        import threading
        from unittest.mock import patch
        from rss_config import DEFAULTS
        f = self.fixture
        f.pair_browser()
        host = f.app.extensions['rss_host']
        url = 'https://example.com/saved'
        config = f.store.get()
        for row in config['playlist']:
            if row['module'] == 'rss':
                row['enabled'] = True
        saved = f.store.update({'playlist': config['playlist'], 'rss': {'feeds': [url], 'count': 2}})
        before = f.store.path.read_bytes()
        entered, release = threading.Event(), threading.Event()
        resumed, allow_fresh = threading.Event(), threading.Event()
        ready, returned = threading.Event(), threading.Event()
        calls, responses = [], []
        service = host.service
        refresh = service.refresh_due
        def observed_refresh():
            refresh()
            if service.peek().items:
                ready.set()
        def fetch(source):
            calls.append(source)
            if len(calls) == 1:
                entered.set()
                release.wait(5)
                title = 'obsolete'
            else:
                resumed.set()
                allow_fresh.wait(5)
                title = 'fresh'
            return f'<rss><channel><item><title>{title}</title></item></channel></rss>'.encode()
        def reset():
            try:
                responses.append(f.paired_request('post', '/api/rss/reset', json={}))
            finally:
                returned.set()
        with patch.object(service.client, 'fetch', side_effect=fetch), patch.object(service, 'refresh_due', side_effect=observed_refresh):
            host.sync(saved)
            request = threading.Thread(target=reset)
            try:
                self.assertTrue(entered.wait(1))
                cancel = service._cancel
                generation = service.peek().generation
                service._rotation.seen.add('id:old')
                request.start()
                self.assertTrue(returned.wait(1), 'reset route waited for in-flight network')
                self.assertEqual(responses[0].status_code, 200)
                self.assertEqual(responses[0].json, {'defaults': DEFAULTS, 'saved': False, 'cache_cleared': True})
                self.assertTrue(cancel.is_set())
                self.assertGreater(service.peek().generation, generation)
                self.assertFalse(service._rotation.seen)
                self.assertTrue(service.running)
                self.assertEqual(f.store.path.read_bytes(), before)
                self.assertEqual(service.feeds, (url,))
                self.assertEqual(service.count, 2)
                release.set()
                self.assertTrue(resumed.wait(1), 'saved physical worker was not woken after reset')
                self.assertEqual(service.peek().items, ())
                self.assertIsNone(service.cache.get(url), 'obsolete fetch resurrected cache')
                allow_fresh.set()
                self.assertTrue(ready.wait(1))
                self.assertEqual([i.story.title for i in service.admit().items], ['fresh'])
                self.assertEqual(calls, [url, url])
                self.assertEqual(f.store.path.read_bytes(), before)
            finally:
                release.set()
                allow_fresh.set()
                if request.ident is not None:
                    request.join(2)
                host.close()

    def test_draft_options_use_bounded_owned_worker_and_never_change_physical(self):
        import time
        from unittest.mock import patch
        f = self.fixture
        f.pair_browser()
        host = f.app.extensions['rss_host']
        before = f.store.path.read_bytes()
        settings = copy.deepcopy(f.store.get()['rss'])
        settings['frame_delay'] = .1
        with patch('rss_transport.SafeFeedClient.fetch', return_value=b'<rss><channel><item><title>Headline</title><description>Overview</description></item></channel></rss>'):
            result = f.paired_request('post', '/api/scroll-timing/rss', json={
                'base_revision': f.store.get()['revision'], 'settings': settings})
            deadline = time.monotonic() + 1
            while result.status_code == 200 and result.json['ready_count'] == 0 and time.monotonic() < deadline:
                time.sleep(.005)
                result = f.paired_request('post', '/api/scroll-timing/rss', json={
                    'base_revision': f.store.get()['revision'], 'settings': settings})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json['ready_count'], 1)
            snapshot = host.preview(settings)
            self.assertEqual(snapshot.stories[0].options.frame_delay, .1)
            worker = host.draft_service._thread
            for _ in range(5):
                self.assertEqual(host.preview(settings), snapshot)
                self.assertIs(host.draft_service._thread, worker)
        self.assertFalse(host.service.running)
        self.assertFalse(host.service._rotation.seen)
        self.assertEqual(f.store.path.read_bytes(), before)
        host.close()
        self.assertFalse(worker.is_alive())

    def test_preview_duration_and_controller_share_ready_snapshot_without_consuming(self):
        import threading
        from unittest.mock import Mock, patch
        from display_controller import DisplayController
        from device_preview import DevicePreviewRequest, DevicePreviewOwner
        from modules.rss_renderer import PlaybackResult
        f = self.fixture
        f.pair_browser()
        host = f.app.extensions['rss_host']
        with patch.object(host.service.client, 'fetch', return_value=b'<rss><channel><item><guid>one</guid><title>Headline</title><description>Overview</description></item></channel></rss>'):
            host.service.refresh_due()
        ready = host.service.peek()
        before = f.store.path.read_bytes()
        timing = f.paired_request('post', '/api/scroll-timing/rss', json={
            'base_revision': f.store.get()['revision'], 'settings': f.store.get()['rss']})
        self.assertEqual(timing.status_code, 200, timing.get_data(as_text=True))
        self.assertEqual(timing.json['duration_seconds'], ready.duration)
        preview = f.paired_request('post', '/api/edit-preview/rss', json={
            'config': {}, 'generation': 1, 'preview_instance': 'e'*32})
        self.assertEqual(preview.status_code, 200, preview.get_data(as_text=True))
        self.assertTrue(preview.json['frames'])
        self.assertEqual(host.service.peek(), ready)
        self.assertEqual(f.store.path.read_bytes(), before)
        controller = DisplayController(Mock(), f.store)
        controller.rss_host = host
        self.addCleanup(controller.stop)
        with patch('modules.rss_renderer.run', return_value=PlaybackResult(1, .05)) as render:
            self.assertTrue(controller._execute('rss', f.store.get(), 15))
            self.assertEqual(render.call_args.args[1], ready.stories)
        self.assertEqual(host.service.peek().items, ())
        self.assertFalse(controller._execute('rss', f.store.get(), 15))
        request = DevicePreviewRequest(owner=DevicePreviewOwner('a','b'), generation=1,
            config={'module': 'rss', 'candidate': f.store.get()}, timeout_seconds=1,
            cleanup=lambda: None, pinned_media={})
        with patch('modules.rss_renderer.run', return_value=PlaybackResult(1, .05)) as render:
            self.assertTrue(controller.render_device_draft(controller.matrix, request,
                stop_event=threading.Event(), prepared=ready))
            self.assertEqual(render.call_args.args[1], ready.stories)

    def setUp(self):
        from tests.test_webapp import WebAppTests
        self.fixture = WebAppTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.pairing.verify.return_value = True

    def test_protected_status_test_reset_and_save(self):
        from unittest.mock import patch
        f = self.fixture
        for path in ('/api/rss/status', '/api/rss/test', '/api/rss/reset'):
            response = f.client.open(path, method='GET' if path.endswith('status') else 'POST', json={})
            self.assertEqual(response.status_code, 401)
        f.pair_browser()
        before = f.store.path.read_bytes()
        status = f.client.get('/api/rss/status')
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.json['running'])
        self.assertEqual(len(status.json['presets']['feeds']), 7)
        self.assertEqual(f.client.post('/api/rss/test', json={'url': 'https://example.com/rss'}).status_code, 403)
        with patch('rss_transport.SafeFeedClient.fetch', return_value=b'<rss><channel><title>News</title><item><title>one</title><pubDate>Sun, 11 Oct 2026 12:00:00 GMT</pubDate></item></channel></rss>') as fetch:
            result = f.paired_request('post', '/api/rss/test', json={'url': 'https://example.com/rss'})
        self.assertEqual(result.status_code, 200, result.get_data(as_text=True))
        self.assertEqual(result.json['title'], 'News')
        self.assertEqual(result.json['count'], 1)
        self.assertIsNotNone(result.json['newest'])
        self.assertFalse(result.json['image_available'])
        self.assertEqual(fetch.call_count, 1)
        reset = f.paired_request('post', '/api/rss/reset', json={})
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset.json['defaults']['count'], 5)
        self.assertEqual(f.store.path.read_bytes(), before)
        revision = f.store.get()['revision']
        saved = f.paired_request('post', '/api/config', headers={'If-Match': f'"{revision}"'}, json={'rss': {'count': 3}})
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        self.assertEqual(f.store.get()['rss']['count'], 3)
        self.assertFalse(f.client.get('/api/rss/status').json['running'])


class ServiceContractTests(unittest.TestCase):
    def test_disk_operations_do_not_hold_reader_lock(self):
        import threading
        class Cache:
            service = None
            def check(cache):
                if cache.service is None:
                    return
                done = threading.Event()
                thread = threading.Thread(target=lambda: (cache.service.peek(), done.set()))
                thread.start()
                ready = done.wait(.5)
                thread.join(.01)
                self.assertTrue(ready, 'reader blocked by disk operation')
            def get(cache, url):
                cache.check()
            def put(cache, *args):
                cache.check()
                return True
            def clear(cache):
                cache.check()
        class Client:
            def fetch(self, url):
                return b'<rss><channel><item><title>one</title></item></channel></rss>'
        cache = Cache()
        service = PreparedFeedService(('https://example.com/rss',), cache=cache, client=Client())
        cache.service = service
        service.configure(service.feeds)
        service.refresh_due()
        service.reset()
        service.close()

    def test_select_retains_normalized_base_contract(self):
        self.assertIs(PreparedFeedService.select, FeedService.select)

    def test_constructor_rejects_invalid_options_including_falsey(self):
        for value in ('invalid', '', False, 0, {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PreparedFeedService(options=value)
