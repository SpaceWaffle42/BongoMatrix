import contextlib
import hashlib
import importlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class _Service:
    def __init__(self):
        self.active = True
        self.n_restarts = 0
        self.main_pid = 12345
        self.events = []
    def is_active(self):
        return self.active

    def stop(self):
        self.events.append("stop")
        self.active = False

    def start_ready(self):
        self.events.append("start_ready")
        self.main_pid += 1
        self.active = True

    def status(self, *, warning_since=None):
        return {
            "service": "matrix-web.service", "active": self.active,
            "substate": "running", "http_status": 302, "main_pid": self.main_pid,
            "n_restarts": 0, "control_group": "/system.slice/matrix-web.service",
            "display_lock": {
                "path": "/run/matrix-display/matrix-display.lock", "dev": 1,
                "ino": 2, "uid": 0, "gid": 0, "mode": "0644", "locked": True,
            },
            "warning_count": 0,
        }


class Priority17TransactionTests(unittest.TestCase):
    def _git(self, root, *arguments):
        return subprocess.check_output(
            ["/usr/bin/git", "-C", str(root), *arguments], text=True,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
        ).strip()

    def _fixture(self, transaction, root, *, boundary="b1"):
        repository = root / "repository"
        repository.mkdir()
        subprocess.run(["/usr/bin/git", "init", "-q", str(repository)], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(repository), "config", "user.name", "test"], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(repository), "config", "user.email", "test@example.invalid"], check=True)
        (repository / "app.py").write_bytes(b"baseline\n")
        (repository / "pkg").mkdir()
        (repository / "pkg/module.py").write_bytes(b"baseline module\n")
        (repository / "deploy").mkdir()
        (repository / "deploy/keep.txt").write_bytes(b"baseline deploy parent\n")
        subprocess.run([
            "/usr/bin/git", "-C", str(repository), "add",
            "app.py", "pkg/module.py", "deploy/keep.txt",
        ], check=True)
        dates = {
            **os.environ, "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000",
        }
        subprocess.run(["/usr/bin/git", "-C", str(repository), "commit", "-qm", "baseline"], check=True, env=dates)
        baseline = self._git(repository, "rev-parse", "HEAD^{commit}")
        baseline_tree = self._git(repository, "rev-parse", "HEAD^{tree}")
        (repository / "app.py").write_bytes(b"candidate\n")
        (repository / "pkg/module.py").write_bytes(b"candidate module\n")
        (repository / "candidate-only.py").write_bytes(b"candidate only\n")
        (repository / "deploy/priority_1_7_transaction.py").write_bytes(
            Path(transaction.__file__).read_bytes()
        )
        subprocess.run([
            "/usr/bin/git", "-C", str(repository), "add",
            "candidate-only.py", "deploy/priority_1_7_transaction.py",
        ], check=True)
        candidate_dates = {
            **dates, "GIT_AUTHOR_DATE": "2000-01-01T00:00:01+0000",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:01+0000",
        }
        subprocess.run(["/usr/bin/git", "-C", str(repository), "commit", "-qam", "candidate"], check=True, env=candidate_dates)
        source = self._git(repository, "rev-parse", "HEAD^{commit}")
        source_tree = self._git(repository, "rev-parse", "HEAD^{tree}")
        diff = subprocess.check_output([
            "/usr/bin/git", "-C", str(repository), "diff", "--binary", "--full-index",
            "--no-ext-diff", f"{baseline}...{source}",
        ])
        destination = root / "destination"
        stage = root / "stage"
        with (
            patch.object(transaction, "PRODUCTION_DESTINATION", str(destination)),
            patch.object(transaction, "PRODUCTION_INTERPRETER", sys.executable),
            patch.object(transaction, "PRODUCTION_SERVICE", "matrix-web.service"),
        ):
            transaction.build_release(types.SimpleNamespace(
                boundary=boundary, repository=str(repository), baseline_commit=baseline,
                source_commit=source,
                expected_reviewed_diff_sha256=hashlib.sha256(diff).hexdigest(),
                stage_root=str(stage), python=sys.executable,
            ))
        subprocess.run([
            "/usr/bin/git", "clone", "-q", "--no-hardlinks", str(repository), str(destination),
        ], check=True)
        subprocess.run([
            "/usr/bin/git", "-C", str(destination), "checkout", "-q", baseline,
        ], check=True)
        private = {}
        # Match ConfigStore._serialize_config(): sorted compact UTF-8 with no newline.
        config = b'{"modules":[],"revision":7,"schema_version":3}'
        if boundary == "b2":
            config = json.dumps({
                "schema_version": 3, "revision": 8,
                "modules": [{"type": "custom_text", "settings": {
                    "layers": [{"type": "pixels", "properties": {
                        "bitmap_rgba": "persisted-b2-bitmap",
                    }}],
                }}],
            }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        for relative, value in (
            ("data/config.json", config),
            ("data/paired-devices.json", b"[]\n"),
            ("data/edit-preview-drafts.json", b"draft-private"),
            ("resources/media/private.bin", b"media-private"),
            ("venv/private.bin", b"venv-private"),
        ):
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
            private[relative] = value
        backup = root / "backup"
        state = backup / "transaction-state.json"
        arguments = types.SimpleNamespace(
            boundary=boundary,
            release_root=str(stage / "release"),
            release_manifest=str(stage / "release-manifest.json"),
            fixture_manifest=str(stage / "physical-fixtures.json"),
            expected_baseline_source_sha=baseline,
            expected_source_sha=source,
            destination=str(destination), backup_root=str(backup),
            service="matrix-web.service", interpreter=sys.executable, state=str(state),
        )
        service = _Service()
        with patch.object(transaction, "SystemdService", return_value=service):
            prepare_report = transaction.prepare_release(arguments)
        return {
            "repository": repository, "reviewed_diff_sha256": hashlib.sha256(diff).hexdigest(),
            "baseline": baseline, "baseline_tree": baseline_tree,
            "source": source, "source_tree": source_tree, "stage": stage,
            "destination": destination, "backup": backup, "state": state,
            "private": private, "service": service, "prepare_report": prepare_report,
        }

    def test_destination_wide_release_lock_excludes_distinct_attempts(self):
        transaction = importlib.import_module("deploy.priority_1_7_transaction")
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "destination"
            destination.mkdir()
            first = transaction._acquire_release_lock(destination)
            try:
                lock_path = transaction._release_lock_path(destination)
                self.assertEqual(
                    lock_path.parent, destination.parent / ".priority-1-7-release-locks",
                )
                self.assertEqual(stat.S_IMODE(lock_path.parent.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
                destination.rename(Path(temporary) / "moved-destination")
                destination.mkdir()
                with self.assertRaisesRegex(RuntimeError, "release lock"):
                    transaction._acquire_release_lock(destination)
            finally:
                os.close(first)
            second = transaction._acquire_release_lock(destination)
            os.close(second)

    def test_load_validation_failure_releases_locked_state_descriptor(self):
        transaction = importlib.import_module("deploy.priority_1_7_transaction")
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "transaction-state.json"
            state.write_text("{}\n")
            state.chmod(0o600)
            before = len(os.listdir("/proc/self/fd"))
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, "transaction state schema"):
                    transaction.Priority17Transaction.load(state)
                self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_production_release_lock_rejects_unprivileged_claim(self):
        transaction = importlib.import_module("deploy.priority_1_7_transaction")
        production = Path("/home/admin/matrix")
        with (
            patch.object(transaction, "PRODUCTION_DESTINATION", str(production)),
            patch.object(transaction.os, "geteuid", return_value=1000),
            self.assertRaisesRegex(PermissionError, "root release authority"),
        ):
            transaction._acquire_release_lock(production)

    def test_prepare_holds_destination_wide_release_lock(self):
        transaction = importlib.import_module("deploy.priority_1_7_transaction")
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "destination"
            destination.mkdir()
            arguments = types.SimpleNamespace(destination=str(destination))

            def assert_locked(observed, descriptor):
                self.assertEqual(observed, arguments)
                self.assertGreaterEqual(descriptor, 0)
                with self.assertRaisesRegex(RuntimeError, "release lock"):
                    transaction._acquire_release_lock(destination)
                return "locked"

            with patch.object(transaction, "_prepare_release_locked", side_effect=assert_locked):
                self.assertEqual(transaction.prepare_release(arguments), "locked")
            descriptor = transaction._acquire_release_lock(destination)
            os.close(descriptor)

    def test_loaded_transaction_holds_destination_wide_release_lock(self):
        transaction = importlib.import_module("deploy.priority_1_7_transaction")
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(transaction, Path(temporary))
            tx = transaction.Priority17Transaction.load(
                fixture["state"], service=fixture["service"],
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "release lock"):
                    transaction._acquire_release_lock(fixture["destination"])
            finally:
                tx.close()
            descriptor = transaction._acquire_release_lock(fixture["destination"])
            os.close(descriptor)

    def test_cli_and_exported_transaction_are_exact(self):
        transaction = importlib.import_module("deploy.priority_1_7_transaction")

        expected = {
            "build", "prepare", "deploy", "status", "rollback", "redeploy",
            "recover", "finalize",
        }
        parser = transaction.build_parser()
        commands = parser._subparsers._group_actions[0].choices
        self.assertEqual(set(commands), expected)
        self.assertEqual(transaction.PRODUCTION_DESTINATION, "/home/admin/matrix")
        self.assertEqual(transaction.PRODUCTION_INTERPRETER, "/home/admin/matrix/venv/bin/python")
        self.assertEqual(transaction.PRODUCTION_SERVICE, "matrix-web.service")

        valid = {
            "build": [
                "--boundary", "b1", "--repository", "/repository",
                "--baseline-commit", "a" * 40, "--source-commit", "b" * 40,
                "--expected-reviewed-diff-sha256", "c" * 64,
                "--stage-root", "/stage", "--python", "/python",
            ],
            "prepare": [
                "--boundary", "b1", "--release-root", "/release",
                "--release-manifest", "/release.json", "--fixture-manifest", "/fixture.json",
                "--expected-baseline-source-sha", "a" * 40,
                "--expected-source-sha", "b" * 40, "--destination", "/destination",
                "--backup-root", "/backup", "--service", "matrix-web.service",
                "--interpreter", "/python", "--state", "/state",
            ],
            "deploy": ["--state", "/state"],
            "status": ["--state", "/state"],
            "rollback": ["--state", "/state"],
            "redeploy": ["--state", "/state"],
            "recover": ["--state", "/state", "--target", "candidate"],
            "finalize": ["--state", "/state"],
        }
        for command, options in valid.items():
            for offset in range(0, len(options), 2):
                duplicate = options[offset:offset + 2]
                with self.subTest(command=command, duplicate=duplicate[0]):
                    with (
                        contextlib.redirect_stderr(io.StringIO()),
                        self.assertRaises(SystemExit) as caught,
                    ):
                        parser.parse_args([command, *options, *duplicate])
                    self.assertEqual(caught.exception.code, 2)

        source_script = Path(__file__).parents[1] / "deploy/priority_1_7_transaction.py"
        with tempfile.TemporaryDirectory() as temporary:
            isolated_script = Path(temporary) / source_script.name
            isolated_script.write_bytes(source_script.read_bytes())
            completed = subprocess.run(
                [sys.executable, "-I", "-B", str(isolated_script), "--help"],
                cwd="/", capture_output=True, text=True, timeout=10,
                env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        for command in sorted(expected):
            self.assertIn(command, completed.stdout)

    def test_build_binds_boundary_diff_runtime_manifest_and_pixel_fixtures(self):
        transaction = importlib.import_module("deploy.priority_1_7_transaction")
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(transaction, Path(temporary))
            release = json.loads((fixture["stage"] / "release-manifest.json").read_bytes())
            physical = json.loads((fixture["stage"] / "physical-fixtures.json").read_bytes())
            exported = fixture["stage"] / "priority_1_7_transaction.py"

            self.assertEqual(release["boundary"], "b1")
            self.assertEqual(release["baseline_sha"], fixture["baseline"])
            self.assertEqual(release["baseline_tree_sha"], fixture["baseline_tree"])
            self.assertEqual(release["source_sha"], fixture["source"])
            self.assertEqual(release["tree_sha"], fixture["source_tree"])
            self.assertEqual(release["runtime"], {
                "destination": str(fixture["destination"]),
                "interpreter": sys.executable,
                "service": "matrix-web.service",
            })
            self.assertEqual(
                [entry["path"] for entry in release["changed_files"]],
                [
                    "app.py", "candidate-only.py", "deploy/priority_1_7_transaction.py",
                    "pkg/module.py",
                ],
            )
            self.assertEqual(
                [entry["path"] for entry in release["runtime_files"]],
                ["app.py", "candidate-only.py", "pkg/module.py"],
            )
            self.assertTrue(exported.is_file())
            self.assertEqual(exported.read_bytes(), Path(transaction.__file__).read_bytes())
            completed = subprocess.run(
                [sys.executable, "-I", "-B", str(exported), "--help"],
                cwd="/", capture_output=True, text=True, timeout=10,
                env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(physical["boundary"], "b1")
            self.assertEqual(
                [entry["name"] for entry in physical["fixtures"]],
                ["corners_hole_black_composite", "translated_clipped_rotation"],
            )
            self.assertTrue(all(entry["width"] == 64 and entry["height"] == 32 for entry in physical["fixtures"]))
            self.assertFalse(any("path" in entry for entry in physical["fixtures"]))

    def test_production_configuration_parser_matches_config_store_serialization(self):
        transaction = importlib.import_module("deploy.priority_1_7_transaction")
        exact = b'{"modules":[],"revision":7,"schema_version":3}'
        self.assertEqual(
            transaction._parse_production_configuration(exact),
            {"modules": [], "revision": 7, "schema_version": 3},
        )
        for invalid in (
            exact + b"\n",
            b'{"modules": [],"revision":7,"schema_version":3}',
            b'{"modules":[],"revision":7,"revision":8,"schema_version":3}',
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "production configuration"):
                    transaction._parse_production_configuration(invalid)

    def test_prepare_snapshots_mutable_state_and_b1_rejects_bitmap_config(self):
        transaction = importlib.import_module("deploy.priority_1_7_transaction")
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(transaction, Path(temporary))
            self.assertEqual(fixture["prepare_report"]["status"], "BACKUP_READY")
            self.assertEqual(fixture["prepare_report"]["boundary"], "b1")
            state = json.loads(fixture["state"].read_bytes())
            self.assertEqual(
                [entry["path"] for entry in state["mutable_state"]],
                ["data", "resources/media"],
            )
            tx = transaction.Priority17Transaction.load(
                fixture["state"], service=fixture["service"],
            )
            try:
                config = fixture["destination"] / "data/config.json"
                original = config.read_bytes()
                config.write_bytes(original + b" ")
                with self.assertRaisesRegex(ValueError, "mutable state changed"):
                    tx.status()
                config.write_bytes(original)
                self.assertEqual(tx.status()["mutable_state"], state["mutable_state"])
            finally:
                tx.close()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self._fixture(transaction, root)
            fixture["state"].unlink()
            # Recreate a fresh authority attempt over the exact prepared archive.
            bitmap_config = {
                "schema_version": 3, "revision": 8,
                "modules": [{"type": "custom_text", "settings": {
                    "layers": [{"type": "pixels", "properties": {
                        "bitmap_rgba": "forbidden-in-b1",
                    }}],
                }}],
            }
            (fixture["destination"] / "data/config.json").write_bytes(
                json.dumps(bitmap_config, sort_keys=True, separators=(",", ":")).encode("utf-8")
            )
            arguments = types.SimpleNamespace(
                boundary="b1", release_root=str(fixture["stage"] / "release"),
                release_manifest=str(fixture["stage"] / "release-manifest.json"),
                fixture_manifest=str(fixture["stage"] / "physical-fixtures.json"),
                expected_baseline_source_sha=fixture["baseline"],
                expected_source_sha=fixture["source"], destination=str(fixture["destination"]),
                backup_root=str(fixture["backup"]), service="matrix-web.service",
                interpreter=sys.executable, state=str(fixture["state"]),
            )
            with (
                patch.object(transaction, "SystemdService", return_value=fixture["service"]),
                self.assertRaisesRegex(ValueError, "B1.*bitmap"),
            ):
                transaction.prepare_release(arguments)

    def test_interrupted_deploy_rollback_and_recovery_converge(self):
        transaction = importlib.import_module("deploy.priority_1_7_transaction")
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(transaction, Path(temporary))
            service = fixture["service"]
            tx = transaction.Priority17Transaction.load(fixture["state"], service=service)
            try:
                for operation, target, expected in (
                    ("deploy", "candidate", b"candidate\n"),
                    ("rollback", "baseline", b"baseline\n"),
                    ("redeploy", "candidate", b"candidate\n"),
                ):
                    with self.subTest(operation=operation):
                        crashed = False

                        def interrupt(point):
                            nonlocal crashed
                            if point.endswith(":published") and not crashed:
                                crashed = True
                                raise transaction.InjectedCrash(point)

                        with self.assertRaises(transaction.InjectedCrash):
                            tx.transition(target, operation=operation, after_boundary=interrupt)
                        self.assertFalse(service.active)
                        tx.close()
                        tx = transaction.Priority17Transaction.load(fixture["state"], service=service)
                        tx.recover(target)
                        self.assertEqual((fixture["destination"] / "app.py").read_bytes(), expected)
                        self.assertTrue(service.active)
                        for relative, value in fixture["private"].items():
                            self.assertEqual((fixture["destination"] / relative).read_bytes(), value)

                status = tx.status()
                self.assertEqual(status["boundary"], "b1")
                self.assertEqual(status["source"], "candidate")
                final = tx.finalize()
                self.assertEqual(final["boundary"], "b1")
                self.assertEqual(final["status"], "FINALIZED")
            finally:
                tx.close()
    def test_finalize_is_durable_idempotent_and_terminal(self):
        transaction = importlib.import_module("deploy.priority_1_7_transaction")
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(transaction, Path(temporary))
            tx = transaction.Priority17Transaction.load(
                fixture["state"], service=fixture["service"],
            )
            tx.transition("candidate", operation="deploy")
            report = tx.finalize()
            tx.close()

            persisted = json.loads(fixture["state"].read_bytes())
            self.assertEqual(persisted["lifecycle"], "FINALIZED")
            self.assertIsInstance(persisted["final_report"], dict)
            report_path = Path(persisted["final_report"]["path"])
            self.assertEqual(json.loads(report_path.read_bytes()), report)
            self.assertEqual(stat.S_IMODE(report_path.stat().st_mode), 0o600)

            tx = transaction.Priority17Transaction.load(
                fixture["state"], service=fixture["service"],
            )
            try:
                self.assertEqual(tx.finalize(), report)
                for operation, target in (
                    ("deploy", "candidate"),
                    ("rollback", "baseline"),
                    ("redeploy", "candidate"),
                    ("recover", "baseline"),
                ):
                    with self.subTest(operation=operation):
                        with self.assertRaisesRegex(ValueError, "finalized"):
                            tx.transition(target, operation=operation)
            finally:
                tx.close()

    def test_finalize_adopts_exact_report_after_state_publication_crash(self):
        transaction = importlib.import_module("deploy.priority_1_7_transaction")
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(transaction, Path(temporary))
            tx = transaction.Priority17Transaction.load(
                fixture["state"], service=fixture["service"],
            )
            tx.transition("candidate", operation="deploy")
            with (
                patch.object(tx, "_persist_state", side_effect=transaction.InjectedCrash("finalize:state")),
                self.assertRaises(transaction.InjectedCrash),
            ):
                tx.finalize()
            report_path = fixture["state"].with_name(fixture["state"].name + ".finalized.json")
            self.assertTrue(report_path.is_file())
            self.assertEqual(json.loads(fixture["state"].read_bytes())["lifecycle"], "PREPARED")
            tx.close()
            fixture["service"].main_pid += 100
            fixture["service"].active = False

            tx = transaction.Priority17Transaction.load(
                fixture["state"], service=fixture["service"],
            )
            try:
                with self.assertRaisesRegex(ValueError, "finalization"):
                    tx.transition("baseline", operation="rollback")
                report = tx.finalize()
                self.assertEqual(report["status"], "FINALIZED")
                self.assertEqual(tx.status(), report)
            finally:
                tx.close()

    def test_pending_final_report_rejects_wrong_version_boolean_counts_and_pid_claim(self):
        transaction = importlib.import_module("deploy.priority_1_7_transaction")
        mutations = {
            "wrong-version": lambda report: report.__setitem__("version", 2),
            "boolean-warning-count": lambda report: report["service"].__setitem__("warning_count", False),
            "unchanged-main-pid": lambda report: report["service"].__setitem__(
                "main_pid", report["service"]["previous_main_pid"],
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                fixture = self._fixture(transaction, Path(temporary), boundary="b1")
                tx = transaction.Priority17Transaction.load(
                    fixture["state"], service=fixture["service"],
                )
                tx.transition("candidate", operation="deploy")
                with patch.object(
                    tx, "_persist_state",
                    side_effect=transaction.InjectedCrash("finalize:state"),
                ):
                    with self.assertRaises(transaction.InjectedCrash):
                        tx.finalize()
                report_path = Path(tx.state["final_report_path"])
                tx.close()
                report = json.loads(report_path.read_bytes())
                mutate(report)
                report_path.write_bytes(transaction.canonical_json(report))
                report_path.chmod(0o600)
                with self.assertRaisesRegex(ValueError, "final report"):
                    transaction.Priority17Transaction.load(
                        fixture["state"], service=fixture["service"],
                    )

    def test_ambiguous_source_requires_explicit_recovery(self):
        transaction = importlib.import_module("deploy.priority_1_7_transaction")
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(transaction, Path(temporary))
            tx = transaction.Priority17Transaction.load(
                fixture["state"], service=fixture["service"],
            )
            try:
                def interrupt(point):
                    if point == "app.py:published":
                        raise transaction.InjectedCrash(point)

                with self.assertRaises(transaction.InjectedCrash):
                    tx.transition("candidate", operation="deploy", after_boundary=interrupt)
                with self.assertRaisesRegex(ValueError, "explicit recovery"):
                    tx.transition("candidate", operation="deploy")
                tx.recover("candidate")
                self.assertEqual(tx.status()["source"], "candidate")
            finally:
                tx.close()

    def test_build_publication_is_atomic_before_and_after_interruption(self):
        transaction = importlib.import_module("deploy.priority_1_7_transaction")
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(transaction, Path(temporary))
            shutil.rmtree(fixture["stage"])

            def arguments(callback):
                return types.SimpleNamespace(
                    boundary="b1", repository=str(fixture["repository"]),
                    baseline_commit=fixture["baseline"], source_commit=fixture["source"],
                    expected_reviewed_diff_sha256=fixture["reviewed_diff_sha256"],
                    stage_root=str(fixture["stage"]), python=sys.executable,
                    after_boundary=callback,
                )

            for point in ("stage:before-publication", "stage:published"):
                with self.subTest(point=point):
                    if fixture["stage"].exists():
                        shutil.rmtree(fixture["stage"])

                    def interrupt(observed):
                        if observed == point:
                            raise transaction.InjectedCrash(observed)

                    with (
                        patch.object(transaction, "PRODUCTION_DESTINATION", str(fixture["destination"])),
                        patch.object(transaction, "PRODUCTION_INTERPRETER", sys.executable),
                        patch.object(transaction, "PRODUCTION_SERVICE", "matrix-web.service"),
                        self.assertRaises(transaction.InjectedCrash),
                    ):
                        transaction.build_release(arguments(interrupt))
                    self.assertFalse(
                        fixture["stage"].with_name(fixture["stage"].name + ".preparing").exists(),
                    )
                    if point == "stage:before-publication":
                        self.assertFalse(fixture["stage"].exists())
                    else:
                        self.assertTrue(fixture["stage"].is_dir())
                        raw = (fixture["stage"] / "release-manifest.json").read_bytes()
                        manifest = transaction._validate_release_manifest(
                            raw, fixture["stage"] / "release", expected_boundary="b1",
                        )
                        self.assertEqual(manifest["source_sha"], fixture["source"])

    def test_prepare_publications_recover_by_exact_archive_adoption(self):
        transaction = importlib.import_module("deploy.priority_1_7_transaction")
        points = (
            "backup:before-publication", "backup:published",
            "state:before-publication", "state:published",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, point in enumerate(points):
                with self.subTest(point=point):
                    case_root = root / f"prepare-{index}"
                    case_root.mkdir()
                    fixture = self._fixture(transaction, case_root)
                    fixture["state"].unlink()
                    if point.startswith("backup:"):
                        shutil.rmtree(fixture["backup"] / "archive")
                    arguments = types.SimpleNamespace(
                        boundary="b1", release_root=str(fixture["stage"] / "release"),
                        release_manifest=str(fixture["stage"] / "release-manifest.json"),
                        fixture_manifest=str(fixture["stage"] / "physical-fixtures.json"),
                        expected_baseline_source_sha=fixture["baseline"],
                        expected_source_sha=fixture["source"],
                        destination=str(fixture["destination"]),
                        backup_root=str(fixture["backup"]), service="matrix-web.service",
                        interpreter=sys.executable, state=str(fixture["state"]),
                    )

                    def interrupt(observed):
                        if observed == point:
                            raise transaction.InjectedCrash(observed)

                    arguments.after_boundary = interrupt
                    with (
                        patch.object(transaction, "SystemdService", return_value=fixture["service"]),
                        self.assertRaises(transaction.InjectedCrash),
                    ):
                        transaction.prepare_release(arguments)
                    archive = fixture["backup"] / "archive"
                    if point == "backup:before-publication":
                        self.assertFalse(archive.exists())
                    else:
                        self.assertTrue(archive.is_dir())
                    if point == "state:published":
                        self.assertTrue(fixture["state"].is_file())
                    else:
                        self.assertFalse(fixture["state"].exists())
                        del arguments.after_boundary
                        with patch.object(
                            transaction, "SystemdService", return_value=fixture["service"],
                        ):
                            report = transaction.prepare_release(arguments)
                        self.assertEqual(report["status"], "BACKUP_READY")
                    tx = transaction.Priority17Transaction.load(
                        fixture["state"], service=fixture["service"],
                    )
                    try:
                        self.assertEqual(tx.status()["source"], "baseline")
                    finally:
                        tx.close()

    def test_every_runtime_publication_interruption_recovers_explicit_target(self):
        transaction = importlib.import_module("deploy.priority_1_7_transaction")
        points = [
            f"{relative}:{moment}"
            for relative in ("app.py", "candidate-only.py", "pkg/module.py")
            for moment in ("before-publication", "published")
        ]
        cases = (
            ("deploy", "candidate", False),
            ("rollback", "baseline", True),
            ("redeploy", "candidate", False),
            ("recover-candidate", "candidate", False),
            ("recover-baseline", "baseline", True),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for case_index, (operation, target, initially_candidate) in enumerate(cases):
                for point_index, point in enumerate(points):
                    with self.subTest(operation=operation, point=point):
                        case_root = root / f"case-{case_index}-{point_index}"
                        case_root.mkdir()
                        fixture = self._fixture(transaction, case_root)
                        service = fixture["service"]
                        tx = transaction.Priority17Transaction.load(
                            fixture["state"], service=service,
                        )
                        try:
                            if initially_candidate:
                                tx.transition("candidate", operation="deploy")

                            def interrupt(observed):
                                if observed == point:
                                    raise transaction.InjectedCrash(observed)

                            with self.assertRaises(transaction.InjectedCrash):
                                if operation.startswith("recover-"):
                                    tx.recover(target, after_boundary=interrupt)
                                else:
                                    tx.transition(
                                        target, operation=operation,
                                        after_boundary=interrupt,
                                    )
                            self.assertFalse(service.active)
                            with self.assertRaises((RuntimeError, ValueError)):
                                tx.status()
                            tx.close()
                            tx = transaction.Priority17Transaction.load(
                                fixture["state"], service=service,
                            )
                            tx.recover(target)
                            self.assertEqual(tx.status()["source"], target)
                            for relative, expected in fixture["private"].items():
                                self.assertEqual(
                                    (fixture["destination"] / relative).read_bytes(), expected,
                                )
                        finally:
                            tx.close()

    def test_b2_rollback_to_exact_b1_preserves_bitmap_configuration(self):
        transaction = importlib.import_module("deploy.priority_1_7_transaction")
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(transaction, Path(temporary), boundary="b2")
            bitmap_bytes = fixture["private"]["data/config.json"]
            tx = transaction.Priority17Transaction.load(
                fixture["state"], service=fixture["service"],
            )
            try:
                tx.transition("candidate", operation="deploy")
                tx.transition("baseline", operation="rollback")
                self.assertEqual(
                    (fixture["destination"] / "app.py").read_bytes(), b"baseline\n",
                )
                self.assertEqual(
                    (fixture["destination"] / "data/config.json").read_bytes(),
                    bitmap_bytes,
                )
                self.assertEqual(tx.status()["boundary"], "b2")
                tx.transition("candidate", operation="redeploy")
                self.assertEqual(
                    (fixture["destination"] / "data/config.json").read_bytes(),
                    bitmap_bytes,
                )
            finally:
                tx.close()

    def test_transition_rejects_service_without_new_pid(self):
        transaction = importlib.import_module("deploy.priority_1_7_transaction")
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(transaction, Path(temporary))
            service = fixture["service"]

            def stale_start():
                service.events.append("stale_start")
                service.active = True

            service.start_ready = stale_start
            tx = transaction.Priority17Transaction.load(fixture["state"], service=service)
            try:
                with self.assertRaisesRegex(RuntimeError, "main PID did not change"):
                    tx.transition("candidate", operation="deploy")
                self.assertFalse(service.active)
            finally:
                tx.close()

    def test_post_stop_mutable_writer_race_fails_before_publication(self):
        transaction = importlib.import_module("deploy.priority_1_7_transaction")
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(transaction, Path(temporary))
            service = fixture["service"]
            original_stop = service.stop

            def racing_stop():
                original_stop()
                (fixture["destination"] / "data/config.json").write_bytes(
                    b'{"modules":[],"revision":999,"schema_version":3}\n'
                )

            service.stop = racing_stop
            tx = transaction.Priority17Transaction.load(fixture["state"], service=service)
            try:
                with self.assertRaisesRegex(ValueError, "mutable state changed before"):
                    tx.transition("candidate", operation="deploy")
                self.assertEqual(
                    (fixture["destination"] / "app.py").read_bytes(), b"baseline\n",
                )
                self.assertFalse(service.active)
            finally:
                tx.close()

    def test_prepare_rejects_substituted_archive(self):
        transaction = importlib.import_module("deploy.priority_1_7_transaction")
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(transaction, Path(temporary))
            fixture["state"].unlink()
            (fixture["backup"] / "archive/extra").write_bytes(b"not-authorized")
            arguments = types.SimpleNamespace(
                boundary="b1", release_root=str(fixture["stage"] / "release"),
                release_manifest=str(fixture["stage"] / "release-manifest.json"),
                fixture_manifest=str(fixture["stage"] / "physical-fixtures.json"),
                expected_baseline_source_sha=fixture["baseline"],
                expected_source_sha=fixture["source"],
                destination=str(fixture["destination"]), backup_root=str(fixture["backup"]),
                service="matrix-web.service", interpreter=sys.executable,
                state=str(fixture["state"]),
            )
            with (
                patch.object(transaction, "SystemdService", return_value=fixture["service"]),
                self.assertRaisesRegex(ValueError, "backup|archive|extra"),
            ):
                transaction.prepare_release(arguments)

    def test_prepare_rejects_runtime_identity_substitution(self):
        transaction = importlib.import_module("deploy.priority_1_7_transaction")
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(transaction, Path(temporary))
            fixture["state"].unlink()
            arguments = types.SimpleNamespace(
                boundary="b1", release_root=str(fixture["stage"] / "release"),
                release_manifest=str(fixture["stage"] / "release-manifest.json"),
                fixture_manifest=str(fixture["stage"] / "physical-fixtures.json"),
                expected_baseline_source_sha=fixture["baseline"],
                expected_source_sha=fixture["source"],
                destination=str(fixture["destination"]), backup_root=str(fixture["backup"]),
                service="substituted.service", interpreter=sys.executable,
                state=str(fixture["state"]),
            )
            with self.assertRaisesRegex(ValueError, "runtime identity mismatch"):
                transaction.prepare_release(arguments)


if __name__ == "__main__":
    unittest.main()
