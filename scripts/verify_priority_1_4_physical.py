#!/usr/bin/env python3
"""Produce and verify deterministic Priority 1.4 physical 64x32 evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

_RELEASE_ROOT = Path(__file__).resolve().parents[1]
if str(_RELEASE_ROOT) not in sys.path:
    # Keep the direct `python -I .../scripts/...py` CLI bound to its release.
    sys.path.insert(0, str(_RELEASE_ROOT))

from modules import custom_text


_SHA_RE = re.compile(r"[0-9a-f]{40}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_MAX_REPORT_BYTES = 256 * 1024
_MAX_FRAMES = 160
_REPORT_KEYS = {
    "version", "source_sha", "width", "height", "fixture_digest",
    "fixtures", "cancellation", "evidence_digest",
}
_FIXTURE_KEYS = {
    "name", "kind", "direction", "border", "background", "border_colour",
    "frame_count", "delay_ms", "frame_sha256", "relative_revisions",
    "pixels", "foreground_pixels_max",
}
_PIXEL_KEYS = {"first", "last"}
_CANCELLATION_KEYS = {"attempted", "published", "revision_before", "revision_after"}


def _fixture(name: str, kind: str, border: str, direction: str = "left") -> dict[str, Any]:
    return {
        "name": name,
        "config": {
            "id": f"custom_{name}", "name": name, "type": kind,
            "text": "EDGE", "font": "5x7", "colour": "#fedcba",
            "background": "#123456", "border": border,
            "border_colour": "#abcdef", "x": -2, "y": 7,
            "direction": direction, "speed": 0.001, "loops": 1,
        },
        "duration": 0.001,
    }


_FIXTURES = (
    _fixture("static_none", "text", "none"),
    _fixture("static_solid", "text", "solid"),
    _fixture("static_inset", "text", "inset"),
    _fixture("ticker_left_solid", "text_scroll", "solid", "left"),
    _fixture("ticker_right_inset", "text_scroll", "inset", "right"),
)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _fixture_authority() -> list[dict[str, object]]:
    return [
        {"name": item["name"], "config": item["config"], "duration": item["duration"]}
        for item in _FIXTURES
    ]


def _rgb(frame: bytes, x: int, y: int) -> str:
    offset = (y * 64 + x) * 3
    red, green, blue = frame[offset:offset + 3]
    return f"#{red:02x}{green:02x}{blue:02x}"


def _pixels(frame: bytes) -> dict[str, str]:
    return {
        "0,0": _rgb(frame, 0, 0), "1,1": _rgb(frame, 1, 1),
        "63,31": _rgb(frame, 63, 31), "62,30": _rgb(frame, 62, 30),
        "32,16": _rgb(frame, 32, 16),
    }


class _CancelAfterDrawing:
    def __init__(self):
        self.calls = 0

    def is_set(self) -> bool:
        self.calls += 1
        return self.calls >= 2

    def wait(self, _seconds: float) -> bool:
        return self.is_set()


def _dimensions(matrix: object) -> tuple[int, int]:
    hardware = getattr(matrix, "matrix", None)
    width = getattr(hardware, "width", None)
    height = getattr(hardware, "height", None)
    if type(width) is not int or type(height) is not int:
        raise ValueError("physical Matrix dimensions are unavailable")
    if (width, height) != (64, 32):
        raise ValueError("physical evidence requires an exact 64x32 Matrix")
    return width, height


def exercise(
    matrix: object,
    source_sha: str,
    *,
    deadline_seconds: float = 10.0,
    stop_event: threading.Event | None = None,
) -> dict[str, object]:
    """Exercise the production renderer and read every submitted real-Matrix RGB frame."""

    if not isinstance(source_sha, str) or _SHA_RE.fullmatch(source_sha) is None:
        raise ValueError("source SHA must be an exact lowercase Git SHA")
    if isinstance(deadline_seconds, bool) or not isinstance(deadline_seconds, (int, float)):
        raise ValueError("deadline must be numeric")
    if not 0 < deadline_seconds <= 30:
        raise ValueError("deadline must be in (0, 30] seconds")
    width, height = _dimensions(matrix)
    deadline = time.monotonic() + deadline_seconds
    external_stop = stop_event or threading.Event()
    records: list[dict[str, object]] = []

    for fixture in _FIXTURES:
        if external_stop.is_set():
            raise RuntimeError("physical exercise was cancelled")
        hashes: list[str] = []
        revisions: list[int] = []
        frames: list[bytes] = []
        baseline_revision = matrix.latest_frame_rgb()[1]
        for canvas, delay in custom_text.basic_text_frames(
            matrix, fixture["config"], fixture["duration"], external_stop,
        ):
            if time.monotonic() >= deadline:
                raise TimeoutError("physical exercise deadline expired")
            if len(frames) >= _MAX_FRAMES:
                raise ValueError("physical fixture exceeded its frame bound")
            if external_stop.is_set():
                raise RuntimeError("physical exercise was cancelled")
            matrix.swap(canvas)
            frame, revision = matrix.latest_frame_rgb()
            if not isinstance(frame, bytes) or len(frame) != width * height * 3:
                raise ValueError("Matrix.latest_frame_rgb() returned invalid RGB bytes")
            if type(revision) is not int or revision <= baseline_revision:
                raise ValueError("Matrix.latest_frame_rgb() revision did not advance")
            expected_revision = baseline_revision + len(frames) + 1
            if revision != expected_revision:
                raise ValueError("physical frame revisions are not contiguous")
            frames.append(frame)
            hashes.append(hashlib.sha256(frame).hexdigest())
            revisions.append(revision - baseline_revision)
        if not frames:
            raise ValueError("physical fixture submitted no frame")
        foreground = bytes.fromhex(fixture["config"]["colour"][1:])
        records.append({
            "name": fixture["name"],
            "kind": fixture["config"]["type"],
            "direction": fixture["config"]["direction"] if fixture["config"]["type"] == "text_scroll" else "none",
            "border": fixture["config"]["border"],
            "background": fixture["config"]["background"],
            "border_colour": fixture["config"]["border_colour"],
            "frame_count": len(frames),
            "delay_ms": round((fixture["config"]["speed"] if fixture["config"]["type"] == "text_scroll" else fixture["duration"]) * 1000),
            "frame_sha256": hashes,
            "relative_revisions": revisions,
            "pixels": {"first": _pixels(frames[0]), "last": _pixels(frames[-1])},
            "foreground_pixels_max": max(frame.count(foreground) for frame in frames),
        })

    before_revision = matrix.latest_frame_rgb()[1]
    cancelled = _CancelAfterDrawing()
    published = custom_text.run(
        matrix, {}, duration=0.001, stop_event=cancelled,
        module_config=_FIXTURES[1]["config"],
    )
    after_revision = matrix.latest_frame_rgb()[1]
    cancellation = {
        "attempted": True, "published": bool(published),
        "revision_before": before_revision, "revision_after": after_revision,
    }
    report: dict[str, object] = {
        "version": 1, "source_sha": source_sha, "width": width, "height": height,
        "fixture_digest": hashlib.sha256(_canonical(_fixture_authority())).hexdigest(),
        "fixtures": records, "cancellation": cancellation,
    }
    report["evidence_digest"] = hashlib.sha256(_canonical(report)).hexdigest()
    verify_report(report, source_sha)
    return report


def _load_report(value: object) -> dict[str, object]:
    if isinstance(value, (str, bytes, bytearray)):
        raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        if len(raw) > _MAX_REPORT_BYTES:
            raise ValueError("physical report is oversized")
        try:
            report = json.loads(raw, object_pairs_hook=_unique_object)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("physical report is unreadable") from error
        if _canonical(report) != raw:
            raise ValueError("physical report is not canonical JSON")
    else:
        report = value
        if len(_canonical(report)) > _MAX_REPORT_BYTES:
            raise ValueError("physical report is oversized")
    if type(report) is not dict:
        raise ValueError("physical report must be an object")
    return report


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("physical report contains duplicate fields")
        result[key] = value
    return result


def _expected_edge_pixels(border: str) -> dict[str, str]:
    background = "#123456"
    border_colour = "#abcdef"
    if border == "solid":
        return {"0,0": border_colour, "63,31": border_colour}
    if border == "inset":
        return {"0,0": background, "1,1": border_colour, "62,30": border_colour}
    return {"0,0": background, "63,31": background}


def verify_report(value: object, expected_source_sha: str) -> dict[str, object]:
    """Fail closed on noncanonical, unbound, malformed, or tampered evidence."""

    if not isinstance(expected_source_sha, str) or _SHA_RE.fullmatch(expected_source_sha) is None:
        raise ValueError("expected source SHA is invalid")
    report = _load_report(value)
    if set(report) != _REPORT_KEYS or report.get("version") != 1:
        raise ValueError("physical report fields are not exact")
    if report.get("source_sha") != expected_source_sha:
        raise ValueError("physical report source SHA mismatch")
    if (report.get("width"), report.get("height")) != (64, 32):
        raise ValueError("physical report dimensions are not 64x32")
    fixture_digest = hashlib.sha256(_canonical(_fixture_authority())).hexdigest()
    if report.get("fixture_digest") != fixture_digest:
        raise ValueError("physical fixture authority mismatch")
    supplied_digest = report.get("evidence_digest")
    unsigned = dict(report)
    unsigned.pop("evidence_digest", None)
    if not isinstance(supplied_digest, str) or supplied_digest != hashlib.sha256(_canonical(unsigned)).hexdigest():
        raise ValueError("physical evidence digest mismatch")

    fixtures = report.get("fixtures")
    if not isinstance(fixtures, list) or len(fixtures) != len(_FIXTURES):
        raise ValueError("physical fixture records are incomplete")
    if [item.get("name") if isinstance(item, dict) else None for item in fixtures] != [item["name"] for item in _FIXTURES]:
        raise ValueError("physical fixture names are missing, duplicate, or reordered")
    for record, fixture in zip(fixtures, _FIXTURES, strict=True):
        if not isinstance(record, dict) or set(record) != _FIXTURE_KEYS:
            raise ValueError("physical fixture record fields are not exact")
        config = fixture["config"]
        expected_direction = config["direction"] if config["type"] == "text_scroll" else "none"
        if (
            record["kind"] != config["type"] or record["direction"] != expected_direction
            or record["border"] != config["border"]
            or record["background"] != config["background"]
            or record["border_colour"] != config["border_colour"]
        ):
            raise ValueError("physical fixture identity mismatch")
        count = record["frame_count"]
        hashes = record["frame_sha256"]
        revisions = record["relative_revisions"]
        if type(count) is not int or not 1 <= count <= _MAX_FRAMES:
            raise ValueError("physical frame count is invalid")
        if config["type"] == "text":
            expected_count = 1
        elif config["direction"] == "left":
            expected_count = 149
        else:
            expected_count = 85
        if count != expected_count or not isinstance(hashes, list) or len(hashes) != count:
            raise ValueError("physical frame count does not match deterministic fixture")
        if any(not isinstance(item, str) or _HASH_RE.fullmatch(item) is None for item in hashes):
            raise ValueError("physical frame hash is invalid")
        if revisions != list(range(1, count + 1)):
            raise ValueError("physical relative revisions are invalid")
        expected_delay = round((config["speed"] if config["type"] == "text_scroll" else fixture["duration"]) * 1000)
        if record["delay_ms"] != expected_delay:
            raise ValueError("physical frame delay changed")
        pixels = record["pixels"]
        if not isinstance(pixels, dict) or set(pixels) != _PIXEL_KEYS:
            raise ValueError("physical pixel assertions are invalid")
        for position in ("first", "last"):
            samples = pixels[position]
            if not isinstance(samples, dict) or set(samples) != {"0,0", "1,1", "63,31", "62,30", "32,16"}:
                raise ValueError("physical pixel sample fields are not exact")
            for coordinate, colour in _expected_edge_pixels(config["border"]).items():
                if samples.get(coordinate) != colour:
                    raise ValueError("physical border/background pixel assertion failed")
        foreground_count = record["foreground_pixels_max"]
        if type(foreground_count) is not int or foreground_count <= 0:
            raise ValueError("physical fixture did not prove rendered text pixels")

    cancellation = report.get("cancellation")
    if not isinstance(cancellation, dict) or set(cancellation) != _CANCELLATION_KEYS:
        raise ValueError("physical cancellation evidence fields are not exact")
    if cancellation["attempted"] is not True or cancellation["published"] is not False:
        raise ValueError("physical cancellation published a stale frame")
    if type(cancellation["revision_before"]) is not int or cancellation["revision_before"] < 0:
        raise ValueError("physical cancellation revision is invalid")
    if cancellation["revision_after"] != cancellation["revision_before"]:
        raise ValueError("physical cancellation changed the published revision")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    exercise_parser = commands.add_parser("exercise")
    exercise_parser.add_argument("--source-sha", required=True)
    exercise_parser.add_argument("--width", type=int, required=True)
    exercise_parser.add_argument("--height", type=int, required=True)
    exercise_parser.add_argument("--deadline-seconds", type=float, default=10.0)
    exercise_parser.add_argument("--output")
    verify_parser = commands.add_parser("verify-report")
    verify_parser.add_argument("--source-sha", required=True)
    verify_parser.add_argument("--report", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "exercise":
        if (arguments.width, arguments.height) != (64, 32):
            raise ValueError("physical evidence requires --width 64 --height 32")
        from matrix_framework import Matrix
        report = exercise(
            Matrix(rows=arguments.height, cols=arguments.width),
            arguments.source_sha,
            deadline_seconds=arguments.deadline_seconds,
        )
        encoded = _canonical(report)
        if arguments.output:
            path = Path(arguments.output)
            path.write_bytes(encoded)
        else:
            print(encoded.decode("utf-8"), end="")
        return 0
    raw = Path(arguments.report).read_bytes()
    verify_report(raw, arguments.source_sha)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, TimeoutError, ValueError) as error:
        print(f"priority-1.4 physical verification failed: {error}", file=__import__("sys").stderr)
        raise SystemExit(2)
