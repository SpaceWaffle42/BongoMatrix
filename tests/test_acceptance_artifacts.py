import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path


class AcceptanceArtifactTests(unittest.TestCase):
    def test_same_attempt_distinct_canonical_operations_are_fresh_and_operation_replay_fails(self):
        from acceptance_artifacts import AcceptanceArtifactTransaction

        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "acceptance-runtime"
            base = {
                "attempt_id": "a" * 32, "acceptance_operation_id": "1" * 64,
                "evidence_id": "b" * 64,
                "expected_source_sha": "c" * 40, "expected_version": "v",
                "mode": "deploy", "nonce": "d" * 64, "owner_revision": 1,
            }
            candidate = AcceptanceArtifactTransaction(
                runtime, {**base, "acceptance_operation_id": "1" * 64},
                owner_generation=1,
            )
            final = AcceptanceArtifactTransaction(
                runtime, {**base, "acceptance_operation_id": "2" * 64},
                owner_generation=2,
            )
            self.assertEqual(
                candidate.path,
                runtime / "transactions" / ("a" * 32) / ("1" * 64),
            )
            self.assertEqual(
                final.path,
                runtime / "transactions" / ("a" * 32) / ("2" * 64),
            )
            with self.assertRaises(FileExistsError):
                AcceptanceArtifactTransaction(
                    runtime, {**base, "acceptance_operation_id": "1" * 64},
                    owner_generation=3,
                )

    def test_exact_attempt_transaction_publishes_canonical_owner_only_frames_once(self):
        from acceptance_artifacts import AcceptanceArtifactTransaction

        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "acceptance-runtime"
            transaction = {
                "attempt_id": "a" * 32, "acceptance_operation_id": "1" * 64,
                "evidence_id": "b" * 64,
                "expected_source_sha": "c" * 40, "expected_version": "9.8.7-test",
                "mode": "deploy", "nonce": "d" * 64,
                "owner_revision": 731,
            }
            publisher = AcceptanceArtifactTransaction(runtime, transaction, owner_generation=4)
            draft, saved = b"d" * 6144, b"s" * 6144
            publisher.publish_draft(draft, frame_revision=10)
            publisher.publish_saved_rollback(saved, frame_revision=11)

            attempt = runtime / "transactions" / ("a" * 32) / ("1" * 64)
            metadata_raw = (attempt / "metadata.json").read_bytes()
            metadata = json.loads(metadata_raw)
            self.assertEqual(metadata_raw, (json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n").encode())
            self.assertEqual((attempt / "draft.rgb").read_bytes(), draft)
            self.assertEqual((attempt / "saved.rgb").read_bytes(), saved)
            self.assertEqual((attempt / "current.rgb").read_bytes(), saved)
            self.assertEqual(metadata["draft_sha256"], hashlib.sha256(draft).hexdigest())
            self.assertEqual(metadata["saved_sha256"], hashlib.sha256(saved).hexdigest())
            self.assertEqual(metadata["current_sha256"], metadata["saved_sha256"])
            self.assertLess(metadata["draft_revision"], metadata["current_revision"])
            for path in (runtime, runtime / "transactions", attempt):
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            for path in attempt.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            with self.assertRaises(FileExistsError):
                AcceptanceArtifactTransaction(runtime, transaction, owner_generation=5)

    def test_remote_verifier_rejects_wrong_and_noncanonical_acceptance_operation_identity(self):
        import argparse
        import importlib.util
        from acceptance_artifacts import AcceptanceArtifactTransaction

        repository = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "operation_identity_remote_verifier",
            repository / "scripts/verify_priority_1_3_remote.py",
        )
        verifier = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(verifier)
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "app"
            (app / "data").mkdir(parents=True)
            publisher = AcceptanceArtifactTransaction(
                app / "data/acceptance-runtime",
                {
                    "attempt_id": "a" * 32,
                    "acceptance_operation_id": "1" * 64,
                    "evidence_id": "b" * 64,
                    "expected_source_sha": "c" * 40,
                    "expected_version": "v", "mode": "deploy",
                    "nonce": "d" * 64, "owner_revision": 1,
                },
                owner_generation=1,
            )
            publisher.publish_draft(b"d" * 6144, frame_revision=10)
            publisher.publish_saved_rollback(
                b"s" * 6144, frame_revision=11, saved_revision=9,
            )
            arguments = argparse.Namespace(
                app_root=str(app), attempt_id="a" * 32,
                acceptance_operation_id="2" * 64,
                evidence_id="b" * 64, expected_sha="c" * 40,
                expected_version="v", mode="deploy", check="rollback-parity",
            )
            with self.assertRaisesRegex(ValueError, "authority does not match"):
                verifier._canonical_transaction(app, arguments)
            arguments.acceptance_operation_id = "A" * 64
            with self.assertRaisesRegex(ValueError, "acceptance operation identity"):
                verifier.run(arguments)

    def test_partial_or_symlink_transaction_fails_closed(self):
        from acceptance_artifacts import AcceptanceArtifactTransaction

        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "acceptance-runtime"
            runtime.mkdir(mode=0o700)
            (runtime / "transactions").symlink_to(Path(directory) / "elsewhere")
            with self.assertRaises((OSError, ValueError)):
                AcceptanceArtifactTransaction(runtime, {
                    "attempt_id": "a" * 32, "acceptance_operation_id": "1" * 64,
                "evidence_id": "b" * 64,
                    "expected_source_sha": "c" * 40, "expected_version": "v",
                    "mode": "deploy", "nonce": "d" * 64, "owner_revision": 1,
                }, owner_generation=1)


if __name__ == "__main__":
    unittest.main()
