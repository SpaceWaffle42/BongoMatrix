import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import cast
import unittest

from deploy import guarded_priority_1_3_release as guarded


class Slice2ActiveClaimAndRegistryTests(unittest.TestCase):
    def _arguments(self, root: Path, **changes: object) -> argparse.Namespace:
        for directory in (root / "stage", root / "bundle", root / "destination", root / "evidence"):
            directory.mkdir(parents=True, exist_ok=True)
        registry_root = root / "transactions"
        registry_root.mkdir(exist_ok=True)
        os.chmod(registry_root, 0o700)
        values: dict[str, object] = {
            "boundary": "b1", "resume": False, "ssh_target": "fixture@host",
            "ssh_identity_file": str(root / "identity"),
            "ssh_known_hosts_file": str(root / "known-hosts"),
            "stage": str(root / "stage"), "bundle": str(root / "bundle"),
            "destination_root": str(root / "destination"), "destination_identity": "matrix-production",
            "service": "matrix-web.service", "initial_service_state": "active",
            "health_url": "http://127.0.0.1:5000/", "health_status": 302,
            "health_timeout_seconds": 30, "attempt_budget_seconds": 6600,
            "commit_reserve_seconds": 60, "watchdog_seconds": 7200,
            "expected_sha": "a" * 40, "expected_version": "0.3.58-preview",
            "previous_sha": "b" * 40, "previous_version": "0.3.57-preview",
            "session_cookie_file": str(root / "cookie"), "evidence_dir": str(root / "evidence"),
            "gitlab_project": "2", "gitlab_mr": "73", "gitlab_token_file": str(root / "token"),
            "chromium": None, "transaction_version": 1,
            "transaction_registry_root": str(registry_root),
            "release_stamp": "2026-08-23T02:51:53Z",
            "release_manifest_digest": "c" * 64, "backup_manifest_digest": "d" * 64,
            "release_spec_digest": "e" * 64,
        }
        values.update(changes)
        return argparse.Namespace(**values)

    def test_claims_validated_canonical_authority_after_inspect_before_first_remote_mutation(self):
        root = Path(tempfile.mkdtemp(prefix="slice2-active-claim-order-"))
        try:
            arguments = self._arguments(root)

            class Executor:
                def __init__(self):
                    self.events: list[tuple[str, object]] = []
                    self.claim_request: dict[str, object] | None = None
                    self.claim_result: guarded.ControlResult | None = None

                def control_remote(self, kind, request, *, operation_timeout):
                    del operation_timeout
                    immutable = copy.deepcopy(request)
                    self.events.append((kind, immutable))
                    if kind.startswith("remote-transaction-"):
                        result = guarded.dispatch_privileged(kind, immutable)
                        if kind == "remote-transaction-claim":
                            self.claim_request = immutable
                            self.claim_result = result
                        return result
                    return guarded.ControlResult(2, "")

                def run_remote(
                    self, attempt_id, operation_id, argv, *, registry=None, run_as, operation_timeout,
                ):
                    del argv, run_as, operation_timeout
                    self.events.append(("remote-start", {
                        "attempt_id": attempt_id, "operation_id": operation_id, "registry": registry,
                    }))
                    if operation_id == "watchdog-arm":
                        operation = __import__("hashlib").sha256(
                            f"{attempt_id}\0{operation_id}".encode("utf-8")
                        ).hexdigest()[:32]
                        unit = f"matrix-p13-op-{attempt_id}-{operation}.service"
                        return guarded.OperationResult(0, unit, f"/system.slice/{unit}")
                    return guarded.OperationResult(2, "", "")

            executor = Executor()
            result = guarded.run_release(
                arguments, executor=executor, acceptance_runner=lambda *_: 2,
                evidence_store=object(), clock=lambda: 100.0, wall_clock=lambda: 1000.0,
            )

            self.assertEqual(result, 2)
            self.assertEqual([event[0] for event in executor.events[:5]], [
                "remote-transaction-create-or-query", "remote-transaction-inspect",
                "remote-transaction-claim", "remote-transaction-transition", "remote-start",
            ])
            self.assertEqual(executor.events[3][1]["from_state"], "READY")
            self.assertEqual(executor.events[3][1]["to_state"], "GUARD_ARMING")
            self.assertEqual(sum(kind == "remote-transaction-claim" for kind, _ in executor.events), 1)
            self.assertIsNotNone(executor.claim_request)
            self.assertIsNotNone(executor.claim_result)
            claim_request = executor.claim_request
            claim_result = executor.claim_result
            assert claim_request is not None and claim_result is not None
            claimed = guarded.validate_transaction_record(
                copy.deepcopy(claim_result.terminal), str(claim_request["release_key"]),
            )
            self.assertEqual(claim_result.digest, claimed["record_digest"])
            self.assertEqual(claimed["claim"], {
                "claimant_id": claim_request["claimant_id"],
                "request_digest": claim_request["request_digest"],
                "claimed_epoch": claim_request["claimed_epoch"],
            })
            self.assertEqual(claimed["transaction_id"], claim_request["transaction_id"])
            self.assertLessEqual(claimed["created_epoch"], claim_request["claimed_epoch"])
            self.assertLessEqual(claim_request["claimed_epoch"], claimed["original_deadline_epoch"])
            first_attempt = claimed["attempts"][0]
            executor_requests = [request for kind, request in executor.events
                                 if kind in {"remote-start", "remote-recover", "remote-commit"}]
            self.assertEqual([request["operation_id"] for request in executor_requests[:2]],
                             ["watchdog-arm", "deploy"])
            self.assertEqual(executor.events[-1][0], "remote-recover")
            self.assertTrue(all(request["attempt_id"] == first_attempt["attempt_id"]
                                for request in executor_requests))
            self.assertTrue(all(request["registry"] == first_attempt["registry"]
                                for request in executor_requests))
        finally:
            shutil.rmtree(root, ignore_errors=True)


    def test_shipped_transaction_claim_rejects_exact_original_deadline_before_mutation(self):
        root = Path(tempfile.mkdtemp(prefix="slice2-shipped-claim-deadline-"))
        try:
            arguments = self._arguments(root, watchdog_seconds=60)
            class Executor:
                def control_remote(self, kind, request, *, operation_timeout):
                    del operation_timeout
                    return guarded.dispatch_privileged(kind, copy.deepcopy(request))
            identity, prepared = guarded._prepare_active_transaction(
                arguments, Executor(), created_epoch=1000,
            )
            deadline = prepared["original_deadline_epoch"]
            record_path = Path(arguments.transaction_registry_root) / f"{identity.release_key}.json"
            before = record_path.read_bytes()
            request = {
                "schema": 1, "registry_root": arguments.transaction_registry_root,
                "release_key": identity.release_key,
                "transaction_id": prepared["transaction_id"],
                "claimant_id": "f" * 32, "request_digest": "e" * 64,
                "claimed_epoch": deadline,
            }
            with self.assertRaisesRegex(ValueError, "deadline|lease|epoch"):
                guarded.dispatch_privileged("remote-transaction-claim", request)
            self.assertEqual(record_path.read_bytes(), before)
            registry = guarded.DurableTransactionRegistry(
                arguments.transaction_registry_root, identity.release_key,
            )
            try:
                self.assertIsNone(registry.inspect(prepared["transaction_id"])["claim"])
            finally:
                registry.close()
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_active_claim_uses_strict_immutable_integer_lease_before_durable_claim(self):
        class Executor:
            def __init__(self):
                self.claim_calls = 0

            def control_remote(self, kind, request, *, operation_timeout):
                del operation_timeout
                if kind == "remote-transaction-claim":
                    self.claim_calls += 1
                return guarded.dispatch_privileged(kind, copy.deepcopy(request))

        def inspect(arguments, identity, transaction_id):
            registry = guarded.DurableTransactionRegistry(
                arguments.transaction_registry_root, identity.release_key,
            )
            try:
                return registry.inspect(transaction_id)
            finally:
                registry.close()

        # The exact immutable deadline is expired: a first claim must fail before
        # the remote registry can durably install it.
        root = Path(tempfile.mkdtemp(prefix="slice2-claim-deadline-first-"))
        try:
            arguments = self._arguments(root, watchdog_seconds=60)
            executor = Executor()
            identity, prepared = guarded._prepare_active_transaction(
                arguments, executor, created_epoch=1000,
            )
            deadline = prepared["original_deadline_epoch"]
            self.assertIs(type(deadline), int)
            record_path = Path(arguments.transaction_registry_root) / f"{identity.release_key}.json"
            before = record_path.read_bytes()
            with self.assertRaises(ValueError):
                guarded._claim_active_transaction(
                    arguments, executor, identity, prepared, coordinator_epoch=deadline,
                )
            self.assertEqual(executor.claim_calls, 0)
            self.assertEqual(record_path.read_bytes(), before)
            self.assertIsNone(inspect(arguments, identity, prepared["transaction_id"])["claim"])
        finally:
            shutil.rmtree(root, ignore_errors=True)

        # A strictly-live claim is durable and deterministic adoption is idempotent;
        # the exact boundary, post-expiry, bool, and float epochs all fail before RPC.
        root = Path(tempfile.mkdtemp(prefix="slice2-claim-deadline-adopt-"))
        try:
            arguments = self._arguments(root, watchdog_seconds=60)
            executor = Executor()
            identity, prepared = guarded._prepare_active_transaction(
                arguments, executor, created_epoch=1000,
            )
            deadline = prepared["original_deadline_epoch"]
            authority = guarded._claim_active_transaction(
                arguments, executor, identity, prepared, coordinator_epoch=deadline - 1,
            )
            durable = inspect(arguments, identity, prepared["transaction_id"])
            self.assertEqual(durable, authority.record)
            self.assertEqual(executor.claim_calls, 1)
            adopted = guarded._claim_active_transaction(
                arguments, executor, identity, durable, coordinator_epoch=deadline - 1,
            )
            self.assertEqual(adopted, authority)
            self.assertEqual(executor.claim_calls, 2)
            record_path = Path(arguments.transaction_registry_root) / f"{identity.release_key}.json"
            for invalid_epoch in (deadline, deadline + 1, True, float(deadline - 1)):
                with self.subTest(invalid_epoch=invalid_epoch):
                    before = record_path.read_bytes()
                    calls = executor.claim_calls
                    with self.assertRaises(ValueError):
                        guarded._claim_active_transaction(
                            arguments, executor, identity, durable,
                            coordinator_epoch=invalid_epoch,
                        )
                    self.assertEqual(executor.claim_calls, calls)
                    self.assertEqual(record_path.read_bytes(), before)
                    self.assertEqual(
                        inspect(arguments, identity, prepared["transaction_id"]), durable,
                    )
        finally:
            shutil.rmtree(root, ignore_errors=True)

        # A real durable claim owned by another claimant cannot be adopted or changed.
        root = Path(tempfile.mkdtemp(prefix="slice2-claim-conflict-real-"))
        try:
            arguments = self._arguments(root, watchdog_seconds=60)
            executor = Executor()
            identity, prepared = guarded._prepare_active_transaction(
                arguments, executor, created_epoch=1000,
            )
            deadline = prepared["original_deadline_epoch"]
            conflicting_request = {
                "schema": 1, "registry_root": arguments.transaction_registry_root,
                "release_key": identity.release_key,
                "transaction_id": prepared["transaction_id"],
                "claimant_id": "f" * 32, "request_digest": "e" * 64,
                "claimed_epoch": deadline - 1,
            }
            guarded.dispatch_privileged("remote-transaction-claim", conflicting_request)
            conflicting = inspect(arguments, identity, prepared["transaction_id"])
            record_path = Path(arguments.transaction_registry_root) / f"{identity.release_key}.json"
            before = record_path.read_bytes()
            with self.assertRaises(ValueError):
                guarded._claim_active_transaction(
                    arguments, executor, identity, conflicting,
                    coordinator_epoch=deadline - 1,
                )
            self.assertEqual(record_path.read_bytes(), before)
            self.assertEqual(inspect(arguments, identity, prepared["transaction_id"]), conflicting)
        finally:
            shutil.rmtree(root, ignore_errors=True)


    def test_claim_denial_stale_or_conflicting_authority_fails_closed_without_mutation(self):
        for mode in ("denied", "stale", "conflict"):
            with self.subTest(mode=mode):
                root = Path(tempfile.mkdtemp(prefix=f"slice2-claim-{mode}-"))
                try:
                    arguments = self._arguments(root)

                    class Executor:
                        def __init__(self):
                            self.events: list[str] = []
                            self.last_terminal: dict[str, object] | None = None
                            self.mutations: list[tuple[str, str]] = []

                        def control_remote(self, kind, request, *, operation_timeout):
                            del operation_timeout
                            self.events.append(kind)
                            if kind in {"remote-transaction-create-or-query", "remote-transaction-inspect"}:
                                result = guarded.dispatch_privileged(kind, copy.deepcopy(request))
                                self.last_terminal = copy.deepcopy(result.terminal)
                                return result
                            if kind == "remote-transaction-claim":
                                if mode == "denied":
                                    return guarded.ControlResult(2, "")
                                if mode == "stale":
                                    assert self.last_terminal is not None
                                    return guarded.ControlResult(
                                        0, str(self.last_terminal["record_digest"]),
                                        copy.deepcopy(self.last_terminal),
                                    )
                                conflicting = dict(
                                    copy.deepcopy(request), claimant_id="f" * 32,
                                    request_digest="e" * 64,
                                )
                                return guarded.dispatch_privileged("remote-transaction-claim", conflicting)
                            raise AssertionError(kind)

                        def run_remote(
                            self, attempt_id, operation_id, argv, *, registry=None, run_as,
                            operation_timeout,
                        ):
                            del argv, registry, run_as, operation_timeout
                            self.mutations.append((attempt_id, operation_id))
                            return guarded.OperationResult(2, "", "")

                    executor = Executor()
                    result = guarded.run_release(
                        arguments, executor=executor, acceptance_runner=lambda *_: 2,
                        evidence_store=object(), clock=lambda: 100.0, wall_clock=lambda: 1000.0,
                    )
                    self.assertEqual(result, 2)
                    self.assertEqual(executor.mutations, [])
                    self.assertEqual(executor.events, [
                        "remote-transaction-create-or-query", "remote-transaction-inspect",
                        "remote-transaction-claim",
                    ])
                finally:
                    shutil.rmtree(root, ignore_errors=True)


    def test_recovery_request_and_executor_require_explicit_canonical_attempt_registry(self):
        from deploy.verify_priority_1_3_acceptance import PinnedRemoteExecutor

        root = Path(tempfile.mkdtemp(prefix="slice2-explicit-registry-"))
        try:
            arguments = self._arguments(root)
            attempt_id = "a" * 32
            canonical_registry = f"/run/matrix-priority-1-3/attempts/{attempt_id}.json"
            legacy_registry = f"/run/matrix-priority-1-3/{attempt_id}.json"

            try:
                omitted = guarded._full_recovery_request(arguments, "initial-deploy", attempt_id)
            except TypeError:
                pass
            else:
                self.fail(
                    "omitted attempt registry was accepted and derived legacy path "
                    f"{omitted['registry']!r}; expected fail-closed rejection"
                )

            request = guarded._full_recovery_request(
                arguments, "initial-deploy", attempt_id, canonical_registry,
            )
            self.assertEqual(request["registry"], canonical_registry)
            for invalid_registry in (None, True, legacy_registry, canonical_registry + "/extra"):
                with self.subTest(helper_registry=invalid_registry):
                    with self.assertRaises((TypeError, ValueError)):
                        guarded._full_recovery_request(
                            arguments, "initial-deploy", attempt_id, invalid_registry,
                        )

            runner_calls = []

            def runner(argv, **kwargs):
                runner_calls.append((argv, kwargs))
                raise AssertionError("invalid registry reached SSH runner")

            for name in ("identity", "known-hosts"):
                path = root / name
                path.write_text("fixture", encoding="ascii")
                path.chmod(0o600)
            executor = PinnedRemoteExecutor(
                "fixture@host", root / "identity", root / "known-hosts", runner=runner,
            )
            for invalid_registry in (None, True, legacy_registry, canonical_registry + "/extra"):
                with self.subTest(executor_registry=invalid_registry):
                    with self.assertRaises((TypeError, ValueError)):
                        executor.run_remote(
                            attempt_id, "deploy", ["/bin/true"], registry=invalid_registry,
                            run_as="root", operation_timeout=10,
                        )
            self.assertEqual(runner_calls, [])
        finally:
            shutil.rmtree(root, ignore_errors=True)


    def test_pinned_executor_frames_remote_start_with_explicit_canonical_attempt_backlink(self):
        from deploy.verify_priority_1_3_acceptance import PinnedRemoteExecutor

        root = Path(tempfile.mkdtemp(prefix="slice2-pinned-registry-"))
        try:
            for name in ("identity", "known-hosts"):
                path = root / name
                path.write_text("fixture", encoding="ascii")
                path.chmod(0o600)
            observed: list[dict[str, object]] = []

            def runner(argv, **kwargs):
                del kwargs
                payload = argv[argv.index("--request-b64") + 1]
                helper = argv[-3]
                request = guarded.decode_privileged_request(helper, payload)
                attempt_id = str(request["attempt_id"])
                operation_id = str(request["operation_id"])
                unit = f"matrix-p13-op-{attempt_id}-{operation_id}.service"
                cgroup = f"/system.slice/{unit}"
                if helper == "remote-start":
                    observed.append(request)
                    envelope = {"returncode": 0, "unit": unit, "cgroup": cgroup}
                else:
                    output_digest = guarded._operation_output_digest(
                        attempt_id, operation_id, str(request["registry"]), unit, cgroup, 0, "", "",
                    )
                    envelope = {
                        "returncode": 0, "state": "TERMINAL", "attempt_id": attempt_id,
                        "operation_id": operation_id, "registry": request["registry"],
                        "unit": unit, "cgroup": cgroup, "registry_digest": "d" * 64,
                        "exit_status": 0, "stdout": "", "stderr": "",
                        "output_digest": output_digest,
                    }
                return subprocess.CompletedProcess(argv, 0, json.dumps(envelope), "")

            attempt_id = "a" * 32
            canonical_registry = f"/run/matrix-priority-1-3/attempts/{attempt_id}.json"
            executor = PinnedRemoteExecutor(
                "fixture@host", root / "identity", root / "known-hosts", runner=runner,
            )
            result = executor.run_remote(
                attempt_id, "deploy", ["/bin/true"], registry=canonical_registry,
                run_as="root", operation_timeout=10,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0]["registry"], canonical_registry)
            self.assertNotEqual(observed[0]["registry"], f"/run/matrix-priority-1-3/{attempt_id}.json")
        finally:
            shutil.rmtree(root, ignore_errors=True)


class Slice2ActiveTransitionAuthorityTests(Slice2ActiveClaimAndRegistryTests):
    def _claimed_authority(self, root, executor):
        arguments = self._arguments(root)
        identity, prepared = guarded._prepare_active_transaction(
            arguments, executor, created_epoch=1000,
        )
        authority = guarded._claim_active_transaction(
            arguments, executor, identity, prepared, coordinator_epoch=1000,
        )
        return arguments, authority

    def test_lost_transition_response_accepts_only_inspected_exact_idempotent_successor(self):
        root = Path(tempfile.mkdtemp(prefix="slice2-lost-transition-"))
        try:
            class Executor:
                def __init__(self):
                    self.calls = []
                    self.lose_transition = False

                def control_remote(this, kind, request, *, operation_timeout):
                    del operation_timeout
                    this.calls.append(kind)
                    result = guarded.dispatch_privileged(kind, copy.deepcopy(request))
                    if kind == "remote-transaction-transition" and this.lose_transition:
                        this.lose_transition = False
                        raise TimeoutError("response lost after durable transition")
                    return result

            executor = Executor()
            arguments, authority = self._claimed_authority(root, executor)
            executor.lose_transition = True
            successor = guarded._active_transition(
                arguments, executor, authority, "initial-deploy", "READY", "GUARD_ARMING",
                lambda: 1000.0,
            )
            self.assertEqual(successor.record["attempts"][0]["state"], "GUARD_ARMING")
            self.assertEqual(executor.calls[-2:], [
                "remote-transaction-transition", "remote-transaction-inspect",
            ])
            self.assertEqual(successor.record["claim"], authority.record["claim"])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_stale_or_malformed_transition_response_fails_closed_at_durable_ready(self):
        for mode in ("stale", "malformed"):
            with self.subTest(mode=mode):
                root = Path(tempfile.mkdtemp(prefix=f"slice2-transition-{mode}-"))
                try:
                    class Executor:
                        def __init__(self):
                            self.mode = None
                            self.last = None
                            self.calls = []

                        def control_remote(this, kind, request, *, operation_timeout):
                            del operation_timeout
                            this.calls.append(kind)
                            if kind == "remote-transaction-transition" and this.mode:
                                if this.mode == "stale":
                                    return guarded.ControlResult(
                                        0, this.last["record_digest"], copy.deepcopy(this.last),
                                    )
                                return guarded.ControlResult(0, "f" * 64, {"schema": 1})
                            result = guarded.dispatch_privileged(kind, copy.deepcopy(request))
                            if type(result) is guarded.ControlResult and type(result.terminal) is dict:
                                this.last = copy.deepcopy(result.terminal)
                            return result

                    executor = Executor()
                    arguments, authority = self._claimed_authority(root, executor)
                    executor.mode = mode
                    with self.assertRaises(ValueError):
                        guarded._active_transition(
                            arguments, executor, authority, "initial-deploy", "READY",
                            "GUARD_ARMING", lambda: 1000.0,
                        )
                    registry = guarded.DurableTransactionRegistry(
                        arguments.transaction_registry_root, authority.identity.release_key,
                    )
                    try:
                        durable = registry.inspect(authority.record["transaction_id"])
                    finally:
                        registry.close()
                    self.assertEqual(durable["attempts"][0]["state"], "READY")
                    self.assertEqual(executor.calls[-1], "remote-transaction-transition")
                finally:
                    shutil.rmtree(root, ignore_errors=True)

    def test_successful_three_phase_run_persists_exact_remote_state_graph_and_advances(self):
        root = Path(tempfile.mkdtemp(prefix="slice2-active-transition-success-"))
        try:
            arguments = self._arguments(root)

            class Clock:
                def __init__(self):
                    self.now = 100.0

                def __call__(self):
                    return self.now

            clock = Clock()

            class Executor:
                def __init__(self):
                    self.events: list[tuple[str, str, str]] = []
                    self.requests: list[tuple[str, dict[str, object]]] = []
                    self.identity = None
                    self.transaction_id = None
                    self.terminals: dict[str, dict[str, object]] = {}

                def state(self):
                    authority = guarded.DurableTransactionRegistry(
                        arguments.transaction_registry_root, self.identity.release_key,
                    )
                    try:
                        return authority.inspect(self.transaction_id)
                    finally:
                        authority.close()

                def control_remote(self, kind, request, *, operation_timeout):
                    self.assert_timeout(operation_timeout)
                    immutable = copy.deepcopy(request)
                    if kind.startswith("remote-transaction-"):
                        self.requests.append((kind, immutable))
                        result = guarded.dispatch_privileged(kind, immutable)
                        record = copy.deepcopy(result.terminal)
                        if kind == "remote-transaction-create-or-query":
                            self.identity = guarded.canonical_release_identity(request["release_document"])
                            self.transaction_id = record["transaction_id"]
                        attempt = record["attempts"][record["current_phase_index"]]
                        self.events.append((kind, attempt["phase"], attempt["state"]))
                        return result
                    current = self.state()
                    attempt = current["attempts"][current["current_phase_index"]]
                    self.events.append((kind, attempt["phase"], attempt["state"]))
                    if kind == "remote-recover":
                        digest = __import__("hashlib").sha256(
                            guarded._canonical_json_bytes({
                                "request_digest": guarded.canonical_recovery_request_digest(immutable),
                                "inner_result_digest": "f" * 64, "returncode": 0,
                            })
                        ).hexdigest()
                        return guarded.ControlResult(0, digest)
                    if kind == "remote-commit" and immutable.get("query") is True:
                        terminal = self.terminals.get(str(immutable["attempt_id"]))
                        if terminal is None:
                            return guarded.ControlResult(3, "")
                        return guarded.ControlResult(0, str(terminal["result_digest"]), copy.deepcopy(terminal))
                    if kind == "remote-commit":
                        success = {key: immutable[key] for key in (
                            "schema", "boundary", "phase", "attempt_id", "operation_id",
                            "evidence_digest", "pending_note_id",
                        )}
                        digest = __import__("hashlib").sha256(
                            guarded._canonical_json_bytes(success) + b"\n"
                        ).hexdigest()
                        terminal = {
                            "schema": 1, "request": immutable,
                            "request_digest": guarded._request_digest("remote-commit", immutable),
                            "result_digest": digest, "committed_epoch": 1000.0,
                        }
                        self.terminals[str(immutable["attempt_id"])] = terminal
                        return guarded.ControlResult(0, digest, copy.deepcopy(terminal))
                    raise AssertionError(kind)

                @staticmethod
                def assert_timeout(value):
                    if type(value) is not int or value <= 0:
                        raise AssertionError(value)

                def run_remote(self, attempt_id, operation_id, argv, *, registry, run_as, operation_timeout):
                    del attempt_id, argv, registry, run_as
                    self.assert_timeout(operation_timeout)
                    current = self.state()
                    attempt = current["attempts"][current["current_phase_index"]]
                    self.events.append(("remote-start:" + operation_id, attempt["phase"], attempt["state"]))
                    operation = __import__("hashlib").sha256(
                        f"{attempt['attempt_id']}\0{operation_id}".encode("utf-8")
                    ).hexdigest()[:32]
                    unit = f"matrix-p13-op-{attempt['attempt_id']}-{operation}.service"
                    return guarded.OperationResult(0, unit, f"/system.slice/{unit}")

            executor = Executor()
            acceptance_timeouts = []

            class Notes:
                def __init__(self):
                    self.next_id = 100
                    self.final_publications = []

                def pending(self, record):
                    current = executor.state(); attempt = current["attempts"][current["current_phase_index"]]
                    executor.events.append(("PENDING", attempt["phase"], attempt["state"]))
                    self.next_id += 1
                    return str(self.next_id)

                def reconcile(self, note_id, state, record):
                    del note_id, record
                    current = executor.state(); attempt = current["attempts"][current["current_phase_index"]]
                    executor.events.append((state, attempt["phase"], attempt["state"]))

                def final(self, record):
                    if self.final_publications:
                        raise AssertionError("duplicate or conflicting FINAL POST")
                    self.final_publications.append(copy.deepcopy(record))
                    current = executor.state(); attempt = current["attempts"][current["current_phase_index"]]
                    executor.events.append(("FINAL", attempt["phase"], attempt["state"]))

                def discover_final(self, record):
                    if len(self.final_publications) > 1:
                        raise ValueError("duplicate external FINAL authority")
                    if not self.final_publications:
                        return None
                    if self.final_publications[0] != record:
                        raise ValueError("conflicting external FINAL authority")
                    return {"note_id": "900", "state": "FINAL",
                            "record": copy.deepcopy(self.final_publications[0])}

            def accept(namespace, timeout):
                Executor.assert_timeout(timeout)
                acceptance_timeouts.append(timeout)
                current = executor.state(); attempt = current["attempts"][current["current_phase_index"]]
                executor.events.append(("accept:" + namespace.mode, attempt["phase"], attempt["state"]))
                evidence = Path(namespace.evidence_file)
                evidence.write_text(json.dumps({"schema": 2, "attempt_id": namespace.attempt_id}) + "\n")
                evidence.chmod(0o600)
                clock.now += 371.515
                return 0

            notes = Notes()
            result = guarded.run_release(
                arguments, executor=executor, acceptance_runner=accept,
                evidence_store=notes, clock=clock, wall_clock=lambda: 1000.0,
            )

            self.assertEqual(result, 0, executor.events)
            graph = [(kind, phase, state) for kind, phase, state in executor.events
                     if kind in {"remote-transaction-transition", "remote-transaction-terminal",
                                 "remote-transaction-advance"}]
            self.assertEqual(graph, [
                *(('remote-transaction-transition', 'initial-deploy', state) for state in (
                    'GUARD_ARMING', 'GUARDED', 'MUTATION_RUNNING', 'MUTATION_DONE',
                    'ACCEPTANCE_RUNNING', 'CANDIDATE_DURABLE', 'PENDING_BOUND',
                    'SEALED_COMMITTING')),
                ('remote-transaction-terminal', 'initial-deploy', 'COMMITTED_UNRECONCILED'),
                ('remote-transaction-transition', 'initial-deploy', 'ACCEPTED'),
                ('remote-transaction-advance', 'rollback-proof', 'READY'),
                *(('remote-transaction-transition', 'rollback-proof', state) for state in (
                    'GUARD_ARMING', 'GUARDED', 'MUTATION_RUNNING', 'MUTATION_DONE',
                    'ACCEPTANCE_RUNNING', 'CANDIDATE_DURABLE', 'PENDING_BOUND',
                    'SEALED_RECOVERING')),
                ('remote-transaction-terminal', 'rollback-proof', 'RECOVERED_UNVERIFIED'),
                ('remote-transaction-transition', 'rollback-proof', 'ROLLBACK_VERIFIED'),
                ('remote-transaction-advance', 'final-deploy', 'READY'),
                *(('remote-transaction-transition', 'final-deploy', state) for state in (
                    'GUARD_ARMING', 'GUARDED', 'MUTATION_RUNNING', 'MUTATION_DONE',
                    'ACCEPTANCE_RUNNING', 'CANDIDATE_DURABLE', 'PENDING_BOUND',
                    'SEALED_COMMITTING')),
                ('remote-transaction-terminal', 'final-deploy', 'COMMITTED_UNRECONCILED'),
                ('remote-transaction-transition', 'final-deploy', 'ACCEPTED'),
                ('remote-transaction-advance', 'final-deploy', 'ACCEPTED'),
            ])
            final = executor.state()
            self.assertEqual(final["state"], "THREE_ATTEMPTS_ACCEPTED")
            self.assertEqual([attempt["state"] for attempt in final["attempts"]],
                             ["ACCEPTED", "ROLLBACK_VERIFIED", "ACCEPTED"])
            self.assertEqual(
                acceptance_timeouts,
                [guarded._AGGREGATE_ACCEPTANCE_BUDGET_SECONDS] * 5,
            )
            self.assertEqual(len(notes.final_publications), 1)
            create_request = next(request for kind, request in executor.requests
                                  if kind == "remote-transaction-create-or-query")
            self.assertEqual(
                cast(int, create_request["original_deadline_epoch"])
                - cast(int, create_request["created_epoch"]),
                3 * arguments.watchdog_seconds
                + 2 * guarded._POST_MUTATION_VERIFICATION_WINDOW_SECONDS,
            )
            self.assertIsNotNone(final["claim"])
            active_requests = [(kind, request) for kind, request in executor.requests if kind in {
                "remote-transaction-transition", "remote-transaction-terminal",
                "remote-transaction-advance",
            }]
            epochs = []
            for kind, request in active_requests:
                self.assertEqual(request["transaction_id"], final["transaction_id"])
                self.assertEqual(request["claimant_id"], final["claim"]["claimant_id"])
                self.assertEqual(request["release_key"], final["release_key"])
                operation = kind.removeprefix("remote-transaction-")
                self.assertEqual(
                    request["request_digest"],
                    guarded._active_transaction_request_digest(operation, request),
                )
                epoch_field = "terminal_epoch" if operation == "terminal" else "transition_epoch"
                epochs.append(request[epoch_field])
            self.assertEqual(epochs, sorted(epochs))
            self.assertTrue(all(final["created_epoch"] <= epoch <= final["original_deadline_epoch"]
                                for epoch in epochs))
        finally:
            shutil.rmtree(root, ignore_errors=True)


class Slice2FreshCoordinatorRecoveryTests(Slice2ActiveClaimAndRegistryTests):
    """Tracer C uses real DurableTransactionRegistry records across fresh facades."""

    class Notes:
        def __init__(self, publications=None):
            self.publications = copy.deepcopy(publications or [])
            self.next_id = 200
            self.finals = []
            self.canonicalizer = object.__new__(guarded.GitLabEvidenceStore)
            self.canonicalizer._publication_authority = None
        def bind_publication_authority(self, identity, transaction, **context):
            self.canonicalizer.bind_publication_authority(identity, transaction, **context)
            for item in self.publications:
                if "release_key" not in item["record"]:
                    item["record"] = self.canonicalizer._canonical_record(
                        item["state"], item["record"],
                        None if item["state"] == "PENDING" else item["note_id"],
                    )
        def discover(self, **identity):
            del identity
            return copy.deepcopy(self.publications)
        def discover_final(self, record):
            canonical = self.canonicalizer._canonical_record("FINAL", copy.deepcopy(record))
            finals = [item for item in self.publications if item["state"] == "FINAL"]
            if len(finals) > 1: raise ValueError("duplicate final")
            if not finals: return None
            if finals[0]["record"] != canonical: raise ValueError("conflicting final")
            return copy.deepcopy(finals[0])
        def pending(self, record):
            self.next_id += 1
            self.publications.append({"note_id": str(self.next_id), "state": "PENDING",
                "record": self.canonicalizer._canonical_record("PENDING", copy.deepcopy(record))})
            return str(self.next_id)
        def reconcile(self, note_id, state, record):
            canonical = self.canonicalizer._canonical_record(state, copy.deepcopy(record), note_id)
            for item in self.publications:
                if item["note_id"] == note_id:
                    item.update(state=state, record=canonical); return
            raise AssertionError("unknown note")
        def final(self, record):
            canonical = self.canonicalizer._canonical_record("FINAL", copy.deepcopy(record))
            finals = [item for item in self.publications if item["state"] == "FINAL"]
            if finals:
                if finals[0]["record"] == canonical:
                    raise AssertionError("duplicate FINAL POST")
                raise AssertionError("conflicting FINAL POST")
            self.next_id += 1
            self.publications.append({"note_id": str(self.next_id), "state": "FINAL",
                                      "record": copy.deepcopy(canonical)})
            self.finals.append(canonical)

    class Executor:
        def __init__(self, arguments, commits=None, recoveries=None):
            self.arguments = arguments
            self.commits = copy.deepcopy(commits or {})
            self.recoveries = copy.deepcopy(recoveries or {})
            self.mutations = []
            self.controls = []
        def control_remote(self, kind, request, *, operation_timeout):
            assert type(operation_timeout) is int and operation_timeout > 0
            request = copy.deepcopy(request); self.controls.append((kind, request))
            if kind.startswith("remote-transaction-"):
                return guarded.dispatch_privileged(kind, request)
            attempt_id = str(request["attempt_id"])
            if kind == "remote-commit":
                if request.get("query") is True:
                    terminal = self.commits.get(attempt_id)
                    return (guarded.ControlResult(3, "") if terminal is None else
                            guarded.ControlResult(0, terminal["result_digest"], copy.deepcopy(terminal)))
                success = {key: request[key] for key in ("schema", "boundary", "phase", "attempt_id",
                           "operation_id", "evidence_digest", "pending_note_id")}
                digest = __import__("hashlib").sha256(guarded._canonical_json_bytes(success) + b"\n").hexdigest()
                terminal = {"schema": 1, "request": request,
                            "request_digest": guarded._request_digest("remote-commit", request),
                            "result_digest": digest, "committed_epoch": 1000.0}
                self.commits[attempt_id] = terminal
                return guarded.ControlResult(0, digest, copy.deepcopy(terminal))
            if kind == "remote-recover":
                if request.get("query") is True:
                    terminal = self.recoveries.get(attempt_id)
                    return (guarded.ControlResult(3, "") if terminal is None else
                            guarded.ControlResult(0, terminal["result_digest"], copy.deepcopy(terminal)))
                digest = __import__("hashlib").sha256(guarded._canonical_json_bytes({
                    "request_digest": guarded.canonical_recovery_request_digest(request),
                    "inner_result_digest": "f" * 64, "returncode": 0})).hexdigest()
                self.recoveries[attempt_id] = {"schema": 1, "request": request,
                    "request_digest": guarded.canonical_recovery_request_digest(request),
                    "result_digest": digest, "recovered_epoch": 1000.0}
                return guarded.ControlResult(0, digest)
            raise AssertionError(kind)
        def run_remote(self, attempt_id, operation_id, argv, *, registry, run_as, operation_timeout):
            del argv, registry, run_as
            assert type(operation_timeout) is int and operation_timeout > 0
            if operation_id != "watchdog-arm": self.mutations.append((attempt_id, operation_id))
            operation = __import__("hashlib").sha256(f"{attempt_id}\0{operation_id}".encode()).hexdigest()[:32]
            unit = f"matrix-p13-op-{attempt_id}-{operation}.service"
            return guarded.OperationResult(0, unit, f"/system.slice/{unit}")

    @staticmethod
    def _accept(namespace, timeout):
        assert type(timeout) is int and timeout > 0
        path = Path(namespace.evidence_file)
        path.write_text(json.dumps({"schema": 2, "attempt_id": namespace.attempt_id}) + "\n")
        path.chmod(0o600)
        return 0

    def _seed_initial_accepted(self, arguments, executor):
        identity, prepared = guarded._prepare_active_transaction(arguments, executor, created_epoch=1000)
        authority = guarded._claim_active_transaction(arguments, executor, identity, prepared,
                                                       coordinator_epoch=1000)
        phase = "initial-deploy"
        for source, target in (("READY", "GUARD_ARMING"), ("GUARD_ARMING", "GUARDED"),
            ("GUARDED", "MUTATION_RUNNING"), ("MUTATION_RUNNING", "MUTATION_DONE"),
            ("MUTATION_DONE", "ACCEPTANCE_RUNNING"), ("ACCEPTANCE_RUNNING", "CANDIDATE_DURABLE"),
            ("CANDIDATE_DURABLE", "PENDING_BOUND"), ("PENDING_BOUND", "SEALED_COMMITTING")):
            authority = guarded._active_transition(arguments, executor, authority, phase, source, target,
                                                    lambda: 1000.0)
        attempt = authority.record["attempts"][0]; evidence_digest = "7" * 64
        operation_id = __import__("hashlib").sha256(f"{attempt['attempt_id']}\0commit".encode()).hexdigest()[:32]
        request = {"schema": 1, "boundary": arguments.boundary, "phase": phase,
            "attempt_id": attempt["attempt_id"], "operation_id": operation_id,
            "registry": attempt["registry"], "evidence_digest": evidence_digest,
            "pending_note_id": "101", "guard_unit": f"matrix-p13-recover-{attempt['attempt_id']}.service",
            "success_record": f"/run/matrix-priority-1-3/{attempt['attempt_id']}/success.json",
            "control_unit": f"matrix-p13-commit-{attempt['attempt_id']}.service",
            "control_cgroup": f"/system.slice/matrix-p13-commit-{attempt['attempt_id']}.service"}
        committed = executor.control_remote("remote-commit", request, operation_timeout=60)
        authority = guarded._active_terminal(arguments, executor, authority, phase, "commit",
            "SEALED_COMMITTING", "COMMITTED_UNRECONCILED", committed.digest, lambda: 1000.0)
        authority = guarded._active_transition(arguments, executor, authority, phase,
            "COMMITTED_UNRECONCILED", "ACCEPTED", lambda: 1000.0)
        authority = guarded._active_advance(arguments, executor, authority, phase, lambda: 1000.0)
        publication = {"note_id": "101", "state": "ACCEPTED", "record": {
            "boundary": arguments.boundary, "phase": phase, "attempt_id": attempt["attempt_id"],
            "stage": arguments.stage, "bundle": arguments.bundle, "evidence_digest": evidence_digest,
            "remote_record_digest": committed.digest}}
        return authority, publication

    def test_fresh_coordinator_adopts_same_unexpired_deterministic_claim(self):
        root = Path(tempfile.mkdtemp(prefix="slice2-fresh-claim-"))
        try:
            arguments = self._arguments(root, resume=True); first = self.Executor(arguments)
            identity, prepared = guarded._prepare_active_transaction(arguments, first, created_epoch=1000)
            original = guarded._claim_active_transaction(arguments, first, identity, prepared,
                                                          coordinator_epoch=1000)
            fresh = self.Executor(arguments)
            _, inspected = guarded._prepare_active_transaction(arguments, fresh, created_epoch=1001)
            adopted = guarded._claim_active_transaction(arguments, fresh, identity, inspected,
                                                         coordinator_epoch=1001)
            self.assertEqual(adopted.record["claim"], original.record["claim"])
            self.assertEqual((adopted.claimant_id, adopted.claimed_epoch),
                             (original.claimant_id, 1000))
        finally: shutil.rmtree(root, ignore_errors=True)

    def test_initial_accepted_advanced_with_all_local_observers_lost_resumes_without_initial_mutation(self):
        root = Path(tempfile.mkdtemp(prefix="slice2-fresh-initial-accepted-"))
        try:
            arguments = self._arguments(root, resume=True); seeder = self.Executor(arguments)
            authority, publication = self._seed_initial_accepted(arguments, seeder)
            shutil.rmtree(arguments.evidence_dir); Path(arguments.evidence_dir).mkdir(mode=0o700)
            fresh = self.Executor(arguments, seeder.commits, seeder.recoveries)
            result = guarded.run_release(arguments, executor=fresh, acceptance_runner=self._accept,
                evidence_store=self.Notes([publication]), clock=lambda: 100.0, wall_clock=lambda: 1000.0)
            self.assertEqual(result, 0, fresh.controls)
            self.assertNotIn((authority.record["attempts"][0]["attempt_id"], "deploy"), fresh.mutations)
            self.assertEqual([item[1] for item in fresh.mutations], ["rollback", "deploy"])
        finally: shutil.rmtree(root, ignore_errors=True)







    @staticmethod
    def _external_attempt_record(state="PENDING", **changes):
        record = {
            "release_key": "2" * 64, "transaction_identity": "d" * 64,
            "transaction_id": "e" * 32, "boundary": "b1",
            "phase": "initial-deploy", "attempt_index": 0, "attempt_id": "a" * 32,
            "stage": "/stage", "bundle": "/bundle", "evidence_digest": "b" * 64,
        }
        if state != "PENDING":
            record.update(note_id="41", terminal_kind="recovery" if state == "ROLLED_BACK" else "commit",
                          remote_record_digest="c" * 64)
        if state == "ROLLED_BACK":
            record["verification_digest"] = "3" * 64
        record.update(changes)
        return record

    def test_gitlab_external_authority_discovers_only_exact_bound_attempt_publications(self):
        store = object.__new__(guarded.GitLabEvidenceStore)
        record = self._external_attempt_record("ACCEPTED")
        body = guarded.GitLabEvidenceStore._body("ATTEMPT", "ACCEPTED", record)
        store._list_notes = lambda: [{"id": 41, "body": body},
                                     {"id": 42, "body": "unrelated"}]
        result = store.discover(transaction_identity="d" * 64, transaction_id="e" * 32,
                                boundary="b1", attempt_ids=("a" * 32, "f" * 32, "1" * 32))
        self.assertEqual(result, [{"note_id": "41", "state": "ACCEPTED",
                                   "record": record}])

    def test_gitlab_adapter_collects_required_publication_from_second_note_page(self):
        import types
        import urllib.parse
        import urllib.request
        store = object.__new__(guarded.GitLabEvidenceStore)
        store._token = "token"
        store._notes_url = "https://gitlab.invalid/api/v4/projects/2/merge_requests/73/notes"
        record = self._external_attempt_record("ACCEPTED")
        body = guarded.GitLabEvidenceStore._body("ATTEMPT", "ACCEPTED", record)
        pages = {
            1: ([{"id": 1000 + index, "body": f"newer unrelated note {index}"}
                 for index in range(100)], {"X-Next-Page": "2"}),
            2: ([{"id": 41, "body": body}], {"X-Next-Page": ""}),
        }
        requested = []

        class Response:
            def __init__(self, value, headers):
                self.value = json.dumps(value).encode("utf-8"); self.headers = headers
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit): return self.value[:limit]
        def urlopen(request, timeout):
            self.assertEqual(timeout, 10)
            self.assertEqual(request.method, "GET")
            self.assertIsNone(request.data)
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            requested.append(query)
            page = int(query.get("page", ["1"])[0])
            value, headers = pages[page]
            return Response(value, headers)
        store._urllib_request = types.SimpleNamespace(Request=urllib.request.Request, urlopen=urlopen)

        result = store.discover(
            transaction_identity="d" * 64, transaction_id="e" * 32,
            boundary="b1", attempt_ids=("a" * 32, "f" * 32, "1" * 32),
        )

        self.assertEqual(result, [{"note_id": "41", "state": "ACCEPTED", "record": record}])
        self.assertEqual([query["page"] for query in requested], [["1"], ["2"]])
        self.assertTrue(all(query["per_page"] == ["100"] for query in requested))

    def test_gitlab_external_authority_discovers_exact_bound_final_publication(self):
        store = object.__new__(guarded.GitLabEvidenceStore)
        record = self._external_attempt_record(
            "ACCEPTED", phase="final-deploy", attempt_index=2, attempt_id="1" * 32,
            accepted_record_digest="4" * 64, verification_digest="5" * 64,
        )
        body = guarded.GitLabEvidenceStore._body("FINAL", "FINAL", record)
        store._list_notes = lambda: [{"id": 42, "body": body}]

        result = store.discover(
            transaction_identity="d" * 64, transaction_id="e" * 32,
            boundary="b1", attempt_ids=("a" * 32, "f" * 32, "1" * 32),
        )

        self.assertEqual(result, [{"note_id": "42", "state": "FINAL", "record": record}])
        self.assertIsNot(result[0]["record"], record)

    def test_gitlab_external_authority_rejects_duplicate_exact_final_publications(self):
        store = object.__new__(guarded.GitLabEvidenceStore)
        record = self._external_attempt_record(
            "ACCEPTED", phase="final-deploy", attempt_index=2, attempt_id="1" * 32,
            accepted_record_digest="4" * 64, verification_digest="5" * 64,
        )
        body = guarded.GitLabEvidenceStore._body("FINAL", "FINAL", record)
        store._list_notes = lambda: [{"id": 42, "body": body}, {"id": 43, "body": body}]

        with self.assertRaisesRegex(ValueError, "final.*duplicated"):
            store.discover(
                transaction_identity="d" * 64, transaction_id="e" * 32,
                boundary="b1", attempt_ids=("a" * 32, "f" * 32, "1" * 32),
            )

    def test_gitlab_final_adoption_rejects_conflicting_exact_identity_publication(self):
        store = object.__new__(guarded.GitLabEvidenceStore)
        store._publication_authority = {
            "release_key": "2" * 64, "transaction_identity": "d" * 64,
            "transaction_id": "e" * 32, "boundary": "b1", "stage": "/stage",
            "bundle": "/bundle", "attempt_ids": ("a" * 32, "f" * 32, "1" * 32),
        }
        conflicting = self._external_attempt_record(
            "ACCEPTED", phase="final-deploy", attempt_index=2, attempt_id="1" * 32,
            accepted_record_digest="4" * 64, verification_digest="6" * 64,
        )
        store.discover = lambda **identity: [
            {"note_id": "42", "state": "FINAL", "record": conflicting}
        ]
        expected = {
            "boundary": "b1", "attempt_id": "1" * 32, "note_id": "41",
            "candidate_evidence_digest": "b" * 64, "remote_record_digest": "c" * 64,
            "accepted_record_digest": "4" * 64, "verification_digest": "5" * 64,
        }

        with self.assertRaisesRegex(ValueError, "conflict"):
            store.discover_final(expected)

    def test_gitlab_publication_writer_emits_one_exact_envelope_for_every_authority(self):
        records = {
            ("ATTEMPT", "PENDING"): self._external_attempt_record(),
            ("ATTEMPT", "ACCEPTED"): self._external_attempt_record("ACCEPTED"),
            ("ATTEMPT", "ROLLED_BACK"): self._external_attempt_record("ROLLED_BACK"),
            ("FINAL", "FINAL"): self._external_attempt_record("ACCEPTED", phase="final-deploy",
                attempt_index=2, attempt_id="1" * 32, accepted_record_digest="4" * 64,
                verification_digest="5" * 64),
        }
        for (kind, state), record in records.items():
            with self.subTest(kind=kind, state=state):
                body = guarded.GitLabEvidenceStore._body(kind, state, record)
                self.assertEqual(json.loads(body.removeprefix("matrix-priority-1-3-release ")), {
                    "schema": 1, "kind": kind, "state": state, "record": record,
                })

    @staticmethod
    def _legacy_gitlab_publication(**changes):
        document = {
            "schema": 1, "state": "ACCEPTED", "boundary": "b1",
            "phase": "initial-deploy", "attempt_id": "a" * 32,
            "stage": "/stage", "bundle": "/bundle",
            "evidence_digest": "b" * 64, "remote_record_digest": "c" * 64,
        }
        document.update(changes)
        return "matrix-priority-1-3-release " + json.dumps(document, sort_keys=True)

    def _discover_legacy_gitlab_publication(self, body):
        store = object.__new__(guarded.GitLabEvidenceStore)
        store._list_notes = lambda: [{"id": 41, "body": body}]
        return store.discover(
            transaction_identity="d" * 64, transaction_id="e" * 32,
            boundary="b1", attempt_ids=("a" * 32, "f" * 32, "1" * 32),
        )

    def test_gitlab_discovery_rejects_bool_true_schema_instead_of_treating_it_as_one(self):
        with self.assertRaises(ValueError):
            self._discover_legacy_gitlab_publication(
                self._legacy_gitlab_publication(schema=True)
            )

    def test_gitlab_discovery_rejects_surplus_outer_and_nested_publication_fields(self):
        with self.assertRaises(ValueError):
            self._discover_legacy_gitlab_publication(self._legacy_gitlab_publication(
                surplus="outer", terminal={"digest": "c" * 64, "surplus": "nested"},
            ))

    def test_gitlab_discovery_rejects_wrong_explicit_transaction_identity_and_id(self):
        with self.assertRaises(ValueError):
            self._discover_legacy_gitlab_publication(self._legacy_gitlab_publication(
                transaction_identity="0" * 64, transaction_id="2" * 32,
            ))

    def test_gitlab_discovery_rejects_other_non_exact_schema_one_values(self):
        for schema in (False, 1.0, "1"):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                self._discover_legacy_gitlab_publication(
                    self._legacy_gitlab_publication(schema=schema)
                )

    def test_ambiguous_inflight_states_fail_closed_without_any_release_mutation(self):
        paths = {
            "GUARD_ARMING": (("READY", "GUARD_ARMING"),),
            "MUTATION_RUNNING": (("READY", "GUARD_ARMING"), ("GUARD_ARMING", "GUARDED"),
                                 ("GUARDED", "MUTATION_RUNNING")),
            "ACCEPTANCE_RUNNING": (("READY", "GUARD_ARMING"), ("GUARD_ARMING", "GUARDED"),
                ("GUARDED", "MUTATION_RUNNING"), ("MUTATION_RUNNING", "MUTATION_DONE"),
                ("MUTATION_DONE", "ACCEPTANCE_RUNNING")),
            "PENDING_BOUND": (("READY", "GUARD_ARMING"), ("GUARD_ARMING", "GUARDED"),
                ("GUARDED", "MUTATION_RUNNING"), ("MUTATION_RUNNING", "MUTATION_DONE"),
                ("MUTATION_DONE", "ACCEPTANCE_RUNNING"), ("ACCEPTANCE_RUNNING", "CANDIDATE_DURABLE"),
                ("CANDIDATE_DURABLE", "PENDING_BOUND")),
            "SEALED_RECOVERING": (("READY", "GUARD_ARMING"), ("GUARD_ARMING", "GUARDED"),
                                  ("GUARDED", "SEALED_RECOVERING")),
        }
        for state, transitions in paths.items():
            with self.subTest(state=state):
                root = Path(tempfile.mkdtemp(prefix="slice2-ambiguous-"))
                try:
                    arguments = self._arguments(root, resume=True); seed = self.Executor(arguments)
                    identity, prepared = guarded._prepare_active_transaction(arguments, seed, created_epoch=1000)
                    authority = guarded._claim_active_transaction(arguments, seed, identity, prepared,
                                                                   coordinator_epoch=1000)
                    for source, target in transitions:
                        authority = guarded._active_transition(arguments, seed, authority, "initial-deploy",
                                                               source, target, lambda: 1000.0)
                    publications = []
                    if state == "PENDING_BOUND":
                        attempt = authority.record["attempts"][0]
                        publications.append({"note_id": "101", "state": "PENDING", "record": {
                            "boundary": arguments.boundary, "phase": "initial-deploy",
                            "attempt_id": attempt["attempt_id"], "stage": arguments.stage,
                            "bundle": arguments.bundle, "evidence_digest": "6" * 64}})
                    fresh = self.Executor(arguments, seed.commits, seed.recoveries)
                    result = guarded.run_release(arguments, executor=fresh, acceptance_runner=self._accept,
                        evidence_store=self.Notes(publications), clock=lambda: 100.0,
                        wall_clock=lambda: 1000.0)
                    self.assertEqual(result, 2)
                    self.assertEqual(fresh.mutations, [])
                finally: shutil.rmtree(root, ignore_errors=True)

    def test_recovery_terminal_query_has_canonical_read_only_request_schema(self):
        root = Path(tempfile.mkdtemp(prefix="slice2-recovery-query-schema-"))
        try:
            arguments = self._arguments(root)
            attempt_id = "a" * 32
            request = guarded._full_recovery_request(arguments, "rollback-proof", attempt_id,
                f"/run/matrix-priority-1-3/attempts/{attempt_id}.json")
            query = {**request, "query": True}
            self.assertEqual(guarded.validate_privileged_request("remote-recover", query), query)
            payload = guarded.encode_privileged_request("remote-recover", query)
            self.assertEqual(guarded.decode_privileged_request("remote-recover", payload), query)
        finally: shutil.rmtree(root, ignore_errors=True)

    def test_recovery_terminal_rejects_non_finite_recovered_epoch_directly(self):
        root = Path(tempfile.mkdtemp(prefix="slice2-recovery-epoch-direct-"))
        try:
            arguments = self._arguments(root); attempt_id = "a" * 32
            request = guarded._full_recovery_request(arguments, "rollback-proof", attempt_id,
                f"/run/matrix-priority-1-3/attempts/{attempt_id}.json")
            terminal = {"schema": 1, "request": request,
                "request_digest": guarded.canonical_recovery_request_digest(request),
                "result_digest": "b" * 64, "recovered_epoch": 1.0}
            for epoch in (float("nan"), float("inf")):
                with self.subTest(epoch=epoch), self.assertRaises(ValueError):
                    guarded._validated_recovery_terminal({**terminal, "recovered_epoch": epoch}, request)
        finally: shutil.rmtree(root, ignore_errors=True)

    def test_recovery_terminal_recovered_epoch_is_exact_positive_finite_number_excluding_bool(self):
        root = Path(tempfile.mkdtemp(prefix="slice2-recovery-epoch-types-"))
        try:
            arguments = self._arguments(root); attempt_id = "a" * 32
            request = guarded._full_recovery_request(arguments, "rollback-proof", attempt_id,
                f"/run/matrix-priority-1-3/attempts/{attempt_id}.json")
            terminal = {"schema": 1, "request": request,
                "request_digest": guarded.canonical_recovery_request_digest(request),
                "result_digest": "b" * 64, "recovered_epoch": 1}
            for epoch in (float("-inf"), True, 0, 0.0, -1, -1.0):
                with self.subTest(epoch=epoch), self.assertRaises(ValueError):
                    guarded._validated_recovery_terminal({**terminal, "recovered_epoch": epoch}, request)
            for epoch in (1, 1.5):
                with self.subTest(valid=epoch):
                    validated = guarded._validated_recovery_terminal(
                        {**terminal, "recovered_epoch": epoch}, request,
                    )
                    self.assertEqual(validated["recovered_epoch"], epoch)
                    self.assertEqual(validated["result_digest"], terminal["result_digest"])
                    self.assertEqual(
                        validated["request_digest"],
                        guarded.canonical_recovery_request_digest(validated["request"]),
                    )
        finally: shutil.rmtree(root, ignore_errors=True)

    def test_recovery_terminal_wire_reconstruction_rejects_non_finite_epoch_before_mutation(self):
        for recovered_epoch in (float("nan"), float("inf")):
            with self.subTest(recovered_epoch=recovered_epoch):
                root = Path(tempfile.mkdtemp(prefix="slice2-recovery-epoch-wire-"))
                try:
                    arguments = self._arguments(root, resume=True); executor = self.Executor(arguments)
                    authority, initial = self._seed_initial_accepted(arguments, executor)
                    phase = "rollback-proof"
                    for source, target in (("READY", "GUARD_ARMING"), ("GUARD_ARMING", "GUARDED"),
                        ("GUARDED", "MUTATION_RUNNING"), ("MUTATION_RUNNING", "MUTATION_DONE"),
                        ("MUTATION_DONE", "ACCEPTANCE_RUNNING"),
                        ("ACCEPTANCE_RUNNING", "CANDIDATE_DURABLE"),
                        ("CANDIDATE_DURABLE", "PENDING_BOUND"),
                        ("PENDING_BOUND", "SEALED_RECOVERING")):
                        authority = guarded._active_transition(
                            arguments, executor, authority, phase, source, target, lambda: 1000.0)
                    attempt = authority.record["attempts"][1]; evidence_digest = "9" * 64
                    request = guarded._full_recovery_request(
                        arguments, phase, attempt["attempt_id"], attempt["registry"])
                    request.update(evidence_digest=evidence_digest, pending_note_id="102")
                    recovered = executor.control_remote("remote-recover", request, operation_timeout=60)
                    executor.recoveries[attempt["attempt_id"]]["recovered_epoch"] = recovered_epoch
                    authority = guarded._active_terminal(arguments, executor, authority, phase, "recovery",
                        "SEALED_RECOVERING", "RECOVERED_UNVERIFIED", recovered.digest, lambda: 1000.0)
                    rollback = {"note_id": "102", "state": "PENDING", "record": {
                        "boundary": arguments.boundary, "phase": phase, "attempt_id": attempt["attempt_id"],
                        "stage": arguments.stage, "bundle": arguments.bundle,
                        "evidence_digest": evidence_digest}}
                    shutil.rmtree(arguments.evidence_dir)
                    Path(arguments.evidence_dir).mkdir(mode=0o700)
                    fresh = self.Executor(arguments, executor.commits, executor.recoveries)
                    result = guarded.run_release(
                        arguments, executor=fresh, acceptance_runner=self._accept,
                        evidence_store=self.Notes([initial, rollback]), clock=lambda: 100.0,
                        wall_clock=lambda: 1000.0)
                    self.assertEqual(result, 2, fresh.controls)
                    self.assertEqual(fresh.mutations, [])
                finally:
                    shutil.rmtree(root, ignore_errors=True)

    def test_recovered_unverified_with_local_loss_republishes_rollback_and_continues_final_deploy(self):
        root = Path(tempfile.mkdtemp(prefix="slice2-fresh-recovered-"))
        try:
            arguments = self._arguments(root, resume=True); executor = self.Executor(arguments)
            authority, initial = self._seed_initial_accepted(arguments, executor)
            phase = "rollback-proof"
            for source, target in (("READY", "GUARD_ARMING"), ("GUARD_ARMING", "GUARDED"),
                ("GUARDED", "MUTATION_RUNNING"), ("MUTATION_RUNNING", "MUTATION_DONE"),
                ("MUTATION_DONE", "ACCEPTANCE_RUNNING"), ("ACCEPTANCE_RUNNING", "CANDIDATE_DURABLE"),
                ("CANDIDATE_DURABLE", "PENDING_BOUND"), ("PENDING_BOUND", "SEALED_RECOVERING")):
                authority = guarded._active_transition(arguments, executor, authority, phase, source, target,
                                                        lambda: 1000.0)
            attempt = authority.record["attempts"][1]; evidence_digest = "9" * 64
            request = guarded._full_recovery_request(arguments, phase, attempt["attempt_id"], attempt["registry"])
            request.update(evidence_digest=evidence_digest, pending_note_id="102")
            recovered = executor.control_remote("remote-recover", request, operation_timeout=60)
            authority = guarded._active_terminal(arguments, executor, authority, phase, "recovery",
                "SEALED_RECOVERING", "RECOVERED_UNVERIFIED", recovered.digest, lambda: 1000.0)
            rollback = {"note_id": "102", "state": "PENDING", "record": {
                "boundary": arguments.boundary, "phase": phase, "attempt_id": attempt["attempt_id"],
                "stage": arguments.stage, "bundle": arguments.bundle, "evidence_digest": evidence_digest}}
            shutil.rmtree(arguments.evidence_dir); Path(arguments.evidence_dir).mkdir(mode=0o700)
            fresh = self.Executor(arguments, executor.commits, executor.recoveries)
            notes = self.Notes([initial, rollback])
            result = guarded.run_release(arguments, executor=fresh, acceptance_runner=self._accept,
                evidence_store=notes, clock=lambda: 100.0, wall_clock=lambda: 1000.0)
            self.assertEqual(result, 0, fresh.controls)
            self.assertNotIn((attempt["attempt_id"], "rollback"), fresh.mutations)
            self.assertEqual([operation for _, operation in fresh.mutations], ["deploy"])
            rebuilt = next(item for item in notes.publications if item["note_id"] == "102")
            self.assertEqual(rebuilt["state"], "ROLLED_BACK")
            self.assertEqual(rebuilt["record"]["remote_record_digest"], recovered.digest)
            self.assertEqual(rebuilt["record"]["terminal_kind"], "recovery")
        finally: shutil.rmtree(root, ignore_errors=True)

    def test_sealed_committing_terminal_with_local_loss_reconstructs_without_repeating_mutation(self):
        root = Path(tempfile.mkdtemp(prefix="slice2-fresh-sealed-commit-"))
        try:
            arguments = self._arguments(root, resume=True); executor = self.Executor(arguments)
            identity, prepared = guarded._prepare_active_transaction(arguments, executor, created_epoch=1000)
            authority = guarded._claim_active_transaction(arguments, executor, identity, prepared,
                                                           coordinator_epoch=1000)
            phase = "initial-deploy"
            for source, target in (("READY", "GUARD_ARMING"), ("GUARD_ARMING", "GUARDED"),
                ("GUARDED", "MUTATION_RUNNING"), ("MUTATION_RUNNING", "MUTATION_DONE"),
                ("MUTATION_DONE", "ACCEPTANCE_RUNNING"), ("ACCEPTANCE_RUNNING", "CANDIDATE_DURABLE"),
                ("CANDIDATE_DURABLE", "PENDING_BOUND"), ("PENDING_BOUND", "SEALED_COMMITTING")):
                authority = guarded._active_transition(arguments, executor, authority, phase, source, target,
                                                        lambda: 1000.0)
            attempt = authority.record["attempts"][0]; digest = "8" * 64
            operation = __import__("hashlib").sha256(f"{attempt['attempt_id']}\0commit".encode()).hexdigest()[:32]
            request = {"schema": 1, "boundary": arguments.boundary, "phase": phase,
                "attempt_id": attempt["attempt_id"], "operation_id": operation,
                "registry": attempt["registry"], "evidence_digest": digest, "pending_note_id": "101",
                "guard_unit": f"matrix-p13-recover-{attempt['attempt_id']}.service",
                "success_record": f"/run/matrix-priority-1-3/{attempt['attempt_id']}/success.json",
                "control_unit": f"matrix-p13-commit-{attempt['attempt_id']}.service",
                "control_cgroup": f"/system.slice/matrix-p13-commit-{attempt['attempt_id']}.service"}
            committed = executor.control_remote("remote-commit", request, operation_timeout=60)
            publication = {"note_id": "101", "state": "PENDING", "record": {
                "boundary": arguments.boundary, "phase": phase, "attempt_id": attempt["attempt_id"],
                "stage": arguments.stage, "bundle": arguments.bundle, "evidence_digest": digest}}
            fresh = self.Executor(arguments, executor.commits, executor.recoveries)
            result = guarded.run_release(arguments, executor=fresh, acceptance_runner=self._accept,
                evidence_store=self.Notes([publication]), clock=lambda: 100.0, wall_clock=lambda: 1000.0)
            self.assertEqual(result, 0, fresh.controls)
            self.assertNotIn((attempt["attempt_id"], "deploy"), fresh.mutations)
            terminal_calls = [r for k, r in fresh.controls
                              if k == "remote-transaction-terminal"
                              and r["phase"] == "initial-deploy"]
            self.assertEqual(len(terminal_calls), 1)
            self.assertEqual(terminal_calls[0]["result_digest"], committed.digest)
        finally: shutil.rmtree(root, ignore_errors=True)

    def test_exact_final_with_complete_local_observer_loss_is_adopted_without_verification_or_release_mutation(self):
        root = Path(tempfile.mkdtemp(prefix="slice2-fresh-external-final-"))
        try:
            arguments = self._arguments(root, resume=False)
            seeder = self.Executor(arguments); notes = self.Notes()
            self.assertEqual(guarded.run_release(
                arguments, executor=seeder, acceptance_runner=self._accept,
                evidence_store=notes, clock=lambda: 100.0, wall_clock=lambda: 1000.0,
            ), 0)
            external_final = copy.deepcopy(next(
                item for item in notes.publications if item["state"] == "FINAL"
            ))
            shutil.rmtree(arguments.evidence_dir); Path(arguments.evidence_dir).mkdir(mode=0o700)
            arguments.resume = True
            fresh = self.Executor(arguments, seeder.commits, seeder.recoveries)
            resumed_notes = self.Notes(notes.publications)
            verification_calls = []

            result = guarded.run_release(
                arguments, executor=fresh,
                acceptance_runner=lambda *call: verification_calls.append(call) or 0,
                evidence_store=resumed_notes, clock=lambda: 100.0, wall_clock=lambda: 1000.0,
            )

            self.assertEqual(result, 0, fresh.controls)
            self.assertEqual(fresh.mutations, [])
            self.assertEqual(verification_calls, [])
            self.assertEqual(resumed_notes.finals, [])
            journal = json.loads((Path(arguments.evidence_dir) / guarded._RECONCILIATION_FILE).read_text())
            self.assertEqual(journal["final"]["state"], "AUTHORITATIVE_FINAL")
            self.assertEqual(journal["final"]["record"]["verification_digest"],
                             external_final["record"]["verification_digest"])
        finally: shutil.rmtree(root, ignore_errors=True)

    def test_three_attempts_accepted_with_local_loss_reconstructs_and_returns_without_release_mutation(self):
        root = Path(tempfile.mkdtemp(prefix="slice2-fresh-three-accepted-"))
        try:
            arguments = self._arguments(root, resume=False)
            seeder = self.Executor(arguments); notes = self.Notes()
            seeded = guarded.run_release(arguments, executor=seeder, acceptance_runner=self._accept,
                evidence_store=notes, clock=lambda: 100.0, wall_clock=lambda: 1000.0)
            self.assertEqual(seeded, 0)
            shutil.rmtree(arguments.evidence_dir); Path(arguments.evidence_dir).mkdir(mode=0o700)
            arguments.resume = True
            fresh = self.Executor(arguments, seeder.commits, seeder.recoveries)
            resumed_notes = self.Notes(notes.publications)
            result = guarded.run_release(arguments, executor=fresh, acceptance_runner=self._accept,
                evidence_store=resumed_notes, clock=lambda: 100.0, wall_clock=lambda: 1000.0)
            self.assertEqual(result, 0, fresh.controls)
            self.assertEqual(fresh.mutations, [])
            self.assertEqual(resumed_notes.finals, [])
            self.assertEqual(resumed_notes.publications, notes.publications)
            self.assertEqual(sum(item["state"] == "FINAL" for item in resumed_notes.publications), 1)
        finally: shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
