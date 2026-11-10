import io

import pytest
from PIL import Image

from bongo import media
from bongo.live import Live
from bongo.pairing import Pairing
from bongo.registry import discover
from bongo.web import create_app


def png(size=(8, 8), colour=(255, 0, 0)):
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "UPLOADED", tmp_path / "uploads")
    pairing = Pairing(tmp_path / "paired.json")
    c = create_app(modules=discover(), live=Live(), pairing=pairing, config_path=tmp_path / "config.json").test_client()
    pairing.show_pin()
    assert c.post("/api/pair", json={"pin": pairing.pin, "name": "t"}).status_code == 200
    return c


def upload(client, name, data, header=True):
    return client.post("/api/media", data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data",
                       headers={"X-Bongo-Upload": "1"} if header else {})


def test_names_must_be_in_one_of_the_two_folders():
    assert media.valid_name("built-in/sleep.gif") and media.valid_name("uploaded/cat.mp4")
    for bad in ("sleep.gif", "/etc/passwd", "uploaded/../x.gif", "other/x.gif", "uploaded/notes.txt", None, 3):
        assert not media.valid_name(bad), bad


def test_clean_names():
    assert media.clean_name("../../My Cat (1).PNG") == "My-Cat-1.png"
    assert media.clean_name("ünïcödé.gif") == "unicode.gif" and media.clean_name("???.gif") == "upload.gif"


def test_upload_list_use_and_delete(client):
    r = upload(client, "Red square.png", png())
    assert r.status_code == 201 and r.get_json()["name"] == "uploaded/Red-square.png"
    assert upload(client, "Red square.png", png()).get_json()["name"] == "uploaded/Red-square-2.png"  # never overwrites
    lib = client.get("/api/media").get_json()
    assert [f["name"] for f in lib["folders"]["uploaded"]] == ["uploaded/Red-square-2.png", "uploaded/Red-square.png"]
    assert client.get("/api/preview.png?module=media&settings=" + '{"file":"uploaded/Red-square.png"}').status_code == 200
    assert client.put("/api/config", json={"playlist": [{"module": "media", "settings": {"file": "uploaded/Red-square.png"}}]}).status_code == 200
    busy = client.delete("/api/media/uploaded/Red-square.png", json={})
    assert busy.status_code == 409 and "playlist entry 1" in busy.get_json()["error"]
    assert client.delete("/api/media/uploaded/Red-square-2.png", json={}).status_code == 200
    assert client.delete("/api/media/built-in/sleep.gif", json={}).status_code == 403
    assert client.delete("/api/media/uploaded/nope.png", json={}).status_code == 404


def test_bad_uploads_are_refused(client, monkeypatch):
    assert upload(client, "x.png", png(), header=False).status_code == 415  # CSRF: needs the custom header
    assert "isn't supported" in upload(client, "x.txt", b"hello").get_json()["error"]
    assert "isn't an image" in upload(client, "fake.png", b"not really a png").get_json()["error"]
    assert "empty" in upload(client, "e.png", b"").get_json()["error"]
    monkeypatch.setattr(media, "MAX_UPLOAD", 100)
    assert "up to" in upload(client, "big.png", png((200, 200), (1, 2, 3)) + b"x" * 200).get_json()["error"]
    assert not list(media.UPLOADED.glob(".upload-*"))  # nothing half-written left behind


def test_unpaired_cannot_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "UPLOADED", tmp_path / "uploads")
    c = create_app(modules=discover(), live=Live(), pairing=Pairing(tmp_path / "p.json"),
                   config_path=tmp_path / "config.json").test_client()
    assert upload(c, "x.png", png()).status_code == 401
    assert c.get("/api/media").status_code == 401


def test_media_setting_is_typed_for_the_editor():
    from bongo.schema import describe
    field = next(f for f in describe(discover()["media"])["settings"] if f["name"] == "file")
    assert field["type"] == "media"


def test_files_used_by_splash_or_sleep_cannot_be_deleted(client):
    upload(client, "logo.png", png())
    assert client.put("/api/config", json={"splash": {"sources": ["uploaded/logo.png"]}}).status_code == 200
    busy = client.delete("/api/media/uploaded/logo.png", json={})
    assert busy.status_code == 409 and "splash" in busy.get_json()["error"]
