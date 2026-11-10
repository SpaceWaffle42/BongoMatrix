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


def describe(module) -> dict:
    """JSON-ready description of a module: shared settings and each style's own."""
    def fields(defaults, choices):
        return [{"name": n, "type": "choice" if n in choices else kind(d), "default": d,
                 **({"choices": list(choices[n])} if n in choices else {})}
                for n, d in defaults.items()]
    shared_choices = module.choices
    return {
        "id": module.id, "name": module.name, "description": module.description,
        "default_style": module.default_style,
        "settings": fields(module.defaults, shared_choices),
        "styles": {name: {"settings": fields(cls.defaults, {**shared_choices, **cls.choices})}
                   for name, cls in module.styles.items()},
    }
