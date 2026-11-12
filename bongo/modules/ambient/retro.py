"""Retro: digital rain, fire, plasma and pipes."""
from __future__ import annotations

import math

import numpy as np

from .sim import Sim, hsv

# 3 x 5 glyphs for the rain (digits and a few letters), one string of 15 bits each, row by row
GLYPHS = ["111101101101111", "010110010010111", "111001111100111", "111001111001111", "101101111001001",
          "111100111001111", "111100111101111", "111001001001001", "111101111101111", "111101111001111",
          "010101111101101", "110101110101110", "011100100100011", "101101010101101", "111010010010010",
          "100100100100111", "101111111101101", "011100010001110", "110101101101110", "101101101101010"]
MASKS = np.array([[[c == "1" for c in g[r * 3:r * 3 + 3]] for r in range(5)] for g in GLYPHS])   # n x 5 x 3


class DigitalRain(Sim):
    """Digital rain: columns of glyphs fall with a bright head and a fading green tail; the
    glyphs keep changing."""
    group = "Retro"
    rate = 20.0
    PITCH = 5            # columns 5 LEDs apart, so neighbouring trails don't run together

    def init(self):
        self.cols = max(1, (self.W + 2) // self.PITCH)
        self.x0 = (self.W - (self.cols * self.PITCH - 2)) // 2
        self.rows = self.H // 6 + 2
        self.glyph = self.r.integers(len(GLYPHS), size=(self.rows, self.cols))
        self.head = self.r.uniform(-self.rows, self.rows + 4, self.cols)   # in glyph rows; some start mid-fall
        self.speed = self.r.uniform(0.12, 0.3, self.cols)
        self.tail = self.r.integers(4, 9, self.cols)

    def step(self):
        before = self.head.copy()
        self.head += self.speed
        for c in np.flatnonzero((before <= 0) & (self.head > 0)):   # about to come on: not beside a falling one
            for n in (c - 1, c + 1):
                if 0 <= n < self.cols and before[n] > -2 and self.head[n] - self.tail[n] < self.rows:
                    self.head[c] = self.r.uniform(-6, -2)
                    break
        seen = (self.head > 0) & (self.head < self.H / 6 + 1)  # heads on the panel right now
        waiting = [c for c in np.flatnonzero(self.head <= 0)
                   if not any(seen[max(0, c - 1):c + 2])]    # never right beside one that's falling
        if seen.sum() < 3 and waiting:                       # always three or more heads in view
            far = max(waiting, key=lambda c: min([abs(c - a) for a in np.flatnonzero(seen)] or [99]) + self.r.random())
            self.head[far] = 0.01
        done = self.head - self.tail > self.rows
        for c in np.flatnonzero(done):
            active = int(((self.head > 0) & (self.head - self.tail < self.rows)).sum())
            self.head[c] = self.r.uniform(-2, -0.5) if active < 4 else self.r.uniform(-10, -1)  # always a few falling
            self.speed[c] = self.r.uniform(0.12, 0.3)
            self.tail[c] = self.r.integers(4, 9)
        flip = self.r.random(self.glyph.shape) < 0.05
        self.glyph[flip] = self.r.integers(len(GLYPHS), size=int(flip.sum()))

    def draw(self):
        img = np.zeros((self.H, self.W, 3))
        for c in range(self.cols):
            h = int(math.floor(self.head[c]))
            frac = self.head[c] - h
            x = self.x0 + c * self.PITCH
            nxt = h + 1                                      # the next glyph glows in as the head nears it
            if 0 <= nxt and nxt * 6 < self.H and frac > 0.05:
                mask = MASKS[self.glyph[nxt % self.rows, c]][: self.H - nxt * 6, : self.W - x]
                img[nxt * 6:nxt * 6 + mask.shape[0], x:x + mask.shape[1]][mask] = np.array((120, 200, 130)) * frac ** 2
            for k in range(self.tail[c]):
                row = h - k
                y = row * 6
                if row < 0 or y >= self.H:
                    continue
                if k == 0:
                    colour = (215, 255, 215)
                else:
                    fade = (1 - k / self.tail[c]) ** 1.4
                    colour = (10 * fade, 225 * fade, 80 * fade)
                mask = MASKS[self.glyph[row % self.rows, c]][: self.H - y, : self.W - x]
                img[y:y + mask.shape[0], x:x + mask.shape[1]][mask] = colour
        return img


class Fire(Sim):
    """Fire: the old demo-scene flame, licking up off the bottom edge in separate tongues."""
    group = "Retro"
    rate = 24.0
    PALETTE = np.array([[0, 0, 0], [70, 4, 0], [150, 20, 0], [215, 60, 0], [250, 120, 0],
                        [255, 180, 30], [255, 228, 120], [255, 250, 215]], float)

    def init(self):
        self.heat = np.zeros((self.H + 4, self.W))      # 4 rows below the panel: the raw embers never show
        self.fuel = self.r.uniform(0.6, 1.0, self.W)
        self.drift = self.r.uniform(0, 1, self.W)       # slowly wandering cooling per column: tongues and gaps
        self.cool = 0.55 / self.H
        for _ in range(80):                             # light it before it's seen
            self.step()

    def step(self):
        self.fuel = np.clip(self.fuel + self.r.uniform(-0.2, 0.2, self.W), 0.4, 1.0)
        self.gaps = getattr(self, "gaps", np.zeros(self.W))
        self.gaps = np.clip(self.gaps + self.r.uniform(-0.06, 0.06, self.W), 0, 1)
        self.drift = np.clip(self.drift + self.r.uniform(-0.08, 0.08, self.W), 0, 1)
        lane = np.convolve(np.r_[self.drift[-2:], self.drift, self.drift[:2]], np.ones(5) / 5, "valid")
        smooth_gaps = np.convolve(np.r_[self.gaps[-3:], self.gaps, self.gaps[:3]], np.ones(7) / 7, "valid")
        dying = np.clip((smooth_gaps - 0.5) / 0.12, 0, 1)    # soft-edged lanes that die down to embers
        self.heat[-2:] = self.fuel * (self.r.random(self.W) < 0.92) * (1 - 0.7 * dying)
        h = self.heat
        below = np.roll(h, -1, 0)
        up = (below + np.roll(below, 1, 1) + np.roll(below, -1, 1) + np.roll(h, -2, 0)) / 4.0
        cooling = self.cool * (0.4 + 1.6 * lane)[None, :] + self.r.uniform(0, 0.015, (self.H, self.W))
        h[:-2] = np.clip(up[:-2] - np.r_[cooling, np.zeros((2, self.W))], 0, 1)

    def draw(self):
        v = self.heat[:self.H] * (len(self.PALETTE) - 1)
        i = np.clip(np.floor(v).astype(int), 0, len(self.PALETTE) - 2)
        t = (v - i)[..., None]
        img = self.PALETTE[i] * (1 - t) + self.PALETTE[i + 1] * t
        img[img.max(-1) < 24] = 0                       # no brown haze: dim embers go black
        return img


class Plasma(Sim):
    """Plasma: smooth swirling colour through the whole rainbow, slowly turning."""
    group = "Retro"
    rate = 30.0

    def draw(self):
        t = self.done / self.rate * 0.6
        xs, ys = self.xs * 64 / max(self.W, 1), self.ys * 32 / max(self.H, 1)   # the same swirl on any canvas
        v = (np.sin(xs / 7 + t) + np.sin(ys / 5 - t * 0.8)
             + np.sin((xs + ys) / 9 + t * 1.3)
             + np.sin(np.hypot(xs - 32 - 12 * np.cos(t * 0.7), ys - 16 - 7 * np.sin(t * 0.9)) / 4)) / 4
        return hsv(v * 0.6 + t * 0.04, 0.8, 0.72) * 255       # every hue equally bright, gently (not full glare)


class Pipes(Sim):
    """Pipes: the old screensaver flattened to 2D. Pipes run along a grid in long straight
    stretches with bright elbows, never alongside each other; when the panel is well filled (or
    after a minute) it fades and starts again."""
    group = "Retro"
    rate = 20.0
    DIRS = ((1, 0), (0, 1), (-1, 0), (0, -1))
    FADE = 45
    HEADS = 2
    EVERY = 3                                                # a head moves a cell every 3rd step: about 20 s to build

    def init(self):
        self.gw, self.gh = (self.W + 1) // 2, (self.H + 1) // 2      # lanes 2 LEDs apart
        self.img = np.zeros((self.H, self.W, 3))
        self.used = np.zeros((self.gh, self.gw), bool)
        self.heads, self.fading, self.misses, self.age, self.hold = [], 0, 0, 0, 0

    def free(self, x, y):
        return 0 <= x < self.gw and 0 <= y < self.gh and not self.used[y, x]

    def ok(self, x, y, d, came=None):
        """Can a pipe heading ``d`` enter (x, y)? Free, and not alongside another pipe (or itself)."""
        dx, dy = self.DIRS[d]
        side = [(x + dy, y + dx), (x - dy, y - dx)]
        return self.free(x, y) and all(self.free(sx, sy) for sx, sy in side if (sx, sy) != came)

    def room(self, x, y, d, n=4):
        """Is there a straight run of ``n`` cells from (x, y) heading ``d``?"""
        dx, dy = self.DIRS[d]
        prev = (x, y)
        for k in range(1, n + 1):
            cell = (x + dx * k, y + dy * k)
            if not self.ok(*cell, d, came=prev):
                return False
            prev = cell
        return True

    def new_head(self):
        """A new pipe somewhere with room to run (so no stubs); a quick random look, then every cell
        in turn, so a full picture is known at once (no waiting about before the hold)."""
        tries = [(int(self.r.integers(self.gw)), int(self.r.integers(self.gh)), int(self.r.integers(4))) for _ in range(40)]
        every = [(x, y, d) for y in range(self.gh) for x in range(self.gw) for d in range(4)]
        self.r.shuffle(every)
        for x, y, d in tries + every:
            if self.ok(x, y, d) and self.room(x, y, d):
                dx, dy = self.DIRS[d]
                ahead = [(x + dx * k, y + dy * k) for k in range(1, 5)]
                for cx, cy in [(x, y)] + ahead:              # its first stretch is kept for it, so it never ends a stub
                    self.used[cy, cx] = True
                return {"x": x, "y": y, "d": d, "c": hsv(self.r.random(), 0.75, 1.0) * 255, "run": 0, "len": 0,
                        "ahead": ahead}
        return None

    def step(self):
        if self.fading:
            self.img *= (self.fading - 1) / self.fading     # an even fade to black
            self.fading -= 1
            if not self.fading:
                self.init()
            return
        if self.hold:                                        # the finished picture, a moment before it fades
            self.hold -= 1
            if not self.hold:
                self.fading = self.FADE
            return
        if self.age % self.EVERY == 0:
            self.grow()
        self.age += 1
        full = self.used.mean() > 0.38 or (self.misses and not self.heads)   # no room left anywhere for a new pipe
        if full or self.age > self.rate * 60:                # done: hold it as soon as it's built
            for p in self.heads:
                while p["ahead"]:                            # a new pipe finishes its kept stretch: no stubs
                    p["ahead"].pop(0)
                    self.lay(p, p["d"], (p["x"], p["y"]))
            self.heads = []
            self.hold = int(self.rate * 3)

    def grow(self):
        while len(self.heads) < self.HEADS:
            h = self.new_head()
            if h is None:
                self.misses += 1
                break
            self.misses = 0
            self.heads.append(h)
        alive = []
        for p in self.heads:
            here = (p["x"], p["y"])
            if p["ahead"]:                                   # along its kept first stretch
                p["ahead"].pop(0)
                self.lay(p, p["d"], here)
                alive.append(p)
                continue
            options = []
            for d in (p["d"], (p["d"] + 1) % 4, (p["d"] + 3) % 4):
                dx, dy = self.DIRS[d]
                if not self.ok(p["x"] + dx, p["y"] + dy, d, came=here):
                    continue
                if d != p["d"] and p.get("last_turn") == (d - p["d"]) % 4 and p["run"] < 3:
                    continue                                 # two turns the same way in a row would hairpin
                options.append(d)
            if not options:
                if p["len"] < 4:                             # a stub: take it out again
                    self.unlay(p)
                continue                                     # boxed in: this pipe ends
            if p["d"] in options and (p["run"] < 4 or self.r.random() > 0.12):
                d = p["d"]
            else:
                turns = [d for d in options if d != p["d"]] or options
                d = turns[int(self.r.integers(len(turns)))]
            if d != p["d"]:
                self.img[p["y"] * 2, p["x"] * 2] = np.minimum(255, p["c"] * 0.55 + 120)   # an elbow joint
                p["last_turn"] = (d - p["d"]) % 4
                p["run"] = 0
            self.lay(p, d, here)
            if p["len"] < 60:
                alive.append(p)
        self.heads = alive

    def lay(self, p, d, here):
        """Run the pipe one cell on, heading ``d``."""
        dx, dy = self.DIRS[d]
        x0, y0 = p["x"] * 2, p["y"] * 2
        p["x"], p["y"], p["d"] = p["x"] + dx, p["y"] + dy, d
        p.setdefault("cells", [here]).append((p["x"], p["y"]))
        for k in (1, 2):
            x, y = x0 + dx * k, y0 + dy * k
            if 0 <= x < self.W and 0 <= y < self.H:
                self.img[y, x] = p["c"] * 0.7
        self.used[p["y"], p["x"]] = True
        p["run"] += 1
        p["len"] += 1

    def unlay(self, p):
        for k, (cx, cy) in enumerate(p.get("cells", [(p["x"], p["y"])])):
            self.used[cy, cx] = False
            self.img[max(0, cy * 2 - 1):cy * 2 + 2, max(0, cx * 2 - 1):cx * 2 + 2] = 0

    def draw(self):
        img = self.img.copy()
        if not self.fading:
            for p in self.heads:                             # the growing ends glint
                x, y = p["x"] * 2, p["y"] * 2
                if 0 <= x < self.W and 0 <= y < self.H:
                    img[y, x] = np.minimum(255, p["c"] * 0.5 + 140)
        return img
