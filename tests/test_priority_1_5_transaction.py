import fcntl
import hashlib
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
        self.events = []

    def is_active(self):
        return self.active

    def stop(self):
        self.events.append("stop")
        self.active = False

    def start_ready(self):
        self.events.append("start-ready")
        self.active = True

    def status(self, *, warning_since=None):
        return {
            "service": "matrix-web.service", "active": self.active,
            "substate": "running", "http_status": 302, "main_pid": 1234,
            "n_restarts": 0, "control_group": "/system.slice/matrix-web.service",
            "display_lock": {
                "path": "/run/matrix-display/matrix-display.lock", "dev": 1,
                "ino": 2, "uid": 0, "gid": 0, "mode": "0644", "locked": True,
            },
            "warning_count": 0,
        }


class Priority15TransactionTests(unittest.TestCase):
    def _git(self, root, *args):
        return subprocess.check_output(
            ["/usr/bin/git", "-C", str(root), *args], text=True,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
        ).strip()

    def test_b1_cli_is_exact_and_source_rollback_is_crash_safe(self):
        source_script = Path(__file__).parents[1] / "deploy/priority_1_5_transaction.py"
        with tempfile.TemporaryDirectory() as isolated_temporary:
            isolated_release = Path(isolated_temporary) / "release"
            isolated_deploy = isolated_release / "deploy"
            isolated_deploy.mkdir(parents=True)
            isolated_script = isolated_deploy / source_script.name
            isolated_script.write_bytes(source_script.read_bytes())
            completed = subprocess.run(
                [sys.executable, "-I", "-B", str(isolated_script), "--help"],
                cwd="/", capture_output=True, text=True, timeout=10,
                env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

        from deploy import priority_1_5_transaction as transaction

        expected = {"build-b1", "prepare-b1", "deploy", "status", "recover", "rollback", "redeploy", "finalize"}
        parser = transaction.build_parser()
        self.assertEqual(set(parser._subparsers._group_actions[0].choices), expected)
        for command in expected - {"build-b1", "prepare-b1", "recover"}:
            with self.subTest(command=command, duplicate=True), self.assertRaises(SystemExit) as caught:
                parser.parse_args([command, "--state", "/safe/state.json", "--state", "/safe/state.json"])
            self.assertEqual(caught.exception.code, 2)
        with self.assertRaises(SystemExit):
            parser.parse_args(["deploy", "--state", "/safe/state.json", "--destination", "/tmp/no"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["/usr/bin/git", "init", "-q", str(repository)], check=True)
            subprocess.run(["/usr/bin/git", "-C", str(repository), "config", "user.name", "test"], check=True)
            subprocess.run(["/usr/bin/git", "-C", str(repository), "config", "user.email", "test@example.invalid"], check=True)
            (repository / "app.py").write_bytes(b"baseline\n")
            subprocess.run(["/usr/bin/git", "-C", str(repository), "add", "app.py"], check=True)
            subprocess.run(["/usr/bin/git", "-C", str(repository), "commit", "-qm", "baseline"], check=True)
            baseline = self._git(repository, "rev-parse", "HEAD^{commit}")
            baseline_tree = self._git(repository, "rev-parse", "HEAD^{tree}")
            (repository / "app.py").write_bytes(b"candidate\n")
            subprocess.run(["/usr/bin/git", "-C", str(repository), "commit", "-qam", "candidate"], check=True)
            source = self._git(repository, "rev-parse", "HEAD^{commit}")
            source_tree = self._git(repository, "rev-parse", "HEAD^{tree}")
            diff = subprocess.check_output([
                "/usr/bin/git", "-C", str(repository), "diff", "--binary", "--full-index",
                "--no-ext-diff", f"{baseline}...{source}",
            ])
            reviewed = hashlib.sha256(diff).hexdigest()
            stage = root / "stage"
            build_argv = [
                "build-b1", "--repository", str(repository), "--baseline-commit", baseline,
                "--source-commit", source, "--expected-reviewed-diff-sha256", reviewed,
                "--stage-root", str(stage), "--python", sys.executable,
            ]
            self.assertEqual(transaction.main(build_argv), 0)
            release_manifest_bytes = (stage / "release-manifest.json").read_bytes()
            release_manifest = json.loads(release_manifest_bytes)
            self.assertEqual(release_manifest["files"][0]["path"], "app.py")
            self.assertEqual((stage / "release/app.py").read_bytes(), b"candidate\n")
            self.assertEqual(release_manifest_bytes, transaction.canonical_json(release_manifest))
            self.assertEqual(stat.S_IMODE(stage.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((stage / "release-manifest.json").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((stage / "physical-fixtures.json").stat().st_mode), 0o600)
            self.assertTrue((stage / "physical-fixtures.json").is_file())

            outside = root / "outside"
            outside.mkdir()
            linked_parent = stage / "release" / "linked-parent"
            linked_parent.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "parent path is unsafe"):
                transaction._contained_path(stage / "release", "linked-parent/file.py")
            linked_parent.unlink()

            fixture_path = stage / "physical-fixtures.json"
            fixture_manifest = json.loads(fixture_path.read_bytes())
            for field, wrong in (("width", 99), ("durations_ms", [200])):
                tampered = json.loads(json.dumps(fixture_manifest))
                tampered["fixtures"][0][field] = wrong
                with self.subTest(fixture_tamper=field), self.assertRaisesRegex(
                        ValueError, "fixture .*mismatch"):
                    transaction._validate_fixture_manifest(
                        transaction.canonical_json(tampered), stage,
                    )

            bad_stage = root / "bad-stage"
            bad_argv = list(build_argv)
            bad_argv[bad_argv.index("--expected-reviewed-diff-sha256") + 1] = "0" * 64
            bad_argv[bad_argv.index("--stage-root") + 1] = str(bad_stage)
            with self.assertRaisesRegex(ValueError, "reviewed diff"):
                transaction.main(bad_argv)
            self.assertFalse(bad_stage.exists())

            destination = root / "destination"
            destination.mkdir()
            (destination / "app.py").write_bytes(b"baseline\n")
            authority_uid, authority_gid = os.geteuid(), os.getegid()
            if os.geteuid() == 0:
                authority_uid = authority_gid = 65534
                os.chown(destination, authority_uid, authority_gid)
                os.chown(destination / "app.py", authority_uid, authority_gid)
            private = {}
            for relative, value in (("data/config.json", b"config-private"), ("data/paired-devices.json", b"paired-private"), ("resources/media/private.bin", b"media-private")):
                path = destination / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value)
                private[relative] = value
            backup = root / "backup"
            state = root / "authority/state.json"
            prepare = [
                "prepare-b1", "--release-root", str(stage / "release"),
                "--release-manifest", str(stage / "release-manifest.json"),
                "--fixture-manifest", str(stage / "physical-fixtures.json"),
                "--expected-baseline-source-sha", baseline,
                "--expected-baseline-tree-sha", baseline_tree,
                "--expected-source-sha", source, "--expected-tree-sha", source_tree,
                "--destination", str(destination), "--backup-root", str(backup),
                "--service", "matrix-web.service", "--interpreter", sys.executable,
                "--state", str(state),
            ]
            service = _Service()
            with patch.object(transaction, "SystemdService", return_value=service):
                self.assertEqual(transaction.main(prepare), 0)
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
            self.assertFalse(Path(str(state) + ".identity").exists())
            state_value = json.loads(state.read_bytes())
            self.assertEqual(
                set(state_value["self"]),
                {"path", "dev", "ino", "uid", "gid", "mode", "sha256"},
            )
            self.assertEqual(state_value["destination_owner"], {
                "uid": authority_uid, "gid": authority_gid,
            })
            baseline_manifest = json.loads((backup / "baseline-source-manifest.json").read_bytes())
            self.assertEqual([entry["path"] for entry in baseline_manifest["files"]], ["app.py"])
            self.assertFalse(any(name in state.read_text() for name in ("config-private", "paired-private", "media-private")))

            # Every interruption after a source publication remains inactive and recoverable.
            tx = transaction.Priority15Transaction.load(state, service=service)
            for requested, target, expected_bytes in (
                ("deploy", "candidate", b"candidate\n"),
                ("rollback", "baseline", b"baseline\n"),
                ("redeploy", "candidate", b"candidate\n"),
            ):
                with self.subTest(crash=requested):
                    crashed = False
                    def interrupt(point):
                        nonlocal crashed
                        if point.endswith(":published") and not crashed:
                            crashed = True
                            raise transaction.InjectedCrash(point)
                    with self.assertRaises(transaction.InjectedCrash):
                        tx.transition(target, operation=requested, after_boundary=interrupt)
                    self.assertFalse(service.active)
                    tx = transaction.Priority15Transaction.load(state, service=service)
                    tx.recover(target)
                    self.assertEqual((destination / "app.py").read_bytes(), expected_bytes)
                    deployed = (destination / "app.py").stat()
                    self.assertEqual((deployed.st_uid, deployed.st_gid),
                                     (authority_uid, authority_gid))
                    self.assertTrue(service.active)
                    for relative, value in private.items():
                        self.assertEqual((destination / relative).read_bytes(), value)

            status = tx.status()
            self.assertEqual(status["source"], "candidate")
            self.assertEqual(status["service"]["service"], "matrix-web.service")
            self.assertEqual(status["service"]["n_restarts_baseline"], 0)
            self.assertTrue(status["service"]["display_lock"]["locked"])
            report = tx.finalize()
            self.assertEqual(set(report), {"version", "boundary", "accepted_source_sha", "accepted_tree_sha", "release_manifest_sha256", "baseline_manifest_sha256", "status"})
            self.assertEqual(report["status"], "accepted")

            # A pathname replacement cannot redirect an admitted destination.
            displaced_destination = root / "displaced-destination"
            destination.rename(displaced_destination)
            destination.mkdir()
            (destination / "app.py").write_bytes(b"candidate\n")
            with self.assertRaisesRegex(ValueError, "destination identity"):
                transaction.Priority15Transaction.load(state, service=service)
            shutil.rmtree(destination)
            displaced_destination.rename(destination)

            # Replacing the state inode with byte-identical content is not authority.
            replacement = state.with_suffix(".replacement")
            replacement.write_bytes(state.read_bytes())
            replacement.chmod(0o600)
            os.replace(replacement, state)
            with self.assertRaisesRegex(ValueError, "state identity"):
                transaction.Priority15Transaction.load(state, service=service)

    def test_systemd_status_binds_cgroup_display_lock_and_warning_baseline(self):
        from deploy import priority_1_5_transaction as transaction

        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "matrix-display.lock"
            lock_path.write_bytes(b"")
            descriptor = os.open(lock_path, os.O_RDWR)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

            properties = types.SimpleNamespace(
                stdout=(
                    "Id=matrix-web.service\nActiveState=active\nSubState=running\n"
                    f"MainPID={os.getpid()}\nNRestarts=0\n"
                    "ControlGroup=/system.slice/matrix-web.service\n"
                )
            )

            class Response:
                status = 302
                def read(self):
                    return b""

            class Connection:
                def request(self, *_args):
                    return None
                def getresponse(self):
                    return Response()
                def close(self):
                    return None

            journal = types.SimpleNamespace(stdout=b"", returncode=0)
            service = transaction.SystemdService("matrix-web.service")
            try:
                with (
                    patch.object(transaction, "DISPLAY_LOCK", lock_path),
                    patch.object(service, "_run", return_value=properties),
                    patch.object(transaction.http.client, "HTTPConnection", return_value=Connection()),
                    patch.object(transaction.subprocess, "run", return_value=journal) as run,
                ):
                    evidence = service.status(warning_since=1)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            self.assertTrue(evidence["display_lock"]["locked"])
            self.assertEqual(evidence["main_pid"], os.getpid())
            self.assertEqual(evidence["warning_count"], 0)
            self.assertEqual(run.call_args.args[0][0], "/usr/bin/journalctl")
