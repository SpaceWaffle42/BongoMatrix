"""Executed shared pixels/timing; no browser host or hardware implied."""
import base64
import datetime as dt
import dataclasses
import importlib.util
import inspect
import io
import threading
import unittest
from unittest.mock import patch
from PIL import Image


class RendererTests(unittest.TestCase):
    def test_moon_pixels_timing_empty_and_cancellation(self):
        self.assertIsNotNone(importlib.util.find_spec('modules.astronomy_renderer'), 'Astronomy shared renderer absent')
        from astronomy import Astronomy, Location, UTC
        from modules import astronomy_renderer as r
        with Astronomy() as engine:
            moon = engine.moon(dt.datetime(2024, 4, 8, 12, tzinfo=UTC), Location(51.4779, 0, 'Europe/London', 'Greenwich', 'weather'))
        p = r.prepare_moon(moon, r.MoonOptions(view='advanced'))
        self.assertEqual(p.labels[0], 'New Moon')
        self.assertEqual(p.labels[1], '0% lit')
        self.assertEqual(p.labels[2], 'R 06:15  S 19:41')
        self.assertIn('08 Apr 19:21', p.labels[3])
        expected = list(r.image_frames((p,)))
        self.assertGreater(len(expected), 1)
        self.assertTrue(all(im.size == (64, 32) and im.mode == 'RGB' for im, _, _ in expected))
        self.assertEqual(expected[0][0].tobytes(), expected[-1][0].tobytes())
        self.assertEqual(expected[-1][1], 10)
        self.assertAlmostEqual(sum(delay for _, delay, _ in expected), r.duration_seconds((p,)))
        self.assertEqual(list(r.image_frames(())), [])
        self.assertEqual(r.duration_seconds(()), 0)
        preview = list(r.preview_frames((p,)))
        for frame, (image, delay, _) in zip(preview, expected, strict=True):
            self.assertEqual(Image.open(io.BytesIO(base64.b64decode(frame['png']))).tobytes(), image.tobytes())
            self.assertAlmostEqual(frame['delay_ms'], delay * 1000)

        class Clock:
            value = 0
            def __call__(self):
                return self.value
        class Stop:
            stopped = False
            def is_set(self):
                return self.stopped
            def wait(self, delay):
                clock.value += delay
                self.stopped = stage == 'hold'
                return self.stopped
        class Matrix:
            def __init__(self):
                self.frames = []
            def draw_image(self, image):
                self.image = image.copy()
                if stage == 'draw':
                    stop.stopped = True
            def swap(self):
                self.frames.append(self.image.tobytes())
        for stage, count in (('none', len(expected)), ('before', 0), ('draw', 0), ('hold', 1)):
            clock, stop, matrix = Clock(), Stop(), Matrix()
            stop.stopped = stage == 'before'
            self.assertEqual(r.run(matrix, (p,), stop, clock=clock), count)
            self.assertEqual(matrix.frames, [im.tobytes() for im, _, _ in expected[:count]])
            if stage == 'none':
                self.assertAlmostEqual(clock.value, r.duration_seconds((p,)))
        stop.stopped = False
        original = r.render_frame
        def cancel(*args):
            image = original(*args)
            stop.stopped = True
            return image
        matrix = Matrix()
        with patch.object(r, 'render_frame', side_effect=cancel):
            self.assertEqual(r.run(matrix, (p,), stop, clock=clock), 0)
        self.assertEqual(list(r.preview_frames((p,), stop)), [])

    def test_simple_view_is_sprite_moon_and_phase_name_only(self):
        from astronomy import Astronomy, Location, UTC
        from modules import astronomy_renderer as r
        location = Location(51.4779, 0, 'Europe/London', 'Greenwich', 'weather')
        with Astronomy() as engine:
            full = engine.moon(dt.datetime(2024, 4, 23, 23, 49, tzinfo=UTC), location)
            crescent = engine.moon(dt.datetime(2024, 4, 12, 12, tzinfo=UTC), location)
        self.assertEqual(r.MoonOptions().view, 'simple')
        with self.assertRaises(ValueError):
            r.MoonOptions(view='fancy')
        card = r.prepare_moon(full)
        self.assertEqual(card.labels, ('Full', 'Moon'))
        self.assertEqual(len(card.lines), 2)
        image = list(r.image_frames((card,)))[-1][0]
        # Large Aseprite sprite occupies the left 28x28 region, centred vertically.
        moon_box = image.crop((0, 0, 32, 32)).getbbox()
        self.assertIsNotNone(moon_box)
        self.assertGreaterEqual(moon_box[2] - moon_box[0], 26)
        self.assertGreaterEqual(moon_box[3] - moon_box[1], 26)
        # Shaded sprite: several real tones, not a flat two-colour disc.
        colours = {image.getpixel((x, y)) for x in range(32) for y in range(32)}
        self.assertGreaterEqual(len(colours), 4)
        # Simple view never shows illumination, rise/set or next-phase details.
        self.assertFalse(any('%' in label or 'Next' in label or 'R ' in label for label in card.labels))
        # Waxing crescent is lit on the right-hand side of the disc.
        thin = list(r.image_frames((r.prepare_moon(crescent),)))[-1][0]
        brightness = lambda x0, x1: sum(sum(thin.getpixel((x, y))) for x in range(x0, x1) for y in range(2, 30))
        self.assertGreater(brightness(16, 30), brightness(2, 16) * 2)
        self.assertEqual(r.prepare_moon(crescent).labels, ('Waxing', 'Crescent'))

    def test_advanced_view_keeps_details_with_sprite_moon(self):
        from astronomy import Astronomy, Location, UTC
        from modules import astronomy_renderer as r
        with Astronomy() as engine:
            moon = engine.moon(dt.datetime(2024, 4, 8, 12, tzinfo=UTC), Location(51.4779, 0, 'Europe/London', 'Greenwich', 'weather'))
        card = r.prepare_moon(moon, r.MoonOptions(view='advanced'))
        self.assertEqual(card.labels[0], 'New Moon')
        self.assertEqual(len(card.labels), 4)
        image = list(r.image_frames((card,)))[-1][0]
        self.assertIsNotNone(image.crop((0, 0, 16, 16)).getbbox())

    def test_moon_sprites_are_versioned_aseprite_exports(self):
        from pathlib import Path
        from modules import astronomy_renderer as r
        sprites = Path(r.__file__).resolve().parent.parent / 'resources' / 'astronomy' / 'sprites'
        for size in (28, 16):
            self.assertTrue((sprites / f'moon_{size}.aseprite').is_file())
            sheet = Image.open(sprites / f'moon_{size}.png')
            self.assertEqual(sheet.size, (size * r.MOON_FRAMES, size))
        self.assertTrue((sprites / 'event_icons.aseprite').is_file())
        self.assertEqual(Image.open(sprites / 'event_icons.png').size, (24, 8))

    def test_rounding_is_on_utc_timeline_at_dst_fold(self):
        self.assertIsNotNone(importlib.util.find_spec('modules.astronomy_renderer'), 'Astronomy shared renderer absent')
        from modules.astronomy_renderer import minute_label
        from zoneinfo import ZoneInfo
        instant = dt.datetime(2024, 10, 27, 0, 59, 40, tzinfo=dt.timezone.utc)
        self.assertEqual(minute_label(instant.astimezone(ZoneInfo('Europe/London'))), '01:00')

    def test_options_prepare_every_pass_disabled_and_font_clipping(self):
        from modules import astronomy_renderer as r
        self.assertTrue(hasattr(r, 'prepare_pass'), 'Recalculation / disabled pass boundary absent')
        from astronomy import Astronomy, Location, UTC
        from font_catalog import available_font_names
        location = Location(51.4779, 0, 'Europe/London', 'Greenwich', 'weather')
        now = dt.datetime(2024, 4, 8, 12, tzinfo=UTC)
        with Astronomy() as engine:
            with patch.object(engine, 'moon', wraps=engine.moon) as calculate:
                self.assertEqual(r.prepare_pass(engine, now, location, current_moon=False), ())
                calculate.assert_not_called()
                first = r.prepare_pass(engine, now, location)
                second = r.prepare_pass(engine, now + dt.timedelta(days=7), location)
                self.assertEqual(calculate.call_count, 2)
                self.assertNotEqual(first[0].labels, second[0].labels)
            stop = threading.Event()
            stop.set()
            self.assertEqual(r.prepare_pass(engine, now, location, stop_event=stop), ())
            moon = engine.moon(now, location)
        for name in available_font_names():
            options = r.MoonOptions(title_font=name, detail_font=name, hold_seconds=3, background='#010203', text_colour='#102030', moon_colour='#abcdef', view='advanced')
            card = r.prepare_moon(moon, options)
            frames = list(r.image_frames((card,)))
            self.assertEqual(frames[-1][1], 3)
            self.assertEqual(frames[0][0].getpixel((63, 17)), (1, 2, 3))
            self.assertEqual(len(card.clipping), 4)
            self.assertTrue(all(isinstance(v, bool) for v in card.clipping))
        for changes in ({'hold_seconds': 0}, {'hold_seconds': True}, {'hold_seconds': float('nan')},
                        {'frame_delay': .0001}, {'title_font': '../5x7'}, {'text_colour': 'red'}):
            with self.assertRaises(ValueError):
                r.MoonOptions(**changes)
        with self.assertRaises(ValueError):
            list(r.image_frames(list(first)))
        with self.assertRaises(ValueError):
            list(r.image_frames(first * 52))

    def test_prepared_event_card_countdown_windows_fonts_and_shared_pixels(self):
        from astronomy import AstronomyEvent, Location, VisibilityWindow, UTC
        from modules import astronomy_renderer as r
        self.assertTrue(hasattr(r, 'prepare_event'), 'Prepared astronomy event cards absent')
        now = dt.datetime(2024, 10, 27, 0, 30, tzinfo=UTC)
        start = now + dt.timedelta(days=2, hours=3)
        event = AstronomyEvent('test:full', 'full_moon', 'Full Moon', start,
            Location(51.4779, 0, 'Europe/London', 'Greenwich', 'weather'),
            (VisibilityWindow(start, start + dt.timedelta(hours=1)),), 'scheduling fixture only')
        card = r.prepare_event(event, now)
        self.assertEqual(card.options.hold_seconds, 8)
        self.assertEqual(card.options.accent, '#B388FF')
        self.assertEqual(card.labels[0], 'Full Moon')
        self.assertIn('29 Oct 03:30', card.labels[1])
        self.assertIn('Partly visible', card.labels[2])
        self.assertEqual(r.event_countdown(event, now), '2d 3h')
        for seconds, text in ((86400, '1d 0h'), (86399, '23h 59m'), (3600, '1h 0m'),
                              (3599, '59m'), (60, '1m'), (1, '1m'), (0, 'NOW')):
            self.assertEqual(r.event_countdown(event, start - dt.timedelta(seconds=seconds)), text)
        self.assertIsNone(r.event_countdown(event, event.windows[-1].end))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            card.labels = ()
        # Even cold font caches may not be used by playback. No source I/O.
        with patch.object(r, '_font', side_effect=AssertionError('font access during playback')), \
                patch('builtins.open', side_effect=AssertionError('disk access during playback')):
            images = list(r.image_frames((card,), now=now))
            previews = list(r.preview_frames((card,), now=now))
        self.assertGreater(len(images), 1)
        self.assertEqual(images[-1][1], 8)
        self.assertAlmostEqual(sum(delay for _, delay, _ in images), r.duration_seconds((card,)))
        # Static lines restore; the live countdown crosses an hour boundary
        # immediately after the initial exact 2d 3h instant.
        self.assertEqual(images[0][0].crop((0, 0, 64, 25)).tobytes(),
                         images[-1][0].crop((0, 0, 64, 25)).tobytes())
        self.assertEqual(images[0][2]['countdown'], '2d 3h')
        self.assertEqual(images[-1][2]['countdown'], '2d 2h')
        for preview, (image, delay, _) in zip(previews, images, strict=True):
            self.assertEqual(Image.open(io.BytesIO(base64.b64decode(preview['png']))).tobytes(), image.tobytes())
            self.assertAlmostEqual(preview['delay_ms'], delay * 1000)
        # Oversized explicit font remains supported with truthful clipping flags.
        huge = r.prepare_event(event, now, r.EventOptions(countdown_font='10x20'))
        self.assertTrue(huge.clipping[-1])
        with self.assertRaises(ValueError):
            r.EventOptions(countdown_font='../5x7')

    def test_event_playback_rechecks_wall_time_and_window_changes_before_swap(self):
        from astronomy import AstronomyEvent, Location, VisibilityWindow, UTC
        from modules import astronomy_renderer as r
        self.assertIn('utc_clock', inspect.signature(r.run).parameters,
                      'Panel playback does not recheck actual event time')
        now = dt.datetime(2024, 10, 27, 0, 59, 59, tzinfo=UTC)
        location = Location(51.4779, 0, 'Europe/London', 'Greenwich', 'weather')
        first = VisibilityWindow(now - dt.timedelta(minutes=10), now + dt.timedelta(seconds=1))
        second = VisibilityWindow(now + dt.timedelta(minutes=20), now + dt.timedelta(minutes=40))
        event = AstronomyEvent('test:windows', 'full_moon', 'Full Moon', now, location,
                               (first, second), 'scheduling fixture only')
        card = r.prepare_event(event, now)
        class Clock:
            elapsed = 0
            def monotonic(self):
                return self.elapsed
            def utc(self):
                return now + dt.timedelta(seconds=self.elapsed)
            def sleep(self, seconds):
                self.elapsed += seconds
        class Matrix:
            def __init__(self):
                self.frames = []
            def draw_image(self, image):
                self.image = image.copy()
            def swap(self):
                self.frames.append(self.image.tobytes())
        clock, matrix = Clock(), Matrix()
        expected = list(r.image_frames((card,), now=now))
        self.assertEqual(expected[0][2]['countdown'], 'NOW')
        self.assertEqual(expected[-1][2]['countdown'], '19m')
        with patch.object(r, '_font', side_effect=AssertionError('playback loaded font')):
            count = r.run(matrix, (card,), clock=clock.monotonic, sleep=clock.sleep, utc_clock=clock.utc)
        self.assertEqual(count, len(expected))
        self.assertEqual(matrix.frames, [im.tobytes() for im, _, _ in expected])
        self.assertAlmostEqual(clock.elapsed, sum(delay for _, delay, _ in expected))
        # Preparation can be old; actual playback must submit nothing after end.
        matrix = Matrix()
        self.assertEqual(r.run(matrix, (card,), utc_clock=lambda: second.end), 0)
        self.assertEqual(matrix.frames, [])
        # Cancellation / expiry during draw is checked immediately before swap.
        one = r.prepare_event(dataclasses.replace(event, windows=(first,)), now)
        clock, matrix = Clock(), Matrix()
        def expire_during_draw(image):
            clock.elapsed = 2
        matrix.draw_image = expire_during_draw
        self.assertEqual(r.run(matrix, (one,), clock=clock.monotonic, sleep=clock.sleep,
                               utc_clock=clock.utc), 0)
        self.assertEqual(matrix.frames, [])
        # The static hold may not outlive the last eligible window.
        ticks = r._scroll_ticks(one)
        expiry = now + dt.timedelta(seconds=ticks * one.options.frame_delay + 2)
        short = r.prepare_event(dataclasses.replace(event, windows=(VisibilityWindow(
            now - dt.timedelta(minutes=10), expiry),)), now)
        frames = list(r.image_frames((short,), now=now))
        self.assertAlmostEqual(sum(delay for _, delay, _ in frames), (expiry - now).total_seconds())

    def test_real_pass_moon_first_nearest_restart_limits_disabled_and_cancel(self):
        from astronomy import Astronomy, Location, UTC
        from modules import astronomy_renderer as r
        self.assertIn('lunar_events', inspect.signature(r.prepare_pass).parameters,
                      'Lunar event pass integration absent')
        location = Location(0, 0, 'UTC', 'Equator', 'map pin')
        now = dt.datetime(2024, 1, 1, tzinfo=UTC)
        with Astronomy() as engine:
            with patch.object(engine, 'lunar_events', wraps=engine.lunar_events) as calculate:
                first = r.prepare_pass(engine, now, location, lunar_events=True, event_limit=2)
                second = r.prepare_pass(engine, now, location, lunar_events=True, event_limit=2)
                self.assertEqual(calculate.call_count, 2)
                self.assertIsInstance(first[0], r.PreparedMoon)
                self.assertEqual(len(first), 3)
                self.assertEqual([c.event.identity for c in first[1:]], [c.event.identity for c in second[1:]])
                self.assertLess(first[1].event.time, first[2].event.time)
                self.assertEqual(first[0].options.hold_seconds, 10)
                self.assertEqual(first[1].options.hold_seconds, 8)
            with patch.object(engine, 'moon', side_effect=AssertionError('disabled Moon calculated')), \
                    patch.object(engine, 'lunar_events', side_effect=AssertionError('disabled events calculated')):
                self.assertEqual(r.prepare_pass(engine, now, location, current_moon=False, lunar_events=False), ())
            self.assertEqual(r.prepare_pass(engine, now, location, current_moon=False,
                                           lunar_events=True, horizon_days=1), ())
            stop = threading.Event()
            stop.set()
            self.assertEqual(r.prepare_pass(engine, now, location, lunar_events=True, stop_event=stop), ())

    def test_hold_rechecks_window_and_countdown_boundaries_without_extending_time(self):
        from astronomy import AstronomyEvent, Location, VisibilityWindow, UTC
        from modules import astronomy_renderer as r
        now = dt.datetime(2024, 1, 1, tzinfo=UTC)
        location = Location(0, 0, 'UTC', 'Equator', 'map pin')
        event = AstronomyEvent('test:hold', 'full_moon', 'Full Moon', now, location,
            (VisibilityWindow(now - dt.timedelta(minutes=10), now + dt.timedelta(hours=1)),),
            'scheduling fixture only')
        card = r.prepare_event(event, now)
        scroll = r._scroll_ticks(card) * card.options.frame_delay
        ends = now + dt.timedelta(seconds=scroll + 2)
        second = VisibilityWindow(now + dt.timedelta(minutes=20), now + dt.timedelta(minutes=40))
        card = r.prepare_event(dataclasses.replace(event, windows=(VisibilityWindow(
            now - dt.timedelta(minutes=10), ends), second)), now)
        frames = list(r.image_frames((card,), now=now))
        hold = [f for f in frames if f[2]['tick'] == r._scroll_ticks(card)]
        self.assertEqual([f[2]['countdown'] for f in hold], ['NOW', '19m'],
                         'An expired window stays on screen throughout the hold')
        self.assertAlmostEqual(sum(f[1] for f in frames), r.duration_seconds((card,)))
        # A minute boundary during a long hold must refresh without font access.
        future = now + dt.timedelta(seconds=scroll + 125)
        card = r.prepare_event(dataclasses.replace(event, windows=(VisibilityWindow(
            future, future + dt.timedelta(hours=1)),)), now, r.EventOptions(hold_seconds=70))
        with patch.object(r, '_font', side_effect=AssertionError('font read')):
            frames = list(r.image_frames((card,), now=now))
        self.assertEqual(frames[-1][2]['countdown'], '1m')
        self.assertAlmostEqual(sum(f[1] for f in frames), r.duration_seconds((card,)))

    def test_duration_excludes_expired_events_and_requires_aware_clock(self):
        from astronomy import AstronomyEvent, Location, VisibilityWindow, UTC
        from modules import astronomy_renderer as r
        now = dt.datetime(2024, 1, 1, tzinfo=UTC)
        end = now + dt.timedelta(seconds=2)
        event = AstronomyEvent('test:expiry', 'full_moon', 'Full Moon', now,
            Location(0, 0, 'UTC', 'Equator', 'map pin'),
            (VisibilityWindow(now - dt.timedelta(minutes=10), end),), 'scheduling fixture only')
        card = r.prepare_event(event, now)
        self.assertAlmostEqual(r.duration_seconds((card,)), 2,
                               msg='Timing still includes frames after visibility expires')
        self.assertEqual(r.duration_seconds((card,), now=end), 0)
        with self.assertRaises(ValueError):
            r.prepare_event(event, now.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            list(r.image_frames((card,), now=now.replace(tzinfo=None)))

    def test_event_cancellation_before_draw_scroll_hold_and_after_render(self):
        from astronomy import AstronomyEvent, Location, VisibilityWindow, UTC
        from modules import astronomy_renderer as r
        now = dt.datetime(2024, 1, 1, tzinfo=UTC)
        event = AstronomyEvent('test:cancel', 'full_moon', 'Full Moon', now,
            Location(0, 0, 'UTC', 'Equator', 'map pin'),
            (VisibilityWindow(now, now + dt.timedelta(hours=1)),), 'scheduling fixture only')
        card = r.prepare_event(event, now)
        frames = list(r.image_frames((card,), now=now))
        class Stop:
            stopped = False
            elapsed = 0
            def is_set(self):
                return self.stopped
            def wait(self, seconds):
                self.elapsed += seconds
                self.stopped = stage == 'scroll' or (stage == 'hold' and seconds > 1)
                return self.stopped
        class Matrix:
            count = 0
            def draw_image(self, image):
                if stage == 'draw':
                    stop.stopped = True
            def swap(self):
                self.count += 1
        for stage, expected in (('before', 0), ('draw', 0), ('scroll', 1), ('hold', len(frames))):
            stop, matrix = Stop(), Matrix()
            stop.stopped = stage == 'before'
            self.assertEqual(r.run(matrix, (card,), stop, clock=lambda: stop.elapsed,
                utc_clock=lambda: now + dt.timedelta(seconds=stop.elapsed)), expected)
            self.assertEqual(matrix.count, expected)
        stop, matrix = Stop(), Matrix()
        original = r.render_frame
        def cancel(*args, **kwargs):
            image = original(*args, **kwargs)
            stop.stopped = True
            return image
        with patch.object(r, 'render_frame', side_effect=cancel):
            self.assertEqual(r.run(matrix, (card,), stop, utc_clock=lambda: now), 0)
        self.assertEqual(list(r.preview_frames((card,), stop, now=now)), [])
