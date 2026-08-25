import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock


def complete_through_controller(publisher, draft, saved, *, generation):
    from device_preview import DevicePreviewOwner, DevicePreviewRequest
    from display_controller import DisplayController

    class Matrix:
        def __init__(self):
            self.frame = bytes([17]) * 6144
            self.revision = 9
            self.observer = None
            self.guard = None

        def latest_frame_rgb(self):
            return self.frame, self.revision

        def set_physical_swap_observer(self, observer):
            self.observer = observer

        def set_publication_guard(self, guard):
            self.guard = guard

        def set_brightness(self, _value):
            pass

        def physical_swap(self, frame):
            if self.guard is not None and not self.guard():
                return False
            self.frame = frame
            self.revision += 1
            if self.observer is not None:
                self.observer(frame, self.revision)
            return True

    now = [100.0]
    matrix = Matrix()
    store = Mock()
    store.get.return_value = {
        "display": {"active_start": "00:00", "active_end": "00:00",
                    "day_brightness": 70, "night_brightness": 5},
        "playlist": [], "custom_modules": [],
    }
    controller = DisplayController(matrix, store)
    controller.start = Mock()
    controller.device_preview_manager._clock = lambda: now[0]
    owner = DevicePreviewOwner("device-a", "a" * 32)
    foreign = DevicePreviewOwner("device-b", "b" * 32)
    controller.begin_acceptance_transaction(publisher, owner, generation=generation)
    request = DevicePreviewRequest(owner, generation, {"module": "clock_lava"}, 10)

    def renderer(render_matrix, _request, *, stop_event):
        render_matrix.physical_swap(draft)
        assert controller.mark_acceptance_foreign_rejection(foreign, generation=generation)
        assert controller.mark_acceptance_result(
            owner, generation=generation, result="heartbeat_accepted",
        )
        now[0] = 110.0
        assert controller.device_preview_manager.active_lease() is None
        assert stop_event.is_set()
        return True

    controller.submit_device_draft(request, renderer=renderer)
    assert controller._execute_pending_device_draft()

    def execute(name, config, duration):
        del config, duration
        assert name == "sleep"
        matrix.physical_swap(saved)
        controller._shutdown.set()
        return True

    controller._execute = execute
    controller._run_loop_unchecked()
    status = controller.acceptance_transaction_status(owner, generation=generation)
    assert status is not None
    controller.device_preview_manager.close()
    return status


class AcceptanceArtifactTests(unittest.TestCase):
    def test_constructor_argument_rejection_does_not_raise_from_deallocator(self):
        import gc
        from unittest.mock import patch

        from acceptance_artifacts import AcceptanceArtifactTransaction

        with tempfile.TemporaryDirectory() as directory, patch("sys.unraisablehook") as hook:
            with self.assertRaises(TypeError):
                AcceptanceArtifactTransaction(
                    Path(directory), {}, owner_generation=1, unexpected=True,  # type: ignore[call-arg]
                )
            gc.collect()

        hook.assert_not_called()

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
            status = complete_through_controller(
                publisher, draft, saved, generation=4,
            )
            self.assertIs(status["foreign_rejected"], True)
            self.assertIs(status["timeout_returned"], True)
            self.assertIs(status["sleep_observed"], True)
            self.assertIs(status["rollback_complete"], True)

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
            self.assertIs(metadata["sleep_observed"], True)
            self.assertLess(metadata["draft_revision"], metadata["current_revision"])
            for path in (runtime, runtime / "transactions", attempt):
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            for path in attempt.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            with self.assertRaises(FileExistsError):
                AcceptanceArtifactTransaction(runtime, transaction, owner_generation=5)

    def test_saved_rollback_rejects_missing_sleep_event_without_partial_artifacts(self):
        from acceptance_artifacts import AcceptanceArtifactTransaction

        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "acceptance-runtime"
            publisher = AcceptanceArtifactTransaction(runtime, {
                "attempt_id": "a" * 32, "acceptance_operation_id": "1" * 64,
                "evidence_id": "b" * 64,
                "expected_source_sha": "c" * 40, "expected_version": "v",
                "mode": "deploy", "nonce": "d" * 64, "owner_revision": 1,
            }, owner_generation=1)
            publisher.publish_draft(b"d" * 6144, frame_revision=10)

            with self.assertRaisesRegex(ValueError, "journey evidence"):
                publisher.publish_saved_rollback(
                    b"s" * 6144, frame_revision=11, saved_revision=9,
                )

            self.assertEqual(publisher.state, publisher.DRAFT_PUBLISHED)
            self.assertFalse((publisher.path / "saved.rgb").exists())
            self.assertFalse((publisher.path / "current.rgb").exists())
            self.assertFalse((publisher.path / "metadata.json").exists())

    def test_saved_rollback_rejects_omitted_journey_evidence(self):
        from acceptance_artifacts import AcceptanceArtifactTransaction

        with tempfile.TemporaryDirectory() as directory:
            publisher = AcceptanceArtifactTransaction(Path(directory), {
                "attempt_id": "a" * 32, "acceptance_operation_id": "1" * 64,
                "evidence_id": "b" * 64, "expected_source_sha": "c" * 40,
                "expected_version": "v", "mode": "deploy", "nonce": "d" * 64,
                "owner_revision": 1,
            }, owner_generation=1)
            publisher.publish_draft(b"d" * 6144, frame_revision=10)

            with self.assertRaisesRegex(ValueError, "journey evidence"):
                publisher.publish_saved_rollback(
                    b"s" * 6144, frame_revision=11, saved_revision=9,
                )

    def test_direct_artifact_caller_cannot_mint_journey_evidence(self):
        from acceptance_artifacts import AcceptanceArtifactTransaction
        from display_controller import DisplayController

        class IdleMatrix:
            def latest_frame_rgb(self):
                return b"s" * 6144, 9

            def set_physical_swap_observer(self, _observer):
                pass

            def set_publication_guard(self, _guard):
                pass

        with tempfile.TemporaryDirectory() as directory:
            publisher = AcceptanceArtifactTransaction(Path(directory), {
                "attempt_id": "a" * 32, "acceptance_operation_id": "1" * 64,
                "evidence_id": "b" * 64, "expected_source_sha": "c" * 40,
                "expected_version": "v", "mode": "deploy", "nonce": "d" * 64,
                "owner_revision": 1,
            }, owner_generation=1)
            idle_controller = DisplayController(IdleMatrix(), Mock())
            idle_controller.start = Mock()

            self.assertFalse(hasattr(publisher, "bind_event_authority"))
            with self.assertRaisesRegex(ValueError, "event authority is invalid"):
                publisher._bind_display_controller(object(), object())
            with self.assertRaisesRegex(RuntimeError, "controller lifecycle is not active"):
                publisher._bind_display_controller(idle_controller, object())
            self.assertIsNone(idle_controller._acceptance_transaction)

            publisher.publish_draft(b"d" * 6144, frame_revision=10)
            with self.assertRaisesRegex(ValueError, "journey evidence"):
                publisher.publish_saved_rollback(
                    b"s" * 6144, frame_revision=11, saved_revision=9,
                )
            self.assertFalse((publisher.path / "metadata.json").exists())
            idle_controller.device_preview_manager.close()

    def test_acceptance_publisher_subclass_cannot_capture_controller_lifecycle_token(self):
        from acceptance_artifacts import AcceptanceArtifactTransaction
        from display_controller import DisplayController

        class Matrix:
            def latest_frame_rgb(self):
                return b"s" * 6144, 9

            def set_physical_swap_observer(self, _observer):
                pass

            def set_publication_guard(self, _guard):
                pass

        class CapturingPublisher(AcceptanceArtifactTransaction):
            captured_token = None

            def _bind_display_controller(self, authority, lifecycle_token):
                self.captured_token = lifecycle_token
                return super()._bind_display_controller(authority, lifecycle_token)

        with tempfile.TemporaryDirectory() as directory:
            publisher = CapturingPublisher(Path(directory), {
                "attempt_id": "a" * 32, "acceptance_operation_id": "1" * 64,
                "evidence_id": "b" * 64, "expected_source_sha": "c" * 40,
                "expected_version": "v", "mode": "deploy", "nonce": "d" * 64,
                "owner_revision": 1,
            }, owner_generation=1)
            controller = DisplayController(Matrix(), Mock())
            controller.start = Mock()

            with self.assertRaisesRegex(TypeError, "publisher type is invalid"):
                controller.begin_acceptance_transaction(
                    publisher, object(), generation=1,
                )

            self.assertIsNone(publisher.captured_token)
            self.assertIsNone(controller._acceptance_transaction)
            self.assertFalse((publisher.path / "metadata.json").exists())
            controller.device_preview_manager.close()

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
            complete_through_controller(
                publisher, b"d" * 6144, b"s" * 6144, generation=1,
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

            metadata_path = publisher.path / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["sleep_observed"] = False
            metadata_path.write_text(
                json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            arguments.acceptance_operation_id = "1" * 64
            with self.assertRaisesRegex(ValueError, "authority does not match"):
                verifier._canonical_transaction(app, arguments)

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
