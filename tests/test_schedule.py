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


def schedule(**sleep):
    return Schedule.from_config({
        "panel": {"brightness": 80},
        "splash": {"module": "solid", "settings": {"colour": GREEN}, "seconds": 0.02},
        "sleep": {"from": "23:00", "to": "07:00", "brightness": 10,
                  "playlist": [{"module": "solid", "settings": {"colour": BLUE}, "seconds": 0.02}], **sleep},
    })


def colours(panel):
    return [f.getpixel((0, 0)) for f in panel.frames]


def test_splash_then_day_playlist_at_day_brightness():
    panel = RecordingPanel()
    play([Entry("solid", seconds=0.02, settings={"colour": RED})], SOLID, panel, at(12), loops=2,
         schedule=schedule())
    seen = colours(panel)
    assert seen[0] == tuple(GREEN) and tuple(RED) in seen and tuple(BLUE) not in seen
    assert panel.brightness == [80]


def test_sleep_playlist_and_brightness_at_night():
    panel = RecordingPanel()
    play([Entry("solid", seconds=0.02, settings={"colour": RED})], SOLID, panel, at(2), loops=2,
         schedule=schedule())
    seen = colours(panel)
    assert tuple(BLUE) in seen and tuple(RED) not in seen
    assert panel.brightness == [10]


def test_no_schedule_behaves_as_before():
    panel = RecordingPanel()
    play([Entry("solid", seconds=0.02)], SOLID, panel, loops=1)
    assert panel.frames and panel.brightness == []


def test_sleep_needs_its_fields():
    with pytest.raises(ValueError, match="playlist"):
        Schedule.from_config({"sleep": {"from": "23:00", "to": "07:00"}})


def test_validate_reports_bad_splash_and_sleep_entries(tmp_path):
    config = cfg.load(tmp_path / "x.json")
    config["splash"] = {"module": "nope"}
    config["sleep"] = {"from": "23:00", "to": "07:00", "playlist": [{"module": "lava_lamp", "settings": {"typo": 1}}]}
    problems = cfg.validate(config, discover())
    assert any(p.startswith("splash") for p in problems) and any(p.startswith("sleep.playlist[0]") for p in problems)
