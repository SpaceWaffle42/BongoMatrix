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
    assert "custom_palettes" in classic and "custom_palettes" in metro  # shared, like v2


@pytest.mark.parametrize("name", CLASSIC)
def test_every_classic_palette_resolves(name):
    style = LAVA.create("classic", {"palettes": [name]}, ctx())
    assert style.outside == rgb(CLASSIC[name][0])
    assert style.render(NOW).size == (64, 32)


MINE = {"name": "Bedroom", "background": "#010203", "wax": "#ffffff", "highlight": "#ff0000"}


def test_a_custom_palette_is_a_name_and_three_colours_used_by_both_styles():
    classic = LAVA.create("classic", {"palettes": [], "custom_palettes": [MINE]}, ctx())
    assert (classic.outside, classic.inside, classic.line) == ((1, 2, 3), (255, 255, 255), (255, 0, 0))
    metro = LAVA.create("metro", {"palettes": [], "custom_palettes": [MINE]}, ctx())
    assert tuple(metro.p["mid"]) == (255, 255, 255) and tuple(metro.p["core"]) == (255, 0, 0)
    assert metro.render(NOW).size == (64, 32)


def test_disabled_custom_palettes_are_kept_but_not_picked():
    off = {**MINE, "enabled": False}
    style = LAVA.create("classic", {"palettes": ["fire_burst"], "custom_palettes": [off]}, ctx())
    assert style.outside == rgb(CLASSIC["fire_burst"][0])
    with pytest.raises(ValueError, match="at least one palette"):
        LAVA.create("classic", {"palettes": [], "custom_palettes": [off]}, ctx())


@pytest.mark.parametrize("bad, message", [
    ([{"name": "", "background": "#000000", "wax": "#ffffff", "highlight": "#ff0000"}], "give it a name"),
    ([{**MINE, "wax": "red"}], "wax must be a colour"),
    ([MINE, {**MINE}], "already called"),
    ([{**MINE, "sparkle": True}], "unknown keys"),
    ([["#000000", "#ffffff", "#ff0000"]], "must be an object"),
])
def test_custom_palettes_are_checked_with_readable_messages(bad, message):
    with pytest.raises(ValueError, match=message):
        LAVA.settings_for("metro", {"custom_palettes": bad})


@pytest.mark.parametrize("name", METRO)
def test_every_metro_palette_renders(name):
    assert LAVA.create("metro", {"palettes": [name]}, ctx()).render(NOW).size == (64, 32)


def test_metro_rejects_unknown_palette():
    with pytest.raises(ValueError, match="plaid"):
        LAVA.create("metro", {"palettes": ["plaid"]}, ctx())


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



def test_seconds_shrink_the_time_font_instead_of_clipping():
    from bongo.module import Context
    from bongo.modules.lava_lamp.metro import fitting
    fonts = Context().fonts
    for name in ("10x20", "9x18B"):
        font = fitting(fonts, fonts(name), "22:58:58", 60)
        assert font.width("22:58:58") <= 58 and font is not fonts(name)
    assert fitting(fonts, fonts("10x20"), "22:58", 60) is fonts("10x20")
