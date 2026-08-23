import json
import os
from pathlib import Path
import secrets
import signal
import shutil
import subprocess
import tempfile
from typing import cast
import unittest

from deploy import guarded_priority_1_3_release as guarded
from deploy.verify_priority_1_3_acceptance import PinnedRemoteExecutor


class OperationCompletionTransportTests(unittest.TestCase):
    def test_pinned_executor_does_not_treat_nonblocking_systemd_start_as_completion(self):
        root = Path(tempfile.mkdtemp(prefix="p13-operation-completion-"))
        try:
            for name in ("identity", "known-hosts"):
                path = root / name
                path.write_text("fixture", encoding="ascii")
                path.chmod(0o600)
            registry_root = Path("/run/matrix-priority-1-3/attempts")
            registry_root.mkdir(parents=True, mode=0o700, exist_ok=True)
            attempt_id = secrets.token_hex(16)
            registry = registry_root / f"{attempt_id}.json"
            systemd_events = []

            def systemd_runner(argv, *, timeout):
                del timeout
                systemd_events.append(tuple(argv))
                if argv[0] == "/usr/bin/systemd-run":
                    stdout_property = next(item for item in argv if item.startswith("--property=StandardOutput=file:"))
                    stdout_path = Path(stdout_property.removeprefix("--property=StandardOutput=file:"))
                    stdout_path.write_text('{"accepted":true}\n', encoding="utf-8")
                    return subprocess.CompletedProcess(argv, 0, "", "")
                if argv[:2] == ["/usr/bin/systemctl", "show"] and argv[-2:] == ["--property=ControlGroup", "--value"]:
                    unit = argv[2]
                    return subprocess.CompletedProcess(argv, 0, f"/system.slice/{unit}\n", "")
                if argv[:2] == ["/usr/bin/systemctl", "show"]:
                    unit = argv[2]
                    inspections = sum(1 for event in systemd_events if event[:2] == ("/usr/bin/systemctl", "show") and event[-1] != "--value")
                    values = {
                        "MainPID": "123" if inspections == 1 else "0",
                        "ActiveState": "active" if inspections == 1 else "inactive",
                        "SubState": "running" if inspections == 1 else "dead",
                        "ControlGroup": f"/system.slice/{unit}",
                        "ExecMainCode": "0" if inspections == 1 else "exited",
                        "ExecMainStatus": "0",
                        "Result": "success",
                    }
                    return subprocess.CompletedProcess(
                        argv, 0, "".join(f"{key}={values[key]}\n" for key in values), "",
                    )
                raise AssertionError(argv)

            controller = guarded.SystemdController(runner=systemd_runner)
            helpers = []

            def ssh_runner(argv, **kwargs):
                del kwargs
                helper = argv[-3]
                request = guarded.decode_privileged_request(helper, argv[-1])
                helpers.append(helper)
                result = guarded.dispatch_privileged(helper, request, controller=controller)
                if result.returncode:
                    return subprocess.CompletedProcess(argv, result.returncode, "", "fixture failure")
                if isinstance(result, guarded.OperationResult):
                    envelope = {"returncode": 0, "unit": result.unit, "cgroup": result.cgroup}
                elif isinstance(result, guarded.OperationInspectResult):
                    envelope = {
                        "returncode": 0, "state": result.state, "attempt_id": result.attempt_id,
                        "operation_id": result.operation_id, "registry": result.registry,
                        "unit": result.unit, "cgroup": result.cgroup,
                        "registry_digest": result.registry_digest,
                    }
                    if result.state == "TERMINAL":
                        envelope.update({"exit_status": result.exit_status, "stdout": result.stdout,
                                         "stderr": result.stderr, "output_digest": result.output_digest})
                else:
                    envelope = {"returncode": 0, "digest": result.digest, "terminal": result.terminal}
                return subprocess.CompletedProcess(argv, 0, json.dumps(envelope, separators=(",", ":")), "")

            executor = PinnedRemoteExecutor(
                "fixture@host", root / "identity", root / "known-hosts", runner=ssh_runner,
            )
            result = executor.run_remote(
                attempt_id, "deploy", ["/usr/bin/printf", '{"accepted":true}\\n'],
                registry=str(registry), run_as="root", operation_timeout=10,
            )

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '{"accepted":true}\n')
            self.assertEqual(helpers, ["remote-start", "remote-operation-inspect", "remote-operation-inspect"])
            self.assertGreaterEqual(len(systemd_events), 4)
            start_argv = systemd_events[0]
            self.assertIn("--property=RemainAfterExit=yes", start_argv)
            self.assertNotIn("--collect", start_argv)
        finally:
            for suffix in ("", ".lock"):
                try:
                    Path(str(locals().get("registry", "")) + suffix).unlink()
                except OSError:
                    pass
            shutil.rmtree(root, ignore_errors=True)


    def _executor_fixture(self, runner, *, clock=None, sleep=None):
        root = Path(tempfile.mkdtemp(prefix="p13-polling-"))
        for name in ("identity", "known-hosts"):
            path = root / name
            path.write_text("fixture", encoding="ascii")
            path.chmod(0o600)
        kwargs = {"runner": runner}
        if clock is not None:
            kwargs.update(clock=clock, sleep=sleep, poll_interval=0.1)
        return root, PinnedRemoteExecutor(
            "fixture@host", root / "identity", root / "known-hosts", **kwargs,
        )

    @staticmethod
    def _wire_for_request(request, *, state="TERMINAL", exit_status=0, stdout="done\n", stderr=""):
        unit = f"matrix-p13-op-{request['attempt_id']}-{request['operation_id']}.service"
        cgroup = f"/system.slice/{unit}"
        if state == "START":
            return {"returncode": 0, "unit": unit, "cgroup": cgroup}
        common = {
            "returncode": 0, "state": state, "attempt_id": request["attempt_id"],
            "operation_id": request["operation_id"], "registry": request["registry"],
            "unit": unit, "cgroup": cgroup, "registry_digest": "d" * 64,
        }
        if state == "TERMINAL":
            common.update({
                "exit_status": exit_status, "stdout": stdout, "stderr": stderr,
                "output_digest": guarded._operation_output_digest(
                    request["attempt_id"], request["operation_id"], request["registry"],
                    unit, cgroup, exit_status, stdout, stderr,
                ),
            })
        return common

    def test_lost_inspect_response_retries_inspect_without_restarting_and_polls_boundedly(self):
        calls = []
        sleeps = []
        now = [0.0]
        def clock(): return now[0]
        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds
        inspections = [0]
        def runner(argv, **kwargs):
            del kwargs
            helper = argv[-3]
            request = guarded.decode_privileged_request(helper, argv[-1])
            calls.append(helper)
            if helper == "remote-start":
                envelope = self._wire_for_request(request, state="START")
            else:
                inspections[0] += 1
                if inspections[0] == 1:
                    raise TimeoutError("lost after remote inspect response")
                envelope = self._wire_for_request(
                    request, state="RUNNING" if inspections[0] == 2 else "TERMINAL",
                )
            return subprocess.CompletedProcess(argv, 0, json.dumps(envelope), "")
        root, executor = self._executor_fixture(runner, clock=clock, sleep=sleep)
        try:
            result = executor.run_remote(
                "a" * 32, "deploy", ["/bin/true"],
                registry="/run/matrix-priority-1-3/attempts/" + "a" * 32 + ".json",
                run_as="root", operation_timeout=3,
            )
            self.assertEqual((result.returncode, result.stdout), (0, "done\n"))
            self.assertEqual(calls.count("remote-start"), 1)
            self.assertEqual(calls.count("remote-operation-inspect"), 3)
            self.assertEqual(sleeps, [0.1, 0.1])
        finally:
            shutil.rmtree(root)

    def test_aggregate_timeout_stops_exact_operation_and_proves_cgroup_extinct(self):
        root = Path(tempfile.mkdtemp(prefix="p13-operation-timeout-stop-"))
        registry_root = Path("/run/matrix-priority-1-3/attempts")
        registry_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        attempt_id = secrets.token_hex(16)
        registry = registry_root / f"{attempt_id}.json"
        now = [0.0]
        helpers = []
        systemd_events = []
        killed = [False]

        def clock():
            return now[0]

        def sleep(seconds):
            now[0] += seconds

        try:
            for name in ("identity", "known-hosts"):
                path = root / name
                path.write_text("fixture", encoding="ascii")
                path.chmod(0o600)
            cgroup_root = root / "cgroup"

            def systemd_runner(argv, *, timeout):
                del timeout
                systemd_events.append(tuple(argv))
                if argv[0] == "/usr/bin/systemd-run":
                    unit = next(item for item in argv if item.startswith("--unit=")).removeprefix("--unit=")
                    cgroup_dir = cgroup_root / "system.slice" / unit
                    cgroup_dir.mkdir(parents=True)
                    (cgroup_dir / "cgroup.events").write_text("populated 1\n", encoding="ascii")
                    (cgroup_dir / "cgroup.procs").write_text("999\n", encoding="ascii")
                    return subprocess.CompletedProcess(argv, 0, "", "")
                if argv[:2] == ["/usr/bin/systemctl", "show"] and argv[-1] == "--value":
                    unit = argv[2]
                    return subprocess.CompletedProcess(argv, 0, f"/system.slice/{unit}\n", "")
                if argv[:3] == ["/usr/bin/systemctl", "kill", "--signal=KILL"]:
                    killed[0] = True
                    unit = argv[-1]
                    cgroup_dir = cgroup_root / "system.slice" / unit
                    (cgroup_dir / "cgroup.events").write_text("populated 0\n", encoding="ascii")
                    (cgroup_dir / "cgroup.procs").write_text("", encoding="ascii")
                    return subprocess.CompletedProcess(argv, 0, "", "")
                if argv[:2] == ["/usr/bin/systemctl", "show"]:
                    unit = argv[2]
                    cgroup = f"/system.slice/{unit}"
                    if "--property=ExecMainCode" in argv:
                        values = {"MainPID": "999", "ActiveState": "active", "SubState": "running",
                                  "ControlGroup": cgroup, "ExecMainCode": "0",
                                  "ExecMainStatus": "0", "Result": "success"}
                    else:
                        values = ({"MainPID": "0", "ActiveState": "failed", "SubState": "failed",
                                   "ControlGroup": cgroup} if killed[0] else
                                  {"MainPID": "999", "ActiveState": "deactivating",
                                   "SubState": "stop-sigterm", "ControlGroup": cgroup})
                    return subprocess.CompletedProcess(
                        argv, 0, "".join(f"{key}={value}\n" for key, value in values.items()), "",
                    )
                return subprocess.CompletedProcess(argv, 0, "", "")

            controller = guarded.SystemdController(
                runner=systemd_runner, cgroup_root=cgroup_root,
                clock=clock, sleep=lambda _seconds: None,
            )

            def ssh_runner(argv, **kwargs):
                del kwargs
                helper = argv[-3]
                request = guarded.decode_privileged_request(helper, argv[-1])
                helpers.append((helper, request))
                result = guarded.dispatch_privileged(helper, request, controller=controller)
                if isinstance(result, guarded.OperationResult):
                    envelope = {"returncode": 0, "unit": result.unit, "cgroup": result.cgroup}
                elif isinstance(result, guarded.OperationInspectResult):
                    envelope = {"returncode": 0, "state": result.state,
                                "attempt_id": result.attempt_id, "operation_id": result.operation_id,
                                "registry": result.registry, "unit": result.unit, "cgroup": result.cgroup,
                                "registry_digest": result.registry_digest}
                else:
                    envelope = {"returncode": result.returncode, "digest": result.digest}
                return subprocess.CompletedProcess(argv, 0, json.dumps(envelope), "")

            executor = PinnedRemoteExecutor(
                "fixture@host", root / "identity", root / "known-hosts", runner=ssh_runner,
                clock=clock, sleep=sleep, poll_interval=0.1,
            )
            with self.assertRaisesRegex(TimeoutError, "aggregate deadline expired"):
                executor.run_remote(
                    attempt_id, "deploy", ["/bin/true"], registry=str(registry),
                    run_as="root", operation_timeout=1,
                )

            stops = [request for helper, request in helpers if helper == "remote-stop"]
            self.assertEqual(len(stops), 1)
            operation = next(request for helper, request in helpers if helper == "remote-start")
            self.assertEqual(stops[0], {
                "schema": 1, "attempt_id": attempt_id, "operation_id": operation["operation_id"],
                "registry": str(registry),
                "unit": f"matrix-p13-op-{attempt_id}-{operation['operation_id']}.service",
                "cgroup": f"/system.slice/matrix-p13-op-{attempt_id}-{operation['operation_id']}.service",
            })
            commands = [event[:3] for event in systemd_events]
            self.assertIn(("/usr/bin/systemctl", "kill", "--signal=TERM"), commands)
            self.assertIn(("/usr/bin/systemctl", "kill", "--signal=KILL"), commands)
            self.assertIn(("/usr/bin/systemctl", "reset-failed", stops[0]["unit"]), systemd_events)
            state = guarded.DurableAttemptRegistry(str(registry), attempt_id, create=False).snapshot()
            operations = cast(dict[str, dict[str, object]], state["operations"])
            self.assertEqual(operations[operation["operation_id"]]["status"], "EXTINCT")
        finally:
            for suffix in ("", ".lock"):
                try:
                    Path(str(registry) + suffix).unlink()
                except OSError:
                    pass
            shutil.rmtree(Path(str(registry) + ".outputs"), ignore_errors=True)
            shutil.rmtree(root, ignore_errors=True)

    def test_nonzero_terminal_is_returned_only_after_inspection(self):
        calls = []
        def runner(argv, **kwargs):
            del kwargs
            helper = argv[-3]
            request = guarded.decode_privileged_request(helper, argv[-1])
            calls.append(helper)
            envelope = self._wire_for_request(
                request, state="START" if helper == "remote-start" else "TERMINAL",
                exit_status=7, stdout="", stderr="failed\n",
            )
            return subprocess.CompletedProcess(argv, 0, json.dumps(envelope), "")
        root, executor = self._executor_fixture(runner)
        try:
            result = executor.run_remote(
                "a" * 32, "deploy", ["/bin/false"],
                registry="/run/matrix-priority-1-3/attempts/" + "a" * 32 + ".json",
                run_as="root", operation_timeout=3,
            )
            self.assertEqual((result.returncode, result.stderr), (7, "failed\n"))
            self.assertEqual(calls, ["remote-start", "remote-operation-inspect"])
        finally:
            shutil.rmtree(root)

    def test_watchdog_arm_remains_admission_only_and_is_not_inspected(self):
        calls = []
        def runner(argv, **kwargs):
            del kwargs
            helper = argv[-3]
            request = guarded.decode_privileged_request(helper, argv[-1])
            calls.append(helper)
            if helper != "remote-start":
                raise AssertionError("watchdog was waited")
            return subprocess.CompletedProcess(
                argv, 0, json.dumps(self._wire_for_request(request, state="START")), "",
            )
        root, executor = self._executor_fixture(runner)
        try:
            result = executor.run_remote(
                "a" * 32, "watchdog-arm", ["/usr/bin/systemd-run", "--unit", "guard"],
                registry="/run/matrix-priority-1-3/attempts/" + "a" * 32 + ".json",
                run_as="root", operation_timeout=3,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(calls, ["remote-start"])
        finally:
            shutil.rmtree(root)

    def test_watchdog_systemd_wrapper_argv_remains_collecting_nonblocking_and_nested_argv_unchanged(self):
        attempt_id = secrets.token_hex(16)
        registry_path = Path("/run/matrix-priority-1-3/attempts") / f"{attempt_id}.json"
        operation_id = __import__("hashlib").sha256(
            f"{attempt_id}\0watchdog-arm".encode("utf-8")
        ).hexdigest()[:32]
        unit = f"matrix-p13-op-{attempt_id}-{operation_id}.service"
        cgroup = f"/system.slice/{unit}"
        events = []
        def runner(argv, *, timeout):
            del timeout
            events.append(tuple(argv))
            if argv[0] == "/usr/bin/systemd-run":
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 0, cgroup + "\n", "")
        nested = ["/usr/bin/systemd-run", "--unit", "matrix-priority-1-3-guard.service",
                  "--no-block", "/trusted/recover"]
        request = {"schema": 1, "attempt_id": attempt_id, "operation_id": operation_id,
                   "registry": str(registry_path), "run_as": "root", "argv": nested}
        try:
            registry = guarded.DurableAttemptRegistry(str(registry_path), attempt_id, create=True)
            result = guarded.SystemdController(runner=runner).start(request, registry)
            self.assertEqual(result, guarded.OperationResult(0, unit, cgroup))
            wrapper = events[0]
            self.assertIn("--collect", wrapper)
            self.assertIn("--no-block", wrapper)
            self.assertNotIn("--property=RemainAfterExit=yes", wrapper)
            separator = wrapper.index("--")
            self.assertEqual(wrapper[separator + 1:], tuple(nested))
        finally:
            for path in (Path(str(registry_path) + ".lock"), registry_path):
                try: path.unlink()
                except OSError: pass
            shutil.rmtree(Path(str(registry_path) + ".outputs"), ignore_errors=True)


    def test_terminal_output_digest_mismatch_fails_closed(self):
        def runner(argv, **kwargs):
            del kwargs
            helper = argv[-3]
            request = guarded.decode_privileged_request(helper, argv[-1])
            envelope = self._wire_for_request(
                request, state="START" if helper == "remote-start" else "TERMINAL",
            )
            if helper == "remote-operation-inspect":
                envelope["output_digest"] = "0" * 64
            return subprocess.CompletedProcess(argv, 0, json.dumps(envelope), "")
        root, executor = self._executor_fixture(runner)
        try:
            with self.assertRaisesRegex(ValueError, "terminal output"):
                executor.run_remote(
                    "a" * 32, "deploy", ["/bin/true"],
                    registry="/run/matrix-priority-1-3/attempts/" + "a" * 32 + ".json",
                    run_as="root", operation_timeout=3,
                )
        finally:
            shutil.rmtree(root)


    def test_remain_after_exit_completed_unit_is_terminal_inspectable_and_output_retrievable(self):
        attempt_id = secrets.token_hex(16)
        registry_path = Path("/run/matrix-priority-1-3/attempts") / f"{attempt_id}.json"
        operation_id = "c" * 32
        unit = f"matrix-p13-op-{attempt_id}-{operation_id}.service"
        cgroup = f"/system.slice/{unit}"
        events = []
        def runner(argv, *, timeout):
            del timeout
            events.append(tuple(argv))
            if argv[0] == "/usr/bin/systemd-run":
                stdout_property = next(item for item in argv if item.startswith("--property=StandardOutput=file:"))
                Path(stdout_property.removeprefix("--property=StandardOutput=file:")).write_text(
                    "completed output\n", encoding="utf-8",
                )
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[-1] == "--value":
                return subprocess.CompletedProcess(argv, 0, cgroup + "\n", "")
            values = {"MainPID": "0", "ActiveState": "active", "SubState": "exited",
                      "ControlGroup": cgroup, "ExecMainCode": "exited",
                      "ExecMainStatus": "0", "Result": "success"}
            return subprocess.CompletedProcess(
                argv, 0, "".join(f"{key}={value}\n" for key, value in values.items()), "",
            )
        controller = guarded.SystemdController(runner=runner)
        request = {"schema": 1, "attempt_id": attempt_id, "operation_id": operation_id,
                   "registry": str(registry_path), "run_as": "root", "argv": ["/bin/true"]}
        try:
            registry = guarded.DurableAttemptRegistry(str(registry_path), attempt_id, create=True)
            started = controller.start(request, registry)
            inspect_request = {"schema": 1, "attempt_id": attempt_id,
                               "operation_id": operation_id, "registry": str(registry_path),
                               "unit": started.unit, "cgroup": started.cgroup}
            completed = controller.inspect(inspect_request, registry)
            replayed = controller.inspect(inspect_request, registry)
            self.assertEqual((completed.state, completed.exit_status, completed.stdout),
                             ("TERMINAL", 0, "completed output\n"))
            self.assertEqual(replayed, completed)
            self.assertEqual(sum(event[:2] == ("/usr/bin/systemctl", "show") and event[-1] != "--value"
                                 for event in events), 1)
        finally:
            for path in (Path(str(registry_path) + ".lock"), registry_path):
                try: path.unlink()
                except OSError: pass
            shutil.rmtree(Path(str(registry_path) + ".outputs"), ignore_errors=True)


    def test_lost_start_response_restart_inspects_same_unit_without_rerun(self):
        attempt_id = secrets.token_hex(16)
        registry_path = Path("/run/matrix-priority-1-3/attempts") / f"{attempt_id}.json"
        operation_id = "e" * 32
        unit = f"matrix-p13-op-{attempt_id}-{operation_id}.service"
        cgroup = f"/system.slice/{unit}"
        events = []
        def runner(argv, *, timeout):
            del timeout
            events.append(tuple(argv))
            if argv[0] == "/usr/bin/systemd-run":
                stdout_property = next(item for item in argv if item.startswith("--property=StandardOutput=file:"))
                Path(stdout_property.removeprefix("--property=StandardOutput=file:")).write_text(
                    "survived restart\n", encoding="utf-8",
                )
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[-1] == "--value":
                return subprocess.CompletedProcess(argv, 0, cgroup + "\n", "")
            values = {"MainPID": "0", "ActiveState": "active", "SubState": "exited",
                      "ControlGroup": cgroup, "ExecMainCode": "exited",
                      "ExecMainStatus": "0", "Result": "success"}
            return subprocess.CompletedProcess(
                argv, 0, "".join(f"{key}={value}\n" for key, value in values.items()), "",
            )
        request = {"schema": 1, "attempt_id": attempt_id, "operation_id": operation_id,
                   "registry": str(registry_path), "run_as": "root", "argv": ["/bin/true"]}
        try:
            registry = guarded.DurableAttemptRegistry(str(registry_path), attempt_id, create=True)
            first = guarded.SystemdController(runner=runner).start(request, registry)
            restarted = guarded.SystemdController(runner=runner)
            replay = restarted.start(request, registry)
            inspect_request = {"schema": 1, "attempt_id": attempt_id,
                               "operation_id": operation_id, "registry": str(registry_path),
                               "unit": replay.unit, "cgroup": replay.cgroup}
            terminal = restarted.inspect(inspect_request, registry)
            self.assertEqual(replay, first)
            self.assertEqual(sum(event[0] == "/usr/bin/systemd-run" for event in events), 1)
            self.assertEqual((terminal.state, terminal.stdout), ("TERMINAL", "survived restart\n"))
        finally:
            for path in (Path(str(registry_path) + ".lock"), registry_path):
                try: path.unlink()
                except OSError: pass
            shutil.rmtree(Path(str(registry_path) + ".outputs"), ignore_errors=True)

    def test_failed_systemd_admission_is_durable_and_replay_does_not_rerun(self):
        attempt_id = secrets.token_hex(16)
        registry_path = Path("/run/matrix-priority-1-3/attempts") / f"{attempt_id}.json"
        operation_id = "f" * 32
        calls = []

        def runner(argv, *, timeout):
            del timeout
            calls.append(tuple(argv))
            if argv[0] == "/usr/bin/systemd-run":
                return subprocess.CompletedProcess(argv, 7, "", "admission rejected\n")
            raise AssertionError(argv)

        request = {"schema": 1, "attempt_id": attempt_id, "operation_id": operation_id,
                   "registry": str(registry_path), "run_as": "root", "argv": ["/bin/false"]}
        try:
            registry = guarded.DurableAttemptRegistry(str(registry_path), attempt_id, create=True)
            controller = guarded.SystemdController(runner=runner)
            failed = controller.start(request, registry)
            replayed = guarded.SystemdController(runner=runner).start(request, registry)
            operation = cast(
                dict[str, dict[str, object]], registry.snapshot()["operations"],
            )[operation_id]
            self.assertEqual((failed.returncode, failed.stderr), (7, "admission rejected\n"))
            self.assertEqual(replayed, failed)
            self.assertEqual(operation["status"], "START_FAILED")
            self.assertEqual(operation["terminal"], {
                "exit_status": 7, "stdout": "", "stderr": "admission rejected\n",
                "output_digest": guarded._operation_output_digest(
                    attempt_id, operation_id, str(registry_path),
                    f"matrix-p13-op-{attempt_id}-{operation_id}.service",
                    f"/system.slice/matrix-p13-op-{attempt_id}-{operation_id}.service",
                    7, "", "admission rejected\n",
                ),
            })
            self.assertEqual(sum(call[0] == "/usr/bin/systemd-run" for call in calls), 1)
        finally:
            for path in (Path(str(registry_path) + ".lock"), registry_path):
                try:
                    path.unlink()
                except OSError:
                    pass
            shutil.rmtree(Path(str(registry_path) + ".outputs"), ignore_errors=True)

    def test_pre_start_replay_reconciles_exact_admission_or_proven_absence(self):
        for admitted in (True, False):
            with self.subTest(admitted=admitted):
                attempt_id = secrets.token_hex(16)
                registry_path = Path("/run/matrix-priority-1-3/attempts") / f"{attempt_id}.json"
                operation_id = "1" * 32
                unit = f"matrix-p13-op-{attempt_id}-{operation_id}.service"
                cgroup = f"/system.slice/{unit}"
                request = {"schema": 1, "attempt_id": attempt_id, "operation_id": operation_id,
                           "registry": str(registry_path), "run_as": "root", "argv": ["/bin/true"]}
                digest = guarded._request_digest("remote-start", request)
                output_root = Path(str(registry_path) + ".outputs")
                calls = []

                def runner(argv, *, timeout):
                    del timeout
                    calls.append(tuple(argv))
                    if argv[0] == "/usr/bin/systemd-run":
                        raise AssertionError("PRE_START replay must not duplicate admission")
                    payload = (f"LoadState=loaded\nControlGroup={cgroup}\n" if admitted
                               else "LoadState=not-found\nControlGroup=\n")
                    return subprocess.CompletedProcess(argv, 0 if admitted else 1, payload, "")

                try:
                    registry = guarded.DurableAttemptRegistry(str(registry_path), attempt_id, create=True)
                    guarded.SystemdController._prepare_output_file(output_root / f"{operation_id}.stdout")
                    guarded.SystemdController._prepare_output_file(output_root / f"{operation_id}.stderr")
                    self.assertTrue(registry.register_operation(
                        operation_id, digest, unit, cgroup,
                        str(output_root / f"{operation_id}.stdout"),
                        str(output_root / f"{operation_id}.stderr"),
                    ))
                    replay = guarded.SystemdController(runner=runner).start(request, registry)
                    operation = cast(
                        dict[str, dict[str, object]], registry.snapshot()["operations"],
                    )[operation_id]
                    self.assertEqual(replay.returncode, 0 if admitted else 2)
                    self.assertEqual(operation["status"], "RUNNING" if admitted else "START_FAILED")
                    self.assertEqual(sum(call[0] == "/usr/bin/systemd-run" for call in calls), 0)
                finally:
                    for path in (Path(str(registry_path) + ".lock"), registry_path):
                        try:
                            path.unlink()
                        except OSError:
                            pass
                    shutil.rmtree(output_root, ignore_errors=True)

    def test_empty_pre_start_outputs_are_adopted_before_single_admission(self):
        attempt_id = secrets.token_hex(16)
        registry_path = Path("/run/matrix-priority-1-3/attempts") / f"{attempt_id}.json"
        operation_id = "2" * 32
        unit = f"matrix-p13-op-{attempt_id}-{operation_id}.service"
        cgroup = f"/system.slice/{unit}"
        output_root = Path(str(registry_path) + ".outputs")
        calls = []

        def runner(argv, *, timeout):
            del timeout
            calls.append(tuple(argv))
            if argv[0] == "/usr/bin/systemd-run":
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 0, cgroup + "\n", "")

        request = {"schema": 1, "attempt_id": attempt_id, "operation_id": operation_id,
                   "registry": str(registry_path), "run_as": "root", "argv": ["/bin/true"]}
        try:
            registry = guarded.DurableAttemptRegistry(str(registry_path), attempt_id, create=True)
            guarded.SystemdController._prepare_output_file(output_root / f"{operation_id}.stdout")
            guarded.SystemdController._prepare_output_file(output_root / f"{operation_id}.stderr")
            admitted = guarded.SystemdController(runner=runner).start(request, registry)
            operation = cast(
                dict[str, dict[str, object]], registry.snapshot()["operations"],
            )[operation_id]
            self.assertEqual(admitted.returncode, 0)
            self.assertEqual(operation["status"], "RUNNING")
            self.assertEqual(sum(call[0] == "/usr/bin/systemd-run" for call in calls), 1)
        finally:
            for path in (Path(str(registry_path) + ".lock"), registry_path):
                try:
                    path.unlink()
                except OSError:
                    pass
            shutil.rmtree(output_root, ignore_errors=True)

    def test_fresh_process_kill_points_converge_without_duplicate_admission(self):
        cases = (
            "after-output-creation", "after-pre-start-publication",
            "after-systemd-admission", "after-unit-identity",
            "after-running-publication", "after-terminal-publication",
        )
        for checkpoint in cases:
            with self.subTest(checkpoint=checkpoint):
                attempt_id = secrets.token_hex(16)
                registry_path = Path("/run/matrix-priority-1-3/attempts") / f"{attempt_id}.json"
                operation_id = "3" * 32
                unit = f"matrix-p13-op-{attempt_id}-{operation_id}.service"
                cgroup = f"/system.slice/{unit}"
                root = Path(tempfile.mkdtemp(prefix="p13-start-killpoint-"))
                admitted = root / "admitted"
                run_count = root / "run-count"
                output_root = Path(str(registry_path) + ".outputs")
                request = {"schema": 1, "attempt_id": attempt_id, "operation_id": operation_id,
                           "registry": str(registry_path), "run_as": "root", "argv": ["/bin/true"]}

                def runner(argv, *, timeout):
                    del timeout
                    if argv[0] == "/usr/bin/systemd-run":
                        count = int(run_count.read_text()) if run_count.exists() else 0
                        run_count.write_text(str(count + 1), encoding="ascii")
                        admitted.write_text("loaded", encoding="ascii")
                        stdout_property = next(
                            item for item in argv if item.startswith("--property=StandardOutput=file:")
                        )
                        Path(stdout_property.removeprefix(
                            "--property=StandardOutput=file:"
                        )).write_text("terminal output\n", encoding="utf-8")
                        return subprocess.CompletedProcess(argv, 0, "", "")
                    if argv[:2] == ["/usr/bin/systemctl", "show"] and argv[-1] == "--value":
                        return subprocess.CompletedProcess(
                            argv, 0 if admitted.exists() else 1,
                            cgroup + "\n" if admitted.exists() else "\n", "",
                        )
                    if "--property=LoadState" in argv:
                        payload = (f"LoadState=loaded\nControlGroup={cgroup}\n" if admitted.exists()
                                   else "LoadState=not-found\nControlGroup=\n")
                        return subprocess.CompletedProcess(
                            argv, 0 if admitted.exists() else 1, payload, "",
                        )
                    values = {"MainPID": "0", "ActiveState": "active", "SubState": "exited",
                              "ControlGroup": cgroup, "ExecMainCode": "exited",
                              "ExecMainStatus": "0", "Result": "success"}
                    return subprocess.CompletedProcess(
                        argv, 0, "".join(f"{key}={value}\n" for key, value in values.items()), "",
                    )

                try:
                    pid = os.fork()
                    if pid == 0:
                        try:
                            registry = guarded.DurableAttemptRegistry(
                                str(registry_path), attempt_id, create=True,
                            )
                            def kill_at(name):
                                if name == checkpoint:
                                    os.kill(os.getpid(), signal.SIGKILL)
                            controller = guarded.SystemdController(runner=runner, checkpoint=kill_at)
                            started = controller.start(request, registry)
                            if checkpoint == "after-terminal-publication" and started.returncode == 0:
                                controller.inspect({
                                    "schema": 1, "attempt_id": attempt_id,
                                    "operation_id": operation_id, "registry": str(registry_path),
                                    "unit": unit, "cgroup": cgroup,
                                }, registry)
                        finally:
                            os._exit(99)
                    _pid, wait_status = os.waitpid(pid, 0)
                    self.assertEqual(os.waitstatus_to_exitcode(wait_status), -signal.SIGKILL)

                    registry = guarded.DurableAttemptRegistry(
                        str(registry_path), attempt_id,
                        create=checkpoint == "after-output-creation",
                    )
                    controller = guarded.SystemdController(runner=runner)
                    if checkpoint == "after-terminal-publication":
                        replay = controller.inspect({
                            "schema": 1, "attempt_id": attempt_id,
                            "operation_id": operation_id, "registry": str(registry_path),
                            "unit": unit, "cgroup": cgroup,
                        }, registry)
                        self.assertEqual(
                            (replay.state, replay.stdout), ("TERMINAL", "terminal output\n"),
                        )
                    else:
                        replay = controller.start(request, registry)
                        expected = 2 if checkpoint == "after-pre-start-publication" else 0
                        self.assertEqual(replay.returncode, expected)
                    self.assertEqual(
                        int(run_count.read_text()) if run_count.exists() else 0,
                        0 if checkpoint == "after-pre-start-publication" else 1,
                    )
                finally:
                    for path in (Path(str(registry_path) + ".lock"), registry_path):
                        try:
                            path.unlink()
                        except OSError:
                            pass
                    shutil.rmtree(output_root, ignore_errors=True)
                    shutil.rmtree(root, ignore_errors=True)

    def test_failed_unit_retains_nonzero_terminal_status_and_stderr(self):
        attempt_id = secrets.token_hex(16)
        registry_path = Path("/run/matrix-priority-1-3/attempts") / f"{attempt_id}.json"
        operation_id = "d" * 32
        unit = f"matrix-p13-op-{attempt_id}-{operation_id}.service"
        cgroup = f"/system.slice/{unit}"
        def runner(argv, *, timeout):
            del timeout
            if argv[0] == "/usr/bin/systemd-run":
                stderr_property = next(item for item in argv if item.startswith("--property=StandardError=file:"))
                Path(stderr_property.removeprefix("--property=StandardError=file:")).write_text(
                    "failed output\n", encoding="utf-8",
                )
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[-1] == "--value":
                return subprocess.CompletedProcess(argv, 0, cgroup + "\n", "")
            values = {"MainPID": "0", "ActiveState": "failed", "SubState": "failed",
                      "ControlGroup": cgroup, "ExecMainCode": "exited",
                      "ExecMainStatus": "7", "Result": "exit-code"}
            return subprocess.CompletedProcess(
                argv, 0, "".join(f"{key}={value}\n" for key, value in values.items()), "",
            )
        controller = guarded.SystemdController(runner=runner)
        request = {"schema": 1, "attempt_id": attempt_id, "operation_id": operation_id,
                   "registry": str(registry_path), "run_as": "root", "argv": ["/bin/false"]}
        try:
            registry = guarded.DurableAttemptRegistry(str(registry_path), attempt_id, create=True)
            started = controller.start(request, registry)
            inspect_request = {"schema": 1, "attempt_id": attempt_id,
                               "operation_id": operation_id, "registry": str(registry_path),
                               "unit": started.unit, "cgroup": started.cgroup}
            failed = controller.inspect(inspect_request, registry)
            self.assertEqual((failed.state, failed.exit_status, failed.stderr),
                             ("TERMINAL", 7, "failed output\n"))
            self.assertEqual(controller.inspect(inspect_request, registry), failed)
        finally:
            for path in (Path(str(registry_path) + ".lock"), registry_path):
                try: path.unlink()
                except OSError: pass
            shutil.rmtree(Path(str(registry_path) + ".outputs"), ignore_errors=True)


    def test_systemd_terminal_result_must_match_typed_exit_status(self):
        attempt_id = secrets.token_hex(16)
        registry_path = Path("/run/matrix-priority-1-3/attempts") / f"{attempt_id}.json"
        operation_id = "b" * 32
        unit = f"matrix-p13-op-{attempt_id}-{operation_id}.service"
        cgroup = f"/system.slice/{unit}"
        def runner(argv, *, timeout):
            del timeout
            if argv[0] == "/usr/bin/systemd-run":
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[-1] == "--value":
                return subprocess.CompletedProcess(argv, 0, cgroup + "\n", "")
            values = {"MainPID": "0", "ActiveState": "inactive", "SubState": "dead",
                      "ControlGroup": cgroup, "ExecMainCode": "exited",
                      "ExecMainStatus": "0", "Result": "exit-code"}
            return subprocess.CompletedProcess(
                argv, 0, "".join(f"{key}={value}\n" for key, value in values.items()), "",
            )
        controller = guarded.SystemdController(runner=runner)
        request = {"schema": 1, "attempt_id": attempt_id, "operation_id": operation_id,
                   "registry": str(registry_path), "run_as": "root", "argv": ["/bin/true"]}
        try:
            registry = guarded.DurableAttemptRegistry(str(registry_path), attempt_id, create=True)
            started = controller.start(request, registry)
            inspect = {"schema": 1, "attempt_id": attempt_id, "operation_id": operation_id,
                       "registry": str(registry_path), "unit": started.unit, "cgroup": started.cgroup}
            with self.assertRaisesRegex(ValueError, "terminal operation state"):
                controller.inspect(inspect, registry)
        finally:
            for path in (Path(str(registry_path) + ".lock"), registry_path):
                try: path.unlink()
                except OSError: pass
            shutil.rmtree(Path(str(registry_path) + ".outputs"), ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
