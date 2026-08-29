#!/usr/bin/env python3
"""Fail-closed physical acceptance for the Priority 1.5 B1 foundation."""

from __future__ import annotations

import argparse

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageSequence

_RELEASE_ROOT = Path(__file__).resolve().parents[1]
if str(_RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(_RELEASE_ROOT))

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_SERVICE_RE = re.compile(r"[A-Za-z0-9_.@-]+\.service")
_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+-preview")
_MANIFEST_KEYS = {"version", "boundary", "source_sha", "tree_sha", "fixtures"}
_FIXTURE_KEYS = {
    "path", "sha256", "size", "kind", "width", "height", "frames",
    "durations_ms",
}
_REPORT_KEYS = {
    "version", "source_sha", "tree_sha", "fixture_manifest_sha256",
    "expected_version", "service", "width", "height", "fit_sha256",
    "gif_delay_ms", "mp4_delay_ms", "cancelled_before_swap",
    "evidence_sha256",
}
_RATE_KEYS = {"10.0", "100.0", "400.0"}
_FIT_KEYS = {"contain", "cover", "stretch", "manual"}
_MAX_JSON_BYTES = 256 * 1024


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ) + "\n"
    ).encode("utf-8")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON contains duplicate fields")
        result[key] = value
    return result


def _load_canonical(path: Path, label: str) -> tuple[dict[str, object], bytes]:
    raw = path.read_bytes()
    if not raw or len(raw) > _MAX_JSON_BYTES:
        raise ValueError(f"{label} size is invalid")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is unreadable") from error
    if type(value) is not dict or canonical_json(value) != raw:
        raise ValueError(f"{label} is not canonical JSON")
    return value, raw


def _existing_regular(value: str, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{label} path must be absolute")
    try:
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except OSError as error:
        raise ValueError(f"{label} path is unavailable") from error
    if resolved != path or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} path must be a nonsymlink regular file")
    return path


def _output_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("report path must be absolute")
    parent = path.parent.resolve(strict=True)
    if parent != path.parent:
        raise ValueError("report parent must not contain symlinks")
    if path.exists() and (path.resolve(strict=True) != path or not path.is_file()):
        raise ValueError("report path must be a nonsymlink regular file")
    return path


def _fixture_path(root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative.startswith("fixtures/"):
        raise ValueError("fixture path is unsafe")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or candidate.as_posix() != relative:
        raise ValueError("fixture path is unsafe")
    path = root / candidate
    if path.resolve(strict=True) != path or not path.is_file():
        raise ValueError("fixture path is unsafe")
    return path


def load_fixture_manifest(path: Path, source_sha: str, tree_sha: str) -> dict[str, object]:
    manifest, _raw = _load_canonical(path, "fixture manifest")
    if set(manifest) != _MANIFEST_KEYS or manifest.get("version") != 1:
        raise ValueError("fixture manifest fields are not exact")
    if manifest.get("boundary") != "b1":
        raise ValueError("fixture manifest boundary mismatch")
    if manifest.get("source_sha") != source_sha or manifest.get("tree_sha") != tree_sha:
        raise ValueError("fixture manifest identity mismatch")
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("fixture manifest is empty")
    previous = ""
    for item in fixtures:
        if type(item) is not dict or set(item) != _FIXTURE_KEYS:
            raise ValueError("fixture fields are not exact")
        relative = item["path"]
        if not isinstance(relative, str) or relative <= previous:
            raise ValueError("fixtures are not strictly path-sorted")
        previous = relative
        fixture_path = _fixture_path(path.parent, relative)
        raw = fixture_path.read_bytes()
        if item["size"] != len(raw) or item["sha256"] != hashlib.sha256(raw).hexdigest():
            raise ValueError("fixture bytes do not match manifest")
        if item["kind"] not in {"gif", "mp4"}:
            raise ValueError("fixture kind is invalid")
        integers = (item["width"], item["height"], item["frames"])
        if any(type(number) is not int or not 1 <= number <= 10000 for number in integers):
            raise ValueError("fixture dimensions/frame count are invalid")
        durations = item["durations_ms"]
        if (
            not isinstance(durations, list) or len(durations) != item["frames"]
            or any(type(delay) is not int or not 20 <= delay <= 10000 for delay in durations)
        ):
            raise ValueError("fixture durations are invalid")
        if item["kind"] == "gif":
            with Image.open(fixture_path) as image:
                actual_durations = [
                    int(frame.info.get("duration", 100))
                    for frame in ImageSequence.Iterator(image)
                ]
                if image.size != (item["width"], item["height"]):
                    raise ValueError("GIF fixture dimensions mismatch")
                if len(actual_durations) != item["frames"] or actual_durations != durations:
                    raise ValueError("GIF fixture timeline mismatch")
    validated = dict(manifest)
    validated["_fixture_root"] = str(path.parent)
    return validated


def _default_version_reader() -> str:
    source = (_RELEASE_ROOT / "webapp.py").read_text(encoding="utf-8")
    match = re.search(r'^APPLICATION_VERSION = "([^"]+)"$', source, re.MULTILINE)
    if match is None:
        raise ValueError("deployed application version is unavailable")
    return match.group(1)


def _run_command(command: list[str]) -> object:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def _require_command(result: object, operation: str, expected_stdout: str | None = None) -> None:
    if getattr(result, "returncode", 1) != 0:
        raise RuntimeError(f"service {operation} failed")
    if expected_stdout is not None and getattr(result, "stdout", "") != expected_stdout:
        raise RuntimeError(f"service {operation} returned unexpected state")


def exercise_foundation(matrix: object, manifest: dict[str, object]) -> dict[str, object]:
    hardware = getattr(matrix, "matrix", None)
    if (getattr(hardware, "width", None), getattr(hardware, "height", None)) != (64, 32):
        raise ValueError("physical Matrix must be exactly 64x32")
    source = Image.new("RGB", (5, 3))
    source.putdata([
        ((x * 47 + y * 13) % 256, (x * 19 + y * 61) % 256,
         (x * 83 + y * 7) % 256)
        for y in range(3) for x in range(5)
    ])
    fits: dict[str, str] = {}
    for fit, geometry in (
        ("contain", {}), ("cover", {}), ("stretch", {}),
        ("manual", {"x": -2, "y": 30, "width": 7, "height": 9}),
    ):
        matrix.clear()
        matrix.draw_image(source, fit=fit, **geometry)
        matrix.swap()
        frame, _revision = matrix.latest_frame_rgb()
        if not isinstance(frame, bytes) or len(frame) != 64 * 32 * 3:
            raise ValueError("physical frame bytes are invalid")
        fits[fit] = hashlib.sha256(frame).hexdigest()

    from modules.media_player import gif_media_frames, video_media_frames

    gif_items = [item for item in manifest["fixtures"] if item["kind"] == "gif"]
    if not gif_items:
        raise ValueError("fixture manifest has no GIF timing authority")
    gif_path = Path(manifest["_fixture_root"]) / gif_items[0]["path"]
    gif_delays: dict[str, float] = {}
    mp4_delays: dict[str, float] = {}
    for label, rate in (("10.0", 0.1), ("100.0", 1.0), ("400.0", 4.0)):
        timeline = gif_media_frames(gif_path, loops=1, playback_rate=rate, bounded_pillow=False)
        candidate, delay = next(timeline)
        candidate.close()
        timeline.close()
        gif_delays[label] = delay * 1000
        _frame, video_delay = next(video_media_frames(
            lambda: iter((object(),)), fps=20, loops=1, playback_rate=rate,
        ))
        mp4_delays[label] = video_delay * 1000

    stop = threading.Event()
    stop.set()
    cancelled = list(video_media_frames(lambda: iter((object(),)), stop_event=stop)) == []
    return {
        "width": 64, "height": 32, "fit_sha256": fits,
        "gif_delay_ms": gif_delays, "mp4_delay_ms": mp4_delays,
        "cancelled_before_swap": cancelled,
    }


def _atomic_report(path: Path, raw: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def verify_report(value: object, source_sha: str, tree_sha: str) -> dict[str, object]:
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        if len(raw) > _MAX_JSON_BYTES:
            raise ValueError("physical report is oversized")
        report = json.loads(raw, object_pairs_hook=_unique_object)
        if canonical_json(report) != raw:
            raise ValueError("physical report is not canonical JSON")
    else:
        report = value
    if type(report) is not dict or set(report) != _REPORT_KEYS or report.get("version") != 1:
        raise ValueError("physical report fields are not exact")
    if report.get("source_sha") != source_sha or report.get("tree_sha") != tree_sha:
        raise ValueError("physical report identity mismatch")
    if report.get("width") != 64 or report.get("height") != 32:
        raise ValueError("physical report dimensions mismatch")
    if (
        not isinstance(report.get("fixture_manifest_sha256"), str)
        or _HASH_RE.fullmatch(report["fixture_manifest_sha256"]) is None
        or not isinstance(report.get("expected_version"), str)
        or _VERSION_RE.fullmatch(report["expected_version"]) is None
        or not isinstance(report.get("service"), str)
        or _SERVICE_RE.fullmatch(report["service"]) is None
    ):
        raise ValueError("physical report authority is invalid")
    if set(report.get("fit_sha256", {})) != _FIT_KEYS or any(
        not isinstance(value, str) or _HASH_RE.fullmatch(value) is None
        for value in report["fit_sha256"].values()
    ):
        raise ValueError("physical fit evidence is invalid")
    for field in ("gif_delay_ms", "mp4_delay_ms"):
        values = report.get(field)
        if type(values) is not dict or set(values) != _RATE_KEYS or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0 for value in values.values()
        ):
            raise ValueError("physical timing evidence is invalid")
    gif = report["gif_delay_ms"]
    mp4 = report["mp4_delay_ms"]
    if not (
        math.isclose(gif["10.0"], gif["100.0"] * 10, rel_tol=0, abs_tol=1e-9)
        and math.isclose(gif["400.0"], gif["100.0"] / 4, rel_tol=0, abs_tol=1e-9)
        and math.isclose(mp4["10.0"], 500.0, rel_tol=0, abs_tol=1e-9)
        and math.isclose(mp4["100.0"], 50.0, rel_tol=0, abs_tol=1e-9)
        and math.isclose(mp4["400.0"], 12.5, rel_tol=0, abs_tol=1e-9)
    ):
        raise ValueError("physical timing evidence changed exact cadence")
    if report.get("cancelled_before_swap") is not True:
        raise ValueError("physical cancellation evidence is invalid")
    digest = report.get("evidence_sha256")
    unsigned = dict(report)
    unsigned.pop("evidence_sha256")
    if not isinstance(digest, str) or digest != hashlib.sha256(canonical_json(unsigned)).hexdigest():
        raise ValueError("physical report digest mismatch")
    return report


def execute(
    arguments: argparse.Namespace, *, matrix_factory: Callable[[], object] | None = None,
    exercise: Callable[[object, dict[str, object]], dict[str, object]] = exercise_foundation,
    command_runner: Callable[[list[str]], object] = _run_command,
    version_reader: Callable[[], str] = _default_version_reader,
    lock_factory: Callable[[str], object] | None = None,
) -> dict[str, object]:
    if _SHA_RE.fullmatch(arguments.source_sha or "") is None:
        raise ValueError("source SHA is invalid")
    if _SHA_RE.fullmatch(arguments.tree_sha or "") is None:
        raise ValueError("tree SHA is invalid")
    if _SERVICE_RE.fullmatch(arguments.service or "") is None:
        raise ValueError("service name is invalid")
    if _VERSION_RE.fullmatch(arguments.expected_version or "") is None:
        raise ValueError("expected version is invalid")
    manifest_path = _existing_regular(arguments.fixture_manifest, "fixture manifest")
    lock_path = _existing_regular(arguments.lock_file, "lock file")
    lock_metadata = lock_path.stat()
    if lock_metadata.st_uid not in {0, os.geteuid()} or lock_metadata.st_mode & 0o022:
        raise ValueError("lock file ownership or mode is unsafe")
    report_path = _output_path(arguments.report)
    manifest = load_fixture_manifest(manifest_path, arguments.source_sha, arguments.tree_sha)
    if version_reader() != arguments.expected_version:
        raise ValueError("deployed application version mismatch")

    active = command_runner(["systemctl", "is-active", arguments.service])
    _require_command(active, "precheck", "active\n")
    stopped = False
    primary: BaseException | None = None
    report: dict[str, object] | None = None
    try:
        _require_command(command_runner(["systemctl", "stop", arguments.service]), "stop")
        stopped = True
        if lock_factory is None:
            from hardware_lock import DisplayOwnershipLock
            lock_factory = DisplayOwnershipLock
        with lock_factory(str(lock_path)):
            factory = matrix_factory
            if factory is None:
                from matrix_framework import Matrix
                factory = lambda: Matrix(rows=32, cols=64)
            evidence = exercise(factory(), manifest)
        report = {
            "version": 1, "source_sha": arguments.source_sha,
            "tree_sha": arguments.tree_sha,
            "fixture_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "expected_version": arguments.expected_version,
            "service": arguments.service, **evidence,
        }
        report["evidence_sha256"] = hashlib.sha256(canonical_json(report)).hexdigest()
        verify_report(report, arguments.source_sha, arguments.tree_sha)
        _atomic_report(report_path, canonical_json(report))
    except BaseException as error:
        primary = error
    finally:
        if stopped:
            try:
                _require_command(command_runner(["systemctl", "start", arguments.service]), "restore")
                _require_command(
                    command_runner(["systemctl", "is-active", arguments.service]),
                    "postcheck", "active\n",
                )
            except BaseException as restore_error:
                if primary is None:
                    primary = restore_error
                else:
                    primary.add_note(f"service restoration also failed: {restore_error}")
    if primary is not None:
        raise primary
    assert report is not None
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--tree-sha", required=True)
    parser.add_argument("--fixture-manifest", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--lock-file", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--report", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    execute(build_parser().parse_args(argv))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        print(f"priority-1.5 foundation physical verification failed: {error}", file=sys.stderr)
        raise SystemExit(2)
