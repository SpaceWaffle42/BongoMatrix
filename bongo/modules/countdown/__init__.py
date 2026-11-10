"""Countdown: days (or HH:MM) until an event, or a ticking clock, over a dark gradient."""
from bongo.module import Module

from .clock import Clock
from .days import Days

MODULE = Module(
    id="countdown",
    name="Countdown",
    description="Counts down to an event. Days shows a big day count; Clock ticks DD:HH:MM:SS.",
    styles={"days": Days, "clock": Clock},
    defaults=dict(
        label="Christmas", target="2026-12-25T00:00", done_message="It's here!",
        accent="#FFB547", text_colour="#FFF4DE",
        background_top="#2A0A06", background_bottom="#0E0201",
        progress_bar=True, start="2026-11-01T00:00",
    ),
)
