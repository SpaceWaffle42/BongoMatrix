#!/usr/bin/env python3
"""Print bounded context from the exact staged blob of a Git-tracked text file."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

DEFAULT_MAX_SOURCE_BYTES = 1_000_000
DEFAULT_MAX_CHARS = 12_000


def repository_root(start: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        check=True,
        capture_output=True,
    )
    raw = result.stdout[:-1] if result.stdout.endswith(b"\n") else result.stdout
    return Path(os.fsdecode(raw)).resolve()


def index_entry(root: Path, relative: str) -> tuple[str, str]:
    """Return the stage-zero mode and object ID for an exact index pathname."""
    result = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    requested = os.fsencode(relative)
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        metadata, separator, path_bytes = record.partition(b"\t")
        if not separator or path_bytes != requested:
            continue
        try:
            mode, oid, stage = metadata.decode("ascii").split()
        except (UnicodeDecodeError, ValueError) as exc:
            raise SystemExit(f"Invalid Git index entry for: {display_path(relative)}") from exc
        if stage != "0":
            raise SystemExit(f"Refusing unresolved index entry: {display_path(relative)}")
        return mode, oid
    raise SystemExit(f"Refusing untracked source: {display_path(relative)}")


def tracked_relative_path(root: Path, requested: str) -> tuple[Path, str]:
    path = Path(requested)
    path = (Path.cwd() / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise SystemExit(f"Refusing path outside repository: {display_path(str(path))}") from exc
    if not path.is_file():
        raise SystemExit(f"Not a regular file: {display_path(relative)}")
    index_entry(root, relative)
    return path, relative


def git_output(root: Path, args: list[str], failure: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=root, check=False, capture_output=True)
    if result.returncode:
        raise SystemExit(failure)
    return result.stdout


def validate_safe_text(text: str, relative: str) -> None:
    for character in text:
        if character in "\n\t":
            continue
        if unicodedata.category(character) in {"Cc", "Cf"}:
            codepoint = f"U+{ord(character):04X}"
            raise SystemExit(
                f"Refusing control-bearing text ({codepoint}): {display_path(relative)}"
            )


def read_index_text(
    root: Path,
    relative: str,
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
) -> str:
    """Read a bounded, safe UTF-8 regular-file blob from the Git index."""
    mode, oid = index_entry(root, relative)
    if mode not in {"100644", "100755"}:
        raise SystemExit(f"Refusing non-regular Git entry: {display_path(relative)}")
    size_bytes = git_output(
        root,
        ["cat-file", "-s", oid],
        f"Unable to size tracked index content: {display_path(relative)}",
    )
    try:
        size = int(size_bytes.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise SystemExit(f"Invalid Git object size: {display_path(relative)}") from exc
    if size > max_source_bytes:
        raise SystemExit(
            f"Refusing source larger than --max-source-bytes ({size}>{max_source_bytes}): "
            f"{display_path(relative)}"
        )
    blob = git_output(
        root,
        ["cat-file", "blob", oid],
        f"Unable to read tracked index content: {display_path(relative)}",
    )
    if len(blob) > max_source_bytes:
        raise SystemExit(
            f"Refusing source larger than --max-source-bytes ({len(blob)}>{max_source_bytes}): "
            f"{display_path(relative)}"
        )
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"Refusing non-UTF-8 source: {display_path(relative)}") from exc
    validate_safe_text(text, relative)
    return text


def display_path(value: str) -> str:
    """Escape filename controls so framing cannot be forged."""
    return value.encode("unicode_escape", "backslashreplace").decode("ascii")


def heading_slice(lines: list[str], heading: str) -> tuple[int, int]:
    wanted = heading.strip().lstrip("#").strip().casefold()
    start = None
    level = None
    heading_re = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
    for index, line in enumerate(lines):
        match = heading_re.match(line)
        if match and match.group(2).strip().casefold() == wanted:
            start = index
            level = len(match.group(1))
            break
    if start is None or level is None:
        raise SystemExit(f"Heading not found: {display_path(heading)}")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = heading_re.match(lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    return start, end


def query_slices(lines: list[str], pattern: str, context: int, max_matches: int) -> list[tuple[int, int]]:
    wanted = pattern.casefold()
    raw = []
    for index, line in enumerate(lines):
        if wanted in line.casefold():
            raw.append((max(0, index - context), min(len(lines), index + context + 1)))
            if len(raw) >= max_matches:
                break
    if not raw:
        raise SystemExit(f"No query matches: {display_path(pattern)}")
    merged: list[list[int]] = []
    for start, end in raw:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def emit(
    lines: list[str],
    relative: str,
    slices: list[tuple[int, int]],
    max_lines: int,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> None:
    emitted_lines = 0
    emitted_chars = 0
    truncated = False
    print(f"SOURCE={display_path(relative)}")
    for block, (start, end) in enumerate(slices):
        if emitted_lines >= max_lines:
            truncated = True
            break
        if block:
            print("--")
        for index in range(start, end):
            if emitted_lines >= max_lines:
                truncated = True
                break
            line = lines[index]
            remaining = max_chars - emitted_chars
            if line and remaining <= 0:
                truncated = True
                break
            output = line[:remaining] if len(line) > remaining else line
            print(f"{index + 1}|{output}")
            emitted_lines += 1
            emitted_chars += len(output)
            if len(output) < len(line):
                truncated = True
                break
        if truncated:
            break
    if truncated:
        print(f"TRUNCATED=max-lines:{max_lines},max-chars:{max_chars}")
    print(f"EMITTED_LINES={emitted_lines}")
    print(f"EMITTED_CHARS={emitted_chars}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", help="Git-tracked text file")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--heading", help="Exact Markdown heading text, with or without #")
    group.add_argument("--query", help="Case-insensitive literal text")
    parser.add_argument("--context", type=int, default=2, help="Lines around query matches (default: 2)")
    parser.add_argument("--max-matches", type=int, default=10, help="Maximum query matches (default: 10)")
    parser.add_argument("--max-lines", type=int, default=120, help="Maximum emitted content lines (default: 120)")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="Maximum emitted source characters (default: 12000)")
    parser.add_argument("--max-source-bytes", type=int, default=DEFAULT_MAX_SOURCE_BYTES, help="Maximum staged blob size accepted (default: 1000000)")
    args = parser.parse_args()
    if not 0 <= args.context <= 50:
        parser.error("--context must be between 0 and 50")
    if not 1 <= args.max_matches <= 100:
        parser.error("--max-matches must be between 1 and 100")
    if not 1 <= args.max_lines <= 1000:
        parser.error("--max-lines must be between 1 and 1000")
    if not 1 <= args.max_chars <= 100_000:
        parser.error("--max-chars must be between 1 and 100000")
    if not 1 <= args.max_source_bytes <= 10_000_000:
        parser.error("--max-source-bytes must be between 1 and 10000000")

    root = repository_root(Path.cwd())
    _, relative = tracked_relative_path(root, args.file)
    text = read_index_text(root, relative, args.max_source_bytes)
    lines = text.splitlines()
    if args.heading:
        slices = [heading_slice(lines, args.heading)]
    else:
        slices = query_slices(lines, args.query, args.context, args.max_matches)
    emit(lines, relative, slices, args.max_lines, args.max_chars)
    return 0


if __name__ == "__main__":
    sys.exit(main())
