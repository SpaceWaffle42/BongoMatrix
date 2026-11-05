"""Priority 1.11: system-wide Screen Safe Area (owner decisions 2026-10-30)."""
import copy
import datetime as dt
import hashlib
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import matrix_framework
from config_store import ConfigStore, DEFAULT_CONFIG, validate_config
from edit_preview import CaptureMatrix
from frame_capture import BdfFont
from safe_area import SafeArea


class _FakeCanvas:
    """Hardware-like canvas: clips to the physical panel only, like rgbmatrix."""

    def __init__(self, width=64, height=32):
        self.width, self.height = width, height
        self.image = Image.new("RGB", (width, height), "black")

    def SetPixel(self, x, y, r, g, b):
        if 0 <= x < self.width and 0 <= y < self.height:
            self.image.putpixel((int(x), int(y)), (r, g, b))

    def SetImage(self, image, x=0, y=0, unsafe=True):
        self.image.paste(image.convert("RGB"), (int(x), int(y)))

    def Fill(self, r, g, b):
        self.image.paste((r, g, b), (0, 0, self.width, self.height))

    def Clear(self):
        self.Fill(0, 0, 0)


class _FakeRGBMatrix:
    def __init__(self, options=None):
        self.width, self.height, self.brightness = 64, 32, 100
        self.shown = None

    def CreateFrameCanvas(self):
        return _FakeCanvas()

    def SwapOnVSync(self, canvas):
        self.shown = canvas.image.copy()
        return _FakeCanvas()


class _FakeFont:
    def LoadFont(self, path):
        self.bdf = BdfFont(path)


def _draw_text(canvas, font, x, y, colour, text):
    advance, points = font.bdf.text_pixels(x, y, text)
    for px, py in points:
        canvas.SetPixel(px, py, colour.red, colour.green, colour.blue)
    return advance


def _draw_line(canvas, x0, y0, x1, y1, colour):
    steps = max(abs(x1 - x0), abs(y1 - y0), 1)
    for index in range(steps + 1):
        canvas.SetPixel(round(x0 + (x1 - x0) * index / steps), round(y0 + (y1 - y0) * index / steps),
                        colour.red, colour.green, colour.blue)


_FAKE_GRAPHICS = types.SimpleNamespace(
    Color=lambda r, g, b: types.SimpleNamespace(red=r, green=g, blue=b),
    Font=_FakeFont, DrawText=_draw_text, DrawLine=_draw_line,
    DrawCircle=lambda canvas, x, y, r, colour: canvas.SetPixel(x + r, y, colour.red, colour.green, colour.blue),
)


def physical_matrix():
    with patch.object(matrix_framework, "RGBMatrix", _FakeRGBMatrix), \
            patch.object(matrix_framework, "RGBMatrixOptions", types.SimpleNamespace), \
            patch.object(matrix_framework, "graphics", _FAKE_GRAPHICS):
        matrix = matrix_framework.Matrix()
    patcher = patch.object(matrix_framework, "graphics", _FAKE_GRAPHICS)
    patcher.start()
    return matrix, patcher


EDGES2 = SafeArea(top=2, right=2, bottom=2, left=2)


class SafeAreaDrawingTests(unittest.TestCase):
    def setUp(self):
        self.matrix, patcher = physical_matrix()
        self.addCleanup(patcher.stop)

    def published(self):
        return self.matrix._hardware.shown, Image.frombytes("RGB", (64, 32), self.matrix.latest_frame_rgb()[0])

    def test_pixel_is_translated_and_clipped_and_fill_never_lights_the_margin(self):
        self.matrix.set_safe_area(EDGES2)
        self.assertEqual((self.matrix.matrix.width, self.matrix.matrix.height), (60, 28))
        self.matrix.fill(0, 0, 90)
        self.matrix.pixel(0, 0, 255, 0, 0)
        self.matrix.pixel(60, 5, 0, 255, 0)  # just outside the inner rectangle
        self.matrix.pixel(-1, 5, 0, 255, 0)
        self.matrix.swap()
        for frame in self.published():
            self.assertEqual(frame.getpixel((2, 2)), (255, 0, 0))
            # 0.3.84 (owner decision 6): the margin ring is always black, fill included.
            self.assertEqual(frame.getpixel((0, 0)), (0, 0, 0), "fill stops at the safe area")
            self.assertEqual(frame.getpixel((62, 7)), (0, 0, 0), "clipped pixel never lights the margin")
            self.assertEqual(frame.getpixel((1, 7)), (0, 0, 0))
            self.assertEqual(frame.getpixel((61, 7)), (0, 0, 90), "fill covers the inner canvas")
            colours = {c: n for n, c in frame.getcolors()}
            self.assertEqual(colours[(0, 0, 90)], 60 * 28 - 1)
            self.assertEqual(colours[(0, 0, 0)], 64 * 32 - 60 * 28)

    def test_text_and_lines_never_reach_the_edge_on_hardware_or_preview(self):
        self.matrix.set_safe_area(EDGES2)
        self.matrix.fill(10, 10, 10)
        white = self.matrix.colour("white")
        self.matrix.draw_text(self.matrix.font("5x7"), -3, 6, white, "HELLO WORLD WIDE")
        self.matrix.draw_line(-5, 27, 70, 27, white)
        self.matrix.draw_line(0, -4, 0, 40, white)
        self.matrix.swap()
        hardware, preview = self.published()
        self.assertEqual(hardware.tobytes(), preview.tobytes())
        for x in range(64):
            for y in range(32):
                if not (2 <= x < 62 and 2 <= y < 30):
                    self.assertEqual(hardware.getpixel((x, y)), (0, 0, 0), (x, y))  # black margin
        self.assertEqual(hardware.getpixel((2, 29)), (255, 255, 255))

    def test_images_fit_inside_and_media_toggle_controls_full_screen_media(self):
        red = Image.new("RGB", (64, 32), (255, 0, 0))
        self.matrix.set_safe_area(EDGES2)
        self.matrix.clear()
        self.matrix.draw_image(red, fit="stretch")
        self.matrix.swap()
        frame = self.published()[0]
        self.assertEqual(frame.getpixel((0, 0)), (0, 0, 0))
        self.assertEqual(frame.getpixel((2, 2)), (255, 0, 0))
        self.assertEqual(frame.getpixel((61, 29)), (255, 0, 0))
        self.assertEqual(frame.getpixel((62, 30)), (0, 0, 0))
        # Toggle ON (default): full-screen media also stays inside.
        with self.matrix.safe_area_scope(EDGES2, media=True):
            self.matrix.clear()
            self.matrix.draw_image(red, fit="stretch")
            self.matrix.swap()
        self.assertEqual(self.published()[0].getpixel((0, 0)), (0, 0, 0))
        # Toggle OFF: full-screen media fills the whole panel under the frame.
        outside = SafeArea(top=2, right=2, bottom=2, left=2, media_inside=False)
        with self.matrix.safe_area_scope(outside, media=True):
            self.assertEqual(self.matrix.matrix.width, 64)
            self.matrix.clear()
            self.matrix.draw_image(red, fit="stretch")
            self.matrix.swap()
        for frame in self.published():
            self.assertEqual(frame.getpixel((0, 0)), (255, 0, 0))
            self.assertEqual(frame.getpixel((63, 31)), (255, 0, 0))
        # Non-media modules still respect the area when the toggle is off.
        with self.matrix.safe_area_scope(outside):
            self.assertEqual(self.matrix.matrix.width, 60)
        self.assertEqual(self.matrix.safe_area, EDGES2, "scope restores the previous area")

    def test_zero_safe_area_keeps_the_raw_hardware_object(self):
        self.matrix.set_safe_area(SafeArea())
        self.assertIs(self.matrix.matrix, self.matrix._hardware)


class CaptureMatrixSafeAreaTests(unittest.TestCase):
    def test_preview_adapter_matches_physical_semantics(self):
        capture = CaptureMatrix(safe_area=SafeArea(top=1, right=3, bottom=4, left=6))
        self.assertEqual((capture.matrix.width, capture.matrix.height), (55, 27))
        capture.fill(0, 50, 0)
        capture.pixel(0, 0, 255, 255, 255)
        capture.pixel(55, 0, 255, 0, 0)
        capture.draw_line(-10, 3, 100, 3, capture.colour_rgb("#ffffff"))
        frame = capture.snapshot()
        self.assertEqual(frame.getpixel((6, 1)), (255, 255, 255))
        self.assertEqual(frame.getpixel((61, 1)), (0, 0, 0), "margin stays black (0.3.84)")
        self.assertEqual(frame.getpixel((5, 4)), (0, 0, 0))
        self.assertEqual(frame.getpixel((6, 4)), (255, 255, 255))
        self.assertEqual(frame.getpixel((60, 4)), (255, 255, 255))
        self.assertEqual(frame.getpixel((61, 4)), (0, 0, 0))
        self.assertEqual(frame.getpixel((60, 5)), (0, 50, 0), "fill covers the inner canvas")


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


ROLLBACK_COMMIT = "5eb747b"  # 0.3.75-preview, the release before 1.11


def _load_with_0_3_75(test, *config_paths):
    """Load each path with the real 0.3.75 ConfigStore (git archive, subprocess).

    Returns one ``(loaded, stderr)`` pair per path.
    """
    import io
    import subprocess
    import sys
    import tarfile
    repo = Path(__file__).resolve().parents[1]
    try:
        archive = subprocess.run(["git", "-C", str(repo), "archive", ROLLBACK_COMMIT],
                                 check=True, capture_output=True, timeout=60).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        test.skipTest(f"0.3.75 sources unavailable (shallow clone?): {error}")
    with tempfile.TemporaryDirectory() as old_tree:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            # CI's Debian Python 3.11.2 predates extraction filters; the archive is our own git tree.
            if hasattr(tarfile, "data_filter"):
                tar.extractall(old_tree, filter="data")
            else:
                tar.extractall(old_tree)
        probe = ("import sys; from pathlib import Path; from config_store import ConfigStore;"
                 "ConfigStore(Path(sys.argv[1]), media_dir=Path(sys.argv[2])); print('LOADED')")
        results = []
        for config_path in config_paths:
            result = subprocess.run(
                [sys.executable, "-c", probe, str(config_path), str(Path(old_tree) / "media")],
                cwd=old_tree, capture_output=True, text=True, timeout=120,
                env={"PATH": "/usr/bin:/bin", "PYTHONPATH": old_tree, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            results.append((result.stdout.strip() == "LOADED", result.stderr[-2000:]))
        return results


class SafeAreaConfigTests(unittest.TestCase):
    def test_fresh_install_defaults_to_zero(self):
        self.assertEqual(DEFAULT_CONFIG["device"]["safe_area"],
                         {"top": 0, "right": 0, "bottom": 0, "left": 0, "media_inside": True})
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            self.assertEqual(store.get()["device"]["safe_area"],
                             {"top": 0, "right": 0, "bottom": 0, "left": 0, "media_inside": True})

    def test_existing_display_without_the_key_reads_two_pixels_without_rewriting(self):
        saved = copy.deepcopy(DEFAULT_CONFIG)
        del saved["device"]["safe_area"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(_encode(saved))
            before = path.read_bytes()
            store = ConfigStore(path)
            self.assertEqual(store.get()["device"]["safe_area"],
                             {"top": 2, "right": 2, "bottom": 2, "left": 2, "media_inside": True})
            self.assertEqual(path.read_bytes(), before, "reading never rewrites the saved file")
            # An unrelated save keeps the projected value in memory but never writes the
            # new key, so the file stays loadable by 0.3.75 (rollback safety, M1).
            updated = store.update({"device": {"name": "Hall"}}, base_revision=0)
            self.assertEqual(updated["device"]["safe_area"]["left"], 2)
            written = json.loads(path.read_bytes())
            self.assertEqual(written["device"]["name"], "Hall")
            self.assertNotIn("safe_area", written["device"])
            self.assertEqual(store.get()["device"]["safe_area"]["top"], 2)

    def test_unrelated_saves_keep_an_existing_display_loadable_by_0_3_75(self):
        saved = copy.deepcopy(DEFAULT_CONFIG)
        del saved["device"]["safe_area"]
        projected = {"top": 2, "right": 2, "bottom": 2, "left": 2, "media_inside": True}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(_encode(saved))
            store = ConfigStore(path, media_dir=Path(directory) / "media")
            store.update({"device": {"name": "Hall"}, "display": {"day_brightness": 70}}, base_revision=0)
            # Sending the unchanged projected value is the same as not sending it.
            store.update({"device": {"name": "Hall 2", "safe_area": dict(projected)}}, base_revision=1)
            store.validate_candidate({"device": {"safe_area": dict(projected)}})
            written = json.loads(path.read_bytes())
            self.assertNotIn("safe_area", written["device"])
            self.assertEqual((written["device"]["name"], written["display"]["day_brightness"]), ("Hall 2", 70))
            # Control: the same file with the key is exactly what 0.3.75 rejects.
            with_key = Path(directory) / "with-key.json"
            written["device"]["safe_area"] = dict(projected)
            with_key.write_bytes(_encode(written))
            (loaded, error), (control_loaded, control_error) = _load_with_0_3_75(self, path, with_key)
            self.assertTrue(loaded, error)
            self.assertFalse(control_loaded)
            self.assertIn("device.safe_area", control_error)

    def test_explicit_safe_area_save_persists_and_survives_unrelated_saves(self):
        saved = copy.deepcopy(DEFAULT_CONFIG)
        del saved["device"]["safe_area"]
        chosen = {"top": 1, "right": 3, "bottom": 0, "left": 4, "media_inside": False}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(_encode(saved))
            store = ConfigStore(path)
            self.assertEqual(store.update({"device": {"safe_area": chosen}}, base_revision=0)["device"]["safe_area"],
                             chosen)
            self.assertEqual(json.loads(path.read_bytes())["device"]["safe_area"], chosen)
            store.update({"device": {"name": "Hall"}}, base_revision=1)
            self.assertEqual(json.loads(path.read_bytes())["device"]["safe_area"], chosen)
            # Once saved explicitly, the 2 px value is a real choice and is kept too.
            two = {"top": 2, "right": 2, "bottom": 2, "left": 2, "media_inside": True}
            store.update({"device": {"safe_area": two}}, base_revision=2)
            self.assertEqual(json.loads(path.read_bytes())["device"]["safe_area"], two)
            store.update({"device": {"name": "Hall 2"}}, base_revision=3)
            self.assertEqual(json.loads(path.read_bytes())["device"]["safe_area"], two)

    def test_validation_is_strict(self):
        valid = {"top": 0, "right": 6, "bottom": 3, "left": 1, "media_inside": False}
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["device"]["safe_area"] = valid
        validate_config(copy.deepcopy(config))
        bad_values = [
            {**valid, "top": 7}, {**valid, "left": -1}, {**valid, "right": 2.0},
            {**valid, "bottom": "2"}, {**valid, "top": True}, {**valid, "media_inside": 1},
            {key: value for key, value in valid.items() if key != "left"},
            {**valid, "extra": 1}, [0, 0, 0, 0], None,
        ]
        for value in bad_values:
            with self.subTest(value=value):
                broken = copy.deepcopy(config)
                broken["device"]["safe_area"] = value
                with self.assertRaises(ValueError):
                    validate_config(broken)
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            saved = store.update({"device": {"safe_area": valid}}, base_revision=0)
            self.assertEqual(saved["device"]["safe_area"], valid)
            for value in ({"top": 1}, {**valid, "extra": 0}, {**valid, "top": 9}):
                with self.subTest(update=value), self.assertRaises(ValueError):
                    store.update({"device": {"safe_area": value}}, base_revision=1)
                with self.subTest(candidate=value), self.assertRaises(ValueError):
                    store.validate_candidate({"device": {"safe_area": value}})
            self.assertEqual(store.get()["device"]["safe_area"], valid)


# Captured from 0.3.75 (FRAME_MARGIN = 2, no system safe area) before removal.
GOLDEN_0375 = {
    "large:0000": "cdc76d43ad450a01d65370c59760b2a63e176366c8464f78a6ebdf8dd64415a6",
    "large:2130": "82d8a430ab256d9ccc5c06b0981d38fc85176af19ad29397b5500ad076ffce05",
    "icon:0000": "2d94e7840f7ef747e6cf442fc497ef51c70243e227104def8213d8c28a9c917f",
    "icon:2130": "ef946cf7eafaeb294cdde6adc4d7cc9048a89490a1faa1d55abbf1e1fc3ec5b0",
    "progress:0000": "b927158d8c7960b51845e38f87c0db09e376a306312cd1aa6a77386b7d89c019",
    "progress:2130": "aa25040f1826aba2fda144cf543e6e0c383bb785d81b2a1c7eb8c48f8ca3c74e",
    "tearoff:0000": "d459ffd6031fe488aa887135335bf00f3ff27e472aef02d106fd947335df9510",
    "tearoff:2130": "860e74b9cc84f18e369de6ebe4311ad2ecc10015074e1ab0692a35b5e0f9d4a1",
    "flip:0000": "e0f7b5fa738261de9cb169f520d5990ea28c11a298320a85004af5b54ae6edde",
    "flip:2130": "4d6e9c45ee9e671bd3706ad57e167097f24e14b5bb106c96570fa0c1dcb530b5",
    "ring:0000": "22d70fdcfb629e9e72b95c7b8ae2e980c6ed8d5b3f41089516f146249b06da03",
    "ring:2130": "a6d52292a5b64089df05c55b305b9e61787352e238dd0755db338b43d65d9625",
    "hero:0000": "6568e5d2669bfa227a1c87e3dd476b8822777ce16a068e4b36ab7aabf134eaa4",
    "hero:2130": "6b5fbbf3621c6317cad0e33bedfc564ffe76239cca553b2c96814233ec828a0f",
    "date:0000": "a1972ba1a54356a4181f6a0446253d4f3d08bf009dbb8e1198b9b267e50d7df2",
    "date:2130": "07dea865dddbc584f80e35ab8974d0e3641b2b3791e06d25aff69350bad75f6e",
}


class CalendarSafeAreaTests(unittest.TestCase):
    def config(self, layout, insets):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["device"]["safe_area"] = {"top": 2, "right": 2, "bottom": 2, "left": 2, "media_inside": True}
        config["event_countdown"].update(layout=layout, timezone="UTC", insets=insets, events=[dict(
            id="launch", title="Launch party tonight", start="2030-01-01T12:00", end=None,
            all_day=False, annual=False, enabled=True, now_seconds=900, accent=None, icon="calendar")])
        return config

    def test_frame_margin_is_gone(self):
        from modules import event_countdown
        self.assertFalse(hasattr(event_countdown, "FRAME_MARGIN"))

    def test_two_pixel_safe_area_is_pixel_identical_to_the_old_calendar_border(self):
        from event_countdown_config import LAYOUTS
        from modules import event_countdown as ec
        now = dt.datetime(2029, 12, 20, 9, 30, tzinfo=dt.timezone.utc)
        for layout in LAYOUTS:
            for insets in ({"top": 0, "right": 0, "bottom": 0, "left": 0},
                           {"top": 1, "right": 3, "bottom": 0, "left": 2}):
                key = f"{layout}:{insets['left']}{insets['top']}{insets['right']}{insets['bottom']}"
                with self.subTest(key):
                    config = self.config(layout, insets)
                    settings = ec.effective_settings(config)
                    selected = ec.select_event(settings, now=now)
                    # Preview path: inner render composed onto the full panel.
                    inner, _ = ec.render_frame(settings, selected, elapsed=0.35, size=(60, 28))
                    composed = SafeArea.from_config(config).compose(inner, settings["background"])
                    self.assertEqual(hashlib.sha256(composed.tobytes()).hexdigest(), GOLDEN_0375[key])
                    # Physical path: Matrix with the 2px safe area draws the same frame.
                    matrix, patcher = physical_matrix()
                    try:
                        matrix.set_safe_area(SafeArea.from_config(config))
                        matrix.fill(*Image.new("RGB", (1, 1), settings["background"]).getpixel((0, 0)))
                        matrix.draw_image(inner)
                        matrix.swap()
                        self.assertEqual(hashlib.sha256(matrix._hardware.shown.tobytes()).hexdigest(),
                                         GOLDEN_0375[key])
                    finally:
                        patcher.stop()


class LegacyMigrationSafeAreaTests(unittest.TestCase):
    def test_older_schema_configs_are_migrated_with_a_two_pixel_safe_area(self):
        for keep_device in (True, False):
            with self.subTest(keep_device=keep_device), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                legacy = copy.deepcopy(DEFAULT_CONFIG)
                legacy["schema_version"] = 3
                if keep_device:
                    del legacy["device"]["safe_area"]
                else:
                    del legacy["device"]
                path.write_text(json.dumps(legacy))
                migrated = ConfigStore(path, media_dir=Path(directory) / "media").get()
                self.assertEqual(migrated["device"]["safe_area"],
                                 {"top": 2, "right": 2, "bottom": 2, "left": 2, "media_inside": True})
                # The migration is persisted, so the next read sees the same value.
                self.assertEqual(json.loads(path.read_text())["device"]["safe_area"],
                                 migrated["device"]["safe_area"])

    def test_older_schema_config_keeps_an_explicit_safe_area(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            legacy = copy.deepcopy(DEFAULT_CONFIG)
            legacy["schema_version"] = 3
            chosen = {"top": 1, "right": 0, "bottom": 3, "left": 6, "media_inside": False}
            legacy["device"]["safe_area"] = chosen
            path.write_text(json.dumps(legacy))
            self.assertEqual(ConfigStore(path, media_dir=Path(directory) / "media").get()["device"]["safe_area"],
                             chosen)

    def test_fresh_install_has_no_safe_area(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            self.assertEqual(store.get()["device"]["safe_area"],
                             {"top": 0, "right": 0, "bottom": 0, "left": 0, "media_inside": True})


class PairingSafeAreaTests(unittest.TestCase):
    """Auditor S1: pairing PIN/address screens use the saved area without waiting for the scheduler."""

    @staticmethod
    def outside_is_dark(frame, area):
        return all(frame.getpixel((x, y)) == (0, 0, 0)
                   for x in range(64) for y in range(32)
                   if not (area.left <= x < 64 - area.right and area.top <= y < 32 - area.bottom))

    def test_pairing_screens_use_the_saved_safe_area_before_the_scheduler_and_after_a_save(self):
        from display_controller import DisplayController
        matrix, patcher = physical_matrix()
        self.addCleanup(patcher.stop)
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            three = {"top": 3, "right": 3, "bottom": 3, "left": 3, "media_inside": True}
            store.update({"device": {"safe_area": three}}, base_revision=0)
            controller = DisplayController(matrix, store, ip_address_provider=lambda: "192.168.1.4")
            self.addCleanup(controller.end_pairing)
            # No scheduler loop has run: the PIN page still uses the saved 3 px area.
            controller.show_pairing_pin("123456", ttl_seconds=60, rotate=False)
            self.assertEqual(matrix.safe_area, SafeArea.from_mapping(three))
            shown = matrix._hardware.shown
            self.assertTrue(self.outside_is_dark(shown, SafeArea.from_mapping(three)))
            self.assertGreater(sum(1 for pixel in shown.getdata() if pixel != (0, 0, 0)), 0)
            # A save while the pairing screen is up applies on the next pairing page.
            five = {"top": 5, "right": 5, "bottom": 5, "left": 5, "media_inside": True}
            store.update({"device": {"safe_area": five}}, base_revision=1)
            self.assertTrue(controller._draw_pairing_page(show_ip=True))
            self.assertEqual(matrix.safe_area, SafeArea.from_mapping(five))
            self.assertTrue(self.outside_is_dark(matrix._hardware.shown, SafeArea.from_mapping(five)))


class ModuleMediaToggleTests(unittest.TestCase):
    """Controller/preview dispatch: which modules count as full-screen media."""

    def config(self, media_inside):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["device"]["safe_area"] = {"top": 2, "right": 3, "bottom": 1, "left": 4, "media_inside": media_inside}
        config["custom_modules"] = [{"id": "custom_media_1", "type": "media"}, {"id": "custom_text_1", "type": "text"}]
        return config

    def test_media_modules_follow_the_toggle_and_other_modules_never_do(self):
        from display_controller import is_full_screen_media, safe_area_scope
        white = Image.new("RGB", (64, 32), "white")
        for media_inside in (True, False):
            config = self.config(media_inside)
            for name, media in (("splash", True), ("sleep", True), ("custom_media_1", True), ("media:clip.gif", True),
                                ("custom_text_1", False), ("event_countdown", False), ("clock_lava", False)):
                with self.subTest(media_inside=media_inside, module=name):
                    self.assertEqual(is_full_screen_media(config, name), media)
                    matrix = CaptureMatrix()
                    canvas = matrix.matrix.CreateFrameCanvas()
                    with safe_area_scope(matrix, config, name):
                        full = media and not media_inside
                        self.assertEqual((matrix.matrix.width, matrix.matrix.height), (64, 32) if full else (57, 29))
                        matrix.fill(0, 0, 0, canvas=canvas)
                        matrix.draw_image(white, canvas=canvas)
                        frame = matrix.snapshot(canvas)
                    self.assertEqual(frame.getpixel((0, 0)), (255, 255, 255) if full else (0, 0, 0))
                    self.assertEqual(frame.getpixel((4, 2)), (255, 255, 255))
                    self.assertEqual(frame.getpixel((63, 31)), (255, 255, 255) if full else (0, 0, 0))
                    # The scope restores the previous (zero) area afterwards.
                    self.assertEqual((matrix.matrix.width, matrix.matrix.height), (64, 32))

    def test_scope_is_a_no_op_for_plain_mocks(self):
        from unittest.mock import Mock
        from display_controller import apply_safe_area, safe_area_scope
        matrix = Mock()
        with safe_area_scope(matrix, self.config(True), "splash"):
            pass
        apply_safe_area(matrix, self.config(True))
        matrix.safe_area_scope.assert_not_called()
        matrix.set_safe_area.assert_not_called()

    def test_clock_lava_centres_inside_the_safe_area(self):
        from modules import clock_lava
        matrix = CaptureMatrix(safe_area=SafeArea(top=2, right=2, bottom=2, left=2))
        self.assertEqual(clock_lava.drawable_size(matrix), (60, 28))
        self.assertEqual(clock_lava.aligned_x("center", 20, 60), 20)
        self.assertEqual(clock_lava.aligned_x("right", 20, 60), 38)

    def test_calendar_preview_composes_the_inner_render_onto_the_panel(self):
        import base64
        import io
        from modules import event_countdown as ec
        config = CalendarSafeAreaTests().config("large", {"top": 0, "right": 0, "bottom": 0, "left": 0})
        config["event_countdown"]["background"] = "#102030"
        frame = next(ec.preview_frames(config, duration=1))
        data = frame.get("png") or frame.get("data") or frame.get("image")
        if isinstance(data, str):
            data = base64.b64decode(data.split(",", 1)[-1])
        image = Image.open(io.BytesIO(data)).convert("RGB")
        self.assertEqual(image.size, (64, 32))
        # 0.3.84 (owner decision 6): the background fills the inner canvas, never the margin.
        for point in ((0, 0), (1, 31), (63, 0), (62, 31)):
            self.assertEqual(image.getpixel(point), (0, 0, 0))
        inner = image.crop((2, 2, 62, 30))
        self.assertEqual(max(inner.getcolors(4096))[1], (0x10, 0x20, 0x30), "background inside")



def _edge_marked_png():
    """64x32 red source whose right 4 columns are green and bottom 4 rows blue."""
    import io
    image = Image.new("RGB", (64, 32), (255, 0, 0))
    image.paste((0, 255, 0), (60, 0, 64, 28))
    image.paste((0, 0, 255), (0, 28, 64, 32))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class _BytesPin:
    def __init__(self, data):
        self.data = data

    def fileno(self):
        return 0

    def dup_file(self):
        import io
        return io.BytesIO(self.data)


FULL_PANEL = {"x": 0, "y": 0, "width": 64, "height": 32, "keep_aspect": False}


class FullScreenMediaFitTests(unittest.TestCase):
    """Full-screen media is scaled into the safe area, not translated and cut off."""

    def assert_fitted(self, frame):
        for x in range(64):
            for y in range(32):
                if not (2 <= x <= 61 and 2 <= y <= 29):
                    self.assertEqual(frame.getpixel((x, y)), (0, 0, 0), (x, y))
        right = frame.getpixel((61, 12))
        self.assertGreater(right[1], right[0], f"right source edge visible: {right}")
        bottom = frame.getpixel((20, 29))
        self.assertGreater(bottom[2], bottom[0], f"bottom source edge visible: {bottom}")
        self.assertEqual(frame.getpixel((2, 2)), (255, 0, 0))

    def assert_full_panel(self, frame):
        self.assertEqual(frame.getpixel((0, 0)), (255, 0, 0))
        self.assertEqual(frame.getpixel((63, 12)), (0, 255, 0))
        self.assertEqual(frame.getpixel((20, 31)), (0, 0, 255))

    def physical_play(self, safe_area):
        from modules.media_player import play_media
        matrix, patcher = physical_matrix()
        self.addCleanup(patcher.stop)
        with matrix.safe_area_scope(safe_area, media=True):
            play_media(matrix, "edge.png", 0.01, settings=dict(FULL_PANEL),
                       pinned_handle=_BytesPin(_edge_marked_png()))
        return matrix._hardware.shown, Image.frombytes("RGB", (64, 32), matrix.latest_frame_rgb()[0])

    def test_play_media_fits_a_full_panel_still_inside_the_safe_area(self):
        for frame in self.physical_play(EDGES2):
            self.assert_fitted(frame)

    def test_play_media_covers_the_whole_panel_when_media_may_go_under_the_frame(self):
        outside = SafeArea(top=2, right=2, bottom=2, left=2, media_inside=False)
        for frame in self.physical_play(outside):
            self.assert_full_panel(frame)

    def test_edit_preview_media_matches_the_panel(self):
        from edit_preview import media_preview_frames
        for safe_area, check in ((EDGES2, self.assert_fitted),
                                 (SafeArea(top=2, right=2, bottom=2, left=2, media_inside=False),
                                  self.assert_full_panel)):
            capture = CaptureMatrix(safe_area=safe_area, media=True)
            frames = list(media_preview_frames(
                capture, "edge.png", 1, dict(FULL_PANEL), pinned_handle=_BytesPin(_edge_marked_png()),
            ))
            self.assertTrue(frames)
            check(capture.snapshot())
            panel = self.physical_play(safe_area)[0]
            self.assertEqual(capture.snapshot().tobytes(), panel.tobytes(), "preview == panel")

    def test_custom_media_screen_with_manual_geometry_is_fitted(self):
        from modules import custom_text
        matrix, patcher = physical_matrix()
        self.addCleanup(patcher.stop)
        module_config = {"type": "media", "source": "edge.png", "fit": "manual", **FULL_PANEL}
        with matrix.safe_area_scope(EDGES2, media=True), \
                patch.object(custom_text, "_wait", lambda *args: False):
            custom_text.run(matrix, {}, 0.01, module_config=module_config,
                            pinned_handles={"edge.png": _BytesPin(_edge_marked_png())})
        self.assert_fitted(matrix._hardware.shown)

if __name__ == "__main__":
    unittest.main()
