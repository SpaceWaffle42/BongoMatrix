import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from config_store import (
    CURRENT_CONFIG_SCHEMA_VERSION, ConfigConflictError, ConfigDurabilityError,
    ConfigStore, DEFAULT_CONFIG, validate_config,
)


class ConfigStoreTests(unittest.TestCase):
    @staticmethod
    def _wait_for_metadata_workers(store, timeout=1.0):
        deadline = time.monotonic() + timeout
        while store._metadata_workers and time.monotonic() < deadline:
            time.sleep(0.005)
        return set(store._metadata_workers)

    @staticmethod
    def _simple(module_id="custom_static", module_type="text"):
        return {
            "id": module_id, "name": "Simple", "type": module_type,
            "text": "HELLO", "font": "5x7", "colour": "#ABCDEF",
            "background": "#123456", "x": 0, "y": 20,
            "direction": "left", "speed": 0.05, "loops": 2,
        }

    def test_v1_simple_custom_appearance_migrates_once_without_touching_unrelated_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            legacy = json.loads(json.dumps(DEFAULT_CONFIG))
            legacy["schema_version"] = 1
            legacy["revision"] = 7
            legacy["device"]["name"] = "untouched"
            canvas_sentinel = self.advanced_module()
            canvas_sentinel["layers"][0]["keyframes"].append({
                "frame": 17, "easing": "ease_in_out",
                "properties": {"rotation": 271, "colour": "#123abc"},
            })
            canvas_before = json.loads(json.dumps(canvas_sentinel))
            legacy["custom_modules"] = [self._simple(), canvas_sentinel]
            path.write_text(json.dumps(legacy))

            migrated = ConfigStore(path).get()

            self.assertEqual(migrated["schema_version"], CURRENT_CONFIG_SCHEMA_VERSION)
            self.assertEqual(migrated["revision"], 8)
            self.assertEqual(migrated["device"]["name"], "untouched")
            self.assertEqual(migrated["custom_modules"][0]["border"], "none")
            self.assertEqual(migrated["custom_modules"][0]["border_colour"], "#ffffff")
            self.assertEqual(migrated["custom_modules"][1], canvas_before)
            migrated_bytes = path.read_bytes()
            self.assertEqual(ConfigStore(path).get(), migrated)
            self.assertEqual(path.read_bytes(), migrated_bytes)

    def test_schema_v2_custom_appearance_migrates_without_canonicalizing_unrelated_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            current = json.loads(json.dumps(DEFAULT_CONFIG))
            current["schema_version"] = 2
            simple = self._simple()
            simple.update(border="solid", border_colour="#A1B2C3")
            current["custom_modules"] = [simple]
            path.write_text(json.dumps(current))
            loaded = ConfigStore(path).get()
            self.assertEqual(loaded["custom_modules"][0]["border_colour"], "#A1B2C3")
            for mutation in (lambda item: item.pop("border"), lambda item: item.update(extra=True)):
                malformed = json.loads(json.dumps(current))
                mutation(malformed["custom_modules"][0])
                original = json.dumps(malformed).encode()
                path.write_bytes(original)
                with self.assertRaisesRegex(ValueError, "invalid fields"):
                    ConfigStore(path)
                self.assertEqual(path.read_bytes(), original)

    def test_schema_v2_announcement_font_migrates_to_both_canonical_fonts_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            legacy = json.loads(json.dumps(DEFAULT_CONFIG))
            legacy["schema_version"] = 2
            announcement = self.announcement()
            announcement["style"] = {
                "foreground": "#AABBCC", "background": "#010203",
                "font": "tom_thumb", "alignment": "right",
                "border": "inset", "border_colour": "#DDEEFF",
            }
            legacy["announcement_board"]["announcements"] = [announcement]
            unrelated_before = json.loads(json.dumps(legacy["device"]))
            path.write_text(json.dumps(legacy))

            migrated = ConfigStore(path).get()

            style = migrated["announcement_board"]["announcements"][0]["style"]
            self.assertEqual(style, {
                "foreground": "#AABBCC", "background": "#010203",
                "headline_font": "tom_thumb", "message_font": "tom_thumb",
                "alignment": "right", "border": "inset", "border_colour": "#DDEEFF",
            })
            self.assertEqual(migrated["device"], unrelated_before)

    def test_current_announcement_style_requires_both_canonical_fonts_and_rejects_legacy_font(self):
        candidate = json.loads(json.dumps(DEFAULT_CONFIG))
        candidate["announcement_board"]["announcements"] = [self.announcement()]
        validate_config(candidate, allow_legacy_announcements=False)
        for mutation in (
            lambda style: style.pop("headline_font"),
            lambda style: style.pop("message_font"),
            lambda style: style.update(font="5x7"),
        ):
            malformed = json.loads(json.dumps(candidate))
            mutation(malformed["announcement_board"]["announcements"][0]["style"])
            with self.subTest(style=malformed["announcement_board"]["announcements"][0]["style"]):
                with self.assertRaisesRegex(ValueError, "announcement style has invalid fields"):
                    validate_config(malformed, allow_legacy_announcements=False)

    def test_basic_custom_modules_accept_new_border_styles_and_preserve_existing_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path)
            for border in ("none", "solid", "inset", "dotted", "dashed", "double"):
                module = self._simple()
                module.update(border=border, border_colour="#A1B2C3")
                saved = store.update(
                    {"custom_modules": [module]}, base_revision=store.get()["revision"],
                )
                self.assertEqual(saved["custom_modules"][0]["border"], border)
                self.assertEqual(saved["custom_modules"][0]["border_colour"], "#a1b2c3")

    def test_current_save_rejects_invalid_custom_border_style_and_colour_without_changing_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path)
            before = path.read_bytes()
            for border, colour in (("groove", "#ffffff"), ("solid", "red"), (True, "#ffffff")):
                simple = self._simple()
                simple.update(border=border, border_colour=colour)
                with self.assertRaises(ValueError):
                    store.update({"custom_modules": [simple]}, base_revision=0)
                self.assertEqual(path.read_bytes(), before)

    def test_legacy_config_gains_revision_without_changing_unrelated_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            legacy = json.loads(json.dumps(DEFAULT_CONFIG))
            legacy.pop("schema_version")
            legacy.pop("revision")
            legacy["device"]["name"] = "Migration sentinel"
            legacy["display"]["day_brightness"] = 37
            legacy["custom_modules"] = [self._simple("custom_v0_simple", "text_scroll")]
            path.write_text(json.dumps(legacy))

            config = ConfigStore(path).get()

            self.assertEqual(config["schema_version"], CURRENT_CONFIG_SCHEMA_VERSION)
            self.assertEqual(config["revision"], 1)
            without_server_fields = dict(config)
            without_server_fields.pop("schema_version")
            without_server_fields.pop("revision")
            expected = json.loads(json.dumps(legacy))
            expected["custom_modules"][0].update(
                colour="#abcdef", border="none", border_colour="#ffffff",
            )
            self.assertEqual(without_server_fields, expected)
            migrated_bytes = path.read_bytes()
            self.assertNotEqual(migrated_bytes, json.dumps(legacy).encode("utf-8"))
            self.assertEqual(ConfigStore(path).get()["revision"], 1)
            self.assertEqual(path.read_bytes(), migrated_bytes)

    def test_malformed_current_schema_is_rejected_without_migration_or_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path)
            custom = {
                "id": "custom_saved", "name": "Saved", "type": "text_scroll",
                "text": "SAVED", "font": "5x7", "colour": "#ffffff",
                "background": "#000000", "border": "none", "border_colour": "#ffffff", "x": 0, "y": 20,
                "direction": "left", "speed": 0.05, "loops": 2,
            }
            store.update({
                "custom_modules": [custom],
                "playlist": [{
                    "module": "custom_saved", "duration": 1,
                    "enabled": True,
                }],
            }, base_revision=0)
            malformed = store.get()
            malformed["custom_modules"][0].pop("loops")
            original = json.dumps(malformed).encode("utf-8")
            path.write_bytes(original)

            with self.assertRaisesRegex(ValueError, "custom text modules have invalid fields"):
                ConfigStore(path)

            self.assertEqual(path.read_bytes(), original)

    def test_current_schema_missing_nested_field_is_rejected_without_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path)
            malformed = store.get()
            malformed["device"].pop("name")
            original = json.dumps(malformed).encode("utf-8")
            path.write_bytes(original)

            with self.assertRaisesRegex(ValueError, "current configuration has missing fields"):
                ConfigStore(path)

            self.assertEqual(path.read_bytes(), original)

    def test_invalid_schema_versions_are_rejected_without_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path)
            for schema_version in (True, -1, 1.5, "1", CURRENT_CONFIG_SCHEMA_VERSION + 1):
                with self.subTest(schema_version=schema_version):
                    invalid = store.get()
                    invalid["schema_version"] = schema_version
                    original = json.dumps(invalid).encode("utf-8")
                    path.write_bytes(original)

                    with self.assertRaisesRegex(ValueError, "schema_version"):
                        ConfigStore(path)

                    self.assertEqual(path.read_bytes(), original)

    def test_invalid_legacy_revisions_are_rejected_without_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for revision in (True, -1, 1.5, "1"):
                with self.subTest(revision=revision):
                    invalid = json.loads(json.dumps(DEFAULT_CONFIG))
                    invalid.pop("schema_version")
                    invalid["revision"] = revision
                    original = json.dumps(invalid).encode("utf-8")
                    path.write_bytes(original)

                    with self.assertRaisesRegex(ValueError, "revision"):
                        ConfigStore(path)

                    self.assertEqual(path.read_bytes(), original)
                    valid = json.loads(json.dumps(DEFAULT_CONFIG))
                    path.write_text(json.dumps(valid))

    def test_schema_version_is_server_owned(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")

            with self.assertRaisesRegex(ValueError, "server-owned"):
                store.update({"schema_version": 1})
            with self.assertRaisesRegex(ValueError, "server-owned"):
                store.validate_candidate({"schema_version": 1})

    def test_explicit_legacy_schema_migrates_once_and_increments_revision_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            legacy = json.loads(json.dumps(DEFAULT_CONFIG))
            legacy["schema_version"] = 0
            legacy["revision"] = 7
            legacy["device"]["name"] = "Version zero sentinel"
            original = json.dumps(legacy).encode("utf-8")
            path.write_bytes(original)

            migrated = ConfigStore(path).get()

            self.assertEqual(migrated["schema_version"], CURRENT_CONFIG_SCHEMA_VERSION)
            self.assertEqual(migrated["revision"], 8)
            self.assertEqual(migrated["device"]["name"], "Version zero sentinel")
            migrated_bytes = path.read_bytes()
            self.assertNotEqual(migrated_bytes, original)

            reloaded = ConfigStore(path).get()

            self.assertEqual(reloaded, migrated)
            self.assertEqual(reloaded["revision"], 8)
            self.assertEqual(path.read_bytes(), migrated_bytes)

    def test_current_schema_reload_preserves_exact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path)
            store.update({"device": {"name": "Current sentinel"}}, base_revision=0)
            original = path.read_bytes()

            reloaded = ConfigStore(path).get()

            self.assertEqual(reloaded["schema_version"], CURRENT_CONFIG_SCHEMA_VERSION)
            self.assertEqual(reloaded["revision"], 1)
            self.assertEqual(path.read_bytes(), original)

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
            changes.pop("schema_version")
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
                "background": "#000000", "border": "none", "border_colour": "#ffffff", "x": 0, "y": 20,
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
        playlist_legacy.pop("schema_version")
        playlist_legacy.pop("revision")
        playlist_legacy["playlist"] = [item] * 257
        cases.append(("playlist", playlist_legacy))

        snapshot_legacy = json.loads(json.dumps(DEFAULT_CONFIG))
        snapshot_legacy.pop("schema_version")
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
            legacy.pop("schema_version")
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
            legacy.pop("schema_version")
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
            legacy.pop("schema_version")
            legacy["media_layout"]["sleep"].pop("playback_speed_percent", None)
            legacy["media_layout"]["sleep"].pop("gif_speed_percent", None)
            expected_layout = json.loads(json.dumps(legacy["media_layout"]["sleep"]))
            path.write_text(json.dumps(legacy))

            config = ConfigStore(path).get()

            self.assertEqual(config["media_layout"]["sleep"]["playback_speed_percent"], 100.0)
            self.assertNotIn("gif_speed_percent", config["media_layout"]["sleep"])
            actual_layout = dict(config["media_layout"]["sleep"])
            actual_layout.pop("playback_speed_percent")
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
            historical.pop("schema_version")
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
            historical.pop("schema_version")
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
            "playback_speed_percent": 100.0,
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
                "headline_font": "5x7",
                "message_font": "4x6",
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
            legacy.pop("schema_version")
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
            legacy.pop("schema_version")
            legacy["openweather_forecast"]["card_seconds"] = 5
            legacy["openweather_forecast"]["forecast_items"] = 7
            legacy["device"]["name"] = "Preserved Matrix"
            path.write_text(json.dumps(legacy))

            config = ConfigStore(path).get()

            self.assertNotIn("card_seconds", config["openweather_forecast"])
            self.assertEqual(config["openweather_forecast"]["forecast_items"], 7)
            self.assertEqual(config["device"]["name"], "Preserved Matrix")

    def test_current_saves_reject_malformed_automatic_playlist_durations_without_rewrite(self):
        automatic_modules = (
            "weather",
            "openweather_overview",
            "openweather_forecast",
            "announcement_board",
        )
        malformed_durations = (True, False, -1, "9", float("nan"), float("inf"))

        for module in automatic_modules:
            for malformed in malformed_durations:
                with self.subTest(module=module, duration=malformed):
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "config.json"
                        store = ConfigStore(path)
                        original = path.read_bytes()
                        playlist = json.loads(json.dumps(store.get()["playlist"]))
                        next(
                            item for item in playlist if item["module"] == module
                        )["duration"] = malformed

                        with self.assertRaisesRegex(ValueError, "duration"):
                            store.update({"playlist": playlist})

                        self.assertEqual(path.read_bytes(), original)

    def test_legacy_load_canonicalizes_malformed_automatic_durations(self):
        automatic_modules = (
            "weather",
            "openweather_overview",
            "openweather_forecast",
            "announcement_board",
        )
        malformed_durations = (True, False, -1, "9", float("nan"), float("inf"))

        for module in automatic_modules:
            for malformed in malformed_durations:
                with self.subTest(module=module, duration=malformed):
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "config.json"
                        legacy = json.loads(json.dumps(DEFAULT_CONFIG))
                        legacy.pop("schema_version")
                        next(
                            item for item in legacy["playlist"] if item["module"] == module
                        )["duration"] = malformed
                        path.write_text(json.dumps(legacy))

                        loaded = ConfigStore(path).get()
                        persisted = json.loads(path.read_text())

                        self.assertEqual(
                            next(
                                item for item in loaded["playlist"]
                                if item["module"] == module
                            )["duration"],
                            15,
                        )
                        self.assertEqual(
                            next(
                                item for item in persisted["playlist"]
                                if item["module"] == module
                            )["duration"],
                            15,
                        )

    def test_current_saves_accept_automatic_playlist_duration_boundaries(self):
        automatic_modules = (
            "weather",
            "openweather_overview",
            "openweather_forecast",
            "announcement_board",
        )

        for module in automatic_modules:
            for boundary in (1, 3600):
                with self.subTest(module=module, duration=boundary):
                    with tempfile.TemporaryDirectory() as directory:
                        store = ConfigStore(Path(directory) / "config.json")
                        playlist = json.loads(json.dumps(store.get()["playlist"]))
                        next(
                            item for item in playlist if item["module"] == module
                        )["duration"] = boundary

                        saved = store.update({"playlist": playlist})

                        self.assertEqual(
                            next(
                                item for item in saved["playlist"]
                                if item["module"] == module
                            )["duration"],
                            boundary,
                        )

    def test_weather_playlist_timing_is_automatic_from_ticker_loops(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            updated = store.get()
            updated.pop("revision")
            updated.pop("schema_version")
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
                        updated.pop("schema_version")
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
            saved.pop("schema_version")
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
            saved.pop("schema_version")
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
            legacy.pop("schema_version")
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
                "border": "none",
                "border_colour": "#ffffff",
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
                "background": "#000000", "border": "none", "border_colour": "#ffffff", "x": 2, "y": 20,
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
                "background": "#000000", "border": "none", "border_colour": "#ffffff", "x": 2, "y": 20,
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
            legacy.pop("schema_version")
            legacy["custom_modules"] = [{
                "id": "custom_legacy", "name": "Legacy message", "type": "text_scroll",
                "text": "LEGACY", "font": "5x7", "colour": "#ffffff",
                "background": "#000000", "border": "none", "border_colour": "#ffffff", "x": 2, "y": 20,
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

    def test_canvas_pixel_bitmap_is_exact_bounded_and_exclusive(self):
        pixels = bytearray(64 * 32 * 4)
        pixels[(10 * 64 + 12) * 4:(10 * 64 + 12) * 4 + 4] = bytes((255, 0, 128, 255))
        bitmap = base64.b64encode(pixels).decode("ascii")
        layer = {
            "id": "layer_bitmap", "name": "Bitmap", "type": "pixels", "visible": True,
            "properties": {
                "bitmap_rgba": bitmap, "offset_x": 0, "offset_y": 0, "rotation": 0,
            },
            "keyframes": [{
                "frame": 1, "easing": "linear",
                "properties": {"offset_x": 4, "offset_y": -3, "rotation": 90},
            }],
        }
        module = self.advanced_module(
            frame_count=2, layers=[layer],
            overrides=[{
                "frame": 0, "layer_id": "layer_bitmap",
                "properties": {"offset_x": -2},
            }],
        )

        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            saved = store.update({"custom_modules": [module]})["custom_modules"][0]
            self.assertEqual(saved["layers"][0]["properties"], layer["properties"])
            self.assertEqual(saved["layers"][0]["keyframes"], layer["keyframes"])

            invalid_properties = [
                {**layer["properties"], "points": [[1, 2]], "colour": "#ffffff"},
                {"bitmap_rgba": bitmap, "offset_x": 0, "offset_y": 0, "colour": "#ffffff"},
                {"bitmap_rgba": bitmap, "offset_x": 0},
                {"bitmap_rgba": bitmap, "offset_x": True, "offset_y": 0},
                {"bitmap_rgba": bitmap, "offset_x": 0.5, "offset_y": 0},
                {"bitmap_rgba": bitmap, "offset_x": -129, "offset_y": 0},
                {"bitmap_rgba": bitmap, "offset_x": 0, "offset_y": 192},
                {"bitmap_rgba": base64.b64encode(bytes(8191)).decode("ascii"), "offset_x": 0, "offset_y": 0},
                {"bitmap_rgba": bitmap.rstrip("="), "offset_x": 0, "offset_y": 0},
            ]
            invalid_modules = []
            for properties in invalid_properties:
                invalid = json.loads(json.dumps(module))
                invalid["layers"][0]["properties"] = properties
                invalid_modules.append(invalid)
            partial_bitmap = json.loads(json.dumps(module))
            partial_bitmap["layers"][0]["keyframes"][0]["properties"]["bitmap_rgba"] = bitmap
            invalid_modules.append(partial_bitmap)

            partial_alpha = bytearray(pixels)
            partial_alpha[3] = 1
            nonzero_transparent = bytearray(pixels)
            nonzero_transparent[0] = 1
            for invalid_bytes in (partial_alpha, nonzero_transparent):
                invalid = json.loads(json.dumps(module))
                invalid["layers"][0]["properties"]["bitmap_rgba"] = base64.b64encode(invalid_bytes).decode("ascii")
                invalid_modules.append(invalid)

            for invalid in invalid_modules:
                with self.subTest(properties=invalid["layers"][0]["properties"]), self.assertRaises(ValueError):
                    store.update({"custom_modules": [invalid]})

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
                "background": "#000000", "border": "none", "border_colour": "#ffffff", "x": 0, "y": 20,
                "direction": "left", "speed": 0.05, "loops": 1,
            }
            canvas = self.advanced_module(layers=[{
                "id": "layer_text", "name": "Text", "type": "text", "visible": True,
                "properties": {"x": 1, "y": 7, "text": "HI", "font": font, "colour": "#ffffff"},
                "keyframes": [],
            }], overrides=[])
            announcement = self.announcement(style={
                "foreground": "#FFFFFF", "background": "#000000",
                "headline_font": font, "message_font": font,
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
            self.assertEqual(updated["announcement_board"]["announcements"][0]["style"]["headline_font"], font)
            self.assertEqual(updated["announcement_board"]["announcements"][0]["style"]["message_font"], font)

    def test_scrolling_duration_supports_an_installed_font_outside_the_old_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            module = {
                "id": "custom_font", "name": "Installed font", "type": "text_scroll",
                "text": "HELLO", "font": "tom_thumb", "colour": "#ffffff",
                "background": "#000000", "border": "none", "border_colour": "#ffffff", "x": 0, "y": 20,
                "direction": "left", "speed": 0.05, "loops": 1,
            }
            playlist = store.get()["playlist"] + [
                {"module": module["id"], "duration": 1, "enabled": True},
            ]

            updated = store.update({"custom_modules": [module], "playlist": playlist})

            duration = updated["playlist"][-1]["duration"]
            self.assertGreater(duration, 0)


    def test_priority_1_6_basic_media_schema_is_media_only_and_canonical(self):
        candidate = json.loads(json.dumps(DEFAULT_CONFIG))
        media = {
            "id": "custom_media", "name": "Media", "type": "media",
            "source": "party.gif", "fit": "manual", "x": -7, "y": 3,
            "width": 80, "height": 40, "loops": 3,
            "playback_speed_percent": 125,
        }
        candidate["custom_modules"] = [media]
        candidate["playlist"].append({
            "module": "custom_media", "duration": 1, "enabled": True,
        })

        validated = validate_config(candidate)

        self.assertEqual(validated["custom_modules"][0]["playback_speed_percent"], 125.0)
        self.assertNotIn("text", validated["custom_modules"][0])
        self.assertEqual(validated["custom_modules"][0]["loops"], 3)

    def test_basic_gif_playlist_duration_matches_physical_delay_floor_speed_and_loops(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            first = Image.new("RGB", (2, 2), "red")
            second = Image.new("RGB", (2, 2), "blue")
            first.save(
                media_root / "timed.gif", save_all=True, append_images=[second],
                duration=[10, 40], loop=0,
            )
            store = ConfigStore(root / "config.json", media_dir=media_root)
            module = {
                "id": "custom_media", "name": "Media", "type": "media",
                "source": "timed.gif", "fit": "contain", "x": 0, "y": 0,
                "width": 64, "height": 32, "loops": 3,
                "playback_speed_percent": 400.0,
            }

            saved = store.update({
                "custom_modules": [module],
                "playlist": [{"module": module["id"], "duration": 99, "enabled": True}],
            })

            self.assertAlmostEqual(saved["playlist"][0]["duration"], 0.06)
            self.assertEqual(json.loads((root / "config.json").read_text())["playlist"][0]["duration"], 0.06)

    def test_same_basic_media_source_with_different_settings_is_probed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            Image.new("RGB", (2, 2), "red").save(media_root / "same.gif")
            store = ConfigStore(root / "config.json", media_dir=media_root)
            modules = [
                {
                    "id": f"custom_same_{index}", "name": f"Same {index}",
                    "type": "media", "source": "same.gif", "fit": "contain",
                    "x": 0, "y": 0, "width": 64, "height": 32,
                    "loops": loops, "playback_speed_percent": speed,
                }
                for index, (loops, speed) in enumerate(((1, 100.0), (3, 200.0)))
            ]
            playlist = [
                {"module": module["id"], "duration": 99, "enabled": True}
                for module in modules
            ]

            with patch.object(
                store, "_probe_raw_metadata", return_value=("gif", (0.1,)), create=True,
            ) as probe:
                saved = store.update({"custom_modules": modules, "playlist": playlist})

            probe.assert_called_once()
            self.assertAlmostEqual(saved["playlist"][0]["duration"], 0.1)
            self.assertAlmostEqual(saved["playlist"][1]["duration"], 0.15)

    def test_hard_linked_media_with_different_decoder_kinds_are_probed_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            gif_source = media_root / "alias.gif"
            mp4_source = media_root / "alias.mp4"
            Image.new("RGB", (2, 2), "red").save(gif_source)
            os.link(gif_source, mp4_source)
            store = ConfigStore(root / "config.json", media_dir=media_root)
            modules = [
                {
                    "id": "custom_gif_alias", "name": "GIF alias", "type": "media",
                    "source": gif_source.name, "fit": "contain", "x": 0, "y": 0,
                    "width": 64, "height": 32, "loops": 1,
                    "playback_speed_percent": 100.0,
                },
                {
                    "id": "custom_mp4_alias", "name": "MP4 alias", "type": "media",
                    "source": mp4_source.name, "fit": "contain", "x": 0, "y": 0,
                    "width": 64, "height": 32, "loops": 1,
                    "playback_speed_percent": 100.0,
                },
            ]

            def probe(_descriptor, suffix, _deadline):
                return ("gif", (0.1,)) if suffix == ".gif" else ("mp4", 20)

            with patch.object(store, "_probe_raw_metadata", side_effect=probe) as metadata_probe:
                saved = store.update({
                    "custom_modules": modules,
                    "playlist": [
                        {"module": module["id"], "duration": 99, "enabled": True}
                        for module in modules
                    ],
                })

            self.assertEqual(
                [call.args[1] for call in metadata_probe.call_args_list],
                [".gif", ".mp4"],
            )
            self.assertEqual(
                [item["duration"] for item in saved["playlist"]],
                [0.1, 1.0],
            )

    def test_default_media_canonicalization_exception_closes_descriptor_once(self):
        store = object.__new__(ConfigStore)
        store._metadata_workers = set()
        module = {
            "id": "custom_close_once", "name": "Close once", "type": "media",
            "source": "close.gif", "fit": "contain", "x": 0, "y": 0,
            "width": 64, "height": 32, "loops": 1,
            "playback_speed_percent": 100.0,
        }
        candidate = {
            "custom_modules": [module],
            "playlist": [{"module": module["id"], "duration": 99, "enabled": True}],
        }
        identity = (1, 2, 3, 4, 5)

        with patch.object(
            store, "_open_media_source", return_value=(123, identity),
        ), patch.object(
            store, "_probe_raw_metadata", side_effect=TimeoutError("probe timeout"),
        ), patch("config_store.os.close") as close, self.assertRaisesRegex(
            TimeoutError, "probe timeout",
        ):
            store._canonicalize_basic_media_durations(candidate)

        close.assert_called_once_with(123)

    def test_media_open_cleanup_failure_closes_all_directories_and_untransferred_source(self):
        store = object.__new__(ConfigStore)
        store.media_root = Path("/media")
        identity = (1, 2, 3, 4, 5)

        for failing_fd in (11, 10):
            with self.subTest(failing_fd=failing_fd):
                closed = []

                def close(descriptor):
                    closed.append(descriptor)
                    if descriptor == failing_fd:
                        raise OSError(f"close {descriptor} failed")

                with patch(
                    "config_store.os.open", side_effect=(10, 11, 12),
                ), patch(
                    "config_store.os.fstat",
                ) as fstat, patch.object(
                    store, "_stat_identity", return_value=identity,
                ), patch("config_store.os.close", side_effect=close), self.assertRaisesRegex(
                    OSError, f"close {failing_fd} failed",
                ):
                    fstat.return_value.st_mode = 0o100644
                    store._open_media_source("built_in/clip.gif", time.monotonic() + 1)

                self.assertEqual(closed, [11, 10, 12])

    def test_media_record_cleanup_attempts_all_and_preserves_hostile_primary_error(self):
        class HostilePrimary(Exception):
            def __str__(self):
                raise RuntimeError("primary stringification failed")

            def add_note(self, note):
                raise RuntimeError("primary note failed")

        class HostileCleanup(Exception):
            def __str__(self):
                raise RuntimeError("cleanup stringification failed")

        store = object.__new__(ConfigStore)
        records = [("one.gif", 21, ()), ("two.gif", 22, ())]
        primary = HostilePrimary()
        attempted = []

        def close(descriptor):
            attempted.append(descriptor)
            if descriptor == 21:
                raise HostileCleanup()
            raise KeyboardInterrupt("second cleanup failed")

        with patch("config_store.os.close", side_effect=close):
            try:
                try:
                    raise primary
                finally:
                    store._close_media_records(records, primary_error=primary)
            except BaseException as error:
                self.assertIs(error, primary)

        self.assertEqual(attempted, [21, 22])

    def test_media_record_cleanup_raises_earliest_cleanup_only_error_after_attempt_all(self):
        store = object.__new__(ConfigStore)
        records = [("one.gif", 31, ()), ("two.gif", 32, ())]
        first = ValueError("first close")
        attempted = []

        def close(descriptor):
            attempted.append(descriptor)
            raise first if descriptor == 31 else RuntimeError("second close")

        with patch("config_store.os.close", side_effect=close), self.assertRaises(
            ValueError,
        ) as raised:
            store._close_media_records(records)

        self.assertIs(raised.exception, first)
        self.assertEqual(attempted, [31, 32])

    def test_media_directory_entry_aba_is_rejected_by_ctime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            source = media_root / "aba.gif"
            Image.new("RGB", (2, 2), "red").save(source)
            store = ConfigStore(root / "config.json", media_dir=media_root)
            original_config = store.path.read_bytes()
            module = {
                "id": "custom_aba", "name": "ABA", "type": "media",
                "source": source.name, "fit": "contain", "x": 0, "y": 0,
                "width": 64, "height": 32, "loops": 1,
                "playback_speed_percent": 100.0,
            }

            def aba_entry():
                held = media_root / "held.gif"
                source.replace(held)
                Image.new("RGB", (3, 3), "blue").save(source)
                source.unlink()
                held.replace(source)

            with patch.object(
                store, "_probe_raw_metadata", return_value=("gif", (0.1,)),
            ), patch.object(
                store, "_test_before_media_commit_check", aba_entry,
            ), self.assertRaisesRegex(ValueError, "source changed"):
                store.update({
                    "custom_modules": [module],
                    "playlist": [{
                        "module": module["id"], "duration": 99, "enabled": True,
                    }],
                })

            self.assertEqual(store.path.read_bytes(), original_config)

    def test_media_replacement_at_last_prewrite_seam_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            source = media_root / "replace.gif"
            Image.new("RGB", (2, 2), "red").save(source)
            store = ConfigStore(root / "config.json", media_dir=media_root)
            original_config = store.path.read_bytes()
            module = {
                "id": "custom_replace", "name": "Replace", "type": "media",
                "source": source.name, "fit": "contain", "x": 0, "y": 0,
                "width": 64, "height": 32, "loops": 1,
                "playback_speed_percent": 100.0,
            }

            def replace_entry():
                replacement = media_root / "new.gif"
                Image.new("RGB", (3, 3), "blue").save(replacement)
                replacement.replace(source)

            with patch.object(
                store, "_probe_raw_metadata", return_value=("gif", (0.1,)),
            ), patch.object(
                store, "_test_before_media_commit_check", replace_entry,
            ), self.assertRaisesRegex(ValueError, "source changed"):
                store.update({
                    "custom_modules": [module],
                    "playlist": [{
                        "module": module["id"], "duration": 99, "enabled": True,
                    }],
                })

            self.assertEqual(store.path.read_bytes(), original_config)

    def test_same_size_media_mutation_with_restored_mtime_is_rejected_at_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            source = media_root / "mutable.gif"
            Image.new("RGB", (2, 2), "red").save(source)
            original_stat = source.stat()
            store = ConfigStore(root / "config.json", media_dir=media_root)
            original_config = store.path.read_bytes()
            module = {
                "id": "custom_mutable", "name": "Mutable", "type": "media",
                "source": source.name, "fit": "contain", "x": 0, "y": 0,
                "width": 64, "height": 32, "loops": 1,
                "playback_speed_percent": 100.0,
            }

            def mutate_in_place():
                data = source.read_bytes()
                source.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
                os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

            with patch.object(
                store, "_probe_raw_metadata", return_value=("gif", (0.1,)),
            ), patch.object(
                store, "_test_before_media_commit_check", mutate_in_place, create=True,
            ), self.assertRaisesRegex(ValueError, "source changed"):
                store.update({
                    "custom_modules": [module],
                    "playlist": [{
                        "module": module["id"], "duration": 99, "enabled": True,
                    }],
                })

            self.assertEqual(store.path.read_bytes(), original_config)

    def test_basic_media_source_symlink_is_never_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            Image.new("RGB", (2, 2), "red").save(media_root / "target.gif")
            (media_root / "linked.gif").symlink_to("target.gif")
            store = ConfigStore(root / "config.json", media_dir=media_root)
            module = {
                "id": "custom_linked", "name": "Linked", "type": "media",
                "source": "linked.gif", "fit": "contain", "x": 0, "y": 0,
                "width": 64, "height": 32, "loops": 1,
                "playback_speed_percent": 100.0,
            }

            with patch.object(store, "_probe_raw_metadata") as probe:
                saved = store.update({
                    "custom_modules": [module],
                    "playlist": [{
                        "module": module["id"], "duration": 99, "enabled": True,
                    }],
                })

            probe.assert_not_called()
            self.assertEqual(saved["playlist"][0]["duration"], 99)

    def test_metadata_worker_launch_overhead_consumes_aggregate_deadline_and_reaps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            Image.new("RGB", (2, 2), "red").save(media_root / "launch.gif")
            store = ConfigStore(root / "config.json", media_dir=media_root)
            module = {
                "id": "custom_launch", "name": "Launch", "type": "media",
                "source": "launch.gif", "fit": "contain", "x": 0, "y": 0,
                "width": 64, "height": 32, "loops": 1,
                "playback_speed_percent": 100.0,
            }
            real_popen = subprocess.Popen
            pids = []

            def slow_launch(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                pids.append(process.pid)
                time.sleep(0.08)
                return process

            with patch.object(
                ConfigStore, "METADATA_PROBE_BUDGET_SECONDS", 0.05,
            ), patch.object(
                ConfigStore, "_METADATA_WORKER_SCRIPT", "import time; time.sleep(60)",
            ), patch("config_store.subprocess.Popen", side_effect=slow_launch), \
                    self.assertRaisesRegex(TimeoutError, "aggregate budget"):
                store.update({
                    "custom_modules": [module],
                    "playlist": [{
                        "module": module["id"], "duration": 99, "enabled": True,
                    }],
                })

            self.assertEqual(self._wait_for_metadata_workers(store), set())
            self.assertEqual(len(pids), 1)
            with self.assertRaises(ProcessLookupError):
                os.kill(pids[0], 0)

    def test_wait_timeout_transfers_killed_worker_to_durable_reaper_within_budget(self):
        class Stream:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class Process:
            pid = 424242

            def __init__(self):
                self.stdout = Stream()
                self.stderr = Stream()
                self.returncode = None
                self.reaped = threading.Event()
                self.timed_waits = 0

            def communicate(self, timeout):
                raise subprocess.TimeoutExpired(["metadata-worker"], timeout)

            def poll(self):
                return -9 if self.reaped.is_set() else None

            def wait(self, timeout=None):
                if timeout is not None:
                    self.timed_waits += 1
                    raise subprocess.TimeoutExpired(["metadata-worker"], timeout)
                self.returncode = -9
                self.reaped.set()
                return self.returncode

        process = Process()
        store = object.__new__(ConfigStore)
        store._metadata_workers = set()
        started = time.monotonic()

        with patch.object(
            ConfigStore, "METADATA_PROBE_BUDGET_SECONDS", 0.05,
        ), patch("config_store.subprocess.Popen", return_value=process), \
                patch("config_store.os.killpg") as killpg, \
                self.assertRaisesRegex(TimeoutError, "aggregate budget"):
            store._probe_raw_metadata(7, ".gif", started + 0.05)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.2)
        killpg.assert_called_once_with(process.pid, 9)
        self.assertTrue(process.reaped.wait(1))
        self.assertGreaterEqual(process.timed_waits, 1)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)
        self.assertEqual(store._metadata_workers, set())

    def test_metadata_worker_never_launches_without_preestablished_reaper_owner(self):
        class FreshReaperStore(ConfigStore):
            _metadata_reaper_threads = ()
            _metadata_reaper_queues = ()
            _metadata_reaper_index = 0
            _metadata_reaper_emergency = {}
            _metadata_reaper_lock = threading.Lock()

        store = object.__new__(FreshReaperStore)
        store._metadata_workers = set()

        with patch(
            "config_store.threading.Thread.start", side_effect=RuntimeError("thread unavailable"),
        ), patch(
            "config_store.subprocess.Popen",
            side_effect=AssertionError("child launched without durable owner"),
        ) as popen, self.assertRaisesRegex(RuntimeError, "thread unavailable"):
            store._probe_raw_metadata(123, ".gif", time.monotonic() + 1)

        popen.assert_not_called()
        self.assertEqual(store._metadata_workers, set())

    def test_metadata_timeout_falls_back_to_exact_child_kill_if_group_kill_fails(self):
        class Stream:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class Process:
            pid = 454545

            def __init__(self):
                self.stdout = Stream()
                self.stderr = Stream()
                self.returncode = None
                self.killed = False
                self.reaped = False

            def communicate(self, timeout):
                raise subprocess.TimeoutExpired(["worker"], timeout)

            def poll(self):
                return -9 if self.reaped else None

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                if not self.killed:
                    raise AssertionError("exact child was not killed")
                self.reaped = True
                self.returncode = -9
                return -9

        store = object.__new__(ConfigStore)
        store._metadata_workers = set()
        process = Process()

        with patch("config_store.subprocess.Popen", return_value=process), patch(
            "config_store.os.killpg", side_effect=PermissionError("group unavailable"),
        ), self.assertRaisesRegex(TimeoutError, "aggregate budget"):
            store._probe_raw_metadata(123, ".gif", time.monotonic() + 1)

        self.assertTrue(process.killed)
        self.assertTrue(process.reaped)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)
        self.assertEqual(store._metadata_workers, set())

    def test_metadata_reaper_retries_interrupted_wait_before_releasing_ownership(self):
        class Stream:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class Process:
            pid = 434343

            def __init__(self):
                self.stdout = Stream()
                self.stderr = Stream()
                self.waits = 0

            def wait(self):
                self.waits += 1
                if self.waits == 1:
                    raise InterruptedError("signal")
                if self.waits == 2:
                    raise OSError("transient wait failure")
                return -9

        process = Process()
        workers = {process}

        ConfigStore._finish_metadata_worker(process, workers)

        self.assertEqual(process.waits, 3)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)
        self.assertEqual(workers, set())

    def test_metadata_reaper_closes_each_pipe_and_releases_accounting_on_close_fault(self):
        class Stream:
            def __init__(self, error=None):
                self.error = error
                self.close_attempted = False

            def close(self):
                self.close_attempted = True
                if self.error is not None:
                    raise self.error

        class Process:
            def __init__(self):
                self.stdout = Stream(ValueError("stdout close failed"))
                self.stderr = Stream()

            def wait(self):
                return -9

        process = Process()
        workers = {process}

        with self.assertRaisesRegex(ValueError, "stdout close failed"):
            ConfigStore._finish_metadata_worker(process, workers)

        self.assertTrue(process.stdout.close_attempted)
        self.assertTrue(process.stderr.close_attempted)
        self.assertEqual(workers, set())

    def test_timed_out_metadata_worker_is_killed_reaped_and_unregistered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            Image.new("RGB", (2, 2), "red").save(media_root / "slow.gif")
            store = ConfigStore(root / "config.json", media_dir=media_root)
            module = {
                "id": "custom_slow_worker", "name": "Slow worker", "type": "media",
                "source": "slow.gif", "fit": "contain", "x": 0, "y": 0,
                "width": 64, "height": 32, "loops": 1,
                "playback_speed_percent": 100.0,
            }
            started_pids = []

            with patch.object(
                ConfigStore, "METADATA_PROBE_BUDGET_SECONDS", 0.2,
            ), patch.object(
                ConfigStore, "_METADATA_WORKER_SCRIPT",
                "import time; time.sleep(60)", create=True,
            ), patch.object(
                ConfigStore, "_test_worker_started", started_pids.append, create=True,
            ), self.assertRaisesRegex(TimeoutError, "aggregate budget"):
                store.update({
                    "custom_modules": [module],
                    "playlist": [{
                        "module": module["id"], "duration": 99, "enabled": True,
                    }],
                })

            self.assertEqual(len(started_pids), 1)
            self.assertEqual(self._wait_for_metadata_workers(store), set())
            with self.assertRaises(ProcessLookupError):
                os.kill(started_pids[0], 0)

    def test_duplicate_failed_basic_media_source_is_attempted_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "bad.gif").write_bytes(b"not a gif")
            store = ConfigStore(root / "config.json", media_dir=media_root)
            modules = [
                {
                    "id": f"custom_bad_{index}", "name": f"Bad {index}",
                    "type": "media", "source": "bad.gif", "fit": "contain",
                    "x": 0, "y": 0, "width": 64, "height": 32,
                    "loops": index + 1, "playback_speed_percent": 100.0,
                }
                for index in range(2)
            ]
            playlist = [
                {"module": module["id"], "duration": 99, "enabled": True}
                for module in modules
            ]

            with patch.object(
                store, "_probe_raw_metadata", side_effect=ValueError("bad metadata"),
            ) as probe:
                saved = store.update({"custom_modules": modules, "playlist": playlist})

            probe.assert_called_once()
            self.assertEqual([item["duration"] for item in saved["playlist"]], [99, 99])

    def test_duplicate_basic_media_rows_probe_each_immutable_source_once_per_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            source = media_root / "same.gif"
            Image.new("RGB", (2, 2), "red").save(source)
            store = ConfigStore(root / "config.json", media_dir=media_root)
            module = {
                "id": "custom_same", "name": "Same", "type": "media",
                "source": source.name, "fit": "contain", "x": 0, "y": 0,
                "width": 64, "height": 32, "loops": 1,
                "playback_speed_percent": 100.0,
            }
            row = {"module": module["id"], "duration": 99, "enabled": True}

            with patch.object(
                store, "_probe_raw_metadata", return_value=("gif", (2.5,)),
            ) as probe:
                saved = store.update({
                    "custom_modules": [module],
                    "playlist": [dict(row), dict(row)],
                })

            probe.assert_called_once()
            self.assertEqual(
                [item["duration"] for item in saved["playlist"]], [2.5, 2.5],
            )

    def test_slow_media_probe_releases_config_lock_and_detects_intervening_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            source = media_root / "slow.gif"
            Image.new("RGB", (2, 2), "red").save(source)
            store = ConfigStore(root / "config.json", media_dir=media_root)
            module = {
                "id": "custom_slow", "name": "Slow", "type": "media",
                "source": source.name, "fit": "contain", "x": 0, "y": 0,
                "width": 64, "height": 32, "loops": 1,
                "playback_speed_percent": 100.0,
            }
            probe_entered = threading.Event()
            release_probe = threading.Event()
            get_done = threading.Event()
            update_done = threading.Event()
            media_results = []
            update_results = []

            def slow_probe(*_args):
                probe_entered.set()
                if not release_probe.wait(2):
                    raise TimeoutError("test did not release probe")
                return "gif", (2.5,)

            def media_update():
                try:
                    media_results.append(store.update({
                        "custom_modules": [module],
                        "playlist": [{
                            "module": module["id"], "duration": 99, "enabled": True,
                        }],
                    }, base_revision=0))
                except Exception as error:
                    media_results.append(error)

            def read_config():
                store.get()
                get_done.set()

            def unrelated_update():
                try:
                    update_results.append(store.update(
                        {"device": {"name": "Concurrent winner"}}, base_revision=0,
                    ))
                except Exception as error:
                    update_results.append(error)
                finally:
                    update_done.set()

            with patch.object(store, "_probe_raw_metadata", side_effect=slow_probe):
                media_thread = threading.Thread(target=media_update)
                media_thread.start()
                self.assertTrue(probe_entered.wait(1))
                get_thread = threading.Thread(target=read_config)
                update_thread = threading.Thread(target=unrelated_update)
                get_thread.start()
                update_thread.start()
                get_completed_while_probe_blocked = get_done.wait(1)
                update_completed_while_probe_blocked = update_done.wait(1)
                release_probe.set()
                for thread in (media_thread, get_thread, update_thread):
                    thread.join(2)
                    self.assertFalse(thread.is_alive())

            self.assertTrue(get_completed_while_probe_blocked)
            self.assertTrue(update_completed_while_probe_blocked)
            self.assertIsInstance(media_results[0], ConfigConflictError)
            self.assertIsInstance(update_results[0], dict)
            self.assertEqual(store.get()["device"]["name"], "Concurrent winner")

    def test_basic_media_probes_share_one_bounded_aggregate_metadata_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            for filename in ("first.gif", "second.gif"):
                Image.new("RGB", (2, 2), "red").save(media_root / filename)
            store = ConfigStore(root / "config.json", media_dir=media_root)
            original_config = store.path.read_bytes()
            modules = [
                {
                    "id": f"custom_{index}", "name": f"Media {index}", "type": "media",
                    "source": filename, "fit": "contain", "x": 0, "y": 0,
                    "width": 64, "height": 32, "loops": 1,
                    "playback_speed_percent": 100.0,
                }
                for index, filename in enumerate(("first.gif", "second.gif"))
            ]
            playlist = [
                {"module": module["id"], "duration": 99, "enabled": True}
                for module in modules
            ]

            def slow_probe(*_args):
                time.sleep(0.02)
                return "gif", (2.5,)

            started = time.monotonic()
            with patch.object(
                ConfigStore, "METADATA_PROBE_BUDGET_SECONDS", 0.01, create=True,
            ), patch.object(
                store, "_probe_raw_metadata", side_effect=slow_probe,
            ) as probe, self.assertRaisesRegex(TimeoutError, "aggregate budget"):
                store.update({
                    "custom_modules": modules, "playlist": playlist,
                })
            elapsed = time.monotonic() - started

            probe.assert_called_once()
            self.assertLess(elapsed, 0.1)
            self.assertEqual(store.path.read_bytes(), original_config)

    def test_media_source_replaced_during_probe_cannot_authorize_stale_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            source = media_root / "changing.gif"
            Image.new("RGB", (2, 2), "red").save(source)
            path = root / "config.json"
            store = ConfigStore(path, media_dir=media_root)
            original = path.read_bytes()
            module = {
                "id": "custom_changing", "name": "Changing", "type": "media",
                "source": source.name, "fit": "contain", "x": 0, "y": 0,
                "width": 64, "height": 32, "loops": 1,
                "playback_speed_percent": 100.0,
            }

            def replacing_probe(*_args):
                replacement = media_root / "replacement.gif"
                Image.new("RGB", (3, 3), "blue").save(replacement)
                replacement.replace(source)
                return "gif", (2.5,)

            with patch.object(
                store, "_probe_raw_metadata", side_effect=replacing_probe,
            ), self.assertRaisesRegex(ValueError, "source changed"):
                store.update({
                    "custom_modules": [module],
                    "playlist": [{
                        "module": module["id"], "duration": 99, "enabled": True,
                    }],
                })

            self.assertEqual(path.read_bytes(), original)

    def test_basic_media_computed_duration_over_save_bound_is_rejected_before_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            Image.new("RGB", (2, 2), "red").save(
                media_root / "too-long.gif", duration=40_000,
            )
            path = root / "config.json"
            store = ConfigStore(path, media_dir=media_root)
            original = path.read_bytes()
            module = {
                "id": "custom_too_long", "name": "Too long", "type": "media",
                "source": "too-long.gif", "fit": "contain", "x": 0, "y": 0,
                "width": 64, "height": 32, "loops": 10,
                "playback_speed_percent": 10.0,
            }

            changes = {
                "custom_modules": [module],
                "playlist": [{
                    "module": module["id"], "duration": 1, "enabled": True,
                }],
            }
            with self.assertRaisesRegex(ValueError, "duration.*3600"):
                store.update(changes)
            with self.assertRaisesRegex(ValueError, "duration.*3600"):
                store.validate_candidate(changes)

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(ConfigStore(path, media_dir=media_root).get(), store.get())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_basic_mp4_playlist_duration_uses_decoded_frames_physical_fps_speed_and_loops(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            source = media_root / "five-frames.mp4"
            subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
                "-i", "color=c=red:s=16x16:r=5:d=1", "-frames:v", "5",
                "-c:v", "mpeg4", str(source),
            ], check=True, timeout=15)
            store = ConfigStore(root / "config.json", media_dir=media_root)
            module = {
                "id": "custom_video", "name": "Video", "type": "media",
                "source": source.name, "fit": "contain", "x": 0, "y": 0,
                "width": 64, "height": 32, "loops": 3,
                "playback_speed_percent": 200.0,
            }

            saved = store.update({
                "custom_modules": [module],
                "playlist": [{"module": module["id"], "duration": 99, "enabled": True}],
            })

            # Physical Basic playback decodes all five source frames without
            # transcoding, then clocks them at 20 FPS / 2x for three loops.
            self.assertEqual(store._metadata_workers, set())
            self.assertAlmostEqual(saved["playlist"][0]["duration"], 5 / 20 / 2 * 3)

    def test_priority_1_6_builtin_media_speed_defaults_and_normalizes_exact_percentages(self):
        candidate = json.loads(json.dumps(DEFAULT_CONFIG))
        self.assertEqual(candidate["media_layout"]["splash"]["playback_speed_percent"], 100.0)
        self.assertEqual(candidate["media_layout"]["sleep"]["playback_speed_percent"], 100.0)

        candidate["media_layout"]["splash"]["playback_speed_percent"] = 100
        candidate["media_layout"]["sleep"]["playback_speed_percent"] = 10.0
        validated = validate_config(candidate)

        self.assertEqual(validated["media_layout"]["splash"]["playback_speed_percent"], 100.0)
        self.assertIs(type(validated["media_layout"]["splash"]["playback_speed_percent"]), float)
        self.assertEqual(validated["media_layout"]["sleep"]["playback_speed_percent"], 10.0)

    def test_priority_1_6_builtin_media_speed_rejects_unsupported_values(self):
        invalid = [
            True, False, 0, -10, 9.9, 400.1, 100.01,
            100.00000004, 399.99999996,
            "100", None, float("inf"), float("nan"),
        ]
        for value in invalid:
            with self.subTest(value=value):
                candidate = json.loads(json.dumps(DEFAULT_CONFIG))
                candidate["media_layout"]["splash"]["playback_speed_percent"] = value
                with self.assertRaisesRegex(ValueError, "playback speed"):
                    validate_config(candidate)

    def test_priority_1_6_schema_two_migrates_all_builtin_media_to_normal_speed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            legacy = json.loads(json.dumps(DEFAULT_CONFIG))
            legacy["schema_version"] = 2
            legacy["revision"] = 41
            legacy["device"]["name"] = "speed migration sentinel"
            for settings in legacy["media_layout"].values():
                settings.pop("playback_speed_percent", None)
            legacy["media_layout"]["sleep"]["gif_speed_percent"] = 275
            path.write_text(json.dumps(legacy))

            migrated = ConfigStore(path).get()

            self.assertEqual(migrated["schema_version"], CURRENT_CONFIG_SCHEMA_VERSION)
            self.assertEqual(migrated["revision"], 42)
            self.assertEqual(migrated["device"]["name"], "speed migration sentinel")
            self.assertNotIn("gif_speed_percent", migrated["media_layout"]["sleep"])
            self.assertEqual(migrated["media_layout"]["splash"]["playback_speed_percent"], 100.0)
            self.assertEqual(migrated["media_layout"]["sleep"]["playback_speed_percent"], 100.0)
            migrated_bytes = path.read_bytes()
            self.assertEqual(ConfigStore(path).get(), migrated)
            self.assertEqual(path.read_bytes(), migrated_bytes)

    def test_priority_1_6_schema_two_adds_nested_media_speed_defaults_without_touching_colours(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            legacy = json.loads(json.dumps(DEFAULT_CONFIG))
            legacy["schema_version"] = 2
            announcement = self.announcement()
            announcement.pop("playback_speed_percent")
            legacy["announcement_board"]["announcements"] = [announcement]
            canvas = self.advanced_module()
            canvas["background"] = "#ABCDEF"
            canvas["layers"].append({
                "id": "layer_media", "name": "Media", "type": "media", "visible": True,
                "properties": {"x": 0, "y": 0, "width": 64, "height": 32,
                               "rotation": 0, "source": "same.gif", "keep_aspect": True},
                "keyframes": [{"frame": 1, "easing": "step", "properties": {"source": "same.gif"}}],
            })
            canvas["overrides"].append({"frame": 2, "layer_id": "layer_media",
                                        "properties": {"source": "same.gif"}})
            legacy["custom_modules"] = [canvas]
            path.write_text(json.dumps(legacy))

            migrated = ConfigStore(path).get()

            self.assertEqual(migrated["announcement_board"]["announcements"][0]["playback_speed_percent"], 100.0)
            layer = migrated["custom_modules"][0]["layers"][1]
            self.assertEqual(layer["properties"]["playback_speed_percent"], 100.0)
            self.assertEqual(layer["keyframes"][0]["properties"]["playback_speed_percent"], 100.0)
            override = next(item for item in migrated["custom_modules"][0]["overrides"]
                            if item["layer_id"] == "layer_media")
            self.assertEqual(override["properties"]["playback_speed_percent"], 100.0)
            self.assertEqual(migrated["custom_modules"][0]["background"], "#ABCDEF")

    def test_priority_1_6_announcement_media_speed_is_canonical(self):
        candidate = json.loads(json.dumps(DEFAULT_CONFIG))
        announcement = self.announcement()
        announcement["playback_speed_percent"] = 175
        candidate["announcement_board"]["announcements"] = [announcement]

        validated = validate_config(candidate)

        self.assertEqual(
            validated["announcement_board"]["announcements"][0]["playback_speed_percent"],
            175.0,
        )


if __name__ == "__main__":
    unittest.main()
