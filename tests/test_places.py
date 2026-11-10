import pytest

from bongo import places
from bongo.live import Live
from bongo.pairing import Pairing
from bongo.registry import discover
from bongo.schema import describe
from bongo.web import create_app

ANSWER = {"results": [
    {"name": "Cheltenham", "admin1": "England", "country": "United Kingdom", "country_code": "gb",
     "latitude": 51.90006, "longitude": -2.07972},
    {"name": "Cheltenham", "admin1": "Pennsylvania", "country": "United States", "country_code": "US",
     "latitude": 40.0587, "longitude": -75.0985},
    {"name": "Broken"},  # no coordinates: skipped
]}


@pytest.fixture(autouse=True)
def fake_geocoder(monkeypatch):
    calls = []
    monkeypatch.setattr(places, "fetch_json", lambda url: calls.append(url) or ANSWER)
    places._cache.clear()
    return calls


def test_search_gives_labelled_places_with_coordinates(fake_geocoder):
    found = places.search("  chelten  ")
    assert [p["label"] for p in found] == ["Cheltenham, England, GB", "Cheltenham, Pennsylvania, US"]
    assert found[0]["latitude"] == 51.9001 and found[0]["longitude"] == -2.0797
    places.search("Chelten")
    assert len(fake_geocoder) == 1  # cached
    assert places.search("x") == []  # too short to ask


def test_places_api_needs_pairing_and_reports_failures(tmp_path, monkeypatch):
    pairing = Pairing(tmp_path / "p.json")
    client = create_app(modules=discover(), live=Live(), pairing=pairing, config_path=tmp_path / "c.json").test_client()
    assert client.get("/api/places?q=chel").status_code == 401
    pairing.show_pin()
    client.post("/api/pair", json={"pin": pairing.pin, "name": "t"})
    assert client.get("/api/places?q=chel").get_json()["places"][0]["label"] == "Cheltenham, England, GB"

    def offline(url):
        raise OSError("no network")
    monkeypatch.setattr(places, "fetch_json", offline)
    places._cache.clear()
    assert client.get("/api/places?q=bath").status_code == 502


def test_weather_and_sky_locations_are_place_searches():
    for module in ("weather", "astronomy"):
        fields = {f["name"]: f for f in describe(discover()[module])["settings"]}
        assert fields["location"]["type"] == "place" and not fields["location"]["advanced"]
        assert (fields["location"]["latitude"], fields["location"]["longitude"]) == ("latitude", "longitude")
        assert fields["latitude"]["advanced"] and fields["longitude"]["advanced"]
