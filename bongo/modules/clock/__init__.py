"""Clock: the time, in many looks, grouped in the dashboard by kind.

Lava (the old Lava Lamp module, moved in on 12 Nov 2026: saved Lava Lamp entries become Clock
entries with the same look), Digital, Glass tube, Word, Analogue, Binary, Pet (the time,
with your pet turning up now and then), and Seasonal tree (an apple tree or an oak through the year).
"""
from bongo.module import Module

from .analogue import Dial, Minimal
from .binary import Binary
from .digital import Big, Flip, Seven
from .lava import LavaClassic, LavaMetro, check as lava_check, migrate as lava_migrate
from .petclock import PetClock
from .tree import SeasonalTree
from .tube import Nixie, NixieSeconds
from .words import WordGrid, WordLines


def check(settings: dict) -> None:
    if "custom_palettes" in settings:  # only the Lava styles have palettes
        lava_check(settings)


MODULE = Module(
    id="clock",
    name="Clock",
    description="The time, in many looks: lava, digital, nixie tubes, words, a dial, binary, with your pet, or beside a tree through the seasons.",
    styles={
        "lava_classic": LavaClassic, "lava_metro": LavaMetro,
        "big": Big, "flip": Flip, "seven": Seven,
        "nixie": Nixie, "nixie_seconds": NixieSeconds,
        "word_grid": WordGrid, "word_lines": WordLines,
        "dial": Dial, "minimal": Minimal,
        "binary": Binary,
        "pet": PetClock,
        "tree": SeasonalTree,
    },
    check=check,
    page_links={"pet": "pet"},   # the Pet style shows the pet, whose page is the Pet module's
    migrate=lava_migrate,
    choice_names={"tree": {"labels": {"apple": "Apple tree", "oak": "Oak"}},
                  "hemisphere": {"labels": {"auto": "From the Matrix's location", "north": "Northern", "south": "Southern"}},
                  "time_place": {"labels": {"sides": "Either side of the tree", "corners": "Small, in the top corners"}}},
    marks={"transition_seconds": [[0, "Cut"], [2.5, "Default"]]},   # (Lava: dragging near one snaps to it)
    notes={"transition_seconds": "When a Lava screen plays back to back, how long its colours take to glide into "
                                 "the next one's. 0: a straight cut."},
    # Lava Lamp entries saved before 12 Nov 2026 are Clock entries now (config.tidy)
    renames={"lava_lamp": {"classic": "lava_classic", "metro": "lava_metro", None: "lava_classic"}},
)
