"""Pet modes as short scripts (walk to the bowl, eat, wash...), recorded into timelines.

Ported from the remastered Cat 10 engine. A script moves the cat and plays reference
animations; every step is recorded as a Shot, and a style plays the shots by the clock.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .art import H, W, Art

BED = (16, 29)        # sitting, lying or curled-up cat lands on the cushion
BOWL_SPOT = (50, 18)  # standing left of the bowl, facing right
BEG_SPOT = (48, 23)   # below the bowl: room for the tall hind-legs pose
CENTRE = (36, 26)

WALKS = [(0, "walk_right"), (45, "walk_right_d"), (90, "walk_down"), (135, "walk_left_d"),
         (180, "walk_left"), (225, "walk_left_up"), (270, "walk_up"), (315, "walk_right_up")]
STRIDE = {"walk_right": 1.0, "walk_left": 1.0, "walk_right_d": 1.1, "walk_left_d": 1.1,
          "walk_right_up": 1.1, "walk_left_up": 1.1, "walk_down": 1.0, "walk_up": 1.0}


@dataclass(frozen=True)
class Shot:
    tag: str
    index: int
    cat: tuple[float, float]
    ball: tuple[float, float] | None
    room: str               # day, empty or night
    overlays: tuple         # ("zzz", colour) / ("bubble", "!") / ("banner", text) / ("tag", name, from_ms) / ("dust",) / ("grumble",)
    ms: float
    moving: int = 0         # -1 left, 0 still, 1 right (for dust)


@dataclass
class Script:
    art: Art
    room: str = "day"
    cat: list = field(default_factory=lambda: list(CENTRE))
    ball: list | None = field(default_factory=lambda: [36, 28])
    overlays: list = field(default_factory=list)
    shots: list = field(default_factory=list)
    _moving: int = 0

    def shot(self, tag, i, ms):
        ball = tuple(self.ball) if self.ball is not None and self.room != "night" else None
        self.shots.append(Shot(tag, i, tuple(self.cat), ball, self.room, tuple(self.overlays), ms, self._moving))

    @property
    def elapsed(self):
        return sum(s.ms for s in self.shots)

    def play(self, tag, loops=1, speed=1.0, frames=None):
        anim = self.art.animations[tag]
        for _ in range(loops):
            for i in (frames if frames is not None else range(len(anim))):
                self.shot(tag, i, anim[i].ms / speed)

    def hold(self, tag, i, ms):
        while ms > 0:
            step = min(ms, 250)
            self.shot(tag, i, step)
            ms -= step

    def walk_to(self, x, y, speed=1.0, stride_mult=1.0):
        dx, dy = x - self.cat[0], y - self.cat[1]
        dist = math.hypot(dx, dy)
        if dist < 0.5:
            return
        ang = math.degrees(math.atan2(dy, dx)) % 360
        have = [w for w in WALKS if w[1] in self.art.animations]  # not every direction is in the atlas
        tag = min(have, key=lambda w: min(abs(ang - w[0]), 360 - abs(ang - w[0])))[1]
        steps = max(1, int(round(dist / (STRIDE[tag] * stride_mult))))
        anim = self.art.animations[tag]
        sx, sy = self.cat
        self._moving = 1 if dx > 0.5 else -1 if dx < -0.5 else 0
        for i in range(1, steps + 1):
            self.cat = [sx + dx * i / steps, sy + dy * i / steps]
            self.shot(tag, i % len(anim), anim[i % len(anim)].ms / speed)
        self._moving = 0


def idle(s, name):
    s.cat = list(CENTRE)
    s.hold("meow_sit", 0, 1500)
    s.play("yawn_sit")
    s.hold("meow_sit", 0, 1200)
    s.play("scratch_r")
    s.hold("meow_sit", 0, 1000)


def eating(s, name):                       # bed -> bowl -> eat -> wash
    s.cat = list(BED)
    s.hold("meow_sit", 0, 800)
    bx, by = s.art.tuning.get("bowl", (0, 0))  # a smaller animal stands nearer the bowl
    s.walk_to(BOWL_SPOT[0] + bx, BOWL_SPOT[1] + by)
    s.play("eat_right", loops=4)
    s.walk_to(42, 25)
    s.play("wash_sit")


def washing(s, name):
    s.cat = [40, 26]
    s.play("wash_sit", loops=3)
    s.hold("meow_sit", 0, 900)


def playing(s, name):                      # bat the ball, chase it, bat it off the wall and back
    reach = s.art.tuning.get("reach", 10)  # how far in front of her feet the ball sits when she bats it
    s.cat, s.ball = [36 - reach, 28], [36, 28]

    def roll(to, speed, brake=True):       # the ball rolls towards ``to`` (slowing to a stop there, if braking)
        while abs(s.ball[0] - to) > 0.25:
            step = max(0.3, min(speed, abs(to - s.ball[0]) * 0.25)) if brake else min(speed, abs(to - s.ball[0]))
            s.ball[0] += step if to > s.ball[0] else -step
            s.shot("paw_right", 0, 60)
        s.ball[0] = to

    s.play("paw_right")
    roll(46, 2.0)                          # across the room, slowing in the middle
    s.walk_to(46 - reach, 28)              # she follows it
    s.play("paw_right")
    roll(58, 1.8, brake=False)             # hits the wall at speed...
    roll(s.cat[0] + reach, 1.2)            # ...and bounces back, a little slower, to her paws
    s.play("paw_right")
    roll(s.ball[0] + 6, 1.2)               # one last little tap
    s.play("on_hind_legs")
    s.hold("on_hind_legs", 2, 600)


def zoomies(s, name):
    """No run cycle in the art, so the real walk cycles played fast with a long stride."""
    s.overlays.append(("dust",))
    s.cat = [-14, 26]
    speed, stride = s.art.tuning.get("zoomies", (3.0, 3.0))
    fast = dict(speed=speed, stride_mult=stride)
    for x, y in ((30, 26), (50, 21), (78, 21)):
        s.walk_to(x, y, **fast)
    s.cat = [78, 23]
    for x, y in ((46, 23), (30, 28), (-14, 28)):
        s.walk_to(x, y, **fast)
    s.cat = [-14, 24]
    s.walk_to(34, 26, **fast)
    s.overlays.clear()
    s.play("on_hind_legs")
    s.hold("meow_sit", 0, 900)
    s.play("wash_sit")


def napping(s, name):                      # yawn, walk to bed, settle, curl up
    s.cat = [46, 26]
    s.play("yawn_stand")
    s.walk_to(*BED)
    s.play("sleep2_l", loops=4)
    s.overlays.append(("zzz", (200, 210, 255)))
    s.play("sleep3_l", loops=10)


def waking(s, name):                       # lying in bed -> yawn -> wash -> up
    s.cat = list(BED)
    s.overlays.append(("zzz", (200, 210, 255)))
    s.play("sleep1_l", loops=5)
    s.overlays.clear()
    s.play("yawn_lie")
    s.play("wash_lie")
    s.play("meow_lie")
    s.walk_to(40, 26)
    s.hold("meow_sit", 0, 900)


def lights_out(s, name):
    s.room = "night"
    s.cat = list(BED)
    s.overlays.append(("zzz", (120, 140, 210)))
    s.play("sleep3_l", loops=14)


def asking(s, name):
    s.room = "empty"
    s.cat = [40, 26]
    s.overlays.append(("bubble", "!"))
    s.play("meow_sit", loops=5)
    s.hold("meow_sit", 0, 500)
    s.play("meow_sit2", loops=3)


def hungry(s, name):                       # a banner scrolls "MOCHI IS HUNGRY!" while she meows
    s.room = "empty"
    s.cat = [40, 27]
    message = f"{name.upper()} IS HUNGRY!"
    s.overlays.append(("banner", message))
    scroll_ms = (W + len(message) * 4) / 12 * 1000
    while s.elapsed < scroll_ms:
        s.play("meow_sit")


def name_tag(s, name):
    s.cat = [-14, 26]
    s.overlays.append(("tag", name.upper(), 4600))
    s.walk_to(32, 26)
    s.hold("meow_sit", 0, 2500)


def begging(s, name):                      # at the empty bowl, up on her hind legs
    s.room = "empty"
    s.cat = [36, 26]
    s.walk_to(*BEG_SPOT)
    for _ in range(2):
        s.play("on_hind_legs")
        s.hold("on_hind_legs", 2, 900)
        s.play("on_hind_legs", frames=[2, 1, 0])
        if _ == 0:
            s.play("meow_stand", loops=3)


def grumpy(s, name):
    s.cat = [40, 26]
    s.overlays.append(("grumble",))
    s.play("hiss_r", loops=5)
    s.overlays.clear()
    s.walk_to(*BED)
    s.play("sleep2_l", loops=5)


def scratching(s, name):
    s.cat = [32, 26]
    s.play("scratch_r", loops=2)
    s.hold("meow_sit", 0, 700)
    s.play("scratch_r")
    s.hold("meow_sit", 0, 900)


MODES = {f.__name__: f for f in (idle, eating, washing, playing, zoomies, napping, waking, lights_out,
                                  asking, hungry, name_tag, begging, grumpy, scratching)}


def record(art: Art, mode: str, name: str) -> list[Shot]:
    script = Script(art)
    if "ball" in art.tuning:  # where the ball lies when nobody's playing (clear of where she sits)
        script.ball = list(art.tuning["ball"])
    MODES[mode](script, name)
    return script.shots
