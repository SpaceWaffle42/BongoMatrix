"""Countdown: days (or HH:MM) until the next of your events, over a dark gradient."""
from bongo.holidays import RULES, choice_groups, choice_labels
from bongo.module import Module

from . import events
from .clock import Clock
from .days import Days
from .flip import Flip
from .ring import Ring
from .tearoff import Tearoff

MODULE = Module(
    id="countdown",
    name="Countdown",
    description="Counts down to the next of your events (birthdays, holidays, opening day...). "
                "Days shows a big day count; Clock ticks DD:HH:MM:SS.",
    styles={"days": Days, "clock": Clock, "flip": Flip, "ring": Ring, "tearoff": Tearoff},
    defaults=dict(
        events=[{"title": "Christmas", "holiday": "christmas"}],
        done_message="It's here!", idle_text="",
        accent="#FFB547", text_colour="#FFF4DE",
        background_top="#2A0A06", background_bottom="#0E0201",
        progress_bar=True,
    ),
    basic=("events", "done_message"),
    check=events.check,
    migrate=events.from_old,  # one label + target (before 11 Nov 2026) -> the events list
    item_fields={"events": [
        {"name": "title", "type": "text", "label": "What", "placeholder": "Mum's birthday"},
        {"name": "date", "type": "date", "label": "Date"},
        {"name": "holiday", "type": "choice", "label": "Or a holiday (every year)",
         "options": [["", "None"]] + [[k, choice_labels()[k], choice_groups()[k]] for k in RULES]},
        {"name": "repeat", "type": "choice", "label": "Repeats",
         "options": [["once", "Just once"], ["yearly", "Every year"]]},
        {"name": "time", "type": "time", "label": "At (blank = all day)", "more": True},
        {"name": "until", "type": "time", "label": "Ends at (else an hour later)", "more": True},
        {"name": "message", "type": "text", "label": "Message when it arrives", "placeholder": "Happy birthday!",
         "more": True},
        {"name": "colour", "type": "colour", "label": "Own colour", "more": True},
        {"name": "from", "type": "date", "label": "Progress bar from (else 30 days before)", "more": True},
    ]},
)
