"""Saving a file only the Matrix's own user can read (an API key, a sign-in), whole or not at all.

And the secret settings of any module (``Module.secrets``: an API key, a token), kept in one such file,
``data/secrets.json``: {module id: {setting: value}}, one value per module, for all its screens. A secret is
never in the config, so never in a backup, on the hub or in anything a browser is sent: the dashboard is told
only whether one is saved (``saved``), and can replace it (``keep``) or clear it (``forget``). A screen gets
its module's in ``self.settings`` (``Module.create``). ``is_secret`` is how anything else asks whether a
setting is one; ``SECRETS`` is the file nothing else may copy or show.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import urllib.parse
from pathlib import Path

from bongo import durable, paths

SECRETS = Path(os.environ.get("BONGO_SECRETS", paths.DATA / "secrets.json"))
MOST_SECRET = 500          # characters in one secret: keys and tokens are far shorter
_lock = threading.Lock()
_secrets_lock = threading.RLock()
_read: tuple = (None, {})  # (the file's mtime when last read, what was in it)


def write(path: Path, text: str) -> None:
    """``text`` into ``path``, mode 0600 from the moment the file exists (never readable by others, even
    briefly), through a temporary file of its own so two saves at once can't mix or lose each other."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w") as out:
                out.write(text)
                out.flush()
                os.fsync(out.fileno())   # through the file it made: no other open of a secret file
            os.replace(temp, path)
            durable.sync_folder(path.parent)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise


# -- modules' secret settings ------------------------------------------------------------------------------------
def _names(value) -> tuple:
    return (value,) if isinstance(value, str) else tuple(n for n in (value or ()) if isinstance(n, str))


def is_secret(module, name: str, style: str | None = None) -> bool:
    """Whether ``module``'s setting ``name`` is a secret: ``Module.secrets``, or the ``Style.secrets`` of ``style``
    (its default look if not given). A secret is never copied, shown, logged or sent anywhere: ask this before doing
    any of those to a setting (a backup does, bongo/backup.py)."""
    if name in _names(getattr(module, "secrets", ())):
        return True
    styles = getattr(module, "styles", None) or {}
    cls = styles.get(style or next(iter(styles), None))
    return name in _names(getattr(cls, "secrets", ()))


def _all() -> dict:
    """Every saved secret, read again only when the file changes. {} when there is none (or it can't be read:
    in an imported module's own process, say, which never sees this file and is handed its own instead)."""
    global _read
    try:
        stamp = SECRETS.stat().st_mtime_ns
    except OSError:
        return {}
    with _secrets_lock:
        if _read[0] != stamp:
            try:
                kept = json.loads(SECRETS.read_text())
            except (OSError, ValueError):
                kept = {}
            _read = (stamp, {m: {n: v for n, v in names.items() if isinstance(n, str) and isinstance(v, str) and v}
                             for m, names in kept.items() if isinstance(names, dict)} if isinstance(kept, dict) else {})
        return _read[1]


def saved(module_id: str, name: str) -> bool:
    """Whether a secret is saved (all the dashboard is ever told about one)."""
    return bool(_all().get(module_id, {}).get(name))


def secrets_of(module) -> dict:
    """``module``'s saved secrets, {setting: value}, for the screen that needs them and nothing else."""
    mine = _all().get(module.id, {})
    return {name: mine[name] for name in (getattr(module, "secrets", None) or ()) if name in mine}


def clean(value) -> str:
    """A pasted secret without the spaces or line break around it. Raises ValueError if it can't be one."""
    if not isinstance(value, str):
        raise ValueError("Type or paste it first.")
    value = value.strip()
    if not value:
        raise ValueError("Type or paste it first.")
    if len(value) > MOST_SECRET or not value.isprintable():
        raise ValueError(f"That doesn't look right: it should be one line, up to {MOST_SECRET} characters.")
    return value


def _change(module_id: str, name: str | None, value: str | None) -> None:
    global _read
    with _secrets_lock:
        kept = {m: dict(names) for m, names in _all().items()}
        if name is None:
            kept.pop(module_id, None)
        elif value is None:
            kept.get(module_id, {}).pop(name, None)
        else:
            kept.setdefault(module_id, {})[name] = value
        kept = {m: names for m, names in kept.items() if names}
        if kept or SECRETS.exists():
            write(SECRETS, json.dumps(kept, indent=1))
        _read = (None, {})


def keep(module_id: str, name: str, value) -> None:
    """Save a secret (replacing the one there). Raises ValueError, in words for the dashboard, if it can't be one."""
    _change(module_id, name, clean(value))


def forget(module_id: str, name: str | None = None) -> None:
    """Clear one secret, or all of a module's (it has been removed)."""
    _change(module_id, name, None)


def hide(module_id: str, text) -> str:
    """``text`` with any of the module's saved secrets blanked out: for what a module prints or says went wrong,
    before it reaches the log (a web address with its key in it, say)."""
    text = str(text)
    for value in _all().get(module_id, {}).values():
        if len(value) >= 6:   # (a shorter one would blank out ordinary words and numbers)
            for form in {value, urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value)}:
                text = text.replace(form, "(secret)")
    return text
