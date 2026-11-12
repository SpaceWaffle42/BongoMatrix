"""Countdown looks of the Calendar (the Countdown module until 12 Nov 2026): days, clock, flip,
ring and tear-off, counting down to the soonest of your dates (or, with none, the next of your
ticked holidays)."""
from bongo import icons
from bongo.holidays import RULES, choice_groups, choice_labels

from . import events
from .clock import Clock
from .days import Days
from .flip import Flip
from .ring import Ring
from .tearoff import Tearoff

# settings only the countdown looks have (the rest are the Calendar's)
DEFAULTS = dict(done_message="It's here!", idle_text="", text_colour="#FFF4DE",
                background_top="#2A0A06", background_bottom="#0E0201", progress_bar=True, icons=True)
STYLES = {"countdown_days": Days, "countdown_clock": Clock, "countdown_flip": Flip,
          "countdown_ring": Ring, "countdown_tearoff": Tearoff}


def grouped(cls):
    """The look as a Calendar style: filed under Countdown, with the countdown settings."""
    return type(cls.__name__, (cls,), {"group": "Countdown", "defaults": {**DEFAULTS, **cls.defaults},
                                       "basic": ("done_message",) + tuple(cls.basic), "__doc__": cls.__doc__})


EVENT_FIELDS = [
    {"name": "title", "type": "text", "label": "What", "placeholder": "Mum's birthday"},
    {"name": "date", "type": "date", "label": "Date", "hidden_when": "holiday"},
    {"name": "holiday", "type": "choice", "label": "Or a holiday (every year)",
     "options": [["", "None"]] + [[k, choice_labels()[k], choice_groups()[k]] for k in RULES]},
    {"name": "repeat", "type": "choice", "label": "Repeats", "hidden_when": "holiday",  # a holiday is yearly anyway
     "options": [["once", "Just once"], ["yearly", "Every year"]]},
    {"name": "time", "type": "time", "label": "At (blank = all day)", "more": True},
    {"name": "until", "type": "time", "label": "Ends at (else an hour later)", "more": True},
    {"name": "message", "type": "text", "label": "Message when it arrives", "placeholder": "Happy birthday!",
     "more": True},
    {"name": "icon", "type": "choice", "label": "Icon", "more": True,
     "options": [["", "Automatic (the holiday's)"], ["none", "No icon"]]
                + [[n, icons.label(n), "For events" if n in icons.EVENT_ICONS else "Holidays"]
                   for n in sorted(icons.ICONS, key=lambda n: (n not in icons.EVENT_ICONS, icons.label(n)))]},
    {"name": "colour", "type": "colour", "label": "Own colour", "more": True},
    {"name": "from", "type": "date", "label": "Progress bar from (else 30 days before)", "more": True},
]
