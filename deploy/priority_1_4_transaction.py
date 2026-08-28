#!/usr/bin/env python3
"""Couple Priority 1.4 deployment to its mutable legacy-config authority."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

_RELEASE_ROOT = Path(__file__).resolve().parents[1]
if str(_RELEASE_ROOT) not in sys.path:
    # `python -I /release/deploy/priority_1_4_transaction.py` intentionally
    # excludes the working directory.  Bind imports to this frozen release.
    sys.path.insert(0, str(_RELEASE_ROOT))

try:
    from deploy import release_bundle
except ImportError:  # Exact staged execution places this helper below release/.
    import release_bundle  # type: ignore[no-redef]

from release_argparse import add_store_once_argument


_SHA_RE = re.compile(r"[0-9a-f]{40}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_SERVICE_RE = re.compile(r"[A-Za-z0-9@_.:-]+\.service")
_STATE_KEYS = {"version", "source_sha", "config", "paired", "media"}
_CONFIG_KEYS = {"path", "sha256", "uid", "gid", "mode", "schema_version", "revision"}
_OPTIONAL_HASH_KEYS = {"path", "present", "sha256"}
_MEDIA_KEYS = {"path", "tree_sha256"}
_STATE_FILES = {"config-pre-migration.json", "state-manifest.json"}
_SYSTEMCTL = "/usr/bin/systemctl"


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("canonical state contains duplicate fields")
        result[key] = value
    return result


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_regular(path: Path) -> tuple[bytes, os.stat_result]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"required regular file is unsafe: {path}")
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ValueError(f"required regular file changed identity: {path}")
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks), opened
    finally:
        os.close(descriptor)


def _atomic_restore(path: Path, data: bytes, uid: int, gid: int, mode: str) -> None:
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("config parent is unsafe")
    temporary = parent / f".priority-1-4-{os.getpid()}-{secrets.token_hex(8)}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    published = False
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short config snapshot write")
            view = view[written:]
        os.fchmod(descriptor, int(mode, 8))
        os.fchown(descriptor, uid, gid)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        published = True
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not published:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _tree_hash(root: Path) -> str:
    if not root.is_dir() or root.is_symlink():
        raise ValueError("media root must be a real directory")
    records: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if path.is_symlink() or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise ValueError("media tree contains unsafe content")
        if stat.S_ISDIR(metadata.st_mode):
            records.append({"path": relative, "type": "directory"})
        else:
            data, opened = _read_regular(path)
            records.append({
                "path": relative, "type": "file", "sha256": _sha(data),
                "mode": f"{stat.S_IMODE(opened.st_mode):04o}",
            })
    return _sha(_canonical(records))


def _paired_record(path: Path) -> dict[str, object]:
    try:
        data, _metadata = _read_regular(path)
    except FileNotFoundError:
        return {"path": "data/paired-devices.json", "present": False, "sha256": None}
    return {"path": "data/paired-devices.json", "present": True, "sha256": _sha(data)}


def _config_record(path: Path) -> tuple[bytes, dict[str, object]]:
    data, metadata = _read_regular(path)
    try:
        parsed = json.loads(data, object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("config snapshot is not valid JSON") from error
    if type(parsed) is not dict:
        raise ValueError("prepare requires an exact legacy config object")
    schema_version = parsed.get("schema_version", 0)
    if type(schema_version) is not int or schema_version not in (0, 1):
        raise ValueError("prepare requires an exact schema-v0 or schema-v1 config")
    revision = parsed.get("revision")
    if type(revision) is not int or revision < 0:
        raise ValueError("config revision is invalid")
    return data, {
        "path": "data/config.json", "sha256": _sha(data),
        "uid": metadata.st_uid, "gid": metadata.st_gid,
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "schema_version": schema_version, "revision": revision,
    }


def _snapshot_mutable(destination: Path) -> tuple[bytes, dict[str, object], dict[str, object], dict[str, object]]:
    config, config_record = _config_record(destination / "data/config.json")
    paired = _paired_record(destination / "data/paired-devices.json")
    media = {"path": "resources/media", "tree_sha256": _tree_hash(destination / "resources/media")}
    return config, config_record, paired, media


@dataclass(frozen=True)
class TransactionPaths:
    source_sha: str
    stage: Path
    destination_root: Path
    source_bundle: Path
    state_bundle: Path
    service_name: str
    health_url: str
    health_status: int
    health_timeout_seconds: int

    def __post_init__(self) -> None:
        for name in ("stage", "destination_root", "source_bundle", "state_bundle"):
            value = Path(getattr(self, name))
            if not value.is_absolute():
                raise ValueError(f"{name} must be absolute")
            object.__setattr__(self, name, value)
        if _SHA_RE.fullmatch(self.source_sha) is None:
            raise ValueError("source SHA must be an exact lowercase Git SHA")
        if _SERVICE_RE.fullmatch(self.service_name) is None:
            raise ValueError("service name is invalid")
        parsed = urllib.parse.urlsplit(self.health_url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port != 5000:
            raise ValueError("health URL must be exact loopback HTTP on port 5000")
        if type(self.health_status) is not int or not 100 <= self.health_status <= 599:
            raise ValueError("health status is invalid")
        if type(self.health_timeout_seconds) is not int or not 1 <= self.health_timeout_seconds <= 60:
            raise ValueError("health timeout is invalid")
        if self.source_bundle == self.state_bundle:
            raise ValueError("source and mutable-state authorities must be separate")

    @property
    def release_root(self) -> Path:
        return self.stage / "release"

    @property
    def staged_tool(self) -> Path:
        return self.stage / "release_bundle.py"

    @property
    def release_spec(self) -> Path:
        return self.stage / "release-spec.json"

    @property
    def release_manifest(self) -> Path:
        return self.source_bundle / "release-manifest.json"

    @property
    def backup_manifest(self) -> Path:
        return self.source_bundle / "backup-manifest.json"


class SystemdHealthService:
    def __init__(self, paths: TransactionPaths):
        self.paths = paths

    def _run(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            (_SYSTEMCTL, *arguments, self.paths.service_name), check=check,
            text=True, capture_output=True, timeout=10, shell=False,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
        )

    def is_active(self) -> bool:
        result = self._run("is-active", "--quiet", check=False)
        if result.returncode not in (0, 3):
            raise RuntimeError("service active-state check failed")
        return result.returncode == 0

    def stop(self) -> None:
        self._run("stop")
        if self.is_active():
            raise RuntimeError("service remains active after stop")

    def start(self) -> None:
        self._run("start")

    def daemon_reload_needed(self) -> bool:
        return True

    def daemon_reload(self) -> None:
        subprocess.run(
            (_SYSTEMCTL, "daemon-reload"), check=True, text=True, capture_output=True,
            timeout=10, shell=False,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
        )

    def run_health_checks(self) -> None:
        deadline = time.monotonic() + self.paths.health_timeout_seconds
        parsed = urllib.parse.urlsplit(self.paths.health_url)
        while time.monotonic() < deadline:
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
            try:
                connection.request("GET", parsed.path or "/")
                response = connection.getresponse()
                response.read()
                if response.status == self.paths.health_status and self.is_active():
                    return
            except OSError:
                pass
            finally:
                connection.close()
            time.sleep(0.1)
        raise RuntimeError("service readiness deadline expired")

    def recovery_commands(self) -> tuple[str, ...]:
        return ()


class Priority14Transaction:
    def __init__(
        self,
        paths: TransactionPaths,
        *,
        release_api: object = release_bundle,
        service: object | None = None,
        physical_factory: Callable[[], object] | None = None,
    ):
        self.paths = paths
        self.release_api = release_api
        self.service = service or SystemdHealthService(paths)
        self.physical_factory = physical_factory

    def _validate_stage(self) -> None:
        p = self.paths
        for path in (p.stage, p.release_root, p.destination_root):
            if not path.is_dir() or path.is_symlink():
                raise ValueError("stage and destination roots must be real directories")
        self.release_api.verify_staged_release(p.staged_tool, p.release_root, p.release_spec)
        marker_data, _metadata = _read_regular(p.release_root / ".matrix-release.json")
        try:
            marker = json.loads(marker_data, object_pairs_hook=_unique_object)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("installed release identity is unreadable") from error
        if type(marker) is not dict or marker.get("source_sha") != p.source_sha:
            raise ValueError("stage source SHA does not match requested source")
        files = marker.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError("stage installed manifest is incomplete")
        names = []
        for entry in files:
            if type(entry) is not dict or set(entry) != {"path", "sha256"}:
                raise ValueError("stage installed file record is invalid")
            relative = entry["path"]
            if not isinstance(relative, str) or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
                raise ValueError("stage installed path is invalid")
            data, _ = _read_regular(p.release_root.joinpath(*PurePosixPath(relative).parts))
            if entry["sha256"] != _sha(data):
                raise ValueError("stage installed bytes changed")
            names.append(relative)
        if names != sorted(set(names)):
            raise ValueError("stage installed file names are duplicate or unordered")

    def _load_state(self) -> tuple[dict[str, object], bytes]:
        p = self.paths
        metadata = p.state_bundle.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != os.geteuid() or metadata.st_gid != os.getegid()
        ):
            raise ValueError("state bundle must be root-owned mode 0700")
        if {path.name for path in p.state_bundle.iterdir()} != _STATE_FILES:
            raise ValueError("state bundle scope is not exact")
        raw, state_stat = _read_regular(p.state_bundle / "state-manifest.json")
        config, config_stat = _read_regular(p.state_bundle / "config-pre-migration.json")
        if stat.S_IMODE(state_stat.st_mode) != 0o400 or stat.S_IMODE(config_stat.st_mode) != 0o400:
            raise ValueError("state authority files must be mode 0400")
        try:
            manifest = json.loads(raw, object_pairs_hook=_unique_object)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("state manifest is unreadable") from error
        if _canonical(manifest) != raw or type(manifest) is not dict or set(manifest) != _STATE_KEYS:
            raise ValueError("state manifest is not exact canonical JSON")
        if manifest["version"] != 1 or manifest["source_sha"] != p.source_sha:
            raise ValueError("state manifest identity mismatch")
        config_record = manifest["config"]
        paired = manifest["paired"]
        media = manifest["media"]
        if type(config_record) is not dict or set(config_record) != _CONFIG_KEYS:
            raise ValueError("state config record is invalid")
        if type(paired) is not dict or set(paired) != _OPTIONAL_HASH_KEYS:
            raise ValueError("state paired record is invalid")
        if type(media) is not dict or set(media) != _MEDIA_KEYS:
            raise ValueError("state media record is invalid")
        if (
            config_record["sha256"] != _sha(config)
            or type(config_record["schema_version"]) is not int
            or config_record["schema_version"] not in (0, 1)
        ):
            raise ValueError("state config bytes mismatch")
        if not isinstance(config_record["sha256"], str) or _HASH_RE.fullmatch(config_record["sha256"]) is None:
            raise ValueError("state config hash is invalid")
        self._assert_mutable_hashes(manifest)
        return manifest, config

    def _assert_mutable_hashes(self, manifest: dict[str, object]) -> None:
        current_paired = _paired_record(self.paths.destination_root / "data/paired-devices.json")
        current_media = {"path": "resources/media", "tree_sha256": _tree_hash(self.paths.destination_root / "resources/media")}
        if current_paired != manifest["paired"] or current_media != manifest["media"]:
            raise ValueError("paired-device or media state changed during transaction")

    def prepare(self) -> dict[str, object]:
        p = self.paths
        self._validate_stage()
        if p.source_bundle.exists() or p.state_bundle.exists():
            raise ValueError("prepare authorities must be fresh")
        before = _snapshot_mutable(p.destination_root)
        try:
            self.release_api.backup_release(
                p.staged_tool, p.release_root, p.destination_root, p.source_bundle,
                p.release_manifest, p.backup_manifest, p.release_spec,
                owner_uid=os.geteuid(), owner_gid=os.getegid(),
            )
            after = _snapshot_mutable(p.destination_root)
            if after != before:
                raise ValueError("mutable state changed while source/config authority was prepared")
            config, config_record, paired, media = before
            p.state_bundle.mkdir(mode=0o700)
            os.chmod(p.state_bundle, 0o700)
            os.chown(p.state_bundle, os.geteuid(), os.getegid())
            config_path = p.state_bundle / "config-pre-migration.json"
            config_path.write_bytes(config)
            os.chmod(config_path, 0o400)
            manifest = {
                "version": 1, "source_sha": p.source_sha, "config": config_record,
                "paired": paired, "media": media,
            }
            state_path = p.state_bundle / "state-manifest.json"
            state_path.write_bytes(_canonical(manifest))
            os.chmod(state_path, 0o400)
            directory_fd = os.open(p.state_bundle, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                for path in (config_path, state_path):
                    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            self._load_state()
            return manifest
        except BaseException:
            if p.state_bundle.exists():
                import shutil
                shutil.rmtree(p.state_bundle, ignore_errors=True)
            raise

    def preflight(self) -> None:
        self._validate_stage()
        self._load_state()
        p = self.paths
        self.release_api.preflight_release(
            p.staged_tool, p.release_root, p.destination_root, p.source_bundle,
            p.release_manifest, p.backup_manifest,
        )

    def _stop(self) -> None:
        if self.service.is_active():
            self.service.stop()
        if self.service.is_active():
            raise RuntimeError("service remains active before transaction mutation")

    def _start_ready(self) -> None:
        self.service.start()
        if not self.service.is_active():
            raise RuntimeError("service did not start")
        self.service.run_health_checks()

    def _restore_legacy_config(self, manifest: dict[str, object], config: bytes) -> None:
        record = manifest["config"]
        if not isinstance(record, dict):
            raise ValueError("state config record is invalid")
        _atomic_restore(
            self.paths.destination_root / "data/config.json", config,
            record["uid"], record["gid"], record["mode"],
        )
        restored, metadata = _read_regular(self.paths.destination_root / "data/config.json")
        if (
            _sha(restored) != record["sha256"] or metadata.st_uid != record["uid"]
            or metadata.st_gid != record["gid"]
            or f"{stat.S_IMODE(metadata.st_mode):04o}" != record["mode"]
        ):
            raise ValueError("legacy config restoration failed")

    def _source_phase(self) -> str:
        """Classify an exact candidate or rollback source; reject mixed state."""

        p = self.paths
        matches: list[str] = []
        for phase, mode in (("candidate", "deploy"), ("rollback", "rollback")):
            try:
                self.release_api.verify_release(
                    p.staged_tool, p.release_root, p.destination_root, p.source_bundle,
                    p.release_manifest, p.backup_manifest, mode=mode,
                )
            except BaseException:
                continue
            matches.append(phase)
        if len(matches) != 1:
            raise ValueError("rollback recovery source state is mixed or ambiguous")
        return matches[0]

    def _deploy(self) -> None:
        self.preflight()
        manifest, config = self._load_state()
        p = self.paths
        mutation_started = False
        try:
            self._stop()
            mutation_started = True
            self.release_api.deploy_release(
                p.staged_tool, p.release_root, p.destination_root, p.source_bundle,
                p.release_manifest, p.backup_manifest, service=self.service,
            )
            self._start_ready()
            self._assert_mutable_hashes(manifest)
        except BaseException as primary:
            if not mutation_started:
                try:
                    self._start_ready()
                except BaseException as recovery_error:
                    raise RuntimeError(
                        f"deployment stop failed and service recovery failed: {recovery_error}"
                    ) from primary
                raise
            recovery_error = None
            try:
                self._stop()
                self.release_api.recover_release(
                    p.staged_tool, p.release_root, p.destination_root, p.source_bundle,
                    p.release_manifest, p.backup_manifest,
                    service=self.service, initially_active=False,
                )
                self._restore_legacy_config(manifest, config)
                self._start_ready()
                self._assert_mutable_hashes(manifest)
            except BaseException as error:
                recovery_error = error
                try:
                    self._stop()
                except BaseException:
                    pass
            if recovery_error is not None:
                raise RuntimeError(f"deployment failed and coherent rollback failed: {recovery_error}") from primary
            raise

    def deploy(self) -> None:
        self._deploy()

    def redeploy(self) -> None:
        self._deploy()

    def rollback(self, *, after_boundary: Callable[[str], None] | None = None) -> None:
        self._validate_stage()
        manifest, config = self._load_state()
        p = self.paths
        candidate, candidate_stat = _read_regular(p.destination_root / "data/config.json")
        mutation_started = False
        try:
            self._stop()
            mutation_started = True
            self.release_api.rollback_release(
                p.release_root, p.destination_root, p.source_bundle,
                p.release_manifest, p.backup_manifest, service=self.service,
            )
            if after_boundary is not None:
                after_boundary("source-restored")
            self._restore_legacy_config(manifest, config)
            if after_boundary is not None:
                after_boundary("config-restored")
            self._start_ready()
            self._assert_mutable_hashes(manifest)
        except BaseException as primary:
            if not mutation_started:
                try:
                    self._start_ready()
                except BaseException as recovery_error:
                    raise RuntimeError(
                        f"rollback stop failed and service recovery failed: {recovery_error}"
                    ) from primary
                raise
            recovery_error = None
            try:
                if self.service.is_active():
                    self._stop()
                source_phase = self._source_phase()
                if source_phase == "candidate":
                    _atomic_restore(
                        p.destination_root / "data/config.json", candidate,
                        candidate_stat.st_uid, candidate_stat.st_gid,
                        f"{stat.S_IMODE(candidate_stat.st_mode):04o}",
                    )
                else:
                    self._restore_legacy_config(manifest, config)
                self._start_ready()
                self._assert_mutable_hashes(manifest)
            except BaseException as error:
                recovery_error = error
                try:
                    self._stop()
                except BaseException:
                    pass
            if recovery_error is not None:
                raise RuntimeError(f"rollback failed and candidate recovery failed: {recovery_error}") from primary
            raise

    def verify(self, phase: str) -> None:
        if phase not in {"candidate", "rollback", "final"}:
            raise ValueError("verify phase must be candidate, rollback, or final")
        self._validate_stage()
        manifest, config = self._load_state()
        p = self.paths
        mode = "rollback" if phase == "rollback" else "deploy"
        self.release_api.verify_release(
            p.staged_tool, p.release_root, p.destination_root, p.source_bundle,
            p.release_manifest, p.backup_manifest, mode=mode,
        )
        current, metadata = _read_regular(p.destination_root / "data/config.json")
        record = manifest["config"]
        if not isinstance(record, dict):
            raise ValueError("state config record is invalid")
        if phase == "rollback":
            if current != config or (
                metadata.st_uid != record["uid"] or metadata.st_gid != record["gid"]
                or f"{stat.S_IMODE(metadata.st_mode):04o}" != record["mode"]
            ):
                raise ValueError("rollback config does not match immutable legacy authority")
        else:
            try:
                parsed = json.loads(current, object_pairs_hook=_unique_object)
            except (UnicodeError, json.JSONDecodeError) as error:
                raise ValueError("deployed config is unreadable") from error
            if (
                type(parsed) is not dict or parsed.get("schema_version") != 2
                or parsed.get("revision") != record["revision"] + 1
            ):
                raise ValueError("candidate config did not migrate exactly once to schema v2")
        if stat.S_IMODE(p.destination_root.lstat().st_mode) != 0o750:
            raise ValueError("destination root mode is not 0750")
        if not self.service.is_active():
            raise ValueError("service is inactive during verification")
        self.service.run_health_checks()
        self._assert_mutable_hashes(manifest)

    def physical(self, phase: str, display_lock: Path, width: int, height: int) -> dict[str, object]:
        if phase not in {"candidate", "final"}:
            raise ValueError("physical phase must be candidate or final")
        if (width, height) != (64, 32):
            raise ValueError("physical evidence requires exact 64x32 dimensions")
        self.verify(phase)
        from scripts import verify_priority_1_4_physical as physical
        from matrix_framework import Matrix
        descriptor = None
        try:
            self._stop()
            metadata = display_lock.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ValueError("display lock is unsafe")
            descriptor = os.open(display_lock, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            matrix = self.physical_factory() if self.physical_factory else Matrix(rows=height, cols=width)
            report = physical.exercise(
                matrix, self.paths.source_sha,
                deadline_seconds=min(30, self.paths.health_timeout_seconds),
            )
            physical.verify_report(report, self.paths.source_sha)
            return report
        finally:
            if descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
            self._start_ready()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "preflight", "deploy", "verify", "physical", "rollback", "redeploy"):
        subparser = commands.add_parser(command)
        for option in ("--source-sha", "--stage", "--destination-root", "--source-bundle", "--state-bundle", "--service", "--health-url"):
            add_store_once_argument(subparser, option, required=True)
        add_store_once_argument(subparser, "--health-status", type=int, required=True)
        add_store_once_argument(subparser, "--health-timeout-seconds", type=int, required=True)
        if command in {"verify", "physical"}:
            add_store_once_argument(subparser, "--phase", choices=("candidate", "rollback", "final"), required=True)
        if command == "physical":
            add_store_once_argument(subparser, "--display-lock", required=True)
            add_store_once_argument(subparser, "--width", type=int, required=True)
            add_store_once_argument(subparser, "--height", type=int, required=True)
    return parser


def _paths(arguments: argparse.Namespace) -> TransactionPaths:
    return TransactionPaths(
        source_sha=arguments.source_sha, stage=Path(arguments.stage),
        destination_root=Path(arguments.destination_root),
        source_bundle=Path(arguments.source_bundle), state_bundle=Path(arguments.state_bundle),
        service_name=arguments.service, health_url=arguments.health_url,
        health_status=arguments.health_status,
        health_timeout_seconds=arguments.health_timeout_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    transaction = Priority14Transaction(_paths(arguments))
    command = arguments.command
    if command == "prepare":
        transaction.prepare()
    elif command == "preflight":
        transaction.preflight()
    elif command == "deploy":
        transaction.deploy()
    elif command == "rollback":
        transaction.rollback()
    elif command == "redeploy":
        transaction.redeploy()
    elif command == "verify":
        transaction.verify(arguments.phase)
    else:
        report = transaction.physical(
            arguments.phase, Path(arguments.display_lock), arguments.width, arguments.height,
        )
        print(_canonical(report).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, release_bundle.ReleaseManifestError) as error:
        print(f"priority-1.4 transaction failed: {error}", file=__import__("sys").stderr)
        raise SystemExit(2)
