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
        destination = Path(_args[2])
        if destination.joinpath("app.py").read_bytes() == b"new source\n":
            raise ValueError("destination no longer matches recorded pre-backup state")
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

    def test_rollback_recovers_candidate_and_service_when_stop_transitions_inactive_then_raises(self):
        temporary, tx, events, service, config_bytes = self.make_transaction()
        self.addCleanup(temporary.cleanup)
        tx.prepare()
        destination = tx.paths.destination_root
        destination.joinpath("app.py").write_bytes(b"new source\n")
        migrated = json.loads(config_bytes)
        migrated.update(schema_version=2, revision=8)
        candidate_config = json.dumps(migrated).encode()
        destination.joinpath("data/config.json").write_bytes(candidate_config)

        original_stop = service.stop
        calls = 0

        def stop_then_raise():
            nonlocal calls
            calls += 1
            original_stop()
            if calls == 1:
                raise RuntimeError("rollback stop failed after transition")

        service.stop = stop_then_raise
        with self.assertRaisesRegex(RuntimeError, "rollback stop failed after transition"):
            tx.rollback()

        self.assertNotIn("api.deploy", events)
        self.assertEqual(destination.joinpath("app.py").read_bytes(), b"new source\n")
        self.assertEqual(destination.joinpath("data/config.json").read_bytes(), candidate_config)
        self.assertTrue(service.is_active())

    def test_deploy_recovers_source_config_and_service_when_stop_transitions_inactive_then_raises(self):
        temporary, tx, events, service, config_bytes = self.make_transaction()
        self.addCleanup(temporary.cleanup)
        tx.prepare()

        original_stop = service.stop
        calls = 0

        def stop_then_raise():
            nonlocal calls
            calls += 1
            original_stop()
            if calls == 1:
                raise RuntimeError("stop failed after transition")

        service.stop = stop_then_raise
        with self.assertRaisesRegex(RuntimeError, "stop failed after transition"):
            tx.deploy()

        self.assertNotIn("api.recover", events)
        self.assertEqual(tx.paths.destination_root.joinpath("app.py").read_bytes(), b"old source\n")
        self.assertEqual(tx.paths.destination_root.joinpath("data/config.json").read_bytes(), config_bytes)
        self.assertTrue(service.is_active())

    def test_deploy_recovers_when_post_stop_active_check_raises(self):
        temporary, tx, events, service, config_bytes = self.make_transaction()
        self.addCleanup(temporary.cleanup)
        tx.prepare()

        original_is_active = service.is_active
        calls = 0

        def fail_post_stop_check():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("post-stop status failure")
            return original_is_active()

        service.is_active = fail_post_stop_check
        with self.assertRaisesRegex(RuntimeError, "post-stop status failure"):
            tx.deploy()

        self.assertNotIn("api.recover", events)
        self.assertEqual(tx.paths.destination_root.joinpath("app.py").read_bytes(), b"old source\n")
        self.assertEqual(tx.paths.destination_root.joinpath("data/config.json").read_bytes(), config_bytes)
        self.assertTrue(service.is_active())

    def test_deploy_recovers_old_pair_when_release_fails_after_source_mutation(self):
        temporary, tx, events, service, config_bytes = self.make_transaction()
        self.addCleanup(temporary.cleanup)
        tx.prepare()
        api = tx.release_api
        self.assertIsInstance(api, FakeReleaseAPI)
        assert isinstance(api, FakeReleaseAPI)
        original_deploy = api.deploy_release

        def deploy_then_raise(*args, **kwargs):
            original_deploy(*args, **kwargs)
            raise RuntimeError("candidate install failed after mutation")

        api.deploy_release = deploy_then_raise
        with self.assertRaisesRegex(RuntimeError, "candidate install failed after mutation"):
            tx.deploy()

        self.assertIn("api.recover", events)
        self.assertEqual(tx.paths.destination_root.joinpath("app.py").read_bytes(), b"old source\n")
        self.assertEqual(tx.paths.destination_root.joinpath("data/config.json").read_bytes(), config_bytes)
        self.assertTrue(service.is_active())

    def test_real_release_bundle_rehearses_candidate_rollback_and_final_with_mutable_authorities(self):
        from deploy import priority_1_4_transaction as transaction
        from deploy import release_bundle

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        stage = root / "stage"
        release = stage / "release"
        destination = root / "destination"
        release.joinpath("deploy").mkdir(parents=True)
        destination.joinpath("deploy").mkdir(parents=True)
        destination.joinpath("data").mkdir()
        destination.joinpath("resources/media").mkdir(parents=True)

        tool_bytes = Path(release_bundle.__file__).read_bytes()
        new_sources = {
            "app.py": b"candidate source\n",
            "deploy/release_bundle.py": tool_bytes,
        }
        marker = {
            "schema": 1,
            "source_sha": SOURCE_SHA,
            "candidate_tracked_files": sorted(new_sources),
            "files": [
                {"path": name, "sha256": hashlib.sha256(content).hexdigest()}
                for name, content in sorted(new_sources.items())
            ],
        }
        new_sources[".matrix-release.json"] = (
            json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        for name, content in new_sources.items():
            path = release / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        stage.joinpath("release_bundle.py").write_bytes(tool_bytes)
        entries = [
            {
                "path": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "uid": os.geteuid(), "gid": os.getegid(), "mode": "0644",
            }
            for name, content in sorted(new_sources.items())
        ]
        stage.joinpath("release-spec.json").write_text(json.dumps({"version": 1, "files": entries}))

        old_sources = {
            ".matrix-release.json": b"prior marker\n",
            "app.py": b"prior source\n",
            "deploy/release_bundle.py": b"prior tool\n",
        }
        for name, content in old_sources.items():
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        destination.chmod(0o750)
        config_v1 = json.dumps({
            "schema_version": 1, "revision": 7, "custom_modules": [],
            "sentinel": {"mutable": "preserved"},
        }, sort_keys=True, separators=(",", ":")).encode()
        config_path = destination / "data/config.json"
        config_path.write_bytes(config_v1)
        config_path.chmod(0o640)
        paired_bytes = b'{"devices":[{"id":"paired-sentinel"}]}'
        media_bytes = b"GIF89a mutable media sentinel"
        destination.joinpath("data/paired-devices.json").write_bytes(paired_bytes)
        destination.joinpath("resources/media/user.gif").write_bytes(media_bytes)

        events = []

        class MigratingService(FakeService):
            def run_health_checks(self):
                super().run_health_checks()
                if destination.joinpath("app.py").read_bytes() == new_sources["app.py"]:
                    current = json.loads(config_path.read_bytes())
                    if current["schema_version"] == 1:
                        current.update(schema_version=2, revision=current["revision"] + 1)
                        config_path.write_text(json.dumps(current))

        service = MigratingService(events)
        paths = transaction.TransactionPaths(
            source_sha=SOURCE_SHA, stage=stage, destination_root=destination,
            source_bundle=root / "source-bundle", state_bundle=root / "state-bundle",
            service_name="matrix-web.service", health_url="http://127.0.0.1:5000/pair",
            health_status=200, health_timeout_seconds=30,
        )
        tx = transaction.Priority14Transaction(paths, release_api=release_bundle, service=service)

        tx.prepare()
        tx.deploy()
        tx.verify("candidate")
        self.assertEqual(destination.joinpath("app.py").read_bytes(), new_sources["app.py"])
        self.assertEqual(json.loads(config_path.read_bytes())["schema_version"], 2)

        tx.rollback()
        tx.verify("rollback")
        for name, content in old_sources.items():
            self.assertEqual(destination.joinpath(name).read_bytes(), content)
        self.assertEqual(config_path.read_bytes(), config_v1)

        tx.redeploy()
        tx.verify("final")
        for name, content in new_sources.items():
            self.assertEqual(destination.joinpath(name).read_bytes(), content)
        self.assertEqual(json.loads(config_path.read_bytes())["revision"], 8)
        self.assertEqual(destination.joinpath("data/paired-devices.json").read_bytes(), paired_bytes)
        self.assertEqual(destination.joinpath("resources/media/user.gif").read_bytes(), media_bytes)
        self.assertTrue(service.is_active())

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

    def test_physical_recovers_service_when_stop_transitions_inactive_then_raises(self):
        from tests.test_priority_1_4_physical import CaptureMatrix

        temporary, tx, _events, service, config_bytes = self.make_transaction(
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

        original_stop = service.stop

        def stop_then_raise():
            original_stop()
            raise RuntimeError("physical stop failed after transition")

        service.stop = stop_then_raise
        with self.assertRaisesRegex(RuntimeError, "physical stop failed after transition"):
            tx.physical("candidate", display_lock, 64, 32)
        self.assertTrue(service.is_active())

    def test_physical_lock_rejection_recovers_service_without_hardware_access(self):
        from tests.test_priority_1_4_physical import CaptureMatrix

        temporary, tx, _events, service, config_bytes = self.make_transaction(
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
        unsafe_lock = Path(temporary.name) / "matrix-display.lock"
        unsafe_lock.mkdir()

        with self.assertRaisesRegex(ValueError, "display lock is unsafe"):
            tx.physical("candidate", unsafe_lock, 64, 32)
        self.assertTrue(service.is_active())


if __name__ == "__main__":
    unittest.main()
