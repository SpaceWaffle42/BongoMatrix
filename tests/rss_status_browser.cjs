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
 await evaluate(`fetch('/pair',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({device_name:'RSS browser',pin:'123456'})}).then(()=>true)`);
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/`});await waitFor(`typeof matrixEditorsReady!=='undefined'&&matrixEditorsReady`);
 if(!await evaluate(`!!document.querySelector('[data-rss-add]')`))throw Error('RSS Add feed control missing from loaded dashboard');
 await click('[data-module="rss"] button:last-child');
 await waitFor(`document.querySelector('[data-rss-status]').textContent.includes('showing cache from')`);
 if(!await evaluate(`document.querySelector('[data-rss-status]').textContent.includes('error')&&document.querySelector('[data-rss-status] details summary').textContent==='Details'`))throw Error('Stale/error accessible summary missing');
 if(!await evaluate(`!!document.querySelector('[data-rss-test-all]')`))throw Error('Test feeds recovery action missing');
 await click('[data-rss-test-all]');
 await waitFor(`Array.from(document.querySelectorAll('[data-rss-row] [role="status"]')).every(el=>el.textContent.includes('Feed rejected:'))`);
 await change('[data-rss-setting="include"]','UnmatchedKeyword');
 await click('label[for="preview-mode-edit"]');
 await waitFor(`document.querySelector('#preview-frame').textContent.includes('Draft error')`);
 if(!await evaluate(`document.querySelector('#preview-frame').textContent.includes('No prepared eligible stories')&&configBody().rss.include[0]==='UnmatchedKeyword'&&matrixHasUnsavedDraft()`))throw Error('Empty preparation lost draft or explanation');
 const stopped=await evaluate('editPreviewGeneration');await sleep(600);
 if(await evaluate('editPreviewGeneration')!==stopped)throw Error('Empty preparation hot-looped');
 console.log(JSON.stringify({errors}));
}finally{socket.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
