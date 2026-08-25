"""Fail-closed candidate acceptance for Priority 1.3 physical draft releases."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

if __package__ in {None, ""}:
    # ``-I`` intentionally removes the script tree from sys.path.  Pin the
    # exported candidate root derived from this executable before package import.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from release_argparse import add_store_once_argument
from deploy.guarded_priority_1_3_release import (
    ControlResult, OperationResult, _operation_output_digest, _request_digest,
    _validated_attempt_registry,
    _validated_commit_terminal, encode_privileged_request, validate_transaction_record,
)


B1_REMOTE_CHECKS = (
    "protected-parity", "production-tests", "persistent-before",
    "local-http", "local-chromium", "physical-journey",
    "persistent-after", "rollback-parity",
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
    _CONTROL_KINDS = {
        "remote-stop", "remote-recover", "remote-commit",
        "remote-transaction-create-or-query", "remote-transaction-claim",
        "remote-transaction-inspect", "remote-transaction-transition",
        "remote-transaction-advance", "remote-transaction-terminal",
    }
    _TIMEOUT_STOP_SECONDS = 30

    def __init__(
        self, target: str, identity_file: str | os.PathLike[str],
        known_hosts_file: str | os.PathLike[str], *,
        connect_timeout: int = 10, runner=subprocess.run,
        clock=time.monotonic, sleep=time.sleep, poll_interval: float = 0.1,
    ) -> None:
        if not isinstance(target, str) or not target or any(c.isspace() for c in target):
            raise ValueError("SSH target is invalid")
        if connect_timeout != 10:
            raise ValueError("SSH connect timeout must be exactly 10 seconds")
        self.target = target
        self.identity_file = _pinned_file(identity_file, "identity")
        self.known_hosts_file = _pinned_file(known_hosts_file, "known-hosts")
        if (type(poll_interval) not in {int, float} or not math.isfinite(poll_interval)
                or not 0.01 <= poll_interval <= 1.0):
            raise ValueError("operation poll interval is invalid")
        self.connect_timeout = connect_timeout
        self._runner = runner
        self._clock = clock
        self._sleep = sleep
        self._poll_interval = float(poll_interval)

    def _ssh(self, helper: str, payload: str, operation_timeout: int) -> RemoteResult:
        if (type(operation_timeout) not in {int, float} or not math.isfinite(operation_timeout)
                or operation_timeout <= 0):
            raise ValueError("operation timeout is invalid")
        argv = [
            "/usr/bin/ssh", "-F", "/dev/null",
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

    @staticmethod
    def _parse_wire_envelope(
        kind: str, result: RemoteResult, request: dict[str, object],
    ) -> dict[str, object]:
        """Parse one exact successful helper envelope before result construction."""
        if type(result.returncode) is not int or result.returncode != 0:
            raise ValueError("remote helper transport result is invalid")
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise ValueError("remote helper returned malformed typed output") from error
        terminal_kinds = {
            "remote-commit", "remote-transaction-create-or-query",
            "remote-transaction-claim", "remote-transaction-inspect",
            "remote-transaction-transition", "remote-transaction-advance",
            "remote-transaction-terminal",
        }
        fields = ({"returncode", "digest", "terminal"} if kind in terminal_kinds else
                  {"returncode", "digest"})
        if kind == "remote-operation-inspect":
            if type(payload) is not dict:
                raise ValueError("remote operation inspection output is invalid")
            common = {"returncode", "state", "attempt_id", "operation_id", "registry", "unit",
                      "cgroup", "registry_digest"}
            state = payload.get("state")
            expected = common if state == "RUNNING" else common | {
                "exit_status", "stdout", "stderr", "output_digest",
            } if state == "TERMINAL" else set()
            if (frozenset(payload) != expected or type(payload.get("returncode")) is not int
                    or payload["returncode"] != 0
                    or payload.get("attempt_id") != request.get("attempt_id")
                    or payload.get("operation_id") != request.get("operation_id")
                    or payload.get("registry") != request.get("registry")
                    or payload.get("unit") != request.get("unit")
                    or payload.get("cgroup") != request.get("cgroup")
                    or type(payload.get("registry_digest")) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", payload["registry_digest"]) is None):
                raise ValueError("remote operation inspection identity is invalid")
            if state == "TERMINAL":
                exit_status, stdout, stderr = payload["exit_status"], payload["stdout"], payload["stderr"]
                if (type(exit_status) is not int or not 0 <= exit_status <= 255
                        or type(stdout) is not str or type(stderr) is not str
                        or len(stdout.encode("utf-8")) > 65536 or len(stderr.encode("utf-8")) > 65536
                        or type(payload.get("output_digest")) is not str
                        or payload["output_digest"] != _operation_output_digest(
                            str(request["attempt_id"]), str(request["operation_id"]),
                            str(request["registry"]), str(request["unit"]), str(request["cgroup"]),
                            exit_status, stdout, stderr,
                        )):
                    raise ValueError("remote operation terminal output is invalid")
            return payload
        if kind == "remote-start":
            actual = frozenset(payload) if type(payload) is dict else frozenset()
            fields = frozenset({"returncode", "unit", "cgroup"})
            if (actual != fields
                    or type(payload.get("returncode")) is not int
                    or payload["returncode"] != 0):
                raise ValueError("remote helper returned invalid typed output")
            unit, cgroup = payload["unit"], payload["cgroup"]
            expected_unit = f"matrix-p13-op-{request['attempt_id']}-{request['operation_id']}.service"
            if (type(unit) is not str or type(cgroup) is not str
                    or unit != expected_unit or cgroup != f"/system.slice/{expected_unit}"):
                raise ValueError("remote operation result is invalid")
            return payload
        if (type(payload) is not dict or frozenset(payload) != fields
                or type(payload.get("returncode")) is not int
                or payload["returncode"] != 0):
            raise ValueError("remote helper returned invalid typed output")
        digest = payload["digest"]
        if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("remote control result is invalid")
        if kind.startswith("remote-transaction-"):
            if type(payload["terminal"]) is not dict:
                raise ValueError("remote transaction result terminal is invalid")
            terminal = validate_transaction_record(payload["terminal"], str(request["release_key"]))
            if digest != terminal["record_digest"]:
                raise ValueError("remote transaction digest is not bound to terminal record")
            if (kind != "remote-transaction-create-or-query"
                    and terminal["transaction_id"] != request["transaction_id"]):
                raise ValueError("remote transaction terminal identity is invalid")
        elif kind == "remote-commit":
            if type(payload["terminal"]) is not dict:
                raise ValueError("remote commit result terminal is invalid")
            terminal = _validated_commit_terminal(
                payload["terminal"], request if request.get("query") is True else None,
            )
            if digest != terminal["result_digest"]:
                raise ValueError("remote commit digest is not bound to terminal result")
        return payload

    def run_remote(self, attempt_id, operation_id, argv, *, registry, run_as, operation_timeout):
        registry = _validated_attempt_registry(attempt_id, registry)
        if run_as not in {"root", "admin"}:
            raise ValueError("remote identity is invalid")
        if not isinstance(attempt_id, str) or re.fullmatch(r"[0-9a-f]{32}", attempt_id) is None:
            raise ValueError("remote attempt identity is invalid")
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("remote operation identity is invalid")
        if type(operation_timeout) is not int or operation_timeout <= 0:
            raise ValueError("operation timeout is invalid")
        deadline = self._clock() + operation_timeout
        canonical_operation = hashlib.sha256(
            f"{attempt_id}\0{operation_id}".encode("utf-8")
        ).hexdigest()[:32]
        request = {
            "schema": 1, "attempt_id": attempt_id, "operation_id": canonical_operation,
            "registry": registry,
            "run_as": run_as, "argv": argv,
        }
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError("remote operation aggregate deadline expired before start")
        result = self._ssh(
            "remote-start", encode_privileged_request("remote-start", request), remaining,
        )
        if result.returncode != 0:
            return OperationResult(result.returncode or 2, "", "", result.stdout, result.stderr)
        payload = self._parse_wire_envelope("remote-start", result, request)
        unit, cgroup = str(payload["unit"]), str(payload["cgroup"])
        if operation_id == "watchdog-arm":
            return OperationResult(0, unit, cgroup)
        inspect_request = {
            "schema": 1, "attempt_id": attempt_id, "operation_id": canonical_operation,
            "registry": registry, "unit": unit, "cgroup": cgroup,
        }
        encoded_inspect = encode_privileged_request("remote-operation-inspect", inspect_request)

        def stop_and_raise_timeout(message: str):
            stop_request = dict(inspect_request)
            try:
                stopped = self._ssh(
                    "remote-stop", encode_privileged_request("remote-stop", stop_request),
                    self._TIMEOUT_STOP_SECONDS,
                )
                if stopped.returncode != 0:
                    raise RuntimeError("remote timeout stop returned failure")
                payload = self._parse_wire_envelope("remote-stop", stopped, stop_request)
                if payload["digest"] != _request_digest("remote-stop", stop_request):
                    raise ValueError("remote timeout stop digest is not bound to exact operation")
            except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError) as error:
                raise RuntimeError(
                    "remote operation aggregate deadline expired without proven extinction"
                ) from error
            raise TimeoutError(message)

        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                stop_and_raise_timeout("remote operation aggregate deadline expired")
            try:
                inspected = self._ssh(
                    "remote-operation-inspect", encoded_inspect, min(10.0, remaining),
                )
            except (TimeoutError, subprocess.TimeoutExpired):
                inspected = None
            if inspected is not None:
                if inspected.returncode != 0:
                    raise ValueError("remote operation inspection transport failed")
                terminal = self._parse_wire_envelope(
                    "remote-operation-inspect", inspected, inspect_request,
                )
                if terminal["state"] == "TERMINAL":
                    return OperationResult(
                        int(terminal["exit_status"]), unit, cgroup,
                        str(terminal["stdout"]), str(terminal["stderr"]),
                    )
            remaining = deadline - self._clock()
            if remaining <= 0:
                stop_and_raise_timeout("remote operation aggregate deadline expired")
            self._sleep(min(self._poll_interval, remaining))

    def control_remote(self, kind, request, *, operation_timeout):
        if kind not in self._CONTROL_KINDS or not isinstance(request, dict):
            raise ValueError("remote control kind is invalid")
        result = self._ssh(kind, encode_privileged_request(kind, request), operation_timeout)
        if result.returncode != 0:
            return ControlResult(result.returncode or 2, "")
        payload = self._parse_wire_envelope(kind, result, request)
        return ControlResult(0, str(payload["digest"]), payload.get("terminal"))



class RemoteExecutor(Protocol):
    def run_remote(self, attempt_id: str, operation_id: str, argv: list[str], *, registry: str,
                   run_as: str, operation_timeout: int) -> object: ...


class JourneyClient(Protocol):
    def run(self, base_url: str, cookie_value: str, *, operation_timeout: int, aggregate_deadline: float) -> dict[str, object]: ...


_LOCAL_BASE_URL = "http://127.0.0.1:5000"
_HEX32_RE = re.compile(r"[0-9a-f]{32}")
_HEX40_RE = re.compile(r"[0-9a-f]{40}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_REMOTE_TIMEOUTS = {"protected-parity": 120, "production-tests": 6360, "persistent-before": 120,
                    "physical-journey": 180, "persistent-after": 120, "rollback-parity": 120}
# Shared contract with the guarded coordinator: 6300s for focused + full
# production unittest execution, plus 180s aggregate transport/journey overhead.
_AGGREGATE_ACCEPTANCE_BUDGET_SECONDS = 6480


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect sanitized Priority 1.3 candidate evidence")
    add_store_once_argument(parser, "--boundary", choices=("b1", "b2"), required=True)
    add_store_once_argument(parser, "--mode", choices=("deploy", "rollback"), required=True)
    add_store_once_argument(parser, "--ssh-target", required=True)
    add_store_once_argument(parser, "--ssh-identity-file", required=True)
    add_store_once_argument(parser, "--ssh-known-hosts-file", required=True)
    add_store_once_argument(parser, "--app-root", required=True)
    add_store_once_argument(parser, "--expected-sha", required=True)
    add_store_once_argument(parser, "--expected-version", required=True)
    add_store_once_argument(parser, "--session-cookie-file", required=True)
    add_store_once_argument(parser, "--evidence-file", required=True)
    add_store_once_argument(parser, "--attempt-id", required=True)
    add_store_once_argument(parser, "--acceptance-operation-id", required=True)
    add_store_once_argument(parser, "--attempt-registry", required=True)
    add_store_once_argument(parser, "--release-spec", required=True)
    add_store_once_argument(parser, "--release-spec-digest", required=True)
    return parser


def _evidence_identity(arguments: argparse.Namespace) -> str:
    if _HEX32_RE.fullmatch(str(arguments.attempt_id)) is None or _HEX40_RE.fullmatch(str(arguments.expected_sha)) is None:
        raise ValueError("acceptance identity is invalid")
    if (type(arguments.acceptance_operation_id) is not str
            or _HEX64_RE.fullmatch(arguments.acceptance_operation_id) is None):
        raise ValueError("acceptance operation identity is invalid")
    if type(arguments.expected_version) is not str or not arguments.expected_version or len(arguments.expected_version) > 128:
        raise ValueError("expected version is invalid")
    identity = {"attempt_id": arguments.attempt_id,
                "acceptance_operation_id": arguments.acceptance_operation_id,
                "boundary": arguments.boundary, "mode": arguments.mode,
                "expected_sha": arguments.expected_sha, "expected_version": arguments.expected_version}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _read_session_cookie(value: str | os.PathLike[str]) -> str:
    path = _pinned_file(value, "session-cookie")
    if path.stat().st_size > 4096:
        raise ValueError("session cookie is oversized")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        raw = os.read(descriptor, 4097)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(raw) > 4096 or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("session cookie changed while read")
    try:
        cookie = raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("session cookie is invalid") from error
    if (not cookie or any(char in cookie for char in "\r\n\0")
            or re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+=[!#$%&'*+.^_`|~0-9A-Za-z:/?@-]+", cookie) is None):
        raise ValueError("session cookie is invalid")
    return cookie


def _request_json(base_url: str, path: str, cookie: str, timeout: float, *, method="GET", payload=None, acceptance_authorization=None):
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Cookie": cookie, "Accept": "application/json"}
    if data is not None:
        headers.update({"Content-Type": "application/json", "X-Matrix-Action": "paired-request"})
    if acceptance_authorization is not None:
        headers["X-Matrix-Acceptance-Authorization"] = acceptance_authorization
    request = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
    # The release gate is pinned to the loopback endpoint. Environment proxy
    # settings must never redirect paired credentials or acceptance authority.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    body = response.read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024:
        raise ValueError("local HTTP response is oversized")
    return response.status, json.loads(body) if body else None


class LocalHttpJourney:
    def __init__(self, transaction=None, authorization_file=None):
        self._transaction = transaction
        self._authorization_file = authorization_file

    def _authorization(self):
        path = _pinned_file(self._authorization_file, "acceptance-authorization")
        if path.stat().st_size > 256:
            raise ValueError("acceptance authorization is oversized")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            before = os.fstat(descriptor)
            raw = os.read(descriptor, 257)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (len(raw) > 256 or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise ValueError("acceptance authorization changed while read")
        value = raw.decode("ascii").strip()
        if _HEX64_RE.fullmatch(value) is None:
            raise ValueError("acceptance authorization is invalid")
        return value

    def run(self, base_url, cookie_value, *, operation_timeout, aggregate_deadline):
        transaction = self._transaction
        if (base_url != _LOCAL_BASE_URL or operation_timeout != 300
                or time.monotonic() >= aggregate_deadline or type(transaction) is not dict):
            raise ValueError("local HTTP endpoint, identity, or deadline is invalid")
        authorization = self._authorization()
        timeout = lambda: _remaining_timeout(aggregate_deadline, operation_timeout)
        status, config = _request_json(base_url, "/api/config", cookie_value, timeout())
        status_code, status_payload = _request_json(base_url, "/api/status", cookie_value, timeout())
        if status != 200 or type(config) is not dict or status_code != 200 or type(status_payload) is not dict:
            raise ValueError("paired HTTP reads failed")
        generation, owner, nonce = 1, secrets.token_hex(16), secrets.token_hex(32)
        staged_request = {
            **transaction, "nonce": nonce, "owner_revision": config.get("revision"),
            "generation": generation, "preview_instance": owner,
        }
        staged, staged_result = _request_json(
            base_url, "/api/release-acceptance/transaction", cookie_value, timeout(),
            method="POST", payload=staged_request, acceptance_authorization=authorization,
        )
        if (staged != 202 or type(staged_result) is not dict
                or staged_result != {"state": "INIT", "nonce": nonce,
                                     "acceptance_operation_id": transaction["acceptance_operation_id"],
                                     "owner_generation": generation}):
            raise ValueError("acceptance transaction staging failed")
        candidate_config = dict(config)
        candidate_config.pop("revision", None)
        candidate_config.pop("schema_version", None)
        request = {"config": candidate_config, "generation": generation,
                   "preview_instance": owner, "timeout_seconds": 30}
        started, result = _request_json(base_url, "/api/device-preview/clock_lava", cookie_value,
                                        timeout(), method="POST", payload=request)
        control = {"generation": generation, "preview_instance": owner}
        immediate_heartbeat, _ = _request_json(base_url, "/api/device-preview/heartbeat", cookie_value, timeout(), method="POST", payload=control)
        foreign = {"generation": generation, "preview_instance": secrets.token_hex(16)}
        foreign_status, _ = _request_json(base_url, "/api/device-preview/heartbeat", cookie_value, timeout(), method="POST", payload=foreign)
        if (started != 202 or type(result) is not dict or result.get("generation") != generation
                or result.get("timeout_seconds") != 30 or result.get("heartbeat_after_seconds") != 5
                or type(result.get("expires_in")) not in {int, float}
                or isinstance(result.get("expires_in"), bool)
                or not 5 < float(result["expires_in"]) <= 30
                or immediate_heartbeat != 409 or foreign_status != 409):
            raise ValueError("paired HTTP device-preview journey failed")
        content_deadline = time.monotonic() + float(result["expires_in"])
        _wait_until_heartbeat_admission(result["heartbeat_after_seconds"], aggregate_deadline=aggregate_deadline)
        heartbeat, _ = _request_json(base_url, "/api/device-preview/heartbeat", cookie_value, timeout(), method="POST", payload=control)
        saved_status, saved = _request_json(base_url, "/api/config", cookie_value, timeout())
        if heartbeat != 202 or saved_status != 200 or saved != config:
            raise ValueError("paired HTTP device-preview journey failed")
        status_request = {
            **control, "nonce": nonce,
            "acceptance_operation_id": transaction["acceptance_operation_id"],
        }
        next_heartbeat = time.monotonic() + float(result["heartbeat_after_seconds"])
        while True:
            now = time.monotonic()
            if now >= next_heartbeat and now < content_deadline:
                maintained, _ = _request_json(
                    base_url, "/api/device-preview/heartbeat", cookie_value,
                    timeout(), method="POST", payload=control,
                )
                if maintained != 202:
                    raise ValueError("acceptance heartbeat maintenance failed")
                next_heartbeat = time.monotonic() + float(result["heartbeat_after_seconds"])
            completion_status, completion = _request_json(
                base_url, "/api/release-acceptance/transaction/status", cookie_value,
                timeout(), method="POST", payload=status_request,
                acceptance_authorization=authorization,
            )
            if completion_status == 200:
                break
            if completion_status != 202 or type(completion) is not dict:
                raise ValueError("acceptance transaction status failed")
            remaining = aggregate_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("acceptance transaction did not physically complete")
            time.sleep(min(0.1, remaining))
        expected_fields = {"state", "nonce", "acceptance_operation_id", "owner_generation",
                           "draft_revision", "saved_revision", "current_revision",
                           "foreign_rejected", "timeout_returned", "sleep_observed",
                           "rollback_complete"}
        if (type(completion) is not dict or set(completion) != expected_fields
                or completion["state"] != "SAVED_ROLLBACK_PUBLISHED"
                or completion["nonce"] != nonce
                or completion["acceptance_operation_id"] != transaction["acceptance_operation_id"]
                or completion["owner_generation"] != generation
                or any(completion[key] is not True for key in (
                    "foreign_rejected", "timeout_returned", "sleep_observed",
                    "rollback_complete",
                ))
                or any(type(completion[key]) is not int for key in ("saved_revision", "draft_revision", "current_revision"))
                or not completion["saved_revision"] < completion["draft_revision"] < completion["current_revision"]):
            raise ValueError("acceptance transaction completion is invalid")
        return {"owner_generation": generation, "nonce": nonce,
                "acceptance_operation_id": transaction["acceptance_operation_id"],
                "saved_revision": completion["saved_revision"],
                "draft_revision": completion["draft_revision"],
                "current_revision": completion["current_revision"],
                "foreign_rejected": completion["foreign_rejected"],
                "timeout_returned": completion["timeout_returned"],
                "sleep_observed": completion["sleep_observed"],
                "saved_output_returned": completion["rollback_complete"]}


class LocalChromiumJourney:
    _PROBE = r"""
const port=process.argv[1],base=process.argv[2],cookie=process.argv[3],sleep=ms=>new Promise(r=>setTimeout(r,ms));let endpoint;
for(let i=0;i<50;i++){try{endpoint=await fetch(`http://127.0.0.1:${port}/json/new`,{method:'PUT'}).then(r=>r.json());break}catch(_){await sleep(100)}}
if(!endpoint)throw Error('CDP unavailable');const ws=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map();let id=0;
ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result)}};
await new Promise((ok,bad)=>{ws.onopen=ok;ws.onerror=bad});const send=(method,params={})=>new Promise((ok,bad)=>{const n=++id;pending.set(n,{resolve:ok,reject:bad});ws.send(JSON.stringify({id:n,method,params}))});
try{const cut=cookie.indexOf('=');await send('Network.enable');await send('Network.setCookie',{name:cookie.slice(0,cut),value:cookie.slice(cut+1),url:base,httpOnly:true,sameSite:'Strict'});await send('Page.navigate',{url:base+'/'});await sleep(250);const expression=`Promise.all([fetch('/api/status').then(r=>r.status),fetch('/api/display-frame').then(r=>r.status)]).then(([status,frame])=>({paired:status===200,explicit_ipv4:location.hostname==='127.0.0.1',status_checked:status===200,frame_checked:frame===200}))`;const result=await send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(result.exceptionDetails)throw Error('browser evaluation failed');console.log(JSON.stringify(result.result.value))}finally{ws.close()}
"""
    def run(self, base_url, cookie_value, *, operation_timeout, aggregate_deadline):
        remaining = _remaining_timeout(aggregate_deadline, operation_timeout)
        chromium = os.environ.get("MATRIX_CHROMIUM", "/usr/bin/chromium")
        node = shutil.which("node")
        if base_url != _LOCAL_BASE_URL or operation_timeout != 300 or remaining <= 0:
            raise ValueError("Chromium endpoint or deadline is invalid")
        if not Path(chromium).is_file() or not os.access(chromium, os.X_OK) or node is None:
            raise ValueError("real Chromium journey is unavailable")
        with tempfile.TemporaryDirectory(prefix="matrix-p13-chromium-") as directory:
            profile = Path(directory) / "profile"
            browser = subprocess.Popen([chromium, "--headless", "--no-sandbox", "--disable-gpu",
                "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                active, end = profile / "DevToolsActivePort", time.monotonic() + min(10, remaining)
                while not active.is_file() and time.monotonic() < end:
                    if browser.poll() is not None: raise ValueError("Chromium exited before DevTools")
                    time.sleep(.05)
                if not active.is_file(): raise TimeoutError("Chromium DevTools deadline expired")
                port = active.read_text(encoding="ascii").splitlines()[0]
                completed = subprocess.run([node, "-e", self._PROBE, port, base_url, cookie_value],
                    text=True, capture_output=True, check=False,
                    timeout=_remaining_timeout(aggregate_deadline, operation_timeout), shell=False)
            finally:
                try:
                    os.killpg(browser.pid, 15); browser.wait(timeout=5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try: os.killpg(browser.pid, 9)
                    except ProcessLookupError: pass
                    browser.wait(timeout=5)
        if completed.returncode != 0 or completed.stderr:
            raise ValueError("real Chromium journey failed")
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        expected = {"paired": True, "explicit_ipv4": True, "status_checked": True, "frame_checked": True}
        if result != expected: raise ValueError("real Chromium journey is incomplete")
        return result


def _wait_until_heartbeat_admission(cadence_seconds: object, *, aggregate_deadline: float) -> None:
    if type(cadence_seconds) not in {int, float} or not 0 < cadence_seconds <= 60:
        raise ValueError("heartbeat cadence metadata is invalid")
    target = time.monotonic() + float(cadence_seconds)
    if aggregate_deadline <= target:
        raise TimeoutError("aggregate acceptance deadline cannot fund heartbeat cadence")
    while True:
        remaining = target - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(remaining)


def _remaining_timeout(aggregate_deadline: float, operation_cap: float) -> float:
    remaining = min(float(operation_cap), aggregate_deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("aggregate acceptance deadline expired")
    return remaining


def _remaining_remote_timeout(aggregate_deadline: float, operation_cap: int) -> int:
    remaining = _remaining_timeout(aggregate_deadline, operation_cap)
    timeout = int(remaining)
    if timeout <= 0:
        raise TimeoutError("aggregate acceptance deadline cannot fund remote operation")
    return timeout


def _digest(value):
    return type(value) is str and _HEX64_RE.fullmatch(value) is not None


def _approved_protected_files(arguments) -> dict[str, dict[str, str]]:
    path = Path(arguments.release_spec)
    expected_digest = arguments.release_spec_digest
    if not path.is_absolute() or not _digest(expected_digest):
        raise ValueError("approved release spec identity is invalid")
    try:
        before = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise ValueError("approved release spec is unavailable") from error
    try:
        opened = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) != 0o400 or opened.st_nlink != 1
                or opened.st_size > 8 * 1024 * 1024):
            raise ValueError("approved release spec is not pinned")
        raw = bytearray()
        while len(raw) <= 8 * 1024 * 1024:
            chunk = os.read(descriptor, min(131072, 8 * 1024 * 1024 + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (len(raw) > 8 * 1024 * 1024
            or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or hashlib.sha256(raw).hexdigest() != expected_digest):
        raise ValueError("approved release spec changed while read")

    def exact_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("approved release spec contains duplicate fields")
            value[key] = item
        return value

    try:
        document = json.loads(bytes(raw), object_pairs_hook=exact_object)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ValueError("approved release spec is invalid") from error
    if (type(document) is not dict or set(document) != {"version", "files"}
            or document.get("version") != 1 or type(document.get("files")) is not list
            or not document["files"] or len(document["files"]) > 4096):
        raise ValueError("approved release spec is invalid")
    approved_files: dict[str, str] = {}
    approved_modes: dict[str, str] = {}
    for entry in document["files"]:
        if (type(entry) is not dict or set(entry) != {"path", "sha256", "uid", "gid", "mode"}
                or type(entry.get("path")) is not str or not _digest(entry.get("sha256"))
                or type(entry.get("uid")) is not int or entry["uid"] < 0
                or type(entry.get("gid")) is not int or entry["gid"] < 0
                or type(entry.get("mode")) is not str
                or re.fullmatch(r"0[0-7]{3}", entry["mode"]) is None):
            raise ValueError("approved release file entry is invalid")
        relative = entry["path"]
        parsed = PurePosixPath(relative)
        if (parsed.is_absolute() or str(parsed) != relative
                or any(part in {"", ".", ".."} for part in parsed.parts)
                or relative in approved_files):
            raise ValueError("approved release file path is invalid")
        approved_files[relative] = entry["sha256"]
        approved_modes[relative] = entry["mode"]
    return {"files": approved_files, "modes": approved_modes}


def _validated_payload(operation, raw, arguments, evidence_id):
    value = json.loads(raw)
    check = "persistent-state" if operation.startswith("persistent-") else operation
    common = {"schema": 1, "check": check, "attempt_id": arguments.attempt_id,
              "acceptance_operation_id": arguments.acceptance_operation_id,
              "evidence_id": evidence_id}
    schema = value.get("schema") if type(value) is dict else None
    if (type(value) is not dict or not (type(schema) is int and schema == 1)
            or any(value.get(k) != v for k, v in common.items())):
        raise ValueError("remote evidence identity mismatch")
    for key in common: value.pop(key)
    if operation == "protected-parity":
        approved = _approved_protected_files(arguments)
        approved_digest = hashlib.sha256(json.dumps(
            approved, sort_keys=True, separators=(",", ":")
        ).encode("ascii")).hexdigest()
        if (set(value) != {"source_sha", "version", "installed_digest", "files", "modes"}
                or value["source_sha"] != arguments.expected_sha or value["version"] != arguments.expected_version
                or not _digest(value["installed_digest"]) or type(value["files"]) is not dict or not value["files"]
                or any(type(k) is not str or not _digest(v) for k, v in value["files"].items())
                or type(value["modes"]) is not dict or not value["modes"]
                or any(type(k) is not str or type(v) is not str
                       or re.fullmatch(r"0[0-7]{3}", v) is None for k, v in value["modes"].items())
                or value["files"] != approved["files"] or value["modes"] != approved["modes"]
                or value["installed_digest"] != approved_digest):
            raise ValueError("installed protected-byte proof mismatch")
    elif operation == "production-tests":
        if set(value) != {"focused", "full"}: raise ValueError("production tests missing")
        for result in value.values():
            if (type(result) is not dict
                    or set(result) != {"passed", "duration_ms", "command_digest", "stderr_size", "stderr_digest"}
                    or result["passed"] is not True or type(result["duration_ms"]) is not int
                    or result["duration_ms"] <= 0 or not _digest(result["command_digest"])
                    or type(result["stderr_size"]) is not int
                    or not 0 <= result["stderr_size"] <= 1024 * 1024
                    or not _digest(result["stderr_digest"])):
                raise ValueError("production tests invalid")
    elif operation.startswith("persistent-"):
        if set(value) != {"config", "paired", "media"} or any(not _digest(v) for v in value.values()):
            raise ValueError("persistent state invalid")
    elif operation == "physical-journey":
        required = {"service_active", "draft_signature", "saved_signature", "width", "height", "owner_generation", "foreign_rejected", "timeout_returned", "sleep_observed"}
        if (set(value) != required or value["service_active"] is not True or value["width"] != 64 or value["height"] != 32
                or type(value["owner_generation"]) is not int or value["owner_generation"] < 0
                or any(value[k] is not True for k in ("foreign_rejected", "timeout_returned", "sleep_observed"))
                or not _digest(value["draft_signature"]) or not _digest(value["saved_signature"])):
            raise ValueError("physical journey invalid")
    else:
        if (set(value) != {"mode", "saved_output", "saved_signature"} or value["mode"] != arguments.mode
                or value["saved_output"] is not True or not _digest(value["saved_signature"])):
            raise ValueError("saved-output parity invalid")
    return value


def _safe_parent(path):
    if not path.is_absolute() or path.name in {"", ".", ".."}: raise ValueError("evidence path invalid")
    parent = path.parent
    info = parent.stat(follow_symlinks=False)
    if (not stat.S_ISDIR(info.st_mode) or parent.is_symlink() or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o022): raise ValueError("evidence parent unsafe")
    if path.exists() or path.is_symlink():
        current = path.lstat()
        if (not stat.S_ISREG(current.st_mode) or stat.S_ISLNK(current.st_mode) or current.st_uid != os.geteuid()
                or stat.S_IMODE(current.st_mode) != 0o600 or current.st_nlink != 1):
            raise ValueError("existing evidence unsafe")
    return parent


def _publish_evidence(path, encoded):
    parent = _safe_parent(path)
    expected_parent = parent.stat(follow_symlinks=False)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    opened_parent = os.fstat(parent_fd)
    if ((opened_parent.st_dev, opened_parent.st_ino) != (expected_parent.st_dev, expected_parent.st_ino)
            or opened_parent.st_uid != os.geteuid() or stat.S_IMODE(opened_parent.st_mode) & 0o022):
        os.close(parent_fd)
        raise ValueError("evidence parent changed during publication")
    temporary, descriptor = f".{path.name}.{secrets.token_hex(16)}", None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        os.fchmod(descriptor, 0o600)
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0: raise OSError("short evidence write")
            view = view[written:]
        os.fsync(descriptor); os.close(descriptor); descriptor = None
        os.replace(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except BaseException:
        if descriptor is not None: os.close(descriptor)
        try: os.unlink(temporary, dir_fd=parent_fd)
        except OSError: pass
        raise
    finally:
        os.close(parent_fd)


def run_acceptance(arguments, executor, *, http_client=None, chromium_client=None):
    try:
        cookie = _read_session_cookie(arguments.session_cookie_file)
        evidence_id, evidence_path = _evidence_identity(arguments), Path(arguments.evidence_file)
        _safe_parent(evidence_path)
        aggregate_deadline, candidate = time.monotonic() + _AGGREGATE_ACCEPTANCE_BUDGET_SECONDS, {}
        plan = (("protected-parity", "protected-parity"), ("production-tests", "production-tests"),
                ("persistent-before", "persistent-state"))
        for operation, check in plan:
            argv = [f"{arguments.app_root}/venv/bin/python", "-I", "-B", f"{arguments.app_root}/scripts/verify_priority_1_3_remote.py",
                    "--check", check, "--mode", arguments.mode, "--app-root", arguments.app_root,
                    "--attempt-id", arguments.attempt_id,
                    "--acceptance-operation-id", arguments.acceptance_operation_id,
                    "--evidence-id", evidence_id, "--expected-sha", arguments.expected_sha,
                    "--expected-version", arguments.expected_version]
            result = executor.run_remote(
                arguments.attempt_id,
                f"acceptance:{arguments.acceptance_operation_id}:{operation}", argv,
                registry=arguments.attempt_registry,
                run_as="admin", operation_timeout=_remaining_remote_timeout(aggregate_deadline, _REMOTE_TIMEOUTS[operation]),
            )
            if (not hasattr(result, "returncode") or result.returncode != 0 or type(result.stdout) is not str
                    or type(result.stderr) is not str or result.stderr): return 2
            candidate[operation] = _validated_payload(operation, result.stdout, arguments, evidence_id)
        local_transaction = {
            "attempt_id": arguments.attempt_id,
            "acceptance_operation_id": arguments.acceptance_operation_id,
            "evidence_id": evidence_id, "expected_source_sha": arguments.expected_sha,
            "expected_version": arguments.expected_version, "mode": arguments.mode,
        }
        authorization_file = Path(arguments.app_root) / "data" / "acceptance-authorization.key"
        local_http = (http_client or LocalHttpJourney(local_transaction, authorization_file)).run(_LOCAL_BASE_URL, cookie, operation_timeout=300, aggregate_deadline=aggregate_deadline)
        local_chromium = (chromium_client or LocalChromiumJourney()).run(_LOCAL_BASE_URL, cookie, operation_timeout=300, aggregate_deadline=aggregate_deadline)
        if (type(local_http) is not dict or set(local_http) != {"owner_generation", "nonce", "acceptance_operation_id", "saved_revision", "draft_revision", "current_revision", "foreign_rejected", "timeout_returned", "sleep_observed", "saved_output_returned"}
                or type(local_http["owner_generation"]) is not int or not _digest(local_http["nonce"])
                or local_http["acceptance_operation_id"] != arguments.acceptance_operation_id
                or any(type(local_http[k]) is not int for k in ("saved_revision", "draft_revision", "current_revision"))
                or not local_http["saved_revision"] < local_http["draft_revision"] < local_http["current_revision"]
                or any(local_http[k] is not True for k in ("foreign_rejected", "timeout_returned", "sleep_observed", "saved_output_returned"))):
            raise ValueError("local HTTP journey incomplete")
        if local_chromium != {"paired": True, "explicit_ipv4": True, "status_checked": True, "frame_checked": True}:
            raise ValueError("local Chromium journey incomplete")
        candidate.update({"local-http": local_http, "local-chromium": local_chromium})
        for operation, check in (("physical-journey", "physical-journey"), ("persistent-after", "persistent-state"), ("rollback-parity", "rollback-parity")):
            argv = [f"{arguments.app_root}/venv/bin/python", "-I", "-B", f"{arguments.app_root}/scripts/verify_priority_1_3_remote.py",
                    "--check", check, "--mode", arguments.mode, "--app-root", arguments.app_root,
                    "--attempt-id", arguments.attempt_id,
                    "--acceptance-operation-id", arguments.acceptance_operation_id,
                    "--evidence-id", evidence_id, "--expected-sha", arguments.expected_sha,
                    "--expected-version", arguments.expected_version]
            result = executor.run_remote(
                arguments.attempt_id,
                f"acceptance:{arguments.acceptance_operation_id}:{operation}", argv,
                registry=arguments.attempt_registry,
                run_as="admin", operation_timeout=_remaining_remote_timeout(aggregate_deadline, _REMOTE_TIMEOUTS[operation]),
            )
            if (not hasattr(result, "returncode") or result.returncode != 0 or type(result.stdout) is not str
                    or type(result.stderr) is not str or result.stderr): return 2
            candidate[operation] = _validated_payload(operation, result.stdout, arguments, evidence_id)
        if candidate["persistent-before"] != candidate["persistent-after"]: raise ValueError("persistent state changed")
        document = {"schema": 2, "boundary": arguments.boundary, "mode": arguments.mode,
                    "attempt_id": arguments.attempt_id,
                    "acceptance_operation_id": arguments.acceptance_operation_id,
                    "evidence_id": evidence_id, "candidate_evidence": candidate}
        _publish_evidence(evidence_path, (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode())
        return 0
    except (OSError, ValueError, TypeError, TimeoutError, subprocess.SubprocessError, json.JSONDecodeError, UnicodeError):
        return 2


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    try:
        executor = PinnedRemoteExecutor(arguments.ssh_target, arguments.ssh_identity_file, arguments.ssh_known_hosts_file)
    except ValueError:
        return 2
    return run_acceptance(arguments, executor)


if __name__ == "__main__":
    raise SystemExit(main())
