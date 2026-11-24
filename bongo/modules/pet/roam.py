"""Free-roaming Life: in a room that ``roams`` (rooms.py) she isn't played from a list of scenes. She
has needs (food, sleep, play, a wash) that grow with time, and does whatever is most pressing, walking
round the furniture to wherever it happens: her bowl, her bed, her ball, a perch she jumps up to.

A Roamer writes her life one action at a time, each a short script (scenes.Script) that carries on
from the last, so the screen can show any moment by the clock. Everything she does comes from a
seeded dice, so the same Matrix on the same day shows the same life (and tests can replay it).

Her needs look after themselves: the feeder fills her bowl when she's hungry. (Owner-managed needs
are to come: see claude/pet-free-roaming-plan.md.)
"""
from __future__ import annotations

import datetime as dt
import math
import random

from . import scenes
from .scenes import LYING, Script, Shot

NIGHT = (22, 7)            # lights out from 22:00 until 07:00
RATES = {"food": 1 / 1500, "sleep": 1 / 1100, "play": 1 / 420, "wash": 1 / 900}   # how fast each need grows, per second
ZZZ = (200, 210, 255)      # (a room shows her Zs in its own colours, by day and by night)
TREE_REST = 240            # seconds on the floor before she fancies the furniture again
MARGIN = dt.timedelta(seconds=2)   # how much of her life either side of now is kept to hand


def like(now: dt.datetime, ref: dt.datetime) -> dt.datetime:
    """``now`` as the same kind of time as ``ref`` (with or without a time zone), so they compare."""
    return now if (now.tzinfo is None) == (ref.tzinfo is None) else now.replace(tzinfo=ref.tzinfo)


def night_at(when: dt.datetime) -> bool:
    return when.hour >= NIGHT[0] or when.hour < NIGHT[1]


class Roamer:
    def __init__(self, art, room, name: str, start: dt.datetime, seed: str):
        self.art, self.room, self.stage, self.name = art, room, scenes.stage_for(art, room), name
        self.rng = random.Random(seed)
        self.clock = start                    # when her next action begins
        self.needs = {k: self.rng.uniform(0.1, 0.6) for k in RATES}
        self.last: Shot | None = None
        self.did: list[str] = []              # what she has done, newest last (for variety, and for tests)
        self.recent: list = []                # her last few actions: (start, shots, what)
        self.ball_was = None
        self.grounded = start - dt.timedelta(seconds=TREE_REST)   # when she last came down off the furniture

    def around(self, now: dt.datetime) -> list:
        """Her actions around ``now``: the one she's in, and as many either side as cover MARGIN, so
        what looks back and ahead from now (the view's glide) has them all. A new list when it changes."""
        now = like(now, self.clock)
        recent = list(self.recent)
        while not recent or self.clock <= now + MARGIN:          # the clock: when her last action ends
            recent.append(self.next())
        while len(recent) > 1 and recent[1][0] <= now - MARGIN:  # gone by long enough ago
            recent.pop(0)
        if len(recent) != len(self.recent) or recent[0] is not self.recent[0] or recent[-1] is not self.recent[-1]:
            self.recent = recent
        return self.recent

    # -- one action ---------------------------------------------------------------------------
    def next(self) -> tuple[dt.datetime, list[Shot], str]:
        """Her next action: (when it starts, its shots, what it was)."""
        s = self.script()
        what = self.choose()
        getattr(self, what)(s)
        if not s.shots:                       # nothing to do after all: she sits a moment
            s.hold("meow_sit", 0, 1000)
        began, seconds = self.clock, s.elapsed / 1000
        for need, rate in RATES.items():
            self.needs[need] = min(1.0, self.needs[need] + rate * seconds)
        self.clock += dt.timedelta(milliseconds=s.elapsed)
        self.last = s.shots[-1]
        self.ball_was = self.last.ball or self.ball_was       # (in the dark it isn't drawn, but it is still there)
        self.did = (self.did + [what])[-12:]
        return began, s.shots, what

    def script(self) -> Script:
        last = self.last
        if last is None:
            if night_at(self.clock):          # she starts the night where she'd be: asleep in bed, lights out
                s = Script(self.art, self.stage, room="night", cat=list(self.stage.bed))
                s.overlays = [("zzz", ZZZ)]
                s.shot("sleep3_l", 0, 1)
                last = s.shots[0]
            else:
                s = Script(self.art, self.stage, cat=list(self.stage.centre), free=True)
                s.ball = list(self.stage.at(44, self.art.tuning.get("ball", scenes.BALL_HOME)[1]))   # out on the open floor
                return s
        s = Script(self.art, self.stage, room=last.room, cat=list(last.cat), perch=last.perch, free=True)
        s.ball = list(last.ball or self.ball_was or self.stage.at(44, self.art.tuning.get("ball", scenes.BALL_HOME)[1]))
        s.enter = last
        if last.tag in LYING:
            s.overlays = [o for o in last.overlays if o[0] == "zzz"]
        return s

    def choose(self) -> str:
        if night_at(self.clock):
            return "night"
        if self.last is not None and self.last.room == "night":
            return "morning"
        n = self.needs
        odds = {"eat": max(0.0, n["food"] - 0.55) ** 2 * 30, "nap": n["sleep"] ** 3 * 5, "ball": n["play"] ** 2 * 2.5,
                "mouse": n["play"] ** 2 * 1.2, "zoom": n["play"] ** 3 * 1.2, "wash": n["wash"] ** 2 * 2,
                "wander": 0.5, "sit": 0.5, "bee": 0.05, "perch": 0.2 if self.room.perches else 0,
                "scratch": 0.2 if self.room.posts else 0}
        up = bool(self.last is not None and self.last.perch)
        if not up and (self.clock - self.grounded).total_seconds() < TREE_REST:
            odds["perch"] = 0                 # just been up there
        hour = self.clock.hour + self.clock.minute / 60
        if 0 <= (NIGHT[0] - hour) % 24 <= 1:
            odds["nap"] = 0                   # nearly bedtime: she saves it for the night
        if scenes.stalk_toy(self.art) is None or self.art.tuning.get("toy") == "carrot":
            odds["mouse"] = 0
        if "bee" not in self.art.toys:
            odds["bee"] = 0
        for recent in self.did[-2:]:          # not the same thing three times running
            if self.did[-2:].count(recent) == 2:
                odds[recent] = 0
        if self.did and self.did[-1] in odds and self.did[-1] != "wander":
            odds[self.did[-1]] *= 0 if self.did[-1] == "sit" else 0.25
        names = sorted(odds)
        return self.rng.choices(names, [odds[k] for k in names])[0]

    # -- getting about ------------------------------------------------------------------------
    def down(self, s: Script):
        """Onto the floor and on her feet: wakes if asleep, jumps down from a perch."""
        s.get_up()
        s.overlays = [o for o in s.overlays if o[0] != "zzz"]
        perches = self.room.perches
        here = next((k for k, v in perches.items() if s.perch and math.dist(v[1], s.cat) < 1.5), None)
        while here:
            _, _, below, floor = perches[here]
            if below:
                s.jump_to(*perches[below][1], perch=perches[below][0])
            else:
                s.jump_to(*floor)
                s.perch = ""
                self.grounded = self.clock
            here = below or None
        s.perch = ""

    def walk(self, s: Script, x, y):
        self.down(s)
        for px, py in self.room.route(tuple(s.cat), (x, y)):
            s.go(px, py)

    def open(self, x, y) -> bool:
        """She can stand here with room to sit, in plain sight."""
        return not any(self.room.blocked(x + dx, y + dy) for dx in (-6, 0, 6) for dy in (-2, 0)) and \
            not self.room.hidden(x, y)

    def spot(self, s: Script):
        """Somewhere else on the floor to go: in the open, a proper walk away, not on top of her ball."""
        w, h = self.room.size
        for _ in range(60):
            x, y = self.rng.randrange(10, w - 10), self.rng.randrange(self.room.floor_top + 2, h - 3)
            if self.open(x, y) and math.dist((x, y), s.cat) >= 16 and (s.ball is None or math.dist((x, y), s.ball) >= 14):
                return x, y
        return self.stage.centre

    # -- what she does ------------------------------------------------------------------------
    def eat(self, s: Script):
        if s.room != "day":
            s.fade_to("day")                  # the feeder fills her bowl
        bx, by = self.art.tuning.get("bowl", (0, 0))
        self.walk(s, self.stage.bowl[0] + bx, self.stage.bowl[1] + by)
        s.play("eat_right", loops=self.rng.randint(4, 6))
        s.fade_to("empty")                    # all gone
        self.needs["food"] = 0.0
        if self.rng.random() < 0.6:
            s.play("wash_sit")
            self.needs["wash"] *= 0.4

    def nap(self, s: Script):
        perches = self.room.perches
        if perches and s.perch and self.rng.random() < 0.7:
            pass                              # where she is, up on her perch
        else:
            self.walk(s, *self.stage.bed)
        if not s.lying:
            s.play("yawn_sit")
            s.play("sleep2_l", loops=3)
        s.overlays = [o for o in s.overlays if o[0] != "zzz"] + [("zzz", ZZZ)]
        s.play("sleep3_l", loops=self.rng.randint(20, 50))
        self.needs["sleep"] = 0.0

    def night(self, s: Script):
        if not (s.in_bed and s.room == "night"):
            self.walk(s, *self.stage.bed)
            if not s.lying:
                s.play("yawn_sit")
                s.play("sleep2_l", loops=2)
            if not any(o[0] == "zzz" for o in s.overlays):
                s.overlays.append(("zzz", ZZZ))
            s.play("sleep3_l", loops=3)       # curled up and asleep...
            s.fade_to("night")                # ...then the lights go out
        s.overlays = [("zzz", ZZZ)]
        s.play("sleep3_l", loops=60)          # a few minutes at a time, all night
        self.needs["sleep"] = 0.0

    def morning(self, s: Script):
        s.fade_to("day")                      # light comes up while she still sleeps
        if not any(o[0] == "zzz" for o in s.overlays):
            s.overlays.append(("zzz", ZZZ))
        s.play("sleep1_l", loops=5)
        s.overlays.clear()
        s.play("yawn_lie")
        s.play("wash_lie")
        s.play("meow_lie")
        s.go(self.stage.bed[0] + 14, self.stage.bed[1] - 3)
        s.hold("meow_sit", 0, 900)
        self.needs.update(food=0.9, wash=0.3)

    def wash(self, s: Script):
        if s.lying:
            s.overlays = [o for o in s.overlays if o[0] != "zzz"]
            s.play("yawn_lie")
            s.play("wash_lie", loops=2)
        else:
            s.play("wash_sit", loops=self.rng.randint(2, 3))
            s.hold("meow_sit", 0, 600)
        self.needs["wash"] = 0.0

    def sit(self, s: Script):
        if s.lying:
            s.get_up()
            s.overlays = [o for o in s.overlays if o[0] != "zzz"]
        s.hold("meow_sit", 0, self.rng.randint(1500, 5000))
        s.play(self.rng.choice(["yawn_sit", "scratch_r", "meow_sit", "meow_sit2"]))
        s.hold("meow_sit", 0, self.rng.randint(1500, 5000))

    def wander(self, s: Script):
        self.walk(s, *self.spot(s))
        s.hold("meow_sit", 0, self.rng.randint(1500, 4500))

    def perch(self, s: Script):
        """Up onto the furniture: to the take-off spot, a jump for each level, then a look round."""
        perches = self.room.perches
        top = self.rng.choice(sorted(perches))
        chain, at = [], top
        while at:
            chain.append(at)
            at = perches[at][2]
        chain.reverse()
        here = next((k for k, v in perches.items() if s.perch and math.dist(v[1], s.cat) < 1.5), None)
        if here == top:
            return self.sit(s)                # already there
        if here and (here in chain or perches[here][2] == top):   # from one level to the next: no trip to the floor
            s.get_up()
            s.overlays = [o for o in s.overlays if o[0] != "zzz"]
            chain = chain[chain.index(here) + 1:] if here in chain else [top]
        else:
            self.walk(s, *perches[chain[0]][3])
        for name in chain:
            piece, feet, _, _ = perches[name]
            s.hold("meow_sit", 0, 350)        # sizing it up
            s.jump_to(*feet, perch=piece)
        s.hold("meow_sit", 0, self.rng.randint(900, 2000))
        s.play(self.rng.choice(["yawn_sit", "wash_sit", "meow_sit"]))
        s.hold("meow_sit", 0, self.rng.randint(800, 2500))

    def scratch(self, s: Script):
        self.walk(s, *self.rng.choice(self.room.posts))
        s.play("paw_right", loops=self.rng.randint(3, 5))
        s.hold("meow_sit", 0, 700)
        self.needs["play"] *= 0.7

    def ball(self, s: Script):
        """Bats her ball about the open floor and chases it, wherever it lies."""
        reach = self.art.tuning.get("reach", 10)
        w = self.room.size[0]
        for _ in range(self.rng.randint(2, 4)):
            bx, by = s.ball
            far = self.rng.randint(18, 40)

            def run(side):   # how far it can roll that way, staying where she can be seen playing with it
                to = bx
                while abs(to - bx) < far and not self.room.blocked(to + side * 6, by):
                    to += side * 2
                while to != bx and (self.room.hidden(to, by) or not self.open(to + side * reach, by)):
                    to -= side * 2                                 # (and where she can get round to its far side)
                if side == -1 and "paw_left" not in self.art.animations or self.room.blocked(bx - side * reach, by):
                    return -1, to
                return abs(to - bx) + (100 if self.open(bx - side * reach, by) else 0), to   # from the open, if she can

            first = 1 if (bx < w / 2) == (self.rng.random() < 0.8) else -1    # mostly towards the open side
            (room_, to), side = max((run(first), first), (run(-first), -first),
                                    key=lambda r: (r[0][0] >= 112, r[0][0] % 100 >= 12, r[1] == first))
            if room_ % 100 < 6 or room_ < 0:
                break                                              # nowhere for it to go from here
            paw = "paw_right" if side == 1 else "paw_left"
            self.walk(s, bx - side * reach, by)
            s.play(paw)
            while abs(s.ball[0] - to) > 0.25:                               # it rolls, slowing to a stop
                step = max(0.3, min(2.0, abs(to - s.ball[0]) * 0.25))
                s.ball[0] += step if to > s.ball[0] else -step
                s.shot(paw, 0, 60)
            s.ball[0] = to
        s.play("on_hind_legs")
        s.hold("on_hind_legs", 2, 500)
        s.play("on_hind_legs", frames=[1, 0])
        self.needs["play"] = 0.0

    def _scene(self, s: Script, mode: str):
        self.walk(s, *self.stage.centre)   # to the open floor the scenes are written for
        scenes.MODES[mode](s, self.name)

    def mouse(self, s: Script):
        self._scene(s, "toy")
        self.needs["play"] *= 0.3

    def bee(self, s: Script):
        self._scene(s, "bee")

    def zoom(self, s: Script):
        """Zoomies: dashes to and fro across the open floor (the view stays put while she tears about)."""
        self.walk(s, *self.stage.centre)
        cx, cy = s.cat
        speed, stride = self.art.tuning.get("zoomies", (3.0, 3.0))
        s.overlays.append(("dust",))
        side = self.rng.choice((-1, 1))
        for _ in range(self.rng.randint(4, 6)):
            for _try in range(12):
                x, y = cx + side * self.rng.randint(18, 26), cy + self.rng.randint(-5, 5)
                if self.open(x, y) and self.room.clear(tuple(s.cat), (x, y)):
                    s.walk_to(x, y, speed=speed, stride_mult=stride)
                    break
            side = -side
        s.overlays = [o for o in s.overlays if o[0] != "dust"]
        s.play("on_hind_legs")
        s.hold("meow_sit", 0, 900)
        self.needs["play"] = 0.0
        self.needs["sleep"] = min(1.0, self.needs["sleep"] + 0.15)


_LIVES: dict = {}   # (room, pet, name) -> her Roamer, so she carries on where she was when her screen comes round again


def roamer(art, key: str, room, pet, now: dt.datetime) -> Roamer:
    who = (key, pet.kind, pet.coat, pet.name)
    r = _LIVES.get(who)
    if r is not None:
        now = like(now, r.clock)
    stale = r is None or now - r.clock > dt.timedelta(minutes=25) or (r.recent and now < r.recent[0][0])
    if stale:   # first time, long away, or the clock went back: she starts afresh from what the time of day says
        r = _LIVES[who] = Roamer(art, room, pet.name, now, f"{who}{now.date().isoformat()}{now.hour}")
    r.art = art       # today's outfit
    return r
