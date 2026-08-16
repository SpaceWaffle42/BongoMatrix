import contextlib
import hashlib
import importlib.util
import io
import os
import re
import subprocess
import stat
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_deploy_module(name):
    path = ROOT / "deploy" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verifier_report(sha, pipeline_id, acceptance="PASS", error=None):
    lines = [
        f"SOURCE_SHA={sha}",
        f"PIPELINE_ID={pipeline_id}",
        f"PHYSICAL_DEVICE_ACCEPTANCE={acceptance}",
    ]
    if error is not None:
        lines.append(f"ERROR={error}")
    return ("\n".join(lines) + "\n").encode("ascii")


class FakeDriver:
    width = 64
    height = 32
    brightness = 73


class FakeMatrix:
    def __init__(self, events, fail_on=None):
        self.matrix = FakeDriver()
        self.events = events
        self.fail_on = fail_on

    def fill(self, red, green, blue):
        self.events.append(("fill", red, green, blue))

    def swap(self):
        self.events.append(("swap",))

    def clear(self):
        self.events.append(("clear",))

    def set_brightness(self, value):
        if self.fail_on == value:
            raise InterruptedError("terminated")
        self.matrix.brightness = max(1, min(100, int(value)))
        self.events.append(("brightness", value, self.matrix.brightness))


class FakeLock:
    def __init__(self, events, error=None):
        self.events = events
        self.error = error

    def __enter__(self):
        self.events.append(("lock", "acquire"))
        if self.error:
            raise self.error
        return self

    def __exit__(self, *_):
        self.events.append(("lock", "release"))


class FakeOps:
    def __init__(self, events):
        self.events = events
        self.active = True
        self.http = True
        self.before = {
            "boot": "boot-a",
            "data": "data-a",
            "media": "media-a",
            "restarts": "0",
        }

    def service_active(self):
        self.events.append(("service", "active?"))
        return self.active

    def http_ready(self):
        self.events.append(("http", "ready?"))
        return self.http

    def snapshot(self):
        self.events.append(("snapshot",))
        return dict(self.before)

    def stop_service(self):
        self.events.append(("service", "stop"))
        self.active = False

    def start_service(self):
        self.events.append(("service", "start"))
        self.active = True

    def wait_ready(self):
        self.events.append(("service", "wait-ready"))
        if not self.active or not self.http:
            raise RuntimeError("service recovery failed")


class PhysicalVerifierTests(unittest.TestCase):
    def test_durable_claim_is_the_only_admission_trigger_and_cannot_race_or_deadlock(self):
        unit = (ROOT / "deploy" / "matrix-ci-physical.service").read_text(encoding="utf-8")
        path_unit = (ROOT / "deploy" / "matrix-ci-physical.path").read_text(encoding="utf-8")
        dispatcher_source = (ROOT / "deploy" / "matrix_ci_physical_dispatcher.py").read_text(encoding="utf-8")
        self.assertIn("PathExists=/run/matrix-ci-physical/claim", path_unit)
        self.assertNotIn("flock", unit)
        self.assertNotIn("service.lock", unit + dispatcher_source)
        self.assertNotRegex(dispatcher_source, r'_systemctl\("start"')

        dispatcher = load_deploy_module("matrix_ci_physical_dispatcher")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-admission-", dir="/root") as directory:
            runtime = Path(directory) / "runtime"
            old_paths = dispatcher.RUNTIME, dispatcher.LABELS, dispatcher.REPORT
            dispatcher.RUNTIME = runtime
            dispatcher.LABELS = runtime / "claim"
            dispatcher.REPORT = runtime / "report"
            try:
                dispatcher.SystemOps().prepare("a" * 40, "1")
                report = runtime / "report"
                report.write_bytes(b"first verifier evidence\n")
                os.chmod(report, 0o600)
                with self.assertRaisesRegex(RuntimeError, "claim"):
                    dispatcher.SystemOps().prepare("b" * 40, "2")
                self.assertEqual(report.read_bytes(), b"first verifier evidence\n")
                self.assertEqual((runtime / "claim").read_text(encoding="ascii"), f"{'a' * 40} 1\n")
            finally:
                dispatcher.RUNTIME, dispatcher.LABELS, dispatcher.REPORT = old_paths

    def test_ssh_loss_after_claim_leaves_pid1_owned_queued_recovery(self):
        dispatcher = load_deploy_module("matrix_ci_physical_dispatcher")
        events = []

        class DisconnectedOps:
            def prepare(self, sha, pipeline):
                events.append(("durable-claim", sha, pipeline))

            def await_pid1_ownership(self):
                events.append(("pid1-path-queued",))
                raise RuntimeError("SSH transport vanished")

            def stop_and_wait(self):
                events.append(("caller-stop",))
                return 0

        with self.assertRaisesRegex(RuntimeError, "SSH transport vanished"):
            dispatcher.dispatch(f"verify {'b' * 40} 2", ops=DisconnectedOps())
        self.assertEqual(
            events,
            [("durable-claim", "b" * 40, "2"), ("pid1-path-queued",)],
        )

    def test_dispatcher_rejects_symlinked_runtime_namespace_without_mutating_target(self):
        dispatcher = load_deploy_module("matrix_ci_physical_dispatcher")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-runtime-") as directory:
            root = Path(directory)
            target = root / "attacker-target"
            target.mkdir(mode=0o755)
            runtime = root / "runtime"
            runtime.symlink_to(target, target_is_directory=True)
            old_paths = (
                dispatcher.RUNTIME,
                dispatcher.LABELS,
                dispatcher.REPORT,
                dispatcher.DISPATCH_LOCK,
            )
            dispatcher.RUNTIME = runtime
            dispatcher.LABELS = runtime / "labels"
            dispatcher.REPORT = runtime / "report"
            dispatcher.DISPATCH_LOCK = runtime / "dispatch.lock"
            try:
                with self.assertRaises(RuntimeError):
                    dispatcher.open_dispatch_lock()
            finally:
                (
                    dispatcher.RUNTIME,
                    dispatcher.LABELS,
                    dispatcher.REPORT,
                    dispatcher.DISPATCH_LOCK,
                ) = old_paths
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
            self.assertEqual(list(target.iterdir()), [])

    def test_dispatcher_rejects_group_writable_runtime_ancestor(self):
        dispatcher = load_deploy_module("matrix_ci_physical_dispatcher")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-ancestor-", dir="/root") as directory:
            unsafe = Path(directory) / "unsafe"
            unsafe.mkdir(mode=0o777)
            os.chmod(unsafe, 0o777)
            parent = unsafe / "trusted-looking-parent"
            parent.mkdir(mode=0o700)
            runtime = parent / "runtime"
            old_paths = (
                dispatcher.RUNTIME,
                dispatcher.LABELS,
                dispatcher.REPORT,
                dispatcher.DISPATCH_LOCK,
            )
            dispatcher.RUNTIME = runtime
            dispatcher.LABELS = runtime / "labels"
            dispatcher.REPORT = runtime / "report"
            dispatcher.DISPATCH_LOCK = runtime / "dispatch.lock"
            try:
                with self.assertRaisesRegex(RuntimeError, "unsafe physical verifier runtime namespace"):
                    dispatcher.open_dispatch_lock()
            finally:
                (
                    dispatcher.RUNTIME,
                    dispatcher.LABELS,
                    dispatcher.REPORT,
                    dispatcher.DISPATCH_LOCK,
                ) = old_paths
            self.assertFalse(runtime.exists())

    def test_durable_claim_atomically_binds_labels_before_service_admission(self):
        dispatcher = load_deploy_module("matrix_ci_physical_dispatcher")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-claim-", dir="/root") as directory:
            runtime = Path(directory) / "runtime"
            old_paths = dispatcher.RUNTIME, dispatcher.LABELS, dispatcher.REPORT
            original_systemctl = dispatcher._systemctl
            dispatcher.RUNTIME = runtime
            dispatcher.LABELS = runtime / "claim"
            dispatcher.REPORT = runtime / "report"
            dispatcher._systemctl = lambda *_args, **_kwargs: types.SimpleNamespace(returncode=0, stdout=b"")
            try:
                dispatcher.SystemOps().prepare("a" * 40, "1")
                with self.assertRaisesRegex(RuntimeError, "claim"):
                    dispatcher.SystemOps().prepare("b" * 40, "2")
                self.assertEqual((runtime / "claim").read_text(encoding="ascii"), f"{'a' * 40} 1\n")
            finally:
                dispatcher.RUNTIME, dispatcher.LABELS, dispatcher.REPORT = old_paths
                dispatcher._systemctl = original_systemctl

    def test_claim_publication_loops_short_writes(self):
        dispatcher = load_deploy_module("matrix_ci_physical_dispatcher")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-short-write-", dir="/root") as directory:
            runtime = Path(directory)
            os.chmod(runtime, 0o700)
            old_paths = dispatcher.RUNTIME, dispatcher.LABELS, dispatcher.REPORT
            original_write = dispatcher.os.write
            dispatcher.RUNTIME = runtime
            dispatcher.LABELS = runtime / "claim"
            dispatcher.REPORT = runtime / "report"
            dispatcher.os.write = lambda fd, payload: original_write(fd, payload[:1])
            try:
                dispatcher.SystemOps().prepare("a" * 40, "123")
                self.assertEqual((runtime / "claim").read_bytes(), f"{'a' * 40} 123\n".encode())
            finally:
                dispatcher.os.write = original_write
                dispatcher.RUNTIME, dispatcher.LABELS, dispatcher.REPORT = old_paths

    def test_interrupted_claim_publication_never_leaves_a_wedged_two_link_claim(self):
        dispatcher = load_deploy_module("matrix_ci_physical_dispatcher")
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-claim-interrupt-", dir="/root") as directory:
            runtime = Path(directory)
            os.chmod(runtime, 0o700)
            old_dispatcher_paths = dispatcher.RUNTIME, dispatcher.LABELS, dispatcher.REPORT
            old_claim = verifier.CLAIM_FILE
            original_fsync = dispatcher.os.fsync
            dispatcher.RUNTIME = runtime
            dispatcher.LABELS = runtime / "claim"
            dispatcher.REPORT = runtime / "report"
            verifier.CLAIM_FILE = runtime / "claim"

            def interrupt_after_publication(fd):
                if (runtime / "claim").exists() and stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise KeyboardInterrupt("disconnect after publication")
                return original_fsync(fd)

            dispatcher.os.fsync = interrupt_after_publication
            try:
                with self.assertRaisesRegex(KeyboardInterrupt, "disconnect after publication"):
                    dispatcher.SystemOps().prepare("a" * 40, "1")
            finally:
                dispatcher.os.fsync = original_fsync

            try:
                claim = runtime / "claim"
                self.assertTrue(claim.exists())
                self.assertEqual(claim.stat().st_nlink, 1)
                verifier._remove_claim()
                dispatcher.SystemOps().prepare("b" * 40, "2")
                self.assertEqual(claim.read_text(encoding="ascii"), f"{'b' * 40} 2\n")
            finally:
                dispatcher.RUNTIME, dispatcher.LABELS, dispatcher.REPORT = old_dispatcher_paths
                verifier.CLAIM_FILE = old_claim

    def test_live_unique_claim_temporary_is_never_stolen(self):
        dispatcher = load_deploy_module("matrix_ci_physical_dispatcher")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-claim-recovery-", dir="/root") as directory:
            runtime = Path(directory)
            os.chmod(runtime, 0o700)
            old_paths = dispatcher.RUNTIME, dispatcher.LABELS, dispatcher.REPORT
            dispatcher.RUNTIME = runtime
            dispatcher.LABELS = runtime / "claim"
            dispatcher.REPORT = runtime / "report"
            temporary = runtime / f"claim.tmp.{os.getpid()}.0"
            old_sequence = dispatcher._TEMPORARY_SEQUENCE
            try:
                temporary.write_bytes(b"")
                os.chmod(temporary, 0o600)
                dispatcher._TEMPORARY_SEQUENCE = iter((0, 1))
                dispatcher.SystemOps().prepare("b" * 40, "2")
                self.assertTrue(temporary.exists())
                self.assertEqual((runtime / "claim").read_text(encoding="ascii"), f"{'b' * 40} 2\n")
            finally:
                dispatcher._TEMPORARY_SEQUENCE = old_sequence
                dispatcher.RUNTIME, dispatcher.LABELS, dispatcher.REPORT = old_paths

    def test_pid1_serialized_scavenging_converges_without_touching_live_or_hostile_nodes(self):
        dispatcher = load_deploy_module("matrix_ci_physical_dispatcher")
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-scavenge-", dir="/root") as directory:
            runtime = Path(directory)
            os.chmod(runtime, 0o700)
            target = runtime / "target"
            target.write_bytes(b"preserve\n")
            os.chmod(target, 0o600)
            live = runtime / f"claim.tmp.{os.getpid()}.1"
            live.write_bytes(b"live")
            os.chmod(live, 0o600)
            stale_claim = runtime / "claim.tmp.999999.2"
            stale_claim.write_bytes(b"dead")
            os.chmod(stale_claim, 0o600)
            hostile = runtime / "claim.tmp.999998.3"
            hostile.symlink_to(target.name)
            stale_report = runtime / "report.tmp.999997.4"
            stale_report.write_bytes(b"dead")
            os.chmod(stale_report, 0o600)
            overflow = []
            for counter in range(130):
                path = runtime / f"report.tmp.999996.{counter + 10}"
                path.write_bytes(b"dead")
                os.chmod(path, 0o600)
                overflow.append(path)

            runtime_fd = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY)
            try:
                self.assertEqual(dispatcher._scavenge_stale_temporaries(runtime_fd, "claim.tmp", limit=128), 1)
                removed = verifier._scavenge_stale_temporaries(runtime_fd, "report.tmp", limit=128)
            finally:
                os.close(runtime_fd)
            self.assertFalse(stale_claim.exists())
            self.assertFalse(stale_report.exists())
            self.assertTrue(live.exists())
            self.assertTrue(hostile.is_symlink())
            self.assertEqual(target.read_bytes(), b"preserve\n")
            self.assertLessEqual(removed, 128)
            self.assertGreaterEqual(sum(path.exists() for path in overflow), 2)

    def test_unique_temporary_cleanup_failures_emit_fixed_diagnostics_without_masking_primary(self):
        modules = (
            load_deploy_module("matrix_ci_physical_dispatcher"),
            load_deploy_module("matrix_ci_physical_verifier"),
        )
        for module in modules:
            with self.subTest(module=module.__name__):
                with tempfile.TemporaryDirectory(prefix="matrix-ci-cleanup-", dir="/root") as directory:
                    runtime = Path(directory)
                    temporary = runtime / "report.tmp.999999.7"
                    temporary.write_bytes(b"secret-token-material")
                    os.chmod(temporary, 0o600)
                    info = temporary.stat()
                    identity = (info.st_dev, info.st_ino)
                    runtime_fd = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY)
                    original_unlink = module.os.unlink
                    primary = RuntimeError("primary-secret-token")
                    stderr = io.StringIO()

                    def fail_unlink(*_args, **_kwargs):
                        raise OSError("sensitive-path-and-os-detail")

                    module.os.unlink = fail_unlink
                    try:
                        with self.assertRaises(RuntimeError) as raised, contextlib.redirect_stderr(stderr):
                            try:
                                raise primary
                            except RuntimeError:
                                module._unlink_owned_temporary(runtime_fd, temporary.name, identity)
                                raise
                    finally:
                        module.os.unlink = original_unlink
                        os.close(runtime_fd)
                    self.assertIs(raised.exception, primary)
                    self.assertEqual(stderr.getvalue(), "physical verifier cleanup warning: temporary unlink failed\n")
                    self.assertNotIn("secret", stderr.getvalue())

    def test_unique_temporary_cleanup_directory_fsync_failure_is_reported_once(self):
        modules = (
            load_deploy_module("matrix_ci_physical_dispatcher"),
            load_deploy_module("matrix_ci_physical_verifier"),
        )
        for module in modules:
            with self.subTest(module=module.__name__):
                with tempfile.TemporaryDirectory(prefix="matrix-ci-cleanup-", dir="/root") as directory:
                    runtime = Path(directory)
                    temporary = runtime / "claim.tmp.999999.8"
                    temporary.write_bytes(b"private-label")
                    os.chmod(temporary, 0o600)
                    info = temporary.stat()
                    identity = (info.st_dev, info.st_ino)
                    runtime_fd = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY)
                    original_fsync = module.os.fsync
                    stderr = io.StringIO()

                    def fail_fsync(fd):
                        if fd == runtime_fd:
                            raise OSError("sensitive-fsync-detail")
                        return original_fsync(fd)

                    module.os.fsync = fail_fsync
                    try:
                        with contextlib.redirect_stderr(stderr):
                            result = module._unlink_owned_temporary(runtime_fd, temporary.name, identity)
                    finally:
                        module.os.fsync = original_fsync
                        os.close(runtime_fd)
                    self.assertIsNone(result)
                    self.assertFalse(temporary.exists())
                    self.assertEqual(stderr.getvalue(), "physical verifier cleanup warning: directory sync failed\n")
                    self.assertNotIn("sensitive", stderr.getvalue())

    def test_stale_temporary_scavenging_cleanup_diagnostics_are_bounded_and_preserve_count(self):
        modules = (
            load_deploy_module("matrix_ci_physical_dispatcher"),
            load_deploy_module("matrix_ci_physical_verifier"),
        )
        for module in modules:
            with self.subTest(module=module.__name__):
                with tempfile.TemporaryDirectory(prefix="matrix-ci-scavenge-warning-", dir="/root") as directory:
                    runtime = Path(directory)
                    for counter in (1, 2):
                        temporary = runtime / f"claim.tmp.999999.{counter}"
                        temporary.write_bytes(b"private")
                        os.chmod(temporary, 0o600)
                    runtime_fd = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY)
                    original_unlink = module.os.unlink
                    stderr = io.StringIO()

                    def fail_unlink(*_args, **_kwargs):
                        raise OSError("sensitive-unlink-detail")

                    module.os.unlink = fail_unlink
                    try:
                        with contextlib.redirect_stderr(stderr):
                            removed = module._scavenge_stale_temporaries(runtime_fd, "claim.tmp")
                    finally:
                        module.os.unlink = original_unlink
                        os.close(runtime_fd)
                    self.assertEqual(removed, 0)
                    self.assertEqual(stderr.getvalue(), "physical verifier cleanup warning: temporary unlink failed\n")

                with tempfile.TemporaryDirectory(prefix="matrix-ci-scavenge-warning-", dir="/root") as directory:
                    runtime = Path(directory)
                    temporary = runtime / "report.tmp.999999.1"
                    temporary.write_bytes(b"private")
                    os.chmod(temporary, 0o600)
                    runtime_fd = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY)
                    original_fsync = module.os.fsync
                    stderr = io.StringIO()
                    module.os.fsync = lambda _fd: (_ for _ in ()).throw(OSError("sensitive-sync-detail"))
                    try:
                        with contextlib.redirect_stderr(stderr):
                            removed = module._scavenge_stale_temporaries(runtime_fd, "report.tmp")
                    finally:
                        module.os.fsync = original_fsync
                        os.close(runtime_fd)
                    self.assertEqual(removed, 1)
                    self.assertEqual(stderr.getvalue(), "physical verifier cleanup warning: directory sync failed\n")

    def test_interrupted_claim_temporaries_cannot_wedge_future_admission(self):
        dispatcher = load_deploy_module("matrix_ci_physical_dispatcher")
        payload = f"{'a' * 40} 1\n".encode("ascii")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-label-", dir="/root") as directory:
            runtime = Path(directory)
            os.chmod(runtime, 0o700)
            old_paths = dispatcher.RUNTIME, dispatcher.LABELS, dispatcher.REPORT
            dispatcher.RUNTIME = runtime
            dispatcher.LABELS = runtime / "claim"
            dispatcher.REPORT = runtime / "report"
            try:
                for length in range(len(payload)):
                    temporary = runtime / "claim.tmp"
                    temporary.write_bytes(payload[:length])
                    os.chmod(temporary, 0o600)
                    with self.subTest(interrupted_length=length):
                        dispatcher.SystemOps().prepare("b" * 40, "2")
                        self.assertEqual((runtime / "claim").read_text(encoding="ascii"), f"{'b' * 40} 2\n")
                        self.assertTrue(temporary.exists())
                    (runtime / "claim").unlink(missing_ok=True)
                    temporary.unlink(missing_ok=True)
                target = runtime / "target"
                target.write_bytes(b"attacker\n")
                os.chmod(target, 0o600)
                for kind in ("symlink", "fifo", "hardlink"):
                    temporary = runtime / "claim.tmp"
                    if kind == "symlink":
                        temporary.symlink_to(target.name)
                    elif kind == "fifo":
                        os.mkfifo(temporary, 0o600)
                    else:
                        os.link(target, temporary)
                    with self.subTest(hostile_kind=kind):
                        dispatcher.SystemOps().prepare("c" * 40, "3")
                        self.assertEqual((runtime / "claim").read_text(encoding="ascii"), f"{'c' * 40} 3\n")
                        self.assertTrue(temporary.exists() or temporary.is_symlink())
                        self.assertEqual(target.read_bytes(), b"attacker\n")
                    (runtime / "claim").unlink(missing_ok=True)
                    temporary.unlink(missing_ok=True)
            finally:
                dispatcher.RUNTIME, dispatcher.LABELS, dispatcher.REPORT = old_paths

    def test_fresh_boot_lock_creation_survives_service_umask_for_both_implementations(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        from hardware_lock import DisplayOwnershipLock

        implementations = (DisplayOwnershipLock, verifier.FixedDisplayOwnershipLock)
        for implementation in implementations:
            with self.subTest(implementation=implementation.__name__):
                with tempfile.TemporaryDirectory(prefix="matrix-display-umask-", dir="/root") as directory:
                    parent = Path(directory) / "matrix-display"
                    parent.mkdir(mode=0o755)
                    os.chmod(parent, 0o755)
                    lock_path = parent / "matrix-display.lock"
                    previous = os.umask(0o077)
                    try:
                        with implementation(lock_path):
                            info = lock_path.stat()
                            self.assertEqual(stat.S_IMODE(info.st_mode), 0o644)
                            self.assertEqual(info.st_nlink, 1)
                    finally:
                        os.umask(previous)

    def test_host_owned_lock_inode_is_read_only_and_persistent_across_lifecycle(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        from hardware_lock import DisplayOwnershipLock

        tmpfiles = ROOT / "deploy" / "matrix-display.conf"
        self.assertTrue(tmpfiles.is_file(), "host tmpfiles policy must create the canonical lock")
        policy = tmpfiles.read_text(encoding="ascii")
        self.assertIn("d /run/matrix-display 0755 root root -", policy)
        self.assertIn("f /run/matrix-display/matrix-display.lock 0644 root root -", policy)
        for unit_name in ("matrix-web.service", "matrix-ci-physical.service"):
            unit = (ROOT / "deploy" / unit_name).read_text(encoding="utf-8")
            self.assertIn("ReadOnlyPaths=/run/matrix-display", unit)
            self.assertNotIn("ReadWritePaths=/run/matrix-display", unit)

        with tempfile.TemporaryDirectory(prefix="matrix-display-lifecycle-", dir="/root") as directory:
            parent = Path(directory) / "matrix-display"
            parent.mkdir(mode=0o755)
            os.chmod(parent, 0o755)
            lock_path = parent / "matrix-display.lock"
            lock_path.write_bytes(b"host-owned canonical inode\n")
            os.chmod(lock_path, 0o644)
            identity = (lock_path.stat().st_dev, lock_path.stat().st_ino)
            for implementation in (
                DisplayOwnershipLock,
                verifier.FixedDisplayOwnershipLock,
                DisplayOwnershipLock,
            ):
                with implementation(lock_path):
                    self.assertEqual((lock_path.stat().st_dev, lock_path.stat().st_ino), identity)
                    self.assertEqual(lock_path.read_bytes(), b"host-owned canonical inode\n")
                self.assertEqual((lock_path.stat().st_dev, lock_path.stat().st_ino), identity)

    def test_production_and_verifier_share_persistent_canonical_display_lock(self):
        web_unit = (ROOT / "deploy" / "matrix-web.service").read_text(encoding="utf-8")
        verifier_unit = (ROOT / "deploy" / "matrix-ci-physical.service").read_text(encoding="utf-8")
        verifier_source = (ROOT / "deploy" / "matrix_ci_physical_verifier.py").read_text(encoding="utf-8")

        self.assertNotIn("RuntimeDirectory=matrix-display", web_unit)
        self.assertNotIn("RuntimeDirectoryPreserve=yes", web_unit)
        install = (ROOT / "deploy" / "matrix-ci-physical-install.md").read_text(encoding="utf-8")
        self.assertIn(
            "install -o root -g root -m 0644 deploy/matrix-web.service /etc/systemd/system/matrix-web.service",
            install,
        )
        self.assertIn("MATRIX_LOCK_FILE=/run/matrix-display/matrix-display.lock", web_unit)
        self.assertIn('LOCK_FILE = "/run/matrix-display/matrix-display.lock"', verifier_source)
        self.assertIn("ReadOnlyPaths=/run/matrix-display", web_unit)
        self.assertIn("ReadOnlyPaths=/run/matrix-display", verifier_unit)
        self.assertIn("ReadWritePaths=/run/matrix-ci-physical", verifier_unit)
        self.assertNotIn("ReadWritePaths=/run/matrix-display", web_unit + verifier_unit)
        self.assertNotIn("matrix-ci-display.lock", web_unit + verifier_unit + verifier_source)

    def test_tmpfiles_install_contract_is_exact_ordered_and_verified(self):
        policy_path = ROOT / "deploy" / "matrix-display.conf"
        policy = policy_path.read_text(encoding="ascii")
        self.assertEqual(
            policy,
            "# Host-owned persistent display arbitration inode; services receive it read-only.\n"
            "d /run/matrix-display 0755 root root -\n"
            "f /run/matrix-display/matrix-display.lock 0644 root root -\n",
        )
        install = (ROOT / "deploy" / "matrix-ci-physical-install.md").read_text(encoding="utf-8")
        install_line = (
            "install -o root -g root -m 0644 deploy/matrix-display.conf "
            "/etc/tmpfiles.d/matrix-display.conf"
        )
        create_line = "/usr/bin/systemd-tmpfiles --create /etc/tmpfiles.d/matrix-display.conf"
        self.assertIn(install_line, install)
        self.assertIn(create_line, install)
        self.assertLess(install.index(install_line), install.index(create_line))
        self.assertLess(install.index(create_line), install.index("systemctl enable --now"))
        self.assertIn("deploy/matrix-display.conf:/etc/tmpfiles.d/matrix-display.conf", install)
        self.assertIn("does not replace an existing inode", install)

        gate = (ROOT / "deploy" / "verify_matrix_ci_physical_install.py").read_text(encoding="utf-8")
        for required in (
            "/etc/tmpfiles.d/matrix-display.conf",
            "ReadOnlyPaths",
            "Requires",
            "After",
            "UMask",
            "/run/matrix-display/matrix-display.lock",
        ):
            self.assertIn(required, gate)

    def test_web_service_cannot_access_physical_ci_namespace(self):
        web_unit = (ROOT / "deploy" / "matrix-web.service").read_text(encoding="utf-8")
        self.assertRegex(web_unit, r"(?m)^InaccessiblePaths=/run/matrix-ci-physical$")

    def test_verifier_report_publication_rejects_prepositioned_links_without_mutation(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        values = {
            "SOURCE_SHA": "a" * 40,
            "PIPELINE_ID": "99",
            "PHYSICAL_DEVICE_ACCEPTANCE": "PASS",
        }
        with tempfile.TemporaryDirectory(prefix="matrix-ci-report-", dir="/root") as directory:
            runtime = Path(directory)
            os.chmod(runtime, 0o700)
            report = runtime / "report"
            target = runtime / "target"
            target.write_bytes(b"attacker-owned\n")
            os.chmod(target, 0o600)
            old_report = verifier.REPORT_FILE
            verifier.REPORT_FILE = report
            try:
                for link_kind in ("symlink", "hardlink"):
                    with self.subTest(link_kind=link_kind):
                        if report.exists() or report.is_symlink():
                            report.unlink()
                        if link_kind == "symlink":
                            report.symlink_to(target.name)
                        else:
                            os.link(target, report)
                        with self.assertRaises(RuntimeError):
                            verifier._write_report(values)
                        self.assertEqual(target.read_bytes(), b"attacker-owned\n")
            finally:
                verifier.REPORT_FILE = old_report

    def test_dispatcher_cannot_observe_report_until_complete_payload_is_published(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        dispatcher = load_deploy_module("matrix_ci_physical_dispatcher")
        values = {
            "SOURCE_SHA": "a" * 40,
            "PIPELINE_ID": "99",
            "PHYSICAL_DEVICE_ACCEPTANCE": "PASS",
        }
        expected = verifier_report("a" * 40, "99")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-report-race-", dir="/root") as directory:
            runtime = Path(directory)
            os.chmod(runtime, 0o700)
            old_report = verifier.REPORT_FILE
            old_dispatcher_paths = dispatcher.RUNTIME, dispatcher.REPORT
            original_write = verifier.os.write
            original_systemctl = dispatcher._systemctl
            write_entered = threading.Event()
            permit_write = threading.Event()
            result = []

            def blocked_write(fd, payload):
                write_entered.set()
                if not permit_write.wait(5):
                    raise RuntimeError("test write barrier timed out")
                return original_write(fd, payload)

            verifier.REPORT_FILE = runtime / "report"
            dispatcher.RUNTIME = runtime
            dispatcher.REPORT = runtime / "report"
            verifier.os.write = blocked_write
            dispatcher._systemctl = lambda *_args, **_kwargs: types.SimpleNamespace(
                returncode=0, stdout=b"active\n"
            )
            writer = threading.Thread(target=verifier._write_report, args=(values,))
            reader = threading.Thread(target=lambda: result.append(dispatcher.SystemOps().read_report()))
            try:
                writer.start()
                self.assertTrue(write_entered.wait(5))
                reader.start()
                reader.join(0.2)
                self.assertTrue(reader.is_alive(), "unpublished report became visible")
                permit_write.set()
                writer.join(5)
                reader.join(5)
                self.assertEqual(result, [expected])
            finally:
                permit_write.set()
                writer.join(5)
                reader.join(5)
                verifier.os.write = original_write
                dispatcher._systemctl = original_systemctl
                verifier.REPORT_FILE = old_report
                dispatcher.RUNTIME, dispatcher.REPORT = old_dispatcher_paths

    def test_interrupted_report_temporaries_cannot_wedge_future_publication(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        values = {"SOURCE_SHA": "a" * 40, "PIPELINE_ID": "99", "PHYSICAL_DEVICE_ACCEPTANCE": "PASS"}
        expected = verifier_report("a" * 40, "99")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-report-recovery-", dir="/root") as directory:
            runtime = Path(directory)
            os.chmod(runtime, 0o700)
            old_report = verifier.REPORT_FILE
            verifier.REPORT_FILE = runtime / "report"
            temporary = runtime / "report.tmp"
            target = runtime / "target"
            target.write_bytes(expected)
            os.chmod(target, 0o600)
            try:
                for length in range(len(expected)):
                    temporary.write_bytes(expected[:length])
                    os.chmod(temporary, 0o600)
                    with self.subTest(interrupted_length=length):
                        verifier._write_report(values)
                        self.assertEqual((runtime / "report").read_bytes(), expected)
                        self.assertTrue(temporary.exists())
                    (runtime / "report").unlink(missing_ok=True)
                    temporary.unlink(missing_ok=True)

                for attack in ("symlink", "fifo", "hardlink"):
                    if attack == "symlink":
                        temporary.symlink_to(target.name)
                    elif attack == "fifo":
                        os.mkfifo(temporary, 0o600)
                    else:
                        os.link(target, temporary)
                    with self.subTest(attack=attack):
                        verifier._write_report(values)
                        self.assertEqual((runtime / "report").read_bytes(), expected)
                        self.assertEqual(target.read_bytes(), expected)
                        self.assertTrue(temporary.exists() or temporary.is_symlink())
                    (runtime / "report").unlink(missing_ok=True)
                    temporary.unlink(missing_ok=True)
            finally:
                verifier.REPORT_FILE = old_report

    def test_verifier_rejects_hostile_claim_fifo_without_blocking(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-hostile-claim-", dir="/root") as directory:
            runtime = Path(directory)
            os.chmod(runtime, 0o700)
            claim = runtime / "claim"
            os.mkfifo(claim, 0o600)
            old_labels = verifier.LABELS_FILE
            verifier.LABELS_FILE = claim
            try:
                with self.assertRaises(RuntimeError):
                    verifier._labels()
            finally:
                verifier.LABELS_FILE = old_labels

    def test_fixed_runtime_never_imports_or_executes_admin_owned_application_code(self):
        verifier_source = (ROOT / "deploy" / "matrix_ci_physical_verifier.py").read_text(encoding="utf-8")
        unit = (ROOT / "deploy" / "matrix-ci-physical.service").read_text(encoding="utf-8")
        install = (ROOT / "deploy" / "matrix-ci-physical-install.md").read_text(encoding="utf-8")

        for forbidden in (
            "from hardware_lock import",
            "from matrix_framework import",
            'sys.path.insert(0, str(APP_ROOT))',
            "/home/admin/matrix/venv/bin/python",
        ):
            self.assertNotIn(forbidden, verifier_source + unit)
        self.assertIn("/usr/bin/python3 -I -B", unit)
        self.assertIn("/usr/local/lib/matrix-ci-python", install)
        self.assertIn("root:root", install)
        self.assertIn("0755", install)

    def test_fixed_binding_loader_rejects_writable_import_root_ancestry(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-bindings-", dir="/root") as directory:
            binding_root = Path(directory) / "bindings"
            package = binding_root / "rgbmatrix"
            package.mkdir(parents=True, mode=0o755)
            for name in ("__init__.py", "core.so", "graphics.so"):
                (package / name).write_bytes(name.encode("ascii"))
                os.chmod(package / name, 0o644)
            os.chmod(binding_root, 0o777)
            module = types.SimpleNamespace(__file__=str(package / "__init__.py"))
            old_root = verifier.BINDINGS_ROOT
            original_import = verifier.importlib.import_module
            verifier.BINDINGS_ROOT = binding_root
            verifier.importlib.import_module = lambda name: module
            try:
                with self.assertRaisesRegex(RuntimeError, "unsafe RGB Matrix binding"):
                    verifier._load_fixed_rgbmatrix()
            finally:
                verifier.BINDINGS_ROOT = old_root
                verifier.importlib.import_module = original_import
                if str(binding_root) in sys.path:
                    sys.path.remove(str(binding_root))

    def test_fixed_binding_loader_rejects_unmanifested_importable_file(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-bindings-", dir="/root") as directory:
            binding_root = Path(directory) / "bindings"
            package = binding_root / "rgbmatrix"
            package.mkdir(parents=True, mode=0o755)
            entries = {}
            for name in ("__init__.py", "core.so", "graphics.so"):
                payload = name.encode("ascii")
                (package / name).write_bytes(payload)
                os.chmod(package / name, 0o644)
                entries[f"rgbmatrix/{name}"] = verifier.hashlib.sha256(payload).hexdigest()
            (package / "attacker.py").write_text("raise SystemExit\n", encoding="ascii")
            os.chmod(package / "attacker.py", 0o644)
            manifest = binding_root / "rgbmatrix.sha256"
            manifest.write_text(
                "".join(f"{entries[name]}  {name}\n" for name in sorted(entries)),
                encoding="ascii",
            )
            os.chmod(manifest, 0o644)
            module = types.SimpleNamespace(__file__=str(package / "__init__.py"))
            old_root = verifier.BINDINGS_ROOT
            original_import = verifier.importlib.import_module
            verifier.BINDINGS_ROOT = binding_root
            verifier.importlib.import_module = lambda name: module
            try:
                with self.assertRaisesRegex(RuntimeError, "unexpected RGB Matrix binding entry"):
                    verifier._load_fixed_rgbmatrix()
            finally:
                verifier.BINDINGS_ROOT = old_root
                verifier.importlib.import_module = original_import
                if str(binding_root) in sys.path:
                    sys.path.remove(str(binding_root))

    def test_rgb_installer_requires_pinned_manifest_and_publishes_exact_trusted_tree(self):
        installer = load_deploy_module("matrix_ci_install_rgb_bindings")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-rgb-source-", dir="/root") as source_dir, tempfile.TemporaryDirectory(prefix="matrix-ci-rgb-dest-", dir="/root") as dest_dir:
            source = Path(source_dir)
            destination = Path(dest_dir) / "bindings"
            payloads = {name: name.encode("ascii") for name in installer.EXPECTED_FILES}
            for name, payload in payloads.items():
                (source / name).write_bytes(payload)
            manifest = "".join(
                f"{hashlib.sha256(payloads[name]).hexdigest()}  rgbmatrix/{name}\n"
                for name in sorted(payloads)
            ).encode("ascii")
            installer.install(source, destination, manifest)
            self.assertEqual(
                {str(path.relative_to(destination)) for path in destination.rglob("*")},
                {"rgbmatrix", "rgbmatrix.sha256", *(f"rgbmatrix/{name}" for name in payloads)},
            )
            self.assertEqual((destination / "rgbmatrix.sha256").read_bytes(), manifest)
            (source / "core.so").write_bytes(b"tampered")
            with self.assertRaisesRegex(RuntimeError, "digest"):
                installer.install(source, Path(dest_dir) / "other", manifest)

    def test_fixed_matrix_adapter_uses_exact_production_hardware_options(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        created = []

        class Options:
            pass

        class Canvas:
            def Fill(self, *_):
                pass

            def Clear(self):
                pass

        class Driver:
            def __init__(self, options):
                self.options = options
                self.width = options.cols * options.chain_length
                self.height = options.rows * options.parallel
                self.brightness = options.brightness
                created.append(self)

            def CreateFrameCanvas(self):
                return Canvas()

            def SwapOnVSync(self, canvas):
                return canvas

        module = types.SimpleNamespace(RGBMatrixOptions=Options, RGBMatrix=Driver)
        matrix = verifier.FixedMatrixAdapter(module)
        options = created[0].options

        self.assertEqual(
            (
                options.rows,
                options.cols,
                options.gpio_slowdown,
                options.chain_length,
                options.parallel,
                options.brightness,
                options.hardware_mapping,
                options.led_rgb_sequence,
                options.drop_privileges,
                options.limit_refresh_rate_hz,
                options.pwm_bits,
            ),
            (32, 64, 4, 1, 1, 100, "adafruit-hat-pwm", "RGB", False, 120, 8),
        )
        self.assertEqual((matrix.matrix.width, matrix.matrix.height), (64, 32))

    def test_fixed_matrix_adapter_blackouts_after_native_constructor_partially_initializes_then_raises(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        events = []

        class Options:
            pass

        class Canvas:
            def Clear(self):
                events.append("canvas-clear")

        class Driver:
            attempts = 0

            def __init__(self, options):
                type(self).attempts += 1
                self.brightness = options.brightness
                events.append(("native-init", type(self).attempts))
                if type(self).attempts == 1:
                    events.append("gpio-partially-initialized")
                    raise ValueError("native construction failed")

            def Clear(self):
                events.append("driver-clear")

            def CreateFrameCanvas(self):
                events.append("canvas-create")
                return Canvas()

            def SwapOnVSync(self, canvas):
                events.append("swap")
                return canvas

            def close(self):
                events.append("close")
                raise RuntimeError("close failed")

        module = types.SimpleNamespace(RGBMatrixOptions=Options, RGBMatrix=Driver)
        with self.assertRaisesRegex(ValueError, "native construction failed"):
            verifier.FixedMatrixAdapter(module)
        self.assertIn(("native-init", 2), events)
        self.assertIn("driver-clear", events)
        self.assertIn("canvas-clear", events)
        self.assertIn("swap", events)
        self.assertIn("close", events)

    def test_owned_driver_canvas_failure_attempts_every_available_blackout_route(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        events = []

        class Options:
            pass

        class Canvas:
            def Clear(self):
                events.append("canvas-clear")

        class Driver:
            canvas_attempts = 0

            def __init__(self, options):
                self.options = options
                self._brightness = options.brightness

            @property
            def brightness(self):
                return self._brightness

            @brightness.setter
            def brightness(self, value):
                self._brightness = value
                events.append(("brightness", value))

            def CreateFrameCanvas(self):
                type(self).canvas_attempts += 1
                events.append(("canvas-create", type(self).canvas_attempts))
                if type(self).canvas_attempts == 1:
                    raise ValueError("canvas construction failed")
                return Canvas()

            def Clear(self):
                events.append("driver-clear")
                raise RuntimeError("driver clear failed")

            def SwapOnVSync(self, canvas):
                events.append("swap")
                return canvas

            def close(self):
                events.append("close")
                raise RuntimeError("close failed")

        module = types.SimpleNamespace(RGBMatrixOptions=Options, RGBMatrix=Driver)
        with self.assertRaisesRegex(ValueError, "canvas construction failed"):
            verifier.FixedMatrixAdapter(module)
        self.assertEqual(
            events,
            [
                ("canvas-create", 1),
                ("brightness", 1),
                "driver-clear",
                ("canvas-create", 2),
                "canvas-clear",
                "swap",
                "close",
            ],
        )

    def test_fixed_display_lock_rejects_concurrent_owner_without_app_import(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-lock-", dir="/root") as directory:
            os.chmod(directory, 0o755)
            lock_path = os.path.join(directory, "display.lock")
            Path(lock_path).touch(mode=0o644)
            os.chmod(lock_path, 0o644)
            first = verifier.FixedDisplayOwnershipLock(lock_path).acquire()
            try:
                locked_inode = os.stat(lock_path).st_ino
                with self.assertRaisesRegex(RuntimeError, "already owned"):
                    verifier.FixedDisplayOwnershipLock(lock_path).acquire()
                contender = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_RDWR|os.O_NOFOLLOW); "
                        "\ntry: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)"
                        "\nexcept BlockingIOError: raise SystemExit(75)"
                        "\nraise SystemExit(0)",
                        lock_path,
                    ],
                    timeout=5,
                    check=False,
                )
                self.assertEqual(contender.returncode, 75)
                self.assertEqual(os.stat(lock_path).st_ino, locked_inode)
            finally:
                first.release()
            with verifier.FixedDisplayOwnershipLock(lock_path):
                pass

    def test_fixed_display_lock_rejects_hardlink_before_mutating_target(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-lock-node-", dir="/root") as directory:
            os.chmod(directory, 0o755)
            target = Path(directory) / "target"
            target.write_text("protected\n", encoding="ascii")
            os.chmod(target, 0o644)
            lock_path = Path(directory) / "display.lock"
            os.link(target, lock_path)

            with self.assertRaisesRegex(RuntimeError, "unsafe display lock"):
                verifier.FixedDisplayOwnershipLock(lock_path).acquire()

            self.assertEqual(target.read_text(encoding="ascii"), "protected\n")

    def test_verifier_reports_acceptance_while_production_remains_stopped(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        events = []
        ops = FakeOps(events)

        report = verifier.run_verification(
            "a" * 40,
            "123",
            ops=ops,
            lock_factory=lambda: FakeLock(events),
            matrix_factory=lambda: (events.append(("matrix", "create")) or FakeMatrix(events)),
        )

        self.assertEqual(report["PHYSICAL_DEVICE_ACCEPTANCE"], "PASS")
        self.assertNotIn("PHYSICAL_DEVICE_RECOVERY", report)
        self.assertFalse(ops.active)
        self.assertLess(events.index(("lock", "acquire")), events.index(("matrix", "create")))
        self.assertLess(events.index(("service", "stop")), events.index(("lock", "acquire")))
        self.assertEqual(events[-1], ("snapshot",))
        self.assertNotIn(("service", "start"), events)

    def test_precondition_failure_still_stops_root_app_before_returning_report(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        events = []
        ops = FakeOps(events)
        ops.http = False

        report = verifier.run_verification(
            "a" * 40,
            "124",
            ops=ops,
            lock_factory=lambda: FakeLock(events),
            matrix_factory=lambda: FakeMatrix(events),
        )

        self.assertEqual(report["PHYSICAL_DEVICE_ACCEPTANCE"], "FAIL")
        self.assertFalse(ops.active)
        self.assertIn(("service", "stop"), events)
        self.assertNotIn(("matrix", "create"), events)

    def test_brightness_clamping_readback_and_black_cleanup_are_fixed(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        events = []

        result = verifier.exercise_matrix(lambda: FakeMatrix(events))

        self.assertEqual(result["dimensions"], [64, 32])
        self.assertEqual(
            result["brightness"],
            [
                {"requested": 0, "observed": 1},
                {"requested": 1, "observed": 1},
                {"requested": 25, "observed": 25},
                {"requested": 50, "observed": 50},
                {"requested": 75, "observed": 75},
                {"requested": 100, "observed": 100},
                {"requested": 101, "observed": 100},
            ],
        )
        self.assertEqual(events[:2], [("fill", 0, 0, 0), ("swap",)])
        self.assertEqual(events[-3:], [("brightness", 1, 1), ("clear",), ("swap",)])

    def test_exercise_matrix_uses_factory_ownership_seam_after_partial_construction_failure(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        events = []

        class PartialFactory:
            def __call__(self):
                events.append("factory-partial-init")
                raise ValueError("factory construction failed")

            def cleanup_partial(self):
                for event in ("dim", "clear", "swap", "close"):
                    events.append(event)
                raise RuntimeError("cleanup failed")

        with self.assertRaisesRegex(ValueError, "factory construction failed"):
            verifier.exercise_matrix(PartialFactory())
        self.assertEqual(events, ["factory-partial-init", "dim", "clear", "swap", "close"])

    def test_matrix_cleanup_attempts_every_stage_without_masking_primary_failure(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        events = []

        class BrokenCleanupMatrix(FakeMatrix):
            def set_brightness(self, value):
                events.append(("brightness-attempt", value))
                if value == 25:
                    raise ValueError("primary brightness failure")
                if value == 1 and ("brightness-attempt", 25) in events:
                    raise RuntimeError("cleanup brightness failure")
                self.matrix.brightness = max(1, min(100, int(value)))

            def clear(self):
                events.append(("clear-attempt",))
                raise RuntimeError("cleanup clear failure")

            def swap(self):
                events.append(("swap-attempt",))
                if ("brightness-attempt", 25) in events:
                    raise RuntimeError("cleanup swap failure")

        with self.assertRaisesRegex(ValueError, "primary brightness failure"):
            verifier.exercise_matrix(lambda: BrokenCleanupMatrix(events))

        self.assertEqual(
            events[-3:],
            [("brightness-attempt", 1), ("clear-attempt",), ("swap-attempt",)],
        )

    def test_lock_contention_writes_acceptance_fail_and_leaves_recovery_to_unit(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        events = []
        ops = FakeOps(events)
        opened = []

        report = verifier.run_verification(
            "b" * 40,
            "456",
            ops=ops,
            lock_factory=lambda: FakeLock(events, RuntimeError("already owned")),
            matrix_factory=lambda: opened.append(True),
        )

        self.assertEqual(opened, [])
        self.assertEqual(report["PHYSICAL_DEVICE_ACCEPTANCE"], "FAIL")
        self.assertIn("already owned", report["ERROR"])
        self.assertFalse(ops.active)
        self.assertNotIn(("service", "start"), events)

    def test_signal_like_failure_is_bounded_evidence_while_service_stays_stopped(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        events = []
        ops = FakeOps(events)

        report = verifier.run_verification(
            "c" * 40,
            "789",
            ops=ops,
            lock_factory=lambda: FakeLock(events),
            matrix_factory=lambda: FakeMatrix(events, fail_on=25),
        )

        self.assertEqual(report["PHYSICAL_DEVICE_ACCEPTANCE"], "FAIL")
        self.assertIn("InterruptedError: terminated", report["ERROR"])
        self.assertLessEqual(len(report["ERROR"]), 512)
        self.assertFalse(ops.active)
        self.assertIn(("clear",), events)

    def test_stop_failure_after_transition_is_reported_for_unit_recovery(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        events = []

        class StopFailureOps(FakeOps):
            def stop_service(self):
                super().stop_service()
                raise RuntimeError("stop transport failed")

        ops = StopFailureOps(events)
        opened = []
        report = verifier.run_verification(
            "c" * 40,
            "790",
            ops=ops,
            lock_factory=lambda: FakeLock(events),
            matrix_factory=lambda: opened.append(True),
        )

        self.assertEqual(opened, [])
        self.assertEqual(report["PHYSICAL_DEVICE_ACCEPTANCE"], "FAIL")
        self.assertIn("stop transport failed", report["ERROR"])
        self.assertFalse(ops.active)

    def test_mutable_state_is_compared_before_and_after_while_production_is_stopped(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        events = []

        class ChangedStateOps(FakeOps):
            def snapshot(self):
                snapshot = super().snapshot()
                if not self.active:
                    snapshot["data"] = "changed"
                return snapshot

        ops = ChangedStateOps(events)
        report = verifier.run_verification(
            "c" * 40,
            "791",
            ops=ops,
            lock_factory=lambda: FakeLock(events),
            matrix_factory=lambda: FakeMatrix(events),
        )

        self.assertEqual(report["PHYSICAL_DEVICE_ACCEPTANCE"], "FAIL")
        self.assertIn("appliance state changed", report["ERROR"])
        self.assertEqual(events.count(("snapshot",)), 2)
        self.assertFalse(ops.active)

    def test_dispatcher_accepts_only_exact_command_and_exact_bounded_report_schema(self):
        dispatcher = load_deploy_module("matrix_ci_physical_dispatcher")
        sha = "d" * 40
        self.assertEqual(dispatcher.parse_command(f"verify {sha} 42"), (sha, "42"))
        for command in (
            "",
            "verify deadbeef 42",
            "verify " + "d" * 40 + " 4x",
            "verify " + "d" * 40 + " 42 extra",
            "verify " + "D" * 40 + " 42",
            "verify " + "d" * 40 + " 42;id",
        ):
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    dispatcher.parse_command(command)

        self.assertEqual(
            dispatcher.parse_verifier_report(verifier_report(sha, "42"), sha, "42"),
            {"SOURCE_SHA": sha, "PIPELINE_ID": "42", "PHYSICAL_DEVICE_ACCEPTANCE": "PASS"},
        )
        invalid_reports = (
            verifier_report(sha, "42") + b"PHYSICAL_DEVICE_ACCEPTANCE=PASS\n",
            verifier_report(sha, "42") + b"UNEXPECTED=value\n",
            verifier_report("e" * 40, "42"),
            verifier_report(sha, "43"),
            verifier_report(sha, "42").replace(b"PASS", b"PASSING"),
            verifier_report(sha, "42") + b"PHYSICAL_DEVICE_RECOVERY=PASS\n",
            verifier_report(sha, "42", "FAIL"),
            verifier_report(sha, "42", "FAIL", "bad\rvalue"),
            b"x" * (dispatcher.MAX_REPORT_BYTES + 1),
        )
        for payload in invalid_reports:
            with self.subTest(payload=payload[:80]):
                with self.assertRaises(ValueError):
                    dispatcher.parse_verifier_report(payload, sha, "42")

    def test_dispatcher_rejects_existing_claim_and_restores_after_in_memory_parse(self):
        dispatcher = load_deploy_module("matrix_ci_physical_dispatcher")
        sha = "e" * 40

        class Ops:
            def __init__(self, busy=False):
                self.busy = busy
                self.events = []

            def await_pid1_ownership(self):
                self.events.append(("pid1", "owns"))

            def prepare(self, sha, pipeline):
                self.events.append(("prepare", sha, pipeline))
                if self.busy:
                    raise RuntimeError("physical verifier durable claim is busy")

            def read_report(self):
                self.events.append(("report",))
                return verifier_report(sha, "51")

            def stop_and_wait(self):
                self.events.append(("stop", "matrix-ci-physical.service"))
                return 0

            def recovery_ready(self):
                self.events.append(("recovery", "verify"))
                return True

        busy = Ops(busy=True)
        with self.assertRaisesRegex(RuntimeError, "claim is busy"):
            dispatcher.dispatch(f"verify {sha} 51", ops=busy)
        self.assertEqual(busy.events, [("prepare", "e" * 40, "51")])

        ready = Ops()
        status, report = dispatcher.dispatch(f"verify {sha} 51", ops=ready)
        self.assertEqual(status, 0)
        self.assertEqual(
            report,
            verifier_report(sha, "51").replace(
                b"PHYSICAL_DEVICE_ACCEPTANCE=PASS\n",
                b"PHYSICAL_DEVICE_ACCEPTANCE=PASS\nPHYSICAL_DEVICE_RECOVERY=PASS\n",
            ),
        )
        self.assertEqual(
            ready.events,
            [
                ("prepare", "e" * 40, "51"),
                ("pid1", "owns"),
                ("report",),
                ("stop", "matrix-ci-physical.service"),
                ("recovery", "verify"),
            ],
        )

    def test_dispatcher_has_no_racy_active_state_and_job_precheck(self):
        dispatcher = load_deploy_module("matrix_ci_physical_dispatcher")
        self.assertFalse(hasattr(dispatcher.SystemOps, "service_busy"))

    def test_dispatcher_systemctl_calls_are_bounded(self):
        dispatcher = load_deploy_module("matrix_ci_physical_dispatcher")
        calls = []
        original = dispatcher.subprocess.run
        dispatcher.subprocess.run = lambda *args, **kwargs: (
            calls.append((args, kwargs))
            or types.SimpleNamespace(returncode=0, stdout=b"")
        )
        try:
            dispatcher._systemctl("show", dispatcher.SERVICE, check=False)
        finally:
            dispatcher.subprocess.run = original
        self.assertEqual(len(calls), 1)
        self.assertGreater(calls[0][1]["timeout"], 0)

    def test_dispatcher_distinguishes_acceptance_fail_from_recovery_pass(self):
        dispatcher = load_deploy_module("matrix_ci_physical_dispatcher")
        sha = "f" * 40

        class Ops:
            def service_busy(self):
                return False

            def prepare(self, _sha, _pipeline):
                pass

            def start(self):
                return 0

            def read_report(self):
                return verifier_report(sha, "52", "FAIL", "brightness mismatch")

            def stop_and_wait(self):
                return 0

            def recovery_ready(self):
                return True

        status, report = dispatcher.dispatch(f"verify {sha} 52", ops=Ops())
        self.assertEqual(status, 1)
        self.assertEqual(
            report,
            (
                f"SOURCE_SHA={sha}\n"
                "PIPELINE_ID=52\n"
                "PHYSICAL_DEVICE_ACCEPTANCE=FAIL\n"
                "PHYSICAL_DEVICE_RECOVERY=PASS\n"
                "ERROR=brightness mismatch\n"
            ).encode("ascii"),
        )

    def test_ci_report_helper_accepts_only_exact_canonical_schema_and_bound_labels(self):
        helper = load_deploy_module("../scripts/verify_physical_device_report")
        sha = "9" * 40
        valid = (
            f"SOURCE_SHA={sha}\n"
            "PIPELINE_ID=77\n"
            "PHYSICAL_DEVICE_ACCEPTANCE=PASS\n"
            "PHYSICAL_DEVICE_RECOVERY=PASS\n"
        ).encode("ascii")
        self.assertEqual(helper.parse_report(valid, sha, "77")["PHYSICAL_DEVICE_ACCEPTANCE"], "PASS")

        for payload in (
            valid + b"PHYSICAL_DEVICE_RECOVERY=PASS\n",
            valid + b"ERROR=contradiction\n",
            valid.replace(b"SOURCE_SHA=", b"SOURCE_SHA=0"),
            valid.replace(b"RECOVERY=PASS", b"RECOVERY=FAIL"),
            valid.replace(b"ACCEPTANCE=PASS", b"ACCEPTANCE=PASSING"),
            valid + b"UNEXPECTED=value\n",
            b"x" * (helper.MAX_REPORT_BYTES + 1),
        ):
            with self.subTest(payload=payload[:80]):
                with self.assertRaises(ValueError):
                    helper.parse_report(payload, sha, "77")

        pipeline = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")
        self.assertIn("python scripts/verify_physical_device_report.py", pipeline)
        self.assertNotRegex(pipeline, r"grep\s+-q")

    def test_pid1_path_unit_owns_durable_claim_and_recovery_cleanup(self):
        path = ROOT / "deploy" / "matrix-ci-physical.path"
        self.assertTrue(path.is_file(), "durable claim path unit is missing")
        path_unit = path.read_text(encoding="utf-8")
        self.assertIn("PathExists=/run/matrix-ci-physical/claim", path_unit)
        self.assertIn("Unit=matrix-ci-physical.service", path_unit)
        install = (ROOT / "deploy" / "matrix-ci-physical-install.md").read_text(encoding="utf-8")
        self.assertIn("matrix-ci-physical.path", install)
        self.assertIn("systemctl enable --now matrix-ci-physical.path", install)

        verifier = load_deploy_module("matrix_ci_physical_verifier")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-claim-cleanup-", dir="/root") as directory:
            runtime = Path(directory)
            os.chmod(runtime, 0o700)
            claim = runtime / "claim"
            claim.write_text(f"{'a' * 40} 1\n", encoding="ascii")
            os.chmod(claim, 0o600)
            events = []
            ops = FakeOps(events)
            ops.active = False
            old_claim = verifier.CLAIM_FILE
            verifier.CLAIM_FILE = claim
            try:
                verifier.restore_only(ops)
                verifier.restore_only(ops)
            finally:
                verifier.CLAIM_FILE = old_claim
            self.assertFalse(claim.exists())
            self.assertEqual(events.count(("service", "start")), 1)
            self.assertEqual(events.count(("service", "wait-ready")), 2)

    def test_service_keeps_production_stopped_until_dispatcher_or_timeout_restores(self):
        unit = (ROOT / "deploy" / "matrix-ci-physical.service").read_text(encoding="utf-8")
        dispatcher = (ROOT / "deploy" / "matrix_ci_physical_dispatcher.py").read_text(encoding="utf-8")
        self.assertIn("Type=exec", unit)
        self.assertNotIn("RemainAfterExit", unit)
        self.assertRegex(unit, r"(?m)^TimeoutStartSec=[1-9][0-9]*$")
        self.assertRegex(unit, r"(?m)^TimeoutStopSec=[1-9][0-9]*$")
        self.assertRegex(unit, r"(?m)^RuntimeMaxSec=[1-9][0-9]*$")
        self.assertNotIn("flock", unit)
        self.assertIn("matrix-ci-physical-verifier", unit)
        self.assertIn("ExecStop=", unit)
        self.assertIn("ExecStopPost=", unit)
        self.assertGreaterEqual(unit.count("--restore-only"), 2)
        self.assertNotIn("/home/admin/matrix-ci", unit)
        self.assertIn("ProtectHome=read-only", unit)
        self.assertNotRegex(unit, r"(?m)^ReadWritePaths=.*?/home/")
        self.assertNotRegex(dispatcher, r'_systemctl\("start"')
        self.assertIn('_systemctl("stop", SERVICE', dispatcher)
        self.assertNotIn('_systemctl("stop", "--wait", SERVICE', dispatcher)

    def test_units_pass_systemd_analyze_verify_with_safe_substituted_paths(self):
        if not Path("/usr/bin/systemd-analyze").is_file():
            self.skipTest("systemd-analyze is unavailable")
        with tempfile.TemporaryDirectory(prefix="matrix-unit-verify-") as directory:
            root = Path(directory)
            physical = (ROOT / "deploy" / "matrix-ci-physical.service").read_text(encoding="utf-8")
            web = (ROOT / "deploy" / "matrix-web.service").read_text(encoding="utf-8")
            physical = re.sub(
                r"(?m)^Exec(?:Start|Stop|StopPost)=.*$",
                lambda match: match.group(0).split("=", 1)[0] + "=/bin/true",
                physical,
            )
            web = re.sub(
                r"(?m)^ExecStart(?:Pre)?=.*$",
                lambda match: match.group(0).split("=", 1)[0] + "=/bin/true",
                web,
            )
            web = web.replace("WorkingDirectory=/home/admin/matrix", f"WorkingDirectory={root}")
            web = re.sub(r"(?m)^(?:Requires|After)=.*matrix-network-setup\.service.*\n", "", web)
            physical_path = root / "matrix-ci-physical.service"
            web_path = root / "matrix-web.service"
            physical_path.write_text(physical, encoding="utf-8")
            web_path.write_text(web, encoding="utf-8")

            result = subprocess.run(
                ["/usr/bin/systemd-analyze", "verify", str(physical_path), str(web_path)],
                text=True,
                capture_output=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_forced_command_install_contract_never_grants_candidate_execution(self):
        example = (ROOT / "deploy" / "matrix-ci-physical-authorized-key.example").read_text(encoding="utf-8")
        self.assertIn('command="/usr/local/libexec/matrix-ci-physical-dispatcher"', example)
        for restriction in ("no-agent-forwarding", "no-port-forwarding", "no-pty", "no-user-rc", "no-X11-forwarding"):
            self.assertIn(restriction, example)
        self.assertNotRegex(example, r"(?:sudo|scp|sftp|/bin/(?:ba)?sh)")

        unit = (ROOT / "deploy" / "matrix-ci-physical.service").read_text(encoding="utf-8")
        web_unit = (ROOT / "deploy" / "matrix-web.service").read_text(encoding="utf-8")
        install = (ROOT / "deploy" / "matrix-ci-physical-install.md").read_text(encoding="utf-8")
        self.assertRegex(unit, r"(?m)^CapabilityBoundingSet=")
        self.assertRegex(web_unit, r"(?m)^CapabilityBoundingSet=")
        self.assertIn("matrix_ci_install_rgb_bindings.py", install)
        self.assertNotIn("for name in __init__.py core.so graphics.so", install)
        for required in ("authorized_keys", "root:root:600", "sshd -T", "dedicated", "ssh-keygen -lf"):
            self.assertIn(required, install)

    def test_install_gate_rejects_forced_command_options_in_key_comment(self):
        gate = load_deploy_module("verify_matrix_ci_physical_install")
        public_key = bytes(range(32))
        blob = len(b"ssh-ed25519").to_bytes(4, "big") + b"ssh-ed25519" + len(public_key).to_bytes(4, "big") + public_key
        encoded = __import__("base64").b64encode(blob).decode("ascii")
        fingerprint = "SHA256:" + __import__("base64").b64encode(
            hashlib.sha256(blob).digest()
        ).decode("ascii").rstrip("=")
        options = (
            'command="/usr/local/libexec/matrix-ci-physical-dispatcher",restrict,'
            "no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding"
        )
        with tempfile.TemporaryDirectory(prefix="matrix-ci-ssh-", dir="/root") as directory:
            ssh_dir = Path(directory)
            os.chmod(ssh_dir, 0o700)
            auth = ssh_dir / "authorized_keys"
            auth.write_text(f"ssh-ed25519 {encoded} {options}\n", encoding="ascii")
            os.chmod(auth, 0o600)
            original_path, original_run = gate.Path, gate.run
            gate.Path = lambda _path: ssh_dir

            def fake_run(*argv):
                if argv[0] == gate.SSH_KEYGEN:
                    return f"256 {fingerprint} matrix-physical-ci (ED25519)\n"
                if argv[:2] == (gate.SSHD, "-T"):
                    return (
                        "authorizedkeysfile .ssh/authorized_keys\n"
                        "authorizedkeyscommand none\n"
                        "authorizedkeyscommanduser nobody\n"
                        "permitrootlogin forced-commands-only\n"
                    )
                return ""

            gate.run = fake_run
            try:
                with self.assertRaisesRegex(RuntimeError, "forced-command"):
                    gate.verify_ssh(
                        fingerprint,
                        gate.SshConnectionContext(
                            "192.0.2.10", "runner.example.test", "198.51.100.20", 22, "none"
                        ),
                    )
            finally:
                gate.Path, gate.run = original_path, original_run

    @unittest.skipUnless(Path("/usr/sbin/sshd").is_file(), "requires a local OpenSSH daemon binary")
    def test_complete_sshd_connection_context_selects_every_match_dimension_fail_closed(self):
        gate = load_deploy_module("verify_matrix_ci_physical_install")
        context = gate.SshConnectionContext(
            source_address="192.0.2.10",
            source_hostname="runner.example.test",
            local_address="198.51.100.20",
            local_port=2222,
            routing_domain="ci",
        )
        expected_context = (
            "user=root,host=runner.example.test,addr=192.0.2.10,"
            "laddr=198.51.100.20,lport=2222,rdomain=ci"
        )
        with tempfile.TemporaryDirectory(prefix="matrix-ci-sshd-context-", dir="/root") as directory:
            config = Path(directory) / "sshd_config"
            base = (
                "AuthorizedKeysFile .ssh/authorized_keys\n"
                "AuthorizedKeysCommand none\n"
                "AuthorizedKeysCommandUser nobody\n"
                "PermitRootLogin forced-commands-only\n"
            )
            attacks = (
                "Match Host runner.example.test\n",
                "Match LocalAddress 198.51.100.20\n",
                "Match LocalPort 2222\n",
                "Match RDomain ci\n",
            )
            for match in attacks:
                with self.subTest(match=match.strip()):
                    config.write_text(
                        base + match + "    AuthorizedKeysFile .ssh/unrestricted\n"
                        "    PermitRootLogin yes\n",
                        encoding="ascii",
                    )
                    output = subprocess.run(
                        [gate.SSHD, "-T", "-f", str(config), "-C", expected_context],
                        check=True,
                        text=True,
                        capture_output=True,
                        timeout=15,
                    ).stdout
                    with self.assertRaises(RuntimeError):
                        gate._parse_effective_sshd_policy(output)

            config.write_text(base, encoding="ascii")
            argv = gate._effective_sshd_argv(context, config_path=str(config))
            self.assertEqual(argv[-1], expected_context)
            output = subprocess.run(argv, check=True, text=True, capture_output=True, timeout=15).stdout
            self.assertEqual(
                gate._parse_effective_sshd_policy(output)["permit_root_login"],
                "forced-commands-only",
            )

    def test_install_gate_rejects_alternate_effective_file_with_unrestricted_matching_key(self):
        gate = load_deploy_module("verify_matrix_ci_physical_install")
        public_key = bytes(range(32))
        blob = len(b"ssh-ed25519").to_bytes(4, "big") + b"ssh-ed25519" + len(public_key).to_bytes(4, "big") + public_key
        encoded = __import__("base64").b64encode(blob).decode("ascii")
        fingerprint = "SHA256:" + __import__("base64").b64encode(
            hashlib.sha256(blob).digest()
        ).decode("ascii").rstrip("=")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-ssh-alt-", dir="/root") as directory:
            ssh_dir = Path(directory)
            os.chmod(ssh_dir, 0o700)
            auth = ssh_dir / "authorized_keys"
            auth.write_text(
                f"{gate.REQUIRED_AUTHORIZED_KEY_OPTIONS} ssh-ed25519 {encoded} matrix-physical-ci\n",
                encoding="ascii",
            )
            os.chmod(auth, 0o600)
            (ssh_dir / "other_keys").write_text(
                f"ssh-ed25519 {encoded} unrestricted-copy\n", encoding="ascii"
            )
            original_path, original_run = gate.Path, gate.run
            gate.Path = lambda _path: ssh_dir

            def fake_run(*argv):
                if argv[0] == gate.SSH_KEYGEN:
                    return f"256 {fingerprint} matrix-physical-ci (ED25519)\n"
                if argv[:2] == (gate.SSHD, "-T"):
                    return (
                        "authorizedkeysfile .ssh/other_keys\n"
                        "authorizedkeyscommand none\n"
                        "authorizedkeyscommanduser nobody\n"
                        "permitrootlogin forced-commands-only\n"
                    )
                return ""

            gate.run = fake_run
            try:
                with self.assertRaisesRegex(RuntimeError, "AuthorizedKeysFile"):
                    gate.verify_ssh(
                        fingerprint,
                        gate.SshConnectionContext(
                            "192.0.2.10", "runner.example.test", "198.51.100.20", 22, "none"
                        ),
                    )
            finally:
                gate.Path, gate.run = original_path, original_run

    def test_effective_sshd_policy_is_uniquely_bound_to_reviewed_key_source(self):
        gate = load_deploy_module("verify_matrix_ci_physical_install")
        safe = (
            "authorizedkeysfile .ssh/authorized_keys\n"
            "authorizedkeyscommand none\n"
            "authorizedkeyscommanduser nobody\n"
            "permitrootlogin forced-commands-only\n"
            "passwordauthentication no\n"
        )
        self.assertEqual(
            gate._parse_effective_sshd_policy(safe),
            {"authorized_keys_file": "/root/.ssh/authorized_keys", "permit_root_login": "forced-commands-only"},
        )
        unsafe = (
            safe.replace(".ssh/authorized_keys", ".ssh/other_keys"),
            safe.replace(".ssh/authorized_keys", "/root/.ssh/other_keys"),
            safe.replace(".ssh/authorized_keys", ".ssh/authorized_keys .ssh/other_keys"),
            safe.replace(".ssh/authorized_keys", "%h/.ssh/authorized_keys"),
            safe.replace(".ssh/authorized_keys", "../root/.ssh/authorized_keys"),
            safe.replace("authorizedkeyscommand none", "authorizedkeyscommand /usr/local/bin/keys"),
            safe.replace("permitrootlogin forced-commands-only", "permitrootlogin prohibit-password"),
            safe + "authorizedkeysfile .ssh/authorized_keys\n",
            safe + "authorizedkeyscommand none\n",
            safe.replace("authorizedkeyscommanduser nobody\n", ""),
            "authorizedkeysfile\n" + safe,
            safe.replace("authorizedkeysfile ", "AuthorizedKeysFile "),
        )
        for output in unsafe:
            with self.subTest(output=output[:100]):
                with self.assertRaises(RuntimeError):
                    gate._parse_effective_sshd_policy(output)

    def test_authorized_keys_parser_accepts_only_exact_effective_ed25519_policy(self):
        gate = load_deploy_module("verify_matrix_ci_physical_install")
        public_key = bytes(range(32))
        blob = len(b"ssh-ed25519").to_bytes(4, "big") + b"ssh-ed25519" + len(public_key).to_bytes(4, "big") + public_key
        encoded = __import__("base64").b64encode(blob).decode("ascii")
        fingerprint = "SHA256:" + __import__("base64").b64encode(
            hashlib.sha256(blob).digest()
        ).decode("ascii").rstrip("=")
        options = gate.REQUIRED_AUTHORIZED_KEY_OPTIONS
        valid = f"{options} ssh-ed25519 {encoded} matrix-physical-ci"
        self.assertEqual(gate._matching_forced_key_lines(valid, fingerprint), [1])
        self.assertEqual(
            gate._matching_forced_key_lines(f'from="192.0.2.0/24",{valid}', fingerprint),
            [1],
        )
        invalid = (
            f"ssh-ed25519 {encoded} {options}",
            f"{options},no-pty ssh-ed25519 {encoded}",
            f"{options.replace('restrict,', 'restrict,restrict,')} ssh-ed25519 {encoded}",
            f"command=\\\"/usr/local/libexec/matrix-ci-physical-dispatcher\\\",restrict,no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding ssh-ed25519 {encoded}",
            f"{valid}\nssh-ed25519 {encoded} unrestricted-copy",
            f'command="/usr/local/libexec/matrix-ci-physical-dispatcher,restrict ssh-ed25519 {encoded}',
        )
        for source in invalid:
            with self.subTest(source=source[:100]):
                with self.assertRaises(RuntimeError):
                    gate._matching_forced_key_lines(source, fingerprint)

    def test_fail_closed_installed_policy_gate_covers_live_only_prerequisites(self):
        gate_path = ROOT / "deploy" / "verify_matrix_ci_physical_install.py"
        self.assertTrue(gate_path.is_file())
        gate = gate_path.read_text(encoding="utf-8")
        for required in (
            "systemctl cat", "systemctl show", "RuntimeMaxUSec", "CapabilityBoundingSet",
            "InaccessiblePaths", "sshd -T", "authorized_keys", "ssh-keygen",
            "matrix-ci-physical.path", "matrix-ci-physical.service",
        ):
            self.assertIn(required, gate)
        install = (ROOT / "deploy" / "matrix-ci-physical-install.md").read_text(encoding="utf-8")
        self.assertIn("verify_matrix_ci_physical_install.py", install)


if __name__ == "__main__":
    unittest.main()
