"""Seasonal tree: an apple tree or an oak in the middle of the panel, through the year.

It comes into leaf, blossoms, fruits, turns and drops its leaves day by day (``year``), takes its weather from
the last cached OpenWeather reading (``sky``), and has visitors (``wildlife``): a robin on its branch,
butterflies on warm days (five kinds, alone, in chasing pairs, settling on a flower, or dancing in a little
group), fireflies on summer nights. The time sits either side of
the tree, small in the top corners, or not at all.

Every picture is a drawing in the Animator (``resources/animator/tree_*.json``). A tree's drawing has layers the
code reads by name: Wood, Leaves (in up to four greens, light to shade: summer), Blossom, Fruit, and Perch
(hidden: the one pixel of branch the robin stands on). Save your own over any of them and the clock uses it.
"""
from __future__ import annotations

import datetime as dt
import math
from functools import lru_cache

from PIL import Image, ImageChops

from bongo import sprites
from bongo.holidays.sky import sunrise_sunset
from bongo.module import Style

from ..common import HOURS
from . import sky, wildlife, year

W, H = 64, 32
GROUND_Y = 31
GROUND = {"spring": (40, 112, 46), "summer": (36, 98, 40), "autumn": (86, 72, 36), "winter": (72, 60, 44)}
SNOW_LYING, FROST = (226, 234, 250), (170, 196, 232)
FLOWERS = ((246, 214, 70), (240, 236, 226), (186, 140, 240))
NIGHT, DUSK = 0.72, 60 * 60          # how bright the tree is at night; how long dusk and dawn last either side of sunset and sunrise (s)


def _lit(img: Image.Image | None) -> dict:
    """{(x, y): (r, g, b)} for every pixel drawn on a layer, placed bottom-centre on the 64 x 32 scene."""
    if img is None:
        return {}
    ox, oy = (W - img.width) // 2, H - img.height
    px = img.load()
    return {(x + ox, y + oy): px[x, y][:3] for y in range(img.height) for x in range(img.width) if px[x, y][3] and 0 <= x + ox < W and 0 <= y + oy < H}


@lru_cache(maxsize=8)
def _sun(day: dt.date, lat: float, lon: float, tz) -> tuple[float, float]:
    """Sunrise and sunset as seconds after midnight (6:00 and 18:00 where the sun doesn't rise or set)."""
    try:
        rise, set_ = sunrise_sunset(day, lat, lon, tz or dt.timezone.utc)
    except (ValueError, ZeroDivisionError):
        return 6 * 3600.0, 18 * 3600.0
    return (rise.hour * 3600 + rise.minute * 60.0), (set_.hour * 3600 + set_.minute * 60.0)


class SeasonalTree(Style):
    """Seasonal tree: an apple tree or an oak through the year, with its weather and its visitors."""
    group = "Seasonal"
    fps = 10
    defaults = {"tree": "apple", "hemisphere": "auto", "show_time": True, "time_place": "sides", "hours": "24",
                "time_colour": "#FFF4DE", "weather": True, "wildlife": True}
    choices = {"tree": list(year.TREES), "hemisphere": ["auto", "north", "south"], "time_place": ["sides", "corners"], "hours": HOURS}
    basic = ("tree", "hemisphere", "show_time", "time_place", "weather", "wildlife")
    show_off: str | None = None   # "See it where it's used": this visitor is always there ("robin", "fireflies",
    #                               or "butterfly:<kind>:<flight>")

    def setup(self):
        s = self.settings
        self.kind = s["tree"] if s["tree"] in year.SPECIES else "apple"
        art = sprites.load(f"tree_{self.kind}")
        self.wood = _lit(art.layer("Wood")) or _lit(art.frame(0))
        perch = _lit(art.layer("Perch"))
        self.perch = min(perch) if perch else (21, 13)
        self.tree = year.prepare(self.kind, _lit(art.layer("Leaves")), _lit(art.layer("Blossom")), _lit(art.layer("Fruit")))
        xs = [x for x, _ in self.wood] + [l.x for l in self.tree.leaves] or [24, 40]
        self.left, self.right = min(xs), max(xs)
        self.critters = {}
        for who in ("robin", "firefly", *(f"butterfly_{k}" for k in wildlife.BUTTERFLIES)):
            try:
                self.critters[who] = sprites.load(f"tree_{who}")
            except (FileNotFoundError, ValueError):
                self.critters[who] = None
        self.big, self.small = self.ctx.fonts("5x7"), self.ctx.fonts("tom-thumb")
        self.ink = tuple(int(s["time_colour"][i:i + 2], 16) for i in (1, 3, 5))
        self._day = None

    # -- where we are in the year, and in the day ---------------------------------------------------
    def hemisphere(self) -> str:
        h = self.settings["hemisphere"]
        if h in ("north", "south"):
            return h
        home = self.ctx.home or {}
        return "south" if float(home.get("latitude", 51.5)) < 0 else "north"

    def light(self, now: dt.datetime) -> tuple[bool, bool]:
        """(night, dusk or dawn) at ``now``, by the sun at the Matrix's location (London if none is set)."""
        home = self.ctx.home or {}
        rise, set_ = _sun(now.date(), float(home.get("latitude", 51.5)), float(home.get("longitude", -0.13)), now.tzinfo)
        s = now.hour * 3600 + now.minute * 60 + now.second
        night = s < rise or s >= set_
        return night, abs(s - rise) < DUSK or abs(s - set_) < DUSK

    # -- the tree for one day: worked out once a day (and again when the snow starts or stops) --------
    def day(self, today: dt.date, f: float, season: str, snowing: bool):
        key = (today, snowing)
        if self._day and self._day[0] == key:
            return self._day[1]
        tree, sp = self.tree, year.SPECIES[self.kind]
        leaves = [(l, year.leaf_colour(tree, l, f)) for l in tree.leaves]
        shown = {(l.x, l.y): c for l, c in leaves if c}
        flowers = {(b[0], b[1]): c for b in tree.blossom if (c := year.blossom_colour(b, f))}
        apples = {(a[0], a[1]): c for a in tree.fruit if (c := year.fruit_colour(tree, a, f))}
        front = {**shown, **flowers, **apples}
        stirs = [p for p in front if p in shown and abs(p[0] - 31.5) > 9 and year.unit("stir", *p) < 0.35]
        tops = {}
        for (x, y) in list(front) + list(self.wood):
            if y < tops.get(x, 99):
                tops[x] = y
        fallen = sum(1 for l, c in leaves if c is None and f >= l.falls) / max(1, len(leaves))
        since = (f - sp.falls[1]) % 1.0                      # the litter rots down over the six weeks after the last leaf falls
        if f < sp.turns[0] or since < 0.5:
            fallen *= 0.0 if f < sp.turns[0] else max(0.0, 1 - since / 0.12) if f >= sp.falls[1] else 1.0
        out = {"front": front, "stirs": stirs, "tops": tops, "fallen": fallen, "season": season, "f": f,
               "falling": [(l.x, l.y, sp.autumn[l.last][l.tone - 1]) for l, c in leaves if c and f >= (l.turns + l.falls) / 2], "litter_colour": [sp.autumn[k][1] for k in range(4)],
               "petals": [(b[0], b[1], b[2]) for b in tree.blossom if b[4] - 0.02 <= f < b[4] + 0.03]}
        self._day = (key, out)
        return out

    # -- drawing ---------------------------------------------------------------------------------------
    def render(self, now):
        s = self.settings
        f = year.season_time(now.date(), self.hemisphere())
        season = year.season_name(f)
        w = sky.now(self.ctx.home) if s["weather"] else sky.CALM
        night, dusky = self.light(now)
        t = now.timestamp() % 86400
        d = self.day(now.date(), f, season, w.kind == "snow")
        scene = Image.new("RGB", (W, H))
        px = scene.load()
        self.ground(px, d, w)
        for (x, y), c in self.wood.items():
            px[x, y] = c
        visits = self.visits(now, season, f, night, dusky, w)
        robin = next((v for v in visits if v.who == "robin"), None)
        if robin:
            self.robin(scene, robin, t)
        gust = w.wind and math.sin(t * (3 + w.wind)) > 0.3
        for (x, y), c in d["front"].items():
            if gust and (x, y) in d["stirs"]:
                continue
            px[x, y] = year.dim(c, 0.86) if w.wet else c
        if gust:
            for (x, y) in d["stirs"]:
                nx = x + (1 if x > 31 else -1)
                if 0 <= nx < W:
                    px[nx, y] = d["front"][(x, y)]
        if w.frost:
            for x, y in d["tops"].items():
                px[x, y] = year.mix(px[x, y], FROST, 0.5)
        if w.kind == "snow":
            for x, y in d["tops"].items():
                if year.unit("snow on", x) < 0.85:
                    px[x, y] = SNOW_LYING
        self.falling(px, d, t, w)
        gain = NIGHT if night else 1.0
        if w.kind == "fog":
            gain *= 0.7
        if gain < 1:
            scene = Image.eval(scene, lambda v: int(v * gain))
        if 0 <= sky.strike(w, t) < 0.12:                        # lightning catches the tree: the sky stays black
            lit = scene.convert("L").point(lambda v: 255 if v else 0)
            scene = Image.composite(Image.blend(scene, Image.new("RGB", scene.size, (235, 240, 255)), 0.28), scene, lit)
        for v in visits:
            if v.who != "robin":
                self.visitor(scene, v, now, t, night)
        img = self.ctx.canvas()
        ox, oy = (img.width - W) // 2, (img.height - H) // 2 if img.height >= H else 0
        img.paste(scene, (ox, oy))
        boxes = self.time_boxes(img, ox, oy, now) if s["show_time"] else []
        keep_out = (lambda x, y: any(a - 2 <= x <= b + 2 and c - 2 <= y <= e + 2 for a, c, b, e, *_ in boxes)) if boxes else (lambda x, y: False)
        sky.draw(img, w, t, keep_out)
        for a, c, b, e, font, x, base, text, outline in boxes:
            font.draw(img, x, base, text, self.ink, outline=outline)
        return img

    def ground(self, px, d, w) -> None:
        """A strip of ground under the tree, fading out before it reaches the time: grass, fallen leaves, snow."""
        season, f = d["season"], d["f"]
        for x in range(10, 54):
            edge = min(1.0, (min(x - 10, 53 - x) + 1) / 6)
            c = SNOW_LYING if w.kind == "snow" else GROUND[season]
            px[x, GROUND_Y] = year.dim(c, (0.7 if w.kind == "snow" else 1.15) * edge * (0.8 + 0.2 * year.unit("ground", x)))
            near = 1 - abs(x - 31.5) / 24
            if w.kind == "snow":
                continue
            if year.unit("litter", x) < d["fallen"] * 1.4 * near:
                px[x, GROUND_Y - 1] = year.dim(d["litter_colour"][int(year.unit("leaf", x) * 4)], 0.8)
            elif 0.03 < f < 0.22 and abs(x - 31.5) > 5 and year.unit("flower", x) < 0.12:
                px[x, GROUND_Y - 1] = FLOWERS[int(year.unit("which", x) * 3)]
            elif 0.02 < f < 0.55 and abs(x - 31.5) > 3 and year.unit("tuft", x) < 0.3 * edge:
                px[x, GROUND_Y - 1] = year.dim(GROUND[season], 1.5)

    def falling(self, px, d, t, w) -> None:
        """Leaves on their way down in autumn (more in the wind), petals at the end of the blossom."""
        def drift(items, count, speed=1.25):
            for i in range(count):
                period = 2.6 + 2 * year.unit("fall", i, 0)
                k, turn = math.modf(t / period + year.unit("fall", i, 1))
                x0, y0, c = items[int(year.unit("which", int(turn), i) * len(items))]
                y = y0 + (GROUND_Y - 1 - y0) * min(1.0, k * speed)
                x = round(x0 + math.sin(k * 9 + i) * 1.5 + w.wind * k * 9)
                if y < GROUND_Y - 1 and 0 <= x < W:
                    px[x, round(y)] = c
        if d["falling"]:
            drift(d["falling"], min(6, 1 + len(d["falling"]) // 60 + 2 * w.wind))
        if d["petals"]:
            drift(d["petals"], 3, 1.1)

    def visits(self, now, season, f, night, dusky, w) -> list:
        if self.show_off:
            who, *more = self.show_off.split(":")
            if who == "butterfly":
                kind, flight = (more + ["brimstone", "chase"])[:2] if more else ("brimstone", "chase")
                kinds = (kind,) if flight in ("flutter", "settle") else (kind, kind) if flight == "chase" else (kind, kind, kind)
                return [wildlife.Visit("butterfly", 0, wildlife.LENGTH[flight][1], kinds=kinds, flight=flight)]
            return [wildlife.Visit(who, 0, 86400)]
        if not self.settings["wildlife"]:
            return []
        return wildlife.visitors(now, season, f, night, dusky, w)

    def robin(self, scene, visit, t) -> None:
        art = self.critters["robin"]
        if art is None:
            return
        fr = art.frame(1 if t % 3.3 < 0.4 else 0)
        foot = [x for x in range(fr.width) if fr.getpixel((x, fr.height - 1))[3]] or [fr.width // 2]
        hop = 1 if visit.length < 86400 and t % visit.length < 0.6 else 0
        x = self.perch[0] - round(sum(foot) / len(foot))
        scene.paste(fr, (x, self.perch[1] - fr.height - hop), fr)

    def visitor(self, scene, visit, now, t, night) -> None:
        s = now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6
        k = visit.at(s) if not self.show_off else (t % visit.length) / visit.length
        if k is None:
            return
        if visit.who == "butterfly":
            for x, y, picture, kind in wildlife.butterfly_spots(visit, k, t):
                art = self.critters.get(f"butterfly_{kind}")
                if art:
                    fr = art.frame(picture)
                    scene.paste(fr, (x - fr.width // 2, y), fr)
        elif visit.who == "fireflies" and (art := self.critters["firefly"]):
            glow = Image.new("RGB", scene.size)            # all of them on one layer, added as light
            for x, y, phase in wildlife.firefly_spots(t):
                fr = art.at(t + phase)
                glow.paste(fr.convert("RGB"), (x - fr.width // 2, y - fr.height // 2), fr)
            scene.paste(ImageChops.add(scene, glow))

    def time_boxes(self, img, ox, oy, now) -> list:
        """Where the hour and the minutes go: [(left, top, right, bottom, font, x, baseline, text, outline)]."""
        s = self.settings
        hour = now.hour if s["hours"] == "24" else (now.hour % 12 or 12)
        hh = f"{hour:02d}" if s["hours"] == "24" else str(hour)
        mm = f"{now.minute:02d}"
        left, right = self.left + ox, self.right + ox
        big, small = self.big, self.small
        wh, wm = big.width(hh) - 1, big.width(mm) - 1
        if s["time_place"] == "sides" and left - 3 >= wh + 1 and img.width - right - 3 >= wm + 1:
            base = oy + 18
            xh = max(1, (left - 2 - wh) // 2)
            xm = right + 3 + max(0, (img.width - right - 3 - wm) // 2)
            return [(xh, base - big.cap, xh + wh, base, big, xh, base, hh, None), (xm, base - big.cap, xm + wm, base, big, xm, base, mm, None)]
        wh, wm = small.width(hh) - 1, small.width(mm) - 1
        base = small.cap + 1
        xm = img.width - 1 - wm
        clear = left > wh + 2 and right < xm - 2
        edge = None if clear else (0, 0, 0)        # over the tree: a black edge keeps it readable
        return [(1, base - small.cap, 1 + wh, base, small, 1, base, hh, edge), (xm, base - small.cap, xm + wm, base, small, xm, base, mm, edge)]
