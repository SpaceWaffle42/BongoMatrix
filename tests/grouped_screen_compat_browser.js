const {default:assert}=await import('node:assert/strict');
const debugPort=process.argv[1],appPort=process.argv[2],sleep=ms=>new Promise(r=>setTimeout(r,ms));
const endpoint=await fetch(`http://127.0.0.1:${debugPort}/json/new`,{method:'PUT'}).then(r=>r.json());
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map(),errors=[],diagnostics=[];let id=0;
socket.onmessage=e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result)}else if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails)};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>new Promise((resolve,reject)=>{const request=++id;pending.set(request,{resolve,reject});socket.send(JSON.stringify({id:request,method,params}))});
const evaluate=async expression=>{const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value};
const until=async expression=>{const end=Date.now()+10000;while(Date.now()<end){if(await evaluate(expression))return;await sleep(25)}throw Error('timeout '+expression)};
const click=async (selector,beforeClick)=>{await evaluate(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({block:'center',behavior:'instant'})`);if(beforeClick)await evaluate(beforeClick);await until(`(()=>{const e=document.querySelector(${JSON.stringify(selector)}),r=e.getBoundingClientRect(),h=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return r.y>=0&&r.bottom<=innerHeight&&(h===e||e.contains(h))})()`);const p=await evaluate(`(()=>{const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2}})()`);await send('Input.dispatchMouseEvent',{type:'mousePressed',...p,button:'left',clickCount:1});await send('Input.dispatchMouseEvent',{type:'mouseReleased',...p,button:'left',clickCount:1})};
try{
 await send('Runtime.enable');await send('Page.enable');await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/pair`});await until("document.querySelector('form')&&document.readyState==='complete'");
 await evaluate("fetch('/pair',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({device_name:'Grouped local browser',pin:'123456'})}).then(r=>r.ok)");
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/`});await until('typeof matrixEditorsReady!=="undefined"&&matrixEditorsReady');
 assert.equal(await evaluate("document.querySelectorAll('[data-screen-role]').length"),2,'missing independent grouped Splash/Sleep cards after shipped dashboard load');
 await until('typeof screenMediaCatalog!=="undefined"&&screenMediaCatalog!==null');
 const persisted=await evaluate("fetch('/api/config',{headers:{'X-Matrix-Client-Contract':'5'}}).then(r=>r.json())");
 const initial=await evaluate('configBody()'),capacity=persisted.playlist.length===256;
 assert.ok(initial.custom_modules.length&&initial.custom_modules[0].layers.length,'nonempty Canvas prerequisite');
 assert.deepEqual(initial.custom_modules,persisted.custom_modules);
 const original=persisted.playlist.find(r=>r.id==='splash_default');
 for(const id of ['splash_duplicate','splash_unavailable'])assert.deepEqual(initial.playlist.find(r=>r.id===id),persisted.playlist.find(r=>r.id===id));
 const originalIds=persisted.playlist.filter(r=>r.id).map(r=>r.id);
 assert.deepEqual(initial.playlist.filter(r=>originalIds.includes(r.id)).map(r=>r.id),originalIds,'relative owning order');
 if(capacity){
  assert.deepEqual(initial.playlist,persisted.playlist,'split preflight must reject all-or-nothing');
  await evaluate("setScreenMediaSelection('splash','all')");
  assert.deepEqual(await evaluate('configBody().playlist'),initial.playlist,'bulk capacity rejection must not partially mutate');
  for(const role of ['splash','sleep']){
   await evaluate(`(()=>{const card=[...document.querySelectorAll('#media-library .media-card')].find(c=>c.querySelector('.media-name').textContent==='removable.gif');[...card.querySelectorAll('button')].find(b=>b.textContent==='Select for ${role}').id='capacity-library-${role}'})()`);
   await click('#capacity-library-'+role);
   assert.deepEqual(await evaluate('configBody()'),initial,'library capacity refusal must be all-or-nothing');
   assert.match(await evaluate("document.querySelector('#message').textContent"),/256/,'capacity refusal must explain the hold, not claim selected');
  }
 }else{
  const legacy='built-in/loading_squares.gif',retained=initial.playlist.find(r=>r.id===original.id),split=initial.playlist.find(r=>r.module==='splash'&&r.sources[0]===original.sources[0]&&!originalIds.includes(r.id)),start=initial.playlist.indexOf(retained)-original.sources.indexOf(legacy),materialized=initial.playlist.slice(start,start+original.sources.length);
  assert.deepEqual(retained.sources,[legacy]);assert.deepEqual(retained.layout,original.layout);assert.equal(retained.enabled,original.enabled);assert.ok(split&&split.id!==retained.id);assert.deepEqual(split.sources,[original.sources[0]]);assert.deepEqual(split.layout,original.layout);assert.equal(split.enabled,original.enabled);
  assert.equal(materialized.length,original.sources.length);assert.deepEqual(materialized.map(r=>r.sources),original.sources.map(source=>[source]));assert.equal(new Set(materialized.map(r=>r.id)).size,original.sources.length);for(const row of materialized){assert.equal(row.module,original.module);assert.deepEqual(row.layout,original.layout);assert.equal(row.enabled,original.enabled);if(row!==retained)assert.ok(row.id&&!originalIds.includes(row.id));}assert.deepEqual(initial.playlist.flatMap(r=>r.sources||[]),persisted.playlist.flatMap(r=>r.sources||[]),'exact flattened source counts/order');
  const selector='#screen-media-splash .screen-media-choice[data-source="removable.gif"] .screen-media-selected';
  await evaluate(`globalThis.__first=document.querySelector(${JSON.stringify(selector)});__first.focus({preventScroll:true})`);
  await click(selector,"globalThis.__grid=__first.closest('.palette-grid');globalThis.__fresh=__first.closest('.screen-media-choice');globalThis.__freshCheck=__first;if(__fresh.dataset.rowId)throw Error('unmaterialized upload prerequisite');__grid.scrollTop=37;if(__grid.scrollTop!==37)throw Error('positive local scroll prerequisite')");
  diagnostics.push(await evaluate(`({connected:__first.isConnected,focused:document.activeElement===__first,activeElement:document.activeElement.outerHTML.slice(0,300),oldRowId:__first.closest('.screen-media-choice')?.dataset.rowId,newRowId:document.querySelector(${JSON.stringify(selector)}).closest('.screen-media-choice').dataset.rowId})`));
  assert.equal(await evaluate('__first.isConnected&&document.activeElement===__first'),true,'first selection must preserve checkbox focus/node');
  assert.equal(await evaluate('__freshCheck.isConnected&&document.activeElement===__freshCheck&&__grid.scrollTop===37'),true,'first upload selection preserves node/focus/positive local scroll');
  assert.equal(await evaluate('__fresh.dataset.rowId===screenRowForSource("splash",__fresh.dataset.source).dataset.itemId'),true,'materialized card pins actual owner');
  await evaluate('__freshCheck.click()');
  assert.equal(await evaluate('__freshCheck.isConnected&&document.activeElement===__freshCheck&&__grid.scrollTop===37'),true,'toggle preserves node/focus/positive local scroll');
  for(const mode of ['all','defaults','none']){
   await evaluate(`setScreenMediaSelection('splash',${JSON.stringify(mode)})`);
   assert.equal(await evaluate('__grid.scrollTop'),37,'bulk local scroll');
   assert.equal(await evaluate('__freshCheck.isConnected&&document.activeElement===__freshCheck'),true,'bulk preserves owning checkbox/focus');
  }
  const beforeRefresh=await evaluate('configBody()');await evaluate('loadScreenMediaCatalog()');
  assert.deepEqual(await evaluate('configBody()'),beforeRefresh,'refresh retains identities/layout/disabled states/Canvas');
 }
 const beforeRemoval=await evaluate('configBody()');
 await evaluate("globalThis.confirm=()=>true;removeMedia('removable.gif')");
 await until("!screenMediaCatalog.splash.choices.some(c=>c.name==='removable.gif')");
 assert.equal(await evaluate("document.querySelectorAll('.screen-media-choice[data-source=\"removable.gif\"]').length"),0,'actual deletion refreshes grouped catalog');
 const afterRemoval=await evaluate('configBody()');
 assert.deepEqual(afterRemoval.custom_modules,beforeRemoval.custom_modules);
 for(const id of ['splash_duplicate','splash_unavailable'])assert.deepEqual(afterRemoval.playlist.find(r=>r.id===id),beforeRemoval.playlist.find(r=>r.id===id));
 console.log(JSON.stringify({errors,capacity,diagnostics}));
}finally{socket.close()}
