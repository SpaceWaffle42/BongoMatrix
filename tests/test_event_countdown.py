"""Focused local-event acceptance; no network or physical device required."""
import copy
import base64
import datetime as dt
import importlib
import io
import json
import tempfile
import shutil
import os
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from PIL import Image
from config_store import ConfigStore, DEFAULT_CONFIG
from webapp import _edit_preview_iterator


def event(**changes):
    result = dict(id='launch', title='Launch', start='2030-01-01T12:00',
                  end=None, all_day=False, annual=False, enabled=True,
                  now_seconds=900, accent=None, icon='calendar')
    result.update(changes)
    return result


class CountdownTests(unittest.TestCase):
    def test_default_omission_roundtrips_explicit_playlist_removal(self):
        from config_store import CONFIG_SNAPSHOT_LIMIT
        encode = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            store = ConfigStore(path)
            candidate = store.get()
            remaining = [dict(module='clock_lava', duration=15, enabled=True)]
            candidate['playlist'] = remaining
            candidate['device']['name'] += ' ' * (CONFIG_SNAPSHOT_LIMIT + 1 - len(encode(candidate)))
            saved = store.update({'device': candidate['device'], 'playlist': [*remaining,
                dict(module='event_countdown', duration=15, enabled=False)]}, base_revision=0)
            saved = store.update({'playlist': remaining}, base_revision=saved['revision'])
            self.assertEqual(saved['revision'], 2)
            self.assertEqual(len(encode(saved)), CONFIG_SNAPSHOT_LIMIT + 1)
            before = path.read_bytes()
            self.assertLessEqual(len(before), CONFIG_SNAPSHOT_LIMIT)
            self.assertNotIn('event_countdown', json.loads(before))
            reopened = ConfigStore(path).get()
            self.assertEqual(reopened, saved, 'omitted defaults must not resurrect a removed row')
            self.assertEqual(path.read_bytes(), before)
            saved = ConfigStore(path).update({}, base_revision=2)
            self.assertEqual(saved['playlist'], remaining)
            self.assertEqual(ConfigStore(path).get(), saved)

    def test_predecessor_row_introduction_requires_full_reconstructed_budget(self):
        from config_store import CONFIG_SNAPSHOT_LIMIT, validate_config
        encode = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        for count in (255, 256):
            for spare in (None, 0, 60, 300, 304):
                with self.subTest(count=count, spare=spare), tempfile.TemporaryDirectory() as directory:
                    predecessor = copy.deepcopy(DEFAULT_CONFIG)
                    predecessor.pop('event_countdown')
                    rows = [dict(module='clock_lava', duration=i + 1, enabled=bool(i % 2))
                            for i in range(count)]
                    predecessor['playlist'] = copy.deepcopy(rows)
                    if spare is not None:
                        predecessor['device']['name'] += ' ' * (
                            CONFIG_SNAPSHOT_LIMIT - spare - len(encode(predecessor)))
                    validate_config(copy.deepcopy(predecessor), enforce_save_bounds=True)
                    path = Path(directory) / 'config.json'
                    path.write_bytes(encode(predecessor))
                    before = path.read_bytes()
                    store = ConfigStore(path)
                    projected = store.get()
                    expected = copy.deepcopy(rows)
                    # Full defaults plus a row: 303 bytes before holiday packs; derive the rest.
                    pack_bytes = len(encode({k: DEFAULT_CONFIG['event_countdown'][k] for k in ('packs', 'holiday_dates', 'hidden_holidays')})) - 2 + 1
                    if count == 255 and (spare is None or spare >= 303 + pack_bytes):
                        expected.append(dict(module='event_countdown', duration=15, enabled=False))
                    self.assertEqual(projected['playlist'], expected)
                    self.assertEqual(path.read_bytes(), before)
                    saved = store.update({}, base_revision=0)
                    self.assertEqual(saved['playlist'], expected)
                    self.assertLessEqual(path.stat().st_size, CONFIG_SNAPSHOT_LIMIT)
                    self.assertEqual(ConfigStore(path).get(), saved)

    def test_annual_timed_intervals_preserve_explicit_fold_endpoints(self):
        from modules.event_countdown import select_event
        utc = dt.timezone.utc
        cases = [
            ('02:15+01:00', '02:45+01:00', '02:45+02:00'),
            ('02:15+02:00', '02:45+01:00', '02:45+02:00'),
            # These are positive elapsed intervals despite nonpositive wall deltas.
            ('02:45+02:00', '02:15+01:00', '03:15+02:00'),
            ('02:15+02:00', '02:15+01:00', '03:15+02:00'),
        ]
        for start, end, future_end in cases:
            with self.subTest(start=start, end=end), tempfile.TemporaryDirectory() as directory:
                store = ConfigStore(Path(directory) / 'config.json')
                store.update({'event_countdown': {'timezone': 'Europe/Berlin',
                    'events': [event(start='2026-10-25T' + start,
                                     end='2026-10-25T' + end, annual=True)]}})
                settings = ConfigStore(store.path).get()['event_countdown']
                for year in (2026, 2027, 2037):
                    with self.subTest(year=year):
                        expected_start = dt.datetime.fromisoformat(
                            f'{year}-10-25T' + (start[:5] + '+02:00' if year == 2027 else start))
                        expected_end = dt.datetime.fromisoformat(
                            f'{year}-10-25T' + (future_end if year == 2027 else end))
                        start_utc, end_utc = expected_start.astimezone(utc), expected_end.astimezone(utc)
                        for now in (start_utc, end_utc - dt.timedelta(seconds=1)):
                            selected = select_event(settings, now=now)
                            assert selected is not None, 'valid positive elapsed interval was discarded'
                            self.assertEqual(selected['start'].astimezone(utc), start_utc)
                            self.assertEqual(selected['end'].astimezone(utc), end_utc)
                            self.assertEqual(selected['seconds'], 0)
                        later = select_event(settings, now=end_utc)
                        self.assertTrue(later is None or later['start'].astimezone(utc) > start_utc)

    def test_annual_intervals_preserve_local_calendar_duration(self):
        from modules.event_countdown import select_event, local_time
        from event_countdown_config import validate
        from zoneinfo import ZoneInfo
        cases = [
            ('2024-02-28', '2024-02-29', True),
            ('2024-02-29', '2024-03-01', True),
            ('2024-02-28T23:00', '2024-02-29T01:00', False),
            ('2024-02-29T23:00', '2024-03-01T01:00', False),
            ('2024-02-28T18:00', '2024-03-02T09:00', False),
            ('2024-12-31T23:00', '2025-01-02T01:00', False),
            ('2024-03-30', '2024-04-01', True),
        ]
        for zone_name in ('UTC', 'Europe/Berlin'):
            zone = ZoneInfo(zone_name)
            for start, end, all_day in cases:
                for year in (2027, 2028):
                    with self.subTest(start=start, end=end, year=year, zone=zone_name):
                        settings = copy.deepcopy(DEFAULT_CONFIG['event_countdown'])
                        settings.update(timezone=zone_name, events=[event(
                            start=start, end=end, annual=True, all_day=all_day)])
                        validate(settings)
                        original = local_time(start, zone)
                        duration = local_time(end, zone) - original
                        expected_start = original.replace(year=year,
                            day=28 if original.month == 2 and original.day == 29 and year == 2027 else original.day)
                        expected_end = expected_start + duration
                        now = expected_start + dt.timedelta(minutes=30)
                        selected = select_event(settings, now=now)
                        self.assertIsNotNone(selected)
                        self.assertEqual(selected['start'], expected_start)
                        self.assertEqual(selected['end'], expected_end)
                        self.assertEqual(selected['seconds'], 0)
                        self.assertGreater(selected['end'], selected['start'])
                        later = select_event(settings, now=expected_end)
                        self.assertTrue(later is None or later['start'] > expected_start)

    def test_annual_endpoint_overflow_yields_to_representable_event(self):
        from modules.event_countdown import select_event
        from event_countdown_config import validate
        settings = copy.deepcopy(DEFAULT_CONFIG['event_countdown'])
        settings['events'] = [event(start='2020-01-01T12:00',
            end='9997-01-01T12:00', annual=True)]
        validate(settings)
        now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        self.assertIsNone(select_event(settings, now=now))
        settings['events'].append(event(id='safe', start='2026-01-02T12:00'))
        self.assertEqual(select_event(settings, now=now)['event']['id'], 'safe')

    def test_predecessor_playlist_capacity_preserves_existing_rows(self):
        from config_store import validate_config
        for count in (255, 256):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                predecessor = copy.deepcopy(DEFAULT_CONFIG)
                predecessor.pop('event_countdown')
                rows = [dict(module='clock_lava', duration=i + 1, enabled=bool(i % 2))
                        for i in range(count)]
                predecessor['playlist'] = copy.deepcopy(rows)
                validate_config(copy.deepcopy(predecessor), enforce_save_bounds=True)
                path = Path(directory) / 'config.json'
                path.write_text(json.dumps(predecessor))
                before = path.read_bytes()
                store = ConfigStore(path)
                projected = store.get()
                self.assertEqual(projected['playlist'][:count], rows)
                self.assertEqual(len(projected['playlist']), 256)
                self.assertEqual(path.read_bytes(), before)
                saved = store.update({}, base_revision=predecessor['revision'])
                self.assertEqual(saved['playlist'], projected['playlist'])
                self.assertEqual(ConfigStore(path).get(), saved)

    def test_predecessor_snapshot_boundary_does_not_charge_for_defaults(self):
        from config_store import validate_config, CONFIG_SNAPSHOT_LIMIT
        encode = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        predecessor = copy.deepcopy(DEFAULT_CONFIG)
        predecessor.pop('event_countdown')
        predecessor['playlist'] = [dict(module='clock_lava', duration=15, enabled=True)]
        # Keep this predecessor boundary independent of later omittable defaults.
        predecessor.pop('astronomy')
        # RSS defaults grew (nine verified feeds); keep the saved RSS object nondefault so
        # compaction cannot reclaim its bytes and the boundary below stays exact.
        predecessor['rss']['feeds'] = predecessor['rss']['feeds'][:1]
        # The established schema permits surrounding whitespace in device names.
        predecessor['device']['name'] += ' ' * (CONFIG_SNAPSHOT_LIMIT - len(encode(predecessor)))
        validate_config(copy.deepcopy(predecessor), enforce_save_bounds=True)
        self.assertEqual(len(encode(predecessor)), CONFIG_SNAPSHOT_LIMIT)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_bytes(encode(predecessor))
            before = path.read_bytes()
            store = ConfigStore(path)
            projected = store.get()
            self.assertEqual(projected['playlist'], predecessor['playlist'])
            self.assertEqual(projected['event_countdown'], DEFAULT_CONFIG['event_countdown'])
            self.assertEqual(path.read_bytes(), before)
            saved = store.update({}, base_revision=0)
            self.assertLessEqual(path.stat().st_size, CONFIG_SNAPSHOT_LIMIT)
            self.assertEqual(ConfigStore(path).get(), saved)
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, '1 MiB'):
                store.update({'event_countdown': {'events': [event()]}})
            self.assertEqual(path.read_bytes(), before)

    def test_empty_countdown_scheduler_yields_without_drawing(self):
        from display_controller import DisplayController
        candidate = copy.deepcopy(DEFAULT_CONFIG)
        candidate['playlist'] = [dict(module='event_countdown', duration=300, enabled=True)]
        matrix, store = Mock(), Mock()
        store.get.return_value = candidate
        controller = DisplayController(matrix, store)
        module = importlib.import_module('modules.event_countdown')
        waits = []
        def finish_cycle(timeout):
            waits.append(timeout)
            controller._shutdown.set()
            return True
        with patch.object(controller, '_is_active', return_value=True), \
             patch.object(controller, '_address_notice_due', return_value=False), \
             patch.object(controller._shutdown, 'wait', side_effect=finish_cycle), \
             patch.object(module, 'run', wraps=module.run) as run:
            controller._run_loop_unchecked()
        run.assert_called_once()
        self.assertEqual(waits, [.25])
        matrix.draw_image.assert_not_called()
        self.assertIsNone(controller.last_error)

    def test_controller_stop_cancels_running_countdown_after_first_frame(self):
        from display_controller import DisplayController
        candidate = copy.deepcopy(DEFAULT_CONFIG)
        candidate['event_countdown']['events'] = [event()]
        drawn = threading.Event()
        matrix = Mock()
        matrix.swap.side_effect = lambda *args: drawn.set()
        controller = DisplayController(matrix, Mock())
        results = []
        worker = threading.Thread(target=lambda: results.append(
            controller._execute('event_countdown', candidate, 300)))
        worker.start()
        try:
            self.assertTrue(drawn.wait(3), 'real countdown failed to publish')
            self.assertTrue(controller.stop(timeout=2))
            worker.join(2)
            self.assertFalse(worker.is_alive(), 'countdown ignored controller cancellation')
            self.assertEqual(results, [True])
            self.assertIsNone(controller.last_error)
        finally:
            controller._shutdown.set()
            controller._interrupt_module()
            worker.join(3)

    def test_ambiguous_local_first_occurrence_and_gap_validation(self):
        from modules.event_countdown import select_event
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / 'config.json')
            saved = store.update({'event_countdown': {'timezone': 'Europe/Berlin',
                'events': [event(start='2026-10-25T02:30')]}})
            selected = select_event(saved['event_countdown'],
                now=dt.datetime(2026, 10, 25, 0, 0, tzinfo=dt.timezone.utc))
            self.assertEqual(selected['seconds'], 1800)
            before = store.path.read_bytes()
            with self.assertRaisesRegex(ValueError, 'DST gap'):
                store.update({'event_countdown': {'events': [event(start='2026-03-29T02:30')]}})
            self.assertEqual(store.path.read_bytes(), before)

    def test_artwork_cancellation_during_decode_reaps_child_and_closes_duplicate(self):
        import matrix_framework
        from media_resolver import MediaResolver
        from modules import event_countdown as module
        candidate = copy.deepcopy(DEFAULT_CONFIG)
        candidate['event_countdown'].update(layout='icon', events=[event(artwork='art.png')])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new('RGB', (8, 8), '#ff00ff').save(root / 'art.png')
            resolver = MediaResolver(root)
            self.addCleanup(resolver.close)
            handle = resolver.resolve('art.png')
            stop = threading.Event()
            children, descriptors = [], []
            popen = matrix_framework.subprocess.Popen
            def cancel_after_spawn(*args, **kwargs):
                process = popen(*args, **kwargs)
                children.append(process)
                descriptors.append(kwargs['stdin'])
                stop.set()
                return process
            with patch.object(matrix_framework.subprocess, 'Popen', cancel_after_spawn):
                self.assertEqual(list(module.preview_frames(candidate, 15, stop,
                                                          pinned_handles={'art.png': handle})), [])
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
            self.assertTrue(children[0].stdout.closed)
            with self.assertRaises(OSError):
                os.fstat(descriptors[0])
            resolver.close()
            self.assertTrue(handle.closed)

    def test_artwork_absent_falls_back_and_non_icon_layout_never_decodes(self):
        from modules import event_countdown as module
        candidate = copy.deepcopy(DEFAULT_CONFIG)
        settings = candidate['event_countdown']
        settings.update(layout='icon', events=[event()])
        plain = next(module.image_frames(candidate, .1))[0]
        settings['events'][0]['artwork'] = None
        self.assertEqual(next(module.image_frames(candidate, .1))[0].tobytes(), plain.tobytes())
        self.assertEqual(plain.getpixel((2, 9)), (113, 112, 255))
        for layout in ('large', 'progress'):
            settings['layout'] = layout
            settings['events'][0]['artwork'] = 'unadmitted.png'
            with patch.object(module.CaptureMatrix, 'media_frames', side_effect=AssertionError('must not decode')):
                next(module.image_frames(candidate, .1))
        settings['layout'] = 'icon'
        with self.assertRaisesRegex(ValueError, 'capability is missing'):
            next(module.image_frames(candidate, .1))

    def test_playlist_artwork_admission_renders_and_releases_handle(self):
        from display_controller import DisplayController
        from media_resolver import MediaResolver
        candidate = copy.deepcopy(DEFAULT_CONFIG)
        candidate['event_countdown'].update(layout='icon', events=[event(artwork='art.png')])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new('RGB', (8, 8), '#ff00ff').save(root / 'art.png')
            matrix, store = Mock(), Mock(media_root=root)
            controller = DisplayController(matrix, store)
            handles = []
            resolve = MediaResolver.resolve
            def record(resolver, reference):
                handle = resolve(resolver, reference)
                handles.append(handle)
                return handle
            with patch.object(MediaResolver, 'resolve', record):
                self.assertTrue(controller._execute('event_countdown', candidate, .01))
            self.assertEqual(matrix.draw_image.call_args.args[0].getpixel((0, 9)), (255, 0, 255))
            self.assertEqual(len(handles), 1)
            self.assertTrue(handles[0].closed)

    def test_device_draft_consumes_pinned_inode_after_path_swap(self):
        from display_controller import DisplayController
        from media_resolver import MediaResolver
        candidate = copy.deepcopy(DEFAULT_CONFIG)
        candidate['event_countdown'].update(layout='icon', events=[event(artwork='art.png')])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new('RGB', (8, 8), '#ff00ff').save(root / 'art.png')
            resolver = MediaResolver(root)
            self.addCleanup(resolver.close)
            handle = resolver.resolve('art.png')
            Image.new('RGB', (8, 8), '#00ff00').save(root / 'replacement.png')
            (root / 'replacement.png').replace(root / 'art.png')
            request = Mock(config={'module': 'event_countdown', 'candidate': candidate},
                           timeout_seconds=.01, pinned_media={'art.png': handle})
            matrix = Mock()
            self.assertTrue(DisplayController(Mock(), Mock()).render_device_draft(
                matrix, request, stop_event=threading.Event()))
            self.assertEqual(matrix.draw_image.call_args.args[0].getpixel((0, 9)), (255, 0, 255))
            preview = _edit_preview_iterator('event_countdown', candidate, None,
                                            request.pinned_media, None, threading.Event())
            frame = next(preview)
            preview.close()
            with Image.open(io.BytesIO(base64.b64decode(frame['png']))) as image:
                self.assertEqual(image.getpixel((0, 9)), (255, 0, 255))

    def test_library_artwork_schema_admission_and_shared_pixels(self):
        from media_resolver import pin_candidate_media, module_media_references
        from modules import event_countdown as module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(Path(__file__).parents[1] / 'resources/media/built-in', root / 'built-in')
            Image.new('RGB', (8, 8), '#ff00ff').save(root / 'art.png')
            store = ConfigStore(root / 'config.json', media_dir=root)
            candidate = store.validate_candidate({'event_countdown': {
                'layout': 'icon', 'events': [event(artwork='art.png')]}})
            self.assertEqual(module_media_references(candidate, 'event_countdown'), ('art.png',))
            with pin_candidate_media(candidate, root) as handles:
                preview = _edit_preview_iterator('event_countdown', candidate, None, handles, None, threading.Event())
                frame = next(preview)
                preview.close()
                with Image.open(io.BytesIO(base64.b64decode(frame['png']))) as image:
                    self.assertEqual(image.getpixel((0, 9)), (255, 0, 255))
                    self.assertEqual(image.getpixel((8, 9)), (0, 0, 0))
                class Panel:
                    def draw_image(self, image): self.image = image.copy()
                    def swap(self): pass
                panel = Panel()
                module.run(panel, candidate, .01, pinned_handles=handles)
                self.assertEqual(panel.image.getpixel((0, 9)), (255, 0, 255))
            self.assertTrue(all(handle.closed for handle in handles.values()))
            self.assertEqual(store.get()['event_countdown']['events'], [])

    def test_one_title_pass_extends_hold_restores_and_cancels(self):
        from modules import event_countdown as module
        config = copy.deepcopy(DEFAULT_CONFIG)
        config['event_countdown']['events'] = [event(title='Title ' * 10)]
        now = dt.datetime(2029, 1, 1, tzinfo=dt.timezone.utc)
        with patch.object(module, 'utc_now', return_value=now):
            frames = list(module.image_frames(config, .1))
            pass_seconds = (frames[0][2]['title_width'] + 4) * .05
            self.assertAlmostEqual(sum(frame[1] for frame in frames), pass_seconds)
            self.assertLess(frames[-2][2]['title_x'], 0)
            self.assertEqual(frames[-1][2]['title_x'], 0)
            self.assertEqual(frames[-1][1], 0)
            longer = list(module.image_frames(config, pass_seconds + .2))
            self.assertAlmostEqual(sum(frame[1] for frame in longer), pass_seconds + .2)
            self.assertTrue(all(frame[2]['title_x'] == 0 for frame in longer[-3:]))
            stop = threading.Event()
            stream = module.preview_frames(config, .1, stop)
            next(stream)
            stop.set()
            self.assertEqual(list(stream), [])

    def test_slow_physical_title_uses_actual_elapsed_and_restores(self):
        from modules import event_countdown as module
        config = copy.deepcopy(DEFAULT_CONFIG)
        config['event_countdown']['events'] = [event(title='Title ' * 3)]
        clock, positions = [0.0], []
        real_render = module.render_frame
        def render(settings, selected, **kwargs):
            image, info = real_render(settings, selected, **kwargs)
            positions.append((clock[0], info['title_x']))
            return image, info
        class SlowMatrix:
            def draw_image(self, image):
                clock[0] += .6
            def swap(self):
                pass
        def sleep(delay):
            clock[0] += delay
        with patch.object(module, 'utc_now', return_value=dt.datetime(2029, 1, 1, tzinfo=dt.timezone.utc)), \
             patch.object(module.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(module.time, 'sleep', side_effect=sleep), \
             patch.object(module, 'render_frame', side_effect=render):
            module.run(SlowMatrix(), config, duration=.1)
        self.assertGreater(len(positions), 2)
        self.assertEqual(positions[1][1], -int(positions[1][0] / .05))
        self.assertEqual(positions[-1][1], 0)

    def test_measured_bearings_auto_font_and_truthful_overflow(self):
        from modules import event_countdown as module
        from frame_capture import BdfFont, Glyph
        def font(width, height, bearing=0):
            value = object.__new__(BdfFont)
            value.default_codepoint = 0
            value.glyphs = {0: Glyph(width, height, bearing, 0, width, (255,) * height, 8)}
            return value
        settings = copy.deepcopy(DEFAULT_CONFIG['event_countdown'])
        settings.update(title_font='title', countdown_font='auto', title_align='left')  # bearing check at x=0
        selected = dict(event=event(title='A'), seconds=0)
        with patch.object(module, 'fonts', return_value={'title': font(3, 3, -2), 'small': font(4, 6), 'large': font(6, 15)}):
            image, info = module.render_frame(settings, selected)
            self.assertEqual(info['title_width'], 5, 'negative bearing must not be clipped')
            self.assertEqual(info['countdown_font'], 'large')
            self.assertFalse(info['countdown_overflow'])
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
            settings.update(countdown_font='large', insets=dict(top=8, bottom=8, left=8, right=8))
            _, info = module.render_frame(settings, selected)
            self.assertTrue(info['countdown_overflow'])
            self.assertEqual(info['countdown_height'], 15)
            self.assertEqual(info['countdown_available_height'], 11)

    def test_long_title_draw_is_visible_only_and_reports_vertical_clipping(self):
        from modules import event_countdown as module
        settings = copy.deepcopy(DEFAULT_CONFIG['event_countdown'])
        settings.update(title_font='10x20', insets=dict(top=8, bottom=8, left=8, right=8))
        selected = dict(event=event(title='Long title ' * 1000 + 'END'), seconds=60)
        font = module.fonts()['10x20']
        with patch.object(font, 'text_pixels', side_effect=AssertionError('whole-title pixels allocated')):
            _, info = module.render_frame(settings, selected)
            self.assertGreater(info['title_width'], 100000)
            self.assertTrue(info['title_overflow'])
            with patch.object(font, 'glyph', wraps=font.glyph) as glyph:
                module.render_frame(settings, selected, elapsed=100)
                self.assertLess(glyph.call_count, 100, 'cached measurement and viewport-only glyph iteration')

    def test_bundled_icons_are_distinct_and_unknown_uses_calendar(self):
        from modules import event_countdown as module
        settings = copy.deepcopy(DEFAULT_CONFIG['event_countdown'])
        settings['layout'] = 'icon'
        images = {}
        for name in ('calendar', 'birthday', 'anniversary', 'launch', 'holiday', 'opening', 'unknown'):
            image, _ = module.render_frame(settings, dict(event=event(icon=name), seconds=60))
            images[name] = image.tobytes()
        self.assertEqual(len(set(images.values())), 6)
        self.assertEqual(images['unknown'], images['calendar'])

    def test_progress_uses_stable_thirty_day_horizon(self):
        from modules import event_countdown as module
        settings = copy.deepcopy(DEFAULT_CONFIG['event_countdown'])
        settings['layout'] = 'progress'
        selected = dict(event=event(), seconds=15 * 86400)
        first, _ = module.render_frame(settings, selected)
        restarted, _ = module.render_frame(settings, selected, initial_seconds=selected['seconds'])
        self.assertEqual(first.tobytes(), restarted.tobytes())
        self.assertEqual(first.getpixel((31, 31)), (113, 112, 255))
        self.assertEqual(first.getpixel((32, 31)), (0, 0, 0))

    def test_physical_playback_uses_wall_clock_and_monotonic_deadline(self):
        from modules import event_countdown as module
        config = copy.deepcopy(DEFAULT_CONFIG)
        config['event_countdown']['events'] = [event(start='2030-01-01T12:00')]
        origin = dt.datetime(2030, 1, 1, 11, 59, 59, tzinfo=dt.timezone.utc)
        clock = [0.0]
        seconds = []
        real_render = module.render_frame
        def render(settings, selected, **kwargs):
            seconds.append(selected['seconds'])
            return real_render(settings, selected, **kwargs)
        class SlowMatrix:
            def draw_image(self, image):
                clock[0] += .6
            def swap(self):
                pass
        def sleep(delay):
            clock[0] += delay
        with patch.object(module, 'utc_now', side_effect=lambda: origin + dt.timedelta(seconds=clock[0])), \
             patch.object(module.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(module.time, 'sleep', side_effect=sleep), \
             patch.object(module, 'render_frame', side_effect=render):
            module.run(SlowMatrix(), config, duration=2)
        self.assertLessEqual(clock[0], 2.65)
        self.assertIn(0, seconds)
        self.assertLessEqual(len(seconds), 4)

    def test_future_annual_original_is_not_omitted(self):
        from modules.event_countdown import select_event
        selected = select_event({'timezone': 'UTC', 'events': [event(annual=True)]},
                                now=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
        self.assertIsNotNone(selected)
        self.assertEqual(selected['start'].year, 2030)

    def test_physical_and_preview_share_pixels_empty_yields_and_stop_cancels(self):
        from display_controller import MODULES
        from edit_preview import CaptureMatrix
        self.assertIn('event_countdown', MODULES, 'playlist runtime is not wired')
        module = importlib.import_module(MODULES['event_countdown'])
        config = copy.deepcopy(DEFAULT_CONFIG)
        class PublishedMatrix(CaptureMatrix):
            def swap(self, canvas=None):
                self._capture.publish(canvas or self.canvas)
                return super().swap(canvas)
        matrix = PublishedMatrix()
        self.assertFalse(module.run(matrix, config, duration=300))
        self.assertEqual(matrix._capture.revision, 0)
        config['event_countdown']['events'] = [event()]
        stop = threading.Event()
        stop.set()
        self.assertFalse(module.run(matrix, config, duration=300, stop_event=stop))
        self.assertEqual(list(module.preview_frames(config, 300, stop)), [])
        now = dt.datetime(2029, 12, 31, tzinfo=dt.timezone.utc)
        with patch.object(module, 'utc_now', return_value=now):
            frame = next(module.preview_frames(config, .05))
            self.assertTrue(module.run(matrix, config, duration=.05))
        image = Image.open(io.BytesIO(base64.b64decode(frame['png'])))
        self.assertEqual(image.size, (64, 32))
        self.assertEqual(matrix._capture.latest_rgb()[0], image.tobytes())
        self.assertIsNotNone(image.getbbox())

    def test_measured_layout_geometry_scroll_restore_and_progress(self):
        module = importlib.import_module('modules.event_countdown')
        self.assertTrue(callable(getattr(module, 'render_frame', None)), 'shared layout renderer is missing')
        settings = copy.deepcopy(DEFAULT_CONFIG['event_countdown'])
        settings.update(layout='progress', insets=dict(top=2, right=3, bottom=2, left=4))
        selected = dict(event=event(title='A long event title that must scroll'), seconds=3600)
        image, info = module.render_frame(settings, selected, elapsed=0, initial_seconds=7200)
        self.assertTrue(info['title_scrolls'])
        self.assertEqual(info['title_x'], 0)
        self.assertEqual(image.getpixel((4, 28)), (113, 112, 255))
        self.assertEqual(image.getpixel((60, 28)), (0, 0, 0))
        for x, y in ((0, 0), (3, 28), (61, 28), (4, 30)):
            self.assertEqual(image.getpixel((x, y)), (0, 0, 0))
        _, moving = module.render_frame(settings, selected, elapsed=.1, initial_seconds=7200)
        self.assertLess(moving['title_x'], 0)
        _, restored = module.render_frame(settings, selected, elapsed=(info['title_width'] + 4) * .05, initial_seconds=7200)
        self.assertEqual(restored['title_x'], 0)
        self.assertEqual(module.compact_countdown(0), 'NOW')
        self.assertEqual(module.compact_countdown(59), '59s')
        self.assertEqual(module.compact_countdown(3600), '1h 0m')
        settings['layout'] = 'icon'
        icon, _ = module.render_frame(settings, selected)
        self.assertNotEqual(icon.tobytes(), image.tobytes())

    def test_invalid_event_updates_are_atomic_and_styles_are_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / 'config.json')
            before = store.path.read_bytes()
            invalid = [event(now_seconds=True), event(title=''), event(start='not a date'),
                       event(end='2020-01-01T12:00'), event(annual='yes'), event(extra=1),
                       event(all_day=True), event(accent='red'), event(icon='../secret'),
                       event(start='2026-03-29T02:30')]
            for value in invalid:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    store.update({'event_countdown': {'timezone': 'Europe/Berlin', 'events': [value]}})
                self.assertEqual(store.path.read_bytes(), before)
            for change in ({'timezone': 'bad/zone'}, {'events': [event(), event()]},
                           {'layout': 'other'}, {'title_font': 'missing'}, {'countdown_font': 'missing'},
                           {'progress_height': True}, {'progress_height': 5}, {'insets': {'left': 9}}):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    store.update({'event_countdown': change})
            settings = store.get()['event_countdown']
            self.assertEqual(settings['title_font'], '5x7')
            self.assertEqual(settings['countdown_font'], 'auto')
            self.assertEqual(settings['progress_height'], 2)
            self.assertEqual(settings['accent'], '#7170FF')
            self.assertEqual(settings['insets'], dict(top=0, right=0, bottom=0, left=0))
            store.update({'event_countdown': {'layout': 'progress', 'events': [event()]}})

    def test_timezone_selection_now_and_recurrence_boundaries(self):
        module = importlib.import_module('modules.event_countdown')
        self.assertTrue(callable(getattr(module, 'select_event', None)), 'selection contract is missing')
        def pick(events, now, zone='UTC'):
            return module.select_event({'events': events, 'timezone': zone}, now=dt.datetime.fromisoformat(now))
        timed = event(start='2030-01-01T12:00')
        self.assertEqual(pick([timed], '2030-01-01T11:59:59+00:00')['seconds'], 1)
        self.assertEqual(pick([timed], '2030-01-01T12:00:00+00:00')['seconds'], 0)
        self.assertIsNotNone(pick([timed], '2030-01-01T12:14:59+00:00'))
        self.assertIsNone(pick([timed], '2030-01-01T12:15:00+00:00'))
        leap = event(start='2024-02-29', all_day=True, annual=True)
        self.assertEqual(pick([leap], '2027-02-27T23:59:59+00:00')['seconds'], 1)
        self.assertEqual(pick([leap], '2027-02-28T23:59:59+00:00')['seconds'], 0)
        self.assertEqual(pick([leap], '2027-03-01T00:00:00+00:00')['start'].date(), dt.date(2028, 2, 29))
        day = event(start='2026-03-29', all_day=True)
        self.assertEqual(pick([day], '2026-03-29T21:59:59+00:00', 'Europe/Berlin')['seconds'], 0)
        self.assertIsNone(pick([day], '2026-03-29T22:00:00+00:00', 'Europe/Berlin'))
        dst = event(start='2026-03-29T03:30')
        self.assertEqual(pick([dst], '2026-03-29T00:30:00+00:00', 'Europe/Berlin')['seconds'], 3600)
        multi = event(start='2030-01-01T12:00', end='2030-01-03T12:00')
        self.assertEqual(pick([multi], '2030-01-03T11:59:59+00:00')['seconds'], 0)
        self.assertIsNone(pick([multi], '2030-01-03T12:00:00+00:00'))
        later = event(id='later', start='2031-01-01T12:00')
        self.assertEqual(pick([later, timed], '2029-01-01T00:00:00+00:00')['event']['id'], 'launch')
        self.assertEqual(pick([timed, event(id='tie')], '2029-01-01T00:00:00+00:00')['event']['id'], 'launch')
        self.assertIsNone(pick([event(enabled=False)], '2029-01-01T00:00:00+00:00'))

    def test_existing_config_projects_disabled_tail_without_rewriting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            old = copy.deepcopy(DEFAULT_CONFIG)
            old.pop('event_countdown', None)
            old['playlist'] = [row for row in old['playlist'] if row['module'] != 'event_countdown']
            path.write_text(json.dumps(old))
            original = path.read_bytes()
            store = ConfigStore(path)
            projected = store.get()
            self.assertEqual(projected['playlist'][-1], dict(module='event_countdown', duration=15, enabled=False))
            self.assertEqual(projected['playlist'][:-1], old['playlist'])
            self.assertEqual(path.read_bytes(), original)
            # Owner 2026-10-14: a new countdown follows the Weather location's zone.
            self.assertEqual(projected['event_countdown']['timezone'], '')
            self.assertEqual(ConfigStore(path).get(), projected)

    def test_saved_local_event_renders_through_server_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / 'config.json')
            self.assertIn('event_countdown', store.get(), 'local countdown config is missing')
            saved = store.update({'event_countdown': {'events': [event()]}})
            frames = _edit_preview_iterator('event_countdown', saved, None, {}, None, threading.Event())
            frame = next(frames)
            self.assertTrue(frame)
            frames.close()
            self.assertEqual(ConfigStore(store.path).get()['event_countdown']['events'][0]['title'], 'Launch')
