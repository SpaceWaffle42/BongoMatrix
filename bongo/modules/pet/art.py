"""The pet art: the atlases built by tools/build_house_pets.py, unpacked once per coat.

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

from PIL import Image

from bongo.pets import art_file

W, H = 64, 32


@dataclass(frozen=True)
class Frame:
    image: Image.Image  # RGBA, trimmed to the cat
    dx: int             # offset of the image from the cat's feet
    dy: int
    ms: int


@dataclass(frozen=True, eq=False)
class Art:
    animations: dict    # tag -> [Frame]
    room: Image.Image   # 64x32 RGB, day, bowl full
    empty: Image.Image  # the same with the bowl empty
    night: Image.Image  # dark, lit only around the bed
    ball: Image.Image   # RGBA
    tuning: dict = field(default_factory=dict)  # per-animal scene tweaks (see scenes.py): bowl, ball, reach, zoomies, toy, gait
    toys: dict = field(default_factory=dict)    # name -> (image, dx, dy): placed by the middle of its bottom edge
    pieces: tuple = ()  # what stands on her home's floor: (depth key, x, y, mask, name) (floor_pieces)
    sized: dict = field(default_factory=dict, repr=False)   # (frame id, rows) -> the frame shrunk for depth

    @property
    def ref_height(self) -> int:
        """What depth scales: her standing height (walk_right's first frame) plus one row, i.e. counted from the
        feet point (one row below her lowest pixel) to her top row, as the approved depth test (depth_fine.py)
        counts it. Measured once."""
        if "ref" not in self.sized:
            f = self.animations["walk_right"][0]
            self.sized["ref"] = 1 - (f.dy + f.image.getchannel("A").getbbox()[1])
        return self.sized["ref"]

    def at_depth(self, frame: Frame, rows: int) -> Frame:
        """``frame`` drawn ``rows`` tall instead of ref_height (depth): a plain nearest-neighbour shrink
        resampled through her feet point, so her feet stay planted and the grid never slides across her.
        The same frame at the same height is always the same picture (cached), so nothing flickers."""
        ref = self.ref_height
        if rows >= ref:
            return frame
        key = (id(frame), rows)
        if key not in self.sized:
            self.sized[key] = shrunk(frame, rows / ref)
        return self.sized[key]


@lru_cache(maxsize=16)
def _index(kind: str) -> dict:
    return json.loads(art_file(f"{kind}.json").read_text())


@lru_cache(maxsize=16)
def _atlas(name: str) -> Image.Image:
    # a lossless WebP kept as base64 text (see tools/build_house_pets.py)
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


@lru_cache(maxsize=8)
def sheet(kind: str = "cat", coat: str = "") -> Image.Image:
    """The atlas for one coat (the kind's default for "" or an unknown coat)."""
    index, found = _index(kind), _coat(kind, coat)
    drawing = found.get("drawing", index.get("default"))
    cats = recoloured(_atlas(kind if drawing in (None, index.get("default")) else f"{kind}-{drawing}"), found.get("swap", {}))
    if (drawing in (None, index.get("default")) and not found.get("swap")) or "room" not in index:
        return cats
    shared = _atlas(kind)  # the room and the ball live in the default atlas, never recoloured with her
    for key in ("room", "ball", "empty", "night"):
        if key in index:
            x, y, w, h = index[key]
            cats.paste(shared.crop((x, y, x + w, y + h)), (x, y))
    return cats


@lru_cache(maxsize=16)
def load(kind: str = "cat", coat: str = "", outfits: tuple = ()) -> Art:
    """The art for one pet (bongo/pets.py): ``<kind>.json``, the coat's atlas, and the outfits
    she's wearing (collar, Santa hat...: names from ``<kind>.json``; unknown ones are ignored)."""
    index, atlas = _index(kind), sheet(kind, coat)
    crop = lambda r: atlas.crop((r[0], r[1], r[0] + r[2], r[1] + r[3]))
    airborne = set(index.get("airborne", ()))
    animations = {}
    for tag, frames in index["animations"].items():
        raw = [Frame(crop(f), f[4], f[5], f[6]) for f in frames]
        placed = aloft(raw) if tag in airborne else [grounded(f) for f in raw]
        worn = [o for o in outfits if o in index.get("outfits", {})]
        if worn:
            under = set(index.get("outfit_under", ()))
            placed = [dressed(kind, f, f.dy - r.dy, [(index["outfits"][o].get(c), o in under) for o in worn])
                      for f, r, c in zip(placed, raw, index["cells"][tag])]
        animations[tag] = placed
    room = crop(index["room"]).convert("RGB")
    empty, base = crop(index["empty"]).convert("RGB"), crop(index["night"]).convert("RGB")
    toys = {name: (crop(r), r[4], r[5]) for name, r in index.get("toys", {}).items()}
    return Art(animations, room, empty, lit_night(base, index["night_light"]), crop(index["ball"]),
               index.get("tuning", {}), toys, floor_pieces(index.get("pieces", {})))


def floor_pieces(entries: dict) -> tuple:
    """A home's or a place's pieces as tools/build_house_pets.py writes them (name -> [x, y, w, h, depth key, one
    hex bitmask per row]) -> ((depth key, x, y, mask, name), ...)."""
    out = []
    for name, (x, y, w, h, key, rows) in sorted(entries.items()):
        bits = np.array([[(int(r, 16) >> (w - 1 - i)) & 1 for i in range(w)] for r in rows], np.uint8) * 255
        out.append((key, x, y, Image.fromarray(bits, "L"), name))
    return tuple(out)


def at_size(pic: Image.Image, rows: int, cache: dict) -> Image.Image:
    """``pic`` (a toy, her ball) ``rows`` tall, nearest neighbour, the same width-to-height; never bigger. The same
    picture at the same height is always the same image (``cache``), so nothing flickers."""
    if rows >= pic.height:
        return pic
    key = ("toy", id(pic), rows)
    if key not in cache:
        cache[key] = pic.resize((max(1, round(pic.width * rows / pic.height)), max(1, rows)), Image.Resampling.NEAREST)
    return cache[key]


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


FLOOR = -1   # every frame is set so its lowest pixel sits one row above the feet point


def grounded(frame: Frame) -> Frame:
    box = frame.image.getchannel("A").getbbox()
    if box is None:
        return frame
    return Frame(frame.image, frame.dx, FLOOR - (box[3] - 1), frame.ms)


def shrunk(frame: Frame, s: float) -> Frame:
    """``frame`` scaled by ``s`` (< 1) about her feet point (pixel centres mapped back through it)."""
    src = np.asarray(frame.image)
    h, w = src.shape[:2]
    x0, y0 = math.floor(frame.dx * s) - 1, math.floor(frame.dy * s) - 1
    x1, y1 = math.ceil((frame.dx + w) * s) + 1, math.ceil((frame.dy + h) * s) + 1
    xs = np.rint((np.arange(x0, x1) + 0.5) / s - 0.5).astype(int) - frame.dx   # source column of each target one
    ys = np.rint((np.arange(y0, y1) + 0.5) / s - 0.5).astype(int) - frame.dy
    okx, oky = (xs >= 0) & (xs < w), (ys >= 0) & (ys < h)
    out = np.zeros((y1 - y0, x1 - x0, 4), np.uint8)
    out[np.ix_(oky, okx)] = src[np.ix_(ys[oky], xs[okx])]
    img = Image.fromarray(out, "RGBA")
    box = img.getchannel("A").getbbox()
    if box is None:
        return Frame(img, x0, y0, frame.ms)
    return Frame(img.crop(box), x0 + box[0], y0 + box[1], frame.ms)


def lit_night(room: Image.Image, light: dict) -> Image.Image:
    """Lights out in a pet's own home: night()'s maths with the home's own ambient and bed glow, and warm
    pools round its lamps (``light``: ambient, glow, bed, reach, cool, lights [[x, y, radius, strength, "#rrggbb"]])."""
    a = np.asarray(room.convert("RGB"), float)
    ys, xs = np.mgrid[0:a.shape[0], 0:a.shape[1]].astype(float)
    bx, by = light.get("bed") or (16, 27)
    bed = np.clip(1 - np.hypot(xs - bx, (ys - by) * 1.6) / light.get("reach", 24), 0, 1)
    lum = (a[..., 0] * 3 + a[..., 1] * 6 + a[..., 2]) / 10
    ambient = light.get("ambient", 0.16)
    k = ambient + light.get("glow", 0.22) * bed
    cool = light.get("cool", (0.75, 0.85, 1.25))
    out = np.stack([lum * k * cool[0], lum * k * cool[1], lum * k * cool[2] + 6], axis=-1)
    for lx, ly, r, strength, colour in light.get("lights", ()):
        m = np.clip(1 - np.hypot(xs - lx, (ys - ly) * 1.3) / r, 0, 1) * strength
        lit = a * (ambient + 0.55) * (np.array(list(bytes.fromhex(colour.lstrip("#"))), float) / 255.0)
        out = out + (lit - out) * m[..., None]
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB")


def missing(kind: str = "cat") -> bool:
    """No art for ``kind`` on this Matrix (a checkout with resources/pet missing)."""
    return not art_file(f"{kind}.json").exists()


def no_art(width: int, height: int, fonts) -> Image.Image:
    """What a Pet screen shows without its art: a blank screen."""
    return Image.new("RGB", (width, height))
