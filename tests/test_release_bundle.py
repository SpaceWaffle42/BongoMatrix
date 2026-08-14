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
                        "daemon-reload-needed",
                        "daemon-reload",
                        "start",
                    ]
                    if initially_active
                    else ["is-active", "daemon-reload-needed", "daemon-reload"],
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
                            "daemon-reload-needed",
                            "start",
                        ]
                        + ([] if fail_at == "start" else ["health"]),
                    )
                else:
                    self.assertEqual(
                        service.calls, ["is-active", "daemon-reload-needed"]
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
