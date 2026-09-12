import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from PIL import Image

from config_store import ConfigConflictError, ConfigStore, DEFAULT_CONFIG, validate_config
from display_controller import DisplayController
from pairing import PairedDeviceStore
from secret_store import SecretStore
from webapp import create_app


class ScreenMediaRevisionTests(unittest.TestCase):
    @staticmethod
    def _image(path, image_format="GIF"):
        Image.new("RGB", (2, 1), "red").save(path, format=image_format)

    @staticmethod
    def _animation(path, durations):
        frames = [Image.new("RGB", (2, 1), colour) for colour in ("red", "green", "blue")]
        frames[:len(durations)][0].save(
            path,
            format="GIF",
            save_all=True,
            append_images=frames[:len(durations)][1:],
            duration=durations,
            loop=0,
        )

    def test_packaged_screen_defaults_are_exact_decodable_assets(self):
        root = Path(__file__).resolve().parents[1] / "resources" / "media"
        defaults = {
            "splash": "built-in/splash_loading_squares.gif",
            "sleep": "built-in/sleep.gif",
        }

        for screen, reference in defaults.items():
            with self.subTest(screen=screen):
                row = next(
                    item for item in DEFAULT_CONFIG["playlist"]
                    if item["module"] == screen
                )
                self.assertEqual(DEFAULT_CONFIG["media"][screen], reference)
                self.assertEqual(row["source"], reference)
                self.assertEqual(row["sources"], [reference])
                path = root / reference
                self.assertTrue(path.is_file())
                self.assertFalse(path.is_symlink())
                with Image.open(path) as image:
                    image.verify()

    def test_discovery_uses_exact_prefix_type_decode_and_canonical_names(self):
        from screen_media import discover_screen_media

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            built_in = root / "built-in"
            built_in.mkdir()
            for name in ("splash_one.gif", "splash_same.gif", "sleep_one.gif"):
                self._image(built_in / name)
            self._image(built_in / "Splash_upper.gif")
            self._image(built_in / "splash_still.png", "PNG")
            (built_in / "splash_broken.gif").write_bytes(b"not an image")
            (built_in / "splash_directory.gif").mkdir()
            os.symlink(built_in / "splash_one.gif", built_in / "splash_link.gif")

            for name in ("upload.gif", "splash_same.gif"):
                self._image(root / name)
            self._image(root / "still.png", "PNG")
            self._image(root / "still.webp", "WEBP")
            self._image(root / "unsupported.bmp", "BMP")
            (root / "broken.gif").write_bytes(b"not an image")

            self.assertEqual(discover_screen_media(root, "splash"), (
                "built-in/splash_one.gif",
                "built-in/splash_same.gif",
                "splash_same.gif",
                "still.png",
                "still.webp",
                "upload.gif",
            ))
            self.assertEqual(discover_screen_media(root, "sleep"), (
                "built-in/sleep_one.gif",
                "splash_same.gif",
                "still.png",
                "still.webp",
                "upload.gif",
            ))

    def test_discovery_rejects_gif_that_verify_accepts_but_runtime_decode_rejects(self):
        from PIL import ImageSequence
        from screen_media import discover_screen_media

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "built-in").mkdir()
            complete = root / "complete.gif"
            self._animation(complete, [100, 100, 100])
            truncated = root / "truncated.gif"
            truncated.write_bytes(complete.read_bytes()[:-3])

            with Image.open(truncated) as image:
                image.verify()
            with self.assertRaises(OSError), Image.open(truncated) as image:
                for frame in ImageSequence.Iterator(image):
                    frame.load()

            self.assertNotIn(
                "truncated.gif",
                discover_screen_media(root, "splash"),
            )

    def test_each_screen_has_one_independent_toggle_and_selection_list(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        splash = next(item for item in config["playlist"] if item["module"] == "splash")
        sleep = next(item for item in config["playlist"] if item["module"] == "sleep")
        splash["enabled"] = False
        splash["sources"] = []
        splash["source"] = ""
        sleep_before = copy.deepcopy(sleep)

        validated = validate_config(config, enforce_save_bounds=True)

        validated_splash = next(
            item for item in validated["playlist"] if item["module"] == "splash"
        )
        validated_sleep = next(
            item for item in validated["playlist"] if item["module"] == "sleep"
        )
        self.assertFalse(validated_splash["enabled"])
        self.assertEqual(validated_splash["sources"], [])
        self.assertEqual(validated_sleep, sleep_before)

    def test_enabled_screen_rejects_empty_selection(self):
        for screen in ("splash", "sleep"):
            with self.subTest(screen=screen):
                config = copy.deepcopy(DEFAULT_CONFIG)
                row = next(item for item in config["playlist"] if item["module"] == screen)
                row["sources"] = []
                row["source"] = ""
                with self.assertRaisesRegex(ValueError, f"enabled {screen} screen"):
                    validate_config(config, enforce_save_bounds=True)

    def test_multiple_rows_for_one_screen_are_rejected(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["playlist"].insert(1, copy.deepcopy(config["playlist"][0]))
        config["playlist"][1]["id"] = "splash_second"

        with self.assertRaisesRegex(ValueError, "exactly one splash screen"):
            validate_config(config, enforce_save_bounds=True)

    def test_selected_media_validation_rejects_nonchoices_and_undecodable_files(self):
        from screen_media import validate_screen_media_config

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "built-in").mkdir()
            self._image(root / "built-in" / "splash_valid.gif")
            self._image(root / "built-in" / "sleep_valid.gif")
            self._image(root / "upload.gif")
            self._image(root / "still.png", "PNG")
            (root / "broken.gif").write_bytes(b"broken")
            self._image(root / "built-in" / "other.gif")
            config = copy.deepcopy(DEFAULT_CONFIG)
            rows = {
                item["module"]: item for item in config["playlist"]
                if item["module"] in {"splash", "sleep"}
            }
            rows["splash"].update(
                source="built-in/splash_valid.gif",
                sources=["built-in/splash_valid.gif", "upload.gif", "still.png"],
            )
            rows["sleep"].update(
                source="built-in/sleep_valid.gif",
                sources=["built-in/sleep_valid.gif"],
            )

            validated = validate_screen_media_config(config, root)
            self.assertEqual(tuple(validated["splash"]), tuple(rows["splash"]["sources"]))
            self.assertEqual(tuple(validated["sleep"]), tuple(rows["sleep"]["sources"]))

            invalid_references = (
                "built-in/other.gif",
                "built-in/Splash_upper.gif",
                "broken.gif",
                "../escape.gif",
                "movie.mp4",
            )
            for reference in invalid_references:
                with self.subTest(reference=reference):
                    candidate = copy.deepcopy(config)
                    row = next(
                        item for item in candidate["playlist"]
                        if item["module"] == "splash"
                    )
                    row["source"] = reference
                    row["sources"] = [reference]
                    with self.assertRaisesRegex(ValueError, "usable selected media"):
                        validate_screen_media_config(candidate, root)

    def test_config_api_rejects_undecodable_screen_selection_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            built_in = media_root / "built-in"
            built_in.mkdir(parents=True)
            self._image(built_in / "splash_loading_squares.gif")
            self._image(built_in / "sleep.gif")
            (media_root / "broken.gif").write_bytes(b"broken")
            store = ConfigStore(root / "config.json", media_dir=media_root)
            devices = PairedDeviceStore(root / "paired-devices.json")
            pairing = Mock()
            pairing.active_pin.return_value = None
            pairing.has_testing_pin.return_value = False
            pairing.verify.return_value = True
            controller = Mock()
            app = create_app(
                controller, store, pairing, secret_key="test",
                session_generation="screen-media-test", media_dir=media_root,
                device_store=devices,
                openweather_secrets=SecretStore(root / "openweather-key"),
            )
            client = app.test_client()
            client.post("/pair", data={"pin": "123456", "device_name": "Test"})
            before = store.path.read_bytes()
            current = store.get()
            playlist = copy.deepcopy(current["playlist"])
            splash = next(item for item in playlist if item["module"] == "splash")
            splash["source"] = "broken.gif"
            splash["sources"] = ["broken.gif"]

            response = client.post(
                "/api/config", json={"playlist": playlist},
                headers={
                    "If-Match": f'"{current["revision"]}"',
                    "X-Matrix-Client-Contract": "4",
                    "X-Matrix-Action": "paired-request",
                },
            )

            self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
            self.assertIn("usable selected media", response.get_json()["error"])
            self.assertEqual(store.path.read_bytes(), before)
            controller.reload.assert_not_called()

    def test_partial_runtime_repair_persists_only_the_affected_screen_once(self):
        from screen_media import repair_saved_screen

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            built_in = media_root / "built-in"
            built_in.mkdir(parents=True)
            self._image(built_in / "splash_loading_squares.gif")
            self._image(built_in / "sleep.gif")
            self._image(media_root / "valid.gif")
            store = ConfigStore(root / "config.json", media_dir=media_root)
            stale = store.get()
            splash = next(item for item in stale["playlist"] if item["module"] == "splash")
            splash["source"] = "missing.gif"
            splash["sources"] = ["missing.gif", "valid.gif", "broken.gif"]
            stale["media"]["splash"] = "missing.gif"
            store._write(stale)
            sleep_before = copy.deepcopy(next(
                item for item in stale["playlist"] if item["module"] == "sleep"
            ))
            unrelated_before = copy.deepcopy(stale["weather"])

            repaired = repair_saved_screen(store, stale, "splash", media_root)
            persisted_once = store.path.read_bytes()
            repeated = repair_saved_screen(store, store.get(), "splash", media_root)

            self.assertEqual(repaired.sources, ("valid.gif",))
            self.assertTrue(repaired.persisted)
            self.assertFalse(repeated.persisted)
            saved = store.get()
            saved_splash = next(item for item in saved["playlist"] if item["module"] == "splash")
            self.assertEqual(saved_splash["source"], "valid.gif")
            self.assertEqual(saved_splash["sources"], ["valid.gif"])
            self.assertEqual(next(
                item for item in saved["playlist"] if item["module"] == "sleep"
            ), sleep_before)
            self.assertEqual(saved["weather"], unrelated_before)
            self.assertEqual(saved["revision"], stale["revision"] + 1)
            self.assertEqual(store.path.read_bytes(), persisted_once)

    def test_all_invalid_runtime_selection_repairs_to_its_named_default(self):
        from screen_media import repair_saved_screen

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            built_in = media_root / "built-in"
            built_in.mkdir(parents=True)
            self._image(built_in / "splash_loading_squares.gif")
            self._image(built_in / "sleep.gif")
            store = ConfigStore(root / "config.json", media_dir=media_root)
            stale = store.get()
            row = next(item for item in stale["playlist"] if item["module"] == "splash")
            row.update(source="missing.gif", sources=["missing.gif"])
            stale["media"]["splash"] = "missing.gif"
            store._write(stale)

            repaired = repair_saved_screen(store, stale, "splash", media_root)

            self.assertEqual(repaired.sources, ("built-in/splash_loading_squares.gif",))
            self.assertTrue(repaired.persisted)
            self.assertFalse(repaired.default_unavailable)

    def test_unavailable_default_reports_empty_without_persisting_invalid_repair(self):
        from screen_media import repair_saved_screen

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            (media_root / "built-in").mkdir(parents=True)
            store = ConfigStore(root / "config.json", media_dir=media_root)
            stale = store.get()
            row = next(item for item in stale["playlist"] if item["module"] == "splash")
            row.update(source="missing.gif", sources=["missing.gif"])
            stale["media"]["splash"] = "missing.gif"
            store._write(stale)
            before = store.path.read_bytes()

            repaired = repair_saved_screen(store, stale, "splash", media_root)

            self.assertEqual(repaired.sources, ())
            self.assertFalse(repaired.persisted)
            self.assertTrue(repaired.default_unavailable)
            self.assertEqual(store.path.read_bytes(), before)

    def test_concurrent_valid_screen_edit_wins_over_stale_runtime_repair(self):
        from screen_media import repair_saved_screen

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            built_in = media_root / "built-in"
            built_in.mkdir(parents=True)
            self._image(built_in / "splash_loading_squares.gif")
            self._image(built_in / "sleep.gif")
            self._image(media_root / "concurrent.gif")
            store = ConfigStore(root / "config.json", media_dir=media_root)
            stale = store.get()
            stale_row = next(item for item in stale["playlist"] if item["module"] == "splash")
            stale_row.update(source="missing.gif", sources=["missing.gif"])
            stale["media"]["splash"] = "missing.gif"
            store._write(stale)

            def concurrent_update(*_args, **_kwargs):
                current = store.get()
                row = next(item for item in current["playlist"] if item["module"] == "splash")
                row.update(source="concurrent.gif", sources=["concurrent.gif"])
                current["media"]["splash"] = "concurrent.gif"
                current["revision"] += 1
                store._write(current)
                raise ConfigConflictError(current["revision"])

            proxy = Mock(update=Mock(side_effect=concurrent_update), get=store.get)
            repaired = repair_saved_screen(proxy, stale, "splash", media_root)

            self.assertFalse(repaired.persisted)
            self.assertEqual(repaired.sources, ("concurrent.gif",))
            saved = store.get()
            self.assertEqual(saved["media"]["splash"], "concurrent.gif")

    def test_splash_uses_every_effective_identifier_once_before_refill(self):
        from screen_media import MediaInfo, ScreenMediaRuntime

        identities = {
            "one.gif": (1, 1, 1, 1, 1),
            "two.gif": (1, 2, 1, 1, 1),
            "three.gif": (1, 3, 1, 1, 1),
        }
        catalog = lambda _screen: {
            reference: MediaInfo(reference, identity, "gif", 1000)
            for reference, identity in identities.items()
        }
        shuffles = []

        def shuffle(values):
            shuffles.append(tuple(values))
            values.reverse()

        runtime = ScreenMediaRuntime(catalog=catalog, shuffle=shuffle)
        config = copy.deepcopy(DEFAULT_CONFIG)
        row = next(item for item in config["playlist"] if item["module"] == "splash")
        row.update(source="one.gif", sources=["one.gif", "two.gif", "three.gif"])

        first_round = [runtime.choose_splash(config).reference for _ in range(3)]
        first_next_round = runtime.choose_splash(config).reference

        self.assertEqual(first_round, ["one.gif", "two.gif", "three.gif"])
        self.assertEqual(first_next_round, "one.gif")
        self.assertEqual(shuffles, [
            ("one.gif", "two.gif", "three.gif"),
            ("one.gif", "two.gif", "three.gif"),
        ])

    def test_splash_failure_retires_only_that_content_identity_until_replacement(self):
        from screen_media import MediaInfo, ScreenMediaRuntime

        identities = {
            "one.gif": (1, 1, 1, 1, 1),
            "two.gif": (1, 2, 1, 1, 1),
        }

        def catalog(_screen):
            return {
                reference: MediaInfo(reference, identity, "gif", 1000)
                for reference, identity in identities.items()
            }

        runtime = ScreenMediaRuntime(catalog=catalog, shuffle=lambda values: values.reverse())
        config = copy.deepcopy(DEFAULT_CONFIG)
        row = next(item for item in config["playlist"] if item["module"] == "splash")
        row.update(source="one.gif", sources=["one.gif", "two.gif"])

        failed = runtime.choose_splash(config)
        runtime.retire_splash(failed)
        after_failure = runtime.choose_splash(config)
        identities["one.gif"] = (1, 10, 2, 2, 2)
        after_replacement = runtime.choose_splash(config)
        runtime.retire_splash(after_replacement)
        runtime.retire_splash(after_failure)

        self.assertEqual(failed.reference, "one.gif")
        self.assertEqual(after_failure.reference, "two.gif")
        self.assertEqual(after_replacement.reference, "one.gif")
        self.assertIsNone(runtime.choose_splash(config))

    def test_sleep_retains_one_identity_until_invalidated_or_session_ends(self):
        from screen_media import MediaInfo, ScreenMediaRuntime

        identities = {
            "one.gif": (1, 1, 1, 1, 1),
            "two.gif": (1, 2, 1, 1, 1),
        }
        choices = []

        def catalog(_screen):
            return {
                reference: MediaInfo(reference, identity, "gif", 1000)
                for reference, identity in identities.items()
            }

        def choose(values):
            choices.append(tuple(item.reference for item in values))
            return values[0]

        runtime = ScreenMediaRuntime(catalog=catalog, choice=choose)
        config = copy.deepcopy(DEFAULT_CONFIG)
        row = next(item for item in config["playlist"] if item["module"] == "sleep")
        row.update(source="one.gif", sources=["one.gif"])

        entered = runtime.begin_sleep(config)
        row["sources"].append("two.gif")
        after_addition = runtime.current_sleep(config)
        row.update(source="two.gif", sources=["two.gif"])
        after_removal = runtime.current_sleep(config)
        identities["two.gif"] = (1, 20, 2, 2, 2)
        after_replacement = runtime.current_sleep(config)
        runtime.end_sleep()
        row.update(source="one.gif", sources=["one.gif", "two.gif"])
        reentered = runtime.begin_sleep(config)

        self.assertEqual(entered.reference, "one.gif")
        self.assertIs(after_addition, entered)
        self.assertEqual(after_removal.reference, "two.gif")
        self.assertNotEqual(after_replacement.identity, after_removal.identity)
        self.assertEqual(reentered.reference, "one.gif")
        self.assertEqual(choices, [
            ("one.gif",),
            ("two.gif",),
            ("two.gif",),
            ("one.gif", "two.gif"),
        ])

    def test_catalog_uses_normalized_selected_gif_timing_and_invalidates_on_replacement(self):
        from screen_media import ScreenMediaCatalog

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            built_in = root / "built-in"
            built_in.mkdir()
            animated = built_in / "splash_animated.gif"
            self._animation(animated, [5, 20, 50])
            self._image(root / "still.png", "PNG")
            catalog = ScreenMediaCatalog(root)

            first = catalog("splash")
            cached = catalog("splash")
            self._animation(animated, [100, 100, 100])
            replaced = catalog("splash")

            self.assertEqual(first["built-in/splash_animated.gif"].gif_duration_ms, 170)
            self.assertIs(first["built-in/splash_animated.gif"], cached["built-in/splash_animated.gif"])
            self.assertIsNone(first["still.png"].gif_duration_ms)
            self.assertEqual(replaced["built-in/splash_animated.gif"].gif_duration_ms, 300)
            self.assertNotEqual(
                replaced["built-in/splash_animated.gif"].identity,
                first["built-in/splash_animated.gif"].identity,
            )

    def test_selected_gif_mean_sets_still_interval_without_changing_gif_hold(self):
        from screen_media import MediaInfo, ScreenMediaRuntime

        catalog = lambda _screen: {
            "five.gif": MediaInfo(
                "five.gif", (1, 1, 1, 1, 1), "gif", 5000,
                frame_delays_ms=(2000, 3000),
            ),
            "nine.gif": MediaInfo(
                "nine.gif", (1, 2, 1, 1, 1), "gif", 9000,
                frame_delays_ms=(4000, 5000),
            ),
            "still.png": MediaInfo("still.png", (1, 3, 1, 1, 1), "still", None),
        }
        runtime = ScreenMediaRuntime(catalog=catalog, choice=lambda values: values[-1])
        controller = DisplayController(Mock(), Mock(media_root=Path("/nonexistent")))
        controller._screen_runtime = runtime
        controller._execute = Mock(return_value=True)
        config = copy.deepcopy(DEFAULT_CONFIG)
        row = next(item for item in config["playlist"] if item["module"] == "sleep")
        row.update(
            duration=8,
            source="five.gif",
            sources=["five.gif", "nine.gif", "still.png"],
        )

        self.assertTrue(controller._execute_scheduled_screen(config, row, entering_sleep=True))
        self.assertEqual(controller._execute.call_args.args[2], 7.0)

        runtime.end_sleep()
        runtime._choice = lambda values: values[0]
        self.assertTrue(controller._execute_scheduled_screen(config, row, entering_sleep=True))
        self.assertEqual(controller._execute.call_args.args[2], 8)

    def test_still_interval_uses_exact_selected_gif_boundaries_and_fallback(self):
        from screen_media import MediaInfo, screen_still_duration_seconds

        def gif(reference, *delays):
            return MediaInfo(
                reference, (1, len(reference), 1, 1, 1), "gif", sum(delays),
                frame_delays_ms=delays,
            )

        row = {"layout": {"playback_speed_percent": 100.0}}
        still = MediaInfo("still.png", (1, 99, 1, 1, 1), "still", None)
        cases = (
            ((gif("five.gif", 2000, 3000),), 5.0),
            ((gif("ten.gif", 4000, 6000),), 10.0),
            ((gif("below.gif", 4999),), 7.0),
            ((gif("above.gif", 10001),), 7.0),
            ((still,), 7.0),
            ((gif("six.gif", 6000), gif("six.gif", 6000), still), 6.0),
        )
        for selected, expected in cases:
            with self.subTest(expected=expected, selected=selected):
                self.assertEqual(screen_still_duration_seconds(row, selected), expected)

        half_speed = {"layout": {"playback_speed_percent": 50.0}}
        self.assertEqual(
            screen_still_duration_seconds(half_speed, (gif("scaled.gif", 2500),)),
            5.0,
        )

    def test_scheduled_splash_retries_the_next_bag_member_after_playback_failure(self):
        from screen_media import MediaInfo, ScreenMediaRuntime

        catalog = lambda _screen: {
            "one.gif": MediaInfo(
                "one.gif", (1, 1, 1, 1, 1), "gif", 100,
                frame_delays_ms=(100,),
            ),
            "two.gif": MediaInfo(
                "two.gif", (1, 2, 1, 1, 1), "gif", 200,
                frame_delays_ms=(100, 100),
            ),
        }
        runtime = ScreenMediaRuntime(catalog=catalog, shuffle=lambda values: values.reverse())
        store = Mock(media_root=Path("/nonexistent"))
        controller = DisplayController(Mock(), store)
        controller._screen_runtime = runtime
        controller._execute = Mock(side_effect=[RuntimeError("decode failed"), True])
        config = copy.deepcopy(DEFAULT_CONFIG)
        row = next(item for item in config["playlist"] if item["module"] == "splash")
        row.update(source="one.gif", sources=["one.gif", "two.gif"])

        rendered = controller._execute_scheduled_screen(
            config, row, maximum_duration=100,
        )

        self.assertTrue(rendered)
        self.assertEqual(controller._execute.call_count, 2)
        first, second = controller._execute.call_args_list
        self.assertEqual(first.args[1]["media"]["splash"], "one.gif")
        self.assertEqual(second.args[1]["media"]["splash"], "two.gif")
        self.assertEqual(first.args[2], 8)
        self.assertEqual(second.args[2], 8)

    def test_active_scheduler_routes_splash_through_the_stateful_runtime(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        splash = next(item for item in config["playlist"] if item["module"] == "splash")
        sleep = next(item for item in config["playlist"] if item["module"] == "sleep")
        config["playlist"] = [splash, sleep]
        store = Mock(get=Mock(return_value=config))
        controller = DisplayController(Mock(), store)
        controller._take_preview = Mock(return_value=None)
        controller._take_media_preview = Mock(return_value=None)
        controller._take_announcement_preview = Mock(return_value=None)
        controller._is_active = Mock(return_value=True)
        controller._seconds_until_schedule_change = Mock(return_value=5)

        def execute_screen(*_args, **_kwargs):
            controller._shutdown.set()
            return True

        controller._execute_scheduled_screen = Mock(side_effect=execute_screen)

        def legacy_execute(*_args, **_kwargs):
            controller._shutdown.set()
            return True

        controller._execute = Mock(side_effect=legacy_execute)

        controller._run_loop_unchecked()

        controller._execute_scheduled_screen.assert_called_once_with(
            config, splash, maximum_duration=5,
        )
        controller._execute.assert_not_called()

    def test_scheduled_sleep_retains_choice_and_caps_duration_at_wake_boundary(self):
        from screen_media import MediaInfo, ScreenMediaRuntime

        catalog = lambda _screen: {
            "one.gif": MediaInfo(
                "one.gif", (1, 1, 1, 1, 1), "gif", 100,
                frame_delays_ms=(100,),
            ),
            "two.gif": MediaInfo(
                "two.gif", (1, 2, 1, 1, 1), "gif", 100,
                frame_delays_ms=(100,),
            ),
        }
        runtime = ScreenMediaRuntime(catalog=catalog, choice=lambda values: values[0])
        controller = DisplayController(Mock(), Mock(media_root=Path("/nonexistent")))
        controller._screen_runtime = runtime
        controller._execute = Mock(return_value=True)
        config = copy.deepcopy(DEFAULT_CONFIG)
        row = next(item for item in config["playlist"] if item["module"] == "sleep")
        row.update(source="one.gif", sources=["one.gif"])

        first = controller._execute_scheduled_screen(
            config, row, maximum_duration=1.5, entering_sleep=True,
        )
        row["sources"].append("two.gif")
        second = controller._execute_scheduled_screen(
            config, row, maximum_duration=1.5, entering_sleep=False,
        )

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(
            [call.args[1]["media"]["sleep"] for call in controller._execute.call_args_list],
            ["one.gif", "one.gif"],
        )
        self.assertEqual(
            [call.args[2] for call in controller._execute.call_args_list],
            [1.5, 1.5],
        )

    def test_inactive_scheduler_owns_one_sleep_session_until_wake(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        sleep = next(item for item in config["playlist"] if item["module"] == "sleep")
        config["playlist"] = [sleep]
        controller = DisplayController(Mock(), Mock(get=Mock(return_value=config)))
        controller._take_preview = Mock(return_value=None)
        controller._take_media_preview = Mock(return_value=None)
        controller._take_announcement_preview = Mock(return_value=None)
        active_checks = iter((False, False, False, False, True))

        def is_active(config, now=None):
            del config, now
            active = next(active_checks)
            if active:
                controller._shutdown.set()
            return active

        controller._is_active = is_active
        controller._seconds_until_schedule_change = Mock(return_value=5)
        controller._screen_runtime = Mock()
        controller._execute_scheduled_screen = Mock(return_value=True)

        def legacy_execute(*_args, **_kwargs):
            controller._shutdown.set()
            return True

        controller._execute = Mock(side_effect=legacy_execute)

        controller._run_loop_unchecked()

        self.assertEqual(controller._execute_scheduled_screen.call_args_list, [
            call(config, sleep, maximum_duration=5, entering_sleep=True),
            call(config, sleep, maximum_duration=5, entering_sleep=False),
        ])
        controller._screen_runtime.end_sleep.assert_called_once_with()
        controller._execute.assert_not_called()

    def test_scheduled_screen_repairs_stale_saved_selection_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            built_in = media_root / "built-in"
            built_in.mkdir(parents=True)
            self._image(built_in / "splash_loading_squares.gif")
            self._image(built_in / "sleep.gif")
            self._image(media_root / "valid.gif")
            store = ConfigStore(root / "config.json", media_dir=media_root)
            stale = store.get()
            row = next(item for item in stale["playlist"] if item["module"] == "splash")
            row.update(source="missing.gif", sources=["missing.gif", "valid.gif"])
            stale["media"]["splash"] = "missing.gif"
            store._write(stale)
            controller = DisplayController(Mock(), store)
            controller._execute = Mock(return_value=True)

            rendered = controller._execute_scheduled_screen(stale, row)

            self.assertTrue(rendered)
            selected = controller._execute.call_args.args[1]
            self.assertEqual(selected["media"]["splash"], "valid.gif")
            saved = store.get()
            saved_row = next(
                item for item in saved["playlist"] if item["module"] == "splash"
            )
            self.assertEqual(saved_row["sources"], ["valid.gif"])
            self.assertEqual(saved["revision"], stale["revision"] + 1)

    def test_sleep_playback_failure_forces_bounded_reselection(self):
        from screen_media import MediaInfo, ScreenMediaRuntime

        catalog = lambda _screen: {
            reference: MediaInfo(reference, identity, "still", None)
            for reference, identity in {
                "one.png": (1, 1, 1, 1, 1),
                "two.png": (1, 2, 1, 1, 1),
            }.items()
        }
        runtime = ScreenMediaRuntime(catalog=catalog, choice=lambda values: values[0])
        controller = DisplayController(Mock(), Mock(media_root=Path("/nonexistent")))
        controller._screen_runtime = runtime
        controller._execute = Mock(side_effect=[RuntimeError("draw failed"), True])
        config = copy.deepcopy(DEFAULT_CONFIG)
        row = next(item for item in config["playlist"] if item["module"] == "sleep")
        row.update(source="one.png", sources=["one.png", "two.png"])

        rendered = controller._execute_scheduled_screen(
            config, row, entering_sleep=True,
        )

        self.assertTrue(rendered)
        self.assertEqual(
            [call.args[1]["media"]["sleep"] for call in controller._execute.call_args_list],
            ["one.png", "two.png"],
        )

    def test_saved_preview_does_not_consume_or_repair_runtime_screen_state(self):
        with tempfile.TemporaryDirectory() as directory:
            media_root = Path(directory) / "media"
            built_in = media_root / "built-in"
            built_in.mkdir(parents=True)
            self._image(built_in / "splash_loading_squares.gif")
            config = copy.deepcopy(DEFAULT_CONFIG)
            store = Mock(media_root=media_root, get=Mock(return_value=config))
            controller = DisplayController(Mock(), store)
            runtime = Mock()
            controller._screen_runtime = runtime

            with patch.object(controller, "start"):
                controller.preview("splash")
            pending = controller._take_preview()
            self.assertIsNotNone(pending)
            try:
                runtime.assert_not_called()
                runtime.choose_splash.assert_not_called()
                runtime.begin_sleep.assert_not_called()
                runtime.current_sleep.assert_not_called()
                store.update.assert_not_called()
                self.assertEqual(
                    pending.config["media"]["splash"],
                    "built-in/splash_loading_squares.gif",
                )
            finally:
                pending.close()


if __name__ == "__main__":
    unittest.main()
