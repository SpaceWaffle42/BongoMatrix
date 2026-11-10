import datetime as dt

import pytest
from PIL import ImageChops

from bongo.modules.text import MODULE

T0 = dt.datetime.fromtimestamp(1_700_000_000)


def frame(style, t=0, **settings):
    return MODULE.create(style, settings).render(T0 + dt.timedelta(seconds=t))


def bbox(img, bg=(11, 12, 14)):
    from PIL import Image
    return ImageChops.difference(img, Image.new("RGB", img.size, bg)).getbbox()


def test_scroll_moves_and_wraps():
    assert frame("scroll", 0).tobytes() != frame("scroll", 1).tobytes()
    s = MODULE.create("scroll", dict(speed=10, gap=10, text="ab"))
    p = (s.font.width("ab") + 10) / 10
    a = s.render(T0)
    assert a.tobytes() == s.render(T0 + dt.timedelta(seconds=p)).tobytes()
    assert a.tobytes() == s.render(T0 + dt.timedelta(seconds=2 * p)).tobytes()


def test_static_alignment():
    boxes = {a: bbox(frame("static", text="Hi", align=a)) for a in ("left", "center", "right")}
    assert boxes["left"][0] < boxes["center"][0] < boxes["right"][0]
    assert boxes["right"][2] <= 64


def test_static_is_still():
    assert frame("static", 0).tobytes() == frame("static", 5).tobytes()


def test_uppercase():
    assert frame("static", text="abc", uppercase=True).tobytes() == frame("static", text="ABC").tobytes()
    assert frame("static", text="abc").tobytes() != frame("static", text="ABC").tobytes()


def test_long_static_text_shrinks_or_clips():
    for text in ("This is a rather long sentence", "x" * 200):
        img = frame("static", text=text)
        assert img.size == (64, 32) and bbox(img)


def test_gradient_background_darkens():
    img = frame("static", text="", background_style="gradient", background="#808080")
    assert img.getpixel((0, 31))[0] < img.getpixel((0, 0))[0]


def test_unknown_setting_rejected():
    with pytest.raises(ValueError):
        MODULE.create("scroll", dict(colr="#fff"))


def ink_cols(img, ink=(255, 244, 222)):
    return {x for x in range(img.width) for y in range(img.height) if img.getpixel((x, y)) == ink}


def test_default_text_wraps_to_two_lines_unclipped():
    img = frame("static")
    cols = ink_cols(img)
    assert cols and 0 not in cols and 63 not in cols
    rows = {y for x in range(64) for y in range(32) if img.getpixel((x, y)) == (255, 244, 222)}
    assert max(rows) - min(rows) > 10  # two lines tall
    assert min(rows) >= 2 and max(rows) <= 29  # block roughly centred


def test_wrapped_alignment_and_long_word():
    left = ink_cols(frame("static", align="left"))
    right = ink_cols(frame("static", align="right"))
    assert left != right and max(left) <= max(right)
    assert ink_cols(frame("static", text="x" * 80))  # single long word clips, no crash


def test_fallback_font_recentres_vertically():
    img = frame("static", text="Hello Bongos", y=30)  # fits in 5x7 on one line
    ys = [y for x in range(64) for y in range(32) if img.getpixel((x, y)) == (255, 244, 222)]
    assert 10 <= min(ys) and max(ys) <= 22
