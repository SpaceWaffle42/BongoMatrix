import datetime as dt

import pytest

from bongo import config as cfg
from bongo.module import Context, Module, Style
from bongo.panel import RecordingPanel
from bongo.player import Entry, play
from bongo.registry import discover
from bongo.schedule import Schedule, within


class Solid(Style):
    fps = 50
    defaults = {"colour": [255, 0, 0]}

    def render(self, now):
        return self.ctx.canvas(tuple(self.settings["colour"]))


SOLID = {"solid": Module(id="solid", name="Solid", styles={"plain": Solid})}
RED, BLUE, GREEN = [255, 0, 0], [0, 0, 255], [0, 255, 0]


def at(hour, minute=0):
    return lambda: type("Ctx", (Context,), {"now": lambda self: dt.datetime(2026, 11, 10, hour, minute)})()


@pytest.mark.parametrize("now,expected", [((22, 59), False), ((23, 0), True), ((3, 0), True), ((7, 0), False)])
def test_overnight_window(now, expected):
    assert within(dt.datetime(2026, 11, 10, *now), "23:00", "07:00") is expected


@pytest.fixture
def library(tmp_path, monkeypatch):
    """uploaded/ holds green.gif (2 x 100 ms), blue.gif (2 x 3 s), red.png (a still)."""
    from PIL import Image

    from bongo import media, schedule as sched
    monkeypatch.setattr(media, "UPLOADED", tmp_path)
    monkeypatch.setattr(sched, "_bags", {})
    monkeypatch.setattr(sched, "_night", {})
    monkeypatch.setattr(sched, "SLEEP_ENTRY_SECONDS", 0.05)
    for name, colour, ms in (("green", GREEN, 100), ("blue", BLUE, 3000)):
        frames = [Image.new("RGB", (8, 8), tuple(colour)), Image.new("RGB", (8, 8), tuple(colour))]
        frames[0].save(tmp_path / f"{name}.gif", save_all=True, append_images=frames[1:], duration=ms, loop=0)
    Image.new("RGB", (8, 8), tuple(RED)).save(tmp_path / "red.png")
    return {**SOLID, "media": discover()["media"]}


def schedule(**sleep):
    return Schedule.from_config({
        "panel": {"brightness": 80},
        "splash": {"sources": ["uploaded/green.gif"], "speed": 400, "fit": "fill"},
        "sleep": {"from": "23:00", "to": "07:00", "brightness": 10, "sources": ["uploaded/blue.gif"], "fit": "fill",
                  **sleep},
    })


def colours(panel):
    return [f.getpixel((0, 0)) for f in panel.frames]


def test_splash_plays_one_gif_pass_then_the_day_playlist(library):
    panel = RecordingPanel()
    play([Entry("solid", seconds=0.02, settings={"colour": RED})], library, panel, at(12), loops=2,
         schedule=schedule())
    seen = colours(panel)
    assert seen[0] == tuple(GREEN) and tuple(RED) in seen and tuple(BLUE) not in seen
    assert panel.brightness == [80]


def test_splash_length_is_one_pass_at_its_speed(library):
    assert schedule().splash.seconds == pytest.approx(0.05)  # 2 x 100 ms at 400 %
    s = Schedule.from_config({"splash": {"sources": ["uploaded/green.gif"]}})
    assert s.splash.seconds == pytest.approx(0.2)


def test_stills_hold_for_the_gifs_average_pass_or_seven_seconds(library):
    from bongo.schedule import hold_seconds
    assert hold_seconds("uploaded/red.png", ["uploaded/red.png", "uploaded/blue.gif"], 100) == pytest.approx(6.0)
    assert hold_seconds("uploaded/red.png", ["uploaded/red.png", "uploaded/green.gif"], 100) == 7.0  # 0.2 s: too short
    assert hold_seconds("uploaded/red.png", ["uploaded/red.png"], 100) == 7.0


def test_every_splash_plays_once_before_any_repeats(library):
    s = Schedule.from_config({"splash": {"sources": ["uploaded/green.gif", "uploaded/blue.gif", "uploaded/red.png"]}})
    first = {s.splash.settings["file"] for _ in range(3)}
    second = {s.splash.settings["file"] for _ in range(3)}
    assert first == second == {"uploaded/green.gif", "uploaded/blue.gif", "uploaded/red.png"}


def test_sleep_shows_one_pick_all_night_at_sleep_brightness(library):
    panel = RecordingPanel()
    s = schedule(sources=["uploaded/blue.gif", "uploaded/red.png"])
    s.splash = None
    play([Entry("solid", seconds=0.02, settings={"colour": GREEN})], library, panel, at(2), loops=1, schedule=s)
    pick = s.sleep_entry().settings["file"]
    assert all(s.sleep_entry().settings["file"] == pick for _ in range(20))
    assert tuple(GREEN) not in colours(panel) and panel.brightness == [10]
    s.wake()
    assert {Schedule.from_config({"sleep": {"from": "23:00", "to": "07:00", "sources": ["uploaded/blue.gif",
            "uploaded/red.png"]}}).sleep_entry().settings["file"] for _ in range(1)}


def test_rules_can_be_switched_off(library):
    s = Schedule.from_config({"splash": {"sources": ["uploaded/green.gif"], "enabled": False},
                              "sleep": {"from": "23:00", "to": "07:00", "sources": ["uploaded/blue.gif"], "enabled": False}})
    assert s.splash is None and not s.asleep(dt.datetime(2026, 11, 10, 2))


def test_no_schedule_behaves_as_before():
    panel = RecordingPanel()
    play([Entry("solid", seconds=0.02)], SOLID, panel, loops=1)
    assert panel.frames and panel.brightness == []


def test_validate_reports_bad_splash_and_sleep(tmp_path):
    config = cfg.load(tmp_path / "x.json")
    config["splash"] = {"sources": ["/etc/passwd", "uploaded/clip.mp4"], "speed": 900}
    config["sleep"] = {"from": "late", "to": "07:00", "sources": []}
    problems = " | ".join(cfg.validate(config, discover()))
    for bit in ("'/etc/passwd' isn't a GIF", "'uploaded/clip.mp4' isn't a GIF", "splash.speed",
                "sleep.sources: pick at least one", "sleep.from"):
        assert bit in problems, bit
    config["splash"] = {"sources": ["built-in/sleep_default.gif"]}
    config["sleep"] = {"from": "23:00", "to": "07:00", "sources": ["built-in/splash_loading_squares.gif"]}
    crossed = " | ".join(cfg.validate(config, discover()))
    assert "isn't a built-in splash screen" in crossed and "isn't a built-in sleep screen" in crossed
    config["splash"] = {"sources": ["built-in/splash_loading_squares.gif"]}
    config["sleep"] = {"from": "23:00", "to": "07:00", "sources": ["built-in/sleep_default.gif"], "brightness": 15}
    assert cfg.validate(config, discover()) == []


def test_player_reports_the_playlist_slot_and_stops_on_reload():
    from bongo.live import Live

    live, seen = Live(), []
    real = live.playing

    def spy(entry, upcoming, index=None):
        seen.append(index)
        real(entry, upcoming, index)
        if len(seen) == 3:
            live.apply_config()

    live.playing = spy
    playlist = [Entry("solid", seconds=0.01), Entry("solid", seconds=0.01, enabled=False),
                Entry("solid", seconds=0.01, settings={"colour": BLUE})]
    play(playlist, SOLID, RecordingPanel(), live=live)  # returns because of the reload
    assert seen == [0, 2, 0] and live.status()["index"] == 0


def test_a_module_that_crashes_while_playing_is_skipped():
    class Boom(Solid):
        def render(self, now):
            raise RuntimeError("boom")

    mods = {**SOLID, "boom": Module(id="boom", name="Boom", styles={"plain": Boom})}
    panel = RecordingPanel()
    play([Entry("boom", seconds=0.02), Entry("solid", seconds=0.02)], mods, panel, loops=1)
    assert panel.frames  # the good entry still played
