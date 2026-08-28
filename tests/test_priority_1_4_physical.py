import copy
import hashlib
import json
import threading
import unittest
from pathlib import Path

from frame_capture import BdfFont, FrameCapture


SOURCE_SHA = "1" * 40


class _Hardware:
    width = 64
    height = 32

    def CreateFrameCanvas(self):
        return object()

    def SwapOnVSync(self, _canvas):
        return object()


class CaptureMatrix:
    """Capture-aware Matrix contract double; no hardware access."""

    def __init__(self, width=64, height=32):
        self.matrix = _Hardware()
        self.matrix.width = width
        self.matrix.height = height
        self.capture = FrameCapture(width, height)
        self.matrix.CreateFrameCanvas = self.create_canvas
        self.font_value = object()
        self.bdf = BdfFont(Path(__file__).parents[1] / "resources/fonts/5x7.bdf")
        self.revision = 0

    def create_canvas(self):
        canvas = object()
        self.capture.register(canvas)
        return canvas

    def font(self, name):
        if name != "5x7":
            raise KeyError(name)
        return self.font_value

    @staticmethod
    def colour_rgb(value):
        return type("Colour", (), {
            "red": int(value[1:3], 16),
            "green": int(value[3:5], 16),
            "blue": int(value[5:7], 16),
        })()

    def clear(self, canvas):
        self.capture.clear(canvas)

    def fill(self, red, green, blue, canvas=None):
        self.capture.fill(canvas, red, green, blue)

    def draw_text(self, _font, x, y, colour, text, canvas=None):
        return self.capture.text(canvas, self.bdf, x, y, colour, text)

    def draw_line(self, x0, y0, x1, y1, colour, width=1, canvas=None):
        self.capture.line(canvas, x0, y0, x1, y1, colour)

    def swap(self, canvas=None):
        self.revision = self.capture.publish(canvas)
        return self.matrix.CreateFrameCanvas()

    def latest_frame_rgb(self):
        return self.capture.latest_rgb()


class Priority14PhysicalTests(unittest.TestCase):
    def test_exercise_report_proves_exact_static_and_ticker_frames_and_rejects_tampering(self):
        from scripts import verify_priority_1_4_physical as physical

        report = physical.exercise(CaptureMatrix(), SOURCE_SHA, deadline_seconds=5)
        physical.verify_report(report, SOURCE_SHA)

        self.assertEqual(set(report), {
            "version", "source_sha", "width", "height", "fixture_digest",
            "fixtures", "cancellation", "evidence_digest",
        })
        self.assertEqual((report["width"], report["height"]), (64, 32))
        fixtures = {item["name"]: item for item in report["fixtures"]}
        self.assertEqual(set(fixtures), {
            "static_none", "static_solid", "static_inset",
            "ticker_left_solid", "ticker_right_inset",
        })
        self.assertEqual({item["direction"] for item in fixtures.values()}, {"none", "left", "right"})
        self.assertEqual({item["border"] for item in fixtures.values()}, {"none", "solid", "inset"})
        for item in fixtures.values():
            self.assertEqual(item["frame_count"], len(item["frame_sha256"]))
            self.assertTrue(item["frame_sha256"])
            self.assertEqual(item["relative_revisions"], list(range(1, item["frame_count"] + 1)))
        self.assertEqual(fixtures["static_none"]["pixels"]["first"]["0,0"], "#123456")
        self.assertEqual(fixtures["static_solid"]["pixels"]["first"]["0,0"], "#abcdef")
        self.assertEqual(fixtures["static_inset"]["pixels"]["first"]["1,1"], "#abcdef")
        self.assertEqual(fixtures["static_inset"]["pixels"]["first"]["0,0"], "#123456")
        self.assertTrue(report["cancellation"]["attempted"])
        self.assertFalse(report["cancellation"]["published"])
        self.assertEqual(
            report["cancellation"]["revision_before"],
            report["cancellation"]["revision_after"],
        )

        for mutate in (
            lambda value: value.update(width=32),
            lambda value: value["fixtures"][0]["frame_sha256"].__setitem__(0, "0" * 64),
            lambda value: value["fixtures"][0]["pixels"]["first"].update({"0,0": "#000000"}),
            lambda value: value["fixtures"].append(copy.deepcopy(value["fixtures"][0])),
            lambda value: value["cancellation"].update(published=True),
        ):
            tampered = copy.deepcopy(report)
            mutate(tampered)
            with self.assertRaises(ValueError):
                physical.verify_report(tampered, SOURCE_SHA)

    def test_verify_report_rejects_forged_frame_hash_even_with_recomputed_self_digest(self):
        from scripts import verify_priority_1_4_physical as physical

        report = physical.exercise(CaptureMatrix(), SOURCE_SHA, deadline_seconds=5)
        forged = copy.deepcopy(report)
        ticker = next(item for item in forged["fixtures"] if item["name"] == "ticker_left_solid")
        ticker["frame_sha256"][17] = "0" * 64
        forged.pop("evidence_digest")
        forged["evidence_digest"] = hashlib.sha256(physical._canonical(forged)).hexdigest()

        with self.assertRaisesRegex(ValueError, "frame hash"):
            physical.verify_report(forged, SOURCE_SHA)

    def test_exercise_is_dimension_bound_and_cancellable_before_publication(self):
        from scripts import verify_priority_1_4_physical as physical

        with self.assertRaisesRegex(ValueError, "64x32"):
            physical.exercise(CaptureMatrix(width=32), SOURCE_SHA)
        stopped = threading.Event()
        stopped.set()
        matrix = CaptureMatrix()
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            physical.exercise(matrix, SOURCE_SHA, stop_event=stopped)
        self.assertEqual(matrix.latest_frame_rgb()[1], 0)
        for deadline in (False, 0, 31):
            with self.assertRaises(ValueError):
                physical.exercise(CaptureMatrix(), SOURCE_SHA, deadline_seconds=deadline)

    def test_verify_report_requires_bounded_canonical_json_and_exact_cli(self):
        from scripts import verify_priority_1_4_physical as physical

        report = physical.exercise(CaptureMatrix(), SOURCE_SHA)
        canonical = physical._canonical(report)
        self.assertEqual(physical.verify_report(canonical, SOURCE_SHA), report)
        with self.assertRaisesRegex(ValueError, "canonical"):
            physical.verify_report(json.dumps(report).encode(), SOURCE_SHA)
        with self.assertRaisesRegex(ValueError, "oversized"):
            physical.verify_report(b" " * (256 * 1024 + 1), SOURCE_SHA)
        parser = physical.build_parser()
        self.assertEqual(
            parser.parse_args([
                "exercise", "--source-sha", SOURCE_SHA, "--width", "64",
                "--height", "32",
            ]).command,
            "exercise",
        )
        self.assertEqual(
            parser.parse_args([
                "verify-report", "--source-sha", SOURCE_SHA, "--report", "/tmp/report",
            ]).command,
            "verify-report",
        )


if __name__ == "__main__":
    unittest.main()
