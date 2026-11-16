"""Classic: the v2 look. Bouncing blobs traced with marching squares, outlined, blurred."""
import numpy as np
from PIL import ImageDraw, ImageFilter

from bongo.fonts import align_x, middle, names
from bongo.module import Style
from bongo.scroll import Scrolls

from ..common import hm
from .field import field
from .metro import date_font, fitting, top_row
from .palettes import CLASSIC, CLASSIC_SWATCHES, enabled_custom, rgb

# Marching-squares segments per corner pattern (corners: top-left=8, top-right=4,
# bottom-right=2, bottom-left=1). Unchanged from v2.
EDGES = {
    1: [(0, .5), (.5, 0)], 2: [(.5, 0), (1, .5)], 3: [(0, .5), (1, .5)],
    4: [(1, .5), (.5, 1)], 5: [(0, .5), (.5, 0), (1, .5), (.5, 1)], 6: [(.5, 0), (.5, 1)],
    7: [(0, .5), (.5, 1)], 8: [(.5, 1), (0, .5)], 9: [(.5, 0), (.5, 1)],
    10: [(.5, 0), (1, .5), (.5, 1), (0, .5)], 11: [(1, .5), (.5, 1)], 12: [(1, .5), (.5, 0)],
    13: [(.5, 0), (0, .5)], 14: [(0, .5), (.5, 0)],
}


class Classic(Scrolls, Style):
    fps = 33
    choices = {"palettes": ["all", *CLASSIC], "time_font": names(), "date_font": names()}
    swatches = {"palettes": CLASSIC_SWATCHES}
    limits = {"blob_count": (1, 24), "blob_min_radius": (1, 32), "blob_max_radius": (1, 32), "blur_radius": (0, 8),
              "drift": (0, 50), "edge_width": (0, 16), "threshold": (0.1, 10), "time_y": (-64, 256), "date_y": (-64, 256)}
    basic = ("palettes",)
    defaults = dict(
        palettes="all",          # "all" or a list of names from palettes.CLASSIC; enabled custom ones join in
        # drift: top speed of a blob, in pixels per second. v2 moved 0.25 px a frame at the ~10 fps a
        # Pi managed with its pure-Python drawing, so 2.5 px/s keeps v2's pace at any frame rate.
        blob_count=4, blob_min_radius=5, blob_max_radius=10, drift=2.5,
        threshold=1.0, edge_width=4, blur_radius=2,
        time_font="9x18B", date_font="clR6x12", time_y=None, date_y=None,  # None: fit the canvas
        text_colour="#ffffff", border_colour="#000000", date_format="%d %b",
    )

    def setup(self):
        s, rng, w, h = self.settings, self.ctx.rng, self.ctx.width, self.ctx.height
        names = list(CLASSIC) if s["palettes"] == "all" else s["palettes"]
        choices = [CLASSIC[n] for n in names] + [
            (p["background"], p["wax"], p["highlight"]) for p in enabled_custom(s["custom_palettes"])]
        self.outside, self.inside, self.line = (rgb(c) for c in rng.choice(choices))
        self.blobs = [
            [rng.uniform(0, w), rng.uniform(0, h), rng.uniform(-s["drift"], s["drift"]),
             rng.uniform(-s["drift"], s["drift"]),
             rng.uniform(s["blob_min_radius"], s["blob_max_radius"])]
            for _ in range(s["blob_count"])
        ]
        self.ys, self.xs = np.mgrid[0:h, 0:w].astype(float)
        self.last = None  # time of the previous frame: blobs move by real time, not by frames
        self.time_font, self.date_font = self.ctx.fonts(s["time_font"]), self.ctx.fonts(s["date_font"])

    def _step(self, now):
        w, h = self.ctx.width, self.ctx.height
        t = now.timestamp()
        # seconds since the last frame (one frame's worth at first; a long gap doesn't jump)
        step = 1 / self.fps if self.last is None else min(max(t - self.last, 0.0), 0.5)
        self.last = t
        for b in self.blobs:
            b[0] += b[2] * step
            b[1] += b[3] * step
            if not 0 <= b[0] < w:
                b[2], b[0] = -b[2], min(max(b[0], 0), w - 1)
            if not 0 <= b[1] < h:
                b[3], b[1] = -b[3], min(max(b[1], 0), h - 1)

    def render(self, now):
        s = self.settings
        self._step(now)
        img = self.ctx.canvas(self.outside)
        draw = ImageDraw.Draw(img)
        inside = field(self.xs, self.ys, ((b[0], b[1], b[4]) for b in self.blobs)) > s["threshold"]
        # Corner pattern of every cell at once, then draw only the cells that are on.
        squares = (8 * inside[:-1, :-1] + 4 * inside[:-1, 1:] + 2 * inside[1:, 1:] + inside[1:, :-1])
        for y, x in zip(*np.nonzero(squares)):
            square = int(squares[y, x])
            if square == 15:
                draw.rectangle([x, y, x + 1, y + 1], fill=self.inside)
                continue
            edges = EDGES[square]
            for (x1, y1), (x2, y2) in zip(edges[::2], edges[1::2]):
                draw.line((x + x1, y + y1, x + x2, y + y2), fill=self.line, width=s["edge_width"])
        img = img.filter(ImageFilter.BoxBlur(s["blur_radius"]))

        text, border = rgb(s["text_colour"]), rgb(s["border_colour"])
        h = self.ctx.height  # v2 used 15 and 27 on a 32-row panel
        label = hm(now, s["hours"])
        time_font, time_y = self.time_font, s["time_y"] if s["time_y"] is not None else h // 2 - 1
        date_y = s["date_y"] if s["date_y"] is not None else h - 5
        show_date = s["show_date"]
        if h < 26 and s["time_y"] is None and s["date_y"] is None:
            # too short for both lines, as on Lava metro: the time wins, in the middle, in the biggest
            # bold font that leaves a clear row above it
            show_date = False
            for time_font in (self.time_font, *map(self.ctx.fonts, ("9x15B", "7x13B", "6x13B", "6x10"))):
                time_y = middle(time_font, h)
                if time_font.width(label) <= img.width - 2 and top_row(time_font, label, time_y) >= 1:
                    break
        if s["show_time"]:
            time_font = fitting(self.ctx.fonts, time_font, label, img.width)
            time_font.draw(img, align_x(s["time_align"], time_font.width(label), img.width), time_y, label, text, border)
        if show_date:
            date = now.strftime(s["date_format"])
            auto = s["time_y"] is None and s["date_y"] is None and s["show_time"]   # (your own positions: your call)
            # no taller than the room under the time (a clear row between that their outlines share), and
            # a date too long even in a smaller font scrolls and is read through rather than being cut off
            rows = date_y - time_y - 2 if auto else None
            font, fits = date_font(self.ctx.fonts, self.date_font, date, img.width, rows)
            if fits:
                font.draw(img, align_x(s["date_align"], font.width(date), img.width), date_y, date, text, border)
            else:
                Scrolls.line(self, img, font, 1, date_y,  # (self.line is the wax's edge colour here)
                             img.width - 2, date, text, now, outline=border, key=("date", date))
        return img
