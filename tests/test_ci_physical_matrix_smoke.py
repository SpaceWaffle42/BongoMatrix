import contextlib
import fcntl
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
from unittest import mock
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

    def test_quiescent_temporary_namespace_has_bounded_work_and_guaranteed_convergence(self):
        modules = (
            load_deploy_module("matrix_ci_physical_dispatcher"),
            load_deploy_module("matrix_ci_physical_verifier"),
        )
        for module in modules:
            with self.subTest(module=module.__name__):
                with tempfile.TemporaryDirectory(prefix="matrix-ci-quiescent-", dir="/root") as directory:
                    root = Path(directory)
                    namespace = root / "temporaries"
                    namespace.mkdir()
                    target = root / "target"
                    target.write_text("unchanged", encoding="ascii")
                    for counter in range(260):
                        path = namespace / f"hostile-{counter:03d}"
                        if counter == 7:
                            path.symlink_to(target)
                        else:
                            path.write_bytes(b"stale")
                    fd = os.open(namespace, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        passes = 0
                        while module._scavenge_quiescent_temporaries(fd, limit=64):
                            passes += 1
                            self.assertLessEqual(module._LAST_SCAVENGE_VISITED, 64)
                            self.assertLess(passes, 10)
                    finally:
                        os.close(fd)
                    self.assertEqual(target.read_text(encoding="ascii"), "unchanged")
                    self.assertEqual(list(namespace.iterdir()), [])

    def test_restore_only_scavenges_only_report_temporaries_without_breaking_dispatch_lock(self):
        dispatcher = load_deploy_module("matrix_ci_physical_dispatcher")
        verifier = load_deploy_module("matrix_ci_physical_verifier")

        class ReadyOps:
            def service_active(self):
                return True

            def start_service(self):
                raise AssertionError("service was already active")

            def wait_ready(self):
                return None

        with tempfile.TemporaryDirectory(prefix="matrix-ci-restore-scavenge-", dir="/root") as directory:
            runtime = Path(directory) / "runtime"
            runtime.mkdir(mode=0o700)
            claim_temporaries = runtime / dispatcher.CLAIM_TEMPORARIES
            report_temporaries = runtime / verifier.REPORT_TEMPORARIES
            claim_temporaries.mkdir(mode=0o700)
            report_temporaries.mkdir(mode=0o700)
            dispatch_lock = runtime / "dispatch.lock"
            report = runtime / "report"
            claim = runtime / "claim"
            for path, payload in (
                (dispatch_lock, b"lock"),
                (report, b"trusted report"),
                (claim, b"a" * 40 + b" 7\n"),
            ):
                path.write_bytes(payload)
                os.chmod(path, 0o600)
            claim_temporary = claim_temporaries / "claim.tmp.sentinel"
            claim_temporary.write_bytes(b"preserve")
            for counter in range(260):
                stale = report_temporaries / f"report.tmp.stale.{counter:03d}"
                stale.write_bytes(b"stale")
                os.chmod(stale, 0o600)

            trusted = (runtime, claim_temporaries, report_temporaries, dispatch_lock, report)
            identities = {path: (path.stat().st_dev, path.stat().st_ino) for path in trusted}
            lock_fd = os.open(dispatch_lock, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
            contender_fd = os.open(dispatch_lock, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            old_claim, old_report = verifier.CLAIM_FILE, verifier.REPORT_FILE
            verifier.CLAIM_FILE = runtime / "claim"
            verifier.REPORT_FILE = runtime / "report"
            try:
                passes = 0
                while list(report_temporaries.iterdir()):
                    verifier.restore_only(ReadyOps(), scavenge_stale_temporaries=True)
                    passes += 1
                    self.assertLess(passes, 10)
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                verifier.CLAIM_FILE, verifier.REPORT_FILE = old_claim, old_report
                os.close(contender_fd)
                os.close(lock_fd)

            self.assertFalse(claim.exists(), "successful recovery consumes only the durable claim")
            self.assertEqual(claim_temporary.read_bytes(), b"preserve")
            for path, identity in identities.items():
                self.assertEqual((path.stat().st_dev, path.stat().st_ino), identity, path)

    def test_compatibility_scavengers_reject_the_wrong_temporary_namespace(self):
        dispatcher = load_deploy_module("matrix_ci_physical_dispatcher")
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-compat-scavenge-", dir="/root") as directory:
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaisesRegex(RuntimeError, "claim temporary namespace"):
                    dispatcher._scavenge_stale_temporaries(directory_fd, "report.tmp")
                with self.assertRaisesRegex(RuntimeError, "report temporary namespace"):
                    verifier._scavenge_stale_temporaries(directory_fd, "claim.tmp")
            finally:
                os.close(directory_fd)

    def _assert_broken_stderr_cannot_mask_owned_cleanup(self, module):
        class BrokenDiagnostics:
            def write(self, _payload):
                raise BrokenPipeError("stderr transport closed")

            def flush(self):
                raise OSError("stderr flush closed")

        with tempfile.TemporaryDirectory(prefix="matrix-ci-broken-stderr-", dir="/root") as directory:
            runtime = Path(directory)
            temporary = runtime / "owned.tmp"
            temporary.write_bytes(b"private")
            info = temporary.stat()
            runtime_fd = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY)
            original_unlink = module.os.unlink
            original_stderr = module.sys.stderr
            primary = RuntimeError("primary lifecycle result")
            module.os.unlink = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("cleanup failed")
            )
            module.sys.stderr = BrokenDiagnostics()
            try:
                with self.assertRaises(RuntimeError) as raised:
                    try:
                        raise primary
                    except RuntimeError:
                        module._unlink_owned_temporary(
                            runtime_fd, temporary.name, (info.st_dev, info.st_ino)
                        )
                        raise
            finally:
                module.sys.stderr = original_stderr
                module.os.unlink = original_unlink
                os.close(runtime_fd)
            self.assertIs(raised.exception, primary)

    def test_dispatcher_owned_cleanup_survives_broken_stderr(self):
        self._assert_broken_stderr_cannot_mask_owned_cleanup(
            load_deploy_module("matrix_ci_physical_dispatcher")
        )

    def test_verifier_owned_cleanup_survives_broken_stderr(self):
        self._assert_broken_stderr_cannot_mask_owned_cleanup(
            load_deploy_module("matrix_ci_physical_verifier")
        )

    def test_scavenger_unlink_and_fsync_warnings_survive_broken_stderr(self):
        class BrokenDiagnostics:
            def write(self, _payload):
                raise BrokenPipeError("stderr transport closed")

            def flush(self):
                raise OSError("stderr flush closed")

        for module in (
            load_deploy_module("matrix_ci_physical_dispatcher"),
            load_deploy_module("matrix_ci_physical_verifier"),
        ):
            for failure in ("unlink", "fsync"):
                with self.subTest(module=module.__name__, failure=failure):
                    with tempfile.TemporaryDirectory(
                        prefix="matrix-ci-broken-scavenger-", dir="/root"
                    ) as directory:
                        runtime = Path(directory)
                        (runtime / "stale").write_bytes(b"private")
                        runtime_fd = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY)
                        original_unlink = module.os.unlink
                        original_fsync = module.os.fsync
                        original_stderr = module.sys.stderr
                        if failure == "unlink":
                            module.os.unlink = lambda *_args, **_kwargs: (
                                _ for _ in ()
                            ).throw(OSError("unlink failed"))
                        else:
                            module.os.fsync = lambda *_args, **_kwargs: (
                                _ for _ in ()
                            ).throw(OSError("fsync failed"))
                        module.sys.stderr = BrokenDiagnostics()
                        try:
                            removed = module._scavenge_quiescent_temporaries(
                                runtime_fd, limit=1
                            )
                        finally:
                            module.sys.stderr = original_stderr
                            module.os.fsync = original_fsync
                            module.os.unlink = original_unlink
                            os.close(runtime_fd)
                        self.assertEqual(removed, 0 if failure == "unlink" else 1)

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
                            removed = module._scavenge_quiescent_temporaries(runtime_fd)
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
                            removed = module._scavenge_quiescent_temporaries(runtime_fd)
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

    def test_host_owned_physical_runtime_exists_before_web_namespace_is_applied(self):
        policy_path = ROOT / "deploy" / "matrix-ci-physical.conf"
        self.assertTrue(policy_path.is_file())
        self.assertEqual(
            policy_path.read_text(encoding="ascii"),
            "# Host-owned prerequisite for matrix-web InaccessiblePaths.\n"
            "d /run/matrix-ci-physical 0700 root root -\n",
        )
        web_unit = (ROOT / "deploy" / "matrix-web.service").read_text(encoding="utf-8")
        self.assertIn("InaccessiblePaths=/run/matrix-ci-physical", web_unit)
        self.assertIn("Requires=matrix-network-setup.service systemd-tmpfiles-setup.service", web_unit)
        install = (ROOT / "deploy" / "matrix-ci-physical-install.md").read_text(encoding="utf-8")
        source = "deploy/matrix-ci-physical.conf"
        destination = "/etc/tmpfiles.d/matrix-ci-physical.conf"
        create = f"/usr/bin/systemd-tmpfiles --create {destination}"
        self.assertIn(f"{source}:{destination}", install)
        self.assertIn(create, install)
        self.assertLess(install.index(create), install.index("systemctl enable --now"))
        gate = (ROOT / "deploy" / "verify_matrix_ci_physical_install.py").read_text(
            encoding="utf-8"
        )
        pipeline = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")
        self.assertIn(destination, gate)
        self.assertIn(source, pipeline)

    def test_restart_install_gate_rejects_non_matrix_web_request_directory(self):
        gate = load_deploy_module("verify_matrix_ci_physical_install")
        with tempfile.TemporaryDirectory(prefix="matrix-restart-install-", dir="/root") as directory:
            root = Path(directory)
            broker = root / "broker"
            service = root / "service"
            path_unit = root / "path"
            policy = root / "policy"
            for artifact in (broker, service, path_unit):
                artifact.write_bytes(b"reviewed\n")
            os.chmod(broker, 0o755)
            os.chmod(service, 0o644)
            os.chmod(path_unit, 0o644)
            policy.write_bytes(gate.EXPECTED_RESTART_TMPFILES_POLICY)
            os.chmod(policy, 0o644)
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            transaction_lock = runtime / "transaction.lock"
            transaction_lock.write_bytes(b"")
            os.chmod(transaction_lock, 0o644)
            submit = root / "submit"
            submit.mkdir(mode=0o700)
            os.chown(submit, 65534, 65534)
            old_paths = (
                gate.RESTART_BROKER,
                gate.RESTART_SERVICE,
                gate.RESTART_PATH,
                gate.RESTART_TMPFILES_POLICY,
                gate.RESTART_DIRECTORY,
            )
            old_submit_directory = getattr(gate, "RESTART_SUBMIT_DIRECTORY")
            old_root_directory = getattr(gate, "RESTART_ROOT_DIRECTORY")
            old_getpwnam = gate.pwd.getpwnam
            old_digests = gate.EXPECTED_ARTIFACT_DIGESTS
            (
                gate.RESTART_BROKER,
                gate.RESTART_SERVICE,
                gate.RESTART_PATH,
                gate.RESTART_TMPFILES_POLICY,
                gate.RESTART_DIRECTORY,
            ) = (broker, service, path_unit, policy, runtime)
            setattr(gate, "RESTART_ROOT_DIRECTORY", root)
            setattr(gate, "RESTART_SUBMIT_DIRECTORY", submit)
            gate.pwd.getpwnam = lambda name: types.SimpleNamespace(
                pw_uid=12345, pw_gid=12345
            ) if name == "matrix-web" else old_getpwnam(name)
            gate.EXPECTED_ARTIFACT_DIGESTS = {
                broker: hashlib.sha256(broker.read_bytes()).hexdigest()
            }
            try:
                os.chmod(root, 0o755)
                with self.assertRaisesRegex(RuntimeError, "restart root directory"):
                    gate.verify_restart_broker_install()
                os.chmod(root, 0o700)
                with self.assertRaisesRegex(RuntimeError, "restart submission directory"):
                    gate.verify_restart_broker_install()
            finally:
                gate.pwd.getpwnam = old_getpwnam
                gate.EXPECTED_ARTIFACT_DIGESTS = old_digests
                (
                    gate.RESTART_BROKER,
                    gate.RESTART_SERVICE,
                    gate.RESTART_PATH,
                    gate.RESTART_TMPFILES_POLICY,
                    gate.RESTART_DIRECTORY,
                ) = old_paths
                setattr(gate, "RESTART_ROOT_DIRECTORY", old_root_directory)
                setattr(gate, "RESTART_SUBMIT_DIRECTORY", old_submit_directory)

    def test_restart_install_gate_rejects_cross_device_runtime_children(self):
        gate = load_deploy_module("verify_matrix_ci_physical_install")

        class DirectoryView:
            def __init__(self, path, device):
                self.path = path
                self.device = device

            def lstat(self):
                info = self.path.lstat()
                return types.SimpleNamespace(
                    st_mode=info.st_mode,
                    st_uid=info.st_uid,
                    st_gid=info.st_gid,
                    st_dev=self.device,
                )

            def __truediv__(self, child):
                return self.path / child

        with tempfile.TemporaryDirectory(prefix="matrix-restart-device-", dir="/root") as directory:
            root = Path(directory)
            state = root / "state"
            submit = root / "submit"
            state.mkdir(mode=0o700)
            submit.mkdir(mode=0o1730)
            os.chmod(submit, 0o1730)
            os.chown(submit, 0, 12345)
            transaction_lock = state / "transaction.lock"
            transaction_lock.write_bytes(b"")
            os.chmod(transaction_lock, 0o644)
            broker = root / "broker"
            service = root / "service"
            path_unit = root / "path"
            policy = root / "policy"
            for artifact in (broker, service, path_unit):
                artifact.write_bytes(b"reviewed\n")
            os.chmod(broker, 0o755)
            os.chmod(service, 0o644)
            os.chmod(path_unit, 0o644)
            policy.write_bytes(gate.EXPECTED_RESTART_TMPFILES_POLICY)
            os.chmod(policy, 0o644)
            old_paths = (
                gate.RESTART_BROKER,
                gate.RESTART_SERVICE,
                gate.RESTART_PATH,
                gate.RESTART_TMPFILES_POLICY,
                gate.RESTART_ROOT_DIRECTORY,
                gate.RESTART_DIRECTORY,
                gate.RESTART_SUBMIT_DIRECTORY,
            )
            old_getpwnam = gate.pwd.getpwnam
            old_digests = gate.EXPECTED_ARTIFACT_DIGESTS
            root_device = root.lstat().st_dev
            setattr(gate, "RESTART_BROKER", broker)
            setattr(gate, "RESTART_SERVICE", service)
            setattr(gate, "RESTART_PATH", path_unit)
            setattr(gate, "RESTART_TMPFILES_POLICY", policy)
            gate.pwd.getpwnam = lambda name: types.SimpleNamespace(
                pw_uid=12345, pw_gid=12345
            ) if name == "matrix-web" else old_getpwnam(name)
            setattr(gate, "EXPECTED_ARTIFACT_DIGESTS", {
                broker: hashlib.sha256(broker.read_bytes()).hexdigest()
            })
            try:
                for differing_child in ("state", "submit"):
                    with self.subTest(differing_child=differing_child):
                        setattr(gate, "RESTART_ROOT_DIRECTORY", DirectoryView(root, root_device))
                        setattr(
                            gate,
                            "RESTART_DIRECTORY",
                            DirectoryView(state, root_device + (differing_child == "state")),
                        )
                        setattr(
                            gate,
                            "RESTART_SUBMIT_DIRECTORY",
                            DirectoryView(submit, root_device + (differing_child == "submit")),
                        )
                        with self.assertRaisesRegex(RuntimeError, "restart runtime filesystem"):
                            gate.verify_restart_broker_install()
            finally:
                gate.pwd.getpwnam = old_getpwnam
                setattr(gate, "EXPECTED_ARTIFACT_DIGESTS", old_digests)
                for name, value in zip(
                    (
                        "RESTART_BROKER",
                        "RESTART_SERVICE",
                        "RESTART_PATH",
                        "RESTART_TMPFILES_POLICY",
                        "RESTART_ROOT_DIRECTORY",
                        "RESTART_DIRECTORY",
                        "RESTART_SUBMIT_DIRECTORY",
                    ),
                    old_paths,
                ):
                    setattr(gate, name, value)

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
        self.assertRegex(
            web_unit,
            r"(?m)^InaccessiblePaths=/run/matrix-ci-physical /run/matrix-device-restart/state "
            r"/etc/shadow /etc/gshadow /etc/ssh$",
        )

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

    def test_report_publication_is_descriptor_confined_to_dedicated_temporary_namespace(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        values = {"SOURCE_SHA": "a" * 40, "PIPELINE_ID": "99", "PHYSICAL_DEVICE_ACCEPTANCE": "PASS"}
        expected = verifier_report("a" * 40, "99")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-report-namespace-", dir="/root") as directory:
            runtime = Path(directory)
            os.chmod(runtime, 0o700)
            target = runtime / "outside-target"
            target.write_bytes(b"unchanged")
            hostile = runtime / "report.tmp.999999.0"
            os.link(target, hostile)
            old_report = verifier.REPORT_FILE
            original_rename = verifier._rename_noreplace
            publications = []
            verifier.REPORT_FILE = runtime / "report"

            def observe_rename(old_name, new_name, source_fd, target_fd=None):
                effective_target_fd = source_fd if target_fd is None else target_fd
                publications.append((
                    old_name,
                    new_name,
                    os.fstat(source_fd).st_ino,
                    os.fstat(effective_target_fd).st_ino,
                ))
                return original_rename(old_name, new_name, source_fd, target_fd)

            verifier._rename_noreplace = observe_rename
            try:
                verifier._write_report(values)
            finally:
                verifier._rename_noreplace = original_rename
                verifier.REPORT_FILE = old_report

            self.assertEqual((runtime / "report").read_bytes(), expected)
            self.assertEqual(target.read_bytes(), b"unchanged")
            self.assertEqual(hostile.stat().st_nlink, 2)
            self.assertEqual(len(publications), 1)
            temporary_name, published_name, source_inode, target_inode = publications[0]
            self.assertRegex(temporary_name, r"^report\.tmp\.[0-9]+\.[0-9]+$")
            self.assertEqual(published_name, "report")
            self.assertNotEqual(source_inode, target_inode)
            namespace = runtime / verifier.REPORT_TEMPORARIES
            self.assertEqual(list(namespace.iterdir()), [])

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

    def test_rgb_installer_rejects_zero_progress_write(self):
        installer = load_deploy_module("matrix_ci_install_rgb_bindings")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-rgb-write-", dir="/root") as directory:
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            original_write = installer.os.write
            installer.os.write = lambda _fd, _payload: 0
            try:
                with self.assertRaisesRegex(RuntimeError, "zero-progress"):
                    installer._write_file(directory_fd, "payload", b"reviewed")
            finally:
                installer.os.write = original_write
                os.close(directory_fd)

    def test_rgb_installer_write_failure_exposes_no_final_and_retry_succeeds(self):
        installer = load_deploy_module("matrix_ci_install_rgb_bindings")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-rgb-source-", dir="/root") as source_dir, tempfile.TemporaryDirectory(prefix="matrix-ci-rgb-parent-", dir="/root") as parent_dir:
            source, parent = Path(source_dir), Path(parent_dir)
            destination = parent / "bindings"
            payloads = {name: (name + "-reviewed").encode("ascii") for name in installer.EXPECTED_FILES}
            for name, payload in payloads.items():
                (source / name).write_bytes(payload)
            manifest = "".join(
                f"{hashlib.sha256(payloads[name]).hexdigest()}  rgbmatrix/{name}\n"
                for name in sorted(payloads)
            ).encode("ascii")
            original_write_file = installer._write_file
            calls = 0

            def fail_second_write(*args):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected partial write failure")
                return original_write_file(*args)

            installer._write_file = fail_second_write
            try:
                with self.assertRaisesRegex(OSError, "injected partial write failure"):
                    installer.install(source, destination, manifest)
            finally:
                installer._write_file = original_write_file
            self.assertFalse(destination.exists())
            self.assertEqual(list(parent.iterdir()), [])
            installer.install(source, destination, manifest)
            self.assertTrue((destination / "rgbmatrix" / "core.so").is_file())

    def test_rgb_installer_fault_boundaries_never_expose_partial_final(self):
        for fault in ("package-create", *(f"fsync-{index}" for index in range(1, 7)), "publish"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(prefix="matrix-ci-rgb-source-", dir="/root") as source_dir, tempfile.TemporaryDirectory(prefix="matrix-ci-rgb-parent-", dir="/root") as parent_dir:
                installer = load_deploy_module("matrix_ci_install_rgb_bindings")
                source, parent = Path(source_dir), Path(parent_dir)
                destination = parent / "bindings"
                payloads = {name: (name + "-reviewed").encode("ascii") for name in installer.EXPECTED_FILES}
                for name, payload in payloads.items():
                    (source / name).write_bytes(payload)
                manifest = "".join(
                    f"{hashlib.sha256(payloads[name]).hexdigest()}  rgbmatrix/{name}\n"
                    for name in sorted(payloads)
                ).encode("ascii")
                original_mkdir = installer.os.mkdir
                original_fsync = installer.os.fsync
                original_publish = installer._rename_noreplace
                fsync_calls = 0

                def injected_mkdir(name, *args, **kwargs):
                    if fault == "package-create" and name == "rgbmatrix":
                        raise OSError("injected package create failure")
                    return original_mkdir(name, *args, **kwargs)

                def injected_fsync(fd):
                    nonlocal fsync_calls
                    fsync_calls += 1
                    if fault == f"fsync-{fsync_calls}":
                        raise OSError("injected fsync failure")
                    return original_fsync(fd)

                def injected_publish(*args):
                    if fault == "publish":
                        raise OSError("injected publication failure")
                    self.assertFalse(destination.exists(), "final appeared before atomic publication")
                    return original_publish(*args)

                installer.os.mkdir = injected_mkdir
                installer.os.fsync = injected_fsync
                installer._rename_noreplace = injected_publish
                try:
                    with self.assertRaisesRegex(OSError, "injected"):
                        installer.install(source, destination, manifest)
                finally:
                    installer.os.mkdir = original_mkdir
                    installer.os.fsync = original_fsync
                    installer._rename_noreplace = original_publish
                self.assertFalse(destination.exists())
                self.assertEqual(list(parent.iterdir()), [])

    def test_rgb_installer_rejects_hostile_final_and_skips_prepositioned_staging_sibling(self):
        installer = load_deploy_module("matrix_ci_install_rgb_bindings")
        with tempfile.TemporaryDirectory(prefix="matrix-ci-rgb-source-", dir="/root") as source_dir, tempfile.TemporaryDirectory(prefix="matrix-ci-rgb-parent-", dir="/root") as parent_dir:
            source, parent = Path(source_dir), Path(parent_dir)
            payloads = {name: name.encode("ascii") for name in installer.EXPECTED_FILES}
            for name, payload in payloads.items():
                (source / name).write_bytes(payload)
            manifest = "".join(
                f"{hashlib.sha256(payloads[name]).hexdigest()}  rgbmatrix/{name}\n"
                for name in sorted(payloads)
            ).encode("ascii")
            target = parent / "hostile-target"
            target.write_text("unchanged", encoding="ascii")
            destination = parent / "bindings"
            destination.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, "fresh trusted path"):
                installer.install(source, destination, manifest)
            self.assertTrue(destination.is_symlink())
            self.assertEqual(target.read_text(encoding="ascii"), "unchanged")
            destination.unlink()

            first, second = b"a" * 16, b"b" * 16
            hostile = parent / f".bindings.tmp.{os.getpid()}.{first.hex()}"
            hostile.write_text("hostile", encoding="ascii")
            values = iter((first, second))
            original_urandom = installer.os.urandom
            installer.os.urandom = lambda _count: next(values)
            try:
                installer.install(source, destination, manifest)
            finally:
                installer.os.urandom = original_urandom
            self.assertEqual(hostile.read_text(encoding="ascii"), "hostile")
            self.assertTrue((destination / "rgbmatrix.sha256").is_file())

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

    def test_state_snapshot_rejects_symlinks_and_hardlinks(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        with tempfile.TemporaryDirectory(prefix="matrix-state-tree-", dir="/root") as directory:
            root = Path(directory)
            target = root.parent / f"{root.name}-outside"
            target.write_bytes(b"outside")
            try:
                (root / "symlink").symlink_to(target)
                with self.assertRaisesRegex(RuntimeError, "state tree"):
                    verifier._tree_digest(root)
                (root / "symlink").unlink()
                os.link(target, root / "hardlink")
                with self.assertRaisesRegex(RuntimeError, "state tree"):
                    verifier._tree_digest(root)
            finally:
                target.unlink(missing_ok=True)

    def test_state_directory_accepts_overlayfs_link_count_without_weakening_mode_checks(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        overlay_directory = types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_nlink=1)
        writable_directory = types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o775, st_nlink=1)

        self.assertTrue(verifier._state_directory(overlay_directory))
        self.assertFalse(verifier._state_directory(writable_directory))

    def test_state_snapshot_rejects_fifo_socket_and_device_nodes(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        with tempfile.TemporaryDirectory(prefix="matrix-state-special-", dir="/root") as directory:
            root = Path(directory)
            creators = {
                "fifo": lambda path: os.mkfifo(path, 0o600),
                "socket": lambda path: __import__("socket").socket(__import__("socket").AF_UNIX).bind(str(path)),
                "device": lambda path: os.mknod(path, stat.S_IFCHR | 0o600, os.makedev(1, 3)),
            }
            for kind, create in creators.items():
                path = root / kind
                resource = None
                try:
                    if kind == "socket":
                        resource = __import__("socket").socket(__import__("socket").AF_UNIX)
                        resource.bind(str(path))
                    else:
                        create(path)
                    with self.subTest(kind=kind):
                        with self.assertRaisesRegex(RuntimeError, "state tree"):
                            verifier._tree_digest(root)
                finally:
                    if resource is not None:
                        resource.close()
                    path.unlink(missing_ok=True)

    def test_state_snapshot_rejects_path_replacement_race(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        with tempfile.TemporaryDirectory(prefix="matrix-state-race-", dir="/root") as directory:
            root = Path(directory)
            victim = root / "victim"
            victim.write_bytes(b"reviewed")
            outside = root.parent / f"{root.name}-outside"
            outside.write_bytes(b"attacker")
            original_os_open = verifier.os.open
            original_path_open = Path.open
            replaced = False

            def replace():
                nonlocal replaced
                if not replaced:
                    victim.unlink()
                    victim.symlink_to(outside)
                    replaced = True

            def racing_os_open(path, *args, **kwargs):
                if path == "victim" and kwargs.get("dir_fd") is not None:
                    replace()
                return original_os_open(path, *args, **kwargs)

            def racing_path_open(path, *args, **kwargs):
                if path == victim:
                    replace()
                return original_path_open(path, *args, **kwargs)

            try:
                with mock.patch.object(verifier.os, "open", side_effect=racing_os_open), \
                     mock.patch.object(Path, "open", autospec=True, side_effect=racing_path_open):
                    with self.assertRaisesRegex((RuntimeError, OSError), "state|symlink|loop|unsafe"):
                        verifier._tree_digest(root)
            finally:
                outside.unlink(missing_ok=True)

    def test_state_snapshot_rejects_each_file_metadata_race_and_closes_descriptor(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        mutations = {
            "st_mode": lambda value: value ^ stat.S_IXUSR,
            "st_uid": lambda value: value + 1,
            "st_gid": lambda value: value + 1,
            "st_nlink": lambda value: value + 1,
            "st_size": lambda value: value + 1,
            "st_mtime_ns": lambda value: value + 1,
            "st_ctime_ns": lambda value: value + 1,
        }

        for window in ("admitted-to-opened", "closed-to-current"):
            for field, mutate in mutations.items():
                with self.subTest(window=window, field=field):
                    with tempfile.TemporaryDirectory(prefix="matrix-state-metadata-race-", dir="/root") as directory:
                        root = Path(directory)
                        victim = root / "victim"
                        victim.write_bytes(b"reviewed")
                        original_open = verifier.os.open
                        original_fstat = verifier.os.fstat
                        original_stat = verifier.os.stat
                        victim_fd = None
                        victim_stats = 0

                        def changed(info):
                            values = {
                                name: getattr(info, name)
                                for name in (
                                    "st_dev", "st_ino", "st_mode", "st_uid", "st_gid",
                                    "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns",
                                )
                            }
                            values[field] = mutate(values[field])
                            return types.SimpleNamespace(**values)

                        def racing_open(path, *args, **kwargs):
                            nonlocal victim_fd
                            fd = original_open(path, *args, **kwargs)
                            if path == "victim" and kwargs.get("dir_fd") is not None:
                                victim_fd = fd
                            return fd

                        def racing_fstat(fd):
                            info = original_fstat(fd)
                            if window == "admitted-to-opened" and fd == victim_fd:
                                return changed(info)
                            return info

                        def racing_stat(path, *args, **kwargs):
                            nonlocal victim_stats
                            info = original_stat(path, *args, **kwargs)
                            if path == "victim" and kwargs.get("dir_fd") is not None:
                                victim_stats += 1
                                if window == "closed-to-current" and victim_stats == 2:
                                    return changed(info)
                            return info

                        with mock.patch.object(verifier.os, "open", side_effect=racing_open), \
                             mock.patch.object(verifier.os, "fstat", side_effect=racing_fstat), \
                             mock.patch.object(verifier.os, "stat", side_effect=racing_stat):
                            with self.assertRaisesRegex(RuntimeError, "state tree file identity changed"):
                                verifier._tree_digest(root)

                        self.assertIsNotNone(victim_fd)
                        with self.assertRaises(OSError):
                            original_fstat(victim_fd)

    def test_state_snapshot_allows_exact_depth_bound_and_rejects_overflow_before_descent(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        max_depth = verifier.MAX_STATE_DEPTH
        self.assertGreater(max_depth, 0)

        with tempfile.TemporaryDirectory(prefix="matrix-state-depth-boundary-", dir="/root") as directory:
            root = Path(directory)
            cursor = root
            for depth in range(max_depth):
                cursor /= f"level-{depth:02d}"
                cursor.mkdir()
            (cursor / "leaf").write_bytes(b"boundary")
            self.assertRegex(verifier._tree_digest(root), r"^[0-9a-f]{64}$")

        with tempfile.TemporaryDirectory(prefix="matrix-state-depth-overflow-", dir="/root") as directory:
            root = Path(directory)
            cursor = root
            for depth in range(max_depth + 1):
                cursor /= f"level-{depth:02d}"
                cursor.mkdir()
            (cursor / "leaf").write_bytes(b"overflow")
            overflow_name = f"level-{max_depth:02d}"
            original_open = verifier.os.open
            descended = []

            def recording_open(path, *args, **kwargs):
                if kwargs.get("dir_fd") is not None:
                    descended.append(path)
                return original_open(path, *args, **kwargs)

            with mock.patch.object(verifier.os, "open", side_effect=recording_open):
                with self.assertRaises(RuntimeError) as raised:
                    verifier._tree_digest(root)

            self.assertEqual(str(raised.exception), "state tree depth bound exceeded")
            self.assertNotIn(overflow_name, descended)
            self.assertNotIn(overflow_name, str(raised.exception))

    def test_state_snapshot_enforces_file_aggregate_and_entry_bounds(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        max_file_bytes = 1024 * 1024
        max_aggregate_bytes = 2 * 1024 * 1024
        max_entries = 256
        cases = (
            ("file", lambda root: (root / "large").write_bytes(b"x" * (max_file_bytes + 1))),
            ("aggregate", lambda root: [
                (root / f"part-{index}").write_bytes(b"x" * (max_file_bytes - 1))
                for index in range(3)
            ]),
            ("entries", lambda root: [
                (root / f"entry-{index}").write_bytes(b"")
                for index in range(max_entries + 1)
            ]),
        )
        for kind, populate in cases:
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory(prefix="matrix-state-bound-", dir="/root") as directory:
                    root = Path(directory)
                    populate(root)
                    with self.assertRaisesRegex(RuntimeError, "state tree"):
                        verifier._tree_digest(root)

    def test_verifier_stops_production_before_snapshot_and_skips_hardware_when_admission_fails(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        events = []

        class SnapshotFailureOps(FakeOps):
            def snapshot(self):
                events.append(("snapshot",))
                if self.active:
                    raise RuntimeError("snapshot while production active")
                raise RuntimeError("unsafe state tree")

        ops = SnapshotFailureOps(events)
        opened = []
        report = verifier.run_verification(
            "a" * 40, "122", ops=ops,
            lock_factory=lambda: FakeLock(events),
            matrix_factory=lambda: opened.append(True),
        )
        self.assertEqual(report["PHYSICAL_DEVICE_ACCEPTANCE"], "FAIL")
        self.assertIn("unsafe state tree", report["ERROR"])
        self.assertLess(events.index(("service", "stop")), events.index(("snapshot",)))
        self.assertFalse(ops.active)
        self.assertEqual(opened, [])

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

    def test_verifier_preflight_retries_transient_http_unreadiness_before_mutation(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        ready = iter((False, False, True))
        sleeps = []
        ops = verifier.ApplianceOps()
        original_http_ready = ops.http_ready
        old_sleep = verifier.time.sleep
        ops.http_ready = lambda: next(ready)
        verifier.time.sleep = sleeps.append
        try:
            self.assertTrue(ops.initial_ready())
        finally:
            ops.http_ready = original_http_ready
            verifier.time.sleep = old_sleep
        self.assertEqual(sleeps, [1, 1])

    def test_verifier_preflight_exhaustion_is_bounded_and_skips_matrix_exercise(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        probes = []
        sleeps = []
        ops = verifier.ApplianceOps()
        original_http_ready = ops.http_ready
        old_sleep = verifier.time.sleep
        ops.http_ready = lambda: (probes.append("probe") or False)
        verifier.time.sleep = sleeps.append
        try:
            self.assertFalse(ops.initial_ready())
        finally:
            ops.http_ready = original_http_ready
            verifier.time.sleep = old_sleep
        self.assertEqual(len(probes), 12)
        self.assertEqual(sleeps, [1] * 11)

        events = []

        class ExhaustedPreflightOps(FakeOps):
            def initial_ready(self):
                self.events.append(("http", "bounded-initial-exhausted"))
                return False

        report = verifier.run_verification(
            "c" * 40,
            "456",
            ops=ExhaustedPreflightOps(events),
            lock_factory=lambda: FakeLock(events),
            matrix_factory=lambda: (events.append(("matrix", "create")) or FakeMatrix(events)),
        )
        self.assertEqual(report["PHYSICAL_DEVICE_ACCEPTANCE"], "FAIL")
        self.assertNotIn(("matrix", "create"), events)

    def test_verification_uses_bounded_initial_readiness_before_stopping_production(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        events = []

        class TransientPreflightOps(FakeOps):
            def __getattribute__(self, name):
                if name == "http_ready":
                    raise AssertionError("bounded initial readiness must not require http_ready")
                return super().__getattribute__(name)

            def initial_ready(self):
                self.events.append(("http", "bounded-initial-ready"))
                return True

        ops = TransientPreflightOps(events)
        report = verifier.run_verification(
            "b" * 40,
            "455",
            ops=ops,
            lock_factory=lambda: FakeLock(events),
            matrix_factory=lambda: FakeMatrix(events),
        )

        self.assertEqual(report["PHYSICAL_DEVICE_ACCEPTANCE"], "PASS")
        self.assertIn(("http", "bounded-initial-ready"), events)
        self.assertNotIn(("http", "single-probe-would-fail"), events)
        self.assertIn(("service", "stop"), events)

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

    def test_state_tree_digest_framing_distinguishes_record_boundary_collision_shape(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        with tempfile.TemporaryDirectory(prefix="matrix-state-framing-", dir="/root") as directory:
            base = Path(directory)
            tree_a = base / "tree-a"
            tree_b = base / "tree-b"
            tree_a.mkdir(mode=0o700)
            tree_b.mkdir(mode=0o700)
            (tree_a / "a").write_bytes(b"x")
            (tree_a / "b").write_bytes(b"")
            (tree_b / "a").write_bytes(b"")
            (tree_b / "xb").write_bytes(b"")
            for path in (*tree_a.iterdir(), *tree_b.iterdir()):
                os.chmod(path, 0o600)

            self.assertNotEqual(verifier._tree_digest(tree_a), verifier._tree_digest(tree_b))

    def test_state_record_stream_framing_alone_distinguishes_collision_shape(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        identical_metadata = types.SimpleNamespace(
            st_dev=1, st_ino=2, st_mode=stat.S_IFREG | 0o600,
            st_uid=0, st_gid=0, st_nlink=1, st_size=0,
            st_mtime_ns=3, st_ctime_ns=4,
        )

        def canonical_stream(records):
            return b"".join(
                verifier._state_record_prefix(
                    verifier._STATE_RECORD_FILE,
                    path,
                    identical_metadata,
                    content_length=len(content),
                ) + content
                for path, content in records
            )

        first = ((b"a", b"x"), (b"b", b""))
        second = ((b"a", b""), (b"xb", b""))
        self.assertEqual(
            b"".join(path + content for path, content in first),
            b"".join(path + content for path, content in second),
        )
        self.assertNotEqual(canonical_stream(first), canonical_stream(second))

    def test_state_tree_digest_is_deterministic_for_zero_length_and_non_ascii_name(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        with tempfile.TemporaryDirectory(prefix="matrix-state-canonical-", dir="/root") as directory:
            root = Path(directory)
            filename = os.fsdecode(b"empty-\xff")
            (root / filename).write_bytes(b"")

            first = verifier._tree_digest(root)
            second = verifier._tree_digest(root)

            self.assertEqual(first, second)

    def test_state_tree_digest_changes_with_file_name(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        with tempfile.TemporaryDirectory(prefix="matrix-state-name-", dir="/root") as directory:
            root = Path(directory)
            original = root / "first"
            original.write_bytes(b"same")
            before = verifier._tree_digest(root)
            original.rename(root / "second")

            self.assertNotEqual(before, verifier._tree_digest(root))

    def test_state_tree_digest_changes_with_file_content(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        with tempfile.TemporaryDirectory(prefix="matrix-state-content-", dir="/root") as directory:
            root = Path(directory)
            target = root / "same"
            target.write_bytes(b"first")
            before = verifier._tree_digest(root)
            target.write_bytes(b"other")

            self.assertNotEqual(before, verifier._tree_digest(root))

    def test_state_record_encoding_rejects_overflow_metadata(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        info = types.SimpleNamespace(
            st_dev=1 << 64, st_ino=1, st_mode=stat.S_IFREG | 0o600,
            st_uid=0, st_gid=0, st_nlink=1, st_size=0,
            st_mtime_ns=0, st_ctime_ns=0,
        )

        with self.assertRaisesRegex(RuntimeError, "metadata is not encodable"):
            verifier._state_record_prefix(
                verifier._STATE_RECORD_FILE, b"a", info, content_length=0,
            )

    def test_mutable_state_is_compared_before_and_after_while_production_is_stopped(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        events = []

        class ChangedStateOps(FakeOps):
            def __init__(self, recorded_events):
                super().__init__(recorded_events)
                self.snapshot_count = 0

            def snapshot(self):
                snapshot = super().snapshot()
                self.snapshot_count += 1
                if self.snapshot_count == 2:
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

    def test_verifier_stop_deadline_includes_request_and_caps_each_poll_to_remaining_time(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        now = [0.0]
        calls = []
        original = verifier._run
        old_monotonic = verifier.time.monotonic
        old_sleep = verifier.time.sleep

        def fake_run(*args, check=True, timeout=None):
            calls.append((args, check, timeout))
            if args[1] == "stop":
                now[0] = 90.0
                return types.SimpleNamespace(returncode=0, stdout="")
            return types.SimpleNamespace(
                returncode=0, stdout="ActiveState=inactive\nJob=\n"
            )

        setattr(verifier, "_run", fake_run)
        verifier.time.monotonic = lambda: now[0]
        verifier.time.sleep = lambda _seconds: None
        try:
            verifier.ApplianceOps().stop_service()
        finally:
            setattr(verifier, "_run", original)
            verifier.time.monotonic = old_monotonic
            verifier.time.sleep = old_sleep

        self.assertEqual(calls[0][2], 15)
        self.assertEqual(calls[1][2], 10)

    def test_verifier_http_probe_rejects_slow_trickle_past_end_to_end_deadline(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        now = [0.0]
        select_timeouts = []
        response = iter(b"HTTP/1.1 200 OK\r\n")
        old_monotonic = verifier.time.monotonic
        old_urlopen = getattr(verifier, "urlopen", None)
        old_socket = getattr(verifier, "socket", None)
        old_select = getattr(verifier, "select", None)

        class SlowResponse:
            status = 200

            def __enter__(self):
                now[0] = 1.2
                return self

            def __exit__(self, *_args):
                return False

        class FakeSocket:
            def setblocking(self, _blocking):
                pass

            def connect_ex(self, _address):
                return verifier.errno.EINPROGRESS

            def getsockopt(self, *_args):
                return 0

            def send(self, data):
                return len(data)

            def recv(self, _size):
                now[0] += 0.6
                return bytes((next(response),))

            def close(self):
                pass

        fake_socket_module = types.SimpleNamespace(
            AF_INET=2,
            SOCK_STREAM=1,
            SOL_SOCKET=1,
            SO_ERROR=4,
            socket=lambda *_args: FakeSocket(),
        )

        def fake_select(readable, writable, exceptional, timeout):
            select_timeouts.append(timeout)
            if writable:
                return [], writable, []
            return readable, [], []

        setattr(verifier, "socket", fake_socket_module)
        setattr(verifier, "select", types.SimpleNamespace(select=fake_select))
        verifier.time.monotonic = lambda: now[0]
        setattr(verifier, "urlopen", lambda *_args, **_kwargs: SlowResponse())
        try:
            self.assertFalse(verifier.ApplianceOps().http_ready(timeout=1))
        finally:
            verifier.time.monotonic = old_monotonic
            if old_urlopen is None:
                delattr(verifier, "urlopen")
            else:
                setattr(verifier, "urlopen", old_urlopen)
            if old_socket is None:
                delattr(verifier, "socket")
            else:
                setattr(verifier, "socket", old_socket)
            if old_select is None:
                delattr(verifier, "select")
            else:
                setattr(verifier, "select", old_select)
        self.assertTrue(select_timeouts)
        self.assertLessEqual(now[0], 1.2)

    def test_verifier_http_probe_close_failure_preserves_probe_result(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        old_socket = verifier.socket
        old_select = verifier.select

        class CloseFailingSocket:
            def __init__(self, response):
                self.response = response

            def setblocking(self, _blocking):
                pass

            def connect_ex(self, _address):
                return 0

            def send(self, data):
                return len(data)

            def recv(self, _size):
                response, self.response = self.response, b""
                return response

            def close(self):
                raise OSError("close failed")

        try:
            for response, expected in (
                (b"HTTP/1.1 200 OK\r\n", True),
                (b"HTTP/1.1 503 Unavailable\r\n", False),
            ):
                with self.subTest(response=response):
                    fake_socket = CloseFailingSocket(response)
                    setattr(
                        verifier,
                        "socket",
                        types.SimpleNamespace(
                            AF_INET=2,
                            SOCK_STREAM=1,
                            SOL_SOCKET=1,
                            SO_ERROR=4,
                            socket=lambda *_args: fake_socket,
                        ),
                    )
                    setattr(
                        verifier,
                        "select",
                        types.SimpleNamespace(
                            select=lambda readable, writable, exceptional, _timeout: (
                                readable, writable, []
                            )
                        ),
                    )
                    self.assertEqual(
                        verifier.ApplianceOps().http_ready(timeout=1), expected
                    )
        finally:
            setattr(verifier, "socket", old_socket)
            setattr(verifier, "select", old_select)

    def test_verifier_start_exceptions_clear_shared_readiness_deadline(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        original = verifier._run

        try:
            for error in (
                subprocess.TimeoutExpired(("systemctl", "start"), 15),
                OSError("start failed"),
            ):
                with self.subTest(error=type(error).__name__):
                    ops = verifier.ApplianceOps()

                    def fail_start(*_args, **_kwargs):
                        raise error

                    setattr(verifier, "_run", fail_start)
                    with self.assertRaises(type(error)):
                        ops.start_service()
                    self.assertFalse(hasattr(ops, "_ready_deadline"))
        finally:
            setattr(verifier, "_run", original)

    def test_verifier_start_and_readiness_share_one_capped_wall_clock_budget(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        now = [0.0]
        command_calls = []
        http_timeouts = []
        http_deadlines = []
        sleeps = []
        original = verifier._run
        old_monotonic = verifier.time.monotonic
        old_sleep = verifier.time.sleep
        ops = verifier.ApplianceOps()
        original_http_ready = ops.http_ready

        def fake_run(*args, check=True, timeout=None):
            command_calls.append((args, check, timeout))
            if args[1] == "start":
                now[0] = 50.0
                return types.SimpleNamespace(returncode=0, stdout="")
            now[0] = 55.0
            return types.SimpleNamespace(returncode=0, stdout="")

        def fake_http_ready(*, timeout=2, deadline=None):
            http_timeouts.append(timeout)
            http_deadlines.append(deadline)
            now[0] = 59.9
            return False

        setattr(verifier, "_run", fake_run)
        verifier.time.monotonic = lambda: now[0]
        verifier.time.sleep = lambda seconds: (
            sleeps.append(seconds), now.__setitem__(0, now[0] + seconds)
        )[-1]
        ops.http_ready = fake_http_ready
        try:
            ops.start_service()
            with self.assertRaisesRegex(RuntimeError, "did not recover"):
                ops.wait_ready()
        finally:
            setattr(verifier, "_run", original)
            verifier.time.monotonic = old_monotonic
            verifier.time.sleep = old_sleep
            ops.http_ready = original_http_ready

        self.assertEqual(command_calls[0][2], 15)
        self.assertEqual(command_calls[1][2], 10)
        self.assertEqual(http_timeouts, [2])
        self.assertEqual(http_deadlines, [60])
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 0.1)
        self.assertLessEqual(now[0], 60)

    def test_verifier_start_is_nonblocking_so_recovery_wait_owns_the_deadline(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        calls = []
        original = verifier._run

        def fake_run(*args, check=True, timeout=None):
            calls.append((args, check))
            return types.SimpleNamespace(returncode=0, stdout="")

        setattr(verifier, "_run", fake_run)
        try:
            verifier.ApplianceOps().start_service()
        finally:
            setattr(verifier, "_run", original)

        self.assertEqual(
            calls,
            [(("/bin/systemctl", "start", "--no-block", verifier.SERVICE), False)],
        )

    def test_verifier_stop_is_nonblocking_and_waits_for_systemd257_terminal_state(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        calls = []
        states = iter((
            "Job=74391\nActiveState=deactivating\n",
            "Job=\nActiveState=inactive\n",
        ))
        now = [0.0]
        original = verifier._run
        old_monotonic = verifier.time.monotonic
        old_sleep = verifier.time.sleep

        def fake_run(*args, check=True, timeout=None):
            calls.append((args, check))
            if args[1] == "stop":
                return types.SimpleNamespace(returncode=0, stdout="")
            return types.SimpleNamespace(returncode=0, stdout=next(states))

        setattr(verifier, "_run", fake_run)
        verifier.time.monotonic = lambda: now[0]
        verifier.time.sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
        try:
            verifier.ApplianceOps().stop_service()
        finally:
            setattr(verifier, "_run", original)
            verifier.time.monotonic = old_monotonic
            verifier.time.sleep = old_sleep

        self.assertEqual(
            calls,
            [
                (("/bin/systemctl", "stop", "--no-block", verifier.SERVICE), False),
                (("/bin/systemctl", "show", verifier.SERVICE, "-p", "ActiveState", "-p", "Job"), False),
                (("/bin/systemctl", "show", verifier.SERVICE, "-p", "ActiveState", "-p", "Job"), False),
            ],
        )

    def test_verifier_stop_and_start_state_fail_closed(self):
        verifier = load_deploy_module("matrix_ci_physical_verifier")
        original = verifier._run
        old_monotonic = verifier.time.monotonic
        old_sleep = verifier.time.sleep

        def run_stop(*, stop_rc=0, show_rc=0, state="ActiveState=inactive\nJob=\n"):
            now = [0.0]

            def fake_run(*args, check=True, timeout=None):
                if args[1] == "stop":
                    return types.SimpleNamespace(returncode=stop_rc, stdout="")
                return types.SimpleNamespace(returncode=show_rc, stdout=state)

            setattr(verifier, "_run", fake_run)
            verifier.time.monotonic = lambda: now[0]
            verifier.time.sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
            verifier.ApplianceOps().stop_service()

        try:
            with self.assertRaisesRegex(RuntimeError, "stop request failed"):
                run_stop(stop_rc=1)
            with self.assertRaisesRegex(RuntimeError, "stop state unavailable"):
                run_stop(show_rc=1)
            for state in (
                "ActiveState=inactive\n",
                "ActiveState=inactive\nJob=\nJob=\n",
                "ActiveState=active\nJob=\n",
                "ActiveState=inactive\nJob=0\n",
                "ActiveState=inactive\nJob=/org/freedesktop/systemd1/job/1\n",
            ):
                with self.subTest(state=state):
                    with self.assertRaisesRegex(RuntimeError, "stop state malformed|did not stop"):
                        run_stop(state=state)

            def failed_start(*args, check=True, timeout=None):
                return types.SimpleNamespace(returncode=1, stdout="")

            setattr(verifier, "_run", failed_start)
            with self.assertRaisesRegex(RuntimeError, "start request failed"):
                verifier.ApplianceOps().start_service()
        finally:
            setattr(verifier, "_run", original)
            verifier.time.monotonic = old_monotonic
            verifier.time.sleep = old_sleep

    def test_dispatcher_stop_is_nonblocking_and_waits_for_pid1_terminal_state(self):
        dispatcher = load_deploy_module("matrix_ci_physical_dispatcher")
        calls = []
        states = iter((
            b"Job=74391\nActiveState=deactivating\n",
            b"Job=\nActiveState=inactive\n",
        ))
        clock = iter((0.0, 0.0, 0.0, 2.0))
        original = dispatcher._systemctl
        old_monotonic = dispatcher.time.monotonic
        old_sleep = dispatcher.time.sleep
        old_wait = dispatcher.STOP_WAIT_SECONDS

        def fake_systemctl(*args, check=True):
            calls.append((args, check))
            if args[0] == "stop":
                return types.SimpleNamespace(returncode=0, stdout=b"")
            return types.SimpleNamespace(returncode=0, stdout=next(states))

        dispatcher._systemctl = fake_systemctl
        dispatcher.time.monotonic = lambda: next(clock)
        dispatcher.time.sleep = lambda _seconds: None
        setattr(dispatcher, "STOP_WAIT_SECONDS", 1)
        try:
            self.assertEqual(dispatcher.SystemOps().stop_and_wait(), 0)
        finally:
            setattr(dispatcher, "_systemctl", original)
            dispatcher.time.monotonic = old_monotonic
            dispatcher.time.sleep = old_sleep
            setattr(dispatcher, "STOP_WAIT_SECONDS", old_wait)

        self.assertEqual(
            calls,
            [
                (("stop", "--no-block", dispatcher.SERVICE), False),
                (("show", dispatcher.SERVICE, "-p", "ActiveState", "-p", "Job"), False),
                (("show", dispatcher.SERVICE, "-p", "ActiveState", "-p", "Job"), False),
            ],
        )

    def test_dispatcher_stop_state_parser_is_fail_closed(self):
        dispatcher = load_deploy_module("matrix_ci_physical_dispatcher")
        original = dispatcher._systemctl
        old_monotonic = dispatcher.time.monotonic
        old_sleep = dispatcher.time.sleep
        old_wait = dispatcher.STOP_WAIT_SECONDS

        def run_with(state, clock):
            def fake_systemctl(*args, check=True):
                if args[0] == "stop":
                    return types.SimpleNamespace(returncode=0, stdout=b"")
                return types.SimpleNamespace(returncode=0, stdout=state)

            setattr(dispatcher, "_systemctl", fake_systemctl)
            ticks = iter(clock)
            dispatcher.time.monotonic = lambda: next(ticks)
            dispatcher.time.sleep = lambda _seconds: None
            setattr(dispatcher, "STOP_WAIT_SECONDS", 1)
            return dispatcher.SystemOps().stop_and_wait()

        try:
            self.assertEqual(run_with(b"ActiveState=failed\nJob=\n", (0.0, 0.0)), 1)
            self.assertEqual(run_with(b"ActiveState=active\nJob=\n", (0.0, 0.0)), 1)
            for malformed in (
                b"ActiveState=inactive\nJob=0\n",
                b"ActiveState=inactive\nJob=-1\n",
                b"ActiveState=inactive\nJob=/org/freedesktop/systemd1/job/51\n",
                b"ActiveState=inactive\nJob=/untrusted/job/51\n",
            ):
                with self.subTest(malformed=malformed):
                    with self.assertRaisesRegex(RuntimeError, "stop state was malformed"):
                        run_with(malformed, (0.0, 0.0))
            with self.assertRaisesRegex(RuntimeError, "stop wait timed out"):
                run_with(
                    b"ActiveState=deactivating\nJob=74391\n",
                    (0.0, 0.0, 2.0),
                )
        finally:
            setattr(dispatcher, "_systemctl", original)
            dispatcher.time.monotonic = old_monotonic
            dispatcher.time.sleep = old_sleep
            setattr(dispatcher, "STOP_WAIT_SECONDS", old_wait)

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
        stop_timeout_match = re.search(r"(?m)^TimeoutStopSec=([1-9][0-9]*)$", unit)
        if stop_timeout_match is None:
            self.fail("physical service stop timeout is missing")
        stop_timeout = int(stop_timeout_match.group(1))
        self.assertGreaterEqual(
            load_deploy_module("matrix_ci_physical_dispatcher").STOP_WAIT_SECONDS,
            stop_timeout * 2 + 20,
        )
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
        self.assertIn('_systemctl("stop", "--no-block", SERVICE', dispatcher)
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
                        "trustedusercakeys none\n"
                        "authorizedprincipalsfile none\n"
                        "authorizedprincipalscommand none\n"
                        "authorizedprincipalscommanduser nobody\n"
                        "hostbasedauthentication no\n"
                        "pubkeyauthentication yes\n"
                        "pubkeyauthoptions none\n"
                        "ignorerhosts yes\n"
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

    def test_install_gate_accepts_only_device_allow_omission_under_auto_policy(self):
        gate = load_deploy_module("verify_matrix_ci_physical_install")
        original_run = gate.run
        calls = []

        def fake_run(*argv):
            calls.append(argv)
            self.assertEqual(
                argv,
                (
                    gate.SYSTEMCTL, "show", "matrix-ci-physical.service",
                    "-p", "DevicePolicy", "-p", "DeviceAllow",
                ),
            )
            return "DevicePolicy=auto\n"

        gate.run = fake_run
        try:
            self.assertEqual(
                gate.properties("matrix-ci-physical.service", "DevicePolicy", "DeviceAllow"),
                {"DevicePolicy": "auto", "DeviceAllow": ""},
            )
        finally:
            gate.run = original_run
        self.assertEqual(len(calls), 1)

    def test_install_gate_rejects_any_other_missing_effective_property(self):
        gate = load_deploy_module("verify_matrix_ci_physical_install")
        original_run = gate.run
        gate.run = lambda *_argv: "DevicePolicy=auto\n"
        try:
            with self.assertRaisesRegex(RuntimeError, "missing effective properties"):
                gate.properties(
                    "matrix-ci-physical.service", "DevicePolicy", "AmbientCapabilities"
                )
        finally:
            gate.run = original_run

    def test_install_gate_rejects_malformed_effective_property_output(self):
        gate = load_deploy_module("verify_matrix_ci_physical_install")
        original_run = gate.run
        gate.run = lambda *_argv: "DevicePolicy=auto\nmalformed\n"
        try:
            with self.assertRaisesRegex(RuntimeError, "malformed systemctl show output"):
                gate.properties("matrix-ci-physical.service", "DevicePolicy")
        finally:
            gate.run = original_run

    def test_install_gate_accepts_repeated_device_allow_records(self):
        gate = load_deploy_module("verify_matrix_ci_physical_install")
        original_run = gate.run
        gate.run = lambda *_argv: (
            "DevicePolicy=closed\n"
            "DeviceAllow=/dev/vcio rw\n"
            "DeviceAllow=/dev/gpiomem rw\n"
            "DeviceAllow=/dev/mem rw\n"
        )
        try:
            self.assertEqual(
                gate.properties("matrix-web.service", "DevicePolicy", "DeviceAllow"),
                {
                    "DevicePolicy": "closed",
                    "DeviceAllow": "/dev/vcio rw\n/dev/gpiomem rw\n/dev/mem rw",
                },
            )
        finally:
            gate.run = original_run

    def test_install_gate_rejects_duplicate_device_allow_records(self):
        gate = load_deploy_module("verify_matrix_ci_physical_install")
        original_run = gate.run
        gate.run = lambda *_argv: (
            "DevicePolicy=closed\n"
            "DeviceAllow=/dev/mem rw\n"
            "DeviceAllow=/dev/mem rw\n"
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "malformed systemctl show output"):
                gate.properties("matrix-web.service", "DevicePolicy", "DeviceAllow")
        finally:
            gate.run = original_run

    def test_install_gate_rejects_each_effective_privilege_boundary_widening(self):
        gate = load_deploy_module("verify_matrix_ci_physical_install")
        baseline = {
            "matrix-ci-physical.service": {
                "Type": "exec", "RuntimeMaxUSec": "90000000", "User": "root", "Group": "root",
                "CapabilityBoundingSet": "cap_dac_read_search cap_sys_rawio", "AmbientCapabilities": "",
                "ExecStart": "/usr/bin/python3 -I -B /usr/local/libexec/matrix-ci-physical-verifier",
                "ExecStop": "/usr/bin/python3 -I -B /usr/local/libexec/matrix-ci-physical-verifier --restore-only",
                "ExecStopPost": "/usr/bin/python3 -I -B /usr/local/libexec/matrix-ci-physical-verifier --restore-only --scavenge-stale-temporaries",
                "Requires": "systemd-tmpfiles-setup.service", "After": "matrix-web.service systemd-tmpfiles-setup.service",
                "ReadOnlyPaths": "/run/matrix-display", "ReadWritePaths": "/run/matrix-ci-physical",
                "DevicePolicy": "auto", "DeviceAllow": "", "UMask": "0077",
            },
            "matrix-ci-physical.path": {"Unit": "matrix-ci-physical.service", "Paths": "/run/matrix-ci-physical/claim", "ActiveState": "active"},
            "matrix-device-restart.path": {"Unit": "matrix-device-restart.service", "Paths": "/run/matrix-device-restart/submit/request", "ActiveState": "active"},
            "matrix-device-restart.service": {
                "Type": "oneshot", "ExecStart": "/usr/local/libexec/matrix-device-restart-broker",
                "User": "root", "Group": "root", "CapabilityBoundingSet": "cap_sys_boot",
                "AmbientCapabilities": "", "ReadWritePaths": "/run/matrix-device-restart",
                "DevicePolicy": "auto", "DeviceAllow": "",
            },
            "matrix-web.service": {
                "User": "root", "Group": "matrix-web",
                "InaccessiblePaths": (
                    "/run/systemd/private /run/dbus/system_bus_socket /run/matrix-ci-physical "
                    "/run/matrix-device-restart/state /etc/shadow /etc/gshadow /etc/ssh"
                ),
                "CapabilityBoundingSet": (
                    "cap_dac_override cap_fowner cap_setgid cap_setuid "
                    "cap_sys_nice cap_sys_rawio"
                ),
                "AmbientCapabilities": "",
                "Requires": "matrix-network-setup.service systemd-tmpfiles-setup.service",
                "After": "network-online.target matrix-network-setup.service systemd-tmpfiles-setup.service",
                "ReadOnlyPaths": "/run/matrix-display",
                "BindReadOnlyPaths": (
                    "/home/admin/matrix:/home/admin/matrix:rbind "
                    "/dev/null:/etc/shadow:rbind /dev/null:/etc/gshadow:rbind"
                ),
                "TemporaryFileSystem": "/etc/ssh:ro",
                "ProtectHome": "tmpfs", "ProtectProc": "invisible", "ProcSubset": "pid",
                "RestrictAddressFamilies": "AF_INET AF_INET6",
                "ReadWritePaths": "/home/admin/matrix/data /home/admin/matrix/resources/media /run/matrix-device-restart/submit",
                "DevicePolicy": "closed",
                "DeviceAllow": ["/dev/vcio rw", "/dev/gpiomem rw", "/dev/mem rw"],
                "UMask": "0077",
            },
        }
        attacks = (
            ("matrix-web.service", "User", "matrix-web"),
            ("matrix-web.service", "Group", "root"),
            ("matrix-web.service", "InaccessiblePaths", "/run/dbus/system_bus_socket /run/matrix-ci-physical /run/matrix-device-restart"),
            ("matrix-web.service", "InaccessiblePaths", "/run/systemd/private /run/matrix-ci-physical /run/matrix-device-restart"),
            (
                "matrix-web.service", "CapabilityBoundingSet",
                "CAP_DAC_OVERRIDE CAP_FOWNER CAP_SETGID CAP_SETUID "
                "CAP_SYS_NICE CAP_SYS_RAWIO CAP_SYS_ADMIN",
            ),
            (
                "matrix-web.service", "AmbientCapabilities",
                "CAP_SYS_ADMIN",
            ),
            ("matrix-web.service", "ReadWritePaths", baseline["matrix-web.service"]["ReadWritePaths"] + " /run/matrix-device-restart/state"),
            (
                "matrix-web.service", "BindReadOnlyPaths",
                "/home/admin/matrix:/home/admin/matrix:rbind /dev/null:/etc/gshadow:rbind",
            ),
            ("matrix-web.service", "TemporaryFileSystem", ""),
            ("matrix-web.service", "DeviceAllow", baseline["matrix-web.service"]["DeviceAllow"] + ["/dev/sda rw"]),
            ("matrix-ci-physical.service", "CapabilityBoundingSet", "CAP_SYS_RAWIO"),
            ("matrix-ci-physical.service", "CapabilityBoundingSet", "CAP_DAC_READ_SEARCH CAP_SYS_RAWIO CAP_SYS_ADMIN"),
            ("matrix-ci-physical.service", "AmbientCapabilities", "CAP_SYS_RAWIO"),
            ("matrix-device-restart.service", "CapabilityBoundingSet", "CAP_SYS_BOOT CAP_SYS_ADMIN"),
            ("matrix-device-restart.service", "AmbientCapabilities", "CAP_SYS_BOOT"),
        )
        original_run = gate.run
        original_verifiers = gate.verify_physical_runtime_install, gate.verify_display_lock_install, gate.verify_restart_broker_install
        gate.verify_physical_runtime_install = gate.verify_display_lock_install = gate.verify_restart_broker_install = lambda: None
        observed = {key: dict(values) for key, values in baseline.items()}

        def fake_run(*argv):
            if argv[1] == "cat":
                return "[Unit]\nDescription=reviewed\n"
            requested = [argv[index + 1] for index, item in enumerate(argv) if item == "-p"]
            values = observed[argv[2]]
            lines = []
            for key in requested:
                value = values[key]
                if key == "DeviceAllow" and values.get("DevicePolicy") == "auto" and value == "":
                    continue
                if key == "DeviceAllow" and isinstance(value, list):
                    lines.extend(f"{key}={item}\n" for item in value)
                else:
                    lines.append(f"{key}={value}\n")
            return "".join(lines)

        try:
            gate.run = fake_run
            gate.verify_units()
            for unit, name, value in attacks:
                observed = {key: dict(values) for key, values in baseline.items()}
                observed[unit][name] = value
                with self.subTest(unit=unit, property=name, value=value):
                    with self.assertRaises(RuntimeError):
                        gate.verify_units()
        finally:
            gate.run = original_run
            gate.verify_physical_runtime_install, gate.verify_display_lock_install, gate.verify_restart_broker_install = original_verifiers

    def test_restart_install_gate_rejects_altered_reviewed_broker_bytes(self):
        gate = load_deploy_module("verify_matrix_ci_physical_install")
        with tempfile.TemporaryDirectory(prefix="matrix-install-digest-", dir="/root") as directory:
            root = Path(directory)
            broker = root / "broker"
            service = root / "service"
            path_unit = root / "path"
            policy = root / "tmpfiles"
            request_directory = root / "requests"
            broker.write_bytes(b"altered broker bytes\n")
            service.write_bytes(b"reviewed service\n")
            path_unit.write_bytes(b"reviewed path\n")
            policy.write_bytes(gate.EXPECTED_RESTART_TMPFILES_POLICY)
            request_directory.mkdir(mode=0o700)
            for installed, mode in ((broker, 0o755), (service, 0o644), (path_unit, 0o644), (policy, 0o644)):
                os.chmod(installed, mode)
            old_paths = gate.RESTART_BROKER, gate.RESTART_SERVICE, gate.RESTART_PATH, gate.RESTART_TMPFILES_POLICY, gate.RESTART_DIRECTORY
            old_account = gate.pwd.getpwnam
            gate.RESTART_BROKER, gate.RESTART_SERVICE, gate.RESTART_PATH = broker, service, path_unit
            gate.RESTART_TMPFILES_POLICY, gate.RESTART_DIRECTORY = policy, request_directory
            gate.pwd.getpwnam = lambda _name: types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
            gate.EXPECTED_ARTIFACT_DIGESTS = {broker: hashlib.sha256(b"reviewed broker bytes\n").hexdigest()}
            try:
                with self.assertRaisesRegex(RuntimeError, "reviewed digest"):
                    gate.verify_restart_broker_install()
            finally:
                gate.RESTART_BROKER, gate.RESTART_SERVICE, gate.RESTART_PATH, gate.RESTART_TMPFILES_POLICY, gate.RESTART_DIRECTORY = old_paths
                gate.pwd.getpwnam = old_account

    def test_install_gate_binds_every_installed_artifact_to_operator_supplied_reviewed_digest(self):
        gate = load_deploy_module("verify_matrix_ci_physical_install")
        with tempfile.TemporaryDirectory(prefix="matrix-all-install-digests-", dir="/root") as directory:
            root = Path(directory)
            artifacts = {}
            expected = {}
            for index, name in enumerate(gate.REVIEWED_ARTIFACTS):
                path = root / f"artifact-{index}"
                payload = f"reviewed:{name}\n".encode("ascii")
                path.write_bytes(payload)
                mode = gate.REVIEWED_ARTIFACTS[name][1]
                os.chmod(path, mode)
                artifacts[name] = (path, mode)
                expected[name] = hashlib.sha256(payload).hexdigest()
            old_artifacts = gate.REVIEWED_ARTIFACTS
            gate.REVIEWED_ARTIFACTS = artifacts
            try:
                gate.verify_reviewed_artifacts(expected)
                for name, (path, mode) in artifacts.items():
                    with self.subTest(name=name):
                        reviewed = path.read_bytes()
                        path.write_bytes(reviewed + b"altered")
                        os.chmod(path, mode)
                        with self.assertRaisesRegex(RuntimeError, "reviewed digest"):
                            gate.verify_reviewed_artifacts(expected)
                        path.write_bytes(reviewed)
                        os.chmod(path, mode)
            finally:
                gate.REVIEWED_ARTIFACTS = old_artifacts

    def test_restart_broker_claims_submission_into_root_only_state_before_delay(self):
        broker = load_deploy_module("matrix_device_restart_broker")
        with tempfile.TemporaryDirectory(
            prefix="matrix-restart-split-", dir="/root"
        ) as directory:
            root = Path(directory)
            state = root / "state"
            submit = root / "submit"
            state.mkdir(mode=0o700)
            submit.mkdir(mode=0o1730)
            os.chmod(state, 0o700)
            os.chmod(submit, 0o1730)
            lock = state / broker.LOCK
            lock.write_bytes(b"")
            os.chmod(lock, 0o644)
            request = submit / broker.REQUEST
            request.write_bytes(b"")
            os.chmod(request, 0o600)
            account = types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
            old_paths = tuple(
                getattr(broker, name)
                for name in ("STATE_PARENT", "SUBMIT_PARENT", "PARENT")
            )
            old_account = broker.pwd.getpwnam
            setattr(broker, "STATE_PARENT", str(state))
            setattr(broker, "SUBMIT_PARENT", str(submit))
            setattr(broker, "PARENT", str(state))
            broker.pwd.getpwnam = lambda _name: account
            events = []

            def observe_delay(_seconds):
                self.assertFalse(request.exists())
                claimed = state / broker.REQUEST
                self.assertTrue(claimed.exists())
                self.assertEqual(
                    (claimed.stat().st_uid, claimed.stat().st_gid), (0, 0)
                )
                events.append("claimed")

            try:
                broker.run_broker(sleep=observe_delay, reboot=lambda: events.append("reboot"))
            finally:
                for name, value in zip(
                    ("STATE_PARENT", "SUBMIT_PARENT", "PARENT"), old_paths
                ):
                    setattr(broker, name, value)
                broker.pwd.getpwnam = old_account
            self.assertEqual(events, ["claimed", "reboot"])
            self.assertFalse((state / broker.REQUEST).exists())
            self.assertFalse(request.exists())

    def test_restart_tmpfiles_creates_sticky_group_admission_directory_and_root_lock(self):
        policy = (ROOT / "deploy" / "matrix-device-restart.conf").read_text(encoding="ascii")
        self.assertEqual(policy, (
            "# Host-owned fixed whole-device restart transaction namespaces on one mount.\n"
            "d /run/matrix-device-restart 0700 root root -\n"
            "d /run/matrix-device-restart/state 0700 root root -\n"
            "f /run/matrix-device-restart/state/transaction.lock 0644 root root -\n"
            "d /run/matrix-device-restart/submit 1730 root matrix-web -\n"
        ))

    def test_restart_terminal_recovery_documentation_is_inode_validated_and_narrow(self):
        guide = (ROOT / "deploy" / "matrix-ci-physical-install.md").read_text(encoding="utf-8")
        for terminal in ("request.failed", "request.cancelled", "request.invalid"):
            self.assertIn(terminal, guide)
        self.assertIn("Restart submissions are consumed without reboot", guide)
        self.assertIn("investigate", guide.lower())
        self.assertIn("O_DIRECTORY | os.O_NOFOLLOW", guide)
        self.assertIn("follow_symlinks=False", guide)
        self.assertIn("(before.st_dev, before.st_ino)", guide)
        self.assertIn("os.unlink(marker, dir_fd=directory_fd)", guide)
        self.assertIn("os.fsync(directory_fd)", guide)
        self.assertNotIn("rm -f /run/matrix-device-restart/request.*", guide)

    def test_restart_broker_exposes_no_terminal_publication_primitive_without_lock_enforcement(self):
        broker = load_deploy_module("matrix_device_restart_broker")
        self.assertFalse(hasattr(broker, "_quarantine_validated_request_locked"))
        self.assertFalse(hasattr(broker, "_remove_validated_request_locked"))
        with tempfile.TemporaryDirectory(prefix="matrix-restart-lock-contract-", dir="/root") as directory:
            parent = Path(directory)
            os.chmod(parent, 0o1730)
            lock = parent / broker.LOCK
            lock.write_bytes(b"")
            os.chmod(lock, 0o644)
            request = parent / broker.REQUEST
            request.write_bytes(b"")
            os.chmod(request, 0o600)
            parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            lock_fd = os.open(lock, os.O_RDONLY | os.O_CLOEXEC)
            try:
                identity = (request.stat().st_dev, request.stat().st_ino)
                with self.assertRaisesRegex(RuntimeError, "transaction lock context"):
                    broker._remove_validated_request_with_lock(parent_fd, lock_fd, identity)
                self.assertTrue(request.exists())
            finally:
                os.close(lock_fd)
                os.close(parent_fd)

    def test_restart_broker_rejects_closed_module_transaction_capability(self):
        broker = load_deploy_module("matrix_device_restart_broker")
        with tempfile.TemporaryDirectory(prefix="matrix-restart-closed-capability-", dir="/root") as directory:
            parent = Path(directory)
            os.chmod(parent, 0o1730)
            lock = parent / broker.LOCK
            lock.write_bytes(b"")
            os.chmod(lock, 0o644)
            request = parent / broker.REQUEST
            request.write_bytes(b"")
            os.chmod(request, 0o600)
            parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            transaction = broker._acquire_transaction_lock(parent_fd)
            transaction.close()
            try:
                identity = (request.stat().st_dev, request.stat().st_ino)
                with self.assertRaisesRegex(RuntimeError, "transaction lock context"):
                    broker._remove_validated_request_with_lock(parent_fd, transaction, identity)
                self.assertTrue(request.exists())
            finally:
                os.close(parent_fd)

    def test_restart_broker_rejects_parent_inode_replacement_for_transaction(self):
        broker = load_deploy_module("matrix_device_restart_broker")
        with tempfile.TemporaryDirectory(prefix="matrix-restart-parent-capability-", dir="/root") as directory:
            root = Path(directory)
            parent = root / "parent"
            replacement = root / "replacement"
            parent.mkdir()
            replacement.mkdir()
            lock = parent / broker.LOCK
            lock.write_bytes(b"")
            os.chmod(lock, 0o644)
            request = parent / broker.REQUEST
            request.write_bytes(b"")
            os.chmod(request, 0o600)
            parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            replacement_fd = os.open(replacement, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            transaction = broker._acquire_transaction_lock(parent_fd)
            try:
                os.dup2(replacement_fd, parent_fd)
                identity = (request.stat().st_dev, request.stat().st_ino)
                with self.assertRaisesRegex(RuntimeError, "transaction parent changed"):
                    broker._remove_validated_request_with_lock(parent_fd, transaction, identity)
                self.assertTrue(request.exists())
            finally:
                transaction.close()
                os.close(replacement_fd)
                os.close(parent_fd)

    def test_restart_broker_rejects_lock_inode_replacement_for_transaction(self):
        broker = load_deploy_module("matrix_device_restart_broker")
        with tempfile.TemporaryDirectory(prefix="matrix-restart-lock-capability-", dir="/root") as directory:
            parent = Path(directory)
            lock = parent / broker.LOCK
            lock.write_bytes(b"")
            os.chmod(lock, 0o644)
            request = parent / broker.REQUEST
            request.write_bytes(b"")
            os.chmod(request, 0o600)
            parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            transaction = broker._acquire_transaction_lock(parent_fd)
            try:
                lock.unlink()
                lock.write_bytes(b"")
                os.chmod(lock, 0o644)
                identity = (request.stat().st_dev, request.stat().st_ino)
                with self.assertRaisesRegex(RuntimeError, "transaction lock"):
                    broker._remove_validated_request_with_lock(parent_fd, transaction, identity)
                self.assertTrue(request.exists())
            finally:
                transaction.close()
                os.close(parent_fd)

    def test_restart_broker_locked_helper_makes_no_nested_flock_call(self):
        broker = load_deploy_module("matrix_device_restart_broker")
        with tempfile.TemporaryDirectory(prefix="matrix-restart-no-reentrant-flock-", dir="/root") as directory:
            parent = Path(directory)
            lock = parent / broker.LOCK
            lock.write_bytes(b"")
            os.chmod(lock, 0o644)
            request = parent / broker.REQUEST
            request.write_bytes(b"")
            os.chmod(request, 0o600)
            parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            transaction = broker._acquire_transaction_lock(parent_fd)
            try:
                identity = (request.stat().st_dev, request.stat().st_ino)
                with mock.patch.object(
                    broker.fcntl,
                    "flock",
                    side_effect=AssertionError("locked helper attempted nested flock"),
                ) as nested_flock:
                    broker._remove_validated_request_with_lock(parent_fd, transaction, identity)
                nested_flock.assert_not_called()
                self.assertFalse(request.exists())
            finally:
                transaction.close()
                os.close(parent_fd)

    def test_restart_broker_refuses_symlink_in_runtime_ancestry(self):
        broker = load_deploy_module("matrix_device_restart_broker")
        with tempfile.TemporaryDirectory(prefix="matrix-restart-broker-ancestry-", dir="/root") as directory:
            root = Path(directory)
            trusted = root / "trusted"
            trusted.mkdir()
            parent = trusted / "requests"
            parent.mkdir()
            os.chmod(parent, 0o1730)
            lock = parent / "transaction.lock"
            lock.write_bytes(b"")
            os.chmod(lock, 0o644)
            request = parent / broker.REQUEST
            request.write_bytes(b"")
            os.chmod(request, 0o600)
            alias = root / "alias"
            alias.symlink_to(trusted, target_is_directory=True)
            old_parent, old_account = broker.PARENT, broker.pwd.getpwnam
            broker.PARENT = str(alias / "requests")
            broker.pwd.getpwnam = lambda _name: types.SimpleNamespace(
                pw_uid=os.getuid(), pw_gid=os.getgid(),
            )
            events = []
            try:
                with self.assertRaises((OSError, RuntimeError)):
                    broker.run_broker(
                        sleep=lambda _seconds: events.append("sleep"),
                        reboot=lambda: events.append("reboot"),
                    )
            finally:
                broker.PARENT, broker.pwd.getpwnam = old_parent, old_account
            self.assertEqual(events, [])
            self.assertTrue(request.exists())

    def test_restart_broker_refuses_admission_when_terminal_state_exists(self):
        broker = load_deploy_module("matrix_device_restart_broker")
        with tempfile.TemporaryDirectory(prefix="matrix-restart-terminal-admission-", dir="/root") as directory:
            parent = Path(directory)
            os.chmod(parent, 0o1730)
            lock = parent / "transaction.lock"
            lock.write_bytes(b"")
            os.chmod(lock, 0o644)
            request = parent / broker.REQUEST
            request.write_bytes(b"")
            os.chmod(request, 0o600)
            terminal = parent / "request.failed"
            terminal.write_bytes(b"")
            os.chmod(terminal, 0o600)
            events = []
            old_parent = broker.PARENT
            old_account = broker.pwd.getpwnam
            broker.PARENT = str(parent)
            broker.pwd.getpwnam = lambda _name: types.SimpleNamespace(
                pw_uid=os.getuid(), pw_gid=os.getgid(),
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "manual recovery"):
                    broker.run_broker(
                        sleep=lambda _seconds: events.append("sleep"),
                        reboot=lambda: events.append("reboot"),
                    )
            finally:
                broker.PARENT = old_parent
                broker.pwd.getpwnam = old_account

            self.assertEqual(events, [])
            self.assertFalse(request.exists(), "terminal admission left path activation live")
            self.assertTrue(terminal.exists())

    def test_restart_broker_rechecks_all_terminal_variants_before_reboot(self):
        broker = load_deploy_module("matrix_device_restart_broker")
        for terminal_name in broker.TERMINALS:
            with self.subTest(terminal=terminal_name):
                with tempfile.TemporaryDirectory(prefix="matrix-restart-pre-reboot-", dir="/root") as directory:
                    parent = Path(directory)
                    os.chmod(parent, 0o1730)
                    lock = parent / "transaction.lock"
                    lock.write_bytes(b"")
                    os.chmod(lock, 0o644)
                    request = parent / broker.REQUEST
                    request.write_bytes(b"")
                    os.chmod(request, 0o600)
                    terminal = parent / f"{broker.REQUEST}.{terminal_name}"
                    old_parent, old_account = broker.PARENT, broker.pwd.getpwnam
                    broker.PARENT = str(parent)
                    broker.pwd.getpwnam = lambda _name: types.SimpleNamespace(
                        pw_uid=os.getuid(), pw_gid=os.getgid(),
                    )
                    events = []

                    def publish_during_delay(_seconds):
                        terminal.write_bytes(b"")
                        os.chmod(terminal, 0o600)
                        events.append("sleep")

                    try:
                        with self.assertRaisesRegex(RuntimeError, "manual recovery"):
                            broker.run_broker(
                                sleep=publish_during_delay,
                                reboot=lambda: events.append("reboot"),
                            )
                    finally:
                        broker.PARENT, broker.pwd.getpwnam = old_parent, old_account
                    self.assertEqual(events, ["sleep"])
                    self.assertTrue(terminal.exists())
                    self.assertFalse(request.exists())
                    self.assertFalse(any(
                        (parent / f"{broker.REQUEST}.{other}").exists()
                        for other in broker.TERMINALS if other != terminal_name
                    ))

    def test_restart_broker_claims_request_as_root_before_releasing_transaction_lock(self):
        broker = load_deploy_module("matrix_device_restart_broker")
        account = broker.pwd.getpwnam("nobody")
        with tempfile.TemporaryDirectory(prefix="matrix-restart-claim-", dir="/root") as directory:
            parent = Path(directory)
            os.chown(parent, 0, account.pw_gid)
            os.chmod(parent, 0o1730)
            lock = parent / "transaction.lock"
            lock.write_bytes(b"")
            os.chmod(lock, 0o644)
            request = parent / broker.REQUEST
            request.write_bytes(b"")
            os.chown(request, account.pw_uid, account.pw_gid)
            os.chmod(request, 0o600)
            old_parent, old_account = broker.PARENT, broker.pwd.getpwnam
            broker.PARENT = str(parent)
            broker.pwd.getpwnam = lambda _name: account
            try:
                broker.run_broker(
                    sleep=lambda _seconds: self.assertEqual(
                        (request.stat().st_uid, request.stat().st_gid), (0, 0)
                    ),
                    reboot=lambda: None,
                )
            finally:
                broker.PARENT, broker.pwd.getpwnam = old_parent, old_account
            self.assertFalse(request.exists())

    def test_restart_broker_retains_validated_marker_through_delay_and_reboot_then_cleans_success(self):
        broker = load_deploy_module("matrix_device_restart_broker")
        with tempfile.TemporaryDirectory(prefix="matrix-restart-broker-", dir="/root") as directory:
            parent = Path(directory)
            os.chmod(parent, 0o1730)
            lock = parent / "transaction.lock"
            lock.write_bytes(b"")
            os.chmod(lock, 0o644)
            request = parent / broker.REQUEST
            request.write_bytes(b"")
            os.chmod(request, 0o600)
            events = []
            old_parent = broker.PARENT
            old_account = broker.pwd.getpwnam
            old_run = broker.subprocess.run
            old_geteuid = broker.os.geteuid
            broker.PARENT = str(parent)
            broker.pwd.getpwnam = lambda _name: types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
            broker.os.geteuid = lambda: 0

            def fake_sleep(seconds):
                self.assertEqual(seconds, broker.DELAY_SECONDS)
                self.assertTrue(request.exists(), "durable admission vanished during delay")
                app = __import__("webapp")
                self.assertIs(
                    app.RestartRequest(request)(), app.RestartRequestOutcome.ACTIVE_REQUEST,
                    "duplicate request did not coalesce",
                )
                events.append("sleep")

            def fake_reboot(*_args, **_kwargs):
                self.assertTrue(request.exists(), "durable admission vanished before reboot")
                events.append("reboot")
                return types.SimpleNamespace(returncode=0)

            broker.subprocess.run = lambda *_args, **_kwargs: self.fail("real subprocess boundary reached")
            try:
                broker.run_broker(sleep=fake_sleep, reboot=fake_reboot)
            finally:
                broker.PARENT = old_parent
                broker.pwd.getpwnam = old_account
                broker.subprocess.run = old_run
                broker.os.geteuid = old_geteuid
            self.assertEqual(events, ["sleep", "reboot"])
            self.assertFalse(request.exists())

    def test_restart_broker_quarantines_failed_timeout_and_cancelled_admissions(self):
        broker = load_deploy_module("matrix_device_restart_broker")
        outcomes = (
            (subprocess.CalledProcessError(1, ["fake-reboot"]), "failed"),
            (subprocess.TimeoutExpired(["fake-reboot"], 10), "failed"),
            (KeyboardInterrupt(), "cancelled"),
        )
        for error, terminal in outcomes:
            with self.subTest(error=type(error).__name__):
                with tempfile.TemporaryDirectory(prefix="matrix-restart-terminal-", dir="/root") as directory:
                    parent = Path(directory)
                    os.chmod(parent, 0o1730)
                    lock = parent / "transaction.lock"
                    lock.write_bytes(b"")
                    os.chmod(lock, 0o644)
                    request = parent / broker.REQUEST
                    request.write_bytes(b"")
                    os.chmod(request, 0o600)
                    old_parent, old_account = broker.PARENT, broker.pwd.getpwnam
                    old_run, old_geteuid = broker.subprocess.run, broker.os.geteuid
                    broker.PARENT = str(parent)
                    broker.pwd.getpwnam = lambda _name: types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
                    broker.os.geteuid = lambda: 0
                    broker.subprocess.run = lambda *_args, **_kwargs: self.fail("real subprocess boundary reached")
                    try:
                        with self.assertRaises(type(error)):
                            broker.run_broker(
                                sleep=lambda _seconds: None,
                                reboot=lambda: (_ for _ in ()).throw(error),
                            )
                    finally:
                        broker.PARENT, broker.pwd.getpwnam = old_parent, old_account
                        broker.subprocess.run, broker.os.geteuid = old_run, old_geteuid
                    self.assertFalse(request.exists(), "path activation would immediately retrigger")
                    terminal_marker = parent / f"{broker.REQUEST}.{terminal}"
                    self.assertTrue(terminal_marker.exists(), "manual recovery state was not retained")
                    self.assertEqual(terminal_marker.stat().st_size, 0)

    def test_restart_broker_invalid_terminal_is_root_owned_before_publication(self):
        broker = load_deploy_module("matrix_device_restart_broker")
        account = broker.pwd.getpwnam("nobody")
        with tempfile.TemporaryDirectory(prefix="matrix-restart-invalid-owner-", dir="/root") as directory:
            parent = Path(directory)
            os.chown(parent, 0, account.pw_gid)
            os.chmod(parent, 0o1730)
            lock = parent / "transaction.lock"
            lock.write_bytes(b"")
            os.chmod(lock, 0o644)
            request = parent / broker.REQUEST
            request.write_bytes(b"invalid")
            os.chown(request, account.pw_uid, account.pw_gid)
            os.chmod(request, 0o600)
            old_parent, old_account = broker.PARENT, broker.pwd.getpwnam
            broker.PARENT = str(parent)
            broker.pwd.getpwnam = lambda _name: account
            try:
                with self.assertRaisesRegex(RuntimeError, "unsafe fixed restart request marker"):
                    broker.run_broker(
                        sleep=lambda _seconds: self.fail("invalid request reached sleep"),
                        reboot=lambda: self.fail("invalid request reached reboot"),
                    )
            finally:
                broker.PARENT, broker.pwd.getpwnam = old_parent, old_account
            terminal = parent / "request.invalid"
            self.assertEqual((terminal.stat().st_uid, terminal.stat().st_gid), (0, 0))
            self.assertEqual(stat.S_IMODE(terminal.stat().st_mode), 0o600)
            self.assertEqual(terminal.stat().st_nlink, 1)
            self.assertEqual(terminal.read_bytes(), b"")

    def test_restart_broker_quarantines_invalid_marker_without_sleep_or_reboot(self):
        broker = load_deploy_module("matrix_device_restart_broker")
        with tempfile.TemporaryDirectory(prefix="matrix-restart-invalid-", dir="/root") as directory:
            parent = Path(directory)
            os.chmod(parent, 0o1730)
            lock = parent / "transaction.lock"
            lock.write_bytes(b"")
            os.chmod(lock, 0o644)
            request = parent / broker.REQUEST
            request.write_bytes(b"not-empty")
            os.chmod(request, 0o600)
            old_parent, old_account = broker.PARENT, broker.pwd.getpwnam
            broker.PARENT = str(parent)
            broker.pwd.getpwnam = lambda _name: types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
            try:
                with self.assertRaisesRegex(RuntimeError, "unsafe fixed restart request marker"):
                    broker.run_broker(
                        sleep=lambda _seconds: self.fail("invalid marker reached delay"),
                        reboot=lambda: self.fail("invalid marker reached reboot"),
                    )
            finally:
                broker.PARENT, broker.pwd.getpwnam = old_parent, old_account
            self.assertFalse(request.exists(), "invalid path activation would immediately retrigger")
            invalid = parent / f"{broker.REQUEST}.invalid"
            self.assertTrue(invalid.exists())
            self.assertEqual(invalid.read_bytes(), b"")

    def test_restart_broker_blocks_split_submission_while_manual_terminal_exists(self):
        app = __import__("webapp")
        broker = load_deploy_module("matrix_device_restart_broker")
        account = types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
        for terminal in ("failed", "cancelled", "invalid"):
            with self.subTest(terminal=terminal):
                with tempfile.TemporaryDirectory(prefix="matrix-restart-manual-", dir="/root") as directory:
                    root = Path(directory) / "matrix-device-restart"
                    state = root / "state"
                    submit = root / "submit"
                    root.mkdir(mode=0o700)
                    state.mkdir(mode=0o700)
                    submit.mkdir(mode=0o1730)
                    os.chmod(submit, 0o1730)
                    lock = state / broker.LOCK
                    lock.write_bytes(b"")
                    os.chmod(lock, 0o644)
                    terminal_path = state / f"{broker.REQUEST}.{terminal}"
                    terminal_path.write_bytes(b"")
                    os.chmod(terminal_path, 0o600)
                    request = submit / broker.REQUEST
                    self.assertIs(
                        app.RestartRequest(request)(),
                        app.RestartRequestOutcome.CREATED,
                    )
                    old_paths = broker.STATE_PARENT, broker.SUBMIT_PARENT, broker.PARENT
                    old_getpwnam = broker.pwd.getpwnam
                    setattr(broker, "STATE_PARENT", str(state))
                    setattr(broker, "SUBMIT_PARENT", str(submit))
                    setattr(broker, "PARENT", broker.STATE_PARENT)
                    broker.pwd.getpwnam = lambda _name: account
                    try:
                        with self.assertRaisesRegex(
                            broker.ManualRecoveryError, "restart requires manual recovery"
                        ):
                            broker.run_broker(
                                sleep=lambda _seconds: self.fail("terminal reached delay"),
                                reboot=lambda: self.fail("terminal reached reboot"),
                            )
                    finally:
                        for name, value in zip(
                            ("STATE_PARENT", "SUBMIT_PARENT", "PARENT"), old_paths
                        ):
                            setattr(broker, name, value)
                        broker.pwd.getpwnam = old_getpwnam
                    self.assertFalse(request.exists())
                    self.assertTrue(terminal_path.exists())

    def test_effective_sshd_policy_rejects_certificate_principal_and_hostbased_sources(self):
        gate = load_deploy_module("verify_matrix_ci_physical_install")
        safe = (
            "authorizedkeysfile .ssh/authorized_keys\n"
            "authorizedkeyscommand none\n"
            "authorizedkeyscommanduser none\n"
            "permitrootlogin forced-commands-only\n"
            "trustedusercakeys none\n"
            "authorizedprincipalsfile none\n"
            "authorizedprincipalscommand none\n"
            "authorizedprincipalscommanduser none\n"
            "hostbasedauthentication no\n"
            "pubkeyauthentication yes\n"
            "pubkeyauthoptions none\n"
            "ignorerhosts yes\n"
        )
        attacks = (
            safe.replace("trustedusercakeys none", "trustedusercakeys /etc/ssh/ci_user_ca.pub"),
            safe.replace("authorizedprincipalsfile none", "authorizedprincipalsfile /etc/ssh/principals/%u"),
            safe.replace("authorizedprincipalscommand none", "authorizedprincipalscommand /bin/true"),
            safe.replace("hostbasedauthentication no", "hostbasedauthentication yes"),
            safe.replace("pubkeyauthoptions none", "pubkeyauthoptions verify-required"),
        )
        for output in attacks:
            with self.subTest(output=output):
                with self.assertRaises(RuntimeError):
                    gate._parse_effective_sshd_policy(output)

    def test_effective_sshd_policy_is_uniquely_bound_to_reviewed_key_source(self):
        gate = load_deploy_module("verify_matrix_ci_physical_install")
        safe = (
            "authorizedkeysfile .ssh/authorized_keys\n"
            "authorizedkeyscommand none\n"
            "authorizedkeyscommanduser nobody\n"
            "permitrootlogin forced-commands-only\n"
            "trustedusercakeys none\n"
            "authorizedprincipalsfile none\n"
            "authorizedprincipalscommand none\n"
            "authorizedprincipalscommanduser nobody\n"
            "hostbasedauthentication no\n"
            "pubkeyauthentication yes\n"
            "pubkeyauthoptions none\n"
            "ignorerhosts yes\n"
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
            safe.replace("authorizedkeyscommanduser nobody", "authorizedkeyscommanduser %u"),
            safe.replace("authorizedprincipalscommanduser nobody", "authorizedprincipalscommanduser /bin/root"),
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
