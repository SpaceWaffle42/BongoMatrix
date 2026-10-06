const {default:assert}=await import('node:assert/strict');
const debugPort=process.argv[1],appPort=process.argv[2],sleep=ms=>new Promise(r=>setTimeout(r,ms));
const endpoint=await fetch(`http://127.0.0.1:${debugPort}/json/new`,{method:'PUT'}).then(r=>r.json());
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map(),errors=[];let id=0;
socket.onmessage=e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result)}else if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails)};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>new Promise((resolve,reject)=>{const request=++id;pending.set(request,{resolve,reject});socket.send(JSON.stringify({id:request,method,params}))});
const evaluate=async expression=>{const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value};
const until=async expression=>{const end=Date.now()+10000;while(Date.now()<end){if(await evaluate(expression))return;await sleep(25)}throw Error('timeout '+expression)};
const click=async selector=>{await evaluate(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({block:'center',behavior:'instant'})`);await until(`(()=>{const e=document.querySelector(${JSON.stringify(selector)}),r=e.getBoundingClientRect(),h=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return r.y>=0&&r.bottom<=innerHeight&&(h===e||e.contains(h))})()`);const p=await evaluate(`(()=>{const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2}})()`);await send('Input.dispatchMouseEvent',{type:'mousePressed',...p,button:'left',clickCount:1});await send('Input.dispatchMouseEvent',{type:'mouseReleased',...p,button:'left',clickCount:1})};
try{
 await send('Runtime.enable');await send('Page.enable');await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/pair`});await until("document.querySelector('form')&&document.readyState==='complete'");
 await evaluate("fetch('/pair',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({device_name:'Grouped local browser',pin:'123456'})}).then(r=>r.ok)");
 const hold=await send('Page.addScriptToEvaluateOnNewDocument',{source:"(()=>{const original=fetch;let release;const held=new Promise(resolve=>release=resolve);globalThis.__releaseCatalog=release;globalThis.fetch=async(input,options)=>{const response=await original(input,options);if(String(input)==='/api/screen-media')await held;return response}})()"});
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/`});await until('typeof matrixEditorsReady!=="undefined"&&matrixEditorsReady&&typeof screenMediaCatalog!=="undefined"');
 assert.equal(await evaluate("document.querySelectorAll('[data-screen-role]').length"),2,'missing independent grouped Splash/Sleep cards after shipped dashboard load');
 const heldDraft=await evaluate('configBody()');
 assert.equal(await evaluate('screenMediaCatalog'),null,'real catalog response must be held');
 for(const role of ['splash','sleep']){
  await evaluate(`assignMedia('${role}','grouped_upload.gif')`);
  assert.deepEqual(await evaluate('configBody()'),heldDraft,'early public selection must retain the complete draft');
  assert.match(await evaluate("document.querySelector('#message').textContent"),/loading.*Retry/);
 }
 await evaluate('__releaseCatalog()');await send('Page.removeScriptToEvaluateOnNewDocument',{identifier:hold.identifier});
 await until('typeof screenMediaCatalog!=="undefined"&&screenMediaCatalog!==null');
 const initial=await evaluate('configBody()');
 for(const role of ['splash','sleep']){
  await evaluate(`assignMedia('${role}','missing.gif')`);
  assert.deepEqual(await evaluate('configBody()'),initial,'missing/ineligible public selection must not mutate');
  assert.match(await evaluate("document.querySelector('#message').textContent"),/not available/);
 }
 for(const role of ['splash','sleep']){
  const before=await evaluate('configBody()');
  await evaluate(`(()=>{const card=[...document.querySelectorAll('#media-library .media-card')].find(c=>c.querySelector('.media-name').textContent==='grouped_upload.gif');[...card.querySelectorAll('button')].find(b=>b.textContent==='Select for ${role}').id='native-library-${role}'})()`);
  await click('#native-library-'+role);
  const selector=`#screen-media-${role} .screen-media-choice[data-source="grouped_upload.gif"]`;
  const state=await evaluate(`(()=>{const c=document.querySelector(${JSON.stringify(selector)});return {checked:c.querySelector('.screen-media-selected').checked,id:c.dataset.rowId,disabled:c.querySelector('.palette-inspect').disabled}})()`);
  assert.equal(state.checked,true,'native library selection must check the owning grouped '+role+' choice');
  assert.ok(state.id);assert.equal(state.disabled,false);
  const selected=await evaluate('configBody()'),owner=selected.playlist.find(r=>r.id===state.id);
  assert.notEqual(state.id,role+'_default');assert.deepEqual(owner.sources,['grouped_upload.gif']);
  assert.deepEqual(selected.playlist.filter(r=>r.id!==state.id),before.playlist,'library action must preserve old IDs/order/layout/opposite/Canvas rows');
  assert.deepEqual(selected.custom_modules,before.custom_modules);assert.deepEqual(selected.screen_enabled,before.screen_enabled);
  await click(selector+' [data-media-layout="x"]');
  await send('Input.dispatchKeyEvent',{type:'keyDown',key:'a',code:'KeyA',modifiers:2});await send('Input.dispatchKeyEvent',{type:'keyUp',key:'a',code:'KeyA',modifiers:2});
  await send('Input.insertText',{text:'7'});
  const edited=await evaluate('configBody()');assert.equal(edited.playlist.find(r=>r.id===state.id).layout.x,7);
  assert.deepEqual(edited.playlist.filter(r=>r.id!==state.id),before.playlist);
  await evaluate(`(()=>{globalThis.__libraryRequests=[];const priorFetch=fetch;globalThis.fetch=async(input,options={})=>{if(String(input)==='/api/edit-preview/${role}')__libraryRequests.push(JSON.parse(options.body));return priorFetch(input,options)}})()`);
  await click(selector+' .palette-inspect');
  await until(`__libraryRequests.length&&document.querySelector('#preview-frame').textContent.includes('Unsaved draft frame')&&previewModule==='${role}'`);
  assert.equal(await evaluate('__libraryRequests.at(-1).selected_media_item_id'),state.id);
  assert.deepEqual(await evaluate('[...ctx.getImageData(10,10,1,1).data]'),[255,0,0,255],'library Inspect must render uploaded red pixels');
 }
 const role='splash',source='grouped_upload.gif';
 await click('#screen-media-splash button[data-screen-action="all"]');
 const cards=await evaluate("[...document.querySelectorAll('#screen-media-splash .screen-media-choice')].map(c=>({source:c.dataset.source,id:c.dataset.rowId}))");
 const upload=cards.find(c=>c.source===source),builtin=cards.find(c=>c.source==='built-in/splash_loading_squares.gif');
 assert.ok(upload&&builtin);assert.notEqual(upload.id,builtin.id,'per-choice cards must not share pooled layout owner');
 const selector='#screen-media-splash .screen-media-choice[data-row-id="'+upload.id+'"]';
 const beforeEdit=await evaluate('configBody()');
 await evaluate(`(()=>{const c=document.querySelector(${JSON.stringify(selector)});for(const [field,value]of [['x',7],['playback_speed_percent',125]]){const i=c.querySelector('[data-media-layout="'+field+'"]');i.value=value;i.dispatchEvent(new Event('input',{bubbles:true}))}})()`);
 const edited=await evaluate('configBody()');
 assert.deepEqual(edited.playlist.filter(r=>r.id!==upload.id),beforeEdit.playlist.filter(r=>r.id!==upload.id),'editing one choice changed sibling/opposite/unrelated rows');
 assert.equal(edited.playlist.find(r=>r.id===upload.id).layout.x,7);assert.equal(edited.playlist.find(r=>r.id===upload.id).layout.playback_speed_percent,125);
 await evaluate(`(()=>{const c=document.querySelector(${JSON.stringify(selector)}),g=c.closest('.palette-grid');g.style.maxHeight='180px';g.style.overflow='auto';g.scrollTop=37;globalThis.__choice=c;globalThis.__check=c.querySelector('.screen-media-selected');__check.focus({preventScroll:true});globalThis.__scroll=g.scrollTop;if(g.scrollHeight<=g.clientHeight||__scroll!==37)throw Error('positive grouped overflow seed required '+g.scrollHeight+'/'+g.clientHeight+'/'+__scroll)})()`);
 await evaluate('__check.click()');
 assert.equal(await evaluate('__check.isConnected&&document.activeElement===__check&&__choice.closest(".palette-grid").scrollTop===__scroll'),true,'checkbox must retain DOM/focus/local scroll');
 await evaluate('__check.click()');
 await evaluate(`(()=>{const b=[...__choice.querySelectorAll('button')].find(b=>b.textContent==='Reset layout');globalThis.__reset=b;b.focus({preventScroll:true});b.click()})()`);
 assert.equal(await evaluate('__reset.isConnected&&document.activeElement===__reset&&__choice.closest(".palette-grid").scrollTop===__scroll'),true,'reset must retain focus/local scroll');
 const reset=await evaluate('configBody()');assert.equal(reset.playlist.find(r=>r.id===upload.id).layout.x,0);assert.equal(reset.playlist.find(r=>r.id===upload.id).layout.playback_speed_percent,100);
 assert.deepEqual(reset.playlist.filter(r=>r.id!==upload.id),beforeEdit.playlist.filter(r=>r.id!==upload.id));
 await evaluate("__choice.closest('.palette-grid').style.maxHeight='';__choice.closest('.palette-grid').style.overflow=''");
 await evaluate("globalThis.__inspectRequests=[];globalThis.__inspectResponses=[];const original=fetch;globalThis.fetch=async(input,options={})=>{const inspect=String(input)==='/api/edit-preview/splash';if(inspect)__inspectRequests.push(JSON.parse(options.body));const response=await original(input,options);if(inspect)__inspectResponses.push({status:response.status,body:response.ok?'frame':await response.clone().text()});return response}");
 await click(selector+' .palette-inspect');
 try{await until("__inspectRequests.length&&document.querySelector('#preview-frame').textContent.includes('Unsaved draft frame')")}catch(error){throw Error(error.message+' '+JSON.stringify(await evaluate('__inspectResponses')))}
 const inspected=await evaluate('__inspectRequests.at(-1)');assert.equal(inspected.selected_media_item_id,upload.id);assert.deepEqual(inspected.config.playlist.find(r=>r.id===upload.id).sources,[source]);
 assert.deepEqual(await evaluate("[...ctx.getImageData(10,10,1,1).data]"),[255,0,0,255],'Inspect must render the selected uploaded source, not random sibling');
 for(const role of ['splash','sleep']){
  assert.equal(await evaluate(`document.querySelectorAll('#screen-media-${role} [data-screen-group]').length`),2);
  assert.equal(await evaluate(`document.querySelectorAll('#screen-media-${role} .duration').length`),0);
  const other=role==='splash'?'sleep':'splash';
  for(const mode of ['all','none','defaults']){
   const before=await evaluate('configBody()');await click(`#screen-media-${role} button[data-screen-action="${mode}"]`);const after=await evaluate('configBody()');
   assert.deepEqual(after.playlist.filter(r=>r.module===other),before.playlist.filter(r=>r.module===other));
   assert.ok(after.playlist.some(r=>r.module===role&&r.enabled&&r.sources.length),'minimum selection required');
  }
 }
 await click('#screen-enabled-splash');const draft=await evaluate('configBody()');assert.equal(draft.screen_enabled.splash,false);assert.equal(draft.screen_enabled.sleep,true);
 for(const row of initial.playlist)assert.ok(draft.playlist.some(r=>r.id? r.id===row.id:r.module===row.module),'existing row identity retained');
 assert.deepEqual(draft.custom_modules,initial.custom_modules,'Canvas/custom modules must be retained');
 await click('#save-config');await until("document.querySelector('#message').textContent==='Configuration saved and display reloaded.'");
 const saved=await evaluate("fetch('/api/config',{headers:{'X-Matrix-Client-Contract':String(MATRIX_CLIENT_CONTRACT)}}).then(r=>r.json())");
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/`});await until('typeof matrixEditorsReady!=="undefined"&&matrixEditorsReady&&typeof screenMediaCatalog!=="undefined"&&screenMediaCatalog!==null');
 const reloaded=await evaluate('configBody()');
 await send('Emulation.setDeviceMetricsOverride',{width:390,height:900,deviceScaleFactor:1,mobile:true});await until('innerWidth===390');
 assert.ok(await evaluate('document.documentElement.scrollWidth<=innerWidth'),'phone overflow');
 console.log(JSON.stringify({errors,draft,saved,reloaded,viewport:390}));
}finally{socket.close()}
