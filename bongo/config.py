"""Device configuration: panel options, timezone and the playlist (data/config.json)."""
from __future__ import annotations

import datetime as dt
import json
import os
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
    # played once at start-up (None: none). A splash rule without "boot" is from before 11 Nov 2026.
    "splash": {"sources": ["built-in/splash_loading_squares.gif"], "boot": None},
}


def load(path: Path = DEFAULT_PATH) -> dict:
    data = json.loads(Path(path).read_text()) if Path(path).exists() else {}
    config = {**DEFAULTS, **data, "panel": {**DEFAULTS["panel"], **data.get("panel", {})},
              "safe_area": {**DEFAULTS["safe_area"], **data.get("safe_area", {})},
              "web": {**DEFAULTS["web"], **data.get("web", {})}}
    return config


def save(config: dict, path: Path = DEFAULT_PATH) -> None:
    """Atomic write so a power cut never leaves half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, path)


def playlist(config: dict, modules: dict[str, Module]) -> list[Entry]:
    """Configured playlist, or one entry per module (default style) when empty."""
    entries = [Entry(**e) for e in config["playlist"]]
    return entries or [Entry(module=m) for m in modules]


def safe_area(config: dict) -> SafeArea:
    return SafeArea.from_config(config)


def context_factory(config: dict):
    """Modules get a canvas the size of the safe area, never the full panel."""
    tz = ZoneInfo(config["timezone"]) if config.get("timezone") else None
    area = safe_area(config)
    pet = pets.of(config)
    home = config.get("location") or None
    return lambda: Context(width=area.width, height=area.height, tz=tz, pet=pet, home=home)


KEYS = {"name", "timezone", "location", "panel", "safe_area", "web", "playlist", "splash", "sleep", "overlays", "update", "pet",
        "wifi_country"}
OVERLAY_KEYS = {"module", "overlay", "every", "seconds", "offset", "settings", "enabled"}
PANEL = {"rows": (8, 128), "cols": (8, 256), "gpio_slowdown": (0, 5), "brightness": (1, 100),
         "chain_length": (1, 8), "parallel": (1, 3)}
ENTRY_KEYS = {"module", "style", "seconds", "settings", "enabled"}


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
    try:
        modules[raw["module"]].settings_for(raw.get("style"), raw.get("settings", {}))
    except ValueError as error:
        return [f"{where}: {error}"]
    return []


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


def _adopt_location(config: dict, modules: dict[str, Module]) -> list[str]:
    """Before 14 Nov 2026 each weather or sky screen had its own location. With no Matrix location yet,
    take the one most of them use, and give the rest their own, so every screen shows the same place
    as before (and new ones use the Matrix's)."""
    if config.get("location") is not None:
        return []
    screens = []
    for key in ("playlist", "overlays"):
        for entry in config.get(key) or []:
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
    notes += pets.adopt(config)  # before the Pet entries lose their own name
    notes += _adopt_location(config, modules)
    for key in ("playlist", "overlays"):  # entries of a module that moved into another one
        for i, entry in enumerate(config.get(key) or []):
            if not isinstance(entry, dict) or entry.get("module") in modules:
                continue
            for module in modules.values():
                styles = module.renames.get(entry.get("module"))
                if styles is not None and entry.get("style") in styles:
                    old = f"{entry['module']}/{entry.get('style') or 'default'}"
                    entry["module"], entry["style"] = module.id, styles[entry.get("style")]
                    notes.append(f"{key}[{i}] {old} is now {module.id}/{entry['style']}")
                    break
    for key in ("playlist", "overlays"):
        for i, entry in enumerate(config.get(key) or []):
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
    if entries and not any(isinstance(e, dict) and e.get("enabled", True) is not False for e in entries):
        problems.append("playlist: turn at least one entry on")
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
            if sleep.get("brightness") is not None and not _number(sleep["brightness"], 0, 100, whole=True):
                problems.append("sleep.brightness: must be a whole number from 0 to 100")
    return problems
