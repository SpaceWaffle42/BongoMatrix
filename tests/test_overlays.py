import datetime as dt

from PIL import Image

from bongo import config as cfg
from bongo.module import Context, Module, Style
from bongo.overlays import Overlays, Rule
from bongo.panel import RecordingPanel
from bongo.player import Entry, play
from bongo.registry import discover

T0 = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.timezone.utc)


class Corner(Style):
    """A white top-left pixel on a transparent frame."""
    fps = 30

    def render(self, now):
        img = Image.new("RGBA", (self.ctx.width, self.ctx.height), (0, 0, 0, 0))
        img.putpixel((0, 0), (255, 255, 255, 255))
        return img


class Broken(Corner):
    def render(self, now):
        raise RuntimeError("boom")


class Blue(Style):
    fps = 50

    def render(self, now):
        return self.ctx.canvas((0, 0, 255))


MODS = {"m": Module(id="m", name="M", styles={"blue": Blue}, overlays={"corner": Corner, "broken": Broken})}


def test_rule_timing():
    r = Rule("m", every=60, seconds=10, offset=5)
    assert not r.on(T0) and r.on(T0 + dt.timedelta(seconds=5)) and not r.on(T0 + dt.timedelta(seconds=15))
    assert Rule("m", every=0).on(T0) and not Rule("m", enabled=False).on(T0)


def test_overlay_composites_over_the_frame_and_broken_ones_switch_off():
    o = Overlays([Rule("m", "corner"), Rule("m", "broken")], MODS, Context)
    out = o(Context().canvas((0, 0, 255)), T0)
    assert out.mode == "RGB" and out.getpixel((0, 0)) == (255, 255, 255) and out.getpixel((5, 5)) == (0, 0, 255)
    assert o.rules[1].enabled is False and bool(o)


def test_player_draws_overlays_on_the_day_playlist():
    panel = RecordingPanel()
    play([Entry("m", "blue", seconds=0.03)], MODS, panel, loops=1,
         overlays=Overlays([Rule("m", "corner")], MODS, Context))
    assert panel.frames and panel.frames[0].getpixel((0, 0)) == (255, 255, 255)


def test_ribbon_shows_only_when_a_notice_is_active():
    mods = discover()
    ribbon = mods["announcements"].create("ribbon", {"items": [{"title": "Bins", "text": "Green bin tonight",
                                                                 "from": "18:00", "to": "22:00"}]},
                                          Context(), overlay=True)
    assert ribbon.render(dt.datetime(2026, 11, 10, 12, 0)).getbbox() is None  # all transparent
    lit = ribbon.render(dt.datetime(2026, 11, 10, 19, 0))
    assert lit.getbbox() is not None and lit.getbbox()[1] >= 20  # a band near the bottom


def test_config_validates_overlays():
    mods = discover()
    good = {"overlays": [{"module": "announcements", "overlay": "ribbon", "every": 300, "seconds": 30}]}
    assert cfg.validate({**cfg.DEFAULTS, **good}, mods) == []
    for bad in ([{"module": "nope"}], [{"module": "announcements", "overlay": "banner"}],
                [{"module": "announcements", "every": "5"}], [{"module": "announcements", "typo": 1}], "x"):
        assert cfg.validate({**cfg.DEFAULTS, "overlays": bad}, mods), bad
