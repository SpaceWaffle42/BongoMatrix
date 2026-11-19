"""The panel controls an imported module's background task may use, if its bongo-module.json asked for them
and its owner agreed at import (bongo/abilities.py, docs/MODULES.md).

A module never reaches the player or the config. What it asks crosses from its process as a small list of
names and values (``[{"do": "sleep"}, {"do": "brightness", "value": 40}]``), the way button presses cross the
other way; here each one is checked against what that module was allowed, checked for sense, rate-limited so a
broken module can't strobe the panel or flood it with messages, and then done with the same few lines the
dashboard's own controls use (``Live``). Anything else is refused, with the reason handed back to the module.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time

log = logging.getLogger("bongo.controls")

# what a module may ask (``do``) -> the control that allows it
COMMANDS = {"sleep": "sleep", "wake": "sleep", "brightness": "brightness",
            "pause": "playback", "resume": "playback", "next": "playback", "message": "message"}
# each control: (seconds between two uses, most uses in WINDOW), for each module on its own
LIMITS = {"sleep": (1.0, 6),   # sleep and wake together: 6 changes in ten minutes, however they come
           "brightness": (1.0, 60), "playback": (1.0, 60), "message": (5.0, 10)}
WINDOW = 600.0
MOST_AT_ONCE = 8                 # asks taken from one tick (bongo.module.Panel.MOST)
MESSAGE_TEXT, MESSAGE_TITLE, MESSAGE_SECONDS = 120, 40, (3, 30)
LONGEST_SLEEP = 7 * 24 * 60      # minutes: the dashboard's own longest Sleep now


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")


def _line(value, most: int) -> str:
    """Text for the panel: on one line, nothing unprintable, no longer than ``most``."""
    return " ".join("".join(c if c.isprintable() else " " for c in str(value)).split())[:most]


class Controls:
    def __init__(self, live, modules: dict, config):
        self.live, self.modules, self.config = live, modules, config   # config(): the saved config, read when needed
        self.lock = threading.Lock()
        self.used: dict[tuple, list] = {}     # (module, control) -> when it was used, within WINDOW
        self.said: dict[tuple, float] = {}    # (module, what) -> when it was last logged
        self.by: dict[str, tuple] = {}        # "sleep" | "brightness" -> (the module's name, what it set, the module's id)

    # -- what a module with "status" is told ---------------------------------------------------------------
    def status(self) -> dict:
        """The facts the paired Now page shows, and no more: never a setting, a file name or a frame."""
        s = self.live.status()
        entry = s.get("entry") or {}
        module = self.modules.get(entry.get("module"))
        level = getattr(self.live, "level", None)
        return {"asleep": bool(s["asleep"]), "paused": bool(s["paused"]),
                "brightness": s["brightness"] if s["brightness"] is not None else level,
                "brightness_by_hand": s["brightness"] is not None,
                # who set the one in force by hand: "dashboard", or the id of the module that did (None: not by hand)
                "brightness_set_by": None if s["brightness"] is None else self.brightness_by() or "dashboard",
                "screen": module.name if module else None, "module": entry.get("module"),
                "style": (entry.get("style") or (module.default_style if module else None)) if entry else None}

    def brightness_by(self) -> str | None:
        """The id of the module whose brightness is in force now (None: none is, or the dashboard set one since)."""
        bright = self.by.get("brightness")
        return bright[2] if bright and self.live.brightness is not None and self.live.brightness == bright[1] else None

    def dashboard_set(self, what: str) -> None:
        """The dashboard has just set ``what`` ("sleep" or "brightness") itself: no module did that one."""
        self.by.pop(what, None)

    def set_by(self) -> dict:
        """For the Now page: which module the sleep or brightness in force now was set by, if one was."""
        out = {}
        sleep, bright = self.by.get("sleep"), self.by.get("brightness")
        if sleep and self.live.sleep_until is not None and self.live.sleep_until == sleep[1]:
            out["sleep"] = sleep[0]
        if bright and self.live.brightness is not None and self.live.brightness == bright[1]:
            out["brightness"] = bright[0]
        return out

    # -- doing what a module asked -------------------------------------------------------------------------
    def apply(self, module_id: str, allowed, commands) -> list[dict]:
        """Do each of ``commands`` that ``module_id`` may (``allowed``: the controls its owner agreed to).
        Returns the ones refused: [{"do": ..., "why": ...}]."""
        refused = []
        if not isinstance(commands, list):
            return [{"do": None, "why": "not a list of commands"}]
        for n, command in enumerate(commands[:MOST_AT_ONCE * 2]):   # (whatever was sent: no more is even looked at)
            do = command.get("do") if isinstance(command, dict) else None
            do = do if isinstance(do, str) else None
            if n >= MOST_AT_ONCE:
                why = f"more than {MOST_AT_ONCE} at once"
            else:
                why = self._one(module_id, allowed or (), do, command if isinstance(command, dict) else {})
            if why:
                refused.append({"do": do[:24] if do else None, "why": why})
                self._say(module_id, f"refused:{do}:{why}", logging.WARNING, "module %s: %s refused: %s", module_id, (do or "a command")[:24], why)
        return refused

    def _say(self, module_id, key, level, message, *args, every: float = WINDOW) -> None:
        """Logged, but the same thing from the same module only once in a while."""
        now = time.monotonic()
        with self.lock:
            if now - self.said.get((module_id, key), -1e9) < every:
                return
            if len(self.said) > 500:
                self.said.clear()
            self.said[(module_id, key)] = now
        log.log(level, message, *args)

    def _too_often(self, module_id: str, control: str) -> str | None:
        gap, most = LIMITS[control]
        now = time.monotonic()
        with self.lock:
            used = [t for t in self.used.get((module_id, control), []) if now - t < WINDOW]
            if used and now - used[-1] < gap:
                why = f"too soon after the last one (at most one every {gap:g} s)"
            elif len(used) >= most:
                why = f"too many (at most {most} in {WINDOW / 60:g} minutes)"
            else:
                why = None
                used.append(now)
            self.used[(module_id, control)] = used
        return why

    def _one(self, module_id: str, allowed, do, command: dict) -> str | None:
        control = COMMANDS.get(do)
        if control is None:
            return "no such control"
        if control not in allowed:
            return f"this module wasn't given the {control} control"
        live, name = self.live, getattr(self.modules.get(module_id), "name", module_id)
        # checked before it counts against the limits, so a mistake doesn't use one up
        if unknown := set(command) - {"do", *{"sleep": ("minutes",), "brightness": ("value",), "message": ("text", "title", "seconds")}.get(do, ())}:
            return f"{do} doesn't take {', '.join(sorted(map(str, unknown)))[:60]}"
        if do == "sleep":
            minutes = command.get("minutes")
            if minutes is not None and not (_number(minutes) and 1 <= minutes <= LONGEST_SLEEP):
                return f"minutes: 1 to {LONGEST_SLEEP}"
        elif do == "brightness":
            value = command.get("value")
            if value is not None and not (_number(value) and 1 <= value <= 100):
                return "value: 1 to 100, or nothing for the Matrix's own"
        elif do == "message":
            text, title = command.get("text"), command.get("title", "")
            seconds = command.get("seconds", 10)
            if not isinstance(text, str) or not _line(text, MESSAGE_TEXT):
                return "text: a few words"
            if len(text) > MESSAGE_TEXT or not isinstance(title, str) or len(title) > MESSAGE_TITLE:
                return f"text: up to {MESSAGE_TEXT} characters; title: up to {MESSAGE_TITLE}"
            if not (_number(seconds) and MESSAGE_SECONDS[0] <= seconds <= MESSAGE_SECONDS[1]):
                return f"seconds: {MESSAGE_SECONDS[0]} to {MESSAGE_SECONDS[1]}"
            entry = self._message(_line(text, MESSAGE_TEXT), _line(title, MESSAGE_TITLE), float(seconds))
            if entry is None:
                return "this Matrix has no module that shows text"
        if why := self._too_often(module_id, control):
            return why
        # -- done exactly as the dashboard's own controls do it (bongo/web: /api/control, /api/push) --
        if do == "sleep":
            if minutes is None:
                minutes = self._until_sleep_changes()
            live.sleep_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=float(minutes))
            live.awake = False
            live.skip.set()
            self.by["sleep"] = (name, live.sleep_until, module_id)
        elif do == "wake":
            live.sleep_until = None
            live.awake = True
            live.skip.set()
        elif do == "brightness":
            live.set_brightness(None if value is None else int(value))   # "set by hand": it ends by itself (Live)
            self.by["brightness"] = (name, live.brightness, module_id)
        elif do in ("pause", "resume"):
            live.paused = do == "pause"
        elif do == "next":
            live.skip.set()
        elif do == "message":
            live.push(entry)
        if do == "brightness":   # (a dimmer being dragged: one line now and then)
            self._say(module_id, "did:brightness", logging.INFO, "module %s set the brightness", module_id, every=30)
        else:
            log.info("module %s: %s", module_id, {"sleep": "put the panel to sleep", "wake": "woke the panel", "pause": "paused the panel",
                                                 "resume": "resumed the panel", "next": "skipped to the next screen",
                                                 "message": "showed a message"}[do])
        return None

    def _until_sleep_changes(self) -> float:
        """Minutes until the sleep hours next start or end; with none set, the dashboard's longest Sleep now."""
        try:
            from zoneinfo import ZoneInfo

            from bongo.schedule import Schedule
            config = self.config()
            now = dt.datetime.now(ZoneInfo(config["timezone"]) if config.get("timezone") else None)   # the Matrix's own clock
            left = Schedule.from_config(config).seconds_until_change(now)
        except Exception:   # a config that can't be read just now: the longest, and it can always be woken
            log.exception("couldn't work out when the sleep hours change")
            left = None
        return LONGEST_SLEEP if left is None else max(1.0, min(LONGEST_SLEEP, left / 60))

    def _message(self, text: str, title: str, seconds: float):
        """The same screen Show on the Matrix makes of a message (bongo/web: /api/push)."""
        from bongo.player import Entry
        if "announcements" in self.modules:
            return Entry("announcements", "banner", seconds, {"items": [{"title": title, "text": text}]})
        if "text" in self.modules:
            return Entry("text", "scroll", seconds, {"text": text})
        return None
