import base64
import hashlib
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from deploy import install_priority_1_3_remote as installer
from deploy import matrix_priority_1_3_remote as remote


class Priority13RemoteBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        (self.source / "deploy").mkdir(parents=True)
        self.sources = {
            "dispatcher": self.source / "deploy/matrix_priority_1_3_remote.py",
            "guarded": self.source / "deploy/guarded_priority_1_3_release.py",
            "argparse": self.source / "release_argparse.py",
        }
        repository = Path(__file__).resolve().parents[1]
        reviewed_sources = {
            "dispatcher": repository / "deploy/matrix_priority_1_3_remote.py",
            "guarded": repository / "deploy/guarded_priority_1_3_release.py",
            "argparse": repository / "release_argparse.py",
        }
        for name, path in self.sources.items():
            path.write_bytes(reviewed_sources[name].read_bytes())
        key_blob = (
            len(b"ssh-ed25519").to_bytes(4, "big") + b"ssh-ed25519"
            + (32).to_bytes(4, "big") + bytes(range(32))
        )
        self.public_key = self.root / "release.pub"
        self.public_key.write_text(
            "ssh-ed25519 " + base64.b64encode(key_blob).decode("ascii")
            + " coordinator\n", encoding="ascii",
        )
        self.install_root = self.root / "appliance"
        (self.install_root / "root/.ssh").mkdir(parents=True)
        (self.install_root / "root/.ssh").chmod(0o700)
        self.authorized = self.install_root / "root/.ssh/authorized_keys"
        self.unrelated = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEhpc3RvcmljYWxLZXlCeXRlc1BhZGRlZA== unrelated"
        self.authorized.write_text(self.unrelated + "\n", encoding="ascii")
        self.authorized.chmod(0o600)
        self.digests = {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in self.sources.items()
        }
        self.source_restriction = "192.168.1.123/32"

    def tearDown(self):
        self.temporary.cleanup()

    def test_forced_command_is_exact_and_executes_only_fixed_reviewed_handler(self):
        payload = "Abc_123-x"
        original = (
            "/usr/local/libexec/matrix-priority-1-3-remote remote-transaction-inspect "
            f"--request-b64 {payload}"
        )
        argv, environment = remote.build_exec(original)
        self.assertEqual(argv, [
            "/home/admin/matrix/venv/bin/python", "-I", "-B",
            "/usr/local/lib/matrix-priority-1-3/deploy/guarded_priority_1_3_release.py",
            "remote-transaction-inspect", "--request-b64", payload,
        ])
        self.assertEqual(environment, {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"})
        with mock.patch.object(remote.os, "execve", side_effect=OSError("fixture")) as execute:
            self.assertEqual(remote.main(environment={"SSH_ORIGINAL_COMMAND": original}), 2)
        execute.assert_called_once_with(argv[0], argv, environment)
        for invalid in (
            "", "/bin/sh", original + "; id", original + " extra",
            original.replace("remote-transaction-inspect", "run"),
            original.replace(payload, "bad+payload"),
            original.replace("/usr/local/libexec/matrix-priority-1-3-remote", "/tmp/helper"),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    remote.build_exec(invalid)

    def test_digest_pinned_install_is_idempotent_and_preserves_unrelated_keys(self):
        result = installer.install(
            self.source, self.install_root, self.public_key, self.digests,
            self.source_restriction,
        )
        self.assertEqual(result["status"], "installed")
        expected = {
            "usr/local/libexec/matrix-priority-1-3-remote": (self.sources["dispatcher"], 0o755),
            "usr/local/lib/matrix-priority-1-3/deploy/guarded_priority_1_3_release.py": (self.sources["guarded"], 0o644),
            "usr/local/lib/matrix-priority-1-3/release_argparse.py": (self.sources["argparse"], 0o644),
        }
        inodes = {}
        for relative, (source, mode) in expected.items():
            installed = self.install_root / relative
            self.assertEqual(installed.read_bytes(), source.read_bytes())
            self.assertEqual(stat.S_IMODE(installed.stat().st_mode), mode)
            inodes[relative] = installed.stat().st_ino
        lines = self.authorized.read_text(encoding="ascii").splitlines()
        self.assertEqual(lines[0], self.unrelated)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].startswith(
            f'from="{self.source_restriction}",' + installer.REQUIRED_KEY_OPTIONS
            + " ssh-ed25519 "
        ))
        self.assertEqual(installer.verify(
            self.source, self.install_root, self.public_key, self.digests,
            self.source_restriction,
        )["status"], "verified")
        self.assertEqual(installer.install(
            self.source, self.install_root, self.public_key, self.digests,
            self.source_restriction,
        )["status"], "already-installed")
        self.assertEqual(
            inodes,
            {relative: (self.install_root / relative).stat().st_ino for relative in expected},
        )
        isolated = subprocess.run(
            [sys.executable, "-I", "-B", str(
                self.install_root / installer._ARTIFACTS["guarded"][1]
            ), "--help"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=10, check=False,
        )
        self.assertEqual((isolated.returncode, isolated.stderr), (0, ""))

    def test_invalid_digest_or_unsafe_existing_key_and_destination_fail_without_mutation(self):
        before = self.authorized.read_bytes()
        wrong = dict(self.digests); wrong["guarded"] = "0" * 64
        with self.assertRaises(ValueError):
            installer.install(
                self.source, self.install_root, self.public_key, wrong,
                self.source_restriction,
            )
        self.assertEqual(self.authorized.read_bytes(), before)
        self.assertFalse((self.install_root / "usr/local/libexec/matrix-priority-1-3-remote").exists())

        self.authorized.write_text(
            self.authorized.read_text(encoding="ascii") + self.public_key.read_text(encoding="ascii"),
            encoding="ascii",
        )
        with self.assertRaises(ValueError):
            installer.install(
                self.source, self.install_root, self.public_key, self.digests,
                self.source_restriction,
            )
        self.assertFalse((self.install_root / "usr/local/libexec/matrix-priority-1-3-remote").exists())

        self.authorized.write_bytes(before)
        destination = self.install_root / "usr/local/libexec/matrix-priority-1-3-remote"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(self.public_key)
        with self.assertRaises(ValueError):
            installer.install(
                self.source, self.install_root, self.public_key, self.digests,
                self.source_restriction,
            )
        self.assertTrue(destination.is_symlink())

    def test_source_restriction_and_effective_sshd_policy_are_exact(self):
        for invalid in ("", "192.168.1.123", "192.168.1.0/24", "not-an-address"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                installer.install(
                    self.source, self.install_root, self.public_key, self.digests,
                    invalid,
                )
        effective = "\n".join((
            "authorizedkeysfile .ssh/authorized_keys",
            "authorizedkeyscommand none",
            "authorizedkeyscommanduser nobody",
            "permitrootlogin forced-commands-only",
            "trustedusercakeys none",
            "authorizedprincipalsfile none",
            "authorizedprincipalscommand none",
            "authorizedprincipalscommanduser nobody",
            "hostbasedauthentication no",
            "pubkeyauthentication yes",
            "pubkeyauthoptions none",
            "ignorerhosts yes",
        )) + "\n"
        self.assertEqual(
            installer.verify_effective_sshd(effective)["authorized_keys_file"],
            "/root/.ssh/authorized_keys",
        )
        for poisoned in (
            effective.replace("permitrootlogin forced-commands-only", "permitrootlogin yes"),
            effective + "authorizedkeysfile /tmp/keys\n",
            effective.replace("trustedusercakeys none", "trustedusercakeys /tmp/ca"),
        ):
            with self.assertRaises(ValueError):
                installer.verify_effective_sshd(poisoned)

    def test_all_privileged_parents_are_preflighted_before_any_artifact_publication(self):
        local = self.install_root / "usr/local"
        local.mkdir(parents=True)
        hostile = self.root / "hostile-libexec"; hostile.mkdir()
        (local / "libexec").symlink_to(hostile)
        with self.assertRaises(ValueError):
            installer.install(
                self.source, self.install_root, self.public_key, self.digests,
                self.source_restriction,
            )
        guarded = local / "lib/matrix-priority-1-3/deploy/guarded_priority_1_3_release.py"
        argparse_file = local / "lib/matrix-priority-1-3/release_argparse.py"
        self.assertFalse((local / "lib").exists())
        self.assertFalse(guarded.exists())
        self.assertFalse(argparse_file.exists())
        self.assertEqual(self.authorized.read_text(encoding="ascii"), self.unrelated + "\n")

    def test_authorization_append_rechecks_current_bytes_and_preserves_unrelated_update(self):
        desired, blob, _ = installer._desired_authorization(
            self.public_key, self.source_restriction,
        )
        installer._preflight_authorized_keys(self.authorized, desired, blob)
        concurrent = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKNvbmN1cnJlbnRLZXlCeXRlc1BhZGRlZA== concurrent"
        with self.authorized.open("a", encoding="ascii") as stream:
            stream.write(concurrent + "\n")
        installer._append_authorization(self.authorized, desired, blob)
        lines = self.authorized.read_text(encoding="ascii").splitlines()
        self.assertEqual(lines[:2], [self.unrelated, concurrent])
        self.assertEqual(len(lines), 3)

    def test_authorization_append_preserves_noncooperating_concurrent_writer(self):
        desired, blob, _ = installer._desired_authorization(
            self.public_key, self.source_restriction,
        )
        entered = threading.Event(); completed = threading.Event()
        concurrent = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKNvbmN1cnJlbnRLZXlCeXRlc1BhZGRlZA== concurrent"
        original = installer._authorization_state

        def append_unrelated():
            self.assertTrue(entered.wait(5))
            with self.authorized.open("a", encoding="ascii") as stream:
                stream.write(concurrent + "\n"); stream.flush(); os.fsync(stream.fileno())
            completed.set()

        def synchronized_state(current, expected, key_blob):
            entered.set()
            self.assertTrue(completed.wait(5))
            return original(current, expected, key_blob)

        writer = threading.Thread(target=append_unrelated)
        writer.start()
        try:
            with mock.patch.object(installer, "_authorization_state", side_effect=synchronized_state):
                installer._append_authorization(self.authorized, desired, blob)
        finally:
            writer.join(5)
        self.assertFalse(writer.is_alive())
        lines = self.authorized.read_text(encoding="ascii").splitlines()
        self.assertEqual(lines, [self.unrelated, concurrent, desired.decode("ascii").rstrip()])

    def test_active_forced_key_with_no_runtime_creates_nothing(self):
        desired, _, _ = installer._desired_authorization(
            self.public_key, self.source_restriction,
        )
        with self.authorized.open("ab") as stream:
            stream.write(desired)
        before = sorted(path.relative_to(self.install_root) for path in self.install_root.rglob("*"))
        with self.assertRaises(ValueError):
            installer.install(
                self.source, self.install_root, self.public_key, self.digests,
                self.source_restriction,
            )
        after = sorted(path.relative_to(self.install_root) for path in self.install_root.rglob("*"))
        self.assertEqual(after, before)
        self.assertFalse((self.install_root / "usr").exists())

    def test_unsafe_existing_endpoint_failure_creates_no_sibling_ancestry(self):
        endpoint = self.install_root / installer._ARTIFACTS["dispatcher"][1]
        endpoint.parent.mkdir(parents=True)
        endpoint.write_text("hostile", encoding="ascii"); endpoint.chmod(0o777)
        before = sorted(path.relative_to(self.install_root) for path in self.install_root.rglob("*"))
        with self.assertRaises(ValueError):
            installer.install(
                self.source, self.install_root, self.public_key, self.digests,
                self.source_restriction,
            )
        after = sorted(path.relative_to(self.install_root) for path in self.install_root.rglob("*"))
        self.assertEqual(after, before)
        self.assertFalse((self.install_root / "usr/local/lib").exists())

    def test_active_forced_key_refuses_incomplete_runtime_repair(self):
        installer.install(
            self.source, self.install_root, self.public_key, self.digests,
            self.source_restriction,
        )
        guarded = self.install_root / installer._ARTIFACTS["guarded"][1]
        argparse_file = self.install_root / installer._ARTIFACTS["argparse"][1]
        guarded.unlink(); argparse_file.unlink()
        with self.assertRaises(ValueError):
            installer.install(
                self.source, self.install_root, self.public_key, self.digests,
                self.source_restriction,
            )
        self.assertFalse(guarded.exists())
        self.assertFalse(argparse_file.exists())

    def test_world_writable_privileged_ancestry_is_rejected_by_install_and_verify(self):
        local = self.install_root / "usr/local"; local.mkdir(parents=True)
        library = local / "lib"; library.mkdir(); library.chmod(0o777)
        libexec = local / "libexec"; libexec.mkdir(); libexec.chmod(0o777)
        with self.assertRaises(ValueError):
            installer.install(
                self.source, self.install_root, self.public_key, self.digests,
                self.source_restriction,
            )
        library.chmod(0o755); libexec.chmod(0o755)
        installer.install(
            self.source, self.install_root, self.public_key, self.digests,
            self.source_restriction,
        )
        library.chmod(0o777)
        with self.assertRaises(ValueError):
            installer.verify(
                self.source, self.install_root, self.public_key, self.digests,
                self.source_restriction,
            )

    def test_writable_root_ancestry_is_rejected_before_install_and_by_verify(self):
        root_ancestor = self.install_root / "root"
        root_ancestor.chmod(0o777)
        before = self.authorized.read_bytes()
        with self.assertRaises(ValueError):
            installer.install(
                self.source, self.install_root, self.public_key, self.digests,
                self.source_restriction,
            )
        self.assertEqual(self.authorized.read_bytes(), before)
        self.assertFalse((self.install_root / "usr").exists())
        root_ancestor.chmod(0o755)
        installer.install(
            self.source, self.install_root, self.public_key, self.digests,
            self.source_restriction,
        )
        root_ancestor.chmod(0o777)
        with self.assertRaises(ValueError):
            installer.verify(
                self.source, self.install_root, self.public_key, self.digests,
                self.source_restriction,
            )

    def test_live_sshd_validation_uses_fixed_binary_and_complete_context(self):
        effective = "\n".join((
            "authorizedkeysfile .ssh/authorized_keys", "authorizedkeyscommand none",
            "authorizedkeyscommanduser nobody", "permitrootlogin forced-commands-only",
            "trustedusercakeys none", "authorizedprincipalsfile none",
            "authorizedprincipalscommand none", "authorizedprincipalscommanduser nobody",
            "hostbasedauthentication no", "pubkeyauthentication yes",
            "pubkeyauthoptions none", "ignorerhosts yes",
        )) + "\n"
        completed = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, effective, ""),
        ]
        with mock.patch.object(installer.subprocess, "run", side_effect=completed) as run:
            result = installer.verify_live_sshd(
                source_address="192.168.1.123", source_host="coordinator.home.lan",
                local_address="192.168.1.4", local_port=22, rdomain="none",
            )
        self.assertEqual(result["permit_root_login"], "forced-commands-only")
        self.assertEqual(run.call_args_list[0].args[0], ["/usr/sbin/sshd", "-t"])
        self.assertEqual(run.call_args_list[1].args[0], [
            "/usr/sbin/sshd", "-T", "-C",
            "user=root,addr=192.168.1.123,host=coordinator.home.lan,"
            "laddr=192.168.1.4,lport=22,rdomain=none",
        ])

    def test_cli_enforces_live_sshd_before_bootstrap_operation(self):
        arguments = [
            "install", "--source-root", str(self.source),
            "--install-root", str(self.install_root),
            "--public-key", str(self.public_key),
            "--source-restriction", self.source_restriction,
            "--sshd-source-address", "192.168.1.123",
            "--sshd-source-host", "coordinator.home.lan",
            "--sshd-local-address", "192.168.1.4",
            "--sshd-local-port", "22", "--sshd-rdomain", "none",
            *sum((["--expected-sha256", f"{name}={digest}"]
                  for name, digest in self.digests.items()), []),
        ]
        order = []
        with mock.patch.object(installer.os, "geteuid", return_value=0), \
                mock.patch.object(installer, "verify_live_sshd",
                                  side_effect=lambda **_: order.append("sshd")), \
                mock.patch.object(installer, "install",
                                  side_effect=lambda *_: order.append("install") or {"status": "installed"}), \
                mock.patch("builtins.print"):
            self.assertEqual(installer.main(arguments), 0)
        self.assertEqual(order, ["sshd", "install"])
        with mock.patch.object(installer.os, "geteuid", return_value=0), \
                mock.patch.object(installer, "verify_live_sshd", side_effect=ValueError("unsafe")), \
                mock.patch.object(installer, "install") as operation:
            self.assertEqual(installer.main(arguments), 2)
        operation.assert_not_called()

    def test_bootstrap_value_flags_are_store_once(self):
        base = [
            "install", "--source-root", str(self.source),
            "--install-root", str(self.install_root),
            "--public-key", str(self.public_key),
            "--source-restriction", self.source_restriction,
            "--sshd-source-address", "192.168.1.123",
            "--sshd-source-host", "coordinator.home.lan",
            "--sshd-local-address", "192.168.1.4",
            "--sshd-local-port", "22",
            "--sshd-rdomain", "none",
            *sum((["--expected-sha256", f"{name}={digest}"]
                  for name, digest in self.digests.items()), []),
        ]
        for option, value in (
            ("--source-root", str(self.source)),
            ("--install-root", str(self.install_root)),
            ("--public-key", str(self.public_key)),
            ("--source-restriction", self.source_restriction),
            ("--sshd-source-address", "192.168.1.123"),
            ("--sshd-source-host", "coordinator.home.lan"),
            ("--sshd-local-address", "192.168.1.4"),
            ("--sshd-local-port", "22"),
            ("--sshd-rdomain", "none"),
        ):
            with self.subTest(option=option), self.assertRaises(SystemExit) as raised:
                installer.build_parser().parse_args([*base, option, value])
            self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
