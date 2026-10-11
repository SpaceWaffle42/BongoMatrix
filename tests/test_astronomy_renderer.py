"""Executed shared pixels/timing; no browser host or hardware implied."""
import base64
import datetime as dt
import importlib.util
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
        p = r.prepare_moon(moon)
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
            options = r.MoonOptions(title_font=name, detail_font=name, hold_seconds=3, background='#010203', text_colour='#102030', moon_colour='#abcdef')
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
