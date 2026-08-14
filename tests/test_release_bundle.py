import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from deploy.release_bundle import ReleaseManifestError, validate_release_manifest


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
