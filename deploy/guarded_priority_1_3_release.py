"""Guarded Priority 1.3 release topology (local coordinator)."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
import threading
from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass(frozen=True)
class ControlResult:
    returncode: int
    digest: str


@dataclass(frozen=True)
class OperationResult:
    returncode: int
    unit: str
    cgroup: str


class PinnedExecutor(Protocol):
    def control_remote(self, kind: str, request: dict[str, object], *, operation_timeout: int) -> ControlResult: ...
    def run_remote(self, attempt_id: str, operation_id: str, argv: list[str], *, run_as: str, operation_timeout: int) -> OperationResult: ...


_PRIVILEGED_HELPERS = frozenset(("remote-start", "remote-stop", "remote-recover", "remote-commit"))
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


_RECOVERY_FIELDS = frozenset((
    "schema", "boundary", "phase", "attempt_id", "evidence_digest", "pending_note_id", "guard_unit"
))


def canonical_recovery_request_digest(request: dict[str, object]) -> str:
    if type(request) is not dict or frozenset(request) != _RECOVERY_FIELDS or request.get("schema") != 1:
        raise ValueError("recovery request has an invalid schema")
    for field in _RECOVERY_FIELDS - {"schema"}:
        if type(request[field]) is not str or not request[field]:
            raise ValueError(f"invalid recovery field: {field}")
    if request["boundary"] not in {"b1", "b2"}:
        raise ValueError("invalid recovery boundary")
    if request["phase"] not in {"initial-deploy", "rollback-proof", "final-deploy"}:
        raise ValueError("invalid recovery phase")
    if re.fullmatch(r"[0-9a-f]{64}", request["evidence_digest"]) is None:
        raise ValueError("invalid evidence digest")
    canonical = json.dumps(request, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
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


def run_three_attempts(
    executor: PinnedExecutor,
    accept: Callable[[str, str], int],
    *, boundary: str, stage: str, bundle: str,
) -> int:
    """Run only initial deploy, rollback proof, and final deploy under fresh guards."""
    topology = (
        ("initial-deploy", "deploy"),
        ("rollback-proof", "rollback"),
        ("final-deploy", "deploy"),
    )
    for phase, operation in topology:
        attempt_id = secrets.token_hex(16)
        arm = _recover_request(boundary, phase, attempt_id, stage, bundle, "arm")
        armed = executor.control_remote("remote-recover", arm, operation_timeout=10)
        if type(armed) is not ControlResult or armed.returncode != 0:
            return 2
        command = [f"{stage}/deploy/release_bundle.py", operation, "--bundle", bundle]
        operated = executor.run_remote(
            attempt_id, operation, command, run_as="root", operation_timeout=10
        )
        if type(operated) is not OperationResult or operated.returncode != 0:
            executor.control_remote(
                "remote-recover",
                _recover_request(boundary, phase, attempt_id, stage, bundle, "execute"),
                operation_timeout=10,
            )
            return operated.returncode if type(operated) is OperationResult and operated.returncode else 2
        result = accept(phase, attempt_id)
        if type(result) is not int or result != 0:
            executor.control_remote(
                "remote-recover",
                _recover_request(boundary, phase, attempt_id, stage, bundle, "execute"),
                operation_timeout=10,
            )
            return result if type(result) is int and result else 2
        committed = executor.control_remote(
            "remote-commit",
            {
                "schema": 1, "boundary": boundary, "phase": phase,
                "attempt_id": attempt_id, "acceptance_result": result,
                "guard_unit": arm["guard_unit"],
            },
            operation_timeout=10,
        )
        if type(committed) is not ControlResult or committed.returncode != 0:
            executor.control_remote(
                "remote-recover",
                _recover_request(boundary, phase, attempt_id, stage, bundle, "execute"),
                operation_timeout=10,
            )
            return 2
    return 0
