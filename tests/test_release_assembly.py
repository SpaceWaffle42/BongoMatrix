"""Finding-bound assembly regressions; collection only, no WebApp execution."""
import json
import unittest
from pathlib import Path
from tests import test_release_projection as projection

MISSING = {
    'test_reject_boolean_only', 'test_reject_missing_actual_browser_assertion',
    'test_reject_missing_actual_frame', 'test_backend_failure_rejected',
    'test_candidate_cannot_use_rollback_expectations', 'test_expected_decoded_frames',
    'test_wrong_pixels', 'test_nonadvancing_revision', 'test_invalid_dimensions',
    'test_decode_failure',
}

def ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from ids(item)
        else:
            yield item.id()

class AssemblyTests(unittest.TestCase):
    def test_missing_controls_discoverable(self):
        root = Path(__file__).resolve().parents[1]
        names = list(ids(unittest.defaultTestLoader.discover(str(root / 'tests'), top_level_dir=str(root))))
        self.assertFalse(unittest.defaultTestLoader.errors)
        self.assertTrue(MISSING <= {name.rsplit('.', 1)[-1] for name in names}, sorted(MISSING - {name.rsplit('.', 1)[-1] for name in names}))
        self.assertEqual(len(names), len(set(names)))

    def test_projection_has_only_three_owning_tests(self):
        names = list(ids(unittest.defaultTestLoader.loadTestsFromModule(projection)))
        self.assertEqual(len(names), 3)
        self.assertTrue(all('.ProjectionTests.' in name for name in names))
        from tests import test_webapp
        original = list(ids(unittest.defaultTestLoader.loadTestsFromModule(test_webapp)))
        self.assertEqual(sum('.WebAppTests.' in name for name in original), 442)

    def test_binding_keeps_real_popen_and_exact_executable_seam(self):
        from tests import release_browser_binding as binding
        from unittest.mock import patch
        executable = binding.browser_executable()
        self.assertTrue(Path(executable).is_file())
        real = binding.subprocess.Popen
        child = binding.bound_popen(real, ['node', '--version'], stdout=binding.subprocess.PIPE, start_new_session=True)
        output, _ = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0)
        self.assertTrue(output.startswith(b'v'))
        seen = []
        def record(argv, **kw):
            seen.append((argv, kw))
            return 'seam-only'
        env = {'binding': 'unchanged'}
        with patch.object(binding, 'browser_executable', return_value=executable):
            result = binding.bound_popen(record, ['/opt/google/chrome/chrome', '--headless'], env=env, start_new_session=True)
        self.assertEqual(result, 'seam-only')
        self.assertEqual(seen, [([executable, '--headless'], {'env': env, 'start_new_session': True})])
        self.assertEqual(seen[0][1]['env'], env)
        seen.clear()
        binding.bound_popen(record, ['node', 'unchanged.mjs'], env=env)
        self.assertEqual(seen, [(['node', 'unchanged.mjs'], {'env': env})])
