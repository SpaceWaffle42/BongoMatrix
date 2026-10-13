"""RSS host contracts, tested without external network."""
import unittest
import copy
import tempfile
from pathlib import Path
from rss_service import PreparedFeedService, FeedService

# Owner 2026-10-13: default feeds (each verified live through SafeFeedClient + parse_feed before adding).
DEFAULT_FEEDS = (
    'https://feeds.bbci.co.uk/news/rss.xml',
    'https://www.reddit.com/r/worldnews/.rss',
    'http://feeds.skynews.com/feeds/rss/world.xml',
    'http://www.espn.com/espn/rss/news',
    'https://rss.nytimes.com/services/xml/rss/nyt/World.xml',
    'https://www.theguardian.com/uk/rss',
    'https://www.allsides.com/rss/news',
    'https://news.google.com/rss/search?q=when:7d+site:reuters.com&ceid=US:en&hl=en-US&gl=US',
    'https://feedx.net/rss/ap.xml',
)


class ConfigIntegrationTests(unittest.TestCase):
    def test_nondefault_predecessor_at_byte_limit_opens_updates_and_retains_removed_rows(self):
        import json
        from config_store import ConfigStore, DEFAULT_CONFIG, CONFIG_SNAPSHOT_LIMIT
        from rss_config import DEFAULTS
        added = ('disabled_feeds', 'layout', 'headline_colour', 'summary_colour', 'background_colour')
        encode = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        for removed in (False, True):
            with self.subTest(removed=removed), tempfile.TemporaryDirectory() as directory:
                old = copy.deepcopy(DEFAULT_CONFIG)
                old['rss']['count'] = 3
                old['event_countdown']['layout'] = 'icon'
                for key in added:
                    old['rss'].pop(key)
                if removed:
                    old['playlist'] = [r for r in old['playlist'] if r['module'] not in ('rss', 'event_countdown')]
                old['device']['name'] += ' ' * (CONFIG_SNAPSHOT_LIMIT - len(encode(old)))
                path = Path(directory) / 'config.json'
                path.write_bytes(encode(old))
                before = path.read_bytes()
                self.assertEqual(len(before), CONFIG_SNAPSHOT_LIMIT)
                store = ConfigStore(path)
                current = store.get()
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(current['playlist'], old['playlist'])
                self.assertEqual(current['device'], old['device'])
                for key in added:
                    self.assertEqual(current['rss'][key], DEFAULTS[key])
                saved = store.update({'rss': {'count': 4}}, base_revision=current['revision'])
                self.assertEqual(saved['rss']['count'], 4)
                self.assertEqual(saved['event_countdown'], old['event_countdown'])
                self.assertEqual(saved['device'], old['device'])
                self.assertEqual(saved['playlist'], old['playlist'])
                self.assertLessEqual(path.stat().st_size, CONFIG_SNAPSHOT_LIMIT)
                self.assertEqual(ConfigStore(path).get(), saved)

    def test_disabled_feeds_persist_order_and_old_sources_migrate(self):
        import json
        from config_store import ConfigStore
        urls = ['https://example.com/a', 'https://example.com/b']
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / 'config.json')
            old = store.update({'rss': {'feeds': urls}})
            old['rss'].pop('disabled_feeds', None)
            for key in ('layout', 'headline_colour', 'summary_colour', 'background_colour'):
                old['rss'].pop(key, None)
            store.path.write_text(json.dumps(old))
            before = store.path.read_bytes()
            store = ConfigStore(store.path)
            self.assertEqual(store.get()['rss'].get('disabled_feeds'), [])
            self.assertEqual(store.path.read_bytes(), before)
            saved = store.update({'rss': {'disabled_feeds': [urls[0]]}})
            self.assertEqual(saved['rss']['feeds'], urls)
            self.assertEqual(ConfigStore(store.path).get()['rss'], saved['rss'])
            saved = store.update({'rss': {'feeds': urls[::-1]}})
            self.assertEqual(saved['rss']['disabled_feeds'], [urls[0]])
            self.assertEqual(saved['rss']['layout'], 'auto')
            saved = store.update({'rss': {'layout': 'text', 'headline_colour': '#12ab34',
                                         'summary_colour': '#abcdef', 'background_colour': '#010203'}})
            self.assertEqual(ConfigStore(store.path).get()['rss'], saved['rss'])
            before = store.path.read_bytes()
            for changes in ({'layout': 'image'}, {'headline_colour': 'red'},
                            {'summary_colour': '#fffffff'}, {'background_colour': False}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    store.update({'rss': changes})
                self.assertEqual(store.path.read_bytes(), before)
            before = store.path.read_bytes()
            for disabled in ([urls[0], urls[0]], ['https://example.com/unknown'], [False], 'all'):
                with self.subTest(disabled=disabled), self.assertRaises(ValueError):
                    store.update({'rss': {'disabled_feeds': disabled}})
                self.assertEqual(store.path.read_bytes(), before)

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
            self.assertEqual(config['rss']['feeds'], list(DEFAULT_FEEDS))
            self.assertEqual(config['rss']['disabled_feeds'], [])
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
    def test_cold_device_cancel_after_heartbeat_clears_pending_request(self):
        from unittest.mock import Mock, patch
        from display_controller import DisplayController
        f = self.fixture
        f.pair_browser()
        host = f.app.extensions['rss_host']
        self.addCleanup(host.close)
        controller = DisplayController(Mock(), f.store)
        self.addCleanup(controller.stop)
        f.controller.submit_device_draft.side_effect = controller.submit_device_draft
        f.controller.heartbeat_device_draft.side_effect = controller.heartbeat_device_draft
        f.controller.cancel_device_draft.side_effect = controller.cancel_device_draft
        with patch.object(controller, 'start'), patch.object(host.draft_service, 'start'):
            response = f.paired_request('post', '/api/device-preview/rss', json={
                'config': {'rss': {'count': 2}}, 'generation': 1, 'preview_instance': 'b' * 32})
            self.assertEqual(response.status_code, 202)
            self.assertFalse(controller._execute_pending_device_draft())
            lease = controller.device_preview_manager.active_lease()
            with patch.object(controller.device_preview_manager, '_clock', return_value=lease.last_heartbeat_at + 5):
                heartbeat = f.paired_request('post', '/api/device-preview/heartbeat', json={
                    'generation': 1, 'preview_instance': 'b' * 32})
                self.assertEqual(heartbeat.status_code, 202)
                self.assertIsNot(controller.device_preview_manager.active_lease(), lease)
                cancelled = f.paired_request('post', '/api/device-preview/cancel', json={
                    'generation': 1, 'preview_instance': 'b' * 32})
                self.assertEqual(cancelled.status_code, 202)
            self.assertIsNone(controller._device_draft_pending)
            self.assertIsNone(controller.device_preview_manager.active_lease())
            controller.matrix.draw_image.assert_not_called()

    def test_failed_latest_sync_reports_committed_and_cannot_revert_to_old_revision(self):
        from unittest.mock import patch
        f = self.fixture
        f.pair_browser()
        host = f.app.extensions['rss_host']
        self.addCleanup(host.close)
        old = f.store.get()
        rows = copy.deepcopy(old['playlist'])
        for row in rows:
            if row['module'] == 'rss':
                row['enabled'] = True
        with patch.object(host.service, 'start', side_effect=RuntimeError('worker failure')):
            response = f.paired_request('post', '/api/config',
                headers={'If-Match': f'"{old["revision"]}"'},
                json={'rss': {'count': 3}, 'playlist': rows})
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.json['committed'])
        self.assertEqual(response.json['config'], f.store.get())
        host.sync(old)
        self.assertEqual(host.service.count, 3)
        with patch.object(host.service.client, 'fetch', return_value=b'<rss><channel/></rss>'):
            host.sync(f.store.get())
            self.assertTrue(host.service.running)
            host.service.close()

    def test_timing_rss_exact_envelope_boundary_known_and_unknown_lengths(self):
        import io
        import json
        from unittest.mock import patch
        from rss_config import timing_request_limit
        f = self.fixture
        f.pair_browser()
        current = f.store.get()
        host = f.app.extensions['rss_host']
        self.addCleanup(host.close)
        body = json.dumps({'base_revision': current['revision'], 'settings': current['rss']}).encode()
        limit = timing_request_limit(current['revision'])
        self.assertLessEqual(len(body), limit)
        at_limit = body + b' ' * (limit - len(body))
        before = f.store.path.read_bytes()
        with patch.object(host.draft_service, 'start'), patch('rss_transport.SafeFeedClient.fetch') as fetch:
            for unknown in (False, True):
                for extra, expected in ((b'', 200), (b' ', 413)):
                    with self.subTest(unknown=unknown, expected=expected):
                        options = {'input_stream': io.BytesIO(at_limit + extra),
                            'environ_overrides': {'CONTENT_LENGTH': '', 'wsgi.input_terminated': True}} if unknown else {'data': at_limit + extra}
                        response = f.paired_request('post', '/api/scroll-timing/rss',
                            content_type='application/json', **options)
                        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
            fetch.assert_not_called()
        self.assertEqual(f.store.path.read_bytes(), before)

    def test_cold_device_generation_replacement_uses_only_latest_enabled_source_and_options(self):
        import threading
        from unittest.mock import Mock, patch
        from display_controller import DisplayController
        f = self.fixture
        f.pair_browser()
        host = f.app.extensions['rss_host']
        self.addCleanup(host.close)
        controller = DisplayController(Mock(), f.store)
        controller.rss_host = host
        self.addCleanup(controller.stop)
        f.controller.submit_device_draft.side_effect = controller.submit_device_draft
        f.controller.render_device_draft.side_effect = controller.render_device_draft
        entered, release, ready = (threading.Event() for _ in range(3))
        urls = ['https://example.com/old', 'https://example.com/latest']
        service = host.draft_service
        original_refresh = service.refresh_due
        def fetch(url):
            if url == urls[0]:
                entered.set()
                release.wait(3)
            return f'<rss><channel><item><title>{url.rsplit("/", 1)[1]}</title></item></channel></rss>'.encode()
        def refresh():
            original_refresh()
            if service.peek().items:
                ready.set()
        def stop_after_swap():
            stop = controller._device_draft_stop
            self.assertIsNotNone(stop)
            stop.set()
        with patch.object(controller, 'start'), patch.object(service.client, 'fetch', side_effect=fetch), patch.object(service, 'refresh_due', side_effect=refresh):
            first = f.paired_request('post', '/api/device-preview/rss', json={
                'config': {'rss': {'feeds': urls}}, 'generation': 1, 'preview_instance': 'd' * 32})
            self.assertEqual(first.status_code, 202)
            try:
                self.assertTrue(entered.wait(1))
                old_request = controller.device_preview_manager.active_lease().request
                self.assertFalse(controller._execute_pending_device_draft())
                second = f.paired_request('post', '/api/device-preview/rss', json={
                    'config': {'rss': {'feeds': urls, 'disabled_feeds': [urls[0]],
                        'layout': 'text', 'headline_colour': '#abcdef'}},
                    'generation': 2, 'preview_instance': 'd' * 32})
                self.assertEqual(second.status_code, 202)
                self.assertEqual(controller.device_preview_manager.active_lease().generation, 2)
                self.assertFalse(controller._execute_pending_device_draft())
                controller.matrix.draw_image.assert_not_called()
            finally:
                release.set()
            self.assertTrue(ready.wait(2))
            with patch.object(controller.matrix, 'swap', side_effect=stop_after_swap):
                self.assertTrue(controller._execute_pending_device_draft())
            call = f.controller.render_device_draft.call_args
            self.assertIsNot(call.args[1], old_request)
            self.assertEqual(call.args[1].generation, 2)
            prepared = call.kwargs['prepared']
            self.assertEqual([item.source for item in prepared.items], [urls[1]])
            self.assertEqual(prepared.stories[0].options.layout, 'text')
            self.assertEqual(prepared.stories[0].options.headline_colour, '#abcdef')
            self.assertFalse(host.service._rotation.seen)
            self.assertFalse(service._rotation.seen)

    def test_timing_admits_maximum_saved_rss_sources_and_disabled_subset(self):
        import json
        from unittest.mock import patch
        from rss_config import validate
        f = self.fixture
        f.pair_browser()
        host = f.app.extensions['rss_host']
        self.addCleanup(host.close)
        settings = copy.deepcopy(f.store.get()['rss'])
        settings['feeds'] = [f'https://example.com/{i:02d}?' + '界' * (2048 - 23) for i in range(20)]
        self.assertEqual({len(url) for url in settings['feeds']}, {2048})
        settings['include'] = ['𐐀' * 64] * 50
        settings['exclude'] = ['𐐁' * 64] * 50
        with patch.object(host.draft_service, 'start'), patch.object(host.service.client, 'fetch') as fetch:
            for disabled_count in (19, 20):
                with self.subTest(disabled_count=disabled_count):
                    settings['disabled_feeds'] = settings['feeds'][:disabled_count]
                    validate(settings)
                    saved = f.paired_request('post', '/api/config',
                        headers={'If-Match': f'"{f.store.get()["revision"]}"'}, json={'rss': settings})
                    self.assertEqual(saved.status_code, 200)
                    body = json.dumps({'base_revision': f.store.get()['revision'],
                        'settings': settings, 'settings_correlation': 'f' * 64}, separators=(',', ':'))
                    response = f.paired_request('post', '/api/scroll-timing/rss',
                        data=body, content_type='application/json')
                    self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                    before = f.store.path.read_bytes()
                    oversized = f.paired_request('post', '/api/scroll-timing/rss',
                        data=body + ' ' * (2 * 1024 * 1024), content_type='application/json')
                    self.assertEqual(oversized.status_code, 413)
                    self.assertEqual(f.store.path.read_bytes(), before)
            fetch.assert_not_called()

    def test_cold_device_preview_defers_render_and_cancel_discards_late_preparation(self):
        import threading
        from unittest.mock import Mock, patch
        from display_controller import DisplayController
        f = self.fixture
        f.pair_browser()
        host = f.app.extensions['rss_host']
        self.addCleanup(host.close)
        matrix = Mock()
        controller = DisplayController(matrix, f.store)
        controller.rss_host = host
        self.addCleanup(controller.stop)
        f.controller.submit_device_draft.side_effect = controller.submit_device_draft
        f.controller.render_device_draft.side_effect = controller.render_device_draft
        f.controller.cancel_device_draft.side_effect = controller.cancel_device_draft
        before = f.store.path.read_bytes()
        for generation, cancel in ((1, False), (2, True)):
            with self.subTest(cancel=cancel):
                entered, release, ready = (threading.Event() for _ in range(3))
                service = host.draft_service
                original_refresh = service.refresh_due
                def fetch(url):
                    entered.set()
                    release.wait(3)
                    return b'<rss><channel><item><title>Draft</title></item></channel></rss>'
                def refresh():
                    original_refresh()
                    if service.peek().items:
                        ready.set()
                matrix.reset_mock()
                with patch.object(controller, 'start'), patch.object(service.client, 'fetch', side_effect=fetch), patch.object(service, 'refresh_due', side_effect=refresh):
                    response = f.paired_request('post', '/api/device-preview/rss', json={
                        'config': {'rss': {'feeds': [f'https://example.com/{generation}'],
                            'layout': 'text', 'headline_colour': '#123456', 'disabled_feeds': []}},
                        'generation': generation, 'preview_instance': 'c' * 32, 'timeout_seconds': 5})
                    self.assertEqual(response.status_code, 202)
                    try:
                        self.assertFalse(controller._execute_pending_device_draft())
                        self.assertTrue(entered.wait(1))
                        self.assertIsNotNone(controller.device_preview_manager.active_lease())
                        self.assertIsNotNone(controller._device_draft_pending)
                        matrix.draw_image.assert_not_called()
                        if cancel:
                            result = f.paired_request('post', '/api/device-preview/cancel', json={
                                'generation': generation, 'preview_instance': 'c' * 32})
                            self.assertEqual(result.status_code, 202)
                    finally:
                        release.set()
                    self.assertTrue(ready.wait(2))
                    if cancel:
                        self.assertFalse(controller._execute_pending_device_draft())
                        matrix.draw_image.assert_not_called()
                    else:
                        # Stop after a real first-frame swap; no fake renderer.
                        with patch.object(matrix, 'swap', side_effect=lambda: controller._device_draft_stop.set()):
                            self.assertTrue(controller._execute_pending_device_draft())
                        self.assertGreater(matrix.draw_image.call_count, 0)
                        prepared = f.controller.render_device_draft.call_args.kwargs['prepared']
                        self.assertEqual(prepared.stories[0].options.layout, 'text')
                        self.assertEqual(prepared.stories[0].options.headline_colour, '#123456')
                    self.assertIsNone(controller.device_preview_manager.active_lease())
                    self.assertFalse(service._rotation.seen)
                    self.assertFalse(host.service._rotation.seen)
                    self.assertEqual(f.store.path.read_bytes(), before)

    def test_overlapping_committed_saves_keep_latest_runtime_and_worker_lifecycle(self):
        import threading
        from unittest.mock import patch
        f = self.fixture
        f.pair_browser()
        host = f.app.extensions['rss_host']
        self.addCleanup(host.close)
        original_sync = host.sync
        for latest_enabled in (False, True):
            with self.subTest(latest_enabled=latest_enabled):
                entered, release = threading.Event(), threading.Event()
                results = []
                first_config = f.store.get()
                rows = copy.deepcopy(first_config['playlist'])
                for row in rows:
                    if row['module'] == 'rss':
                        row['enabled'] = True
                def delayed_sync(config):
                    if config['rss']['count'] == 2:
                        entered.set()
                        if not release.wait(3):
                            raise TimeoutError('save barrier')
                    return original_sync(config)
                def save_first():
                    results.append(f.paired_request('post', '/api/config',
                        headers={'If-Match': f'"{first_config["revision"]}"'},
                        json={'playlist': rows, 'rss': {'count': 2, 'feeds': ['https://example.com/old'], 'disabled_feeds': []}}))
                with patch.object(host, 'sync', side_effect=delayed_sync), patch.object(host.service.client, 'fetch', return_value=b'<rss><channel/></rss>'):
                    first = threading.Thread(target=save_first)
                    first.start()
                    try:
                        self.assertTrue(entered.wait(2))
                        current = f.store.get()
                        for row in current['playlist']:
                            if row['module'] == 'rss':
                                row['enabled'] = latest_enabled
                        second = f.paired_request('post', '/api/config',
                            headers={'If-Match': f'"{current["revision"]}"'},
                            json={'playlist': current['playlist'], 'rss': {'count': 3,
                                'feeds': ['https://example.com/new', 'https://example.com/off'],
                                'disabled_feeds': ['https://example.com/off']}})
                        self.assertEqual(second.status_code, 200)
                    finally:
                        release.set()
                        first.join(3)
                    self.assertFalse(first.is_alive())
                    self.assertEqual(results[0].status_code, 200)
                    self.assertEqual(f.store.get()['rss']['count'], 3)
                    self.assertEqual(host.service.count, 3)
                    self.assertEqual(host.service.feeds, ('https://example.com/new',))
                    self.assertEqual(host.service.running, latest_enabled)
                    host.service.close()

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
        from modules.rss_renderer import prepare_story
        def decode(story, *args, **kwargs):
            # Story preparation (site banner + text) is the blocking stage now.
            stops.append(story)
            entered.set()
            release.wait(5)
            return prepare_story(story, *args, **kwargs)
        def reset():
            try:
                responses.append(f.paired_request('post', '/api/rss/reset', json={}))
            finally:
                returned.set()
        wire = f'<rss><channel><item><title>obsolete</title><enclosure type="image/png" url="{image}"/></item></channel></rss>'.encode()
        with patch.object(draft.client, 'fetch', return_value=wire) as fetch, patch('modules.rss_renderer.prepare_story', side_effect=decode), patch.object(draft, 'refresh_due', side_effect=observed_refresh):
            host.preview(settings)
            request = threading.Thread(target=reset)
            try:
                self.assertTrue(entered.wait(1))
                self.assertIsNotNone(draft.cache.get(settings['feeds'][0]))
                host.service._rotation.seen.add('id:old')
                request.start()
                self.assertTrue(returned.wait(1), 'reset route waited for draft decoder')
                self.assertEqual(responses[0].status_code, 200)
                self.assertTrue(stops)
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
        settings['feeds'], settings['disabled_feeds'] = settings['feeds'][:1], []  # one fixture story
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
    def test_host_filters_disabled_sources_and_invalidates_ready_in_both_lanes(self):
        from unittest.mock import patch
        from rss_host import RSSHost
        from rss_config import DEFAULTS
        urls = ['https://example.com/a', 'https://example.com/b']
        with tempfile.TemporaryDirectory() as directory:
            host = RSSHost(directory)
            self.addCleanup(host.close)
            settings = copy.deepcopy(DEFAULTS)
            settings['feeds'] = urls
            config = {'rss': settings, 'playlist': []}
            def fetch(url):
                return f'<rss><channel><item><title>{url[-1]}</title></item></channel></rss>'.encode()
            with patch.object(host.draft_service, 'start'):
                for service, configure in ((host.service, lambda: host.sync(config)),
                                           (host.draft_service, lambda: host.preview(settings))):
                    settings['disabled_feeds'] = []
                    configure()
                    with patch.object(service.client, 'fetch', side_effect=fetch) as request:
                        service.refresh_due()
                        self.assertEqual([i.source for i in service.peek().items], urls)
                        generation, cancel = service.peek().generation, service._cancel
                        settings['disabled_feeds'] = [urls[0]]
                        configure()
                        self.assertGreater(service.peek().generation, generation)
                        self.assertTrue(cancel.is_set())
                        self.assertEqual(service.admit().items, ())
                        service.reset()
                        request.reset_mock()
                        service.refresh_due()
                        self.assertEqual([c.args[0] for c in request.call_args_list], [urls[1]])
                        self.assertEqual([i.source for i in service.admit().items], [urls[1]])
                        settings['disabled_feeds'] = urls[:]
                        configure()
                        request.reset_mock()
                        service.refresh_due()
                        self.assertEqual(service.admit().items, ())
                        request.assert_not_called()
                    self.assertEqual(settings['feeds'], urls)

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
