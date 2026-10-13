"""Offline astronomy integration; no external providers or production state."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import threading
import datetime as dt
import importlib.util
import os


class HostTests(unittest.TestCase):
    def test_stale_saved_snapshot_refreshes_only_draft_lane(self):
        clock = [dt.datetime(2024, 4, 8, 12, tzinfo=dt.timezone.utc)]
        host = self.host_class()(utc_clock=lambda: clock[0])
        self.addCleanup(host.close)
        config = self.config()
        host.sync(config)
        saved = host.saved.wait(threading.Event())
        self.assertIs(host.preview(config), saved)
        clock[0] += dt.timedelta(days=1)
        draft = host.wait_preview(config, threading.Event())
        self.assertEqual(draft.prepared_at, clock[0])
        self.assertIs(host.saved.peek(), saved)
        self.assertEqual(host._revision, config['revision'])

    def test_real_unchanged_draft_refreshes_at_local_date_and_phase_boundaries(self):
        from astronomy import Astronomy, Location
        config = self.config()
        config['astronomy'].update(location_source='override', latitude=51.4779, longitude=0)
        with Astronomy() as engine:
            phase = engine.moon(dt.datetime(2024, 4, 8, 12, tzinfo=dt.timezone.utc),
                Location(51.4779, 0, 'Europe/London', 'Greenwich', 'Test coordinates')).next_phase.time
        for boundary in (dt.datetime(2024, 4, 8, 23, tzinfo=dt.timezone.utc), phase):
            with self.subTest(boundary=boundary):
                clock = [boundary - dt.timedelta(seconds=1)]
                host = self.host_class()(utc_clock=lambda: clock[0])
                control = self.host_class()(utc_clock=lambda: clock[0])
                try:
                    first = host.wait_preview(config, threading.Event())
                    self.assertEqual(first.status, 'ready', first.error)
                    self.assertIs(host.preview(config), first)
                    clock[0] = boundary + dt.timedelta(seconds=1)
                    second = host.wait_preview(config, threading.Event())
                    fresh = control.wait_preview(config, threading.Event())
                    self.assertIsNot(second, first)
                    self.assertEqual(second.prepared_at, clock[0])
                    self.assertEqual(second.cards, fresh.cards)
                    self.assertNotEqual(first.cards[0].labels, second.cards[0].labels)
                finally:
                    host.close()
                    control.close()

    def test_empty_freshness_and_polling_do_not_restart_inflight_jobs(self):
        entered, release = threading.Event(), threading.Event()
        clock = [dt.datetime(2024, 4, 8, 22, 59, 59, tzinfo=dt.timezone.utc)]
        calls = []
        def prepare(settings, location, now, stop):
            calls.append(now)
            entered.set()
            release.wait(5)
            return ()
        host = self.host_class()(preparer=prepare, utc_clock=lambda: clock[0])
        self.addCleanup(host.close)
        config = self.config()
        try:
            initial = host.preview(config, owner='timing')
            self.assertTrue(entered.wait(1))
            clock[0] += dt.timedelta(seconds=2)
            for _ in range(10):
                self.assertEqual(host.preview(config, owner='timing').generation, initial.generation)
            release.set()
            # Observe this admitted generation directly: a new host request may
            # correctly refresh if the worker wins the release/request race.
            first = host.draft.wait(threading.Event(), generation=initial.generation)
            # If preparation crossed the boundary it is immediately stale, not
            # perpetually restarted while it is still running.
            second = host.wait_preview(config, threading.Event(), owner='timing')
            self.assertGreater(second.generation, first.generation)
            self.assertEqual(second.status, 'empty')
            self.assertEqual(len(calls), 2)
            clock[0] += dt.timedelta(minutes=5)
            third = host.wait_preview(config, threading.Event(), owner='timing')
            self.assertGreater(third.generation, second.generation)
            self.assertEqual(len(calls), 3)
        finally:
            release.set()

    def test_owner_replacement_distinct_contention_and_bounded_sharing(self):
        from astronomy_host import PreparationBusy
        entered, release = threading.Event(), threading.Event()
        def prepare(*args):
            entered.set()
            release.wait(5)
            return ()
        host = self.host_class()(preparer=prepare)
        self.addCleanup(host.close)
        first = self.config()
        changed = copy.deepcopy(first)
        changed['astronomy']['moon_hold_seconds'] += 1
        try:
            initial = host.preview(first, owner=('edit', 'page-a'))
            self.assertTrue(entered.wait(1))
            stop = host.draft._cancel
            with self.assertRaises(PreparationBusy):
                host.preview(changed, owner=('edit', 'page-b'))
            self.assertFalse(stop.is_set())
            replacement = host.preview(changed, owner=('edit', 'page-a'))
            self.assertGreater(replacement.generation, initial.generation)
            self.assertTrue(stop.is_set())
            for n in range(63):
                self.assertEqual(host.preview(changed, owner=('timing', n)).generation, replacement.generation)
            with self.assertRaises(PreparationBusy):
                host.preview(changed, owner=('device', 'overflow'))
            self.assertEqual(len(host.draft._owners), 64)
        finally:
            release.set()

    def test_same_key_cancellation_keeps_other_waiter_alive(self):
        self._shared_waiter_cancellation(None)

    def test_same_owner_same_key_waiters_are_reference_counted(self):
        self._shared_waiter_cancellation(('edit', 'same-page'))

    def _shared_waiter_cancellation(self, owner):
        entered, release = threading.Event(), threading.Event()
        def prepare(*args):
            entered.set()
            release.wait(5)
            return ()
        host = self.host_class()(preparer=prepare)
        self.addCleanup(host.close)
        config = self.config()
        results = {}
        first_stop, second_stop = threading.Event(), threading.Event()
        def wait(name, stop):
            results[name] = host.wait_preview(config, stop, owner=owner)
        first = threading.Thread(target=wait, args=('first', first_stop))
        second = threading.Thread(target=wait, args=('second', second_stop))
        first.start()
        try:
            self.assertTrue(entered.wait(1))
            # Hold the lane condition until the second thread is in wait().
            from unittest.mock import patch
            joined = threading.Event()
            original = host.draft.wait
            def waiting(*args, **kwargs):
                joined.set()
                return original(*args, **kwargs)
            with patch.object(host.draft, 'wait', side_effect=waiting):
                second.start()
                self.assertTrue(joined.wait(1))
                first_stop.set()
                first.join(1)
                self.assertFalse(first.is_alive())
                self.assertFalse(host.draft._cancel.is_set())
                release.set()
                second.join(2)
            self.assertIsNone(results['first'])
            self.assertEqual(results['second'].status, 'empty')
        finally:
            release.set()
            first.join(2)
            if second.ident is not None:
                second.join(2)

    def host_class(self):
        self.assertIsNotNone(importlib.util.find_spec('astronomy_host'), 'Background astronomy host absent')
        from astronomy_host import AstronomyHost
        return AstronomyHost

    def config(self, revision=1):
        from config_store import DEFAULT_CONFIG
        config = copy.deepcopy(DEFAULT_CONFIG)
        config['revision'] = revision
        config['astronomy']['timezone'] = 'Europe/London'
        # Refresh-boundary assertions need the dated, scrolling Advanced labels;
        # the Simple view (phase name only) is covered by the renderer tests.
        config['astronomy']['view'] = 'advanced'
        for row in config['playlist']:
            if row['module'] == 'astronomy':
                row['enabled'] = True
        return config

    def test_real_background_snapshot_location_preview_and_cleanup(self):
        host = self.host_class()(utc_clock=lambda: dt.datetime(2024, 4, 20, tzinfo=dt.timezone.utc))
        self.addCleanup(host.close)
        config = self.config()
        host.sync(config)
        snapshot = host.saved.wait(threading.Event(), timeout=10)
        self.assertEqual(snapshot.status, 'ready', snapshot.error)
        self.assertEqual(len(snapshot.cards), 1)
        self.assertEqual(snapshot.location[2], 'Europe/London')
        self.assertIn('weather', snapshot.location[4].lower())
        self.assertIsInstance(snapshot.cards, tuple)
        self.assertGreater(snapshot.duration, 10)
        self.assertIs(host.preview(config), snapshot)
        self.assertIs(host.preview(config), snapshot)  # non-consuming
        host.close()
        self.assertFalse(host.saved.running)
        self.assertFalse(host.draft.running)

    def test_generation_fences_old_completion_stale_save_and_disabled_work(self):
        entered, release = threading.Event(), threading.Event()
        stops = []
        def prepare(settings, location, now, stop):
            stops.append(stop)
            entered.set()
            release.wait(5)
            return ()
        host = self.host_class()(preparer=prepare)
        self.addCleanup(host.close)
        try:
            old = self.config(1)
            host.sync(old)
            self.assertTrue(entered.wait(1))
            newer = self.config(2)
            newer['astronomy']['current_moon'] = False
            host.sync(newer)
            self.assertTrue(stops[0].is_set())
            host.sync(old)  # out-of-order completed Save must not reactivate
            self.assertEqual(host.saved.peek().status, 'disabled')
            generation = host.saved.peek().generation
            release.set()
            host.saved.wait_idle(2)
            self.assertEqual(host.saved.peek().generation, generation)
            self.assertEqual(host.saved.peek().cards, ())
            self.assertEqual(len(stops), 1)
        finally:
            release.set()

    def test_cold_draft_waits_cancellably_and_does_not_consume_saved(self):
        entered, release = threading.Event(), threading.Event()
        def prepare(settings, location, now, stop):
            entered.set()
            release.wait(5)
            return ()
        host = self.host_class()(preparer=prepare)
        self.addCleanup(host.close)
        config = self.config()
        try:
            first = host.preview(config)
            self.assertEqual(first.status, 'preparing')
            self.assertTrue(entered.wait(1))
            cancel = threading.Event()
            cancel.set()
            preparation_stop = host.draft._cancel
            self.assertIsNone(host.wait_preview(config, cancel))
            self.assertTrue(preparation_stop.is_set(), 'Cancelled preview left numerical work running')
            self.assertEqual(host.saved.peek().status, 'disabled')
            release.set()
            ready = host.wait_preview(config, threading.Event())
            self.assertEqual(ready.status, 'empty')
            self.assertIs(host.preview(config), ready)
        finally:
            release.set()


class SettingsTests(unittest.TestCase):
    def test_settings_persist_explicit_location_and_reject_unsupported_fields(self):
        from config_store import ConfigStore
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / 'config.json')
            self.assertIn('astronomy', store.get(), 'Astronomy settings not integrated')
            settings = store.get()['astronomy']
            self.assertEqual(settings['timezone'], '')  # blank = follow the Weather timezone
            self.assertEqual(settings['location_source'], 'weather')
            saved = store.update({'astronomy': {'timezone': 'Europe/London', 'lunar_events': True}})
            self.assertEqual(ConfigStore(store.path).get(), saved)
            before = store.path.read_bytes()
            for changes in ({'timezone': 'Not/AZone'}, {'latitude': True}, {'event_limit': 51},
                            {'title_font': 'not-installed'}, {'meteor_showers': True}):
                with self.assertRaises(ValueError):
                    store.update({'astronomy': changes})
                self.assertEqual(store.path.read_bytes(), before)
            rows = [r for r in saved['playlist'] if r['module'] != 'astronomy']
            store.update({'playlist': rows})
            self.assertEqual(ConfigStore(store.path).get()['playlist'], rows)

    def test_view_defaults_simple_projects_old_saves_and_persists_advanced(self):
        from config_store import ConfigStore
        from astronomy_config import DEFAULTS, validate
        self.assertEqual(DEFAULTS['view'], 'simple')
        with self.assertRaises(ValueError):
            validate(dict(DEFAULTS, view='fancy'))
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / 'config.json')
            saved = store.update({'astronomy': {'timezone': 'Europe/London', 'moon_colour': '#ABCDEF'}})
            # A save written before the toggle existed has no 'view' key.
            old = json.loads(store.path.read_text())
            old['astronomy'].pop('view')
            store.path.write_text(json.dumps(old))
            before = store.path.read_bytes()
            projected = ConfigStore(store.path).get()
            self.assertEqual(projected['astronomy'], saved['astronomy'])
            self.assertEqual(store.path.read_bytes(), before)  # read never rewrites
            advanced = ConfigStore(store.path).update({'astronomy': {'view': 'advanced'}})
            self.assertEqual(advanced['astronomy']['moon_colour'], '#ABCDEF')
            self.assertEqual(ConfigStore(store.path).get()['astronomy']['view'], 'advanced')

    def test_role_colours_default_white_and_old_saves_inherit_text_colour(self):
        # Owner 2026-10-12: separate title/date/description colours. A save made before
        # they existed keeps its look: missing role colours project from its text_colour.
        from unittest.mock import patch
        import config_store
        from config_store import ConfigStore, DEFAULT_CONFIG, _project_persisted_screen_rows, _serialize_config
        from astronomy_config import DEFAULTS, ROLE_COLOURS, validate
        self.assertEqual(ROLE_COLOURS, ('title_colour', 'date_colour', 'description_colour'))
        self.assertTrue(all(DEFAULTS[k] == '#FFFFFF' for k in ROLE_COLOURS))
        for key in ROLE_COLOURS:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate(dict(DEFAULTS, **{key: 'blue'}))
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / 'config.json')
            saved = store.update({'astronomy': {'timezone': 'Europe/London', 'text_colour': '#FFCC00'}})
            old = json.loads(store.path.read_text())
            for key in ROLE_COLOURS:
                old['astronomy'].pop(key)
            store.path.write_text(json.dumps(old))
            before = store.path.read_bytes()
            projected = ConfigStore(store.path).get()['astronomy']
            self.assertEqual({k: projected[k] for k in ROLE_COLOURS}, dict.fromkeys(ROLE_COLOURS, '#FFCC00'))
            self.assertEqual(store.path.read_bytes(), before)  # read never rewrites
            updated = ConfigStore(store.path).update({'astronomy': {'date_colour': '#00FF00'}})['astronomy']
            self.assertEqual((updated['title_colour'], updated['date_colour'], updated['description_colour']),
                             ('#FFCC00', '#00FF00', '#FFCC00'))
            self.assertEqual(ConfigStore(store.path).get()['astronomy'], updated)
        # Pre-v4 migration inherits text_colour too (not the white default).
        for schema in (2, 3):
            with self.subTest(schema=schema), tempfile.TemporaryDirectory() as directory:
                legacy = json.loads(json.dumps(DEFAULT_CONFIG))
                legacy['schema_version'] = schema
                legacy['astronomy'] = {k: v for k, v in legacy['astronomy'].items() if k not in ROLE_COLOURS}
                legacy['astronomy']['text_colour'] = '#ABCDEF'
                path = Path(directory) / 'config.json'
                path.write_text(json.dumps(legacy))
                migrated = ConfigStore(path).get()['astronomy']
                self.assertEqual({k: migrated[k] for k in ROLE_COLOURS}, dict.fromkeys(ROLE_COLOURS, '#ABCDEF'))
        # Size-limit compaction drops a role colour only when it equals text_colour (lossless).
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config['astronomy'] = dict(config['astronomy'], text_colour='#111111', title_colour='#111111',
                                   date_colour='#FFFFFF', description_colour='#111111')
        with patch.object(config_store, 'CONFIG_SNAPSHOT_LIMIT', 0):
            stored = json.loads(_serialize_config(config))
        self.assertNotIn('title_colour', stored['astronomy'])
        self.assertNotIn('description_colour', stored['astronomy'])
        self.assertEqual(stored['astronomy']['date_colour'], '#FFFFFF')
        self.assertEqual(_project_persisted_screen_rows(stored)['astronomy'], config['astronomy'])

    def test_capacity_predecessor_with_both_nondefault_modules_is_preserved(self):
        from config_store import ConfigStore, DEFAULT_CONFIG, CONFIG_SNAPSHOT_LIMIT
        encode = lambda v: json.dumps(v, sort_keys=True, separators=(',', ':')).encode()
        for count in (255, 256):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                old = copy.deepcopy(DEFAULT_CONFIG)
                old.pop('astronomy', None)
                old['rss']['count'] = 3
                old['event_countdown']['layout'] = 'icon'
                old['playlist'] = [dict(module='clock_lava', duration=i+1, enabled=False) for i in range(count)]
                old['device']['name'] += ' ' * (CONFIG_SNAPSHOT_LIMIT - len(encode(old)))
                path = Path(directory) / 'config.json'
                path.write_bytes(encode(old))
                before = path.read_bytes()
                store = ConfigStore(path)
                self.assertIn('astronomy', store.get(), 'Astronomy predecessor projection absent')
                self.assertEqual(store.get()['playlist'], old['playlist'])
                self.assertEqual(path.read_bytes(), before)
                # The revision gains no digit at this first save.
                saved = store.update({})
                self.assertLessEqual(path.stat().st_size, CONFIG_SNAPSHOT_LIMIT)
                self.assertEqual(ConfigStore(path).get(), saved)


class WebTests(unittest.TestCase):
    def test_device_admission_retains_page_owner_against_edit_and_timing(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from device_preview import PreviewAdmissionStatus
        f = self.f
        f.pair_browser()
        host = f.app.extensions['astronomy_host']
        entered, release, cancel = threading.Event(), threading.Event(), threading.Event()
        callback = {}
        def prepare(*args):
            entered.set()
            release.wait(5)
            return ()
        def submit(request, *, renderer):
            callback.update(request=request, renderer=renderer)
            return SimpleNamespace(status=PreviewAdmissionStatus.ACCEPTED,
                lease=SimpleNamespace(connectivity_deadline=99999999999))
        with patch.object(host.draft, '_prepare', side_effect=prepare), patch.object(f.controller, 'submit_device_draft', side_effect=submit):
            response = f.paired_request('post', '/api/device-preview/astronomy', json={
                'config': {'astronomy': {'timezone': 'Europe/London'}},
                'generation': 1, 'preview_instance': 'd'*32})
            self.assertEqual(response.status_code, 202)
            worker = threading.Thread(target=lambda: callback['renderer'](f.controller.matrix, callback['request'], stop_event=cancel))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                claims = list(host.draft._owners)
                self.assertEqual(claims[0][0][0], 'device')
                self.assertEqual(claims[0][0][2], 'd'*32)
                stop = host.draft._cancel
                changed = dict(f.store.get()['astronomy'], timezone='Europe/London', moon_hold_seconds=11)
                response = f.paired_request('post', '/api/scroll-timing/astronomy', json={
                    'base_revision': f.store.get()['revision'], 'settings': changed, 'preview_instance': 't'*32})
                # Hex page IDs use the same validator as edit and device preview.
                self.assertEqual(response.status_code, 400)
                response = f.paired_request('post', '/api/scroll-timing/astronomy', json={
                    'base_revision': f.store.get()['revision'], 'settings': changed, 'preview_instance': 'b'*32})
                self.assertEqual(response.status_code, 429)
                response = f.paired_request('post', '/api/edit-preview/astronomy', json={
                    'config': {'astronomy': changed}, 'generation': 1, 'preview_instance': 'a'*32})
                self.assertEqual(response.status_code, 429, response.json)
                self.assertFalse(stop.is_set())
                release.set()
                worker.join(2)
                self.assertFalse(worker.is_alive())
                f.controller.render_device_draft.assert_called_once()
            finally:
                release.set()
                cancel.set()
                worker.join(2)
                callback['request'].cleanup()

    def test_continuous_device_pass_refreshes_ready_and_empty_results(self):
        from unittest.mock import Mock, patch
        from astronomy_host import AstronomyHost, _prepare
        from display_controller import DisplayController
        from device_preview import DevicePreviewOwner, DevicePreviewRequest
        for initially_empty in (False, True):
            with self.subTest(initially_empty=initially_empty):
                clock = [dt.datetime(2024, 4, 8, 12, tzinfo=dt.timezone.utc)]
                config = self.f.store.get()
                config['astronomy'].update(timezone='Europe/London', location_source='override', latitude=51.4779, longitude=0)
                calls = []
                def prepare(settings, location, now, stop):
                    calls.append(now)
                    return () if initially_empty and len(calls) == 1 else _prepare(settings, location, now, stop)
                host = AstronomyHost(preparer=prepare, utc_clock=lambda: clock[0])
                controller = DisplayController(Mock(), self.f.store)
                controller.astronomy_host = host
                stop = threading.Event()
                owner = DevicePreviewOwner('device', 'page')
                request = DevicePreviewRequest(owner, 1, {'module': 'astronomy', 'candidate': config}, timeout_seconds=None)
                snapshots = []
                def render(matrix, candidate, **options):
                    snapshots.append(options['prepared'])
                    if len(snapshots) == 1:
                        clock[0] += dt.timedelta(days=1)
                    else:
                        stop.set()
                    return bool(options['prepared'].cards)
                try:
                    first = host.wait_preview(config, threading.Event())
                    with patch('modules.astronomy_display.run', side_effect=render):
                        controller.render_device_draft(controller.matrix, request, stop_event=stop, prepared=first)
                    self.assertEqual(len(snapshots), 2)
                    self.assertGreater(snapshots[1].generation, snapshots[0].generation)
                    self.assertEqual(snapshots[1].prepared_at, clock[0])
                    control = AstronomyHost(utc_clock=lambda: clock[0])
                    try:
                        self.assertEqual(snapshots[1].cards, control.wait_preview(config, threading.Event()).cards)
                    finally:
                        control.close()
                    self.assertEqual(host.saved.peek().status, 'disabled')
                finally:
                    stop.set()
                    controller.stop()
                    host.close()

    def test_timing_uses_validated_unsaved_weather_and_retains_override(self):
        from unittest.mock import patch
        f = self.f
        f.pair_browser()
        config = f.store.get()
        before = f.store.path.read_bytes()
        settings = config['astronomy']
        settings['timezone'] = 'Europe/London'
        weather = dict(config['weather'], latitude=-33.8688, longitude=151.2093, location='Sydney draft')
        payload = dict(base_revision=config['revision'], settings=settings, weather=weather)
        host = f.app.extensions['astronomy_host']
        with patch.object(host.draft, '_prepare', return_value=()):
            response = f.paired_request('post', '/api/scroll-timing/astronomy', json=payload)
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(host.draft.peek().location[:3], (-33.8688, 151.2093, 'Europe/London'))
            self.assertEqual(host.draft.peek().location[4], 'Weather coordinates; explicit astronomy timezone')
            host.draft.wait(threading.Event())
            settings.update(location_source='override', latitude=10, longitude=20)
            response = f.paired_request('post', '/api/scroll-timing/astronomy', json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(host.draft.peek().location[:3], (10, 20, 'Europe/London'))
            for invalid in ([], dict(weather, latitude=999), dict(weather, longitude=True)):
                response = f.paired_request('post', '/api/scroll-timing/astronomy', json=dict(payload, weather=invalid))
                self.assertEqual(response.status_code, 400)
            response = f.paired_request('post', '/api/scroll-timing/astronomy', json=dict(payload, base_revision=-1))
            self.assertEqual(response.status_code, 412)
            # MR !159 review finding 2b: a pre-deploy page omits weather.timezone.
            stale = {k: v for k, v in weather.items() if k != 'timezone'}
            settings.update(location_source='weather', timezone='')
            response = f.paired_request('post', '/api/scroll-timing/astronomy', json=dict(payload, weather=stale))
            self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(f.store.path.read_bytes(), before)

    @unittest.skipUnless(os.environ.get('MATRIX_CHROMIUM'), 'Chromium required')
    def test_browser_weather_search_calculate_and_invalidation(self):
        import io
        from PIL import Image
        f = self.f
        f.controller.status.return_value = {'current_module': None, 'running': False}
        frame = io.BytesIO()
        Image.new('RGB', (64, 32)).save(frame, format='PNG')
        f.controller.frame_png.side_effect = lambda: (io.BytesIO(frame.getvalue()), 0)
        f.location_search.return_value = [dict(label='Sydney draft', latitude=-33.8688, longitude=151.2093)]
        source = Path(__file__).with_name('astronomy_browser.cjs').read_text()
        prefix = source[:source.index(" await click('[data-module=\"astronomy\"] button:last-child');")]
        probe = prefix + r'''
 await click('[data-module="astronomy"] button:last-child');
 await change('[data-astronomy-setting="timezone"]','Europe/London');
 await evaluate(`(()=>{const original=window.fetch;window.astronomyRequests=[];window.fetch=(url,options)=>{if(url==='/api/scroll-timing/astronomy')window.astronomyRequests.push(JSON.parse(options.body));return original(url,options)};return true})()`);
 await change('#weather-location','Sydney');
 await waitFor(`!!document.querySelector('#weather-results [role="option"]')`);
 await click('#weather-results [role="option"]');
 await click('[data-astronomy-calculate]');
 await waitFor(`document.querySelector('[data-astronomy-timing]').textContent.includes('ready')`);
 const result=await evaluate(`({request:window.astronomyRequests[0],draftWeather:configBody().weather,unsaved:matrixHasUnsavedDraft()})`);
 await evaluate(`resetBuiltInModule('weather');true`);
 result.resetInvalidated=await evaluate(`astronomyTiming===null`);
 await change('#weather-location','Sydney');
 await waitFor(`!!document.querySelector('#weather-results [role="option"]')`);
 await click('#weather-results [role="option"]');
 await click('[data-astronomy-calculate]');
 await waitFor(`document.querySelector('[data-astronomy-timing]').textContent.includes('ready')`);
 await evaluate(`(()=>{const original=window.fetch;window.fetch=(url,options)=>{const promise=original(url,options);return url==='/api/scroll-timing/astronomy'?promise.then(response=>new Promise(resolve=>{window.releaseAstronomyTiming=()=>resolve(response)})):promise};return true})()`);
 await click('[data-astronomy-calculate]');
 await waitFor(`typeof window.releaseAstronomyTiming==='function'`);
 await change('#weather-location','changed');
 await evaluate(`window.releaseAstronomyTiming();true`);
 await sleep(100);
 result.invalidated=await evaluate(`astronomyTiming===null&&document.querySelector('[data-astronomy-timing]').textContent.includes('changed')`);
 result.errors=errors;
 console.log(JSON.stringify(result));
}finally{socket.close()}
})().catch(error=>{console.error(error.stack);process.exitCode=1});
'''
        before = f.store.path.read_bytes()
        result = f.run_live_chromium_probe(probe, timeout=45)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['request'].get('weather'), result['draftWeather'])
        self.assertEqual(result['request']['settings']['timezone'], 'Europe/London')
        self.assertRegex(result['request']['preview_instance'], r'^[0-9a-f]{32}$')
        self.assertEqual(f.app.extensions['astronomy_host'].draft.peek().location[:3], (-33.8688, 151.2093, 'Europe/London'))
        self.assertTrue(result['invalidated'])
        self.assertTrue(result['resetInvalidated'])
        self.assertTrue(result['unsaved'])
        self.assertEqual(f.store.path.read_bytes(), before)

    def test_timing_cannot_supersede_admitted_edit_preparation(self):
        from unittest.mock import patch
        f = self.f
        f.pair_browser()
        host = f.app.extensions['astronomy_host']
        entered, release = threading.Event(), threading.Event()
        result = {}
        def prepare(*args):
            entered.set()
            release.wait(5)
            return ()
        def edit():
            result['response'] = f.paired_request('post', '/api/edit-preview/astronomy', json={
                'config': {'astronomy': {'timezone': 'Europe/London'}},
                'generation': 1, 'preview_instance': 'a'*32})
        with patch.object(host.draft, '_prepare', side_effect=prepare):
            worker = threading.Thread(target=edit)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                settings = f.store.get()['astronomy']
                settings.update(timezone='Europe/London', moon_hold_seconds=11)
                timing = f.paired_request('post', '/api/scroll-timing/astronomy', json={
                    'base_revision': f.store.get()['revision'], 'settings': settings})
                self.assertEqual(timing.status_code, 429, timing.json)
                self.assertTrue(timing.json['retryable'])
            finally:
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result['response'].status_code, 200, result['response'].json)

    def test_bounded_settings_fit_timing_envelope_and_all_disabled_is_empty(self):
        f = self.f
        f.pair_browser()
        config = f.store.get()
        settings = config['astronomy']
        from zoneinfo import available_timezones
        settings.update(location_source='override', location_label='X'*200, timezone=max(available_timezones(), key=len),
            current_moon=False, lunar_events=False, horizon_days=366, event_limit=50)
        payload = {'base_revision': config['revision'], 'settings': settings}
        self.assertLess(len(json.dumps(payload).encode()), 16*1024)
        response = f.paired_request('post', '/api/scroll-timing/astronomy', json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['status'], 'disabled')
        self.assertEqual(response.json['duration_seconds'], 0)
        response = f.paired_request('post', '/api/scroll-timing/astronomy', data=' '*(16*1024+1), content_type='application/json')
        self.assertEqual(response.status_code, 413)

    @unittest.skipUnless(os.environ.get('MATRIX_CHROMIUM'), 'Chromium required')
    def test_browser_native_save_reload_preview_and_responsive_editor(self):
        import io
        from PIL import Image
        self.f.controller.status.return_value = {'current_module': None, 'running': False}
        frame = io.BytesIO()
        Image.new('RGB', (64, 32)).save(frame, format='PNG')
        self.f.controller.frame_png.side_effect = lambda: (io.BytesIO(frame.getvalue()), 0)
        result = self.f.run_live_chromium_probe(Path(__file__).with_name('astronomy_browser.cjs').read_text(), timeout=60)
        self.assertEqual(result['errors'], [])
        self.assertEqual(self.f.store.get()['astronomy'], result['saved'])

    def setUp(self):
        from tests.test_webapp import WebAppTests
        self.f = WebAppTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.pairing.verify.return_value = True

    def test_routes_save_timing_preview_and_controller_share_real_preparation(self):
        from unittest.mock import Mock, patch
        from display_controller import DisplayController
        f = self.f
        self.assertEqual(f.client.get('/api/astronomy/status').status_code, 401)
        f.pair_browser()
        revision = f.store.get()['revision']
        response = f.paired_request('post', '/api/config', headers={'If-Match': f'"{revision}"'},
            json={'astronomy': {'timezone': 'Europe/London'}})
        self.assertEqual(response.status_code, 200)
        host = f.app.extensions['astronomy_host']
        before = f.store.path.read_bytes()
        config = f.store.get()
        snapshot = host.wait_preview(config, threading.Event())
        self.assertEqual(snapshot.status, 'ready', snapshot.error)
        timing = f.paired_request('post', '/api/scroll-timing/astronomy', json={
            'base_revision': config['revision'], 'settings': config['astronomy']})
        self.assertEqual(timing.status_code, 200, timing.get_data(as_text=True))
        self.assertEqual(timing.json['duration_seconds'], snapshot.duration)
        preview = f.paired_request('post', '/api/edit-preview/astronomy', json={
            'config': {}, 'generation': 1, 'preview_instance': 'e'*32})
        self.assertEqual(preview.status_code, 200, preview.get_data(as_text=True))
        self.assertTrue(preview.json['frames'])
        self.assertIs(host.preview(config), snapshot)
        self.assertEqual(f.store.path.read_bytes(), before)
        controller = DisplayController(Mock(), f.store)
        controller.astronomy_host = host
        self.addCleanup(controller.stop)
        # Preparation stays real; virtualize only the slow physical card hold.
        with patch('modules.astronomy_renderer.run', return_value=1) as render:
            self.assertTrue(controller._execute('astronomy', config, 15))
            self.assertEqual(len(render.call_args.args[1]), 1)
        self.assertEqual(f.store.path.read_bytes(), before)

    def test_cold_device_callback_waits_for_ready_and_cancel_does_not_draw(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from device_preview import PreviewAdmissionStatus
        f = self.f
        self.assertIn('astronomy_host', f.app.extensions, 'Application astronomy host absent')
        f.pair_browser()
        host = f.app.extensions['astronomy_host']
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = host.draft._prepare
        def prepare(*args):
            entered.set()
            release.wait(5)
            return original(*args)
        callback = {}
        def submit(request, *, renderer):
            callback.update(request=request, renderer=renderer)
            return SimpleNamespace(status=PreviewAdmissionStatus.ACCEPTED,
                lease=SimpleNamespace(connectivity_deadline=99999999999))
        cancel = threading.Event()
        with patch.object(host.draft, '_prepare', side_effect=prepare), patch.object(f.controller, 'submit_device_draft', side_effect=submit):
            response = f.paired_request('post', '/api/device-preview/astronomy', json={
                'config': {'astronomy': {'timezone': 'Europe/London'}},
                'generation': 1, 'preview_instance': 'd'*32})
            self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
            def run():
                callback['renderer'](f.controller.matrix, callback['request'], stop_event=cancel)
                finished.set()
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertFalse(finished.wait(.1), 'Cold device lease discarded before preparation')
                f.controller.render_device_draft.assert_not_called()
                cancel.set()
                self.assertTrue(finished.wait(1))
                f.controller.render_device_draft.assert_not_called()
            finally:
                release.set()
                worker.join(2)
                callback['request'].cleanup()


class WeatherTimezoneDefaultTests(unittest.TestCase):
    """Owner: Astronomy timezone defaults to the Weather location's; user may override."""

    def test_blank_astronomy_timezone_follows_weather_and_explicit_overrides(self):
        from astronomy_config import DEFAULTS, location_values
        from config_store import DEFAULT_CONFIG
        self.assertEqual(DEFAULTS['timezone'], '')
        self.assertEqual(DEFAULT_CONFIG['weather']['timezone'], 'Europe/London')
        weather = dict(DEFAULT_CONFIG['weather'], latitude=42.24, longitude=-8.72,
                       location='Vigo, Galicia, ES', timezone='Europe/Madrid')
        follow = location_values({'astronomy': dict(DEFAULTS), 'weather': weather})
        self.assertEqual(follow[:4], (42.24, -8.72, 'Europe/Madrid', 'Vigo, Galicia, ES'))
        self.assertIn('Weather timezone', follow[4])
        own = location_values({'astronomy': dict(DEFAULTS, timezone='Asia/Tokyo'), 'weather': weather})
        self.assertEqual(own[2], 'Asia/Tokyo')
        override = location_values({'astronomy': dict(DEFAULTS, location_source='override', latitude=1, longitude=2),
                                    'weather': weather})
        self.assertEqual(override[:3], (1, 2, 'Europe/Madrid'))
        self.assertIsNone(location_values({'astronomy': dict(DEFAULTS), 'weather': dict(weather, timezone='')}))

    def test_weather_timezone_validated_and_projected_for_old_saves_without_rewrite(self):
        from config_store import ConfigStore, DEFAULT_CONFIG
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / 'config.json')
            for bad in ('Not/AZone', 5, 'x' * 101):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    store.update({'weather': {'timezone': bad}})
            saved = store.update({'weather': {'location': 'Vigo, Galicia, ES', 'latitude': 42.24,
                                              'longitude': -8.72, 'timezone': 'Europe/Madrid'}})
            self.assertEqual(ConfigStore(store.path).get()['weather']['timezone'], 'Europe/Madrid')
            self.assertEqual(saved['openweather_overview'].get('timezone'), None)
            # Saves written before weather.timezone existed.
            for coords, expected in (((51.5074, -0.1278), 'Europe/London'), ((42.24, -8.72), '')):
                old = json.loads(store.path.read_text())
                old['weather'].pop('timezone', None)
                old['weather']['latitude'], old['weather']['longitude'] = coords
                store.path.write_text(json.dumps(old))
                before = store.path.read_bytes()
                with self.subTest(coords=coords):
                    self.assertEqual(ConfigStore(store.path).get()['weather']['timezone'], expected)
                    self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(ConfigStore(store.path).update({})['weather']['timezone'], '')

    def test_pre_v4_migration_projects_weather_timezone_instead_of_default(self):
        # Second review MR !159 finding 1: migration must not stamp London on other places.
        from config_store import ConfigStore, DEFAULT_CONFIG
        for schema in (2, 3):
            for coords, expected in (((35.7, 139.7), ''), ((51.5074, -0.1278), 'Europe/London')):
                with self.subTest(schema=schema, coords=coords), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'config.json'
                    legacy = json.loads(json.dumps(DEFAULT_CONFIG))
                    legacy['schema_version'] = schema
                    legacy['weather'].pop('timezone')
                    legacy['weather']['latitude'], legacy['weather']['longitude'] = coords
                    path.write_text(json.dumps(legacy))
                    self.assertEqual(ConfigStore(path).get()['weather']['timezone'], expected)
                    self.assertEqual(json.loads(path.read_text())['weather']['timezone'], expected)

    def test_moving_weather_without_timezone_clears_stale_zone(self):
        # MR !159 review finding 2a: coordinates changed without a zone must not keep the old one.
        from config_store import ConfigStore
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / 'config.json')
            self.assertEqual(store.get()['weather']['timezone'], 'Europe/London')
            vigo = {'location': 'Vigo', 'latitude': 42.24, 'longitude': -8.72}
            self.assertEqual(store.validate_candidate({'weather': vigo})['weather']['timezone'], '')
            self.assertEqual(store.update({'weather': vigo})['weather']['timezone'], '')
            self.assertEqual(store.update({'weather': dict(vigo, timezone='Europe/Madrid')})['weather']['timezone'],
                             'Europe/Madrid')
            # Same coordinates or unrelated weather edits keep the chosen zone.
            self.assertEqual(store.update({'weather': {'location': 'Vigo, ES'}})['weather']['timezone'], 'Europe/Madrid')
            self.assertEqual(store.update({'weather': dict(vigo)})['weather']['timezone'], 'Europe/Madrid')

    def test_size_limit_compaction_drops_only_reconstructible_projected_fields(self):
        # Second reviews MR !157 #1 / MR !159 #4: compaction branch must be lossless.
        from unittest.mock import patch
        import config_store
        from config_store import DEFAULT_CONFIG, _project_persisted_screen_rows, _serialize_config
        cases = (
            ({}, {}, None, None),  # London default zone, simple view: both reconstructed
            ({'latitude': 42.24, 'longitude': -8.72, 'timezone': 'Europe/Madrid'}, {'view': 'advanced'},
             'Europe/Madrid', 'advanced'),
        )
        for weather, astronomy, kept_zone, kept_view in cases:
            config = json.loads(json.dumps(DEFAULT_CONFIG))
            config['weather'].update(weather)
            config['astronomy'] = dict(config['astronomy'], enabled=True, **astronomy)
            with self.subTest(weather=weather), patch.object(config_store, 'CONFIG_SNAPSHOT_LIMIT', 0):
                stored = json.loads(_serialize_config(config))
            self.assertEqual(stored['weather'].get('timezone'), kept_zone)
            self.assertEqual(stored['astronomy'].get('view'), kept_view)
            restored = _project_persisted_screen_rows(stored)
            self.assertEqual(restored['weather'], config['weather'])
            self.assertEqual(restored['astronomy'], config['astronomy'])

    def test_location_search_carries_provider_timezone(self):
        from unittest.mock import patch
        from modules.weather_app import OpenMeteoClient
        result = {'results': [
            {'name': 'Vigo', 'admin1': 'Galicia', 'country_code': 'ES', 'latitude': 42.24, 'longitude': -8.72,
             'timezone': 'Europe/Madrid'},
            {'name': 'Nowhere', 'country_code': 'XX', 'latitude': 1, 'longitude': 2, 'timezone': 'Bad/Zone'},
            {'name': 'Plain', 'country_code': 'YY', 'latitude': 3, 'longitude': 4}]}
        with patch.object(OpenMeteoClient, '_get_json', return_value=result):
            places = OpenMeteoClient().search_locations('x')
        self.assertEqual([p['timezone'] for p in places], ['Europe/Madrid', '', ''])
