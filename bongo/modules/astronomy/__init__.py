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
                  accent="#FFB547", text_colour="#FFF4DE"),
    basic=("location", "latitude", "longitude"),
)
