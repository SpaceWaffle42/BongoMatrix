"""deploy/ship.py gates: pure planning logic plus refusal paths with all I/O faked."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location('ship_under_test', ROOT / 'deploy/ship.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ShipPlanningTests(unittest.TestCase):
    def setUp(self):
        self.ship = load()

    def test_only_runtime_code_is_deployable(self):
        runtime = ['webapp.py', 'app/static/screen_media.js', 'app/templates/dashboard.html', 'modules/clock.py',
                   'setup/portal.html', 'pairing.py']
        never = ['data/config.json', 'resources/media/splash1.gif', 'tests/test_webapp.py', 'docs/CI.md',
                 'deploy/ship.py', 'venv/bin/python', 'backups/x/config.json', '.gitlab-ci.yml', 'README.md',
                 'app/__pycache__/x.pyc', 'requirements.txt', '.git/config']
        for path in runtime:
            self.assertTrue(self.ship.is_runtime(path), path)
        for path in never:
            self.assertFalse(self.ship.is_runtime(path), path)

    def test_plan_lists_changed_and_new_runtime_files_only(self):
        main = {'webapp.py': 'b', 'pairing.py': 'same', 'app/new.js': 'n', 'tests/test_x.py': 'changed'}
        prod = {'webapp.py': 'a', 'pairing.py': 'same', 'tests/test_x.py': 'old', 'data/config.json': 'x'}
        self.assertEqual(self.ship.plan_files(main, prod), ['app/new.js', 'webapp.py'])

    def test_version_and_public_page_parsing(self):
        self.assertEqual(self.ship.version_of(b'X = 1\nAPPLICATION_VERSION = "0.3.68-preview"\n'), '0.3.68-preview')
        with self.assertRaises(self.ship.ShipError):
            self.ship.version_of(b'nothing here')
        page = b'<footer>Matrix 0.3.67-preview</footer><span>0.3.67-preview</span>'
        self.assertEqual(self.ship.public_versions(page), ['0.3.67-preview'])


class ShipRefusalTests(unittest.TestCase):
    """Every refusal must happen before anything is staged on the device."""

    def setUp(self):
        self.ship = load()
        self.stage = mock.patch.object(self.ship, 'stage_and_run', side_effect=AssertionError('must not deploy'))
        self.stage.start()
        self.addCleanup(self.stage.stop)
        mock.patch.object(self.ship, 'write_result', return_value=Path('/dev/null')).start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(self.ship, 'git', side_effect=lambda *a: b'f' * 40 + b'\n').start()

    def scenario(self, ci='success', files=('webapp.py',), version='0.3.68-preview', live=('0.3.67-preview',), apply=True,
                 unsupported=(), argv=None):
        mock.patch.object(self.ship, 'ci_status', return_value=(ci, 430)).start()
        mock.patch.object(self.ship, 'build_plan', return_value=(list(files), version, list(unsupported))).start()
        mock.patch.object(self.ship, 'fetch_public', return_value=' '.join(live).encode()).start()
        with mock.patch('sys.stdout', new=io.StringIO()) as out:
            code = self.ship.main(argv if argv is not None else (['--apply'] if apply else []))
        return code, out.getvalue()

    def test_red_or_running_ci_refuses(self):
        for status in ('failed', 'running', 'missing', 'canceled'):
            with self.subTest(status=status):
                code, out = self.scenario(ci=status)
                self.assertEqual(code, 1)
                self.assertIn('NOT DEPLOYABLE', out)

    def test_unchanged_version_refuses(self):
        code, out = self.scenario(version='0.3.67-preview')
        self.assertEqual(code, 1)
        self.assertIn('bump APPLICATION_VERSION', out)
        code, out = self.scenario(files=('app/static/screen_media.js',))
        self.assertEqual(code, 1)
        self.assertIn('webapp.py (APPLICATION_VERSION) unchanged', out)

    def test_nothing_to_deploy_refuses(self):
        code, out = self.scenario(files=())
        self.assertEqual(code, 1)
        self.assertIn('already matches origin/main', out)

    def test_dry_run_never_deploys(self):
        code, out = self.scenario(apply=False)
        self.assertEqual(code, 0)
        self.assertIn('dry run OK', out)

    def test_unidentifiable_live_version_refuses(self):
        for live in ((), ('0.3.66-preview', '0.3.67-preview')):
            for apply in (False, True):
                with self.subTest(live=live, apply=apply):
                    code, out = self.scenario(live=live, apply=apply)
                    self.assertEqual(code, 1)
                    self.assertIn('cannot identify the production version', out)

    def test_same_version_has_no_bypass(self):
        with mock.patch('sys.stderr', new=io.StringIO()), self.assertRaises(SystemExit):
            self.scenario(version='0.3.67-preview', argv=['--apply', '--allow-same-version'])

    def test_unsupported_differences_refuse(self):
        code, out = self.scenario(unsupported=['requirements.txt differs from production'])
        self.assertEqual(code, 1)
        self.assertIn('requirements.txt differs', out)


class ShipInventoryTests(unittest.TestCase):
    def setUp(self):
        self.ship = load()

    def test_dependency_unit_and_deleted_runtime_files_are_reported(self):
        main = {'webapp.py': 'b', 'requirements.txt': 'r2', 'deploy/matrix-web.service': 'u2', 'README.md': ''}
        prod = {'webapp.py': 'a', 'requirements.txt': 'r1', self.ship.UNIT_KEY: 'u1',
                'modules/removed.py': 'x', 'tests/old_test.py': 'y'}
        problems = self.ship.unsupported_changes(main, prod)
        self.assertEqual(len(problems), 3, problems)
        self.assertIn('requirements.txt', problems[0])
        self.assertIn('matrix-web.service', problems[1])
        self.assertIn('modules/removed.py', problems[2])
        self.assertNotIn('tests/old_test.py', problems[2])
        self.assertEqual(self.ship.plan_files(main, prod), ['webapp.py'])

    def test_matching_inventory_has_no_unsupported_changes(self):
        main = {'webapp.py': 'b', 'requirements.txt': 'r', 'deploy/matrix-web.service': 'u'}
        prod = {'webapp.py': 'a', 'requirements.txt': 'r', self.ship.UNIT_KEY: 'u', 'data/x': 'state'}
        self.assertEqual(self.ship.unsupported_changes(main, prod), [])

    def test_ci_status_only_counts_main(self):
        sha = 'f' * 40
        pipelines = [{'id': 9, 'ref': 'feature', 'sha': sha, 'status': 'success'},
                     {'id': 7, 'ref': 'main', 'sha': sha, 'status': 'failed'},
                     {'id': 8, 'ref': 'main', 'sha': 'e' * 40, 'status': 'success'}]
        with mock.patch.object(self.ship, 'gitlab', return_value=pipelines) as api:
            self.assertEqual(self.ship.ci_status(sha), ('failed', 7))
        self.assertIn('ref=main', api.call_args[0][0])
        with mock.patch.object(self.ship, 'gitlab', return_value=pipelines[:1]):
            self.assertEqual(self.ship.ci_status(sha), ('missing', None))


class ShipApplyTests(unittest.TestCase):
    def setUp(self):
        self.ship = load()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(self.ship, 'write_result', return_value=Path('/dev/null')).start()
        mock.patch.object(self.ship, 'git', side_effect=lambda *a: b'f' * 40 + b'\n').start()
        mock.patch.object(self.ship, 'ci_status', return_value=('success', 430)).start()
        mock.patch.object(self.ship, 'build_plan', return_value=(['webapp.py'], '0.3.68-preview', {})).start()

    def run_apply(self, installer_status, pages, rollback_status='ROLLED_BACK_NORMAL'):
        calls = []
        self.result = None

        def stage_and_run(files, sha, rollback_stage=None):
            calls.append(rollback_stage)
            if rollback_stage:
                return {'status': rollback_status}
            return {'status': installer_status, 'stage': '/var/tmp/matrix-ship-1'}

        def keep(result):
            self.result = dict(result)
            return Path('/dev/null')
        mock.patch.object(self.ship, 'write_result', side_effect=keep).start()
        mock.patch.object(self.ship, 'stage_and_run', side_effect=stage_and_run).start()
        mock.patch.object(self.ship, 'fetch_public',
                          side_effect=[p if isinstance(p, Exception) else p.encode() for p in pages]).start()
        with mock.patch('sys.stdout', new=io.StringIO()) as out, mock.patch('sys.stderr', new=io.StringIO()):
            code = self.ship.main(['--apply'])
        return code, calls, out.getvalue()

    def test_success_requires_public_version_readback(self):
        code, calls, out = self.run_apply('INSTALLED', ['0.3.67-preview', '0.3.68-preview'])
        self.assertEqual((code, calls), (0, [None]))
        self.assertIn('DEPLOYED 0.3.68-preview', out)

    def test_wrong_public_version_triggers_rollback(self):
        code, calls, out = self.run_apply('INSTALLED', ['0.3.67-preview', '0.3.67-preview', '0.3.67-preview'])
        self.assertEqual(code, 2)
        self.assertEqual(calls, [None, '/var/tmp/matrix-ship-1'])
        self.assertIn('rolling back', out)

    def test_public_check_exception_triggers_rollback(self):
        code, calls, out = self.run_apply('INSTALLED', ['0.3.67-preview', OSError('connection reset'), '0.3.67-preview'])
        self.assertEqual(code, 2)
        self.assertEqual(calls, [None, '/var/tmp/matrix-ship-1'])
        self.assertEqual(self.result['status'], 'ROLLED_BACK')
        self.assertIn('connection reset', self.result['public_check_failure'])

    def test_failed_rollback_is_not_reported_as_rolled_back(self):
        code, calls, out = self.run_apply('INSTALLED', ['0.3.67-preview', '0.3.67-preview', '0.3.68-preview'],
                                          rollback_status='FAILED_NEEDS_OPERATOR')
        self.assertEqual(code, 2)
        self.assertEqual(self.result['status'], 'ROLLBACK_FAILED')
        code, calls, out = self.run_apply('INSTALLED', ['0.3.67-preview', '0.3.67-preview', '0.3.68-preview'])
        self.assertEqual(self.result['status'], 'ROLLBACK_FAILED', 'public page must show the previous version')
        code, calls, out = self.run_apply('INSTALLED', ['0.3.67-preview', '0.3.67-preview', OSError('down')])
        self.assertEqual(self.result['status'], 'ROLLBACK_FAILED')

    def test_installer_failure_is_reported_without_double_rollback(self):
        code, calls, out = self.run_apply('FAILED_PREVIOUS_VERSION_RESTORED', ['0.3.67-preview', '0.3.67-preview'])
        self.assertEqual((code, calls), (2, [None]))
        self.assertIn('installer failed', out)

    def test_rollback_stage_path_is_validated(self):
        with self.assertRaises(self.ship.ShipError):
            self.ship.stage_and_run([], '', rollback_stage='/var/tmp/../../etc')


def load_remote():
    spec = importlib.util.spec_from_file_location('ship_remote_under_test', ROOT / 'deploy/ship_remote.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ShipRemoteSafetyTests(unittest.TestCase):
    def test_remote_installer_refuses_state_paths_and_keeps_checkout_mode(self):
        remote = load_remote()
        self.assertEqual(remote.ROOT_OWNER, (1000, 985, 0o750))
        self.assertEqual(remote.STATE_PREFIXES, ('data/', 'resources/', 'venv/', '.git/', 'backups/'))
        source = (ROOT / 'deploy/ship_remote.py').read_text(encoding='utf-8')
        self.assertIn("'data', 'resources/media'", source)   # opaque state backup before any write
        self.assertLess(source.index("status='BACKUP_READY'"), source.index("publish(ROOT / entry['path']"))


class ShipRemoteRecoveryTests(unittest.TestCase):
    """Device installer control flow on a temporary tree; systemd and locks are faked."""

    def setUp(self):
        self.remote = load_remote()
        tmp = tempfile.TemporaryDirectory(prefix='matrix-ship-test-')
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.root, self.stage = base / 'matrix', base / 'stage'
        (self.root / 'modules').mkdir(parents=True)
        (self.stage / 'payload/modules').mkdir(parents=True)
        self.old = {'webapp.py': b'old web', 'modules/a.py': b'old a'}
        self.new = {'webapp.py': b'new web', 'modules/a.py': b'new a'}
        for name in self.old:
            (self.root / name).write_bytes(self.old[name])
            (self.stage / 'payload' / name).write_bytes(self.new[name])
        files = [{'path': n, 'sha256': hashlib.sha256(self.new[n]).hexdigest()} for n in self.new]
        (self.stage / 'spec.json').write_text(json.dumps({'source_sha': 'f' * 40, 'files': files}))
        r = self.remote
        r.ROOT, r.STAGE = self.root, self.stage
        r.NEW_FILE_META = {'uid': os.getuid(), 'gid': os.getgid(), 'mode': 0o644}
        self.events = []
        self.start_failures = []
        for name, fn in {
            'root_ok': lambda: None, 'locked_and_quiet': lambda: None,
            'props': lambda: {'PrivateNetwork': 'no'},
            'stop': lambda: self.events.append('stop'),
            'take': lambda path, create=False: self.events.append('take') or 99,
            'release': lambda fd: self.events.append('release'),
            'start_normal': self.start_normal,
        }.items():
            patcher = mock.patch.object(r, name, side_effect=fn)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.geteuid = mock.patch.object(r.os, 'geteuid', return_value=0)
        self.geteuid.start()
        self.addCleanup(self.geteuid.stop)

    def start_normal(self):
        self.events.append('start')
        if self.start_failures:
            raise self.start_failures.pop(0)
        return {'loopback_http': '302'}

    def report(self, name='execution.json'):
        return json.loads((self.stage / name).read_text())

    def tree(self):
        return {n: (self.root / n).read_bytes() for n in self.old}

    def test_install_then_rollback_restores_previous_code(self):
        self.assertEqual(self.remote.main([]), 0)
        self.assertEqual(self.report()['status'], 'INSTALLED')
        self.assertEqual(self.tree(), self.new)
        self.assertEqual(self.remote.main(['--rollback']), 0)
        self.assertEqual(self.report('rollback-execution.json')['status'], 'ROLLED_BACK_NORMAL')
        self.assertEqual(self.tree(), self.old)

    def test_failed_start_after_install_restores_previous_code(self):
        self.start_failures.append(RuntimeError('no HTTP'))
        self.assertEqual(self.remote.main([]), 2)
        report = self.report()
        self.assertEqual(report['status'], 'FAILED_PREVIOUS_VERSION_RESTORED')
        self.assertEqual(self.tree(), self.old)
        self.assertEqual(self.events.count('start'), 2)

    def test_restore_validates_everything_before_first_write(self):
        self.assertEqual(self.remote.main([]), 0)
        (self.root / 'modules/a.py').write_bytes(b'edited by someone else')
        self.assertEqual(self.remote.main(['--rollback']), 2)
        report = self.report('rollback-execution.json')
        self.assertIn('changed after this deploy', report['primary_error']['message'])
        self.assertEqual(self.tree(), {'webapp.py': self.new['webapp.py'], 'modules/a.py': b'edited by someone else'})
        self.assertFalse(report['production_mutated'])
        self.assertEqual(report['status'], 'ROLLBACK_FAILED_UNCHANGED_RESTARTED')

    def test_partial_restore_leaves_service_stopped_for_operator(self):
        self.start_failures.append(RuntimeError('no HTTP'))
        real_publish = self.remote.publish
        calls = []

        def flaky(path, data, meta):
            calls.append(path)
            if len(calls) == 4:        # 2 install writes, 1st restore write ok, 2nd restore write fails
                raise OSError('disk error')
            return real_publish(path, data, meta)
        with mock.patch.object(self.remote, 'publish', side_effect=flaky):
            self.assertEqual(self.remote.main([]), 2)
        report = self.report()
        self.assertEqual(report['status'], 'FAILED_NEEDS_OPERATOR')
        self.assertIn('part-way', report['cleanup_error']['message'])
        self.assertTrue(report['production_mutated'])
        self.assertEqual(self.events.count('start'), 1, 'mixed tree must not be restarted')

    def test_report_write_failure_does_not_skip_recovery(self):
        real_write = Path.write_text

        def full_disk(path, *args, **kwargs):
            if path.name == 'code-manifest.json' or path.suffix == '.tmp':
                raise OSError(28, 'No space left on device')
            return real_write(path, *args, **kwargs)
        with mock.patch.object(Path, 'write_text', full_disk):
            self.assertEqual(self.remote.main([]), 2)
        self.assertEqual(self.tree(), self.old)
        self.assertIn('start', self.events, 'stopped service must be restarted')

    def test_failure_after_stop_attempt_restarts_service(self):
        def stop_then_fail():
            self.events.append('stop')
            raise AssertionError('property query failed')
        with mock.patch.object(self.remote, 'stop', side_effect=stop_then_fail):
            self.assertEqual(self.remote.main([]), 2)
        self.assertEqual(self.report()['status'], 'FAILED_PREVIOUS_VERSION_RESTORED')
        self.assertEqual(self.events[-1], 'start')
        self.assertEqual(self.tree(), self.old)


if __name__ == '__main__':
    unittest.main()
