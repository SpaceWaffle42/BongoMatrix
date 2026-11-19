"""Back up and restore (Settings > Back up and restore): the show as one file, to keep or to put on another Matrix.

A backup is ``<name>-matrix-YYYY-MM-DD.bongo.json``:

    {"bongo_backup": 1, "name": "Kitchen", "made": "2026-11-18T09:30:00+00:00", "version": "3.0.412",
     "addons": {"sports": "Sports"},
     "config": {"playlist": [...], "splash": {...}, "sleep": {...}, "location": {...}, "timezone": "Europe/London"}}

What is never in one, and why it can't be:

- Only ``SECTIONS`` of the config are copied, each key by key from a list of what belongs there. The rest of
  the config (the panel and its brightness, the safe area, the dashboard's address, the update source, the
  name's own settings) is never looked at, and neither is any other file: the OpenWeather key, Spotify's
  sign-in, the calendar links, the paired browsers and Wi-Fi each live in a file of their own (bongo/private.py),
  which nothing here opens.
- A screen keeps only the settings its module has today (so a key an older build kept in a screen's settings
  goes), without the ones the module calls secrets (``Module.secrets``, ``Style.secrets``: ``_declared``).
- As a net under that, for a module that forgot to say and for what is typed into any setting (bongo/secretlike.py):
  a setting whose name reads as a secret, at any depth, and any text that carries one (a web address with a
  password, a key or a token in it, a private calendar link, a ``WIFI:`` code, a JWT...) are left out too. A
  setting is left out whole, never mended: half an address would be a feed that quietly doesn't work. In a list
  (News feeds), the entry that carries the secret goes and the others stay.

What the net can't tell stays in, and the dashboard says so ("Check any web addresses with a key in them before
sharing the file", shown while a backup would hold a web address at all): a key in the path of an address that
looks like any other, and whatever is typed into free text.

A screen that had something left out says which settings in ``left_out``, so the restore can say what to type in
again. Media files aren't in a backup either: screens name them, and the restore says which are missing.

Restoring (``plan``) is all or nothing. The file is read into a whole new config (this Matrix's own for
everything a backup doesn't hold), brought up to date and checked by the same ``config.tidy`` and
``config.validate`` a dashboard save goes through, and only a config that passes is handed back to be saved.
Anything else raises ``Refused`` with the reason in plain words, and nothing has changed.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from bongo import config as cfg, media, secretlike

log = logging.getLogger("bongo.backup")

FORMAT = 1                      # goes up when an older Matrix could no longer read the file
MARK = "bongo_backup"           # the key that makes a file a backup; its value is the format
SUFFIX = ".bongo.json"
MOST_BYTES = 1024 * 1024        # a backup is tens of KB: a file over this isn't one
MOST_PLAYLIST = 60 * 1024       # a dashboard save sends the whole playlist and may be 64 KB (bongo/web): a restored
#                                 playlist must still be one the dashboard can save
SECTIONS = ("playlist", "splash", "sleep", "location", "timezone")   # all a backup holds of the config
ENTRY = ("module", "style", "seconds", "settings", "enabled", "times")   # (config.ENTRY_KEYS)
SPLASH = ("sources", "speed", "fit", "enabled", "boot")
SLEEP = ("sources", "speed", "fit", "enabled", "from", "to", "fade", "curve")   # not its brightness: that's the Matrix's own
LOCATION = ("name", "latitude", "longitude")
SLEEP_BRIGHTNESS = 15           # what the dashboard's Sleep card starts at, for a Matrix that has no night brightness yet


class Refused(ValueError):
    """Why a file can't be restored, in words for the dashboard. ``newer``: it's from a newer version;
    ``problems``: what config.validate said; ``about``: whose backup it is, when that could be read."""

    def __init__(self, message: str, *, newer: bool = False, problems=(), about: dict | None = None):
        super().__init__(message)
        self.newer, self.problems, self.about = newer, list(problems), about


# -- making one ------------------------------------------------------------------------------------------------
def slug(name) -> str:
    """``Living room`` -> ``living-room-matrix``: the start of a backup's file name (letters and digits only)."""
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40].strip("-")
    if not text:
        return "bongo-matrix"
    return text if text.endswith("matrix") else f"{text}-matrix"


def file_name(name, day: dt.date) -> str:
    return f"{slug(name)}-{day.isoformat()}{SUFFIX}"


def _names(value) -> tuple:
    return (value,) if isinstance(value, str) else tuple(n for n in (value or ()) if isinstance(n, str))


def _declared(module, name: str, style: str | None = None) -> bool:
    """Whether ``module`` calls its setting ``name`` a secret (``Module.secrets``, and ``style``'s own
    ``Style.secrets``). The one place a backup asks: everything else here is the net under it."""
    from bongo import private
    return private.is_secret(module, name, style)


def _scrub(value):
    """``value`` without anything inside it that reads as a secret (bongo/secretlike.py), and whether something
    was taken out. Text that carries one goes whole (None): it is never mended."""
    if isinstance(value, str):
        return (None, True) if secretlike.text(value) else (value, False)
    if isinstance(value, list):
        kept, cut = [], False
        for item in value:
            item, gone = _scrub(item)
            cut = cut or gone
            if not (gone and item is None):
                kept.append(item)
        return kept, cut
    if isinstance(value, dict):
        kept, cut = {}, False
        for name, item in value.items():
            if secretlike.name(name, item):
                cut = True
                continue
            item, gone = _scrub(item)
            cut = cut or gone
            if not (gone and item is None):
                kept[name] = item
        return kept, cut
    return value, False


def _screen(raw, modules) -> dict | None:
    """One playlist entry as a backup holds it (None: not an entry at all)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("module"), str):
        return None
    out = {k: copy.deepcopy(raw[k]) for k in ENTRY if k in raw and k != "settings"}
    settings = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
    module = modules.get(raw["module"])
    known = None
    if module is not None:   # (a module this Matrix doesn't have can't say: the net alone, then)
        cls = module.styles.get(raw.get("style") or module.default_style)
        known = {**module.defaults, **(cls.defaults if cls else {})}
    kept, left = {}, []
    for name, value in settings.items():
        if not isinstance(name, str) or (known is not None and name not in known):
            continue             # not a setting it has (config.tidy drops these as it loads): nothing to hand on
        declared = module is not None and _declared(module, name, raw.get("style") if isinstance(raw.get("style"), str) else None)
        if (declared and value not in (None, "")) or secretlike.name(name, value):
            left.append(name)
            continue
        value, cut = _scrub(copy.deepcopy(value))
        if cut:
            left.append(name)
            if value is None:
                continue
        kept[name] = value
    if kept:
        out["settings"] = kept
    if left:
        out["left_out"] = sorted(left)
    return out


def _only(rule, keys):
    return {k: copy.deepcopy(rule[k]) for k in keys if k in rule} if isinstance(rule, dict) else None


def make(config: dict, modules: dict, now: dt.datetime | None = None) -> dict:
    """The backup of ``config`` (as ``config.load`` and ``config.tidy`` give it)."""
    from bongo import __version__, addons

    if now is None:
        try:
            now = cfg.context_factory(config)().now()   # the Matrix's own day (its time zone): the date in the file's name
        except Exception:   # a time zone the Pi doesn't know (a hand-edited config): the Pi's own time, then
            now = dt.datetime.now()
    if now.tzinfo is None:
        now = now.astimezone()   # the Pi's own zone, said in the file
    playlist = [s for s in (_screen(e, modules) for e in config.get("playlist") or []) if s is not None]
    used = {s["module"] for s in playlist}
    zone = config.get("timezone")
    return {
        MARK: FORMAT,
        "name": str(config.get("name") or "")[:60],
        "made": now.isoformat(timespec="seconds"),
        "version": __version__,
        # the imported modules its screens use, by name, so a Matrix without one can say which to import; a screen
        # kept for one this Matrix doesn't have (restored from another's backup) is one too, its name unknown here
        "addons": {**{k: None for k in sorted(used) if k not in modules},
                   **{k: m.name for k, m in list(modules.items()) if k in used and addons.is_imported(m)}},
        "config": {"playlist": playlist, "splash": _only(config.get("splash"), SPLASH),
                   "sleep": _only(config.get("sleep"), SLEEP), "location": _only(config.get("location"), LOCATION),
                   "timezone": zone if isinstance(zone, str) and zone else None},
    }


def onto(saved: dict | None, plan: "Plan") -> dict:
    """The config file to write for ``plan``: the file as it is (``saved``, as read, without the defaults ``config.load``
    fills in), with only the sections a backup replaces put in. Everything else stays exactly as it was."""
    out = copy.deepcopy(saved) if isinstance(saved, dict) else {}
    for key in SECTIONS:
        out[key] = copy.deepcopy(plan.config[key])
    return out


def addresses(backup: dict) -> bool:
    """Whether a backup holds a web address at all in some screen's settings: one may still have a key in its
    path, which nothing can tell from any other path, so the dashboard says to check before sharing the file."""
    def holds(value) -> bool:
        if isinstance(value, dict):
            return any(holds(v) for v in value.values())
        if isinstance(value, list):
            return any(holds(v) for v in value)
        return secretlike.address(value)
    return any(holds(s.get("settings")) for s in backup["config"]["playlist"])


def dump(backup: dict) -> bytes:
    return (json.dumps(backup, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


# -- the last one made: remembered beside the config, never in it (so never in a backup) --------------------------
def state_path(config_path) -> Path:
    return Path(config_path).with_name("backup-state.json")


def remember(config_path, file: str, size: int, at: dt.datetime) -> None:
    """Note the backup just downloaded. Not being able to write the note is no reason not to hand the backup over."""
    path = state_path(config_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".backup-state-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"last": {"file": file, "bytes": size, "at": at.isoformat(timespec="seconds")}}, f)
            os.replace(tmp, path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as error:
        log.warning("couldn't note the last backup: %s", error)


def last(config_path) -> dict | None:
    """The last backup downloaded: {"file", "bytes", "at", "day"} ("18 Nov", with the year if it wasn't this one)."""
    try:
        said = json.loads(state_path(config_path).read_text())["last"]
        at = dt.datetime.fromisoformat(said["at"])
        out = {"file": str(said["file"])[:120], "bytes": int(said["bytes"]), "at": at.isoformat(timespec="seconds")}
    except (OSError, ValueError, KeyError, TypeError):
        return None
    out["day"] = day_words(at, year=at.year != dt.datetime.now(at.tzinfo).year)
    return out


def day_words(day, year: bool = True) -> str:
    """2 Nov 2026 (English whatever the Pi's language: the dashboard is)."""
    month = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")[day.month - 1]
    return f"{day.day} {month} {day.year}" if year else f"{day.day} {month}"


# -- reading one ---------------------------------------------------------------------------------------------
NOT_ONE = "This isn't a Bongo Matrix backup. A backup is a file ending .bongo.json, made with Download backup."


def read(text) -> dict:
    """The backup in ``text`` (a file's contents), or ``Refused``: too big, not JSON, or not a backup."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            raise Refused(NOT_ONE) from None
    if not isinstance(text, str):
        raise Refused(NOT_ONE)
    if len(text.encode("utf-8", "replace")) > MOST_BYTES:
        raise Refused(f"This file is too big to be a backup: a backup is never more than {MOST_BYTES // (1024 * 1024)} MB.")

    def no_such_number(word):   # NaN and Infinity aren't JSON, and no setting can be one
        raise ValueError(word)
    try:
        data = json.loads(text.lstrip("﻿"), parse_constant=no_such_number)
    except (ValueError, RecursionError):
        raise Refused("This file can't be read: it isn't a Bongo Matrix backup, or it's damaged or cut off.") from None
    if not isinstance(data, dict) or MARK not in data:
        raise Refused(NOT_ONE)
    if isinstance(data[MARK], bool) or not isinstance(data[MARK], int) or data[MARK] < 1 or not isinstance(data.get("config"), dict):
        raise Refused("This backup is damaged: it doesn't say what it holds.")
    return data


def about(data: dict) -> dict:
    """Whose backup it is: {"name", "date", "version"}, each as text to show (a file can say anything)."""
    name = data.get("name")
    name = re.sub(r"\s+", " ", "".join(c for c in name if c.isprintable() or c == " ")).strip()[:60] if isinstance(name, str) else ""
    version = data.get("version")
    version = version.strip()[:20] if isinstance(version, str) and version.isprintable() else ""
    try:
        day = day_words(dt.datetime.fromisoformat(data.get("made")))   # the day it was on that Matrix, as its file says
    except (TypeError, ValueError):
        day = ""
    return {"name": name, "date": day, "version": version}


def _newer(data: dict) -> bool:
    from bongo import __version__
    from bongo.hub import version_key

    if data[MARK] > FORMAT:
        return True
    theirs = data.get("version")
    if not isinstance(theirs, str) or not re.fullmatch(r"[0-9]+(\.[A-Za-z0-9_]+)*", theirs.strip()):   # (ASCII digits only)
        return False
    try:
        return version_key(theirs.strip()) > version_key(__version__)
    except TypeError:   # (never, as the parts are built: but a comparison mustn't be what stops a restore)
        return False


def _own_secrets(entry: dict, names: list, current: dict) -> bool:
    """A screen whose backup left secrets out (``names``) gets this Matrix's own back, if it has that very screen now:
    the same module and look, and every other setting the same (restoring last week's backup onto the Matrix it
    came from mustn't cost it its guest Wi-Fi password). True if it did."""
    settings = entry.get("settings") if isinstance(entry.get("settings"), dict) else {}
    rest = {k: v for k, v in settings.items() if k not in names}
    for mine in current.get("playlist") or []:
        had = mine.get("settings") if isinstance(mine, dict) and isinstance(mine.get("settings"), dict) else None
        if had is None or (mine.get("module"), mine.get("style") or None) != (entry["module"], entry.get("style") or None):
            continue
        if {k: v for k, v in had.items() if k not in names} == rest and any(n in had for n in names):
            entry["settings"] = {**rest, **{n: copy.deepcopy(had[n]) for n in names if n in had}}
            return True
    return False


@dataclass
class Plan:
    config: dict                 # the whole config to save
    review: dict                 # what to tell the owner first: see ``plan``
    notes: list = field(default_factory=list)   # what was brought up to date, for the log


def _count(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else many or one + 's'}"


def _listed(names, most: int = 5) -> str:
    names = list(dict.fromkeys(names))
    return ", ".join(names[:most]) + (f" and {len(names) - most} more" if len(names) > most else "")


def _file(name) -> str:
    return str(name).split("/", 1)[-1][:80]


def _shown(name) -> str:
    """A file name from a backup, safe and short to show (it may be anything at all)."""
    name = "".join(c for c in str(name) if c.isprintable())
    return name if len(name) <= 60 else name[:57] + "..."


def _damaged(what: str) -> Refused:
    return Refused(f"This backup is damaged: {what}. Nothing has been changed.")


def _odd_text(value) -> bool:
    """Whether a backup holds text no setting can have, anywhere: a control character (a NUL in a file name)."""
    if isinstance(value, str):
        return any(ord(c) < 32 and c not in "\t\n\r" for c in value)
    if isinstance(value, dict):
        return any(_odd_text(k) or _odd_text(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_odd_text(v) for v in value)
    return False


def plan(data: dict, current: dict, modules: dict) -> Plan:
    """What restoring ``data`` (from ``read``) onto the Matrix whose config is ``current`` would save, and what
    to say about it first. ``review`` is {"name", "date", "version", "screens", "warnings": [{"kind", "text"}],
    "notes": [text]}. Raises ``Refused`` and nothing else, whatever the file holds; changes nothing either way."""
    try:
        return _plan(data, current, modules)
    except Refused:
        raise
    except Exception:   # something no check here foresaw: logged, and said in words (never a 500, never a change)
        log.exception("a backup couldn't be read")
        raise Refused("This file can't be read as a backup: something in it isn't what a backup holds. "
                      "Nothing has been changed.", about=about(data) if isinstance(data, dict) else None) from None


def _plan(data: dict, current: dict, modules: dict) -> Plan:
    from bongo import __version__

    who = about(data)
    if _newer(data):
        if data[MARK] > FORMAT or not who["version"] or who["version"] == __version__:
            said = "This backup was made by a newer Bongo Matrix than this one."
        else:
            said = f"This backup was made by a newer Bongo Matrix ({who['version']}) than this one ({__version__})."
        raise Refused(f"{said} Update this Matrix first (Settings › Software), then restore it.", newer=True, about=who)
    held = data["config"]
    if _odd_text(held):
        raise _damaged("it has a character no setting can have (in a file name, say)")
    if missing := [k for k in SECTIONS if k not in held]:
        raise _damaged(f"it has no {missing[0]}")
    if not isinstance(held["playlist"], list):
        raise _damaged("its playlist isn't a list of screens")
    dropped = sorted(set(held) - set(SECTIONS))   # parts this version doesn't know (a newer one's): left where they are
    playlist, left_out = [], {}
    for i, raw in enumerate(held["playlist"]):
        if not isinstance(raw, dict) or not cfg.module_id(raw.get("module")):
            raise _damaged(f"screen {i + 1} of its playlist isn't a screen")
        entry = {k: copy.deepcopy(raw[k]) for k in ENTRY if k in raw}
        dropped += sorted(set(raw) - set(ENTRY) - {"left_out"})
        names = [n for n in raw.get("left_out") or [] if isinstance(n, str)] if isinstance(raw.get("left_out"), list) else []
        if names and not _own_secrets(entry, names, current):
            left_out[id(entry)] = names
        playlist.append(entry)
    if len(json.dumps(playlist)) > MOST_PLAYLIST:
        raise Refused(f"This backup's playlist is too big for a Matrix: {len(playlist)} screens with more settings than "
                      "the dashboard could save. Nothing has been changed.", about=who)
    merged = copy.deepcopy(current)
    merged["playlist"] = playlist
    for key, keys in (("splash", SPLASH), ("sleep", SLEEP), ("location", LOCATION)):
        rule = held[key]
        if rule is not None and not isinstance(rule, dict):
            raise _damaged(f"its {key} isn't a set of settings")
        if rule is not None:
            dropped += sorted(k for k in set(rule) - set(keys) if not (key == "sleep" and k == "brightness"))
        merged[key] = _only(rule, keys)
    if merged["sleep"] is not None:   # the night brightness stays this Matrix's own
        mine = current.get("sleep").get("brightness") if isinstance(current.get("sleep"), dict) else None
        merged["sleep"]["brightness"] = SLEEP_BRIGHTNESS if mine is None else mine
    if held["timezone"] is not None and not isinstance(held["timezone"], str):
        raise _damaged("its time zone isn't a name")
    merged["timezone"] = held["timezone"] or None

    notes = cfg.tidy(merged, modules)   # as loading it would: an older Bongo's settings brought up to date
    addon_names = data.get("addons") if isinstance(data.get("addons"), dict) else {}
    absent, lookless, kept = {}, [], []
    for entry in merged["playlist"]:
        module = modules.get(entry["module"])
        if module is None:           # kept, switched off (config.validate lets it be, and the player never plays it)
            entry["enabled"] = False
            name = addon_names.get(entry["module"])
            absent.setdefault(entry["module"], [entry["module"] in addon_names, 0, name if isinstance(name, str) else ""])[1] += 1
        elif isinstance(entry.get("style"), str) and entry["style"] not in module.styles:
            lookless.append(f"{module.name}: {entry['style'][:40]}")   # a look only a newer version has: nothing safe to guess
            continue
        kept.append(entry)
    merged["playlist"] = kept

    def named(module_id):
        imported, _, name = absent[module_id]
        name = name.strip()[:40] if name.strip().isprintable() else ""
        return name or module_id.replace("_", " ").capitalize()

    # only what this restore would bring in can stop it: a problem this Matrix already has elsewhere (its
    # overlays, say) is put right on its own page, as with a dashboard save
    emptied = copy.deepcopy(current)
    emptied.update(playlist=[], splash=None, sleep=None, location=None, timezone=None)
    before = set(cfg.validate(emptied, modules))
    problems = [p for p in cfg.validate(merged, modules) if p not in before]
    if problems:
        if absent and kept and not any(e.get("enabled", True) is not False for e in kept):
            raise Refused("Nothing in this backup could play here: every screen that's switched on needs "
                          f"{'an add-on' if len(absent) == 1 else 'add-ons'} this Matrix doesn't have ({_listed(map(named, absent))}). "
                          "Import it in Settings › Modules first, then restore. Nothing has been changed.", about=who)
        raise Refused("This backup can't be restored on this Matrix. Nothing has been changed.", problems=problems, about=who)

    warnings = []

    def warn(kind, text):
        warnings.append({"kind": kind, "text": text})

    if not kept:
        warn("empty", "This backup's playlist is empty: this Matrix would play one screen of every module in turn, "
                      "as a new Matrix does.")
    # media: files are named, never carried
    gone, users = [], 0
    for entry in kept:
        module = modules.get(entry["module"])
        if module is None or not isinstance(entry.get("settings"), dict):
            continue
        cls = module.styles.get(entry.get("style") or module.default_style)
        choices = {**module.choices, **(cls.choices if cls else {})}
        files = [v for k, v in entry["settings"].items() if choices.get(k) == "media" and isinstance(v, str)
                 and media.valid_name(v) and media.resolve(v) is None]
        if files:
            users += 1
            gone += files
    if users:
        many = len(set(gone)) > 1
        warn("media", f"{_count(users, 'screen uses', 'screens use')} media this Matrix doesn't have ({_listed(map(_file, gone))}): "
                      f"{'it shows' if users == 1 else 'they show'} as missing until you upload {'them' if many else 'it'}.")
    gone, where = [], []
    for key, label in (("splash", "Splash"), ("sleep", "Sleep")):
        rule = merged.get(key) or {}
        files = [s for s in rule.get("sources") or [] if media.valid_name(s) and media.resolve(s) is None]
        if files:
            where.append(label)
            gone += files
    boot = (merged.get("splash") or {}).get("boot")
    if boot and media.valid_name(boot) and media.resolve(boot) is None:
        where.append("the boot screen")
        gone.append(boot)
    if where:
        one = len(where) == 1 and where[0] != "the boot screen"
        said = where[0] if len(where) == 1 else f"{', '.join(where[:-1])} and {where[-1]}"
        said = said[0].upper() + said[1:]
        warn("media", f"{said} {'uses' if len(where) == 1 else 'use'} media this Matrix doesn't have ({_listed(map(_file, gone))}): "
                      f"{'it shows' if one or len(where) == 1 else 'they show'} as missing until you upload "
                      f"{'them' if len(set(gone)) > 1 else 'it'}.")
    # what a screen needs that is never in a backup: a key, a sign-in, a link (Module.needs)
    needs = {}
    for entry in kept:
        module = modules.get(entry["module"])
        if module is None or not module.needs:
            continue
        try:
            said = module.needs(module.settings_for(entry.get("style"), entry.get("settings") or {}))
        except Exception:   # a broken check mustn't stop a restore: the screen says for itself later
            log.exception("%s: needs failed", module.id)
            said = None
        if said:
            needs[(module.name, str(said))] = needs.get((module.name, str(said)), 0) + 1
    for (name, said), n in needs.items():
        warn("needs", f"{_count(n, f'{name} screen needs', f'{name} screens need')} {said}.")
    # what the backup left out of a screen's settings: named by screen and setting, never by what it was
    again = {}
    for at, entry in enumerate(kept, 1):
        names = left_out.get(id(entry))
        module = modules.get(entry["module"])
        if not names or module is None:
            continue
        held = entry.get("settings") if isinstance(entry.get("settings"), dict) else {}
        for part in (False, True):   # a setting gone whole; one that lost an entry (a feed in a list of feeds)
            some = [n.replace("_", " ")[:40] for n in names if (n in held) is part]
            if some:
                group = again.setdefault((module.name, part), [[], []])
                group[0].append(at)
                group[1] += some
    for (name, part), (places, settings) in again.items():
        one = len(places) == 1
        which = f"{name} screen {places[0]}" if one else f"{name} screens {_listed(map(str, places), 8)}"
        if part:
            warn("secrets", f"{which} lost part of {'its' if one else 'their'} {_listed(settings)}: an entry or address with a "
                            f"password or key in it is never in a backup. Add it again from the playlist.")
        else:
            warn("secrets", f"{which} {'needs its' if one else 'need their'} {_listed(settings)} typed in again: passwords, keys "
                            f"and addresses with one in them are never in a backup. Open {'it' if one else 'them'} from the playlist.")
    # add-ons (or modules) this Matrix doesn't have
    for imported in (True, False):
        ids = [k for k, v in absent.items() if v[0] is imported]
        if not ids:
            continue
        n, many = sum(absent[k][1] for k in ids), len(ids) > 1
        thing = ("add-ons" if many else "an add-on") if imported else ("modules" if many else "a module")
        warn("addons", f"{_count(n, 'screen uses', 'screens use')} {thing} this Matrix doesn't have ({_listed(map(named, ids))}): "
                       + ("it's" if n == 1 else "they're") + " kept, switched off, until "
                       + (f"you import {'them' if many else 'it'} (Settings › Modules)." if imported else
                          f"this Matrix has {'them' if many else 'it'}."))
    # modules switched off on this Matrix
    off = cfg.modules_off(merged)
    resting = [e["module"] for e in kept if e["module"] in off and e["module"] in modules and e.get("enabled", True) is not False]
    if resting:
        names = _listed(modules[k].name for k in resting)
        warn("off", f"{_count(len(resting), 'screen belongs', 'screens belong')} to a module that's switched off on this Matrix "
                    f"({names}): skipped until you switch it on in Settings › Modules.")
    if lookless:
        warn("looks", f"{_count(len(lookless), 'screen uses', 'screens use')} a look this version doesn't have "
                      f"({_listed(lookless)}): left out.")
    said, plain = [], []
    for note in notes:   # (config.tidy's words, for the log): said here in the dashboard's, which screen and which setting
        limit = re.fullmatch(r"playlist\[(\d+)\] ([a-z0-9_]+): ([a-z0-9_]+): (.+) is outside (.+) to (.+), now (.+)", note)
        swap = re.fullmatch(r"(splash|sleep): (?:dropped (.+) \((?:made for another screen|not a media file)\)|nothing left, using (.+))", note)
        boot = re.fullmatch(r"splash: boot screen is now (.+)", note)
        if limit:
            at, mid, setting, value, low, high, now = limit.groups()
            module = modules.get(mid)
            now = "its default" if now == "the default" else now
            said.append(f"Screen {int(at) + 1} ({module.name if module else mid}): {setting.replace('_', ' ')} was {value[:20]}, "
                        f"outside {low} to {high}: now {now[:20]}.")
        elif swap and swap.group(2):
            warn("screens", f"{swap.group(1).capitalize()}: {_shown(swap.group(2))} in the backup isn't a file this Matrix can show "
                            f"there: left out.")
        elif swap:
            warn("screens", f"{swap.group(1).capitalize()}: none of the backup's files could be used, so it shows "
                            f"{_file(swap.group(3))}, the built-in one.")
        elif boot:
            warn("screens", "The boot screen in the backup isn't a file this Matrix can show: "
                            + ("none, until you pick one in Settings › Splash." if boot.group(1) == "None" else f"{_file(boot.group(1))} instead."))
        else:
            plain.append(note)
    if plain or dropped:
        said.append(f"{_count(len(plain) + len(dropped), 'setting')} from another version of Bongo Matrix "
                    f"{'was' if len(plain) + len(dropped) == 1 else 'were'} brought into line with this one.")
    review = {**who, "screens": len(kept), "warnings": warnings, "notes": said}
    return Plan(merged, review, notes + [f"left out {str(name)[:40]!r} (not known to this version)" for name in dropped])
