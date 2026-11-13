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
    toy: tuple | None = None  # (name, x, y, flipped, bitten px): a toy on the floor (or a bee in the air)
    fade: tuple | None = None  # (from room, 0..1): the room is fading from another (lights going out, the bowl filling)


@dataclass
class Script:
    art: Art
    room: str = "day"
    cat: list = field(default_factory=lambda: list(CENTRE))
    ball: list | None = field(default_factory=lambda: [36, 28])
    toy: list | None = None   # [name, x, y, flipped, bitten]
    overlays: list = field(default_factory=list)
    shots: list = field(default_factory=list)
    _moving: int = 0
    fade: tuple | None = None
    enter: Shot | None = None   # the last shot of the scene before (None: this scene starts the loop afresh)
    origin: tuple | None = None # where this scene starts when it starts afresh (the loop's first scene)

    @property
    def entering(self) -> bool:
        return self.enter is not None

    @property
    def last(self) -> Shot | None:
        return self.shots[-1] if self.shots else self.enter

    @property
    def lying(self) -> bool:
        return self.last is not None and self.last.tag in LYING

    def start(self, x, y, anywhere=False, room="day"):
        """Where the scene begins. Afresh: she is simply there. After another scene: she gets up and
        walks there from where she is (or, ``anywhere``, stays put if she's on the floor in view), and
        the room changes by fading: lights come up before she wakes; the bowl changes once she's there."""
        self.origin = (x, y)
        if not self.entering:
            self.cat, self.room = [x, y], room
            return
        if self.room == "night" and room != "night":
            self.fade_to(room)
        self.get_up()
        if not (anywhere and on_screen(self.cat)):
            self.go(x, y)
        if self.room != room:
            self.fade_to(room)

    def get_up(self):
        last = self.last
        if last is None:
            return
        if last.tag in LYING:
            self.overlays = [o for o in self.overlays if o[0] != "zzz"]
            self.play("yawn_lie")                       # wakes before she gets up
        elif last.tag == "on_hind_legs" and last.index > 0:
            self.play("on_hind_legs", frames=list(range(last.index - 1, -1, -1)))   # back down on all fours

    def go(self, x, y):
        """Walk there; a pixel or two is a shuffle during her rest pose, not a one-frame walk."""
        dx, dy = x - self.cat[0], y - self.cat[1]
        if max(abs(dx), abs(dy)) <= 2:
            last = self.last
            tag, i = (last.tag, last.index) if last is not None and not last.tag.startswith("walk") else ("meow_sit", 0)
            while abs(x - self.cat[0]) > 0.25 or abs(y - self.cat[1]) > 0.25:
                self.cat = [self.cat[0] + max(-1, min(1, x - self.cat[0])), self.cat[1] + max(-1, min(1, y - self.cat[1]))]
                self.shot(tag, i, 120)
            self.cat = [x, y]
        else:
            self.walk_to(x, y)

    def settle_in_bed(self):
        """Into bed and lying down (the scenes that start asleep), unless she's already there."""
        if self.lying and math.hypot(self.cat[0] - BED[0], self.cat[1] - BED[1]) < 0.5:
            return
        self.get_up()
        self.go(*BED)
        self.play("sleep2_l")

    def fade_to(self, room):
        """The room fades to ``room`` over ``FADE_MS`` (a step every 60 ms at most, so it's smooth) while
        she keeps her pose: a resting pose carries on breathing; anything else holds its last frame
        (never walking on the spot, never bobbing up and down)."""
        if room == self.room:
            return
        last = self.last
        tag, i = (last.tag, last.index) if last is not None else ("meow_sit", 0)
        if tag.startswith("walk"):
            tag, i = "meow_sit", 0
        anim = self.art.animations[tag]
        cycles = tag in LOOPING and len(anim) > 1
        from_room, done, k = self.room, 0.0, i
        self.room = room
        while done < FADE_MS:
            left = min(anim[k].ms if cycles else FADE_MS, FADE_MS - done)
            while left > 0:
                ms = min(60, left)
                left -= ms
                done += ms
                self.fade = (from_room, done / FADE_MS) if done < FADE_MS else None
                self.shot(tag, k, ms)
            if cycles:
                k = (k + 1) % len(anim)
        self.fade = None

    def shot(self, tag, i, ms):
        dark = self.room == "night" and not self.fade
        ball = tuple(self.ball) if self.ball is not None and not dark else None   # fading: drawn, dimmed with the light
        toy = tuple(self.toy) if self.toy is not None and not dark else None
        self.shots.append(Shot(tag, i, tuple(self.cat), ball, self.room, tuple(self.overlays), ms, self._moving, toy,
                               self.fade))

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
        anim = self.art.animations[tag]
        sx, sy = self.cat
        self._moving = 1 if dx > 0.5 else -1 if dx < -0.5 else 0
        sideways = tag == "walk_left_d" or abs(dx) >= abs(dy)   # a steep walk has no stepping art: it glides
        gait = self.art.tuning.get("gait", {}).get(tag) if stride_mult == 1.0 and sideways else None
        if gait:   # the gait drives the main axis exactly (x for a side walk); the other follows in whole pixels
            sx, sy, tx, ty = round(sx), round(sy), round(x), round(y)
            run, done, i = max(abs(tx - sx), abs(ty - sy)), 0, 0
            while done < run:
                i += 1
                done = min(run, done + gait[i % len(gait)])
                self.cat = [sx + round((tx - sx) * done / run), sy + round((ty - sy) * done / run)]
                self.shot(tag, i % len(anim), anim[i % len(anim)].ms / speed)
            last = i % len(anim)
            if tag in ("walk_right", "walk_left") and last in (2, 4, 6):   # never stop mid-hop or mid-pass:
                self.shot(tag, last + 1, anim[last + 1].ms / speed)         # land in place, then hand off
            self.cat = [x, y]
        else:
            steps = max(1, int(round(dist / (STRIDE[tag] * stride_mult))))
            for i in range(1, steps + 1):
                self.cat = [sx + dx * i / steps, sy + dy * i / steps]
                self.shot(tag, i % len(anim), anim[i % len(anim)].ms / speed)
        self._moving = 0


LYING = ("sleep1_l", "sleep2_l", "sleep3_l", "yawn_lie", "wash_lie", "meow_lie")
LOOPING = ("sleep1_l", "sleep2_l", "sleep3_l", "meow_sit", "meow_sit2")   # resting poses that can keep cycling
FADE_MS = 900
BALL_HOME = (58, 28)   # where the ball rests by the wall when nobody's playing (tuning "ball" moves it)
MOUSE_OFF = 72         # the toy mouse skids in from, and is batted off to, just past the right edge


def on_screen(cat) -> bool:
    return 4 <= cat[0] <= W - 4


def idle(s, name):
    s.start(*CENTRE, anywhere=True)
    s.hold("meow_sit", 0, 1500)
    s.play("yawn_sit")
    s.hold("meow_sit", 0, 1200)
    s.play("scratch_r")
    s.hold("meow_sit", 0, 1000)


def eating(s, name):                       # (bed ->) bowl -> eat -> wash
    s.start(*BED, anywhere=True)
    if not s.entering:
        s.hold("meow_sit", 0, 800)
    bx, by = s.art.tuning.get("bowl", (0, 0))  # a smaller animal stands nearer the bowl
    s.go(BOWL_SPOT[0] + bx, BOWL_SPOT[1] + by)
    s.play("eat_right", loops=4)
    s.go(42, 25)
    s.play("wash_sit")


def washing(s, name):
    s.start(40, 26, anywhere=True)
    s.play("wash_sit", loops=3)
    s.hold("meow_sit", 0, 900)


def playing(s, name):                      # off the wall, chase it, bat it off the wall and back, home again
    reach = s.art.tuning.get("reach", 10)  # how far in front of her feet the ball sits when she bats it
    home = s.art.tuning.get("ball", BALL_HOME)
    if s.ball is None:
        s.ball = list(home)

    def roll(to, speed, brake=True):       # the ball rolls towards ``to`` (slowing to a stop there, if braking)
        while abs(s.ball[0] - to) > 0.25:
            step = max(0.3, min(speed, abs(to - s.ball[0]) * 0.25)) if brake else min(speed, abs(to - s.ball[0]))
            s.ball[0] += step if to > s.ball[0] else -step
            s.shot("paw_right", 0, 60)
        s.ball[0] = to

    s.start(s.ball[0] - reach, 28)         # to wherever the ball lies
    s.play("paw_right")
    if s.ball[0] > 40:                     # by the wall: it bounces straight back across the room
        roll(min(home[0], s.ball[0] + 2), 2.0, brake=False)
        roll(28, 2.0)
        s.go(28 - reach, 28)               # she goes after it
        s.play("paw_right")
    roll(46, 2.0)                          # across the room, slowing in the middle
    s.go(46 - reach, 28)                   # she follows it
    s.play("paw_right")
    roll(home[0], 1.8, brake=False)        # hits the wall at speed...
    roll(s.cat[0] + reach, 1.2)            # ...and bounces back, a little slower, to her paws
    s.play("paw_right")
    roll(home[0], 1.2)                     # one last tap: it rolls home to the wall
    s.ball = list(home)
    s.play("on_hind_legs")
    s.hold("on_hind_legs", 2, 600)


def zoomies(s, name):
    """No run cycle in the art, so the real walk cycles played fast with a long stride."""
    s.start(-14, 26)                       # out of the room first...
    s.overlays.append(("dust",))
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


def napping(s, name):                      # yawn, (walk to bed,) settle, curl up
    if s.room == "night":                  # after lights out: it's a daytime nap, so morning comes first
        s.fade_to("day")
    if not (s.lying and math.hypot(s.cat[0] - BED[0], s.cat[1] - BED[1]) < 0.5):
        s.start(46, 26, anywhere=True)
        s.play("yawn_stand")
        s.go(*BED)
    s.play("sleep2_l", loops=4)
    s.overlays = [o for o in s.overlays if o[0] != "zzz"] + [("zzz", (200, 210, 255))]
    s.play("sleep3_l", loops=10)


def waking(s, name):                       # lying in bed -> yawn -> wash -> up
    if s.entering:
        s.origin = BED
        s.settle_in_bed()
        s.fade_to("day")                   # after lights out: morning comes while she still sleeps
    else:
        s.start(*BED)
    if not any(o[0] == "zzz" for o in s.overlays):
        s.overlays.append(("zzz", (200, 210, 255)))
    s.play("sleep1_l", loops=5)
    s.overlays.clear()
    s.play("yawn_lie")
    s.play("wash_lie")
    s.play("meow_lie")
    s.go(30, 26)                           # up and out of bed
    s.hold("meow_sit", 0, 900)


def lights_out(s, name):
    if s.entering:
        s.origin = BED
        s.settle_in_bed()
        s.fade_to("night")                 # lights go out once she's settled
    else:
        s.start(*BED, room="night")
    s.overlays = [("zzz", (120, 140, 210))]
    s.play("sleep3_l", loops=14)


def asking(s, name):
    s.start(40, 26, anywhere=True, room="empty")
    s.overlays.append(("bubble", "!"))
    s.play("meow_sit", loops=5)
    s.hold("meow_sit", 0, 500)
    s.play("meow_sit2", loops=3)


def hungry(s, name):                       # a banner scrolls "MOCHI IS HUNGRY!" while she meows
    s.start(40, 26, anywhere=True, room="empty")
    message = f"{name.upper()} IS HUNGRY!"
    s.overlays.append(("banner", message))
    scroll_ms = (W + len(message) * 4) / 12 * 1000 + 2 * BANNER_SLIDE_MS
    cycle = sum(f.ms for f in s.art.animations["meow_sit"])
    began = s.elapsed
    while s.elapsed - began + cycle <= scroll_ms:   # whole meows, then sits, so the text ends with the scene
        s.play("meow_sit")
    s.hold("meow_sit", 0, scroll_ms - (s.elapsed - began))


def name_tag(s, name):
    again = s.entering and any(o[0] == "tag" for o in s.enter.overlays)
    if again:                              # her own screen, round again: out of the room and back in
        s.origin = (-14, 26)
        s.go(-14, 26)
        s.walk_to(32, 26)
    elif s.entering:
        s.start(32, 26, anywhere=abs(s.cat[0] - 32) <= 8 and abs(s.cat[1] - 26) <= 2)   # over from wherever she is
    else:
        s.cat, s.origin = [-14, 26], (-14, 26)
        s.walk_to(32, 26)
    s.overlays.append(("tag", name.upper()))   # rises as she arrives, whatever the walk
    s.hold("meow_sit", 0, 2500 + TAG_RISE_MS)


def begging(s, name):                      # at the empty bowl, up on her hind legs
    if s.entering:
        s.start(*BEG_SPOT, room="empty")
    else:
        s.start(36, 26, room="empty")
        s.go(*BEG_SPOT)
    for _ in range(2):
        s.play("on_hind_legs")
        s.hold("on_hind_legs", 2, 900)
        s.play("on_hind_legs", frames=[2, 1, 0])
        if _ == 0:
            s.play("meow_stand", loops=3)


def grumpy(s, name):
    s.start(40, 26, anywhere=True)
    s.overlays.append(("grumble",))
    s.play("hiss_r", loops=5)
    s.overlays.clear()
    s.go(*BED)
    s.play("sleep2_l", loops=5)


def scratching(s, name):
    s.start(32, 26, anywhere=True)
    s.play("scratch_r", loops=2)
    s.hold("meow_sit", 0, 700)
    s.play("scratch_r")
    s.hold("meow_sit", 0, 900)


def toy(s, name):
    """Playtime with her toy: a cat stalks a toy mouse, pounces and bats it about; a bunny hops
    over to a carrot and nibbles it down to nothing."""
    if s.art.tuning.get("toy") == "carrot":
        return _carrot(s)
    reach = s.art.tuning.get("reach", 10)
    # she stalks it from whichever side she's on: on the right, it all happens the other way round
    side = -1 if s.entering and on_screen(s.cat) and s.cat[0] > 24 else 1
    X = (lambda x: x) if side == 1 else (lambda x: W - x)
    paw = "paw_right" if side == 1 else "paw_left"
    near = reach if side == 1 else reach + 1

    def slide(to, speed, spin=0, brake=True):   # the mouse skids across the floor (slowing to a stop)
        n = 0
        while abs(s.toy[1] - to) > 0.25:
            step = max(0.3, min(speed, abs(to - s.toy[1]) * 0.25)) if brake else min(speed, abs(to - s.toy[1]))
            s.toy[1] += step if to > s.toy[1] else -step
            if n < spin and n % 2 == 0:      # spinning round as it goes
                s.toy[3] = not s.toy[3]
            n += 1
            s.shot(paw if s.last.tag == paw else "meow_sit", 0, 60)
        s.toy[1] = to

    if side == 1:   # stays put if she's already on the left, else goes round to the far side first
        s.start(10, 28, anywhere=s.entering and on_screen(s.cat) and abs(s.cat[1] - 28) <= 2)
    else:           # on the right: from where she is, or a step over if she's short of her run-up
        s.start(max(s.cat[0], X(24)), 28)
    s.hold("meow_sit", 0, 300)
    s.toy = ["mouse", X(MOUSE_OFF), 28, side == -1, 0]
    slide(X(46), 2.2)                       # the mouse skids in and stops
    s.hold("meow_sit", 0, 900)              # she's spotted it
    if side * (X(24) - s.cat[0]) > 2:
        s.walk_to(X(24), 28, speed=0.5)     # creeps closer...
    elif side * (X(24) - s.cat[0]) > 0.25:
        s.go(X(24), 28)
    s.hold("meow_sit", 0, 500)              # ...waits...
    s.play("on_hind_legs", frames=[0, 1])   # ...gathers herself...
    s.walk_to(X(46) - side * near, 28, speed=3.0, stride_mult=3.0)  # ...and pounces
    s.play(paw)
    slide(X(52), 1.8)
    s.go(X(52) - side * near, 28)
    s.play(paw)
    slide(X(MOUSE_OFF), 1.6, spin=6, brake=False)   # and bats it right out of the room
    s.toy = None
    s.hold("meow_sit", 0, 700)              # very pleased with herself
    s.play("wash_sit")


def _carrot(s):
    nose = 18                               # how far in front of her feet her nose is when she eats
    s.start(*BED, anywhere=True)
    s.hold("meow_sit", 0, 700)
    s.toy = ["carrot_1", 44, 28, True, 0]   # tip towards her
    s.go(44 - nose, 28)
    for bite in (0, 3, 6, 9):               # she eats it from her end, a bit at a time
        s.toy[1], s.toy[4] = 44 - bite, bite  # she pulls it in as she eats, so it stays at her nose
        s.play("eat_right", loops=2)
    s.toy = None                            # all gone
    s.play("wash_sit")
    s.hold("meow_sit", 0, 900)


def bee(s, name):
    """A bee drifts in; she watches it, sits up tall as it buzzes over her head, swats at it as
    it dips past, and it flies off."""
    s.start(30, 28, anywhere=s.entering and abs(s.cat[0] - 30) <= 8 and abs(s.cat[1] - 28) <= 2)
    swat = s.art.tuning.get("swat", "paw_right")
    path = []                               # the bee's flight: in from the left, loops over her, dips, out right
    for i in range(80):
        u = i / 79
        dip = 6 * math.sin(math.pi * (u - 0.5) / 0.25) ** 2 if 0.5 < u < 0.75 else 0
        overhead = s.art.tuning.get("bee_lift", 0) * math.sin(math.pi * min(1, max(0, (i - 28) / 20)))
        path.append((-6 + u * 76, 13 + 3 * math.sin(u * math.pi * 3) + dip - overhead))

    def fly(k0, k1, tag, i=0):
        for k in range(k0, k1):
            s.toy = ["bee", path[k][0], path[k][1], False, 0]
            s.shot(tag, i, 60)

    s.hold("meow_sit", 0, 300)
    fly(0, 30, "meow_sit")                  # watching it come
    s.play("on_hind_legs")                  # up tall as it passes overhead
    fly(30, 46, "on_hind_legs", 2)
    if swat == "paw_right":                 # back down to swat; others watch it go from where they are
        s.play("on_hind_legs", frames=[2, 1, 0])
    anim = s.art.animations[swat]
    order = s.art.tuning.get("swat_frames") or list(range(len(anim)))
    for i, k in zip(order, range(46, 80)):  # swat! (the bee keeps going)
        s.toy = ["bee", path[k][0], path[k][1], False, 0]
        s.shot(swat, i, anim[i].ms)
    fly(46 + len(order), 80, "meow_sit")    # and off it goes
    s.toy = None
    s.hold("meow_sit", 0, 600)
    s.play("wash_sit")


BANNER_SLIDE_MS = 280   # the hungry banner slides in from the top and back out
TAG_RISE_MS = 320       # the name tag rises in and sinks away


MODES = {f.__name__: f for f in (idle, eating, washing, playing, zoomies, napping, waking, lights_out,
                                  asking, hungry, name_tag, begging, grumpy, scratching, toy, bee)}


def record(art: Art, mode: str, name: str, enter: Shot | None = None) -> list[Shot]:
    """One scene's shots. ``enter``: the last shot of the scene before, so this one carries on from
    where she is (the room, the ball and any Zs carry over); None: it starts afresh."""
    return script_for(art, mode, name, enter).shots


def script_for(art: Art, mode: str, name: str, enter: Shot | None = None) -> Script:
    script = Script(art)
    script.ball = list(art.tuning.get("ball", BALL_HOME))
    if enter is not None:
        script.enter, script.room, script.cat = enter, enter.room, list(enter.cat)
        if enter.ball is not None:
            script.ball = list(enter.ball)
        if enter.tag in LYING:   # still asleep: her Zs carry on until she wakes
            script.overlays = [o for o in enter.overlays if o[0] == "zzz"]
    MODES[mode](script, name)
    return script


def link(art: Art, end: Shot, head: Shot) -> list[Shot]:
    """From the loop's last shot back to its first shot, so the loop closes without a jump."""
    s = Script(art, room=end.room, cat=list(end.cat), ball=list(end.ball) if end.ball else
               list(art.tuning.get("ball", BALL_HOME)))
    s.enter = end
    if end.tag in LYING:
        s.overlays = [o for o in end.overlays if o[0] == "zzz"]
    room = head.room
    x, y = head.cat
    if head.tag in LYING:
        if s.room == "night" and room != "night":
            s.fade_to(room)
        s.settle_in_bed()
        if s.room != room:
            s.fade_to(room)
        s.overlays = []
    else:
        s.start(x, y, room=room)
    return s.shots


def record_loop(art: Art, modes: list[str], name: str) -> list[tuple[Shot, str]]:
    """The modes one after another as one seamless loop: each scene carries on from where the one
    before left her, and the first carries on from where the last leaves her, so the loop closes on
    itself with nothing in between. (Scenes end where they end whatever came before, so recording the
    loop again from its own end settles in a step or two; if it ever didn't, a ``link`` closes it.)
    Each shot is labelled with its mode."""
    def chain(enter):
        out: list[tuple[Shot, str]] = []
        for mode in modes:
            script = script_for(art, mode, name, out[-1][0] if out else enter)
            out += [(shot, mode) for shot in script.shots]
        return out
    out = chain(None)
    if not out:
        return out
    for _ in range(4):
        again = chain(out[-1][0])
        if again and again[-1][0] == out[-1][0]:   # ends exactly where it began from: closed
            return again
        out = again
    two = []   # a loop that alternates (her toy from one side, then the other) closes after two times round
    for mode in modes + modes:
        two += [(shot, mode) for shot in script_for(art, mode, name, two[-1][0] if two else out[-1][0]).shots]
    if two and two[-1][0] == out[-1][0]:
        return two
    return out + [(shot, "link") for shot in link(art, out[-1][0], out[0][0])]
