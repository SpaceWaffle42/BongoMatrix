"""The 5001/5002 preview harness must serve real decoded media timelines.

Owner UAT on 5002 (2026-10-10, item timeline-gif-import) found every Advanced
Canvas media frame black: the synthetic controller was a bare Mock, so
/api/media-timeline answered 500 and the editor had nothing to draw.
"""

import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image


class PreviewMediaTimelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.playwright_rectify_preview import build

        cls.tmp = tempfile.TemporaryDirectory()
        cls.app, cls.cookie, _store = build(Path(cls.tmp.name), public_demo=True)
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def timeline(self, name, frame_count, fps):
        response = self.client.get(
            f"/api/media-timeline/{name}?frame_count={frame_count}&fps={fps}"
            "&playback_speed_percent=100.0"
        )
        self.assertEqual(
            (response.status_code, response.mimetype), (200, "image/png"),
            response.data[:200],
        )
        return Image.open(io.BytesIO(response.data)).convert("RGB")

    def test_gif_timeline_has_every_decoded_frame_in_order(self):
        sprite = self.timeline("SYNTHETIC_paged.gif", 6, 33)
        self.assertEqual(sprite.size, (64, 32 * 6))
        colours = [sprite.getpixel((5, 32 * index + 5)) for index in range(6)]
        self.assertEqual(colours, [(255, 0, 0), (0, 255, 0), (0, 0, 255)] * 2)

    def test_still_image_timeline_shows_the_image(self):
        sprite = self.timeline("SYNTHETIC_still.png", 1, 20)
        self.assertEqual(sprite.size, (64, 32))
        self.assertEqual(sprite.getpixel((5, 5)), (0x12, 0xAB, 0x34))

    def test_controller_still_never_touches_hardware(self):
        controller = self.app.extensions["offline_controller"]
        self.assertFalse(hasattr(controller.matrix, "canvas"))
        self.assertEqual((controller.matrix.matrix.width, controller.matrix.matrix.height), (64, 32))


if __name__ == "__main__":
    unittest.main()
