import pytest

from bongo import fade
from bongo.fade import Fader, level_at
from bongo.panel import RecordingPanel
from bongo.schedule import Schedule, screen_problems


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.mark.parametrize("curve", list(fade.CURVES))
def test_every_curve_starts_and_ends_on_the_levels(curve):
    assert level_at(80, 15, 0, curve) == pytest.approx(80)
    assert level_at(80, 15, 1, curve) == pytest.approx(15)
    mid = level_at(80, 15, 0.5, curve)
    assert 15 < mid < 80


def test_curves_differ():
    mids = {c: round(level_at(80, 10, 0.25, c), 1) for c in fade.CURVES}
    assert len(set(mids.values())) == len(mids)


def test_ramp_glides_over_the_seconds():
    clock, panel = Clock(), RecordingPanel()
    f = Fader(panel, clock)
    f.ramp_to(80)                 # first light: straight there
    assert panel.brightness == [80]
    f.ramp_to(20, seconds=5, curve="linear")
    clock.t = 2.5
    f.tick()
    assert panel.brightness[-1] == 50
    assert f.ramping
    clock.t = 5.1
    f.tick()
    assert panel.brightness[-1] == 20 and not f.ramping


def test_asking_for_the_same_target_does_not_restart():
    clock, panel = Clock(), RecordingPanel()
    f = Fader(panel, clock)
    f.ramp_to(80)
    f.ramp_to(20, seconds=4, curve="linear")
    clock.t = 2
    f.ramp_to(20, seconds=4, curve="linear")
    f.tick()
    assert panel.brightness[-1] == 50


def test_override_jumps_and_zero_seconds_jumps():
    clock, panel = Clock(), RecordingPanel()
    f = Fader(panel, clock)
    f.ramp_to(80)
    f.ramp_to(10, seconds=5)
    f.set_brightness(60)
    assert panel.brightness[-1] == 60 and not f.ramping
    f.ramp_to(30, seconds=0)
    assert panel.brightness[-1] == 30


def test_fade_settings_are_read_and_checked():
    rule = {"from": "23:00", "to": "07:00", "sources": ["built-in/sleep_default.gif"], "fade": 12, "curve": "ease_out"}
    s = Schedule.from_config({"sleep": rule})
    assert (s.fade_seconds, s.fade_curve) == (12, "ease_out")
    assert Schedule.from_config({"sleep": {k: v for k, v in rule.items() if k not in ("fade", "curve")}}).fade_seconds == 5
    assert screen_problems("sleep", rule, sleep=True) == []
    assert screen_problems("sleep", {**rule, "fade": -1, "curve": "wobbly"}, sleep=True) == [
        "sleep.fade: seconds from 0 to 600", "sleep.curve: one of smooth, linear, ease_in, ease_out, perceptual"]
