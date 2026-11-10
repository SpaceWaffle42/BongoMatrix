import pytest
from PIL import Image

from bongo import config as cfg
from bongo.__main__ import _margins
from bongo.module import Context
from bongo.safe_area import SafeArea


def test_inner_size_and_placement():
    area = SafeArea(top=2, right=3, bottom=1, left=4)
    assert (area.width, area.height) == (57, 29)
    panel = area.place(Image.new("RGB", (57, 29), (255, 0, 0)))
    assert panel.size == (64, 32)
    assert panel.getpixel((4, 2)) == (255, 0, 0) and panel.getpixel((60, 30)) == (255, 0, 0)
    assert panel.getpixel((3, 2)) == (0, 0, 0) and panel.getpixel((61, 30)) == (0, 0, 0)
    assert panel.getpixel((4, 1)) == (0, 0, 0) and panel.getpixel((4, 31)) == (0, 0, 0)


def test_no_margins_is_a_pass_through():
    frame = Image.new("RGB", (64, 32))
    assert SafeArea().place(frame) is frame


@pytest.mark.parametrize("bad", [dict(top=-1), dict(left=20, right=20), dict(top=10, bottom=10)])
def test_rejects_negative_or_too_small(bad):
    with pytest.raises(ValueError):
        SafeArea(**bad)


def test_config_drives_context_size(tmp_path):
    config = cfg.load(tmp_path / "none.json")
    config["safe_area"] = {"top": 1, "left": 2, "right": 2}
    config = {**config, "safe_area": {**cfg.DEFAULTS["safe_area"], **config["safe_area"]}}
    ctx = cfg.context_factory(config)()
    assert (ctx.width, ctx.height) == (60, 31)


def test_calibration_card_marks_the_edge():
    area = SafeArea(top=2, right=2, bottom=2, left=2)
    card = area.calibration(Context().fonts("tom-thumb"))
    assert card.size == (64, 32)
    assert card.getpixel((2, 2)) != (0, 0, 0) and card.getpixel((61, 29)) != (0, 0, 0)


@pytest.mark.parametrize("text,expected", [("2", (2, 2, 2, 2)), ("1,3", (1, 3, 1, 3)), ("1,2,3,4", (1, 2, 3, 4))])
def test_margin_shorthand(text, expected):
    m = _margins(text)
    assert (m["top"], m["right"], m["bottom"], m["left"]) == expected
