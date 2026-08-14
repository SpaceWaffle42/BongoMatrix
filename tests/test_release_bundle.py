import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

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
