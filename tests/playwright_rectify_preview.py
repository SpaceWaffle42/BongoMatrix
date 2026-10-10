"""Real Playwright + real Flask preview, synthetic LOCAL state only; hardware NOTRUN.

Run with the isolated Playwright venv. --serve PORT hosts the explicit local demo.
No interception or fabricated successful route responses are used.
"""
import argparse
import datetime
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import threading
import time
from types import ModuleType
from unittest.mock import Mock
try:
    from tests.preview_request_contract import RequestGate
except ModuleNotFoundError:
    from preview_request_contract import RequestGate

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RESULT = Path(os.environ.get('MATRIX_PLAYWRIGHT_RESULT', '/root/.local/state/hermes-worker-dashboard/matrix-development-20261008-r11b-playwright-fix-result.json'))

def record(name, status, **evidence):
    result = json.loads(RESULT.read_text())
    result.setdefault('checks', []).append(dict(name=name, status=status, **evidence))
    result['updated_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    RESULT.write_text(json.dumps(result, indent=2) + '\n')

class _DecodeOnlyMatrix:
    """Real media decoding for the synthetic preview, with no panel behind it.

    The Advanced Canvas editor draws media layers from /api/media-timeline,
    which calls ``controller.matrix.media_frames``. A Mock there made every
    media frame black in the preview tiers.
    """

    def __init__(self, width=64, height=32):
        from types import SimpleNamespace
        self.matrix = SimpleNamespace(width=width, height=height)

    def media_frames(self, *args, **kwargs):
        from matrix_framework import Matrix
        return Matrix.media_frames(self, *args, **kwargs)

    def _media_file(self, filename):
        from matrix_framework import Matrix
        return Matrix._media_file(self, filename)


def build(state, public_demo=False):
    hardware = ModuleType('rgbmatrix')
    def forbidden(*args, **kwargs):
        raise RuntimeError('Native hardware forbidden in synthetic preview')
    hardware.RGBMatrix = hardware.RGBMatrixOptions = forbidden
    hardware.graphics = Mock()
    sys.modules['rgbmatrix'] = hardware
    from PIL import Image
    from config_store import ConfigStore
    from pairing import PairedDeviceStore
    from secret_store import SecretStore
    from webapp import create_app
    media = state / 'media'
    media.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / 'resources/media/built-in', media / 'built-in', dirs_exist_ok=True)
    Image.new('RGB', (64, 32), '#12ab34').save(media / 'SYNTHETIC_still.png')
    for name, delays in [('SYNTHETIC_motion.gif', [120,180,240]), ('SYNTHETIC_slow.gif', [1200,1800,2400]), ('SYNTHETIC_paged.gif', [30]*90)]:
        frames = [Image.new('RGB', (64,32), ['red','lime','blue'][i%3]) for i in range(len(delays))]
        frames[0].save(media / name, save_all=True, append_images=frames[1:], duration=delays, loop=0)
        for frame in frames:
            frame.close()
    store = ConfigStore(state / 'config.json', media_dir=media)
    devices = PairedDeviceStore(state / 'paired.json')
    existing = devices.list()
    device = existing[0] if existing else devices.add('SYNTHETIC local Playwright demo', max_devices=8)
    controller = Mock()
    controller.status.return_value = {'running':False,'current_module':None,'last_error':None,'preview_only':True}
    def frame():
        stream = io.BytesIO()
        Image.new('RGB',(64,32),'black').save(stream, format='PNG')
        stream.seek(0)
        return stream, 1
    controller.frame_png.side_effect = frame
    controller.matrix = _DecodeOnlyMatrix()
    pairing = Mock()
    pairing.active_pin.return_value = None
    pairing.has_testing_pin.return_value = False
    app = create_app(controller, store, pairing, secret_key=secrets.token_hex(32), session_generation='synthetic-playwright', media_dir=media, device_store=devices, openweather_secrets=SecretStore(state/'absent-secret'), location_search=lambda q:[], config_exclusion_sentinel=state/'absent-sentinel', config_exclusion_marker=state/'absent-marker', config_exclusion_authority=state/'absent-authority')
    app.extensions['offline_controller'] = controller
    app.extensions['offline_dispatched'] = []
    from flask import request
    @app.before_request
    def offline_dispatch_observer():
        app.extensions['offline_dispatched'].append((request.method, request.path))
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(paired=True, generation='synthetic-playwright', device_id=device['id'])
    cookie = client.get_cookie('session').value
    if not public_demo:
        assert app.test_client().get('/api/config').status_code == 401
        assert client.get('/api/config', headers={'X-Matrix-Client-Contract':'5'}).status_code == 200
    if public_demo:
        from flask import request, session, jsonify
        def boundary():
            path = request.path
            allowed = path == '/' or path.startswith(('/static/', '/media/', '/api/edit-preview/', '/api/media-timeline/', '/api/scroll-timing/')) or path in {'/api/config','/api/media','/api/screen-media','/api/status','/api/display-frame'}
            if not allowed:
                return jsonify(error='SYNTHETIC preview: hardware and host operations disabled'), 403
            session.update(paired=True, generation='synthetic-playwright', device_id=device['id'])
        app.before_request_funcs.setdefault(None, []).insert(0, boundary)
        @app.after_request
        def banner(response):
            if request.path == '/' and response.status_code == 200:
                html = response.get_data(as_text=True)
                pos = html.find('>', html.find('<body')) + 1
                response.set_data(html[:pos] + '<div style="padding:12px;background:#663300;color:white">SYNTHETIC LOCAL FIX CANDIDATE — hardware NOTRUN; automatic local fixture auth; private Save only; production unchanged.</div>' + html[pos:])
            return response
    return app, cookie, store

OBSERVER = """() => {
 window.__draws=[]; window.__pages=[]; window.__terminalCancelErrors=0;
 const original=CanvasRenderingContext2D.prototype.drawImage;
 CanvasRenderingContext2D.prototype.drawImage=function(...args){const out=original.apply(this,args);if(this.canvas.id==='matrix-preview'&&previewMode==='edit')__draws.push({time:performance.now(),generation:editPreviewGeneration,pixel:[...this.getImageData(32,16,1,1).data]});return out};
 const old=fetch;window.fetch=async(...args)=>{const terminalCancel=String(args[0]).includes('/api/edit-preview/cancel')&&document.querySelector('#preview-frame')?.textContent.includes('draft complete');const r=await old(...args);if(terminalCancel&&!r.ok)__terminalCancelErrors++;if(String(args[0]).includes('/api/edit-preview/')&&!String(args[0]).includes('/cancel'))r.clone().json().then(p=>__pages.push({generation:p.generation,page:p.page,complete:p.complete,count:p.frames?.length,delays:p.frames?.map(f=>f.delay_ms),status:r.status})).catch(()=>{});return r};
}"""
READY = "typeof matrixEditorsReady!=='undefined'&&matrixEditorsReady&&typeof screenMediaCatalog!=='undefined'&&screenMediaCatalog"

def observe(page, evidence, live=False):
    events = []
    page.on('pageerror', lambda e: events.append({'kind':'uncaught','type':type(e).__name__}))
    page.on('console', lambda m: events.append({'kind':'console','level':m.type}) if m.type == 'error' else None)
    from urllib.parse import urlsplit
    page.on('response', lambda r: events.append({'kind':'response','method':r.request.method,'path':urlsplit(r.url).path,'status':r.status,'timing':r.request.timing}))
    page.on('requestfailed', lambda r: events.append({'kind':'requestfailed','method':r.method,'path':urlsplit(r.url).path,'error':r.failure}))
    return events

def finite(page, role, name, expected_count, wait_ms, evidence):
    card = page.locator(f'#screen-media-{role} .screen-media-choice[data-source="{name}"]')
    # A persistent demo can retain the prior journey's saved 200% speed.
    # Native-delay expectations require a browser-owned 100% draft fixture.
    card.locator('.screen-media-selected').check()
    speed = card.locator('[data-media-layout="playback_speed_percent"]')
    if float(speed.input_value()) != 100:
        speed.fill('100')
    card.locator('.screen-media-selected').check()
    page.wait_for_timeout(600)
    # Fence any input-owned pass; next pass must be the explicit Inspect.
    page.evaluate('cancelEditPreview()')
    page.evaluate('__draws=[];__pages=[]')
    card.locator('.palette-inspect').click()
    page.wait_for_function(f'__draws.length>={expected_count}', timeout=25000)
    # Still images own the configured hold time (default seven seconds).
    # Do not mistake a legitimate final-frame hold for automatic repetition.
    if name.endswith('.png'):
        delays = page.evaluate('__pages.flatMap(p=>p.delays||[])')
        page.wait_for_timeout(sum(delays))
    page.wait_for_timeout(wait_ms)
    observed = page.evaluate("({draws:__draws,pages:__pages,generation:editPreviewGeneration,timer:editPreviewAnimationTimer!==null,frames:editPreviewFrames.length,status:document.querySelector('#preview-frame').textContent,pixel:[...ctx.getImageData(32,16,1,1).data],size:[ctx.canvas.width,ctx.canvas.height]})")
    page.screenshot(path=str(evidence / f'{role}-{name}.png'))
    assert len(observed['draws']) == expected_count, f'Product RED: one Inspect repeated: {len(observed["draws"])} draws expected {expected_count}; generations {[p["generation"] for p in observed["pages"]]}'
    assert observed['timer'] is False and observed['frames'] == 0, observed
    assert observed['size'] == [64,32]
    assert observed['pages'][-1]['complete'] is True
    assert len({draw['generation'] for draw in observed['draws']}) == 1
    delays = [delay for item in observed['pages'] for delay in item['delays']]
    assert len(delays) == expected_count
    for index in range(1, len(observed['draws'])):
        assert observed['draws'][index]['time']-observed['draws'][index-1]['time'] >= delays[index-1]-8, 'Native frame delay was cut short'
    assert page.evaluate('editPreviewSession===null&&editPreviewController===null&&editPreviewToken===null')
    assert 'complete' in observed['status']
    assert observed['pixel'] == observed['draws'][-1]['pixel'], 'Last frame must remain visible'
    assert page.evaluate('editPreviewLastBitmap!==null')
    assert page.evaluate('performance.now()')-observed['draws'][-1]['time'] >= delays[-1]-8, 'Final native delay was cut short'
    assert page.evaluate('__terminalCancelErrors') == 0, 'Product RED: completed backend session is cancelled again and produces HTTP errors'
    return observed

def run(args):
    from playwright.sync_api import sync_playwright
    from werkzeug.serving import make_server, WSGIRequestHandler
    evidence = ROOT / '.E' / args.phase
    evidence.mkdir(parents=True, exist_ok=True)
    server = thread = app = store = None
    failed = False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path='/usr/bin/google-chrome', headless=True, args=['--no-sandbox','--disable-background-networking'])
            record('tool-version', 'PASS', playwright=__import__('importlib.metadata',fromlist=['version']).version('playwright'), chrome=browser.version, headless=True, phase=args.phase)
            if args.baseline or args.live:
                context = browser.new_context(viewport={'width':1440,'height':1000})
                # Live traces deliberately exclude snapshots, screenshots and network payloads.
                if not args.live:
                    context.tracing.start(screenshots=True, snapshots=True, sources=True)
                page = context.new_page()
                events = observe(page, evidence, live=args.live)
                response = page.goto(args.url, wait_until='domcontentloaded', timeout=20000)
                if not args.live:
                    page.wait_for_function(READY)
                    page.evaluate(OBSERVER)
                    card=page.locator('#screen-media-sleep .screen-media-choice[data-source="DEMO_upload_animation.gif"] .palette-inspect')
                    card.click()
                    page.wait_for_function('__draws.length>=2')
                    page.wait_for_timeout(1400)
                    record('old-5001-animation-observation','OBSERVED',draws=page.evaluate('__draws'),pages=page.evaluate('__pages'))
                if not args.live or '/pair' in page.url:
                    page.screenshot(path=str(evidence/'unauthenticated-or-demo.png'))
                record('live-observational' if args.live else 'old-5001-baseline', 'AUTH_BLOCKED' if args.live and '/pair' in page.url else 'OBSERVED', status_code=response.status, url=page.url, title=page.title(), production_patch_installed=False, network_events=len(events))
                (evidence/'network.json').write_text(json.dumps(events,indent=2))
                if not args.live:
                    context.tracing.stop(path=str(evidence/'trace.zip'))
                context.close()
                browser.close()
                return 0
            if not args.url:
                app, cookie, store = build(evidence/'synthetic-state')
                class Quiet(WSGIRequestHandler):
                    def log(self,*a,**k): pass
                server = make_server('127.0.0.1',args.port,app,threaded=True,request_handler=Quiet)
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                base = f'http://127.0.0.1:{server.server_port}'
            else:
                base=args.url
            context=browser.new_context(viewport={'width':1440,'height':1000})
            if not args.url:
                context.add_cookies([{'name':'session','value':cookie,'url':base,'httpOnly':True,'sameSite':'Strict'}])
            gate=RequestGate(base)
            gate.install(context)
            context.tracing.start(screenshots=True,snapshots=True,sources=True)
            page=context.new_page()
            events=observe(page,evidence)
            try:
                page.goto(base,wait_until='domcontentloaded')
                page.wait_for_function(READY)
                from webapp import APPLICATION_VERSION
                assert page.locator('.sidebar-foot').inner_text().find('Version '+APPLICATION_VERSION)>=0
                record('visible-release-version','PASS',version=APPLICATION_VERSION)
                page.wait_for_timeout(2000)
                assert not gate.blocked, gate.blocked
                if app:
                    assert store is not None
                    before=store.get()
                    dispatched=len(app.extensions['offline_dispatched'])
                    forbidden=['/api/config','/api/media','/api/settings','/api/start','/api/stop','/api/restart','/api/preview/sleep','/api/edit-preview/sleep','/api/pair']
                    for path in forbidden:
                        rejected=page.evaluate("path=>fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(()=>false,()=>true)",path)
                        assert rejected, path
                    assert len(gate.blocked)==len(forbidden)
                    assert not any(method=='POST' and path in forbidden for method,path in app.extensions['offline_dispatched'][dispatched:])
                    assert store.get()==before
                    # Status polling is observational; no hardware control calls.
                    assert all(call[0] in ('status','frame_png') for call in app.extensions['offline_controller'].mock_calls)
                    record('observational-browser-pre-dispatch-denial','PASS',denied=gate.blocked,server_mutations=0)
                    gate.blocked.clear()
                gate.product_journey=True
                page.evaluate(OBSERVER)
                roles=['sleep'] if args.phase=='red' else ['sleep','splash']
                for role in roles:
                    for invocation in range(2 if args.phase!='red' else 1):
                        data=finite(page,role,'SYNTHETIC_motion.gif',3,850,evidence)
                        assert [d['pixel'][:3] for d in data['draws']] == [[255,0,0],[0,255,0],[0,0,255]]
                        assert data['pages'][0]['delays'] == [120,180,240]
                        record(f'{role}-gif-invocation-{invocation}', 'PASS', phase=args.phase, evidence=data)
                if args.phase!='red':
                    for role in roles:
                        record(role+'-still','PASS',evidence=finite(page,role,'SYNTHETIC_still.png',1,1200,evidence))
                        record(role+'-slow','PASS',evidence=finite(page,role,'SYNTHETIC_slow.gif',3,2800,evidence))
                        data=finite(page,role,'SYNTHETIC_paged.gif',90,600,evidence)
                        assert len(data['pages'])>1, 'Fixture must cross actual page boundary'
                        record(role+'-multipage','PASS',evidence=data)
                    # Browser-owned layout editing/debounce, supersession and cancellation.
                    card=page.locator('#screen-media-sleep .screen-media-choice[data-source="SYNTHETIC_motion.gif"]')
                    page.evaluate('__draws=[];__pages=[]')
                    card.locator('[data-media-layout="playback_speed_percent"]').fill('200')
                    page.wait_for_function('__draws.length>=3')
                    page.wait_for_timeout(600)
                    edit=page.evaluate('({draws:__draws,pages:__pages,timer:editPreviewAnimationTimer!==null})')
                    assert len(edit['draws'])==3 and not edit['timer'], edit
                    assert edit['pages'][0]['delays']==[60,90,120], edit
                    record('editing-debounce-new-finite-pass','PASS',evidence=edit)
                    slow=page.locator('#screen-media-sleep .screen-media-choice[data-source="SYNTHETIC_slow.gif"]')
                    page.evaluate('__draws=[];__pages=[]')
                    slow.locator('.palette-inspect').click()
                    page.wait_for_function('__draws.length>=1')
                    canceled_generation=page.evaluate('editPreviewGeneration')
                    card.locator('.palette-inspect').click()
                    page.wait_for_function(f'__draws.some(d=>d.generation>{canceled_generation})')
                    count=page.evaluate('__draws.length')
                    page.wait_for_timeout(1600)
                    assert page.evaluate(f'__draws.filter(d=>d.generation==={canceled_generation}).length')==1
                    assert page.evaluate('editPreviewSession===null&&editPreviewAnimationTimer===null')
                    record('cancel-superseded-slow-no-stale-frame','PASS',cancelled_generation=canceled_generation,draw_count=page.evaluate('__draws.length'))
                    # A second Inspect restarts explicitly; cancellation after first frame cannot resume.
                    slow.locator('.palette-inspect').click()
                    page.wait_for_function('editPreviewFrames.length>0')
                    page.locator('label[for="preview-mode-live"]').click()
                    dialog=page.locator('.edit-preview-switch-dialog')
                    assert dialog.is_visible()
                    dialog.get_by_role('button',name='Stay Editing').click()
                    assert page.evaluate("previewMode==='edit'")
                    record('switch-cancellation-preserves-draft','PASS')
                    page.evaluate('cancelEditPreview()')
                    before=page.evaluate('__draws.length')
                    page.wait_for_timeout(2000)
                    assert page.evaluate('__draws.length')==before
                    assert page.evaluate('editPreviewSession===null&&editPreviewFrames.length===0&&editPreviewAnimationTimer===null&&editPreviewLastBitmap===null')
                    record('explicit-cancel-no-resumption','PASS')
                    page.set_viewport_size({'width':390,'height':844})
                    record('narrow-finite','PASS',evidence=finite(page,'sleep','SYNTHETIC_motion.gif',3,850,evidence))
                    geometry=page.evaluate("(()=>{const c=ctx.canvas,r=c.getBoundingClientRect();return {intrinsic:[c.width,c.height],width:r.width,height:r.height,viewport:innerWidth,overflow:document.documentElement.scrollWidth-innerWidth}})()")
                    assert geometry['intrinsic']==[64,32] and abs(geometry['width']/geometry['height']-2)<.02 and geometry['overflow']<=1, geometry
                    record('narrow-geometry','PASS',evidence=geometry)
                    draft=page.evaluate('configBody()')
                    gate.save_armed=True
                    page.locator('#save-config').click()
                    page.wait_for_function("!matrixDirty&&document.querySelector('#message').textContent.includes('saved')")
                    saved=page.evaluate("fetch('/api/config',{headers:{'X-Matrix-Client-Contract':'5'}}).then(r=>r.json())")
                    assert saved['playlist']==draft['playlist']
                    page.reload(wait_until='domcontentloaded')
                    page.wait_for_function(READY)
                    assert page.evaluate('configBody().playlist')==draft['playlist']
                    record('save-backend-reload','PASS',revision=saved['revision'])
                assert not gate.blocked, gate.blocked
                assert not gate.save_armed, 'Explicit Save admission was not consumed'
                record('pre-dispatch-contract','PASS',allowed=gate.allowed,blocked=gate.blocked)
                assert not [e for e in events if e['kind']=='uncaught']
                record('console-network-summary','PASS',phase=args.phase,uncaught=0,console_errors=sum(e['kind']=='console' for e in events),failed_requests=sum(e['kind']=='requestfailed' for e in events))
            except Exception as error:
                failed=True
                import traceback
                bounded_trace = [{'filename': frame.filename, 'line': frame.lineno, 'function': frame.name} for frame in traceback.extract_tb(error.__traceback__)[-8:]]
                record(args.phase,'FAIL',classification='PRODUCT_OR_ASSERTION_FAILURE',error=str(error)[:1600],exception_class=type(error).__name__,traceback=bounded_trace)
                print(json.dumps({'exception_class':type(error).__name__,'traceback':bounded_trace}))
                page.screenshot(path=str(evidence/'failure.png'))
                print(str(error)[:1600])
            finally:
                (evidence/'network.json').write_text(json.dumps(events,indent=2))
                context.tracing.stop(path=str(evidence/'trace.zip'))
                context.close()
                browser.close()
    finally:
        if server:
            server.shutdown();server.server_close();thread.join(5)
        if app:
            app.extensions['close_edit_preview_services']()
        record(args.phase+'-cleanup','PASS',server_stopped=thread is None or not thread.is_alive())
    return 2 if failed else 0

if __name__=='__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser()
    parser.add_argument('--phase',default='green')
    parser.add_argument('--url')
    parser.add_argument('--port',type=int,default=0)
    parser.add_argument('--baseline',action='store_true')
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--serve',type=int)
    args=parser.parse_args()
    if args.serve:
        app,_,_=build(ROOT/'.E'/'host-state',public_demo=True)
        try:
            app.run(host='0.0.0.0' if args.serve==5001 else '127.0.0.1',port=args.serve,debug=False,use_reloader=False,threaded=True)
        finally:
            app.extensions['close_edit_preview_services']()
    else:
        sys.exit(run(args))