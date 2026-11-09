"""Shared media placement (owner decision 2, 2026-10-31).

Splash/Sleep layouts, playlist row layouts and custom-media fits behave exactly
as if the physical panel were (64-l-r) x (32-t-b): saved x/y/width/height are
pixels on that smaller canvas, clipped at its edge, never rescaled. One
placement function (safe_area.place_media) serves the physical Matrix and the
Edit Preview. With no safe area every frame is pixel-identical to main.
"""
import base64
import copy
import hashlib
import io
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import matrix_framework
import safe_area as safe_area_module
from config_store import DEFAULT_CONFIG, validate_config
from edit_preview import CaptureMatrix, media_preview_frames
from safe_area import SafeArea
from tests.test_safe_area import _FAKE_GRAPHICS, _FakeCanvas, _FakeRGBMatrix, physical_matrix

ROOT = Path(__file__).resolve().parents[1]


class _Pin:
    def __init__(self, data):
        self.data = data

    def fileno(self):
        return 0

    def dup_file(self):
        return io.BytesIO(self.data)


def _png(size):
    width, height = size
    image = Image.new("RGB", size, (200, 0, 0))
    image.paste((0, 200, 0), (width - 4, 0, width, height))
    image.paste((0, 0, 200), (0, height - 4, width, height))
    image.paste((255, 255, 255), (width // 4, height // 4, width // 2, height // 2))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _gif():
    frames = []
    for colour in ((200, 0, 0), (0, 200, 0), (0, 0, 200)):
        frame = Image.new("RGB", (64, 32), colour)
        frame.paste((255, 255, 255), (16, 8, 32, 16))
        frames.append(frame)
    output = io.BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=20, loop=0)
    return output.getvalue()


SOURCES = {
    "png64x32.png": _png((64, 32)),
    "png32x32.png": _png((32, 32)),
    "png128x48.png": _png((128, 48)),
    "gif64x32.gif": _gif(),
}
INSETS = {
    "4": SafeArea(4, 4, 4, 4),
    "6": SafeArea(6, 6, 6, 6),
    "T1R2B3L6": SafeArea(top=1, right=2, bottom=3, left=6),
}
LAYOUTS = {
    "full64x32_keep": dict(x=0, y=0, width=64, height=32, keep_aspect=True),
    "full64x32_stretch": dict(x=0, y=0, width=64, height=32, keep_aspect=False),
    "box32x32@16_keep": dict(x=16, y=0, width=32, height=32, keep_aspect=True),
    "box32x32@16_stretch": dict(x=16, y=0, width=32, height=32, keep_aspect=False),
    "box20x10@40,20_stretch": dict(x=40, y=20, width=20, height=10, keep_aspect=False),
}
FITS = {
    "contain": dict(x=0, y=0, width=64, height=32),
    "cover": dict(x=0, y=0, width=64, height=32),
    "stretch": dict(x=0, y=0, width=64, height=32),
    "manual": dict(x=16, y=0, width=32, height=32),
}


def _layout_settings(layout):
    return dict(layout, video_fps=20, playback_speed_percent=100.0)


def _fit_settings(fit, geometry):
    return {"type": "media", "fit": fit, "loops": 1, "playback_speed_percent": 100.0, **geometry}


def _small_panel(width, height):
    """A physical Matrix on fake hardware whose panel REALLY is width x height."""
    class Canvas(_FakeCanvas):
        def __init__(self):
            super().__init__(width, height)

    class Hardware(_FakeRGBMatrix):
        def __init__(self, options=None):
            super().__init__(options)
            self.width, self.height = width, height

        def CreateFrameCanvas(self):
            return Canvas()

        def SwapOnVSync(self, canvas):
            self.shown = canvas.image.copy()
            return Canvas()

    with patch.object(matrix_framework, "RGBMatrix", Hardware), \
            patch.object(matrix_framework, "RGBMatrixOptions", types.SimpleNamespace), \
            patch.object(matrix_framework, "graphics", _FAKE_GRAPHICS):
        matrix = matrix_framework.Matrix()
    patcher = patch.object(matrix_framework, "graphics", _FAKE_GRAPHICS)
    patcher.start()
    return matrix, patcher


def _physical_run(matrix, kind, name, settings, scope):
    from modules import custom_text
    from modules.media_player import play_media
    pin = _Pin(SOURCES[name])
    with patch("time.sleep", lambda _s: None), patch.object(custom_text, "_wait", lambda *_a: False), scope:
        if kind == "layout":
            play_media(matrix, name, 0.001, settings=settings, pinned_handle=pin, gif_loops=1)
        else:
            custom_text.run(matrix, {}, 0.001, module_config=dict(settings, source=name),
                            pinned_handles={name: pin})
    return matrix._hardware.shown


def physical(kind, name, settings, safe_area):
    """Panel frame of the physical Matrix (64x32 fake hardware) inside the media scope."""
    matrix, patcher = physical_matrix()
    try:
        return _physical_run(matrix, kind, name, settings, matrix.safe_area_scope(safe_area, media=True))
    finally:
        patcher.stop()


def smaller_panel(kind, name, settings, size):
    """The same module on a physical panel that really is the inner size (no safe area)."""
    matrix, patcher = _small_panel(*size)
    try:
        import contextlib
        return _physical_run(matrix, kind, name, settings, contextlib.nullcontext())
    finally:
        patcher.stop()


def edit_preview(name, settings, safe_area):
    """Last frame of the Edit Preview media seam (as webapp._edit_preview_iterator calls it)."""
    capture = CaptureMatrix(safe_area=safe_area, media=True)
    frames = [
        Image.open(io.BytesIO(base64.b64decode(frame["png"]))).convert("RGB")
        for frame in media_preview_frames(capture, name, 1, settings, pinned_handle=_Pin(SOURCES[name]),
                                          gif_loops=1, gif_max_duration=None)
    ]
    return frames[-1]


def inner_crop(frame, safe_area):
    x, y, width, height = safe_area.viewport()
    return frame.crop((x, y, x + width, y + height))


def lit_margin(frame, safe_area):
    x0, y0, width, height = safe_area.viewport()
    return sum(1 for y in range(32) for x in range(64)
               if not (x0 <= x < x0 + width and y0 <= y < y0 + height) and frame.getpixel((x, y)) != (0, 0, 0))


def _cases():
    for name in SOURCES:
        for label, layout in LAYOUTS.items():
            yield "layout", name, label, _layout_settings(layout)
        for fit, geometry in FITS.items():
            yield "fit", name, fit, _fit_settings(fit, geometry)


def zero_digests():
    """sha256 of physical + edit-preview frames with no safe area and with media_inside off."""
    result = {}
    surfaces = {"0": SafeArea(), "4/media-outside": SafeArea(4, 4, 4, 4, media_inside=False)}
    for kind, name, label, settings in _cases():
        for inset, safe_area in surfaces.items():
            value = hashlib.sha256()
            value.update(physical(kind, name, settings, safe_area).tobytes())
            value.update(edit_preview(name, settings, safe_area).tobytes())
            result[f"{kind}|{name}|{label}|{inset}"] = value.hexdigest()
    return result


def library_preview_digests():
    """A media library preview (play_media with no saved layout) on the physical panel."""
    from modules.media_player import play_media
    result = {}
    for name in SOURCES:
        for inset, safe_area in {"0": SafeArea(), "4": INSETS["4"], "T1R2B3L6": INSETS["T1R2B3L6"]}.items():
            matrix, patcher = physical_matrix()
            try:
                with patch("time.sleep", lambda _s: None), matrix.safe_area_scope(safe_area, media=True):
                    play_media(matrix, name, 0.001, settings=None, pinned_handle=_Pin(SOURCES[name]), gif_loops=1)
                result[f"{name}|{inset}"] = hashlib.sha256(matrix._hardware.shown.tobytes()).hexdigest()
            finally:
                patcher.stop()
    return result


def _combined(digests):
    value = hashlib.sha256()
    for key in sorted(digests):
        value.update(key.encode())
        value.update(digests[key].encode())
    return value.hexdigest()


# Rendered by origin/main e062776 (0.3.85-preview), Pillow 13.0.0, with this file's
# fixtures: zero_digests() and library_preview_digests() combined with _combined().
MAIN_ZERO_DIGEST = "beea2a6ec9891e9156c913f01254da069b5009be2d2dac2ac7a6973ba1e5c568"
MAIN_ZERO_COUNT = 72
MAIN_LIBRARY_PREVIEW_DIGEST = "eb9c2cc68fb40cd4d7c8077d25a7fffb6f58d19bb39aad500aef38606bec3f19"


class SavedLayoutOnSmallerCanvasTests(unittest.TestCase):
    """M3: saved pixels on the inner canvas, clipped at its edge; no proportional rescale."""

    def assert_as_if_smaller(self, kind, name, label, settings):
        for inset, safe_area in INSETS.items():
            with self.subTest(kind=kind, source=name, case=label, inset=inset):
                panel = physical(kind, name, settings, safe_area)
                reference = smaller_panel(kind, name, settings, safe_area.inner_size())
                self.assertEqual(inner_crop(panel, safe_area).tobytes(), reference.tobytes(),
                                 "physical inner pixels == a panel that really is the inner size")
                self.assertEqual(lit_margin(panel, safe_area), 0, "margin stays black")
                self.assertEqual(edit_preview(name, settings, safe_area).tobytes(), panel.tobytes(),
                                 "Edit Preview == panel")

    def test_splash_sleep_and_playlist_row_layouts(self):
        for name in ("png64x32.png", "png32x32.png", "gif64x32.gif"):
            for label, layout in LAYOUTS.items():
                self.assert_as_if_smaller("layout", name, label, _layout_settings(layout))

    def test_custom_media_fits(self):
        for name in ("png64x32.png", "png128x48.png"):
            for fit, geometry in FITS.items():
                self.assert_as_if_smaller("fit", name, fit, _fit_settings(fit, geometry))

    def test_centred_square_box_stays_centred_and_square(self):
        # main rescaled this box to 26x20 at x13 (a 20x20 image 3 px left of centre).
        # Authored pixels: the 32x32 box stays at inner x16, 32 wide, clipped at row 20.
        safe_area = INSETS["6"]
        panel = physical("layout", "png32x32.png", _layout_settings(LAYOUTS["box32x32@16_keep"]), safe_area)
        self.assertEqual(inner_crop(panel, safe_area).getbbox(), (16, 0, 48, 20))

    def test_fill_screen_layout_of_the_inner_size_fills_the_inner_canvas(self):
        for inset, safe_area in INSETS.items():
            width, height = safe_area.inner_size()
            layout = _layout_settings(dict(x=0, y=0, width=width, height=height, keep_aspect=False))
            with self.subTest(inset=inset):
                panel = physical("layout", "png64x32.png", layout, safe_area)
                expected = Image.open(io.BytesIO(SOURCES["png64x32.png"])).convert("RGB").resize(
                    (width, height), Image.Resampling.LANCZOS)
                self.assertEqual(inner_crop(panel, safe_area).tobytes(), expected.tobytes())


class NoSafeAreaIdentityTests(unittest.TestCase):
    def test_zero_safe_area_and_media_outside_are_pixel_identical_to_main(self):
        digests = zero_digests()
        self.assertEqual(len(digests), MAIN_ZERO_COUNT)
        self.assertEqual(_combined(digests), MAIN_ZERO_DIGEST)

    def test_media_library_preview_without_a_layout_is_unchanged(self):
        self.assertEqual(_combined(library_preview_digests()), MAIN_LIBRARY_PREVIEW_DIGEST)


class SharedPlacementTests(unittest.TestCase):
    """S2/S4/M1: one placement function in the drawing layer, used by both adapters."""

    def test_both_adapters_place_media_through_safe_area_place_media(self):
        place = safe_area_module.place_media  # AttributeError on main: no shared function
        image = Image.new("RGB", (64, 32), "red")
        matrix, patcher = physical_matrix()
        self.addCleanup(patcher.stop)
        for adapter in (matrix, CaptureMatrix(safe_area=INSETS["4"], media=True)):
            for options in ({"fit": "cover"}, {"fit": "manual", "x": 1, "y": 2, "width": 10, "height": 5},
                            {"width": 20, "height": 10, "keep_aspect": True}, {"width": 20}, {}):
                with self.subTest(adapter=type(adapter).__name__, options=options), \
                        patch.object(safe_area_module, "place_media", wraps=place) as spy:
                    adapter.draw_image(image, **options)
                    spy.assert_called_once()

    def test_fit_media_geometry_and_its_call_sites_are_gone(self):
        self.assertFalse(hasattr(safe_area_module, "fit_media_geometry"))
        for path in ("safe_area.py", "edit_preview.py", "matrix_framework.py",
                     "modules/media_player.py", "modules/custom_text.py"):
            self.assertNotIn("fit_media_geometry", (ROOT / path).read_text(), path)

    def test_draw_image_without_a_size_blits_one_to_one(self):
        image = Image.new("RGB", (64, 32), "white")
        for inset, safe_area in INSETS.items():
            matrix, patcher = physical_matrix()
            try:
                with self.subTest(inset=inset), matrix.safe_area_scope(safe_area):
                    self.assertEqual(matrix.draw_image(image).size, (64, 32))
                    self.assertEqual(CaptureMatrix(safe_area=safe_area).draw_image(image).size, (64, 32))
            finally:
                patcher.stop()

    def test_one_sided_size_scales_proportionally_on_both_adapters(self):
        image = Image.new("RGB", (64, 32), "white")
        matrix, patcher = physical_matrix()
        self.addCleanup(patcher.stop)
        self.assertEqual(matrix.draw_image(image, width=40).size, (40, 20))
        self.assertEqual(CaptureMatrix().draw_image(image, width=40).size, (40, 20))

    def test_keep_aspect_size_matches_pillow_thumbnail(self):
        place = safe_area_module.place_media
        for source in ((64, 32), (32, 32), (128, 48), (7, 3), (3, 7), (100, 1), (1, 100)):
            for box in ((64, 32), (32, 32), (20, 10), (13, 29), (1, 1), (60, 28)):
                expected = Image.new("RGB", source)
                expected.thumbnail(box, Image.Resampling.LANCZOS)
                size, position = place(source, None, {"x": 3, "y": 4, "width": box[0], "height": box[1],
                                                       "keep_aspect": True}, (52, 20))
                self.assertEqual((size, position), (expected.size, (3, 4)), (source, box))

    def test_fit_maths_use_the_canvas_size(self):
        place = safe_area_module.place_media
        layout = {"x": 16, "y": 0, "width": 32, "height": 32}
        self.assertEqual(place((64, 32), "contain", layout, (52, 20)), ((40, 20), (6, 0)))
        self.assertEqual(place((32, 32), "cover", layout, (52, 20)), ((52, 52), (0, -16)))
        self.assertEqual(place((32, 32), "stretch", layout, (52, 20)), ((52, 20), (0, 0)))
        self.assertEqual(place((64, 32), "manual", layout, (52, 20)), ((32, 32), (16, 0)))
        with self.assertRaises(ValueError):
            place((64, 32), "manual", {"x": 1.5, "y": 0, "width": 4, "height": 4}, (52, 20))


class SavedConfigCompatibilityTests(unittest.TestCase):
    def test_old_full_panel_layouts_load_unchanged_with_a_safe_area(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["device"]["safe_area"] = {"top": 6, "right": 6, "bottom": 6, "left": 6, "media_inside": True}
        config["media_layout"]["splash"].update(x=16, y=0, width=32, height=32)
        before = copy.deepcopy(config)
        validate_config(config)
        # Authored pixels survive as written: no rescale to the inner size on load.
        # (validate_config already mirrors media_layout onto the Splash row; that sync is
        # pre-existing behaviour, so the row must carry the same authored numbers.)
        self.assertEqual(config["media_layout"], before["media_layout"])
        row = next(item for item in config["playlist"] if item["module"] == "splash")
        self.assertEqual({k: row["layout"][k] for k in ("x", "y", "width", "height")},
                         {"x": 16, "y": 0, "width": 32, "height": 32})


if __name__ == "__main__":
    unittest.main()
