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
