import argparse
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from scripts import verify_priority_1_3_remote as remote_verifier


class Priority13AcceptanceTests(unittest.TestCase):
    def test_remote_physical_verifier_pins_systemctl_and_environment(self):
        completed = subprocess.CompletedProcess(
            ["/usr/bin/systemctl"], 0, stdout="active\n", stderr="",
        )
        metadata = {
            "nonce": "f" * 64,
            "draft_sha256": "d" * 64,
            "saved_sha256": "s" * 64,
            "owner_generation": 4,
        }
        frames = {"draft": b"d" * 6144, "saved": b"s" * 6144}
        with tempfile.TemporaryDirectory() as directory:
            transaction = Path(directory)
            with mock.patch.object(
                remote_verifier.subprocess, "run", return_value=completed,
            ) as run, mock.patch.object(
                remote_verifier, "_canonical_transaction",
                return_value=(metadata, frames, transaction),
            ):
                report = remote_verifier._physical(
                    Path(directory), argparse.Namespace(),
                )

        self.assertTrue(report["service_active"])
        run.assert_called_once_with(
            ["/usr/bin/systemctl", "is-active", "matrix-web.service"],
            check=False,
            text=True,
            capture_output=True,
            timeout=15,
            shell=False,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )

    def test_exported_verifier_acceptance_runs_with_readme_venv_and_no_dot_venv(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            exported = Path(directory) / "readme-layout"
            (exported / "tests").mkdir(parents=True)
            (exported / "scripts").mkdir()
            (exported / "venv/bin").mkdir(parents=True)
            shutil.copy2(__file__, exported / "tests" / Path(__file__).name)
            (exported / "tests/__init__.py").write_text("")
            shutil.copy2(
                repository / "scripts/verify_priority_1_3_remote.py",
                exported / "scripts/verify_priority_1_3_remote.py",
            )
            shutil.copy2(repository / "release_argparse.py", exported / "release_argparse.py")
            (exported / "venv/bin/python").symlink_to(Path(sys.executable).resolve())
            self.assertFalse((exported / ".venv").exists())

            completed = subprocess.run(
                [
                    str(exported / "venv/bin/python"),
                    "-B",
                    "-m",
                    "unittest",
                    "-v",
                    "tests.test_priority_1_3_acceptance.Priority13AcceptanceTests."
                    "test_exported_candidate_ships_executable_remote_verifier",
                ],
                cwd=exported,
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_readme_venv_runs_all_remaining_release_fixtures_without_dot_venv(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            exported = Path(directory) / "readme-layout"
            exported.mkdir()
            tracked = subprocess.run(
                ["git", "ls-files", "-z"],
                cwd=repository,
                check=True,
                capture_output=True,
            ).stdout.split(b"\0")
            for encoded in tracked:
                if not encoded:
                    continue
                relative = Path(encoded.decode("utf-8"))
                source = repository / relative
                if source.is_file():
                    destination = exported / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
            (exported / "venv/bin").mkdir(parents=True)
            (exported / "venv/bin/python").symlink_to(Path(sys.executable).resolve())
            self.assertFalse((exported / ".venv").exists())

            completed = subprocess.run(
                [
                    str(exported / "venv/bin/python"),
                    "-B",
                    "-m",
                    "unittest",
                    "-v",
                    "tests.test_production_test_verifier.ProductionTestVerifierTests."
                    "test_standard_venv_runs_exact_real_tests_and_binds_bounded_stderr",
                    "tests.test_slice1_identity_and_codec.Slice1IdentityAndCodecTests."
                    "test_fresh_process_inspect_rejects_redigested_execution_service_mismatch",
                    "tests.test_slice1_identity_and_codec.Slice1IdentityAndCodecTests."
                    "test_fresh_process_inspect_rejects_every_coherently_redigested_schema_corruption",
                    "tests.test_slice1_identity_and_codec.Slice1IdentityAndCodecTests."
                    "test_exact_schema_v1_fresh_process_inspect_rejects_coherently_redigested_non_int_values",
                ],
                cwd=exported,
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_acceptance_fixtures_do_not_hardcode_ci_dot_venv_alias(self):
        repository = Path(__file__).resolve().parents[1]
        fixtures = (
            repository / "tests/test_priority_1_3_acceptance.py",
            repository / "tests/test_guarded_priority_1_3_release.py",
            repository / "tests/test_release_gate_cli_and_build.py",
            repository / "tests/test_production_test_verifier.py",
            repository / "tests/test_slice1_identity_and_codec.py",
        )

        forbidden = ".venv" + "/bin/python"
        for fixture in fixtures:
            with self.subTest(fixture=fixture.name):
                self.assertNotIn(forbidden, fixture.read_text())

    def test_readme_runtime_venv_and_ci_compatibility_alias_are_explicit(self):
        repository = Path(__file__).resolve().parents[1]
        readme = (repository / "README.md").read_text()
        pipeline = (repository / ".gitlab-ci.yml").read_text()

        self.assertIn("python3 -m venv venv", readme)
        self.assertIn("./venv/bin/python -m pip install", readme)
        self.assertIn("./venv/bin/python -B -m unittest", readme)
        self.assertNotIn(".venv" + "/bin/python", readme)
        self.assertIn("python3 -m venv .venv-ci", pipeline)
        self.assertIn("ln -sT .venv-ci .venv", pipeline)
        self.assertIn('test "$(readlink .venv)" = ".venv-ci"', pipeline)
        self.assertIn(".venv-ci/bin/coverage run", pipeline)

    def test_exported_candidate_ships_executable_remote_verifier(self):
        repository = Path(__file__).resolve().parents[1]
        source = repository / "scripts" / "verify_priority_1_3_remote.py"
        self.assertTrue(source.is_file(), "shipped remote verifier is absent")
        with tempfile.TemporaryDirectory() as directory:
            exported = Path(directory) / "candidate"
            (exported / "scripts").mkdir(parents=True)
            shutil.copy2(source, exported / "scripts" / source.name)
            shutil.copy2(repository / "release_argparse.py", exported / "release_argparse.py")
            verifier = exported / "scripts" / source.name
            completed = subprocess.run(
                [sys.executable, "-I", "-B", str(verifier), "--help"],
                cwd=exported, text=True, capture_output=True, check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_exact_exported_acceptance_cli_runs_isolated_with_shipped_verifier(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            exported = Path(directory) / "candidate"
            exported.mkdir()
            tracked = subprocess.run(
                ["git", "ls-files", "-z"], cwd=repository,
                capture_output=True, check=True,
            ).stdout.split(b"\0")
            for encoded in tracked:
                if not encoded:
                    continue
                relative = Path(encoded.decode("utf-8"))
                source = repository / relative
                if source.is_file():
                    destination = exported / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
            verifier_source = repository / "scripts" / "verify_priority_1_3_remote.py"
            verifier = exported / "scripts" / verifier_source.name
            verifier.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(verifier_source, verifier)
            shutil.copy2(repository / "release_argparse.py", exported / "release_argparse.py")
            completed = subprocess.run(
                [sys.executable, "-I", "-B",
                 str(exported / "deploy" / "verify_priority_1_3_acceptance.py"), "--help"],
                cwd=exported, text=True, capture_output=True, check=False,
            )
            verifier_executable = bool(verifier.stat().st_mode & 0o111)
            cookie = Path(directory) / "session-cookie"
            cookie.write_text("session=opaque", encoding="ascii")
            cookie.chmod(0o600)
            production = subprocess.run([
                sys.executable, "-I", "-B",
                str(exported / "deploy" / "verify_priority_1_3_acceptance.py"),
                "--boundary", "b1", "--mode", "deploy",
                "--ssh-target", "admin@192.0.2.10",
                "--ssh-identity-file", str(Path(directory) / "missing-identity"),
                "--ssh-known-hosts-file", str(cookie),
                "--app-root", str(exported), "--expected-sha", "1" * 40,
                "--expected-version", "9.8.7-test",
                "--session-cookie-file", str(cookie),
                "--evidence-file", str(Path(directory) / "evidence.json"),
                "--attempt-id", "a" * 32,
                "--acceptance-operation-id", "1" * 64,
                "--attempt-registry", "/run/matrix-priority-1-3/attempts/" + "a" * 32 + ".json",
            ], cwd=exported, text=True, capture_output=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--session-cookie-file", completed.stdout)
        self.assertTrue(verifier_executable)
        self.assertEqual(production.returncode, 2, production.stderr)
        self.assertNotIn("ModuleNotFoundError", production.stderr)

    def test_remote_verifier_reads_installed_bytes_and_rejects_manifest_mismatch(self):
        repository = Path(__file__).resolve().parents[1]
        verifier = repository / "scripts" / "verify_priority_1_3_remote.py"
        with tempfile.TemporaryDirectory() as directory:
            app_root = Path(directory) / "installed"
            app_root.mkdir()
            (app_root / "webapp.py").write_text('APPLICATION_VERSION = "9.8.7-test"\nVALUE = 1\n')
            protected = app_root / "protected.txt"
            protected.write_bytes(b"installed candidate bytes\n")
            protected.chmod(0o640)
            import hashlib
            manifest = {
                "schema": 1,
                "source_sha": "1" * 40,
                "candidate_tracked_files": ["protected.txt"],
                "files": [{"path": "protected.txt", "sha256": hashlib.sha256(protected.read_bytes()).hexdigest()}],
            }
            marker = app_root / ".matrix-release.json"
            marker.write_text(json.dumps(manifest))
            marker.chmod(0o400)
            command = [
                sys.executable, "-I", "-B", str(verifier),
                "--check", "protected-parity", "--mode", "deploy",
                "--app-root", str(app_root), "--attempt-id", "a" * 32,
                "--acceptance-operation-id", "1" * 64, "--evidence-id", "b" * 64,
                "--expected-sha", "1" * 40, "--expected-version", "9.8.7-test",
            ]
            completed = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(completed.stdout)
            self.assertEqual(report["source_sha"], "1" * 40)
            self.assertEqual(report["version"], "9.8.7-test")
            self.assertEqual(report["attempt_id"], "a" * 32)
            self.assertEqual(report["evidence_id"], "b" * 64)
            self.assertEqual(report["files"], {
                ".matrix-release.json": hashlib.sha256(marker.read_bytes()).hexdigest(),
                "protected.txt": manifest["files"][0]["sha256"],
            })
            self.assertEqual(report["modes"], {
                ".matrix-release.json": "0400", "protected.txt": "0640",
            })
            self.assertEqual(
                report["installed_digest"],
                hashlib.sha256(json.dumps({
                    "files": report["files"], "modes": report["modes"],
                }, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest(),
            )
            spoof = subprocess.run(command[:-4] + ["--expected-sha", "2" * 40, "--expected-version", "9.8.7-test"], text=True, capture_output=True, check=False)
            self.assertNotEqual(spoof.returncode, 0, "remote verifier accepted caller-reported proof values")
            protected.write_bytes(b"different installed bytes\n")
            mismatched = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertNotEqual(mismatched.returncode, 0)
            self.assertEqual(mismatched.stdout, "")

    def test_remote_evidence_schema_requires_exact_integer_one(self):
        acceptance = self._module()
        arguments = mock.Mock(
            attempt_id="a" * 32, acceptance_operation_id="1" * 64,
            expected_sha="1" * 40,
            expected_version="9.8.7-test", mode="deploy",
        )
        for schema in (True, False, 1.0, "1"):
            payload = {
                "schema": schema, "check": "rollback-parity",
                "attempt_id": arguments.attempt_id,
                "acceptance_operation_id": arguments.acceptance_operation_id,
                "evidence_id": "b" * 64,
                "mode": "deploy", "saved_output": True,
                "saved_signature": "c" * 64,
            }
            with self.subTest(schema=schema, schema_type=type(schema).__name__), self.assertRaisesRegex(
                ValueError, "remote evidence identity mismatch"
            ):
                acceptance._validated_payload(
                    "rollback-parity", json.dumps(payload), arguments, "b" * 64,
                )

    def test_installed_release_manifest_schema_requires_exact_integer_one(self):
        repository = Path(__file__).resolve().parents[1]
        verifier = repository / "scripts" / "verify_priority_1_3_remote.py"
        with tempfile.TemporaryDirectory() as directory:
            app_root = Path(directory) / "installed"
            app_root.mkdir()
            (app_root / "webapp.py").write_text(
                'APPLICATION_VERSION = "9.8.7-test"\nVALUE = 1\n'
            )
            protected = app_root / "protected.txt"
            protected.write_bytes(b"installed candidate bytes\n")
            import hashlib
            manifest_path = app_root / ".matrix-release.json"
            manifest = {
                "schema": 1, "source_sha": "1" * 40,
                "files": [{
                    "path": "protected.txt",
                    "sha256": hashlib.sha256(protected.read_bytes()).hexdigest(),
                }],
            }
            command = [
                sys.executable, "-I", "-B", str(verifier),
                "--check", "protected-parity", "--mode", "deploy",
                "--app-root", str(app_root), "--attempt-id", "a" * 32,
                "--acceptance-operation-id", "1" * 64, "--evidence-id", "b" * 64,
                "--expected-sha", "1" * 40, "--expected-version", "9.8.7-test",
            ]
            for schema in (True, False, 1.0, "1"):
                with self.subTest(schema=schema, schema_type=type(schema).__name__):
                    manifest["schema"] = schema
                    manifest_path.write_text(json.dumps(manifest))
                    completed = subprocess.run(
                        command, text=True, capture_output=True, check=False,
                    )
                    self.assertNotEqual(
                        completed.returncode, 0,
                        f"installed manifest accepted schema {schema!r}",
                    )
                    self.assertEqual(completed.stdout, "")

    def _module(self):
        return importlib.import_module("deploy.verify_priority_1_3_acceptance")

    def test_commit_terminal_wire_parser_enforces_exact_finite_positive_epoch_contract(self):
        acceptance = self._module()
        guarded = importlib.import_module("deploy.guarded_priority_1_3_release")
        attempt = "a" * 32
        request = {
            "schema": 1, "boundary": "b1", "phase": "initial-deploy",
            "attempt_id": attempt,
            "operation_id": __import__("hashlib").sha256(
                f"{attempt}\0commit".encode()
            ).hexdigest()[:32],
            "registry": "/run/matrix-priority-1-3/attempt.json",
            "evidence_digest": "b" * 64, "pending_note_id": "17",
            "guard_unit": f"matrix-p13-recover-{attempt}.service",
            "success_record": "/run/matrix-priority-1-3/success.json",
            "control_unit": f"matrix-p13-commit-{attempt}.service",
            "control_cgroup": f"/system.slice/matrix-p13-commit-{attempt}.service",
        }
        digest = "c" * 64
        terminal = {
            "schema": 1, "request": request,
            "request_digest": guarded._request_digest("remote-commit", request),
            "result_digest": digest, "committed_epoch": 100.25,
        }
        invalid = (
            ("nan", float("nan")), ("positive infinity", float("inf")),
            ("negative infinity", float("-inf")), ("true", True), ("false", False),
            ("integer zero", 0), ("float zero", 0.0),
            ("negative integer", -1), ("negative float", -1.25),
        )
        for label, value in invalid:
            with self.subTest(label=label):
                envelope = {"returncode": 0, "digest": digest,
                            "terminal": dict(terminal, committed_epoch=value)}
                result = acceptance.RemoteResult(0, json.dumps(envelope), "")
                with self.assertRaisesRegex(ValueError, "remote commit terminal time is invalid"):
                    acceptance.PinnedRemoteExecutor._parse_wire_envelope(
                        "remote-commit", result, request,
                    )
        for label, value in (("positive float", 100.25), ("positive integer", 100)):
            with self.subTest(label=label):
                envelope = {"returncode": 0, "digest": digest,
                            "terminal": dict(terminal, committed_epoch=value)}
                result = acceptance.RemoteResult(0, json.dumps(envelope), "")
                parsed = acceptance.PinnedRemoteExecutor._parse_wire_envelope(
                    "remote-commit", result, request,
                )
                self.assertEqual(parsed["terminal"]["committed_epoch"], value)
                self.assertIs(type(parsed["terminal"]["committed_epoch"]), type(value))

    def test_remote_verifier_rejects_preseeded_legacy_physical_files_for_every_attempt(self):
        repository = Path(__file__).resolve().parents[1]
        verifier = repository / "scripts" / "verify_priority_1_3_remote.py"
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "app"
            runtime = app / "data/acceptance-runtime"
            runtime.mkdir(parents=True)
            for name, byte in (("draft-frame.rgb", b"d"), ("saved-frame.rgb", b"s"), ("current-frame.rgb", b"s")):
                runtime.joinpath(name).write_bytes(byte * 6144)
            runtime.joinpath("journey-state.json").write_text(json.dumps({
                "owner_generation": 4, "foreign_rejected": True,
                "timeout_returned": True, "sleep_returned": True,
            }))
            fake_bin = Path(directory) / "bin"
            fake_bin.mkdir()
            marker = Path(directory) / "poisoned-systemctl"
            systemctl = fake_bin / "systemctl"
            systemctl.write_text(
                f"#!/bin/sh\nprintf 'poisoned' > {marker}\nprintf 'active\\n'\n"
            )
            systemctl.chmod(0o755)
            env = {**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]}
            for attempt, evidence in (("a" * 32, "b" * 64), ("c" * 32, "d" * 64)):
                for check in ("physical-journey", "rollback-parity"):
                    completed = subprocess.run([
                        sys.executable, "-I", "-B", str(verifier),
                        "--check", check, "--mode", "deploy", "--app-root", str(app),
                        "--attempt-id", attempt, "--acceptance-operation-id", "1" * 64,
                        "--evidence-id", evidence,
                        "--expected-sha", "1" * 40, "--expected-version", "9.8.7-test",
                    ], text=True, capture_output=True, env=env, check=False)
                    self.assertNotEqual(completed.returncode, 0, (attempt, check, completed.stdout))
                    self.assertEqual(completed.stdout, "")
            self.assertFalse(marker.exists())

    def test_remote_verifier_executes_real_tests_service_frames_and_state_checks(self):
        repository = Path(__file__).resolve().parents[1]
        verifier = repository / "scripts" / "verify_priority_1_3_remote.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "app"
            (app / "venv/bin").mkdir(parents=True)
            (app / "data").mkdir(parents=True)
            (app / "resources/media").mkdir(parents=True)
            created = subprocess.run(
                ["/usr/bin/python3", "-m", "venv", "--without-pip", str(app / "venv")],
                check=False, text=True, capture_output=True,
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            (app / "actual_app.py").write_text("def production_value(): return 73\n")
            (app / "tests").mkdir()
            (app / "tests/__init__.py").write_text("")
            (app / "tests/test_webapp.py").write_text(
                "import pathlib, unittest\nfrom actual_app import production_value\n"
                "R=pathlib.Path(__file__).resolve().parents[1]\n"
                "class WebAppTests(unittest.TestCase):\n"
                " def test_physical_draft_preview_api_is_paired_bounded_private_and_non_persistent(self): "
                "self.assertEqual(production_value(),73);(R/'focused.marker').write_text('ran')\n"
                " def test_discovery_runs_actual_test(self): "
                "self.assertEqual(production_value(),73);(R/'full.marker').write_text('ran')\n"
            )
            (app / "data/config.json").write_text('{"revision":1}')
            (app / "data/paired-devices.json").write_text("[]")
            (app / "resources/media/example.bin").write_bytes(b"media")
            from acceptance_artifacts import AcceptanceArtifactTransaction
            runtime = app / "data/acceptance-runtime"
            publisher = AcceptanceArtifactTransaction(runtime, {
                "attempt_id": "a" * 32, "acceptance_operation_id": "1" * 64,
                "evidence_id": "b" * 64, "expected_source_sha": "1" * 40,
                "expected_version": "9.8.7-test", "mode": "deploy",
                "nonce": "f" * 64, "owner_revision": 1,
            }, owner_generation=4)
            publisher.publish_draft(b"d" * 6144, frame_revision=10)
            publisher.publish_saved_rollback(b"s" * 6144, frame_revision=11, saved_revision=9)
            env = dict(os.environ)
            base = [sys.executable, "-I", "-B", str(verifier),
                    "--mode", "deploy", "--app-root", str(app),
                    "--attempt-id", "a" * 32, "--acceptance-operation-id", "1" * 64,
                    "--evidence-id", "b" * 64,
                    "--expected-sha", "1" * 40, "--expected-version", "9.8.7-test"]
            reports = {}
            for check in ("production-tests", "persistent-state"):
                completed = subprocess.run(base + ["--check", check], text=True, capture_output=True, env=env, check=False)
                self.assertEqual(completed.returncode, 0, (check, completed.stderr))
                reports[check] = json.loads(completed.stdout)
            physical_arguments = remote_verifier.build_parser().parse_args(
                base[4:] + ["--check", "physical-journey"]
            )
            active = subprocess.CompletedProcess(
                ["/usr/bin/systemctl"], 0, stdout="active\n", stderr="",
            )
            with mock.patch.object(
                remote_verifier.subprocess, "run", return_value=active,
            ):
                reports["physical-journey"] = remote_verifier.run(physical_arguments)
            completed = subprocess.run(
                base + ["--check", "rollback-parity"],
                text=True, capture_output=True, env=env, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            reports["rollback-parity"] = json.loads(completed.stdout)
            self.assertEqual((app / "focused.marker").read_text(), "ran")
            self.assertEqual((app / "full.marker").read_text(), "ran")
            self.assertGreater(reports["production-tests"]["focused"]["stderr_size"], 0)
            self.assertGreater(reports["production-tests"]["full"]["stderr_size"], 0)
            self.assertTrue(reports["physical-journey"]["service_active"])
            self.assertEqual((reports["physical-journey"]["width"], reports["physical-journey"]["height"]), (64, 32))
            self.assertTrue(reports["rollback-parity"]["saved_output"])
            self.assertEqual(set(reports["persistent-state"]) - {"schema", "check", "attempt_id", "acceptance_operation_id", "evidence_id"}, {"config", "paired", "media"})
            runtime.joinpath("transactions", "a" * 32, "1" * 64, "current.rgb").write_bytes(b"x" * 6144)
            failed = subprocess.run(base + ["--check", "rollback-parity"], text=True, capture_output=True, env=env, check=False)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(failed.stdout, "")

    def test_real_local_http_and_chromium_journeys_use_explicit_ipv4_and_cookie(self):
        acceptance = self._module()
        observed = []
        config = {"revision": 1, "schedule": [], "custom_modules": [], "media": {}}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def _send(self, status, payload, content_type="application/json"):
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
            def do_GET(self):
                observed.append(("GET", self.path, self.headers.get("Cookie")))
                if self.headers.get("Cookie") != "session=browser-fixture": return self._send(401, {"error": "unauthorized"})
                if self.path == "/api/config": return self._send(200, config)
                if self.path == "/api/status": return self._send(200, {"state": "active"})
                if self.path == "/api/display-frame": return self._send(200, b"png", "image/png")
                return self._send(200, b"<html><body>paired</body></html>", "text/html")
            def do_POST(self):
                observed.append(("POST", self.path, self.headers.get("Cookie"), self.headers.get("X-Matrix-Action")))
                length = int(self.headers.get("Content-Length", "0")); payload = json.loads(self.rfile.read(length))
                if self.headers.get("Cookie") != "session=browser-fixture" or self.headers.get("X-Matrix-Action") != "paired-request": return self._send(403, {"error": "forbidden"})
                if self.path == "/api/release-acceptance/transaction":
                    self.server.nonce = payload["nonce"]
                    return self._send(202, {"state": "INIT", "nonce": payload["nonce"],
                        "acceptance_operation_id": payload["acceptance_operation_id"],
                        "owner_generation": payload["generation"]})
                if self.path == "/api/release-acceptance/transaction/status":
                    return self._send(200, {"state": "SAVED_ROLLBACK_PUBLISHED", "nonce": payload["nonce"],
                        "acceptance_operation_id": "1" * 64,
                        "owner_generation": payload["generation"], "saved_revision": 9,
                        "draft_revision": 10, "current_revision": 11})
                if self.path.endswith("/clock_lava"):
                    self.server.owner = payload["preview_instance"]
                    self.server.heartbeat_admission_at = time.monotonic() + 5
                    return self._send(202, {"generation": payload["generation"], "timeout_seconds": payload["timeout_seconds"], "heartbeat_after_seconds": 5})
                if self.path.endswith("/heartbeat") and payload["preview_instance"] == "foreign": return self._send(409, {"error": "foreign"})
                if self.path.endswith("/heartbeat"):
                    admitted = payload["preview_instance"] == self.server.owner and time.monotonic() >= self.server.heartbeat_admission_at
                    if admitted: self.server.heartbeat_admission_at = time.monotonic() + 5
                    return self._send(202 if admitted else 409, {"active": admitted})
                if self.path.endswith("/cancel"): return self._send(202, {"cancelled": True})
                return self._send(404, {})
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            http_started = time.monotonic()
            authorization_root = Path(tempfile.mkdtemp())
            authorization = authorization_root / "acceptance-authorization"
            authorization.write_text("e" * 64); authorization.chmod(0o600)
            transaction = {"attempt_id": "a" * 32, "acceptance_operation_id": "1" * 64,
                "evidence_id": "b" * 64, "expected_source_sha": "c" * 40, "expected_version": "test",
                "mode": "deploy"}
            with mock.patch.object(acceptance, "_LOCAL_BASE_URL", base), mock.patch("secrets.token_hex", side_effect=["owner", "d" * 64, "foreign"]):
                http_report = acceptance.LocalHttpJourney(transaction, authorization).run(base, "session=browser-fixture", operation_timeout=300, aggregate_deadline=time.monotonic() + 30)
            self.assertGreaterEqual(time.monotonic() - http_started, 5)
            self.assertEqual(http_report["foreign_rejected"], True)
            self.assertEqual(sum(item[1].endswith("/heartbeat") for item in observed), 3)
            with mock.patch.object(acceptance, "_LOCAL_BASE_URL", base):
                chromium_report = acceptance.LocalChromiumJourney().run(base, "session=browser-fixture", operation_timeout=300, aggregate_deadline=time.monotonic() + 30)
            self.assertEqual(chromium_report, {"paired": True, "explicit_ipv4": True, "status_checked": True, "frame_checked": True})
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
            if "authorization_root" in locals(): shutil.rmtree(authorization_root)
        self.assertTrue(any(item[1] == "/api/status" and item[2] == "session=browser-fixture" for item in observed))
        self.assertTrue(any(item[1] == "/api/display-frame" and item[2] == "session=browser-fixture"
                            for item in observed))

    def test_one_aggregate_deadline_yields_only_positive_remaining_operation_time(self):
        acceptance = self._module()
        with mock.patch.object(acceptance.time, "monotonic", return_value=90.25):
            self.assertEqual(acceptance._remaining_timeout(100.0, 300), 9.75)
            self.assertEqual(acceptance._remaining_timeout(100.0, 5), 5.0)
        with mock.patch.object(acceptance.time, "monotonic", return_value=100.0):
            with self.assertRaises(TimeoutError):
                acceptance._remaining_timeout(100.0, 300)

    def test_session_cookie_rejects_newline_injection_before_normalization(self):
        acceptance = self._module()
        with tempfile.TemporaryDirectory() as directory:
            cookie = Path(directory) / "cookie"
            cookie.write_bytes(b"session=opaque\n")
            cookie.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "session cookie is invalid"):
                acceptance._read_session_cookie(cookie)

    def test_acceptance_operation_id_is_required_canonical_and_evidence_bound(self):
        acceptance = self._module()
        required = [
            "--boundary", "b1", "--mode", "deploy",
            "--ssh-target", "admin@192.0.2.10", "--ssh-identity-file", "/pins/id",
            "--ssh-known-hosts-file", "/pins/known", "--app-root", "/srv/matrix",
            "--expected-sha", "1" * 40, "--expected-version", "test",
            "--session-cookie-file", "/pins/cookie", "--evidence-file", "/evidence/result.json",
            "--attempt-id", "a" * 32,
            "--attempt-registry", "/run/matrix-priority-1-3/attempts/" + "a" * 32 + ".json",
            "--release-spec", "/stage/release-spec.json",
            "--release-spec-digest", "d" * 64,
        ]
        with self.assertRaises(SystemExit):
            acceptance.build_parser().parse_args(required)
        first = acceptance.build_parser().parse_args(
            required + ["--acceptance-operation-id", "1" * 64]
        )
        second = acceptance.build_parser().parse_args(
            required + ["--acceptance-operation-id", "2" * 64]
        )
        self.assertNotEqual(
            acceptance._evidence_identity(first), acceptance._evidence_identity(second),
        )
        for malformed in ("1" * 63, "A" * 64, "../operation", "1" * 65):
            arguments = acceptance.build_parser().parse_args(
                required + ["--acceptance-operation-id", malformed]
            )
            with self.assertRaisesRegex(ValueError, "acceptance operation identity"):
                acceptance._evidence_identity(arguments)

    def test_protected_parity_is_bound_to_the_locally_approved_release_spec(self):
        acceptance = self._module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release_spec = root / "release-spec.json"
            approved_digest = "a" * 64
            approved = {
                "version": 1,
                "files": [{
                    "path": "webapp.py", "sha256": approved_digest,
                    "uid": 0, "gid": 0, "mode": "0644",
                }],
            }
            encoded = (json.dumps(approved, sort_keys=True, separators=(",", ":")) + "\n").encode()
            release_spec.write_bytes(encoded)
            release_spec.chmod(0o400)
            arguments = argparse.Namespace(
                attempt_id="a" * 32,
                acceptance_operation_id="1" * 64,
                expected_sha="1" * 40,
                expected_version="9.8.7-test",
                release_spec=str(release_spec),
                release_spec_digest=hashlib.sha256(encoded).hexdigest(),
            )
            evidence_id = "e" * 64
            common = {
                "schema": 1, "check": "protected-parity",
                "attempt_id": arguments.attempt_id,
                "acceptance_operation_id": arguments.acceptance_operation_id,
                "evidence_id": evidence_id,
                "source_sha": arguments.expected_sha,
                "version": arguments.expected_version,
            }
            approved_record = {
                "files": {"webapp.py": approved_digest},
                "modes": {"webapp.py": "0644"},
            }
            approved_payload = {
                **common,
                "installed_digest": hashlib.sha256(json.dumps(
                    approved_record, sort_keys=True, separators=(",", ":")
                ).encode("ascii")).hexdigest(),
                **approved_record,
            }
            accepted = acceptance._validated_payload(
                "protected-parity", json.dumps(approved_payload), arguments, evidence_id,
            )
            self.assertEqual(accepted["files"], approved_record["files"])
            self.assertEqual(accepted["modes"], approved_record["modes"])

            arbitrary_record = {
                "files": {"attacker.py": "f" * 64},
                "modes": {"attacker.py": "0777"},
            }
            payload = {
                **common,
                "installed_digest": hashlib.sha256(json.dumps(
                    arbitrary_record, sort_keys=True, separators=(",", ":")
                ).encode("ascii")).hexdigest(),
                **arbitrary_record,
            }
            with self.assertRaisesRegex(ValueError, "installed protected-byte proof mismatch"):
                acceptance._validated_payload(
                    "protected-parity", json.dumps(payload), arguments, evidence_id,
                )

    def test_cookie_is_pinned_and_local_http_chromium_deadlines_are_required(self):
        acceptance = self._module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cookie = root / "cookie"
            cookie.write_text("session=opaque-paired-session")
            cookie.chmod(0o600)
            evidence = root / "evidence.json"
            protected = {"files": {"webapp.py": "f" * 64}, "modes": {"webapp.py": "0644"}}
            release_spec = root / "release-spec.json"
            release_spec_bytes = (json.dumps({"version": 1, "files": [{
                "path": "webapp.py", "sha256": "f" * 64,
                "uid": 0, "gid": 0, "mode": "0644",
            }]}, sort_keys=True, separators=(",", ":")) + "\n").encode()
            release_spec.write_bytes(release_spec_bytes)
            release_spec.chmod(0o400)
            arguments = acceptance.build_parser().parse_args([
                "--boundary", "b1", "--mode", "deploy",
                "--ssh-target", "admin@192.0.2.10", "--ssh-identity-file", str(cookie),
                "--ssh-known-hosts-file", str(cookie), "--app-root", "/srv/matrix",
                "--expected-sha", "1" * 40, "--expected-version", "9.8.7-test",
                "--session-cookie-file", str(cookie), "--evidence-file", str(evidence),
                "--attempt-id", "a" * 32,
                "--acceptance-operation-id", "1" * 64,
                "--attempt-registry", "/run/matrix-priority-1-3/attempts/" + "a" * 32 + ".json",
                "--release-spec", str(release_spec),
                "--release-spec-digest", hashlib.sha256(release_spec_bytes).hexdigest(),
            ])
            evidence_id = acceptance._evidence_identity(arguments)
            class Executor:
                def __init__(self): self.calls = []
                def run_remote(self, attempt_id, operation_id, argv, *, registry, run_as, operation_timeout):
                    self.calls.append((attempt_id, operation_id, tuple(argv), operation_timeout))
                    check = argv[argv.index("--check") + 1]
                    common = {"schema": 1, "check": check, "attempt_id": "a" * 32,
                              "acceptance_operation_id": "1" * 64, "evidence_id": evidence_id}
                    payloads = {
                        "protected-parity": {**common, "source_sha": "1" * 40, "version": "9.8.7-test", "installed_digest": hashlib.sha256(json.dumps(protected, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest(), **protected},
                        "production-tests": {**common, "focused": {"passed": True, "duration_ms": 1, "command_digest": "c" * 64, "stderr_size": 17, "stderr_digest": "e" * 64}, "full": {"passed": True, "duration_ms": 2, "command_digest": "d" * 64, "stderr_size": 23, "stderr_digest": "f" * 64}},
                        "persistent-state": {**common, "config": "c" * 64, "paired": "a" * 64, "media": "b" * 64},
                        "physical-journey": {**common, "service_active": True, "draft_signature": "d" * 64, "saved_signature": "a" * 64, "width": 64, "height": 32, "owner_generation": 7, "foreign_rejected": True, "timeout_returned": True, "sleep_returned": True},
                        "rollback-parity": {**common, "mode": "deploy", "saved_output": True, "saved_signature": "a" * 64},
                    }
                    return acceptance.RemoteResult(0, json.dumps(payloads[check]), "")
            class Client:
                def __init__(self, result): self.result, self.calls = result, []
                def run(self, base_url, cookie_value, *, operation_timeout, aggregate_deadline):
                    self.calls.append((base_url, cookie_value, operation_timeout, aggregate_deadline))
                    return self.result
            http = Client({"owner_generation": 7, "nonce": "f" * 64,
                "acceptance_operation_id": "1" * 64, "saved_revision": 9, "draft_revision": 10, "current_revision": 11,
                "foreign_rejected": True, "timeout_returned": True,
                "saved_output_returned": True})
            chromium = Client({"paired": True, "explicit_ipv4": True, "status_checked": True, "frame_checked": True})
            executor = Executor()
            self.assertEqual(acceptance.run_acceptance(arguments, executor, http_client=http, chromium_client=chromium), 0)
            self.assertTrue(evidence.is_file())
            self.assertEqual(oct(evidence.stat().st_mode & 0o777), "0o600")
            self.assertEqual(len(http.calls), 1)
            self.assertEqual(len(chromium.calls), 1)
            self.assertEqual(http.calls[0][0], "http://127.0.0.1:5000")
            self.assertEqual(http.calls[0][1], "session=opaque-paired-session")
            self.assertGreater(http.calls[0][2], 10)
            self.assertGreater(http.calls[0][3], 0)
            self.assertNotIn("opaque-paired-session", evidence.read_text())
            operation_name = "acceptance:" + "1" * 64 + ":production-tests"
            self.assertGreater(dict((call[1], call[3]) for call in executor.calls)[operation_name], 6000)
            real_write = acceptance.os.write
            def short_write(descriptor, data):
                return real_write(descriptor, bytes(data[:3]))
            with mock.patch.object(acceptance.os, "write", side_effect=short_write):
                self.assertEqual(acceptance.run_acceptance(arguments, Executor(), http_client=http, chromium_client=chromium), 0)
            json.loads(evidence.read_text())
            durable_before = evidence.read_bytes()
            with mock.patch.object(acceptance.os, "replace", side_effect=OSError("injected replace failure")):
                self.assertNotEqual(acceptance.run_acceptance(arguments, Executor(), http_client=http, chromium_client=chromium), 0)
            self.assertEqual(evidence.read_bytes(), durable_before)
            self.assertFalse(any(path.name.startswith(".evidence.json.") for path in root.iterdir()))
            cookie.chmod(0o644)
            evidence_before = evidence.read_bytes()
            self.assertNotEqual(acceptance.run_acceptance(arguments, Executor(), http_client=http, chromium_client=chromium), 0)
            self.assertEqual(evidence.read_bytes(), evidence_before)
            cookie.chmod(0o600)
            linked = root / "cookie-link"
            linked.symlink_to(cookie)
            arguments.session_cookie_file = str(linked)
            self.assertNotEqual(acceptance.run_acceptance(arguments, Executor(), http_client=http, chromium_client=chromium), 0)

    def test_b1_deploy_and_rollback_acceptance_is_exact_fail_closed_and_sanitized(self):
        acceptance = self._module()
        required = [
            "--boundary", "b1", "--mode", "deploy",
            "--ssh-target", "admin@192.0.2.10",
            "--ssh-identity-file", "/pins/id",
            "--ssh-known-hosts-file", "/pins/known_hosts",
            "--app-root", "/srv/matrix", "--expected-sha", "a" * 40,
            "--expected-version", "0.3.58-preview",
            "--session-cookie-file", "/pins/cookie",
            "--evidence-file", "/evidence/result.json",
            "--attempt-id", "b" * 32,
            "--acceptance-operation-id", "1" * 64,
            "--attempt-registry", "/run/matrix-priority-1-3/attempts/" + "b" * 32 + ".json",
            "--release-spec", "/stage/release-spec.json",
            "--release-spec-digest", "d" * 64,
        ]
        parsed = acceptance.build_parser().parse_args(required)
        self.assertEqual((parsed.boundary, parsed.mode, parsed.attempt_id), ("b1", "deploy", "b" * 32))
        for missing in ("--ssh-target", "--expected-sha", "--evidence-file", "--attempt-id", "--acceptance-operation-id", "--session-cookie-file", "--release-spec", "--release-spec-digest"):
            broken = required[:]
            index = broken.index(missing)
            del broken[index:index + 2]
            with self.subTest(missing=missing), self.assertRaises(SystemExit):
                acceptance.build_parser().parse_args(broken)
        with self.assertRaises(SystemExit):
            acceptance.build_parser().parse_args(required + ["--unexpected", "fallback"])

    def test_pinned_control_transport_rejects_boolean_false_and_noncanonical_success_envelopes(self):
        acceptance = self._module()
        from deploy import guarded_priority_1_3_release as guarded
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity_file = root / "identity"
            known_hosts = root / "known_hosts"
            identity_file.write_text("private pin")
            known_hosts.write_text("host pin")
            identity_file.chmod(0o600)
            known_hosts.chmod(0o600)
            digest = "d" * 64
            stop = {
                "schema": 1, "attempt_id": "a" * 32, "operation_id": "b" * 32,
                "registry": "/run/attempt.json", "unit": "matrix-p13-op-a.service",
                "cgroup": "/system.slice/matrix-p13-op-a.service",
            }
            document = {
                "schema": 1, "transaction_version": 1, "boundary": "b1",
                "expected_sha": "a" * 40, "expected_version": "1",
                "previous_sha": "b" * 40, "previous_version": "0",
                "release_stamp": "stamp", "release_manifest_digest": "c" * 64,
                "backup_manifest_digest": "d" * 64, "release_spec_digest": "e" * 64,
                "destination_identity": {"root": "matrix", "service": "matrix-web.service"},
                "gitlab": {"project": "2", "mr": "73"},
            }
            release_key = guarded.canonical_release_identity(document).release_key
            create = {
                "schema": 1, "registry_root": "/run/transactions", "release_key": release_key,
                "release_document": document,
                "execution_contract": {"schema": 1, "stage": "/stage", "bundle": "/bundle",
                                       "destination_root": "/destination", "service": "matrix-web.service"},
                "created_epoch": 1000, "original_deadline_epoch": 8200,
            }
            inspect = {
                "schema": 1, "registry_root": "/run/transactions", "release_key": release_key,
                "transaction_id": "f" * 32,
            }
            commit_query = {
                "schema": 1, "query": True, "boundary": "b1", "phase": "initial-deploy",
                "attempt_id": "a" * 32, "operation_id": "b" * 32,
                "registry": "/run/attempt.json", "evidence_digest": "c" * 64,
                "success_record": "/run/success.json",
            }
            cases = (
                ("boolean false returncode", "remote-stop", stop,
                 {"returncode": False, "digest": digest}),
                ("extra stop field", "remote-stop", stop,
                 {"returncode": 0, "digest": digest, "extra": True}),
                ("create missing terminal", "remote-transaction-create-or-query", create,
                 {"returncode": 0, "digest": digest}),
                ("inspect untyped terminal", "remote-transaction-inspect", inspect,
                 {"returncode": 0, "digest": digest, "terminal": []}),
                ("commit query missing terminal", "remote-commit", commit_query,
                 {"returncode": 0, "digest": digest}),
            )
            for label, kind, request, envelope in cases:
                with self.subTest(label=label):
                    def runner(argv, **kwargs):
                        del kwargs
                        return subprocess.CompletedProcess(argv, 0, json.dumps(envelope), "")
                    executor = acceptance.PinnedRemoteExecutor(
                        "admin@192.0.2.10", identity_file, known_hosts, runner=runner,
                    )
                    with self.assertRaisesRegex(ValueError, "remote .*typed|remote .*result"):
                        executor.control_remote(kind, request, operation_timeout=5)

    def test_remote_start_rejects_noncanonical_stdout_stderr_envelope(self):
        acceptance = self._module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = root / "identity"
            known_hosts = root / "known_hosts"
            identity.write_text("private pin")
            known_hosts.write_text("host pin")
            identity.chmod(0o600)
            known_hosts.chmod(0o600)

            def runner(argv, **kwargs):
                del kwargs
                envelope = {
                    "returncode": 0,
                    "unit": "matrix-p13-op-" + "a" * 32 + "-" + "b" * 32 + ".service",
                    "cgroup": "/system.slice/matrix-p13-op-" + "a" * 32 + "-" + "b" * 32 + ".service",
                    "stdout": "alternate success output",
                    "stderr": "alternate success error",
                }
                return subprocess.CompletedProcess(argv, 0, json.dumps(envelope), "")

            executor = acceptance.PinnedRemoteExecutor(
                "admin@192.0.2.10", identity, known_hosts, runner=runner,
            )
            with self.assertRaisesRegex(ValueError, "remote helper returned invalid typed output"):
                executor.run_remote(
                    "a" * 32, "op-1", ["/usr/bin/python3", "-I", "check.py"],
                    registry="/run/matrix-priority-1-3/attempts/" + "a" * 32 + ".json",
                    run_as="admin", operation_timeout=9,
                )

    def test_pinned_remote_executor_ignores_poisoned_agent_and_config_and_has_no_alternate_path(self):
        acceptance = self._module()
        import os
        import subprocess
        from unittest import mock
        from deploy import guarded_priority_1_3_release as guarded
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = root / "identity"
            known_hosts = root / "known_hosts"
            identity.write_text("private pin")
            known_hosts.write_text("host pin")
            identity.chmod(0o600)
            known_hosts.chmod(0o600)
            poisoned_home = root / "home"
            (poisoned_home / ".ssh").mkdir(parents=True)
            (poisoned_home / ".ssh/config").write_text(
                "Host *\n  ProxyCommand /tmp/steal\n  IdentityAgent /tmp/agent\n"
            )
            captured = {}
            def runner(argv, **kwargs):
                helper = argv[-3]
                request = guarded.decode_privileged_request(helper, argv[-1])
                captured.setdefault("argv", tuple(argv))
                captured.setdefault("kwargs", kwargs)
                unit = f"matrix-p13-op-{request['attempt_id']}-{request['operation_id']}.service"
                cgroup = f"/system.slice/{unit}"
                if helper == "remote-start":
                    envelope = {"returncode": 0, "unit": unit, "cgroup": cgroup}
                else:
                    digest = guarded._operation_output_digest(
                        request["attempt_id"], request["operation_id"], request["registry"],
                        unit, cgroup, 0, "", "",
                    )
                    envelope = {
                        "returncode": 0, "state": "TERMINAL", "attempt_id": request["attempt_id"],
                        "operation_id": request["operation_id"], "registry": request["registry"],
                        "unit": unit, "cgroup": cgroup, "registry_digest": "d" * 64,
                        "exit_status": 0, "stdout": "", "stderr": "", "output_digest": digest,
                    }
                return subprocess.CompletedProcess(argv, 0, json.dumps(envelope), "")
            poisoned = {
                "HOME": str(poisoned_home), "SSH_AUTH_SOCK": "/tmp/poison-agent",
                "GIT_SSH_COMMAND": "ssh -F /tmp/poison", "ALL_PROXY": "socks5://poison",
            }
            with mock.patch.dict(os.environ, poisoned, clear=False):
                executor = acceptance.PinnedRemoteExecutor(
                    "admin@192.0.2.10", identity, known_hosts, runner=runner
                )
                result = executor.run_remote(
                    "a" * 32, "op-1", ["/usr/bin/python3", "-I", "check.py"],
                    registry="/run/matrix-priority-1-3/attempts/" + "a" * 32 + ".json",
                    run_as="admin", operation_timeout=9,
                )
            self.assertIsInstance(result, acceptance.OperationResult)
            self.assertEqual(result.returncode, 0)
            argv = captured["argv"]
            self.assertEqual(argv[:3], ("/usr/bin/ssh", "-F", "/dev/null"))
            for exact in (
                "BatchMode=yes", "IdentitiesOnly=yes", "IdentityAgent=none",
                "StrictHostKeyChecking=yes", f"UserKnownHostsFile={known_hosts}",
                "ConnectTimeout=10",
            ):
                self.assertIn(exact, argv)
            self.assertIn(str(identity), argv)
            self.assertEqual(argv[-3:-1], ("remote-start", "--request-b64"))
            self.assertNotIn("/tmp/poison", " ".join(argv))
            self.assertFalse(captured["kwargs"].get("shell", False))
            child_env = captured["kwargs"]["env"]
            self.assertNotIn("SSH_AUTH_SOCK", child_env)
            self.assertNotIn("GIT_SSH_COMMAND", child_env)
            self.assertNotIn("ALL_PROXY", child_env)
            self.assertFalse(hasattr(executor, "run_local"))
            with self.assertRaises(ValueError):
                executor.control_remote("arbitrary", {}, operation_timeout=5)
            identity_link = root / "identity-link"
            identity_link.symlink_to(identity)
            with self.assertRaises(ValueError):
                acceptance.PinnedRemoteExecutor(
                    "admin@192.0.2.10", identity_link, known_hosts, runner=runner
                )
