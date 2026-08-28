import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import deploy.release_bundle as release_bundle
from deploy.release_bundle import (
    ReleaseManifestError,
    create_backup_manifest,
    validate_release_manifest,
)


class ReleaseBundleTests(unittest.TestCase):
    def test_production_export_matches_validator_for_systemd_units(self):
        for path in (
            "deploy/matrix-web.service",
            "deploy/matrix-web.SoCkEt",
            "deploy/matrix-web.TIMER",
        ):
            with self.subTest(path=path):
                with self.assertRaisesRegex(
                    ReleaseManifestError, "release path targets a systemd unit"
                ):
                    release_bundle._reject_forbidden_release_path(path)
                self.assertFalse(release_bundle._is_production_export_path(path))

        self.assertTrue(
            release_bundle._is_production_export_path("deploy/release_bundle.py")
        )

    def test_systemd_service_control_pins_environment_and_shared_deadline(self):
        clock = mock.Mock(side_effect=[100.0, 390.0, 399.0])
        completed = mock.Mock(returncode=0)

        with mock.patch.object(
            release_bundle.subprocess, "run", return_value=completed,
        ) as run:
            service = release_bundle._SystemdServiceControl(
                "matrix-web.service", deadline=400.0, monotonic=clock,
            )
            self.assertTrue(service.is_active())
            service.stop()
            service.daemon_reload()

        self.assertEqual(
            [entry.args[0] for entry in run.call_args_list],
            [
                ("/usr/bin/systemctl", "is-active", "--quiet", "matrix-web.service"),
                ("/usr/bin/systemctl", "stop", "matrix-web.service"),
                ("/usr/bin/systemctl", "daemon-reload"),
            ],
        )
        self.assertEqual(
            [entry.kwargs["timeout"] for entry in run.call_args_list],
            [15.0, 10.0, 1.0],
        )
        for entry in run.call_args_list:
            self.assertEqual(
                entry.kwargs["env"],
                {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            )
            self.assertIs(entry.kwargs["shell"], False)

    def test_release_manifest_requires_source_hash_and_intended_owner_mode_for_every_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release_root = root / "release"
            release_root.mkdir()
            sources = {
                "webapp.py": b"application bytes\n",
                "app/templates/dashboard.html": b"dashboard bytes\n",
            }
            intended = {
                "webapp.py": (0, 0, "0644"),
                "app/templates/dashboard.html": (0, 0, "0600"),
            }
            for relative_path, source_bytes in sources.items():
                path = release_root / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(source_bytes)

            manifest_path = root / "release-manifest.json"

            def entry(relative_path):
                uid, gid, mode = intended[relative_path]
                return {
                    "path": relative_path,
                    "sha256": hashlib.sha256(sources[relative_path]).hexdigest(),
                    "uid": uid,
                    "gid": gid,
                    "mode": mode,
                }

            complete_manifest = {
                "version": 1,
                "files": [entry(relative_path) for relative_path in sources],
            }
            manifest_path.write_text(
                json.dumps(complete_manifest), encoding="utf-8"
            )

            validated = validate_release_manifest(release_root, manifest_path)

            self.assertEqual(validated, complete_manifest)

            invalid_manifests = {}
            for required_field in ("sha256", "uid", "gid", "mode"):
                missing_field = json.loads(json.dumps(complete_manifest))
                del missing_field["files"][1][required_field]
                invalid_manifests[f"missing {required_field}"] = missing_field

            missing_file = json.loads(json.dumps(complete_manifest))
            missing_file["files"].pop()
            invalid_manifests["unlisted release file"] = missing_file

            extra_file = json.loads(json.dumps(complete_manifest))
            extra_file["files"].append(
                {
                    "path": "not-in-release.py",
                    "sha256": "0" * 64,
                    "uid": 0,
                    "gid": 0,
                    "mode": "0644",
                }
            )
            invalid_manifests["manifest path without source"] = extra_file

            duplicate_file = json.loads(json.dumps(complete_manifest))
            duplicate_file["files"].append(entry("webapp.py"))
            invalid_manifests["duplicate path"] = duplicate_file

            wrong_hash = json.loads(json.dumps(complete_manifest))
            wrong_hash["files"][0]["sha256"] = "0" * 64
            invalid_manifests["source hash mismatch"] = wrong_hash

            for ambiguous_path in (
                "/webapp.py",
                "./webapp.py",
                "app/../webapp.py",
                "app//templates/dashboard.html",
            ):
                ambiguous = json.loads(json.dumps(complete_manifest))
                ambiguous["files"][0]["path"] = ambiguous_path
                invalid_manifests[f"noncanonical path {ambiguous_path}"] = ambiguous

            for label, invalid_manifest in invalid_manifests.items():
                with self.subTest(label=label):
                    manifest_path.write_text(
                        json.dumps(invalid_manifest), encoding="utf-8"
                    )
                    with self.assertRaises(ReleaseManifestError):
                        validate_release_manifest(release_root, manifest_path)

    def test_backup_manifest_records_prior_metadata_only_for_present_before_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release_root = root / "release"
            destination_root = root / "destination"
            bundle = root / "bundle"
            release_root.mkdir()
            destination_root.mkdir()

            release_sources = {
                "webapp.py": b"new application bytes\n",
                "app/templates/dashboard.html": b"new dashboard bytes\n",
            }
            release_entries = []
            for relative_path, source_bytes in release_sources.items():
                source = release_root / relative_path
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(source_bytes)
                release_entries.append(
                    {
                        "path": relative_path,
                        "sha256": hashlib.sha256(source_bytes).hexdigest(),
                        "uid": 123,
                        "gid": 456,
                        "mode": "0600",
                    }
                )

            release_manifest_path = root / "release-manifest.json"
            release_manifest_path.write_text(
                json.dumps({"version": 1, "files": release_entries}),
                encoding="utf-8",
            )
            release_manifest = validate_release_manifest(
                release_root, release_manifest_path
            )

            prior_bytes = b"prior application bytes\n"
            present_destination = destination_root / "webapp.py"
            present_destination.write_bytes(prior_bytes)
            present_destination.chmod(0o640)
            prior_stat = present_destination.stat()

            backup_manifest_path = bundle / "backup-manifest.json"
            backup_manifest = create_backup_manifest(
                destination_root,
                bundle,
                release_manifest,
                backup_manifest_path,
            )

            expected_present = {
                "path": "webapp.py",
                "present_before": True,
                "sha256": hashlib.sha256(prior_bytes).hexdigest(),
                "uid": prior_stat.st_uid,
                "gid": prior_stat.st_gid,
                "mode": "0640",
            }
            expected_absent = {
                "path": "app/templates/dashboard.html",
                "present_before": False,
            }
            self.assertEqual(
                backup_manifest,
                {"version": 1, "files": [expected_present, expected_absent]},
            )
            self.assertEqual(
                json.loads(backup_manifest_path.read_text(encoding="utf-8")),
                backup_manifest,
            )
            self.assertEqual(
                (bundle / "archive/webapp.py").read_bytes(), prior_bytes
            )
            self.assertFalse(
                (bundle / "archive/app/templates/dashboard.html").exists()
            )
            self.assertEqual(set(expected_absent), {"path", "present_before"})

            unsafe_destination = destination_root / "app/templates/dashboard.html"
            unsafe_destination.parent.mkdir(parents=True)
            unsafe_destination.symlink_to(present_destination)
            with self.assertRaises(ReleaseManifestError):
                create_backup_manifest(
                    destination_root,
                    root / "unsafe-bundle",
                    release_manifest,
                    root / "unsafe-bundle/backup-manifest.json",
                )

            unsafe_destination.unlink()
            unsafe_destination.mkdir()
            with self.assertRaises(ReleaseManifestError):
                create_backup_manifest(
                    destination_root,
                    root / "nonregular-bundle",
                    release_manifest,
                    root / "nonregular-bundle/backup-manifest.json",
                )

            unsafe_release_manifest = json.loads(json.dumps(release_manifest))
            unsafe_release_manifest["files"][0]["path"] = "../webapp.py"
            with self.assertRaises(ReleaseManifestError):
                create_backup_manifest(
                    destination_root,
                    root / "unsafe-path-bundle",
                    unsafe_release_manifest,
                    root / "unsafe-path-bundle/backup-manifest.json",
                )

    def test_staged_tool_and_release_root_hashes_must_match_release_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release_root = root / "release"
            release_root.mkdir()
            sources = {
                "deploy/release_bundle.py": b"approved release tool bytes\n",
                "webapp.py": b"approved application bytes\n",
            }
            entries = []
            for relative_path, source_bytes in sources.items():
                source = release_root / relative_path
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(source_bytes)
                entries.append(
                    {
                        "path": relative_path,
                        "sha256": hashlib.sha256(source_bytes).hexdigest(),
                        "uid": 0,
                        "gid": 0,
                        "mode": "0644",
                    }
                )

            manifest = {"version": 1, "files": entries}
            manifest_path = root / "release-manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            staged_tool = root / "release_bundle.py"
            staged_tool.write_bytes(sources["deploy/release_bundle.py"])

            self.assertEqual(
                release_bundle.verify_staged_release(
                    staged_tool, release_root, manifest_path
                ),
                manifest,
            )

            staged_tool.write_bytes(b"substituted staged tool bytes\n")
            with self.assertRaises(ReleaseManifestError):
                release_bundle.verify_staged_release(
                    staged_tool, release_root, manifest_path
                )
            staged_tool.write_bytes(sources["deploy/release_bundle.py"])

            changed_source = release_root / "webapp.py"
            changed_source.write_bytes(b"changed application bytes\n")
            with self.assertRaises(ReleaseManifestError):
                release_bundle.verify_staged_release(
                    staged_tool, release_root, manifest_path
                )
            changed_source.write_bytes(sources["webapp.py"])

            changed_source.unlink()
            with self.assertRaises(ReleaseManifestError):
                release_bundle.verify_staged_release(
                    staged_tool, release_root, manifest_path
                )
            changed_source.write_bytes(sources["webapp.py"])

            extra_source = release_root / "unexpected.py"
            extra_source.write_bytes(b"unexpected bytes\n")
            with self.assertRaises(ReleaseManifestError):
                release_bundle.verify_staged_release(
                    staged_tool, release_root, manifest_path
                )
            extra_source.unlink()

            changed_source.unlink()
            changed_source.symlink_to(release_root / "deploy/release_bundle.py")
            with self.assertRaises(ReleaseManifestError):
                release_bundle.verify_staged_release(
                    staged_tool, release_root, manifest_path
                )
            changed_source.unlink()
            changed_source.write_bytes(sources["webapp.py"])

            nonregular_source = release_root / "nonregular"
            os.mkfifo(nonregular_source)
            with self.assertRaises(ReleaseManifestError):
                release_bundle.verify_staged_release(
                    staged_tool, release_root, manifest_path
                )
            nonregular_source.unlink()

            mismatched_manifest = json.loads(json.dumps(manifest))
            mismatched_manifest["files"][1]["sha256"] = "0" * 64
            manifest_path.write_text(
                json.dumps(mismatched_manifest), encoding="utf-8"
            )
            with self.assertRaises(ReleaseManifestError):
                release_bundle.verify_staged_release(
                    staged_tool, release_root, manifest_path
                )

    def test_preflight_rejects_tampered_archive_destination_or_forbidden_mutable_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def prepare(relative_paths=("deploy/release_bundle.py", "webapp.py")):
                case = root / f"case-{len(tuple(root.iterdir()))}"
                release_root = case / "release"
                destination_root = case / "destination"
                bundle = case / "bundle"
                release_root.mkdir(parents=True)
                destination_root.mkdir()
                sources = {}
                entries = []
                for relative_path in relative_paths:
                    source_bytes = f"approved {relative_path}\n".encode()
                    sources[relative_path] = source_bytes
                    source = release_root / relative_path
                    source.parent.mkdir(parents=True, exist_ok=True)
                    source.write_bytes(source_bytes)
                    entries.append({
                        "path": relative_path,
                        "sha256": hashlib.sha256(source_bytes).hexdigest(),
                        "uid": os.getuid(), "gid": os.getgid(), "mode": "0644",
                    })
                release_manifest_path = case / "release-manifest.json"
                release_manifest_path.write_text(
                    json.dumps({"version": 1, "files": entries}), encoding="utf-8"
                )
                prior_bytes = b"installed application bytes\n"
                if "webapp.py" in relative_paths:
                    destination = destination_root / "webapp.py"
                    destination.write_bytes(prior_bytes)
                    destination.chmod(0o640)
                validated_release = validate_release_manifest(
                    release_root, release_manifest_path
                )
                backup_manifest_path = bundle / "backup-manifest.json"
                backup_manifest = create_backup_manifest(
                    destination_root, bundle, validated_release, backup_manifest_path
                )
                staged_tool = case / "release_bundle.py"
                staged_tool.write_bytes(sources["deploy/release_bundle.py"])
                return {
                    "release_root": release_root,
                    "destination_root": destination_root,
                    "bundle": bundle,
                    "release_manifest_path": release_manifest_path,
                    "backup_manifest_path": backup_manifest_path,
                    "release_manifest": validated_release,
                    "backup_manifest": backup_manifest,
                    "staged_tool": staged_tool,
                }

            def preflight(case):
                return release_bundle.preflight_release(
                    case["staged_tool"], case["release_root"],
                    case["destination_root"], case["bundle"],
                    case["release_manifest_path"], case["backup_manifest_path"],
                )

            valid = prepare()
            before = {
                path.relative_to(root).as_posix(): (
                    path.read_bytes() if path.is_file() else None,
                    stat.S_IMODE(path.lstat().st_mode),
                )
                for path in root.rglob("*")
            }
            self.assertEqual(
                preflight(valid),
                (valid["release_manifest"], valid["backup_manifest"]),
            )
            after = {
                path.relative_to(root).as_posix(): (
                    path.read_bytes() if path.is_file() else None,
                    stat.S_IMODE(path.lstat().st_mode),
                )
                for path in root.rglob("*")
            }
            self.assertEqual(after, before)

            tampered_archive = prepare()
            (tampered_archive["bundle"] / "archive/webapp.py").write_bytes(
                b"tampered archive bytes\n"
            )
            with self.assertRaises(ReleaseManifestError):
                preflight(tampered_archive)

            changed_destination = prepare()
            (changed_destination["destination_root"] / "webapp.py").write_bytes(
                b"changed after backup\n"
            )
            with self.assertRaises(ReleaseManifestError):
                preflight(changed_destination)

            changed_mode = prepare()
            (changed_mode["destination_root"] / "webapp.py").chmod(0o600)
            with self.assertRaises(ReleaseManifestError):
                preflight(changed_mode)

            created_after_backup = prepare()
            new_destination = (
                created_after_backup["destination_root"]
                / "deploy/release_bundle.py"
            )
            new_destination.parent.mkdir()
            new_destination.write_bytes(b"appeared after backup\n")
            with self.assertRaises(ReleaseManifestError):
                preflight(created_after_backup)

            removed_after_backup = prepare()
            (removed_after_backup["destination_root"] / "webapp.py").unlink()
            with self.assertRaises(ReleaseManifestError):
                preflight(removed_after_backup)

            for label, mutate in (
                (
                    "extra top-level field",
                    lambda manifest: manifest.update({"unexpected": True}),
                ),
                ("wrong scope", lambda manifest: manifest["files"].pop()),
                (
                    "present record missing metadata",
                    lambda manifest: manifest["files"][1].pop("sha256"),
                ),
                (
                    "absent record has prior metadata",
                    lambda manifest: manifest["files"][0].update(
                        {"sha256": "0" * 64}
                    ),
                ),
            ):
                with self.subTest(backup_manifest=label):
                    malformed = prepare()
                    manifest = json.loads(
                        malformed["backup_manifest_path"].read_text(
                            encoding="utf-8"
                        )
                    )
                    mutate(manifest)
                    malformed["backup_manifest_path"].write_text(
                        json.dumps(manifest), encoding="utf-8"
                    )
                    with self.assertRaises(ReleaseManifestError):
                        preflight(malformed)

            absent_archive = prepare()
            absent_placeholder = (
                absent_archive["bundle"] / "archive/deploy/release_bundle.py"
            )
            absent_placeholder.parent.mkdir(parents=True, exist_ok=True)
            absent_placeholder.write_bytes(b"must not exist\n")
            with self.assertRaises(ReleaseManifestError):
                preflight(absent_archive)

            archive_symlink = prepare()
            archive_file = archive_symlink["bundle"] / "archive/webapp.py"
            outside = archive_symlink["bundle"] / "outside"
            outside.write_bytes(archive_file.read_bytes())
            archive_file.unlink()
            archive_file.symlink_to(outside)
            with self.assertRaises(ReleaseManifestError):
                preflight(archive_symlink)

            destination_symlink = prepare()
            destination_file = destination_symlink["destination_root"] / "webapp.py"
            outside_destination = destination_symlink["destination_root"] / "outside"
            outside_destination.write_bytes(destination_file.read_bytes())
            destination_file.unlink()
            destination_file.symlink_to(outside_destination)
            with self.assertRaises(ReleaseManifestError):
                preflight(destination_symlink)

            forbidden_paths = (
                "config.json",
                "data/runtime-state.json",
                "data/paired_devices.json",
                "resources/media/user.png",
                ".env",
                "provider-credentials.json",
                "secrets/provider-token",
                "venv/bin/python",
                "app/__pycache__/webapp.pyc",
                "deploy/matrix-web.service",
                "REVISION",
            )
            for forbidden_path in forbidden_paths:
                with self.subTest(forbidden_path=forbidden_path):
                    forbidden = prepare(("deploy/release_bundle.py", forbidden_path))
                    with self.assertRaises(ReleaseManifestError):
                        preflight(forbidden)

    def test_rollback_restores_present_files_and_deletes_only_absent_before_files(self):
        class FakeService:
            def __init__(self, active):
                self.active = active
                self.calls = []

            def is_active(self):
                self.calls.append("is-active")
                return self.active

            def stop(self):
                self.calls.append("stop")
                self.active = False

            def daemon_reload_needed(self):
                self.calls.append("daemon-reload-needed")
                return True

            def daemon_reload(self):
                self.calls.append("daemon-reload")

            def start(self):
                self.calls.append("start")
                self.active = True

        for initially_active in (True, False):
            with self.subTest(initially_active=initially_active), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                release_root = root / "release"
                destination_root = root / "destination"
                bundle = root / "bundle"
                release_root.mkdir()
                destination_root.mkdir()
                (destination_root / "app").mkdir()

                sources = {
                    "deploy/release_bundle.py": b"approved release tool bytes\n",
                    "app/new.py": b"new module bytes\n",
                    "webapp.py": b"new application bytes\n",
                }
                entries = []
                for relative_path, source_bytes in sources.items():
                    source = release_root / relative_path
                    source.parent.mkdir(parents=True, exist_ok=True)
                    source.write_bytes(source_bytes)
                    entries.append({
                        "path": relative_path,
                        "sha256": hashlib.sha256(source_bytes).hexdigest(),
                        "uid": os.getuid(),
                        "gid": os.getgid(),
                        "mode": "0600",
                    })

                release_manifest_path = root / "release-manifest.json"
                release_manifest_path.write_text(
                    json.dumps({"version": 1, "files": entries}), encoding="utf-8"
                )
                old_tool = b"installed release tool bytes\n"
                old_webapp = b"installed application bytes\n"
                installed = {
                    "deploy/release_bundle.py": (old_tool, 0o640),
                    "webapp.py": (old_webapp, 0o644),
                }
                for relative_path, (prior_bytes, mode) in installed.items():
                    destination = destination_root / relative_path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(prior_bytes)
                    destination.chmod(mode)
                unrelated = destination_root / "unrelated.txt"
                unrelated.write_bytes(b"outside release scope\n")
                unrelated.chmod(0o600)
                unrelated_before = (
                    unrelated.read_bytes(),
                    stat.S_IMODE(unrelated.stat().st_mode),
                )

                validated_release = validate_release_manifest(
                    release_root, release_manifest_path
                )
                backup_manifest_path = bundle / "backup-manifest.json"
                create_backup_manifest(
                    destination_root,
                    bundle,
                    validated_release,
                    backup_manifest_path,
                )
                staged_tool = root / "release_bundle.py"
                staged_tool.write_bytes(sources["deploy/release_bundle.py"])
                before = {
                    relative_path: (
                        (destination_root / relative_path).read_bytes(),
                        (destination_root / relative_path).stat().st_uid,
                        (destination_root / relative_path).stat().st_gid,
                        stat.S_IMODE((destination_root / relative_path).stat().st_mode),
                    )
                    for relative_path in installed
                }

                replacements = []
                fsync_count_at_failure = None

                def fail_after_second_replace(relative_path):
                    nonlocal fsync_count_at_failure
                    replacements.append(relative_path)
                    if len(replacements) == 2:
                        fsync_count_at_failure = fsync.call_count
                        raise RuntimeError("injected mid-deploy failure")

                service = FakeService(initially_active)
                with mock.patch.object(
                    release_bundle.os, "fsync", wraps=os.fsync
                ) as fsync, self.assertRaisesRegex(
                    RuntimeError, "injected mid-deploy failure"
                ):
                    release_bundle.deploy_release(
                        staged_tool,
                        release_root,
                        destination_root,
                        bundle,
                        release_manifest_path,
                        backup_manifest_path,
                        service=service,
                        after_replace=fail_after_second_replace,
                    )

                self.assertEqual(replacements, ["deploy/release_bundle.py", "app/new.py"])
                self.assertIsNotNone(fsync_count_at_failure)
                if fsync_count_at_failure is not None:
                    self.assertGreater(fsync.call_count, fsync_count_at_failure)
                for relative_path, expected in before.items():
                    restored = destination_root / relative_path
                    restored_stat = restored.stat()
                    self.assertEqual(
                        (
                            restored.read_bytes(),
                            restored_stat.st_uid,
                            restored_stat.st_gid,
                            stat.S_IMODE(restored_stat.st_mode),
                        ),
                        expected,
                    )
                self.assertFalse((destination_root / "app/new.py").exists())
                self.assertEqual(
                    (unrelated.read_bytes(), stat.S_IMODE(unrelated.stat().st_mode)),
                    unrelated_before,
                )
                self.assertEqual(list(destination_root.rglob(".release-bundle-*")), [])
                self.assertEqual(service.active, initially_active)
                self.assertEqual(
                    service.calls,
                    [
                        "is-active",
                        "stop",
                        "is-active",
                        "is-active",
                        "daemon-reload-needed",
                        "daemon-reload",
                        "is-active",
                        "start",
                    ]
                    if initially_active
                    else ["is-active", "is-active", "is-active", "daemon-reload-needed", "daemon-reload", "is-active"],
                )

    def test_failed_action_runs_recovery_start_and_health_commands(self):
        class FakeService:
            def __init__(self, destination, *, active, fail_at=None):
                self.destination = destination
                self.active = active
                self.fail_at = fail_at
                self.calls = []

            def is_active(self):
                self.calls.append("is-active")
                return self.active

            def stop(self):
                self.calls.append("stop")
                self.active = False

            def daemon_reload_needed(self):
                self.calls.append("daemon-reload-needed")
                return False

            def daemon_reload(self):
                self.calls.append("daemon-reload")

            def _assert_restored(self):
                if self.destination.read_bytes() != b"installed application bytes\n":
                    raise AssertionError("recovery ran before rollback verification")

            def start(self):
                self.calls.append("start")
                self._assert_restored()
                if self.fail_at == "start":
                    raise RuntimeError("injected start failure")
                self.active = True

            def run_health_checks(self):
                self.calls.append("health")
                self._assert_restored()
                if self.fail_at == "health":
                    raise RuntimeError("injected health failure")

            def recovery_commands(self):
                return (
                    "systemctl start matrix-preview.service",
                    "curl --fail http://127.0.0.1:5000/health",
                )

        for initially_active, fail_at in (
            (True, None),
            (False, None),
            (True, "start"),
            (True, "health"),
        ):
            with self.subTest(
                initially_active=initially_active, fail_at=fail_at
            ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                release_root = root / "release"
                destination_root = root / "destination"
                bundle = root / "bundle"
                release_root.mkdir()
                destination_root.mkdir()
                (destination_root / "deploy").mkdir()
                sources = {
                    "deploy/release_bundle.py": b"approved release tool bytes\n",
                    "webapp.py": b"approved application bytes\n",
                }
                entries = []
                for relative_path, source_bytes in sources.items():
                    source = release_root / relative_path
                    source.parent.mkdir(parents=True, exist_ok=True)
                    source.write_bytes(source_bytes)
                    entries.append(
                        {
                            "path": relative_path,
                            "sha256": hashlib.sha256(source_bytes).hexdigest(),
                            "uid": os.getuid(),
                            "gid": os.getgid(),
                            "mode": "0644",
                        }
                    )
                release_manifest_path = root / "release-manifest.json"
                release_manifest = {"version": 1, "files": entries}
                release_manifest_path.write_text(
                    json.dumps(release_manifest), encoding="utf-8"
                )
                installed = destination_root / "webapp.py"
                installed.write_bytes(b"installed application bytes\n")
                validated = validate_release_manifest(
                    release_root, release_manifest_path
                )
                backup_manifest_path = bundle / "backup-manifest.json"
                create_backup_manifest(
                    destination_root, bundle, validated, backup_manifest_path
                )
                staged_tool = root / "release_bundle.py"
                staged_tool.write_bytes(sources["deploy/release_bundle.py"])
                service = FakeService(
                    installed, active=initially_active, fail_at=fail_at
                )

                def fail_action(relative_path):
                    if relative_path == "webapp.py":
                        raise RuntimeError("injected deploy failure")

                if fail_at is None:
                    expected_error = RuntimeError
                    expected_message = "injected deploy failure"
                else:
                    expected_error = ReleaseManifestError
                    expected_message = "service recovery failed"
                with self.assertRaisesRegex(expected_error, expected_message) as raised:
                    release_bundle.deploy_release(
                        staged_tool,
                        release_root,
                        destination_root,
                        bundle,
                        release_manifest_path,
                        backup_manifest_path,
                        service=service,
                        after_replace=fail_action,
                    )

                self.assertEqual(
                    installed.read_bytes(), b"installed application bytes\n"
                )
                if initially_active:
                    self.assertEqual(
                        service.calls,
                        [
                            "is-active",
                            "stop",
                            "is-active",
                            "is-active",
                            "daemon-reload-needed",
                            "is-active",
                            "start",
                        ]
                        + ([] if fail_at == "start" else ["health"]),
                    )
                else:
                    self.assertEqual(
                        service.calls, ["is-active", "is-active", "is-active", "daemon-reload-needed", "is-active"]
                    )
                self.assertEqual(service.active, initially_active and fail_at != "start")
                if fail_at is not None:
                    message = str(raised.exception)
                    self.assertIn("systemctl start matrix-preview.service", message)
                    self.assertIn(
                        "curl --fail http://127.0.0.1:5000/health", message
                    )

    def test_dry_run_changes_nothing_and_prints_exact_manifest_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release_root = root / "release"
            destination_root = root / "destination"
            bundle = root / "bundle"
            release_root.mkdir()
            destination_root.mkdir()
            sources = {
                "deploy/release_bundle.py": b"approved release tool bytes\n",
                "webapp.py": b"approved application bytes\n",
            }
            entries = []
            for index, (relative_path, source_bytes) in enumerate(sources.items()):
                source = release_root / relative_path
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(source_bytes)
                entries.append(
                    {
                        "path": relative_path,
                        "sha256": hashlib.sha256(source_bytes).hexdigest(),
                        "uid": os.getuid(),
                        "gid": os.getgid(),
                        "mode": "0600" if index == 0 else "0640",
                    }
                )
            release_manifest_path = root / "release-manifest.json"
            release_manifest_path.write_text(
                json.dumps({"version": 1, "files": entries}), encoding="utf-8"
            )
            installed = destination_root / "webapp.py"
            prior_bytes = b"installed application bytes\n"
            installed.write_bytes(prior_bytes)
            installed.chmod(0o644)
            validated = validate_release_manifest(release_root, release_manifest_path)
            backup_manifest_path = bundle / "backup-manifest.json"
            backup_manifest = create_backup_manifest(
                destination_root, bundle, validated, backup_manifest_path
            )
            staged_tool = root / "release_bundle.py"
            staged_tool.write_bytes(sources["deploy/release_bundle.py"])
            release_spec_path = root / "release-spec.json"
            release_spec_path.write_text(json.dumps(validated), encoding="utf-8")
            (bundle / "release-manifest.json").write_bytes(
                release_bundle._canonical_json(validated)
            )
            (bundle / "release-spec.json").write_text(
                json.dumps(validated), encoding="utf-8"
            )
            (bundle / "staged-tool.sha256").write_text(
                hashlib.sha256(staged_tool.read_bytes()).hexdigest() + "\n",
                encoding="ascii",
            )

            def snapshot():
                return {
                    path.relative_to(root).as_posix(): (
                        path.read_bytes() if path.is_file() else None,
                        path.lstat().st_uid,
                        path.lstat().st_gid,
                        stat.S_IMODE(path.lstat().st_mode),
                        path.lstat().st_mtime_ns,
                    )
                    for path in root.rglob("*")
                }

            before = snapshot()
            output = io.StringIO()
            arguments = [
                "deploy",
                "--release-root", str(release_root),
                "--destination-root", str(destination_root),
                "--bundle", str(bundle),
                "--release-manifest", str(release_manifest_path),
                "--backup-manifest", str(backup_manifest_path),
                "--release-spec", str(release_spec_path),
                "--service", "matrix-preview.service",
                "--dry-run",
            ]
            with mock.patch.object(release_bundle, "__file__", str(staged_tool)), \
                    mock.patch.object(
                        release_bundle,
                        "_service_control",
                        side_effect=AssertionError,
                        create=True,
                    ), redirect_stdout(output):
                self.assertEqual(release_bundle.main(arguments), 0)

            prior = backup_manifest["files"][1]
            self.assertEqual(
                output.getvalue(),
                "".join(
                    (
                        f"PREFLIGHT path=deploy/release_bundle.py source_sha256={entries[0]['sha256']} present_before=false\n",
                        f"DEPLOY path=deploy/release_bundle.py sha256={entries[0]['sha256']} uid={os.getuid()} gid={os.getgid()} mode=0600\n",
                        "ROLLBACK path=deploy/release_bundle.py action=delete present_before=false\n",
                        f"PREFLIGHT path=webapp.py source_sha256={entries[1]['sha256']} present_before=true backup_sha256={prior['sha256']}\n",
                        f"DEPLOY path=webapp.py sha256={entries[1]['sha256']} uid={os.getuid()} gid={os.getgid()} mode=0640\n",
                        f"ROLLBACK path=webapp.py action=restore present_before=true sha256={prior['sha256']} uid={prior['uid']} gid={prior['gid']} mode={prior['mode']}\n",
                        "SERVICE name=matrix-preview.service action=stop-if-active\n",
                        "SERVICE name=matrix-preview.service action=start-and-health-if-initially-active\n",
                    )
                ),
            )
            self.assertEqual(snapshot(), before)

            parser = release_bundle.build_parser()
            help_output = parser.format_help()
            for command in ("backup", "preflight", "deploy", "rollback", "verify"):
                self.assertIn(command, help_output)
            for option in (
                "--release-root", "--destination-root", "--bundle",
                "--release-manifest", "--backup-manifest", "--service", "--dry-run",
            ):
                self.assertIn(option, help_output)
            self.assertNotIn("password", help_output.lower())
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args(["deploy", "--dry-run"])
                with self.assertRaises(SystemExit):
                    parser.parse_args(arguments + ["--unsupported"])

    def _release_case(self, root, *, present=True):
        release_root = root / "release"
        destination_root = root / "destination"
        release_root.mkdir(parents=True)
        destination_root.mkdir()
        sources = {
            "deploy/release_bundle.py": b"tool\n",
            "app/code.py": b"new code\n",
        }
        entries = []
        for relative_path, content in sources.items():
            path = release_root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            entries.append({
                "path": relative_path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "uid": os.geteuid(), "gid": os.getegid(), "mode": "0644",
            })
        if present:
            installed = destination_root / "app/code.py"
            installed.parent.mkdir(parents=True)
            installed.write_bytes(b"old code\n")
            installed.chmod(0o640)
        release_spec = root / "release-spec.json"
        release_spec.write_text(json.dumps({"version": 1, "files": entries}))
        staged_tool = root / "release_bundle.py"
        staged_tool.write_bytes(sources["deploy/release_bundle.py"])
        return {
            "release_root": release_root,
            "destination_root": destination_root,
            "release_spec": release_spec,
            "release_manifest": root / "release-manifest.json",
            "backup_manifest": root / "bundle/backup-manifest.json",
            "bundle": root / "bundle",
            "staged_tool": staged_tool,
            "entries": entries,
        }

    def test_backup_is_descriptor_confined_fresh_owner_only_and_publishes_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = self._release_case(root)
            outside = root / "outside"
            outside.mkdir()
            (root / "bundle-link").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ReleaseManifestError):
                release_bundle.backup_release(
                    case["staged_tool"], case["release_root"],
                    case["destination_root"], root / "bundle-link/sub",
                    case["release_manifest"], root / "bundle-link/sub/backup-manifest.json",
                    case["release_spec"], owner_uid=os.geteuid(), owner_gid=os.getegid(),
                )
            self.assertEqual(list(outside.iterdir()), [])

            result = release_bundle.backup_release(
                case["staged_tool"], case["release_root"],
                case["destination_root"], case["bundle"],
                case["release_manifest"], case["backup_manifest"],
                case["release_spec"], owner_uid=os.geteuid(), owner_gid=os.getegid(),
            )
            bundle_stat = case["bundle"].lstat()
            self.assertEqual(stat.S_IMODE(bundle_stat.st_mode), 0o700)
            self.assertEqual((bundle_stat.st_uid, bundle_stat.st_gid),
                             (os.geteuid(), os.getegid()))
            self.assertEqual(json.loads(case["release_manifest"].read_text()),
                             {"version": 1, "files": case["entries"]})
            self.assertEqual((case["bundle"] / "release-spec.json").read_bytes(),
                             case["release_spec"].read_bytes())
            self.assertEqual((case["bundle"] / "release-manifest.json").read_bytes(),
                             case["release_manifest"].read_bytes())
            self.assertEqual(
                (case["bundle"] / "staged-tool.sha256").read_text().strip(),
                hashlib.sha256(case["staged_tool"].read_bytes()).hexdigest(),
            )
            self.assertEqual(result["files"][1]["present_before"], True)
            with self.assertRaises(ReleaseManifestError):
                release_bundle.backup_release(
                    case["staged_tool"], case["release_root"],
                    case["destination_root"], case["bundle"],
                    case["release_manifest"], case["backup_manifest"],
                    case["release_spec"], owner_uid=os.geteuid(), owner_gid=os.getegid(),
                )

    def test_backup_snapshot_uses_one_pinned_destination_root_across_all_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "destination"
            destination.mkdir()
            original = root / "destination-original"
            replacement = root / "destination-replacement"
            replacement.mkdir()
            manifest_entries = []
            for name, old_bytes, new_bytes in (
                ("one.txt", b"original one\n", b"replacement one\n"),
                ("two.txt", b"original two\n", b"replacement two\n"),
            ):
                (destination / name).write_bytes(old_bytes)
                (replacement / name).write_bytes(new_bytes)
                manifest_entries.append({
                    "path": name, "sha256": hashlib.sha256(b"release\n").hexdigest(),
                    "uid": os.geteuid(), "gid": os.getegid(), "mode": "0644",
                })
            manifest = {"version": 1, "files": manifest_entries}
            bundle = root / "bundle"
            real_read = release_bundle._read_prior_destination
            reads = 0

            def swap_between_entries(authority, relative_path):
                nonlocal reads
                result = real_read(authority, relative_path)
                reads += 1
                if reads == 1:
                    destination.rename(original)
                    replacement.rename(destination)
                return result

            with mock.patch.object(
                release_bundle, "_read_prior_destination", side_effect=swap_between_entries
            ):
                result = create_backup_manifest(
                    destination, bundle, manifest, bundle / "backup-manifest.json"
                )

            self.assertEqual(
                [entry["sha256"] for entry in result["files"]],
                [hashlib.sha256(b"original one\n").hexdigest(),
                 hashlib.sha256(b"original two\n").hexdigest()],
            )
            self.assertEqual((bundle / "archive/one.txt").read_bytes(), b"original one\n")
            self.assertEqual((bundle / "archive/two.txt").read_bytes(), b"original two\n")

    def test_backup_parent_swap_cannot_redirect_destination_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = self._release_case(root)
            outside = root / "outside"
            (outside / "app").mkdir(parents=True)
            (outside / "app/code.py").write_bytes(b"outside secret\n")
            real_open = release_bundle.os.open
            swapped = False

            def swapping_open(path, flags, *args, **kwargs):
                nonlocal swapped
                if path == "app" and kwargs.get("dir_fd") is not None and not swapped:
                    swapped = True
                    original = case["destination_root"] / "app"
                    original.rename(case["destination_root"] / "app-old")
                    original.symlink_to(outside / "app", target_is_directory=True)
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(release_bundle.os, "open", side_effect=swapping_open), \
                    self.assertRaises(ReleaseManifestError):
                release_bundle.backup_release(
                    case["staged_tool"], case["release_root"],
                    case["destination_root"], case["bundle"],
                    case["release_manifest"], case["backup_manifest"],
                    case["release_spec"], owner_uid=os.geteuid(), owner_gid=os.getegid(),
                )
            self.assertFalse(case["bundle"].exists())

    def test_literal_plan_cli_prebackup_dry_run_deploy_rollback_verify_and_cleanup(self):
        class Service:
            def __init__(self):
                self.active = False
            def is_active(self): return self.active
            def stop(self): self.active = False
            def daemon_reload_needed(self): return False
            def daemon_reload(self): pass
            def start(self): self.active = True
            def run_health_checks(self): pass
            def recovery_commands(self): return ()

        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory) / "stage"
            stage.mkdir(mode=0o700)
            case = self._release_case(stage)
            production_destination = Path(directory) / "destination"
            case["destination_root"].replace(production_destination)
            case["destination_root"] = production_destination
            # Match the staged layout from plan lines 522-532.
            staged_tool = stage / "release_bundle.py"
            case["staged_tool"].replace(staged_tool)
            bundle = Path(directory) / "backups/release"
            bundle.parent.mkdir()
            release_manifest = stage / "release-manifest.json"
            backup_manifest = bundle / "backup-manifest.json"
            common = [
                "--release-root", str(case["release_root"]),
                "--destination-root", str(case["destination_root"]),
                "--bundle", str(bundle),
                "--release-manifest", str(release_manifest),
                "--backup-manifest", str(backup_manifest),
                "--service", "matrix-web.service",
            ]
            prebackup = ["preflight", *common,
                         "--release-spec", str(case["release_spec"]), "--dry-run"]
            service = Service()
            with mock.patch.object(release_bundle, "__file__", str(staged_tool)), \
                    mock.patch.object(release_bundle, "_service_control", return_value=service), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(release_bundle.main(prebackup), 0)
                self.assertFalse(bundle.exists())
                self.assertFalse(release_manifest.exists())
                self.assertEqual(release_bundle.main(
                    ["backup", *common, "--release-spec", str(case["release_spec"]) ]
                ), 0)
                self.assertEqual(release_bundle.main(
                    ["preflight", *common, "--release-spec", str(case["release_spec"]) ]
                ), 0)
                self.assertEqual(release_bundle.main(
                    ["deploy", *common, "--release-spec", str(case["release_spec"]) ]
                ), 0)
                self.assertEqual(release_bundle.main(
                    ["verify", *common, "--mode", "deploy"]
                ), 0)
                self.assertEqual(release_bundle.main(
                    ["rollback", *common, "--dry-run"]
                ), 0)
                self.assertEqual((case["destination_root"] / "app/code.py").read_bytes(),
                                 b"new code\n")
                self.assertEqual(release_bundle.main(["rollback", *common]), 0)
                self.assertEqual(release_bundle.main(
                    ["verify", *common, "--mode", "rollback"]
                ), 0)
                self.assertEqual(release_bundle.main(["cleanup", *common]), 0)
            self.assertFalse(stage.exists())
            self.assertTrue(bundle.is_dir())
            self.assertTrue((bundle / "release-spec.json").is_file())

    def test_cleanup_recovery_never_replaces_substitute_inserted_before_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = self._cleanup_ready_case(root)
            stage = case["staged_tool"].parent
            substitute_identity = None
            real_recovery = release_bundle._rename_noreplace

            def insert_substitute_before_restore(src, dst, *args, **kwargs):
                nonlocal substitute_identity
                if (
                    src == "stage"
                    and dst == stage.name
                    and kwargs.get("src_dir_fd") != kwargs.get("dst_dir_fd")
                    and not stage.exists()
                ):
                    stage.mkdir()
                    substitute_identity = stage.stat().st_ino
                return real_recovery(src, dst, *args, **kwargs)

            with mock.patch.object(
                release_bundle, "_remove_tree_contents_fd",
                side_effect=OSError("injected partial cleanup failure"),
            ), mock.patch.object(
                release_bundle, "_rename_noreplace",
                side_effect=insert_substitute_before_restore,
            ), self.assertRaisesRegex(ReleaseManifestError, "confined cleanup failed"):
                release_bundle.cleanup_stage(
                    case["staged_tool"], case["release_root"],
                    case["destination_root"], case["bundle"],
                    case["release_manifest"], case["backup_manifest"],
                )

            self.assertIsNotNone(substitute_identity)
            self.assertEqual(stage.stat().st_ino, substitute_identity)
            quarantines = list(root.glob(".release-cleanup-root-*"))
            self.assertEqual(len(quarantines), 1)
            self.assertTrue((quarantines[0] / "stage").is_dir())

    def test_cleanup_final_quarantine_removal_rejects_parent_entry_substitution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = self._cleanup_ready_case(root)
            moved_quarantine = root / "moved-cleanup-quarantine"
            substitute_identity = None
            real_fsync = release_bundle.os.fsync

            def substitute_empty_quarantine_before_parent_removal(fd):
                nonlocal substitute_identity
                candidates = list(root.glob(".release-cleanup-root-*"))
                if len(candidates) == 1 and not list(candidates[0].iterdir()):
                    descriptor = os.fstat(fd)
                    candidate = candidates[0]
                    metadata = candidate.stat()
                    if (descriptor.st_dev, descriptor.st_ino) == (
                        metadata.st_dev, metadata.st_ino
                    ):
                        candidate.rename(moved_quarantine)
                        candidate.mkdir(mode=0o700)
                        substitute_identity = candidate.stat().st_ino
                return real_fsync(fd)

            with mock.patch.object(
                release_bundle.os, "fsync",
                side_effect=substitute_empty_quarantine_before_parent_removal,
            ), self.assertRaisesRegex(ReleaseManifestError, "confined cleanup failed"):
                release_bundle.cleanup_stage(
                    case["staged_tool"], case["release_root"],
                    case["destination_root"], case["bundle"],
                    case["release_manifest"], case["backup_manifest"],
                )

            self.assertIsNotNone(substitute_identity)
            substitutes = list(root.glob(".release-cleanup-root-*"))
            self.assertEqual(len(substitutes), 1)
            self.assertEqual(substitutes[0].stat().st_ino, substitute_identity)
            self.assertTrue(moved_quarantine.is_dir())

    def test_cleanup_rejects_untrusted_parent_before_filesystem_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = self._cleanup_ready_case(root)
            stage = case["staged_tool"].parent
            root.chmod(0o777)
            mutations = []

            def record(name, operation):
                def wrapped(*args, **kwargs):
                    mutations.append(name)
                    return operation(*args, **kwargs)
                return wrapped

            with mock.patch.object(
                release_bundle.os, "rename", record("rename", release_bundle.os.rename)
            ), mock.patch.object(
                release_bundle.os, "unlink", record("unlink", release_bundle.os.unlink)
            ), mock.patch.object(
                release_bundle.os, "rmdir", record("rmdir", release_bundle.os.rmdir)
            ), self.assertRaisesRegex(ReleaseManifestError, "cleanup parent is unsafe"):
                release_bundle.cleanup_stage(
                    case["staged_tool"], case["release_root"],
                    case["destination_root"], case["bundle"],
                    case["release_manifest"], case["backup_manifest"],
                )

            self.assertEqual(mutations, [])
            self.assertTrue(stage.is_dir())

    def test_cleanup_private_quarantine_isolates_original_name_before_final_unlink_and_rmdir(self):
        for operation in ("unlink", "rmdir"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                case = self._cleanup_ready_case(root)
                stage = case["staged_tool"].parent
                victim = root / "victim"
                if operation == "unlink":
                    victim.write_bytes(b"replacement victim survives\n")
                else:
                    victim.mkdir()
                    (victim / "must-survive.txt").write_bytes(
                        b"replacement victim survives\n"
                    )
                real_operation = getattr(release_bundle.os, operation)
                attacked = False

                def substitute_original_stage_before_delete(path, *args, **kwargs):
                    nonlocal attacked
                    if not attacked and kwargs.get("dir_fd") is not None:
                        if stage.exists():
                            raise AssertionError("stage was not isolated before deletion")
                        victim.rename(stage)
                        attacked = True
                        raise OSError(f"injected {operation} failure")
                    return real_operation(path, *args, **kwargs)

                with mock.patch.object(
                    release_bundle.os, operation,
                    side_effect=substitute_original_stage_before_delete,
                ), self.assertRaises(ReleaseManifestError):
                    release_bundle.cleanup_stage(
                        case["staged_tool"], case["release_root"],
                        case["destination_root"], case["bundle"],
                        case["release_manifest"], case["backup_manifest"],
                    )

                self.assertTrue(attacked)
                if operation == "unlink":
                    self.assertEqual(
                        stage.read_bytes(), b"replacement victim survives\n"
                    )
                else:
                    self.assertEqual(
                        (stage / "must-survive.txt").read_bytes(),
                        b"replacement victim survives\n",
                    )
                quarantines = list(root.glob(".release-cleanup-root-*"))
                self.assertEqual(len(quarantines), 1)
                self.assertTrue((quarantines[0] / "stage").is_dir())
                quarantine_stat = quarantines[0].stat()
                self.assertEqual(
                    (quarantine_stat.st_uid, quarantine_stat.st_gid,
                     stat.S_IMODE(quarantine_stat.st_mode)),
                    (0, 0, 0o700),
                )

    def test_cleanup_rechecks_pinned_stage_leaf_before_recursive_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / "stage"
            stage.mkdir(mode=0o700)
            case = self._release_case(stage)
            destination = root / "destination"
            case["destination_root"].replace(destination)
            case["destination_root"] = destination
            bundle = root / "backups/release"
            bundle.parent.mkdir()
            case["bundle"] = bundle
            case["backup_manifest"] = bundle / "backup-manifest.json"
            release_bundle.backup_release(
                case["staged_tool"], case["release_root"], destination, bundle,
                case["release_manifest"], case["backup_manifest"], case["release_spec"],
                owner_uid=os.geteuid(), owner_gid=os.getegid(),
            )
            no_service = type("NoService", (), {
                "is_active": lambda self: False,
                "stop": lambda self: None,
                "daemon_reload_needed": lambda self: False,
                "daemon_reload": lambda self: None,
                "start": lambda self: None,
                "run_health_checks": lambda self: None,
            })()
            release_bundle.deploy_release(
                case["staged_tool"], case["release_root"], destination, bundle,
                case["release_manifest"], case["backup_manifest"], service=no_service,
            )
            victim = root / "victim"
            victim.mkdir()
            marker = victim / "must-survive.txt"
            marker.write_bytes(b"outside victim\n")
            original_stage = root / "stage-original"
            def swap_stage():
                stage.rename(original_stage)
                victim.rename(stage)

            with self.assertRaises(ReleaseManifestError):
                release_bundle.cleanup_stage(
                    case["staged_tool"], case["release_root"], destination, bundle,
                    case["release_manifest"], case["backup_manifest"],
                    before_remove=swap_stage,
                )

            self.assertEqual((stage / marker.name).read_bytes(), b"outside victim\n")
            self.assertTrue(original_stage.is_dir())

    def test_cleanup_quarantines_checked_leaf_before_unlink_and_preserves_substitute(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / "stage"
            stage.mkdir()
            target = stage / "checked.txt"
            target.write_bytes(b"checked inode\n")
            victim = root / "victim.txt"
            victim.write_bytes(b"victim must survive\n")
            checked_away = root / "checked-away.txt"
            stage_fd = release_bundle._open_absolute_directory(stage)
            identities = release_bundle._tree_identities_from_fd(stage_fd)
            real_rename = release_bundle.os.rename
            attacked = False

            def substitute_before_quarantine(src, dst, *args, **kwargs):
                nonlocal attacked
                if src == "checked.txt" and kwargs.get("src_dir_fd") == stage_fd and not attacked:
                    attacked = True
                    target.rename(checked_away)
                    victim.rename(target)
                return real_rename(src, dst, *args, **kwargs)

            try:
                with mock.patch.object(
                    release_bundle.os, "rename", side_effect=substitute_before_quarantine
                ), self.assertRaises(ReleaseManifestError):
                    release_bundle._remove_tree_contents_fd(stage_fd, identities)
            finally:
                os.close(stage_fd)

            survivors = [path for path in (victim, target) if path.exists()]
            self.assertEqual(len(survivors), 1)
            self.assertEqual(survivors[0].read_bytes(), b"victim must survive\n")
            self.assertEqual(checked_away.read_bytes(), b"checked inode\n")

    def test_cleanup_descendant_swap_does_not_delete_substituted_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / "stage"
            child = stage / "child"
            child.mkdir(parents=True)
            (child / "stage-file").write_bytes(b"stage\n")
            victim = root / "victim"
            victim.mkdir()
            (victim / "must-survive.txt").write_bytes(b"outside victim\n")
            original_child = stage / "child-original"
            stage_fd = release_bundle._open_absolute_directory(stage)
            try:
                identities = release_bundle._tree_identities_from_fd(stage_fd)
                child.rename(original_child)
                victim.rename(child)
                with self.assertRaises(ReleaseManifestError):
                    release_bundle._remove_tree_contents_fd(stage_fd, identities)
            finally:
                os.close(stage_fd)

            self.assertEqual(
                (child / "must-survive.txt").read_bytes(), b"outside victim\n"
            )
            self.assertEqual((original_child / "stage-file").read_bytes(), b"stage\n")

    def test_deploy_and_preflight_require_release_spec_before_side_effects(self):
        common = [
            "--release-root", "/must-not-open/release",
            "--destination-root", "/must-not-open/destination",
            "--bundle", "/must-not-open/bundle",
            "--release-manifest", "/must-not-open/release-manifest.json",
            "--backup-manifest", "/must-not-open/backup-manifest.json",
            "--service", "matrix-web.service",
        ]
        for command in ("preflight", "deploy"):
            with self.subTest(command=command), \
                    mock.patch.object(release_bundle, "_service_control") as service_control, \
                    mock.patch.object(release_bundle, "preflight_release") as preflight, \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                release_bundle.main([command, *common])
            self.assertEqual(raised.exception.code, 2)
            service_control.assert_not_called()
            preflight.assert_not_called()

    def _recover_service(self, initially_active=True, fail_once=None):
        class Service:
            def __init__(self):
                self.active = initially_active
                self.events = []
                self.fail_once = fail_once
            def _event(self, name):
                self.events.append(name)
                if self.fail_once == name:
                    self.fail_once = None
                    raise RuntimeError(f"interrupted {name}")
            def is_active(self): self._event("is_active"); return self.active
            def stop(self): self._event("stop"); self.active = False
            def daemon_reload_needed(self): return True
            def daemon_reload(self): self._event("daemon_reload")
            def start(self): self._event("start"); self.active = True
            def run_health_checks(self): self._event("readiness")
            def recovery_commands(self): return ()
        return Service()

    def _assert_case_baseline(self, case):
        self.assertFalse((case["destination_root"] / "deploy/release_bundle.py").exists())
        restored = case["destination_root"] / "app/code.py"
        self.assertEqual(restored.read_bytes(), b"old code\n")
        self.assertEqual(stat.S_IMODE(restored.stat().st_mode), 0o640)

    def test_recover_restores_verified_backup_from_every_partial_deploy_boundary(self):
        for boundary in range(3):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                case = self._backed_case(Path(directory))
                for entry in case["entries"][:boundary]:
                    target = case["destination_root"] / entry["path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes((case["release_root"] / entry["path"]).read_bytes())
                    target.chmod(int(entry["mode"], 8))
                service = self._recover_service()
                release_bundle.recover_release(
                    case["staged_tool"], case["release_root"], case["destination_root"],
                    case["bundle"], case["release_manifest"], case["backup_manifest"],
                    service=service, initially_active=True,
                )
                self._assert_case_baseline(case)
                self.assertTrue(service.active)
                self.assertIn("readiness", service.events)

    def test_recover_completes_verified_backup_restore_from_every_partial_rollback_boundary(self):
        for boundary in range(3):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                case = self._backed_case(Path(directory))
                no_service = self._recover_service(initially_active=False)
                release_bundle.deploy_release(
                    case["staged_tool"], case["release_root"], case["destination_root"],
                    case["bundle"], case["release_manifest"], case["backup_manifest"],
                    service=no_service,
                )
                backup = json.loads(case["backup_manifest"].read_text())["files"]
                for entry in backup[:boundary]:
                    target = case["destination_root"] / entry["path"]
                    if entry["present_before"]:
                        target.write_bytes((case["bundle"] / "archive" / entry["path"]).read_bytes())
                        target.chmod(int(entry["mode"], 8))
                    else:
                        target.unlink(missing_ok=True)
                service = self._recover_service()
                release_bundle.recover_release(
                    case["staged_tool"], case["release_root"], case["destination_root"],
                    case["bundle"], case["release_manifest"], case["backup_manifest"],
                    service=service, initially_active=True,
                )
                self._assert_case_baseline(case)
                self.assertTrue(service.active)

    def test_recover_worker_retries_every_restore_service_and_readiness_interruption_to_exact_baseline(self):
        boundaries = ("stop", "remove:0", "restore:1", "daemon_reload", "start", "readiness")
        for interrupted in boundaries:
            with self.subTest(interrupted=interrupted), tempfile.TemporaryDirectory() as directory:
                case = self._backed_case(Path(directory))
                for entry in case["entries"]:
                    target = case["destination_root"] / entry["path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes((case["release_root"] / entry["path"]).read_bytes())
                    target.chmod(int(entry["mode"], 8))
                service = self._recover_service()
                fired = False
                def interrupt(boundary):
                    nonlocal fired
                    if boundary == interrupted and not fired:
                        fired = True
                        raise RuntimeError(f"worker killed after {boundary}")
                with self.assertRaises(RuntimeError):
                    release_bundle.recover_release(
                        case["staged_tool"], case["release_root"], case["destination_root"],
                        case["bundle"], case["release_manifest"], case["backup_manifest"],
                        service=service, initially_active=True, after_boundary=interrupt,
                    )
                self.assertTrue(fired)
                release_bundle.recover_release(
                    case["staged_tool"], case["release_root"], case["destination_root"],
                    case["bundle"], case["release_manifest"], case["backup_manifest"],
                    service=service, initially_active=True,
                )
                self._assert_case_baseline(case)
                self.assertTrue(service.active)
                self.assertEqual(service.events[-1], "readiness")

    def test_parser_has_exact_command_argument_parity(self):
        parser = release_bundle.build_parser()
        help_text = parser.format_help()
        for command in ("backup", "preflight", "deploy", "rollback", "recover", "verify", "cleanup"):
            self.assertIn(command, help_text)
        common = [
            "--release-root", "/release", "--destination-root", "/destination",
            "--bundle", "/bundle", "--release-manifest", "/release.json",
            "--backup-manifest", "/backup.json", "--service", "matrix-web.service",
            "--initial-service-state", "active", "--health-url", "http://127.0.0.1:5000/",
            "--health-status", "302", "--health-timeout-seconds", "30",
        ]
        parsed = parser.parse_args(["recover", *common])
        self.assertEqual(parsed.command, "recover")
        self.assertEqual((parsed.health_status, parsed.health_timeout_seconds), (302, 30))
        for required in (
            "--release-root", "--destination-root", "--bundle", "--release-manifest",
            "--backup-manifest", "--service", "--initial-service-state",
        ):
            broken = common[:]
            at = broken.index(required)
            del broken[at:at + 2]
            with self.subTest(required=required), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(["recover", *broken])
        for forbidden in ("--dry-run", "--release-spec", "--mode"):
            with self.subTest(forbidden=forbidden), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(["recover", *common, forbidden, "deploy"])
        for readiness in ("--health-url", "--health-status", "--health-timeout-seconds"):
            inactive = common[:]
            inactive[inactive.index("active")] = "inactive"
            # Remove the other readiness arguments so each one is rejected independently.
            for other in ("--health-url", "--health-status", "--health-timeout-seconds"):
                if other != readiness:
                    at = inactive.index(other)
                    del inactive[at:at + 2]
            with self.subTest(inactive=readiness), self.assertRaises(ReleaseManifestError):
                release_bundle.main(["recover", *inactive])
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parser.parse_args(["recover", *common, "--service", "duplicate.service"])

        with mock.patch.object(release_bundle, "recover_release") as recover, \
                mock.patch.object(release_bundle, "_verify_deployed") as verify_deployed, \
                mock.patch.object(release_bundle, "_service_control") as service_control:
            service = service_control.return_value
            self.assertEqual(release_bundle.main(["recover", *common]), 0)
        recover.assert_called_once()
        self.assertIs(recover.call_args.kwargs["service"], service)
        self.assertTrue(recover.call_args.kwargs["initially_active"])
        verify_deployed.assert_not_called()

    def _cleanup_ready_case(self, root):
        stage = root / "stage"
        stage.mkdir(mode=0o700)
        case = self._release_case(stage)
        destination = root / "destination"
        case["destination_root"].replace(destination)
        case["destination_root"] = destination
        bundle = root / "backups/release"
        bundle.parent.mkdir()
        case["bundle"] = bundle
        case["backup_manifest"] = bundle / "backup-manifest.json"
        release_bundle.backup_release(
            case["staged_tool"], case["release_root"], destination, bundle,
            case["release_manifest"], case["backup_manifest"], case["release_spec"],
            owner_uid=os.geteuid(), owner_gid=os.getegid(),
        )
        no_service = type("NoService", (), {
            "is_active": lambda self: False, "stop": lambda self: None,
            "daemon_reload_needed": lambda self: False,
            "daemon_reload": lambda self: None, "start": lambda self: None,
            "run_health_checks": lambda self: None,
        })()
        release_bundle.deploy_release(
            case["staged_tool"], case["release_root"], destination, bundle,
            case["release_manifest"], case["backup_manifest"], service=no_service,
        )
        return case

    def _backed_case(self, root):
        case = self._release_case(root)
        (case["destination_root"] / "deploy").mkdir()
        release_bundle.backup_release(
            case["staged_tool"], case["release_root"], case["destination_root"],
            case["bundle"], case["release_manifest"], case["backup_manifest"],
            case["release_spec"], owner_uid=os.geteuid(), owner_gid=os.getegid(),
        )
        return case

    def test_deploy_pins_destination_root_identity_across_service_callbacks(self):
        class SwappingService:
            def __init__(self, destination_root, original_root):
                self.destination_root = destination_root
                self.original_root = original_root
                self.active = True

            def is_active(self):
                return self.active

            def stop(self):
                self.active = False
                self.destination_root.rename(self.original_root)
                self.destination_root.mkdir()
                for source in self.original_root.rglob("*"):
                    target = self.destination_root / source.relative_to(self.original_root)
                    if source.is_dir():
                        target.mkdir()
                    else:
                        target.write_bytes(source.read_bytes())
                        target.chmod(stat.S_IMODE(source.stat().st_mode))

            def daemon_reload_needed(self):
                return False

            def daemon_reload(self):
                pass

            def start(self):
                self.active = True

            def run_health_checks(self):
                pass

            def recovery_commands(self):
                return ()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = self._backed_case(root)
            original_root = root / "destination-original"
            original_before = {
                entry["path"]: (
                    (case["destination_root"] / entry["path"]).read_bytes()
                    if (case["destination_root"] / entry["path"]).exists() else None
                )
                for entry in case["entries"]
            }
            service = SwappingService(case["destination_root"], original_root)

            with self.assertRaisesRegex(ReleaseManifestError, "destination root"):
                release_bundle.deploy_release(
                    case["staged_tool"], case["release_root"],
                    case["destination_root"], case["bundle"],
                    case["release_manifest"], case["backup_manifest"], service=service,
                )

            for root_path in (original_root, case["destination_root"]):
                for relative_path, expected in original_before.items():
                    candidate = root_path / relative_path
                    self.assertEqual(candidate.read_bytes() if candidate.exists() else None, expected)
            self.assertTrue(service.active)

    def test_deploy_records_post_replace_publication_and_revalidates_after_stop(self):
        class Service:
            def __init__(self, destination, mutate=False):
                self.destination = destination
                self.mutate = mutate
                self.active = True
                self.calls = []
            def is_active(self): self.calls.append("is-active"); return self.active
            def stop(self):
                self.calls.append("stop"); self.active = False
                if self.mutate:
                    self.destination.write_bytes(b"intervening state\n")
            def daemon_reload_needed(self): return False
            def daemon_reload(self): pass
            def start(self): self.calls.append("start"); self.active = True
            def run_health_checks(self): self.calls.append("health")
            def recovery_commands(self): return ()

        with tempfile.TemporaryDirectory() as directory:
            case = self._backed_case(Path(directory))
            installed = case["destination_root"] / "app/code.py"
            service = Service(installed, mutate=True)
            with self.assertRaises(ReleaseManifestError):
                release_bundle.deploy_release(
                    case["staged_tool"], case["release_root"], case["destination_root"],
                    case["bundle"], case["release_manifest"], case["backup_manifest"],
                    service=service,
                )
            self.assertEqual(installed.read_bytes(), b"intervening state\n")
            self.assertTrue(service.active)

        with tempfile.TemporaryDirectory() as directory:
            case = self._backed_case(Path(directory))
            installed = case["destination_root"] / "app/code.py"
            service = Service(installed)
            real_replace = release_bundle.os.replace
            published = False
            def track_replace(*args, **kwargs):
                nonlocal published
                real_replace(*args, **kwargs)
                published = True
            real_fsync = release_bundle.os.fsync
            def fail_post_publish_fsync(fd):
                if published:
                    raise OSError("directory fsync failed after publication")
                return real_fsync(fd)
            with mock.patch.object(release_bundle.os, "replace", side_effect=track_replace), \
                    mock.patch.object(release_bundle.os, "fsync", side_effect=fail_post_publish_fsync), \
                    self.assertRaises(Exception):
                release_bundle.deploy_release(
                    case["staged_tool"], case["release_root"], case["destination_root"],
                    case["bundle"], case["release_manifest"], case["backup_manifest"],
                    service=service,
                )
            self.assertEqual(installed.read_bytes(), b"old code\n")
            self.assertTrue(service.active)

    def test_deploy_final_verification_runs_after_service_start(self):
        class MutatingService:
            def __init__(self, installed):
                self.installed = installed
                self.active = True
                self.starts = 0

            def is_active(self):
                return self.active

            def stop(self):
                self.active = False

            def daemon_reload_needed(self):
                return False

            def daemon_reload(self):
                pass

            def start(self):
                self.starts += 1
                self.active = True
                if self.starts == 1:
                    self.installed.write_bytes(b"mutated by service start\n")

            def run_health_checks(self):
                pass

            def recovery_commands(self):
                return ()

        with tempfile.TemporaryDirectory() as directory:
            case = self._backed_case(Path(directory))
            installed = case["destination_root"] / "app/code.py"
            service = MutatingService(installed)

            with self.assertRaisesRegex(ReleaseManifestError, "deployed release"):
                release_bundle.deploy_release(
                    case["staged_tool"], case["release_root"],
                    case["destination_root"], case["bundle"],
                    case["release_manifest"], case["backup_manifest"], service=service,
                )

            self.assertEqual(installed.read_bytes(), b"old code\n")
            self.assertEqual(service.starts, 2)
            self.assertTrue(service.active)

    def test_main_deploy_rolls_back_post_start_approval_authority_substitution(self):
        class MutatingService:
            def __init__(self, case):
                self.case = case
                self.active = True
                self.starts = 0

            def is_active(self): return self.active
            def stop(self): self.active = False
            def daemon_reload_needed(self): return False
            def daemon_reload(self): pass
            def run_health_checks(self): pass
            def recovery_commands(self): return ()

            def start(self):
                self.starts += 1
                self.active = True
                if self.starts != 1:
                    return
                evil = b"unapproved replacement\n"
                source = self.case["release_root"] / "app/code.py"
                source.write_bytes(evil)
                manifest = json.loads(self.case["release_manifest"].read_text())
                entry = next(item for item in manifest["files"] if item["path"] == "app/code.py")
                entry["sha256"] = hashlib.sha256(evil).hexdigest()
                self.case["release_manifest"].write_text(json.dumps(manifest))
                installed = self.case["destination_root"] / "app/code.py"
                installed.write_bytes(evil)
                installed.chmod(0o644)

        with tempfile.TemporaryDirectory() as directory:
            case = self._backed_case(Path(directory))
            service = MutatingService(case)
            common = [
                "--release-root", str(case["release_root"]),
                "--destination-root", str(case["destination_root"]),
                "--bundle", str(case["bundle"]),
                "--release-manifest", str(case["release_manifest"]),
                "--backup-manifest", str(case["backup_manifest"]),
                "--release-spec", str(case["release_spec"]),
                "--service", "matrix-web.service",
            ]
            with mock.patch.object(release_bundle, "__file__", str(case["staged_tool"])), \
                    mock.patch.object(release_bundle, "_service_control", return_value=service), \
                    self.assertRaises(ReleaseManifestError):
                release_bundle.main(["deploy", *common])

            self.assertEqual(
                (case["destination_root"] / "app/code.py").read_bytes(), b"old code\n"
            )
            self.assertEqual(service.starts, 2)
            self.assertTrue(service.active)

    def test_deploy_recovery_uses_immutable_preflight_backup_snapshot(self):
        class Service:
            def __init__(self, archive):
                self.archive = archive
                self.active = True
                self.health_calls = 0
            def is_active(self): return self.active
            def stop(self): self.active = False
            def daemon_reload_needed(self): return False
            def daemon_reload(self): pass
            def start(self):
                self.active = True
                self.archive.chmod(0o600)
                self.archive.write_bytes(b"EVIL archive substitution\n")
            def run_health_checks(self):
                self.health_calls += 1
                if self.health_calls == 1:
                    raise RuntimeError("health failed after archive substitution")
            def recovery_commands(self): return ()

        with tempfile.TemporaryDirectory() as directory:
            case = self._backed_case(Path(directory))
            installed = case["destination_root"] / "app/code.py"
            archive = case["bundle"] / "archive/app/code.py"
            service = Service(archive)
            published_contents = []
            real_replace = release_bundle._atomic_replace_confined

            def record_publication(destination, path, content, *args, **kwargs):
                published_contents.append((path, content))
                return real_replace(destination, path, content, *args, **kwargs)

            with mock.patch.object(
                release_bundle, "_atomic_replace_confined", side_effect=record_publication
            ), self.assertRaises(Exception):
                release_bundle.deploy_release(
                    case["staged_tool"], case["release_root"], case["destination_root"],
                    case["bundle"], case["release_manifest"], case["backup_manifest"],
                    service=service,
                )

            self.assertEqual(installed.read_bytes(), b"old code\n")
            self.assertNotIn(
                ("app/code.py", b"EVIL archive substitution\n"), published_contents
            )
            self.assertTrue(service.active)

    def test_deploy_stops_active_service_before_every_rollback_publication(self):
        class Service:
            def __init__(self):
                self.active = True
                self.health_calls = 0
            def is_active(self): return self.active
            def stop(self): self.active = False
            def daemon_reload_needed(self): return False
            def daemon_reload(self): pass
            def start(self): self.active = True
            def run_health_checks(self):
                self.health_calls += 1
                if self.health_calls == 1:
                    raise RuntimeError("health failed")
            def recovery_commands(self): return ()

        with tempfile.TemporaryDirectory() as directory:
            case = self._backed_case(Path(directory))
            service = Service()
            publication_states = []
            real_replace = release_bundle._atomic_replace_confined

            def record_service_state(*args, **kwargs):
                publication_states.append(service.active)
                return real_replace(*args, **kwargs)

            with mock.patch.object(
                release_bundle, "_atomic_replace_confined", side_effect=record_service_state
            ), self.assertRaisesRegex(RuntimeError, "health failed"):
                release_bundle.deploy_release(
                    case["staged_tool"], case["release_root"], case["destination_root"],
                    case["bundle"], case["release_manifest"], case["backup_manifest"],
                    service=service,
                )

            self.assertGreater(len(publication_states), len(case["entries"]))
            self.assertEqual(publication_states, [False] * len(publication_states))
            self.assertTrue(service.active)

    def test_initially_inactive_recovery_stops_service_activated_by_failing_daemon_reload(self):
        class Service:
            def __init__(self):
                self.active = False
                self.stops = 0
            def is_active(self): return self.active
            def stop(self): self.stops += 1; self.active = False
            def daemon_reload_needed(self): return True
            def daemon_reload(self):
                self.active = True
                raise RuntimeError("reload activated service then failed")
            def start(self): self.active = True
            def run_health_checks(self): raise AssertionError("inactive recovery ran health")
            def recovery_commands(self): return ("manual recovery",)

        with tempfile.TemporaryDirectory() as directory:
            case = self._backed_case(Path(directory))
            service = Service()
            with self.assertRaisesRegex(
                ReleaseManifestError, "reload activated service then failed"
            ) as raised:
                release_bundle.deploy_release(
                    case["staged_tool"], case["release_root"], case["destination_root"],
                    case["bundle"], case["release_manifest"], case["backup_manifest"],
                    service=service,
                )

            self.assertEqual(
                (case["destination_root"] / "app/code.py").read_bytes(), b"old code\n"
            )
            self.assertFalse(service.active)
            self.assertGreaterEqual(service.stops, 2)
            self.assertIn("daemon-reload", str(raised.exception))

    def test_deploy_post_verifies_and_rolls_back_if_start_or_health_fails(self):
        class Service:
            def __init__(self, fail): self.fail = fail; self.active = True
            def is_active(self): return self.active
            def stop(self): self.active = False
            def daemon_reload_needed(self): return False
            def daemon_reload(self): pass
            def start(self):
                if self.fail == "start": raise RuntimeError("start failed")
                self.active = True
            def run_health_checks(self):
                if self.fail == "health": raise RuntimeError("health failed")
            def recovery_commands(self): return ("recover-service",)

        for failure in ("tamper", "start", "health"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                case = self._backed_case(Path(directory))
                installed = case["destination_root"] / "app/code.py"
                service = Service(failure)
                def after_replace(path):
                    if failure == "tamper" and path == "app/code.py":
                        installed.write_bytes(b"post deploy tamper\n")
                with self.assertRaises(Exception):
                    release_bundle.deploy_release(
                        case["staged_tool"], case["release_root"], case["destination_root"],
                        case["bundle"], case["release_manifest"], case["backup_manifest"],
                        service=service, after_replace=after_replace,
                    )
                self.assertEqual(installed.read_bytes(), b"old code\n")

    def test_rollback_pins_destination_root_identity_across_service_callbacks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = self._backed_case(root)
            no_service = type("NoService", (), {
                "is_active": lambda self: False, "stop": lambda self: None,
                "daemon_reload_needed": lambda self: False,
                "daemon_reload": lambda self: None, "start": lambda self: None,
                "run_health_checks": lambda self: None,
            })()
            release_bundle.deploy_release(
                case["staged_tool"], case["release_root"], case["destination_root"],
                case["bundle"], case["release_manifest"], case["backup_manifest"],
                service=no_service,
            )
            original_root = root / "destination-original"
            deployed_before = {
                entry["path"]: (case["destination_root"] / entry["path"]).read_bytes()
                for entry in case["entries"]
            }

            class SwappingService:
                active = True
                def is_active(self): return self.active
                def stop(self):
                    self.active = False
                    case["destination_root"].rename(original_root)
                    case["destination_root"].mkdir()
                    for source in original_root.rglob("*"):
                        target = case["destination_root"] / source.relative_to(original_root)
                        if source.is_dir(): target.mkdir()
                        else:
                            target.write_bytes(source.read_bytes())
                            target.chmod(stat.S_IMODE(source.stat().st_mode))
                def daemon_reload_needed(self): return False
                def daemon_reload(self): pass
                def start(self): self.active = True
                def run_health_checks(self): pass
                def recovery_commands(self): return ()

            service = SwappingService()
            with self.assertRaisesRegex(ReleaseManifestError, "destination root"):
                release_bundle.rollback_release(
                    case["release_root"], case["destination_root"], case["bundle"],
                    case["release_manifest"], case["backup_manifest"], service=service,
                )
            for root_path in (original_root, case["destination_root"]):
                for relative_path, expected in deployed_before.items():
                    self.assertEqual((root_path / relative_path).read_bytes(), expected)
            self.assertTrue(service.active)

    def test_rollback_validates_exact_schema_and_archive_scope_before_service_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            case = self._backed_case(Path(directory))
            class Service:
                def __init__(self): self.calls = []
                def is_active(self): self.calls.append("is-active"); return True
                def stop(self): self.calls.append("stop")
                def daemon_reload_needed(self): return False
                def daemon_reload(self): pass
                def start(self): pass
                def run_health_checks(self): pass
            # Put destination into valid deployed state first.
            release_bundle.deploy_release(
                case["staged_tool"], case["release_root"], case["destination_root"],
                case["bundle"], case["release_manifest"], case["backup_manifest"],
                service=type("N", (), {"is_active":lambda s:False,
                    "stop":lambda s:None,"daemon_reload_needed":lambda s:False,
                    "daemon_reload":lambda s:None,"start":lambda s:None,
                    "run_health_checks":lambda s:None})(),
            )
            manifest = json.loads(case["backup_manifest"].read_text())
            manifest["files"][1]["uid"] = True
            case["backup_manifest"].chmod(0o600)
            case["backup_manifest"].write_text(json.dumps(manifest))
            service = Service()
            with self.assertRaises(ReleaseManifestError):
                release_bundle.rollback_release(
                    case["release_root"], case["destination_root"], case["bundle"],
                    case["release_manifest"], case["backup_manifest"], service=service,
                )
            self.assertEqual(service.calls, [])

    def test_rollback_recovery_uses_immutable_deployed_snapshot(self):
        class Service:
            def __init__(self, source):
                self.source = source
                self.active = True
                self.health_calls = 0
            def is_active(self): return self.active
            def stop(self): self.active = False
            def daemon_reload_needed(self): return False
            def daemon_reload(self): pass
            def start(self):
                self.active = True
                self.source.write_bytes(b"EVIL release substitution\n")
                self.source.chmod(0o600)
            def run_health_checks(self):
                self.health_calls += 1
                if self.health_calls == 1:
                    raise RuntimeError("rollback health failed")
            def recovery_commands(self): return ()

        with tempfile.TemporaryDirectory() as directory:
            case = self._backed_case(Path(directory))
            no_service = type("NoService", (), {
                "is_active": lambda self: False, "stop": lambda self: None,
                "daemon_reload_needed": lambda self: False,
                "daemon_reload": lambda self: None, "start": lambda self: None,
                "run_health_checks": lambda self: None,
            })()
            release_bundle.deploy_release(
                case["staged_tool"], case["release_root"], case["destination_root"],
                case["bundle"], case["release_manifest"], case["backup_manifest"],
                service=no_service,
            )
            installed = case["destination_root"] / "app/code.py"
            before_bytes = installed.read_bytes()
            before_stat = installed.stat()
            service = Service(case["release_root"] / "app/code.py")

            with self.assertRaises(Exception):
                release_bundle.rollback_release(
                    case["release_root"], case["destination_root"], case["bundle"],
                    case["release_manifest"], case["backup_manifest"], service=service,
                )

            after_stat = installed.stat()
            self.assertEqual(installed.read_bytes(), before_bytes)
            self.assertEqual(
                (after_stat.st_uid, after_stat.st_gid, stat.S_IMODE(after_stat.st_mode)),
                (before_stat.st_uid, before_stat.st_gid, stat.S_IMODE(before_stat.st_mode)),
            )
            self.assertTrue(service.active)

    def test_rollback_stops_active_service_before_every_rollback_of_rollback_publication(self):
        class Service:
            def __init__(self):
                self.active = True
                self.health_calls = 0
            def is_active(self): return self.active
            def stop(self): self.active = False
            def daemon_reload_needed(self): return False
            def daemon_reload(self): pass
            def start(self): self.active = True
            def run_health_checks(self):
                self.health_calls += 1
                if self.health_calls == 1:
                    raise RuntimeError("rollback health failed")
            def recovery_commands(self): return ()

        with tempfile.TemporaryDirectory() as directory:
            case = self._backed_case(Path(directory))
            no_service = type("NoService", (), {
                "is_active": lambda self: False, "stop": lambda self: None,
                "daemon_reload_needed": lambda self: False,
                "daemon_reload": lambda self: None, "start": lambda self: None,
                "run_health_checks": lambda self: None,
            })()
            release_bundle.deploy_release(
                case["staged_tool"], case["release_root"], case["destination_root"],
                case["bundle"], case["release_manifest"], case["backup_manifest"],
                service=no_service,
            )
            service = Service()
            publication_states = []
            real_replace = release_bundle._atomic_replace_confined

            def record_service_state(*args, **kwargs):
                publication_states.append(service.active)
                return real_replace(*args, **kwargs)

            with mock.patch.object(
                release_bundle, "_atomic_replace_confined", side_effect=record_service_state
            ), self.assertRaisesRegex(RuntimeError, "rollback health failed"):
                release_bundle.rollback_release(
                    case["release_root"], case["destination_root"], case["bundle"],
                    case["release_manifest"], case["backup_manifest"], service=service,
                )

            self.assertGreater(len(publication_states), 2)
            self.assertEqual(publication_states, [False] * len(publication_states))
            self.assertTrue(service.active)

    def test_explicit_rollback_failure_restores_deployed_state_and_service_diagnostics(self):
        class Service:
            def __init__(self, failure):
                self.failure = failure; self.active = True
            def is_active(self): return self.active
            def stop(self): self.active = False
            def daemon_reload_needed(self): return True
            def daemon_reload(self):
                if self.failure == "daemon": raise RuntimeError("daemon failed")
            def start(self):
                if self.failure == "start": raise RuntimeError("start failed")
                self.active = True
            def run_health_checks(self):
                if self.failure == "health": raise RuntimeError("health failed")
            def recovery_commands(self): return ("recover-service",)

        for failure in ("remove", "restore", "fsync", "daemon", "start", "health"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                case = self._backed_case(Path(directory))
                no_service = type("NoService", (), {
                    "is_active": lambda s: False, "stop": lambda s: None,
                    "daemon_reload_needed": lambda s: False,
                    "daemon_reload": lambda s: None, "start": lambda s: None,
                    "run_health_checks": lambda s: None,
                })()
                release_bundle.deploy_release(
                    case["staged_tool"], case["release_root"], case["destination_root"],
                    case["bundle"], case["release_manifest"], case["backup_manifest"],
                    service=no_service,
                )
                deployed = {
                    entry["path"]: (case["destination_root"] / entry["path"]).read_bytes()
                    for entry in case["entries"]
                }
                service = Service(failure)
                patches = []
                if failure == "remove":
                    original = release_bundle._remove_confined
                    count = 0
                    def fail_remove(*args, **kwargs):
                        nonlocal count
                        original(*args, **kwargs); count += 1
                        if count == 1: raise OSError("remove fsync failed")
                    patches.append(mock.patch.object(release_bundle, "_remove_confined",
                                                     side_effect=fail_remove))
                elif failure == "restore":
                    original = release_bundle._atomic_replace_confined
                    count = 0
                    def fail_restore(*args, **kwargs):
                        nonlocal count
                        original(*args, **kwargs); count += 1
                        if count == 1: raise OSError("restore fsync failed")
                    patches.append(mock.patch.object(release_bundle, "_atomic_replace_confined",
                                                     side_effect=fail_restore))
                elif failure == "fsync":
                    original = release_bundle.os.fsync
                    failed = False
                    def fail_once(fd):
                        nonlocal failed
                        if not failed:
                            failed = True; raise OSError("fsync failed")
                        return original(fd)
                    patches.append(mock.patch.object(release_bundle.os, "fsync",
                                                     side_effect=fail_once))
                for active_patch in patches: active_patch.start()
                try:
                    with self.assertRaises(Exception) as raised:
                        release_bundle.rollback_release(
                            case["release_root"], case["destination_root"], case["bundle"],
                            case["release_manifest"], case["backup_manifest"], service=service,
                        )
                finally:
                    for active_patch in reversed(patches): active_patch.stop()
                for relative_path, content in deployed.items():
                    self.assertEqual((case["destination_root"] / relative_path).read_bytes(),
                                     content)
                if failure in ("start", "health", "daemon"):
                    self.assertIn(failure, str(raised.exception))
                if failure != "start":
                    self.assertTrue(service.active)

    def test_verify_rejects_provenance_archive_scope_and_destination_metadata_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = self._backed_case(root)
            no_service = type("NoService", (), {
                "is_active": lambda s: False, "stop": lambda s: None,
                "daemon_reload_needed": lambda s: False,
                "daemon_reload": lambda s: None, "start": lambda s: None,
                "run_health_checks": lambda s: None,
            })()
            release_bundle.deploy_release(
                case["staged_tool"], case["release_root"], case["destination_root"],
                case["bundle"], case["release_manifest"], case["backup_manifest"],
                service=no_service,
            )
            release_bundle.verify_release(
                case["staged_tool"], case["release_root"], case["destination_root"],
                case["bundle"], case["release_manifest"], case["backup_manifest"],
                mode="deploy",
            )
            deployed = case["destination_root"] / "app/code.py"
            deployed.chmod(0o600)
            with self.assertRaises(ReleaseManifestError):
                release_bundle.verify_release(
                    case["staged_tool"], case["release_root"], case["destination_root"],
                    case["bundle"], case["release_manifest"], case["backup_manifest"],
                    mode="deploy",
                )
            deployed.chmod(0o644)
            (case["bundle"] / "archive/extra").write_bytes(b"extra")
            with self.assertRaises(ReleaseManifestError):
                release_bundle.verify_release(
                    case["staged_tool"], case["release_root"], case["destination_root"],
                    case["bundle"], case["release_manifest"], case["backup_manifest"],
                    mode="deploy",
                )
