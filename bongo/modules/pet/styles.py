"""Pet styles: each mode is a style, Life strings modes together, Wander is an overlay."""
from __future__ import annotations

import bisect
import hashlib
import math

from PIL import Image, ImageDraw

from bongo.module import Style

from .art import H, W, load, missing, no_art
from bongo.pets import wearing

from . import places, roam
from .scenes import BANNER_SLIDE_MS, MODES, STRIDE, TAG_RISE_MS, record_loop, stage_for

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


CAT_HEAD = 18   # the sitting cat's height the overlays were placed for; a kind's tuning "head" moves them down


def overlay(draw, item, t, shot, life=float("inf"), view=(0, 0, W, H), head=CAT_HEAD):
    """Speech bubbles, Zs, banners and the name tag, drawn over the scene ``t`` ms after the item
    appeared; ``life``: how long it stays, so each comes and goes gently (no Z is ever left mid-air).
    ``view`` is the part of the room on screen (x, y, width, height): they are kept inside it."""
    kind, cx, cy = item[0], int(shot.cat[0]), int(shot.cat[1])
    cy += max(0, CAT_HEAD - head)   # a smaller pet: Zs, bubble, tag and grumble start lower, nearer her head
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
        fy = int(shot.cat[1])                         # dust stays at her feet
        draw.point((bx, fy - 1), fill=(240, 220, 190))
        draw.point((bx - 2 * shot.moving, fy - 3), fill=(240, 220, 190))
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


def fit(scene: Image.Image, width: int, height: int, feet=None, tall: int = 18, above: int = 1) -> Image.Image:
    """The part of the room that ``window`` picks."""
    if (width, height) == scene.size:
        return scene
    x, y = window(width, height, feet, tall, above, scene.size)
    return scene.crop((x, y, x + width, y + height))


class Camera:
    """The view in a room bigger than the screen (Billy 24 Nov: "not so jittery", then "we need a mix of the two").
    It frames her as the old camera did, near the middle with room ahead of her as she walks, but moves like the
    steady one: it aims LEAD px ahead of her in the way she is walking and lets her drift ZONE px either side of
    that before it moves; then it moves with her, step for step, so she holds still on screen and the room goes by.
    Changes of aim (she sets off, turns round, stops) glide an even GLIDE px a frame; it never turns straight back
    within TURN_MS; still for STILL_MS, it glides to put her in the middle. It never loses her: up to RUSH a frame
    (more if asked less often, as the dashboard's preview is) to keep her on screen. Up and down it keeps
    FLOOR rows of floor under her and room for her tallest pose. Her ball or toy mouse stays in view while she
    plays if both fit. A gap over FRESH_MS, or the clock going back, starts it afresh with her in the middle.
    While it gets ahead of her she eases back on screen a pixel a step; between her steps it holds still."""
    LEAD = 8              # columns it looks ahead of her while she walks one way
    LEAD_MS = 300         # she has walked one way this long before it looks ahead
    WALK_MS = 300         # she counts as walking for this long after a step (slow walks pause up to 300 ms)
    TURN_PX = 2           # she has gone this far back the other way before it counts as turning round
    ZONE = 4              # columns she may drift either side of where it aims before it moves
    MARGIN = (3, 0)       # columns / rows kept between her and the edge (sideways: for her ball lean)
    FLOOR = 5             # rows of floor kept under her feet in a room taller than the screen
    GLIDE = 2             # pixels a frame for a glide while she stands (frames are 50 ms at 20 fps)
    CATCH = 1             # while she walks, it gets ahead of her only as she steps: her step and CATCH more
    RUSH = (12, 6)        # the most it moves in one frame to keep all of her on screen
    TURN_MS = 200         # it won't glide back the way it just came within this long
    FRAME_MS = 50         # a frame on the panel (the Pet module draws at 20 fps)
    FRESH_MS = 1000       # after a gap this long (or the clock going back) it starts afresh: her in the middle
    BALL_REACH = 5        # columns either side of her ball or toy mouse kept on screen while she plays
    STILL_MS = 600        # still this long, and the view glides to put her back in the middle

    def __init__(self):
        self.at = None        # (x, y): the view's top left
        self.clock = None     # when it was last asked, in ms
        self.turned = {}      # axis -> (which way, when) it last moved
        self.feet = None      # where her feet were (middle, feet row)
        self.still = None     # since when they have been there
        self.stepped_at = None   # when she last stepped sideways
        self.way = (0, None)  # which way she is walking (-1, 0, 1), and since when
        self.back = 0         # how far she has gone back against that way (a turn once it reaches TURN_PX)

    @staticmethod
    def hold(at, lo, hi, size, limit, spare, head_first=False):
        """Along one axis: the start nearest ``at`` that keeps lo..hi on screen with ``spare`` to spare. When
        that won't fit: her head at the top edge (head_first), or her middle in the middle."""
        if hi - lo + 1 <= size - 2 * spare:      # lo..hi are rows/columns she takes up, both ends included
            at = min(at, lo - spare)
            at = max(at, hi + spare - size + 1)
        else:
            at = lo if head_first else (lo + hi) // 2 - size // 2
        return max(0, min(limit, at))

    def view(self, clock, her, thing, size, room):
        """Where the view starts now. ``her`` is (left, top, right, feet): her reach either side of her feet,
        her tallest pose with a clear row over her ears, and her feet; ``thing`` is her ball or toy mouse to
        keep in view while she plays, or None; ``size`` the screen and ``room`` the room."""
        (w, h), (rw, rh) = size, room
        left, top, right, feet = her
        mid = (left + right) // 2
        if self.at is not None and clock == self.clock:
            return self.at                          # asked twice for the same moment (two previews): no move
        limit = (max(0, rw - w), max(0, rh - h))
        floor = feet + (self.FLOOR if rh > h else 1)
        fresh = self.at is None or not 0 <= clock - self.clock <= self.FRESH_MS
        missed = 1 if fresh else max(1, round((clock - self.clock) / self.FRAME_MS))   # frames since it was last asked
        frames = min(2, missed)                     # a late frame's glide catches up one; a slow preview's keep-up all
        if fresh:
            self.at, self.turned = (mid - w // 2, floor - h + 1), {}   # her in the middle; refined below
            self.feet, self.still, self.stepped_at, self.way, self.back = None, clock, None, (0, None), 0
        stepped = 0 if self.feet is None else mid - self.feet[0]   # how far she moved sideways this frame
        if stepped:
            self.stepped_at = clock
            if self.way[0] == 0 or stepped * self.way[0] > 0:  # on her way (or setting off)
                self.back = 0
                if self.way[0] == 0:
                    self.way = (1 if stepped > 0 else -1, clock)
            else:                                   # back against her way: a turn only after TURN_PX
                self.back += abs(stepped)
                if self.back >= self.TURN_PX:
                    self.way, self.back = (1 if stepped > 0 else -1, clock), 0
        if self.feet != (mid, feet):
            self.feet, self.still = (mid, feet), clock
        walking = self.stepped_at is not None and clock - self.stepped_at <= self.WALK_MS
        between = walking and not stepped           # her feet are still between two strides
        if not walking and self.way[0]:
            self.way, self.back = (0, None), 0      # she stopped: no more looking ahead until she walks again
        settled = not fresh and clock - self.still >= self.STILL_MS and thing is None
        way, since = self.way
        lead = way * self.LEAD if walking and since is not None and clock - since >= self.LEAD_MS else 0
        aim = mid + lead - w // 2
        self.clock = clock
        at = list(self.at)
        # sideways: within ZONE of the aim (none once she has settled), and her ball too if both fit
        zone = 0 if settled else self.ZONE
        goal_x = max(aim - zone, min(aim + zone, at[0]))
        # she has stopped: it stays put until she settles (never pans out to finish its look ahead, then back)
        if not walking and not settled:
            goal_x = at[0]
        if thing is not None:
            lo, hi = min(left, thing[0] - self.BALL_REACH), max(right, thing[0] + self.BALL_REACH)
            if hi - lo + 1 <= w - 2 * self.MARGIN[0]:
                goal_x = self.hold(goal_x, lo, hi, w, limit[0], self.MARGIN[0])
        goals = (max(0, min(limit[0], goal_x)), self.hold(at[1], top, floor, h, limit[1], 0, head_first=True))
        if fresh:
            self.at = goals
            return self.at
        for a, (size_a, near, far) in enumerate(((w, left, right), (h, top, feet + 1))):
            d = goals[a] - at[a]
            step_way = (d > 0) - (d < 0)
            last = self.turned.get(a)
            back = last and last[0] == -step_way and clock - last[1] < self.TURN_MS
            if a == 0 and stepped:                  # she stepped: it goes with her and CATCH more, so she eases
                go = d * stepped > 0 and not back      # back a pixel a step; never against her step
                moved = at[a] + (step_way * min(abs(d), abs(stepped) + self.CATCH) if go else 0)
            elif a == 0 and between:                # between her strides it holds (she'd drift back while still)
                moved = at[a]
            else:
                step = 0 if back else (abs(d) if abs(d) <= self.GLIDE and not settled else min(abs(d), self.GLIDE * frames))
                moved = at[a] + step_way * step
            if far - near < size_a:                 # all of her (and a row of floor) stays on screen, whatever
                keep = max(far - size_a + 1, min(near, moved))
                moved = at[a] + max(-self.RUSH[a] * missed, min(self.RUSH[a] * missed, keep - at[a]))
            moved = max(0, min(limit[a], moved))
            if moved != at[a]:
                self.turned[a] = ((moved > at[a]) - (moved < at[a]), clock)
            at[a] = moved
        self.at = (at[0], at[1])
        return self.at


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
        # a bigger room has its own size, bed, bowl and floor; the house is her own home (the pack kinds' room
        # keeps its old layout) and the places all have the front-floor one
        self.stage = stage_for(self.art, self.place)
        day, empty, night = places.rooms(self.settings["place"]) if self.place else (self.art.room, self.art.empty, self.art.night)
        self.rooms = {"day": day, "empty": empty, "night": night}
        self.timeline = record_loop(self.art, self.modes(), self.ctx.pet.name, self.stage)   # seamless: scenes link up
        self.starts, t = [], 0.0
        for shot, _ in self.timeline:
            self.starts.append(t)
            t += shot.ms
        self.total = t
        self.lives = lifetimes(self.timeline, self.starts, self.total)
        self.camera, self.last_view = Camera(), None
        frames = [f for a in self.art.animations.values() for f in a]   # her widest reach from her feet and her
        self.reach = (max(max(-f.dx, f.dx + f.image.width) for f in frames),   # tallest pose: the view keeps room
                      max(max(f.image.height, -f.dy) for f in frames))         # for them, so it doesn't follow her legs

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
        big = (self.stage.w, self.stage.h) != (W, H)
        empty = img.copy() if big else None   # the room without her: what's in front of her is drawn back from it
        ball_on_top = None
        if shot.ball:
            ball = art.ball
            if shot.fade and "night" in (shot.fade[0], shot.room):   # dims with the light
                lit = 1 - shot.fade[1] if shot.room == "night" else shot.fade[1]
                ball = ball.copy()
                ball.putalpha(ball.getchannel("A").point(lambda a: int(a * lit)))
            if self.stage.front and shot.cat[1] < shot.ball[1]:   # she's further back up the floor: it's in front
                ball_on_top = ball
            else:
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
        depth = self.stage.scale(shot.cat[1])
        if depth < 1:   # further back up the floor, smaller (whole rows of height, through her feet)
            frame = art.at_depth(frame, self.stage.rows(art.ref_height, shot.cat[1]))
        # (rounded half up, the one place her position is rounded: never banker's rounding, which would
        # alternate on half pixels)
        x, y = math.floor(shot.cat[0] + 0.5) + frame.dx, math.floor(shot.cat[1] + 0.5) + frame.dy
        layer = Image.new("RGBA", img.size)
        layer.paste(frame.image, (x, y), frame.image)
        img.alpha_composite(layer)
        if ball_on_top is not None:
            img.alpha_composite(ball_on_top, (int(shot.ball[0]) - 4, int(shot.ball[1]) - 4))
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
        feet = shot.cat
        # a banner pushes her down as it slides in, so her face stays under it
        above = 1 + max((banner_rows(t - began, ended - began) for item, began, ended in self.lives[k] if item[0] == "banner"), default=0)
        if any(item[0] == "zzz" for item, _, _ in self.lives[k]):
            above = max(above, 4)                     # room for a Z to rise
        tall = max(frame.image.height, -frame.dy)   # her top row in this frame, counted up from her feet
        if big:   # the room is bigger than the screen: the camera follows her (Camera)
            thing = None
            if self.timeline[k][1] in PLAY:
                for it in (shot.ball, shot.toy and shot.toy[0] != "bee" and shot.toy[1:3]):
                    if it and abs(it[1] - shot.cat[1]) < 16 and 0 <= it[0] < img.width:
                        thing = (round(it[0]), round(it[1]))
                        break
            cx, cy = round(feet[0]), round(feet[1])
            side, tallest = self.reach
            her = (cx - side, cy - tallest - 1, cx + side, cy)   # room for her tallest pose and a clear row (banners and
            #   Zs fit in that room: test_pet_rooms checks; going by them would make the view hop as a banner comes up)
            self.last_view = self.camera.view(clock, her, thing, (w, h), img.size)
            view = (*self.last_view, w, h)
        else:
            view = (*window(w, h, feet, tall, above, img.size), w, h)   # the part of the room on screen: she is in it
            self.last_view = view[:2]
        for item, began, ended in self.lives[k]:
            if item[0] == "zzz" and self.place:   # Zs that show up on a pale floor, changing with the light
                dark = 1.0 if shot.room == "night" else 0.0
                if shot.fade and "night" in (shot.fade[0], shot.room):
                    dark = shot.fade[1] if shot.room == "night" else 1 - shot.fade[1]
                item = ("zzz", tuple(round(a + (b - a) * dark) for a, b in zip(self.place.zzz, NIGHT_ZZZ)))
            overlay(draw, item, t - began, shot, ended - began, view, round(art.tuning.get("head", CAT_HEAD) * depth))
        if big:
            x0, y0 = self.last_view
            return img.convert("RGB").crop((x0, y0, x0 + w, y0 + h))
        return fit(img.convert("RGB"), self.ctx.width, self.ctx.height, feet, tall, above)

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
