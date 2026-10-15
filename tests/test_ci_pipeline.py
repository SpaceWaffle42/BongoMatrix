from pathlib import Path
import ast
import os
import re
import subprocess
import tempfile
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

    def test_pipeline_targets_merge_requests_and_all_branches_with_protected_physical_gate(self):
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
            ["validate", "test", "quality", "security", "summary", "physical"],
        )
        self.assertNotIn("deploy", self.sections)
        for name in self.sections:
            if name == "physical_device_acceptance":
                continue
            self.assertNotRegex(
                block(self.sections, name),
                r"\b(?:systemctl|scp|rsync|ssh)\b",
                name,
            )
        physical = block(self.sections, "physical_device_acceptance")
        self.assertIn("ssh", physical)
        self.assertNotRegex(physical, r"\b(?:scp|rsync|systemctl|sudo)\b")
        self.assertNotRegex(physical, r"\b(?:git archive|tar|candidate|PYTHONPATH)\b")
        self.assertIn("resource_group: matrix-physical-device", physical)
        self.assertIn("retry: 0", physical)
        active_lines = "\n".join(
            line for line in self.pipeline.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        self.assertNotIn("Auto-DevOps.gitlab-ci.yml", active_lines)

    def test_physical_job_rules_are_exactly_protected_default_branch_push(self):
        physical = block(self.sections, "physical_device_acceptance")
        rule_lines = [line.strip() for line in physical.splitlines() if line.strip().startswith(("- if:", "- when:"))]
        self.assertEqual(
            rule_lines,
            [
                '- if: \'$CI_PIPELINE_SOURCE == "push" && $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH && $CI_COMMIT_REF_PROTECTED == "true"\'',
                "- when: never",
            ],
        )
        self.assertRegex(physical, r"needs:\n\s+- job: ci_summary\n\s+artifacts: false")
        self.assertIn("resource_group: matrix-physical-device", physical)
        self.assertIn("interruptible: false", physical)
        self.assertIn("retry: 0", physical)
        self.assertIn("timeout: 3m", physical)
        self.assertIn("ServerAliveInterval=10", physical)
        self.assertIn("ServerAliveCountMax=2", physical)

    def test_physical_job_drops_privilege_and_bounds_remote_output_with_primary_status(self):
        physical = block(self.sections, "physical_device_acceptance")
        self.assertLess(physical.index("apt-get install"), physical.index("setpriv"))
        self.assertIn("useradd --uid 65532", physical)
        self.assertNotRegex(physical, r"(?m)^\s+python scripts/")
        self.assertRegex(physical, r"setpriv --reuid=65532 --regid=65532 --clear-groups python scripts/verify_physical_device_report\.py")
        self.assertIn("head -c 4097", physical)
        self.assertIn("PIPESTATUS[0]", physical)
        self.assertIn("overflow_status", physical)
        self.assertNotIn("cat physical-device-report.txt", physical)
        self.assertIn('primary_status="$acceptance_status"', physical)
        self.assertIn('test "$primary_status" -eq 0', physical)
        self.assertLess(physical.index('primary_status="$acceptance_status"'), physical.index('primary_status="$overflow_status"'))

    def test_install_documentation_supplies_all_reviewed_artifact_digests_without_live_self_certification(self):
        install = (ROOT / "deploy" / "matrix-ci-physical-install.md").read_text(encoding="utf-8")
        names = (
            "matrix-ci-physical-verifier", "matrix-ci-physical-dispatcher",
            "matrix-ci-install-rgb-bindings", "verify-matrix-ci-physical-install",
            "matrix-device-restart-broker", "matrix-ci-physical.service",
            "matrix-ci-physical.path", "matrix-device-restart.service",
            "matrix-device-restart.path", "matrix-web.service",
            "matrix-ci-physical.conf", "matrix-display.conf",
            "matrix-device-restart.conf",
        )
        self.assertEqual(install.count("--expected-artifact-sha256"), len(names))
        for name in names:
            self.assertRegex(
                install,
                rf"--expected-artifact-sha256 {re.escape(name)}=\"?\$[A-Z0-9_]+\"?",
            )
        self.assertIn("separately reviewed release input", install)
        self.assertIn("Never compute these expected digests from the installed destinations", install)
        self.assertIn("unique sibling staging directory", install)
        self.assertIn("atomic no-replace publication", install)
        self.assertIn("ordinary pre-publication failures are cleaned up for a safe retry", install)
        self.assertLess(
            install.index("sha256sum deploy/matrix_ci_physical_verifier.py"),
            install.index("install -o root -g root -m 0755 deploy/matrix_ci_physical_verifier.py"),
        )

    def test_physical_secrets_are_protected_and_file_typed_by_documented_contract(self):
        physical = block(self.sections, "physical_device_acceptance")
        install = (ROOT / "deploy" / "matrix-ci-physical-install.md").read_text(encoding="utf-8")

        self.assertIn('"$MATRIX_DEVICE_TARGET"', physical)
        self.assertNotIn("MATRIX_DEVICE_SSH_TARGET", physical + install)
        self.assertIn("MATRIX_DEVICE_TARGET", install)
        for variable in ("MATRIX_DEVICE_SSH_KEY", "MATRIX_DEVICE_KNOWN_HOSTS"):
            self.assertIn(variable, physical)
            self.assertRegex(install, rf"{variable}.*protected.*FILE")
        self.assertRegex(install, r"MATRIX_DEVICE_TARGET.*protected.*variable")
        self.assertIn("never available to merge-request or branch jobs", install)
        self.assertNotRegex(physical, r"(?m)^\s*(?:variables:|MATRIX_DEVICE_[A-Z_]+:)\s*$")

    def test_full_test_job_exercises_optional_browser_and_media_paths(self):
        job = block(self.sections, "full_test_suite")
        self.assertIn("image: node:22-bookworm", job)
        self.assertIn("MATRIX_CHROMIUM: /usr/bin/chromium", job)
        self.assertIn("chromium ffmpeg", job)
        self.assertIn("openssh-server", job)
        self.assertIn("install -d -m 0755 /run/sshd", job)
        self.assertLess(
            job.index("install -d -m 0755 /run/sshd"),
            job.index("coverage run"),
        )
        self.assertIn("python3-pil", job)
        self.assertIn("python3-opencv", job)
        self.assertIn("install -o root -g root -m 0755 /usr/local/bin/node /usr/bin/node", job)
        self.assertIn("test ! -L /usr/bin/node", job)
        self.assertIn("cmp -s /usr/local/bin/node /usr/bin/node", job)
        self.assertIn(
            "/usr/bin/node -e \"if (typeof WebSocket !== 'function') process.exit(1)\"",
            job,
        )
        self.assertLess(
            job.index("install -o root -g root -m 0755 /usr/local/bin/node /usr/bin/node"),
            job.index("coverage run"),
        )
        self.assertLess(job.index("typeof WebSocket"), job.index("coverage run"))
        self.assertIn(
            "coverage run --source=. --omit='tests/*' scripts/ci_shard.py run"
            ' --index "$CI_NODE_INDEX" --total "$CI_NODE_TOTAL"',
            job,
        )
        self.assertIn('test_status=$?', job)
        self.assertIn('exit "$test_status"', job)
        self.assertRegex(job, r"reports:\n\s+junit:")
        self.assertIn("when: always", job)
        self.assertIn("coverage_report:", block(self.sections, "test_results"))

    def test_collection_preflight_precedes_full_canonical_suite(self):
        job = block(self.sections, "full_test_suite")
        preflight = '.venv-ci/bin/python scripts/ci_collection.py --output "shards/collection-$CI_NODE_INDEX.json"'
        self.assertIn(preflight, job)
        self.assertLess(job.index(preflight), job.index("set +e"))
        self.assertLess(job.index(preflight), job.index("coverage run"))
        self.assertIn("- shards/", job)
        self.assertNotIn("allow_failure", job)

    def test_sharded_suite_is_joined_by_an_exact_partition_proof(self):
        job = block(self.sections, "full_test_suite")
        self.assertRegex(job, r"\n  parallel: [2-9]\n")
        self.assertIn("COVERAGE_RCFILE: ci/coveragerc", job)
        rc = (ROOT / "ci/coveragerc").read_text(encoding="utf-8")
        self.assertIn("parallel = True", rc)
        self.assertIn("data_file = coverage-data/.coverage", rc)
        self.assertIn("- coverage-data/", job)

        join = block(self.sections, "test_results")
        self.assertIn("when: always", join)
        self.assertRegex(join, r"- job: full_test_suite\n\s+artifacts: true")
        verify = "python scripts/ci_shard.py verify --collections shards --manifests shards --results test-results"
        self.assertIn(verify, join)
        self.assertLess(join.index(verify), join.index("coverage combine coverage-data"))
        self.assertLess(join.index("coverage combine"), join.index("coverage xml -o coverage.xml"))
        self.assertIn("- shard-verification.json", join)

    def test_quality_and_security_evidence_does_not_wait_for_the_suite(self):
        for name in ("python_static_analysis", "dependency_audit", "semgrep-sast", "secret_detection"):
            self.assertIn("\n  needs: []\n", "\n" + block(self.sections, name), name)
        summary = block(self.sections, "ci_summary")
        self.assertRegex(summary, r"- job: test_results\n\s+artifacts: true")

    def test_full_test_job_provisions_the_repository_venv_contract(self):
        job = block(self.sections, "full_test_suite")
        self.assertIn("rm -rf -- .venv .venv-ci", job)
        self.assertIn("python3 -m venv .venv-ci", job)
        self.assertIn("ln -sT .venv-ci .venv", job)
        self.assertIn('test "$(readlink .venv)" = ".venv-ci"', job)
        self.assertLess(
            job.index("rm -rf -- .venv .venv-ci"),
            job.index("python3 -m venv .venv-ci"),
        )
        self.assertLess(
            job.index("ln -sT .venv-ci .venv"),
            job.index('test "$(readlink .venv)" = ".venv-ci"'),
        )
        self.assertLess(job.index('test "$(readlink .venv)" = ".venv-ci"'), job.index("coverage run"))

    def test_full_test_job_replaces_stale_repository_venv_state(self):
        job = block(self.sections, "full_test_suite")
        setup = [
            line.strip().removeprefix("- ")
            for line in job.splitlines()
            if line.strip().startswith("- ")
            and any(token in line for token in (".venv-ci", "readlink .venv"))
            and "pip install" not in line
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old-venv"
            (old / "bin").mkdir(parents=True)
            stale_python = old / "bin/python"
            stale_python.write_text("#!/bin/sh\nexit 99\n", encoding="ascii")
            stale_python.chmod(0o755)
            (root / ".venv").symlink_to("old-venv", target_is_directory=True)
            stale_ci = root / ".venv-ci"
            stale_ci.mkdir()
            (stale_ci / "stale-marker").write_text("stale", encoding="ascii")
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            fake_python = fake_bin / "python3"
            fake_python.write_text(
                "#!/bin/sh\n"
                "test \"$1 $2\" = '-m venv'\n"
                "mkdir -p \"$3/bin\"\n"
                "printf '#!/bin/sh\\nexit 0\\n' > \"$3/bin/python\"\n"
                "chmod 0755 \"$3/bin/python\"\n",
                encoding="ascii",
            )
            fake_python.chmod(0o755)

            completed = subprocess.run(
                ["/bin/sh", "-ceu", "\n".join(setup)],
                cwd=root, text=True, capture_output=True, check=False,
                env={
                    **os.environ,
                    "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "CI_NODE_INDEX": "1",
                    "CI_NODE_TOTAL": "2",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue((root / ".venv").is_symlink())
            self.assertEqual(os.readlink(root / ".venv"), ".venv-ci")
            self.assertTrue(os.access(root / ".venv/bin/python", os.X_OK))
            self.assertFalse((root / ".venv-ci/stale-marker").exists())
            self.assertFalse((old / ".venv-ci").exists())

    def test_acceptance_browser_regression_uses_the_ci_configured_chromium(self):
        acceptance_tests = (ROOT / "tests/test_priority_1_3_acceptance.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(
            'mock.patch.dict(os.environ, {"MATRIX_CHROMIUM": "/usr/bin/google-chrome"})',
            acceptance_tests,
        )

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

    def test_physical_verifier_has_no_optimized_away_assertions(self):
        path = ROOT / "scripts" / "verify_priority_1_5_foundation_physical.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assertions = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Assert)]
        self.assertEqual(assertions, [])

    def test_expected_top_level_jobs_and_controls_are_present(self):
        required = {
            "workflow", "stages", "include", "variables", ".default_project_job",
            "python_syntax", "full_test_suite", "python_static_analysis",
            "dependency_audit", "semgrep-sast", "secret_detection",
            "test_results", "ci_summary", "physical_device_acceptance",
            "non_essential_browser_tests",
        }
        self.assertEqual(set(self.sections), required)

    def test_non_essential_browser_tests_run_separately_with_two_passes_and_never_block(self):
        blocking = block(self.sections, "full_test_suite")
        self.assertIn('MATRIX_CI_SKIP_NON_ESSENTIAL: "1"', blocking)
        self.assertNotIn("allow_failure", blocking)
        job = block(self.sections, "non_essential_browser_tests")
        self.assertIn("extends: full_test_suite", job)
        self.assertIn('MATRIX_CI_SKIP_NON_ESSENTIAL: "0"', job)
        self.assertIn("allow_failure: true", job)
        # Inherits full_test_suite's parallel; must run once, not per shard.
        self.assertIn("parallel: 1", job)
        self.assertIn("when: always", job)
        # At most two passes per pipeline: the first run plus one retry, including on test failure.
        self.assertRegex(job, r"retry:\n\s+max: 1\n\s+when:\n\s+- script_failure")
        self.assertRegex(job, r"needs:\n\s+- job: full_test_suite\n\s+artifacts: false")
        self.assertIn("scripts/ci_collection.py --print-non-essential", job)
        self.assertIn('test -n "$non_essential"', job)
        self.assertIn("-m xmlrunner -o test-results $non_essential", job)
        self.assertIn('exit "$test_status"', job)
        # The required summary gate never depends on the quarantined job.
        self.assertNotIn("non_essential_browser_tests", block(self.sections, "ci_summary"))


if __name__ == "__main__":
    unittest.main()
