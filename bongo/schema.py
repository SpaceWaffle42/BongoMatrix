"""Settings schema: what each module's settings are, inferred from their defaults.

The dashboard builds every module's editor from ``describe(module)``, so modules need no UI
code. Types come from the default value; a module or style may add ``choices`` for settings
with a fixed set of values (alignments, palette names).
"""
from __future__ import annotations

import re

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


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


def check(name: str, default, value, choices=None) -> None:
    """Raise ValueError if ``value`` can't stand in for a setting whose default is ``default``."""
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
        allowed = choices if not isinstance(value, list) else None
        if allowed is not None and value not in allowed:
            raise ValueError(f"{name}: {value!r} is not one of {list(allowed)}")
        if isinstance(value, list) and any(v not in choices for v in value):
            raise ValueError(f"{name}: {[v for v in value if v not in choices]} not in {list(choices)}")
        return
    k = kind(default)
    ok = {
        "auto": True,
        "bool": isinstance(value, bool),
        "int": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "colour": isinstance(value, str) and bool(HEX.match(value)),
        "text": isinstance(value, str),
        "list": isinstance(value, list),
        "object": isinstance(value, dict),
    }.get(k, True)
    if not ok:
        expected = "a colour like #FFB547" if k == "colour" else f"a {k}"
        raise ValueError(f"{name}: expected {expected}, got {value!r}")


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
        **({"help": module.help} if module.help else {}),
        "default_style": module.default_style,
        "settings": fields(module.defaults, shared_choices, basic=module.basic),
        "styles": {name: {"settings": fields(cls.defaults, {**shared_choices, **cls.choices}, cls.swatches, cls.basic),
                          **({"group": cls.group} if cls.group else {})}
                   for name, cls in module.styles.items()},
    }
