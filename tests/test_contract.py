"""Every module and every style must honour the contract. New modules are picked up automatically."""
import datetime as dt
import random

import pytest

from bongo.module import Context, Module
from bongo.registry import discover

MODULES = discover(strict=True)
CASES = [(m, s) for m in MODULES.values() for s in m.styles]
NOW = dt.datetime(2026, 11, 9, 23, 48)


def test_modules_found():
    assert "lava_lamp" in MODULES


@pytest.mark.parametrize("module,style", CASES, ids=[f"{m.id}/{s}" for m, s in CASES])
def test_style_renders_panel_sized_rgb_frames(module: Module, style: str):
    instance = module.create(style, ctx=Context(rng=random.Random(1)))
    for i in range(3):
        frame = instance.render(NOW + dt.timedelta(seconds=i))
        assert frame.size == (64, 32) and frame.mode == "RGB"
    assert 1 <= instance.fps <= 60


SAFE_SIZES = [(62, 30), (58, 28), (56, 24), (48, 20), (32, 16)]


@pytest.mark.parametrize("module,style", CASES, ids=[f"{m.id}/{s}" for m, s in CASES])
@pytest.mark.parametrize("size", SAFE_SIZES, ids=[f"{w}x{h}" for w, h in SAFE_SIZES])
def test_style_fits_any_safe_area(module: Module, style: str, size):
    """Modules draw on a canvas the size of the safe area, so they must lay out relative to it."""
    w, h = size
    instance = module.create(style, ctx=Context(width=w, height=h, rng=random.Random(1)))
    for i in range(2):
        frame = instance.render(NOW + dt.timedelta(seconds=i))
        assert frame.size == (w, h) and frame.mode == "RGB"


@pytest.mark.parametrize("module,style", CASES, ids=[f"{m.id}/{s}" for m, s in CASES])
def test_style_is_reproducible_with_a_seed(module: Module, style: str):
    a = module.create(style, ctx=Context(rng=random.Random(7))).render(NOW)
    b = module.create(style, ctx=Context(rng=random.Random(7))).render(NOW)
    assert a.tobytes() == b.tobytes()


@pytest.mark.parametrize("module", MODULES.values(), ids=list(MODULES))
def test_unknown_settings_are_rejected(module: Module):
    with pytest.raises(ValueError, match="unknown setting"):
        module.settings_for(None, {"definitely_not_a_setting": 1})


@pytest.mark.parametrize("module", MODULES.values(), ids=list(MODULES))
def test_unknown_style_is_rejected(module: Module):
    with pytest.raises(ValueError, match="no style"):
        module.create("no_such_style")


OVERLAYS = [(m, o) for m in MODULES.values() for o in m.overlays]


@pytest.mark.parametrize("module,overlay", OVERLAYS, ids=[f"{m.id}/{o}" for m, o in OVERLAYS])
@pytest.mark.parametrize("size", [(64, 32), *SAFE_SIZES], ids=lambda s: f"{s[0]}x{s[1]}")
def test_overlays_render_rgba_frames_of_the_canvas_size(module: Module, overlay: str, size):
    instance = module.create(overlay, ctx=Context(width=size[0], height=size[1], rng=random.Random(1)), overlay=True)
    for i in range(2):
        frame = instance.render(NOW + dt.timedelta(seconds=i))
        assert frame.size == size and frame.mode == "RGBA"
    assert 1 <= instance.fps <= 60


def test_overlays_are_not_playlist_styles():
    for module in MODULES.values():
        assert not set(module.overlays) & set(module.styles), module.id
