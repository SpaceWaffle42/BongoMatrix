import io
import os
import pwd
import shutil
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from config_store import ConfigStore
from pairing import PairedDeviceStore
from secret_store import SecretDurabilityError, SecretStore
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

    def test_paired_device_session_remains_durable_until_revoked(self):
        self.assertEqual(self.app.permanent_session_lifetime, timedelta(days=365))

    def test_removed_device_session_is_immediately_rejected(self):
        self.pair_browser()
        device_id = self.device_store.list()[0]["id"]
        self.device_store.remove(device_id)

        response = self.client.get("/api/status")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "Pairing session expired.")

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
process.stdout.write(JSON.stringify({examples: canvasStarterExamples, modules, rainbowColours}));
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
        self.assertEqual((item["width"], item["height"]), (64, 32))
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
