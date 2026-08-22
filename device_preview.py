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


class DevicePreviewManager:
    """Own the one transient physical-preview lease independently of saved config."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._active: DevicePreviewLease | None = None
        self._closed_request_refs: dict[int, weakref.ReferenceType[DevicePreviewRequest]] = {}

    def submit(
        self, request: DevicePreviewRequest, *, close_displaced: bool = True
    ) -> DevicePreviewAdmission:
        now = self._clock()
        displaced = None
        with self._lock:
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
            elif now - lease.last_heartbeat_at < 5:
                return DevicePreviewHeartbeat(PreviewHeartbeatStatus.TOO_EARLY, None)
            else:
                renewed = replace(
                    lease,
                    connectivity_deadline=now + 15,
                    last_heartbeat_at=now,
                )
                self._active = renewed
                return DevicePreviewHeartbeat(PreviewHeartbeatStatus.ACCEPTED, renewed)
        self._close_request(expired.request)
        return DevicePreviewHeartbeat(PreviewHeartbeatStatus.EXPIRED, None)


    def is_current(self, owner: DevicePreviewOwner, generation: int) -> bool:
        lease = self.active_lease()
        return bool(lease and lease.owner == owner and lease.generation == generation)

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
        if lease is not None:
            self._close_request(lease.request)

    def active_lease(self) -> DevicePreviewLease | None:
        now = self._clock()
        expired = None
        with self._lock:
            if self._active is not None and self._is_expired(self._active, now):
                expired = self._active
                self._active = None
            active = self._active
        if expired is not None:
            self._close_request(expired.request)
        return active
