"""Calendar: today, the month, how far through the year, and countdowns to what's next, in four
groups of looks. Holidays come from ``bongo.holidays``; your own dates are the ``events`` setting,
one list shared by every look (the countdowns count down to the soonest).

The Countdown module moved in on 12 Nov 2026: saved Countdown entries become Calendar entries with
the matching countdown look, and old Calendar dates ("11-21", "2026-11-21") take the countdown's
form."""
import logging
import re

from bongo import calendars
from bongo.holidays import RULES, choice_groups, choice_labels
from bongo.module import Module

from . import countdown
from .help import HELP
from .countdown import events
from .styles import DateLine, Desk, Dots, Month, Numbers, Page, Percent, Progress, Rings, Week, Weeks, Year


def group(name, *classes):
    return {cls: type(cls.__name__, (cls,), {"group": name, "__doc__": cls.__doc__}) for cls in classes}


TODAY = group("Today", Page, Desk, DateLine, Week)
MONTH = group("Month", Month, Numbers, Dots, Year)
PROGRESS = group("Progress", Progress, Rings, Weeks, Percent)


def migrate(settings: dict) -> list[str]:
    """Old Countdown settings (one label + target) and old Calendar dates, into today's form."""
    notes = events.from_old(settings)
    fixed, dropped = [], []
    for e in settings.get("events") or []:
        if isinstance(e, dict) and isinstance(e.get("date"), str) and not e.get("holiday"):
            when = e["date"].strip()
            if m := re.fullmatch(r"(\d\d)-(\d\d)", when):           # every year
                e.update(date=f"2000-{m[1]}-{m[2]}", repeat="yearly")
            elif re.fullmatch(r"\d{4}-\d\d-\d\d", when):
                e.setdefault("repeat", "once")
            else:                                                     # a rule the editor never offered
                dropped.append(e)
                continue
        fixed.append(e)
    if dropped:
        notes.append(f"calendar: dropped {len(dropped)} date(s) written as rules: "
                     + ", ".join(repr(e.get("title", "")) for e in dropped))
    if "events" in settings:
        settings["events"] = fixed
    return notes


def with_linked(settings: dict, ctx) -> dict:
    """Your own dates plus the showings of the calendars linked in Settings (from their last good copy),
    and the Matrix's colour coding (Settings > Calendars) as ``_colours``."""
    if ctx.calendar and ctx.calendar.get("code"):
        settings = {**settings, "_colours": ctx.calendar}
    if not settings.get("linked_calendars", True):
        return settings
    try:
        linked = calendars.events(ctx.tz)
    except Exception:  # noqa: BLE001 - a calendar problem never stops the screen; it's in the log
        logging.getLogger("bongo.calendars").exception("couldn't add the linked calendars")
        linked = []
    return {**settings, "events": list(settings.get("events") or []) + linked} if linked else settings


MODULE = Module(
    id="calendar",
    name="Calendar",
    description="Today, the month, how far through the year, and a countdown to what's next. One list of "
                "your dates feeds every look.",
    styles={"page": TODAY[Page], "desk": TODAY[Desk], "date_line": TODAY[DateLine], "week": TODAY[Week],
            "month": MONTH[Month], "numbers": MONTH[Numbers], "dots": MONTH[Dots], "year": MONTH[Year],
            "progress": PROGRESS[Progress], "rings": PROGRESS[Rings], "weeks": PROGRESS[Weeks],
            "percent": PROGRESS[Percent],
            **{k: countdown.grouped(v) for k, v in countdown.STYLES.items()}},
    defaults=dict(
        holidays=["christmas", "new_year", "valentines", "easter", "halloween", "bonfire_night"],
        events=[],
        linked_calendars=True,  # also show the calendars linked in Settings > Calendars (Google, iPhone...)
        week_start="monday",
        text_colour="#ECEEF1", accent="#FFB547", event_colour="#5AC8FA", background="#0B0C0E",
        icons=False,  # an 8x8 icon beside each upcoming date (Page); the countdowns default it on
    ),
    choices={"holidays": list(RULES), "week_start": ["monday", "sunday"]},
    limits={"times_to_scroll": (1, 10)},
    basic=("events", "linked_calendars", "holidays", "week_start"),
    check=events.check,
    migrate=migrate,
    help=HELP,
    extend=with_linked,
    choice_names={"holidays": {"labels": choice_labels(), "groups": choice_groups()}},
    item_fields={"events": countdown.EVENT_FIELDS},
    renames={"countdown": {"days": "countdown_days", "clock": "countdown_clock", "flip": "countdown_flip",
                           "ring": "countdown_ring", "tearoff": "countdown_tearoff", None: "countdown_days"}},
    # (said when a backup is restored: a calendar's link is never in one)
    needs=lambda settings: None if not settings.get("linked_calendars", True) or calendars.load() else
    "linked calendars (Google, iPhone), and none are linked here: links are never in a backup, so add yours in Settings › Calendars",
)
