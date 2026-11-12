"""Pet styles: each mode is a style, Life strings modes together, Wander is an overlay."""
from __future__ import annotations

import bisect
import hashlib

from PIL import Image, ImageDraw

from bongo.module import Style

from .art import H, W, load, missing, no_art
from bongo.pets import wearing

from .scenes import MODES, STRIDE, record

# The engine's 3x5 font: tiny, and the same on every panel.
FONT = {
    "0": "111101101101111", "1": "010110010010111", "2": "111001111100111", "3": "111001111001111",
    "4": "101101111001001", "5": "111100111001111", "6": "111100111101111", "7": "111001001001001",
    "8": "111101111101111", "9": "111101111001111", ":": "000010000010000", " ": "000000000000000",
    "A": "010101111101101", "B": "110101110101110", "C": "011100100100011", "D": "110101101101110",
    "E": "111100110100111", "F": "111100110100100", "G": "011100101101011", "H": "101101111101101",
    "I": "111010010010111", "J": "001001001101010", "K": "101101110101101", "L": "100100100100111",
    "M": "101111111101101", "N": "110101101101101", "O": "010101101101010", "P": "110101110100100",
    "Q": "010101101110011", "R": "110101110101101", "S": "011100010001110", "T": "111010010010010",
    "U": "101101101101111", "V": "101101101101010", "W": "101101111111101", "X": "101101010101101",
    "Y": "101101010010010", "Z": "111001010100111", "!": "010010010000010", "?": "110001010000010",
    "'": "010010000000000", "-": "000000111000000", ".": "000000000000010",
}


def text(draw, x, y, s, colour):
    for ch in s.upper():
        for i, bit in enumerate(FONT.get(ch, FONT[" "])):
            if bit == "1":
                draw.point((x + i % 3, y + i // 3), fill=colour)
        x += 4
    return x


def overlay(draw, item, t, shot):
    """Speech bubbles, Zs, banners and the name tag, drawn over the scene at ``t`` ms."""
    kind, cx, cy = item[0], int(shot.cat[0]), int(shot.cat[1])
    if kind == "zzz":
        for k in range(2):
            p = ((t / 120) + k * 12) % 24
            if p < 20:
                # from her head: the sleeping poses face left, head about 10 px left of her feet point
                text(draw, max(1, cx - 12 - int(p) // 4), cy - 14 - int(p) // 2, "Z", item[1])  # never off the left edge
    elif kind == "bubble" and (t // 600) % 2 == 0:
        x, y = cx + 6, cy - 22
        draw.rectangle([x - 1, y - 1, x + 3, y + 5], fill=(255, 250, 240))
        draw.point((x, y + 6), fill=(255, 250, 240))
        text(draw, x, y, item[1], (200, 60, 40))
    elif kind == "banner":
        width = len(item[1]) * 4
        draw.rectangle([0, 0, W - 1, 6], fill=(26, 19, 0))
        text(draw, W - int(t / 1000 * 12) % (W + width), 1, item[1], (255, 181, 71))
    elif kind == "tag" and t >= item[2]:
        name, rise = item[1], min(4, int((t - item[2]) / 80))
        tw = len(name) * 4 - 1
        x = max(1, min(W - tw - 2, cx - tw // 2))
        y = max(0, cy - 27 + (4 - rise))
        draw.rectangle([x - 1, y, x + tw, y + 6], fill=(26, 19, 0))
        text(draw, x, y + 1, name, (255, 227, 179))
    elif kind == "dust" and shot.moving and int(t / 40) % 2 == 0:
        bx = cx - 11 * shot.moving
        draw.point((bx, cy - 1), fill=(240, 220, 190))
        draw.point((bx - 2 * shot.moving, cy - 3), fill=(240, 220, 190))
    elif kind == "grumble" and (t // 330) % 2 == 0:
        for k in range(5):
            draw.point((cx - 2 + k, cy - 24 + (k + int(t / 110)) % 3), fill=(90, 90, 110))


def fit(scene: Image.Image, width: int, height: int) -> Image.Image:
    """The room is drawn at 64x32; a smaller safe area shows its middle, floor kept."""
    if (width, height) == (W, H):
        return scene
    x = max(0, (W - width) // 2)
    y = max(0, H - height)
    return scene.crop((x, y, x + width, y + height))


class _Scene(Style):
    fps = 20
    MODE: tuple[str, ...] = ()

    def setup(self):
        self.art = None
        if missing(self.ctx.pet.kind):                  # a public build before a pack is imported
            return
        self.art = load(self.ctx.pet.kind, self.ctx.pet.coat, wearing(self.ctx.pet, self.ctx.now().date()))
        self.timeline = [(shot, mode) for mode in self.modes() for shot in record(self.art, mode, self.ctx.pet.name)]
        self.starts, t = [], 0.0
        for shot, _ in self.timeline:
            self.starts.append(t)
            t += shot.ms
        self.total = t
        # where each mode's run begins, so overlays (Zs, banners) count time from their own scene
        self.run_start, begin = [], 0.0
        for k, (shot, mode) in enumerate(self.timeline):
            if k and self.timeline[k - 1][1] != mode:
                begin = self.starts[k]
            self.run_start.append(begin)

    def modes(self):
        return list(self.MODE)

    def render(self, now):
        if self.art is None:
            return no_art(self.ctx.width, self.ctx.height, self.ctx.fonts)
        t = (now.timestamp() * 1000) % self.total
        k = bisect.bisect_right(self.starts, t) - 1
        shot = self.timeline[k][0]
        art = self.art
        img = {"day": art.room, "empty": art.empty, "night": art.night}[shot.room].convert("RGBA")
        if shot.ball:
            img.alpha_composite(art.ball, (int(shot.ball[0]) - 4, int(shot.ball[1]) - 4))
        # a bee flies in front of her; a toy on the floor is behind her, except beside a sitting cat,
        # where it would hide under her tail
        flying = shot.toy and (shot.toy[0] == "bee" or shot.tag in ("meow_sit", "wash_sit"))
        if shot.toy and not flying:
            self.draw_toy(img, shot.toy, t)
        frame = art.animations[shot.tag][shot.index]
        x, y = int(round(shot.cat[0])) + frame.dx, int(round(shot.cat[1])) + frame.dy
        layer = Image.new("RGBA", (W, H))
        layer.paste(frame.image, (x, y), frame.image)
        img.alpha_composite(layer)
        if flying:
            self.draw_toy(img, shot.toy, t)
        draw = ImageDraw.Draw(img)
        for item in shot.overlays:
            overlay(draw, item, t - self.run_start[k], shot)
        return fit(img.convert("RGB"), self.ctx.width, self.ctx.height)

    def draw_toy(self, img, toy, t):
        name, x, y, flipped, bitten = toy
        if name not in self.art.toys:
            return
        pic, dx, dy = self.art.toys[name]
        if name == "bee" and int(t // 100) % 2 == 0:  # buzzing: wings flick up above her body
            wings = self.art.toys["bee_wings"][0]
            up = Image.new("RGBA", (pic.width, pic.height + 3))
            up.paste(wings, (0, 0), wings)
            up.paste(pic, (0, 3), pic)
            pic, dy = up, dy - 3
        if flipped:
            pic = pic.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if bitten:  # eaten from the end nearest her (the left, as she faces right)
            pic = pic.crop((min(bitten, pic.width - 1), 0, pic.width, pic.height))
            dx += bitten
        layer = Image.new("RGBA", img.size)
        layer.paste(pic, (int(round(x)) + dx, int(round(y)) + dy), pic)
        img.alpha_composite(layer)


def mode_style(mode: str) -> type:
    doc = (MODES[mode].__doc__ or "").strip()
    return type(mode.title().replace("_", ""), (_Scene,), {"MODE": (mode,), "__doc__": doc or f"The pet {mode.replace('_', ' ')}."})


DAYTIME = ["idle", "eating", "washing", "playing", "scratching", "napping", "waking", "zoomies",
           "asking", "begging", "grumpy", "name_tag", "toy", "bee"]


class Life(_Scene):
    """Her day: the ticked modes one after another, in an order that changes each day."""
    defaults = {"modes": DAYTIME}
    choices = {"modes": list(MODES)}
    basic = ("modes",)

    def modes(self):
        picked = [m for m in self.settings["modes"] if m in MODES] or ["idle"]
        day = self.ctx.now().date().isoformat()
        return sorted(picked, key=lambda m: hashlib.sha256(f"{day}{m}".encode()).digest())


class Wander(Style):
    """Overlay: the cat walks across whatever is playing, right to left or left to right."""
    fps = 20
    defaults = {"direction": "both", "floor": 0}
    choices = {"direction": ["both", "right", "left"]}

    def setup(self):
        self.art = None if missing(self.ctx.pet.kind) else \
            load(self.ctx.pet.kind, self.ctx.pet.coat, wearing(self.ctx.pet, self.ctx.now().date()))

    def render(self, now):
        w, h = self.ctx.width, self.ctx.height
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        if self.art is None:
            return img
        path = w + 32                                   # off one edge to off the other
        seconds = path / STRIDE["walk_right"] / 10      # walk cycle: about 10 px a second
        lap = int(now.timestamp() // seconds)
        rightwards = {"right": True, "left": False}.get(self.settings["direction"], lap % 2 == 0)
        tag = "walk_right" if rightwards else "walk_left"
        anim = self.art.animations[tag]
        progress = (now.timestamp() % seconds) / seconds
        x = -16 + progress * path if rightwards else w + 16 - progress * path
        cycle = sum(f.ms for f in anim)
        t, i = (now.timestamp() * 1000) % cycle, 0
        while t >= anim[i].ms:
            t -= anim[i].ms
            i += 1
        frame = anim[i]
        feet = h - self.settings["floor"]  # paws on the bottom row (art: lowest pixel = feet - 1)
        img.paste(frame.image, (int(x) + frame.dx, feet + frame.dy), frame.image)
        return img
