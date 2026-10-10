"""Owner request 2026-10-10: a Generate button builds media frames on demand.

The popup offers "Replace all frames" (the timeline becomes the GIF/MP4, or the
still image shows on every frame) and "Generate frames in place" (the media
starts at the highlighted frame and earlier frames are untouched).
"""

import shutil
import unittest

from tests.test_canvas_timeline_rework import ASSET, GIF, run_ops, saved


STILL = {"name": "still.png", "kind": "png", "animated": False, "duration_ms": None, "fps": 1, "source_frames": 1}
FAST_GIF = {"name": "blink.gif", "kind": "gif", "animated": True, "duration_ms": 500, "fps": 10, "source_frames": 5}


def canvas(frame_count=6, fps=10, source="party.gif"):
    """A text layer that moves every frame, plus a media layer."""
    return {
        "id": "custom_gen", "name": "Gen", "type": "canvas", "background": "#000000",
        "frame_count": frame_count, "fps": fps, "playback": "hold",
        "layers": [
            {
                "id": "layer_text", "name": "Text", "type": "text", "visible": True,
                "properties": {"x": 0, "y": 8, "rotation": 0, "text": "HI", "font": "5x7", "colour": "#ffffff"},
                "keyframes": [{"frame": 0, "easing": "linear", "properties": {"x": 0}},
                              {"frame": frame_count - 1, "easing": "linear", "properties": {"x": 30}}],
            },
            {
                "id": "layer_media", "name": "Media", "type": "media", "visible": True,
                "properties": {"present": True, "x": 0, "y": 0, "width": 64, "height": 32, "rotation": 0,
                               "source": source, "keep_aspect": True, "playback_speed_percent": 100.0},
                "keyframes": [{"frame": 0, "easing": "step", "properties": {"x": 0}}],
            },
        ],
        "overrides": [{"frame": 1, "layer_id": "layer_media", "properties": {"present": False}}],
    }


def presence(module, layer_id="layer_media"):
    """Is the layer present on each frame, using the saved-schema resolution rules."""
    layer = next(item for item in module["layers"] if item["id"] == layer_id)
    keys = sorted((key for key in layer["keyframes"] if "present" in key["properties"]), key=lambda key: key["frame"])
    result = []
    for frame in range(module["frame_count"]):
        value = layer["properties"].get("present", True)
        for key in keys:
            if key["frame"] <= frame:
                value = key["properties"]["present"]
        for override in module["overrides"]:
            if override["frame"] == frame and override["layer_id"] == layer_id and "present" in override["properties"]:
                value = override["properties"]["present"]
        result.append(value)
    return result


def generate(module, mode, frame, media):
    report = run_ops(module, {"g": {"op": "generateMedia", "args": ["layer_media", mode, frame]}}, media)
    return report["g"]["result"], report["g"]["module"]


@unittest.skipUnless(shutil.which("node"), "Node.js is required")
class GenerateFramesTests(unittest.TestCase):
    def test_replace_all_frames_makes_the_timeline_the_gif(self):
        result, module = generate(canvas(frame_count=6, fps=20), "replace", 3, [GIF])
        module = saved(module)
        self.assertTrue(result["changed"])
        self.assertEqual((module["frame_count"], module["fps"], module["playback"]), (30, 10, "loop"))
        self.assertEqual(presence(module), [True] * 30)
        self.assertEqual(result["frame"], 0)
        self.assertIn("Generated 30 frames of party.gif at 10 FPS, replacing the timeline", result["message"])
        # The other layer keeps its edits that still fit.
        text = module["layers"][0]
        self.assertEqual([key["frame"] for key in text["keyframes"]], [0, 5])

    def test_replace_all_frames_can_shrink_a_long_timeline(self):
        result, module = generate(canvas(frame_count=40, fps=10, source="blink.gif"), "replace", 0, [FAST_GIF])
        module = saved(module)
        self.assertEqual(module["frame_count"], 5)
        self.assertEqual(module["overrides"], [])

    def test_generate_in_place_starts_at_the_highlighted_frame_and_keeps_earlier_frames(self):
        result, module = generate(canvas(frame_count=6, fps=10), "in_place", 2, [GIF, FAST_GIF])
        module = saved(module)
        self.assertTrue(result["changed"])
        self.assertEqual((module["frame_count"], module["fps"]), (32, 10))
        self.assertEqual(presence(module), [False, False] + [True] * 30)
        self.assertEqual(result["frame"], 2)
        self.assertIn("from frame 3 to 32", result["message"])
        # Frames before the start are unchanged apart from the media layer being hidden there.
        self.assertEqual(module["layers"][0]["keyframes"], canvas(frame_count=6)["layers"][0]["keyframes"])

    def test_generate_in_place_hides_the_media_after_it_has_played(self):
        result, module = generate(canvas(frame_count=12, fps=10, source="blink.gif"), "in_place", 3, [FAST_GIF])
        module = saved(module)
        self.assertEqual(module["frame_count"], 12)
        self.assertEqual(presence(module), [False] * 3 + [True] * 5 + [False] * 4)

    def test_generate_in_place_respects_the_120_frame_limit_and_says_so(self):
        result, module = generate(canvas(frame_count=100, fps=10), "in_place", 99, [GIF])
        self.assertEqual(saved(module)["frame_count"], 120)
        self.assertTrue(result["changed"])
        self.assertIn("120-frame limit", result["message"])

    def test_still_image_replace_shows_it_on_every_frame(self):
        result, module = generate(canvas(frame_count=6, source="still.png"), "replace", 4, [STILL])
        module = saved(module)
        self.assertEqual(module["frame_count"], 6)
        self.assertEqual(presence(module), [True] * 6)
        self.assertIn("now shows on all 6 frames", result["message"])

    def test_still_image_in_place_shows_from_the_highlighted_frame(self):
        result, module = generate(canvas(frame_count=6, source="still.png"), "in_place", 4, [STILL])
        module = saved(module)
        self.assertEqual(presence(module), [False] * 4 + [True] * 2)

    def test_playback_speed_changes_the_generated_length(self):
        module = canvas(frame_count=1, fps=10)
        module["layers"][1]["properties"]["playback_speed_percent"] = 200.0
        _result, module = generate(module, "replace", 0, [GIF])
        self.assertEqual(module["frame_count"], 15)

    def test_refuses_layers_that_are_not_media_or_missing_from_the_library(self):
        report = run_ops(canvas(), {"text": {"op": "generateMedia", "args": ["layer_text", "replace", 0]}}, [GIF])
        self.assertEqual(report["text"]["result"]["reason"], "not_media")
        result, module = generate(canvas(), "replace", 0, [])
        self.assertEqual(result["reason"], "missing_media")
        self.assertEqual(module, canvas())


def owner_added_layer():
    """Exactly what "Add layer > Image/GIF/MP4" creates: the library's first file in a frame-0 keyframe."""
    module = canvas(frame_count=1, fps=20, source="still.png")
    module["overrides"] = []
    module["layers"][0]["keyframes"] = module["layers"][0]["keyframes"][:1]
    media = module["layers"][1]
    media["keyframes"] = [{"frame": 0, "easing": "step", "properties": dict(media["properties"])}]
    return module


def source_on_each_frame(module, layer_id="layer_media"):
    layer = next(item for item in module["layers"] if item["id"] == layer_id)
    keys = sorted((key for key in layer["keyframes"] if "source" in key["properties"]), key=lambda key: key["frame"])
    result = []
    for frame in range(module["frame_count"]):
        value = layer["properties"]["source"]
        for key in keys:
            if key["frame"] <= frame:
                value = key["properties"]["source"]
        for override in module["overrides"]:
            if override["frame"] == frame and override["layer_id"] == layer_id and "source" in override["properties"]:
                value = override["properties"]["source"]
        result.append(value)
    return result


@unittest.skipUnless(shutil.which("node"), "Node.js is required")
class ChosenMediaStaysTests(unittest.TestCase):
    """Owner UAT 2026-10-10 23:03Z: Generate "keeps switching back to the default png"."""

    def test_choosing_a_gif_replaces_the_default_source_on_every_frame(self):
        report = run_ops(owner_added_layer(), {"pick": {"op": "setMediaSource", "args": ["layer_media", "party.gif"]}}, [STILL, GIF])
        module = dict(report["pick"]["module"], frame_count=30)
        self.assertTrue(report["pick"]["result"]["changed"])
        self.assertEqual(source_on_each_frame(saved(module)), ["party.gif"] * 30)

    def test_generate_uses_the_gif_chosen_on_the_highlighted_frame_not_the_default_png(self):
        # Data saved by the old picker: default PNG in the frame-0 keyframe, the GIF only as a frame edit.
        module = owner_added_layer()
        module["overrides"] = [{"frame": 0, "layer_id": "layer_media", "properties": {"source": "party.gif"}}]
        result, generated = generate(module, "replace", 0, [STILL, GIF])
        generated = saved(generated)
        self.assertIn("party.gif", result["message"])
        self.assertEqual((generated["frame_count"], generated["fps"]), (30, 10))
        self.assertEqual(source_on_each_frame(generated), ["party.gif"] * 30)

    def test_media_picker_uses_the_whole_layer_source_setter(self):
        self.assertIn("setLayerMediaSource(module, layer.id, value)", ASSET.read_text())


class GenerateMarkupTests(unittest.TestCase):
    def test_button_and_popup_choices_are_wired(self):
        asset = ASSET.read_text()
        for text in ("'Generate'", "'Generate frames'", "'Replace all frames'", "'Generate frames in place'",
                     "openGenerateDialog(module.id)", "dialog.showModal()"):
            self.assertIn(text, asset)


if __name__ == "__main__":
    unittest.main()
