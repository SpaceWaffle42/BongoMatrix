(async()=>{
const debugPort=process.argv[1],appPort=process.argv[2],sleep=ms=>new Promise(r=>setTimeout(r,ms));
const endpoint=await fetch(`http://127.0.0.1:${debugPort}/json/new`,{method:'PUT'}).then(r=>r.json());
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map(),errors=[];let id=0;
socket.onmessage=e=>{const m=JSON.parse(e.data);if(m.id&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result)}else if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails.text)};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>new Promise((resolve,reject)=>{const request=++id;pending.set(request,{resolve,reject});socket.send(JSON.stringify({id:request,method,params}))});
const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw Error(JSON.stringify(result.exceptionDetails));return result.result.value};
const waitFor=async expression=>{for(let i=0;i<200;i++){try{if(await evaluate(expression))return}catch(_){}await sleep(50)}throw Error('timeout: '+expression)};
const click=async selector=>{const point=await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)throw Error('Missing control '+${JSON.stringify(selector)});el.scrollIntoView({block:'center',behavior:'instant'});const r=el.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()`);await send('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...point});await send('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...point})};
const change=async(selector,value)=>evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});el.value=${JSON.stringify(value)};el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
try{
 await send('Runtime.enable');await send('Page.enable');
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/pair`});await waitFor(`document.querySelector('form')`);await send('Runtime.evaluate',{expression:"localStorage.setItem('matrix.editorAdvanced.v1',JSON.stringify(Object.fromEntries(['weather','openweather_overview','openweather_forecast','clock_lava','rss','astronomy','event_countdown'].map(m=>[m,true]))))"});
 await evaluate(`fetch('/pair',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({device_name:'Countdown browser',pin:'123456'})}).then(()=>true)`);
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/`});await waitFor(`typeof matrixEditorsReady!=='undefined'&&matrixEditorsReady`);
 if(!await evaluate(`!!document.querySelector('[data-module-editor="event_countdown"] [data-countdown-add]')`))throw Error('Countdown Add event control missing from loaded dashboard');
 await click('[data-module="event_countdown"] button:last-child');
 await change('[data-countdown-setting="timezone"]','Pacific/Kiritimati');
 await click('[data-countdown-add]');
 const zoned=await evaluate(`(()=>{const parts=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:'Pacific/Kiritimati',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date()).map(p=>[p.type,p.value]));return parts.year+'-'+parts.month+'-'+parts.day})()`);
 // Reviewer finding 2026-10-13: Add event creates an all-day event for today in the configured zone.
 if(await evaluate(`document.querySelector('[data-countdown-field="start"]').value`)!==zoned)throw Error('New event does not use configured timezone');
 await click('[data-countdown-field="all_day"]');
 if(await evaluate(`document.querySelector('[data-countdown-field="start"]').value`)!==zoned+'T00:00')throw Error('New event did not switch to timed');
 await change('[data-countdown-field="title"]','Browser launch');
 // Offset-bearing instants must be localized before discarding the time.
 for(const [zone,start,end,wantStart,wantEnd] of [
   ['Pacific/Kiritimati','2030-01-01T23:30Z','2030-01-02T00:30Z','2030-01-02','2030-01-03'],
   ['America/Los_Angeles','2030-01-01T01:30+02:00','2030-01-02T08:00Z','2029-12-31','2030-01-02'],
   ['Europe/Berlin','2026-10-25T00:30Z','2026-10-25T01:30Z','2026-10-25','2026-10-26'],
   ['Europe/Berlin','2026-03-29T01:30Z','','2026-03-29',''],
   ['Pacific/Kiritimati','2030-12-31T23:30','2031-01-01T00:00','2030-12-31','2031-01-01'],
   ['UTC','2032-02-29','2032-02-29','2032-02-29','2032-03-01']
 ]){
   await change('[data-countdown-setting="timezone"]',zone);
   await change('[data-countdown-field="start"]',start);await change('[data-countdown-field="end"]',end);
   await click('[data-countdown-field="all_day"]');
   const actual=await evaluate(`['start','end'].map(k=>document.querySelector('[data-countdown-field="'+k+'"]').value)`);
   if(JSON.stringify(actual)!==JSON.stringify([wantStart,wantEnd]))throw Error('Zoned all-day conversion '+JSON.stringify({zone,start,end,actual,wantStart,wantEnd}));
   await click('[data-countdown-field="all_day"]');
   if(await evaluate(`document.querySelector('[data-countdown-field="start"]').value`)!==wantStart+'T00:00')throw Error('Date toggle lost local date');
 }
 await change('[data-countdown-field="end"]','');
 await change('[data-countdown-field="start"]','2030-01-01T12:00');
 await click('[data-countdown-field="all_day"]');
 if(await evaluate(`document.querySelector('[data-countdown-field="start"]').value`)!=='2030-01-01')throw Error('All-day toggle did not convert timestamp');
 await click('[data-countdown-field="all_day"]');
 if(await evaluate(`document.querySelector('[data-countdown-field="start"]').value`)!=='2030-01-01T00:00')throw Error('Timed toggle did not convert date');
 await change('[data-countdown-setting="layout"]','icon');
 if(!await evaluate(`!!document.querySelector('select[data-countdown-field="artwork"] option[value="countdown-art.png"]')`))throw Error('Library artwork selector missing');
 await change('[data-countdown-field="artwork"]','countdown-art.png');
 await evaluate(`reconcileMediaRemoval('countdown-art.png')`);
 if(!await evaluate(`document.querySelector('[data-countdown-field="artwork"]').selectedOptions[0].textContent.includes('(missing)')`))throw Error('Removed artwork not marked missing');
 await evaluate(`refreshMedia()`);
 if(!await evaluate(`document.querySelector('[data-countdown-field="artwork"]').value==='countdown-art.png'&&!document.querySelector('[data-countdown-field="artwork"]').selectedOptions[0].textContent.includes('(missing)')`))throw Error('Library refresh lost artwork selection');
 await click('[data-countdown-add]');await click('.countdown-event:last-child [data-countdown-delete]');
 await click('#save-config');
 await waitFor(`!matrixHasUnsavedDraft()`);
 const result=await evaluate(`fetch('/api/config').then(r=>r.json()).then(c=>c.event_countdown)`);
 if(result.events.length!==1||result.events[0].title!=='Browser launch')throw Error('CRUD save mismatch '+JSON.stringify(result));
 await click('label[for="preview-mode-edit"]');
 await waitFor(`document.querySelector('#preview-frame').textContent.includes('Unsaved draft frame')`);
 await waitFor(`(()=>{if(!editPreviewLastBitmap)return false;const c=document.createElement('canvas');c.width=64;c.height=32;const x=c.getContext('2d');x.drawImage(editPreviewLastBitmap,0,0);const p=x.getImageData(0,9,1,1).data;return p[0]===255&&p[1]===0&&p[2]===255})()`);
 await change('[data-countdown-setting="title_font"]','10x20');
 await change('[data-countdown-setting="countdown_font"]','10x20');
 for(const edge of ['top','bottom','left','right'])await change(`[data-countdown-setting="${edge}"]`,'8');
 await waitFor(`document.querySelector('[data-countdown-warning]').textContent.includes('Title clips')&&document.querySelector('[data-countdown-warning]').textContent.includes('Countdown clips')`);
 const measuredWarning=await evaluate(`document.querySelector('[data-countdown-warning]').textContent`);
 if(!/\d+ × \d+ px/.test(measuredWarning))throw Error('Warning lacks measured geometry: '+measuredWarning);
 await evaluate(`globalThis.oldCountdownGeneration=editPreviewGeneration;globalThis.oldCountdownInfo=structuredClone(countdownPreviewInfo)`);
 await change('[data-countdown-field="title"]','Fit');
 if(!await evaluate(`!document.querySelector('[data-countdown-warning]').textContent.includes('Title clips')`))throw Error('Changed draft kept stale geometry');
 await change('[data-countdown-setting="title_font"]','5x7');
 await change('[data-countdown-setting="countdown_font"]','auto');
 for(const edge of ['top','bottom','left','right'])await change(`[data-countdown-setting="${edge}"]`,'0');
 await waitFor(`document.querySelector('[data-countdown-warning]').textContent.includes('Measured layout fits')`);
 await evaluate(`updateCountdownPreviewInfo(oldCountdownInfo,oldCountdownGeneration)`);
 if(!await evaluate(`document.querySelector('[data-countdown-warning]').textContent.includes('Measured layout fits')`))throw Error('Obsolete generation overwrote measured warning');
 await evaluate(`clearTimeout(editPreviewAnimationTimer);editPreviewAnimationTimer=null;closeEditPreviewFrames();animateEditPreview(editPreviewGeneration,editPreviewController,true)`);
 if(!await evaluate(`editPreviewAnimationTimer===null&&document.querySelector('#preview-frame').textContent.includes('complete')`))throw Error('Completed countdown replays its title pass');
 for(const width of [320,1280]){
   await send('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
   if(!await evaluate(`document.documentElement.scrollWidth<=window.innerWidth`))throw Error('Horizontal overflow at '+width);
 }
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/`});await waitFor(`typeof matrixEditorsReady!=='undefined'&&matrixEditorsReady`);
 const restored=await evaluate(`configBody().event_countdown`);
 // A second real dashboard, not a synthesized config response. Each mutation
 // goes through Save and the real server revision/If-Match contract.
 const peerTarget=await fetch(`http://127.0.0.1:${debugPort}/json/new`,{method:'PUT'}).then(r=>r.json());
 const peerSocket=new WebSocket(peerTarget.webSocketDebuggerUrl),peerPending=new Map();let peerId=0;
 peerSocket.onmessage=e=>{const m=JSON.parse(e.data);if(m.id&&peerPending.has(m.id)){const p=peerPending.get(m.id);peerPending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result)}};
 await new Promise((resolve,reject)=>{peerSocket.onopen=resolve;peerSocket.onerror=reject});
 const peerSend=(method,params={})=>new Promise((resolve,reject)=>{const id=++peerId;peerPending.set(id,{resolve,reject});peerSocket.send(JSON.stringify({id,method,params}))});
 const peerEval=async expression=>{const r=await peerSend('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value};
 const peerWait=async expression=>{for(let i=0;i<200;i++){try{if(await peerEval(expression))return}catch(_){}await sleep(50)}throw Error('peer timeout: '+expression)};
 const titleInput=`document.querySelector('[data-countdown-field="title"]')`;
 const editTitle=value=>`(()=>{const el=${titleInput};el.value=${JSON.stringify(value)};el.dispatchEvent(new Event('input',{bubbles:true}));return true})()`;
 try{
   await peerSend('Page.navigate',{url:`http://127.0.0.1:${appPort}/`});await peerWait(`typeof matrixEditorsReady!=='undefined'&&matrixEditorsReady`);
   const oldRevision=await evaluate('matrixLoadedRevision');
   await peerEval(editTitle('Remote accepted'));if(!(await peerEval('saveConfig()')).saved)throw Error('Peer save failed');
   await evaluate('matrixCheckForNewerConfig()');
   await waitFor(`matrixEditorsReady&&matrixLoadedRevision>${oldRevision}&&${titleInput}.value==='Remote accepted'`);
   if(!await evaluate(`configBody().event_countdown.events[0].title==='Remote accepted'&&!matrixHasUnsavedDraft()`))throw Error('Clean remote reload did not synchronize editor/serializer');
   await click('[data-module="event_countdown"] button:last-child');
   await change('[data-countdown-field="title"]','Local unsaved');await evaluate(`${titleInput}.focus()`);
   await peerEval(editTitle('Remote conflicting'));await peerEval('saveConfig()');
   await evaluate('matrixCheckForNewerConfig()');await waitFor(`document.querySelector('.config-revision-dialog')?.open`);
   if(!await evaluate(`${titleInput}.value==='Local unsaved'&&document.querySelector('#save-config').disabled`))throw Error('Conflict erased draft or did not gate save');
   await click('.config-revision-dialog button:last-child');
   await evaluate('matrixCheckForNewerConfig()');
   if(!await evaluate(`!document.querySelector('.config-revision-dialog')&&${titleInput}.value==='Local unsaved'&&document.activeElement===${titleInput}`))throw Error('Remain lost draft/focus or repeated warning');
   const stale=await evaluate('saveConfig()');if(stale.saved)throw Error('Stale draft overwrote peer config');
   await peerEval(editTitle('Remote latest'));await peerEval('saveConfig()');
   await evaluate('matrixCheckForNewerConfig()');await waitFor(`document.querySelector('.config-revision-dialog')?.open`);
   await click('.config-revision-dialog button:first-child');
   await waitFor(`matrixEditorsReady&&${titleInput}.value==='Remote latest'&&!matrixHasUnsavedDraft()`);
   // Deterministic barrier AFTER a real server commit, BEFORE Save consumes its
   // response. A newer local edit must survive and still block clean reload.
   await evaluate(`(()=>{const realFetch=fetch;globalThis.releaseSave=null;globalThis.saveArrived=false;globalThis.fetch=async(...args)=>{const response=await realFetch(...args);if(args[0]==='/api/config'&&args[1]?.method==='POST'){globalThis.saveArrived=response.status===200;await new Promise(resolve=>globalThis.releaseSave=resolve)}return response};return true})()`);
   await evaluate(editTitle('Barrier submitted'));
   await evaluate(`void(saveConfig().then(result=>globalThis.barrierResult=result))`);await waitFor('saveArrived&&releaseSave');
   await evaluate(editTitle('Newer local edit'));await evaluate('releaseSave()');await waitFor('globalThis.barrierResult');
   if(!await evaluate(`barrierResult.saved&&${titleInput}.value==='Newer local edit'&&configBody().event_countdown.events[0].title==='Newer local edit'&&matrixHasUnsavedDraft()`))throw Error('Save response erased newer countdown edit');
   const stored=await peerEval(`fetch('/api/config').then(r=>r.json())`);
   if(stored.event_countdown.events[0].title!=='Barrier submitted')throw Error('Barrier did not use actual committed snapshot');
   // Reload deliberately discards the test draft; restore original fixture title.
   await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/`});await waitFor(`matrixEditorsReady&&${titleInput}.value==='Barrier submitted'`);
   await evaluate(editTitle('Browser launch'));if(!(await evaluate('saveConfig()')).saved)throw Error('Restore failed');
 }finally{peerSocket.close()}
 console.log(JSON.stringify({title:restored.events[0].title,events:restored.events.length,layout:restored.layout,artwork:restored.events[0].artwork,errors}));
}finally{socket.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
