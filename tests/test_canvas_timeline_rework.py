"""Roadmap 1.8: clear Advanced Canvas timeline operations and animated-media import."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from config_store import DEFAULT_CONFIG, validate_config
from edit_preview import CaptureMatrix
from modules.custom_text import canvas_timeline_frames


ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "app" / "static" / "custom_canvas.js"


def keyed_module():
    """Three step-keyed frames whose pixels differ, so frame order is observable."""
    def key(frame, x, colour):
        return {"frame": frame, "easing": "step", "properties": {"x": x, "colour": colour}}
    return {
        "id": "custom_timeline", "name": "Timeline", "type": "canvas", "background": "#000000",
        "frame_count": 3, "fps": 4, "playback": "loop",
        "layers": [{
            "id": "layer_box", "name": "Box", "type": "rectangle", "visible": True,
            "properties": {"x": 0, "y": 4, "width": 6, "height": 6, "rotation": 0, "colour": "#ff0000", "filled": True},
            "keyframes": [key(0, 2, "#ff0000"), key(1, 20, "#00ff00"), key(2, 40, "#0000ff")],
        }],
        "overrides": [{"frame": 1, "layer_id": "layer_box", "properties": {"y": 12}}],
    }


HARNESS = r"""
const fs = require('fs');
global.window = global;
global.document = {documentElement: null, querySelectorAll: () => [], querySelector: () => ({replaceChildren() {}, append() {}}), createElement: () => ({})};
global.CSS = {escape: value => value};
global.customModules = [];
global.mediaItems = JSON.parse(process.argv[2]);
global.confirm = () => true;
global.setInterval = () => 0;
global.syncCustomModuleUI = () => {};
global.syncPlaylistDurations = () => {};
global.setMessage = () => {};
eval(fs.readFileSync(process.argv[1], 'utf8'));
const ops = global.matrixTimelineFrames;
if (!ops || !Object.isFrozen(ops)) throw new Error('missing frozen timeline frame operations');
const input = JSON.parse(process.argv[3]);
const out = {};
for (const [name, call] of Object.entries(input.calls)) {
  const module = JSON.parse(JSON.stringify(input.module));
  const result = ops[call.op](module, ...call.args);
  out[name] = {result: {changed: result.changed, reason: result.reason || null, frame: result.frame ?? null, message: result.message || null}, module: result.module};
}
console.log(JSON.stringify(out));
"""


def run_ops(module, calls, media_items=()):
    result = subprocess.run(
        ["node", "-e", HARNESS, str(ASSET), json.dumps(list(media_items)), json.dumps({"module": module, "calls": calls})],
        check=True, capture_output=True, text=True, timeout=30,
    )
    return json.loads(result.stdout)


def rendered(module):
    matrix = CaptureMatrix()
    return [
        matrix.snapshot(canvas).convert("RGB").tobytes()
        for canvas, _delay in canvas_timeline_frames(matrix, module["frame_count"] / module["fps"], None, module)
    ][:module["frame_count"]]


def saved(module):
    candidate = json.loads(json.dumps(DEFAULT_CONFIG))
    candidate["custom_modules"] = [module]
    candidate["playlist"].append({"module": module["id"], "duration": 5, "enabled": True})
    return validate_config(candidate)["custom_modules"][0]


@unittest.skipUnless(shutil.which("node"), "Node.js is required")
class TimelineFrameOperationTests(unittest.TestCase):
    def test_move_frame_swaps_whole_frames_and_physical_order_follows(self):
        original = rendered(keyed_module())
        report = run_ops(keyed_module(), {
            "later": {"op": "moveFrame", "args": [0, 1]},
            "earlier": {"op": "moveFrame", "args": [2, -1]},
            "edge": {"op": "moveFrame", "args": [0, -1]},
        })
        later = saved(report["later"]["module"])
        self.assertTrue(report["later"]["result"]["changed"])
        self.assertEqual(report["later"]["result"]["frame"], 1)
        self.assertEqual(rendered(later), [original[1], original[0], original[2]])
        self.assertEqual(rendered(saved(report["earlier"]["module"])), [original[0], original[2], original[1]])
        self.assertFalse(report["edge"]["result"]["changed"])
        self.assertEqual(report["edge"]["result"]["reason"], "out_of_bounds")
        self.assertEqual(report["edge"]["module"], keyed_module())

    def test_delete_frame_removes_its_content_and_closes_the_gap(self):
        original = rendered(keyed_module())
        report = run_ops(keyed_module(), {"middle": {"op": "deleteFrame", "args": [1]}})
        module = saved(report["middle"]["module"])
        self.assertTrue(report["middle"]["result"]["changed"])
        self.assertEqual(module["frame_count"], 2)
        self.assertEqual(module["overrides"], [])
        self.assertEqual([item["frame"] for item in module["layers"][0]["keyframes"]], [0, 1])
        self.assertEqual(rendered(module), [original[0], original[2]])

    def test_delete_refuses_the_last_frame(self):
        single = dict(keyed_module(), frame_count=1, overrides=[])
        single["layers"] = [dict(single["layers"][0], keyframes=[single["layers"][0]["keyframes"][0]])]
        report = run_ops(single, {"only": {"op": "deleteFrame", "args": [0]}})
        self.assertFalse(report["only"]["result"]["changed"])
        self.assertEqual(report["only"]["result"]["reason"], "last_frame")
        self.assertEqual(report["only"]["module"], single)

    def test_insert_frame_holds_the_current_picture_and_shifts_later_frames(self):
        original = rendered(keyed_module())
        report = run_ops(keyed_module(), {"after_first": {"op": "insertFrame", "args": [0]}})
        module = saved(report["after_first"]["module"])
        self.assertEqual(report["after_first"]["result"]["frame"], 1)
        self.assertEqual(module["frame_count"], 4)
        self.assertEqual(rendered(module), [original[0], original[0], original[1], original[2]])

    def test_insert_refuses_beyond_the_120_frame_limit(self):
        full = dict(keyed_module(), frame_count=120)
        report = run_ops(full, {"full": {"op": "insertFrame", "args": [119]}})
        self.assertFalse(report["full"]["result"]["changed"])
        self.assertEqual(report["full"]["result"]["reason"], "frame_limit")

    def test_playhead_readout_states_frame_time_rate_and_mode(self):
        report = run_ops(dict(keyed_module(), frame_count=24, fps=12, playback="ping_pong"), {
            "readout": {"op": "playheadText", "args": [2]},
        })
        self.assertEqual(report["readout"]["result"]["message"], "Frame 3 of 24 · 0.17 s of 2.00 s · 12 FPS · Ping-pong")


GIF = {"name": "party.gif", "kind": "gif", "animated": True, "duration_ms": 3000, "fps": 10, "source_frames": 30}
LONG_MP4 = {"name": "long.mp4", "kind": "mp4", "animated": True, "duration_ms": 20000, "fps": 6, "source_frames": 600}


def media_module(frame_count=1, fps=12):
    return {
        "id": "custom_media", "name": "Media", "type": "canvas", "background": "#000000",
        "frame_count": frame_count, "fps": fps, "playback": "hold",
        "layers": [{
            "id": "layer_media", "name": "Media", "type": "media", "visible": True,
            "properties": {"x": 0, "y": 0, "width": 64, "height": 32, "rotation": 0, "source": "still.png", "keep_aspect": True},
            "keyframes": [{"frame": 0, "easing": "step", "properties": {"x": 0}}],
        }],
        "overrides": [],
    }


@unittest.skipUnless(shutil.which("node"), "Node.js is required")
class AnimatedMediaImportTests(unittest.TestCase):
    def test_choosing_a_gif_on_an_existing_layer_builds_every_frame_at_source_timing(self):
        report = run_ops(media_module(), {"gif": {"op": "adoptMediaSource", "args": ["layer_media", "party.gif", 0]}}, [GIF])
        module = saved(report["gif"]["module"])
        self.assertTrue(report["gif"]["result"]["changed"])
        self.assertEqual((module["frame_count"], module["fps"], module["playback"]), (30, 10, "loop"))
        self.assertIn("30 timeline frames", report["gif"]["result"]["message"])

    def test_single_frame_canvas_adopts_gif_timing_even_with_frame_edits(self):
        module = media_module()
        module["overrides"] = [{"frame": 0, "layer_id": "layer_media", "properties": {"source": "party.gif"}}]
        report = run_ops(module, {"gif": {"op": "adoptMediaSource", "args": ["layer_media", "party.gif", 0]}}, [GIF])
        result = report["gif"]["module"]
        self.assertEqual((result["frame_count"], result["fps"], result["playback"]), (30, 10, "loop"))

    def test_existing_animation_keeps_its_frame_rate_when_media_is_chosen(self):
        report = run_ops(media_module(frame_count=4, fps=20), {"gif": {"op": "adoptMediaSource", "args": ["layer_media", "party.gif", 0]}}, [GIF])
        module = report["gif"]["module"]
        self.assertEqual((module["frame_count"], module["fps"]), (60, 20))

    def test_media_longer_than_120_frames_explains_what_was_clipped(self):
        report = run_ops(media_module(frame_count=2, fps=12), {"mp4": {"op": "adoptMediaSource", "args": ["layer_media", "long.mp4", 0]}}, [LONG_MP4])
        module = report["mp4"]["module"]
        self.assertEqual(module["frame_count"], 120)
        message = report["mp4"]["result"]["message"]
        self.assertIn("first 10.00 s of 20.00 s", message)
        self.assertIn("6 FPS or lower", message)

    def test_still_media_leaves_the_timeline_alone(self):
        report = run_ops(media_module(), {"still": {"op": "adoptMediaSource", "args": ["layer_media", "still.png", 0]}},
                         [{"name": "still.png", "kind": "png", "animated": False}])
        self.assertFalse(report["still"]["result"]["changed"])
        self.assertEqual(report["still"]["module"]["frame_count"], 1)


class TimelineMarkupTests(unittest.TestCase):
    def test_timeline_groups_labels_and_keyboard_are_wired(self):
        asset = ASSET.read_text()
        for text in (
            "'Playhead'", "'Frames'", "'Selected layer on this frame'",
            "'Duplicate frame'", "'Delete frame'", "'Move frame earlier'", "'Move frame later'",
            "aria-keyshortcuts", "'ArrowLeft'", "'ArrowRight'", "'Home'", "'End'",
            "setAttribute('aria-current', 'true')", "timeline-playhead",
        ):
            self.assertIn(text, asset)
        self.assertIn("adoptMediaSource(module, layer.id, value, state.frame)", asset)


if __name__ == "__main__":
    unittest.main()
