"""Guarded Priority 1.3 release topology (local coordinator)."""
from __future__ import annotations

import argparse
import base64
import binascii
import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import time
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Protocol

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release_argparse import add_store_once_argument


@dataclass(frozen=True)
class ControlResult:
    returncode: int
    digest: str
    terminal: dict[str, object] | None = None


@dataclass(frozen=True)
class OperationResult:
    returncode: int
    unit: str
    cgroup: str
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class OperationInspectResult:
    returncode: int
    state: str
    attempt_id: str
    operation_id: str
    registry: str
    unit: str
    cgroup: str
    registry_digest: str
    exit_status: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    output_digest: str | None = None


@dataclass(frozen=True)
class CanonicalReleaseIdentity:
    release_key: str
    transaction_identity: str
    document: dict[str, object]


_RELEASE_DOCUMENT_FIELDS = frozenset((
    "schema", "transaction_version", "boundary", "expected_sha", "expected_version",
    "previous_sha", "previous_version", "release_stamp", "release_manifest_digest",
    "backup_manifest_digest", "release_spec_digest", "destination_identity", "gitlab",
))


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _domain_digest(domain: str, value: object) -> str:
    domain_bytes = domain.encode("ascii")
    payload = _canonical_json_bytes(value)
    framed = (b"MATRIX-P13\x00" + len(domain_bytes).to_bytes(2, "big") + domain_bytes
              + len(payload).to_bytes(8, "big") + payload)
    return hashlib.sha256(framed).hexdigest()


def _is_schema_v1(value: object) -> bool:
    return type(value) is int and value == 1


def canonical_release_identity(
    document: dict[str, object], *, local_context: object | None = None,
) -> CanonicalReleaseIdentity:
    """Return path-independent, typed/domain-separated semantic release identity.

    ``local_context`` is accepted only to make the exclusion boundary explicit; its
    paths, usernames, environment and mutable files are never read or hashed.
    """
    del local_context
    if type(document) is not dict or frozenset(document) != _RELEASE_DOCUMENT_FIELDS:
        raise ValueError("release document schema is not exact")
    if not _is_schema_v1(document.get("schema")) or type(document.get("transaction_version")) is not int             or not 1 <= document["transaction_version"] <= 65535:
        raise ValueError("release transaction version is invalid")
    if document.get("boundary") not in {"b1", "b2"}:
        raise ValueError("release boundary is invalid")
    for field in ("expected_sha", "previous_sha"):
        if type(document.get(field)) is not str or re.fullmatch(r"[0-9a-f]{40}", document[field]) is None:
            raise ValueError(f"{field} is invalid")
    for field in ("release_manifest_digest", "backup_manifest_digest", "release_spec_digest"):
        if type(document.get(field)) is not str or re.fullmatch(r"[0-9a-f]{64}", document[field]) is None:
            raise ValueError(f"{field} is invalid")
    for field in ("expected_version", "previous_version", "release_stamp"):
        value = document.get(field)
        if type(value) is not str or not value or len(value.encode("utf-8")) > 256 or "\x00" in value:
            raise ValueError(f"{field} is invalid")
    destination = document.get("destination_identity")
    if type(destination) is not dict or frozenset(destination) != {"root", "service"}:
        raise ValueError("destination identity is invalid")
    if (type(destination.get("root")) is not str or not destination["root"]
            or len(destination["root"].encode("utf-8")) > 256 or "\x00" in destination["root"]
            or type(destination.get("service")) is not str
            or re.fullmatch(r"[A-Za-z0-9_.@:-]+\.service", destination["service"]) is None):
        raise ValueError("destination identity is invalid")
    gitlab = document.get("gitlab")
    if (type(gitlab) is not dict or frozenset(gitlab) != {"project", "mr"}
            or any(type(gitlab.get(field)) is not str or not gitlab[field].isdigit()
                   for field in ("project", "mr"))):
        raise ValueError("GitLab identity is invalid")
    canonical = json.loads(_canonical_json_bytes(document).decode("ascii"))
    release_key = _domain_digest("release-key/v1", canonical)
    transaction_identity = _domain_digest(
        "transaction-identity/v1",
        {"release_key": release_key, "transaction_version": canonical["transaction_version"]},
    )
    return CanonicalReleaseIdentity(release_key, transaction_identity, canonical)


class PinnedExecutor(Protocol):
    def control_remote(self, kind: str, request: dict[str, object], *, operation_timeout: int) -> ControlResult: ...
    def run_remote(self, attempt_id: str, operation_id: str, argv: list[str], *, registry: str,
                   run_as: str, operation_timeout: int) -> OperationResult: ...


_TRANSACTION_HELPERS = frozenset((
    "remote-transaction-create-or-query", "remote-transaction-claim",
    "remote-transaction-inspect", "remote-transaction-transition",
    "remote-transaction-advance", "remote-transaction-terminal",
))
_PRIVILEGED_HELPERS = frozenset(("remote-start", "remote-operation-inspect", "remote-stop", "remote-recover", "remote-commit")) | _TRANSACTION_HELPERS
_REQUEST_B64_RE = re.compile(r"[A-Za-z0-9_-]+\Z")


def _canonical_argv_bytes(argv: object) -> bytes:
    if type(argv) is not list or not 1 <= len(argv) <= 256:
        raise ValueError("argv must be a nonempty list with at most 256 entries")
    for argument in argv:
        if type(argument) is not str or "\x00" in argument:
            raise ValueError("every argument must be a NUL-free string")
        try:
            encoded_argument = argument.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("argument is not valid UTF-8") from exc
        if len(encoded_argument) > 4096:
            raise ValueError("argument exceeds 4096 UTF-8 bytes")
    encoded = json.dumps(argv, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 49152:
        raise ValueError("canonical argv exceeds 49152 bytes")
    return encoded


def encode_privileged_argv(argv: list[str]) -> str:
    encoded = base64.urlsafe_b64encode(_canonical_argv_bytes(argv)).rstrip(b"=").decode("ascii")
    if len(encoded) > 65536:
        raise ValueError("encoded request exceeds 65536 bytes")
    return encoded


def decode_privileged_argv(helper: str, request_b64: str) -> list[str]:
    if helper not in _PRIVILEGED_HELPERS:
        raise ValueError("unsupported privileged helper")
    if type(request_b64) is not str or not request_b64 or len(request_b64) > 65536:
        raise ValueError("invalid encoded request size")
    if _REQUEST_B64_RE.fullmatch(request_b64) is None:
        raise ValueError("request is not unpadded base64url")
    try:
        raw = base64.b64decode(
            request_b64 + "=" * (-len(request_b64) % 4), altchars=b"-_", validate=True
        )
        text = raw.decode("utf-8")
        argv = json.loads(text)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid privileged request") from exc
    canonical = _canonical_argv_bytes(argv)
    if encode_privileged_argv(argv) != request_b64 or canonical != raw:
        raise ValueError("noncanonical privileged request")
    return argv


def build_privileged_execution(
    helper: str, request_b64: str, *, executable: str
) -> list[str]:
    argv = decode_privileged_argv(helper, request_b64)
    return [executable, helper, "--", *argv]


def _recovery_execstart(stage: str, request_b64: str) -> list[str]:
    if (
        type(stage) is not str
        or not stage.startswith("/")
        or re.fullmatch(r"/[A-Za-z0-9_./-]+", stage) is None
        or ".." in stage.split("/")
        or stage.endswith("/")
    ):
        raise ValueError("stage must be a normalized absolute path")
    decode_privileged_argv("remote-recover", request_b64)
    return [
        "/home/admin/matrix/venv/bin/python",
        "-I",
        "-B",
        f"{stage}/deploy/guarded_priority_1_3_release.py",
        "remote-recover",
        "--request-b64",
        request_b64,
    ]


def immediate_recovery_execstart(stage: str, request_b64: str) -> list[str]:
    return _recovery_execstart(stage, request_b64)


def watchdog_recovery_execstart(stage: str, request_b64: str) -> list[str]:
    return _recovery_execstart(stage, request_b64)


_ATTEMPT_REGISTRY_FIELDS = frozenset((
    "schema", "attempt_id", "registry", "state", "operations", "watchdog",
    "commit", "recovery", "record_digest",
))
_ATTEMPT_OPERATION_FIELDS = frozenset((
    "attempt_id", "operation_id", "kind", "registry", "request_digest", "unit", "cgroup",
    "stdout_path", "stderr_path", "status", "terminal",
))
_ATTEMPT_OPERATION_TERMINAL_FIELDS = frozenset((
    "exit_status", "stdout", "stderr", "output_digest",
))
_OPERATION_OUTPUT_CAP = 65536
_ATTEMPT_COMMIT_FIELDS = frozenset((
    "request_digest", "request", "unit", "cgroup", "result_digest", "committed_epoch",
))
_ATTEMPT_RECOVERY_FIELDS = frozenset((
    "request_digest", "request", "result_digest", "recovered_epoch",
))
_ATTEMPT_REGISTRY_STATES = frozenset((
    "OPEN", "SEALED_COMMITTING", "SEALED_COMMITTED", "SEALED_RECOVERING", "SEALED_RECOVERED",
))


def _attempt_registry_digest(record: dict[str, object]) -> str:
    body = dict(record)
    body.pop("record_digest", None)
    return _domain_digest("attempt-registry-record/v1", body)


def _valid_finite_epoch(value: object) -> bool:
    return type(value) in {int, float} and math.isfinite(value) and value > 0


def _operation_output_digest(
    attempt_id: str, operation_id: str, registry: str, unit: str, cgroup: str,
    exit_status: int, stdout: str, stderr: str,
) -> str:
    return _domain_digest("operation-output/v1", {
        "attempt_id": attempt_id, "operation_id": operation_id, "registry": registry,
        "unit": unit, "cgroup": cgroup, "exit_status": exit_status,
        "stdout": stdout, "stderr": stderr,
    })


def _validate_attempt_registry_record(
    record: object, *, attempt_id: str, registry_path: str,
) -> dict[str, object]:
    if (type(record) is not dict or frozenset(record) != _ATTEMPT_REGISTRY_FIELDS
            or not _is_schema_v1(record.get("schema"))):
        raise ValueError("attempt registry schema is invalid")
    if record.get("attempt_id") != attempt_id or record.get("registry") != registry_path:
        raise ValueError("attempt registry backlink is invalid")
    if record.get("state") not in _ATTEMPT_REGISTRY_STATES or record.get("watchdog") is not None:
        raise ValueError("attempt registry state is invalid")
    operations = record.get("operations")
    if type(operations) is not dict:
        raise ValueError("attempt registry operations are invalid")
    for operation_id, operation in operations.items():
        if (type(operation_id) is not str or re.fullmatch(r"[0-9a-f]{32}", operation_id) is None
                or type(operation) is not dict or frozenset(operation) != _ATTEMPT_OPERATION_FIELDS):
            raise ValueError("attempt registry operation schema is invalid")
        unit = operation.get("unit")
        if (operation.get("attempt_id") != attempt_id or operation.get("operation_id") != operation_id
                or operation.get("kind") != "remote-start" or operation.get("registry") != registry_path
                or type(unit) is not str
                or re.fullmatch(r"matrix-p13-op-[0-9a-f]{32}-[0-9a-f]{32}\.service", unit) is None
                or operation.get("cgroup") != "/system.slice/" + unit
                or type(operation.get("request_digest")) is not str
                or _DIGEST_RE.fullmatch(operation["request_digest"]) is None
                or operation.get("stdout_path") != f"{registry_path}.outputs/{operation_id}.stdout"
                or operation.get("stderr_path") != f"{registry_path}.outputs/{operation_id}.stderr"
                or operation.get("status") not in {
                    "PRE_START", "RUNNING", "START_FAILED", "TERMINAL", "EXTINCT",
                }):
            raise ValueError("attempt registry operation is invalid")
        terminal = operation.get("terminal")
        if operation["status"] in {"START_FAILED", "TERMINAL"}:
            if (type(terminal) is not dict or frozenset(terminal) != _ATTEMPT_OPERATION_TERMINAL_FIELDS
                    or type(terminal.get("exit_status")) is not int
                    or not 0 <= terminal["exit_status"] <= 255
                    or type(terminal.get("stdout")) is not str
                    or type(terminal.get("stderr")) is not str
                    or len(terminal["stdout"].encode("utf-8")) > _OPERATION_OUTPUT_CAP
                    or len(terminal["stderr"].encode("utf-8")) > _OPERATION_OUTPUT_CAP
                    or terminal.get("output_digest") != _operation_output_digest(
                        attempt_id, operation_id, registry_path, unit, operation["cgroup"],
                        terminal["exit_status"], terminal["stdout"], terminal["stderr"],
                    )):
                raise ValueError("attempt registry operation terminal is invalid")
        elif terminal is not None:
            raise ValueError("nonterminal attempt operation has terminal output")
    commit, recovery, state = record.get("commit"), record.get("recovery"), record["state"]
    if commit is not None:
        if type(commit) is not dict or frozenset(commit) != _ATTEMPT_COMMIT_FIELDS:
            raise ValueError("attempt registry commit schema is invalid")
        request = commit.get("request")
        validate_privileged_request("remote-commit", request)
        if (request.get("query") is True or request.get("attempt_id") != attempt_id
                or request.get("registry") != registry_path
                or commit.get("request_digest") != _request_digest("remote-commit", request)
                or commit.get("unit") != request.get("control_unit")
                or commit.get("cgroup") != request.get("control_cgroup")):
            raise ValueError("attempt registry commit backlink is invalid")
        terminal = state == "SEALED_COMMITTED"
        if ((terminal and (type(commit.get("result_digest")) is not str
                           or re.fullmatch(r"[0-9a-f]{64}", commit["result_digest"]) is None
                           or not _valid_finite_epoch(commit.get("committed_epoch"))))
                or (not terminal and (commit.get("result_digest") is not None
                                      or commit.get("committed_epoch") is not None))):
            raise ValueError("attempt registry commit terminal is invalid")
    if recovery is not None:
        if type(recovery) is not dict or frozenset(recovery) != _ATTEMPT_RECOVERY_FIELDS:
            raise ValueError("attempt registry recovery schema is invalid")
        request = recovery.get("request")
        validate_privileged_request("remote-recover", request)
        if (request.get("query") is True or request.get("attempt_id") != attempt_id
                or request.get("registry") != registry_path
                or recovery.get("request_digest") != canonical_recovery_request_digest(request)):
            raise ValueError("attempt registry recovery backlink is invalid")
        terminal = state == "SEALED_RECOVERED"
        if ((terminal and (type(recovery.get("result_digest")) is not str
                           or re.fullmatch(r"[0-9a-f]{64}", recovery["result_digest"]) is None
                           or not _valid_finite_epoch(recovery.get("recovered_epoch"))))
                or (not terminal and (recovery.get("result_digest") is not None
                                      or recovery.get("recovered_epoch") is not None))):
            raise ValueError("attempt registry recovery terminal is invalid")
    legal = {
        "OPEN": commit is None and recovery is None,
        "SEALED_COMMITTING": commit is not None and recovery is None,
        "SEALED_COMMITTED": commit is not None and recovery is None,
        "SEALED_RECOVERING": recovery is not None and state != "SEALED_COMMITTED",
        "SEALED_RECOVERED": recovery is not None,
    }
    if not legal[state]:
        raise ValueError("attempt registry terminal topology is invalid")
    digest = record.get("record_digest")
    if (type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or digest != _attempt_registry_digest(record)):
        raise ValueError("attempt registry digest is invalid")
    return record


class DurableAttemptRegistry:
    """Root-owned durable attempt state guarded by a stable flock inode."""

    def __init__(self, path: str | os.PathLike[str], attempt_id: str, *, create: bool = False):
        if os.geteuid() != 0:
            raise PermissionError("privileged registry requires root")
        if re.fullmatch(r"[0-9a-f]{32}", attempt_id) is None:
            raise ValueError("attempt ID must be 32 lowercase hexadecimal characters")
        self.path = Path(path)
        if not self.path.is_absolute() or self.path.name in {"", ".", ".."}:
            raise ValueError("registry path must be absolute")
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.attempt_id = attempt_id
        parent = self.path.parent
        metadata = parent.stat(follow_symlinks=False)
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0
                or metadata.st_mode & 0o022):
            raise PermissionError("registry parent must be root-owned and not group/world writable")
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        self._lock_fd = os.open(self.lock_path, flags, 0o600)
        os.fchmod(self._lock_fd, 0o600)
        lock_metadata = os.fstat(self._lock_fd)
        if (not stat.S_ISREG(lock_metadata.st_mode) or lock_metadata.st_uid != 0
                or stat.S_IMODE(lock_metadata.st_mode) != 0o600
                or lock_metadata.st_nlink != 1):
            os.close(self._lock_fd)
            raise PermissionError("registry lock metadata is unsafe")
        with self._locked():
            if create:
                if self.path.exists():
                    record = self._read_locked()
                    if record.get("attempt_id") != attempt_id:
                        raise ValueError("registry attempt identity mismatch")
                else:
                    self._write_locked(self._initial_record())
            else:
                record = self._read_locked()
                if record.get("attempt_id") != attempt_id:
                    raise ValueError("registry attempt identity mismatch")

    def _initial_record(self) -> dict[str, object]:
        record = {
            "schema": 1, "attempt_id": self.attempt_id, "registry": str(self.path),
            "state": "OPEN", "operations": {}, "watchdog": None,
            "commit": None, "recovery": None, "record_digest": "",
        }
        record["record_digest"] = _attempt_registry_digest(record)
        return record

    @contextmanager
    def _locked(self) -> Iterator[None]:
        fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    def _read_locked(self) -> dict[str, object]:
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open(self.path, flags)
        try:
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
                    or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1
                    or metadata.st_size > 1024 * 1024):
                raise PermissionError("registry metadata is unsafe")
            raw = b""
            while len(raw) <= 1024 * 1024:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                raw += chunk
            if len(raw) > 1024 * 1024:
                raise ValueError("registry is oversized")
            try:
                record = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise ValueError("attempt registry is malformed") from exc
        finally:
            os.close(descriptor)
        _validate_attempt_registry_record(
            record, attempt_id=self.attempt_id, registry_path=str(self.path),
        )
        if raw != _canonical_json_bytes(record) + b"\n":
            raise ValueError("attempt registry JSON is not canonical")
        return record

    def _write_locked(self, record: dict[str, object]) -> None:
        record["record_digest"] = _attempt_registry_digest(record)
        _validate_attempt_registry_record(
            record, attempt_id=self.attempt_id, registry_path=str(self.path),
        )
        encoded = _canonical_json_bytes(record) + b"\n"
        parent_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        temporary = f".{self.path.name}.{secrets.token_hex(16)}"
        descriptor = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                 os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
            os.fchmod(descriptor, 0o600)
            os.fchown(descriptor, 0, 0)
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short registry write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, self.path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(parent_fd)

    def snapshot(self) -> dict[str, object]:
        with self._locked():
            return json.loads(json.dumps(self._read_locked()))

    def mutate(self, callback: Callable[[dict[str, object]], object]) -> object:
        with self._locked():
            record = self._read_locked()
            result = callback(record)
            self._write_locked(record)
            return result

    def register_operation(
        self, operation_id: str, request_digest: str, unit: str, cgroup: str,
        stdout_path: str, stderr_path: str,
    ) -> bool:
        if re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
            raise ValueError("operation ID must be 32 lowercase hexadecimal characters")
        def update(record: dict[str, object]) -> bool:
            operations = record.get("operations")
            if record.get("state") != "OPEN" or type(operations) is not dict:
                return False
            existing = operations.get(operation_id)
            if existing is not None:
                return bool(type(existing) is dict and existing.get("request_digest") == request_digest
                            and existing.get("unit") == unit and existing.get("cgroup") == cgroup
                            and existing.get("stdout_path") == stdout_path
                            and existing.get("stderr_path") == stderr_path)
            operations[operation_id] = {
                "attempt_id": self.attempt_id, "operation_id": operation_id,
                "kind": "remote-start", "registry": str(self.path), "request_digest": request_digest,
                "unit": unit, "cgroup": cgroup, "stdout_path": stdout_path,
                "stderr_path": stderr_path, "status": "PRE_START", "terminal": None,
            }
            return True
        return bool(self.mutate(update))

    def transition_operation_start(
        self, operation_id: str, request_digest: str, *,
        to_status: str, terminal: dict[str, object] | None = None,
    ) -> bool:
        if to_status not in {"RUNNING", "START_FAILED"}:
            raise ValueError("operation start transition is invalid")
        def update(record: dict[str, object]) -> bool:
            operations = record.get("operations")
            if type(operations) is not dict:
                return False
            operation = operations.get(operation_id)
            if (type(operation) is not dict or operation.get("request_digest") != request_digest):
                return False
            if operation.get("status") == to_status and operation.get("terminal") == terminal:
                return True
            if operation.get("status") != "PRE_START" or operation.get("terminal") is not None:
                return False
            operation["status"] = to_status
            operation["terminal"] = terminal
            return True
        return bool(self.mutate(update))


_TRANSACTION_TOPOLOGY = (
    ("initial-deploy", "deploy"), ("rollback-proof", "rollback"), ("final-deploy", "deploy"),
)
_ACCEPTANCE_INVOCATIONS = frozenset((
    "candidate", "recovery-verification", "final-verification",
))


def _canonical_acceptance_operation_id(
    attempt_id: str, phase: str, topology_operation: str, topology_index: int,
    invocation: str,
) -> str:
    if (type(attempt_id) is not str or _ID_RE.fullmatch(attempt_id) is None
            or type(topology_index) is not int
            or not 0 <= topology_index < len(_TRANSACTION_TOPOLOGY)
            or _TRANSACTION_TOPOLOGY[topology_index] != (phase, topology_operation)
            or invocation not in _ACCEPTANCE_INVOCATIONS
            or invocation == "final-verification" and phase != "final-deploy"):
        raise ValueError("acceptance operation identity authority is invalid")
    return _domain_digest("acceptance-operation-id/v1", {
        "attempt_id": attempt_id, "phase": phase,
        "topology_operation": topology_operation, "topology_index": topology_index,
        "invocation": invocation,
    })

_ATTEMPT_TRANSITIONS = {
    "BLOCKED": frozenset(("READY",)),
    "READY": frozenset(("GUARD_ARMING",)),
    "GUARD_ARMING": frozenset(("GUARDED",)),
    "GUARDED": frozenset(("MUTATION_RUNNING", "SEALED_RECOVERING")),
    "MUTATION_RUNNING": frozenset(("MUTATION_DONE", "SEALED_RECOVERING")),
    "MUTATION_DONE": frozenset(("ACCEPTANCE_RUNNING", "SEALED_RECOVERING")),
    "ACCEPTANCE_RUNNING": frozenset(("CANDIDATE_DURABLE", "SEALED_RECOVERING")),
    "CANDIDATE_DURABLE": frozenset(("PENDING_BOUND", "SEALED_RECOVERING")),
    "PENDING_BOUND": frozenset(("SEALED_COMMITTING", "SEALED_RECOVERING")),
    "COMMITTED_UNRECONCILED": frozenset(("ACCEPTED",)),
    "RECOVERED_UNVERIFIED": frozenset(("ROLLBACK_VERIFIED",)),
}
_TRANSACTION_RECORD_FIELDS = frozenset((
    "schema", "release_key", "release_document", "execution_contract", "transaction_id",
    "transaction_identity", "state", "created_epoch", "original_deadline_epoch",
    "current_phase_index", "attempts", "claim", "final", "last_advance", "record_digest",
))
_ATTEMPT_RECORD_FIELDS = frozenset((
    "phase", "operation", "attempt_id", "attempt_identity", "registry", "state", "watchdog",
    "candidate", "pending_note", "commit", "acceptance", "recovery", "last_transition",
))


def transaction_record_digest(record: dict[str, object]) -> str:
    if type(record) is not dict:
        raise ValueError("transaction record is invalid")
    unsigned = dict(record)
    unsigned.pop("record_digest", None)
    return _domain_digest("transaction-record/v1", unsigned)


_TRANSITION_FIELDS = frozenset(("from_state", "to_state", "request_digest", "transition_epoch"))
_TERMINAL_FIELDS = frozenset((
    "kind", "from_state", "to_state", "request_digest", "result_digest", "terminal_epoch",
))
_CLAIM_FIELDS = frozenset(("claimant_id", "request_digest", "claimed_epoch"))
_ADVANCE_FIELDS = frozenset(("phase", "request_digest", "transition_epoch"))
_ATTEMPT_STATES = frozenset((
    "BLOCKED", "READY", "GUARD_ARMING", "GUARDED", "MUTATION_RUNNING", "MUTATION_DONE",
    "ACCEPTANCE_RUNNING", "CANDIDATE_DURABLE", "PENDING_BOUND", "SEALED_COMMITTING",
    "SEALED_RECOVERING", "COMMITTED_UNRECONCILED", "ACCEPTED", "RECOVERED_UNVERIFIED",
    "ROLLBACK_VERIFIED",
))


def _canonical_attempt_id(
    transaction_identity: str, transaction_id: str, phase: str, operation: str, index: int,
) -> str:
    return _domain_digest("attempt-id/v1", {
        "transaction_identity": transaction_identity, "transaction_id": transaction_id,
        "phase": phase, "operation": operation, "index": index,
    })[:32]


def _valid_digest(value: object) -> bool:
    return type(value) is str and _DIGEST_RE.fullmatch(value) is not None


def _valid_id(value: object) -> bool:
    return type(value) is str and _ID_RE.fullmatch(value) is not None


def _validate_execution_service_coherence(
    release_document: dict[str, object], execution_contract: dict[str, object],
) -> None:
    destination = release_document["destination_identity"]
    if execution_contract["service"] != destination["service"]:
        raise ValueError("execution service does not match release destination")


def _validate_epoch(value: object, record: dict[str, object]) -> None:
    if (type(value) is not int or value < record["created_epoch"]
            or value >= record["original_deadline_epoch"]):
        raise ValueError("transaction metadata epoch is outside its immutable lease")


def _validate_transition(value: object, state: str, record: dict[str, object]) -> None:
    if (type(value) is not dict or frozenset(value) != _TRANSITION_FIELDS
            or value.get("to_state") != state or not _valid_digest(value.get("request_digest"))
            or type(value.get("from_state")) is not str
            or state not in _ATTEMPT_TRANSITIONS.get(value["from_state"], frozenset())):
        raise ValueError("transaction transition metadata is invalid")
    _validate_epoch(value.get("transition_epoch"), record)


def _validate_terminal(value: object, state: str, record: dict[str, object]) -> None:
    if (type(value) is not dict or frozenset(value) != _TERMINAL_FIELDS
            or not _valid_digest(value.get("request_digest"))
            or not _valid_digest(value.get("result_digest"))):
        raise ValueError("transaction terminal metadata is invalid")
    expected = {
        "COMMITTED_UNRECONCILED": ("commit", "SEALED_COMMITTING"),
        "RECOVERED_UNVERIFIED": ("recovery", "SEALED_RECOVERING"),
    }.get(state)
    if expected is None or (value.get("kind"), value.get("from_state"), value.get("to_state")) != (
            expected[0], expected[1], state):
        raise ValueError("transaction terminal metadata does not match state")
    _validate_epoch(value.get("terminal_epoch"), record)


def validate_transaction_record(record: object, release_key: str) -> dict[str, object]:
    """Validate the complete canonical durable transaction schema and state graph."""
    if (type(record) is not dict or frozenset(record) != _TRANSACTION_RECORD_FIELDS
            or not _is_schema_v1(record.get("schema")) or record.get("release_key") != release_key
            or record.get("record_digest") != transaction_record_digest(record)):
        raise ValueError("transaction record schema or digest is invalid")
    identity = canonical_release_identity(record.get("release_document"))
    transaction_id = record.get("transaction_id")
    if (identity.release_key != release_key
            or record.get("transaction_identity") != identity.transaction_identity
            or not _valid_id(transaction_id)):
        raise ValueError("transaction identity is invalid")
    contract = record.get("execution_contract")
    if (type(contract) is not dict or frozenset(contract) != {
            "schema", "stage", "bundle", "destination_root", "service"
        } or not _is_schema_v1(contract.get("schema"))):
        raise ValueError("transaction execution contract is invalid")
    for field in ("stage", "bundle", "destination_root"):
        _absolute(contract.get(field), field)
    if type(contract.get("service")) is not str or _SERVICE_RE.fullmatch(contract["service"]) is None:
        raise ValueError("transaction execution service is invalid")
    _validate_execution_service_coherence(identity.document, contract)
    created, deadline = record.get("created_epoch"), record.get("original_deadline_epoch")
    if (type(created) is not int or type(deadline) is not int or created < 0
            or not 60 <= deadline - created <= 86400):
        raise ValueError("transaction immutable lease is invalid")

    claim = record.get("claim")
    if claim is not None:
        if (type(claim) is not dict or frozenset(claim) != _CLAIM_FIELDS
                or not _valid_id(claim.get("claimant_id"))
                or not _valid_digest(claim.get("request_digest"))):
            raise ValueError("transaction claim schema is invalid")
        _validate_epoch(claim.get("claimed_epoch"), record)

    attempts = record.get("attempts")
    if type(attempts) is not list or len(attempts) != len(_TRANSACTION_TOPOLOGY):
        raise ValueError("transaction attempt topology is invalid")
    attempt_ids: set[str] = set()
    for index, attempt in enumerate(attempts):
        phase, operation = _TRANSACTION_TOPOLOGY[index]
        if type(attempt) is not dict or frozenset(attempt) != _ATTEMPT_RECORD_FIELDS:
            raise ValueError("transaction attempt schema is invalid")
        attempt_id = attempt.get("attempt_id")
        canonical_attempt_id = _canonical_attempt_id(
            identity.transaction_identity, transaction_id, phase, operation, index,
        )
        expected_identity = _domain_digest("attempt-identity/v1", {
            "transaction_identity": identity.transaction_identity, "transaction_id": transaction_id,
            "phase": phase, "operation": operation, "index": index, "attempt_id": attempt_id,
        })
        if ((attempt.get("phase"), attempt.get("operation")) != (phase, operation)
                or attempt_id != canonical_attempt_id or attempt_id in attempt_ids
                or attempt.get("attempt_identity") != expected_identity
                or attempt.get("registry") != f"/run/matrix-priority-1-3/attempts/{attempt_id}.json"):
            raise ValueError("transaction attempt identity or registry backlink is invalid")
        attempt_ids.add(attempt_id)
        state = attempt.get("state")
        if state not in _ATTEMPT_STATES:
            raise ValueError("transaction attempt state is invalid")
        if any(attempt.get(field) is not None for field in (
                "watchdog", "candidate", "pending_note", "acceptance")):
            raise ValueError("unsupported transaction attempt metadata is populated")
        transition, commit, recovery = (attempt.get("last_transition"), attempt.get("commit"),
                                        attempt.get("recovery"))
        if state in {"BLOCKED", "READY"}:
            if transition is not None or commit is not None or recovery is not None:
                raise ValueError("idle transaction attempt has terminal metadata")
        elif state in {"COMMITTED_UNRECONCILED", "RECOVERED_UNVERIFIED"}:
            terminal = commit if state == "COMMITTED_UNRECONCILED" else recovery
            if terminal is None or (recovery if state == "COMMITTED_UNRECONCILED" else commit) is not None:
                raise ValueError("transaction attempt terminal ownership is invalid")
            _validate_terminal(terminal, state, record)
            if transition != terminal:
                raise ValueError("transaction terminal backlink is invalid")
        elif state in {"ACCEPTED", "ROLLBACK_VERIFIED"}:
            owned = commit if state == "ACCEPTED" else recovery
            terminal_state = "COMMITTED_UNRECONCILED" if state == "ACCEPTED" else "RECOVERED_UNVERIFIED"
            if owned is None or (recovery if state == "ACCEPTED" else commit) is not None:
                raise ValueError("transaction completed attempt terminal is invalid")
            _validate_terminal(owned, terminal_state, record)
            _validate_transition(transition, state, record)
            if transition["from_state"] != terminal_state:
                raise ValueError("transaction completed attempt transition is invalid")
        else:
            if commit is not None or recovery is not None:
                raise ValueError("nonterminal transaction attempt has terminal metadata")
            _validate_transition(transition, state, record)

    index = record.get("current_phase_index")
    state = record.get("state")
    if type(index) is not int or not 0 <= index < len(attempts) or state not in {
            "TX_OPEN", "THREE_ATTEMPTS_ACCEPTED"}:
        raise ValueError("transaction state or current phase is invalid")
    expected_completed = ("ACCEPTED", "ROLLBACK_VERIFIED", "ACCEPTED")
    if any(attempts[position]["state"] != expected_completed[position] for position in range(index)):
        raise ValueError("completed transaction phase topology is invalid")
    if any(attempts[position]["state"] != "BLOCKED" for position in range(index + 1, len(attempts))):
        raise ValueError("future transaction phase topology is invalid")
    if state == "THREE_ATTEMPTS_ACCEPTED":
        if index != 2 or attempts[2]["state"] != "ACCEPTED":
            raise ValueError("transaction completion state is inconsistent")
    elif attempts[index]["state"] == "BLOCKED":
        raise ValueError("current transaction attempt cannot be blocked")

    advance = record.get("last_advance")
    if index == 0:
        if advance is not None:
            raise ValueError("initial transaction phase has advance metadata")
    else:
        expected_advance_phase = (_TRANSACTION_TOPOLOGY[index][0]
                                  if state == "THREE_ATTEMPTS_ACCEPTED"
                                  else _TRANSACTION_TOPOLOGY[index - 1][0])
        if (type(advance) is not dict or frozenset(advance) != _ADVANCE_FIELDS
                or advance.get("phase") != expected_advance_phase
                or not _valid_digest(advance.get("request_digest"))):
            raise ValueError("transaction advance metadata is invalid")
        _validate_epoch(advance.get("transition_epoch"), record)
    if state == "THREE_ATTEMPTS_ACCEPTED":
        if (type(advance) is not dict or advance.get("phase") != _TRANSACTION_TOPOLOGY[2][0]):
            raise ValueError("final transaction advance metadata is invalid")
    if claim is None:
        if (state != "TX_OPEN" or index != 0 or attempts[0]["state"] != "READY"
                or advance is not None):
            raise ValueError("unclaimed transaction contains progress")
    if record.get("final") is not None:
        raise ValueError("unsupported transaction final metadata is populated")
    return record


def _open_directory_without_symlinks(path: str) -> int:
    if not os.path.isabs(path) or os.path.normpath(path) != path:
        raise ValueError("registry root must be normalized and absolute")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.split("/")[1:]:
            if not component:
                continue
            next_descriptor = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class DurableTransactionRegistry:
    """Sole durable release authority, confined beneath one root-owned directory."""

    _MAX_RECORDS = 256
    _MAX_RECORD_BYTES = 1024 * 1024

    def __init__(self, root: str, release_key: str):
        if os.geteuid() != 0:
            raise PermissionError("transaction registry requires root")
        if _DIGEST_RE.fullmatch(release_key) is None:
            raise ValueError("release key is invalid")
        self.root = root
        self.release_key = release_key
        self.record_name = f"{release_key}.json"
        self.lock_name = f"{release_key}.lock"
        self._root_fd = _open_directory_without_symlinks(root)
        metadata = os.fstat(self._root_fd)
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0
                or stat.S_IMODE(metadata.st_mode) != 0o700):
            os.close(self._root_fd)
            raise PermissionError("transaction registry root must be root-owned mode 0700")
        self._lock_fd = os.open(
            self.lock_name, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600, dir_fd=self._root_fd,
        )
        os.fchmod(self._lock_fd, 0o600)
        os.fchown(self._lock_fd, 0, 0)
        lock_metadata = os.fstat(self._lock_fd)
        if (not stat.S_ISREG(lock_metadata.st_mode) or lock_metadata.st_uid != 0
                or stat.S_IMODE(lock_metadata.st_mode) != 0o600 or lock_metadata.st_nlink != 1):
            self.close()
            raise PermissionError("transaction lock metadata is unsafe")

    def close(self) -> None:
        lock_fd, root_fd = getattr(self, "_lock_fd", None), getattr(self, "_root_fd", None)
        self._lock_fd = self._root_fd = None
        if lock_fd is not None:
            os.close(lock_fd)
        if root_fd is not None:
            os.close(root_fd)

    @contextmanager
    def locked(self) -> Iterator[None]:
        if self._lock_fd is None:
            raise RuntimeError("transaction registry is closed")
        fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    def _validate_record(self, record: object) -> dict[str, object]:
        return validate_transaction_record(record, self.release_key)

    def _read_locked(self) -> dict[str, object]:
        descriptor = os.open(
            self.record_name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=self._root_fd
        )
        try:
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
                    or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1
                    or metadata.st_size > self._MAX_RECORD_BYTES):
                raise PermissionError("transaction record metadata is unsafe")
            chunks, size = [], 0
            while True:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > self._MAX_RECORD_BYTES:
                    raise ValueError("transaction record is oversized")
                chunks.append(chunk)
        finally:
            os.close(descriptor)
        raw = b"".join(chunks)
        try:
            record = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("transaction record is malformed") from exc
        validated = self._validate_record(record)
        if raw != _canonical_json_bytes(validated) + b"\n":
            raise ValueError("transaction record JSON is not canonical")
        return validated

    def _write_locked(self, record: dict[str, object]) -> None:
        record["record_digest"] = transaction_record_digest(record)
        self._validate_record(record)
        encoded = _canonical_json_bytes(record) + b"\n"
        if len(encoded) > self._MAX_RECORD_BYTES:
            raise ValueError("transaction record is oversized")
        temporary = f".{self.record_name}.{secrets.token_hex(16)}"
        descriptor = None
        try:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600, dir_fd=self._root_fd,
            )
            os.fchmod(descriptor, 0o600)
            os.fchown(descriptor, 0, 0)
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short transaction record write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, self.record_name, src_dir_fd=self._root_fd, dst_dir_fd=self._root_fd)
            os.fsync(self._root_fd)
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=self._root_fd)
            except OSError:
                pass
            raise

    def _snapshot(self, record: dict[str, object]) -> dict[str, object]:
        return json.loads(_canonical_json_bytes(record).decode("ascii"))

    def create_or_query(self, request: dict[str, object]) -> dict[str, object]:
        with self.locked():
            try:
                existing = self._read_locked()
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if (existing["release_document"] != request["release_document"]
                        or existing["execution_contract"] != request["execution_contract"]):
                    raise ValueError("transaction create request mismatches existing release")
                return self._snapshot(existing)
            records = 0
            for name in os.listdir(self._root_fd):
                if name.endswith(".json"):
                    metadata = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
                    if not stat.S_ISREG(metadata.st_mode):
                        raise PermissionError("transaction registry contains unsafe record")
                    records += 1
            if records >= self._MAX_RECORDS:
                raise RuntimeError("transaction registry lifecycle bound reached")
            identity = canonical_release_identity(request["release_document"])
            transaction_id = secrets.token_hex(16)
            attempts = []
            for index, (phase, operation) in enumerate(_TRANSACTION_TOPOLOGY):
                attempt_id = _canonical_attempt_id(
                    identity.transaction_identity, transaction_id, phase, operation, index,
                )
                attempts.append({
                    "phase": phase, "operation": operation, "attempt_id": attempt_id,
                    "attempt_identity": _domain_digest("attempt-identity/v1", {
                        "transaction_identity": identity.transaction_identity, "transaction_id": transaction_id,
                        "phase": phase, "operation": operation, "index": index, "attempt_id": attempt_id,
                    }),
                    "registry": f"/run/matrix-priority-1-3/attempts/{attempt_id}.json",
                    "state": "READY" if index == 0 else "BLOCKED", "watchdog": None,
                    "candidate": None, "pending_note": None, "commit": None,
                    "acceptance": None, "recovery": None, "last_transition": None,
                })
            record = {
                "schema": 1, "release_key": self.release_key,
                "release_document": json.loads(_canonical_json_bytes(request["release_document"]).decode("ascii")),
                "execution_contract": json.loads(_canonical_json_bytes(request["execution_contract"]).decode("ascii")),
                "transaction_id": transaction_id, "transaction_identity": identity.transaction_identity,
                "state": "TX_OPEN", "created_epoch": request["created_epoch"],
                "original_deadline_epoch": request["original_deadline_epoch"],
                "current_phase_index": 0, "attempts": attempts, "claim": None,
                "final": None, "last_advance": None, "record_digest": "",
            }
            self._write_locked(record)
            return self._snapshot(record)

    def mutate(self, callback: Callable[[dict[str, object]], None]) -> dict[str, object]:
        with self.locked():
            record = self._read_locked()
            callback(record)
            self._write_locked(record)
            return self._snapshot(record)

    def inspect(self, transaction_id: str) -> dict[str, object]:
        with self.locked():
            record = self._read_locked()
            self._require_transaction(record, transaction_id)
            return self._snapshot(record)

    @staticmethod
    def _require_transaction(record: dict[str, object], transaction_id: str) -> None:
        if record.get("transaction_id") != transaction_id:
            raise ValueError("transaction ID mismatch")

    @staticmethod
    def _require_claim(record: dict[str, object], claimant_id: str) -> None:
        claim = record.get("claim")
        if type(claim) is not dict or claim.get("claimant_id") != claimant_id:
            raise ValueError("transaction claimant mismatch")

    @staticmethod
    def _require_live_epoch(record: dict[str, object], value: object) -> None:
        _validate_epoch(value, record)

    @staticmethod
    def _current_attempt(record: dict[str, object], phase: str) -> dict[str, object]:
        index = record.get("current_phase_index")
        if type(index) is not int or not 0 <= index < 3:
            raise ValueError("transaction phase index is invalid")
        attempt = record["attempts"][index]
        if attempt.get("phase") != phase:
            raise ValueError("transaction phase is not current")
        return attempt

    def claim(self, request: dict[str, object]) -> dict[str, object]:
        def update(record: dict[str, object]) -> None:
            self._require_transaction(record, request["transaction_id"])
            self._require_live_epoch(record, request["claimed_epoch"])
            expected = {"claimant_id": request["claimant_id"], "request_digest": request["request_digest"],
                        "claimed_epoch": request["claimed_epoch"]}
            if record["claim"] is None:
                record["claim"] = expected
            elif record["claim"] != expected:
                raise ValueError("transaction already has a different claimant")
        return self.mutate(update)

    def transition(self, request: dict[str, object]) -> dict[str, object]:
        def update(record: dict[str, object]) -> None:
            self._require_transaction(record, request["transaction_id"])
            self._require_claim(record, request["claimant_id"])
            self._require_live_epoch(record, request["transition_epoch"])
            attempt = self._current_attempt(record, request["phase"])
            transition = {"from_state": request["from_state"], "to_state": request["to_state"],
                          "request_digest": request["request_digest"],
                          "transition_epoch": request["transition_epoch"]}
            if attempt["state"] == request["to_state"] and attempt["last_transition"] == transition:
                return
            allowed = _ATTEMPT_TRANSITIONS.get(request["from_state"], frozenset())
            if attempt["state"] != request["from_state"] or request["to_state"] not in allowed:
                raise ValueError("transaction transition is not monotonic")
            attempt["state"] = request["to_state"]
            attempt["last_transition"] = transition
        return self.mutate(update)

    def terminal(self, request: dict[str, object]) -> dict[str, object]:
        def update(record: dict[str, object]) -> None:
            self._require_transaction(record, request["transaction_id"])
            self._require_claim(record, request["claimant_id"])
            self._require_live_epoch(record, request["terminal_epoch"])
            attempt = self._current_attempt(record, request["phase"])
            field = "commit" if request["kind"] == "commit" else "recovery"
            terminal = {"kind": request["kind"], "from_state": request["from_state"],
                        "to_state": request["to_state"], "request_digest": request["request_digest"],
                        "result_digest": request["result_digest"], "terminal_epoch": request["terminal_epoch"]}
            if attempt["state"] == request["to_state"] and attempt[field] == terminal:
                return
            expected = (("SEALED_COMMITTING", "COMMITTED_UNRECONCILED") if field == "commit"
                        else ("SEALED_RECOVERING", "RECOVERED_UNVERIFIED"))
            if (request["from_state"], request["to_state"]) != expected \
                    or attempt["state"] != request["from_state"] or attempt[field] is not None:
                raise ValueError("transaction terminal is not admissible")
            attempt[field] = terminal
            attempt["state"] = request["to_state"]
            attempt["last_transition"] = terminal
        return self.mutate(update)

    def advance(self, request: dict[str, object]) -> dict[str, object]:
        def update(record: dict[str, object]) -> None:
            self._require_transaction(record, request["transaction_id"])
            self._require_claim(record, request["claimant_id"])
            self._require_live_epoch(record, request["transition_epoch"])
            expected = {"phase": request["phase"], "request_digest": request["request_digest"],
                        "transition_epoch": request["transition_epoch"]}
            index = record["current_phase_index"]
            if record["last_advance"] == expected:
                if index == min(2, _TRANSACTION_TOPOLOGY.index((request["phase"],
                                     "rollback" if request["phase"] == "rollback-proof" else "deploy")) + 1):
                    return
            attempt = self._current_attempt(record, request["phase"])
            if attempt["state"] not in {"ACCEPTED", "ROLLBACK_VERIFIED"}:
                raise ValueError("transaction phase has no advance terminal")
            if index == 2:
                record["state"] = "THREE_ATTEMPTS_ACCEPTED"
            else:
                next_attempt = record["attempts"][index + 1]
                if next_attempt["state"] != "BLOCKED":
                    raise ValueError("next transaction phase is not blocked")
                next_attempt["state"] = "READY"
                record["current_phase_index"] = index + 1
            record["last_advance"] = expected
        return self.mutate(update)


class SystemdController:
    """Injectable argv-only systemd operation and extinction controller."""

    _SYSTEMCTL = "/usr/bin/systemctl"
    _SYSTEMD_RUN = "/usr/bin/systemd-run"
    _ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
    _TERMINAL_STATES = frozenset(("inactive", "failed", "dead"))
    _TERMINAL_UNIT_STATES = frozenset((
        ("active", "exited"), ("inactive", "dead"), ("failed", "failed"),
    ))

    def __init__(self, *, runner: Callable[..., object] | None = None,
                 cgroup_root: str | os.PathLike[str] = "/sys/fs/cgroup",
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 checkpoint: Callable[[str], None] | None = None):
        self._runner = runner or self._run
        self.cgroup_root = Path(cgroup_root)
        self.clock = clock
        self.sleep = sleep
        self.checkpoint = checkpoint or (lambda _name: None)

    @staticmethod
    def _run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        if not argv or argv[0] not in {SystemdController._SYSTEMCTL, SystemdController._SYSTEMD_RUN}:
            raise ValueError("systemd executable is not an approved absolute path")
        return subprocess.run(argv, check=False, text=True, capture_output=True,
                              timeout=timeout, shell=False, env=SystemdController._ENVIRONMENT)

    def _command(self, argv: list[str], timeout: float = 5) -> object:
        result = self._runner(argv, timeout=timeout)
        if (type(getattr(result, "returncode", None)) is not int
                or type(getattr(result, "stdout", None)) is not str
                or type(getattr(result, "stderr", None)) is not str):
            raise RuntimeError("systemd runner returned an invalid result")
        return result

    @staticmethod
    def _prepare_output_file(path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=False, exist_ok=True)
        parent = path.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode)
                or parent.st_uid != 0 or stat.S_IMODE(parent.st_mode) != 0o700):
            raise PermissionError("operation output directory is unsafe")
        try:
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            descriptor = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
            existing = os.fstat(descriptor)
            if (not stat.S_ISREG(existing.st_mode) or existing.st_uid != 0
                    or stat.S_IMODE(existing.st_mode) != 0o600 or existing.st_nlink != 1
                    or existing.st_size != 0):
                os.close(descriptor)
                raise PermissionError("existing pre-start output artifact is unsafe")
        try:
            os.fchmod(descriptor, 0o600)
            os.fchown(descriptor, 0, 0)
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
                    or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
                raise PermissionError("operation output artifact is unsafe")
        finally:
            os.close(descriptor)

    @staticmethod
    def _read_output_file(path: str) -> str:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
                    or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1
                    or metadata.st_size > _OPERATION_OUTPUT_CAP):
                raise ValueError("operation output artifact is unsafe or oversized")
            raw = b""
            while len(raw) <= _OPERATION_OUTPUT_CAP:
                chunk = os.read(descriptor, min(65536, _OPERATION_OUTPUT_CAP + 1 - len(raw)))
                if not chunk:
                    break
                raw += chunk
            if len(raw) > _OPERATION_OUTPUT_CAP:
                raise ValueError("operation output exceeds cap")
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("operation output is not canonical UTF-8") from exc
        finally:
            os.close(descriptor)

    @staticmethod
    def _inspect_result(record: dict[str, object], operation: dict[str, object]) -> OperationInspectResult:
        terminal = operation["terminal"]
        return OperationInspectResult(
            0, str(operation["status"]), str(operation["attempt_id"]),
            str(operation["operation_id"]), str(operation["registry"]), str(operation["unit"]),
            str(operation["cgroup"]), str(record["record_digest"]),
            None if terminal is None else int(terminal["exit_status"]),
            None if terminal is None else str(terminal["stdout"]),
            None if terminal is None else str(terminal["stderr"]),
            None if terminal is None else str(terminal["output_digest"]),
        )

    def start(self, request: dict[str, object], registry: DurableAttemptRegistry) -> OperationResult:
        validate_privileged_request("remote-start", request)
        attempt = str(request["attempt_id"])
        operation = str(request["operation_id"])
        unit = f"matrix-p13-op-{attempt}-{operation}.service"
        cgroup = f"/system.slice/{unit}"
        request_digest = _request_digest("remote-start", request)
        output_root = Path(str(registry.path) + ".outputs")
        stdout_path = output_root / f"{operation}.stdout"
        stderr_path = output_root / f"{operation}.stderr"
        existing = registry.snapshot()["operations"].get(operation)

        def failed_terminal(returncode: object, stdout: object, stderr: object) -> OperationResult:
            code = returncode if type(returncode) is int and 1 <= returncode <= 255 else 2
            if (type(stdout) is not str or type(stderr) is not str
                    or len(stdout.encode("utf-8")) > _OPERATION_OUTPUT_CAP
                    or len(stderr.encode("utf-8")) > _OPERATION_OUTPUT_CAP):
                raise RuntimeError("systemd admission failure output is invalid")
            terminal = {
                "exit_status": code, "stdout": stdout, "stderr": stderr,
                "output_digest": _operation_output_digest(
                    attempt, operation, str(registry.path), unit, cgroup, code, stdout, stderr,
                ),
            }
            if not registry.transition_operation_start(
                    operation, request_digest, to_status="START_FAILED", terminal=terminal):
                raise RuntimeError("failed operation admission could not become durable")
            return OperationResult(code, unit, cgroup, stdout, stderr)

        def reconcile_pre_start() -> OperationResult:
            shown = self._command([
                self._SYSTEMCTL, "show", unit, "--property=LoadState", "--property=ControlGroup",
            ])
            values: dict[str, str] = {}
            for line in shown.stdout.splitlines():
                if "=" not in line:
                    return OperationResult(2, unit, cgroup)
                key, value = line.split("=", 1)
                if key not in {"LoadState", "ControlGroup"} or key in values:
                    return OperationResult(2, unit, cgroup)
                values[key] = value
            if set(values) != {"LoadState", "ControlGroup"}:
                return OperationResult(2, unit, cgroup)
            if shown.returncode == 0 and values == {"LoadState": "loaded", "ControlGroup": cgroup}:
                if not registry.transition_operation_start(
                        operation, request_digest, to_status="RUNNING"):
                    return OperationResult(2, unit, cgroup)
                return OperationResult(0, unit, cgroup)
            if values == {"LoadState": "not-found", "ControlGroup": ""}:
                return failed_terminal(2, "", "systemd unit was proven absent before admission\n")
            return OperationResult(2, unit, cgroup)

        if existing is not None:
            if (type(existing) is not dict or existing.get("request_digest") != request_digest
                    or existing.get("unit") != unit or existing.get("cgroup") != cgroup
                    or existing.get("stdout_path") != str(stdout_path)
                    or existing.get("stderr_path") != str(stderr_path)):
                return OperationResult(2, unit, cgroup)
            if existing.get("status") == "PRE_START":
                return reconcile_pre_start()
            if existing.get("status") == "START_FAILED":
                terminal = existing.get("terminal")
                if type(terminal) is not dict:
                    return OperationResult(2, unit, cgroup)
                return OperationResult(
                    int(terminal["exit_status"]), unit, cgroup,
                    str(terminal["stdout"]), str(terminal["stderr"]),
                )
            return OperationResult(0, unit, cgroup)
        self._prepare_output_file(stdout_path)
        self._prepare_output_file(stderr_path)
        self.checkpoint("after-output-creation")
        if not registry.register_operation(
                operation, request_digest, unit, cgroup, str(stdout_path), str(stderr_path)):
            return OperationResult(2, unit, cgroup)
        self.checkpoint("after-pre-start-publication")
        command = [
            self._SYSTEMD_RUN, f"--unit={unit}", "--property=Type=exec",
            "--property=KillMode=control-group", "--property=TimeoutStopSec=5s",
            "--property=SendSIGKILL=yes", f"--property=User={request['run_as']}",
            f"--property=StandardOutput=file:{stdout_path}",
            f"--property=StandardError=file:{stderr_path}",
        ]
        watchdog_operation = hashlib.sha256(
            f"{attempt}\0watchdog-arm".encode("utf-8")
        ).hexdigest()[:32]
        if operation == watchdog_operation:
            command.append("--collect")
        else:
            command.append("--property=RemainAfterExit=yes")
        command.extend(("--no-block", "--", *request["argv"]))
        result = self._command(command)
        if result.returncode != 0:
            return failed_terminal(result.returncode, result.stdout, result.stderr)
        self.checkpoint("after-systemd-admission")
        shown = self._command([self._SYSTEMCTL, "show", unit, "--property=ControlGroup", "--value"])
        if shown.returncode != 0 or shown.stdout.strip() != cgroup:
            return OperationResult(2, unit, cgroup)
        self.checkpoint("after-unit-identity")
        if not registry.transition_operation_start(
                operation, request_digest, to_status="RUNNING"):
            return OperationResult(2, unit, cgroup)
        self.checkpoint("after-running-publication")
        return OperationResult(0, unit, cgroup)

    def inspect(
        self, request: dict[str, object], registry: DurableAttemptRegistry,
    ) -> OperationInspectResult:
        validate_privileged_request("remote-operation-inspect", request)
        record = registry.snapshot()
        operation = record["operations"].get(request["operation_id"])
        if (type(operation) is not dict or operation.get("attempt_id") != request["attempt_id"]
                or operation.get("registry") != request["registry"]
                or operation.get("unit") != request["unit"]
                or operation.get("cgroup") != request["cgroup"]):
            raise ValueError("operation inspection identity is not registered")
        if operation["status"] == "TERMINAL":
            return self._inspect_result(record, operation)
        if operation["status"] != "RUNNING":
            raise ValueError("extinct operation cannot be inspected as completion")
        shown = self._command([
            self._SYSTEMCTL, "show", str(request["unit"]), "--property=MainPID",
            "--property=ActiveState", "--property=SubState", "--property=ControlGroup",
            "--property=ExecMainCode", "--property=ExecMainStatus", "--property=Result",
        ])
        if shown.returncode != 0:
            raise RuntimeError("operation inspection failed")
        values: dict[str, str] = {}
        expected = {"MainPID", "ActiveState", "SubState", "ControlGroup", "ExecMainCode",
                    "ExecMainStatus", "Result"}
        for line in shown.stdout.splitlines():
            if "=" not in line:
                raise ValueError("operation inspection output is malformed")
            key, value = line.split("=", 1)
            if key not in expected or key in values:
                raise ValueError("operation inspection output is not exact")
            values[key] = value
        if set(values) != expected or values["ControlGroup"] != request["cgroup"]:
            raise ValueError("operation inspection identity changed")
        unit_state = (values["ActiveState"], values["SubState"])
        if unit_state not in self._TERMINAL_UNIT_STATES:
            if (values["ActiveState"] not in {"active", "activating"}
                    or values["SubState"] in self._TERMINAL_STATES
                    or not values["MainPID"].isdigit()):
                raise ValueError("running operation state is invalid")
            return self._inspect_result(record, operation)
        if (values["MainPID"] != "0" or values["ExecMainCode"] != "exited"
                or not values["ExecMainStatus"].isdigit()):
            raise ValueError("terminal operation state is invalid")
        exit_status = int(values["ExecMainStatus"])
        if (not 0 <= exit_status <= 255
                or (exit_status == 0 and values["Result"] != "success")
                or (exit_status != 0 and values["Result"] != "exit-code")):
            raise ValueError("terminal operation state has invalid exit result")
        stdout = self._read_output_file(str(operation["stdout_path"]))
        stderr = self._read_output_file(str(operation["stderr_path"]))
        output_digest = _operation_output_digest(
            str(request["attempt_id"]), str(request["operation_id"]), str(request["registry"]),
            str(request["unit"]), str(request["cgroup"]), exit_status, stdout, stderr,
        )
        terminal = {"exit_status": exit_status, "stdout": stdout, "stderr": stderr,
                    "output_digest": output_digest}
        def complete(state: dict[str, object]) -> None:
            current = state["operations"].get(request["operation_id"])
            if (type(current) is not dict or current.get("status") != "RUNNING"
                    or current.get("unit") != request["unit"] or current.get("cgroup") != request["cgroup"]):
                raise ValueError("operation identity changed before terminal write")
            current["status"] = "TERMINAL"
            current["terminal"] = terminal
        registry.mutate(complete)
        self.checkpoint("after-terminal-publication")
        completed = registry.snapshot()
        return self._inspect_result(completed, completed["operations"][request["operation_id"]])

    def _unit_and_cgroup_extinct(self, unit: str, cgroup: str) -> bool:
        shown = self._command([
            self._SYSTEMCTL, "show", unit, "--property=MainPID", "--property=ActiveState",
            "--property=SubState", "--property=ControlGroup",
        ])
        if shown.returncode != 0:
            return False
        values: dict[str, str] = {}
        for line in shown.stdout.splitlines():
            if "=" not in line:
                return False
            key, value = line.split("=", 1)
            if key in values:
                return False
            values[key] = value
        if (values.get("MainPID") != "0" or values.get("ActiveState") not in self._TERMINAL_STATES
                or values.get("SubState") not in self._TERMINAL_STATES):
            return False
        path = self.cgroup_root / cgroup.removeprefix("/")
        if not path.exists():
            return True
        try:
            events = (path / "cgroup.events").read_text(encoding="ascii").splitlines()
            procs = (path / "cgroup.procs").read_text(encoding="ascii")
        except OSError:
            return False
        populated = [line.split() for line in events if line.startswith("populated ")]
        return populated == [["populated", "0"]] and not procs.strip()

    def _wait_extinct(self, unit: str, cgroup: str, seconds: float) -> bool:
        deadline = self.clock() + seconds
        while True:
            if self._unit_and_cgroup_extinct(unit, cgroup):
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(min(0.1, max(0.0, deadline - self.clock())))
            # Deterministic runners with non-advancing clocks get one observation per phase.
            if self.clock() + 1e-9 < deadline and self.sleep is not time.sleep:
                return False

    def quiesce(self, unit: str, cgroup: str) -> bool:
        # A RemainAfterExit unit has no process for systemctl kill, but must still be
        # stopped so its active/exited metadata can be released after inspection.
        self._command([self._SYSTEMCTL, "kill", "--signal=TERM", "--kill-whom=all", unit])
        stopped = self._command([self._SYSTEMCTL, "stop", unit])
        if stopped.returncode != 0:
            return False
        extinct = self._wait_extinct(unit, cgroup, 5.0)
        if not extinct:
            killed = self._command([self._SYSTEMCTL, "kill", "--signal=KILL", "--kill-whom=all", unit])
            if killed.returncode != 0 or not self._wait_extinct(unit, cgroup, 5.0):
                return False
        reset = self._command([self._SYSTEMCTL, "reset-failed", unit])
        return reset.returncode == 0

    def term(self, unit: str) -> None:
        result = self._command([self._SYSTEMCTL, "kill", "--signal=TERM", "--kill-whom=all", unit])
        if result.returncode != 0:
            raise RuntimeError("TERM failed")

    def kill(self, unit: str) -> None:
        result = self._command([self._SYSTEMCTL, "kill", "--signal=KILL", "--kill-whom=all", unit])
        if result.returncode != 0:
            raise RuntimeError("KILL failed")

    def is_extinct(self, cgroup: str) -> bool:
        unit = cgroup.rsplit("/", 1)[-1]
        return self._unit_and_cgroup_extinct(unit, cgroup)


class AttemptRegistry:
    """In-memory model of the root-owned, lock-serialized attempt registry."""

    def __init__(self, attempt_id: str):
        self.attempt_id = attempt_id
        self.state = "OPEN"
        self.recovery_digest: str | None = None
        self.recovery_result_digest: str | None = None
        self.operations: dict[str, tuple[str, str]] = {}
        self.commit_cgroup: tuple[str, str] | None = None
        self.commit_request_digest: str | None = None
        self.commit_result_digest: str | None = None
        self._lock = threading.RLock()

    def register_operation(self, operation_id: str, unit: str, cgroup: str) -> bool:
        with self._lock:
            if self.state != "OPEN" or operation_id in self.operations:
                return False
            self.operations[operation_id] = (unit, cgroup)
            return True

    def seal_recovering(self, request_digest: str) -> bool:
        with self._lock:
            if self.state == "OPEN" or self.state == "SEALED_COMMITTING":
                self.state = "SEALED_RECOVERING"
                self.recovery_digest = request_digest
                return True
            return self.state == "SEALED_RECOVERING" and self.recovery_digest == request_digest


_START_FIELDS = frozenset(("schema", "attempt_id", "operation_id", "registry", "run_as", "argv"))
_INSPECT_FIELDS = frozenset(("schema", "attempt_id", "operation_id", "registry", "unit", "cgroup"))
_STOP_FIELDS = frozenset(("schema", "attempt_id", "operation_id", "registry", "unit", "cgroup"))
_RECOVERY_FIELDS = frozenset((
    "schema", "boundary", "phase", "attempt_id", "operation_id", "registry", "stage",
    "release_root", "destination_root", "bundle", "release_manifest", "backup_manifest",
    "release_spec", "provenance_digest", "evidence_digest", "pending_note_id", "service",
    "initial_service_state", "health_url", "health_status", "health_timeout_seconds", "guard_unit",
))
_RECOVERY_QUERY_FIELDS = _RECOVERY_FIELDS | frozenset(("query",))
_COMMIT_FIELDS = frozenset((
    "schema", "boundary", "phase", "attempt_id", "operation_id", "registry",
    "evidence_digest", "pending_note_id", "guard_unit", "success_record",
    "control_unit", "control_cgroup",
))
_COMMIT_QUERY_FIELDS = frozenset((
    "schema", "query", "boundary", "phase", "attempt_id", "operation_id", "registry",
    "evidence_digest", "success_record",
))
_TRANSACTION_REQUEST_FIELDS = {
    "remote-transaction-create-or-query": frozenset((
        "schema", "registry_root", "release_key", "release_document", "execution_contract",
        "created_epoch", "original_deadline_epoch",
    )),
    "remote-transaction-claim": frozenset((
        "schema", "registry_root", "release_key", "transaction_id", "claimant_id",
        "request_digest", "claimed_epoch",
    )),
    "remote-transaction-inspect": frozenset((
        "schema", "registry_root", "release_key", "transaction_id",
    )),
    "remote-transaction-transition": frozenset((
        "schema", "registry_root", "release_key", "transaction_id", "claimant_id", "phase",
        "from_state", "to_state", "request_digest", "transition_epoch",
    )),
    "remote-transaction-advance": frozenset((
        "schema", "registry_root", "release_key", "transaction_id", "claimant_id", "phase",
        "request_digest", "transition_epoch",
    )),
    "remote-transaction-terminal": frozenset((
        "schema", "registry_root", "release_key", "transaction_id", "claimant_id", "phase", "kind",
        "from_state", "to_state", "request_digest", "result_digest", "terminal_epoch",
    )),
}
_REQUEST_FIELDS = {
    "remote-start": _START_FIELDS, "remote-operation-inspect": _INSPECT_FIELDS,
    "remote-stop": _STOP_FIELDS,
    "remote-recover": _RECOVERY_FIELDS, "remote-commit": _COMMIT_FIELDS,
    **_TRANSACTION_REQUEST_FIELDS,
}
_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_UNIT_RE = re.compile(r"matrix-p13-(?:op|commit|recover)-[A-Za-z0-9_.@:-]+\.service\Z")
_SERVICE_RE = re.compile(r"[A-Za-z0-9_.@:-]+\.service\Z")


def _absolute(value: object, label: str) -> str:
    if type(value) is not str or not value.startswith("/") or ".." in value.split("/") or "\x00" in value:
        raise ValueError(f"{label} must be a normalized absolute path")
    if os.path.normpath(value) != value:
        raise ValueError(f"{label} must be normalized")
    return value


def validate_privileged_request(helper: str, request: object) -> dict[str, object]:
    fields = ((_COMMIT_QUERY_FIELDS if helper == "remote-commit" else _RECOVERY_QUERY_FIELDS)
              if helper in {"remote-commit", "remote-recover"} and type(request) is dict
              and request.get("query") is True else _REQUEST_FIELDS.get(helper))
    if fields is None or type(request) is not dict or frozenset(request) != fields or not _is_schema_v1(request.get("schema")):
        raise ValueError(f"{helper} request has an invalid schema")
    if helper in _TRANSACTION_HELPERS:
        root = _absolute(request.get("registry_root"), "registry_root")
        if root == "/" or len(root.encode("utf-8")) > 4096:
            raise ValueError("transaction registry root is invalid")
        release_key = request.get("release_key")
        if type(release_key) is not str or _DIGEST_RE.fullmatch(release_key) is None:
            raise ValueError("release key is invalid")
        if helper == "remote-transaction-create-or-query":
            identity = canonical_release_identity(request.get("release_document"))
            if identity.release_key != release_key:
                raise ValueError("release key does not match canonical release document")
            contract = request.get("execution_contract")
            if (type(contract) is not dict or frozenset(contract) != {
                    "schema", "stage", "bundle", "destination_root", "service"
                } or not _is_schema_v1(contract.get("schema"))):
                raise ValueError("execution contract is invalid")
            for field in ("stage", "bundle", "destination_root"):
                _absolute(contract.get(field), field)
            if type(contract.get("service")) is not str or _SERVICE_RE.fullmatch(contract["service"]) is None:
                raise ValueError("execution service is invalid")
            _validate_execution_service_coherence(identity.document, contract)
            created, deadline = request.get("created_epoch"), request.get("original_deadline_epoch")
            if (type(created) is not int or type(deadline) is not int or created < 0
                    or not 60 <= deadline - created <= 86400):
                raise ValueError("transaction lifecycle is invalid")
            return request
        transaction = request.get("transaction_id")
        if type(transaction) is not str or _ID_RE.fullmatch(transaction) is None:
            raise ValueError("transaction ID is invalid")
        if helper == "remote-transaction-inspect":
            return request
        claimant = request.get("claimant_id")
        digest = request.get("request_digest")
        if type(claimant) is not str or _ID_RE.fullmatch(claimant) is None:
            raise ValueError("claimant ID is invalid")
        if type(digest) is not str or _DIGEST_RE.fullmatch(digest) is None:
            raise ValueError("transaction request digest is invalid")
        epoch_field = "claimed_epoch" if helper == "remote-transaction-claim" else (
            "terminal_epoch" if helper == "remote-transaction-terminal" else "transition_epoch")
        if type(request.get(epoch_field)) is not int or request[epoch_field] < 0:
            raise ValueError("transaction epoch is invalid")
        if helper == "remote-transaction-claim":
            return request
        if request.get("phase") not in {"initial-deploy", "rollback-proof", "final-deploy"}:
            raise ValueError("transaction phase is invalid")
        if helper == "remote-transaction-advance":
            return request
        if type(request.get("from_state")) is not str or type(request.get("to_state")) is not str:
            raise ValueError("transaction state is invalid")
        if helper == "remote-transaction-terminal":
            if request.get("kind") not in {"commit", "recovery"}:
                raise ValueError("transaction terminal kind is invalid")
            result = request.get("result_digest")
            if type(result) is not str or _DIGEST_RE.fullmatch(result) is None:
                raise ValueError("transaction result digest is invalid")
        return request
    attempt = request.get("attempt_id")
    operation = request.get("operation_id")
    if type(attempt) is not str or _ID_RE.fullmatch(attempt) is None:
        raise ValueError("invalid attempt ID")
    if type(operation) is not str or _ID_RE.fullmatch(operation) is None:
        raise ValueError("invalid operation ID")
    _absolute(request.get("registry"), "registry")
    if helper == "remote-start":
        if request.get("run_as") not in {"root", "admin"}:
            raise ValueError("invalid run identity")
        _canonical_argv_bytes(request.get("argv"))
    elif helper in {"remote-operation-inspect", "remote-stop"}:
        if type(request.get("unit")) is not str or _UNIT_RE.fullmatch(request["unit"]) is None:
            raise ValueError("invalid unit")
        cgroup = _absolute(request.get("cgroup"), "cgroup")
        if not cgroup.startswith("/system.slice/"):
            raise ValueError("invalid cgroup")
    elif helper == "remote-recover":
        if request.get("boundary") not in {"b1", "b2"}:
            raise ValueError("invalid recovery boundary")
        if request.get("phase") not in {"initial-deploy", "rollback-proof", "final-deploy"}:
            raise ValueError("invalid recovery phase")
        for field in ("stage", "release_root", "destination_root", "bundle", "release_manifest",
                      "backup_manifest", "release_spec"):
            _absolute(request.get(field), field)
        for field in ("provenance_digest", "evidence_digest"):
            value = request.get(field)
            if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
                raise ValueError(f"invalid {field}")
        if type(request.get("pending_note_id")) is not str or not request["pending_note_id"].isdigit():
            raise ValueError("invalid pending note ID")
        if type(request.get("service")) is not str or _SERVICE_RE.fullmatch(request["service"]) is None:
            raise ValueError("invalid service")
        if type(request.get("guard_unit")) is not str or _UNIT_RE.fullmatch(request["guard_unit"]) is None:
            raise ValueError("invalid guard unit")
        state = request.get("initial_service_state")
        readiness = (request.get("health_url"), request.get("health_status"), request.get("health_timeout_seconds"))
        if state == "active":
            if readiness != ("http://127.0.0.1:5000/", 302, 30):
                raise ValueError("active recovery requires exact readiness")
        elif state == "inactive":
            if readiness != (None, None, None):
                raise ValueError("inactive recovery forbids readiness")
        else:
            raise ValueError("invalid initial service state")
    else:
        if request.get("boundary") not in {"b1", "b2"} or request.get("phase") not in {
            "initial-deploy", "rollback-proof", "final-deploy"
        }:
            raise ValueError("invalid commit identity")
        value = request.get("evidence_digest")
        if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
            raise ValueError("invalid commit digest")
        for field in ("registry", "success_record"):
            _absolute(request.get(field), field)
        if request.get("query") is not True:
            if type(request.get("pending_note_id")) is not str or not request["pending_note_id"].isdigit():
                raise ValueError("invalid pending note ID")
            _absolute(request.get("control_cgroup"), "control_cgroup")
            for field in ("guard_unit", "control_unit"):
                if type(request.get(field)) is not str or _UNIT_RE.fullmatch(request[field]) is None:
                    raise ValueError(f"invalid {field}")
    return request


def encode_privileged_request(helper: str, request: dict[str, object]) -> str:
    validated = validate_privileged_request(helper, request)
    canonical = json.dumps(validated, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return encode_privileged_argv([canonical])


def decode_privileged_request(helper: str, request_b64: str) -> dict[str, object]:
    argv = decode_privileged_argv(helper, request_b64)
    if len(argv) != 1:
        raise ValueError("privileged helper requires one canonical request")
    try:
        request = json.loads(argv[0])
    except json.JSONDecodeError as exc:
        raise ValueError("privileged request is malformed") from exc
    validated = validate_privileged_request(helper, request)
    canonical = json.dumps(validated, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if argv[0] != canonical:
        raise ValueError("privileged request object is noncanonical")
    return validated


def canonical_recovery_request_digest(request: dict[str, object]) -> str:
    validated = validate_privileged_request("remote-recover", request)
    canonical = json.dumps(validated, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def remote_recover_gateway(
    registry: AttemptRegistry,
    *,
    request: dict[str, object],
    controller: object,
    acquire_release_lock: Callable[[], int],
    inner_recover: Callable[[], ControlResult],
    checkpoint: Callable[[str], None] | None = None,
) -> ControlResult:
    """Replayable seal, extinction, lock, and inner-recovery gateway."""
    request_digest = canonical_recovery_request_digest(request)
    if request["attempt_id"] != registry.attempt_id:
        raise ValueError("recovery attempt identity mismatch")
    checkpoint = checkpoint or (lambda _name: None)
    with registry._lock:
        if registry.state == "SEALED_RECOVERED":
            if registry.recovery_digest != request_digest or registry.recovery_result_digest is None:
                raise ValueError("recovery request does not match terminal record")
            return ControlResult(0, registry.recovery_result_digest)
        if not registry.seal_recovering(request_digest):
            raise ValueError("recovery request does not match sealed attempt")
        targets = ([] if registry.commit_cgroup is None else [registry.commit_cgroup])
        targets.extend(registry.operations.values())
    checkpoint("after-seal")
    for unit, cgroup in targets:
        controller.term(unit)
        if not controller.is_extinct(cgroup):
            controller.kill(unit)
            if not controller.is_extinct(cgroup):
                return ControlResult(2, "")
        checkpoint(f"after-extinction:{unit}")
    checkpoint("before-lock")
    lock_result = acquire_release_lock()
    if type(lock_result) is not int or lock_result != 0:
        return ControlResult(2, "")
    checkpoint("after-lock")
    checkpoint("before-inner-recovery")
    inner_result = inner_recover()
    if type(inner_result) is not ControlResult or inner_result.returncode != 0 or not inner_result.digest:
        return ControlResult(2, "")
    checkpoint("after-inner-recovery")
    result_payload = {
        "request_digest": request_digest,
        "inner_result_digest": inner_result.digest,
        "returncode": 0,
    }
    result_digest = hashlib.sha256(
        json.dumps(result_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    checkpoint("before-terminal-record")
    with registry._lock:
        if registry.state != "SEALED_RECOVERING" or registry.recovery_digest != request_digest:
            return ControlResult(2, "")
        registry.recovery_result_digest = result_digest
        registry.state = "SEALED_RECOVERED"
    checkpoint("after-terminal-record")
    return ControlResult(0, result_digest)


def remote_commit(
    registry: AttemptRegistry,
    *,
    request_digest: str,
    control_unit: str,
    control_cgroup: str,
    guard_unit: str,
    quiesce_operations: Callable[[list[tuple[str, str]]], int],
    write_success: Callable[[], str],
    disarm_guard: Callable[[str], int],
) -> ControlResult:
    """Seal and commit without registering or quiescing the commit control unit."""
    with registry._lock:
        if registry.state == "SEALED_COMMITTED":
            if registry.commit_request_digest != request_digest or registry.commit_result_digest is None:
                raise ValueError("commit request does not match terminal record")
            return ControlResult(0, registry.commit_result_digest)
        if registry.state == "OPEN":
            registry.state = "SEALED_COMMITTING"
            registry.commit_request_digest = request_digest
            registry.commit_cgroup = (control_unit, control_cgroup)
        elif registry.state != "SEALED_COMMITTING" or registry.commit_request_digest != request_digest:
            raise ValueError("commit request does not match sealed attempt")
        elif registry.commit_cgroup != (control_unit, control_cgroup):
            raise ValueError("commit control identity changed")
        targets = list(registry.operations.values())
    if quiesce_operations(targets) != 0:
        return ControlResult(2, "")
    result_digest = write_success()
    if type(result_digest) is not str or not result_digest:
        return ControlResult(2, "")
    if disarm_guard(guard_unit) != 0:
        return ControlResult(2, "")
    with registry._lock:
        if registry.state != "SEALED_COMMITTING" or registry.commit_request_digest != request_digest:
            return ControlResult(2, "")
        registry.commit_result_digest = result_digest
        registry.state = "SEALED_COMMITTED"
    return ControlResult(0, result_digest)


def quiesce_then_recover(
    registry: AttemptRegistry,
    *,
    request_digest: str,
    controller: object,
    recover: Callable[[], int],
) -> int:
    """Seal admission and prove every recorded cgroup extinct before recovery."""
    if not registry.seal_recovering(request_digest):
        return 2
    targets = list(registry.operations.values())
    if registry.commit_cgroup is not None:
        targets.insert(0, registry.commit_cgroup)
    for unit, cgroup in targets:
        controller.term(unit)
        if not controller.is_extinct(cgroup):
            controller.kill(unit)
            if not controller.is_extinct(cgroup):
                return 2
    return recover()


@dataclass(frozen=True)
class AttemptOutcome:
    state: str
    reason: str


def run_guarded_attempt(
    *,
    clock: Callable[[], float],
    attempt_budget_seconds: int,
    commit_reserve_seconds: int,
    watchdog_seconds: int,
    arm_watchdog: Callable[[float], object],
    work: Callable[[float], int],
    recover: Callable[[str], int],
    commit: Callable[[], int],
) -> AttemptOutcome:
    """Run one attempt against deadlines fixed before any mutating work."""
    if (attempt_budget_seconds, commit_reserve_seconds, watchdog_seconds) != (6600, 60, 7200):
        raise ValueError("exact guarded deadlines are required")
    started = clock()
    attempt_deadline = started + attempt_budget_seconds - commit_reserve_seconds
    watchdog_deadline = started + watchdog_seconds
    if attempt_deadline >= watchdog_deadline:
        raise ValueError("attempt deadline must precede watchdog")
    arm_watchdog(watchdog_deadline)
    work_result = work(attempt_deadline)
    if clock() >= attempt_deadline:
        recover("attempt-deadline")
        return AttemptOutcome("PENDING", "attempt-deadline")
    if type(work_result) is not int or work_result != 0:
        recover("work-failed")
        return AttemptOutcome("PENDING", "work-failed")
    commit_result = commit()
    if type(commit_result) is not int or commit_result != 0:
        recover("commit-failed")
        return AttemptOutcome("PENDING", "commit-failed")
    return AttemptOutcome("COMMITTED", "success")


def _recover_request(boundary: str, phase: str, attempt_id: str, stage: str, bundle: str, action: str) -> dict[str, object]:
    return {
        "schema": 1,
        "action": action,
        "boundary": boundary,
        "phase": phase,
        "attempt_id": attempt_id,
        "stage": stage,
        "bundle": bundle,
        "namespace": f"/run/matrix-priority-1-3/{attempt_id}",
        "guard_unit": f"matrix-p13-recover-{attempt_id}.service",
    }


def _request_digest(helper: str, request: dict[str, object]) -> str:
    validate_privileged_request(helper, request)
    return hashlib.sha256(json.dumps(request, ensure_ascii=True, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _rename_noreplace(parent_fd: int, source: str, target: str) -> None:
    """Atomically publish one same-directory name without replacement."""
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as error:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable") from error
    renameat2.argtypes = (
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(parent_fd, os.fsencode(source), parent_fd, os.fsencode(target), 1) != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise FileExistsError(error_number, os.strerror(error_number), target)
        raise OSError(error_number, os.strerror(error_number), target)


def _read_existing_success_record(parent_fd: int, name: str, expected_size: int) -> bytes:
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent_fd,
        )
    except OSError as error:
        raise PermissionError("existing success record is unsafe") from error
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0
                or stat.S_IMODE(before.st_mode) != 0o400 or before.st_nlink != 1
                or before.st_size != expected_size):
            raise PermissionError("existing success record is unsafe")
        encoded = b""
        while len(encoded) <= expected_size:
            chunk = os.read(descriptor, expected_size + 1 - len(encoded))
            if not chunk:
                break
            encoded += chunk
        after = os.fstat(descriptor)
        path_metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        identity = lambda metadata: (
            metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_uid,
            metadata.st_gid, metadata.st_nlink, metadata.st_size,
        )
        if (len(encoded) != expected_size or identity(before) != identity(after)
                or identity(after) != identity(path_metadata)):
            raise PermissionError("existing success record changed during validation")
        return encoded
    finally:
        os.close(descriptor)


def _publish_success_record(path_value: object, request: dict[str, object]) -> str:
    path = Path(_absolute(path_value, "success_record"))
    parent = path.parent
    parent_metadata = parent.stat(follow_symlinks=False)
    if (not stat.S_ISDIR(parent_metadata.st_mode) or parent_metadata.st_uid != 0
            or parent_metadata.st_mode & 0o022):
        raise PermissionError("success parent is unsafe")
    payload = {
        "schema": 1, "boundary": request["boundary"], "phase": request["phase"],
        "attempt_id": request["attempt_id"], "operation_id": request["operation_id"],
        "evidence_digest": request["evidence_digest"], "pending_note_id": request["pending_note_id"],
    }
    encoded = (json.dumps(payload, ensure_ascii=True, sort_keys=True,
                          separators=(",", ":")) + "\n").encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    opened_parent = os.fstat(parent_fd)
    if ((opened_parent.st_dev, opened_parent.st_ino) !=
            (parent_metadata.st_dev, parent_metadata.st_ino)
            or not stat.S_ISDIR(opened_parent.st_mode) or opened_parent.st_uid != 0
            or opened_parent.st_mode & 0o022):
        os.close(parent_fd)
        raise PermissionError("success parent changed during validation")
    temporary = f".{path.name}.{secrets.token_hex(16)}"
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             os.O_CLOEXEC | os.O_NOFOLLOW, 0o400, dir_fd=parent_fd)
        os.fchmod(descriptor, 0o400)
        os.fchown(descriptor, 0, 0)
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short success-record write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            _rename_noreplace(parent_fd, temporary, path.name)
        except FileExistsError:
            existing = _read_existing_success_record(parent_fd, path.name, len(encoded))
            if existing != encoded:
                raise FileExistsError("success record differs")
            os.unlink(temporary, dir_fd=parent_fd)
        else:
            if _read_existing_success_record(parent_fd, path.name, len(encoded)) != encoded:
                raise RuntimeError("published success record changed")
        os.fsync(parent_fd)
        return digest
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(parent_fd)


def _release_recover_argv(request: dict[str, object]) -> list[str]:
    argv = [
        f"{request['stage']}/venv/bin/python", "-I", "-B",
        f"{request['stage']}/deploy/release_bundle.py", "recover",
        "--release-root", str(request["release_root"]),
        "--destination-root", str(request["destination_root"]),
        "--bundle", str(request["bundle"]),
        "--release-manifest", str(request["release_manifest"]),
        "--backup-manifest", str(request["backup_manifest"]),
        "--service", str(request["service"]),
        "--initial-service-state", str(request["initial_service_state"]),
    ]
    if request["initial_service_state"] == "active":
        argv.extend(["--health-url", str(request["health_url"]),
                     "--health-status", str(request["health_status"]),
                     "--health-timeout-seconds", str(request["health_timeout_seconds"])])
    return argv


def _default_recovery_runner(argv: list[str]) -> ControlResult:
    completed = subprocess.run(argv, check=False, text=True, capture_output=True,
                               timeout=300, shell=False)
    if completed.returncode != 0 or completed.stderr:
        return ControlResult(completed.returncode or 2, "")
    digest = hashlib.sha256(completed.stdout.encode("utf-8")).hexdigest()
    return ControlResult(0, digest)


def _read_success_record_digest(request: dict[str, object]) -> str:
    path = Path(_absolute(request.get("success_record"), "success_record"))
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
                or stat.S_IMODE(metadata.st_mode) != 0o400 or metadata.st_nlink != 1
                or metadata.st_size > 65536):
            raise ValueError("success record metadata is invalid")
        raw = os.read(descriptor, 65537)
        if len(raw) > 65536 or os.read(descriptor, 1):
            raise ValueError("success record is oversized")
    finally:
        os.close(descriptor)
    expected = {
        "schema": 1, "boundary": request["boundary"], "phase": request["phase"],
        "attempt_id": request["attempt_id"], "operation_id": request["operation_id"],
        "evidence_digest": request["evidence_digest"], "pending_note_id": request["pending_note_id"],
    }
    encoded = (json.dumps(expected, ensure_ascii=True, sort_keys=True,
                          separators=(",", ":")) + "\n").encode("utf-8")
    if raw != encoded:
        raise ValueError("success record does not match terminal request")
    return hashlib.sha256(raw).hexdigest()


def _validated_commit_terminal(
    terminal: object, query: dict[str, object] | None = None,
) -> dict[str, object]:
    if type(terminal) is not dict or frozenset(terminal) != {
        "schema", "request", "request_digest", "result_digest", "committed_epoch"
    } or not _is_schema_v1(terminal.get("schema")):
        raise ValueError("remote commit terminal schema is invalid")
    request = terminal.get("request")
    validate_privileged_request("remote-commit", request)
    if request.get("query") is True:
        raise ValueError("remote commit terminal cannot contain a query")
    request_digest = _request_digest("remote-commit", request)
    if terminal.get("request_digest") != request_digest:
        raise ValueError("remote commit terminal request digest is invalid")
    result_digest = terminal.get("result_digest")
    if type(result_digest) is not str or _DIGEST_RE.fullmatch(result_digest) is None:
        raise ValueError("remote commit terminal result digest is invalid")
    committed_epoch = terminal.get("committed_epoch")
    if (type(committed_epoch) not in {int, float}
            or not math.isfinite(committed_epoch) or committed_epoch <= 0):
        raise ValueError("remote commit terminal time is invalid")
    if query is not None:
        validate_privileged_request("remote-commit", query)
        if query.get("query") is not True:
            raise ValueError("remote commit discovery requires a query")
        for field in _COMMIT_QUERY_FIELDS - {"query"}:
            if query.get(field) != request.get(field):
                raise ValueError("remote commit terminal does not match query")
    return terminal


def dispatch_privileged(
    helper: str, request: dict[str, object], *,
    controller: object | None = None,
    recovery_runner: Callable[[list[str]], ControlResult] | None = None,
    release_lock: Callable[[], int] | None = None,
) -> ControlResult | OperationResult:
    validate_privileged_request(helper, request)
    if helper in _TRANSACTION_HELPERS:
        registry = DurableTransactionRegistry(str(request["registry_root"]), str(request["release_key"]))
        try:
            if helper == "remote-transaction-create-or-query":
                record = registry.create_or_query(request)
            elif helper == "remote-transaction-inspect":
                record = registry.inspect(str(request["transaction_id"]))
            elif helper == "remote-transaction-claim":
                record = registry.claim(request)
            elif helper == "remote-transaction-transition":
                record = registry.transition(request)
            elif helper == "remote-transaction-advance":
                record = registry.advance(request)
            else:
                record = registry.terminal(request)
            return ControlResult(0, str(record["record_digest"]), record)
        finally:
            registry.close()
    if helper == "remote-commit" and request.get("query") is True:
        if not Path(str(request["registry"])).is_file():
            return ControlResult(3, "")
        registry = DurableAttemptRegistry(str(request["registry"]), str(request["attempt_id"]))
        record = registry.snapshot()
        commit = record.get("commit")
        if record.get("state") != "SEALED_COMMITTED" or type(commit) is not dict:
            return ControlResult(3, "")
        terminal = _validated_commit_terminal({
            "schema": 1, "request": commit.get("request"),
            "request_digest": commit.get("request_digest"),
            "result_digest": commit.get("result_digest"),
            "committed_epoch": commit.get("committed_epoch"),
        }, request)
        if _read_success_record_digest(terminal["request"]) != terminal["result_digest"]:
            raise ValueError("remote commit terminal result does not match success record")
        return ControlResult(0, str(terminal["result_digest"]), terminal)
    if helper == "remote-recover" and request.get("query") is True:
        if not Path(str(request["registry"])).is_file():
            return ControlResult(3, "")
        registry = DurableAttemptRegistry(str(request["registry"]), str(request["attempt_id"]))
        record = registry.snapshot()
        recovery = record.get("recovery")
        if record.get("state") != "SEALED_RECOVERED" or type(recovery) is not dict:
            return ControlResult(3, "")
        expected_request = dict(request); expected_request.pop("query")
        terminal = _validated_recovery_terminal({
            "schema": 1, "request": recovery.get("request"),
            "request_digest": recovery.get("request_digest"),
            "result_digest": recovery.get("result_digest"),
            "recovered_epoch": recovery.get("recovered_epoch"),
        }, expected_request)
        return ControlResult(0, str(terminal["result_digest"]), terminal)
    registry = DurableAttemptRegistry(str(request["registry"]), str(request["attempt_id"]),
                                      create=helper == "remote-start")
    systemd = controller or SystemdController()
    if helper == "remote-start":
        return systemd.start(request, registry)
    if helper == "remote-operation-inspect":
        return systemd.inspect(request, registry)
    if helper == "remote-stop":
        record = registry.snapshot()
        operations = record.get("operations")
        operation = operations.get(request["operation_id"]) if type(operations) is dict else None
        if (type(operation) is not dict or operation.get("unit") != request["unit"]
                or operation.get("cgroup") != request["cgroup"]):
            return ControlResult(2, "")
        if not systemd.quiesce(str(request["unit"]), str(request["cgroup"])):
            return ControlResult(2, "")
        def stopped(state: dict[str, object]) -> None:
            current = state["operations"][request["operation_id"]]
            if current["unit"] != request["unit"] or current["cgroup"] != request["cgroup"]:
                raise ValueError("operation identity changed")
            current["status"] = "EXTINCT"
        registry.mutate(stopped)
        return ControlResult(0, _request_digest(helper, request))
    if helper == "remote-commit":
        digest = _request_digest(helper, request)
        def seal_commit(state: dict[str, object]) -> object:
            commit = state.get("commit")
            if state.get("state") == "SEALED_COMMITTED":
                if type(commit) is not dict or commit.get("request_digest") != digest:
                    raise ValueError("commit terminal mismatch")
                terminal = _validated_commit_terminal({
                    "schema": 1, "request": commit.get("request"),
                    "request_digest": commit.get("request_digest"),
                    "result_digest": commit.get("result_digest"),
                    "committed_epoch": commit.get("committed_epoch"),
                })
                return ("terminal", terminal)
            if state.get("state") == "OPEN":
                state["state"] = "SEALED_COMMITTING"
                state["commit"] = {"request_digest": digest,
                                   "request": json.loads(json.dumps(request, sort_keys=True)),
                                   "unit": request["control_unit"],
                                   "cgroup": request["control_cgroup"], "result_digest": None,
                                   "committed_epoch": None}
            elif (state.get("state") != "SEALED_COMMITTING" or type(commit) is not dict
                  or commit.get("request_digest") != digest
                  or commit.get("unit") != request["control_unit"]
                  or commit.get("cgroup") != request["control_cgroup"]):
                raise ValueError("commit seal mismatch")
            return ("continue", None)
        status, terminal = registry.mutate(seal_commit)
        if status == "terminal":
            return ControlResult(0, str(terminal["result_digest"]), terminal)
        record = registry.snapshot()
        operations = record.get("operations")
        for operation in (operations.values() if type(operations) is dict else ()):
            if (type(operation) is not dict or not systemd.quiesce(
                    str(operation["unit"]), str(operation["cgroup"]))):
                return ControlResult(2, "")
        result_digest = _publish_success_record(request["success_record"], request)
        guard_cgroup = "/system.slice/" + str(request["guard_unit"])
        if not systemd.quiesce(str(request["guard_unit"]), guard_cgroup):
            return ControlResult(2, "")
        def finish_commit(state: dict[str, object]) -> None:
            commit = state.get("commit")
            if state.get("state") != "SEALED_COMMITTING" or type(commit) is not dict or commit.get("request_digest") != digest:
                raise ValueError("commit takeover detected")
            commit["result_digest"] = result_digest
            commit["committed_epoch"] = time.time()
            state["state"] = "SEALED_COMMITTED"
        registry.mutate(finish_commit)
        terminal = _validated_commit_terminal({
            "schema": 1, "request": request, "request_digest": digest,
            "result_digest": result_digest, "committed_epoch": registry.snapshot()["commit"]["committed_epoch"],
        })
        return ControlResult(0, result_digest, terminal)

    request_digest = canonical_recovery_request_digest(request)
    def seal_recovery(state: dict[str, object]) -> object:
        recovery = state.get("recovery")
        if state.get("state") == "SEALED_RECOVERED":
            if type(recovery) is not dict or recovery.get("request_digest") != request_digest:
                raise ValueError("recovery terminal mismatch")
            return ("terminal", recovery.get("result_digest"))
        if state.get("state") in {"OPEN", "SEALED_COMMITTING"}:
            state["state"] = "SEALED_RECOVERING"
            state["recovery"] = {"request_digest": request_digest,
                                 "request": json.loads(json.dumps(request, sort_keys=True)),
                                 "result_digest": None, "recovered_epoch": None}
        elif (state.get("state") != "SEALED_RECOVERING" or type(recovery) is not dict
              or recovery.get("request_digest") != request_digest):
            raise ValueError("recovery seal mismatch")
        return ("continue", None)
    status, terminal_digest = registry.mutate(seal_recovery)
    if status == "terminal":
        return ControlResult(0, str(terminal_digest))
    record = registry.snapshot()
    targets = []
    commit = record.get("commit")
    if type(commit) is dict:
        targets.append((commit.get("unit"), commit.get("cgroup")))
    operations = record.get("operations")
    if type(operations) is dict:
        targets.extend((item.get("unit"), item.get("cgroup")) for item in operations.values()
                       if type(item) is dict)
    for unit, cgroup in targets:
        if type(unit) is not str or type(cgroup) is not str or not systemd.quiesce(unit, cgroup):
            return ControlResult(2, "")
    held_lock = None
    try:
        if release_lock is not None:
            if release_lock() != 0:
                return ControlResult(2, "")
        else:
            lock_path = Path(str(request["registry"])).with_name("release-transaction.lock")
            held_lock = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
            os.fchmod(held_lock, 0o600)
            fcntl.flock(held_lock, fcntl.LOCK_EX)
        inner = (recovery_runner or _default_recovery_runner)(_release_recover_argv(request))
        if type(inner) is not ControlResult or inner.returncode != 0 or _DIGEST_RE.fullmatch(inner.digest) is None:
            return ControlResult(2, "")
        result_digest = hashlib.sha256(json.dumps({"request_digest": request_digest,
                                                   "inner_result_digest": inner.digest,
                                                   "returncode": 0}, sort_keys=True,
                                                  separators=(",", ":")).encode("utf-8")).hexdigest()
        def finish_recovery(state: dict[str, object]) -> None:
            recovery = state.get("recovery")
            if state.get("state") != "SEALED_RECOVERING" or type(recovery) is not dict or recovery.get("request_digest") != request_digest:
                raise ValueError("recovery takeover detected")
            recovery["result_digest"] = result_digest
            recovery["recovered_epoch"] = time.time()
            state["state"] = "SEALED_RECOVERED"
        registry.mutate(finish_recovery)
        return ControlResult(0, result_digest)
    finally:
        if held_lock is not None:
            os.close(held_lock)


_EXTERNAL_PUBLICATION_PREFIX = "matrix-priority-1-3-release "
_EXTERNAL_PUBLICATION_ENVELOPE_FIELDS = frozenset(("schema", "kind", "state", "record"))
_EXTERNAL_ATTEMPT_COMMON_FIELDS = frozenset((
    "release_key", "transaction_identity", "transaction_id", "boundary", "phase",
    "attempt_index", "attempt_id", "stage", "bundle", "evidence_digest",
))
_EXTERNAL_ATTEMPT_STATE_FIELDS = {
    "PENDING": _EXTERNAL_ATTEMPT_COMMON_FIELDS,
    "ACCEPTED": _EXTERNAL_ATTEMPT_COMMON_FIELDS | {"note_id", "terminal_kind", "remote_record_digest"},
    "ROLLED_BACK": _EXTERNAL_ATTEMPT_COMMON_FIELDS | {
        "note_id", "terminal_kind", "remote_record_digest", "verification_digest",
    },
}
_EXTERNAL_FINAL_FIELDS = _EXTERNAL_ATTEMPT_COMMON_FIELDS | {
    "note_id", "terminal_kind", "remote_record_digest", "accepted_record_digest",
    "verification_digest",
}


def _validate_external_publication_document(
    value: object, *, transaction_identity: str | None = None,
    transaction_id: str | None = None, boundary: str | None = None,
    attempt_ids: tuple[str, ...] | None = None,
) -> dict[str, object]:
    """Validate and detach the sole schema-v1 external publication contract."""
    if (type(value) is not dict or frozenset(value) != _EXTERNAL_PUBLICATION_ENVELOPE_FIELDS
            or type(value.get("schema")) is not int or value["schema"] != 1
            or value.get("kind") not in {"ATTEMPT", "FINAL"}
            or type(value.get("record")) is not dict):
        raise ValueError("matching GitLab release note schema is invalid")
    kind, state, record = value["kind"], value.get("state"), value["record"]
    expected_fields = (_EXTERNAL_ATTEMPT_STATE_FIELDS.get(state) if kind == "ATTEMPT"
                       else (_EXTERNAL_FINAL_FIELDS if state == "FINAL" else None))
    if expected_fields is None or frozenset(record) != expected_fields:
        raise ValueError("matching GitLab release note kind, state, or record is invalid")
    phase_index = {phase: index for index, (phase, _) in enumerate(_TRANSACTION_TOPOLOGY)}
    if (not _valid_digest(record.get("release_key"))
            or not _valid_digest(record.get("transaction_identity"))
            or not _valid_id(record.get("transaction_id"))
            or record.get("boundary") not in {"b1", "b2"}
            or record.get("phase") not in phase_index
            or type(record.get("attempt_index")) is not int
            or record["attempt_index"] != phase_index[record["phase"]]
            or not _valid_id(record.get("attempt_id"))
            or type(record.get("stage")) is not str or not os.path.isabs(record["stage"])
            or os.path.normpath(record["stage"]) != record["stage"]
            or type(record.get("bundle")) is not str or not os.path.isabs(record["bundle"])
            or os.path.normpath(record["bundle"]) != record["bundle"]
            or not _valid_digest(record.get("evidence_digest"))):
        raise ValueError("matching GitLab release note record types are invalid")
    if state != "PENDING":
        expected_terminal = "recovery" if state == "ROLLED_BACK" else "commit"
        if (type(record.get("note_id")) is not str or not record["note_id"].isdigit()
                or int(record["note_id"]) <= 0 or str(int(record["note_id"])) != record["note_id"]
                or record.get("terminal_kind") != expected_terminal
                or not _valid_digest(record.get("remote_record_digest"))):
            raise ValueError("matching GitLab release note terminal binding is invalid")
    if state in {"ROLLED_BACK", "FINAL"} and not _valid_digest(record.get("verification_digest")):
        raise ValueError("matching GitLab release note verification binding is invalid")
    if state == "FINAL":
        if (record["phase"] != "final-deploy" or record["attempt_index"] != 2
                or not _valid_digest(record.get("accepted_record_digest"))):
            raise ValueError("matching GitLab final authority is invalid")
    if transaction_identity is not None and record["transaction_identity"] != transaction_identity:
        raise ValueError("matching GitLab release note transaction identity is invalid")
    if transaction_id is not None and record["transaction_id"] != transaction_id:
        raise ValueError("matching GitLab release note transaction ID is invalid")
    if boundary is not None and record["boundary"] != boundary:
        raise ValueError("matching GitLab release note boundary is invalid")
    if attempt_ids is not None:
        index = record["attempt_index"]
        if (type(attempt_ids) is not tuple or len(attempt_ids) != len(_TRANSACTION_TOPOLOGY)
                or any(not _valid_id(item) for item in attempt_ids)
                or len(set(attempt_ids)) != len(attempt_ids)
                or record["attempt_id"] != attempt_ids[index]):
            raise ValueError("matching GitLab release note attempt is not canonical")
    return json.loads(_canonical_json_bytes(value).decode("ascii"))


class GitLabEvidenceStore:
    """Fail-closed GitLab MR-note external publication authority."""

    def __init__(self, project: str, mr: str, token_file: str) -> None:
        import urllib.parse
        import urllib.request
        self._urllib_parse = urllib.parse
        self._urllib_request = urllib.request
        if not str(project).isdigit() or not str(mr).isdigit():
            raise ValueError("GitLab project and MR must be numeric")
        token_path = Path(token_file)
        metadata = token_path.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.geteuid() or metadata.st_nlink != 1):
            raise ValueError("GitLab token must be an owner-only regular file")
        self._token = token_path.read_text(encoding="utf-8").strip()
        if not self._token or any(character in self._token for character in "\r\n\0"):
            raise ValueError("GitLab token is invalid")
        base = os.environ.get("CI_API_V4_URL", "https://gitlab.com/api/v4").rstrip("/")
        self._notes_url = (f"{base}/projects/{urllib.parse.quote(str(project), safe='')}"
                           f"/merge_requests/{urllib.parse.quote(str(mr), safe='')}/notes")
        self._publication_authority = None

    def bind_publication_authority(
        self, identity: CanonicalReleaseIdentity, transaction: dict[str, object], *,
        boundary: str, stage: str, bundle: str,
    ) -> None:
        attempts = transaction.get("attempts") if type(transaction) is dict else None
        if (type(identity) is not CanonicalReleaseIdentity or type(attempts) is not list
                or len(attempts) != len(_TRANSACTION_TOPOLOGY)
                or transaction.get("release_key") != identity.release_key
                or transaction.get("transaction_identity") != identity.transaction_identity
                or boundary != identity.document["boundary"]):
            raise ValueError("external publication authority binding is invalid")
        self._publication_authority = {
            "release_key": identity.release_key,
            "transaction_identity": identity.transaction_identity,
            "transaction_id": transaction["transaction_id"], "boundary": boundary,
            "stage": stage, "bundle": bundle,
            "attempt_ids": tuple(item["attempt_id"] for item in attempts),
        }

    def _canonical_record(self, state: str, record: dict[str, object], note_id: str | None = None) -> dict[str, object]:
        authority = self._publication_authority
        if type(authority) is not dict or type(record) is not dict:
            raise ValueError("external publication authority is not bound")
        if state == "FINAL":
            expected = _RECONCILIATION_FINAL_RECORD_FIELDS
            if frozenset(record) != expected:
                raise ValueError("external final source record is invalid")
            attempt_id, phase, index = record.get("attempt_id"), "final-deploy", 2
            last = authority["attempt_ids"][index]
            if attempt_id != last:
                raise ValueError("external final attempt is not canonical")
            # The final authority binds the accepted candidate and terminal already
            # authenticated by the reconciliation journal.
            candidate = record.get("candidate_evidence_digest")
            terminal = record.get("remote_record_digest")
            source_note = record.get("note_id")
            result = {**{key: authority[key] for key in (
                "release_key", "transaction_identity", "transaction_id", "boundary", "stage", "bundle")},
                "phase": phase, "attempt_index": index, "attempt_id": attempt_id,
                "note_id": source_note, "evidence_digest": candidate,
                "terminal_kind": "commit", "remote_record_digest": terminal,
                "accepted_record_digest": record.get("accepted_record_digest"),
                "verification_digest": record.get("verification_digest")}
            return result
        base = frozenset(("boundary", "phase", "attempt_id", "stage", "bundle", "evidence_digest"))
        extras = {"PENDING": frozenset(), "ACCEPTED": frozenset(("remote_record_digest",)),
                  "ROLLED_BACK": frozenset(("remote_record_digest", "recovery_digest", "verification_digest"))}.get(state)
        if extras is None or frozenset(record) != base | extras:
            raise ValueError("external attempt source record is invalid")
        phase = record.get("phase")
        index_by_phase = {value[0]: index for index, value in enumerate(_TRANSACTION_TOPOLOGY)}
        index = index_by_phase.get(phase)
        if (type(index) is not int or record.get("attempt_id") != authority["attempt_ids"][index]
                or record.get("boundary") != authority["boundary"]
                or record.get("stage") != authority["stage"] or record.get("bundle") != authority["bundle"]):
            raise ValueError("external attempt source is not canonical")
        result = {**{key: authority[key] for key in (
            "release_key", "transaction_identity", "transaction_id", "boundary", "stage", "bundle")},
            "phase": phase, "attempt_index": index, "attempt_id": record["attempt_id"],
            "evidence_digest": record["evidence_digest"]}
        if state != "PENDING":
            if note_id is None:
                raise ValueError("external terminal note binding is absent")
            result.update(note_id=note_id, terminal_kind="recovery" if state == "ROLLED_BACK" else "commit",
                          remote_record_digest=record["remote_record_digest"])
        if state == "ROLLED_BACK":
            if record["recovery_digest"] != record["remote_record_digest"]:
                raise ValueError("external recovery terminal binding is inconsistent")
            result["verification_digest"] = record["verification_digest"]
        return result

    @staticmethod
    def _body(kind: str, state: str, record: dict[str, object]) -> str:
        document = _validate_external_publication_document(
            {"schema": 1, "kind": kind, "state": state, "record": record}
        )
        return _EXTERNAL_PUBLICATION_PREFIX + json.dumps(
            document, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )

    def _request(self, url: str, method: str, body: str) -> dict[str, object]:
        encoded = self._urllib_parse.urlencode({"body": body}).encode("ascii")
        request = self._urllib_request.Request(
            url, data=encoded, method=method,
            headers={"PRIVATE-TOKEN": self._token, "Content-Type": "application/x-www-form-urlencoded"},
        )
        with self._urllib_request.urlopen(request, timeout=10) as response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError("GitLab response is oversized")
        value = json.loads(raw)
        if type(value) is not dict:
            raise ValueError("GitLab response is invalid")
        return value

    # Complete bounded collection: GitLab allows 100 notes per page.  Twenty
    # pages / 2,000 notes is the hard fail-closed authority-discovery budget.
    _MAX_NOTE_PAGES = 20
    _MAX_NOTES = 2000

    def _list_notes(self) -> list[dict[str, object]]:
        separator = "&" if "?" in self._notes_url else "?"
        collected: list[dict[str, object]] = []
        seen_ids: set[int] = set()
        seen_pages: set[tuple[int, ...]] = set()
        page = 1
        while True:
            if page > self._MAX_NOTE_PAGES:
                raise ValueError("GitLab notes exceed the hard page cap")
            url = (self._notes_url + separator
                   + f"page={page}&per_page=100&order_by=updated_at&sort=desc")
            request = self._urllib_request.Request(
                url, method="GET", headers={"PRIVATE-TOKEN": self._token},
            )
            with self._urllib_request.urlopen(request, timeout=10) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError("GitLab notes response is oversized")
                headers = getattr(response, "headers", None)
                next_header = headers.get("X-Next-Page") if hasattr(headers, "get") else None
            value = json.loads(raw)
            if (type(value) is not list or len(value) > 100
                    or any(type(item) is not dict for item in value)):
                raise ValueError("GitLab notes page envelope is malformed")
            page_ids: list[int] = []
            for item in value:
                note_id = item.get("id")
                if type(note_id) is not int or note_id <= 0:
                    raise ValueError("GitLab note ID is malformed")
                if note_id in seen_ids:
                    raise ValueError("GitLab note ID is repeated across pages")
                seen_ids.add(note_id)
                page_ids.append(note_id)
            signature = tuple(page_ids)
            if signature in seen_pages:
                raise ValueError("GitLab notes page is repeated")
            seen_pages.add(signature)
            if len(collected) + len(value) > self._MAX_NOTES:
                raise ValueError("GitLab notes exceed the hard note cap")
            collected.extend(value)

            if next_header is not None:
                if type(next_header) is not str:
                    raise ValueError("GitLab next-page metadata is malformed")
                if next_header == "":
                    return collected
                if (not next_header.isdigit() or str(int(next_header)) != next_header
                        or int(next_header) != page + 1):
                    raise ValueError("GitLab next-page metadata is malformed or repeated")
                page = int(next_header)
                continue
            if len(value) < 100:
                return collected
            page += 1

    def discover(self, *, transaction_identity: str, transaction_id: str,
                 boundary: str, attempt_ids: tuple[str, ...]) -> list[dict[str, object]]:
        if (not _valid_digest(transaction_identity) or not _valid_id(transaction_id)
                or boundary not in {"b1", "b2"} or type(attempt_ids) is not tuple
                or len(attempt_ids) != len(_TRANSACTION_TOPOLOGY)
                or any(not _valid_id(item) for item in attempt_ids)
                or len(set(attempt_ids)) != len(attempt_ids)):
            raise ValueError("external note discovery identity is invalid")
        discovered, seen_attempts = [], set()
        seen_final = False
        for note in self._list_notes():
            note_id, body = note.get("id"), note.get("body")
            if type(note_id) is not int or note_id <= 0 or type(body) is not str or not body.startswith(_EXTERNAL_PUBLICATION_PREFIX):
                continue
            try:
                raw = json.loads(body[len(_EXTERNAL_PUBLICATION_PREFIX):])
            except json.JSONDecodeError as error:
                raise ValueError("matching GitLab release note is malformed") from error
            document = _validate_external_publication_document(
                raw, transaction_identity=transaction_identity, transaction_id=transaction_id,
                boundary=boundary, attempt_ids=attempt_ids,
            )
            if document["kind"] == "FINAL":
                if seen_final:
                    raise ValueError("matching GitLab final authority is duplicated")
                seen_final = True
                discovered.append({"note_id": str(note_id), "state": "FINAL",
                                   "record": document["record"]})
                continue
            attempt_id = document["record"]["attempt_id"]
            if attempt_id in seen_attempts:
                raise ValueError("matching GitLab release note attempt is duplicated")
            if document["state"] != "PENDING" and document["record"]["note_id"] != str(note_id):
                raise ValueError("matching GitLab release note ID binding is invalid")
            seen_attempts.add(attempt_id)
            discovered.append({"note_id": str(note_id), "state": document["state"],
                               "record": document["record"]})
        return discovered

    def discover_final(self, record: dict[str, object]) -> dict[str, object] | None:
        authority = self._publication_authority
        canonical = self._canonical_record("FINAL", record)
        if type(authority) is not dict:
            raise ValueError("external publication authority is not bound")
        publications = self.discover(
            transaction_identity=authority["transaction_identity"],
            transaction_id=authority["transaction_id"], boundary=authority["boundary"],
            attempt_ids=authority["attempt_ids"],
        )
        finals = [item for item in publications
                  if type(item) is dict and item.get("state") == "FINAL"]
        if len(finals) > 1:
            raise ValueError("matching GitLab final authority is duplicated")
        if not finals:
            return None
        if finals[0].get("record") != canonical:
            raise ValueError("matching GitLab final authority conflicts with requested final")
        return json.loads(_canonical_json_bytes(finals[0]).decode("ascii"))

    def pending(self, record: dict[str, object]) -> str:
        canonical = self._canonical_record("PENDING", record)
        value = self._request(self._notes_url, "POST", self._body("ATTEMPT", "PENDING", canonical))
        note_id = value.get("id")
        if type(note_id) is not int or note_id <= 0:
            raise ValueError("GitLab note ID is invalid")
        return str(note_id)

    def reconcile(self, note_id: str, state: str, record: dict[str, object]) -> None:
        if state not in {"ACCEPTED", "ROLLED_BACK"} or not note_id.isdigit():
            raise ValueError("GitLab reconciliation is invalid")
        canonical = self._canonical_record(state, record, note_id)
        self._request(f"{self._notes_url}/{note_id}", "PUT", self._body("ATTEMPT", state, canonical))

    def final(self, record: dict[str, object]) -> None:
        canonical = self._canonical_record("FINAL", record)
        self._request(self._notes_url, "POST", self._body("FINAL", "FINAL", canonical))


def _remaining_integer(deadline: float, clock: Callable[[], float], cap: int) -> int:
    remaining = int(min(float(cap), deadline - clock()))
    if remaining <= 0:
        raise TimeoutError("guarded attempt deadline expired")
    return remaining


# One complete acceptance can spend 900s focused + 5400s full discovery and
# 180s on the remaining remote/local checks.  Every invocation must be fully
# fundable before it starts; the immutable attempt deadline remains authoritative.
_AGGREGATE_ACCEPTANCE_BUDGET_SECONDS = 6480
_POST_ACCEPTANCE_PUBLICATION_RESERVE_SECONDS = 60
_POST_MUTATION_VERIFICATION_WINDOW_SECONDS = (
    _AGGREGATE_ACCEPTANCE_BUDGET_SECONDS + _POST_ACCEPTANCE_PUBLICATION_RESERVE_SECONDS
)


def _funded_acceptance_timeout(deadline: float, clock: Callable[[], float]) -> int:
    remaining = deadline - clock()
    if remaining < _AGGREGATE_ACCEPTANCE_BUDGET_SECONDS:
        raise TimeoutError("insufficient immutable attempt budget for complete acceptance")
    return _AGGREGATE_ACCEPTANCE_BUDGET_SECONDS


def _invoke_funded_acceptance(
    runner: Callable[[argparse.Namespace, int], int], arguments: argparse.Namespace,
    deadline: float, clock: Callable[[], float],
) -> int:
    # Admission happens before the runner can create a subprocess or remote operation.
    timeout = _funded_acceptance_timeout(deadline, clock)
    return runner(arguments, timeout)


def _post_mutation_verification_deadline(clock: Callable[[], float]) -> float:
    return clock() + _POST_MUTATION_VERIFICATION_WINDOW_SECONDS


def _production_acceptance_runner(arguments: argparse.Namespace, timeout: int) -> int:
    argv = [
        f"{arguments.stage}/venv/bin/python", "-I", "-B",
        f"{arguments.stage}/deploy/verify_priority_1_3_acceptance.py",
        "--boundary", arguments.boundary, "--mode", arguments.mode,
        "--ssh-target", arguments.ssh_target,
        "--ssh-identity-file", arguments.ssh_identity_file,
        "--ssh-known-hosts-file", arguments.ssh_known_hosts_file,
        "--app-root", arguments.destination_root,
        "--expected-sha", arguments.accepted_sha,
        "--expected-version", arguments.accepted_version,
        "--session-cookie-file", arguments.session_cookie_file,
        "--evidence-file", arguments.evidence_file,
        "--attempt-id", arguments.attempt_id,
        "--acceptance-operation-id", arguments.acceptance_operation_id,
        "--attempt-registry", arguments.attempt_registry,
        "--release-spec", f"{arguments.stage}/release-spec.json",
        "--release-spec-digest", arguments.release_spec_digest,
    ]
    environment = dict(os.environ)
    if getattr(arguments, "chromium", None):
        environment["MATRIX_CHROMIUM"] = arguments.chromium
    completed = subprocess.run(
        argv, check=False, text=True, capture_output=True, timeout=timeout,
        env=environment, shell=False,
    )
    return completed.returncode if completed.returncode else (2 if completed.stderr else 0)


def _validated_attempt_registry(attempt_id: object, registry: object) -> str:
    if (type(attempt_id) is not str or _ID_RE.fullmatch(attempt_id) is None
            or type(registry) is not str
            or registry != f"/run/matrix-priority-1-3/attempts/{attempt_id}.json"):
        raise ValueError("attempt registry is not the canonical transaction backlink")
    return registry


def _full_recovery_request(
    arguments: argparse.Namespace, phase: str, attempt_id: str, registry: str,
) -> dict[str, object]:
    registry = _validated_attempt_registry(attempt_id, registry)
    operation_id = hashlib.sha256(f"{attempt_id}\0recovery".encode("utf-8")).hexdigest()[:32]
    active = arguments.initial_service_state == "active"
    return {
        "schema": 1, "boundary": arguments.boundary, "phase": phase,
        "attempt_id": attempt_id, "operation_id": operation_id,
        "registry": registry,
        "stage": arguments.stage, "release_root": f"{arguments.stage}/release",
        "destination_root": arguments.destination_root, "bundle": arguments.bundle,
        "release_manifest": f"{arguments.stage}/release-manifest.json",
        "backup_manifest": f"{arguments.bundle}/backup-manifest.json",
        "release_spec": f"{arguments.stage}/release-spec.json",
        "provenance_digest": hashlib.sha256(
            f"{arguments.expected_sha}\0{arguments.stage}\0{arguments.bundle}".encode("utf-8")
        ).hexdigest(),
        "evidence_digest": "0" * 64, "pending_note_id": "0",
        "service": arguments.service, "initial_service_state": arguments.initial_service_state,
        "health_url": arguments.health_url if active else None,
        "health_status": arguments.health_status if active else None,
        "health_timeout_seconds": arguments.health_timeout_seconds if active else None,
        "guard_unit": f"matrix-p13-recover-{attempt_id}.service",
    }


def _release_operation_argv(arguments: argparse.Namespace, operation: str) -> list[str]:
    argv = [
        f"{arguments.stage}/venv/bin/python", "-I", "-B",
        f"{arguments.stage}/deploy/release_bundle.py", operation,
        "--release-root", f"{arguments.stage}/release",
        "--destination-root", arguments.destination_root,
        "--bundle", arguments.bundle,
        "--release-manifest", f"{arguments.stage}/release-manifest.json",
        "--backup-manifest", f"{arguments.bundle}/backup-manifest.json",
        "--service", arguments.service,
    ]
    if operation == "deploy":
        argv.extend(("--release-spec", f"{arguments.stage}/release-spec.json"))
    return argv


_RECONCILIATION_ERRORS = (OSError, ValueError, TypeError, RuntimeError,
                          subprocess.SubprocessError, json.JSONDecodeError)
_RECONCILIATION_FILE = "guarded-release-reconciliation.json"


_RECONCILIATION_FIELDS = frozenset(("schema", "identity", "boundary", "attempts", "final"))
_RECONCILIATION_ATTEMPT_FIELDS = frozenset((
    "phase", "operation", "attempt_id", "note_id", "accepted_record", "evidence_file",
    "state", "remaining_seconds", "deadline_epoch",
))
_RECONCILIATION_ACCEPTED_FIELDS = frozenset((
    "boundary", "phase", "attempt_id", "stage", "bundle", "evidence_digest",
    "remote_record_digest",
))
_RECONCILIATION_FINAL_FIELDS = frozenset((
    "state", "record", "evidence_file", "remaining_seconds", "deadline_epoch",
))
_RECONCILIATION_FINAL_RECORD_FIELDS = frozenset((
    "boundary", "attempt_id", "note_id", "candidate_evidence_digest",
    "remote_record_digest", "accepted_record_digest", "verification_digest",
))


def _validate_reconciliation(
    value: object, *, identity: str | None = None, boundary: str | None = None,
    attempt_ids: tuple[str, ...] | None = None, stage: str | None = None,
    bundle: str | None = None,
) -> dict[str, object]:
    """Validate and deeply detach the complete local reconciliation authority."""
    if (type(value) is not dict or frozenset(value) != _RECONCILIATION_FIELDS
            or not _is_schema_v1(value.get("schema"))
            or not _valid_digest(value.get("identity"))
            or value.get("boundary") not in {"b1", "b2"}
            or type(value.get("attempts")) is not list
            or len(value["attempts"]) > len(_TRANSACTION_TOPOLOGY)):
        raise ValueError("reconciliation journal schema is invalid")
    if identity is not None and value["identity"] != identity:
        raise ValueError("reconciliation journal transaction identity is invalid")
    if boundary is not None and value["boundary"] != boundary:
        raise ValueError("reconciliation journal release boundary is invalid")
    if attempt_ids is not None:
        if (type(attempt_ids) is not tuple or len(attempt_ids) != len(_TRANSACTION_TOPOLOGY)
                or any(not _valid_id(item) for item in attempt_ids)
                or len(set(attempt_ids)) != len(attempt_ids)):
            raise ValueError("reconciliation transaction attempt authority is invalid")

    seen: set[str] = set()
    for index, entry in enumerate(value["attempts"]):
        phase, operation = _TRANSACTION_TOPOLOGY[index]
        if (type(entry) is not dict or frozenset(entry) != _RECONCILIATION_ATTEMPT_FIELDS
                or (entry.get("phase"), entry.get("operation")) != (phase, operation)
                or not _valid_id(entry.get("attempt_id")) or entry["attempt_id"] in seen
                or entry.get("state") not in {"COMMITTED_UNRECONCILED", "ACCEPTED"}
                or type(entry.get("note_id")) is not str or not entry["note_id"].isdigit()
                or int(entry["note_id"]) <= 0 or str(int(entry["note_id"])) != entry["note_id"]
                or type(entry.get("evidence_file")) is not str
                or not os.path.isabs(entry["evidence_file"])
                or os.path.normpath(entry["evidence_file"]) != entry["evidence_file"]
                or Path(entry["evidence_file"]).name != f"{entry['attempt_id']}-candidate.json"
                or type(entry.get("remaining_seconds")) is not int
                or not 0 <= entry["remaining_seconds"] <= 60
                or type(entry.get("deadline_epoch")) not in {int, float}
                or not 0 <= float(entry["deadline_epoch"]) < float("inf")):
            raise ValueError("reconciliation attempt schema is invalid")
        if (index + 1 < len(value["attempts"]) and entry["state"] != "ACCEPTED"
                or entry["state"] == "ACCEPTED" and entry["remaining_seconds"] <= 0):
            raise ValueError("reconciliation attempt sequence or accepted lease is invalid")
        if attempt_ids is not None and entry["attempt_id"] != attempt_ids[index]:
            raise ValueError("reconciliation attempt is not bound to its transaction")
        accepted = entry.get("accepted_record")
        if (type(accepted) is not dict or frozenset(accepted) != _RECONCILIATION_ACCEPTED_FIELDS
                or accepted.get("boundary") != value["boundary"]
                or accepted.get("phase") != phase
                or accepted.get("attempt_id") != entry["attempt_id"]
                or type(accepted.get("stage")) is not str or not os.path.isabs(accepted["stage"])
                or os.path.normpath(accepted["stage"]) != accepted["stage"]
                or type(accepted.get("bundle")) is not str or not os.path.isabs(accepted["bundle"])
                or os.path.normpath(accepted["bundle"]) != accepted["bundle"]
                or not _valid_digest(accepted.get("evidence_digest"))
                or not _valid_digest(accepted.get("remote_record_digest"))
                or stage is not None and accepted.get("stage") != stage
                or bundle is not None and accepted.get("bundle") != bundle
                or index and (accepted.get("stage"), accepted.get("bundle")) != (
                    value["attempts"][0]["accepted_record"]["stage"],
                    value["attempts"][0]["accepted_record"]["bundle"],
                )):
            raise ValueError("reconciliation accepted record is invalid or unbound")
        seen.add(entry["attempt_id"])

    final = value.get("final")
    if final is not None:
        attempts = value["attempts"]
        if (type(final) is not dict or frozenset(final) != _RECONCILIATION_FINAL_FIELDS
                or final.get("state") not in {"FINAL_UNRECONCILED", "AUTHORITATIVE_FINAL"}
                or len(attempts) != len(_TRANSACTION_TOPOLOGY)
                or any(entry["state"] != "ACCEPTED" for entry in attempts)
                or type(final.get("evidence_file")) is not str
                or not os.path.isabs(final["evidence_file"])
                or os.path.normpath(final["evidence_file"]) != final["evidence_file"]
                or Path(final["evidence_file"]).name != f"{attempts[-1]['attempt_id']}-final-verification.json"
                or type(final.get("remaining_seconds")) is not int
                or not 0 <= final["remaining_seconds"] <= 60
                or type(final.get("deadline_epoch")) not in {int, float}
                or not 0 <= float(final["deadline_epoch"]) < float("inf")
                or not 0 <= float(final["deadline_epoch"]) - float(attempts[-1]["deadline_epoch"])
                <= _POST_MUTATION_VERIFICATION_WINDOW_SECONDS):
            raise ValueError("reconciliation final entry schema is invalid")
        if final["state"] == "AUTHORITATIVE_FINAL" and final["remaining_seconds"] <= 0:
            raise ValueError("authoritative reconciliation final lease is invalid")
        record = final.get("record")
        accepted_digest = hashlib.sha256(_canonical_json_bytes(
            attempts[-1]["accepted_record"]
        )).hexdigest()
        if (type(record) is not dict or frozenset(record) != _RECONCILIATION_FINAL_RECORD_FIELDS
                or record.get("boundary") != value["boundary"]
                or record.get("attempt_id") != attempts[-1]["attempt_id"]
                or record.get("note_id") != attempts[-1]["note_id"]
                or record.get("candidate_evidence_digest") != attempts[-1]["accepted_record"]["evidence_digest"]
                or record.get("remote_record_digest") != attempts[-1]["accepted_record"]["remote_record_digest"]
                or record.get("accepted_record_digest") != accepted_digest
                or not _valid_digest(record.get("verification_digest"))):
            raise ValueError("reconciliation final record is invalid or unbound")
    try:
        return json.loads(_canonical_json_bytes(value).decode("ascii"))
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("reconciliation journal cannot be detached") from error


def _active_release_document(arguments: argparse.Namespace) -> dict[str, object]:
    return {
        "schema": 1, "transaction_version": arguments.transaction_version,
        "boundary": arguments.boundary, "expected_sha": arguments.expected_sha,
        "expected_version": arguments.expected_version, "previous_sha": arguments.previous_sha,
        "previous_version": arguments.previous_version, "release_stamp": arguments.release_stamp,
        "release_manifest_digest": arguments.release_manifest_digest,
        "backup_manifest_digest": arguments.backup_manifest_digest,
        "release_spec_digest": arguments.release_spec_digest,
        "destination_identity": {"root": arguments.destination_identity, "service": arguments.service},
        "gitlab": {"project": arguments.gitlab_project, "mr": arguments.gitlab_mr},
    }


def _validated_active_transaction_result(
    result: object, identity: CanonicalReleaseIdentity, *,
    expected_transaction_id: object | None = None,
) -> tuple[dict[str, object], str]:
    """Validate and detach one complete active-authority response envelope."""
    if (type(result) is not ControlResult or type(result.returncode) is not int
            or result.returncode != 0 or type(result.digest) is not str
            or _DIGEST_RE.fullmatch(result.digest) is None or type(result.terminal) is not dict):
        raise ValueError("remote transaction control result is invalid")
    try:
        record = json.loads(_canonical_json_bytes(result.terminal).decode("ascii"))
        record = validate_transaction_record(record, identity.release_key)
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as error:
        if str(error).startswith("transaction attempt identity"):
            raise ValueError("remote transaction attempt identity is invalid") from error
        raise ValueError("remote transaction record is invalid") from error
    if (record["release_key"] != identity.release_key
            or record["release_document"] != identity.document
            or record["transaction_identity"] != identity.transaction_identity):
        raise ValueError("remote transaction release identity is invalid")
    if (expected_transaction_id is not None
            and record["transaction_id"] != expected_transaction_id):
        raise ValueError("remote transaction identity does not match inspection")
    if result.digest != record["record_digest"]:
        raise ValueError("remote transaction control digest is not bound to its terminal record")
    return record, result.digest


@dataclass(frozen=True)
class ActiveTransactionAuthority:
    """Detached canonical authority retained for the active coordinator lifecycle."""

    identity: CanonicalReleaseIdentity
    record: dict[str, object]
    digest: str
    claimant_id: str
    claimed_epoch: int
    lease_deadline_epoch: int


def _claim_active_transaction(
    arguments: argparse.Namespace, executor: PinnedExecutor,
    identity: CanonicalReleaseIdentity, prepared: dict[str, object], *, coordinator_epoch: int,
) -> ActiveTransactionAuthority:
    """Claim exactly one canonical transaction before any executor mutation."""
    prepared = validate_transaction_record(
        json.loads(_canonical_json_bytes(prepared).decode("ascii")), identity.release_key,
    )
    lease_deadline_epoch = prepared["original_deadline_epoch"]
    existing_claim = prepared["claim"]
    claimed_epoch = (existing_claim["claimed_epoch"]
                     if type(existing_claim) is dict else coordinator_epoch)
    if (type(coordinator_epoch) is not int or type(claimed_epoch) is not int
            or type(lease_deadline_epoch) is not int
            or not prepared["created_epoch"] <= claimed_epoch < lease_deadline_epoch
            or not prepared["created_epoch"] <= coordinator_epoch < lease_deadline_epoch):
        raise ValueError("active transaction claim lease is invalid")
    claim_identity = {
        "release_key": identity.release_key,
        "transaction_identity": identity.transaction_identity,
        "transaction_id": prepared["transaction_id"],
        "claimed_epoch": claimed_epoch,
        "lease_deadline_epoch": lease_deadline_epoch,
    }
    claimant_id = _domain_digest("active-coordinator-claimant/v1", claim_identity)[:32]
    request_digest = _domain_digest(
        "active-coordinator-claim/v1", {**claim_identity, "claimant_id": claimant_id},
    )
    request = {
        "schema": 1, "registry_root": arguments.transaction_registry_root,
        "release_key": identity.release_key, "transaction_id": prepared["transaction_id"],
        "claimant_id": claimant_id, "request_digest": request_digest,
        "claimed_epoch": claimed_epoch,
    }
    validate_privileged_request("remote-transaction-claim", request)
    claimed = executor.control_remote("remote-transaction-claim", request, operation_timeout=60)
    record, digest = _validated_active_transaction_result(
        claimed, identity, expected_transaction_id=prepared["transaction_id"],
    )
    expected = json.loads(_canonical_json_bytes(prepared).decode("ascii"))
    expected["claim"] = {
        "claimant_id": claimant_id, "request_digest": request_digest,
        "claimed_epoch": claimed_epoch,
    }
    expected["record_digest"] = transaction_record_digest(expected)
    if record != expected:
        raise ValueError("remote transaction claim terminal is not canonical")
    return ActiveTransactionAuthority(
        identity=identity, record=record, digest=digest, claimant_id=claimant_id,
        claimed_epoch=claimed_epoch, lease_deadline_epoch=lease_deadline_epoch,
    )


def _active_transaction_epoch(authority: ActiveTransactionAuthority,
                              wall_clock: Callable[[], float]) -> int:
    value = int(wall_clock())
    epochs = [authority.claimed_epoch]
    record = authority.record
    advance = record.get("last_advance")
    if type(advance) is dict:
        epochs.append(int(advance["transition_epoch"]))
    for attempt in record["attempts"]:
        transition = attempt.get("last_transition")
        if type(transition) is dict:
            field = "terminal_epoch" if "terminal_epoch" in transition else "transition_epoch"
            epochs.append(int(transition[field]))
    value = max([value, *epochs])
    if not record["created_epoch"] <= value <= authority.lease_deadline_epoch:
        raise ValueError("active transaction epoch is outside its immutable lease")
    return value


def _active_transaction_request_digest(kind: str, request: dict[str, object]) -> str:
    unsigned = dict(request)
    unsigned.pop("request_digest", None)
    return _domain_digest(f"active-transaction-{kind}/v1", unsigned)


def _active_transaction_expected_successor(authority: ActiveTransactionAuthority, kind: str,
                                           request: dict[str, object]) -> dict[str, object]:
    expected = json.loads(_canonical_json_bytes(authority.record).decode("ascii"))
    index = expected["current_phase_index"]
    attempt = expected["attempts"][index]
    if attempt["phase"] != request["phase"]:
        raise ValueError("active transaction phase is not current")
    if kind == "transition":
        metadata = {key: request[key] for key in (
            "from_state", "to_state", "request_digest", "transition_epoch",
        )}
        attempt["state"] = request["to_state"]
        attempt["last_transition"] = metadata
    elif kind == "terminal":
        metadata = {key: request[key] for key in (
            "kind", "from_state", "to_state", "request_digest", "result_digest", "terminal_epoch",
        )}
        attempt["state"] = request["to_state"]
        attempt["last_transition"] = metadata
        attempt["commit" if request["kind"] == "commit" else "recovery"] = metadata
    elif kind == "advance":
        metadata = {key: request[key] for key in ("phase", "request_digest", "transition_epoch")}
        if index == 2:
            expected["state"] = "THREE_ATTEMPTS_ACCEPTED"
        else:
            expected["attempts"][index + 1]["state"] = "READY"
            expected["current_phase_index"] = index + 1
        expected["last_advance"] = metadata
    else:
        raise ValueError("unknown active transaction operation")
    expected["record_digest"] = transaction_record_digest(expected)
    return expected


def _active_transaction_control(arguments: argparse.Namespace, executor: PinnedExecutor,
                                authority: ActiveTransactionAuthority, kind: str,
                                request: dict[str, object]) -> ActiveTransactionAuthority:
    helper = f"remote-transaction-{kind}"
    validate_privileged_request(helper, request)
    expected = _active_transaction_expected_successor(authority, kind, request)
    try:
        result = executor.control_remote(helper, request, operation_timeout=60)
    except (OSError, TimeoutError, subprocess.SubprocessError):
        inspect_request = {
            "schema": 1, "registry_root": arguments.transaction_registry_root,
            "release_key": authority.identity.release_key,
            "transaction_id": authority.record["transaction_id"],
        }
        result = executor.control_remote(
            "remote-transaction-inspect", inspect_request, operation_timeout=60,
        )
    record, digest = _validated_active_transaction_result(
        result, authority.identity, expected_transaction_id=authority.record["transaction_id"],
    )
    if record != expected:
        raise ValueError("remote transaction response is not the exact canonical successor")
    if (record["claim"] != authority.record["claim"]
            or record["claim"]["claimant_id"] != authority.claimant_id
            or record["claim"]["claimed_epoch"] != authority.claimed_epoch
            or record["original_deadline_epoch"] != authority.lease_deadline_epoch):
        raise ValueError("remote transaction response did not retain active authority")
    return ActiveTransactionAuthority(
        identity=authority.identity, record=record, digest=digest,
        claimant_id=authority.claimant_id, claimed_epoch=authority.claimed_epoch,
        lease_deadline_epoch=authority.lease_deadline_epoch,
    )


def _active_transition(arguments: argparse.Namespace, executor: PinnedExecutor,
                       authority: ActiveTransactionAuthority, phase: str,
                       from_state: str, to_state: str,
                       wall_clock: Callable[[], float]) -> ActiveTransactionAuthority:
    request = {
        "schema": 1, "registry_root": arguments.transaction_registry_root,
        "release_key": authority.identity.release_key,
        "transaction_id": authority.record["transaction_id"],
        "claimant_id": authority.claimant_id, "phase": phase,
        "from_state": from_state, "to_state": to_state,
        "transition_epoch": _active_transaction_epoch(authority, wall_clock),
    }
    request["request_digest"] = _active_transaction_request_digest("transition", request)
    return _active_transaction_control(arguments, executor, authority, "transition", request)


def _active_terminal(arguments: argparse.Namespace, executor: PinnedExecutor,
                     authority: ActiveTransactionAuthority, phase: str, kind: str,
                     from_state: str, to_state: str, result_digest: str,
                     wall_clock: Callable[[], float]) -> ActiveTransactionAuthority:
    if type(result_digest) is not str or _DIGEST_RE.fullmatch(result_digest) is None:
        raise ValueError("active transaction terminal result digest is invalid")
    request = {
        "schema": 1, "registry_root": arguments.transaction_registry_root,
        "release_key": authority.identity.release_key,
        "transaction_id": authority.record["transaction_id"],
        "claimant_id": authority.claimant_id, "phase": phase, "kind": kind,
        "from_state": from_state, "to_state": to_state, "result_digest": result_digest,
        "terminal_epoch": _active_transaction_epoch(authority, wall_clock),
    }
    request["request_digest"] = _active_transaction_request_digest("terminal", request)
    return _active_transaction_control(arguments, executor, authority, "terminal", request)


def _active_advance(arguments: argparse.Namespace, executor: PinnedExecutor,
                    authority: ActiveTransactionAuthority, phase: str,
                    wall_clock: Callable[[], float]) -> ActiveTransactionAuthority:
    request = {
        "schema": 1, "registry_root": arguments.transaction_registry_root,
        "release_key": authority.identity.release_key,
        "transaction_id": authority.record["transaction_id"],
        "claimant_id": authority.claimant_id, "phase": phase,
        "transition_epoch": _active_transaction_epoch(authority, wall_clock),
    }
    request["request_digest"] = _active_transaction_request_digest("advance", request)
    return _active_transaction_control(arguments, executor, authority, "advance", request)


def _prepare_active_transaction(
    arguments: argparse.Namespace, executor: PinnedExecutor, *, created_epoch: int,
) -> tuple[CanonicalReleaseIdentity, dict[str, object]]:
    identity = canonical_release_identity(_active_release_document(arguments))
    request = {
        "schema": 1, "registry_root": arguments.transaction_registry_root,
        "release_key": identity.release_key, "release_document": identity.document,
        "execution_contract": {
            "schema": 1, "stage": arguments.stage, "bundle": arguments.bundle,
            "destination_root": arguments.destination_root, "service": arguments.service,
        },
        "created_epoch": created_epoch,
        "original_deadline_epoch": created_epoch + (
            len(_TRANSACTION_TOPOLOGY) * arguments.watchdog_seconds
            + 2 * _POST_MUTATION_VERIFICATION_WINDOW_SECONDS
        ),
    }
    validate_privileged_request("remote-transaction-create-or-query", request)
    prepared = executor.control_remote(
        "remote-transaction-create-or-query", request, operation_timeout=60,
    )
    record, prepared_digest = _validated_active_transaction_result(prepared, identity)
    inspect_request = {
        "schema": 1, "registry_root": arguments.transaction_registry_root,
        "release_key": identity.release_key, "transaction_id": record["transaction_id"],
    }
    inspected = executor.control_remote("remote-transaction-inspect", inspect_request, operation_timeout=60)
    inspected_record, inspected_digest = _validated_active_transaction_result(
        inspected, identity, expected_transaction_id=record["transaction_id"],
    )
    if (inspected_record, inspected_digest) != (record, prepared_digest):
        raise ValueError("remote transaction inspection mismatch")
    return identity, record


def _write_reconciliation(path: Path, record: dict[str, object]) -> None:
    detached = _validate_reconciliation(record)
    encoded = (_canonical_json_bytes(detached) + b"\n")
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    temporary = f".{path.name}.{secrets.token_hex(16)}"
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                             0o600, dir_fd=parent_fd)
        os.fchmod(descriptor, 0o600)
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short reconciliation write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(parent_fd)


def _load_reconciliation(path: Path, identity: str) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return _validate_reconciliation(value, identity=identity)


_MAX_RECONCILIATION_EVIDENCE_BYTES = 16 * 1024 * 1024


def _bounded_local_evidence_digest(path: str, *, required: bool) -> str | None:
    """Hash one immutable local observer file without following descriptor races."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        if not required:
            return None
        raise ValueError("required reconciliation evidence is unavailable")
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1
                or metadata.st_size > _MAX_RECONCILIATION_EVIDENCE_BYTES):
            raise ValueError("reconciliation evidence metadata is unsafe")
        digest, size = hashlib.sha256(), 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_RECONCILIATION_EVIDENCE_BYTES:
                raise ValueError("reconciliation evidence is oversized")
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _validate_reconciliation_authenticity(
    journal: dict[str, object], arguments: argparse.Namespace, executor: PinnedExecutor,
    remote_attempts: list[dict[str, object]], *, wall_clock: Callable[[], float],
    cached_commit_terminals: dict[str, dict[str, object]] | None = None,
    allow_missing_final_evidence: bool = False,
) -> dict[str, dict[str, object]]:
    """Bind local reconciliation claims to evidence bytes and sealed remote commits."""
    if type(remote_attempts) is not list or len(remote_attempts) != len(_TRANSACTION_TOPOLOGY):
        raise ValueError("remote transaction attempt authority is incomplete")
    cached = dict(cached_commit_terminals or {})
    commit_terminals: dict[str, dict[str, object]] = {}
    for index, entry in enumerate(journal["attempts"]):
        accepted = entry["accepted_record"]
        local_digest = _bounded_local_evidence_digest(entry["evidence_file"], required=False)
        if local_digest is not None and local_digest != accepted["evidence_digest"]:
            raise ValueError("reconciliation evidence digest is not bound to local bytes")
        remote = remote_attempts[index]
        if (type(remote) is not dict or remote.get("attempt_id") != entry["attempt_id"]
                or type(remote.get("registry")) is not str):
            raise ValueError("reconciliation attempt is not bound to remote transaction authority")
        if entry["phase"] == "rollback-proof":
            recovery = remote.get("recovery")
            if (remote.get("state") not in {"RECOVERED_UNVERIFIED", "ROLLBACK_VERIFIED"}
                    or type(recovery) is not dict
                    or recovery.get("kind") != "recovery"
                    or recovery.get("to_state") != "RECOVERED_UNVERIFIED"
                    or recovery.get("result_digest") != accepted["remote_record_digest"]):
                raise ValueError("reconciliation record is not bound to authoritative recovery")
            continue
        operation_id = hashlib.sha256(
            f"{entry['attempt_id']}\0commit".encode("utf-8")
        ).hexdigest()[:32]
        query = {
            "schema": 1, "query": True, "boundary": journal["boundary"],
            "phase": entry["phase"], "attempt_id": entry["attempt_id"],
            "operation_id": operation_id, "registry": remote["registry"],
            "evidence_digest": accepted["evidence_digest"],
            "success_record": f"/run/matrix-priority-1-3/{entry['attempt_id']}/success.json",
        }
        remaining = min(int(entry["remaining_seconds"]),
                        max(0, int(float(entry["deadline_epoch"]) - wall_clock())))
        if remaining <= 0:
            raise ValueError("reconciliation authenticity deadline expired")
        cached_terminal = cached.pop(entry["attempt_id"], None)
        if cached_terminal is None:
            discovered = executor.control_remote(
                "remote-commit", query, operation_timeout=min(60, remaining),
            )
            if type(discovered) is not ControlResult or discovered.returncode != 0:
                raise ValueError("authoritative remote commit is unavailable")
            terminal = _validated_commit_terminal(discovered.terminal, query)
            envelope_digest = discovered.digest
        else:
            terminal = _validated_commit_terminal(cached_terminal, query)
            envelope_digest = terminal["result_digest"]
        request = terminal["request"]
        if (envelope_digest != terminal["result_digest"]
                or accepted["remote_record_digest"] != terminal["result_digest"]
                or request["pending_note_id"] != entry["note_id"]):
            raise ValueError("reconciliation record is not bound to authoritative remote commit")
        commit_terminals[entry["attempt_id"]] = terminal
    if cached:
        raise ValueError("cached reconciliation terminals are not bound to journal attempts")
    final = journal.get("final")
    if final is not None:
        verification_digest = _bounded_local_evidence_digest(
            final["evidence_file"], required=not allow_missing_final_evidence,
        )
        if (verification_digest is not None
                and verification_digest != final["record"]["verification_digest"]):
            raise ValueError("final verification digest is not bound to local bytes")
        accepted_digest = hashlib.sha256(
            _canonical_json_bytes(journal["attempts"][-1]["accepted_record"])
        ).hexdigest()
        if final["record"]["accepted_record_digest"] != accepted_digest:
            raise ValueError("final verification is not bound to accepted authority")
    return commit_terminals


def _synchronize_reconciliation_authority(
    arguments: argparse.Namespace, executor: PinnedExecutor,
    authority: ActiveTransactionAuthority, journal: dict[str, object],
    *, wall_clock: Callable[[], float],
    commit_terminals: dict[str, dict[str, object]] | None = None,
    allow_missing_final_evidence: bool = False,
) -> ActiveTransactionAuthority:
    """Converge authenticated local completion with the exact remote state graph."""
    _validate_reconciliation(
        journal, identity=authority.identity.transaction_identity,
        boundary=arguments.boundary,
        attempt_ids=tuple(item["attempt_id"] for item in authority.record["attempts"]),
        stage=arguments.stage, bundle=arguments.bundle,
    )
    terminals = _validate_reconciliation_authenticity(
        journal, arguments, executor, authority.record["attempts"],
        wall_clock=wall_clock, cached_commit_terminals=commit_terminals,
        allow_missing_final_evidence=allow_missing_final_evidence,
    )
    for index, entry in enumerate(journal["attempts"]):
        record = authority.record
        remote = record["attempts"][index]
        if (remote["attempt_id"] != entry["attempt_id"]
                or (remote["phase"], remote["operation"]) != (
                    entry["phase"], entry["operation"],
                )):
            raise ValueError("local reconciliation progress mismatches remote topology")
        current = record["current_phase_index"]
        if current < index:
            raise ValueError("local reconciliation progress is ahead of remote authority")
        rollback = entry["phase"] == "rollback-proof"
        completion = "ROLLBACK_VERIFIED" if rollback else "ACCEPTED"
        if current > index:
            if remote["state"] != completion or entry["state"] != "ACCEPTED":
                raise ValueError("advanced remote phase mismatches local completion")
            continue
        state = remote["state"]
        if rollback:
            recovery = remote.get("recovery")
            if state == "SEALED_RECOVERING":
                # A journal digest is not a recovery terminal.  Fresh observer-loss
                # discovery is deliberately outside this known-authority tracer.
                raise ValueError("sealed recovery has no authoritative terminal result")
            if (state not in {"RECOVERED_UNVERIFIED", "ROLLBACK_VERIFIED"}
                    or type(recovery) is not dict
                    or recovery.get("result_digest") != entry["accepted_record"]["remote_record_digest"]):
                raise ValueError("rollback reconciliation mismatches remote recovery authority")
            if state == "RECOVERED_UNVERIFIED":
                if entry["state"] != "ACCEPTED":
                    raise ValueError("remote rollback cannot complete before local publication")
                authority = _active_transition(
                    arguments, executor, authority, entry["phase"],
                    "RECOVERED_UNVERIFIED", "ROLLBACK_VERIFIED", wall_clock,
                )
        else:
            terminal = terminals.get(entry["attempt_id"])
            if terminal is None:
                raise ValueError("commit reconciliation lacks an authoritative terminal")
            digest = terminal["result_digest"]
            if state == "SEALED_COMMITTING":
                authority = _active_terminal(
                    arguments, executor, authority, entry["phase"], "commit",
                    "SEALED_COMMITTING", "COMMITTED_UNRECONCILED", digest, wall_clock,
                )
                state = "COMMITTED_UNRECONCILED"
            commit = authority.record["attempts"][index].get("commit")
            if (state not in {"COMMITTED_UNRECONCILED", "ACCEPTED"}
                    or type(commit) is not dict or commit.get("result_digest") != digest):
                raise ValueError("commit reconciliation mismatches remote terminal authority")
            if state == "COMMITTED_UNRECONCILED" and entry["state"] == "ACCEPTED":
                authority = _active_transition(
                    arguments, executor, authority, entry["phase"],
                    "COMMITTED_UNRECONCILED", "ACCEPTED", wall_clock,
                )
            elif state == "ACCEPTED" and entry["state"] != "ACCEPTED":
                raise ValueError("remote acceptance is ahead of local publication")
        record = authority.record
        remote = record["attempts"][index]
        if remote["state"] == completion and entry["state"] == "ACCEPTED":
            already_advanced = (
                index < record["current_phase_index"]
                or index == len(_TRANSACTION_TOPOLOGY) - 1
                and record["state"] == "THREE_ATTEMPTS_ACCEPTED"
                and type(record.get("last_advance")) is dict
                and record["last_advance"].get("phase") == entry["phase"]
            )
            if not already_advanced:
                authority = _active_advance(
                    arguments, executor, authority, entry["phase"], wall_clock,
                )
    return authority


def _bounded_reconcile_attempt(notes: object, entry: dict[str, object], journal: dict[str, object],
                               journal_path: Path, clock: Callable[[], float],
                               wall_clock: Callable[[], float] = time.time,
                               authenticator: Callable[[], object] | None = None) -> bool:
    _validate_reconciliation(journal)
    matches = [index for index, candidate in enumerate(journal["attempts"])
               if type(candidate) is dict and candidate.get("attempt_id") == entry.get("attempt_id")]
    if len(matches) != 1 or journal["attempts"][matches[0]] != entry:
        raise ValueError("reconciliation attempt is not bound to its journal")
    entry_index = matches[0]
    remaining, deadline_epoch = entry.get("remaining_seconds"), entry.get("deadline_epoch")
    if type(remaining) is not int or remaining <= 0 or type(deadline_epoch) not in {int, float}:
        return False
    remaining = min(remaining, max(0, int(float(deadline_epoch) - wall_clock())))
    if remaining <= 0:
        entry["remaining_seconds"] = 0
        _write_reconciliation(journal_path, journal)
        return False
    deadline = clock() + remaining
    for _ in range(3):
        before = clock()
        try:
            validated = _validate_reconciliation(journal)
            if authenticator is not None:
                authenticator()
            published = validated["attempts"][entry_index]
            notes.reconcile(str(published["note_id"]), "ACCEPTED", published["accepted_record"])
        except _RECONCILIATION_ERRORS:
            elapsed = max(1, int(clock() - before))
            entry["remaining_seconds"] = max(0, min(int(entry["remaining_seconds"]),
                                                      int(deadline - clock()),
                                                      int(float(deadline_epoch) - wall_clock())) - elapsed)
            _write_reconciliation(journal_path, journal)
            if entry["remaining_seconds"] <= 0:
                return False
            continue
        entry["state"] = "ACCEPTED"
        entry["remaining_seconds"] = max(1, min(int(entry["remaining_seconds"]),
                                                  max(1, int(deadline - clock())),
                                                  max(1, int(float(deadline_epoch) - wall_clock()))))
        _write_reconciliation(journal_path, journal)
        _validate_reconciliation(journal)
        return True
    return False


def _bounded_publish_final(notes: object, final: dict[str, object], journal: dict[str, object],
                           journal_path: Path, clock: Callable[[], float],
                           wall_clock: Callable[[], float] = time.time,
                           authenticator: Callable[[], object] | None = None) -> bool:
    _validate_reconciliation(journal)
    if journal.get("final") != final:
        raise ValueError("reconciliation final entry is not bound to its journal")
    remaining, deadline_epoch = final.get("remaining_seconds"), final.get("deadline_epoch")
    if type(remaining) is not int or remaining <= 0 or type(deadline_epoch) not in {int, float}:
        return False
    remaining = min(remaining, max(0, int(float(deadline_epoch) - wall_clock())))
    if remaining <= 0:
        final["remaining_seconds"] = 0
        _write_reconciliation(journal_path, journal)
        return False
    deadline = clock() + remaining
    discover_final = getattr(notes, "discover_final", None)
    if not callable(discover_final):
        raise ValueError("exact external final discovery is unavailable")
    for _ in range(3):
        before = clock()
        validated = _validate_reconciliation(journal)
        if authenticator is not None:
            authenticator()
        record = validated["final"]["record"]
        existing = discover_final(record)
        if existing is None:
            try:
                notes.final(record)
            except _RECONCILIATION_ERRORS:
                existing = discover_final(record)
                if existing is None:
                    elapsed = max(1, int(clock() - before))
                    final["remaining_seconds"] = max(0, min(int(final["remaining_seconds"]),
                                                             int(deadline - clock()),
                                                             int(float(deadline_epoch) - wall_clock())) - elapsed)
                    _write_reconciliation(journal_path, journal)
                    if final["remaining_seconds"] <= 0:
                        return False
                    continue
        final["state"] = "AUTHORITATIVE_FINAL"
        final["remaining_seconds"] = max(1, min(int(final["remaining_seconds"]),
                                                 max(1, int(deadline - clock())),
                                                 max(1, int(float(deadline_epoch) - wall_clock()))))
        _write_reconciliation(journal_path, journal)
        _validate_reconciliation(journal)
        return True
    return False



def _validated_recovery_terminal(value: object, expected_request: dict[str, object]) -> dict[str, object]:
    fields = frozenset(("schema", "request", "request_digest", "result_digest", "recovered_epoch"))
    recovered_epoch = value.get("recovered_epoch") if type(value) is dict else None
    if (type(value) is not dict or frozenset(value) != fields or not _is_schema_v1(value.get("schema"))
            or value.get("request") != expected_request
            or value.get("request_digest") != canonical_recovery_request_digest(expected_request)
            or not _valid_digest(value.get("result_digest"))
            or type(recovered_epoch) not in {int, float}
            or not math.isfinite(recovered_epoch) or recovered_epoch <= 0):
        raise ValueError("authoritative remote recovery terminal is invalid")
    return json.loads(_canonical_json_bytes(value).decode("ascii"))

_EXTERNAL_PUBLICATION_FIELDS = frozenset(("note_id", "state", "record"))


def _reconstruct_reconciliation_from_remote(
    arguments: argparse.Namespace, notes: object, executor: PinnedExecutor,
    runner: Callable[[argparse.Namespace, int], int], authority: ActiveTransactionAuthority,
    *, wall_clock: Callable[[], float],
) -> dict[str, object]:
    """Rebuild the observer journal solely from canonical remote and note authorities."""
    discover = getattr(notes, "discover", None)
    if not callable(discover):
        raise ValueError("external reconciliation authority is unavailable")
    record = authority.record
    attempts = record["attempts"]
    attempt_ids = tuple(item["attempt_id"] for item in attempts)
    publications = discover(
        transaction_identity=authority.identity.transaction_identity,
        transaction_id=record["transaction_id"], boundary=arguments.boundary,
        attempt_ids=attempt_ids,
    )
    if type(publications) is not list:
        raise ValueError("external reconciliation authority is malformed")
    by_attempt: dict[str, dict[str, object]] = {}
    external_final: dict[str, object] | None = None
    for publication in publications:
        if (type(publication) is not dict
                or frozenset(publication) != _EXTERNAL_PUBLICATION_FIELDS
                or type(publication.get("note_id")) is not str
                or not publication["note_id"].isdigit()
                or int(publication["note_id"]) <= 0
                or publication.get("state") not in {"PENDING", "ACCEPTED", "ROLLED_BACK", "FINAL"}
                or type(publication.get("record")) is not dict):
            raise ValueError("external reconciliation publication is invalid")
        kind = "FINAL" if publication["state"] == "FINAL" else "ATTEMPT"
        document = _validate_external_publication_document(
            {"schema": 1, "kind": kind, "state": publication["state"],
             "record": publication["record"]},
            transaction_identity=authority.identity.transaction_identity,
            transaction_id=record["transaction_id"], boundary=arguments.boundary,
            attempt_ids=attempt_ids,
        )
        item = document["record"]
        if kind == "FINAL":
            if external_final is not None:
                raise ValueError("external reconciliation final is ambiguous")
            external_final = {"note_id": publication["note_id"], "record": item}
            continue
        attempt_id = item["attempt_id"]
        if attempt_id in by_attempt:
            raise ValueError("external reconciliation publication is ambiguous")
        if document["state"] != "PENDING" and item["note_id"] != publication["note_id"]:
            raise ValueError("external reconciliation publication note binding is invalid")
        by_attempt[str(attempt_id)] = {"note_id": publication["note_id"],
            "state": document["state"], "record": item}

    journal = {"schema": 1, "identity": authority.identity.transaction_identity,
               "boundary": arguments.boundary, "attempts": [], "final": None}
    now = wall_clock()
    for index, remote in enumerate(attempts):
        state = remote["state"]
        completion = "ROLLBACK_VERIFIED" if index == 1 else "ACCEPTED"
        resumable_commit = index != 1 and state in {"SEALED_COMMITTING", "COMMITTED_UNRECONCILED"}
        resumable_recovery = index == 1 and state in {"RECOVERED_UNVERIFIED", "ROLLBACK_VERIFIED"}
        if state != completion and not resumable_commit and not resumable_recovery:
            break
        publication = by_attempt.get(remote["attempt_id"])
        expected_states = ({"PENDING", "ACCEPTED"} if resumable_commit else
                           ({"PENDING", "ROLLED_BACK"} if resumable_recovery
                            else ({"ROLLED_BACK"} if index == 1 else {"ACCEPTED"})))
        if publication is None or publication["state"] not in expected_states:
            raise ValueError("remote attempt lacks its exact external publication")
        external = publication["record"]
        base = {"boundary", "phase", "attempt_id", "stage", "bundle", "evidence_digest"}
        if (not base.issubset(external)
                or external.get("boundary") != arguments.boundary
                or external.get("phase") != remote["phase"]
                or external.get("attempt_id") != remote["attempt_id"]
                or external.get("stage") != arguments.stage
                or external.get("bundle") != arguments.bundle
                or not _valid_digest(external.get("evidence_digest"))):
            raise ValueError("external publication is not bound to the release transaction")
        result_digest = external.get("remote_record_digest")
        if resumable_recovery:
            recovery_request = _full_recovery_request(
                arguments, remote["phase"], remote["attempt_id"], remote["registry"],
            )
            recovery_request["evidence_digest"] = external["evidence_digest"]
            recovery_request["pending_note_id"] = publication["note_id"]
            query = {**recovery_request, "query": True}
            discovered = executor.control_remote("remote-recover", query, operation_timeout=60)
            if type(discovered) is not ControlResult or discovered.returncode != 0:
                raise ValueError("recovered attempt lacks authoritative recovery terminal")
            recovery_terminal = _validated_recovery_terminal(discovered.terminal, recovery_request)
            if discovered.digest != recovery_terminal["result_digest"]:
                raise ValueError("recovery terminal envelope is invalid")
            result_digest = recovery_terminal["result_digest"]
            owned = remote.get("recovery")
            if type(owned) is not dict or owned.get("result_digest") != result_digest:
                raise ValueError("recovery terminal is not bound to transaction authority")
            deadline_epoch = min(float(record["original_deadline_epoch"]), now + 60.0)
            if publication["state"] == "PENDING":
                recovery_evidence = Path(arguments.evidence_dir) / f"{remote['attempt_id']}-recovery.json"
                verify_arguments = argparse.Namespace(
                    **vars(arguments), phase=remote["phase"], mode="rollback",
                    attempt_id=remote["attempt_id"], accepted_sha=arguments.previous_sha,
                    accepted_version=arguments.previous_version, evidence_file=str(recovery_evidence),
                    acceptance_operation_id=_canonical_acceptance_operation_id(
                        remote["attempt_id"], remote["phase"], remote["operation"], index,
                        "recovery-verification",
                    ),
                    attempt_registry=remote["registry"],
                )
                if runner(verify_arguments, 60) != 0 or not recovery_evidence.is_file():
                    raise ValueError("recovered rollback verification is unavailable")
                verification_digest = hashlib.sha256(recovery_evidence.read_bytes()).hexdigest()
                notes.reconcile(publication["note_id"], "ROLLED_BACK", {
                    **{key: external[key] for key in (
                        "boundary", "phase", "attempt_id", "stage", "bundle", "evidence_digest")},
                    "remote_record_digest": result_digest, "recovery_digest": result_digest,
                    "verification_digest": verification_digest,
                })
                external = {**external, "note_id": publication["note_id"],
                            "terminal_kind": "recovery", "remote_record_digest": result_digest,
                            "verification_digest": verification_digest}
                publication["state"] = "ROLLED_BACK"
        elif resumable_commit:
            operation_id = hashlib.sha256(
                f"{remote['attempt_id']}\0commit".encode("utf-8")
            ).hexdigest()[:32]
            query = {"schema": 1, "query": True, "boundary": arguments.boundary,
                     "phase": remote["phase"], "attempt_id": remote["attempt_id"],
                     "operation_id": operation_id, "registry": remote["registry"],
                     "evidence_digest": external["evidence_digest"],
                     "success_record": f"/run/matrix-priority-1-3/{remote['attempt_id']}/success.json"}
            discovered = executor.control_remote("remote-commit", query, operation_timeout=60)
            if type(discovered) is not ControlResult or discovered.returncode != 0:
                raise ValueError("sealed commit has no authoritative terminal")
            terminal_record = _validated_commit_terminal(discovered.terminal, query)
            if discovered.digest != terminal_record["result_digest"]:
                raise ValueError("sealed commit terminal envelope is invalid")
            result_digest = terminal_record["result_digest"]
            if (publication["state"] == "ACCEPTED"
                    and external.get("remote_record_digest") != result_digest):
                raise ValueError("accepted publication mismatches sealed commit terminal")
            deadline_epoch = min(float(record["original_deadline_epoch"]),
                                 float(terminal_record["committed_epoch"]) + 60.0)
        else:
            terminal = remote["recovery"] if index == 1 else remote["commit"]
            if (not _valid_digest(result_digest) or type(terminal) is not dict
                    or terminal.get("result_digest") != result_digest):
                raise ValueError("external publication is not bound to the sealed terminal")
            deadline_epoch = min(float(record["original_deadline_epoch"]), now + 60.0)
        if deadline_epoch <= now:
            raise ValueError("reconstructed reconciliation lease expired")
        accepted = {key: external[key] for key in base}
        accepted["remote_record_digest"] = result_digest
        journal["attempts"].append({
            "phase": remote["phase"], "operation": remote["operation"],
            "attempt_id": remote["attempt_id"], "note_id": publication["note_id"],
            "accepted_record": accepted,
            "evidence_file": str(Path(arguments.evidence_dir) / f"{remote['attempt_id']}-candidate.json"),
            "state": "ACCEPTED" if publication["state"] in {"ACCEPTED", "ROLLED_BACK"}
                     else "COMMITTED_UNRECONCILED",
            "remaining_seconds": max(1, min(60, int(deadline_epoch - now))),
            "deadline_epoch": deadline_epoch,
        })
    if external_final is not None:
        if record["state"] != "THREE_ATTEMPTS_ACCEPTED" or len(journal["attempts"]) != len(_TRANSACTION_TOPOLOGY):
            raise ValueError("external final precedes complete remote authority")
        item = external_final["record"]
        accepted = journal["attempts"][-1]["accepted_record"]
        accepted_digest = hashlib.sha256(_canonical_json_bytes(accepted)).hexdigest()
        if (item["note_id"] != journal["attempts"][-1]["note_id"]
                or item["evidence_digest"] != accepted["evidence_digest"]
                or item["remote_record_digest"] != accepted["remote_record_digest"]
                or item["accepted_record_digest"] != accepted_digest):
            raise ValueError("external final is not bound to the accepted transaction")
        deadline_epoch = min(float(record["original_deadline_epoch"]), now + 60.0)
        if deadline_epoch <= now:
            raise ValueError("reconstructed final authority lease expired")
        journal["final"] = {
            "state": "AUTHORITATIVE_FINAL",
            "record": {
                "boundary": item["boundary"], "attempt_id": item["attempt_id"],
                "note_id": item["note_id"], "candidate_evidence_digest": item["evidence_digest"],
                "remote_record_digest": item["remote_record_digest"],
                "accepted_record_digest": item["accepted_record_digest"],
                "verification_digest": item["verification_digest"],
            },
            "evidence_file": str(Path(arguments.evidence_dir) / f"{item['attempt_id']}-final-verification.json"),
            "remaining_seconds": max(1, min(60, int(deadline_epoch - now))),
            "deadline_epoch": deadline_epoch,
        }
    progressed = record["current_phase_index"] + (record["state"] == "THREE_ATTEMPTS_ACCEPTED")
    if len(journal["attempts"]) < int(progressed):
        raise ValueError("remote progress cannot be reconstructed from external authority")
    current = record["attempts"][record["current_phase_index"]]
    if (record["state"] != "THREE_ATTEMPTS_ACCEPTED"
            and len(journal["attempts"]) == record["current_phase_index"]
            and current["state"] != "READY"):
        raise ValueError("ambiguous in-flight remote state is not replay-admissible")
    return _validate_reconciliation(
        journal, identity=authority.identity.transaction_identity,
        boundary=arguments.boundary, attempt_ids=attempt_ids,
        stage=arguments.stage, bundle=arguments.bundle,
    )


def run_release(
    arguments: argparse.Namespace, *, executor: PinnedExecutor | None = None,
    acceptance_runner: Callable[[argparse.Namespace, int], int] | None = None,
    evidence_store: object | None = None, clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time, attempt_ids: Iterator[str] | None = None,
) -> int:
    """Execute exactly initial deploy, rollback proof, and final deploy."""
    if (arguments.attempt_budget_seconds, arguments.commit_reserve_seconds,
            arguments.watchdog_seconds) != (6600, 60, 7200):
        return 2
    try:
        for name in ("stage", "bundle", "destination_root", "evidence_dir"):
            _absolute(str(getattr(arguments, name)), name)
        if arguments.initial_service_state == "active":
            if (arguments.health_url, arguments.health_status, arguments.health_timeout_seconds) != (
                    "http://127.0.0.1:5000/", 302, 30):
                return 2
        elif any(value is not None for value in (
                arguments.health_url, arguments.health_status, arguments.health_timeout_seconds)):
            return 2
        if executor is None:
            if __package__ in {None, ""}:
                sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from deploy.verify_priority_1_3_acceptance import PinnedRemoteExecutor
            executor = PinnedRemoteExecutor(
                arguments.ssh_target, arguments.ssh_identity_file,
                arguments.ssh_known_hosts_file,
            )
        runner = acceptance_runner or _production_acceptance_runner
        notes = evidence_store or GitLabEvidenceStore(
            arguments.gitlab_project, arguments.gitlab_mr, arguments.gitlab_token_file
        )
        topology = _TRANSACTION_TOPOLOGY
        journal_path = Path(arguments.evidence_dir) / _RECONCILIATION_FILE
        coordinator_epoch = int(wall_clock())
        canonical_identity, transaction = _prepare_active_transaction(
            arguments, executor, created_epoch=coordinator_epoch,
        )
        authority = _claim_active_transaction(
            arguments, executor, canonical_identity, transaction,
            coordinator_epoch=coordinator_epoch,
        )
        transaction = authority.record
        identity = canonical_identity.transaction_identity
        bind_publication = getattr(notes, "bind_publication_authority", None)
        if callable(bind_publication):
            bind_publication(canonical_identity, transaction, boundary=arguments.boundary,
                             stage=arguments.stage, bundle=arguments.bundle)
        remote_attempts = transaction["attempts"]
        authoritative_attempt_ids = tuple(attempt["attempt_id"] for attempt in remote_attempts)
        del attempt_ids  # retained only for call-signature compatibility; never identity authority
        if journal_path.exists():
            journal = _load_reconciliation(journal_path, identity)
        else:
            if getattr(arguments, "resume", False):
                journal = _reconstruct_reconciliation_from_remote(
                    arguments, notes, executor, runner, authority, wall_clock=wall_clock,
                )
                _write_reconciliation(journal_path, journal)
            else:
                journal = {"schema": 1, "identity": identity, "boundary": arguments.boundary,
                           "attempts": [], "final": None}
        journal = _validate_reconciliation(
            journal, identity=identity, boundary=arguments.boundary,
            attempt_ids=authoritative_attempt_ids, stage=arguments.stage,
            bundle=arguments.bundle,
        )
        attempts = journal["attempts"]
        allow_missing_final_evidence = False
        final_for_authentication = journal.get("final")
        if (type(final_for_authentication) is dict
                and final_for_authentication.get("state") == "AUTHORITATIVE_FINAL"
                and not Path(final_for_authentication["evidence_file"]).is_file()):
            discover = getattr(notes, "discover", None)
            if not callable(discover):
                raise ValueError("external final authority is unavailable")
            final_publications = [item for item in discover(
                transaction_identity=canonical_identity.transaction_identity,
                transaction_id=transaction["transaction_id"], boundary=arguments.boundary,
                attempt_ids=authoritative_attempt_ids,
            ) if type(item) is dict and item.get("state") == "FINAL"]
            if len(final_publications) != 1:
                raise ValueError("external final authority is absent or ambiguous")
            external = final_publications[0].get("record")
            local = final_for_authentication["record"]
            if (type(external) is not dict or any((
                    external.get("boundary") != local["boundary"],
                    external.get("attempt_id") != local["attempt_id"],
                    external.get("note_id") != local["note_id"],
                    external.get("evidence_digest") != local["candidate_evidence_digest"],
                    external.get("remote_record_digest") != local["remote_record_digest"],
                    external.get("accepted_record_digest") != local["accepted_record_digest"],
                    external.get("verification_digest") != local["verification_digest"],
            ))):
                raise ValueError("external final authority conflicts with reconciliation")
            allow_missing_final_evidence = True
        def authenticate_reconciliation() -> dict[str, dict[str, object]]:
            return _validate_reconciliation_authenticity(
                journal, arguments, executor, remote_attempts, wall_clock=wall_clock,
                allow_missing_final_evidence=allow_missing_final_evidence,
            )
        authenticated_commit_terminals = authenticate_reconciliation()
        authority = _synchronize_reconciliation_authority(
            arguments, executor, authority, journal, wall_clock=wall_clock,
            commit_terminals=authenticated_commit_terminals,
            allow_missing_final_evidence=allow_missing_final_evidence,
        )
        remote_attempts = authority.record["attempts"]
        if len(attempts) > len(topology):
            return 2
        for index, entry in enumerate(attempts):
            if (type(entry) is not dict or (entry.get("phase"), entry.get("operation")) != topology[index]
                    or entry.get("state") not in {"COMMITTED_UNRECONCILED", "ACCEPTED"}):
                return 2
            if entry["state"] == "COMMITTED_UNRECONCILED":
                if not _bounded_reconcile_attempt(
                        notes, entry, journal, journal_path, clock, wall_clock,
                        authenticator=authenticate_reconciliation):
                    return 2
                authority = _synchronize_reconciliation_authority(
                    arguments, executor, authority, journal, wall_clock=wall_clock,
                    commit_terminals=authenticated_commit_terminals,
                )
                remote_attempts = authority.record["attempts"]
        if journal["final"] is not None:
            final = journal["final"]
            if final.get("state") == "AUTHORITATIVE_FINAL":
                _validate_reconciliation(
                    journal, identity=identity, boundary=arguments.boundary,
                    attempt_ids=authoritative_attempt_ids, stage=arguments.stage,
                    bundle=arguments.bundle,
                )
                authenticate_reconciliation()
                return 0
            if final.get("state") != "FINAL_UNRECONCILED":
                return 2
            if not _bounded_publish_final(
                    notes, final, journal, journal_path, clock, wall_clock,
                    authenticator=authenticate_reconciliation):
                return 2
            _validate_reconciliation(
                journal, identity=identity, boundary=arguments.boundary,
                attempt_ids=authoritative_attempt_ids, stage=arguments.stage,
                bundle=arguments.bundle,
            )
            authenticate_reconciliation()
            return 0

        for phase_index, (phase, operation) in enumerate(topology[len(attempts):], start=len(attempts)):
            remote_attempt = remote_attempts[phase_index]
            attempt_id = remote_attempt["attempt_id"]
            attempt_registry = remote_attempt["registry"]
            if _ID_RE.fullmatch(attempt_id) is None:
                return 2
            started = clock()
            work_deadline = started + arguments.attempt_budget_seconds - arguments.commit_reserve_seconds
            attempt_deadline = started + arguments.attempt_budget_seconds
            watchdog_deadline = started + arguments.watchdog_seconds
            if not work_deadline < attempt_deadline < watchdog_deadline:
                return 2

            # A prior process may have lost control after the remote terminal write but
            # before its first local journal write.  Candidate evidence is only a
            # discovery trigger: the remote typed terminal and exact idempotent request
            # replay are authoritative.
            evidence_path = Path(arguments.evidence_dir) / f"{attempt_id}-candidate.json"
            if evidence_path.is_file():
                evidence_digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
                commit_operation = hashlib.sha256(f"{attempt_id}\0commit".encode("utf-8")).hexdigest()[:32]
                query = {
                    "schema": 1, "query": True, "boundary": arguments.boundary, "phase": phase,
                    "attempt_id": attempt_id, "operation_id": commit_operation,
                    "registry": attempt_registry,
                    "evidence_digest": evidence_digest,
                    "success_record": f"/run/matrix-priority-1-3/{attempt_id}/success.json",
                }
                discovered = executor.control_remote(
                    "remote-commit", query,
                    operation_timeout=_remaining_integer(attempt_deadline, clock, 60),
                )
                if type(discovered) is ControlResult and discovered.returncode == 0:
                    terminal = _validated_commit_terminal(discovered.terminal, query)
                    if discovered.digest != terminal["result_digest"]:
                        return 2
                    commit_request = dict(terminal["request"])
                    replayed = executor.control_remote(
                        "remote-commit", commit_request,
                        operation_timeout=_remaining_integer(attempt_deadline, clock, 60),
                    )
                    if (type(replayed) is not ControlResult or replayed.returncode != 0
                            or replayed.digest != terminal["result_digest"]):
                        return 2
                    if replayed.terminal is not None:
                        replay_terminal = _validated_commit_terminal(replayed.terminal)
                        if replay_terminal != terminal:
                            return 2
                    pending_record = {
                        "boundary": arguments.boundary, "phase": phase, "attempt_id": attempt_id,
                        "stage": arguments.stage, "bundle": arguments.bundle,
                        "evidence_digest": evidence_digest,
                    }
                    authenticated_commit_terminals[attempt_id] = terminal
                    accepted = {**pending_record, "remote_record_digest": terminal["result_digest"]}
                    deadline_epoch = float(terminal["committed_epoch"]) + 60.0
                    entry = {
                        "phase": phase, "operation": operation, "attempt_id": attempt_id,
                        "note_id": commit_request["pending_note_id"], "accepted_record": accepted,
                        "evidence_file": str(evidence_path), "state": "COMMITTED_UNRECONCILED",
                        "remaining_seconds": max(0, min(60, int(deadline_epoch - wall_clock()))),
                        "deadline_epoch": deadline_epoch,
                    }
                    attempts.append(entry)
                    _write_reconciliation(journal_path, journal)
                    authority = _synchronize_reconciliation_authority(
                        arguments, executor, authority, journal, wall_clock=wall_clock,
                        commit_terminals=authenticated_commit_terminals,
                    )
                    remote_attempts = authority.record["attempts"]
                    if not _bounded_reconcile_attempt(
                            notes, entry, journal, journal_path, clock, wall_clock,
                            authenticator=authenticate_reconciliation):
                        return 2
                    authority = _synchronize_reconciliation_authority(
                        arguments, executor, authority, journal, wall_clock=wall_clock,
                        commit_terminals=authenticated_commit_terminals,
                    )
                    remote_attempts = authority.record["attempts"]
                    continue
                if type(discovered) is not ControlResult or discovered.returncode != 3:
                    return 2

            recovery_request = _full_recovery_request(arguments, phase, attempt_id, attempt_registry)
            recovery_payload = encode_privileged_request("remote-recover", recovery_request)
            watchdog_command = [
                "/usr/bin/systemd-run", "--unit", recovery_request["guard_unit"],
                "--on-active", f"{arguments.watchdog_seconds}s", "--timer-property=AccuracySec=1s",
                "--property=Type=exec", "--property=Restart=on-failure", "--",
                *watchdog_recovery_execstart(arguments.stage, recovery_payload),
            ]
            authority = _active_transition(
                arguments, executor, authority, phase, "READY", "GUARD_ARMING", wall_clock,
            )
            guard = executor.run_remote(
                attempt_id, "watchdog-arm", watchdog_command, registry=attempt_registry,
                run_as="root", operation_timeout=_remaining_integer(work_deadline, clock, 10),
            )
            if type(guard) is not OperationResult or guard.returncode != 0:
                return 2
            authority = _active_transition(
                arguments, executor, authority, phase, "GUARD_ARMING", "GUARDED", wall_clock,
            )

            note_id: str | None = None
            committed_authoritatively = False
            def recover_and_verify() -> bool:
                nonlocal authority
                if committed_authoritatively:
                    return False
                current = authority.record["attempts"][authority.record["current_phase_index"]]["state"]
                if current != "SEALED_RECOVERING":
                    if "SEALED_RECOVERING" not in _ATTEMPT_TRANSITIONS.get(current, frozenset()):
                        return False
                    authority = _active_transition(
                        arguments, executor, authority, phase, current, "SEALED_RECOVERING", wall_clock,
                    )
                if evidence_path.is_file():
                    recovery_request["evidence_digest"] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
                if note_id is not None:
                    recovery_request["pending_note_id"] = note_id
                recovered = executor.control_remote(
                    "remote-recover", recovery_request,
                    operation_timeout=_remaining_integer(attempt_deadline, clock, 300),
                )
                if (type(recovered) is not ControlResult or recovered.returncode != 0
                        or type(recovered.digest) is not str
                        or _DIGEST_RE.fullmatch(recovered.digest) is None
                        or recovered.terminal is not None):
                    return False
                authority = _active_terminal(
                    arguments, executor, authority, phase, "recovery", "SEALED_RECOVERING",
                    "RECOVERED_UNVERIFIED", recovered.digest, wall_clock,
                )
                recovery_evidence = Path(arguments.evidence_dir) / f"{attempt_id}-recovery.json"
                verify_arguments = argparse.Namespace(
                    **vars(arguments), phase=phase, mode="rollback", attempt_id=attempt_id,
                    accepted_sha=arguments.previous_sha, accepted_version=arguments.previous_version,
                    evidence_file=str(recovery_evidence),
                    acceptance_operation_id=_canonical_acceptance_operation_id(
                        attempt_id, phase, operation, phase_index, "recovery-verification",
                    ),
                    attempt_registry=attempt_registry,
                )
                verification_deadline = _post_mutation_verification_deadline(clock)
                verified = _invoke_funded_acceptance(runner, verify_arguments, verification_deadline, clock)
                if verified != 0 or not recovery_evidence.is_file():
                    return False
                authority = _active_transition(
                    arguments, executor, authority, phase, "RECOVERED_UNVERIFIED",
                    "ROLLBACK_VERIFIED", wall_clock,
                )
                if note_id is not None:
                    notes.reconcile(note_id, "ROLLED_BACK", {
                        "boundary": arguments.boundary, "phase": phase, "attempt_id": attempt_id,
                        "stage": arguments.stage, "bundle": arguments.bundle,
                        "evidence_digest": recovery_request["evidence_digest"],
                        "remote_record_digest": recovered.digest, "recovery_digest": recovered.digest,
                        "verification_digest": hashlib.sha256(recovery_evidence.read_bytes()).hexdigest(),
                    })
                return True

            authority = _active_transition(
                arguments, executor, authority, phase, "GUARDED", "MUTATION_RUNNING", wall_clock,
            )
            operation_result = executor.run_remote(
                attempt_id, operation, _release_operation_argv(arguments, operation),
                registry=attempt_registry, run_as="root",
                operation_timeout=_remaining_integer(work_deadline, clock, 6540),
            )
            if type(operation_result) is not OperationResult or operation_result.returncode != 0:
                recover_and_verify()
                return operation_result.returncode if type(operation_result) is OperationResult and operation_result.returncode else 2
            authority = _active_transition(
                arguments, executor, authority, phase, "MUTATION_RUNNING", "MUTATION_DONE", wall_clock,
            )
            mode = "rollback" if phase == "rollback-proof" else "deploy"
            accepted_sha = arguments.previous_sha if mode == "rollback" else arguments.expected_sha
            accepted_version = arguments.previous_version if mode == "rollback" else arguments.expected_version
            acceptance_arguments = argparse.Namespace(
                **vars(arguments), phase=phase, mode=mode, attempt_id=attempt_id,
                accepted_sha=accepted_sha, accepted_version=accepted_version, evidence_file=str(evidence_path),
                acceptance_operation_id=_canonical_acceptance_operation_id(
                    attempt_id, phase, operation, phase_index, "candidate",
                ),
                attempt_registry=attempt_registry,
            )
            authority = _active_transition(
                arguments, executor, authority, phase, "MUTATION_DONE", "ACCEPTANCE_RUNNING", wall_clock,
            )
            acceptance_result = _invoke_funded_acceptance(runner, acceptance_arguments, work_deadline, clock)
            if acceptance_result != 0 or not evidence_path.is_file() or clock() >= work_deadline:
                recover_and_verify()
                return acceptance_result if type(acceptance_result) is int and acceptance_result else 2
            evidence_digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
            authority = _active_transition(
                arguments, executor, authority, phase, "ACCEPTANCE_RUNNING", "CANDIDATE_DURABLE", wall_clock,
            )
            pending_record = {"boundary": arguments.boundary, "phase": phase, "attempt_id": attempt_id,
                              "stage": arguments.stage, "bundle": arguments.bundle,
                              "evidence_digest": evidence_digest}
            note_id = notes.pending(pending_record)
            authority = _active_transition(
                arguments, executor, authority, phase, "CANDIDATE_DURABLE", "PENDING_BOUND", wall_clock,
            )
            if phase == "rollback-proof":
                recovery_request["evidence_digest"] = evidence_digest
                recovery_request["pending_note_id"] = note_id
                authority = _active_transition(
                    arguments, executor, authority, phase, "PENDING_BOUND", "SEALED_RECOVERING", wall_clock,
                )
                recovered = executor.control_remote(
                    "remote-recover", recovery_request,
                    operation_timeout=_remaining_integer(attempt_deadline, clock, 300),
                )
                if (type(recovered) is not ControlResult or recovered.returncode != 0
                        or type(recovered.digest) is not str
                        or _DIGEST_RE.fullmatch(recovered.digest) is None
                        or recovered.terminal is not None):
                    return 2
                authority = _active_terminal(
                    arguments, executor, authority, phase, "recovery", "SEALED_RECOVERING",
                    "RECOVERED_UNVERIFIED", recovered.digest, wall_clock,
                )
                recovery_evidence = Path(arguments.evidence_dir) / f"{attempt_id}-recovery.json"
                verify_arguments = argparse.Namespace(
                    **vars(arguments), phase=phase, mode="rollback", attempt_id=attempt_id,
                    accepted_sha=arguments.previous_sha, accepted_version=arguments.previous_version,
                    evidence_file=str(recovery_evidence),
                    acceptance_operation_id=_canonical_acceptance_operation_id(
                        attempt_id, phase, operation, phase_index, "recovery-verification",
                    ),
                    attempt_registry=attempt_registry,
                )
                verification_deadline = _post_mutation_verification_deadline(clock)
                verified = _invoke_funded_acceptance(
                    runner, verify_arguments, verification_deadline, clock,
                )
                if verified != 0 or not recovery_evidence.is_file():
                    return 2
                authority = _active_transition(
                    arguments, executor, authority, phase, "RECOVERED_UNVERIFIED",
                    "ROLLBACK_VERIFIED", wall_clock,
                )
                accepted = {**pending_record, "remote_record_digest": recovered.digest}
                notes.reconcile(note_id, "ROLLED_BACK", {
                    **accepted, "recovery_digest": recovered.digest,
                    "verification_digest": hashlib.sha256(recovery_evidence.read_bytes()).hexdigest(),
                })
                entry = {
                    "phase": phase, "operation": operation, "attempt_id": attempt_id,
                    "note_id": note_id, "accepted_record": accepted,
                    "evidence_file": str(evidence_path), "state": "ACCEPTED",
                    "remaining_seconds": _remaining_integer(verification_deadline, clock, 60),
                }
                entry["deadline_epoch"] = wall_clock() + int(entry["remaining_seconds"])
                attempts.append(entry)
                _write_reconciliation(journal_path, journal)
                authority = _synchronize_reconciliation_authority(
                    arguments, executor, authority, journal, wall_clock=wall_clock,
                    commit_terminals=authenticated_commit_terminals,
                )
                remote_attempts = authority.record["attempts"]
                continue

            commit_operation = hashlib.sha256(f"{attempt_id}\0commit".encode("utf-8")).hexdigest()[:32]
            commit_request = {
                "schema": 1, "boundary": arguments.boundary, "phase": phase,
                "attempt_id": attempt_id, "operation_id": commit_operation,
                "registry": attempt_registry,
                "evidence_digest": evidence_digest, "pending_note_id": note_id,
                "guard_unit": recovery_request["guard_unit"],
                "success_record": f"/run/matrix-priority-1-3/{attempt_id}/success.json",
                "control_unit": f"matrix-p13-commit-{attempt_id}.service",
                "control_cgroup": f"/system.slice/matrix-p13-commit-{attempt_id}.service",
            }
            authority = _active_transition(
                arguments, executor, authority, phase, "PENDING_BOUND", "SEALED_COMMITTING", wall_clock,
            )
            committed = executor.control_remote(
                "remote-commit", commit_request,
                operation_timeout=_remaining_integer(attempt_deadline, clock, 60),
            )
            if type(committed) is not ControlResult or committed.returncode != 0:
                recover_and_verify()
                return 2
            terminal = _validated_commit_terminal(committed.terminal)
            if terminal["request"] != commit_request or committed.digest != terminal["result_digest"]:
                return 2
            authority = _active_terminal(
                arguments, executor, authority, phase, "commit", "SEALED_COMMITTING",
                "COMMITTED_UNRECONCILED", committed.digest, wall_clock,
            )
            committed_authoritatively = True
            authenticated_commit_terminals[attempt_id] = terminal
            accepted = {**pending_record, "remote_record_digest": committed.digest}
            entry = {"phase": phase, "operation": operation, "attempt_id": attempt_id,
                     "note_id": note_id, "accepted_record": accepted,
                     "evidence_file": str(evidence_path), "state": "COMMITTED_UNRECONCILED",
                     "remaining_seconds": _remaining_integer(attempt_deadline, clock, 60)}
            entry["deadline_epoch"] = wall_clock() + int(entry["remaining_seconds"])
            attempts.append(entry)
            _write_reconciliation(journal_path, journal)
            authority = _synchronize_reconciliation_authority(
                arguments, executor, authority, journal, wall_clock=wall_clock,
                commit_terminals=authenticated_commit_terminals,
            )
            remote_attempts = authority.record["attempts"]
            if not _bounded_reconcile_attempt(
                    notes, entry, journal, journal_path, clock, wall_clock,
                    authenticator=authenticate_reconciliation):
                return 2
            authority = _synchronize_reconciliation_authority(
                arguments, executor, authority, journal, wall_clock=wall_clock,
                commit_terminals=authenticated_commit_terminals,
            )
            remote_attempts = authority.record["attempts"]

        last = attempts[-1]
        final_deadline = _post_mutation_verification_deadline(clock)
        final_evidence = Path(arguments.evidence_dir) / f"{last['attempt_id']}-final-verification.json"
        final_arguments = argparse.Namespace(
            **vars(arguments), phase="final-deploy", mode="deploy", attempt_id=last["attempt_id"],
            accepted_sha=arguments.expected_sha, accepted_version=arguments.expected_version,
            evidence_file=str(final_evidence),
            acceptance_operation_id=_canonical_acceptance_operation_id(
                last["attempt_id"], "final-deploy", "deploy", 2, "final-verification",
            ),
            attempt_registry=remote_attempts[2]["registry"],
        )
        if _invoke_funded_acceptance(runner, final_arguments, final_deadline, clock) != 0 or not final_evidence.is_file():
            return 2
        final_record = {
            "boundary": arguments.boundary, "attempt_id": last["attempt_id"],
            "note_id": last["note_id"],
            "candidate_evidence_digest": last["accepted_record"]["evidence_digest"],
            "remote_record_digest": last["accepted_record"]["remote_record_digest"],
            "accepted_record_digest": hashlib.sha256(json.dumps(
                last["accepted_record"], sort_keys=True, separators=(",", ":")
            ).encode("utf-8")).hexdigest(),
            "verification_digest": hashlib.sha256(final_evidence.read_bytes()).hexdigest(),
        }
        final_remaining = _remaining_integer(final_deadline, clock, 60)
        final = {"state": "FINAL_UNRECONCILED", "record": final_record,
                 "evidence_file": str(final_evidence),
                 "remaining_seconds": final_remaining,
                 "deadline_epoch": wall_clock() + final_remaining}
        journal["final"] = final
        _write_reconciliation(journal_path, journal)
        if not _bounded_publish_final(
                notes, final, journal, journal_path, clock, wall_clock,
                authenticator=authenticate_reconciliation):
            return 2
        _validate_reconciliation(
            journal, identity=identity, boundary=arguments.boundary,
            attempt_ids=authoritative_attempt_ids, stage=arguments.stage,
            bundle=arguments.bundle,
        )
        authenticate_reconciliation()
        return 0
    except (OSError, ValueError, TypeError, StopIteration, TimeoutError, RuntimeError,
            subprocess.SubprocessError, json.JSONDecodeError):
        return 2

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Privileged guarded Priority 1.3 release helper")
    subparsers = parser.add_subparsers(dest="helper", required=True)
    for helper in sorted(_PRIVILEGED_HELPERS):
        command = subparsers.add_parser(helper)
        add_store_once_argument(command, "--request-b64", required=True)
    run = subparsers.add_parser("run", description="Run the guarded Priority 1.3 release transaction")
    add_store_once_argument(run, "--boundary", choices=("b1", "b2"), required=True)
    run.add_argument("--resume", action="store_true")
    add_store_once_argument(run, "--ssh-target", required=True)
    add_store_once_argument(run, "--ssh-identity-file", required=True)
    add_store_once_argument(run, "--ssh-known-hosts-file", required=True)
    add_store_once_argument(run, "--stage", required=True)
    add_store_once_argument(run, "--bundle", required=True)
    add_store_once_argument(run, "--destination-root", required=True)
    add_store_once_argument(run, "--service", required=True)
    add_store_once_argument(run, "--initial-service-state", choices=("active", "inactive"), required=True)
    add_store_once_argument(run, "--health-url")
    add_store_once_argument(run, "--health-status", type=int)
    add_store_once_argument(run, "--health-timeout-seconds", type=int)
    add_store_once_argument(run, "--attempt-budget-seconds", type=int, required=True)
    add_store_once_argument(run, "--commit-reserve-seconds", type=int, required=True)
    add_store_once_argument(run, "--watchdog-seconds", type=int, required=True)
    add_store_once_argument(run, "--expected-sha", required=True)
    add_store_once_argument(run, "--expected-version", required=True)
    add_store_once_argument(run, "--previous-sha", required=True)
    add_store_once_argument(run, "--previous-version", required=True)
    add_store_once_argument(run, "--transaction-version", type=int, default=1)
    add_store_once_argument(run, "--transaction-registry-root", default="/run/matrix-priority-1-3/transactions")
    add_store_once_argument(run, "--destination-identity", required=True)
    add_store_once_argument(run, "--release-stamp", required=True)
    add_store_once_argument(run, "--release-manifest-digest", required=True)
    add_store_once_argument(run, "--backup-manifest-digest", required=True)
    add_store_once_argument(run, "--release-spec-digest", required=True)
    add_store_once_argument(run, "--session-cookie-file", required=True)
    add_store_once_argument(run, "--evidence-dir", required=True)
    add_store_once_argument(run, "--gitlab-project", required=True)
    add_store_once_argument(run, "--gitlab-mr", required=True)
    add_store_once_argument(run, "--gitlab-token-file", required=True)
    add_store_once_argument(run, "--chromium")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        if arguments.helper == "run":
            return run_release(arguments)
        request = decode_privileged_request(arguments.helper, arguments.request_b64)
        result = dispatch_privileged(arguments.helper, request)
        if result.returncode != 0:
            return result.returncode or 2
        payload = {"returncode": 0}
        if isinstance(result, OperationResult):
            payload.update({"unit": result.unit, "cgroup": result.cgroup})
        elif isinstance(result, OperationInspectResult):
            payload = {
                "returncode": 0, "state": result.state, "attempt_id": result.attempt_id,
                "operation_id": result.operation_id, "registry": result.registry,
                "unit": result.unit, "cgroup": result.cgroup,
                "registry_digest": result.registry_digest,
            }
            if result.state == "TERMINAL":
                payload.update({
                    "exit_status": result.exit_status, "stdout": result.stdout,
                    "stderr": result.stderr, "output_digest": result.output_digest,
                })
        else:
            payload["digest"] = result.digest
            if result.terminal is not None:
                payload["terminal"] = result.terminal
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError, TypeError, PermissionError, RuntimeError,
            subprocess.SubprocessError, json.JSONDecodeError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
