"""What an imported module may ask for beyond drawing its screens (docs/MODULES.md, "Background modules,
controls and the home network"). A module asks in its ``bongo-module.json``:

    "abilities": {
      "background": true,
      "stay": true,
      "controls": ["status", "sleep", "brightness", "playback", "message"],
      "home_address": {"label": "Server address", "ports": [1883, 8883]},
      "settings": {"fields": [{"name": "password", "type": "text", "label": "Password"}], "note": "..."}
    }

Nothing here knows what any module is for. This file checks what was asked (``check``: a name it doesn't know
is refused, never skipped), says each ability in plain words for the import review (``words``), and keeps a
module's own settings (``Store``): one set for the whole module, edited in a card of its own in Settings, in a
file only the Matrix's user can read (``Store.file``, written with bongo/private.py). One of them is a secret
when the module names it in ``Module.secrets``, like any other secret setting: its value is then kept where
every module's secrets are (bongo/private.py: ``keep``, ``saved``, ``forget``), never in that file, and never
sent to a browser. A module with no "abilities" gets none of this and runs exactly as before.
"""
from __future__ import annotations

import copy
import ipaddress
import json
import logging
import math
import re
import threading
import time
import types
from pathlib import Path

log = logging.getLogger("bongo.abilities")

KNOWN = ("background", "stay", "controls", "home_address", "settings")
CONTROLS = ("status", "sleep", "brightness", "playback", "message")    # (bongo/controls.py does them)
TYPES = ("text", "bool", "int", "number", "choice")   # a field of a module's settings card, as its manifest says it
#                                                       (a "text" one named in Module.secrets is a "secret": ``mark_secrets``)
NAME = re.compile(r"[a-z][a-z0-9_]{0,23}\Z")
RESERVED = ("address", "port")   # the home-network address's own two fields
MOST_FIELDS, MOST_PORTS, MOST_ROWS = 12, 4, 8
MAX_LABEL, MAX_NOTE, MAX_TEXT, LONGEST_TEXT, MAX_STATUS = 30, 400, 100, 200, 80
HOST = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*\.?\Z")


class Problem(ValueError):
    """What's wrong with a module's "abilities", or with a value typed into its settings: said as it is."""


class Newer(Problem):
    """The module was made for a newer Matrix than this one."""


def _text(value, most: int, what: str, needed: bool = True) -> str:
    if value is None and not needed:
        return ""
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > most or not value.strip().isprintable():
        raise Problem(f"{what} must be some text (up to {most} characters)")
    return value.strip()


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# -- what a module asks for (its bongo-module.json) --------------------------------------------------------
def check(data, api=None) -> dict:
    """A manifest's "abilities" (and its "api"), checked and tidied. {}: it asks for none. Raises ``Problem``."""
    from bongo.module import API   # the module API this Matrix has: 2 added everything in this file

    if api is not None:
        if not isinstance(api, int) or isinstance(api, bool) or api < 1:
            raise Problem('"api" must be a whole number (the module API it needs: 2 for abilities)')
        if api > API:
            raise Newer(f"it needs a newer Matrix than this one (it was made for module API {api}; this Matrix has {API}). "
                          "Update the Matrix in Settings > Software, then import it again")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise Problem('"abilities" must be an object')
    for key in data:
        if key not in KNOWN:
            raise Problem(f'"abilities": this Matrix doesn\'t know "{str(key)[:40]}" (it knows {", ".join(KNOWN)}). '
                          "If the name is right, the module needs a newer Matrix: update it in Settings > Software")
    out = {}
    if "background" in data:
        if data["background"] is not True:
            raise Problem('"abilities": "background" is true, or left out')
        out["background"] = True
    if "stay" in data:                       # its screens may keep the panel while something is live (Style.stay)
        if data["stay"] is not True:
            raise Problem('"abilities": "stay" is true, or left out')
        out["stay"] = True
    if "controls" in data:
        wanted = data["controls"]
        if not isinstance(wanted, list) or not wanted or len(set(map(str, wanted))) != len(wanted):
            raise Problem(f'"abilities": "controls" is a list of {", ".join(CONTROLS)} (each once)')
        for name in wanted:
            if name not in CONTROLS:
                raise Problem(f'"abilities": this Matrix has no control called "{str(name)[:40]}" (it has {", ".join(CONTROLS)}). '
                              "If the name is right, the module needs a newer Matrix: update it in Settings > Software")
        if not out.get("background"):
            raise Problem('"abilities": "controls" need "background": true (they are used from its background task)')
        out["controls"] = [c for c in CONTROLS if c in wanted]
    if "home_address" in data:
        home = data["home_address"]
        if not isinstance(home, dict) or set(home) - {"label", "ports"}:
            raise Problem('"abilities": "home_address" is {"ports": [1883], "label": "Server address"}')
        ports = home.get("ports")
        if (not isinstance(ports, list) or not 1 <= len(ports) <= MOST_PORTS or len(set(map(str, ports))) != len(ports)
                or not all(isinstance(p, int) and not isinstance(p, bool) and 1 <= p <= 65535 for p in ports)):
            raise Problem(f'"abilities": "home_address" needs "ports": 1 to {MOST_PORTS} port numbers, each on its own (no ranges)')
        out["home_address"] = {"ports": list(ports),
                               "label": _text(home.get("label"), MAX_LABEL, '"home_address": "label"', False) or "Address"}
    if "settings" in data:
        out["settings"] = _card(data["settings"], bool(out.get("home_address")))
    return out


def _card(card, has_home: bool) -> dict:
    if not isinstance(card, dict) or set(card) - {"fields", "table", "note"}:
        raise Problem('"abilities": "settings" is {"fields": [...], "table": {...}, "note": "..."}')
    fields = card.get("fields", [])
    if not isinstance(fields, list) or len(fields) > MOST_FIELDS:
        raise Problem(f'"abilities": "settings": "fields" is a list of up to {MOST_FIELDS} fields')
    out, seen = {"fields": []}, set()
    for field in fields:
        if not isinstance(field, dict) or set(field) - {"name", "type", "label", "default", "note", "limits", "choices", "max"}:
            raise Problem('"settings": a field is {"name", "type", "label"}, with "default", "note", "limits", "choices" or "max" if it needs them')
        name, kind = field.get("name"), field.get("type")
        if not isinstance(name, str) or not NAME.match(name) or name in seen:
            raise Problem(f'"settings": a field\'s "name" is lower-case letters, digits and _ (up to 24, starting with a letter), each once: {str(name)[:40]!r}')
        if name in RESERVED:
            raise Problem(f'"settings": "{name}" is the name of the home-network address\'s own field; call this one something else')
        if kind == "secret":
            raise Problem(f'"settings": {name}: a secret is a "text" field that the module names in MODULE.secrets (secrets=("{name}",))')
        if kind not in TYPES:
            raise Problem(f'"settings": {name}: this Matrix has no field type "{str(kind)[:40]}" (it has {", ".join(TYPES)})')
        seen.add(name)
        one = {"name": name, "type": kind, "label": _text(field.get("label"), MAX_LABEL, f'"settings": {name}: "label"')}
        if field.get("note") is not None:
            one["note"] = _text(field["note"], 200, f'"settings": {name}: "note"')
        if kind == "choice":
            choices = field.get("choices")
            if (not isinstance(choices, list) or not 1 <= len(choices) <= 20 or len(set(map(str, choices))) != len(choices)
                    or not all(isinstance(c, str) and c.strip() == c and 1 <= len(c) <= 40 and c.isprintable() for c in choices)):
                raise Problem(f'"settings": {name}: "choices" is a list of 1 to 20 short names')
            one["choices"] = list(choices)
        elif "choices" in field:
            raise Problem(f'"settings": {name}: only a "choice" field has "choices"')
        if kind in ("int", "number"):
            limits = field.get("limits")
            if (not isinstance(limits, list) or len(limits) != 2 or not all(_number(v) for v in limits) or limits[0] > limits[1]
                    or (kind == "int" and not all(isinstance(v, int) for v in limits))):
                raise Problem(f'"settings": {name}: a number needs "limits": [lowest, highest]')
            one["limits"] = list(limits)
        elif "limits" in field:
            raise Problem(f'"settings": {name}: only a number has "limits"')
        if kind == "text":
            most = field.get("max", MAX_TEXT)
            if not isinstance(most, int) or isinstance(most, bool) or not 1 <= most <= LONGEST_TEXT:
                raise Problem(f'"settings": {name}: "max" is a length from 1 to {LONGEST_TEXT}')
            one["max"] = most
        elif "max" in field:
            raise Problem(f'"settings": {name}: only a "text" field has "max"')
        start = {"text": "", "bool": False, "choice": one.get("choices", [""])[0]}.get(kind, one.get("limits", [0])[0])
        try:
            one["default"] = _value(one, field.get("default", start))
        except Problem as error:
            raise Problem(f'"settings": its "default": {error}') from None
        out["fields"].append(one)
    if card.get("table") is not None:
        table = card["table"]
        rows = table.get("rows") if isinstance(table, dict) else None
        if (not isinstance(table, dict) or set(table) - {"title", "rows"} or not isinstance(rows, list)
                or not 1 <= len(rows) <= MOST_ROWS or not all(isinstance(r, list) and len(r) in (1, 2) for r in rows)):
            raise Problem(f'"settings": "table" is {{"title": "...", "rows": [["what", "tag"], ...]}} (up to {MOST_ROWS} rows)')
        out["table"] = {"title": _text(table.get("title"), 40, '"settings": "table": "title"'),
                        "rows": [[_text(r[0], 80, '"settings": "table": a row'), _text(r[1], 16, '"settings": "table": a tag') if len(r) > 1 else ""]
                                 for r in rows]}
    if card.get("note") is not None:
        out["note"] = _text(card["note"], MAX_NOTE, '"settings": "note"')
    if not out["fields"] and not has_home and "table" not in out and "note" not in out:
        raise Problem('"abilities": "settings" has nothing in it')
    return out


def mark_secrets(abilities: dict, names) -> list[str]:
    """Make each field of the settings card that the module names in ``Module.secrets`` a secret (in place).
    Returns the ones that can't be: only a "text" field with nothing as its default can."""
    wrong = []
    for field in ((abilities or {}).get("settings") or {}).get("fields", []):
        if field["name"] not in (names or ()) or field["type"] == "secret":
            continue
        if field["type"] != "text" or field.get("default"):
            wrong.append(field["name"])
            continue
        field["type"] = "secret"
        field.pop("default", None), field.pop("max", None)
    return wrong


def fields(abilities: dict) -> list[dict]:
    """Every field of a module's settings card: its home-network address first (two fields of the Matrix's
    own, so the gate knows exactly which setting is the address), then the ones it declared."""
    out = []
    home = (abilities or {}).get("home_address")
    if home:
        out.append({"name": "address", "type": "host", "label": home["label"], "default": ""})
        out.append({"name": "port", "type": "port", "label": "Port", "default": home["ports"][0], "choices": list(home["ports"])})
    return out + copy.deepcopy(((abilities or {}).get("settings") or {}).get("fields", []))


def has_card(abilities: dict) -> bool:
    return bool((abilities or {}).get("settings") or (abilities or {}).get("home_address"))


def words(abilities: dict) -> list[dict]:
    """Each thing a module asks for, in plain words: for the import review, before "I trust" can be ticked,
    and for its row in Settings > Modules. [{"key", "title", "detail"}]."""
    out = []
    asks = abilities or {}
    if asks.get("background"):
        out.append({"key": "background", "title": "Keep working in the background",
                    "detail": "It runs all the time it is switched on, not only while one of its screens is showing."})
    if asks.get("stay"):
        out.append({"key": "stay", "title": "Keep the panel while something is live",
                    "detail": "Once its screen's turn comes, it may stay on past its time while it says something is live: "
                              "never in the sleep hours, at most 4 hours in any 12, and Next still moves on. "
                              "Its own settings say whether it asks."})
    said = {
        "status": ("See what the panel is doing", "Whether it's asleep or paused, its brightness and the name of the screen "
                                                  "that's playing. Never your settings, and never the picture."),
        "sleep": ("Put the panel to sleep and wake it", "The same as Sleep now and Wake now."),
        "brightness": ("Change the brightness", "As if set by hand: it stays until you switch back to Auto, or with Follow daylight on "
                                                     "until the next sunrise, sunset or sleep change."),
        "playback": ("Pause, resume and skip to the next screen", ""),
        "message": ("Show short messages on the panel", "Like Show on the Matrix: a few words, for a few seconds."),
    }
    for name in asks.get("controls", []):
        out.append({"key": f"controls.{name}", "title": said[name][0], "detail": said[name][1]})
    home = asks.get("home_address")
    if home:
        ports = home["ports"]
        out.append({"key": "home_address", "title": "Connect to one device on your home network",
                    "detail": f"The one you name in its settings, on port {' or '.join(map(str, ports))}. "
                              "It can reach nothing else on your home network, and never this Matrix itself."})
    secrets = [f["label"] for f in (asks.get("settings") or {}).get("fields", []) if f["type"] == "secret"]
    if secrets:
        out.append({"key": "settings.secret", "title": f"Keep {'a secret' if len(secrets) == 1 else 'secrets'} you type in ({', '.join(secrets)})",
                    "detail": "Kept on the Matrix and given to this module's code, which could send it to anywhere it can reach."})
    return out


def added(new: dict, old: dict) -> list[dict]:
    """What a new version asks for that the one installed didn't (an update is approved again with these said)."""
    had = {w["key"]: w for w in words(old)}
    more = [w for w in words(new) if w["key"] not in had or w["title"] != had[w["key"]]["title"]]
    was, now = (old or {}).get("home_address"), (new or {}).get("home_address")
    if was and now and set(now["ports"]) - set(was["ports"]) and not any(w["key"] == "home_address" for w in more):
        more += [w for w in words(new) if w["key"] == "home_address"]
    return more


# -- a value typed into a module's settings ----------------------------------------------------------------
def host(value) -> str:
    """A home-network address as typed: a name or an IP address, nothing else ("" for none). Raises ``Problem``.
    Whether a name really is on the home network is decided when the module connects (bongo/sandbox/gate.py)."""
    if not isinstance(value, str):
        raise Problem("type a name or an IP address")
    text = value.strip().lower()
    if not text:
        return ""
    if "://" in text or "/" in text or "@" in text or " " in text:
        raise Problem("just the name or IP address of the device (no http://, no path)")
    try:
        ip = ipaddress.ip_address(text.strip("[]"))
    except ValueError:
        ip = None
    if ip is None:
        if re.fullmatch(r"[^:\[\]]+:\d{1,5}", text):
            raise Problem("just the name or IP address: the port has its own box")
        if len(text) > 253 or not HOST.match(text) or text.replace(".", "").isdigit():
            raise Problem("that isn't a name or an IP address")
        return text.rstrip(".")
    from bongo.sandbox.gate import home_reason
    why = home_reason(str(ip))
    if why:
        raise Problem(f"{ip} {why}")
    return str(ip)


def _value(field: dict, value):
    kind, name = field["type"], field["label"]
    if kind == "bool":
        if not isinstance(value, bool):
            raise Problem(f"{name}: on or off")
        return value
    if kind in ("int", "number"):
        if not _number(value) or (kind == "int" and not isinstance(value, int)):
            raise Problem(f"{name}: a {'whole ' if kind == 'int' else ''}number")
        low, high = field["limits"]
        if not low <= value <= high:
            raise Problem(f"{name}: from {low} to {high}")
        return value
    if kind == "choice":
        if value not in field["choices"]:
            raise Problem(f"{name}: one of {', '.join(field['choices'])}")
        return value
    if kind == "port":
        if isinstance(value, bool) or value not in field["choices"]:
            raise Problem(f"{name}: {' or '.join(map(str, field['choices']))}")
        return value
    if kind == "host":
        try:
            return host(value)
        except Problem as error:
            raise Problem(f"{name}: {error}") from None
    most = field.get("max", MAX_TEXT)   # ("text"; a secret never comes this way: bongo.private checks and keeps it)
    if not isinstance(value, str) or len(value) > most or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise Problem(f"{name}: up to {most} characters, on one line")
    return value.strip()


def status_text(value) -> str:
    """A module's one-line status, made safe to show: plain text on one line, no longer than ``MAX_STATUS``."""
    text = " ".join("".join(c if c.isprintable() else " " for c in str(value)).split())
    return text[:MAX_STATUS - 1] + "…" if len(text) > MAX_STATUS else text


class Store:
    """Every imported module's own settings (``file``: {id: {name: value}}), and each one's latest status line
    (kept in memory only). A secret among them isn't in that file: bongo/private.py keeps it."""

    def __init__(self):
        self.lock = threading.RLock()
        self._kept = None             # what the file holds, once read
        self._at = None               # (path, mtime...) it was read at
        self.statuses: dict[str, dict] = {}

    @staticmethod
    def file() -> Path:
        from bongo import addons
        return addons.DATA / "module-settings.json"

    def _load(self) -> dict:
        path = self.file()
        try:
            st = path.stat()
            at = (str(path), st.st_mtime_ns, st.st_ino, st.st_size)
        except OSError:
            at = (str(path), None)
        if self._kept is None or at != self._at:
            try:
                kept = json.loads(path.read_text()) if at[1] is not None else {}
            except (OSError, ValueError):
                kept = {}
            self._kept = {k: v for k, v in kept.items() if isinstance(v, dict)} if isinstance(kept, dict) else {}
            self._at = at
        return self._kept

    def _save(self, kept: dict) -> None:
        from bongo import private
        path = self.file()
        if kept:
            private.write(path, json.dumps(kept, indent=1))
        else:
            path.unlink(missing_ok=True)
        self._kept, self._at = None, None

    def values(self, module_id: str, abilities: dict | None = None) -> dict:
        """The module's settings as its own process is given them, its saved secrets among them ("" for one
        not saved): never sent to a browser. ``abilities``: as installed (read from there when left out)."""
        from bongo import private
        if abilities is None:
            from bongo import addons
            abilities = (addons.installed().get(module_id) or {}).get("abilities") or {}
        with self.lock:
            out = dict(self._load().get(module_id) or {})
        names = [f["name"] for f in fields(abilities) if f["type"] == "secret"]
        if names:   # (asked for by name, the way a screen's are: bongo.private.secrets_of)
            saved = private.secrets_of(types.SimpleNamespace(id=module_id, secrets=names))
            out.update({name: saved.get(name, "") for name in names})
        return out

    def adopt(self, module_id: str, abilities: dict) -> dict:
        """Installed or updated: keep what was typed before where the new version still has that field (and the
        value still fits), and fill in the rest from its defaults. A module with no settings keeps none.
        (Its secrets stay where they are, by name: bongo/private.py.)"""
        with self.lock:
            kept = dict(self._load())
            had = kept.get(module_id) or {}
            new = {}
            for field in fields(abilities):
                name = field["name"]
                if field["type"] == "secret":
                    continue
                try:
                    new[name] = _value(field, had[name]) if name in had else field.get("default", "")
                except Problem:
                    new[name] = field.get("default", "")
            if fields(abilities):
                kept[module_id] = new
            else:
                kept.pop(module_id, None)
            if kept != self._load():
                self._save(kept)
            return new

    def change(self, module_id: str, abilities: dict, sent: dict) -> None:
        """Save what the dashboard sent ({name: value}). A field left out stays as it is; a secret sent as ""
        or null is cleared. Raises ``Problem`` (nothing saved) if any value isn't right."""
        from bongo import private
        if not isinstance(sent, dict):
            raise Problem("send the settings as an object")
        known = {f["name"]: f for f in fields(abilities)}
        if unknown := sorted(set(map(str, sent)) - set(known)):
            raise Problem(f"it has no setting called {', '.join(unknown)[:80]}")
        with self.lock:
            new, secrets = self.adopt(module_id, abilities), {}
            for name, value in sent.items():
                field = known[name]
                if field["type"] != "secret":
                    new[name] = _value(field, value)
                elif value in (None, ""):
                    secrets[name] = None
                else:
                    try:
                        secrets[name] = private.clean(value)
                    except ValueError as error:
                        raise Problem(f"{field['label']}: {error}") from None
            kept = dict(self._load())
            kept[module_id] = new
            self._save(kept)
            for name, value in secrets.items():   # (each checked above: nothing is half saved)
                if value is None:
                    private.forget(module_id, name)
                else:
                    private.keep(module_id, name, value)

    def forget(self, module_id: str) -> None:
        """The module has been removed: its settings go (``addons.remove`` clears its secrets, with any others)."""
        with self.lock:
            kept = dict(self._load())
            if kept.pop(module_id, None) is not None:
                self._save(kept)
            self.statuses.pop(module_id, None)

    def public(self, module_id: str, abilities: dict) -> dict:
        """The settings card as the dashboard shows it: a secret is only ever "set" or not, never its value."""
        from bongo import private
        with self.lock:
            values = dict(self._load().get(module_id) or {})
        shown = []
        for field in fields(abilities):
            one = {k: v for k, v in field.items() if k != "default"}
            if field["type"] == "secret":
                one["set"] = private.saved(module_id, field["name"])
            else:
                one["value"] = values.get(field["name"], field.get("default", ""))
            shown.append(one)
        card = (abilities or {}).get("settings") or {}
        return {"fields": shown, **({"table": card["table"]} if card.get("table") else {}),
                **({"note": card["note"]} if card.get("note") else {})}

    # -- the status line -----------------------------------------------------------------------------------
    def say(self, module_id: str, text, ok: bool = True, by: str = "module") -> None:
        """A module's status line (``by`` "module": what it reported; "matrix": the Matrix speaking of it).
        Never with one of its secrets in it."""
        from bongo import private
        text = status_text(private.hide(module_id, status_text(text)))
        with self.lock:
            if text:
                self.statuses[module_id] = {"text": text, "ok": bool(ok), "by": by, "at": time.time()}
            else:
                self.statuses.pop(module_id, None)

    def status(self, module_id: str) -> dict | None:
        with self.lock:
            one = self.statuses.get(module_id)
            return dict(one) if one else None


STORE = Store()
