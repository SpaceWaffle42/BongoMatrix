from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
CI_CONFIG = ROOT / ".gitlab-ci.yml"
TOP_LEVEL_KEY = re.compile(r"^([A-Za-z0-9_.-]+):(?:\s.*)?$")


def top_level_sections(source):
    """Split this deliberately simple CI file into effective top-level blocks."""
    sections = {}
    current = None
    for raw_line in source.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        match = TOP_LEVEL_KEY.fullmatch(line)
        if match:
            current = match.group(1)
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return sections


def block(sections, name):
    return "\n".join(sections[name])


def active_cache_keys(text):
    return [
        line
        for line in text.splitlines()
        if not line.lstrip().startswith("#")
        and re.match(r"^\s*cache\s*:", line)
    ]


def workflow_rules(sections):
    rules = []
    for line in sections["workflow"]:
        stripped = line.strip()
        if stripped == "rules:":
            continue
        if stripped.startswith("- if: "):
            expression = stripped.removeprefix("- if: ").strip("'")
            rules.append([expression, "on_success"])
        elif stripped.startswith("when: "):
            rules[-1][1] = stripped.removeprefix("when: ")
        elif stripped.startswith("- when: "):
            rules.append([None, stripped.removeprefix("- when: ")])
        else:
            raise AssertionError(f"Unsupported workflow rule syntax: {line}")
    return [tuple(rule) for rule in rules]


def workflow_allows(rules, source, branch=False, open_merge_request=False):
    conditions = {
        '$CI_PIPELINE_SOURCE == "merge_request_event"': source == "merge_request_event",
        '$CI_PIPELINE_SOURCE == "schedule"': source == "schedule",
        '$CI_PIPELINE_SOURCE == "push" && $CI_COMMIT_BRANCH && $CI_OPEN_MERGE_REQUESTS': (
            source == "push" and branch and open_merge_request
        ),
        '$CI_PIPELINE_SOURCE == "push" && $CI_COMMIT_BRANCH': (
            source == "push" and branch
        ),
    }
    for expression, action in rules:
        if expression is None or conditions.get(expression, False):
            return action != "never"
    return False


class GitLabPipelineContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pipeline = CI_CONFIG.read_text(encoding="utf-8")
        cls.sections = top_level_sections(cls.pipeline)

    def test_pipeline_targets_merge_requests_and_all_branches_without_deploying(self):
        rules = workflow_rules(self.sections)
        self.assertEqual(
            rules,
            [
                ('$CI_PIPELINE_SOURCE == "merge_request_event"', "on_success"),
                ('$CI_PIPELINE_SOURCE == "schedule"', "on_success"),
                (
                    '$CI_PIPELINE_SOURCE == "push" && $CI_COMMIT_BRANCH && $CI_OPEN_MERGE_REQUESTS',
                    "never",
                ),
                ('$CI_PIPELINE_SOURCE == "push" && $CI_COMMIT_BRANCH', "on_success"),
                (None, "never"),
            ],
        )
        cases = [
            ("merge_request_event", False, False, True),
            ("push", True, False, True),
            ("push", True, True, False),
            ("push", False, False, False),
            ("web", True, False, False),
            ("schedule", True, False, True),
            ("api", True, False, False),
            ("trigger", True, False, False),
            ("parent_pipeline", False, False, False),
        ]
        for source, branch, open_mr, expected in cases:
            with self.subTest(source=source, branch=branch, open_mr=open_mr):
                self.assertEqual(
                    workflow_allows(rules, source, branch, open_mr),
                    expected,
                )

        stages = block(self.sections, "stages")
        self.assertEqual(
            re.findall(r"^  - ([a-z-]+)$", stages, flags=re.MULTILINE),
            ["validate", "test", "quality", "security", "summary"],
        )
        self.assertNotIn("deploy", self.sections)
        active_lines = "\n".join(
            line for line in self.pipeline.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        self.assertNotRegex(active_lines, r"\b(?:systemctl|scp|rsync|ssh)\b")
        self.assertNotIn("Auto-DevOps.gitlab-ci.yml", active_lines)

    def test_full_test_job_exercises_optional_browser_and_media_paths(self):
        job = block(self.sections, "full_test_suite")
        self.assertIn("image: node:22-bookworm", job)
        self.assertIn("MATRIX_CHROMIUM: /usr/bin/chromium", job)
        self.assertIn("chromium ffmpeg", job)
        self.assertIn("python3-pil", job)
        self.assertIn("python3-opencv", job)
        self.assertIn(
            "coverage run --source=. --omit='tests/*' -m xmlrunner discover -s tests",
            job,
        )
        self.assertIn('test_status=$?', job)
        self.assertIn('exit "$test_status"', job)
        self.assertRegex(job, r"reports:\n\s+junit:")
        self.assertIn("coverage_report:", job)
        self.assertIn("when: always", job)

    def test_pipeline_publishes_quality_and_security_evidence(self):
        variables = block(self.sections, "variables")
        self.assertIn('AST_ENABLE_MR_PIPELINES: "true"', variables)

        includes = block(self.sections, "include")
        self.assertIn("Jobs/SAST.gitlab-ci.yml", includes)
        self.assertIn("Jobs/Secret-Detection.gitlab-ci.yml", includes)

        static = block(self.sections, "python_static_analysis")
        self.assertIn("--output-format=gitlab", static)
        self.assertIn("codequality: gl-code-quality-report.json", static)

        audit = block(self.sections, "dependency_audit")
        self.assertIn("pip-audit -r requirements.txt", audit)
        self.assertIn("dependency-audit.json", audit)

        for name in ("semgrep-sast", "secret_detection"):
            security_job = block(self.sections, name)
            self.assertIn("stage: security", security_job)
            self.assertIn("- matrix-docker", security_job)
            self.assertIn("interruptible: true", security_job)

    def test_project_jobs_inherit_cancelation_and_runner_without_cache_volumes(self):
        hidden_default = block(self.sections, ".default_project_job")
        self.assertIn("image: python:3.12-bookworm", hidden_default)
        self.assertIn("- matrix-docker", hidden_default)
        self.assertIn("interruptible: true", hidden_default)
        self.assertEqual([], active_cache_keys(self.pipeline))

        adversarial = self.pipeline.replace(
            "full_test_suite:\n",
            "full_test_suite:\n  cache:\n    paths:\n      - .cache/pip/\n",
            1,
        )
        self.assertEqual(["  cache:"], active_cache_keys(adversarial))

        for name in (
            "python_syntax",
            "full_test_suite",
            "python_static_analysis",
            "dependency_audit",
        ):
            self.assertIn("extends: .default_project_job", block(self.sections, name))

    def test_summary_job_reduces_reports_to_bounded_gitlab_artifacts(self):
        summary = block(self.sections, "ci_summary")
        self.assertIn("stage: summary", summary)
        self.assertIn("when: always", summary)
        for job in (
            "full_test_suite",
            "python_static_analysis",
            "dependency_audit",
            "semgrep-sast",
            "secret_detection",
        ):
            self.assertRegex(summary, rf"- job: {job}\n\s+artifacts: true")
        for optional_job in ("semgrep-sast", "secret_detection"):
            self.assertRegex(
                summary,
                rf"- job: {optional_job}\n\s+artifacts: true\n\s+optional: true",
            )
        self.assertIn("scripts/ci_summary.py", summary)
        self.assertIn("python -m pip install --no-cache-dir defusedxml==0.7.1", summary)
        self.assertIn("ci/sast-baseline.json", summary)
        self.assertIn(
            "--expected-baseline-source 8b9994024098320fbea99d92c117036c918fede4",
            summary,
        )
        self.assertIn("ci-summary.json", summary)
        self.assertIn("ci-summary.txt", summary)
        self.assertIn("expire_in: 14 days", summary)

    def test_expected_top_level_jobs_and_controls_are_present(self):
        required = {
            "workflow", "stages", "include", "variables", ".default_project_job",
            "python_syntax", "full_test_suite", "python_static_analysis",
            "dependency_audit", "semgrep-sast", "secret_detection",
            "ci_summary",
        }
        self.assertEqual(set(self.sections), required)


if __name__ == "__main__":
    unittest.main()
