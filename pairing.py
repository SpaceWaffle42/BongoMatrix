"""Short-lived, single-use PIN pairing with brute-force throttling."""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class PairingDurabilityError(OSError):
    """The registry is visible after replace, but directory durability is uncertain."""

    def __init__(self, devices, device=None):
        super().__init__("paired-device registry committed but durability is uncertain")
        self.devices = json.loads(json.dumps(devices))
        self.device = json.loads(json.dumps(device)) if device is not None else None


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
                self._validate_devices(devices)
                self._devices = devices
            else:
                self._write(self._devices)

    @staticmethod
    def _validate_devices(devices):
        if len(devices) > 10:
            raise ValueError("paired device file contains too many entries")
        seen = set()
        for device in devices:
            if not isinstance(device, dict) or set(device) != {"id", "name", "paired_at"}:
                raise ValueError("paired device entries must contain id, name, and paired_at")
            device_id = device["id"]
            name = device["name"]
            paired_at = device["paired_at"]
            if (
                not isinstance(device_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{32}", device_id)
                or device_id in seen
            ):
                raise ValueError("paired device ids must be unique URL-safe strings")
            if (
                not isinstance(name, str)
                or name != name.strip()
                or not name.isprintable()
                or not 1 <= len(name) <= 40
            ):
                raise ValueError("paired device names must contain 1 to 40 printable characters")
            if isinstance(paired_at, bool) or not isinstance(paired_at, int) or paired_at < 0:
                raise ValueError("paired device timestamps must be non-negative integers")
            seen.add(device_id)

    def _write(self, devices):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=self.path.name, dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(devices, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError as error:
                raise PairingDurabilityError(devices) from error
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def list(self):
        with self._lock:
            return json.loads(json.dumps(self._devices))

    def contains(self, device_id):
        with self._lock:
            return any(device["id"] == device_id for device in self._devices)

    @contextmanager
    def transaction(self):
        with self._lock:
            yield

    def remove(self, device_id):
        with self._lock:
            remaining = [device for device in self._devices if device["id"] != device_id]
            if len(remaining) == len(self._devices):
                return False
            try:
                self._write(remaining)
            except PairingDurabilityError as error:
                self._devices = error.devices
                raise
            self._devices = remaining
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
            updated = [*self._devices, device]
            self._validate_devices(updated)
            try:
                self._write(updated)
            except PairingDurabilityError as error:
                self._devices = error.devices
                raise PairingDurabilityError(error.devices, device=device) from error
            self._devices = updated
            return json.loads(json.dumps(device))


class PairingManager:
    def __init__(
        self,
        ttl_seconds=600,
        max_attempts=5,
        lockout_seconds=60,
        clock=time.monotonic,
        testing_pin=None,
    ):
        self.ttl_seconds = ttl_seconds
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self.clock = clock
        self._testing_pin = testing_pin
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
            candidate = str(candidate)
            if self._testing_pin is not None and hmac.compare_digest(candidate, self._testing_pin):
                return True
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

    def has_testing_pin(self):
        """Report capability without exposing the configured PIN."""
        return self._testing_pin is not None

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
