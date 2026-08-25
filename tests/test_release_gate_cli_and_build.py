import argparse
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deploy import guarded_priority_1_3_release as guarded
from deploy import release_bundle
from deploy import verify_priority_1_3_acceptance as acceptance
from deploy.release_bundle import ReleaseManifestError, validate_release_manifest
from scripts import verify_priority_1_3_remote as remote_verifier


class ReleaseTreePreparationTests(unittest.TestCase):
    def _committed_repository(self, root):
        repository = root / "repository"
        sources = {
            "acceptance_artifacts.py": "VALUE = 'acceptance'\n",
            "release_argparse.py": "VALUE = 'parser'\n",
            "webapp.py": 'APPLICATION_VERSION = "9.8.7-test"\n',
            "requirements.txt": "Flask==3.1.2\n",
            "app/__init__.py": "\n",
            "app/runtime.py": "APPLICATION = object()\n",
            "app/wsgi.py": "from app.runtime import APPLICATION\napp = APPLICATION\n",
            "deploy/release_bundle.py": "# exact staged release tool\n",
            "deploy/guarded_priority_1_3_release.py": "# guarded release\n",
            "deploy/verify_priority_1_3_acceptance.py": "# acceptance runner\n",
            "scripts/verify_priority_1_3_remote.py": "#!/usr/bin/env python3\n# remote verifier\n",
        }
        repository.mkdir()
        for relative, content in sources.items():
            path = repository / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        (repository / "scripts/verify_priority_1_3_remote.py").chmod(0o755)
        subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.email", "release@example.invalid"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Release Test"], cwd=repository, check=True)
        subprocess.run(["git", "add", "--", *sorted(sources)], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "exact release fixture"], cwd=repository, check=True)
        return repository, self._head(repository)

    @staticmethod
    def _head(repository):
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repository, check=True,
            text=True, capture_output=True,
        ).stdout.strip()

    @staticmethod
    def _argv(project, repository, source_sha, stage):
        return [
            "prepare-release-tree", "--repository", str(repository),
            "--source-commit", source_sha, "--stage", str(stage),
            "--python", sys.executable,
            "--uid", str(os.getuid()), "--gid", str(os.getgid()),
        ]

    def test_release_tree_fixtures_use_tmpdir_and_preserve_checkout_status(self):
        project = Path(__file__).resolve().parents[1]
        before = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z"],
            cwd=project,
            check=True,
            capture_output=True,
        ).stdout

        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            root = Path(directory)
            self.assertFalse(root.is_relative_to(project))
            repository, source_sha = self._committed_repository(root)
            stage = root / "stage"
            self.assertEqual(
                release_bundle.main(self._argv(project, repository, source_sha, stage)),
                0,
            )

        after = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z"],
            cwd=project,
            check=True,
            capture_output=True,
        ).stdout
        self.assertEqual(after, before)
        forbidden = "TemporaryDirectory(" + "dir=project)"
        self.assertNotIn(forbidden, Path(__file__).read_text())

    def test_exact_commit_export_prepares_inner_manifest_before_outer_spec(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            root = Path(directory)
            repository, source_sha = self._committed_repository(root)
            stage = root / "stage"
            self.assertEqual(release_bundle.main(self._argv(project, repository, source_sha, stage)), 0)
            release_root = stage / "release"
            expected = {
                "acceptance_artifacts.py", "app/wsgi.py", "app/runtime.py",
                "scripts/verify_priority_1_3_remote.py", ".matrix-release.json",
            }
            self.assertTrue(expected.issubset({
                path.relative_to(release_root).as_posix()
                for path in release_root.rglob("*") if path.is_file()
            }))
            self.assertTrue(stat.S_IMODE(
                (release_root / "scripts/verify_priority_1_3_remote.py").stat().st_mode
            ) & 0o111)
            inner = json.loads((release_root / ".matrix-release.json").read_text(encoding="utf-8"))
            self.assertEqual(inner["source_sha"], source_sha)
            self.assertEqual(
                inner["candidate_tracked_files"],
                sorted(entry["path"] for entry in inner["files"]),
            )
            spec_path = stage / "release-spec.json"
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            self.assertIn(".matrix-release.json", {entry["path"] for entry in spec["files"]})
            self.assertEqual(validate_release_manifest(release_root, spec_path), spec)
            self.assertEqual(
                (stage / "release_bundle.py").read_bytes(),
                (release_root / "deploy/release_bundle.py").read_bytes(),
            )

    def test_validated_exact_commit_export_supports_legacy_tarfile_extractall(self):
        project = Path(__file__).resolve().parents[1]
        original_extractall = release_bundle.tarfile.TarFile.extractall

        def legacy_extractall(archive, path=".", members=None):
            return original_extractall(archive, path, members)

        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            root = Path(directory)
            repository, source_sha = self._committed_repository(root)
            stage = root / "stage"
            with mock.patch.object(
                release_bundle.tarfile.TarFile, "extractall", legacy_extractall,
            ):
                self.assertEqual(
                    release_bundle.main(
                        self._argv(project, repository, source_sha, stage)
                    ),
                    0,
                )
            self.assertTrue((stage / "release-spec.json").is_file())

    def test_builder_remote_proof_matches_the_approved_outer_release_spec(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            root = Path(directory)
            repository, source_sha = self._committed_repository(root)
            stage = root / "stage"
            self.assertEqual(
                release_bundle.main(self._argv(project, repository, source_sha, stage)), 0,
            )
            release_root = stage / "release"
            proof = remote_verifier._protected(release_root)
            arguments = argparse.Namespace(
                attempt_id="a" * 32,
                acceptance_operation_id="1" * 64,
                expected_sha=source_sha,
                expected_version="9.8.7-test",
                release_spec=str(stage / "release-spec.json"),
                release_spec_digest=__import__("hashlib").sha256(
                    (stage / "release-spec.json").read_bytes()
                ).hexdigest(),
            )
            evidence_id = "e" * 64
            payload = {
                "schema": 1, "check": "protected-parity",
                "attempt_id": arguments.attempt_id,
                "acceptance_operation_id": arguments.acceptance_operation_id,
                "evidence_id": evidence_id,
                **proof,
            }

            accepted = acceptance._validated_payload(
                "protected-parity", json.dumps(payload), arguments, evidence_id,
            )

            self.assertIn(".matrix-release.json", accepted["files"])
            self.assertEqual(set(accepted["files"]), {
                entry["path"]
                for entry in json.loads(
                    (stage / "release-spec.json").read_text(encoding="utf-8")
                )["files"]
            })

    def test_required_file_only_untracked_in_worktree_is_rejected_at_exact_commit_export(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            root = Path(directory)
            repository, _ = self._committed_repository(root)
            verifier = repository / "scripts/verify_priority_1_3_remote.py"
            subprocess.run(["git", "rm", "-q", "scripts/verify_priority_1_3_remote.py"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "omit verifier"], cwd=repository, check=True)
            source_sha = self._head(repository)
            verifier.parent.mkdir(parents=True, exist_ok=True)
            verifier.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            verifier.chmod(0o755)
            with self.assertRaisesRegex(ReleaseManifestError, "exact commit export"):
                release_bundle.main(self._argv(project, repository, source_sha, root / "stage"))

    def test_wsgi_import_failure_prevents_outer_spec_freeze(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            root = Path(directory)
            repository, _ = self._committed_repository(root)
            (repository / "app/wsgi.py").write_text("import missing_runtime_dependency\n", encoding="utf-8")
            subprocess.run(["git", "add", "app/wsgi.py"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "broken WSGI"], cwd=repository, check=True)
            stage = root / "stage"
            with self.assertRaisesRegex(ReleaseManifestError, "WSGI"):
                release_bundle.main(self._argv(project, repository, self._head(repository), stage))
            self.assertFalse((stage / "release-spec.json").exists())

    def test_preseeded_inner_manifest_is_rejected_as_order_inversion(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            root = Path(directory)
            repository, _ = self._committed_repository(root)
            (repository / ".matrix-release.json").write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "add", ".matrix-release.json"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "preseed generated manifest"], cwd=repository, check=True)
            with self.assertRaisesRegex(ReleaseManifestError, "fresh|order"):
                release_bundle.main(self._argv(
                    project, repository, self._head(repository), root / "stage"
                ))


class DuplicateReleaseCliTests(unittest.TestCase):
    def _release_bundle_cases(self):
        common = [
            "--release-root", "/release", "--destination-root", "/destination",
            "--bundle", "/bundle", "--release-manifest", "/release.json",
            "--backup-manifest", "/backup.json", "--service", "matrix-web.service",
        ]
        return {
            "prepare-acceptance-manifest": ["--release-root", "/release", "--source-sha", "a" * 40,
                                            "--candidate-tracked-files-json", "[]"],
            "prepare-release-tree": ["--repository", "/repo", "--source-commit", "a" * 40,
                                     "--stage", "/stage", "--python", "/python", "--uid", "0", "--gid", "0"],
            "backup": [*common, "--release-spec", "/spec.json"],
            "preflight": [*common, "--release-spec", "/spec.json"],
            "deploy": [*common, "--release-spec", "/spec.json"],
            "rollback": common,
            "verify": [*common, "--mode", "deploy"],
            "cleanup": common,
            "recover": [*common, "--initial-service-state", "active", "--health-url", "http://127.0.0.1/",
                        "--health-status", "302", "--health-timeout-seconds", "30"],
        }

    @staticmethod
    def _guarded_run():
        return [
            "--boundary", "b1", "--ssh-target", "fixture@host", "--ssh-identity-file", "/identity",
            "--ssh-known-hosts-file", "/known", "--stage", "/stage", "--bundle", "/bundle",
            "--destination-root", "/destination", "--service", "matrix-web.service",
            "--initial-service-state", "active", "--health-url", "http://127.0.0.1/",
            "--health-status", "302", "--health-timeout-seconds", "30",
            "--attempt-budget-seconds", "6600", "--commit-reserve-seconds", "60",
            "--watchdog-seconds", "7200", "--expected-sha", "a" * 40,
            "--expected-version", "new", "--previous-sha", "b" * 40,
            "--previous-version", "old", "--transaction-version", "1",
            "--transaction-registry-root", "/transactions", "--destination-identity", "matrix-production",
            "--release-stamp", "stamp", "--release-manifest-digest", "c" * 64,
            "--backup-manifest-digest", "d" * 64, "--release-spec-digest", "e" * 64,
            "--session-cookie-file", "/cookie", "--evidence-dir", "/evidence",
            "--gitlab-project", "2", "--gitlab-mr", "73", "--gitlab-token-file", "/token",
            "--chromium", "/chromium",
        ]

    @staticmethod
    def _acceptance():
        return [
            "--boundary", "b1", "--mode", "deploy", "--ssh-target", "fixture@host",
            "--ssh-identity-file", "/identity", "--ssh-known-hosts-file", "/known",
            "--app-root", "/app", "--expected-sha", "a" * 40, "--expected-version", "new",
            "--session-cookie-file", "/cookie", "--evidence-file", "/evidence",
            "--attempt-id", "a" * 32, "--acceptance-operation-id", "b" * 64,
            "--attempt-registry", "/registry",
            "--release-spec", "/stage/release-spec.json", "--release-spec-digest", "e" * 64,
        ]

    @staticmethod
    def _remote():
        return [
            "--check", "protected-parity", "--mode", "deploy", "--app-root", "/app",
            "--attempt-id", "a" * 32, "--acceptance-operation-id", "b" * 64,
            "--evidence-id", "c" * 64, "--expected-sha", "d" * 40,
            "--expected-version", "new",
        ]

    def _assert_every_value_option_is_store_once(self, parser, prefix, argv):
        parsed = parser.parse_args([*prefix, *argv])
        self.assertIsNotNone(parsed)
        for index in range(0, len(argv), 2):
            option, value = argv[index:index + 2]
            with self.subTest(prefix=prefix, option=option), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    parser.parse_args([*prefix, *argv, option, value])
                self.assertEqual(raised.exception.code, 2)

    def test_every_value_bearing_release_cli_option_rejects_duplicates(self):
        release_parser = release_bundle.build_parser()
        for command, argv in self._release_bundle_cases().items():
            self._assert_every_value_option_is_store_once(release_parser, [command], argv)
        self._assert_every_value_option_is_store_once(guarded.build_parser(), ["run"], self._guarded_run())
        for helper in sorted(guarded._PRIVILEGED_HELPERS):
            self._assert_every_value_option_is_store_once(
                guarded.build_parser(), [helper], ["--request-b64", "YQ"]
            )
        self._assert_every_value_option_is_store_once(acceptance.build_parser(), [], self._acceptance())
        self._assert_every_value_option_is_store_once(remote_verifier.build_parser(), [], self._remote())

    def test_positive_parser_dispatch_reaches_each_owning_main(self):
        with mock.patch.object(release_bundle, "prepare_acceptance_manifest") as prepare:
            self.assertEqual(release_bundle.main([
                "prepare-acceptance-manifest", "--release-root", "/release", "--source-sha", "a" * 40,
                "--candidate-tracked-files-json", "[]",
            ]), 0)
            prepare.assert_called_once_with("/release", "a" * 40, [])
        guarded_args = guarded.build_parser().parse_args(["run", *self._guarded_run()])
        with mock.patch.object(guarded, "run_release", return_value=0) as run:
            self.assertEqual(guarded.main(["run", *self._guarded_run()]), 0)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0].boundary, guarded_args.boundary)
        with mock.patch.object(acceptance, "PinnedRemoteExecutor", return_value=object()), \
                mock.patch.object(acceptance, "run_acceptance", return_value=0) as run:
            self.assertEqual(acceptance.main(self._acceptance()), 0)
            run.assert_called_once()
        with mock.patch.object(remote_verifier, "run", return_value={"schema": 1}) as run:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(remote_verifier.main(self._remote()), 0)
            run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
