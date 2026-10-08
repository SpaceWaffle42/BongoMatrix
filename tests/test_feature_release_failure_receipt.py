"""R9 offline diagnostic contract; never device/production acceptance."""
import importlib.util,json,subprocess,unittest,tempfile,time
from pathlib import Path
from tests.release_browser_binding import provisioned_browser
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('failure_caller',ROOT/'deploy/feature_release_acceptance.py')
caller=importlib.util.module_from_spec(spec);spec.loader.exec_module(caller)
DRIVER=ROOT/'deploy/feature_release_browser.mjs'
class FailureReceiptTests(unittest.TestCase):
 def test_startup_failure_is_safe_json_not_input_reflection(self):
  sentinel='PRIVATE_PIN_COOKIE_MEDIA_SENTINEL'
  p=subprocess.run(['node',str(DRIVER)],input=('broken '+sentinel).encode(),capture_output=True,timeout=10)
  self.assertEqual(p.returncode,2)
  self.assertEqual(p.stdout,b'')
  self.assertNotIn(sentinel.encode(),p.stderr)
  self.assertTrue(p.stderr.startswith(b'{'), 'startup must enter safe diagnostic boundary, not raw Node stack')
  record=json.loads(p.stderr)
  self.assertEqual(record['stage'],'INPUT_PARSE')
  self.assertEqual(record['exception_class'],'SyntaxError')
  self.assertNotIn('message',record)
 def test_failed_child_retained_privately_not_success(self):
  # Real owned Chrome + real child error propagation, local-only no app contact.
  context=dict(phase='initial',app_pid=123,nonce='d'*64,binding_sha256='a'*64,source_identity='b'*64,spec_sha256='c'*64,driver_sha256=caller.sha(DRIVER.read_bytes()),caller_sha256=caller.sha((ROOT/'deploy/feature_release_acceptance.py').read_bytes()))
  with tempfile.TemporaryDirectory() as tmp:
   with provisioned_browser(caller) as children:
    with self.assertRaises(ValueError):caller.chrome_journey(context,'http://127.0.0.1:1',tmp,time.monotonic()+15,DRIVER)
   self.assertEqual(len(children),2)
   self.assertTrue(all(child.poll() is not None for child in children))
   paths=list(Path(tmp).glob('failure-*.json'))
   self.assertEqual(len(paths),1,'failed browser must retain safe diagnostic in host-private evidence')
   record=json.loads(paths[0].read_bytes());self.assertEqual(record['kind'],'MATRIX_HOST_FAILURE')
   self.assertEqual(record['request'],context)
   self.assertEqual(record['diagnostic']['stage'],'PAGE_READY')
   self.assertEqual(paths[0].stat().st_mode&0o777,0o600)
   self.assertFalse(list(Path(tmp).glob('browser-*')))
 def test_strict_schema_rejects_noise_and_privacy_payloads(self):
  p=subprocess.run(['node',str(DRIVER)],input=b'bad PRIVATE_PIN_COOKIE_MEDIA_SENTINEL',capture_output=True,timeout=10)
  good=json.loads(p.stderr);self.assertEqual(caller.validate_failure(p.stderr,'initial'),good)
  bad=[]
  for key,value in [('message','PRIVATE_PIN_COOKIE_MEDIA_SENTINEL'),('__proto__',{}),('session','PRIVATE_PIN_COOKIE_MEDIA_SENTINEL'),('argv',[]),('stage','FRAME_POLL\nSECRET'),('phase','foreign')]:
   d=dict(good);d[key]=value;bad.append(json.dumps(d).encode())
  for value in [float('nan'),float('inf'),-1,True,1.2,'PRIVATE_PIN_COOKIE_MEDIA_SENTINEL']:
   d=json.loads(p.stderr);d['frames']['last_revision']=value;bad.append(json.dumps(d).encode())
  for raw in [b'noise'+p.stderr,p.stderr+b'noise',b'x'*8193,p.stderr.replace(b'"schema":1',b'"schema":1,"schema":1'),b'[]']+bad:
   with self.subTest(payload_index=len(raw)):
    with self.assertRaises((ValueError,TypeError,KeyError)):caller.validate_failure(raw,'initial')
 def test_malformed_child_never_partially_retained(self):
  context=dict(phase='initial',app_pid=123,nonce='d'*64,binding_sha256='a'*64,source_identity='b'*64,spec_sha256='c'*64,caller_sha256=caller.sha((ROOT/'deploy/feature_release_acceptance.py').read_bytes()))
  from types import SimpleNamespace
  for raw in (b'noise PRIVATE_PIN_COOKIE_MEDIA_SENTINEL',b'x'*8193,b'{"stage":"FRAME_POLL","message":"PRIVATE_PIN_COOKIE_MEDIA_SENTINEL"}'):
   with tempfile.TemporaryDirectory() as tmp:
    context['driver_sha256']=caller.sha(DRIVER.read_bytes())
    caller.retain_failure(context,tmp,DRIVER,SimpleNamespace(pid=321,returncode=2),raw,'BROWSER_FAILED')
    data=(Path(tmp)/'failure-initial.json').read_bytes();self.assertNotIn(b'PRIVATE_PIN_COOKIE_MEDIA_SENTINEL',data)
    r=json.loads(data);self.assertIsNone(r['diagnostic']);self.assertEqual(r['classification'],'MALFORMED_BROWSER_FAILURE');self.assertFalse(r['acceptance'])
 def test_unauthorized_argv_and_oversized_input_are_rejected(self):
  for argv,data in [(['PRIVATE_PIN_COOKIE_MEDIA_SENTINEL'],b'{}'),([],b'x'*8193)]:
   p=subprocess.run(['node',str(DRIVER),*argv],input=data,capture_output=True,timeout=10)
   self.assertEqual(p.returncode,2);self.assertEqual(p.stdout,b'');self.assertNotIn(b'PRIVATE_PIN_COOKIE_MEDIA_SENTINEL',p.stderr);self.assertEqual(json.loads(p.stderr)['stage'],'INPUT_PARSE')
if __name__=='__main__':unittest.main()
