import importlib.util
from collections import Counter
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SUMMARY_SCRIPT = ROOT / "scripts" / "ci_summary.py"
SOURCE_COMMIT = "8b9994024098320fbea99d92c117036c918fede4"
ACCEPTED_ID = "a" * 64
NEW_SAST_ID = "b" * 64
SECRET_ID = "c" * 64


def load_summary_module():
    spec = importlib.util.spec_from_file_location("ci_summary", SUMMARY_SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Cannot load {SUMMARY_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def security_report(vulnerabilities, scan_type="sast"):
    component = {
        "id": "test-scanner",
        "name": "Test Scanner",
        "version": "1.0.0",
        "vendor": {"name": "Test"},
    }
    location = (
        {"commit": {"sha": "a" * 40}, "file": "module.py", "start_line": 1}
        if scan_type == "secret_detection"
        else {"file": "module.py", "start_line": 1}
    )
    complete_findings = [
        {
            "identifiers": [
                {"type": "test", "name": "Test finding", "value": finding["id"]}
            ],
            "location": {**location, "end_line": 1},
            "cvss_vectors": [
                {
                    "vendor": "Test",
                    "vector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
                }
            ],
            "links": [{"name": "Reference", "url": "https://example.invalid/finding"}],
            "flags": [
                {
                    "type": "flagged-as-likely-false-positive",
                    "origin": "Test Scanner",
                    "description": "fixture flag",
                }
            ],
            "tracking": {
                "type": "source",
                "items": [
                    {
                        "file": "module.py",
                        "start_line": 1,
                        "end_line": 1,
                        "signatures": [
                            {"algorithm": "scope-offset", "value": "fixture"}
                        ],
                    }
                ],
            },
            "details": {
                "fixture": {"name": "Fixture", "type": "text", "value": "valid"}
            },
            **finding,
        }
        for finding in vulnerabilities
    ]
    return {
        "version": "15.2.2",
        "scan": {
            "analyzer": component,
            "end_time": "2026-08-09T00:00:01",
            "scanner": component,
            "start_time": "2026-08-09T00:00:00",
            "status": "success",
            "type": scan_type,
            "messages": [{"level": "info", "value": "scan completed"}],
            "options": [{"name": "SECURE_LOG_LEVEL", "value": "info"}],
            "primary_identifiers": [
                {"type": "test", "name": "Test finding", "value": "test"}
            ],
            "observability": {"events": []},
        },
        "schema": "https://example.invalid/security-report-schema.json",
        "remediations": [],
        "vulnerabilities": complete_findings,
    }


def secret_report(vulnerabilities):
    return security_report(vulnerabilities, scan_type="secret_detection")


def code_quality_finding():
    return {
        "description": "quality finding",
        "check_name": "E001",
        "fingerprint": "f" * 64,
        "severity": "major",
        "location": {"path": "module.py", "lines": {"begin": 1}},
    }


def sast_baseline(findings):
    counts = dict(sorted(Counter(finding["severity"] for finding in findings).items()))
    return {
        "schema_version": 1,
        "source_commit": SOURCE_COMMIT,
        "accepted_findings": findings,
        "accepted_counts": counts,
        "rationale": {},
    }


def write_shard_verification(root, tests, **overrides):
    report = {"collected": tests, "junit_cases": tests, "shards": 2,
              "missing": [], "foreign": [], "duplicated": []}
    report.update(overrides)
    write_json(root / "shard-verification.json", report)


def write_clean_reports(root):
    results = root / "test-results"
    results.mkdir()
    (results / "suite.xml").write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0"/>',
        encoding="utf-8",
    )
    (root / "coverage.xml").write_text(
        '<coverage line-rate="1" lines-covered="1" lines-valid="1"/>',
        encoding="utf-8",
    )
    write_shard_verification(root, 1)
    write_shard_verification(root, 1)
    write_json(root / "gl-code-quality-report.json", [])
    write_json(
        root / "dependency-audit.json",
        {"dependencies": [{"name": "package", "version": "1.0", "vulns": []}], "fixes": []},
    )
    write_json(root / "gl-sast-report.json", security_report([]))
    write_json(root / "gl-secret-detection-report.json", secret_report([]))
    baseline = root / "sast-baseline.json"
    write_json(baseline, sast_baseline([]))
    return baseline


class CiSummaryTests(unittest.TestCase):
    def test_accepted_security_baseline_and_clean_reports_produce_pass_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "test-results"
            results.mkdir()
            (results / "suite.xml").write_text(
                '<testsuite tests="3" failures="0" errors="0" skipped="0"/>',
                encoding="utf-8",
            )
            (root / "coverage.xml").write_text(
                '<coverage line-rate="0.81" lines-covered="81" lines-valid="100"/>',
                encoding="utf-8",
            )
            write_shard_verification(root, 3)
            write_json(root / "gl-code-quality-report.json", [])
            write_json(
                root / "dependency-audit.json",
                {"dependencies": [{"name": "Pillow", "version": "10.0", "vulns": []}], "fixes": []},
            )
            write_json(
                root / "gl-sast-report.json",
                security_report([{"id": ACCEPTED_ID, "severity": "Medium"}]),
            )
            write_json(
                root / "gl-secret-detection-report.json",
                secret_report([]),
            )
            write_json(
                root / "sast-baseline.json",
                sast_baseline([{"id": ACCEPTED_ID, "severity": "Medium"}]),
            )

            module = load_summary_module()
            summary = module.build_summary(root, root / "sast-baseline.json", SOURCE_COMMIT)

            self.assertEqual("PASS", summary["overall_status"])
            self.assertEqual(
                {"tests": 3, "failures": 0, "errors": 0, "skipped": 0},
                summary["tests"],
            )
            self.assertEqual(81.0, summary["coverage"]["line_percent"])
            self.assertEqual(0, summary["code_quality"]["findings"])
            self.assertEqual(
                {"dependencies": 1, "vulnerabilities": 0},
                summary["dependency_audit"],
            )
            self.assertEqual(
                {
                    "total": 1,
                    "accepted": 1,
                    "new": 0,
                    "new_by_severity": {},
                },
                summary["sast"],
            )
            self.assertEqual(0, summary["secret_detection"]["findings"])
            self.assertEqual(
                "\n".join(
                    [
                        "Matrix CI summary: PASS",
                        "Tests: 3 run, 0 failures, 0 errors, 0 skipped",
                        "Coverage: 81.00% (81/100 lines)",
                        "Ruff: 0 findings",
                        "Dependencies: 1 checked, 0 vulnerabilities",
                        "SAST: 1 accepted baseline, 0 new",
                        "Secrets: 0 findings",
                    ]
                ),
                module.render_text(summary),
            )

            (results / "suite.xml").write_text(
                '<testsuite tests="0" failures="0" errors="0" skipped="0"/>',
                encoding="utf-8",
            )
            # An empty run cannot carry a valid shard proof, so it never passes.
            empty_suite = module.build_summary(root, root / "sast-baseline.json", SOURCE_COMMIT)
            self.assertNotEqual("PASS", empty_suite["overall_status"])
            (results / "suite.xml").write_text(
                '<testsuite tests="3" failures="0" errors="0" skipped="0"/>',
                encoding="utf-8",
            )
            for bad in ({"missing": ["test_x.T.test_y"]}, {"duplicated": ["test_x.T.test_y"]},
                        {"foreign": ["test_x.T.test_y"]}, {"junit_cases": 2}, {"collected": 4}):
                with self.subTest(shard_proof=bad):
                    write_shard_verification(root, 3, **bad)
                    rejected = module.build_summary(root, root / "sast-baseline.json", SOURCE_COMMIT)
                    self.assertEqual("INCOMPLETE", rejected["overall_status"])
                    self.assertEqual(["shard-verification.json"], rejected["invalid_reports"])
            write_shard_verification(root, 3)

            write_json(
                root / "sast-baseline.json",
                sast_baseline([{"id": ACCEPTED_ID, "severity": "Low"}]),
            )
            changed = module.build_summary(root, root / "sast-baseline.json", SOURCE_COMMIT)
            self.assertEqual("FAIL", changed["overall_status"])
            self.assertEqual(0, changed["sast"]["accepted"])
            self.assertEqual({"Medium": 1}, changed["sast"]["new_by_severity"])

    def test_cli_writes_bounded_failure_summary_and_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "test-results"
            results.mkdir()
            (results / "suite.xml").write_text(
                '<testsuite tests="2" failures="1" errors="0" skipped="0"/>',
                encoding="utf-8",
            )
            (root / "coverage.xml").write_text(
                '<coverage line-rate="0.5" lines-covered="5" lines-valid="10"/>',
                encoding="utf-8",
            )
            write_shard_verification(root, 2)
            write_json(root / "gl-code-quality-report.json", [code_quality_finding()])
            write_json(
                root / "dependency-audit.json",
                {
                    "dependencies": [
                        {"name": "package", "version": "1.0", "vulns": [{"id": "CVE-private", "fix_versions": []}]}
                    ],
                    "fixes": [],
                },
            )
            write_json(
                root / "gl-sast-report.json",
                security_report(
                    [{"id": NEW_SAST_ID, "severity": "High", "name": "sensitive"}]
                ),
            )
            write_json(
                root / "gl-secret-detection-report.json",
                secret_report(
                    [
                        {
                            "id": SECRET_ID,
                            "severity": "Critical",
                            "name": "must-not-be-rendered",
                        }
                    ]
                ),
            )
            baseline = root / "sast-baseline.json"
            write_json(baseline, sast_baseline([]))
            json_output = root / "ci-summary.json"
            text_output = root / "ci-summary.txt"

            module = load_summary_module()
            exit_code = module.main(
                [
                    "--root",
                    str(root),
                    "--baseline",
                    str(baseline),
                    "--expected-baseline-source",
                    SOURCE_COMMIT,
                    "--json-output",
                    str(json_output),
                    "--text-output",
                    str(text_output),
                ]
            )

            self.assertEqual(1, exit_code)
            stored = json.loads(json_output.read_text(encoding="utf-8"))
            self.assertEqual("FAIL", stored["overall_status"])
            self.assertEqual({"High": 1}, stored["sast"]["new_by_severity"])
            rendered = text_output.read_text(encoding="utf-8")
            self.assertIn("Matrix CI summary: FAIL", rendered)
            self.assertIn("SAST: 0 accepted baseline, 1 new", rendered)
            self.assertNotIn("CVE-private", rendered)
            self.assertNotIn("must-not-be-rendered", rendered)

    def test_cli_records_incomplete_summary_when_upstream_artifacts_are_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "sast-baseline.json"
            write_json(baseline, sast_baseline([]))
            json_output = root / "ci-summary.json"
            text_output = root / "ci-summary.txt"

            module = load_summary_module()
            exit_code = module.main(
                [
                    "--root",
                    str(root),
                    "--baseline",
                    str(baseline),
                    "--expected-baseline-source",
                    SOURCE_COMMIT,
                    "--json-output",
                    str(json_output),
                    "--text-output",
                    str(text_output),
                ]
            )

            self.assertEqual(1, exit_code)
            stored = json.loads(json_output.read_text(encoding="utf-8"))
            self.assertEqual("INCOMPLETE", stored["overall_status"])
            self.assertEqual(
                [
                    "test-results/*.xml",
                    "coverage.xml",
                    "shard-verification.json",
                    "gl-code-quality-report.json",
                    "dependency-audit.json",
                    "gl-sast-report.json",
                    "gl-secret-detection-report.json",
                ],
                stored["missing_reports"],
            )
            self.assertIn("Matrix CI summary: INCOMPLETE", text_output.read_text(encoding="utf-8"))

    def test_empty_object_reports_are_incomplete_instead_of_false_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "test-results"
            results.mkdir()
            (results / "suite.xml").write_text(
                '<testsuite tests="1" failures="0" errors="0" skipped="0"/>',
                encoding="utf-8",
            )
            (root / "coverage.xml").write_text(
                '<coverage line-rate="1" lines-covered="1" lines-valid="1"/>',
                encoding="utf-8",
            )
            write_shard_verification(root, 1)
            for name in (
                "gl-code-quality-report.json",
                "dependency-audit.json",
                "gl-sast-report.json",
                "gl-secret-detection-report.json",
            ):
                write_json(root / name, {})
            baseline = root / "sast-baseline.json"
            write_json(baseline, sast_baseline([]))
            json_output = root / "ci-summary.json"
            text_output = root / "ci-summary.txt"

            module = load_summary_module()
            exit_code = module.main(
                [
                    "--root",
                    str(root),
                    "--baseline",
                    str(baseline),
                    "--expected-baseline-source",
                    SOURCE_COMMIT,
                    "--json-output",
                    str(json_output),
                    "--text-output",
                    str(text_output),
                ]
            )

            self.assertEqual(1, exit_code)
            stored = json.loads(json_output.read_text(encoding="utf-8"))
            self.assertEqual("INCOMPLETE", stored["overall_status"])
            self.assertEqual(
                [
                    "gl-code-quality-report.json",
                    "dependency-audit.json",
                    "gl-sast-report.json",
                    "gl-secret-detection-report.json",
                ],
                stored["invalid_reports"],
            )

    def test_malformed_json_still_writes_sanitized_incomplete_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "test-results"
            results.mkdir()
            (results / "suite.xml").write_text(
                '<testsuite tests="1" failures="0" errors="0" skipped="0"/>',
                encoding="utf-8",
            )
            (root / "coverage.xml").write_text(
                '<coverage line-rate="1" lines-covered="1" lines-valid="1"/>',
                encoding="utf-8",
            )
            write_shard_verification(root, 1)
            write_json(root / "gl-code-quality-report.json", [])
            write_json(
                root / "dependency-audit.json",
                {"dependencies": [{"name": "package", "version": "1.0", "vulns": []}], "fixes": []},
            )
            (root / "gl-sast-report.json").write_text(
                '{"vulnerabilities": [secret-invalid-json', encoding="utf-8"
            )
            write_json(root / "gl-secret-detection-report.json", secret_report([]))
            baseline = root / "sast-baseline.json"
            write_json(baseline, sast_baseline([]))
            json_output = root / "ci-summary.json"
            text_output = root / "ci-summary.txt"

            module = load_summary_module()
            exit_code = module.main(
                [
                    "--root",
                    str(root),
                    "--baseline",
                    str(baseline),
                    "--expected-baseline-source",
                    SOURCE_COMMIT,
                    "--json-output",
                    str(json_output),
                    "--text-output",
                    str(text_output),
                ]
            )

            self.assertEqual(1, exit_code)
            stored = json.loads(json_output.read_text(encoding="utf-8"))
            self.assertEqual("INCOMPLETE", stored["overall_status"])
            self.assertEqual(["gl-sast-report.json"], stored["invalid_reports"])
            rendered = text_output.read_text(encoding="utf-8")
            self.assertIn("Invalid reports: gl-sast-report.json", rendered)
            self.assertNotIn("secret-invalid-json", rendered)

    def test_overlong_json_integer_still_writes_incomplete_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = write_clean_reports(root)
            (root / "gl-sast-report.json").write_text(
                '{"version": ' + "9" * 5_000 + "}", encoding="utf-8"
            )
            json_output = root / "ci-summary.json"
            text_output = root / "ci-summary.txt"
            module = load_summary_module()

            exit_code = module.main(
                [
                    "--root",
                    str(root),
                    "--baseline",
                    str(baseline),
                    "--expected-baseline-source",
                    SOURCE_COMMIT,
                    "--json-output",
                    str(json_output),
                    "--text-output",
                    str(text_output),
                ]
            )

            self.assertEqual(1, exit_code)
            stored = json.loads(json_output.read_text(encoding="utf-8"))
            self.assertEqual("INCOMPLETE", stored["overall_status"])
            self.assertEqual(["gl-sast-report.json"], stored["invalid_reports"])
            self.assertIn(
                "Invalid reports: gl-sast-report.json",
                text_output.read_text(encoding="utf-8"),
            )

    def test_report_metadata_and_baseline_corruption_are_incomplete(self):
        invalid_cases = [
            (
                "gl-code-quality-report.json",
                [{}],
            ),
            (
                "dependency-audit.json",
                {"dependencies": [], "fixes": []},
            ),
            (
                "dependency-audit.json",
                {
                    "dependencies": [{"name": "package", "vulns": []}],
                    "fixes": [],
                },
            ),
            (
                "dependency-audit.json",
                {
                    "dependencies": [
                        {"name": "package", "version": "1.0", "vulns": []}
                    ],
                    "fixes": [{}],
                },
            ),
            (
                "dependency-audit.json",
                {
                    "dependencies": [
                        {"name": "package", "version": "1.0", "vulns": [{}]}
                    ],
                    "fixes": [],
                },
            ),
            (
                "dependency-audit.json",
                {
                    "dependencies": [
                        {"name": "package", "version": "1.0", "vulns": []}
                    ],
                    "fixes": [],
                    "error": {},
                },
            ),
            (
                "dependency-audit.json",
                {
                    "dependencies": [{"name": "package", "version": "1.0", "vulns": []}],
                    "fixes": [],
                    "error": "audit failed",
                },
            ),
            (
                "gl-sast-report.json",
                {
                    "version": "15.2.2",
                    "scan": {"status": "failure"},
                    "vulnerabilities": [],
                },
            ),
            (
                "gl-sast-report.json",
                {
                    "version": "16.0.0",
                    "scan": {"status": "success"},
                    "vulnerabilities": [],
                },
            ),
            (
                "gl-sast-report.json",
                {
                    **security_report([]),
                    "scan": {
                        **security_report([])["scan"],
                        "messages": [{"level": "fatal", "value": "secret-marker"}],
                    },
                },
            ),
            (
                "gl-sast-report.json",
                {
                    **security_report([]),
                    "scan": {**security_report([])["scan"], "messages": ""},
                },
            ),
            (
                "gl-sast-report.json",
                {
                    **security_report([]),
                    "scan": {**security_report([])["scan"], "messages": {}},
                },
            ),
            (
                "gl-sast-report.json",
                {
                    **security_report([]),
                    "scan": {**security_report([])["scan"], "options": {}},
                },
            ),
            (
                "gl-sast-report.json",
                {
                    **security_report([]),
                    "scan": {
                        **security_report([])["scan"],
                        "partial_scan": {},
                    },
                },
            ),
            (
                "gl-sast-report.json",
                {**security_report([]), "schema": {}},
            ),
            (
                "gl-sast-report.json",
                {**security_report([]), "remediations": {}},
            ),
            (
                "gl-sast-report.json",
                {
                    **security_report([]),
                    "scan": {
                        **security_report([])["scan"],
                        "messages": [{"level": "warn", "value": "partial scan"}],
                    },
                },
            ),
            (
                "gl-sast-report.json",
                {
                    **security_report([]),
                    "scan": {
                        **security_report([])["scan"],
                        "start_time": "2026-99-09T00:00:00",
                    },
                },
            ),
            (
                "gl-sast-report.json",
                {
                    **security_report([]),
                    "scan": {
                        **security_report([])["scan"],
                        "start_time": "2026-08-09T00:00:02",
                        "end_time": "2026-08-09T00:00:01",
                    },
                },
            ),
            (
                "gl-sast-report.json",
                secret_report([]),
            ),
            (
                "gl-sast-report.json",
                {
                    **security_report([]),
                    "vulnerabilities": [
                        {"id": ACCEPTED_ID, "severity": "Medium"}
                    ],
                },
            ),
            (
                "gl-sast-report.json",
                security_report(
                    [
                        {"id": ACCEPTED_ID, "severity": "Medium"},
                        {"id": ACCEPTED_ID, "severity": "Medium"},
                    ]
                ),
            ),
            (
                "gl-sast-report.json",
                {
                    **security_report(
                        [{"id": ACCEPTED_ID, "severity": "Medium"}]
                    ),
                    "vulnerabilities": [
                        {
                            **security_report(
                                [{"id": ACCEPTED_ID, "severity": "Medium"}]
                            )["vulnerabilities"][0],
                            "description": {},
                        }
                    ],
                },
            ),
            (
                "gl-sast-report.json",
                {
                    "version": "15.2.2",
                    "scan": {"status": "success"},
                    "vulnerabilities": [
                        {"id": "e" * 64, "severity": "secret-marker" * 1000}
                    ],
                },
            ),
            (
                "gl-secret-detection-report.json",
                {
                    **secret_report(
                        [{"id": SECRET_ID, "severity": "Critical"}]
                    ),
                    "vulnerabilities": [
                        {
                            **secret_report(
                                [{"id": SECRET_ID, "severity": "Critical"}]
                            )["vulnerabilities"][0],
                            "location": {"file": "module.py", "start_line": 1},
                        }
                    ],
                },
            ),
            (
                "gl-secret-detection-report.json",
                {
                    "version": "15.2.4",
                    "scan": {"status": "failure"},
                    "vulnerabilities": [],
                },
            ),
            (
                "sast-baseline.json",
                {
                    **sast_baseline([]),
                    "schema_version": 2,
                },
            ),
            (
                "sast-baseline.json",
                {
                    **sast_baseline([]),
                    "source_commit": "f" * 40,
                },
            ),
            (
                "sast-baseline.json",
                sast_baseline(
                    [
                        {"id": "d" * 64, "severity": "Low"},
                        {"id": "d" * 64, "severity": "Low"},
                    ]
                ),
            ),
        ]
        for field, value in (
            ("cvss_vectors", [{}]),
            ("cvss_vectors", [{"vendor": "Test", "vector": "x" * 32}]),
            ("links", [{}]),
            ("flags", [{}]),
            ("tracking", {}),
            ("details", {}),
            ("details", {"field": {"name": "Field", "type": "text"}}),
            ("details", {"field": {"name": "Field", "type": "invalid"}}),
        ):
            report = security_report([{"id": ACCEPTED_ID, "severity": "Medium"}])
            report["vulnerabilities"][0][field] = value
            invalid_cases.append(("gl-sast-report.json", report))
        for end_line in ({}, 10_000_001):
            for filename, report in (
                (
                    "gl-sast-report.json",
                    security_report([{"id": ACCEPTED_ID, "severity": "Medium"}]),
                ),
                (
                    "gl-secret-detection-report.json",
                    secret_report([{"id": SECRET_ID, "severity": "Critical"}]),
                ),
            ):
                report["vulnerabilities"][0]["location"]["end_line"] = end_line
                invalid_cases.append((filename, report))
        module = load_summary_module()
        for filename, replacement in invalid_cases:
            with self.subTest(filename=filename, replacement=replacement):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    baseline = write_clean_reports(root)
                    write_json(root / filename, replacement)
                    summary = module.build_summary(root, baseline, SOURCE_COMMIT)
                    self.assertEqual("INCOMPLETE", summary["overall_status"])
                    self.assertEqual([filename], summary["invalid_reports"])
                    serialized = json.dumps(summary)
                    self.assertNotIn("secret-marker", serialized)
                    self.assertLess(len(serialized), 1000)

    def test_junit_and_coverage_reject_inconsistent_or_unsafe_values(self):
        module = load_summary_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_clean_reports(root)
            report_path = root / "test-results" / "suite.xml"
            report_path.write_text(
                "<testsuites>"
                '<testsuite tests="1" failures="0" errors="0" skipped="0"/>'
                '<testsuite tests="2" failures="0" errors="0" skipped="0"/>'
                "</testsuites>",
                encoding="utf-8",
            )
            self.assertEqual(3, module._test_counts(root)["tests"])

            for invalid_junit in (
                '<testsuite tests="1"/>',
                '<testsuite tests="1" failures="0" errors="0" skipped="-1"/>',
                (
                    '<testsuite tests="'
                    + "1"
                    + "0" * 4000
                    + '" failures="0" errors="0" skipped="0"/>'
                ),
                '<testsuite tests="1" failures="1" errors="1" skipped="0"/>',
                '<testsuite tests="1" failures="0" errors="0" skipped="0">'
                "<testcase><failure/></testcase></testsuite>",
                '<!DOCTYPE testsuite [<!ENTITY probe "unsafe">]>'
                '<testsuite tests="1" failures="0" errors="0" skipped="0">'
                '<testcase name="&probe;"/></testsuite>',
                '<testsuites><testsuite tests="2" failures="1" errors="0" skipped="0">'
                '<testcase name="direct"><failure/></testcase>'
                '<testsuite tests="1" failures="0" errors="0" skipped="0"/>'
                "</testsuite></testsuites>",
            ):
                with self.subTest(junit=invalid_junit):
                    report_path.write_text(invalid_junit, encoding="utf-8")
                    with self.assertRaises(module.InvalidReport):
                        module._test_counts(root)

            coverage_path = root / "coverage.xml"
            for invalid_coverage in (
                '<coverage line-rate="nan" lines-covered="1" lines-valid="1"/>',
                '<coverage line-rate="0" lines-covered="-1" lines-valid="1"/>',
                '<coverage line-rate="1" lines-covered="2" lines-valid="1"/>',
                '<coverage line-rate="0.5" lines-covered="9" lines-valid="10"/>',
            ):
                with self.subTest(coverage=invalid_coverage):
                    coverage_path.write_text(invalid_coverage, encoding="utf-8")
                    with self.assertRaises(module.InvalidReport):
                        module._coverage(root)

    def test_malformed_xml_still_writes_sanitized_incomplete_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "test-results"
            results.mkdir()
            (results / "suite.xml").write_text(
                '<testsuite tests="1" failures="0" errors="0" skipped="0"/>',
                encoding="utf-8",
            )
            (root / "coverage.xml").write_text("<coverage secret-invalid-xml", encoding="utf-8")
            write_shard_verification(root, 1)
            write_json(root / "gl-code-quality-report.json", [])
            write_json(
                root / "dependency-audit.json",
                {"dependencies": [{"name": "package", "version": "1.0", "vulns": []}], "fixes": []},
            )
            write_json(root / "gl-sast-report.json", security_report([]))
            write_json(root / "gl-secret-detection-report.json", secret_report([]))
            baseline = root / "sast-baseline.json"
            write_json(baseline, sast_baseline([]))
            json_output = root / "ci-summary.json"
            text_output = root / "ci-summary.txt"

            module = load_summary_module()
            exit_code = module.main(
                [
                    "--root",
                    str(root),
                    "--baseline",
                    str(baseline),
                    "--expected-baseline-source",
                    SOURCE_COMMIT,
                    "--json-output",
                    str(json_output),
                    "--text-output",
                    str(text_output),
                ]
            )

            self.assertEqual(1, exit_code)
            stored = json.loads(json_output.read_text(encoding="utf-8"))
            self.assertEqual("INCOMPLETE", stored["overall_status"])
            self.assertEqual(["coverage.xml"], stored["invalid_reports"])
            rendered = text_output.read_text(encoding="utf-8")
            self.assertIn("Invalid reports: coverage.xml", rendered)
            self.assertNotIn("secret-invalid-xml", rendered)


if __name__ == "__main__":
    unittest.main()
