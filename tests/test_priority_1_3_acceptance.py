import importlib
import json
import tempfile
import unittest
from pathlib import Path


class Priority13AcceptanceTests(unittest.TestCase):
    def _module(self):
        return importlib.import_module("deploy.verify_priority_1_3_acceptance")

    def test_b1_deploy_and_rollback_acceptance_is_exact_fail_closed_and_sanitized(self):
        acceptance = self._module()
        required = [
            "--boundary", "b1", "--mode", "deploy",
            "--ssh-target", "admin@192.0.2.10",
            "--ssh-identity-file", "/pins/id",
            "--ssh-known-hosts-file", "/pins/known_hosts",
            "--app-root", "/srv/matrix", "--expected-sha", "a" * 40,
            "--expected-version", "0.3.58-preview",
            "--session-cookie-file", "/pins/cookie",
            "--evidence-file", "/evidence/result.json",
        ]
        parsed = acceptance.build_parser().parse_args(required)
        self.assertEqual((parsed.boundary, parsed.mode), ("b1", "deploy"))
        for missing in ("--ssh-target", "--expected-sha", "--evidence-file"):
            broken = required[:]
            index = broken.index(missing)
            del broken[index:index + 2]
            with self.subTest(missing=missing), self.assertRaises(SystemExit):
                acceptance.build_parser().parse_args(broken)

        class FakeExecutor:
            def __init__(self, fail=False):
                self.fail = fail
                self.calls = []
            def run_remote(self, attempt_id, operation_id, argv, *, run_as, operation_timeout):
                self.calls.append((attempt_id, operation_id, tuple(argv), run_as, operation_timeout))
                if self.fail:
                    return acceptance.RemoteResult(1, "", "token=top-secret")
                payloads = {
                    "protected-parity": {"sha": "a" * 40, "version": "0.3.58-preview"},
                    "production-tests": {"focused": 12, "full": 300},
                    "physical-journey": {"draft_signature": "d" * 64, "saved_signature": "s" * 64, "width": 64, "height": 32, "owner_generation": 3, "foreign_rejected": True, "timeout_returned": True, "sleep_returned": True},
                    "persistent-state": {"config": "c" * 64, "paired": "p" * 64, "media": "m" * 64},
                    "rollback-parity": {"mode": "deploy", "saved_output": True},
                }
                return acceptance.RemoteResult(0, json.dumps(payloads[operation_id]), "")

        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "candidate.json"
            parsed.evidence_file = str(evidence)
            executor = FakeExecutor()
            result = acceptance.run_acceptance(parsed, executor)
            self.assertEqual(result, 0)
            document = json.loads(evidence.read_text())
            self.assertEqual(set(document), {"schema", "candidate_evidence"})
            self.assertEqual(document["schema"], 1)
            self.assertNotIn("secret", evidence.read_text().lower())
            self.assertNotIn("cookie", evidence.read_text().lower())
            self.assertEqual([call[1] for call in executor.calls], list(acceptance.B1_REMOTE_CHECKS))
            self.assertTrue(all(call[3] == "admin" and call[4] == 10 for call in executor.calls))

            evidence.unlink()
            failing = FakeExecutor(fail=True)
            self.assertNotEqual(acceptance.run_acceptance(parsed, failing), 0)
            self.assertFalse(evidence.exists())

    def test_pinned_remote_executor_ignores_poisoned_agent_and_config_and_has_no_alternate_path(self):
        acceptance = self._module()
        import os
        import subprocess
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = root / "identity"
            known_hosts = root / "known_hosts"
            identity.write_text("private pin")
            known_hosts.write_text("host pin")
            identity.chmod(0o600)
            known_hosts.chmod(0o600)
            poisoned_home = root / "home"
            (poisoned_home / ".ssh").mkdir(parents=True)
            (poisoned_home / ".ssh/config").write_text(
                "Host *\n  ProxyCommand /tmp/steal\n  IdentityAgent /tmp/agent\n"
            )
            captured = {}
            def runner(argv, **kwargs):
                captured["argv"] = tuple(argv)
                captured["kwargs"] = kwargs
                return subprocess.CompletedProcess(argv, 0, '{"ok":true}', "")
            poisoned = {
                "HOME": str(poisoned_home), "SSH_AUTH_SOCK": "/tmp/poison-agent",
                "GIT_SSH_COMMAND": "ssh -F /tmp/poison", "ALL_PROXY": "socks5://poison",
            }
            with mock.patch.dict(os.environ, poisoned, clear=False):
                executor = acceptance.PinnedRemoteExecutor(
                    "admin@192.0.2.10", identity, known_hosts, runner=runner
                )
                result = executor.run_remote(
                    "a" * 32, "op-1", ["/usr/bin/python3", "-I", "check.py"],
                    run_as="admin", operation_timeout=9,
                )
            self.assertIsInstance(result, acceptance.RemoteResult)
            argv = captured["argv"]
            self.assertEqual(argv[:3], ("ssh", "-F", "/dev/null"))
            for exact in (
                "BatchMode=yes", "IdentitiesOnly=yes", "IdentityAgent=none",
                "StrictHostKeyChecking=yes", f"UserKnownHostsFile={known_hosts}",
                "ConnectTimeout=10",
            ):
                self.assertIn(exact, argv)
            self.assertIn(str(identity), argv)
            self.assertEqual(argv[-3:-1], ("remote-start", "--request-b64"))
            self.assertNotIn("/tmp/poison", " ".join(argv))
            self.assertFalse(captured["kwargs"].get("shell", False))
            child_env = captured["kwargs"]["env"]
            self.assertNotIn("SSH_AUTH_SOCK", child_env)
            self.assertNotIn("GIT_SSH_COMMAND", child_env)
            self.assertNotIn("ALL_PROXY", child_env)
            self.assertFalse(hasattr(executor, "run_local"))
            with self.assertRaises(ValueError):
                executor.control_remote("arbitrary", {}, operation_timeout=5)
            identity_link = root / "identity-link"
            identity_link.symlink_to(identity)
            with self.assertRaises(ValueError):
                acceptance.PinnedRemoteExecutor(
                    "admin@192.0.2.10", identity_link, known_hosts, runner=runner
                )
