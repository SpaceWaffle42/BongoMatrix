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


@lru_cache(maxsize=1)
def load() -> Art:
    index = json.loads((FOLDER / "cat.json").read_text())
    # a lossless WebP kept as base64 text (see tools/build_pet_atlas.py)
    atlas = Image.open(io.BytesIO(base64.b64decode((FOLDER / "cat.webp.b64").read_text()))).convert("RGBA")
    crop = lambda r: atlas.crop((r[0], r[1], r[0] + r[2], r[1] + r[3]))
    animations = {tag: [Frame(crop(f), f[4], f[5], f[6]) for f in frames]
                  for tag, frames in index["animations"].items()}
    room = crop(index["room"]).convert("RGB")
    return Art(animations, room, empty_bowl(room), night(room), crop(index["ball"]))


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
