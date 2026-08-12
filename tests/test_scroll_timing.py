import datetime as dt
import json
import math
import subprocess
import unittest

from modules.announcement_board import announcement_timing_plan
from modules.scroll_timing import (
    duration_milliseconds,
    format_duration_seconds,
    frame_delay_for_seconds,
    scroll_frames,
    timing_javascript,
)


class ScrollTimingTests(unittest.TestCase):
    def test_inclusive_frames_and_exact_milliseconds_cover_directions_and_loops(self):
        for direction in ("left", "right"):
            with self.subTest(direction=direction):
                self.assertEqual(scroll_frames(17, matrix_width=64, direction=direction), 82)
        self.assertEqual(duration_milliseconds(82, 3, 0.055), 13530)

    def test_one_decimal_display_is_half_up_and_unavailable_is_not_zero(self):
        self.assertEqual(format_duration_seconds(1249), "1.2 s")
        self.assertEqual(format_duration_seconds(1250), "1.3 s")
        self.assertEqual(format_duration_seconds(None), "Unavailable")

    def test_inverse_rounds_half_up_to_one_microsecond_then_clamps(self):
        tied = frame_delay_for_seconds(1.25005, 100, 1)
        self.assertEqual(tied["applied_delay"], 0.012501)
        self.assertEqual(tied["achieved_seconds"], 1.25)
        self.assertEqual(tied["reason"], "rounded to the nearest 0.000001-second frame delay")
        minimum = frame_delay_for_seconds(0.1, 100, 2)
        maximum = frame_delay_for_seconds(999, 100, 2)
        self.assertEqual((minimum["applied_delay"], minimum["reason"]), (0.01, "clamped to minimum frame delay"))
        self.assertEqual((maximum["applied_delay"], maximum["reason"]), (1.0, "clamped to maximum frame delay"))

    def test_inverse_resolves_a_one_hundredth_second_runtime_edit(self):
        result = frame_delay_for_seconds(5.01, 100, 1)

        self.assertEqual(result["applied_delay"], 0.0501)
        self.assertEqual(result["achieved_seconds"], 5.01)
        self.assertIsNone(result["reason"])

    def test_inverse_rejects_booleans_non_finite_and_invalid_counts(self):
        for value in (True, False, math.nan, math.inf, -1, 0):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    frame_delay_for_seconds(value, 100, 1)
        for frames, loops in ((0, 1), (1, 0), (True, 1), (1, True)):
            with self.subTest(frames=frames, loops=loops):
                with self.assertRaises(ValueError):
                    frame_delay_for_seconds(1, frames, loops)

    def test_python_and_generated_javascript_vectors_are_identical(self):
        vectors = [
            {"seconds": 16.275, "frames": 70, "loops": 1},
            {"seconds": 1.25, "frames": 100, "loops": 1},
            {"seconds": 0.1, "frames": 100, "loops": 2},
            {"seconds": 999, "frames": 100, "loops": 2},
            {"seconds": 9.84, "frames": 82, "loops": 3},
        ]
        script = timing_javascript() + "\nconsole.log(JSON.stringify(" + json.dumps(vectors) + ".map(v=>frameDelayForSeconds(v.seconds,v.frames,v.loops))));"
        result = subprocess.run(["node", "-e", script], text=True, capture_output=True, check=True)
        javascript = json.loads(result.stdout)
        python = [frame_delay_for_seconds(item["seconds"], item["frames"], item["loops"]) for item in vectors]
        self.assertEqual(javascript, python)

    def test_python_and_javascript_match_systematic_positive_half_ties(self):
        vectors = [
            {"seconds": (step_index + 0.5) * 0.000001 * frames * loops,
             "frames": frames, "loops": loops}
            for frames in (65, 70, 82, 127, 255)
            for loops in (1, 2, 7, 10)
            for step_index in range(10000, 10200)
        ]
        script = timing_javascript() + "\nconsole.log(JSON.stringify(" + json.dumps(vectors) + ".map(v=>frameDelayForSeconds(v.seconds,v.frames,v.loops))));"
        result = subprocess.run(["node"], input=script, text=True, capture_output=True, check=True)
        javascript = json.loads(result.stdout)
        python = [frame_delay_for_seconds(item["seconds"], item["frames"], item["loops"]) for item in vectors]
        self.assertEqual(javascript, python)

    @staticmethod
    def announcement(mode, *, enabled=True, start="08:00", end="18:00"):
        timing = {"mode": "static", "duration_seconds": 8} if mode == "static" else {"mode": "scroll", "loops": 2, "frame_delay": 0.05}
        return {
            "enabled": enabled, "headline": "HELLO", "message": "WORLD",
            "style": {"font": "5x7"}, "timing": timing,
            "schedule": {"start_date": "2030-01-15", "end_date": "2030-01-15", "days": [1], "start_time": start, "end_time": end},
        }

    def test_announcement_plan_uses_fixed_eligibility_order_and_next_boundary(self):
        now = dt.datetime(2030, 1, 15, 9, 30)
        static = self.announcement("static")
        scrolling = self.announcement("scroll")
        disabled = self.announcement("static", enabled=False)
        plan = announcement_timing_plan([static, scrolling, disabled], now=now, text_width_provider=lambda _font, _text: 10)
        self.assertEqual(plan["status"], "fresh")
        self.assertEqual(plan["item_milliseconds"], [8000, 7500])
        self.assertEqual(plan["milliseconds"], 15500)
        self.assertEqual(plan["display"], "15.5 s")
        self.assertEqual(plan["refresh_at"], dt.datetime(2030, 1, 15, 9, 31))

    def test_announcement_plan_is_explicitly_unavailable_when_none_eligible(self):
        plan = announcement_timing_plan([self.announcement("static", enabled=False)], now=dt.datetime(2030, 1, 15, 9, 30))
        self.assertEqual(plan["status"], "unavailable")
        self.assertIsNone(plan["milliseconds"])
        self.assertEqual(plan["display"], "Unavailable")


if __name__ == "__main__":
    unittest.main()
