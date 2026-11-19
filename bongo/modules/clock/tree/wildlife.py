"""Who comes to the tree, and when: a robin on its branch by day; butterflies on warm, still spring and summer
days (five kinds, each in its own weeks, on their own, chasing each other in pairs, settling on a flower, or a
few dancing up together); fireflies on summer nights. The visits are worked out from the date and the time, so
a restart (or another Matrix) shows the same ones."""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from .year import unit

ROBIN_EVERY, FLY_EVERY, GLOW_EVERY = 180, 150, 600                 # seconds between each one's chances to turn up
ROBIN_CHANCE = {"winter": 0.6, "autumn": 0.45, "spring": 0.35, "summer": 0.15}
# each kind of butterfly and the weeks it flies (season time: 0 is the first day of spring)
BUTTERFLIES = {"brimstone": ((0.03, 0.30),), "orange_tip": ((0.12, 0.32),), "common_blue": ((0.20, 0.50),),
               "peacock": ((0.08, 0.20), (0.38, 0.56)), "admiral": ((0.30, 0.58),)}
FLIGHTS = (("flutter", 0.35), ("chase", 0.30), ("settle", 0.22), ("dance", 0.13))   # what they do, and how often
LENGTH = {"flutter": (14, 20), "chase": (16, 22), "settle": (16, 22), "dance": (12, 16)}


@dataclass(frozen=True)
class Visit:
    who: str
    start: float      # seconds since midnight
    length: float
    flip: bool = False
    kinds: tuple = ()          # butterflies: which kind each one is
    flight: str = ""           # butterflies: flutter, chase, settle or dance
    seed: int = 0

    def at(self, s: float) -> float | None:
        """How far into the visit ``s`` is (0..1), or None: not now."""
        k = (s - self.start) / self.length
        return k if 0 <= k < 1 else None


def flying(f: float) -> list:
    """The kinds of butterfly about at season time ``f``."""
    return [k for k, weeks in BUTTERFLIES.items() if any(a <= f < b for a, b in weeks)]


def _slot(who: str, day: dt.date, s: float, every: int, chance: float, length, offset=(5, 40), make=None) -> Visit | None:
    k = int(s // every)
    for slot in (k, k - 1):                                  # a visit can run on into the next slot
        if slot < 0 or unit(who, day, slot, "turn up") >= chance:
            continue
        extra = make(day, slot) if make else {}
        size = extra.pop("length", length)
        start = slot * every + offset[0] + (offset[1] - offset[0]) * unit(who, day, slot, "when")
        v = Visit(who, start, size[0] + (size[1] - size[0]) * unit(who, day, slot, "how long"),
                  unit(who, day, slot, "way") < 0.5, seed=slot, **extra)
        if v.at(s) is not None:
            return v
    return None


def butterflies(f: float):
    """What a butterfly visit is (kinds and flight), for a slot, at season time ``f``."""
    about = flying(f)

    def make(day, slot):
        r = unit("flight", day, slot)
        for flight, share in FLIGHTS:
            if r < share:
                break
            r -= share
        pick = lambda n: about[int(unit("kind", day, slot, n) * len(about))]   # noqa: E731
        first = pick(0)
        if flight == "chase":   # mostly two of a kind; now and then two kinds
            kinds = (first, first if unit("same", day, slot) < 0.7 else pick(1))
        elif flight == "dance":
            kinds = (first, first, pick(2))
        else:
            kinds = (first,)
        return {"kinds": kinds, "flight": flight, "length": LENGTH[flight]}
    return make


def visitors(now: dt.datetime, season: str, f: float, night: bool, dusky: bool, weather) -> list:
    """The visits going on at ``now`` (each at most once)."""
    day, s = now.date(), now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6
    out = []
    if not night and weather.kind != "storm":
        robin = _slot("robin", day, s, ROBIN_EVERY, ROBIN_CHANCE[season], (45, 120))
        if robin:
            out.append(robin)
    still = not weather.wet and weather.kind not in ("snow", "fog") and weather.wind == 0
    if still and not night and not dusky and flying(f):
        fly = _slot("butterfly", day, s, FLY_EVERY, 0.5, (14, 20), (5, 120), butterflies(f))
        if fly:
            out.append(fly)
    if night and 0.22 <= f <= 0.48 and not weather.wet:   # summer nights, now and then, a few minutes at a time
        glow = _slot("fireflies", day, s, GLOW_EVERY, 0.5, (150, 360), (10, 200))
        if glow:
            out.append(glow)
    return out


def flap(t: float, phase: float = 0.0, slow: bool = False) -> int:
    """Which wing picture to show (0 open, 1 half up, 2 closed): a quick beat in flight; at rest, the wings
    open and close slowly now and then."""
    if slow:                                              # resting: wings half up, opened now and then, shut for a moment
        c = (t + phase) % 3.0
        return 1 if c < 1.9 else 0 if c < 2.6 else 2
    return (0, 1, 2, 1)[int((t + phase) * 14) % 4]


def butterfly_spots(visit: Visit, k: float, t: float, width: int = 64) -> list:
    """[(x, y, picture, kind)] for each butterfly of a visit ``k`` of the way through it, ``t`` seconds in. They
    keep to the open space under the crown (rows 20 to 28), well below the time."""
    side = -1 if visit.flip else 1
    def across(k):                                        # from one side to the other
        x = -6 + k * (width + 12)
        return width - x if visit.flip else x
    out = []
    if visit.flight == "flutter":
        x, y = across(k), 24 + 2.5 * math.sin(k * 19) + 0.6 * math.sin(t * 9)
        out.append((x, y, flap(t), visit.kinds[0]))
    elif visit.flight == "chase":                          # one darts about, the other chases it, cutting its corners
        ph = [unit("chase", visit.seed, n) * 6.3 for n in range(6)]
        def lead(u):                                      # never turns back: the wobble is slower than the crossing
            x = across(u / visit.length) + side * 1.1 * math.sin(2.3 * u + ph[0])
            y = 24 + 1.6 * math.sin(1.7 * u + ph[1]) + 1.0 * math.sin(3.1 * u + ph[2]) + 0.6 * math.sin(5.3 * u + ph[3])
            return x, y
        u = k * visit.length
        speed = (width + 12) / visit.length
        gap = max(2.1 + 0.5 * math.sin(0.9 * u + ph[4]) + 0.2 * math.sin(2.3 * u), 8 / speed)   # darts in, drops back (never closer than 8 px of crossing)
        x, y = lead(u)
        out.append((x, y + 0.5 * math.sin(9 * t + ph[4]), flap(t), visit.kinds[0]))
        at = u - gap                                      # the chaser smooths the leader's turns and bobs on its own
        x, y = (sum(c) / 3 for c in zip(lead(at - 0.35), lead(at), lead(at + 0.35)))
        out.append((x, y + 0.5 * math.sin(8.5 * t + ph[5]), flap(t, 0.14), visit.kinds[1]))
    elif visit.flight == "settle":                         # in to a flower, a rest with slow wings, then away
        spot = 14 + unit("spot", visit.seed) * 12
        spot = spot if side > 0 else width - spot
        if k < 0.3:
            q = k / 0.3
            x, y = across(0) + (spot - across(0)) * q, 23 + (27 - 23) * q + 2 * math.sin(q * 9)
            out.append((x, y, flap(t), visit.kinds[0]))
        elif k < 0.75:
            out.append((spot, 27, flap(t, slow=True), visit.kinds[0]))
        else:
            q = (k - 0.75) / 0.25
            x, y = spot + (across(1) - spot) * q, 27 - 5 * math.sin(q * math.pi / 2) + math.sin(q * 11)
            out.append((x, y, flap(t), visit.kinds[0]))
    elif visit.flight == "dance":                          # three crossing loosely together, rising and falling past each other
        ph = [unit("dance", visit.seed, n) * 6.3 for n in range(6)]
        u = k * visit.length
        slots = (-9, 0, 7)
        for n, kind in enumerate(visit.kinds):
            x = across(k) + side * (slots[n] + 0.9 * math.sin(0.8 * u + ph[n]))
            y = 24 + 2.4 * math.sin(1.4 * u + ph[n + 3] + n * 2.1) + 0.5 * math.sin(9 * t + n * 1.7)
            out.append((x, y, flap(t, (0.07, 0.14, 0.21)[n]), kind))
    low = 28 if visit.flight == "settle" else 27          # only a settling one comes down to the flowers
    return [(round(x), max(20, min(low, round(y))), fr, kind) for x, y, fr, kind in out]


def firefly_spots(t: float, count: int = 6):
    """(x, y, phase) of each firefly: drifting slowly about below the crown, each blinking in its own time."""
    for i in range(count):
        x = 10 + unit("ff", i, 0) * 44 + 2.5 * math.sin(t * 0.25 + i * 1.7)
        y = 18 + unit("ff", i, 1) * 10 + 1.5 * math.cos(t * 0.2 + i)
        yield round(x), round(y), unit("ff", i, 2) * 4
