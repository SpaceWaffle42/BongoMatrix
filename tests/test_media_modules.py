import threading
import unittest
from unittest.mock import Mock, patch

from modules import sleep_screen, splash_screen


class MediaModuleTests(unittest.TestCase):
    def test_splash_uses_configured_gif(self):
        matrix = Mock()
        matrix.show_gif.return_value = True
        result = splash_screen.run(matrix, {"media": {"splash": "squares_tb.gif"}}, duration=1)
        self.assertTrue(result)
        matrix.show_gif.assert_called_once_with("squares_tb.gif", loops=0, stop_event=None, max_duration=1)

    def test_sleep_uses_configured_gif(self):
        matrix = Mock()
        matrix.show_gif.return_value = True
        stop = threading.Event()
        result = sleep_screen.run(matrix, {"media": {"sleep": "sleep.gif"}}, duration=2, stop_event=stop)
        self.assertTrue(result)
        matrix.show_gif.assert_called_once_with("sleep.gif", loops=0, stop_event=stop, max_duration=2)

    def test_sleep_gif_speed_percentage_is_forwarded_as_playback_rate(self):
        matrix = Mock()
        matrix.show_gif.return_value = True
        settings = {
            "x": 0, "y": 0, "width": 64, "height": 32,
            "keep_aspect": True, "video_fps": 20, "gif_speed_percent": 250,
        }

        result = sleep_screen.run(
            matrix,
            {"media": {"sleep": "sleep.gif"}, "media_layout": {"sleep": settings}},
            duration=2,
        )

        self.assertTrue(result)
        matrix.show_gif.assert_called_once_with(
            "sleep.gif", x=0, y=0, width=64, height=32, keep_aspect=True,
            playback_rate=2.5, loops=0, stop_event=None, max_duration=2,
        )

    def test_mp4_uses_interruptible_video_playback(self):
        matrix = Mock()
        matrix.show_video.return_value = True
        stop = threading.Event()

        result = splash_screen.run(
            matrix,
            {"media": {"splash": "custom.mp4"}},
            duration=8,
            stop_event=stop,
        )

        self.assertTrue(result)
        matrix.show_video.assert_called_once_with(
            "custom.mp4", loops=0, stop_event=stop, max_duration=8
        )

    def test_media_layout_controls_position_size_aspect_and_video_rate(self):
        matrix = Mock()
        matrix.show_video.return_value = True
        stop = threading.Event()
        config = {
            "media": {"splash": "custom.mp4"},
            "media_layout": {"splash": {
                "x": 2, "y": 1, "width": 60, "height": 30,
                "keep_aspect": False, "video_fps": 24,
            }},
        }

        result = splash_screen.run(matrix, config, duration=8, stop_event=stop)

        self.assertTrue(result)
        matrix.show_video.assert_called_once_with(
            "custom.mp4", x=2, y=1, width=60, height=30,
            keep_aspect=False, fps_override=24, loops=0,
            stop_event=stop, max_duration=8,
        )

    def test_pre_cancelled_static_uploaded_image_is_not_published(self):
        matrix = Mock()
        matrix.show_image.return_value = True
        stop = threading.Event()
        stop.set()
        result = splash_screen.run(
            matrix,
            {"media": {"splash": "custom.png"}},
            duration=8,
            stop_event=stop,
        )
        self.assertFalse(result)
        matrix.show_image.assert_not_called()
        matrix.show_gif.assert_not_called()

    def test_pre_cancelled_static_sleep_image_is_not_published(self):
        matrix = Mock()
        matrix.show_image.return_value = True
        stop = threading.Event()
        stop.set()
        settings = {
            "x": 0, "y": 0, "width": 64, "height": 32,
            "keep_aspect": True, "video_fps": 20, "gif_speed_percent": 400,
        }

        result = sleep_screen.run(
            matrix,
            {"media": {"sleep": "custom.png"}, "media_layout": {"sleep": settings}},
            duration=8,
            stop_event=stop,
        )

        self.assertFalse(result)
        matrix.show_image.assert_not_called()
        matrix.show_gif.assert_not_called()


if __name__ == "__main__":
    unittest.main()
