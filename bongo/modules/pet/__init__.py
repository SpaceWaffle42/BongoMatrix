"""Pet: the Matrix's pet (Mochi the cat by default; ten animals of our own) living in its own home (a living room, a burrow, a park, a cage, a terrarium, a lagoon,
a cave, a garden plot), drawn smaller further back up the floor.

Every mode from the remastered Cat 10 engine is a style, Life plays them through the day,
each in the house or one of eight other places (places.py: a garden, a beach, the Moon...),
and the Wander overlay lets it stroll across other screens. Which pet and its name are one
Matrix-wide setting (bongo/pets.py, Settings > Pet), so every screen shows the same pet.
"""
from bongo.module import Module

from .places import LABELS
from .scenes import MODES
from .styles import Life, Wander, mode_style


def migrate(settings: dict) -> list[str]:
    """The bee (Last Tick's art) is gone since 24 Nov 2026: a saved list of Life's moments that had it keeps the rest."""
    if isinstance(settings.get("modes"), list) and "bee" in settings["modes"]:
        settings["modes"] = [m for m in settings["modes"] if m != "bee"]
        return ["pet: the bee is gone from Life's moments"]
    return []


MODULE = Module(
    id="pet",
    name="Pet",
    description="Your pet at home: eating, napping, playing, zoomies and more, in its own home or one of eight "
                "other places (a garden, a beach, the Moon...), or in a bigger room it walks about in while "
                "the view follows it (a living room, a big garden, a playroom, an oasis). Life strings them "
                "through the day; the Wander overlay walks it across other screens. Pick the pet and its name "
                "in Settings > Pet.",
    styles={"life": Life, **{mode: mode_style(mode) for mode in MODES}},
    overlays={"wander": Wander},
    choice_names={"place": {"labels": LABELS}},
    renames={"pet": {"bee": "idle"}},   # a saved Bee screen (gone with Last Tick's art) is a quiet moment
    migrate=migrate,
)
