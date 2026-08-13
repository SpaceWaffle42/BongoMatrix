import base64
import copy
import datetime
import gc
import io
import json
import importlib
import random
import sys
import tempfile
import threading
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
from modules import clock_lava, custom_text
import edit_preview


class Colour:
    def __init__(self, red, green, blue):
        self.red = red
        self.green = green
        self.blue = blue


class EditPreviewTests(unittest.TestCase):
    def test_cancel_before_first_frame_publishes_nothing(self):
        class RecordingMatrix(CaptureMatrix):
            def __init__(self):
                super().__init__()
                self.submissions = []

            def swap(self, canvas=None):
                self.submissions.append(self.snapshot(canvas))
                return super().swap(canvas)

        class GuardedIterator:
            def __init__(self, iterator):
                self.iterator = iterator
                self.next_calls = 0
                self.close_calls = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.next_calls += 1
                raise AssertionError("a pre-cancelled session advanced its renderer")

            def close(self):
                self.close_calls += 1
                self.iterator.close()

        candidate = copy.deepcopy(DEFAULT_CONFIG)
        candidate["lava"]["show_time"] = False
        candidate["lava"]["show_date"] = False
        matrix = RecordingMatrix()
        lava = edit_preview.continuous_lava_preview_frames(
            matrix, candidate, {"kind": "built_in", "id": "fire_burst"},
        )
        guarded = GuardedIterator(lava)
        cancelled = threading.Event()
        cancelled.set()
        session = PreviewSession(guarded, stop_event=cancelled)

        page = session.page()
        session.cancel()
        session.cancel()

        self.assertEqual(page["frames"], [])
        self.assertTrue(page["complete"])
        self.assertIsNone(page["next_token"])
        self.assertEqual(guarded.next_calls, 0)
        self.assertEqual(guarded.close_calls, 1)
        self.assertEqual(matrix.submissions, [])
        self.assertEqual(session.retained_frame_count, 0)
        self.assertIsNone(getattr(lava, "gi_frame"))
        with self.assertRaisesRegex(
            StopIteration, "explicit new session is required to restart",
        ):
            session.page(page["next_token"])

    def test_cancel_between_pages_closes_iterator_and_invalidates_token(self):
        class RecordingMatrix(CaptureMatrix):
            def __init__(self):
                super().__init__()
                self.swaps = 0
                self.submissions = []

            def swap(self, canvas=None):
                self.swaps += 1
                self.submissions.append(self.snapshot(canvas))
                return super().swap(canvas)

        class CloseCountingIterator:
            def __init__(self, iterator):
                self.iterator = iterator
                self.next_calls = 0
                self.close_calls = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.next_calls += 1
                return next(self.iterator)

            def close(self):
                self.close_calls += 1
                self.iterator.close()

        candidate = copy.deepcopy(DEFAULT_CONFIG)
        candidate["lava"].update({
            "show_time": False,
            "show_date": False,
            "blur_radius": 0,
            "blob_count": 2,
        })
        original_candidate = copy.deepcopy(candidate)
        matrix = RecordingMatrix()
        cancelled = threading.Event()
        lava = edit_preview.continuous_lava_preview_frames(
            matrix, candidate, {"kind": "built_in", "id": "fire_burst"},
            stop_event=cancelled,
        )
        guarded = CloseCountingIterator(lava)
        session = PreviewSession(guarded, stop_event=cancelled)
        session.MAX_FRAMES = 2

        with patch.object(
            clock_lava, "render_lava_frame", wraps=clock_lava.render_lava_frame,
        ) as render:
            page = session.page()
            token = page["next_token"]
            before_cancel = {
                "next_calls": guarded.next_calls,
                "renders": render.call_count,
                "swaps": matrix.swaps,
                "submissions": [image.tobytes() for image in matrix.submissions],
                "renderer": matrix.snapshot().tobytes(),
            }

            self.assertEqual(page["page"], 0)
            self.assertEqual(len(page["frames"]), 2)
            self.assertFalse(page["complete"])
            self.assertIsNotNone(token)
            self.assertEqual(session.retained_frame_count, 1)

            session.cancel()
            self.assertTrue(cancelled.is_set())
            self.assertEqual(guarded.close_calls, 1)
            self.assertEqual(session.retained_frame_count, 0)
            self.assertIsNone(getattr(lava, "gi_frame"))

            for stale_token in (token, None):
                with self.subTest(token=stale_token):
                    with self.assertRaisesRegex(
                        StopIteration,
                        "explicit new session is required to restart",
                    ):
                        session.page(stale_token)

            session.cancel()
            self.assertEqual(guarded.close_calls, 1)
            self.assertEqual(
                {
                    "next_calls": guarded.next_calls,
                    "renders": render.call_count,
                    "swaps": matrix.swaps,
                    "submissions": [image.tobytes() for image in matrix.submissions],
                    "renderer": matrix.snapshot().tobytes(),
                },
                before_cancel,
            )

        self.assertEqual(candidate, original_candidate)

    def test_duplicate_disabled_and_missing_playlist_rows_choose_documented_duration(self):
        candidate = copy.deepcopy(DEFAULT_CONFIG)
        candidate["custom_modules"] = [
            {"id": "custom_simple", "type": "text_scroll"},
            {"id": "custom_canvas", "type": "advanced_canvas"},
        ]
        candidate["playlist"] = [
            {"module": "weather", "duration": 91, "enabled": False},
            {"module": "weather", "duration": 0, "enabled": True},
            {"module": "weather", "duration": 92, "enabled": True},
            {"module": "clock_lava", "duration": 93, "enabled": False},
            {"module": "custom_simple", "duration": 7, "enabled": True},
            {"module": "custom_simple", "duration": 8, "enabled": True},
            {"module": "announcement_board", "duration": 9, "enabled": True},
            {"module": "media:example.gif", "duration": 10, "enabled": False},
        ]
        original = copy.deepcopy(candidate)

        expected = {
            # First enabled duplicate wins and zero remains a real physical duration.
            "weather": 0,
            "custom_simple": 7,
            "announcement_board": 9,
            # Disabled-only and missing rows use current physical module defaults.
            "clock_lava": 300,
            "openweather_overview": 20,
            "openweather_forecast": 30,
            "custom_canvas": 15,
            "splash": 8,
            "sleep": 60,
            "media:example.gif": 15,
        }
        for module_name, duration in expected.items():
            with self.subTest(module=module_name):
                self.assertEqual(
                    edit_preview.preview_duration(candidate, module_name), duration,
                )

        self.assertEqual(candidate, original)
        self.assertFalse(candidate["playlist"][0]["enabled"])
        self.assertTrue(candidate["playlist"][1]["enabled"])
        self.assertTrue(candidate["playlist"][2]["enabled"])
        self.assertFalse(candidate["playlist"][-1]["enabled"])

    def test_lava_preview_pages_only_selected_palette_until_cancelled(self):
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2030, 1, 15, 9, 30, tzinfo=tz)

        cases = (
            (
                "disabled built-in",
                {"kind": "built_in", "id": "fire_burst"},
                {"#000000", "#ff0000", "#ffff00"},
            ),
            (
                "disabled custom with edited colours",
                {
                    "kind": "custom", "id": "palette_inspected",
                    "colours": ["#102030", "#405060", "#90a0b0"],
                },
                {"#102030", "#405060", "#90a0b0"},
            ),
        )
        for label, descriptor, expected_colours in cases:
            with self.subTest(case=label):
                candidate = copy.deepcopy(DEFAULT_CONFIG)
                candidate["lava"].update({
                    "enabled_palettes": ["ocean_wave"],
                    "custom_enabled": False,
                    "custom_palettes": [{
                        "id": "palette_inspected", "name": "Inspected",
                        "colours": ["#102030", "#405060", "#90a0b0"],
                        "enabled": False,
                    }],
                    "blur_radius": 0,
                    "show_time": False,
                    "show_date": False,
                    "blob_count": 3,
                    "blob_speed": 0.5,
                })
                original = copy.deepcopy(candidate)

                with patch.object(frame_capture_module, "Image", Image), \
                     patch("modules.clock_lava.datetime.datetime", FixedDateTime), \
                     patch("modules.clock_lava.random.uniform", side_effect=random.Random(17).uniform):
                    iterator = edit_preview.continuous_lava_preview_frames(
                        CaptureMatrix(), candidate, descriptor,
                    )
                    session = PreviewSession(iterator)
                    session.MAX_FRAMES = 2
                    first = session.page()
                    second = session.page(first["next_token"])

                pages = (first, second)
                frames = [frame for page in pages for frame in page["frames"]]
                self.assertEqual([page["page"] for page in pages], [0, 1])
                self.assertTrue(all(not page["complete"] for page in pages))
                self.assertTrue(all(page["next_token"] for page in pages))
                self.assertEqual([frame["delay_ms"] for frame in frames], [30] * 4)
                decoded = [
                    Image.open(io.BytesIO(base64.b64decode(frame["png"]))).convert("RGB")
                    for frame in frames
                ]
                self.assertTrue(all(image.size == (64, 32) for image in decoded))
                self.assertEqual(len({image.tobytes() for image in decoded}), len(decoded))
                observed = {
                    "#%02x%02x%02x" % image.getpixel((x, y))
                    for image in decoded
                    for y in range(image.height)
                    for x in range(image.width)
                }
                self.assertTrue(observed <= expected_colours)
                self.assertGreaterEqual(len(observed), 2)
                self.assertEqual(candidate, original)

                token = second["next_token"]
                session.cancel()
                with self.assertRaises(StopIteration):
                    session.page(token)
                self.assertIsNone(getattr(iterator, "gi_frame"))
                self.assertEqual(session.retained_frame_count, 0)

    def test_lava_playlist_duration_and_default_preserve_physical_wrapper_semantics(self):
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2030, 1, 15, 9, 30, tzinfo=tz)

        config = copy.deepcopy(DEFAULT_CONFIG)
        config["lava"].update({
            "enabled_palettes": [],
            "custom_enabled": True,
            "custom_colours": ["#001122", "#ff5500", "#ffee00"],
            "show_time": False,
            "show_date": False,
            "text_colour": "#ffffff",
            "border_colour": "#000000",
            "blob_count": 2,
        })
        config["playlist"] = [
            {"module": "clock_lava", "duration": 0.05, "enabled": True},
            {"module": "clock_lava", "duration": 0.09, "enabled": True},
        ]
        original_lava = copy.deepcopy(config["lava"])
        original_playlist = copy.deepcopy(config["playlist"])

        with patch("modules.clock_lava.datetime.datetime", FixedDateTime), \
             patch("modules.clock_lava.random.uniform", side_effect=lambda low, high: (low + high) / 2):
            frames = list(edit_preview.lava_preview_frames(CaptureMatrix(), config))

        self.assertEqual([frame["delay_ms"] for frame in frames], [30, 20])
        self.assertEqual(config["lava"], original_lava)
        self.assertEqual(config["playlist"], original_playlist)

        disabled = copy.deepcopy(config)
        disabled["playlist"] = [
            {"module": "clock_lava", "duration": 1, "enabled": False},
        ]
        self.assertEqual(
            edit_preview.preview_duration(disabled, "clock_lava"), 300,
        )
        self.assertFalse(disabled["playlist"][0]["enabled"])

        with patch.object(clock_lava, "FRAME_DELAY", 100), \
             patch("modules.clock_lava.datetime.datetime", FixedDateTime), \
             patch("modules.clock_lava.random.uniform", side_effect=lambda low, high: (low + high) / 2):
            default_frames = list(edit_preview.lava_preview_frames(CaptureMatrix(), disabled))
        self.assertEqual([frame["delay_ms"] for frame in default_frames], [100000, 100000, 100000])
        self.assertFalse(disabled["playlist"][0]["enabled"])

        physical_matrix = CaptureMatrix()
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        with patch.object(clock_lava, "render_lava_frame", wraps=clock_lava.render_lava_frame) as shared, \
             patch("modules.clock_lava.datetime.datetime", FixedDateTime), \
             patch("modules.clock_lava.random.uniform", side_effect=lambda low, high: (low + high) / 2), \
             patch("modules.clock_lava.time.time", side_effect=[0, 0]):
            clock_lava.clock(
                duration=1, matrix=physical_matrix, stop_event=stop,
                lava_config=config["lava"],
            )
        self.assertEqual(shared.call_count, 1)
        self.assertEqual(clock_lava.clock.__defaults__, (300, None, None, None))
        self.assertEqual(clock_lava.run.__defaults__, (300, None))

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

    def test_gif_and_mp4_speed_repeat_and_final_delay_match_physical_invocation(self):
        frame_capture_patch = patch.object(frame_capture_module, "Image", Image)
        frame_capture_patch.start()
        self.addCleanup(frame_capture_patch.stop)

        fake_graphics = types.SimpleNamespace(
            Color=lambda *rgb: types.SimpleNamespace(
                red=rgb[0], green=rgb[1], blue=rgb[2],
            ),
            Font=Mock,
        )
        fake_rgb = types.SimpleNamespace(
            RGBMatrix=Mock, RGBMatrixOptions=Mock, graphics=fake_graphics,
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
            def __init__(self, paths, video_frames):
                self.paths = paths
                self.video_frames = video_frames
                self.video_decode_calls = 0
                self.matrix = types.SimpleNamespace(width=64, height=32)
                self.canvas = Canvas()
                self._frame_capture = FrameCapture(64, 32)
                self._frame_capture.register(self.canvas)
                self.submissions = []

            def _media_file(self, filename):
                return self.paths[filename]

            def _video_frame_timeline(self, *args, **kwargs):
                self.video_decode_calls += 1
                return iter(frame.copy() for frame in self.video_frames)

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

        settings = {
            "x": 0, "y": 0, "width": 64, "height": 32,
            "keep_aspect": False, "gif_speed_percent": 200,
            "video_fps": 20,
        }
        red = Image.new("RGB", (64, 32), "red")
        blue = Image.new("RGB", (64, 32), "blue")
        with tempfile.TemporaryDirectory() as directory:
            gif_path = Path(directory) / "timeline.gif"
            mp4_path = Path(directory) / "timeline.mp4"
            red.save(
                gif_path, format="GIF", save_all=True, append_images=[blue],
                duration=[100, 200], loop=0,
            )
            mp4_path.write_bytes(b"deterministic injected decoder input")

            for filename, duration, expected_delays, decoded in (
                ("timeline.gif", 0.32, [50, 100, 50, 100, 20], []),
                ("timeline.mp4", 0.23, [50, 50, 50, 50, 30], [red, blue]),
            ):
                with self.subTest(media=filename):
                    physical = PhysicalMatrix(
                        {filename: gif_path if filename.endswith(".gif") else mp4_path},
                        decoded,
                    )
                    stop = AdvancingStop()
                    playback_patches = [
                        patch.object(
                            framework.time, "monotonic",
                            side_effect=lambda: stop.elapsed,
                        ),
                        patch.object(framework.Image, "open", side_effect=Image.open),
                        patch.object(
                            framework.ImageSequence, "Iterator",
                            side_effect=ImageSequence.Iterator,
                        ),
                    ]
                    if filename.endswith(".mp4"):
                        playback_patches.append(patch.object(
                            framework.subprocess, "Popen",
                            side_effect=AssertionError(
                                "shared injected video decoder seam was not consumed"
                            ),
                        ))
                    with playback_patches[0], playback_patches[1], playback_patches[2]:
                        if len(playback_patches) == 4:
                            playback_patches[3].start()
                        try:
                            self.assertTrue(play_media(
                                physical, filename, duration, stop_event=stop,
                                settings=settings,
                            ))
                        finally:
                            if len(playback_patches) == 4:
                                playback_patches[3].stop()

                    preview = CaptureMatrix()
                    preview_decode_calls = [0]

                    def preview_video_decoder(*_args, **_kwargs):
                        preview_decode_calls[0] += 1
                        return iter(frame.copy() for frame in decoded)

                    session = PreviewSession(edit_preview.media_preview_frames(
                        preview,
                        gif_path if filename.endswith(".gif") else mp4_path,
                        duration,
                        settings=settings,
                        video_decoder=(
                            None if filename.endswith(".gif") else
                            preview_video_decoder
                        ),
                    ))
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
                    self.assertEqual(
                        [frame["delay_ms"] for frame in frames], expected_delays,
                    )
                    self.assertEqual(
                        [round(delay * 1000, 9) for delay in stop.waits],
                        expected_delays,
                    )
                    self.assertAlmostEqual(sum(stop.waits), duration)
                    if filename.endswith(".mp4"):
                        self.assertEqual(physical.video_decode_calls, 3)
                        self.assertEqual(preview_decode_calls[0], 3)
                    self.assertEqual(
                        [image.tobytes() for image in preview_images],
                        [image.tobytes() for image in physical.submissions],
                    )
                    self.assertTrue(all(not page["complete"] for page in pages[:-1]))
                    self.assertTrue(pages[-1]["complete"])
                    self.assertIsNone(pages[-1]["next_token"])
                    with self.assertRaises(StopIteration):
                        session.page()

    def test_advanced_canvas_hold_repeat_and_ping_pong_match_physical_invocation(self):
        frame_capture_patch = patch.object(frame_capture_module, "Image", Image)
        frame_capture_patch.start()
        self.addCleanup(frame_capture_patch.stop)

        media_frames = [
            Image.new("RGB", (64, 32), colour)
            for colour in ("#220000", "#002200", "#000022")
        ]
        layer = {
            "id": "layer_media", "name": "Media", "type": "media", "visible": True,
            "properties": {
                "source": "timeline.gif", "x": 0, "y": 0, "width": 64,
                "height": 32, "rotation": 0, "keep_aspect": False,
            },
            "keyframes": [],
        }
        keyframed = {
            "id": "layer_keyframed", "name": "Keyframed", "type": "rectangle",
            "visible": True,
            "properties": {
                "x": 0, "y": 0, "width": 2, "height": 2, "rotation": 0,
                "colour": "#ff0000", "filled": True,
            },
            "keyframes": [
                {"frame": 0, "easing": "linear", "properties": {"x": 0, "colour": "#ff0000"}},
                {"frame": 2, "easing": "linear", "properties": {"x": 4, "colour": "#0000ff"}},
            ],
        }

        class RecordingMatrix(CaptureMatrix):
            def __init__(self):
                super().__init__()
                self.submissions = []
                self.decode_calls = []

            def media_frames(self, source, frame_count, fps, **kwargs):
                self.decode_calls.append((source, frame_count, fps, kwargs))
                return [image.copy() for image in media_frames]

            def swap(self, canvas=None):
                self.submissions.append(self.snapshot(canvas))
                return super().swap(canvas)

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

        for playback, duration, expected_indices in (
            ("hold", 1.1, [0, 1, 2, 2, 2]),
            ("loop", 1.85, [0, 1, 2, 0, 1, 2, 0, 1]),
            ("ping_pong", 2.1, [0, 1, 2, 1, 0, 1, 2, 1, 0]),
        ):
            with self.subTest(playback=playback):
                module = {
                    "id": "custom_canvas_timeline", "name": "Canvas timeline",
                    "type": "canvas", "background": "#000000", "frame_count": 3,
                    "fps": 4, "playback": playback, "layers": [layer, keyframed],
                    "overrides": [{
                        "frame": 1, "layer_id": "layer_keyframed",
                        "properties": {"y": 3},
                    }],
                }
                candidate = copy.deepcopy(DEFAULT_CONFIG)
                candidate["custom_modules"] = [copy.deepcopy(module)]
                candidate["playlist"] = [{
                    "module": module["id"], "duration": duration, "enabled": True,
                }]
                validate_config(candidate, enforce_save_bounds=True)
                module = candidate["custom_modules"][0]

                physical = RecordingMatrix()
                stop = AdvancingStop()
                with patch(
                    "modules.custom_text.time.monotonic", side_effect=lambda: stop.elapsed,
                ):
                    self.assertTrue(custom_text.run(
                        physical, candidate, duration=duration, stop_event=stop,
                        module_config=module,
                    ))

                preview = RecordingMatrix()
                session = PreviewSession(edit_preview.advanced_canvas_preview_frames(
                    preview, module, duration,
                ))
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
                expected_delays = [250] * (len(expected_indices) - 1) + [
                    round((duration - 0.25 * (len(expected_indices) - 1)) * 1000, 9)
                ]
                self.assertEqual([frame["delay_ms"] for frame in frames], expected_delays)
                self.assertEqual(
                    [round(delay * 1000, 9) for delay in stop.waits], expected_delays,
                )
                self.assertAlmostEqual(sum(stop.waits), duration)
                self.assertEqual(
                    [image.tobytes() for image in preview_images],
                    [image.tobytes() for image in physical.submissions],
                )
                signatures = [preview_images[index].tobytes() for index in range(3)]
                self.assertEqual(
                    [signatures.index(image.tobytes()) for image in preview_images],
                    expected_indices,
                )
                self.assertTrue(all(not page["complete"] for page in pages[:-1]))
                self.assertTrue(pages[-1]["complete"])
                self.assertIsNone(pages[-1]["next_token"])
                self.assertEqual(len(physical.decode_calls), 1)
                self.assertEqual(len(preview.decode_calls), 1)
                self.assertEqual(physical.decode_calls[0][:3], ("timeline.gif", 3, 4))
                with self.assertRaises(StopIteration):
                    session.page()

        self.assertEqual(custom_text.run.__defaults__, (15, None, None))

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

        with patch("modules.media_player.time.monotonic", return_value=0.0):
            rendered = play_media(
                matrix, filename, 1.25, stop_event=stop_event, settings=settings,
            )

        self.assertEqual(expected, [(filename, 1250.0, settings)])
        self.assertTrue(rendered)
        self.assertEqual(matrix.shown, [(filename, None, settings)])
        self.assertEqual(stop_event.waits, [expected[0][1] / 1000])

    def test_weather_variants_preserve_direction_loop_count_and_forecast_cards(self):
        from modules.openweather_common import (
            OpenWeatherForecastPeriod, OpenWeatherSnapshot,
        )
        from modules.openweather_forecast import PreparedForecast
        from modules.openweather_overview import PreparedOverview
        from modules.weather_app import WeatherSnapshot
        from modules import openweather_forecast, openweather_overview, weather_app

        class RecordingMatrix(CaptureMatrix):
            def __init__(self):
                super().__init__()
                self.positions = []
                self.labels = []
                self.measurements = []
                self.submissions = []

            def colour(self, name):
                colours = {
                    "retro_soft_grey": "#dcd5d5", "black": "#000000",
                    "vapor_pink": "#ff71ce", "vapor_cyan": "#01cdfe",
                    "neon_yellow": "#ffb300", "retro_aqua": "#00ffff",
                }
                return self.colour_rgb(colours[name])

            def draw_text_with_border(
                self, font, x, y, fill, border, text, width=1, canvas=None,
            ):
                for dx in range(-width, width + 1):
                    for dy in range(-width, width + 1):
                        if dx or dy:
                            self.draw_text(font, x + dx, y + dy, border, text, canvas=canvas)
                self.labels.append(text)
                return self.draw_text(font, x, y, fill, text, canvas=canvas)

            def draw_text(self, font, x, y, colour, text, canvas=None, rotation=0):
                result = super().draw_text(font, x, y, colour, text, canvas=canvas, rotation=rotation)
                if x == -10000:
                    self.measurements.append(result - x)
                return result

            def widget_scroll(
                self, text, font, x, y, colour, canvas=None, step=1, wrap=True,
            ):
                self.positions.append(x)
                text_end = self.draw_text(font, x, y, colour, text, canvas=canvas)
                return x - step, text_end - x

            def swap(self, canvas=None):
                self.submissions.append(self.snapshot(canvas))
                return super().swap(canvas)

        class RecordingStop:
            def __init__(self):
                self.waits = []

            def is_set(self):
                return False

            def wait(self, delay):
                self.waits.append(delay)
                return False

        weather = WeatherSnapshot(
            location="Hove", status="Clear", temperature=16, feels_like=15,
            high=18, low=12, humidity=70,
        )
        current = OpenWeatherSnapshot(
            location="Hove", status="Cloudy", temperature=17, feels_like=16,
            high=19, low=13, humidity=68, icon_code="04d",
        )
        periods = tuple(
            OpenWeatherForecastPeriod(
                label=f"Tue {hour}:00", status=status, temperature=temperature,
                feels_like=temperature - 1, high=temperature + 2,
                low=temperature - 3, icon_code=icon_code,
            )
            for hour, status, temperature, icon_code in (
                ("15", "Rain", 18, "10d"),
                ("18", "Clear", 16, "01n"),
            )
        )
        icon = Image.new("RGB", (20, 20), "blue")

        class PreparedWeatherClient:
            def fetch(self, *_args, **_kwargs):
                return weather

        cases = (
            (
                "weather", weather_app.run, edit_preview.weather_preview_frames,
                {"weather": {"scroll_direction": "right", "scroll_speed": 0.075,
                             "scroll_loops": 2}}, weather,
                {"client": PreparedWeatherClient()},
            ),
            (
                "overview", openweather_overview.run,
                edit_preview.openweather_overview_preview_frames,
                {"weather": {"location": "Hove"},
                 "openweather_overview": {"scroll_direction": "left",
                     "scroll_speed": 0.05, "scroll_loops": 2}},
                PreparedOverview(weather=current, icon=icon), {"prepared": PreparedOverview(weather=current, icon=icon)},
            ),
            (
                "forecast", openweather_forecast.run,
                edit_preview.openweather_forecast_preview_frames,
                {"weather": {"location": "Hove"},
                 "openweather_forecast": {"scroll_direction": "right",
                     "scroll_speed": 0.025, "scroll_loops": 2}},
                PreparedForecast(
                    periods=periods,
                    icons={period.icon_code: icon for period in periods},
                ),
                {"prepared": PreparedForecast(
                    periods=periods,
                    icons={period.icon_code: icon for period in periods},
                )},
            ),
        )

        for name, physical_run, preview_factory, config, prepared, physical_options in cases:
            with self.subTest(variant=name):
                physical = RecordingMatrix()
                stop = RecordingStop()
                self.assertTrue(physical_run(
                    physical, config, duration=999, stop_event=stop,
                    **physical_options,
                ))

                preview = RecordingMatrix()
                session = PreviewSession(preview_factory(preview, config, prepared))
                session.MAX_FRAMES = 7
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
                direction = config[
                    "weather" if name == "weather" else f"openweather_{name}"
                ]["scroll_direction"]
                loops = config[
                    "weather" if name == "weather" else f"openweather_{name}"
                ]["scroll_loops"]
                expected_positions = []
                for text_width in preview.measurements:
                    one_loop = (
                        list(range(-text_width, preview.width + 1))
                        if direction == "right"
                        else list(range(preview.width, -text_width - 1, -1))
                    )
                    expected_positions.extend(one_loop * loops)

                self.assertEqual(preview.positions, expected_positions)
                self.assertEqual(physical.positions, expected_positions)
                self.assertEqual(len(frames), len(expected_positions))
                self.assertEqual(
                    [image.tobytes() for image in preview_images],
                    [image.tobytes() for image in physical.submissions],
                )
                self.assertEqual(
                    [frame["delay_ms"] for frame in frames],
                    [delay * 1000 for delay in stop.waits],
                )
                self.assertTrue(pages[-1]["complete"])
                self.assertIsNone(pages[-1]["next_token"])
                self.assertTrue(all(not page["complete"] for page in pages[:-1]))
                if name == "forecast":
                    rendered_labels = [
                        label for label in physical.labels if label.startswith("Tue ")
                    ]
                    expected_labels = []
                    for period, text_width in zip(periods, preview.measurements):
                        card_frames = (preview.width + text_width + 1) * loops
                        expected_labels.extend([period.label] * card_frames)
                    self.assertEqual(rendered_labels, expected_labels)

        self.assertEqual(weather_app.run.__defaults__, (20, None, None))
        self.assertEqual(openweather_overview.run.__defaults__, (20, None, None, None, None))
        self.assertEqual(openweather_forecast.run.__defaults__, (30, None, None, None, None))

    def test_static_hold_delay_is_clipped_to_invocation_deadline(self):
        class Clock:
            def __init__(self):
                self.elapsed = 0.0

            def monotonic(self):
                return self.elapsed

        class PhysicalCaptureMatrix(CaptureMatrix):
            def __init__(self, path, clock):
                super().__init__()
                self.path = path
                self.clock = clock
                self.submissions = []

            def show_image(self, filename, delay=None, **geometry):
                self.clock.elapsed += 0.03
                with Image.open(self.path) as image:
                    self.clear()
                    self.draw_image(image, **geometry)
                self.submissions.append(self.snapshot())
                return True

        class AdvancingStop:
            def __init__(self, clock):
                self.clock = clock
                self.waits = []

            def wait(self, delay):
                self.waits.append(delay)
                self.clock.elapsed += delay
                return False

        settings = {
            "x": 0, "y": 0, "width": 64, "height": 32,
            "keep_aspect": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "still.png"
            Image.new("RGB", (64, 32), "purple").save(path)

            physical_clock = Clock()
            physical = PhysicalCaptureMatrix(path, physical_clock)
            stop = AdvancingStop(physical_clock)
            with patch(
                "modules.media_player.time.monotonic", side_effect=physical_clock.monotonic,
            ):
                self.assertTrue(play_media(
                    physical, str(path), 0.05, stop_event=stop, settings=settings,
                ))

            preview_clock = Clock()
            preview = PhysicalCaptureMatrix(path, preview_clock)
            frames = list(static_media_frames(
                str(path), 0.05, settings,
                render=lambda filename, geometry: preview.show_image(
                    filename, delay=None, **geometry,
                ),
                monotonic=preview_clock.monotonic,
            ))

            self.assertEqual(len(frames), 1)
            self.assertAlmostEqual(frames[0][1], 20.0)
            self.assertEqual(stop.waits, [frames[0][1] / 1000])
            self.assertAlmostEqual(sum(stop.waits), 0.02)
            self.assertEqual(
                physical.submissions[0].tobytes(), preview.submissions[0].tobytes(),
            )

            exhausted = PhysicalCaptureMatrix(path, Clock())
            self.assertEqual(
                list(static_media_frames(
                    str(path), 0, settings,
                    render=lambda filename, geometry: exhausted.show_image(
                        filename, delay=None, **geometry,
                    ),
                )),
                [],
            )
            self.assertEqual(exhausted.submissions, [])

    def test_announcement_static_scroll_transition_and_final_delay_match_physical_invocation(self):
        import modules.announcement_board as announcement_board

        frame_capture_patch = patch.object(frame_capture_module, "Image", Image)
        frame_capture_patch.start()
        self.addCleanup(frame_capture_patch.stop)

        class RecordingCaptureMatrix(CaptureMatrix):
            def __init__(self):
                super().__init__()
                self.submissions = []

            def swap(self, canvas=None):
                self.submissions.append(self.snapshot(canvas))
                return super().swap(canvas)

        class AdvancingStop:
            def __init__(self):
                self.elapsed = 0.0
                self.waits = []
                self.cancelled = False

            def is_set(self):
                return self.cancelled

            def wait(self, delay):
                self.waits.append(delay)
                self.elapsed += delay
                return self.cancelled

        def item(item_id, headline, timing, transition):
            return {
                "id": item_id,
                "enabled": True,
                "name": headline,
                "layout": "text_notice",
                "headline": headline,
                "message": "",
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
                "timing": timing,
                "transition": transition,
            }

        announcements = [
            item(
                "announcement_static", "HELD",
                {"mode": "static", "duration_seconds": 1}, {"type": "cut"},
            ),
            item(
                "announcement_scroll", "A",
                {"mode": "scroll", "loops": 2, "frame_delay": 0.01},
                {"type": "cut"},
            ),
            item(
                "announcement_fade", "FINAL",
                {"mode": "static", "duration_seconds": 1},
                {"type": "fade", "duration_seconds": 0.4},
            ),
        ]
        validated = copy.deepcopy(DEFAULT_CONFIG)
        validated["announcement_board"]["announcements"] = announcements
        validate_config(validated, enforce_save_bounds=True)
        announcements = validated["announcement_board"]["announcements"]

        physical = RecordingCaptureMatrix()
        stop = AdvancingStop()
        now = datetime.datetime(2030, 1, 15, 12, 0)
        with patch("modules.announcement_board.time.monotonic", side_effect=lambda: stop.elapsed):
            self.assertTrue(announcement_board.run(
                physical,
                {"announcement_board": {"announcements": announcements}},
                duration=3,
                stop_event=stop,
                now_provider=lambda: now,
            ))

        preview = CaptureMatrix()
        frames = list(edit_preview.announcement_preview_frames(
            preview, announcements, duration=3,
        ))
        preview_images = [
            Image.open(io.BytesIO(base64.b64decode(frame["png"]))).copy()
            for frame in frames
        ]
        physical_bytes = [image.tobytes() for image in physical.submissions]
        preview_bytes = [image.tobytes() for image in preview_images]
        preview_delays = [frame["delay_ms"] for frame in frames]
        physical_delays = [delay * 1000 for delay in stop.waits]

        font = BdfFont(Path(__file__).parents[1] / "resources" / "fonts" / "5x7.bdf")
        text_width = font.text_pixels(0, 20, "A")[0]
        scroll_frames_per_loop = 64 + text_width + 1
        transition_start = 1 + scroll_frames_per_loop * 2
        self.assertEqual(len(frames), transition_start + 5)
        self.assertEqual(preview_bytes, physical_bytes)
        self.assertEqual(preview_delays, physical_delays)
        self.assertEqual(preview_delays[:1], [1000])
        self.assertEqual(
            preview_delays[1:transition_start], [10] * (scroll_frames_per_loop * 2),
        )
        self.assertEqual(
            [round(delay, 9) for delay in preview_delays[transition_start:]],
            [100, 100, 100, 100, 200],
        )
        self.assertAlmostEqual(sum(preview_delays), 3000)
        self.assertEqual(
            preview_bytes[1], preview_bytes[1 + scroll_frames_per_loop],
            "each saved leftward traversal must reset to the exact first position",
        )
        self.assertEqual(
            preview_bytes[-1], preview_bytes[-2],
            "the terminal page must be the one explicit hold submission",
        )
        self.assertEqual(len(physical.submissions), 146)

        cancelled = RecordingCaptureMatrix()
        cancelled_stop = AdvancingStop()
        cancelled_stop.cancelled = True
        self.assertFalse(announcement_board.run(
            cancelled,
            {"announcement_board": {"announcements": announcements}},
            duration=3,
            stop_event=cancelled_stop,
            now_provider=lambda: now,
        ))
        self.assertEqual(cancelled.submissions, [])
        self.assertEqual(announcement_board.run_preview.__defaults__, (None,))

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
