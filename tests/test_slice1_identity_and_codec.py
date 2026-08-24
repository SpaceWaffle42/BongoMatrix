import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from deploy import guarded_priority_1_3_release as guarded


class Slice1IdentityAndCodecTests(unittest.TestCase):
    def _run_arguments(self, root: Path, **changes: object) -> argparse.Namespace:
        for directory in (root / "stage", root / "bundle", root / "destination", root / "evidence"):
            directory.mkdir(parents=True, exist_ok=True)
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
            "transaction_registry_root": "/run/matrix-priority-1-3/transactions",
            "release_stamp": "2026-08-23T02:51:53Z",
            "release_manifest_digest": "c" * 64, "backup_manifest_digest": "d" * 64,
            "release_spec_digest": "e" * 64,
        }
        values.update(changes)
        return argparse.Namespace(**values)

    def test_active_run_release_preparation_uses_only_canonical_remote_identity_topology(self):
        roots = [Path(tempfile.mkdtemp(prefix="slice1-active-a-"))]
        self.assertFalse(hasattr(guarded, "_release_identity"))
        try:
            observations: list[dict[str, object]] = []

            class Executor:
                def __init__(self, evidence: Path):
                    self.evidence = evidence
                    self.controls: list[tuple[str, dict[str, object]]] = []
                    self.responses: list[tuple[str, dict[str, object]]] = []
                    self.operations: list[tuple[str, str, tuple[str, ...]]] = []

                def control_remote(self, kind, request, *, operation_timeout):
                    del operation_timeout
                    immutable = copy.deepcopy(request)
                    self.controls.append((kind, immutable))
                    if kind in guarded._TRANSACTION_HELPERS:
                        result = guarded.dispatch_privileged(kind, request)
                        if type(result) is not guarded.ControlResult or result.returncode != 0:
                            raise AssertionError(f"canonical authority failed for {kind}")
                        record = guarded.validate_transaction_record(
                            copy.deepcopy(result.terminal), request["release_key"],
                        )
                        if kind == "remote-transaction-create-or-query":
                            first_attempt = record["attempts"][0]
                            (self.evidence / f"{first_attempt['attempt_id']}-candidate.json").write_text("{}\n")
                        self.responses.append((kind, copy.deepcopy(record)))
                        return guarded.ControlResult(0, record["record_digest"], copy.deepcopy(record))
                    if kind == "remote-commit" and request.get("query") is True:
                        return guarded.ControlResult(3, "")
                    raise AssertionError(kind)

                def run_remote(self, attempt_id, operation_id, argv, *, registry, run_as, operation_timeout):
                    del run_as, operation_timeout
                    self.operations.append((attempt_id, operation_id, tuple(argv)))
                    return guarded.OperationResult(2, "", "")

            variants = [
                {}, {},
                {"release_stamp": "2026-08-23T02:52:00Z"},
                {"release_manifest_digest": "1" * 64},
                {"backup_manifest_digest": "2" * 64},
                {"release_spec_digest": "3" * 64},
            ]
            for index, changes in enumerate(variants):
                root = roots[0] if index < 2 else Path(tempfile.mkdtemp(prefix="slice1-active-distinct-"))
                if index >= 2:
                    roots.append(root)
                registry_root = root / "transactions"
                registry_root.mkdir(exist_ok=True)
                os.chmod(registry_root, 0o700)
                arguments = self._run_arguments(
                    root, transaction_registry_root=str(registry_root), **changes,
                )
                executor = Executor(root / "evidence")
                result = guarded.run_release(
                    arguments, executor=executor, acceptance_runner=lambda *_: 2,
                    evidence_store=object(), clock=lambda: 100.0, wall_clock=lambda: 1000.0,
                )
                self.assertNotEqual(result, 0)
                create_kind, create = executor.controls[0]
                self.assertEqual(create_kind, "remote-transaction-create-or-query")
                record = executor.responses[0][1]
                first = record["attempts"][0]
                query = next(request for kind, request in executor.controls if kind == "remote-commit")
                self.assertEqual(query["attempt_id"], first["attempt_id"])
                self.assertEqual(query["registry"], first["registry"])
                self.assertTrue((root / "evidence" / f"{first['attempt_id']}-candidate.json").is_file())
                self.assertEqual(executor.operations[0][0], first["attempt_id"])
                payload = executor.operations[0][2][executor.operations[0][2].index("--request-b64") + 1]
                recovery = guarded.decode_privileged_request("remote-recover", payload)
                self.assertEqual(recovery["registry"], first["registry"])
                claimed_record = next(response for kind, response in executor.responses
                                      if kind == "remote-transaction-claim")
                observations.append(claimed_record)

            for field in ("release_key", "transaction_identity", "transaction_id"):
                self.assertEqual(observations[0][field], observations[1][field])
            self.assertEqual(
                [(item["phase"], item["operation"], item["attempt_id"], item["registry"])
                 for item in observations[0]["attempts"]],
                [(item["phase"], item["operation"], item["attempt_id"], item["registry"])
                 for item in observations[1]["attempts"]],
            )
            self.assertEqual(observations[1]["attempts"][0]["state"], "GUARD_ARMING")
            baseline_key = observations[0]["release_key"]
            baseline_tx = observations[0]["transaction_identity"]
            for record in observations[2:]:
                self.assertNotEqual(record["release_key"], baseline_key)
                self.assertNotEqual(record["transaction_identity"], baseline_tx)
                self.assertNotEqual(record["transaction_id"], observations[0]["transaction_id"])
                self.assertTrue(set(a["attempt_id"] for a in record["attempts"]).isdisjoint(
                    a["attempt_id"] for a in observations[0]["attempts"]
                ))
        finally:
            for root in roots:
                shutil.rmtree(root, ignore_errors=True)


    def test_active_preparation_rejects_redigested_noncanonical_current_phase_topology(self):
        root = Path(tempfile.mkdtemp(prefix="slice1-active-topology-"))
        try:
            registry_root = root / "transactions"
            registry_root.mkdir()
            os.chmod(registry_root, 0o700)
            arguments = self._run_arguments(
                root, transaction_registry_root=str(registry_root), watchdog_seconds=7200,
            )
            self_test = self

            class AdversarialExecutor:
                def __init__(self):
                    self.record: dict[str, object] | None = None
                    self.kinds: list[str] = []

                def control_remote(self, kind, request, *, operation_timeout):
                    del operation_timeout
                    self.kinds.append(kind)
                    if kind == "remote-transaction-create-or-query":
                        result = guarded.dispatch_privileged(kind, request)
                        if type(result) is not guarded.ControlResult or result.returncode != 0:
                            raise AssertionError("authority failed to create canonical transaction")
                        record = guarded.validate_transaction_record(
                            copy.deepcopy(result.terminal), request["release_key"],
                        )
                        record["state"] = "TX_OPEN"
                        record["current_phase_index"] = 1
                        record["claim"] = {
                            "claimant_id": "1" * 32, "request_digest": "2" * 64,
                            "claimed_epoch": 1000,
                        }
                        record["attempts"][0]["state"] = "BLOCKED"
                        record["attempts"][1]["state"] = "READY"
                        record["attempts"][2]["state"] = "BLOCKED"
                        record["record_digest"] = guarded.transaction_record_digest(record)
                        with self_test.assertRaisesRegex(
                            ValueError, "completed transaction phase topology is invalid",
                        ):
                            guarded.validate_transaction_record(record, request["release_key"])
                        self.record = record
                        return guarded.ControlResult(0, record["record_digest"], copy.deepcopy(record))
                    if kind == "remote-transaction-inspect":
                        return guarded.ControlResult(0, self.record["record_digest"], copy.deepcopy(self.record))
                    raise AssertionError(kind)

            executor = AdversarialExecutor()
            with self.assertRaisesRegex(ValueError, "remote transaction .*invalid"):
                guarded._prepare_active_transaction(arguments, executor, created_epoch=1000)
            self.assertEqual(executor.kinds, ["remote-transaction-create-or-query"])
        finally:
            shutil.rmtree(root, ignore_errors=True)


    def test_active_preparation_rejects_coherently_forged_attempt_id(self):
        root = Path(tempfile.mkdtemp(prefix="slice1-active-forged-attempt-"))
        try:
            registry_root = root / "transactions"
            registry_root.mkdir()
            os.chmod(registry_root, 0o700)
            arguments = self._run_arguments(
                root, transaction_registry_root=str(registry_root), watchdog_seconds=7200,
            )

            class AuthorityExecutor:
                def __init__(self, *, forge_attempt_id: bool):
                    self.forge_attempt_id = forge_attempt_id
                    self.record: dict[str, object] | None = None

                def control_remote(self, kind, request, *, operation_timeout):
                    del operation_timeout
                    if kind == "remote-transaction-create-or-query":
                        result = guarded.dispatch_privileged(kind, request)
                        if type(result) is not guarded.ControlResult or result.returncode != 0:
                            raise AssertionError("authority failed to create canonical transaction")
                        record = copy.deepcopy(result.terminal)
                        identity = guarded.canonical_release_identity(request["release_document"])
                        for index, attempt in enumerate(record["attempts"]):
                            phase, operation = guarded._TRANSACTION_TOPOLOGY[index]
                            expected = guarded._canonical_attempt_id(
                                identity.transaction_identity, record["transaction_id"],
                                phase, operation, index,
                            )
                            if attempt["attempt_id"] != expected:
                                raise AssertionError("authority emitted a noncanonical attempt ID")
                        if self.forge_attempt_id:
                            attempt = record["attempts"][0]
                            forged_id = "f" * 32 if attempt["attempt_id"] != "f" * 32 else "0" * 32
                            attempt["attempt_id"] = forged_id
                            attempt["attempt_identity"] = guarded._domain_digest("attempt-identity/v1", {
                                "transaction_identity": identity.transaction_identity,
                                "transaction_id": record["transaction_id"],
                                "phase": attempt["phase"], "operation": attempt["operation"],
                                "index": 0, "attempt_id": forged_id,
                            })
                            attempt["registry"] = (
                                f"/run/matrix-priority-1-3/attempts/{forged_id}.json"
                            )
                            record["record_digest"] = guarded.transaction_record_digest(record)
                        self.record = record
                        return guarded.ControlResult(0, record["record_digest"], copy.deepcopy(record))
                    if kind == "remote-transaction-inspect":
                        return guarded.ControlResult(0, self.record["record_digest"], copy.deepcopy(self.record))
                    raise AssertionError(kind)

            with self.subTest(case="canonical authority record"):
                identity, legal = guarded._prepare_active_transaction(
                    arguments, AuthorityExecutor(forge_attempt_id=False), created_epoch=1000,
                )
                first_phase, first_operation = guarded._TRANSACTION_TOPOLOGY[0]
                self.assertEqual(
                    legal["attempts"][0]["attempt_id"],
                    guarded._canonical_attempt_id(
                        identity.transaction_identity, legal["transaction_id"],
                        first_phase, first_operation, 0,
                    ),
                )

            with self.subTest(case="coherently forged attempt ID and backlinks"):
                with self.assertRaisesRegex(ValueError, "remote transaction attempt identity is invalid"):
                    guarded._prepare_active_transaction(
                        arguments, AuthorityExecutor(forge_attempt_id=True), created_epoch=1000,
                    )
        finally:
            shutil.rmtree(root, ignore_errors=True)


    def _active_canonical_record(
        self, arguments: argparse.Namespace, request: dict[str, object],
    ) -> dict[str, object]:
        result = guarded.dispatch_privileged("remote-transaction-create-or-query", request)
        self.assertIsInstance(result, guarded.ControlResult)
        return copy.deepcopy(result.terminal)

    def _active_rejection(
        self, arguments: argparse.Namespace, executor: object,
    ) -> ValueError | None:
        try:
            guarded._prepare_active_transaction(arguments, executor, created_epoch=1000)
        except ValueError as error:
            return error
        return None

    def test_active_preparation_rejects_create_envelope_digest_not_bound_to_terminal_record(self):
        root = Path(tempfile.mkdtemp(prefix="slice1-active-create-digest-"))
        try:
            registry_root = root / "transactions"
            registry_root.mkdir(); os.chmod(registry_root, 0o700)
            arguments = self._run_arguments(root, transaction_registry_root=str(registry_root))

            class Executor:
                def __init__(self):
                    self.record = None; self.kinds = []

                def control_remote(inner, kind, request, *, operation_timeout):
                    del operation_timeout; inner.kinds.append(kind)
                    if kind == "remote-transaction-create-or-query":
                        inner.record = self._active_canonical_record(arguments, request)
                        wrong = "f" * 64 if inner.record["record_digest"] != "f" * 64 else "0" * 64
                        return guarded.ControlResult(0, wrong, copy.deepcopy(inner.record))
                    if kind == "remote-transaction-inspect":
                        return guarded.ControlResult(
                            0, inner.record["record_digest"], copy.deepcopy(inner.record),
                        )
                    raise AssertionError(kind)

            executor = Executor()
            error = self._active_rejection(arguments, executor)
            self.assertEqual(executor.kinds, ["remote-transaction-create-or-query"])
            self.assertIsNotNone(error, "active seam accepted an unbound create envelope digest")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_active_preparation_rejects_inspect_envelope_digest_not_bound_to_terminal_record(self):
        root = Path(tempfile.mkdtemp(prefix="slice1-active-inspect-digest-"))
        try:
            registry_root = root / "transactions"
            registry_root.mkdir(); os.chmod(registry_root, 0o700)
            arguments = self._run_arguments(root, transaction_registry_root=str(registry_root))

            class Executor:
                def __init__(self):
                    self.record = None; self.kinds = []

                def control_remote(inner, kind, request, *, operation_timeout):
                    del operation_timeout; inner.kinds.append(kind)
                    if kind == "remote-transaction-create-or-query":
                        inner.record = self._active_canonical_record(arguments, request)
                        return guarded.ControlResult(
                            0, inner.record["record_digest"], copy.deepcopy(inner.record),
                        )
                    if kind == "remote-transaction-inspect":
                        wrong = "f" * 64 if inner.record["record_digest"] != "f" * 64 else "0" * 64
                        return guarded.ControlResult(0, wrong, copy.deepcopy(inner.record))
                    raise AssertionError(kind)

            executor = Executor()
            error = self._active_rejection(arguments, executor)
            self.assertEqual(executor.kinds, [
                "remote-transaction-create-or-query", "remote-transaction-inspect",
            ])
            self.assertIsNotNone(error, "active seam accepted an unbound inspect envelope digest")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_active_preparation_detaches_and_rejects_canonical_create_inspect_disagreement(self):
        root = Path(tempfile.mkdtemp(prefix="slice1-active-detached-"))
        try:
            registry_root = root / "transactions"
            registry_root.mkdir(); os.chmod(registry_root, 0o700)
            arguments = self._run_arguments(root, transaction_registry_root=str(registry_root))

            class Executor:
                def __init__(self):
                    self.shared = None; self.created = None; self.inspected = None; self.kinds = []

                def control_remote(inner, kind, request, *, operation_timeout):
                    del operation_timeout; inner.kinds.append(kind)
                    if kind == "remote-transaction-create-or-query":
                        inner.shared = self._active_canonical_record(arguments, request)
                        inner.created = copy.deepcopy(inner.shared)
                        return guarded.ControlResult(0, inner.shared["record_digest"], inner.shared)
                    if kind == "remote-transaction-inspect":
                        inner.shared["claim"] = {
                            "claimant_id": "1" * 32, "request_digest": "2" * 64,
                            "claimed_epoch": 1000,
                        }
                        inner.shared["record_digest"] = guarded.transaction_record_digest(inner.shared)
                        guarded.validate_transaction_record(inner.shared, request["release_key"])
                        inner.inspected = copy.deepcopy(inner.shared)
                        return guarded.ControlResult(
                            0, inner.shared["record_digest"], copy.deepcopy(inner.shared),
                        )
                    raise AssertionError(kind)

            executor = Executor()
            error = self._active_rejection(arguments, executor)
            self.assertEqual(executor.kinds, [
                "remote-transaction-create-or-query", "remote-transaction-inspect",
            ])
            self.assertNotEqual(executor.created, executor.inspected)
            self.assertIsNotNone(error, "active seam compared aliased records and accepted disagreement")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_active_preparation_rejects_missing_runtime_typed_envelope_digest(self):
        root = Path(tempfile.mkdtemp(prefix="slice1-active-missing-digest-"))
        try:
            registry_root = root / "transactions"
            registry_root.mkdir(); os.chmod(registry_root, 0o700)
            arguments = self._run_arguments(root, transaction_registry_root=str(registry_root))

            class Executor:
                def __init__(self):
                    self.record = None; self.kinds = []

                def control_remote(inner, kind, request, *, operation_timeout):
                    del operation_timeout; inner.kinds.append(kind)
                    if kind == "remote-transaction-create-or-query":
                        inner.record = self._active_canonical_record(arguments, request)
                        return guarded.ControlResult(0, None, copy.deepcopy(inner.record))
                    if kind == "remote-transaction-inspect":
                        return guarded.ControlResult(
                            0, inner.record["record_digest"], copy.deepcopy(inner.record),
                        )
                    raise AssertionError(kind)

            executor = Executor()
            error = self._active_rejection(arguments, executor)
            self.assertEqual(executor.kinds, ["remote-transaction-create-or-query"])
            self.assertIsNotNone(error, "active seam accepted a missing runtime envelope digest")
        finally:
            shutil.rmtree(root, ignore_errors=True)


    def _transaction_fixture(self, root: Path) -> tuple[dict[str, object], Path, dict[str, object]]:
        os.chmod(root, 0o700)
        document = {
            "schema": 1, "transaction_version": 1, "boundary": "b1",
            "expected_sha": "a" * 40, "expected_version": "0.3.58-preview",
            "previous_sha": "b" * 40, "previous_version": "0.3.57-preview",
            "release_stamp": "2026-08-23T02:51:53Z",
            "release_manifest_digest": "c" * 64, "backup_manifest_digest": "d" * 64,
            "release_spec_digest": "e" * 64,
            "destination_identity": {"root": "matrix-production", "service": "matrix-web.service"},
            "gitlab": {"project": "2", "mr": "73"},
        }
        identity = guarded.canonical_release_identity(document)
        request = {
            "schema": 1, "registry_root": str(root), "release_key": identity.release_key,
            "release_document": document,
            "execution_contract": {"schema": 1, "stage": "/trusted/stage", "bundle": "/trusted/bundle",
                                   "destination_root": "/trusted/destination", "service": "matrix-web.service"},
            "created_epoch": 1000, "original_deadline_epoch": 8200,
        }
        result = guarded.dispatch_privileged("remote-transaction-create-or-query", request)
        self.assertIsInstance(result, guarded.ControlResult)
        record = copy.deepcopy(result.terminal)
        self.assertIsInstance(record, dict)
        return request, root / f"{identity.release_key}.json", record

    def test_durable_transaction_codec_rejects_duplicate_key_authority_bytes(self):
        root = Path(tempfile.mkdtemp(prefix="slice1-duplicate-record-json-"))
        try:
            request, record_path, record = self._transaction_fixture(root)
            canonical = guarded._canonical_json_bytes(record) + b"\n"
            duplicate = canonical.replace(b'"schema":1', b'"schema":false,"schema":1', 1)
            self.assertNotEqual(duplicate, canonical)
            record_path.write_bytes(duplicate)
            record_path.chmod(0o600)
            inspect = {"schema": 1, "registry_root": str(root),
                       "release_key": request["release_key"],
                       "transaction_id": record["transaction_id"]}
            before = record_path.read_bytes()
            with self.assertRaisesRegex(ValueError, "malformed|duplicate|canonical"):
                guarded.dispatch_privileged("remote-transaction-inspect", inspect)
            self.assertEqual(record_path.read_bytes(), before)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_durable_transaction_codec_rejects_noncanonical_authority_bytes(self):
        root = Path(tempfile.mkdtemp(prefix="slice1-noncanonical-record-json-"))
        try:
            request, record_path, record = self._transaction_fixture(root)
            noncanonical = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            self.assertNotEqual(noncanonical, guarded._canonical_json_bytes(record) + b"\n")
            record_path.write_bytes(noncanonical)
            record_path.chmod(0o600)
            inspect = {"schema": 1, "registry_root": str(root),
                       "release_key": request["release_key"],
                       "transaction_id": record["transaction_id"]}
            before = record_path.read_bytes()
            with self.assertRaisesRegex(ValueError, "canonical"):
                guarded.dispatch_privileged("remote-transaction-inspect", inspect)
            self.assertEqual(record_path.read_bytes(), before)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_create_request_rejects_execution_service_mismatching_release_destination(self):
        root = Path(tempfile.mkdtemp(prefix="slice1-create-service-mismatch-"))
        try:
            request, _record_path, _record = self._transaction_fixture(root)
            mismatched = copy.deepcopy(request)
            mismatched["execution_contract"]["service"] = "matrix-worker.service"
            with self.assertRaisesRegex(ValueError, "service.*destination"):
                guarded.validate_privileged_request("remote-transaction-create-or-query", mismatched)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_fresh_process_inspect_rejects_redigested_execution_service_mismatch(self):
        root = Path(tempfile.mkdtemp(prefix="slice1-durable-service-mismatch-"))
        try:
            request, record_path, record = self._transaction_fixture(root)
            record["execution_contract"]["service"] = "matrix-worker.service"
            record["record_digest"] = guarded.transaction_record_digest(record)
            record_path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            inspect = {
                "schema": 1, "registry_root": str(root), "release_key": request["release_key"],
                "transaction_id": record["transaction_id"],
            }
            script = Path(__file__).parents[1] / "deploy/guarded_priority_1_3_release.py"
            python = Path(__file__).parents[1] / ".venv/bin/python"
            payload = guarded.encode_privileged_request("remote-transaction-inspect", inspect)
            completed = subprocess.run(
                [str(python), "-I", "-B", str(script), "remote-transaction-inspect",
                 "--request-b64", payload], check=False, text=True, capture_output=True,
            )
            self.assertNotEqual(completed.returncode, 0,
                                "fresh authority accepted coherently redigested service mismatch")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_attempt_transition_table_is_exact_and_illegal_pairs_fail_closed(self):
        expected = {
            "BLOCKED": frozenset(("READY",)),
            "READY": frozenset(("GUARD_ARMING",)),
            "GUARD_ARMING": frozenset(("GUARDED",)),
            "GUARDED": frozenset(("MUTATION_RUNNING", "SEALED_RECOVERING")),
            "MUTATION_RUNNING": frozenset(("MUTATION_DONE", "SEALED_RECOVERING")),
            "MUTATION_DONE": frozenset(("ACCEPTANCE_RUNNING", "SEALED_RECOVERING")),
            "ACCEPTANCE_RUNNING": frozenset(("CANDIDATE_DURABLE", "SEALED_RECOVERING")),
            "CANDIDATE_DURABLE": frozenset(("PENDING_BOUND", "SEALED_RECOVERING")),
            "PENDING_BOUND": frozenset(("SEALED_COMMITTING", "SEALED_RECOVERING")),
            "COMMITTED_UNRECONCILED": frozenset(("ACCEPTED",)),
            "RECOVERED_UNVERIFIED": frozenset(("ROLLBACK_VERIFIED",)),
        }
        self.assertEqual(guarded._ATTEMPT_TRANSITIONS, expected)
        root = Path(tempfile.mkdtemp(prefix="slice1-transition-table-"))
        try:
            request, _record_path, baseline = self._transaction_fixture(root)
            targets = {
                "GUARD_ARMING", "GUARDED", "MUTATION_RUNNING", "MUTATION_DONE",
                "ACCEPTANCE_RUNNING", "CANDIDATE_DURABLE", "PENDING_BOUND",
                "SEALED_COMMITTING", "SEALED_RECOVERING",
            }
            sources = {source for source in expected if source not in {
                "BLOCKED", "COMMITTED_UNRECONCILED", "RECOVERED_UNVERIFIED"
            }}
            for source in sorted(sources):
                for target in sorted(targets):
                    value = copy.deepcopy(baseline)
                    value["claim"] = {"claimant_id": "1" * 32, "request_digest": "2" * 64,
                                      "claimed_epoch": 1001}
                    attempt = value["attempts"][0]
                    attempt["state"] = target
                    attempt["last_transition"] = {
                        "from_state": source, "to_state": target,
                        "request_digest": "3" * 64, "transition_epoch": 1002,
                    }
                    value["record_digest"] = guarded.transaction_record_digest(value)
                    admissible = target in expected[source]
                    with self.subTest(source=source, target=target):
                        if admissible:
                            self.assertEqual(
                                guarded.validate_transaction_record(value, request["release_key"]), value
                            )
                        else:
                            with self.assertRaises(ValueError):
                                guarded.validate_transaction_record(value, request["release_key"])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_fresh_process_inspect_rejects_every_coherently_redigested_schema_corruption(self):
        root = Path(tempfile.mkdtemp(prefix="slice1-corrupt-corpus-"))
        try:
            request, record_path, baseline = self._transaction_fixture(root)
            inspect = {
                "schema": 1, "registry_root": str(root), "release_key": request["release_key"],
                "transaction_id": baseline["transaction_id"],
            }
            script = Path(__file__).parents[1] / "deploy/guarded_priority_1_3_release.py"
            python = Path(__file__).parents[1] / ".venv/bin/python"

            def progressed() -> dict[str, object]:
                value = copy.deepcopy(baseline)
                value["claim"] = {"claimant_id": "1" * 32, "request_digest": "2" * 64,
                                  "claimed_epoch": 1001}
                attempt = value["attempts"][0]
                attempt["state"] = "GUARD_ARMING"
                attempt["last_transition"] = {"from_state": "READY", "to_state": "GUARD_ARMING",
                                                "request_digest": "3" * 64, "transition_epoch": 1002}
                return value

            cases: dict[str, object] = {}
            value = copy.deepcopy(baseline); value["state"] = "UNKNOWN"; cases["unknown state"] = value
            value = progressed(); value["claim"] = None; cases["illegal claim/state combination"] = value
            value = copy.deepcopy(baseline); value["final"] = {"state": "DONE"}; cases["illegal final combination"] = value
            value = progressed(); value["attempts"][0]["last_transition"]["from_state"] = "BLOCKED"; cases["invalid transition metadata"] = value
            value = copy.deepcopy(baseline); value["attempts"][0]["registry"] = "/run/matrix-priority-1-3/attempts/" + "f" * 32 + ".json"; cases["wrong attempt backlink"] = value
            value = copy.deepcopy(baseline); del value["attempts"][0]["registry"]; cases["missing attempt backlink"] = value
            value = copy.deepcopy(baseline); value["attempts"][0]["attempt_identity"] = "f" * 64; cases["forged attempt identity"] = value
            value = copy.deepcopy(baseline); forged = value["attempts"][0]; forged["attempt_id"] = "f" * 32
            forged["registry"] = f"/run/matrix-priority-1-3/attempts/{forged['attempt_id']}.json"
            forged["attempt_identity"] = guarded._domain_digest("attempt-identity/v1", {
                "transaction_identity": value["transaction_identity"], "transaction_id": value["transaction_id"],
                "phase": forged["phase"], "operation": forged["operation"], "index": 0,
                "attempt_id": forged["attempt_id"],
            }); cases["coherently forged attempt ID and backlinks"] = value
            value = copy.deepcopy(baseline); value["attempts"][0], value["attempts"][1] = value["attempts"][1], value["attempts"][0]; cases["reordered phase topology"] = value
            value = copy.deepcopy(baseline); value["attempts"][0]["operation"] = "rollback"; cases["wrong phase topology"] = value
            value = copy.deepcopy(baseline); value["claim"] = {"claimant_id": "1" * 32, "request_digest": "2" * 64,
                                                                "claimed_epoch": 8201}; cases["owner lease inconsistency"] = value
            value = progressed(); attempt = value["attempts"][0]; attempt["state"] = "COMMITTED_UNRECONCILED"; attempt["commit"] = {
                "kind": "recovery", "from_state": "SEALED_COMMITTING", "to_state": "COMMITTED_UNRECONCILED",
                "request_digest": "4" * 64, "result_digest": "5" * 64, "terminal_epoch": 1003,
            }; attempt["last_transition"] = copy.deepcopy(attempt["commit"]); cases["terminal mismatch"] = value
            value = progressed(); value["claim"]["extra"] = True; cases["extra nested field"] = value
            value = progressed(); del value["attempts"][0]["last_transition"]["request_digest"]; cases["missing nested field"] = value
            value = copy.deepcopy(baseline); value["current_phase_index"] = 1; cases["phase index/state inconsistency"] = value
            value = copy.deepcopy(baseline); value["attempts"][1]["state"] = "READY"; cases["multiple ready attempts"] = value

            for label, corrupted in cases.items():
                with self.subTest(label=label):
                    corrupted["record_digest"] = guarded.transaction_record_digest(corrupted)
                    record_path.write_text(json.dumps(corrupted, sort_keys=True, separators=(",", ":")) + "\n")
                    payload = guarded.encode_privileged_request("remote-transaction-inspect", inspect)
                    completed = subprocess.run(
                        [str(python), "-I", "-B", str(script), "remote-transaction-inspect",
                         "--request-b64", payload], check=False, text=True, capture_output=True,
                    )
                    self.assertNotEqual(completed.returncode, 0, label)
        finally:
            shutil.rmtree(root, ignore_errors=True)


    _NON_EXACT_SCHEMA_V1_VALUES = (True, False, 1.0, "1")

    def _coherently_reidentify_record(
        self, record: dict[str, object], schema_value: object,
    ) -> tuple[str, dict[str, object]]:
        value = copy.deepcopy(record)
        value["release_document"]["schema"] = schema_value
        canonical_document = json.loads(guarded._canonical_json_bytes(
            value["release_document"],
        ).decode("ascii"))
        release_key = guarded._domain_digest("release-key/v1", canonical_document)
        transaction_identity = guarded._domain_digest("transaction-identity/v1", {
            "release_key": release_key,
            "transaction_version": canonical_document["transaction_version"],
        })
        value["release_key"] = release_key
        value["transaction_identity"] = transaction_identity
        for index, attempt in enumerate(value["attempts"]):
            phase, operation = guarded._TRANSACTION_TOPOLOGY[index]
            attempt_id = guarded._canonical_attempt_id(
                transaction_identity, value["transaction_id"], phase, operation, index,
            )
            attempt["attempt_id"] = attempt_id
            attempt["attempt_identity"] = guarded._domain_digest("attempt-identity/v1", {
                "transaction_identity": transaction_identity,
                "transaction_id": value["transaction_id"], "phase": phase,
                "operation": operation, "index": index, "attempt_id": attempt_id,
            })
            attempt["registry"] = f"/run/matrix-priority-1-3/attempts/{attempt_id}.json"
        value["record_digest"] = guarded.transaction_record_digest(value)
        return release_key, value

    def test_exact_schema_v1_canonical_release_identity_rejects_non_int_values(self):
        root = Path(tempfile.mkdtemp(prefix="slice1-exact-release-schema-"))
        try:
            request, _record_path, _record = self._transaction_fixture(root)
            for schema_value in self._NON_EXACT_SCHEMA_V1_VALUES:
                with self.subTest(schema_value=repr(schema_value)):
                    document = copy.deepcopy(request["release_document"])
                    document["schema"] = schema_value
                    with self.assertRaisesRegex(ValueError, "schema|version"):
                        guarded.canonical_release_identity(document)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_exact_schema_v1_create_request_and_execution_contract_reject_non_int_values(self):
        root = Path(tempfile.mkdtemp(prefix="slice1-exact-create-schema-"))
        try:
            request, _record_path, _record = self._transaction_fixture(root)
            for location in ("request", "execution_contract"):
                for schema_value in self._NON_EXACT_SCHEMA_V1_VALUES:
                    with self.subTest(location=location, schema_value=repr(schema_value)):
                        candidate = copy.deepcopy(request)
                        if location == "request":
                            candidate["schema"] = schema_value
                        else:
                            candidate["execution_contract"]["schema"] = schema_value
                        with self.assertRaisesRegex(ValueError, "schema|contract"):
                            guarded.validate_privileged_request(
                                "remote-transaction-create-or-query", candidate,
                            )
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_exact_schema_v1_durable_record_release_document_and_contract_reject_non_int_values(self):
        root = Path(tempfile.mkdtemp(prefix="slice1-exact-durable-schema-"))
        try:
            request, _record_path, baseline = self._transaction_fixture(root)
            for location in ("record", "release_document", "execution_contract"):
                for schema_value in self._NON_EXACT_SCHEMA_V1_VALUES:
                    with self.subTest(location=location, schema_value=repr(schema_value)):
                        if location == "release_document":
                            release_key, candidate = self._coherently_reidentify_record(
                                baseline, schema_value,
                            )
                        else:
                            release_key = request["release_key"]
                            candidate = copy.deepcopy(baseline)
                            target = candidate if location == "record" else candidate[location]
                            target["schema"] = schema_value
                            candidate["record_digest"] = guarded.transaction_record_digest(candidate)
                        with self.assertRaisesRegex(ValueError, "schema|version|contract"):
                            guarded.validate_transaction_record(candidate, release_key)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_exact_schema_v1_fresh_process_inspect_rejects_coherently_redigested_non_int_values(self):
        script = Path(__file__).parents[1] / "deploy/guarded_priority_1_3_release.py"
        python = Path(__file__).parents[1] / ".venv/bin/python"
        for location in ("record", "release_document", "execution_contract"):
            for schema_value in self._NON_EXACT_SCHEMA_V1_VALUES:
                with self.subTest(location=location, schema_value=repr(schema_value)):
                    root = Path(tempfile.mkdtemp(prefix="slice1-exact-inspect-schema-"))
                    try:
                        request, _record_path, baseline = self._transaction_fixture(root)
                        if location == "release_document":
                            release_key, candidate = self._coherently_reidentify_record(
                                baseline, schema_value,
                            )
                        else:
                            release_key = request["release_key"]
                            candidate = copy.deepcopy(baseline)
                            target = candidate if location == "record" else candidate[location]
                            target["schema"] = schema_value
                            candidate["record_digest"] = guarded.transaction_record_digest(candidate)
                        record_path = root / f"{release_key}.json"
                        record_path.write_text(
                            json.dumps(candidate, sort_keys=True, separators=(",", ":")) + "\n"
                        )
                        os.chmod(record_path, 0o600)
                        inspect = {
                            "schema": 1, "registry_root": str(root), "release_key": release_key,
                            "transaction_id": candidate["transaction_id"],
                        }
                        payload = guarded.encode_privileged_request(
                            "remote-transaction-inspect", inspect,
                        )
                        completed = subprocess.run(
                            [str(python), "-I", "-B", str(script),
                             "remote-transaction-inspect", "--request-b64", payload],
                            check=False, text=True, capture_output=True,
                        )
                        self.assertNotEqual(
                            completed.returncode, 0,
                            f"fresh authority accepted {location} schema {schema_value!r}",
                        )
                    finally:
                        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
