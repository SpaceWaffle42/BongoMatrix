"""Bigger places for the pet: medium (96x48) and large (128x64) rooms she walks about in while the
view follows her (styles.window). Our own house-style art (tools/build_house_pets.py: ``rooms/rooms.json`` beside
``rooms/rooms.webp.b64``): each room's floor and walls (with the soft shadows under what stands there) and its
props and plants, placed here by their left edge and the floor row they stand on. Every Matrix has them.

A room is the same thing to the scene engine as a place in places.py (day, night, back, front, zzz),
plus three things of its own: ``size``, a ``stage`` (where its bed, bowl and play floor are:
scenes.Stage) and ``pieces`` (what stands on the floor, each with its depth key: styles draws her, her toys and
these back to front).
"""
from __future__ import annotations

import json
import math
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw

from bongo.pets import art_file

from . import art
from .places import DEEP_ZZZ, ZZZ, mix, rgb, shade
from .scenes import Stage

MEDIUM, LARGE = (96, 48), (128, 64)
SIZE_NAMES = {MEDIUM: "medium", LARGE: "large"}


@lru_cache(maxsize=1)
def sheet() -> tuple[dict, dict, dict]:
    """(floors by room, pieces by name, each piece's depth key row in its own picture)."""
    index, atlas = json.loads(art_file("rooms/rooms.json").read_text()), art._atlas("rooms/rooms")
    crop = lambda r: atlas.crop((r[0], r[1], r[0] + r[2], r[1] + r[3]))   # noqa: E731
    return ({k: crop(r).convert("RGB") for k, r in index["floors"].items()},
            {k: crop(r) for k, r in index["pieces"].items()}, index["ground"])


def pieces() -> dict:
    """Every prop and plant by name."""
    return sheet()[1]


def put(img, x, y, c, a=1.0):
    if 0 <= x < img.width and 0 <= y < img.height:
        px = img.load()
        px[int(x), int(y)] = mix(px[int(x), int(y)][:3], c, a)


def fireflies(img, t, spots, colour=rgb("e8ff7a"), halo=rgb("9cd04a")):
    for k, (cx, cy) in enumerate(spots):
        x = cx + 8 * math.sin(t / 2300 + k * 1.7)
        y = cy + 4 * math.sin(t / 1700 + k * 2.3)
        pulse = (0.5 + 0.5 * math.sin(t / 900 + k * 2.1)) ** 2
        put(img, x, y, colour, pulse)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            put(img, x + dx, y + dy, halo, pulse * 0.35)


def butterfly(img, t, cx, cy, reach, colour, phase=0.0):
    x = cx + reach * math.sin(t / 2600 + phase)
    y = cy + 3 * math.sin(t / 1300 + phase * 2)
    put(img, x, y, rgb("3a2a1a"))
    put(img, x, y - 1, rgb("3a2a1a"))
    for dx in (-1, 1):                                # wings either side of her body: open, then folded up
        put(img, x + dx, y - 1, colour)
        if int(t / 120) % 2:
            put(img, x + dx, y, colour)
            put(img, x + 2 * dx, y - 1, colour)


class Room:
    """One bigger place. ``things``: (piece, left, base row) standing on the floor; ``bed`` and
    ``bowl`` the same for hers. ``patch``: the top-left corner of the 64x32 patch of floor the
    scenes play on (scenes.Stage): its right edge is what her ball bounces off."""
    zzz = ZZZ
    size = MEDIUM
    key = name = blurb = ""
    things: tuple = ()
    bed = bowl = ("", 0, 0)
    patch = (0, 0)
    cool, ambient = (0.72, 0.84, 1.3), 0.36
    roams = False            # Life here is free-roaming (roam.py) rather than the scenes in turn
    floor_top = 22           # the highest row her feet go on the floor (above it: the wall, the back row)
    perches: dict = {}       # name -> (piece she sits on, her feet there, the perch to jump from: "" the floor, floor spot)
    posts: tuple = ()        # floor spots where she stands to scratch the post on her right

    @property
    def label(self) -> str:
        return f"{self.name} ({SIZE_NAMES[self.size]})"

    @property
    def stage(self) -> Stage:
        w, h = self.size
        got = pieces()
        bed, bowl = got[self.bed[0]], got[self.bowl[0]]
        feet = (self.bed[1] + bed.width // 2, self.bed[2] - 2)                   # curled up on the cushion
        cx, cy = self.bowl[1] + bowl.width // 2, self.bowl[2] - bowl.height // 2
        return Stage(w, h, *self.patch, bed=feet, bowl=(cx - 13, cy + 7), beg=(cx - 15, cy + 12),
                     front=False)   # its own floor: the furniture's layout, as drawn

    def standing(self, full=True):
        """Everything on the floor, back to front: (base row, left, picture)."""
        got = pieces()
        bowl = (self.bowl[0] + ("_full" if full else ""), *self.bowl[1:])
        return sorted(((base, x, got[name]) for name, x, base in (*self.things, self.bed, bowl)), key=lambda t: t[0])

    def day(self, full=True):
        img = sheet()[0][self.key].convert("RGBA")
        for base, x, pic in self.standing(full):
            img.alpha_composite(pic, (x, base - pic.height + 1))
        return img.convert("RGB")

    def floor_pieces(self, full=True) -> tuple:
        """What stands on the floor as drawn (her bowl full or empty): (depth key, x, y, mask, name), the key from
        the art's ground rows (her bed and bowl lie flat: their top row). Her bowl is named as in ``bowl``."""
        got, rows = pieces(), sheet()[2]
        bowl = (self.bowl[0] + "_full" * full, *self.bowl[1:])
        return tuple((base - got[n].height + 1 + rows[n], x, base - got[n].height + 1, got[n].getchannel("A"),
                      self.bowl[0] if n == bowl[0] else n)
                     for n, x, base in (*self.things, self.bed, bowl) if rows.get(n) is not None)

    @property
    def pieces(self) -> tuple:
        return self.floor_pieces(True)

    def ground(self, name: str) -> float | None:
        """The floor row she counts as standing on up on piece ``name`` (one below its depth key: as at its foot)."""
        return next((key + 1 for key, *_, n in self.pieces if n == name), None)

    def night(self):
        """Lights out: dark and cool, lit softly around her bed."""
        a = np.asarray(self.day(True), float)
        h, w = a.shape[:2]
        ys, xs = np.mgrid[0:h, 0:w].astype(float)
        bx, by = self.stage.bed
        glow = np.clip(1 - np.hypot(xs - bx, (ys - by + 8) * 1.6) / 34, 0, 1)
        lum = (a[..., 0] * 3 + a[..., 1] * 6 + a[..., 2]) / 10 * (self.ambient + 0.3 * glow)
        out = np.stack([lum * self.cool[0], lum * self.cool[1], lum * self.cool[2] + 6], axis=-1)
        return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))

    def back(self, img, t, room):
        pass

    def front(self, img, t, night):
        pass

    def blocked(self, x, y) -> bool:
        """Her feet can't be here: off the floor, or where furniture (or her bowl) stands."""
        w, h = self.size
        if not (6 <= x <= w - 6 and self.floor_top <= y <= h - 3):
            return True
        got = pieces()
        for name, left, base in (*self.things, self.bowl):
            deep = 12 if name == self.bowl[0] else 7       # how far back from its front edge it stands
            side = 2 if name == self.bowl[0] else 5       # (she stands right beside her bowl to eat)
            if left - side <= x <= left + got[name].width + side - 1 and base - deep <= y <= base + 2:
                return True
        return False

    def hidden(self, x, y) -> bool:
        """Standing here she'd be out of sight behind something tall, or lying in her bed."""
        got = pieces()
        for name, left, base in self.things:
            pic = got[name]
            if base > y + 2 and left - 3 <= x <= left + pic.width + 2 and y > base - pic.height:
                return True
        name, left, base = self.bed
        return left - 3 <= x <= left + got[name].width + 2 and base - got[name].height + 2 <= y <= base + 1

    def clear(self, a, b) -> bool:
        """A straight walk from a to b crosses nothing."""
        n = max(1, int(max(abs(b[0] - a[0]), abs(b[1] - a[1])) // 2))
        return not any(self.blocked(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(1, n))

    def route(self, a, b) -> list:
        """The corners of a walk from a to b round the furniture (b alone if the way is clear; a
        spot she can't stand on is left by, or reached by, the straight line)."""
        if self.clear(a, b):
            return [b]
        step = 4
        cell = lambda p: (int(round(p[0] / step)), int(round(p[1] / step)))   # noqa: E731
        start, goal = cell(a), cell(b)
        seen, queue = {start: None}, [start]
        while queue and goal not in seen:
            cx, cy = queue.pop(0)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                nxt = (cx + dx, cy + dy)
                if nxt not in seen and (nxt == goal or not self.blocked(nxt[0] * step, nxt[1] * step)):
                    seen[nxt] = (cx, cy)
                    queue.append(nxt)
        if goal not in seen:
            return [b]
        cells, at = [], goal
        while at is not None:
            cells.append((at[0] * step, at[1] * step))
            at = seen[at]
        cells = [a, *cells[::-1][1:-1], b]
        out, i = [], 0
        while i < len(cells) - 1:                      # keep only the corners: the farthest spot in a clear line
            j = len(cells) - 1
            while j > i + 1 and not self.clear(cells[i], cells[j]):
                j -= 1
            out.append(cells[j])
            i = j
        return out


class LivingRoom(Room):
    key = "living_room"
    name, blurb = "Living room", "Floorboards, a rug, her little house and a scratching post. Clouds pass the window; stars at night."
    size, patch = MEDIUM, (32, 16)
    things = (("house_1", 1, 30), ("post_8", 72, 36), ("food_2", 50, 19))
    bed, bowl = ("bed_plain", 2, 46), ("bowl_a", 44, 28)
    WINDOW = (36, 1, 47, 6)


    def back(self, img, t, room):
        x0, y0, x1, y1 = self.WINDOW
        d = ImageDraw.Draw(img)
        if room == "night":
            d.rectangle(self.WINDOW, fill=rgb("1c2648"))
            for k, (sx, sy) in enumerate(((2, 1), (7, 3), (10, 0), (4, 4))):
                put(img, x0 + sx, y0 + sy, (230, 230, 255), 0.35 + 0.65 * (0.5 + 0.5 * math.sin(t / 700 + k * 1.9)))
        else:
            d.rectangle(self.WINDOW, fill=rgb("8cc0e0"))
            cx = x0 - 6 + int(t / 900) % (x1 - x0 + 12)                          # a cloud drifts by
            for dx, dy in ((0, 0), (1, 0), (2, 0), (3, 0), (1, -1), (2, -1)):
                if x0 <= cx + dx <= x1:
                    put(img, cx + dx, y0 + 3 + dy, (250, 250, 250))


class GardenRoom(Room):
    key = "big_garden"
    name, blurb = "Big garden", "A lawn with shrubs, flower beds and stepping stones. Butterflies by day, fireflies at night."
    size, patch = MEDIUM, (32, 16)
    cool, ambient = (0.6, 0.85, 1.25), 0.42
    things = (("shrubs_10", 0, 22), ("shrubs_12", 22, 21), ("shrubs_11", 72, 22), ("bonsai_3", 46, 24),
              ("grasses_4", 2, 30), ("flowers_12", 80, 31), ("flowers_8", 16, 30))
    bed, bowl = ("bed_basket", 2, 46), ("bowl_b", 60, 30)


    def front(self, img, t, night):
        if night:
            fireflies(img, t, [(10, 8), (40, 12), (66, 6), (86, 30), (52, 36), (24, 40), (76, 44)])
        else:
            butterfly(img, t, 56, 7, 26, rgb("8fd0ff"))


class Playroom(Room):
    key = "playroom"
    name, blurb = "Playroom", "Carpet tiles, two tall cat trees to walk behind, a little house and her carrier. Fairy lights twinkle on the wall."
    size, patch = LARGE, (32, 13)
    things = (("tree_3", 0, 63), ("tree_5", 94, 63), ("carrier_1", 38, 28), ("house_4", 73, 29), ("food_5", 20, 21))
    bed, bowl = ("bed_round", 36, 62), ("bowl_c", 80, 58)
    roams, floor_top = True, 32
    perches = {"box": ("tree_3", (29, 41), "", (46, 60)), "ledge": ("tree_3", (9, 24), "box", None)}
    LIGHTS = (rgb("ffd166"), rgb("ef6f6c"), rgb("7bdff2"), rgb("b8f28a"))


    def back(self, img, t, room):
        for k, x in enumerate(range(3, self.size[0], 7)):                       # a string of lights along the wall
            y = 2 + (1 if k % 2 else 0)
            on = 0.5 + 0.5 * math.sin(t / 600 + k * 2.4)
            c = self.LIGHTS[k % 4]
            put(img, x, y, c if room == "night" else shade(c, 0.8), 0.45 + 0.55 * on)
            if room == "night":
                for dx, dy in ((1, 0), (-1, 0), (0, 1)):
                    put(img, x + dx, y + dy, c, 0.22 * on)


class Oasis(Room):
    key = "oasis"
    name, blurb = "Oasis", "Sand, palms and cacti round a pool that glints in the sun. Fireflies come out at night."
    size, patch = LARGE, (64, 30)
    zzz = DEEP_ZZZ
    cool, ambient = (0.66, 0.8, 1.3), 0.3
    things = (("palms_30", 26, 24), ("palms_40", 98, 30), ("palms_44", 40, 20), ("palms_36", 70, 15),
              ("grasses_2", 46, 30), ("grasses_3", 2, 26), ("cacti_20", 38, 46), ("cacti_13", 0, 44),
              ("cacti_27", 58, 47), ("cacti_24", 44, 50), ("palms_33", 20, 40))
    bed, bowl = ("bed_blue", 6, 62), ("bowl_d", 104, 46)
    POOL = (52, 18, 94, 38)


    def back(self, img, t, room):
        x0, y0, x1, y1 = self.POOL
        for k in range(5):                                                       # glints wander over the water
            x = (x0 + x1) / 2 + (x1 - x0) * 0.3 * math.sin(t / 2100 + k * 1.3)
            y = (y0 + y1) / 2 - 1 + (y1 - y0) * 0.22 * math.sin(t / 1500 + k * 2.1)
            c = (120, 150, 200) if room == "night" else (214, 244, 250)
            a = (0.5 + 0.5 * math.sin(t / 500 + k * 1.7)) * (0.5 if room == "night" else 0.9)
            put(img, x, y, c, a)
            put(img, x + 1, y, c, a * 0.6)

    def front(self, img, t, night):
        if night:
            fireflies(img, t, [(14, 12), (44, 44), (72, 10), (100, 50), (116, 22), (60, 56)])


ROOMS = {"living_room": LivingRoom(), "big_garden": GardenRoom(), "playroom": Playroom(), "oasis": Oasis()}


@lru_cache(maxsize=None)
def images(key: str) -> tuple:
    """(day, empty bowl, night) for a room, drawn once."""
    room = ROOMS[key]
    return room.day(True), room.day(False), room.night()
