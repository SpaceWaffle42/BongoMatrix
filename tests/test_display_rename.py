"""Roadmap 1.10c: rename only at the display (PIN-paired browser + panel code)."""

import io
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from config_store import ConfigStore
from device_name import DeviceName, DisplayNameStore, name_problem
from display_controller import DisplayController
from pairing import PairedDeviceStore
from secret_store import SecretStore
from webapp import create_app

ACTION = {"X-Matrix-Action": "paired-request"}


class NameRuleTests(unittest.TestCase):
    def test_accepts_letters_digits_hyphens(self):
        for name in ("kitchen", "Kids-Room-2", "a", "x" * 63, "matrix-2"):
            self.assertIsNone(name_problem(name), name)

    def test_rejects_bad_names(self):
        for name in ("", "-front", "back-", "two words", "café", "x" * 64,
                     "a.b", "under_score", None, 7):
            self.assertIsNotNone(name_problem(name), repr(name))

    def test_matrix_is_reserved_in_any_case(self):
        for name in ("matrix", "Matrix", "MATRIX"):
            self.assertIn("reserved", name_problem(name))


class DisplayNameStoreTests(unittest.TestCase):
    def test_round_trip_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DisplayNameStore(Path(tmp) / "data" / "display-name")
            self.assertIsNone(store.get())
            store.set("kitchen")
            self.assertEqual(store.get(), "kitchen")
            self.assertEqual((Path(tmp) / "data" / "display-name").read_text(), "kitchen\n")
            store.clear()
            self.assertIsNone(store.get())
            store.clear()

    def test_corrupt_file_reads_as_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "display-name"
            path.write_text("bad name!\n")
            self.assertIsNone(DisplayNameStore(path).get())
            with self.assertRaises(ValueError):
                DisplayNameStore(path).set("matrix")

    def test_custom_name_drives_published_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DisplayNameStore(Path(tmp) / "display-name")
            avahi = Mock()
            avahi.get.side_effect = ["matrix-7d03", "kitchen"]
            avahi.set.return_value = True
            name = DeviceName(lambda: store.get() or "matrix-7d03", avahi=avahi)
            store.set("kitchen")
            self.assertEqual(name.refresh(), "kitchen.local")
            avahi.set.assert_called_once_with("kitchen")


class FakeAvahi:
    def __init__(self, taken=()):
        self.host = "matrix-7d03"
        self.taken = {name.lower() for name in taken}

    def get(self):
        return self.host

    def set(self, name):
        self.host = name
        return True

    def resolves(self, name):
        return name.lower() in self.taken or name.lower() == self.host.lower()


class RenameFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        root.chmod(0o755)
        self.store = ConfigStore(root / "config.json")
        self.device_store = PairedDeviceStore(root / "paired-devices.json")
        media = root / "media"
        shutil.copytree(Path(__file__).parents[1] / "resources" / "media" / "built-in",
                        media / "built-in")
        self.avahi = FakeAvahi(taken={"office"})
        self.names = DisplayNameStore(root / "display-name")
        with patch("device_name.builtin_mac", return_value="d8:3a:dd:4f:7d:03"):
            self.display_name = DeviceName.for_device(avahi=self.avahi, store=self.names)
        self.display_name.refresh()
        self.capture = Mock()
        self.capture.matrix.width = 64
        self.controller = DisplayController(
            self.capture, self.store, ip_address_provider=lambda: "192.168.1.5",
            announce_address=True, device_name=self.display_name,
        )
        self.controller._address_watch.start("192.168.1.5")
        self.controller._announced_name = self.display_name.current()
        self.pairing = Mock()
        self.pairing.active_pin.return_value = None
        self.pairing.has_testing_pin.return_value = False
        self.app = create_app(
            self.controller, self.store, self.pairing, secret_key="test",
            session_generation="generation-one", media_dir=media,
            device_store=self.device_store,
            openweather_secrets=SecretStore(root / "secrets" / "openweather-api-key"),
            display_name=self.display_name,
        )
        self.client = self.app.test_client()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.controller.stop)
        self.addCleanup(self.controller._shutdown.set)

    def pair(self, client=None, name="Phone"):
        client = client or self.client
        self.pairing.verify.return_value = True
        client.post("/pair", data={"pin": "123456", "device_name": name})
        self.pairing.verify.return_value = False
        return client

    def post(self, path, body=None, client=None):
        return (client or self.client).post(path, json=body or {}, headers=ACTION)

    def panel_code(self):
        with self.controller._state_lock:
            self.assertTrue(self.controller._pairing_pause.is_set(), "panel shows the code")
            self.assertEqual(self.controller._pairing_label, "RENAME?")
            return self.controller._pairing_pin

    def test_unpaired_browser_cannot_read_or_rename(self):
        self.assertEqual(self.client.get("/api/display-name").status_code, 401)
        self.assertEqual(self.post("/api/display-name/start", {"name": "kitchen"}).status_code, 401)
        self.assertIsNone(self.names.get())

    def test_get_reports_default_name_and_address(self):
        self.pair()
        data = self.client.get("/api/display-name").get_json()
        self.assertEqual(data["name"], "matrix-7d03")
        self.assertEqual(data["default"], "matrix-7d03")
        self.assertFalse(data["custom"])
        self.assertEqual(data["address"], "matrix-7d03.local")
        self.assertFalse(data["pending"])

    def test_rename_needs_code_from_panel_then_publishes(self):
        self.pair()
        started = self.post("/api/display-name/start", {"name": "Kitchen"})
        self.assertEqual(started.status_code, 202, started.get_json())
        self.assertEqual(started.get_json()["expires_in"], 120)
        code = self.panel_code()
        self.assertRegex(code, r"^[0-9]{4}$")
        self.assertIsNone(self.names.get(), "nothing saved before the code")
        self.assertTrue(self.client.get("/api/display-name").get_json()["pending"])
        saved = self.post("/api/display-name/confirm", {"code": code})
        self.assertEqual(saved.status_code, 200, saved.get_json())
        data = saved.get_json()
        self.assertEqual(data["name"], "Kitchen")
        self.assertEqual(data["address"], "Kitchen.local")
        self.assertTrue(data["custom"])
        self.assertEqual(self.names.get(), "Kitchen")
        self.assertEqual(self.avahi.host, "Kitchen")
        self.assertFalse(self.controller._pairing_pause.is_set(), "panel released")
        self.assertEqual(self.controller._address_notice, ("renamed", "192.168.1.5"))

    def test_three_wrong_codes_cancel(self):
        self.pair()
        self.post("/api/display-name/start", {"name": "kitchen"})
        code = self.panel_code()
        wrong = "0000" if code != "0000" else "1111"
        first = self.post("/api/display-name/confirm", {"code": wrong})
        self.assertEqual(first.status_code, 403)
        self.assertEqual(first.get_json()["attempts_left"], 2)
        self.assertEqual(self.post("/api/display-name/confirm", {"code": wrong}).status_code, 403)
        third = self.post("/api/display-name/confirm", {"code": wrong})
        self.assertEqual(third.status_code, 410)
        self.assertFalse(self.controller._pairing_pause.is_set())
        self.assertEqual(self.post("/api/display-name/confirm", {"code": code}).status_code, 410)
        self.assertIsNone(self.names.get())

    def test_new_codes_are_rate_limited_and_locked_after_failures(self):
        self.pair()
        clock = [1000.0]
        with patch("webapp.time.monotonic", side_effect=lambda: clock[0]):
            self.assertEqual(self.post("/api/display-name/start", {"name": "kitchen"}).status_code, 202)
            again = self.post("/api/display-name/start", {"name": "kitchen"})
            self.assertEqual(again.status_code, 429)
            self.assertIn("Retry-After", again.headers)
            clock[0] += 11
            self.assertEqual(self.post("/api/display-name/start", {"name": "kitchen"}).status_code, 202)
            code = self.panel_code()
            wrong = "0000" if code != "0000" else "1111"
            for _ in range(3):
                self.post("/api/display-name/confirm", {"code": wrong})
            clock[0] += 60
            self.assertEqual(self.post("/api/display-name/start", {"name": "kitchen"}).status_code, 429)
            clock[0] += 241
            self.assertEqual(self.post("/api/display-name/start", {"name": "kitchen"}).status_code, 202)

    def test_code_expires_after_two_minutes(self):
        self.pair()
        clock = [1000.0]
        with patch("webapp.time.monotonic", side_effect=lambda: clock[0]):
            self.post("/api/display-name/start", {"name": "kitchen"})
            code = self.panel_code()
            clock[0] += 121
            self.assertEqual(self.post("/api/display-name/confirm", {"code": code}).status_code, 410)
        self.assertIsNone(self.names.get())

    def test_panel_timer_ends_code(self):
        self.pair()
        with patch("webapp.RENAME_CODE_TTL_SECONDS", 1):
            self.post("/api/display-name/start", {"name": "kitchen"})
        code = self.panel_code()
        for _ in range(300):
            if not self.controller._pairing_pause.is_set():
                break
            threading.Event().wait(0.01)
        self.assertEqual(self.post("/api/display-name/confirm", {"code": code}).status_code, 410)

    def test_other_paired_browser_cannot_use_the_code(self):
        self.pair()
        other = self.pair(self.app.test_client(), name="Kid tablet")
        self.post("/api/display-name/start", {"name": "kitchen"})
        code = self.panel_code()
        stolen = self.post("/api/display-name/confirm", {"code": code}, client=other)
        self.assertEqual(stolen.status_code, 410)
        with patch("webapp.RENAME_START_COOLDOWN_SECONDS", 0):
            blocked = self.post("/api/display-name/start", {"name": "kidsroom"}, client=other)
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(self.post("/api/display-name/confirm", {"code": code}).status_code, 200)

    def test_pairing_pin_request_voids_rename_code(self):
        self.pair()
        self.post("/api/display-name/start", {"name": "kitchen"})
        code = self.panel_code()
        token = self.controller._pairing_generation
        self.controller.show_pairing_pin("654321", ttl_seconds=60)
        self.assertFalse(self.controller.panel_code_current(token))
        self.assertEqual(self.post("/api/display-name/confirm", {"code": code}).status_code, 410)
        self.assertEqual(self.controller._pairing_pin, "654321", "PIN stays on the panel")
        with patch("webapp.time.monotonic", return_value=time.monotonic() + 30):
            start = self.post("/api/display-name/start", {"name": "kitchen"})
        self.assertEqual(start.status_code, 409, "never replaces a pairing PIN")

    def test_rejects_invalid_reserved_and_duplicate_names_before_code(self):
        self.pair()
        for name, status in (("bad name", 400), ("matrix", 400), ("x" * 64, 400),
                             ("office", 409), ("OFFICE", 409)):
            response = self.post("/api/display-name/start", {"name": name})
            self.assertEqual(response.status_code, status, name)
            self.assertFalse(self.controller._pairing_pause.is_set(), name)

    def test_own_current_name_is_not_a_duplicate(self):
        self.pair()
        self.names.set("kitchen")
        self.display_name.refresh()
        response = self.post("/api/display-name/start", {"name": "kitchen"})
        self.assertEqual(response.status_code, 202)

    def test_reset_to_default_clears_custom_name(self):
        self.pair()
        self.names.set("kitchen")
        self.display_name.refresh()
        self.assertEqual(self.post("/api/display-name/start", {"reset": True}).status_code, 202)
        saved = self.post("/api/display-name/confirm", {"code": self.panel_code()})
        self.assertEqual(saved.status_code, 200)
        self.assertIsNone(self.names.get())
        self.assertEqual(saved.get_json()["address"], "matrix-7d03.local")
        self.assertFalse(saved.get_json()["custom"])

    def test_cancel_releases_panel(self):
        self.pair()
        self.post("/api/display-name/start", {"name": "kitchen"})
        self.panel_code()
        self.assertEqual(self.post("/api/display-name/cancel").status_code, 200)
        self.assertFalse(self.controller._pairing_pause.is_set())

    def test_requires_paired_action_header(self):
        self.pair()
        response = self.client.post("/api/display-name/start", json={"name": "kitchen"})
        self.assertEqual(response.status_code, 403)

    def test_renamed_panel_draws_label_and_code(self):
        self.pair()
        self.post("/api/display-name/start", {"name": "kitchen"})
        code = self.panel_code()
        drawn = [call.args[5] for call in self.capture.draw_text_with_border.call_args_list]
        self.assertIn("RENAME?", drawn)
        self.assertIn(code, drawn)
        self.assertNotIn("PAIR", drawn)

    # -- 1.10c review findings (1-10c-review.md) -------------------------
    def frame_body(self, client=None):
        response = (client or self.client).get("/api/display-frame")
        self.assertEqual(response.status_code, 200)
        return response.get_data()

    def test_browsers_never_see_a_frame_with_the_rename_code(self):
        """Finding 1: the code is only for someone at the panel."""
        self.pair()
        other = self.pair(self.app.test_client(), name="Kid tablet")
        secret = b"frame-with-code"
        self.capture.latest_frame_png.side_effect = lambda: (io.BytesIO(secret), 5)
        self.post("/api/display-name/start", {"name": "kitchen"})
        self.panel_code()
        self.assertNotEqual(self.frame_body(), secret)
        self.assertNotEqual(self.frame_body(other), secret)
        self.post("/api/display-name/cancel")
        self.assertNotEqual(self.frame_body(other), secret, "stale code frame stays hidden")
        self.capture.latest_frame_png.side_effect = lambda: (io.BytesIO(b"next"), 6)
        self.assertEqual(self.frame_body(other), b"next", "later frames are visible again")

    def test_browsers_never_see_a_frame_with_the_pairing_pin(self):
        self.pair()
        secret = b"frame-with-pin"
        self.capture.latest_frame_png.side_effect = lambda: (io.BytesIO(secret), 9)
        self.controller.show_pairing_pin("654321", ttl_seconds=60)
        self.assertNotEqual(self.frame_body(), secret)

    def test_wrong_codes_count_across_new_challenges(self):
        """Finding 2: asking for a new code never resets the failure count."""
        self.pair()
        clock = [1000.0]
        with patch("webapp.time.monotonic", side_effect=lambda: clock[0]):
            self.post("/api/display-name/start", {"name": "kitchen"})
            code = self.panel_code()
            wrong = "0000" if code != "0000" else "1111"
            for _ in range(2):
                self.assertEqual(self.post("/api/display-name/confirm", {"code": wrong}).status_code, 403)
            self.post("/api/display-name/cancel")
            clock[0] += 11
            self.assertEqual(self.post("/api/display-name/start", {"name": "kitchen"}).status_code, 202)
            code = self.panel_code()
            wrong = "0000" if code != "0000" else "1111"
            locked = self.post("/api/display-name/confirm", {"code": wrong})
            self.assertEqual(locked.status_code, 410, "third failure overall locks out")
            clock[0] += 11
            self.assertEqual(self.post("/api/display-name/start", {"name": "kitchen"}).status_code, 429)
            clock[0] += 300
            self.assertEqual(self.post("/api/display-name/start", {"name": "kitchen"}).status_code, 202)
        self.assertIsNone(self.names.get())

    def test_pairing_request_cannot_interleave_with_confirm(self):
        """Finding 3: code check and save are atomic with pairing takeover."""
        self.pair()
        other = self.pair(self.app.test_client(), name="Kid tablet")
        self.pairing.issue_pin.return_value = "654321"
        self.pairing.remaining_seconds.return_value = 60
        self.post("/api/display-name/start", {"name": "kitchen"})
        code = self.panel_code()
        real_set = self.names.set
        observed = {}

        def set_while_pairing_races(name):
            results = []
            racer = threading.Thread(target=lambda: results.append(
                self.post("/api/devices/pair", client=other).status_code), daemon=True)
            racer.start()
            racer.join(0.3)
            observed["blocked"] = racer.is_alive()
            observed["racer"], observed["results"] = racer, results
            real_set(name)

        with patch.object(self.names, "set", side_effect=set_while_pairing_races):
            saved = self.post("/api/display-name/confirm", {"code": code})
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(observed["blocked"], "pairing waits for the rename to commit")
        observed["racer"].join(5)
        self.assertEqual(observed["results"], [202])
        self.assertEqual(self.controller._pairing_pin, "654321", "PIN shown after the rename")

    def test_start_and_confirm_take_locks_in_the_same_order(self):
        """No deadlock when a confirm arrives while a new code is being issued."""
        self.pair()
        cooldown = patch("webapp.RENAME_START_COOLDOWN_SECONDS", 0)
        cooldown.start()
        self.addCleanup(cooldown.stop)
        self.post("/api/display-name/start", {"name": "kitchen"})
        real_end = self.controller.end_panel_code
        racers = []

        def end_while_confirm_races(token):
            racer = threading.Thread(target=lambda: self.post(
                "/api/display-name/confirm", {"code": "0000"}), daemon=True)
            racer.start()
            racer.join(0.2)
            racers.append(racer)
            return real_end(token)

        with patch.object(self.controller, "end_panel_code", side_effect=end_while_confirm_races):
            starter = threading.Thread(target=lambda: self.post(
                "/api/display-name/start", {"name": "kitchen"}), daemon=True)
            starter.start()
            starter.join(5)
        self.assertFalse(starter.is_alive(), "start deadlocked against confirm")
        self.assertEqual(len(racers), 1, "start replaced the pending code")
        racers[0].join(5)
        self.assertFalse(racers[0].is_alive(), "confirm deadlocked against start")

    def test_name_taken_during_confirmation_window_is_refused(self):
        """Finding 4: occupancy is rechecked right before saving."""
        self.pair()
        self.post("/api/display-name/start", {"name": "kitchen"})
        code = self.panel_code()
        self.avahi.taken.add("kitchen")
        refused = self.post("/api/display-name/confirm", {"code": code})
        self.assertEqual(refused.status_code, 409)
        self.assertIsNone(self.names.get())
        self.assertEqual(self.avahi.host, "matrix-7d03")

    def test_publish_failure_is_not_reported_as_new_address(self):
        """Finding 5: saved but unpublished keeps showing the verified address."""
        self.pair()
        self.avahi.set = lambda name: False
        self.post("/api/display-name/start", {"name": "kitchen"})
        saved = self.post("/api/display-name/confirm", {"code": self.panel_code()})
        self.assertEqual(saved.status_code, 200)
        data = saved.get_json()
        self.assertTrue(data["saved"])
        self.assertEqual(data["name"], "kitchen")
        self.assertFalse(data["published"])
        self.assertEqual(data["address"], "matrix-7d03.local")

    # -- owner 2026-10-15: visiting the IP moves to the .local name when reachable --
    def page(self, path, host, client=None):
        response = (client or self.client).get(path, base_url=f"http://{host}")
        self.assertEqual(response.status_code, 200, (path, host))
        return response.get_data(as_text=True)

    def ip_paired_client(self):
        client = self.app.test_client()
        self.pairing.verify.return_value = True
        client.post("/pair", data={"pin": "123456", "device_name": "Phone"},
                    base_url="http://192.168.1.5:5000")
        self.pairing.verify.return_value = False
        return client

    def test_unpaired_ip_pair_page_moves_to_the_verified_name(self):
        page = self.page("/pair", "192.168.1.5:5000")
        self.assertIn('data-canonical-host="matrix-7d03.local"', page)
        self.assertIn('data-canonical-id="matrix-7d03"', page)
        self.assertIn('data-canonical-auto="1"', page)
        # Only an answer that proves it is this display (same id) moves the page,
        # and assign() keeps the IP page in history so Back still works.
        self.assertIn("mode:'cors'", page)
        self.assertIn("d.id!==root.dataset.canonicalId", page)
        self.assertIn("location.assign", page)
        self.assertNotIn("location.replace", page)
        self.assertIn("touched", page, "no move once the visitor has started typing")

    def test_paired_ip_browser_is_never_moved_only_offered_a_link(self):
        """Review 2026-10-15 MUST 2: no pairing travels between addresses."""
        dashboard = self.page("/", "192.168.1.5:5000", client=self.ip_paired_client())
        self.assertIn('data-canonical-host="matrix-7d03.local"', dashboard)
        self.assertNotIn("data-canonical-auto", dashboard)
        self.assertIn('id="canonical-link"', dashboard)
        self.assertNotIn("handoff", dashboard)

    def test_handoff_routes_do_not_exist(self):
        rules = {rule.rule for rule in self.app.url_map.iter_rules()}
        self.assertNotIn("/api/pairing/handoff", rules)
        self.assertNotIn("/pair/handoff", rules)

    def test_post_result_pages_never_carry_the_move(self):
        """Review MUST 1: a page rendered by a POST must not be turned into a GET."""
        self.pairing.verify.return_value = False
        failed = self.client.post("/pair", data={"pin": "000000", "device_name": "Phone"},
                                  base_url="http://192.168.1.5:5000")
        self.assertNotIn("data-canonical", failed.get_data(as_text=True))

    def test_name_pages_and_other_hosts_never_redirect(self):
        for host in ("matrix-7d03.local:5000", "MATRIX-7D03.local", "kitchen.local:5000", "localhost:5000"):
            self.assertNotIn("data-canonical-host", self.page("/pair", host), host)
        named = self.app.test_client()
        self.pairing.verify.return_value = True
        named.post("/pair", data={"pin": "123456", "device_name": "Laptop"},
                   base_url="http://matrix-7d03.local:5000")
        self.pairing.verify.return_value = False
        self.assertNotIn("data-canonical-host", self.page("/", "matrix-7d03.local:5000", client=named))

    def test_ipv6_literals_also_move_to_the_name(self):
        for host in ("[fe80::1]:5000", "[fe80::1]"):
            self.assertIn('data-canonical-host="matrix-7d03.local"', self.page("/pair", host), host)

    def test_no_redirect_without_a_valid_published_name(self):
        for odd in ("bad name.local", "matrix-7d03", None):
            with patch.object(self.display_name, "current", return_value=odd):
                self.assertNotIn("data-canonical-host", self.page("/pair", "192.168.1.5:5000"), odd)
        app = create_app(
            self.controller, self.store, self.pairing, secret_key="test",
            session_generation="generation-one", media_dir=Path(self.temp.name) / "media",
            device_store=self.device_store,
            openweather_secrets=SecretStore(Path(self.temp.name) / "s" / "k"),
        )
        self.assertNotIn("data-canonical-host", self.page("/pair", "192.168.1.5:5000", app.test_client()))

    def test_renamed_display_moves_to_its_new_name_with_the_same_id(self):
        self.pair()
        self.post("/api/display-name/start", {"name": "kitchen"})
        self.post("/api/display-name/confirm", {"code": self.panel_code()})
        page = self.page("/pair", "192.168.1.5:5000", self.app.test_client())
        self.assertIn('data-canonical-host="kitchen.local"', page)
        self.assertIn('data-canonical-id="matrix-7d03"', page)

    def test_unavailable_without_device_name(self):
        app = create_app(
            self.controller, self.store, self.pairing, secret_key="test",
            session_generation="generation-one", media_dir=Path(self.temp.name) / "media",
            device_store=self.device_store,
            openweather_secrets=SecretStore(Path(self.temp.name) / "s" / "k"),
        )
        client = self.pair(app.test_client())
        self.assertEqual(client.get("/api/display-name").status_code, 503)


if __name__ == "__main__":
    unittest.main()
