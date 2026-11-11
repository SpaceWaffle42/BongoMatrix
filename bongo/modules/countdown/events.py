"""The events a Countdown counts to, and which one is on now.

Each event is a small dict from the dashboard:

    {"title": "Mum's birthday", "date": "2026-03-14", "repeat": "yearly",
     "time": "18:30", "until": "20:00", "message": "Happy birthday!", "colour": "#FF6FA8",
     "from": "2026-02-01"}

or a holiday instead of a date: ``{"title": "Christmas", "holiday": "christmas"}`` (holidays
come round every year by themselves). Only ``title`` and a date or holiday are needed.

The display always shows the soonest event that isn't finished. An event with no time is "on"
for the whole of its day; one with a time is "on" until ``until`` (that day), or for an hour.
While it's on, the panel shows its message; then it moves on to the next event.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import NamedTuple

from bongo.holidays import RULES, next_date

REPEATS = ("once", "yearly")
FIELDS = {"title", "date", "holiday", "repeat", "time", "until", "message", "colour", "from"}
ON_FOR = dt.timedelta(hours=1)          # a timed event with no "until"
PROGRESS = dt.timedelta(days=30)        # the bar fills over this unless "from" is set


class Occurrence(NamedTuple):
    event: dict
    target: dt.datetime   # when the countdown reaches zero
    end: dt.datetime      # when the event stops showing as "on"
    start: dt.datetime    # where the progress bar starts


def _time(text):
    return dt.time.fromisoformat(text) if text else None


def _day(text):
    return dt.date.fromisoformat(str(text)[:10])


def _yearly(day: dt.date, after: dt.date) -> dt.date:
    """``day``'s month and day on or after ``after`` (29 Feb falls on 28 Feb in other years)."""
    for year in (after.year, after.year + 1):
        try:
            d = day.replace(year=year)
        except ValueError:
            d = dt.date(year, 2, 28)
        if d >= after:
            return d
    raise AssertionError("unreachable")


def _span(event, day, tz):
    """(target, end) for the event on ``day``."""
    at = _time(event.get("time"))
    target = dt.datetime.combine(day, at or dt.time(), tzinfo=tz)
    if at is None:
        return target, target + dt.timedelta(days=1)
    until = _time(event.get("until"))
    end = dt.datetime.combine(day, until, tzinfo=tz) if until and until > at else target + ON_FOR
    return target, end


def occurrence(event: dict, now: dt.datetime) -> Occurrence | None:
    """The event's next showing that hasn't ended by ``now`` (None: a one-off already over)."""
    tz, today = now.tzinfo, now.date()
    for after in (today, today + dt.timedelta(days=1)):  # today's may have ended already
        if event.get("holiday"):
            day = next_date(event["holiday"], after)
        elif event.get("repeat") == "yearly":
            day = _yearly(_day(event["date"]), after)
        else:
            day = _day(event["date"])
        target, end = _span(event, day, tz)
        if end > now:
            break
        if not event.get("holiday") and event.get("repeat") != "yearly":
            return None
    start = dt.datetime.combine(_day(event["from"]), dt.time(), tzinfo=tz) if event.get("from") else target - PROGRESS
    return Occurrence(event, target, end, min(start, target))


def current(events: list[dict], now: dt.datetime) -> Occurrence | None:
    """The soonest event that isn't over (first in the list wins a tie)."""
    found = [o for o in (occurrence(e, now) for e in events) if o is not None]
    return min(found, key=lambda o: o.target, default=None)


def problems(events) -> list[str]:
    """What's wrong with an events list, in words for the dashboard."""
    if not isinstance(events, list):
        return ["events: expected a list"]
    out = []
    for i, e in enumerate(events, 1):
        where = f"events: event {i}"
        if not isinstance(e, dict):
            out.append(f"{where} must be an object")
            continue
        if extra := set(e) - FIELDS:
            out.append(f"{where}: unknown fields {sorted(extra)}")
        if not str(e.get("title") or "").strip():
            out.append(f"{where} needs a title")
        if bool(e.get("holiday")) == bool(e.get("date")):
            out.append(f"{where} needs a date or a holiday (one, not both)")
        if e.get("holiday") and e["holiday"] not in RULES:
            out.append(f"{where}: unknown holiday {e['holiday']!r}")
        for key, parse in (("date", _day), ("from", _day), ("time", _time), ("until", _time)):
            if e.get(key):
                try:
                    parse(e[key])
                except (TypeError, ValueError):
                    out.append(f"{where}: {key} {e[key]!r} isn't a valid {'time' if key in ('time', 'until') else 'date'}")
        if e.get("repeat") not in (None, *REPEATS):
            out.append(f"{where}: repeat must be once or yearly")
        if e.get("colour") and not re.fullmatch(r"#[0-9A-Fa-f]{6}", str(e["colour"])):
            out.append(f"{where}: colour must look like #FFB547")
        for key in ("title", "message"):
            if e.get(key) is not None and not isinstance(e[key], str):
                out.append(f"{where}: {key} must be text")
    return out


def check(settings: dict) -> None:
    if found := problems(settings["events"]):
        raise ValueError("; ".join(found))


def from_old(settings: dict) -> list[str]:
    """Settings from before the events list (one label + target) become one event, in place."""
    if "target" not in settings and "label" not in settings and "start" not in settings:
        return []
    target = settings.pop("target", None) or "next:christmas"
    event = {"title": settings.pop("label", None) or "Christmas"}
    start = settings.pop("start", None)
    if target.startswith("next:"):
        key = target[5:].strip().lower()
        if key in RULES:
            event["holiday"] = key
        elif m := re.fullmatch(r"(\d\d)-(\d\d)", key):
            event.update(date=f"2000-{m[1]}-{m[2]}", repeat="yearly")
        else:
            event["holiday"] = "christmas"  # a custom rule the editor can't show: nearest safe choice
    else:
        when = dt.datetime.fromisoformat(target)
        event["date"] = when.date().isoformat()
        if when.time() != dt.time():
            event["time"] = when.strftime("%H:%M")
    if start:
        event["from"] = str(start)[:10]
    if "events" not in settings:
        settings["events"] = [event]
    return [f"countdown: moved {event['title']!r} into the events list"]
