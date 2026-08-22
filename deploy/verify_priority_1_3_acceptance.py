"""Fail-closed candidate acceptance for Priority 1.3 physical draft releases."""
from __future__ import annotations

import argparse
import base64
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


B1_REMOTE_CHECKS = (
    "protected-parity",
    "production-tests",
    "physical-journey",
    "persistent-state",
    "rollback-parity",
)


@dataclass(frozen=True)
class RemoteResult:
    returncode: int
    stdout: str
    stderr: str


def _pinned_file(value: str | os.PathLike[str], label: str) -> Path:
    path = Path(value)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError(f"{label} pin is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid() or metadata.st_nlink != 1
    ):
        raise ValueError(f"{label} pin must be owner-only regular file")
    return path


def _canonical_argv_payload(argv: list[str]) -> str:
    if not isinstance(argv, list) or not 1 <= len(argv) <= 256:
        raise ValueError("remote argv count is invalid")
    for argument in argv:
        if not isinstance(argument, str) or "\0" in argument or len(argument.encode("utf-8")) > 4096:
            raise ValueError("remote argv is invalid")
    raw = json.dumps(argv, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > 49152:
        raise ValueError("remote argv aggregate is too large")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class PinnedRemoteExecutor:
    """The sole argv-only SSH transport for acceptance and guarded controls."""
    _CONTROL_KINDS = {"remote-stop", "remote-recover", "remote-commit"}

    def __init__(
        self, target: str, identity_file: str | os.PathLike[str],
        known_hosts_file: str | os.PathLike[str], *,
        connect_timeout: int = 10, runner=subprocess.run,
    ) -> None:
        if not isinstance(target, str) or not target or any(c.isspace() for c in target):
            raise ValueError("SSH target is invalid")
        if connect_timeout != 10:
            raise ValueError("SSH connect timeout must be exactly 10 seconds")
        self.target = target
        self.identity_file = _pinned_file(identity_file, "identity")
        self.known_hosts_file = _pinned_file(known_hosts_file, "known-hosts")
        self.connect_timeout = connect_timeout
        self._runner = runner

    def _ssh(self, helper: str, payload: str, operation_timeout: int) -> RemoteResult:
        if type(operation_timeout) is not int or operation_timeout <= 0:
            raise ValueError("operation timeout is invalid")
        argv = [
            "ssh", "-F", "/dev/null",
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "IdentityAgent=none", "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={self.known_hosts_file}",
            "-o", f"ConnectTimeout={self.connect_timeout}",
            "-i", str(self.identity_file), "--", self.target,
            "/usr/local/libexec/matrix-priority-1-3-remote", helper,
            "--request-b64", payload,
        ]
        environment = dict(os.environ)
        for name in tuple(environment):
            upper = name.upper()
            if upper in {"SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_SSH", "GIT_SSH_COMMAND"} or upper.endswith("_PROXY"):
                environment.pop(name, None)
        completed = self._runner(
            argv, check=False, text=True, capture_output=True,
            timeout=operation_timeout, env=environment, shell=False,
        )
        return RemoteResult(completed.returncode, completed.stdout, completed.stderr)

    def run_remote(self, attempt_id, operation_id, argv, *, run_as, operation_timeout):
        if run_as not in {"root", "admin"}:
            raise ValueError("remote identity is invalid")
        if not isinstance(attempt_id, str) or not isinstance(operation_id, str):
            raise ValueError("remote operation identity is invalid")
        request_argv = [attempt_id, operation_id, run_as, *argv]
        return self._ssh("remote-start", _canonical_argv_payload(request_argv), operation_timeout)

    def control_remote(self, kind, request, *, operation_timeout):
        if kind not in self._CONTROL_KINDS or not isinstance(request, dict):
            raise ValueError("remote control kind is invalid")
        canonical_request = json.dumps(request, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        return self._ssh(kind, _canonical_argv_payload([canonical_request]), operation_timeout)


class RemoteExecutor(Protocol):
    def run_remote(self, attempt_id: str, operation_id: str, argv: list[str], *, run_as: str, operation_timeout: int) -> RemoteResult: ...


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect sanitized Priority 1.3 candidate evidence")
    parser.add_argument("--boundary", choices=("b1", "b2"), required=True)
    parser.add_argument("--mode", choices=("deploy", "rollback"), required=True)
    parser.add_argument("--ssh-target", required=True)
    parser.add_argument("--ssh-identity-file", required=True)
    parser.add_argument("--ssh-known-hosts-file", required=True)
    parser.add_argument("--app-root", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--session-cookie-file", required=True)
    parser.add_argument("--evidence-file", required=True)
    return parser


def _validated_payload(operation: str, raw: str, arguments: argparse.Namespace) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ValueError(f"{operation} returned malformed evidence") from error
    if not isinstance(value, dict):
        raise ValueError(f"{operation} returned non-object evidence")
    if operation == "protected-parity":
        if value != {"sha": arguments.expected_sha, "version": arguments.expected_version}:
            raise ValueError("protected parity mismatch")
    elif operation == "production-tests":
        if set(value) != {"focused", "full"} or any(type(value[k]) is not int or value[k] <= 0 for k in value):
            raise ValueError("production test evidence is incomplete")
    elif operation == "physical-journey":
        required = {"draft_signature", "saved_signature", "width", "height", "owner_generation", "foreign_rejected", "timeout_returned", "sleep_returned"}
        if set(value) != required or value["width"] != 64 or value["height"] != 32 or not all(value[k] is True for k in ("foreign_rejected", "timeout_returned", "sleep_returned")):
            raise ValueError("physical journey evidence is incomplete")
        for key in ("draft_signature", "saved_signature"):
            if not isinstance(value[key], str) or len(value[key]) != 64:
                raise ValueError("physical signature is invalid")
    elif operation == "persistent-state":
        if set(value) != {"config", "paired", "media"} or any(not isinstance(v, str) or len(v) != 64 for v in value.values()):
            raise ValueError("persistent state evidence is incomplete")
    elif operation == "rollback-parity":
        if value != {"mode": arguments.mode, "saved_output": True}:
            raise ValueError("rollback/deploy saved-output evidence mismatch")
    return value


def run_acceptance(arguments: argparse.Namespace, executor: RemoteExecutor) -> int:
    evidence_path = Path(arguments.evidence_file)
    try:
        candidate: dict[str, object] = {}
        attempt = f"accept-{arguments.boundary}-{arguments.mode}"
        for operation in B1_REMOTE_CHECKS:
            argv = [
                f"{arguments.app_root}/venv/bin/python", "-I", "-B",
                f"{arguments.app_root}/scripts/verify_priority_1_3_remote.py",
                "--check", operation, "--mode", arguments.mode,
            ]
            result = executor.run_remote(
                attempt, operation, argv, run_as="admin", operation_timeout=10
            )
            if type(result) is not RemoteResult or result.returncode != 0 or result.stderr:
                return 2
            candidate[operation] = _validated_payload(operation, result.stdout, arguments)
        document = {"schema": 1, "candidate_evidence": candidate}
        encoded = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(evidence_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return 0
    except (OSError, ValueError, TypeError):
        try:
            evidence_path.unlink(missing_ok=True)
        except OSError:
            pass
        return 2


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        executor = PinnedRemoteExecutor(
            arguments.ssh_target, arguments.ssh_identity_file,
            arguments.ssh_known_hosts_file,
        )
    except ValueError:
        return 2
    return run_acceptance(arguments, executor)


if __name__ == "__main__":
    raise SystemExit(main())
