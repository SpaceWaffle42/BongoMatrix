"""Offline actual exported operator seams; never production/PID1 acceptance."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
def module(name, path):
    spec=importlib.util.spec_from_file_location(name,path)
    assert spec and spec.loader
    value=importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value
op=module('custody_operator',Path(os.environ.get('MATRIX_CUSTODY_OPERATOR',str(ROOT/'release-controls/r11b-prod-rectify/scoped_release_operator.py'))))
caller=module('custody_caller',Path(os.environ.get('MATRIX_CUSTODY_CALLER',str(ROOT/'deploy/feature_release_acceptance.py'))))
SECRET='PRIVATE_PIN_COOKIE_MEDIA_SENTINEL'
class CustodyTests(unittest.TestCase):
    def test_execute_retains_primary_before_successful_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage=Path(tmp); binding={'source_identity':'b'*64,'spec_sha256':'c'*64}
            (stage/'binding.json').write_text(json.dumps(binding))
            services=SimpleNamespace(phase='initial',validate=lambda:None,contain=lambda:(_ for _ in ()).throw(ValueError(SECRET)))
            def recovery(stage,services,run,binding):
                op.journal(stage,run,'recovered',outcome='recovered')
            with patch.object(op,'assert_binding'),patch.object(op,'assert_service_target'),patch.object(op,'recover_locked',side_effect=recovery):
                with self.assertRaises(ValueError):op.execute(stage,services,None)
            paths=list(stage.glob('diagnostic-primary-initial.json'))
            self.assertEqual(len(paths),1,'actual execute lost primary custody')
            record=json.loads(paths[0].read_bytes())
            self.assertEqual(record['stage_name'],'OPERATOR_EXECUTE')
            self.assertEqual(record['exception_class'],'ValueError')
            self.assertFalse(record['acceptance']);self.assertNotIn(SECRET,paths[0].read_text())
            self.assertEqual(json.loads((stage/'run.json').read_text())['outcome'],'recovered')
    def test_primary_failed_recovery_then_stoppost_replay_retains_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage=Path(tmp); binding={'source_identity':'b'*64,'spec_sha256':'c'*64}
            (stage/'binding.json').write_text(json.dumps(binding))
            services=SimpleNamespace(phase='initial',validate=lambda:None,contain=lambda:(_ for _ in ()).throw(ValueError(SECRET)))
            def failed(stage,services,run,binding):
                op.journal(stage,run,'recovering-state')
                raise RuntimeError(SECRET)
            with patch.object(op,'assert_binding'),patch.object(op,'assert_service_target'),patch.object(op,'recover_locked',side_effect=failed):
                with self.assertRaises(ValueError,msg='recovery replaced primary exception'):op.execute(stage,services,None)
            primary=(stage/'diagnostic-primary-initial.json').read_bytes()
            recovery=(stage/'diagnostic-recovery-inline-initial.json').read_bytes()
            self.assertEqual(json.loads(recovery)['exception_class'],'RuntimeError')
            with patch.object(op,'recover_locked',side_effect=failed):
                with self.assertRaises(RuntimeError):op.recover(stage,services)
            stoppost=(stage/'diagnostic-recovery-stoppost-initial.json').read_bytes()
            def success(stage,services,run,binding):op.journal(stage,run,'recovered',outcome='recovered')
            with patch.object(op,'recover_locked',side_effect=success):
                op.recover(stage,services);op.recover(stage,services)
            self.assertEqual((stage/'diagnostic-primary-initial.json').read_bytes(),primary)
            self.assertEqual((stage/'diagnostic-recovery-inline-initial.json').read_bytes(),recovery)
            self.assertEqual((stage/'diagnostic-recovery-stoppost-initial.json').read_bytes(),stoppost)
            self.assertNotIn(SECRET.encode(),primary+recovery+stoppost)
            self.assertNotEqual(json.loads((stage/'run.json').read_bytes())['outcome'],'committed')
    def test_actual_launcher_readback_retains_child_not_transport_cause(self):
        import contextlib,io
        with tempfile.TemporaryDirectory() as tmp:
            stage=Path(tmp);(stage/'binding.json').write_text(json.dumps({'source_identity':'b'*64,'spec_sha256':'c'*64}))
            (stage/'scoped_release_operator.py').write_bytes(Path(op.__file__).read_bytes())
            program=stage/'caller.py';program.write_text('# offline caller')
            original_open=op.os.open
            def local_open(path,*args,**kwargs):
                if str(path)=='/run/lock/matrix-scoped-release.lock':path=stage/'global.lock'
                return original_open(path,*args,**kwargs)
            def child(*args,**kwargs):
                op.retain_diagnostic(stage,'initial','primary','OPERATOR_EXECUTE',ValueError(SECRET))
                raise subprocess.CalledProcessError(2,['SECRET_ARGV'],stderr=SECRET)
            output=io.StringIO()
            with patch.object(op,'SystemServices'),patch.object(op.os,'open',side_effect=local_open),patch.object(op.subprocess,'run',side_effect=child),contextlib.redirect_stdout(output):
                with self.assertRaises(subprocess.CalledProcessError):
                    op.main(['launch','--stage',str(stage),'--phase','initial','--acceptance-program',str(program),'--acceptance-sha256',op.sha(program.read_bytes())])
            self.assertTrue((stage/'diagnostic-launcher-initial.json').exists(),'launcher lost child readback')
            self.assertIn('OPERATOR_EXECUTE',output.getvalue())
            self.assertNotIn(SECRET,output.getvalue())
    def target_fixture(self,stage,fail):
        from contextlib import ExitStack
        from tests.test_release_caller_contract import proof
        context={'stage':str(stage),'phase':'initial','app_pid':123,'binding_sha256':'a'*64,'source_identity':'b'*64,'spec_sha256':'c'*64}
        (stage/'caller-driver.json').write_text(json.dumps({'sha256':'e'*64}))
        original_read=caller.read_private; original_publish=caller.publish_private
        def read(path,*args):
            if str(path).endswith('/webapp.py'):return b'APPLICATION_VERSION = "0.3.66-preview"\n'
            return original_read(path,*args)
        def publish(path,value):
            if Path(path).name=='caller-request-initial.json':
                original_publish(path,value)
                if fail!='missing':
                    p=proof();p.update({k:value[k] for k in caller.IDENTITIES});p['version_visible']=value['version'];p['driver_sha256']='e'*64
                    if fail=='validation':p['offline']=True
                    original_publish(stage/'caller-receipt-initial.json',p)
            elif Path(path).name=='caller-proof-initial.json' and fail=='publication':raise OSError(SECRET)
            else:original_publish(path,value)
        calls={'namespace':0,'source':0,'state':0}
        def checked(kind):
            calls[kind]+=1
            if calls[kind]==2 and fail==kind:raise RuntimeError(SECRET)
            return {'offline_fixture':True} if kind=='namespace' else {}
        with ExitStack() as stack:
            for name,value in [('validate_request',lambda c:({'destination':str(stage)},{})),('namespace_identity',lambda *a,**k:checked('namespace')),('verify_sources',lambda *a:checked('source')),('state_evidence',lambda *a:checked('state')),('read_private',read),('publish_private',publish),('native_driver',lambda p:(_ for _ in ()).throw(RuntimeError(SECRET)) if fail=='native' else {'offline_fixture':True})]:
                stack.enter_context(patch.object(caller,name,side_effect=value))
            with self.assertRaises((ValueError,RuntimeError,OSError)):
                caller.target(context,wait_seconds=0 if fail=='missing' else 1)
        return context
    def test_actual_target_stages_missing_receipt_native_and_publication(self):
        for failure,expected in [('missing','TARGET_RECEIPT_WAIT'),('validation','TARGET_RECEIPT_VALIDATE'),('native','TARGET_NATIVE_DRIVER'),('publication','TARGET_PROOF_PUBLISH'),('namespace','TARGET_PROCESS_RECHECK'),('source','TARGET_SOURCE_RECHECK'),('state','TARGET_STATE_RECHECK')]:
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as tmp:
                stage=Path(tmp);self.target_fixture(stage,failure)
                path=stage/'diagnostic-target-initial.json'
                self.assertTrue(path.exists(),'actual target lost failing stage')
                value=json.loads(path.read_bytes());self.assertEqual(value['stage_name'],expected)
                self.assertFalse(value['acceptance']);self.assertNotIn(SECRET,path.read_text())
                self.assertFalse((stage/'caller-proof-initial.json').exists())
    def test_caller_acceptance_retains_valid_target_diagnostic_and_rejects_malformed(self):
        for malformed in (False,True):
            with self.subTest(malformed=malformed),tempfile.TemporaryDirectory() as tmp:
                stage=Path(tmp);binding={'source_identity':'b'*64,'spec_sha256':'c'*64}
                (stage/'binding.json').write_text(json.dumps(binding))
                program=stage/'caller.py';program.write_text('# offline caller')
                (stage/'request-initial.json').write_text(json.dumps({'acceptance_program':str(program),'acceptance_sha256':op.sha(program.read_bytes())}))
                services=SimpleNamespace(target={'mode':'production'},properties=lambda:{'MainPID':'123'},namespace_command=lambda argv:argv)
                context=op.diagnostic_context(stage,'initial');context['app_pid']=123
                if malformed:(stage/'diagnostic-target-initial.json').write_text(json.dumps({'message':SECRET}))
                else:op.failure_custody().retain(context,'target','TARGET_NATIVE_DRIVER',RuntimeError(SECRET),2)
                with patch.object(op.subprocess,'run',return_value=SimpleNamespace(returncode=2,stdout=b'',stderr=SECRET.encode())):
                    with self.assertRaises(ValueError):op.caller_acceptance(stage,services,'initial')({k:context[k] for k in ('stage','binding_sha256','source_identity','spec_sha256')})
                record=json.loads((stage/'diagnostic-caller-initial.json').read_bytes())
                self.assertEqual(record['stage_name'],'OPERATOR_CALLER' if malformed else 'TARGET_NATIVE_DRIVER')
                self.assertFalse(record['acceptance']);self.assertNotIn(SECRET,json.dumps(record))
                self.assertFalse((stage/'caller-proof-initial.json').exists())
    def test_persistence_failure_does_not_skip_execute_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage=Path(tmp);(stage/'binding.json').write_text(json.dumps({'source_identity':'b'*64,'spec_sha256':'c'*64}))
            services=SimpleNamespace(phase='initial',validate=lambda:None,contain=lambda:(_ for _ in ()).throw(ValueError(SECRET)))
            # A malformed existing first-failure file must not be reflected or overwritten.
            (stage/'diagnostic-primary-initial.json').write_text(json.dumps({'message':SECRET}))
            with patch.object(op,'assert_binding'),patch.object(op,'assert_service_target'),patch.object(op,'recover_locked') as recovery:
                with self.assertRaises(ValueError):op.execute(stage,services,None)
            recovery.assert_called_once()
            self.assertNotEqual(json.loads((stage/'run.json').read_bytes())['outcome'],'committed')
            with patch.object(op,'failure_custody',side_effect=OSError(SECRET)),patch.object(op,'assert_binding'),patch.object(op,'assert_service_target'),patch.object(op,'recover_locked') as recovery:
                (stage/'run.json').unlink()
                with self.assertRaises(ValueError):op.execute(stage,services,None)
            recovery.assert_called_once()
    def test_successor_full_source_export_requires_distinct_exact_pin(self):
        import os,stat
        allow=['tests/playwright_rectify_preview.py','tests/preview_request_contract.py','tests/test_preview_request_contract.py','tests/test_release_failure_custody.py','deploy/release_failure_custody.py','release-controls/r11b-prod-rectify/scoped_release_operator.py','release-controls/r11b-prod-rectify/release_bundle.py','release-controls/r11b-prod-rectify/release_failure_custody.py']
        paths=set(subprocess.check_output(['git','-C',str(ROOT),'ls-files','-z']).decode().strip('\0').split('\0'))|set(allow)
        files=[{'path':p,'sha256':op.sha((ROOT/p).read_bytes()),'size':(ROOT/p).stat().st_size,'mode':oct(stat.S_IMODE((ROOT/p).stat().st_mode))} for p in sorted(paths)]
        head=subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD']).decode().strip()
        digest=op.sha(op.encoded({'head':head,'files':files}));manifest={'head':head,'files':files,'source_identity':digest}
        selected=set(op.RUNTIME_PATHS)|set(op.HELPER_HASHES)
        route={'source_plan':{'runtime_delta':[e for e in files if e['path'] in op.RUNTIME_PATHS],'operational_package_entries':[e for e in files if e['path'] in op.HELPER_HASHES]}}
        with tempfile.TemporaryDirectory() as tmp:
            controls=Path(tmp);(controls/'release_bundle.py').write_bytes((ROOT/'deploy/release_bundle.py').read_bytes())
            pin={'generation':'r11b-prod-rectify','head':head,'source_identity':digest,'source_count':len(files)}
            (controls/'successor-source-pin.json').write_text(json.dumps(pin))
            with patch.object(op,'__file__',str(controls/'scoped_release_operator.py')):
                output=controls/'export';op.export_source(ROOT,manifest,route,output,expected_identity=digest)
                exported=json.loads((output/'export.json').read_bytes())
                self.assertEqual(exported['verified_entries'],len(files));self.assertEqual(exported['payload_entries'],17)
                self.assertEqual(set(exported['payload']),selected)
                pin['source_identity']='0'*64;(controls/'successor-source-pin.json').write_text(json.dumps(pin))
                with self.assertRaises(ValueError):op.export_source(ROOT,manifest,route,controls/'rejected',expected_identity=digest)
    def test_sanitized_schema_identity_and_real_write_failure(self):
        import contextlib,io,copy
        custody=op.failure_custody()
        with tempfile.TemporaryDirectory() as tmp:
            context={'stage':tmp,'phase':'initial','binding_sha256':'a'*64}
            value=custody.record(context,'OPERATOR_EXECUTE',ValueError(SECRET),2)
            for key,bad in [('message',SECRET),('acceptance',True),('exception_class',SECRET),('site',{'file':SECRET,'function':'execute','line':1}),('identity',dict(context,binding_sha256='b'*64)),('exit_code',True)]:
                changed=copy.deepcopy(value);changed[key]=bad
                with self.subTest(key=key),self.assertRaises((ValueError,TypeError)):custody.validate(changed,context)
            stream=io.StringIO()
            with patch.object(custody.os,'open',side_effect=PermissionError(SECRET)),contextlib.redirect_stderr(stream):
                self.assertIsNone(custody.retain(context,'primary','OPERATOR_EXECUTE',ValueError(SECRET)))
            self.assertIn('UNAVAILABLE',stream.getvalue());self.assertNotIn(SECRET,stream.getvalue())
            self.assertFalse(list(Path(tmp).glob('diagnostic-*')))
    def test_later_receipt_cannot_replace_failed_target_with_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage=Path(tmp);self.target_fixture(stage,'missing')
            diagnostic=(stage/'diagnostic-target-initial.json').read_bytes()
            (stage/'caller-receipt-initial.json').write_text('{}')
            self.assertEqual((stage/'diagnostic-target-initial.json').read_bytes(),diagnostic)
            self.assertFalse((stage/'caller-proof-initial.json').exists())
if __name__=='__main__':unittest.main()
