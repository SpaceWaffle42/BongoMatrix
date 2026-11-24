"""Your own calendars (Google, iPhone/iCloud, Outlook...) on the Calendar screens, read from each
calendar's link: read-only and one-way, with no sign-in.

- Google: Settings > (the calendar) > Integrate calendar > "Secret address in iCal format".
- iPhone/iCloud: Calendar > Calendars > (i) > Public Calendar > Share Link (a ``webcal://`` link).

Anyone with a Google secret address can read that calendar, so the links live in
``data/calendars.json`` (only on the Pi, mode 600: never in the config, the dashboard or the logs,
and no error message ever quotes one). Each calendar is fetched again when a Calendar screen comes
round and its copy is 15 minutes old, by one background worker, one calendar at a time (a big feed
takes a while to read on a Pi Zero, and only one is in memory at once). The copies are kept in
``data/calendar-cache.json`` so a restart or a dropped connection shows the last good copy (for a
day at most). Rendering never waits on the network. BONGO_OFFLINE=1 stops all fetching.

Each showing becomes the Calendar module's own event form ({"title", "date", "time", "until",
"colour", "icon"}), so every Calendar look and countdown shows them beside your own dates. A screen
gets the next ``SHOW_DAYS`` days of them, at most ``SHOW_MOST``: the looks check every event on
every frame.
"""
from __future__ import annotations

import datetime as dt
import http.client
import json
import logging
import os
import queue
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from bongo import durable

DATA = Path(__file__).resolve().parents[1] / "data"
FILE = Path(os.environ.get("BONGO_CALENDARS", DATA / "calendars.json"))
CACHE = Path(os.environ.get("BONGO_CALENDAR_CACHE", DATA / "calendar-cache.json"))
EVERY = 15 * 60            # seconds between fetches of a calendar
KEEP = 24 * 3600           # a copy older than this (no fetch has worked for a day) isn't shown
SHOW_DAYS = 120            # what a screen is given: the next four months...
AHEAD = SHOW_DAYS + 2      # days of showings read ahead (no more than a screen can show)
MOST = 400                 # showings kept per calendar
SHOW_MOST = 120            # ...and no more than this many showings, soonest first
MAX_BYTES = 5_000_000
TIMEOUT = 20
MOST_CALENDARS = 8
COLOURS = ("#5AC8FA", "#FF6FA8", "#4ADE80", "#FFB547", "#B98CFF", "#FF7A59", "#ECEEF1")
PRIVATE = {"PRIVATE", "CONFIDENTIAL"}
log = logging.getLogger("bongo.calendars")
_lock = threading.RLock()
_cache: dict | None = None
_queue: queue.Queue = queue.Queue()
_queued: set[str] = set()   # calendars waiting for (or in) the worker
_worker: list[threading.Thread] = []

# an 8x8 icon guessed from the title (a calendar feed has no icons); the first match wins
ICON_WORDS = {
    "cake": r"birthday|bday|b'day", "plane": r"flight|airport|fly to|flying", "bin": r"\bbins?\b|recycling|rubbish",
    "ring": r"wedding|anniversary|engage", "school": r"\bschool\b|\bterm\b|inset day", "car": r"\bmot\b|garage|car service",
    "ball": r"football|match\b|\bgym\b|training", "music": r"concert|\bgig\b|band\b", "party": r"party|celebrat",
    "heart": r"date night|valentine", "tree": r"christmas|xmas", "gift": r"present|secret santa",
    "paw": r"\bvet\b|\bdog\b|\bcat\b", "house": r"moving|house|plumber|electrician", "pin": r"dentist|doctor|appointment|\bgp\b",
    "work": r"\bwork\b|meeting|shift|office",
}


class FeedError(ValueError):
    """Why a calendar couldn't be read, in words for the dashboard (never with the link in it)."""


def _private_write(path: Path, text: str) -> None:
    """Write a file only the Matrix can read (it's created that way, never readable for a moment)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    durable.replace(tmp, path)


# -- the saved list (with the links) ----------------------------------------------------------------

def load() -> list[dict]:
    try:
        feeds = json.loads(FILE.read_text()).get("feeds")
    except (OSError, ValueError, AttributeError):
        return []
    return [f for f in feeds if isinstance(f, dict) and f.get("id") and f.get("url")] if isinstance(feeds, list) else []


def _save(feeds: list[dict]) -> None:
    _private_write(FILE, json.dumps({"feeds": feeds}, indent=1))


def public(feed: dict) -> dict:
    """What the dashboard may see of a calendar: everything but its link."""
    out = {k: feed.get(k) for k in ("id", "name", "colour", "on", "busy", "only")}
    out["icons"] = icons_of(feed)
    state = _state().get(feed["id"], {})
    out.update(checked=state.get("checked"), ok=state.get("ok"), error=state.get("error"),
               count=len(state.get("events") or []), kind=kind_of(feed["url"]), waiting=feed["id"] in _queued)
    return out


def kind_of(url: str) -> str:
    host = re.sub(r"(?i)^(webcal|https?)://", "", url).split("/")[0].lower()
    return "google" if "google.com" in host else "icloud" if "icloud.com" in host else "outlook" \
        if "outlook" in host or "office365" in host or "live.com" in host else "other"


def clean_url(url: str) -> str:
    """The link as the Matrix fetches it (webcal:// is https://). Only https: the link is a secret."""
    url = str(url or "").strip()
    if not re.fullmatch(r"(?i)(webcal|https)://[A-Za-z0-9.-]+\.[A-Za-z]{2,}(:\d{1,5})?(/[\x21-\x7e]*)?", url):
        if re.match(r"(?i)http://", url):
            raise FeedError("Use the https:// link: a plain http:// one would send it unencrypted")
        raise FeedError("Paste the calendar's link: it starts with https:// or webcal://")
    if len(url) > 2000:
        raise FeedError("That link is too long to be a calendar link")
    return re.sub(r"(?i)^webcal://", "https://", url)


def _clean(feed: dict, data: dict) -> dict:
    name = re.sub(r"\s+", " ", str(data.get("name", feed.get("name", "")))).strip()[:30]
    if not name:
        raise FeedError("Give the calendar a name, like Family or Bins")
    colour = str(data.get("colour", feed.get("colour") or COLOURS[0])).upper()
    if not re.fullmatch(r"#[0-9A-F]{6}", colour):
        raise FeedError("Pick a colour from the list")
    only = re.sub(r"\s+", " ", str(data.get("only", feed.get("only", "")))).strip()[:80]
    icons = _clean_icons(data["icons"]) if "icons" in data else feed.get("icons")
    return {**feed, "name": name, "colour": colour, "only": only, **({"icons": icons} if icons else {}),
            "on": bool(data.get("on", feed.get("on", True))), "busy": bool(data.get("busy", feed.get("busy", False)))}


def _room_for(url: str, feeds: list[dict]) -> None:
    if len(feeds) >= MOST_CALENDARS:
        raise FeedError(f"That's {MOST_CALENDARS} calendars already: remove one first")
    if any(f["url"] == url for f in feeds):
        raise FeedError("That calendar is already added")


def add(data: dict, fetch=None) -> dict:
    """A new calendar, read once first, so a wrong link is caught here and not later."""
    url = clean_url(data.get("url"))
    _room_for(url, load())
    feed = _clean({"id": secrets.token_hex(4), "url": url, "colour": COLOURS[len(load()) % len(COLOURS)]}, data)
    try:
        refresh(feed, fetch, keep=False)                     # a link that can't be read is refused here
    except FeedError:
        _forget(feed["id"])
        raise
    with _lock:
        feeds = load()
        try:
            _room_for(url, feeds)                            # another tab may have added it meanwhile
        except FeedError:
            _forget(feed["id"])
            raise
        _save(feeds + [feed])
    log.info("calendar added: %s (%s, %d showings)", feed["name"], kind_of(url), len(_state()[feed["id"]]["events"]))
    return feed


def change(feed_id: str, data: dict) -> dict:
    """Rename, recolour, switch on or off, filter: never the link (remove it and add it again)."""
    with _lock:
        feeds = load()
        feed = next((f for f in feeds if f["id"] == feed_id), None)
        if feed is None:
            raise KeyError(feed_id)
        new = _clean(feed, data)
        _save([new if f["id"] == feed_id else f for f in feeds])
    if new["busy"] != feed.get("busy", False):               # private events are kept or dropped as it's read
        ask(new["id"])
    return new


def _forget(feed_id: str) -> None:
    with _lock:
        state = _state()
        if feed_id in state:
            state = {k: v for k, v in state.items() if k != feed_id}
            _write_cache(state)


def remove(feed_id: str) -> bool:
    with _lock:
        feeds = load()
        if not any(f["id"] == feed_id for f in feeds):
            return False
        _save([f for f in feeds if f["id"] != feed_id])
        _forget(feed_id)
    log.info("calendar removed")
    return True


# -- reading a calendar -------------------------------------------------------------------------

def fetch_url(url: str) -> bytes:
    if os.environ.get("BONGO_OFFLINE"):
        raise FeedError("the Matrix is set to stay offline")
    req = urllib.request.Request(url, headers={"User-Agent": "BongoMatrix/3 (calendar)", "Accept": "text/calendar, */*"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise FeedError({401: "the calendar asked for a sign-in", 403: "the calendar isn't shared (or the link was reset)",
                         404: "there's no calendar at that link (was it reset?)"}.get(error.code, f"the calendar's server said {error.code}")) from None
    except (OSError, ValueError, http.client.HTTPException):   # their messages can quote the link: never shown
        raise FeedError("couldn't reach the calendar; check the Matrix's internet connection") from None
    return data


def _iso(value) -> str:
    """A time as text: in UTC, or marked as 'floating' (no zone in the calendar: the Matrix's own)."""
    return value.replace(tzinfo=None).isoformat() + "@float" if value.tzinfo is None else \
        value.astimezone(dt.timezone.utc).isoformat()


def parse(data: bytes, now: dt.datetime, busy_private: bool = False, days: int = AHEAD) -> list[dict]:
    """Every showing from ``now`` to ``days`` ahead: repeats unrolled with their moved and deleted ones,
    cancelled ones dropped, private ones dropped (or Busy). One broken event is skipped, not the lot."""
    import icalendar
    import recurring_ical_events
    if len(data) > MAX_BYTES:
        raise FeedError(f"the calendar is bigger than {MAX_BYTES // 1_000_000} MB")
    if b"BEGIN:VCALENDAR" not in data[:4000]:
        raise FeedError("that link doesn't give a calendar (copy the iCal / webcal link, not the page's address)")
    try:
        cal = icalendar.Calendar.from_ical(data)
        try:
            query = recurring_ical_events.of(cal, skip_bad_series=True)   # a broken repeat skips that event only
        except TypeError:                                                  # an older recurring-ical-events
            query = recurring_ical_events.of(cal)
        found = query.between(now - dt.timedelta(days=1), now + dt.timedelta(days=days))
    except Exception as error:  # noqa: BLE001 - a broken feed, named by its kind of problem only
        raise FeedError(f"the calendar couldn't be read ({type(error).__name__})") from None
    out = []
    for ev in found:
        try:
            if str(ev.get("STATUS", "")).upper() == "CANCELLED":
                continue
            title = re.sub(r"\s+", " ", str(ev.get("SUMMARY", ""))).strip() or "(no title)"
            if str(ev.get("CLASS", "")).upper() in PRIVATE:
                if not busy_private:
                    continue
                title = "Busy"
            start, end = ev["DTSTART"].dt, ev.get("DTEND").dt if ev.get("DTEND") else None
            if isinstance(start, dt.datetime):
                same = isinstance(end, dt.datetime) and (end.tzinfo is None) == (start.tzinfo is None)
                end = _iso(end) if same and end > start else None      # an odd end: an hour, like no end
                out.append({"title": title[:60], "start": _iso(start), "end": end})
            else:
                last = (end - dt.timedelta(days=1)) if isinstance(end, dt.date) and end > start else start
                out.append({"title": title[:60], "day": start.isoformat(), "last": last.isoformat()})
        except Exception:  # noqa: BLE001 - one odd event (no start, a strange date) doesn't stop the rest
            continue
    out.sort(key=lambda e: e.get("start") or e["day"])
    return out[:MOST]


def refresh(feed: dict, fetch=None, keep: bool = True) -> None:
    """Fetch and read one calendar now. On any failure the last good copy stays, with the reason
    (``keep``: only for a calendar still on the list, so a removed one never comes back)."""
    fetch = fetch or fetch_url
    started = time.time()
    try:
        # (the one clock, ``now_s``: what counts as still to come is worked out from it everywhere else too)
        events = parse(fetch(feed["url"]), dt.datetime.fromtimestamp(now_s(), dt.timezone.utc), feed.get("busy", False))
    except ImportError:
        error = FeedError("the calendar reader isn't installed yet: update the Matrix")
    except FeedError as e:
        error = e
    except Exception as e:  # noqa: BLE001 - never the message: it might quote the link
        error = FeedError(f"the calendar couldn't be read ({type(e).__name__})")
    else:
        error = None
    with _lock:
        if keep and not any(f["id"] == feed["id"] for f in load()):
            return
        state = dict(_state())
        old = state.get(feed["id"], {})
        if error is None:
            state[feed["id"]] = {"checked": started, "ok": started, "error": None, "events": events}
        else:
            state[feed["id"]] = {**old, "checked": started, "error": str(error)}
        if error is None and old.get("events") == events and not old.get("error") and now_s() - (old.get("ok") or 0) < KEEP / 2:
            global _cache
            _cache = state                                   # nothing new: spare the SD card the write
        else:
            _write_cache(state)
    if error is not None:
        log.warning("calendar %s: %s", feed.get("name"), error)
        raise error


def now_s() -> float:
    return time.time()


# -- the worker: one calendar at a time, in the background ---------------------------------------

def ask(feed_id: str) -> None:
    """Fetch this calendar again soon (in the background)."""
    with _lock:
        if feed_id in _queued:
            return
        _queued.add(feed_id)
        _queue.put(feed_id)
        if not _worker or not _worker[0].is_alive():
            _worker[:] = [threading.Thread(target=_work, name="calendars", daemon=True)]
            _worker[0].start()


def _work() -> None:
    while True:
        try:
            feed_id = _queue.get(timeout=60)
        except queue.Empty:
            with _lock:
                if _queue.empty():
                    _worker.clear()
                    return
            continue
        feed = next((f for f in load() if f["id"] == feed_id), None)
        try:
            if feed is not None:
                refresh(feed)
        except FeedError:
            pass
        except Exception:  # noqa: BLE001 - never take the player down
            log.error("calendar refresh failed")
        finally:
            with _lock:
                _queued.discard(feed_id)


def refresh_all(force: bool = False) -> int:
    """Ask the worker for every switched-on calendar (``force``: even if it was read just now)."""
    if os.environ.get("BONGO_OFFLINE"):
        return 0
    n = 0
    for feed in load():
        st = _state().get(feed["id"], {})
        if feed.get("on", True) and (force or now_s() - (st.get("checked") or 0) >= EVERY):
            ask(feed["id"])
            n += 1
    return n


# -- the cache ----------------------------------------------------------------------------------

def _state() -> dict:
    global _cache
    with _lock:
        if _cache is None:
            try:
                _cache = json.loads(CACHE.read_text())
            except (OSError, ValueError):
                _cache = {}
            if not isinstance(_cache, dict):
                _cache = {}
        return _cache


def _write_cache(state: dict) -> None:
    """Replace the whole state (readers keep the dict they already have)."""
    global _cache
    _cache = state
    try:
        _private_write(CACHE, json.dumps(state))
    except OSError as error:
        log.warning("couldn't save the calendar cache: %s", type(error).__name__)


def _reset_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None
        _queued.clear()


# -- what the Calendar screens show -------------------------------------------------------------

ICON_MODES = ("guess", "one", "none")
MOST_RULES = 20


def icons_of(feed: dict) -> dict:
    """A calendar's icons: guessed from each title (the default), one for every event, or none; and
    "icons by word" rules, which come first: a title with that word (or a word starting with it) gets that icon."""
    look = feed.get("icons") or {}
    return {"mode": look.get("mode", "guess"), "one": look.get("one"), "rules": look.get("rules") or []}


def _clean_icons(value) -> dict:
    from bongo.icons import ICONS
    if not isinstance(value, dict):
        raise FeedError("Icons: send the icon choices")
    mode, one = value.get("mode", "guess"), value.get("one") or None
    if mode not in ICON_MODES:
        raise FeedError("Icons: guess from each title, one for every event, or none")
    if one is not None and one not in ICONS or mode == "one" and one is None:
        raise FeedError("Pick an icon from the list")
    rules, seen = [], set()
    for rule in value.get("rules") or []:
        if not (isinstance(rule, (list, tuple)) and len(rule) == 2 and isinstance(rule[0], str)):
            raise FeedError("Icons by word: a word and an icon each")
        word = re.sub(r"\s+", " ", str(rule[0])).strip().lower()[:30]
        if not word or rule[1] not in ICONS:
            raise FeedError("Icons by word: type a word and pick an icon from the list")
        if word not in seen:
            seen.add(word)
            rules.append([word, rule[1]])
    if len(rules) > MOST_RULES:
        raise FeedError(f"Icons by word: up to {MOST_RULES} words")
    return {"mode": mode, "one": one, "rules": rules}


def icon_for(feed: dict, title: str) -> str | None:
    """The icon an event of this calendar shows (see icons_of)."""
    look, low = icons_of(feed), title.lower()
    for word, icon in look["rules"]:
        if re.search(r"\b" + re.escape(word), low):
            return icon
    if look["mode"] == "none":
        return None
    if look["mode"] == "one":
        return look["one"]
    return guess_icon(title)


def guess_icon(title: str) -> str | None:
    low = title.lower()
    return next((icon for icon, words in ICON_WORDS.items() if re.search(words, low)), None)


def _when(text: str, tz) -> dt.datetime:
    """A saved time in the Matrix's zone (``tz`` None: the Pi's own zone, with its summer time)."""
    if text.endswith("@float"):
        naive = dt.datetime.fromisoformat(text[:-6])
        return naive.replace(tzinfo=tz) if tz else naive.astimezone()
    moment = dt.datetime.fromisoformat(text)
    return moment.astimezone(tz) if tz else moment.astimezone()


def _wanted(title: str, only: str) -> bool:
    words = [w for w in re.split(r"[,\s]+", only.lower()) if w]
    return not words or any(re.search(rf"\b{re.escape(w)}\b", title.lower()) for w in words)


def _shown(e: dict, tz, now: dt.datetime) -> dict | None:
    """One saved showing as a Calendar event on its next (or current) day, or None if it's over."""
    if "day" in e:
        last = dt.date.fromisoformat(e["last"])
        if last < now.date():
            return None
        return {"date": max(dt.date.fromisoformat(e["day"]), now.date()).isoformat()}   # a holiday under way: today
    start = _when(e["start"], tz)
    end = _when(e["end"], tz) if e.get("end") else start + dt.timedelta(hours=1)
    if end <= now:
        return None
    day = max(start.date(), now.date())                      # past midnight, still going: today's part
    ev = {"date": day.isoformat(), "time": start.strftime("%H:%M") if day == start.date() else "00:00"}
    until = end.strftime("%H:%M") if end.date() == day else "23:59"
    if until > ev["time"]:
        ev["until"] = until
    return ev


def events(tz=None, now: dt.datetime | None = None, kick: bool = True, days: int | None = None,
           with_calendar: bool = False) -> list[dict]:
    """The switched-on calendars' next showings as Calendar events (soonest first, the next ``days``
    days (default ``SHOW_DAYS``), at most ``SHOW_MOST``), from the last good copy; due ones are asked for again. Each has its
    calendar's colour and icon; ``with_calendar`` adds which calendar it's from (``calendar``: its id)."""
    feeds = [f for f in load() if f.get("on", True)]
    if not feeds:
        return []
    if kick:
        refresh_all()
    if isinstance(tz, str):
        tz = ZoneInfo(tz)
    now = now or (dt.datetime.now(tz) if tz else dt.datetime.now().astimezone())
    horizon = (now + dt.timedelta(days=SHOW_DAYS if days is None else days)).date().isoformat()
    state, out = _state(), []
    for feed in feeds:
        st = state.get(feed["id"]) or {}
        if not st.get("ok") or now_s() - st["ok"] > KEEP:
            continue
        mine = 0
        for e in st.get("events") or []:
            if (e.get("day") or e.get("start", ""))[:10] > horizon or mine >= SHOW_MOST:
                break                                          # saved soonest first
            if not _wanted(e["title"], feed.get("only", "")) or (ev := _shown(e, tz, now)) is None:
                continue
            out.append((feed, {"title": e["title"][:40], "repeat": "once", **ev, "colour": feed.get("colour") or COLOURS[0]}))
            mine += 1
    out = sorted(out, key=lambda fe: (fe[1]["date"], fe[1].get("time", "")))[:SHOW_MOST]
    for feed, ev in out:                                       # icons for the ones shown only
        if icon := icon_for(feed, ev["title"]):
            ev["icon"] = icon
        if with_calendar:
            ev["calendar"] = feed["id"]
    return [ev for _, ev in out]
