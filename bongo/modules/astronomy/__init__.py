"""Moon phase and sun arc from compact analytic formulas (no ephemeris files)."""
from bongo.module import Module

from .moon import Moon
from .sun import Sun

MODULE = Module(
    id="astronomy",
    name="Sky",
    description="Moon phase with the next full or new moon, or today's sun arc. Computed offline.",
    styles={"moon": Moon, "sun": Sun},
    defaults=dict(location="London", latitude=51.5074, longitude=-0.1278,
                  own_location=False,  # False: the Matrix's location from Settings; True: this screen's own
                  accent="#FFB547", text_colour="#FFF4DE"),
    basic=("location", "own_location"),
    limits={"latitude": (-90, 90), "longitude": (-180, 180)},
    places={"location": ("latitude", "longitude")},  # pick a real place; coordinates follow
)
