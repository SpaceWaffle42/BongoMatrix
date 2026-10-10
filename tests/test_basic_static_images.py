"""Roadmap 1.9: Basic custom modules can show a single static image (PNG/JPEG/WebP)."""

import io
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from PIL import Image

from config_store import ConfigStore, DEFAULT_CONFIG, validate_config
from edit_preview import CaptureMatrix, media_preview_frames
import matrix_framework
from modules import custom_text


ROOT = Path(__file__).resolve().parents[1]
FITS = ("contain", "cover", "stretch", "manual")


def gradient(width=40, height=10):
    image = Image.new("RGB", (width, height))
    for x in range(width):
        for y in range(height):
            image.putpixel((x, y), ((x * 6) % 256, (y * 25) % 256, 128))
    return image


def png_bytes(image):
    payload = io.BytesIO()
    image.save(payload, format="PNG")
    return payload.getvalue()


class _Pinned:
    def __init__(self, data):
        self.data = data

    def dup_file(self):
        return io.BytesIO(self.data)


def media_module(source="photo.png", fit="contain", **changes):
    module = {
        "id": "custom_photo", "name": "Photo", "type": "media", "source": source,
        "fit": fit, "x": 3, "y": 2, "width": 30, "height": 20, "loops": 1,
        "playback_speed_percent": 100.0,
    }
    module.update(changes)
    return module


def physical_frame(image, fit):
    """The exact pixels the physical Matrix submits for one fitted image."""
    prepared, draw_x, draw_y = matrix_framework.Matrix._prepare_media_fit(
        None, image, fit, 64, 32, x=3, y=2, width=30, height=20,
    )
    frame = Image.new("RGB", (64, 32))
    frame.paste(prepared, (draw_x, draw_y))
    return frame


class StaticImageSchemaTests(unittest.TestCase):
    def test_basic_media_module_accepts_png_jpeg_and_webp_sources(self):
        for source in ("photo.png", "photo.jpg", "photo.jpeg", "photo.webp", "built-in/photo.PNG"):
            with self.subTest(source=source):
                candidate = json.loads(json.dumps(DEFAULT_CONFIG))
                candidate["custom_modules"] = [media_module(source)]
                candidate["playlist"].append({"module": "custom_photo", "duration": 8, "enabled": True})
                validated = validate_config(candidate)
                self.assertEqual(validated["custom_modules"][0]["source"], source)

    def test_unsupported_basic_media_source_is_rejected_with_clear_message(self):
        for source in ("photo.bmp", "photo.tiff", "notes.txt"):
            with self.subTest(source=source):
                candidate = json.loads(json.dumps(DEFAULT_CONFIG))
                candidate["custom_modules"] = [media_module(source)]
                with self.assertRaisesRegex(ValueError, "PNG, JPEG, WebP, GIF or MP4"):
                    validate_config(candidate)

    def test_saved_static_image_module_keeps_user_display_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            gradient().save(media_root / "photo.png")
            store = ConfigStore(root / "config.json", media_dir=media_root)
            saved = store.update({
                "custom_modules": [media_module("photo.png")],
                "playlist": [{"module": "custom_photo", "duration": 7.5, "enabled": True}],
            })
            self.assertEqual(saved["playlist"][0]["duration"], 7.5)
            persisted = json.loads((root / "config.json").read_text())
            self.assertEqual(persisted["custom_modules"][0]["source"], "photo.png")
            self.assertEqual(persisted["playlist"][0]["duration"], 7.5)


class StaticImageRenderingTests(unittest.TestCase):
    def test_browser_preview_fit_matches_physical_matrix_for_every_fit(self):
        image = gradient()
        for fit in FITS:
            with self.subTest(fit=fit):
                preview = CaptureMatrix()
                preview.draw_image(image, x=3, y=2, width=30, height=20, fit=fit)
                self.assertEqual(
                    preview.snapshot().tobytes(), physical_frame(image, fit).tobytes(),
                )

    def test_physical_run_shows_fitted_image_for_the_playlist_duration(self):
        image = gradient()
        for fit in FITS:
            with self.subTest(fit=fit):
                capture = CaptureMatrix()
                matrix = Mock(wraps=capture)
                matrix.matrix = capture.matrix
                submitted = []
                matrix.swap.side_effect = lambda canvas=None: submitted.append(
                    capture.snapshot(canvas).copy()) or capture.swap(canvas)
                stop = Mock(spec=threading.Event)
                stop.is_set.return_value = False
                stop.wait.return_value = False
                shown = custom_text.run(
                    matrix, {}, duration=6.5, stop_event=stop,
                    module_config=media_module("photo.png", fit),
                    pinned_handles={"photo.png": _Pinned(png_bytes(image))},
                )
                self.assertTrue(shown)
                self.assertEqual(len(submitted), 1)
                self.assertEqual(submitted[0].tobytes(), physical_frame(image, fit).tobytes())
                stop.wait.assert_called_once_with(6.5)

    def test_physical_run_stops_before_drawing_when_cancelled(self):
        matrix = Mock()
        stop = threading.Event()
        stop.set()
        self.assertFalse(custom_text.run(
            matrix, {}, duration=5, stop_event=stop,
            module_config=media_module("photo.webp"),
            pinned_handles={"photo.webp": _Pinned(png_bytes(gradient()))},
        ))
        matrix.swap.assert_not_called()

    def test_physical_run_requires_the_pinned_capability(self):
        with self.assertRaisesRegex(ValueError, "capability"):
            custom_text.run(
                Mock(), {}, duration=5, stop_event=threading.Event(),
                module_config=media_module("photo.png"), pinned_handles={},
            )

    def test_edit_preview_frame_matches_physical_output(self):
        image = gradient()
        for fit in FITS:
            with self.subTest(fit=fit):
                frames = list(media_preview_frames(
                    CaptureMatrix(), "photo.png", 4, media_module("photo.png", fit),
                    pinned_handle=_Pinned(png_bytes(image)), monotonic=lambda: 0.0,
                ))
                self.assertEqual(len(frames), 1)
                self.assertEqual(frames[0]["delay_ms"], 4000)
                import base64
                decoded = Image.open(io.BytesIO(base64.b64decode(frames[0]["png"]))).convert("RGB")
                self.assertEqual(decoded.tobytes(), physical_frame(image, fit).tobytes())


DASHBOARD_PROBE = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const pick = name => {
  const start = source.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('missing ' + name);
  let depth = 0, index = source.indexOf('{', start);
  for (; index < source.length; index++) {
    if (source[index] === '{') depth++;
    if (source[index] === '}' && --depth === 0) break;
  }
  return source.slice(start, index + 1);
};
global.mediaItems = [
  {name: 'wide.png', kind: 'png', width: 40, height: 10},
  {name: 'square.jpeg', kind: 'jpeg', width: 32, height: 32},
  {name: 'party.gif', kind: 'gif', width: 64, height: 32},
  {name: 'clip.mp4', kind: 'mp4', width: 128, height: 64},
  {name: 'other.webp', kind: 'webp', width: 64, height: 32},
];
eval(['isStaticMediaSource', 'basicMediaChoices', 'mediaFitNote'].map(pick).join(';'));
const note = (source, fit, extra = {}) => mediaFitNote({source, fit, x: 3, y: 2, width: 30, height: 20, ...extra});
console.log(JSON.stringify({
  choices: basicMediaChoices(),
  staticFlags: ['wide.png', 'square.jpeg', 'party.gif', 'clip.mp4', 'other.webp'].map(isStaticMediaSource),
  contain: note('wide.png', 'contain'),
  cover: note('wide.png', 'cover'),
  stretch: note('wide.png', 'stretch'),
  stretchSameAspect: note('other.webp', 'stretch'),
  manual: note('wide.png', 'manual', {x: 50}),
  manualInside: note('wide.png', 'manual'),
  missing: note('gone.png', 'cover'),
}));
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is required")
class StaticImageDashboardTests(unittest.TestCase):
    def probe(self):
        result = subprocess.run(
            ["node", "-e", DASHBOARD_PROBE, str(ROOT / "app" / "templates" / "dashboard.html")],
            check=True, capture_output=True, text=True, timeout=30,
        )
        return json.loads(result.stdout)

    def test_media_file_choices_include_static_images(self):
        report = self.probe()
        self.assertEqual(
            report["choices"], ["wide.png", "square.jpeg", "party.gif", "clip.mp4", "other.webp"],
        )
        self.assertEqual(report["staticFlags"], [True, True, False, False, True])

    def test_fit_note_makes_clipping_and_stretching_explicit(self):
        report = self.probe()
        self.assertIn("whole image", report["contain"])
        self.assertIn("cropped", report["cover"])
        self.assertIn("64 px cropped from the sides", report["cover"])
        self.assertIn("stretched", report["stretch"])
        self.assertIn("4.00:1", report["stretch"])
        self.assertNotIn("distorted", report["stretchSameAspect"])
        self.assertIn("clipped", report["manual"])
        self.assertNotIn("clipped", report["manualInside"])
        self.assertEqual(report["missing"], "")

    def test_static_image_hides_animation_controls_and_keeps_editable_duration(self):
        template = (ROOT / "app" / "templates" / "dashboard.html").read_text()
        self.assertIn("still=isStaticMediaSource(current.source)", template)
        self.assertIn("data-static-image-hidden", template)
        self.assertIn("isMediaAutomatic=module?.type==='media'&&!isStaticMediaSource(module.source)", template)


if __name__ == "__main__":
    unittest.main()
