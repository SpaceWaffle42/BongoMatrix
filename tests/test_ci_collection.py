"""Discovery contract mutations use disposable modules, never product fixtures."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CollectionContractTests(unittest.TestCase):
    def probe(self, sources, required=None, non_essential=None, extra_args=()):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, source in sources.items():
                (root / name).write_text(source, encoding='utf-8')
            manifest = root / 'required.json'
            manifest.write_text(json.dumps(required or {'test_owner': ['Owner.test_required']}))
            quarantine = root / 'non-essential.json'
            quarantine.write_text(json.dumps({'tests': non_essential or {}}))
            return subprocess.run(
                [sys.executable, str(ROOT / 'scripts/ci_collection.py'),
                 '--tests', str(root), '--required', str(manifest),
                 '--non-essential', str(quarantine), *extra_args],
                cwd=ROOT, capture_output=True, text=True, timeout=20,
            )

    MARKED = ('import unittest\n'
              'def non_essential(obj):\n'
              '    obj.__matrix_non_essential__ = "flake"\n'
              '    return obj\n'
              'class Owner(unittest.TestCase):\n'
              '    def test_required(self): pass\n'
              '    @non_essential\n'
              '    def test_flaky(self): pass\n')
    BOTH = {'test_owner': ['Owner.test_required', 'Owner.test_flaky']}

    def source(self, extra=''):
        return ('import unittest\nclass Owner(unittest.TestCase):\n'
                '    def test_required(self): pass\n' + extra)

    def test_duplicate_required_module_keys_reject_missing_earlier_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'test_owner.py').write_text(self.source(), encoding='utf-8')
            manifest = root / 'required.json'
            manifest.write_text(
                '{"test_owner":["Owner.test_missing"],'
                '"test_owner":["Owner.test_required"]}', encoding='utf-8')
            output = root / 'collection.json'
            result = subprocess.run(
                [sys.executable, str(ROOT / 'scripts/ci_collection.py'),
                 '--tests', str(root), '--required', str(manifest), '--output', str(output),
                 '--non-essential', str(ROOT / 'ci/non-essential-tests.json')],
                cwd=ROOT, capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('duplicate required key', result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertFalse(output.exists())

    def test_added_valid_test_is_allowed(self):
        result = self.probe({'test_owner.py': self.source('    def test_added(self): pass\n')})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['tests'], 2)

    def test_imported_foreign_case_is_rejected(self):
        result = self.probe({'test_owner.py': self.source(),
                             'test_foreign.py': 'from test_owner import Owner\n'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('foreign', result.stderr)

    def test_duplicate_case_alias_is_rejected(self):
        result = self.probe({'test_owner.py': self.source() + 'Alias = Owner\n'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('duplicate', result.stderr)

    def test_dropped_required_case_is_rejected(self):
        result = self.probe({'test_owner.py': self.source().replace('test_required', 'test_other')})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing required', result.stderr)

    def test_empty_collection_is_rejected(self):
        result = self.probe({'test_owner.py': '# no cases\n'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('empty', result.stderr)

    def test_collection_error_is_rejected(self):
        result = self.probe({'test_owner.py': 'raise RuntimeError("broken import")\n'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('collection error', result.stderr)

    def test_dropped_required_module_is_rejected(self):
        result = self.probe({'test_other.py': self.source()})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing required', result.stderr)

    def test_listed_non_essential_test_is_reported_and_printable(self):
        listed = {'test_owner': ['Owner.test_flaky']}
        result = self.probe({'test_owner.py': self.MARKED}, self.BOTH, listed)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['non_essential'], 1)
        printed = self.probe({'test_owner.py': self.MARKED}, self.BOTH, listed,
                             ('--print-non-essential',))
        self.assertEqual(printed.returncode, 0, printed.stderr)
        self.assertEqual(printed.stdout.split(), ['tests.test_owner.Owner.test_flaky'])

    def test_unlisted_non_essential_marker_is_rejected(self):
        result = self.probe({'test_owner.py': self.MARKED}, self.BOTH)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('non-essential marker/list mismatch', result.stderr)

    def test_listed_but_unmarked_non_essential_test_is_rejected(self):
        result = self.probe({'test_owner.py': self.source()}, None,
                            {'test_owner': ['Owner.test_required']})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('non-essential marker/list mismatch', result.stderr)

    def test_non_essential_test_must_stay_required(self):
        result = self.probe({'test_owner.py': self.MARKED}, None,
                            {'test_owner': ['Owner.test_flaky']})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('non-essential not required', result.stderr)

    def test_repository_quarantine_is_exactly_the_reviewed_issue_32_list(self):
        declared = json.loads((ROOT / 'ci/non-essential-tests.json').read_text())
        self.assertIn('#32', declared['issue'])
        self.assertEqual(declared['tests'], {
            'test_feature_release_full_frames': [
                'FullFrameTests.test_decode_failure',
                'FullFrameTests.test_expected_decoded_frames',
                'FullFrameTests.test_invalid_dimensions',
                'FullFrameTests.test_nonadvancing_revision',
                'FullFrameTests.test_wrong_pixels',
            ],
            'test_release_projection': ['ProjectionTests.test_native_dashboard_projection'],
        })
