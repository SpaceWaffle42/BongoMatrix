#!/usr/bin/python3
"""Harmless fixed-command SSH/systemd fixture for the exported production-chain test."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

root = Path(os.environ["FIXTURE_ROOT"])
sys.path.insert(0, os.environ["FIXTURE_EXPORT"])
from deploy.guarded_priority_1_3_release import (
    _operation_output_digest, decode_privileged_request, dispatch_privileged,
)

assert all(name not in os.environ for name in (
    "SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_SSH", "GIT_SSH_COMMAND",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
))
argv = sys.argv[1:]
expected = [
    "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
    "-o", "IdentityAgent=none", "-o", "StrictHostKeyChecking=yes",
    "-o", f"UserKnownHostsFile={root / 'known-hosts'}", "-o", "ConnectTimeout=10",
    "-i", str(root / "identity"), "--", "fixture@host",
    "/usr/local/libexec/matrix-priority-1-3-remote",
]
assert argv[:len(expected)] == expected, (argv, expected)
helper = argv[len(expected)]
assert argv[len(expected) + 1] == "--request-b64" and len(argv) == len(expected) + 3
request = decode_privileged_request(helper, argv[-1])
event = {"helper": helper, "argv": argv, "request": request}
if helper == "remote-start":
    command = request["argv"]
    attempt = request["attempt_id"]
    operation = request["operation_id"]
    unit = f"matrix-p13-op-{attempt}-{operation}.service"
    cgroup = "/system.slice/" + unit
    event["unit"] = unit
    event["lifecycle"] = (
        "ARMED" if "/usr/bin/systemd-run" in command else
        "MUTATION" if any(str(item).endswith("/release_bundle.py") for item in command) else "CHECK"
    )
    state_path = root / "remote-operations.json"
    operations = json.loads(state_path.read_text()) if state_path.exists() else {}
    current = operations.get(operation)
    terminal_stdout = ""
    if event["lifecycle"] == "CHECK":
        check = command[command.index("--check") + 1]
        mode = command[command.index("--mode") + 1]
        evidence_id = command[command.index("--evidence-id") + 1]
        acceptance_operation_id = command[command.index("--acceptance-operation-id") + 1]
        assert command.count("--acceptance-operation-id") == 1
        assert acceptance_operation_id == os.environ["EXPECTED_ACCEPTANCE_OPERATION_ID"]
        expected_sha = command[command.index("--expected-sha") + 1]
        expected_version = command[command.index("--expected-version") + 1]
        assert expected_sha == ("b" * 40 if mode == "rollback" else "a" * 40)
        assert expected_version == ("0.3.57-preview" if mode == "rollback" else "0.3.58-preview")
        payload = {"schema": 1, "check": check, "attempt_id": attempt,
                   "acceptance_operation_id": acceptance_operation_id,
                   "evidence_id": evidence_id}
        digest = "e" * 64
        if check == "protected-parity":
            protected = {"files": {"app.py": digest}, "modes": {"app.py": "0644"}}
            payload.update(source_sha="b" * 40 if mode == "rollback" else "a" * 40,
                           version="0.3.57-preview" if mode == "rollback" else "0.3.58-preview",
                           installed_digest=hashlib.sha256(json.dumps(
                               protected, sort_keys=True, separators=(",", ":")
                           ).encode()).hexdigest(), **protected)
        elif check == "production-tests":
            payload.update(
                focused={"passed": True, "duration_ms": 1, "command_digest": digest,
                         "stderr_digest": digest, "stderr_size": 0},
                full={"passed": True, "duration_ms": 2, "command_digest": digest,
                      "stderr_digest": digest, "stderr_size": 0},
            )
        elif check == "persistent-state":
            payload.update(config=digest, paired=digest, media=digest)
        elif check in {"physical-journey", "rollback-parity"}:
            verifier_path = root / "export/scripts/verify_priority_1_3_remote.py"
            spec = importlib.util.spec_from_file_location("fixture_remote_verifier", verifier_path)
            assert spec is not None and spec.loader is not None
            remote_verifier = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(remote_verifier)
            verifier_arguments = remote_verifier.build_parser().parse_args(command[4:])
            if check == "physical-journey":
                class ActiveService:
                    returncode = 0
                    stdout = "active\n"
                    stderr = ""
                remote_verifier.subprocess.run = lambda *args, **kwargs: ActiveService()
            payload = remote_verifier.run(verifier_arguments)
        terminal_stdout = json.dumps(payload, separators=(",", ":"))
    canonical = {"request": request, "unit": unit, "cgroup": cgroup, "inspections": 0,
                 "exit_status": 0, "stdout": terminal_stdout, "stderr": ""}
    if current is None:
        operations[operation] = canonical
        state_path.write_text(json.dumps(operations, sort_keys=True))
    else:
        assert current["request"] == request and current["unit"] == unit and current["cgroup"] == cgroup
    result = {"returncode": 0, "unit": unit, "cgroup": cgroup}
elif helper == "remote-operation-inspect":
    state_path = root / "remote-operations.json"
    operations = json.loads(state_path.read_text())
    operation = request["operation_id"]
    current = operations[operation]
    assert all(current[field] == request[field] for field in ("unit", "cgroup"))
    assert current["request"]["attempt_id"] == request["attempt_id"]
    assert current["request"]["registry"] == request["registry"]
    current["inspections"] += 1
    operations[operation] = current
    state_path.write_text(json.dumps(operations, sort_keys=True))
    common = {
        "returncode": 0, "attempt_id": request["attempt_id"], "operation_id": operation,
        "registry": request["registry"], "unit": request["unit"], "cgroup": request["cgroup"],
        "registry_digest": hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest(),
    }
    if current["inspections"] == 1:
        event["lifecycle"] = "INSPECT_RUNNING"
        result = {**common, "state": "RUNNING"}
    else:
        event["lifecycle"] = "INSPECT_TERMINAL"
        digest = _operation_output_digest(
            request["attempt_id"], operation, request["registry"], request["unit"], request["cgroup"],
            current["exit_status"], current["stdout"], current["stderr"],
        )
        result = {**common, "state": "TERMINAL", "exit_status": current["exit_status"],
                  "stdout": current["stdout"], "stderr": current["stderr"], "output_digest": digest}
else:
    digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result = {"returncode": 0, "digest": digest}
    if helper.startswith("remote-transaction-"):
        controlled = dispatch_privileged(helper, request)
        result = {"returncode": controlled.returncode, "digest": controlled.digest,
                  "terminal": controlled.terminal}
        event["lifecycle"] = "TRANSACTION"
    elif helper == "remote-commit":
        state_path = root / "remote-commits.json"
        terminals = json.loads(state_path.read_text()) if state_path.exists() else {}
        attempt = request["attempt_id"]
        if request.get("query") is True:
            event["lifecycle"] = "COMMIT_QUERY"
            terminal = terminals.get(attempt)
            if terminal is None:
                result = {"returncode": 3}
            else:
                stored = terminal["request"]
                fields = ("schema", "boundary", "phase", "attempt_id", "operation_id", "registry",
                          "evidence_digest", "success_record")
                if any(request.get(field) != stored.get(field) for field in fields):
                    result = {"returncode": 2}
                else:
                    result = {"returncode": 0, "digest": terminal["result_digest"], "terminal": terminal}
        elif attempt in terminals:
            event["lifecycle"] = "COMMIT_REPLAY"
            terminal = terminals[attempt]
            result = ({"returncode": 0, "digest": terminal["result_digest"], "terminal": terminal}
                      if terminal["request"] == request else {"returncode": 2})
        else:
            event["lifecycle"] = "DISARMED_COMMITTED"
            success = {
                "schema": 1, "boundary": request["boundary"], "phase": request["phase"],
                "attempt_id": attempt, "operation_id": request["operation_id"],
                "evidence_digest": request["evidence_digest"],
                "pending_note_id": request["pending_note_id"],
            }
            success_bytes = (json.dumps(success, sort_keys=True, separators=(",", ":")) + "\n").encode()
            result_digest = hashlib.sha256(success_bytes).hexdigest()
            terminal = {
                "schema": 1, "request": request, "request_digest": digest,
                "result_digest": result_digest, "committed_epoch": time.time(),
            }
            terminals[attempt] = terminal
            state_path.write_text(json.dumps(terminals, sort_keys=True))
            result = {"returncode": 0, "digest": result_digest, "terminal": terminal}
    else:
        event["lifecycle"] = "CONTROL"
with (root / "transport.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(event, sort_keys=True) + "\n")
if result["returncode"] == 0:
    print(json.dumps(result, separators=(",", ":")))
raise SystemExit(result["returncode"])
