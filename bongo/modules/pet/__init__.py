"""Pet: Mochi the cat (your Cat 10 art), living in her room on the panel.

Every mode from the remastered Cat 10 engine is a style, Life plays them through the day,
and the Wander overlay lets her stroll across other screens.
"""
from bongo.module import Module

from .scenes import MODES
from .styles import Life, Wander, mode_style

MODULE = Module(
    id="pet",
    name="Pet",
    description="Your cat in her room: eating, napping, playing, zoomies and more. Life strings them "
                "through the day; the Wander overlay walks her across other screens.",
    styles={"life": Life, **{mode: mode_style(mode) for mode in MODES}},
    overlays={"wander": Wander},
    defaults={"name": "Mochi"},
    basic=("name",),
)
