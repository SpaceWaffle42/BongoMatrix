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
 await evaluate("fetch('/pair',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({device_name:'Compatibility regression',pin:'123456'})}).then(r=>r.ok)");
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/`});await until('typeof matrixEditorsReady!=="undefined"&&matrixEditorsReady&&typeof screenMediaCatalog!=="undefined"&&screenMediaCatalog!==null');
 const draft=await evaluate('configBody()');
 await evaluate('loadScreenMediaCatalog()');const repeated=await evaluate('configBody()');
 const preview=await evaluate(`(async()=>{previewMediaItemId='splash_default';const r=await fetch('/api/edit-preview/splash',{method:'POST',headers:{'Content-Type':'application/json','X-Matrix-Client-Contract':'5','X-Matrix-Action':PAIRED_ACTION_SIGNAL},body:JSON.stringify({config:configBody(true),generation:1,preview_instance:'e'.repeat(32),selected_media_item_id:previewMediaItemId})});const body=await r.json();if(r.ok)await api('/api/edit-preview/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session:body.session,preview_instance:'e'.repeat(32)})});return {status:r.status,error:body.error}})()`);
 await evaluate("globalThis.__saveRequests=[];const original=fetch;globalThis.fetch=async(input,options={})=>{if(String(input)==='/api/config'&&options.method==='POST')__saveRequests.push({match:options.headers['If-Match'],body:JSON.parse(options.body)});return original(input,options)}");
 await click('#save-config');await until("document.querySelector('#message').textContent==='Configuration saved and display reloaded.'||document.querySelector('#message').textContent.includes('usable selected media')");
 const message=await evaluate("document.querySelector('#message').textContent"),requests=await evaluate('__saveRequests');
 const saved=await evaluate("fetch('/api/config',{headers:{'X-Matrix-Client-Contract':'5'}}).then(r=>r.json())");
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/`});await until('typeof matrixEditorsReady!=="undefined"&&matrixEditorsReady&&typeof screenMediaCatalog!=="undefined"&&screenMediaCatalog!==null');
 const reloaded=await evaluate('configBody()');
 console.log(JSON.stringify({errors,draft,repeated,preview,message,requests,saved,reloaded}));
}finally{socket.close()}
