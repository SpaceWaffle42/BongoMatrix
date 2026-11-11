"""The pet art: the atlases built by tools/build_pet_atlas.py, unpacked once per coat.

``<kind>.json`` holds one set of frame rectangles that fits every coat. Each coat names the
drawing it uses (``<kind>.webp.b64`` for the default one, which also holds the room and the ball,
or ``<kind>-<drawing>.webp.b64``) and, when it's a recolouring of that drawing, a colour swap."""
from __future__ import annotations

import base64
import io
import json
import math
from dataclasses import dataclass

import numpy as np
from functools import lru_cache
from pathlib import Path

from PIL import Image

FOLDER = Path(__file__).resolve().parents[3] / "resources" / "pet"
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


@lru_cache(maxsize=4)
def _index(kind: str) -> dict:
    return json.loads((FOLDER / f"{kind}.json").read_text())


@lru_cache(maxsize=8)
def _atlas(name: str) -> Image.Image:
    # a lossless WebP kept as base64 text (see tools/build_pet_atlas.py)
    return Image.open(io.BytesIO(base64.b64decode((FOLDER / f"{name}.webp.b64").read_text()))).convert("RGBA")


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
    if drawing in (None, index.get("default")):
        return cats
    shared = _atlas(kind)  # the room and the ball live in the default atlas
    for key in ("room", "ball"):
        x, y, w, h = index[key]
        cats.paste(shared.crop((x, y, x + w, y + h)), (x, y))
    return cats


@lru_cache(maxsize=4)
def load(kind: str = "cat", coat: str = "") -> Art:
    """The art for one pet (bongo/pets.py): ``<kind>.json`` and the coat's atlas."""
    index, atlas = _index(kind), sheet(kind, coat)
    crop = lambda r: atlas.crop((r[0], r[1], r[0] + r[2], r[1] + r[3]))
    animations = {tag: [grounded(tag, i, Frame(crop(f), f[4], f[5], f[6])) for i, f in enumerate(frames)]
                  for tag, frames in index["animations"].items()}
    room = crop(index["room"]).convert("RGB")
    return Art(animations, room, empty_bowl(room), night(room), crop(index["ball"]))


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
