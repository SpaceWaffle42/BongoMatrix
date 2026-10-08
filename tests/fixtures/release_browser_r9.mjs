// Owned native CDP journey. Stdin only for private session; stdout metadata only.
// The offline_transport branch is a declared fixture and cannot pass caller validation.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
// Failure metadata is diagnostic only; never an acceptance receipt.
const STAGES=new Set('INPUT_PARSE CDP_CONNECT PAGE_READY VERSION_CHECK SAVED_BEFORE SELECT DRAFT_ASSERT INSPECT_HTTP INSPECT_DECODE EARLIER_FRAME DEVICE_START HEARTBEAT_OWN HEARTBEAT_FOREIGN STATUS_ASSERT FRAME_POLL FRAME_DIMENSIONS_ASSERT FRAME_REVISION_ASSERT FRAME_PIXELS_ASSERT CANCEL DRAFT_AFTER SAVED_AFTER PAGE_ERRORS'.split(' '));
let stage='INPUT_PARSE',roleTag='none',phaseTag='initial',failure=null,ws,pending=new Map(),controls=[],jsonRequest,send;
const freshFrames=()=>({inspector_count:0,observed_count:0,inspector_hashes:[],observed_hashes:[],hashes_truncated:0,before_revision:null,last_revision:null,max_revision:null,dimensions:null,revision_advanced:false,pixels_matched:false,match_seen:false,elapsed_ms:0});
let frames=freshFrames(),http={method:null,path:null,status:null},pollStarted=0;
const at=s=>{if(!STAGES.has(s))throw Error();stage=s};
const bounded=(n,max=2147483647)=>Number.isSafeInteger(n)&&n>=0&&n<=max?n:null;
const remember=(key,digest)=>{if(!frames[key].includes(digest)){if(frames[key].length<16)frames[key].push(digest);else frames.hashes_truncated=Math.min(1000000,frames.hashes_truncated+1)}};
const exceptionClass=e=>e instanceof SyntaxError?'SyntaxError':e instanceof TypeError?'TypeError':e instanceof RangeError?'RangeError':e instanceof assert.AssertionError?'AssertionError':e?.constructor===Error?'Error':'OTHER';
const snapshot=e=>({schema:1,kind:'MATRIX_CALLER_FAILURE',component:'browser',phase:phaseTag,stage,role:roleTag,exception_class:exceptionClass(e),http:{...http},frames:structuredClone(frames),cleanup:{attempted:false,completed:false}});
try {
let raw='';assert.equal(process.argv.length,2); for await (const chunk of process.stdin){assert.ok(Buffer.byteLength(raw)+chunk.length<=8192);raw+=chunk}
const input=JSON.parse(raw), sleep=ms=>new Promise(r=>setTimeout(r,ms)); assert.ok(input&&typeof input==='object'&&!Array.isArray(input)); if(['initial','rollback','final'].includes(input.phase))phaseTag=input.phase; at('CDP_CONNECT');
const endpoint=await fetch(`http://127.0.0.1:${input.debug_port}/json/new`,{method:'PUT'}).then(r=>r.json());
ws=new WebSocket(endpoint.webSocketDebuggerUrl); let serial=0, errors=0;
ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);if(!p)return;pending.delete(m.id);clearTimeout(p.timer);m.error?p.reject(Error('CDP command refused')):p.resolve(m.result)}else if(m.method==='Runtime.exceptionThrown')errors++};
await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject});
send=(method,params={})=>new Promise((resolve,reject)=>{const id=++serial,timer=setTimeout(()=>{pending.delete(id);reject(Error('bounded CDP deadline'))},7000);pending.set(id,{resolve,reject,timer});ws.send(JSON.stringify({id,method,params}))});
const evaluate=async expression=>{const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error('browser assertion/evaluation failed');return r.result.value};
const until=async expression=>{const end=Date.now()+7000;while(Date.now()<end){if(await evaluate(expression))return;await sleep(30)}throw Error('bounded browser assertion deadline')};
const click=async selector=>{
 await evaluate(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({block:'center',behavior:'instant'})`);
 await until(`(()=>{const e=document.querySelector(${JSON.stringify(selector)}),r=e.getBoundingClientRect(),h=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return r.width>0&&r.height>0&&r.y>=0&&r.bottom<=innerHeight&&(h===e||e.contains(h))})()`);
 const p=await evaluate(`(()=>{const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2}})()`);
 await send('Input.dispatchMouseEvent',{type:'mousePressed',...p,button:'left',clickCount:1});
 await send('Input.dispatchMouseEvent',{type:'mouseReleased',...p,button:'left',clickCount:1});
};
jsonRequest=async(path,payload)=>{http={method:payload===undefined?'GET':'POST',path,status:null};const result=await evaluate(`(async()=>{const r=await fetch(${JSON.stringify(path)},{method:${payload===undefined?'"GET"':'"POST"'},headers:{'Content-Type':'application/json','X-Matrix-Action':'paired-request','X-Matrix-Client-Contract':String(typeof MATRIX_CLIENT_CONTRACT==='undefined'?4:MATRIX_CLIENT_CONTRACT)},${payload===undefined?'':`body:JSON.stringify(${JSON.stringify(payload)}),`}cache:'no-store'});let body;try{body=await r.json()}catch(_){return{status:r.status,redirected:r.redirected,decoded:false}}return{status:r.status,redirected:r.redirected,decoded:true,body}})()`);http.status=bounded(result.status,599);assert.equal(result.redirected,false);assert.equal(result.decoded,true);return result};
const hash=bytes=>createHash('sha256').update(Buffer.from(bytes)).digest('hex');
const frame=async()=>{http={method:'GET',path:'/api/display-frame',status:null};const f=await evaluate(`(async()=>{const r=await fetch('/api/display-frame',{cache:'no-store'});if(r.status!==200||r.redirected)return{http:r.status,ok:false};try{const blob=await r.blob(),bytes=new Uint8Array(await blob.arrayBuffer()),png=btoa(String.fromCharCode(...bytes));const b=await createImageBitmap(blob);const c=document.createElement('canvas');c.width=b.width;c.height=b.height;const x=c.getContext('2d');x.drawImage(b,0,0);b.close();return{ok:true,http:r.status,png,dimensions:[c.width,c.height],revision:Number(r.headers.get('X-Matrix-Frame')),pixels:[...x.getImageData(0,0,c.width,c.height).data]}}catch(_){return{ok:false,http:r.status}}})()`);http.status=bounded(f.http,599);assert.equal(f.ok,true);return f};
const decode=async png=>evaluate(`(async()=>{const r=await fetch('data:image/png;base64,'+${JSON.stringify(png)});const b=await createImageBitmap(await r.blob());const c=document.createElement('canvas');c.width=b.width;c.height=b.height;c.getContext('2d').drawImage(b,0,0);b.close();return{dimensions:[c.width,c.height],pixels:[...c.getContext('2d').getImageData(0,0,c.width,c.height).data]}})()`);

 at('PAGE_READY'); await send('Runtime.enable'); await send('Page.enable'); await send('Network.enable');
 if(!input.offline_transport){assert.equal(await send('Network.setCookie',{name:'session',value:input.cookie,url:input.base,httpOnly:true,sameSite:'Strict'}).then(r=>r.success),true)}
 await send('Page.navigate',{url:input.base+'/'});
 if(input.offline_transport){
  await until("document.querySelector('#verify')&&document.readyState==='complete'");
  await click('#verify');await until('typeof received!=="undefined"');
  const received=await evaluate('received');assert.equal(received.nonce,input.nonce);assert.equal(received.received,'actual-owned-input');assert.equal(errors,0);
  console.log(JSON.stringify({offline:true,nonce:input.nonce,native_click:true,actual_http:true}));
 }else{
  await until("document.readyState==='complete'&&typeof configBody==='function'&&typeof matrixEditorsReady!=='undefined'&&matrixEditorsReady");if(input.phase!=='rollback')await until("[...automaticWeatherModules].every(m=>providerTimingPlans.get(m)?.status!=='loading')&&announcementTimingPlan!==null");
  at('VERSION_CHECK'); const visible=await evaluate("document.querySelector('.sidebar-foot').textContent");
  assert.ok(visible.includes('Version '+input.version));
  at('SAVED_BEFORE'); const savedBefore=await jsonRequest('/api/config');assert.equal(savedBefore.status,200);
  // Never intercept/replace a successful response. Only forbid persistent mutations.
  await evaluate(`(()=>{const original=fetch;globalThis.__acceptanceInspects=[];globalThis.fetch=async(url,options={})=>{const path=String(url).split('?')[0],method=(options.method||'GET').toUpperCase();if(method!=='GET'&&!path.startsWith('/api/edit-preview/')&&!path.startsWith('/api/device-preview/'))throw Error('persistent mutation forbidden');const r=await original(url,options);if(['/api/edit-preview/splash','/api/edit-preview/sleep'].includes(path)&&options.body){const body=JSON.parse(options.body);__acceptanceInspects.push({path,selected:body.selected_media_item_id,status:r.status,response:r.ok?await r.clone().json():null})}return r}})()`);
  const roles={}; let predecessor_frame;
  if(input.phase==='rollback'){
   const f=await frame();assert.deepEqual(f.dimensions,[64,32]);assert.ok(f.revision>0);
   const status=await jsonRequest('/api/status');assert.equal(status.status,200);assert.equal(status.body.last_error,null);
   predecessor_frame={dimensions:f.dimensions,revision:f.revision,sha256:hash(f.pixels),png:f.png,http:f.http};
  }else{
   await until("typeof screenMediaCatalog!=='undefined'&&screenMediaCatalog!==null&&document.querySelectorAll('[data-screen-role]').length===2");
   for(const [index,role] of ['splash','sleep'].entries()){
    roleTag=role;frames=freshFrames();at('SELECT'); const before=await evaluate('configBody()');
    const source=await evaluate(`(()=>{const choices=screenMediaCatalog[${JSON.stringify(role)}].choices.filter(c=>c.name.endsWith('.gif'));const eligible=choices.filter(c=>[...document.querySelectorAll('#media-library .media-name')].some(n=>n.textContent===c.name));return (eligible.find(c=>!screenRowForSource(${JSON.stringify(role)},c.name))||eligible[0])?.name})()`);
    assert.ok(source,'existing eligible GIF required; no upload fallback');
    await evaluate(`(()=>{const c=[...document.querySelectorAll('#media-library .media-card')].find(c=>c.querySelector('.media-name').textContent===${JSON.stringify(source)});[...c.querySelectorAll('button')].find(b=>b.textContent===${JSON.stringify('Select for '+role)}).id='acceptance-library-${role}'})()`);
    await click('#acceptance-library-'+role);
    const selector=`#screen-media-${role} .screen-media-choice[data-source=${JSON.stringify(source)}]`;
    const selected=await evaluate(`(()=>{const c=document.querySelector(${JSON.stringify(selector)});return {id:c.dataset.rowId,checked:c.querySelector('.screen-media-selected').checked,inspect:!c.querySelector('.palette-inspect').disabled}})()`);
    assert.ok(selected.id);assert.equal(selected.checked,true);assert.equal(selected.inspect,true);
    at('DRAFT_ASSERT'); const selectedDraft=await evaluate('configBody()');
    assert.deepEqual(selectedDraft.playlist.filter(r=>r.id!==selected.id),before.playlist.filter(r=>r.id!==selected.id));
    assert.deepEqual(selectedDraft.custom_modules,before.custom_modules);assert.deepEqual(selectedDraft.screen_enabled,before.screen_enabled);
    await click(selector+' [data-media-layout="x"]');
    await send('Input.dispatchKeyEvent',{type:'keyDown',key:'a',code:'KeyA',modifiers:2});await send('Input.dispatchKeyEvent',{type:'keyUp',key:'a',code:'KeyA',modifiers:2});await send('Input.insertText',{text:'7'});
    const draft=await evaluate('configBody()'), row=draft.playlist.find(r=>r.id===selected.id);
    assert.equal(row.layout.x,7);assert.deepEqual(row.sources,[source]);
    assert.deepEqual(draft.playlist.filter(r=>r.id!==selected.id),before.playlist.filter(r=>r.id!==selected.id));
    assert.deepEqual(draft.custom_modules,before.custom_modules);assert.deepEqual(draft.screen_enabled,before.screen_enabled);
    at('INSPECT_HTTP'); await click(selector+' .palette-inspect');
    await until(`__acceptanceInspects.some(p=>p.path==='/api/edit-preview/${role}'&&p.selected===${JSON.stringify(selected.id)}&&p.status===200&&p.response?.frames?.length)&&document.querySelector('#preview-frame').textContent.includes('Unsaved draft frame')`);
    const inspector=await evaluate(`__acceptanceInspects.filter(p=>p.path==='/api/edit-preview/${role}'&&p.selected===${JSON.stringify(selected.id)}&&p.status===200).at(-1)`);assert.deepEqual(await evaluate('configBody()'),draft,'inspector must not mutate unsaved browser draft');
    at('INSPECT_DECODE');http={method:'POST',path:'/api/edit-preview/'+role,status:bounded(inspector.status,599)};frames.inspector_count=Math.min(1000000,inspector.response.frames.length); const pixels=[];for(const f of inspector.response.frames){const decoded=await decode(f.png);assert.deepEqual(decoded.dimensions,[64,32]);pixels.push(hash(decoded.pixels));remember('inspector_hashes',pixels.at(-1))}
    // Device route has no selected_media_item_id field. Use a separate UNSAVED
    // singleton ENABLED projection; retain saved row IDs and never alter UI draft.
    const native=structuredClone(draft);for(const r of native.playlist){if(r.module===role)r.enabled=r.id===selected.id}native.screen_enabled[role]=true;
    const generation=index+1, owner=input.nonce.slice(0,32), control={generation,preview_instance:owner};
    at('EARLIER_FRAME'); const earlier=await frame();frames.before_revision=bounded(earlier.revision);
    at('DEVICE_START'); const start=await jsonRequest('/api/device-preview/'+role,{config:native,...control});
    assert.equal(start.status,202);assert.equal(start.body.generation,generation);assert.equal(start.body.active_module,role);assert.equal(start.body.heartbeat_after_seconds,5);controls.push(control);
    await sleep(5100);
    at('HEARTBEAT_OWN'); const hb=await jsonRequest('/api/device-preview/heartbeat',control);assert.equal(hb.status,202);assert.equal(hb.body.generation,generation);assert.equal(hb.body.active,true);
    at('HEARTBEAT_FOREIGN'); const foreign=await jsonRequest('/api/device-preview/heartbeat',{...control,preview_instance:(owner[0]==='0'?'1':'0')+owner.slice(1)});assert.equal(foreign.status,409);
    at('STATUS_ASSERT'); const status=await jsonRequest('/api/status');assert.equal(status.status,200);assert.equal(status.body.last_error,null);assert.equal(status.body.current_module,'device-draft:'+role);
    at('FRAME_POLL');pollStarted=Date.now(); let actual,matched;const end=Date.now()+5000;
    while(Date.now()<end){actual=await frame();matched=hash(actual.pixels);frames.observed_count=Math.min(1000000,frames.observed_count+1);remember('observed_hashes',matched);frames.last_revision=bounded(actual.revision);frames.max_revision=bounded(Math.max(frames.max_revision||0,actual.revision));frames.dimensions=actual.dimensions.map(n=>bounded(n,4096));frames.revision_advanced=actual.revision>earlier.revision;frames.pixels_matched=pixels.includes(matched);frames.match_seen||=frames.revision_advanced&&frames.pixels_matched;frames.elapsed_ms=Math.min(60000,Date.now()-pollStarted);if(actual.revision>earlier.revision&&pixels.includes(matched))break;await sleep(30)}
    at('FRAME_DIMENSIONS_ASSERT');assert.deepEqual(actual.dimensions,[64,32]);at('FRAME_REVISION_ASSERT');assert.ok(actual.revision>earlier.revision);at('FRAME_PIXELS_ASSERT');assert.ok(pixels.includes(matched),'actual native pipeline must match selected inspector pixels');
    at('CANCEL'); const cancel=await jsonRequest('/api/device-preview/cancel',control);assert.equal(cancel.status,202);assert.equal(cancel.body.cancelled,true);controls=[];
    at('DRAFT_AFTER'); const remains=await evaluate('configBody()');assert.deepEqual(remains,draft,'native projection must not mutate unsaved browser draft');
    roles[role]={selected_id:selected.id,source,library_native:true,checkbox:true,layout_x:7,independent:true,inspect_selected:true,inspect_http:inspector.status,png_dimensions:actual.dimensions,inspector_png:inspector.response.frames[pixels.indexOf(matched)].png,native_png:actual.png,inspector_pixels_sha256:matched,frame_pixels_sha256:matched,frame_revision_before:earlier.revision,frame_revision_after:actual.revision,native_http:start.status,heartbeat_http:hb.status,foreign_heartbeat_http:foreign.status,cancel_http:cancel.status,generation,owner,current_module:status.body.current_module,singleton:true};
   }
  }
  at('SAVED_AFTER'); const savedAfter=await jsonRequest('/api/config');assert.equal(savedAfter.status,200);assert.deepEqual(savedAfter.body,savedBefore.body);at('PAGE_ERRORS');assert.equal(errors,0);
  const {cookie,debug_port,base,...identity}=input;
  console.log(JSON.stringify({...identity,browser:true,framebuffer:true,offline:Boolean(input.offline_fixture),version_visible:input.version,page_errors:errors,saved_config_unchanged:true,roles,...(predecessor_frame?{predecessor_dashboard:true,predecessor_frame}:{}),limits:['logical SwapOnVSync frame, not photographic LED proof','owner lease + singleton source + matching pixels, not per-frame owner metadata']}));
 }
 }catch(e){failure=snapshot(e);process.exitCode=2}
finally{
 let cleanupOk=true;
 if(failure)failure.cleanup.attempted=true;
 for(const control of controls){try{await jsonRequest('/api/device-preview/cancel',control)}catch(_){cleanupOk=false}}
 if(send){try{await send('Page.close')}catch(_){cleanupOk=false}}
 if(ws)ws.close();for(const p of pending.values()){clearTimeout(p.timer);p.reject(Error('owned CDP closed'))}pending.clear();
 if(failure){failure.cleanup.completed=cleanupOk;process.stderr.write(JSON.stringify(failure)+'\n')}
}
