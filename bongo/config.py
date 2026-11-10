"""Device configuration: panel options, timezone and the playlist (data/config.json)."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

from bongo.module import Context, Module
from bongo.player import Entry
from bongo.safe_area import SafeArea

DEFAULT_PATH = Path("data/config.json")

DEFAULTS = {
    "timezone": None,
    "panel": {"rows": 32, "cols": 64, "hardware_mapping": "adafruit-hat-pwm",
              "gpio_slowdown": 4, "brightness": 80},
    "safe_area": {"top": 0, "right": 0, "bottom": 0, "left": 0},
    "playlist": [],
}


def load(path: Path = DEFAULT_PATH) -> dict:
    data = json.loads(Path(path).read_text()) if Path(path).exists() else {}
    config = {**DEFAULTS, **data, "panel": {**DEFAULTS["panel"], **data.get("panel", {})},
              "safe_area": {**DEFAULTS["safe_area"], **data.get("safe_area", {})}}
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


def validate(config: dict, modules: dict[str, Module]) -> list[str]:
    """Problems with the playlist, as readable messages (empty list = fine)."""
    problems = []
    for i, entry in enumerate(playlist(config, modules)):
        try:
            if entry.module not in modules:
                raise ValueError(f"unknown module {entry.module!r}")
            modules[entry.module].settings_for(entry.style, entry.settings)
        except ValueError as error:
            problems.append(f"playlist[{i}]: {error}")
    return problems
