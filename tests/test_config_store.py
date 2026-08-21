import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from config_store import (
    ConfigConflictError, ConfigDurabilityError, ConfigStore, DEFAULT_CONFIG,
    validate_config,
)


class ConfigStoreTests(unittest.TestCase):
    def test_legacy_config_gains_revision_without_changing_unrelated_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            legacy = json.loads(json.dumps(DEFAULT_CONFIG))
            legacy.pop("revision")
            legacy["device"]["name"] = "Migration sentinel"
            legacy["display"]["day_brightness"] = 37
            path.write_text(json.dumps(legacy))

            config = ConfigStore(path).get()

            self.assertEqual(config["revision"], 0)
            without_revision = dict(config)
            without_revision.pop("revision")
            self.assertEqual(without_revision, legacy)

    def test_read_only_preview_candidate_is_strict_bounded_and_does_not_change_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path)
            saved = store.update({
                "device": {"name": "Saved sentinel"},
                "media": {"splash": "missing-preview-only.gif"},
            }, base_revision=0)
            original_bytes = path.read_bytes()
            original = store.get()
            changes = json.loads(json.dumps(original))
            changes.pop("revision")
            changes["device"]["name"] = "Unsaved preview"
            changes["display"]["day_brightness"] = 37

            candidate = store.validate_candidate(changes)

            self.assertEqual(candidate["revision"], saved["revision"])
            self.assertEqual(candidate["device"]["name"], "Unsaved preview")
            self.assertEqual(candidate["display"]["day_brightness"], 37)
            self.assertEqual(candidate["media"]["splash"], "missing-preview-only.gif")
            candidate["device"]["name"] = "Mutated return value"
            self.assertEqual(store.get(), original)
            self.assertEqual(path.read_bytes(), original_bytes)

            invalid_cases = [
                ({"revision": saved["revision"]}, "server-owned"),
                ({"device": {"unknown": "value"}}, "Unknown configuration key"),
                ({"display": {"day_brightness": True}}, "brightness"),
                ({"media": {"splash": "../escape.gif"}}, "media reference"),
                ({"playlist": [{"module": "splash", "enabled": True}]}, "duration"),
                ({"playlist": [
                    {"module": "splash", "duration": 1, "enabled": True}
                ] * 257}, "at most 256"),
            ]
            for invalid, message in invalid_cases:
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, message):
                    store.validate_candidate(invalid)
                self.assertEqual(store.get(), original)
                self.assertEqual(path.read_bytes(), original_bytes)

            modules = []
            for module_index in range(8):
                layers = []
                for layer_index in range(64):
                    layers.append({
                        "id": f"layer_{module_index}_{layer_index}",
                        "name": "Large preview layer",
                        "type": "text",
                        "visible": True,
                        "properties": {
                            "x": 0, "y": 7, "text": "X" * 200,
                            "font": "5x7", "colour": "#ffffff",
                        },
                        "keyframes": [
                            {
                                "frame": frame,
                                "easing": "linear",
                                "properties": {"text": "Y" * 200},
                            }
                            for frame in range(8)
                        ],
                    })
                modules.append(self.advanced_module(
                    id=f"custom_preview_large_{module_index}",
                    name=f"Preview large {module_index}",
                    layers=layers,
                    overrides=[],
                ))
            with self.assertRaisesRegex(ValueError, "1 MiB"):
                store.validate_candidate({"custom_modules": modules})
            self.assertEqual(store.get(), original)
            self.assertEqual(path.read_bytes(), original_bytes)

    def test_atomic_update_increments_revision_once_with_dependent_records(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            custom = {
                "id": "custom_welcome", "name": "Welcome", "type": "text",
                "text": "HELLO", "font": "5x7", "colour": "#ffffff",
                "background": "#000000", "x": 0, "y": 20,
                "direction": "left", "speed": 0.05, "loops": 1,
            }

            saved = store.update({
                "device": {"name": "Revision one"},
                "custom_modules": [custom],
                "playlist": [{"module": custom["id"], "duration": 8, "enabled": True}],
            }, base_revision=0)

            self.assertEqual(saved["revision"], 1)
            self.assertEqual(store.get(), saved)
            self.assertEqual(saved["playlist"][0]["module"], saved["custom_modules"][0]["id"])

    def test_same_base_revision_cannot_commit_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            barrier = threading.Barrier(3)
            results = []

            def update(name):
                barrier.wait()
                try:
                    results.append(store.update({"device": {"name": name}}, base_revision=0))
                except ConfigConflictError as error:
                    results.append(error)

            threads = [threading.Thread(target=update, args=(name,)) for name in ("First", "Second")]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(2)

            commits = [result for result in results if isinstance(result, dict)]
            conflicts = [result for result in results if isinstance(result, ConfigConflictError)]
            self.assertEqual((len(commits), len(conflicts)), (1, 1))
            self.assertEqual(commits[0]["revision"], 1)
            self.assertEqual(conflicts[0].current_revision, 1)
            self.assertEqual(store.get(), commits[0])

    def test_revision_is_server_owned_and_must_be_nonnegative_integer_on_load(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            for value in (True, -1, 1.5, "1"):
                with self.subTest(value=value):
                    saved = store.get()
                    saved["revision"] = value
                    store.path.write_text(json.dumps(saved))
                    with self.assertRaisesRegex(ValueError, "revision"):
                        ConfigStore(store.path)
                    saved["revision"] = 0
                    store.path.write_text(json.dumps(saved))
            with self.assertRaisesRegex(ValueError, "server-owned"):
                store.update({"revision": 4})

    def test_current_saves_bound_playlist_and_canonical_snapshot_without_rewriting_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path)
            original = path.read_bytes()
            item = {"module": "splash", "duration": 1, "enabled": True}
            with self.assertRaisesRegex(ValueError, "at most 256"):
                store.update({"playlist": [item] * 257})
            self.assertEqual(path.read_bytes(), original)

            modules = []
            for module_index in range(8):
                layers = []
                for layer_index in range(64):
                    layers.append({
                        "id": f"layer_{module_index}_{layer_index}", "name": "Large layer",
                        "type": "text", "visible": True,
                        "properties": {"x": 0, "y": 7, "text": "X" * 200,
                                       "font": "5x7", "colour": "#ffffff"},
                        "keyframes": [
                            {"frame": frame, "easing": "linear", "properties": {"text": "Y" * 200}}
                            for frame in range(8)
                        ],
                    })
                modules.append(self.advanced_module(
                    id=f"custom_large_{module_index}", name=f"Large {module_index}",
                    layers=layers, overrides=[],
                ))
            with self.assertRaisesRegex(ValueError, "1 MiB"):
                store.update({"custom_modules": modules})
            self.assertEqual(path.read_bytes(), original)

    def test_atomic_writer_bounds_the_exact_persisted_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path)
            empty_size = len(json.dumps(
                {"padding": ""}, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8"))
            exact = {"padding": "X" * (1_048_576 - empty_size)}

            store._write(exact)

            self.assertEqual(path.stat().st_size, 1_048_576)
            original = path.read_bytes()
            oversized = {"padding": exact["padding"] + "X"}
            with self.assertRaisesRegex(ValueError, "1 MiB"):
                store._write(oversized)
            self.assertEqual(path.read_bytes(), original)

    def test_over_limit_legacy_files_are_reported_without_rewrite(self):
        item = {"module": "splash", "duration": 1, "enabled": True}
        cases = []
        playlist_legacy = json.loads(json.dumps(DEFAULT_CONFIG))
        playlist_legacy.pop("revision")
        playlist_legacy["playlist"] = [item] * 257
        cases.append(("playlist", playlist_legacy))

        snapshot_legacy = json.loads(json.dumps(DEFAULT_CONFIG))
        snapshot_legacy.pop("revision")
        modules = []
        for module_index in range(8):
            layers = []
            for layer_index in range(64):
                layers.append({
                    "id": f"layer_{module_index}_{layer_index}", "name": "Large layer",
                    "type": "text", "visible": True,
                    "properties": {"x": 0, "y": 7, "text": "X" * 200,
                                   "font": "5x7", "colour": "#ffffff"},
                    "keyframes": [
                        {"frame": frame, "easing": "linear", "properties": {"text": "Y" * 200}}
                        for frame in range(8)
                    ],
                })
            modules.append(self.advanced_module(
                id=f"custom_legacy_large_{module_index}",
                name=f"Legacy large {module_index}", layers=layers, overrides=[],
            ))
        snapshot_legacy["custom_modules"] = modules
        cases.append(("snapshot", snapshot_legacy))

        for label, legacy in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                original = json.dumps(legacy).encode("utf-8")
                path.write_bytes(original)

                with self.assertRaisesRegex(
                    ValueError, "legacy configuration exceeds current save bounds",
                ):
                    ConfigStore(path)

                self.assertEqual(path.read_bytes(), original)

    def test_directory_sync_failure_reports_visible_committed_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path)

            with patch(
                "config_store.os.fsync",
                side_effect=[None, OSError("directory unavailable")],
            ), self.assertRaises(ConfigDurabilityError) as raised:
                store.update({"device": {"name": "Committed Despite Error"}})

            self.assertEqual(
                raised.exception.config["device"]["name"],
                "Committed Despite Error",
            )
            self.assertEqual(store.get()["device"]["name"], "Committed Despite Error")

    def test_legacy_config_gains_disabled_openweather_apps_without_changing_existing_playlist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            legacy = json.loads(json.dumps(DEFAULT_CONFIG))
            legacy.pop("openweather_overview", None)
            legacy.pop("openweather_forecast", None)
            legacy["playlist"] = [
                item for item in legacy["playlist"]
                if item["module"] not in {"openweather_overview", "openweather_forecast"}
            ]
            expected_existing = json.loads(json.dumps(legacy["playlist"]))
            path.write_text(json.dumps(legacy))

            config = ConfigStore(path).get()

            self.assertEqual(config["playlist"][:len(expected_existing)], expected_existing)
            self.assertEqual(config["playlist"][-2:], [
                {"module": "openweather_overview", "duration": 15, "enabled": False},
                {"module": "openweather_forecast", "duration": 15, "enabled": False},
            ])
            self.assertTrue(next(item for item in config["playlist"] if item["module"] == "weather")["enabled"])
            self.assertIn("openweather_overview", config)
            self.assertIn("openweather_forecast", config)

    def test_legacy_config_gains_disabled_empty_announcement_board_without_reordering_playlist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            legacy = json.loads(json.dumps(DEFAULT_CONFIG))
            legacy.pop("announcement_board", None)
            legacy["playlist"] = [
                item for item in legacy["playlist"]
                if item["module"] != "announcement_board"
            ]
            expected_existing = json.loads(json.dumps(legacy["playlist"]))
            path.write_text(json.dumps(legacy))

            config = ConfigStore(path).get()

            self.assertEqual(config["playlist"][:-1], expected_existing)
            self.assertEqual(config["playlist"][-1], {
                "module": "announcement_board", "duration": 15, "enabled": False,
            })
            self.assertEqual(config["announcement_board"], {"announcements": []})

    def test_legacy_config_gains_default_sleep_gif_speed_without_changing_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            legacy = json.loads(json.dumps(DEFAULT_CONFIG))
            legacy["media_layout"]["sleep"].pop("gif_speed_percent", None)
            expected_layout = json.loads(json.dumps(legacy["media_layout"]["sleep"]))
            path.write_text(json.dumps(legacy))

            config = ConfigStore(path).get()

            self.assertEqual(config["media_layout"]["sleep"]["gif_speed_percent"], 100)
            actual_layout = dict(config["media_layout"]["sleep"])
            actual_layout.pop("gif_speed_percent")
            self.assertEqual(actual_layout, expected_layout)
            self.assertNotIn("gif_speed_percent", config["media_layout"]["splash"])

    def test_static_announcement_is_validated_and_colours_are_canonicalized(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")

            config = store.update({
                "announcement_board": {"announcements": [self.announcement()]},
            })

            announcement = config["announcement_board"]["announcements"][0]
            self.assertEqual(announcement["style"]["foreground"], "#ffffff")
            self.assertEqual(announcement["style"]["border_colour"], "#ffffff")
            self.assertEqual(announcement["timing"], {
                "mode": "static", "duration_seconds": 8,
            })

    def test_scrolling_announcement_uses_only_bounded_loops_and_frame_delay(self):
        scrolling = self.announcement()
        scrolling["timing"] = {"mode": "scroll", "loops": 2, "frame_delay": 0.05}
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")

            saved = store.update({
                "announcement_board": {"announcements": [scrolling]},
            })

            self.assertEqual(
                saved["announcement_board"]["announcements"][0]["timing"],
                {"mode": "scroll", "loops": 2, "frame_delay": 0.05},
            )
            scrolling["timing"]["duration_seconds"] = 8
            with self.assertRaisesRegex(ValueError, "scrolling announcement timing"):
                store.update({"announcement_board": {"announcements": [scrolling]}})

    def test_fade_transition_has_only_a_bounded_duration(self):
        fading = self.announcement()
        fading["transition"] = {"type": "fade", "duration_seconds": 0.4}
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")

            saved = store.update({
                "announcement_board": {"announcements": [fading]},
            })

            self.assertEqual(
                saved["announcement_board"]["announcements"][0]["transition"],
                {"type": "fade", "duration_seconds": 0.4},
            )
            fading["transition"]["steps"] = 4
            with self.assertRaisesRegex(ValueError, "fade announcement transition"):
                store.update({"announcement_board": {"announcements": [fading]}})

    def test_slide_transition_has_direction_and_bounded_duration(self):
        sliding = self.announcement()
        sliding["transition"] = {
            "type": "slide", "direction": "left", "duration_seconds": 0.4,
        }
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")

            saved = store.update({
                "announcement_board": {"announcements": [sliding]},
            })

            self.assertEqual(
                saved["announcement_board"]["announcements"][0]["transition"],
                {"type": "slide", "direction": "left", "duration_seconds": 0.4},
            )
            sliding["transition"]["direction"] = "up"
            with self.assertRaisesRegex(ValueError, "slide announcement transition direction"):
                store.update({"announcement_board": {"announcements": [sliding]}})

    def test_wipe_transition_has_direction_and_bounded_duration(self):
        wiping = self.announcement()
        wiping["transition"] = {
            "type": "wipe", "direction": "left", "duration_seconds": 0.4,
        }
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")

            saved = store.update({
                "announcement_board": {"announcements": [wiping]},
            })

            self.assertEqual(
                saved["announcement_board"]["announcements"][0]["transition"],
                {"type": "wipe", "direction": "left", "duration_seconds": 0.4},
            )
            wiping["transition"]["direction"] = "down"
            with self.assertRaisesRegex(ValueError, "wipe announcement transition direction"):
                store.update({"announcement_board": {"announcements": [wiping]}})

    def test_pixel_dissolve_transition_has_only_a_bounded_duration(self):
        dissolve = self.announcement()
        dissolve["transition"] = {"type": "pixel_dissolve", "duration_seconds": 0.4}
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")

            saved = store.update({
                "announcement_board": {"announcements": [dissolve]},
            })

            self.assertEqual(
                saved["announcement_board"]["announcements"][0]["transition"],
                {"type": "pixel_dissolve", "duration_seconds": 0.4},
            )
            dissolve["transition"]["seed"] = 42
            with self.assertRaisesRegex(ValueError, "pixel dissolve announcement transition"):
                store.update({"announcement_board": {"announcements": [dissolve]}})

    def test_editor_shaped_announcement_preserves_disabled_state_and_name(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            item = self.announcement(enabled=False, name="Front desk welcome")

            updated = store.update({"announcement_board": {"announcements": [item]}})

            saved = updated["announcement_board"]["announcements"][0]
            self.assertFalse(saved["enabled"])
            self.assertEqual(saved["name"], "Front desk welcome")

    def test_legacy_announcement_record_gains_enabled_name_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path)
            historical = store.get()
            legacy = self.announcement()
            legacy.pop("enabled")
            legacy.pop("name")
            historical["announcement_board"]["announcements"] = [legacy]
            path.write_text(json.dumps(historical))

            updated = ConfigStore(path).get()

            saved = updated["announcement_board"]["announcements"][0]
            self.assertTrue(saved["enabled"])
            self.assertEqual(saved["name"], "Welcome")

    def test_announcement_weekdays_are_preserved_and_legacy_defaults_to_every_day(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path)
            weekdays = self.announcement(enabled=True, name="Weekdays")
            weekdays["schedule"]["days"] = [0, 2, 4]

            updated = store.update({"announcement_board": {"announcements": [weekdays]}})
            self.assertEqual(updated["announcement_board"]["announcements"][0]["schedule"]["days"], [0, 2, 4])

            legacy = self.announcement()
            legacy.pop("enabled")
            legacy.pop("name")
            legacy["schedule"].pop("days")
            historical = store.get()
            historical["announcement_board"]["announcements"] = [legacy]
            path.write_text(json.dumps(historical))
            updated = ConfigStore(path).get()
            self.assertEqual(updated["announcement_board"]["announcements"][0]["schedule"]["days"], [0, 1, 2, 3, 4, 5, 6])

    def test_announcement_rejects_runtime_unsupported_scrolling_combinations(self):
        cases = []
        media_layout = self.announcement(timing={"mode": "scroll", "loops": 1, "frame_delay": 0.05})
        media_layout["layout"] = "icon_notice"
        cases.append(media_layout)
        bordered = self.announcement(timing={"mode": "scroll", "loops": 1, "frame_delay": 0.05})
        bordered["style"]["border"] = "solid"
        cases.append(bordered)
        transitioned = self.announcement(timing={"mode": "scroll", "loops": 1, "frame_delay": 0.05})
        transitioned["transition"] = {"type": "fade", "duration_seconds": 0.5}
        cases.append(transitioned)

        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            for announcement in cases:
                with self.subTest(announcement=announcement):
                    with self.assertRaisesRegex(ValueError, "scrolling announcement"):
                        store.update({"announcement_board": {"announcements": [announcement]}})

    def test_announcement_rejects_equal_start_and_end_times(self):
        invalid = self.announcement()
        invalid["schedule"]["end_time"] = invalid["schedule"]["start_time"]
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")

            with self.assertRaisesRegex(ValueError, "must differ"):
                store.update({"announcement_board": {"announcements": [invalid]}})

    def test_sleep_gif_speed_rejects_malformed_and_unsafe_percentages(self):
        invalid_values = [False, 0, -1, 24, 401, 10_000, 1.5, float("nan"), float("inf"), "100", None]
        for value in invalid_values:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                store = ConfigStore(Path(directory) / "config.json")
                with self.assertRaises(ValueError):
                    store.update({"media_layout": {"sleep": {"gif_speed_percent": value}}})

    @staticmethod
    def announcement(**changes):
        item = {
            "id": "announcement_welcome",
            "enabled": True,
            "name": "Welcome",
            "layout": "text_notice",
            "headline": "Welcome",
            "message": "Front desk closes at 5 PM",
            "media": None,
            "schedule": {
                "start_date": "2030-01-15",
                "end_date": "2030-01-15",
                "start_time": "09:00",
                "end_time": "17:00",
                "days": [0, 1, 2, 3, 4, 5, 6],
            },
            "style": {
                "foreground": "#FFFFFF",
                "background": "#000000",
                "font": "5x7",
                "alignment": "center",
                "border": "none",
                "border_colour": "#FFFFFF",
            },
            "timing": {"mode": "static", "duration_seconds": 8},
            "transition": {"type": "cut"},
        }
        item.update(changes)
        return item

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
            self.assertEqual(store.get()["weather"]["location"], "London, England, GB")
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

    def test_display_schedule_requires_runtime_parseable_times_and_integer_brightness(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            for value in (
                "8:0", "08:00:00", "08:00+01:00",
                "٠٨:٠٠", "０８:００", True, None,
            ):
                with self.subTest(time=value), self.assertRaises(ValueError):
                    store.update({"display": {"active_start": value}})
            for value in (True, False, 1.5, "50"):
                with self.subTest(brightness=value), self.assertRaises(ValueError):
                    store.update({"display": {"day_brightness": value}})

            saved = store.update({"display": {
                "active_start": "08:00", "active_end": "20:00",
                "day_brightness": 1, "night_brightness": 100,
            }})
            self.assertEqual(saved["display"], {
                "active_start": "08:00", "active_end": "20:00",
                "day_brightness": 1, "night_brightness": 100,
            })

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

    def test_default_weather_location_is_london_for_every_weather_app(self):
        expected = {
            "location": "London, England, GB",
            "latitude": 51.5074,
            "longitude": -0.1278,
        }

        for module in ("weather", "openweather_overview", "openweather_forecast"):
            self.assertEqual(
                {key: DEFAULT_CONFIG[module][key] for key in expected},
                expected,
            )

    def test_legacy_forecast_card_seconds_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            legacy = json.loads(json.dumps(DEFAULT_CONFIG))
            legacy["openweather_forecast"]["card_seconds"] = 5
            legacy["openweather_forecast"]["forecast_items"] = 7
            legacy["device"]["name"] = "Preserved Matrix"
            path.write_text(json.dumps(legacy))

            config = ConfigStore(path).get()

            self.assertNotIn("card_seconds", config["openweather_forecast"])
            self.assertEqual(config["openweather_forecast"]["forecast_items"], 7)
            self.assertEqual(config["device"]["name"], "Preserved Matrix")

    def test_weather_playlist_timing_is_automatic_from_ticker_loops(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            updated = store.get()
            updated.pop("revision")
            updated["weather"]["scroll_loops"] = 2
            updated["openweather_overview"]["scroll_loops"] = 3
            updated["openweather_forecast"]["scroll_loops"] = 4
            for item in updated["playlist"]:
                if item["module"] in {
                    "weather", "openweather_overview", "openweather_forecast",
                }:
                    item["duration"] = 999

            saved = store.update(updated)

            self.assertEqual(saved["weather"]["scroll_loops"], 2)
            self.assertEqual(saved["openweather_overview"]["scroll_loops"], 3)
            self.assertEqual(saved["openweather_forecast"]["scroll_loops"], 4)
            weather_durations = {
                item["module"]: item["duration"]
                for item in saved["playlist"]
                if item["module"] in {
                    "weather", "openweather_overview", "openweather_forecast",
                }
            }
            self.assertEqual(weather_durations, {
                "weather": 999,
                "openweather_overview": 999,
                "openweather_forecast": 999,
            })

    def test_weather_scroll_loops_are_bounded_in_all_weather_apps(self):
        for section in (
            "weather", "openweather_overview", "openweather_forecast",
        ):
            for invalid in (True, 0, 11, 1.5):
                with self.subTest(section=section, invalid=invalid):
                    with tempfile.TemporaryDirectory() as directory:
                        store = ConfigStore(Path(directory) / "config.json")
                        updated = store.get()
                        updated.pop("revision")
                        updated[section]["scroll_loops"] = invalid
                        with self.assertRaisesRegex(ValueError, "scroll loops"):
                            store.update(updated)

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

    def test_weather_location_is_authoritative_for_every_weather_app(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")

            updated = store.update({
                "weather": {
                    "location": "Worthing, England, GB",
                    "latitude": 50.8179,
                    "longitude": -0.3729,
                },
                "openweather_overview": {
                    "location": "Stale overview location",
                    "latitude": 1,
                    "longitude": 2,
                },
                "openweather_forecast": {
                    "location": "Stale forecast location",
                    "latitude": 3,
                    "longitude": 4,
                },
            })

            expected = {
                "location": "Worthing, England, GB",
                "latitude": 50.8179,
                "longitude": -0.3729,
            }
            for module in ("weather", "openweather_overview", "openweather_forecast"):
                self.assertEqual(
                    {key: updated[module][key] for key in expected},
                    expected,
                )

    def test_legacy_independent_locations_migrate_to_the_existing_shared_location(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            saved = json.loads(json.dumps(DEFAULT_CONFIG))
            saved["weather"].update({
                "location": "Cheltenham, England, GB",
                "latitude": 51.90006,
                "longitude": -2.07972,
            })
            saved["openweather_overview"].update({
                "location": "Stale overview",
                "latitude": 1,
                "longitude": 2,
            })
            saved["openweather_forecast"].update({
                "location": "Stale forecast",
                "latitude": 3,
                "longitude": 4,
            })
            path.write_text(json.dumps(saved))

            migrated = ConfigStore(path).get()

            expected = {
                "location": "Cheltenham, England, GB",
                "latitude": 51.90006,
                "longitude": -2.07972,
            }
            for module in ("weather", "openweather_overview", "openweather_forecast"):
                self.assertEqual(
                    {key: migrated[module][key] for key in expected},
                    expected,
                )

    def test_weather_display_options_are_saved_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            updated = store.update({"weather": {
                "units": "fahrenheit",
                "temperature_y": 18,
                "ticker_y": 29,
                "scroll_direction": "right",
                "scroll_speed": 0.075,
            }})
            self.assertEqual(updated["weather"]["units"], "fahrenheit")
            self.assertEqual(updated["weather"]["temperature_y"], 18)
            self.assertEqual(updated["weather"]["ticker_y"], 29)
            self.assertEqual(updated["weather"]["scroll_direction"], "right")
            self.assertEqual(updated["weather"]["scroll_speed"], 0.075)

            invalid_values = {
                "units": "kelvin",
                "temperature_y": 32,
                "ticker_y": -1,
                "scroll_direction": "up",
            }
            for key, value in invalid_values.items():
                with self.subTest(key=key), self.assertRaises(ValueError):
                    store.update({"weather": {key: value}})
            for value in (True, 0.009, 1.001, "normal"):
                with self.subTest(scroll_speed=value), self.assertRaises(ValueError):
                    store.update({"weather": {"scroll_speed": value}})

    def test_legacy_weather_speed_presets_migrate_to_frame_delay_seconds(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            saved = json.loads(json.dumps(DEFAULT_CONFIG))
            saved["device"]["name"] = "Migration sentinel"
            saved["display"]["day_brightness"] = 37
            presets = {
                "weather": ("slow", 0.1),
                "openweather_overview": ("normal", 0.05),
                "openweather_forecast": ("fast", 0.025),
            }
            for module, (preset, _seconds) in presets.items():
                saved[module]["scroll_speed"] = preset
            path.write_text(json.dumps(saved))

            migrated = ConfigStore(path).get()

            for module, (_preset, seconds) in presets.items():
                self.assertEqual(migrated[module]["scroll_speed"], seconds)
            self.assertEqual(migrated["device"]["name"], "Migration sentinel")
            self.assertEqual(migrated["display"]["day_brightness"], 37)

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
            zero_selected = store.update({"lava": {
                "enabled_palettes": [], "custom_enabled": False,
            }})
            self.assertEqual(zero_selected["lava"]["enabled_palettes"], [])
            self.assertFalse(zero_selected["lava"]["custom_enabled"])
            reloaded = ConfigStore(Path(directory) / "config.json").get()
            self.assertEqual(reloaded["lava"]["enabled_palettes"], [])
            self.assertFalse(any(
                palette["enabled"] for palette in reloaded["lava"]["custom_palettes"]
            ))
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

    def test_save_bounds_validate_the_exact_incremented_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            validated = []

            def capture(config, **options):
                validated.append((json.loads(json.dumps(config)), options))
                return validate_config(config, **options)

            with patch("config_store.validate_config", side_effect=capture):
                updated = store.update({"device": {"name": "Incremented snapshot"}})

            validated_snapshot, options = validated[-1]
            self.assertEqual(validated_snapshot["revision"], updated["revision"])
            self.assertTrue(options["enforce_save_bounds"])

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

    def test_every_installed_font_is_valid_in_every_user_font_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            font = "tom_thumb"
            simple = {
                "id": "custom_font", "name": "Installed font", "type": "text",
                "text": "HELLO", "font": font, "colour": "#ffffff",
                "background": "#000000", "x": 0, "y": 20,
                "direction": "left", "speed": 0.05, "loops": 1,
            }
            canvas = self.advanced_module(layers=[{
                "id": "layer_text", "name": "Text", "type": "text", "visible": True,
                "properties": {"x": 1, "y": 7, "text": "HI", "font": font, "colour": "#ffffff"},
                "keyframes": [],
            }], overrides=[])
            announcement = self.announcement(style={
                "foreground": "#FFFFFF", "background": "#000000", "font": font,
                "alignment": "center", "border": "none", "border_colour": "#FFFFFF",
            })

            updated = store.update({
                "lava": {"time_font": font, "date_font": font},
                "custom_modules": [simple, canvas],
                "announcement_board": {"announcements": [announcement]},
            })

            self.assertEqual(updated["lava"]["time_font"], font)
            self.assertEqual(updated["custom_modules"][0]["font"], font)
            self.assertEqual(updated["custom_modules"][1]["layers"][0]["properties"]["font"], font)
            self.assertEqual(updated["announcement_board"]["announcements"][0]["style"]["font"], font)

    def test_scrolling_duration_supports_an_installed_font_outside_the_old_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            module = {
                "id": "custom_font", "name": "Installed font", "type": "text_scroll",
                "text": "HELLO", "font": "tom_thumb", "colour": "#ffffff",
                "background": "#000000", "x": 0, "y": 20,
                "direction": "left", "speed": 0.05, "loops": 1,
            }
            playlist = store.get()["playlist"] + [
                {"module": module["id"], "duration": 1, "enabled": True},
            ]

            updated = store.update({"custom_modules": [module], "playlist": playlist})

            duration = updated["playlist"][-1]["duration"]
            self.assertGreater(duration, 0)


if __name__ == "__main__":
    unittest.main()
