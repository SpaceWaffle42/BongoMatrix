"""Focused OFFLINE public-route and native dashboard/CDP projection tests.
No physical framebuffer/production acceptance claim. Uses existing local fixtures.
"""
import copy, io, json, os, secrets, subprocess, sys, unittest, threading
SAFE_EXCEPTIONS=[]
def safe_trace(frame,event,arg):
    if event=='exception' and Path(frame.f_code.co_filename).name in ('edit_preview.py','webapp.py','media_resolver.py') and issubclass(arg[0],Exception) and arg[0] not in (StopIteration,KeyError):
        record={'class':arg[0].__name__,'file':Path(frame.f_code.co_filename).name,'function':frame.f_code.co_name,'line':frame.f_lineno}
        if record not in SAFE_EXCEPTIONS and len(SAFE_EXCEPTIONS)<12:
            SAFE_EXCEPTIONS.append(record);print('SAFE_FIRST_EXCEPTION '+json.dumps(record),file=sys.stderr)
    return safe_trace
threading.settrace(safe_trace)
from pathlib import Path
from unittest.mock import patch
W=Path(__file__).resolve().parents[1]
from tests import test_webapp as webapp_fixture
from device_preview import DevicePreviewManager
from PIL import Image
DRIVER=Path(os.environ.get('PROJECTION_DRIVER',W/'deploy/feature_release_browser.mjs'))
OBS=[]
class ProjectionTests(unittest.TestCase):
    def fixture(self, count=1, existing=False):
        f=webapp_fixture.WebAppTests();f.setUp()
        def cleanup():
            f.app.extensions['close_edit_preview_services']()
            manager=getattr(f.controller,'device_preview_manager',None)
            if isinstance(manager,DevicePreviewManager):manager.close()
            registry=f.app.extensions.get('edit_preview_registry')
            if registry is not None:self.assertFalse(registry.reaper_alive)
            f.doCleanups();f.tearDown()
        self.addCleanup(cleanup)
        f.physical_draft_fixture()
        for i,color in enumerate(('red','blue')):
            Image.new('RGB',(64,32),color).save(f.media_dir/f'fixture_{i}.gif',format='GIF')
        saved=f.store.get();rows=copy.deepcopy(saved['playlist'])
        for role in ('splash','sleep'):
            row=next(r for r in rows if r['module']==role)
            if existing:row.update(source='fixture_0.gif',sources=['fixture_0.gif'])
            if count==2:
                extra=copy.deepcopy(row);extra['id']=role+'_saved_second';extra.update(source='fixture_1.gif',sources=['fixture_1.gif']);rows.append(extra)
        f.store.update({'playlist':rows},client_contract=5)
        return f
    def projection(self,draft,role,selected):
        line=next(l.strip() for l in DRIVER.read_text().splitlines() if l.strip().startswith('const native=structuredClone(draft);'))
        code="const assert=require('node:assert/strict');const {draft,role,selected}=JSON.parse(process.argv[1]);"+line+'console.log(JSON.stringify(native));'
        result=subprocess.run(['node','-e',code,json.dumps(dict(draft=draft,role=role,selected=selected))],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0);return json.loads(result.stdout)
    def observe(self,f):
        original=f.store.validate_candidate
        def wrapped(changes,**kw):
            current=f.store.get();current_ids={r.get('id') for r in current['playlist'] if r['module'] in ('splash','sleep')};ids={r.get('id') for r in changes.get('playlist',[]) if r['module'] in ('splash','sleep')}
            record={'saved_count':len(current_ids),'candidate_count':len(ids),'saved_ids_subset':current_ids<=ids,'failure_class':None}
            try:return original(changes,**kw)
            except (KeyError,TypeError,ValueError) as e:record['failure_class']=type(e).__name__;raise
            finally:OBS.append(record)
        return patch.object(f.store,'validate_candidate',side_effect=wrapped)
    def test_temporal_shipped_block(self):
        from tests.release_temporal_support import run
        baseline=run(W/'tests/fixtures/release_browser_r9.mjs')
        self.assertEqual(baseline['exit_code'],2)
        self.assertEqual(baseline['output']['stage'],'FRAME_PIXELS_ASSERT')
        result=run(DRIVER)
        destination=Path(os.environ.get('NATIVE_REPORT_DIR',W))
        (destination/'temporal-stable-red.json').write_text(json.dumps(baseline,indent=2)+'\n')
        (destination/'temporal-current.json').write_text(json.dumps(result,indent=2)+'\n')
        self.assertEqual(result['exit_code'],0, result['output'].get('stage'))
        self.assertTrue(result['captured_evidence_reused_after_late_phase'])
        self.assertTrue(result['receipt_metadata_equality'])
    def test_public_route_projection(self):
        for count,existing,choose_new in ((1,False,True),(1,True,False),(2,True,False),(2,True,True)):
            for role in ('splash','sleep'):
                with self.subTest(saved_rows=count,existing=existing,new=choose_new,role=role):
                    f=self.fixture(count,existing);saved=f.store.get();saved_bytes=f.store.path.read_bytes()
                    draft={k:copy.deepcopy(v) for k,v in saved.items() if k not in ('revision','schema_version')}
                    selected=copy.deepcopy(next(r for r in draft['playlist'] if r['module']==role))
                    if choose_new:
                        selected.update(id=role+'_unsaved',source='fixture_0.gif',sources=['fixture_0.gif']);draft['playlist'].append(selected)
                    selected['layout']['x']=7
                    next(r for r in draft['playlist'] if r.get('id')==selected['id'])['layout']['x']=7
                    before=copy.deepcopy(draft);native=self.projection(draft,role,selected)
                    control={'generation':1,'preview_instance':'e'*32}
                    with self.observe(f):
                        response=f.paired_request('POST','/api/device-preview/'+role,json={'config':native,**control},headers={'X-Matrix-Client-Contract':'5'})
                    self.assertEqual(response.status_code,202,'actual public contract-5 admission')
                    expected=copy.deepcopy(draft['playlist']);chosen=next(r for r in expected if r.get('id')==selected['id']);first=next(i for i,r in enumerate(expected) if r['module']==role);expected.remove(chosen);expected.insert(first,chosen)
                    self.assertEqual(native['playlist'],[dict(r,enabled=r['id']==selected['id']) if r['module']==role else r for r in expected])
                    self.assertEqual(native['custom_modules'],draft['custom_modules']);self.assertEqual(draft,before)
                    self.assertEqual(native['screen_enabled'],dict(draft['screen_enabled'],**{role:True}))
                    active=f.controller.device_preview_manager.active_lease();self.assertEqual(active.request.config['candidate']['playlist'],native['playlist'])
                    # Existing production manager, deterministic authoritative clock boundary.
                    manager=f.controller.device_preview_manager
                    with patch.object(manager,'_clock',return_value=active.last_heartbeat_at+5):
                        hb=f.paired_request('POST','/api/device-preview/heartbeat',json=control)
                    self.assertEqual(hb.status_code,202)
                    self.assertEqual(f.paired_request('POST','/api/device-preview/cancel',json=control).status_code,202)
                    invalid=copy.deepcopy(native);remove=next(r for r in saved['playlist'] if r['module']==role);invalid['playlist']=[r for r in invalid['playlist'] if r.get('id')!=remove['id']]
                    with self.observe(f):
                        rejected=f.paired_request('POST','/api/device-preview/'+role,json={'config':invalid,**dict(control,generation=2)},headers={'X-Matrix-Client-Contract':'5'})
                    self.assertEqual(rejected.status_code,400);self.assertEqual(f.store.get(),saved);self.assertEqual(f.store.path.read_bytes(),saved_bytes)
    def test_native_dashboard_projection(self):
        # Real dashboard, existing CDP runner, exact reviewed selection/inspector/projection.
        # Device manager is real; controller is the existing mock fixture, NOT framebuffer proof.
        for count,existing in ((1,False),(2,False),(1,True),(2,True)):
            with self.subTest(saved_rows=count,existing=existing):
                f=self.fixture(count,existing)
                # Keep every saved source resolvable: the inspector pins the entire
                # candidate, not just the selected row. Explicit fixture_0 selection
                # below already makes the native journey deterministic.
                # Keep built-in media: unrelated enabled modules also render in inspectors.
                def mock_frame():
                    frame=io.BytesIO();Image.new('RGB',(64,32),'black').save(frame,format='PNG');frame.seek(0);return frame,1
                f.controller.frame_png.side_effect=mock_frame
                before=f.store.path.read_bytes();cookie=f.client.get_cookie('session').value
                lines=DRIVER.read_text().splitlines()
                self.assertEqual(len(lines),112)
                def exact(first,last):
                    # One-based inclusive ranges of the reviewed, exact 98-line driver.
                    # R9 wraps startup and inserts safe observers without moving semantic blocks.
                    offset=11 if first<=24 else 10
                    if first==24: return lines[34]+'\n'
                    if first==96: return '\n'.join(lines[107:110])+'\n'
                    return '\n'.join(lines[first-1+offset:last+offset])+'\n'
                probe="const assert=require('node:assert/strict');const {createHash}=require('node:crypto');\n(async()=>{\n"+'\n'.join(lines[5:14])+'\n'
                probe+='const input='+json.dumps({'cookie':cookie,'nonce':secrets.token_hex(32),'version':'0.3.65','offline_fixture':True})+';input.debug_port=process.argv[1];input.base="http://127.0.0.1:"+process.argv[2];\nconst sleep=ms=>new Promise(r=>setTimeout(r,ms));\n'
                # No ESM imports/stdin loop or original transport/rollback branch braces.
                # No actual-frame function or actual-frame assertions in this focused mock-controller test.
                probe+=exact(7,22)+exact(24,24)+'try {\n'+exact(27,27)
                probe+="assert.equal(await send('Network.setCookie',{name:'session',value:input.cookie,url:input.base,httpOnly:true,sameSite:'Strict'}).then(r=>r.success),true);\n"
                selection=exact(48,74).replace("return (eligible.find(c=>!screenRowForSource(${JSON.stringify(role)},c.name))||eligible[0])?.name","return eligible.find(c=>c.name==='fixture_0.gif')?.name")
                probe+="const firstDiff=(a,b,p='config')=>{if(JSON.stringify(a)===JSON.stringify(b))return null;if(a&&b&&typeof a==='object'&&typeof b==='object'){for(const k of [...new Set([...Object.keys(a),...Object.keys(b)])]){const d=firstDiff(a[k],b[k],p+'.'+k);if(d)return d}}return {key:p.slice(0,160),before_type:typeof a,after_type:typeof b}};const traceDiff=(stage,a,b)=>{const d=firstDiff(a,b);if(d)process.stderr.write('SAFE_FIRST_DIFF '+JSON.stringify({stage,...d})+'\\n')};\n"
                selection=selection.replace("    const pixels=[];", "    const postInspect=await evaluate('configBody()');traceDiff('inspector',draft,postInspect);\n    const pixels=[];")
                final=exact(85,86).replace("assert.deepEqual(remains,draft", "traceDiff('device',draft,remains);assert.deepEqual(remains,draft")
                probe+=exact(29,29)+exact(36,41)+'const reports=[];\n'+selection+"stage='device-controls';\n"+'\n'.join(l for l in lines if any(marker in l for marker in ("at('DEVICE_START'); const start=","assert.equal(start.status,202);","await sleep(5100);","at('HEARTBEAT_OWN');","at('HEARTBEAT_FOREIGN');")))+'\n'+"stage='draft-unchanged';\n"+final
                probe+='reports.push({role,selected:true,inspect_http:inspector.status,native_http:start.status,heartbeat_http:hb.status,foreign_heartbeat_http:foreign.status,cancel_http:cancel.status});\n}\n'
                probe+=exact(90,90)
                probe+='console.log(JSON.stringify({offline:true,framebuffer_proof:false,reports,page_errors:errors,saved_config_unchanged:true}));\n}finally{\n'+exact(96,97)+'}\n})().catch(()=>{process.stderr.write("focused native probe failed at "+stage+" (private details withheld)\\n");process.exitCode=2});\n'
                with self.observe(f):report=f.run_live_chromium_probe(probe,timeout=50)
                self.assertTrue(report['offline']);self.assertFalse(report['framebuffer_proof']);self.assertEqual(report['page_errors'],0)
                self.assertTrue(report['saved_config_unchanged'])
                self.assertEqual(len(report['reports']),2);self.assertEqual(f.store.path.read_bytes(),before)
                self.assertTrue(all(r['saved_ids_subset'] and r['failure_class'] is None for r in OBS))
                # Only public control metadata; never persist cookie, nonce, IDs, or private exceptions.
                report['existing_row']=existing;report['saved_rows_per_role']=count
                destination=Path(os.environ.get('NATIVE_REPORT_DIR',W))
                (destination/(('native-existing' if existing else 'native-new')+'-'+str(count)+'.json')).write_text(json.dumps(report,indent=2))
if __name__=='__main__':
    try:unittest.main(verbosity=2)
    finally:(W/(os.environ.get('PHASE','test')+'-validator-metadata.json')).write_text(json.dumps(OBS,indent=2))
