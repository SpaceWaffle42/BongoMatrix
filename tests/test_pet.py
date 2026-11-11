import datetime as dt

import pytest

from bongo.module import Context
from bongo.modules.pet import MODULE as PET
from bongo.modules.pet.art import load
from bongo.modules.pet.scenes import MODES, record

NOW = dt.datetime(2026, 11, 10, 12, 0)


def test_atlas_has_every_animation_the_modes_use():
    art = load()
    assert art.room.size == (64, 32)
    for mode in MODES:
        for shot in record(art, mode, "Mochi"):
            assert 0 <= shot.index < len(art.animations[shot.tag]), (mode, shot.tag)


def test_life_is_default_and_every_mode_is_a_style():
    assert next(iter(PET.styles)) == "life"
    assert set(MODES) <= set(PET.styles)
    assert "wander" in PET.overlays


@pytest.mark.parametrize("mode", list(MODES))
def test_each_mode_animates(mode):
    style = PET.create(mode, None, Context())
    frames = {style.render(NOW + dt.timedelta(milliseconds=150 * i)).tobytes() for i in range(40)}
    assert len(frames) > 1


def test_name_tag_shows_the_name():
    style = PET.create("name_tag", {"name": "Bean"}, Context())
    shots = [s for s, _ in style.timeline]
    assert any(item[0] == "tag" and item[1].upper() == "BEAN" for s in shots for item in s.overlays)


def test_hungry_banner_uses_the_name():
    style = PET.create("hungry", {"name": "Bean"}, Context())
    banners = {item[1] for s, _ in style.timeline for item in s.overlays if item[0] == "banner"}
    assert any("BEAN" in b.upper() for b in banners)


def test_smaller_safe_area_keeps_the_floor():
    style = PET.create("idle", None, Context(width=48, height=24))
    assert style.render(NOW).size == (48, 24)


def test_life_uses_only_ticked_modes():
    style = PET.create("life", {"modes": ["eating", "napping"]}, Context())
    assert {mode for _, mode in style.timeline} == {"eating", "napping"}


def test_wander_is_mostly_transparent_and_moves():
    style = PET.create("wander", None, Context(), overlay=True)
    a, b = style.render(NOW), style.render(NOW + dt.timedelta(seconds=2))
    assert a.mode == "RGBA"
    alpha = a.getchannel("A").histogram()
    assert alpha[0] > 64 * 32 * 0.7
    assert a.tobytes() != b.tobytes()


def test_pillow_reads_the_webp_atlas():
    from PIL import features
    assert features.check("webp")
