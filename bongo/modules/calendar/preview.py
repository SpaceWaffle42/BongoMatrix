"""Settings > Calendars > Coming up: everything the Calendar screens will show over the next few weeks,
in one list: the linked calendars (bongo/calendars.py), the dates added on Calendar screens and the
holidays ticked there. Each comes with its colour (the colour coding the panel uses) and its icon."""
from __future__ import annotations

import datetime as dt
import logging

from bongo import calendars, holidays

from .countdown.events import _day, _yearly

log = logging.getLogger(__name__)

OWN, HOLIDAYS = "own", "holidays"
MOST_DAYS = 120   # as far ahead as the screens are given


def _screens(config: dict, modules: dict) -> list[dict]:
    """The settings of every Calendar screen that's on (with its defaults filled in)."""
    module = modules.get("calendar")
    if module is None or "calendar" in (config.get("modules_off") or []):
        return []
    out = []
    for entry in config.get("playlist") or []:
        if not isinstance(entry, dict) or entry.get("module") != "calendar" or entry.get("enabled") is False:
            continue
        try:
            out.append(module.settings_for(entry.get("style"), entry.get("settings") or {}))
        except ValueError:
            continue   # a broken screen is reported where it's edited
    return out


def _days(spec, start: dt.date, end: dt.date):
    """The days a holiday id, or one of your dates, falls on from ``start`` up to (not) ``end``."""
    day = start
    while day < end:
        if isinstance(spec, dt.date):
            if start <= spec < end:
                yield spec
            return
        try:
            found = holidays.next_date(spec, day) if isinstance(spec, str) else _yearly(spec[1], day)
        except ValueError:
            return
        if found >= end:
            return
        yield found
        day = found + dt.timedelta(days=1)


def coming_up(config: dict, modules: dict, tz=None, now: dt.datetime | None = None, days: int = 56) -> dict:
    from bongo.config import calendar_look

    days = max(1, min(MOST_DAYS, int(days)))
    now = now or (dt.datetime.now(tz) if tz else dt.datetime.now().astimezone())
    start, end = now.date(), now.date() + dt.timedelta(days=days)
    look = calendar_look(config)
    screens = _screens(config, modules)
    items, seen = [], set()

    def add(source, day, title, colour, icon=None, time=None):
        key = (day.isoformat(), time or "", title.strip().lower())
        if key in seen:                                        # the same date on two screens, or a holiday twice
            return
        seen.add(key)
        items.append({"date": day.isoformat(), "time": time, "title": title.strip(), "source": source, "colour": colour, "icon": icon})

    for hid in sorted({h for s in screens for h in s.get("holidays", []) if h in holidays.RULES}):
        for day in _days(hid, start, end):
            add(HOLIDAYS, day, holidays.label(hid), look["holidays"], holidays.RULES[hid].icon)
    for s in screens:
        for e in s.get("events") or []:
            try:
                if e.get("holiday"):
                    spec = e["holiday"]
                elif e.get("repeat") == "yearly":
                    spec = ("yearly", _day(e["date"]))
                else:
                    spec = _day(e["date"])
            except (KeyError, ValueError):
                continue
            icon = e.get("icon") if e.get("icon") not in (None, "", "none") else None
            if icon is None and e.get("holiday") in holidays.RULES and e.get("icon") != "none":
                icon = holidays.RULES[e["holiday"]].icon
            for day in _days(spec, start, end):
                add(OWN, day, str(e.get("title", "")), e.get("colour") or look["own"], icon, e.get("time") or None)
    linked = calendars.load()
    if any(s.get("linked_calendars", True) for s in screens):
        try:
            for e in calendars.events(tz, now, kick=False, days=days, with_calendar=True):
                if e["date"] >= end.isoformat():
                    continue
                add(e["calendar"], _day(e["date"]), e["title"], e["colour"], e.get("icon"), e.get("time"))
        except Exception:  # noqa: BLE001 - a calendar problem never stops the page; it's in the log
            log.exception("couldn't read the linked calendars for the preview")
    items.sort(key=lambda i: (i["date"], i["time"] or "", i["source"] == HOLIDAYS))
    sources = [{"id": f["id"], "name": f["name"], "colour": f.get("colour"), "linked": True, "on": f.get("on", True)} for f in linked]
    sources += [{"id": OWN, "name": "Your dates", "colour": look["own"]}, {"id": HOLIDAYS, "name": "Holidays", "colour": look["holidays"]}]
    return {"items": items, "sources": sources, "code": look["code"], "days": days, "today": start.isoformat(),
            "screens": len(screens)}
