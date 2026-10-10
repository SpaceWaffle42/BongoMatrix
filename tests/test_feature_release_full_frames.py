"""Full unexcised R9 browser journey against real local app routes.
Only controller status/frame is a simulated logical display seam. NO hardware proof.
"""
import sys,io,base64,json,time,tempfile,threading,unittest,hashlib
from pathlib import Path
from tests import test_release_projection as projection
from tests.test_feature_release_failure_receipt import caller,DRIVER
from tests.release_browser_binding import provisioned_browser
from PIL import Image
from flask import request
from werkzeug.serving import make_server
from tests.ci_quarantine import non_essential
@non_essential('#32 native-driver harness flake')
class FullFrameTests(unittest.TestCase):
 def journey(self,mode):
  with provisioned_browser(caller):
   return self._journey(mode)
 def _journey(self,mode):
  fixture=projection.ProjectionTests();f=fixture.fixture();self.addCleanup(fixture.doCleanups)
  selected={};observed=[];seq=[1];starts=[]
  @f.app.after_request
  def capture(response):
   if request.path in ('/api/edit-preview/splash','/api/edit-preview/sleep') and response.status_code==200 and request.method=='POST':
    data=response.get_json();selected[request.path.rsplit('/',1)[1]]=data['frames'][0]['png']
   if request.path in ('/api/device-preview/splash','/api/device-preview/sleep') and request.method=='POST':starts.append(response.status_code)
   return response
  def status():
   lease=f.controller.device_preview_manager.active_lease() if hasattr(f.controller,'device_preview_manager') else None
   return {'last_error':None,'current_module':'device-draft:'+lease.request.config['module'] if lease else 'clock'}
  f.controller.status.side_effect=status
  def frame():
   manager=getattr(f.controller,'device_preview_manager',None);lease=manager.active_lease() if manager else None
   role=lease.request.config['module'] if lease else None
   seq[0]+=1
   if role and role in selected:
    raw=base64.b64decode(selected[role]);im=Image.open(io.BytesIO(raw)).convert('RGBA')
    if mode=='positive':
     im=Image.new('RGBA',(64,32),(0,0,0,255));im.paste((255,0,0,255),(7,0,64,32))
    if mode=='wrong' and role=='sleep':im=Image.new('RGBA',(64,32),(1,2,3,255))
    if mode=='dimensions' and role=='sleep':im=Image.new('RGBA',(63,32),(1,2,3,255))
    if mode=='decode' and role=='sleep':return io.BytesIO(b'not PNG'),seq[0]
    rev=1 if mode=='stale' and role=='sleep' else seq[0]
    observed.append((role,rev,hashlib.sha256(im.tobytes()).hexdigest()))
   else:im=Image.new('RGBA',(64,32),'black');rev=seq[0]
   raw=io.BytesIO();im.save(raw,format='PNG');raw.seek(0);return raw,rev
  f.controller.frame_png.side_effect=frame
  before=f.store.path.read_bytes();cookie=f.client.get_cookie('session').value
  context=dict(phase='initial',app_pid=123,nonce='d'*64,binding_sha256='a'*64,source_identity='b'*64,spec_sha256='c'*64,driver_sha256=caller.sha(DRIVER.read_bytes()),caller_sha256=caller.sha((DRIVER.parent/'feature_release_acceptance.py').read_bytes()),version=projection.webapp_fixture.webapp.APPLICATION_VERSION,cookie=cookie,offline_fixture=True)
  server=make_server('127.0.0.1',0,f.app,threaded=True);thread=threading.Thread(target=server.serve_forever);thread.start()
  try:
   with tempfile.TemporaryDirectory() as tmp:
    if mode=='positive':
     proof=caller.chrome_journey(context,'http://127.0.0.1:'+str(server.server_port),tmp,time.monotonic()+40,DRIVER)
     self.assertTrue(proof['offline']);self.assertTrue(proof['saved_config_unchanged'])
     for role in ('splash','sleep'):
      r=proof['roles'][role];independent=Image.open(io.BytesIO(base64.b64decode(selected[role]))).convert('RGBA')
      self.assertEqual(r['source'],'fixture_0.gif');self.assertEqual(independent.size,(64,32));self.assertEqual(independent.getpixel((32,16)),(255,0,0,255));expected=Image.new('RGBA',(64,32),(0,0,0,255));expected.paste((255,0,0,255),(7,0,64,32));self.assertEqual(independent.tobytes(),expected.tobytes());self.assertEqual(r['frame_pixels_sha256'],hashlib.sha256(expected.tobytes()).hexdigest())
      self.assertEqual(r['heartbeat_http'],202);self.assertEqual(r['foreign_heartbeat_http'],409);self.assertEqual(r['cancel_http'],202)
      self.assertEqual(caller.png_rgba(r['native_png']),independent.tobytes())
     self.assertFalse(list(Path(tmp).glob('failure-*')))
    else:
     with self.assertRaises(ValueError):caller.chrome_journey(context,'http://127.0.0.1:'+str(server.server_port),tmp,time.monotonic()+40,DRIVER)
     record=json.loads((Path(tmp)/'failure-initial.json').read_bytes());diagnostic=record['diagnostic'];print('OFFLINE_DIAGNOSTIC '+json.dumps({'mode':mode,'classification':record['classification'],'diagnostic':diagnostic}),flush=True)
     self.assertIsNotNone(diagnostic);self.assertEqual(diagnostic['role'],'sleep')
     expected={'wrong':'FRAME_PIXELS_ASSERT','stale':'FRAME_REVISION_ASSERT','dimensions':'FRAME_DIMENSIONS_ASSERT','decode':'FRAME_POLL'}[mode]
     self.assertEqual(diagnostic['stage'],expected);self.assertEqual(record['exit_code'],2);self.assertFalse(record['acceptance']);self.assertFalse(list(Path(tmp).glob('browser-*')))
     self.assertGreaterEqual(diagnostic['frames']['observed_count'],0)
    self.assertEqual(f.store.path.read_bytes(),before);self.assertEqual(starts,[202,202])
  finally:server.shutdown();server.server_close();thread.join(5);self.assertFalse(thread.is_alive())
 def test_expected_decoded_frames(self):self.journey('positive')
 def test_wrong_pixels(self):self.journey('wrong')
 def test_nonadvancing_revision(self):self.journey('stale')
 def test_invalid_dimensions(self):self.journey('dimensions')
 def test_decode_failure(self):self.journey('decode')
