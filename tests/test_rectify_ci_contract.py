"""Finding-bound CI isolation and exact release-only ownership regressions."""
from pathlib import Path
import subprocess
import sys
import unittest
ROOT = Path(__file__).resolve().parents[1]
CONTROLS = ("release-controls/r11b-prod-rectify/release_bundle.py", "release-controls/r11b-prod-rectify/release_failure_custody.py", "release-controls/r11b-prod-rectify/scoped_release_operator.py")
class RectifyCIContractTests(unittest.TestCase):
    def test_preview_import_does_not_change_process_umask(self):
        code = "import os; os.umask(0o022); import tests.playwright_rectify_preview; print(oct(os.umask(0o022)))"
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "0o22")
    def test_exact_declared_controls_are_not_runtime_but_new_runtime_is(self):
        from deploy import release_bundle
        for path in CONTROLS:
            with self.subTest(path=path): self.assertFalse(release_bundle._is_production_export_path(path))
        for path in ("new_runtime.py", "app/new_runtime.py", "modules/new_runtime.py", "release-controls/r11b-prod-rectify/new_runtime.py", "deploy/release_bundle.py", "release_argparse.py"):
            with self.subTest(path=path): self.assertTrue(release_bundle._is_production_export_path(path))
