"""Short-lived, single-use PIN pairing with brute-force throttling."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path


def initialize_pairing_display(controller, pairing, device_store):
    """Start content for trusted devices, otherwise display a fresh pairing PIN."""
    if device_store.list():
        controller.start()
        return
    pin = pairing.issue_pin()
    controller.show_pairing_pin(pin, ttl_seconds=pairing.remaining_seconds())


class PairedDeviceStore:
    """Persistent allowlist for browser sessions that may control the Matrix."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._devices = []
        if self.path is not None:
            if self.path.exists():
                devices = json.loads(self.path.read_text())
                if not isinstance(devices, list):
                    raise ValueError("paired device file must contain a JSON list")
                self._devices = devices
            else:
                self._write()

    def _write(self):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=self.path.name, dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(self._devices, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def list(self):
        with self._lock:
            return json.loads(json.dumps(self._devices))

    def contains(self, device_id):
        with self._lock:
            return any(device["id"] == device_id for device in self._devices)

    def remove(self, device_id):
        with self._lock:
            remaining = [device for device in self._devices if device["id"] != device_id]
            if len(remaining) == len(self._devices):
                return False
            self._devices = remaining
            self._write()
            return True

    def add(self, name, max_devices):
        name = name.strip() if isinstance(name, str) else ""
        if not 1 <= len(name) <= 40 or any(not character.isprintable() for character in name):
            raise ValueError("device name must be 1-40 printable characters")
        with self._lock:
            if len(self._devices) >= max_devices:
                raise ValueError("maximum paired devices reached")
            device = {
                "id": secrets.token_urlsafe(24),
                "name": name,
                "paired_at": int(time.time()),
            }
            self._devices.append(device)
            self._write()
            return json.loads(json.dumps(device))


class PairingManager:
    def __init__(
        self,
        ttl_seconds=600,
        max_attempts=5,
        lockout_seconds=60,
        clock=time.monotonic,
    ):
        self.ttl_seconds = ttl_seconds
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self.clock = clock
        self._pin = None
        self._expires_at = 0
        self._failed_attempts = 0
        self._locked_until = 0
        self._lock = threading.Lock()

    def issue_pin(self):
        with self._lock:
            self._pin = f"{secrets.randbelow(1_000_000):06d}"
            self._expires_at = self.clock() + self.ttl_seconds
            self._failed_attempts = 0
            self._locked_until = 0
            return self._pin

    def verify(self, candidate):
        with self._lock:
            now = self.clock()
            if now < self._locked_until:
                return False
            valid = (
                self._pin is not None
                and now <= self._expires_at
                and hmac.compare_digest(str(candidate), self._pin)
            )
            if valid:
                self._pin = None
                self._expires_at = 0
                self._failed_attempts = 0
                self._locked_until = 0
                return True
            if self._pin is not None and now <= self._expires_at:
                self._failed_attempts += 1
                if self._failed_attempts >= self.max_attempts:
                    self._failed_attempts = 0
                    self._locked_until = now + self.lockout_seconds
            return False

    def is_locked(self):
        with self._lock:
            return self.clock() < self._locked_until

    def active_pin(self):
        with self._lock:
            if self._pin is None or self.clock() > self._expires_at:
                return None
            return self._pin

    def remaining_seconds(self):
        with self._lock:
            return max(0, self._expires_at - self.clock()) if self._pin else 0
