"""Patterns: the Game of Life, falling sand and a maze that builds and solves itself."""
from __future__ import annotations

import math
from collections import deque

import numpy as np

from .sim import Sim, hsv


class Life(Sim):
    """Game of Life: Conway's automaton on a wrap-around board; colour shows how long a cell has
    lived. When it settles into a loop or dies out, new life is sprinkled in."""
    group = "Patterns"
    rate = 5.0
    fps = 10

    def init(self):
        self.alive = np.zeros((self.H, self.W), bool)       # a few clusters, not a screen of noise
        for _ in range(max(3, self.W * self.H // 300)):
            y, x = int(self.r.integers(self.H)), int(self.r.integers(self.W))
            ys, xs = (np.arange(6)[:, None] + y) % self.H, (np.arange(6)[None, :] + x) % self.W
            self.alive[ys, xs] |= self.r.random((6, 6)) < 0.45
        self.age = np.zeros((self.H, self.W))
        self.glow = np.where(self.alive[..., None], hsv(0.55, 0.8, 1.0) * 255, 0.0)   # drawn like the rest from the start
        self.seen = deque(maxlen=24)

    def step(self):
        g = self.alive
        n = sum(np.roll(np.roll(g, i, 0), j, 1).astype(int) for i in (-1, 0, 1) for j in (-1, 0, 1) if i or j)
        g = (n == 3) | (g & (n == 2))
        self.age = np.where(g, self.age + 1, 0)
        key = g.tobytes()
        if key in self.seen or g.mean() < 0.04:            # stuck or nearly empty: drop in a few new patches
            for _ in range(int(self.r.integers(1, 4))):
                y, x = int(self.r.integers(self.H)), int(self.r.integers(self.W))
                patch = self.r.random((5, 5)) < 0.45
                ys, xs = (np.arange(5)[:, None] + y) % self.H, (np.arange(5)[None, :] + x) % self.W
                g[ys, xs] |= patch
            self.seen.clear()
        self.seen.append(key)
        self.alive = g
        colour = hsv(0.55 - np.minimum(self.age, 30) / 60, 0.8, 1.0) * 255
        self.glow = np.where(g[..., None], colour, self.glow * 0.55)   # the dead fade out

    def draw(self):
        return self.glow


class Sand(Sim):
    """Falling sand: two pours of slowly changing colour build dunes; when they're deep, the pours
    stop, a gap opens in the middle of the floor and widens, and it all funnels out."""
    group = "Patterns"
    rate = 30.0

    def init(self):
        self.full = np.zeros((self.H, self.W), bool)
        self.colour = np.zeros((self.H, self.W, 3))
        self.hue = self.r.random()
        self.draining = False
        self.gap = 0.0
        self.rest = 0

    def step(self):
        t = self.done / self.rate
        self.hue = (self.hue + 0.0015) % 1
        if self.rest:                                        # a moment of empty panel after draining
            self.rest -= 1
        elif not self.draining and (self.done % 3 == 0 or self.full.mean() < 0.08):   # quick to start, slow to build
            for k, x in enumerate((self.W * (0.25 + 0.12 * np.sin(t / 2.3)), self.W * (0.72 + 0.12 * np.cos(t / 3.1)))):
                x = int(np.clip(x, 0, self.W - 1))
                if not self.full[0, x]:
                    self.full[0, x] = True
                    self.colour[0, x] = hsv(self.hue + k * 0.4, 0.6, self.r.uniform(0.75, 1.0)) * 255
        elif self.draining:
            self.gap = min(self.W / 2, self.gap + 0.06)      # the floor opens slowly from the middle
            lo, hi = int(self.W / 2 - self.gap), int(math.ceil(self.W / 2 + self.gap))
            self.full[-1, lo:hi] = False
            self.colour[-1, lo:hi] = 0
        for _ in range(3 if self.draining else 1):          # draining: grains slide faster into the gap
            self.fall()
        f = self.full
        if not self.draining and f.mean() > 0.45:
            self.draining, self.gap = True, 0.0
        if self.draining and not f.any():
            self.draining, self.rest = False, int(self.rate * 1.5)

    def fall(self):
        f, c = self.full, self.colour
        for y in range(self.H - 2, -1, -1):                  # bottom up, so a grain moves once a step
            row = np.flatnonzero(f[y])
            if not len(row):
                continue
            down = row[~f[y + 1, row]]
            f[y + 1, down], c[y + 1, down] = True, c[y, down]
            f[y, down], c[y, down] = False, 0
            stuck = np.setdiff1d(row, down)
            pad_below, pad_row = np.r_[True, f[y + 1], True], np.r_[True, f[y], True]
            can = ~(pad_below[stuck] | pad_row[stuck]) | ~(pad_below[stuck + 2] | pad_row[stuck + 2])
            for x in self.r.permutation(stuck[can]):        # blocked: slide down a side (only grains that can)
                for dx in (-1, 1) if self.r.random() < 0.5 else (1, -1):
                    nx = x + dx
                    if 0 <= nx < self.W and not f[y + 1, nx] and not f[y, nx]:
                        f[y + 1, nx], c[y + 1, nx] = True, c[y, x]
                        f[y, x], c[y, x] = False, 0
                        break

    def draw(self):
        return self.colour


class Maze(Sim):
    """Maze: carves a new maze, then solves it with a rainbow trail, holds a moment, and fades
    into the next one."""
    group = "Patterns"
    rate = 20.0                                              # one step a frame, so the sparkle moves a cell at a time
    FADE = 50            # steps the finished maze takes to fade

    def init(self):
        self.cw, self.ch = max(2, (self.W // 2 - 1) // 2), max(2, (self.H // 2 - 1) // 2)
        self.ox = (self.W - (2 * self.cw + 1) * 2) // 2
        self.oy = (self.H - (2 * self.ch + 1) * 2) // 2
        self.new_maze()
        self.age = 20                                        # the first maze is there from the start

    def new_maze(self):
        cw, ch = self.cw, self.ch
        open_ = np.zeros((2 * ch + 1, 2 * cw + 1), bool)
        open_[1, 1] = True
        steps, stack, seen = [], [(0, 0)], {(0, 0)}
        while stack:                                         # depth-first carving
            x, y = stack[-1]
            nbrs = [(x + dx, y + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
                    if 0 <= x + dx < cw and 0 <= y + dy < ch and (x + dx, y + dy) not in seen]
            if nbrs:
                nx, ny = nbrs[self.r.integers(len(nbrs))]
                seen.add((nx, ny))
                stack.append((nx, ny))
                steps += [(x + nx + 1, y + ny + 1), (2 * nx + 1, 2 * ny + 1)]
            else:
                stack.pop()
        for sx, sy in steps:
            open_[sy, sx] = True
        goal = (2 * cw - 1, 2 * ch - 1)
        prev, q = {(1, 1): None}, deque([(1, 1)])
        while q:                                             # breadth-first solving
            c = q.popleft()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = (c[0] + dx, c[1] + dy)
                if open_[nb[1], nb[0]] and nb not in prev:
                    prev[nb] = c
                    q.append(nb)
        path, c = [], goal
        while c:
            path.append(c)
            c = prev[c]
        self.steps, self.path, self.goal = steps, path[::-1], goal
        self.carve_per = max(1, round(len(steps) / (self.rate * 5)))   # carving takes about 5 s
        self.solve_per = max(1, round(len(self.path) / (self.rate * 3)))
        self.carved, self.solved, self.hold, self.fade, self.age = 0, 0, 0, 0, 0

    def step(self):
        self.age += 1
        if self.carved < len(self.steps):
            self.carved = min(len(self.steps), self.carved + self.carve_per)
        elif self.solved < len(self.path):
            self.solved = min(len(self.path), self.solved + self.solve_per)
        elif self.hold < len(self.path) + 3 + self.rate * 1.5:   # the sparkle runs the whole way, then it rests a moment
            self.hold += 1
        elif self.fade < self.FADE:
            self.fade += 1
        else:
            self.new_maze()

    def draw(self):
        open_ = np.zeros((2 * self.ch + 1, 2 * self.cw + 1), bool)
        open_[1, 1] = True
        for sx, sy in self.steps[:self.carved]:
            open_[sy, sx] = True
        near = open_.copy()                                  # walls beside a carved passage light up; the rest waits dim
        near[1:] |= open_[:-1]
        near[:-1] |= open_[1:]
        near[:, 1:] |= open_[:, :-1]
        near[:, :-1] |= open_[:, 1:]
        g = np.where(near[..., None], np.array((30, 60, 140.0)), np.array((12, 24, 56.0)))
        g[open_] = 0
        if self.carved < len(self.steps) and self.carved:
            sx, sy = self.steps[self.carved - 1]
            g[sy, sx] = (255, 255, 255)                      # the carving head
        for i, (px, py) in enumerate(self.path[:self.solved]):
            g[py, px] = hsv(i / len(self.path) * 0.8, 0.9, 1.0) * 255
        if self.solved < len(self.path):
            gx, gy = self.goal
            g[gy, gx] = (255, 200, 0) if (self.done // 8) % 2 else (120, 90, 0)
        elif self.hold and not self.fade:                    # solved: a sparkle runs along the way through
            head = self.hold - 1                             # one cell a step from the start, with a fading tail
            for k, fade in ((0, 1.0), (1, 0.55), (2, 0.25)):
                if 0 <= head - k < len(self.path):
                    px, py = self.path[head - k]
                    g[py, px] = g[py, px] * (1 - fade) + np.array((255, 255, 255)) * fade
        img = np.zeros((self.H, self.W, 3))
        big = np.kron(g, np.ones((2, 2, 1)))
        img[self.oy:self.oy + big.shape[0], self.ox:self.ox + big.shape[1]] = big
        return img * (1 - self.fade / self.FADE) * min(1.0, (self.age + 1) / 20)   # a new maze fades in
