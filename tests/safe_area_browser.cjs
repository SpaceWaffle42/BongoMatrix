(async()=>{
// 1.11 Screen Safe Area: Device settings inputs, live dotted yellow preview edge, save + reload.
const debugPort=process.argv[1],appPort=process.argv[2],sleep=ms=>new Promise(r=>setTimeout(r,ms));
const endpoint=await fetch(`http://127.0.0.1:${debugPort}/json/new`,{method:'PUT'}).then(r=>r.json());
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map(),errors=[];let id=0;
socket.onmessage=e=>{const m=JSON.parse(e.data);if(m.id&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result)}else if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails.exception?.description||m.params.exceptionDetails.text)};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>new Promise((resolve,reject)=>{const request=++id;pending.set(request,{resolve,reject});socket.send(JSON.stringify({id:request,method,params}))});
const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw Error(JSON.stringify(result.exceptionDetails));return result.result.value};
const waitFor=async(expression,message)=>{for(let i=0;i<200;i++){try{if(await evaluate(expression))return}catch(_){}await sleep(50)}throw Error((message||'timeout')+': '+expression)};
const assert=async(expression,message)=>{if(!await evaluate(expression))throw Error(message+' '+JSON.stringify(await evaluate(`(()=>{try{return ${expression}}catch(e){return String(e)}})()`)))};
const click=async selector=>{const point=await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)throw Error('Missing control '+${JSON.stringify(selector)});el.scrollIntoView({block:'center',behavior:'instant'});const r=el.getBoundingClientRect();if(!r.width||!r.height)throw Error('Invisible control '+${JSON.stringify(selector)});return {x:r.x+r.width/2,y:r.y+r.height/2}})()`);await send('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...point});await send('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...point})};
const key=async(code,keyName)=>{for(const type of ['rawKeyDown','keyUp'])await send('Input.dispatchKeyEvent',{type,key:keyName,code,windowsVirtualKeyCode:{ArrowRight:39,ArrowDown:40,ArrowLeft:37,PageDown:34,Enter:13}[keyName]})};
const change=async(selector,value)=>evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)throw Error('Missing '+${JSON.stringify(selector)});if(el.type==='checkbox')el.checked=${JSON.stringify(value)};else el.value=${JSON.stringify(value)};el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
try{
 await send('Runtime.enable');await send('Page.enable');
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/pair`});await waitFor(`document.querySelector('form')`);
 await evaluate(`fetch('/pair',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({device_name:'Safe area',pin:'123456'})}).then(()=>true)`);
 const open=async()=>{await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/`});await waitFor(`typeof matrixEditorsReady!=='undefined'&&matrixEditorsReady`,'dashboard not ready')};
 await open();
 const markerShown=`(()=>{const m=document.querySelector('#safe-area-marker');return !!m&&!m.hidden&&m.checkVisibility()})()`;
 // 1. Device settings shows the legacy 2 px safe area and the media toggle on.
 await assert(`document.querySelector('#device #safe-area-setting legend').textContent.trim()==='Screen Safe Area'`,'safe area group missing from Device settings');
 for(const edge of ['top','right','bottom','left'])await assert(`(()=>{const el=document.querySelector('#safe-area-${edge}');return el.value==='2'&&el.min==='0'&&el.max==='6'&&el.step==='1'})()`,'legacy value/limits wrong for '+edge);
 await assert(`document.querySelector('#safe-area-media-inside').checked&&document.querySelector('label.safe-area-media').textContent.includes('Full-screen media stays inside the safe area')`,'media toggle wrong');
 // 2. The marker is never shown when the inputs are not being edited.
 await assert(`!${markerShown}`,'marker visible before editing');
 // 2b. An unrelated save does not send the (projected) safe area (0.3.75 rollback safety).
 await evaluate(`(()=>{window.__configPosts=[];const original=window.fetch;window.fetch=(url,options={})=>{if(String(url).endsWith('/api/config')&&String(options.method).toUpperCase()==='POST')window.__configPosts.push(JSON.parse(options.body));return original(url,options)};return true})()`);
 await change('#device-name','Hall');
 await click('#save-config');await waitFor(`!matrixHasUnsavedDraft()&&window.__configPosts.length===1`,'unrelated save failed');
 const unrelatedSave=await evaluate(`(()=>{const body=window.__configPosts[0];return {name:body.device.name,sentSafeArea:'safe_area' in body.device}})()`);
 // 3. Editing Left shows a dotted, pulsing yellow edge on the preview that follows the unsaved value.
 await click('#safe-area-left');
 await waitFor(markerShown,'marker not shown while editing');
 await change('#safe-area-left','4');
 await assert(`(()=>{const m=document.querySelector('#safe-area-marker'),s=getComputedStyle(m);return m.closest('.screen-wrap')&&m.style.left==='6.25%'&&m.style.top==='6.25%'&&s.borderTopStyle==='dotted'&&s.borderTopColor==='rgb(255, 214, 0)'&&s.animationName==='safe-area-pulse'})()`,'marker does not reflect unsaved left=4');
 const markerWhileEditing=await evaluate(markerShown);
 // 3b. Blank, out-of-range, fractional or negative values are flagged and block saving.
 const invalidAttempts=[];
 for(const value of ['','9','2.7','-1']){
  await change('#safe-area-left',value);
  const before=await evaluate(`window.__configPosts.length`);
  await click('#save-config');await sleep(300);
  invalidAttempts.push(await evaluate(`(()=>{const el=document.querySelector('#safe-area-left'),m=document.querySelector('#message');return {value:${JSON.stringify(value)},ariaInvalid:el.getAttribute('aria-invalid'),valid:el.validity.valid,validationMessage:el.validationMessage,message:m.textContent,error:m.classList.contains('error'),posted:window.__configPosts.length-${before}}})()`));
 }
 await change('#safe-area-left','4');
 const fixedInput=await evaluate(`(()=>{const el=document.querySelector('#safe-area-left');return {ariaInvalid:el.getAttribute('aria-invalid'),valid:el.validity.valid}})()`);
 // 4. Saving (focus leaves the inputs) hides the marker and stores the exact setting.
 await click('#save-config');await waitFor(`!matrixHasUnsavedDraft()`,'save failed');
 await assert(`!${markerShown}`,'marker still visible after saving');
 const saved=await evaluate(`fetch('/api/config').then(r=>r.json()).then(c=>c.device.safe_area)`);
 const changedSentSafeArea=await evaluate(`'safe_area' in window.__configPosts.at(-1).device`);
 // 5. Reload reads the saved values back; marker stays hidden.
 await open();
 const reloaded=await evaluate(`['top','right','bottom','left'].map(e=>document.querySelector('#safe-area-'+e).value).join(',')`);
 await assert(`!${markerShown}`,'marker visible after reload');
 console.log(JSON.stringify({saved,reloaded,markerWhileEditing,unrelatedSave,invalidAttempts,fixedInput,changedSentSafeArea,errors}));
}finally{socket.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
