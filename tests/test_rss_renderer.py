"""Focused contract tests; no feed/network access or saved config registration."""
import dataclasses
import importlib.util
import unittest


class RendererTests(unittest.TestCase):
    def test_layout_and_colours_use_existing_regions_and_shared_output(self):
        import base64
        import io
        from PIL import Image
        from rss_config import DEFAULTS, render_options
        from rss_data import Story
        from modules import rss_renderer as r
        self.assertEqual(DEFAULTS.get('layout'), 'auto')
        thumbnail = r.PreparedThumbnail((bytes((250, 0, 0)) * 256,), (100,))
        story = Story((), 'Headline', 'Summary', '', None)
        for layout in ('auto', 'text'):
            settings = dict(DEFAULTS, layout=layout, headline_colour='#12ab34',
                            summary_colour='#abcdef', background_colour='#010203')
            p = r.prepare_story(story, render_options(settings), thumbnail=thumbnail)
            expected = list(r.image_frames((p,)))
            image, _ = r.render_frame(p, 10)
            hh = 16 if layout == 'auto' else 7
            self.assertEqual(p.warnings['headline_available_height'], hh)
            self.assertEqual(image.getpixel((63, hh)), (1, 2, 3))
            self.assertIn((18, 171, 52), set(image.crop((17 if layout == 'auto' else 0, 0, 64, hh)).getdata()))
            self.assertIn((171, 205, 239), set(image.crop((0, hh + 1, 64, 32)).getdata()))
            self.assertEqual((250, 0, 0) in set(image.getdata()), layout == 'auto')
            frames, now = [], [0.0]
            class Matrix:
                def draw_image(self, image):
                    frames.append(image.tobytes())
                def swap(self):
                    pass
            r.run(Matrix(), (p,), clock=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0] + delay))
            self.assertEqual(frames, [im.tobytes() for im, _, _ in expected])
            self.assertEqual(frames, [Image.open(io.BytesIO(base64.b64decode(f['png']))).tobytes() for f in r.preview_frames((p,))])
            self.assertAlmostEqual(now[0], r.duration_seconds((p,)))

    def test_executable_options_are_numeric_bounded_and_installed(self):
        self.assertIsNotNone(importlib.util.find_spec('rss_config'), 'RSS render options absent')
        from rss_config import RenderOptions, SPEED_DELAYS
        from font_catalog import available_font_names
        self.assertEqual(dict(SPEED_DELAYS), {'slow': .1, 'normal': .05, 'fast': .025})
        options = RenderOptions()
        self.assertEqual((options.headline_font, options.summary_font, options.frame_delay,
                          options.headline_mode, options.thumbnail_speed), ('5x7', '5x8', .05, 'once', 100.0))
        for name in available_font_names():
            self.assertEqual(RenderOptions(headline_font=name, summary_font=name).headline_font, name)
        for kwargs in ({'frame_delay': True}, {'frame_delay': float('nan')}, {'frame_delay': .001},
                       {'frame_delay': 2}, {'headline_font': '../5x7'}, {'headline_mode': 'bad'},
                       {'thumbnail_speed': 9}, {'thumbnail_speed': 401}, {'thumbnail_speed': float('inf')}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RenderOptions(**kwargs)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            options.frame_delay = .1


    def test_preparation_is_immutable_bounded_measured_and_clipped(self):
        self.assertIsNotNone(importlib.util.find_spec('modules.rss_renderer'), 'Prepared renderer absent')
        from modules.rss_renderer import prepare_story, render_frame
        from rss_config import RenderOptions
        from rss_data import Story
        from font_catalog import FONT_DIR, available_font_names, font_key
        from frame_capture import BdfFont
        story = Story(('a',), 'Ag', 'A ' * 40, '', None)
        for name in available_font_names():
            with self.subTest(font=name):
                p = prepare_story(story, RenderOptions(headline_font=name, summary_font=name))
                self.assertTrue(all(line.width <= 64 or len(line.glyphs) == 1 for line in p.lines))
                font = BdfFont(next(path for path in FONT_DIR.glob('*.bdf') if font_key(path) == name))
                _, points = font.text_pixels(0, 0, 'Ag')
                height = max(y for x, y in points) - min(y for x, y in points) + 1
                self.assertEqual(p.headline.height, height)
                self.assertEqual(p.warnings['headline_clipped'], height > 7)
                image, info = render_frame(p, 0)
                self.assertEqual(image.size, (64, 32))
                # Summary's first lines are visible and still at tick 0 (no empty area, no rise from below).
                self.assertIsNotNone(image.crop((0, 8, 64, 32)).getbbox())
                self.assertEqual(info['summary_y'], 0)
                expected = {(x - min(0, min(a for a, b in points)), y - min(b for a, b in points)) for x, y in points}
                actual = {(x, y) for y in range(7) for x in range(64) if image.getpixel((x, y)) != (0, 0, 0)}
                self.assertEqual(actual, {(x, y) for x, y in expected if 0 <= x < 64 and 0 <= y < 7})
                with self.assertRaises(TypeError):
                    p.warnings['headline_clipped'] = False
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    p.lines = ()
        with self.assertRaises(ValueError):
            prepare_story(dataclasses.replace(story, title='x' * 301))
        with self.assertRaises(ValueError):
            prepare_story(dataclasses.replace(story, summary='x' * 2001))


    def test_passes_finish_restore_freeze_and_exact_duration(self):
        """Headline first, then summary; nothing ever blank or jumping.

        Phase 1: a headline wider than its row loops once seamlessly (a copy
        follows after GAP pixels) and rests at its start; the summary's first
        lines are shown still. Phase 2: the summary scrolls up and off while
        the headline stays still. headline_mode is legacy and has no effect.
        """
        from modules import rss_renderer as r
        from rss_config import RenderOptions
        from rss_data import Story
        self.assertTrue(hasattr(r, 'image_frames'), 'Shared frame timeline absent')
        for mode in ('once',):
            for summary in ('A', 'A ' * 160):
                p = r.prepare_story(Story(('a',), 'A' * 20, summary, '', None), RenderOptions(headline_mode=mode))
                frames = list(r.image_frames((p,)))
                loop = p.headline.width + r.HEADLINE_GAP
                self.assertGreater(loop, r.READ_HOLD)
                scroll = (len(p.lines) - 1) * p.line_pitch + p.lines[-1].height
                self.assertEqual(len(frames), loop + scroll + 1)
                self.assertAlmostEqual(sum(delay for image, delay, info in frames), (loop + scroll + 1) * .05)
                self.assertEqual(r.duration_seconds((p,)), (loop + scroll + 1) * .05)
                start_row = frames[0][0].crop((0, 0, 64, 7)).tobytes()
                # Headline phase: summary still at the top, headline moves 1px per frame.
                for tick in (0, 1, loop // 2, loop - 1):
                    self.assertEqual(frames[tick][2]['headline_x'], -tick)
                    self.assertEqual(frames[tick][2]['summary_y'], 0)
                    self.assertIsNotNone(frames[tick][0].crop((0, 0, 64, 7)).getbbox(), 'headline row never blank')
                    self.assertIsNotNone(frames[tick][0].crop((0, 8, 64, 32)).getbbox(), 'summary never blank while headline scrolls')
                # Seamless: after one loop the row is exactly the start, then it stays still.
                self.assertEqual(frames[loop][0].crop((0, 0, 64, 7)).tobytes(), start_row)
                for tick in (loop, loop + scroll // 2, loop + scroll):
                    self.assertEqual(frames[tick][2]['headline_x'], 0)
                    self.assertEqual(frames[tick][0].crop((0, 0, 64, 7)).tobytes(), start_row)
                # Summary phase: scrolls up 1px per frame until it has left the area.
                self.assertEqual(frames[loop + 1][2]['summary_y'], -1)
                self.assertFalse(frames[loop + scroll - 1][2]['summary_done'])
                self.assertIsNotNone(frames[loop + scroll - 1][0].crop((0, 8, 64, 32)).getbbox())
                self.assertTrue(frames[loop + scroll][2]['summary_done'])
                self.assertIsNone(frames[loop + scroll][0].crop((0, 8, 64, 32)).getbbox())
        self.assertEqual(list(r.image_frames(())), [])
        self.assertEqual(r.duration_seconds(()), 0)
        blank = r.prepare_story(Story((), '', '', '', None))
        self.assertEqual(list(r.image_frames((blank,))), [])
        self.assertEqual(r.duration_seconds((blank,)), 0)
        # A short static header without overview is a single bounded tick.
        short = r.prepare_story(Story((), 'A', '', '', None))
        self.assertEqual(len(list(r.image_frames((short,)))), 1)

    def test_continuous_mode_loops_summary_while_headline_scrolls(self):
        """Owner option: summary loops upward continuously while the headline crosses."""
        from modules import rss_renderer as r
        from rss_config import RenderOptions
        from rss_data import Story
        continuous = RenderOptions(headline_mode='continuous')
        # Long headline, short summary: the summary wraps round several times.
        p = r.prepare_story(Story(('a',), 'A' * 20, 'A ' * 20, '', None), continuous)
        loop = p.headline.width + r.HEADLINE_GAP
        cycle = (len(p.lines) - 1) * p.line_pitch + p.lines[-1].height + r.SUMMARY_GAP
        self.assertGreater(loop, cycle)
        frames = list(r.image_frames((p,)))
        self.assertEqual(len(frames), loop + 1)
        self.assertEqual(r.duration_seconds((p,)), (loop + 1) * .05)
        start_row = frames[0][0].crop((0, 0, 64, 7)).tobytes()
        start_summary = frames[0][0].crop((0, 8, 64, 32)).tobytes()
        for tick in (1, cycle - 1, cycle + 1, loop - 1):
            # Both move together from the first frame.
            self.assertEqual(frames[tick][2]['headline_x'], -(tick % loop))
            self.assertEqual(frames[tick][2]['summary_y'], -(tick % cycle))
            self.assertIsNotNone(frames[tick][0].crop((0, 0, 64, 7)).getbbox(), 'headline row never blank')
        # The summary wraps seamlessly to its start and keeps going.
        self.assertEqual(frames[cycle][0].crop((0, 8, 64, 32)).tobytes(), start_summary)
        self.assertEqual(frames[cycle][2]['summary_y'], 0)
        # Pass ends with the headline back at its start.
        self.assertEqual(frames[loop][0].crop((0, 0, 64, 7)).tobytes(), start_row)
        self.assertTrue(frames[loop][2]['summary_done'])
        # Short headline, long summary: the summary completes one full cycle.
        q = r.prepare_story(Story(('a',), 'Hi', 'A ' * 160, '', None), continuous)
        qcycle = (len(q.lines) - 1) * q.line_pitch + q.lines[-1].height + r.SUMMARY_GAP
        qframes = list(r.image_frames((q,)))
        self.assertEqual(len(qframes), qcycle + 1)
        self.assertTrue(all(info['headline_x'] == 0 for _, _, info in qframes))
        self.assertEqual(qframes[qcycle][0].crop((0, 8, 64, 32)).tobytes(), qframes[0][0].crop((0, 8, 64, 32)).tobytes())
        # Very short story still holds READ_HOLD ticks.
        s = r.prepare_story(Story(('a',), 'Hi', 'A', '', None), continuous)
        self.assertEqual(len(list(r.image_frames((s,)))), max(r.READ_HOLD, s.lines[-1].height + r.SUMMARY_GAP) + 1)
        # Default stays headline-first.
        self.assertEqual(RenderOptions().headline_mode, 'once')

    def test_short_headline_does_not_scroll_and_banner_narrows_the_row(self):
        from modules import rss_renderer as r
        from rss_data import Story
        from rss_banners import site_banner
        p = r.prepare_story(Story(('a',), 'Hi', 'A', '', None))
        frames = list(r.image_frames((p,)))
        scroll = p.lines[-1].height
        # Nothing to loop: the summary is held still for READ_HOLD ticks, then scrolls off.
        self.assertEqual(len(frames), r.READ_HOLD + scroll + 1)
        self.assertTrue(all(info['headline_x'] == 0 for _, _, info in frames))
        self.assertEqual(frames[r.READ_HOLD][2]['summary_y'], 0)
        self.assertEqual(frames[r.READ_HOLD + 1][2]['summary_y'], -1)
        # 47px headline fits a plain row (64) but not beside the 15px banner (48).
        title = 'A' * 12
        plain = r.prepare_story(Story(('a',), title, 'A', '', None))
        self.assertLessEqual(plain.headline.width, 64)
        banner = r.prepare_story(Story(('a',), title, 'A', '', None),
                                 banner=site_banner('https://feeds.bbci.co.uk/news/rss.xml', ''))
        if banner.headline.width > 64 - 16:
            loop = banner.headline.width + r.HEADLINE_GAP
            self.assertEqual(len(list(r.image_frames((banner,)))), max(loop, r.READ_HOLD) + scroll + 1)
            self.assertEqual(len(list(r.image_frames((plain,)))), r.READ_HOLD + scroll + 1, 'fits without banner: no scroll')


    def test_prepared_thumbnails_source_cadence_and_image_regions(self):
        from modules import rss_renderer as r
        from rss_config import RenderOptions
        from rss_data import Story
        from font_catalog import available_font_names
        self.assertTrue(hasattr(r, 'PreparedThumbnail'), 'Prepared thumbnail boundary absent')
        red, blue = bytes((255, 0, 0)) * 256, bytes((0, 0, 255)) * 256
        thumbnail = r.PreparedThumbnail((red, blue), (30, 70))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            thumbnail.frames = ()
        for frames, durations in (((), ()), ((red,), (0,)), ((red[:-1],), (20,)),
                                  ((bytearray(red),), (20,)), ((red,) * 301, (20,) * 301),
                                  ([red], (20,)), ((red,), (True,))):
            with self.subTest(durations=str(durations)[:20]), self.assertRaises(ValueError):
                r.PreparedThumbnail(frames, durations)
        story = Story((), 'Ag' * 10, 'A ' * 40, '', None)
        for name in available_font_names():
            p = r.prepare_story(story, RenderOptions(headline_font=name, summary_font=name), thumbnail=thumbnail)
            self.assertEqual(p.warnings['headline_available_height'], 16)
            self.assertEqual(p.warnings['headline_clipped'], p.headline.height > 16)
            for tick in (0, 10, 25, 100):
                image, info = r.render_frame(p, tick)
                self.assertEqual(image.crop((0, 0, 16, 16)).tobytes(), red)
                self.assertIsNone(image.crop((16, 0, 17, 16)).getbbox())
                self.assertIsNone(image.crop((0, 16, 64, 17)).getbbox())
                if tick == 0:
                    self.assertIsNotNone(image.crop((0, 17, 64, 32)).getbbox(), 'summary shown, still, from the start')
                    self.assertEqual(info['summary_y'], 0)
        for speed in (10, 100, 400):
            p = r.prepare_story(story, RenderOptions(thumbnail_speed=speed), thumbnail=thumbnail)
            frames = list(r.image_frames((p,)))
            transitions = [(info['elapsed_seconds'], info['thumbnail_index']) for i, (image, delay, info) in enumerate(frames)
                           if i == 0 or info['thumbnail_index'] != frames[i - 1][2]['thumbnail_index']]
            self.assertEqual(transitions[:3], [(0.0, 0), (.03 * 100 / speed, 1), (.1 * 100 / speed, 0)])
            self.assertAlmostEqual(sum(d for im, d, info in frames), r.duration_seconds((p,)))
            for im, delay, info in frames:
                self.assertGreater(delay, 0)
                self.assertEqual(im.crop((0, 0, 16, 16)).tobytes(), (red, blue)[info['thumbnail_index']])
        static = r.prepare_story(story, thumbnail=r.PreparedThumbnail((red,), (30,)))
        self.assertTrue(all(info['thumbnail_index'] == 0 for im, delay, info in r.image_frames((static,))))


    def test_physical_preview_pixels_waits_and_cancellation_share_generator(self):
        import base64
        import io
        from unittest.mock import patch
        from PIL import Image
        from modules import rss_renderer as r
        from rss_data import Story
        self.assertTrue(hasattr(r, 'run') and hasattr(r, 'preview_frames'), 'Shared output adapters absent')
        p = r.prepare_story(Story((), 'A' * 20, 'A ' * 150, '', None))
        expected = list(r.image_frames((p,)))
        class Clock:
            now = 0.0
            def __call__(self):
                return self.now
        class Stop:
            stopped = False
            def is_set(self):
                return self.stopped
            def wait(self, delay):
                clock.now += delay
                if stage == 'wait':
                    self.stopped = True
                return self.stopped
        class Matrix:
            def __init__(self):
                self.frames = []
            def draw_image(self, image):
                self.image = image.copy()
                clock.now += .002
                if stage == 'draw':
                    stop.stopped = True
            def swap(self):
                self.frames.append(self.image.tobytes())
                clock.now += .001
        for stage, count in (('none', len(expected)), ('before', 0), ('draw', 0), ('wait', 1)):
            clock, stop, matrix = Clock(), Stop(), Matrix()
            stop.stopped = stage == 'before'
            with patch.object(r, '_font', side_effect=AssertionError('Font IO in playback')):
                result = r.run(matrix, (p,), stop, clock=clock)
            self.assertEqual(len(matrix.frames), count)
            self.assertEqual(result.frames, count)
            self.assertEqual(matrix.frames, [im.tobytes() for im, delay, info in expected[:count]])
            self.assertAlmostEqual(result.elapsed_seconds, clock.now)
            if stage == 'none':
                self.assertAlmostEqual(clock.now, r.duration_seconds((p,)))
        preview = list(r.preview_frames((p,)))
        for encoded, (image, delay, info) in zip(preview, expected, strict=True):
            self.assertEqual(Image.open(io.BytesIO(base64.b64decode(encoded['png']))).tobytes(), image.tobytes())
            self.assertAlmostEqual(encoded['delay_ms'], delay * 1000)
            self.assertEqual(encoded['rss'], info)
        stop.stopped = True
        self.assertEqual(list(r.preview_frames((p,), stop)), [])
        # Cancellation inside frame generation must also prevent draw/swap.
        stop.stopped = False
        original = r.render_frame
        def cancel_during_frame(*args, **kwargs):
            result = original(*args, **kwargs)
            stop.stopped = True
            return result
        matrix = Matrix()
        with patch.object(r, 'render_frame', side_effect=cancel_during_frame):
            self.assertEqual(r.run(matrix, (p,), stop, clock=clock).frames, 0)
        self.assertEqual(matrix.frames, [])


    def test_pass_admission_is_immutable_and_bounded(self):
        from modules import rss_renderer as r
        from rss_data import Story
        blank = r.prepare_story(Story((), '', '', '', None))
        for stories in ([blank], (blank,) * 51, iter((blank,)), (object(),)):
            with self.subTest(type=type(stories)), self.assertRaises(ValueError):
                list(r.image_frames(stories))
        for stories in ([blank], (blank,) * 51, (object(),)):
            with self.assertRaises(ValueError):
                r.duration_seconds(stories)
        self.assertEqual(list(r.image_frames((blank,) * 50)), [])


    def test_capture_adapter_literal_timing_and_bounded_rasters(self):
        """Acceptance on existing implementation; no production change expected."""
        from unittest.mock import patch
        from PIL import Image
        from edit_preview import CaptureMatrix
        from modules import rss_renderer as r
        from rss_config import RenderOptions
        from rss_data import Story
        # Pinned to 4x6 so the 5px summary arithmetic below is independent of the default font.
        p = r.prepare_story(Story((), 'A' * 20, 'A', '', None), RenderOptions(summary_font='4x6'))
        frames = list(r.image_frames((p,)))
        # 100px headline loops once (100 + 16 gap = 116 ticks), then the 5px summary scrolls off.
        self.assertEqual(len(frames), 116 + 5 + 1)
        self.assertAlmostEqual(r.duration_seconds((p,)), 122 * .05)
        self.assertEqual(frames[100][2]['headline_x'], -100)
        self.assertEqual(frames[115][2]['headline_x'], -115)
        self.assertEqual(frames[116][2]['headline_x'], 0)
        self.assertEqual(frames[116][2]['summary_y'], 0)
        self.assertEqual(frames[120][2]['summary_y'], -4)
        self.assertFalse(frames[120][2]['summary_done'])
        self.assertEqual(frames[121][2]['summary_y'], -5)
        self.assertTrue(frames[121][2]['summary_done'])
        large = r.prepare_story(Story((), 'W' * 300, 'W' * 2000, '', None))
        self.assertEqual(sum(len(line.glyphs) for line in large.lines), 2000)
        sizes = []
        create = Image.new
        def bounded_new(mode, size, *args, **kwargs):
            sizes.append(size)
            return create(mode, size, *args, **kwargs)
        with patch.object(Image, 'new', side_effect=bounded_new):
            for tick in (0, 10, 100, 700):
                r.render_frame(large, tick)
        self.assertTrue(all(w <= 64 and h <= 32 for w, h in sizes))
        thumb = r.PreparedThumbnail((bytes((255, 0, 0)) * 256, bytes((0, 255, 0)) * 256), (30, 70))
        image_story = r.prepare_story(Story((), 'A' * 20, 'A ' * 40, '', None), thumbnail=thumb)
        expected = iter(r.image_frames((image_story,)))
        class Capturing(CaptureMatrix):
            count = 0
            def swap(self, canvas=None):
                image, delay, info = next(expected)
                self_test.assertEqual(self.snapshot(canvas).tobytes(), image.tobytes())
                self.count += 1
                return super().swap(canvas)
        self_test, now = self, [0.0]
        def sleep(delay):
            now[0] += delay
        matrix = Capturing()
        with patch.object(CaptureMatrix, 'media_frames', side_effect=AssertionError('Decoder in playback')):
            result = r.run(matrix, (image_story,), clock=lambda: now[0], sleep=sleep)
        self.assertEqual(result.frames, matrix.count)
        self.assertIsNone(next(expected, None))
        self.assertAlmostEqual(result.elapsed_seconds, r.duration_seconds((image_story,)))


if __name__ == '__main__':
    unittest.main()
