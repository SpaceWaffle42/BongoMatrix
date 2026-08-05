import datetime as dt
import unittest
from unittest.mock import Mock

from PIL import Image

import modules.announcement_board as announcement_board
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
    def test_disabled_announcement_is_never_active(self):
        announcement = self.item("announcement_disabled", "08:00", "18:00")
        announcement["enabled"] = False

        self.assertEqual(
            active_announcements([announcement], now=dt.datetime(2030, 1, 15, 9, 0)),
            [],
        )

    def test_weekdays_filter_daytime_and_use_owning_date_for_overnight(self):
        daytime = self.item("announcement_daytime", "09:00", "17:00")
        daytime["schedule"]["start_date"] = "2030-01-14"
        daytime["schedule"]["days"] = [0]
        self.assertEqual(active_announcements([daytime], now=dt.datetime(2030, 1, 15, 10, 0)), [])
        self.assertEqual(active_announcements([daytime], now=dt.datetime(2030, 1, 14, 10, 0)), [daytime])

        overnight = self.item("announcement_overnight_day", "22:00", "02:00")
        overnight["schedule"]["start_date"] = "2030-01-14"
        overnight["schedule"]["days"] = [0]
        self.assertEqual(active_announcements([overnight], now=dt.datetime(2030, 1, 15, 1, 0)), [overnight])

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
    def test_rechecks_eligibility_before_each_record_starts(self):
        matrix = Mock()
        matrix.matrix.width = 64
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
        first = self.item("announcement_first", "09:00", "10:00")
        second = self.item("announcement_expired", "09:00", "09:01")
        moments = iter([
            dt.datetime(2030, 1, 15, 9, 0),
            dt.datetime(2030, 1, 15, 9, 2),
        ])

        self.assertTrue(run(
            matrix,
            {"announcement_board": {"announcements": [first, second]}},
            duration=0,
            stop_event=stop,
            now_provider=lambda: next(moments),
        ))

        matrix.swap.assert_called_once_with("canvas")
        stop.wait.assert_called_once_with(8)

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
    def test_preview_renders_disabled_out_of_schedule_draft_without_mutating_it(self):
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
        draft = self.item("announcement_preview", "09:00", "17:00")
        draft["enabled"] = False
        draft["schedule"]["start_date"] = "2099-01-01"
        draft["schedule"]["end_date"] = "2099-01-01"
        expected = dict(draft)

        result = announcement_board.run_preview(matrix, draft, stop_event=stop)

        self.assertTrue(result)
        self.assertEqual(draft, expected)
        matrix.swap.assert_called_once_with("canvas")
        stop.wait.assert_called_once_with(8)

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
    def test_scrolling_text_notice_completes_saved_loops_using_only_frame_delay(self):
        matrix = Mock()
        matrix.matrix.width = 3
        matrix.matrix.CreateFrameCanvas.return_value = "canvas"
        matrix.font.return_value = "font:5x7"
        matrix.colour_rgb.side_effect = lambda value: {
            "#ffffff": Mock(red=255, green=255, blue=255),
            "#000000": Mock(red=0, green=0, blue=0),
        }[value]
        matrix.draw_text.return_value = 2
        matrix.swap.return_value = "canvas"
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        announcement = self.item("announcement_scroll", "09:00", "17:00")
        announcement["message"] = ""
        announcement["timing"] = {"mode": "scroll", "loops": 2, "frame_delay": 0.05}

        result = run(
            matrix,
            {"announcement_board": {"announcements": [announcement]}},
            duration=0.001,
            stop_event=stop,
            now_provider=lambda: dt.datetime(2030, 1, 15, 12, 0),
        )

        self.assertTrue(result)
        self.assertEqual(
            [entry.args[1] for entry in matrix.draw_text.call_args_list],
            [3, 2, 1, 0, -1, -2, 3, 2, 1, 0, -1, -2],
        )
        self.assertEqual(matrix.swap.call_count, 12)
        self.assertEqual([entry.args[0] for entry in stop.wait.call_args_list], [0.05] * 12)
    def test_centered_static_notice_draws_saved_solid_border(self):
        matrix = Mock()
        matrix.matrix.width = 64
        matrix.matrix.height = 32
        matrix.matrix.CreateFrameCanvas.return_value = "canvas"
        matrix.font.return_value = "font:5x7"
        white = Mock(red=255, green=255, blue=255)
        black = Mock(red=0, green=0, blue=0)
        matrix.colour_rgb.side_effect = lambda value: {"#ffffff": white, "#000000": black}[value]
        matrix.draw_text.return_value = 20
        matrix.swap.return_value = "canvas"
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        announcement = self.item("announcement_centered", "09:00", "17:00")
        announcement["message"] = ""
        announcement["style"]["alignment"] = "center"
        announcement["style"]["border"] = "solid"

        result = run(
            matrix,
            {"announcement_board": {"announcements": [announcement]}},
            duration=0,
            stop_event=stop,
            now_provider=lambda: dt.datetime(2030, 1, 15, 12, 0),
        )

        self.assertTrue(result)
        self.assertEqual(matrix.draw_text.call_args_list[-1].args[1], 22)
        self.assertEqual(matrix.draw_line.call_count, 4)
        self.assertEqual(
            [entry.args[:4] for entry in matrix.draw_line.call_args_list],
            [(0, 0, 63, 0), (63, 0, 63, 31), (63, 31, 0, 31), (0, 31, 0, 0)],
        )
    def test_right_aligned_static_notice_draws_inset_border(self):
        matrix = Mock()
        matrix.matrix.width = 64
        matrix.matrix.height = 32
        matrix.matrix.CreateFrameCanvas.return_value = "canvas"
        matrix.font.return_value = "font:5x7"
        white = Mock(red=255, green=255, blue=255)
        black = Mock(red=0, green=0, blue=0)
        matrix.colour_rgb.side_effect = lambda value: {"#ffffff": white, "#000000": black}[value]
        matrix.draw_text.return_value = 20
        matrix.swap.return_value = "canvas"
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        announcement = self.item("announcement_right", "09:00", "17:00")
        announcement["message"] = ""
        announcement["style"]["alignment"] = "right"
        announcement["style"]["border"] = "inset"

        self.assertTrue(run(
            matrix,
            {"announcement_board": {"announcements": [announcement]}},
            duration=0,
            stop_event=stop,
            now_provider=lambda: dt.datetime(2030, 1, 15, 12, 0),
        ))

        self.assertEqual(matrix.draw_text.call_args_list[-1].args[1], 42)
        self.assertEqual(
            [entry.args[:4] for entry in matrix.draw_line.call_args_list],
            [(1, 1, 62, 1), (62, 1, 62, 30), (62, 30, 1, 30), (1, 30, 1, 1)],
        )
    def test_static_icon_notice_draws_media_library_frame_beside_text(self):
        matrix = Mock()
        matrix.matrix.width = 64
        matrix.matrix.height = 32
        matrix.matrix.CreateFrameCanvas.return_value = "canvas"
        matrix.font.side_effect = lambda name: f"font:{name}"
        matrix.colour_rgb.side_effect = lambda value: {
            "#ffffff": Mock(red=255, green=255, blue=255),
            "#000000": Mock(red=0, green=0, blue=0),
        }[value]
        matrix.media_frames.return_value = ["icon-frame"]
        matrix.swap.return_value = "canvas"
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        announcement = self.item("announcement_icon", "09:00", "17:00")
        announcement["layout"] = "icon_notice"
        announcement["media"] = "events/open-day.png"

        self.assertTrue(run(
            matrix,
            {"announcement_board": {"announcements": [announcement]}},
            duration=0,
            stop_event=stop,
            now_provider=lambda: dt.datetime(2030, 1, 15, 12, 0),
        ))

        matrix.media_frames.assert_called_once_with(
            "events/open-day.png", 1, 1, stop_event=stop,
        )
        matrix.draw_image.assert_called_once_with(
            "icon-frame", 1, 1, width=14, height=14, canvas="canvas",
        )
        self.assertEqual([call.args[1] for call in matrix.draw_text.call_args_list], [18, 18])
        stop.wait.assert_called_once_with(8)
    def test_removed_icon_media_renders_visible_fallback_without_crashing(self):
        matrix = Mock()
        matrix.matrix.width = 64
        matrix.matrix.height = 32
        matrix.matrix.CreateFrameCanvas.return_value = "canvas"
        matrix.font.side_effect = lambda name: f"font:{name}"
        matrix.colour_rgb.side_effect = lambda value: {
            "#ffffff": Mock(red=255, green=255, blue=255),
            "#000000": Mock(red=0, green=0, blue=0),
        }[value]
        matrix.media_frames.return_value = []
        matrix.swap.return_value = "canvas"
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        announcement = self.item("announcement_missing", "09:00", "17:00")
        announcement["layout"] = "icon_notice"
        announcement["media"] = "removed.png"

        self.assertTrue(run(
            matrix,
            {"announcement_board": {"announcements": [announcement]}},
            duration=0,
            stop_event=stop,
            now_provider=lambda: dt.datetime(2030, 1, 15, 12, 0),
        ))

        matrix.draw_image.assert_not_called()
        self.assertIn("Media unavailable", [call.args[4] for call in matrix.draw_text.call_args_list])
        matrix.swap.assert_called_once_with("canvas")
        stop.wait.assert_called_once_with(8)
    def test_static_image_banner_draws_media_above_headline(self):
        matrix = Mock()
        matrix.matrix.width = 64
        matrix.matrix.height = 32
        matrix.matrix.CreateFrameCanvas.return_value = "canvas"
        matrix.font.return_value = "font:5x7"
        white = Mock(red=255, green=255, blue=255)
        black = Mock(red=0, green=0, blue=0)
        matrix.colour_rgb.side_effect = lambda value: {
            "#ffffff": white,
            "#000000": black,
        }[value]
        matrix.media_frames.return_value = ["banner-frame"]
        matrix.swap.return_value = "canvas"
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        announcement = self.item("announcement_banner", "09:00", "17:00")
        announcement["layout"] = "image_banner"
        announcement["media"] = "promotions/sale.png"
        announcement["message"] = ""

        self.assertTrue(run(
            matrix,
            {"announcement_board": {"announcements": [announcement]}},
            duration=0,
            stop_event=stop,
            now_provider=lambda: dt.datetime(2030, 1, 15, 12, 0),
        ))

        matrix.draw_image.assert_called_once_with(
            "banner-frame", 0, 0, width=64, height=16,
            keep_aspect=False, canvas="canvas",
        )
        self.assertEqual(matrix.draw_text.call_args.args[1:5], (2, 24, white, "Welcome"))
        stop.wait.assert_called_once_with(8)
    def test_static_full_image_notice_fills_panel_and_overlays_headline(self):
        matrix = Mock()
        matrix.matrix.width = 64
        matrix.matrix.height = 32
        matrix.matrix.CreateFrameCanvas.return_value = "canvas"
        matrix.font.return_value = "font:5x7"
        white = Mock(red=255, green=255, blue=255)
        black = Mock(red=0, green=0, blue=0)
        matrix.colour_rgb.side_effect = lambda value: {"#ffffff": white, "#000000": black}[value]
        matrix.media_frames.return_value = ["full-frame"]
        matrix.swap.return_value = "canvas"
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        announcement = self.item("announcement_full", "09:00", "17:00")
        announcement["layout"] = "full_image_notice"
        announcement["media"] = "events/keynote.jpg"
        announcement["message"] = ""

        self.assertTrue(run(
            matrix,
            {"announcement_board": {"announcements": [announcement]}},
            duration=0,
            stop_event=stop,
            now_provider=lambda: dt.datetime(2030, 1, 15, 12, 0),
        ))

        matrix.draw_image.assert_called_once_with(
            "full-frame", 0, 0, width=64, height=32,
            keep_aspect=False, canvas="canvas",
        )
        self.assertEqual(matrix.draw_text.call_args.args[1:5], (2, 30, white, "Welcome"))
        stop.wait.assert_called_once_with(8)
    def test_fade_transition_submits_four_brightness_steps_before_static_hold(self):
        matrix = Mock()
        matrix.matrix.width = 64
        matrix.matrix.height = 32
        matrix.matrix.CreateFrameCanvas.return_value = "canvas"
        matrix.font.return_value = "font:5x7"

        def colour(value):
            return Mock(
                red=int(value[1:3], 16),
                green=int(value[3:5], 16),
                blue=int(value[5:7], 16),
            )

        matrix.colour_rgb.side_effect = colour
        matrix.swap.return_value = "canvas"
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        announcement = self.item("announcement_fade", "09:00", "17:00")
        announcement["message"] = ""
        announcement["transition"] = {"type": "fade", "duration_seconds": 0.4}

        self.assertTrue(run(
            matrix,
            {"announcement_board": {"announcements": [announcement]}},
            duration=0,
            stop_event=stop,
            now_provider=lambda: dt.datetime(2030, 1, 15, 12, 0),
        ))

        self.assertEqual(
            [call.args[3].red for call in matrix.draw_text.call_args_list],
            [64, 128, 191, 255],
        )
        self.assertEqual(matrix.swap.call_count, 4)
        self.assertEqual(
            [call.args[0] for call in stop.wait.call_args_list],
            [0.1, 0.1, 0.1, 0.1, 8],
        )
    def test_slide_left_transition_moves_notice_in_four_steps_before_hold(self):
        matrix = Mock()
        matrix.matrix.width = 64
        matrix.matrix.height = 32
        matrix.matrix.CreateFrameCanvas.return_value = "canvas"
        matrix.font.return_value = "font:5x7"
        matrix.colour_rgb.side_effect = lambda value: {
            "#ffffff": Mock(red=255, green=255, blue=255),
            "#000000": Mock(red=0, green=0, blue=0),
        }[value]
        matrix.swap.return_value = "canvas"
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        announcement = self.item("announcement_slide", "09:00", "17:00")
        announcement["message"] = ""
        announcement["transition"] = {
            "type": "slide", "direction": "left", "duration_seconds": 0.4,
        }

        self.assertTrue(run(
            matrix,
            {"announcement_board": {"announcements": [announcement]}},
            duration=0,
            stop_event=stop,
            now_provider=lambda: dt.datetime(2030, 1, 15, 12, 0),
        ))

        self.assertEqual([call.args[1] for call in matrix.draw_text.call_args_list], [50, 34, 18, 2])
        self.assertEqual(matrix.swap.call_count, 4)
        self.assertEqual(
            [call.args[0] for call in stop.wait.call_args_list],
            [0.1, 0.1, 0.1, 0.1, 8],
        )
    def test_wipe_left_transition_reveals_four_column_bands_before_hold(self):
        matrix = Mock()
        matrix.matrix.width = 64
        matrix.matrix.height = 32
        matrix.matrix.CreateFrameCanvas.return_value = "canvas"
        matrix.font.return_value = "font:5x7"
        white = Mock(red=255, green=255, blue=255)
        black = Mock(red=0, green=0, blue=0)
        matrix.colour_rgb.side_effect = lambda value: {"#ffffff": white, "#000000": black}[value]
        matrix.swap.return_value = "canvas"
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        announcement = self.item("announcement_wipe", "09:00", "17:00")
        announcement["message"] = ""
        announcement["transition"] = {
            "type": "wipe", "direction": "left", "duration_seconds": 0.4,
        }

        self.assertTrue(run(
            matrix,
            {"announcement_board": {"announcements": [announcement]}},
            duration=0,
            stop_event=stop,
            now_provider=lambda: dt.datetime(2030, 1, 15, 12, 0),
        ))

        self.assertEqual(
            [call.args[0] for call in matrix.draw_line.call_args_list],
            list(range(16, 64)) + list(range(32, 64)) + list(range(48, 64)),
        )
        self.assertTrue(all(call.args[1:4] == (0, call.args[0], 31) for call in matrix.draw_line.call_args_list))
        self.assertTrue(all(call.args[4] is black for call in matrix.draw_line.call_args_list))
        self.assertEqual(matrix.swap.call_count, 4)
        self.assertEqual([call.args[0] for call in stop.wait.call_args_list], [0.1] * 4 + [8])
    def test_pixel_dissolve_order_is_stable_complete_and_id_specific(self):
        first = announcement_board.dissolve_pixel_order("announcement_one", 4, 2)
        repeated = announcement_board.dissolve_pixel_order("announcement_one", 4, 2)
        other = announcement_board.dissolve_pixel_order("announcement_two", 4, 2)

        self.assertEqual(first, repeated)
        self.assertEqual(set(first), {(x, y) for y in range(2) for x in range(4)})
        self.assertEqual(len(first), 8)
        self.assertNotEqual(first, other)
    def test_pixel_dissolve_masks_unrevealed_suffix_in_four_stable_steps(self):
        matrix = Mock()
        matrix.matrix.width = 4
        matrix.matrix.height = 2
        matrix.matrix.CreateFrameCanvas.return_value = "canvas"
        matrix.font.return_value = "font:5x7"
        white = Mock(red=255, green=255, blue=255)
        black = Mock(red=0, green=0, blue=0)
        matrix.colour_rgb.side_effect = lambda value: {"#ffffff": white, "#000000": black}[value]
        matrix.swap.return_value = "canvas"
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        announcement = self.item("announcement_dissolve", "09:00", "17:00")
        announcement["message"] = ""
        announcement["transition"] = {
            "type": "pixel_dissolve", "duration_seconds": 0.4,
        }
        order = announcement_board.dissolve_pixel_order("announcement_dissolve", 4, 2)

        self.assertTrue(run(
            matrix,
            {"announcement_board": {"announcements": [announcement]}},
            duration=0,
            stop_event=stop,
            now_provider=lambda: dt.datetime(2030, 1, 15, 12, 0),
        ))

        expected_hidden = order[2:] + order[4:] + order[6:]
        self.assertEqual(
            [call.args[:2] for call in matrix.pixel.call_args_list],
            expected_hidden,
        )
        self.assertTrue(all(call.args[2:5] == (0, 0, 0) for call in matrix.pixel.call_args_list))
        self.assertEqual(matrix.swap.call_count, 4)
        self.assertEqual([call.args[0] for call in stop.wait.call_args_list], [0.1] * 4 + [8])
    def test_fade_transition_dims_icon_pixels_as_well_as_text(self):
        matrix = Mock()
        matrix.matrix.width = 64
        matrix.matrix.height = 32
        matrix.matrix.CreateFrameCanvas.return_value = "canvas"
        matrix.font.return_value = "font:5x7"
        matrix.colour_rgb.side_effect = lambda value: Mock(
            red=int(value[1:3], 16), green=int(value[3:5], 16), blue=int(value[5:7], 16),
        )
        matrix.media_frames.return_value = [Image.new("RGB", (1, 1), "white")]
        matrix.swap.return_value = "canvas"
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        announcement = self.item("announcement_icon_fade", "09:00", "17:00")
        announcement["layout"] = "icon_notice"
        announcement["media"] = "icon.png"
        announcement["message"] = ""
        announcement["transition"] = {"type": "fade", "duration_seconds": 0.4}

        self.assertTrue(run(
            matrix,
            {"announcement_board": {"announcements": [announcement]}},
            duration=0,
            stop_event=stop,
            now_provider=lambda: dt.datetime(2030, 1, 15, 12, 0),
        ))

        self.assertEqual(
            [call.args[0].getpixel((0, 0))[0] for call in matrix.draw_image.call_args_list],
            [64, 128, 191, 255],
        )
        self.assertEqual([call.args[3].red for call in matrix.draw_text.call_args_list], [64, 128, 191, 255])
    def test_slide_transition_moves_icon_and_text_together(self):
        matrix = Mock()
        matrix.matrix.width = 64
        matrix.matrix.height = 32
        matrix.matrix.CreateFrameCanvas.return_value = "canvas"
        matrix.font.return_value = "font:5x7"
        matrix.colour_rgb.side_effect = lambda value: Mock(
            red=int(value[1:3], 16), green=int(value[3:5], 16), blue=int(value[5:7], 16),
        )
        matrix.media_frames.return_value = ["icon-frame"]
        matrix.swap.return_value = "canvas"
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        announcement = self.item("announcement_icon_slide", "09:00", "17:00")
        announcement["layout"] = "icon_notice"
        announcement["media"] = "icon.png"
        announcement["message"] = ""
        announcement["transition"] = {
            "type": "slide", "direction": "left", "duration_seconds": 0.4,
        }

        self.assertTrue(run(
            matrix,
            {"announcement_board": {"announcements": [announcement]}},
            duration=0,
            stop_event=stop,
            now_provider=lambda: dt.datetime(2030, 1, 15, 12, 0),
        ))

        self.assertEqual([call.args[1] for call in matrix.draw_image.call_args_list], [49, 33, 17, 1])
        self.assertEqual([call.args[1] for call in matrix.draw_text.call_args_list], [66, 50, 34, 18])


if __name__ == "__main__":
    unittest.main()
