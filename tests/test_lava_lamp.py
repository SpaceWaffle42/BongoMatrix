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


def test_every_built_in_palette_shows_its_colours_in_the_dashboard():
    import re

    from bongo.schema import describe
    styles = describe(LAVA)["styles"]
    for style, names in (("classic", CLASSIC), ("metro", METRO)):
        field = next(f for f in styles[style]["settings"] if f["name"] == "palettes")
        assert set(field["swatches"]) == set(names)
        for colours in field["swatches"].values():
            assert len(colours) == 3 and all(re.fullmatch(r"#[0-9A-F]{6}", c) for c in colours)


def _lab(hex_colour):
    r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lin = [((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92 for c in (r, g, b)]
    x = (lin[0] * .4124 + lin[1] * .3576 + lin[2] * .1805) / .95047
    y = lin[0] * .2126 + lin[1] * .7152 + lin[2] * .0722
    z = (lin[0] * .0193 + lin[1] * .1192 + lin[2] * .9505) / 1.08883
    f = [v ** (1 / 3) if v > .008856 else 7.787 * v + 16 / 116 for v in (x, y, z)]
    return 116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])


def test_classic_palettes_are_clearly_different_from_each_other():
    """No two palettes may look alike: perceptual (CIELAB) distance, weighted to the wax."""
    import itertools
    import math

    from bongo.modules.lava_lamp.palettes import hex_of
    weights = (0.25, 0.5, 0.25)  # background, wax (most of what you see), highlight

    def apart(a, b):
        return sum(w * math.dist(_lab(hex_of(x)), _lab(hex_of(y))) for w, x, y in zip(weights, a, b))
    closest = min(itertools.combinations(CLASSIC, 2), key=lambda p: apart(CLASSIC[p[0]], CLASSIC[p[1]]))
    assert apart(CLASSIC[closest[0]], CLASSIC[closest[1]]) >= 28, closest


def test_classic_drifts_at_v2s_pace_whatever_the_frame_rate():
    import datetime as dt
    def travel(fps, seconds=4):
        style = LAVA.create("classic", {"palettes": ["fire_burst"]}, ctx())
        start = [list(b) for b in style.blobs]
        t0 = dt.datetime(2026, 11, 11, 12)
        for k in range(int(fps * seconds)):
            style._step(t0 + dt.timedelta(seconds=k / fps))
        return [abs(b[0] - a[0]) for a, b in zip(start, style.blobs)]
    slow, fast = travel(10), travel(33)
    assert all(abs(a - b) < 0.3 for a, b in zip(slow, fast))       # same distance at 10 or 33 fps
    assert max(slow) <= 2.5 * 4 + 0.01                              # never faster than 2.5 px/s


def test_old_per_frame_blob_speed_becomes_drift_per_second():
    from bongo.modules.lava_lamp import migrate
    settings = {"blob_speed": 0.25, "palettes": "all"}
    assert migrate(settings) and settings == {"drift": 2.5, "palettes": "all"}
    assert migrate(settings) == []
