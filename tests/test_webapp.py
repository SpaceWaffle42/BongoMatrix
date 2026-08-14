import copy
import hashlib
import io
import json
import re
import os
import signal
import pwd
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from werkzeug.serving import make_server

from PIL import Image, UnidentifiedImageError

from config_store import ConfigStore
from font_catalog import available_font_names
from pairing import PairedDeviceStore, PairingDurabilityError
from secret_store import SecretDurabilityError, SecretStore
from modules.scroll_timing_cache import ScrollTimingCache, TimingCacheBusy
from modules.scroll_timing import frame_delay_for_seconds
from tests import test_config_store
import media_resolver
import webapp
from webapp import create_app


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        Path(self.temp.name).chmod(0o755)
        self.store = ConfigStore(Path(self.temp.name) / "config.json")
        self.device_store = PairedDeviceStore(Path(self.temp.name) / "paired-devices.json")
        self.openweather_secrets = SecretStore(Path(self.temp.name) / "secrets" / "openweather-api-key")
        self.scroll_timing_cache = ScrollTimingCache()
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
            scroll_timing_cache=self.scroll_timing_cache,
        )
        self.client = self.app.test_client()

    def post_config(self, json=None, client=None, require_revision=True, **kwargs):
        client = client or self.client
        if require_revision:
            headers = dict(kwargs.pop("headers", {}))
            headers.setdefault("If-Match", f'"{self.store.get()["revision"]}"')
            kwargs["headers"] = headers
        return client.post("/api/config", json=json, **kwargs)

    def pair_browser(self):
        self.pairing.verify.return_value = True
        self.client.post("/pair", data={"pin": "123456", "device_name": "Test browser"})

    def test_provider_scroll_timing_is_bounded_cached_and_paired(self):
        from modules.weather_app import WeatherSnapshot

        payload = {
            "base_revision": self.store.get()["revision"],
            "settings": {
                **self.store.get()["weather"],
                "location": "Unsaved Town, GB",
                "scroll_loops": 2,
                "scroll_speed": 0.055,
            },
        }
        self.assertEqual(
            self.client.post("/api/scroll-timing/weather", json=payload).status_code,
            401,
        )
        self.pair_browser()
        snapshot = WeatherSnapshot(
            location="Unsaved Town, GB", status="Clear sky", temperature=20,
            feels_like=19, high=23, low=12, humidity=50,
        )
        with patch("modules.weather_app.OpenMeteoClient.fetch", return_value=snapshot) as fetch:
            response = self.client.post("/api/scroll-timing/weather", json=payload)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()
        self.assertEqual(result["status"], "fresh")
        self.assertEqual(result["module"], "weather")
        self.assertEqual(result["loops"], 2)
        self.assertGreater(result["frames_per_loop"], 64)
        self.assertGreater(result["milliseconds"], 0)
        self.assertRegex(result["settings_digest"], r"^[0-9a-f]{64}$")
        self.assertNotIn("http", response.get_data(as_text=True).lower())
        fetch.assert_called_once_with(
            "Unsaved Town, GB", latitude=payload["settings"]["latitude"],
            longitude=payload["settings"]["longitude"], units="celsius",
            deadline=fetch.call_args.kwargs["deadline"],
        )
        self.assertGreater(fetch.call_args.kwargs["deadline"], time.monotonic())
        self.assertLessEqual(fetch.call_args.kwargs["deadline"] - time.monotonic(), 5)

        oversized = b'{"settings":"' + b'x' * (16 * 1024) + b'"}'
        response = self.client.post(
            "/api/scroll-timing/weather", data=oversized,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 413)

    def test_provider_scroll_timing_rejects_unknown_length_body_before_json_parsing(self):
        self.pair_browser()
        oversized = b'{"settings":"' + b'x' * (17 * 1024) + b'"}'
        response = self.client.open(
            "/api/scroll-timing/weather", method="POST",
            input_stream=io.BytesIO(oversized), content_type="application/json",
            environ_overrides={"CONTENT_LENGTH": "", "wsgi.input_terminated": True},
        )
        self.assertEqual(response.status_code, 413, response.get_data(as_text=True))

    def test_timing_cache_revalidates_stale_fallback_after_single_flight_wait(self):
        now = [0.0]
        cache = ScrollTimingCache(clock=lambda: now[0], fresh_seconds=5, stale_seconds=10)
        key = ("provider", "module", "digest", 0)
        cache.get_or_fill(key, lambda: {"milliseconds": 1000})
        now[0] = 9.0

        class ExpiringWait:
            def wait(self, _timeout):
                now[0] = 11.0
                return True

        cache._fills[key] = ExpiringWait()
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            cache.get_or_fill(key, lambda: self.fail("follower must not fill"))

    def test_timing_cache_ttls_resources_single_flight_and_fill_limit(self):
        now = [0.0]
        cache = ScrollTimingCache(clock=lambda: now[0], fresh_seconds=5, stale_seconds=10,
                                  max_entries=2, max_entry_bytes=64, max_bytes=90, max_fills=1)
        key = ("provider", "module", "a", 0)
        self.assertEqual(cache.get_or_fill(key, lambda: {"milliseconds": 1000})["cache"], "fill")
        now[0] = 5
        self.assertEqual(cache.get_or_fill(key, lambda: self.fail("fresh boundary refilled"))["status"], "fresh")
        now[0] = 5.001
        self.assertEqual(cache.get_or_fill(key, lambda: (_ for _ in ()).throw(OSError()))["status"], "stale")
        now[0] = 10.001
        with self.assertRaises(OSError):
            cache.get_or_fill(key, lambda: (_ for _ in ()).throw(OSError()))

        now[0] = 20
        for suffix in ("b", "c", "d"):
            cache.get_or_fill(("provider", "module", suffix, 0), lambda suffix=suffix: {"text": suffix * 20})
        self.assertLessEqual(len(cache._entries), 2)
        self.assertLessEqual(cache._bytes, 90)
        oversized_key = ("provider", "module", "oversized", 0)
        cache.get_or_fill(oversized_key, lambda: {"text": "x" * 100})
        self.assertNotIn(oversized_key, cache._entries)

        leader_started = threading.Event()
        release = threading.Event()
        results = []
        concurrent = ScrollTimingCache(max_fills=1)
        shared_key = ("provider", "module", "shared", 0)
        def fill():
            leader_started.set();release.wait(2);return {"milliseconds": 2000}
        leader = threading.Thread(target=lambda: results.append(concurrent.get_or_fill(shared_key, fill)))
        follower = threading.Thread(target=lambda: results.append(concurrent.get_or_fill(shared_key, lambda: self.fail("duplicate fill"))))
        leader.start();self.assertTrue(leader_started.wait(1));follower.start();release.set();leader.join(2);follower.join(2)
        self.assertEqual(len(results), 2)
        self.assertEqual({result["cache"] for result in results}, {"fill", "coalesced"})

        blocked = threading.Event();release = threading.Event()
        first = threading.Thread(target=lambda: concurrent.get_or_fill(
            ("provider", "module", "first", 0),
            lambda: (blocked.set(), release.wait(2), {"milliseconds": 1})[-1]))
        first.start();self.assertTrue(blocked.wait(1))
        with self.assertRaisesRegex(Exception, "busy"):
            concurrent.get_or_fill(("provider", "module", "second", 0), lambda: {"milliseconds": 2})
        release.set();first.join(2)

    def test_provider_timing_propagates_one_monotonic_deadline_to_builder(self):
        self.pair_browser()
        settings = self.store.get()["weather"]
        payload = {"base_revision": self.store.get()["revision"], "settings": settings}
        observed = []

        def prepare(_config, *, deadline):
            observed.append(deadline - time.monotonic())
            return {"frames_per_loop": 100, "total_frames": 100, "loops": 1,
                    "frame_delay": .05, "milliseconds": 5000, "texts": ["weather"]}

        original_validate = webapp.validate_config
        def slow_validate(*args, **kwargs):
            time.sleep(.05)
            return original_validate(*args, **kwargs)
        with patch("webapp.validate_config", side_effect=slow_validate), \
             patch("modules.weather_app.prepare_ticker_plan", side_effect=prepare):
            response = self.client.post("/api/scroll-timing/weather", json=payload)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(len(observed), 1)
        self.assertGreater(observed[0], 0)
        self.assertLess(observed[0], 4.98)

    def test_timing_cache_production_bounds_and_request_deadline_cover_followers_and_slots(self):
        cache = ScrollTimingCache()
        self.assertEqual((cache.max_entries, cache.max_entry_bytes, cache.max_bytes), (16, 8192, 65536))
        for index in range(17):
            cache.get_or_fill(("p", "m", str(index), 0), lambda index=index: {"text": "x" * 4000, "index": index})
        self.assertEqual(len(cache._entries), 16)
        self.assertLessEqual(cache._bytes, 65536)
        oversized = ("p", "m", "oversized", 0)
        cache.get_or_fill(oversized, lambda: {"text": "x" * 8192})
        self.assertNotIn(oversized, cache._entries)

        occupied = ScrollTimingCache()
        releases = [threading.Event(), threading.Event()]
        started = [threading.Event(), threading.Event()]
        leaders = []
        for index in range(2):
            def run(index=index):
                occupied.get_or_fill(("p", "m", f"leader-{index}", 0),
                                     lambda: (started[index].set(), releases[index].wait(2), {"milliseconds": 1})[-1])
            leaders.append(threading.Thread(target=run))
            leaders[-1].start()
            self.assertTrue(started[index].wait(1))
        with self.assertRaises(TimingCacheBusy):
            occupied.get_or_fill(("p", "m", "third", 0), lambda: {"milliseconds": 2}, deadline=time.monotonic() + .05)
        before = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "deadline"):
            occupied.get_or_fill(("p", "m", "leader-0", 0), lambda: self.fail("follower filled"), deadline=before + .05)
        self.assertLess(time.monotonic() - before, .5)
        for release in releases: release.set()
        for leader in leaders: leader.join(2)

    def test_unavailable_automatic_timing_preserves_nonzero_compatibility_durations(self):
        automatic = {"weather", "openweather_overview", "openweather_forecast", "announcement_board"}
        durations = {item["module"]: item["duration"] for item in self.store.get()["playlist"] if item["module"] in automatic}
        self.assertTrue(durations)
        self.assertTrue(all(1 <= duration <= 3600 for duration in durations.values()), durations)

        current = self.store.get()
        weather = next(item for item in current["playlist"] if item["module"] == "weather")
        weather["duration"] = 321
        saved = self.store.update({"playlist": current["playlist"]}, base_revision=current["revision"])
        self.assertEqual(next(item for item in saved["playlist"] if item["module"] == "weather")["duration"], 321)

    def test_announcement_timing_api_drives_fresh_and_unavailable_aggregate(self):
        self.pair_browser()
        current = self.store.get()
        active = test_config_store.ConfigStoreTests.announcement()
        today = time.strftime("%Y-%m-%d")
        active["schedule"].update({"start_date": today, "end_date": today,
                                   "days": [time.localtime().tm_wday],
                                   "start_time": "00:00", "end_time": "23:59"})
        payload = {"base_revision": current["revision"], "settings": {"announcements": [active]}}
        response = self.client.post("/api/scroll-timing/announcement_board", json=payload)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["display"], "8.0 s")
        self.assertEqual(response.get_json()["status"], "fresh")
        self.assertIn("refresh_at", response.get_json())
        payload["settings"]["announcements"][0]["enabled"] = False
        unavailable = self.client.post("/api/scroll-timing/announcement_board", json=payload)
        self.assertEqual(unavailable.status_code, 200, unavailable.get_data(as_text=True))
        self.assertEqual(unavailable.get_json()["display"], "Unavailable")
        self.assertIsNone(unavailable.get_json()["milliseconds"])

    def test_openweather_secret_success_rotates_timing_generation_and_invalidates_cache(self):
        self.pair_browser()
        settings = self.store.get()["openweather_overview"]
        payload = {
            "base_revision": self.store.get()["revision"],
            "settings": settings,
            "weather": self.store.get()["weather"],
        }
        plan = {
            "frames_per_loop": 100,
            "loops": settings["scroll_loops"],
            "milliseconds": 5000,
            "display": "5.0 s",
        }
        first_key = "first_key_material_123456"
        second_key = "second_key_material_654321"

        with patch("modules.openweather_overview.prepare_ticker_plan", return_value=plan) as prepare:
            initial = self.client.post("/api/scroll-timing/openweather_overview", json=payload)
            self.assertEqual(initial.status_code, 200, initial.get_data(as_text=True))
            cached = self.client.post("/api/scroll-timing/openweather_overview", json=payload)
            self.assertEqual(cached.get_json()["cache"], "hit")
            self.assertEqual(prepare.call_count, 1)

            saved = self.client.put("/api/secrets/openweather", json={"api_key": first_key})
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(len(self.scroll_timing_cache._entries), 0)
            after_set = self.client.post("/api/scroll-timing/openweather_overview", json=payload)
            self.assertEqual(after_set.status_code, 200, after_set.get_data(as_text=True))
            self.assertEqual(prepare.call_count, 2)

            replaced = self.client.put("/api/secrets/openweather", json={"api_key": second_key})
            self.assertEqual(replaced.status_code, 200)
            self.assertEqual(len(self.scroll_timing_cache._entries), 0)
            after_replace = self.client.post("/api/scroll-timing/openweather_overview", json=payload)
            self.assertEqual(after_replace.status_code, 200, after_replace.get_data(as_text=True))
            self.assertEqual(prepare.call_count, 3)

            cleared = self.client.delete("/api/secrets/openweather")
            self.assertEqual(cleared.status_code, 200)
            self.assertEqual(len(self.scroll_timing_cache._entries), 0)
            after_clear = self.client.post("/api/scroll-timing/openweather_overview", json=payload)
            self.assertEqual(after_clear.status_code, 200, after_clear.get_data(as_text=True))
            self.assertEqual(prepare.call_count, 4)

        cache_keys = repr(tuple(self.scroll_timing_cache._entries))
        self.assertNotIn(first_key, cache_keys)
        self.assertNotIn(second_key, cache_keys)

    def test_dashboard_exposes_every_installed_font_to_all_font_editors(self):
        self.pair_browser()

        body = self.client.get("/").get_data(as_text=True)
        match = re.search(r"const installedFonts=(.*?);", body)
        self.assertIsNotNone(match)
        fonts = json.loads(match.group(1))
        canvas_source = (Path(__file__).parents[1] / "app/static/custom_canvas.js").read_text()

        self.assertEqual(fonts, list(available_font_names()))
        self.assertIn("options:installedFonts", body)
        self.assertIn("const announcementAppearanceFields=[{label:'Font',path:'style.font',type:'select',options:installedFonts}", body)
        self.assertIn("const fonts = globalThis.matrixInstalledFonts || [];", canvas_source)

    def test_dashboard_renders_the_canonical_preview_version_beneath_lan_control(self):
        self.pair_browser()

        body = self.client.get("/").get_data(as_text=True)

        application_version = getattr(webapp, "APPLICATION_VERSION", None)
        self.assertEqual(application_version, "0.3.46-preview")
        self.assertEqual(body.count(f"Version {application_version}"), 1)
        self.assertRegex(
            body,
            rf"LAN CONTROL</span><br><span[^>]*>Version {re.escape(application_version)}</span>",
        )
        template = (Path(__file__).parents[1] / "app" / "templates" / "dashboard.html").read_text()
        self.assertNotIn("0.3.46-preview", template)

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

        response = self.post_config( json={"device": {"name": "Saved Matrix"}})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.content_type, "application/json")
        self.assertTrue(response.get_json()["committed"])
        self.assertEqual(response.get_json()["config"]["device"]["name"], "Saved Matrix")
        self.assertEqual(self.store.get()["device"]["name"], "Saved Matrix")
        self.assertNotIn("display unavailable", response.get_data(as_text=True))

    def test_config_get_supports_revision_etag(self):
        self.pair_browser()

        response = self.client.get("/api/config")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["ETag"], '"0"')
        self.assertEqual(response.get_json()["revision"], 0)
        for validator in ('"0"', 'W/"0"', '"99", W/"0"', '"a,b", W/"0"', '*'):
            with self.subTest(validator=validator):
                unchanged = self.client.get("/api/config", headers={"If-None-Match": validator})
                self.assertEqual(unchanged.status_code, 304)
                self.assertEqual(unchanged.headers["ETag"], '"0"')
                self.assertEqual(unchanged.get_data(), b"")
        changed = self.client.get("/api/config", headers={"If-None-Match": 'W/"9", "10"'})
        self.assertEqual(changed.status_code, 200)

    def test_config_save_requires_base_revision(self):
        self.pair_browser()
        before = self.store.get()

        response = self.post_config(
            json={"device": {"name": "Revisionless loser"}}, require_revision=False,
        )

        self.assertEqual(response.status_code, 428)
        self.assertLessEqual(len(response.data), 1024)
        self.assertEqual(response.headers["X-Matrix-Minimum-Client-Contract"], "2")
        self.assertEqual(self.store.get(), before)
        self.controller.reload.assert_not_called()

    def test_optional_conditional_config_save_rejects_stale_revision(self):
        self.pair_browser()
        committed = self.post_config( json={"device": {"name": "Winner"}},
            headers={"If-Match": '"opaque", "0"'},
        )

        self.assertEqual(committed.status_code, 200)
        self.assertEqual(committed.headers["ETag"], '"1"')
        self.assertEqual(committed.get_json()["revision"], 1)
        stale = self.post_config( json={"device": {"name": "Loser"}},
            headers={"If-Match": '"0"'},
        )
        self.assertEqual(stale.status_code, 412)
        self.assertEqual(stale.headers["ETag"], '"1"')
        self.assertEqual(stale.get_json(), {
            "error": "Configuration has changed since this client loaded it.",
            "revision": 1,
        })
        self.assertEqual(self.store.get()["device"]["name"], "Winner")
        self.assertEqual(self.controller.reload.call_count, 1)

    def test_config_conditionals_enforce_parser_and_route_bounds(self):
        self.pair_browser()
        malformed_cases = [
            ("GET", "If-None-Match", '0'),
            ("GET", "If-None-Match", '"0",'),
            ("POST", "If-Match", 'W/'),
            ("POST", "If-Match", '"a\\"b"'),
            ("GET", "If-None-Match", '"valid" junk'),
        ]
        for method, header, value in malformed_cases:
            with self.subTest(method=method, header=header, value=value):
                response = self.client.open(
                    "/api/config", method=method, json={} if method == "POST" else None,
                    headers={header: value},
                )
                self.assertEqual(response.status_code, 400)
        valid_nonmatching = ('""', '"opaque"', 'W/"opaque"', '"a:b"', '"a,b"', '"\x80"')
        for validator in valid_nonmatching:
            with self.subTest(valid_nonmatching=validator):
                changed = self.client.get(
                    "/api/config", headers={"If-None-Match": validator},
                )
                stale = self.post_config( json={}, headers={"If-Match": validator},
                )
                self.assertEqual(changed.status_code, 200)
                self.assertEqual(stale.status_code, 412)
        for header in ("If-None-Match", "If-Match"):
            with self.subTest(header=header):
                response = self.client.open(
                    "/api/config", method="POST" if header == "If-Match" else "GET",
                    json={} if header == "If-Match" else None,
                    headers={header: '"' + ('1' * 4095) + '"'},
                )
                self.assertEqual(response.status_code, 431)

        weak_write = self.post_config( json={}, headers={"If-Match": 'W/"0"'})
        stale_list = self.post_config( json={}, headers={"If-Match": '"8", "9"'})
        self.assertEqual(weak_write.status_code, 412)
        self.assertEqual(stale_list.status_code, 412)
        wildcard = self.post_config( json={"device": {"name": "Wildcard"}}, headers={"If-Match": "*"},
        )
        self.assertEqual((wildcard.status_code, wildcard.headers["ETag"]), (200, '"1"'))

        oversized = self.post_config( data=b"{" + (b" " * 1_048_576),
            content_type="application/json",
        )
        self.assertEqual(oversized.status_code, 413)

        streamed_oversized = self.post_config( data=b"{" + (b" " * 1_048_576),
            content_type="application/json",
            environ_overrides={"CONTENT_LENGTH": "", "wsgi.input_terminated": True},
        )
        self.assertEqual(streamed_oversized.status_code, 413)

    def test_dashboard_is_never_reused_from_browser_cache(self):
        self.pair_browser()

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_preview_does_not_publish_config_revision(self):
        self.pair_browser()
        before = self.store.get()["revision"]

        response = self.client.post("/api/preview/clock_lava")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.store.get()["revision"], before)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_dashboard_conditionally_saves_loaded_revision(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("const MATRIX_CLIENT_CONTRACT=2", body)
        self.assertIn("let matrixLoadedRevision=0", body)
        self.assertIn("'If-Match':matrixRevisionETag()", body)
        self.assertIn("matrixLoadedRevision=saved.revision", body)
        self.assertIn("matrixLoadedRevision=error.data.config.revision", body)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_dashboard_preload_edit_remains_dirty_when_sync_starts(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        sync_start = body.index("const MATRIX_CLIENT_CONTRACT=2")
        sync_end = body.index("const customFontAdvances", sync_start)
        save_start = body.index("async function saveConfig(")
        save_end = body.index("\nfunction updateOpenWeatherSecretStatus", save_start)
        editor_initialization = next(
            line for line in body.splitlines()
            if line.startswith("renderMedia();")
        )
        baseline_initialization = next(
            line for line in body.splitlines()
            if line.startswith("matrixSavedSnapshot=matrixCurrentSnapshot();")
        )
        production = (
            body[sync_start:sync_end] + body[save_start:save_end] + "\n"
            + editor_initialization + "\n" + baseline_initialization
        )
        scenario = r"""
class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.listeners={};this.disabled=false;this.removed=false;this.value='';}
  append(...items){this.children.push(...items)}
  setAttribute(name,value){this[name]=value}
  addEventListener(name,handler){(this.listeners[name]??=[]).push(handler)}
  showModal(){this.open=true;currentDialog=this}
  focus(){document.activeElement=this}
  close(){this.open=false}
  remove(){this.removed=true;if(currentDialog===this)currentDialog=null}
}
let currentDialog=null,reloads=0,redirect=null,nextTimer=1;
const windowListeners={},documentListeners={},responses=[];
const saveButton=new Element('button'),bodyElement=new Element('body'),field=new Element('input');
field.value='initial rendered';
const document={
  body:bodyElement,activeElement:field,visibilityState:'visible',
  querySelector(selector){if(selector==='#save-config')return saveButton;if(selector==='.config-revision-dialog')return currentDialog;return null},
  createElement(tag){return new Element(tag)},
  addEventListener(name,handler){(documentListeners[name]??=[]).push(handler)},
  removeEventListener(name,handler){documentListeners[name]=(documentListeners[name]||[]).filter(item=>item!==handler)},
};
const locationObject={reload(){reloads++}};
const window={
  addEventListener(name,handler){(windowListeners[name]??=[]).push(handler)},
  removeEventListener(name,handler){windowListeners[name]=(windowListeners[name]||[]).filter(item=>item!==handler)},
};
Object.defineProperty(window,'location',{get(){return locationObject},set(value){redirect=value}});
function configBody(){return {device:{name:field.value}}}
function setMessage(){}
function setInterval(){return nextTimer++} function clearInterval(){}
function setTimeout(){return nextTimer++} function clearTimeout(){}
function renderMedia(){} function renderCustomLavaPalettes(){} function renderCustomModules(){} function renderBuiltInEditors(){} function refreshDisplayFrame(){}
const automaticWeatherModules=[];const providerTimingPlans=new Map();function loadProviderTiming(){}
function loadAnnouncementTiming(){}
function updateStatus(){} function updateOpenWeatherSecretStatus(){}
function response(status,revision=null,data=null){return {status,statusText:'failed',redirected:false,ok:status>=200&&status<300,headers:{get(name){return name==='X-Matrix-Minimum-Client-Contract'?'2':null}},json:async()=>(data??{revision})}}
async function fetch(){const item=responses.shift();if(!item)throw new Error('unexpected fetch');return await item}
globalThis.document=document;globalThis.window=window;globalThis.fetch=fetch;
""" + production + r"""
const initialSnapshot=matrixCurrentSnapshot();
if(PRELOAD_EDIT){
  field.value='typed before load';field.focus();
  for(const handler of documentListeners.input||[])handler({target:field});
}
responses.push(response(200,1));
for(const handler of windowListeners.load||[])handler();
await new Promise(resolve=>setImmediate(resolve));
if(PRELOAD_EDIT){
  if(matrixSavedSnapshot!==initialSnapshot||!matrixDirty||!matrixHasUnsavedDraft()||reloads!==0||!currentDialog||currentDialog.matrixRemoteRevision!==1||field.value!=='typed before load'||document.activeElement===field||!saveButton.disabled){
    throw new Error('pre-load edit was reclassified as saved: newer poll reloaded or failed to open the dirty conflict dialog');
  }
  const actions=currentDialog.children.at(-1);
  if(currentDialog.children[0].textContent!=='Newer saved configuration available'||actions.children[0].textContent!=='Reload latest saved state'||actions.children[1].textContent!=='Remain with unsaved draft')throw new Error('newer poll did not open the exact dirty conflict dialog');
  const dialogFocus=document.activeElement;
  responses.push(response(200,2));
  const saved=await saveConfig();
  if(saved.saved!==false||document.activeElement!==dialogFocus||field.value!=='typed before load'||!saveButton.disabled)throw new Error('save was not gated while the conflict remained unresolved');
  actions.children[1].onclick();
  if(document.activeElement!==field||field.value!=='typed before load'||saveButton.disabled||matrixRecoveryPending)throw new Error('Remain did not preserve the pre-load draft and restore its focus');
  responses.push(response(200,2,{revision:2}));
  const successful=await saveConfig();
  if(!successful.saved||matrixSavedSnapshot!==matrixCurrentSnapshot()||matrixDirty||matrixLoadedRevision!==2)throw new Error('normal save did not update the saved baseline');
}else{
  if(matrixSavedSnapshot!==initialSnapshot||matrixDirty||matrixHasUnsavedDraft()||reloads!==1||currentDialog)throw new Error('initial no-edit path was not clean or did not reload exactly once');
}
"""
        harness = r"""
const vm=require('node:vm'),crypto=require('node:crypto').webcrypto;
const scenario=SCENARIO_SOURCE;
const run=preloadEdit=>vm.runInNewContext(
  '(async()=>{'+scenario.replaceAll('PRELOAD_EDIT',preloadEdit?'true':'false')+'})()',
  {console,AbortController,structuredClone,setImmediate,crypto},
);
(async()=>{
 await run(true);
 await run(false);
 console.log('PRELOAD_EDIT_SYNC_PASS');
})().catch(error=>{console.error(error);process.exit(1)});
""".replace("SCENARIO_SOURCE", json.dumps(scenario))
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], text=True, capture_output=True,
            cwd=Path(__file__).parents[1], check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PRELOAD_EDIT_SYNC_PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    @unittest.skipUnless(
        os.environ.get("MATRIX_CHROMIUM")
        and Path(os.environ.get("MATRIX_CHROMIUM", "")).is_file()
        and os.access(os.environ.get("MATRIX_CHROMIUM", ""), os.X_OK),
        "MATRIX_CHROMIUM browser is required",
    )
    def test_advanced_canvas_baseline_uses_real_editor_before_load(self):
        advanced_canvas = {
            "id": "custom_adversarial_canvas",
            "name": "Distinctive advanced canvas",
            "type": "canvas",
            "background": "#102938",
            "frame_count": 37,
            "fps": 23,
            "playback": "ping_pong",
            "layers": [{
                "id": "layer_distinctive",
                "name": "Layer with exact fields",
                "type": "rectangle",
                "visible": True,
                "properties": {
                    "present": True, "x": 7, "y": 11, "width": 19, "height": 13,
                    "rotation": 17, "colour": "#abcdef", "filled": True, "z_index": 9,
                },
                "keyframes": [{
                    "frame": 31, "easing": "ease_in_out",
                    "properties": {"x": 41, "rotation": 271, "colour": "#fedcba"},
                }],
            }],
            "overrides": [{
                "frame": 19, "layer_id": "layer_distinctive",
                "properties": {"y": 29, "present": False, "z_index": -4},
            }],
        }
        self.store.update({"custom_modules": [advanced_canvas]})
        expected_canvas = self.store.get()["custom_modules"][0]
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        asset = (Path(__file__).parents[1] / "app/static/custom_canvas.js").read_text()
        chromium = os.environ["MATRIX_CHROMIUM"]
        node = shutil.which("node")
        assert node is not None

        probe_template = r"""
<script>
const PROBE_EDIT=__PROBE_EDIT__,PROBE_EXPECTED=__PROBE_EXPECTED__;
let probePosted=null;
globalThis.fetch=async(path,options={})=>{
  path=String(path);
  if(path.startsWith('/api/display-frame'))return {status:503,redirected:false,ok:false};
  if(path==='/api/status')return {status:200,redirected:false,ok:true,json:async()=>({running:false,minimum_client_contract:2})};
  if(path==='/api/config'&&options.method==='POST'){
    probePosted=JSON.parse(options.body);
    return {status:200,redirected:false,ok:true,headers:{get:()=>null},json:async()=>({revision:2})};
  }
  if(path==='/api/config')return PROBE_EDIT
    ? {status:200,redirected:false,ok:true,headers:{get:()=> '2'},json:async()=>({revision:2})}
    : {status:304,redirected:false,ok:false,headers:{get:()=> '2'},json:async()=>({})};
  return {status:404,redirected:false,ok:false,headers:{get:()=>null},json:async()=>({})};
};
const probeField=document.querySelector('#device-name');
if(PROBE_EDIT){probeField.value='typed after canvas init before load';probeField.focus();probeField.dispatchEvent(new Event('input',{bubbles:true}));}
addEventListener('load',async()=>{
  await new Promise(resolve=>setTimeout(resolve,0));
  const saved=JSON.parse(matrixSavedSnapshot),current=JSON.parse(matrixCurrentSnapshot());
  const initial={
    savedCanvas:saved.custom_modules[0],currentCanvas:current.custom_modules[0],
    canvasEditor:document.querySelector('.custom-module-card')?.dataset.editorType||null,
    dirty:matrixDirty,unsaved:matrixHasUnsavedDraft(),value:probeField.value,
    conflict:!!document.querySelector('.config-revision-dialog'),recovery:matrixRecoveryPending,
  };
  if(PROBE_EDIT)document.querySelector('.config-revision-dialog .btn:not(.primary)').click();
  stopMatrixConfigSync();
  probeField.value=PROBE_EDIT?'unrelated save after pre-load edit':'unrelated normal save';
  const savedResult=await saveConfig();
  globalThis.__advancedCanvasProbe={initial,savedResult,postedCanvas:probePosted?.custom_modules?.[0],expected:PROBE_EXPECTED};
});
</script>
"""
        cdp_probe = r"""
const port=process.argv[1],pages=JSON.parse(process.argv[2]);let endpoint;
for(let attempt=0;attempt<50;attempt++){try{endpoint=await fetch(`http://127.0.0.1:${port}/json/new`,{method:'PUT'}).then(r=>r.json());break}catch(_){await new Promise(r=>setTimeout(r,100))}}
if(!endpoint)throw new Error('Chromium DevTools endpoint unavailable');
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map();let nextId=1,loadedResolve=()=>{};
socket.onmessage=event=>{const message=JSON.parse(event.data);if(message.id&&pending.has(message.id)){const request=pending.get(message.id);pending.delete(message.id);message.error?request.reject(new Error(JSON.stringify(message.error))):request.resolve(message.result)}else if(message.method==='Page.loadEventFired')loadedResolve()};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>{const id=nextId++;socket.send(JSON.stringify({id,method,params}));return new Promise((resolve,reject)=>pending.set(id,{resolve,reject}))};
const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true});if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value};
try{
 await send('Runtime.enable');await send('Page.enable');
 const reports=[];
 for(const page of pages){
   const loaded=new Promise(resolve=>loadedResolve=resolve);await send('Page.navigate',{url:'file://'+page});await loaded;
   let report;for(let attempt=0;attempt<100;attempt++){report=await evaluate('globalThis.__advancedCanvasProbe||null');if(report)break;await new Promise(resolve=>setTimeout(resolve,20))}
   if(!report)throw new Error('advanced canvas probe timed out');reports.push(report);
 }
 console.log(JSON.stringify(reports));
}finally{socket.close()}
"""
        with tempfile.TemporaryDirectory(prefix="matrix-canvas-baseline-") as directory:
            directory = Path(directory)
            (directory / "custom_canvas.js").write_text(asset)
            pages = []
            for edit in (False, True):
                injected = probe_template.replace("__PROBE_EDIT__", "true" if edit else "false").replace(
                    "__PROBE_EXPECTED__", json.dumps(expected_canvas),
                )
                page_body = body.replace('src="/static/custom_canvas.js"', 'src="custom_canvas.js"')
                page = directory / ("edit.html" if edit else "clean.html")
                page.write_text(page_body.replace("</body>", injected + "</body>"))
                pages.append(str(page))
            profile = directory / "profile"
            browser = subprocess.Popen([
                chromium, "--headless", "--no-sandbox", "--disable-gpu",
                "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                active_port = profile / "DevToolsActivePort"
                for _attempt in range(50):
                    if active_port.is_file():
                        break
                    if browser.poll() is not None:
                        self.fail("Chromium exited before publishing its DevTools port")
                    time.sleep(0.1)
                self.assertTrue(active_port.is_file(), "Chromium DevTools port unavailable")
                completed = subprocess.run(
                    [node, "-e", cdp_probe, active_port.read_text().splitlines()[0], json.dumps(pages)],
                    capture_output=True, text=True, timeout=30,
                )
            finally:
                browser.terminate()
                try:
                    browser.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    browser.kill(); browser.wait(timeout=5)
                for cleanup_attempt in range(20):
                    try:
                        shutil.rmtree(profile)
                        break
                    except FileNotFoundError:
                        break
                    except OSError:
                        if cleanup_attempt == 19:
                            raise
                        time.sleep(0.05)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        clean, edited = json.loads(completed.stdout.strip().splitlines()[-1])
        for report in (clean, edited):
            self.assertEqual(report["initial"]["savedCanvas"], expected_canvas)
            self.assertEqual(report["initial"]["currentCanvas"], expected_canvas)
            self.assertEqual(report["initial"]["canvasEditor"], "canvas")
            self.assertEqual(report["postedCanvas"], expected_canvas)
            self.assertTrue(report["savedResult"]["saved"])
        self.assertFalse(clean["initial"]["dirty"])
        self.assertFalse(clean["initial"]["unsaved"])
        self.assertFalse(clean["initial"]["conflict"])
        self.assertTrue(edited["initial"]["dirty"])
        self.assertTrue(edited["initial"]["unsaved"])
        self.assertTrue(edited["initial"]["conflict"])
        self.assertTrue(edited["initial"]["recovery"])
        self.assertEqual(edited["initial"]["value"], "typed after canvas init before load")

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    @unittest.skipUnless(
        os.environ.get("MATRIX_CHROMIUM")
        and Path(os.environ.get("MATRIX_CHROMIUM", "")).is_file()
        and os.access(os.environ.get("MATRIX_CHROMIUM", ""), os.X_OK),
        "MATRIX_CHROMIUM browser is required",
    )
    def test_delayed_canvas_asset_preserves_prebaseline_real_input(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        asset = (Path(__file__).parents[1] / "app/static/custom_canvas.js").read_bytes()
        state = {
            name: {"pages": 0, "assets": 0, "gets": 0, "posts": 0}
            for name in ("edited", "clean")
        }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def send(self, status, content=b"", content_type="application/json", headers=None):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(content)

            def scenario(self):
                referer = self.headers.get("Referer", "")
                return "clean" if "/clean" in referer or "scenario=clean" in self.path else "edited"

            def do_GET(self):
                scenario = self.scenario()
                if self.path in ("/edited", "/clean"):
                    scenario = self.path[1:]
                    state[scenario]["pages"] += 1
                    page = body.replace(
                        'src="/static/custom_canvas.js"',
                        f'src="/custom_canvas.js?scenario={scenario}"',
                    ).encode()
                    self.send(200, page, "text/html; charset=utf-8")
                elif self.path.startswith("/custom_canvas.js"):
                    state[scenario]["assets"] += 1
                    if state[scenario]["assets"] == 1:
                        time.sleep(2.5)
                    self.send(200, asset, "text/javascript; charset=utf-8")
                elif self.path == "/static/custom_canvas.css":
                    self.send(200, b"", "text/css")
                elif self.path.startswith("/api/display-frame"):
                    self.send(503, json.dumps({"error": "probe"}).encode())
                elif self.path == "/api/status":
                    self.send(200, json.dumps({"running": False, "minimum_client_contract": 2}).encode())
                elif self.path == "/api/config":
                    state[scenario]["gets"] += 1
                    time.sleep(0.2)
                    revision = 3 if state[scenario]["posts"] else 2
                    if state[scenario]["gets"] == 1 or (state[scenario]["posts"] and state[scenario]["gets"] == 2):
                        self.send(200, json.dumps({"revision": revision}).encode(), headers={
                            "ETag": f'"{revision}"', "X-Matrix-Minimum-Client-Contract": "2",
                        })
                    else:
                        self.send(304, b"", headers={"ETag": f'"{revision}"'})
                else:
                    self.send(404, b"{}")

            def do_POST(self):
                scenario = self.scenario()
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                if self.path == "/api/config":
                    state[scenario]["posts"] += 1
                    self.send(200, b'{"revision":2}')
                else:
                    self.send(404, b"{}")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        def stop_server():
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
        self.addCleanup(stop_server)
        chromium = os.environ["MATRIX_CHROMIUM"]
        node = shutil.which("node")
        assert node is not None
        cdp_probe = r"""
const debugPort=process.argv[1],appPort=process.argv[2];
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
let endpoint;
for(let attempt=0;attempt<50;attempt++){try{endpoint=await fetch(`http://127.0.0.1:${debugPort}/json/new`,{method:'PUT'}).then(r=>r.json());break}catch(_){await sleep(100)}}
if(!endpoint)throw new Error('Chromium DevTools endpoint unavailable');
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map();let nextId=1;
socket.onmessage=event=>{const message=JSON.parse(event.data);if(message.id&&pending.has(message.id)){const request=pending.get(message.id);pending.delete(message.id);message.error?request.reject(new Error(JSON.stringify(message.error))):request.resolve(message.result)}};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>{const id=nextId++;socket.send(JSON.stringify({id,method,params}));return new Promise((resolve,reject)=>pending.set(id,{resolve,reject}))};
const evaluate=async(expression,awaitPromise=false)=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise});if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value};
const waitFor=async(expression,timeout=10000)=>{const deadline=Date.now()+timeout;let value;while(Date.now()<deadline){try{value=await evaluate(expression);if(value)return value}catch(_){}await sleep(25)}throw new Error('timed out waiting for '+expression)};
try{
  await send('Runtime.enable');await send('Page.enable');
  await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/edited`});
  await waitFor(`document.readyState==='loading'&&typeof matrixDirty==='boolean'&&matrixSavedSnapshot===null&&typeof window.orderedLayersForFrame==='undefined'&&!!document.querySelector('#device-name')`);
  await evaluate(`document.querySelector('#device-name').focus();document.querySelector('#device-name').select();true`);
  await send('Input.insertText',{text:'pre-baseline Chromium draft'});
  const during=await evaluate(`({ready:document.readyState,external:typeof window.orderedLayersForFrame!=='undefined',baseline:matrixSavedSnapshot,dirty:matrixDirty,value:document.querySelector('#device-name').value,focused:document.activeElement===document.querySelector('#device-name')})`);
  if(during.ready!=='loading'||during.external||during.baseline!==null||!during.dirty||during.value!=='pre-baseline Chromium draft'||!during.focused)throw new Error('CDP input did not create the intended parser-blocked dirty state: '+JSON.stringify(during));
  await waitFor(`document.readyState==='complete'`);
  const outcome=await waitFor(`document.querySelector('.config-revision-dialog')&&({
    value:document.querySelector('#device-name').value,dirty:matrixDirty,unsaved:matrixHasUnsavedDraft(),
    equal:matrixSavedSnapshot===matrixCurrentSnapshot(),recovery:matrixRecoveryPending,disabled:document.querySelector('#save-config').disabled,
    title:document.querySelector('.config-revision-dialog h2').textContent,
    buttons:[...document.querySelectorAll('.config-revision-dialog button')].map(button=>button.textContent),
    focus:document.activeElement?.textContent
  })`,7000);
  if(outcome.value!=='pre-baseline Chromium draft'||!outcome.dirty||!outcome.unsaved||!outcome.equal||!outcome.recovery||!outcome.disabled||outcome.title!=='Newer saved configuration available'||JSON.stringify(outcome.buttons)!==JSON.stringify(['Reload latest saved state','Remain with unsaved draft'])||outcome.focus!=='Reload latest saved state')throw new Error('pre-baseline draft was silently reloaded or conflict behavior was incomplete: '+JSON.stringify(outcome));
  const gated=await evaluate(`saveConfig()`,true);
  if(gated.saved!==false||await evaluate(`document.querySelector('#device-name').value!=='pre-baseline Chromium draft'||!document.querySelector('#save-config').disabled||document.activeElement?.textContent!=='Reload latest saved state'`))throw new Error('conflict did not gate save without moving focus');
  await evaluate(`document.querySelectorAll('.config-revision-dialog button')[1].click();true`);
  if(!await waitFor(`!document.querySelector('.config-revision-dialog')&&document.activeElement===document.querySelector('#device-name')&&!matrixRecoveryPending&&!document.querySelector('#save-config').disabled`))throw new Error('Remain did not preserve draft and restore focus');
  const saved=await evaluate(`saveConfig()`,true);
  const committed=await evaluate(`({value:document.querySelector('#device-name').value,dirty:matrixDirty,unsaved:matrixHasUnsavedDraft(),equal:matrixSavedSnapshot===matrixCurrentSnapshot(),revision:matrixLoadedRevision})`);
  if(!saved.saved||committed.value!=='pre-baseline Chromium draft'||committed.dirty||committed.unsaved||!committed.equal||committed.revision!==2)throw new Error('successful save did not commit the draft baseline: '+JSON.stringify(committed));
  const directMutation=await evaluate(`(()=>{const field=document.querySelector('#device-name');field.value='programmatic draft';const unsaved=matrixHasUnsavedDraft(),dirty=matrixDirty;field.value='pre-baseline Chromium draft';return {unsaved,dirty,restoredClean:!matrixHasUnsavedDraft()}})()`);
  if(!directMutation.unsaved||directMutation.dirty||!directMutation.restoredClean)throw new Error('snapshot mismatch did not catch a direct DOM mutation: '+JSON.stringify(directMutation));
  await evaluate(`matrixCheckForNewerConfig()`);
  await waitFor(`performance.getEntriesByType('navigation')[0]?.type==='reload'`,7000);

  const clean=await fetch(`http://127.0.0.1:${debugPort}/json/new`,{method:'PUT'}).then(response=>response.json());
  const cleanSocket=new WebSocket(clean.webSocketDebuggerUrl),cleanPending=new Map();let cleanId=1;
  cleanSocket.onmessage=event=>{const message=JSON.parse(event.data);if(message.id&&cleanPending.has(message.id)){const request=cleanPending.get(message.id);cleanPending.delete(message.id);message.error?request.reject(new Error(JSON.stringify(message.error))):request.resolve(message.result)}};
  await new Promise((resolve,reject)=>{cleanSocket.onopen=resolve;cleanSocket.onerror=reject});
  const cleanSend=(method,params={})=>{const id=cleanId++;cleanSocket.send(JSON.stringify({id,method,params}));return new Promise((resolve,reject)=>cleanPending.set(id,{resolve,reject}))};
  const cleanEval=async expression=>{const result=await cleanSend('Runtime.evaluate',{expression,returnByValue:true});return result.result?.value};
  await cleanSend('Runtime.enable');await cleanSend('Page.enable');await cleanSend('Page.navigate',{url:`http://127.0.0.1:${appPort}/clean`});
  const cleanDeadline=Date.now()+10000;let cleanInitial=false;while(Date.now()<cleanDeadline){try{cleanInitial=await cleanEval(`document.readyState==='complete'&&matrixSavedSnapshot!==null&&!matrixDirty&&!matrixHasUnsavedDraft()`);if(cleanInitial)break}catch(_){}await sleep(20)}
  if(!cleanInitial)throw new Error('no-edit delayed-asset path was not initially clean');
  let cleanReload=false;while(Date.now()<cleanDeadline){try{cleanReload=await cleanEval(`performance.getEntriesByType('navigation')[0]?.type==='reload'`);if(cleanReload)break}catch(_){}await sleep(20)}
  if(!cleanReload)throw new Error('no-edit delayed-asset path did not auto-reload');
  await sleep(500);cleanSocket.close();
  console.log('DELAYED_CANVAS_PREBASELINE_PASS');
}finally{socket.close()}
"""
        with tempfile.TemporaryDirectory(prefix="matrix-delayed-canvas-") as directory:
            profile = Path(directory) / "profile"
            browser = subprocess.Popen([
                chromium, "--headless", "--no-sandbox", "--disable-gpu",
                "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                active_port = profile / "DevToolsActivePort"
                for _attempt in range(50):
                    if active_port.is_file():
                        break
                    if browser.poll() is not None:
                        self.fail("Chromium exited before publishing its DevTools port")
                    time.sleep(0.1)
                self.assertTrue(active_port.is_file(), "Chromium DevTools port unavailable")
                completed = subprocess.run([
                    node, "-e", cdp_probe, active_port.read_text().splitlines()[0], str(server.server_port),
                ], capture_output=True, text=True, timeout=35)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("DELAYED_CANVAS_PREBASELINE_PASS", completed.stdout)
                self.assertEqual(state["edited"]["pages"], 2)
                self.assertEqual(state["edited"]["posts"], 1)
                self.assertEqual(state["clean"]["pages"], 2)
                self.assertEqual(state["clean"]["posts"], 0)
            finally:
                active_failure = sys.exc_info()[0] is not None
                browser.terminate()
                try:
                    browser.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    browser.kill(); browser.wait(timeout=5)
                cleanup_error = None
                for cleanup_attempt in range(20):
                    try:
                        shutil.rmtree(profile)
                        cleanup_error = None
                        break
                    except FileNotFoundError:
                        cleanup_error = None
                        break
                    except OSError as error:
                        cleanup_error = error
                        time.sleep(0.05)
                if cleanup_error is not None and not active_failure:
                    raise cleanup_error
    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_stale_save_preserves_client_draft(self):
        self.pair_browser()
        winner = self.post_config(json={"device": {"name": "Winner"}}, headers={"If-Match": '"0"'})
        stale = self.post_config(json={"device": {"name": "Draft"}}, headers={"If-Match": '"0"'})
        body = self.client.get("/").get_data(as_text=True)

        self.assertEqual(winner.status_code, 200)
        self.assertEqual(stale.status_code, 412)
        self.assertEqual(stale.get_json()["revision"], 1)
        start = body.index("async function saveConfig(")
        end = body.index("\nfunction updateOpenWeatherSecretStatus", start)
        save_function = body[start:end]
        harness = r"""
let matrixRecoveryPending=false,matrixLoadedRevision=0,highestNotifiedRemoteRevision=0;
let matrixSavedSnapshot='saved baseline',matrixDirty=true,messageText='',request=null;
const configState={device:{name:'unsaved draft'}};
function matrixRevisionETag(){return '"'+matrixLoadedRevision+'"'}
function configBody(){return structuredClone(configState)}
function setMessage(text){messageText=text}
async function api(path,options){request={path,options};const error=new Error('Configuration changed elsewhere.');error.data={revision:1};throw error}
""" + save_function + r"""
(async()=>{
 const result=await saveConfig();
 if(result.saved!==false||matrixLoadedRevision!==0||highestNotifiedRemoteRevision!==0||matrixSavedSnapshot!=='saved baseline'||matrixDirty!==true)throw new Error('stale response changed local save state');
 if(configState.device.name!=='unsaved draft'||request.options.headers['If-Match']!=='"0"'||JSON.parse(request.options.body).device.name!=='unsaved draft')throw new Error('draft request was not preserved');
 console.log('STALE_SAVE_PRESERVATION_PASS');
})().catch(error=>{console.error(error);process.exit(1)});
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], text=True, capture_output=True,
            cwd=Path(__file__).parents[1], check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("STALE_SAVE_PRESERVATION_PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_paired_revision_sync_preserves_dirty_draft_and_orders_responses(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        required = (
            "let highestNotifiedRemoteRevision=matrixLoadedRevision",
            "async function matrixCheckForNewerConfig()",
            "'If-None-Match':matrixRevisionETag()",
            "if(remoteRevision<=matrixLoadedRevision)",
            "if(remoteRevision<=highestNotifiedRemoteRevision)",
            "matrixSyncController.abort()",
            "setInterval(matrixCheckForNewerConfig,2500)",
            "window.addEventListener('focus',matrixCheckForNewerConfig)",
            "window.addEventListener('online',matrixCheckForNewerConfig)",
            "document.visibilityState==='visible'",
            "clearInterval(matrixStatusTimer)",
            "if(!matrixSyncStopped&&previewMode==='live'&&matrixFrameTimer===null&&matrixFrameController===null)matrixFrameTimer=setTimeout",
            "Reload latest saved state",
            "Remain with unsaved draft",
        )
        for source in required:
            with self.subTest(source=source):
                self.assertIn(source, body)

        start = body.index("const MATRIX_CLIENT_CONTRACT=2")
        end = body.index("const customFontAdvances", start)
        synchronization = body[start:end]
        save_start = body.index("async function saveConfig(")
        save_end = body.index("\nfunction updateOpenWeatherSecretStatus", save_start)
        save_function = body[save_start:save_end]
        harness = r"""
class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.listeners={};this.disabled=false;this.removed=false;}
  append(...items){this.children.push(...items)}
  setAttribute(name,value){this[name]=value}
  addEventListener(name,handler){this.listeners[name]=handler}
  showModal(){this.open=true;currentDialog=this}
  focus(){document.activeElement=this}
  close(){this.open=false}
  remove(){this.removed=true;if(currentDialog===this)currentDialog=null}
}
let currentDialog=null,reloads=0,redirect=null,configState={device:{name:'saved'}},nextTimer=1;
const windowListeners={},documentListeners={},clearedIntervals=[],clearedTimeouts=[];
const message=new Element('p'),saveButton=new Element('button'),bodyElement=new Element('body'),originalFocus=new Element('input');
message.classList={toggle(){}};
const document={
  body:bodyElement,activeElement:originalFocus,visibilityState:'visible',
  querySelector(selector){if(selector==='#message')return message;if(selector==='#save-config')return saveButton;if(selector==='.config-revision-dialog')return currentDialog;return null},
  createElement(tag){return new Element(tag)},
  addEventListener(name,handler){documentListeners[name]=handler},
  removeEventListener(name,handler){if(documentListeners[name]===handler)delete documentListeners[name]},
};
const locationObject={reload(){reloads++}};
const window={
  addEventListener(name,handler){windowListeners[name]=handler},
  removeEventListener(name,handler){if(windowListeners[name]===handler)delete windowListeners[name]},
};
Object.defineProperty(window,'location',{get(){return locationObject},set(value){redirect=value}});
function configBody(){return structuredClone(configState)}
function setMessage(text){message.textContent=text}
function setInterval(){return nextTimer++} function clearInterval(id){clearedIntervals.push(id)}
function setTimeout(){return nextTimer++} function clearTimeout(id){clearedTimeouts.push(id)}
function cancelEditPreview(){}
const responses=[];
function response(status,revision=null,data=null){return {status,statusText:'failed',redirected:false,ok:status>=200&&status<300,headers:{get(name){return name==='X-Matrix-Minimum-Client-Contract'?'2':null}},json:async()=>(data??{revision})}}
async function fetch(){const item=responses.shift();return await item}
globalThis.document=document;globalThis.window=window;globalThis.fetch=fetch;
""" + synchronization + "\n" + save_function + r"""
(async()=>{
 matrixSavedSnapshot=matrixCurrentSnapshot();
 responses.push(response(200,1));await matrixCheckForNewerConfig();
 if(reloads!==1||!matrixRecoveryPending)throw new Error('clean newer snapshot did not reload exactly once');
 matrixUpgradeReloadRequested=false;setMatrixRecoveryPending(false);matrixLoadedRevision=0;highestNotifiedRemoteRevision=0;
 configState.device.name='unsaved';matrixDirty=true;const savedBaseline=matrixSavedSnapshot;
 document.activeElement=originalFocus;responses.push(response(200,1));await matrixCheckForNewerConfig();
 let dialog=currentDialog;if(!dialog||matrixLoadedRevision!==0||matrixSavedSnapshot!==savedBaseline||configState.device.name!=='unsaved')throw new Error('dirty draft/base changed');
 let actions=dialog.children.at(-1);if(!saveButton.disabled)throw new Error('save was not gated');
 actions.children[1].onclick();
 if(highestNotifiedRemoteRevision!==1||matrixRecoveryPending||saveButton.disabled||document.activeElement!==originalFocus)throw new Error('Remain did not preserve and restore state');
 const priorDialogs=bodyElement.children.length;responses.push(response(200,1));await matrixCheckForNewerConfig();
 if(bodyElement.children.length!==priorDialogs)throw new Error('same revision warned twice');
 responses.push(response(200,2));await matrixCheckForNewerConfig();
 if(!currentDialog||bodyElement.children.length!==priorDialogs+1)throw new Error('newer revision did not warn');
 currentDialog.children.at(-1).children[1].onclick();
 let resolveOld,resolveNew;responses.push(new Promise(resolve=>resolveOld=resolve));responses.push(new Promise(resolve=>resolveNew=resolve));
 const oldCheck=matrixCheckForNewerConfig(),newCheck=matrixCheckForNewerConfig();resolveNew(response(200,4));await newCheck;
 currentDialog.children.at(-1).children[1].onclick();resolveOld(response(200,3));await oldCheck;
 if(highestNotifiedRemoteRevision!==4||currentDialog)throw new Error('older delayed response was not superseded');
 matrixLoadedRevision=0;highestNotifiedRemoteRevision=0;matrixUpgradeReloadRequested=false;setMatrixRecoveryPending(false);
 configState.device.name='saved';matrixSavedSnapshot=matrixCurrentSnapshot();configState.device.name='local save';matrixDirty=true;document.activeElement=originalFocus;
 const submittedSnapshot=matrixCurrentSnapshot(),reloadsBeforeLocalSave=reloads;
 let resolveSave;responses.push(new Promise(resolve=>resolveSave=resolve));responses.push(response(200,1));responses.push(response(200,3));
 const localSave=saveConfig();await matrixCheckForNewerConfig();
 const localSaveDialog=currentDialog,localSaveDialogFocus=document.activeElement;
 if(!localSaveDialog||localSaveDialog.matrixRemoteRevision!==1||!matrixRecoveryPending||!saveButton.disabled)throw new Error('committed local revision was not observed during pending save');
 await matrixCheckForNewerConfig();
 if(currentDialog!==localSaveDialog||currentDialog.matrixRemoteRevision!==3||document.activeElement!==localSaveDialogFocus)throw new Error('later revision was forgotten while successful save response remained pending');
 resolveSave(response(200,1));const localSaveResult=await localSave;
 if(!localSaveResult.saved||currentDialog!==localSaveDialog||currentDialog.matrixRemoteRevision!==3||!matrixRecoveryPending||!saveButton.disabled)throw new Error('successful local save dismissed or ungated a later remote conflict');
 if(matrixLoadedRevision!==1||matrixSavedSnapshot!==submittedSnapshot||matrixCurrentSnapshot()!==submittedSnapshot||document.activeElement!==localSaveDialogFocus||reloads!==reloadsBeforeLocalSave)throw new Error('successful local save did not advance baseline/revision while preserving draft, focus, and reload state');
 currentDialog.children.at(-1).children[1].onclick();
 if(highestNotifiedRemoteRevision!==3)throw new Error('Remain did not acknowledge the latest revision retained by the successful-save dialog');
 matrixLoadedRevision=0;highestNotifiedRemoteRevision=0;matrixUpgradeReloadRequested=false;setMatrixRecoveryPending(false);
 configState.device.name='warning baseline';matrixSavedSnapshot=matrixCurrentSnapshot();configState.device.name='warning equality draft';matrixDirty=true;document.activeElement=originalFocus;
 const warningEqualitySnapshot=matrixCurrentSnapshot(),reloadsBeforeWarningEquality=reloads;
 let resolveWarningEquality;responses.push(new Promise(resolve=>resolveWarningEquality=resolve));responses.push(response(200,1));
 const warningEqualitySave=saveConfig();await matrixCheckForNewerConfig();
 if(!currentDialog||document.activeElement===originalFocus)throw new Error('warning equality poll did not focus its real dialog');
 resolveWarningEquality(response(503,null,{error:'Committed equality warning',committed:true,config:{revision:1}}));const warningEqualityResult=await warningEqualitySave;
 if(!warningEqualityResult.saved||warningEqualityResult.warning!=='Committed equality warning'||message.textContent!=='Committed equality warning')throw new Error('committed equality warning was not preserved');
 if(currentDialog||matrixRecoveryPending||saveButton.disabled||document.activeElement!==originalFocus)throw new Error('committed equality warning did not dismiss, ungate, and restore focus');
 if(matrixLoadedRevision!==1||matrixSavedSnapshot!==warningEqualitySnapshot||reloads!==reloadsBeforeWarningEquality)throw new Error('committed equality warning did not advance baseline/revision without reload');
 matrixLoadedRevision=0;highestNotifiedRemoteRevision=0;matrixUpgradeReloadRequested=false;setMatrixRecoveryPending(false);
 configState.device.name='warning newer baseline';matrixSavedSnapshot=matrixCurrentSnapshot();configState.device.name='warning newer draft';matrixDirty=true;document.activeElement=originalFocus;
 const warningNewerSnapshot=matrixCurrentSnapshot(),reloadsBeforeWarningNewer=reloads;
 let resolveWarningNewer;responses.push(new Promise(resolve=>resolveWarningNewer=resolve));responses.push(response(200,1));responses.push(response(200,3));
 const warningNewerSave=saveConfig();await matrixCheckForNewerConfig();const warningNewerDialog=currentDialog,warningNewerDialogFocus=document.activeElement;
 if(!warningNewerDialog||warningNewerDialog.matrixRemoteRevision!==1)throw new Error('committed-warning equality poll did not open its real dialog');
 await matrixCheckForNewerConfig();
 if(currentDialog!==warningNewerDialog||currentDialog.matrixRemoteRevision!==3||document.activeElement!==warningNewerDialogFocus)throw new Error('later revision was forgotten while committed-warning response remained pending');
 resolveWarningNewer(response(503,null,{error:'Committed newer warning',committed:true,config:{revision:1}}));const warningNewerResult=await warningNewerSave;
 if(!warningNewerResult.saved||warningNewerResult.warning!=='Committed newer warning'||message.textContent!=='Committed newer warning')throw new Error('committed genuinely-newer warning was not preserved');
 if(currentDialog!==warningNewerDialog||currentDialog.matrixRemoteRevision!==3||!matrixRecoveryPending||!saveButton.disabled||document.activeElement!==warningNewerDialogFocus)throw new Error('committed warning dismissed, ungated, or moved focus from a later poll dialog');
 if(matrixLoadedRevision!==1||matrixSavedSnapshot!==warningNewerSnapshot||matrixCurrentSnapshot()!==warningNewerSnapshot||reloads!==reloadsBeforeWarningNewer)throw new Error('committed newer warning did not advance baseline/revision while preserving draft and reload state');
 currentDialog.children.at(-1).children[1].onclick();
 if(highestNotifiedRemoteRevision!==3)throw new Error('Remain did not acknowledge the latest revision retained by the committed-warning dialog');
 matrixLoadedRevision=0;highestNotifiedRemoteRevision=0;matrixUpgradeReloadRequested=false;setMatrixRecoveryPending(false);
 configState.device.name='stale baseline';matrixSavedSnapshot=matrixCurrentSnapshot();configState.device.name='stale draft';matrixDirty=true;document.activeElement=originalFocus;
 const staleBaseline=matrixSavedSnapshot,staleDraft=matrixCurrentSnapshot(),reloadsBeforeStale=reloads;
 let resolveStale;responses.push(new Promise(resolve=>resolveStale=resolve));responses.push(response(200,2));
 const staleSave=saveConfig();await matrixCheckForNewerConfig();const staleDialog=currentDialog,staleDialogFocus=document.activeElement;
 resolveStale(response(412,null,{error:'Stale save',revision:2}));const staleResult=await staleSave;
 if(staleResult.saved||currentDialog!==staleDialog||!matrixRecoveryPending||!saveButton.disabled)throw new Error('stale save changed the poll-created recovery barrier');
 if(matrixLoadedRevision!==0||matrixSavedSnapshot!==staleBaseline||matrixCurrentSnapshot()!==staleDraft||document.activeElement!==staleDialogFocus||reloads!==reloadsBeforeStale)throw new Error('stale save changed base/baseline/draft/focus/reload state');
 currentDialog.children.at(-1).children[1].onclick();
 matrixSyncTimer=81;matrixStatusTimer=82;matrixFrameTimer=83;responses.push(response(401));await matrixCheckForNewerConfig();
 if(!matrixSyncStopped||redirect!=='/pair'||!clearedIntervals.includes(81)||!clearedIntervals.includes(82)||!clearedTimeouts.includes(83))throw new Error('authorization loss did not tear down polling');
 console.log('PAIRED_SYNC_STATE_MACHINE_PASS');
})().catch(error=>{console.error(error);process.exit(1)});
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], text=True, capture_output=True,
            cwd=Path(__file__).parents[1], check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PAIRED_SYNC_STATE_MACHINE_PASS", result.stdout)

    def test_two_paired_clients_recover_atomic_snapshots_and_reject_stale_save(self):
        self.pairing.verify.return_value = True
        client_a = self.app.test_client()
        client_b = self.app.test_client()
        self.assertEqual(client_a.post("/pair", data={"pin": "123456", "device_name": "A"}).status_code, 302)
        self.assertEqual(client_b.post("/pair", data={"pin": "123456", "device_name": "B"}).status_code, 302)

        base_a = client_a.get("/api/config").get_json()
        base_b = client_b.get("/api/config").get_json()
        module = {
            "id": "custom_sync", "name": "Created", "type": "text",
            "text": "PAIR", "font": "5x7", "colour": "#ffffff",
            "background": "#000000", "x": 2, "y": 20,
            "direction": "left", "speed": 0.05, "loops": 1,
        }
        created = client_a.post(
            "/api/config",
            json={
                "pairing": {"max_devices": 10},
                "custom_modules": [module],
                "playlist": [{
                    "module": "custom_sync", "duration": 15, "enabled": True,
                }],
            },
            headers={"If-Match": f'"{base_a["revision"]}"'},
        )
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        self.assertEqual(created.get_json()["revision"], 1)

        recovered = client_b.get(
            "/api/config", headers={"If-None-Match": f'"{base_b["revision"]}"'},
        )
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(recovered.get_json()["custom_modules"], [module])
        self.assertEqual(recovered.get_json()["playlist"], [{
            "module": "custom_sync", "duration": 15, "enabled": True,
        }])

        race_barrier = threading.Barrier(3)
        race_results = {}

        def race_save(label, client):
            race_barrier.wait()
            race_results[label] = client.post(
                "/api/config", json={"device": {"name": label}},
                headers={"If-Match": '"1"'},
            )

        race_threads = [
            threading.Thread(target=race_save, args=("Winner A", client_a)),
            threading.Thread(target=race_save, args=("Winner B", client_b)),
        ]
        for thread in race_threads:
            thread.start()
        race_barrier.wait()
        for thread in race_threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(response.status_code for response in race_results.values()), [200, 412])
        conflict = next(response for response in race_results.values() if response.status_code == 412)
        self.assertEqual(conflict.get_json()["revision"], 2)

        deleted = client_a.post(
            "/api/config", json={"custom_modules": [], "playlist": base_a["playlist"]},
            headers={"If-Match": '"2"'},
        )
        self.assertEqual(deleted.status_code, 200)
        missed = client_b.get("/api/config", headers={"If-None-Match": '"1"'})
        self.assertEqual(missed.get_json()["revision"], 3)
        self.assertEqual(missed.get_json()["custom_modules"], [])
        self.assertEqual(missed.get_json()["playlist"], base_a["playlist"])

        maximum_playlist = [
            {"module": "clock_lava", "duration": 1, "enabled": True}
            for _index in range(256)
        ]
        maximum = client_a.post(
            "/api/config", json={"playlist": maximum_playlist},
            headers={"If-Match": '"3"'},
        )
        self.assertEqual(maximum.status_code, 200, maximum.get_data(as_text=True))
        self.assertEqual(maximum.get_json()["revision"], 4)
        complete_snapshot = client_a.get("/api/config")
        self.assertEqual(len(complete_snapshot.get_json()["playlist"]), 256)
        self.assertLessEqual(len(complete_snapshot.data), 1_048_576)

        with client_b.session_transaction() as b_session:
            b_device_id = b_session["device_id"]
        revoked = client_a.delete(f"/api/devices/{b_device_id}")
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(client_b.get("/api/config").status_code, 401)
        self.assertEqual(
            client_b.post("/api/config", json={}, headers={"If-Match": '"4"'}).status_code,
            401,
        )

        polling_clients = [client_a]
        for index in range(9):
            client = self.app.test_client()
            self.assertEqual(
                client.post(
                    "/pair", data={"pin": "123456", "device_name": f"Poll client {index}"},
                ).status_code,
                302,
            )
            polling_clients.append(client)
        polling_device_ids = set()
        for client in polling_clients:
            with client.session_transaction() as polling_session:
                polling_device_ids.add(polling_session["device_id"])
        self.assertEqual(len(polling_device_ids), 10)

        response_sizes = []
        started = time.monotonic()
        for client in polling_clients:
            unchanged = client.get("/api/config", headers={"If-None-Match": '"4"'})
            self.assertEqual(unchanged.status_code, 304)
            response_sizes.append(len(unchanged.data))
        elapsed = time.monotonic() - started
        self.assertEqual(response_sizes, [0] * 10)
        self.assertLess(elapsed, 5.0)

    def _run_dashboard_chromium_probe(self, body, probe, timeout=30):
        chromium = os.environ["MATRIX_CHROMIUM"]
        node = shutil.which("node")
        assert node is not None
        with tempfile.TemporaryDirectory(prefix="matrix-sticky-preview-") as directory:
            page = Path(directory) / "dashboard.html"
            page.write_text(body)
            profile = Path(directory) / "profile"
            browser = subprocess.Popen(
                [
                    chromium, "--headless", "--no-sandbox", "--disable-gpu",
                    "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                active_port = profile / "DevToolsActivePort"
                for _attempt in range(50):
                    if active_port.is_file():
                        break
                    if browser.poll() is not None:
                        self.fail("Chromium exited before publishing its DevTools port")
                    time.sleep(0.1)
                self.assertTrue(active_port.is_file(), "Chromium DevTools port unavailable")
                completed = subprocess.run(
                    [node, "-e", probe, active_port.read_text().splitlines()[0], str(page)],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            finally:
                browser.terminate()
                try:
                    browser.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    browser.kill()
                    browser.wait(timeout=5)
                for cleanup_attempt in range(20):
                    try:
                        shutil.rmtree(profile)
                        break
                    except FileNotFoundError:
                        break
                    except OSError:
                        if cleanup_attempt == 19:
                            raise
                        time.sleep(0.05)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout.strip().splitlines()[-1])

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    @unittest.skipUnless(
        os.environ.get("MATRIX_CHROMIUM")
        and Path(os.environ.get("MATRIX_CHROMIUM", "")).is_file()
        and os.access(os.environ.get("MATRIX_CHROMIUM", ""), os.X_OK),
        "MATRIX_CHROMIUM browser is required",
    )
    def test_live_edit_segmented_control_updates_unsaved_preview_without_saving(self):
        self.pair_browser()
        before_config = Path(self.store.path).read_bytes()
        body = self.client.get("/").get_data(as_text=True)
        self.controller.reset_mock()
        probe = r"""
const port=process.argv[1],pagePath=process.argv[2];let endpoint;
for(let attempt=0;attempt<50;attempt++){try{endpoint=await fetch(`http://127.0.0.1:${port}/json/new`,{method:'PUT'}).then(r=>r.json());break}catch(_){await new Promise(r=>setTimeout(r,100))}}
if(!endpoint)throw new Error('Chromium DevTools endpoint unavailable');
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map();let nextId=1,loadResolve,loaded=new Promise(resolve=>loadResolve=resolve);
socket.onmessage=event=>{const message=JSON.parse(event.data);if(message.id&&pending.has(message.id)){const request=pending.get(message.id);pending.delete(message.id);message.error?request.reject(new Error(JSON.stringify(message.error))):request.resolve(message.result)}else if(message.method==='Page.loadEventFired')loadResolve()};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>{const id=nextId++;socket.send(JSON.stringify({id,method,params}));return new Promise((resolve,reject)=>pending.set(id,{resolve,reject}))};
const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value};
const waitFor=async expression=>{for(let attempt=0;attempt<100;attempt++){if(await evaluate(expression))return;await new Promise(r=>setTimeout(r,25))}throw new Error('waitFor timed out: '+expression)};
try{
 await send('Runtime.enable');await send('Page.enable');await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 await send('Page.addScriptToEvaluateOnNewDocument',{source:`
  globalThis.__requests=[];globalThis.__displayActive=0;globalThis.__displayMaxActive=0;
  const red='iVBORw0KGgoAAAANSUhEUgAAAEAAAAAgCAIAAAAt/+nTAAAAT0lEQVR4nO3PUQkAIBTAwFfBClawfzdD+HEIgwW4zdrn64YLGtCCBrSgAS1oQAsa0IIGtKABLWhACxrQgga0oAEtaEALGtCCBrSgAS147AJI7DAuPOp5lgAAAABJRU5ErkJggg==';
  const green='iVBORw0KGgoAAAANSUhEUgAAAEAAAAAgCAIAAAAt/+nTAAAAT0lEQVR4nO3PUQkAIBTAwBfOClawfwRD+HEIgwW4zdrn64YLGtCCBrSgAS1oQAsa0IIGtKABLWhACxrQgga0oAEtaEALGtCCBrSgAS147AIqPPhqwA8fegAAAABJRU5ErkJggg==';
  const blue='iVBORw0KGgoAAAANSUhEUgAAAEAAAAAgCAIAAAAt/+nTAAAAT0lEQVR4nO3PUQkAIBTAwNc/ihWsYChD+HEIgwW4zdrn64YLGtCCBrSgAS1oQAsa0IIGtKABLWhACxrQgga0oAEtaEALGtCCBrSgAS147ALszYkAB9CYOQAAAABJRU5ErkJggg==';
  const bytes=value=>Uint8Array.from(atob(value),character=>character.charCodeAt(0));
  globalThis.fetch=async(input,options={})=>{const url=String(input),method=options.method||'GET',record={url,method,body:options.body||null,time:performance.now()};__requests.push(record);
   if(url.startsWith('/api/display-frame')){__displayActive++;__displayMaxActive=Math.max(__displayMaxActive,__displayActive);try{if(__requests.filter(item=>item.url.startsWith('/api/display-frame')).length===1)await new Promise((resolve,reject)=>{const timer=setTimeout(resolve,3000);options.signal?.addEventListener('abort',()=>{clearTimeout(timer);reject(new DOMException('Aborted','AbortError'))},{once:true})});return new Response(new Blob([bytes(red)],{type:'image/png'}),{status:200,headers:{'X-Matrix-Frame':'live'}})}finally{__displayActive--}}
   if(url==='/api/config')return new Response(null,{status:304,headers:{'X-Matrix-Minimum-Client-Contract':'2'}});
   if(url.startsWith('/api/status'))return new Response(JSON.stringify({running:true,current_module:'weather',minimum_client_contract:2}),{status:200,headers:{'Content-Type':'application/json'}});
   if(url.startsWith('/api/edit-preview/')&&url!='/api/edit-preview/page'&&url!='/api/edit-preview/cancel'){const payload=JSON.parse(options.body);record.payload=payload;return new Response(JSON.stringify({session:String(payload.generation).padStart(32,'0'),generation:payload.generation,page:0,frames:[{png:green,delay_ms:60}],next_token:'a'.repeat(64),complete:false}),{status:200,headers:{'Content-Type':'application/json'}})}
   if(url==='/api/edit-preview/page'){const payload=JSON.parse(options.body),generation=Number(payload.session);record.payload=payload;return new Response(JSON.stringify({session:payload.session,generation,page:1,frames:[{png:blue,delay_ms:500}],next_token:null,complete:true}),{status:200,headers:{'Content-Type':'application/json'}})}
   if(url==='/api/edit-preview/cancel')return new Response(JSON.stringify({cancelled:true}),{status:200,headers:{'Content-Type':'application/json'}});
   return new Response(JSON.stringify({error:'fixture endpoint unavailable'}),{status:503,headers:{'Content-Type':'application/json'}})
  };
 `});
 loaded=new Promise(resolve=>loadResolve=resolve);await send('Page.navigate',{url:'file://'+pagePath});await Promise.race([loaded,new Promise((_,reject)=>setTimeout(()=>reject(new Error('load timeout')),5000))]);
 const initial=await evaluate(`(()=>{const group=document.querySelector('[role="radiogroup"]'),live=document.querySelector('#preview-mode-live'),edit=document.querySelector('#preview-mode-edit'),canvas=document.querySelector('#matrix-preview');return {canvasCount:document.querySelectorAll('#matrix-preview').length,groupBelow:Boolean(group)&&group.getBoundingClientRect().top>=canvas.getBoundingClientRect().bottom,labels:[live?.labels?.[0]?.textContent.trim()||null,edit?.labels?.[0]?.textContent.trim()||null],liveChecked:live?.checked??null,editChecked:edit?.checked??null,frame:document.querySelector('#preview-frame')?.textContent||null}})()`);
 await evaluate(`(()=>{addCustomModule();const selected=document.querySelector('#preview-select');selected.value='weather';selected.dispatchEvent(new Event('change',{bubbles:true}))})()`);
 const editPoint=await evaluate(`(()=>{const r=document.querySelector('label[for="preview-mode-edit"]')?.getBoundingClientRect();return r?{x:r.left+r.width/2,y:r.top+r.height/2}:null})()`);
 if(editPoint){await send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[editPoint]});await send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]})}
 await waitFor(`__requests.some(item=>item.url.startsWith('/api/edit-preview/weather'))`);
 await evaluate(`(()=>{const set=(selector,value)=>{const control=document.querySelector(selector);control.value=value;control.dispatchEvent(new Event('input',{bubbles:true}))};set('[data-built-in-module="weather"][data-setting="now_x"]','9');set('[data-built-in-module="weather"][data-setting="text_colour"]','#123456');set('#weather-scroll-speed','0.123');const text=document.querySelector('.custom-module-card [data-field="text"]');if(text){text.value='UNSAVED TEST';text.dispatchEvent(new Event('input',{bubbles:true}))}})()`);
 await waitFor(`__requests.filter(item=>item.url.startsWith('/api/edit-preview/weather')).length>=2`);
 await new Promise(r=>setTimeout(r,25));
 const firstEditPixel=await evaluate(`[...document.querySelector('#matrix-preview').getContext('2d').getImageData(0,0,1,1).data]`);
 await new Promise(r=>setTimeout(r,70));
 const edited=await evaluate(`(()=>{const requests=__requests.filter(item=>item.url.startsWith('/api/edit-preview/weather')),pageRequests=__requests.filter(item=>item.url==='/api/edit-preview/page'),latest=requests.at(-1).payload,pixel=[...document.querySelector('#matrix-preview').getContext('2d').getImageData(0,0,1,1).data],forbidden=__requests.filter(item=>item.method==='POST'&&(item.url==='/api/config'||item.url.startsWith('/api/preview/')||item.url.includes('/api/media/')&&item.url.endsWith('/preview')||item.url.startsWith('/api/control/')));return {instance:latest?.preview_instance||null,generations:requests.map(item=>item.payload.generation),modulePaths:requests.map(item=>item.url),pagePayloads:pageRequests.map(item=>item.payload),latest,pixel,label:document.querySelector('#preview-module').textContent,forbidden:forbidden.map(item=>item.url),canvasCount:document.querySelectorAll('#matrix-preview').length}})()`);
 edited.firstPixel=firstEditPixel;
 await evaluate(`document.documentElement.style.scrollBehavior='auto';scrollTo(0,document.documentElement.scrollHeight);syncPreviewPopout()`);await new Promise(r=>setTimeout(r,50));
 const poppedLabel=await evaluate(`document.querySelector('#preview-module').textContent`);
 const livePoint=await evaluate(`(()=>{const r=document.querySelector('label[for="preview-mode-live"]').getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})()`);
 await send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[livePoint]});await send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
 await waitFor(`document.querySelector('.edit-preview-switch-dialog')?.open`);await evaluate(`document.querySelector('.edit-preview-switch-dialog button').click();matrixDirty=false;matrixSavedSnapshot=matrixCurrentSnapshot();document.querySelector('#preview-mode-live').click()`);
 await waitFor(`__requests.filter(item=>item.url.startsWith('/api/display-frame')).length>=2`);await new Promise(r=>setTimeout(r,180));
 const restored=await evaluate(`(()=>({live:document.querySelector('#preview-mode-live').checked,label:document.querySelector('#preview-module').textContent,cancels:__requests.filter(item=>item.url==='/api/edit-preview/cancel').length,displayRequests:__requests.filter(item=>item.url.startsWith('/api/display-frame')).length,maxDisplayActive:__displayMaxActive,editTimers:globalThis.editPreviewAnimationTimer??null,canvasCount:document.querySelectorAll('#matrix-preview').length}))()`);
 console.log(JSON.stringify({initial,edited,poppedLabel,restored}));
}finally{socket.close()}
"""
        report = self._run_dashboard_chromium_probe(body, probe)
        self.assertEqual(report["initial"]["canvasCount"], 1, report)
        self.assertTrue(report["initial"]["groupBelow"], report)
        self.assertEqual(report["initial"]["labels"], ["Live", "Edit Preview"], report)
        self.assertTrue(report["initial"]["liveChecked"], report)
        self.assertFalse(report["initial"]["editChecked"], report)
        self.assertIn("Live", report["initial"]["frame"])
        self.assertRegex(report["edited"]["instance"], r"^[0-9a-f]{32}$")
        self.assertGreaterEqual(len(report["edited"]["generations"]), 2, report)
        self.assertEqual(report["edited"]["generations"], sorted(set(report["edited"]["generations"])), report)
        self.assertGreaterEqual(len(report["edited"]["pagePayloads"]), 2, report)
        self.assertTrue(all(set(payload) == {"session", "preview_instance", "token"} for payload in report["edited"]["pagePayloads"]), report)
        latest = report["edited"]["latest"]
        self.assertEqual(latest["config"]["weather"]["now_x"], 9)
        self.assertEqual(latest["config"]["weather"]["text_colour"], "#123456")
        self.assertEqual(latest["config"]["weather"]["scroll_speed"], 0.123)
        self.assertEqual(latest["config"]["custom_modules"][0]["text"], "UNSAVED TEST")
        self.assertEqual(report["edited"]["firstPixel"], [68, 85, 102, 255], report)
        self.assertEqual(report["edited"]["pixel"], [170, 187, 204, 255], report)
        self.assertIn("Unsaved draft · weather", report["edited"]["label"])
        self.assertIn("Unsaved draft · weather", report["poppedLabel"])
        self.assertEqual(report["edited"]["forbidden"], [], report)
        self.assertEqual(report["edited"]["canvasCount"], 1, report)
        self.assertTrue(report["restored"]["live"], report)
        self.assertIn("Live", report["restored"]["label"])
        self.assertGreaterEqual(report["restored"]["cancels"], 1, report)
        self.assertLessEqual(report["restored"]["maxDisplayActive"], 1, report)
        self.assertIsNone(report["restored"]["editTimers"], report)
        self.assertEqual(report["restored"]["canvasCount"], 1, report)
        self.assertEqual(Path(self.store.path).read_bytes(), before_config)
        self.assertEqual(self.controller.method_calls, [])

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    @unittest.skipUnless(
        os.environ.get("MATRIX_CHROMIUM")
        and Path(os.environ.get("MATRIX_CHROMIUM", "")).is_file()
        and os.access(os.environ.get("MATRIX_CHROMIUM", ""), os.X_OK),
        "MATRIX_CHROMIUM browser is required",
    )
    def test_edit_preview_last_request_wins_and_renders_one_selected_lava_palette(self):
        self.store.update({"lava": {
            "enabled_palettes": ["fire_burst"],
            "custom_palettes": [{
                "id": "palette_sunset",
                "name": "Saved sunset",
                "colours": ["#101020", "#ff6600", "#ffee99"],
                "enabled": True,
            }],
        }})
        self.pair_browser()
        before_config = Path(self.store.path).read_bytes()
        body = self.client.get("/").get_data(as_text=True)
        self.controller.reset_mock()
        probe = r"""
const port=process.argv[1],pagePath=process.argv[2];let endpoint;
for(let attempt=0;attempt<50;attempt++){try{endpoint=await fetch(`http://127.0.0.1:${port}/json/new`,{method:'PUT'}).then(r=>r.json());break}catch(_){await new Promise(r=>setTimeout(r,100))}}
if(!endpoint)throw new Error('Chromium DevTools endpoint unavailable');
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map();let nextId=1,loadResolve,loaded=new Promise(resolve=>loadResolve=resolve);
socket.onmessage=event=>{const message=JSON.parse(event.data);if(message.id&&pending.has(message.id)){const request=pending.get(message.id);pending.delete(message.id);message.error?request.reject(new Error(JSON.stringify(message.error))):request.resolve(message.result)}else if(message.method==='Page.loadEventFired')loadResolve()};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>{const id=nextId++;socket.send(JSON.stringify({id,method,params}));return new Promise((resolve,reject)=>pending.set(id,{resolve,reject}))};
const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value};
const waitFor=async expression=>{for(let attempt=0;attempt<160;attempt++){if(await evaluate(expression))return;await new Promise(r=>setTimeout(r,25))}throw new Error('waitFor timed out: '+expression)};
try{
 await send('Runtime.enable');await send('Page.enable');
 await send('Page.addScriptToEvaluateOnNewDocument',{source:`
  globalThis.__requests=[];globalThis.__aborted=[];globalThis.__errors=[];
  addEventListener('error',event=>__errors.push(String(event.error||event.message)));addEventListener('unhandledrejection',event=>__errors.push(String(event.reason)));
  const red='iVBORw0KGgoAAAANSUhEUgAAAEAAAAAgCAIAAAAt/+nTAAAAT0lEQVR4nO3PUQkAIBTAwFfBClawfzdD+HEIgwW4zdrn64YLGtCCBrSgAS1oQAsa0IIGtKABLWhACxrQgga0oAEtaEALGtCCBrSgAS147AJI7DAuPOp5lgAAAABJRU5ErkJggg==';
  const green='iVBORw0KGgoAAAANSUhEUgAAAEAAAAAgCAIAAAAt/+nTAAAAT0lEQVR4nO3PUQkAIBTAwBfOClawfwRD+HEIgwW4zdrn64YLGtCCBrSgAS1oQAsa0IIGtKABLWhACxrQgga0oAEtaEALGtCCBrSgAS147AIqPPhqwA8fegAAAABJRU5ErkJggg==';
  const blue='iVBORw0KGgoAAAANSUhEUgAAAEAAAAAgCAIAAAAt/+nTAAAAT0lEQVR4nO3PUQkAIBTAwNc/ihWsYChD+HEIgwW4zdrn64YLGtCCBrSgAS1oQAsa0IIGtKABLWhACxrQgga0oAEtaEALGtCCBrSgAS147ALszYkAB9CYOQAAAABJRU5ErkJggg==';
  const bytes=value=>Uint8Array.from(atob(value),character=>character.charCodeAt(0));
  globalThis.fetch=async(input,options={})=>{const url=String(input),method=options.method||'GET',record={url,method,body:options.body||null};__requests.push(record);
   if(url.startsWith('/api/display-frame'))return new Response(new Blob([bytes(red)],{type:'image/png'}),{status:200,headers:{'X-Matrix-Frame':'live'}});
   if(url==='/api/config')return new Response(null,{status:304,headers:{'X-Matrix-Minimum-Client-Contract':'2'}});
   if(url.startsWith('/api/status'))return new Response(JSON.stringify({running:true,current_module:'clock_lava',minimum_client_contract:2}),{status:200,headers:{'Content-Type':'application/json'}});
   if(url.startsWith('/api/edit-preview/')&&url!='/api/edit-preview/page'&&url!='/api/edit-preview/cancel'){
    const payload=JSON.parse(options.body),descriptor=payload.selected_lava_palette;record.payload=payload;
    if(descriptor?.id==='fire_burst')await new Promise((resolve,reject)=>{const timer=setTimeout(resolve,350);options.signal?.addEventListener('abort',()=>{clearTimeout(timer);record.aborted=true;__aborted.push(descriptor.id);reject(new DOMException('Aborted','AbortError'))},{once:true})});
    if(url.endsWith('/weather')&&payload.config.weather.now_x===13)return new Response(JSON.stringify({error:'provider unavailable'}),{status:503,headers:{'Content-Type':'application/json'}});
    const frames=descriptor?.kind==='custom'?[{png:blue,delay_ms:35},{png:green,delay_ms:80}]:[{png:green,delay_ms:35},{png:blue,delay_ms:80}];
    return new Response(JSON.stringify({session:String(payload.generation).padStart(32,'0'),generation:payload.generation,page:0,frames,next_token:null,complete:true}),{status:200,headers:{'Content-Type':'application/json'}})
   }
   if(url==='/api/edit-preview/cancel')return new Response(JSON.stringify({cancelled:true}),{status:200,headers:{'Content-Type':'application/json'}});
   return new Response(JSON.stringify({status:'fresh',frames_per_loop:10,settings_correlation:'fixture',milliseconds:1000,refresh_at:'2099-01-01T00:00:00Z'}),{status:200,headers:{'Content-Type':'application/json'}})
  };
 `});
 loaded=new Promise(resolve=>loadResolve=resolve);await send('Page.navigate',{url:'file://'+pagePath});await Promise.race([loaded,new Promise((_,reject)=>setTimeout(()=>reject(new Error('load timeout')),5000))]);
 const initial=await evaluate(`(()=>{const radios=[...document.querySelectorAll('input[name="edit-preview-lava-palette"]')];return {count:radios.length,labels:radios.map(r=>r.labels?.[0]?.textContent.trim()),selected:radios.filter(r=>r.checked).map(r=>r.value)}})()`);
 if(initial.count){
  await evaluate(`(()=>{const select=document.querySelector('#preview-select');select.value='clock_lava';select.dispatchEvent(new Event('change',{bubbles:true}));const edit=document.querySelector('#preview-mode-edit');edit.checked=true;edit.dispatchEvent(new Event('change',{bubbles:true}))})()`);
  await waitFor(`__requests.some(r=>r.payload?.selected_lava_palette?.id==='fire_burst')`);
  const savedBefore=await evaluate(`JSON.stringify(configBody())`),dirtyBefore=await evaluate(`matrixDirty`);
  await evaluate(`(()=>{const radio=[...document.querySelectorAll('input[name="edit-preview-lava-palette"]')].find(r=>r.value==='built_in:ember_glow');radio.focus();radio.checked=true;radio.dispatchEvent(new Event('change',{bubbles:true}))})()`);
  await waitFor(`__requests.some(r=>r.payload?.selected_lava_palette?.id==='ember_glow')`);await new Promise(r=>setTimeout(r,120));
  const builtIn=await evaluate(`(()=>{const requests=__requests.filter(r=>r.payload?.selected_lava_palette),latest=requests.at(-1);return {descriptor:latest.payload.selected_lava_palette,enabled:document.querySelector('.lava-palette[value="ember_glow"]').checked,savedSame:JSON.stringify(configBody())===${JSON.stringify(savedBefore)},dirtySame:matrixDirty===${JSON.stringify(dirtyBefore)},pixel:[...document.querySelector('#matrix-preview').getContext('2d').getImageData(0,0,1,1).data],selected:[...document.querySelectorAll('input[name="edit-preview-lava-palette"]:checked')].map(r=>r.value),aborted:[...__aborted]}})()`);
  await evaluate(`(()=>{const radio=[...document.querySelectorAll('input[name="edit-preview-lava-palette"]')].find(r=>r.value==='custom:palette_sunset');radio.checked=true;radio.dispatchEvent(new Event('change',{bubbles:true}));const colours=['#112233','#445566','#778899'];document.querySelectorAll('.custom-lava-card[data-id="palette_sunset"] [data-colour]').forEach((input,index)=>{input.value=colours[index];input.dispatchEvent(new Event('input',{bubbles:true}))})})()`);
  await waitFor(`__requests.some(r=>JSON.stringify(r.payload?.selected_lava_palette?.colours)==='["#112233","#445566","#778899"]')`);await new Promise(r=>setTimeout(r,45));
  const customPixels=[];for(let sample=0;sample<30;sample++){customPixels.push(await evaluate(`[...document.querySelector('#matrix-preview').getContext('2d').getImageData(0,0,1,1).data]`));await new Promise(r=>setTimeout(r,15))}
  const custom=await evaluate(`(()=>{const request=__requests.filter(r=>r.payload?.selected_lava_palette?.kind==='custom').at(-1),radio=document.querySelector('input[name="edit-preview-lava-palette"]:checked');radio.focus();renderCustomLavaPalettes();const rerendered=document.querySelector('input[name="edit-preview-lava-palette"]:checked');return {descriptor:request.payload.selected_lava_palette,pixels:${JSON.stringify(customPixels)},selected:rerendered?.value,focus:document.activeElement===rerendered,configHasDescriptor:Object.hasOwn(configBody(),'selected_lava_palette')}})()`);
  const modules=['weather','openweather_overview','openweather_forecast','splash','sleep','announcement_board'];
  for(const module of modules){await evaluate(`(()=>{const select=document.querySelector('#preview-select');select.value='${module}';select.dispatchEvent(new Event('change',{bubbles:true}))})()`);await waitFor(`__requests.some(r=>r.url.endsWith('/${module}')&&r.payload)`)}
  await evaluate(`(()=>{const select=document.querySelector('#preview-select');select.value='weather';select.dispatchEvent(new Event('change',{bubbles:true}))})()`);await waitFor(`__requests.filter(r=>r.url.endsWith('/weather')&&r.payload).length>=2`);await new Promise(r=>setTimeout(r,45));
  const validPixel=await evaluate(`[...document.querySelector('#matrix-preview').getContext('2d').getImageData(0,0,1,1).data]`);
  await evaluate(`(()=>{const x=document.querySelector('[data-built-in-module="weather"][data-setting="now_x"]');x.value='13';x.dispatchEvent(new Event('input',{bubbles:true}))})()`);await waitFor(`document.querySelector('#preview-frame').textContent.includes('Draft error')`);
  const errorPixel=await evaluate(`[...document.querySelector('#matrix-preview').getContext('2d').getImageData(0,0,1,1).data]`),errorAccessible=await evaluate(`document.querySelector('#preview-frame').getAttribute('role')==='alert'`);
  await evaluate(`(()=>{const x=document.querySelector('[data-built-in-module="weather"][data-setting="now_x"]');x.value='14';x.dispatchEvent(new Event('input',{bubbles:true}))})()`);await waitFor(`document.querySelector('#preview-frame').textContent.includes('Unsaved draft frame')`);
  const final=await evaluate(`(()=>({validPixel:${JSON.stringify(validPixel)},errorPixel:${JSON.stringify(errorPixel)},recoveredPixel:[...document.querySelector('#matrix-preview').getContext('2d').getImageData(0,0,1,1).data],errorAccessible:${JSON.stringify(errorAccessible)},paths:[...new Set(__requests.filter(r=>r.payload).map(r=>r.url))],forbidden:__requests.filter(r=>r.method==='POST'&&(r.url==='/api/config'||r.url.startsWith('/api/preview/')||r.url.startsWith('/api/control/')||r.url.includes('/preview'))&&!r.url.startsWith('/api/edit-preview/')).map(r=>r.url),errors:[...__errors],canvasCount:document.querySelectorAll('#matrix-preview').length}))()`);
  console.log(JSON.stringify({initial,builtIn,custom,final}));
 }else console.log(JSON.stringify({initial}));
}finally{socket.close()}
"""
        report = self._run_dashboard_chromium_probe(body, probe)
        self.assertGreater(report["initial"]["count"], 1, report)
        self.assertIn("Inspect fire burst in Edit Preview", report["initial"]["labels"])
        self.assertIn("Inspect Saved sunset in Edit Preview", report["initial"]["labels"])
        self.assertEqual(report["initial"]["selected"], [], report)
        self.assertEqual(report["builtIn"]["descriptor"], {"kind": "built_in", "id": "ember_glow"})
        self.assertFalse(report["builtIn"]["enabled"], report)
        self.assertTrue(report["builtIn"]["savedSame"], report)
        self.assertTrue(report["builtIn"]["dirtySame"], report)
        self.assertEqual(report["builtIn"]["selected"], ["built_in:ember_glow"], report)
        self.assertIn("fire_burst", report["builtIn"]["aborted"], report)
        self.assertEqual(report["custom"]["descriptor"], {
            "kind": "custom", "id": "palette_sunset",
            "colours": ["#112233", "#445566", "#778899"],
        })
        self.assertGreater(len({tuple(pixel) for pixel in report["custom"]["pixels"]}), 1, report)
        self.assertEqual(report["custom"]["selected"], "custom:palette_sunset", report)
        self.assertTrue(report["custom"]["focus"], report)
        self.assertFalse(report["custom"]["configHasDescriptor"], report)
        self.assertEqual(report["final"]["validPixel"], report["final"]["errorPixel"], report)
        self.assertNotEqual(report["final"]["errorPixel"], report["final"]["recoveredPixel"], report)
        self.assertTrue(report["final"]["errorAccessible"], report)
        for module in ("weather", "openweather_overview", "openweather_forecast", "splash", "sleep", "announcement_board"):
            self.assertIn(f"/api/edit-preview/{module}", report["final"]["paths"], report)
        self.assertEqual(report["final"]["forbidden"], [], report)
        self.assertEqual(report["final"]["errors"], [], report)
        self.assertEqual(report["final"]["canvasCount"], 1, report)
        self.assertEqual(Path(self.store.path).read_bytes(), before_config)
        self.assertEqual(self.controller.method_calls, [])

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    @unittest.skipUnless(
        os.environ.get("MATRIX_CHROMIUM")
        and Path(os.environ.get("MATRIX_CHROMIUM", "")).is_file()
        and os.access(os.environ.get("MATRIX_CHROMIUM", ""), os.X_OK),
        "MATRIX_CHROMIUM browser is required",
    )
    def test_malformed_edit_preview_continuation_cancels_session_and_recovers(self):
        self.pair_browser()
        before_config = Path(self.store.path).read_bytes()
        body = self.client.get("/").get_data(as_text=True)
        self.controller.reset_mock()
        probe = r"""
const port=process.argv[1],pagePath=process.argv[2];let endpoint;
for(let attempt=0;attempt<50;attempt++){try{endpoint=await fetch(`http://127.0.0.1:${port}/json/new`,{method:'PUT'}).then(r=>r.json());break}catch(_){await new Promise(r=>setTimeout(r,50))}}
if(!endpoint)throw new Error('Chromium DevTools endpoint unavailable');
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map();let nextId=1,resolveLoad,loaded=new Promise(resolve=>resolveLoad=resolve);
socket.onmessage=event=>{const message=JSON.parse(event.data);if(message.id&&pending.has(message.id)){const request=pending.get(message.id);pending.delete(message.id);message.error?request.reject(new Error(JSON.stringify(message.error))):request.resolve(message.result)}else if(message.method==='Page.loadEventFired')resolveLoad()};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>{const id=nextId++;socket.send(JSON.stringify({id,method,params}));return new Promise((resolve,reject)=>pending.set(id,{resolve,reject}))};
const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value};
const waitFor=async expression=>{for(let attempt=0;attempt<160;attempt++){if(await evaluate(expression))return;await new Promise(resolve=>setTimeout(resolve,25))}throw new Error('waitFor timed out: '+expression)};
try{
 await send('Runtime.enable');await send('Page.enable');await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 await send('Page.addScriptToEvaluateOnNewDocument',{source:`
  globalThis.__requests=[];globalThis.__errors=[];globalThis.__starts=0;globalThis.__variant='base64';globalThis.__session=null;
  addEventListener('error',event=>__errors.push(String(event.error||event.message)));addEventListener('unhandledrejection',event=>__errors.push(String(event.reason)));
  const png='iVBORw0KGgoAAAANSUhEUgAAAEAAAAAgCAIAAAAt/+nTAAAAT0lEQVR4nO3PUQkAIBTAwBfOClawfwRD+HEIgwW4zdrn64YLGtCCBrSgAS1oQAsa0IIGtKABLWhACxrQgga0oAEtaEALGtCCBrSgAS147AIqPPhqwA8fegAAAABJRU5ErkJggg==',bytes=Uint8Array.from(atob(png),c=>c.charCodeAt(0));
  globalThis.fetch=async(input,options={})=>{const url=String(input),method=options.method||'GET',payload=options.body?JSON.parse(options.body):null;__requests.push({url,method,payload});
   if(url.startsWith('/api/display-frame'))return new Response(new Blob([bytes],{type:'image/png'}));
   if(url==='/api/edit-preview/cancel')return new Response(JSON.stringify({cancelled:true}),{headers:{'Content-Type':'application/json'}});
   if(url==='/api/edit-preview/page'){const page=__variant==='page'?{session:__session,generation:payload.generation??__starts,page:1,frames:null,next_token:7,complete:false}:{session:__session,generation:payload.generation??__starts,page:1,frames:[{png:__variant==='base64'?'%%% malformed base64 %%%':'bm90IGEgcG5n',delay_ms:20}],next_token:'b'.repeat(64),complete:false};return new Response(JSON.stringify(page),{headers:{'Content-Type':'application/json'}})}
   if(url.startsWith('/api/edit-preview/')){__starts++;const generation=payload.generation,fails=__variant!=='valid';__session=(__starts%10).toString().repeat(32);return new Response(JSON.stringify({session:__session,generation,page:0,frames:[{png,delay_ms:10000}],next_token:fails?'a'.repeat(64):null,complete:!fails}),{headers:{'Content-Type':'application/json'}})}
   if(url==='/api/config')return new Response(null,{status:304,headers:{'X-Matrix-Minimum-Client-Contract':'2'}});
   return new Response(JSON.stringify({running:true,current_module:'weather',minimum_client_contract:2}),{headers:{'Content-Type':'application/json'}})
  };
 `});
 loaded=new Promise(resolve=>resolveLoad=resolve);await send('Page.navigate',{url:'file://'+pagePath});await loaded;
 await evaluate(`document.querySelector('#preview-mode-edit').click()`);await waitFor(`document.querySelector('#preview-frame').textContent.includes('Draft error')`);await waitFor(`__requests.filter(request=>request.url==='/api/edit-preview/cancel').length===1`);
 const captureFailure=()=>evaluate(`({session:editPreviewSession,token:editPreviewToken,timerActive:editPreviewAnimationTimer!==null,cancelSessions:__requests.filter(request=>request.url==='/api/edit-preview/cancel').map(request=>request.payload.session),pageRequests:__requests.filter(request=>request.url==='/api/edit-preview/page').length,pixel:[...document.querySelector('#matrix-preview').getContext('2d').getImageData(0,0,1,1).data],mode:previewMode,status:document.querySelector('#preview-frame').textContent})`);
 const failures=[await captureFailure()];
 await evaluate(`__variant='valid';startEditPreview()`);await waitFor(`__starts>=2&&document.querySelector('#preview-frame').textContent.includes('Unsaved draft frame')`);
 await evaluate(`__variant='png';startEditPreview()`);await waitFor(`document.querySelector('#preview-frame').textContent.includes('Draft error')&&__requests.filter(request=>request.url==='/api/edit-preview/cancel'&&request.payload.session==='3'.repeat(32)).length===1`);failures.push(await captureFailure());
 await evaluate(`__variant='valid';startEditPreview()`);await waitFor(`__starts>=4&&document.querySelector('#preview-frame').textContent.includes('Unsaved draft frame')`);
 await evaluate(`__variant='page';startEditPreview()`);await waitFor(`document.querySelector('#preview-frame').textContent.includes('Draft error')&&__requests.filter(request=>request.url==='/api/edit-preview/cancel'&&request.payload.session==='5'.repeat(32)).length===1`);failures.push(await captureFailure());
 await evaluate(`__variant='valid';startEditPreview()`);await waitFor(`__starts>=6&&document.querySelector('#preview-frame').textContent.includes('Unsaved draft frame')`);
 const recovered=await evaluate(`({starts:__starts,session:editPreviewSession,status:document.querySelector('#preview-frame').textContent,errors:[...__errors],canvasCount:document.querySelectorAll('#matrix-preview').length})`);
 console.log(JSON.stringify({failures,recovered}));
}finally{socket.close()}
"""
        report = self._run_dashboard_chromium_probe(body, probe)
        self.assertEqual(len(report["failures"]), 3, report)
        for index, failed in enumerate(report["failures"]):
            self.assertEqual(failed["session"], None, report)
            self.assertEqual(failed["token"], None, report)
            self.assertFalse(failed["timerActive"], report)
            self.assertEqual(failed["pageRequests"], index + 1, report)
            self.assertEqual(failed["mode"], "edit", report)
            self.assertIn("Draft error", failed["status"])
            self.assertGreater(failed["pixel"][3], 0, report)
            self.assertIn(str(index * 2 + 1) * 32, failed["cancelSessions"], report)
        self.assertEqual(report["recovered"]["starts"], 6, report)
        self.assertEqual(report["recovered"]["session"], "6" * 32, report)
        self.assertIn("Unsaved draft frame", report["recovered"]["status"])
        self.assertEqual(report["recovered"]["errors"], [], report)
        self.assertEqual(report["recovered"]["canvasCount"], 1, report)
        self.assertEqual(Path(self.store.path).read_bytes(), before_config)
        self.assertEqual(self.controller.method_calls, [])

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    @unittest.skipUnless(
        os.environ.get("MATRIX_CHROMIUM")
        and Path(os.environ.get("MATRIX_CHROMIUM", "")).is_file()
        and os.access(os.environ.get("MATRIX_CHROMIUM", ""), os.X_OK),
        "MATRIX_CHROMIUM browser is required",
    )
    def test_edit_preview_switch_away_requires_exact_unsaved_choice(self):
        self.pair_browser()
        before_config = Path(self.store.path).read_bytes()
        body = self.client.get("/").get_data(as_text=True)
        self.controller.reset_mock()
        probe = r"""
const port=process.argv[1],pagePath=process.argv[2];let endpoint;
for(let attempt=0;attempt<50;attempt++){try{endpoint=await fetch(`http://127.0.0.1:${port}/json/new`,{method:'PUT'}).then(r=>r.json());break}catch(_){await new Promise(r=>setTimeout(r,100))}}
if(!endpoint)throw new Error('Chromium DevTools endpoint unavailable');
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map();let nextId=1,loadCount=0;
socket.onmessage=event=>{const message=JSON.parse(event.data);if(message.id&&pending.has(message.id)){const request=pending.get(message.id);pending.delete(message.id);message.error?request.reject(new Error(JSON.stringify(message.error))):request.resolve(message.result)}else if(message.method==='Page.loadEventFired')loadCount++};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>{const id=nextId++;socket.send(JSON.stringify({id,method,params}));return new Promise((resolve,reject)=>pending.set(id,{resolve,reject}))};
const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value};
const waitFor=async expression=>{for(let attempt=0;attempt<120;attempt++){if(await evaluate(expression))return;await new Promise(r=>setTimeout(r,25))}throw new Error('waitFor timed out: '+expression)};
try{
 await send('Runtime.enable');await send('Page.enable');await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 await send('Page.addScriptToEvaluateOnNewDocument',{source:`
  globalThis.__requests=[];globalThis.__errors=[];
  addEventListener('error',event=>__errors.push(String(event.error||event.message)));addEventListener('unhandledrejection',event=>__errors.push(String(event.reason)));
  const png='iVBORw0KGgoAAAANSUhEUgAAAEAAAAAgCAIAAAAt/+nTAAAAT0lEQVR4nO3PUQkAIBTAwBfOClawfwRD+HEIgwW4zdrn64YLGtCCBrSgAS1oQAsa0IIGtKABLWhACxrQgga0oAEtaEALGtCCBrSgAS147AIqPPhqwA8fegAAAABJRU5ErkJggg==';
  const bytes=Uint8Array.from(atob(png),c=>c.charCodeAt(0));
  globalThis.fetch=async(input,options={})=>{const url=String(input),method=options.method||'GET';__requests.push({url,method});
   if(url.startsWith('/api/display-frame'))return new Response(new Blob([bytes],{type:'image/png'}),{status:200,headers:{'X-Matrix-Frame':'saved'}});
   if(url.startsWith('/api/edit-preview/')&&url!='/api/edit-preview/cancel')return new Response(JSON.stringify({session:'1'.repeat(32),generation:1,page:0,frames:[{png,delay_ms:1000}],next_token:null,complete:true}),{status:200,headers:{'Content-Type':'application/json'}});
   if(url==='/api/edit-preview/cancel')return new Response(JSON.stringify({cancelled:true}),{status:200,headers:{'Content-Type':'application/json'}});
   if(url==='/api/config')return new Response(null,{status:304,headers:{'X-Matrix-Minimum-Client-Contract':'2'}});
   return new Response(JSON.stringify({running:true,current_module:'weather',minimum_client_contract:2}),{status:200,headers:{'Content-Type':'application/json'}})
  };
 `});
 await send('Page.navigate',{url:'file://'+pagePath});await waitFor(`document.readyState==='complete'`);
 await evaluate(`document.querySelector('#preview-mode-edit').click()`);await waitFor(`previewMode==='edit'`);await evaluate(`document.querySelector('#preview-mode-live').click()`);
 const clean=await evaluate(`({mode:previewMode,live:document.querySelector('#preview-mode-live').checked,dialogs:document.querySelectorAll('dialog[open]').length})`);
 await evaluate(`document.querySelector('#preview-mode-edit').click();(()=>{const input=document.querySelector('#device-name');input.value='UNSAVED SWITCH SENTINEL';input.dispatchEvent(new Event('input',{bubbles:true}));input.focus();input.setSelectionRange(2,8);document.querySelector('.palette-grid').scrollTop=31;const palette=document.querySelector('input[name="edit-preview-lava-palette"]');if(palette){palette.checked=true;palette.dispatchEvent(new Event('change',{bubbles:true}))}document.querySelector('#preview-mode-live').click()})()`);
 const opened=await evaluate(`(()=>{const dialog=document.querySelector('.edit-preview-switch-dialog');return {mode:previewMode,edit:document.querySelector('#preview-mode-edit').checked,buttons:[...dialog?.querySelectorAll('button')||[]].map(b=>b.textContent),count:document.querySelectorAll('.edit-preview-switch-dialog').length,open:dialog?.open||false}})()`);
 if(opened.open){
 await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape'});await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape'});
 const escaped=await evaluate(`document.querySelector('.edit-preview-switch-dialog')?.open||false`);
 await evaluate(`(()=>{const buttons=[...document.querySelectorAll('.edit-preview-switch-dialog button')];buttons.at(-1).focus()})()`);await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Tab',code:'Tab'});await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Tab',code:'Tab'});
 const trapped=await evaluate(`document.activeElement===document.querySelector('.edit-preview-switch-dialog button')`);
 await evaluate(`document.querySelector('.edit-preview-switch-dialog button').click()`);
 const stayed=await evaluate(`(()=>{const input=document.querySelector('#device-name'),palette=document.querySelector('input[name="edit-preview-lava-palette"]:checked');return {mode:previewMode,value:input.value,focus:document.activeElement===input,selection:[input.selectionStart,input.selectionEnd],palette:palette?.value||null,scroll:document.querySelector('.palette-grid').scrollTop,dialog:Boolean(document.querySelector('.edit-preview-switch-dialog'))}})()`);
 await evaluate(`showMatrixRevisionDialog(matrixLoadedRevision+1);document.querySelector('#preview-mode-live').click()`);
 const barrier=await evaluate(`({revision:document.querySelectorAll('.config-revision-dialog[open]').length,switches:document.querySelectorAll('.edit-preview-switch-dialog').length,saveDisabled:document.querySelector('#save-config').disabled,mode:previewMode})`);
 await evaluate(`document.querySelector('.config-revision-dialog .config-upgrade-actions button:last-child').click();document.querySelector('#preview-mode-live').click()`);
 await waitFor(`document.querySelector('.edit-preview-switch-dialog')?.open`);
 const beforeReload=loadCount;await evaluate(`document.querySelector('.edit-preview-switch-dialog .discard-edit-preview').click()`);
 for(let attempt=0;attempt<120&&loadCount===beforeReload;attempt++)await new Promise(r=>setTimeout(r,25));
 await waitFor(`document.readyState==='complete'&&typeof previewMode==='string'`);
 const discarded=await evaluate(`({mode:previewMode,live:document.querySelector('#preview-mode-live').checked,value:document.querySelector('#device-name').value,canvasCount:document.querySelectorAll('#matrix-preview').length,forbidden:__requests.filter(r=>r.method==='POST'&&(r.url==='/api/config'||r.url.startsWith('/api/preview/')||r.url.startsWith('/api/control/'))).map(r=>r.url),errors:[...__errors]})`);
 console.log(JSON.stringify({clean,opened,escaped,trapped,stayed,barrier,discarded,reloaded:loadCount>beforeReload}));
 }else console.log(JSON.stringify({clean,opened}));
}finally{socket.close()}
"""
        report = self._run_dashboard_chromium_probe(body, probe)
        self.assertEqual(report["clean"], {"mode": "live", "live": True, "dialogs": 0}, report)
        self.assertEqual(report["opened"]["buttons"], ["Stay Editing", "Discard Draft and switch to Live"], report)
        self.assertEqual(report["opened"]["count"], 1, report)
        self.assertTrue(report["opened"]["open"], report)
        self.assertEqual(report["opened"]["mode"], "edit", report)
        self.assertTrue(report["opened"]["edit"], report)
        self.assertTrue(report["escaped"], report)
        self.assertTrue(report["trapped"], report)
        self.assertEqual(report["stayed"]["mode"], "edit", report)
        self.assertEqual(report["stayed"]["value"], "UNSAVED SWITCH SENTINEL", report)
        self.assertTrue(report["stayed"]["focus"], report)
        self.assertEqual(report["stayed"]["selection"], [2, 8], report)
        self.assertGreater(report["stayed"]["scroll"], 0, report)
        self.assertFalse(report["stayed"]["dialog"], report)
        self.assertEqual(report["barrier"], {"revision": 1, "switches": 0, "saveDisabled": True, "mode": "edit"}, report)
        self.assertTrue(report["reloaded"], report)
        self.assertEqual(report["discarded"]["mode"], "live", report)
        self.assertTrue(report["discarded"]["live"], report)
        self.assertNotEqual(report["discarded"]["value"], "UNSAVED SWITCH SENTINEL", report)
        self.assertEqual(report["discarded"]["canvasCount"], 1, report)
        self.assertEqual(report["discarded"]["forbidden"], [], report)
        self.assertEqual(report["discarded"]["errors"], [], report)
        self.assertEqual(Path(self.store.path).read_bytes(), before_config)
        self.assertEqual(self.controller.method_calls, [])

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    @unittest.skipUnless(
        os.environ.get("MATRIX_CHROMIUM")
        and Path(os.environ.get("MATRIX_CHROMIUM", "")).is_file()
        and os.access(os.environ.get("MATRIX_CHROMIUM", ""), os.X_OK),
        "MATRIX_CHROMIUM browser is required",
    )
    def test_live_edit_preview_is_responsive_accessible_and_preserves_sticky_preview(self):
        self.pair_browser()
        before_config = Path(self.store.path).read_bytes()
        body = self.client.get("/").get_data(as_text=True)
        self.controller.reset_mock()
        probe = r"""
const port=process.argv[1],path=process.argv[2];let target;
for(let n=0;n<50;n++){try{target=await fetch(`http://127.0.0.1:${port}/json/new`,{method:'PUT'}).then(r=>r.json());break}catch(_){await new Promise(r=>setTimeout(r,50))}}
const ws=new WebSocket(target.webSocketDebuggerUrl),pending=new Map();let id=0,resolveLoad,loaded=new Promise(r=>resolveLoad=r);ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(m.error):p.resolve(m.result)}else if(m.method==='Page.loadEventFired')resolveLoad()};await new Promise((r,j)=>{ws.onopen=r;ws.onerror=j});const send=(method,params={})=>new Promise((resolve,reject)=>{const key=++id;pending.set(key,{resolve,reject});ws.send(JSON.stringify({id:key,method,params}))});const ev=async expression=>{const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value};
try{await send('Runtime.enable');await send('Page.enable');await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await send('Page.addScriptToEvaluateOnNewDocument',{source:`globalThis.__errors=[];addEventListener('error',e=>__errors.push(String(e.message)));addEventListener('unhandledrejection',e=>__errors.push(String(e.reason)));const png='iVBORw0KGgoAAAANSUhEUgAAAEAAAAAgCAIAAAAt/+nTAAAAT0lEQVR4nO3PUQkAIBTAwBfOClawfwRD+HEIgwW4zdrn64YLGtCCBrSgAS1oQAsa0IIGtKABLWhACxrQgga0oAEtaEALGtCCBrSgAS147AIqPPhqwA8fegAAAABJRU5ErkJggg==',bytes=Uint8Array.from(atob(png),c=>c.charCodeAt(0));globalThis.fetch=async(u,o={})=>String(u).startsWith('/api/display-frame')?new Response(new Blob([bytes],{type:'image/png'})):String(u).startsWith('/api/edit-preview/')?new Response(JSON.stringify({session:'2'.repeat(32),generation:1,page:0,frames:[{png,delay_ms:999}],next_token:null,complete:true}),{headers:{'Content-Type':'application/json'}}):String(u)==='/api/config'?new Response(null,{status:304,headers:{'X-Matrix-Minimum-Client-Contract':'2'}}):new Response(JSON.stringify({running:true,current_module:'clock_lava',minimum_client_contract:2}),{headers:{'Content-Type':'application/json'}})`});loaded=new Promise(r=>resolveLoad=r);await send('Page.navigate',{url:'file://'+path});await loaded;
const edit=await ev(`(()=>{const r=document.querySelector('label[for="preview-mode-edit"]').getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2}})()`);await send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[edit]});await send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});await ev(`(()=>{const i=document.querySelector('#device-name');i.value='RESPONSIVE DRAFT';i.dispatchEvent(new Event('input',{bubbles:true}));i.focus();i.setSelectionRange(3,9);document.querySelector('.palette-grid').scrollTop=37})()`);
const sizes=[[320,568],[360,800],[390,844],[412,915],[768,1024],[820,1180],[1024,768],[1280,720],[1440,900],[844,390],[599,800],[600,800],[601,800],[899,800],[900,800],[901,800]],geometry=[];for(const [width,height]of sizes){await send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<1024});await ev(`scrollTo(0,document.documentElement.scrollHeight);resizePreviewPopout()`);geometry.push(await ev(`(()=>{const i=document.querySelector('#device-name'),g=document.querySelector('.palette-grid'),c=document.querySelector('#matrix-preview'),live=document.querySelector('.preview-live'),details=document.querySelector('.preview-details'),modes=document.querySelector('.preview-modes'),inner=document.querySelector('.preview-live-inner'),lr=live.getBoundingClientRect(),dr=details.getBoundingClientRect(),mr=modes.getBoundingClientRect(),ir=inner.getBoundingClientRect(),below=document.elementFromPoint(Math.max(1,lr.left+1),Math.min(innerHeight-1,lr.bottom+2));return{viewport:[innerWidth,innerHeight],overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth,targets:[...document.querySelectorAll('.preview-modes label')].map(e=>{const r=e.getBoundingClientRect();return[r.width,r.height]}),lavaTargets:[...document.querySelectorAll('.palette-inspect')].map(e=>{const r=e.getBoundingClientRect();return[r.width,r.height]}),containment:[dr.bottom-lr.bottom,mr.bottom-lr.bottom,ir.bottom-lr.bottom],interceptsBelow:live.contains(below),stack:[parseFloat(getComputedStyle(i).scrollMarginTop),lr.bottom],canvas:[document.querySelectorAll('#matrix-preview').length,c.width,c.height],draft:i.value,selection:[i.selectionStart,i.selectionEnd],scroll:g.scrollTop,focus:document.activeElement===i}})()`))}
const dprs=[];for(const dpr of[1,2,3]){await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:dpr,mobile:true});dprs.push(await ev(`devicePixelRatio`))}await send('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});const reduced=await ev(`getComputedStyle(document.querySelector('.preview-live')).animationDuration`);await send('Emulation.setPageScaleFactor',{pageScaleFactor:2});const zoom=await ev(`({scale:visualViewport.scale,overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth})`);await send('Emulation.setPageScaleFactor',{pageScaleFactor:1});const heights=[];for(const height of[430,844]){await send('Emulation.setDeviceMetricsOverride',{width:390,height,deviceScaleFactor:1,mobile:true});heights.push(await ev(`({height:innerHeight,draft:document.querySelector('#device-name').value,scroll:document.querySelector('.palette-grid').scrollTop})`))}await ev(`scrollTo(0,document.documentElement.scrollHeight);syncPreviewPopout()`);const popped=await ev(`({active:document.querySelector('.preview-live').classList.contains('is-popped'),count:document.querySelectorAll('#matrix-preview').length,overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth})`);const live=await ev(`(()=>{const r=document.querySelector('label[for="preview-mode-live"]').getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2}})()`);await send('Input.dispatchMouseEvent',{type:'mousePressed',x:live.x,y:live.y,button:'left',clickCount:1});await send('Input.dispatchMouseEvent',{type:'mouseReleased',x:live.x,y:live.y,button:'left',clickCount:1});const modal=await ev(`(()=>{const d=document.querySelector('.edit-preview-switch-dialog');return{names:[...d.querySelectorAll('button')].map(b=>b.textContent),targets:[...d.querySelectorAll('button')].map(b=>{const r=b.getBoundingClientRect();return[r.width,r.height]})}})()`);await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Tab',code:'Tab'});await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Tab',code:'Tab'});await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Tab',code:'Tab',modifiers:8});await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Tab',code:'Tab',modifiers:8});await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter'});await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter'});await ev(`document.querySelector('#preview-mode-edit').focus()`);await send('Input.dispatchKeyEvent',{type:'keyDown',key:' ',code:'Space'});await send('Input.dispatchKeyEvent',{type:'keyUp',key:' ',code:'Space'});const final=await ev(`(()=>{const i=document.querySelector('#device-name');return{mode:document.querySelector('#preview-mode-edit').checked?'edit':'live',draft:i.value,selection:[i.selectionStart,i.selectionEnd],scroll:document.querySelector('.palette-grid').scrollTop,count:document.querySelectorAll('#matrix-preview').length,errors:__errors}})()`);console.log(JSON.stringify({geometry,dprs,reduced,zoom,heights,popped,modal,final}))}finally{ws.close()}
"""
        report = self._run_dashboard_chromium_probe(body, probe, timeout=60)
        expected = [[320, 568], [360, 800], [390, 844], [412, 915], [768, 1024], [820, 1180], [1024, 768], [1280, 720], [1440, 900], [844, 390], [599, 800], [600, 800], [601, 800], [899, 800], [900, 800], [901, 800]]
        self.assertEqual([item["viewport"] for item in report["geometry"]], expected, report)
        for item in report["geometry"]:
            self.assertLessEqual(item["overflow"], 1, report)
            self.assertTrue(all(width >= 44 and height >= 44 for width, height in item["targets"]), report)
            self.assertTrue(item["lavaTargets"], report)
            self.assertTrue(all(width >= 44 and height >= 44 for width, height in item["lavaTargets"]), report)
            self.assertTrue(all(delta <= 1 for delta in item["containment"]), report)
            self.assertFalse(item["interceptsBelow"], report)
            self.assertAlmostEqual(item["stack"][0], item["stack"][1], delta=1)
            self.assertEqual(item["canvas"], [1, 64, 32], report)
            self.assertEqual((item["draft"], item["selection"], item["scroll"], item["focus"]), ("RESPONSIVE DRAFT", [3, 9], 37, True), report)
        self.assertEqual(report["dprs"], [1, 2, 3], report)
        self.assertIn(report["reduced"], {"0.01ms", "1e-05s"}, report)
        self.assertEqual(report["zoom"]["scale"], 2, report)
        self.assertLessEqual(report["zoom"]["overflow"], 1, report)
        self.assertEqual(report["heights"], [{"height": 430, "draft": "RESPONSIVE DRAFT", "scroll": 37}, {"height": 844, "draft": "RESPONSIVE DRAFT", "scroll": 37}], report)
        self.assertEqual(report["popped"], {"active": True, "count": 1, "overflow": 0}, report)
        self.assertEqual(report["modal"]["names"], ["Stay Editing", "Discard Draft and switch to Live"], report)
        self.assertTrue(all(width >= 44 and height >= 44 for width, height in report["modal"]["targets"]), report)
        self.assertEqual(report["final"], {"mode": "edit", "draft": "RESPONSIVE DRAFT", "selection": [3, 9], "scroll": 37, "count": 1, "errors": []}, report)
        self.assertEqual(Path(self.store.path).read_bytes(), before_config)
        self.assertEqual(self.controller.method_calls, [])

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    @unittest.skipUnless(
        os.environ.get("MATRIX_CHROMIUM")
        and Path(os.environ.get("MATRIX_CHROMIUM", "")).is_file()
        and os.access(os.environ.get("MATRIX_CHROMIUM", ""), os.X_OK),
        "MATRIX_CHROMIUM browser is required",
    )
    def test_matrix_preview_matches_original_at_top_and_pops_out_after_scroll(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        probe = r"""
const port=process.argv[1],pagePath=process.argv[2];let endpoint;
for(let attempt=0;attempt<50;attempt++){try{endpoint=await fetch(`http://127.0.0.1:${port}/json/new`,{method:'PUT'}).then(r=>r.json());break}catch(_){await new Promise(r=>setTimeout(r,100))}}
if(!endpoint)throw new Error('Chromium DevTools endpoint unavailable');
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map();let nextId=1,loadResolve,loaded=new Promise(resolve=>loadResolve=resolve);
socket.onmessage=event=>{const message=JSON.parse(event.data);if(message.id&&pending.has(message.id)){const request=pending.get(message.id);pending.delete(message.id);message.error?request.reject(new Error(JSON.stringify(message.error))):request.resolve(message.result)}else if(message.method==='Page.loadEventFired')loadResolve()};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>{const id=nextId++;socket.send(JSON.stringify({id,method,params}));return new Promise((resolve,reject)=>pending.set(id,{resolve,reject}))};
const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value};
try{
 await send('Runtime.enable');await send('Page.enable');await send('Emulation.setDeviceMetricsOverride',{width:1280,height:720,deviceScaleFactor:1,mobile:false});loaded=new Promise(resolve=>loadResolve=resolve);await send('Page.navigate',{url:'file://'+pagePath});await Promise.race([loaded,new Promise((_,reject)=>setTimeout(()=>reject(new Error('load timeout')),5000))]);
 const top=await evaluate(`(()=>{const live=document.querySelector('.preview-live'),card=document.querySelector('.preview-card'),quick=document.querySelector('.quick-card'),canvas=document.querySelector('#matrix-preview'),r=live?.getBoundingClientRect(),cr=card?.getBoundingClientRect(),qr=quick?.getBoundingClientRect(),cn=canvas?.getBoundingClientRect();return {popped:live?.classList.contains('is-popped')||false,position:live?getComputedStyle(live).position:null,parent:live?.parentElement?.className||null,card:[cr?.left,cr?.right,cr?.width,cr?.height],quick:[qr?.left,qr?.right,qr?.width,qr?.height],minHeight:getComputedStyle(card).minHeight,inlineHeight:card.style.height,sameRow:Boolean(cr&&qr)&&Math.abs(cr.top-qr.top)<2,canvas:[cn?.width,cn?.height],intrinsic:[canvas?.width,canvas?.height],counts:document.querySelectorAll('#matrix-preview').length}})()`);
 await evaluate(`document.documentElement.style.scrollBehavior='auto';scrollTo(0,document.documentElement.scrollHeight)`);await new Promise(r=>setTimeout(r,100));
 const popped=await evaluate(`(()=>{const live=document.querySelector('.preview-live'),topbar=document.querySelector('.topbar').getBoundingClientRect(),quick=document.querySelector('.quick-card').getBoundingClientRect(),canvas=document.querySelector('#matrix-preview'),r=live?.getBoundingClientRect(),cn=canvas?.getBoundingClientRect();return {popped:live?.classList.contains('is-popped')||false,position:live?getComputedStyle(live).position:null,top:r?.top,topbarBottom:topbar.bottom,width:r?.width,canvas:[cn?.width,cn?.height],animationName:getComputedStyle(live).animationName,quickScrolled:quick.bottom<0,intrinsic:[canvas?.width,canvas?.height],counts:document.querySelectorAll('#matrix-preview').length}})()`);
 await evaluate(`scrollTo(0,0);syncPreviewPopout()`);await new Promise(r=>setTimeout(r,100));
 const restored=await evaluate(`(()=>{const live=document.querySelector('.preview-live'),card=document.querySelector('.preview-card');return {popped:live?.classList.contains('is-popped')||false,position:live?getComputedStyle(live).position:null,parent:live?.parentElement===card,scrollY,threshold:card.offsetTop}})()`);
 console.log(JSON.stringify({top,popped,restored}));
}finally{socket.close()}
"""
        report = self._run_dashboard_chromium_probe(body, probe)
        self.assertFalse(report["top"]["popped"], report)
        self.assertEqual(report["top"]["position"], "static", report)
        self.assertIn("preview-card", report["top"]["parent"])
        self.assertTrue(report["top"]["sameRow"], report)
        self.assertEqual(report["top"]["minHeight"], "auto", report)
        self.assertEqual(report["top"]["inlineHeight"], "", report)
        self.assertAlmostEqual(report["top"]["card"][3], report["top"]["quick"][3], delta=1)
        self.assertLess(report["top"]["card"][1], report["top"]["quick"][0])
        self.assertGreater(report["top"]["card"][2], report["top"]["quick"][2])
        self.assertAlmostEqual(report["top"]["canvas"][0] / report["top"]["canvas"][1], 2, places=2)
        self.assertEqual(report["top"]["intrinsic"], [64, 32])
        self.assertEqual(report["top"]["counts"], 1)
        self.assertTrue(report["popped"]["popped"], report)
        self.assertEqual(report["popped"]["position"], "fixed", report)
        self.assertEqual(report["popped"]["animationName"], "preview-pop-out", report)
        self.assertAlmostEqual(report["popped"]["top"], report["popped"]["topbarBottom"], delta=1)
        self.assertEqual([round(value) for value in report["popped"]["canvas"]], [256, 128])
        self.assertTrue(report["popped"]["quickScrolled"], report)
        self.assertEqual(report["popped"]["intrinsic"], [64, 32])
        self.assertEqual(report["popped"]["counts"], 1)
        self.assertFalse(report["restored"]["popped"], report)
        self.assertEqual(report["restored"]["position"], "static", report)
        self.assertTrue(report["restored"]["parent"], report)
        self.assertEqual(report["restored"]["scrollY"], 0, report)
        self.assertGreater(report["restored"]["threshold"], 0, report)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    @unittest.skipUnless(
        os.environ.get("MATRIX_CHROMIUM")
        and Path(os.environ.get("MATRIX_CHROMIUM", "")).is_file()
        and os.access(os.environ.get("MATRIX_CHROMIUM", ""), os.X_OK),
        "MATRIX_CHROMIUM browser is required",
    )
    def test_matrix_preview_popout_preserves_mobile_document_flow(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        probe = r"""
const port=process.argv[1],pagePath=process.argv[2];let endpoint;
for(let attempt=0;attempt<50;attempt++){try{endpoint=await fetch(`http://127.0.0.1:${port}/json/new`,{method:'PUT'}).then(r=>r.json());break}catch(_){await new Promise(r=>setTimeout(r,100))}}
if(!endpoint)throw new Error('Chromium DevTools endpoint unavailable');
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map();let nextId=1,loadResolve,loaded=new Promise(resolve=>loadResolve=resolve);
socket.onmessage=event=>{const message=JSON.parse(event.data);if(message.id&&pending.has(message.id)){const request=pending.get(message.id);pending.delete(message.id);message.error?request.reject(new Error(JSON.stringify(message.error))):request.resolve(message.result)}else if(message.method==='Page.loadEventFired')loadResolve()};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>{const id=nextId++;socket.send(JSON.stringify({id,method,params}));return new Promise((resolve,reject)=>pending.set(id,{resolve,reject}))};
const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value};
try{
 await send('Runtime.enable');await send('Page.enable');await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});loaded=new Promise(resolve=>loadResolve=resolve);await send('Page.navigate',{url:'file://'+pagePath});await Promise.race([loaded,new Promise((_,reject)=>setTimeout(()=>reject(new Error('load timeout')),5000))]);
 const before=await evaluate(`(()=>{const card=document.querySelector('.preview-card'),quick=document.querySelector('.quick-card');return {cardHeight:card.getBoundingClientRect().height,quickTop:quick.offsetTop,documentHeight:document.documentElement.scrollHeight,threshold:card.offsetTop,inlineHeight:card.style.height}})()`);
 await evaluate(`scrollTo(0,document.querySelector('.preview-card').offsetTop+1);syncPreviewPopout()`);
 const after=await evaluate(`(()=>{const card=document.querySelector('.preview-card'),quick=document.querySelector('.quick-card'),live=document.querySelector('.preview-live');return {popped:live.classList.contains('is-popped'),cardHeight:card.getBoundingClientRect().height,quickTop:quick.offsetTop,documentHeight:document.documentElement.scrollHeight,inlineHeight:card.style.height}})()`);
 console.log(JSON.stringify({before,after}));
}finally{socket.close()}
"""
        report = self._run_dashboard_chromium_probe(body, probe)
        self.assertEqual(report["before"]["inlineHeight"], "", report)
        self.assertTrue(report["after"]["popped"], report)
        self.assertNotEqual(report["after"]["inlineHeight"], "", report)
        self.assertAlmostEqual(report["after"]["cardHeight"], report["before"]["cardHeight"], delta=1)
        self.assertAlmostEqual(report["after"]["quickTop"], report["before"]["quickTop"], delta=1)
        self.assertAlmostEqual(report["after"]["documentHeight"], report["before"]["documentHeight"], delta=1)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    @unittest.skipUnless(
        os.environ.get("MATRIX_CHROMIUM")
        and Path(os.environ.get("MATRIX_CHROMIUM", "")).is_file()
        and os.access(os.environ.get("MATRIX_CHROMIUM", ""), os.X_OK),
        "MATRIX_CHROMIUM browser is required",
    )
    def test_matrix_preview_popout_remeasures_placeholder_across_breakpoints(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        probe = r"""
const port=process.argv[1],pagePath=process.argv[2];let endpoint;
for(let attempt=0;attempt<50;attempt++){try{endpoint=await fetch(`http://127.0.0.1:${port}/json/new`,{method:'PUT'}).then(r=>r.json());break}catch(_){await new Promise(r=>setTimeout(r,100))}}
if(!endpoint)throw new Error('Chromium DevTools endpoint unavailable');
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map();let nextId=1,loadResolve,loaded=new Promise(resolve=>loadResolve=resolve);
socket.onmessage=event=>{const message=JSON.parse(event.data);if(message.id&&pending.has(message.id)){const request=pending.get(message.id);pending.delete(message.id);message.error?request.reject(new Error(JSON.stringify(message.error))):request.resolve(message.result)}else if(message.method==='Page.loadEventFired')loadResolve()};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>{const id=nextId++;socket.send(JSON.stringify({id,method,params}));return new Promise((resolve,reject)=>pending.set(id,{resolve,reject}))};
const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value};
try{
 await send('Runtime.enable');await send('Page.enable');await send('Emulation.setDeviceMetricsOverride',{width:1280,height:720,deviceScaleFactor:1,mobile:false});loaded=new Promise(resolve=>loadResolve=resolve);await send('Page.navigate',{url:'file://'+pagePath});await Promise.race([loaded,new Promise((_,reject)=>setTimeout(()=>reject(new Error('load timeout')),5000))]);
 await evaluate(`document.documentElement.style.scrollBehavior='auto';scrollTo(0,document.documentElement.scrollHeight)`);await new Promise(r=>setTimeout(r,50));await evaluate(`syncPreviewPopout()`);
 const desktop=await evaluate(`(()=>{const card=document.querySelector('.preview-card'),live=document.querySelector('.preview-live');return {popped:live.classList.contains('is-popped'),height:card.getBoundingClientRect().height,inlineHeight:card.style.height,animationName:getComputedStyle(live).animationName}})()`);
 await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});await evaluate(`resizePreviewPopout()`);
 const resized=await evaluate(`(()=>{const card=document.querySelector('.preview-card'),quick=document.querySelector('.quick-card'),live=document.querySelector('.preview-live');return {popped:live.classList.contains('is-popped'),height:card.getBoundingClientRect().height,inlineHeight:card.style.height,quickTop:quick.offsetTop,documentHeight:document.documentElement.scrollHeight,animationName:getComputedStyle(live).animationName,opacity:getComputedStyle(live).opacity}})()`);
 await evaluate(`scrollTo(0,0);syncPreviewPopout()`);
 const natural=await evaluate(`(()=>{const card=document.querySelector('.preview-card'),quick=document.querySelector('.quick-card');return {popped:document.querySelector('.preview-live').classList.contains('is-popped'),height:card.getBoundingClientRect().height,inlineHeight:card.style.height,quickTop:quick.offsetTop,documentHeight:document.documentElement.scrollHeight}})()`);
 console.log(JSON.stringify({desktop,resized,natural}));
}finally{socket.close()}
"""
        report = self._run_dashboard_chromium_probe(body, probe)
        self.assertTrue(report["desktop"]["popped"], report)
        self.assertEqual(report["desktop"]["animationName"], "preview-pop-out", report)
        self.assertTrue(report["resized"]["popped"], report)
        self.assertEqual(report["resized"]["animationName"], "none", report)
        self.assertEqual(report["resized"]["opacity"], "1", report)
        self.assertFalse(report["natural"]["popped"], report)
        self.assertNotEqual(report["desktop"]["height"], report["natural"]["height"], report)
        self.assertNotEqual(report["resized"]["inlineHeight"], report["desktop"]["inlineHeight"], report)
        self.assertAlmostEqual(report["resized"]["height"], report["natural"]["height"], delta=1)
        self.assertAlmostEqual(report["resized"]["quickTop"], report["natural"]["quickTop"], delta=1)
        self.assertAlmostEqual(report["resized"]["documentHeight"], report["natural"]["documentHeight"], delta=1)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    @unittest.skipUnless(
        os.environ.get("MATRIX_CHROMIUM")
        and Path(os.environ.get("MATRIX_CHROMIUM", "")).is_file()
        and os.access(os.environ.get("MATRIX_CHROMIUM", ""), os.X_OK),
        "MATRIX_CHROMIUM browser is required",
    )
    def test_matrix_preview_stays_below_topbar_while_desktop_editors_scroll(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        probe = r"""
const port=process.argv[1],pagePath=process.argv[2];let endpoint;
for(let attempt=0;attempt<50;attempt++){try{endpoint=await fetch(`http://127.0.0.1:${port}/json/new`,{method:'PUT'}).then(r=>r.json());break}catch(_){await new Promise(r=>setTimeout(r,100))}}
if(!endpoint)throw new Error('Chromium DevTools endpoint unavailable');
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map();let nextId=1,loadResolve,loaded=new Promise(resolve=>loadResolve=resolve);
socket.onmessage=event=>{const message=JSON.parse(event.data);if(message.id&&pending.has(message.id)){const request=pending.get(message.id);pending.delete(message.id);message.error?request.reject(new Error(JSON.stringify(message.error))):request.resolve(message.result)}else if(message.method==='Page.loadEventFired')loadResolve()};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>{const id=nextId++;socket.send(JSON.stringify({id,method,params}));return new Promise((resolve,reject)=>pending.set(id,{resolve,reject}))};
const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value};
try{
 await send('Runtime.enable');await send('Page.enable');
 loaded=new Promise(resolve=>loadResolve=resolve);await send('Page.navigate',{url:'file://'+pagePath});await Promise.race([loaded,new Promise((_,reject)=>setTimeout(()=>reject(new Error('load timeout')),5000))]);
 const results=[];await evaluate(`document.documentElement.style.scrollBehavior='auto'`);
 for(const [width,height] of [[1024,768],[1280,720],[1440,900]]){
  await send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
  results.push(await evaluate(`(()=>{const main=document.querySelector('main'),topbar=document.querySelector('.topbar'),strip=document.querySelector('.preview-live'),canvas=document.querySelector('#matrix-preview'),hardware=document.querySelector('.quick-card'),save=document.querySelector('.sticky-save');const direct=strip?.parentElement?.classList.contains('preview-card');const controls=['#weather-location','#day-brightness','#media-upload','#max-paired-devices'],checks=[];for(const selector of controls){const control=document.querySelector(selector);control.scrollIntoView({block:'nearest'});control.focus();syncPreviewPopout();control.scrollIntoView({block:'nearest'});if(control.type==='text'){control.value='unsaved sticky draft';control.setSelectionRange(2,8)}else if(control.type==='number')control.value=selector==='#day-brightness'?'73':'7';const rect=control.getBoundingClientRect(),stripRect=strip?.getBoundingClientRect(),saveRect=save.getBoundingClientRect(),point=document.elementFromPoint(Math.max(rect.left+1,Math.min(rect.right-1,rect.left+rect.width/2)),Math.max(rect.top+1,Math.min(rect.bottom-1,rect.top+rect.height/2)));checks.push({selector,below:Boolean(stripRect)&&rect.top>=stripRect.bottom-1,above:rect.bottom<=saveRect.top+1,hit:point===control||control.contains(point),focused:document.activeElement===control,value:control.value,selection:control.type==='text'?[control.selectionStart,control.selectionEnd]:null,visible:Boolean(stripRect)&&stripRect.bottom>0&&stripRect.top<innerHeight});}scrollTo(0,document.documentElement.scrollHeight);syncPreviewPopout();const stripRect=strip?.getBoundingClientRect(),topRect=topbar.getBoundingClientRect(),mainRect=main.getBoundingClientRect(),canvasRect=canvas.getBoundingClientRect(),hardwareRect=hardware.getBoundingClientRect();return {width:innerWidth,height:innerHeight,direct,position:strip?getComputedStyle(strip).position:null,top:stripRect?.top,topbarBottom:topRect.bottom,stripHeight:stripRect?.height,stripWidth:stripRect?.width,mainWidth:mainRect.width,canvasWidth:canvasRect.width,canvasHeight:canvasRect.height,canvasIntrinsic:[canvas.width,canvas.height],visible:stripRect?.bottom>0&&stripRect?.top<innerHeight,overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth,hardwareScrolled:hardwareRect.bottom<0,checks,counts:{canvas:document.querySelectorAll('#matrix-preview').length,module:document.querySelectorAll('#preview-module').length,frame:document.querySelectorAll('#preview-frame').length}}})()`));
 }
 const poll=await evaluate(`(()=>{const source=[...document.scripts].map(script=>script.textContent).join(' '),matches=source.match(/setTimeout\\(refreshDisplayFrame,100\\)/g)||[];return {matches:matches.length,timerDeclared:(source.match(/matrixFrameTimer/g)||[]).length>0}})()`);
 console.log(JSON.stringify({results,poll}));
}finally{socket.close()}
"""
        payload = self._run_dashboard_chromium_probe(body, probe)
        report = payload["results"]
        self.assertEqual(payload["poll"], {"matches": 1, "timerDeclared": True})
        self.assertEqual([item["width"] for item in report], [1024, 1280, 1440])
        for item in report:
            self.assertTrue(item["direct"], item)
            self.assertEqual(item["position"], "fixed", item)
            self.assertAlmostEqual(item["top"], item["topbarBottom"], delta=1)
            self.assertAlmostEqual(item["stripHeight"], 160, delta=1)
            self.assertAlmostEqual(item["stripWidth"], item["mainWidth"], delta=1)
            self.assertAlmostEqual(item["canvasWidth"], 256, delta=1)
            self.assertAlmostEqual(item["canvasHeight"], 128, delta=1)
            self.assertEqual(item["canvasIntrinsic"], [64, 32])
            self.assertTrue(item["visible"], item)
            self.assertLessEqual(item["overflow"], 1, item)
            self.assertTrue(item["hardwareScrolled"], item)
            self.assertEqual(item["counts"], {"canvas": 1, "module": 1, "frame": 1})
            for check in item["checks"]:
                self.assertTrue(check["below"], check)
                self.assertTrue(check["above"], check)
                self.assertTrue(check["hit"], check)
                self.assertTrue(check["focused"], check)
                self.assertTrue(check["visible"], check)
        self.assertIn("Live physical output", body)
        self.assertIn("/api/display-frame", body)


    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    @unittest.skipUnless(
        os.environ.get("MATRIX_CHROMIUM")
        and Path(os.environ.get("MATRIX_CHROMIUM", "")).is_file()
        and os.access(os.environ.get("MATRIX_CHROMIUM", ""), os.X_OK),
        "MATRIX_CHROMIUM browser is required",
    )
    def test_matrix_preview_remains_compact_reachable_and_sticky_across_responsive_matrix(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        probe = r"""
const port=process.argv[1],pagePath=process.argv[2];let endpoint;
for(let attempt=0;attempt<50;attempt++){try{endpoint=await fetch(`http://127.0.0.1:${port}/json/new`,{method:'PUT'}).then(r=>r.json());break}catch(_){await new Promise(r=>setTimeout(r,100))}}
if(!endpoint)throw new Error('Chromium DevTools endpoint unavailable');
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map();let nextId=1,loadResolve,loaded=new Promise(resolve=>loadResolve=resolve);
socket.onmessage=event=>{const message=JSON.parse(event.data);if(message.id&&pending.has(message.id)){const request=pending.get(message.id);pending.delete(message.id);message.error?request.reject(new Error(JSON.stringify(message.error))):request.resolve(message.result)}else if(message.method==='Page.loadEventFired')loadResolve()};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>{const id=nextId++;socket.send(JSON.stringify({id,method,params}));return new Promise((resolve,reject)=>pending.set(id,{resolve,reject}))};
const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value};
const metrics=(width,height,dpr=1)=>send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:dpr,mobile:false,screenOrientation:{type:width>height?'landscapePrimary':'portraitPrimary',angle:width>height?90:0}});
try{
 await send('Runtime.enable');await send('Page.enable');loaded=new Promise(resolve=>loadResolve=resolve);await send('Page.navigate',{url:'file://'+pagePath});await Promise.race([loaded,new Promise((_,reject)=>setTimeout(()=>reject(new Error('load timeout')),5000))]);await evaluate(`document.documentElement.style.scrollBehavior='auto'`);
 const states=[];
 for(const [width,height,dpr] of [[320,568,1],[360,800,2],[390,844,3],[412,915,1],[768,1024,2],[820,1180,1],[1024,768,1],[1280,720,1],[1440,900,1],[844,390,2],[599,700,1],[600,700,1],[601,700,1],[899,700,1],[900,700,1],[901,700,1]]){
  await metrics(width,height,dpr);states.push(await evaluate(`(()=>{const strip=document.querySelector('.preview-live'),canvas=document.querySelector('#matrix-preview'),meta=document.querySelector('.preview-meta'),save=document.querySelector('.sticky-save'),field=document.querySelector('#device-name');scrollTo(0,0);syncPreviewPopout();field.value='draft-'+innerWidth;field.focus();field.setSelectionRange(2,6);field.scrollIntoView({block:'center'});syncPreviewPopout();field.scrollIntoView({block:'nearest'});const stripRect=strip.getBoundingClientRect(),canvasRect=canvas.getBoundingClientRect(),fieldRect=field.getBoundingClientRect(),saveRect=save.getBoundingClientRect(),style=getComputedStyle(canvas);return {width:innerWidth,height:innerHeight,dpr:devicePixelRatio,position:getComputedStyle(strip).position,top:stripRect.top,stripHeight:stripRect.height,canvas:[canvasRect.width,canvasRect.height],intrinsic:[canvas.width,canvas.height],ratio:canvasRect.width/canvasRect.height,pixelated:['pixelated','crisp-edges'].includes(style.imageRendering),visible:stripRect.bottom>0&&stripRect.top<innerHeight,editingSpace:saveRect.top-stripRect.bottom,inside:stripRect.left>=-.5&&stripRect.right<=innerWidth+.5,overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth,fieldBelow:fieldRect.top>=stripRect.bottom-1,fieldAbove:fieldRect.bottom<=saveRect.top+1,focused:document.activeElement===field,value:field.value,selection:[field.selectionStart,field.selectionEnd],metaReadable:meta.getBoundingClientRect().width>0&&meta.getBoundingClientRect().height>0}})()`));
 }
 await metrics(390,844);const before=await evaluate(`(()=>{const f=document.querySelector('#device-name');f.value='survives resize';f.focus();f.setSelectionRange(2,8);scrollTo(0,600);syncPreviewPopout();return {value:f.value,selection:[f.selectionStart,f.selectionEnd],focused:document.activeElement===f,scroll:scrollY}})()`);await metrics(390,500);const dynamic=await evaluate(`(()=>{const f=document.querySelector('#device-name'),s=document.querySelector('.preview-live').getBoundingClientRect(),c=document.querySelector('#matrix-preview').getBoundingClientRect();return {value:f.value,selection:[f.selectionStart,f.selectionEnd],focused:document.activeElement===f,scroll:scrollY,strip:s.height,canvas:[c.width,c.height],position:getComputedStyle(document.querySelector('.preview-live')).position}})()`);
 await metrics(320,568);await send('Emulation.setPageScaleFactor',{pageScaleFactor:2});const zoom=await evaluate(`(()=>{scrollTo(0,0);const strip=document.querySelector('.preview-live').getBoundingClientRect(),initialViewport={top:visualViewport.offsetTop,bottom:visualViewport.offsetTop+visualViewport.height},previewVisible=strip.bottom>initialViewport.top&&strip.top<initialViewport.bottom,save=document.querySelector('.sticky-save'),field=document.querySelector('#max-paired-devices');field.scrollIntoView({block:'center'});field.focus();const r=field.getBoundingClientRect(),fieldViewport={top:visualViewport.offsetTop,bottom:visualViewport.offsetTop+visualViewport.height};const fieldVisible=r.top>=fieldViewport.top&&r.bottom<=fieldViewport.bottom;save.scrollIntoView({block:'end'});const saveRect=save.getBoundingClientRect(),saveViewport={top:visualViewport.offsetTop,bottom:visualViewport.offsetTop+visualViewport.height},saveVisible=saveRect.top<saveViewport.bottom&&saveRect.bottom>saveViewport.top;return {scale:visualViewport.scale,visible:previewVisible,reachable:fieldVisible,saveVisible}})()`);await send('Emulation.resetPageScaleFactor');
 await send('Emulation.setEmulatedMedia',{media:'screen',features:[{name:'prefers-reduced-motion',value:'reduce'}]});const reduced=await evaluate(`({matches:matchMedia('(prefers-reduced-motion: reduce)').matches,position:getComputedStyle(document.querySelector('.preview-live')).position})`);
 console.log(JSON.stringify({states,before,dynamic,zoom,reduced}));
}finally{socket.close()}
"""
        report = self._run_dashboard_chromium_probe(body, probe, timeout=45)
        self.assertEqual(len(report["states"]), 16)
        for item in report["states"]:
            self.assertEqual(item["position"], "fixed", item)
            self.assertAlmostEqual(item["top"], 64, delta=1)
            expected_height = 88 if item["height"] <= 500 else (132 if item["width"] <= 600 else (152 if item["width"] <= 900 else 160))
            expected_canvas = [128, 64] if item["height"] <= 500 or item["width"] <= 360 else ([160, 80] if item["width"] <= 600 else ([240, 120] if item["width"] <= 900 else [256, 128]))
            self.assertAlmostEqual(item["stripHeight"], expected_height, delta=1, msg=item)
            self.assertEqual([round(value) for value in item["canvas"]], expected_canvas, item)
            self.assertEqual(item["intrinsic"], [64, 32])
            self.assertAlmostEqual(item["ratio"], 2, places=2)
            self.assertTrue(item["pixelated"], item)
            self.assertTrue(item["visible"], item)
            self.assertTrue(item["inside"], item)
            self.assertLessEqual(item["overflow"], 1, item)
            self.assertTrue(item["fieldBelow"], item)
            self.assertTrue(item["fieldAbove"], item)
            self.assertTrue(item["focused"], item)
            self.assertTrue(item["metaReadable"], item)
            if item["height"] <= 568:
                self.assertGreaterEqual(item["editingSpace"], 160, item)
        for key in ("value", "selection", "focused"):
            self.assertEqual(report["before"][key], report["dynamic"][key])
        self.assertGreaterEqual(report["before"]["scroll"], 600)
        self.assertGreaterEqual(report["dynamic"]["scroll"], 600)
        self.assertEqual(report["dynamic"]["strip"], 88)
        self.assertEqual([round(value) for value in report["dynamic"]["canvas"]], [128, 64])
        self.assertEqual(report["dynamic"]["position"], "fixed")
        self.assertAlmostEqual(report["zoom"]["scale"], 2, places=2)
        self.assertTrue(report["zoom"]["visible"])
        self.assertTrue(report["zoom"]["reachable"])
        self.assertTrue(report["zoom"]["saveVisible"])
        self.assertEqual(report["reduced"], {"matches": True, "position": "fixed"})

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    @unittest.skipUnless(
        os.environ.get("MATRIX_CHROMIUM")
        and Path(os.environ.get("MATRIX_CHROMIUM", "")).is_file()
        and os.access(os.environ.get("MATRIX_CHROMIUM", ""), os.X_OK),
        "MATRIX_CHROMIUM browser is required",
    )
    def test_real_chromium_paired_sync_conflict_matrix(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        chromium = os.environ["MATRIX_CHROMIUM"]
        node = shutil.which("node")
        assert node is not None
        probe = r"""
const port=process.argv[1],pagePath=process.argv[2];let endpoint;
for(let attempt=0;attempt<50;attempt++){try{endpoint=await fetch(`http://127.0.0.1:${port}/json/new`,{method:'PUT'}).then(r=>r.json());break}catch(_){await new Promise(r=>setTimeout(r,100))}}
if(!endpoint)throw new Error('Chromium DevTools endpoint unavailable');
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map();let nextId=1,loadResolve=()=>{},loaded;
socket.onmessage=event=>{const message=JSON.parse(event.data);if(message.id&&pending.has(message.id)){const request=pending.get(message.id);pending.delete(message.id);message.error?request.reject(new Error(JSON.stringify(message.error))):request.resolve(message.result)}else if(message.method==='Page.loadEventFired')loadResolve()};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>{const id=nextId++;socket.send(JSON.stringify({id,method,params}));return new Promise((resolve,reject)=>pending.set(id,{resolve,reject}))};
const evaluate=async(expression,awaitPromise=false)=>{const result=await send('Runtime.evaluate',{expression,awaitPromise,returnByValue:true});if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value};
const waitFor=async(expression,label)=>{for(let attempt=0;attempt<100;attempt++){if(await evaluate(expression))return;await new Promise(resolve=>setTimeout(resolve,20))}throw new Error('timeout waiting for '+label)};
const metrics=async(width,height,mobile=width<900)=>send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile,screenOrientation:{type:width>height?'landscapePrimary':'portraitPrimary',angle:width>height?90:0}});
const dialogPoint=async(revision,value,label)=>evaluate(`(()=>{const field=document.querySelector('#device-name');field.value=${JSON.stringify(value)};field.focus();field.setSelectionRange(2,5);showMatrixRevisionDialog(${revision});const button=[...document.querySelectorAll('.config-revision-dialog button')].find(item=>item.textContent===${JSON.stringify(label)}),rect=button.getBoundingClientRect();return {x:rect.left+rect.width/2,y:rect.top+rect.height/2}})()`);
const mouseClick=async point=>{await send('Input.dispatchMouseEvent',{type:'mousePressed',x:point.x,y:point.y,button:'left',clickCount:1});await send('Input.dispatchMouseEvent',{type:'mouseReleased',x:point.x,y:point.y,button:'left',clickCount:1})};
const touchTap=async point=>{await send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:point.x,y:point.y,radiusX:1,radiusY:1,force:1,id:1}]});await send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]})};
try{
 await send('Runtime.enable');await send('Page.enable');await send('Network.enable');
 await metrics(320,568,true);await send('Emulation.setTouchEmulationEnabled',{enabled:true,maxTouchPoints:1});
 loaded=new Promise(resolve=>loadResolve=resolve);await send('Page.navigate',{url:'file://'+pagePath});await Promise.race([loaded,new Promise((_,reject)=>setTimeout(()=>reject(new Error('load timeout')),5000))]);
 const viewports=[[320,568],[360,800],[390,844],[412,915],[768,1024],[820,1180],[1024,768],[1280,720],[1440,900]],geometry=[];
 for(let index=0;index<viewports.length;index++){
  const [width,height]=viewports[index];await metrics(width,height,false);
  geometry.push(await evaluate(`(()=>{const field=document.querySelector('#device-name');field.value='unsaved-${index}';field.focus();field.setSelectionRange(2,5);scrollTo(0,Math.min(300,document.documentElement.scrollHeight));const before={value:field.value,start:field.selectionStart,end:field.selectionEnd,scroll:scrollY};showMatrixRevisionDialog(${index+1});const dialog=document.querySelector('.config-revision-dialog'),buttons=[...dialog.querySelectorAll('button')],rect=dialog.getBoundingClientRect(),sizes=buttons.map(button=>{const r=button.getBoundingClientRect();return [r.width,r.height]});const initiallyFocused=document.activeElement===buttons[0];dialog.dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',bubbles:true}));const trapped=document.activeElement===buttons[1];buttons[1].click();return {width:innerWidth,height:innerHeight,before,after:{value:field.value,start:field.selectionStart,end:field.selectionEnd,scroll:scrollY},initiallyFocused,trapped,dialogFits:rect.top>=0&&rect.bottom<=innerHeight,sizes,restored:document.activeElement===field}})()`));
 }
 await metrics(320,568,true);const zoomBefore=await evaluate(`({innerWidth,innerHeight,scale:visualViewport.scale,visualWidth:visualViewport.width,visualHeight:visualViewport.height})`);await send('Emulation.setPageScaleFactor',{pageScaleFactor:2});const zoomAfter=await evaluate(`(()=>{const field=document.querySelector('#device-name');field.value='zoom draft';field.focus();field.setSelectionRange(2,5);showMatrixRevisionDialog(100);const dialog=document.querySelector('.config-revision-dialog'),buttons=[...dialog.querySelectorAll('button')],rect=dialog.getBoundingClientRect(),result={innerWidth,innerHeight,scale:visualViewport.scale,visualWidth:visualViewport.width,visualHeight:visualViewport.height,dialogFits:rect.top>=0&&rect.bottom<=innerHeight,sizes:buttons.map(button=>{const r=button.getBoundingClientRect();return [r.width,r.height]})};buttons[1].click();return result})()`);await send('Emulation.resetPageScaleFactor');
 const breakpoint=[];for(const width of [599,600,601,899,900,901]){await metrics(width,700);breakpoint.push(await evaluate(`({width:innerWidth,grid:getComputedStyle(document.querySelector('.grid')).display,nav:getComputedStyle(document.querySelector('.nav')).display})`))}
 await metrics(899,700);const dynamicBefore=await evaluate(`(()=>{const field=document.querySelector('#device-name');field.value='dynamic draft';field.focus();field.setSelectionRange(1,6);scrollTo(0,Math.min(240,document.documentElement.scrollHeight));return {width:innerWidth,nav:getComputedStyle(document.querySelector('.nav')).display,value:field.value,start:field.selectionStart,end:field.selectionEnd,scroll:scrollY}})()`);await metrics(901,700,false);const dynamicAfter=await evaluate(`(()=>{const field=document.querySelector('#device-name');return {width:innerWidth,nav:getComputedStyle(document.querySelector('.nav')).display,value:field.value,start:field.selectionStart,end:field.selectionEnd,scroll:scrollY}})()`);
 await metrics(390,844,true);const mousePoint=await dialogPoint(101,'mouse draft','Remain with unsaved draft');await mouseClick(mousePoint);const mouse=await evaluate(`(()=>{const field=document.querySelector('#device-name');return {closed:!document.querySelector('.config-revision-dialog'),restored:document.activeElement===field,value:field.value,start:field.selectionStart,end:field.selectionEnd}})()`);
 const touchPoint=await dialogPoint(102,'touch draft','Remain with unsaved draft');await touchTap(touchPoint);const touch=await evaluate(`(()=>{const field=document.querySelector('#device-name');return {closed:!document.querySelector('.config-revision-dialog'),restored:document.activeElement===field,value:field.value,start:field.selectionStart,end:field.selectionEnd}})()`);
 await evaluate(`(()=>{if(matrixSyncTimer!==null){clearInterval(matrixSyncTimer);matrixSyncTimer=null}globalThis.__matrixRecovery={fetches:0,events:[],visibility:[]};globalThis.fetch=async()=>{__matrixRecovery.fetches++;return {status:304,redirected:false,ok:true,headers:{get:()=>String(MATRIX_CLIENT_CONTRACT)},json:async()=>({})}};addEventListener('offline',()=>__matrixRecovery.events.push('offline'));addEventListener('online',()=>__matrixRecovery.events.push('online'));document.addEventListener('visibilitychange',()=>__matrixRecovery.visibility.push({state:document.visibilityState,fetches:__matrixRecovery.fetches}));return true})()`);
 await send('Network.emulateNetworkConditions',{offline:true,latency:0,downloadThroughput:-1,uploadThroughput:-1});await waitFor(`navigator.onLine===false&&__matrixRecovery.events.includes('offline')`,'offline event');const offlineFetches=await evaluate(`__matrixRecovery.fetches`);await send('Network.emulateNetworkConditions',{offline:false,latency:0,downloadThroughput:-1,uploadThroughput:-1});await waitFor(`navigator.onLine===true&&__matrixRecovery.events.includes('online')&&__matrixRecovery.fetches>${offlineFetches}`,'online recovery poll');const onlineOffline=await evaluate(`({events:__matrixRecovery.events,offlineFetches:${offlineFetches},fetches:__matrixRecovery.fetches})`);
 const visibilityBefore=await evaluate(`__matrixRecovery.fetches`),visibilityMode='event-override';const visibilityHidden=await evaluate(`(()=>{globalThis.__matrixVisibilityState='hidden';Object.defineProperty(document,'visibilityState',{configurable:true,get:()=>globalThis.__matrixVisibilityState});document.dispatchEvent(new Event('visibilitychange'));return __matrixRecovery.visibility.at(-1)})()`);await evaluate(`(()=>{globalThis.__matrixVisibilityState='visible';document.dispatchEvent(new Event('visibilitychange'));return true})()`);await waitFor(`__matrixRecovery.fetches>${visibilityBefore}`,'visible event recovery');const visibilityVisible={state:await evaluate(`document.visibilityState`),fetches:await evaluate(`__matrixRecovery.fetches`)};
 await send('Emulation.setEmulatedMedia',{media:'screen',features:[{name:'prefers-reduced-motion',value:'reduce'}]});const reducedPoint=await dialogPoint(103,'reduced draft','Remain with unsaved draft');const reducedBefore=await evaluate(`({matches:matchMedia('(prefers-reduced-motion: reduce)').matches,focused:document.activeElement.textContent==='Reload latest saved state',fits:(()=>{const r=document.querySelector('.config-revision-dialog').getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight})()})`);await mouseClick(reducedPoint);const reducedAfter=await evaluate(`({closed:!document.querySelector('.config-revision-dialog'),restored:document.activeElement===document.querySelector('#device-name')})`);
 await metrics(390,844,true);const reloadPoint=await dialogPoint(104,'discard this draft','Reload latest saved state');loaded=new Promise(resolve=>loadResolve=resolve);await mouseClick(reloadPoint);await Promise.race([loaded,new Promise((_,reject)=>setTimeout(()=>reject(new Error('reload choice timeout')),5000))]);const reloadChoice=await evaluate(`({value:document.querySelector('#device-name').value,dialog:!!document.querySelector('.config-revision-dialog')})`);
 console.log(JSON.stringify({geometry,zoomBefore,zoomAfter,breakpoint,dynamicBefore,dynamicAfter,mouse,touch,onlineOffline,visibilityMode,visibilityBefore,visibilityHidden,visibilityVisible,reducedBefore,reducedAfter,reloadChoice}));
}finally{socket.close()}
"""
        with tempfile.TemporaryDirectory(prefix="matrix-sync-chromium-") as directory:
            page = Path(directory) / "dashboard.html"
            page.write_text(body)
            profile = Path(directory) / "profile"
            browser = subprocess.Popen([
                chromium, "--headless", "--no-sandbox", "--disable-gpu",
                "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                active_port = profile / "DevToolsActivePort"
                for _attempt in range(50):
                    if active_port.is_file():
                        break
                    if browser.poll() is not None:
                        self.fail("Chromium exited before publishing its DevTools port")
                    time.sleep(0.1)
                self.assertTrue(active_port.is_file(), "Chromium DevTools port unavailable")
                completed = subprocess.run(
                    [node, "-e", probe, active_port.read_text().splitlines()[0], str(page)],
                    capture_output=True, text=True, timeout=30,
                )
            finally:
                browser.terminate()
                try:
                    browser.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    browser.kill(); browser.wait(timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertIn("visibilityHidden", report, report)
        geometry = report["geometry"]
        self.assertEqual(
            [(item["width"], item["height"]) for item in geometry],
            [
                (320, 568), (360, 800), (390, 844), (412, 915),
                (768, 1024), (820, 1180), (1024, 768),
                (1280, 720), (1440, 900),
            ],
        )
        for item in geometry:
            self.assertEqual(item["before"], item["after"])
            self.assertTrue(item["initiallyFocused"])
            self.assertTrue(item["trapped"])
            self.assertTrue(item["dialogFits"])
            self.assertTrue(item["restored"])
            self.assertTrue(all(width >= 44 and height >= 44 for width, height in item["sizes"]))
        self.assertGreater(report["zoomBefore"]["innerWidth"], 0)
        self.assertGreater(report["zoomBefore"]["innerHeight"], 0)
        self.assertGreater(report["zoomAfter"]["scale"], report["zoomBefore"]["scale"])
        self.assertAlmostEqual(report["zoomAfter"]["scale"], 2, places=2)
        self.assertAlmostEqual(
            report["zoomAfter"]["visualWidth"] * report["zoomAfter"]["scale"],
            320, places=1,
        )
        self.assertAlmostEqual(
            report["zoomAfter"]["visualHeight"] * report["zoomAfter"]["scale"],
            568, places=1,
        )
        self.assertTrue(report["zoomAfter"]["dialogFits"])
        self.assertTrue(all(width >= 44 and height >= 44 for width, height in report["zoomAfter"]["sizes"]))
        self.assertLess(report["zoomAfter"]["visualWidth"], report["zoomBefore"]["visualWidth"])
        self.assertLess(report["zoomAfter"]["visualHeight"], report["zoomBefore"]["visualHeight"])
        self.assertEqual(
            [(item["width"], item["grid"], item["nav"]) for item in report["breakpoint"]],
            [
                (599, "block", "none"), (600, "block", "none"),
                (601, "grid", "none"), (899, "grid", "none"),
                (900, "grid", "none"), (901, "grid", "grid"),
            ],
        )
        self.assertEqual(report["dynamicBefore"]["width"], 899)
        self.assertEqual(report["dynamicAfter"]["width"], 901)
        self.assertEqual(report["dynamicBefore"]["nav"], "none")
        self.assertEqual(report["dynamicAfter"]["nav"], "grid")
        for key in ("value", "start", "end", "scroll"):
            self.assertEqual(report["dynamicBefore"][key], report["dynamicAfter"][key])
        for activation, value in ((report["mouse"], "mouse draft"), (report["touch"], "touch draft")):
            self.assertTrue(activation["closed"])
            self.assertTrue(activation["restored"])
            self.assertEqual(activation["value"], value)
        self.assertEqual(report["onlineOffline"]["events"], ["offline", "online"])
        self.assertGreater(report["onlineOffline"]["fetches"], report["onlineOffline"]["offlineFetches"])
        self.assertIn(report["visibilityMode"], {"target-switch", "event-override"})
        self.assertEqual(report["visibilityHidden"]["state"], "hidden")
        self.assertEqual(report["visibilityHidden"]["fetches"], report["visibilityBefore"])
        self.assertEqual(report["visibilityVisible"]["state"], "visible")
        self.assertGreater(report["visibilityVisible"]["fetches"], report["visibilityBefore"])
        self.assertEqual(report["reducedBefore"], {"matches": True, "focused": True, "fits": True})
        self.assertEqual(report["reducedAfter"], {"closed": True, "restored": True})
        self.assertNotEqual(report["reloadChoice"]["value"], "discard this draft")
        self.assertFalse(report["reloadChoice"]["dialog"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_open_b1_dashboard_transitions_safely_to_b2_server(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("const MATRIX_CLIENT_CONTRACT=2", body)
        self.assertIn("let matrixLoadedRevision=0", body)
        self.assertIn("'X-Matrix-Client-Contract':String(MATRIX_CLIENT_CONTRACT)", body)
        self.assertIn("'If-Match':matrixRevisionETag()", body)
        self.assertIn("function matrixHasUnsavedDraft()", body)
        self.assertIn("Reload latest saved state", body)
        self.assertIn("Remain with unsaved draft", body)
        self.assertIn("aria-labelledby", body)
        self.assertIn("aria-describedby", body)
        self.assertIn("event.preventDefault()", body)
        self.assertIn("config-upgrade-actions", body)
        self.assertIn("width:min(32rem,calc(100vw - 2rem))", body)
        self.assertIn("min-height:44px", body)

        frozen_b1 = body.replace("const MATRIX_CLIENT_CONTRACT=2", "const MATRIX_CLIENT_CONTRACT=1", 1)
        start = frozen_b1.index("const MATRIX_CLIENT_CONTRACT=1")
        end = frozen_b1.index("const customFontAdvances", start)
        bridge = frozen_b1[start:end]
        harness = r"""
class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.textContent='';this.open=false;this.listeners={};}
  append(...items){this.children.push(...items)}
  setAttribute(name,value){this[name]=value}
  addEventListener(name,handler){this.listeners[name]=handler}
  showModal(){this.open=true}
  focus(){this.focused=true}
  close(){this.open=false}
  remove(){this.removed=true}
}
const listeners={}, bodyElement=new Element('body');
const document={
  body:bodyElement,
  activeElement:new Element('input'),
  querySelector(selector){return selector==='#message'?new Element():null},
  createElement(tag){return new Element(tag)},
  addEventListener(name,handler){listeners[name]=handler},
};
let reloads=0, requests=[],configState={canvas:{operation:'saved'}};
function configBody(){return structuredClone(configState)}
const window={location:{reload(){reloads++},set href(value){this._href=value}},matrixInstalledFonts:[]};
async function fetch(path,options={}){requests.push({path,options});return {status:200,redirected:false,ok:true,json:async()=>({})}}
globalThis.document=document;globalThis.window=window;globalThis.fetch=fetch;
""" + bridge + r"""
(async()=>{
  matrixSavedSnapshot=matrixCurrentSnapshot();
  await api('/api/status');
  if(requests[0].options.headers['X-Matrix-Client-Contract']!=='1')throw new Error('contract header missing');
  handleMinimumClientContract(2);
  if(reloads!==1)throw new Error('clean page did not reload');
  matrixUpgradeReloadRequested=false;
  configState.canvas.operation='template conversion';
  listeners.input({target:{}});
  handleMinimumClientContract(3);
  const first=bodyElement.children.at(-1),firstActions=first.children.at(-1);
  if(!first.open)throw new Error('dirty guard not shown');
  if(firstActions.children[0].textContent!=='Reload latest saved state'||firstActions.children[1].textContent!=='Remain with unsaved draft')throw new Error('choices changed');
  if(!firstActions.children[0].focused)throw new Error('initial focus missing');
  let cancelled=false;first.listeners.cancel({preventDefault(){cancelled=true}});
  if(!cancelled||!first.open)throw new Error('escape dismissed decision');
  firstActions.children[1].onclick();
  const count=bodyElement.children.length;
  handleMinimumClientContract(3);
  if(bodyElement.children.length!==count)throw new Error('acknowledged contract churned');
  handleMinimumClientContract(4);
  if(bodyElement.children.length!==count+1)throw new Error('newer contract did not warn');
  const second=bodyElement.children.at(-1),secondActions=second.children.at(-1);
  secondActions.children[1].onclick();
  configState.canvas.operation='saved';
  matrixDirty=false;
  matrixUpgradeReloadRequested=false;
  handleMinimumClientContract(4);
  if(reloads!==2)throw new Error('clean page remained stranded after acknowledgement');
  console.log('B1_TRANSITION_PASS');
})().catch(error=>{console.error(error);process.exit(1)});
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], text=True, capture_output=True,
            cwd=Path(__file__).parents[1], check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("B1_TRANSITION_PASS", result.stdout)

    def test_dashboard_programmatic_editor_mutations_mark_the_draft_dirty(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        mutators = (
            "moveModule", "duplicateAnnouncement", "deleteAnnouncement",
            "moveAnnouncement", "resetAnnouncementBoard", "addAnnouncement",
            "resetBuiltInModule", "addCustomLavaPalette", "removeCustomLavaPalette",
            "addCustomModule", "removeCustomModule",
        )
        for index, function_name in enumerate(mutators):
            with self.subTest(function=function_name):
                start = body.index(f"function {function_name}(")
                next_start = body.find("\nfunction ", start + 1)
                source = body[start:next_start if next_start >= 0 else None]
                self.assertIn("markMatrixDirty()", source)

    def test_config_update_reports_committed_state_when_directory_sync_fails(self):
        self.pair_browser()

        with patch(
            "config_store.os.fsync",
            side_effect=[None, OSError("directory unavailable")],
        ):
            response = self.post_config(
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

        def blocked_update(changes, **options):
            config_entered.set()
            self.assertTrue(release_config.wait(2))
            return original_update(changes, **options)

        def lower_limit():
            client = self.app.test_client()
            with client.session_transaction() as session_data:
                session_data["paired"] = True
                session_data["generation"] = "generation-one"
                session_data["device_id"] = self.device_store.list()[0]["id"]
            results["config"] = self.post_config(
                json={"pairing": {"max_devices": 1}}, client=client,
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
        malformed = self.post_config( data="{", content_type="application/json",
        )
        wrong_type = self.post_config(
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
                response = self.post_config(
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
        self.assertEqual(body.count("field:'scroll_loops',label:'Scroll loops'"), 3)
        self.assertEqual(body.count("field:'scroll_speed',label:'Frame delay (seconds)'"), 3)
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

        response = self.post_config( json={"pairing": {"max_devices": 1}})
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
        self.assertIn('<label for="media-upload">Choose media file</label>', body)
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
let messages=[],previewCalls=0,previewModule='',matrixLoadedRevision=0,matrixDirty=true,matrixRecoveryPending=false,highestNotifiedRemoteRevision=0;
function matrixRevisionETag(){return '"'+matrixLoadedRevision+'"';}
function configBody(){return {};}
function setMessage(text,error=false){messages.push({text,error});}
function dismissMatrixRevisionDialogThrough(){}
global.document={querySelector(){return {value:''};}};
async function api(path){
  if(path==='/api/config'){const error=new Error('Configuration saved, but display reload failed.');error.data={committed:true,display_reloaded:false,config:{revision:1}};throw error;}
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
let messages=[],previewModule='',matrixLoadedRevision=0,matrixDirty=true,matrixRecoveryPending=false,highestNotifiedRemoteRevision=0;
function matrixRevisionETag(){return '"'+matrixLoadedRevision+'"';}
function configBody(){return {};}
function setMessage(text,error=false){messages.push({text,error});}
function dismissMatrixRevisionDialogThrough(){}
global.document={querySelector(){return {value:''};}};
async function api(path){
  if(path==='/api/config'){const error=new Error('Configuration saved, but display reload failed.');error.data={committed:true,config:{revision:1}};throw error;}
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

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_playlist_uses_one_physical_time_format_for_every_row(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertEqual(body.count('class="physical-time"'), len(self.store.get()["playlist"]))
        self.assertIn("function formatPhysicalTime(seconds)", body)
        source = body[body.index("function formatPhysicalTime(seconds)"):]
        function_source = source[:source.index("\nfunction ", 1)]
        harness = function_source + r"""
const cases = new Map([
  [0, '00:00:00'],
  [0.025, '00:00:00.025'],
  [8, '00:00:08'],
  [90, '00:01:30'],
  [3661, '01:01:01'],
]);
for (const [seconds, expected] of cases) {
  const actual = formatPhysicalTime(seconds);
  if (actual !== expected) throw new Error(`${seconds}: ${actual} !== ${expected}`);
}
console.log('UNIFIED_PHYSICAL_TIME_FORMAT_PASS');
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("UNIFIED_PHYSICAL_TIME_FORMAT_PASS", result.stdout)

    def test_physical_time_wraps_on_narrow_mobile_layouts(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        mobile = body[body.index("@media(max-width:600px)"):body.index("</style>")]
        self.assertIn("@media(max-width:600px)", body)
        self.assertIn(".module-row{grid-template-columns:24px minmax(0,1fr)}", body)
        self.assertIn(".module-row>label{min-width:0}", body)
        self.assertIn(
            ".physical-time{white-space:normal;overflow-wrap:anywhere}",
            mobile,
        )

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    @unittest.skipUnless(
        os.environ.get("MATRIX_CHROMIUM")
        and Path(os.environ.get("MATRIX_CHROMIUM", "")).is_file()
        and os.access(os.environ.get("MATRIX_CHROMIUM", ""), os.X_OK),
        "MATRIX_CHROMIUM browser is required",
    )
    def test_playlist_timing_and_reorder_controls_in_real_chromium_at_320px(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        chromium = os.environ["MATRIX_CHROMIUM"]
        node = shutil.which("node")
        assert node is not None
        probe = r"""
const port=process.argv[1],pagePath=process.argv[2];
let endpoint;
for(let attempt=0;attempt<50;attempt++){
  try{endpoint=await fetch(`http://127.0.0.1:${port}/json/new`,{method:'PUT'}).then(response=>response.json());break}
  catch(error){await new Promise(resolve=>setTimeout(resolve,100))}
}
if(!endpoint)throw new Error('Chromium DevTools endpoint unavailable');
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map(),exceptions=[];
let nextId=1,loadResolve,loaded=new Promise(resolve=>{loadResolve=resolve});
socket.onmessage=event=>{const message=JSON.parse(event.data);if(message.id&&pending.has(message.id)){const request=pending.get(message.id);pending.delete(message.id);message.error?request.reject(new Error(JSON.stringify(message.error))):request.resolve(message.result);return}if(message.method==='Runtime.exceptionThrown')exceptions.push(message.params.exceptionDetails);if(message.method==='Page.loadEventFired')loadResolve()};
try{
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
function send(method,params={}){const id=nextId++;socket.send(JSON.stringify({id,method,params}));return new Promise((resolve,reject)=>pending.set(id,{resolve,reject}))}
await send('Runtime.enable');await send('Page.enable');
await send('Emulation.setDeviceMetricsOverride',{width:320,height:900,deviceScaleFactor:1,mobile:false});
loaded=new Promise(resolve=>{loadResolve=resolve});
await send('Page.navigate',{url:'file://'+pagePath});
await Promise.race([loaded,new Promise((_,reject)=>setTimeout(()=>reject(new Error('page load timeout')),5000))]);
const expression=`JSON.stringify((()=>{const wrapper=createPlaylistRow({module:'custom_probe',duration:15,enabled:true});document.querySelector('#playlist').append(wrapper);providerTimingPlans.set('weather',{frames_per_loop:1000000,status:'stale'});syncPlaylistDurations();const buttons=[...wrapper.querySelectorAll('.module-actions button')],duration=wrapper.querySelector('.duration');duration.value='90';duration.dispatchEvent(new Event('input',{bubbles:true}));const physical=wrapper.querySelector('.physical-time'),automatic=document.querySelector('.module-row[data-module="weather"] .physical-time'),style=getComputedStyle(automatic),rect=automatic.getBoundingClientRect(),itemRect=automatic.closest('.module-item').getBoundingClientRect(),range=document.createRange();range.selectNodeContents(automatic);return {viewport:innerWidth,labels:buttons.slice(0,2).map(button=>button.getAttribute('aria-label')),physical:physical.textContent,automatic:automatic.textContent,whiteSpace:style.whiteSpace,overflowWrap:style.overflowWrap,lineCount:range.getClientRects().length,clipped:rect.right>itemRect.right+.5}})())`;
const evaluated=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
if(evaluated.exceptionDetails)throw new Error(JSON.stringify(evaluated.exceptionDetails));
console.log(JSON.stringify({result:JSON.parse(evaluated.result.value),exceptions:exceptions.map(item=>item.text)}));
}finally{socket.close()}
"""
        with tempfile.TemporaryDirectory(prefix="matrix-chromium-") as directory:
            page = Path(directory) / "dashboard.html"
            page.write_text(body)
            profile = Path(directory) / "profile"
            browser = subprocess.Popen(
                [
                    chromium,
                    "--headless",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--remote-debugging-port=0",
                    f"--user-data-dir={profile}",
                    "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                active_port = profile / "DevToolsActivePort"
                for _attempt in range(50):
                    if active_port.is_file():
                        break
                    if browser.poll() is not None:
                        self.fail("Chromium exited before publishing its DevTools port")
                    time.sleep(0.1)
                self.assertTrue(active_port.is_file(), "Chromium DevTools port unavailable")
                port = active_port.read_text().splitlines()[0]
                completed = subprocess.run(
                    [node, "-e", probe, str(port), str(page)],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            finally:
                browser.terminate()
                try:
                    browser.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    browser.kill()
                    browser.wait(timeout=5)
                for cleanup_attempt in range(20):
                    try:
                        shutil.rmtree(profile)
                        break
                    except FileNotFoundError:
                        break
                    except OSError:
                        if cleanup_attempt == 19:
                            raise
                        time.sleep(0.05)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(report["exceptions"], [])
        self.assertEqual(report["result"]["viewport"], 320)
        self.assertEqual(report["result"]["labels"], ["Move up", "Move down"])
        self.assertEqual(report["result"]["physical"], "Physical time · 00:01:30")
        self.assertRegex(
            report["result"]["automatic"],
            r"^Physical time · Stale · \d+\.\d s$",
        )
        self.assertEqual(report["result"]["whiteSpace"], "normal")
        self.assertEqual(report["result"]["overflowWrap"], "anywhere")
        self.assertGreaterEqual(report["result"]["lineCount"], 2)
        self.assertFalse(report["result"]["clipped"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    @unittest.skipUnless(
        os.environ.get("MATRIX_CHROMIUM")
        and Path(os.environ.get("MATRIX_CHROMIUM", "")).is_file()
        and os.access(os.environ.get("MATRIX_CHROMIUM", ""), os.X_OK),
        "MATRIX_CHROMIUM browser is required",
    )
    def test_scrolling_seconds_editing_and_provider_states_in_real_chromium(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        chromium = os.environ["MATRIX_CHROMIUM"]
        probe = r"""
const port=process.argv[1],pagePath=process.argv[2];let endpoint;
for(let attempt=0;attempt<50;attempt++){try{endpoint=await fetch(`http://127.0.0.1:${port}/json/new`,{method:'PUT'}).then(r=>r.json());break}catch(_){await new Promise(r=>setTimeout(r,100))}}
if(!endpoint)throw new Error('CDP unavailable');
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map();let id=0,resolveLoad,loaded=new Promise(r=>resolveLoad=r);
socket.onmessage=e=>{const m=JSON.parse(e.data);if(m.id&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result)}else if(m.method==='Page.loadEventFired')resolveLoad()};
await new Promise((r,j)=>{socket.onopen=r;socket.onerror=j});
const send=(method,params={})=>new Promise((resolve,reject)=>{const request=++id;pending.set(request,{resolve,reject});socket.send(JSON.stringify({id:request,method,params}))});
await send('Runtime.enable');await send('Page.enable');await send('Page.navigate',{url:'file://'+pagePath});await Promise.race([loaded,new Promise((_,j)=>setTimeout(()=>j(Error('load timeout')),5000))]);
const expression=`JSON.stringify((()=>{addCustomModule();const card=[...document.querySelectorAll('.custom-module-card')].at(-1),row=document.querySelector('.module-row[data-module="'+CSS.escape(card.dataset.id)+'"]'),seconds=row.querySelector('.duration'),delay=card.querySelector('[data-field="speed"]'),loops=card.querySelector('[data-field="loops"]');const beforeLoops=loops.value,beforeDelay=delay.value;seconds.value='14.01';seconds.dispatchEvent(new Event('input',{bubbles:true}));addAnnouncement();const boardRow=document.querySelector('.module-row[data-module="announcement_board"]'),disabledAnnouncementSeconds=boardRow.querySelector('.duration').value;const announcement=announcements.at(-1);announcement.enabled=true;announcement.layout='text_notice';announcement.style.border='none';announcement.transition={type:'cut'};announcement.timing={mode:'scroll',loops:2,frame_delay:.05};renderAllAnnouncementEditors();announcementDraftMutated(announcement);const announcementSeconds=boardRow.querySelector('.duration').value,announcementPhysical=boardRow.querySelector('.physical-time').textContent;announcement.enabled=false;announcementDraftMutated(announcement);return {seconds:seconds.value,delay:delay.value,loops:loops.value,beforeLoops,beforeDelay,disabledAnnouncementSeconds,announcementSeconds,announcementPhysical,removedAnnouncementSeconds:boardRow.querySelector('.duration').value,removedAnnouncementPhysical:boardRow.querySelector('.physical-time').textContent,separateRuntimeControls:document.querySelectorAll('.scroll-seconds-editor').length,providerStates:[...document.querySelectorAll('[data-provider-timing-state]')].map(e=>e.textContent)}})())`;
const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw Error(JSON.stringify(result.exceptionDetails));console.log(result.result.value);socket.close();
"""
        with tempfile.TemporaryDirectory(prefix="matrix-seconds-") as directory:
            page = Path(directory) / "dashboard.html"
            page.write_text(body)
            profile = Path(directory) / "profile"
            browser = subprocess.Popen([chromium, "--headless", "--no-sandbox", "--disable-gpu", "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                active = profile / "DevToolsActivePort"
                for _ in range(50):
                    if active.is_file(): break
                    if browser.poll() is not None: self.fail("Chromium exited")
                    time.sleep(.1)
                port = active.read_text().splitlines()[0]
                completed = subprocess.run([shutil.which("node"), "-e", probe, port, str(page)], capture_output=True, text=True, timeout=15)
            finally:
                browser.terminate()
                try: browser.wait(5)
                except subprocess.TimeoutExpired: browser.kill(); browser.wait(5)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(result["seconds"], "14.01")
        self.assertEqual(result["loops"], result["beforeLoops"])
        self.assertNotEqual(result["delay"], result["beforeDelay"])
        self.assertRegex(result["delay"], r"^\d+\.\d{4,6}$")
        self.assertEqual(result["disabledAnnouncementSeconds"], "")
        self.assertGreater(float(result["announcementSeconds"]), 0)
        self.assertRegex(result["announcementPhysical"], r"^Physical time · \d+\.\d s$")
        self.assertEqual(result["removedAnnouncementSeconds"], "")
        self.assertEqual(result["removedAnnouncementPhysical"], "")
        self.assertEqual(result["separateRuntimeControls"], 0)
        self.assertEqual(len(result["providerStates"]), 3)
        self.assertTrue(all(state != "0.0 s" for state in result["providerStates"]))

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    @unittest.skipUnless(
        os.environ.get("MATRIX_CHROMIUM")
        and Path(os.environ.get("MATRIX_CHROMIUM", "")).is_file()
        and os.access(os.environ.get("MATRIX_CHROMIUM", ""), os.X_OK),
        "MATRIX_CHROMIUM browser is required",
    )
    def test_scrolling_timing_paired_responsive_integration(self):
        self.pair_browser()
        peer = self.app.test_client()
        self.pairing.verify.return_value = True
        peer.post("/pair", data={"pin": "123456", "device_name": "Peer browser"})
        base_revision = self.store.get()["revision"]
        original_loops = self.store.get()["weather"]["scroll_loops"]
        saved = self.client.post(
            "/api/config",
            json={"weather": {"scroll_speed": 0.055}},
            headers={"If-Match": f'"{base_revision}"'},
        )
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        self.assertEqual(saved.get_json()["weather"]["scroll_loops"], original_loops)
        reopened = self.client.get("/api/config")
        self.assertEqual(reopened.get_json()["weather"]["scroll_speed"], 0.055)
        stale = peer.post(
            "/api/config",
            json={"weather": {"scroll_speed": 0.06}},
            headers={"If-Match": f'"{base_revision}"'},
        )
        self.assertEqual(stale.status_code, 412)
        self.assertEqual(self.store.get()["weather"]["scroll_speed"], 0.055)

        self.scroll_timing_cache.get_or_fill(
            ("openweather", "openweather_overview", "digest-only", 0),
            lambda: {"milliseconds": 1000, "display": "1.0 s"},
        )
        self.assertTrue(self.scroll_timing_cache._entries)
        restarted_cache = ScrollTimingCache()
        self.assertFalse(restarted_cache._entries)

        self.controller.status.return_value = {
            "running": False, "current_module": None, "last_error": None,
        }
        body = self.client.get("/").get_data(as_text=True)
        chromium = os.environ["MATRIX_CHROMIUM"]
        node = shutil.which("node")
        assert node is not None
        expected_vectors = [
            frame_delay_for_seconds(1.25, 100, 1),
            frame_delay_for_seconds(0.1, 100, 2),
            frame_delay_for_seconds(999, 100, 2),
            frame_delay_for_seconds(9.84, 82, 3),
        ]
        probe = r"""
const port=process.argv[1],pagePath=process.argv[2],expected=JSON.parse(process.argv[3]);let endpoint;
for(let attempt=0;attempt<50;attempt++){try{endpoint=await fetch(`http://127.0.0.1:${port}/json/new`,{method:'PUT'}).then(r=>r.json());break}catch(_){await new Promise(r=>setTimeout(r,100))}}
if(!endpoint)throw Error('CDP unavailable');
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map(),exceptions=[];let id=0,resolveLoad,loaded=new Promise(r=>resolveLoad=r);
socket.onmessage=e=>{const m=JSON.parse(e.data);if(m.id&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result)}else if(m.method==='Page.loadEventFired')resolveLoad();else if(m.method==='Runtime.exceptionThrown')exceptions.push(m.params.exceptionDetails.text)};
await new Promise((r,j)=>{socket.onopen=r;socket.onerror=j});
const send=(method,params={})=>new Promise((resolve,reject)=>{const request=++id;pending.set(request,{resolve,reject});socket.send(JSON.stringify({id:request,method,params}))});
const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw Error(JSON.stringify(result.exceptionDetails));return result.result.value};
await send('Runtime.enable');await send('Page.enable');await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await send('Emulation.setTouchEmulationEnabled',{enabled:true,maxTouchPoints:5});await send('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});await send('Page.navigate',{url:'file://'+pagePath});await Promise.race([loaded,new Promise((_,j)=>setTimeout(()=>j(Error('load timeout')),5000))]);
const vectors=await evaluate(`JSON.stringify([[1.25,100,1],[.1,100,2],[999,100,2],[9.84,82,3]].map(v=>frameDelayForSeconds(...v)))`);
if(JSON.stringify(JSON.parse(vectors))!==JSON.stringify(expected))throw Error('Python/JavaScript timing vector mismatch');
const provider=await evaluate(`(async()=>{let aborted=0,calls=0;api=async(_path,options)=>{calls++;const payload=JSON.parse(options.body),digest=await timingSettingsDigest('weather',payload);await new Promise((resolve,reject)=>{const timer=setTimeout(resolve,30);options.signal.addEventListener('abort',()=>{aborted++;clearTimeout(timer);reject(new DOMException('Aborted','AbortError'))},{once:true})});return {status:'fresh',milliseconds:5000,frames_per_loop:100,loops:1,settings_digest:digest,settings_correlation:payload.settings_correlation}};const row=document.querySelector('.module-row[data-module="weather"]'),edit=row.querySelector('.module-actions button:last-child');edit.click();setTimeout(()=>{document.querySelector('#weather-location').value='NEW DRAFT'},5);await new Promise(r=>setTimeout(r,420));return JSON.stringify({calls,aborted,state:row.closest('.module-item').querySelector('[data-provider-timing-state]').textContent,hidden:row.closest('.module-item').querySelector('.module-editor').hidden})})()`);
const providerResult=JSON.parse(provider);if(providerResult.calls!==2||providerResult.hidden||!providerResult.state.includes('5.5 s'))throw Error('provider lifecycle mismatch: '+provider);
const announcementOrder=await evaluate(`(async()=>{const pending=[];api=(_path,options)=>new Promise(resolve=>pending.push({resolve,body:options.body}));const first=loadAnnouncementTiming();announcements.push({id:'newer'});const second=loadAnnouncementTiming();pending[1].resolve({status:'fresh',milliseconds:2000,refresh_at:new Date(Date.now()+60000).toISOString()});await second;pending[0].resolve({status:'fresh',milliseconds:1000,refresh_at:new Date(Date.now()+60000).toISOString()});await first;return announcementTimingPlan.milliseconds})()`);if(announcementOrder!==2000)throw Error('announcement timing regressed to older draft: '+announcementOrder);
await evaluate(`(()=>{addCustomModule();const input=[...document.querySelectorAll('.custom-module-card')].at(-1).querySelector('[data-field="text"]');input.value='PRESERVED DRAFT';input.dispatchEvent(new Event('input',{bubbles:true}));input.focus();return true})()`);
const viewports=[[320,568],[360,800],[390,844],[412,915],[599,900],[600,900],[601,900],[768,1024],[844,390],[899,800],[900,800],[901,800],[1024,768],[1280,720],[1440,900]],reports=[];
for(const [width,height] of viewports){await send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<1024,screenOrientation:{type:width>height?'landscapePrimary':'portraitPrimary',angle:width>height?90:0}});reports.push(JSON.parse(await evaluate(`JSON.stringify((()=>{const edit=document.querySelector('.module-row[data-module="weather"] .module-actions button:last-child'),input=[...document.querySelectorAll('.custom-module-card')].at(-1).querySelector('[data-field="text"]'),offender=[...document.querySelectorAll('body *')].map(element=>({element,right:element.getBoundingClientRect().right})).sort((a,b)=>b.right-a.right)[0];return {width:innerWidth,overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth,editWidth:edit.getBoundingClientRect().width,editHeight:edit.getBoundingClientRect().height,draft:input.value,offender:offender?.element?.outerHTML?.slice(0,180)}})())`)))}
await send('Emulation.setPageScaleFactor',{pageScaleFactor:2});const zoomDraft=await evaluate(`[...document.querySelectorAll('.custom-module-card')].at(-1).querySelector('[data-field="text"]').value`);await send('Emulation.setPageScaleFactor',{pageScaleFactor:1});
if(reports.some(r=>r.overflow>1||r.draft!=='PRESERVED DRAFT'))throw Error('responsive overflow or draft loss: '+JSON.stringify(reports));
if(reports.filter(r=>r.width<=600).some(r=>r.editWidth<44||r.editHeight<44))throw Error('mobile editor action below 44px: '+JSON.stringify(reports));
if(zoomDraft!=='PRESERVED DRAFT')throw Error('zoom discarded draft');
console.log(JSON.stringify({reports,provider:providerResult,exceptions}));socket.close();
"""
        with tempfile.TemporaryDirectory(prefix="matrix-integration-") as directory:
            page = Path(directory) / "dashboard.html"
            page.write_text(body)
            profile = Path(directory) / "profile"
            browser = subprocess.Popen(
                [chromium, "--headless", "--no-sandbox", "--disable-gpu", "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                active = profile / "DevToolsActivePort"
                for _attempt in range(50):
                    if active.is_file():
                        break
                    if browser.poll() is not None:
                        self.fail("Chromium exited before publishing its DevTools port")
                    time.sleep(0.1)
                self.assertTrue(active.is_file(), "Chromium DevTools port unavailable")
                port = active.read_text().splitlines()[0]
                completed = subprocess.run(
                    [node, "-e", probe, port, str(page), json.dumps(expected_vectors)],
                    capture_output=True, text=True, timeout=30,
                )
            finally:
                browser.terminate()
                try:
                    browser.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    browser.kill()
                    browser.wait(timeout=5)
                for cleanup_attempt in range(20):
                    try:
                        shutil.rmtree(profile)
                        break
                    except FileNotFoundError:
                        break
                    except OSError:
                        if cleanup_attempt == 19:
                            raise
                        time.sleep(0.05)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(report["exceptions"], [])
        self.assertEqual(len(report["reports"]), 15)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    @unittest.skipUnless(
        os.environ.get("MATRIX_CHROMIUM")
        and Path(os.environ.get("MATRIX_CHROMIUM", "")).is_file()
        and os.access(os.environ.get("MATRIX_CHROMIUM", ""), os.X_OK),
        "MATRIX_CHROMIUM browser is required",
    )
    def test_live_http_pairing_timing_contracts_and_state_preservation(self):
        self.pairing.active_pin.return_value = "123456"
        self.pairing.verify.return_value = True
        self.controller.status.return_value = {
            "running": False, "current_module": None, "last_error": None,
        }
        frame = io.BytesIO()
        Image.new("RGB", (64, 32)).save(frame, format="PNG")
        frame_bytes = frame.getvalue()
        self.controller.frame_png.side_effect = lambda: (io.BytesIO(frame_bytes), 1)
        current = self.store.get()

        def scenario_settings(name):
            settings = copy.deepcopy(current["weather"])
            settings.update({"location": name, "latitude": None, "longitude": None})
            return settings

        def digest_for(settings):
            source = {"module": "weather", "settings": settings}
            return hashlib.sha256(json.dumps(
                source, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()

        scenarios = {
            digest_for(scenario_settings("STALE")): "stale",
            digest_for(scenario_settings("BUSY")): "busy",
            digest_for(scenario_settings("UNAVAILABLE")): "unavailable",
        }
        provider_locations = []

        def prepare(config, *, deadline):
            del deadline
            location = config["weather"]["location"]
            provider_locations.append(location)
            if location == "OLD":
                time.sleep(.7)
                milliseconds = 7000
            elif location == "NEW":
                time.sleep(.1)
                milliseconds = 9000
            else:
                milliseconds = 5500
            loops = config["weather"]["scroll_loops"]
            delay = config["weather"]["scroll_speed"]
            frames = {"OLD": 127, "NEW": 164}.get(location, 100)
            actual_milliseconds = round(frames * loops * delay * 1000)
            return {
                "frames_per_loop": frames, "total_frames": frames * loops,
                "loops": loops,
                "frame_delay": delay,
                "milliseconds": actual_milliseconds,
                "display": f"{actual_milliseconds / 1000:.1f} s",
            }

        def cached(key, fill, *, deadline=None):
            del deadline
            scenario = scenarios.get(key[2])
            if scenario == "busy":
                raise TimingCacheBusy("busy")
            if scenario == "unavailable":
                raise OSError("provider unavailable")
            if scenario == "stale":
                return {
                    "frames_per_loop": 100, "total_frames": 100, "loops": 1,
                    "frame_delay": .05, "milliseconds": 6000, "display": "6.0 s",
                    "status": "stale", "cache": "stale",
                }
            return {**fill(), "status": "fresh", "cache": "fill"}

        server = make_server("127.0.0.1", 0, self.app, threaded=True)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        chromium = os.environ["MATRIX_CHROMIUM"]
        node = shutil.which("node")
        assert node is not None
        probe = r"""
const debugPort=process.argv[1],appPort=process.argv[2],sleep=ms=>new Promise(r=>setTimeout(r,ms));let endpoint;
for(let attempt=0;attempt<50;attempt++){try{endpoint=await fetch(`http://127.0.0.1:${debugPort}/json/new`,{method:'PUT'}).then(r=>r.json());break}catch(_){await sleep(100)}}
if(!endpoint)throw Error('CDP unavailable');
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map(),exceptions=[];let id=0;
socket.onmessage=e=>{const m=JSON.parse(e.data);if(m.id&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result)}else if(m.method==='Runtime.exceptionThrown')exceptions.push(m.params.exceptionDetails.text)};
await new Promise((r,j)=>{socket.onopen=r;socket.onerror=j});
const send=(method,params={})=>new Promise((resolve,reject)=>{const request=++id;pending.set(request,{resolve,reject});socket.send(JSON.stringify({id:request,method,params}))});
const evaluate=async(expression,awaitPromise=false)=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise});if(result.exceptionDetails)throw Error(JSON.stringify(result.exceptionDetails));return result.result.value};
const waitFor=async(expression,timeout=10000)=>{const end=Date.now()+timeout;while(Date.now()<end){try{const value=await evaluate(expression);if(value)return value}catch(_){}await sleep(25)}throw Error('timeout: '+expression)};
try{
 await send('Runtime.enable');await send('Page.enable');
 await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 await send('Emulation.setTouchEmulationEnabled',{enabled:true,maxTouchPoints:5});
 await send('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/pair`});
 await waitFor(`document.querySelector('input[name="device_name"]')`);
 const unauthorized=await evaluate(`fetch('/api/scroll-timing/weather',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(r=>r.status)`,true);
 if(unauthorized!==401)throw Error('unauthorized contract mismatch: '+unauthorized);
 await evaluate(`document.querySelector('input[name="device_name"]').focus();true`);
 await send('Input.insertText',{text:'Live HTTP Chromium'});
 await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Tab',code:'Tab'});await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Tab',code:'Tab'});
 await evaluate(`document.querySelector('input[name="pin"]').focus();true`);
 await send('Input.insertText',{text:'123456'});
 const pairButton=await evaluate(`(()=>{const r=document.querySelector('form button').getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})()`);
 await send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:pairButton.x,y:pairButton.y}]});await send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
 await waitFor(`location.pathname==='/'&&typeof api==='function'&&document.readyState==='complete'`);
 await waitFor(`providerTimingPlans.get('weather')?.status==='fresh'&&document.querySelector('.module-row[data-module="weather"] .duration').value==='5.00'`);
 const initialTiming=await evaluate(`(()=>{const row=document.querySelector('.module-row[data-module="weather"]'),editor=row.closest('.module-item').querySelector('.module-editor'),duration=row.querySelector('.duration');return {editorHidden:editor.hidden,duration:duration.value,readOnly:duration.readOnly,physical:row.querySelector('.physical-time').textContent}})()`);
 if(!initialTiming.editorHidden||initialTiming.readOnly||initialTiming.duration!=='5.00'||initialTiming.physical!=='Physical time · 5.0 s')throw Error('initial timing was not calculated before editing: '+JSON.stringify(initialTiming));
 const contracts=await evaluate(`(async()=>{
   const make=name=>{const payload=providerTimingPayload('weather');payload.settings.location=name;payload.settings.latitude=null;payload.settings.longitude=null;return payload};
   const freshPayload=providerTimingPayload('weather'),fresh=await api('/api/scroll-timing/weather',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(freshPayload)}),freshDigest=await timingSettingsDigest('weather',freshPayload);
   const raw=async payload=>{const response=await fetch('/api/scroll-timing/weather',{method:'POST',headers:{'Content-Type':'application/json','X-Matrix-Client-Contract':String(MATRIX_CLIENT_CONTRACT)},body:JSON.stringify(payload)});return {status:response.status,data:await response.json(),retry:response.headers.get('Retry-After')}};
   const stale=await raw(make('STALE')),busy=await raw(make('BUSY')),unavailable=await raw(make('UNAVAILABLE'));
   const staleBase=make('STALE');staleBase.base_revision=-1;const precondition=await raw(staleBase);
   const oversizedResponse=await fetch('/api/scroll-timing/weather',{method:'POST',headers:{'Content-Type':'application/json','X-Matrix-Client-Contract':String(MATRIX_CLIENT_CONTRACT)},body:'{"settings":"'+'x'.repeat(17000)+'"}'});
   return {fresh,freshDigest,stale,busy,unavailable,precondition,oversized:oversizedResponse.status};
 })()`,true);
 if(contracts.fresh.status!=='fresh'||contracts.fresh.settings_digest!==contracts.freshDigest)throw Error('fresh digest contract mismatch: '+JSON.stringify(contracts));
 if(contracts.stale.status!==200||contracts.stale.data.status!=='stale')throw Error('stale contract mismatch: '+JSON.stringify(contracts.stale));
 if(contracts.precondition.status!==412||typeof contracts.precondition.data.revision!=='number')throw Error('412 contract mismatch');
 if(contracts.oversized!==413)throw Error('413 contract mismatch: '+contracts.oversized);
 if(contracts.busy.status!==429||contracts.busy.retry!=='1'||!contracts.busy.data.retryable)throw Error('429 contract mismatch');
 if(contracts.unavailable.status!==503||contracts.unavailable.data.status!=='unavailable')throw Error('503 contract mismatch');
 await evaluate(`providerTimingPlans.delete('weather');syncPlaylistDurations();true`);
 const fallback=await evaluate(`(()=>{const row=document.querySelector('.module-row[data-module="weather"]'),duration=row.querySelector('.duration'),loops=builtInSettings.weather.scroll_loops,delay=builtInSettings.weather.scroll_speed;duration.value='6.0';duration.dispatchEvent(new Event('input',{bubbles:true}));return {readOnly:duration.readOnly,physical:row.querySelector('.physical-time').textContent,loopsBefore:loops,loopsAfter:builtInSettings.weather.scroll_loops,delayBefore:delay,delayAfter:builtInSettings.weather.scroll_speed}})()`);
 if(fallback.readOnly||fallback.physical!=='Physical time · 00:00:06'||fallback.loopsAfter!==fallback.loopsBefore||fallback.delayAfter!==fallback.delayBefore)throw Error('unresolved duration fallback mismatch: '+JSON.stringify(fallback));
 await evaluate(`(()=>{const button=document.querySelector('.module-row[data-module="weather"] .module-actions button:last-child');button.scrollIntoView({block:'center',behavior:'instant'});button.click();return true})()`);
 await waitFor(`!document.querySelector('.module-row[data-module="weather"]').closest('.module-item').querySelector('.module-editor').hidden`);
 const resolvedFallback=await evaluate(`(()=>{const row=document.querySelector('.module-row[data-module="weather"]'),editor=row.closest('.module-item').querySelector('.module-editor');return {loops:Number(editor.querySelector('[data-setting="scroll_loops"]').value),delay:Number(editor.querySelector('[data-setting="scroll_speed"]').value),duration:Number(row.querySelector('.duration').value),physical:row.querySelector('.physical-time').textContent}})()`);
 if(resolvedFallback.loops!==fallback.loopsBefore||resolvedFallback.delay===fallback.delayBefore||!resolvedFallback.physical.endsWith(' s'))throw Error('resolved duration did not adjust delay only: '+JSON.stringify({fallback,resolvedFallback}));
 let timingRect=await evaluate(`(()=>{const e=document.querySelector('.module-row[data-module="weather"] .duration');e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})()`);
 await send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:timingRect.x,y:timingRect.y}]});await send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
 await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Control',code:'ControlLeft',modifiers:2});await send('Input.dispatchKeyEvent',{type:'keyDown',key:'a',code:'KeyA',modifiers:2});await send('Input.dispatchKeyEvent',{type:'keyUp',key:'a',code:'KeyA',modifiers:2});await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Control',code:'ControlLeft'});await evaluate(`document.activeElement.value=''`);await send('Input.insertText',{text:'6.0'});
 await evaluate(`(()=>{const input=document.querySelector('#weather-location');input.value='OLD';input.dispatchEvent(new Event('input',{bubbles:true}));return true})()`);
 await sleep(350);await waitFor(`document.querySelector('[data-provider-timing-state]').dataset.state==='loading'`);
 await evaluate(`(()=>{const input=document.querySelector('#weather-location'),editor=document.querySelector('.module-row[data-module="weather"]').closest('.module-item').querySelector('.module-editor');input.value='NEW';input.dispatchEvent(new Event('input',{bubbles:true}));input.setSelectionRange(1,2);const timing=document.querySelector('.module-row[data-module="weather"] .duration');timing.focus();editor.style.maxHeight='120px';editor.style.overflow='auto';editor.scrollTop=80;scrollTo(0,400);return {page:scrollY,local:editor.scrollTop}})()`);
 const before=await evaluate(`(()=>{const editor=document.querySelector('.module-row[data-module="weather"]').closest('.module-item').querySelector('.module-editor');return {page:scrollY,local:editor.scrollTop,seconds:document.querySelector('.module-row[data-module="weather"] .duration').value}})()`);
 await sleep(1000);
 const preserved=await evaluate(`(async()=>{const editor=document.querySelector('.module-row[data-module="weather"]').closest('.module-item').querySelector('.module-editor'),timing=document.querySelector('.module-row[data-module="weather"] .duration'),locationInput=document.querySelector('#weather-location'),payload=providerTimingPayload('weather');return {focus:document.activeElement===timing,seconds:timing.value,selection:[locationInput.selectionStart,locationInput.selectionEnd],page:scrollY,local:editor.scrollTop,digest:providerTimingPlans.get('weather').settings_digest,currentDigest:await timingSettingsDigest('weather',payload),frames:providerTimingPlans.get('weather').frames_per_loop,delay:document.querySelector('[data-built-in-module="weather"][data-setting="scroll_speed"]').value,state:editor.querySelector('[data-provider-timing-state]').textContent}})()`,true);
 const expectedSeconds=before.seconds;
 if(!preserved.focus||preserved.seconds!==expectedSeconds||preserved.state!==expectedSeconds+' s'||JSON.stringify(preserved.selection)!=='[1,2]'||Math.abs(preserved.page-before.page)>1||preserved.local!==before.local||preserved.digest!==preserved.currentDigest)throw Error('provider completion discarded state: '+JSON.stringify({before,preserved,expectedSeconds}));
 const reduced=await evaluate(`({matches:matchMedia('(prefers-reduced-motion: reduce)').matches,duration:getComputedStyle(document.querySelector('.palette-group summary'),'::before').transitionDuration})`);
 if(!reduced.matches||reduced.duration!=='0s')throw Error('reduced motion not honored: '+JSON.stringify(reduced));
 await send('Emulation.setPageScaleFactor',{pageScaleFactor:2});
 const zoom=await evaluate(`(()=>{const input=document.querySelector('.module-row[data-module="weather"] .duration'),label=input.closest('.duration-label'),style=getComputedStyle(input);return {scale:visualViewport.scale,overflow:label.scrollWidth-label.clientWidth,width:input.getBoundingClientRect().width,font:parseFloat(style.fontSize),label:label.textContent}})()`);
 if(zoom.scale<1.9||zoom.overflow>1||zoom.width<44||zoom.font<12||!zoom.label.includes('Display duration (seconds)'))throw Error('200% zoom timing control unreadable: '+JSON.stringify(zoom));
 console.log(JSON.stringify({contracts,preserved,reduced,zoom,exceptions}));
}finally{socket.close()}
"""
        with tempfile.TemporaryDirectory(prefix="matrix-live-http-") as directory, \
             patch.object(self.scroll_timing_cache, "get_or_fill", side_effect=cached), \
             patch("modules.weather_app.prepare_ticker_plan", side_effect=prepare):
            profile = Path(directory) / "profile"
            browser = subprocess.Popen([
                chromium, "--headless", "--no-sandbox", "--disable-gpu",
                "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                active = profile / "DevToolsActivePort"
                for _attempt in range(50):
                    if active.is_file():
                        break
                    if browser.poll() is not None:
                        self.fail("Chromium exited before publishing DevTools port")
                    time.sleep(.1)
                self.assertTrue(active.is_file(), "Chromium DevTools port unavailable")
                completed = subprocess.run([
                    node, "-e", probe, active.read_text().splitlines()[0], str(server.server_port),
                ], capture_output=True, text=True, timeout=30)
            finally:
                try:
                    os.killpg(browser.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    browser.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(browser.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    browser.wait(timeout=5)
                time.sleep(.1)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(report["exceptions"], [])
        self.assertIn("OLD", provider_locations)
        self.assertIn("NEW", provider_locations)

    def test_real_chromium_regression_skips_a_non_executable_browser_path(self):
        with tempfile.NamedTemporaryFile() as candidate:
            Path(candidate.name).chmod(0o600)
            environment = dict(
                os.environ,
                MATRIX_CHROMIUM=candidate.name,
                PYTHONDONTWRITEBYTECODE="1",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "unittest",
                    "tests.test_webapp.WebAppTests."
                    "test_playlist_timing_and_reorder_controls_in_real_chromium_at_320px",
                    "-v",
                ],
                cwd=Path(__file__).parents[1],
                env=environment,
                capture_output=True,
                text=True,
                timeout=15,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("skipped", completed.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_provider_completion_preserves_focused_hundredth_target_and_delay_precision(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        timing_start = body.index("const MATRIX_SCROLL_WIDTH=")
        timing_end = body.index("\nfunction fontCharacterAdvance", timing_start)
        ticker_start = body.index("function tickerFrames(")
        ticker_end = body.index("\nfunction onPlaylistDurationInput", ticker_start)
        load_start = body.index("async function loadProviderTiming(")
        load_end = body.index("\nlet announcementTimingRequest", load_start)
        harness = (
            body[timing_start:timing_end] + "\n" +
            body[ticker_start:ticker_end] + "\n" +
            body[load_start:load_end] + r"""
const providerTimingPlans=new Map(),providerTimingRequests=new Map();
const playlistDurationTargets=new Map([['weather',6.01]]);
const builtInSettings={weather:{scroll_loops:1,scroll_speed:.05}};
const duration={value:'6.01'},delay={value:'.05'},state={textContent:'',dataset:{}};
const container={querySelector(selector){return selector==='[data-provider-timing-state]'?state:null}};
global.document={
  activeElement:duration,
  querySelector(selector){
    if(selector.includes('.module-row'))return duration;
    if(selector.includes('[data-setting="scroll_speed"]'))return delay;
    return null;
  },
};
function markMatrixDirty(){}
function providerTimingPayload(){return {settings:{}}}
async function timingSettingsDigest(){return 'same'}
function providerPlanMilliseconds(){return 6010}
function syncPlaylistDurations(){}
async function api(){return {status:'fresh',frames_per_loop:127,settings_digest:'same',settings_correlation:'same'}}
(async()=>{
  await loadProviderTiming('weather',container);
  if(document.activeElement!==duration)throw new Error('duration focus was lost');
  if(duration.value!=='6.01')throw new Error('focused hundredth target was rounded: '+duration.value);
  if(Number(delay.value)!==0.047323||builtInSettings.weather.scroll_speed!==0.047323)throw new Error('microsecond frame delay precision was lost: '+JSON.stringify({control:delay.value,setting:builtInSettings.weather.scroll_speed,state}));
  console.log('PROVIDER_HUNDREDTH_COMPLETION_PASS');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
        )
        result = subprocess.run(
            [shutil.which("node") or "", "-e", harness],
            cwd=Path(__file__).parents[1], text=True, capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PROVIDER_HUNDREDTH_COMPLETION_PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_announcement_unavailable_response_and_failure_preserve_enabled_draft_timing(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        function_start = body.index("const customFontAdvances=")
        function_end = body.index("\nfunction playlist()", function_start)
        load_start = body.index("async function loadAnnouncementTiming()")
        load_end = body.index("\nlet announcementTimingDebounce", load_start)
        harness = body[function_start:function_end] + "\n" + body[load_start:load_end] + r"""
const announcement={
  enabled:true,headline:'FIRST ANNOUNCEMENT',message:'',style:{font:'5x7'},
  timing:{mode:'static',duration_seconds:8},transition:{type:'cut'},
};
const announcements=[announcement];
const builtInSettings={};
const matrixLoadedRevision=0;let announcementTimingRequest=null,announcementTimingGeneration=0;
const label={childNodes:[{textContent:''}]},physical={textContent:''};let row;
const duration={value:'',hidden:false,readOnly:false,title:'',min:'',max:'',step:'',placeholder:'',dataset:{},closest(selector){return selector==='.module-row'?row:label},removeAttribute(name){delete this[name]}};
row={dataset:{module:'announcement_board'},querySelector(selector){return selector==='.duration'?duration:selector==='.physical-time'?physical:null}};
global.document={activeElement:null,querySelector(){return null},querySelectorAll(selector){if(selector==='.module-row')return [row];if(selector==='.custom-module-card')return [];return []},createElement(){return {setAttribute(){}}}};
global.setTimeout=()=>0;global.clearTimeout=()=>{};
function customModulesFromEditor(){return []}
function announcementsFromEditor(){return structuredClone(announcements)}
syncPlaylistDurations();
if(Number(duration.value)!==8||physical.textContent!=='Physical time · 8.0 s')throw new Error('first enabled draft did not calculate before server timing');
api=async()=>({status:'unavailable',milliseconds:null,refresh_at:new Date(Date.now()+60000).toISOString()});
(async()=>{
  await loadAnnouncementTiming();
  if(Number(duration.value)!==8||physical.textContent!=='Physical time · 8.0 s')throw new Error('explicit unavailable response reset first enabled draft timing: '+JSON.stringify({value:duration.value,physical:physical.textContent}));
  api=async()=>{throw new Error('offline')};
  await loadAnnouncementTiming();
  if(Number(duration.value)!==8||physical.textContent!=='Physical time · 8.0 s')throw new Error('failed request reset first enabled draft timing: '+JSON.stringify({value:duration.value,physical:physical.textContent}));
  announcement.enabled=false;announcementTimingPlan=null;syncPlaylistDurations();
  if(duration.value!==''||physical.textContent!=='')throw new Error('disabled final announcement did not clear timing');
  console.log('ANNOUNCEMENT_FIRST_DRAFT_TIMING_PASS');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
        result = subprocess.run(
            [shutil.which("node") or "", "-e", harness],
            cwd=Path(__file__).parents[1], text=True, capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ANNOUNCEMENT_FIRST_DRAFT_TIMING_PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_announcement_server_timing_does_not_replace_multiple_enabled_draft_total(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        function_start = body.index("const customFontAdvances=")
        function_end = body.index("\nfunction playlist()", function_start)
        load_start = body.index("async function loadAnnouncementTiming()")
        load_end = body.index("\nlet announcementTimingDebounce", load_start)
        harness = body[function_start:function_end] + "\n" + body[load_start:load_end] + r"""
const announcements=[
  {enabled:true,headline:'FIRST',message:'',style:{font:'5x7'},timing:{mode:'static',duration_seconds:8},transition:{type:'cut'}},
  {enabled:true,headline:'SECOND',message:'',style:{font:'5x7'},timing:{mode:'static',duration_seconds:12},transition:{type:'cut'}},
];
const builtInSettings={};
const matrixLoadedRevision=0;let announcementTimingRequest=null,announcementTimingGeneration=0;
const label={childNodes:[{textContent:''}]},physical={textContent:''};let row;
const duration={value:'',hidden:false,readOnly:false,title:'',min:'',max:'',step:'',placeholder:'',dataset:{},closest(selector){return selector==='.module-row'?row:label},removeAttribute(name){delete this[name]}};
row={dataset:{module:'announcement_board'},querySelector(selector){return selector==='.duration'?duration:selector==='.physical-time'?physical:null}};
global.document={activeElement:null,querySelector(){return null},querySelectorAll(selector){if(selector==='.module-row')return [row];if(selector==='.custom-module-card')return [];return []},createElement(){return {setAttribute(){}}}};
global.setTimeout=()=>0;global.clearTimeout=()=>{};
function customModulesFromEditor(){return []}
function announcementsFromEditor(){return structuredClone(announcements)}
syncPlaylistDurations();
if(Number(duration.value)!==20||physical.textContent!=='Physical time · 20.0 s')throw new Error('multiple enabled drafts did not calculate their combined total');
api=async()=>({status:'fresh',milliseconds:8000,refresh_at:new Date(Date.now()+60000).toISOString()});
(async()=>{
  await loadAnnouncementTiming();
  if(Number(duration.value)!==20||physical.textContent!=='Physical time · 20.0 s')throw new Error('schedule-active server subset replaced multiple enabled draft total: '+JSON.stringify({value:duration.value,physical:physical.textContent}));
  console.log('ANNOUNCEMENT_MULTIPLE_DRAFT_TOTAL_PASS');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
        result = subprocess.run(
            [shutil.which("node") or "", "-e", harness],
            cwd=Path(__file__).parents[1], text=True, capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ANNOUNCEMENT_MULTIPLE_DRAFT_TOTAL_PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_playlist_physical_time_readouts_synchronize_in_the_browser(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        function_start = body.index("const customFontAdvances=")
        function_end = body.index("\nfunction playlist()", function_start)
        function_source = body[function_start:function_end]
        harness = function_source + r"""
const custom={id:'custom_scroll',type:'text_scroll',text:'A',font:'5x7',loops:2,speed:.05};
const builtInSettings={weather:{scroll_loops:1,scroll_speed:.05}};
function makeRow(module,value){
  const label={childNodes:[{textContent:''}]};
  const physical={textContent:''};let row;
  const duration={value:String(value),hidden:false,readOnly:false,title:'',min:'',max:'',step:'',dataset:{},closest(selector){return selector==='.module-row'?row:label},removeAttribute(name){delete this[name]}};
  row={dataset:{module},duration,physical,querySelector(selector){return selector==='.duration'?duration:selector==='.physical-time'?physical:null}};return row;
}
const rows=[makeRow('splash',90),makeRow('custom_scroll',1),makeRow('weather',9),makeRow('announcement_board','')];
const announcements=[];
const runtime={hidden:true,textContent:''};
const card={dataset:{id:'custom_scroll'},querySelector(selector){return selector==='.scroll-runtime'?runtime:null}};
global.document={querySelector(){return null},querySelectorAll(selector){if(selector==='.module-row')return rows;if(selector==='.custom-module-card')return [card];return []},createElement(){return {setAttribute(){}}}};
function markMatrixDirty(){}
function customModulesFromEditor(){return [custom]}
syncPlaylistDurations();
if(rows[3].duration.value!==''||/\d/.test(rows[3].physical.textContent))throw new Error('empty announcement board should not show a duration');
announcements.push({id:'announcement_draft',enabled:false,headline:'NEW ANNOUNCEMENT',message:'',style:{font:'5x7'},timing:{mode:'static',duration_seconds:8},transition:{type:'cut'}});
syncPlaylistDurations();
if(rows[3].duration.value!==''||/\d/.test(rows[3].physical.textContent))throw new Error('disabled announcement should not show a board duration');
announcements[0].enabled=true;
syncPlaylistDurations();
if(Number(rows[3].duration.value)!==8||rows[3].physical.textContent!=='Physical time · 8.0 s')throw new Error('enabling announcement did not update board duration');
announcements[0].enabled=false;
syncPlaylistDurations();
if(rows[3].duration.value!==''||/\d/.test(rows[3].physical.textContent))throw new Error('removing the last enabled announcement did not clear board duration');
announcements[0].enabled=true;
syncPlaylistDurations();
if(rows[2].duration.readOnly || rows[2].physical.textContent!=='Physical time · 00:00:09' || Number(rows[2].duration.value)!==9)throw new Error('unresolved weather runtime did not use editable playlist fallback');
const weatherLoopsBefore=builtInSettings.weather.scroll_loops,weatherDelayBefore=builtInSettings.weather.scroll_speed;
rows[2].duration.value='12.0';onPlaylistDurationInput({currentTarget:rows[2].duration});
if(builtInSettings.weather.scroll_loops!==weatherLoopsBefore||builtInSettings.weather.scroll_speed!==weatherDelayBefore||rows[2].physical.textContent!=='Physical time · 00:00:12')throw new Error('unresolved weather edit changed timing before frames were known');
providerTimingPlans.set('weather',{frames_per_loop:100,status:'fresh'});
const pending=setTickerDelay('weather',null,playlistDurationTargets.get('weather'));syncPlaylistDurations();
if(!pending||builtInSettings.weather.scroll_loops!==weatherLoopsBefore||builtInSettings.weather.scroll_speed!==0.12||Number(rows[2].duration.value)!==12)throw new Error('resolved weather target did not adjust frame delay only');
if(rows[0].physical.textContent!=='Physical time · 00:01:30')throw new Error('fixed runtime did not synchronize');
if(Number(rows[1].duration.value)!==7 || rows[1].physical.textContent!=='Physical time · 7.0 s')throw new Error('scroll runtime did not synchronize: '+JSON.stringify({value:rows[1].duration.value,physical:rows[1].physical.textContent}));
if(rows[1].duration.hidden || rows[1].duration.readOnly || rows[1].duration.closest().childNodes[0].textContent!=='Display duration (seconds) ')throw new Error('calculated scroll row did not match editable Lava layout');
if(rows[2].duration.hidden || rows[2].duration.readOnly || rows[2].duration.closest().childNodes[0].textContent!=='Display duration (seconds) ')throw new Error('provider row did not match editable Lava layout');
const loopsBefore=custom.loops;rows[1].duration.value='14.0';onPlaylistDurationInput({currentTarget:rows[1].duration});
if(custom.loops!==loopsBefore||custom.speed!==0.1||Number(rows[1].duration.value)!==14)throw new Error('editing duration did not change frame delay only: '+JSON.stringify(custom));
const delayBeforeHundredth=custom.speed;rows[1].duration.value='14.01';onPlaylistDurationInput({currentTarget:rows[1].duration});
if(custom.loops!==loopsBefore||custom.speed===delayBeforeHundredth||rows[1].duration.value!=='14.01')throw new Error('one-hundredth duration edit was not visibly reflected in frame delay: '+JSON.stringify({duration:rows[1].duration.value,speed:custom.speed}));
if(rows[2].physical.textContent!=='Physical time · 12.0 s' || Number(rows[2].duration.value)!==12)throw new Error('weather runtime did not use resolved scroll target');
if(runtime.hidden || runtime.textContent!=='Physical time · 14.0 s')throw new Error('custom editor badge did not synchronize');
console.log('PLAYLIST_TIMING_DOM_PASS');
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PLAYLIST_TIMING_DOM_PASS", result.stdout)

    def test_announcement_board_has_friendly_label_and_automatic_playlist_timing(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn(">Announcement Board<", body)
        self.assertIn(
            "const automaticBoardModules=new Set(['announcement_board'])",
            body,
        )
        self.assertIn("loadAnnouncementTiming", body)
        self.assertIn("announcementTimingPlan", body)

    def test_scrolling_seconds_resync_and_provider_digest_lifecycle_are_wired(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("stableJSONStringify", body)
        self.assertIn("currentDigest!==result.settings_correlation", body)
        self.assertIn("scheduleProviderTiming", body)
        self.assertIn("providerTimingPlans.get", body)
        self.assertIn("data-scroll-geometry", body)
        self.assertNotIn("Physical time · calculated at playback", body)

    def test_scrolling_text_duration_is_automatic_in_the_playlist_editor(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("function calculatedScrollDuration(module)", body)
        self.assertIn("duration.readOnly=isBoardAutomatic", body)
        self.assertIn("function onPlaylistDurationInput(event)", body)
        self.assertIn("const result=setTickerDelay(moduleName,module,requested)", body)
        self.assertIn("duration.hidden=false", body)
        self.assertNotIn("Content-controlled runtime", body)
        self.assertNotIn("Derived scroll runtime", body)
        self.assertIn("physicalTime.textContent='Physical time · '+formatPhysicalTime(duration.value)", body)
        self.assertIn("calculatedScrollDuration(module)", body)

    def test_automatic_ticker_rows_do_not_render_playlist_defaults_as_runtime(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        row = re.search(r'data-module="weather".*?class="duration"[^>]+', body)
        if row is None:
            self.fail("weather duration input was not rendered")
        self.assertIn('value=""', row.group(0))
        self.assertRegex(row.group(0), r'data-pending-duration="[0-9.]+"')

    def test_timing_increments_are_consistent_across_editors(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn('class="duration" type="number" min="1" max="3600" step=".01"', body)
        self.assertIn("duration.step=.01", body)
        self.assertNotIn("duration.step=1", body)
        self.assertIn("announcementStaticTimingFields=[{label:'Display duration (seconds)',path:'timing.duration_seconds',type:'number',min:1,max:3600,step:.01}]", body)
        self.assertEqual(body.count("label:'Frame delay (seconds)',type:'number',min:.01,max:1,step:.000001"), 3)
        self.assertIn("if(field==='speed'){control.min=.01;control.max=1;control.step=.000001}", body)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_provider_timing_small_exponential_coordinates_correlate_once(self):
        self.pairing.active_pin.return_value = "123456"
        self.pairing.verify.return_value = True
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        provider_start = body.index("function providerTimingPayload(module)")
        provider_end = body.index("\nlet announcementTimingRequest", provider_start)
        shipped_provider_handling = body[provider_start:provider_end]
        settings = copy.deepcopy(self.store.get()["weather"])
        settings.update({"latitude": 1e-7, "longitude": -1e-7})
        plan = {
            "frames_per_loop": 100, "total_frames": 100, "loops": 1,
            "frame_delay": .05, "milliseconds": 5000, "display": "5.0 s",
        }
        server = make_server("127.0.0.1", 0, self.app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        harness = "(async()=>{\n" + shipped_provider_handling + r"""
const base=process.argv[1],builtInSettings={weather:JSON.parse(process.argv[2])};
const matrixLoadedRevision=Number(process.argv[3]);
const providerTimingPlans=new Map(),providerTimingRequests=new Map();
const playlistDurationTargets=new Map();
const state={textContent:'',dataset:{}};
const container={querySelector(selector){return selector==='[data-provider-timing-state]'?state:null}};
const controls={
  '#weather-location':{value:builtInSettings.weather.location},
  '#weather-latitude':{value:'1e-7'},'#weather-longitude':{value:'-1e-7'},
  '#weather-units':{value:builtInSettings.weather.units},
};
global.document={activeElement:null,querySelector(selector){return controls[selector]||null}};
function providerPlanMilliseconds(_module,result){return result.milliseconds}
function oneDecimalSeconds(milliseconds){return (milliseconds/1000).toFixed(1)+' s'}
function syncPlaylistDurations(){}
function setTickerDelay(){throw new Error('unexpected duration target')}
let attempts=0,accepted=0;
async function api(path,options){
  attempts++;
  if(attempts>4)throw new Error('recursive request guard');
  const response=await fetch(base+path,{...options,headers:{...options.headers,Cookie:cookie}});
  if(response.ok)accepted++;
  const data=await response.json();
  if(!response.ok)throw new Error(data.error||response.statusText);
  return data;
}
const pair=await fetch(base+'/pair',{method:'POST',redirect:'manual',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({device_name:'Exponential coordinate probe',pin:'123456'})});
if(pair.status!==302)throw new Error('pairing failed: '+pair.status);
const cookie=pair.headers.get('set-cookie').split(';',1)[0];
const correlated=await loadProviderTiming('weather',container);
const currentPayload=providerTimingPayload('weather');
const currentDigest=await timingSettingsDigest('weather',currentPayload);
const result=providerTimingPlans.get('weather')||null;
console.log(JSON.stringify({attempts,accepted,correlated,state,result,currentDigest}));
})().catch(error=>{console.error(error);process.exitCode=1});
"""
        with patch("modules.weather_app.prepare_ticker_plan", return_value=plan):
            completed = subprocess.run(
                [
                    shutil.which("node") or "", "-e", harness,
                    f"http://127.0.0.1:{server.server_port}",
                    json.dumps(settings), str(self.store.get()["revision"]),
                ],
                cwd=Path(__file__).parents[1], capture_output=True, text=True,
                timeout=15,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(report["accepted"], 1, report)
        self.assertEqual(report["attempts"], 1, report)
        self.assertTrue(report["correlated"], report)
        self.assertEqual(report["result"]["status"], "fresh", report)
        self.assertEqual(report["result"]["settings_correlation"], report["currentDigest"])
        self.assertNotEqual(report["result"]["settings_digest"], report["currentDigest"])
        cache_keys = list(self.scroll_timing_cache._entries)
        self.assertEqual(len(cache_keys), 1, cache_keys)
        self.assertEqual(cache_keys[0][2], report["result"]["settings_digest"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_provider_timing_route_digest_matches_browser_effective_unicode_drafts(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        shared_start = body.index("function withSharedWeatherLocation(settings)")
        shared_end = body.index("\nfunction configBody", shared_start)
        provider_start = body.index("function providerTimingPayload(module)")
        provider_end = body.index("\nfunction scheduleProviderTiming", provider_start)
        functions = body[shared_start:shared_end] + "\n" + body[provider_start:provider_end]
        settings = {
            "weather": self.store.get()["weather"],
            "openweather_overview": self.store.get()["openweather_overview"],
        }
        invocation = r"""
const values={
  '#weather-location':'Málaga ☀','#weather-latitude':'12.34',
  '#weather-longitude':'56.78','#weather-units':'celsius',
};
global.document={querySelector(selector){return {value:values[selector]??''}}};
(async()=>{
  const result={};
  for(const module of ['weather','openweather_overview']){
    const payload=providerTimingPayload(module);
    result[module]={payload,digest:await timingSettingsDigest(module,payload)};
  }
  console.log(JSON.stringify(result));
})().catch(error=>{console.error(error);process.exitCode=1});
"""
        script = (
            "const matrixLoadedRevision=" + str(self.store.get()["revision"]) + ";"
            "const builtInSettings=" + json.dumps(settings, ensure_ascii=False) + ";"
            + functions + invocation
        )
        result = subprocess.run(
            [shutil.which("node") or "", "-e", script], capture_output=True,
            text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        browser = json.loads(result.stdout)
        plan = {
            "frames_per_loop": 100, "total_frames": 100, "loops": 1,
            "frame_delay": .05, "milliseconds": 5000, "display": "5.0 s",
        }
        route_digests = {}
        with patch("modules.weather_app.prepare_ticker_plan", return_value=plan):
            response = self.client.post(
                "/api/scroll-timing/weather", json=browser["weather"]["payload"],
            )
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            route_digests["weather"] = response.get_json()["settings_digest"]
        with patch("modules.openweather_overview.prepare_ticker_plan", return_value=plan):
            response = self.client.post(
                "/api/scroll-timing/openweather_overview",
                json=browser["openweather_overview"]["payload"],
            )
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            route_digests["openweather_overview"] = response.get_json()["settings_digest"]

        self.assertEqual(
            browser["openweather_overview"]["payload"]["settings"]["location"],
            "Málaga ☀",
        )
        self.assertEqual(route_digests, {
            module: browser[module]["digest"] for module in route_digests
        })

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_timing_digest_falls_back_on_insecure_lan_origins(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        start = body.index("function stableJSONStringify")
        end = body.index("\nfunction scheduleProviderTiming", start)
        functions = body[start:end]
        source = {
            "module": "weather",
            "settings": {"location": "Málaga ☀", "scroll_loops": 3, "scroll_speed": 0.05},
        }
        canonical = json.dumps(source, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        invocation = """
const payload={settings:{location:'Málaga ☀',scroll_loops:3,scroll_speed:0.05}};
timingSettingsDigest('weather',payload).then(value=>console.log(value)).catch(error=>{console.error(error);process.exitCode=1});
"""
        script = (
            "const vm=require('node:vm');"
            "vm.runInNewContext(" + json.dumps(functions + invocation) + ","
            "{TextEncoder,Uint8Array,console,crypto:undefined,process});"
        )
        node = shutil.which("node") or ""
        self.assertTrue(node)
        result = subprocess.run(
            [node, "-e", script], capture_output=True, text=True, timeout=10
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), expected)

    def test_dynamic_playlist_reorder_buttons_have_accessible_names(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("moveUp.setAttribute('aria-label','Move up')", body)
        self.assertIn("moveDown.setAttribute('aria-label','Move down')", body)

    def test_weather_scroll_controls_use_numeric_frame_delay(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)

        self.assertEqual(body.count("field:'scroll_speed',label:'Frame delay (seconds)',type:'number',min:.01,max:1,step:.000001"), 3)
        self.assertEqual(body.count("field:'scroll_loops',label:'Scroll loops',type:'number'"), 3)
        self.assertNotIn("label:'Tick speed'", body)
        self.assertNotIn("label:'Ticker loops'", body)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_weather_frame_delay_serializes_as_a_number(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        helper_start = body.index("function withSharedWeatherLocation(settings)")
        helper_end = body.index("\nasync function saveConfig", helper_start)
        harness = body[helper_start:helper_end] + r"""
const values={
  '#max-paired-devices':'5','#day-brightness':'50','#night-brightness':'25',
  '#weather-scroll-speed':'0.05','#weather-latitude':'','#weather-longitude':'',
};
global.document={
  querySelector(selector){return {value:values[selector]??'',checked:false}},
  querySelectorAll(){return []},
};
let customModules=[];
let customLavaPalettes=[];
const builtInSettings={weather:{},openweather_overview:{},openweather_forecast:{},clock_lava:{},splash:{},sleep:{}};
const legacyCustomLavaColours=[];
const announcements=[];
function customModulesFromEditor(){return []}
function customLavaPalettesFromEditor(){return []}
function announcementsFromEditor(){return []}
function playlist(){return []}
const payload=configBody();
if(typeof payload.weather.scroll_speed!=='number' || payload.weather.scroll_speed!==0.05){
  throw new Error('weather frame delay was not numeric: '+JSON.stringify(payload.weather.scroll_speed));
}
console.log('WEATHER_FRAME_DELAY_SERIALIZATION_PASS');
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WEATHER_FRAME_DELAY_SERIALIZATION_PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_all_scroll_editors_share_labels_and_physical_time_readouts(self):
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        asset = (Path(__file__).parents[1] / "app" / "static" / "custom_canvas.js").read_text()

        self.assertIn("function calculatedAnnouncementDuration(announcement)", body)
        self.assertIn("function scrollPhysicalTimeReadout", body)
        self.assertIn("customControl('Frame delay (seconds)','speed','number'", body)
        self.assertIn("customControl('Frame delay (seconds)', 'speed', 'number'", asset)
        self.assertIn("state.dataset.providerTimingState=''", body)
        self.assertIn("state.textContent='Unavailable'", body)
        self.assertIn("loadProviderTiming(module,editor)", body)
        self.assertIn("function announcementDraftMutated(announcement=null)", body)
        self.assertIn("if(announcement)syncAnnouncementRuntime(announcement);else{syncPlaylistDurations();scheduleAnnouncementTiming()}", body)

        function_start = body.index("const customFontAdvances=")
        function_end = body.index("\nconst automaticWeatherModules", function_start)
        sync_start = body.index("function syncAnnouncementRuntime(announcement)")
        sync_end = body.index("\nfunction ", sync_start + 1)
        harness = body[function_start:function_end] + "\n" + body[sync_start:sync_end] + r"""
const announcement={headline:'A',message:'',style:{font:'5x7'},timing:{mode:'scroll',loops:2,frame_delay:.05},transition:{type:'cut'}};
if(calculatedTextWidth('helvR12','é😀')!==16)throw new Error('Unicode font width does not match the BDF renderer');
if(calculatedAnnouncementDuration(announcement)!==7)throw new Error('scroll announcement runtime mismatch');
announcement.timing={mode:'static',duration_seconds:8};
if(calculatedAnnouncementDuration(announcement)!==8)throw new Error('static announcement runtime mismatch');
announcement.transition={type:'fade',duration_seconds:1.5};
if(calculatedAnnouncementDuration(announcement)!==9.5)throw new Error('transition runtime was omitted');
const runtime={textContent:''};
const syncPlaylistDurations=()=>{};
const scheduleAnnouncementTiming=()=>{};
global.document={querySelectorAll(selector){return selector.startsWith('[data-announcement-runtime=')?[runtime]:[]}};
syncAnnouncementRuntime(announcement);
if(runtime.textContent!=='Physical time · 9.5 s')throw new Error('transition edit did not synchronize runtime badge');
console.log('UNIFIED_SCROLL_RUNTIME_PASS');
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("UNIFIED_SCROLL_RUNTIME_PASS", result.stdout)

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

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_browser_rotated_aspect_media_uses_fitted_origin_centre_and_clip(self):
        asset = (Path(__file__).parents[1] / "app/static/custom_canvas.js").read_text()
        start = asset.index("  function drawShape(")
        end = asset.index("\n\n  function drawEditor(", start)
        function_source = asset[start:end]
        harness = f"""
function mediaFrameIndex(){{return 0;}}
function beginLayerRotation(){{}}
let naturalWidth=32,naturalHeight=32;
function mediaTimelineElement(){{return {{naturalWidth,naturalHeight}};}}
{function_source}
function probe(sourceWidth,sourceHeight){{
  naturalWidth=sourceWidth; naturalHeight=sourceHeight;
  const calls=[];
  const context={{
    save(){{}},restore(){{}},set globalAlpha(value){{}},set strokeStyle(value){{}},
    set fillStyle(value){{}},set lineWidth(value){{}},beginPath(){{calls.push(['beginPath']);}},
    rect(...args){{calls.push(['rect',...args]);}},clip(){{calls.push(['clip']);}},
    translate(...args){{calls.push(['translate',...args]);}},rotate(value){{calls.push(['rotate',Math.round(value*1000)/1000]);}},
    drawImage(...args){{calls.push(['drawImage',...args.slice(5)]);}},
  }};
  drawShape(context,{{type:'media'}},{{source:'media.png',x:3,y:4,width:64,height:32,keep_aspect:true,rotation:90}},1,0,1,1);
  return calls;
}}
console.log(JSON.stringify({{square:probe(32,32),tall:probe(16,32),wide:probe(64,16)}}));
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=True,
        )
        data = json.loads(result.stdout)

        for name, width, height in (
            ("square", 32, 32), ("tall", 16, 32), ("wide", 64, 16),
        ):
            with self.subTest(name=name):
                self.assertIn(["rect", 3, 4, width, height], data[name])
                self.assertIn(["clip"], data[name])
                self.assertIn(["translate", 3 + width / 2, 4 + height / 2], data[name])
                self.assertIn(
                    ["drawImage", -width / 2, -height / 2, width, height],
                    data[name],
                )

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_browser_aspect_media_geometry_matches_complete_physical_path(self):
        asset = (Path(__file__).parents[1] / "app/static/custom_canvas.js").read_text()
        start = asset.index("  function drawShape(")
        end = asset.index("\n\n  function drawEditor(", start)
        function_source = asset[start:end]
        cases = [
            {"source": [1, 4], "box": [5.5, 3.5], "position": [2.5, 3.5]},
            {"source": [3, 2], "box": [20, 10], "position": [3, 4]},
            {"source": [20, 10], "box": [15, 15], "position": [-2.5, -3.5]},
            {"source": [5, 2], "box": [9, 1], "position": [0.5, 1.5]},
            {"source": [2, 5], "box": [1, 9], "position": [4.5, 5.5]},
        ]
        source_sizes = [(1, 1), (1, 4), (3, 2), (8, 13), (20, 10)]
        box_values = (1, 1.5, 2.5, 3.5, 5, 5.5, 9.5, 15)
        for source_index, source in enumerate(source_sizes):
            for width_index, width in enumerate(box_values):
                for height_index, height in enumerate(box_values):
                    cases.append({
                        "source": source,
                        "box": [width, height],
                        "position": [
                            box_values[(source_index + width_index) % len(box_values)],
                            -box_values[(source_index + height_index) % len(box_values)],
                        ],
                    })
        harness = f"""
function mediaFrameIndex(){{return 0;}}
function beginLayerRotation(){{}}
let naturalWidth=1,naturalHeight=1;
function mediaTimelineElement(){{return {{naturalWidth,naturalHeight}};}}
{function_source}
function probe(item){{
  naturalWidth=item.source[0]; naturalHeight=item.source[1];
  const calls=[];
  const context={{
    save(){{}},restore(){{}},set globalAlpha(value){{}},set strokeStyle(value){{}},
    set fillStyle(value){{}},set lineWidth(value){{}},beginPath(){{}},
    rect(...args){{calls.push(['rect',...args]);}},clip(){{}},
    translate(...args){{calls.push(['translate',...args]);}},rotate(){{}},
    drawImage(...args){{calls.push(['drawImage',...args.slice(5)]);}},
  }};
  drawShape(context,{{type:'media'}},{{source:'media.png',x:item.position[0],y:item.position[1],width:item.box[0],height:item.box[1],keep_aspect:true,rotation:90}},1,0,1,1);
  return calls;
}}
console.log(JSON.stringify({json.dumps(cases)}.map(probe)));
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=True,
        )
        browser_calls = json.loads(result.stdout)
        for case, calls in zip(cases, browser_calls, strict=True):
            source_width, source_height = case["source"]
            box_width, box_height = map(round, case["box"])
            x, y = map(round, case["position"])
            physical = Image.new("RGB", (source_width, source_height))
            physical.thumbnail(
                (box_width, box_height), Image.Resampling.LANCZOS,
            )
            fitted_width, fitted_height = physical.size
            with self.subTest(
                source=case["source"], box=case["box"],
                position=case["position"],
            ):
                self.assertIn(["rect", x, y, fitted_width, fitted_height], calls)
                self.assertIn(
                    ["translate", x + fitted_width / 2, y + fitted_height / 2],
                    calls,
                )
                self.assertIn(
                    ["drawImage", -fitted_width / 2, -fitted_height / 2,
                     fitted_width, fitted_height],
                    calls,
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
        self.assertIn("layerName.className = 'layer-name'", asset)
        self.assertIn("layerMeta.className = 'layer-meta'", asset)
        self.assertIn("forward.textContent = '↑'", asset)
        self.assertIn("backward.textContent = '↓'", asset)
        self.assertIn("forward.setAttribute('aria-label', 'Bring forward')", asset)
        self.assertIn("backward.setAttribute('aria-label', 'Send backward')", asset)
        self.assertIn(".layer-name", styles)
        self.assertIn(".layer-meta", styles)
        self.assertIn("layer-frame-cell", asset)
        self.assertIn("cell.dataset.layerId", asset)
        self.assertIn("cell.dataset.frame", asset)
        self.assertIn("orderedLayersForFrame(module, state.frame)", asset)
        self.assertIn(".aseprite-timeline", styles)
        self.assertIn(".layer-track-row", styles)
        self.assertIn(".layer-frame-cell", styles)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    @unittest.skipUnless(
        os.environ.get("MATRIX_CHROMIUM")
        and Path(os.environ.get("MATRIX_CHROMIUM", "")).is_file()
        and os.access(os.environ.get("MATRIX_CHROMIUM", ""), os.X_OK),
        "MATRIX_CHROMIUM browser is required",
    )
    def test_advanced_canvas_mobile_timeline_geometry_in_real_chromium(self):
        layers = []
        for index in range(14):
            layers.append({
                "id": f"layer_track_{index}",
                "name": f"Layer track {index + 1}",
                "type": "rectangle",
                "visible": True,
                "properties": {
                    "x": index, "y": index % 16, "width": 8, "height": 8,
                    "rotation": 0, "colour": "#00ffff", "filled": False,
                },
                "keyframes": ([
                    {"frame": 0, "easing": "linear", "properties": {"x": index}},
                    {"frame": 119, "easing": "linear", "properties": {"x": index + 1}},
                ] if index == 0 else []),
            })
        module = test_config_store.ConfigStoreTests.advanced_module(
            frame_count=120,
            layers=layers,
            overrides=[{
                "frame": 60, "layer_id": "layer_track_0",
                "properties": {"colour": "#ff00ff"},
            }],
        )
        self.store.update({"custom_modules": [module]})
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        css = (Path(__file__).parents[1] / "app/static/custom_canvas.css").read_text()
        javascript = (Path(__file__).parents[1] / "app/static/custom_canvas.js").read_text()
        stylesheet = '<link rel="stylesheet" href="/static/custom_canvas.css">'
        script = '<script src="/static/custom_canvas.js"></script>'
        self.assertIn(stylesheet, body)
        self.assertIn(script, body)
        body = body.replace(stylesheet, f"<style>{css}</style>", 1)
        body = body.replace(script, f"<script>{javascript}</script>", 1)

        probe = r"""
const port=process.argv[1],pagePath=process.argv[2];let endpoint;
for(let attempt=0;attempt<50;attempt++){try{endpoint=await fetch(`http://127.0.0.1:${port}/json/new`,{method:'PUT'}).then(response=>response.json());break}catch(_){await new Promise(resolve=>setTimeout(resolve,100))}}
if(!endpoint)throw new Error('Chromium DevTools endpoint unavailable');
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map(),exceptions=[];let nextId=1,loadResolve=()=>{},loaded;
socket.onmessage=event=>{const message=JSON.parse(event.data);if(message.id&&pending.has(message.id)){const request=pending.get(message.id);pending.delete(message.id);message.error?request.reject(new Error(JSON.stringify(message.error))):request.resolve(message.result);return}if(message.method==='Runtime.exceptionThrown')exceptions.push(message.params.exceptionDetails);if(message.method==='Page.loadEventFired')loadResolve()};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>{const id=nextId++;socket.send(JSON.stringify({id,method,params}));return new Promise((resolve,reject)=>pending.set(id,{resolve,reject}))};
const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value};
const metrics=async(width,height,touch,dpr=1)=>{await send('Emulation.setTouchEmulationEnabled',{enabled:touch,maxTouchPoints:touch?2:1});await send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:dpr,mobile:false,screenOrientation:{type:width>height?'landscapePrimary':'portraitPrimary',angle:width>height?90:0}})};
try{
 await send('Runtime.enable');await send('Page.enable');
 await metrics(390,844,true);loaded=new Promise(resolve=>loadResolve=resolve);await send('Page.navigate',{url:'file://'+pagePath});await Promise.race([loaded,new Promise((_,reject)=>setTimeout(()=>reject(new Error('load timeout')),5000))]);
 const viewports=[[320,568,true,1],[360,800,true,1],[390,844,true,1],[844,390,true,1],[412,915,true,1],[599,800,false,1],[600,800,false,1],[601,800,false,1],[768,1024,true,1],[820,1180,true,1],[899,768,false,1],[900,768,false,1],[901,768,false,1],[1024,768,true,1],[1280,720,false,1],[1440,900,false,1],[390,844,true,2],[390,844,true,3]],reports=[];
 for(const [width,height,touch,dpr] of viewports){
  await metrics(width,height,touch,dpr);
  reports.push(await evaluate(`(async()=>{await new Promise(requestAnimationFrame);const scroll=document.querySelector('.aseprite-timeline-scroll'),cell=document.querySelector('.layer-frame-cell'),header=document.querySelector('.frame-chip'),label=document.querySelector('.layer-track-label.active'),corner=document.querySelector('.layer-track-corner'),active=document.querySelector('.layer-frame-cell.active'),timeline=document.querySelector('.timeline'),context=document.querySelector('.timeline-context'),contextButtons=[...document.querySelectorAll('.timeline-context-actions button')],layerRow=document.querySelector('.layer-row'),layerSelect=layerRow.querySelector('.layer-select'),layerMeta=layerRow.querySelector('.layer-meta'),layerActions=[...layerRow.querySelectorAll('.layer-stack-action,.layer-remove')],before=JSON.stringify(customModules),scrollRect=scroll.getBoundingClientRect(),cellRect=cell.getBoundingClientRect(),headerRect=header.getBoundingClientRect(),labelRect=label.getBoundingClientRect(),cornerRect=corner.getBoundingClientRect(),activeIdentity=[active.dataset.layerId,active.dataset.frame];scroll.scrollLeft=scroll.scrollWidth;await new Promise(requestAnimationFrame);const labelAfter=label.getBoundingClientRect();scroll.scrollTop=scroll.scrollHeight;await new Promise(requestAnimationFrame);const cornerAfter=corner.getBoundingClientRect();const documentOverflow=document.documentElement.scrollWidth-document.documentElement.clientWidth;timeline.hidden=true;await new Promise(requestAnimationFrame);const baselineOverflow=document.documentElement.scrollWidth-document.documentElement.clientWidth;timeline.hidden=false;await new Promise(requestAnimationFrame);const wideElements=[...document.querySelectorAll('body *')].map(element=>{const rect=element.getBoundingClientRect();return {tag:element.tagName,className:String(element.className||''),left:rect.left,right:rect.right,width:rect.width,scrollWidth:element.scrollWidth}}).filter(item=>item.right>innerWidth+1||item.width>innerWidth+1).sort((left,right)=>right.right-left.right).slice(0,8),layerRowRect=layerRow.getBoundingClientRect(),layerSelectRect=layerSelect.getBoundingClientRect();return {layerStack:{inside:layerRowRect.left>=-0.5&&layerRowRect.right<=innerWidth+0.5,selectWidth:layerSelectRect.width,metaWidth:layerMeta.getBoundingClientRect().width,metaScrollWidth:layerMeta.scrollWidth,metaText:layerMeta.textContent,actions:layerActions.map(button=>{const rect=button.getBoundingClientRect();return {text:button.textContent,label:button.getAttribute('aria-label'),title:button.title,width:rect.width,height:rect.height}})},width:innerWidth,height:innerHeight,touch:${touch},dpr:devicePixelRatio,layoutColumns:getComputedStyle(document.querySelector('.advanced-layout')).gridTemplateColumns.split(' ').length,documentOverflow,baselineOverflow,timelineOverflow:Math.max(0,documentOverflow-baselineOverflow),wideElements,localOverflow:scroll.scrollWidth-scroll.clientWidth,scrollerInside:scrollRect.left>=-0.5&&scrollRect.right<=innerWidth+0.5,cell:[cellRect.width,cellRect.height],header:[headerRect.width,headerRect.height],label:[labelRect.width,labelRect.height],stickyLabelDelta:Math.abs(labelAfter.left-labelRect.left),stickyHeaderDelta:Math.abs(cornerAfter.top-cornerRect.top),contextOutsideScroll:!scroll.contains(context),contextSummary:context.querySelector('.timeline-context-summary').textContent,contextActions:contextButtons.map(button=>{const rect=button.getBoundingClientRect();return {label:button.getAttribute('aria-label'),disabled:button.disabled,width:rect.width,height:rect.height}}),cellNames:[...document.querySelectorAll('.layer-frame-cell')].slice(0,8).map(button=>({aria:button.getAttribute('aria-label'),title:button.title,text:button.textContent})),statePreserved:before===JSON.stringify(customModules),activePreserved:activeIdentity[0]===document.querySelector('.layer-frame-cell.active').dataset.layerId&&activeIdentity[1]===document.querySelector('.layer-frame-cell.active').dataset.frame}})()`));
 }
 console.log(JSON.stringify({reports,exceptions:exceptions.map(item=>item.text)}));
}finally{socket.close()}
"""
        chromium = os.environ["MATRIX_CHROMIUM"]
        node = shutil.which("node")
        assert node is not None
        with tempfile.TemporaryDirectory(prefix="matrix-timeline-geometry-") as directory:
            page = Path(directory) / "dashboard.html"
            page.write_text(body)
            profile = Path(directory) / "profile"
            browser = subprocess.Popen([
                chromium, "--headless", "--no-sandbox", "--disable-gpu",
                "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                active_port = profile / "DevToolsActivePort"
                for _attempt in range(50):
                    if active_port.is_file():
                        break
                    if browser.poll() is not None:
                        self.fail("Chromium exited before publishing its DevTools port")
                    time.sleep(0.1)
                self.assertTrue(active_port.is_file(), "Chromium DevTools port unavailable")
                completed = subprocess.run(
                    [node, "-e", probe, active_port.read_text().splitlines()[0], str(page)],
                    capture_output=True, text=True, timeout=30,
                )
            finally:
                browser.terminate()
                try:
                    browser.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    browser.kill(); browser.wait(timeout=5)
                for cleanup_attempt in range(20):
                    try:
                        shutil.rmtree(profile)
                        break
                    except FileNotFoundError:
                        break
                    except OSError:
                        if cleanup_attempt == 19:
                            raise
                        time.sleep(0.05)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(report["exceptions"], [])
        self.assertEqual(
            [(item["width"], item["height"], item["touch"], item["dpr"]) for item in report["reports"]],
            [
                (320, 568, True, 1), (360, 800, True, 1),
                (390, 844, True, 1), (844, 390, True, 1),
                (412, 915, True, 1),
                (599, 800, False, 1), (600, 800, False, 1),
                (601, 800, False, 1), (768, 1024, True, 1),
                (820, 1180, True, 1), (899, 768, False, 1),
                (900, 768, False, 1), (901, 768, False, 1),
                (1024, 768, True, 1), (1280, 720, False, 1),
                (1440, 900, False, 1), (390, 844, True, 2),
                (390, 844, True, 3),
            ],
        )
        for item in report["reports"]:
            with self.subTest(viewport=(item["width"], item["height"])):
                self.assertLessEqual(item["timelineOverflow"], 1, item)
                self.assertGreater(item["localOverflow"], 0)
                self.assertTrue(item["scrollerInside"])
                self.assertLessEqual(item["stickyLabelDelta"], 1)
                self.assertLessEqual(item["stickyHeaderDelta"], 1)
                self.assertTrue(item["statePreserved"])
                self.assertTrue(item["activePreserved"])
                self.assertEqual(item["layoutColumns"], 1 if item["width"] <= 900 else 2, item)
                self.assertTrue(item["contextOutsideScroll"])
                self.assertEqual(item["contextSummary"], "Layer track 1 · frame 1")
                layer_stack = item["layerStack"]
                self.assertTrue(layer_stack["inside"], item)
                self.assertGreaterEqual(layer_stack["selectWidth"], 72, item)
                self.assertGreaterEqual(layer_stack["metaWidth"], 72, item)
                self.assertTrue(layer_stack["metaText"].startswith("depth "), item)
                self.assertEqual(
                    [(action["text"], action["label"], action["title"])
                     for action in layer_stack["actions"]],
                    [
                        ("↑", "Bring forward", "Bring forward"),
                        ("↓", "Send backward", "Send backward"),
                        ("×", "Remove Layer track 14", "Remove Layer track 14"),
                    ],
                )
                for cell_name in item["cellNames"]:
                    self.assertEqual(cell_name["aria"], cell_name["title"], cell_name)
                    self.assertNotEqual(cell_name["aria"], cell_name["text"], cell_name)
                self.assertEqual(
                    [action["label"] for action in item["contextActions"]],
                    [
                        "Previous frame", "Next frame",
                        "Move item earlier", "Move item later",
                    ],
                )
                self.assertEqual(
                    [action["disabled"] for action in item["contextActions"]],
                    [True, False, True, False],
                )
                if item["touch"] or item["width"] <= 900:
                    for action in item["contextActions"]:
                        self.assertGreaterEqual(action["width"], 44)
                        self.assertGreaterEqual(action["height"], 44)
                    for action in layer_stack["actions"]:
                        self.assertGreaterEqual(action["width"], 44)
                        self.assertGreaterEqual(action["height"], 44)
                    for target in (item["cell"], item["header"], item["label"]):
                        self.assertGreaterEqual(target[0], 44)
                        self.assertGreaterEqual(target[1], 44)
                else:
                    self.assertAlmostEqual(item["cell"][0], 28, delta=0.5)
                    self.assertAlmostEqual(item["cell"][1], 28, delta=0.5)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_browser_timeline_item_move_is_atomic_and_collision_safe(self):
        harness = r"""
const fs=require('fs');
global.window={};
global.customModules=[];
global.mediaItems=[];
global.document={querySelector:()=>({replaceChildren(){},append(){}}),querySelectorAll:()=>[]};
global.syncCustomModuleUI=()=>{};
global.renderCustomModules=()=>{};
global.setInterval=()=>0;
eval(fs.readFileSync('app/static/custom_canvas.js','utf8'));
const move=window.moveTimelineItem;
if(typeof move!=='function')throw new Error('production moveTimelineItem helper is missing');
const source={
 id:'custom_motion',type:'canvas',frame_count:8,fps:12,playback:'loop',background:'#000000',
 layers:[
  {id:'layer_a',name:'A',type:'rectangle',visible:true,properties:{x:1},keyframes:[
   {frame:1,easing:'linear',properties:{x:2}},
   {frame:5,easing:'ease_in_out',properties:{x:6}},
  ]},
  {id:'layer_b',name:'B',type:'rectangle',visible:true,properties:{x:3},keyframes:[
   {frame:3,easing:'step',properties:{x:4}},
  ]},
 ],
 overrides:[
  {frame:1,layer_id:'layer_a',properties:{opacity:0.5}},
  {frame:2,layer_id:'layer_a',properties:{colour:'#ff0000'}},
  {frame:6,layer_id:'layer_a',properties:{colour:'#00ff00'}},
  {frame:1,layer_id:'layer_b',properties:{colour:'#0000ff'}},
 ],
};
function unchanged(before){if(JSON.stringify(source)!==before)throw new Error('helper mutated its input')}
function rejected(layer,from,to,reason){const before=JSON.stringify(source),result=move(source,layer,from,to);unchanged(before);if(result.moved||result.module!==source||result.reason!==reason||result.frame!==from)throw new Error(`unexpected rejection ${from}->${to}: `+JSON.stringify(result));}
rejected('layer_a',1,-1,'out_of_bounds');
rejected('layer_a',1,8,'out_of_bounds');
rejected('layer_a',-1,4,'out_of_bounds');
rejected('layer_a',1,1,'same_frame');
rejected('layer_a',1,5,'target_occupied');
rejected('layer_a',1,2,'target_occupied');
rejected('layer_a',2,1,'target_occupied');
rejected('layer_a',2,6,'target_occupied');
rejected('layer_a',4,7,'source_missing');
rejected('missing',1,4,'source_missing');
rejected('layer_a',1.5,4,'out_of_bounds');
const cancelledBefore=JSON.stringify(source),cancelled={pointerId:7,cancelled:true};if(!cancelled.cancelled)move(source,'layer_a',1,4);unchanged(cancelledBefore);
const beforeDual=JSON.stringify(source),dual=move(source,'layer_a',1,4);unchanged(beforeDual);
if(!dual.moved||dual.frame!==4||dual.module===source)throw new Error('valid dual-data move was rejected');
if(JSON.stringify(dual.module.layers[0].keyframes)!==JSON.stringify([
 {frame:4,easing:'linear',properties:{x:2}},
 {frame:5,easing:'ease_in_out',properties:{x:6}},
]))throw new Error('keyframe identity/order changed: '+JSON.stringify(dual.module.layers[0].keyframes));
if(JSON.stringify(dual.module.overrides)!==JSON.stringify([
 {frame:4,layer_id:'layer_a',properties:{opacity:0.5}},
 {frame:2,layer_id:'layer_a',properties:{colour:'#ff0000'}},
 {frame:6,layer_id:'layer_a',properties:{colour:'#00ff00'}},
 {frame:1,layer_id:'layer_b',properties:{colour:'#0000ff'}},
]))throw new Error('dual-data override changed: '+JSON.stringify(dual.module.overrides));
const beforeOverride=JSON.stringify(source),override=move(source,'layer_a',2,4);unchanged(beforeOverride);
if(!override.moved||override.frame!==4||override.module===source)throw new Error('valid override-only move was rejected');
if(JSON.stringify(override.module.overrides)!==JSON.stringify([
 {frame:1,layer_id:'layer_a',properties:{opacity:0.5}},
 {frame:4,layer_id:'layer_a',properties:{colour:'#ff0000'}},
 {frame:6,layer_id:'layer_a',properties:{colour:'#00ff00'}},
 {frame:1,layer_id:'layer_b',properties:{colour:'#0000ff'}},
]))throw new Error('override identity/order changed: '+JSON.stringify(override.module.overrides));
const keyframeOnly=move(source,'layer_a',5,7);if(!keyframeOnly.moved||keyframeOnly.frame!==7)throw new Error('keyframe-only move failed');
if(JSON.stringify(JSON.parse(JSON.stringify(dual.module)))!==JSON.stringify(dual.module))throw new Error('moved module did not serialize exactly');
console.log('ADVANCED_CANVAS_TIMELINE_MOVE_PASS');
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness],
            cwd=Path(__file__).parents[1], text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ADVANCED_CANVAS_TIMELINE_MOVE_PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_moved_canvas_timeline_save_reopen_and_stale_peer_conflict(self):
        module = test_config_store.ConfigStoreTests.advanced_module(
            id="custom_motion", name="Motion saved through API", frame_count=120,
            layers=[{
                "id": "layer_a", "name": "Primary motion", "type": "rectangle",
                "visible": True,
                "properties": {
                    "x": 1, "y": 2, "width": 8, "height": 8,
                    "rotation": 0, "colour": "#00ffff", "filled": False,
                },
                "keyframes": [
                    {"frame": 1, "easing": "linear", "properties": {"x": 2}},
                    {"frame": 4, "easing": "ease_in_out", "properties": {"x": 5}},
                ],
            }],
            overrides=[{
                "frame": 3, "layer_id": "layer_a",
                "properties": {"colour": "#ff0000"},
            }],
        )
        self.store.update({"custom_modules": [module]})
        self.pair_browser()
        stale_client = self.app.test_client()
        self.assertEqual(
            stale_client.post(
                "/pair", data={"pin": "123456", "device_name": "Stale canvas peer"},
            ).status_code,
            302,
        )
        baseline = self.client.get("/api/config")
        self.assertEqual(baseline.status_code, 200)
        baseline_revision = baseline.get_json()["revision"]

        harness = r"""
const fs=require('fs');
global.window={};global.customModules=[];global.mediaItems=[];
global.document={querySelector:()=>({replaceChildren(){},append(){}}),querySelectorAll:()=>[]};
global.syncCustomModuleUI=()=>{};global.renderCustomModules=()=>{};global.setInterval=()=>0;
eval(fs.readFileSync('app/static/custom_canvas.js','utf8'));
const source=JSON.parse(process.argv[1]);
const result=window.moveTimelineItem(source,'layer_a',1,2);
if(!result.moved)throw new Error(JSON.stringify(result));
console.log(JSON.stringify(result.module));
"""
        node = shutil.which("node")
        assert node is not None
        moved_result = subprocess.run(
            [node, "-e", harness, json.dumps(module)],
            cwd=Path(__file__).parents[1], text=True, capture_output=True, check=False,
        )
        self.assertEqual(moved_result.returncode, 0, moved_result.stderr)
        moved = json.loads(moved_result.stdout)
        self.assertEqual(
            [item["frame"] for item in moved["layers"][0]["keyframes"]],
            [2, 4],
        )

        saved = self.client.post(
            "/api/config", json={"custom_modules": [moved]},
            headers={"If-Match": f'"{baseline_revision}"'},
        )
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        saved_revision = saved.get_json()["revision"]
        reopened = self.client.get("/api/config")
        self.assertEqual(reopened.status_code, 200)
        self.assertEqual(reopened.get_json()["custom_modules"], [moved])

        stale_payload = json.loads(json.dumps(module))
        stale_payload["name"] = "Unsaved dirty peer draft"
        conflict = stale_client.post(
            "/api/config", json={"custom_modules": [stale_payload]},
            headers={"If-Match": f'"{baseline_revision}"'},
        )
        self.assertEqual(conflict.status_code, 412, conflict.get_data(as_text=True))
        self.assertEqual(conflict.headers["ETag"], f'"{saved_revision}"')
        self.assertEqual(conflict.get_json()["revision"], saved_revision)
        final_snapshot = self.client.get("/api/config").get_json()
        self.assertEqual(final_snapshot["custom_modules"], [moved])
        self.assertEqual(final_snapshot["revision"], saved_revision)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    @unittest.skipUnless(
        os.environ.get("MATRIX_CHROMIUM")
        and Path(os.environ.get("MATRIX_CHROMIUM", "")).is_file()
        and os.access(os.environ.get("MATRIX_CHROMIUM", ""), os.X_OK),
        "MATRIX_CHROMIUM browser is required",
    )
    def test_advanced_canvas_timeline_touch_keyboard_and_reflow_state_in_real_chromium(self):
        layers = [
            {
                "id": "layer_a", "name": "Primary motion", "type": "rectangle",
                "visible": True,
                "properties": {
                    "x": 1, "y": 2, "width": 8, "height": 8,
                    "rotation": 0, "colour": "#00ffff", "filled": False,
                },
                "keyframes": [
                    {"frame": 1, "easing": "linear", "properties": {"x": 2}},
                    {"frame": 4, "easing": "ease_in_out", "properties": {"x": 5}},
                ],
            },
            {
                "id": "layer_b", "name": "Background", "type": "rectangle",
                "visible": True,
                "properties": {
                    "x": 10, "y": 10, "width": 12, "height": 12,
                    "rotation": 0, "colour": "#ffffff", "filled": True,
                },
                "keyframes": [],
            },
        ]
        module = test_config_store.ConfigStoreTests.advanced_module(
            id="custom_motion", name="Motion draft", frame_count=120,
            layers=layers,
            overrides=[
                {"frame": 3, "layer_id": "layer_a", "properties": {"colour": "#ff0000"}},
                {"frame": 6, "layer_id": "layer_a", "properties": {"colour": "#00ff00"}},
            ],
        )
        self.store.update({"custom_modules": [module]})
        self.pair_browser()
        body = self.client.get("/").get_data(as_text=True)
        css = (Path(__file__).parents[1] / "app/static/custom_canvas.css").read_text()
        javascript = (Path(__file__).parents[1] / "app/static/custom_canvas.js").read_text()
        stylesheet = '<link rel="stylesheet" href="/static/custom_canvas.css">'
        script = '<script src="/static/custom_canvas.js"></script>'
        self.assertIn(stylesheet, body)
        self.assertIn(script, body)
        body = body.replace(stylesheet, f"<style>{css}</style>", 1)
        body = body.replace(script, f"<script>{javascript}</script>", 1)

        probe = r"""
const port=process.argv[1],pagePath=process.argv[2];let endpoint;
for(let attempt=0;attempt<50;attempt++){try{endpoint=await fetch(`http://127.0.0.1:${port}/json/new`,{method:'PUT'}).then(response=>response.json());break}catch(_){await new Promise(resolve=>setTimeout(resolve,100))}}
if(!endpoint)throw new Error('Chromium DevTools endpoint unavailable');
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map(),exceptions=[];let nextId=1,loadResolve=()=>{},loaded;
socket.onmessage=event=>{const message=JSON.parse(event.data);if(message.id&&pending.has(message.id)){const request=pending.get(message.id);pending.delete(message.id);message.error?request.reject(new Error(JSON.stringify(message.error))):request.resolve(message.result);return}if(message.method==='Runtime.exceptionThrown')exceptions.push(message.params.exceptionDetails);if(message.method==='Page.loadEventFired')loadResolve()};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>{const id=nextId++;socket.send(JSON.stringify({id,method,params}));return new Promise((resolve,reject)=>pending.set(id,{resolve,reject}))};
const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value};
const metrics=async(width,height,touch=true,mobile=touch)=>{await send('Emulation.setTouchEmulationEnabled',{enabled:touch,maxTouchPoints:touch?2:1});await send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile});await evaluate(`new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve(true))))`)};
const point=selector=>evaluate(`(async()=>{const element=document.querySelector(${JSON.stringify(selector)});if(!element)throw new Error('missing '+${JSON.stringify(selector)});element.scrollIntoView({behavior:'instant',block:'center',inline:'center'});await new Promise(requestAnimationFrame);let rect=element.getBoundingClientRect();const scroller=element.closest('.aseprite-timeline-scroll'),sticky=scroller?.querySelector('.layer-track-label.active');if(scroller&&sticky){const stickyRect=sticky.getBoundingClientRect();if(rect.left<stickyRect.right+6){scroller.scrollLeft=Math.max(0,scroller.scrollLeft-(stickyRect.right+6-rect.left));await new Promise(requestAnimationFrame);rect=element.getBoundingClientRect()}const visibleRight=Math.min(scroller.getBoundingClientRect().right,visualViewport.width);if(rect.right>visibleRight-4){scroller.scrollLeft+=rect.right-visibleRight+4;await new Promise(requestAnimationFrame);rect=element.getBoundingClientRect()}}const x=rect.left+rect.width/2,y=rect.top+rect.height/2,hit=document.elementFromPoint(x,y);return {x,y,scale:visualViewport.scale,hitTag:hit?.tagName,hitAria:hit?.getAttribute?.('aria-label'),hitClass:String(hit?.className||''),hitLayer:hit?.dataset?.layerId,hitFrame:hit?.dataset?.frame}})()`);
let touchId=10;const touchTap=async target=>{const id=touchId++,stamp=Date.now()/1000;await send('Input.dispatchTouchEvent',{type:'touchStart',timestamp:stamp,touchPoints:[{x:target.x,y:target.y,radiusX:2,radiusY:2,force:1,id}]});await send('Input.dispatchTouchEvent',{type:'touchEnd',timestamp:stamp+0.05,touchPoints:[]});await evaluate(`new Promise(resolve=>requestAnimationFrame(()=>resolve(true)))`)};
const pressEnter=async()=>{const key={key:'Enter',code:'Enter',windowsVirtualKeyCode:13,nativeVirtualKeyCode:13};await send('Input.dispatchKeyEvent',{type:'rawKeyDown',...key});await send('Input.dispatchKeyEvent',{type:'char',...key,text:'\r',unmodifiedText:'\r'});await send('Input.dispatchKeyEvent',{type:'keyUp',...key});await evaluate(`new Promise(resolve=>requestAnimationFrame(()=>resolve(true)))`)};
const mouseDrag=async(source,target)=>{await send('Input.dispatchMouseEvent',{type:'mouseMoved',x:source.x,y:source.y});await send('Input.dispatchMouseEvent',{type:'mousePressed',x:source.x,y:source.y,button:'left',buttons:1,clickCount:1});await send('Input.dispatchMouseEvent',{type:'mouseMoved',x:target.x,y:target.y,button:'left',buttons:1});await send('Input.dispatchMouseEvent',{type:'mouseReleased',x:target.x,y:target.y,button:'left',buttons:0,clickCount:1});await evaluate(`new Promise(resolve=>requestAnimationFrame(()=>resolve(true)))`)};
const mouseClick=async target=>{await send('Input.dispatchMouseEvent',{type:'mouseMoved',x:target.x,y:target.y});await send('Input.dispatchMouseEvent',{type:'mousePressed',x:target.x,y:target.y,button:'left',buttons:1,clickCount:1});await send('Input.dispatchMouseEvent',{type:'mouseReleased',x:target.x,y:target.y,button:'left',buttons:0,clickCount:1});await evaluate(`new Promise(resolve=>requestAnimationFrame(()=>resolve(true)))`)};
const touchDrag=async(source,target,cancel=false)=>{const id=touchId++,stamp=Date.now()/1000;await send('Input.dispatchTouchEvent',{type:'touchStart',timestamp:stamp,touchPoints:[{x:source.x,y:source.y,radiusX:2,radiusY:2,force:1,id}]});await send('Input.dispatchTouchEvent',{type:'touchMove',timestamp:stamp+0.05,touchPoints:[{x:target.x,y:target.y,radiusX:2,radiusY:2,force:1,id}]});await send('Input.dispatchTouchEvent',{type:cancel?'touchCancel':'touchEnd',timestamp:stamp+0.1,touchPoints:[]});await evaluate(`new Promise(resolve=>requestAnimationFrame(()=>resolve(true)))`)};
const pointerEvent=async(selector,type,options)=>evaluate(`(()=>{const element=document.querySelector(${JSON.stringify(selector)});element.dispatchEvent(new PointerEvent(${JSON.stringify(type)},{bubbles:true,cancelable:true,...${JSON.stringify(options)}}));return true})()`);
try{
 await send('Runtime.enable');await send('Page.enable');await metrics(390,844,true);
 loaded=new Promise(resolve=>loadResolve=resolve);await send('Page.navigate',{url:'file://'+pagePath});await Promise.race([loaded,new Promise((_,reject)=>setTimeout(()=>reject(new Error('load timeout')),5000))]);
 await evaluate(`(()=>{if(matrixSyncTimer!==null){clearInterval(matrixSyncTimer);matrixSyncTimer=null}if(matrixFrameTimer!==null){clearTimeout(matrixFrameTimer);matrixFrameTimer=null}globalThis.__timelineFetches=[];const original=globalThis.fetch;globalThis.fetch=(url,options={})=>{__timelineFetches.push({url:String(url),method:options.method||'GET'});return original(url,options)};const add=EventTarget.prototype.addEventListener,remove=EventTarget.prototype.removeEventListener;globalThis.__trackedWindowResize=new Set();globalThis.__trackedViewportResize=new Set();EventTarget.prototype.addEventListener=function(type,listener,options){if(type==='resize'){if(this===window)__trackedWindowResize.add(listener);else if(this===visualViewport)__trackedViewportResize.add(listener)}return add.call(this,type,listener,options)};EventTarget.prototype.removeEventListener=function(type,listener,options){if(type==='resize'){if(this===window)__trackedWindowResize.delete(listener);else if(this===visualViewport)__trackedViewportResize.delete(listener)}return remove.call(this,type,listener,options)};document.querySelector('.layer-row.active button.btn').click();return true})()`);await send('Emulation.setCPUThrottlingRate',{rate:4});
 const selectedPoint=await point('.layer-frame-cell[data-layer-id="layer_a"][data-frame="1"]');await touchTap(selectedPoint);
 const selected=await evaluate(`({summary:document.querySelector('.timeline-context-summary').textContent,active:[document.querySelector('.layer-frame-cell.active').dataset.layerId,document.querySelector('.layer-frame-cell.active').dataset.frame]})`);
 const emptyPoint=await point('.layer-frame-cell[data-layer-id="layer_b"][data-frame="5"]'),emptyBefore=await evaluate(`(()=>{const active=document.querySelector('.layer-frame-cell.active'),scroll=document.querySelector('.aseprite-timeline-scroll');return {active:[active.dataset.layerId,active.dataset.frame],scrollLeft:scroll.scrollLeft}})()`),panStamp=Date.now()/1000;
 await send('Input.dispatchTouchEvent',{type:'touchStart',timestamp:panStamp,touchPoints:[{x:emptyPoint.x,y:emptyPoint.y,radiusX:2,radiusY:2,force:1,id:70}]});await send('Input.dispatchTouchEvent',{type:'touchMove',timestamp:panStamp+0.04,touchPoints:[{x:emptyPoint.x-70,y:emptyPoint.y,radiusX:2,radiusY:2,force:1,id:70}]});await send('Input.dispatchTouchEvent',{type:'touchEnd',timestamp:panStamp+0.08,touchPoints:[]});await evaluate(`new Promise(resolve=>requestAnimationFrame(()=>resolve(true)))`);
 const emptyPan=await evaluate(`(()=>{const active=document.querySelector('.layer-frame-cell.active'),scroll=document.querySelector('.aseprite-timeline-scroll');return {before:${JSON.stringify(emptyBefore)},active:[active.dataset.layerId,active.dataset.frame],scrollLeft:scroll.scrollLeft}})()`);await touchTap(await point('.layer-frame-cell[data-layer-id="layer_a"][data-frame="1"]'));
 await point('.layer-frame-cell[data-layer-id="layer_a"][data-frame="1"]');const secondPoints=await evaluate(`(()=>{const centre=element=>{const rect=element.getBoundingClientRect();return {x:rect.left+rect.width/2,y:rect.top+rect.height/2}};return {source:centre(document.querySelector('.layer-frame-cell[data-layer-id="layer_a"][data-frame="1"]')),elsewhere:centre(document.querySelector('.timeline-context-status'))}})()`),secondSource=secondPoints.source,secondElsewhere=secondPoints.elsewhere,secondStamp=Date.now()/1000,secondBefore=await evaluate(`JSON.stringify(customModules)`);
 await send('Input.dispatchTouchEvent',{type:'touchStart',timestamp:secondStamp,touchPoints:[{x:secondSource.x,y:secondSource.y,radiusX:2,radiusY:2,force:1,id:71}]});const dragStarted=await evaluate(`!!document.querySelector('.layer-frame-cell.dragging')`);await send('Input.dispatchTouchEvent',{type:'touchStart',timestamp:secondStamp+0.03,touchPoints:[{x:secondSource.x,y:secondSource.y,radiusX:2,radiusY:2,force:1,id:71},{x:secondElsewhere.x,y:secondElsewhere.y,radiusX:2,radiusY:2,force:1,id:72}]});const dragAfterSecond=await evaluate(`!!document.querySelector('.layer-frame-cell.dragging')`);await send('Input.dispatchTouchEvent',{type:'touchCancel',timestamp:secondStamp+0.06,touchPoints:[]});const secondTouchCancelled={dragStarted,dragAfterSecond,unchanged:secondBefore===await evaluate(`JSON.stringify(customModules)`)};
 const tinySource=await point('.layer-frame-cell[data-layer-id="layer_a"][data-frame="1"]'),tinyStamp=Date.now()/1000,tinyBefore=await evaluate(`JSON.stringify(customModules)`);await send('Input.dispatchTouchEvent',{type:'touchStart',timestamp:tinyStamp,touchPoints:[{x:tinySource.x,y:tinySource.y,radiusX:2,radiusY:2,force:1,id:73}]});await send('Input.dispatchTouchEvent',{type:'touchMove',timestamp:tinyStamp+0.03,touchPoints:[{x:tinySource.x+4,y:tinySource.y,radiusX:2,radiusY:2,force:1,id:73}]});await send('Input.dispatchTouchEvent',{type:'touchEnd',timestamp:tinyStamp+0.06,touchPoints:[]});await evaluate(`new Promise(resolve=>requestAnimationFrame(()=>resolve(true)))`);const subthreshold=await evaluate(`(()=>{const active=document.querySelector('.layer-frame-cell.active');return {unchanged:JSON.stringify(customModules)===${JSON.stringify(tinyBefore)},active:[active.dataset.layerId,active.dataset.frame]}})()`);
 const nextSelector='.timeline-context button[data-action="move"][data-direction="1"]',nextPoint=await point(nextSelector);
 const beforeMulti=await evaluate(`JSON.stringify(customModules)`);
 await send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:nextPoint.x-5,y:nextPoint.y,radiusX:2,radiusY:2,force:1,id:1},{x:nextPoint.x+5,y:nextPoint.y,radiusX:2,radiusY:2,force:1,id:2}]});await send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});await new Promise(resolve=>setTimeout(resolve,200));
 const multiTouchSafe=beforeMulti===await evaluate(`JSON.stringify(customModules)`);
 const disabledPoint=await point('.timeline-context button[data-action="move"][data-direction="-1"]');
 await send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:disabledPoint.x,y:disabledPoint.y,radiusX:3,radiusY:3,force:1,id:3}]});await new Promise(resolve=>setTimeout(resolve,600));const longPress=await evaluate(`({selection:getSelection().toString(),userSelect:getComputedStyle(document.querySelector('.timeline-context-actions button')).userSelect,module:JSON.stringify(customModules)})`);await send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});await new Promise(resolve=>setTimeout(resolve,200));
 const validMovePoint=await point(nextSelector);await evaluate(`(()=>{globalThis.__timelineTouchEvents=[];const button=document.querySelector(${JSON.stringify(nextSelector)});for(const type of ['touchstart','touchend','click'])button.addEventListener(type,event=>__timelineTouchEvents.push({type,touches:event.touches?.length,changed:event.changedTouches?.length,detail:event.detail,timeStamp:event.timeStamp,prevented:event.defaultPrevented}));return true})()`);await touchTap(validMovePoint);
 const touchMove=await evaluate(`(()=>{const module=customModules.find(item=>item.id==='custom_motion'),active=document.querySelector('.layer-frame-cell.active');return {point:${JSON.stringify(validMovePoint)},events:__timelineTouchEvents,enabled:!document.querySelector(${JSON.stringify(nextSelector)}).disabled,frames:module.layers.find(item=>item.id==='layer_a').keyframes.map(item=>item.frame),active:[active.dataset.layerId,active.dataset.frame],focused:document.activeElement?.getAttribute('aria-label'),dirty:matrixHasUnsavedDraft(),summary:document.querySelector('.timeline-context-summary').textContent}})()`);
 const collisionSelector='.timeline-context button[data-action="move"][data-direction="1"]';
 const collisionBefore=await evaluate(`JSON.stringify(customModules)`);await evaluate(`document.querySelector(${JSON.stringify(collisionSelector)}).click()`);
 const collision=await evaluate(`({unchanged:JSON.stringify(customModules)===${JSON.stringify(collisionBefore)},disabled:document.querySelector(${JSON.stringify(collisionSelector)}).disabled,status:document.querySelector('.timeline-context-status').textContent})`);
 const overrideCellPoint=await point('.layer-frame-cell[data-layer-id="layer_a"][data-frame="6"]');await touchTap(overrideCellPoint);const overrideSelection=await evaluate(`(()=>{const active=document.querySelector('.layer-frame-cell.active');return [active.dataset.layerId,active.dataset.frame]})()`);
 const keyboardBefore=await evaluate(`(()=>{const button=document.querySelector('.timeline-context button[data-action="move"][data-direction="-1"]');globalThis.__timelineKeyEvents=[];for(const type of ['keydown','keyup','click'])button.addEventListener(type,event=>__timelineKeyEvents.push({type,key:event.key,detail:event.detail,sourceTouch:event.sourceCapabilities?.firesTouchEvents}));button.focus();return {cellPoint:${JSON.stringify(overrideCellPoint)},selection:${JSON.stringify(overrideSelection)},disabled:button.disabled,focused:document.activeElement===button,aria:document.activeElement?.getAttribute('aria-label')}})()`);await pressEnter();
 const keyboardMove=await evaluate(`(()=>{const module=customModules.find(item=>item.id==='custom_motion'),active=document.querySelector('.layer-frame-cell.active');return {before:${JSON.stringify(keyboardBefore)},events:__timelineKeyEvents,frames:module.overrides.filter(item=>item.layer_id==='layer_a').map(item=>item.frame),active:[active.dataset.layerId,active.dataset.frame],focused:document.activeElement?.getAttribute('aria-label'),dirty:matrixHasUnsavedDraft()}})()`);
 await evaluate(`(()=>{globalThis.__markerFocusCalls=[];globalThis.__markerOriginalFocus=HTMLElement.prototype.focus;HTMLElement.prototype.focus=function(options){__markerFocusCalls.push({aria:this.getAttribute?.('aria-label'),preventScroll:options?.preventScroll===true});return __markerOriginalFocus.call(this,options)}})()`);
 await point('.layer-frame-cell[data-layer-id="layer_a"][data-frame="5"]');
 const markerTouchBefore=await evaluate(`(()=>{const scroll=document.querySelector('.aseprite-timeline-scroll');return {local:scroll.scrollLeft,pageX:scrollX,pageY:scrollY}})()`);
 const touchDragPoints=await evaluate(`(()=>{const centre=element=>{const rect=element.getBoundingClientRect();return {x:rect.left+rect.width/2,y:rect.top+rect.height/2}};return {source:centre(document.querySelector('.layer-frame-cell[data-layer-id="layer_a"][data-frame="5"]')),target:centre(document.querySelector('.layer-frame-cell[data-layer-id="layer_a"][data-frame="6"]'))}})()`);await touchDrag(touchDragPoints.source,touchDragPoints.target);
 const markerTouchMove=await evaluate(`(()=>{const module=customModules.find(item=>item.id==='custom_motion'),active=document.querySelector('.layer-frame-cell.active'),scroll=document.querySelector('.aseprite-timeline-scroll');return {frames:module.overrides.filter(item=>item.layer_id==='layer_a').map(item=>item.frame),active:[active.dataset.layerId,active.dataset.frame],dirty:matrixHasUnsavedDraft(),focusCalls:__markerFocusCalls.splice(0).filter(item=>item.aria?.includes(', frame ')),local:scroll.scrollLeft,pageX:scrollX,pageY:scrollY,before:${JSON.stringify(markerTouchBefore)}}})()`);
 await point('.layer-frame-cell[data-layer-id="layer_a"][data-frame="6"]');
 const markerMouseBefore=await evaluate(`(()=>{const scroll=document.querySelector('.aseprite-timeline-scroll');return {local:scroll.scrollLeft,pageX:scrollX,pageY:scrollY}})()`);
 const dragPoints=await evaluate(`(()=>{const centre=element=>{const rect=element.getBoundingClientRect(),x=rect.left+rect.width/2,y=rect.top+rect.height/2,hit=document.elementFromPoint(x,y);return {x,y,hitLayer:hit?.dataset?.layerId,hitFrame:hit?.dataset?.frame,hitClass:String(hit?.className||'')}};return {source:centre(document.querySelector('.layer-frame-cell[data-layer-id="layer_a"][data-frame="6"]')),target:centre(document.querySelector('.layer-frame-cell[data-layer-id="layer_a"][data-frame="7"]'))}})()`);await mouseDrag(dragPoints.source,dragPoints.target);
 const mouseMove=await evaluate(`(()=>{const module=customModules.find(item=>item.id==='custom_motion'),active=document.querySelector('.layer-frame-cell.active'),scroll=document.querySelector('.aseprite-timeline-scroll'),calls=__markerFocusCalls.splice(0).filter(item=>item.aria?.includes(', frame '));HTMLElement.prototype.focus=__markerOriginalFocus;delete globalThis.__markerOriginalFocus;return {points:${JSON.stringify(dragPoints)},frames:module.overrides.filter(item=>item.layer_id==='layer_a').map(item=>item.frame),active:[active.dataset.layerId,active.dataset.frame],dirty:matrixHasUnsavedDraft(),focusCalls:calls,local:scroll.scrollLeft,pageX:scrollX,pageY:scrollY,before:${JSON.stringify(markerMouseBefore)} }})()`);
 const cancelBefore=await evaluate(`JSON.stringify(customModules)`);await point('.layer-frame-cell[data-layer-id="layer_a"][data-frame="7"]');const cancelPoints=await evaluate(`(()=>{globalThis.__cancelEvents={pointer:0,touch:0};const source=document.querySelector('.layer-frame-cell[data-layer-id="layer_a"][data-frame="7"]');source.addEventListener('pointercancel',()=>__cancelEvents.pointer++);source.addEventListener('touchcancel',()=>__cancelEvents.touch++);const centre=element=>{const rect=element.getBoundingClientRect();return {x:rect.left+rect.width/2,y:rect.top+rect.height/2}};return {source:centre(source),target:centre(document.querySelector('.layer-frame-cell[data-layer-id="layer_a"][data-frame="6"]'))}})()`);await touchDrag(cancelPoints.source,cancelPoints.target,true);const cancelled=await evaluate(`({unchanged:JSON.stringify(customModules)===${JSON.stringify(cancelBefore)},pointerCancels:__cancelEvents.pointer,touchCancels:__cancelEvents.touch,dragging:!!document.querySelector('.layer-frame-cell.dragging')})`);
 const blurBefore=await evaluate(`JSON.stringify(customModules)`),blurStamp=Date.now()/1000;await send('Input.dispatchTouchEvent',{type:'touchStart',timestamp:blurStamp,touchPoints:[{...cancelPoints.source,radiusX:2,radiusY:2,force:1,id:74}]});const blurStarted=await evaluate(`!!document.querySelector('.layer-frame-cell.dragging')`);await evaluate(`dispatchEvent(new Event('blur'))`);const blurCancelled=!await evaluate(`!!document.querySelector('.layer-frame-cell.dragging')`);await send('Input.dispatchTouchEvent',{type:'touchEnd',timestamp:blurStamp+0.05,touchPoints:[]});const blurSafe={started:blurStarted,cancelled:blurCancelled,unchanged:blurBefore===await evaluate(`JSON.stringify(customModules)`)};
 const rerenderDragBefore=await evaluate(`JSON.stringify(customModules)`),rerenderStamp=Date.now()/1000;await send('Input.dispatchTouchEvent',{type:'touchStart',timestamp:rerenderStamp,touchPoints:[{...cancelPoints.source,radiusX:2,radiusY:2,force:1,id:75}]});const rerenderDragStarted=await evaluate(`!!document.querySelector('.layer-frame-cell.dragging')`);await evaluate(`document.querySelector('.layer-row.active button.btn').click()`);await evaluate(`new Promise(resolve=>requestAnimationFrame(resolve))`);const rerenderDragCancelled=!await evaluate(`!!document.querySelector('.layer-frame-cell.dragging')`);await send('Input.dispatchTouchEvent',{type:'touchEnd',timestamp:rerenderStamp+0.05,touchPoints:[]});const rerenderDragSafe={started:rerenderDragStarted,cancelled:rerenderDragCancelled,unchanged:rerenderDragBefore===await evaluate(`JSON.stringify(customModules)`)};
 await evaluate(`(()=>{globalThis.__timelineFocusCalls=[];globalThis.__timelineOriginalFocus=HTMLElement.prototype.focus;HTMLElement.prototype.focus=function(options){__timelineFocusCalls.push({aria:this.getAttribute?.('aria-label'),preventScroll:options?.preventScroll===true});return __timelineOriginalFocus.call(this,options)}})()`);
 const moveFocusRestores=[];for(const direction of [-1,1]){moveFocusRestores.push(await evaluate(`(async()=>{const button=document.querySelector('.timeline-context button[data-action="move"][data-direction="${direction}"]'),scroll=document.querySelector('.aseprite-timeline-scroll');scroll.scrollLeft=180;document.querySelector('.canvas-toolbar input[type="text"]').focus({preventScroll:true});window.scrollTo(0,0);const before={local:scroll.scrollLeft,pageX:scrollX,pageY:scrollY};button.click();await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));const nextScroll=document.querySelector('.aseprite-timeline-scroll');return {direction:${direction},local:nextScroll.scrollLeft,beforeLocal:before.local,pageX:scrollX,pageY:scrollY,beforePageX:before.pageX,beforePageY:before.pageY,focused:document.activeElement?.getAttribute('aria-label')}})()`));}
 const moveFocusCalls=await evaluate(`(()=>{const calls=__timelineFocusCalls.filter(item=>item.aria?.startsWith('Move item'));HTMLElement.prototype.focus=__timelineOriginalFocus;delete globalThis.__timelineOriginalFocus;return calls})()`);
 const focusRerenders=await evaluate(`(async()=>{const cases=[['frame','.frame-chip[data-frame="7"]'],['layer','.layer-track-label.active'],['property','[data-property="colour"]'],['toolbar','.canvas-toolbar select']];const reports=[];for(const [name,selector] of cases){const scroll=document.querySelector('.aseprite-timeline-scroll');scroll.scrollLeft=137;window.scrollTo(0,Math.min(240,document.documentElement.scrollHeight-innerHeight));const before={local:scroll.scrollLeft,pageX:scrollX,pageY:scrollY};const target=document.querySelector(selector);target.focus({preventScroll:true});document.querySelector('.layer-row.active button.btn').click();await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));const nextScroll=document.querySelector('.aseprite-timeline-scroll');reports.push({name,focused:document.activeElement?.matches(selector)||false,local:nextScroll.scrollLeft,beforeLocal:before.local,pageX:scrollX,pageY:scrollY,beforePageX:before.pageX,beforePageY:before.pageY});}return reports})()`);
 await evaluate(`(()=>{document.querySelector('.layer-track-label[data-layer-id="layer_a"]')?.click()})()`);
 await evaluate(`(()=>{const scroll=document.querySelector('.aseprite-timeline-scroll');scroll.scrollLeft=180;scroll.scrollTop=70;document.querySelector('.timeline-context button[data-action="move"][data-direction="-1"]').focus();document.querySelector('.layer-row.active button.btn').click();return new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve(true))))})()`);
 const rerender=await evaluate(`(()=>{const scroll=document.querySelector('.aseprite-timeline-scroll');return {scrollLeft:scroll.scrollLeft,scrollTop:scroll.scrollTop,focused:document.activeElement?.getAttribute('aria-label')}})()`);
 await metrics(768,700,true,false);await metrics(320,500,true,false);
 const reflow=await evaluate(`(()=>{const active=document.querySelector('.layer-frame-cell.active'),scroll=document.querySelector('.aseprite-timeline-scroll'),activeRect=active.getBoundingClientRect(),scrollRect=scroll.getBoundingClientRect();return {active:[active.dataset.layerId,active.dataset.frame],scrollLeft:scroll.scrollLeft,scrollTop:scroll.scrollTop,clientWidth:scroll.clientWidth,scrollWidth:scroll.scrollWidth,innerWidth,activeRect:{left:activeRect.left,right:activeRect.right},scrollRect:{left:scrollRect.left,right:scrollRect.right},focused:document.activeElement?.getAttribute('aria-label')}})()`);
 await metrics(390,844,true);const fieldState=await evaluate(`(()=>{const field=document.querySelector('[data-id="custom_motion"] .canvas-toolbar label:first-child input');field.value='Mobile keyboard draft';field.dispatchEvent(new Event('input',{bubbles:true}));field.scrollIntoView({block:'center'});field.focus();field.setSelectionRange(2,8);return true})()`);await metrics(390,430,true);
 const keyboardViewport=await evaluate(`(()=>{const field=document.querySelector('[data-id="custom_motion"] .canvas-toolbar label:first-child input'),active=document.querySelector('.layer-frame-cell.active');return {height:innerHeight,value:field.value,start:field.selectionStart,end:field.selectionEnd,focused:document.activeElement===field,active:[active.dataset.layerId,active.dataset.frame],dirty:matrixHasUnsavedDraft()}})()`);
 const keyboardCycles=[];for(const height of [844,430,844,430]){await metrics(390,height,true);await evaluate(`(async()=>{visualViewport.dispatchEvent(new Event('resize'));for(let frame=0;frame<6;frame++)await new Promise(requestAnimationFrame);await new Promise(resolve=>setTimeout(resolve,150));return true})()`);keyboardCycles.push(await evaluate(`(()=>{const field=document.querySelector('[data-id="custom_motion"] .canvas-toolbar label:first-child input'),rect=field.getBoundingClientRect(),viewport=visualViewport;return {height:innerHeight,value:field.value,start:field.selectionStart,end:field.selectionEnd,focused:document.activeElement===field,visible:rect.top>=viewport.offsetTop-1&&rect.bottom<=viewport.offsetTop+viewport.height+1,rect:[rect.top,rect.bottom],viewport:[viewport.offsetTop,viewport.height,viewport.pageTop]}})()`))}
 await metrics(390,844,true);await evaluate(`(()=>{document.activeElement?.blur();const scroll=document.querySelector('.aseprite-timeline-scroll');scroll.scrollLeft=140;window.scrollTo(0,Math.min(180,document.documentElement.scrollHeight-innerHeight));return new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve(true))))})()`);const orientationBefore=await evaluate(`(()=>{const scroll=document.querySelector('.aseprite-timeline-scroll'),active=document.querySelector('.layer-frame-cell.active');return {local:scroll.scrollLeft,page:scrollY,maxPage:document.documentElement.scrollHeight-innerHeight,active:[active.dataset.layerId,active.dataset.frame],focused:document.activeElement?.dataset?.control||null}})()`);await metrics(844,390,true);const orientationAfter=await evaluate(`(()=>{const scroll=document.querySelector('.aseprite-timeline-scroll'),active=document.querySelector('.layer-frame-cell.active');return {local:scroll.scrollLeft,page:scrollY,maxPage:document.documentElement.scrollHeight-innerHeight,active:[active.dataset.layerId,active.dataset.frame],focused:document.activeElement?.dataset?.control||null}})()`);
 await metrics(899,700,false,false);await evaluate(`(()=>{const scroll=document.querySelector('.aseprite-timeline-scroll');scroll.scrollLeft=160;return true})()`);const breakpointBefore=await evaluate(`(()=>{const scroll=document.querySelector('.aseprite-timeline-scroll');return {local:scroll.scrollLeft,columns:getComputedStyle(document.querySelector('.advanced-layout')).gridTemplateColumns.split(' ').length}})()`);await metrics(901,700,false,false);const breakpointAfter=await evaluate(`(()=>{const scroll=document.querySelector('.aseprite-timeline-scroll'),active=document.querySelector('.layer-frame-cell.active');return {local:scroll.scrollLeft,columns:getComputedStyle(document.querySelector('.advanced-layout')).gridTemplateColumns.split(' ').length,active:[active.dataset.layerId,active.dataset.frame]}})()`);
 await metrics(390,844,true);await send('Emulation.setPageScaleFactor',{pageScaleFactor:2});await send('Emulation.setEmulatedMedia',{media:'screen',features:[{name:'prefers-reduced-motion',value:'reduce'}]});const zoomReduced=await evaluate(`(()=>{const scroll=document.querySelector('.aseprite-timeline-scroll'),active=document.querySelector('.layer-frame-cell.active'),context=document.querySelector('.timeline-context').getBoundingClientRect(),button=document.querySelector('.timeline-context-actions button').getBoundingClientRect();return {scale:visualViewport.scale,reduced:matchMedia('(prefers-reduced-motion: reduce)').matches,active:[active.dataset.layerId,active.dataset.frame],localOverflow:scroll.scrollWidth-scroll.clientWidth,button:[button.width,button.height],safeHorizontal:context.left>=-0.5&&context.right<=innerWidth+0.5}})()`);await send('Emulation.resetPageScaleFactor');await send('Emulation.setEmulatedMedia',{media:'screen',features:[{name:'prefers-reduced-motion',value:'no-preference'}]});
 await metrics(1280,720,false,false);const desktopBefore=await evaluate(`JSON.stringify(customModules)`);await mouseClick(await point('.timeline-context button[data-action="seek"][data-direction="-1"]'));await evaluate(`document.querySelector('.timeline-context button[data-action="seek"][data-direction="1"]').focus()`);await pressEnter();const desktop=await evaluate(`(()=>{const active=document.querySelector('.layer-frame-cell.active'),cell=document.querySelector('.layer-frame-cell').getBoundingClientRect();return {active:[active.dataset.layerId,active.dataset.frame],unchanged:JSON.stringify(customModules)===${JSON.stringify(desktopBefore)},columns:getComputedStyle(document.querySelector('.advanced-layout')).gridTemplateColumns.split(' ').length,cell:[cell.width,cell.height],focused:document.activeElement?.getAttribute('aria-label'),overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth}})()`);
 const safeAreaRule=await evaluate(`[...document.styleSheets].some(sheet=>[...sheet.cssRules].some(rule=>rule.cssText.includes('safe-area-inset')))`);
 await metrics(390,844,true);await touchTap(await point('.timeline-toolbar button.btn.primary'));await evaluate(`(()=>{globalThis.__timelineChildMutations=0;globalThis.__timelineStartFrame=document.querySelector('.layer-frame-cell.active').dataset.frame;new MutationObserver(items=>{__timelineChildMutations+=items.filter(item=>item.type==='childList').length}).observe(document.querySelector('.aseprite-timeline'),{subtree:true,childList:true,attributes:true});return true})()`);await new Promise(resolve=>setTimeout(resolve,700));
 const playback=await evaluate(`({start:__timelineStartFrame,end:document.querySelector('.layer-frame-cell.active').dataset.frame,childMutations:__timelineChildMutations})`);await touchTap(await point('.timeline-toolbar button.btn.primary'));await send('Emulation.setCPUThrottlingRate',{rate:1});
 const finalState=await evaluate(`(()=>{const payload=configBody(),module=customModules.find(item=>item.id==='custom_motion'),mutatingFetches=__timelineFetches.filter(item=>item.method!=='GET'||item.url.includes('/api/preview'));return {payloadMatches:JSON.stringify(payload.custom_modules.find(item=>item.id==='custom_motion'))===JSON.stringify(module),dirty:matrixHasUnsavedDraft(),mutatingFetches,exceptions:${JSON.stringify(exceptions)}}})()`);
 const resizeLifecycle=await evaluate(`(async()=>{visualViewport.dispatchEvent(new Event('resize'));document.querySelector('.layer-row.active button.btn').click();await new Promise(resolve=>setTimeout(resolve,150));const before={window:__trackedWindowResize.size,viewport:__trackedViewportResize.size};const originalConfirm=globalThis.confirm;globalThis.confirm=()=>true;applyCanvasTemplate('custom_motion','blank');await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));const afterTemplate={window:__trackedWindowResize.size,viewport:__trackedViewportResize.size};removeCustomModule('custom_motion');globalThis.confirm=originalConfirm;await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));return {before,afterTemplate,after:{window:__trackedWindowResize.size,viewport:__trackedViewportResize.size}}})()`);
 console.log(JSON.stringify({selected,emptyPan,secondTouchCancelled,subthreshold,multiTouchSafe,longPress,touchMove,collision,keyboardMove,markerTouchMove,mouseMove,cancelled,blurSafe,rerenderDragSafe,moveFocusRestores,moveFocusCalls,focusRerenders,rerender,reflow,keyboardViewport,keyboardCycles,orientationBefore,orientationAfter,breakpointBefore,breakpointAfter,zoomReduced,desktop,safeAreaRule,playback,finalState,resizeLifecycle,exceptions:exceptions.map(item=>item.text)}));
}finally{socket.close()}
"""
        chromium = os.environ["MATRIX_CHROMIUM"]
        node = shutil.which("node")
        assert node is not None
        with tempfile.TemporaryDirectory(prefix="matrix-timeline-interactions-") as directory:
            page = Path(directory) / "dashboard.html"
            page.write_text(body)
            profile = Path(directory) / "profile"
            browser = subprocess.Popen([
                chromium, "--headless", "--no-sandbox", "--disable-gpu",
                "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                active_port = profile / "DevToolsActivePort"
                for _attempt in range(50):
                    if active_port.is_file():
                        break
                    if browser.poll() is not None:
                        self.fail("Chromium exited before publishing its DevTools port")
                    time.sleep(0.1)
                self.assertTrue(active_port.is_file(), "Chromium DevTools port unavailable")
                completed = subprocess.run(
                    [node, "-e", probe, active_port.read_text().splitlines()[0], str(page)],
                    capture_output=True, text=True, timeout=45,
                )
            finally:
                browser.terminate()
                try:
                    browser.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    browser.kill(); browser.wait(timeout=5)
                for cleanup_attempt in range(20):
                    try:
                        shutil.rmtree(profile)
                        break
                    except FileNotFoundError:
                        break
                    except OSError:
                        if cleanup_attempt == 19:
                            raise
                        time.sleep(0.05)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(report["exceptions"], [])
        self.assertEqual(report["selected"], {"summary": "Primary motion · frame 2", "active": ["layer_a", "1"]})
        self.assertEqual(report["emptyPan"]["active"], report["emptyPan"]["before"]["active"], report["emptyPan"])
        self.assertNotEqual(report["emptyPan"]["scrollLeft"], report["emptyPan"]["before"]["scrollLeft"], report["emptyPan"])
        self.assertEqual(report["secondTouchCancelled"], {"dragStarted": True, "dragAfterSecond": False, "unchanged": True})
        self.assertEqual(report["subthreshold"], {"unchanged": True, "active": ["layer_a", "1"]})
        self.assertTrue(report["multiTouchSafe"])
        self.assertEqual(report["longPress"]["selection"], "")
        self.assertEqual(report["longPress"]["userSelect"], "none")
        self.assertEqual(report["touchMove"]["frames"], [2, 4], report["touchMove"])
        self.assertEqual(report["touchMove"]["active"], ["layer_a", "2"])
        self.assertEqual(report["touchMove"]["focused"], "Move item earlier")
        self.assertTrue(report["touchMove"]["dirty"])
        self.assertEqual(report["touchMove"]["summary"], "Primary motion · frame 3")
        self.assertTrue(report["collision"]["unchanged"])
        self.assertTrue(report["collision"]["disabled"])
        self.assertIn("target occupied", report["collision"]["status"])
        self.assertEqual(report["keyboardMove"]["frames"], [3, 5], report["keyboardMove"])
        self.assertEqual(report["keyboardMove"]["active"], ["layer_a", "5"])
        self.assertEqual(report["keyboardMove"]["focused"], "Move item later")
        self.assertTrue(report["keyboardMove"]["dirty"])
        self.assertEqual(report["markerTouchMove"]["frames"], [3, 6], report["markerTouchMove"])
        self.assertEqual(report["markerTouchMove"]["active"], ["layer_a", "6"])
        self.assertTrue(report["markerTouchMove"]["dirty"])
        self.assertEqual(report["mouseMove"]["frames"], [3, 7], report["mouseMove"])
        self.assertEqual(report["mouseMove"]["active"], ["layer_a", "7"])
        self.assertTrue(report["mouseMove"]["dirty"])
        for key in ("markerTouchMove", "mouseMove"):
            item = report[key]
            self.assertGreaterEqual(len(item["focusCalls"]), 1, item)
            self.assertTrue(all(call["preventScroll"] for call in item["focusCalls"]), item)
            self.assertTrue(all("Primary motion, frame" in call["aria"] for call in item["focusCalls"]), item)
            self.assertEqual(item["local"], item["before"]["local"], item)
            self.assertEqual((item["pageX"], item["pageY"]),
                             (item["before"]["pageX"], item["before"]["pageY"]), item)
        self.assertEqual(report["cancelled"], {"unchanged": True, "pointerCancels": 1, "touchCancels": 1, "dragging": False})
        self.assertEqual(report["blurSafe"], {"started": True, "cancelled": True, "unchanged": True})
        self.assertEqual(report["rerenderDragSafe"], {"started": True, "cancelled": True, "unchanged": True})
        self.assertEqual([item["direction"] for item in report["moveFocusRestores"]], [-1, 1])
        for item in report["moveFocusRestores"]:
            self.assertEqual(item["local"], item["beforeLocal"], item)
            self.assertEqual((item["pageX"], item["pageY"]), (item["beforePageX"], item["beforePageY"]), item)
            self.assertEqual(item["focused"], "Move item earlier" if item["direction"] < 0 else "Move item later")
        self.assertEqual(len(report["moveFocusCalls"]), 2, report["moveFocusCalls"])
        self.assertTrue(all(item["preventScroll"] for item in report["moveFocusCalls"]), report["moveFocusCalls"])
        self.assertEqual([item["name"] for item in report["focusRerenders"]], ["frame", "layer", "property", "toolbar"])
        for item in report["focusRerenders"]:
            self.assertTrue(item["focused"], item)
            self.assertEqual(item["local"], item["beforeLocal"], item)
            self.assertEqual((item["pageX"], item["pageY"]), (item["beforePageX"], item["beforePageY"]), item)
        self.assertGreater(report["rerender"]["scrollLeft"], 0, report["rerender"])
        self.assertEqual(report["rerender"]["focused"], "Move item earlier")
        self.assertEqual(report["reflow"]["active"], ["layer_a", "7"])
        self.assertGreater(report["reflow"]["scrollLeft"], 0, report["reflow"])
        self.assertEqual(report["reflow"]["focused"], "Move item earlier")
        self.assertEqual(report["keyboardViewport"], {
            "height": 430, "value": "Mobile keyboard draft", "start": 2, "end": 8,
            "focused": True, "active": ["layer_a", "7"], "dirty": True,
        })
        self.assertEqual([item["height"] for item in report["keyboardCycles"]], [844, 430, 844, 430])
        for item in report["keyboardCycles"]:
            self.assertEqual((item["value"], item["start"], item["end"]), ("Mobile keyboard draft", 2, 8), item)
            self.assertTrue(item["focused"], item)
            self.assertTrue(item["visible"], item)
        self.assertEqual(report["orientationAfter"]["active"], report["orientationBefore"]["active"])
        self.assertEqual(report["orientationAfter"]["focused"], report["orientationBefore"]["focused"])
        self.assertEqual(report["orientationAfter"]["local"], report["orientationBefore"]["local"])
        self.assertLessEqual(
            abs(report["orientationAfter"]["page"] - report["orientationBefore"]["page"]),
            32,
            (report["orientationBefore"], report["orientationAfter"]),
        )
        self.assertEqual(report["breakpointBefore"], {"local": 160, "columns": 1})
        self.assertEqual(report["breakpointAfter"], {"local": 160, "columns": 2, "active": ["layer_a", "7"]})
        self.assertAlmostEqual(report["zoomReduced"]["scale"], 2, places=2)
        self.assertTrue(report["zoomReduced"]["reduced"])
        self.assertEqual(report["zoomReduced"]["active"], ["layer_a", "7"])
        self.assertGreater(report["zoomReduced"]["localOverflow"], 0)
        self.assertTrue(all(size >= 44 for size in report["zoomReduced"]["button"]))
        self.assertTrue(report["zoomReduced"]["safeHorizontal"])
        self.assertEqual(report["desktop"]["active"], ["layer_a", "7"])
        self.assertTrue(report["desktop"]["unchanged"])
        self.assertEqual(report["desktop"]["columns"], 2)
        self.assertTrue(all(abs(size - 28) <= 0.5 for size in report["desktop"]["cell"]))
        self.assertEqual(report["desktop"]["focused"], "Next frame")
        self.assertLessEqual(report["desktop"]["overflow"], 1)
        self.assertTrue(report["safeAreaRule"])
        self.assertNotEqual(report["playback"]["start"], report["playback"]["end"])
        self.assertEqual(report["playback"]["childMutations"], 0)
        self.assertTrue(report["finalState"]["payloadMatches"])
        self.assertTrue(report["finalState"]["dirty"])
        self.assertEqual(report["finalState"]["mutatingFetches"], [])
        self.assertEqual(report["resizeLifecycle"], {
            "before": {"window": 1, "viewport": 1},
            "afterTemplate": {"window": 1, "viewport": 1},
            "after": {"window": 0, "viewport": 0},
        })

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
const mediaPreviews=new Map(), mediaFailureStates=new Map(), mediaLoadQueue=[], customModules=[{type:'canvas',layers:[{id:'layer_media',type:'media',properties:{source:'built-in/clip.mp4'},keyframes:[]}],overrides:[]}], mediaItems=[{name:'built-in/clip.mp4',revision:'r1'}];let activeMediaLoads=0;
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
const context={save(){},restore(){},beginPath(){},rect(){},clip(){},translate(){},rotate(){},drawImage(...args){drawArgs=args}};
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
const mediaPreviews=new Map(), mediaFailureStates=new Map(), mediaLoadQueue=[];let activeMediaLoads=0;
const mediaItems=Array.from({length:33},(_,index)=>({name:'failed-'+index+'.gif',revision:'r'+index}));
const retainedLayer={id:'layer_retained',type:'media',properties:{source:mediaItems[0].name},keyframes:[{properties:{source:mediaItems[31].name}}]};
const customModules=[{
  type:'canvas',
  layers:[retainedLayer,...mediaItems.slice(1,31).map((item,index)=>({id:'layer_'+index,type:'media',properties:{source:item.name},keyframes:[]}))],
  overrides:[{layer_id:'layer_retained',properties:{source:mediaItems[32].name}}]
}];
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
if(mediaFailureStates.size!==33)throw new Error('live terminal revisions were not retained');
if(mediaFailureStates.size>20*(64+512+512))throw new Error('failure state exceeded schema bound');
if(mediaTimelineElement(mediaItems[0].name,12,12)!==null||created!==before)throw new Error('oldest terminal revision restarted');
customModules[0].layers=[retainedLayer];customModules[0].overrides=[];retainedLayer.keyframes=[];
pruneMediaFailureStates();
if(mediaFailureStates.size!==1||!mediaFailureStates.has(mediaItems[0].name))throw new Error('obsolete terminal revisions were not pruned');
for(let index=0;index<1000;index++){
  const source='obsolete-'+index+'.gif';
  const first=mediaTimelineElement(source,12,12);first.listeners.error();
  const second=mediaTimelineElement(source,12,12);second.listeners.error();
}
if(mediaFailureStates.size!==1||!mediaFailureStates.has(mediaItems[0].name))throw new Error('obsolete source churn grew failure state');
const afterChurn=created;
if(mediaTimelineElement(mediaItems[0].name,12,12)!==null||created!==afterChurn)throw new Error('retained terminal revision restarted after pruning');
let saves=0,restores=0;
const context={save(){saves++},restore(){restores++}};
drawShape(context,{type:'media',keyframes:[]},{source:mediaItems[0].name,colour:'#fff',width:10,height:10,x:0,y:0,rotation:0,keep_aspect:true},1,0,12,12);
if(saves!==1||restores!==1||created!==afterChurn)throw new Error('terminal draw was unsafe');
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
            "scroll_direction": "right", "scroll_speed": 0.075,
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
        self.assertIn('"scroll_speed": 0.075', body)
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

                response = self.post_config(
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

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_announcement_media_selects_refresh_in_place_after_library_changes(self):
        source = (Path(__file__).parents[1] / "app" / "templates" / "dashboard.html").read_text()
        start = source.index("function syncAnnouncementMediaSelects")
        end = source.index("\nfunction syncMediaSelects", start)
        function_source = source[start:end]
        harness = r"""
class Select {
  constructor(value){this.value=value;this.options=[];this.dataset={announcementPath:'media'};}
  replaceChildren(){this.options=[];}
  append(option){this.options.push(option);}
}
const select=new Select('draft/selected.gif');
const document={
  activeElement:select,
  querySelectorAll(selector){
    if(selector!=='.module-editor[data-module-editor="announcement_board"] select[data-announcement-path="media"]')throw new Error('unexpected selector '+selector);
    return [select];
  },
  createElement(tag){if(tag!=='option')throw new Error('unexpected element '+tag);return {value:'',textContent:''};},
};
let mediaItems=[{name:'draft/selected.gif'},{name:'new/upload.gif'}];
""" + function_source + r"""
syncAnnouncementMediaSelects();
if(select.value!=='draft/selected.gif')throw new Error('unsaved draft selection changed');
if(!select.options.some(option=>option.value==='new/upload.gif'))throw new Error('uploaded media missing');
if(document.activeElement!==select)throw new Error('focus identity changed after upload');
mediaItems=[{name:'new/upload.gif'}];
syncAnnouncementMediaSelects();
const missing=select.options.find(option=>option.value==='draft/selected.gif');
if(!missing||missing.textContent!=='draft/selected.gif (missing)')throw new Error('removed media not marked missing');
if(select.value!=='draft/selected.gif')throw new Error('removed draft value changed');
if(document.activeElement!==select)throw new Error('focus identity changed after removal');
console.log('ANNOUNCEMENT_MEDIA_SELECT_REFRESH_PASS');
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ANNOUNCEMENT_MEDIA_SELECT_REFRESH_PASS", result.stdout)

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
const markMatrixDirty=()=>{};
let timingRefreshes=0;
const scheduleAnnouncementTiming=()=>{timingRefreshes++};
const validateAnnouncementBoard=()=>true;
const announcementDraftMutated=()=>{scheduleAnnouncementTiming();validateAnnouncementBoard()};
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
focusLog.length=0;timingRefreshes=0;deleteAnnouncement(announcements[0].id);
if(!focusLog.at(-1)?.includes('[data-announcement-action="add"]'))throw new Error('delete-last focus');
if(timingRefreshes!==1)throw new Error('delete did not refresh aggregate');
announcements=[__ANNOUNCEMENT_JSON__];focusLog.length=0;timingRefreshes=0;resetAnnouncementBoard();
if(!focusLog.at(-1)?.includes('[data-announcement-action="add"]'))throw new Error('reset focus');
if(timingRefreshes!==1)throw new Error('reset did not refresh aggregate');
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
    def test_announcement_weekday_change_refreshes_aggregate(self):
        source = (Path(__file__).parents[1] / "app" / "templates" / "dashboard.html").read_text()
        start = source.index("function renderAnnouncementSchedule")
        end = source.index("\nconst announcementAppearanceFields", start)
        functions = source[start:end]
        harness = functions + """
const announcement={schedule:{days:[0,1,2,3,4,5,6]}};
const checkboxes=[];
const announcementScheduleFields=[];
const announcementWeekdays=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
const announcementControl=()=>({});
let timingRefreshes=0;
const scheduleAnnouncementTiming=()=>{timingRefreshes++};
const validateAnnouncementBoard=()=>true;
const announcementDraftMutated=()=>{scheduleAnnouncementTiming();validateAnnouncementBoard()};
const document={createElement(tag){
  if(tag==='input'){
    const input={checked:false,dataset:{},addEventListener(type,handler){this.handler=handler}};
    checkboxes.push(input);return input;
  }
  return {textContent:'',append(){}};
}};
const fieldset={append(){}};
renderAnnouncementSchedule(announcement,fieldset);
checkboxes[0].checked=false;checkboxes[0].handler();
if(announcement.schedule.days.includes(0))throw new Error('weekday model did not update');
if(timingRefreshes!==1)throw new Error('weekday did not refresh aggregate');
console.log('ANNOUNCEMENT_WEEKDAY_REFRESH_PASS');
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", harness], cwd=Path(__file__).parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ANNOUNCEMENT_WEEKDAY_REFRESH_PASS", result.stdout)

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
const document={createElement:tag=>tag==='label'?label:control,querySelector:()=>heading,querySelectorAll:()=>[]};
const CSS={escape:value=>value};
let announcementTimingPlan={};
const syncPlaylistDurations=()=>{};
const scheduleAnnouncementTiming=()=>{};
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

        saved = self.post_config(
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

    def test_save_shared_media_resolver_preserves_existing_contract(self):
        self.pair_browser()
        outside = Path(self.temp.name) / "outside.png"
        outside.write_bytes(b"outside sentinel")
        selected = {
            "splash.gif": b"splash sentinel",
            "sleep.png": b"sleep sentinel",
            "announcement.jpg": b"announcement sentinel",
            "canvas.webp": b"canvas sentinel",
        }
        for reference, content in selected.items():
            (self.media_dir / reference).write_bytes(content)
        (self.media_dir / "directory.png").mkdir()
        (self.media_dir / "linked.png").symlink_to(outside)

        config_path = self.store.path
        real_resolve = media_resolver.MediaResolver.resolve
        error_handles = []
        error_attempts = []

        def observe_error_resolve(resolver, reference):
            error_attempts.append(reference)
            handle = real_resolve(resolver, reference)
            error_handles.append((handle, handle.fileno()))
            return handle

        rejected = (
            ({"media": {"splash": "missing.gif"}}, "media file does not exist: missing.gif", 0),
            ({"media": {"splash": "../outside.png"}}, "media filename is invalid", 0),
            ({"media": {"splash": "directory.png"}}, "media file is not regular: directory.png", 0),
            ({"media": {"splash": "linked.png"}}, "media filename is invalid", 0),
            (
                {
                    "media": {"splash": "splash.gif", "sleep": "sleep.png"},
                    "announcement_board": {"announcements": [
                        test_config_store.ConfigStoreTests.announcement(media="missing.gif")
                    ]},
                },
                "media file does not exist: missing.gif",
                2,
            ),
        )
        for changes, expected_error, expected_opened in rejected:
            with self.subTest(rejected=changes):
                before_config = self.store.get()
                before_bytes = config_path.read_bytes()
                before_media = {
                    path.relative_to(self.media_dir).as_posix(): (
                        path.read_bytes() if path.is_file() and not path.is_symlink() else None
                    )
                    for path in self.media_dir.iterdir()
                }
                opened_before = len(error_handles)
                with patch.object(
                    media_resolver.MediaResolver, "resolve", autospec=True,
                    side_effect=observe_error_resolve,
                ):
                    response = self.post_config(json=changes)
                self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
                self.assertEqual(response.get_json(), {"error": expected_error})
                self.assertEqual(self.store.get(), before_config)
                self.assertEqual(config_path.read_bytes(), before_bytes)
                self.assertEqual(
                    {
                        path.relative_to(self.media_dir).as_posix(): (
                            path.read_bytes() if path.is_file() and not path.is_symlink() else None
                        )
                        for path in self.media_dir.iterdir()
                    },
                    before_media,
                )
                self.assertEqual(outside.read_bytes(), b"outside sentinel")
                self.controller.reload.assert_not_called()
                opened = error_handles[opened_before:]
                self.assertEqual(
                    len(opened), expected_opened,
                    f"resolver attempts were {error_attempts}",
                )
                for handle, descriptor in opened:
                    self.assertTrue(handle.closed)
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)

        announcement = test_config_store.ConfigStoreTests.announcement(
            media="announcement.jpg"
        )
        canvas = test_config_store.ConfigStoreTests.advanced_module(
            id="custom_media",
            name="Pinned canvas",
            frame_count=1,
            fps=10,
            playback="hold",
            layers=[{
                "id": "layer_media", "name": "Media", "type": "media", "visible": True,
                "properties": {
                    "x": 0, "y": 0, "width": 64, "height": 32, "rotation": 0,
                    "source": "canvas.webp", "keep_aspect": True,
                },
                "keyframes": [],
            }],
            overrides=[],
        )
        changes = {
            "device": {"name": "Pinned Save"},
            "media": {"splash": "splash.gif", "sleep": "sleep.png"},
            "announcement_board": {"announcements": [announcement]},
            "custom_modules": [canvas],
        }
        resolved = []
        def observe_resolve(resolver, reference):
            handle = real_resolve(resolver, reference)
            descriptor = handle.fileno()
            resolved.append((reference, handle, descriptor, os.pread(descriptor, 4096, 0)))
            return handle

        with patch.object(
            media_resolver.MediaResolver, "resolve", autospec=True,
            side_effect=observe_resolve,
        ):
            response = self.post_config(json=changes)

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.headers["ETag"], '"1"')
        expected = self.store.get()
        self.assertEqual(response.get_json(), expected)
        self.assertEqual(expected["revision"], 1)
        self.assertEqual(expected["device"]["name"], "Pinned Save")
        self.assertEqual(
            {reference: content for reference, _handle, _descriptor, content in resolved},
            selected,
        )
        self.assertEqual(len(resolved), len(selected))
        for _reference, handle, descriptor, _content in resolved:
            self.assertTrue(handle.closed)
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self.assertEqual(
            {reference: (self.media_dir / reference).read_bytes() for reference in selected},
            selected,
        )
        self.assertEqual(outside.read_bytes(), b"outside sentinel")
        self.controller.reload.assert_called_once_with()

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

        response = self.post_config( json={"custom_modules": config["custom_modules"]}
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
        response = self.post_config( json={"media": {"splash": "missing.gif"}})
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
            "if(error.data?.committed){matrixLoadedRevision=error.data.config.revision;highestNotifiedRemoteRevision=Math.max(highestNotifiedRemoteRevision,matrixLoadedRevision);dismissMatrixRevisionDialogThrough(matrixLoadedRevision);matrixSavedSnapshot=submittedSnapshot;matrixDirty=false;if(!silent)setMessage(error.message,true);return {saved:true,warning:error.message,data:error.data}}",
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

    def test_unauthenticated_requests_are_redirected(self):
        dashboard = self.client.get("/")
        timing_api = self.client.post("/api/scroll-timing/weather", json={})

        self.assertEqual(dashboard.status_code, 302)
        self.assertIn("/pair", dashboard.headers["Location"])
        self.assertEqual(timing_api.status_code, 401)
        self.assertEqual(timing_api.get_json(), {"error": "Pairing session expired."})

    def test_valid_pin_unlocks_dashboard_and_updates_configuration(self):
        self.pairing.verify.return_value = True
        response = self.client.post("/pair", data={"pin": "123456", "device_name": "Test browser"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.controller.end_pairing.assert_called_once()
        with self.client.session_transaction() as browser_session:
            self.assertTrue(browser_session.permanent)
            self.assertEqual(browser_session["generation"], "generation-one")
        response = self.post_config( json={"weather": {"location": "London, GB"}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["weather"]["location"], "London, GB")
        self.controller.reload.assert_called_once()

    def test_edit_preview_endpoint_is_paired_strict_no_store_and_never_calls_controller(self):
        from contextlib import contextmanager
        from edit_preview import PreviewInstanceCapacityError, PreviewSessionExpiredError

        start_path = "/api/edit-preview/clock_lava"
        page_path = "/api/edit-preview/page"
        cancel_path = "/api/edit-preview/cancel"
        preview_instance = "0123456789abcdef0123456789abcdef"
        candidate = copy.deepcopy(self.store.get())
        candidate.pop("revision")
        start_payload = {
            "config": candidate,
            "generation": 1,
            "preview_instance": preview_instance,
        }

        def assert_response(response, status):
            self.assertEqual(response.status_code, status, response.get_data(as_text=True))
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertNotIn("ETag", response.headers)
            body = response.get_json()
            self.assertIsInstance(body, dict)
            self.assertNotIn("revision", body)
            self.assertNotIn("device_id", json.dumps(body))
            return body

        assert_response(self.client.post(start_path, json=start_payload), 401)
        assert_response(self.client.post(page_path, json={}), 401)
        assert_response(self.client.post(cancel_path, json={}), 401)

        self.pair_browser()
        self.controller.reset_mock()
        before_config = Path(self.store.path).read_bytes()
        before_devices = Path(self.device_store.path).read_bytes()
        before_media = sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        )

        oversized = b'{"config":"' + (b"x" * 1_048_576) + b'"}'
        assert_response(self.client.post(
            start_path, data=oversized, content_type="application/json",
        ), 413)
        malformed_payloads = [
            {},
            {**start_payload, "extra": True},
            {**start_payload, "generation": True},
            {**start_payload, "preview_instance": "ABC"},
        ]
        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                assert_response(self.client.post(start_path, json=payload), 400)
        assert_response(
            self.client.post("/api/edit-preview/not-a-module", json=start_payload), 404,
        )

        lifecycle = []

        @contextmanager
        def pinned(candidate_value, media_root, *, allow_missing=()):
            lifecycle.append("pin-enter")
            handles = {"draft.png": object()}
            yield handles
            lifecycle.append("pin-exit")

        def prepare(_provider, module_name, candidate_value, stop_event):
            self.assertEqual(lifecycle[-1:], ["pin-enter"])
            lifecycle.append("provider")
            return {"prepared": module_name}

        def iterator(module_name, candidate_value, selected_palette, pinned_media, prepared, stop_event):
            self.assertEqual(lifecycle[-2:], ["pin-enter", "provider"])
            self.assertEqual(pinned_media, {"draft.png": pinned_media["draft.png"]})
            self.assertEqual(prepared, {"prepared": "clock_lava"})
            lifecycle.append("render")
            for index in range(34):
                yield {"png": f"frame-{index}", "delay_ms": index + 1}

        with patch("webapp.pin_candidate_media", pinned), patch(
            "webapp._prepare_edit_preview_provider", prepare, create=True,
        ), patch("webapp._edit_preview_iterator", iterator, create=True):
            response = self.client.post(start_path, json=start_payload)
            first = assert_response(response, 200)
            self.assertEqual(
                set(first),
                {"session", "generation", "page", "frames", "next_token", "complete"},
            )
            self.assertRegex(first["session"], r"^[0-9a-f]{32}$")
            self.assertEqual(first["generation"], 1)
            self.assertEqual(first["page"], 0)
            self.assertEqual(len(first["frames"]), 32)
            self.assertFalse(first["complete"])
            self.assertRegex(first["next_token"], r"^[0-9a-f]{64}$")
            self.assertEqual(lifecycle, ["pin-enter", "provider", "render"])

            continuation = {
                "session": first["session"],
                "preview_instance": preview_instance,
                "token": first["next_token"],
            }
            second = assert_response(self.client.post(page_path, json=continuation), 200)
            self.assertEqual(
                set(second),
                {"session", "generation", "page", "frames", "next_token", "complete"},
            )
            self.assertEqual(second["generation"], 1)
            self.assertEqual(second["page"], 1)
            self.assertEqual(len(second["frames"]), 2)
            self.assertTrue(second["complete"])
            self.assertIsNone(second["next_token"])
            self.assertEqual(lifecycle, ["pin-enter", "provider", "render", "pin-exit"])

            assert_response(self.client.post(page_path, json=continuation), 409)

            second_payload = {**start_payload, "generation": 2}
            with patch("webapp._edit_preview_iterator", side_effect=RuntimeError("decode detail"), create=True):
                assert_response(self.client.post(start_path, json=second_payload), 503)

            registry = self.app.extensions["edit_preview_registry"]
            with patch.object(
                registry, "lookup", side_effect=PreviewSessionExpiredError("private expiry"),
            ):
                expired = self.client.post(page_path, json={
                    "session": "1" * 32,
                    "preview_instance": preview_instance,
                    "token": "2" * 64,
                })
            assert_response(expired, 410)
            with patch.object(
                registry, "register", side_effect=PreviewInstanceCapacityError("private capacity"),
            ):
                limited = self.client.post(
                    start_path, json={**start_payload, "generation": 3},
                )
            assert_response(limited, 429)
            self.assertEqual(limited.headers.get("Retry-After"), "2")

            def active_iterator(*args):
                yield from (
                    {"png": f"active-{index}", "delay_ms": 1}
                    for index in range(40)
                )

            with patch("webapp._edit_preview_iterator", active_iterator, create=True):
                active = self.client.post(
                    start_path, json={**start_payload, "generation": 4},
                )
            active_body = assert_response(active, 200)
            cancel = assert_response(self.client.post(cancel_path, json={
                "session": active_body["session"],
                "preview_instance": preview_instance,
            }), 200)
            self.assertEqual(cancel, {"cancelled": True})
            assert_response(self.client.post(cancel_path, json={
                "session": active_body["session"],
                "preview_instance": preview_instance,
            }), 409)

        self.assertEqual(Path(self.store.path).read_bytes(), before_config)
        self.assertEqual(Path(self.device_store.path).read_bytes(), before_devices)
        self.assertEqual([
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        ], before_media)
        self.assertEqual(self.controller.method_calls, [])
        registry.close()
        self.assertFalse(registry.reaper_alive)

    def test_edit_preview_routes_render_splash_sleep_and_media_consumers(self):
        self.pair_browser()
        self.controller.reset_mock()
        before_config = Path(self.store.path).read_bytes()
        before_devices = Path(self.device_store.path).read_bytes()

        red = Image.new("RGB", (8, 8), "red")
        blue = Image.new("RGB", (8, 8), "blue")
        for filename in ("splash.gif", "sleep.gif"):
            red.save(
                self.media_dir / filename, format="GIF", save_all=True,
                append_images=[blue], duration=[100, 100], loop=0,
            )
        for filename in ("announcement.png", "layer.png", "layer.gif", "layer.mp4"):
            red.save(self.media_dir / filename, format="PNG")

        base = copy.deepcopy(self.store.get())
        base.pop("revision")
        base["media"] = {"splash": "splash.gif", "sleep": "sleep.gif"}
        base["playlist"] = [
            {"module": "splash", "duration": 1, "enabled": True},
            {"module": "sleep", "duration": 1, "enabled": True},
        ]
        preview_index = 0

        def start(module_name, candidate):
            nonlocal preview_index
            preview_index += 1
            instance = f"{preview_index:032x}"
            response = self.client.post(f"/api/edit-preview/{module_name}", json={
                "config": candidate,
                "generation": preview_index,
                "preview_instance": instance,
            })
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            body = response.get_json()
            self.assertGreater(len(body["frames"]), 0)
            if not body["complete"]:
                cancelled = self.client.post("/api/edit-preview/cancel", json={
                    "session": body["session"], "preview_instance": instance,
                })
                self.assertEqual(cancelled.status_code, 200)
            return body

        for module_name in ("splash", "sleep"):
            with self.subTest(module=module_name):
                start(module_name, copy.deepcopy(base))
        video_splash = copy.deepcopy(base)
        video_splash["media"]["splash"] = "layer.mp4"
        with self.subTest(module="splash_mp4"):
            import warnings
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                start("splash", video_splash)
            self.assertFalse([
                warning for warning in caught
                if issubclass(warning.category, ResourceWarning)
            ], caught)

        announcements = copy.deepcopy(base)
        static = test_config_store.ConfigStoreTests.announcement(
            id="announcement_static_media", layout="full_image_notice",
            headline="STATIC", message="", media="announcement.png",
            timing={"mode": "static", "duration_seconds": 1},
        )
        scrolling = test_config_store.ConfigStoreTests.announcement(
            id="announcement_scroll_media", layout="text_notice",
            headline="A", message="", media=None,
            timing={"mode": "scroll", "loops": 1, "frame_delay": 0.05},
        )
        announcements["announcement_board"] = {"announcements": [static, scrolling]}
        announcements["playlist"] = [
            {"module": "announcement_board", "duration": 2, "enabled": True},
        ]
        with self.subTest(module="announcement_board"):
            start("announcement_board", announcements)

        canvas = copy.deepcopy(base)
        layers = []
        for index, source in enumerate(("layer.png", "layer.gif", "layer.mp4")):
            layers.append({
                "id": f"layer_media_{index}", "name": source, "type": "media",
                "visible": True,
                "properties": {
                    "source": source, "x": index, "y": index, "width": 8,
                    "height": 8, "rotation": 0, "keep_aspect": True,
                },
                "keyframes": [],
            })
        module = test_config_store.ConfigStoreTests.advanced_module(
            id="custom_media_layers", name="Media layers", frame_count=2, fps=2,
            playback="hold", layers=layers, overrides=[],
        )
        canvas["custom_modules"] = [module]
        canvas["playlist"] = [
            {"module": module["id"], "duration": 1, "enabled": True},
        ]
        with self.subTest(module=module["id"]):
            start(module["id"], canvas)

        self.assertEqual(Path(self.store.path).read_bytes(), before_config)
        self.assertEqual(Path(self.device_store.path).read_bytes(), before_devices)
        self.assertEqual(self.controller.method_calls, [])
        registry = self.app.extensions.get("edit_preview_registry")
        if registry is not None:
            registry.close()

    def test_edit_preview_routes_bound_unknown_length_json_before_rendering(self):
        self.pair_browser()
        instance = "a" * 32
        candidate = copy.deepcopy(self.store.get())
        candidate.pop("revision")
        payload = json.dumps({
            "config": candidate,
            "generation": 1,
            "preview_instance": instance,
        }, separators=(",", ":")).encode("utf-8")
        self.assertLess(len(payload), 1_048_576)

        def unknown_length(path, body):
            return self.client.open(
                path, method="POST", input_stream=io.BytesIO(body),
                content_type="application/json",
                environ_overrides={
                    "CONTENT_LENGTH": "", "wsgi.input_terminated": True,
                },
            )

        with patch.object(
            webapp, "_edit_preview_iterator",
            side_effect=AssertionError("oversized request reached renderer"),
        ) as renderer:
            oversized = unknown_length(
                "/api/edit-preview/clock_lava", b"{" + b" " * 1_048_576,
            )
            self.assertEqual(oversized.status_code, 413, oversized.get_data(as_text=True))
            self.assertEqual(oversized.get_json(), {"error": "Preview request exceeds 1 MiB."})
            self.assertEqual(oversized.headers.get("Cache-Control"), "no-store")
            renderer.assert_not_called()

        exact_valid = payload + b" " * (1_048_576 - len(payload))
        with patch.object(
            webapp, "_edit_preview_iterator",
            return_value=iter([{"png": "frame", "delay_ms": 1}]),
        ):
            accepted = unknown_length("/api/edit-preview/clock_lava", exact_valid)
        self.assertEqual(accepted.status_code, 200, accepted.get_data(as_text=True))
        self.assertEqual(accepted.headers.get("Cache-Control"), "no-store")
        accepted_body = accepted.get_json()
        if not accepted_body["complete"]:
            self.client.post("/api/edit-preview/cancel", json={
                "session": accepted_body["session"], "preview_instance": instance,
            })

        exact_malformed = unknown_length(
            "/api/edit-preview/clock_lava", b"{" + b" " * 1_048_575,
        )
        self.assertEqual(exact_malformed.status_code, 400)
        self.assertEqual(exact_malformed.get_json(), {"error": "Preview request is invalid."})
        self.assertEqual(exact_malformed.headers.get("Cache-Control"), "no-store")

        oversized_control = b"{" + b" " * 1_048_576
        for path in ("/api/edit-preview/page", "/api/edit-preview/cancel"):
            with self.subTest(path=path):
                response = unknown_length(path, oversized_control)
                self.assertEqual(response.status_code, 413, response.get_data(as_text=True))
                self.assertEqual(response.get_json(), {"error": "Preview request exceeds 1 MiB."})
                self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    def test_preview_services_restart_after_close_and_failed_init(self):
        """Process preview services restart atomically without leaking stale work."""
        from contextlib import contextmanager
        from edit_preview import PreviewInstanceRegistry
        from preview_provider import PreviewProviderCancelled

        start_path = "/api/edit-preview/clock_lava"
        weather_path = "/api/edit-preview/weather"
        cancel_path = "/api/edit-preview/cancel"
        preview_instance = "51515151515151515151515151515151"
        self.pair_browser()
        with self.client.session_transaction() as paired_session:
            browser_session = dict(paired_session)
        self.controller.reset_mock()

        before_config = Path(self.store.path).read_bytes()
        before_devices = Path(self.device_store.path).read_bytes()
        before_media = sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        )
        baseline_threads = {thread.ident for thread in threading.enumerate()}
        baseline_fds = len(os.listdir("/proc/self/fd"))
        registries = []
        providers = []
        fail_provider_init = threading.Event()
        provider_prepare_started = threading.Event()

        class TrackedRegistry(PreviewInstanceRegistry):
            def __init__(self):
                super().__init__()
                self.close_calls = 0
                registries.append(self)

            def close(self):
                was_closed = self.is_closed
                super().close()
                if not was_closed:
                    self.close_calls += 1

        class TrackedProvider:
            MAX_WORKERS = 2

            def __init__(self):
                if fail_provider_init.is_set():
                    fail_provider_init.clear()
                    raise RuntimeError("private provider factory detail")
                self._condition = threading.Condition()
                self._closed = False
                self._active = set()
                self.close_calls = 0
                self.prepare_calls = 0
                providers.append(self)

            @property
            def is_closed(self):
                with self._condition:
                    return self._closed

            @property
            def active_child_count(self):
                return 0

            @property
            def active_worker_count(self):
                with self._condition:
                    return len(self._active)

            @property
            def available_worker_slots(self):
                return self.MAX_WORKERS - self.active_worker_count

            def prepare(self, provider, *, settings, credentials, cancel_event,
                        request_deadline):
                location = settings["weather"]["location"]
                owner_stop = threading.Event()
                with self._condition:
                    if self._closed:
                        raise RuntimeError("closed provider must not serve")
                    self.prepare_calls += 1
                    self._active.add(owner_stop)
                try:
                    if location == "blocked-old":
                        provider_prepare_started.set()
                        while not (cancel_event.is_set() or owner_stop.wait(0.005)):
                            pass
                        raise PreviewProviderCancelled("private cancellation detail")
                    return (
                        ("feels_like", 19), ("high", 23), ("humidity", 50),
                        ("location", location), ("low", 12),
                        ("status", "Clear sky"), ("temperature", 20),
                    )
                finally:
                    with self._condition:
                        self._active.remove(owner_stop)
                        self._condition.notify_all()

            def shutdown(self):
                with self._condition:
                    if not self._closed:
                        self._closed = True
                        self.close_calls += 1
                    for owner_stop in self._active:
                        owner_stop.set()
                    while self._active:
                        self._condition.wait(0.25)

        @contextmanager
        def pinned(candidate, media_root, *, allow_missing=()):
            yield {}

        def iterator(module_name, candidate, selected_palette, pinned_media, prepared,
                     stop_event):
            for index in range(40):
                yield {"png": f"{candidate['weather']['location']}-{index}", "delay_ms": 1}

        def payload(generation, *, location="singleton"):
            candidate = copy.deepcopy(self.store.get())
            candidate.pop("revision")
            candidate["weather"]["location"] = location
            return {
                "config": candidate,
                "generation": generation,
                "preview_instance": preview_instance,
            }

        def paired_client():
            client = self.app.test_client()
            with client.session_transaction() as client_session:
                client_session.update(browser_session)
            return client

        def assert_response(response, status):
            self.assertEqual(response.status_code, status, response.get_data(as_text=True))
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertNotIn("ETag", response.headers)
            body = response.get_json()
            self.assertNotIn("revision", body)
            self.assertNotIn("device_id", json.dumps(body))
            return body

        factory_patch = patch.multiple(
            "webapp", PreviewInstanceRegistry=TrackedRegistry,
            PreviewProvider=TrackedProvider, pin_candidate_media=pinned,
            _edit_preview_iterator=iterator,
        )
        with factory_patch:
            # Lazy creation is one registry/provider pair, shared by successful
            # requests and independent Flask clients. Request teardown owns neither.
            self.assertNotIn("edit_preview_registry", self.app.extensions)
            first = assert_response(self.client.post(start_path, json=payload(1)), 200)
            first_registry = self.app.extensions["edit_preview_registry"]
            first_provider = self.app.extensions["edit_preview_provider"]
            self.assertEqual((len(registries), len(providers)), (1, 1))
            second = assert_response(paired_client().post(
                cancel_path, json={
                    "session": first["session"],
                    "preview_instance": preview_instance,
                },
            ), 200)
            self.assertEqual(second, {"cancelled": True})
            again = assert_response(paired_client().post(
                start_path, json=payload(2),
            ), 200)
            self.assertIs(self.app.extensions["edit_preview_registry"], first_registry)
            self.assertIs(self.app.extensions["edit_preview_provider"], first_provider)
            self.assertEqual((len(registries), len(providers)), (1, 1))
            assert_response(self.client.post(cancel_path, json={
                "session": again["session"], "preview_instance": preview_instance,
            }), 200)

            # Explicit close replaces both closed services. Concurrent first use
            # after close must publish exactly one replacement pair.
            self.app.extensions["close_edit_preview_services"]()
            self.assertTrue(first_registry.is_closed)
            self.assertTrue(first_provider.is_closed)
            self.assertEqual((first_registry.close_calls, first_provider.close_calls), (1, 1))
            results = []
            failures = []
            barrier = threading.Barrier(3)

            def concurrent_start(index):
                try:
                    barrier.wait()
                    results.append((index, paired_client().post(
                        start_path,
                        json={**payload(index + 3), "preview_instance": f"{index + 1:032x}"},
                    )))
                except BaseException as error:
                    failures.append(error)

            threads = [
                threading.Thread(target=concurrent_start, args=(index,),
                                 name=f"preview-restart-{index}")
                for index in range(2)
            ]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(1)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(failures, [])
            self.assertEqual([response.status_code for _index, response in results], [200, 200])
            self.assertEqual((len(registries), len(providers)), (2, 2))
            replacement_registry = self.app.extensions["edit_preview_registry"]
            replacement_provider = self.app.extensions["edit_preview_provider"]
            self.assertIsNot(replacement_registry, first_registry)
            self.assertIsNot(replacement_provider, first_provider)
            for index, response in results:
                body = assert_response(response, 200)
                assert_response(paired_client().post(cancel_path, json={
                    "session": body["session"],
                    "preview_instance": f"{index + 1:032x}",
                }), 200)

            # A failed pair construction is sanitized and never cached. The
            # partially-created registry is closed exactly once, then retried.
            self.app.extensions["close_edit_preview_services"]()
            fail_provider_init.set()
            failed = assert_response(self.client.post(
                start_path, json=payload(20),
            ), 503)
            self.assertEqual(failed, {"error": "Preview is temporarily unavailable."})
            partial_registry = registries[-1]
            self.assertTrue(partial_registry.is_closed)
            self.assertEqual(partial_registry.close_calls, 1)
            self.assertIs(self.app.extensions.get("edit_preview_registry"), replacement_registry)
            recovered = assert_response(self.client.post(
                start_path, json=payload(21),
            ), 200)
            healthy_registry = self.app.extensions["edit_preview_registry"]
            healthy_provider = self.app.extensions["edit_preview_provider"]
            self.assertIsNot(healthy_registry, partial_registry)
            self.assertFalse(healthy_registry.is_closed)
            self.assertFalse(healthy_provider.is_closed)
            self.assertEqual((len(registries), len(providers)), (4, 3))
            assert_response(self.client.post(cancel_path, json={
                "session": recovered["session"],
                "preview_instance": preview_instance,
            }), 200)

            # A newer generation must also cancel pre-session provider work
            # promptly and preserve the typed superseded HTTP outcome.
            superseded_result = {}
            superseded_client = paired_client()
            provider_prepare_started.clear()

            def superseded_start():
                superseded_result["response"] = superseded_client.post(
                    weather_path, json=payload(29, location="blocked-old"),
                )

            superseded_thread = threading.Thread(
                target=superseded_start, name="preview-superseded-provider-init",
            )
            superseded_thread.start()
            self.assertTrue(provider_prepare_started.wait(1))
            newer_at = time.monotonic()
            superseding = assert_response(paired_client().post(
                weather_path, json=payload(30, location="new-healthy"),
            ), 200)
            self.assertLess(time.monotonic() - newer_at, 0.25)
            superseded_thread.join(0.25)
            self.assertFalse(superseded_thread.is_alive())
            stale_generation = assert_response(superseded_result["response"], 409)
            self.assertEqual(
                stale_generation, {"error": "Preview generation was superseded."},
            )
            assert_response(paired_client().post(cancel_path, json={
                "session": superseding["session"],
                "preview_instance": preview_instance,
            }), 200)

            # Closing during pre-session provider preparation signals promptly.
            # A newer service generation for the same identity is not blocked by
            # the old PreviewInstance admission lock and stale work publishes none.
            old_result = {}
            old_client = paired_client()
            provider_prepare_started.clear()

            def blocked_start():
                old_result["response"] = old_client.post(
                    weather_path, json=payload(30, location="blocked-old"),
                )

            old_thread = threading.Thread(
                target=blocked_start, name="preview-blocked-provider-init",
            )
            old_thread.start()
            self.assertTrue(provider_prepare_started.wait(1))
            closed_at = time.monotonic()
            self.app.extensions["close_edit_preview_services"]()
            self.assertLess(time.monotonic() - closed_at, 0.25)
            newer = assert_response(paired_client().post(
                weather_path, json=payload(31, location="new-healthy"),
            ), 200)
            old_thread.join(0.25)
            self.assertFalse(old_thread.is_alive())
            stale = assert_response(old_result["response"], 503)
            self.assertEqual(stale, {"error": "Preview is temporarily unavailable."})
            self.assertNotIn("frames", stale)
            assert_response(paired_client().post(cancel_path, json={
                "session": newer["session"], "preview_instance": preview_instance,
            }), 200)

            self.app.extensions["close_edit_preview_services"]()
            self.app.extensions["close_edit_preview_services"]()

        self.assertTrue(all(registry.is_closed for registry in registries))
        self.assertTrue(all(registry.total_count == 0 for registry in registries))
        self.assertTrue(all(registry.expired_count == 0 for registry in registries))
        self.assertTrue(all(not registry.reaper_alive for registry in registries))
        self.assertTrue(all(registry.close_calls == 1 for registry in registries))
        self.assertTrue(all(provider.is_closed for provider in providers))
        self.assertTrue(all(provider.close_calls == 1 for provider in providers))
        self.assertTrue(all(provider.active_worker_count == 0 for provider in providers))
        self.assertTrue(all(provider.available_worker_slots == 2 for provider in providers))
        self.assertEqual(Path(self.store.path).read_bytes(), before_config)
        self.assertEqual(Path(self.device_store.path).read_bytes(), before_devices)
        self.assertEqual(sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        ), before_media)
        self.assertEqual(self.controller.method_calls, [])
        self.assertEqual(
            {thread.ident for thread in threading.enumerate()} - baseline_threads, set(),
        )
        self.assertEqual(len(os.listdir("/proc/self/fd")), baseline_fds)

    def test_preview_service_failed_initialization_leaves_no_reaper_child_or_fd(self):
        """Failed service factories clean partial resources before one atomic retry."""
        from contextlib import contextmanager
        import multiprocessing
        import subprocess

        from edit_preview import PreviewInstanceRegistry, PreviewSessionCapacity

        self.pair_browser()
        with self.client.session_transaction() as paired:
            browser_session = dict(paired)
            device_id = paired["device_id"]
        self.controller.reset_mock()

        before_children = {child.pid for child in multiprocessing.active_children()}
        before_threads = {(thread.ident, thread.name) for thread in threading.enumerate()}
        before_fds = len(list(Path("/proc/self/fd").iterdir()))
        before_config = Path(self.store.path).read_bytes()
        before_devices = Path(self.device_store.path).read_bytes()
        before_media = sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        )

        phase = {"failure": "registry"}
        close_services = self.app.extensions["close_edit_preview_services"]
        factory_started = threading.Event()
        release_factory = threading.Event()
        registries = []
        providers = []
        failures = []
        serial = iter(range(1, 100))

        class TrackedRegistry(PreviewInstanceRegistry):
            def __init__(self):
                self.capacity = PreviewSessionCapacity()
                super().__init__(capacity=self.capacity)
                self.read_fd, self.write_fd = os.pipe()
                self.fd_close_calls = {self.read_fd: 0, self.write_fd: 0}
                self.close_calls = 0
                self.lock_probe_calls = 0
                self.probe_service_lock = False
                self._tracked_closed = False
                registries.append(self)

            def close(self):
                if not self._tracked_closed:
                    self._tracked_closed = True
                    if self.probe_service_lock:
                        callback = threading.Thread(
                            target=close_services,
                            name="preview-partial-registry-lock-probe",
                        )
                        callback.start()
                        callback.join(0.25)
                        if callback.is_alive():
                            raise AssertionError("registry cleanup held app service lock")
                        self.lock_probe_calls += 1
                    super().close()
                    for descriptor in (self.read_fd, self.write_fd):
                        os.close(descriptor)
                        self.fd_close_calls[descriptor] += 1
                    self.close_calls += 1

        class TrackedProvider:
            MAX_WORKERS = 2

            def __init__(self):
                self._closed = False
                self.close_calls = 0
                self.worker_join_calls = 0
                self.child_reap_calls = 0
                self.child_handle_close_calls = 0
                self.lock_probe_calls = 0
                self.probe_service_lock = False
                self.read_fd, self.write_fd = os.pipe()
                self.fd_close_calls = {self.read_fd: 0, self.write_fd: 0}
                self.stop = threading.Event()
                self.worker = threading.Thread(
                    target=self.stop.wait,
                    name=f"preview-init-worker-{next(serial)}",
                    daemon=True,
                )
                self.worker.start()
                self.child = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                providers.append(self)

            @property
            def is_closed(self):
                return self._closed

            @property
            def active_worker_count(self):
                return int(self.worker.is_alive())

            @property
            def active_child_count(self):
                return int(self.child.poll() is None)

            @property
            def available_worker_slots(self):
                return self.MAX_WORKERS - self.active_worker_count

            def prepare(self, provider, *, settings, credentials, cancel_event,
                        request_deadline):
                raise AssertionError("clock preview must not invoke provider preparation")

            def shutdown(self):
                if self._closed:
                    return
                self._closed = True
                if self.probe_service_lock:
                    callback = threading.Thread(
                        target=close_services,
                        name="preview-partial-provider-lock-probe",
                    )
                    callback.start()
                    callback.join(0.25)
                    if callback.is_alive():
                        raise AssertionError("provider cleanup held app service lock")
                    self.lock_probe_calls += 1
                self.stop.set()
                self.worker.join(1)
                self.worker_join_calls += 1
                if self.child.poll() is None:
                    self.child.terminate()
                self.child.wait(timeout=1)
                self.child_reap_calls += 1
                self.child_handle_close_calls += 1
                for descriptor in (self.read_fd, self.write_fd):
                    os.close(descriptor)
                    self.fd_close_calls[descriptor] += 1
                self.close_calls += 1

        def registry_factory():
            registry = TrackedRegistry()
            if phase["failure"] == "registry":
                phase["failure"] = None
                factory_started.set()
                if not release_factory.wait(1):
                    raise AssertionError("registry factory release timed out")
                registry.probe_service_lock = True
                registry.close()
                raise RuntimeError("private registry init worker/fd identifier")
            return registry

        def provider_factory():
            provider = TrackedProvider()
            if phase["failure"] == "provider":
                phase["failure"] = None
                factory_started.set()
                if not release_factory.wait(1):
                    raise AssertionError("provider factory release timed out")
                provider.probe_service_lock = True
                provider.shutdown()
                raise RuntimeError(
                    f"private provider child {provider.child.pid} fd {provider.read_fd}"
                )
            return provider

        @contextmanager
        def pinned(candidate, media_root, *, allow_missing=()):
            yield {}

        def iterator(module_name, candidate, selected_palette, pinned_media, prepared,
                     stop_event):
            for index in range(40):
                yield {"png": f"safe-frame-{index}", "delay_ms": 1}

        def paired_client():
            client = self.app.test_client()
            with client.session_transaction() as session:
                session.update(browser_session)
            return client

        def payload(index):
            candidate = copy.deepcopy(self.store.get())
            candidate.pop("revision")
            return {
                "config": candidate,
                "generation": index,
                "preview_instance": f"{index:032x}",
            }

        def assert_transport(response, status):
            self.assertEqual(response.status_code, status, response.get_data(as_text=True))
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertNotIn("ETag", response.headers)
            body = response.get_json()
            serialized = json.dumps(body)
            self.assertNotIn("revision", body)
            self.assertNotIn("device_id", serialized)
            self.assertNotIn(device_id, serialized)
            self.assertNotIn("private", serialized)
            return body

        def exercise_failed_boundary(boundary, base_index):
            phase["failure"] = boundary
            factory_started.clear()
            release_factory.clear()
            owner_result = {}
            waiter_results = []
            boundary_failures = []

            def request(index, destination):
                try:
                    destination.append((index, paired_client().post(
                        "/api/edit-preview/clock_lava", json=payload(index),
                    )))
                except BaseException as error:
                    boundary_failures.append(error)

            owner_values = []
            owner = threading.Thread(
                target=request, args=(base_index, owner_values),
                name=f"preview-{boundary}-failure-owner",
            )
            owner.start()
            self.assertTrue(factory_started.wait(1))
            partial_registry = registries[-1]
            partial_provider = providers[-1] if boundary == "provider" else None
            self.assertIsNot(
                self.app.extensions.get("edit_preview_registry"), partial_registry,
            )
            if partial_provider is not None:
                self.assertIsNot(
                    self.app.extensions.get("edit_preview_provider"), partial_provider,
                )

            waiters = [
                threading.Thread(
                    target=request, args=(base_index + offset, waiter_results),
                    name=f"preview-{boundary}-waiter-{offset}",
                )
                for offset in (1, 2)
            ]
            for waiter in waiters:
                waiter.start()
            release_factory.set()
            owner.join(1)
            for waiter in waiters:
                waiter.join(1)
            self.assertFalse(owner.is_alive())
            self.assertFalse(any(waiter.is_alive() for waiter in waiters))
            self.assertEqual(boundary_failures, [])
            self.assertEqual(len(owner_values), 1)
            owner_result["body"] = assert_transport(owner_values[0][1], 503)
            self.assertEqual(
                owner_result["body"],
                {"error": "Preview is temporarily unavailable."},
            )
            self.assertEqual(len(waiter_results), 2)
            healthy_registry = self.app.extensions["edit_preview_registry"]
            healthy_provider = self.app.extensions["edit_preview_provider"]
            self.assertIsNot(healthy_registry, partial_registry)
            self.assertIsNot(healthy_provider, partial_provider)
            sessions = []
            for index, response in waiter_results:
                body = assert_transport(response, 200)
                sessions.append((index, body["session"]))
            self.assertIs(
                self.app.extensions["get_edit_preview_services"]()[0], healthy_registry,
            )
            self.assertIs(
                self.app.extensions["get_edit_preview_services"]()[1], healthy_provider,
            )
            first_index, first_session = sessions.pop(0)
            cancelled = assert_transport(paired_client().post(
                "/api/edit-preview/cancel", json={
                    "session": first_session,
                    "preview_instance": f"{first_index:032x}",
                },
            ), 200)
            self.assertEqual(cancelled, {"cancelled": True})
            reused = assert_transport(paired_client().post(
                "/api/edit-preview/clock_lava", json=payload(base_index + 3),
            ), 200)
            sessions.append((base_index + 3, reused["session"]))
            self.assertIs(self.app.extensions["edit_preview_registry"], healthy_registry)
            self.assertIs(self.app.extensions["edit_preview_provider"], healthy_provider)
            for index, session_id in sessions:
                cancelled = assert_transport(paired_client().post(
                    "/api/edit-preview/cancel", json={
                        "session": session_id,
                        "preview_instance": f"{index:032x}",
                    },
                ), 200)
                self.assertEqual(cancelled, {"cancelled": True})
            self.assertEqual(
                (healthy_registry.total_count, healthy_registry.expired_count), (0, 0),
            )
            self.assertEqual(
                (healthy_registry.capacity.active_count,
                 healthy_registry.capacity.waiting_count),
                (0, 0),
            )
            return partial_registry, partial_provider, healthy_registry, healthy_provider

        factory_patch = patch.multiple(
            "webapp", PreviewInstanceRegistry=registry_factory,
            PreviewProvider=provider_factory, pin_candidate_media=pinned,
            _edit_preview_iterator=iterator,
        )
        try:
            with factory_patch:
                registry_partial, _provider_partial, first_registry, first_provider = (
                    exercise_failed_boundary("registry", 101)
                )
                self.assertTrue(registry_partial.is_closed)
                self.assertEqual(registry_partial.close_calls, 1)
                self.assertEqual(registry_partial.lock_probe_calls, 1)
                self.assertEqual(registry_partial.reaper_join_count, 1)
                self.assertFalse(registry_partial.reaper_alive)

                self.app.extensions["close_edit_preview_services"]()
                provider_registry, provider_partial, second_registry, second_provider = (
                    exercise_failed_boundary("provider", 201)
                )
                self.assertTrue(provider_registry.is_closed)
                self.assertEqual(provider_registry.close_calls, 1)
                self.assertEqual(provider_registry.reaper_join_count, 1)
                self.assertFalse(provider_registry.reaper_alive)
                self.assertTrue(provider_partial.is_closed)
                self.assertEqual(
                    (provider_partial.close_calls, provider_partial.worker_join_calls,
                     provider_partial.child_reap_calls,
                     provider_partial.child_handle_close_calls,
                     provider_partial.lock_probe_calls),
                    (1, 1, 1, 1, 1),
                )
                self.assertEqual(set(provider_partial.fd_close_calls.values()), {1})
                self.app.extensions["close_edit_preview_services"]()
                self.app.extensions["close_edit_preview_services"]()
        finally:
            release_factory.set()
            for registry in registries:
                registry.close()
            for provider in providers:
                provider.shutdown()

        self.assertEqual(len(registries), 4)
        self.assertEqual(len(providers), 3)
        self.assertTrue(all(registry.is_closed for registry in registries))
        self.assertTrue(all(registry.close_calls == 1 for registry in registries))
        self.assertTrue(all(registry.reaper_join_count == 1 for registry in registries))
        self.assertTrue(all(not registry.reaper_alive for registry in registries))
        self.assertTrue(all(registry.total_count == 0 for registry in registries))
        self.assertTrue(all(registry.expired_count == 0 for registry in registries))
        self.assertTrue(all(registry.capacity.active_count == 0 for registry in registries))
        self.assertTrue(all(registry.capacity.waiting_count == 0 for registry in registries))
        self.assertTrue(all(set(registry.fd_close_calls.values()) == {1}
                            for registry in registries))
        self.assertTrue(all(provider.is_closed for provider in providers))
        self.assertTrue(all(provider.close_calls == 1 for provider in providers))
        self.assertTrue(all(provider.worker_join_calls == 1 for provider in providers))
        self.assertTrue(all(provider.child_reap_calls == 1 for provider in providers))
        self.assertTrue(all(provider.child_handle_close_calls == 1 for provider in providers))
        self.assertTrue(all(set(provider.fd_close_calls.values()) == {1}
                            for provider in providers))
        self.assertTrue(all(provider.active_worker_count == 0 for provider in providers))
        self.assertTrue(all(provider.active_child_count == 0 for provider in providers))
        self.assertEqual(Path(self.store.path).read_bytes(), before_config)
        self.assertEqual(Path(self.device_store.path).read_bytes(), before_devices)
        self.assertEqual(sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        ), before_media)
        self.assertEqual(self.controller.method_calls, [])
        self.assertEqual(
            {child.pid for child in multiprocessing.active_children()}, before_children,
        )
        self.assertEqual(
            {(thread.ident, thread.name) for thread in threading.enumerate()}, before_threads,
        )
        self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), before_fds)

    def test_same_page_slow_generation_is_superseded_by_newer_generation(self):
        from contextlib import contextmanager

        start_path = "/api/edit-preview/clock_lava"
        page_path = "/api/edit-preview/page"
        cancel_path = "/api/edit-preview/cancel"
        preview_instance = "44444444444444444444444444444444"

        self.pair_browser()
        with self.client.session_transaction() as browser_session:
            paired_session = dict(browser_session)
            device_id = browser_session["device_id"]
        old_page_client = self.app.test_client()
        new_start_client = self.app.test_client()
        for client in (old_page_client, new_start_client):
            with client.session_transaction() as client_session:
                client_session.update(paired_session)

        self.controller.reset_mock()
        before_config = Path(self.store.path).read_bytes()
        before_devices = Path(self.device_store.path).read_bytes()
        before_media = sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        )
        iterators = {}
        stop_events = {}
        constructed = []
        pin_enters = []
        pin_exits = []

        class SlowIterator:
            def __init__(self, label, *, block_on_call=None):
                self.label = label
                self.block_on_call = block_on_call
                self.next_calls = 0
                self.close_calls = 0
                self.concurrent_close_calls = 0
                self.active = False
                self.lookahead_ready = threading.Event()
                self.release_lookahead = threading.Event()

            def __iter__(self):
                return self

            def __next__(self):
                self.next_calls += 1
                if self.next_calls > 100:
                    raise StopIteration
                self.active = True
                try:
                    frame = {
                        "png": f"{self.label}-frame-{self.next_calls}",
                        "delay_ms": self.next_calls,
                    }
                    if self.next_calls == self.block_on_call:
                        self.lookahead_ready.set()
                        if not self.release_lookahead.wait(2):
                            raise AssertionError("slow lookahead was not released")
                    return frame
                finally:
                    self.active = False

            def close(self):
                if self.active:
                    self.concurrent_close_calls += 1
                    raise AssertionError("iterator close overlapped advancement")
                self.close_calls += 1
                if self.close_calls > 1:
                    raise AssertionError("iterator closed more than once")

        @contextmanager
        def pinned(candidate, media_root, *, allow_missing=()):
            label = candidate["weather"]["location"]
            pin_enters.append(label)
            try:
                yield {}
            finally:
                pin_exits.append(label)

        def prepare(_provider, module_name, candidate, stop_event):
            label = candidate["weather"]["location"]
            stop_events[label] = stop_event
            return {"label": label}

        def iterator(module_name, candidate, selected_palette, pinned_media, prepared,
                     stop_event):
            label = prepared["label"]
            constructed.append(label)
            current = SlowIterator(label, block_on_call=34 if label == "old-slow" else None)
            iterators[label] = current
            return current

        def payload(label, generation):
            candidate = copy.deepcopy(self.store.get())
            candidate.pop("revision")
            candidate["weather"]["location"] = label
            return {
                "config": candidate,
                "generation": generation,
                "preview_instance": preview_instance,
            }

        def assert_response(response, status):
            self.assertEqual(response.status_code, status, response.get_data(as_text=True))
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertNotIn("ETag", response.headers)
            body = response.get_json()
            self.assertIsInstance(body, dict)
            serialized = json.dumps(body)
            self.assertNotIn("revision", body)
            self.assertNotIn("device_id", serialized)
            self.assertNotIn(device_id, serialized)
            return body

        def continuation(page):
            return {
                "session": page["session"],
                "preview_instance": preview_instance,
                "token": page["next_token"],
            }

        registry = None
        old_result = {}
        old_failure = []
        with patch("webapp.pin_candidate_media", pinned), patch(
            "webapp._prepare_edit_preview_provider", prepare, create=True,
        ), patch("webapp._edit_preview_iterator", iterator, create=True):
            old = assert_response(
                self.client.post(start_path, json=payload("old-slow", 7)), 200,
            )
            self.assertEqual(old["generation"], 7)
            self.assertEqual(old["page"], 0)
            self.assertEqual(len(old["frames"]), 32)
            self.assertFalse(old["complete"])
            registry = self.app.extensions["edit_preview_registry"]
            old_instance = registry.lookup(
                old["session"], (device_id, preview_instance),
            )
            old_session = old_instance.admit()

            def continue_old():
                try:
                    old_result["response"] = old_page_client.post(
                        page_path, json=continuation(old),
                    )
                except BaseException as error:
                    old_failure.append(error)

            old_thread = threading.Thread(
                target=continue_old, name="slow-edit-preview-route-page",
            )
            old_thread.start()
            self.assertTrue(
                iterators["old-slow"].lookahead_ready.wait(1),
                "old route did not reach the continuation lookahead barrier",
            )

            newer_started = time.monotonic()
            newer = assert_response(
                new_start_client.post(start_path, json=payload("new-fast", 8)), 200,
            )
            newer_latency = time.monotonic() - newer_started
            self.assertLess(newer_latency, 0.25)
            self.assertEqual(newer["generation"], 8)
            self.assertEqual(newer["page"], 0)
            self.assertEqual(len(newer["frames"]), 32)
            self.assertTrue(stop_events["old-slow"].is_set())
            self.assertFalse(stop_events["new-fast"].is_set())
            self.assertTrue(old_thread.is_alive())
            self.assertEqual(registry.total_count, 2)

            new_page_started = time.monotonic()
            newer_second = assert_response(new_start_client.post(
                page_path, json=continuation(newer),
            ), 200)
            self.assertLess(time.monotonic() - new_page_started, 0.25)
            self.assertEqual(newer_second["generation"], 8)
            self.assertEqual(newer_second["page"], 1)
            self.assertEqual(len(newer_second["frames"]), 32)
            self.assertFalse(newer_second["complete"])

            constructed_before_rejection = list(constructed)
            advances_before_rejection = {
                label: current.next_calls for label, current in iterators.items()
            }
            for label, generation in (("equal-rejected", 8), ("older-rejected", 6)):
                with self.subTest(rejected_generation=generation):
                    assert_response(new_start_client.post(
                        start_path, json=payload(label, generation),
                    ), 400)
                    self.assertEqual(constructed, constructed_before_rejection)
                    self.assertEqual(
                        {name: current.next_calls for name, current in iterators.items()},
                        advances_before_rejection,
                    )

            iterators["old-slow"].release_lookahead.set()
            old_thread.join(0.25)
            self.assertFalse(old_thread.is_alive())
            self.assertEqual(old_failure, [])
            superseded = assert_response(old_result["response"], 409)
            self.assertEqual(superseded, {"error": "Preview generation was superseded."})
            self.assertNotIn("frames", superseded)
            self.assertNotIn("next_token", superseded)
            self.assertNotIn("session", superseded)
            self.assertNotIn("generation", superseded)
            self.assertEqual(old_session.retained_frame_count, 0)
            self.assertEqual(iterators["old-slow"].close_calls, 1)
            self.assertEqual(iterators["old-slow"].concurrent_close_calls, 0)
            self.assertEqual(pin_exits.count("old-slow"), 1)
            self.assertEqual(registry.total_count, 1)

            newer_third = assert_response(new_start_client.post(
                page_path, json=continuation(newer_second),
            ), 200)
            self.assertEqual(newer_third["generation"], 8)
            self.assertEqual(newer_third["page"], 2)
            self.assertEqual(len(newer_third["frames"]), 32)
            self.assertFalse(newer_third["complete"])
            self.assertFalse(stop_events["new-fast"].is_set())
            cancelled = assert_response(new_start_client.post(cancel_path, json={
                "session": newer["session"],
                "preview_instance": preview_instance,
            }), 200)
            self.assertEqual(cancelled, {"cancelled": True})
            self.assertEqual(iterators["new-fast"].close_calls, 1)
            self.assertEqual(iterators["new-fast"].concurrent_close_calls, 0)
            self.assertEqual(pin_exits.count("new-fast"), 1)
            self.assertEqual(registry.total_count, 0)

        self.assertCountEqual(
            pin_enters, ["old-slow", "new-fast"],
        )
        self.assertCountEqual(pin_exits, pin_enters)
        self.assertEqual(Path(self.store.path).read_bytes(), before_config)
        self.assertEqual(Path(self.device_store.path).read_bytes(), before_devices)
        self.assertEqual(sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        ), before_media)
        self.assertEqual(self.controller.method_calls, [])
        registry.close()
        self.assertFalse(registry.reaper_alive)
        self.assertNotIn(old_thread, threading.enumerate())

    def test_two_tabs_on_one_paired_device_do_not_cancel_each_other(self):
        from contextlib import contextmanager

        start_path = "/api/edit-preview/clock_lava"
        page_path = "/api/edit-preview/page"
        cancel_path = "/api/edit-preview/cancel"
        tab_a_instance = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        tab_b_instance = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

        self.pair_browser()
        with self.client.session_transaction() as paired_browser_session:
            paired_identity = dict(paired_browser_session)
            device_id = paired_browser_session["device_id"]
        tab_a_client = self.app.test_client()
        tab_b_client = self.app.test_client()
        for client in (tab_a_client, tab_b_client):
            with client.session_transaction() as tab_session:
                tab_session.update(paired_identity)
            with client.session_transaction() as tab_session:
                self.assertEqual(tab_session["device_id"], device_id)

        self.controller.reset_mock()
        before_config = Path(self.store.path).read_bytes()
        before_devices = Path(self.device_store.path).read_bytes()
        before_media = sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        )
        iterators = {}
        stop_events = {}
        pin_enters = []
        pin_exits = []

        class CountingIterator:
            def __init__(self, label):
                self.label = label
                self.next_calls = 0
                self.close_calls = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.next_calls += 1
                return {
                    "png": f"{self.label}-frame-{self.next_calls}",
                    "delay_ms": self.next_calls,
                }

            def close(self):
                self.close_calls += 1
                if self.close_calls > 1:
                    raise AssertionError(f"{self.label} iterator closed more than once")

        @contextmanager
        def pinned(candidate, media_root, *, allow_missing=()):
            label = candidate["weather"]["location"]
            pin_enters.append(label)
            try:
                yield {}
            finally:
                pin_exits.append(label)

        def prepare(_provider, module_name, candidate, stop_event):
            label = candidate["weather"]["location"]
            stop_events[label] = stop_event
            return {"label": label}

        def iterator(module_name, candidate, selected_palette, pinned_media, prepared,
                     stop_event):
            label = prepared["label"]
            current = CountingIterator(label)
            iterators[label] = current
            return current

        def payload(label, preview_instance):
            candidate = copy.deepcopy(self.store.get())
            candidate.pop("revision")
            candidate["weather"]["location"] = label
            return {
                "config": candidate,
                "generation": 1,
                "preview_instance": preview_instance,
            }

        def assert_response(response, status):
            self.assertEqual(response.status_code, status, response.get_data(as_text=True))
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertNotIn("ETag", response.headers)
            body = response.get_json()
            self.assertIsInstance(body, dict)
            serialized = json.dumps(body)
            self.assertNotIn("revision", body)
            self.assertNotIn("device_id", serialized)
            self.assertNotIn(device_id, serialized)
            return body

        def continuation(page, preview_instance):
            return {
                "session": page["session"],
                "preview_instance": preview_instance,
                "token": page["next_token"],
            }

        registry = None
        with patch("webapp.pin_candidate_media", pinned), patch(
            "webapp._prepare_edit_preview_provider", prepare, create=True,
        ), patch("webapp._edit_preview_iterator", iterator, create=True):
            tab_a = assert_response(tab_a_client.post(
                start_path, json=payload("tab-a", tab_a_instance),
            ), 200)
            tab_b = assert_response(tab_b_client.post(
                start_path, json=payload("tab-b", tab_b_instance),
            ), 200)
            registry = self.app.extensions["edit_preview_registry"]
            self.assertEqual(registry.total_count, 2)
            self.assertEqual(registry.count_for_device(device_id), 2)
            self.assertNotEqual(tab_a["session"], tab_b["session"])
            self.assertNotEqual(tab_a["next_token"], tab_b["next_token"])
            self.assertEqual(iterators["tab-a"].next_calls, 33)
            self.assertEqual(iterators["tab-b"].next_calls, 33)

            baseline_advances = {
                label: current.next_calls for label, current in iterators.items()
            }
            assert_response(tab_b_client.post(
                page_path, json=continuation(tab_a, tab_b_instance),
            ), 409)
            assert_response(tab_a_client.post(
                page_path, json=continuation(tab_b, tab_a_instance),
            ), 409)
            assert_response(tab_b_client.post(cancel_path, json={
                "session": tab_a["session"],
                "preview_instance": tab_b_instance,
            }), 409)
            assert_response(tab_a_client.post(cancel_path, json={
                "session": tab_b["session"],
                "preview_instance": tab_a_instance,
            }), 409)
            self.assertEqual(
                {label: current.next_calls for label, current in iterators.items()},
                baseline_advances,
            )
            self.assertEqual(iterators["tab-a"].close_calls, 0)
            self.assertEqual(iterators["tab-b"].close_calls, 0)
            self.assertEqual(pin_exits, [])
            self.assertFalse(stop_events["tab-a"].is_set())
            self.assertFalse(stop_events["tab-b"].is_set())

            tab_a_page = assert_response(tab_a_client.post(
                page_path, json=continuation(tab_a, tab_a_instance),
            ), 200)
            self.assertEqual(tab_a_page["page"], 1)
            self.assertEqual(iterators["tab-a"].next_calls, 65)
            self.assertEqual(iterators["tab-b"].next_calls, 33)

            cancelled_a = assert_response(tab_a_client.post(cancel_path, json={
                "session": tab_a["session"],
                "preview_instance": tab_a_instance,
            }), 200)
            self.assertEqual(cancelled_a, {"cancelled": True})
            self.assertTrue(stop_events["tab-a"].is_set())
            self.assertFalse(stop_events["tab-b"].is_set())
            self.assertEqual(iterators["tab-a"].close_calls, 1)
            self.assertEqual(iterators["tab-b"].close_calls, 0)
            self.assertEqual(pin_exits.count("tab-a"), 1)
            self.assertEqual(pin_exits.count("tab-b"), 0)
            self.assertEqual(registry.total_count, 1)

            tab_b_page = assert_response(tab_b_client.post(
                page_path, json=continuation(tab_b, tab_b_instance),
            ), 200)
            self.assertEqual(tab_b_page["page"], 1)
            self.assertEqual(iterators["tab-b"].next_calls, 65)
            self.assertEqual(iterators["tab-a"].next_calls, 65)
            cancelled_b = assert_response(tab_b_client.post(cancel_path, json={
                "session": tab_b["session"],
                "preview_instance": tab_b_instance,
            }), 200)
            self.assertEqual(cancelled_b, {"cancelled": True})
            self.assertTrue(stop_events["tab-b"].is_set())
            self.assertEqual(iterators["tab-b"].close_calls, 1)
            self.assertEqual(pin_exits.count("tab-b"), 1)
            self.assertEqual(registry.total_count, 0)

        self.assertCountEqual(pin_enters, ["tab-a", "tab-b"])
        self.assertCountEqual(pin_exits, pin_enters)
        self.assertEqual(Path(self.store.path).read_bytes(), before_config)
        self.assertEqual(Path(self.device_store.path).read_bytes(), before_devices)
        self.assertEqual(sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        ), before_media)
        self.assertEqual(self.controller.method_calls, [])
        registry.close()
        self.assertEqual(registry.total_count, 0)
        self.assertFalse(registry.reaper_alive)

    def test_two_paired_devices_have_independent_preview_sessions(self):
        from contextlib import contextmanager
        import multiprocessing

        start_path = "/api/edit-preview/clock_lava"
        page_path = "/api/edit-preview/page"
        cancel_path = "/api/edit-preview/cancel"
        preview_instance = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

        device_a = self.client
        device_b = self.app.test_client()
        self.pairing.verify.return_value = True
        device_a.post(
            "/pair", data={"pin": "123456", "device_name": "Independent A"},
        )
        device_b.post(
            "/pair", data={"pin": "123456", "device_name": "Independent B"},
        )
        with device_a.session_transaction() as session_a:
            device_a_id = session_a["device_id"]
        with device_b.session_transaction() as session_b:
            device_b_id = session_b["device_id"]
        self.assertNotEqual(device_a_id, device_b_id)

        self.controller.reset_mock()
        before_config = Path(self.store.path).read_bytes()
        before_devices = Path(self.device_store.path).read_bytes()
        before_media = sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        )
        before_children = {child.pid for child in multiprocessing.active_children()}
        before_threads = {
            (thread.ident, thread.name) for thread in threading.enumerate()
        }
        before_fds = len(list(Path("/proc/self/fd").iterdir()))
        iterators = {}
        stop_events = {}
        pin_enters = []
        pin_exits = []

        class CountingIterator:
            def __init__(self, label):
                self.label = label
                self.next_calls = 0
                self.close_calls = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.next_calls += 1
                return {
                    "png": f"frame-{self.next_calls}",
                    "delay_ms": self.next_calls,
                }

            def close(self):
                self.close_calls += 1
                if self.close_calls > 1:
                    raise AssertionError(f"{self.label} iterator closed more than once")

        @contextmanager
        def pinned(candidate, media_root, *, allow_missing=()):
            label = candidate["weather"]["location"]
            pin_enters.append(label)
            try:
                yield {}
            finally:
                pin_exits.append(label)

        def prepare(_provider, module_name, candidate, stop_event):
            label = candidate["weather"]["location"]
            stop_events[label] = stop_event
            return {"label": label}

        def iterator(module_name, candidate, selected_palette, pinned_media, prepared,
                     stop_event):
            label = prepared["label"]
            current = CountingIterator(label)
            iterators[label] = current
            return current

        def payload(label, generation):
            candidate = copy.deepcopy(self.store.get())
            candidate.pop("revision")
            candidate["weather"]["location"] = label
            return {
                "config": candidate,
                "generation": generation,
                "preview_instance": preview_instance,
            }

        def assert_response(response, status):
            self.assertEqual(response.status_code, status, response.get_data(as_text=True))
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertNotIn("ETag", response.headers)
            body = response.get_json()
            self.assertIsInstance(body, dict)
            wire = response.get_data(as_text=True)
            self.assertNotIn("revision", body)
            self.assertNotIn("device_id", wire)
            self.assertNotIn(device_a_id, wire)
            self.assertNotIn(device_b_id, wire)
            return body

        def start(client, label, generation):
            body = assert_response(
                client.post(start_path, json=payload(label, generation)), 200,
            )
            self.assertEqual(body["generation"], generation)
            self.assertEqual(body["page"], 0)
            self.assertEqual(len(body["frames"]), 32)
            self.assertFalse(body["complete"])
            self.assertRegex(body["session"], r"^[0-9a-f]{32}$")
            self.assertRegex(body["next_token"], r"^[0-9a-f]{64}$")
            return body

        def continuation(page):
            return {
                "session": page["session"],
                "preview_instance": preview_instance,
                "token": page["next_token"],
            }

        registry = None
        with patch("webapp.pin_candidate_media", pinned), patch(
            "webapp._prepare_edit_preview_provider", prepare, create=True,
        ), patch("webapp._edit_preview_iterator", iterator, create=True):
            first_a = start(device_a, "device-a-generation-7", 7)
            first_b = start(device_b, "device-b-generation-7", 7)
            registry = self.app.extensions["edit_preview_registry"]

            self.assertNotEqual(first_a["session"], first_b["session"])
            self.assertNotEqual(first_a["next_token"], first_b["next_token"])
            self.assertEqual(registry.total_count, 2)
            self.assertEqual(registry.count_for_device(device_a_id), 1)
            self.assertEqual(registry.count_for_device(device_b_id), 1)
            instance_a = registry.lookup(
                first_a["session"], (device_a_id, preview_instance),
            )
            instance_b = registry.lookup(
                first_b["session"], (device_b_id, preview_instance),
            )
            self.assertIsNot(instance_a, instance_b)
            self.assertEqual(instance_a.generation, 7)
            self.assertEqual(instance_b.generation, 7)

            advances_before_cross_access = {
                label: current.next_calls for label, current in iterators.items()
            }
            for response in (
                device_b.post(page_path, json=continuation(first_a)),
                device_a.post(page_path, json=continuation(first_b)),
                device_b.post(cancel_path, json={
                    "session": first_a["session"],
                    "preview_instance": preview_instance,
                }),
                device_a.post(cancel_path, json={
                    "session": first_b["session"],
                    "preview_instance": preview_instance,
                }),
            ):
                self.assertEqual(
                    assert_response(response, 409),
                    {"error": "Preview session is no longer active."},
                )
            self.assertEqual(
                {label: current.next_calls for label, current in iterators.items()},
                advances_before_cross_access,
            )
            self.assertEqual(pin_exits, [])
            self.assertEqual(iterators["device-a-generation-7"].close_calls, 0)
            self.assertEqual(iterators["device-b-generation-7"].close_calls, 0)
            self.assertFalse(stop_events["device-a-generation-7"].is_set())
            self.assertFalse(stop_events["device-b-generation-7"].is_set())

            page_a = assert_response(
                device_a.post(page_path, json=continuation(first_a)), 200,
            )
            self.assertEqual(page_a["generation"], 7)
            self.assertEqual(page_a["page"], 1)
            self.assertEqual(iterators["device-a-generation-7"].next_calls, 65)
            self.assertEqual(iterators["device-b-generation-7"].next_calls, 33)

            newer_a = start(device_a, "device-a-generation-8", 8)
            self.assertTrue(stop_events["device-a-generation-7"].is_set())
            self.assertFalse(stop_events["device-a-generation-8"].is_set())
            self.assertFalse(stop_events["device-b-generation-7"].is_set())
            self.assertEqual(iterators["device-a-generation-7"].close_calls, 1)
            self.assertEqual(pin_exits.count("device-a-generation-7"), 1)
            self.assertEqual(iterators["device-b-generation-7"].close_calls, 0)
            self.assertEqual(pin_exits.count("device-b-generation-7"), 0)
            self.assertEqual(iterators["device-b-generation-7"].next_calls, 33)
            self.assertEqual(registry.total_count, 2)
            self.assertEqual(registry.count_for_device(device_a_id), 1)
            self.assertEqual(registry.count_for_device(device_b_id), 1)
            self.assertEqual(
                registry.lookup(
                    newer_a["session"], (device_a_id, preview_instance),
                ).generation,
                8,
            )
            self.assertIs(
                registry.lookup(
                    first_b["session"], (device_b_id, preview_instance),
                ),
                instance_b,
            )
            self.assertEqual(
                assert_response(
                    device_a.post(page_path, json=continuation(page_a)), 409,
                ),
                {"error": "Preview session is no longer active."},
            )

            page_newer_a = assert_response(
                device_a.post(page_path, json=continuation(newer_a)), 200,
            )
            self.assertEqual((page_newer_a["generation"], page_newer_a["page"]), (8, 1))
            self.assertEqual(iterators["device-a-generation-8"].next_calls, 65)
            self.assertEqual(iterators["device-b-generation-7"].next_calls, 33)

            page_b = assert_response(
                device_b.post(page_path, json=continuation(first_b)), 200,
            )
            self.assertEqual((page_b["generation"], page_b["page"]), (7, 1))
            self.assertEqual(iterators["device-b-generation-7"].next_calls, 65)
            self.assertEqual(iterators["device-a-generation-8"].next_calls, 65)

            self.assertEqual(
                assert_response(device_a.post(cancel_path, json={
                    "session": newer_a["session"],
                    "preview_instance": preview_instance,
                }), 200),
                {"cancelled": True},
            )
            self.assertTrue(stop_events["device-a-generation-8"].is_set())
            self.assertFalse(stop_events["device-b-generation-7"].is_set())
            self.assertEqual(iterators["device-a-generation-8"].close_calls, 1)
            self.assertEqual(iterators["device-b-generation-7"].close_calls, 0)
            self.assertEqual(pin_exits.count("device-a-generation-8"), 1)
            self.assertEqual(pin_exits.count("device-b-generation-7"), 0)
            self.assertEqual(registry.total_count, 1)

            self.assertEqual(
                assert_response(device_b.post(cancel_path, json={
                    "session": first_b["session"],
                    "preview_instance": preview_instance,
                }), 200),
                {"cancelled": True},
            )
            self.assertTrue(stop_events["device-b-generation-7"].is_set())
            self.assertEqual(iterators["device-b-generation-7"].close_calls, 1)
            self.assertEqual(pin_exits.count("device-b-generation-7"), 1)
            self.assertEqual(registry.total_count, 0)

        self.assertCountEqual(
            pin_enters,
            ["device-a-generation-7", "device-b-generation-7", "device-a-generation-8"],
        )
        self.assertCountEqual(pin_exits, pin_enters)
        self.assertTrue(all(current.close_calls == 1 for current in iterators.values()))
        self.assertEqual(Path(self.store.path).read_bytes(), before_config)
        self.assertEqual(Path(self.device_store.path).read_bytes(), before_devices)
        self.assertEqual(sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        ), before_media)
        self.assertEqual(self.controller.method_calls, [])
        registry.close()
        self.assertEqual(registry.total_count, 0)
        self.assertFalse(registry.reaper_alive)
        self.assertEqual(
            {child.pid for child in multiprocessing.active_children()}, before_children,
        )
        self.assertEqual(
            {(thread.ident, thread.name) for thread in threading.enumerate()},
            before_threads,
        )
        self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), before_fds)

    def test_two_paired_devices_cannot_cancel_or_page_each_other(self):
        from contextlib import contextmanager
        import multiprocessing

        start_path = "/api/edit-preview/clock_lava"
        page_path = "/api/edit-preview/page"
        cancel_path = "/api/edit-preview/cancel"
        preview_instance = "dddddddddddddddddddddddddddddddd"

        device_a = self.client
        device_b = self.app.test_client()
        self.pairing.verify.return_value = True
        device_a.post(
            "/pair", data={"pin": "123456", "device_name": "Device A"},
        )
        device_b.post(
            "/pair", data={"pin": "123456", "device_name": "Device B"},
        )
        with device_a.session_transaction() as device_a_session:
            device_a_id = device_a_session["device_id"]
        with device_b.session_transaction() as device_b_session:
            device_b_id = device_b_session["device_id"]
        self.assertNotEqual(device_a_id, device_b_id)

        self.controller.reset_mock()
        before_config = Path(self.store.path).read_bytes()
        before_devices = Path(self.device_store.path).read_bytes()
        before_media = sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        )
        before_children = {child.pid for child in multiprocessing.active_children()}
        before_threads = {
            (thread.ident, thread.name) for thread in threading.enumerate()
        }
        before_fds = len(list(Path("/proc/self/fd").iterdir()))
        iterators = {}
        stop_events = {}
        pin_enters = []
        pin_exits = []

        class CountingIterator:
            def __init__(self, label):
                self.label = label
                self.next_calls = 0
                self.close_calls = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.next_calls += 1
                return {
                    "png": f"{self.label}-frame-{self.next_calls}",
                    "delay_ms": self.next_calls,
                }

            def close(self):
                self.close_calls += 1
                if self.close_calls > 1:
                    raise AssertionError(f"{self.label} iterator closed more than once")

        @contextmanager
        def pinned(candidate, media_root, *, allow_missing=()):
            label = candidate["weather"]["location"]
            pin_enters.append(label)
            try:
                yield {}
            finally:
                pin_exits.append(label)

        def prepare(_provider, module_name, candidate, stop_event):
            label = candidate["weather"]["location"]
            stop_events[label] = stop_event
            return {"label": label}

        def iterator(module_name, candidate, selected_palette, pinned_media, prepared,
                     stop_event):
            label = prepared["label"]
            current = CountingIterator(label)
            iterators[label] = current
            return current

        def payload(label):
            candidate = copy.deepcopy(self.store.get())
            candidate.pop("revision")
            candidate["weather"]["location"] = label
            return {
                "config": candidate,
                "generation": 1,
                "preview_instance": preview_instance,
            }

        def assert_response(response, status):
            self.assertEqual(response.status_code, status, response.get_data(as_text=True))
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertNotIn("ETag", response.headers)
            body = response.get_json()
            self.assertIsInstance(body, dict)
            serialized = json.dumps(body)
            self.assertNotIn("revision", body)
            self.assertNotIn("device_id", serialized)
            self.assertNotIn(device_a_id, serialized)
            self.assertNotIn(device_b_id, serialized)
            return body

        def continuation(page):
            return {
                "session": page["session"],
                "preview_instance": preview_instance,
                "token": page["next_token"],
            }

        registry = None
        with patch("webapp.pin_candidate_media", pinned), patch(
            "webapp._prepare_edit_preview_provider", prepare, create=True,
        ), patch("webapp._edit_preview_iterator", iterator, create=True):
            page_a = assert_response(
                device_a.post(start_path, json=payload("device-a")), 200,
            )
            page_b = assert_response(
                device_b.post(start_path, json=payload("device-b")), 200,
            )
            registry = self.app.extensions["edit_preview_registry"]
            self.assertEqual(registry.total_count, 2)
            self.assertEqual(
                registry.count_for_device((device_a_id, preview_instance)), 1,
            )
            self.assertEqual(
                registry.count_for_device((device_b_id, preview_instance)), 1,
            )
            self.assertNotEqual(page_a["session"], page_b["session"])
            self.assertNotEqual(page_a["next_token"], page_b["next_token"])
            self.assertEqual(iterators["device-a"].next_calls, 33)
            self.assertEqual(iterators["device-b"].next_calls, 33)

            advances_before_cross_access = {
                label: current.next_calls for label, current in iterators.items()
            }
            for response in (
                device_b.post(page_path, json=continuation(page_a)),
                device_a.post(page_path, json=continuation(page_b)),
                device_b.post(cancel_path, json={
                    "session": page_a["session"],
                    "preview_instance": preview_instance,
                }),
                device_a.post(cancel_path, json={
                    "session": page_b["session"],
                    "preview_instance": preview_instance,
                }),
            ):
                self.assertEqual(
                    assert_response(response, 409),
                    {"error": "Preview session is no longer active."},
                )
            self.assertEqual(
                {label: current.next_calls for label, current in iterators.items()},
                advances_before_cross_access,
            )
            self.assertEqual(pin_exits, [])
            self.assertEqual(iterators["device-a"].close_calls, 0)
            self.assertEqual(iterators["device-b"].close_calls, 0)
            self.assertFalse(stop_events["device-a"].is_set())
            self.assertFalse(stop_events["device-b"].is_set())

            second_a = assert_response(
                device_a.post(page_path, json=continuation(page_a)), 200,
            )
            self.assertEqual(second_a["page"], 1)
            self.assertEqual(iterators["device-a"].next_calls, 65)
            self.assertEqual(iterators["device-b"].next_calls, 33)
            self.assertEqual(
                assert_response(device_a.post(cancel_path, json={
                    "session": page_a["session"],
                    "preview_instance": preview_instance,
                }), 200),
                {"cancelled": True},
            )
            self.assertTrue(stop_events["device-a"].is_set())
            self.assertFalse(stop_events["device-b"].is_set())
            self.assertEqual(iterators["device-a"].close_calls, 1)
            self.assertEqual(iterators["device-b"].close_calls, 0)
            self.assertEqual(pin_exits.count("device-a"), 1)
            self.assertEqual(pin_exits.count("device-b"), 0)
            self.assertEqual(registry.total_count, 1)

            second_b = assert_response(
                device_b.post(page_path, json=continuation(page_b)), 200,
            )
            self.assertEqual(second_b["page"], 1)
            self.assertEqual(iterators["device-a"].next_calls, 65)
            self.assertEqual(iterators["device-b"].next_calls, 65)
            self.assertEqual(
                assert_response(device_b.post(cancel_path, json={
                    "session": page_b["session"],
                    "preview_instance": preview_instance,
                }), 200),
                {"cancelled": True},
            )
            self.assertTrue(stop_events["device-b"].is_set())
            self.assertEqual(iterators["device-b"].close_calls, 1)
            self.assertEqual(pin_exits.count("device-b"), 1)
            self.assertEqual(registry.total_count, 0)

        self.assertCountEqual(pin_enters, ["device-a", "device-b"])
        self.assertCountEqual(pin_exits, pin_enters)
        self.assertEqual(Path(self.store.path).read_bytes(), before_config)
        self.assertEqual(Path(self.device_store.path).read_bytes(), before_devices)
        self.assertEqual(sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        ), before_media)
        self.assertEqual(self.controller.method_calls, [])
        registry.close()
        self.assertEqual(registry.total_count, 0)
        self.assertFalse(registry.reaper_alive)
        self.assertEqual(
            {child.pid for child in multiprocessing.active_children()}, before_children,
        )
        self.assertEqual(
            {(thread.ident, thread.name) for thread in threading.enumerate()},
            before_threads,
        )
        self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), before_fds)

    def test_two_devices_and_two_tabs_are_isolated_and_same_instance_generation_supersedes(self):
        from contextlib import contextmanager

        start_path = "/api/edit-preview/clock_lava"
        page_path = "/api/edit-preview/page"
        cancel_path = "/api/edit-preview/cancel"
        tab_one = "11111111111111111111111111111111"
        tab_two = "22222222222222222222222222222222"
        device_two_tab = "33333333333333333333333333333333"

        device_one = self.client
        device_two = self.app.test_client()
        self.pairing.verify.return_value = True
        device_one.post(
            "/pair", data={"pin": "123456", "device_name": "First device"},
        )
        device_two.post(
            "/pair", data={"pin": "123456", "device_name": "Second device"},
        )
        with device_one.session_transaction() as device_one_session:
            device_one_id = device_one_session["device_id"]
        with device_two.session_transaction() as device_two_session:
            device_two_id = device_two_session["device_id"]
        self.assertNotEqual(device_one_id, device_two_id)

        self.controller.reset_mock()
        before_config = Path(self.store.path).read_bytes()
        before_devices = Path(self.device_store.path).read_bytes()
        before_media = sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        )
        iterators = {}
        stop_events = {}
        pin_enters = []
        pin_exits = []

        class CountingIterator:
            def __init__(self, label):
                self.label = label
                self.next_calls = 0
                self.close_calls = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.next_calls += 1
                if self.next_calls > 40:
                    raise StopIteration
                return {
                    "png": f"{self.label}-frame-{self.next_calls}",
                    "delay_ms": self.next_calls,
                }

            def close(self):
                self.close_calls += 1
                if self.close_calls > 1:
                    raise AssertionError(f"{self.label} iterator closed more than once")

        @contextmanager
        def pinned(candidate, media_root, *, allow_missing=()):
            label = candidate["weather"]["location"]
            pin_enters.append(label)
            try:
                yield {}
            finally:
                pin_exits.append(label)

        def prepare(_provider, module_name, candidate, stop_event):
            label = candidate["weather"]["location"]
            stop_events[label] = stop_event
            return {"label": label, "stop": stop_event}

        def iterator(module_name, candidate, selected_palette, pinned_media, prepared, stop_event):
            label = prepared["label"]
            self.assertIs(prepared["stop"], stop_event)
            current = CountingIterator(label)
            iterators[label] = current
            return current

        def payload(label, generation, preview_instance):
            candidate = copy.deepcopy(self.store.get())
            candidate.pop("revision")
            candidate["weather"]["location"] = label
            return {
                "config": candidate,
                "generation": generation,
                "preview_instance": preview_instance,
            }

        def assert_response(response, status):
            self.assertEqual(response.status_code, status, response.get_data(as_text=True))
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertNotIn("ETag", response.headers)
            body = response.get_json()
            self.assertIsInstance(body, dict)
            serialized = json.dumps(body)
            self.assertNotIn("device_id", serialized)
            self.assertNotIn(device_one_id, serialized)
            self.assertNotIn(device_two_id, serialized)
            return body

        def start(client, label, generation, preview_instance):
            body = assert_response(client.post(
                start_path, json=payload(label, generation, preview_instance),
            ), 200)
            self.assertEqual(body["generation"], generation)
            self.assertEqual(body["page"], 0)
            self.assertEqual(len(body["frames"]), 32)
            self.assertFalse(body["complete"])
            self.assertRegex(body["next_token"], r"^[0-9a-f]{64}$")
            return body

        def continuation(body, preview_instance):
            return {
                "session": body["session"],
                "preview_instance": preview_instance,
                "token": body["next_token"],
            }

        registry = None
        with patch("webapp.pin_candidate_media", pinned), patch(
            "webapp._prepare_edit_preview_provider", prepare, create=True,
        ), patch("webapp._edit_preview_iterator", iterator, create=True), patch(
            "edit_preview.PreviewSessionCapacity.MAX_ACTIVE", 4,
        ):
            first_tab_old = start(device_one, "first-tab-old", 7, tab_one)
            second_tab = start(device_one, "second-tab", 3, tab_two)
            other_device = start(device_two, "other-device", 7, device_two_tab)
            registry = self.app.extensions["edit_preview_registry"]

            self.assertEqual(registry.total_count, 3)
            self.assertEqual(registry.count_for_device(device_one_id), 2)
            self.assertEqual(registry.count_for_device(device_two_id), 1)
            baseline_advances = {
                label: current.next_calls for label, current in iterators.items()
            }

            assert_response(device_one.post(page_path, json={
                **continuation(first_tab_old, tab_one),
                "preview_instance": tab_two,
            }), 409)
            assert_response(device_two.post(
                page_path, json=continuation(first_tab_old, tab_one),
            ), 409)
            assert_response(device_one.post(
                cancel_path,
                json={
                    "session": other_device["session"],
                    "preview_instance": device_two_tab,
                },
            ), 409)
            self.assertEqual(
                {label: current.next_calls for label, current in iterators.items()},
                baseline_advances,
            )

            first_tab_new = start(device_one, "first-tab-new", 8, tab_one)
            self.assertTrue(stop_events["first-tab-old"].is_set())
            self.assertFalse(stop_events["first-tab-new"].is_set())
            self.assertFalse(stop_events["second-tab"].is_set())
            self.assertFalse(stop_events["other-device"].is_set())
            self.assertEqual(iterators["first-tab-old"].close_calls, 1)
            self.assertEqual(pin_exits.count("first-tab-old"), 1)
            self.assertEqual(registry.total_count, 3)
            self.assertEqual(iterators["second-tab"].next_calls, baseline_advances["second-tab"])
            self.assertEqual(iterators["other-device"].next_calls, baseline_advances["other-device"])
            self.assertEqual(iterators["second-tab"].close_calls, 0)
            self.assertEqual(iterators["other-device"].close_calls, 0)

            assert_response(device_one.post(
                page_path, json=continuation(first_tab_old, tab_one),
            ), 409)
            assert_response(device_one.post(cancel_path, json={
                "session": first_tab_old["session"], "preview_instance": tab_one,
            }), 409)

            constructed = set(iterators)
            advances = sum(current.next_calls for current in iterators.values())
            for generation in (8, 7):
                with self.subTest(rejected_generation=generation):
                    assert_response(device_one.post(
                        start_path,
                        json=payload(f"rejected-{generation}", generation, tab_one),
                    ), 400)
                    self.assertEqual(set(iterators), constructed)
                    self.assertEqual(
                        sum(current.next_calls for current in iterators.values()), advances,
                    )

            second_page = assert_response(device_one.post(
                page_path, json=continuation(second_tab, tab_two),
            ), 200)
            other_page = assert_response(device_two.post(
                page_path, json=continuation(other_device, device_two_tab),
            ), 200)
            self.assertTrue(second_page["complete"])
            self.assertTrue(other_page["complete"])
            self.assertEqual(iterators["second-tab"].next_calls, 41)
            self.assertEqual(iterators["other-device"].next_calls, 41)
            self.assertEqual(iterators["second-tab"].close_calls, 0)
            self.assertEqual(iterators["other-device"].close_calls, 0)
            self.assertEqual(pin_exits.count("second-tab"), 1)
            self.assertEqual(pin_exits.count("other-device"), 1)

            cancelled = assert_response(device_one.post(cancel_path, json={
                "session": first_tab_new["session"], "preview_instance": tab_one,
            }), 200)
            self.assertEqual(cancelled, {"cancelled": True})
            self.assertEqual(iterators["first-tab-new"].close_calls, 1)
            self.assertEqual(pin_exits.count("first-tab-new"), 1)
            self.assertEqual(registry.total_count, 0)

        self.assertCountEqual(
            pin_enters,
            [
                "first-tab-old", "second-tab", "other-device", "first-tab-new",
            ],
        )
        self.assertCountEqual(pin_exits, pin_enters)
        self.assertEqual(Path(self.store.path).read_bytes(), before_config)
        self.assertEqual(Path(self.device_store.path).read_bytes(), before_devices)
        self.assertEqual(sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        ), before_media)
        self.assertEqual(self.controller.method_calls, [])
        registry.close()
        self.assertFalse(registry.reaper_alive)

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

    def test_capacity_and_provider_failures_are_bounded_and_sanitized(self):
        from contextlib import contextmanager

        from edit_preview import PreviewInstanceRegistry, PreviewSessionCapacity

        start_path = "/api/edit-preview/clock_lava"
        page_path = "/api/edit-preview/page"
        cancel_path = "/api/edit-preview/cancel"
        private = f"{self.temp.name}/provider?credential=https://secret.invalid/token"

        device_a = self.client
        device_b = self.app.test_client()
        self.pairing.verify.return_value = True
        device_a.post("/pair", data={"pin": "123456", "device_name": "Capacity A"})
        device_b.post("/pair", data={"pin": "123456", "device_name": "Capacity B"})
        with device_a.session_transaction() as state_a:
            identity_a = dict(state_a)
            device_a_id = state_a["device_id"]
        with device_b.session_transaction() as state_b:
            identity_b = dict(state_b)
            device_b_id = state_b["device_id"]

        def tab_client(identity):
            client = self.app.test_client()
            with client.session_transaction() as tab:
                tab.update(identity)
            return client

        tabs_a = [tab_client(identity_a) for _ in range(6)]
        tabs_b = [tab_client(identity_b) for _ in range(6)]
        candidate = copy.deepcopy(self.store.get())
        candidate.pop("revision")
        candidate["weather"]["location"] = private

        def payload(instance, generation=1):
            return {
                "config": copy.deepcopy(candidate),
                "generation": generation,
                "preview_instance": instance,
            }

        forbidden = (
            private, self.temp.name, "secret.invalid", "credential", "https://",
            device_a_id, device_b_id, "frames", "next_token", "session", "revision",
            "etag", "token",
        )

        def assert_error(response, status, message):
            self.assertEqual(response.status_code, status, response.get_data(as_text=True))
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertNotIn("ETag", response.headers)
            self.assertEqual(response.get_json(), {"error": message})
            wire = response.get_data(as_text=True).lower()
            for value in forbidden:
                self.assertNotIn(value.lower(), wire)
            if status == 429:
                self.assertEqual(response.headers.get("Retry-After"), "2")

        pin_enters = []
        pin_exits = []
        provider_calls = []
        renderers = []

        class EndlessIterator:
            def __init__(self, label):
                self.label = label
                self.next_calls = 0
                self.close_calls = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.next_calls += 1
                return {"png": f"{self.label}-{self.next_calls}", "delay_ms": 1}

            def close(self):
                self.close_calls += 1
                if self.close_calls > 1:
                    raise AssertionError("renderer closed more than once")

        @contextmanager
        def pinned(candidate_value, media_root, *, allow_missing=()):
            label = candidate_value["weather"]["location"]
            pin_enters.append(label)
            try:
                yield {}
            finally:
                pin_exits.append(label)

        def prepare(_provider, module_name, candidate_value, stop_event):
            provider_calls.append(candidate_value["weather"]["location"])
            return None

        def renderer(module_name, candidate_value, selected_palette, pinned_media,
                     prepared, stop_event):
            current = EndlessIterator(candidate_value["weather"]["location"])
            renderers.append(current)
            return current

        self.controller.reset_mock()
        before_config = Path(self.store.path).read_bytes()
        before_devices = Path(self.device_store.path).read_bytes()
        before_media = sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        )
        registry = None
        sessions = []
        try:
            with patch("webapp.pin_candidate_media", pinned), patch(
                "webapp._prepare_edit_preview_provider", prepare,
            ), patch("webapp._edit_preview_iterator", renderer), patch(
                "edit_preview.PreviewSessionCapacity.MAX_ACTIVE", 8,
            ):
                for index in range(4):
                    response = tabs_a[index].post(
                        start_path, json=payload(f"{index + 1:032x}"),
                    )
                    self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                    sessions.append((tabs_a[index], f"{index + 1:032x}", response.get_json()))
                registry = self.app.extensions["edit_preview_registry"]
                self.assertEqual(registry.total_count, 4)
                self.assertEqual(registry.count_for_device(device_a_id), 4)

                before_rejected = (
                    len(pin_enters), len(provider_calls), len(renderers),
                    [item.next_calls for item in renderers], registry.total_count,
                )
                assert_error(
                    tabs_a[4].post(start_path, json=payload("5" * 32)), 429,
                    "Preview capacity is temporarily exhausted.",
                )
                self.assertEqual(
                    (
                        len(pin_enters), len(provider_calls), len(renderers),
                        [item.next_calls for item in renderers], registry.total_count,
                    ),
                    before_rejected,
                )
                self.assertTrue(all(not item[2]["complete"] for item in sessions))

                for index in range(4):
                    instance = f"{index + 9:032x}"
                    response = tabs_b[index].post(start_path, json=payload(instance))
                    self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                    sessions.append((tabs_b[index], instance, response.get_json()))
                self.assertEqual(registry.total_count, 8)
                self.assertEqual(registry.count_for_device(device_a_id), 4)
                self.assertEqual(registry.count_for_device(device_b_id), 4)
                before_rejected = (len(pin_enters), len(provider_calls), len(renderers))
                assert_error(
                    tabs_b[4].post(start_path, json=payload("e" * 32)), 429,
                    "Preview capacity is temporarily exhausted.",
                )
                self.assertEqual(
                    (len(pin_enters), len(provider_calls), len(renderers)), before_rejected,
                )
                self.assertEqual(registry.total_count, 8)

                released_client, released_instance, released = sessions.pop(0)
                cancelled = released_client.post(cancel_path, json={
                    "session": released["session"],
                    "preview_instance": released_instance,
                })
                self.assertEqual(cancelled.status_code, 200, cancelled.get_data(as_text=True))
                self.assertEqual(registry.total_count, 7)
                retry_instance = "f" * 32
                retry = tabs_a[5].post(
                    start_path, json=payload(retry_instance, generation=2),
                )
                self.assertEqual(retry.status_code, 200, retry.get_data(as_text=True))
                sessions.append((tabs_a[5], retry_instance, retry.get_json()))
                self.assertEqual(registry.total_count, 8)

                survivor_client, survivor_instance, survivor = sessions[0]
                page = survivor_client.post(page_path, json={
                    "session": survivor["session"],
                    "preview_instance": survivor_instance,
                    "token": survivor["next_token"],
                })
                self.assertEqual(page.status_code, 200, page.get_data(as_text=True))
                self.assertEqual(page.get_json()["page"], 1)

                for client, instance, body in list(sessions):
                    response = client.post(cancel_path, json={
                        "session": body["session"], "preview_instance": instance,
                    })
                    self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                sessions.clear()
                self.assertEqual(registry.total_count, 0)
                self.assertEqual(registry.counts_by_device, {})
                self.assertEqual(registry._capacity.active_count, 0)
        finally:
            for client, instance, body in list(sessions):
                client.post(cancel_path, json={
                    "session": body["session"], "preview_instance": instance,
                })

        class Clock:
            now = 100.0

            def monotonic(self):
                return self.now

            def wait(self, condition, timeout):
                self.now += timeout
                return False

        clock = Clock()
        capacity = registry._capacity
        capacity._monotonic = clock.monotonic
        capacity._wait = clock.wait
        pin_enters.clear()
        pin_exits.clear()
        provider_calls.clear()
        renderers.clear()
        active = []
        try:
            with patch("webapp.pin_candidate_media", pinned), patch(
                "webapp._prepare_edit_preview_provider", prepare,
            ), patch("webapp._edit_preview_iterator", renderer):
                for index in range(2):
                    instance = f"{index + 20:032x}"
                    response = tabs_a[index].post(start_path, json=payload(instance))
                    self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                    active.append((tabs_a[index], instance, response.get_json()))
                before_third = (len(pin_enters), len(provider_calls), len(renderers))
                assert_error(
                    tabs_b[0].post(start_path, json=payload("d" * 32)), 429,
                    "Preview capacity is temporarily exhausted.",
                )
                self.assertEqual(
                    (len(pin_enters), len(provider_calls), len(renderers)), before_third,
                )
                self.assertEqual(capacity.active_count, 2)
                self.assertEqual(capacity.waiting_count, 0)
                self.assertEqual(registry.total_count, 2)

                client, instance, body = active.pop(0)
                self.assertEqual(client.post(cancel_path, json={
                    "session": body["session"], "preview_instance": instance,
                }).status_code, 200)
                recovered_instance = "c" * 32
                recovered = tabs_b[0].post(
                    start_path, json=payload(recovered_instance, generation=2),
                )
                self.assertEqual(recovered.status_code, 200, recovered.get_data(as_text=True))
                active.append((tabs_b[0], recovered_instance, recovered.get_json()))
                for client, instance, body in active:
                    self.assertEqual(client.post(cancel_path, json={
                        "session": body["session"], "preview_instance": instance,
                    }).status_code, 200)
                self.assertEqual((capacity.active_count, capacity.waiting_count), (0, 0))
                self.assertEqual(registry.total_count, 0)
        finally:
            for client, instance, body in active:
                client.post(cancel_path, json={
                    "session": body["session"], "preview_instance": instance,
                })

        from preview_provider import (
            PreviewProviderCapacityError, PreviewProviderTimeout,
            PreviewProviderUnavailable,
        )

        failure_cases = (
            (PreviewProviderTimeout(private), "provider-timeout"),
            (PreviewProviderUnavailable(private), "provider-unavailable"),
            (webapp.EditPreviewDecodeError(private), "decode"),
            (webapp.EditPreviewRenderError(private), "render"),
        )
        pin_enters.clear()
        pin_exits.clear()
        provider_calls.clear()
        renderers.clear()
        try:
            for index, (failure, phase) in enumerate(failure_cases):
                instance = f"{index + 30:032x}"
                if phase.startswith("provider"):
                    provider_patch = patch(
                        "webapp._prepare_edit_preview_provider", side_effect=failure,
                    )
                    renderer_patch = patch("webapp._edit_preview_iterator", renderer)
                else:
                    provider_patch = patch(
                        "webapp._prepare_edit_preview_provider", prepare,
                    )
                    renderer_patch = patch(
                        "webapp._edit_preview_iterator", side_effect=failure,
                    )
                with patch("webapp.pin_candidate_media", pinned), provider_patch, renderer_patch:
                    response = tabs_a[index].post(start_path, json=payload(instance))
                assert_error(response, 503, "Preview is temporarily unavailable.")
                self.assertEqual(registry.total_count, 0)
                self.assertEqual(capacity.active_count, 0)
                self.assertEqual(capacity.waiting_count, 0)
                self.assertEqual(len(pin_enters), len(pin_exits))

            with patch("webapp.pin_candidate_media", pinned), patch(
                "webapp._prepare_edit_preview_provider",
                side_effect=PreviewProviderCapacityError(private),
            ), patch("webapp._edit_preview_iterator", renderer):
                response = tabs_a[5].post(start_path, json=payload("b" * 32))
            assert_error(
                response, 429, "Preview capacity is temporarily exhausted.",
            )
            self.assertEqual(registry.total_count, 0)
            self.assertEqual(capacity.active_count, 0)
        finally:
            registry.close()
            self.app.extensions.pop("edit_preview_registry", None)

        self.assertEqual(Path(self.store.path).read_bytes(), before_config)
        self.assertEqual(Path(self.device_store.path).read_bytes(), before_devices)
        self.assertEqual(sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        ), before_media)
        self.assertEqual(self.controller.method_calls, [])
        self.assertEqual(pin_enters, pin_exits)

    def test_preview_service_restart_cleans_registry_workers_and_descriptors(self):
        """Restart keeps the broad lifecycle contract and closes pinned decode FDs."""
        from contextlib import contextmanager
        import multiprocessing
        from edit_preview import PreviewInstanceRegistry, PreviewSessionCapacity

        # Keep the exact restart node tied to the already accepted real-route
        # singleton, concurrent replacement, blocked-provider, stale-session,
        # sanitized-error, teardown, and immutable-state contract.
        self.test_preview_services_restart_after_close_and_failed_init()

        before_children = {child.pid for child in multiprocessing.active_children()}
        before_threads = {(thread.ident, thread.name) for thread in threading.enumerate()}
        before_fds = len(list(Path("/proc/self/fd").iterdir()))
        before_config = Path(self.store.path).read_bytes()
        before_devices = Path(self.device_store.path).read_bytes()
        before_media = sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        )
        self.controller.reset_mock()

        registries = []
        providers = []
        pins = []
        iterators = []
        decoder_blocked = threading.Event()
        response = {}
        failures = []

        class TrackedRegistry(PreviewInstanceRegistry):
            def __init__(self):
                capacity = PreviewSessionCapacity()
                super().__init__(capacity=capacity)
                self.capacity = capacity
                self.close_calls = 0
                registries.append(self)

            def close(self):
                was_closed = self.is_closed
                super().close()
                if not was_closed:
                    self.close_calls += 1

        class TrackedProvider:
            MAX_WORKERS = 2

            def __init__(self):
                self.is_closed = False
                self.close_calls = 0
                self.prepare_calls = 0
                providers.append(self)

            @property
            def active_child_count(self):
                return 0

            @property
            def active_worker_count(self):
                return 0

            @property
            def available_worker_slots(self):
                return self.MAX_WORKERS

            def prepare(self, provider, *, settings, credentials, cancel_event,
                        request_deadline):
                if self.is_closed:
                    raise RuntimeError("closed provider attempted to serve")
                self.prepare_calls += 1
                return (
                    ("feels_like", 19), ("high", 23), ("humidity", 50),
                    ("location", settings["weather"]["location"]), ("low", 12),
                    ("status", "Clear sky"), ("temperature", 20),
                )

            def shutdown(self):
                if not self.is_closed:
                    self.is_closed = True
                    self.close_calls += 1

        class BlockedDecoderIterator:
            def __init__(self, stop_event):
                self.stop_event = stop_event
                self.close_calls = 0
                self.read_fd, self.write_fd = os.pipe()
                self.fd_close_calls = {self.read_fd: 0, self.write_fd: 0}
                iterators.append(self)

            def __iter__(self):
                return self

            def __next__(self):
                decoder_blocked.set()
                while not self.stop_event.wait(0.005):
                    pass
                raise StopIteration

            def close(self):
                self.close_calls += 1
                if self.close_calls > 1:
                    raise AssertionError("decoder iterator closed more than once")
                for descriptor in (self.read_fd, self.write_fd):
                    os.close(descriptor)
                    self.fd_close_calls[descriptor] += 1

        @contextmanager
        def pinned(candidate, media_root, *, allow_missing=()):
            read_fd, write_fd = os.pipe()
            record = {
                "fds": (read_fd, write_fd),
                "fd_close_calls": {read_fd: 0, write_fd: 0},
                "close_calls": 0,
            }
            pins.append(record)
            try:
                yield {"weather": "pinned"}
            finally:
                record["close_calls"] += 1
                if record["close_calls"] > 1:
                    raise AssertionError("pinned descriptors closed more than once")
                for descriptor in record["fds"]:
                    os.close(descriptor)
                    record["fd_close_calls"][descriptor] += 1

        def iterator(module_name, candidate, selected_palette, pinned_media, prepared,
                     stop_event):
            resource = BlockedDecoderIterator(stop_event)

            def owned_decoder():
                try:
                    yield from resource
                finally:
                    resource.close()

            return owned_decoder()

        candidate = copy.deepcopy(self.store.get())
        candidate.pop("revision")
        candidate["weather"]["location"] = "blocked-decoder-private"
        payload = {
            "config": candidate,
            "generation": 100,
            "preview_instance": "abababababababababababababababab",
        }
        with self.client.session_transaction() as paired:
            browser_session = dict(paired)

        def blocked_start():
            try:
                client = self.app.test_client()
                with client.session_transaction() as session:
                    session.update(browser_session)
                response["value"] = client.post(
                    "/api/edit-preview/weather", json=payload,
                )
            except BaseException as error:
                failures.append(error)

        factory_patch = patch.multiple(
            "webapp", PreviewInstanceRegistry=TrackedRegistry,
            PreviewProvider=TrackedProvider, pin_candidate_media=pinned,
            _edit_preview_iterator=iterator,
        )
        blocked_thread = threading.Thread(
            target=blocked_start, name="preview-restart-blocked-decoder",
        )
        try:
            with factory_patch:
                blocked_thread.start()
                self.assertTrue(decoder_blocked.wait(1))
                registry = self.app.extensions["edit_preview_registry"]
                provider = self.app.extensions["edit_preview_provider"]
                self.assertIs(self.app.extensions["get_edit_preview_services"]()[0], registry)
                self.assertIs(self.app.extensions["get_edit_preview_services"]()[1], provider)
                with self.app.app_context():
                    pass
                self.assertFalse(registry.is_closed)
                self.assertFalse(provider.is_closed)

                closed_at = time.monotonic()
                self.app.extensions["close_edit_preview_services"]()
                self.assertLess(time.monotonic() - closed_at, 0.25)
                blocked_thread.join(1)
                self.assertFalse(blocked_thread.is_alive())
                self.assertEqual(failures, [])
                self.assertEqual(response["value"].status_code, 200)
                self.assertEqual(response["value"].headers.get("Cache-Control"), "no-store")
                self.assertNotIn("blocked-decoder-private", response["value"].get_data(as_text=True))
                self.assertTrue(registry.is_closed)
                self.assertTrue(provider.is_closed)
                self.assertEqual((registry.close_calls, provider.close_calls), (1, 1))
                self.assertEqual(registry.reaper_join_count, 1)
                self.assertFalse(registry.reaper_alive)
                self.assertEqual((registry.total_count, registry.expired_count), (0, 0))
                self.assertEqual(
                    (registry.capacity.active_count, registry.capacity.waiting_count),
                    (0, 0),
                )
                self.assertEqual((provider.active_worker_count, provider.active_child_count), (0, 0))
                with self.assertRaisesRegex(RuntimeError, "closed"):
                    registry.register(("closed", "closed"), lambda: iter(()))
                with self.assertRaisesRegex(RuntimeError, "closed provider"):
                    provider.prepare(
                        None, settings={"weather": {"location": "closed"}},
                        credentials=None, cancel_event=threading.Event(),
                        request_deadline=1,
                    )
        finally:
            blocked_thread.join(1)
            for registry in registries:
                registry.close()
            for provider in providers:
                provider.shutdown()

        self.assertEqual((len(registries), len(providers)), (1, 1))
        self.assertEqual((len(pins), len(iterators)), (1, 1))
        for resource in iterators:
            self.assertEqual(resource.close_calls, 1)
            self.assertEqual(set(resource.fd_close_calls.values()), {1})
            for descriptor in resource.fd_close_calls:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)
        for resource in pins:
            self.assertEqual(resource["close_calls"], 1)
            self.assertEqual(set(resource["fd_close_calls"].values()), {1})
            for descriptor in resource["fd_close_calls"]:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)
        self.assertEqual(Path(self.store.path).read_bytes(), before_config)
        self.assertEqual(Path(self.device_store.path).read_bytes(), before_devices)
        self.assertEqual(sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        ), before_media)
        self.assertEqual(self.controller.method_calls, [])
        self.assertEqual(
            {child.pid for child in multiprocessing.active_children()}, before_children,
        )
        self.assertEqual(
            {(thread.ident, thread.name) for thread in threading.enumerate()}, before_threads,
        )
        self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), before_fds)

    def test_preview_service_close_is_idempotent_during_active_page(self):
        """Concurrent service close suppresses a claimed page and cleans once."""
        from contextlib import contextmanager
        import multiprocessing

        from edit_preview import PreviewInstanceRegistry, PreviewSessionCapacity

        self.pair_browser()
        with self.client.session_transaction() as paired:
            browser_session = dict(paired)
            device_id = paired["device_id"]
        self.controller.reset_mock()

        before_children = {child.pid for child in multiprocessing.active_children()}
        before_threads = {(thread.ident, thread.name) for thread in threading.enumerate()}
        before_fds = len(list(Path("/proc/self/fd").iterdir()))
        before_config = Path(self.store.path).read_bytes()
        before_devices = Path(self.device_store.path).read_bytes()
        before_media = sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        )

        registries = []
        providers = []
        pins = []
        decoders = []
        service_callbacks = []
        service_accessor = [None]
        decoder_blocked = threading.Event()

        class TrackedRegistry(PreviewInstanceRegistry):
            def __init__(self):
                capacity = PreviewSessionCapacity()
                super().__init__(capacity=capacity)
                self.capacity = capacity
                self.close_calls = 0
                self._close_tracking_lock = threading.Lock()
                self._close_tracked = False
                registries.append(self)

            def close(self):
                with self._close_tracking_lock:
                    first_close = not self._close_tracked
                    if first_close:
                        self._close_tracked = True
                        # Re-enter the app-owned service accessor from a lifecycle
                        # callback. It must not run under the service-pair lock.
                        callback_result = []
                        callback = threading.Thread(
                            target=lambda: callback_result.append(
                                service_accessor[0]()
                            ),
                            name="preview-service-lock-callback",
                        )
                        callback.start()
                        callback.join(0.25)
                        if callback.is_alive():
                            raise AssertionError("registry callback ran under service lock")
                        service_callbacks.append(callback_result[0])
                super().close()
                if first_close:
                    self.close_calls += 1

        class TrackedProvider:
            MAX_WORKERS = 2

            def __init__(self):
                self._lock = threading.Lock()
                self.is_closed = False
                self.close_calls = 0
                self.prepare_calls = 0
                self.shutdown_seen = threading.Event()
                self.child_close_calls = 0
                self.child_reap_calls = 0
                self.child_join_calls = 0
                self.child_fds = os.pipe()
                providers.append(self)

            @property
            def active_child_count(self):
                return 0

            @property
            def active_worker_count(self):
                return 0

            @property
            def available_worker_slots(self):
                return self.MAX_WORKERS

            def prepare(self, provider, *, settings, credentials, cancel_event,
                        request_deadline):
                with self._lock:
                    if self.is_closed:
                        raise RuntimeError("closed provider attempted to serve")
                    self.prepare_calls += 1
                return (
                    ("feels_like", 19), ("high", 23), ("humidity", 50),
                    ("location", settings["weather"]["location"]), ("low", 12),
                    ("status", "Clear sky"), ("temperature", 20),
                )

            def shutdown(self):
                with self._lock:
                    if self.is_closed:
                        return
                    self.is_closed = True
                    self.close_calls += 1
                    self.shutdown_seen.set()
                    for descriptor in self.child_fds:
                        os.close(descriptor)
                        self.child_close_calls += 1
                    self.child_reap_calls += 1
                    self.child_join_calls += 1

        class BlockedDecoder:
            def __init__(self, stop_event):
                self.stop_event = stop_event
                self.next_calls = 0
                self.close_calls = 0
                self.concurrent_close_calls = 0
                self.active = False
                self.stop_seen = threading.Event()
                self.fds = os.pipe()
                self.fd_close_calls = {descriptor: 0 for descriptor in self.fds}
                decoders.append(self)

            def __iter__(self):
                return self

            def __next__(self):
                self.next_calls += 1
                self.active = True
                try:
                    if self.next_calls == 34:
                        decoder_blocked.set()
                        if not self.stop_event.wait(1):
                            raise AssertionError("service close did not stop active decoder")
                        self.stop_seen.set()
                    return {
                        "png": f"private-frame-{self.next_calls}",
                        "delay_ms": self.next_calls,
                    }
                finally:
                    self.active = False

            def close(self):
                if self.active:
                    self.concurrent_close_calls += 1
                    raise AssertionError("decoder close overlapped iteration")
                self.close_calls += 1
                if self.close_calls > 1:
                    raise AssertionError("decoder closed more than once")
                for descriptor in self.fds:
                    os.close(descriptor)
                    self.fd_close_calls[descriptor] += 1

        @contextmanager
        def pinned(candidate, media_root, *, allow_missing=()):
            fds = os.pipe()
            record = {
                "fds": fds,
                "close_calls": 0,
                "fd_close_calls": {descriptor: 0 for descriptor in fds},
            }
            pins.append(record)
            try:
                yield {"draft": "pinned"}
            finally:
                record["close_calls"] += 1
                if record["close_calls"] > 1:
                    raise AssertionError("pin stack closed more than once")
                for descriptor in fds:
                    os.close(descriptor)
                    record["fd_close_calls"][descriptor] += 1

        def iterator(module_name, candidate, selected_palette, pinned_media, prepared,
                     stop_event):
            decoder = BlockedDecoder(stop_event)

            def owned_decoder():
                try:
                    while True:
                        yield next(decoder)
                except StopIteration:
                    return
                finally:
                    decoder.close()

            return owned_decoder()

        def client_for_thread():
            client = self.app.test_client()
            with client.session_transaction() as paired:
                paired.update(browser_session)
            return client

        def assert_response(response, status):
            self.assertEqual(response.status_code, status, response.get_data(as_text=True))
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertNotIn("ETag", response.headers)
            return response.get_json()

        candidate = copy.deepcopy(self.store.get())
        candidate.pop("revision")
        candidate["weather"]["location"] = "private-active-page-location"
        preview_instance = "cd" * 16
        payload = {
            "config": candidate,
            "generation": 200,
            "preview_instance": preview_instance,
        }
        page_result = {}
        page_failures = []
        close_results = []
        close_failures = []
        close_barrier = threading.Barrier(3)

        def active_page(first):
            try:
                page_result["response"] = client_for_thread().post(
                    "/api/edit-preview/page",
                    json={
                        "session": first["session"],
                        "preview_instance": preview_instance,
                        "token": first["next_token"],
                    },
                )
            except BaseException as error:
                page_failures.append(error)

        def close_service(caller):
            try:
                close_barrier.wait()
                started = time.monotonic()
                self.app.extensions["close_edit_preview_services"]()
                close_results.append((caller, time.monotonic() - started))
            except BaseException as error:
                close_failures.append(error)

        factory_patch = patch.multiple(
            "webapp", PreviewInstanceRegistry=TrackedRegistry,
            PreviewProvider=TrackedProvider, pin_candidate_media=pinned,
            _edit_preview_iterator=iterator,
        )
        page_thread = None
        close_threads = []
        try:
            with factory_patch:
                first = assert_response(
                    self.client.post("/api/edit-preview/weather", json=payload), 200,
                )
                self.assertEqual(len(first["frames"]), 32)
                self.assertFalse(first["complete"])
                registry = self.app.extensions["edit_preview_registry"]
                provider = self.app.extensions["edit_preview_provider"]
                service_accessor[0] = self.app.extensions["get_edit_preview_services"]
                identity = (device_id, preview_instance)
                instance = registry.lookup(first["session"], identity)
                preview_session = instance.admit()

                page_thread = threading.Thread(
                    target=active_page, args=(first,),
                    name="preview-service-close-active-page",
                )
                page_thread.start()
                self.assertTrue(decoder_blocked.wait(1), "page did not claim and block")

                replay = assert_response(client_for_thread().post(
                    "/api/edit-preview/page",
                    json={
                        "session": first["session"],
                        "preview_instance": preview_instance,
                        "token": first["next_token"],
                    },
                ), 409)
                self.assertEqual(replay, {"error": "Preview continuation was already used."})

                close_threads = [
                    threading.Thread(
                        target=close_service, args=(index,),
                        name=f"preview-service-close-{index}",
                    )
                    for index in range(2)
                ]
                for thread in close_threads:
                    thread.start()
                close_barrier.wait()
                page_thread.join(0.5)
                for thread in close_threads:
                    thread.join(0.5)

                self.assertFalse(page_thread.is_alive())
                self.assertTrue(all(not thread.is_alive() for thread in close_threads))
                self.assertEqual(page_failures, [])
                self.assertEqual(close_failures, [])
                self.assertEqual({caller for caller, _elapsed in close_results}, {0, 1})
                self.assertTrue(all(elapsed < 0.25 for _caller, elapsed in close_results))
                shutdown = assert_response(page_result["response"], 503)
                self.assertEqual(shutdown, {"error": "Preview is temporarily unavailable."})
                for forbidden in ("frames", "next_token", "session", "generation"):
                    self.assertNotIn(forbidden, shutdown)

                self.assertTrue(decoders[0].stop_seen.is_set())
                self.assertTrue(provider.shutdown_seen.is_set())
                self.assertEqual(decoders[0].concurrent_close_calls, 0)
                self.assertEqual(decoders[0].close_calls, 1)
                self.assertEqual(pins[0]["close_calls"], 1)
                self.assertEqual(preview_session.state, "CLOSED")
                self.assertEqual(preview_session.retained_frame_count, 0)
                self.assertEqual(registry.lifecycle_state, "CLOSED")
                self.assertEqual(registry.close_calls, 1)
                self.assertEqual(registry.reaper_join_count, 1)
                self.assertFalse(registry.reaper_alive)
                self.assertEqual(provider.close_calls, 1)
                self.assertEqual(
                    (provider.child_close_calls, provider.child_reap_calls,
                     provider.child_join_calls),
                    (2, 1, 1),
                )
                self.assertEqual(len(service_callbacks), 1)
                callback_registry, callback_provider = service_callbacks[0]
                self.assertIs(
                    self.app.extensions["edit_preview_registry"], callback_registry,
                )
                self.assertIs(
                    self.app.extensions["edit_preview_provider"], callback_provider,
                )

                self.app.extensions["close_edit_preview_services"]()
                self.app.extensions["close_edit_preview_services"]()
                self.assertEqual((registry.close_calls, provider.close_calls), (1, 1))
                self.assertEqual(registry.reaper_join_count, 1)
                self.assertEqual(decoders[0].close_calls, 1)
                self.assertEqual(pins[0]["close_calls"], 1)

                stale = assert_response(client_for_thread().post(
                    "/api/edit-preview/page",
                    json={
                        "session": first["session"],
                        "preview_instance": preview_instance,
                        "token": first["next_token"],
                    },
                ), 409)
                self.assertEqual(stale, {"error": "Preview session is no longer active."})
                replacement_registry = self.app.extensions["edit_preview_registry"]
                replacement_provider = self.app.extensions["edit_preview_provider"]
                self.assertIsNot(replacement_registry, registry)
                self.assertIsNot(replacement_provider, provider)

                replacement = assert_response(client_for_thread().post(
                    "/api/edit-preview/weather",
                    json={**payload, "generation": 201},
                ), 200)
                self.assertEqual(replacement["generation"], 201)
                assert_response(client_for_thread().post(
                    "/api/edit-preview/cancel",
                    json={
                        "session": replacement["session"],
                        "preview_instance": preview_instance,
                    },
                ), 200)
                self.app.extensions["close_edit_preview_services"]()
        finally:
            if page_thread is not None:
                page_thread.join(1)
            for thread in close_threads:
                thread.join(1)
            for registry in registries:
                registry.close()
            for provider in providers:
                provider.shutdown()

        self.assertEqual((len(registries), len(providers)), (2, 2))
        self.assertTrue(all(registry.lifecycle_state == "CLOSED" for registry in registries))
        self.assertTrue(all(registry.close_calls == 1 for registry in registries))
        self.assertTrue(all(registry.reaper_join_count == 1 for registry in registries))
        self.assertTrue(all(registry.total_count == 0 for registry in registries))
        self.assertTrue(all(registry.expired_count == 0 for registry in registries))
        self.assertTrue(all(registry.capacity.active_count == 0 for registry in registries))
        self.assertTrue(all(registry.capacity.waiting_count == 0 for registry in registries))
        self.assertTrue(all(provider.close_calls == 1 for provider in providers))
        self.assertTrue(all(provider.active_worker_count == 0 for provider in providers))
        self.assertTrue(all(provider.active_child_count == 0 for provider in providers))
        self.assertTrue(all(decoder.close_calls == 1 for decoder in decoders))
        self.assertTrue(all(set(decoder.fd_close_calls.values()) == {1} for decoder in decoders))
        self.assertTrue(all(pin["close_calls"] == 1 for pin in pins))
        self.assertTrue(all(set(pin["fd_close_calls"].values()) == {1} for pin in pins))
        self.assertEqual(Path(self.store.path).read_bytes(), before_config)
        self.assertEqual(Path(self.device_store.path).read_bytes(), before_devices)
        self.assertEqual(sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        ), before_media)
        self.assertEqual(self.controller.method_calls, [])
        self.assertEqual(
            {child.pid for child in multiprocessing.active_children()}, before_children,
        )
        self.assertEqual(
            {(thread.ident, thread.name) for thread in threading.enumerate()}, before_threads,
        )
        self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), before_fds)

    def test_preview_instance_registry_is_bounded_and_expires(self):
        from contextlib import contextmanager
        import multiprocessing

        from edit_preview import PreviewInstanceRegistry, PreviewSessionCapacity

        class Clock:
            def __init__(self):
                self.now = 1000.0
                self.capacity = None

            def monotonic(self):
                return self.now

            def advance(self, seconds):
                capacity = self.capacity
                with capacity._condition:
                    self.now += seconds
                capacity.request_reap()

            def timeout_wait(self, condition, timeout):
                self.now += timeout
                return False

        class DeterministicCapacity(PreviewSessionCapacity):
            def __init__(self, **kwargs):
                self._reap_requested = threading.Semaphore(0)
                self.reap_completed = threading.Event()
                super().__init__(**kwargs)

            def request_reap(self):
                self.reap_completed.clear()
                self._reap_requested.release()

            def run_expiry_reaper(self):
                while True:
                    self._reap_requested.acquire()
                    with self._condition:
                        if self._closed:
                            return
                    self.last_reaped = self.reap_expired()
                    self.reap_completed.set()

            def close(self):
                super().close()
                self._reap_requested.release()

        class CountingIterator:
            def __init__(self, label, stop_event, *, block_on=None):
                self.label = label
                self.stop_event = stop_event
                self.block_on = block_on
                self.next_calls = 0
                self.close_calls = 0
                self.blocked = threading.Event()
                self.closed = threading.Event()

            def __iter__(self):
                return self

            def __next__(self):
                self.next_calls += 1
                if self.next_calls == self.block_on:
                    self.blocked.set()
                    if not self.stop_event.wait(1):
                        raise AssertionError("active iterator was not stopped by expiry")
                    return {"png": f"{self.label}-{self.next_calls}", "delay_ms": 1}
                if self.stop_event.is_set():
                    raise StopIteration
                return {"png": f"{self.label}-{self.next_calls}", "delay_ms": 1}

            def close(self):
                self.close_calls += 1
                if self.close_calls > 1:
                    raise AssertionError(f"{self.label} iterator closed more than once")
                self.closed.set()

        class LightweightProvider:
            MAX_WORKERS = 2

            def __init__(self):
                self.is_closed = False
                self.close_calls = 0

            @property
            def active_child_count(self):
                return 0

            @property
            def active_worker_count(self):
                return 0

            @property
            def available_worker_slots(self):
                return self.MAX_WORKERS

            def shutdown(self):
                if not self.is_closed:
                    self.is_closed = True
                    self.close_calls += 1

        start_path = "/api/edit-preview/clock_lava"
        page_path = "/api/edit-preview/page"
        cancel_path = "/api/edit-preview/cancel"
        private = f"{self.temp.name}/private?credential=https://secret.invalid/key"

        devices = [self.client, self.app.test_client(), self.app.test_client()]
        self.pairing.verify.return_value = True
        identities = []
        device_ids = []
        for index, client in enumerate(devices):
            client.post(
                "/pair", data={"pin": "123456", "device_name": f"Bounded {index}"},
            )
            with client.session_transaction() as paired:
                identities.append(dict(paired))
                device_ids.append(paired["device_id"])
        self.assertEqual(len(set(device_ids)), 3)

        def tab(device_index):
            client = self.app.test_client()
            with client.session_transaction() as paired:
                paired.update(identities[device_index])
            return client

        tabs = [[tab(device) for _ in range(6)] for device in range(3)]
        candidate = copy.deepcopy(self.store.get())
        candidate.pop("revision")

        def payload(label, instance, generation=1):
            value = copy.deepcopy(candidate)
            value["weather"]["location"] = f"{private}-{label}"
            return {
                "config": value,
                "generation": generation,
                "preview_instance": instance,
            }

        def assert_error(response, status, message):
            self.assertEqual(response.status_code, status, response.get_data(as_text=True))
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertNotIn("ETag", response.headers)
            self.assertEqual(response.get_json(), {"error": message})
            wire = response.get_data(as_text=True).lower()
            for forbidden in (
                private, self.temp.name, "secret.invalid", "credential", "https://",
                *device_ids, "revision", "frame", "next_token",
            ):
                self.assertNotIn(forbidden.lower(), wire)
            if status == 429:
                self.assertEqual(response.headers.get("Retry-After"), "2")

        def assert_success(response):
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertNotIn("ETag", response.headers)
            return response.get_json()

        pin_enters = []
        pin_exits = []
        provider_calls = []
        renderer_calls = []
        iterators = []
        block_next = False

        @contextmanager
        def pinned(candidate_value, media_root, *, allow_missing=()):
            label = candidate_value["weather"]["location"]
            pin_enters.append(label)
            try:
                yield {}
            finally:
                pin_exits.append(label)

        def prepare(provider_value, module_name, candidate_value, stop_event):
            provider_calls.append(candidate_value["weather"]["location"])
            return None

        def renderer(module_name, candidate_value, selected_palette, pinned_media,
                     prepared, stop_event):
            nonlocal block_next
            label = candidate_value["weather"]["location"]
            renderer_calls.append(label)
            current = CountingIterator(
                label, stop_event, block_on=34 if block_next else None,
            )
            block_next = False
            iterators.append(current)
            return current

        self.controller.reset_mock()
        before_config = Path(self.store.path).read_bytes()
        before_devices = Path(self.device_store.path).read_bytes()
        before_media = sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        )
        before_children = {child.pid for child in multiprocessing.active_children()}
        before_threads = {(thread.ident, thread.name) for thread in threading.enumerate()}
        before_fds = len(list(Path("/proc/self/fd").iterdir()))

        clock = Clock()
        capacity = DeterministicCapacity(
            monotonic=clock.monotonic, wait=clock.timeout_wait,
        )
        clock.capacity = capacity
        capacity.IDLE_TTL = 1.0
        registry = PreviewInstanceRegistry(capacity=capacity)
        registry.EXPIRED_TOMBSTONE_TTL = 100.0
        provider = LightweightProvider()
        registry_factory = Mock(return_value=registry)
        provider_factory = Mock(return_value=provider)
        sessions = []

        def await_reaper(event, message):
            for _attempt in range(20):
                if event.wait(0.05):
                    return
                with capacity._condition:
                    capacity._condition.notify_all()
            self.fail(message)

        reaper_names = [
            thread.name for thread in threading.enumerate()
            if thread.name == "edit-preview-expiry-reaper"
        ]
        baseline_reapers = sum(
            name == "edit-preview-expiry-reaper" for _ident, name in before_threads
        )
        self.assertEqual(len(reaper_names), baseline_reapers + 1)
        self.assertTrue(registry.reaper_alive)
        self.assertTrue(registry.reaper_is_daemon)

        try:
            with patch("webapp.PreviewInstanceRegistry", registry_factory), patch(
                "webapp.PreviewProvider", provider_factory,
            ), patch("webapp.pin_candidate_media", pinned), patch(
                "webapp._prepare_edit_preview_provider", prepare, create=True,
            ), patch("webapp._edit_preview_iterator", renderer, create=True):
                # Retained instances are bounded to four per paired device and
                # eight process-wide, even when each tab has a distinct identity.
                capacity.MAX_ACTIVE = 8
                for device in range(2):
                    for index in range(4):
                        instance = f"{device * 8 + index + 1:032x}"
                        body = assert_success(tabs[device][index].post(
                            start_path,
                            json=payload(f"retained-{device}-{index}", instance),
                        ))
                        sessions.append((tabs[device][index], instance, body))
                self.assertEqual(registry.total_count, 8)
                self.assertEqual(registry.count_for_device(device_ids[0]), 4)
                self.assertEqual(registry.count_for_device(device_ids[1]), 4)
                self.assertEqual(capacity.active_count, 8)

                constructions = (
                    len(pin_enters), len(provider_calls), len(renderer_calls), len(iterators),
                )
                assert_error(
                    tabs[0][4].post(
                        start_path, json=payload("per-device-rejected", "a" * 32),
                    ),
                    429, "Preview capacity is temporarily exhausted.",
                )
                assert_error(
                    tabs[2][0].post(
                        start_path, json=payload("process-rejected", "b" * 32),
                    ),
                    429, "Preview capacity is temporarily exhausted.",
                )
                self.assertEqual(
                    (len(pin_enters), len(provider_calls), len(renderer_calls), len(iterators)),
                    constructions,
                )
                self.assertEqual(registry.total_count, 8)

                for client, instance, body in sessions:
                    self.assertEqual(assert_success(client.post(cancel_path, json={
                        "session": body["session"], "preview_instance": instance,
                    })), {"cancelled": True})
                sessions.clear()
                self.assertEqual((registry.total_count, capacity.active_count), (0, 0))

                # Only two renderer sessions are active. A rejected third start
                # reaches no pin, provider, or renderer construction.
                capacity.MAX_ACTIVE = 2
                for index in range(2):
                    instance = f"{index + 40:032x}"
                    body = assert_success(tabs[index][0].post(
                        start_path, json=payload(f"active-{index}", instance, 2),
                    ))
                    sessions.append((tabs[index][0], instance, body))
                constructions = (
                    len(pin_enters), len(provider_calls), len(renderer_calls), len(iterators),
                )
                capacity.ACQUIRE_TIMEOUT = 0
                assert_error(
                    tabs[2][0].post(
                        start_path, json=payload("active-rejected", "c" * 32, 2),
                    ),
                    429, "Preview capacity is temporarily exhausted.",
                )
                capacity.ACQUIRE_TIMEOUT = 2.0
                self.assertEqual(
                    (len(pin_enters), len(provider_calls), len(renderer_calls), len(iterators)),
                    constructions,
                )
                self.assertEqual((capacity.active_count, registry.total_count), (2, 2))

                # The daemon expires retained idle sessions without another request,
                # releases both resource layers once, and leaves identity tombstones.
                expired_idle = list(sessions)
                sessions.clear()
                clock.advance(capacity.IDLE_TTL + 0.001)
                for iterator in iterators[-2:]:
                    await_reaper(iterator.closed, "idle expiry did not close")
                    self.assertEqual(iterator.close_calls, 1)
                self.assertEqual((capacity.active_count, registry.total_count), (0, 0))
                self.assertEqual(registry.expired_count, 2)
                self.assertEqual(pin_exits[-2:], pin_enters[-2:])

                owner, expired_instance, expired = expired_idle[0]
                expired_page = {
                    "session": expired["session"],
                    "preview_instance": expired_instance,
                    "token": expired["next_token"],
                }
                expired_cancel = {
                    "session": expired["session"],
                    "preview_instance": expired_instance,
                }
                assert_error(
                    owner.post(page_path, json=expired_page), 410,
                    "Preview session expired.",
                )
                assert_error(
                    owner.post(cancel_path, json=expired_cancel), 410,
                    "Preview session expired.",
                )
                assert_error(
                    tabs[2][0].post(page_path, json=expired_page), 409,
                    "Preview session is no longer active.",
                )

                retry_instance = "d" * 32
                retry = assert_success(tabs[2][0].post(
                    start_path, json=payload("retry-after-expiry", retry_instance, 3),
                ))
                self.assertEqual(capacity.active_count, 1)
                self.assertEqual(assert_success(tabs[2][0].post(cancel_path, json={
                    "session": retry["session"], "preview_instance": retry_instance,
                })), {"cancelled": True})

                # Expiry during a page signals active work, then closes and releases
                # ownership exactly once after that page leaves the iterator.
                active_instance = "e" * 32
                block_next = True
                active = assert_success(tabs[0][0].post(
                    start_path, json=payload("expire-active-page", active_instance, 4),
                ))
                active_iterator = iterators[-1]
                active_result = {}
                active_failures = []

                def continue_active():
                    try:
                        active_result["response"] = tabs[0][0].post(page_path, json={
                            "session": active["session"],
                            "preview_instance": active_instance,
                            "token": active["next_token"],
                        })
                    except BaseException as error:
                        active_failures.append(error)

                active_thread = threading.Thread(
                    target=continue_active, name="preview-active-expiry-page",
                )
                active_thread.start()
                self.assertTrue(active_iterator.blocked.wait(1))
                with capacity._condition:
                    self.assertEqual(len(capacity._sessions), 1)
                    active_last_activity = next(iter(capacity._sessions.values()))
                clock.advance(
                    active_last_activity + capacity.IDLE_TTL + 0.001 - clock.now,
                )
                await_reaper(capacity.reap_completed, "active reaper did not run")
                self.assertEqual(capacity.last_reaped, 1)
                await_reaper(active_iterator.closed, "active expiry did not close")
                active_thread.join(1)
                self.assertFalse(active_thread.is_alive())
                self.assertEqual(active_failures, [])
                self.assertEqual(active_result["response"].status_code, 200)
                self.assertEqual(active_iterator.close_calls, 1)
                self.assertEqual((capacity.active_count, registry.total_count), (0, 0))
                assert_error(
                    tabs[0][0].post(cancel_path, json={
                        "session": active["session"],
                        "preview_instance": active_instance,
                    }), 410, "Preview session expired.",
                )

                # Expiry tombstones retain only the eight newest identities, and
                # disappear after their own TTL so old expiry degrades to generic 409.
                oldest = None
                for index in range(9):
                    instance = f"{index + 100:032x}"
                    body = assert_success(tabs[index % 3][0].post(
                        start_path, json=payload(f"tombstone-{index}", instance, index + 10),
                    ))
                    if oldest is None:
                        oldest = (tabs[index % 3][0], instance, body)
                    current_iterator = iterators[-1]
                    clock.advance(capacity.IDLE_TTL + 0.001)
                    await_reaper(current_iterator.closed, "tombstone expiry did not close")
                    self.assertEqual(current_iterator.close_calls, 1)
                self.assertEqual(registry.expired_count, 8)
                self.assertLessEqual(
                    registry.expired_count, registry.MAX_EXPIRED_TOMBSTONES,
                )
                old_client, old_instance, old_body = oldest
                assert_error(old_client.post(cancel_path, json={
                    "session": old_body["session"],
                    "preview_instance": old_instance,
                }), 409, "Preview session is no longer active.")

                newest_instance = f"{108:032x}"
                newest_body = body
                assert_error(tabs[2][0].post(cancel_path, json={
                    "session": newest_body["session"],
                    "preview_instance": newest_instance,
                }), 410, "Preview session expired.")
                clock.advance(registry.EXPIRED_TOMBSTONE_TTL + 0.001)
                assert_error(tabs[2][0].post(cancel_path, json={
                    "session": newest_body["session"],
                    "preview_instance": newest_instance,
                }), 409, "Preview session is no longer active.")
                self.assertEqual(registry.expired_count, 0)

                self.app.extensions["close_edit_preview_services"]()

            self.assertEqual((registry_factory.call_count, provider_factory.call_count), (1, 1))
            self.assertEqual(registry.reaper_join_count, 1)
            self.assertFalse(registry.reaper_alive)
            self.assertTrue(provider.is_closed)
            self.assertEqual(provider.close_calls, 1)
            self.assertEqual(provider.active_child_count, 0)
            self.assertEqual(provider.active_worker_count, 0)
            self.assertEqual(provider.available_worker_slots, provider.MAX_WORKERS)
            self.assertEqual((registry.total_count, registry.expired_count), (0, 0))
            self.assertEqual((capacity.active_count, capacity.waiting_count), (0, 0))
            self.assertEqual(pin_enters, pin_exits)
            self.assertTrue(all(iterator.close_calls == 1 for iterator in iterators))
            self.assertEqual(Path(self.store.path).read_bytes(), before_config)
            self.assertEqual(Path(self.device_store.path).read_bytes(), before_devices)
            self.assertEqual(sorted(
                (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
                for path in self.media_dir.rglob("*") if path.is_file()
            ), before_media)
            self.assertEqual(self.controller.method_calls, [])
        finally:
            registry.close()
            provider.shutdown()

        self.assertEqual(
            {child.pid for child in multiprocessing.active_children()}, before_children,
        )
        self.assertEqual(
            {(thread.ident, thread.name) for thread in threading.enumerate()}, before_threads,
        )
        self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), before_fds)

    def test_malformed_or_reused_preview_instance_and_tokens_fail_closed(self):
        from contextlib import contextmanager
        import multiprocessing

        from edit_preview import PreviewInstanceRegistry, PreviewSessionCapacity

        class Clock:
            now = 500.0

            def monotonic(self):
                return self.now

        class CountingIterator:
            def __init__(self, label):
                self.label = label
                self.next_calls = 0
                self.close_calls = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.next_calls += 1
                return {"png": f"frame-{self.label}-{self.next_calls}", "delay_ms": 1}

            def close(self):
                self.close_calls += 1
                if self.close_calls > 1:
                    raise AssertionError(f"iterator {self.label} closed more than once")

        class LightweightProvider:
            MAX_WORKERS = 2

            def __init__(self):
                self.prepare_calls = 0
                self.close_calls = 0
                self.is_closed = False

            @property
            def active_child_count(self):
                return 0

            @property
            def active_worker_count(self):
                return 0

            @property
            def available_worker_slots(self):
                return self.MAX_WORKERS

            def shutdown(self):
                if not self.is_closed:
                    self.is_closed = True
                    self.close_calls += 1

        start_path = "/api/edit-preview/clock_lava"
        page_path = "/api/edit-preview/page"
        cancel_path = "/api/edit-preview/cancel"
        owner_instance = "1" * 32
        wrong_instance = "2" * 32
        expired_instance = "3" * 32
        private_values = (
            str(Path(self.temp.name) / "private-preview.png"),
            "https://secret.invalid/preview?credential=private-token",
        )

        owner = self.client
        attacker = self.app.test_client()
        self.pairing.verify.return_value = True
        owner.post("/pair", data={"pin": "123456", "device_name": "Token owner"})
        attacker.post("/pair", data={"pin": "123456", "device_name": "Token attacker"})
        with owner.session_transaction() as owner_session:
            owner_device = owner_session["device_id"]
        with attacker.session_transaction() as attacker_session:
            attacker_device = attacker_session["device_id"]
        self.assertNotEqual(owner_device, attacker_device)

        candidate = copy.deepcopy(self.store.get())
        candidate.pop("revision")
        candidate["weather"]["location"] = private_values[1]
        start_payload = {
            "config": candidate,
            "generation": 1,
            "preview_instance": owner_instance,
        }
        self.controller.reset_mock()
        before_config = Path(self.store.path).read_bytes()
        before_devices = Path(self.device_store.path).read_bytes()
        before_media = sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        )
        before_children = {child.pid for child in multiprocessing.active_children()}
        before_threads = {(thread.ident, thread.name) for thread in threading.enumerate()}
        before_fds = len(list(Path("/proc/self/fd").iterdir()))

        clock = Clock()
        capacity = PreviewSessionCapacity(monotonic=clock.monotonic)
        registry = PreviewInstanceRegistry(capacity=capacity)
        provider = LightweightProvider()
        iterators = []
        pin_enters = []
        pin_exits = []
        provider_calls = []
        renderer_calls = []

        def assert_error(response, status, message):
            self.assertEqual(response.status_code, status, response.get_data(as_text=True))
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertNotIn("ETag", response.headers)
            self.assertEqual(response.get_json(), {"error": message})
            wire = response.get_data(as_text=True)
            for forbidden in (
                "revision", "generation", "frame", "preview_instance", "path", "url",
                owner_device, attacker_device, *private_values,
            ):
                self.assertNotIn(forbidden.lower(), wire.lower())

        def assert_success(response):
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertNotIn("ETag", response.headers)
            return response.get_json()

        @contextmanager
        def pinned(candidate_value, media_root, *, allow_missing=()):
            label = candidate_value["weather"]["location"]
            pin_enters.append(label)
            try:
                yield {}
            finally:
                pin_exits.append(label)

        def prepare(provider_value, module_name, candidate_value, stop_event):
            provider_calls.append((provider_value, module_name))
            provider_value.prepare_calls += 1
            return None

        def renderer(module_name, candidate_value, selected_palette, pinned_media,
                     prepared, stop_event):
            label = f"session-{len(iterators)}"
            renderer_calls.append((module_name, label))
            current = CountingIterator(label)
            iterators.append(current)
            return current

        malformed_instances = [
            None, True, 1, [], {}, "", "1" * 31, "1" * 33,
            "A" * 32, "g" * 32,
        ]
        malformed_start_requests = [
            {"data": "{", "content_type": "application/json"},
            {"data": "{}", "content_type": "text/plain"},
            {"data": "null", "content_type": "application/json"},
            {"json": []},
            {"json": "not-an-object"},
            {"json": {}},
            {"json": {key: value for key, value in start_payload.items()
                      if key != "preview_instance"}},
            {"json": {**start_payload, "extra": "must-fail"}},
            *({"json": {**start_payload, "preview_instance": value}}
              for value in malformed_instances),
        ]

        registry_factory = Mock(return_value=registry)
        provider_factory = Mock(return_value=provider)
        try:
            with patch("webapp.PreviewInstanceRegistry", registry_factory), patch(
                "webapp.PreviewProvider", provider_factory,
            ), patch("webapp.pin_candidate_media", pinned), patch(
                "webapp._prepare_edit_preview_provider", prepare, create=True,
            ), patch("webapp._edit_preview_iterator", renderer, create=True):
                for request_args in malformed_start_requests:
                    with self.subTest(start_request=request_args):
                        assert_error(
                            owner.post(start_path, **request_args), 400,
                            "Preview request is invalid.",
                        )
                self.assertEqual(registry_factory.call_count, 0)
                self.assertEqual(provider_factory.call_count, 0)
                self.assertEqual(capacity.active_count, 0)
                self.assertEqual(registry.total_count, 0)
                self.assertEqual(pin_enters, [])
                self.assertEqual(provider_calls, [])
                self.assertEqual(renderer_calls, [])

                first = assert_success(owner.post(start_path, json=start_payload))
                self.assertRegex(first["session"], r"^[0-9a-f]{32}$")
                self.assertRegex(first["next_token"], r"^[0-9a-f]{64}$")
                self.assertEqual((registry_factory.call_count, provider_factory.call_count), (1, 1))
                self.assertEqual((len(pin_enters), len(provider_calls), len(iterators)), (1, 1, 1))
                owner_iterator = iterators[0]
                continuation = {
                    "session": first["session"],
                    "preview_instance": owner_instance,
                    "token": first["next_token"],
                }

                malformed_session_values = [
                    None, True, 1, [], {}, "", "a" * 31, "a" * 33,
                    "A" * 32, "g" * 32,
                ]
                malformed_token_values = [
                    None, True, 1, [], {}, "", "b" * 63, "b" * 65,
                    "B" * 64, "g" * 64,
                ]
                malformed_page_requests = [
                    {"data": "{", "content_type": "application/json"},
                    {"data": json.dumps(continuation), "content_type": "text/plain"},
                    {"data": "null", "content_type": "application/json"},
                    {"json": []},
                    {"json": "not-an-object"},
                    {"json": {}},
                    *({"json": {key: value for key, value in continuation.items() if key != missing}}
                      for missing in continuation),
                    {"json": {**continuation, "extra": "must-fail"}},
                    *({"json": {**continuation, "session": value}}
                      for value in malformed_session_values),
                    *({"json": {**continuation, "preview_instance": value}}
                      for value in malformed_instances),
                    *({"json": {**continuation, "token": value}}
                      for value in malformed_token_values),
                ]
                calls_before_malformed = owner_iterator.next_calls
                for request_args in malformed_page_requests:
                    with self.subTest(page_request=request_args):
                        assert_error(
                            owner.post(page_path, **request_args), 400,
                            "Preview request is invalid.",
                        )
                self.assertEqual(owner_iterator.next_calls, calls_before_malformed)

                cancel_payload = {
                    "session": first["session"],
                    "preview_instance": owner_instance,
                }
                malformed_cancel_requests = [
                    {"data": "{", "content_type": "application/json"},
                    {"data": json.dumps(cancel_payload), "content_type": "text/plain"},
                    {"data": "null", "content_type": "application/json"},
                    {"json": []},
                    {"json": {}},
                    *({"json": {key: value for key, value in cancel_payload.items()
                                if key != missing}}
                      for missing in cancel_payload),
                    {"json": {**cancel_payload, "extra": "must-fail"}},
                    *({"json": {**cancel_payload, "session": value}}
                      for value in malformed_session_values),
                    *({"json": {**cancel_payload, "preview_instance": value}}
                      for value in malformed_instances),
                ]
                for request_args in malformed_cancel_requests:
                    with self.subTest(cancel_request=request_args):
                        assert_error(
                            owner.post(cancel_path, **request_args), 400,
                            "Preview request is invalid.",
                        )
                self.assertEqual(owner_iterator.next_calls, calls_before_malformed)
                self.assertEqual(owner_iterator.close_calls, 0)

                unknown_session = {**continuation, "session": "f" * 32}
                unknown_token = {**continuation, "token": "e" * 64}
                wrong_instance_page = {**continuation, "preview_instance": wrong_instance}
                conflict_attempts = [
                    (owner, page_path, unknown_session),
                    (owner, page_path, unknown_token),
                    (owner, page_path, wrong_instance_page),
                    (attacker, page_path, continuation),
                    (owner, cancel_path, {
                        "session": "f" * 32, "preview_instance": owner_instance,
                    }),
                    (owner, cancel_path, {
                        "session": first["session"], "preview_instance": wrong_instance,
                    }),
                    (attacker, cancel_path, cancel_payload),
                ]
                for client, path, payload in conflict_attempts:
                    with self.subTest(conflict_path=path, payload=payload):
                        message = (
                            "Preview continuation was already used."
                            if payload is unknown_token
                            else "Preview session is no longer active."
                        )
                        assert_error(client.post(path, json=payload), 409, message)
                self.assertEqual(owner_iterator.next_calls, calls_before_malformed)
                self.assertEqual(owner_iterator.close_calls, 0)

                second = assert_success(owner.post(page_path, json=continuation))
                calls_after_valid_claim = owner_iterator.next_calls
                self.assertGreater(calls_after_valid_claim, calls_before_malformed)
                assert_error(
                    owner.post(page_path, json=continuation), 409,
                    "Preview continuation was already used.",
                )
                self.assertEqual(owner_iterator.next_calls, calls_after_valid_claim)

                current = {**continuation, "token": second["next_token"]}
                third = assert_success(owner.post(page_path, json=current))
                self.assertEqual(third["page"], second["page"] + 1)
                owner_cancel = assert_success(owner.post(cancel_path, json=cancel_payload))
                self.assertEqual(owner_cancel, {"cancelled": True})
                owner_calls_after_cancel = owner_iterator.next_calls
                self.assertEqual(owner_iterator.close_calls, 1)
                assert_error(
                    owner.post(cancel_path, json=cancel_payload), 409,
                    "Preview session is no longer active.",
                )
                self.assertEqual(owner_iterator.next_calls, owner_calls_after_cancel)

                expiring = assert_success(owner.post(start_path, json={
                    **start_payload,
                    "preview_instance": expired_instance,
                }))
                expired_iterator = iterators[-1]
                expired_calls = expired_iterator.next_calls
                expired_page = {
                    "session": expiring["session"],
                    "preview_instance": expired_instance,
                    "token": expiring["next_token"],
                }
                expired_cancel = {
                    "session": expiring["session"],
                    "preview_instance": expired_instance,
                }
                clock.now += capacity.IDLE_TTL + 0.001
                self.assertEqual(capacity.reap_expired(), 1)
                self.assertEqual(expired_iterator.close_calls, 1)
                self.assertEqual(expired_iterator.next_calls, expired_calls)
                self.assertEqual(capacity.active_count, 0)
                self.assertEqual(capacity.available_slots, capacity.MAX_ACTIVE)
                self.assertEqual(registry.total_count, 0)
                self.assertEqual(registry.expired_count, 1)
                assert_error(
                    owner.post(page_path, json=expired_page), 410,
                    "Preview session expired.",
                )
                assert_error(
                    owner.post(cancel_path, json=expired_cancel), 410,
                    "Preview session expired.",
                )
                assert_error(
                    attacker.post(page_path, json=expired_page), 409,
                    "Preview session is no longer active.",
                )
                self.assertEqual(expired_iterator.next_calls, expired_calls)

                self.app.extensions["close_edit_preview_services"]()

            self.assertEqual(Path(self.store.path).read_bytes(), before_config)
            self.assertEqual(Path(self.device_store.path).read_bytes(), before_devices)
            self.assertEqual(sorted(
                (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
                for path in self.media_dir.rglob("*") if path.is_file()
            ), before_media)
            self.assertEqual(self.controller.method_calls, [])
            self.assertEqual(len(pin_enters), 2)
            self.assertEqual(pin_exits, pin_enters)
            self.assertEqual(len(provider_calls), 2)
            self.assertEqual(len(renderer_calls), 2)
            self.assertTrue(all(iterator.close_calls == 1 for iterator in iterators))
            self.assertEqual(registry.total_count, 0)
            self.assertEqual(registry.expired_count, 0)
            self.assertFalse(registry.reaper_alive)
            self.assertTrue(provider.is_closed)
            self.assertEqual(provider.close_calls, 1)
            self.assertEqual(provider.active_child_count, 0)
            self.assertEqual(provider.active_worker_count, 0)
            self.assertEqual(provider.available_worker_slots, provider.MAX_WORKERS)
        finally:
            registry.close()
            provider.shutdown()

        self.assertEqual(
            {child.pid for child in multiprocessing.active_children()}, before_children,
        )
        self.assertEqual(
            {(thread.ident, thread.name) for thread in threading.enumerate()}, before_threads,
        )
        self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), before_fds)

    def test_malformed_tokens_and_expired_sessions_have_stable_errors(self):
        from contextlib import contextmanager

        from edit_preview import PreviewInstanceRegistry, PreviewSessionCapacity

        class Clock:
            now = 100.0

            def monotonic(self):
                return self.now

        class CountingIterator:
            def __init__(self, count):
                self._remaining = iter(range(count))
                self.next_calls = 0
                self.close_calls = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.next_calls += 1
                index = next(self._remaining)
                return {"png": f"stable-frame-{index}", "delay_ms": 1}

            def close(self):
                self.close_calls += 1

        start_path = "/api/edit-preview/clock_lava"
        page_path = "/api/edit-preview/page"
        cancel_path = "/api/edit-preview/cancel"
        preview_instance = "a" * 32
        cancel_instance = "b" * 32
        expired_instance = "c" * 32
        wrong_instance = "d" * 32
        private_sentinel = f"{self.temp.name}/private?credential=https://secret.invalid/key"
        candidate = copy.deepcopy(self.store.get())
        candidate.pop("revision")
        candidate["weather"]["location"] = private_sentinel

        self.pair_browser()
        self.controller.reset_mock()
        before_config = Path(self.store.path).read_bytes()
        before_devices = Path(self.device_store.path).read_bytes()
        before_media = sorted(
            (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
            for path in self.media_dir.rglob("*") if path.is_file()
        )
        clock = Clock()
        capacity = PreviewSessionCapacity(monotonic=clock.monotonic)
        registry = PreviewInstanceRegistry(capacity=capacity)
        provider_calls = []
        iterators = []
        pin_enters = []
        pin_exits = []

        def assert_error(response, status, message):
            self.assertEqual(response.status_code, status, response.get_data(as_text=True))
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertNotIn("ETag", response.headers)
            self.assertEqual(response.get_json(), {"error": message})
            wire = response.get_data(as_text=True)
            for forbidden in (
                "revision", "device_id", private_sentinel, self.temp.name,
                "secret.invalid", "credential", "https://",
            ):
                self.assertNotIn(forbidden, wire)

        @contextmanager
        def pinned(*args, **kwargs):
            pin_enters.append(object())
            try:
                yield {}
            finally:
                pin_exits.append(object())

        def prepare(*args):
            provider_calls.append(args[0])
            return None

        def renderer(*args):
            count = (70, 100, 100)[len(iterators)]
            iterator = CountingIterator(count)
            iterators.append(iterator)
            return iterator

        start_payload = {
            "config": candidate,
            "generation": 1,
            "preview_instance": preview_instance,
        }
        bad_start_requests = [
            {"data": "not-json", "content_type": "application/json"},
            {"data": "{}", "content_type": "text/plain"},
            {"json": []},
            {"json": {}},
            {"json": {**start_payload, "extra": True}},
            {"json": {**start_payload, "preview_instance": "A" * 32}},
        ]

        try:
            with patch("webapp.PreviewInstanceRegistry", return_value=registry), patch(
                "webapp.pin_candidate_media", pinned,
            ), patch(
                "webapp._prepare_edit_preview_provider", prepare, create=True,
            ), patch("webapp._edit_preview_iterator", renderer, create=True):
                for request_args in bad_start_requests:
                    with self.subTest(start_request=request_args):
                        assert_error(
                            self.client.post(start_path, **request_args), 400,
                            "Preview request is invalid.",
                        )
                self.assertEqual(provider_calls, [])
                self.assertEqual(iterators, [])
                self.assertEqual(pin_enters, [])

                first_response = self.client.post(start_path, json=start_payload)
                self.assertEqual(first_response.status_code, 200)
                first = first_response.get_json()
                self.assertFalse(first["complete"])
                first_token = first["next_token"]
                self.assertRegex(first_token, r"^[0-9a-f]{64}$")
                self.assertEqual(len(provider_calls), 1)
                self.assertEqual(len(iterators), 1)
                valid_iterator = iterators[0]

                continuation = {
                    "session": first["session"],
                    "preview_instance": preview_instance,
                    "token": first_token,
                }
                bad_page_requests = [
                    {"data": "{", "content_type": "application/json"},
                    {"data": json.dumps(continuation), "content_type": "text/plain"},
                    {"json": []},
                    {"json": {key: value for key, value in continuation.items() if key != "token"}},
                    {"json": {**continuation, "extra": True}},
                    {"json": {**continuation, "session": "A" * 32}},
                    {"json": {**continuation, "preview_instance": "bad"}},
                    {"json": {**continuation, "token": "z" * 64}},
                ]
                calls_before_bad_pages = valid_iterator.next_calls
                for request_args in bad_page_requests:
                    with self.subTest(page_request=request_args):
                        assert_error(
                            self.client.post(page_path, **request_args), 400,
                            "Preview request is invalid.",
                        )
                self.assertEqual(valid_iterator.next_calls, calls_before_bad_pages)
                self.assertEqual(len(provider_calls), 1)
                self.assertEqual(len(iterators), 1)

                second_response = self.client.post(page_path, json=continuation)
                self.assertEqual(second_response.status_code, 200)
                second = second_response.get_json()
                self.assertFalse(second["complete"])
                calls_after_consumption = valid_iterator.next_calls
                assert_error(
                    self.client.post(page_path, json=continuation), 409,
                    "Preview continuation was already used.",
                )
                self.assertEqual(valid_iterator.next_calls, calls_after_consumption)

                current = {**continuation, "token": second["next_token"]}
                unknown = {**current, "session": "f" * 32}
                wrong_identity = {**current, "preview_instance": wrong_instance}
                for payload in (unknown, wrong_identity):
                    assert_error(
                        self.client.post(page_path, json=payload), 409,
                        "Preview session is no longer active.",
                    )
                self.assertEqual(valid_iterator.next_calls, calls_after_consumption)

                terminal_response = self.client.post(page_path, json=current)
                self.assertEqual(terminal_response.status_code, 200)
                self.assertTrue(terminal_response.get_json()["complete"])
                self.assertEqual(len(pin_exits), 1)

                cancelled = self.client.post(start_path, json={
                    **start_payload,
                    "generation": 1,
                    "preview_instance": cancel_instance,
                }).get_json()
                cancel_iterator = iterators[-1]
                ordinary_cancel = self.client.post(cancel_path, json={
                    "session": cancelled["session"],
                    "preview_instance": cancel_instance,
                })
                self.assertEqual(ordinary_cancel.status_code, 200)
                self.assertEqual(ordinary_cancel.get_json(), {"cancelled": True})
                self.assertEqual(cancel_iterator.close_calls, 1)

                expiring = self.client.post(start_path, json={
                    **start_payload,
                    "generation": 1,
                    "preview_instance": expired_instance,
                }).get_json()
                expired_iterator = iterators[-1]
                expired_token = expiring["next_token"]
                expired_calls = expired_iterator.next_calls
                self.assertEqual(capacity.active_count, 1)
                clock.now += capacity.IDLE_TTL + 0.001
                self.assertEqual(capacity.reap_expired(), 1)
                self.assertEqual(expired_iterator.close_calls, 1)
                self.assertEqual(expired_iterator.next_calls, expired_calls)
                self.assertEqual(capacity.active_count, 0)
                self.assertEqual(capacity.available_slots, capacity.MAX_ACTIVE)
                self.assertEqual(registry.total_count, 0)
                self.assertEqual(registry.expired_count, 1)
                self.assertLessEqual(
                    registry.expired_count, registry.MAX_EXPIRED_TOMBSTONES,
                )

                expired_page = {
                    "session": expiring["session"],
                    "preview_instance": expired_instance,
                    "token": expired_token,
                }
                expired_cancel = {
                    "session": expiring["session"],
                    "preview_instance": expired_instance,
                }
                assert_error(
                    self.client.post(page_path, json=expired_page), 410,
                    "Preview session expired.",
                )
                assert_error(
                    self.client.post(cancel_path, json=expired_cancel), 410,
                    "Preview session expired.",
                )
                assert_error(
                    self.client.post(cancel_path, json={
                        **expired_cancel, "preview_instance": wrong_instance,
                    }), 409, "Preview session is no longer active.",
                )
                self.assertEqual(expired_iterator.next_calls, expired_calls)
                self.assertEqual(expired_iterator.close_calls, 1)

                clock.now += registry.EXPIRED_TOMBSTONE_TTL + 0.001
                assert_error(
                    self.client.post(cancel_path, json=expired_cancel), 409,
                    "Preview session is no longer active.",
                )
                self.assertEqual(registry.expired_count, 0)

            self.assertEqual(Path(self.store.path).read_bytes(), before_config)
            self.assertEqual(Path(self.device_store.path).read_bytes(), before_devices)
            self.assertEqual(sorted(
                (path.relative_to(self.media_dir).as_posix(), path.read_bytes())
                for path in self.media_dir.rglob("*") if path.is_file()
            ), before_media)
            self.assertEqual(self.controller.method_calls, [])
            self.assertEqual(len(provider_calls), 3)
            self.assertEqual(len(iterators), 3)
            self.assertEqual(len(pin_enters), 3)
            self.assertEqual(len(pin_exits), 3)
        finally:
            registry.close()
        self.assertEqual(registry.expired_count, 0)
        self.assertFalse(registry.reaper_alive)

    def test_stale_session_generation_is_rejected(self):
        with self.client.session_transaction() as browser_session:
            browser_session["paired"] = True
            browser_session["generation"] = "old-generation"
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/pair", response.headers["Location"])

    def test_preview_service_atexit_close_joins_reaper_and_reaps_children(self):
        """The exact lifecycle node reuses the isolated WSGI process proof below."""
        getattr(
            WSGIImportTests,
            "test_wsgi_import_owns_one_registry_and_atexit_closes_it",
        )(self)


class WSGIImportTests(unittest.TestCase):
    def test_wsgi_import_owns_one_registry_and_atexit_closes_it(self):
        script = r'''
import atexit
import importlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import types
from unittest.mock import patch

root = Path(sys.argv[1])
registered = []
atexit.register = lambda callback, *args, **kwargs: registered.append(callback) or callback

# Importing the reusable factory is not a process-entrypoint side effect.
import webapp
real_create_app = webapp.create_app

class Passive:
    def __init__(self, *args, **kwargs):
        self.path = kwargs.get("path")

class FakeController(Passive):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stop_calls = 0
        self.method_calls = []

    def stop(self):
        self.stop_calls += 1

class FakeLock(Passive):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire(self):
        self.acquire_calls += 1

    def release(self):
        self.release_calls += 1

class FakePairing(Passive):
    pass

class FakeDeviceStore(Passive):
    def any_devices(self):
        return True

registries = []
providers = []

class TrackedRegistry:
    def __init__(self):
        self.is_closed = False
        self.close_calls = 0
        self.stop = threading.Event()
        self.read_fd, self.write_fd = os.pipe()
        self.reaper = threading.Thread(
            target=self.stop.wait, name="tracked-preview-reaper", daemon=True,
        )
        self.reaper.start()
        registries.append(self)

    @property
    def reaper_alive(self):
        return self.reaper.is_alive()

    def close(self):
        if self.is_closed:
            return
        self.is_closed = True
        self.close_calls += 1
        self.stop.set()
        self.reaper.join(1)
        os.close(self.read_fd)
        os.close(self.write_fd)

class TrackedProvider:
    def __init__(self):
        self.is_closed = False
        self.close_calls = 0
        self.stop = threading.Event()
        self.read_fd, self.write_fd = os.pipe()
        self.worker = threading.Thread(
            target=self.stop.wait, name="tracked-preview-worker", daemon=True,
        )
        self.worker.start()
        self.child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
        )
        providers.append(self)

    def shutdown(self):
        if self.is_closed:
            return
        self.is_closed = True
        self.close_calls += 1
        self.stop.set()
        self.worker.join(1)
        self.child.terminate()
        self.child.wait(timeout=1)
        os.close(self.read_fd)
        os.close(self.write_fd)

create_calls = []

with tempfile.TemporaryDirectory() as temp:
    temp = Path(temp)
    package = temp / "app"
    package.mkdir()
    shutil.copy2(root / "app" / "__init__.py", package / "__init__.py")
    shutil.copy2(root / "app" / "wsgi.py", package / "wsgi.py")

    def tracked_create_app(*args, **kwargs):
        create_calls.append((args, kwargs))
        return real_create_app(
            FakeController(), Passive(), FakePairing(), secret_key="test",
            session_generation="wsgi-test", media_dir=temp / "media",
            location_search=lambda query: [], device_store=FakeDeviceStore(),
            openweather_secrets=Passive(),
        )

    # Calling create_app in a test/import context must not own process cleanup.
    standalone = tracked_create_app()
    create_calls.clear()
    assert "edit_preview_registry" not in standalone.extensions
    assert standalone.extensions["close_edit_preview_services"] not in registered

    fake_modules = {}
    for name, attributes in {
        "config_store": {"ConfigStore": Passive},
        "display_controller": {"DisplayController": FakeController},
        "hardware_lock": {"DisplayOwnershipLock": FakeLock},
        "matrix_framework": {"Matrix": Passive},
        "pairing": {
            "PairedDeviceStore": FakeDeviceStore,
            "PairingManager": FakePairing,
            "initialize_pairing_display": lambda *args: None,
        },
        "secret_store": {"SecretStore": Passive},
    }.items():
        module = types.ModuleType(name)
        for attribute, value in attributes.items():
            setattr(module, attribute, value)
        fake_modules[name] = module

    with patch.dict(sys.modules, fake_modules), \
         patch.object(webapp, "create_app", tracked_create_app), \
         patch.object(webapp, "PreviewInstanceRegistry", TrackedRegistry), \
         patch.object(webapp, "PreviewProvider", TrackedProvider):
        sys.path.insert(0, str(temp))
        entry = importlib.import_module("app.wsgi")
        sys.path.insert(0, str(package))
        alias = importlib.import_module("wsgi")
        importlib.reload(entry)
        importlib.reload(alias)

        assert len(create_calls) == 1, len(create_calls)
        assert entry.app is alias.app
        assert registries == [] and providers == []
        preview_callbacks = [
            callback for callback in registered
            if callback is entry.app.extensions["close_edit_preview_services"]
        ]
        assert len(preview_callbacks) == 1, registered

        baseline_fds = len(os.listdir("/proc/self/fd"))
        baseline_threads = {thread.ident for thread in threading.enumerate()}
        baseline_children = {child.pid for child in multiprocessing.active_children()}
        registry, provider = entry.app.extensions["get_edit_preview_services"]()
        assert (len(registries), len(providers)) == (1, 1)
        assert entry.app.extensions["edit_preview_registry"] is registry
        assert entry.app.extensions["edit_preview_provider"] is provider
        assert alias.app.extensions["get_edit_preview_services"]() == (registry, provider)

        with entry.app.test_request_context("/"):
            pass
        with entry.app.app_context():
            pass
        assert not registry.is_closed and not provider.is_closed

        callback = preview_callbacks[0]
        callback()
        callback()
        assert (registry.close_calls, provider.close_calls) == (1, 1)
        assert registry.is_closed and provider.is_closed
        assert not registry.reaper_alive and not provider.worker.is_alive()
        assert provider.child.poll() is not None
        assert len(os.listdir("/proc/self/fd")) == baseline_fds
        assert {thread.ident for thread in threading.enumerate()} == baseline_threads
        assert {child.pid for child in multiprocessing.active_children()} == baseline_children
        assert entry.controller.method_calls == []

print(json.dumps({
    "create_calls": len(create_calls),
    "preview_callbacks": len(preview_callbacks),
    "registries": len(registries),
    "providers": len(providers),
    "registry_close_calls": registry.close_calls,
    "provider_close_calls": provider.close_calls,
    "reaper_alive": registry.reaper_alive,
    "worker_alive": provider.worker.is_alive(),
    "child_returncode": provider.child.returncode,
}))
'''
        result = subprocess.run(
            [sys.executable, "-c", script, str(Path(__file__).parents[1])],
            cwd=Path(__file__).parents[1], text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "create_calls": 1,
            "preview_callbacks": 1,
            "registries": 1,
            "providers": 1,
            "registry_close_calls": 1,
            "provider_close_calls": 1,
            "reaper_alive": False,
            "worker_alive": False,
            "child_returncode": -signal.SIGTERM,
        })


if __name__ == "__main__":
    unittest.main()
