import datetime as dt
import threading

import pytest
from PIL import Image

from bongo import config as cfg
from bongo.fonts import Fonts, align_x
from bongo.module import Context, Module, Style
from bongo.panel import RecordingPanel, led_look
from bongo.player import Entry, play, play_entry
from bongo.registry import discover


class Solid(Style):
    fps = 50
    defaults = {"colour": [255, 0, 0]}

    def render(self, now):
        return self.ctx.canvas(tuple(self.settings["colour"]))


SOLID = Module(id="solid", name="Solid", styles={"plain": Solid})


def test_font_draws_on_baseline_and_measures():
    font = Fonts()("5x7")
    img = Image.new("L", (40, 10))
    advance = font.draw(img, 0, 8, "AB", 255)
    assert advance == font.width("AB") == 10
    assert img.getbbox() is not None and img.getbbox()[3] <= 9


def test_align_x():
    assert align_x("left", 10, 64) == 2
    assert align_x("center", 10, 64) == 27
    assert align_x("right", 10, 64) == 52


def test_play_entry_stops_on_time():
    ticks = iter(range(100))
    panel = RecordingPanel()
    shown = play_entry(SOLID.create(), 5, panel, clock=lambda: next(ticks))
    assert shown == len(panel.frames) > 0


def test_play_cycles_enabled_entries_and_skips_broken_ones():
    panel = RecordingPanel()
    playlist = [Entry("solid", seconds=0.02), Entry("missing"), Entry("solid", enabled=False)]
    play(playlist, {"solid": SOLID}, panel, loops=2)
    assert panel.frames and all(f.getpixel((0, 0)) == (255, 0, 0) for f in panel.frames)


def test_play_respects_stop():
    stop = threading.Event()
    stop.set()
    panel = RecordingPanel()
    play([Entry("solid")], {"solid": SOLID}, panel, stop=stop)
    assert panel.frames == []


def test_config_defaults_round_trip_and_validation(tmp_path):
    path = tmp_path / "config.json"
    config = cfg.load(path)
    assert config["panel"]["cols"] == 64
    config["playlist"] = [{"module": "lava_lamp", "style": "metro", "settings": {"palette": "ocean"}},
                          {"module": "lava_lamp", "settings": {"typo": 1}},
                          {"module": "nope"}]
    cfg.save(config, path)
    assert cfg.load(path)["playlist"][0]["style"] == "metro"
    problems = cfg.validate(cfg.load(path), discover())
    assert len(problems) == 2 and "typo" in problems[0] and "nope" in problems[1]


def test_empty_playlist_falls_back_to_every_module():
    modules = discover()
    assert [e.module for e in cfg.playlist(cfg.load("/nonexistent"), modules)] == list(modules)


def test_context_uses_timezone():
    config = {**cfg.DEFAULTS, "timezone": "Europe/London"}
    assert cfg.context_factory(config)().now().tzinfo is not None


def test_led_look_upscales():
    assert led_look(Context().canvas(), 4).size == (256, 128)
