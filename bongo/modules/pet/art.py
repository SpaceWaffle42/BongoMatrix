"""The Cat 10 art: the atlas built by tools/build_pet_atlas.py, unpacked once."""
from __future__ import annotations

import base64
import io
import json
import math
from dataclasses import dataclass
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
def load(kind: str = "cat") -> Art:
    """The art for one kind of pet (bongo/pets.py): ``<kind>.json`` and its atlas."""
    index = json.loads((FOLDER / f"{kind}.json").read_text())
    # a lossless WebP kept as base64 text (see tools/build_pet_atlas.py)
    atlas = Image.open(io.BytesIO(base64.b64decode((FOLDER / f"{kind}.webp.b64").read_text()))).convert("RGBA")
    crop = lambda r: atlas.crop((r[0], r[1], r[0] + r[2], r[1] + r[3]))
    animations = {tag: [grounded(tag, i, Frame(crop(f), f[4], f[5], f[6])) for i, f in enumerate(frames)]
                  for tag, frames in index["animations"].items()}
    room = crop(index["room"]).convert("RGB")
    return Art(animations, room, empty_bowl(room), night(room), crop(index["ball"]))


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
