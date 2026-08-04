import types
import unittest
from unittest.mock import Mock, call, patch

from modules import custom_text


class CustomTextModuleTests(unittest.TestCase):
    @staticmethod
    def module_options(**changes):
        options = {
            "id": "custom_welcome",
            "name": "Welcome",
            "type": "text",
            "text": "HELLO",
            "font": "5x7",
            "colour": "#ffcc00",
            "background": "#001122",
            "x": 3,
            "y": 20,
            "direction": "left",
            "speed": 0.05,
            "loops": 1,
        }
        options.update(changes)
        return options

    @staticmethod
    def canvas_options(**changes):
        options = {
            "id": "custom_canvas",
            "name": "Canvas",
            "type": "canvas",
            "background": "#000000",
            "frame_count": 3,
            "fps": 10,
            "playback": "loop",
            "layers": [{
                "id": "layer_square", "name": "Square", "type": "rectangle", "visible": True,
                "properties": {"x": 0, "y": 0, "width": 10, "height": 10, "rotation": 0, "colour": "#000000", "filled": False},
                "keyframes": [
                    {"frame": 0, "easing": "linear", "properties": {"x": 0, "rotation": 0, "colour": "#000000"}},
                    {"frame": 2, "easing": "linear", "properties": {"x": 20, "rotation": 90, "colour": "#ffffff"}},
                ],
            }],
            "overrides": [],
        }
        options.update(changes)
        return options

    @staticmethod
    def matrix():
        matrix = Mock()
        matrix.matrix.width = 64
        matrix.matrix.height = 32
        matrix.matrix.CreateFrameCanvas.return_value = "canvas"
        matrix.font.return_value = "font"
        def colour(value):
            return types.SimpleNamespace(
                red=int(value[1:3], 16), green=int(value[3:5], 16), blue=int(value[5:7], 16)
            )
        matrix._colours = {value: colour(value) for value in ("#ffcc00", "#001122", "#000000", "#ffffff")}
        matrix.colour_rgb.side_effect = lambda value: matrix._colours.setdefault(value, colour(value))
        matrix.swap.return_value = "canvas"
        return matrix

    def test_static_custom_text_draws_saved_content_and_position(self):
        matrix = self.matrix()
        stop = Mock()
        stop.wait.return_value = False

        result = custom_text.run(
            matrix,
            {},
            duration=4,
            stop_event=stop,
            module_config=self.module_options(),
        )

        self.assertTrue(result)
        matrix.fill.assert_called_once_with(0, 17, 34, canvas="canvas")
        matrix.draw_text.assert_called_once_with(
            "font", 3, 20, matrix._colours["#ffcc00"], "HELLO", canvas="canvas"
        )
        matrix.swap.assert_called_once_with("canvas")
        stop.wait.assert_called_once_with(4)

    def test_scrolling_custom_text_moves_left_using_saved_speed(self):
        matrix = self.matrix()
        matrix.draw_text.return_value = 24
        stop = Mock()
        stop.wait.side_effect = [False, True]
        options = self.module_options(type="text_scroll", direction="left", speed=0.07)

        with patch("modules.custom_text.time.monotonic", side_effect=[0, 0, 0.1]):
            result = custom_text.run(
                matrix,
                {},
                duration=20,
                stop_event=stop,
                module_config=options,
            )

        self.assertTrue(result)
        positions = [entry.args[1] for entry in matrix.draw_text.call_args_list]
        self.assertEqual(positions, [64, 63])
        self.assertEqual(stop.wait.call_args_list, [call(0.07), call(0.07)])

    def test_scrolling_custom_text_stops_after_saved_number_of_loops(self):
        matrix = self.matrix()
        matrix.matrix.width = 3
        matrix.draw_text.return_value = 2
        stop = Mock()
        stop.wait.return_value = False
        options = self.module_options(type="text_scroll", direction="left", loops=2)

        with patch("modules.custom_text.time.monotonic", side_effect=[0] * 14 + [21]):
            result = custom_text.run(
                matrix, {}, duration=20, stop_event=stop, module_config=options,
            )

        self.assertTrue(result)
        self.assertEqual(
            [entry.args[1] for entry in matrix.draw_text.call_args_list],
            [3, 2, 1, 0, -1, -2, 3, 2, 1, 0, -1, -2],
        )

    def test_right_scrolling_custom_text_stops_after_saved_number_of_loops(self):
        matrix = self.matrix()
        matrix.matrix.width = 3
        matrix.draw_text.return_value = 2
        stop = Mock()
        stop.wait.return_value = False
        options = self.module_options(type="text_scroll", direction="right", loops=2)

        with patch("modules.custom_text.time.monotonic", side_effect=[0] * 14 + [21]):
            result = custom_text.run(
                matrix, {}, duration=20, stop_event=stop, module_config=options,
            )

        self.assertTrue(result)
        self.assertEqual(
            [entry.args[1] for entry in matrix.draw_text.call_args_list],
            [0, -2, -1, 0, 1, 2, 3, -2, -1, 0, 1, 2, 3],
        )

    def test_manual_playlist_duration_does_not_truncate_saved_scroll_loops(self):
        matrix = self.matrix()
        matrix.matrix.width = 3
        matrix.draw_text.return_value = 2
        stop = Mock()
        stop.wait.return_value = False
        options = self.module_options(type="text_scroll", direction="left", speed=0.05, loops=2)

        with patch("modules.custom_text.time.monotonic", side_effect=[0, 1]):
            result = custom_text.run(
                matrix, {}, duration=0.01, stop_event=stop, module_config=options,
            )

        self.assertTrue(result)
        self.assertEqual(len(matrix.draw_text.call_args_list), 12)
        self.assertEqual(stop.wait.call_count, 12)

    def test_canvas_text_rotation_reaches_capture_aware_matrix_renderer(self):
        matrix = self.matrix()
        layer = {"id": "layer_text", "name": "Text", "type": "text", "visible": True, "keyframes": []}
        properties = {
            "x": 10, "y": 15, "text": "HI", "font": "5x7",
            "colour": "#ffffff", "rotation": 90,
        }

        custom_text._draw_layer(matrix, "canvas", layer, properties)

        matrix.draw_text.assert_called_once_with(
            "font", 10, 15, matrix._colours["#ffffff"], "HI",
            rotation=90, canvas="canvas",
        )

    def test_canvas_time_rotation_reaches_capture_aware_matrix_renderer(self):
        matrix = self.matrix()
        layer = {"id": "layer_time", "name": "Time", "type": "time", "visible": True, "keyframes": []}
        properties = {
            "x": 8, "y": 14, "format": "24h", "font": "5x7",
            "colour": "#ffffff", "rotation": -45,
        }

        with patch.object(custom_text, "_time_text", return_value="12:34"):
            custom_text._draw_layer(matrix, "canvas", layer, properties)

        matrix.draw_text.assert_called_once_with(
            "font", 8, 14, matrix._colours["#ffffff"], "12:34",
            rotation=-45, canvas="canvas",
        )

    def test_canvas_pixels_rotate_around_their_bounds_centre(self):
        matrix = self.matrix()
        layer = {"id": "layer_pixels", "name": "Pixels", "type": "pixels", "visible": True, "keyframes": []}
        properties = {"points": [[10, 10], [12, 10]], "colour": "#ffffff", "rotation": 90}

        custom_text._draw_layer(matrix, "canvas", layer, properties)

        self.assertEqual(
            matrix.pixel.call_args_list,
            [
                call(11, 9, 255, 255, 255, canvas="canvas"),
                call(11, 11, 255, 255, 255, canvas="canvas"),
            ],
        )

    def test_canvas_line_rotation_uses_its_midpoint_in_physical_renderer(self):
        matrix = self.matrix()
        layer = {"id": "layer_line", "name": "Line", "type": "line", "visible": True, "keyframes": []}
        properties = {
            "x1": 10, "y1": 10, "x2": 20, "y2": 10,
            "width": 1, "colour": "#ffffff", "rotation": 90,
        }

        custom_text._draw_layer(matrix, "canvas", layer, properties)

        matrix.draw_line.assert_called_once_with(
            15, 5, 15, 15, matrix._colours["#ffffff"], width=1, canvas="canvas",
        )

    def test_canvas_keyframes_interpolate_numbers_colours_and_apply_frame_override(self):
        module = self.canvas_options(overrides=[{
            "frame": 1, "layer_id": "layer_square", "properties": {"rotation": 30},
        }])

        properties = custom_text.resolve_layer_properties(
            module["layers"][0], 1, module["overrides"]
        )

        self.assertEqual(properties["x"], 10)
        self.assertEqual(properties["rotation"], 30)
        self.assertEqual(properties["colour"], "#808080")

    def test_canvas_keyframes_keep_base_value_before_first_property_keyframe(self):
        module = self.canvas_options()
        layer = module["layers"][0]
        layer["properties"]["x"] = 3
        layer["keyframes"] = [
            {"frame": 2, "easing": "ease_in", "properties": {"x": 20}},
        ]

        properties = custom_text.resolve_layer_properties(layer, 1, [])

        self.assertEqual(properties["x"], 3)

    def test_canvas_layers_can_change_front_to_back_depth_by_frame(self):
        back = {
            "id": "layer_red", "name": "Red", "type": "rectangle", "visible": True,
            "properties": {"x": 0, "y": 0, "width": 8, "height": 8, "rotation": 0, "colour": "#ff0000", "filled": True, "z_index": 0},
            "keyframes": [
                {"frame": 0, "easing": "linear", "properties": {"z_index": 0}},
                {"frame": 2, "easing": "linear", "properties": {"z_index": 8}},
            ],
        }
        front = {
            "id": "layer_blue", "name": "Blue", "type": "rectangle", "visible": True,
            "properties": {"x": 0, "y": 0, "width": 8, "height": 8, "rotation": 0, "colour": "#0000ff", "filled": True, "z_index": 3},
            "keyframes": [],
        }
        module = self.canvas_options(frame_count=3, layers=[back, front])

        frame_zero = custom_text.ordered_layers_for_frame(module, 0)
        frame_one = custom_text.ordered_layers_for_frame(module, 1)
        frame_two = custom_text.ordered_layers_for_frame(module, 2)

        self.assertEqual([layer["id"] for layer, _ in frame_zero], ["layer_red", "layer_blue"])
        self.assertEqual([layer["id"] for layer, _ in frame_one], ["layer_red", "layer_blue"])
        self.assertEqual([layer["id"] for layer, _ in frame_two], ["layer_blue", "layer_red"])
        legacy = self.canvas_options(layers=[front, back])
        for layer in legacy["layers"]:
            layer["properties"].pop("z_index", None)
            layer["keyframes"] = []
        self.assertEqual(
            [layer["id"] for layer, _ in custom_text.ordered_layers_for_frame(legacy, 0)],
            ["layer_blue", "layer_red"],
        )

    def test_canvas_renderer_draws_keyframed_front_layer_last(self):
        matrix = self.matrix()
        stop = Mock()
        stop.wait.side_effect = [False, False, True]
        red = {
            "id": "layer_red", "name": "Red", "type": "rectangle", "visible": True,
            "properties": {"x": 0, "y": 0, "width": 8, "height": 8, "rotation": 0, "colour": "#ff0000", "filled": True, "z_index": 0},
            "keyframes": [
                {"frame": 0, "easing": "linear", "properties": {"z_index": 0}},
                {"frame": 2, "easing": "linear", "properties": {"z_index": 8}},
            ],
        }
        blue = {
            "id": "layer_blue", "name": "Blue", "type": "rectangle", "visible": True,
            "properties": {"x": 0, "y": 0, "width": 8, "height": 8, "rotation": 0, "colour": "#0000ff", "filled": True, "z_index": 3},
            "keyframes": [],
        }

        with patch("modules.custom_text.time.monotonic", side_effect=[0, 0, 0.1, 0.2]):
            custom_text.run(
                matrix, {}, duration=1, stop_event=stop,
                module_config=self.canvas_options(frame_count=3, layers=[red, blue]),
            )

        colours = [(entry.args[4].red, entry.args[4].green, entry.args[4].blue) for entry in matrix.draw_line.call_args_list]
        frames = [colours[offset:offset + 14] for offset in range(0, len(colours), 14)]
        self.assertEqual(len(frames), 3)
        self.assertEqual([frame[-1] for frame in frames], [(0, 0, 255), (0, 0, 255), (255, 0, 0)])

    def test_canvas_renderer_respects_keyframed_layer_presence(self):
        matrix = self.matrix()
        stop = Mock()
        stop.wait.side_effect = [False, False, True]
        layer = {
            "id": "layer_presence", "name": "Appearing square", "type": "rectangle", "visible": True,
            "properties": {
                "x": 4, "y": 4, "width": 8, "height": 8, "rotation": 0,
                "colour": "#ffffff", "filled": False, "present": False,
            },
            "keyframes": [
                {"frame": 1, "easing": "step", "properties": {"present": True}},
                {"frame": 2, "easing": "step", "properties": {"present": False}},
            ],
        }

        with patch("modules.custom_text.time.monotonic", side_effect=[0, 0, 0.1, 0.2]):
            result = custom_text.run(
                matrix, {}, duration=1, stop_event=stop,
                module_config=self.canvas_options(frame_count=3, layers=[layer]),
            )

        self.assertTrue(result)
        self.assertEqual(matrix.swap.call_count, 3)
        self.assertEqual(matrix.draw_line.call_count, 4)

    def test_canvas_playback_sequences_support_loop_ping_pong_and_hold(self):
        self.assertEqual(custom_text.frame_sequence(3, "loop"), [0, 1, 2])
        self.assertEqual(custom_text.frame_sequence(3, "ping_pong"), [0, 1, 2, 1])
        self.assertEqual(custom_text.frame_sequence(3, "hold"), [0, 1, 2])

    def test_canvas_media_preloading_does_not_consume_module_duration(self):
        matrix = self.matrix()
        clock = [0.0]
        def preload(*_):
            clock[0] = 5.0
            return ["frame"]
        matrix.media_frames.side_effect = preload
        stop = Mock()
        stop.wait.return_value = True
        layer = {
            "id": "layer_media", "name": "Media", "type": "media", "visible": True,
            "properties": {
                "x": 0, "y": 0, "width": 64, "height": 32, "rotation": 0,
                "source": "clip.mp4", "keep_aspect": True,
            },
            "keyframes": [],
        }

        with patch("modules.custom_text.time.monotonic", side_effect=lambda: clock[0]):
            result = custom_text.run(
                matrix, {}, duration=1, stop_event=stop,
                module_config=self.canvas_options(frame_count=1, layers=[layer]),
            )

        self.assertTrue(result)
        matrix.draw_image.assert_called_once()
        matrix.swap.assert_called_once()

    def test_media_frame_index_starts_when_an_inserted_layer_first_appears(self):
        layer = {
            "properties": {"present": False},
            "keyframes": [{
                "frame": 5, "easing": "step", "properties": {"present": True},
            }],
        }

        self.assertEqual(custom_text.media_frame_index(layer, 5), 0)
        self.assertEqual(custom_text.media_frame_index(layer, 6), 1)
        self.assertEqual(custom_text.media_frame_index(layer, 9), 4)

    def test_canvas_renderer_plays_and_resizes_image_gif_and_mp4_layers(self):
        matrix = self.matrix()
        media_frames = {
            "photo.png": ["photo-0", "photo-1", "photo-2"],
            "animation.gif": ["gif-0", "gif-1", "gif-2"],
            "built-in/clip.mp4": ["video-0", "video-1", "video-2"],
        }
        matrix.media_frames.side_effect = lambda source, frame_count, fps: media_frames[source]
        stop = Mock()
        stop.wait.side_effect = [False, True]
        layers = [
            {
                "id": "layer_photo", "name": "Photo", "type": "media", "visible": True,
                "properties": {"x": 1, "y": 2, "width": 20, "height": 10, "rotation": 0, "source": "photo.png", "keep_aspect": True},
                "keyframes": [],
            },
            {
                "id": "layer_gif", "name": "GIF", "type": "media", "visible": True,
                "properties": {"x": 4, "y": 5, "width": 24, "height": 12, "rotation": 15, "source": "animation.gif", "keep_aspect": False},
                "keyframes": [],
            },
            {
                "id": "layer_video", "name": "MP4", "type": "media", "visible": True,
                "properties": {"x": 8, "y": 6, "width": 32, "height": 16, "rotation": 0, "source": "built-in/clip.mp4", "keep_aspect": True},
                "keyframes": [],
            },
        ]

        with patch("modules.custom_text.time.monotonic", side_effect=[0, 0, 0.1]):
            result = custom_text.run(
                matrix, {}, duration=1, stop_event=stop,
                module_config=self.canvas_options(layers=layers),
            )

        self.assertTrue(result)
        self.assertEqual(matrix.media_frames.call_count, 3)
        self.assertEqual(
            [entry.args[0] for entry in matrix.draw_image.call_args_list],
            ["photo-0", "gif-0", "video-0", "photo-1", "gif-1", "video-1"],
        )
        matrix.draw_image.assert_any_call(
            "gif-1", 4, 5, width=24, height=12, keep_aspect=False,
            rotation=15, canvas="canvas",
        )

    def test_canvas_renderer_draws_dynamic_time_layer(self):
        matrix = self.matrix()
        stop = Mock()
        stop.wait.return_value = True
        layer = {
            "id": "layer_time", "name": "Time", "type": "time", "visible": True,
            "properties": {
                "x": 3, "y": 12, "format": "12h_seconds",
                "font": "7x13", "colour": "#ffffff",
            },
            "keyframes": [],
        }

        with patch("modules.custom_text.time.strftime", return_value="09:05:07 PM"):
            result = custom_text.run(
                matrix, {}, duration=1, stop_event=stop,
                module_config=self.canvas_options(frame_count=1, layers=[layer]),
            )

        self.assertTrue(result)
        matrix.draw_text.assert_called_once_with(
            "font", 3, 12, matrix._colours["#ffffff"],
            "9:05:07 PM", canvas="canvas",
        )

    def test_canvas_renderer_draws_each_safe_primitive_on_one_owned_canvas(self):
        matrix = self.matrix()
        stop = Mock()
        stop.wait.return_value = True
        layers = [
            {"id": "layer_text", "name": "Text", "type": "text", "visible": True,
             "properties": {"x": 1, "y": 7, "text": "HI", "font": "5x7", "colour": "#ffffff"}, "keyframes": []},
            {"id": "layer_pixels", "name": "Pixels", "type": "pixels", "visible": True,
             "properties": {"points": [[1, 2], [3, 4]], "colour": "#ffffff"}, "keyframes": []},
            {"id": "layer_line", "name": "Line", "type": "line", "visible": True,
             "properties": {"x1": 0, "y1": 0, "x2": 5, "y2": 5, "width": 1, "colour": "#ffffff"}, "keyframes": []},
            {"id": "layer_rect", "name": "Rect", "type": "rectangle", "visible": True,
             "properties": {"x": 10, "y": 5, "width": 8, "height": 6, "rotation": 25, "colour": "#ffffff", "filled": False}, "keyframes": []},
            {"id": "layer_poly", "name": "Poly", "type": "polygon", "visible": True,
             "properties": {"points": [[20, 5], [25, 10], [20, 15]], "rotation": 0, "colour": "#ffffff", "filled": False}, "keyframes": []},
            {"id": "layer_circle", "name": "Circle", "type": "circle", "visible": True,
             "properties": {"x": 35, "y": 12, "radius": 4, "colour": "#ffffff", "filled": False}, "keyframes": []},
            {"id": "layer_ellipse", "name": "Ellipse", "type": "ellipse", "visible": True,
             "properties": {"x": 50, "y": 16, "radius_x": 6, "radius_y": 3, "rotation": 15, "colour": "#ffffff", "filled": False}, "keyframes": []},
        ]

        result = custom_text.run(
            matrix, {}, duration=1, stop_event=stop,
            module_config=self.canvas_options(frame_count=1, layers=layers),
        )

        self.assertTrue(result)
        matrix.draw_text.assert_called_once()
        self.assertEqual(matrix.pixel.call_count, 2)
        self.assertGreaterEqual(matrix.draw_line.call_count, 8)
        matrix.draw_circle.assert_called_once()
        matrix.swap.assert_called_once_with("canvas")
        stop.wait.assert_called_once_with(0.1)


if __name__ == "__main__":
    unittest.main()
