"""Fail-closed publication of physical release-acceptance evidence.

This module is intentionally not a general artifact API.  A transaction is a
single-use, attempt-bound state machine whose files are created beneath an
owner-only runtime directory using directory-relative, no-follow operations.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from pathlib import Path
from typing import Mapping

_FRAME_SIZE = 64 * 32 * 3
_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX40 = re.compile(r"[0-9a-f]{40}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_MODES = frozenset({"deploy", "rollback"})
_TRANSACTION_FIELDS = frozenset({
    "attempt_id", "acceptance_operation_id", "evidence_id", "expected_source_sha",
    "expected_version", "mode", "nonce", "owner_revision",
})


def _exact_int(value: object, *, positive: bool = False) -> int:
    if type(value) is not int or (value <= 0 if positive else value < 0):
        raise ValueError("acceptance integer is invalid")
    return value


def _validated_transaction(value: Mapping[str, object]) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != _TRANSACTION_FIELDS:
        raise ValueError("acceptance transaction fields are invalid")
    result = dict(value)
    if type(result["attempt_id"]) is not str or _HEX32.fullmatch(result["attempt_id"]) is None:
        raise ValueError("attempt identity is invalid")
    operation_id = result["acceptance_operation_id"]
    if type(operation_id) is not str or _HEX64.fullmatch(operation_id) is None:
        raise ValueError("acceptance operation identity is invalid")
    if _HEX64.fullmatch(str(result["evidence_id"])) is None:
        raise ValueError("evidence identity is invalid")
    if _HEX40.fullmatch(str(result["expected_source_sha"])) is None:
        raise ValueError("source identity is invalid")
    version = result["expected_version"]
    if type(version) is not str or not version or len(version.encode("utf-8")) > 128 or "\0" in version:
        raise ValueError("version identity is invalid")
    if result["mode"] not in _MODES:
        raise ValueError("acceptance mode is invalid")
    if _HEX64.fullmatch(str(result["nonce"])) is None:
        raise ValueError("acceptance nonce is invalid")
    _exact_int(result["owner_revision"])
    return result


def _owner_directory(path: Path, *, create: bool) -> int:
    """Open one exact directory without following its final component."""
    if create:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    info = os.fstat(descriptor)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        os.close(descriptor)
        raise PermissionError("acceptance directory is not owner-only")
    return descriptor


def _mkdir_open_at(parent: int, name: str, *, exclusive: bool) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        if exclusive:
            raise
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                         dir_fd=parent)
    info = os.fstat(descriptor)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        os.close(descriptor)
        raise PermissionError("acceptance directory is not canonical")
    return descriptor


def _write_all(descriptor: int, data: bytes) -> None:
    position = 0
    while position < len(data):
        written = os.write(descriptor, data[position:])
        if written <= 0:
            raise OSError("short acceptance artifact write")
        position += written


class AcceptanceArtifactTransaction:
    """One fresh INIT -> DRAFT_PUBLISHED -> SAVED_ROLLBACK_PUBLISHED transaction."""

    INIT = "INIT"
    DRAFT_PUBLISHED = "DRAFT_PUBLISHED"
    SAVED_ROLLBACK_PUBLISHED = "SAVED_ROLLBACK_PUBLISHED"

    def __init__(self, runtime: str | os.PathLike[str], transaction: dict[str, object], *, owner_generation: int):
        self._lock = threading.Lock()
        self._closed = False
        self._transaction = _validated_transaction(transaction)
        self.owner_generation = _exact_int(owner_generation)
        self._state = self.INIT
        self._draft_revision: int | None = None
        self._draft_digest: str | None = None
        self._completion: dict[str, object] = {}
        self._closed = False

        runtime_path = Path(runtime)
        # The runtime's parent is configuration-owned; reject a linked final runtime
        # and use dirfd-relative operations for every descendant.
        self._runtime_fd = _owner_directory(runtime_path, create=True)
        try:
            self._transactions_fd = _mkdir_open_at(self._runtime_fd, "transactions", exclusive=False)
            try:
                self._attempt_fd = _mkdir_open_at(
                    self._transactions_fd, str(self._transaction["attempt_id"]), exclusive=False,
                )
                try:
                    self._operation_fd = _mkdir_open_at(
                        self._attempt_fd, str(self._transaction["acceptance_operation_id"]),
                        exclusive=True,
                    )
                except BaseException:
                    os.close(self._attempt_fd)
                    raise
            except BaseException:
                os.close(self._transactions_fd)
                raise
        except BaseException:
            os.close(self._runtime_fd)
            raise
        os.fsync(self._operation_fd)
        self.path = (runtime_path / "transactions" / str(self._transaction["attempt_id"])
                     / str(self._transaction["acceptance_operation_id"]))

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def status(self) -> dict[str, object]:
        with self._lock:
            return self.status_unlocked()

    def _atomic_file(self, name: str, data: bytes) -> None:
        temporary = f".{name}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC |
                             os.O_NOFOLLOW, 0o600, dir_fd=self._operation_fd)
        try:
            _write_all(descriptor, data)
            os.fsync(descriptor)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=self._operation_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(descriptor)
        try:
            os.rename(temporary, name, src_dir_fd=self._operation_fd, dst_dir_fd=self._operation_fd)
            os.fsync(self._operation_fd)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=self._operation_fd)
            except OSError:
                pass
            raise

    @staticmethod
    def _frame(frame: object) -> bytes:
        if type(frame) is not bytes or len(frame) != _FRAME_SIZE:
            raise ValueError("acceptance frame must be exact 64x32 RGB bytes")
        return frame

    def publish_draft(self, frame: bytes, *, frame_revision: int) -> dict[str, object]:
        frame = self._frame(frame)
        revision = _exact_int(frame_revision, positive=True)
        with self._lock:
            if self._state != self.INIT or self._closed:
                raise RuntimeError("acceptance draft publication is out of order")
            self._atomic_file("draft.rgb", frame)
            self._draft_revision = revision
            self._draft_digest = hashlib.sha256(frame).hexdigest()
            self._state = self.DRAFT_PUBLISHED
            return {"state": self._state, "draft_revision": revision,
                    "draft_sha256": self._draft_digest}

    def publish_saved_rollback(
        self, frame: bytes, *, frame_revision: int, saved_revision: int | None = None,
        foreign_rejected: bool = True, timeout_returned: bool = True,
        heartbeat_accepted: bool = True,
    ) -> dict[str, object]:
        frame = self._frame(frame)
        current_revision = _exact_int(frame_revision, positive=True)
        with self._lock:
            if self._state != self.DRAFT_PUBLISHED or self._closed or self._draft_revision is None:
                raise RuntimeError("acceptance rollback publication is out of order")
            if current_revision <= self._draft_revision:
                raise ValueError("rollback frame revision must follow draft")
            baseline_revision = self._draft_revision - 1 if saved_revision is None else _exact_int(saved_revision)
            if not baseline_revision < self._draft_revision:
                raise ValueError("saved baseline revision must precede draft")
            if any(type(item) is not bool or item is not True for item in
                   (foreign_rejected, timeout_returned, heartbeat_accepted)):
                raise ValueError("acceptance journey assertions are incomplete")
            saved_digest = hashlib.sha256(frame).hexdigest()
            if saved_digest == self._draft_digest:
                raise ValueError("draft did not differ from saved output")
            self._atomic_file("saved.rgb", frame)
            self._atomic_file("current.rgb", frame)
            try:
                boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            except (OSError, UnicodeError) as error:
                raise OSError("service boot identity is unavailable") from error
            metadata = {
                "schema": 1, **self._transaction,
                "service_boot_id": boot_id, "service_pid": os.getpid(),
                "owner_generation": self.owner_generation,
                "saved_revision": baseline_revision,
                "draft_revision": self._draft_revision,
                "current_revision": current_revision,
                "saved_sha256": saved_digest,
                "draft_sha256": self._draft_digest,
                "current_sha256": saved_digest,
                "width": 64, "height": 32,
                "foreign_rejected": True, "timeout_returned": True,
                "heartbeat_accepted": True, "rollback_complete": True,
            }
            raw = (json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            self._atomic_file("metadata.json", raw)
            self._state = self.SAVED_ROLLBACK_PUBLISHED
            self._completion = {
                "saved_revision": baseline_revision,
                "current_revision": current_revision,
            }
            self._closed = True
            return self.status_unlocked()

    def status_unlocked(self) -> dict[str, object]:
        result: dict[str, object] = {
            "state": self._state, "nonce": self._transaction["nonce"],
            "acceptance_operation_id": self._transaction["acceptance_operation_id"],
            "owner_generation": self.owner_generation,
        }
        if self._draft_revision is not None:
            result["draft_revision"] = self._draft_revision
        result.update(self._completion)
        return result

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
            for name in ("_operation_fd", "_attempt_fd", "_transactions_fd", "_runtime_fd"):
                descriptor = getattr(self, name, None)
                if descriptor is not None:
                    os.close(descriptor)
                    setattr(self, name, None)

    def __del__(self):
        try:
            self.close()
        except OSError:
            pass
