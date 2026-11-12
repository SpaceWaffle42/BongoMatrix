"""Sky: starfield, aurora, fireflies, an ocean sunset and a flock of starlings."""
from __future__ import annotations

import math

import numpy as np

from .sim import Sim, blob, gradient, hsv


class Starfield(Sim):
    """Starfield: stars stream out of the middle towards you, brightening and streaking as they
    pass."""
    group = "Sky"
    rate = 30.0

    def init(self):
        self.n = max(24, self.W * self.H // 24)
        self.stars = np.c_[self.r.uniform(-1, 1, (self.n, 2)), self.r.uniform(0.05, 1, self.n)]

    def step(self):
        self.stars[:, 2] -= 0.008
        gone = self.stars[:, 2] < 0.04
        k = int(gone.sum())
        if k:
            self.stars[gone] = np.c_[self.r.uniform(-1, 1, (k, 2)), np.ones(k)]

    def draw(self):
        img = np.zeros((self.H, self.W, 3))
        cx, cy, sx, sy = self.W / 2, self.H / 2, self.W / 4, self.H / 4
        for x, y, z in self.stars:
            b = min(1.0, max(0.0, (1 - z) * 1.6))              # far stars start dark and brighten as they come
            if b < 0.04:
                continue
            for tz, a in ((z + 0.05, 0.3), (z + 0.025, 0.55), (z, 1.0)):     # a short streak behind it
                px, py = int(cx + x / tz * sx), int(cy + y / tz * sy)
                if 0 <= px < self.W and 0 <= py < self.H:
                    img[py, px] = np.maximum(img[py, px], np.array((215, 225, 255)) * b * a)
        return img


class Aurora(Sim):
    """Aurora: green and violet curtains hang from bright lower edges and ripple slowly over a dark
    ridge, fading out before they reach it."""
    group = "Sky"
    rate = 20.0

    def init(self):
        self.sky = gradient(self.H, (1, 2, 8), (5, 9, 24))
        x = np.arange(self.W)
        ridge = self.H * (0.80 + 0.06 * np.sin(x / 9 + 1) + 0.03 * np.sin(x / 3.7)) - 1
        self.ground = self.ys >= ridge[None, :]
        self.rim = (self.ys >= ridge[None, :]) & (self.ys < ridge[None, :] + 1)
        self.rays = [self.r.uniform(0.6, 1.0, self.W) for _ in range(3)]   # per-column ray strength
        above = np.clip(ridge[None, :] - self.ys, 0, None)
        self.base = np.clip((above - 1) / 5, 0, 1) ** 2        # curtains fade out just above the ridge (no bright rim on it)

    def step(self):
        for k in range(3):
            self.rays[k] = np.clip(self.rays[k] + self.r.uniform(-0.05, 0.05, self.W), 0.55, 1.0)

    def draw(self):
        t = self.done / self.rate * 0.35
        xs, ys = self.xs * 64 / self.W, self.ys * 32 / self.H
        img = np.broadcast_to(self.sky, (self.H, self.W, 3)).copy()
        for k, (hue, amp, edge0, length) in enumerate(((0.36, 1.0, 17, 11), (0.80, 0.55, 12, 8), (0.45, 0.75, 20, 9))):
            edge = edge0 + 1.8 * np.sin(xs / 13 + t * (0.8 + 0.3 * k) + k * 2) + 0.8 * np.sin(xs / 5 - 1.5 * t + k)
            above = np.clip(edge - ys, 0, None)
            soft = np.clip(above / 4, 0, 1)
            soft = soft * soft * (3 - 2 * soft)              # brightest a few rows up, easing off over the last four
            curtain = np.where(ys <= edge, np.exp(-above / length) * (0.2 + 0.8 * soft), 0.2 * np.exp(-(ys - edge) / 1.5))
            ripple = np.clip(0.25 + 0.75 * np.sin(xs / 4 + 2.5 * t + k * 1.7), 0, 1) * np.clip(ys / 5, 0, 1)  # folds, dark sky above
            ray = np.interp(xs[0], np.linspace(0, 64, self.W), self.rays[k])[None, :]
            img += hsv(hue, 0.8, 1.0) * ((curtain * ripple * ray) ** 1.6 * amp * self.base)[..., None] * 235   # bright folds, dark gaps
        img[self.ground] = 0
        return img


class Fireflies(Sim):
    """Fireflies: soft lights drift and pulse over dark swaying grass. Very calm."""
    group = "Sky"
    rate = 20.0

    def init(self):
        n = max(6, self.W * self.H // 128)
        self.x = self.r.uniform(0, self.W, n)
        self.y = self.r.uniform(self.H * 0.35, self.H * 0.8, n)
        self.a = self.r.uniform(0, 2 * math.pi, n)
        self.phase = self.r.permutation(np.linspace(0, 2 * math.pi, n, endpoint=False))   # evenly spread flashes
        self.pace = self.r.uniform(0.97, 1.03, n)
        self.blade = self.H - 1 - (np.abs(np.sin(np.arange(self.W) * 1.7)) * 2.5 + self.r.uniform(0, 3, self.W))
        self.sky = gradient(self.H, (2, 2, 10), (6, 9, 22))
        self.top, self.low = 4.0, self.H - 8.0       # clear of the grass tops

    def step(self):
        self.a += self.r.uniform(-0.3, 0.3, len(self.a))
        self.x = self.x + 0.22 * np.cos(self.a)
        side = (self.x < 3) | (self.x > self.W - 4)
        self.a[side] = math.pi - self.a[side]                 # turn back from the sides
        self.x = np.clip(self.x, 3, self.W - 4)
        self.y += 0.16 * np.sin(self.a)
        d = np.hypot(self.x[:, None] - self.x[None], self.y[:, None] - self.y[None]) + np.eye(len(self.x)) * 99
        close = d < 8                                        # keep room between them, always
        self.x += 0.35 * np.sum(np.sign(self.x[:, None] - self.x[None]) * close, 1)
        self.y += 0.25 * np.sum(np.sign(self.y[:, None] - self.y[None]) * close, 1)
        out = (self.y < self.top) | (self.y > self.low)
        self.a[out] = -self.a[out]                           # bounce softly off the top and the grass
        self.y = np.clip(self.y, self.top, self.low)

    def draw(self):
        t = self.done / self.rate
        img = np.broadcast_to(self.sky, (self.H, self.W, 3)).copy()
        wind = 1.6 * np.sin(t * 0.9 - np.arange(self.W) * 0.18)   # a slow wave of wind along the grass
        for x in range(self.W):
            top = int(self.blade[x])
            for y in range(max(0, top), self.H):
                k = (y - top) / max(1, self.H - top)
                lean = int(round(wind[x] * max(0.0, 1 - (y - top) / 2.5)))   # tips lean, roots stay
                img[y, min(self.W - 1, max(0, x + lean))] = (14 - 8 * k, 52 - 30 * k, 18 - 10 * k)
        glows = np.maximum(0.0, np.sin(t * 1.6 * self.pace + self.phase)) ** 2
        for i in np.argsort(-glows):                          # a dimmer one near a brighter one stays dark
            near = np.hypot(self.x - self.x[i], self.y - self.y[i]) < 7
            near[i] = False
            if np.any(near & (glows > glows[i])):
                glows[i] = 0
        for x, y, glow in zip(self.x, self.y, glows):
            if glow > 0.01:
                img += blob(self.xs, self.ys, x, y, 2.5)[..., None] * np.array((200, 235, 70)) * glow
        return img


class Ocean(Sim):
    """Ocean sunset: a low sun half set on a flat horizon, swell rolling in towards you, and a
    path of glints across the water."""
    group = "Sky"
    rate = 20.0

    def init(self):
        self.horizon = round(self.H / 2)
        g = (self.ys / self.horizon)[..., None]
        top, mid, low = np.array((25, 12, 70.0)), np.array((170, 60, 100)), np.array((255, 150, 60))
        sky = np.where(g < 0.55, top * (1 - g / 0.55) + mid * (g / 0.55), mid * (1 - (g - 0.55) / 0.45) + low * ((g - 0.55) / 0.45))
        self.sun = (self.W * 0.62, self.horizon)
        sky += blob(self.xs, self.ys, *self.sun, 20 * self.H / 32)[..., None] * (255, 200, 90)
        self.sky = sky
        depth = np.clip((self.ys - self.horizon) / (self.H - self.horizon), 0, 1)[..., None]
        near, mid, far = np.array((8, 22, 60.0)), np.array((70, 40, 80.0)), np.array((165, 82, 62.0))
        self.water = np.where(depth < 0.3, far * (1 - depth / 0.3) + mid * (depth / 0.3),
                              mid * (1 - (depth - 0.3) / 0.7) + near * ((depth - 0.3) / 0.7))   # the sky's orange on the water
        self.glints = []
        for _ in range(10):
            self.step()

    def draw(self):
        t = self.done / self.rate * 0.5
        h = self.horizon
        img = np.where((self.ys < h)[..., None], self.sky, self.water)
        xs = self.xs * 64 / self.W
        for k, (row, shade) in enumerate(((h + 3, 0.9), (h + 7, 0.75), (h + 12, 0.6))):   # swell, nearer and bigger
            if row >= self.H:
                continue
            amp = 0.6 + 0.5 * k
            crest = row + amp * np.sin(xs / (6 + k * 2) + t * (1 + k * 0.4) * (1 if k % 2 else -1))
            band = (self.ys >= crest) & (self.ys < crest + 1)    # one row: foam stays horizontal
            img = np.where(band[..., None], img * shade, img)
            foam = band & (np.sin(xs * 0.7 + t * 2.5 + k * 2) * np.sin(xs * 0.23 - t * 0.9 + k) > 0.8)
            img = np.where(foam[..., None], img * 0.6 + np.array((120, 80, 70)), img)
        swell = 1 + 0.12 * np.sin(self.xs / 9 - t * 1.6 + (self.ys - h) * 0.6)   # the water rolls
        img = np.where((self.ys >= h)[..., None], img * swell[..., None], img)
        for x, y, n, life, age in self.glints:               # the sun's path: short dashes, each on its own time
            d = (y - h) / max(1, self.H - h)
            k = math.sin(math.pi * age / life)
            colour = np.array((255, 245, 200)) * (1 - d) + np.array((255, 160, 80)) * d   # white-gold to amber
            for j in range(n):
                if 0 <= x + j < self.W:
                    img[y, x + j] = img[y, x + j] * (1 - k) + colour * k
        return img

    def step(self):
        h = self.horizon
        self.glints = [[x, y, n, life, age + 1] for x, y, n, life, age in self.glints if age + 1 < life]
        for _ in range(3):
            y = h + int(self.r.integers(0, self.H - h))
            width = 1.5 + 9 * (y - h) / max(1, self.H - h)    # a cone, narrow at the horizon
            x = int(round(self.sun[0] + self.r.normal(0, width / 2)))
            if any(abs(gy - y) <= 1 and abs(gx - x) <= 3 for gx, gy, *_ in self.glints):
                continue                                     # dashes never touch
            self.glints.append([x, y, int(self.r.integers(1, 4)), int(self.r.integers(3, 9)), 0])


class Starlings(Sim):
    """Starlings: a flock wheels and folds against a dusk sky, following a slowly wandering point."""
    group = "Sky"
    rate = 25.0
    COHESION, ALIGN = 0.003, 0.2
    EDGE = (0.15, 0.3)                                       # how hard they turn away from the sides, top and bottom
    HAWK_EVERY, HAWK_R, HAWK_PUSH = 3.5, 11.0, 0.6

    def init(self):
        n = max(24, self.W * self.H // 24)
        centre = np.array((self.W / 2, self.H / 2))
        a = self.r.uniform(0, 2 * math.pi)
        head = np.array((math.cos(a), math.sin(a) * 0.4))
        head /= np.hypot(*head)
        along = self.r.uniform(-self.W * 0.25, self.W * 0.25, (n, 1))
        self.p = centre + along * head + self.r.normal(0, 1.2, (n, 2))   # one flock from the start, already a ribbon
        self.v = np.tile(head * 0.6, (n, 1)) + self.r.normal(0, 0.08, (n, 2))
        self.trail = np.zeros((self.H, self.W))
        self.sky = gradient(self.H, (60, 50, 110), (235, 120, 70))
        self.goal_a = self.r.uniform(0, 2 * math.pi)
        self.centre = self.p.mean(0)

    def hawk(self, t):
        """Where the hawk is (every few seconds it crosses the flock in about 1.5 s), or None."""
        k, f = divmod(t, self.HAWK_EVERY)
        if f > 1.5:
            return None
        a = self.goal_a + k * 2.1
        d = np.array((math.cos(a), math.sin(a)))
        return self.centre + d * (f / 1.5 - 0.5) * 30

    def step(self):
        p, v = self.p, self.v
        t = self.done / self.rate
        if t % self.HAWK_EVERY < 1 / self.rate:
            self.centre = p.mean(0)
        goal = np.array((self.W / 2 + self.W * 0.3 * math.sin(t * 0.37 + self.goal_a),
                         self.H / 2 + self.H * 0.12 * math.sin(t * 0.59)))       # quick enough to draw it into ribbons
        d = p[:, None] - p[None]
        dist = np.hypot(d[..., 0], d[..., 1]) + np.eye(len(p)) * 99
        near = (dist < 16).astype(float)
        count = near.sum(1, keepdims=True)
        mean_p = np.where(count > 0, near @ p / np.maximum(count, 1), p)
        mean_v = np.where(count > 0, near @ v / np.maximum(count, 1), v)
        sep = (d * (dist < 2).astype(float)[..., None]).sum(1)
        heading = v.mean(0)
        heading = heading / max(1e-6, np.hypot(*heading))
        side = np.array((-heading[1], heading[0]))
        pull = mean_p - p
        pull = pull - (pull @ heading)[:, None] * heading[None] * 0.85   # cohesion mostly across the heading: a ribbon, not a ball
        v += pull * self.COHESION + (mean_v - v) * self.ALIGN + sep * 0.03 + (goal - p.mean(0)) * 0.004   # the whole flock steers for the goal, so it doesn't bunch
        hawk = self.hawk(t)
        if hawk is not None:                                 # now and then a hawk cuts through: they scatter round it and stretch
            away = p - hawk
            dh = np.hypot(away[:, 0], away[:, 1])[:, None]
            v += away / np.maximum(dh, 1e-6) * np.clip((self.HAWK_R - dh) / self.HAWK_R, 0, None) * self.HAWK_PUSH
        along = (p - p.mean(0)) @ heading                   # a wave of turning runs back along the flock
        v += side[None, :] * (0.1 * np.sin(t * 1.3 - along / 5))[:, None]
        v += self.r.normal(0, 0.04, v.shape)                # each bird a little its own way
        m = np.array((10.0, 8.0))                            # they start turning well before an edge, a little at a time
        v += (np.clip((m - p) / m, 0, 1) - np.clip((p - (np.array((self.W, self.H)) - m)) / m, 0, 1)) * np.array(self.EDGE)
        s = np.hypot(v[:, 0], v[:, 1])[:, None]
        v[:] = v / np.maximum(s, 1e-6) * np.clip(s, 0.45, 0.9)
        p += v
        self.trail *= 0.6
        for x, y in p:
            if 0 <= int(x) < self.W and 0 <= int(y) < self.H:
                self.trail[int(y), int(x)] = 1

    def draw(self):
        return self.sky * (1 - 0.9 * self.trail[..., None])   # dark birds against the glow
