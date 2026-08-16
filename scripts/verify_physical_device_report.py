#!/usr/bin/env python3
"""Fail-closed validator for canonical physical-device CI evidence."""

import argparse
import sys
from pathlib import Path

MAX_REPORT_BYTES = 4096
MAX_ERROR_CHARS = 512


def parse_report(payload, expected_sha, expected_pipeline_id):
    if len(payload) > MAX_REPORT_BYTES:
        raise ValueError("physical-device report exceeded bound")
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("physical-device report is not ASCII") from error
    if not text.endswith("\n") or "\r" in text:
        raise ValueError("physical-device report has invalid line endings")
    lines = text[:-1].split("\n")
    if len(lines) not in (4, 5):
        raise ValueError("physical-device report has unexpected field count")
    pairs = []
    for line in lines:
        if "=" not in line:
            raise ValueError("physical-device report has malformed field")
        pairs.append(line.split("=", 1))
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("physical-device report has duplicate fields")
    values = dict(pairs)
    acceptance = values.get("PHYSICAL_DEVICE_ACCEPTANCE")
    expected_keys = [
        "SOURCE_SHA",
        "PIPELINE_ID",
        "PHYSICAL_DEVICE_ACCEPTANCE",
        "PHYSICAL_DEVICE_RECOVERY",
    ]
    if acceptance == "FAIL":
        expected_keys.append("ERROR")
    elif acceptance != "PASS":
        raise ValueError("physical-device report has invalid acceptance")
    if keys != expected_keys:
        raise ValueError("physical-device report has unexpected or contradictory fields")
    if values["SOURCE_SHA"] != expected_sha or values["PIPELINE_ID"] != expected_pipeline_id:
        raise ValueError("physical-device report labels do not match pipeline")
    if values["PHYSICAL_DEVICE_RECOVERY"] != "PASS":
        raise ValueError("physical-device recovery did not pass")
    if acceptance == "FAIL":
        error = values["ERROR"]
        if not 1 <= len(error) <= MAX_ERROR_CHARS or any(not " " <= char <= "~" for char in error):
            raise ValueError("physical-device report has invalid error evidence")
    return values


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--pipeline-id", required=True)
    args = parser.parse_args(argv)
    try:
        payload = args.report.read_bytes()
        values = parse_report(payload, args.source_sha, args.pipeline_id)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0 if values["PHYSICAL_DEVICE_ACCEPTANCE"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
