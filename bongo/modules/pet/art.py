"""The pet art: the atlases built by tools/build_pet_atlas.py, unpacked once per coat.

``<kind>.json`` holds one set of frame rectangles that fits every coat. Each coat names the
drawing it uses (``<kind>.webp.b64`` for the default one, which also holds the room and the ball,
or ``<kind>-<drawing>.webp.b64``) and, when it's a recolouring of that drawing, a colour swap."""
from __future__ import annotations

import base64
import io
import json
import math
from dataclasses import dataclass, field

import numpy as np
from functools import lru_cache
from pathlib import Path

from PIL import Image

from bongo.pets import art_file

W, H = 64, 32


@dataclass(frozen=True)
class Frame:
    image: Image.Image  # RGBA, trimmed to the cat
    dx: int             # offset of the image from the cat's feet
    dy: int
    ms: int


@dataclass(frozen=True)
class Art:
    animations: dict    # tag -> [Frame]
    room: Image.Image   # 64x32 RGB, day, bowl full
    empty: Image.Image  # the same with the bowl empty
    night: Image.Image  # dark, lit only around the bed
    ball: Image.Image   # RGBA
    tuning: dict = field(default_factory=dict)  # per-animal scene tweaks (see scenes.py): bowl, ball, reach, zoomies, toy, gait
    toys: dict = field(default_factory=dict)    # name -> (image, dx, dy): placed by the middle of its bottom edge


@lru_cache(maxsize=4)
def _index(kind: str) -> dict:
    return json.loads(art_file(f"{kind}.json").read_text())


@lru_cache(maxsize=8)
def _atlas(name: str) -> Image.Image:
    # a lossless WebP kept as base64 text (see tools/build_pet_atlas.py)
    return Image.open(io.BytesIO(base64.b64decode(art_file(f"{name}.webp.b64").read_text()))).convert("RGBA")


def _coat(kind: str, coat: str) -> dict:
    index = _index(kind)
    found = {c["id"]: c for c in index.get("coats", [])}
    return found.get(coat) or found.get(index.get("default", ""), {})


def recoloured(atlas: Image.Image, swap: dict) -> Image.Image:
    """``atlas`` with every RGBA colour in ``swap`` (hex -> hex) replaced."""
    if not swap:
        return atlas
    px = np.asarray(atlas).copy()
    flat = px.reshape(-1, 4).view("u4").ravel()
    for old, new in swap.items():
        flat[flat == np.frombuffer(bytes.fromhex(old), "u4")[0]] = np.frombuffer(bytes.fromhex(new), "u4")[0]
    return Image.fromarray(px, "RGBA")


@lru_cache(maxsize=4)
def sheet(kind: str = "cat", coat: str = "") -> Image.Image:
    """The atlas for one coat (the kind's default for "" or an unknown coat)."""
    index, found = _index(kind), _coat(kind, coat)
    drawing = found.get("drawing", index.get("default"))
    cats = recoloured(_atlas(kind if drawing in (None, index.get("default")) else f"{kind}-{drawing}"), found.get("swap", {}))
    if drawing in (None, index.get("default")) or "room" not in index:
        return cats
    shared = _atlas(kind)  # the room and the ball live in the default atlas
    for key in ("room", "ball"):
        x, y, w, h = index[key]
        cats.paste(shared.crop((x, y, x + w, y + h)), (x, y))
    return cats


# How far the body moves on entering each walk frame: the pushes land on the frames where the art's
# planted paws sweep back, so the paws stay put instead of treading (review, 12 Nov). 12 px per cycle.
GAIT = {"cat": {"walk_right": (1, 2, 2, 1, 1, 2, 2, 1), "walk_left": (1, 2, 2, 1, 1, 2, 2, 1),
                "walk_left_d": (1, 1, 1, 1, 1, 1)}}


def tuned(kind: str, index: dict) -> dict:
    return {**({"gait": GAIT[kind]} if kind in GAIT else {}), **index.get("tuning", {})}


@lru_cache(maxsize=8)
def load(kind: str = "cat", coat: str = "", outfits: tuple = ()) -> Art:
    """The art for one pet (bongo/pets.py): ``<kind>.json``, the coat's atlas, and the outfits
    she's wearing (collar, Santa hat...: names from ``<kind>.json``; unknown ones are ignored)."""
    index, atlas = _index(kind), sheet(kind, coat)
    crop = lambda r: atlas.crop((r[0], r[1], r[0] + r[2], r[1] + r[3]))
    airborne = set(index.get("airborne", ()))
    animations = {}
    for tag, frames in index["animations"].items():
        raw = [Frame(crop(f), f[4], f[5], f[6]) for f in frames]
        placed = aloft(raw) if tag in airborne else [grounded(tag, i, f) for i, f in enumerate(raw)]
        worn = [o for o in outfits if o in index.get("outfits", {})]
        if worn:
            under = set(index.get("outfit_under", ()))
            placed = [dressed(kind, f, f.dy - r.dy, [(index["outfits"][o].get(c), o in under) for o in worn])
                      for f, r, c in zip(placed, raw, index["cells"][tag])]
        animations[tag] = placed
    if "room_from" in index:  # this animal lives in another one's room (the bunny in the cat's)
        home = load(index["room_from"])
        return Art(animations, home.room, home.empty, home.night, home.ball, tuned(kind, index), home.toys)
    room = crop(index["room"]).convert("RGB")
    return Art(animations, room, empty_bowl(room), night(room), crop(index["ball"]), tuned(kind, index), toys())


@lru_cache(maxsize=1)
def toys() -> dict:
    """The toys (toys.json beside toys.webp.b64), or none if there's no toy art."""
    try:
        index = json.loads(art_file("toys.json").read_text())
    except OSError:
        return {}
    sheet = _atlas("toys")
    return {name: (sheet.crop((r[0], r[1], r[0] + r[2], r[1] + r[3])), r[4], r[5]) for name, r in index.items()}


def dressed(kind: str, frame: Frame, shift: int, pieces: list) -> Frame:
    """``frame`` with accessory pieces ((piece, under): [x, y, w, h, dx, dy] in the outfit atlas,
    drawn behind her if ``under``, else on top) moved by the same ``shift`` the frame got when it
    was set on the floor."""
    pieces = [(p, u) for p, u in pieces if p]
    if not pieces:
        return frame
    sheet = _atlas(f"{kind}-outfits")
    boxes = [(frame.dx, frame.dy, frame.dx + frame.image.width, frame.dy + frame.image.height)]
    boxes += [(p[4], p[5] + shift, p[4] + p[2], p[5] + shift + p[3]) for p, _ in pieces]
    x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
    img = Image.new("RGBA", (x1 - x0, y1 - y0))
    put = lambda p: img.alpha_composite(sheet.crop((p[0], p[1], p[0] + p[2], p[1] + p[3])), (p[4] - x0, p[5] + shift - y0))
    for p, under in pieces:
        if under:
            put(p)
    img.alpha_composite(frame.image, (frame.dx - x0, frame.dy - y0))
    for p, under in pieces:
        if not under:
            put(p)
    return Frame(img, x0, y0, frame.ms)


def aloft(frames: list) -> list:
    """A hop: the frames keep their heights relative to each other (the art lifts her off the
    ground mid-hop), and the whole animation is set so its lowest pose stands on the floor line."""
    lowest = max((f.dy + f.image.getchannel("A").getbbox()[3] - 1) for f in frames if f.image.getbbox())
    return [Frame(f.image, f.dx, f.dy + FLOOR - lowest, f.ms) for f in frames]


def face(kind: str = "cat", coat: str = "") -> Image.Image | None:
    """A small picture of the coat's face for the dashboard's picker (None if the kind has none)."""
    r = _index(kind).get("face")
    if not r:
        return None
    img = sheet(kind, coat).crop((r[0], r[1], r[0] + r[2], r[1] + r[3]))
    box = img.getchannel("A").getbbox()
    return img.crop(box) if box else img


# The source animations don't share a floor line: lying poses floated 1-2 px, the tall standing
# poses sank 2 px and the walk hopped 3 px. Every frame is set so its lowest pixel sits one row
# above the feet point (the standing pose's convention), except the walk's two mid-stride
# frames, which keep a 1 px lift so the gait still has a little bounce.
FLOOR = -1
LIFT = {"walk_left": {2: -2, 6: -2}, "walk_right": {2: -2, 6: -2}}


def grounded(tag: str, i: int, frame: Frame) -> Frame:
    box = frame.image.getchannel("A").getbbox()
    if box is None:
        return frame
    lowest = box[3] - 1  # last opaque row inside the image
    return Frame(frame.image, frame.dx, LIFT.get(tag, {}).get(i, FLOOR) - lowest, frame.ms)


def empty_bowl(room: Image.Image) -> Image.Image:
    img = room.copy()
    px = img.load()
    for y in range(9, 12):
        for x in range(57, 62):
            r, g, b = px[x, y]
            if (r, g, b) != (0x75, 0x64, 0x64) and r < 0xA0:
                px[x, y] = (0x5A, 0x48, 0x48)
    return img


def night(room: Image.Image) -> Image.Image:
    img = room.copy()
    px = img.load()
    for y in range(H):
        for x in range(W):
            r, g, b = px[x, y]
            lum = (r * 3 + g * 6 + b) / 10
            bedlight = max(0.0, 1 - math.hypot(x - 16, (y - 20) * 1.6) / 22)
            k = 0.16 + 0.22 * bedlight
            px[x, y] = (int(lum * k * 0.75), int(lum * k * 0.85), int(lum * k * 1.25 + 6))
    return img


def forget() -> None:
    """Drop every cached atlas and frame (a pack was just imported)."""
    for f in (_index, _atlas, sheet, load, toys):
        f.cache_clear()


def missing(kind: str = "cat") -> bool:
    """No art for ``kind`` on this Matrix (a public build before its owner imports a pack)."""
    return not art_file(f"{kind}.json").exists()


def no_art(width: int, height: int, fonts) -> Image.Image:
    """What a Pet screen shows when there's no art yet: where to get some."""
    img = Image.new("RGB", (width, height))
    small = fonts("tom-thumb")
    for i, (line, colour) in enumerate((("IMPORT YOUR", (255, 181, 71)), ("PET PACK IN", (230, 230, 230)),
                                        ("SETTINGS >", (230, 230, 230)), ("PET", (230, 230, 230)))):
        small.draw(img, max(0, (width - small.width(line)) // 2), 7 + i * 7, line, colour)
    return img
