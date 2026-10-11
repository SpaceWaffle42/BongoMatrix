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
import re
import subprocess
import threading
from pathlib import Path

LOGGER = logging.getLogger(__name__)

LOCAL_SUFFIX = ".local"
NAME_PREFIX = "matrix-"
SYSFS_NET = Path("/sys/class/net")
BUSCTL_TIMEOUT_SECONDS = 5
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


def _is_clash_rename(host, desired):
    """avahi resolves a name clash by appending ``-2``, ``-3``, ..."""
    return bool(re.fullmatch(re.escape(desired) + r"-[0-9]+", host or ""))


class DeviceName:
    """Keeps avahi publishing this device's name; reports the actual name."""

    def __init__(self, desired, avahi=None):
        self._desired = desired
        self._avahi = avahi or AvahiHostName()
        self._lock = threading.Lock()
        self._current = None

    @classmethod
    def for_device(cls, sysfs=SYSFS_NET, avahi=None):
        mac = builtin_mac(sysfs)
        name = default_name(mac)
        return cls(lambda: name, avahi=avahi)

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
