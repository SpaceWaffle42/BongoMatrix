import importlib
import os
import subprocess
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
    def test_import_without_rgbmatrix_allows_capture_but_physical_matrix_fails_closed(self):
        script = """
import matrix_framework
colour = matrix_framework.graphics.Color(1, 2, 3)
assert (colour.red, colour.green, colour.blue) == (1, 2, 3)
try:
    matrix_framework.Matrix()
except RuntimeError as error:
    assert str(error) == 'RGB Matrix hardware bindings are unavailable'
else:
    raise AssertionError('physical matrix construction unexpectedly succeeded')
"""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=directory, env=environment,
                text=True, capture_output=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_import_propagates_unrelated_rgbmatrix_transitive_module_not_found(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "rgbmatrix.py").write_text(
                "raise ModuleNotFoundError(\"No module named 'unrelated_native_dependency'\", "
                "name='unrelated_native_dependency')\n",
                encoding="utf-8",
            )
            script = """
try:
    import matrix_framework
except ModuleNotFoundError as error:
    assert error.name == 'unrelated_native_dependency'
    assert str(error) == "No module named 'unrelated_native_dependency'"
else:
    raise AssertionError('transitive import fault was hidden by fallback')
"""
            environment = os.environ.copy()
            environment["PYTHONPATH"] = os.pathsep.join((
                directory, str(Path(__file__).resolve().parents[1]),
            ))
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=directory, env=environment,
                text=True, capture_output=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)

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

    def test_show_accepts_custom_swap_without_admission_capability(self):
        class CustomSwapAdapter:
            def __init__(self):
                self.swaps = 0

            def swap(self):
                self.swaps += 1

        adapter = CustomSwapAdapter()

        self.assertTrue(Matrix.show(adapter))
        self.assertEqual(adapter.swaps, 1)

    def test_show_image_cancelled_during_final_canvas_draw_is_not_published(self):
        matrix = Matrix.__new__(Matrix)
        matrix.canvas = Mock()
        matrix._frame_capture = Mock()
        matrix.load_image = Mock(return_value="decoded-image")
        matrix.show = Mock()
        stop_event = threading.Event()
        matrix.canvas.SetImage.side_effect = lambda *_args: stop_event.set()

        result = matrix.show_image(
            "custom.png", delay=None, stop_event=stop_event,
        )

        self.assertFalse(result)
        matrix.canvas.SetImage.assert_called_once_with("decoded-image", 0, 0)
        matrix.show.assert_not_called()

    def test_show_image_guard_rejection_is_reported_as_not_rendered(self):
        matrix = Matrix.__new__(Matrix)
        matrix.canvas = Mock()
        matrix.matrix = Mock()
        matrix._frame_capture = Mock()
        matrix._physical_swap_observer = None
        matrix.load_image = Mock(return_value="decoded-image")
        stop_event = threading.Event()

        def reject_at_hardware_admission():
            stop_event.set()
            return False

        matrix._publication_guard = reject_at_hardware_admission

        result = matrix.show_image(
            "custom.png", delay=None, stop_event=stop_event,
        )

        self.assertFalse(result)
        matrix.matrix.SwapOnVSync.assert_not_called()
        matrix._frame_capture.publish.assert_not_called()

    def test_static_playback_propagates_hardware_guard_rejection(self):
        from modules.media_player import play_media

        matrix = Matrix.__new__(Matrix)
        matrix.canvas = Mock()
        matrix.matrix = Mock()
        matrix._frame_capture = Mock()
        matrix._physical_swap_observer = None
        matrix.load_image = Mock(return_value="decoded-image")
        stop_event = threading.Event()

        def reject_at_hardware_admission():
            stop_event.set()
            return False

        matrix._publication_guard = reject_at_hardware_admission

        self.assertFalse(play_media(
            matrix, "custom.png", 8, stop_event=stop_event,
        ))
        matrix.matrix.SwapOnVSync.assert_not_called()
        matrix._frame_capture.publish.assert_not_called()

    def test_show_image_cancelled_during_capture_update_is_not_published(self):
        matrix = Matrix.__new__(Matrix)
        matrix.canvas = Mock()
        matrix._frame_capture = Mock()
        matrix.load_image = Mock(return_value="decoded-image")
        matrix.show = Mock()
        stop_event = threading.Event()
        matrix._frame_capture.set_image.side_effect = (
            lambda *_args: stop_event.set()
        )

        result = matrix.show_image(
            "custom.png", delay=None, stop_event=stop_event,
        )

        self.assertFalse(result)
        matrix.show.assert_not_called()

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
        real_close = os.close
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
            real_close(write_fd)
            worker.join(1)

        process.stdout.close()
        with framework._decoder_reaper_lock:
            reaper = framework._decoder_reaper_thread
        self.assertIsNotNone(reaper)
        reaper.join(0.5)
        self.assertFalse(reaper.is_alive(), "deferred decoder reaper outlived its test")
        framework._reset_decoder_reaper_for_tests()
        self.assertTrue(stopped_promptly, "cancellation did not interrupt image decoding")
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [])
        pillow_open.assert_not_called()
        process.kill.assert_called_once_with()
        self.assertGreaterEqual(process.wait.call_count, 1)
        with self.assertRaises(OSError):
            os.fstat(write_fd)
        self.assertIsNone(framework._decoder_reaper_owner)

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

    def test_media_frames_reaper_start_failure_precedes_child_spawn(self):
        matrix = Matrix.__new__(Matrix)
        matrix.matrix = Mock(width=64, height=32)
        matrix._media_file = Mock(return_value=framework.pathlib.Path("/tmp/asset.gif"))

        with patch.object(
            framework, "_ensure_decoder_reaper",
            side_effect=RuntimeError("reaper unavailable"),
        ), patch.object(
            framework.os, "open", return_value=9,
        ), patch.object(
            framework.os, "close",
        ) as close_fd, patch.object(
            framework.subprocess, "Popen",
        ) as popen, patch.object(
            framework, "_read_exact_interruptibly", return_value=b"",
        ):
            with self.assertRaisesRegex(RuntimeError, "reaper unavailable"):
                matrix.media_frames("asset.gif", 1, 1)

        popen.assert_not_called()
        close_fd.assert_called_once_with(9)

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

    def test_decoder_reaper_normal_lifecycle_leaves_no_thread(self):
        framework._reset_decoder_reaper_for_tests()
        work_queue = framework._ensure_decoder_reaper()
        reaper = framework._decoder_reaper_thread

        framework._release_decoder_reaper(work_queue)

        self.assertFalse(reaper.is_alive())
        self.assertIsNone(framework._decoder_reaper_thread)
        self.assertIsNone(framework._decoder_reaper_queue)
        self.assertEqual(framework._decoder_reaper_leases, 0)

    def test_decoder_reaper_concurrent_leases_stop_only_after_final_release(self):
        framework._reset_decoder_reaper_for_tests()
        first_queue = framework._ensure_decoder_reaper()
        reaper = framework._decoder_reaper_thread
        second_queue = framework._ensure_decoder_reaper()
        self.assertIsNot(second_queue, first_queue)
        self.assertIs(second_queue.owner, first_queue.owner)

        framework._release_decoder_reaper(first_queue)
        self.assertTrue(reaper.is_alive())
        self.assertIs(framework._decoder_reaper_thread, reaper)
        self.assertEqual(framework._decoder_reaper_leases, 1)

        framework._release_decoder_reaper(second_queue)
        self.assertFalse(reaper.is_alive())
        self.assertIsNone(framework._decoder_reaper_thread)
        self.assertEqual(framework._decoder_reaper_leases, 0)

    def test_media_frames_popen_failure_releases_owner_and_input_fd(self):
        framework._reset_decoder_reaper_for_tests()
        matrix = Matrix.__new__(Matrix)
        matrix.matrix = Mock(width=64, height=32)
        matrix._media_file = Mock(return_value=framework.pathlib.Path("/tmp/asset.gif"))

        with patch.object(framework.os, "geteuid", return_value=1), patch.object(
            framework.os, "open", return_value=9,
        ), patch.object(framework.os, "close") as close_fd, patch.object(
            framework.subprocess, "Popen", side_effect=RuntimeError("spawn failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "spawn failed"):
                matrix.media_frames("asset.gif", 1, 1)

        close_fd.assert_called_once_with(9)
        self.assertIsNone(framework._decoder_reaper_thread)
        self.assertIsNone(framework._decoder_reaper_queue)
        self.assertEqual(framework._decoder_reaper_leases, 0)

    def test_decoder_reaper_deferred_process_precedes_stop_and_worker_exits(self):
        framework._reset_decoder_reaper_for_tests()
        operations = []
        real_queue_type = framework.queue.SimpleQueue

        class RecordingQueue:
            def __init__(self):
                self._queue = real_queue_type()

            def put(self, item):
                operations.append(item)
                self._queue.put(item)

            def get(self):
                return self._queue.get()

        process = Mock()
        reaped = threading.Event()
        process.wait.side_effect = lambda: reaped.set()
        with patch.object(framework.queue, "SimpleQueue", RecordingQueue):
            work_queue = framework._ensure_decoder_reaper()
            reaper = framework._decoder_reaper_thread
            framework._release_decoder_reaper(work_queue, deferred_process=process)

        self.assertEqual(operations, [])
        self.assertTrue(reaped.wait(0.5), "deferred decoder was not reaped")
        reaper.join(0.5)
        self.assertFalse(reaper.is_alive())

    def test_media_frames_deferred_handoff_put_fault_preserves_primary_and_child_owner(self):
        framework._reset_decoder_reaper_for_tests()
        matrix = Matrix.__new__(Matrix)
        matrix.matrix = Mock(width=64, height=32)
        matrix._media_file = Mock(return_value=framework.pathlib.Path("/tmp/asset.gif"))
        real_queue_type = framework.queue.SimpleQueue
        reaped = threading.Event()

        class DeferredPutFaultQueue:
            def __init__(self):
                self._queue = real_queue_type()

            def put(self, item):
                if item is not framework._DECODER_REAPER_STOP:
                    raise RuntimeError("deferred put failed")
                self._queue.put(item)

            def get(self):
                return self._queue.get()

        process = Mock()
        process.stdout = Mock()
        process.poll.return_value = None

        def wait_for_decoder(*_args, **kwargs):
            if "timeout" in kwargs:
                raise subprocess.TimeoutExpired("ffmpeg", kwargs["timeout"])
            reaped.set()
            return 0

        process.wait.side_effect = wait_for_decoder
        with patch.object(framework.queue, "SimpleQueue", DeferredPutFaultQueue), patch.object(
            framework.os, "geteuid", return_value=1,
        ), patch.object(framework.os, "open", return_value=9), patch.object(
            framework.os, "close",
        ), patch.object(framework.subprocess, "Popen", return_value=process), patch.object(
            framework, "_read_exact_interruptibly",
            side_effect=ValueError("primary decode failed"),
        ):
            with self.assertRaisesRegex(ValueError, "primary decode failed"):
                matrix.media_frames("asset.gif", 1, 1, decode_timeout=1)

        self.assertTrue(reaped.wait(0.5), "killed decoder lost its pre-spawn owner")

    def test_decoder_reaper_stop_put_fault_never_detaches_live_worker(self):
        framework._reset_decoder_reaper_for_tests()
        real_queue_type = framework.queue.SimpleQueue

        class StopPutFaultQueue:
            def __init__(self):
                self._queue = real_queue_type()

            def put(self, item):
                if item is framework._DECODER_REAPER_STOP:
                    raise RuntimeError("stop put failed")
                self._queue.put(item)

            def get(self):
                return self._queue.get()

            def force_stop(self):
                self._queue.put(framework._DECODER_REAPER_STOP)

        with patch.object(framework.queue, "SimpleQueue", StopPutFaultQueue):
            work_queue = framework._ensure_decoder_reaper()
            reaper = framework._decoder_reaper_thread
            try:
                try:
                    framework._release_decoder_reaper(work_queue)
                except RuntimeError:
                    pass
                worker_alive = reaper.is_alive()
                tracked_thread = framework._decoder_reaper_thread
                tracked_owner = framework._decoder_reaper_queue
            finally:
                if reaper.is_alive():
                    work_queue.force_stop()
                    reaper.join(0.5)
                framework._decoder_reaper_thread = None
                framework._decoder_reaper_queue = None
                framework._decoder_reaper_leases = 0
                framework._decoder_reaper_had_deferred = False

        self.assertTrue(
            not worker_alive
            or (tracked_thread is reaper and tracked_owner is work_queue),
            "live decoder reaper became unreachable after STOP publication failed",
        )

    def test_decoder_reaper_new_acquire_waits_for_old_owned_child_to_exit(self):
        framework._reset_decoder_reaper_for_tests()
        release_wait = threading.Event()
        process = Mock()
        process.wait.side_effect = lambda: release_wait.wait(0.5)
        first_queue = framework._ensure_decoder_reaper()
        first_reaper = framework._decoder_reaper_thread

        framework._release_decoder_reaper(first_queue, deferred_process=process)
        with self.assertRaisesRegex(RuntimeError, "shutting down"):
            framework._ensure_decoder_reaper()
        self.assertIs(framework._decoder_reaper_thread, first_reaper)
        self.assertIs(framework._decoder_reaper_queue, first_queue.owner)

        release_wait.set()
        first_reaper.join(0.5)
        second_queue = framework._ensure_decoder_reaper()
        second_reaper = framework._decoder_reaper_thread

        self.assertIsNot(second_queue, first_queue)
        self.assertIsNot(second_reaper, first_reaper)
        framework._release_decoder_reaper(second_queue)
        self.assertFalse(first_reaper.is_alive())
        self.assertFalse(second_reaper.is_alive())

    def test_decoder_reaper_release_join_fault_preserves_popen_failure(self):
        framework._reset_decoder_reaper_for_tests()
        matrix = Matrix.__new__(Matrix)
        matrix.matrix = Mock(width=64, height=32)
        matrix._media_file = Mock(return_value=framework.pathlib.Path("/tmp/asset.gif"))
        reaper = Mock()
        reaper.is_alive.return_value = True
        reaper.join.side_effect = RuntimeError("join failed")

        try:
            with patch.object(framework.os, "geteuid", return_value=1), patch.object(
                framework.os, "open", return_value=9,
            ), patch.object(framework.os, "close"), patch.object(
                framework.threading, "Thread", return_value=reaper,
            ), patch.object(
                framework.subprocess, "Popen", side_effect=ValueError("primary spawn failed"),
            ):
                with self.assertRaisesRegex(ValueError, "primary spawn failed"):
                    matrix.media_frames("asset.gif", 1, 1)

            reaper.start.assert_called_once_with()
            reaper.join.assert_called_once()
            self.assertIs(framework._decoder_reaper_thread, reaper)
            self.assertIsNotNone(framework._decoder_reaper_queue)
            self.assertEqual(framework._decoder_reaper_leases, 0)
        finally:
            with framework._decoder_reaper_lock:
                framework._set_decoder_reaper_owner(None)

    def test_video_frame_timeline_reaper_start_failure_precedes_child_spawn(self):
        matrix = Matrix.__new__(Matrix)
        reaper = Mock()
        reaper.start.side_effect = RuntimeError("reaper unavailable")

        with patch.object(
            framework, "_decoder_reaper_thread", None, create=True,
        ), patch.object(
            framework, "_decoder_reaper_queue", None, create=True,
        ), patch.object(
            framework.threading, "Thread", return_value=reaper,
        ), patch.object(
            framework.os, "open", return_value=9,
        ), patch.object(
            framework.os, "close",
        ) as close_fd, patch.object(
            framework.subprocess, "Popen",
        ) as popen, patch.object(
            framework, "_read_exact_interruptibly", return_value=b"",
        ):
            with self.assertRaisesRegex(RuntimeError, "reaper unavailable"):
                list(matrix._video_frame_timeline("video.mp4", 2, 2, True))

        popen.assert_not_called()
        close_fd.assert_called_once_with(9)

    def test_video_frame_timeline_exhausted_deadline_defers_without_waiting(self):
        matrix = Matrix.__new__(Matrix)
        process = Mock()
        process.stdout = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [
            framework.subprocess.TimeoutExpired("ffmpeg", 2),
            framework.subprocess.TimeoutExpired("ffmpeg", 0.25),
        ]
        owned_queue = Mock()

        with patch.object(
            framework, "_ensure_decoder_reaper", return_value=owned_queue,
        ), patch.object(
            framework, "_release_decoder_reaper",
        ) as release_reaper, patch.object(
            framework.os, "open", return_value=9,
        ), patch.object(
            framework.os, "close",
        ), patch.object(
            framework.subprocess, "Popen", return_value=process,
        ), patch.object(
            framework, "_read_exact_interruptibly", return_value=b"",
        ):
            self.assertEqual(list(matrix._video_frame_timeline(
                "video.mp4", 2, 2, True, deadline=0.0,
            )), [])

        process.terminate.assert_called_once_with()
        process.wait.assert_not_called()
        process.kill.assert_not_called()
        release_reaper.assert_called_once_with(
            owned_queue, deferred_process=process,
        )

    def test_video_frame_timeline_cancellation_defers_without_waiting(self):
        matrix = Matrix.__new__(Matrix)
        process = Mock()
        process.stdout = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [
            framework.subprocess.TimeoutExpired("ffmpeg", 2),
            framework.subprocess.TimeoutExpired("ffmpeg", 0.25),
        ]
        owned_queue = Mock()
        stop = threading.Event()

        def cancel_during_read(*_args, **_kwargs):
            stop.set()
            return b""

        with patch.object(
            framework, "_ensure_decoder_reaper", return_value=owned_queue,
        ), patch.object(
            framework, "_release_decoder_reaper",
        ) as release_reaper, patch.object(
            framework.os, "open", return_value=9,
        ), patch.object(
            framework.os, "close",
        ), patch.object(
            framework.subprocess, "Popen", return_value=process,
        ), patch.object(
            framework, "_read_exact_interruptibly", side_effect=cancel_during_read,
        ), patch.object(
            framework.time, "monotonic", return_value=10.0,
        ):
            self.assertEqual(list(matrix._video_frame_timeline(
                "video.mp4", 2, 2, True,
                stop_event=stop, deadline=100.0,
            )), [])

        process.terminate.assert_called_once_with()
        process.wait.assert_not_called()
        process.kill.assert_not_called()
        release_reaper.assert_called_once_with(
            owned_queue, deferred_process=process,
        )

    def test_video_frame_timeline_late_cancellation_cannot_enter_wait(self):
        matrix = Matrix.__new__(Matrix)
        process = Mock()
        process.stdout = Mock()
        process.poll.return_value = None
        owned_queue = Mock()
        stop = threading.Event()

        def cancel_inside_wait(*_args, **_kwargs):
            stop.set()
            return 0

        process.wait.side_effect = cancel_inside_wait

        with patch.object(
            framework, "_ensure_decoder_reaper", return_value=owned_queue,
        ), patch.object(
            framework, "_release_decoder_reaper",
        ) as release_reaper, patch.object(
            framework.os, "open", return_value=9,
        ), patch.object(
            framework.os, "close",
        ), patch.object(
            framework.subprocess, "Popen", return_value=process,
        ), patch.object(
            framework, "_read_exact_interruptibly", return_value=b"",
        ):
            self.assertEqual(list(matrix._video_frame_timeline(
                "video.mp4", 2, 2, True, stop_event=stop,
            )), [])

        process.terminate.assert_called_once_with()
        process.wait.assert_not_called()
        process.kill.assert_not_called()
        release_reaper.assert_called_once_with(
            owned_queue, deferred_process=process,
        )

    def test_video_frame_timeline_timeout_transfers_reaping_to_existing_owner_across_process_races(self):
        matrix = Matrix.__new__(Matrix)
        for race in ("poll", "terminate"):
            with self.subTest(race=race):
                process = Mock()
                process.stdout = Mock()
                process.poll.return_value = None
                if race == "poll":
                    process.poll.side_effect = ProcessLookupError("poll raced")
                else:
                    process.terminate.side_effect = ProcessLookupError("terminate raced")
                process.wait.side_effect = [
                    framework.subprocess.TimeoutExpired("ffmpeg", 2),
                    framework.subprocess.TimeoutExpired("ffmpeg", 0.25),
                ]
                owned_queue = Mock()

                with patch.object(
                    framework, "_ensure_decoder_reaper", return_value=owned_queue,
                ), patch.object(
                    framework, "_release_decoder_reaper",
                ) as release_reaper, patch.object(
                    framework.threading, "Thread",
                    side_effect=RuntimeError("thread creation unavailable after spawn"),
                ) as thread, patch.object(
                    framework.os, "open", return_value=9,
                ), patch.object(
                    framework.os, "close",
                ), patch.object(
                    framework.subprocess, "Popen", return_value=process,
                ), patch.object(
                    framework, "_read_exact_interruptibly", return_value=b"",
                ):
                    self.assertEqual(
                        list(matrix._video_frame_timeline("video.mp4", 2, 2, True)),
                        [],
                    )

                self.assertEqual(process.wait.call_count, 2)
                process.kill.assert_called_once_with()
                release_reaper.assert_called_once_with(
                    owned_queue, deferred_process=process,
                )
                thread.assert_not_called()

    def test_video_frame_timeline_closes_stdout_and_input_on_normal_exhaustion(self):
        matrix = Matrix.__new__(Matrix)
        raw = b"\x10\x20\x30" * 4
        image = Mock()
        process = Mock()
        process.stdout = Mock()
        process.poll.return_value = 0
        process.wait.return_value = 0

        with patch.object(framework.os, "open", return_value=9), patch.object(
            framework.os, "close"
        ) as close_fd, patch.object(
            framework.subprocess, "Popen", return_value=process
        ), patch.object(
            framework, "_read_exact_interruptibly", side_effect=[raw, b""]
        ), patch.object(
            framework.Image, "frombytes", return_value=image, create=True
        ):
            frames = list(matrix._video_frame_timeline("video.mp4", 2, 2, True))

        self.assertEqual(frames, [image])
        process.stdout.close.assert_called_once_with()
        close_fd.assert_called_once_with(9)

    def test_video_frame_timeline_closes_stdout_and_input_on_early_close(self):
        matrix = Matrix.__new__(Matrix)
        raw = b"\x10\x20\x30" * 4
        process = Mock()
        process.stdout = Mock()
        process.poll.return_value = None
        process.wait.return_value = 0

        with patch.object(framework.os, "open", return_value=9), patch.object(
            framework.os, "close"
        ) as close_fd, patch.object(
            framework.subprocess, "Popen", return_value=process
        ), patch.object(
            framework, "_read_exact_interruptibly", return_value=raw
        ), patch.object(
            framework.Image, "frombytes", return_value=Mock(), create=True
        ):
            timeline = matrix._video_frame_timeline("video.mp4", 2, 2, True)
            next(timeline)
            close_fd.assert_called_once_with(9)
            timeline.close()

        process.stdout.close.assert_called_once_with()
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=2)
        close_fd.assert_called_once_with(9)

    def test_video_frame_timeline_cleanup_preserves_primary_decoder_failure(self):
        matrix = Matrix.__new__(Matrix)
        process = Mock()
        process.stdout.close.side_effect = RuntimeError("stdout close failed")
        process.poll.return_value = 0
        process.wait.return_value = 0

        with patch.object(framework.os, "open", return_value=9), patch.object(
            framework.os, "close",
        ) as close_fd, patch.object(
            framework.subprocess, "Popen", return_value=process,
        ), patch.object(
            framework, "_read_exact_interruptibly",
            side_effect=ValueError("primary read failed"),
        ):
            with self.assertRaisesRegex(ValueError, "primary read failed"):
                list(matrix._video_frame_timeline("video.mp4", 2, 2, True))

        process.stdout.close.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=2)
        close_fd.assert_called_once_with(9)

    def test_video_frame_timeline_surfaces_cleanup_failure_without_primary_failure(self):
        matrix = Matrix.__new__(Matrix)
        process = Mock()
        process.stdout.close.side_effect = RuntimeError("stdout close failed")
        process.poll.return_value = 0
        process.wait.return_value = 0

        with patch.object(framework.os, "open", return_value=9), patch.object(
            framework.os, "close",
        ) as close_fd, patch.object(
            framework.subprocess, "Popen", return_value=process,
        ), patch.object(
            framework, "_read_exact_interruptibly", return_value=b"",
        ):
            with self.assertRaisesRegex(RuntimeError, "stdout close failed"):
                list(matrix._video_frame_timeline("video.mp4", 2, 2, True))

        process.wait.assert_called_once_with(timeout=2)
        close_fd.assert_called_once_with(9)

    def test_video_frame_timeline_parent_input_close_failure_surfaces_without_unsafe_retry(self):
        matrix = Matrix.__new__(Matrix)
        process = Mock()
        process.poll.return_value = 0
        process.wait.return_value = 0
        close_calls = []

        def ambiguous_close(fd):
            close_calls.append(fd)
            if len(close_calls) == 1:
                raise InterruptedError("parent input close ownership ambiguous")
            raise OSError("numeric descriptor was unsafely retried after ambiguous close")

        with patch.object(framework.os, "open", return_value=9), patch.object(
            framework.os, "close", side_effect=ambiguous_close,
        ), patch.object(
            framework.subprocess, "Popen", return_value=process,
        ), patch.object(
            framework, "_read_exact_interruptibly", return_value=b"",
        ) as read_frame:
            with self.assertRaisesRegex(
                InterruptedError, "parent input close ownership ambiguous",
            ):
                list(matrix._video_frame_timeline("video.mp4", 2, 2, True))

        self.assertEqual(close_calls, [9])
        read_frame.assert_not_called()
        process.stdout.close.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=2)

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

        owned_queue = Mock()
        with patch.object(framework.time, "monotonic", side_effect=[0, 46]), patch.object(
            framework.os, "open", return_value=9,
        ), patch.object(framework.os, "close"), patch.object(
            framework.subprocess, "Popen", return_value=process,
        ), patch.object(
            framework, "_read_exact_interruptibly", side_effect=cancel_during_read,
        ), patch.object(
            framework, "_ensure_decoder_reaper", return_value=owned_queue,
        ), patch.object(
            framework, "_release_decoder_reaper",
        ) as release_reaper, patch.object(
            framework.threading, "Thread",
            side_effect=AssertionError("reaper ownership must exist before decoder spawn"),
        ) as thread:
            self.assertEqual(
                matrix.media_frames("animation.gif", 1, 1, stop_event=stop),
                [],
            )

        process.kill.assert_called_once_with()
        process.wait.assert_not_called()
        release_reaper.assert_called_once_with(
            owned_queue, deferred_process=process,
        )
        thread.assert_not_called()

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
        owned_queue = Mock()
        with patch.object(framework.time, "monotonic", side_effect=lambda: clock[0]), patch.object(
            framework.os, "open", return_value=9,
        ), patch.object(framework.os, "close"), patch.object(
            framework.subprocess, "Popen", side_effect=launch_decoder,
        ), patch.object(
            framework, "_read_ppm_frame_interruptibly", side_effect=read_frame,
        ), patch.object(
            framework, "_ensure_decoder_reaper", return_value=owned_queue,
        ), patch.object(
            framework, "_release_decoder_reaper",
        ) as release_reaper, patch.object(
            framework.threading, "Thread",
            side_effect=AssertionError("per-failure reaper threads are forbidden"),
        ):
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
        self.assertEqual(
            [call.kwargs["deferred_process"] for call in release_reaper.call_args_list[:-1]],
            processes[:-1],
        )
        self.assertIsNone(
            release_reaper.call_args_list[-1].kwargs["deferred_process"],
        )

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

    def test_media_frames_cleanup_preserves_primary_decoder_failure(self):
        matrix = Matrix.__new__(Matrix)
        matrix.matrix = Mock(width=64, height=32)
        matrix._media_file = Mock(return_value=framework.pathlib.Path("/tmp/asset.gif"))

        for cleanup_case in ("stdout", "input_fd"):
            with self.subTest(cleanup_case=cleanup_case):
                process = Mock()
                process.poll.return_value = 0
                process.wait.return_value = 0
                if cleanup_case == "stdout":
                    process.stdout.close.side_effect = OSError("stdout close failed")

                close_error = (
                    OSError("input close failed")
                    if cleanup_case == "input_fd" else None
                )
                with patch.object(
                    framework.os, "open", return_value=19,
                ), patch.object(
                    framework.os, "close", side_effect=close_error,
                ) as close_fd, patch.object(
                    framework.subprocess, "Popen", return_value=process,
                ), patch.object(
                    framework, "_read_ppm_frame_interruptibly",
                    side_effect=ValueError("primary decode failed"),
                ):
                    with self.assertRaisesRegex(ValueError, "primary decode failed"):
                        matrix.media_frames(
                            "asset.gif", frame_count=1, fps=10,
                            preserve_size=True, decode_timeout=1,
                        )

                process.stdout.close.assert_called_once_with()
                process.wait.assert_not_called()
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

    def test_show_gif_pre_cancelled_returns_false_without_publication(self):
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
            self.assertFalse(matrix.show_gif("sleep.gif", loops=0, stop_event=stop, max_duration=10))
        matrix.canvas.SetImage.assert_not_called()
        matrix._media_file.assert_not_called()

    def test_gif_playback_cancel_during_blocked_copy_returns_without_publication(self):
        from modules.media_player import play_media

        matrix = Matrix.__new__(Matrix)
        matrix.canvas = Mock()
        matrix.matrix = Mock(width=64, height=32)
        matrix.clear = Mock()
        matrix.draw_image = Mock()
        matrix.swap = Mock(return_value=matrix.canvas)
        matrix._media_file = Mock(return_value=Mock())
        stop = threading.Event()
        entered = threading.Event()
        release = threading.Event()
        returned = threading.Event()
        candidate_closed = threading.Event()
        gif = Mock()
        frame = Mock(info={"duration": 100})
        candidate = Mock()
        candidate.close.side_effect = candidate_closed.set

        def blocked_copy():
            entered.set()
            release.wait()
            return candidate

        frame.copy.side_effect = blocked_copy
        result = {}

        def consume():
            try:
                result["shown"] = play_media(
                    matrix, "clip.gif", 8, stop_event=stop,
                )
            finally:
                returned.set()

        consumer = threading.Thread(target=consume)
        with patch.object(framework.Image, "open", return_value=gif), patch.object(
            framework.ImageSequence, "Iterator", return_value=iter((frame,)),
        ):
            consumer.start()
            self.assertTrue(entered.wait(0.5))
            stop.set()
            prompt_return = returned.wait(0.2)
            release.set()
            self.assertTrue(returned.wait(0.5))
            consumer.join(0.5)

        self.assertTrue(prompt_return)
        self.assertFalse(consumer.is_alive())
        self.assertFalse(result["shown"])
        matrix.clear.assert_not_called()
        matrix.draw_image.assert_not_called()
        matrix.swap.assert_not_called()
        self.assertTrue(candidate_closed.wait(0.5))
        candidate.close.assert_called_once_with()
        gif.close.assert_called_once_with()

    def test_show_gif_closes_transferred_candidate_and_timeline_on_all_exits(self):
        scenarios = (
            "normal", "cancel_during_draw", "draw_error",
            "admission_rejection", "wait_error",
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                matrix = Matrix.__new__(Matrix)
                matrix.canvas = Mock()
                matrix.matrix = Mock(width=64, height=32)
                matrix.clear = Mock()
                matrix.draw_image = Mock()
                matrix.swap = Mock(return_value=matrix.canvas)
                matrix._media_file = Mock(return_value=Mock())
                candidate = Mock()

                class Timeline:
                    def __init__(self):
                        self.emitted = False
                        self.close_calls = 0

                    def __iter__(self):
                        return self

                    def __next__(self):
                        if self.emitted:
                            raise StopIteration
                        self.emitted = True
                        return candidate, 0.01

                    def close(self):
                        self.close_calls += 1

                timeline = Timeline()
                stop = None
                admitted = True
                expected_error = None
                if scenario == "cancel_during_draw":
                    stop = threading.Event()
                    matrix.draw_image.side_effect = lambda *_a, **_k: stop.set()
                elif scenario == "draw_error":
                    matrix.draw_image.side_effect = RuntimeError("draw failed")
                    expected_error = "draw failed"
                elif scenario == "admission_rejection":
                    admitted = False
                elif scenario == "wait_error":
                    stop = Mock()
                    stop.is_set.return_value = False
                    stop.wait.side_effect = RuntimeError("wait failed")
                    expected_error = "wait failed"

                with patch(
                    "modules.media_player.gif_media_frames",
                    return_value=timeline,
                ), patch.object(
                    framework, "_swap_admitted", return_value=admitted,
                ), patch.object(framework.time, "sleep"):
                    if expected_error is None:
                        matrix.show_gif(
                            "owned.gif", loops=1, stop_event=stop,
                            max_duration=1,
                        )
                    else:
                        with self.assertRaisesRegex(RuntimeError, expected_error):
                            matrix.show_gif(
                                "owned.gif", loops=1, stop_event=stop,
                                max_duration=1,
                            )

                candidate.close.assert_called_once_with()
                self.assertEqual(timeline.close_calls, 1)

    def test_show_gif_cleanup_faults_preserve_primary_and_attempt_all_stages(self):
        scenarios = (
            ("draw_primary", "draw-primary"),
            ("swap_primary", "swap-primary"),
            ("wait_primary", "wait-primary"),
            ("normal_cleanup", "candidate-close"),
            ("cancel_cleanup", "candidate-close"),
            ("admission_cleanup", "candidate-close"),
            ("timeline_only", "timeline-close"),
        )
        for scenario, expected_error in scenarios:
            with self.subTest(scenario=scenario):
                matrix = Matrix.__new__(Matrix)
                matrix.canvas = Mock()
                matrix.matrix = Mock(width=64, height=32)
                matrix.clear = Mock()
                matrix.draw_image = Mock()
                matrix.swap = Mock(return_value=matrix.canvas)
                matrix._media_file = Mock(return_value=Mock())
                candidate = Mock()
                if scenario != "timeline_only":
                    candidate.close.side_effect = OSError("candidate-close")

                class Timeline:
                    def __init__(self):
                        self.emitted = False
                        self.close_calls = 0

                    def __iter__(self):
                        return self

                    def __next__(self):
                        if self.emitted:
                            raise StopIteration
                        self.emitted = True
                        return candidate, 0.01

                    def close(self):
                        self.close_calls += 1
                        raise OSError("timeline-close")

                timeline = Timeline()
                stop = None
                admitted = True
                if scenario == "draw_primary":
                    matrix.draw_image.side_effect = RuntimeError("draw-primary")
                elif scenario == "swap_primary":
                    matrix.swap.side_effect = RuntimeError("swap-primary")
                elif scenario == "wait_primary":
                    stop = Mock()
                    stop.is_set.return_value = False
                    stop.wait.side_effect = RuntimeError("wait-primary")
                elif scenario == "cancel_cleanup":
                    cancel_event = threading.Event()
                    stop = cancel_event
                    matrix.draw_image.side_effect = (
                        lambda *_a, **_k: cancel_event.set()
                    )
                elif scenario == "admission_cleanup":
                    admitted = False

                with patch(
                    "modules.media_player.gif_media_frames",
                    return_value=timeline,
                ), patch.object(
                    framework, "_swap_admitted", return_value=admitted,
                ), patch.object(framework.time, "sleep"), self.assertRaisesRegex(
                    (RuntimeError if scenario.endswith("primary") else OSError),
                    expected_error,
                ):
                    matrix.show_gif(
                        "cleanup.gif", loops=1, stop_event=stop,
                        max_duration=1,
                    )

                candidate.close.assert_called_once_with()
                self.assertEqual(timeline.close_calls, 1)

    def test_show_gif_cleanup_lookup_and_diagnostics_cannot_replace_primary(self):
        class CleanupBase(BaseException):
            pass

        class GetterOwner:
            def __init__(self, error):
                self.error = error
                self.close_lookups = 0

            @property
            def close(self):
                self.close_lookups += 1
                raise self.error

        class HostilePrimary(RuntimeError):
            def add_note(self, _note):
                raise RuntimeError("note-fault")

        class HostileCleanup(OSError):
            def __repr__(self):
                raise RuntimeError("repr-fault")

        scenarios = (
            "candidate_getter_with_primary",
            "timeline_getter_with_primary",
            "both_getters_without_primary",
            "hostile_note",
            "hostile_repr",
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                matrix = Matrix.__new__(Matrix)
                matrix.canvas = Mock()
                matrix.matrix = Mock(width=64, height=32)
                matrix.clear = Mock()
                matrix.draw_image = Mock()
                matrix.swap = Mock(return_value=matrix.canvas)
                matrix._media_file = Mock(return_value=Mock())
                candidate = Mock()
                timeline_error = None
                expected = None

                if scenario == "candidate_getter_with_primary":
                    candidate = GetterOwner(CleanupBase("candidate-getattr"))
                    expected = RuntimeError("draw-primary")
                    matrix.draw_image.side_effect = expected
                elif scenario == "timeline_getter_with_primary":
                    timeline_error = CleanupBase("timeline-getattr")
                    expected = RuntimeError("draw-primary")
                    matrix.draw_image.side_effect = expected
                elif scenario == "both_getters_without_primary":
                    candidate = GetterOwner(CleanupBase("candidate-getattr"))
                    timeline_error = CleanupBase("timeline-getattr")
                    expected = candidate.error
                elif scenario == "hostile_note":
                    expected = HostilePrimary("primary-note")
                    matrix.draw_image.side_effect = expected
                    candidate.close.side_effect = OSError("candidate-close")
                else:
                    expected = RuntimeError("draw-primary")
                    matrix.draw_image.side_effect = expected
                    candidate.close.side_effect = HostileCleanup("candidate-close")

                class Timeline:
                    def __init__(self):
                        self.emitted = False
                        self.close_calls = 0
                        self.close_lookups = 0

                    def __iter__(self):
                        return self

                    def __next__(self):
                        if self.emitted:
                            raise StopIteration
                        self.emitted = True
                        return candidate, 0.01

                    @property
                    def close(self):
                        self.close_lookups += 1
                        if timeline_error is not None:
                            raise timeline_error

                        def close_timeline():
                            self.close_calls += 1

                        return close_timeline

                timeline = Timeline()
                with patch(
                    "modules.media_player.gif_media_frames",
                    return_value=timeline,
                ), patch.object(
                    framework, "_swap_admitted", return_value=True,
                ), patch.object(framework.time, "sleep"):
                    try:
                        matrix.show_gif("getter.gif", loops=1, max_duration=1)
                    except BaseException as observed:
                        self.assertIs(observed, expected)
                    else:
                        self.fail("expected primary or first cleanup error")

                if isinstance(candidate, GetterOwner):
                    self.assertEqual(candidate.close_lookups, 1)
                else:
                    candidate.close.assert_called_once_with()
                self.assertEqual(timeline.close_lookups, 1)
                if timeline_error is None:
                    self.assertEqual(timeline.close_calls, 1)

    def test_show_gif_cancelled_during_final_draw_is_not_published(self):
        matrix = Matrix.__new__(Matrix)
        matrix.canvas = Mock()
        matrix.matrix = Mock(width=64, height=32)
        matrix.matrix.SwapOnVSync.return_value = matrix.canvas
        matrix._frame_capture = Mock()
        matrix._frame_capture.publish.return_value = 1
        matrix._physical_swap_observer = None
        matrix._publication_guard = lambda: True
        matrix.clear = Mock()
        stop = threading.Event()
        matrix.draw_image = Mock(side_effect=lambda *_args, **_kwargs: stop.set())
        matrix._media_file = Mock(return_value=Mock())

        with patch(
            "modules.media_player.gif_media_frames",
            return_value=[(Mock(), 0.1)],
        ):
            result = matrix.show_gif(
                "sleep.gif", loops=1, stop_event=stop, max_duration=10,
            )

        self.assertFalse(result)
        matrix.matrix.SwapOnVSync.assert_not_called()
        matrix._frame_capture.publish.assert_not_called()

    def test_gif_playback_propagates_first_hardware_guard_rejection(self):
        from modules.media_player import play_media

        matrix = Matrix.__new__(Matrix)
        matrix.canvas = Mock()
        matrix.matrix = Mock(width=64, height=32)
        matrix.matrix.SwapOnVSync.return_value = matrix.canvas
        matrix._frame_capture = Mock()
        matrix._frame_capture.publish.return_value = 1
        matrix._physical_swap_observer = None
        matrix._publication_guard = lambda: False
        matrix.clear = Mock()
        matrix.draw_image = Mock()
        matrix._media_file = Mock(return_value=Mock())
        stop = threading.Event()

        with patch(
            "modules.media_player.gif_media_frames",
            return_value=[(Mock(), 0.0)],
        ):
            result = play_media(
                matrix, "clip.gif", 8, stop_event=stop,
            )

        self.assertFalse(result)
        matrix.matrix.SwapOnVSync.assert_not_called()
        matrix._frame_capture.publish.assert_not_called()

    def test_gif_playback_preserves_true_after_one_admitted_frame(self):
        from modules.media_player import play_media

        matrix = Matrix.__new__(Matrix)
        matrix.canvas = Mock()
        matrix.matrix = Mock(width=64, height=32)
        matrix.matrix.SwapOnVSync.return_value = matrix.canvas
        matrix._frame_capture = Mock()
        matrix._frame_capture.publish.return_value = 1
        matrix._physical_swap_observer = None
        admissions = iter((True, False))
        matrix._publication_guard = lambda: next(admissions)
        matrix.clear = Mock()
        matrix.draw_image = Mock()
        matrix._media_file = Mock(return_value=Mock())
        stop = threading.Event()

        with patch(
            "modules.media_player.gif_media_frames",
            return_value=[(Mock(), 0.0), (Mock(), 0.0)],
        ):
            result = play_media(
                matrix, "clip.gif", 8, stop_event=stop,
            )

        self.assertTrue(result)
        matrix.matrix.SwapOnVSync.assert_called_once_with(matrix.canvas)
        matrix._frame_capture.publish.assert_called_once_with(matrix.canvas)

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
        ), patch.object(framework.time, "monotonic", return_value=0.0), patch.object(
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

    def test_show_video_pre_cancelled_skips_media_lookup_and_decoder(self):
        matrix = Matrix.__new__(Matrix)
        matrix.matrix = Mock(width=64, height=32)
        matrix._media_file = Mock(return_value=Mock())
        matrix._video_frame_timeline = Mock(return_value=iter(()))
        stop = threading.Event()
        stop.set()

        self.assertFalse(matrix.show_video(
            "clip.mp4", loops=1, stop_event=stop, max_duration=10,
        ))

        matrix._media_file.assert_not_called()
        matrix._video_frame_timeline.assert_not_called()

    def test_show_video_cancelled_during_capture_update_is_not_published(self):
        matrix = Matrix.__new__(Matrix)
        matrix.canvas = Mock()
        matrix.matrix = Mock(width=64, height=32)
        matrix.matrix.SwapOnVSync.return_value = matrix.canvas
        matrix._frame_capture = Mock()
        matrix._frame_capture.publish.return_value = 1
        matrix._physical_swap_observer = None
        matrix._publication_guard = lambda: True
        matrix.clear = Mock()
        matrix._media_file = Mock(return_value=Mock())
        stop = threading.Event()
        matrix._frame_capture.set_image.side_effect = (
            lambda *_args: stop.set()
        )

        with patch(
            "modules.media_player.video_media_frames",
            return_value=[(Mock(), 0.1)],
        ):
            result = matrix.show_video(
                "clip.mp4", stop_event=stop, max_duration=10,
            )

        self.assertFalse(result)
        matrix.matrix.SwapOnVSync.assert_not_called()
        matrix._frame_capture.publish.assert_not_called()

    def test_video_playback_propagates_first_hardware_guard_rejection(self):
        from modules.media_player import play_media

        matrix = Matrix.__new__(Matrix)
        matrix.canvas = Mock()
        matrix.matrix = Mock(width=64, height=32)
        matrix.matrix.SwapOnVSync.return_value = matrix.canvas
        matrix._frame_capture = Mock()
        matrix._frame_capture.publish.return_value = 1
        matrix._physical_swap_observer = None
        matrix._publication_guard = lambda: False
        matrix.clear = Mock()
        matrix._media_file = Mock(return_value=Mock())
        stop = threading.Event()

        with patch(
            "modules.media_player.video_media_frames",
            return_value=[(Mock(), 0.0)],
        ):
            result = play_media(
                matrix, "clip.mp4", 8, stop_event=stop,
            )

        self.assertFalse(result)
        matrix.matrix.SwapOnVSync.assert_not_called()
        matrix._frame_capture.publish.assert_not_called()

    def test_video_playback_preserves_true_after_one_admitted_frame(self):
        from modules.media_player import play_media

        matrix = Matrix.__new__(Matrix)
        matrix.canvas = Mock()
        matrix.matrix = Mock(width=64, height=32)
        matrix.matrix.SwapOnVSync.return_value = matrix.canvas
        matrix._frame_capture = Mock()
        matrix._frame_capture.publish.return_value = 1
        matrix._physical_swap_observer = None
        admissions = iter((True, False))
        matrix._publication_guard = lambda: next(admissions)
        matrix.clear = Mock()
        matrix._media_file = Mock(return_value=Mock())
        stop = threading.Event()

        with patch(
            "modules.media_player.video_media_frames",
            return_value=[(Mock(), 0.0), (Mock(), 0.0)],
        ):
            result = play_media(
                matrix, "clip.mp4", 8, stop_event=stop,
            )

        self.assertTrue(result)
        matrix.matrix.SwapOnVSync.assert_called_once_with(matrix.canvas)
        matrix._frame_capture.publish.assert_called_once_with(matrix.canvas)

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
