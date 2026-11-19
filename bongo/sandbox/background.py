"""The Matrix's side of an imported module's background task (``bongo.module.Background``): for each module
that asked for one, whose owner agreed, and that is switched on, a thread here asks the module's child process
for a ``tick`` every few seconds.

It has nothing to do with the player: no frame ever waits for a tick. The tick runs in the module's own process
(the same one that draws its screens, shut in as before), under the same limits. One that takes longer than
``TICK_WAIT`` is a hang: the process is stopped (``remote.Child``) and started again after a rest that grows
(``BACKOFF``), and it is said in the log once, not every time. Switched off or removed, the thread ends and the
process is stopped: off still means not loaded or run at all.

What comes back from a tick is small and checked here: a status line (bongo/abilities.py) and what it asks of
the panel (bongo/controls.py).
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from zoneinfo import ZoneInfo

from bongo.sandbox import remote

log = logging.getLogger("bongo.modules")

TICK_WAIT = 5.0                       # s for one tick (the first one starts the task too)
BACKOFF = (5, 30, 300, 300, 1800)     # s before trying again after 1, 2, 3... failures in a row
FIRST = 0.2                           # s before a task's first tick
STOP_WAIT = 2.0                       # s a task's ``stop`` may take (switched off, removed, the Matrix stopping)

RUNNERS: dict[str, "Runner"] = {}
CONTROLS = None                       # bongo.controls.Controls, once the display has one (``attach``)
_lock = threading.Lock()


_details: dict = {"at": None, "by_id": {}}


def details(module_id: str):
    """(how its process is started, the controls it was given, all it was allowed) for a module with a background task, as
    installed now; None if it has none. Read again only when the list of installed modules has changed, so a
    change there (another version, its sites switch) is what the very next tick goes by."""
    from bongo import addons

    path = addons.state_file()
    try:
        st = path.stat()
        at = (str(path), st.st_mtime_ns, st.st_ino, st.st_size)   # (each save is a new file put in its place)
    except OSError:
        at = (str(path), None)
    with _lock:
        if _details["at"] != at:
            _details["by_id"] = {
                k: (addons.spec_for(k, info), tuple(info["abilities"].get("controls", ())), info["abilities"])
                for k, info in addons.installed().items()
                if (info.get("abilities") or {}).get("background") and (info.get("described") or {}).get("background")}
            _details["at"] = at
        return _details["by_id"].get(module_id)


class Runner(threading.Thread):
    """One module's background task, as seen from the Matrix."""

    def __init__(self, module_id: str, zone: str | None):
        super().__init__(daemon=True, name=f"background-{module_id}")
        self.module_id, self.zone = module_id, zone
        self.gone = threading.Event()
        self.fails = 0
        self.ticks = 0
        self.refused: list = []
        self.child = None             # the module's process, as last asked

    def run(self):
        from bongo.abilities import STORE

        wait = FIRST
        while not self.gone.wait(wait):
            try:
                wait = self.tick()
                if self.fails and not self.gone.is_set():
                    log.info("module %s: its background task is running again", self.module_id)
                    self.fails = 0
            except remote.Busy:               # one of its screens has its process just now: next time
                wait = 1.0
            except Exception as error:  # noqa: BLE001 - whatever a module does, this thread carries on
                if self.gone.is_set():
                    return
                self.fails += 1
                wait = BACKOFF[min(self.fails, len(BACKOFF)) - 1]
                why = str(error) if isinstance(error, remote.ChildError) else f"{type(error).__name__}: {error}"
                if self.fails == 1:           # said once; "running again" when it is
                    # (a process that hung or died has been said, as a warning, where it was stopped: remote.Child)
                    ended = isinstance(error, remote.ChildError) and self.child is not None and not self.child.alive()
                    log.log(logging.INFO if ended else logging.WARNING,
                            "module %s: its background task stopped (%s); it will be tried again, less and less often",
                            self.module_id, why[:300], exc_info=None if isinstance(error, remote.ChildError) else error)
                STORE.say(self.module_id, f"Not running: {why}", ok=False, by="matrix")

    def tick(self) -> float:
        from bongo.abilities import STORE

        known = details(self.module_id)
        with _lock:                           # (so it can't start a process for a module just switched off)
            if RUNNERS.get(self.module_id) is not self or known is None:
                self.gone.set()
                return 0.0
            spec, allowed, asked = known
            child = remote.child(spec)
            child.kept = True                 # (its ticks may be an hour apart: that isn't being idle)
            self.child = child
        now = dt.datetime.now(ZoneInfo(self.zone)) if self.zone else dt.datetime.now().astimezone()
        msg = {"op": "tick", "now": now.isoformat(), "settings": STORE.values(self.module_id, asked), "refused": self.refused}
        if "status" in allowed and CONTROLS is not None:
            msg["panel"] = CONTROLS.status()
        header = child.call(msg, TICK_WAIT, most_body=0)   # (an answer to a tick is a few words, never a frame)
        self.ticks += 1
        if self.gone.is_set():
            return 0.0
        said = header.get("status")
        if isinstance(said, dict):
            STORE.say(self.module_id, said.get("text", ""), ok=said.get("ok") is not False)
        elif (STORE.status(self.module_id) or {}).get("by") == "matrix":
            STORE.say(self.module_id, "")     # it's running again and has nothing to say: "Not running" goes
        asked = header.get("commands") or []
        self.refused = CONTROLS.apply(self.module_id, allowed, asked) if CONTROLS is not None and asked else []
        try:
            return max(1.0, min(3600.0, float(header.get("every", 5))))
        except (TypeError, ValueError):
            return 5.0


def attach(live, modules: dict, config) -> None:
    """Give background tasks the panel's controls (``config()``: the saved config, read when one is needed)."""
    global CONTROLS
    from bongo.controls import Controls
    CONTROLS = Controls(live, modules, config)


def sync(config: dict | None = None) -> None:
    """Make the running background tasks match what is installed and switched on: start the ones that should
    be running and stop (process and all) the ones that shouldn't."""
    from bongo import addons, config as cfg
    from bongo.abilities import STORE

    off = cfg.modules_off(config or {})
    zone = (config or {}).get("timezone") or None
    wanted = {k for k, info in addons.installed().items()
              if (info.get("abilities") or {}).get("background") and (info.get("described") or {}).get("background") and k not in off}
    stopped = []
    with _lock:
        for module_id in list(RUNNERS):
            if module_id not in wanted:
                RUNNERS.pop(module_id).gone.set()
                stopped.append(module_id)
        for module_id in wanted:
            runner = RUNNERS.get(module_id)
            if runner is None or not runner.is_alive():
                runner = RUNNERS[module_id] = Runner(module_id, zone)
                runner.start()
            else:
                runner.zone = zone
    for module_id in stopped:
        goodbye(module_id)
        remote.stop(module_id)
        STORE.say(module_id, "")


def goodbye(module_id: str) -> None:
    """Its task's ``stop`` (a last word to whatever it is connected to), if its process is running: at most
    ``STOP_WAIT`` seconds. One that takes longer is cut off (its process is stopped). Never starts a process."""
    c = remote.CHILDREN.get(module_id)
    if c is None or not c.lock.acquire(timeout=STOP_WAIT):   # (a tick under way may finish first)
        return
    try:
        if c.alive():
            c._exchange({"op": "stop"}, STOP_WAIT)
    except remote.ChildError:
        pass                                 # (hung or crashed: _exchange has stopped it, and said so once)
    finally:
        c.lock.release()


def drop(module_id: str) -> None:
    """Its background task is no longer asked for anything (while another version of it is put in place, or it
    is removed); ``sync`` starts it again if it should run. Its ``stop`` is called first."""
    with _lock:
        runner = RUNNERS.pop(module_id, None)
    if runner is not None:
        runner.gone.set()
        goodbye(module_id)


def stop_all() -> None:
    """The Matrix is stopping: every task's ``stop`` at once (all together within ``STOP_WAIT``), then every process."""
    with _lock:
        gone = list(RUNNERS.values())
        RUNNERS.clear()
    for runner in gone:
        runner.gone.set()
    said = [threading.Thread(target=goodbye, args=(r.module_id,), daemon=True) for r in gone]
    for t in said:
        t.start()
    for t in said:
        t.join(STOP_WAIT + 1.5)
    for runner in gone:
        remote.stop(runner.module_id)
