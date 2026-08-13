import base64
import copy
import gc
import io
import json
import importlib
import sys
import tempfile
import types
import unittest
import weakref
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, ImageSequence

from config_store import DEFAULT_CONFIG, validate_config
import frame_capture as frame_capture_module
from edit_preview import (
    CaptureMatrix, PreviewSession, custom_text_preview_frames, preview_frames,
    static_announcement_preview_frames, static_media_frames,
)
from frame_capture import BdfFont, FrameCapture
from modules.media_player import play_media
import edit_preview


class Colour:
    def __init__(self, red, green, blue):
        self.red = red
        self.green = green
        self.blue = blue


class EditPreviewTests(unittest.TestCase):
    def test_finite_cycle_can_restart_only_after_terminal_page(self):
        frame_capture_patch = patch.object(frame_capture_module, "Image", Image)
        frame_capture_patch.start()
        self.addCleanup(frame_capture_patch.stop)

        custom_options = {
            "type": "text_scroll",
            "text": "RESTART",
            "font": "5x7",
            "colour": "#ffffff",
            "background": "#001122",
            "y": 20,
            "direction": "left",
            "speed": 0.05,
            "loops": 1,
        }
        announcement = {
            "id": "announcement_restart",
            "enabled": True,
            "name": "Restart lifecycle",
            "layout": "text_notice",
            "headline": "Welcome",
            "message": "Terminal page required",
            "media": None,
            "schedule": {
                "start_date": "2030-01-15",
                "end_date": "2030-01-15",
                "start_time": "09:00",
                "end_time": "17:00",
                "days": [0, 1, 2, 3, 4, 5, 6],
            },
            "style": {
                "foreground": "#ffffff",
                "background": "#001122",
                "font": "5x7",
                "alignment": "left",
                "border": "none",
                "border_colour": "#ffffff",
            },
            "timing": {"mode": "static", "duration_seconds": 1.2},
            "transition": {"type": "fade", "duration_seconds": 0.4},
        }
        validated = copy.deepcopy(DEFAULT_CONFIG)
        validated["announcement_board"]["announcements"] = [announcement]
        validate_config(validated, enforce_save_bounds=True)
        announcement = validated["announcement_board"]["announcements"][0]

        first = Image.new("RGB", (2, 2), "red")
        second = Image.new("RGB", (2, 2), "blue")
        with tempfile.TemporaryDirectory() as directory:
            media_path = Path(directory) / "restart.gif"
            first.save(
                media_path, format="GIF", save_all=True, append_images=[second],
                duration=[100, 200], loop=0,
            )
            cases = {
                "custom_text": lambda: custom_text_preview_frames(
                    CaptureMatrix(), custom_options,
                ),
                "announcement": lambda: static_announcement_preview_frames(
                    CaptureMatrix(), announcement,
                ),
                "media": lambda: edit_preview.media_preview_frames(
                    CaptureMatrix(), media_path, 0.32,
                    settings={
                        "x": 0, "y": 0, "width": 64, "height": 32,
                        "keep_aspect": True, "gif_speed_percent": 200,
                    },
                ),
            }

            for name, iterator_factory in cases.items():
                with self.subTest(iterator=name):
                    session = PreviewSession(iterator_factory())
                    session.MAX_FRAMES = 2
                    first_page = session.page()
                    self.assertFalse(first_page["complete"])
                    first_token = first_page["next_token"]

                    with self.assertRaises(RuntimeError):
                        session.restart(iterator_factory)
                    with self.assertRaises(ValueError):
                        session.page("out-of-order-token")

                    pages = [first_page]
                    token = first_token
                    while True:
                        page = session.page(token)
                        pages.append(page)
                        if page["complete"]:
                            break
                        if len(pages) == 2:
                            with self.assertRaises(ValueError):
                                session.page(first_token)
                        token = page["next_token"]

                    self.assertEqual(
                        [page["page"] for page in pages], list(range(len(pages))),
                    )
                    continuation_tokens = [
                        page["next_token"] for page in pages[:-1]
                    ]
                    self.assertEqual(
                        len(continuation_tokens), len(set(continuation_tokens)),
                    )
                    self.assertIsNone(pages[-1]["next_token"])
                    with self.assertRaises(StopIteration):
                        session.page(token)

                    restarted = session.restart(iterator_factory)
                    restarted.MAX_FRAMES = 2
                    restarted_pages = []
                    restarted_token = None
                    while True:
                        restarted_page = restarted.page(restarted_token)
                        restarted_pages.append(restarted_page)
                        if restarted_page["complete"]:
                            break
                        restarted_token = restarted_page["next_token"]

                    self.assertIsNot(restarted, session)
                    self.assertEqual(restarted_pages[0]["page"], 0)
                    self.assertEqual(
                        restarted_pages[0]["frames"][0], first_page["frames"][0],
                    )
                    self.assertEqual(
                        [frame for page in restarted_pages for frame in page["frames"]],
                        [frame for page in pages for frame in page["frames"]],
                    )
                    self.assertTrue(restarted_pages[-1]["complete"])

    def test_media_timing_and_loops_complete_before_terminal_page(self):
        frame_capture_patch = patch.object(frame_capture_module, "Image", Image)
        frame_capture_patch.start()
        self.addCleanup(frame_capture_patch.stop)

        first = Image.new("RGB", (2, 2), "red")
        second = Image.new("RGB", (2, 2), "blue")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "timeline.gif"
            first.save(
                path, format="GIF", save_all=True, append_images=[second],
                duration=[100, 200], loop=0,
            )

            fake_graphics = types.SimpleNamespace(
                Color=lambda *rgb: types.SimpleNamespace(
                    red=rgb[0], green=rgb[1], blue=rgb[2],
                ),
                Font=Mock,
            )
            fake_rgb = types.SimpleNamespace(
                RGBMatrix=Mock,
                RGBMatrixOptions=Mock,
                graphics=fake_graphics,
            )
            previous = sys.modules.get("rgbmatrix")
            sys.modules["rgbmatrix"] = fake_rgb
            try:
                framework = importlib.import_module("matrix_framework")
            finally:
                if previous is None:
                    sys.modules.pop("rgbmatrix", None)
                else:
                    sys.modules["rgbmatrix"] = previous

            class Canvas:
                def SetImage(self, image, x, y):
                    pass

                def Clear(self):
                    pass

            class PhysicalMatrix(framework.Matrix):
                def __init__(self):
                    self.matrix = types.SimpleNamespace(width=64, height=32)
                    self.canvas = Canvas()
                    self._frame_capture = FrameCapture(64, 32)
                    self._frame_capture.register(self.canvas)
                    self.submissions = []

                def _media_file(self, filename):
                    return path

                def swap(self, canvas=None):
                    self.submissions.append(self._frame_capture.snapshot(self.canvas))
                    return self.canvas

            class AdvancingStop:
                def __init__(self):
                    self.elapsed = 0.0
                    self.waits = []

                def is_set(self):
                    return False

                def wait(self, delay):
                    self.waits.append(delay)
                    self.elapsed += delay
                    return False

            physical = PhysicalMatrix()
            stop = AdvancingStop()
            with patch.object(
                framework.time, "monotonic", side_effect=lambda: stop.elapsed,
            ), patch.object(
                framework.Image, "open", side_effect=Image.open,
            ), patch.object(
                framework.ImageSequence, "Iterator", side_effect=ImageSequence.Iterator,
            ):
                self.assertTrue(
                    play_media(
                        physical, str(path), 0.32, stop_event=stop,
                        settings={
                            "x": 0, "y": 0, "width": 64, "height": 32,
                            "keep_aspect": True, "gif_speed_percent": 200,
                            "video_fps": 20,
                        },
                    )
                )

            session = PreviewSession(
                edit_preview.media_preview_frames(
                    CaptureMatrix(), str(path), 0.32,
                    settings={
                        "x": 0, "y": 0, "width": 64, "height": 32,
                        "keep_aspect": True, "gif_speed_percent": 200,
                        "video_fps": 20,
                    },
                )
            )
            session.MAX_FRAMES = 2
            pages = []
            token = None
            while True:
                page = session.page(token)
                pages.append(page)
                if page["complete"]:
                    break
                token = page["next_token"]

        frames = [frame for page in pages for frame in page["frames"]]
        preview_images = [
            Image.open(io.BytesIO(base64.b64decode(frame["png"]))).copy()
            for frame in frames
        ]
        self.assertEqual([page["page"] for page in pages], [0, 1, 2])
        self.assertTrue(all(not page["complete"] for page in pages[:-1]))
        self.assertTrue(pages[-1]["complete"])
        self.assertIsNone(pages[-1]["next_token"])
        self.assertEqual([frame["delay_ms"] for frame in frames], [50, 100, 50, 100, 20])
        self.assertAlmostEqual(sum(stop.waits), 0.32)
        self.assertEqual(
            [image.tobytes() for image in preview_images],
            [image.tobytes() for image in physical.submissions],
        )
        self.assertEqual(
            [image.getpixel((0, 0)) for image in preview_images],
            [(255, 0, 0), (0, 0, 255), (255, 0, 0), (0, 0, 255), (255, 0, 0)],
        )
        with self.assertRaises(StopIteration):
            session.page()

    def test_capture_adapter_matches_physical_capture_for_64_by_32_primitives(self):
        font_path = Path(__file__).parents[1] / "resources" / "fonts" / "5x7.bdf"
        font = BdfFont(font_path)
        colour = Colour(255, 0, 0)
        image = Image.new("RGB", (2, 2), (0, 255, 0))

        physical = FrameCapture(64, 32)
        canvas = object()
        physical.register(canvas)
        physical.fill(canvas, 1, 2, 3)
        physical.pixel(canvas, 0, 0, 4, 5, 6)
        physical.pixel(canvas, 63, 31, 7, 8, 9)
        physical.pixel(canvas, 64, 32, 255, 255, 255)
        physical.line(canvas, -2, 4, 4, 4, colour, width=2)
        physical.circle(canvas, 10, 10, 3, colour, filled=True)
        physical.set_image(canvas, image, 63, 30)
        expected_advance = physical.text(canvas, font, 1, 20, colour, "!")
        advance, points = font.text_pixels(24, 20, "!")
        centre_x = (min(x for x, _ in points) + max(x for x, _ in points)) / 2
        centre_y = (min(y for _, y in points) + max(y for _, y in points)) / 2
        for x, y in points:
            physical.pixel(
                canvas,
                round(centre_x - (y - centre_y)),
                round(centre_y + (x - centre_x)),
                colour.red,
                colour.green,
                colour.blue,
            )
        physical.publish(canvas)
        expected = Image.open(physical.png()).copy()

        preview = CaptureMatrix(64, 32)
        preview.fill(1, 2, 3)
        preview.pixel(0, 0, 4, 5, 6)
        preview.pixel(63, 31, 7, 8, 9)
        preview.pixel(64, 32, 255, 255, 255)
        preview.draw_line(-2, 4, 4, 4, colour, width=2)
        preview.draw_circle(10, 10, 3, colour, filled=True)
        preview.draw_image(image, x=63, y=30, keep_aspect=False)
        actual_advance = preview.draw_text(font, 1, 20, colour, "!")
        preview.draw_text(font, 24, 20, colour, "!", rotation=90)

        self.assertEqual(actual_advance, expected_advance)
        self.assertEqual(preview.snapshot().tobytes(), expected.tobytes())
        self.assertEqual(preview.snapshot().getpixel((0, 0)), (4, 5, 6))
        self.assertEqual(preview.snapshot().getpixel((63, 31)), (0, 255, 0))

    def test_physical_wrappers_and_preview_consume_the_same_frame_iterators(self):
        class FakeMatrix:
            def __init__(self):
                self.shown = []

            def show_image(self, filename, delay=None, **geometry):
                self.shown.append((filename, delay, geometry))
                return True

        class FakeStopEvent:
            def __init__(self):
                self.waits = []

            def wait(self, delay):
                self.waits.append(delay)
                return False

        filename = "resources/media/still.png"
        settings = {"x": 2, "y": 3, "width": 20, "height": 10, "keep_aspect": True}
        expected = list(preview_frames(static_media_frames(filename, 1.25, settings)))
        matrix = FakeMatrix()
        stop_event = FakeStopEvent()

        rendered = play_media(matrix, filename, 1.25, stop_event=stop_event, settings=settings)

        self.assertEqual(expected, [(filename, 1250.0, settings)])
        self.assertTrue(rendered)
        self.assertEqual(matrix.shown, [(filename, None, settings)])
        self.assertEqual(stop_event.waits, [expected[0][1] / 1000])

    def test_announcement_transition_and_hold_complete_before_terminal_page(self):
        import modules.announcement_board as announcement_board

        class RecordingCaptureMatrix(CaptureMatrix):
            def __init__(self):
                super().__init__()
                self.submissions = []

            def swap(self, canvas=None):
                self.submissions.append(self.snapshot(canvas))
                return super().swap(canvas)

        class RecordingStopEvent:
            def __init__(self):
                self.waits = []

            def is_set(self):
                return False

            def wait(self, delay):
                self.waits.append(delay)
                return False

        announcement = {
            "id": "announcement_preview_fade",
            "enabled": True,
            "name": "Preview fade",
            "layout": "text_notice",
            "headline": "Welcome",
            "message": "Front desk closes at 5 PM",
            "media": None,
            "schedule": {
                "start_date": "2030-01-15",
                "end_date": "2030-01-15",
                "start_time": "09:00",
                "end_time": "17:00",
                "days": [0, 1, 2, 3, 4, 5, 6],
            },
            "style": {
                "foreground": "#ffffff",
                "background": "#001122",
                "font": "5x7",
                "alignment": "left",
                "border": "none",
                "border_colour": "#ffffff",
            },
            "timing": {"mode": "static", "duration_seconds": 1.2},
            "transition": {"type": "fade", "duration_seconds": 0.4},
        }
        validated = copy.deepcopy(DEFAULT_CONFIG)
        validated["announcement_board"]["announcements"] = [announcement]
        validate_config(validated, enforce_save_bounds=True)
        announcement = validated["announcement_board"]["announcements"][0]

        physical = RecordingCaptureMatrix()
        stop_event = RecordingStopEvent()
        self.assertTrue(
            announcement_board.run_preview(physical, announcement, stop_event=stop_event)
        )

        preview_matrix = CaptureMatrix()
        session = PreviewSession(
            static_announcement_preview_frames(preview_matrix, announcement)
        )
        session.MAX_FRAMES = int(4)
        transition_page = session.page()
        self.assertFalse(transition_page["complete"])
        self.assertIsNotNone(transition_page["next_token"])
        terminal_page = session.page(transition_page["next_token"])

        pages = [transition_page, terminal_page]
        frames = [frame for page in pages for frame in page["frames"]]
        preview_images = [
            Image.open(io.BytesIO(base64.b64decode(frame["png"]))).copy()
            for frame in frames
        ]
        self.assertEqual([len(page["frames"]) for page in pages], [4, 1])
        self.assertTrue(terminal_page["complete"])
        self.assertIsNone(terminal_page["next_token"])
        self.assertEqual([page["page"] for page in pages], [0, 1])
        self.assertTrue(all(len(page["frames"]) <= session.MAX_FRAMES for page in pages))
        self.assertEqual(
            [image.tobytes() for image in preview_images],
            [image.tobytes() for image in physical.submissions],
        )
        self.assertEqual(
            [frame["delay_ms"] for frame in frames],
            [delay * 1000 for delay in stop_event.waits],
        )
        self.assertEqual([frame["delay_ms"] for frame in frames], [100, 100, 100, 100, 1200])
        self.assertEqual(sum(frame["delay_ms"] for frame in frames), 1600)
        self.assertEqual(
            preview_images[-1].tobytes(), preview_images[-2].tobytes(),
            "the single hold frame must retain the completed transition image",
        )
        with self.assertRaises(StopIteration):
            session.page()

    def test_long_left_ticker_pages_complete_every_configured_loop_and_reset(self):
        options = {
            "type": "text_scroll",
            "text": "A LONG LEFT TICKER",
            "font": "5x7",
            "colour": "#ffcc00",
            "background": "#001122",
            "y": 20,
            "direction": "left",
            "speed": 0.05,
            "loops": 2,
        }
        session = PreviewSession(custom_text_preview_frames(CaptureMatrix(), options))
        pages = []
        token = None
        while True:
            page = session.page(token)
            pages.append(page)
            encoded = json.dumps(page, separators=(",", ":")).encode()
            self.assertLessEqual(len(page["frames"]), 32)
            self.assertLessEqual(len(encoded), 512 * 1024)
            self.assertLessEqual(session.retained_frame_count, 33)
            if page["complete"]:
                break
            token = page["next_token"]

        frames = [frame for page in pages for frame in page["frames"]]
        font = BdfFont(Path(__file__).parents[1] / "resources" / "fonts" / "5x7.bdf")
        text_width = font.text_pixels(0, options["y"], options["text"])[0]
        frames_per_loop = 64 + text_width + 1
        self.assertEqual(len(frames), frames_per_loop * options["loops"])
        self.assertTrue(all(len(page["frames"]) == 32 for page in pages[:-1]))
        self.assertEqual([page["page"] for page in pages], list(range(len(pages))))
        self.assertTrue(all(not page["complete"] for page in pages[:-1]))
        self.assertIsNone(pages[-1]["next_token"])
        self.assertEqual(sum(frame["delay_ms"] for frame in frames), len(frames) * 50)

        def image(frame):
            return Image.open(io.BytesIO(base64.b64decode(frame["png"]))).copy()

        first = image(frames[0])
        reset = image(frames[frames_per_loop])
        last = image(frames[-1])
        self.assertEqual(first.tobytes(), reset.tobytes())
        self.assertEqual(first.getpixel((0, 0)), (0, 17, 34))
        self.assertEqual(last.getbbox(), (0, 0, 64, 32))
        with self.assertRaises(StopIteration):
            session.page()

        restarted = PreviewSession(custom_text_preview_frames(CaptureMatrix(), options))
        restarted_first = image(restarted.page()["frames"][0])
        self.assertEqual(restarted_first.tobytes(), first.tobytes())

    def test_long_right_ticker_pages_complete_every_configured_loop_and_reset(self):
        options = {
            "type": "text_scroll",
            "text": "A LONG RIGHT TICKER",
            "font": "5x7",
            "colour": "#ffcc00",
            "background": "#001122",
            "y": 20,
            "direction": "right",
            "speed": 0.05,
            "loops": 2,
        }
        session = PreviewSession(custom_text_preview_frames(CaptureMatrix(), options))
        pages = []
        token = None
        while True:
            page = session.page(token)
            pages.append(page)
            encoded = json.dumps(page, separators=(",", ":")).encode()
            self.assertLessEqual(len(page["frames"]), 32)
            self.assertLessEqual(len(encoded), 512 * 1024)
            self.assertLessEqual(session.retained_frame_count, 33)
            if page["complete"]:
                break
            token = page["next_token"]

        frames = [frame for page in pages for frame in page["frames"]]
        font = BdfFont(Path(__file__).parents[1] / "resources" / "fonts" / "5x7.bdf")
        text_width = font.text_pixels(0, options["y"], options["text"])[0]
        frames_per_loop = 64 + text_width + 1
        self.assertEqual(len(frames), frames_per_loop * options["loops"])
        self.assertTrue(all(len(page["frames"]) == 32 for page in pages[:-1]))
        self.assertEqual([page["page"] for page in pages], list(range(len(pages))))
        self.assertTrue(all(not page["complete"] for page in pages[:-1]))
        self.assertIsNone(pages[-1]["next_token"])
        self.assertEqual(sum(frame["delay_ms"] for frame in frames), len(frames) * 50)

        def image(frame):
            return Image.open(io.BytesIO(base64.b64decode(frame["png"]))).copy()

        first = image(frames[0])
        reset = image(frames[frames_per_loop])
        last = image(frames[-1])
        left_options = dict(options, direction="left", loops=1)
        left_frames = list(custom_text_preview_frames(CaptureMatrix(), left_options))
        self.assertEqual(first.tobytes(), reset.tobytes())
        self.assertEqual(first.getpixel((0, 0)), (0, 17, 34))
        self.assertEqual(last.getbbox(), (0, 0, 64, 32))
        self.assertEqual(
            image(frames[text_width]).tobytes(), image(left_frames[64]).tobytes()
        )
        with self.assertRaises(StopIteration):
            session.page()

        restarted = PreviewSession(custom_text_preview_frames(CaptureMatrix(), options))
        restarted_first = image(restarted.page()["frames"][0])
        self.assertEqual(restarted_first.tobytes(), first.tobytes())

    def test_maximum_width_custom_text_ten_loops_completes_without_history_growth(self):
        class WeakFrame(dict):
            pass

        options = {
            "type": "text_scroll",
            "text": "@" * 200,
            "font": "helvR12",
            "colour": "#ffffff",
            "background": "#000000",
            "y": 31,
            "direction": "left",
            "speed": 0.01,
            "loops": 10,
        }
        validated = copy.deepcopy(DEFAULT_CONFIG)
        validated["custom_modules"] = [{
            "id": "custom_maximum", "name": "Maximum ticker", "x": 0, **options,
        }]
        validated["playlist"] = [{
            "module": "custom_maximum", "duration": 1, "enabled": True,
        }]
        validate_config(validated, enforce_save_bounds=True)

        font = BdfFont(
            Path(__file__).parents[1] / "resources" / "fonts" / f"{options['font']}.bdf"
        )
        text_width = font.text_pixels(0, options["y"], options["text"])[0]
        frames_per_loop = 64 + text_width + 1
        expected_frames = frames_per_loop * options["loops"]
        source = (
            WeakFrame(frame)
            for frame in custom_text_preview_frames(CaptureMatrix(), options)
        )
        session = PreviewSession(source)
        token = None
        page_number = 0
        frame_count = 0
        delay_total = 0
        tokens = []
        first_loop = []
        prior_frame_refs = []

        while True:
            page = session.page(token)
            gc.collect()
            self.assertTrue(all(reference() is None for reference in prior_frame_refs))
            self.assertEqual(page["page"], page_number)
            self.assertLessEqual(len(page["frames"]), 32)
            self.assertLessEqual(
                len(json.dumps(page, separators=(",", ":")).encode()), 512 * 1024,
            )
            self.assertLessEqual(session.retained_frame_count, 33)

            for frame in page["frames"]:
                loop_offset = frame_count % frames_per_loop
                signature = frame["png"]
                if frame_count < frames_per_loop:
                    first_loop.append(signature)
                else:
                    self.assertEqual(signature, first_loop[loop_offset])
                frame_count += 1
                delay_total += frame["delay_ms"]

            if page["complete"]:
                self.assertIsNone(page["next_token"])
                terminal_frame_refs = [weakref.ref(frame) for frame in page["frames"]]
                del frame
                del page
                gc.collect()
                self.assertTrue(
                    all(reference() is None for reference in terminal_frame_refs)
                )
                break
            self.assertIsInstance(page["next_token"], str)
            self.assertNotIn(page["next_token"], tokens)
            tokens.append(page["next_token"])
            token = page["next_token"]
            prior_frame_refs = [weakref.ref(frame) for frame in page["frames"]]
            del frame
            del page
            page_number += 1

        self.assertEqual(frame_count, expected_frames)
        self.assertEqual(delay_total, expected_frames * 10)
        self.assertGreater(len(set(first_loop)), 64)
        self.assertEqual(len(tokens), page_number)
        with self.assertRaises(StopIteration):
            session.page()


if __name__ == "__main__":
    unittest.main()
