"""Finding-bound shipped-block execution; simulated POST origin/logical capture, real clock/decoder/pager. No hardware or HTTP claim."""
import base64,copy,hashlib,io,json,os,selectors,subprocess,tempfile,threading,time
from pathlib import Path
from PIL import Image
from config_store import DEFAULT_CONFIG,validate_config
from webapp import _edit_preview_iterator
from display_controller import playlist_item_config
from edit_preview import CaptureMatrix,PreviewSession
from matrix_framework import Matrix
from media_resolver import MediaResolver
from modules import sleep_screen
W=Path(__file__).parent
SHA=lambda b:hashlib.sha256(b).hexdigest()
def run(driver):
 stop=threading.Event();thread=None;session=None;pin=None;resolver=None;child=None
 with tempfile.TemporaryDirectory() as tmp:
  try:
   root=Path(tmp);gif=root/'distinct.gif'
   images=[Image.new('RGB',(64,32),(i*5+1,255-i*5,37)) for i in range(40)]
   images[0].save(gif,format='GIF',save_all=True,append_images=images[1:],duration=[100]*39+[8000],loop=0,optimize=False,disposal=2)
   for im in images:im.close()
   assert SHA(gif.read_bytes())=='2cf16758239dd2288ef6f3572765d15803cb9d36068883b96ca6ef5e06f0f32e'
   config=copy.deepcopy(DEFAULT_CONFIG);config['media']['sleep']='distinct.gif';config['media_layout']['sleep']['x']=7;row=next(x for x in config['playlist'] if x['module']=='sleep');row.update(source='distinct.gif',sources=['distinct.gif']);row['layout']['x']=7;validate_config(config);row=next(x for x in config['playlist'] if x['module']=='sleep');frozen=copy.deepcopy(config)
   resolver=MediaResolver(root);pin=resolver.resolve('distinct.gif');session=PreviewSession(_edit_preview_iterator('sleep',config,None,{'distinct.gif':pin},None,threading.Event(),selected_screen_row=row))
   page=session.page();remaining=session.page(page['next_token']);assert len(page['frames'])==32 and len(remaining['frames'])==8 and remaining['complete']
   def decode(png):
    with Image.open(io.BytesIO(base64.b64decode(png))) as im:return SHA(im.convert('RGBA').tobytes())
   pixels=[decode(f['png']) for f in page['frames']];last_hash=decode(remaining['frames'][-1]['png']);assert last_hash not in pixels
   publications=[];origin=None
   class LogicalMatrix(CaptureMatrix):
    show_gif=Matrix.show_gif
    def __init__(self):super().__init__();self.lock=threading.RLock()
    def swap(self,canvas=None):
     with self.lock:
      self._capture.publish(canvas or self.canvas);publications.append(self.frame());return super().swap(canvas)
    def frame(self):
     with self.lock:
      raw,rev=self._capture.latest_rgb()
      with Image.frombytes('RGB',(64,32),raw) as im:
       rgba=im.convert('RGBA');png=io.BytesIO();rgba.save(png,format='PNG');data={'ok':True,'http':200,'revision':rev,'dimensions':[64,32],'pixels':list(rgba.tobytes()),'png':base64.b64encode(png.getvalue()).decode()};rgba.close();return data
   matrix=LogicalMatrix();before=matrix.frame();playback={};events=[];captured=[]
   def play():
    try:playback['returned']=sleep_screen.run(matrix,playlist_item_config(config,row),duration=86400,stop_event=stop,pinned_handle=pin);playback['elapsed']=time.monotonic()-origin
    except BaseException as exc:playback['failure_class']=type(exc).__name__
   text=Path(driver).read_text();lines=text.splitlines();begin=next(i for i,x in enumerate(lines) if "at('DEVICE_START'); const start=" in x);finish=next(i for i,x in enumerate(lines) if 'roles[role]=' in x)
   block='\n'.join(lines[begin:finish+1])
   observer='\n'.join(lines[5:14])
   prelude="const assert=require('node:assert/strict'),{createHash}=require('node:crypto'),readline=require('node:readline');const rl=readline.createInterface({input:process.stdin});const replies=[];let receiver;rl.on('line',l=>{const v=JSON.parse(l);if(receiver){const r=receiver;receiver=null;r(v)}else replies.push(v)});const rpc=async(op,payload)=>{console.log(JSON.stringify({op,payload}));return replies.length?replies.shift():await new Promise(r=>receiver=r)};\n"
   setup="const sleep=ms=>new Promise(r=>setTimeout(r,ms)),hash=b=>createHash('sha256').update(Buffer.from(b)).digest('hex');const frame=()=>rpc('frame');const jsonRequest=(path,payload)=>rpc(path,payload);const evaluate=async()=>draft;const roles={},role='sleep',source='distinct.gif',selected={id:'controlled'},draft={},native={},generation=1,owner='d'.repeat(32),control={generation,preview_instance:owner};const earlier="+json.dumps({'revision':before['revision']})+",pixels="+json.dumps(pixels)+",inspector={status:200,response:"+json.dumps(page)+"};\n"
   js=prelude+observer+'\n(async()=>{'+setup+block+"\nconsole.log(JSON.stringify({result:roles[role],frames,stage}));})().catch(e=>{console.log(JSON.stringify({failure:exceptionClass(e),stage,frames}));process.exitCode=2}).finally(()=>rl.close());"
   script=root/'shipped.cjs';script.write_text(js);child=subprocess.Popen(['node',str(script)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
   selector=selectors.DefaultSelector();selector.register(child.stdout,selectors.EVENT_READ);deadline=time.monotonic()+22;out=None
   while time.monotonic()<deadline:
    if not selector.select(max(0,deadline-time.monotonic())):raise TimeoutError('owned Node deadline')
    line=child.stdout.readline()
    if not line:break
    message=json.loads(line)
    if 'op' not in message:out=message;continue
    op=message['op'];events.append({'op':op,'elapsed':None if origin is None else time.monotonic()-origin})
    if op=='/api/device-preview/sleep':
     assert message['payload']=={'config':{},'generation':1,'preview_instance':'d'*32};origin=time.monotonic();thread=threading.Thread(target=play,name='r10-owned-native-capture');thread.start();response={'status':202,'body':{'generation':1,'active_module':'sleep','heartbeat_after_seconds':5}}
    elif op=='frame':response=matrix.frame();captured.append(response)
    elif op=='/api/device-preview/heartbeat':response={'status':202 if message['payload']=={'generation':1,'preview_instance':'d'*32} else 409,'body':{'generation':1,'active':True}}
    elif op=='/api/status':response={'status':200,'body':{'last_error':None,'current_module':'device-draft:sleep'}}
    elif op=='/api/device-preview/cancel':response={'status':202,'body':{'cancelled':True}}
    else:raise AssertionError(op)
    child.stdin.write(json.dumps(response)+'\n');child.stdin.flush()
   selector.close();child.wait(timeout=3);stderr=child.stderr.read();assert out is not None and not stderr
   thread.join(13);assert not thread.is_alive() and playback.get('returned') and len(publications)==40
   assert [SHA(bytes(f['pixels'])) for f in publications[:32]]==pixels and SHA(bytes(publications[-1]['pixels']))==last_hash and config==frozen
   result={'exit_code':child.returncode,'output':out,'events':events,'page_counts':[32,8],'publication_count':40,'playback':playback,'fixture_sha256':SHA(gif.read_bytes()),'shipped_block_sha256':SHA(block.encode()),'config_unchanged':True,'worker_thread_exited':True}
   if child.returncode==0:
    r=out['result'];f=next(f for f in captured if SHA(bytes(f['pixels']))==r['frame_pixels_sha256'] and f['revision']==r['frame_revision_after']);assert r['native_png']==f['png'] and r['png_dimensions']==[64,32] and r['frame_revision_after']>before['revision'];assert r['inspector_png']==page['frames'][pixels.index(r['frame_pixels_sha256'])]['png'];assert r['inspector_pixels_sha256']==r['frame_pixels_sha256'];assert captured[-1]==f
    hb=next(e for e in events if e['op']=='/api/device-preview/heartbeat');first=next(e for e in events if e['op']=='frame');assert first['elapsed']<1 and hb['elapsed']>=5.1 and events.index(first)<events.index(hb);assert SHA(bytes(matrix.frame()['pixels']))==last_hash;assert r['frame_pixels_sha256']!=last_hash;result['captured_evidence_reused_after_late_phase']=True;result['receipt_metadata_equality']=True
   return result
  finally:
   stop.set()
   if thread:thread.join(3);assert not thread.is_alive()
   if child:
    if child.poll() is None:child.kill();child.wait(timeout=3)
    for stream in (child.stdin,child.stdout,child.stderr):stream.close()
   if session:session.cancel()
   if pin:pin.close()
   if resolver:resolver.close()
