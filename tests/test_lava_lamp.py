import datetime as dt
import random

import pytest

from bongo.module import Context
from bongo.modules.lava_lamp import MODULE as LAVA
from bongo.modules.lava_lamp.palettes import CLASSIC, METRO, rgb

NOW = dt.datetime(2026, 11, 9, 23, 48)


def ctx(seed=1):
    return Context(rng=random.Random(seed))


def test_has_classic_and_metro_with_classic_first():
    assert list(LAVA.styles) == ["classic", "metro"]


def test_styles_share_module_clock_settings_but_keep_their_own():
    classic, metro = LAVA.settings_for("classic"), LAVA.settings_for("metro")
    assert classic["time_format"] == metro["time_format"] == "%H:%M"
    assert "blur_radius" in classic and "blur_radius" not in metro
    assert "palette" in metro and "palette" not in classic


@pytest.mark.parametrize("name", CLASSIC)
def test_every_classic_palette_resolves(name):
    style = LAVA.create("classic", {"palettes": [name]}, ctx())
    assert style.outside == rgb(CLASSIC[name][0])
    assert style.render(NOW).size == (64, 32)


def test_classic_custom_palette():
    style = LAVA.create("classic", {"palettes": [], "custom_palettes": [["#010203", "#ffffff", "#ff0000"]]}, ctx())
    assert style.outside == (1, 2, 3)


@pytest.mark.parametrize("name", METRO)
def test_every_metro_palette_renders(name):
    assert LAVA.create("metro", {"palette": name}, ctx()).render(NOW).size == (64, 32)


def test_metro_rejects_unknown_palette():
    with pytest.raises(ValueError, match="plaid"):
        LAVA.create("metro", {"palette": "plaid"}, ctx())


@pytest.mark.parametrize("style", ["classic", "metro"])
def test_wax_moves_between_frames(style):
    lamp = LAVA.create(style, {"show_time": False, "show_date": False}, ctx())
    first = lamp.render(NOW).tobytes()
    assert lamp.render(NOW + dt.timedelta(seconds=2)).tobytes() != first


def test_metro_is_a_pure_function_of_time():
    lamp = LAVA.create("metro", None, ctx())
    assert lamp.render(NOW).tobytes() == lamp.render(NOW).tobytes()


@pytest.mark.parametrize("style", ["classic", "metro"])
def test_time_text_is_drawn(style):
    with_text = LAVA.create(style, {"show_date": False}, ctx()).render(NOW)
    without = LAVA.create(style, {"show_time": False, "show_date": False}, ctx()).render(NOW)
    assert with_text.tobytes() != without.tobytes()
