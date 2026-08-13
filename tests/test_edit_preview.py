import base64
import copy
import gc
import io
import json
import unittest
import weakref
from pathlib import Path

from PIL import Image

from config_store import DEFAULT_CONFIG, validate_config
from edit_preview import (
    CaptureMatrix, PreviewSession, custom_text_preview_frames, preview_frames,
    static_media_frames,
)
from frame_capture import BdfFont, FrameCapture
from modules.media_player import play_media


class Colour:
    def __init__(self, red, green, blue):
        self.red = red
        self.green = green
        self.blue = blue


class EditPreviewTests(unittest.TestCase):
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
