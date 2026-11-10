import datetime as dt
import random
import threading
import time
from pathlib import Path

import pytest

from bongo.module import Context
from bongo.modules.rss import MODULE as RSS, data
from bongo.modules.rss.styles import age

FIX = Path(__file__).parent / "fixtures" / "rss"
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 9, 23, 48, tzinfo=UTC)
FEED = "https://example.test/feed.xml"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def fail(url):
        raise OSError("network disabled in tests")

    monkeypatch.setattr(data, "fetch_bytes", fail)
    for store in (data._cache, data._in_flight, data._retry_at):
        store.clear()
    yield
    for store in (data._cache, data._in_flight, data._retry_at):
        store.clear()


def settings(**over):
    return RSS.settings_for(None, {"feeds": [FEED], **over})


def item(title, minutes_ago, source="Wire"):
    return data.Item(title, NOW - dt.timedelta(minutes=minutes_ago), source)


def test_parse_rss_strips_html_and_entities():
    feed = data.parse((FIX / "sample.rss").read_bytes())
    assert feed.title == "Sample Wire"
    assert [i.title for i in feed.items] == [
        "Fire crews tackle blaze at depot & docks", "Council says it's \"on track\" - mostly", "Undated item"]
    assert feed.items[0].published == dt.datetime(2026, 11, 9, 22, 30, tzinfo=UTC)
    assert feed.items[2].published is None and feed.items[0].source == "Sample Wire"


def test_parse_atom():
    feed = data.parse((FIX / "sample.atom").read_bytes())
    assert feed.title == "Atom Daily"
    assert [i.title for i in feed.items] == ["Markets rally as rates hold", "Rare comet visible tonight"]
    assert feed.items[0].published == dt.datetime(2026, 11, 9, 23, 40, tzinfo=UTC)


@pytest.mark.parametrize("bad", [b"<!DOCTYPE x>", b"<!doctype x>", b"<!ENTITY a 'b'>"])
def test_doctype_and_entity_rejected(bad):
    with pytest.raises(ValueError):
        data.parse(b'<?xml version="1.0"?>' + bad + b"<rss><channel><title>x</title></channel></rss>")


def test_merge_sorts_newest_first_and_caps():
    data.seed("a", data.Feed("A", (item("old", 300, "A"), item("new", 5, "A"))))
    data.seed("b", data.Feed("B", (item("mid", 60, "B"), data.Item("undated", None, "B"), item("new", 5, "B"))))
    got = data.latest({"feeds": ["a", "b"], "max_items": 3, "refresh_minutes": 999})
    assert [i.title for i in got] == ["new", "mid", "old"]  # duplicate title dropped, undated last, capped


def test_latest_none_until_loaded():
    assert data.latest(settings()) is None


def test_failed_refresh_keeps_last_data():
    data.seed(FEED, data.Feed("Wire", (item("kept", 5),), fetched=time.time() - 99999))
    data._refresh(FEED)  # fetch_bytes raises OSError
    assert [i.title for i in data.latest(settings())] == ["kept"]
    assert FEED in data._retry_at


def test_offline_env_prevents_threads(monkeypatch):
    monkeypatch.setenv("BONGO_OFFLINE", "1")
    started = []
    monkeypatch.setattr(data, "fetch_bytes", lambda url: started.append(url) or b"")
    before = set(threading.enumerate())
    assert data.latest(settings()) is None
    assert not started and not data._in_flight
    assert not [t for t in set(threading.enumerate()) - before if t.name == "rss"]


def test_refresh_success_populates_cache(monkeypatch):
    monkeypatch.setattr(data, "fetch_bytes", lambda url: (FIX / "sample.atom").read_bytes())
    data._refresh(FEED)
    assert len(data.latest(settings())) == 2


def make(style, size=(64, 32), **over):
    seed()
    return RSS.create(style, {"feeds": [FEED], **over}, Context(width=size[0], height=size[1], rng=random.Random(1)))


def seed(titles=("Alpha headline", "Bravo headline is somewhat longer", "Charlie")):
    data.seed(FEED, data.Feed("Wire", tuple(item(t, 30 + 90 * i) for i, t in enumerate(titles))))


def at(seconds):
    return NOW + dt.timedelta(seconds=seconds)


def test_age_text():
    assert [age(NOW, NOW - dt.timedelta(minutes=m)) for m in (0, 35, 120, 3000)] == ["NOW", "35M", "2H", "2D"]
    assert age(NOW, None) == ""


def test_ticker_moves_then_advances_to_next_headline():
    ticker = make("ticker")
    speed, w = ticker.settings["scroll_speed"], ticker.ctx.width
    font = ticker.sizes[0]
    first = (w + font.width("Alpha headline") + 8) / speed
    base = dt.datetime.fromtimestamp(0, UTC)  # cycle starts at t=0
    early, later = ticker.render(base + dt.timedelta(seconds=1)), ticker.render(base + dt.timedelta(seconds=2))
    assert early.tobytes() != later.tobytes()
    just_before = ticker.render(base + dt.timedelta(seconds=first - 0.2))
    just_after = ticker.render(base + dt.timedelta(seconds=first + 0.1))
    # headline 2 enters at the right edge: its last column holds text pixels while 1 has fully left
    bg = ticker.backdrop
    rows = range(8, 32)
    assert all(just_before.getpixel((x, y)) == bg.getpixel((x, y)) for x in range(0, w // 2) for y in rows)
    assert any(just_after.getpixel((w - 1, y)) != bg.getpixel((w - 1, y)) or
               just_after.getpixel((w - 2, y)) != bg.getpixel((w - 2, y)) for y in rows)


def test_headline_rotates():
    card = make("headline", rotate_seconds=8)
    same = lambda x, y: x.tobytes() == y.tobytes()
    base = NOW.timestamp() // 8 * 8
    f0, f1, f_next = (card.render(dt.datetime.fromtimestamp(base + s, UTC)) for s in (0, 7, 8))
    assert same(f0, f1) and not same(f1, f_next)


def test_headline_layout_wraps_and_falls_back():
    card = make("headline")
    font, lines, _ = card.layout("Storm batters coast, power cut", 62, 24)
    assert len(lines) <= 3 and all(font.width(l) <= 62 for l in lines)
    assert card.layout("Hi", 62, 24)[1] == ["Hi"]
    assert card.layout("Supercalifragilisticexpialidocious " * 3, 20, 24) is None


def test_waiting_screen_and_animation():
    for style in RSS.styles:
        inst = RSS.create(style, {"feeds": [FEED]}, Context(rng=random.Random(1)))
        a, b = inst.render(at(0)), inst.render(at(0.5))
        assert a.getbbox() and a.tobytes() != b.tobytes()
