import json
import tempfile
import unittest
from pathlib import Path

from config_store import ConfigStore, DEFAULT_CONFIG


class ConfigStoreTests(unittest.TestCase):
    @staticmethod
    def advanced_module(**changes):
        module = {
            "id": "custom_spinner",
            "name": "Spinning square",
            "type": "canvas",
            "background": "#000000",
            "frame_count": 24,
            "fps": 12,
            "playback": "loop",
            "layers": [{
                "id": "layer_square",
                "name": "Square",
                "type": "rectangle",
                "visible": True,
                "properties": {
                    "x": 24, "y": 8, "width": 16, "height": 16,
                    "rotation": 0, "colour": "#00ffff", "filled": False,
                },
                "keyframes": [
                    {"frame": 0, "easing": "linear", "properties": {"rotation": 0}},
                    {"frame": 23, "easing": "ease_in_out", "properties": {"rotation": 360}},
                ],
            }],
            "overrides": [{
                "frame": 12,
                "layer_id": "layer_square",
                "properties": {"colour": "#ff00ff"},
            }],
        }
        module.update(changes)
        return module

    def test_legacy_built_in_media_references_migrate_into_built_in_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            media = Path(directory) / "media"
            built_in = media / "built-in"
            built_in.mkdir(parents=True)
            (built_in / "squares_tb.gif").write_bytes(b"bundled")
            (built_in / "sleep.gif").write_bytes(b"bundled")
            legacy = json.loads(json.dumps(DEFAULT_CONFIG))
            legacy["media"] = {"splash": "squares_tb.gif", "sleep": "sleep.gif"}
            legacy["custom_modules"] = [{
                "id": "custom_media", "name": "Legacy media", "type": "canvas",
                "background": "#000000", "frame_count": 2, "fps": 10, "playback": "hold",
                "layers": [{
                    "id": "layer_media", "name": "Media", "type": "media", "visible": True,
                    "properties": {
                        "x": 0, "y": 0, "width": 64, "height": 32, "rotation": 0,
                        "source": "sleep.gif", "keep_aspect": True,
                    },
                    "keyframes": [{
                        "frame": 1, "easing": "linear", "properties": {"source": "sleep.gif"},
                    }],
                }],
                "overrides": [{
                    "frame": 0, "layer_id": "layer_media", "properties": {"source": "sleep.gif"},
                }],
            }]
            path.write_text(json.dumps(legacy))

            config = ConfigStore(path, media_dir=media).get()

            self.assertEqual(config["media"]["splash"], "built-in/squares_tb.gif")
            self.assertEqual(config["media"]["sleep"], "built-in/sleep.gif")
            layer = config["custom_modules"][0]["layers"][0]
            self.assertEqual(layer["properties"]["source"], "built-in/sleep.gif")
            self.assertEqual(layer["keyframes"][0]["properties"]["source"], "built-in/sleep.gif")
            self.assertEqual(
                config["custom_modules"][0]["overrides"][0]["properties"]["source"],
                "built-in/sleep.gif",
            )

    def test_intentionally_demoted_bundled_media_is_not_migrated_back_on_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            media = Path(directory) / "media"
            media.mkdir()
            (media / "sleep.gif").write_bytes(b"demoted")
            saved = json.loads(json.dumps(DEFAULT_CONFIG))
            saved["media"]["sleep"] = "sleep.gif"
            path.write_text(json.dumps(saved))

            config = ConfigStore(path, media_dir=media).get()

            self.assertEqual(config["media"]["sleep"], "sleep.gif")

    def test_missing_file_creates_defaults_and_round_trips_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path)
            self.assertEqual(store.get()["weather"]["location"], "Brighton, GB")
            self.assertTrue(path.exists())

            updated = store.update({"weather": {"location": "London, GB"}})
            self.assertEqual(updated["weather"]["location"], "London, GB")
            self.assertEqual(updated["display"]["day_brightness"], DEFAULT_CONFIG["display"]["day_brightness"])
            self.assertEqual(json.loads(path.read_text())["weather"]["location"], "London, GB")

    def test_pairing_capacity_defaults_to_four_and_accepts_only_one_through_ten(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            self.assertEqual(store.get()["pairing"]["max_devices"], 4)
            for value in (1, 10):
                self.assertEqual(
                    store.update({"pairing": {"max_devices": value}})["pairing"]["max_devices"],
                    value,
                )
            for value in (0, 11, True, 4.5, "4"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    store.update({"pairing": {"max_devices": value}})

    def test_rejects_invalid_module_names_brightness_and_media_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            with self.assertRaises(ValueError):
                store.update({"playlist": [{"module": "shell", "duration": 1, "enabled": True}]})
            with self.assertRaises(ValueError):
                store.update({"display": {"day_brightness": 101}})
            for filename in ("../session.key", "/etc/shadow", 123, "payload.svg"):
                with self.subTest(filename=filename), self.assertRaises(ValueError):
                    store.update({"media": {"splash": filename}})
            with self.assertRaises(ValueError):
                store.update({"unknown": True})
            with self.assertRaises(ValueError):
                store.update({"device": {"name": ""}})

    def test_accepts_supported_uploaded_image_extensions(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            for filename in ("custom.gif", "custom.png", "custom.jpg", "custom.jpeg", "custom.webp"):
                with self.subTest(filename=filename):
                    updated = store.update({"media": {"splash": filename}})
                    self.assertEqual(updated["media"]["splash"], filename)

    def test_weather_location_accepts_selected_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            updated = store.update({"weather": {
                "location": "Brighton, England, GB",
                "latitude": 50.8225,
                "longitude": -0.1372,
            }})
            self.assertEqual(updated["weather"]["latitude"], 50.8225)
            with self.assertRaises(ValueError):
                store.update({"weather": {"latitude": 91}})

    def test_weather_display_options_are_saved_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            updated = store.update({"weather": {
                "units": "fahrenheit",
                "temperature_y": 18,
                "ticker_y": 29,
                "scroll_direction": "right",
                "scroll_speed": "slow",
            }})
            self.assertEqual(updated["weather"]["units"], "fahrenheit")
            self.assertEqual(updated["weather"]["temperature_y"], 18)
            self.assertEqual(updated["weather"]["ticker_y"], 29)
            self.assertEqual(updated["weather"]["scroll_direction"], "right")
            self.assertEqual(updated["weather"]["scroll_speed"], "slow")

            invalid_values = {
                "units": "kelvin",
                "temperature_y": 32,
                "ticker_y": -1,
                "scroll_direction": "up",
                "scroll_speed": "warp",
            }
            for key, value in invalid_values.items():
                with self.subTest(key=key), self.assertRaises(ValueError):
                    store.update({"weather": {key: value}})

    def test_weather_elements_can_move_to_every_matrix_edge(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")

            updated = store.update({"weather": {
                "now_x": -63, "now_y": 0,
                "temperature_x": 63, "temperature_y": 31,
                "high_x": -63, "high_y": 31,
                "low_x": 63, "low_y": 0,
                "line_y": 0,
                "ticker_y": 31,
            }})

            self.assertEqual((updated["weather"]["now_x"], updated["weather"]["now_y"]), (-63, 0))
            self.assertEqual((updated["weather"]["temperature_x"], updated["weather"]["temperature_y"]), (63, 31))
            self.assertEqual((updated["weather"]["line_y"], updated["weather"]["ticker_y"]), (0, 31))

            for key, value in (
                ("now_x", -64), ("temperature_x", 64),
                ("now_y", -1), ("high_y", 32),
                ("temperature_y", -1), ("ticker_y", 32),
            ):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    store.update({"weather": {key: value}})

    def test_builtin_module_settings_are_saved_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            updated = store.update({
                "weather": {
                    "now_x": 3, "now_y": 8, "temperature_x": 4,
                    "high_x": 31, "high_y": 10, "low_x": 32, "low_y": 18,
                    "line_y": 22, "background_colour": "#010203",
                    "text_colour": "#aabbcc", "high_colour": "#ff0000",
                    "low_colour": "#00ff00", "line_colour": "#0000ff",
                    "ticker_colour": "#ffff00",
                },
                "lava": {
                    "blob_count": 6, "blob_min_radius": 3, "blob_max_radius": 12,
                    "blob_speed": 0.4, "field_threshold": 1.5,
                    "blur_radius": 1, "edge_width": 3,
                    "show_time": False, "show_date": True,
                    "time_font": "7x13B", "date_font": "6x10",
                    "time_align": "left", "date_align": "right",
                    "time_y": 16, "date_y": 29,
                    "text_colour": "#eeeeee", "border_colour": "#111111",
                },
                "media_layout": {
                    "splash": {"x": 2, "y": 1, "width": 60, "height": 30, "keep_aspect": True, "video_fps": 24},
                    "sleep": {"x": 0, "y": 0, "width": 64, "height": 32, "keep_aspect": False, "video_fps": 12},
                },
            })
            self.assertEqual(updated["weather"]["line_y"], 22)
            self.assertEqual((updated["weather"]["high_x"], updated["weather"]["low_y"]), (31, 18))
            self.assertEqual(updated["lava"]["blob_count"], 6)
            self.assertEqual(updated["lava"]["time_align"], "left")
            self.assertEqual(updated["media_layout"]["splash"]["width"], 60)

            invalid_updates = (
                {"weather": {"line_y": 32}},
                {"weather": {"high_x": 64}},
                {"weather": {"high_colour": "red"}},
                {"lava": {"blob_count": 0}},
                {"lava": {"blob_min_radius": 13, "blob_max_radius": 12}},
                {"lava": {"time_align": "middle"}},
                {"media_layout": {"splash": {"width": 65}}},
                {"media_layout": {"sleep": {"keep_aspect": "yes"}}},
            )
            for change in invalid_updates:
                with self.subTest(change=change), self.assertRaises(ValueError):
                    store.update(change)

    def test_lava_palettes_can_be_checked_unchecked_and_customized(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            updated = store.update({"lava": {
                "enabled_palettes": ["fire_burst", "ocean_wave"],
                "custom_enabled": True,
                "custom_colours": ["#102030", "#A0B0C0", "#ffffff"],
            }})
            self.assertEqual(updated["lava"]["enabled_palettes"], ["fire_burst", "ocean_wave"])
            self.assertTrue(updated["lava"]["custom_enabled"])
            self.assertEqual(updated["lava"]["custom_colours"], ["#102030", "#a0b0c0", "#ffffff"])

            with self.assertRaises(ValueError):
                store.update({"lava": {"enabled_palettes": ["not-a-palette"]}})
            with self.assertRaises(ValueError):
                store.update({"lava": {"enabled_palettes": [], "custom_enabled": False}})
            with self.assertRaises(ValueError):
                store.update({"lava": {"custom_colours": ["red", "#000000", "#ffffff"]}})

    def test_legacy_single_custom_palette_is_migrated_to_saved_palette(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            legacy = json.loads(json.dumps(DEFAULT_CONFIG))
            legacy["lava"].pop("custom_palettes")
            legacy["lava"]["custom_enabled"] = True
            legacy["lava"]["custom_colours"] = ["#102030", "#A0B0C0", "#ffffff"]
            path.write_text(json.dumps(legacy))

            updated = ConfigStore(path).get()

            self.assertFalse(updated["lava"]["custom_enabled"])
            self.assertEqual(updated["lava"]["custom_palettes"], [{
                "id": "palette_migrated",
                "name": "My custom palette",
                "colours": ["#102030", "#a0b0c0", "#ffffff"],
                "enabled": True,
            }])

    def test_multiple_named_custom_lava_palettes_can_be_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            palettes = [
                {
                    "id": "palette_sunset",
                    "name": "Sunset mix",
                    "colours": ["#101020", "#FF6600", "#ffee99"],
                    "enabled": True,
                },
                {
                    "id": "palette_ocean",
                    "name": "Deep ocean",
                    "colours": ["#000010", "#0066aa", "#00ffff"],
                    "enabled": False,
                },
            ]

            updated = store.update({"lava": {
                "enabled_palettes": [],
                "custom_enabled": False,
                "custom_palettes": palettes,
            }})

            self.assertEqual(updated["lava"]["custom_palettes"][0]["name"], "Sunset mix")
            self.assertEqual(
                updated["lava"]["custom_palettes"][0]["colours"],
                ["#101020", "#ff6600", "#ffee99"],
            )
            self.assertFalse(updated["lava"]["custom_palettes"][1]["enabled"])

    def test_custom_lava_palettes_reject_duplicate_names_ids_and_unsafe_values(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            base = {
                "id": "palette_one",
                "name": "My colours",
                "colours": ["#000000", "#112233", "#ffffff"],
                "enabled": True,
            }
            for second in (
                {**base, "name": "Other"},
                {**base, "id": "palette_two", "name": " my colours "},
                {**base, "id": "bad/path", "name": "Other"},
                {**base, "id": "palette_two", "name": "Other", "colours": ["red"] * 3},
            ):
                with self.subTest(second=second), self.assertRaises(ValueError):
                    store.update({"lava": {"custom_palettes": [base, second]}})

    def test_custom_text_modules_can_be_added_to_playlist(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            custom = {
                "id": "custom_welcome",
                "name": "Welcome message",
                "type": "text_scroll",
                "text": "Welcome to the Matrix",
                "font": "5x7",
                "colour": "#ffcc00",
                "background": "#001122",
                "x": 2,
                "y": 20,
                "direction": "left",
                "speed": 0.05,
            }
            updated = store.update({
                "custom_modules": [custom],
                "playlist": [{"module": "custom_welcome", "duration": 12, "enabled": True}],
            })
            self.assertEqual(updated["custom_modules"][0]["text"], "Welcome to the Matrix")
            self.assertEqual(updated["playlist"][0]["module"], "custom_welcome")

    def test_scrolling_custom_text_loop_count_defaults_to_one_and_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            custom = {
                "id": "custom_looped", "name": "Looped message", "type": "text_scroll",
                "text": "LOOP", "font": "5x7", "colour": "#ffffff",
                "background": "#000000", "x": 2, "y": 20,
                "direction": "left", "speed": 0.05,
            }

            saved = store.update({"custom_modules": [custom]})["custom_modules"][0]
            self.assertEqual(saved["loops"], 1)

            custom["loops"] = 10
            saved = store.update({"custom_modules": [custom]})["custom_modules"][0]
            self.assertEqual(saved["loops"], 10)

            for loops in (0, 11, True):
                custom["loops"] = loops
                with self.subTest(loops=loops), self.assertRaisesRegex(ValueError, "loops"):
                    store.update({"custom_modules": [custom]})

    def test_scrolling_custom_text_playlist_duration_is_calculated_from_loops_and_frame_delay(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            custom = {
                "id": "custom_timed", "name": "Timed message", "type": "text_scroll",
                "text": "HI", "font": "5x7", "colour": "#ffffff",
                "background": "#000000", "x": 2, "y": 20,
                "direction": "left", "speed": 0.2, "loops": 3,
            }

            updated = store.update({
                "custom_modules": [custom],
                "playlist": [{"module": "custom_timed", "duration": 1, "enabled": True}],
            })

            self.assertEqual(updated["playlist"][0]["duration"], 45.0)

            custom.update(text="A", font="4x6", speed=0.0115, loops=1)
            updated = store.update({
                "custom_modules": [custom],
                "playlist": [{"module": "custom_timed", "duration": 1, "enabled": True}],
            })
            self.assertEqual(updated["playlist"][0]["duration"], 0.794)

    def test_legacy_scrolling_text_without_loop_count_migrates_on_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path)
            legacy = store.get()
            legacy["custom_modules"] = [{
                "id": "custom_legacy", "name": "Legacy message", "type": "text_scroll",
                "text": "LEGACY", "font": "5x7", "colour": "#ffffff",
                "background": "#000000", "x": 2, "y": 20,
                "direction": "left", "speed": 0.05,
            }]
            legacy["playlist"].append({"module": "custom_legacy", "duration": 15, "enabled": True})
            path.write_text(json.dumps(legacy))

            migrated = ConfigStore(path).get()

            self.assertEqual(migrated["custom_modules"][0]["loops"], 1)

    def test_advanced_canvas_module_can_be_saved_and_added_to_playlist(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            advanced = self.advanced_module()

            updated = store.update({
                "custom_modules": [advanced],
                "playlist": [{"module": advanced["id"], "duration": 12, "enabled": True}],
            })

            saved = updated["custom_modules"][0]
            self.assertEqual(saved["type"], "canvas")
            self.assertEqual(saved["layers"][0]["keyframes"][1]["properties"]["rotation"], 360)
            self.assertEqual(saved["overrides"][0]["frame"], 12)

    def test_advanced_canvas_module_supports_safe_matrix_primitives(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            layers = [
                {"id": "layer_text", "name": "Text", "type": "text", "visible": True,
                 "properties": {"x": 1, "y": 7, "text": "HELLO", "font": "5x7", "colour": "#ffffff"}, "keyframes": []},
                {"id": "layer_pixels", "name": "Pixels", "type": "pixels", "visible": True,
                 "properties": {"points": [[1, 2], [3, 4]], "colour": "#ff0000"}, "keyframes": []},
                {"id": "layer_line", "name": "Line", "type": "line", "visible": True,
                 "properties": {"x1": 0, "y1": 0, "x2": 63, "y2": 31, "width": 2, "colour": "#00ff00"}, "keyframes": []},
                {"id": "layer_polygon", "name": "Polygon", "type": "polygon", "visible": True,
                 "properties": {"points": [[10, 10], [20, 10], [15, 20]], "rotation": 0, "colour": "#0000ff", "filled": True}, "keyframes": []},
                {"id": "layer_circle", "name": "Circle", "type": "circle", "visible": True,
                 "properties": {"x": 32, "y": 16, "radius": 8, "colour": "#ffff00", "filled": False}, "keyframes": []},
                {"id": "layer_ellipse", "name": "Ellipse", "type": "ellipse", "visible": True,
                 "properties": {"x": 32, "y": 16, "radius_x": 12, "radius_y": 6, "rotation": 20, "colour": "#ff00ff", "filled": False}, "keyframes": []},
                self.advanced_module()["layers"][0],
            ]
            advanced = self.advanced_module(layers=layers, overrides=[])

            saved = store.update({"custom_modules": [advanced]})["custom_modules"][0]

            self.assertEqual([layer["type"] for layer in saved["layers"]], [
                "text", "pixels", "line", "polygon", "circle", "ellipse", "rectangle",
            ])

    def test_advanced_canvas_module_supports_keyframed_layer_presence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            advanced = self.advanced_module()
            layer = advanced["layers"][0]
            layer["properties"]["present"] = False
            layer["keyframes"][0]["properties"]["present"] = False
            layer["keyframes"][1]["properties"]["present"] = True

            saved = store.update({"custom_modules": [advanced]})["custom_modules"][0]

            self.assertFalse(saved["layers"][0]["properties"]["present"])
            self.assertTrue(saved["layers"][0]["keyframes"][1]["properties"]["present"])
            advanced["layers"][0]["keyframes"][1]["properties"]["present"] = "yes"
            with self.assertRaisesRegex(ValueError, "present must be a boolean"):
                store.update({"custom_modules": [advanced]})

    def test_advanced_canvas_text_and_line_rotation_is_optional_and_keyframeable(self):
        cases = {
            "text": {"x": 2, "y": 10, "text": "ROTATE", "font": "5x7", "colour": "#ffffff"},
            "time": {"x": 2, "y": 10, "format": "24h", "font": "5x7", "colour": "#ffffff"},
            "pixels": {"points": [[10, 10], [12, 10]], "colour": "#ffffff"},
            "line": {"x1": 10, "y1": 10, "x2": 20, "y2": 10, "width": 1, "colour": "#ffffff"},
        }
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            for layer_type, properties in cases.items():
                with self.subTest(layer_type=layer_type):
                    layer = {
                        "id": f"layer_{layer_type}", "name": layer_type.title(),
                        "type": layer_type, "visible": True,
                        "properties": {**properties, "rotation": 0},
                        "keyframes": [{"frame": 1, "easing": "linear", "properties": {"rotation": 90}}],
                    }
                    advanced = self.advanced_module(
                        frame_count=2, layers=[layer],
                        overrides=[{"frame": 0, "layer_id": layer["id"], "properties": {"rotation": 45}}],
                    )

                    saved = store.update({"custom_modules": [advanced]})["custom_modules"][0]
                    self.assertEqual(saved["layers"][0]["keyframes"][0]["properties"]["rotation"], 90)
                    self.assertEqual(saved["overrides"][0]["properties"]["rotation"], 45)

                    legacy = json.loads(json.dumps(advanced))
                    legacy["layers"][0]["properties"].pop("rotation")
                    legacy["layers"][0]["keyframes"] = []
                    legacy["overrides"] = []
                    store.update({"custom_modules": [legacy]})

    def test_advanced_canvas_module_supports_keyframed_layer_depth(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            advanced = self.advanced_module()
            layer = advanced["layers"][0]
            layer["properties"]["z_index"] = -3
            layer["keyframes"][0]["properties"]["z_index"] = -3
            layer["keyframes"][1]["properties"]["z_index"] = 7
            advanced["overrides"][0]["properties"]["z_index"] = 4

            saved = store.update({"custom_modules": [advanced]})["custom_modules"][0]

            self.assertEqual(saved["layers"][0]["properties"]["z_index"], -3)
            self.assertEqual(saved["layers"][0]["keyframes"][1]["properties"]["z_index"], 7)
            self.assertEqual(saved["overrides"][0]["properties"]["z_index"], 4)
            for invalid_depth in (-65, 65, 1.5, True):
                invalid = json.loads(json.dumps(advanced))
                invalid["layers"][0]["keyframes"][1]["properties"]["z_index"] = invalid_depth
                with self.subTest(z_index=invalid_depth), self.assertRaisesRegex(ValueError, "z_index"):
                    store.update({"custom_modules": [invalid]})

    def test_advanced_canvas_module_supports_dynamic_time_layers(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            layer = {
                "id": "layer_time", "name": "Time", "type": "time", "visible": True,
                "properties": {
                    "x": 2, "y": 12, "format": "12h_seconds",
                    "font": "7x13", "colour": "#ffffff",
                },
                "keyframes": [],
            }
            advanced = self.advanced_module(frame_count=1, layers=[layer], overrides=[])

            saved = store.update({"custom_modules": [advanced]})

            self.assertEqual(saved["custom_modules"][0]["layers"][0]["properties"]["format"], "12h_seconds")
            for invalid_format in ("unix", "", 24):
                invalid = json.loads(json.dumps(advanced))
                invalid["layers"][0]["properties"]["format"] = invalid_format
                with self.subTest(format=invalid_format), self.assertRaises(ValueError):
                    store.update({"custom_modules": [invalid]})

    def test_advanced_canvas_module_supports_resizable_media_layers(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            layers = [
                {
                    "id": "layer_image", "name": "Image", "type": "media", "visible": True,
                    "properties": {
                        "x": 2, "y": 3, "width": 32, "height": 16, "rotation": 0,
                        "source": "photo.png", "keep_aspect": True,
                    },
                    "keyframes": [],
                },
                {
                    "id": "layer_video", "name": "Video", "type": "media", "visible": True,
                    "properties": {
                        "x": 0, "y": 0, "width": 64, "height": 32, "rotation": 0,
                        "source": "built-in/intro.mp4", "keep_aspect": False,
                    },
                    "keyframes": [],
                },
            ]

            saved = store.update({"custom_modules": [
                self.advanced_module(layers=layers, overrides=[])
            ]})

            self.assertEqual(saved["custom_modules"][0]["layers"][0]["properties"]["width"], 32)
            self.assertEqual(saved["custom_modules"][0]["layers"][1]["properties"]["source"], "built-in/intro.mp4")

    def test_advanced_canvas_module_rejects_excessive_aggregate_points(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            points = [[index % 64, index // 64] for index in range(2048)]
            layers = [
                {
                    "id": f"layer_pixels_{index}", "name": f"Pixels {index}",
                    "type": "pixels", "visible": True,
                    "properties": {"points": points, "colour": "#ffffff"},
                    "keyframes": [],
                }
                for index in range(5)
            ]

            with self.assertRaises(ValueError):
                store.update({"custom_modules": [
                    self.advanced_module(layers=layers, overrides=[])
                ]})

    def test_advanced_canvas_module_validates_timeline_and_layer_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            invalid_modules = [
                self.advanced_module(frame_count=121),
                self.advanced_module(fps=0),
                self.advanced_module(playback="reverse"),
                self.advanced_module(layers=[]),
                self.advanced_module(layers=[{
                    **self.advanced_module()["layers"][0],
                    "type": "python",
                }]),
                self.advanced_module(overrides=[{
                    "frame": 24,
                    "layer_id": "layer_square",
                    "properties": {"rotation": 45},
                }]),
            ]
            for module in invalid_modules:
                with self.subTest(module=module), self.assertRaises(ValueError):
                    store.update({"custom_modules": [module]})

    def test_custom_text_modules_reject_unknown_references_and_unsafe_values(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            with self.assertRaises(ValueError):
                store.update({"playlist": [{"module": "custom_missing", "duration": 5, "enabled": True}]})
            invalid = {
                "id": "custom_bad/path",
                "name": "Bad",
                "type": "text",
                "text": "Hello",
                "font": "5x7",
                "colour": "#ffffff",
                "background": "#000000",
                "x": 0,
                "y": 20,
                "direction": "left",
                "speed": 0.05,
            }
            with self.assertRaises(ValueError):
                store.update({"custom_modules": [invalid]})


if __name__ == "__main__":
    unittest.main()
