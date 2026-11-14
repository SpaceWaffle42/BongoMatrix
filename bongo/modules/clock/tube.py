"""Glass tube: nixie tubes, with glowing orange cathodes, the unlit ones faint behind, a mesh
and sockets. When a digit changes, the old cathode fades as the new one lights."""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
from PIL import Image, ImageFilter

from bongo.module import Style

from .common import HOURS, colon_on, digits, scaled, tall

SIZE = (64, 32)     # the size it is designed at; a safe area gets slimmer tubes drawn to fit
FADE = 0.5          # seconds a cathode takes to hand over to the next


class Nixie(Style):
    """Nixie: four glass tubes with glowing orange digits, the other cathodes faintly behind."""
    group = "Glass tube"
    fps = 10
    defaults = {"hours": "24"}
    choices = {"hours": HOURS}
    basic = ("hours",)
    SECONDS = False

    DIGITS = (("9x18B", 2), ("6x13B", 2), ("9x18B", 1), ("6x10", 1), ("tom-thumb", 1))   # font, times taller
    TUBES = ((4, 2, 4), (3, 2, 3), (2, 1, 3), (2, 1, 2), (2, 1, 1))   # room round the digit, gap, colon gap

    def setup(self):
        self.sets = {}
        self.glyphs = self.digits(*self.DIGITS[0])

    def digits(self, name, scale):
        """The ten digits in ``name``, ``scale`` times taller, as 0-1 arrays (kept once made)."""
        if (name, scale) not in self.sets:
            font = self.ctx.fonts(name)
            self.sets[name, scale] = {ch: np.asarray(tall(font, ch, scale), float) / 255 for ch in "0123456789"}
        return self.sets[name, scale]

    def fit(self, n, colons, W, H):
        """(digits, tube width, gap, colon gap) for ``n`` tubes in W x H: the full-size tubes, else slimmer
        and closer ones, else smaller digits. None when nothing fits (then it's drawn full size and cropped)."""
        for name, scale in self.DIGITS:
            glyphs = self.digits(name, scale)
            gh, gw = (max(g.shape[k] for g in glyphs.values()) for k in (0, 1))
            if gh + 2 > H:
                continue
            for room, gap, colgap in self.TUBES:
                if n * (gw + room) + (n - 1) * gap + colgap * colons <= W:
                    return glyphs, gw + room, gap, colgap
        return None

    def shown(self, now):
        s = digits(now, self.settings["hours"], seconds=self.SECONDS)
        return s[1:] if self.settings["hours"] == "12" and s[0] == "0" else s   # 12-hour: one tube fewer

    def render(self, now):
        s, before = self.shown(now), self.shown(now - dt.timedelta(seconds=1))
        p = min(1.0, (now.microsecond / 1e6) / FADE)
        n = len(s)
        pairs = [n - 3 - 2 * k for k in range((n - 1) // 2)]       # where the colons go (after these tubes)
        W, H = self.ctx.width, self.ctx.height
        fitted = self.fit(n, len(pairs), W, H)
        if fitted is None:
            W, H = SIZE
            fitted = self.fit(n, len(pairs), W, H)
        self.glyphs, tw, gap, colgap = fitted
        total = n * tw + (n - 1) * gap + colgap * len(pairs)
        self.size = (W, H)
        x0 = (W - total) // 2
        base = np.zeros((H, W, 3))
        light = np.zeros((H, W))
        tick = now.timestamp()
        glass = min(26, self.glyphs["8"].shape[0] + 6)             # rows 3 to 29 of a full panel
        top = max(0, (H - glass) // 2)
        bottom = min(H - 1, top + glass)
        x = x0
        lit_rows = None
        self.inside = np.zeros((H, W), bool)
        for i, ch in enumerate(s):
            base[top:bottom, x:x + tw] += (10, 9, 8)
            base[top:bottom, x] += (40, 40, 45)
            base[top:bottom, x + tw - 1] += (25, 25, 30)
            base[top, x + 1:x + tw - 1] += (35, 35, 40)
            base[bottom, x:x + tw] = (55, 45, 40)                   # the socket, just under its tube
            base[top + 2:bottom - 1:2, x + 1:x + tw - 1] += (6, 6, 6)   # the mesh
            ghost = self.place(self.glyphs["8"], x, tw)
            base += ghost[..., None] * np.array([20, 14, 10])
            flicker = 0.94 + 0.06 * math.sin(tick * 9.1 + i * 2.3)
            new = self.place(self.glyphs[ch], x, tw)
            if i < len(before) and before[i] != ch and len(before) == n and p < 1:
                old = self.place(self.glyphs[before[i]], x, tw)
                e = p * p * (3 - 2 * p)                      # ease: the old one dims as the new one glows up
                new = np.maximum(old * (1 - e), new * e)
            light = np.maximum(light, new * flicker)
            ys, xs = np.nonzero(self.place(self.glyphs["8"], x, tw))
            self.inside[ys.min():ys.max() + 1, xs.min() + 1:xs.max()] = True
            if lit_rows is None:
                rows = np.flatnonzero(self.place(self.glyphs["8"], x, tw).any(1))
                lit_rows = (rows.min(), rows.max())
            x += tw + gap
            if i in pairs:                                          # the colon, between tube pairs
                cx = x - gap + (gap + colgap) // 2
                mid = (lit_rows[0] + lit_rows[1]) / 2
                apart = min(4, max(1, (lit_rows[1] - lit_rows[0]) // 4))
                for yy in (round(mid - apart), round(mid + apart)):
                    light[yy, cx] = 0.9 if colon_on(now) else 0.15
                x += colgap
        core = light[..., None] * np.array([255, 150, 60])
        halo = np.asarray(Image.fromarray((light * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.0 if self.SECONDS else 1.6)),
                          float)[..., None] / 255 * np.array([255, 80, 10]) * 1.3
        if self.SECONDS:                                              # keep the glow out of the digits' holes
            halo = halo * np.where(self.inside[..., None], 0.2, 1.0)
        hot = (light[..., None] > 0.75) * np.array([60, 60, 40])
        img = Image.fromarray(np.clip(base + core + halo + hot, 0, 255).astype(np.uint8))
        return scaled(img, self.ctx.width, self.ctx.height)

    def place(self, glyph, x, tw):
        """A glyph centred in the tube at ``x`` as a full-panel layer."""
        W, H = self.size
        out = np.zeros((H, W))
        gh, gw = glyph.shape
        gx = x + (tw - gw) // 2
        gy = (H - gh) // 2
        out[gy:gy + gh, gx:gx + gw] = glyph
        return out


class NixieSeconds(Nixie):
    """Nixie with seconds: six slimmer tubes for hours, minutes and seconds."""
    SECONDS = True
    DIGITS = (("6x10", 3), ("6x10", 2), ("6x10", 1), ("tom-thumb", 1))   # 5 wide, open counters, for the slim tubes
    TUBES = ((3, 1, 3), (2, 1, 2), (2, 1, 1), (2, 0, 1))
