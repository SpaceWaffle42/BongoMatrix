import datetime as dt
from pathlib import Path

import pytest

from bongo.module import Context
from bongo.modules import media as mm

MODULE = mm.MODULE
RED, GREEN, BLUE = (255, 0, 0), (0, 255, 0), (0, 0, 255)


def _make_fixtures(folder: Path) -> Path:
    """Tiny test images, drawn here so no binary files live in the repo."""
    from PIL import Image, ImageDraw
    folder.mkdir(parents=True, exist_ok=True)
    frames = [Image.new("RGB", (16, 8), c) for c in (RED, GREEN, BLUE)]
    for i, f in enumerate(frames):  # 3 frames lasting 100/200/300 ms, white square moving
        ImageDraw.Draw(f).rectangle([i, i, i + 3, i + 3], fill=(255, 255, 255))
    frames[0].save(folder / "anim.gif", save_all=True, append_images=frames[1:], duration=[100, 200, 300], loop=0)
    t = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    ImageDraw.Draw(t).rectangle([2, 2, 5, 5], fill=(255, 0, 0, 255))
    t.save(folder / "transparent.png")
    w = Image.new("RGB", (40, 10), (0, 0, 200))
    d = ImageDraw.Draw(w)
    d.rectangle([0, 0, 9, 9], fill=RED)
    d.rectangle([30, 0, 39, 9], fill=GREEN)
    d.rectangle([15, 3, 24, 6], fill=(255, 255, 0))
    w.save(folder / "wide.png")
    return folder


import tempfile  # noqa: E402

FIX = _make_fixtures(Path(tempfile.mkdtemp(prefix="bongo-media-")))


def near(a, b, tol=12):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def at(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)


def make(style="fit", file="anim.gif", size=(64, 32), **kw):
    return MODULE.create(style, {"file": str(FIX / file), **kw}, Context(width=size[0], height=size[1]))


def test_frame_selected_by_time_with_durations():
    m = make()
    # frames last 100/200/300 ms; sample the top-left-ish background pixel at (0, 0) of the image area
    px = lambda ms: m.render(at(ms)).getpixel((32, 0))
    assert [px(t) for t in (0, 99, 100, 299, 300, 599, 600, 700)] == [RED, RED, GREEN, GREEN, BLUE, BLUE, RED, GREEN]


def test_speed_multiplier():
    m = make(speed=2.0)
    assert m.render(at(50)).getpixel((32, 0)) == GREEN  # 50ms * 2 = 100ms -> second frame


@pytest.mark.parametrize("size", [(64, 32), (58, 28)])
def test_geometry(size):
    w, h = size
    fit, fill, pix = (make(s, "wide.png", size) for s in ("fit", "fill", "pixel"))
    f = fit.render(at(0))
    assert f.getpixel((0, 0)) == (0, 0, 0) and near(f.getpixel((w // 2, h // 2)), (255, 255, 0))  # letterboxed
    assert near(f.getpixel((0, h // 2)), RED) and near(f.getpixel((w - 1, h // 2)), GREEN)  # full width visible
    g = fill.render(at(0))
    assert all(g.getpixel((x, y)) != (0, 0, 0) for x in (0, w - 1) for y in (0, h - 1))
    assert near(g.getpixel((6, h // 2)), (0, 0, 200))  # red end cropped away
    p = pix.render(at(0))  # 40x10 -> 1x at 58 wide, 1x at 64 wide; centred, unsmoothed
    assert {c for _, c in p.getcolors(10000)} <= {(0, 0, 0), RED, GREEN, (0, 0, 200), (255, 255, 0)}
    assert p.getpixel((w // 2, h // 2)) == (255, 255, 0) and p.getpixel((0, 0)) == (0, 0, 0)


def test_pixel_integer_scale():
    p = make("pixel", "anim.gif", (64, 32)).render(at(0))  # 16x8 -> 4x: fills 64x32
    assert p.getpixel((0, 0)) == (255, 255, 255) and p.getpixel((63, 31)) == RED
    assert p.getpixel((15, 15)) == (255, 255, 255) and p.getpixel((16, 0)) == RED  # hard 4px blocks


def test_transparency_shows_background():
    m = make("pixel", "transparent.png", (64, 32), background="#102030")
    f = m.render(at(0))
    assert f.getpixel((0, 0)) == (16, 32, 48) and f.getpixel((32, 16)) == RED


def test_brightness_dims():
    full, half = make().render(at(0)), make(brightness=0.5).render(at(0))
    assert half.getpixel((32, 0)) == (127, 0, 0) and full.getpixel((32, 0)) == RED


@pytest.mark.parametrize("file", ["nope.gif", "clip.mp4", "../../../etc/passwd", "/definitely/not/here.gif"])
def test_missing_or_unsafe_file_gives_placeholder(file, tmp_path, monkeypatch):
    monkeypatch.setattr(mm, "ROOT", tmp_path)
    (tmp_path.parent / "clip.mp4").write_bytes(b"x")
    m = MODULE.create("fit", {"file": file}, Context())
    f = m.render(at(0))
    assert f.size == (64, 32) and f.getcolors(5000) and len(f.getcolors(5000)) > 1


def test_path_escape_rejected_even_if_target_exists(tmp_path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    (tmp_path / "secret.gif").write_bytes((FIX / "anim.gif").read_bytes())
    monkeypatch.setattr(mm, "ROOT", root)
    assert mm.locate("../secret.gif") is None
    assert mm.locate(str(tmp_path / "secret.gif")) is not None  # absolute + exists is allowed


def test_relative_path_resolves_against_root(monkeypatch):
    monkeypatch.setattr(mm, "ROOT", FIX)
    m = MODULE.create("fit", {"file": "anim.gif"}, Context())
    assert m.render(at(150)).getpixel((32, 0)) == GREEN


def test_default_file_works_whether_or_not_it_exists():
    assert MODULE.create(None, None, Context(width=32, height=16)).render(at(0)).size == (32, 16)


BUILT_IN = sorted((mm.ROOT / "built-in").glob("*.gif"))


@pytest.mark.skipif(not BUILT_IN, reason="repo media not present locally (it is in CI)")
@pytest.mark.parametrize("path", BUILT_IN, ids=[p.name for p in BUILT_IN])
def test_built_in_gifs_decode_and_animate(path):
    """The real built-in library: every GIF decodes, animates, and isn't blank."""
    style = MODULE.create("fit", {"file": f"built-in/{path.name}"}, Context())
    assert len(style.frames) > 1
    frames = {style.render(at(ms)).tobytes() for ms in range(0, 3000, 50)}
    assert len(frames) > 1
    assert any(f.getbbox() for f in style.frames)  # some loaders start from a black frame
