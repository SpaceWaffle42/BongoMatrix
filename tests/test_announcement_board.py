import datetime as dt
import unittest
from unittest.mock import Mock

from modules.announcement_board import active_announcements, run


class AnnouncementBoardTests(unittest.TestCase):
    @staticmethod
    def item(item_id, start_time, end_time):
        return {
            "id": item_id,
            "layout": "text_notice",
            "headline": "Welcome",
            "message": "Front desk closes at 5 PM",
            "media": None,
            "schedule": {
                "start_date": "2030-01-15",
                "end_date": "2030-01-15",
                "start_time": start_time,
                "end_time": end_time,
            },
            "style": {
                "foreground": "#ffffff",
                "background": "#000000",
                "font": "5x7",
                "alignment": "left",
                "border": "none",
                "border_colour": "#ffffff",
            },
            "timing": {"mode": "static", "duration_seconds": 8},
            "transition": {"type": "cut"},
        }

    def test_daytime_window_is_start_inclusive_end_exclusive_and_preserves_order(self):
        announcements = [
            self.item("announcement_second", "08:00", "18:00"),
            self.item("announcement_first", "09:00", "17:00"),
        ]

        at_start = active_announcements(
            announcements, now=dt.datetime(2030, 1, 15, 9, 0),
        )
        at_end = active_announcements(
            announcements, now=dt.datetime(2030, 1, 15, 17, 0),
        )

        self.assertEqual(
            [item["id"] for item in at_start],
            ["announcement_second", "announcement_first"],
        )
        self.assertEqual(
            [item["id"] for item in at_end],
            ["announcement_second"],
        )
    def test_overnight_range_uses_eligible_start_dates_and_extends_final_date_into_morning(self):
        announcement = self.item("announcement_overnight", "22:00", "02:00")
        announcement["schedule"]["end_date"] = "2030-01-16"

        active_times = [
            dt.datetime(2030, 1, 15, 22, 0),
            dt.datetime(2030, 1, 16, 1, 59),
            dt.datetime(2030, 1, 16, 22, 0),
            dt.datetime(2030, 1, 17, 1, 59),
        ]
        inactive_times = [
            dt.datetime(2030, 1, 15, 1, 59),
            dt.datetime(2030, 1, 17, 2, 0),
        ]

        for now in active_times:
            with self.subTest(now=now):
                self.assertEqual(active_announcements([announcement], now=now), [announcement])
        for now in inactive_times:
            with self.subTest(now=now):
                self.assertEqual(active_announcements([announcement], now=now), [])
    def test_no_active_announcement_yields_without_touching_matrix(self):
        matrix = Mock()
        config = {
            "announcement_board": {
                "announcements": [self.item("announcement_future", "09:00", "17:00")],
            },
        }

        result = run(
            matrix,
            config,
            duration=0,
            now_provider=lambda: dt.datetime(2030, 1, 14, 12, 0),
        )

        self.assertFalse(result)
        matrix.assert_not_called()
        self.assertEqual(matrix.mock_calls, [])
    def test_active_static_text_notice_renders_one_frame_and_waits_its_own_duration(self):
        matrix = Mock()
        matrix.matrix.CreateFrameCanvas.return_value = "canvas"
        matrix.font.side_effect = lambda name: f"font:{name}"
        matrix.colour_rgb.side_effect = lambda value: {
            "#ffffff": Mock(red=255, green=255, blue=255),
            "#000000": Mock(red=0, green=0, blue=0),
        }[value]
        matrix.swap.return_value = "canvas"
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        announcement = self.item("announcement_active", "09:00", "17:00")

        result = run(
            matrix,
            {"announcement_board": {"announcements": [announcement]}},
            duration=0,
            stop_event=stop,
            now_provider=lambda: dt.datetime(2030, 1, 15, 12, 0),
        )

        self.assertTrue(result)
        matrix.fill.assert_called_once_with(0, 0, 0, canvas="canvas")
        self.assertEqual(
            [(entry.args[0], entry.args[4]) for entry in matrix.draw_text.call_args_list],
            [("font:5x7", "Welcome"), ("font:4x6", "Front desk closes at 5 PM")],
        )
        matrix.swap.assert_called_once_with("canvas")
        stop.wait.assert_called_once_with(8)


if __name__ == "__main__":
    unittest.main()
