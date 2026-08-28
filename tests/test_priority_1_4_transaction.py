import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE_SHA = "2" * 40


class FakeService:
    def __init__(self, events):
        self.events = events
        self.active = True

    def is_active(self):
        return self.active

    def stop(self):
        self.events.append("service.stop")
        self.active = False

    def start(self):
        self.events.append("service.start")
        self.active = True

    def run_health_checks(self):
        self.events.append("service.health")
        if not self.active:
            raise RuntimeError("inactive")

    def daemon_reload_needed(self):
        return False

    def daemon_reload(self):
        self.events.append("service.reload")

    def recovery_commands(self):
        return ()


class FakeReleaseAPI:
    def __init__(self, events):
        self.events = events

    def verify_staged_release(self, tool, release, spec):
        self.events.append("api.verify-stage")
        return {"version": 1, "files": []}

    def backup_release(self, tool, release, destination, bundle, manifest, backup, spec, **_kwargs):
        self.events.append("api.backup")
        Path(bundle).mkdir(mode=0o700)
        Path(manifest).write_text('{"files":[],"version":1}\n')
        Path(backup).write_text('{"files":[],"version":1}\n')
        Path(bundle, "release-spec.json").write_text('{"files":[],"version":1}\n')
        Path(bundle, "staged-tool.sha256").write_text(hashlib.sha256(Path(tool).read_bytes()).hexdigest() + "\n")
        return {"version": 1, "files": []}

    def preflight_release(self, *_args, **_kwargs):
        self.events.append("api.preflight")
        return {"version": 1, "files": []}, {"version": 1, "files": []}

    def deploy_release(self, _tool, release, destination, *_args, service, **_kwargs):
        self.events.append("api.deploy")
        self.assert_inactive(service)
        Path(destination, "app.py").write_bytes(Path(release, "app.py").read_bytes())

    def verify_release(self, *_args, mode, **_kwargs):
        self.events.append(f"api.verify-{mode}")

    def rollback_release(self, _release, destination, *_args, service, **_kwargs):
        self.events.append("api.rollback")
        self.assert_inactive(service)
        Path(destination, "app.py").write_bytes(b"old source\n")

    def recover_release(self, *_args, service, **_kwargs):
        self.events.append("api.recover")
        self.assert_inactive(service)
        Path(_args[2], "app.py").write_bytes(b"old source\n")

    @staticmethod
    def assert_inactive(service):
        if service.is_active():
            raise AssertionError("release mutation ran while service active")


class Priority14TransactionTests(unittest.TestCase):
    def make_transaction(self, *, physical_factory=None):
        from deploy import priority_1_4_transaction as transaction

        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        stage = root / "stage"
        release = stage / "release"
        destination = root / "destination"
        release.mkdir(parents=True)
        destination.joinpath("data").mkdir(parents=True)
        destination.joinpath("resources/media").mkdir(parents=True)
        stage.joinpath("release_bundle.py").write_bytes(b"frozen tool\n")
        stage.joinpath("release-spec.json").write_text('{"files":[],"version":1}\n')
        release.joinpath(".matrix-release.json").write_text(json.dumps({
            "schema": 1, "source_sha": SOURCE_SHA,
            "candidate_tracked_files": ["app.py"],
            "files": [{"path": "app.py", "sha256": hashlib.sha256(b"new source\n").hexdigest()}],
        }, sort_keys=True, separators=(",", ":")) + "\n")
        release.joinpath("app.py").write_bytes(b"new source\n")
        destination.joinpath("app.py").write_bytes(b"old source\n")
        config = {
            "schema_version": 1, "revision": 7,
            "custom_modules": [], "sentinel": {"unchanged": True},
        }
        config_path = destination / "data/config.json"
        config_bytes = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
        config_path.write_bytes(config_bytes)
        config_path.chmod(0o640)
        destination.joinpath("data/paired-devices.json").write_bytes(b'{"devices":[]}')
        destination.joinpath("resources/media/user.gif").write_bytes(b"GIF89a")
        events = []
        service = FakeService(events)
        api = FakeReleaseAPI(events)
        paths = transaction.TransactionPaths(
            source_sha=SOURCE_SHA, stage=stage, destination_root=destination,
            source_bundle=root / "source-bundle", state_bundle=root / "state-bundle",
            service_name="matrix-web.service", health_url="http://127.0.0.1:5000/pair",
            health_status=200, health_timeout_seconds=30,
        )
        tx = transaction.Priority14Transaction(
            paths, release_api=api, service=service,
            physical_factory=physical_factory,
        )
        return temporary, tx, events, service, config_bytes

    def test_isolated_cli_imports_only_the_frozen_release_and_exposes_exact_phases(self):
        repository = Path(__file__).parents[1]
        commands = {
            "prepare", "preflight", "deploy", "verify", "physical",
            "rollback", "redeploy",
        }
        for script in (
            repository / "deploy/priority_1_4_transaction.py",
            repository / "scripts/verify_priority_1_4_physical.py",
        ):
            completed = subprocess.run(
                [sys.executable, "-I", "-B", str(script), "--help"],
                cwd="/", capture_output=True, text=True, timeout=10,
                env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            if script.name == "priority_1_4_transaction.py":
                self.assertTrue(commands.issubset(set(completed.stdout.replace("{", "").replace("}", "").replace(",", " ").split())))
            else:
                self.assertIn("exercise", completed.stdout)
                self.assertIn("verify-report", completed.stdout)

    def test_rollback_restores_source_and_v1_config_before_service_restart(self):
        temporary, tx, events, service, config_bytes = self.make_transaction()
        self.addCleanup(temporary.cleanup)
        tx.prepare()
        destination = tx.paths.destination_root
        destination.joinpath("app.py").write_bytes(b"new source\n")
        migrated = json.loads(config_bytes)
        migrated.update(schema_version=2, revision=8)
        destination.joinpath("data/config.json").write_text(json.dumps(migrated))
        observed = []

        def boundary(name):
            observed.append((
                name, destination.joinpath("app.py").read_bytes(),
                destination.joinpath("data/config.json").read_bytes(), service.is_active(),
            ))

        tx.rollback(after_boundary=boundary)

        self.assertEqual(destination.joinpath("app.py").read_bytes(), b"old source\n")
        self.assertEqual(destination.joinpath("data/config.json").read_bytes(), config_bytes)
        self.assertEqual(
            events[-4:], ["service.stop", "api.rollback", "service.start", "service.health"],
        )
        restored = next(item for item in observed if item[0] == "config-restored")
        self.assertEqual(restored[1:3], (b"old source\n", config_bytes))
        self.assertFalse(restored[3])
        self.assertTrue(service.is_active())
        metadata = destination.joinpath("data/config.json").stat()
        self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o640)

    def test_physical_phase_owns_display_exclusively_and_always_recovers_service(self):
        from tests.test_priority_1_4_physical import CaptureMatrix

        temporary, tx, events, service, config_bytes = self.make_transaction(
            physical_factory=CaptureMatrix,
        )
        self.addCleanup(temporary.cleanup)
        tx.prepare()
        destination = tx.paths.destination_root
        destination.chmod(0o750)
        destination.joinpath("app.py").write_bytes(b"new source\n")
        migrated = json.loads(config_bytes)
        migrated.update(schema_version=2, revision=8)
        destination.joinpath("data/config.json").write_text(json.dumps(migrated))
        display_lock = Path(temporary.name) / "matrix-display.lock"
        display_lock.touch(mode=0o600)

        report = tx.physical("candidate", display_lock, 64, 32)

        self.assertEqual((report["width"], report["height"]), (64, 32))
        self.assertTrue(service.is_active())
        self.assertEqual(events[-3:], ["service.stop", "service.start", "service.health"])


if __name__ == "__main__":
    unittest.main()
