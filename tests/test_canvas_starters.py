"""Every advanced-canvas starter example must save, render and animate on the 64x32 matrix."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from config_store import ConfigStore
from edit_preview import CaptureMatrix
from modules.custom_text import canvas_timeline_frames


ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "app" / "static" / "custom_canvas.js"
EXPECTED_STARTERS = (
    "spinning_square",
    "spinning_rgb_cube",
    "three_lines_circle",
    "matrix_rain",
    "rainbow_pulse",
    "neon_clock",
    "starfield_warp",
    "bouncing_ball",
    "equalizer_bars",
    "pixel_heart",
    "chomp_chase",
    "fireworks",
    "sunset",
    "rainbow_marquee",
)

# Loads the real dashboard asset in Node with a minimal DOM stub and applies each starter to a
# fresh single-frame canvas draft, exactly as the "Use example" button does.
STARTER_PROBE = r"""
const fs = require('fs');
global.window = global;
const list = {replaceChildren: () => {}, append: () => {}};
global.document = {
  querySelectorAll: () => [],
  querySelector: selector => (selector === '#custom-modules' ? list : null),
  createElement: () => ({}),
};
global.CSS = {escape: value => value};
global.customModules = [];
global.mediaItems = [];
global.confirm = () => true;
global.setInterval = () => 0;
global.syncCustomModuleUI = () => {};
global.syncPlaylistDurations = () => {};
global.setMessage = () => {};
global.atob = value => Buffer.from(value, 'base64').toString('binary');
global.btoa = value => Buffer.from(value, 'binary').toString('base64');
eval(fs.readFileSync(process.argv[1], 'utf8'));
global.renderCustomModules = () => {};
const base = {
  id: 'custom_example', name: 'Example', type: 'canvas', background: '#000000',
  frame_count: 1, fps: 10, playback: 'hold',
  layers: [{id: 'layer_initial', name: 'Initial', type: 'text', visible: true,
    properties: {x: 1, y: 7, text: 'X', font: '4x6', colour: '#ffffff'}, keyframes: []}],
  overrides: [],
};
const out = [];
for (const example of canvasStarterExamples) {
  global.customModules = [structuredClone(base)];
  applyCanvasTemplate('custom_example', example.id);
  const twice = structuredClone(global.customModules[0]);
  global.customModules = [structuredClone(base)];
  applyCanvasTemplate('custom_example', example.id);
  out.push({id: example.id, label: example.label, module: global.customModules[0], twice});
}
process.stdout.write(JSON.stringify(out));
"""


def strip_layer_ids(module):
    return [{key: value for key, value in layer.items() if key != "id"} for layer in module["layers"]]


@unittest.skipUnless(shutil.which("node"), "Node.js is required")
class CanvasStarterExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        node = shutil.which("node")
        assert node is not None
        completed = subprocess.run(
            [node, "-e", STARTER_PROBE, str(ASSET)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        cls.starters = json.loads(completed.stdout)

    def test_starter_catalogue_is_remastered_and_extended(self):
        self.assertEqual(tuple(starter["id"] for starter in self.starters), EXPECTED_STARTERS)
        labels = [starter["label"] for starter in self.starters]
        self.assertEqual(len(set(labels)), len(labels))

    def test_every_starter_is_an_animated_loop_that_the_config_store_accepts(self):
        for starter in self.starters:
            module = starter["module"]
            with self.subTest(starter=starter["id"]):
                self.assertGreaterEqual(module["frame_count"], 24)
                self.assertIn(module["playback"], {"loop", "ping_pong"})
                self.assertTrue(any(layer["keyframes"] for layer in module["layers"]))
                self.assertEqual(module["overrides"], [])
                with tempfile.TemporaryDirectory() as directory:
                    store = ConfigStore(Path(directory) / "config.json")
                    store.update({"custom_modules": [module]})
                    self.assertEqual(store.get()["custom_modules"][0]["layers"], module["layers"])

    def test_every_starter_is_deterministic_apart_from_fresh_layer_ids(self):
        for starter in self.starters:
            with self.subTest(starter=starter["id"]):
                self.assertEqual(strip_layer_ids(starter["module"]), strip_layer_ids(starter["twice"]))
                ids = [layer["id"] for layer in starter["module"]["layers"]]
                self.assertEqual(len(set(ids)), len(ids))

    def test_every_starter_draws_visible_motion_on_the_physical_renderer(self):
        for starter in self.starters:
            module = starter["module"]
            with self.subTest(starter=starter["id"]):
                matrix = CaptureMatrix()
                frames = [
                    matrix.snapshot(canvas).convert("RGB").tobytes()
                    for canvas, _delay in canvas_timeline_frames(matrix, module["frame_count"] / module["fps"], None, module)
                ]
                self.assertGreaterEqual(len(frames), module["frame_count"])
                background = bytes.fromhex(module["background"][1:])
                lit = [sum(frame[i:i + 3] != background for i in range(0, len(frame), 3)) for frame in frames]
                # Readable on the panel: content on (nearly) every frame, and it actually moves.
                self.assertGreaterEqual(sum(count > 0 for count in lit), len(frames) * .9, lit)
                self.assertGreaterEqual(max(lit), 60, lit)
                self.assertGreater(len(set(frames)), len(frames) // 4)


if __name__ == "__main__":
    unittest.main()
