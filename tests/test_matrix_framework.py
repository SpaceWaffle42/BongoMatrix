import importlib
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

fake_graphics = types.SimpleNamespace(Color=lambda *rgb: types.SimpleNamespace(red=rgb[0], green=rgb[1], blue=rgb[2]), Font=Mock)
fake_rgb = types.SimpleNamespace(RGBMatrix=Mock, RGBMatrixOptions=Mock, graphics=fake_graphics)
fake_image = Mock()
fake_image.Resampling.LANCZOS = 1
fake_pil = types.SimpleNamespace(Image=fake_image, ImageDraw=Mock(), ImageSequence=Mock())
previous_modules = {name: sys.modules.get(name) for name in ("rgbmatrix", "PIL")}
sys.modules["rgbmatrix"] = fake_rgb
sys.modules["PIL"] = fake_pil
framework = importlib.import_module("matrix_framework")
for name, previous in previous_modules.items():
    if previous is None:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = previous
Matrix = framework.Matrix


class MatrixFrameworkTests(unittest.TestCase):
    def test_colour_rgb_accepts_html_hex_values(self):
        with patch("matrix_framework.graphics.Color", side_effect=lambda red, green, blue: (red, green, blue)):
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
            framework.Image, "frombytes", side_effect=images, create=True
        ):
            frames = matrix.media_frames("clip.mp4", frame_count=3, fps=12)

        self.assertEqual(frames, images)
        command = popen.call_args.args[0]
        self.assertIn("-stream_loop", command)
        video_filter = command[command.index("-vf") + 1]
        self.assertIn("scale=64:32", video_filter)
        self.assertIn("fps=12", video_filter)
        self.assertIn("pipe:0", command)
        self.assertEqual(popen.call_args.kwargs["stdin"], 9)
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
        process.stdout.read.side_effect = [frame, b""]
        process.wait.return_value = 0
        image = Mock()

        with patch.object(framework.os, "open", return_value=9), patch.object(
            framework.os, "lseek"
        ), patch.object(framework.os, "close"), patch.object(
            framework.subprocess, "Popen", return_value=process
        ), patch.object(
            framework.Image, "frombytes", return_value=image, create=True
        ), patch.object(framework.time, "sleep"):
            self.assertTrue(matrix.show_video("clip.mp4", loops=1))

        matrix.canvas.SetImage.assert_called_once_with(image, 0, 0)
        matrix._frame_capture.set_image.assert_called_once_with(matrix.canvas, image, 0, 0)
        matrix.swap.assert_called_once()

    def test_widget_scroll_is_mirrored_into_the_live_preview(self):
        matrix = Matrix.__new__(Matrix)
        canvas = Mock(width=64)
        font = Mock()
        colour = types.SimpleNamespace(red=255, green=179, blue=0)
        matrix.canvas = canvas
        matrix._frame_capture = Mock()
        matrix._capture_fonts = {id(font): "capture-font"}

        with patch("matrix_framework.graphics.DrawText", return_value=18, create=True):
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

        with patch("matrix_framework.graphics.DrawText", return_value=18, create=True):
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
