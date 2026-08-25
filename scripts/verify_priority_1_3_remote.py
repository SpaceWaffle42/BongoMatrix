#!/usr/bin/env python3
"""Immutable remote-side Priority 1.3 acceptance checks.

The verifier reads the installed candidate and runtime artifacts directly.  Caller
expectations are deliberately not accepted: the local acceptance process compares
these independently observed values with its trusted expectations.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release_argparse import add_store_once_argument

_CHECKS = (
    "protected-parity", "production-tests", "physical-journey",
    "persistent-state", "rollback-parity",
)
_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX40 = re.compile(r"[0-9a-f]{40}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_MAX_INTERPRETER_SYMLINKS = 16
_MAX_UNITTEST_STDERR_BYTES = 1024 * 1024
_TRUSTED_SYSTEM_INTERPRETER_ROOTS = (Path("/usr/bin"), Path("/usr/local/bin"))
_FIXED_TEST_ENV = {
    "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONHASHSEED": "0",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
}
# This source is immutable.  The validated app root and fixed unittest arguments are
# separate argv fields, never interpolated into executable Python source.
_UNITTEST_BOOTSTRAP = (
    "import os,sys,unittest\n"
    "root=os.fsdecode(os.fsencode(sys.argv[1]));del sys.argv[1]\n"
    "os.chdir(root);sys.path.insert(0,root)\n"
    "unittest.main(module=None,argv=sys.argv)\n"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify installed Priority 1.3 candidate bytes and behavior")
    add_store_once_argument(parser, "--check", choices=_CHECKS, required=True)
    add_store_once_argument(parser, "--mode", choices=("deploy", "rollback"), required=True)
    add_store_once_argument(parser, "--app-root", required=True)
    add_store_once_argument(parser, "--attempt-id", required=True)
    add_store_once_argument(parser, "--acceptance-operation-id", required=True)
    add_store_once_argument(parser, "--evidence-id", required=True)
    add_store_once_argument(parser, "--expected-sha", required=True)
    add_store_once_argument(parser, "--expected-version", required=True)
    return parser


def _regular(path: Path, label: str) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"{label} is not a pinned regular file")
    return info


def _read(path: Path, label: str, *, limit: int = 8 * 1024 * 1024) -> bytes:
    info = _regular(path, label)
    if info.st_size > limit:
        raise ValueError(f"{label} is oversized")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(descriptor, min(131072, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(data) > limit or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError(f"{label} changed while read")
    return bytes(data)


def _read_with_mode(
    path: Path, label: str, *, limit: int = 8 * 1024 * 1024,
) -> tuple[bytes, str]:
    info = _regular(path, label)
    if info.st_size > limit:
        raise ValueError(f"{label} is oversized")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(descriptor, min(131072, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (len(data) > limit
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode)):
        raise ValueError(f"{label} changed while read")
    return bytes(data), f"{stat.S_IMODE(after.st_mode):04o}"


def _relative(root: Path, raw: object) -> Path:
    if type(raw) is not str or not raw or raw.startswith("/") or "\\" in raw:
        raise ValueError("manifest path is invalid")
    parts = Path(raw).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("manifest path is invalid")
    path = root.joinpath(*parts)
    if path.parent.resolve(strict=True) != root.joinpath(*parts[:-1]).resolve(strict=True):
        raise ValueError("manifest parent escaped app root")
    return path


def _version(root: Path) -> str:
    source = _read(root / "webapp.py", "installed version source", limit=2 * 1024 * 1024)
    tree = ast.parse(source, filename="webapp.py")
    values = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
              for target in node.targets if isinstance(target, ast.Name)
              and target.id == "APPLICATION_VERSION" and isinstance(node.value, ast.Constant)
              and type(node.value.value) is str]
    if len(values) != 1 or not values[0] or len(values[0]) > 128:
        raise ValueError("installed version is unavailable")
    return values[0]


def _protected(root: Path) -> dict[str, object]:
    raw, marker_mode = _read_with_mode(
        root / ".matrix-release.json", "installed release manifest",
        limit=1024 * 1024,
    )
    manifest = json.loads(raw)
    schema = manifest.get("schema") if type(manifest) is dict else None
    if (type(manifest) is not dict
            or set(manifest) != {"schema", "source_sha", "candidate_tracked_files", "files"}
            or not (type(schema) is int and schema == 1)
            or _HEX40.fullmatch(str(manifest["source_sha"])) is None):
        raise ValueError("installed release manifest is invalid")
    entries = manifest["files"]
    candidate_tracked_files = manifest["candidate_tracked_files"]
    if type(entries) is not list or not entries or len(entries) > 4096:
        raise ValueError("installed release file list is invalid")
    if (type(candidate_tracked_files) is not list
            or any(type(path) is not str for path in candidate_tracked_files)
            or len(candidate_tracked_files) != len(set(candidate_tracked_files))
            or candidate_tracked_files != sorted(candidate_tracked_files)):
        raise ValueError("candidate tracked file authority is invalid")
    observed: dict[str, str] = {
        ".matrix-release.json": hashlib.sha256(raw).hexdigest(),
    }
    modes: dict[str, str] = {".matrix-release.json": marker_mode}
    for entry in entries:
        if type(entry) is not dict or set(entry) != {"path", "sha256"} or _HEX64.fullmatch(str(entry["sha256"])) is None:
            raise ValueError("installed release entry is invalid")
        relative = entry["path"]
        path = _relative(root, relative)
        data, mode = _read_with_mode(path, "installed protected byte")
        digest = hashlib.sha256(data).hexdigest()
        if digest != entry["sha256"] or relative in observed:
            raise ValueError("installed protected-byte digest mismatch")
        observed[relative] = digest
        modes[relative] = mode
    if candidate_tracked_files != sorted(
        relative for relative in observed if relative != ".matrix-release.json"
    ):
        raise ValueError("candidate tracked files do not match installed protected bytes")
    protected = {"files": observed, "modes": modes}
    aggregate = hashlib.sha256(json.dumps(protected, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()
    return {"source_sha": manifest["source_sha"], "version": _version(root),
            "installed_digest": aggregate, **protected}


def _hash_tree(path: Path) -> str:
    if not path.exists():
        return hashlib.sha256(b"absent").hexdigest()
    if path.is_symlink():
        raise ValueError("persistent state tree is linked")
    if path.is_file():
        return hashlib.sha256(_read(path, "persistent state")).hexdigest()
    if not path.is_dir():
        raise ValueError("persistent state is invalid")
    records: list[tuple[str, str]] = []
    for child in sorted(path.rglob("*")):
        if child.is_symlink():
            raise ValueError("persistent state contains link")
        if child.is_file():
            records.append((child.relative_to(path).as_posix(), hashlib.sha256(_read(child, "persistent state")).hexdigest()))
        elif not child.is_dir():
            raise ValueError("persistent state contains special file")
    return hashlib.sha256(json.dumps(records, separators=(",", ":")).encode("ascii")).hexdigest()


def _confined(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validated_interpreter(
    root: Path, *, trusted_uid: int | None = None,
    trusted_system_roots: tuple[Path, ...] | None = None,
) -> Path:
    """Admit only a bounded, owner-trusted venv interpreter symlink chain.

    This deliberately walks each link with lstat instead of treating realpath as
    authority.  The final executable must be either confined to this venv or under
    one of the explicit system interpreter roots.
    """
    uid = 0 if trusted_uid is None else trusted_uid
    if type(uid) is not int or uid < 0:
        raise ValueError("trusted interpreter uid is invalid")
    roots = _TRUSTED_SYSTEM_INTERPRETER_ROOTS if trusted_system_roots is None else trusted_system_roots
    if type(roots) is not tuple or not roots:
        raise ValueError("trusted interpreter roots are invalid")
    trusted_roots = tuple(Path(item) for item in roots)
    if any(not item.is_absolute() for item in trusted_roots):
        raise ValueError("trusted interpreter roots are invalid")

    venv = root / "venv"
    for directory, label in ((root, "app root"), (venv, "venv root"), (venv / "bin", "venv bin")):
        info = directory.stat(follow_symlinks=False)
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o022):
            raise ValueError(f"{label} is not owner-trusted")

    initial = venv / "bin/python"
    current = initial
    visited: set[tuple[int, int]] = set()
    for hop in range(_MAX_INTERPRETER_SYMLINKS + 1):
        info = current.lstat()
        identity = (info.st_dev, info.st_ino)
        if identity in visited:
            raise ValueError("installed Python symlink loop")
        visited.add(identity)
        if info.st_uid != uid:
            raise ValueError("installed Python hop has untrusted owner")
        if stat.S_ISLNK(info.st_mode):
            if hop == _MAX_INTERPRETER_SYMLINKS:
                raise ValueError("installed Python symlink chain is too deep")
            target = os.readlink(current)
            if not target or "\0" in target:
                raise ValueError("installed Python symlink target is invalid")
            candidate = Path(target) if os.path.isabs(target) else current.parent / target
            current = Path(os.path.normpath(os.fspath(candidate)))
            continue
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o022
                or stat.S_IMODE(info.st_mode) & 0o111 == 0):
            raise ValueError("installed Python final target must be regular executable and not writable")
        if not (_confined(current, venv) or any(_confined(current, item) for item in trusted_roots)):
            raise ValueError("installed Python final target is outside trusted interpreter roots")
        return initial
    raise ValueError("installed Python symlink chain is too deep")


def _run(
    argv: list[str], timeout: int, *, allow_bounded_stderr: bool = False,
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    started = time.monotonic()
    completed = subprocess.run(
        argv, check=False, text=True, capture_output=True, timeout=timeout,
        env=environment, shell=False,
    )
    elapsed = max(1, int((time.monotonic() - started) * 1000))
    stderr = completed.stderr.encode("utf-8")
    if completed.returncode != 0:
        raise ValueError("production command failed")
    if stderr and not allow_bounded_stderr:
        raise ValueError("production command failed")
    if len(stderr) > _MAX_UNITTEST_STDERR_BYTES:
        raise ValueError("production unittest stderr is oversized")
    result = {"passed": True, "duration_ms": elapsed,
              "command_digest": hashlib.sha256(json.dumps(argv, separators=(",", ":")).encode()).hexdigest()}
    if allow_bounded_stderr:
        result.update({"stderr_size": len(stderr), "stderr_digest": hashlib.sha256(stderr).hexdigest()})
    return result


def _production_tests(root: Path) -> dict[str, object]:
    python = _validated_interpreter(root)
    prefix = [str(python), "-I", "-B", "-c", _UNITTEST_BOOTSTRAP, str(root)]
    focused = [*prefix,
               "tests.test_webapp.WebAppTests.test_physical_draft_preview_api_is_paired_bounded_private_and_non_persistent", "-v"]
    full = [*prefix, "discover", "-s", "tests", "-v"]
    return {
        "focused": _run(focused, 900, allow_bounded_stderr=True, environment=_FIXED_TEST_ENV),
        "full": _run(full, 5400, allow_bounded_stderr=True, environment=_FIXED_TEST_ENV),
    }


def _canonical_transaction(root: Path, arguments: argparse.Namespace) -> tuple[dict[str, object], dict[str, bytes], Path]:
    runtime = root / "data/acceptance-runtime"
    runtime_info = runtime.stat(follow_symlinks=False)
    if (not stat.S_ISDIR(runtime_info.st_mode) or runtime.is_symlink()
            or runtime_info.st_uid != os.geteuid() or stat.S_IMODE(runtime_info.st_mode) & 0o077):
        raise ValueError("acceptance runtime is not owner-only")
    transactions = runtime / "transactions"
    attempt = transactions / arguments.attempt_id
    transaction = attempt / arguments.acceptance_operation_id
    for directory, label in ((transactions, "transaction root"), (attempt, "attempt transaction"),
                             (transaction, "acceptance operation transaction")):
        try:
            info = directory.stat(follow_symlinks=False)
        except FileNotFoundError as error:
            raise ValueError("transaction authority does not match this verification") from error
        if (not stat.S_ISDIR(info.st_mode) or directory.is_symlink()
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError(f"{label} is not canonical")
    raw = _read(transaction / "metadata.json", "transaction metadata", limit=8192)
    if stat.S_IMODE((transaction / "metadata.json").stat(follow_symlinks=False).st_mode) != 0o600:
        raise ValueError("transaction metadata is not owner-only")
    metadata = json.loads(raw)
    fields = {
        "schema", "attempt_id", "acceptance_operation_id", "evidence_id",
        "expected_source_sha", "expected_version",
        "mode", "nonce", "service_boot_id", "service_pid", "owner_generation",
        "owner_revision", "saved_revision", "draft_revision", "current_revision",
        "saved_sha256", "draft_sha256", "current_sha256", "width", "height",
        "foreign_rejected", "timeout_returned", "heartbeat_accepted", "rollback_complete",
        "sleep_observed",
    }
    canonical = (json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if type(metadata) is not dict or set(metadata) != fields or raw != canonical:
        raise ValueError("transaction metadata is noncanonical")
    boot_id = _read(Path("/proc/sys/kernel/random/boot_id"), "service boot identity", limit=64).decode("ascii").strip()
    if (metadata["schema"] != 1 or metadata["attempt_id"] != arguments.attempt_id
            or metadata["acceptance_operation_id"] != arguments.acceptance_operation_id
            or metadata["evidence_id"] != arguments.evidence_id
            or metadata["expected_source_sha"] != arguments.expected_sha
            or metadata["expected_version"] != arguments.expected_version
            or metadata["mode"] != arguments.mode or _HEX64.fullmatch(str(metadata["nonce"])) is None
            or metadata["service_boot_id"] != boot_id
            or type(metadata["service_pid"]) is not int or metadata["service_pid"] <= 0
            or not Path(f"/proc/{metadata['service_pid']}").is_dir()
            or type(metadata["owner_generation"]) is not int or metadata["owner_generation"] < 0
            or type(metadata["owner_revision"]) is not int or metadata["owner_revision"] < 0
            or metadata["width"] != 64 or metadata["height"] != 32
            or any(metadata[key] is not True for key in (
                "foreign_rejected", "timeout_returned", "heartbeat_accepted",
                "rollback_complete", "sleep_observed",
            ))):
        raise ValueError("transaction authority does not match this verification")
    revisions = [metadata[key] for key in ("saved_revision", "draft_revision", "current_revision")]
    if any(type(value) is not int for value in revisions) or not revisions[0] < revisions[1] < revisions[2]:
        raise ValueError("transaction frame revisions are invalid")
    frames = {}
    for kind in ("saved", "draft", "current"):
        path = transaction / f"{kind}.rgb"
        if stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) != 0o600:
            raise ValueError("transaction frame is not owner-only")
        frame = _read(path, f"transaction {kind} frame", limit=6144)
        digest = hashlib.sha256(frame).hexdigest()
        if len(frame) != 6144 or digest != metadata[f"{kind}_sha256"]:
            raise ValueError("transaction frame digest mismatch")
        frames[kind] = frame
    if frames["draft"] == frames["saved"] or frames["current"] != frames["saved"]:
        raise ValueError("transaction draft/saved semantics are invalid")
    return metadata, frames, transaction


def _claim(transaction: Path, name: str, nonce: str) -> None:
    descriptor = os.open(transaction / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        encoded = (nonce + "\n").encode("ascii")
        if os.write(descriptor, encoded) != len(encoded):
            raise OSError("short transaction claim write")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _physical(root: Path, arguments: argparse.Namespace) -> dict[str, object]:
    service = subprocess.run(
        ["/usr/bin/systemctl", "is-active", "matrix-web.service"],
        check=False,
        text=True,
        capture_output=True,
        timeout=15,
        shell=False,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )
    if service.returncode != 0 or service.stdout.strip() != "active" or service.stderr:
        raise ValueError("service is not active")
    metadata, frames, transaction = _canonical_transaction(root, arguments)
    _claim(transaction, "physical.verified", metadata["nonce"])
    return {"service_active": True, "width": 64, "height": 32,
            "draft_signature": metadata["draft_sha256"],
            "saved_signature": metadata["saved_sha256"],
            "owner_generation": metadata["owner_generation"],
            "foreign_rejected": metadata["foreign_rejected"],
            "timeout_returned": metadata["timeout_returned"],
            "sleep_observed": metadata["sleep_observed"]}


def _rollback(root: Path, arguments: argparse.Namespace) -> dict[str, object]:
    metadata, _frames, transaction = _canonical_transaction(root, arguments)
    verified = _read(transaction / "physical.verified", "physical verification claim", limit=65)
    if verified != (metadata["nonce"] + "\n").encode("ascii"):
        raise ValueError("physical verification claim does not match")
    _claim(transaction, "transaction.consumed", metadata["nonce"])
    return {"mode": arguments.mode, "saved_output": True,
            "saved_signature": metadata["saved_sha256"]}


def run(arguments: argparse.Namespace) -> dict[str, object]:
    if _HEX32.fullmatch(arguments.attempt_id) is None or _HEX64.fullmatch(arguments.evidence_id) is None:
        raise ValueError("acceptance identity is invalid")
    if (type(arguments.acceptance_operation_id) is not str
            or _HEX64.fullmatch(arguments.acceptance_operation_id) is None):
        raise ValueError("acceptance operation identity is invalid")
    root = Path(arguments.app_root)
    info = root.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or root.is_symlink() or not root.is_absolute():
        raise ValueError("app root is unsafe")
    if arguments.check == "protected-parity":
        result = _protected(root)
        if (result["source_sha"] != arguments.expected_sha
                or result["version"] != arguments.expected_version):
            raise ValueError("installed identity differs from expected candidate")
    elif arguments.check == "production-tests":
        result = _production_tests(root)
    elif arguments.check == "physical-journey":
        result = _physical(root, arguments)
    elif arguments.check == "persistent-state":
        result = {"config": _hash_tree(root / "data/config.json"),
                  "paired": _hash_tree(root / "data/paired-devices.json"),
                  "media": _hash_tree(root / "resources/media")}
    else:
        result = _rollback(root, arguments)
    return {"schema": 1, "check": arguments.check, "attempt_id": arguments.attempt_id,
            "acceptance_operation_id": arguments.acceptance_operation_id,
            "evidence_id": arguments.evidence_id, **result}


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        report = run(arguments)
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError, TypeError, SyntaxError, json.JSONDecodeError,
            UnicodeError, subprocess.SubprocessError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
