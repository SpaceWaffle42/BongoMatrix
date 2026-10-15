#!/usr/bin/env python3
"""Build a compact, machine-readable summary from GitLab CI reports."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import math
from pathlib import Path
import re
from typing import Any, cast

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException


GITLAB_SECURITY_SEVERITIES = {
    "Info",
    "Unknown",
    "Low",
    "Medium",
    "High",
    "Critical",
}
MAX_SUMMARY_COUNT = 10_000_000
CVSS_PATTERNS = (
    re.compile(r"^((AV:[NAL]|AC:[LMH]|Au:[MSN]|[CIA]:[NPC]|E:(U|POC|F|H|ND)|RL:(OF|TF|W|U|ND)|RC:(UC|UR|C|ND)|CDP:(N|L|LM|MH|H|ND)|TD:(N|L|M|H|ND)|[CIA]R:(L|M|H|ND))/)*(AV:[NAL]|AC:[LMH]|Au:[MSN]|[CIA]:[NPC]|E:(U|POC|F|H|ND)|RL:(OF|TF|W|U|ND)|RC:(UC|UR|C|ND)|CDP:(N|L|LM|MH|H|ND)|TD:(N|L|M|H|ND)|[CIA]R:(L|M|H|ND))$"),
    re.compile(r"^CVSS:3[.][01]/((AV:[NALP]|AC:[LH]|PR:[NLH]|UI:[NR]|S:[UC]|[CIA]:[NLH]|E:[XUPFH]|RL:[XOTWU]|RC:[XURC]|[CIA]R:[XLMH]|MAV:[XNALP]|MAC:[XLH]|MPR:[XNLH]|MUI:[XNR]|MS:[XUC]|M[CIA]:[XNLH])/)*(AV:[NALP]|AC:[LH]|PR:[NLH]|UI:[NR]|S:[UC]|[CIA]:[NLH]|E:[XUPFH]|RL:[XOTWU]|RC:[XURC]|[CIA]R:[XLMH]|MAV:[XNALP]|MAC:[XLH]|MPR:[XNLH]|MUI:[XNR]|MS:[XUC]|M[CIA]:[XNLH])$"),
    re.compile(r"^CVSS:4[.]0/AV:[NALP]/AC:[LH]/AT:[NP]/PR:[NLH]/UI:[NPA]/VC:[HLN]/VI:[HLN]/VA:[HLN]/SC:[HLN]/SI:[HLN]/SA:[HLN](/E:[XAPU])?(/CR:[XHML])?(/IR:[XHML])?(/AR:[XHML])?(/MAV:[XNALP])?(/MAC:[XLH])?(/MAT:[XNP])?(/MPR:[XNLH])?(/MUI:[XNPA])?(/MVC:[XNLH])?(/MVI:[XNLH])?(/MVA:[XNLH])?(/MSC:[XNLH])?(/MSI:[XNLHS])?(/MSA:[XNLHS])?(/S:[XNP])?(/AU:[XNY])?(/R:[XAUI])?(/V:[XDC])?(/RE:[XLMH])?(/U:(X|Clear|Green|Amber|Red))?$"),
)


class InvalidReport(Exception):
    def __init__(self, report_name: str):
        super().__init__(report_name)
        self.report_name = report_name


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _read_report_json(path: Path, invalid_reports: list[str]):
    try:
        return _read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        invalid_reports.append(path.name)
        return None


def _test_counts(root: Path) -> dict[str, int]:
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    try:
        for path in sorted((root / "test-results").glob("*.xml")):
            report = ET.parse(path).getroot()
            if report.tag == "testsuite":
                suites = [report]
            elif report.tag == "testsuites":
                suites = list(report.iter("testsuite"))
                if any(
                    any(child.tag == "testsuite" for child in suite)
                    for suite in suites
                ):
                    raise InvalidReport("test-results/*.xml")
            else:
                raise InvalidReport("test-results/*.xml")
            if not suites or any(suite.find("./testsuite") is not None for suite in suites):
                raise InvalidReport("test-results/*.xml")
            for suite in suites:
                if any(key not in suite.attrib for key in counts):
                    raise InvalidReport("test-results/*.xml")
                values = {key: int(suite.attrib[key]) for key in counts}
                if any(
                    value < 0 or value > MAX_SUMMARY_COUNT
                    for value in values.values()
                ):
                    raise InvalidReport("test-results/*.xml")
                if (
                    values["failures"] + values["errors"] + values["skipped"]
                    > values["tests"]
                ):
                    raise InvalidReport("test-results/*.xml")
                testcases = suite.findall("./testcase")
                if testcases:
                    observed = {
                        "tests": len(testcases),
                        "failures": sum(
                            case.find("./failure") is not None for case in testcases
                        ),
                        "errors": sum(
                            case.find("./error") is not None for case in testcases
                        ),
                        "skipped": sum(
                            case.find("./skipped") is not None for case in testcases
                        ),
                    }
                    if observed != values:
                        raise InvalidReport("test-results/*.xml")
                for key, value in values.items():
                    counts[key] += value
                if any(value > MAX_SUMMARY_COUNT for value in counts.values()):
                    raise InvalidReport("test-results/*.xml")
    except InvalidReport:
        raise
    except (
        OSError,
        ET.ParseError,
        DefusedXmlException,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        raise InvalidReport("test-results/*.xml") from error
    return counts


def _coverage(root: Path) -> dict[str, float | int]:
    try:
        report = ET.parse(root / "coverage.xml").getroot()
        if report.tag != "coverage":
            raise InvalidReport("coverage.xml")
        line_rate = float(report.attrib["line-rate"])
        lines_covered = int(report.attrib["lines-covered"])
        lines_valid = int(report.attrib["lines-valid"])
        if (
            not math.isfinite(line_rate)
            or not 0 <= line_rate <= 1
            or lines_valid <= 0
            or lines_valid > MAX_SUMMARY_COUNT
            or lines_covered < 0
            or lines_covered > lines_valid
            or abs(line_rate - (lines_covered / lines_valid)) > 0.0001
        ):
            raise InvalidReport("coverage.xml")
        return {
            "line_percent": round(line_rate * 100, 2),
            "lines_covered": lines_covered,
            "lines_valid": lines_valid,
        }
    except InvalidReport:
        raise
    except (
        OSError,
        ET.ParseError,
        DefusedXmlException,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        raise InvalidReport("coverage.xml") from error


def _valid_shard_verification(report: Any, tests: dict[str, int]) -> bool:
    """The sharded suite must have executed exactly the collected inventory once."""
    if not isinstance(report, dict):
        return False
    counts = ("collected", "junit_cases", "shards")
    lists = ("missing", "foreign", "duplicated")
    if not all(
        isinstance(report.get(key), int)
        and not isinstance(report.get(key), bool)
        and 0 < report[key] <= MAX_SUMMARY_COUNT
        for key in counts
    ):
        return False
    if not all(report.get(key) == [] for key in lists):
        return False
    return report["junit_cases"] == tests["tests"] and report["collected"] <= tests["tests"]


def _valid_code_quality_findings(findings: Any) -> bool:
    valid_severities = {"info", "minor", "major", "critical", "blocker"}
    if not isinstance(findings, list):
        return False
    for finding in findings:
        if not isinstance(finding, dict):
            return False
        location = finding.get("location")
        lines = location.get("lines") if isinstance(location, dict) else None
        if not (
            isinstance(finding.get("description"), str)
            and bool(finding["description"])
            and isinstance(finding.get("check_name"), str)
            and bool(finding["check_name"])
            and isinstance(finding.get("fingerprint"), str)
            and bool(finding["fingerprint"])
            and finding.get("severity") in valid_severities
            and isinstance(location, dict)
            and isinstance(location.get("path"), str)
            and bool(location["path"])
            and isinstance(lines, dict)
            and isinstance(lines.get("begin"), int)
            and not isinstance(lines["begin"], bool)
            and lines["begin"] > 0
        ):
            return False
    return True


def _valid_scanner_component(component: Any) -> bool:
    if not isinstance(component, dict):
        return False
    vendor = component.get("vendor")
    required_strings = ("id", "name", "version")
    return (
        all(
            isinstance(component.get(key), str) and bool(component[key])
            for key in required_strings
        )
        and isinstance(vendor, dict)
        and isinstance(vendor.get("name"), str)
        and bool(vendor["name"])
    )


def _valid_identifier(identifier: Any) -> bool:
    return bool(
        isinstance(identifier, dict)
        and all(
            isinstance(identifier.get(key), str) and bool(identifier[key])
            for key in ("type", "name", "value")
        )
        and (
            "url" not in identifier
            or (
                isinstance(identifier["url"], str)
                and re.match(r"^(https?|ftp)://.+", identifier["url"])
            )
        )
    )


def _valid_scan_optional_fields(scan: dict[str, Any]) -> bool:
    options = scan.get("options", [])
    if not (
        isinstance(options, list)
        and all(
            isinstance(option, dict)
            and isinstance(option.get("name"), str)
            and 0 < len(option["name"]) <= 255
            and "value" in option
            and (
                option["value"] is None
                or isinstance(option["value"], (str, int, bool))
            )
            and (
                "source" not in option
                or option["source"] in {"argument", "file", "env_variable", "other"}
            )
            for option in options
        )
    ):
        return False
    partial_scan = scan.get("partial_scan")
    if "partial_scan" in scan and not (
        isinstance(partial_scan, dict) and partial_scan.get("mode") == "differential"
    ):
        return False
    primary_identifiers = scan.get("primary_identifiers", [])
    if not (
        isinstance(primary_identifiers, list)
        and all(_valid_identifier(item) for item in primary_identifiers)
    ):
        return False
    observability = scan.get("observability", {})
    if not isinstance(observability, dict):
        return False
    events = observability.get("events", [])
    return bool(
        isinstance(events, list)
        and all(
            isinstance(event, dict)
            and isinstance(event.get("event"), str)
            and bool(event["event"])
            and all(
                key not in event or isinstance(event[key], str)
                for key in ("property", "label")
            )
            and (
                "value" not in event
                or (
                    isinstance(event["value"], (int, float))
                    and not isinstance(event["value"], bool)
                    and math.isfinite(event["value"])
                )
            )
            for event in events
        )
    )


def _valid_remediations(remediations: Any) -> bool:
    return bool(
        isinstance(remediations, list)
        and all(
            isinstance(remediation, dict)
            and isinstance(remediation.get("fixes"), list)
            and all(
                isinstance(fix, dict)
                and isinstance(fix.get("id"), str)
                and bool(fix["id"])
                for fix in remediation["fixes"]
            )
            and isinstance(remediation.get("summary"), str)
            and bool(remediation["summary"])
            and isinstance(remediation.get("diff"), str)
            and bool(remediation["diff"])
            for remediation in remediations
        )
    )


def _valid_tracking(tracking: Any) -> bool:
    if not isinstance(tracking, dict) or not isinstance(tracking.get("items"), list):
        return False
    if "type" in tracking and tracking["type"] != "source":
        return False
    return all(
        isinstance(item, dict)
        and isinstance(item.get("signatures"), list)
        and bool(item["signatures"])
        and all(
            isinstance(signature, dict)
            and isinstance(signature.get("algorithm"), str)
            and bool(signature["algorithm"])
            and isinstance(signature.get("value"), str)
            and bool(signature["value"])
            for signature in item["signatures"]
        )
        and all(
            key not in item or isinstance(item[key], str) for key in ("file",)
        )
        and all(
            key not in item
            or (
                isinstance(item[key], (int, float))
                and not isinstance(item[key], bool)
                and math.isfinite(item[key])
                and item[key] > 0
            )
            for key in ("start_line", "end_line")
        )
        for item in tracking["items"]
    )


def _valid_detail(detail: Any, depth: int = 0) -> bool:
    if not isinstance(detail, dict) or depth > 8:
        return False
    detail_type = detail.get("type")
    if detail_type in {"text", "markdown"}:
        return isinstance(detail.get("value"), str)
    if detail_type == "url":
        return bool(
            isinstance(detail.get("href"), str)
            and detail["href"]
            and ("text" not in detail or isinstance(detail["text"], str))
        )
    if detail_type == "code":
        return bool(
            isinstance(detail.get("value"), str)
            and ("lang" not in detail or isinstance(detail["lang"], str))
        )
    if detail_type == "value":
        value = detail.get("value")
        return bool(
            isinstance(value, (str, bool))
            or (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            )
        )
    if detail_type == "diff":
        return all(isinstance(detail.get(key), str) for key in ("before", "after"))
    if detail_type == "commit":
        return isinstance(detail.get("value"), str) and bool(detail["value"])
    if detail_type == "file-location":
        integer_fields = ("line_start", "line_end", "column_start", "column_end")
        return bool(
            isinstance(detail.get("file_name"), str)
            and bool(detail["file_name"])
            and isinstance(detail.get("line_start"), int)
            and not isinstance(detail["line_start"], bool)
            and 1 <= detail["line_start"] <= MAX_SUMMARY_COUNT
            and all(
                key not in detail
                or (
                    isinstance(detail[key], int)
                    and not isinstance(detail[key], bool)
                    and 1 <= detail[key] <= MAX_SUMMARY_COUNT
                )
                for key in integer_fields
            )
            and (("column_start" in detail) == ("column_end" in detail))
        )
    if detail_type == "module-location":
        return bool(
            isinstance(detail.get("module_name"), str)
            and bool(detail["module_name"])
            and isinstance(detail.get("offset"), int)
            and not isinstance(detail["offset"], bool)
        )
    if detail_type == "list":
        items = detail.get("items")
        return bool(
            isinstance(items, list)
            and len(items) <= 1_000
            and all(_valid_detail(item, depth + 1) for item in items)
        )
    if detail_type == "table":
        header = detail.get("header", [])
        rows = detail.get("rows")
        return bool(
            isinstance(header, list)
            and len(header) <= 1_000
            and all(_valid_detail(item, depth + 1) for item in header)
            and isinstance(rows, list)
            and len(rows) <= 1_000
            and all(
                isinstance(row, list)
                and len(row) <= 1_000
                and all(_valid_detail(item, depth + 1) for item in row)
                for row in rows
            )
        )
    if detail_type == "named-list":
        items = detail.get("items")
        return bool(
            isinstance(items, dict)
            and len(items) <= 1_000
            and all(
                isinstance(name, str)
                and isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and bool(item["name"])
                and (
                    "description" not in item
                    or isinstance(item["description"], str)
                )
                and _valid_detail(item, depth + 1)
                for name, item in items.items()
            )
        )
    return False


def _valid_optional_finding_fields(finding: dict[str, Any]) -> bool:
    cvss_vectors = finding.get("cvss_vectors")
    if "cvss_vectors" in finding and not (
        isinstance(cvss_vectors, list)
        and 1 <= len(cvss_vectors) <= 10
        and all(
            isinstance(vector, dict)
            and isinstance(vector.get("vendor"), str)
            and bool(vector["vendor"])
            and isinstance(vector.get("vector"), str)
            and 16 <= len(vector["vector"]) <= 256
            and any(pattern.fullmatch(vector["vector"]) for pattern in CVSS_PATTERNS)
            for vector in cvss_vectors
        )
    ):
        return False
    links = finding.get("links")
    if "links" in finding and not (
        isinstance(links, list)
        and all(
            isinstance(link, dict)
            and isinstance(link.get("url"), str)
            and re.match(r"^(https?|ftp)://.+", link["url"])
            and ("name" not in link or isinstance(link["name"], str))
            for link in links
        )
    ):
        return False
    flags = finding.get("flags")
    if "flags" in finding and not (
        isinstance(flags, list)
        and all(
            isinstance(flag, dict)
            and flag.get("type") == "flagged-as-likely-false-positive"
            and isinstance(flag.get("origin"), str)
            and bool(flag["origin"])
            and isinstance(flag.get("description"), str)
            and bool(flag["description"])
            for flag in flags
        )
    ):
        return False
    if "tracking" in finding and not _valid_tracking(finding["tracking"]):
        return False
    if "details" in finding and not (
        isinstance(finding["details"], dict)
        and bool(finding["details"])
        and all(
            isinstance(name, str)
            and bool(name)
            and isinstance(value, dict)
            and isinstance(value.get("name"), str)
            and bool(value["name"])
            and (
                "description" not in value
                or isinstance(value["description"], str)
            )
            and _valid_detail(value)
            for name, value in finding["details"].items()
        )
    ):
        return False
    return True


def _valid_location_optional_fields(location: dict[str, Any]) -> bool:
    for key in ("end_line",):
        if key in location and not (
            isinstance(location[key], (int, float))
            and not isinstance(location[key], bool)
            and math.isfinite(location[key])
            and 0 < location[key] <= MAX_SUMMARY_COUNT
        ):
            return False
    if (
        "end_line" in location
        and "start_line" in location
        and location["end_line"] < location["start_line"]
    ):
        return False
    return all(
        key not in location or isinstance(location[key], str)
        for key in ("class", "method")
    )


def _valid_security_finding(finding: Any, expected_type: str) -> bool:
    if not isinstance(finding, dict):
        return False
    identifiers = finding.get("identifiers")
    location = finding.get("location")
    common_valid = bool(
        isinstance(finding.get("id"), str)
        and re.fullmatch(r"[0-9a-f]{64}", finding["id"])
        and isinstance(finding.get("severity"), str)
        and finding["severity"] in GITLAB_SECURITY_SEVERITIES
        and isinstance(identifiers, list)
        and bool(identifiers)
        and all(_valid_identifier(identifier) for identifier in identifiers)
        and isinstance(location, dict)
        and all(
            key not in finding or isinstance(finding[key], str)
            for key in (
                "name",
                "description",
                "solution",
                "raw_source_code_extract",
            )
        )
        and all(
            key not in finding or isinstance(finding[key], list)
            for key in ("cvss_vectors", "links", "flags")
        )
        and all(
            key not in finding or isinstance(finding[key], dict)
            for key in ("details", "tracking")
        )
        and _valid_optional_finding_fields(finding)
        and _valid_location_optional_fields(location)
    )
    if not common_valid:
        return False
    location = cast(dict[str, Any], location)
    if expected_type == "sast":
        return bool(
            isinstance(location.get("file"), str)
            and bool(location["file"])
            and isinstance(location.get("start_line"), int)
            and not isinstance(location["start_line"], bool)
            and 0 < location["start_line"] <= MAX_SUMMARY_COUNT
        )
    commit = location.get("commit")
    return bool(
        expected_type == "secret_detection"
        and isinstance(commit, dict)
        and isinstance(commit.get("sha"), str)
        and re.fullmatch(r"[0-9a-f]{40}", commit["sha"])
        and (
            "file" not in location
            or (isinstance(location["file"], str) and bool(location["file"]))
        )
        and (
            "start_line" not in location
            or (
                isinstance(location["start_line"], int)
                and not isinstance(location["start_line"], bool)
                and 0 < location["start_line"] <= MAX_SUMMARY_COUNT
            )
        )
    )


def _valid_security_report(report: Any, expected_type: str) -> bool:
    if not isinstance(report, dict):
        return False
    version = report.get("version")
    scan = report.get("scan")
    vulnerabilities = report.get("vulnerabilities")
    if not isinstance(scan, dict):
        return False
    messages = scan.get("messages", [])
    if not (
        isinstance(messages, list)
        and all(
            isinstance(message, dict)
            and message.get("level") in {"info", "warn", "fatal"}
            and isinstance(message.get("value"), str)
            and bool(message["value"])
            for message in messages
        )
        and not any(message["level"] in {"warn", "fatal"} for message in messages)
    ):
        return False
    timestamp_pattern = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
    start_time = scan.get("start_time")
    end_time = scan.get("end_time")
    if not (
        isinstance(start_time, str)
        and timestamp_pattern.fullmatch(start_time)
        and isinstance(end_time, str)
        and timestamp_pattern.fullmatch(end_time)
    ):
        return False
    try:
        started = datetime.strptime(start_time, "%Y-%m-%dT%H:%M:%S")
        ended = datetime.strptime(end_time, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return False
    if ended < started:
        return False
    if not (
        isinstance(version, str)
        and re.fullmatch(r"15\.\d+\.\d+", version)
        and (
            "schema" not in report
            or (
                isinstance(report["schema"], str)
                and re.match(r"^https?://.+", report["schema"])
            )
        )
        and _valid_remediations(report.get("remediations", []))
        and scan.get("status") == "success"
        and scan.get("type") == expected_type
        and _valid_scanner_component(scan.get("analyzer"))
        and _valid_scanner_component(scan.get("scanner"))
        and _valid_scan_optional_fields(scan)
        and isinstance(vulnerabilities, list)
        and all(
            _valid_security_finding(finding, expected_type)
            for finding in vulnerabilities
        )
    ):
        return False
    finding_ids = [finding["id"] for finding in vulnerabilities]
    return len(finding_ids) == len(set(finding_ids))


def _valid_baseline(baseline: Any, expected_source_commit: str) -> bool:
    if not (
        isinstance(baseline, dict)
        and baseline.get("schema_version") == 1
        and isinstance(baseline.get("source_commit"), str)
        and re.fullmatch(r"[0-9a-f]{40}", baseline["source_commit"])
        and baseline["source_commit"] == expected_source_commit
        and isinstance(baseline.get("accepted_findings"), list)
        and isinstance(baseline.get("accepted_counts"), dict)
        and isinstance(baseline.get("rationale"), dict)
    ):
        return False
    findings = baseline["accepted_findings"]
    if not all(
        isinstance(finding, dict)
        and isinstance(finding.get("id"), str)
        and re.fullmatch(r"[0-9a-f]{64}", finding["id"])
        and isinstance(finding.get("severity"), str)
        and finding["severity"] in GITLAB_SECURITY_SEVERITIES
        for finding in findings
    ):
        return False
    ids = [finding["id"] for finding in findings]
    counts = dict(sorted(Counter(finding["severity"] for finding in findings).items()))
    return len(ids) == len(set(ids)) and baseline["accepted_counts"] == counts


def build_summary(
    root: Path, baseline_path: Path, expected_baseline_source: str
) -> dict[str, Any]:
    required_reports = [
        ("test-results/*.xml", bool(list((root / "test-results").glob("*.xml")))),
        ("coverage.xml", (root / "coverage.xml").is_file()),
        ("shard-verification.json", (root / "shard-verification.json").is_file()),
        (
            "gl-code-quality-report.json",
            (root / "gl-code-quality-report.json").is_file(),
        ),
        ("dependency-audit.json", (root / "dependency-audit.json").is_file()),
        ("gl-sast-report.json", (root / "gl-sast-report.json").is_file()),
        (
            "gl-secret-detection-report.json",
            (root / "gl-secret-detection-report.json").is_file(),
        ),
    ]
    missing_reports = [name for name, present in required_reports if not present]
    if missing_reports:
        return {
            "schema_version": 1,
            "overall_status": "INCOMPLETE",
            "missing_reports": missing_reports,
        }

    tests = _test_counts(root)
    coverage = _coverage(root)
    invalid_reports = []
    shards = _read_report_json(root / "shard-verification.json", invalid_reports)
    if not _valid_shard_verification(shards, tests) and "shard-verification.json" not in invalid_reports:
        invalid_reports.append("shard-verification.json")
    code_quality = _read_report_json(
        root / "gl-code-quality-report.json", invalid_reports
    )
    audit = _read_report_json(root / "dependency-audit.json", invalid_reports)
    sast_report = _read_report_json(root / "gl-sast-report.json", invalid_reports)
    secret_report = _read_report_json(
        root / "gl-secret-detection-report.json", invalid_reports
    )
    baseline = _read_report_json(baseline_path, invalid_reports)

    if (
        not _valid_code_quality_findings(code_quality)
        and "gl-code-quality-report.json" not in invalid_reports
    ):
        invalid_reports.append("gl-code-quality-report.json")
    if not (
        isinstance(audit, dict)
        and isinstance(audit.get("dependencies"), list)
        and bool(audit["dependencies"])
        and audit.get("fixes") == []
        and "error" not in audit
        and "errors" not in audit
        and all(
            isinstance(dependency, dict)
            and isinstance(dependency.get("name"), str)
            and bool(dependency["name"])
            and isinstance(dependency.get("version"), str)
            and bool(dependency["version"])
            and isinstance(dependency.get("vulns"), list)
            and all(
                isinstance(vulnerability, dict)
                and isinstance(vulnerability.get("id"), str)
                and bool(vulnerability["id"])
                and isinstance(vulnerability.get("fix_versions"), list)
                and all(
                    isinstance(version, str) and bool(version)
                    for version in vulnerability["fix_versions"]
                )
                for vulnerability in dependency["vulns"]
            )
            for dependency in audit.get("dependencies", [])
        )
    ) and "dependency-audit.json" not in invalid_reports:
        invalid_reports.append("dependency-audit.json")
    if (
        not _valid_security_report(sast_report, "sast")
        and "gl-sast-report.json" not in invalid_reports
    ):
        invalid_reports.append("gl-sast-report.json")
    if (
        not _valid_security_report(secret_report, "secret_detection")
        and "gl-secret-detection-report.json" not in invalid_reports
    ):
        invalid_reports.append("gl-secret-detection-report.json")
    if (
        not _valid_baseline(baseline, expected_baseline_source)
        and baseline_path.name not in invalid_reports
    ):
        invalid_reports.append(baseline_path.name)
    if invalid_reports:
        return {
            "schema_version": 1,
            "overall_status": "INCOMPLETE",
            "invalid_reports": invalid_reports,
        }

    code_quality = cast(list[dict[str, Any]], code_quality)
    audit = cast(dict[str, Any], audit)
    sast_report = cast(dict[str, Any], sast_report)
    secret_report = cast(dict[str, Any], secret_report)
    baseline = cast(dict[str, Any], baseline)

    dependencies = audit["dependencies"]
    dependency_vulnerabilities = sum(
        len(dependency["vulns"]) for dependency in dependencies
    )

    sast_findings = sast_report["vulnerabilities"]
    accepted_keys = {
        (finding["id"], finding["severity"])
        for finding in baseline["accepted_findings"]
    }
    accepted = [
        finding
        for finding in sast_findings
        if (finding.get("id"), finding.get("severity")) in accepted_keys
    ]
    new_findings = [
        finding
        for finding in sast_findings
        if (finding.get("id"), finding.get("severity")) not in accepted_keys
    ]
    new_by_severity = dict(
        sorted(Counter(finding.get("severity", "Unknown") for finding in new_findings).items())
    )
    secrets = secret_report["vulnerabilities"]

    passing = (
        tests["tests"] > 0
        and tests["failures"] == 0
        and tests["errors"] == 0
        and coverage["lines_valid"] > 0
        and len(code_quality) == 0
        and dependency_vulnerabilities == 0
        and not new_findings
        and not secrets
    )
    return {
        "schema_version": 1,
        "overall_status": "PASS" if passing else "FAIL",
        "tests": tests,
        "coverage": coverage,
        "code_quality": {"findings": len(code_quality)},
        "dependency_audit": {
            "dependencies": len(dependencies),
            "vulnerabilities": dependency_vulnerabilities,
        },
        "sast": {
            "total": len(sast_findings),
            "accepted": len(accepted),
            "new": len(new_findings),
            "new_by_severity": new_by_severity,
        },
        "secret_detection": {"findings": len(secrets)},
    }


def render_text(summary: dict[str, Any]) -> str:
    if summary["overall_status"] == "INCOMPLETE":
        lines = ["Matrix CI summary: INCOMPLETE"]
        if summary.get("missing_reports"):
            lines.append("Missing reports: " + ", ".join(summary["missing_reports"]))
        if summary.get("invalid_reports"):
            lines.append("Invalid reports: " + ", ".join(summary["invalid_reports"]))
        return "\n".join(lines)

    tests = summary["tests"]
    coverage = summary["coverage"]
    quality = summary["code_quality"]
    audit = summary["dependency_audit"]
    sast = summary["sast"]
    secrets = summary["secret_detection"]
    return "\n".join(
        [
            f'Matrix CI summary: {summary["overall_status"]}',
            (
                f'Tests: {tests["tests"]} run, {tests["failures"]} failures, '
                f'{tests["errors"]} errors, {tests["skipped"]} skipped'
            ),
            (
                f'Coverage: {coverage["line_percent"]:.2f}% '
                f'({coverage["lines_covered"]}/{coverage["lines_valid"]} lines)'
            ),
            f'Ruff: {quality["findings"]} findings',
            (
                f'Dependencies: {audit["dependencies"]} checked, '
                f'{audit["vulnerabilities"]} vulnerabilities'
            ),
            f'SAST: {sast["accepted"]} accepted baseline, {sast["new"]} new',
            f'Secrets: {secrets["findings"]} findings',
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--expected-baseline-source", required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--text-output", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        summary = build_summary(
            args.root, args.baseline, args.expected_baseline_source
        )
    except InvalidReport as error:
        summary = {
            "schema_version": 1,
            "overall_status": "INCOMPLETE",
            "invalid_reports": [error.report_name],
        }
    text = render_text(summary)
    args.json_output.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    args.text_output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if summary["overall_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
