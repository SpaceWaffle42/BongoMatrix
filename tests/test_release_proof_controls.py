"""Original pure proof controls, relocated without assertion changes."""
import unittest
from tests.test_release_caller_contract import caller, request, proof

class CallerContract(unittest.TestCase):
    def test_reject_boolean_only(self):
        with self.assertRaises(ValueError):
            caller.validate_browser_proof(request(), {'accepted':True})


    def test_reject_missing_actual_browser_assertion(self):
        p = proof(); del p['roles']['splash']['library_native']
        with self.assertRaises(ValueError): caller.validate_browser_proof(request(), p)


    def test_reject_missing_actual_frame(self):
        p = proof(); del p['roles']['sleep']['frame_pixels_sha256']
        with self.assertRaises(ValueError): caller.validate_browser_proof(request(), p)


    def test_backend_failure_rejected(self):
        p=proof(); p['roles']['splash']['native_http']=503
        with self.assertRaises(ValueError): caller.validate_browser_proof(request(),p)


    def test_candidate_cannot_use_rollback_expectations(self):
        p=proof(); p['roles']={}; p['predecessor_dashboard']=True
        with self.assertRaises(ValueError): caller.validate_browser_proof(request(),p)
