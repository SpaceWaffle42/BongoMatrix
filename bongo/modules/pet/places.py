"""Places the pet can live, besides her home: eight rooms and outdoor scenes, our own art in the house
style (drawn in the Animator, built into resources/pet/places), so they ship in every build.

Each place is a 64x32 room in the front-floor layout every home has: the back wall (or sky) at the
top, the floor below with her bed bottom-left (sleep spot (14, 29)) and her bowl front right (x 52-61,
rows 25-30; she eats at (43, 30)), the ball's home in the middle at (32, 28), and nothing in her paths.
Each gives:
    day(full) / night()          the room by day (bowl full or empty) and with the lights out
    back(img, t, room)           things that move on the floor, under her (waves, steam, embers)
    front(img, t, night)         things that move over her (snow, fireflies, leaves, a butterfly)
``zzz`` is the colour of her daytime Zs there (night ones keep their own), so they show on any floor.
``t`` is a continuous clock in ms, so nothing snaps when a scene loop starts again; ``room`` is
"day", "empty" or "night". Rooms are drawn once per place and kept (``rooms``)."""
from __future__ import annotations

import math
import random
from functools import lru_cache

import numpy as np

from PIL import Image

W, H = 64, 32


def rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, k):
    return tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))


def shade(c, k):
    return tuple(max(0, min(255, int(v * k))) for v in c)


def put(img, x, y, c, a=1.0):
    if 0 <= x < W and 0 <= y < H:
        px = img.load()
        old = px[int(x), int(y)][:3]
        px[int(x), int(y)] = mix(old, c, a)



def glow(img, mask, colour, strength):
    """Wash ``colour`` over ``img`` in place, by ``mask`` (0..1 per pixel) x ``strength``."""
    a = np.asarray(img, float)
    k = (mask * strength)[..., None]
    img.paste(Image.fromarray((a + (np.array(colour, float) - a) * k).astype(np.uint8)))


def fireflies(img, t, spots, colour=rgb("e8ff7a"), halo=rgb("9cd04a")):
    for k, (cx, cy) in enumerate(spots):
        x = cx + 8 * math.sin(t / 2300 + k * 1.7)
        y = cy + 4 * math.sin(t / 1700 + k * 2.3)
        pulse = (0.5 + 0.5 * math.sin(t / 900 + k * 2.1)) ** 2
        put(img, x, y, colour, pulse)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            put(img, x + dx, y + dy, halo, pulse * 0.35)


ZZZ = (200, 210, 255)   # the house's daytime Zs
DEEP_ZZZ = (60, 80, 170)


class Place:
    """One of the places: its still picture is our own art in the house style (rooms3, built into
    resources/pet/places by tools/build_house_pets.py: day, bowl empty, and the night base with its night sky)
    in the front-floor layout of every pet's home (bed bottom-left, bowl front right). Only the moving
    things are drawn here, over it."""
    key = ""
    zzz = ZZZ
    light = {"ambient": 0.40, "glow": 0.30, "bed": [14, 28]}   # lights out: as the homes, the glow round her bed

    def day(self, full=True):
        return _picture(self.key, "day" if full else "empty")

    def night(self):
        from .art import lit_night
        return lit_night(_picture(self.key, "night"), self.light)

    @property
    def pieces(self) -> tuple:
        """What stands on its floor: (depth key, x, y, mask, name) (art.floor_pieces)."""
        from .art import floor_pieces
        return floor_pieces(_sheet()[0][self.key].get("pieces", {}))

    def back(self, img, t, room):
        pass

    def front(self, img, t, night):
        pass


@lru_cache(maxsize=None)
def _sheet():
    from bongo.pets import art_file
    from .art import _atlas
    import json
    return json.loads(art_file("places/places.json").read_text()), _atlas("places/places")


def _picture(key, which):
    index, atlas = _sheet()
    x, y, w, h = index[key][which]
    return atlas.crop((x, y, x + w, y + h)).convert("RGB")


# -- 1. Garden ------------------------------------------------------------------------------
class Garden(Place):
    key = "garden"
    name, blurb = "Garden", "A brick wall with ivy, shrubs and a flower border, a wicker basket and a terracotta dish. Butterflies by day, fireflies at night."

    def front(self, img, t, night):
        if night:
            fireflies(img, t, [(8, 8), (30, 6), (46, 10), (56, 22), (38, 18), (24, 14)])
        else:
            x = 34 + 22 * math.sin(t / 2600)                            # a butterfly flutters over the lawn
            y = 18 + 5 * math.sin(t / 1300)
            flap = int(t / 120) % 2
            c = rgb("8fd0ff")                                          # cool, so it never reads as her ginger
            put(img, x, y, rgb("3a2a1a"))
            if flap:
                put(img, x - 1, y - 1, c); put(img, x + 1, y - 1, c); put(img, x - 1, y, c); put(img, x + 1, y, c)
            else:
                put(img, x - 1, y, c); put(img, x + 1, y, c)


_SEA = [mix(rgb("2f8fbf"), rgb("54b7d6"), y / 8) for y in range(10)]
_SEA_NIGHT = [shade(c, 0.35) for c in _SEA]
_FOAM, _FOAM_NIGHT, _GLINT = rgb("f4fbfb"), (90, 110, 140), rgb("bfe9f2")


# -- 2. Beach -------------------------------------------------------------------------------
class Beach(Place):
    key = "beach"
    zzz = DEEP_ZZZ
    name, blurb = "Beach", "Warm sand, a striped towel, a coconut-shell bowl, a sandcastle under an umbrella, and the tide washing in at the back."

    def sea_edge(self, t):
        return 6.0 + 1.0 * math.sin(t / 1400)

    def back(self, img, t, room):
        night = room == "night"
        px = img.load()
        edge = self.sea_edge(t)
        foam = _FOAM_NIGHT if night else _FOAM
        for x in range(W):
            e = edge + 0.6 * math.sin(x / 5 + t / 700)
            for y in range(0, min(9, int(e) + 2)):          # the sea is rows 0-6, the foam and wet sand rows 6-8
                if y < e - 1:
                    c = _SEA_NIGHT[y] if night else _SEA[y]
                    sparkle = math.sin(x * 12.9898 + y * 78.233) * 43758.5453 % 1   # fixed spots that twinkle
                    if not night and sparkle > 0.93 and math.sin(t / 500 + sparkle * 40) > 0.6:
                        c = _GLINT                                           # glints
                    px[x, y] = c
                elif y < e + 1:
                    px[x, y] = foam                                           # the foam line
                else:
                    px[x, y] = shade(px[x, y], 0.9)                           # wet sand
        if night:                                                             # the moon on the water
            for k, (x, y) in enumerate(((47, 1), (49, 2), (51, 1))):
                put(img, x, y, rgb("e9f0ff"), 0.25 + 0.35 * (0.5 + 0.5 * math.sin(t / 700 + k * 2)))


# -- 3. Snow day ----------------------------------------------------------------------------
class Snow(Place):
    key = "snow"
    zzz = DEEP_ZZZ
    name, blurb = "Snow day", "Fresh snow with paw prints, a snowman and pines, a knitted blanket and a steaming bowl, with snow falling."

    def back(self, img, t, room):
        if room == "day":                                                      # steam off a full bowl
            for k in range(3):
                p = ((t / 1100) + k / 3) % 1
                x, y = 56.5 + math.sin(p * 6 + k) * 1.5, 24 - p * 5
                a = 0.7 * math.sin(math.pi * p)                                   # fades in, then out
                put(img, x, y, rgb("f4f8ff"), a)
                put(img, x, y - 1, rgb("ffffff"), a * 0.6)

    def front(self, img, t, night):
        rnd = random.Random(9)
        for k in range(26):
            x0, speed, sway = rnd.uniform(0, W), rnd.uniform(4, 9), rnd.uniform(0, 6.28)
            y = (t / 1000 * speed + k * 7.3) % (H + 4) - 2
            x = (x0 + 2 * math.sin(t / 900 + sway)) % W
            put(img, x, y, rgb("ffffff") if not night else rgb("b9c6e6"), 0.9)


# -- 4. Space station -----------------------------------------------------------------------
class Space(Place):
    key = "space"
    name, blurb = "Space station", "Deck plates, a console with blinking lights, a glowing sleep pod, a nutrient bowl, and stars sliding past the long window."

    def back(self, img, t, room):
        night = room == "night"
        rnd = random.Random(5)
        for k in range(14):                                                   # stars slide past the window
            x = 15 + (rnd.uniform(0, 30) - t / 1000 * rnd.uniform(1.5, 4)) % 30
            y = 2 + rnd.randrange(6)
            put(img, x, y, rgb("ffffff"), rnd.uniform(0.4, 1) * (0.55 if night else 1))
        for k, ((x, y), c) in enumerate((((4, 4), "4bff7a"), ((4, 7), "ff4b4b"), ((8, 4), "ffc23a"))):
            on = (int(t / 1800) + k) % 2                                       # status lights blink, slowly
            put(img, x, y, rgb(c), (1 if on else 0.2) * (0.4 if night else 1))
        pulse = 0.5 + 0.5 * math.sin(t / 700)                                 # the pod's strip breathes
        for x in range(5, 28):
            put(img, x, 27, rgb("9ff6ff"), 0.45 * pulse)


# -- 5. Library -----------------------------------------------------------------------------
_YS, _XS = np.mgrid[0:H, 0:W].astype(float)
_FIRE = np.clip(1 - np.hypot(_XS - 52, _YS - 12) / 34, 0, 1)          # the library hearth's light
_LAMP = np.clip(1 - np.hypot(_XS - 57, (_YS - 11) * 1.2) / 30, 0, 1)  # the tatami lamp's light
_SHADE = np.clip(1 - np.hypot(_XS - 57, (_YS - 12) * 0.7) / 4.5, 0, 1)   # the andon's paper, lit


class Library(Place):
    key = "library"
    name, blurb = "Library", "Bookshelves, a patterned rug, a velvet cushion and a brass bowl by the hearth. At night the firelight flickers across the floor."

    def back(self, img, t, room):
        night = room == "night"
        flick = 0.8 + 0.2 * math.sin(t / 90) * math.sin(t / 233)
        for k, (x, y) in enumerate(((49, 11), (52, 10), (55, 11))):            # embers in the hearth
            put(img, x, y, rgb("ffd27a") if (k + int(t / 300)) % 3 == 0 else rgb("ff8a2a"), 0.6 + 0.4 * flick)
            for dx, dy in ((-1, 0), (0, -1), (0, 1)):                            # a soft glow round each
                put(img, x + dx, y + dy, rgb("ff8a2a"), 0.3 * flick)
        if night:                                                              # firelight flickers over the floor
            glow(img, _FIRE, rgb("ff8a2a"), 0.22 * flick)


# -- 6. Tatami ------------------------------------------------------------------------------
class Tatami(Place):
    key = "tatami"
    name, blurb = "Tatami room", "Woven tatami mats behind shoji, a scroll and a bonsai, an indigo zabuton, a ceramic bowl, and a paper lamp that glows at night."

    def back(self, img, t, room):
        if room == "night":                                                     # the lamp glows over the room
            flick = 0.9 + 0.1 * math.sin(t / 170) * math.sin(t / 61)
            glow(img, _LAMP, rgb("ffb35c"), 0.45 * flick)
            glow(img, _SHADE, rgb("ffe3a8"), 0.8 * flick)                        # the lamp itself, lit


# -- 7. Forest ------------------------------------------------------------------------------
class Forest(Place):
    key = "forest"
    name, blurb = "Forest floor", "Tree trunks and ferns, toadstools by a log, a leaf nest and an acorn-cup bowl. Leaves drift down by day; green fireflies at night."

    def front(self, img, t, night):
        if night:                                                                   # fewer, greener fireflies
            fireflies(img, t, [(12, 8), (40, 10), (54, 20)], colour=rgb("7aff9a"), halo=rgb("3a9a5a"))
        else:
            rnd = random.Random(11)
            for k in range(3):                                                      # leaves drift down
                x0 = rnd.uniform(6, W - 6)
                p = (t / 7000 + k / 3) % 1
                x = x0 + 5 * math.sin(p * 9 + k)
                y = p * (H + 4) - 2
                c = rgb(("c9d24a", "9a3a1a", "c9d24a")[k])                  # away from her ginger
                for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
                    put(img, x + dx, y + dy, c if (dx + dy) % 2 == 0 else shade(c, 0.8))


# -- 8. Moon base ---------------------------------------------------------------------------
class Moon(Place):
    key = "moon"
    name, blurb = "The Moon", "Grey dust and craters, Earth hanging in the black, a pennant, a crater bed with a quilt and a tin bowl."

    def back(self, img, t, room):
        rnd = random.Random(13)
        for k in range(8):                                                          # stars twinkle in the black
            x, y = rnd.randrange(W), rnd.randrange(7)
            if 16 <= x <= 26:
                continue                                                            # (Earth is there)
            put(img, x, y, rgb("ffffff"), 0.4 + 0.6 * (0.5 + 0.5 * math.sin(t / 400 + k * 1.3)))


PLACES = {"garden": Garden(), "beach": Beach(), "snow": Snow(), "space": Space(), "library": Library(),
          "tatami": Tatami(), "forest": Forest(), "moon": Moon()}
from . import rooms as _rooms   # noqa: E402  (it uses the helpers above)

BIG = _rooms.ROOMS   # the bigger rooms (rooms.py)
CHOICES = ["house", *PLACES, *BIG]
LABELS = {"house": "Home", **{k: v.name for k, v in PLACES.items()}, **{k: v.label for k, v in BIG.items()}}


def get(place: str):
    """The place by its setting (None: the house)."""
    return PLACES.get(place) or BIG.get(place)


@lru_cache(maxsize=None)
def rooms(place: str) -> tuple:
    """(day, empty bowl, night) for a place, drawn once."""
    if place in BIG:
        return _rooms.images(place)
    p = PLACES[place]
    return p.day(True), p.day(False), p.night()
