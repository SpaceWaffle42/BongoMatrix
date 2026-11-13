"""Places the pet can live, besides the house: eight rooms and outdoor scenes, all original pixel art
drawn in code (none of it comes from the pet art pack, so it ships in every build).

Each place is a 64x32 top-down floor with the bed bottom-left (cushion around (16,21), where every
sleeping pose lands) and the bowl top-right (around (59,10), where she eats), and nothing in her
paths: the ball and the toy mouse both travel along y 28 (their pixels cover rows 24-31), and she
walks, sits and begs across the middle. Each gives:
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

from PIL import Image, ImageDraw

W, H = 64, 32


def rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, k):
    return tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))


def shade(c, k):
    return tuple(max(0, min(255, int(v * k))) for v in c)


def noise_fill(img, base, spread, seed):
    """Speckled ground: base colour with small random light/dark variation."""
    rnd = random.Random(seed)
    px = img.load()
    for y in range(H):
        for x in range(W):
            k = 1 + rnd.uniform(-spread, spread)
            px[x, y] = shade(base, k)


def put(img, x, y, c, a=1.0):
    if 0 <= x < W and 0 <= y < H:
        px = img.load()
        old = px[int(x), int(y)][:3]
        px[int(x), int(y)] = mix(old, c, a)


def generic_night(day, cool=(0.72, 0.84, 1.3), lift=6, ambient=0.16, glow=0.24, glow_c=None):
    """Lights out: dark and cool, lit softly around the bed (like the house room)."""
    img = day.copy()
    px = img.load()
    for y in range(H):
        for x in range(W):
            r, g, b = px[x, y]
            lum = (r * 3 + g * 6 + b) / 10
            bed = max(0.0, 1 - math.hypot(x - 16, (y - 20) * 1.6) / 22)
            k = ambient + glow * bed
            c = (lum * k * cool[0], lum * k * cool[1], lum * k * cool[2] + lift)
            if glow_c:  # a warm light source tints its pool
                c = mix(c, shade(glow_c, k * 1.6), bed * 0.5)
            px[x, y] = tuple(int(min(255, v)) for v in c)
    return img


def bed_cushion(img, rim, cushion, highlight, cx=16, cy=21, rx=15, ry=10):
    """A round bed in 3/4 view: rim, a shadowed inner wall and a soft cushion."""
    d = ImageDraw.Draw(img)
    d.ellipse([cx - rx, cy - ry + 1, cx + rx, cy + ry + 1], fill=shade(rim, 0.55))          # shadow under
    d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=rim)
    d.ellipse([cx - rx + 2, cy - ry + 1, cx + rx - 2, cy + ry - 3], fill=shade(rim, 1.18))  # top of the rim
    d.ellipse([cx - rx + 4, cy - ry + 3, cx + rx - 4, cy + ry - 3], fill=shade(rim, 0.7))   # inner wall
    d.ellipse([cx - rx + 5, cy - ry + 5, cx + rx - 5, cy + ry - 2], fill=cushion)
    d.ellipse([cx - rx + 7, cy - ry + 5, cx + 2, cy - 1], fill=highlight)


def bowl(img, full, body, rim, food, cx=59, cy=10):
    d = ImageDraw.Draw(img)
    d.ellipse([cx - 4, cy - 2, cx + 4, cy + 4], fill=shade(body, 0.6))
    d.ellipse([cx - 4, cy - 3, cx + 4, cy + 3], fill=body)
    d.ellipse([cx - 3, cy - 2, cx + 3, cy + 1], fill=rim)
    d.ellipse([cx - 2, cy - 1, cx + 2, cy + 1], fill=food if full else shade(rim, 0.55))
    if full:
        put(img, cx - 1, cy - 1, shade(food, 1.3))
        put(img, cx + 1, cy, shade(food, 0.75))


_YS, _XS = np.mgrid[0:H, 0:W].astype(float)
_FIRE = np.clip(1 - np.hypot(_XS - 64, _YS - 18) / 40, 0, 1)          # the library hearth's light
_LAMP = np.clip(1 - np.hypot(_XS - 6, (_YS - 5) * 1.2) / 30, 0, 1)    # the tatami lamp's light


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


# -- 1. Garden ------------------------------------------------------------------------------
class Garden:
    zzz = ZZZ
    name, blurb = "Garden", "A mown lawn with daisies, a wicker basket and a terracotta dish. Butterflies by day, fireflies at night."

    def day(self, full=True):
        img = Image.new("RGB", (W, H))
        px = img.load()
        rnd = random.Random(1)
        for y in range(H):
            for x in range(W):
                stripe = 1.06 if (x + y // 3) // 8 % 2 else 0.94      # mower stripes
                px[x, y] = shade(rgb("6fa646"), stripe * (1 + rnd.uniform(-0.07, 0.07)))
        for _ in range(55):                                            # grass tufts
            x, y = rnd.randrange(W), rnd.randrange(H)
            put(img, x, y, rgb("8cc35a"))
            put(img, x, y + 1, rgb("4f8433"))
        stones = [(36, 4), (43, 6), (50, 4)]                            # stepping stones along the top
        for sx, sy in stones:
            d = ImageDraw.Draw(img)
            d.ellipse([sx - 3, sy - 2, sx + 3, sy + 2], fill=rgb("a7a39a"))
            d.ellipse([sx - 2, sy - 2, sx + 2, sy], fill=rgb("c4c0b6"))
        for x, y in [(30, 2), (6, 3), (40, 12), (50, 14), (62, 18), (26, 30), (22, 5)]:  # daisies
            for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
                put(img, x + dx, y + dy, rgb("f6f3ea"))
            put(img, x, y, rgb("f2c440"))
        # wicker basket: woven rim
        bed_cushion(img, rgb("b07a3c"), rgb("e9d6b0"), rgb("f6ead0"))
        for a in range(0, 360, 18):
            x = 16 + 14 * math.cos(math.radians(a))
            y = 21 + 9 * math.sin(math.radians(a))
            put(img, x, y, rgb("8a5a26"))
        bowl(img, full, rgb("c0643a"), rgb("d98b5c"), rgb("7a4a2a"))
        return img

    def night(self):
        img = generic_night(self.day(True), cool=(0.6, 0.85, 1.25), ambient=0.18)
        px = img.load()
        for x, y in [(30, 2), (6, 3), (40, 12), (50, 14), (62, 18), (26, 30), (22, 5)]:  # daisies close at night
            for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
                if 0 <= x + dx < W and 0 <= y + dy < H:
                    px[x + dx, y + dy] = mix(px[x + dx, y + dy], px[x + 2 if x + 2 < W else x - 2, y], 0.8)
        return img

    def back(self, img, t, room):
        pass

    def front(self, img, t, night):
        if night:
            fireflies(img, t, [(8, 5), (30, 4), (46, 9), (56, 22), (38, 18), (24, 27)])
        else:
            x = 34 + 22 * math.sin(t / 2600)                            # a butterfly flutters about
            y = 8 + 5 * math.sin(t / 1300)
            flap = int(t / 120) % 2
            c = rgb("8fd0ff")                                          # cool, so it never reads as her ginger
            put(img, x, y, rgb("3a2a1a"))
            if flap:
                put(img, x - 1, y - 1, c); put(img, x + 1, y - 1, c); put(img, x - 1, y, c); put(img, x + 1, y, c)
            else:
                put(img, x - 1, y, c); put(img, x + 1, y, c)


_SEA = [mix(rgb("2f8fbf"), rgb("54b7d6"), y / 6) for y in range(8)]
_SEA_NIGHT = [shade(c, 0.35) for c in _SEA]
_FOAM, _FOAM_NIGHT, _GLINT = rgb("f4fbfb"), (90, 110, 140), rgb("bfe9f2")


# -- 2. Beach -------------------------------------------------------------------------------
class Beach:
    zzz = DEEP_ZZZ
    name, blurb = "Beach", "Warm sand, a striped towel, a coconut-shell bowl, shells, and the tide washing in at the top."

    def day(self, full=True):
        img = Image.new("RGB", (W, H))
        noise_fill(img, rgb("e8cf98"), 0.05, 2)
        rnd = random.Random(3)
        for _ in range(40):
            put(img, rnd.randrange(W), rnd.randrange(6, H), rgb("d4b67c"))
        # towel (the bed): a striped rectangle in perspective, a rolled pillow
        d = ImageDraw.Draw(img)
        towel = Image.new("L", (W, H))
        ImageDraw.Draw(towel).polygon([(3, 12), (29, 11), (31, 31), (1, 31)], fill=255)
        bands = Image.new("RGB", (W, H))
        bd = ImageDraw.Draw(bands)
        for i, c in enumerate(("e0574a", "f2f0ea", "3f8fd2", "f2f0ea", "e0574a", "f2f0ea", "3f8fd2")):
            bd.rectangle([0, 11 + i * 3, W, 13 + i * 3], fill=rgb(c))           # solid 3 px bands
        img.paste(bands, (0, 0), towel)                                         # clipped to the towel
        d.rectangle([4, 9, 27, 12], fill=rgb("f6c64a"))                    # rolled pillow
        d.line([(4, 12), (27, 12)], fill=rgb("c99a2c"))
        # shells and a starfish
        for x, y in ((40, 24), (52, 29)):
            put(img, x, y, rgb("f7e6e0")); put(img, x + 1, y, rgb("eac2b6")); put(img, x, y + 1, rgb("eac2b6"))
        sx, sy = 34, 8
        for a in range(5):
            ang = math.radians(a * 72 - 90)
            for r in (1, 2):
                put(img, sx + r * math.cos(ang), sy + r * math.sin(ang), rgb("f08a4b"))
        put(img, sx, sy, rgb("f6a26b"))
        # coconut bowl
        bowl(img, full, rgb("6b4423"), rgb("f3ecdc"), rgb("c77b49"))
        return img

    def sea_edge(self, t):
        return 3.6 + 1.0 * math.sin(t / 1400)

    def night(self):
        return generic_night(self.day(True), cool=(0.62, 0.76, 1.35), ambient=0.17)

    def back(self, img, t, room):
        night = room == "night"
        px = img.load()
        edge = self.sea_edge(t)
        foam = _FOAM_NIGHT if night else _FOAM
        for x in range(W):
            e = edge + 0.6 * math.sin(x / 5 + t / 700)
            for y in range(0, min(6, int(e) + 2)):          # never past row 5: the bowl starts at row 7
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

    def front(self, img, t, night):
        pass


# -- 3. Snow day ----------------------------------------------------------------------------
class Snow:
    zzz = DEEP_ZZZ
    name, blurb = "Snow day", "Fresh snow with footprints, a knitted blanket and a steaming bowl, with snow falling."

    def day(self, full=True):
        img = Image.new("RGB", (W, H))
        px = img.load()
        rnd = random.Random(4)
        for y in range(H):
            for x in range(W):
                drift = 0.04 * math.sin(x / 7 + y / 5)
                px[x, y] = shade(rgb("d8e1ec"), 0.97 + drift + rnd.uniform(-0.02, 0.02))
        for i in range(9):                                                     # little paw prints
            x, y = 36 + i * 3, 30 - i * 2 - (i % 2)
            put(img, x, y, rgb("b4c3d8")); put(img, x + 1, y, rgb("b4c3d8"))
        d = ImageDraw.Draw(img)
        d.ellipse([40, 2, 52, 7], fill=rgb("d8e2f0"))                         # a drift's shadow
        bed_cushion(img, rgb("b8323a"), rgb("e9dccb"), rgb("fbf3e6"))         # knitted blanket nest
        for a in range(0, 360, 24):                                            # cable-knit dots on the rim
            x = 16 + 13 * math.cos(math.radians(a)); y = 21 + 8.5 * math.sin(math.radians(a))
            put(img, x, y, rgb("e5626a"))
        bowl(img, full, rgb("3d6fa8"), rgb("cfe0f2"), rgb("a8673a"))
        return img

    def night(self):
        return generic_night(self.day(True), cool=(0.6, 0.72, 1.25), ambient=0.24, glow=0.26, glow_c=rgb("ffcf8a"))

    def back(self, img, t, room):
        if room == "day":                                                      # steam off a full bowl
            for k in range(3):
                p = ((t / 1100) + k / 3) % 1
                x, y = 59 + math.sin(p * 6 + k) * 1.5, 7 - p * 6
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
class Space:
    zzz = ZZZ
    name, blurb = "Space station", "Deck plates and hazard stripes, a glowing sleep pod, a nutrient bowl, and stars sliding past the window."

    def day(self, full=True):
        img = Image.new("RGB", (W, H))
        px = img.load()
        for y in range(H):
            for x in range(W):
                plate = (x // 8 + y // 8) % 2
                c = rgb("4a5260") if plate else rgb("434a57")
                if x % 8 == 0 or y % 8 == 0:
                    c = rgb("323844")
                if (x % 8, y % 8) in ((2, 2), (6, 2), (2, 6), (6, 6)):
                    c = rgb("6a7384")                                        # rivets
                px[x, y] = c
        d = ImageDraw.Draw(img)
        d.rectangle([34, 0, 63, 5], fill=rgb("0a0d18"))                      # window strip
        d.rectangle([34, 5, 63, 5], fill=rgb("8892a6"))
        for x in range(36, 64):                                               # hazard stripes on the floor
            for y in (30, 31):
                px[x, y] = rgb("e2b23a") if (x + y) // 2 % 2 else rgb("1f2229")
        # sleep pod: a rounded capsule with a cyan glow ring
        d.ellipse([1, 11, 31, 31], fill=rgb("252a35"))
        d.ellipse([2, 12, 30, 30], fill=rgb("3fd4e6"))
        d.ellipse([3, 13, 29, 29], fill=rgb("2b313d"))
        d.ellipse([6, 15, 26, 28], fill=rgb("8fa4c4"))
        d.ellipse([8, 15, 18, 20], fill=rgb("b4c6e0"))
        bowl(img, full, rgb("9aa6ba"), rgb("d7dfec"), rgb("7bd36a"))
        return img

    def night(self):
        return generic_night(self.day(True), cool=(0.55, 0.75, 1.4), ambient=0.2, glow=0.3, glow_c=rgb("3fd4e6"))

    def back(self, img, t, room):
        night = room == "night"
        rnd = random.Random(5)
        for k in range(14):                                                   # stars slide past the window
            x = 34 + (rnd.uniform(0, 30) - t / 1000 * rnd.uniform(1.5, 4)) % 30
            y = 1 + rnd.randrange(4)
            put(img, x, y, rgb("ffffff"), rnd.uniform(0.4, 1) * (0.55 if night else 1))
        for k, (x, y) in enumerate(((34, 8), (34, 12))):                       # status lights blink, slowly
            on = (int(t / 1800) + k) % 2
            put(img, x, y, rgb("ff4b4b") if k else rgb("4bff7a"), (1 if on else 0.2) * (0.4 if night else 1))
        pulse = 0.5 + 0.5 * math.sin(t / 700)                                 # the pod ring breathes
        for a in range(0, 360, 6):
            x = 16 + 14 * math.cos(math.radians(a)); y = 21 + 9 * math.sin(math.radians(a))
            put(img, x, y, rgb("9ff6ff"), 0.35 * pulse)

    def front(self, img, t, night):
        pass


# -- 5. Library -----------------------------------------------------------------------------
class Library:
    zzz = ZZZ
    name, blurb = "Library", "Dark wood, a patterned rug, stacks of books, a velvet cushion and a brass bowl, with the hearth glowing at the edge. At night the firelight flickers across the floor."

    def day(self, full=True):
        img = Image.new("RGB", (W, H))
        px = img.load()
        rnd = random.Random(6)
        for y in range(H):
            for x in range(W):
                board = (y // 4) % 2
                seam = (x + (y // 4) * 13) % 21 == 0
                c = rgb("5b3a26") if board else rgb("54351f")
                px[x, y] = shade(c, 0.75 if seam else 1 + rnd.uniform(-0.05, 0.05))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 9, 36, 31], fill=rgb("7a1f2b"))                        # the rug
        d.rectangle([2, 11, 34, 31], outline=rgb("d9a441"))
        for x in range(4, 33, 4):
            for y in range(13, 31, 4):
                put(img, x, y, rgb("c98a33"))
                put(img, x + 2, y + 2, rgb("4a1720"))
        for (bx, by, cols) in ((40, 1, ("8e3b2e", "2f5d50", "c9a35a", "3c4f7a")), (24, 1, ("3c4f7a", "8e3b2e", "6f6a3a"))):
            for i, c in enumerate(cols):                                         # book stacks, seen from above
                d.rectangle([bx + (i % 2), by + i * 2, bx + 7 + (i % 2), by + i * 2 + 1], fill=rgb(c))
                put(img, bx + 1 + (i % 2), by + i * 2, rgb("e8dcc0"))
        d.rectangle([61, 15, 63, 21], fill=rgb("3a2a24"))                       # the hearth, at the edge
        d.line([(60, 15), (60, 21)], fill=rgb("4e3e36"))                        # its stone lip
        bed_cushion(img, rgb("3a2f5a"), rgb("6b5aa8"), rgb("9b8ad6"))            # velvet cushion
        bowl(img, full, rgb("b8862e"), rgb("e2c06b"), rgb("8a5a3a"))
        return img

    def night(self):
        return generic_night(self.day(True), cool=(1.05, 0.8, 0.62), lift=2, ambient=0.14, glow=0.3, glow_c=rgb("ff9a3c"))

    def back(self, img, t, room):
        night = room == "night"
        flick = 0.8 + 0.2 * math.sin(t / 90) * math.sin(t / 233)
        for k, (x, y) in enumerate(((62, 16), (63, 18), (62, 20))):            # embers in the hearth
            put(img, x, y, rgb("ffd27a") if (k + int(t / 300)) % 3 == 0 else rgb("ff8a2a"), 0.6 + 0.4 * flick)
            for dx, dy in ((-1, 0), (0, -1), (0, 1)):                            # a soft glow round each
                put(img, x + dx, y + dy, rgb("ff8a2a"), 0.3 * flick)
        if night:                                                              # firelight flickers over the floor
            glow(img, _FIRE, rgb("ff8a2a"), 0.22 * flick)

    def front(self, img, t, night):
        pass


# -- 6. Tatami ------------------------------------------------------------------------------
class Tatami:
    zzz = ZZZ
    name, blurb = "Tatami room", "Woven tatami mats, an indigo zabuton, a ceramic bowl, a little bonsai, and a paper lamp in the corner that glows over her bed at night."

    def day(self, full=True):
        img = Image.new("RGB", (W, H))
        px = img.load()
        for y in range(H):
            for x in range(W):
                mat_v = x < 32
                weave = (y if mat_v else x) % 2
                c = rgb("cfc28a") if weave else rgb("c3b47c")
                edge = (x in (0, 31, 32, 63)) or (y in (0, 31)) or (not mat_v and y == 16)
                px[x, y] = rgb("2b3a2c") if edge else c
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([2, 10, 30, 31], radius=3, fill=rgb("26325e"))       # zabuton
        d.rounded_rectangle([4, 12, 28, 29], radius=3, fill=rgb("33437a"))
        d.rounded_rectangle([5, 13, 27, 28], radius=2, outline=rgb("4a5a92"))     # stitching
        put(img, 16, 20, rgb("d9d4c0"))                                           # the centre tuft
        d.ellipse([39, 2, 47, 8], fill=rgb("7a5032"))                             # bonsai pot + tree
        for cx, cy, r in ((39, 2, 3), (43, 1, 3), (47, 2, 3), (42, 4, 2), (45, 4, 2)):   # a cloud-pruned canopy
            d.ellipse([cx - r, cy - r + 1, cx + r, cy + r - 1], fill=rgb("3f7a3a"))
        for x, y in ((38, 1), (43, 0), (47, 1), (44, 3)):
            put(img, x, y, rgb("6fae5a"))
        d.ellipse([2, 1, 10, 8], fill=rgb("3a2c24"))                              # round paper lamp, from above
        d.ellipse([3, 2, 9, 7], fill=rgb("f3e6c8"))
        d.ellipse([5, 3, 7, 5], fill=rgb("ffe3a8"))                               # its light
        bowl(img, full, rgb("e9e4da"), rgb("4a6a8a"), rgb("e7a35a"))
        return img

    def night(self):
        img = generic_night(self.day(True), cool=(0.7, 0.8, 1.2), ambient=0.15)
        px = img.load()
        for x, y in ((38, 1), (43, 0), (47, 1), (44, 3), (40, 3)):                 # the bonsai keeps a little shape
            px[x, y] = (40, 60, 52)
        return img

    def back(self, img, t, room):
        night = room == "night"
        if night:                                                              # the lamp glows over the zabuton
            flick = 0.9 + 0.1 * math.sin(t / 170) * math.sin(t / 61)
            glow(img, _LAMP, rgb("ffb35c"), 0.55 * flick)
            d = ImageDraw.Draw(img)                                                # the lamp itself, lit
            d.ellipse([3, 2, 9, 7], fill=mix(rgb("f3e6c8"), rgb("ffe3a8"), 0.5))
            d.ellipse([5, 3, 7, 5], fill=rgb("fff2d0"))

    def front(self, img, t, night):
        pass


# -- 7. Forest ------------------------------------------------------------------------------
class Forest:
    zzz = ZZZ
    name, blurb = "Forest floor", "Moss and fallen leaves, a fairy ring of toadstools by a log, a leaf nest and an acorn-cup bowl. Leaves drift down by day; green fireflies at night."

    def day(self, full=True):
        img = Image.new("RGB", (W, H))
        noise_fill(img, rgb("4d6b35"), 0.12, 7)
        rnd = random.Random(8)
        for _ in range(40):
            x, y = rnd.randrange(W), rnd.randrange(H)
            c = rgb(rnd.choice(("c97a2e", "a8461f", "d9a23a", "7a5a2a")))
            put(img, x, y, c); put(img, x + 1, y, shade(c, 0.85))
        d = ImageDraw.Draw(img)
        for a in range(0, 360, 60):                                                  # a fairy ring of toadstools
            x, y = round(18 + 7 * math.cos(math.radians(a))), round(4 + 2.5 * math.sin(math.radians(a)))
            put(img, x, y + 1, rgb("efe6d6"))                                       # stalk
            d.rectangle([x - 1, y - 1, x + 1, y], fill=rgb("c8322a"))              # cap
            put(img, x, y - 1, rgb("f6efe2"))                                       # a spot
        d.ellipse([34, 2, 40, 5], fill=rgb("6b4a2a")); d.line([(35, 3), (39, 3)], fill=rgb("8a6a3a"))  # a fallen log
        bed_cushion(img, rgb("8a5a26"), rgb("b98a3a"), rgb("e0b65a"))             # a nest of leaves
        for a in range(0, 360, 15):
            x = 16 + 14 * math.cos(math.radians(a)); y = 21 + 9 * math.sin(math.radians(a))
            put(img, x, y, rgb(("c97a2e", "a8461f", "d9a23a")[a // 15 % 3]))
        bowl(img, full, rgb("8a5a2a"), rgb("c49a5a"), rgb("6a3a1a"))              # acorn cup
        put(img, 59, 6, rgb("5a3a1a"))
        return img

    def night(self):
        return generic_night(self.day(True), cool=(0.6, 0.85, 1.2), ambient=0.17)

    def back(self, img, t, room):
        pass

    def front(self, img, t, night):
        if night:                                                                   # fewer, greener fireflies
            fireflies(img, t, [(12, 6), (40, 8), (54, 20)], colour=rgb("7aff9a"), halo=rgb("3a9a5a"))
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
class Moon:
    zzz = ZZZ
    name, blurb = "The Moon", "Grey dust and craters, Earth hanging in the black above her bed, a crater bed with a quilt and a tin bowl."

    def day(self, full=True):
        img = Image.new("RGB", (W, H))
        noise_fill(img, rgb("9a9a9e"), 0.06, 12)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 63, 3], fill=rgb("05060c"))                          # the black sky's edge
        for x in range(W):
            put(img, x, 4, rgb("6e6e74"))
        d.ellipse([18, -4, 26, 3], fill=rgb("2f6fd6"))                         # Earth
        d.ellipse([20, -2, 23, 1], fill=rgb("4fae5a"))
        d.ellipse([22, 1, 25, 3], fill=rgb("f2f2f2"))
        for cx, cy, r in ((28, 9, 4), (54, 16, 3), (38, 8, 3)):                   # craters
            d.ellipse([cx - r, cy - r * 0.6, cx + r, cy + r * 0.6], fill=rgb("7a7a80"))
            d.ellipse([cx - r + 1, cy - r * 0.6 + 1, cx + r, cy + r * 0.6], fill=rgb("8a8a90"))
        bed_cushion(img, rgb("6e6e76"), rgb("d77a4a"), rgb("f0a070"))              # crater bed, orange quilt
        for x in range(8, 26):                                                    # quilting
            for y in range(15, 29):
                if (x + y) % 5 == 0 and ((x - 16) / 10) ** 2 + ((y - 22) / 6.5) ** 2 < 1:
                    put(img, x, y, rgb("b85a32"))
        bowl(img, full, rgb("b8bcc6"), rgb("e6e8ee"), rgb("c47a3a"))
        for i in range(6):                                                         # boot prints
            put(img, 30 + i * 4, 30 - i, rgb("7e7e84"))
        return img

    def night(self):
        return generic_night(self.day(True), cool=(0.7, 0.8, 1.2), ambient=0.2)

    def back(self, img, t, room):
        rnd = random.Random(13)
        for k in range(8):                                                          # stars twinkle in the black
            x, y = rnd.randrange(W), rnd.randrange(4)
            if 16 <= x <= 28:
                continue
            put(img, x, y, rgb("ffffff"), 0.4 + 0.6 * (0.5 + 0.5 * math.sin(t / 400 + k * 1.3)))

    def front(self, img, t, night):
        pass


PLACES = {"garden": Garden(), "beach": Beach(), "snow": Snow(), "space": Space(), "library": Library(),
          "tatami": Tatami(), "forest": Forest(), "moon": Moon()}
CHOICES = ["house", *PLACES]
LABELS = {"house": "House", **{k: v.name for k, v in PLACES.items()}}


@lru_cache(maxsize=None)
def rooms(place: str) -> tuple:
    """(day, empty bowl, night) for a place, drawn once."""
    p = PLACES[place]
    return p.day(True), p.day(False), p.night()
