"""The tree's year: where a day falls in its seasons, and what each leaf, blossom and apple looks like then.

Season time ``f`` runs from 0 to 1 starting on the first day of spring: 1 March in the Northern hemisphere,
1 September in the Southern. Every leaf has its own days to open, turn and fall (fixed for that leaf, from
its place on the drawing), so the tree moves on a little every day and looks the same all day.

Summer is the colours the Leaves layer was drawn in. Its greens are ranked lightest to darkest into four
tones, and every other season is drawn in that season's four tones, so the drawing's light and shade carry
through the whole year.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field

SPRING_STARTS = {"north": (3, 1), "south": (9, 1)}


def season_time(day: dt.date, hemisphere: str) -> float:
    """0 on the first day of spring, 0.25 summer, 0.5 autumn, 0.75 winter, back to 0 a year later."""
    month, first = SPRING_STARTS.get(hemisphere, SPRING_STARTS["north"])
    start = dt.date(day.year, month, first)
    if day < start:
        start = dt.date(day.year - 1, month, first)
    return ((day - start).days % 365) / 365


def season_name(f: float) -> str:
    return ("spring", "summer", "autumn", "winter")[min(3, int(f * 4))]


def unit(*parts) -> float:
    """A repeatable number in [0, 1) from ``parts`` (the same on every Matrix, after every restart)."""
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big") / 2 ** 64


def mix(a, b, k: float):
    k = 0.0 if k < 0 else 1.0 if k > 1 else k
    return tuple(round(a[i] + (b[i] - a[i]) * k) for i in range(3))


def dim(c, k: float):
    return tuple(max(0, min(255, round(v * k))) for v in c)


def hexc(h: str):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def ramp(*colours) -> tuple:
    return tuple(hexc(c) for c in colours)   # tone 1 (lit) to tone 4 (in shadow)


@dataclass(frozen=True)
class Species:
    """When this kind of tree does what. All times are season time (see ``season_time``)."""
    buds: float              # the first buds break
    leafed: float            # the last leaf is out
    turns: tuple             # leaves start turning between these two
    falls: tuple             # and are gone between these two (each leaf a few weeks after it turned)
    bare: float              # nothing left but the leaves it keeps
    autumn: tuple            # the colours a leaf goes through, each a ramp of four tones
    keeps: float = 0.0       # the share of leaves that hang on, brown, through the winter (an oak's)
    blossom: tuple | None = None    # (opens, gone): each flower somewhere between
    fruit: tuple | None = None      # (set, ripe, gone)


BUD = ramp("#d8ee8c", "#bcdc6c", "#9cc458", "#7aa446")
FRESH = ramp("#b4e86a", "#86d04e", "#5cb03e", "#3c8a32")

SPECIES = {
    "apple": Species(
        buds=0.04, leafed=0.30, turns=(0.55, 0.66), falls=(0.61, 0.77), bare=0.78,
        autumn=(ramp("#f0d24c", "#d8b034", "#b88c26", "#8a661c"), ramp("#f6a83c", "#e0842c", "#b8641e", "#8a4a18"),
                ramp("#ec6a3c", "#cc4a2c", "#9e3422", "#72261a"), ramp("#bc8c5a", "#9a6a40", "#785030", "#543a22")),
        blossom=(0.07, 0.18), fruit=(0.28, 0.45, 0.66)),
    "oak": Species(
        buds=0.10, leafed=0.31, turns=(0.55, 0.68), falls=(0.66, 0.83), bare=0.84, keeps=0.06,
        autumn=(ramp("#c8c454", "#a8a43c", "#88842e", "#64621f"), ramp("#d6a654", "#b4843a", "#8e642a", "#68461e"),
                ramp("#c87a3e", "#a45c2c", "#80441f", "#5c3016"), ramp("#a87c52", "#86623e", "#664a2e", "#48341f"))),
}
TREES = tuple(SPECIES)


def tones(colours) -> dict:
    """{colour: tone 1..4} for the colours a Leaves layer was drawn in: lightest first, in up to four bands."""
    ranked = sorted(set(colours), key=lambda c: -(0.3 * c[0] + 0.59 * c[1] + 0.11 * c[2]))
    n = len(ranked)
    return {c: 1 + (k * 4 // n if n > 1 else 1) for k, c in enumerate(ranked)}


@dataclass
class Leaf:
    x: int
    y: int
    summer: tuple          # its colour in the drawing
    tone: int              # 1 (lit) .. 4 (in shadow)
    opens: float = 0.0     # season times
    turns: float = 0.0
    falls: float = 0.0
    last: int = 0          # how far through the autumn colours it goes (0 yellow .. 3 brown)
    keeps: bool = False    # hangs on through the winter


@dataclass
class Tree:
    """A tree drawing made ready for the year: its leaves, blossom and fruit, each with its own days."""
    kind: str
    leaves: list = field(default_factory=list)
    blossom: list = field(default_factory=list)   # (x, y, colour, opens, gone)
    fruit: list = field(default_factory=list)     # (x, y, colour, falls)


def prepare(kind: str, leaves: dict, blossom: dict, fruit: dict, centre: float = 31.5) -> Tree:
    """``leaves``/``blossom``/``fruit``: {(x, y): colour} from the drawing's layers."""
    sp = SPECIES.get(kind, SPECIES["apple"])
    tone_of = tones(leaves.values())
    tree = Tree(kind)
    if leaves:
        top = min(y for _, y in leaves)
        reach = max(abs(x - centre) for x, _ in leaves) or 1
    for (x, y), c in sorted(leaves.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        u = [unit(kind, x, y, k) for k in range(5)]
        outer = min(1.0, abs(x - centre) / reach * 0.6 + (y - top) / 24 * 0.4)
        order = 0.65 * u[0] + 0.35 * (1 - outer)       # the top and the middle come out first, the low outer twigs last
        opens = sp.buds + (sp.leafed - sp.buds) * order
        turns = sp.turns[0] + (sp.turns[1] - sp.turns[0]) * (0.6 * u[1] + 0.4 * outer)
        tf = (turns - sp.turns[0]) / (sp.turns[1] - sp.turns[0])      # an early turner falls early too
        falls = max(turns + 0.03, sp.falls[0] + (sp.falls[1] - sp.falls[0]) * (0.55 * tf + 0.45 * u[2]))
        last = min(3, int(u[3] * 4))
        tree.leaves.append(Leaf(x, y, c, tone_of[c], opens, turns, falls, last, u[4] < sp.keeps))
    if sp.blossom:
        a, b = sp.blossom
        for (x, y), c in blossom.items():
            u0, u1 = unit(kind, x, y, "b0"), unit(kind, x, y, "b1")
            opens = a + (b - a) * 0.45 * u0
            tree.blossom.append((x, y, c, opens, opens + (b - a) * (0.45 + 0.4 * u1)))
    if sp.fruit:
        for (x, y), c in fruit.items():
            tree.fruit.append((x, y, c, sp.fruit[2] - 0.05 * unit(kind, x, y, "f")))
    return tree


def leaf_colour(tree: Tree, leaf: Leaf, f: float):
    """This leaf's colour at season time ``f``, or None: not out yet, or fallen."""
    sp = SPECIES.get(tree.kind, SPECIES["apple"])
    t = leaf.tone - 1
    if f < leaf.opens:
        return dim(sp.autumn[3][t], 0.85) if leaf.keeps and f < sp.buds else None   # an oak's dead leaves until the buds break
    if f >= leaf.falls:
        return dim(sp.autumn[3][t], 0.85) if leaf.keeps else None
    age = f - leaf.opens
    if age < 0.025:                                     # a bud for a week or so
        return BUD[t]
    if age < 0.10:                                      # fresh and bright, darkening into the summer green
        return mix(BUD[t], FRESH[t], (age - 0.025) / 0.03) if age < 0.055 else mix(FRESH[t], leaf.summer, (age - 0.055) / 0.045)
    if f < leaf.turns:
        late = max(0.0, (f - (leaf.turns - 0.14)) / 0.14)   # late summer: the green deepens, a little yellow creeps in
        return mix(dim(leaf.summer, 1 - 0.08 * late), sp.autumn[0][t], 0.22 * late * late)
    k = (f - leaf.turns) / max(0.001, leaf.falls - leaf.turns) * (leaf.last + 1)   # through its autumn colours
    if k < 1:
        return mix(dim(leaf.summer, 0.9), sp.autumn[0][t], k)
    i = int(k)
    if i > leaf.last:
        return sp.autumn[leaf.last][t]
    return mix(sp.autumn[i - 1][t], sp.autumn[i][t], k - i)


def blossom_colour(flower, f: float):
    x, y, c, opens, gone = flower
    if not opens <= f < gone:
        return None
    k = (f - opens) / (gone - opens)
    return mix((250, 250, 240), c, 0.6) if k < 0.15 else c          # opening paler, then its own colour


def fruit_colour(tree: Tree, apple, f: float):
    sp = SPECIES.get(tree.kind)
    if not sp or not sp.fruit:
        return None
    x, y, c, falls = apple
    set_, ripe, _ = sp.fruit
    if not set_ <= f < falls:
        return None
    return mix((150, 200, 60), c, (f - set_) / (ripe - set_))         # small and green, reddening as it ripens


def stage(tree: Tree, f: float) -> str:
    """A few words for the dashboard: where the tree is in its year."""
    sp = SPECIES.get(tree.kind, SPECIES["apple"])
    if sp.blossom and sp.blossom[0] <= f < sp.blossom[1]:
        return "In blossom"
    if f < sp.buds:
        return "Bare"
    if f < sp.leafed:
        return "Coming into leaf"
    if f < sp.turns[0]:
        return "In full leaf"
    if f < sp.falls[1]:
        return "Leaves turning and falling"
    return "Bare"
