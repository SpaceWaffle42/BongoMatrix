"""The child process an imported module runs in. Started by ``remote.py`` (through ``jail.spawn``); everything
here runs before the module's own code is loaded, and the module never sees the Matrix's files or network.

It answers, over stdin/stdout (``wire.py``):
    {"op": "describe"}                                  -> the module's name, settings and styles
    {"op": "create", "style", "settings", "ctx"}        -> {"h": handle}
    {"op": "render", "h", "now", "hand_over_at"}                -> {"finished": bool, "busy": bool} + the frame's RGB bytes
    {"op": "tick", "now", "settings", "panel", "refused"}       -> {"every", "status", "commands"}: its background task
    {"op": "close", "h"}  /  {"op": "bye"}
(A render's answer also carries "stay": bool, the screen's ``Style.stay``: something is live, keep the panel.)
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import random
import socket
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
    background = getattr(module, "background", None)
    if background is not None:
        from bongo.module import Background
        if not (isinstance(background, type) and issubclass(background, Background)):
            raise TypeError("MODULE.background is not a bongo.module.Background class")
    if not module.styles and background is None:
        raise ValueError("MODULE has no styles")
    styles = {}
    for name, cls in module.styles.items():
        styles[str(name)] = {"defaults": _plain(cls.defaults), "choices": _plain(cls.choices), "limits": _plain(cls.limits),
                             "swatches": _plain(cls.swatches), "basic": _plain(cls.basic), "group": cls.group,
                             "fps": max(1, min(60, int(cls.fps))),
                             "secrets": [str(n) for n in getattr(cls, "secrets", ())]}   # (never put in a backup)
    from bongo.schema import actions

    return {"id": module.id, "name": module.name, "description": module.description, "defaults": _plain(module.defaults),
            "notes": _plain(getattr(module, "notes", {})),   # (the line under a setting, Module.notes)
            "actions": actions(module.actions),
            "choices": _plain(module.choices), "limits": _plain(module.limits), "basic": _plain(module.basic),
            "item_fields": _plain(module.item_fields), "suggestions": _plain(module.suggestions),
            "choice_names": _plain(module.choice_names), "help": _plain(module.help), "styles": styles,
            "secrets": [str(n) for n in getattr(module, "secrets", ())],
            "background": None if background is None else {"every": _every(background.every)}}


def _every(value) -> float:
    """Seconds between a background task's ticks: 1 to 3600 (5 if it isn't a number)."""
    try:
        return max(1.0, min(3600.0, float(value)))
    except (TypeError, ValueError):
        return 5.0


SPEC: dict | None = None      # how this child was started (``main``)
SETTINGS: dict = {}           # the module's own settings, as last given (with a screen, or a tick)


def _failed(status: bytes, why: str) -> OSError:
    """The exception for one way of failing to reach the home-network address (gate.HOME_FAILS). Its words
    (``str(error)``) are exactly what the module's card says; ``errno`` is set too, for code that looks."""
    import errno
    kind, number = {b"403": (PermissionError, errno.EACCES),             # the Matrix won't dial it
                    b"429": (PermissionError, errno.EAGAIN),             # ...not now: too many tries
                    b"404": (socket.gaierror, None),                       # the name can't be found
                    b"502": (ConnectionRefusedError, errno.ECONNREFUSED),  # the device said no on that port
                    b"503": (OSError, errno.EHOSTUNREACH),                 # no route to it
                    b"504": (TimeoutError, errno.ETIMEDOUT)}.get(status, (ConnectionError, None))
    error = kind(why)
    if number is not None:
        error.errno = number
    return error


def home_connect(timeout: float = 5.0):
    """``bongo.module.home_connect``: a socket to the module's one home-network address. Through the gate, which
    dials the address in the module's settings and nothing else; the module never says where to."""
    if SPEC is None:
        raise PermissionError("only an imported module has a home-network address")
    if not SPEC.get("gate"):   # its owner unticked "Only allow the sites it lists": it has the Matrix's network
        host, port = SETTINGS.get("address"), SETTINGS.get("port")
        if not host or not port:
            raise PermissionError("no home-network address is set in its settings")
        return socket.create_connection((host, int(port)), timeout=timeout)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout + 2)                       # (the gate gives up at ``timeout`` and says so)
        s.connect(SPEC["gate"])
        s.sendall(f"BONGO {SPEC['id']}\nHOME / BONGO/1\r\nWait: {float(timeout):g}\r\n\r\n".encode())
        head = b""
        while not head.endswith(b"\r\n\r\n"):   # a byte at a time: what follows belongs to the far end
            part = s.recv(1)
            if not part or len(head) > 1024:
                raise ConnectionError("the Matrix closed the connection")
            head += part
        if head.split(b" ", 2)[1:2] != [b"200"]:
            why = b""
            while len(why) < 500 and (part := s.recv(500)):
                why += part
            raise _failed(head.split(b" ", 2)[1], why.decode("utf-8", "replace").strip() or "refused")
        s.settimeout(None)
        return s
    except BaseException:
        s.close()
        raise


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

    global SETTINGS
    if isinstance(info.get("module_settings"), dict):
        SETTINGS = info["module_settings"]
    return Context(width=int(info.get("width", 64)), height=int(info.get("height", 32)),
                   tz=ZoneInfo(info["tz"]) if info.get("tz") else None, rng=random.Random(info.get("seed")),
                   home=info.get("home"), calendar=info.get("calendar"), preview=bool(info.get("preview")),
                   module_settings=dict(SETTINGS))


class _Task:
    """The module's background task (``Module.background``), made at its first tick."""

    def __init__(self, module):
        self.module, self.task, self.panel, self.given = module, None, None, None

    def stop(self) -> None:
        """Its ``Background.stop``, once, if it was started (asks of the panel made there go nowhere)."""
        task, self.task = self.task, None
        if task is not None:
            task.stop()

    def tick(self, msg: dict) -> dict:
        global SETTINGS
        from bongo.module import Panel

        if self.module.background is None:
            raise ValueError("MODULE has no background task")
        settings = msg.get("settings") if isinstance(msg.get("settings"), dict) else {}
        changed, self.given = settings != self.given, settings   # (not SETTINGS: a screen may have been handed the new ones first)
        SETTINGS = settings
        if self.task is None:
            panel = Panel()
            task = self.module.background(dict(settings), panel)
            task.start()
            self.task, self.panel = task, panel       # (only once it has started: one that fails is tried again)
        elif changed:
            self.task.settings = dict(settings)
            self.task.settings_changed()
        task, panel = self.task, self.panel
        panel.status, panel.refused = msg.get("panel"), list(msg.get("refused") or [])
        now = dt.datetime.fromisoformat(msg["now"])
        try:
            task.tick(now)
        finally:
            asked, panel._asked = panel._asked, []    # what it asked for before failing still counts for nothing
        status = getattr(task, "_status", None)
        return {"ok": True, "every": _every(task.every), "status": status if isinstance(status, dict) else None,
                "commands": _plain(asked)}


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
    task = _Task(module)
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
                try:
                    stay = bool(getattr(style, "stay", False))   # (Style.stay: something is live, keep the panel)
                except Exception:
                    stay = False
                stdout.send({"ok": True, "finished": bool(style.finished), "busy": busy, "stay": stay}, img.convert("RGB").tobytes())
            elif op == "act":
                if msg.get("name") in module.actions:
                    screens[msg["h"]].act(msg["name"])
                stdout.send({"ok": True})
            elif op == "tick":
                stdout.send(task.tick(msg))
            elif op == "stop":
                task.stop()
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
    global SPEC
    # The wire keeps the real stdout; anything the module prints goes to stderr (the Matrix's log).
    wire_out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    stdin = sys.stdin.buffer
    spec, _ = wire.recv(stdin)
    from bongo.sandbox import jail
    SPEC = spec

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
