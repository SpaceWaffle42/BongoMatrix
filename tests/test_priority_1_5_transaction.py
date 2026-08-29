import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class _Service:
    def __init__(self):
        self.active = True
        self.events = []
        self.ready_hook = lambda: None

    def is_active(self):
        return self.active

    def stop(self):
        self.events.append("stop")
        self.active = False

    def start_ready(self):
        self.events.append("start-ready")
        self.active = True
        self.ready_hook()

    def status(self, *, warning_since=None):
        return {
            "service": "matrix-web.service", "active": self.active,
            "substate": "running", "http_status": 302, "main_pid": 1234,
            "n_restarts": 0, "control_group": "/system.slice/matrix-web.service",
            "display_lock": {
                "path": "/run/matrix-display/matrix-display.lock", "dev": 1,
                "ino": 2, "uid": 0, "gid": 0, "mode": "0644", "locked": True,
            },
            "warning_count": 0,
        }


class Priority15TransactionTests(unittest.TestCase):
    def _git(self, root, *args):
        return subprocess.check_output(
            ["/usr/bin/git", "-C", str(root), *args], text=True,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
        ).strip()

    def _build_prepare_fixture(self, transaction, root):
        repository = root / "repository"
        repository.mkdir()
        subprocess.run(["/usr/bin/git", "init", "-q", str(repository)], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(repository), "config", "user.name", "test"], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(repository), "config", "user.email", "test@example.invalid"], check=True)
        (repository / "app.py").write_bytes(b"baseline\n")
        (repository / "pkg").mkdir()
        (repository / "pkg/module.py").write_bytes(b"baseline module\n")
        subprocess.run(["/usr/bin/git", "-C", str(repository), "add", "app.py", "pkg/module.py"], check=True)
        dates = {**os.environ, "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000",
                 "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000"}
        subprocess.run(["/usr/bin/git", "-C", str(repository), "commit", "-qm", "baseline"],
                       check=True, env=dates)
        baseline = self._git(repository, "rev-parse", "HEAD^{commit}")
        baseline_tree = self._git(repository, "rev-parse", "HEAD^{tree}")
        (repository / "app.py").write_bytes(b"candidate\n")
        (repository / "pkg/module.py").write_bytes(b"candidate module\n")
        (repository / "candidate-only.py").write_bytes(b"candidate only\n")
        subprocess.run([
            "/usr/bin/git", "-C", str(repository), "add", "candidate-only.py",
        ], check=True)
        candidate_dates = {**dates, "GIT_AUTHOR_DATE": "2000-01-01T00:00:01+0000",
                           "GIT_COMMITTER_DATE": "2000-01-01T00:00:01+0000"}
        subprocess.run(["/usr/bin/git", "-C", str(repository), "commit", "-qam", "candidate"],
                       check=True, env=candidate_dates)
        source = self._git(repository, "rev-parse", "HEAD^{commit}")
        tree = self._git(repository, "rev-parse", "HEAD^{tree}")
        diff = subprocess.check_output([
            "/usr/bin/git", "-C", str(repository), "diff", "--binary", "--full-index",
            "--no-ext-diff", f"{baseline}...{source}",
        ])
        stage = root / "stage"
        build = types.SimpleNamespace(
            repository=str(repository), baseline_commit=baseline, source_commit=source,
            expected_reviewed_diff_sha256=hashlib.sha256(diff).hexdigest(),
            stage_root=str(stage), python=sys.executable,
        )
        transaction.build_b1(build)
        return repository, stage, baseline, baseline_tree, source, tree

    def _prepare_arguments(self, root, repository, stage, baseline, baseline_tree,
                           source, tree, name):
        destination = root / f"destination-{name}"
        subprocess.run(["/usr/bin/git", "clone", "-q", "--no-hardlinks",
                        str(repository), str(destination)], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(destination), "checkout", "-q", baseline], check=True)
        private = {}
        for relative, value in (("data/config.json", b"config-private"),
                                ("data/paired-devices.json", b"paired-private"),
                                ("resources/media/private.bin", b"media-private")):
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
            private[relative] = value
        backup = root / f"backup-{name}"
        state = root / f"authority-{name}/state.json"
        arguments = types.SimpleNamespace(
            release_root=str(stage / "release"),
            release_manifest=str(stage / "release-manifest.json"),
            fixture_manifest=str(stage / "physical-fixtures.json"),
            expected_baseline_source_sha=baseline,
            expected_baseline_tree_sha=baseline_tree,
            expected_source_sha=source, expected_tree_sha=tree,
            destination=str(destination), backup_root=str(backup),
            service="matrix-web.service", interpreter=sys.executable, state=str(state),
        )
        return arguments, destination, backup, state, private

    def _fork_prepare(self, transaction, arguments, populate=None, start_fd=None):
        pid = os.fork()
        if pid == 0:
            try:
                if start_fd is not None:
                    os.read(start_fd, 1)
                private_names = {"config.json", "paired-devices.json", "private.bin"}
                real_open = transaction.os.open
                def reject_private_open(path, *args, **kwargs):
                    if Path(os.fsdecode(path)).name in private_names:
                        os._exit(97)
                    return real_open(path, *args, **kwargs)
                patches = [
                    patch.object(transaction, "SystemdService", return_value=_Service()),
                    patch.object(transaction.os, "open", side_effect=reject_private_open),
                ]
                if populate is not None:
                    patches.append(patch.object(transaction, "_populate_state_fd", side_effect=populate))
                for active in patches:
                    active.start()
                try:
                    transaction.prepare_b1(arguments)
                finally:
                    for active in reversed(patches):
                        active.stop()
            except BaseException:
                os._exit(98)
            os._exit(0)
        return pid

    def test_candidate_only_rollback_and_recovery_preserve_unknown_regular_files(self):
        from deploy import priority_1_5_transaction as transaction

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self._build_prepare_fixture(transaction, root)

            for operation in ("rollback", "recover"):
                for mutation in ("replacement", "content", "mode"):
                    with self.subTest(operation=operation, mutation=mutation):
                        name = f"unknown-{operation}-{mutation}"
                        arguments, destination, _backup, state, _private = self._prepare_arguments(
                            root, *fixture, name,
                        )
                        service = _Service()
                        with patch.object(transaction, "SystemdService", return_value=service):
                            transaction.prepare_b1(arguments)
                        tx = transaction.Priority15Transaction.load(state, service=service)
                        candidate_only = destination / "candidate-only.py"
                        try:
                            tx.transition("candidate", operation="deploy")
                            installed = candidate_only.stat()
                            installed_inode = (installed.st_dev, installed.st_ino)
                            if mutation == "replacement":
                                substitute = destination / "unknown.tmp"
                                substitute.write_bytes(b"unknown replacement bytes\n")
                                substitute.chmod(stat.S_IMODE(installed.st_mode))
                                os.replace(substitute, candidate_only)
                                self.assertNotEqual(
                                    (candidate_only.stat().st_dev, candidate_only.stat().st_ino),
                                    installed_inode,
                                )
                            elif mutation == "content":
                                candidate_only.write_bytes(b"unknown in-place bytes\n")
                                self.assertEqual(
                                    (candidate_only.stat().st_dev, candidate_only.stat().st_ino),
                                    installed_inode,
                                )
                            else:
                                candidate_only.chmod(0o600)
                                self.assertEqual(
                                    (candidate_only.stat().st_dev, candidate_only.stat().st_ino),
                                    installed_inode,
                                )

                            unknown = (
                                candidate_only.stat().st_dev,
                                candidate_only.stat().st_ino,
                                candidate_only.read_bytes(),
                                stat.S_IMODE(candidate_only.stat().st_mode),
                            )
                            def action():
                                if operation == "rollback":
                                    tx.transition("baseline", operation="rollback")
                                else:
                                    tx.recover("baseline")
                            with self.assertRaisesRegex(ValueError, "rollback unlink target"):
                                action()
                            self.assertFalse(service.active)
                            self.assertEqual(
                                (
                                    candidate_only.stat().st_dev,
                                    candidate_only.stat().st_ino,
                                    candidate_only.read_bytes(),
                                    stat.S_IMODE(candidate_only.stat().st_mode),
                                ),
                                unknown,
                            )
                        finally:
                            tx.close()

    def test_candidate_only_exact_candidate_rollback_and_crash_recovery_converge(self):
        from deploy import priority_1_5_transaction as transaction

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self._build_prepare_fixture(transaction, root)
            for operation in ("rollback", "recover-after-crash"):
                with self.subTest(operation=operation):
                    arguments, destination, _backup, state, private = self._prepare_arguments(
                        root, *fixture, operation,
                    )
                    service = _Service()
                    with patch.object(transaction, "SystemdService", return_value=service):
                        transaction.prepare_b1(arguments)
                    tx = transaction.Priority15Transaction.load(state, service=service)
                    candidate_only = destination / "candidate-only.py"
                    real_open = transaction.os.open
                    private_names = {"config.json", "paired-devices.json", "private.bin"}
                    def reject_private_open(path, *args, **kwargs):
                        if Path(os.fsdecode(path)).name in private_names:
                            raise AssertionError("transaction opened a private runtime path")
                        return real_open(path, *args, **kwargs)
                    try:
                        with patch.object(transaction.os, "open", side_effect=reject_private_open):
                            tx.transition("candidate", operation="deploy")
                        if operation == "rollback":
                            original_inode = candidate_only.stat().st_ino
                            approved_replacement = destination / "approved.tmp"
                            approved_replacement.write_bytes(b"candidate only\n")
                            approved_replacement.chmod(0o644)
                            os.replace(approved_replacement, candidate_only)
                            self.assertNotEqual(candidate_only.stat().st_ino, original_inode)
                            with patch.object(transaction.os, "open", side_effect=reject_private_open):
                                tx.transition("baseline", operation="rollback")
                        else:
                            crashed = False
                            def interrupt(point):
                                nonlocal crashed
                                if point.endswith(":published") and not crashed:
                                    crashed = True
                                    raise transaction.InjectedCrash(point)
                            with (
                                patch.object(transaction.os, "open", side_effect=reject_private_open),
                                self.assertRaises(transaction.InjectedCrash),
                            ):
                                tx.transition(
                                    "baseline", operation="rollback", after_boundary=interrupt,
                                )
                            self.assertFalse(service.active)
                            tx.close()
                            tx = transaction.Priority15Transaction.load(state, service=service)
                            with patch.object(transaction.os, "open", side_effect=reject_private_open):
                                tx.recover("baseline")
                        self.assertFalse(candidate_only.exists())
                        self.assertEqual((destination / "app.py").read_bytes(), b"baseline\n")
                        self.assertEqual(
                            (destination / "pkg/module.py").read_bytes(), b"baseline module\n",
                        )
                        self.assertTrue(service.active)
                        for relative, expected in private.items():
                            self.assertEqual((destination / relative).read_bytes(), expected)
                    finally:
                        tx.close()

    def test_candidate_only_rollback_preserves_unsupported_objects(self):
        from deploy import priority_1_5_transaction as transaction

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self._build_prepare_fixture(transaction, root)
            for object_type in ("symlink", "fifo", "device"):
                with self.subTest(object_type=object_type):
                    arguments, destination, _backup, state, _private = self._prepare_arguments(
                        root, *fixture, f"unsafe-{object_type}",
                    )
                    service = _Service()
                    with patch.object(transaction, "SystemdService", return_value=service):
                        transaction.prepare_b1(arguments)
                    tx = transaction.Priority15Transaction.load(state, service=service)
                    candidate_only = destination / "candidate-only.py"
                    try:
                        tx.transition("candidate", operation="deploy")
                        candidate_only.unlink()
                        sentinel = None
                        if object_type == "symlink":
                            sentinel = destination / "symlink-sentinel"
                            sentinel.write_bytes(b"unknown symlink target\n")
                            candidate_only.symlink_to(sentinel)
                        elif object_type == "fifo":
                            os.mkfifo(candidate_only, 0o600)
                        else:
                            os.mknod(candidate_only, stat.S_IFCHR | 0o600, os.makedev(1, 3))
                        before = candidate_only.lstat()
                        identity = (
                            before.st_dev, before.st_ino, before.st_mode, before.st_rdev,
                        )
                        with self.assertRaisesRegex(ValueError, "rollback unlink target"):
                            tx.transition("baseline", operation="rollback")
                        self.assertFalse(service.active)
                        after = candidate_only.lstat()
                        self.assertEqual(
                            (after.st_dev, after.st_ino, after.st_mode, after.st_rdev), identity,
                        )
                        if object_type == "symlink":
                            if sentinel is None:
                                raise AssertionError("symlink sentinel was not created")
                            self.assertEqual(sentinel.read_bytes(), b"unknown symlink target\n")
                    finally:
                        tx.close()

    def test_prepare_publication_converges_after_real_process_crashes(self):
        from deploy import priority_1_5_transaction as transaction

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self._build_prepare_fixture(transaction, root)
            real_populate = transaction._populate_state_fd

            def partial_state(_path, _state, descriptor):
                os.write(descriptor, b"partial")
                os.fsync(descriptor)
                os._exit(71)

            def after_state_fsync(path, state_value, descriptor):
                real_populate(path, state_value, descriptor)
                os._exit(72)

            def after_backup_before_state(_path, _state, _descriptor):
                os._exit(73)

            for name, crash, expected_exit in (
                ("partial-state", partial_state, 71),
                ("state-fsynced", after_state_fsync, 72),
                ("backup-published", after_backup_before_state, 73),
            ):
                with self.subTest(crash=name):
                    arguments, destination, backup, state, private = self._prepare_arguments(
                        root, *fixture, name,
                    )
                    pid = self._fork_prepare(transaction, arguments, crash)
                    waited, status = os.waitpid(pid, 0)
                    self.assertEqual(waited, pid)
                    self.assertTrue(os.WIFEXITED(status))
                    self.assertEqual(os.WEXITSTATUS(status), expected_exit)
                    self.assertFalse(state.exists(), "a pre-publication state became visible")
                    self.assertFalse(Path(str(state) + ".identity").exists())

                    service = _Service()
                    with patch.object(transaction, "SystemdService", return_value=service):
                        prepared = transaction.prepare_b1(arguments)
                    self.assertTrue(state.is_file())
                    self.assertEqual(state.read_bytes(), transaction.canonical_json(prepared))
                    tx = transaction.Priority15Transaction.load(state, service=service)
                    tx.close()
                    baseline_manifest = json.loads(
                        (backup / "baseline-source-manifest.json").read_bytes()
                    )
                    self.assertEqual(
                        [item["sha256"] for item in baseline_manifest["files"]],
                        [hashlib.sha256(b"baseline\n").hexdigest(), None,
                         hashlib.sha256(b"baseline module\n").hexdigest()],
                    )
                    for relative, expected in private.items():
                        self.assertEqual((destination / relative).read_bytes(), expected)

    def test_concurrent_prepare_never_overwrites_state_or_backup(self):
        from deploy import priority_1_5_transaction as transaction

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self._build_prepare_fixture(transaction, root)
            arguments, _destination, backup, state, _private = self._prepare_arguments(
                root, *fixture, "concurrent",
            )
            read_end, write_end = os.pipe()
            try:
                children = [self._fork_prepare(transaction, arguments, start_fd=read_end)
                            for _ in range(2)]
                os.write(write_end, b"xx")
                results = []
                for child in children:
                    _waited, status = os.waitpid(child, 0)
                    results.append(os.WEXITSTATUS(status))
            finally:
                os.close(read_end)
                os.close(write_end)
            self.assertEqual(sorted(results), [0, 98])
            before = (state.stat().st_dev, state.stat().st_ino, state.read_bytes())
            tx = transaction.Priority15Transaction.load(state, service=_Service())
            tx.close()
            self.assertEqual(
                (state.stat().st_dev, state.stat().st_ino, state.read_bytes()), before,
            )
            self.assertTrue((backup / "baseline-source-manifest.json").is_file())
            self.assertEqual(list(backup.parent.glob(f".{backup.name}.*")), [])
            self.assertEqual(list(state.parent.glob(f".{state.name}.*")), [])

    def test_orphan_backup_root_substitution_during_state_construction_fails_closed(self):
        from deploy import priority_1_5_transaction as transaction

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self._build_prepare_fixture(transaction, root)
            arguments, _destination, backup, state, _private = self._prepare_arguments(
                root, *fixture, "adopt-root-swap",
            )

            def leave_orphan(_path, _state, _descriptor):
                os._exit(73)

            child = self._fork_prepare(transaction, arguments, leave_orphan)
            _waited, status = os.waitpid(child, 0)
            self.assertEqual(os.WEXITSTATUS(status), 73)
            self.assertTrue(backup.is_dir())
            self.assertFalse(state.exists())

            admitted = backup.with_name("admitted-backup")
            real_populate = transaction._populate_state_fd

            def substitute(path, state_value, descriptor):
                real_populate(path, state_value, descriptor)
                backup.rename(admitted)
                backup.mkdir(mode=0o700)
                (backup / "unknown-sentinel").write_bytes(b"substitute")

            with (
                patch.object(transaction, "SystemdService", return_value=_Service()),
                patch.object(transaction, "_populate_state_fd", side_effect=substitute),
                self.assertRaisesRegex(ValueError, "backup namespace changed"),
            ):
                transaction.prepare_b1(arguments)

            self.assertFalse(state.exists())
            self.assertEqual((backup / "unknown-sentinel").read_bytes(), b"substitute")
            self.assertEqual((admitted / "source/app.py").read_bytes(), b"baseline\n")

    def test_orphan_backup_nested_parent_substitution_during_state_construction_fails_closed(self):
        from deploy import priority_1_5_transaction as transaction

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self._build_prepare_fixture(transaction, root)
            arguments, _destination, backup, state, _private = self._prepare_arguments(
                root, *fixture, "adopt-nested-swap",
            )

            def leave_orphan(_path, _state, _descriptor):
                os._exit(73)

            child = self._fork_prepare(transaction, arguments, leave_orphan)
            _waited, status = os.waitpid(child, 0)
            self.assertEqual(os.WEXITSTATUS(status), 73)

            admitted_parent = backup / "source/admitted-pkg"
            nested_parent = backup / "source/pkg"
            real_populate = transaction._populate_state_fd

            def substitute(path, state_value, descriptor):
                real_populate(path, state_value, descriptor)
                nested_parent.rename(admitted_parent)
                nested_parent.mkdir(mode=0o700)
                (nested_parent / "unknown-sentinel").write_bytes(b"substitute")

            with (
                patch.object(transaction, "SystemdService", return_value=_Service()),
                patch.object(transaction, "_populate_state_fd", side_effect=substitute),
                self.assertRaisesRegex(ValueError, "backup namespace changed"),
            ):
                transaction.prepare_b1(arguments)

            self.assertFalse(state.exists())
            self.assertEqual((nested_parent / "unknown-sentinel").read_bytes(), b"substitute")
            self.assertEqual((admitted_parent / "module.py").read_bytes(), b"baseline module\n")

    def test_orphan_backup_nested_parent_substitution_after_state_publication_removes_only_state(self):
        from deploy import priority_1_5_transaction as transaction

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self._build_prepare_fixture(transaction, root)
            arguments, _destination, backup, state, _private = self._prepare_arguments(
                root, *fixture, "adopt-post-publication-swap",
            )

            def leave_orphan(_path, _state, _descriptor):
                os._exit(73)

            child = self._fork_prepare(transaction, arguments, leave_orphan)
            _waited, status = os.waitpid(child, 0)
            self.assertEqual(os.WEXITSTATUS(status), 73)

            admitted_parent = backup / "source/admitted-pkg"
            nested_parent = backup / "source/pkg"
            real_rename = transaction._rename_noreplace

            def publish_then_substitute(source_name, destination_name, *, parent_fd):
                real_rename(source_name, destination_name, parent_fd=parent_fd)
                if destination_name == state.name:
                    nested_parent.rename(admitted_parent)
                    nested_parent.mkdir(mode=0o700)
                    (nested_parent / "unknown-sentinel").write_bytes(b"substitute")

            with (
                patch.object(transaction, "SystemdService", return_value=_Service()),
                patch.object(transaction, "_rename_noreplace", side_effect=publish_then_substitute),
                self.assertRaisesRegex(ValueError, "backup namespace changed"),
            ):
                transaction.prepare_b1(arguments)

            self.assertFalse(state.exists())
            self.assertEqual((nested_parent / "unknown-sentinel").read_bytes(), b"substitute")
            self.assertEqual((admitted_parent / "module.py").read_bytes(), b"baseline module\n")

    def test_b1_cli_is_exact_and_source_rollback_is_crash_safe(self):
        source_script = Path(__file__).parents[1] / "deploy/priority_1_5_transaction.py"
        with tempfile.TemporaryDirectory() as isolated_temporary:
            isolated_release = Path(isolated_temporary) / "release"
            isolated_deploy = isolated_release / "deploy"
            isolated_deploy.mkdir(parents=True)
            isolated_script = isolated_deploy / source_script.name
            isolated_script.write_bytes(source_script.read_bytes())
            completed = subprocess.run(
                [sys.executable, "-I", "-B", str(isolated_script), "--help"],
                cwd="/", capture_output=True, text=True, timeout=10,
                env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

        from deploy import priority_1_5_transaction as transaction

        expected = {"build-b1", "prepare-b1", "deploy", "status", "recover", "rollback", "redeploy", "finalize"}
        parser = transaction.build_parser()
        self.assertEqual(set(parser._subparsers._group_actions[0].choices), expected)
        for command in expected - {"build-b1", "prepare-b1", "recover"}:
            with self.subTest(command=command, duplicate=True), self.assertRaises(SystemExit) as caught:
                parser.parse_args([command, "--state", "/safe/state.json", "--state", "/safe/state.json"])
            self.assertEqual(caught.exception.code, 2)
        with self.assertRaises(SystemExit):
            parser.parse_args(["deploy", "--state", "/safe/state.json", "--destination", "/tmp/no"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["/usr/bin/git", "init", "-q", str(repository)], check=True)
            subprocess.run(["/usr/bin/git", "-C", str(repository), "config", "user.name", "test"], check=True)
            subprocess.run(["/usr/bin/git", "-C", str(repository), "config", "user.email", "test@example.invalid"], check=True)
            (repository / "app.py").write_bytes(b"baseline\n")
            (repository / "pkg").mkdir()
            (repository / "pkg/module.py").write_bytes(b"baseline module\n")
            subprocess.run(["/usr/bin/git", "-C", str(repository), "add", "app.py", "pkg/module.py"], check=True)
            git_dates = {**os.environ, "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000",
                         "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000"}
            subprocess.run(["/usr/bin/git", "-C", str(repository), "commit", "-qm", "baseline"],
                           check=True, env=git_dates)
            baseline = self._git(repository, "rev-parse", "HEAD^{commit}")
            baseline_tree = self._git(repository, "rev-parse", "HEAD^{tree}")
            (repository / "app.py").write_bytes(b"candidate\n")
            (repository / "pkg/module.py").write_bytes(b"candidate module\n")
            candidate_dates = {**git_dates, "GIT_AUTHOR_DATE": "2000-01-01T00:00:01+0000",
                               "GIT_COMMITTER_DATE": "2000-01-01T00:00:01+0000"}
            subprocess.run(["/usr/bin/git", "-C", str(repository), "commit", "-qam", "candidate"],
                           check=True, env=candidate_dates)
            source = self._git(repository, "rev-parse", "HEAD^{commit}")
            source_tree = self._git(repository, "rev-parse", "HEAD^{tree}")
            diff = subprocess.check_output([
                "/usr/bin/git", "-C", str(repository), "diff", "--binary", "--full-index",
                "--no-ext-diff", f"{baseline}...{source}",
            ])
            reviewed = hashlib.sha256(diff).hexdigest()
            stage = root / "stage"
            build_argv = [
                "build-b1", "--repository", str(repository), "--baseline-commit", baseline,
                "--source-commit", source, "--expected-reviewed-diff-sha256", reviewed,
                "--stage-root", str(stage), "--python", sys.executable,
            ]
            real_subprocess_run = subprocess.run
            def build_without_host_encoder(argv):
                def reject_ffmpeg(command, *args, **kwargs):
                    if command and command[0] == "/usr/bin/ffmpeg":
                        raise AssertionError("build-b1 invoked a host ffmpeg encoder")
                    return real_subprocess_run(command, *args, **kwargs)
                with (
                    patch.object(transaction.subprocess, "run", side_effect=reject_ffmpeg),
                    patch.object(
                        transaction.Image.Image, "save",
                        side_effect=AssertionError("build-b1 invoked a Pillow writer"),
                    ),
                ):
                    return transaction.main(argv)
            self.assertEqual(build_without_host_encoder(build_argv), 0)
            release_manifest_bytes = (stage / "release-manifest.json").read_bytes()
            release_manifest = json.loads(release_manifest_bytes)
            self.assertEqual(release_manifest["files"][0]["path"], "app.py")
            self.assertEqual((stage / "release/app.py").read_bytes(), b"candidate\n")
            self.assertEqual(release_manifest_bytes, transaction.canonical_json(release_manifest))
            self.assertEqual(stat.S_IMODE(stage.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((stage / "release-manifest.json").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((stage / "physical-fixtures.json").stat().st_mode), 0o600)
            self.assertTrue((stage / "physical-fixtures.json").is_file())

            outside = root / "outside"
            outside.mkdir()
            linked_parent = stage / "release" / "linked-parent"
            linked_parent.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "parent path is unsafe"):
                transaction._contained_path(stage / "release", "linked-parent/file.py")
            linked_parent.unlink()

            fixture_path = stage / "physical-fixtures.json"
            fixture_manifest = json.loads(fixture_path.read_bytes())
            fixtures = fixture_manifest["fixtures"]
            expected_fixtures = [
                {"path": "fixtures/b1-landscape-loop.gif", "sha256": "eeca6314667ff792803738970918d13bb967f1855a9eb35cfaf7e2c0435b1483", "size": 561, "kind": "gif", "width": 65, "height": 33, "frames": 3, "durations_ms": [60, 140, 300]},
                {"path": "fixtures/b1-landscape.mp4", "sha256": "93a4b38eaf4e1fb6036c5f735958637bcb954bc1f3897f843bc84459051ae7de", "size": 2084, "kind": "mp4", "width": 11, "height": 5, "frames": 3, "durations_ms": [200, 200, 200]},
                {"path": "fixtures/b1-portrait-finite.gif", "sha256": "4ac60e3e0b6d5dd62113c45deda6d3ada52e4f9c362ce17b348d31b9941acbd9", "size": 3293, "kind": "gif", "width": 3, "height": 7, "frames": 4, "durations_ms": [20, 80, 200, 400]},
                {"path": "fixtures/b1-portrait.mp4", "sha256": "f462de666a87cc665d4b6786206ff04ecae9a4375e9dc8a1aa3d61653123f8fc", "size": 2281, "kind": "mp4", "width": 5, "height": 9, "frames": 4, "durations_ms": [100, 100, 100, 100]},
                {"path": "fixtures/b1-small-loop.gif", "sha256": "e9e51ceb101da4c184c88b6ee9fe84aebedb0dd57371631907cc2edfd2b67ab9", "size": 2421, "kind": "gif", "width": 2, "height": 2, "frames": 3, "durations_ms": [40, 120, 240]},
                {"path": "fixtures/b1-small.mp4", "sha256": "6ee2813bb3ec914898e3c710f189fe8f73cb29cbcbff3e91eb01e4758ff858eb", "size": 1734, "kind": "mp4", "width": 3, "height": 3, "frames": 3, "durations_ms": [40, 40, 40]},
            ]
            self.assertEqual(fixtures, expected_fixtures)
            expected_fixture_manifest = transaction.canonical_json({
                "version": 1, "boundary": "b1", "source_sha": source,
                "tree_sha": source_tree, "fixtures": expected_fixtures,
            })
            self.assertEqual(fixture_path.read_bytes(), expected_fixture_manifest)
            self.assertEqual(
                hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
                "5397f8925ce97930ef597496985417e8bdc174a086840d695f36eb1ada687d80",
            )
            self.assertGreaterEqual(len(fixtures), 6)
            self.assertTrue(all(entry["frames"] > 1 for entry in fixtures))
            self.assertTrue(any(entry["width"] < entry["height"] for entry in fixtures))
            self.assertTrue(any(entry["width"] > entry["height"] for entry in fixtures))
            self.assertTrue(any(entry["width"] <= 3 and entry["height"] <= 3 for entry in fixtures))
            self.assertTrue(any(entry["width"] % 2 or entry["height"] % 2 for entry in fixtures))
            self.assertTrue(any(entry["width"] > 64 or entry["height"] > 32 for entry in fixtures))
            self.assertGreater(len({duration for entry in fixtures for duration in entry["durations_ms"]}), 2)
            from scripts import verify_priority_1_5_foundation_physical as physical_verifier
            loaded = physical_verifier.load_fixture_manifest(fixture_path, source, source_tree)
            self.assertEqual(loaded["fixtures"], fixtures)
            without_oversized = [
                entry for entry in fixtures
                if entry["width"] <= 64 and entry["height"] <= 32
            ]
            no_oversized_path = stage / "physical-fixtures-no-oversized.json"
            no_oversized_path.write_bytes(transaction.canonical_json({
                "version": 1, "boundary": "b1", "source_sha": source,
                "tree_sha": source_tree, "fixtures": without_oversized,
            }))
            with self.assertRaisesRegex(ValueError, "fixture corpus"):
                physical_verifier.load_fixture_manifest(
                    no_oversized_path, source, source_tree,
                )
            gif_loops = []
            from PIL import Image
            for entry in fixtures:
                if entry["kind"] == "gif":
                    with Image.open(stage / entry["path"]) as image:
                        gif_loops.append(image.info.get("loop"))
            self.assertIn(0, gif_loops)
            self.assertIn(1, gif_loops)
            repeat_stage = root / "repeat-stage"
            repeat_argv = list(build_argv)
            repeat_argv[repeat_argv.index("--stage-root") + 1] = str(repeat_stage)
            self.assertEqual(build_without_host_encoder(repeat_argv), 0)
            repeat_manifest = json.loads((repeat_stage / "physical-fixtures.json").read_bytes())
            self.assertEqual(repeat_manifest["fixtures"], fixtures)
            self.assertEqual(
                (repeat_stage / "physical-fixtures.json").read_bytes(),
                fixture_path.read_bytes(),
            )
            for entry in fixtures:
                self.assertEqual(
                    (repeat_stage / entry["path"]).read_bytes(),
                    (stage / entry["path"]).read_bytes(),
                )
            for field, wrong in (("width", 99), ("durations_ms", [200] * fixtures[0]["frames"])):
                tampered = json.loads(json.dumps(fixture_manifest))
                tampered["fixtures"][0][field] = wrong
                with self.subTest(fixture_tamper=field), self.assertRaisesRegex(
                        ValueError, "fixture .*mismatch"):
                    transaction._validate_fixture_manifest(
                        transaction.canonical_json(tampered), stage,
                    )

            bad_stage = root / "bad-stage"
            bad_argv = list(build_argv)
            bad_argv[bad_argv.index("--expected-reviewed-diff-sha256") + 1] = "0" * 64
            bad_argv[bad_argv.index("--stage-root") + 1] = str(bad_stage)
            with self.assertRaisesRegex(ValueError, "reviewed diff"):
                transaction.main(bad_argv)
            self.assertFalse(bad_stage.exists())

            destination = root / "destination"
            subprocess.run(["/usr/bin/git", "clone", "-q", "--no-hardlinks", str(repository), str(destination)], check=True)
            subprocess.run(["/usr/bin/git", "-C", str(destination), "checkout", "-q", baseline], check=True)
            authority_uid, authority_gid = os.geteuid(), os.getegid()
            private = {}
            for relative, value in (("data/config.json", b"config-private"), ("data/paired-devices.json", b"paired-private"), ("resources/media/private.bin", b"media-private")):
                path = destination / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value)
                private[relative] = value
            backup = root / "backup"
            state = root / "authority/state.json"
            prepare = [
                "prepare-b1", "--release-root", str(stage / "release"),
                "--release-manifest", str(stage / "release-manifest.json"),
                "--fixture-manifest", str(stage / "physical-fixtures.json"),
                "--expected-baseline-source-sha", baseline,
                "--expected-baseline-tree-sha", baseline_tree,
                "--expected-source-sha", source, "--expected-tree-sha", source_tree,
                "--destination", str(destination), "--backup-root", str(backup),
                "--service", "matrix-web.service", "--interpreter", sys.executable,
                "--state", str(state),
            ]
            service = _Service()
            (destination / "app.py").write_bytes(b"arbitrary unapproved predecessor\n")
            with patch.object(transaction, "SystemdService", return_value=service), self.assertRaisesRegex(
                    ValueError, "approved baseline"):
                transaction.main(prepare)
            self.assertFalse(state.exists())
            self.assertFalse(backup.exists())
            subprocess.run(["/usr/bin/git", "-C", str(destination), "checkout", "-q", "--", "app.py"], check=True)
            backup.mkdir(mode=0o700)
            unknown = backup / "unknown-sentinel"
            unknown.write_bytes(b"do-not-delete")
            with patch.object(transaction, "SystemdService", return_value=service), self.assertRaisesRegex(
                    ValueError, "published backup"):
                transaction.main(prepare)
            self.assertEqual(unknown.read_bytes(), b"do-not-delete")
            self.assertFalse(state.exists())
            shutil.rmtree(backup)
            with patch.object(transaction, "SystemdService", return_value=service):
                self.assertEqual(transaction.main(prepare), 0)
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
            self.assertFalse(Path(str(state) + ".identity").exists())
            state_value = json.loads(state.read_bytes())
            self.assertEqual(
                set(state_value["self"]),
                {"path", "dev", "ino", "uid", "gid", "mode", "sha256"},
            )
            self.assertEqual(state_value["destination_owner"], {
                "uid": authority_uid, "gid": authority_gid,
            })
            baseline_manifest = json.loads((backup / "baseline-source-manifest.json").read_bytes())
            self.assertEqual([entry["path"] for entry in baseline_manifest["files"]], ["app.py", "pkg/module.py"])
            self.assertFalse(any(name in state.read_text() for name in ("config-private", "paired-private", "media-private")))

            # A real competing process holding the state inode rejects admission
            # before any service or source transition can occur.
            holder = subprocess.Popen(
                [sys.executable, "-c", (
                    "import fcntl,os,sys,time; fd=os.open(sys.argv[1],os.O_RDWR|os.O_NOFOLLOW); "
                    "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); print('locked',flush=True); time.sleep(10)"
                ), str(state)], stdout=subprocess.PIPE, text=True,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "locked")
                before_events = list(service.events)
                before_app = (destination / "app.py").read_bytes()
                with self.assertRaisesRegex(RuntimeError, "transaction state is locked"):
                    transaction.Priority15Transaction.load(state, service=service)
                self.assertEqual(service.events, before_events)
                self.assertEqual((destination / "app.py").read_bytes(), before_app)
            finally:
                holder.terminate()
                holder.wait(timeout=5)
                holder.stdout.close()

            # Every interruption after a source publication remains inactive and recoverable.
            tx = transaction.Priority15Transaction.load(state, service=service)
            for requested, target, expected_bytes in (
                ("deploy", "candidate", b"candidate\n"),
                ("rollback", "baseline", b"baseline\n"),
                ("redeploy", "candidate", b"candidate\n"),
            ):
                with self.subTest(crash=requested):
                    crashed = False
                    def interrupt(point):
                        nonlocal crashed
                        if point.endswith(":published") and not crashed:
                            crashed = True
                            raise transaction.InjectedCrash(point)
                    with self.assertRaises(transaction.InjectedCrash):
                        tx.transition(target, operation=requested, after_boundary=interrupt)
                    self.assertFalse(service.active)
                    tx.close()
                    tx = transaction.Priority15Transaction.load(state, service=service)
                    tx.recover(target)
                    self.assertEqual((destination / "app.py").read_bytes(), expected_bytes)
                    deployed = (destination / "app.py").stat()
                    self.assertEqual((deployed.st_uid, deployed.st_gid),
                                     (authority_uid, authority_gid))
                    self.assertTrue(service.active)
                    for relative, value in private.items():
                        self.assertEqual((destination / relative).read_bytes(), value)

            # Publication verification must consume the already admitted root,
            # not reopen the destination pathname after mutation.
            real_phase = getattr(tx, "_phase")
            def phase_through_pinned_root(destination_root_fd):
                self.assertIsInstance(destination_root_fd, int)
                return real_phase(destination_root_fd)
            with patch.object(tx, "_phase", side_effect=phase_through_pinned_root):
                tx.transition("candidate", operation="redeploy")

            # Swap an admitted parent at a deterministic publication barrier.
            # Mutation must remain pinned to the original parent and namespace
            # disagreement must fail closed while the service stays inactive.
            original_pkg = destination / "pkg"
            displaced_pkg = destination / "pkg.displaced"
            attacker_pkg = destination / "pkg.attacker"
            attacker_pkg.mkdir()
            (attacker_pkg / "module.py").write_bytes(b"attacker sentinel\n")
            swapped = False
            def substitute_parent(point):
                nonlocal swapped
                if point == "app.py:published" and not swapped:
                    swapped = True
                    original_pkg.rename(displaced_pkg)
                    attacker_pkg.rename(original_pkg)
            with self.assertRaisesRegex(ValueError, "destination namespace changed"):
                tx.transition("candidate", operation="redeploy", after_boundary=substitute_parent)
            self.assertFalse(service.active)
            self.assertEqual((original_pkg / "module.py").read_bytes(), b"attacker sentinel\n")
            shutil.rmtree(original_pkg)
            displaced_pkg.rename(original_pkg)
            tx.close()
            tx = transaction.Priority15Transaction.load(state, service=service)
            tx.recover("candidate")

            # Substitution after all publications but during readiness must be
            # detected before success; the substitute remains untouched and
            # the newly started service is stopped again.
            readiness_displaced = root / "destination.readiness-displaced"
            readiness_substitute = root / "destination.readiness-substitute"
            readiness_substitute.mkdir()
            (readiness_substitute / "app.py").write_bytes(b"readiness attacker app\n")
            (readiness_substitute / "pkg").mkdir()
            (readiness_substitute / "pkg/module.py").write_bytes(b"readiness attacker module\n")
            def substitute_root_after_readiness():
                destination.rename(readiness_displaced)
                readiness_substitute.rename(destination)
            service.ready_hook = substitute_root_after_readiness
            with self.assertRaisesRegex(ValueError, "destination namespace changed"):
                tx.transition("candidate", operation="redeploy")
            service.ready_hook = lambda: None
            self.assertFalse(service.active)
            self.assertEqual((destination / "app.py").read_bytes(), b"readiness attacker app\n")
            self.assertEqual((destination / "pkg/module.py").read_bytes(), b"readiness attacker module\n")
            shutil.rmtree(destination)
            readiness_displaced.rename(destination)
            tx.close()
            tx = transaction.Priority15Transaction.load(state, service=service)
            tx.recover("candidate")

            status = tx.status()
            self.assertEqual(status["source"], "candidate")
            self.assertEqual(status["service"]["service"], "matrix-web.service")
            self.assertEqual(status["service"]["n_restarts_baseline"], 0)
            self.assertTrue(status["service"]["display_lock"]["locked"])
            report = tx.finalize()
            self.assertEqual(set(report), {"version", "boundary", "accepted_source_sha", "accepted_tree_sha", "release_manifest_sha256", "baseline_manifest_sha256", "status"})
            self.assertEqual(report["status"], "accepted")

            # A pathname replacement cannot redirect an admitted destination.
            displaced_destination = root / "displaced-destination"
            destination.rename(displaced_destination)
            destination.mkdir()
            (destination / "app.py").write_bytes(b"candidate\n")
            tx.close()
            with self.assertRaisesRegex(ValueError, "destination identity"):
                transaction.Priority15Transaction.load(state, service=service)
            shutil.rmtree(destination)
            displaced_destination.rename(destination)

            # Replacing the state inode with byte-identical content is not authority.
            replacement = state.with_suffix(".replacement")
            replacement.write_bytes(state.read_bytes())
            replacement.chmod(0o600)
            os.replace(replacement, state)
            with self.assertRaisesRegex(ValueError, "state identity"):
                transaction.Priority15Transaction.load(state, service=service)

    def test_systemd_status_binds_cgroup_display_lock_and_warning_baseline(self):
        from deploy import priority_1_5_transaction as transaction

        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "matrix-display.lock"
            lock_path.write_bytes(b"")
            descriptor = os.open(lock_path, os.O_RDWR)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

            properties = types.SimpleNamespace(
                stdout=(
                    "Id=matrix-web.service\nActiveState=active\nSubState=running\n"
                    f"MainPID={os.getpid()}\nNRestarts=0\n"
                    "ControlGroup=/system.slice/matrix-web.service\n"
                )
            )

            class Response:
                status = 302
                def read(self):
                    return b""

            class Connection:
                def request(self, *_args):
                    return None
                def getresponse(self):
                    return Response()
                def close(self):
                    return None

            journal = types.SimpleNamespace(stdout=b"", returncode=0)
            service = transaction.SystemdService("matrix-web.service")
            try:
                with (
                    patch.object(transaction, "DISPLAY_LOCK", lock_path),
                    patch.object(service, "_run", return_value=properties),
                    patch.object(transaction.http.client, "HTTPConnection", return_value=Connection()),
                    patch.object(transaction.subprocess, "run", return_value=journal) as run,
                ):
                    evidence = service.status(warning_since=1)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            self.assertTrue(evidence["display_lock"]["locked"])
            self.assertEqual(evidence["main_pid"], os.getpid())
            self.assertEqual(evidence["warning_count"], 0)
            self.assertEqual(run.call_args.args[0][0], "/usr/bin/journalctl")
