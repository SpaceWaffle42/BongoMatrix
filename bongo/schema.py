"""Settings schema: what each module's settings are, inferred from their defaults.

The dashboard builds every module's editor from ``describe(module)``, so modules need no UI
code. Types come from the default value; a module or style may add ``choices`` for settings
with a fixed set of values (alignments, palette names).
"""
from __future__ import annotations

import math
import re

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_TEXT = 1000   # characters in one text setting: far more than a panel can show, short enough to draw fast


def kind(default) -> str:
    if default is None:
        return "auto"  # None means "work it out" (e.g. a baseline that follows the canvas)
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, int):
        return "int"
    if isinstance(default, float):
        return "number"
    if isinstance(default, str):
        return "colour" if HEX.match(default) else "text"
    if isinstance(default, list):
        return "list"
    if isinstance(default, dict):
        return "object"
    return "any"


def number(value) -> bool:
    """A real number: not True/False, and not NaN or infinity (which Python's JSON reader lets through)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


ACTION = re.compile(r"[a-z][a-z0-9_]{0,23}\Z")
MOST_ACTIONS, MAX_LABEL = 6, 20


def actions(value) -> dict:
    """A module's buttons (``Module.actions``), checked: {name: the words on the button}. Raises ValueError."""
    if not isinstance(value, dict):
        raise ValueError("actions must be {name: label}")
    if len(value) > MOST_ACTIONS:
        raise ValueError(f"at most {MOST_ACTIONS} actions")
    for name, label in value.items():
        if not isinstance(name, str) or not ACTION.match(name):
            raise ValueError(f"action name {name!r}: lower-case letters, digits and _ (up to 24), starting with a letter")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= MAX_LABEL or label != label.strip() or not label.isprintable():
            raise ValueError(f"action {name}: its label must be 1 to {MAX_LABEL} characters")
    return dict(value)


def check(name: str, default, value, choices=None, limits=None) -> None:
    """Raise ValueError if ``value`` can't stand in for a setting whose default is ``default``.
    ``limits``: (lowest, highest) for a number setting, from the module's or style's ``limits``."""
    if choices == "media":  # a file from the media library
        from bongo import media
        if not media.valid_name(value):
            raise ValueError(f"{name}: {value!r} isn't a media file; use built-in/<file> or uploaded/<file>")
        return
    if choices == "palettes":  # a module-specific list of objects, checked by the module's own check()
        if not isinstance(value, list):
            raise ValueError(f"{name}: expected a list")
        return
    if choices is not None:
        many = isinstance(default, list) or (default == "all" and "all" in choices)   # several can be picked (or "all")
        if isinstance(value, list):
            if not many:
                raise ValueError(f"{name}: pick one of {list(choices)}, not a list")
            if any(v not in choices for v in value):
                raise ValueError(f"{name}: {[v for v in value if v not in choices]} not in {list(choices)}")
        elif many and not (value == "all" and "all" in choices):
            raise ValueError(f"{name}: expected a list of {list(choices)}")
        elif value not in choices:
            raise ValueError(f"{name}: {value!r} is not one of {list(choices)}")
        return
    k = kind(default)
    ok = {
        "auto": value is None or number(value),   # worked out for you, or a number of your own
        "bool": isinstance(value, bool),
        "int": isinstance(value, int) and not isinstance(value, bool),
        "number": number(value),
        "colour": isinstance(value, str) and bool(HEX.match(value)),
        "text": isinstance(value, str) and len(value) <= MAX_TEXT,
        "list": isinstance(value, list),
        "object": isinstance(value, dict),
    }.get(k, True)
    if not ok:
        if k == "text" and isinstance(value, str):
            raise ValueError(f"{name}: keep it under {MAX_TEXT} characters (it has {len(value)})")
        expected = {"colour": "a colour like #FFB547", "auto": "a number, or nothing for automatic"}.get(k, f"a {k}")
        raise ValueError(f"{name}: expected {expected}, got {str(value)[:60]!r}")
    if limits and number(value) and not limits[0] <= value <= limits[1]:
        raise ValueError(f"{name}: {value} is outside {limits[0]} to {limits[1]}")


HELP_KEYS = {"title": str, "intro": str, "steps": list, "tips": list, "links": list,
             "required": bool}   # required: it shows nothing until this is done


def help_problems(help: dict) -> list[str]:
    """What's wrong with a module's ``help`` guide (empty: fine). Checked by the tests for every module."""
    problems = [f"help.{k}: unknown" for k in help if k not in HELP_KEYS]
    problems += [f"help.{k}: expected {t.__name__}" for k, t in HELP_KEYS.items() if k in help and not isinstance(help[k], t)]
    if not help.get("title") or not help.get("steps"):
        problems.append("help: needs a title and at least one step")
    for k in ("steps", "tips"):
        for line in help.get(k) if isinstance(help.get(k), list) else []:
            if not isinstance(line, str) or not line.strip() or line.count("`") % 2:
                problems.append(f"help.{k}: {line!r} must be text with `backticks` in pairs")
    for link in help.get("links") or []:
        ok = isinstance(link, dict) and isinstance(link.get("label"), str) and (
            str(link.get("url", "")).startswith("https://") or str(link.get("route", "")).startswith("/"))
        if not ok:
            problems.append(f"help.links: {link!r} needs a label and an https url or a /route")
    return problems


def describe(module) -> dict:
    """JSON-ready description of a module: shared settings and each style's own."""
    tiered = bool(module.basic or any(cls.basic for cls in module.styles.values()))

    def fields(defaults, choices, swatches=None, basic=()):
        swatches = swatches or {}

        def typed(n, d):
            if n in module.places:
                lat, lon = module.places[n]
                return {"type": "place", "latitude": lat, "longitude": lon}
            if n in module.suggestions:
                return {"type": kind(d), "suggestions": module.suggestions[n]}
            if n in module.item_fields:
                return {"type": "items", "fields": module.item_fields[n]}
            if choices.get(n) in ("media", "palettes"):
                return {"type": choices[n]}
            if n in choices:
                extra = {"swatches": swatches[n]} if n in swatches else {}
                return {"type": "choice", "choices": list(choices[n]), **extra, **module.choice_names.get(n, {})}
            return {"type": kind(d)}
        return [{"name": n, "default": d, **typed(n, d), "advanced": tiered and n not in basic}
                for n, d in defaults.items()]
    shared_choices = module.choices
    return {
        "id": module.id, "name": module.name, "description": module.description,
        **({"limits": {n: list(v) for n, v in module.limits.items()}} if module.limits else {}),   # [lowest, highest]
        **({"help": module.help} if module.help else {}),
        **({"actions": [{"name": n, "label": label} for n, label in module.actions.items()]} if module.actions else {}),
        "default_style": module.default_style,
        "settings": fields(module.defaults, shared_choices, basic=module.basic),
        "styles": {name: {**({"limits": {n: list(v) for n, v in cls.limits.items()}} if cls.limits else {}),
                          "settings": fields(cls.defaults, {**shared_choices, **cls.choices}, cls.swatches, cls.basic),
                          **({"group": cls.group} if cls.group else {})}
                   for name, cls in module.styles.items()},
    }
