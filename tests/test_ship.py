"""deploy/ship.py gates: pure planning logic plus refusal paths with all I/O faked."""
import importlib.util
import io
import json
from pathlib import Path
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

    def scenario(self, ci='success', files=('webapp.py',), version='0.3.68-preview', live=('0.3.67-preview',), apply=True):
        mock.patch.object(self.ship, 'ci_status', return_value=(ci, 430)).start()
        mock.patch.object(self.ship, 'build_plan', return_value=(list(files), version, {})).start()
        mock.patch.object(self.ship, 'fetch_public', return_value=' '.join(live).encode()).start()
        with mock.patch('sys.stdout', new=io.StringIO()) as out:
            code = self.ship.main(['--apply'] if apply else [])
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


class ShipApplyTests(unittest.TestCase):
    def setUp(self):
        self.ship = load()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(self.ship, 'write_result', return_value=Path('/dev/null')).start()
        mock.patch.object(self.ship, 'git', side_effect=lambda *a: b'f' * 40 + b'\n').start()
        mock.patch.object(self.ship, 'ci_status', return_value=('success', 430)).start()
        mock.patch.object(self.ship, 'build_plan', return_value=(['webapp.py'], '0.3.68-preview', {})).start()

    def run_apply(self, installer_status, pages):
        calls = []

        def stage_and_run(files, sha, rollback_stage=None):
            calls.append(rollback_stage)
            if rollback_stage:
                return {'status': 'ROLLED_BACK_NORMAL'}
            return {'status': installer_status, 'stage': '/var/tmp/matrix-ship-1'}
        mock.patch.object(self.ship, 'stage_and_run', side_effect=stage_and_run).start()
        mock.patch.object(self.ship, 'fetch_public', side_effect=[p.encode() for p in pages]).start()
        with mock.patch('sys.stdout', new=io.StringIO()) as out:
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

    def test_installer_failure_is_reported_without_double_rollback(self):
        code, calls, out = self.run_apply('FAILED_PREVIOUS_VERSION_RESTORED', ['0.3.67-preview', '0.3.67-preview'])
        self.assertEqual((code, calls), (2, [None]))
        self.assertIn('installer failed', out)

    def test_rollback_stage_path_is_validated(self):
        with self.assertRaises(self.ship.ShipError):
            self.ship.stage_and_run([], '', rollback_stage='/var/tmp/../../etc')


class ShipRemoteSafetyTests(unittest.TestCase):
    def test_remote_installer_refuses_state_paths_and_keeps_checkout_mode(self):
        source = (ROOT / 'deploy/ship_remote.py').read_text(encoding='utf-8')
        self.assertIn("ROOT_OWNER = (1000, 985, 0o750)", source)
        self.assertIn("startswith(('data/', 'resources/', 'venv/', '.git/', 'backups/'))", source)
        self.assertIn("'data', 'resources/media'", source)   # opaque state backup before any write
        self.assertLess(source.index("status='BACKUP_READY'"), source.index("publish(ROOT / entry['path']"))
        compile(source, 'ship_remote.py', 'exec')
        json.dumps({})


if __name__ == '__main__':
    unittest.main()
