"""Transient ownership records for unsaved physical-device preview."""

from __future__ import annotations

import copy
import threading
import time
import weakref
from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType
from typing import Callable, Mapping


class PreviewAdmissionStatus(Enum):
    ACCEPTED = "accepted"
    OWNER_CONFLICT = "owner_conflict"
    STALE_GENERATION = "stale_generation"


class PreviewHeartbeatStatus(Enum):
    ACCEPTED = "accepted"
    TOO_EARLY = "too_early"
    OWNER_CONFLICT = "owner_conflict"
    STALE_GENERATION = "stale_generation"
    EXPIRED = "expired"
    NOT_ACTIVE = "not_active"


@dataclass(frozen=True)
class DevicePreviewOwner:
    device_id: str
    preview_instance: str


@dataclass(frozen=True)
class DevicePreviewRequest:
    owner: DevicePreviewOwner
    generation: int
    config: Mapping[str, object]
    timeout_seconds: int = 60
    cleanup: Callable[[], None] | None = None
    pinned_media: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "config",
            MappingProxyType(copy.deepcopy(dict(self.config))),
        )
        object.__setattr__(
            self,
            "pinned_media",
            MappingProxyType(dict(self.pinned_media or {})),
        )


@dataclass(frozen=True)
class DevicePreviewLease:
    request: DevicePreviewRequest
    content_deadline: float
    connectivity_deadline: float
    last_heartbeat_at: float

    @property
    def owner(self) -> DevicePreviewOwner:
        return self.request.owner

    @property
    def generation(self) -> int:
        return self.request.generation


@dataclass(frozen=True)
class DevicePreviewAdmission:
    status: PreviewAdmissionStatus
    lease: DevicePreviewLease | None


@dataclass(frozen=True)
class DevicePreviewHeartbeat:
    status: PreviewHeartbeatStatus
    lease: DevicePreviewLease | None


HEARTBEAT_INTERVAL_SECONDS = 5


class DevicePreviewManager:
    HEARTBEAT_INTERVAL_SECONDS = HEARTBEAT_INTERVAL_SECONDS
    """Own the one transient physical-preview lease independently of saved config."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        on_expire: Callable[[DevicePreviewLease], None] | None = None,
    ) -> None:
        self._clock = clock
        self._lock = threading.Condition(threading.RLock())
        self._active: DevicePreviewLease | None = None
        self._closing: DevicePreviewLease | None = None
        self._on_expire = on_expire
        self._reaper_thread: threading.Thread | None = None
        self._reaper_stop = False
        self._closed_request_refs: dict[int, weakref.ReferenceType[DevicePreviewRequest]] = {}

    def _ensure_reaper_locked(self) -> None:
        if self._reaper_thread is not None:
            return
        self._reaper_stop = False
        self._reaper_thread = threading.Thread(
            target=self._reap_expired,
            name="device-preview-expiry-reaper",
            daemon=True,
        )
        self._reaper_thread.start()

    def _reap_expired(self) -> None:
        try:
            while True:
                expired = None
                with self._lock:
                    while not self._reaper_stop:
                        lease = self._active
                        if lease is None:
                            self._lock.wait()
                            continue
                        remaining = min(
                            lease.content_deadline, lease.connectivity_deadline,
                        ) - self._clock()
                        if remaining > 0:
                            self._lock.wait(min(remaining, 0.05))
                            continue
                        self._active = None
                        self._closing = lease
                        expired = lease
                        break
                    if self._reaper_stop:
                        return
                self._settle_expired(expired)
        finally:
            with self._lock:
                if self._reaper_thread is threading.current_thread():
                    self._reaper_thread = None
                self._lock.notify_all()

    def _settle_expired(self, lease: DevicePreviewLease) -> None:
        try:
            if self._on_expire is not None:
                self._on_expire(lease)
        finally:
            try:
                self._close_request(lease.request)
            finally:
                with self._lock:
                    if self._closing is lease:
                        self._closing = None
                    self._lock.notify_all()

    def submit(
        self, request: DevicePreviewRequest, *, close_displaced: bool = True
    ) -> DevicePreviewAdmission:
        now = self._clock()
        displaced = None
        with self._lock:
            while self._closing is not None:
                self._lock.wait()
            self._ensure_reaper_locked()
            if self._active is not None:
                if self._active.owner != request.owner:
                    return DevicePreviewAdmission(
                        PreviewAdmissionStatus.OWNER_CONFLICT,
                        None,
                    )
                if request.generation <= self._active.generation:
                    return DevicePreviewAdmission(
                        PreviewAdmissionStatus.STALE_GENERATION,
                        None,
                    )
                displaced = self._active
            connectivity_deadline = (
                self._active.connectivity_deadline
                if self._active is not None
                else now + 15
            )
            last_heartbeat_at = (
                self._active.last_heartbeat_at
                if self._active is not None
                else now
            )
            lease = DevicePreviewLease(
                request=request,
                content_deadline=now + request.timeout_seconds,
                connectivity_deadline=connectivity_deadline,
                last_heartbeat_at=last_heartbeat_at,
            )
            self._active = lease
            self._lock.notify_all()
        if displaced is not None and close_displaced:
            self._close_request(displaced.request)
        return DevicePreviewAdmission(PreviewAdmissionStatus.ACCEPTED, lease)

    @staticmethod
    def _is_expired(lease: DevicePreviewLease, now: float) -> bool:
        return now >= min(lease.content_deadline, lease.connectivity_deadline)

    def heartbeat(
        self,
        owner: DevicePreviewOwner,
        *,
        generation: int,
    ) -> DevicePreviewHeartbeat:
        now = self._clock()
        expired = None
        with self._lock:
            lease = self._active
            if lease is None:
                return DevicePreviewHeartbeat(PreviewHeartbeatStatus.NOT_ACTIVE, None)
            if self._is_expired(lease, now):
                self._active = None
                self._closing = lease
                self._lock.notify_all()
                expired = lease
            elif lease.owner != owner:
                return DevicePreviewHeartbeat(
                    PreviewHeartbeatStatus.OWNER_CONFLICT,
                    None,
                )
            elif lease.generation != generation:
                return DevicePreviewHeartbeat(
                    PreviewHeartbeatStatus.STALE_GENERATION,
                    None,
                )
            elif now - lease.last_heartbeat_at < self.HEARTBEAT_INTERVAL_SECONDS:
                return DevicePreviewHeartbeat(PreviewHeartbeatStatus.TOO_EARLY, None)
            else:
                renewed = replace(
                    lease,
                    connectivity_deadline=now + 15,
                    last_heartbeat_at=now,
                )
                self._active = renewed
                self._lock.notify_all()
                return DevicePreviewHeartbeat(PreviewHeartbeatStatus.ACCEPTED, renewed)
        self._settle_expired(expired)
        return DevicePreviewHeartbeat(PreviewHeartbeatStatus.EXPIRED, None)


    def is_current(self, owner: DevicePreviewOwner, generation: int) -> bool:
        now = self._clock()
        with self._lock:
            lease = self._active
            return bool(
                lease
                and not self._is_expired(lease, now)
                and lease.owner == owner
                and lease.generation == generation
            )

    def _close_request(self, request: DevicePreviewRequest) -> None:
        cleanup = request.cleanup
        if cleanup is None:
            return
        request_id = id(request)
        manager_ref = weakref.ref(self)

        def forget(reference: weakref.ReferenceType[DevicePreviewRequest]) -> None:
            manager = manager_ref()
            if manager is None:
                return
            with manager._lock:
                if manager._closed_request_refs.get(request_id) is reference:
                    manager._closed_request_refs.pop(request_id, None)

        with self._lock:
            existing = self._closed_request_refs.get(request_id)
            if existing is not None and existing() is request:
                return
            self._closed_request_refs[request_id] = weakref.ref(request, forget)
        cleanup()

    def cancel(
        self,
        owner: DevicePreviewOwner,
        *,
        generation: int,
        close: bool = True,
    ) -> DevicePreviewLease | None:
        with self._lock:
            lease = self._active
            if lease is None or lease.owner != owner or lease.generation != generation:
                return None
            self._active = None
            self._lock.notify_all()
        if close:
            self._close_request(lease.request)
        return lease

    def fail(self, owner: DevicePreviewOwner, *, generation: int) -> bool:
        return self.cancel(owner, generation=generation) is not None

    def finish(self, lease: DevicePreviewLease) -> None:
        self._close_request(lease.request)

    def close(self) -> None:
        with self._lock:
            lease = self._active
            self._active = None
            self._reaper_stop = True
            reaper = self._reaper_thread
            self._lock.notify_all()
        if reaper is not None and reaper is not threading.current_thread():
            reaper.join()
        if lease is not None:
            self._close_request(lease.request)

    def active_lease(self) -> DevicePreviewLease | None:
        now = self._clock()
        expired = None
        with self._lock:
            if self._active is not None and self._is_expired(self._active, now):
                expired = self._active
                self._active = None
                self._closing = expired
                self._lock.notify_all()
            active = self._active
        if expired is not None:
            self._settle_expired(expired)
        return active
