import io
import json
import re
import os
import pwd
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, UnidentifiedImageError

from config_store import ConfigStore
from pairing import PairedDeviceStore, PairingDurabilityError
from secret_store import SecretDurabilityError, SecretStore
from tests import test_config_store
from webapp import create_app


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        Path(self.temp.name).chmod(0o755)
        self.store = ConfigStore(Path(self.temp.name) / "config.json")
        self.device_store = PairedDeviceStore(Path(self.temp.name) / "paired-devices.json")
        self.openweather_secrets = SecretStore(Path(self.temp.name) / "secrets" / "openweather-api-key")
        self.media_dir = Path(self.temp.name) / "media"
        self.media_dir.mkdir()
        self.media_dir.chmod(0o755)
        self.controller = Mock()
        self.pairing = Mock()
        self.location_search = Mock(return_value=[{
            "label": "Brighton, England, GB",
            "latitude": 50.8225,
            "longitude": -0.1372,
        }])
        self.app = create_app(
            self.controller,
            self.store,
            self.pairing,
            secret_key="test",
            session_generation="generation-one",
            media_dir=self.media_dir,
            location_search=self.location_search,
            device_store=self.device_store,
            openweather_secrets=self.openweather_secrets,
        )
        self.client = self.app.test_client()

    def pair_browser(self):
        self.pairing.verify.return_value = True
        self.client.post("/pair", data={"pin": "123456", "device_name": "Test browser"})

    def test_control_stop_reports_timeout_without_claiming_success(self):
        self.pair_browser()
        self.controller.stop.return_value = False
        self.controller.status.return_value = {"running": True}

        response = self.client.post("/api/control/stop")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {
            "error": "Display did not stop within the timeout.",
            "running": True,
        })

    def test_config_update_reports_committed_state_when_display_reload_fails(self):
        self.pair_browser()
        self.controller.reload.side_effect = RuntimeError("display unavailable")

        response = self.client.post("/api/config", json={"device": {"name": "Saved Matrix"}})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.content_type, "application/json")
        self.assertTrue(response.get_json()["committed"])
        self.assertEqual(response.get_json()["config"]["device"]["name"], "Saved Matrix")
        self.assertEqual(self.store.get()["device"]["name"], "Saved Matrix")
        self.assertNotIn("display unavailable", response.get_data(as_text=True))

    def test_config_update_reports_committed_state_when_directory_sync_fails(self):
        self.pair_browser()

        with patch(
            "config_store.os.fsync",
            side_effect=[None, OSError("directory unavailable")],
        ):
            response = self.client.post(
                "/api/config",
                json={"device": {"name": "Committed Despite Error"}},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.content_type, "application/json")
        self.assertEqual(response.get_json()["committed"], True)
        self.assertEqual(response.get_json()["durability_uncertain"], True)
        self.assertEqual(
            response.get_json()["config"]["device"]["name"],
            "Committed Despite Error",
        )
        self.assertEqual(self.store.get()["device"]["name"], "Committed Despite Error")
        self.controller.reload.assert_called_once_with()
        self.assertNotIn("directory unavailable", response.get_data(as_text=True))

    def test_lowering_pairing_limit_serializes_with_new_pairing(self):
        self.pair_browser()
        config_entered = threading.Event()
        release_config = threading.Event()
        original_update = self.store.update
        results = {}

        def blocked_update(changes):
            config_entered.set()
            self.assertTrue(release_config.wait(2))
            return original_update(changes)

        def lower_limit():
            client = self.app.test_client()
            with client.session_transaction() as session_data:
                session_data["paired"] = True
                session_data["generation"] = "generation-one"
                session_data["device_id"] = self.device_store.list()[0]["id"]
            results["config"] = client.post(
                "/api/config", json={"pairing": {"max_devices": 1}},
            )

        def add_device():
            client = self.app.test_client()
            results["pair"] = client.post(
                "/pair", data={"pin": "123456", "device_name": "Racing browser"},
            )

        with patch.object(self.store, "update", side_effect=blocked_update):
            config_thread = threading.Thread(target=lower_limit)
            pair_thread = threading.Thread(target=add_device)
            config_thread.start()
            self.assertTrue(config_entered.wait(2))
            pair_thread.start()
            release_config.set()
            config_thread.join(2)
            pair_thread.join(2)

        self.assertFalse(config_thread.is_alive())
        self.assertFalse(pair_thread.is_alive())
        self.assertEqual(results["config"].status_code, 200)
        self.assertEqual(results["pair"].status_code, 409)
        self.assertEqual(self.store.get()["pairing"]["max_devices"], 1)
        self.assertEqual(len(self.device_store.list()), 1)

    def test_api_errors_are_json_and_config_requires_json_content_type(self):
        self.pair_browser()
        malformed = self.client.post(
            "/api/config", data="{", content_type="application/json",
        )
        wrong_type = self.client.post(
            "/api/config",
            data=json.dumps({"device": {"name": "Wrong type"}}),
            content_type="text/plain",
        )
        missing = self.client.post("/api/media/missing.gif/preview")

        self.assertEqual((malformed.status_code, malformed.is_json), (400, True))
        self.assertEqual((wrong_type.status_code, wrong_type.is_json), (415, True))
        self.assertEqual((missing.status_code, missing.is_json), (404, True))

    def test_config_rejects_non_object_media_as_client_validation_error(self):
        self.pair_browser()
        for media in ([], "bad", 3, None):
            with self.subTest(media=media):
                response = self.client.post(
                    "/api/config",
                    data=json.dumps({"media": media}),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.is_json)

    def test_device_registry_write_failure_returns_sanitized_json(self):
        self.pair_browser()
        device_id = self.device_store.list()[0]["id"]

        with patch.object(self.device_store, "_write", side_effect=OSError("disk details")):
            response = self.client.delete(f"/api/devices/{device_id}")

        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.is_json)
        self.assertEqual(response.get_json(), {"error": "Internal server error."})
        self.assertNotIn("disk details", response.get_data(as_text=True))
        self.assertTrue(self.device_store.contains(device_id))

    def test_device_removal_reports_committed_durability_uncertainty(self):
        self.pair_browser()
        device_id = self.device_store.list()[0]["id"]

        with patch.object(
            self.device_store,
            "remove",
            side_effect=PairingDurabilityError([]),
        ):
            response = self.client.delete(f"/api/devices/{device_id}")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {
            "removed": device_id,
            "current": True,
            "committed": True,
            "durability_uncertain": True,
            "error": "The device was removed, but durable storage could not be confirmed.",
        })
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_removed_current_device_sees_forwarded_durability_warning(self):
        self.pair_browser()
        device_id = self.device_store.list()[0]["id"]
        with patch.object(
            self.device_store, "remove", side_effect=PairingDurabilityError([])
        ):
            response = self.client.delete(f"/api/devices/{device_id}")

        self.assertEqual(response.status_code, 503)
        warning = response.get_json()["error"]
        pairing_page = self.client.get(
            "/pair", query_string={"warning": "device-removal-durability"}
        )
        self.assertIn(warning, pairing_page.get_data(as_text=True))

    def test_removed_current_device_warning_survives_when_another_device_remains(self):
        self.pair_browser()
        current_id = self.device_store.list()[0]["id"]
        second = self.device_store.add("Second browser", max_devices=4)
        self.pairing.active_pin.return_value = None
        with patch.object(
            self.device_store, "_write", side_effect=PairingDurabilityError([second])
        ):
            response = self.client.delete(f"/api/devices/{current_id}")

        self.assertEqual(response.status_code, 503)
        warning = response.get_json()["error"]
        pairing_page = self.client.get(
            "/pair", query_string={"warning": "device-removal-durability"}
        )
        self.assertEqual(pairing_page.status_code, 403)
        self.assertIn(warning, pairing_page.get_data(as_text=True))

    def test_pairing_page_does_not_render_arbitrary_warning_query_text(self):
        response = self.client.get(
            "/pair", query_string={"warning": "Forged trusted warning"}
        )
        self.assertNotIn("Forged trusted warning", response.get_data(as_text=True))

    def test_logout_clears_session_after_committed_removal_uncertainty(self):
        self.pair_browser()
        with patch.object(
            self.device_store, "remove", side_effect=PairingDurabilityError([])
        ):
            response = self.client.post("/logout")

        self.assertEqual(response.status_code, 302)
        self.assertIn("warning=", response.headers["Location"])
        with self.client.session_transaction() as browser_session:
            self.assertNotIn("device_id", browser_session)
            self.assertNotIn("paired", browser_session)
        warning_page = self.client.get(response.headers["Location"])
        self.assertIn("removed", warning_page.get_data(as_text=True))

    def test_logout_precommit_failure_preserves_recovery_session(self):
        self.pair_browser()
        device_id = self.device_store.list()[0]["id"]
        with patch.object(self.device_store, "remove", side_effect=OSError("disk failed")):
            response = self.client.post("/logout")

        self.assertEqual(response.status_code, 503)
        self.assertIn("could not be removed", response.get_data(as_text=True))
        with self.client.session_transaction() as browser_session:
            self.assertEqual(browser_session.get("device_id"), device_id)
            self.assertTrue(browser_session.get("paired"))
        self.assertTrue(self.device_store.contains(device_id))
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_openweather_secret_api_is_paired_and_never_echoes_the_key(self):
        key = "test_key_1234567890"
        self.assertEqual(self.client.get("/api/secrets/openweather").status_code, 401)
        self.assertEqual(
            self.client.put("/api/secrets/openweather", json={"api_key": key}).status_code,
            401,
        )
        self.pair_browser()

        self.assertEqual(
            self.client.get("/api/secrets/openweather").get_json(),
            {"configured": False},
        )
        saved = self.client.put("/api/secrets/openweather", json={"api_key": key})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.get_json(), {"configured": True})
        self.assertNotIn(key, saved.get_data(as_text=True))
        self.assertEqual(self.openweather_secrets.get(), key)

        status = self.client.get("/api/secrets/openweather")
        self.assertEqual(status.get_json(), {"configured": True})
        self.assertNotIn(key, status.get_data(as_text=True))

        removed = self.client.delete("/api/secrets/openweather")
        self.assertEqual(removed.get_json(), {"configured": False})
        self.assertIsNone(self.openweather_secrets.get())

    def test_default_openweather_secret_store_uses_config_data_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            media = root / "resources" / "media"
            media.mkdir(parents=True)
            store = ConfigStore(data / "config.json")
            devices = PairedDeviceStore(data / "paired-devices.json")
            pairing = Mock()
            pairing.verify.return_value = True
            app = create_app(
                Mock(), store, pairing,
                secret_key="test", session_generation="generation-one",
                media_dir=media, device_store=devices,
            )
            client = app.test_client()
            client.post("/pair", data={"pin": "123456", "device_name": "Test browser"})

            response = client.put(
                "/api/secrets/openweather", json={"api_key": "test_key_1234567890"}
            )

            self.assertEqual(response.status_code, 200)
            self.assertTrue((data / "secrets" / "openweather-api-key").is_file())
            self.assertFalse((root / "resources" / "openweather-api-key").exists())

    def test_openweather_secret_api_reports_post_commit_state_on_fsync_failure(self):
        self.pair_browser()
        with patch.object(
            self.openweather_secrets, "set",
            side_effect=SecretDurabilityError(configured=True),
        ):
            saved = self.client.put(
                "/api/secrets/openweather", json={"api_key": "test_key_1234567890"}
            )
        self.assertEqual(saved.status_code, 503)
        self.assertEqual(saved.get_json()["configured"], True)
        self.assertNotIn("test_key_1234567890", saved.get_data(as_text=True))

        with patch.object(
            self.openweather_secrets, "clear",
            side_effect=SecretDurabilityError(configured=False),
        ):
            cleared = self.client.delete("/api/secrets/openweather")
        self.assertEqual(cleared.status_code, 503)
        self.assertEqual(cleared.get_json()["configured"], False)

        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("error.data=data", body)
        self.assertIn("error.data?.configured", body)

    def test_openweather_secret_storage_failures_return_sanitized_json_503(self):
        self.pair_browser()
        cases = (
            ("status", "get", {}, "/api/secrets/openweather"),
            ("set", "put", {"json": {"api_key": "test_key_1234567890"}}, "/api/secrets/openweather"),
            ("clear", "delete", {}, "/api/secrets/openweather"),
        )
        for attribute, method, kwargs, path in cases:
            with self.subTest(attribute=attribute), patch.object(
                self.openweather_secrets,
                attribute,
                side_effect=RuntimeError("unsafe /private/secret path"),
            ):
                response = getattr(self.client, method)(path, **kwargs)

            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.content_type, "application/json")
            self.assertEqual(response.get_json(), {"error": "Secret storage is unavailable."})
            self.assertNotIn("/private/secret", response.get_data(as_text=True))
            self.assertNotIn("test_key_1234567890", response.get_data(as_text=True))

    def test_dashboard_remains_available_when_secret_storage_is_unavailable(self):
        self.pair_browser()
        with patch.object(
            self.openweather_secrets,
            "configured",
            side_effect=OSError("unavailable /private/secret path"),
        ):
            response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Secret storage unavailable", body)
        self.assertIn("initialOpenWeatherSecretUnavailable=true", body)
        self.assertNotIn("/private/secret", body)

    def test_malformed_stored_secret_returns_json_503_and_keeps_dashboard_available(self):
        path = self.openweather_secrets.path
        path.parent.mkdir(mode=0o700)
        path.write_bytes(b"\xff\xfe")
        path.chmod(0o600)
        self.pair_browser()

        status = self.client.get("/api/secrets/openweather")
        dashboard = self.client.get("/")

        self.assertEqual(status.status_code, 503)
        self.assertEqual(status.content_type, "application/json")
        self.assertEqual(status.get_json(), {"error": "Secret storage is unavailable."})
        self.assertEqual(dashboard.status_code, 200)
        body = dashboard.get_data(as_text=True)
        self.assertIn("initialOpenWeatherSecretUnavailable=true", body)
        self.assertNotIn("Stored API key", status.get_data(as_text=True))
        self.assertNotIn("Stored API key", body)

    def test_dashboard_masks_openweather_secret_and_keeps_it_out_of_config(self):
        key = "test_key_1234567890"
        self.openweather_secrets.set(key)
        self.pair_browser()

        body = self.client.get("/").get_data(as_text=True)

        self.assertIn('id="openweather-api-key"', body)
        self.assertIn('type="password"', body)
        self.assertIn('autocomplete="new-password"', body)
        self.assertIn('id="openweather-secret-status"', body)
        self.assertIn("/api/secrets/openweather", body)
        self.assertNotIn(key, body)
        self.assertNotIn("api_key", str(self.store.get()))

    def test_dashboard_offers_two_disabled_openweather_apps_with_independent_settings_editors(self):
        self.pair_browser()

        body = self.client.get("/").get_data(as_text=True)

        self.assertIn('data-module="weather"', body)
        self.assertIn('data-module="openweather_overview"', body)
        self.assertIn('data-module="openweather_forecast"', body)
        self.assertIn("Weather (Free · Open-Meteo)", body)
        self.assertIn("OpenWeather Overview", body)
        self.assertIn("OpenWeather Forecast", body)
        self.assertIn('data-module-editor="openweather_overview"', body)
        self.assertIn('data-module-editor="openweather_forecast"', body)
        self.assertIn("openweather_overview:withSharedWeatherLocation(builtInSettings.openweather_overview)", body)
        self.assertIn("openweather_forecast:withSharedWeatherLocation(builtInSettings.openweather_forecast)", body)
        self.assertIn("forecast_items", body)
        self.assertNotIn("field:'card_seconds'", body)
        self.assertEqual(body.count("field:'scroll_loops'"), 3)
        self.assertEqual(body.count("label:'Ticker loops'"), 3)
        self.assertEqual(body.count("label:'Tick speed'"), 3)
        self.assertNotIn("makeWeatherLocationGroup", body)
        config = self.store.get()
        self.assertTrue(next(item for item in config["playlist"] if item["module"] == "weather")["enabled"])
        self.assertFalse(next(item for item in config["playlist"] if item["module"] == "openweather_overview")["enabled"])
        self.assertFalse(next(item for item in config["playlist"] if item["module"] == "openweather_forecast")["enabled"])

    def test_successful_pairing_registers_a_named_device_session(self):
        self.pair_browser()

        devices = self.device_store.list()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["name"], "Test browser")
        with self.client.session_transaction() as browser_session:
            self.assertEqual(browser_session["device_id"], devices[0]["id"])

    def test_first_pairing_durability_uncertainty_keeps_browser_authorized(self):
        self.pairing.verify.return_value = True
        with patch("pairing.os.fsync", side_effect=[None, OSError("directory unavailable")]):
            response = self.client.post(
                "/pair", data={"pin": "123456", "device_name": "First browser"}
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("already paired", response.get_data(as_text=True))
        devices = self.device_store.list()
        self.assertEqual(len(devices), 1)
        with self.client.session_transaction() as browser_session:
            self.assertEqual(browser_session["device_id"], devices[0]["id"])
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_paired_device_session_remains_durable_until_revoked(self):
        self.assertEqual(self.app.permanent_session_lifetime, timedelta(days=365))

    def test_removed_device_session_is_immediately_rejected(self):
        self.pair_browser()
        device_id = self.device_store.list()[0]["id"]
        self.device_store.remove(device_id)

        response = self.client.get("/api/status")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "Pairing session expired.")

    def test_unpaired_client_cannot_interrupt_an_already_paired_display(self):
        self.device_store.add("Existing browser", max_devices=4)
        self.pairing.active_pin.return_value = None

        response = self.client.get("/pair")

        self.assertEqual(response.status_code, 403)
        self.assertIn("paired device", response.get_data(as_text=True).lower())
        self.pairing.issue_pin.assert_not_called()
        self.controller.show_pairing_pin.assert_not_called()

    def test_paired_login_screen_offers_an_additional_device_button(self):
        self.pair_browser()

        response = self.client.get("/pair")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('action="/pair/new"', body)
        self.assertIn("PAIR ANOTHER DEVICE", body)

    def test_paired_device_can_start_additional_pairing_without_exposing_pin(self):
        self.pair_browser()
        self.pairing.issue_pin.return_value = "[REDACTED]"
        self.pairing.remaining_seconds.return_value = 600

        response = self.client.post("/api/devices/pair")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json(), {"pairing": "ready"})
        self.controller.show_pairing_pin.assert_called_with("[REDACTED]", ttl_seconds=600)
        self.assertNotIn("[REDACTED]", response.get_data(as_text=True))

    def test_additional_pairing_is_blocked_at_configured_device_limit(self):
        self.store.update({"pairing": {"max_devices": 1}})
        self.pair_browser()
        self.pairing.issue_pin.reset_mock()

        response = self.client.post("/api/devices/pair")

        self.assertEqual(response.status_code, 409)
        self.assertIn("limit", response.get_json()["error"])
        self.pairing.issue_pin.assert_not_called()

    def test_pairing_capacity_race_returns_a_clear_error_instead_of_server_failure(self):
        self.pairing.verify.return_value = True
        self.device_store.add = Mock(side_effect=ValueError("maximum paired devices reached"))

        response = self.client.post(
            "/pair", data={"pin": "123456", "device_name": "Racing phone"}
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("reached its paired-device limit", response.get_data(as_text=True))

    def test_paired_device_management_api_lists_and_revokes_devices(self):
        self.pair_browser()
        current = self.device_store.list()[0]
        other = self.device_store.add("Hall display", max_devices=4)

        listing = self.client.get("/api/devices")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.get_json()["max_devices"], 4)
        listed = {item["id"]: item for item in listing.get_json()["devices"]}
        self.assertTrue(listed[current["id"]]["current"])
        self.assertFalse(listed[other["id"]]["current"])

        removed = self.client.delete(f"/api/devices/{other['id']}")
        self.assertEqual(removed.status_code, 200)
        self.assertFalse(self.device_store.contains(other["id"]))

    def test_unpair_removes_the_current_device_from_registry(self):
        self.pair_browser()
        device_id = self.device_store.list()[0]["id"]

        response = self.client.post("/logout")

        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.device_store.contains(device_id))

    def test_dashboard_has_paired_device_settings_and_validates_capacity_against_count(self):
        self.pair_browser()
        self.device_store.add("Second browser", max_devices=4)

        body = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="paired-devices"', body)
        self.assertIn('id="max-paired-devices"', body)
        self.assertIn('min="1" max="10"', body)
        self.assertIn("Pair another device", body)
        self.assertIn("Second browser", body)

        response = self.client.post("/api/config", json={"pairing": {"max_devices": 1}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("below the 2 currently paired devices", response.get_json()["error"])

    @staticmethod
    def gif_bytes():
        payload = io.BytesIO()
        Image.new("RGB", (64, 32), "purple").save(payload, format="GIF")
        payload.seek(0)
        return payload

    @staticmethod
    def animated_gif_bytes():
        payload = io.BytesIO()
        frames = [Image.new("RGB", (64, 32), colour) for colour in ("red", "green", "blue")]
        frames[0].save(
            payload, format="GIF", save_all=True, append_images=frames[1:],
            duration=[100, 100, 100], loop=0,
        )
        payload.seek(0)
        return payload

    @staticmethod
    def mp4_bytes():
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
                    "-i", "testsrc2=size=96x54:rate=12:duration=0.5",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(path),
                ],
                check=True,
            )
            return io.BytesIO(path.read_bytes())

    def test_dashboard_has_matrix_preview_and_structured_media_workspace(self):
        self.pair_browser()
        response = self.client.get("/")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="matrix-preview"', body)
        self.assertIn('id="media-library"', body)
        self.assertIn('id="media-upload"', body)
        self.assertIn('class="app-shell"', body)
        self.assertIn('id="weather-results"', body)
        self.assertIn('id="weather-latitude"', body)
        self.assertIn("/api/display-frame", body)
        self.assertNotIn("function drawClock", body)
        self.assertIn("Live physical output", body)
        self.assertIn('id="lava-palettes"', body)
        self.assertIn('id="custom-module-builder"', body)
        self.assertIn("Add custom module", body)
        self.assertIn("video/mp4", body)
        self.assertIn("previewMedia(item.name)", body)
        self.assertIn("/preview`,{method:'POST'}", body)
        self.assertNotIn("window.open(", body)

    def test_dashboard_uses_one_top_level_location_for_every_weather_app(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertEqual(body.count('id="weather-location"'), 1)
        self.assertIn("Used by all weather apps", body)
        self.assertNotIn("makeWeatherLocationGroup", body)
        self.assertNotIn("openweather_overview-location", body)
        self.assertNotIn("openweather_forecast-location", body)
        self.assertIn('.location-option[aria-selected="true"]', body)
        self.assertIn("function withSharedWeatherLocation(settings)", body)
        self.assertIn(
            "openweather_overview:withSharedWeatherLocation(builtInSettings.openweather_overview)",
            body,
        )
        self.assertIn(
            "openweather_forecast:withSharedWeatherLocation(builtInSettings.openweather_forecast)",
            body,
        )

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_weather_location_dropdown_supports_pointer_and_keyboard_selection(self):
        source = (Path(__file__).parents[1] / "app" / "templates" / "dashboard.html").read_text()
        start = source.index("function wireWeatherLocationSearch")
        end = source.index("\nconst locationInput", start)
        function_source = source[start:end]
        harness = r"""
class Element {
  constructor(id='') { this.id=id; this.value=''; this.hidden=true; this._children=[]; this.attributes={}; this.listeners={}; this.textContent=''; }
  get children() { const collection={length:this._children.length,[Symbol.iterator]:()=>this._children[Symbol.iterator]()}; this._children.forEach((item,index)=>collection[index]=item); return collection; }
  setAttribute(name,value) { this.attributes[name]=String(value); }
  getAttribute(name) { return this.attributes[name]; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name,handler) { (this.listeners[name]??=[]).push(handler); }
  emit(name,key) { const event={key,target:this,prevented:false,preventDefault(){this.prevented=true}}; for(const handler of this.listeners[name]??[]) handler(event); return event; }
  append(...items) { this._children.push(...items); }
  replaceChildren(...items) { this._children=[...items]; }
  contains(target) { return target===this || target===input || target===results || this._children.includes(target); }
}
const search=new Element('search'), input=new Element('weather-location'), results=new Element('weather-results'), hint=new Element('hint');
input.closest=()=>search;
const places=[
  {label:'London, England, GB',latitude:51.5074,longitude:-0.1278},
  {label:'London, Ontario, CA',latitude:42.9834,longitude:-81.233}
];
let chosen=null, edited=null, deferred=false, pendingResolve=null;
global.document={listeners:{},createElement:()=>new Element(),addEventListener(name,handler){(this.listeners[name]??=[]).push(handler)},emit(name,event){for(const handler of this.listeners[name]??[])handler(event)}};
global.clearTimeout=()=>{};
global.setTimeout=handler=>{queueMicrotask(handler);return 1};
global.api=async()=>deferred?await new Promise(resolve=>{pendingResolve=resolve}):places;
global.setMessage=message=>{throw new Error(message)};
""" + function_source + r"""
const controller=wireWeatherLocationSearch(input,results,hint,place=>{chosen=place},value=>{edited=value});
async function settle(){await new Promise(resolve=>setImmediate(resolve));await new Promise(resolve=>setImmediate(resolve));}
(async()=>{
  input.value='Lon'; input.emit('input'); await settle();
  if(results.hidden || results.children.length!==2 || input.getAttribute('aria-expanded')!=='true') throw new Error('results did not open');
  results.children[0].onclick();
  if(chosen?.label!=='London, England, GB' || !results.hidden) throw new Error('pointer selection failed');
  input.value='London'; input.emit('input'); await settle();
  const down1=input.emit('keydown','ArrowDown'), down2=input.emit('keydown','ArrowDown');
  const enter=input.emit('keydown','Enter');
  if(!down1.prevented || !down2.prevented || !enter.prevented) throw new Error('navigation keys were not handled');
  if(chosen?.label!=='London, Ontario, CA') throw new Error('keyboard selection failed');
  if(!results.hidden || input.getAttribute('aria-expanded')!=='false') throw new Error('selection did not close results');
  if(edited!=='London') throw new Error('editing callback did not receive the query');
  deferred=true; input.value='Old location'; input.emit('input'); await settle();
  if(typeof pendingResolve!=='function') throw new Error('deferred request did not start');
  input.emit('keydown','Escape'); pendingResolve(places); await settle();
  if(!results.hidden || results.children.length!==0) throw new Error('Escape allowed stale results to reopen');
  pendingResolve=null; input.value='Another old location'; input.emit('input'); await settle();
  if(typeof pendingResolve!=='function') throw new Error('second deferred request did not start');
  document.emit('click',{target:new Element('outside')}); pendingResolve(places); await settle();
  if(!results.hidden || results.children.length!==0) throw new Error('outside click allowed stale results to reopen');
  controller.cancel();
  console.log('LOCATION_DROPDOWN_BEHAVIOR_PASS');
})().catch(error=>{console.error(error);process.exit(1)});
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("LOCATION_DROPDOWN_BEHAVIOR_PASS", result.stdout)

    def test_dashboard_dropdowns_have_contrasting_background_and_text(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn(
            "select{background-color:var(--surface);color:var(--text);color-scheme:dark}",
            body,
        )
        self.assertIn(
            "select option,select optgroup{background-color:var(--surface);color:var(--text)}",
            body,
        )

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_advanced_upload_adopts_committed_item_without_losing_warning(self):
        source = (Path(__file__).parents[1] / "app" / "static" / "custom_canvas.js").read_text()
        start = source.index("  async function uploadAdvancedMedia")
        end = source.index("\n  window.uploadAdvancedMedia", start)
        function_source = source[start:end]
        harness = r"""
global.FormData=class { append(){} };
let refreshed=0, layers=[], messages=[];
async function api(){const error=new Error('Saved, but durable storage could not be confirmed.');error.data={committed:true,item:{name:'saved.gif'}};throw error;}
async function refreshMedia(){refreshed++;}
function addAdvancedLayer(id,type,name){layers.push({id,type,name});}
function setMessage(text,error=false){messages.push({text,error});}
""" + function_source + r"""
(async()=>{
  const input={files:[{name:'saved.gif',size:100}],value:'selected'};
  await uploadAdvancedMedia('custom_test',input);
  if(refreshed!==1) throw new Error('committed upload did not refresh media');
  if(JSON.stringify(layers)!==JSON.stringify([{id:'custom_test',type:'media',name:'saved.gif'}])) throw new Error('committed upload did not add its layer');
  const final=messages.at(-1);
  if(!final.error || !final.text.includes('durable storage')) throw new Error('commit warning was lost');
  if(input.value!=='') throw new Error('file input was not reset');
  console.log('ADVANCED_COMMITTED_UPLOAD_PASS');
})().catch(error=>{console.error(error);process.exit(1)});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ADVANCED_COMMITTED_UPLOAD_PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_advanced_successful_upload_reports_later_refresh_failure_as_committed(self):
        source = (Path(__file__).parents[1] / "app" / "static" / "custom_canvas.js").read_text()
        start = source.index("  async function uploadAdvancedMedia")
        end = source.index("\n  window.uploadAdvancedMedia", start)
        function_source = source[start:end]
        harness = r"""
global.FormData=class {append(){}};
let layerAttempts=0,messages=[];
async function api(){return {name:'saved.gif'};}
async function refreshMedia(){throw new Error('Library refresh failed.');}
globalThis.reconcileMediaItem=()=>{throw new Error('Local reconciliation failed.');};
function addAdvancedLayer(){layerAttempts++;throw new Error('Editor rendering failed.');}
function setMessage(text,error=false){messages.push({text,error});}
""" + function_source + r"""
(async()=>{
  const input={files:[{name:'saved.gif',size:100}],value:'selected'};
  await uploadAdvancedMedia('custom_test',input);
  const final=messages.at(-1);
  if(!final.error || !final.text.includes('saved') || !final.text.includes('Library refresh failed') || !final.text.includes('Local reconciliation failed') || !final.text.includes('Editor rendering failed'))throw new Error('known upload commit was lost');
  if(layerAttempts!==1)throw new Error('saved media adoption was not attempted exactly once');
  if(input.value!=='')throw new Error('file input was not reset');
  console.log('ADVANCED_SUCCESS_REFRESH_FAILURE_PASS');
})().catch(error=>{console.error(error);process.exit(1)});
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ADVANCED_SUCCESS_REFRESH_FAILURE_PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_advanced_successful_upload_reports_silent_layer_adoption_failure(self):
        source = (Path(__file__).parents[1] / "app" / "static" / "custom_canvas.js").read_text()
        start = source.index("  async function uploadAdvancedMedia")
        end = source.index("\n  window.uploadAdvancedMedia", start)
        function_source = source[start:end]
        harness = r"""
global.FormData=class {append(){}};
let messages=[],layerAttempts=0;
async function api(){return {name:'saved.gif'};}
async function refreshMedia(){}
function addAdvancedLayer(){layerAttempts++;return false;}
function setMessage(text,error=false){messages.push({text,error});}
""" + function_source + r"""
(async()=>{
  const input={files:[{name:'saved.gif',size:100}],value:'selected'};
  let rejection=null;
  try{await uploadAdvancedMedia('missing_module',input);}catch(error){rejection=error.message;}
  const final=messages.at(-1);
  if(rejection!==null)throw new Error('handler rejected: '+rejection);
  if(layerAttempts!==1)throw new Error('layer adoption was not attempted exactly once');
  if(!final?.error || !final.text.includes('saved.gif') || !final.text.includes('saved') || !final.text.includes('editor'))throw new Error('silent adoption failure hid committed upload');
  if(input.value!=='')throw new Error('file input was not reset');
  console.log('ADVANCED_SILENT_ADOPTION_FAILURE_PASS');
})().catch(error=>{console.error(error);process.exit(1)});
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ADVANCED_SILENT_ADOPTION_FAILURE_PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_standard_upload_preserves_committed_warning_when_refresh_fails(self):
        self.pair_browser()
        page = self.client.get("/").get_data(as_text=True)
        start = page.index("async function uploadMedia()")
        end = page.index("\n\nasync function removeMedia", start)
        function_source = page[start:end]
        harness = r"""
let messages=[];
const input={files:[{name:'saved.gif',size:10}],value:'selected'};
global.FormData=class {append(){}};
global.document={querySelector(){return input;}};
function setMessage(text,error=false){messages.push({text,error});}
async function api(){const error=new Error('Saved, but durability is uncertain.');error.data={committed:true,item:{name:'saved.gif'}};throw error;}
async function refreshMedia(){throw new Error('Library refresh failed.');}
""" + function_source + r"""
(async()=>{
  let rejection=null;
  try{await uploadMedia();}catch(error){rejection=error.message;}
  const final=messages.at(-1);
  if(rejection!==null)throw new Error('handler rejected: '+rejection);
  if(input.value!=='')throw new Error('file input was not reset');
  if(!final.error || !final.text.includes('Saved, but durability is uncertain.') || !final.text.includes('Library refresh failed.'))throw new Error('combined warning was not shown');
  console.log('STANDARD_COMMITTED_UPLOAD_REFRESH_FAILURE_PASS');
})().catch(error=>{console.error(error);process.exit(1)});
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("STANDARD_COMMITTED_UPLOAD_REFRESH_FAILURE_PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_standard_successful_upload_reports_later_refresh_failure_as_committed(self):
        self.pair_browser()
        page = self.client.get("/").get_data(as_text=True)
        start = page.index("async function uploadMedia()")
        end = page.index("\n\nasync function removeMedia", start)
        function_source = page[start:end]
        harness = r"""
let messages=[];
const input={files:[{name:'saved.gif',size:10}],value:'selected'};
global.FormData=class {append(){}};
global.document={querySelector(){return input;}};
function setMessage(text,error=false){messages.push({text,error});}
async function api(){return {name:'saved.gif'};}
async function refreshMedia(){throw new Error('Library refresh failed.');}
globalThis.reconcileMediaItem=()=>{throw new Error('Local reconciliation failed.');};
""" + function_source + r"""
(async()=>{
  await uploadMedia();
  const final=messages.at(-1);
  if(!final.error || !final.text.includes('saved.gif') || !final.text.includes('uploaded') || !final.text.includes('Library refresh failed') || !final.text.includes('Local reconciliation failed'))throw new Error('known upload commit was lost');
  if(input.value!=='')throw new Error('file input was not reset');
  console.log('STANDARD_SUCCESS_REFRESH_FAILURE_PASS');
})().catch(error=>{console.error(error);process.exit(1)});
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("STANDARD_SUCCESS_REFRESH_FAILURE_PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_committed_device_removal_reconciles_current_and_other_devices(self):
        self.pair_browser()
        page = self.client.get("/").get_data(as_text=True)
        start = page.index("async function removePairedDevice")
        end = page.index("\nasync function preview", start)
        function_source = page[start:end]
        harness = r"""
let removal={committed:true,current:true},messages=[];
global.confirm=()=>true;
global.window={location:{}};
function setMessage(text,error=false){messages.push({text,error});}
async function api(){const error=new Error('Removal committed, but durability is uncertain.');error.data=removal;throw error;}
""" + function_source + r"""
(async()=>{
  await removePairedDevice('one','One');
  if(typeof window.location!=='string' || !window.location.startsWith('/pair?warning='))throw new Error('current device did not reconcile through pairing page');
  removal={committed:true,current:false};window.location={};
  await removePairedDevice('two','Two');
  if(typeof window.location!=='string' || !window.location.startsWith('/?warning='))throw new Error('other device did not reconcile through dashboard');
  console.log('COMMITTED_DEVICE_REMOVAL_RECONCILIATION_PASS');
})().catch(error=>{console.error(error);process.exit(1)});
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("COMMITTED_DEVICE_REMOVAL_RECONCILIATION_PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_dashboard_maps_only_known_navigation_warning_codes(self):
        source = (Path(__file__).parents[1] / "app" / "templates" / "dashboard.html").read_text()
        warning_source = next(
            line for line in source.splitlines() if line.startswith("const navigationWarningCode=")
        )
        harness = r"""
let messages=[],code='forged-warning';
global.window={location:{search:''}};
global.URLSearchParams=class {get(){return code;}};
function setMessage(text,error=false){messages.push({text,error});}
function execute(){eval(WARNING_SOURCE);}
execute();
if(messages.length!==0)throw new Error('arbitrary warning code was trusted');
code='device-removal-durability';execute();
if(messages.length!==1 || !messages[0].error || !messages[0].text.includes('durable storage'))throw new Error('known warning code was not mapped');
console.log('NAVIGATION_WARNING_CODE_PASS');
""".replace("WARNING_SOURCE", json.dumps(warning_source))
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("NAVIGATION_WARNING_CODE_PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_local_media_reconciliation_updates_library_selectors_and_rendering(self):
        source = (Path(__file__).parents[1] / "app" / "templates" / "dashboard.html").read_text()
        start = source.index("function reconcileMediaItem")
        end = source.index("\nasync function previewMedia", start)
        function_source = source[start:end]
        harness = r"""
let mediaItems=[{name:'old.gif'},{name:'remove.gif'}],syncs=0,renders=0;
function syncMediaSelects(){syncs++;}
function renderMedia(){renders++;}
""" + function_source + r"""
reconcileMediaItem({name:'saved.gif',type:'gif'});
if(!mediaItems.some(item=>item.name==='saved.gif'))throw new Error('upload item missing');
reconcileMediaItem({name:'saved.gif',type:'updated'});
if(mediaItems.filter(item=>item.name==='saved.gif').length!==1 || mediaItems.find(item=>item.name==='saved.gif').type!=='updated')throw new Error('upload item was not replaced');
reconcileMediaRemoval('remove.gif');
if(mediaItems.some(item=>item.name==='remove.gif'))throw new Error('removed item remained');
if(syncs!==3 || renders!==3)throw new Error('selectors or library were not rerendered');
console.log('LOCAL_MEDIA_RECONCILIATION_PASS');
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("LOCAL_MEDIA_RECONCILIATION_PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_committed_media_removal_preserves_warning_when_refresh_fails(self):
        self.pair_browser()
        page = self.client.get("/").get_data(as_text=True)
        start = page.index("async function removeMedia")
        end = page.index("\nasync function assignMedia", start)
        function_source = page[start:end]
        harness = r"""
let messages=[];
global.confirm=()=>true;
function setMessage(text,error=false){messages.push({text,error});}
async function api(){const error=new Error('Removal committed, but durability is uncertain.');error.data={committed:true,removed:'saved.gif'};throw error;}
async function refreshMedia(){throw new Error('Library refresh failed.');}
""" + function_source + r"""
(async()=>{
  let rejection=null;
  try{await removeMedia('saved.gif');}catch(error){rejection=error.message;}
  const final=messages.at(-1);
  if(rejection!==null)throw new Error('handler rejected: '+rejection);
  if(!final.error || !final.text.includes('Removal committed') || !final.text.includes('Library refresh failed.'))throw new Error('combined removal warning was not shown');
  console.log('COMMITTED_MEDIA_REMOVAL_RECONCILIATION_PASS');
})().catch(error=>{console.error(error);process.exit(1)});
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("COMMITTED_MEDIA_REMOVAL_RECONCILIATION_PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_successful_media_removal_reports_later_refresh_failure_as_committed(self):
        self.pair_browser()
        page = self.client.get("/").get_data(as_text=True)
        start = page.index("async function removeMedia")
        end = page.index("\nasync function assignMedia", start)
        function_source = page[start:end]
        harness = r"""
let messages=[];
global.confirm=()=>true;
function setMessage(text,error=false){messages.push({text,error});}
async function api(){return {removed:'saved.gif'};}
async function refreshMedia(){throw new Error('Library refresh failed.');}
globalThis.reconcileMediaRemoval=()=>{throw new Error('Local reconciliation failed.');};
""" + function_source + r"""
(async()=>{
  await removeMedia('saved.gif');
  const final=messages.at(-1);
  if(!final.error || !final.text.includes('saved.gif') || !final.text.includes('removed') || !final.text.includes('Library refresh failed') || !final.text.includes('Local reconciliation failed'))throw new Error('known removal commit was lost');
  console.log('MEDIA_REMOVAL_SUCCESS_REFRESH_FAILURE_PASS');
})().catch(error=>{console.error(error);process.exit(1)});
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MEDIA_REMOVAL_SUCCESS_REFRESH_FAILURE_PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_silent_committed_save_warning_survives_custom_preview(self):
        source = (Path(__file__).parents[1] / "app" / "templates" / "dashboard.html").read_text()
        save_start = source.index("async function saveConfig")
        save_end = source.index("\nfunction updateOpenWeatherSecretStatus", save_start)
        preview_start = source.index("async function preview(name)")
        preview_end = source.index("\nfunction previewSelected", preview_start)
        function_source = source[save_start:save_end] + "\n" + source[preview_start:preview_end]
        harness = r"""
let messages=[],previewCalls=0,previewModule='';
function configBody(){return {};}
function setMessage(text,error=false){messages.push({text,error});}
global.document={querySelector(){return {value:''};}};
async function api(path){
  if(path==='/api/config'){const error=new Error('Configuration saved, but display reload failed.');error.data={committed:true,display_reloaded:false};throw error;}
  previewCalls++;return {};
}
""" + function_source + r"""
(async()=>{
  await preview('custom_test');
  if(previewCalls!==1) throw new Error('preview did not continue after committed save');
  const final=messages.at(-1);
  if(!final.error || !final.text.includes('reload failed') || !final.text.includes('Previewing custom_test')) throw new Error('preview overwrote the committed-save warning');
  console.log('SILENT_COMMITTED_SAVE_PASS');
})().catch(error=>{console.error(error);process.exit(1)});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SILENT_COMMITTED_SAVE_PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_failed_preview_preserves_committed_save_warning(self):
        source = (Path(__file__).parents[1] / "app" / "templates" / "dashboard.html").read_text()
        save_start = source.index("async function saveConfig")
        save_end = source.index("\nfunction updateOpenWeatherSecretStatus", save_start)
        preview_start = source.index("async function preview(name)")
        preview_end = source.index("\nfunction previewSelected", preview_start)
        function_source = source[save_start:save_end] + "\n" + source[preview_start:preview_end]
        harness = r"""
let messages=[],previewModule='';
function configBody(){return {};}
function setMessage(text,error=false){messages.push({text,error});}
global.document={querySelector(){return {value:''};}};
async function api(path){
  if(path==='/api/config'){const error=new Error('Configuration saved, but display reload failed.');error.data={committed:true};throw error;}
  throw new Error('Preview hardware unavailable.');
}
""" + function_source + r"""
(async()=>{
  await preview('custom_test');
  const final=messages.at(-1);
  if(!final.error || !final.text.includes('display reload failed') || !final.text.includes('Preview hardware unavailable'))throw new Error('failed preview overwrote committed-save warning');
  console.log('FAILED_PREVIEW_COMMITTED_WARNING_PASS');
})().catch(error=>{console.error(error);process.exit(1)});
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FAILED_PREVIEW_COMMITTED_WARNING_PASS", result.stdout)

    def test_announcement_board_has_friendly_label_and_automatic_playlist_timing(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn(">Announcement Board<", body)
        self.assertIn(
            "const automaticBoardModules=new Set(['announcement_board'])",
            body,
        )

    def test_scrolling_text_duration_is_automatic_in_the_playlist_editor(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("function calculatedScrollDuration(module)", body)
        self.assertIn("duration.readOnly=isAutomatic", body)
        self.assertIn("isAutomatic?'Auto seconds ':'Seconds '", body)
        self.assertIn("calculatedScrollDuration(module)", body)

    def test_weather_playlist_duration_is_automatic_from_ticker_controls(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("const automaticWeatherModules=new Set(['weather','openweather_overview','openweather_forecast'])", body)
        self.assertIn("isWeatherAutomatic=automaticWeatherModules.has(row.dataset.module)", body)
        self.assertIn("duration.value=0", body)
        self.assertIn("'Auto · ticker loops × tick speed '", body)

    def test_custom_scrolling_text_editor_has_bounded_loop_count_defaulting_to_one(self):
        self.pair_browser()

        body = self.client.get("/").get_data(as_text=True)
        asset = (Path(__file__).parents[1] / "app" / "static" / "custom_canvas.js").read_text()

        self.assertIn("customControl('Scroll loops','loops','number'", body)
        self.assertIn("loops:Number(card.querySelector('[data-field=loops]').value)", body)
        self.assertIn("loops:1", body)
        self.assertIn("customControl('Scroll loops', 'loops', 'number'", asset)
        self.assertIn("loops: Number(card.querySelector('[data-field=loops]').value)", asset)

    def test_dashboard_has_advanced_canvas_timeline_creation_controls(self):
        self.pair_browser()

        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("Convert to advanced canvas", body)
        self.assertIn("advanced-canvas-editor", body)
        self.assertIn("Onion skin", body)
        self.assertIn("Duplicate frame", body)
        self.assertIn("Copy frame", body)
        self.assertIn("Paste frame", body)
        self.assertIn("Spinning square", body)
        self.assertIn("addAdvancedLayer", body)
        self.assertIn("addCanvasKeyframe", body)

    def test_advanced_editor_exposes_rotation_for_text_time_pixels_and_line(self):
        self.pair_browser()

        response = self.client.get("/static/custom_canvas.js")
        try:
            script = response.get_data(as_text=True)
        finally:
            response.close()

        self.assertIn("function supportsRotation(type)", script)
        self.assertIn("function beginLayerRotation(context, properties, type)", script)
        for marker in (
            "if (type === 'text') return {present: true, x: 2, y: 8, rotation: 0",
            "if (type === 'time') return {present: true, x: 2, y: 12, rotation: 0",
            "if (type === 'pixels') return {present: true, points: [[32, 16]], rotation: 0",
            "if (type === 'line') return {present: true, x1: 20, y1: 16, x2: 44, y2: 16, rotation: 0",
            "text: [['text', 'Text', 'text'], ['font', 'Font', 'select'], ['x', 'X', 'number'], ['y', 'Baseline Y', 'number'], ['rotation', 'Rotation', 'number']",
            "line: [['x1', 'Start X', 'number'], ['y1', 'Start Y', 'number'], ['x2', 'End X', 'number'], ['y2', 'End Y', 'number'], ['rotation', 'Rotation', 'number']",
        ):
            self.assertIn(marker, script)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_browser_rotates_text_and_line_around_the_same_bounds_centre(self):
        harness = r"""
const fs = require('fs');
global.window = {};
global.customModules = [];
global.mediaItems = [];
global.document = {querySelector: () => ({replaceChildren() {}, append() {}}), querySelectorAll: () => []};
global.syncCustomModuleUI = () => {};
global.renderCustomModules = () => {};
global.setInterval = () => 0;
eval(fs.readFileSync('app/static/custom_canvas.js', 'utf8'));
function transforms(properties, type) {
  const calls = [];
  const context = {translate: (x, y) => calls.push(['translate', x, y]), rotate: angle => calls.push(['rotate', Math.round(angle * 1000) / 1000])};
  window.beginLayerRotation(context, properties, type);
  return calls;
}
console.log(JSON.stringify({
  line: transforms({x1:10,y1:10,x2:20,y2:10,rotation:90}, 'line'),
  text: transforms({x:2,y:8,text:'HI',format:'24h',rotation:180}, 'text'),
  circle: transforms({x:10,y:10,radius:4,rotation:90}, 'circle'),
}));
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=True,
        )

        self.assertEqual(
            result.stdout.strip(),
            '{"line":[["translate",15,10],["rotate",1.571],["translate",-15,-10]],"text":[["translate",7,4.5],["rotate",3.142],["translate",-7,-4.5]],"circle":[]}',
        )

    def test_advanced_editor_has_aseprite_style_layer_tracks_and_depth_controls(self):
        self.pair_browser()

        script_response = self.client.get("/static/custom_canvas.js")
        style_response = self.client.get("/static/custom_canvas.css")
        try:
            asset = script_response.get_data(as_text=True)
            styles = style_response.get_data(as_text=True)
        finally:
            script_response.close()
            style_response.close()

        self.assertIn("function orderedLayersForFrame", asset)
        self.assertIn("function renderLayerTimeline", asset)
        self.assertIn("Layer timeline · top rows are in front", asset)
        self.assertIn("Depth at this frame", asset)
        self.assertIn("Bring forward", asset)
        self.assertIn("Send backward", asset)
        self.assertIn("layer-frame-cell", asset)
        self.assertIn("cell.dataset.layerId", asset)
        self.assertIn("cell.dataset.frame", asset)
        self.assertIn("orderedLayersForFrame(module, state.frame)", asset)
        self.assertIn(".aseprite-timeline", styles)
        self.assertIn(".layer-track-row", styles)
        self.assertIn(".layer-frame-cell", styles)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_browser_depth_order_matches_layer_array_fallback_and_keyframes(self):
        harness = r"""
const fs = require('fs');
global.window = {};
global.customModules = [];
global.mediaItems = [];
global.document = {querySelector: () => ({replaceChildren() {}, append() {}}), querySelectorAll: () => []};
global.syncCustomModuleUI = () => {};
global.renderCustomModules = () => {};
global.setInterval = () => 0;
eval(fs.readFileSync('app/static/custom_canvas.js', 'utf8'));
const back = {id:'layer_back', properties:{z_index:0}, keyframes:[{frame:0,easing:'linear',properties:{z_index:0}},{frame:2,easing:'linear',properties:{z_index:8}}]};
const front = {id:'layer_front', properties:{z_index:3}, keyframes:[]};
const module = {layers:[back, front], overrides:[]};
const legacy = {layers:[{id:'layer_a',properties:{},keyframes:[]},{id:'layer_b',properties:{},keyframes:[]}],overrides:[]};
console.log(JSON.stringify({
  frame0: window.orderedLayersForFrame(module, 0).map(item => item.layer.id),
  frame1: window.orderedLayersForFrame(module, 1).map(item => item.layer.id),
  frame2: window.orderedLayersForFrame(module, 2).map(item => item.layer.id),
  legacy: window.orderedLayersForFrame(legacy, 0).map(item => item.layer.id),
}));
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=True,
        )

        self.assertEqual(
            result.stdout.strip(),
            '{"frame0":["layer_back","layer_front"],"frame1":["layer_back","layer_front"],"frame2":["layer_front","layer_back"],"legacy":["layer_a","layer_b"]}',
        )

    def test_dashboard_new_layers_are_keyframed_and_can_be_shown_or_hidden_by_frame(self):
        self.pair_browser()

        response = self.client.get("/static/custom_canvas.js")
        try:
            asset = response.get_data(as_text=True)
        finally:
            response.close()

        self.assertIn("function initializeLayerAtCurrentFrame", asset)
        self.assertIn("layer.properties.present = false", asset)
        self.assertIn("properties: clone(initialProperties)", asset)
        self.assertIn("function setLayerPresence", asset)
        self.assertIn("Show from this frame", asset)
        self.assertIn("Hide from this frame", asset)
        self.assertIn("properties.present !== false", asset)
        self.assertIn("Present at this frame", asset)

    def test_dashboard_offers_a_spinning_rgb_cube_template(self):
        self.pair_browser()

        response = self.client.get("/static/custom_canvas.js")
        try:
            asset = response.get_data(as_text=True)
        finally:
            response.close()

        self.assertIn("function spinningRgbCube", asset)
        self.assertIn("spinning_rgb_cube", asset)
        self.assertIn("Spinning RGB cube", asset)
        self.assertIn("const cubeEdges", asset)
        self.assertIn("'#ff0000'", asset)
        self.assertIn("'#00ff00'", asset)
        self.assertIn("'#0000ff'", asset)
        self.assertIn("frame_count: 32", asset)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_advanced_editor_offers_five_valid_examples_including_matrix_rain_and_rainbow(self):
        harness = r"""
const fs = require('fs');
global.window = global;
const list = {replaceChildren: () => {}, append: () => {}};
global.document = {
  querySelectorAll: () => [],
  querySelector: selector => selector === '#custom-modules' ? list : null,
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
eval(fs.readFileSync('app/static/custom_canvas.js', 'utf8'));
global.renderCustomModules = () => {};
const base = {
  id: 'custom_example', name: 'Example', type: 'canvas', background: '#000000',
  frame_count: 1, fps: 10, playback: 'hold',
  layers: [{
    id: 'layer_initial', name: 'Initial', type: 'text', visible: true,
    properties: {x: 1, y: 7, text: 'X', font: '4x6', colour: '#ffffff'},
    keyframes: [],
  }],
  overrides: [],
};
const modules = [];
for (const example of canvasStarterExamples) {
  global.customModules = [structuredClone(base)];
  applyCanvasTemplate('custom_example', example.id);
  modules.push(structuredClone(global.customModules[0]));
}
const rainbow = modules.find(module => module.layers.every(layer => layer.type === 'circle'));
const rainbowColours = Array.from(
  {length: rainbow.frame_count},
  (_, frame) => orderedLayersForFrame(rainbow, frame).map(item => item.properties.colour),
);
process.stdout.write(JSON.stringify({
  examples: canvasStarterExamples,
  modules,
  rainbowColours,
  singleFramePingPong: advanceCanvasPlaybackFrame(0, 1, 'ping_pong', 1),
}));
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness],
            cwd=Path(__file__).parents[1], text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = __import__("json").loads(result.stdout)

        self.assertEqual(
            [example["id"] for example in payload["examples"]],
            [
                "spinning_square", "spinning_rgb_cube", "three_lines_circle",
                "matrix_rain", "rainbow_pulse",
            ],
        )
        modules = dict(zip(
            [example["id"] for example in payload["examples"]],
            payload["modules"],
        ))
        self.assertGreaterEqual(len(modules["matrix_rain"]["layers"]), 24)
        self.assertEqual(modules["matrix_rain"]["playback"], "loop")
        self.assertTrue(all(layer["keyframes"] for layer in modules["matrix_rain"]["layers"]))
        self.assertEqual(
            {layer["type"] for layer in modules["rainbow_pulse"]["layers"]},
            {"circle"},
        )
        self.assertEqual(len(modules["rainbow_pulse"]["layers"]), 7)
        self.assertEqual(modules["rainbow_pulse"]["playback"], "loop")
        self.assertEqual(
            payload["singleFramePingPong"],
            {"frame": 0, "direction": 1, "done": False},
        )

        from modules.custom_text import ordered_layers_for_frame
        physical_colours = [
            [properties["colour"] for _, properties in ordered_layers_for_frame(
                modules["rainbow_pulse"], frame,
            )]
            for frame in range(modules["rainbow_pulse"]["frame_count"])
        ]
        self.assertEqual(payload["rainbowColours"], physical_colours)

        for module in payload["modules"]:
            with tempfile.TemporaryDirectory() as directory:
                ConfigStore(Path(directory) / "config.json").update({
                    "custom_modules": [module],
                })

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_browser_playback_sequences_match_physical_renderer(self):
        asset = (Path(__file__).parents[1] / "app/static/custom_canvas.js").read_text()
        self.assertIn(
            "const advanced = window.advanceCanvasPlaybackFrame(state.frame, module.frame_count, module.playback, direction);",
            asset,
        )
        self.assertIn("state.frame = advanced.frame; direction = advanced.direction;", asset)
        self.assertIn("if (advanced.done) { clearInterval(state.timer); state.playing = false;", asset)
        harness = r"""
const fs = require('fs');
global.window = global;
global.document = {
  querySelectorAll: () => [],
  querySelector: () => ({replaceChildren() {}, append() {}}),
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
eval(fs.readFileSync('app/static/custom_canvas.js', 'utf8'));
const output = {};
for (const count of [1, 2, 3]) for (const playback of ['loop', 'hold', 'ping_pong']) {
  let frame = 0, direction = 1;
  const frames = [0];
  for (let step = 0; step < 20; step++) {
    const result = advanceCanvasPlaybackFrame(frame, count, playback, direction);
    if (result.frame < 0 || result.frame >= count) throw new Error('frame out of range');
    if (![-1, 1].includes(result.direction)) throw new Error('direction out of range');
    if (result.done || (result.frame === 0 && frames.length > 0 && playback !== 'hold')) break;
    frames.push(result.frame);
    frame = result.frame;
    direction = result.direction;
  }
  output[`${count}:${playback}`] = frames;
}
process.stdout.write(JSON.stringify(output));
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness],
            cwd=Path(__file__).parents[1], text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        browser = json.loads(result.stdout)

        from modules.custom_text import frame_sequence
        for count in (1, 2, 3):
            for playback in ("loop", "hold", "ping_pong"):
                self.assertEqual(
                    browser[f"{count}:{playback}"],
                    frame_sequence(count, playback),
                )

    def test_dashboard_advanced_canvas_has_time_image_shortcuts_and_rotation_snapping(self):
        self.pair_browser()

        response = self.client.get("/static/custom_canvas.js")
        try:
            asset = response.get_data(as_text=True)
        finally:
            response.close()

        self.assertIn("Rotation snap", asset)
        self.assertIn("optionValue + '°'", asset)
        self.assertIn("function snapRotation", asset)
        self.assertIn("snapRotation(rawRotation, state.rotationSnap)", asset)
        self.assertIn("makeButton('Add time'", asset)
        self.assertIn("makeButton('Add image'", asset)
        self.assertIn("function addSimpleImage", asset)
        self.assertIn("/\\.(png|jpe?g|webp)$/i", asset)
        self.assertIn("function currentTimeText", asset)
        self.assertIn("time: [['format', 'Time format', 'time-format']", asset)

    def test_advanced_editor_expands_timeline_when_animated_media_is_added(self):
        self.pair_browser()

        response = self.client.get("/static/custom_canvas.js")
        try:
            asset = response.get_data(as_text=True)
        finally:
            response.close()

        self.assertIn("function expandTimelineForAnimatedMedia", asset)
        self.assertIn("item?.animated", asset)
        self.assertIn("item.duration_ms", asset)
        self.assertIn("state.frame + mediaFrames", asset)
        self.assertIn("module.fps = item.fps", asset)
        self.assertIn("module.playback = 'loop'", asset)
        self.assertIn("timeline frames created automatically", asset)
        self.assertIn("expandTimelineForAnimatedMedia(module, source)", asset)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_advanced_canvas_media_preview_samples_selected_timeline_frame(self):
        source = (Path(__file__).parents[1] / "app" / "static" / "custom_canvas.js").read_text()
        start = source.index("function mediaTimelineRevision(source")
        end = source.index("\n  function drawEditor", start)
        functions = source[start:end]
        harness = """
const mediaPreviews=new Map(), mediaFailureStates=new Map(), mediaLoadQueue=[], customModules=[], mediaItems=[{name:'built-in/clip.mp4',revision:'r1'}];let activeMediaLoads=0;
const document={
  createElement(tag){return {tag,listeners:{},addEventListener(type,listener){this.listeners[type]=listener}}},
  querySelectorAll(){return []}
};
function byId(){return null}
function beginLayerRotation(){}
""" + functions + """
const first=mediaTimelineElement('built-in/clip.mp4',24,12.5);
const second=mediaTimelineElement('built-in/clip.mp4',24,12.5);
if(first!==second)throw new Error('cache miss');
if(first.tag!=='img')throw new Error('preview must use sampled sprite');
if(first.src!=='/api/media-timeline/built-in/clip.mp4?frame_count=24&fps=12.5&revision=r1')throw new Error(first.src);
first.naturalWidth=10;first.naturalHeight=240;
let drawArgs=null;
const context={save(){},restore(){},translate(){},rotate(){},drawImage(...args){drawArgs=args}};
drawShape(context,{type:'media',keyframes:[]},{x:0,y:0,width:10,height:10,rotation:0,source:'built-in/clip.mp4',keep_aspect:true},1,7,12.5,24);
if(!drawArgs||drawArgs.length!==9||drawArgs[1]!==0||drawArgs[2]!==70||drawArgs[3]!==10||drawArgs[4]!==10)throw new Error('wrong sprite crop '+JSON.stringify(drawArgs));
first.listeners.error();
const retry=mediaTimelineElement('built-in/clip.mp4',24,12.5);
if(retry===first)throw new Error('failed timeline remained cached');
retry.listeners.error();
if(mediaTimelineElement('built-in/clip.mp4',24,12.5)!==null)throw new Error('terminal failure retried without revision change');
mediaItems[0].revision='r2';
const replacement=mediaTimelineElement('built-in/clip.mp4',24,12.5);
if(!replacement||replacement===retry||!replacement.src.endsWith('&revision=r2'))throw new Error('revision did not replace timeline');
for(let count=1;count<=33;count++)mediaTimelineElement('many.gif',count,12);
if(mediaPreviews.size!==32)throw new Error('unbounded cache '+mediaPreviews.size);
if(mediaLoadQueue.length>32)throw new Error('unbounded queue '+mediaLoadQueue.length);
console.log('TIMELINE_MEDIA_PREVIEW_PASS');
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TIMELINE_MEDIA_PREVIEW_PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_advanced_canvas_terminal_media_failures_stay_bounded_and_draw_safely(self):
        source = (Path(__file__).parents[1] / "app" / "static" / "custom_canvas.js").read_text()
        start = source.index("function mediaTimelineRevision(source")
        end = source.index("\n  function drawEditor", start)
        functions = source[start:end]
        harness = """
const mediaPreviews=new Map(), mediaFailureStates=new Map(), mediaLoadQueue=[], customModules=[];let activeMediaLoads=0;
const mediaItems=Array.from({length:33},(_,index)=>({name:'failed-'+index+'.gif',revision:'r'+index}));
let created=0;
const document={
  createElement(tag){created++;return {tag,listeners:{},addEventListener(type,listener){this.listeners[type]=listener}}},
  querySelectorAll(){return []}
};
function byId(){return null}
function beginLayerRotation(){}
globalThis.setTimeout=()=>0;
""" + functions + """
for(const item of mediaItems){
  const first=mediaTimelineElement(item.name,12,12);first.listeners.error();
  const retry=mediaTimelineElement(item.name,12,12);retry.listeners.error();
}
const before=created;
if(mediaTimelineElement(mediaItems[0].name,12,12)!==null||created!==before)throw new Error('oldest terminal revision restarted');
let saves=0,restores=0;
const context={save(){saves++},restore(){restores++}};
drawShape(context,{type:'media',keyframes:[]},{source:mediaItems.at(-1).name,colour:'#fff',width:10,height:10,x:0,y:0,rotation:0,keep_aspect:true},1,0,12,12);
if(saves!==1||restores!==1||created!==before)throw new Error('terminal draw was unsafe');
console.log('TERMINAL_MEDIA_PREVIEW_PASS');
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TERMINAL_MEDIA_PREVIEW_PASS", result.stdout)

    def test_media_metadata_revision_changes_when_filename_is_reused(self):
        self.pair_browser()
        media = self.media_dir / "same.png"
        Image.new("RGB", (2, 2), "red").save(media)
        first = self.client.get("/api/media").get_json()[0]["revision"]
        media.unlink()
        Image.new("RGB", (2, 2), "blue").save(media)
        second = self.client.get("/api/media").get_json()[0]["revision"]

        self.assertNotEqual(first, second)

    def test_dashboard_advanced_canvas_supports_uploaded_resizable_media_layers(self):
        self.pair_browser()

        dashboard_response = self.client.get("/")
        asset_response = self.client.get("/static/custom_canvas.js")
        try:
            body = dashboard_response.get_data(as_text=True)
            asset = asset_response.get_data(as_text=True)
        finally:
            dashboard_response.close()
            asset_response.close()

        self.assertIn("custom_canvas.js", body)
        self.assertIn("item.name.split('/').map(encodeURIComponent).join('/')", body)
        self.assertIn("Image / GIF / MP4", asset)
        self.assertIn("Upload image, GIF or MP4", asset)
        self.assertIn("uploadAdvancedMedia", asset)
        self.assertIn("addAdvancedLayer(id, 'media', item.name)", asset)
        self.assertIn("mediaItems", asset)
        self.assertIn("keep_aspect", asset)
        self.assertIn("drawImage", asset)

    def test_dashboard_groups_media_without_reclassification_controls(self):
        self.pair_browser()

        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("Built-in media", body)
        self.assertIn("My media", body)
        self.assertIn("Default files managed from the built-in media folder.", body)
        self.assertIn("Files you can remove from the dashboard.", body)
        self.assertNotIn("Move to Built-in", body)
        self.assertNotIn("Move to My media", body)
        self.assertNotIn("classifyMedia", body)
        self.assertNotIn("/classification", body)

    def test_dashboard_has_friendly_weather_layout_motion_and_units_controls(self):
        self.store.update({"weather": {
            "units": "fahrenheit", "temperature_y": 18, "ticker_y": 29,
            "scroll_direction": "right", "scroll_speed": "slow",
        }})
        self.pair_browser()

        body = self.client.get("/").get_data(as_text=True)

        self.assertIn('id="weather-units"', body)
        for control_id in (
            "weather-temperature-y", "weather-ticker-y",
            "weather-scroll-direction", "weather-scroll-speed",
        ):
            self.assertIn(f"id:'{control_id}'", body)
        self.assertIn('<option value="fahrenheit" selected>', body)
        self.assertIn('"scroll_direction": "right"', body)
        self.assertIn('"scroll_speed": "slow"', body)
        self.assertIn("units:document.querySelector('#weather-units').value", body)
        self.assertIn("temperature_y:Number(document.querySelector('#weather-temperature-y').value)", body)
        self.assertIn("ticker_y:Number(document.querySelector('#weather-ticker-y').value)", body)

    def test_announcement_board_editor_has_accessible_grouped_foundation(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn('data-module-editor="announcement_board"', body)
        self.assertIn("initialAnnouncements", body)
        self.assertIn("announcementEditorGroups", body)
        self.assertIn("makeButton('Add announcement'", body)
        self.assertIn("makeButton('Reset announcement board'", body)
        self.assertIn("createElement('fieldset')", body)
        self.assertIn("createElement('legend')", body)
        for group in ("Content", "Schedule", "Appearance", "Timing", "Transition"):
            self.assertIn(f"name:'{group}'", body)

    def test_announcement_native_constraints_are_required_and_announced(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("if(['date','time','number'].includes(type))control.required=true", body)
        self.assertIn("nativeMessages=[]", body)
        self.assertIn("if(nativeError&&!nativeMessages.includes(nativeError))nativeMessages.push(nativeError)", body)
        self.assertIn("const messages=[...Object.values(errors),...nativeMessages]", body)
        self.assertIn("status.textContent=messages.length?messages.join(' '):'Announcement fields are valid.'", body)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_announcement_client_validation_matches_contextual_scrolling_schema(self):
        source = (Path(__file__).parents[1] / "app" / "templates" / "dashboard.html").read_text()
        match = re.search(
            r"function validateAnnouncement\(announcement\)\{.*?\nfunction applyAnnouncementValidation",
            source,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        if match is None:
            self.fail("announcement validator function not found")
        function_source = match.group(0).rsplit("\nfunction applyAnnouncementValidation", 1)[0]
        announcement = test_config_store.ConfigStoreTests.announcement(
            timing={"mode": "scroll", "loops": 1, "frame_delay": 0.05},
        )
        announcement["schedule"]["days"] = list(range(7))
        announcement["layout"] = "icon_notice"
        harness = function_source + "\nconst a=" + json.dumps(announcement) + ";let e=validateAnnouncement(a);if(!e.layout)throw new Error('layout');a.layout='text_notice';a.style.border='solid';e=validateAnnouncement(a);if(!e['style.border'])throw new Error('border');a.style.border='none';a.transition={type:'fade',duration_seconds:.5};e=validateAnnouncement(a);if(!e['transition.type'])throw new Error('transition');console.log('SCROLL_CONTEXT_PASS');"
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SCROLL_CONTEXT_PASS", result.stdout)

    def test_announcement_context_rerender_restores_focused_control(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("function rerenderAnnouncementEditorsWithFocus(announcement,path)", body)
        self.assertIn("CSS.escape(announcement.id)", body)
        self.assertIn("CSS.escape(path)", body)
        self.assertIn("rerenderAnnouncementEditorsWithFocus(announcement,'timing.mode')", body)
        self.assertIn("rerenderAnnouncementEditorsWithFocus(announcement,'transition.type')", body)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_announcement_client_validation_allows_backend_valid_media_fallback(self):
        source = (Path(__file__).parents[1] / "app" / "templates" / "dashboard.html").read_text()
        start = source.index("function validateAnnouncement(announcement)")
        end = source.index("\nfunction applyAnnouncementValidation", start)
        function_source = source[start:end]
        announcement = test_config_store.ConfigStoreTests.announcement()
        announcement["schedule"]["days"] = list(range(7))
        announcement["layout"] = "image_banner"
        announcement["media"] = None
        harness = function_source + "\nconst errors=validateAnnouncement(" + json.dumps(announcement) + ");if(errors.media)throw new Error(errors.media);console.log('MEDIA_FALLBACK_VALIDATION_PASS');"
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MEDIA_FALLBACK_VALIDATION_PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_announcement_client_validation_rejects_backend_nonprintable_unicode(self):
        source = (Path(__file__).parents[1] / "app" / "templates" / "dashboard.html").read_text()
        start = source.index("function validateAnnouncement(announcement)")
        end = source.index("\nfunction applyAnnouncementValidation", start)
        function_source = source[start:end]
        announcement = test_config_store.ConfigStoreTests.announcement()
        announcement["schedule"]["days"] = list(range(7))
        harness = function_source + "\nconst a=" + json.dumps(announcement) + ";for(const value of ['Bad\\u0085Name','Bad\\u2028Name']){a.name=value;const errors=validateAnnouncement(a);if(!errors.name)throw new Error('accepted '+JSON.stringify(value));}a.name='😀'.repeat(100);if(validateAnnouncement(a).name)throw new Error('rejected 100 code points');a.name+='😀';if(!validateAnnouncement(a).name)throw new Error('accepted 101 code points');console.log('NONPRINTABLE_UNICODE_REJECTED');"
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("NONPRINTABLE_UNICODE_REJECTED", result.stdout)

    def test_global_action_message_is_an_adaptive_live_region(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn('id="message" role="status" aria-live="polite" aria-atomic="true"', body)
        self.assertIn("message.setAttribute('role',error?'alert':'status')", body)
        self.assertIn("message.setAttribute('aria-live',error?'assertive':'polite')", body)

    def test_announcement_editor_enforces_backend_board_limit(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("if(announcements.length>=50)", body)
        self.assertIn("Announcement Board supports up to 50 announcements.", body)
        self.assertIn("if(announcements.length>50)", body)

    def test_announcement_validation_marks_controls_and_blocks_save(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("function validateAnnouncement(announcement)", body)
        self.assertIn("function validateAnnouncementBoard({focusFirst=false}={})", body)
        self.assertIn("status.setAttribute('role','status')", body)
        self.assertIn("status.setAttribute('aria-live','polite')", body)
        self.assertIn("control.setAttribute('aria-invalid',String(Boolean(error)))", body)
        self.assertIn("control.setAttribute('aria-describedby',status.id)", body)
        self.assertIn("validateAnnouncementBoard()", body)
        self.assertIn("if(typeof validateAnnouncementBoard==='function'&&!validateAnnouncementBoard({focusFirst:true}))", body)
        self.assertIn("return {saved:false,warning:'Fix the highlighted announcement fields before saving.'}", body)
        self.assertLess(
            body.index("if(typeof validateAnnouncementBoard==='function'&&!validateAnnouncementBoard({focusFirst:true}))"),
            body.index("await api('/api/config'"),
        )

    def test_announcement_board_editor_serializes_ordered_state_in_config_body(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("function announcementsFromEditor()", body)
        self.assertIn("announcement_board:{announcements:announcementsFromEditor()}", body)

    def test_current_config_api_rejects_legacy_announcement_shapes(self):
        self.pair_browser()
        for missing in ("enabled", "name", "schedule.days"):
            with self.subTest(missing=missing):
                announcement = test_config_store.ConfigStoreTests.announcement()
                announcement["schedule"]["days"] = list(range(7))
                announcement["enabled"] = True
                announcement["name"] = "Welcome"
                if missing == "schedule.days":
                    del announcement["schedule"]["days"]
                else:
                    del announcement[missing]

                response = self.client.post(
                    "/api/config",
                    json={"announcement_board": {"announcements": [announcement]}},
                )

                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.content_type, "application/json")
                self.assertIn("invalid fields", response.get_json()["error"])

    def test_add_announcement_creates_disabled_schema_valid_draft(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("function addAnnouncement()", body)
        self.assertIn("id:nextAnnouncementId()", body)
        self.assertIn("id='announcement_'+Date.now().toString(36)", body)
        self.assertIn("enabled:false", body)
        self.assertIn("layout:'text_notice'", body)
        self.assertIn("timing:{mode:'static',duration_seconds:8}", body)
        self.assertIn("transition:{type:'cut'}", body)
        self.assertIn("schedule:{start_date:today,end_date:today,days:[0,1,2,3,4,5,6],start_time:'00:00',end_time:'23:59'}", body)
        self.assertIn("makeButton('Add announcement',addAnnouncement", body)

    def test_announcement_content_group_exposes_bound_schema_fields(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("function announcementControl(announcement,labelText,path,type", body)
        for field in ("enabled", "name", "layout", "headline", "message", "media"):
            self.assertIn(f"path:'{field}'", body)
        for layout in ("text_notice", "icon_notice", "image_banner", "full_image_notice"):
            self.assertIn(f"'{layout}'", body)
        self.assertIn("announcementPathSet(announcement,path,value)", body)

    def test_announcement_media_control_tracks_layout_without_saving(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("function renderAnnouncementContent(announcement,fieldset)", body)
        self.assertIn("mediaControl.hidden=announcement.layout==='text_notice'", body)
        self.assertIn("layoutControl.addEventListener('input',syncMediaVisibility)", body)
        self.assertIn("missing.textContent=current+' (missing)'", body)

    def test_announcement_schedule_group_binds_dates_times_and_weekdays(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        for path in ("schedule.start_date", "schedule.end_date", "schedule.start_time", "schedule.end_time"):
            self.assertIn(f"path:'{path}'", body)
        self.assertIn("const announcementWeekdays=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']", body)
        self.assertIn("announcement.schedule.days.includes(index)", body)
        self.assertIn("announcement.schedule.days=[...selected].sort()", body)
        self.assertIn("if(group.name==='Schedule')renderAnnouncementSchedule(announcement,fieldset)", body)

    def test_announcement_appearance_group_exposes_all_style_fields(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        for path in ("style.font", "style.foreground", "style.background", "style.alignment", "style.border", "style.border_colour"):
            self.assertIn(f"path:'{path}'", body)
        self.assertIn("options:['left','center','right']", body)
        self.assertIn("options:['none','solid','inset']", body)
        self.assertIn("if(group.name==='Appearance')renderAnnouncementFields(announcement,fieldset,announcementAppearanceFields)", body)

    def test_announcement_timing_switches_between_exact_static_and_scroll_schemas(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("function renderAnnouncementTiming(announcement,fieldset)", body)
        self.assertIn("announcement.timing={mode:'static',duration_seconds:8}", body)
        self.assertIn("announcement.timing={mode:'scroll',loops:1,frame_delay:.05}", body)
        self.assertIn("path:'timing.duration_seconds'", body)
        self.assertIn("path:'timing.loops'", body)
        self.assertIn("path:'timing.frame_delay'", body)
        self.assertIn("type==='number'?Number(control.value)", body)
        self.assertIn("if(group.name==='Timing')renderAnnouncementTiming(announcement,fieldset)", body)

    def test_announcement_transition_switches_to_exact_contextual_schema(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("function renderAnnouncementTransition(announcement,fieldset)", body)
        self.assertIn("['cut','fade','slide','wipe','pixel_dissolve']", body)
        self.assertIn("announcement.transition={type:'cut'}", body)
        self.assertIn("announcement.transition={type:type,duration_seconds:.5}", body)
        self.assertIn("announcement.transition={type:type,direction:'left',duration_seconds:.5}", body)
        self.assertIn("path:'transition.direction'", body)
        self.assertIn("path:'transition.duration_seconds'", body)
        self.assertIn("if(group.name==='Transition')renderAnnouncementTransition(announcement,fieldset)", body)

    def test_announcement_card_previews_unsaved_draft_without_saving(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("function previewAnnouncement(id)", body)
        self.assertIn("makeButton('Preview on Matrix',()=>previewAnnouncement(announcement.id),'btn primary')", body)
        self.assertIn("applyAnnouncementValidation(card,announcement)", body)
        self.assertIn("api('/api/preview/announcement',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(announcement)})", body)
        preview_function = body[body.index("function previewAnnouncement(id)"):body.index("function duplicateAnnouncement(id)")]
        self.assertNotIn("saveConfig", preview_function)

    def test_announcement_actions_duplicate_delete_reorder_and_confirm_reset(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("function duplicateAnnouncement(id)", body)
        self.assertIn("copy.id=nextAnnouncementId()", body)
        self.assertIn("copy.enabled=false", body)
        self.assertIn("function deleteAnnouncement(id)", body)
        self.assertIn("confirm('Delete '+announcement.name+'?')", body)
        self.assertIn("function moveAnnouncement(id,offset)", body)
        self.assertIn("announcements.splice(index+offset,0,announcements.splice(index,1)[0])", body)
        self.assertIn("function resetAnnouncementBoard()", body)
        self.assertIn("confirm('Reset Announcement Board and remove all announcements?')", body)
        self.assertIn("announcements=[]", body)
        self.assertIn("addButton.dataset.announcementAction='add'", body)
        for label in ("Duplicate", "Delete", "Move up", "Move down"):
            self.assertIn(f"makeButton('{label}'", body)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_announcement_actions_preserve_focus_and_bounded_copy_names(self):
        source = (Path(__file__).parents[1] / "app" / "templates" / "dashboard.html").read_text()
        start = source.index("function renderAllAnnouncementEditors")
        end = source.index("\nfunction renderAnnouncementEditor", start)
        functions = source[start:end]
        announcement = test_config_store.ConfigStoreTests.announcement()
        announcement["schedule"]["days"] = list(range(7))
        announcement["name"] = "N" * 100
        harness = functions + """
let announcements=[__ANNOUNCEMENT_JSON__],announcementIdSequence=0;
const focusLog=[];
const CSS={escape:value=>value};
const document={querySelectorAll:()=>[{}],querySelector:selector=>({focus:()=>focusLog.push(selector)})};
const requestAnimationFrame=callback=>callback();
const confirm=()=>true;
const setMessage=()=>{};
const renderAnnouncementEditor=()=>{};
const announcementsFromEditor=()=>structuredClone(announcements);
const nextAnnouncementId=()=>`announcement_copy_${++announcementIdSequence}`;
const base=structuredClone(announcements[0]);
for(const length of [95,96,100]){
  announcements=[structuredClone(base)];announcements[0].name='N'.repeat(length);
  duplicateAnnouncement(announcements[0].id);
  if(announcements[1].name.length>100)throw new Error('copy name exceeded schema at '+length);
  duplicateAnnouncement(announcements[1].id);
  if(announcements[2].name.length>100||announcements[2].name===announcements[1].name)throw new Error('repeated copy name at '+length);
}
announcements=[];focusLog.length=0;addAnnouncement();
if(!focusLog.at(-1)?.includes('[data-announcement-path="name"]'))throw new Error('add focus');
focusLog.length=0;deleteAnnouncement(announcements[0].id);
if(!focusLog.at(-1)?.includes('[data-announcement-action="add"]'))throw new Error('delete-last focus');
announcements=[__ANNOUNCEMENT_JSON__];focusLog.length=0;resetAnnouncementBoard();
if(!focusLog.at(-1)?.includes('[data-announcement-action="add"]'))throw new Error('reset focus');
console.log('ANNOUNCEMENT_ACTION_BOUNDARIES_PASS');
""".replace("__ANNOUNCEMENT_JSON__", json.dumps(announcement))
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ANNOUNCEMENT_ACTION_BOUNDARIES_PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_announcement_name_input_updates_the_visible_card_heading(self):
        source = (Path(__file__).parents[1] / "app" / "templates" / "dashboard.html").read_text()
        start = source.index("function announcementPathGet")
        end = source.index("\nfunction renderAnnouncementContent", start)
        functions = source[start:end]
        announcement = test_config_store.ConfigStoreTests.announcement()
        harness = functions + """
const announcement=__ANNOUNCEMENT_JSON__;
const heading={textContent:announcement.name};
const control={dataset:{},listeners:{},addEventListener(type,handler){this.listeners[type]=handler}};
const label={textContent:'',append(){}};
const document={createElement:tag=>tag==='label'?label:control,querySelector:()=>heading};
const CSS={escape:value=>value};
const validateAnnouncementBoard=()=>true;
announcementControl(announcement,'Name','name','text');
control.value='Updated heading';control.listeners.input();
if(announcement.name!=='Updated heading')throw new Error('model stale');
if(heading.textContent!=='Updated heading')throw new Error('heading stale');
console.log('ANNOUNCEMENT_HEADING_SYNC_PASS');
""".replace("__ANNOUNCEMENT_JSON__", json.dumps(announcement))
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ANNOUNCEMENT_HEADING_SYNC_PASS", result.stdout)

    def test_playlist_builtin_editors_are_grouped_complete_and_resettable(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn('class="module-item"', body)
        self.assertIn('data-module-editor="weather"', body)
        self.assertIn('data-module-editor="clock_lava"', body)
        self.assertIn('data-module-editor="splash"', body)
        self.assertIn('data-module-editor="sleep"', body)
        self.assertIn("Edit", body)
        self.assertIn("Reset to default", body)
        self.assertIn("initialBuiltInSettings", body)
        self.assertIn("builtInDefaults", body)
        self.assertIn("renderBuiltInEditors", body)
        for field in (
            "line_y", "high_x", "high_y", "low_x", "low_y",
            "blob_count", "blob_min_radius", "blob_max_radius", "blob_speed",
            "time_align", "date_align", "show_time", "show_date",
            "keep_aspect", "video_fps",
        ):
            self.assertIn(f"field:'{field}'", body)
        self.assertIn("onclick=\"toggleSlotEditor('splash')\"", body)
        self.assertIn("onclick=\"toggleSlotEditor('sleep')\"", body)
        self.assertIn('id="splash-slot-editor"', body)
        self.assertIn('id="sleep-slot-editor"', body)
        self.assertIn("media_layout:{splash:builtInSettings.splash,sleep:builtInSettings.sleep}", body)
        self.assertIn("...builtInSettings.clock_lava", body)

    def test_sleep_editor_exposes_bounded_gif_playback_speed_percentage(self):
        self.store.update({"media_layout": {"sleep": {"gif_speed_percent": 175}}})
        self.pair_browser()

        body = self.client.get("/").get_data(as_text=True)

        self.assertIn(
            "field:'gif_speed_percent',label:'GIF playback speed (%)',type:'number',min:25,max:400,step:5",
            body,
        )
        self.assertIn('"gif_speed_percent": 175', body)
        self.assertIn('"gif_speed_percent": 100', body)
        self.assertIn("builtInSettings[module][definition.field]=", body)
        self.assertIn("builtInSettings[module]=structuredClone(builtInDefaults[module])", body)
        self.assertIn("media_layout:{splash:builtInSettings.splash,sleep:builtInSettings.sleep}", body)

    def test_sleep_gif_speed_round_trips_through_paired_config_api(self):
        self.pair_browser()

        saved = self.client.post(
            "/api/config",
            json={"media_layout": {"sleep": {"gif_speed_percent": 225}}},
        )

        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.get_json()["media_layout"]["sleep"]["gif_speed_percent"], 225)
        loaded = self.client.get("/api/config")
        self.assertEqual(loaded.status_code, 200)
        self.assertEqual(loaded.get_json()["media_layout"]["sleep"]["gif_speed_percent"], 225)
        self.controller.reload.assert_called_once_with()

    def test_weather_editor_has_full_positioning_and_separate_separator_and_ticker_controls(self):
        self.pair_browser()

        body = self.client.get("/").get_data(as_text=True)

        for field in (
            "now_x", "now_y", "temperature_x", "temperature_y",
            "high_x", "high_y", "low_x", "low_y", "line_y", "ticker_y",
        ):
            self.assertIn(f"field:'{field}'", body)
        self.assertIn("{name:'Separator'", body)
        self.assertIn("{name:'Ticker'", body)
        self.assertIn("field:'temperature_y',label:'Temperature height',type:'number',min:0,max:31", body)
        self.assertIn("field:'ticker_y',label:'Ticker height',type:'number',min:0,max:31", body)
        self.assertIn("field:'now_x',label:'NOW horizontal position',type:'number',min:-63,max:63", body)

    def test_dashboard_groups_built_in_and_saved_custom_lava_palettes(self):
        self.store.update({"lava": {
            "custom_palettes": [{
                "id": "palette_sunset",
                "name": "Saved sunset",
                "colours": ["#101020", "#ff6600", "#ffee99"],
                "enabled": True,
            }],
        }})
        self.pair_browser()

        response = self.client.get("/")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="lava-palette-groups"', body)
        self.assertIn('data-palette-group="built-in"', body)
        self.assertIn('data-palette-group="custom"', body)
        self.assertIn('id="custom-lava-palettes"', body)
        self.assertIn("Saved sunset", body)
        self.assertIn("Add saved palette", body)
        self.assertIn("initialCustomLavaPalettes", body)
        self.assertIn("renderCustomLavaPalettes", body)
        self.assertIn("custom_palettes:customLavaPalettesFromEditor()", body)

    def test_display_frame_is_paired_and_returns_the_controller_frame(self):
        self.controller.frame_png.return_value = (io.BytesIO(b"png-frame"), 17)
        self.assertEqual(self.client.get("/api/display-frame").status_code, 401)
        self.pair_browser()

        response = self.client.get("/api/display-frame")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"png-frame")
        self.assertEqual(response.mimetype, "image/png")
        self.assertEqual(response.headers["X-Matrix-Frame"], "17")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_location_search_is_paired_and_returns_normalized_suggestions(self):
        self.assertEqual(self.client.get("/api/locations?q=Brighton").status_code, 401)
        self.pair_browser()
        response = self.client.get("/api/locations?q=Brighton")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()[0]["label"], "Brighton, England, GB")
        self.location_search.assert_called_once_with("Brighton", count=6)

    def test_location_search_rejects_short_or_oversized_queries(self):
        self.pair_browser()
        self.assertEqual(self.client.get("/api/locations?q=B").status_code, 400)
        self.assertEqual(self.client.get("/api/locations?q=" + "x" * 101).status_code, 400)

    def test_canvas_media_layer_rejects_a_missing_media_source(self):
        self.pair_browser()
        config = self.store.get()
        config["custom_modules"] = [{
            "id": "custom_media", "name": "Media", "type": "canvas",
            "background": "#000000", "frame_count": 1, "fps": 10, "playback": "hold",
            "layers": [{
                "id": "layer_media", "name": "Media", "type": "media", "visible": True,
                "properties": {
                    "x": 0, "y": 0, "width": 64, "height": 32, "rotation": 0,
                    "source": "missing.png", "keep_aspect": True,
                },
                "keyframes": [],
            }],
            "overrides": [],
        }]

        response = self.client.post(
            "/api/config", json={"custom_modules": config["custom_modules"]}
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("media file does not exist: missing.png", response.get_json()["error"])

    def test_media_used_by_a_canvas_layer_cannot_be_removed(self):
        self.pair_browser()
        (self.media_dir / "custom.gif").write_bytes(self.gif_bytes().getvalue())
        self.store.update({"custom_modules": [{
            "id": "custom_media", "name": "Media", "type": "canvas",
            "background": "#000000", "frame_count": 1, "fps": 10, "playback": "hold",
            "layers": [{
                "id": "layer_media", "name": "Media", "type": "media", "visible": True,
                "properties": {
                    "x": 0, "y": 0, "width": 64, "height": 32, "rotation": 0,
                    "source": "custom.gif", "keep_aspect": True,
                },
                "keyframes": [],
            }],
            "overrides": [],
        }]})

        response = self.client.delete("/api/media/custom.gif")

        self.assertEqual(response.status_code, 409)
        self.assertTrue((self.media_dir / "custom.gif").exists())

    def test_media_inside_built_in_folder_is_classified_and_protected(self):
        self.pair_browser()
        built_in = self.media_dir / "built-in"
        built_in.mkdir()
        (built_in / "system.gif").write_bytes(self.gif_bytes().getvalue())
        (self.media_dir / "custom.gif").write_bytes(self.gif_bytes().getvalue())

        listing = self.client.get("/api/media").get_json()

        self.assertEqual(
            [(item["name"], item["built_in"]) for item in listing],
            [("built-in/system.gif", True), ("custom.gif", False)],
        )
        served = self.client.get("/media/built-in/system.gif")
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.mimetype, "image/gif")
        served.close()
        self.assertEqual(self.client.delete("/api/media/built-in/system.gif").status_code, 403)

    def test_media_classification_cannot_be_changed_through_the_api(self):
        self.pair_browser()
        built_in = self.media_dir / "built-in"
        built_in.mkdir()
        payload = self.gif_bytes().getvalue()
        (self.media_dir / "custom.gif").write_bytes(payload)
        (built_in / "system.gif").write_bytes(payload)

        promoted = self.client.post(
            "/api/media/custom.gif/classification", json={"built_in": True}
        )
        demoted = self.client.post(
            "/api/media/built-in/system.gif/classification", json={"built_in": False}
        )

        self.assertEqual(promoted.status_code, 405)
        self.assertEqual(demoted.status_code, 405)
        self.assertEqual((self.media_dir / "custom.gif").read_bytes(), payload)
        self.assertEqual((built_in / "system.gif").read_bytes(), payload)
        self.assertFalse((built_in / "custom.gif").exists())
        self.assertFalse((self.media_dir / "system.gif").exists())

    def test_media_timeline_endpoint_is_paired_bounded_and_source_sized(self):
        media = self.media_dir / "square.png"
        Image.new("RGB", (10, 10), "red").save(media)
        decoded = Image.new("RGB", (10, 10), "blue")
        self.controller.matrix.media_frames.return_value = [decoded] * 8

        self.assertEqual(
            self.client.get("/api/media-timeline/square.png?frame_count=8&fps=12.5").status_code,
            401,
        )
        self.pair_browser()
        response = self.client.get("/api/media-timeline/square.png?frame_count=8&fps=12.5")

        self.assertEqual((response.status_code, response.mimetype), (200, "image/png"))
        with Image.open(io.BytesIO(response.data)) as image:
            self.assertEqual((image.size, image.getpixel((0, 70))), ((10, 80), (0, 0, 255)))
        self.controller.matrix.media_frames.assert_called_once_with(
            "square.png", 8, 12.5, preserve_size=True, decode_timeout=5,
            resolved_path=media,
        )
        for query in ("frame_count=0&fps=12", "frame_count=121&fps=12", "frame_count=1&fps=0", "frame_count=1&fps=61", "frame_count=1&fps=nan"):
            self.assertEqual(
                self.client.get("/api/media-timeline/square.png?" + query).status_code,
                400,
            )
        (self.media_dir / "directory.png").mkdir()
        os.symlink(media, self.media_dir / "link.png")
        for name in ("missing.png", "directory.png", "link.png", "%2e%2e/config.json"):
            self.assertEqual(
                self.client.get(f"/api/media-timeline/{name}?frame_count=1&fps=12").status_code,
                404,
            )
        self.controller.matrix.media_frames.side_effect = ValueError("decoder internals")
        unavailable = self.client.get("/api/media-timeline/square.png?frame_count=1&fps=12")
        self.assertEqual(unavailable.status_code, 422)
        self.assertEqual(unavailable.get_json(), {"error": "Media timeline is unavailable."})
        self.assertNotIn(b"decoder internals", unavailable.data)
        slots = self.app.extensions["media_timeline_slots"]
        self.assertTrue(slots.acquire(blocking=False))
        self.assertTrue(slots.acquire(blocking=False))
        try:
            busy = self.client.get("/api/media-timeline/square.png?frame_count=1&fps=12")
        finally:
            slots.release()
            slots.release()
        self.assertEqual((busy.status_code, busy.headers.get("Retry-After")), (429, "1"))
        self.assertEqual(busy.get_json(), {"error": "Media timeline service is busy."})

    def test_media_listing_reports_automatic_timeline_for_animated_gif(self):
        self.pair_browser()
        (self.media_dir / "animated.gif").write_bytes(self.animated_gif_bytes().getvalue())

        item = self.client.get("/api/media").get_json()[0]

        self.assertTrue(item["animated"])
        self.assertEqual(item["source_frames"], 3)
        self.assertEqual(item["frame_count"], 3)
        self.assertEqual(item["fps"], 10)
        self.assertEqual(item["duration_ms"], 300)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_media_listing_reports_automatic_timeline_for_video(self):
        self.pair_browser()
        (self.media_dir / "animated.mp4").write_bytes(self.mp4_bytes().getvalue())

        item = self.client.get("/api/media").get_json()[0]

        self.assertTrue(item["animated"])
        self.assertEqual(item["source_frames"], 6)
        self.assertEqual(item["frame_count"], 6)
        self.assertEqual(item["fps"], 12)
        self.assertEqual(item["duration_ms"], 500)

    def test_media_upload_list_and_download(self):
        self.pair_browser()
        response = self.client.post(
            "/api/media",
            data={"file": (self.gif_bytes(), "custom.gif")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue((self.media_dir / "custom.gif").is_file())
        listing = self.client.get("/api/media").get_json()
        self.assertEqual([item["name"] for item in listing], ["custom.gif"])
        served = self.client.get("/media/custom.gif")
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.mimetype, "image/gif")
        served.close()

    def test_media_preview_queues_selected_file_for_the_physical_display(self):
        self.pair_browser()
        (self.media_dir / "custom.gif").write_bytes(self.gif_bytes().getvalue())

        response = self.client.post("/api/media/custom.gif/preview")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json(), {"previewing": "custom.gif"})
        self.controller.preview_media.assert_called_once_with("custom.gif")

        built_in = self.media_dir / "built-in"
        built_in.mkdir()
        (built_in / "system.gif").write_bytes(self.gif_bytes().getvalue())
        built_in_response = self.client.post("/api/media/built-in/system.gif/preview")
        self.assertEqual(built_in_response.status_code, 202)
        self.controller.preview_media.assert_called_with("built-in/system.gif")

        (self.media_dir / "linked.gif").symlink_to(self.media_dir / "custom.gif")
        self.assertEqual(self.client.post("/api/media/linked.gif/preview").status_code, 404)
        self.assertEqual(self.client.post("/api/media/missing.gif/preview").status_code, 404)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_mp4_upload_is_optimized_listed_and_served(self):
        self.pair_browser()

        response = self.client.post(
            "/api/media",
            data={"file": (self.mp4_bytes(), "clip.mp4")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        item = response.get_json()
        self.assertEqual(item["kind"], "mp4")
        self.assertEqual((item["width"], item["height"]), (56, 32))
        listing = self.client.get("/api/media").get_json()
        self.assertEqual([entry["name"] for entry in listing], ["clip.mp4"])
        served = self.client.get("/media/clip.mp4")
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.mimetype, "video/mp4")
        served.close()

    def test_sanitizer_can_start_as_unprivileged_identity_before_exec(self):
        helper = Path(self.temp.name) / "media_sanitizer.py"
        shutil.copy2(Path(__file__).parents[1] / "media_sanitizer.py", helper)
        nobody = pwd.getpwnam("nobody")
        app = create_app(
            self.controller,
            self.store,
            self.pairing,
            secret_key="test",
            session_generation="preexec-generation",
            media_dir=self.media_dir,
            location_search=self.location_search,
            sanitizer_path=helper,
            sanitizer_python="/usr/bin/python3",
            sanitizer_identity=(nobody.pw_uid, nobody.pw_gid),
        )
        client = app.test_client()
        self.pairing.verify.return_value = True
        client.post("/pair", data={"pin": "123456", "device_name": "Test browser"})

        response = client.post(
            "/api/media",
            data={"file": (self.gif_bytes(), "preexec.gif")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        self.assertTrue((self.media_dir / "preexec.gif").is_file())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_mp4_upload_and_metadata_probe_run_as_unprivileged_identity(self):
        nobody = pwd.getpwnam("nobody")
        helper = Path(self.temp.name) / "mp4_media_sanitizer.py"
        shutil.copy2(Path(__file__).parents[1] / "media_sanitizer.py", helper)
        app = create_app(
            self.controller,
            self.store,
            self.pairing,
            secret_key="test",
            session_generation="mp4-preexec-generation",
            media_dir=self.media_dir,
            location_search=self.location_search,
            sanitizer_path=helper,
            sanitizer_python="/usr/bin/python3",
            sanitizer_identity=(nobody.pw_uid, nobody.pw_gid),
        )
        client = app.test_client()
        self.pairing.verify.return_value = True
        client.post("/pair", data={"pin": "123456", "device_name": "Test browser"})

        response = client.post(
            "/api/media",
            data={"file": (self.mp4_bytes(), "unprivileged.mp4")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["kind"], "mp4")

    def test_large_high_resolution_upload_is_accepted_and_optimized_for_matrix(self):
        self.pair_browser()
        payload = io.BytesIO()
        source = Image.frombytes("RGB", (2600, 2200), os.urandom(2600 * 2200 * 3))
        source.save(payload, format="PNG", compress_level=0)
        source.close()
        original_size = payload.tell()
        self.assertGreater(original_size, 16 * 1024 * 1024)
        payload.seek(0)

        response = self.client.post(
            "/api/media",
            data={"file": (payload, "large-source.png")},
            content_type="multipart/form-data",
        )
        payload.close()

        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        item = response.get_json()
        response.close()
        response.request.environ["wsgi.input"].close()
        self.assertLessEqual(item["width"], 64)
        self.assertLessEqual(item["height"], 32)
        self.assertLess(item["size"], original_size)
        with Image.open(self.media_dir / "large-source.png") as optimized:
            self.assertLessEqual(optimized.width, 64)
            self.assertLessEqual(optimized.height, 32)

    def test_media_routes_require_pairing_and_upload_does_not_overwrite(self):
        self.assertEqual(self.client.get("/api/media").status_code, 401)
        self.assertEqual(self.client.get("/media/custom.gif").status_code, 302)
        self.assertEqual(self.client.delete("/api/media/custom.gif").status_code, 401)
        self.pair_browser()
        (self.media_dir / "custom.gif").write_bytes(self.gif_bytes().read())
        response = self.client.post(
            "/api/media",
            data={"file": (self.gif_bytes(), "custom.gif")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 409)

    def test_media_serving_rejects_symlinks_and_config_rejects_missing_media(self):
        self.pair_browser()
        outside = Path(self.temp.name) / "outside.gif"
        outside.write_bytes(self.gif_bytes().read())
        (self.media_dir / "leak.gif").symlink_to(outside)
        self.assertEqual(self.client.get("/media/leak.gif").status_code, 404)
        response = self.client.post("/api/config", json={"media": {"splash": "missing.gif"}})
        self.assertEqual(response.status_code, 400)

    def test_rejected_upload_does_not_leave_a_retry_blocking_destination(self):
        self.pair_browser()
        failures = (
            KeyError("missing metadata"),
            IndexError("missing stream"),
            subprocess.CalledProcessError(1, ["ffprobe"]),
            subprocess.TimeoutExpired(["ffprobe"], 1),
            json.JSONDecodeError("bad metadata", "{", 1),
            UnidentifiedImageError("bad image"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with patch("webapp.subprocess.run"), patch(
                    "webapp.Image.open", side_effect=failure,
                ):
                    response = self.client.post(
                        "/api/media",
                        data={"file": (self.gif_bytes(), "rejected.gif")},
                        content_type="multipart/form-data",
                    )

                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.is_json)
                self.assertIn("error", response.get_json())
                self.assertFalse((self.media_dir / "rejected.gif").exists())

                retry = self.client.post(
                    "/api/media",
                    data={"file": (self.gif_bytes(), "rejected.gif")},
                    content_type="multipart/form-data",
                )
                self.assertEqual(retry.status_code, 201)
                (self.media_dir / "rejected.gif").unlink()

        with patch("webapp.subprocess.run"), patch(
            "webapp.Image.open", side_effect=RuntimeError("unexpected bug"),
        ):
            response = self.client.post(
                "/api/media",
                data={"file": (self.gif_bytes(), "rejected.gif")},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 500)
        self.assertFalse((self.media_dir / "rejected.gif").exists())

    def test_upload_reports_committed_state_if_publication_rollback_fails(self):
        self.pair_browser()
        destination = self.media_dir / "rollback-failed.gif"
        original_unlink = Path.unlink

        def fail_destination_unlink(path, *args, **kwargs):
            if path == destination:
                raise OSError("disk unavailable")
            return original_unlink(path, *args, **kwargs)

        with (
            patch("webapp.subprocess.run"),
            patch("webapp.Image.open", side_effect=KeyError("missing metadata")),
            patch.object(Path, "unlink", autospec=True, side_effect=fail_destination_unlink),
        ):
            response = self.client.post(
                "/api/media",
                data={"file": (self.gif_bytes(), destination.name)},
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.is_json)
        self.assertEqual(response.get_json()["committed"], True)
        self.assertEqual(response.get_json()["filename"], destination.name)
        self.assertTrue(destination.exists())
        destination.unlink()

    def test_upload_reports_committed_state_if_temporary_cleanup_fails(self):
        self.pair_browser()
        original_unlink = Path.unlink

        for prefix in (".upload-", ".sanitized-"):
            with self.subTest(prefix=prefix):
                destination = self.media_dir / f"cleanup-{prefix[1:-1]}.gif"
                failed_paths = []

                def fail_selected_cleanup(path, *args, **kwargs):
                    if (
                        path.parent == self.media_dir
                        and path.name.startswith(prefix)
                        and not failed_paths
                    ):
                        failed_paths.append(path)
                        raise OSError("cleanup unavailable")
                    return original_unlink(path, *args, **kwargs)

                with patch.object(
                    Path,
                    "unlink",
                    autospec=True,
                    side_effect=fail_selected_cleanup,
                ):
                    response = self.client.post(
                        "/api/media",
                        data={"file": (self.gif_bytes(), destination.name)},
                        content_type="multipart/form-data",
                    )

                self.assertEqual(response.status_code, 503)
                self.assertTrue(response.is_json)
                self.assertEqual(response.get_json()["committed"], True)
                self.assertEqual(response.get_json()["item"]["name"], destination.name)
                self.assertTrue(destination.exists())
                self.assertEqual(len(failed_paths), 1)
                self.assertTrue(failed_paths[0].exists())

                destination.unlink()
                for leaked in self.media_dir.iterdir():
                    if leaked.name.startswith((".upload-", ".sanitized-")):
                        leaked.unlink(missing_ok=True)

    def test_rejected_upload_reports_temporary_cleanup_failures(self):
        self.pair_browser()
        original_unlink = Path.unlink

        for prefix in (".upload-", ".sanitized-"):
            with self.subTest(prefix=prefix):
                destination = self.media_dir / f"rejected-{prefix[1:-1]}.gif"
                failed_paths = []

                def fail_selected_cleanup(path, *args, **kwargs):
                    if path.name.startswith(prefix) and not failed_paths:
                        failed_paths.append(path)
                        raise OSError("cleanup unavailable")
                    return original_unlink(path, *args, **kwargs)

                with (
                    patch("webapp.Image.open", side_effect=KeyError("bad metadata")),
                    patch.object(
                        Path, "unlink", autospec=True, side_effect=fail_selected_cleanup,
                    ),
                ):
                    response = self.client.post(
                        "/api/media",
                        data={"file": (self.gif_bytes(), destination.name)},
                        content_type="multipart/form-data",
                    )

                self.assertEqual(response.status_code, 500)
                self.assertEqual(response.get_json()["committed"], False)
                self.assertEqual(response.get_json()["cleanup_failed"], True)
                self.assertFalse(destination.exists())
                self.assertEqual(len(failed_paths), 1)
                self.assertTrue(failed_paths[0].exists())
                failed_paths[0].unlink(missing_ok=True)

    def test_sanitizer_descriptor_close_failures_do_not_lose_output_ownership(self):
        self.pair_browser()
        original_close = os.close
        original_run = subprocess.run
        for failed_call, filename in ((1, "source-close.gif"), (2, "output-close.gif")):
            with self.subTest(failed_call=failed_call):
                close_calls = 0
                helper_finished = False

                def run_helper(*args, **kwargs):
                    nonlocal helper_finished
                    result = original_run(*args, **kwargs)
                    helper_finished = True
                    return result

                def close_then_fail(fd):
                    nonlocal close_calls
                    original_close(fd)
                    if helper_finished:
                        close_calls += 1
                        if close_calls == failed_call:
                            raise OSError("descriptor close status unavailable")

                with (
                    patch("webapp.subprocess.run", side_effect=run_helper),
                    patch("webapp.os.close", side_effect=close_then_fail),
                ):
                    response = self.client.post(
                        "/api/media",
                        data={"file": (self.gif_bytes(), filename)},
                        content_type="multipart/form-data",
                    )

                self.assertEqual(response.status_code, 201)
                self.assertTrue((self.media_dir / filename).exists())
                self.assertEqual(list(self.media_dir.glob(".sanitized-*")), [])

    def test_sanitizer_rejection_reports_helper_output_cleanup_failure(self):
        self.pair_browser()
        original_unlink = Path.unlink
        failed_paths = []

        def fail_sanitized_cleanup(path, *args, **kwargs):
            if path.parent == self.media_dir and path.name.startswith(".sanitized-"):
                if path not in failed_paths:
                    failed_paths.append(path)
                raise OSError("cleanup unavailable")
            return original_unlink(path, *args, **kwargs)

        with (
            patch(
                "webapp.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, ["sanitizer"]),
            ),
            patch.object(
                Path, "unlink", autospec=True, side_effect=fail_sanitized_cleanup,
            ),
        ):
            response = self.client.post(
                "/api/media",
                data={"file": (self.gif_bytes(), "rejected-helper.gif")},
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["committed"], False)
        self.assertEqual(response.get_json()["cleanup_failed"], True)
        self.assertEqual(len(failed_paths), 1)
        self.assertTrue(failed_paths[0].exists())
        failed_paths[0].unlink(missing_ok=True)

    def test_upload_reports_post_publication_durability_uncertainty(self):
        self.pair_browser()
        with patch("webapp.os.fsync", side_effect=OSError("directory unavailable")):
            response = self.client.post(
                "/api/media",
                data={"file": (self.gif_bytes(), "uncertain.gif")},
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["committed"], True)
        self.assertEqual(response.get_json()["durability_uncertain"], True)
        self.assertEqual(response.get_json()["item"]["name"], "uncertain.gif")
        self.assertTrue((self.media_dir / "uncertain.gif").exists())

    def test_upload_rollback_reports_directory_durability_uncertainty(self):
        self.pair_browser()
        with (
            patch("webapp.os.fsync", side_effect=[None, OSError("directory unavailable")]),
            patch("webapp.Image.open", side_effect=KeyError("bad metadata")),
        ):
            response = self.client.post(
                "/api/media",
                data={"file": (self.gif_bytes(), "rollback-uncertain.gif")},
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["committed"], False)
        self.assertEqual(response.get_json()["durability_uncertain"], True)
        self.assertFalse((self.media_dir / "rollback-uncertain.gif").exists())

    def test_media_upload_rejects_unsafe_or_invalid_files(self):
        self.pair_browser()
        for filename, payload in (
            ("../escape.gif", self.gif_bytes()),
            ("not-media.txt", io.BytesIO(b"hello")),
            ("wrong-extension.png", self.gif_bytes()),
            ("broken.gif", io.BytesIO(b"not a gif")),
            ("broken.mp4", io.BytesIO(b"not an mp4")),
        ):
            with self.subTest(filename=filename):
                response = self.client.post(
                    "/api/media",
                    data={"file": (payload, filename)},
                    content_type="multipart/form-data",
                )
                self.assertEqual(response.status_code, 400)

    def test_media_remove_deletes_unreferenced_file_but_protects_active_media(self):
        self.pair_browser()
        (self.media_dir / "unused.gif").write_bytes(self.gif_bytes().read())
        (self.media_dir / "squares_tb.gif").write_bytes(self.gif_bytes().read())
        self.assertEqual(self.client.delete("/api/media/unused.gif").status_code, 200)
        self.assertFalse((self.media_dir / "unused.gif").exists())
        response = self.client.delete("/api/media/squares_tb.gif")
        self.assertEqual(response.status_code, 200)
        self.assertFalse((self.media_dir / "squares_tb.gif").exists())

        (self.media_dir / "durability.gif").write_bytes(self.gif_bytes().read())
        with patch("webapp.os.fsync", side_effect=OSError("directory unavailable")):
            uncertain = self.client.delete("/api/media/durability.gif")
        self.assertEqual(uncertain.status_code, 503)
        self.assertEqual(uncertain.get_json()["committed"], True)
        self.assertEqual(uncertain.get_json()["durability_uncertain"], True)
        self.assertFalse((self.media_dir / "durability.gif").exists())

        (self.media_dir / "active.gif").write_bytes(self.gif_bytes().read())
        self.store.update({"media": {"splash": "active.gif"}})
        response = self.client.delete("/api/media/active.gif")
        self.assertEqual(response.status_code, 409)
        self.assertTrue((self.media_dir / "active.gif").exists())
        (self.media_dir / "hermes_self_portrait.png").write_bytes(self.gif_bytes().read())
        response = self.client.delete("/api/media/hermes_self_portrait.png")
        self.assertEqual(response.status_code, 200)

    def tearDown(self):
        self.temp.cleanup()

    def test_dashboard_handles_committed_partial_success_without_blind_retry(self):
        self.pair_browser()
        page = self.client.get("/").get_data(as_text=True)

        self.assertIn(
            "if(error.data?.committed){if(!silent)setMessage(error.message,true);return {saved:true,warning:error.message,data:error.data}}",
            page,
        )
        self.assertIn("if(error.data?.committed){input.value='';try{await refreshMedia()}", page)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_rendered_dashboard_inline_javascript_is_syntactically_valid(self):
        self.pair_browser()
        page = self.client.get("/").get_data(as_text=True)
        inline = "\n".join(
            script for script in re.findall(r"<script[^>]*>(.*?)</script>", page, re.DOTALL)
            if script.strip()
        )

        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "--check", "-"],
            input=inline,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dashboard_requires_pairing(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/pair", response.headers["Location"])

    def test_valid_pin_unlocks_dashboard_and_updates_configuration(self):
        self.pairing.verify.return_value = True
        response = self.client.post("/pair", data={"pin": "123456", "device_name": "Test browser"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.controller.end_pairing.assert_called_once()
        with self.client.session_transaction() as browser_session:
            self.assertTrue(browser_session.permanent)
            self.assertEqual(browser_session["generation"], "generation-one")
        response = self.client.post("/api/config", json={"weather": {"location": "London, GB"}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["weather"]["location"], "London, GB")
        self.controller.reload.assert_called_once()

    def test_unsaved_announcement_preview_validates_without_mutating_config(self):
        self.pair_browser()
        before = self.store.get()
        announcement = {
            "id": "announcement_preview", "enabled": False, "name": "Draft preview",
            "layout": "text_notice", "headline": "PREVIEW", "message": "Unsaved draft",
            "media": None,
            "schedule": {"start_date": "2030-01-15", "end_date": "2030-01-15", "days": [0, 1, 2, 3, 4, 5, 6], "start_time": "09:00", "end_time": "17:00"},
            "style": {"foreground": "#FFFFFF", "background": "#000000", "font": "5x7", "alignment": "center", "border": "none", "border_colour": "#FFFFFF"},
            "timing": {"mode": "static", "duration_seconds": 8},
            "transition": {"type": "cut"},
        }

        response = self.client.post("/api/preview/announcement", json=announcement)

        self.assertEqual(response.status_code, 202)
        previewed = self.controller.preview_announcement.call_args.args[0]
        self.assertFalse(previewed["enabled"])
        self.assertEqual(previewed["style"]["foreground"], "#ffffff")
        self.assertEqual(self.store.get(), before)
        self.controller.reload.assert_not_called()

    def test_unsaved_announcement_preview_rejects_legacy_missing_fields(self):
        self.pair_browser()
        base = {
            "id": "announcement_preview", "enabled": False, "name": "Draft preview",
            "layout": "text_notice", "headline": "PREVIEW", "message": "Unsaved draft",
            "media": None,
            "schedule": {"start_date": "2030-01-15", "end_date": "2030-01-15", "days": [0, 1, 2, 3, 4, 5, 6], "start_time": "09:00", "end_time": "17:00"},
            "style": {"foreground": "#FFFFFF", "background": "#000000", "font": "5x7", "alignment": "center", "border": "none", "border_colour": "#FFFFFF"},
            "timing": {"mode": "static", "duration_seconds": 8},
            "transition": {"type": "cut"},
        }
        invalid_payloads = []
        for field in ("enabled", "name"):
            payload = json.loads(json.dumps(base))
            payload.pop(field)
            invalid_payloads.append(payload)
        missing_days = json.loads(json.dumps(base))
        missing_days["schedule"].pop("days")
        invalid_payloads.append(missing_days)

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.client.post("/api/preview/announcement", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn("invalid fields", response.get_json()["error"])
        self.controller.preview_announcement.assert_not_called()

    def test_unsaved_announcement_preview_rejects_invalid_payload_without_hardware_work(self):
        self.pair_browser()

        response = self.client.post("/api/preview/announcement", json={"headline": "BROKEN"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content_type, "application/json")
        self.assertIn("invalid", response.get_json()["error"].lower())
        self.controller.preview_announcement.assert_not_called()

    def test_preview_only_accepts_known_modules(self):
        self.pairing.verify.return_value = True
        self.client.post("/pair", data={"pin": "123456", "device_name": "Test browser"})
        self.assertEqual(self.client.post("/api/preview/shell").status_code, 404)
        self.assertEqual(self.client.post("/api/preview/clock_lava").status_code, 202)
        self.controller.preview.assert_called_once_with("clock_lava")

    def test_custom_text_module_is_available_to_preview_endpoint(self):
        custom = {
            "id": "custom_welcome", "name": "Welcome", "type": "text_scroll",
            "text": "HELLO", "font": "5x7", "colour": "#ffffff",
            "background": "#000000", "x": 0, "y": 20,
            "direction": "left", "speed": 0.05,
        }
        self.store.update({
            "custom_modules": [custom],
            "playlist": [{"module": "custom_welcome", "duration": 10, "enabled": True}],
        })
        self.pair_browser()

        response = self.client.post("/api/preview/custom_welcome")

        self.assertEqual(response.status_code, 202)
        self.controller.preview.assert_called_once_with("custom_welcome")

    def test_stale_session_generation_is_rejected(self):
        with self.client.session_transaction() as browser_session:
            browser_session["paired"] = True
            browser_session["generation"] = "old-generation"
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/pair", response.headers["Location"])


if __name__ == "__main__":
    unittest.main()
