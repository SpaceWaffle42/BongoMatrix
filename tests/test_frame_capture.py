import tempfile
import threading
import unittest
from pathlib import Path

from PIL import Image

from frame_capture import BdfFont, FrameCapture
from matrix_framework import Matrix


class FakeColour:
    def __init__(self, red, green, blue):
        self.red = red
        self.green = green
        self.blue = blue


class FrameCaptureTests(unittest.TestCase):
    def test_published_frame_contains_the_exact_rendered_pixels(self):
        capture = FrameCapture(4, 2)
        canvas = object()
        capture.register(canvas)
        capture.fill(canvas, 1, 2, 3)
        capture.pixel(canvas, 2, 1, 40, 50, 60)

        revision = capture.publish(canvas)
        image = Image.open(capture.png())

        self.assertEqual(revision, 1)
        self.assertEqual(image.size, (4, 2))
        self.assertEqual(image.getpixel((0, 0)), (1, 2, 3))
        self.assertEqual(image.getpixel((2, 1)), (40, 50, 60))

    def test_set_image_clips_at_the_matrix_edges(self):
        capture = FrameCapture(3, 2)
        canvas = object()
        capture.register(canvas)
        source = Image.new("RGB", (3, 2), (9, 8, 7))

        capture.set_image(canvas, source, 2, 1)
        capture.publish(canvas)
        image = Image.open(capture.png())

        self.assertEqual(image.getpixel((2, 1)), (9, 8, 7))
        self.assertEqual(image.getpixel((1, 1)), (0, 0, 0))

    def test_bdf_text_uses_the_same_baseline_and_advance_as_matrix_graphics(self):
        font = BdfFont(Path(__file__).parents[1] / "resources" / "fonts" / "5x7.bdf")
        capture = FrameCapture(8, 8)
        canvas = object()
        capture.register(canvas)

        advance = capture.text(canvas, font, 1, 6, FakeColour(255, 0, 0), "!")
        capture.publish(canvas)
        image = Image.open(capture.png())

        self.assertEqual(advance, 6)
        self.assertEqual(image.getpixel((3, 0)), (255, 0, 0))
        self.assertEqual(image.getpixel((3, 4)), (0, 0, 0))
        self.assertEqual(image.getpixel((3, 5)), (255, 0, 0))


    def test_stale_device_draft_is_guarded_immediately_before_matrix_swap(self):
        class FakeHardware:
            width = 4
            height = 2

            def __init__(self):
                self.submissions = []

            def SwapOnVSync(self, canvas):
                self.submissions.append(canvas)
                return object()

        submitted = object()
        hardware = FakeHardware()
        matrix = Matrix.__new__(Matrix)
        matrix.matrix = hardware
        matrix.canvas = submitted
        matrix._frame_capture = FrameCapture(4, 2)
        matrix._frame_capture.register(submitted)
        matrix._frame_capture.fill(submitted, 9, 8, 7)

        guard_started = threading.Event()
        release_guard = threading.Event()
        authority = [True]

        def publication_guard():
            guard_started.set()
            self.assertTrue(release_guard.wait(1))
            return authority[0]

        matrix.set_publication_guard(publication_guard)
        result = []
        worker = threading.Thread(target=lambda: result.append(matrix.swap()))
        worker.start()
        self.assertTrue(guard_started.wait(0.5))
        authority[0] = False
        release_guard.set()
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [submitted])
        self.assertEqual(hardware.submissions, [])
        self.assertEqual(matrix._frame_capture.revision, 0)

        matrix.set_publication_guard(None)
        matrix.swap()
        self.assertEqual(hardware.submissions, [submitted])
        self.assertEqual(matrix._frame_capture.revision, 1)


if __name__ == "__main__":
    unittest.main()