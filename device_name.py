"""Roadmap 1.10b: a fixed per-device mDNS name, matrix-xxxx.local.

The default is ``matrix-`` plus the last four hex digits of the built-in
(hardware, not randomised) MAC address, so it survives reboots and power cuts
without storing anything.  The name is published through the avahi-daemon that
already runs on the device (D-Bus ``SetHostName`` via ``busctl``); the system
host name is left alone.  If another device already uses the name, avahi picks
``matrix-xxxx-2`` itself; that rename is kept and reported, never fought.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

LOGGER = logging.getLogger(__name__)

LOCAL_SUFFIX = ".local"
NAME_PREFIX = "matrix-"
SYSFS_NET = Path("/sys/class/net")
BUSCTL_TIMEOUT_SECONDS = 5
RESOLVE_TIMEOUT_SECONDS = 3
_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def _read(path):
    try:
        return path.read_text().strip()
    except OSError:
        return None


def builtin_mac(sysfs=SYSFS_NET):
    """Return the lowercase MAC of the built-in network chip, or None.

    Only physical interfaces (with a ``device`` link) whose address is the
    permanent one (``addr_assign_type`` 0) and globally administered count.
    Wired interfaces win over wireless, then interface name order, so the
    result does not depend on which link happens to be up.
    """
    candidates = []
    try:
        interfaces = sorted(Path(sysfs).iterdir())
    except OSError:
        return None
    for iface in interfaces:
        if not (iface / "device").exists():
            continue
        if _read(iface / "addr_assign_type") not in ("0", None):
            continue
        mac = (_read(iface / "address") or "").lower()
        if not _MAC.match(mac) or mac == "00:00:00:00:00:00":
            continue
        if int(mac[:2], 16) & 0x02:  # locally administered / randomised
            continue
        wireless = (iface / "wireless").exists()
        candidates.append((wireless, iface.name, mac))
    return min(candidates)[2] if candidates else None


def default_name(mac):
    """``matrix-`` plus the last four hex digits of ``mac``."""
    if not mac:
        return None
    digits = mac.replace(":", "").lower()
    return NAME_PREFIX + digits[-4:]


class AvahiHostName:
    """Read and set the avahi-daemon host name over the system D-Bus."""

    def __init__(self, run=subprocess.run):
        self._run = run

    def _call(self, method, *args):
        argv = [
            "busctl", "--json=short", "call",
            "org.freedesktop.Avahi", "/", "org.freedesktop.Avahi.Server", method,
        ]
        if args:
            argv += ["s", *args]
        return self._run(
            argv, capture_output=True, text=True, check=True,
            timeout=BUSCTL_TIMEOUT_SECONDS,
        )

    def get(self):
        try:
            result = self._call("GetHostName")
            value = json.loads(result.stdout)["data"][0]
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError):
            return None
        return value if isinstance(value, str) and value else None

    def set(self, name):
        try:
            self._call("SetHostName", name)
        except (OSError, subprocess.SubprocessError):
            LOGGER.warning("Could not publish mDNS name %s", name)
            return False
        return True

    def resolves(self, name):
        """True when some host on the network answers to ``name.local``."""
        argv = [
            "busctl", "--json=short", f"--timeout={RESOLVE_TIMEOUT_SECONDS}", "call",
            "org.freedesktop.Avahi", "/", "org.freedesktop.Avahi.Server",
            "ResolveHostName", "iisiu", "--", "-1", "0", name + LOCAL_SUFFIX, "-1", "0",
        ]
        try:
            self._run(
                argv, capture_output=True, text=True, check=True,
                timeout=RESOLVE_TIMEOUT_SECONDS + 2,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return True


def _is_clash_rename(host, desired):
    """avahi resolves a name clash by appending ``-2``, ``-3``, ..."""
    return bool(re.fullmatch(re.escape(desired) + r"-[0-9]+", host or ""))


# Roadmap 1.10c: owner-chosen display name rules.
MAX_NAME_LENGTH = 63
RESERVED_NAMES = frozenset({"matrix"})
_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def name_problem(name):
    """Return why ``name`` cannot be a display name, or None if it can."""
    if not isinstance(name, str) or not name:
        return "Enter a name."
    if len(name) > MAX_NAME_LENGTH:
        return f"Use at most {MAX_NAME_LENGTH} characters."
    if not _NAME.match(name):
        return "Use letters, digits and hyphens only, not starting or ending with a hyphen."
    if name.lower() in RESERVED_NAMES:
        return "\"matrix\" is reserved for the directory page."
    return None


class DisplayNameStore:
    """One-line file holding the owner's chosen name; absent means default."""

    def __init__(self, path):
        self.path = Path(path)

    def get(self):
        try:
            value = self.path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            return None
        return value if name_problem(value) is None else None

    def set(self, name):
        if name_problem(name) is not None:
            raise ValueError(name_problem(name))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".display-name.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(name + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o644)
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        self._sync_directory()

    def clear(self):
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        self._sync_directory()

    def _sync_directory(self):
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class DeviceName:
    """Keeps avahi publishing this device's name; reports the actual name."""

    def __init__(self, desired, avahi=None):
        self._desired = desired
        self._avahi = avahi or AvahiHostName()
        self._lock = threading.Lock()
        self._current = None

    @classmethod
    def for_device(cls, sysfs=SYSFS_NET, avahi=None, store=None):
        default = default_name(builtin_mac(sysfs))
        instance = cls(
            lambda: (store.get() if store is not None else None) or default, avahi=avahi,
        )
        instance.default = default
        instance.store = store
        return instance

    default = None
    store = None

    def desired(self):
        return self._desired()

    def in_use_elsewhere(self, name):
        """True when another device already answers to ``name.local``.

        The name this device currently publishes is ours, not a duplicate.
        """
        current = (self.current() or "").lower()
        if current in (name.lower() + LOCAL_SUFFIX,):
            return False
        return self._avahi.resolves(name)

    def current(self):
        """Last known published name (``matrix-xxxx.local``) or None."""
        with self._lock:
            return self._current

    def refresh(self):
        """Make sure avahi publishes the desired name; return the actual one."""
        desired = self._desired()
        host = self._avahi.get() if desired else None
        if desired and host is not None and host != desired and not _is_clash_rename(host, desired):
            if self._avahi.set(desired):
                host = self._avahi.get()
        actual = host + LOCAL_SUFFIX if desired and host else None
        with self._lock:
            self._current = actual
        return actual
