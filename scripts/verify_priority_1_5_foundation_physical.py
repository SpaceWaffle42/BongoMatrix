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
from fractions import Fraction
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
_FFPROBE = "/usr/bin/ffprobe"
_SYSTEMCTL = "/usr/bin/systemctl"
_COMMAND_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
_MANIFEST_KEYS = {"version", "boundary", "source_sha", "tree_sha", "fixtures"}
_FIXTURE_KEYS = {
    "path", "sha256", "size", "kind", "width", "height", "frames",
    "durations_ms",
}
_REPORT_KEYS = {
    "version", "source_sha", "tree_sha", "fixture_manifest_sha256",
    "expected_version", "service", "width", "height", "fixture_cases",
    "cancellation", "evidence_sha256",
}
_RATE_KEYS = {"10.0", "100.0", "400.0"}
_GEOMETRY_KEYS = {"contain", "cover", "stretch", "manual-negative"}
_CASE_KEYS = {
    "kind", "width", "height", "frames", "durations_ms", "categories",
    "timing_ms", "frame_sha256", "loop_frame_sha256", "geometry_sha256",
}
_CANCELLATION_KEYS = {
    "fixture", "draws", "submissions", "prior_submission_count",
    "prior_revision", "prior_sha256", "history_unchanged",
}
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
        dimensions = (item["width"], item["height"])
        if (
            any(type(number) is not int or not 1 <= number <= 10000
                for number in dimensions)
            or type(item["frames"]) is not int or not 1 <= item["frames"] <= 120
        ):
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
        else:
            probe_result = subprocess.run(
                [
                    _FFPROBE, "-v", "error", "-select_streams", "v:0",
                    "-show_entries",
                    "stream=width,height,nb_frames,avg_frame_rate:frame=duration_time",
                    "-of", "json", str(fixture_path),
                ],
                check=False, capture_output=True, text=True, timeout=15, shell=False,
                env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            )
            if probe_result.returncode != 0 or len(probe_result.stdout) > 1024 * 1024:
                raise ValueError("MP4 fixture decoder mismatch")
            try:
                probe = json.loads(probe_result.stdout, object_pairs_hook=_unique_object)
                streams = probe["streams"]
                frames = probe["frames"]
                if type(streams) is not list or len(streams) != 1 or type(frames) is not list or not frames:
                    raise ValueError
                stream = streams[0]
                fallback = Fraction(1, 1) / Fraction(stream["avg_frame_rate"])
                actual_durations = []
                for frame in frames:
                    seconds = Fraction(frame.get("duration_time")) if frame.get("duration_time") else fallback
                    milliseconds = seconds * 1000
                    if milliseconds.denominator != 1:
                        raise ValueError
                    actual_durations.append(milliseconds.numerator)
                actual = (stream["width"], stream["height"], len(frames), actual_durations)
                if "nb_frames" in stream and int(stream["nb_frames"]) != len(frames):
                    raise ValueError
            except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
                raise ValueError("MP4 fixture decoder mismatch") from error
            if actual != (item["width"], item["height"], item["frames"], durations):
                raise ValueError("MP4 fixture metadata mismatch")

    fixtures_by_kind = {
        kind: [item for item in fixtures if item["kind"] == kind]
        for kind in ("gif", "mp4")
    }
    dimensions = [(item["width"], item["height"]) for item in fixtures]
    required_geometry = {
        "small": any(width < 64 and height < 32 for width, height in dimensions),
        "odd": any(width % 2 or height % 2 for width, height in dimensions),
        "portrait": any(width < height for width, height in dimensions),
        "landscape": any(width > height for width, height in dimensions),
        "oversized": any(width > 64 or height > 32 for width, height in dimensions),
    }
    if (
        any(not fixtures_by_kind[kind] for kind in fixtures_by_kind)
        or any(not any(item["frames"] >= 2 for item in fixtures_by_kind[kind])
               for kind in fixtures_by_kind)
        or any(len(set(item["durations_ms"])) != 1
               or item["durations_ms"][0] > 1000
               for item in fixtures_by_kind["mp4"])
        or len({delay for item in fixtures for delay in item["durations_ms"]}) < 2
        or not all(required_geometry.values())
    ):
        raise ValueError("fixture corpus lacks required GIF/MP4 timing or geometry diversity")
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
    return subprocess.run(
        command, text=True, capture_output=True, check=False, shell=False,
        env=_COMMAND_ENV,
    )


def _require_command(result: object, operation: str, expected_stdout: str | None = None) -> None:
    if getattr(result, "returncode", 1) != 0:
        raise RuntimeError(f"service {operation} failed")
    if expected_stdout is not None and getattr(result, "stdout", "") != expected_stdout:
        raise RuntimeError(f"service {operation} returned unexpected state")


def _fixture_categories(width: int, height: int) -> list[str]:
    categories = []
    if width < 64 and height < 32:
        categories.append("small")
    if width % 2 or height % 2:
        categories.append("odd")
    if width < height:
        categories.append("portrait")
    if width > height:
        categories.append("landscape")
    if width > 64 or height > 32:
        categories.append("oversized")
    return categories


def _frame_sha256(image: Image.Image) -> str:
    prepared = image.convert("RGB")
    try:
        return hashlib.sha256(prepared.tobytes("raw", "RGB")).hexdigest()
    finally:
        if prepared is not image:
            prepared.close()


def exercise_foundation(matrix: object, manifest: dict[str, object]) -> dict[str, object]:
    hardware = getattr(matrix, "matrix", None)
    if (getattr(hardware, "width", None), getattr(hardware, "height", None)) != (64, 32):
        raise ValueError("physical Matrix must be exactly 64x32")

    from modules.media_player import gif_media_frames, video_media_frames

    fixture_root = Path(manifest["_fixture_root"])
    fixtures = manifest["fixtures"]
    successful_history: list[tuple[str, int]] = []
    matrix.set_physical_swap_observer(lambda raw, revision: successful_history.append(
        (hashlib.sha256(raw).hexdigest(), revision)
    ))
    cases: dict[str, object] = {}

    for item in fixtures:
        relative = item["path"]
        path = fixture_root / relative
        timing: dict[str, list[float]] = {}
        native_frames: list[Image.Image] = []
        frame_hashes: list[str] = []
        loop_hashes: list[str] = []

        if item["kind"] == "gif":
            for label, rate in (("10.0", 0.1), ("100.0", 1.0), ("400.0", 4.0)):
                timeline = gif_media_frames(
                    path, loops=2, playback_rate=rate, bounded_pillow=False,
                )
                hashes: list[str] = []
                delays: list[float] = []
                try:
                    for index, (candidate, delay) in enumerate(timeline):
                        try:
                            hashes.append(_frame_sha256(candidate))
                            if index < item["frames"]:
                                delays.append(delay * 1000)
                                if label == "100.0":
                                    native_frames.append(candidate.copy().convert("RGB"))
                        finally:
                            candidate.close()
                finally:
                    timeline.close()
                if len(hashes) != item["frames"] * 2:
                    raise ValueError("GIF fixture did not execute two complete loops")
                timing[label] = delays
                if label == "100.0":
                    loop_hashes = hashes
                    frame_hashes = hashes[:item["frames"]]
        else:
            durations = item["durations_ms"]
            if len(set(durations)) != 1:
                raise ValueError("MP4 fixture cadence must be uniform")
            fps = 1000.0 / durations[0]
            native_frames = matrix.media_frames(
                relative, item["frames"], fps, preserve_size=True,
                resolved_path=path,
            )
            if len(native_frames) != item["frames"]:
                raise ValueError("MP4 fixture did not decode every manifested frame")
            for label, rate in (("10.0", 0.1), ("100.0", 1.0), ("400.0", 4.0)):
                timeline = video_media_frames(
                    lambda: iter(native_frames), fps=fps, loops=2,
                    playback_rate=rate,
                )
                hashes: list[str] = []
                delays: list[float] = []
                for index, (candidate, delay) in enumerate(timeline):
                    hashes.append(_frame_sha256(candidate))
                    if index < item["frames"]:
                        delays.append(delay * 1000)
                if len(hashes) != item["frames"] * 2:
                    raise ValueError("MP4 fixture did not execute two complete loops")
                timing[label] = delays
                if label == "100.0":
                    loop_hashes = hashes
                    frame_hashes = hashes[:item["frames"]]

        expected_timing = {
            label: [delay / rate for delay in item["durations_ms"]]
            for label, rate in (("10.0", 0.1), ("100.0", 1.0), ("400.0", 4.0))
        }
        if any(
            len(timing[label]) != len(expected_timing[label])
            or any(not math.isclose(actual, expected, rel_tol=0, abs_tol=1e-9)
                   for actual, expected in zip(timing[label], expected_timing[label]))
            for label in _RATE_KEYS
        ):
            raise ValueError("fixture timing changed native cadence")
        timing = expected_timing
        if item["frames"] >= 2 and len(set(frame_hashes)) < 2:
            raise ValueError("fixture lacks visible multi-frame edge progression")
        if loop_hashes[:item["frames"]] != loop_hashes[item["frames"]:]:
            raise ValueError("fixture loop progression is not repeatable")

        geometry_hashes: dict[str, str] = {}
        first = native_frames[0]
        for fit, geometry in (
            ("contain", {}), ("cover", {}), ("stretch", {}),
            ("manual-negative", {
                "fit": "manual", "x": -max(1, first.width // 2),
                "y": -max(1, first.height // 2),
                "width": first.width, "height": first.height,
            }),
        ):
            matrix.clear()
            matrix.draw_image(first, fit=geometry.pop("fit", fit), **geometry)
            matrix.swap()
            frame, _revision = matrix.latest_frame_rgb()
            if not isinstance(frame, bytes) or len(frame) != 64 * 32 * 3:
                raise ValueError("physical frame bytes are invalid")
            geometry_hashes[fit] = hashlib.sha256(frame).hexdigest()

        cases[relative] = {
            "kind": item["kind"], "width": item["width"],
            "height": item["height"], "frames": item["frames"],
            "durations_ms": list(item["durations_ms"]),
            "categories": _fixture_categories(item["width"], item["height"]),
            "timing_ms": timing, "frame_sha256": frame_hashes,
            "loop_frame_sha256": loop_hashes,
            "geometry_sha256": geometry_hashes,
        }
        for candidate in native_frames:
            candidate.close()

    cancellation: dict[str, object] = {}
    for item in fixtures:
        stop = threading.Event()
        draws = 0
        original_set_image = matrix._frame_capture.set_image
        original_media_file = matrix._media_file

        def cancel_after_draw(*args: object, **kwargs: object) -> object:
            nonlocal draws
            result = original_set_image(*args, **kwargs)
            draws += 1
            stop.set()
            return result

        before_frame, before_revision = matrix.latest_frame_rgb()
        before_history = list(successful_history)
        matrix._frame_capture.set_image = cancel_after_draw
        matrix._media_file = lambda filename: (
            fixture_root / item["path"] if filename == item["path"] else None
        )
        try:
            if item["kind"] == "gif":
                shown = matrix.show_gif(item["path"], loops=1, stop_event=stop)
            else:
                shown = matrix.show_video(
                    item["path"], loops=1,
                    fps_override=1000.0 / item["durations_ms"][0],
                    stop_event=stop,
                )
        finally:
            matrix._frame_capture.set_image = original_set_image
            matrix._media_file = original_media_file
        after_frame, after_revision = matrix.latest_frame_rgb()
        unchanged = (
            successful_history == before_history
            and (after_frame, after_revision) == (before_frame, before_revision)
        )
        if shown or not stop.is_set() or draws != 1 or not unchanged:
            raise ValueError("post-draw pre-framebuffer cancellation was not preserved")
        cancellation[item["path"]] = {
            "fixture": item["path"], "draws": draws,
            "submissions": len(successful_history) - len(before_history),
            "prior_submission_count": len(before_history),
            "prior_revision": before_revision,
            "prior_sha256": hashlib.sha256(before_frame).hexdigest(),
            "history_unchanged": unchanged,
        }

    return {
        "width": 64, "height": 32, "fixture_cases": cases,
        "cancellation": cancellation,
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


def verify_report(
    value: object, source_sha: str, tree_sha: str, manifest: dict[str, object],
) -> dict[str, object]:
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        if len(raw) > _MAX_JSON_BYTES:
            raise ValueError("physical report is oversized")
        report = json.loads(raw, object_pairs_hook=_unique_object)
        if canonical_json(report) != raw:
            raise ValueError("physical report is not canonical JSON")
    else:
        report = value
    if type(report) is not dict or set(report) != _REPORT_KEYS or report.get("version") != 2:
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

    expected_items = {item["path"]: item for item in manifest["fixtures"]}
    cases = report.get("fixture_cases")
    if type(cases) is not dict or set(cases) != set(expected_items):
        raise ValueError("physical fixture evidence omits or adds manifest entries")
    for relative, item in expected_items.items():
        case = cases[relative]
        if type(case) is not dict or set(case) != _CASE_KEYS:
            raise ValueError("physical fixture case fields are not exact")
        if (
            case["kind"] != item["kind"] or case["width"] != item["width"]
            or case["height"] != item["height"] or case["frames"] != item["frames"]
            or case["durations_ms"] != item["durations_ms"]
            or case["categories"] != _fixture_categories(item["width"], item["height"])
        ):
            raise ValueError("physical fixture case does not attest its manifest entry")
        timing = case["timing_ms"]
        if type(timing) is not dict or set(timing) != _RATE_KEYS:
            raise ValueError("physical fixture timing rates are incomplete")
        for label, divisor in (("10.0", 0.1), ("100.0", 1.0), ("400.0", 4.0)):
            expected = [delay / divisor for delay in item["durations_ms"]]
            values = timing[label]
            if (
                type(values) is not list or len(values) != item["frames"]
                or any(isinstance(number, bool) or not isinstance(number, (int, float))
                       or not math.isfinite(number) or number <= 0 for number in values)
                or any(not math.isclose(actual, wanted, rel_tol=0, abs_tol=1e-9)
                       for actual, wanted in zip(values, expected))
            ):
                raise ValueError("physical fixture timing changed native cadence")
        frames = case["frame_sha256"]
        loops = case["loop_frame_sha256"]
        if (
            type(frames) is not list or len(frames) != item["frames"]
            or type(loops) is not list or len(loops) != item["frames"] * 2
            or any(type(digest) is not str or _HASH_RE.fullmatch(digest) is None
                   for digest in frames + loops)
            or item["frames"] >= 2 and len(set(frames)) < 2
            or loops[:item["frames"]] != frames
            or loops[item["frames"]:] != frames
        ):
            raise ValueError("physical fixture loop or edge progression is invalid")
        geometry = case["geometry_sha256"]
        if type(geometry) is not dict or set(geometry) != _GEOMETRY_KEYS or any(
            type(digest) is not str or _HASH_RE.fullmatch(digest) is None
            for digest in geometry.values()
        ):
            raise ValueError("physical fixture geometry evidence is invalid")

    cancellation = report.get("cancellation")
    if type(cancellation) is not dict or set(cancellation) != set(expected_items):
        raise ValueError("physical cancellation fixture matrix is incomplete")
    for relative, evidence in cancellation.items():
        if type(evidence) is not dict or set(evidence) != _CANCELLATION_KEYS:
            raise ValueError("physical cancellation fields are not exact")
        fixture = expected_items[relative]
        if (
            evidence["fixture"] != relative
            or evidence["draws"] != 1 or evidence["submissions"] != 0
            or type(evidence["prior_submission_count"]) is not int
            or evidence["prior_submission_count"] < 1
            or type(evidence["prior_revision"]) is not int
            or evidence["prior_revision"] < 1
            or type(evidence["prior_sha256"]) is not str
            or _HASH_RE.fullmatch(evidence["prior_sha256"]) is None
            or evidence["history_unchanged"] is not True
        ):
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

    active = command_runner([_SYSTEMCTL, "is-active", arguments.service])
    _require_command(active, "precheck", "active\n")
    stopped = False
    primary: BaseException | None = None
    report: dict[str, object] | None = None
    try:
        _require_command(command_runner([_SYSTEMCTL, "stop", arguments.service]), "stop")
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
            "version": 2, "source_sha": arguments.source_sha,
            "tree_sha": arguments.tree_sha,
            "fixture_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "expected_version": arguments.expected_version,
            "service": arguments.service, **evidence,
        }
        report["evidence_sha256"] = hashlib.sha256(canonical_json(report)).hexdigest()
        verify_report(report, arguments.source_sha, arguments.tree_sha, manifest)
        _atomic_report(report_path, canonical_json(report))
    except BaseException as error:
        primary = error
    finally:
        if stopped:
            try:
                _require_command(command_runner([_SYSTEMCTL, "start", arguments.service]), "restore")
                _require_command(
                    command_runner([_SYSTEMCTL, "is-active", arguments.service]),
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
