"""Clock: the time, in many looks, grouped in the dashboard by kind.

Lava (the old Lava Lamp module, moved in on 12 Nov 2026: saved Lava Lamp entries become Clock
entries with the same look), Digital, Glass tube, Word, Analogue, Binary, and Pet (the time,
with your pet turning up now and then).
"""
from bongo.module import Module

from .analogue import Dial, Minimal
from .binary import Binary
from .digital import Big, Flip, Seven
from .lava import LavaClassic, LavaMetro, check as lava_check, migrate as lava_migrate
from .petclock import PetClock
from .tube import Nixie, NixieSeconds
from .words import WordGrid, WordLines


def check(settings: dict) -> None:
    if "custom_palettes" in settings:  # only the Lava styles have palettes
        lava_check(settings)


MODULE = Module(
    id="clock",
    name="Clock",
    description="The time, in many looks: lava, digital, nixie tubes, words, a dial, binary, or with your pet.",
    styles={
        "lava_classic": LavaClassic, "lava_metro": LavaMetro,
        "big": Big, "flip": Flip, "seven": Seven,
        "nixie": Nixie, "nixie_seconds": NixieSeconds,
        "word_grid": WordGrid, "word_lines": WordLines,
        "dial": Dial, "minimal": Minimal,
        "binary": Binary,
        "pet": PetClock,
    },
    check=check,
    migrate=lava_migrate,
    # Lava Lamp entries saved before 12 Nov 2026 are Clock entries now (config.tidy)
    renames={"lava_lamp": {"classic": "lava_classic", "metro": "lava_metro", None: "lava_classic"}},
)
