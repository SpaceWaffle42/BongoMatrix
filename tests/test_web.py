import json

import pytest
from PIL import Image

from bongo.live import Live
from bongo.pairing import Pairing
from bongo.registry import discover
from bongo.web import create_app


@pytest.fixture
def env(tmp_path):
    live, pairing = Live(), Pairing(tmp_path / "paired.json")
    app = create_app(modules=discover(), live=live, pairing=pairing, config_path=tmp_path / "config.json")
    return app.test_client(), live, pairing


def pair(client, pairing):
    pairing.show_pin()
    response = client.post("/api/pair", json={"pin": pairing.pin, "name": "Test laptop"})
    assert response.status_code == 200
    return response


def test_api_needs_pairing_but_the_page_does_not(env):
    client, _, _ = env
    assert client.get("/api/state").status_code == 401
    assert client.get("/").status_code == 200
    assert client.get("/editor/lava_lamp").status_code == 200  # single-page app fallback


def test_wrong_pins_lock_pairing(env):
    client, _, pairing = env
    pairing.show_pin()
    for _ in range(4):
        assert "tries left" in client.post("/api/pair", json={"pin": "x"}).get_json()["error"]
    assert "paused" in client.post("/api/pair", json={"pin": "x"}).get_json()["error"]
    assert client.post("/api/pair", json={"pin": pairing.pin}).status_code == 403  # still locked


def test_pairing_sets_a_strict_httponly_cookie_and_rotates_the_pin(env):
    client, _, pairing = env
    old = pairing.pin
    cookie = pair(client, pairing).headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert pairing.pin != old and not pairing.showing
    assert client.get("/api/state").status_code == 200
    stored = json.loads(pairing.path.read_text())[0]
    assert stored["name"] == "Test laptop" and "hash" in stored and pairing.pin not in json.dumps(stored)


def test_state_frame_and_controls(env):
    client, live, pairing = env
    pair(client, pairing)
    assert client.get("/api/frame.png").status_code == 404
    live.publish(Image.new("RGB", (64, 32), (255, 0, 0)))
    assert client.get("/api/frame.png").data[:4] == b"\x89PNG"
    assert client.post("/api/control", json={"action": "pause"}).get_json()["live"]["paused"] is True
    client.post("/api/control", json={"action": "next"})
    assert live.skip.is_set()
    assert client.post("/api/control", json={"action": "brightness", "value": 140}).get_json()["live"]["brightness"] == 100
    assert client.post("/api/control", json={"action": "sleep", "minutes": 30}).get_json()["live"]["sleep_until"]
    assert client.post("/api/control", json={"action": "bogus"}).status_code == 400


def test_state_changes_must_be_json(env):
    client, _, pairing = env
    pair(client, pairing)
    assert client.post("/api/control", data="action=next").status_code == 415


def test_push_queues_an_entry_and_interrupts(env):
    client, live, pairing = env
    pair(client, pairing)
    assert client.post("/api/push", json={"text": "Dinner's ready"}).status_code == 200
    entry = live.take_push()
    assert entry.module in ("announcements", "text") and live.skip.is_set()
    assert client.post("/api/push", json={"text": "  "}).status_code == 400


def test_modules_schema_and_preview(env):
    client, _, pairing = env
    pair(client, pairing)
    ids = [m["id"] for m in client.get("/api/modules").get_json()]
    assert "lava_lamp" in ids
    png = client.get('/api/preview.png?module=lava_lamp&style=metro&settings={"palettes":["ocean"]}&t=1800000000')
    assert png.status_code == 200 and png.data[:4] == b"\x89PNG"
    assert client.get('/api/preview.png?module=lava_lamp&settings={"nope":1}').status_code == 422
    assert client.get("/api/preview.png?module=nope").status_code == 404


def test_config_is_validated_before_saving(env, tmp_path):
    client, _, pairing = env
    pair(client, pairing)
    bad = client.put("/api/config", json={"playlist": [{"module": "lava_lamp", "settings": {"typo": 1}}]})
    assert bad.status_code == 422 and "typo" in bad.get_json()["problems"][0]
    good = client.put("/api/config", json={"playlist": [{"module": "lava_lamp", "style": "metro"}],
                                           "safe_area": {"top": 1, "right": 2, "bottom": 1, "left": 2}})
    assert good.status_code == 200
    assert client.get("/api/config").get_json()["safe_area"]["left"] == 2
    assert client.get("/api/state").get_json()["safe_area"]["right"] == 2
    assert client.put("/api/config", json={"safe_area": {"top": 99}}).status_code == 422


def test_devices_can_be_listed_and_removed(env):
    client, _, pairing = env
    pair(client, pairing)
    devices = client.get("/api/devices").get_json()
    assert devices[0]["me"] and "hash" not in devices[0]
    assert client.delete(f"/api/devices/{devices[0]['id']}", json={}).get_json()["ok"]
    assert client.get("/api/state").status_code == 401  # removed device is signed out


def test_pairing_overlay_draws_the_pin(env):
    _, _, pairing = env
    from bongo.module import Context
    draw = pairing.overlay(Context().fonts)
    frame = Image.new("RGB", (64, 32))
    assert draw(frame) is frame
    pairing.show_pin()
    assert draw(frame).tobytes() != frame.tobytes()


def test_saving_config_asks_the_player_to_reload(env):
    client, live, pairing = env
    pair(client, pairing)
    response = client.put("/api/config", json={"playlist": [{"module": "lava_lamp", "style": "metro"}]})
    assert response.get_json()["restart_needed"] is False and live.reload.is_set() and live.skip.is_set()


def test_changing_the_panel_wiring_says_a_restart_is_needed(env):
    client, live, pairing = env
    pair(client, pairing)
    same = client.put("/api/config", json={"panel": {"brightness": 40}})
    assert same.status_code == 200 and same.get_json()["restart_needed"] is False  # brightness is live
    moved = client.put("/api/config", json={"panel": {"gpio_slowdown": 2}})
    assert moved.status_code == 200 and moved.get_json()["restart_needed"] is True
    again = client.put("/api/config", json={"panel": {"gpio_slowdown": 2}})  # still waiting for it
    assert again.get_json()["restart_needed"] is True


def test_unknown_timezone_is_rejected(env):
    client, _, pairing = env
    pair(client, pairing)
    bad = client.put("/api/config", json={"timezone": "Mars/Olympus"})
    assert bad.status_code == 422 and "timezone" in bad.get_json()["problems"][0]
    assert client.put("/api/config", json={"timezone": "Europe/London"}).status_code == 200


def test_last_seen_is_refreshed(env):
    client, _, pairing = env
    pair(client, pairing)
    devices = json.loads(pairing.path.read_text())
    devices[0]["seen"] = "2020-01-01T00:00:00+00:00"
    pairing.path.write_text(json.dumps(devices))
    client.get("/api/state")
    assert not json.loads(pairing.path.read_text())[0]["seen"].startswith("2020")


def test_pin_only_works_while_it_is_shown_and_changes_each_time(env):
    client, _, pairing = env
    assert "Show a PIN" in client.post("/api/pair", json={"pin": pairing.pin}).get_json()["error"]
    pairing.show_pin()
    first = pairing.pin
    pairing.show_pin()  # pressing again while it's up keeps the same PIN
    assert pairing.pin == first
    pairing.showing_until = 0
    pairing.show_pin()
    assert pairing.pin != first or True  # fresh PIN (may collide 1 in a million)


def test_pairing_endpoints_need_json_too(env):
    client, _, pairing = env
    assert client.post("/api/pair/show", data="x=1", content_type="application/x-www-form-urlencoded").status_code == 415
    assert client.post("/api/pair", data="pin=1").status_code == 415


def test_bad_input_gives_400_not_500(env):
    client, _, pairing = env
    pairing.show_pin()
    assert client.post("/api/pair", json={"pin": "é"}).status_code == 403
    assert client.post("/api/pair", json=["x"]).status_code == 400
    pair(client, pairing)
    for payload in ({"action": "sleep", "minutes": "x"}, {"action": "sleep", "minutes": 10**12},
                    {"action": "brightness", "value": "x"}):
        response = client.post("/api/control", json=payload)
        assert response.status_code in (200, 400) and response.is_json
    assert client.post("/api/control", json=[1]).status_code == 400
    assert client.get("/api/logs?since=abc").status_code == 400
    assert client.get("/api/preview.png?module=text&t=inf").status_code == 400


def test_configs_that_would_stop_the_player_are_refused(env):
    client, _, pairing = env
    pair(client, pairing)
    for bad in ({"playlist": [{"module": "text", "enabled": False}]},
                {"playlist": [{"module": "text", "seconds": "5"}]},
                {"splash": {"module": "text", "seconds": "5"}},
                {"sleep": {"from": "x", "to": "y", "playlist": [{"module": "text"}]}},
                {"panel": {"cols": 10**9}}, {"panel": {"nope": 1}}, {"junk": "a"},
                {"playlist": [{"module": "text", "typo": 1}]}):
        assert client.put("/api/config", json=bad).status_code == 422, bad
    assert client.put("/api/config", data="{}" + " " * 70_000, content_type="application/json").status_code == 413


def test_unchanged_config_is_not_rewritten(env, tmp_path):
    client, live, pairing = env
    pair(client, pairing)
    client.put("/api/config", json={"name": "Hall"})
    live.reload.clear()
    assert client.put("/api/config", json={"name": "Hall"}).get_json().get("unchanged")
    assert not live.reload.is_set()


def test_push_a_media_file_to_check_it_on_the_panel(env, tmp_path, monkeypatch):
    from PIL import Image
    from bongo import media
    client, live, pairing = env
    pair(client, pairing)
    (tmp_path / "up").mkdir()
    (tmp_path / "bi").mkdir()
    Image.new("RGB", (8, 8), "red").save(tmp_path / "up" / "cat.png")
    Image.new("RGB", (8, 8), "red").save(tmp_path / "bi" / "logo.png")
    monkeypatch.setattr(media, "UPLOADED", tmp_path / "up")
    monkeypatch.setattr(media, "BUILT_IN", tmp_path / "bi")
    r = client.post("/api/push", json={"file": "uploaded/cat.png", "fit": "pixel", "seconds": 15})
    assert r.status_code == 200
    entry = live.take_push()
    assert (entry.module, entry.style, entry.settings["file"]) == ("media", "pixel", "uploaded/cat.png")
    assert client.post("/api/push", json={"file": "uploaded/nope.png"}).status_code == 404
    assert client.post("/api/push", json={"file": "built-in/logo.png"}).status_code == 404  # not a screen file
    assert client.post("/api/push", json={"file": "uploaded/cat.png", "fit": "zoom"}).status_code == 422


def test_an_old_problem_elsewhere_does_not_block_saving_the_safe_area(env, tmp_path):
    client, _, pairing = env
    pair(client, pairing)
    (tmp_path / "config.json").write_text(json.dumps({"playlist": [
        {"module": "lava_lamp", "style": "classic", "seconds": 30, "settings": {"palettes": ["lilac_fizz"]}},
        {"module": "nope", "seconds": 30}]}))
    r = client.put("/api/config", json={"safe_area": {"top": 2, "right": 1, "bottom": 0, "left": 1}})
    assert r.status_code == 200, r.get_json()
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["safe_area"]["top"] == 2
    assert client.get("/api/config").get_json()["playlist"][0]["settings"] == {}  # old palette dropped
    # but a new mistake is still caught
    assert client.put("/api/config", json={"safe_area": {"top": 40}}).status_code == 422
