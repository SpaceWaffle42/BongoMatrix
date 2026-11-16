"""Device configuration: panel options, timezone and the playlist (data/config.json)."""
from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

from bongo import media, pets
from bongo.module import Context, Module
from bongo.player import Entry
from bongo.safe_area import SafeArea
from bongo.schedule import screen_problems

DEFAULT_PATH = Path("data/config.json")

DEFAULTS = {
    "timezone": None,
    # where the Matrix is: {"name", "latitude", "longitude"}; weather and sky screens use it unless given their own
    "location": None,
    "panel": {"rows": 32, "cols": 64, "hardware_mapping": "adafruit-hat-pwm",
              "gpio_slowdown": 4, "brightness": 80},
    "safe_area": {"top": 0, "right": 0, "bottom": 0, "left": 0},
    # port80: plain http://name.local; hub_picture: the hub (matrix.local) may show its live picture;
    # badge (optional): the colour of this Matrix's logo on the hub, one of bongo.logo.COLOURS (none: picked from its id)
    "web": {"enabled": True, "host": "0.0.0.0", "port": 5000, "port80": True, "hub_picture": True},
    "playlist": [],
    # v2's default splash screen; "splash" items in the playlist play it. "boot": an optional file
    # played once at start-up (None: none; a new Matrix gets Round wall). A splash rule without "boot" is
    # from before 11 Nov 2026 and stays without one.
    "splash": {"sources": ["built-in/splash_loading_squares.gif"], "boot": "built-in/boot_round_wall.gif"},
    # modules switched off in Settings > Modules: their playlist and overlay screens are skipped and they're off
    # the dashboard's Add a module list; screens and settings are kept for switching back on. Splash, sleep
    # and pushes ("Show on the Matrix") have their own switches and still use the modules they need
    "modules_off": [],
    # Settings > Calendars: colour code the Calendar screens by where a date comes from. Linked calendars
    # have their own colours (bongo/calendars.py); "own" is the dates added on Calendar screens (an event's
    # own colour wins), "holidays" the holidays ticked there. Off: each screen's own colours, as before.
    "calendar_colours": {"code": False, "own": "#5AC8FA", "holidays": "#FF7A59"},
}


log = logging.getLogger(__name__)
_warned: set = set()   # damaged files already reported (the dashboard loads the config on every request)


def backup_path(path: Path = DEFAULT_PATH) -> Path:
    """The config as it was before the last save: what ``load`` falls back to if the file is damaged."""
    return Path(path).with_name(Path(path).name + ".bak")


def _read(path: Path) -> dict | None:
    """A saved config, or None if the file is missing or damaged (empty, cut off, not an object)."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load(path: Path = DEFAULT_PATH) -> dict:
    """The saved config over the defaults. Never raises: a damaged file (a power cut, a bad hand edit)
    gives the copy from before the last save, else the defaults, so the Matrix and its dashboard still
    start and it can be put right from there."""
    path, data = Path(path), {}
    if path.exists():
        data = _read(path)
        if data is None:
            data = _read(backup_path(path))
            try:
                seen = (str(path), path.stat().st_mtime_ns)
            except OSError:
                seen = (str(path), 0)
            if seen not in _warned:
                _warned.add(seen)
                log.error("%s is damaged (empty, cut off or not a config); using %s", path,
                          f"the copy from before the last save, {backup_path(path).name}" if data else "the defaults")
            data = data or {}

    defaults = copy.deepcopy(DEFAULTS)   # a copy: tidy() and the dashboard change the config in place

    def part(key):   # a section that isn't an object (null, a list) counts as missing
        return {**defaults[key], **data[key]} if isinstance(data.get(key), dict) else defaults[key]
    config = {**defaults, **data, "panel": part("panel"), "safe_area": part("safe_area"), "web": part("web")}
    if not isinstance(config["playlist"], list):
        config["playlist"] = []
    return config


def _write(path: Path, text: str) -> None:
    """Atomic and flushed to the card, so a power cut leaves the old file or the new one, never half."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save(config: dict, path: Path = DEFAULT_PATH) -> None:
    """Write the config, keeping the one it replaces as ``config.json.bak`` (only if that one reads fine)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(config, indent=2)
    if _read(path) is not None:
        try:
            _write(backup_path(path), path.read_text())
        except OSError as error:   # no backup is no reason not to save
            log.warning("couldn't keep a backup of %s: %s", path, error)
    _write(path, text)


def modules_off(config: dict) -> set[str]:
    """The modules switched off in Settings > Modules."""
    off = config.get("modules_off")
    return {m for m in off if isinstance(m, str)} if isinstance(off, list) else set()


def playlist(config: dict, modules: dict[str, Module]) -> list[Entry]:
    """Configured playlist, or one entry per module (default style) when empty. Screens of a module
    that's switched off are kept in the list but skipped, like a screen switched off on its own."""
    off = modules_off(config)
    entries = [Entry(**{**e, "enabled": e.get("enabled", True) and e["module"] not in off})
               for e in config["playlist"] if isinstance(e, dict)]   # (validate() reports anything else)
    return entries or [Entry(module=m) for m in list(modules) if m not in off]   # (a copy: an import may change it)


def safe_area(config: dict) -> SafeArea:
    return SafeArea.from_config(config)


def calendar_look(config: dict) -> dict:
    """The Calendar screens' colour coding, with defaults for anything not set."""
    look = config.get("calendar_colours")
    return {**DEFAULTS["calendar_colours"], **(look if isinstance(look, dict) else {})}


def context_factory(config: dict):
    """Modules get a canvas the size of the safe area, never the full panel."""
    tz = ZoneInfo(config["timezone"]) if config.get("timezone") else None
    area = safe_area(config)
    pet = pets.of(config)
    home = config.get("location") or None
    calendar = calendar_look(config)
    return lambda: Context(width=area.width, height=area.height, tz=tz, pet=pet, home=home, calendar=calendar)


KEYS = {"name", "timezone", "location", "panel", "safe_area", "web", "playlist", "splash", "sleep", "overlays", "update", "pet",
        "wifi_country", "modules_off", "calendar_colours", "settings_layout"}
OVERLAY_KEYS = {"module", "overlay", "every", "seconds", "offset", "settings", "enabled"}
PANEL = {"rows": (8, 128), "cols": (8, 256), "gpio_slowdown": (0, 5), "brightness": (1, 100),
         "chain_length": (1, 8), "parallel": (1, 3)}
ENTRY_KEYS = {"module", "style", "seconds", "settings", "enabled", "times"}
MOST_TIMES = 4   # times of day one playlist entry may have (Timing > When it plays)
_CLOCK = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


def _number(value, low, high, whole=False) -> bool:
    ok = isinstance(value, int) if whole else isinstance(value, (int, float))
    return ok and not isinstance(value, bool) and low <= value <= high


def _entry(where: str, raw, modules: dict[str, Module]) -> list[str]:
    """Problems with one playlist-style entry, checked on the raw JSON so nothing can crash later."""
    if not isinstance(raw, dict):
        return [f"{where}: must be an object"]
    if unknown := set(raw) - ENTRY_KEYS:
        return [f"{where}: unknown keys {sorted(unknown)}"]
    if raw.get("module") not in modules:
        return [f"{where}: unknown module {raw.get('module')!r}"]
    if "seconds" in raw and not _number(raw["seconds"], 1, 3600):
        return [f"{where}: seconds must be a number from 1 to 3600"]
    if not isinstance(raw.get("enabled", True), bool):
        return [f"{where}: enabled must be true or false"]
    if not isinstance(raw.get("settings", {}), dict) or not isinstance(raw.get("style") or "", str):
        return [f"{where}: settings must be an object and style a name"]
    if "times" in raw and (problem := _times(raw["times"])):
        return [f"{where}: {problem}"]
    try:
        modules[raw["module"]].settings_for(raw.get("style"), raw.get("settings", {}))
    except ValueError as error:
        return [f"{where}: {error}"]
    return []


def _times(raw) -> str | None:
    """What's wrong with an entry's times of day (None: nothing). Left out means all day, so a list is never empty."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MOST_TIMES:
        return f"times must be a list of 1 to {MOST_TIMES} times (leave it out to play all day)"
    for t in raw:
        if not isinstance(t, dict) or set(t) != {"from", "to"} or not all(
                isinstance(t[k], str) and _CLOCK.fullmatch(t[k]) for k in ("from", "to")):
            return 'each time needs "from" and "to" as HH:MM, like {"from": "08:00", "to": "10:00"}'
        if t["from"] == t["to"]:
            return f"a time can't start and end at {t['from']}: change one, or leave times out to play all day"
    return None


def _time(value) -> bool:
    try:
        dt.time.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


def _screen_files(rule, screen: str) -> list[str]:
    """v2's screen GIFs ship built in: point uploaded copies at the built-in ones. A built-in file only
    plays on the screen it was made for (your uploads play anywhere), so drop one on the wrong screen."""
    if not isinstance(rule, dict) or not isinstance(rule.get("sources"), list):
        return []
    notes, keep = [], []
    for name in rule["sources"]:
        twin = media.shipped(name)
        if twin and media.for_screen(twin, screen):   # else it's just your own file, for this screen too
            notes.append(f"{screen}: {name} is built in now, using {twin}")
            name = twin
        if not media.for_screen(name, screen):
            why = "made for another screen" if media.valid_name(name) else "not a media file"
            notes.append(f"{screen}: dropped {name} ({why})")
            continue
        if name not in keep:
            keep.append(name)
    if not keep and rule["sources"]:
        keep = [f"built-in/{'splash_loading_squares' if screen == 'splash' else 'sleep_default'}.gif"]
        notes.append(f"{screen}: nothing left, using {keep[0]}")
    rule["sources"] = keep
    boot = rule.get("boot")
    if boot is not None:
        twin = media.shipped(boot)
        twin = twin if twin and media.for_screen(twin, "splash") else None
        rule["boot"] = twin or (boot if media.for_screen(boot, "splash") else None)
        if rule["boot"] != boot:
            notes.append(f"{screen}: boot screen is now {rule['boot']}")
    return notes


def location_problems(home) -> list[str]:
    if home is None:
        return []
    ok = isinstance(home, dict) and isinstance(home.get("name"), str) and 0 < len(home["name"].strip()) <= 80 \
        and all(isinstance(home.get(k), (int, float)) and not isinstance(home.get(k), bool) for k in ("latitude", "longitude"))
    if not ok or not (-90 <= home["latitude"] <= 90 and -180 <= home["longitude"] <= 180):
        return ["location: pick a place from the search (a name, latitude -90 to 90 and longitude -180 to 180)"]
    return []


def _listed(config: dict, key: str) -> list:
    """The playlist or overlays as a list; a hand-edited ``true`` or ``5`` is left for validate() to report."""
    value = config.get(key)
    return value if isinstance(value, list) else []


def _adopt_location(config: dict, modules: dict[str, Module]) -> list[str]:
    """Before 14 Nov 2026 each weather or sky screen had its own location. With no Matrix location yet,
    take the one most of them use, and give the rest their own, so every screen shows the same place
    as before (and new ones use the Matrix's)."""
    if config.get("location") is not None:
        return []
    screens = []
    for key in ("playlist", "overlays"):
        for entry in _listed(config, key):
            module = modules.get(entry.get("module")) if isinstance(entry, dict) else None
            if module and module.places and isinstance(entry.get("settings", {}), dict) \
                    and not entry.get("settings", {}).get("own_location"):
                name, (lat, lon) = next(iter(module.places.items()))
                s = entry.get("settings") or {}
                screens.append((entry, module, (s.get(name, module.defaults[name]), s.get(lat, module.defaults[lat]),
                                                 s.get(lon, module.defaults[lon]))))
    if not screens:
        return []
    votes = [where for _, _, where in screens]
    name, lat, lon = max(votes, key=votes.count)   # ties: the first screen's
    config["location"] = {"name": name, "latitude": lat, "longitude": lon}
    notes = [f"location: the Matrix's location is now {name} (from its screens)"]
    for entry, module, where in screens:
        if where != (name, lat, lon):
            key, (klat, klon) = next(iter(module.places.items()))
            entry.setdefault("settings", {}).update({"own_location": True, key: where[0], klat: where[1], klon: where[2]})
            notes.append(f"{entry['module']}: keeps its own location, {where[0]}")
    return notes


def tidy(config: dict, modules: dict[str, Module]) -> list[str]:
    """Quietly update a config saved by an older Bongo: drop settings a module no longer has and
    choices it no longer offers (a renamed palette, say), so they fall back to the defaults instead
    of stopping that entry from playing and every dashboard save. Changes ``config`` in place and
    returns what it changed, for the log."""
    notes = []
    splash = config.get("splash")
    if isinstance(splash, dict) and "boot" not in splash:
        # Before 11 Nov 2026 the splash played only at start-up. It is a playlist item now (as in
        # v2), so put one at the top of the playlist: it still plays first, and then every pass.
        config["splash"] = splash = {**splash, "boot": None}
        playlist = config.get("playlist")
        if splash.get("enabled", True) and splash.get("sources") and isinstance(playlist, list) and playlist \
                and not any(isinstance(e, dict) and e.get("module") == "splash" for e in playlist):
            playlist.insert(0, {"module": "splash", "enabled": True})
            notes.append("splash: now a playlist item; added one at the top of the playlist")
    for screen in ("splash", "sleep"):
        notes += _screen_files(config.get(screen), screen)
    if all(isinstance(config.get(key) or [], list) for key in ("playlist", "overlays")):   # (else validate() says so)
        notes += pets.adopt(config)  # before the Pet entries lose their own name
    off = config.get("modules_off")
    if isinstance(off, list) and any(m not in modules for m in off):   # a module that's no longer installed
        config["modules_off"] = [m for m in off if m in modules]
        notes.append(f"modules_off: forgot {sorted(set(off) - set(modules))} (not installed)")
    notes += _adopt_location(config, modules)
    for key in ("playlist", "overlays"):  # entries of a module that moved into another one
        for i, entry in enumerate(_listed(config, key)):
            if not isinstance(entry, dict) or entry.get("module") in modules:
                continue
            for module in list(modules.values()):
                styles = module.renames.get(entry.get("module"))
                if styles is not None and entry.get("style") in styles:
                    old = f"{entry['module']}/{entry.get('style') or 'default'}"
                    entry["module"], entry["style"] = module.id, styles[entry.get("style")]
                    notes.append(f"{key}[{i}] {old} is now {module.id}/{entry['style']}")
                    break
    for key in ("playlist", "overlays"):
        for i, entry in enumerate(_listed(config, key)):
            if not isinstance(entry, dict) or entry.get("module") not in modules or not isinstance(entry.get("settings"), dict):
                continue
            module, overlay = modules[entry["module"]], key == "overlays"
            kinds = module.overlays if overlay else module.styles
            style = entry.get("style")
            if style is not None and style not in kinds:
                continue  # validate() reports it; nothing safe to guess
            cls = module._style(style, overlay)
            known = {**module.defaults, **cls.defaults}
            choices = {**module.choices, **cls.choices}
            limits = {**module.limits, **cls.limits}
            settings = entry["settings"]
            if module.migrate:
                notes += [f"{key}[{i}] {note}" for note in module.migrate(settings)]
            for name in list(settings):
                value, offered = settings[name], choices.get(name)
                if isinstance(offered, str):  # "media", "palettes": checked by their own rules
                    offered = None
                if name not in known:
                    del settings[name]
                    notes.append(f"{key}[{i}] {entry['module']}: dropped old setting {name!r}")
                elif name in limits and isinstance(value, (int, float)) and not isinstance(value, bool) \
                        and not limits[name][0] <= value <= limits[name][1]:   # NaN lands here too
                    low, high = limits[name]
                    if value != value:   # NaN: back to the default
                        del settings[name]
                    else:
                        settings[name] = int(min(high, max(low, value))) if isinstance(known[name], int) \
                            else min(high, max(low, value))
                    notes.append(f"{key}[{i}] {entry['module']}: {name}: {value} is outside {low} to {high}, "
                                 f"now {settings.get(name, 'the default')}")
                elif offered is not None and isinstance(value, list):
                    kept = [v for v in value if v in offered]
                    if kept != value:
                        notes.append(f"{key}[{i}] {entry['module']}: {name}: dropped {sorted(set(value) - set(kept))}")
                        if kept:
                            settings[name] = kept
                        else:
                            del settings[name]
                elif offered is not None and isinstance(value, str) and value not in offered:
                    del settings[name]
                    notes.append(f"{key}[{i}] {entry['module']}: {name}: {value!r} is gone, back to the default")
    return notes


def validate(config: dict, modules: dict[str, Module]) -> list[str]:
    """Everything that could stop the player, as readable messages (empty list = fine).
    A config that passes here must be playable: the dashboard saves only what passes."""
    problems = []
    if unknown := set(config) - KEYS:
        problems.append(f"unknown settings {sorted(unknown)}")
    country = config.get("wifi_country")
    if country is not None and not (isinstance(country, str) and len(country) == 2 and country.isalpha() and country.isupper()):
        problems.append("wifi_country must be a two-letter country code such as GB")
    problems += location_problems(config.get("location"))
    name = config.get("name")
    if name is not None and not (isinstance(name, str) and len(name) <= 60):   # (the dashboard's status page reads it as text)
        problems.append("name: the Matrix's name is text, up to 60 characters")
    if config.get("timezone"):
        try:
            ZoneInfo(config["timezone"])
        except (ValueError, KeyError, OSError, TypeError):
            problems.append(f"timezone: unknown time zone {config['timezone']!r}")
    panel = config.get("panel", {})
    if not isinstance(panel, dict):
        problems.append("panel: must be an object")
    else:
        for key, value in panel.items():
            if key == "hardware_mapping":
                if not isinstance(value, str):
                    problems.append("panel.hardware_mapping: must be text")
            elif key not in PANEL:
                problems.append(f"panel: unknown setting {key!r}")
            elif not _number(value, *PANEL[key], whole=True):
                problems.append(f"panel.{key}: must be a whole number from {PANEL[key][0]} to {PANEL[key][1]}")
    try:
        safe_area(config)
    except (ValueError, TypeError, AttributeError, KeyError) as error:
        problems.append(f"safe_area: {error}")
    entries = config.get("playlist", [])
    if not isinstance(entries, list):
        problems.append("playlist: must be a list")
        entries = []
    for i, raw in enumerate(entries):
        problems += _entry(f"playlist[{i}]", raw, modules)
    layout = config.get("settings_layout")   # Settings > Rearrange: which card goes where (the dashboard fills in the rest)
    if layout is not None and not (isinstance(layout, dict) and set(layout) <= {"top", "left", "right", "full"} and all(
            isinstance(v, list) and len(v) <= 40 and all(isinstance(x, str) and len(x) <= 40 for x in v) for v in layout.values())):
        problems.append('settings_layout: {"top", "left", "right", "full"}: lists of card names')
    look = config.get("calendar_colours")
    if look is not None and not (isinstance(look, dict) and set(look) <= {"code", "own", "holidays"}
                                 and isinstance(look.get("code", False), bool)
                                 and all(re.fullmatch(r"#[0-9A-Fa-f]{6}", str(look.get(k, "#000000"))) for k in ("own", "holidays"))):
        problems.append('calendar_colours: {"code": true or false, "own": "#RRGGBB", "holidays": "#RRGGBB"}')
    off = config.get("modules_off", [])
    if not (isinstance(off, list) and all(isinstance(m, str) for m in off)):
        problems.append("modules_off: a list of module names")
        off = []
    elif unknown := [m for m in off if m not in modules]:
        problems.append(f"modules_off: unknown modules {unknown}")
    if entries and not any(isinstance(e, dict) and e.get("enabled", True) is not False for e in entries):
        problems.append("playlist: turn at least one entry on")
    elif entries and not any(isinstance(e, dict) and e.get("enabled", True) is not False and e.get("module") not in off
                             for e in entries):
        problems.append("playlist: every screen that's on belongs to a module that's switched off; switch one back on "
                        "in Settings > Modules, or turn on another screen")
    elif not entries and len(off) and not any(m not in off for m in list(modules)):
        problems.append("modules_off: leave at least one module on")
    if config.get("splash"):
        problems += screen_problems("splash", config["splash"])
    from bongo.update import problems as update_problems
    problems += update_problems(config.get("update"))
    problems += pets.problems(config.get("pet"))
    overlays = config.get("overlays", [])
    if not isinstance(overlays, list):
        problems.append("overlays: must be a list")
        overlays = []
    for i, raw in enumerate(overlays):
        where = f"overlays[{i}]"
        if not isinstance(raw, dict) or set(raw) - OVERLAY_KEYS:
            problems.append(f"{where}: must be an object with {sorted(OVERLAY_KEYS)}")
            continue
        module = modules.get(raw.get("module"))
        if module is None:
            problems.append(f"{where}: unknown module {raw.get('module')!r}")
            continue
        for key, low, high in (("every", 0, 86400), ("seconds", 1, 86400), ("offset", 0, 86400)):
            if key in raw and not _number(raw[key], low, high):
                problems.append(f"{where}: {key} must be a number from {low} to {high}")
        if not isinstance(raw.get("enabled", True), bool) or not isinstance(raw.get("settings", {}), dict):
            problems.append(f"{where}: enabled must be true/false and settings an object")
            continue
        try:
            module.settings_for(raw.get("overlay"), raw.get("settings", {}), overlay=True)
        except ValueError as error:
            problems.append(f"{where}: {error}")
    sleep = config.get("sleep")
    if sleep:
        problems += screen_problems("sleep", sleep, sleep=True)
        if isinstance(sleep, dict):
            for key in ("from", "to"):
                if not _time(sleep.get(key)):
                    problems.append(f"sleep.{key}: use a time like 23:00")
            if sleep.get("enabled", True) is not False and _time(sleep.get("from")) and _time(sleep.get("to")) \
                    and dt.time.fromisoformat(sleep["from"]) == dt.time.fromisoformat(sleep["to"]):   # (else asleep all day;
                # switched off it's never asleep, so it can always be saved off)
                problems.append(f"sleep: a time can't start and end at {sleep['from']}: change one")
            if sleep.get("brightness") is not None and not _number(sleep["brightness"], 0, 100, whole=True):
                problems.append("sleep.brightness: must be a whole number from 0 to 100")
    return problems
