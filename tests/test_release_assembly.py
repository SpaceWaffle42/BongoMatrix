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
        loader = unittest.TestLoader()
        names = list(ids(loader.discover(str(root / 'tests'), top_level_dir=str(root))))
        self.assertFalse(loader.errors)
        required = json.loads((root / 'ci/required-tests.json').read_text())
        qualified = {f'tests.{module}.{name}' for module, methods in required.items()
                     for name in methods if name.rsplit('.', 1)[-1] in MISSING}
        self.assertEqual({name.rsplit('.', 1)[-1] for name in qualified}, MISSING)
        self.assertTrue(qualified <= set(names), sorted(qualified - set(names)))
        self.assertEqual(len(names), len(set(names)))

    def test_projection_has_only_three_owning_tests(self):
        # Historical method name retained as a required behavior ID, not a total.
        names = list(ids(unittest.TestLoader().loadTestsFromModule(projection)))
        required = json.loads((Path(__file__).resolve().parents[1] / 'ci/required-tests.json').read_text())
        self.assertTrue({name.removeprefix(projection.__name__ + '.') for name in names} >= set(required['test_release_projection']))
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(name.startswith(projection.__name__ + '.ProjectionTests.') for name in names))
        from tests import test_webapp
        original = list(ids(unittest.TestLoader().loadTestsFromModule(test_webapp)))
        self.assertTrue({name.removeprefix(test_webapp.__name__ + '.') for name in original} >= set(required['test_webapp']))
        self.assertEqual(len(original), len(set(original)))
        self.assertTrue(all(name.startswith(test_webapp.__name__ + '.') for name in original))

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
