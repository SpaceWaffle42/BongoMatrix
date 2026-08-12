"""Bounded process-local cache for provider-backed scrolling timing plans."""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict


class TimingCacheBusy(RuntimeError):
    """No provider preparation slot is available."""


class ScrollTimingCache:
    def __init__(
        self, *, clock=time.monotonic, fresh_seconds=300, stale_seconds=86400,
        max_entries=16, max_entry_bytes=8192, max_bytes=65536, max_fills=2,
    ):
        self.clock = clock
        self.fresh_seconds = fresh_seconds
        self.stale_seconds = stale_seconds
        self.max_entries = max_entries
        self.max_entry_bytes = max_entry_bytes
        self.max_bytes = max_bytes
        self._entries = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()
        self._fills = {}
        self._slots = threading.BoundedSemaphore(max_fills)

    def clear(self, provider=None):
        with self._lock:
            for key in list(self._entries):
                if provider is None or key[0] == provider:
                    _, size = self._entries.pop(key)
                    self._bytes -= size

    def _cached(self, key):
        entry = self._entries.get(key)
        if entry is None:
            return None
        record, size = entry
        age = self.clock() - record["stored_at"]
        if age > self.stale_seconds:
            self._entries.pop(key)
            self._bytes -= size
            return None
        self._entries.move_to_end(key)
        return record, age

    def _store(self, key, plan):
        payload = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
        size = len(payload)
        if size > self.max_entry_bytes:
            return
        old = self._entries.pop(key, None)
        if old:
            self._bytes -= old[1]
        record = {"stored_at": self.clock(), "plan": plan}
        self._entries[key] = (record, size)
        self._bytes += size
        while len(self._entries) > self.max_entries or self._bytes > self.max_bytes:
            _, (_, removed_size) = self._entries.popitem(last=False)
            self._bytes -= removed_size

    def get_or_fill(self, key, fill, *, deadline=None):
        """Return fresh data, coalesce a fill, or visibly fall back to stale data."""
        if deadline is None:
            deadline = self.clock() + 5

        def remaining():
            budget = deadline - self.clock()
            if budget <= 0:
                raise TimeoutError("provider timing deadline exceeded")
            return budget

        remaining()
        with self._lock:
            cached = self._cached(key)
            if cached and cached[1] <= self.fresh_seconds:
                return {**cached[0]["plan"], "status": "fresh", "cache": "hit"}
            pending = self._fills.get(key)
            if pending is None:
                pending = threading.Event()
                self._fills[key] = pending
                leader = True
            else:
                leader = False
        if not leader:
            if not pending.wait(remaining()):
                raise TimeoutError("provider timing deadline exceeded")
            remaining()
            with self._lock:
                cached = self._cached(key)
                if cached:
                    return {**cached[0]["plan"], "status": "fresh" if cached[1] <= self.fresh_seconds else "stale", "cache": "coalesced"}
            raise RuntimeError("provider timing unavailable")
        remaining()
        if not self._slots.acquire(blocking=False):
            with self._lock:
                self._fills.pop(key, None)
                pending.set()
            remaining()
            with self._lock:
                cached = self._cached(key)
                if cached:
                    return {**cached[0]["plan"], "status": "stale", "cache": "fallback"}
            raise TimingCacheBusy("provider timing service is busy")
        try:
            try:
                remaining()
                plan = fill()
                remaining()
            except Exception:
                with self._lock:
                    cached = self._cached(key)
                    if cached:
                        return {**cached[0]["plan"], "status": "stale", "cache": "fallback"}
                raise
            with self._lock:
                self._store(key, plan)
            return {**plan, "status": "fresh", "cache": "fill"}
        finally:
            self._slots.release()
            with self._lock:
                self._fills.pop(key, None)
                pending.set()
