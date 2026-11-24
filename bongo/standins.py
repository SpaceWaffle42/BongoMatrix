"""Stand-ins: what a browser is sent in place of a secret that is saved with a screen.

A module's settings that are secrets are named in ``secrets`` (bongo/module.py). There are two kinds, and
between them a saved secret never goes back to a browser:

- A shared one (``Module.secrets``: an API key the whole module uses) is not kept with any screen. The Matrix
  keeps one value for the module in a private file (bongo/private.py: ``keep``, ``saved``, ``forget``).
- One that is a look's own (``Style.secrets``: the guest Wi-Fi password of one QR screen) is a setting of its
  screen like any other, saved with it in the config. This file is about those. What the dashboard is sent in its
  place is a stand-in, ``(saved:1f0c...)``: it says one is saved and nothing about what it is. A page sends the
  stand-in back with the screen (moved, copied or left alone) and ``unmask`` puts the saved value back; a new
  value typed in its place is saved as it is, and an empty one clears it.

``bongo.private.is_secret`` answers for both kinds.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import urllib.parse
from pathlib import Path

from bongo import paths, private

SALT = Path(os.environ.get("BONGO_STAND_INS", paths.DATA / "stand-ins.key"))
STAND_IN = re.compile(r"\(saved:[0-9a-f]{20}\)")
_lock = threading.Lock()
_salt: list = []   # this Matrix's own (see ``_salted``)


def own_names(module, style: str | None = None) -> tuple:
    """The secrets a screen of ``module`` in ``style`` (its default look if not given) keeps with it."""
    styles = getattr(module, "styles", None) or {}
    cls = styles.get(style or next(iter(styles), None))
    shared = getattr(module, "secrets", None) or ()
    return tuple(n for n in (getattr(cls, "secrets", None) or ()) if n not in shared)


def _salted() -> bytes:
    """Something only this Matrix knows, mixed into every stand-in so that one can't be worked back to its secret
    by guessing. Kept in a private file so stand-ins outlast a restart (a page left open still saves); if it can't
    be kept, this run's own."""
    with _lock:
        if _salt:
            return _salt[0]
    try:
        salt = bytes.fromhex(SALT.read_text().strip())
        if len(salt) < 16:
            raise ValueError
    except (OSError, ValueError):
        salt = secrets.token_bytes(32)
        try:
            private.write(SALT, salt.hex())
        except OSError:
            pass
    with _lock:
        _salt[:] = [salt]
    return salt


def stand_in(module_id: str, name: str, value: str) -> str:
    """What a browser is sent in place of a saved secret: the same for the same secret, telling nothing of it."""
    mark = hmac.new(_salted(), f"{module_id}\0{name}\0{value}".encode(), hashlib.sha256).hexdigest()[:20]
    return f"(saved:{mark})"


def _screens(config):
    """Every screen in a config (or in what a page sent of one) that could hold settings."""
    for key in ("playlist", "overlays"):
        entries = config.get(key) if isinstance(config, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and isinstance(entry.get("settings"), dict):
                yield key, entry


def _names(modules: dict, key: str, entry: dict) -> tuple:
    """The secret settings ``entry`` keeps with it: its look's own (``Style.secrets``)."""
    module = modules.get(entry.get("module")) if isinstance(entry.get("module"), str) else None
    if module is None:
        return ()
    if key == "overlays":   # (an overlay is a style of its own kind, named by "overlay")
        cls = (getattr(module, "overlays", None) or {}).get(entry.get("overlay"))
        return tuple(getattr(cls, "secrets", None) or ())
    style = entry.get("style")
    return own_names(module, style if isinstance(style, str) else None)


def mask_entry(entry: dict, modules: dict, key: str = "playlist") -> dict:
    """One screen as a browser may see it: each saved secret swapped for its stand-in. ``entry`` isn't changed."""
    settings = entry.get("settings") if isinstance(entry, dict) else None
    if not isinstance(settings, dict):
        return entry
    hidden = {n: stand_in(entry["module"], n, settings[n]) for n in _names(modules, key, entry)
              if isinstance(settings.get(n), str) and settings[n] and not STAND_IN.fullmatch(settings[n])}
    return {**entry, "settings": {**settings, **hidden}} if hidden else entry


def mask(config: dict, modules: dict) -> dict:
    """A config as a browser may see it (``mask_entry`` for every screen). ``config`` isn't changed."""
    out = dict(config)
    for key in ("playlist", "overlays"):
        if isinstance(config.get(key), list):
            out[key] = [mask_entry(e, modules, key) if isinstance(e, dict) else e for e in config[key]]
    return out


def unmask(sent: dict, saved: dict, modules: dict) -> dict:
    """What a page sent, with each stand-in swapped back for the saved secret it stands for: any screen's of the
    same module and setting in ``saved``, so a screen that was moved or copied keeps its secret. A stand-in for
    nothing saved is left as it is (``stand_ins``; config.validate refuses it). ``sent`` isn't changed."""
    known: dict = {}
    for key, entry in _screens(saved):
        for name in _names(modules, key, entry):
            value = entry["settings"].get(name)
            if isinstance(value, str) and value:
                known[(entry.get("module"), name, stand_in(entry["module"], name, value))] = value
    out = dict(sent)
    for key in ("playlist", "overlays"):
        if not isinstance(sent.get(key), list):
            continue
        out[key] = []
        for entry in sent[key]:
            settings = entry.get("settings") if isinstance(entry, dict) else None
            if isinstance(settings, dict):
                back = {n: known[(entry.get("module"), n, settings[n])] for n in _names(modules, key, entry)
                        if isinstance(settings.get(n), str) and (entry.get("module"), n, settings[n]) in known}
                if back:
                    entry = {**entry, "settings": {**settings, **back}}
            out[key].append(entry)
    return out


def stand_ins(module, style: str | None, settings: dict) -> list:
    """The secret settings in ``settings`` that are still a stand-in: never a value to save or to draw with."""
    return [n for n in own_names(module, style) if isinstance(settings.get(n), str) and STAND_IN.fullmatch(settings[n])]


def blank(text, values) -> str:
    """``text`` with each of ``values`` (secrets) blanked out, however a web address would have written it: for
    what a module prints or says went wrong, before it reaches the log."""
    text = str(text)
    for value in sorted((v for v in values if isinstance(v, str) and len(v) >= 6), key=len, reverse=True):
        for form in {value, urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value)}:   # (a shorter one
            text = text.replace(form, "(secret)")                                    # would blank out ordinary words)
    return text


HUSHED: set = set()   # secret settings screens have been handed (``hush``): never in the log


def hush(values) -> None:
    """Note secrets a screen has just been handed with its settings (``Module.create`` does), so that no log line,
    the Matrix's own or what an imported module prints, ever carries one (``install``)."""
    with _lock:
        HUSHED.update(v for v in values if isinstance(v, str) and len(v) >= 6)
        if len(HUSHED) > 500:   # (only what was handed lately: a secret replaced a thousand times ago isn't kept for ever)
            HUSHED.clear()


def secrets_now() -> list:
    """Every secret the log must not carry: what screens were handed, and every module's own saved one."""
    try:
        saved = [v for names in private._all().values() for v in names.values()]
    except Exception:   # (no file, or one this process can't see: an imported module's own process)
        saved = []
    return [*HUSHED, *saved]


def clean_record(record) -> None:
    """``record`` with any secret blanked out of its message and of the error it carries."""
    values = secrets_now()
    if not values:
        return
    try:
        text = record.getMessage()
        clean = blank(text, values)
        if clean != text:
            record.msg, record.args = clean, ()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = blank(record.exc_text, values)
    except Exception:   # a line that can't be put together is logging's to report, not ours to lose
        pass


_installed: list = []


def install() -> None:
    """Every log record made in this process, by any logger (built-in modules, imported ones, the Matrix itself), has
    its secrets blanked out as it is made, before any handler (the Logs page, the journal) sees it. Once."""
    with _lock:
        if _installed:
            return
        made = logging.getLogRecordFactory()

        def factory(*args, **kwargs):
            record = made(*args, **kwargs)
            clean_record(record)
            return record
        logging.setLogRecordFactory(factory)
        _installed.append(factory)


install()
