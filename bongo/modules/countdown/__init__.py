"""Countdown: days (or HH:MM) until an event, or a ticking clock, over a dark gradient."""
from bongo.module import Module

from .clock import Clock
from .days import Days
from .flip import Flip
from .ring import Ring
from .tearoff import Tearoff

MODULE = Module(
    id="countdown",
    name="Countdown",
    description="Counts down to an event. Days shows a big day count; Clock ticks DD:HH:MM:SS.",
    styles={"days": Days, "clock": Clock, "flip": Flip, "ring": Ring, "tearoff": Tearoff},
    defaults=dict(
        label="Christmas", target="next:christmas", done_message="It's here!",
        accent="#FFB547", text_colour="#FFF4DE",
        background_top="#2A0A06", background_bottom="#0E0201",
        progress_bar=True, start=None,
    ),
)
