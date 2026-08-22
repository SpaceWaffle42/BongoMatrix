import importlib
import unittest


class GuardedPriority13ReleaseTests(unittest.TestCase):
    def _module(self):
        return importlib.import_module("deploy.guarded_priority_1_3_release")

    def test_three_attempts_use_remote_typed_signals_crash_recovery_and_zero_only_success(self):
        guarded = self._module()

        class Executor:
            def __init__(self): self.controls = []; self.operations = []
            def control_remote(self, kind, request, *, operation_timeout):
                self.controls.append((kind, request, operation_timeout))
                return guarded.ControlResult(0, "typed-digest")
            def run_remote(self, attempt_id, operation_id, argv, *, run_as, operation_timeout):
                self.operations.append((attempt_id, operation_id, tuple(argv), run_as, operation_timeout))
                return guarded.OperationResult(0, f"matrix-p13-op-{attempt_id}-{operation_id}.service", f"/system.slice/{attempt_id}")

        executor = Executor()
        acceptance_results = iter((0, 0, 0))
        result = guarded.run_three_attempts(
            executor, lambda phase, attempt_id: next(acceptance_results),
            boundary="b1", stage="/stage", bundle="/bundle",
        )
        self.assertEqual(result, 0)
        attempts = [request["attempt_id"] for kind, request, _ in executor.controls if kind == "remote-recover" and request["action"] == "arm"]
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(set(attempts)), 3)
        self.assertEqual(
            [request["phase"] for kind, request, _ in executor.controls if kind == "remote-recover" and request["action"] == "arm"],
            ["initial-deploy", "rollback-proof", "final-deploy"],
        )
        commits = [request for kind, request, _ in executor.controls if kind == "remote-commit"]
        self.assertEqual(len(commits), 3)
        self.assertTrue(all(request["acceptance_result"] == 0 for request in commits))
        self.assertTrue(all(op[3] == "root" for op in executor.operations))

        failed = Executor()
        result = guarded.run_three_attempts(
            failed, lambda phase, attempt_id: 7,
            boundary="b1", stage="/stage", bundle="/bundle",
        )
        self.assertNotEqual(result, 0)
        self.assertFalse(any(kind == "remote-commit" for kind, _, _ in failed.controls))
        self.assertTrue(any(kind == "remote-recover" and request["action"] == "execute" for kind, request, _ in failed.controls))

    def test_fake_clock_attempt_deadline_precedes_watchdog_and_forces_pending_recovery(self):
        guarded = self._module()

        class Clock:
            def __init__(self): self.now = 100.0
            def monotonic(self): return self.now

        clock = Clock()
        events = []

        def arm_watchdog(deadline):
            events.append(("armed", deadline))
            return "guard"

        def work(deadline):
            events.append(("work", deadline))
            clock.now = deadline + 0.001
            return 0

        result = guarded.run_guarded_attempt(
            clock=clock.monotonic,
            attempt_budget_seconds=6600,
            commit_reserve_seconds=60,
            watchdog_seconds=7200,
            arm_watchdog=arm_watchdog,
            work=work,
            recover=lambda reason: events.append(("recover", reason)) or 0,
            commit=lambda: events.append(("commit",)) or 0,
        )

        self.assertEqual(events[0], ("armed", 7300.0))
        self.assertEqual(events[1], ("work", 6640.0))
        self.assertLess(events[1][1], events[0][1])
        self.assertEqual(result.state, "PENDING")
        self.assertEqual(result.reason, "attempt-deadline")
        self.assertIn(("recover", "attempt-deadline"), events)
        self.assertNotIn(("commit",), events)

    def test_remote_descendants_and_start_race_are_sealed_term_killed_and_extinct_before_recovery(self):
        guarded = self._module()
        registry = guarded.AttemptRegistry("attempt-a")
        self.assertTrue(registry.register_operation("op-a", "unit-a", "/cg/a"))
        self.assertTrue(registry.register_operation("op-b", "unit-b", "/cg/b"))
        events = []

        class Controller:
            def __init__(self): self.killed = set()
            def term(self, unit):
                events.append(("TERM", unit))
                if unit == "unit-a":
                    events.append(("racing-start", registry.register_operation("late", "unit-late", "/cg/late")))
            def is_extinct(self, cgroup):
                events.append(("extinct?", cgroup))
                return cgroup == "/cg/a" or cgroup.removeprefix("/cg/") in self.killed
            def kill(self, unit):
                events.append(("KILL", unit))
                self.killed.add(unit.removeprefix("unit-"))

        result = guarded.quiesce_then_recover(
            registry,
            request_digest="recovery-digest",
            controller=Controller(),
            recover=lambda: events.append(("RECOVER",)) or 0,
        )

        self.assertEqual(result, 0)
        self.assertEqual(registry.state, "SEALED_RECOVERING")
        self.assertIn(("racing-start", False), events)
        self.assertIn(("TERM", "unit-a"), events)
        self.assertIn(("TERM", "unit-b"), events)
        self.assertNotIn(("KILL", "unit-a"), events)
        self.assertIn(("KILL", "unit-b"), events)
        recover_index = events.index(("RECOVER",))
        self.assertLess(events.index(("extinct?", "/cg/a")), recover_index)
        self.assertLess(events.index(("extinct?", "/cg/b"), events.index(("KILL", "unit-b"))), recover_index)
        self.assertFalse(registry.register_operation("after", "unit-after", "/cg/after"))

    def test_privileged_request_framing_is_canonical_bounded_and_exact_argv(self):
        import base64
        import json
        guarded = self._module()
        helpers = ("remote-start", "remote-stop", "remote-recover", "remote-commit")
        argv = ["/stage/deploy/release_bundle.py", "recover", "--service", "matrix.service", "snowman-\u2603"]
        canonical = json.dumps(argv, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        expected = base64.urlsafe_b64encode(canonical).rstrip(b"=").decode("ascii")
        self.assertEqual(guarded.encode_privileged_argv(argv), expected)
        for helper in helpers:
            self.assertEqual(guarded.decode_privileged_argv(helper, expected), argv)
            self.assertEqual(
                guarded.build_privileged_execution(helper, expected, executable="/trusted/helper"),
                ["/trusted/helper", helper, "--", *argv],
            )

        frame = lambda value: base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
        invalid = [
            expected + "=",                         # padding
            "+" + expected[1:],                     # alternate alphabet
            "A" * 65537,                            # encoded bound
            frame(b"\xff"),                       # malformed UTF-8
            frame(b"["),                            # malformed JSON
            frame(b"[ \"x\" ]"),                # noncanonical whitespace
            frame(b"\"scalar\""),              # wrong scalar shape
            frame(b"[]"),                           # empty
            frame(b"[1]"),                          # non-string
            frame(b"[[\"nested\"]]"),           # nested
            frame(json.dumps(["bad\x00arg"], ensure_ascii=True, separators=(",", ":")).encode()),
            frame(json.dumps(["x"] * 257, ensure_ascii=True, separators=(",", ":")).encode()),
            frame(json.dumps(["x" * 4097], ensure_ascii=True, separators=(",", ":")).encode()),
            frame(json.dumps(["x" * 4096] * 13, ensure_ascii=True, separators=(",", ":")).encode()),
        ]
        for helper in helpers:
            for payload in invalid:
                with self.subTest(helper=helper, payload_size=len(payload)):
                    with self.assertRaises(ValueError):
                        guarded.decode_privileged_argv(helper, payload)
        for bad_helper in ("start", "recover", "", "REMOTE-START"):
            with self.assertRaises(ValueError):
                guarded.decode_privileged_argv(bad_helper, expected)

    def test_remote_commit_is_idempotent_self_exempt_and_wins_start_disarm_races(self):
        guarded = self._module()
        registry = guarded.AttemptRegistry("attempt-c")
        self.assertTrue(registry.register_operation("deploy", "unit-deploy", "/cg/deploy"))
        events = []

        def quiesce(targets):
            events.append(("quiesce", tuple(targets)))
            events.append(("start-race", registry.register_operation("late-a", "unit-late-a", "/cg/late-a")))
            return 0

        def write_success():
            events.append(("write-success",))
            return "success-record-digest"

        def disarm(unit):
            events.append(("disarm", unit))
            events.append(("disarm-start-race", registry.register_operation("late-b", "unit-late-b", "/cg/late-b")))
            return 0

        result = guarded.remote_commit(
            registry,
            request_digest="commit-request-digest",
            control_unit="matrix-p13-commit-attempt-c.service",
            control_cgroup="/cg/commit",
            guard_unit="matrix-p13-recover-attempt-c.service",
            quiesce_operations=quiesce,
            write_success=write_success,
            disarm_guard=disarm,
        )
        self.assertEqual(result, guarded.ControlResult(0, "success-record-digest"))
        self.assertEqual(registry.state, "SEALED_COMMITTED")
        self.assertIn(("start-race", False), events)
        self.assertIn(("disarm-start-race", False), events)
        quiesced = next(event[1] for event in events if event[0] == "quiesce")
        self.assertIn(("unit-deploy", "/cg/deploy"), quiesced)
        self.assertNotIn(("matrix-p13-commit-attempt-c.service", "/cg/commit"), quiesced)

        before_retry = list(events)
        retry = guarded.remote_commit(
            registry,
            request_digest="commit-request-digest",
            control_unit="matrix-p13-commit-attempt-c.service",
            control_cgroup="/cg/commit",
            guard_unit="matrix-p13-recover-attempt-c.service",
            quiesce_operations=quiesce,
            write_success=write_success,
            disarm_guard=disarm,
        )
        self.assertEqual(retry, result)
        self.assertEqual(events, before_retry)
        with self.assertRaises(ValueError):
            guarded.remote_commit(
                registry,
                request_digest="mismatch",
                control_unit="matrix-p13-commit-attempt-c.service",
                control_cgroup="/cg/commit",
                guard_unit="matrix-p13-recover-attempt-c.service",
                quiesce_operations=quiesce,
                write_success=write_success,
                disarm_guard=disarm,
            )

    def test_immediate_and_watchdog_execstart_are_identical_remote_recover_only(self):
        guarded = self._module()
        request_b64 = guarded.encode_privileged_argv([
            "b1", "final-deploy", "attempt-r", "evidence-digest", "pending-note", "guard-unit"
        ])
        expected = [
            "/home/admin/matrix/venv/bin/python", "-I", "-B",
            "/trusted/stage/deploy/guarded_priority_1_3_release.py",
            "remote-recover", "--request-b64", request_b64,
        ]
        immediate = guarded.immediate_recovery_execstart("/trusted/stage", request_b64)
        watchdog = guarded.watchdog_recovery_execstart("/trusted/stage", request_b64)
        self.assertEqual(immediate, expected)
        self.assertEqual(watchdog, expected)
        self.assertEqual(immediate, watchdog)
        self.assertNotIn("release_bundle.py", " ".join(immediate))
        self.assertFalse(any(part in {"sh", "bash", "-c"} for part in immediate))
        for bad_stage in ("trusted/stage", "/trusted/../stage", "/trusted/stage;sh", "/trusted/stage\n"):
            with self.assertRaises(ValueError):
                guarded.immediate_recovery_execstart(bad_stage, request_b64)

    def test_matching_sealed_recovery_replays_every_gateway_boundary_and_rejects_mismatch(self):
        guarded = self._module()
        request = {
            "schema": 1,
            "boundary": "b1",
            "phase": "final-deploy",
            "attempt_id": "attempt-z",
            "evidence_digest": "e" * 64,
            "pending_note_id": "pending-7",
            "guard_unit": "matrix-p13-recover-attempt-z.service",
        }
        boundaries = (
            "after-seal",
            "after-extinction:unit-commit",
            "after-extinction:unit-a",
            "after-extinction:unit-b",
            "before-lock",
            "after-lock",
            "before-inner-recovery",
            "after-inner-recovery",
            "before-terminal-record",
            "after-terminal-record",
        )

        class Crash(RuntimeError): pass

        for crash_at in boundaries:
            with self.subTest(crash_at=crash_at):
                registry = guarded.AttemptRegistry("attempt-z")
                registry.register_operation("a", "unit-a", "/cg/a")
                registry.register_operation("b", "unit-b", "/cg/b")
                registry.state = "SEALED_COMMITTING"
                registry.commit_request_digest = "old-commit"
                registry.commit_cgroup = ("unit-commit", "/cg/commit")
                events = []

                class Controller:
                    def term(self, unit): events.append(("TERM", unit))
                    def is_extinct(self, cgroup): events.append(("extinct", cgroup)); return True
                    def kill(self, unit): events.append(("KILL", unit))

                def checkpoint(name):
                    events.append(("checkpoint", name))
                    if name == crash_at:
                        raise Crash(name)

                with self.assertRaises(Crash):
                    guarded.remote_recover_gateway(
                        registry,
                        request=request,
                        controller=Controller(),
                        acquire_release_lock=lambda: events.append(("lock",)) or 0,
                        inner_recover=lambda: events.append(("inner",)) or guarded.ControlResult(0, "inner-digest"),
                        checkpoint=checkpoint,
                    )
                self.assertIn(registry.state, {"SEALED_RECOVERING", "SEALED_RECOVERED"})

                mismatch = dict(request, evidence_digest="f" * 64)
                with self.assertRaises(ValueError):
                    guarded.remote_recover_gateway(
                        registry,
                        request=mismatch,
                        controller=Controller(),
                        acquire_release_lock=lambda: 0,
                        inner_recover=lambda: guarded.ControlResult(0, "wrong"),
                    )

                result = guarded.remote_recover_gateway(
                    registry,
                    request=request,
                    controller=Controller(),
                    acquire_release_lock=lambda: events.append(("lock-replay",)) or 0,
                    inner_recover=lambda: events.append(("inner-replay",)) or guarded.ControlResult(0, "inner-digest"),
                )
                self.assertEqual(result.returncode, 0)
                self.assertTrue(result.digest)
                self.assertEqual(registry.state, "SEALED_RECOVERED")
                self.assertEqual(registry.recovery_digest, guarded.canonical_recovery_request_digest(request))
                before_terminal_replay = list(events)
                self.assertEqual(
                    guarded.remote_recover_gateway(
                        registry,
                        request=request,
                        controller=Controller(),
                        acquire_release_lock=lambda: events.append(("unexpected-lock",)) or 0,
                        inner_recover=lambda: events.append(("unexpected-inner",)) or guarded.ControlResult(0, "wrong"),
                    ),
                    result,
                )
                self.assertEqual(events, before_terminal_replay)
                self.assertFalse(hasattr(result, "rolled_back"))
