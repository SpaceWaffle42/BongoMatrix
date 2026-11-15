"""The child process an imported module runs in. Started by ``remote.py`` (through ``jail.spawn``); everything
here runs before the module's own code is loaded, and the module never sees the Matrix's files or network.

It answers, over stdin/stdout (``wire.py``):
    {"op": "describe"}                                  -> the module's name, settings and styles
    {"op": "create", "style", "settings", "ctx"}        -> {"h": handle}
    {"op": "render", "h", "now", "hand_over_at"}                -> {"finished": bool, "busy": bool} + the frame's RGB bytes
    {"op": "close", "h"}  /  {"op": "bye"}
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import random
import sys
import traceback
from zoneinfo import ZoneInfo

from bongo.sandbox import wire

MOST_SCREENS = 24   # style instances kept at once; the oldest goes when another starts


def _plain(value):
    """Settings and choices as JSON can carry them (tuples become lists)."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"{type(value).__name__} isn't a setting a Matrix can save")


def describe(module) -> dict:
    """What the Matrix needs to list the module, check its settings and show its form, without running it."""
    from bongo.module import Module

    if not isinstance(module, Module):
        raise TypeError("MODULE is not a bongo.module.Module")
    if module.overlays:
        raise ValueError("imported modules can't have overlays yet")
    if not module.styles:
        raise ValueError("MODULE has no styles")
    styles = {}
    for name, cls in module.styles.items():
        styles[str(name)] = {"defaults": _plain(cls.defaults), "choices": _plain(cls.choices), "limits": _plain(cls.limits),
                             "swatches": _plain(cls.swatches), "basic": _plain(cls.basic), "group": cls.group,
                             "fps": max(1, min(60, int(cls.fps)))}
    from bongo.schema import actions

    return {"id": module.id, "name": module.name, "description": module.description, "defaults": _plain(module.defaults),
            "actions": actions(module.actions),
            "choices": _plain(module.choices), "limits": _plain(module.limits), "basic": _plain(module.basic),
            "item_fields": _plain(module.item_fields), "suggestions": _plain(module.suggestions),
            "choice_names": _plain(module.choice_names), "help": _plain(module.help), "styles": styles}


def load(folder: str, module_id: str):
    """The folder's ``MODULE``, imported as a package of its own (``bongo_ext_<id>``)."""
    name = f"bongo_ext_{module_id}"
    spec = importlib.util.spec_from_file_location(name, os.path.join(folder, "__init__.py"), submodule_search_locations=[folder])
    if spec is None or spec.loader is None:
        raise ImportError("no __init__.py")
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    module = getattr(package, "MODULE", None)
    if module is None:
        raise ImportError("__init__.py has no MODULE")
    if module.id != module_id:
        raise ValueError(f"MODULE.id is {module.id!r} but bongo-module.json says {module_id!r}")
    return module


def context(info: dict):
    from bongo.module import Context

    return Context(width=int(info.get("width", 64)), height=int(info.get("height", 32)),
                   tz=ZoneInfo(info["tz"]) if info.get("tz") else None, rng=random.Random(info.get("seed")),
                   home=info.get("home"), calendar=info.get("calendar"), preview=bool(info.get("preview")))


class _Numbered:
    """Each answer carries its request's number, so the Matrix can tell it's the answer to that request."""

    def __init__(self, out, n):
        self.out, self.n = out, n

    def send(self, header, body=None):
        wire.send(self.out, {**header, "n": self.n}, body)


def serve(module, stdin, out) -> None:
    screens: dict[int, object] = {}
    zones: dict[int, ZoneInfo | None] = {}
    count = 0
    while True:
        try:
            msg, _ = wire.recv(stdin)
        except wire.WireError:
            return
        op = msg.get("op")
        n = msg.get("n")
        stdout = _Numbered(out, n)
        try:
            if op == "bye":
                return
            if op == "describe":
                stdout.send({"ok": True, "module": describe(module)})
            elif op == "create":
                ctx = context(msg.get("ctx") or {})
                style = module.create(msg.get("style"), msg.get("settings") or {}, ctx)
                count += 1
                screens[count], zones[count] = style, ctx.tz
                while len(screens) > MOST_SCREENS:
                    oldest = next(iter(screens))
                    screens.pop(oldest), zones.pop(oldest)
                stdout.send({"ok": True, "h": count, "fps": style.fps})
            elif op == "render":
                style = screens[msg["h"]]
                now = dt.datetime.fromisoformat(msg["now"])
                if zones[msg["h"]] and now.tzinfo:
                    now = now.astimezone(zones[msg["h"]])
                ends = msg.get("hand_over_at")
                style.hand_over_at = float(ends) if isinstance(ends, (int, float)) and not isinstance(ends, bool) else None
                img = style.render(now)
                if img.size != (style.ctx.width, style.ctx.height):
                    raise ValueError(f"render gave a {img.size[0]}x{img.size[1]} frame, not {style.ctx.width}x{style.ctx.height}")
                try:
                    busy = bool(style.busy)
                except Exception:           # a broken ``busy`` just means the screen can hand over
                    busy = False
                stdout.send({"ok": True, "finished": bool(style.finished), "busy": busy}, img.convert("RGB").tobytes())
            elif op == "act":
                if msg.get("name") in module.actions:
                    screens[msg["h"]].act(msg["name"])
                stdout.send({"ok": True})
            elif op == "close":
                screens.pop(msg.get("h"), None), zones.pop(msg.get("h"), None)
                stdout.send({"ok": True})
            else:
                stdout.send({"ok": False, "error": f"unknown request {op!r}"})
        except Exception as error:  # noqa: BLE001 - the module's mistake goes back to the Matrix, which logs it
            traceback.print_exc(file=sys.stderr)
            stdout.send({"ok": False, "error": f"{type(error).__name__}: {error}"[:500]})


def main() -> None:
    # The wire keeps the real stdout; anything the module prints goes to stderr (the Matrix's log).
    wire_out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    stdin = sys.stdin.buffer
    spec, _ = wire.recv(stdin)
    from bongo.sandbox import jail

    try:
        jail.enter(spec)                       # its own user, files and network: before any of its code runs
        module = load(spec["dir"], spec.get("module_id", spec["id"]))
        wire.send(wire_out, {"ok": True, "module": describe(module), "enforced": jail.ENFORCED})
    except Exception as error:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        wire.send(wire_out, {"ok": False, "error": f"{type(error).__name__}: {error}"[:500]})
        return
    serve(module, stdin, wire_out)


if __name__ == "__main__":
    main()
