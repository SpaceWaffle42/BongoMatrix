"""Bigger places for the pet: medium (96x48) and large (128x64) rooms she walks about in while the
view follows her (styles.window). The floors and walls are drawn here; the furniture and plants are
Last Tick's props (``props`` and ``plants`` atlases in resources/pet, built by bongo/petpack.py), so
like the pet herself they are only on a Matrix that has that art: without it the rooms aren't offered.

A room is the same thing to the scene engine as a place in places.py (day, night, back, front, zzz),
plus three things of its own: ``size``, a ``stage`` (where its bed, bowl and play floor are:
scenes.Stage) and ``over`` (the furniture in front of her is drawn back over her).
"""
from __future__ import annotations

import json
import math
import random
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
def pieces() -> dict:
    """Every prop and plant on this Matrix by name (none without the art)."""
    out = {}
    for sheet in ("props", "plants"):
        try:
            index = json.loads(art_file(f"{sheet}.json").read_text())
            atlas = art._atlas(sheet)
        except (OSError, ValueError):
            continue
        for name, (x, y, w, h) in index.items():
            out[name] = atlas.crop((x, y, x + w, y + h))
    return out


def put(img, x, y, c, a=1.0):
    if 0 <= x < img.width and 0 <= y < img.height:
        px = img.load()
        px[int(x), int(y)] = mix(px[int(x), int(y)][:3], c, a)


# -- floors and walls (drawn here) -----------------------------------------------------------
def wood(img, top, base=rgb("b98a5a"), plank=6):
    px, rnd = img.load(), random.Random(4)
    for y in range(top, img.height):
        row = (y - top) // plank
        for x in range(img.width):
            k = 1 + rnd.uniform(-0.04, 0.04) + (0.05 if row % 2 else -0.03)
            if (y - top) % plank == plank - 1:
                k *= 0.8                                              # the gap between boards
            elif (x + row * 17) % 29 == 0:
                k *= 0.86                                             # board ends
            px[x, y] = shade(base, k)


def wall(img, rows, colour, stripe, skirt):
    d = ImageDraw.Draw(img)
    for x in range(img.width):
        d.line([(x, 0), (x, rows - 3)], fill=stripe if (x // 4) % 2 else colour)
    d.rectangle([0, rows - 3, img.width, rows - 1], fill=skirt)
    d.line([(0, rows - 3), (img.width, rows - 3)], fill=shade(skirt, 1.3))


def rug(img, box, colour, trim):
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    d.rounded_rectangle(box, 3, fill=colour)
    d.rounded_rectangle([x0 + 2, y0 + 2, x1 - 2, y1 - 2], 2, outline=trim)
    for x in range(x0 + 6, x1 - 4, 6):
        d.point((x, (y0 + y1) // 2), fill=trim)


def window_frame(img, box):
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    d.rectangle([x0 - 1, y0 - 1, x1 + 1, y1 + 1], fill=rgb("f3ead8"))
    d.rectangle(box, fill=rgb("8fc7ea"))


def lawn(img):
    px, rnd = img.load(), random.Random(1)
    for y in range(img.height):
        for x in range(img.width):
            stripe = 1.06 if (x + y // 3) // 8 % 2 else 0.94          # mower stripes
            px[x, y] = shade(rgb("6fa646"), stripe * (1 + rnd.uniform(-0.07, 0.07)))
    for _ in range(img.width * img.height // 40):                      # tufts
        x, y = rnd.randrange(img.width), rnd.randrange(img.height - 1)
        put(img, x, y, rgb("8cc35a"))
        put(img, x, y + 1, rgb("4f8433"))


def stones(img, spots):
    d = ImageDraw.Draw(img)
    for x, y in spots:
        d.ellipse([x - 4, y - 2, x + 4, y + 2], fill=rgb("b9b3a4"))
        d.ellipse([x - 3, y - 2, x + 2, y], fill=rgb("d2ccbd"))


def carpet(img, a, b):
    px, rnd = img.load(), random.Random(7)
    for y in range(img.height):
        for x in range(img.width):
            px[x, y] = shade(a if ((x // 8) + (y // 8)) % 2 else b, 1 + rnd.uniform(-0.05, 0.05))


def sand(img):
    px, rnd = img.load(), random.Random(3)
    for y in range(img.height):
        for x in range(img.width):
            px[x, y] = shade(rgb("d9bb7c"), 1 + rnd.uniform(-0.05, 0.05))
    d, rnd = ImageDraw.Draw(img), random.Random(5)
    for _ in range(img.width * img.height // 200):                     # ripples
        x, y = rnd.randrange(img.width), rnd.randrange(img.height)
        d.line([(x, y), (x + rnd.randint(3, 7), y)], fill=rgb("c7a665"))


def pool(img, box):
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    d.ellipse(box, fill=rgb("b7a26a"))
    d.ellipse([x0 + 2, y0 + 1, x1 - 2, y1 - 2], fill=rgb("3f9fb5"))
    d.ellipse([x0 + 8, y0 + 4, x1 - 14, y1 - 8], fill=rgb("5dc1d3"))


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
    name = blurb = ""
    things: tuple = ()
    bed = bowl = ("", 0, 0)
    patch = (0, 0)
    cool, ambient = (0.72, 0.84, 1.3), 0.36

    @property
    def needs(self) -> set:
        return {t[0] for t in self.things} | {self.bed[0], self.bowl[0], self.bowl[0] + "_full"}

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
        return Stage(w, h, *self.patch, bed=feet, bowl=(cx - 13, cy + 7), beg=(cx - 15, cy + 12))

    def floor(self, img):
        raise NotImplementedError

    def standing(self, full=True):
        """Everything on the floor, back to front: (base row, left, picture)."""
        got = pieces()
        bowl = (self.bowl[0] + ("_full" if full else ""), *self.bowl[1:])
        return sorted(((base, x, got[name]) for name, x, base in (*self.things, self.bed, bowl)), key=lambda t: t[0])

    def day(self, full=True):
        img = Image.new("RGB", self.size)
        self.floor(img)
        img = img.convert("RGBA")
        for base, x, pic in self.standing(full):
            shadow = Image.new("RGBA", img.size)                                 # a soft shadow on the floor under it
            ImageDraw.Draw(shadow).ellipse([x + 1, base - 2, x + pic.width - 2, base + 1], fill=(0, 0, 0, 46))
            img.alpha_composite(shadow)
            img.alpha_composite(pic, (x, base - pic.height + 1))
        return img.convert("RGB")

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

    def over(self, img, room, feet):
        """She walks behind whatever stands in front of her: copy those things back over her from
        ``room`` (the room as it is drawn this frame, without her)."""
        got = pieces()
        for name, x, base in self.things:             # (never her bed or bowl: those lie flat under her)
            pic = got[name]
            if base > feet[1] + 2 and x - 12 < feet[0] < x + pic.width + 12:
                box = (x, base - pic.height + 1, x + pic.width, base + 1)
                img.paste(room.crop(box), box, pic.getchannel("A").point(lambda v: 255 if v > 127 else 0))


class LivingRoom(Room):
    name, blurb = "Living room", "Floorboards, a rug, her little house and a scratching post. Clouds pass the window; stars at night."
    size, patch = MEDIUM, (32, 16)
    things = (("house_1", 1, 30), ("post_8", 72, 36), ("food_2", 50, 19))
    bed, bowl = ("bed_plain", 2, 46), ("bowl_a", 44, 28)
    WINDOW = (36, 1, 47, 6)

    def floor(self, img):
        wood(img, 10)
        wall(img, 10, rgb("d8c7a6"), rgb("cdb994"), rgb("7a5536"))
        window_frame(img, self.WINDOW)
        rug(img, [36, 30, 92, 46], rgb("9c4f4f"), rgb("e6c88a"))

    def back(self, img, t, room):
        x0, y0, x1, y1 = self.WINDOW
        d = ImageDraw.Draw(img)
        if room == "night":
            d.rectangle(self.WINDOW, fill=(10, 14, 40))
            for k, (sx, sy) in enumerate(((2, 1), (7, 3), (10, 0), (4, 4))):
                put(img, x0 + sx, y0 + sy, (230, 230, 255), 0.35 + 0.65 * (0.5 + 0.5 * math.sin(t / 700 + k * 1.9)))
        else:
            d.rectangle(self.WINDOW, fill=rgb("8fc7ea"))
            cx = x0 - 6 + int(t / 900) % (x1 - x0 + 12)                          # a cloud drifts by
            for dx, dy in ((0, 0), (1, 0), (2, 0), (3, 0), (1, -1), (2, -1)):
                if x0 <= cx + dx <= x1:
                    put(img, cx + dx, y0 + 3 + dy, (250, 250, 250))


class GardenRoom(Room):
    name, blurb = "Big garden", "A lawn with shrubs, flower beds and stepping stones. Butterflies by day, fireflies at night."
    size, patch = MEDIUM, (32, 16)
    cool, ambient = (0.6, 0.85, 1.25), 0.42
    things = (("shrubs_10", 0, 22), ("shrubs_12", 22, 21), ("shrubs_11", 72, 22), ("bonsai_3", 46, 24),
              ("grasses_4", 2, 30), ("flowers_12", 80, 31), ("flowers_8", 16, 30))
    bed, bowl = ("bed_basket", 2, 46), ("bowl_b", 60, 30)

    def floor(self, img):
        lawn(img)
        stones(img, ((38, 34), (33, 39), (28, 33)))

    def front(self, img, t, night):
        if night:
            fireflies(img, t, [(10, 8), (40, 12), (66, 6), (86, 30), (52, 36), (24, 40), (76, 44)])
        else:
            butterfly(img, t, 56, 7, 26, rgb("8fd0ff"))


class Playroom(Room):
    name, blurb = "Playroom", "Carpet tiles, two tall cat trees to walk behind, a little house and her carrier. Fairy lights twinkle on the wall."
    size, patch = LARGE, (32, 13)
    things = (("tree_3", 0, 63), ("tree_5", 94, 63), ("carrier_1", 38, 28), ("house_4", 73, 29), ("food_5", 20, 21))
    bed, bowl = ("bed_round", 36, 62), ("bowl_c", 74, 58)
    LIGHTS = (rgb("ffd166"), rgb("ef6f6c"), rgb("7bdff2"), rgb("b8f28a"))

    def floor(self, img):
        carpet(img, rgb("58708f"), rgb("4f6684"))
        wall(img, 11, rgb("e3d4c2"), rgb("d9c8b3"), rgb("5b4a3f"))
        rug(img, [40, 32, 90, 46], rgb("c9836a"), rgb("f0d9a8"))

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
    name, blurb = "Oasis", "Sand, palms and cacti round a pool that glints in the sun. Fireflies come out at night."
    size, patch = LARGE, (64, 30)
    zzz = DEEP_ZZZ
    cool, ambient = (0.66, 0.8, 1.3), 0.3
    things = (("palms_30", 26, 24), ("palms_40", 98, 30), ("palms_44", 40, 20), ("palms_36", 70, 15),
              ("grasses_2", 46, 30), ("grasses_3", 2, 26), ("cacti_20", 38, 46), ("cacti_13", 0, 44),
              ("cacti_27", 58, 47), ("cacti_24", 44, 50), ("palms_33", 20, 40))
    bed, bowl = ("bed_blue", 6, 62), ("bowl_d", 104, 46)
    POOL = (52, 18, 94, 38)

    def floor(self, img):
        sand(img)
        pool(img, self.POOL)

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


def available() -> list[str]:
    """The rooms this Matrix has every piece for."""
    got = pieces()
    return [key for key, room in ROOMS.items() if room.needs <= set(got)]


@lru_cache(maxsize=None)
def images(key: str) -> tuple:
    """(day, empty bowl, night) for a room, drawn once."""
    room = ROOMS[key]
    return room.day(True), room.day(False), room.night()


def forget() -> None:
    """A pack was just imported: look at the art again."""
    pieces.cache_clear()
    images.cache_clear()
