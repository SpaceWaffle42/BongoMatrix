"""Weather: rain on a window, and snow that drifts and melts."""
from __future__ import annotations

import math

import numpy as np

from .sim import Sim, blob, gradient, hsv


class RainOnGlass(Sim):
    """Rain on glass: beads gather on the window over blurred city lights, then run down, leaving a
    clearer wet streak that fades."""
    group = "Weather"
    rate = 20.0

    def init(self):
        n = max(6, self.W // 7)
        self.lights = []
        for k in range(n):                                   # spread across the whole width
            x = (k + self.r.uniform(0.1, 0.9)) * self.W / n
            hue = self.r.uniform(0.55, 0.68) if k % 3 == 1 else self.r.uniform(0.03, 0.12)   # mostly warm, some blue
            self.lights.append((x, self.r.uniform(2, self.H - 4), self.r.uniform(3.5, 6.5),
                                hsv(hue, 0.85, 1.0) * self.r.uniform(160, 210), self.r.uniform(0, 2 * math.pi)))
        self.drops = []          # [x, y, size, falling speed, how fast it gathers, top speed]
        self.wet = np.zeros((self.H, self.W))
        for _ in range(18):
            self.drops.append(self.drop(self.r.uniform(0, self.H), self.r.uniform(0, 1)))
        for _ in range(60):                                  # already raining when it starts
            self.step()

    def drop(self, y, size=0.0):
        return [self.r.uniform(0, self.W), y, size, 0.0, self.r.uniform(0.01, 0.035), self.r.uniform(0.7, 1.4)]

    def step(self):
        if self.r.random() < 0.3:
            self.drops.append(self.drop(self.r.uniform(-1, self.H * 0.7)))
        self.wet *= 0.94
        for d in self.drops:
            d[2] += d[4]                                     # it gathers water...
            if d[2] > 1.0:                                   # ...until it's heavy enough to run
                d[3] = min(d[5], d[3] + 0.04)
                d[1] += d[3]
                d[0] += self.r.uniform(-0.15, 0.15)
                x, y = int(d[0]), int(d[1])
                if 0 <= x < self.W and 0 <= y < self.H:
                    self.wet[y, x] = 1
        self.drops = [d for d in self.drops if d[1] < self.H + 2 and 0 <= d[0] < self.W]

    def draw(self):
        t = self.done / self.rate
        img = np.zeros((self.H, self.W, 3)) + (5, 6, 14)
        for x, y, r, colour, ph in self.lights:             # the lights breathe a little
            img += blob(self.xs, self.ys, x, y, r * r)[..., None] * colour * (0.85 + 0.15 * math.sin(t * 0.6 + ph))
        img = img * (1 + 0.45 * self.wet[..., None]) + 8 * self.wet[..., None]   # a streak: the lights show clearer
        for x, y, size, *_ in self.drops:
            x, y = int(x), int(y)
            if not (0 <= x < self.W and 0 <= y < self.H):
                continue
            b = min(1.0, 0.4 + size * 0.6)
            img[y, x] = img[y, x] * 0.3 + np.array((200, 215, 240)) * b
            if size > 0.6 and y + 1 < self.H:                # a bigger bead: two LEDs, darker underneath
                img[y + 1, x] = img[y + 1, x] * 0.3 + np.array((90, 100, 130)) * b
        return img


class Snow(Sim):
    """Snow: flakes drift down in two layers and build into soft drifts; once it's deep, it melts
    away and starts again."""
    group = "Weather"
    rate = 20.0

    def init(self):
        self.sky = gradient(self.H, (5, 6, 16), (12, 16, 34))
        self.pile = np.zeros(self.W)
        self.flakes = []        # [x, y, fall speed, sway phase, near]
        self.melting = False
        self.new_lean()
        for _ in range(int(self.H * 2.2)):                   # already snowing when it starts
            self.spawn(self.r.uniform(-1, self.H))

    def new_lean(self):
        """Where the wind drops more snow: two or three broad mounds, different each time."""
        x = np.arange(self.W)
        ph = self.r.uniform(0, 2 * math.pi, 2)
        f = self.r.uniform(1.0, 1.8)
        lean = 1 + 0.7 * np.sin(x / self.W * 2 * math.pi * f + ph[0]) + 0.3 * np.sin(x / self.W * 2 * math.pi * 3.1 + ph[1])
        self.lean = np.clip(lean, 0.15, None) / np.clip(lean, 0.15, None).sum()

    def spawn(self, y=-1.0):
        near = self.r.random() < 0.45
        x = self.r.choice(self.W, p=self.lean) + self.r.uniform(0, 1) if near else self.r.uniform(0, self.W)
        self.flakes.append([x, y, self.r.uniform(0.35, 0.6) if near else self.r.uniform(0.18, 0.3),
                            self.r.uniform(0, 6), near])

    def step(self):
        for _ in range(int(self.r.random() < 0.6) + int(self.r.random() < 0.25)):
            self.spawn()
        t = self.done / self.rate
        keep = []
        for f in self.flakes:
            f[1] += f[2]
            f[0] = (f[0] + 0.12 * math.sin(t * 2 + f[3])) % self.W
            x = int(f[0])
            if f[4] and f[1] >= self.H - 1 - self.pile[x]:  # only the near layer lands
                self.pile[x] += 0.5
                for nb in (x - 1, x + 1):                   # slides off a side that's 2 or more higher
                    if 0 <= nb < self.W and self.pile[x] - self.pile[nb] >= 2:
                        self.pile[x] -= 0.5
                        self.pile[nb] += 0.5
            elif f[1] < self.H:
                keep.append(f)
        self.flakes = keep
        if self.pile.max() > self.H * 0.38:
            self.melting = True
        if self.melting:
            self.pile = np.maximum(0, self.pile - 0.05)
            self.melting = self.pile.max() > 0
            if not self.melting:                             # the wind changes for the next fall
                self.new_lean()

    def draw(self):
        img = np.broadcast_to(self.sky, (self.H, self.W, 3)).copy()
        for x, y, _, _, near in self.flakes:
            x, y = int(x), int(y)
            if 0 <= y < self.H:
                img[y, x] = (235, 240, 255) if near else (110, 120, 150)
        for x in range(self.W):
            h = int(self.pile[x])
            if h:
                img[self.H - h:, x] = (120, 128, 150)        # soft inside...
                img[self.H - h, x] = (225, 232, 250)          # ...the top catches the light
            part = self.pile[x] - h
            if part > 0.1 and h < self.H:
                img[self.H - h - 1, x] = img[self.H - h - 1, x] * (1 - part) + np.array((200, 210, 235)) * part
        return img
