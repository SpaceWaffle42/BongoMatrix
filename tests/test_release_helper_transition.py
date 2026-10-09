"""Native confined preparation/source/state lifecycle; service double, not PID1/production acceptance."""
import copy
import http.server
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
OLD_HEAD = '9315292170e9ee14a28c2c3c228bc05e981de9e4'
OLD_SHA = '118d435da056b880f40dbcb4a3f0e3c1978c6e422f6508102edfd00b63e5b0e0'
NEW_SHA = '6d1a98df08809b5292e85bd968f7f3b2582a189a854520ce636dc1018efca755'
PRIOR = {
    'deploy/release_bundle.py': {'sha256': OLD_SHA, 'size': 108049, 'uid': 1000, 'gid': 985, 'mode': '0664'},
    'release_argparse.py': {'sha256': '090e98340173d99605f9701c290e78cafc21da49e072dfd5242786685ee5a5f1', 'size': 1043, 'uid': 1000, 'gid': 985, 'mode': '0664'},
}

def load(path):
    spec = importlib.util.spec_from_file_location('transition_operator', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class Redirect(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302); self.send_header('Location', '/pair'); self.end_headers()
    def log_message(self, format, *args): pass

class Services:
    """Only service lifecycle is simulated; real HTTP, package and private-state checks."""
    def __init__(self, op, binding):
        self.op, self.target = op, binding['target']
        self.phase, self.contained, self.active = 'initial', False, True
    def validate(self): assert self.active
    def contain(self): self.contained = True
    def stop(self): self.active = False
    def start(self): self.active = True
    def prove_exclusion(self): assert self.contained
    def release(self): self.contained = False
    def readiness(self, timeout):
        assert self.active
        return self.op.probe_http(timeout, fixture_port=self.target['port'])

class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='matrix-transition-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.controls = self.base/'controls'; self.controls.mkdir(mode=0o700)
        source_operator = Path(os.environ.get('MATRIX_CUSTODY_OPERATOR', str(ROOT/'release-controls/r11b-prod-rectify/scoped_release_operator.py')))
        for name in ('scoped_release_operator.py', 'release_bundle.py', 'release_failure_custody.py'):
            (self.controls/name).write_bytes(source_operator.with_name(name).read_bytes())
        (self.controls/'release').mkdir(mode=0o700)
        (self.controls/'release/release_argparse.py').write_bytes((ROOT/'release_argparse.py').read_bytes())
        self.op = load(self.controls/'scoped_release_operator.py')
        paths = set(subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '-z']).decode().strip('\0').split('\0'))
        paths.add('tests/test_release_helper_transition.py')
        files = [{'path': p, 'sha256': self.op.sha((ROOT/p).read_bytes()), 'size': (ROOT/p).stat().st_size,
                  'mode': oct(stat.S_IMODE((ROOT/p).stat().st_mode))} for p in sorted(paths)]
        head = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD']).decode().strip()
        digest = self.op.sha(self.op.encoded({'head': head, 'files': files}))
        self.manifest = {'head': head, 'files': files, 'source_identity': digest}
        self.op.publish(self.controls/'successor-source-pin.json', {'generation': 'r11b-prod-rectify', 'head': head,
                        'source_identity': digest, 'source_count': len(files)})
        self.destination = self.base/'matrix-release-proof-transition'; self.destination.mkdir(mode=0o750)
        self.destination.chmod(0o750)
        for e in files:
            if self.op.is_runtime(e['path']) and e['path'] not in self.op.ABSENT_ADDITIONS:
                p = self.destination/e['path']; p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes((ROOT/e['path']).read_bytes()); p.chmod(0o644)
        old = subprocess.check_output(['git', '-C', str(ROOT), 'show', OLD_HEAD+':deploy/release_bundle.py'])
        self.assertEqual(self.op.sha(old), OLD_SHA); self.assertEqual(len(old), 108049)
        self.old = old
        for relative, prior in PRIOR.items():
            p = self.destination/relative; p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(old if relative == 'deploy/release_bundle.py' else (ROOT/relative).read_bytes())
            os.chown(p, prior['uid'], prior['gid']); p.chmod(int(prior['mode'], 8))
        for relative in self.op.MUTABLE_ROOTS:
            p = self.destination/relative; p.mkdir(parents=True, mode=0o750)
            (p/'opaque').write_bytes(b'private fixture bytes\x00\xff'); (p/'opaque').chmod(0o640)
        self.state = {r: self.op.tree_snapshot(self.destination/r) for r in self.op.MUTABLE_ROOTS}
        delta = []
        for relative in self.op.RUNTIME_PATHS:
            p = self.destination/relative
            prior = self.op.record(*self.op.read_at(self.destination, relative)) if p.exists() else {'absent': True}
            delta.append({'path': relative, 'sha256': self.op.sha((ROOT/relative).read_bytes()), 'predecessor': prior})
        self.transition = {'version': 1, 'retained_binding_sha256': '80d9c488f0d079b12a18b0859d7272ab095629fd019da6adb2b9ad6a33e06937',
                           'predecessor': copy.deepcopy(PRIOR), 'candidate': dict(self.op.HELPER_HASHES),
                           'prepare_roles': ['initial', 'final'], 'rollback_role': 'predecessor'}
        self.route = {'source_plan': {'runtime_delta': delta, 'operational_package_entries': [
            {'path': p, 'sha256': h} for p,h in self.op.HELPER_HASHES.items()], 'helper_transition': self.transition}}
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.thread.join, 2); self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.stage = self.base/'stage'

    def prepare(self, stage=None, source=None):
        return self.op.prepare_package(source or ROOT, self.manifest, self.route, self.destination, stage or self.stage,
                expected_identity=self.manifest['source_identity'], fixture_unit='matrix-release-proof-transition.service',
                fixture_port=self.server.server_port)

    def receipt(self, stage, binding, services, context, *, fail_source=False):
        if services.phase == 'rollback':
            self.op.assert_binding(stage, binding, predecessor=True)
        else:
            self.op.primitives().verify_release(*self.op.bundle_args(stage, binding), mode='deploy')
        current = {r:self.op.tree_snapshot(self.destination/r) for r in self.op.MUTABLE_ROOTS}
        self.assertEqual(current, self.state)
        ready = services.readiness(30)
        if fail_source:
            # Exercise actual opaque-state restoration, including extra-file removal.
            (self.destination/'data/opaque').write_bytes(b'changed private fixture')
            (self.destination/'resources/media/extra').write_bytes(b'new private fixture')
        return {**context, 'accepted': True, 'acceptedScope': 'disposable-pid1-fixture-only',
                'phase': services.phase, 'target': services.target,
                'checks': {'fixture_http': ready['status'] == 302, 'fixture_source': not fail_source, 'fixture_state': current == self.state},
                'evidence_sha256': self.op.sha(self.op.encoded({'state': current, 'http': ready, 'scope': 'unit-service-double-not-pid1'}))}

    def test_native_prepare_retained_predecessor(self):
        binding = self.prepare()
        self.assertEqual(binding['predecessor']['deploy/release_bundle.py'], PRIOR['deploy/release_bundle.py'])
        self.assertEqual(self.op.sha((self.stage/'release/deploy/release_bundle.py').read_bytes()), NEW_SHA)
        self.op.assert_binding(self.stage, binding, predecessor=True)

    def test_native_cli_prepare_full_and_scoped(self):
        for scoped in (False, True):
            stage = self.base/('cli-scoped' if scoped else 'cli-full')
            source = ROOT
            if scoped:
                source = self.base/'export'
                self.op.export_source(ROOT, self.manifest, self.route, source, expected_identity=self.manifest['source_identity'])
                source = source/'release'
            self.op.publish(self.base/'manifest.json', self.manifest); self.op.publish(self.base/'route.json', self.route)
            args = ['python3', '-I', '-B', str(self.controls/'scoped_release_operator.py'), 'prepare', '--source', str(source),
                    '--manifest', str(self.base/'manifest.json'), '--route', str(self.base/'route.json'), '--destination', str(self.destination),
                    '--stage', str(stage), '--reviewed-head', self.manifest['head'], '--source-identity', self.manifest['source_identity'],
                    '--fixture-unit', 'matrix-release-proof-transition.service', '--fixture-port', str(self.server.server_port)]
            if scoped: args.append('--scoped')
            done = subprocess.run(args, capture_output=True)
            self.assertEqual(done.returncode, 0, done.stderr.decode())
            self.op.assert_binding(stage, self.op.load_json(stage/'binding.json'), predecessor=True)

    def test_unknown_current_and_already_candidate_rejected(self):
        p = self.destination/'deploy/release_bundle.py'
        for data in (b'unknown helper', (ROOT/'deploy/release_bundle.py').read_bytes()):
            p.write_bytes(data)
            with self.assertRaises(self.op.OperatorError): self.prepare()
            self.assertFalse(self.stage.exists())

    def test_unknown_candidate_rejected_without_hash_patch(self):
        source = self.base/'bad-source'
        self.op.export_source(ROOT, self.manifest, self.route, source, expected_identity=self.manifest['source_identity'])
        p = source/'release/deploy/release_bundle.py'; p.chmod(0o600); p.write_bytes(b'unknown candidate')
        with self.assertRaises(self.op.OperatorError):
            self.op.prepare_package(source/'release', self.manifest, self.route, self.destination, self.stage, scoped=True,
                expected_identity=self.manifest['source_identity'], fixture_unit='matrix-release-proof-transition.service', fixture_port=self.server.server_port)
        self.assertFalse(self.stage.exists())

    def test_metadata_mismatch_rejected(self):
        for relative in PRIOR:
            p = self.destination/relative; p.chmod(0o644)
            with self.assertRaises(self.op.OperatorError): self.prepare()
            self.assertFalse(self.stage.exists()); p.chmod(0o664)

    def test_route_pair_cross_binding_rejected(self):
        for key in ('predecessor', 'candidate', 'prepare_roles', 'retained_binding_sha256'):
            self.route['source_plan']['helper_transition'] = copy.deepcopy(self.transition)
            pair = self.route['source_plan']['helper_transition']
            if key == 'predecessor': pair[key]['deploy/release_bundle.py']['sha256'] = NEW_SHA
            elif key == 'candidate': pair[key]['deploy/release_bundle.py'] = OLD_SHA
            elif key == 'prepare_roles': pair[key] = ['rollback']
            else: pair[key] = '0'*64
            with self.assertRaises(self.op.OperatorError): self.prepare()
            self.assertFalse(self.stage.exists())

    def test_exact_source_pin_missing_or_mismatched_rejected(self):
        pin = self.controls/'successor-source-pin.json'
        value = self.op.load_json(pin); pin.unlink()
        with self.assertRaises(self.op.OperatorError): self.prepare()
        value['source_identity'] = '0'*64; self.op.publish(pin, value)
        with self.assertRaises(self.op.OperatorError): self.prepare()
        self.assertFalse(self.stage.exists())

    def test_prepared_pair_source_root_cross_binding_rejected(self):
        binding = self.prepare()
        for key in ('source_identity', 'route_sha256', 'roots', 'predecessor', 'helper_transition'):
            bad = copy.deepcopy(binding)
            if key == 'roots': bad[key]['.']['ino'] += 1
            elif key == 'predecessor': bad[key]['deploy/release_bundle.py']['sha256'] = NEW_SHA
            elif key == 'helper_transition': bad[key]['plan']['prepare_roles'] = ['rollback']
            else: bad[key] = '0'*64
            with self.assertRaises(self.op.OperatorError): self.op.assert_binding(self.stage, bad, predecessor=True)

    def test_mixed_helper_and_owner_metadata_rejected(self):
        p = self.destination/'release_argparse.py'; original = p.read_bytes(); p.write_bytes(b'unknown dependency')
        with self.assertRaises(self.op.OperatorError): self.prepare()
        p.write_bytes(original)
        for uid,gid in ((0,985),(1000,0)):
            os.chown(p,uid,gid)
            with self.assertRaises(self.op.OperatorError): self.prepare()
        self.assertFalse(self.stage.exists())

    def test_root_and_predecessor_drift_rejected(self):
        binding = self.prepare()
        self.destination.chmod(0o700)
        with self.assertRaises(self.op.OperatorError): self.op.assert_binding(self.stage, binding, predecessor=True)
        self.destination.chmod(0o750)
        p = self.destination/'deploy/release_bundle.py'; p.chmod(0o644)
        with self.assertRaises(self.op.OperatorError): self.op.assert_binding(self.stage, binding, predecessor=True)

    def test_phase_mismatch_rejected_before_mutation(self):
        binding = self.prepare(); services = Services(self.op, binding); services.phase = 'rollback'
        with self.assertRaises(self.op.OperatorError): self.op.execute(self.stage, services, lambda c: self.receipt(self.stage,binding,services,c))
        self.assertEqual((self.destination/'deploy/release_bundle.py').read_bytes(), self.old)
        self.assertFalse((self.stage/'bundle').exists())

    def test_false_source_receipt_recovers_exact_predecessor_and_private_metadata(self):
        binding = self.prepare(); services = Services(self.op, binding)
        with self.assertRaises(self.op.OperatorError):
            self.op.execute(self.stage,services,lambda c:self.receipt(self.stage,binding,services,c,fail_source=True),keep_containment=True)
        self.op.assert_binding(self.stage,binding,predecessor=True)
        self.assertEqual(self.op.load_json(self.stage/'run.json')['outcome'],'recovered')
        self.assertEqual({r:self.op.tree_snapshot(self.destination/r) for r in self.op.MUTABLE_ROOTS},self.state)

    def test_native_backup_deploy_rollback_same_package_final(self):
        binding = self.prepare(); services = Services(self.op,binding)
        run = self.op.execute(self.stage,services,lambda c:self.receipt(self.stage,binding,services,c),keep_containment=True)
        self.assertEqual(run['outcome'],'committed')
        self.assertEqual(self.op.sha((self.destination/'deploy/release_bundle.py').read_bytes()),NEW_SHA)
        backup_files = [p for p in (self.stage/'bundle').rglob('*') if p.is_file() and p.read_bytes()==self.old]
        self.assertTrue(backup_files,'native backup must contain exact retained old helper')
        services.phase = 'rollback'
        rollback = self.op.rollback_proof(self.stage,services,lambda c:self.receipt(self.stage,binding,services,c))
        self.assertTrue(rollback['rollback_accepted'])
        self.assertEqual(self.op.record(*self.op.read_at(self.destination,'deploy/release_bundle.py')),PRIOR['deploy/release_bundle.py'])
        final = self.base/'final'; final_binding = self.prepare(final)
        self.assertEqual(final_binding['spec_sha256'],binding['spec_sha256'])
        self.assertEqual(final_binding['source_identity'],binding['source_identity'])
        self.op.require_final_origin(self.stage)
        caller = ROOT/'deploy/feature_release_acceptance.py'
        for stage,phase,origin in ((self.stage,'rollback',None),(final,'final',str(self.stage))):
            (stage/'scoped_release_operator.py').write_bytes((self.controls/'scoped_release_operator.py').read_bytes())
            self.op.publish(stage/('request-'+phase+'.json'), {
                'stage':str(stage),'phase':phase,'origin_stage':origin,
                'binding_sha256':self.op.sha((stage/'binding.json').read_bytes()),
                'operator_sha256':self.op.sha((self.controls/'scoped_release_operator.py').read_bytes()),
                'acceptance_program':str(caller),'acceptance_sha256':self.op.sha(caller.read_bytes())})
        self.op.register_related_final(self.stage,final)
        descriptor = self.op.related_final_descriptor(self.stage,final)
        self.assertEqual(descriptor['source_identity'],binding['source_identity'])
        self.assertEqual(descriptor['spec_sha256'],binding['spec_sha256'])
        final_services = Services(self.op,final_binding); final_services.phase='final'
        result = self.op.execute(final,final_services,lambda c:self.receipt(final,final_binding,final_services,c))
        self.assertEqual(result['outcome'],'committed')
        self.assertEqual(self.op.sha((self.destination/'deploy/release_bundle.py').read_bytes()),NEW_SHA)
        self.assertEqual({r:self.op.tree_snapshot(self.destination/r) for r in self.op.MUTABLE_ROOTS},self.state)

if __name__ == '__main__': unittest.main()
