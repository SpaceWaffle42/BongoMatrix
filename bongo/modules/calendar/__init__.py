"""Calendar: today's page with what's coming up, the month at a glance, and how far through
the year, month, week and day we are. Holidays come from ``bongo.holidays``; your own dates
are the ``events`` setting."""
from bongo.holidays import RULES, choice_groups, choice_labels
from bongo.module import Module

from .styles import Month, Page, Progress

MODULE = Module(
    id="calendar",
    name="Calendar",
    description="Page: today plus the next dates coming up. Month: the month as a grid of lit days. "
                "Progress: year, month, week and day as bars.",
    styles={"page": Page, "month": Month, "progress": Progress},
    defaults=dict(
        holidays=["christmas", "new_year", "valentines", "easter", "halloween", "bonfire_night"],
        events=[],  # [{"date": "MM-DD" or "YYYY-MM-DD" or a holiday spec, "title": "Mum's birthday"}]
        week_start="monday",
        text_colour="#ECEEF1", accent="#FFB547", event_colour="#5AC8FA", background="#0B0C0E",
    ),
    choices={"holidays": list(RULES), "week_start": ["monday", "sunday"]},
    basic=("holidays", "events", "week_start"),
    choice_names={"holidays": {"labels": choice_labels(), "groups": choice_groups()}},
    item_fields={"events": [
        {"name": "title", "type": "text", "label": "What", "placeholder": "Mum's birthday"},
        {"name": "date", "type": "text", "label": "When", "placeholder": "11-21 every year, or 2026-11-21 once"},
    ]},
)
