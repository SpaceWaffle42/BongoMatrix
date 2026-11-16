"""The Matrix's side of an imported module: a ``Module`` like any other (the playlist, the dashboard and
the player use it the usual way) whose styles ask the module's child process for every frame.

The module is described once, at install, and that description is kept (``addons.py``), so listing it,
checking its settings and showing its form never run its code. A child starts the first time one of its
screens does (playing, or a preview), and stops when the module is switched off or removed.
A child that hangs or crashes is stopped; its screen hands over to the next one, and the next screen of
that module starts it again (unless it keeps crashing: then it's left for a few minutes).
"""
from __future__ import annotations

import logging
import os
import select
import threading
import time
import weakref
from pathlib import Path

from PIL import Image

from bongo.module import Module, Style
from bongo.sandbox import jail, wire

log = logging.getLogger("bongo.modules")

START_WAIT = 20.0     # s for a child to start and load its module
CREATE_WAIT = 10.0    # s for a screen's setup
FRAME_WAIT = 2.0      # s for one frame
CRASHES = 3           # crashes within REST: left alone for REST
REST = 5 * 60


class ChildError(Exception):
    pass


class Busy(ChildError):
    """Another screen of the same module (a preview being set up, say) has its child just now: nothing is wrong."""


FRAME_LOCK_WAIT = 0.25  # s a frame waits for its child; busy longer, the screen keeps its last frame
IDLE = 10 * 60          # s with only previews asking and none for this long: the child is stopped (started again
                        # when next needed). One that has had a screen on the panel is kept: a cold start would hold
                        # up the panel each time round the playlist
WATCH_EVERY = 5         # s between looks at each child's memory and when it was last asked for anything


class _Reader:
    """Reads a child's stdout with a deadline (a hung module mustn't hang the panel)."""

    def __init__(self, fd: int):
        self.fd, self.deadline = fd, None

    def read(self, n: int) -> bytes:
        left = None if self.deadline is None else self.deadline - time.monotonic()
        if left is not None and left <= 0 or not select.select([self.fd], [], [], left)[0]:
            raise TimeoutError("the module took too long")
        return os.read(self.fd, n)


class _Writer:
    """Writes to a child's stdin with the same deadline (a module that stops reading mustn't hang it either)."""

    def __init__(self, fd: int, reader: _Reader):
        self.fd, self.reader = fd, reader
        os.set_blocking(fd, False)

    def write(self, data: bytes):
        view = memoryview(data)
        while view:
            left = None if self.reader.deadline is None else self.reader.deadline - time.monotonic()
            if left is not None and left <= 0 or not select.select([], [self.fd], [], left)[1]:
                raise TimeoutError("the module stopped listening")
            try:
                view = view[os.write(self.fd, view):]
            except BlockingIOError:
                continue

    def flush(self):
        pass


MOST_LINES = 20       # lines of a module's output logged per 10 s (the rest is skipped)
MOST_MEMORY = 300 << 20


class Child:
    """One running module: its process, and one request at a time."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.lock = threading.Lock()
        self.proc = None
        self.generation = 0
        self.enforced = False
        self.crashes: list[float] = []
        self.error = ""
        self.retired = False      # replaced or stopped from outside: it never starts again (its screens hand over)
        self.asked = 0            # each request's number; its answer must carry it
        self.used = time.monotonic()   # when a screen or preview last asked it for anything (``IDLE``)
        self.on_panel = False          # it has drawn a screen for the panel, not only previews

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _start(self):
        if self.retired:
            raise ChildError("stopped")
        recent = [t for t in self.crashes if time.monotonic() - t < REST]
        if len(recent) >= CRASHES:
            raise ChildError(f"stopped after crashing {CRASHES} times: {self.error}")
        jail.kill_user(self.spec["uid"])          # anything left of an earlier run
        spec = {**self.spec, "stage": str(jail.run_dir() / f"stage-{self.spec['id']}")}
        if self.spec.get("net") == "gate":
            from bongo.sandbox.gate import gate
            g = gate()
            g.allow(self.spec["id"], self.spec.get("sites", []), self.spec["uid"])
            spec["gate"] = str(g.path)
        if jail.can_shut_in():
            Path(spec["stage"]).mkdir(mode=0o700, exist_ok=True)
        self.proc = jail.spawn(spec)
        self.reader = _Reader(self.proc.stdout.fileno())
        self.writer = _Writer(self.proc.stdin.fileno(), self.reader)
        self.generation += 1
        threading.Thread(target=self._log, args=(self.proc,), daemon=True, name=f"module-{self.spec['id']}").start()
        _watch(self)
        header, _ = self._exchange(None, START_WAIT)
        if not header.get("ok"):
            self._crashed(header.get("error", "it didn't start"))
            raise ChildError(header.get("error", "it didn't start"))
        self.enforced = bool(header.get("enforced"))
        self.described = header.get("module")

    def _log(self, proc):
        """What the module prints, a line at a time (long lines cut, and not too many)."""
        fd, buf, window, count = proc.stderr.fileno(), b"", time.monotonic(), 0
        while True:
            try:
                part = os.read(fd, 4096)
            except OSError:
                return
            if not part:
                return
            buf += part
            lines = buf.split(b"\n")
            buf = lines.pop()
            if len(buf) > 500:                    # a line with no end: keep only its start
                lines.append(buf[:500])
                buf = b""
            for line in lines:
                if time.monotonic() - window > 10:
                    window, count = time.monotonic(), 0
                count += 1
                if count <= MOST_LINES:
                    log.info("[%s] %s", self.spec["id"], line[:500].decode("utf-8", "replace").rstrip())
                elif count == MOST_LINES + 1:
                    log.info("[%s] (more output skipped)", self.spec["id"])

    def _exchange(self, msg, wait, most_body=wire.MOST_BODY):
        self.reader.deadline = time.monotonic() + wait
        try:
            if msg is not None:
                self.asked += 1
                msg = {**msg, "n": self.asked}
                wire.send(self.writer, msg)
            header, body = wire.recv(self.reader, most_body)
            if msg is not None and header.get("n") != self.asked:
                raise ValueError("an answer out of turn")
            return header, body
        except (TimeoutError, wire.WireError, OSError, ValueError) as error:
            self._crashed(f"{error}")
            raise ChildError(str(error)) from None

    def _crashed(self, why: str):
        self.error = why
        self.crashes = [t for t in self.crashes if time.monotonic() - t < REST] + [time.monotonic()]
        log.warning("module %s stopped: %s", self.spec["id"], why)
        self.stop()

    def call(self, msg: dict, wait: float, body: bool = False, most_body: int = wire.MOST_BODY, lock_wait: float | None = None):
        if not self.lock.acquire(timeout=wait + 1 if lock_wait is None else lock_wait):
            raise Busy("busy")
        self.used = time.monotonic()
        try:
            if not self.alive():
                self._start()
            header, data = self._exchange(msg, wait, most_body)
            if not header.get("ok"):
                raise ChildError(header.get("error", "failed"))
            return (header, data) if body else header
        finally:
            self.lock.release()

    def describe(self) -> dict:
        with self.lock:
            self.used = time.monotonic()
            if not self.alive():
                self._start()
            return self.described

    def close(self, handle: int, generation: int):
        if generation != self.generation or not self.alive() or not self.lock.acquire(timeout=0.5):
            return
        try:
            self._exchange({"op": "close", "h": handle}, 1.0)
        except ChildError:
            pass
        finally:
            self.lock.release()

    def stop(self):
        """End it at once (and everything it started): never waits on the module."""
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.kill()
        except OSError:
            pass
        jail.kill_user(self.spec["uid"])
        try:
            proc.wait(2.0)
        except Exception:  # noqa: BLE001 - it's been killed; the kernel tidies up
            pass
        for f in (proc.stdin, proc.stdout):
            try:
                f.close()
            except OSError:
                pass


_watched: list[Child] = []
_watch_lock = threading.Lock()


def _watch(c: Child):
    """Every few seconds: a module using too much memory (all its processes together) is stopped, and so is one
    started only for previews that nobody has asked for anything for a while, so it isn't left using the CPU."""
    with _watch_lock:
        _watched[:] = [w for w in _watched if w.alive() and w is not c] + [c]
        if len(_watched) > 1:
            return

    def run():
        while True:
            time.sleep(WATCH_EVERY)
            with _watch_lock:
                now = [w for w in _watched if w.alive()]
                _watched[:] = now
            if not now:
                with _watch_lock:
                    if not _watched:
                        return
                continue
            for w in now:
                if not w.on_panel and time.monotonic() - w.used > IDLE and w.lock.acquire(blocking=False):
                    try:                             # (never while a screen is asking it something)
                        if time.monotonic() - w.used > IDLE:
                            log.info("module %s: not used for a while, stopped until it's next needed", w.spec["id"])
                            w.stop()
                    finally:
                        w.lock.release()
                    continue
                used = jail.memory_of(w.spec["uid"])
                if used > MOST_MEMORY:
                    w.error = f"used too much memory ({used >> 20} MB)"
                    log.warning("module %s stopped: %s", w.spec["id"], w.error)
                    w.crashes.append(time.monotonic())
                    proc = w.proc
                    if proc is not None:
                        try:
                            proc.kill()
                        except OSError:
                            pass
                    jail.kill_user(w.spec["uid"])
    threading.Thread(target=run, daemon=True, name="module-memory").start()


CHILDREN: dict[str, Child] = {}
_children_lock = threading.Lock()


def child(spec: dict) -> Child:
    with _children_lock:
        c = CHILDREN.get(spec["id"])
        if c is None or c.spec != spec:
            if c is not None:
                c.retired = True
                with c.lock:
                    c.stop()
            c = CHILDREN[spec["id"]] = Child(spec)
        return c


def stop(module_id: str):
    with _children_lock:
        c = CHILDREN.pop(module_id, None)
    if c:
        c.retired = True
        with c.lock:
            c.stop()


def stop_all():
    for module_id in list(CHILDREN):
        stop(module_id)


class RemoteStyle(Style):
    """A style of an imported module: each frame comes from its child."""
    spec: dict = {}
    style_name: str = ""

    def setup(self):
        self.child = child(self.spec)
        self.blank = self.ctx.canvas()
        self.info = {"width": self.ctx.width, "height": self.ctx.height, "tz": getattr(self.ctx.tz, "key", None),
                     "seed": self.ctx.rng.getrandbits(32), "home": self.ctx.home, "calendar": self.ctx.calendar,
                     "preview": self.ctx.preview}
        self._create()

    def _create(self):
        self.handle = None
        if not self.ctx.preview:
            self.child.on_panel = True
        try:
            header = self.child.call({"op": "create", "style": self.style_name, "settings": self.settings, "ctx": self.info}, CREATE_WAIT)
            self.handle, self.generation = header["h"], self.child.generation
            weakref.finalize(self, self.child.close, self.handle, self.generation)
        except ChildError as error:
            log.warning("module %s, %s: couldn't start: %s", self.spec["id"], self.style_name, error)
            self.finished = True

    most_overrun = 60.0   # s an imported screen may stay past its time while busy (a built-in one: player.OVERRUN)

    def act(self, name):
        if self.handle is None or self.generation != self.child.generation:
            return
        try:
            self.child.call({"op": "act", "h": self.handle, "name": name}, FRAME_WAIT)
        except ChildError as error:
            log.warning("module %s, %s: the %s button: %s", self.spec["id"], self.style_name, name, error)

    def render(self, now):
        if self.handle is not None and (self.generation != self.child.generation or not self.child.alive()):
            self._create()          # its child was restarted (switched back on, a setting changed): start again there
        if self.handle is None:
            self.finished = True
            return self.blank
        try:
            header, data = self.child.call({"op": "render", "h": self.handle, "now": now.isoformat(), "hand_over_at": self.hand_over_at}, FRAME_WAIT, body=True,
                                           most_body=self.ctx.width * self.ctx.height * 3, lock_wait=FRAME_LOCK_WAIT)
            if data is None or len(data) != self.ctx.width * self.ctx.height * 3:
                raise ChildError("a frame of the wrong size")
            self.finished, self.busy = bool(header.get("finished")), bool(header.get("busy"))
            self.last = Image.frombytes("RGB", (self.ctx.width, self.ctx.height), data)
            return self.last
        except Busy:
            # another screen of this module has its child (a preview being set up): this one is fine, it shows
            # its last frame again and asks next frame, rather than going black and ending
            return getattr(self, "last", self.blank)
        except ChildError as error:
            log.warning("module %s, %s: %s", self.spec["id"], self.style_name, error)
            self.handle = None
            self.finished = True
            return self.blank


def build(described: dict, spec: dict) -> Module:
    """A Module from a module's description (made at install) whose styles run in its child."""
    styles = {}
    for name, s in described["styles"].items():
        styles[name] = type(f"Remote_{described['id']}_{name}", (RemoteStyle,), {
            "spec": spec, "style_name": name, "defaults": s.get("defaults") or {}, "choices": s.get("choices") or {},
            "limits": {k: tuple(v) for k, v in (s.get("limits") or {}).items()}, "swatches": s.get("swatches") or {},
            "basic": tuple(s.get("basic") or ()), "group": s.get("group"), "fps": int(s.get("fps") or 10)})
    return Module(id=described["id"], name=described["name"], styles=styles, defaults=described.get("defaults") or {},
                  description=described.get("description") or "", choices=described.get("choices") or {},
                  limits={k: tuple(v) for k, v in (described.get("limits") or {}).items()},
                  basic=tuple(described.get("basic") or ()), item_fields=described.get("item_fields") or {},
                  suggestions=described.get("suggestions") or {}, choice_names=described.get("choice_names") or {},
                  help=described.get("help") or None, actions=described.get("actions") or {})
