import copy
import os
import stat
import tempfile
import threading
import time
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

    def test_legacy_splash_is_finite_saved_selection_only_and_fully_decoded(self):
        from screen_media import ScreenMediaCatalog, discover_screen_media, validate_screen_media_config
        from tests.test_config_store import ConfigStoreTests

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            built_in = root / "built-in"
            built_in.mkdir()
            for name in ("loading_squares.gif", "splash_loading_squares.gif", "sleep.gif", "unrelated.gif"):
                self._animation(built_in / name, [100, 200, 300])
            saved = ConfigStoreTests.startup_predecessor()
            for row in saved["playlist"]:
                if row["module"] in {"splash", "sleep"}:
                    row["sources"] = [row["source"]]
            legacy = "built-in/loading_squares.gif"
            self.assertNotIn(legacy, discover_screen_media(root, "splash"))
            self.assertIn(legacy, discover_screen_media(root, "splash", saved_config=saved))
            self.assertNotIn(legacy, discover_screen_media(root, "sleep", saved_config=saved))
            catalog = ScreenMediaCatalog(root, saved_config=lambda: saved)
            self.assertEqual(catalog("splash")[legacy].gif_duration_ms, 600)
            self.assertIn(legacy, validate_screen_media_config(saved, root, saved_config=saved)["splash"])
            with self.assertRaises(ValueError):
                validate_screen_media_config(saved, root)
            for reference in ("built-in/unrelated.gif", "built-in/../loading_squares.gif", "../loading_squares.gif"):
                candidate = copy.deepcopy(saved)
                row = candidate["playlist"][0]
                row.update(source=reference, sources=[reference])
                with self.subTest(reference=reference), self.assertRaises(ValueError):
                    validate_screen_media_config(candidate, root, saved_config=saved)
            new_selection = copy.deepcopy(DEFAULT_CONFIG)
            new_selection["playlist"][0].update(source=legacy, sources=[legacy])
            with self.assertRaises(ValueError):
                validate_screen_media_config(new_selection, root, saved_config=DEFAULT_CONFIG)
            candidate = copy.deepcopy(saved)
            row = copy.deepcopy(candidate["playlist"][0])
            row["id"] = "new_splash"
            candidate["playlist"].append(row)
            with self.assertRaises(ValueError):
                validate_screen_media_config(candidate, root, saved_config=saved)
            path = built_in / "loading_squares.gif"
            valid = path.read_bytes()
            for defect in ("corrupt", "truncated", "format", "symlink", "missing", "directory"):
                path.unlink()
                if defect == "corrupt": path.write_bytes(b"not a gif")
                elif defect == "truncated": path.write_bytes(valid[:-3])
                elif defect == "format": self._image(path, "PNG")
                elif defect == "symlink": path.symlink_to(built_in / "splash_loading_squares.gif")
                elif defect == "directory": path.mkdir()
                with self.subTest(defect=defect):
                    self.assertNotIn(legacy, discover_screen_media(root, "splash", saved_config=saved))
                    self.assertNotIn(legacy, catalog("splash"))
                    with self.assertRaises(ValueError):
                        validate_screen_media_config(saved, root, saved_config=saved)
                if path.is_dir(): path.rmdir()
                elif path.exists() or path.is_symlink(): path.unlink()
                path.write_bytes(valid)

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

    def test_multiple_rows_for_one_screen_still_require_unique_ids(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["playlist"].insert(1, copy.deepcopy(config["playlist"][0]))

        with self.assertRaisesRegex(ValueError, "ids must be unique"):
            validate_config(config, enforce_save_bounds=True)

    def test_admitted_multirow_predecessor_validates_and_consumes_each_rows_meaning(self):
        from screen_media import MediaInfo, ScreenMediaRuntime, validate_screen_media_config

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            built_in = root / "built-in"
            built_in.mkdir()
            self._image(built_in / "splash_loading_squares.gif")
            self._image(built_in / "sleep.gif")
            self._image(root / "second.gif")
            config = copy.deepcopy(DEFAULT_CONFIG)
            first = next(item for item in config["playlist"] if item["module"] == "splash")
            second = copy.deepcopy(first)
            second.update({
                "id": "splash_second", "source": "second.gif",
                "sources": ["second.gif"], "duration": 19,
                "layout": {**second["layout"], "x": 9},
            })
            config["playlist"].insert(1, second)

            validated = validate_screen_media_config(config, root)
            catalog = {
                reference: MediaInfo(
                    reference, (1, index, 1, 1, 1), "gif", index * 100,
                    frame_delays_ms=(index * 100,),
                )
                for index, reference in enumerate(validated["splash"], start=1)
            }
            runtime = ScreenMediaRuntime(
                catalog=lambda screen: catalog if screen == "splash" else {},
                shuffle=lambda values: None,
            )
            first_media = runtime.choose_splash(config, first)
            second_media = runtime.choose_splash(config, second)
            assert first_media is not None
            assert second_media is not None

            self.assertEqual(validated["splash"], (
                "built-in/splash_loading_squares.gif", "second.gif",
            ))
            self.assertEqual(first_media.reference, first["source"])
            self.assertEqual(second_media.reference, second["source"])
            self.assertEqual(runtime.duration(config, "splash", first_media, first), 0.1)
            self.assertEqual(runtime.duration(config, "splash", second_media, second), 0.2)

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

    def test_config_api_saves_disabled_empty_splash_and_sleep_conditionally(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            built_in = media_root / "built-in"
            built_in.mkdir(parents=True)
            self._image(built_in / "splash_loading_squares.gif")
            self._image(built_in / "sleep.gif")
            store = ConfigStore(root / "config.json", media_dir=media_root)
            devices = PairedDeviceStore(root / "paired-devices.json")
            pairing = Mock()
            pairing.active_pin.return_value = None
            pairing.has_testing_pin.return_value = False
            pairing.verify.return_value = True
            controller = Mock()
            app = create_app(
                controller, store, pairing, secret_key="test",
                session_generation="screen-media-empty-test", media_dir=media_root,
                device_store=devices,
                openweather_secrets=SecretStore(root / "openweather-key"),
            )
            client = app.test_client()
            client.post("/pair", data={"pin": "123456", "device_name": "Test"})

            for expected_revision, screen in enumerate(("splash", "sleep"), start=1):
                with self.subTest(screen=screen):
                    current = store.get()
                    playlist = copy.deepcopy(current["playlist"])
                    row = next(item for item in playlist if item["module"] == screen)
                    other_screen = "sleep" if screen == "splash" else "splash"
                    other_before = copy.deepcopy(next(
                        item for item in current["playlist"]
                        if item["module"] == other_screen
                    ))
                    row.update(enabled=False, source="", sources=[])

                    response = client.post(
                        "/api/config", json={"playlist": playlist},
                        headers={
                            "If-Match": f'"{current["revision"]}"',
                            "X-Matrix-Client-Contract": "4",
                            "X-Matrix-Action": "paired-request",
                        },
                    )

                    self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                    saved = response.get_json()
                    saved_row = next(
                        item for item in saved["playlist"] if item["module"] == screen
                    )
                    self.assertEqual(
                        (saved_row["enabled"], saved_row["source"], saved_row["sources"]),
                        (False, "", []),
                    )
                    self.assertEqual(next(
                        item for item in saved["playlist"]
                        if item["module"] == other_screen
                    ), other_before)
                    self.assertEqual(saved["revision"], expected_revision)
                    self.assertEqual(ConfigStore(store.path, media_dir=media_root).get(), saved)

            self.assertEqual(controller.reload.call_count, 2)
            stale = client.post(
                "/api/config", json={"device": {"name": "stale"}},
                headers={
                    "If-Match": '"0"', "X-Matrix-Client-Contract": "4",
                    "X-Matrix-Action": "paired-request",
                },
            )
            self.assertEqual(stale.status_code, 412)
            before_invalid = store.path.read_bytes()
            current = store.get()
            playlist = copy.deepcopy(current["playlist"])
            next(item for item in playlist if item["module"] == "splash")["enabled"] = True
            invalid = client.post(
                "/api/config", json={"playlist": playlist},
                headers={
                    "If-Match": f'"{current["revision"]}"',
                    "X-Matrix-Client-Contract": "4",
                    "X-Matrix-Action": "paired-request",
                },
            )
            self.assertEqual(invalid.status_code, 400)
            self.assertIn("enabled splash screen", invalid.get_json()["error"])
            self.assertEqual(store.path.read_bytes(), before_invalid)

    def test_saved_screen_preview_auto_starts_after_successful_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            built_in = media_root / "built-in"
            built_in.mkdir(parents=True)
            self._image(built_in / "splash_loading_squares.gif")
            self._image(built_in / "sleep.gif")
            store = ConfigStore(root / "config.json", media_dir=media_root)
            devices = PairedDeviceStore(root / "paired-devices.json")
            pairing = Mock()
            pairing.active_pin.return_value = None
            pairing.has_testing_pin.return_value = False
            pairing.verify.return_value = True
            controller = DisplayController(Mock(), store)

            def wait_for_shutdown():
                controller._shutdown.wait(5)

            controller._run_loop = wait_for_shutdown
            app = create_app(
                controller, store, pairing, secret_key="test",
                session_generation="screen-preview-restart-test", media_dir=media_root,
                device_store=devices,
                openweather_secrets=SecretStore(root / "openweather-key"),
            )
            client = app.test_client()
            client.post("/pair", data={"pin": "123456", "device_name": "Test"})
            headers = {
                "X-Matrix-Action": "paired-request",
                "X-Matrix-Client-Contract": "4",
            }
            try:
                for screen in ("splash", "sleep"):
                    with self.subTest(screen=screen):
                        initial = client.post(f"/api/preview/{screen}", headers=headers)
                        stopped = client.post("/api/control/stop", headers=headers)
                        restarted = client.post(f"/api/preview/{screen}", headers=headers)

                        self.assertEqual(initial.status_code, 202)
                        self.assertEqual(stopped.status_code, 200)
                        self.assertEqual(restarted.status_code, 202)
                        self.assertTrue(controller.running)
            finally:
                controller.stop(timeout=1)
                controller.device_preview_manager.close()
                controller._openweather_provider.shutdown()

    def test_saved_screen_preview_restarts_after_stopped_successful_reload(self):
        from matrix_framework import Matrix

        class HardwareBoundary:
            show_gif = Matrix.show_gif

            def __init__(self, preview_started):
                self.frames = []
                self.pixel = None
                self.preview_invocation = 0
                self.preview_started = preview_started

            def clear(self):
                self.pixel = None

            def set_brightness(self, _value):
                pass

            def draw_image(self, image, **_kwargs):
                self.pixel = image.convert("RGB").getpixel((0, 0))
                if 0 < self.preview_invocation <= len(self.preview_started):
                    self.preview_started[self.preview_invocation - 1].set()

            def swap(self):
                self.frames.append(self.pixel)

        def wait_for_frames(matrix, count):
            deadline = time.monotonic() + 2
            while len(matrix.frames) < count and time.monotonic() < deadline:
                time.sleep(0.01)
            return len(matrix.frames) >= count

        for screen in ("splash", "sleep"):
            for sequence in (
                "stop-preview",
                "stop-save-preview",
                "stop-reload-preview",
                "reload-stop-before-ack-preview",
            ):
                with (
                    self.subTest(screen=screen, sequence=sequence),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    media_root = root / "media"
                    built_in = media_root / "built-in"
                    built_in.mkdir(parents=True)
                    self._image(built_in / "splash_loading_squares.gif")
                    self._image(built_in / "sleep.gif")
                    frames = [
                        Image.new("RGB", (8, 8), colour)
                        for colour in ("red", "blue")
                    ]
                    frames[0].save(
                        media_root / "proof.gif", format="GIF", save_all=True,
                        append_images=frames[1:], duration=[30, 30], loop=0,
                    )
                    for frame in frames:
                        frame.close()
                    store = ConfigStore(root / "config.json", media_dir=media_root)
                    config = store.get()
                    for item in config["playlist"]:
                        item["enabled"] = False
                    row = next(
                        item for item in config["playlist"]
                        if item["module"] == screen
                    )
                    row.update(source="proof.gif", sources=["proof.gif"])
                    config["media"][screen] = "proof.gif"
                    store._write(config)
                    devices = PairedDeviceStore(root / "paired-devices.json")
                    pairing = Mock()
                    pairing.active_pin.return_value = None
                    pairing.has_testing_pin.return_value = False
                    pairing.verify.return_value = True
                    preview_started = (threading.Event(), threading.Event())
                    matrix = HardwareBoundary(preview_started)
                    controller = DisplayController(matrix, store)
                    preview_invocations = []
                    original_execute = controller._execute

                    def tracked_execute(
                        name, config, duration, *, pinned_handle=None, pinned_handles=None,
                    ):
                        if name == screen:
                            preview_invocations.append(name)
                            matrix.preview_invocation = len(preview_invocations)
                        return original_execute(
                            name, config, duration,
                            pinned_handle=pinned_handle, pinned_handles=pinned_handles,
                        )

                    controller._execute = tracked_execute
                    app = create_app(
                        controller, store, pairing,
                        secret_key="dormant-reload-preview-test",
                        session_generation="dormant-reload-preview-test",
                        media_dir=media_root, device_store=devices,
                        openweather_secrets=SecretStore(root / "openweather-key"),
                    )
                    client = app.test_client()
                    client.post(
                        "/pair", data={"pin": "123456", "device_name": "Test"},
                    )
                    headers = {
                        "X-Matrix-Action": "paired-request",
                        "X-Matrix-Client-Contract": "4",
                    }
                    release_scheduler = threading.Event()
                    stop_thread = None
                    try:
                        initial = client.post(
                            f"/api/preview/{screen}", headers=headers,
                        )
                        self.assertEqual(initial.status_code, 202)
                        self.assertTrue(preview_started[0].wait(2))
                        self.assertTrue(wait_for_frames(matrix, 2))
                        self.assertEqual(matrix.frames[:2], [(255, 0, 0), (0, 0, 255)])

                        if sequence == "reload-stop-before-ack-preview":
                            scheduler_waiting = threading.Event()
                            original_acknowledge = controller._acknowledge_reload_boundary

                            def gated_acknowledge():
                                with controller._state_lock:
                                    reload_pending = bool(controller._reload_obligations)
                                if reload_pending:
                                    scheduler_waiting.set()
                                    self.assertTrue(release_scheduler.wait(3))
                                if controller._shutdown.is_set():
                                    return False
                                return original_acknowledge()

                            controller._acknowledge_reload_boundary = gated_acknowledge
                            reloaded = client.post(
                                "/api/control/reload", headers=headers,
                            )
                            self.assertEqual(reloaded.status_code, 200)
                            self.assertTrue(scheduler_waiting.wait(2))
                            stop_responses = []
                            stop_thread = threading.Thread(
                                target=lambda: stop_responses.append(client.post(
                                    "/api/control/stop", headers=headers,
                                )),
                            )
                            stop_thread.start()
                            self.assertTrue(controller._shutdown.wait(0.5))
                            release_scheduler.set()
                            stop_thread.join(2)
                            self.assertFalse(stop_thread.is_alive())
                            self.assertEqual(stop_responses[0].status_code, 200)
                            with controller._state_lock:
                                self.assertEqual(
                                    set(controller._reload_obligations.values()),
                                    {"settled-success"},
                                )
                        else:
                            stopped = client.post(
                                "/api/control/stop", headers=headers,
                            )
                            self.assertEqual(stopped.status_code, 200)
                            if sequence == "stop-save-preview":
                                current = store.get()
                                saved = client.post(
                                    "/api/config",
                                    json={"device": {"name": "After Stop"}},
                                    headers={
                                        **headers,
                                        "If-Match": f'"{current["revision"]}"',
                                    },
                                )
                                self.assertEqual(
                                    saved.status_code, 200,
                                    saved.get_data(as_text=True),
                                )
                            elif sequence == "stop-reload-preview":
                                reloaded = client.post(
                                    "/api/control/reload", headers=headers,
                                )
                                self.assertEqual(reloaded.status_code, 200)

                        matrix.frames.clear()
                        fresh = client.post(
                            f"/api/preview/{screen}", headers=headers,
                        )
                        self.assertEqual(
                            fresh.status_code, 202, fresh.get_data(as_text=True),
                        )
                        if sequence == "reload-stop-before-ack-preview":
                            with controller._state_lock:
                                self.assertFalse(controller._reload_obligations)
                        self.assertTrue(
                            preview_started[1].wait(2),
                            "the second saved Preview invocation did not become active",
                        )
                        self.assertEqual(preview_invocations, [screen, screen])
                        self.assertTrue(wait_for_frames(matrix, 2))
                        self.assertEqual(matrix.frames[:2], [(255, 0, 0), (0, 0, 255)])
                    finally:
                        release_scheduler.set()
                        if stop_thread is not None:
                            stop_thread.join(2)
                        controller.stop(timeout=2)
                        controller.device_preview_manager.close()
                        controller._openweather_provider.shutdown()

    def test_config_api_ignores_missing_alias_for_disabled_empty_screen(self):
        for screen in ("splash", "sleep"):
            with self.subTest(screen=screen), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                media_root = root / "media"
                built_in = media_root / "built-in"
                built_in.mkdir(parents=True)
                self._image(built_in / "splash_loading_squares.gif")
                self._image(built_in / "sleep.gif")
                vanished = media_root / "vanished.png"
                self._image(vanished, "PNG")
                store = ConfigStore(root / "config.json", media_dir=media_root)
                stale = store.get()
                row = next(item for item in stale["playlist"] if item["module"] == screen)
                row.update(source="vanished.png", sources=["vanished.png"])
                stale["media"][screen] = "vanished.png"
                store._write(stale)
                vanished.unlink()
                before = store.path.read_bytes()
                devices = PairedDeviceStore(root / "paired-devices.json")
                pairing = Mock()
                pairing.active_pin.return_value = None
                pairing.has_testing_pin.return_value = False
                pairing.verify.return_value = True
                controller = Mock()
                app = create_app(
                    controller, store, pairing, secret_key="test",
                    session_generation=f"missing-{screen}-alias-test",
                    media_dir=media_root, device_store=devices,
                    openweather_secrets=SecretStore(root / "openweather-key"),
                )
                client = app.test_client()
                client.post("/pair", data={"pin": "123456", "device_name": "Test"})
                current = store.get()
                playlist = copy.deepcopy(current["playlist"])
                row = next(item for item in playlist if item["module"] == screen)
                row.update(enabled=False, source="", sources=[])

                response = client.post(
                    "/api/config", json={"playlist": playlist},
                    headers={
                        "If-Match": f'"{current["revision"]}"',
                        "X-Matrix-Action": "paired-request",
                        "X-Matrix-Client-Contract": "4",
                    },
                )

                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                saved = response.get_json()
                saved_row = next(
                    item for item in saved["playlist"] if item["module"] == screen
                )
                self.assertEqual(
                    (saved_row["enabled"], saved_row["source"], saved_row["sources"]),
                    (False, "", []),
                )
                self.assertEqual(saved["media"][screen], "vanished.png")
                self.assertNotEqual(store.path.read_bytes(), before)
                controller.reload.assert_called_once_with()

    def test_config_api_ignores_unselected_missing_alias_with_selected_legacy_sibling(self):
        for screen in ("splash", "sleep"):
            for second_enabled in (False, True):
                with (
                    self.subTest(screen=screen, second_enabled=second_enabled),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    media_root = root / "media"
                    built_in = media_root / "built-in"
                    built_in.mkdir(parents=True)
                    self._image(built_in / "splash_loading_squares.gif")
                    self._image(built_in / "sleep.gif")
                    vanished = media_root / "vanished.png"
                    valid = media_root / "valid.png"
                    self._image(vanished, "PNG")
                    self._image(valid, "PNG")
                    store = ConfigStore(root / "config.json", media_dir=media_root)
                    grandfathered = store.get()
                    first = next(
                        item for item in grandfathered["playlist"]
                        if item["module"] == screen
                    )
                    first_id = first["id"]
                    first.update(source="vanished.png", sources=["vanished.png"])
                    grandfathered["media"][screen] = "vanished.png"
                    second = copy.deepcopy(first)
                    second.update(
                        id=f"{screen}_legacy_second", enabled=second_enabled,
                        source="valid.png", sources=["valid.png"],
                    )
                    grandfathered["playlist"].insert(
                        grandfathered["playlist"].index(first) + 1, second,
                    )
                    store._write(grandfathered)
                    vanished.unlink()
                    devices = PairedDeviceStore(root / "paired-devices.json")
                    pairing = Mock()
                    pairing.active_pin.return_value = None
                    pairing.has_testing_pin.return_value = False
                    pairing.verify.return_value = True
                    controller = Mock()
                    app = create_app(
                        controller, store, pairing,
                        secret_key="legacy-sibling-alias-test",
                        session_generation="legacy-sibling-alias-test",
                        media_dir=media_root, device_store=devices,
                        openweather_secrets=SecretStore(root / "openweather-key"),
                    )
                    client = app.test_client()
                    client.post(
                        "/pair", data={"pin": "123456", "device_name": "Test"},
                    )
                    current = store.get()
                    playlist = copy.deepcopy(current["playlist"])
                    first = next(
                        item for item in playlist if item.get("id") == first_id
                    )
                    first.update(enabled=False, source="", sources=[])
                    headers = {
                        "If-Match": f'"{current["revision"]}"',
                        "X-Matrix-Action": "paired-request",
                        "X-Matrix-Client-Contract": "4",
                    }

                    response = client.post(
                        "/api/config", json={"playlist": playlist}, headers=headers,
                    )

                    self.assertEqual(
                        response.status_code, 200, response.get_data(as_text=True),
                    )
                    saved = response.get_json()
                    saved_rows = [
                        item for item in saved["playlist"]
                        if item["module"] == screen
                    ]
                    self.assertEqual([item["id"] for item in saved_rows], [
                        first_id, f"{screen}_legacy_second",
                    ])
                    self.assertEqual(
                        (saved_rows[0]["enabled"], saved_rows[0]["source"], saved_rows[0]["sources"]),
                        (False, "", []),
                    )
                    self.assertEqual(
                        (
                            saved_rows[1]["enabled"], saved_rows[1]["source"],
                            saved_rows[1]["sources"],
                        ),
                        (second_enabled, "valid.png", ["valid.png"]),
                    )
                    self.assertEqual(saved["media"][screen], "vanished.png")

                    valid_alias = client.post(
                        "/api/config",
                        json={
                            "playlist": playlist,
                            "media": {screen: "valid.png"},
                        },
                        headers={
                            **headers, "If-Match": f'"{saved["revision"]}"',
                        },
                    )
                    self.assertEqual(
                        valid_alias.status_code, 200,
                        valid_alias.get_data(as_text=True),
                    )
                    controlled = valid_alias.get_json()
                    controlled_rows = [
                        item for item in controlled["playlist"]
                        if item["module"] == screen
                    ]
                    self.assertEqual(controlled_rows, saved_rows)
                    self.assertEqual(controlled["media"][screen], "valid.png")

                    for invalid_kind in ("enabled-empty", "selected-missing"):
                        invalid_playlist = copy.deepcopy(controlled["playlist"])
                        invalid_first = next(
                            item for item in invalid_playlist
                            if item.get("id") == first_id
                        )
                        invalid_second = next(
                            item for item in invalid_playlist
                            if item.get("id") == f"{screen}_legacy_second"
                        )
                        if invalid_kind == "enabled-empty":
                            invalid_first["enabled"] = True
                        else:
                            invalid_second.update(
                                source="missing.png", sources=["missing.png"],
                            )
                        before_invalid = store.path.read_bytes()
                        rejected = client.post(
                            "/api/config",
                            json={"playlist": invalid_playlist},
                            headers={
                                **headers,
                                "If-Match": f'"{controlled["revision"]}"',
                            },
                        )
                        self.assertEqual(rejected.status_code, 400)
                        self.assertEqual(store.path.read_bytes(), before_invalid)

                    self.assertEqual(controller.reload.call_count, 2)

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

    def test_splash_bags_are_isolated_by_logical_row_identity(self):
        from screen_media import MediaInfo, ScreenMediaRuntime

        catalog = {
            reference: MediaInfo(reference, (1, index, 1, 1, 1), "gif", 1000)
            for index, reference in enumerate(("a1.gif", "a2.gif", "b1.gif", "b2.gif"), 1)
        }
        runtime = ScreenMediaRuntime(
            catalog=lambda _screen: catalog,
            shuffle=lambda values: values.reverse(),
        )
        config = copy.deepcopy(DEFAULT_CONFIG)
        first = next(item for item in config["playlist"] if item["module"] == "splash")
        first.update(id="splash_a", source="a1.gif", sources=["a1.gif", "a2.gif"])
        second = copy.deepcopy(first)
        second.update(id="splash_b", source="b1.gif", sources=["b1.gif", "b2.gif"])
        config["playlist"].insert(config["playlist"].index(first) + 1, second)

        selected = [
            runtime.choose_splash(config, row).reference
            for row in (first, second, first, second)
        ]

        self.assertEqual(selected, ["a1.gif", "b1.gif", "a2.gif", "b2.gif"])

    def test_splash_retirement_is_scoped_to_the_failing_row(self):
        from screen_media import MediaInfo, ScreenMediaRuntime

        catalog = {
            reference: MediaInfo(reference, (1, index, 1, 1, 1), "gif", 1000)
            for index, reference in enumerate(("shared.gif", "a.gif", "b.gif"), 1)
        }
        runtime = ScreenMediaRuntime(
            catalog=lambda _screen: catalog,
            shuffle=lambda values: values.reverse(),
        )
        config = copy.deepcopy(DEFAULT_CONFIG)
        first = next(item for item in config["playlist"] if item["module"] == "splash")
        first.update(id="splash_a", source="shared.gif", sources=["shared.gif", "a.gif"])
        second = copy.deepcopy(first)
        second.update(id="splash_b", sources=["shared.gif", "b.gif"])
        config["playlist"].insert(config["playlist"].index(first) + 1, second)

        failed = runtime.choose_splash(config, first)
        runtime.retire_splash(failed, first)
        sibling = runtime.choose_splash(config, second)
        retry = runtime.choose_splash(config, first)

        self.assertEqual(failed.reference, "shared.gif")
        self.assertEqual(sibling.reference, "shared.gif")
        self.assertEqual(retry.reference, "a.gif")

    def test_removed_splash_row_drops_its_unfinished_bag_before_readdition(self):
        from screen_media import MediaInfo, ScreenMediaRuntime

        catalog = {
            reference: MediaInfo(reference, (1, index, 1, 1, 1), "gif", 1000)
            for index, reference in enumerate(("a1.gif", "a2.gif", "b1.gif", "b2.gif"), 1)
        }
        runtime = ScreenMediaRuntime(
            catalog=lambda _screen: catalog,
            shuffle=lambda values: values.reverse(),
        )
        config = copy.deepcopy(DEFAULT_CONFIG)
        first = next(item for item in config["playlist"] if item["module"] == "splash")
        first.update(id="splash_a", source="a1.gif", sources=["a1.gif", "a2.gif"])
        second = copy.deepcopy(first)
        second.update(id="splash_b", source="b1.gif", sources=["b1.gif", "b2.gif"])
        config["playlist"].insert(config["playlist"].index(first) + 1, second)

        before_removal = runtime.choose_splash(config, first)
        config["playlist"].remove(first)
        runtime.choose_splash(config, second)
        config["playlist"].insert(config["playlist"].index(second), first)
        after_readdition = runtime.choose_splash(config, first)

        self.assertEqual(before_removal.reference, "a1.gif")
        self.assertEqual(after_readdition.reference, "a1.gif")

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

    def test_sleep_choices_are_isolated_by_logical_row_identity(self):
        from screen_media import MediaInfo, ScreenMediaRuntime

        catalog = {
            reference: MediaInfo(reference, (1, index, 1, 1, 1), "gif", 1000)
            for index, reference in enumerate(("a1.gif", "a2.gif", "b1.gif", "b2.gif"), 1)
        }
        choices = []

        def choose(values):
            choices.append(tuple(item.reference for item in values))
            return values[0]

        runtime = ScreenMediaRuntime(catalog=lambda _screen: catalog, choice=choose)
        config = copy.deepcopy(DEFAULT_CONFIG)
        first = next(item for item in config["playlist"] if item["module"] == "sleep")
        first.update(id="sleep_a", source="a1.gif", sources=["a1.gif", "a2.gif"])
        second = copy.deepcopy(first)
        second.update(id="sleep_b", source="b1.gif", sources=["b1.gif", "b2.gif"])
        config["playlist"].insert(config["playlist"].index(first) + 1, second)

        selected = [
            runtime.begin_sleep(config, first),
            runtime.current_sleep(config, second),
            runtime.current_sleep(config, first),
        ]

        self.assertEqual([item.reference for item in selected], [
            "a1.gif", "b1.gif", "a1.gif",
        ])
        self.assertIs(selected[2], selected[0])
        self.assertEqual(choices, [
            ("a1.gif", "a2.gif"),
            ("b1.gif", "b2.gif"),
        ])

    def test_sleep_retirement_is_scoped_to_the_failing_row(self):
        from screen_media import MediaInfo, ScreenMediaRuntime

        catalog = {
            reference: MediaInfo(reference, (1, index, 1, 1, 1), "still", None)
            for index, reference in enumerate(("shared.png", "a.png", "b.png"), 1)
        }
        runtime = ScreenMediaRuntime(
            catalog=lambda _screen: catalog,
            choice=lambda values: values[0],
        )
        config = copy.deepcopy(DEFAULT_CONFIG)
        first = next(item for item in config["playlist"] if item["module"] == "sleep")
        first.update(id="sleep_a", source="shared.png", sources=["shared.png", "a.png"])
        second = copy.deepcopy(first)
        second.update(id="sleep_b", sources=["shared.png", "b.png"])
        config["playlist"].insert(config["playlist"].index(first) + 1, second)

        failed = runtime.begin_sleep(config, first)
        sibling = runtime.current_sleep(config, second)
        runtime.retire_sleep(failed, first)
        retry = runtime.current_sleep(config, first)
        sibling_after_failure = runtime.current_sleep(config, second)

        self.assertEqual(failed.reference, "shared.png")
        self.assertEqual(retry.reference, "a.png")
        self.assertIs(sibling_after_failure, sibling)
        self.assertEqual(sibling.reference, "shared.png")

    def test_removed_sleep_row_drops_its_retained_choice_before_readdition(self):
        from screen_media import MediaInfo, ScreenMediaRuntime

        catalog = {
            reference: MediaInfo(reference, (1, index, 1, 1, 1), "still", None)
            for index, reference in enumerate(("a.png", "b.png"), 1)
        }
        choices = []

        def choose(values):
            choices.append(tuple(item.reference for item in values))
            return values[0]

        runtime = ScreenMediaRuntime(catalog=lambda _screen: catalog, choice=choose)
        config = copy.deepcopy(DEFAULT_CONFIG)
        first = next(item for item in config["playlist"] if item["module"] == "sleep")
        first.update(id="sleep_a", source="a.png", sources=["a.png"])
        second = copy.deepcopy(first)
        second.update(id="sleep_b", source="b.png", sources=["b.png"])
        config["playlist"].insert(config["playlist"].index(first) + 1, second)

        runtime.begin_sleep(config, first)
        runtime.current_sleep(config, second)
        config["playlist"].remove(first)
        runtime.current_sleep(config, second)
        config["playlist"].insert(config["playlist"].index(second), first)
        runtime.current_sleep(config, first)

        self.assertEqual(choices, [("a.png",), ("b.png",), ("a.png",)])

    def test_sleep_session_end_clears_every_rows_retained_choice(self):
        from screen_media import MediaInfo, ScreenMediaRuntime

        catalog = {
            reference: MediaInfo(reference, (1, index, 1, 1, 1), "still", None)
            for index, reference in enumerate(("a.png", "b.png"), 1)
        }
        choices = []

        def choose(values):
            choices.append(tuple(item.reference for item in values))
            return values[0]

        runtime = ScreenMediaRuntime(catalog=lambda _screen: catalog, choice=choose)
        config = copy.deepcopy(DEFAULT_CONFIG)
        first = next(item for item in config["playlist"] if item["module"] == "sleep")
        first.update(id="sleep_a", source="a.png", sources=["a.png"])
        second = copy.deepcopy(first)
        second.update(id="sleep_b", source="b.png", sources=["b.png"])
        config["playlist"].insert(config["playlist"].index(first) + 1, second)

        runtime.begin_sleep(config, first)
        runtime.current_sleep(config, second)
        runtime.end_sleep()
        runtime.begin_sleep(config, first)
        runtime.current_sleep(config, second)

        self.assertEqual(choices, [
            ("a.png",), ("b.png",), ("a.png",), ("b.png",),
        ])

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

    def test_selected_gif_mean_sets_still_interval_without_overriding_gif_runtime(self):
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
        self.assertEqual(controller._execute.call_args.args[2], 5.0)

    def test_screen_gifs_use_exact_scaled_one_pass_runtime_not_row_duration(self):
        from screen_media import MediaInfo, ScreenMediaRuntime

        catalog = {
            "short.gif": MediaInfo(
                "short.gif", (1, 1, 1, 1, 1), "gif", 135,
                frame_delays_ms=(5, 20, 100),
            ),
            "long.gif": MediaInfo(
                "long.gif", (1, 2, 1, 1, 1), "gif", 500,
                frame_delays_ms=(200, 300),
            ),
        }
        config = copy.deepcopy(DEFAULT_CONFIG)
        first = next(item for item in config["playlist"] if item["module"] == "splash")
        first.update(
            id="splash_short", source="short.gif", sources=["short.gif"], duration=1,
            layout={**first["layout"], "playback_speed_percent": 133.3},
        )
        second = copy.deepcopy(first)
        second.update(
            id="splash_long", source="long.gif", sources=["long.gif"], duration=99,
        )
        config["playlist"] = [first, second]
        controller = DisplayController(Mock(), Mock(media_root=Path("/nonexistent")))
        controller._screen_runtime = ScreenMediaRuntime(
            catalog=lambda _screen: catalog, shuffle=lambda values: None,
        )
        controller._execute = Mock(return_value=True)

        self.assertTrue(controller._execute_scheduled_screen(config, first))
        self.assertTrue(controller._execute_scheduled_screen(config, second))

        calls = controller._execute.call_args_list
        self.assertEqual(
            [call.args[1]["media"]["splash"] for call in calls],
            ["short.gif", "long.gif"],
        )
        self.assertAlmostEqual(calls[0].args[2], 0.1000225056264066)
        self.assertAlmostEqual(calls[1].args[2], 0.37509377344336087)
        self.assertNotEqual(calls[0].args[2], first["duration"])
        self.assertNotEqual(calls[1].args[2], second["duration"])

    def test_scheduled_screen_gifs_complete_one_real_pass_despite_render_overhead(self):
        from matrix_framework import Matrix

        class Clock:
            def __init__(self):
                self.now = 0.0

            def __call__(self):
                return self.now

            def advance(self, seconds):
                self.now += seconds

        class HardwareBoundary:
            show_gif = Matrix.show_gif

            def __init__(self, clock):
                self.clock = clock
                self.frames = []
                self.pixel = None

            def clear(self):
                pass

            def draw_image(self, image, **_kwargs):
                self.pixel = image.convert("RGB").getpixel((0, 0))[0]
                self.clock.advance(0.02)

            def swap(self):
                self.frames.append(self.pixel)

        # Sleep no longer stops after one pass (it loops until wake; see
        # test_saved_sleep_gif_loops_seamlessly_in_one_execution_until_night_ends).
        for screen in ("splash",):
            for legacy_duration in (1, 99, 3600):
                with self.subTest(screen=screen, legacy_duration=legacy_duration):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        media_root = root / "media"
                        media_root.mkdir()
                        reference = f"{screen}_complete.gif"
                        frames = [
                            Image.new("RGB", (2, 2), (red, 0, 0))
                            for red in (0, 40, 80, 120, 160)
                        ]
                        frames[0].save(
                            media_root / reference,
                            format="GIF", save_all=True,
                            append_images=frames[1:], duration=10,
                            loop=0, optimize=False,
                        )
                        for frame in frames:
                            frame.close()
                        store = ConfigStore(root / "config.json", media_dir=media_root)
                        config = store.get()
                        row = next(
                            item for item in config["playlist"]
                            if item["module"] == screen
                        )
                        row.update(
                            source=reference, sources=[reference],
                            duration=legacy_duration,
                        )
                        config["media"][screen] = reference
                        store._write(config)
                        clock = Clock()
                        matrix = HardwareBoundary(clock)
                        controller = DisplayController(matrix, store)
                        try:
                            with patch("matrix_framework.time.monotonic", clock):
                                rendered = controller._execute_scheduled_screen(
                                    config, row, maximum_duration=100,
                                    entering_sleep=screen == "sleep",
                                )
                        finally:
                            controller._openweather_provider.shutdown()

                        self.assertTrue(rendered)
                        self.assertEqual(matrix.frames, [0, 40, 80, 120, 160])

    def test_saved_sleep_gif_loops_seamlessly_in_one_execution_until_night_ends(self):
        """Owner 2026-10-13: Sleep froze between passes because each GIF pass was
        a separate scheduler turn (repair, resolve, reopen, decode). It must keep
        looping in one uninterrupted playback until wake; Splash stays one pass."""
        from matrix_framework import Matrix

        class HardwareBoundary:
            show_gif = Matrix.show_gif

            def __init__(self):
                self.frames = []
                self.pixel = None

            def clear(self):
                pass

            def draw_image(self, image, **_kwargs):
                self.pixel = image.convert("RGB").getpixel((0, 0))[0]

            def swap(self):
                self.frames.append(self.pixel)

        reds = (0, 40, 80, 120, 160)
        for screen in ("sleep", "splash"):
            with self.subTest(screen=screen), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                media_root = root / "media"
                media_root.mkdir()
                reference = f"{screen}_loop.gif"
                frames = [Image.new("RGB", (2, 2), (red, 0, 0)) for red in reds]
                frames[0].save(
                    media_root / reference, format="GIF", save_all=True,
                    append_images=frames[1:], duration=20, loop=0, optimize=False,
                )
                for frame in frames:
                    frame.close()
                store = ConfigStore(root / "config.json", media_dir=media_root)
                config = store.get()
                row = next(i for i in config["playlist"] if i["module"] == screen)
                row.update(source=reference, sources=[reference], duration=1)
                config["media"][screen] = reference
                store._write(config)
                matrix = HardwareBoundary()
                controller = DisplayController(matrix, store)
                controller._execute = Mock(wraps=controller._execute)
                try:
                    rendered = controller._execute_scheduled_screen(
                        config, row, maximum_duration=0.45,
                        entering_sleep=screen == "sleep",
                    )
                finally:
                    controller._openweather_provider.shutdown()

                self.assertTrue(rendered)
                self.assertEqual(controller._execute.call_count, 1)
                if screen == "splash":
                    self.assertEqual(matrix.frames, list(reds))
                    continue
                # One 100 ms pass repeats in the same playback, frames in order.
                self.assertGreaterEqual(len(matrix.frames), 3 * len(reds))
                self.assertEqual(
                    matrix.frames,
                    [reds[i % len(reds)] for i in range(len(matrix.frames))],
                )

    def test_real_schedule_deadline_truncates_gif_and_preserves_splash_choice(self):
        from matrix_framework import Matrix

        class HardwareBoundary:
            show_gif = Matrix.show_gif

            def __init__(self):
                self.frames = []
                self.pixel = None

            def clear(self):
                pass

            def draw_image(self, image, **_kwargs):
                self.pixel = image.convert("RGB").getpixel((0, 0))[0]

            def swap(self):
                self.frames.append(self.pixel)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            for reference, reds in (
                ("first.gif", (0, 20, 40, 60, 80)),
                ("second.gif", (100, 120, 140, 160, 180)),
            ):
                frames = [Image.new("RGB", (2, 2), (red, 0, 0)) for red in reds]
                frames[0].save(
                    media_root / reference,
                    format="GIF", save_all=True, append_images=frames[1:],
                    duration=100, loop=0, optimize=False,
                )
                for frame in frames:
                    frame.close()
            store = ConfigStore(root / "config.json", media_dir=media_root)
            config = store.get()
            row = next(item for item in config["playlist"] if item["module"] == "splash")
            row.update(
                source="first.gif", sources=["first.gif", "second.gif"],
                duration=3600,
            )
            config["media"]["splash"] = "first.gif"
            store._write(config)
            matrix = HardwareBoundary()
            controller = DisplayController(matrix, store)
            controller._screen_runtime._shuffle = lambda values: None
            try:
                interrupted = controller._execute_scheduled_screen(
                    config, row, maximum_duration=0.15,
                )
                interrupted_frames = list(matrix.frames)
                matrix.frames.clear()
                completed = controller._execute_scheduled_screen(
                    config, row, maximum_duration=10,
                )
            finally:
                controller._openweather_provider.shutdown()

            self.assertFalse(interrupted)
            self.assertEqual(interrupted_frames, [100, 120])
            self.assertTrue(completed)
            self.assertEqual(matrix.frames, [100, 120, 140, 160, 180])

    def test_screen_gif_runtime_uses_safe_fallback_for_malformed_timing_inputs(self):
        from screen_media import MediaInfo, ScreenMediaRuntime

        runtime = ScreenMediaRuntime(catalog=lambda _screen: {})
        valid = MediaInfo(
            "valid.gif", (1, 1, 1, 1, 1), "gif", 100,
            frame_delays_ms=(100,),
        )
        malformed_delays = MediaInfo(
            "broken.gif", (1, 2, 1, 1, 1), "gif", 100,
            frame_delays_ms=(100,),
        )
        object.__setattr__(malformed_delays, "frame_delays_ms", ("broken",))
        cases = (
            ({"layout": {"playback_speed_percent": 0}}, valid),
            ({"layout": {"playback_speed_percent": "fast"}}, valid),
            ({"layout": {"playback_speed_percent": float("nan")}}, valid),
            ({"layout": {"playback_speed_percent": 100}}, malformed_delays),
        )
        for row, media in cases:
            with self.subTest(row=row, media=media):
                self.assertEqual(runtime.duration({}, "splash", media, row), 7.0)

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
        self.assertEqual(first.args[2], 0.1)
        self.assertEqual(second.args[2], 0.2)

    def test_nonfirst_screen_row_repairs_only_its_missing_media_before_execution(self):
        from screen_media import ScreenMediaCatalog, ScreenMediaRuntime

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            built_in = media_root / "built-in"
            built_in.mkdir(parents=True)
            self._image(built_in / "splash_loading_squares.gif")
            self._image(built_in / "sleep.gif")
            self._image(media_root / "first.gif")
            self._image(media_root / "second.gif")
            store = ConfigStore(root / "config.json", media_dir=media_root)
            stale = store.get()
            first = next(item for item in stale["playlist"] if item["module"] == "splash")
            first.update(source="first.gif", sources=["first.gif"])
            second = copy.deepcopy(first)
            second.update(
                id="splash_second", source="missing.gif",
                sources=["missing.gif", "second.gif"], duration=19,
            )
            stale["playlist"].insert(stale["playlist"].index(first) + 1, second)
            store._write(stale)
            first_before = copy.deepcopy(first)
            controller = DisplayController(Mock(), store)
            controller._screen_runtime = ScreenMediaRuntime(
                catalog=ScreenMediaCatalog(media_root), shuffle=lambda values: None,
            )
            controller._execute = Mock(return_value=True)

            rendered = controller._execute_scheduled_screen(stale, second)

            self.assertTrue(rendered)
            saved_rows = [
                item for item in store.get()["playlist"] if item["module"] == "splash"
            ]
            self.assertEqual(saved_rows[0], first_before)
            self.assertEqual(saved_rows[1]["source"], "second.gif")
            self.assertEqual(saved_rows[1]["sources"], ["second.gif"])
            self.assertEqual(
                controller._execute.call_args.args[1]["media"]["splash"], "second.gif",
            )

    def test_scheduled_nonfirst_screen_row_keeps_identity_pool_layout_and_natural_runtime(self):
        from screen_media import MediaInfo, ScreenMediaRuntime

        for screen in ("splash", "sleep"):
            with self.subTest(screen=screen), tempfile.TemporaryDirectory() as directory:
                media_root = Path(directory)
                built_in = media_root / "built-in"
                built_in.mkdir()
                self._image(built_in / "splash_loading_squares.gif")
                self._image(built_in / "sleep.gif")
                first_reference = f"{screen}_first.gif"
                second_reference = f"{screen}_second.gif"
                self._image(media_root / first_reference)
                self._image(media_root / second_reference)
                config = copy.deepcopy(DEFAULT_CONFIG)
                first = next(
                    item for item in config["playlist"] if item["module"] == screen
                )
                first.update(source=first_reference, sources=[first_reference], duration=8)
                second = copy.deepcopy(first)
                second.update(
                    id=f"{screen}_second", source=second_reference,
                    sources=[second_reference], duration=19,
                    layout={**second["layout"], "x": 9},
                )
                config["playlist"].insert(config["playlist"].index(first) + 1, second)
                catalog = {
                    first_reference: MediaInfo(
                        first_reference, (1, 1, 1, 1, 1), "gif", 100,
                        frame_delays_ms=(100,),
                    ),
                    second_reference: MediaInfo(
                        second_reference, (1, 2, 1, 1, 1), "gif", 200,
                        frame_delays_ms=(200,),
                    ),
                }
                store = Mock(media_root=media_root)
                controller = DisplayController(Mock(), store)
                controller._screen_runtime = ScreenMediaRuntime(
                    catalog=lambda requested: catalog if requested == screen else {},
                    shuffle=lambda values: None, choice=lambda values: values[0],
                )
                controller._execute = Mock(return_value=True)

                rendered = controller._execute_scheduled_screen(
                    config, second, entering_sleep=screen == "sleep",
                )

                self.assertTrue(rendered)
                selected = controller._execute.call_args.args[1]
                self.assertEqual(selected["media"][screen], second_reference)
                self.assertEqual(selected["media_layout"][screen]["x"], 9)
                self.assertEqual(controller._execute.call_args.args[2], 0.2)
                store.compare_and_update.assert_not_called()

    def test_active_scheduler_does_not_treat_screen_row_duration_as_a_deadline(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        first = next(item for item in config["playlist"] if item["module"] == "splash")
        first.update(id="splash_short", duration=2)
        second = copy.deepcopy(first)
        second.update(id="splash_shorter", duration=3)
        config["playlist"] = [first, second]
        store = Mock(get=Mock(return_value=config))
        controller = DisplayController(Mock(), store)
        controller._take_preview = Mock(return_value=None)
        controller._take_media_preview = Mock(return_value=None)
        controller._take_announcement_preview = Mock(return_value=None)
        controller._is_active = Mock(return_value=True)
        controller._seconds_until_schedule_change = Mock(return_value=100)

        def execute_screen(*_args, **_kwargs):
            if controller._execute_scheduled_screen.call_count == 2:
                controller._shutdown.set()
            return True

        controller._execute_scheduled_screen = Mock(side_effect=execute_screen)

        controller._run_loop_unchecked()

        self.assertEqual(controller._execute_scheduled_screen.call_args_list, [
            call(config, first, maximum_duration=100),
            call(config, second, maximum_duration=100),
        ])

    def test_short_splash_rows_still_render_stills_for_the_seven_second_fallback(self):
        from screen_media import MediaInfo, ScreenMediaRuntime

        config = copy.deepcopy(DEFAULT_CONFIG)
        first = next(item for item in config["playlist"] if item["module"] == "splash")
        first.update(
            id="splash_two", source="first.png", sources=["first.png"], duration=2,
        )
        second = copy.deepcopy(first)
        second.update(
            id="splash_three", source="second.png", sources=["second.png"], duration=3,
        )
        config["playlist"] = [first, second]
        catalog = {
            "first.png": MediaInfo("first.png", (1, 1, 1, 1, 1), "still", None),
            "second.png": MediaInfo("second.png", (1, 2, 1, 1, 1), "still", None),
        }
        store = Mock(get=Mock(return_value=config), media_root=Path("/nonexistent"))
        controller = DisplayController(Mock(), store)
        controller._screen_runtime = ScreenMediaRuntime(
            catalog=lambda screen: catalog if screen == "splash" else {},
            shuffle=lambda values: values.reverse(),
        )
        controller._take_preview = Mock(return_value=None)
        controller._take_media_preview = Mock(return_value=None)
        controller._take_announcement_preview = Mock(return_value=None)
        controller._is_active = Mock(return_value=True)
        controller._seconds_until_schedule_change = Mock(return_value=100)
        durations = []

        def execute(_screen, _selected, duration):
            durations.append(duration)
            if len(durations) == 2:
                controller._shutdown.set()
            return True

        controller._execute = Mock(side_effect=execute)

        controller._run_loop_unchecked()

        self.assertEqual(durations, [7.0, 7.0])

    def test_real_schedule_deadline_caps_the_seven_second_still_hold(self):
        from screen_media import MediaInfo, ScreenMediaRuntime

        config = copy.deepcopy(DEFAULT_CONFIG)
        row = next(item for item in config["playlist"] if item["module"] == "splash")
        row.update(source="still.png", sources=["still.png"], duration=2)
        runtime = ScreenMediaRuntime(catalog=lambda _screen: {
            "still.png": MediaInfo("still.png", (1, 1, 1, 1, 1), "still", None),
        })
        controller = DisplayController(Mock(), Mock(media_root=Path("/nonexistent")))
        controller._screen_runtime = runtime
        controller._execute = Mock(return_value=True)

        rendered = controller._execute_scheduled_screen(
            config, row, maximum_duration=1.5,
        )

        self.assertTrue(rendered)
        self.assertEqual(controller._execute.call_args.args[2], 1.5)

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
                "one.gif", (1, 1, 1, 1, 1), "gif", 2000,
                frame_delays_ms=(2000,),
            ),
            "two.gif": MediaInfo(
                "two.gif", (1, 2, 1, 1, 1), "gif", 2000,
                frame_delays_ms=(2000,),
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

    def _night_controller(self, rows, catalog, *, nights, plays_per_night, render=None):
        from screen_media import ScreenMediaRuntime

        config = copy.deepcopy(DEFAULT_CONFIG)
        template = next(item for item in config["playlist"] if item["module"] == "sleep")
        index = config["playlist"].index(template)
        config["playlist"][index:index + 1] = [
            dict(copy.deepcopy(template), id=row_id, enabled=True,
                 source=sources[0], sources=list(sources))
            for row_id, sources in rows
        ]
        store = Mock(get=Mock(return_value=config))
        store.media_root = Mock(exists=Mock(return_value=False))
        controller = DisplayController(Mock(), store)
        row_picks = []

        def row_choice(values):
            row_picks.append(tuple(row["id"] for row in values))
            return values[(len(row_picks) - 1) % len(values)]

        controller._screen_runtime = ScreenMediaRuntime(
            catalog=lambda screen: catalog if screen == "sleep" else {},
            choice=lambda values: values[0], row_choice=row_choice,
        )
        for name in ("_take_preview", "_take_media_preview", "_take_announcement_preview"):
            setattr(controller, name, Mock(return_value=None))
        controller._seconds_until_schedule_change = Mock(return_value=100)
        schedule = []
        for _night in range(nights):
            schedule += [False] * plays_per_night + [True]
        state = {"position": 0, "night": 0}
        played = []

        def is_active(_config, now=None):
            del now
            if state["position"] >= len(schedule):
                controller._shutdown.set()
                return True
            return schedule[state["position"]]

        def execute(name, selected, _duration, **_kwargs):
            if name != "sleep":
                return True
            played.append((state["night"], selected["media"]["sleep"]))
            return True if render is None else render(selected["media"]["sleep"], len(played))

        def healthy():
            if schedule[min(state["position"], len(schedule) - 1)]:
                state["night"] += 1
            state["position"] += 1

        controller._is_active = is_active
        controller._execute = Mock(side_effect=execute)
        controller._mark_scheduler_healthy = healthy
        controller._run_loop_unchecked()
        return played, row_picks

    def test_multiple_enabled_sleep_rows_play_one_pick_for_the_whole_night(self):
        from screen_media import MediaInfo

        catalog = {
            reference: MediaInfo(reference, (1, index, 1, 1, 1), "gif", 1000)
            for index, reference in enumerate(("a.gif", "b.gif", "c.gif"), 1)
        }
        played, row_picks = self._night_controller(
            [("sleep_a", ["a.gif"]), ("sleep_b", ["b.gif"]), ("sleep_c", ["c.gif"])],
            catalog, nights=2, plays_per_night=4,
        )

        by_night = {}
        for night, reference in played:
            by_night.setdefault(night, []).append(reference)
        # Night 1 picks sleep_a, night 2 re-picks (sleep_b); never rotates within a night.
        self.assertEqual(by_night, {0: ["a.gif"] * 4, 1: ["b.gif"] * 4})
        self.assertEqual(row_picks, [("sleep_a", "sleep_b", "sleep_c")] * 2)

    def test_unusable_nightly_sleep_pick_falls_back_once_and_then_holds(self):
        from screen_media import MediaInfo

        catalog = {"b.gif": MediaInfo("b.gif", (1, 2, 1, 1, 1), "gif", 1000)}
        played, row_picks = self._night_controller(
            [("sleep_a", ["missing.gif"]), ("sleep_b", ["b.gif"])],
            catalog, nights=1, plays_per_night=3,
        )

        self.assertEqual(played, [(0, "b.gif")] * 3)
        self.assertEqual(row_picks, [("sleep_a", "sleep_b")])

    def test_interrupted_nightly_sleep_pick_is_kept_not_replaced(self):
        from screen_media import MediaInfo

        catalog = {
            reference: MediaInfo(reference, (1, index, 1, 1, 1), "gif", 1000)
            for index, reference in enumerate(("a.gif", "b.gif"), 1)
        }
        # The second play is interrupted (e.g. a preview); the pick must survive it.
        played, row_picks = self._night_controller(
            [("sleep_a", ["a.gif"]), ("sleep_b", ["b.gif"])],
            catalog, nights=1, plays_per_night=4,
            render=lambda _reference, count: count != 2,
        )

        self.assertEqual(played, [(0, "a.gif")] * 4)
        self.assertEqual(row_picks, [("sleep_a", "sleep_b")])

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
        controller._screen_runtime.night_sleep_rows.side_effect = lambda rows: rows
        controller._screen_runtime.sleep_row_unrendered.return_value = True

        def execute_scheduled_screen(*_args, **kwargs):
            if kwargs.get("entering_sleep"):
                controller._sleep_session_active = True
            return True

        controller._execute_scheduled_screen = Mock(side_effect=execute_scheduled_screen)

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

    def _run_first_sleep_repair_retry(self, fault):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            built_in = media_root / "built-in"
            built_in.mkdir(parents=True)
            self._image(built_in / "splash_loading_squares.gif")
            self._image(built_in / "sleep.gif")
            store = ConfigStore(root / "config.json", media_dir=media_root)
            stale = store.get()
            sleep = next(
                item for item in stale["playlist"] if item["module"] == "sleep"
            )
            sleep.update(source="missing.gif", sources=["missing.gif"])
            stale["media"]["sleep"] = "missing.gif"
            store._write(stale)

            controller = DisplayController(Mock(), store)
            controller._take_preview = Mock(return_value=None)
            controller._take_media_preview = Mock(return_value=None)
            controller._take_announcement_preview = Mock(return_value=None)
            controller._is_active = Mock(return_value=False)
            controller._seconds_until_schedule_change = Mock(return_value=100)
            controller._execute = Mock(return_value=True)
            shutdown_event = controller._shutdown
            waits = []
            controller._shutdown = Mock(
                is_set=shutdown_event.is_set,
                set=shutdown_event.set,
                wait=lambda timeout=None: waits.append(timeout) or False,
            )
            controller._mark_scheduler_healthy = lambda: controller._shutdown.set()

            original_fsync = os.fsync
            original_replace = os.replace
            injected = []

            def fsync(fd):
                is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
                if not injected and (
                    (fault == "file_fsync" and not is_directory)
                    or (fault == "directory_fsync" and is_directory)
                ):
                    injected.append(fault)
                    raise OSError(f"one-shot {fault}")
                return original_fsync(fd)

            def replace(source, destination):
                if fault == "before_replace" and not injected:
                    injected.append(fault)
                    raise OSError("one-shot before_replace")
                return original_replace(source, destination)

            with (
                patch("config_store.os.fsync", side_effect=fsync),
                patch("config_store.os.replace", side_effect=replace),
            ):
                controller._run_loop()

            saved = store.get()
            saved_sleep = next(
                item for item in saved["playlist"] if item["module"] == "sleep"
            )
            self.assertEqual(injected, [fault])
            self.assertEqual(waits, [0.25])
            self.assertEqual(controller._execute.call_count, 1)
            self.assertTrue(controller._sleep_session_active)
            self.assertTrue(controller._screen_runtime._sleep_active)
            self.assertEqual(saved["revision"], stale["revision"] + 1)
            self.assertEqual(saved_sleep["sources"], ["built-in/sleep.gif"])

    def test_first_sleep_file_fsync_failure_retries_runtime_admission(self):
        self._run_first_sleep_repair_retry("file_fsync")

    def test_first_sleep_pre_replace_failure_retries_runtime_admission(self):
        self._run_first_sleep_repair_retry("before_replace")

    def test_first_sleep_directory_fsync_failure_retries_runtime_admission(self):
        self._run_first_sleep_repair_retry("directory_fsync")

    def test_second_sleep_repair_failure_preserves_first_rows_session_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            built_in = media_root / "built-in"
            built_in.mkdir(parents=True)
            self._image(built_in / "splash_loading_squares.gif")
            self._image(built_in / "sleep.gif")
            self._image(media_root / "first.png", "PNG")
            store = ConfigStore(root / "config.json", media_dir=media_root)
            stale = store.get()
            first = next(
                item for item in stale["playlist"] if item["module"] == "sleep"
            )
            first.update(id="sleep_first", source="first.png", sources=["first.png"])
            second = copy.deepcopy(first)
            second.update(id="sleep_second", source="missing.png", sources=["missing.png"])
            stale["playlist"].insert(stale["playlist"].index(first) + 1, second)
            stale["media"]["sleep"] = "first.png"
            store._write(stale)

            controller = DisplayController(Mock(), store)
            controller._take_preview = Mock(return_value=None)
            controller._take_media_preview = Mock(return_value=None)
            controller._take_announcement_preview = Mock(return_value=None)
            controller._is_active = Mock(return_value=False)
            controller._seconds_until_schedule_change = Mock(return_value=100)
            choices = []

            def choose(values):
                choices.append(tuple(item.reference for item in values))
                return values[0]

            controller._screen_runtime._choice = choose
            controller._screen_runtime._row_choice = lambda rows: rows[-1]
            executions = []

            def execute(_screen, selected, _duration):
                executions.append(selected["media"]["sleep"])
                return True

            controller._execute = Mock(side_effect=execute)
            shutdown_event = controller._shutdown
            waits = []
            controller._shutdown = Mock(
                is_set=shutdown_event.is_set,
                set=shutdown_event.set,
                wait=lambda timeout=None: waits.append(timeout) or False,
            )
            healthy = []

            def mark_healthy():
                healthy.append(True)
                if len(healthy) == 3:
                    controller._shutdown.set()

            controller._mark_scheduler_healthy = mark_healthy
            original_fsync = os.fsync
            injected = []

            def fsync(fd):
                if not injected and not stat.S_ISDIR(os.fstat(fd).st_mode):
                    injected.append("second-row-file-fsync")
                    raise OSError("one-shot second-row file fsync")
                return original_fsync(fd)

            with patch("config_store.os.fsync", side_effect=fsync):
                controller._run_loop()

            # Tonight's pick is the second row. Its first repair write fails and
            # the cycle backs off; the retry repairs it to the default and that one
            # row then holds for the rest of the night (the first row never plays).
            self.assertEqual(injected, ["second-row-file-fsync"])
            self.assertEqual(waits, [0.25])
            self.assertEqual(executions, ["built-in/sleep.gif"] * 3)
            self.assertEqual(choices, [("built-in/sleep.gif",)])
            self.assertTrue(controller._sleep_session_active)
            self.assertTrue(controller._screen_runtime._sleep_active)

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
