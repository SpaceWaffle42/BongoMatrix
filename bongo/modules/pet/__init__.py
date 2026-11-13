"""Pet: the Matrix's pet (Mochi the cat by default, your Cat 10 art) living in her room.

Every mode from the remastered Cat 10 engine is a style, Life plays them through the day,
each in the house or one of eight other places (places.py: a garden, a beach, the Moon...),
and the Wander overlay lets her stroll across other screens. Which pet and its name are one
Matrix-wide setting (bongo/pets.py, Settings > Pet), so every screen shows the same pet.
"""
from bongo.module import Module

from .places import LABELS
from .scenes import MODES
from .styles import Life, Wander, mode_style

MODULE = Module(
    id="pet",
    name="Pet",
    description="Your pet at home: eating, napping, playing, zoomies and more, in the house or one of eight "
                "other places (a garden, a beach, the Moon...). Life strings them through the day; the Wander "
                "overlay walks her across other screens. Pick the pet and her name in Settings > Pet.",
    styles={"life": Life, **{mode: mode_style(mode) for mode in MODES}},
    overlays={"wander": Wander},
    choice_names={"place": {"labels": LABELS}},
)
