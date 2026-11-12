"""Pet: the time, big and centred, and now and then the Matrix's pet (bongo/pets.py) turns up and
plays a scene around it.

A cat walks by, walks behind the digits, chases a ball, jumps on the time, paws at a digit,
naps, or knocks a colon dot off. A bunny is too tall to fit under the time, so she has her own
set: hops by behind the digits, peeks out from behind them, noses a ball along, naps.

When she turns up: every five minutes there's a roll. The first chance is 1 in 4; after a visit
the next chance halves (1 in 8, 1 in 16 ...), so visits don't come back to back; a roll without a
visit puts it back to 1 in 4. The rolls are worked out from the date and the time, so a restart or
another Matrix showing the same pet shows the same visits.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
from functools import lru_cache

import numpy as np
from PIL import Image, ImageChops, ImageFilter

from bongo.module import Style
from bongo.modules.pet.art import load, missing
from bongo.pets import wearing

from .common import HOURS, hm, scaled

W, H, FLOOR = 64, 32, 32
BASE = 19                       # baseline of the time: digits on rows 9-18
INK = (255, 236, 200)
EDGE = (8, 6, 14)               # the dark edge round the digits
STEP = 100                      # ms a scene frame
ROLL = 300                      # seconds between rolls
SCENES = ("walk_by", "behind", "ball", "jump_on", "scratch", "nap", "colon")
TALL_SCENES = ("walk_by", "peek", "ball", "nap")   # for a pet whose ears would reach the digits


def _unit(*parts) -> float:
    """A repeatable number in [0, 1) from ``parts``."""
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big") / 2 ** 64


@lru_cache(maxsize=64)
def visits(day: dt.date, often: bool, scenes: tuple = SCENES) -> dict:
    """{roll index in the day: scene} for the rolls that bring a visit."""
    out, streak = {}, 0
    for k in range(24 * 3600 // ROLL):
        chance = 1.0 if often else 0.25 / 2 ** streak
        if _unit(day, k, "visit") < chance:
            out[k] = scenes[int(_unit(day, k, "scene") * len(scenes))]
            streak += 1
        else:
            streak = 0
    return out


def body_offset(art, tag) -> float:
    """Where an animation's body sits relative to its feet point (from its first frame), so poses
    line up with each other when she switches between them."""
    f0 = art.animations[tag][0]
    box = f0.image.getchannel("A").getbbox()
    return f0.dx + (box[0] + box[2]) / 2


def extent(art, tag, i):
    """(left, top, right, bottom) of frame ``i`` of ``tag``, relative to (body centre, feet)."""
    f = art.animations[tag][i % len(art.animations[tag])]
    box = f.image.getchannel("A").getbbox()
    off = f.dx - body_offset(art, tag)
    return off + box[0], f.dy + box[1], off + box[2], f.dy + box[3]


def is_tall(art) -> bool:
    """Would she cover most of the digits sitting on the floor? (A bunny's ears do.)"""
    return min(extent(art, "meow_sit", i)[1] for i in range(len(art.animations["meow_sit"]))) < -19


class PetClock(Style):
    """Pet: the time big and centred; now and then your pet turns up and plays a scene around it."""
    group = "Pet"
    fps = 10
    defaults = {"hours": "24", "visits": "sometimes"}
    choices = {"hours": HOURS, "visits": ["sometimes", "often"]}
    basic = ("hours", "visits")

    def setup(self):
        self.font = self.ctx.fonts("9x18B")
        self.small = self.ctx.fonts("tom-thumb")
        self.bg = Image.new("RGB", (W, H))
        for y in range(H):
            k = y / (H - 1)
            self.bg.paste(tuple(int(a + (b - a) * k) for a, b in zip((18, 14, 40), (40, 22, 30))), (0, y, W, y + 1))
        self.scenes = {}
        self.art_day = None

    def art(self, now):
        if self.art_day != now.date():  # her outfit can change at midnight
            pet = self.ctx.pet
            self._art = load(pet.kind, pet.coat, wearing(pet, now.date()))
            self.art_day = now.date()
        return self._art

    def scene_list(self, now):
        return TALL_SCENES if is_tall(self.art(now)) else SCENES

    def render(self, now):
        text = hm(now, self.settings["hours"])
        if missing(self.ctx.pet.kind):                       # no pet art yet: just the time
            return scaled(self.draw({}, text, now), self.ctx.width, self.ctx.height)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        k = int((now - midnight).total_seconds() // ROLL)
        scene = visits(now.date(), self.settings["visits"] == "often", self.scene_list(now)).get(k)
        frame = {}
        if scene:
            start = midnight + dt.timedelta(seconds=k * ROLL)
            frames = self.scene(scene, hm(start, self.settings["hours"]), now)
            i = int((now - start).total_seconds() * 1000 // STEP)
            if i < len(frames):
                frame = frames[i]
        return scaled(self.draw(frame, text, now), self.ctx.width, self.ctx.height)

    # -- drawing --------------------------------------------------------------------------------

    def layout(self, text):
        x0 = (W - self.font.width(text)) // 2
        xs, x = [], x0
        for ch in text:
            xs.append(x)
            x += self.font.width(ch)
        return xs

    def draw(self, fr, text, now):
        """A frame dict: cat=(tag, i, cx, feet), behind (she's behind the digits), over (digits
        drawn over her, without their edge), sink, offs {digit: (dx, dy)}, hide (digits left out),
        dots (loose colon dots, dot_size), ball=(x, y, quarter turns), z=[(x, y, brightness)]."""
        img = self.bg.copy()
        xs = self.layout(text)
        sink, offs, hide, over = fr.get("sink", 0), fr.get("offs", {}), fr.get("hide", ()), fr.get("over", ())
        chars = {i: (xs[i] + offs.get(i, (0, 0))[0], BASE + sink + offs.get(i, (0, 0))[1], ch)
                 for i, ch in enumerate(text) if i not in hide}
        art = self.art(now) if fr else None
        cat, behind = fr.get("cat"), fr.get("behind")
        clip = fr.get("clip")
        if cat and behind:
            self.put(img, art, *cat, clip=clip)
        self.ink(img, [c for i, c in chars.items() if i not in over])
        if cat and not behind:
            self.put(img, art, *cat, clip=clip)
        if fr.get("edged") and over:                         # drawn over her with their dark edge, so they read on fur
            self.ink(img, [chars[i] for i in over if i in chars])
        else:
            for i in over:
                if i in chars:
                    x, y, ch = chars[i]
                    self.font.draw(img, x, y, ch, INK)
        dot = fr.get("dot_size", (3, 2))
        if fr.get("dots"):
            ink = Image.new("L", (W, H))                     # where the digits are: a dot's edge never cuts into them
            for x, y, ch in chars.values():
                self.font.draw(ink, x, y, ch, 255)
            for x, y in fr["dots"]:                          # colon dots drawn on their own, with their edge
                x, y = round(x), round(y)
                edge = Image.new("L", (W, H))
                edge.paste(255, (x - 1, y - 1, x + dot[0] + 1, y + dot[1] + 1))
                img.paste(EDGE, (0, 0), ImageChops.subtract(edge, ink))
                img.paste(INK, (x, y, x + dot[0], y + dot[1]))
        if fr.get("ball"):
            bx, by, turns = fr["ball"]
            ball = art.ball.rotate(90 * (turns % 4)) if turns % 4 else art.ball   # it rolls
            box, still = ball.getchannel("A").getbbox(), art.ball.getchannel("A").getbbox()
            left = round(bx) - (still[2] - still[0]) // 2    # its left edge moves with it, whichever way up it is
            img.paste(ball, (left - box[0], round(by) - box[3]), ball)
        for x, y, b in fr.get("z", ()):                      # Zs rising from her head
            self.small.draw(img, round(x), round(y), "z", tuple(int(c * b) for c in (150, 180, 255)))
        return img

    def ink(self, img, chars):
        mask = Image.new("L", (W, H))
        for x, y, ch in chars:
            self.font.draw(mask, x, y, ch, 255)
        img.paste(EDGE, (0, 0), mask.filter(ImageFilter.MaxFilter(3)))
        for x, y, ch in chars:
            self.font.draw(img, x, y, ch, INK)

    def put(self, img, art, tag, i, cx, feet, clip=None):
        """``cx`` is the middle of her body, ``feet`` the row under her paws; ``clip`` (lo, hi) keeps
        only the columns lo <= x < hi (so a leg swinging back never pops in at the panel's edge)."""
        f = art.animations[tag][i % len(art.animations[tag])]
        layer = Image.new("RGBA", (W, H))
        layer.paste(f.image, (round(cx - body_offset(art, tag)) + f.dx, round(feet) + f.dy), f.image)
        if clip is not None:
            lo, hi = max(0, round(clip[0])), min(W, round(clip[1]))
            keep = Image.new("L", (W, H))
            if hi > lo:
                keep.paste(255, (lo, 0, hi, H))
            layer.putalpha(ImageChops.multiply(layer.getchannel("A"), keep))
        img.paste(Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB"))

    # -- the scenes -----------------------------------------------------------------------------

    def scene(self, name, text, now):
        key = (name, text)
        if key not in self.scenes:
            if len(self.scenes) > 8:
                self.scenes.clear()
            self.scenes[key] = Script(self.art(now), self.layout(text), text, self.font).run(name)
        return self.scenes[key]


class Script:
    """Builds one scene as a list of frames (see PetClock.draw), one every STEP ms. She always
    enters and leaves fully off the panel, and stops and starts walking with her feet down."""

    def __init__(self, art, xs, text, font):
        self.art, self.xs, self.text, self.font = art, xs, text, font
        self.frames = []
        self.last_step = 0
        self.rolling = None                                     # [x, speed, quarter turns] while a ball is out
        self.kick = None                                        # (speed, frames to wait) once she's struck it
        mask = Image.new("L", (W, H))
        font.draw(mask, xs[0], BASE, text, 255)
        self.top = mask.getbbox()[1] if mask.getbbox() else 9    # the first row of the digits
        self.tall = is_tall(art)
        walks = ("walk_right", "walk_left")
        boxes = [extent(art, t, i) for t in walks for i in range(len(art.animations[t]))]
        self.left_off = -math.ceil(max(b[2] for b in boxes)) - 1     # body centre with her just off the left
        self.right_off = W - math.floor(min(b[0] for b in boxes)) + 1    # ... and just off the right
        self.ground = {t: [i for i in range(len(art.animations[t])) if extent(art, t, i)[3] >= 0] or [0]
                       for t in walks}

    def run(self, name):
        getattr(self, name)()
        if self.frames and self.frames[-1].get("cat"):
            self.frames.append({})                           # the last pixel of her has gone
        return self.frames

    def add(self, **f):
        if self.tall and f.get("cat") and "behind" not in f and f["cat"][3] >= FLOOR:
            f["behind"] = True                               # her ears would cover the time
        if self.rolling is not None:                            # a loose ball keeps rolling
            b = self.rolling
            if self.kick is not None:                        # struck: it stays put while touching, then eases off
                speed, wait = self.kick
                if wait:
                    self.kick = (speed, wait - 1)
                else:                                        # one pixel first, so it leaves her paw, then rolls
                    b[0] += 1
                    b[1] = speed
                    self.kick = None
            elif b[1] > 0.05:
                b[0] += b[1]
                b[1] = b[1] * 0.95 if b[0] > W else max(0.0, b[1] - 0.04)
            if b[0] < W + 8:
                f["ball"] = (b[0], FLOOR, int(b[0] // 3))
        self.frames.append(f)

    def ms(self, tag, i):
        return max(1, round(self.art.animations[tag][i].ms / STEP))

    def walk(self, cx, to, feet=FLOOR, speed=1, **extra):
        """Whole pixels, one walk frame per ``speed`` px. A walk that comes on from off the panel
        starts a little further out, so she stops on a frame with her feet down."""
        step = 1 if to > cx else -1
        tag = "walk_right" if step > 0 else "walk_left"
        n, ground = len(self.art.animations[tag]), self.ground[tag]
        k = ground[0]
        if cx in (self.left_off, self.right_off):
            frames = math.ceil(abs(to - cx) / speed)
            while (k + frames - 1) % n not in ground:
                frames += 1
            cx = to - step * frames * speed
        back = front = None                                   # running edges while she's half off the panel
        while cx != to:
            cx += step * min(speed, abs(to - cx))
            i = k % n
            left, _, right, _ = extent(self.art, tag, i)
            lead, trail = (cx + right, cx + left) if step > 0 else (cx + left, cx + right)
            clip = None
            if (lead >= W) if step > 0 else (lead <= 0):      # going off: her back edge only ever moves on
                back = trail if back is None else (max(back, trail) if step > 0 else min(back, trail))
                if (back >= W) if step > 0 else (back <= 0):
                    cx = to                                   # she's gone
                    break
                clip = (back, W) if step > 0 else (0, back)
            elif (trail < 0) if step > 0 else (trail > W):    # coming on: her front edge only ever moves on
                front = lead if front is None else (max(front, lead) if step > 0 else min(front, lead))
                clip = (0, front) if step > 0 else (front, W)
            self.add(cat=(tag, i, cx, feet), **extra, **({"clip": clip} if clip else {}))
            self.last_step = i
            k += 1
        return cx

    def anim(self, tag, cx, feet=FLOOR, loops=1, slow=1, **extra):
        for _ in range(loops):
            for i in range(len(self.art.animations[tag])):
                for _ in range(self.ms(tag, i) * slow):
                    self.add(cat=(tag, i, cx, feet), **extra)

    def hold(self, tag, i, cx, n, feet=FLOOR, **extra):
        for _ in range(n):
            self.add(cat=(tag, i, cx, feet), **extra)

    def reach(self, tag, side):
        """The frame of ``tag`` that reaches furthest to ``side`` (+1 right, -1 left), and how far."""
        boxes = [extent(self.art, tag, i) for i in range(len(self.art.animations[tag]))]
        i = max(range(len(boxes)), key=lambda k: boxes[k][2] if side > 0 else -boxes[k][0])
        return i, boxes[i][2] if side > 0 else boxes[i][0]

    # -- the scenes -----------------------------------------------------------------------------

    def walk_by(self):
        self.walk(self.left_off, self.right_off)

    def behind(self):
        self.walk(self.right_off, self.left_off, feet=BASE, behind=True)

    def peek(self):
        """(a tall pet) She hops in behind the time and sits there with her ears up over it."""
        right = self.xs[-1] + self.font.width(self.text[-1])
        cx = self.walk(self.right_off, min(W - 7, right + 4))   # just past the last digit, ears up beside it
        self.hold("walk_left", self.last_step, cx, 2)
        self.hold("meow_sit", 0, cx, 8)
        self.anim("wash_sit", cx, slow=2)
        self.anim("meow_sit", cx, slow=2)
        self.hold("meow_sit", 0, cx, 6)
        self.walk(cx, self.left_off)

    def ball(self):
        """A ball rolls in and stops; she comes up and taps it, then sends it off with a second
        swipe (a bunny noses it) and trots after it."""
        bat = "eat_right" if self.tall else "paw_right"
        box = self.art.ball.getchannel("A").getbbox()
        half = (box[2] - box[0]) / 2
        hit, reach = self.low_reach(bat, box[3] - box[1])   # the strike frame, and how close she stands for it
        rest, start, n = 24.0, -6.0, 26
        for k in range(1, n + 1):                            # rolls in, slowing to a stop
            x = start + (rest - start) * (1 - (1 - k / n) ** 3)
            self.add(ball=(x, FLOOR, 0))
        self.rolling = [rest, 0.0, 0]
        cx = self.walk(self.left_off, self.stand(bat, hit, rest))
        if self.tall:
            self.hold(bat, 0, cx, 4)                         # side-on, looking at it
        else:
            self.hold("walk_right", self.last_step, cx, 4)
        self.strike(bat, hit, cx, 0.6, extra=2)              # a tap: it rolls on a little
        self.hold(bat, 0, cx, 4)
        rest, speed = self.rolling[0], self.rolling[1]       # where the tap leaves it, once it stops
        if self.kick is not None:
            rest, speed = rest + 1, self.kick[0]
        while speed > 0.05:
            rest += speed
            speed = max(0.0, speed - 0.04)
        cx = self.walk(cx, self.stand(bat, hit, rest))       # steps up to it again, where it'll lie
        self.strike(bat, hit, cx, 2.2 if self.tall else 3.4)  # the swipe (a firmer nudge) that sends it off
        self.walk(cx, self.right_off, speed=1 if self.tall else 2)   # after it (she lets it lead)

    def strike(self, bat, hit, cx, speed, extra=0):
        """Play ``bat``; on the strike frame she lunges a pixel onto the ball, which sits against
        her paw for that frame and then moves off (a pixel first, then at ``speed``)."""
        for i in range(len(self.art.animations[bat])):
            for t in range(self.ms(bat, i) + (extra if i == hit else 0)):
                if i == hit and t == 0:
                    self.kick = (speed, 1)
                self.add(cat=(bat, i, cx + 1 if i == hit else cx, FLOOR))

    def low_reach(self, tag, height):
        """The frame of ``tag`` whose paw (or nose) reaches furthest right within ``height`` rows of
        the floor, and how far right of her body centre that pixel is."""
        best = (0, -99)
        for i, f in enumerate(self.art.animations[tag]):
            px = np.array(f.image.convert("RGBA"))
            a = (px[..., 3] > 0) & (px[..., :3].max(-1) > 48)   # her fur or nose, not the dark outline (it reads as a gap)
            rows = [r for r in range(a.shape[0]) if f.dy + r >= -height]   # only the rows the ball fills
            cols = np.flatnonzero(a[rows].any(0)) if rows else []
            if len(cols):
                x = cols.max() + f.dx - body_offset(self.art, tag)
                if x > best[1]:
                    best = (i, x)
        return best

    def stand(self, bat, hit, ball_x):
        """Where she stands so that her lunge (one pixel, on the strike frame ``hit``) puts a lit
        pixel of her paw (or nose) right against the ball, row by row, as the ball lies at ``ball_x``
        (whichever way up it is then); standing, a pixel separates them."""
        ball = self.art.ball.rotate(90 * (int(ball_x // 3) % 4))
        box, still = ball.getchannel("A").getbbox(), self.art.ball.getchannel("A").getbbox()
        bx = round(ball_x) - (still[2] - still[0]) // 2 - box[0]
        by = FLOOR - box[3]
        b = np.array(ball.getchannel("A")) > 0
        f = self.art.animations[bat][hit]
        px = np.array(f.image.convert("RGBA"))
        lit = (px[..., 3] > 0) & (px[..., :3].max(-1) > 48)
        gaps = []
        for r in range(b.shape[0]):
            if not b[r].any():
                continue
            y = by + r
            cr = y - FLOOR - f.dy                            # her sprite row on this panel row
            if 0 <= cr < lit.shape[0] and lit[cr].any():
                gaps.append(bx + np.flatnonzero(b[r]).min() - (lit[cr].nonzero()[0].max() + f.dx))
        if not gaps:
            return self.touching(ball_x, still, self.low_reach(bat, still[3] - still[1])[1])
        want = min(gaps) - 1                                 # round(cx + 1 - offset) must come to this
        off = body_offset(self.art, bat)
        return next(cx for cx in range(round(want + off) - 3, round(want + off) + 3) if round(cx + 1 - off) == want)

    def touching(self, ball_x, box, reach):
        """Where she stands: a pixel short of the ball, so only the strike (a lunge of one pixel)
        puts that paw (or nose) right against its left edge."""
        left = round(ball_x) - (box[2] - box[0]) // 2
        return round(left - reach) - 2

    def jump_on(self):
        """She creeps up behind the time, crouches, and leaps up onto it; it gives under her paws.
        She sits up there a while, then pushes off (it springs back) and drops down behind it."""
        c = self.text.index(":")
        land_x = round(self.xs[c - 1] + self.font.width(self.text[c - 1]) / 2)   # onto the digit before the colon
        land_x = max(land_x, round(self.xs[0]) + 13)          # (a lone first digit: all four paws on the time)
        crouch = "eat_right"                                 # side-on, head down, weight back: below the digits
        cx = self.walk(self.left_off, max(12, land_x - 14), behind=True)
        for i in (0, 1, 2, 2, 2, 2, 2, 2, 2, 2):             # sizes it up
            self.add(cat=(crouch, i, cx, FLOOR), behind=True)
        sink = 11
        land_feet = self.top + sink - extent(self.art, "meow_sit", 0)[3]   # paws on the sunk digits
        leap = ("walk_right", 2)                             # the stretched stride
        lift = extent(self.art, *leap)[3]                    # its paws sit this far above its feet row
        n = 9
        for k in range(1, n + 1):                            # an arc up and down onto the time
            p = k / n
            x = cx + (land_x - cx) * math.sin(p * math.pi / 2)   # most of the way across early: no skid as she lands
            feet = FLOOR + (land_feet - FLOOR) * p - 7 * math.sin(math.pi * p)
            paws = feet + lift                               # the row under her paws as drawn
            duck = round(sink * min(1.0, max(0.0, (p - 0.3) / 0.45)))   # the time ducks under her as she comes over
            give = min(duck, max(0, math.ceil(paws - self.top)))        # but never drops away below her paws
            self.add(cat=(*leap, x, feet - lift), sink=give, behind=paws > self.top + give)   # behind it until she's over it
        self.add(cat=(crouch, 2, land_x, land_feet), sink=sink)        # touch down
        self.add(cat=(crouch, 1, land_x, land_feet + 1), sink=sink + 1)   # the time gives a little under her
        for tag in ("meow_sit", "wash_sit", "yawn_sit"):
            self.anim(tag, land_x, land_feet, sink=sink)
        self.hold("meow_sit", 0, land_x, 4, feet=land_feet, sink=sink)
        for i in (0, 1):                                     # gathers herself
            self.add(cat=(crouch, i, land_x, land_feet), sink=sink)
        end_x = land_x + 18
        spring = [sink - 4, sink - 8, -1, -2, -1, 0]       # once she's past it: up in front of her, overshoot, settle
        passed = False
        for k in range(1, n + 1):
            p = k / n
            x = land_x + (end_x - land_x) * p
            feet = land_feet + (FLOOR - land_feet) * p - 9 * math.sin(math.pi * p)
            paws = feet + lift
            if not passed and paws > self.top + sink and p > 0.5:
                passed = True                                # she drops past its top: behind it from here
                back = sink
            elif passed and spring:
                back = spring.pop(0)
            else:
                back = sink                                  # it waits under her while she's over it
            self.add(cat=(*leap, x, feet - lift), sink=back, behind=passed)
        for back in spring or [0]:                           # the rest of the spring as she lands
            self.add(cat=(crouch, 1, end_x, FLOOR), sink=back, behind=True)
        self.walk(end_x, self.right_off, behind=True)        # off behind the time she came down behind

    def scratch(self):
        """She stretches up under the last digit and claws at it; it shakes."""
        target = len(self.text) - 1
        tag = "on_hind_legs"
        last = len(self.art.animations[tag]) - 1
        centre = self.xs[target] + self.font.width(self.text[target]) / 2
        cx = self.walk(self.right_off, round(centre) + 2)   # clear of the digit before it
        self.hold("walk_left", self.last_step, cx, 2)
        self.hold("meow_sit", 0, cx, 5, over={target}, edged=True)   # under the digit the whole time she's there
        for loop in range(4):                                # up on her hind legs, claws in the digit
            for i in range(last + 1):
                for t in range(self.ms(tag, i)):
                    shake = (-1 if (loop + t) % 2 else 1, 0) if i == last else (0, 0)
                    self.add(cat=(tag, i, cx, FLOOR), offs={target: shake}, over={target}, edged=True)
        self.anim("wash_sit", cx, over={target}, edged=True)
        self.walk(cx, self.right_off)

    def nap(self):
        """She curls up at the left end, where the Zs can rise clear of the time."""
        sleep = "sleep3_l"
        head = extent(self.art, sleep, 0)
        cx = self.walk(self.right_off, round(1 - head[0]))  # the left of her (her head) at x 1
        still = dict(over={0}, edged=True)                   # the first digit stays on top of her while she's here
        self.frames[-1].update(still)                        # from the moment she stops
        self.hold("walk_left", self.last_step, cx, 2, **still)
        self.anim("yawn_sit", cx, **still)
        self.anim("sleep1_l", cx, loops=2, **still)
        top = FLOOR + head[1]
        zs, frames = [], 0
        while frames < 90:
            for i in range(len(self.art.animations[sleep])):
                for _ in range(self.ms(sleep, i)):
                    if frames % 12 == 0:
                        zs.append([2.0, top - 1.0])
                    for z in zs:
                        z[1] -= 0.4
                        z[0] += 0.12
                    zs = [z for z in zs if z[1] > 3]
                    self.add(cat=(sleep, i, cx, FLOOR), z=[(x, y, min(1.0, (y - 2) / 6)) for x, y in zs],
                             over={0}, edged=True)
                    frames += 1
        self.anim("yawn_lie", cx, **still)
        self.anim("yawn_sit", cx, **still)
        self.walk(cx, self.left_off)

    def colon(self):
        """She stands up under the colon and bats at it; the lower dot flies off and bounces, and
        springs home after she's gone."""
        c = self.text.index(":")
        mask = Image.new("L", (W, H))
        self.font.draw(mask, self.xs[c], BASE, ":", 255)
        a = np.array(mask) > 0
        rows, cols = np.flatnonzero(a.any(1)), np.flatnonzero(a.any(0))
        size = (int(cols.max() - cols.min() + 1), int(np.sum(rows < rows.min() + 4)))
        top_dot = (int(cols.min()), int(rows.min()))
        low_dot = (int(cols.min()), int(rows.max() - size[1] + 1))
        tag = "on_hind_legs"
        f = self.art.animations[tag][-1]                     # her reaching paw: the right-hand top pixel of the stretch
        ys, xs_ = np.nonzero(np.array(f.image.getchannel("A")) > 0)
        paw = xs_[ys == ys.min()].max() + f.dx - body_offset(self.art, tag)
        cx = self.walk(self.left_off, round(cols.min() + 1 - paw))
        self.hold("walk_right", self.last_step, cx, 2)
        self.hold("meow_sit", 0, cx, 6)
        last = len(self.art.animations[tag]) - 1
        for loop in range(3):                                # up on her hind legs, batting at it
            for i in range(last + 1):
                for t in range(self.ms(tag, i)):
                    jig = (0, -1) if (i == last and t == 0) else (0, 0)
                    self.add(cat=(tag, i, cx, FLOOR), offs={c: jig}, over={c})
        x, y = float(low_dot[0]), float(low_dot[1])          # the lower dot comes loose, off to the right
        vx, vy = 0.8, -0.5
        loose = dict(hide={c}, dot_size=size)
        while True:
            vy += 0.5
            x += vx
            y += vy
            if y >= FLOOR - size[1]:
                y = FLOOR - size[1]
                if abs(vy) < 1.5:
                    break
                vy, vx = -vy * 0.45, vx * 0.6
            self.add(cat=("meow_sit", 0, cx, FLOOR), dots=[top_dot, (x, y)], **loose)
        self.hold("meow_sit", 0, cx, 4, dots=[top_dot, (x, y)], **loose)
        self.anim("meow_sit", cx, loops=2, dots=[top_dot, (x, y)], **loose)
        self.walk(cx, self.right_off, dots=[top_dot, (x, y)], **loose)    # she leaves
        self.hold_dots(4, [top_dot, (x, y)], loose)
        while x > low_dot[0] + 0.5:                                       # it rolls back along the floor...
            x = max(float(low_dot[0]), x - 1)
            self.add(dots=[top_dot, (x, y)], **loose)
        x = float(low_dot[0])
        self.hold_dots(3, [top_dot, (x, y)], loose)
        for dy in (-2, -3, -2, 0):                                        # ...a hop to get going...
            self.add(dots=[top_dot, (x, y + dy)], **loose)
        n, rise = 9, y - low_dot[1]
        for k in range(1, n + 1):                                         # ...and straight up the gap, home
            p = k / n
            overshoot = 2 * math.sin(math.pi * p) if p > 0.5 else 0
            self.add(dots=[top_dot, (x, y - rise * math.sin(p * math.pi / 2) - overshoot)], **loose)
        self.add()

    def hold_dots(self, n, dots, loose):
        for _ in range(n):
            self.add(dots=dots, **loose)
