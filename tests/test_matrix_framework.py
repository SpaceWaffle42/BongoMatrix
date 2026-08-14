import importlib
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

fake_graphics = types.SimpleNamespace(Color=lambda *rgb: types.SimpleNamespace(red=rgb[0], green=rgb[1], blue=rgb[2]), Font=Mock)
fake_rgb = types.SimpleNamespace(RGBMatrix=Mock, RGBMatrixOptions=Mock, graphics=fake_graphics)
fake_image = Mock()
fake_image.Resampling.LANCZOS = 1
fake_pil = types.SimpleNamespace(Image=fake_image, ImageDraw=Mock(), ImageSequence=Mock())
previous_modules = {name: sys.modules.get(name) for name in ("rgbmatrix", "PIL")}
sys.modules["rgbmatrix"] = fake_rgb
sys.modules["PIL"] = fake_pil
previous_framework = sys.modules.pop("matrix_framework", None)
framework = importlib.import_module("matrix_framework")
if previous_framework is None:
    sys.modules.pop("matrix_framework", None)
else:
    sys.modules["matrix_framework"] = previous_framework
for name, previous in previous_modules.items():
    if previous is None:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = previous
from PIL import Image as PillowImage, ImageSequence as PillowImageSequence
Matrix = framework.Matrix


class MatrixFrameworkTests(unittest.TestCase):
    def test_colour_rgb_accepts_html_hex_values(self):
        with patch.object(
            framework.graphics, "Color",
            side_effect=lambda red, green, blue: (red, green, blue),
        ):
            self.assertEqual(Matrix.colour_rgb("#1aB2ff"), (26, 178, 255))
        with self.assertRaises(ValueError):
            Matrix.colour_rgb("red")

    def test_set_brightness_clamps_and_updates_driver(self):
        matrix = Matrix.__new__(Matrix)
        matrix.matrix = Mock()
        matrix.set_brightness(120)
        self.assertEqual(matrix.matrix.brightness, 100)
        matrix.set_brightness(0)
        self.assertEqual(matrix.matrix.brightness, 1)

    def test_draw_text_rotates_glyph_pixels_around_text_centre(self):
        matrix = Matrix.__new__(Matrix)
        canvas = Mock()
        font = Mock()
        capture_font = Mock()
        capture_font.text_pixels.return_value = (3, [(10, 10), (12, 10)])
        matrix._capture_fonts = {id(font): capture_font}
        matrix.pixel = Mock()

        advance = matrix.draw_text(
            font, 10, 10, types.SimpleNamespace(red=255, green=0, blue=0),
            "-", rotation=90, canvas=canvas,
        )

        self.assertEqual(advance, 3)
        self.assertEqual(
            matrix.pixel.call_args_list,
            [
                unittest.mock.call(11, 9, 255, 0, 0, canvas=canvas),
                unittest.mock.call(11, 11, 255, 0, 0, canvas=canvas),
            ],
        )

    def test_draw_image_resizes_rotates_and_mirrors_the_same_canvas(self):
        matrix = Matrix.__new__(Matrix)
        matrix._frame_capture = Mock()
        canvas = Mock()
        source = Mock()
        prepared = Mock()
        rotated = Mock()
        matrix._prepare_image = Mock(return_value=prepared)
        prepared.rotate.return_value = rotated

        matrix.draw_image(
            source, 4, 5, width=20, height=10, keep_aspect=False,
            rotation=30, canvas=canvas,
        )

        matrix._prepare_image.assert_called_once_with(source, width=20, height=10, keep_aspect=False)
        prepared.rotate.assert_called_once_with(-30, expand=False)
        canvas.SetImage.assert_called_once_with(rotated, 4, 5)
        matrix._frame_capture.set_image.assert_called_once_with(canvas, rotated, 4, 5)

    def test_media_frames_cancellation_interrupts_blocked_image_decoder(self):
        matrix = Matrix.__new__(Matrix)
        matrix.matrix = Mock(width=64, height=32)
        matrix._media_file = Mock(return_value=framework.pathlib.Path("/tmp/animation.gif"))
        stop = threading.Event()
        decoder_started = threading.Event()
        release_pillow = threading.Event()
        finished = threading.Event()
        result = []
        read_fd, write_fd = os.pipe()
        process = Mock()
        process.stdout = os.fdopen(read_fd, "rb", buffering=0)
        process.poll.return_value = None
        process.wait.return_value = 0
        media = MagicMock()

        def blocked_image_open(*_args, **_kwargs):
            decoder_started.set()
            release_pillow.wait(1)
            return media

        def launch_decoder(*_args, **_kwargs):
            decoder_started.set()
            return process

        def decode():
            try:
                result.extend(
                    matrix.media_frames(
                        "animation.gif", 1, 1, stop_event=stop, decode_timeout=0.05,
                    )
                )
            finally:
                finished.set()

        with patch.object(
            framework.Image, "open", side_effect=blocked_image_open,
        ) as pillow_open, patch.object(
            framework.ImageSequence, "Iterator", return_value=[]
        ), patch.object(framework.os, "open", return_value=9), patch.object(
            framework.os, "close"
        ), patch.object(
            framework.subprocess, "Popen", side_effect=launch_decoder
        ):
            worker = threading.Thread(target=decode)
            worker.start()
            self.assertTrue(decoder_started.wait(0.5))
            stop.set()
            stopped_promptly = finished.wait(0.3)
            release_pillow.set()
            os.close(write_fd)
            worker.join(1)

        process.stdout.close()
        self.assertTrue(stopped_promptly, "cancellation did not interrupt image decoding")
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [])
        pillow_open.assert_not_called()
        process.kill.assert_called_once_with()

    def test_media_frames_observes_cancellation_during_image_decode(self):
        matrix = Matrix.__new__(Matrix)
        matrix.matrix = Mock(width=64, height=32)
        matrix._media_file = Mock(return_value=framework.pathlib.Path("/tmp/animation.gif"))
        stop = threading.Event()
        stop.set()
        media = Mock()

        with patch.object(framework.Image, "open", return_value=media) as image_open, patch.object(
            framework.ImageSequence, "Iterator", return_value=[Mock(), Mock()]
        ) as iterator:
            self.assertEqual(
                matrix.media_frames("animation.gif", 1, 1, stop_event=stop),
                [],
            )
        image_open.assert_not_called()
        iterator.assert_not_called()

    def test_media_frames_repeats_clean_short_decoder_timeline(self):
        matrix = Matrix.__new__(Matrix)
        matrix.matrix = Mock(width=64, height=32)
        matrix._media_file = Mock(return_value=framework.pathlib.Path("/tmp/still.png"))
        raw = b"\x10\x20\x30" * (64 * 32)
        image = Mock()
        process = Mock()
        process.stdout = Mock()
        process.poll.return_value = 0
        process.wait.return_value = 0

        with patch.object(framework.os, "open", return_value=9), patch.object(
            framework.os, "close"
        ), patch.object(framework.subprocess, "Popen", return_value=process) as popen, patch.object(
            framework, "_read_exact_interruptibly", side_effect=[raw, b""]
        ), patch.object(
            framework.Image, "frombytes", return_value=image, create=True
        ) as frombytes:
            frames = matrix.media_frames("still.png", 3, 2)

        self.assertEqual(frames, [image, image, image])
        frombytes.assert_called_once_with("RGB", (64, 32), raw)
        video_filter = popen.call_args.args[0][popen.call_args.args[0].index("-vf") + 1]
        self.assertIn("fps=2:eof_action=pass", video_filter)

    def test_media_frames_can_preserve_source_dimensions_for_canvas_layers(self):
        matrix = Matrix.__new__(Matrix)
        matrix.matrix = Mock(width=64, height=32)
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o755)
            path = Path(directory) / "square.png"
            PillowImage.new("RGB", (10, 10), (255, 0, 0)).save(path)
            os.chmod(path, 0o644)
            matrix._media_file = Mock(return_value=path)

            with patch.object(
                framework.Image, "frombytes", side_effect=PillowImage.frombytes,
            ):
                frames = matrix.media_frames(
                    "square.png", 2, 10, preserve_size=True, decode_timeout=5,
                )

        self.assertEqual([frame.size for frame in frames], [(10, 10), (10, 10)])
        self.assertEqual(frames[0].getbbox(), (0, 0, 10, 10))

    def test_media_frames_bounds_post_kill_reaping(self):
        matrix = Matrix.__new__(Matrix)
        matrix.matrix = Mock(width=64, height=32)
        matrix._media_file = Mock(return_value=framework.pathlib.Path("/tmp/animation.gif"))
        stop = threading.Event()
        process = Mock()
        process.stdout = Mock()
        process.poll.return_value = None
        process.wait.return_value = 0

        def cancel_during_read(*_args, **_kwargs):
            stop.set()
            return b""

        with patch.object(framework.os, "open", return_value=9), patch.object(
            framework.os, "close"
        ), patch.object(framework.subprocess, "Popen", return_value=process), patch.object(
            framework, "_read_exact_interruptibly", side_effect=cancel_during_read,
        ):
            self.assertEqual(
                matrix.media_frames("animation.gif", 1, 1, stop_event=stop),
                [],
            )

        process.kill.assert_called_once_with()
        self.assertIn("timeout", process.wait.call_args.kwargs)
        self.assertLessEqual(process.wait.call_args.kwargs["timeout"], 0.25)

    def test_media_frames_defers_reaping_immediately_after_decode_deadline(self):
        matrix = Matrix.__new__(Matrix)
        matrix.matrix = Mock(width=64, height=32)
        matrix._media_file = Mock(return_value=framework.pathlib.Path("/tmp/animation.gif"))
        stop = threading.Event()
        process = Mock()
        process.stdout = Mock()
        process.poll.return_value = None

        def cancel_during_read(*_args, **_kwargs):
            stop.set()
            return b""

        reaper = Mock()
        with patch.object(framework.time, "monotonic", side_effect=[0, 46]), patch.object(
            framework.os, "open", return_value=9,
        ), patch.object(framework.os, "close"), patch.object(
            framework.subprocess, "Popen", return_value=process,
        ), patch.object(
            framework, "_read_exact_interruptibly", side_effect=cancel_during_read,
        ), patch.object(framework.threading, "Thread", return_value=reaper) as thread:
            self.assertEqual(
                matrix.media_frames("animation.gif", 1, 1, stop_event=stop),
                [],
            )

        process.kill.assert_called_once_with()
        process.wait.assert_not_called()
        self.assertTrue(thread.call_args.kwargs["daemon"])
        reaper.start.assert_called_once_with()

    def test_canvas_many_source_deadline_includes_decoder_cleanup(self):
        from config_store import _validate_canvas_module
        from modules import custom_text

        matrix = Matrix.__new__(Matrix)
        matrix.matrix = Mock(width=64, height=32)
        matrix.matrix.CreateFrameCanvas.return_value = "canvas"
        matrix.fill = Mock()
        matrix.colour_rgb = Mock(return_value=Mock())
        matrix.draw_image = Mock()
        matrix.draw_line = Mock()
        matrix.swap = Mock(return_value="canvas")
        clock = [0.0]
        current_source = [None]
        attempted_sources = []
        processes = []

        media_layers = []
        for index in range(62):
            source = f"{index:03d}-broken.gif"
            media_layers.append({
                "id": f"layer_media_{index}", "name": f"Media {index}",
                "type": "media", "visible": True,
                "properties": {
                    "x": 0, "y": 0, "width": 8, "height": 8,
                    "rotation": 0, "source": source, "keep_aspect": True,
                },
                "keyframes": [],
            })
        media_layers.append({
            "id": "layer_media_healthy", "name": "Healthy media",
            "type": "media", "visible": True,
            "properties": {
                "x": 8, "y": 0, "width": 8, "height": 8,
                "rotation": 0, "source": "zzz-healthy.png", "keep_aspect": True,
            },
            "keyframes": [],
        })
        primitive = {
            "id": "layer_primitive", "name": "Primitive", "type": "rectangle",
            "visible": True,
            "properties": {
                "x": 1, "y": 1, "width": 6, "height": 5,
                "rotation": 0, "colour": "#ffffff", "filled": False,
            },
            "keyframes": [],
        }
        options = {
            "id": "custom_many_media", "name": "Many media", "type": "canvas",
            "background": "#000000", "frame_count": 1, "fps": 1,
            "playback": "hold", "layers": [*media_layers, primitive],
            "overrides": [],
        }
        _validate_canvas_module(options)

        def media_path(source):
            current_source[0] = source
            return framework.pathlib.Path("/tmp/source.gif")

        def launch_decoder(*_args, **_kwargs):
            source = current_source[0]
            attempted_sources.append(source)
            process = Mock()
            process.stdout = Mock(source=source)
            process.poll.return_value = 0 if source == "zzz-healthy.png" else None

            def wait(timeout=None):
                if source == "zzz-healthy.png":
                    return 0
                assert timeout is not None
                clock[0] += timeout
                raise framework.subprocess.TimeoutExpired("ffmpeg", timeout)

            process.wait.side_effect = wait
            processes.append(process)
            return process

        def read_frame(stream, *, deadline, **_kwargs):
            if stream.source == "zzz-healthy.png":
                return "healthy-frame"
            clock[0] = deadline
            return None

        matrix._media_file = Mock(side_effect=media_path)
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        reaper = Mock()
        with patch.object(framework.time, "monotonic", side_effect=lambda: clock[0]), patch.object(
            framework.os, "open", return_value=9,
        ), patch.object(framework.os, "close"), patch.object(
            framework.subprocess, "Popen", side_effect=launch_decoder,
        ), patch.object(
            framework, "_read_ppm_frame_interruptibly", side_effect=read_frame,
        ), patch.object(framework.threading, "Thread", return_value=reaper):
            result = custom_text.run(
                matrix, {}, duration=1, stop_event=stop, module_config=options,
            )

        self.assertTrue(result)
        self.assertLessEqual(clock[0], custom_text.CANVAS_MEDIA_PREPARE_TIMEOUT)
        self.assertEqual(attempted_sources, sorted(attempted_sources))
        self.assertEqual(len(attempted_sources), 63)
        self.assertEqual(attempted_sources[-1], "zzz-healthy.png")
        matrix.draw_image.assert_called_once_with(
            "healthy-frame", 8, 0, width=8, height=8, keep_aspect=True,
            rotation=0, canvas="canvas",
        )
        self.assertGreaterEqual(matrix.draw_line.call_count, 4)
        matrix.swap.assert_called_once_with("canvas")
        self.assertTrue(all(
            process.wait.call_count == 0
            for process in processes[:-1]
        ))

    def test_media_frames_stdout_close_failure_still_reaps_and_closes_input(self):
        matrix = Matrix.__new__(Matrix)
        matrix.matrix = Mock(width=64, height=32)
        matrix._media_file = Mock(return_value=framework.pathlib.Path("/tmp/asset.gif"))
        process = Mock()
        process.stdout.close.side_effect = OSError("pipe close failed")
        process.poll.return_value = None
        process.wait.return_value = 0

        with patch.object(framework.os, "open", return_value=19), patch.object(
            framework.os, "close",
        ) as close_fd, patch.object(
            framework.subprocess, "Popen", return_value=process,
        ), patch.object(
            framework, "_read_ppm_frame_interruptibly", return_value="frame",
        ):
            with self.assertRaisesRegex(OSError, "pipe close failed"):
                matrix.media_frames(
                    "asset.gif", frame_count=1, fps=10,
                    preserve_size=True, decode_timeout=1,
                )

        process.kill.assert_called_once_with()
        self.assertGreaterEqual(process.wait.call_count, 2)
        close_fd.assert_called_once_with(19)

    def test_media_frames_cancels_while_waiting_for_decoder_completion(self):
        matrix = Matrix.__new__(Matrix)
        matrix.matrix = Mock(width=64, height=32)
        matrix._media_file = Mock(return_value=framework.pathlib.Path("/tmp/animation.gif"))
        stop = threading.Event()
        completion_started = threading.Event()
        finished = threading.Event()
        process = Mock()
        process.stdout = Mock()
        process.poll.return_value = None
        raw = b"\x10\x20\x30" * (64 * 32)
        result, errors = [], []

        def stalled_wait(timeout=None):
            if timeout == 0.25:
                return 0
            assert timeout is not None
            completion_started.set()
            time.sleep(min(timeout, 1))
            raise framework.subprocess.TimeoutExpired("ffmpeg", timeout)

        process.wait.side_effect = stalled_wait

        def decode():
            try:
                result.extend(matrix.media_frames("animation.gif", 1, 1, stop_event=stop))
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        with patch.object(framework.os, "open", return_value=9), patch.object(
            framework.os, "close",
        ), patch.object(
            framework.subprocess, "Popen", return_value=process,
        ), patch.object(framework, "_read_exact_interruptibly", return_value=raw):
            worker = threading.Thread(target=decode)
            worker.start()
            self.assertTrue(completion_started.wait(0.5))
            stop.set()
            stopped_promptly = finished.wait(0.3)
            worker.join(1.2)

        self.assertTrue(stopped_promptly, "completion wait ignored cancellation")
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result, [])
        process.kill.assert_called_once_with()

    def test_media_frames_preserves_tall_and_wide_source_geometry(self):
        matrix = Matrix.__new__(Matrix)
        matrix.matrix = Mock(width=64, height=32)
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o755)
            for name, size in (("tall.png", (10, 20)), ("wide.png", (20, 10))):
                with self.subTest(size=size):
                    path = Path(directory) / name
                    PillowImage.new("RGB", size, (255, 0, 0)).save(path)
                    os.chmod(path, 0o644)
                    matrix._media_file = Mock(return_value=path)
                    with patch.object(
                        framework.Image, "frombytes", side_effect=PillowImage.frombytes,
                    ):
                        frames = matrix.media_frames(
                            name, 1, 10, preserve_size=True, decode_timeout=5,
                        )
                    self.assertEqual(frames[0].size, size)

    def test_media_frames_resolved_path_is_regular_and_bypasses_global_lookup(self):
        matrix = Matrix.__new__(Matrix)
        matrix.matrix = Mock(width=64, height=32)
        matrix._media_file = Mock(side_effect=AssertionError("global lookup used"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.png"
            PillowImage.new("RGB", (10, 10), "red").save(path)
            os.chmod(directory, 0o755)
            os.chmod(path, 0o644)
            with patch.object(
                framework.Image, "frombytes", side_effect=PillowImage.frombytes,
            ):
                frames = matrix.media_frames(
                    "logical.png", 1, 1, preserve_size=True,
                    resolved_path=path,
                )
            self.assertEqual(frames[0].size, (10, 10))
            self.assertEqual(
                matrix.media_frames("logical.png", 1, 1, resolved_path=path.parent),
                [],
            )
        matrix._media_file.assert_not_called()

    def test_media_frames_deadline_interrupts_blocked_decoder(self):
        matrix = Matrix.__new__(Matrix)
        matrix.matrix = Mock(width=64, height=32)
        matrix._media_file = Mock(return_value=framework.pathlib.Path("/tmp/animation.gif"))
        read_fd, write_fd = os.pipe()
        process = Mock()
        process.stdout = os.fdopen(read_fd, "rb", buffering=0)
        process.poll.return_value = None
        process.wait.return_value = 0
        started = time.monotonic()
        try:
            with patch.object(framework.os, "open", return_value=9), patch.object(
                framework.os, "close"
            ), patch.object(framework.subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(ValueError, "media decoder did not finish in time"):
                    matrix.media_frames("animation.gif", 1, 1, decode_timeout=0.05)
        finally:
            os.close(write_fd)
            process.stdout.close()

        self.assertLess(time.monotonic() - started, 0.3)
        process.kill.assert_called_once_with()

    def test_media_frames_decodes_mp4_to_bounded_canvas_timeline(self):
        matrix = Matrix.__new__(Matrix)
        matrix.matrix = Mock(width=64, height=32)
        matrix._media_file = Mock(return_value=framework.pathlib.Path("/tmp/clip.mp4"))
        raw = b"\x10\x20\x30" * (64 * 32)
        process = Mock()
        process.stdout.read.side_effect = [raw, raw, raw]
        process.wait.return_value = 0
        images = [Mock(), Mock(), Mock()]

        with patch.object(framework.os, "open", return_value=9), patch.object(
            framework.os, "close"
        ), patch.object(framework.subprocess, "Popen", return_value=process) as popen, patch.object(
            framework, "_read_exact_interruptibly", return_value=raw
        ) as reader, patch.object(
            framework.Image, "frombytes", side_effect=images, create=True
        ):
            frames = matrix.media_frames("clip.mp4", frame_count=3, fps=12)

        self.assertEqual(frames, images)
        self.assertEqual(reader.call_count, 3)
        self.assertTrue(all(call.kwargs["stop_event"] is None for call in reader.call_args_list))
        command = popen.call_args.args[0]
        self.assertNotIn("-stream_loop", command)
        video_filter = command[command.index("-vf") + 1]
        self.assertIn("scale=64:32", video_filter)
        self.assertIn("fps=12", video_filter)
        self.assertIn("pipe:0", command)
        self.assertEqual(popen.call_args.kwargs["stdin"], 9)
        self.assertIs(popen.call_args.kwargs["stderr"], framework.subprocess.DEVNULL)
        self.assertNotIn("pass_fds", popen.call_args.kwargs)

    def test_show_gif_can_be_interrupted(self):
        matrix = Matrix.__new__(Matrix)
        matrix.canvas = Mock()
        matrix.matrix = Mock(width=64, height=32)
        matrix.clear = Mock()
        matrix.swap = Mock(return_value=matrix.canvas)
        frame = Mock(info={"duration": 100})
        frame.copy.return_value = frame
        matrix._prepare_image = Mock(return_value=frame)
        stop = threading.Event()
        stop.set()
        path = Mock()
        matrix._media_file = Mock(return_value=path)
        with patch.object(framework.Image, "open", return_value=Mock()), patch.object(framework.ImageSequence, "Iterator", return_value=[frame]):
            self.assertTrue(matrix.show_gif("sleep.gif", loops=0, stop_event=stop, max_duration=10))
        matrix.canvas.SetImage.assert_not_called()

    def test_show_gif_playback_rate_scales_embedded_frame_duration(self):
        matrix = Matrix.__new__(Matrix)
        matrix.canvas = Mock()
        matrix.matrix = Mock(width=64, height=32)
        matrix._frame_capture = Mock()
        matrix.clear = Mock()
        matrix.swap = Mock(return_value=matrix.canvas)
        frame = Mock(info={"duration": 200})
        frame.copy.return_value = frame
        matrix._prepare_image = Mock(return_value=frame)
        matrix._media_file = Mock(return_value=Mock())

        for rate, expected_delay in ((0.5, 0.4), (1.0, 0.2), (2.0, 0.1)):
            matrix.canvas.SetImage.reset_mock()
            matrix._frame_capture.set_image.reset_mock()
            matrix.swap.reset_mock()
            with self.subTest(rate=rate), patch.object(
                framework.Image, "open", return_value=Mock()
            ), patch.object(
                framework.ImageSequence, "Iterator", return_value=[frame]
            ), patch.object(framework.time, "sleep") as sleep:
                self.assertTrue(matrix.show_gif("sleep.gif", loops=1, playback_rate=rate))
                matrix.canvas.SetImage.assert_called_once()
                matrix._frame_capture.set_image.assert_called_once()
                matrix.swap.assert_called_once_with()
                sleep.assert_called_once_with(expected_delay)

    def test_show_gif_rejects_invalid_playback_rates_before_opening_media(self):
        matrix = Matrix.__new__(Matrix)
        matrix._media_file = Mock(side_effect=AssertionError("media lookup must not run"))

        for rate in (False, 0, -1, 0.24, 4.01, float("nan"), float("inf"), "1"):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                matrix.show_gif("sleep.gif", playback_rate=rate)

        matrix._media_file.assert_not_called()

    def test_show_gif_with_no_decodable_frames_closes_and_returns_false(self):
        matrix = Matrix.__new__(Matrix)
        matrix._media_file = Mock(return_value=Mock())
        opened = Mock()

        with patch.object(framework.Image, "open", return_value=opened), patch.object(
            framework.ImageSequence, "Iterator", return_value=[]
        ):
            self.assertFalse(matrix.show_gif("sleep.gif", loops=0))

        opened.close.assert_called_once_with()

    def test_show_gif_never_uses_a_zero_frame_delay(self):
        matrix = Matrix.__new__(Matrix)
        matrix.canvas = Mock()
        matrix.matrix = Mock(width=64, height=32)
        matrix._frame_capture = Mock()
        matrix.clear = Mock()
        matrix.swap = Mock(return_value=matrix.canvas)
        frame = Mock(info={"duration": 0})
        frame.copy.return_value = frame
        matrix._prepare_image = Mock(return_value=frame)
        matrix._media_file = Mock(return_value=Mock())

        with patch.object(framework.Image, "open", return_value=Mock()), patch.object(
            framework.ImageSequence, "Iterator", return_value=[frame]
        ), patch.object(framework.time, "sleep") as sleep:
            self.assertTrue(matrix.show_gif("sleep.gif", loops=1, playback_rate=4.0))

        sleep.assert_called_once_with(0.01)

    def test_show_gif_wait_never_exceeds_remaining_module_duration(self):
        matrix = Matrix.__new__(Matrix)
        matrix.canvas = Mock()
        matrix.matrix = Mock(width=64, height=32)
        matrix._frame_capture = Mock()
        matrix.clear = Mock()
        matrix.swap = Mock(return_value=matrix.canvas)
        frame = Mock(info={"duration": 10_000})
        frame.copy.return_value = frame
        matrix._prepare_image = Mock(return_value=frame)
        matrix._media_file = Mock(return_value=Mock())

        with patch.object(framework.Image, "open", return_value=Mock()), patch.object(
            framework.ImageSequence, "Iterator", return_value=[frame]
        ), patch.object(framework.time, "monotonic", side_effect=[0.0, 0.0, 0.0]), patch.object(
            framework.time, "sleep"
        ) as sleep:
            self.assertTrue(
                matrix.show_gif(
                    "sleep.gif", loops=1, playback_rate=0.25, max_duration=0.05
                )
            )

        sleep.assert_called_once_with(0.05)

    def test_slowest_show_gif_rate_stops_promptly_when_interrupted(self):
        matrix = Matrix.__new__(Matrix)
        matrix.canvas = Mock()
        matrix.matrix = Mock(width=64, height=32)
        matrix._frame_capture = Mock()
        matrix.clear = Mock()
        frame_ready = threading.Event()
        matrix.swap = Mock(side_effect=lambda: frame_ready.set() or matrix.canvas)
        frame = Mock(info={"duration": 10_000})
        frame.copy.return_value = frame
        matrix._prepare_image = Mock(return_value=frame)
        matrix._media_file = Mock(return_value=Mock())
        stop = threading.Event()
        result = []

        with patch.object(framework.Image, "open", return_value=Mock()), patch.object(
            framework.ImageSequence, "Iterator", return_value=[frame]
        ):
            worker = threading.Thread(
                target=lambda: result.append(
                    matrix.show_gif(
                        "sleep.gif", loops=0, playback_rate=0.25, stop_event=stop
                    )
                )
            )
            worker.start()
            self.assertTrue(frame_ready.wait(0.5))
            stop.set()
            worker.join(0.5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [True])

    def test_show_gif_playback_does_not_modify_the_media_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sleep.gif"
            first = PillowImage.new("RGB", (2, 2), "red")
            second = PillowImage.new("RGB", (2, 2), "blue")
            first.save(
                path,
                format="GIF",
                save_all=True,
                append_images=[second],
                duration=[100, 200],
                loop=0,
            )
            before = path.read_bytes()
            matrix = Matrix.__new__(Matrix)
            matrix.canvas = Mock()
            matrix.matrix = Mock(width=64, height=32)
            matrix._frame_capture = Mock()
            matrix.clear = Mock()
            matrix.swap = Mock(return_value=matrix.canvas)
            matrix._prepare_image = Mock(side_effect=lambda frame, **_: frame.copy())
            matrix._media_file = Mock(return_value=path)

            with patch.object(framework.Image, "open", side_effect=PillowImage.open), patch.object(
                framework.ImageSequence, "Iterator", side_effect=PillowImageSequence.Iterator
            ), patch.object(framework.time, "sleep"):
                self.assertTrue(matrix.show_gif("sleep.gif", loops=1, playback_rate=2.0))

            self.assertEqual(path.read_bytes(), before)

    def test_show_video_decodes_frames_into_hardware_and_live_preview(self):
        matrix = Matrix.__new__(Matrix)
        matrix.matrix = Mock(width=64, height=32)
        matrix.canvas = Mock()
        matrix._frame_capture = Mock()
        matrix.clear = Mock()
        matrix.swap = Mock(return_value=matrix.canvas)
        matrix._media_file = Mock(return_value=framework.pathlib.Path("/tmp/clip.mp4"))
        frame = b"\x10\x20\x30" * (64 * 32)
        process = Mock()
        process.wait.return_value = 0
        image = Mock()

        with patch.object(framework.os, "open", return_value=9), patch.object(
            framework.os, "lseek"
        ), patch.object(framework.os, "close"), patch.object(
            framework.subprocess, "Popen", return_value=process
        ) as popen, patch.object(
            framework.Image, "frombytes", return_value=image, create=True
        ), patch.object(
            framework, "_read_exact_interruptibly", side_effect=[frame, b""]
        ), patch.object(framework.time, "sleep"):
            self.assertTrue(matrix.show_video("clip.mp4", loops=1))

        matrix.canvas.SetImage.assert_called_once_with(image, 0, 0)
        matrix._frame_capture.set_image.assert_called_once_with(matrix.canvas, image, 0, 0)
        matrix.swap.assert_called_once()
        command = popen.call_args.args[0]
        video_filter = command[command.index("-vf") + 1]
        self.assertEqual(
            video_filter,
            "scale=64:32:force_original_aspect_ratio=decrease,"
            "pad=64:32:(ow-iw)/2:(oh-ih)/2:black",
        )

    def test_show_video_cancellation_interrupts_blocked_decoder_read(self):
        matrix = Matrix.__new__(Matrix)
        matrix.matrix = Mock(width=64, height=32)
        matrix.canvas = Mock()
        matrix._frame_capture = Mock()
        matrix.clear = Mock()
        matrix.swap = Mock(return_value=matrix.canvas)
        stop = threading.Event()
        decoder_started = threading.Event()
        finished = threading.Event()
        read_fd, write_fd = os.pipe()
        process = Mock()
        process.stdout = os.fdopen(read_fd, "rb", buffering=0)
        process.poll.return_value = None
        process.wait.return_value = 0

        def launch(*_args, **_kwargs):
            decoder_started.set()
            return process

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blocked.mp4"
            path.write_bytes(b"input")
            matrix._media_file = Mock(return_value=path)
            with patch.object(framework.subprocess, "Popen", side_effect=launch):
                worker = threading.Thread(
                    target=lambda: (
                        matrix.show_video("blocked.mp4", stop_event=stop), finished.set()
                    )
                )
                worker.start()
                self.assertTrue(decoder_started.wait(0.5))
                stop.set()
                stopped_promptly = finished.wait(0.3)
                os.close(write_fd)
                worker.join(1)

        process.stdout.close()
        self.assertTrue(stopped_promptly, "cancellation did not interrupt the FFmpeg read")
        self.assertFalse(worker.is_alive())
        process.terminate.assert_called_once_with()

    def test_blocked_decoder_read_observes_video_deadline(self):
        read_fd, write_fd = os.pipe()
        started = time.monotonic()
        try:
            with os.fdopen(read_fd, "rb", buffering=0) as stream:
                frame = framework._read_exact_interruptibly(
                    stream, 64 * 32 * 3, deadline=started + 0.05
                )
        finally:
            os.close(write_fd)

        self.assertEqual(frame, b"")
        self.assertLess(time.monotonic() - started, 0.3)

    def test_widget_scroll_is_mirrored_into_the_live_preview(self):
        matrix = Matrix.__new__(Matrix)
        canvas = Mock(width=64)
        font = Mock()
        colour = types.SimpleNamespace(red=255, green=179, blue=0)
        matrix.canvas = canvas
        matrix._frame_capture = Mock()
        matrix._capture_fonts = {id(font): "capture-font"}

        with patch.object(framework.graphics, "DrawText", return_value=18, create=True):
            position, width = matrix.widget_scroll(
                "Brighton | Clear sky", font, 30, 31, colour, canvas=canvas
            )

        self.assertEqual((position, width), (29, 18))
        matrix._frame_capture.text.assert_called_once_with(
            canvas, "capture-font", 30, 31, colour, "Brighton | Clear sky"
        )

    def test_widget_scroll_wraps_right_moving_text_to_the_left_edge(self):
        matrix = Matrix.__new__(Matrix)
        canvas = Mock(width=64)
        font = Mock()
        colour = types.SimpleNamespace(red=255, green=179, blue=0)
        matrix.canvas = canvas
        matrix._frame_capture = Mock()
        matrix._capture_fonts = {id(font): "capture-font"}

        with patch.object(framework.graphics, "DrawText", return_value=18, create=True):
            position, width = matrix.widget_scroll(
                "Weather", font, 65, 31, colour, canvas=canvas, step=-1, wrap=True
            )

        self.assertEqual((position, width), (-18, 18))

    def test_media_paths_cannot_escape_media_directory(self):
        matrix = Matrix.__new__(Matrix)
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "media"
            media.mkdir()
            inside = media / "ok.png"
            inside.write_bytes(b"png")
            outside = Path(directory) / "secret.txt"
            outside.write_text("secret")
            with patch.object(framework, "media_path", media):
                self.assertEqual(matrix._media_file("ok.png"), inside.resolve())
                self.assertIsNone(matrix._media_file("../secret.txt"))
                self.assertIsNone(matrix._media_file(str(outside)))


if __name__ == "__main__":
    unittest.main()
