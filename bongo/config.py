"""Device configuration: panel options, timezone and the playlist (data/config.json)."""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

from bongo.module import Context, Module
from bongo.player import Entry
from bongo.safe_area import SafeArea
from bongo.schedule import screen_problems

DEFAULT_PATH = Path("data/config.json")

DEFAULTS = {
    "timezone": None,
    "panel": {"rows": 32, "cols": 64, "hardware_mapping": "adafruit-hat-pwm",
              "gpio_slowdown": 4, "brightness": 80},
    "safe_area": {"top": 0, "right": 0, "bottom": 0, "left": 0},
    "web": {"enabled": True, "host": "0.0.0.0", "port": 5000},
    "playlist": [],
    "splash": {"sources": ["built-in/splash_loading_squares.gif"]},  # v2's default start-up screen
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
    return lambda: Context(width=area.width, height=area.height, tz=tz)


KEYS = {"name", "timezone", "panel", "safe_area", "web", "playlist", "splash", "sleep", "overlays"}
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


def validate(config: dict, modules: dict[str, Module]) -> list[str]:
    """Everything that could stop the player, as readable messages (empty list = fine).
    A config that passes here must be playable: the dashboard saves only what passes."""
    problems = []
    if unknown := set(config) - KEYS:
        problems.append(f"unknown settings {sorted(unknown)}")
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
