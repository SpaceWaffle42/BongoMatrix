"""ci_shard contract: disposable test trees only, never product fixtures."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHARD = ROOT / 'scripts/ci_shard.py'
COLLECT = ROOT / 'scripts/ci_collection.py'

MODULES = {
    'test_alpha.py': ('import unittest\n'
                      'class Alpha(unittest.TestCase):\n'
                      '    order = []\n'
                      '    @classmethod\n'
                      '    def setUpClass(cls): cls.ready = True\n'
                      '    def test_a(self): self.assertTrue(self.ready)\n'
                      '    def test_b(self):\n'
                      '        for i in range(2):\n'
                      '            with self.subTest(i=i): pass\n'),
    'test_beta.py': ('import unittest\n'
                     'class Beta(unittest.TestCase):\n'
                     '    def test_c(self): pass\n'
                     'class Gamma(unittest.TestCase):\n'
                     '    def test_d(self): pass\n'
                     '    def test_e(self): pass\n'),
}
WEIGHTS = {'test_alpha.Alpha.test_a': 30, 'test_alpha.Alpha.test_b': 1,
           'test_beta.Beta.test_c': 20, 'test_beta.Gamma.test_d': 5}


def load_shard_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location('ci_shard_under_test', SHARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ShardContractTests(unittest.TestCase):
    def tree(self, root, modules=MODULES):
        tests = root / 'tests'
        tests.mkdir()
        for name, source in modules.items():
            (tests / name).write_text(source, encoding='utf-8')
        weights = root / 'weights.json'
        weights.write_text(json.dumps({'default_seconds': 1, 'tests': WEIGHTS}), encoding='utf-8')
        required = root / 'required.json'
        required.write_text(json.dumps({'test_alpha': ['Alpha.test_a']}), encoding='utf-8')
        return tests, weights, required

    def call(self, *args, cwd):
        return subprocess.run([sys.executable, *map(str, args)], cwd=cwd,
                              capture_output=True, text=True, timeout=60)

    def run_all_shards(self, root, total, skip=()):
        tests, weights, required = self.tree(root)
        (root / 'shards').mkdir()
        for index in range(1, total + 1):
            collected = self.call(COLLECT, '--tests', tests, '--required', required,
                                  '--output', root / f'shards/collection-{index}.json', cwd=root)
            self.assertEqual(collected.returncode, 0, collected.stderr)
            if index in skip:
                continue
            ran = self.call(SHARD, 'run', '--index', index, '--total', total, '--tests', tests,
                            '--weights', weights, '--manifest', root / f'shards/shard-{index}.json',
                            '--output', root / 'test-results', cwd=root)
            self.assertEqual(ran.returncode, 0, ran.stderr)
        return self.call(SHARD, 'verify', '--collections', root / 'shards', '--manifests', root / 'shards',
                         '--results', root / 'test-results', '--output', root / 'verify.json', cwd=root)

    def test_plan_is_deterministic_balanced_and_keeps_classes_whole(self):
        module = load_shard_module()
        ids = ['m.A.t1', 'm.A.t2', 'm.B.t1', 'm.C.t1', 'm.C.t2', 'm.D.t1']
        weights = {'m.A.t1': 50, 'm.A.t2': 10, 'm.B.t1': 40, 'm.C.t1': 15, 'm.C.t2': 10}
        first, load = module.plan(ids, weights, 2, default=5)
        self.assertEqual((first, load), module.plan(list(reversed(ids)), weights, 2, default=5))
        self.assertEqual(sorted(t for shard in first for t in shard), sorted(ids))
        for cls in ('m.A', 'm.C'):
            owners = {i for i, shard in enumerate(first) for t in shard if t.startswith(cls + '.')}
            self.assertEqual(len(owners), 1, cls)
        # LPT by class cost (A=60, B=40, C=25, D=5) balances exactly.
        self.assertEqual(load, [65.0, 65.0])
        with self.assertRaises(module.ShardError):
            module.plan(['m.A.t1', 'm.A.t1'], {}, 2)
        with self.assertRaises(module.ShardError):
            module.plan(ids, weights, 0)

    def test_shards_partition_the_suite_and_verify_proves_exact_coverage(self):
        for total in (1, 2, 3):
            with self.subTest(total=total), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                verified = self.run_all_shards(root, total)
                self.assertEqual(verified.returncode, 0, verified.stderr)
                report = json.loads((root / 'verify.json').read_text())
                self.assertEqual((report['collected'], report['shards']), (5, total))
                self.assertEqual((report['missing'], report['foreign'], report['duplicated']), ([], [], []))
                manifests = [json.loads((root / f'shards/shard-{i}.json').read_text()) for i in range(1, total + 1)]
                assigned = [t for m in manifests for t in m['tests']]
                self.assertEqual(len(assigned), len(set(assigned)))
                self.assertEqual(len(assigned), 5)

    def test_missing_shard_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            verified = self.run_all_shards(Path(directory), 2, skip={2})
            self.assertNotEqual(verified.returncode, 0)
            self.assertIn('incomplete', verified.stderr)

    def test_dropped_or_duplicated_junit_cases_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(self.run_all_shards(root, 2).returncode, 0)
            reports = sorted((root / 'test-results').glob('*.xml'))
            extra = root / 'test-results' / 'TEST-duplicate.xml'
            extra.write_bytes(reports[0].read_bytes())
            again = self.call(SHARD, 'verify', '--collections', root / 'shards', '--manifests', root / 'shards',
                              '--results', root / 'test-results', '--output', root / 'v2.json', cwd=root)
            self.assertNotEqual(again.returncode, 0)
            self.assertIn('duplicated=', again.stderr)
            extra.unlink()
            reports[0].unlink()
            dropped = self.call(SHARD, 'verify', '--collections', root / 'shards', '--manifests', root / 'shards',
                                '--results', root / 'test-results', '--output', root / 'v3.json', cwd=root)
            self.assertNotEqual(dropped.returncode, 0)
            self.assertIn('missing=', dropped.stderr)

    def test_import_error_fails_every_shard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broken = dict(MODULES, **{'test_broken.py': 'raise RuntimeError("broken import")\n'})
            tests, weights, _ = self.tree(root, broken)
            for index in (1, 2):
                ran = self.call(SHARD, 'run', '--index', index, '--total', 2, '--tests', tests, '--weights', weights,
                                '--manifest', root / f'shard-{index}.json', '--output', root / 'out', cwd=root)
                self.assertNotEqual(ran.returncode, 0)
                self.assertIn('collection error', ran.stderr)

    def test_failing_test_fails_only_its_shard_but_is_still_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failing = dict(MODULES)
            failing['test_beta.py'] = failing['test_beta.py'].replace('def test_c(self): pass', 'def test_c(self): self.fail("boom")')
            tests, weights, _ = self.tree(root, failing)
            codes = []
            for index in (1, 2):
                ran = self.call(SHARD, 'run', '--index', index, '--total', 2, '--tests', tests, '--weights', weights,
                                '--manifest', root / f'shard-{index}.json', '--output', root / 'out', cwd=root)
                codes.append(ran.returncode)
            self.assertEqual(sorted(codes), [0, 1])
            xml = ''.join(p.read_text() for p in (root / 'out').glob('*.xml'))
            self.assertIn('test_c', xml)
            self.assertIn('<failure', xml)

    def test_weights_cover_the_real_suite(self):
        weights = json.loads((ROOT / 'ci/test-weights.json').read_text(encoding='utf-8'))
        self.assertGreater(len(weights['tests']), 1000)
        self.assertTrue(all(isinstance(v, (int, float)) and v >= 0 for v in weights['tests'].values()))
        modules = {p.stem for p in (ROOT / 'tests').glob('test_*.py')}
        weighted = {t.split('.', 1)[0] for t in weights['tests']}
        # New modules fall back to default_seconds; a deleted module must not linger.
        self.assertLessEqual(weighted, modules)


if __name__ == '__main__':
    unittest.main()
