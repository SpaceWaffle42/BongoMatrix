"""Pet styles: each mode is a style, Life strings modes together, Wander is an overlay."""
from __future__ import annotations

import bisect
import math
import hashlib

from PIL import Image, ImageDraw

from bongo.module import Style

from .art import H, W, load, missing, no_art
from bongo.pets import wearing

from . import places, roam
from .scenes import BANNER_SLIDE_MS, HOUSE, MODES, STRIDE, TAG_RISE_MS, record_loop

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


def overlay(draw, item, t, shot, life=float("inf"), view=(0, 0, W, H)):
    """Speech bubbles, Zs, banners and the name tag, drawn over the scene ``t`` ms after the item
    appeared; ``life``: how long it stays, so each comes and goes gently (no Z is ever left mid-air).
    ``view`` is the part of the room on screen (x, y, width, height): they are kept inside it."""
    kind, cx, cy = item[0], int(shot.cat[0]), int(shot.cat[1])
    vx, vy, vw, vh = view
    if kind == "zzz":
        for k in range(2):
            p = ((t / 120) + k * 12) % 24
            began = t - p * 120                       # when this Z set off from her head
            if p < 20 and began >= 0 and began + 20 * 120 <= life:
                # from her head: the sleeping poses face left, head about 10 px left of her feet point
                y = cy - 14 - int(p) // 2
                if y > vy:                            # a Z that reaches the top of the screen has floated away
                    text(draw, max(vx + 1, cx - 12 - int(p) // 4), y, "Z", item[1])  # never off the left edge
    elif kind == "bubble" and (t // 600) % 2 == 0:
        x, y = min(cx + 6, vx + vw - 5), max(cy - 22, vy + 2)
        draw.rectangle([x - 1, y - 1, x + 3, y + 5], fill=(255, 250, 240))
        draw.point((x, y + 6), fill=(255, 250, 240))
        text(draw, x, y, item[1], (200, 60, 40))
    elif kind == "banner":
        width = len(item[1]) * 4
        top = vy - 7 + banner_rows(t, life)           # slides down in, and back up out
        draw.rectangle([vx, top, vx + vw - 1, top + 6], fill=(26, 19, 0))
        moved = max(0.0, t - BANNER_SLIDE_MS)
        x = vx + vw - int(moved / 1000 * 12)
        if x > vx - width:
            text(draw, x, top + 1, item[1], (255, 181, 71))
    elif kind == "tag":
        name = item[1]
        rise = max(0, min(4, int(t / (TAG_RISE_MS / 4)), int((life - t) / (TAG_RISE_MS / 4))))
        if rise == 0:
            return
        tw = len(name) * 4 - 1
        x = max(vx + 1, min(vx + vw - tw - 2, cx - tw // 2))
        y = max(vy, cy - 27 + (4 - rise))
        draw.rectangle([x - 1, y, x + tw, y + 6], fill=(26, 19, 0))
        text(draw, x, y + 1, name, (255, 227, 179))
    elif kind == "dust" and shot.moving and int(t / 40) % 2 == 0:
        bx = cx - 11 * shot.moving
        draw.point((bx, cy - 1), fill=(240, 220, 190))
        draw.point((bx - 2 * shot.moving, cy - 3), fill=(240, 220, 190))
    elif kind == "grumble" and (t // 330) % 2 == 0:
        for k in range(5):
            draw.point((cx - 2 + k, max(vy, cy - 24) + (k + int(t / 110)) % 3), fill=(90, 90, 110))


def lifetimes(timeline, starts, total, loop=True):
    """For each shot, its overlays with when each appeared and when it goes (ms on the loop's clock).
    An overlay that runs on over the loop's end carries on from the start, so it never restarts."""
    n = len(timeline)
    lives = [[] for _ in range(n)]
    for item in {o for shot, _ in timeline for o in shot.overlays}:
        runs, k = [], 0
        while k < n:
            if item in timeline[k][0].overlays:
                j = k
                while j < n and item in timeline[j][0].overlays:
                    j += 1
                runs.append([k, j, starts[k], starts[j] if j < n else total])
                k = j
            else:
                k += 1
        if loop and len(runs) > 1 and runs[0][0] == 0 and runs[-1][1] == n:   # across the wrap: one run
            first, last = runs[0], runs[-1]
            first[2], last[3] = last[2] - total, total + first[3]
            last_end = last[3]
            first[3] = last_end - total
        for k0, k1, began, ended in runs:
            for k in range(k0, k1):
                lives[k].append((item, began, ended))
    return lives


def window(width: int, height: int, feet=None, tall: int = 18, above: int = 1, room=(W, H)) -> tuple[int, int]:
    """The house and the small places are drawn at 64x32 (``room``: a bigger place's size). A smaller
    safe area shows the part of the room the pet is in: where that part starts. ``feet`` is the middle
    of her feet, ``tall`` her height in this pose and ``above`` the rows to keep clear over her head
    (more while a banner is up). Sideways she is in the middle; upright she stands a row above the
    bottom edge (in a room taller than the house: a third of the way up, so the floor in front of her
    shows too), or, when there isn't the height for that, her head comes first. It stops at the walls.
    Without her: the room's middle, floor kept."""
    rw, rh = room
    if feet is None:
        x, y = (rw - width) // 2, rh - height
    else:
        x = round(feet[0] - width / 2)
        y = round(feet[1]) - height + 1 if tall + above + 1 <= height else round(feet[1]) - tall - above
        if rh > H:   # five rows of floor under her; room over her head even up on her hind legs (no hop)
            y = min(y + max(5, height // 6), round(feet[1]) - tall - above)
    return min(max(0, x), max(0, rw - width)), min(max(0, y), max(0, rh - height))


PLAY = {"playing", "toy", "ball", "mouse"}   # scenes and free-roaming actions where she plays with her ball or toy mouse


def lead(shot, width: int, room_width: int = W) -> float:
    """In a bigger room: how far the view looks ahead of her, towards her ball or toy mouse on the
    floor, so what she is playing with is on screen too. It grows as the thing goes further from her,
    up to a limit, and never shrinks back on its own, so the view doesn't drift towards it and back.
    It fades for things off her floor or leaving the room, so nothing coming or going makes it jump."""
    far, out = width / 8, 0.0                       # no further: when play stops, the view slides back that far
    lit = 1.0
    if shot.fade and "night" in (shot.fade[0], shot.room):   # her ball fades with the light
        lit = 1 - shot.fade[1] if shot.room == "night" else shot.fade[1]
    for thing, k in ((shot.ball, lit), (shot.toy and shot.toy[0] == "mouse" and shot.toy[1:3], 1.0)):
        if not thing:
            continue
        x, y = thing[0], thing[1]
        k *= max(0.0, 1 - abs(y - shot.cat[1]) / 16)                         # only what is on the floor beside her
        k *= max(0.0, min(1.0, (x + 8) / 16, (room_width + 8 - x) / 16))     # and not what is leaving the room
        d = x - shot.cat[0]
        out += max(-far, min(far, d / 2)) * k        # half way to it, up to a limit: it only grows as it goes
    return max(-far, min(far, out))


def fit(scene: Image.Image, width: int, height: int, feet=None, tall: int = 18, above: int = 1) -> Image.Image:
    """The part of the room that ``window`` picks."""
    if (width, height) == scene.size:
        return scene
    x, y = window(width, height, feet, tall, above, scene.size)
    return scene.crop((x, y, x + width, y + height))


def banner_rows(t, life):
    """How many of the banner's seven rows are on screen ``t`` ms into its ``life``."""
    return round(7 * max(0.0, min(1.0, t / BANNER_SLIDE_MS, (life - t) / BANNER_SLIDE_MS)))


NIGHT_ZZZ = (120, 140, 210)   # her Zs in the dark (scenes.py lights_out and waking use the same)


class _Scene(Style):
    fps = 20
    MODE: tuple[str, ...] = ()
    defaults = {"place": "house"}                    # where she lives: the house, or one of places.PLACES
    choices = {"place": places.CHOICES}
    basic = ("place",)

    def setup(self):
        self.art = None
        if missing(self.ctx.pet.kind):                  # a public build before a pack is imported
            return
        self.art = load(self.ctx.pet.kind, self.ctx.pet.coat, wearing(self.ctx.pet, self.ctx.now().date()))
        self.place = places.get(self.settings["place"])
        self.stage = getattr(self.place, "stage", HOUSE)   # a bigger room has its own size, bed, bowl and floor
        day, empty, night = places.rooms(self.settings["place"]) if self.place else (self.art.room, self.art.empty, self.art.night)
        self.rooms = {"day": day, "empty": empty, "night": night}
        self.timeline = record_loop(self.art, self.modes(), self.ctx.pet.name, self.stage)   # seamless: scenes link up
        self.starts, t = [], 0.0
        for shot, _ in self.timeline:
            self.starts.append(t)
            t += shot.ms
        self.total = t
        self.lives = lifetimes(self.timeline, self.starts, self.total)

    def modes(self):
        return list(self.MODE)

    def room(self, which, clock):
        """The room ("day", "empty", "night") at this moment, with the place's floor things moving."""
        img = self.rooms[which]
        if self.place:
            img = img.copy()
            self.place.back(img, clock, which)
        return img

    def render(self, now):
        if self.art is None:
            return no_art(self.ctx.width, self.ctx.height, self.ctx.fonts)
        clock = now.timestamp() * 1000          # the place's moving things run on this, never restarting
        t, k = self.locate(now, clock)
        shot = self.timeline[k][0]
        art = self.art
        img = self.room(shot.room, clock).convert("RGBA")
        if shot.fade:   # the room changing: lights going out or up, the bowl filling or emptying
            img = Image.blend(self.room(shot.fade[0], clock).convert("RGBA"), img, shot.fade[1])
        big = self.stage is not HOUSE
        empty = img.copy() if big else None   # the room without her: what's in front of her is drawn back from it
        if shot.ball:
            ball = art.ball
            if shot.fade and "night" in (shot.fade[0], shot.room):   # dims with the light
                lit = 1 - shot.fade[1] if shot.room == "night" else shot.fade[1]
                ball = ball.copy()
                ball.putalpha(ball.getchannel("A").point(lambda a: int(a * lit)))
            img.alpha_composite(ball, (int(shot.ball[0]) - 4, int(shot.ball[1]) - 4))
        # a bee flies in front of her; a toy on the floor is behind her, except beside a sitting cat,
        # where it would hide under her tail
        flying = shot.toy and (shot.toy[0] == "bee" or shot.tag in ("meow_sit", "wash_sit"))
        if shot.toy and not flying:
            self.draw_toy(img, shot.toy, t)
        if big:   # her ball and toy go behind what stands in front of them, as she does
            for thing in (shot.ball, None if flying or not shot.toy else shot.toy[1:3]):
                if thing:
                    self.place.over(img, empty, (thing[0], thing[1] - 3))
        frame = art.animations[shot.tag][shot.index]
        x, y = int(round(shot.cat[0])) + frame.dx, int(round(shot.cat[1])) + frame.dy
        layer = Image.new("RGBA", img.size)
        layer.paste(frame.image, (x, y), frame.image)
        img.alpha_composite(layer)
        if big:
            self.place.over(img, empty, shot.cat, shot.perch)
        if flying:
            self.draw_toy(img, shot.toy, t)
        if self.place:   # what moves over her: snow, fireflies, leaves (crossing over as the lights change)
            img = img.convert("RGB")
            if shot.fade and "night" in (shot.fade[0], shot.room):
                dark = shot.fade[1] if shot.room == "night" else 1 - shot.fade[1]
                lit, unlit = img.copy(), img.copy()
                self.place.front(lit, clock, False)
                self.place.front(unlit, clock, True)
                img = Image.blend(lit, unlit, dark)
            else:
                self.place.front(img, clock, shot.room == "night")
        draw = ImageDraw.Draw(img)
        w, h = min(self.ctx.width, img.width), min(self.ctx.height, img.height)
        feet = self.focus(t, shot, w) if big else shot.cat   # what the view follows
        # a banner pushes her down as it slides in, so her face stays under it
        above = 1 + max((banner_rows(t - began, ended - began) for item, began, ended in self.lives[k] if item[0] == "banner"), default=0)
        if any(item[0] == "zzz" for item, _, _ in self.lives[k]):
            above = max(above, 4)                     # room for a Z to rise
        tall = max(frame.image.height, -frame.dy)   # her top row in this frame, counted up from her feet
        view = (*window(w, h, feet, tall, above, img.size), w, h)   # the part of the room on screen: she is in it
        for item, began, ended in self.lives[k]:
            if item[0] == "zzz" and self.place:   # Zs that show up on a pale floor, changing with the light
                dark = 1.0 if shot.room == "night" else 0.0
                if shot.fade and "night" in (shot.fade[0], shot.room):
                    dark = shot.fade[1] if shot.room == "night" else 1 - shot.fade[1]
                item = ("zzz", tuple(round(a + (b - a) * dark) for a, b in zip(self.place.zzz, NIGHT_ZZZ)))
            overlay(draw, item, t - began, shot, ended - began, view)
        return fit(img.convert("RGB"), self.ctx.width, self.ctx.height, feet, tall, above)

    GLIDE = 600   # ms either side of now that the view's glide takes in
    SETTLE = 200  # ms: a pause in her steps longer than this and the view starts to ease in by itself

    def segments(self, t, R):
        """The shots around ``t`` (the loop's own clock) as (start, end, shot, what), nearest first both
        ways: back to t - R, then on to t + R. Past either end of a loop it wraps round; past
        either end of free roaming's few actions, the end shot carries on."""
        n, loop = len(self.starts), getattr(self, "roamer", None) is None
        k = max(0, bisect.bisect_right(self.starts, t) - 1)
        end = lambda i: self.starts[i + 1] if i + 1 < n else (self.total if loop else self.starts[i] + self.timeline[i][0].ms)   # noqa: E731
        i, shift = k, 0.0
        while True:                                     # back
            a, b = self.starts[i] + shift, end(i) + shift
            if not loop and i == 0:
                a = -math.inf
            yield a, b, *self.timeline[i]
            if a <= t - R:
                break
            i -= 1
            if i < 0:
                i, shift = n - 1, shift - self.total
        i, shift = k + 1, 0.0
        while True:                                     # on
            if i >= n:
                if not loop:
                    break
                i, shift = 0, shift + self.total
            a, b = self.starts[i] + shift, end(i) + shift
            if a >= t + R:
                break
            if not loop and i == n - 1:
                b = math.inf
            yield a, b, *self.timeline[i]
            i += 1

    def stepped(self, t, here):
        """When her feet last took a step walking or running (to ``here``, where they are at ``t``),
        looking back 2 x SETTLE; None if they haven't, or if they got here some other way (a jump)."""
        walk = False                                    # her feet got here walking or running
        for a, b, seg, _ in self.segments(t, 2 * self.SETTLE):
            if a > t:
                break
            there = (round(seg.cat[0]), round(seg.cat[1]))
            if there != here:
                return b if walk and t - b < 2 * self.SETTLE else None
            walk = walk or seg.tag.startswith("walk")
            if a <= t - 2 * self.SETTLE:
                break
        return None

    def mean(self, t, R, width=0):
        """Where she is, averaged over R ms either side of ``t``, the nearest moments counting most.
        Worked out exactly from her shots, so it changes smoothly in time (sampling it would beat
        against her steps and wobble). With ``width``: plus how far the view leans towards her ball
        or toy mouse while she plays with it, averaged the same way."""
        def upto(u):                                    # how much of the weight lies before t + u
            if u <= -R:
                return 0.0
            if u >= R:
                return 1.0
            return (u + R) ** 2 / (2 * R * R) if u <= 0 else 1 - (R - u) ** 2 / (2 * R * R)

        x = y = ahead = 0.0
        for a, b, seg, what in self.segments(t, R):
            w = upto(b - t) - upto(a - t)
            if w <= 0:
                continue
            x += w * seg.cat[0]
            y += w * seg.cat[1]
            if width and what in PLAY:
                ahead += w * lead(seg, width, self.stage.w)
        return x + ahead, y

    def focus(self, t, shot, width):
        """What the view follows in a bigger room: she glides into it rather than whipping about when
        she runs or jumps (it sets off as she gathers herself), and while she plays it leans towards
        her ball or toy mouse. While she walks, the view takes its place only as her feet move and
        holds still between her steps, so the room goes by on the same frames as she does. (Gliding
        a pixel every frame while she steps two every other frame makes her shuffle to and fro on
        screen.) Running, the same, however long her strides. When she stops, the view lets go over
        SETTLE rather than snapping to where the glide has got to. Jumps glide every frame.
        Never far from her: she stays on screen."""
        cx, cy = round(shot.cat[0]), round(shot.cat[1])
        at, last = t, self.stepped(t, (cx, cy))
        if last is not None:                            # walking: held since her last step, and when she
            gap = t - last                              # stops, let go gently: catching up at twice the pace
            at = last if gap < self.SETTLE else last + 2 * (gap - self.SETTLE)
        x, y = self.mean(min(at, t), self.GLIDE, width)
        reach = round(width / 2) - 12                   # her middle stays this far in from the sides
        return max(cx - reach, min(cx + reach, round(x))), max(cy - 8, min(cy + 4, round(y)))

    def locate(self, now, clock):
        """Where the loop is at this moment: (ms on the loop's clock, which shot)."""
        t = clock % self.total
        return t, bisect.bisect_right(self.starts, t) - 1

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
    defaults = {**_Scene.defaults, "modes": DAYTIME, "free_roaming": True}   # (in the places that allow it: rooms.py)
    choices = {**_Scene.choices, "modes": list(MODES)}
    basic = ("place", "modes")

    def setup(self):
        super().setup()
        self.roamer, self.window = None, None
        if self.art is not None and self.settings["free_roaming"] and getattr(self.place, "roams", False):
            self.roamer = roam.roamer(self.art, self.settings["place"], self.place, self.ctx.pet, self.ctx.now())
            self.began = {}

    def locate(self, now, clock):
        """Free-roaming: there is no loop. Her last few actions are the timeline, on the real clock."""
        if self.roamer is None:
            return super().locate(now, clock)
        now = roam.like(now, self.roamer.clock)
        if self.roamer.recent and now < self.roamer.recent[0][0]:      # the clock went back: she starts afresh
            self.roamer = roam.roamer(self.art, self.settings["place"], self.place, self.ctx.pet, now)
        around = self.roamer.around(now)
        if around is not self.window:
            self.window = around
            self.timeline, self.starts = [], []
            for began, shots, what in around:
                at = roam.like(began, now).timestamp() * 1000
                for shot in shots:
                    self.timeline.append((shot, what))
                    self.starts.append(at)
                    at += shot.ms
            self.total = at
            self.lives = lifetimes(self.timeline, self.starts, self.total, loop=False)
            for k, items in enumerate(self.lives):     # what was already up when this window began keeps its age
                for j, (item, start, end) in enumerate(items):
                    if start == self.starts[0] and self.began.get(item, start) < start:
                        items[j] = (item, self.began[item], end)
            self.began = {item: start for items in self.lives for item, start, _ in items}
        return clock, max(0, min(len(self.starts) - 1, bisect.bisect_right(self.starts, clock) - 1))


    def modes(self):
        picked = [m for m in self.settings["modes"] if m in MODES] or ["idle"]
        day = self.ctx.now().date().isoformat()
        return sorted(picked, key=lambda m: hashlib.sha256(f"{day}{m}".encode()).digest())


class Wander(Style):
    """Overlay: the cat walks across whatever is playing, right to left or left to right."""
    fps = 20
    defaults = {"direction": "both", "floor": 0}
    limits = {"floor": (0, 64)}
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
        gait = self.art.tuning.get("gait", {}).get("walk_right")
        if gait:   # pose and position from one frame count: she only moves when her legs do (no glide at 20 fps)
            anim = self.art.animations["walk_right"]
            per_cycle, cycle = sum(gait), sum(f.ms for f in anim)
            seconds = path / per_cycle * cycle / 1000
        else:
            seconds = path / STRIDE["walk_right"] / 10      # walk cycle: about 10 px a second
        lap = int(now.timestamp() // seconds)
        rightwards = {"right": True, "left": False}.get(self.settings["direction"], lap % 2 == 0)
        anim = self.art.animations["walk_right" if rightwards else "walk_left"]
        if gait:
            n = int((now.timestamp() % seconds) * 1000 // (cycle / len(anim)))
            i = n % len(anim)
            step = (n // len(anim)) * per_cycle + sum(gait[1:i + 1])
            x = -16 + step if rightwards else w + 16 - step
        else:      # no gait for this pet (the bunny): the walk glides as it always has
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
