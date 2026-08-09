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


class GitLabPipelineContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pipeline = CI_CONFIG.read_text(encoding="utf-8")
        cls.sections = top_level_sections(cls.pipeline)

    def test_pipeline_targets_merge_requests_and_main_without_deploying(self):
        workflow = block(self.sections, "workflow")
        self.assertIn('CI_PIPELINE_SOURCE == "merge_request_event"', workflow)
        self.assertIn("CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH", workflow)
        self.assertIn('CI_PIPELINE_SOURCE == "web"', workflow)

        stages = block(self.sections, "stages")
        self.assertEqual(
            re.findall(r"^  - ([a-z-]+)$", stages, flags=re.MULTILINE),
            ["validate", "test", "quality", "security"],
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

    def test_expected_top_level_jobs_and_controls_are_present(self):
        required = {
            "workflow", "stages", "include", "variables", ".default_project_job",
            "python_syntax", "full_test_suite", "python_static_analysis",
            "dependency_audit", "semgrep-sast", "secret_detection",
        }
        self.assertEqual(set(self.sections), required)


if __name__ == "__main__":
    unittest.main()
