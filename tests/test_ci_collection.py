"""Discovery contract mutations use disposable modules, never product fixtures."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CollectionContractTests(unittest.TestCase):
    def probe(self, sources, required=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, source in sources.items():
                (root / name).write_text(source, encoding='utf-8')
            manifest = root / 'required.json'
            manifest.write_text(json.dumps(required or {'test_owner': ['Owner.test_required']}))
            return subprocess.run(
                [sys.executable, str(ROOT / 'scripts/ci_collection.py'),
                 '--tests', str(root), '--required', str(manifest)],
                cwd=ROOT, capture_output=True, text=True, timeout=20,
            )

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
                 '--tests', str(root), '--required', str(manifest), '--output', str(output)],
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
