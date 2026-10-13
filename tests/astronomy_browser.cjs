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
 await evaluate(`fetch('/pair',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({device_name:'Astronomy browser',pin:'123456'})}).then(()=>true)`);
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/`});await waitFor(`typeof matrixEditorsReady!=='undefined'&&matrixEditorsReady`);
 if(!await evaluate(`!!document.querySelector('[data-astronomy-setting="timezone"]')`))throw Error('Astronomy editor absent from loaded dashboard');
 await click('[data-module="astronomy"] button:last-child');
 await change('[data-astronomy-setting="timezone"]','Europe/London');
 await change('[data-astronomy-setting="location_source"]','override');
 await change('[data-astronomy-setting="latitude"]','51.5');
 await change('[data-astronomy-setting="longitude"]','-0.1');
 await change('[data-astronomy-setting="location_label"]','London test');
 await change('[data-astronomy-setting="title_font"]','5x7');
 await change('[data-astronomy-setting="moon_hold_seconds"]','3');
 // Owner: separate title/date/description colours, each a labelled colour picker.
 for(const [key,label] of [['title_colour','Title colour'],['date_colour','Date colour'],['description_colour','Description colour']]){
  if(!await evaluate(`(()=>{const el=document.querySelector('[data-astronomy-setting="${key}"]');return !!el&&el.type==='color'&&el.closest('label').textContent.includes('${label}')})()`))throw Error('Missing colour control '+key);
 }
 await change('[data-astronomy-setting="title_colour"]','#ff0000');
 await change('[data-astronomy-setting="date_colour"]','#00ff00');
 await change('[data-astronomy-setting="description_colour"]','#0000ff');
 await click('[data-astronomy-setting="lunar_events"]');
 await click('[data-astronomy-calculate]');
 await waitFor(`document.querySelector('[data-astronomy-timing]').textContent.includes('ready')`);
 if(!await evaluate(`document.querySelector('[data-astronomy-note]').textContent.includes('not implemented')`))throw Error('Unsupported capabilities not disclosed');
 if(await evaluate(`!!document.querySelector('[data-astronomy-setting="meteor_showers"]')`))throw Error('Fake enabled capability');
 await click('#save-config');await waitFor(`!matrixHasUnsavedDraft()`);
 const saved=await evaluate(`fetch('/api/config').then(r=>r.json()).then(c=>c.astronomy)`);
 if(saved.timezone!=='Europe/London'||!saved.lunar_events||saved.moon_hold_seconds!==3||saved.latitude!==51.5)throw Error('Native Save mismatch');
 if(saved.title_colour!=='#ff0000'||saved.date_colour!=='#00ff00'||saved.description_colour!=='#0000ff')throw Error('Role colours not saved');
 await click('label[for="preview-mode-edit"]');
 await waitFor(`document.querySelector('#preview-frame').textContent.includes('Unsaved draft frame')`);
 if(!await evaluate(`editPreviewLastBitmap.width===64&&editPreviewLastBitmap.height===32`))throw Error('Preview raster not 64x32');
 for(const width of [320,768,1280]){
  await send('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
  if(!await evaluate(`document.documentElement.scrollWidth<=window.innerWidth`))throw Error('Horizontal overflow at '+width);
 }
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/`});await waitFor(`typeof matrixEditorsReady!=='undefined'&&matrixEditorsReady`);
 if(!await evaluate(`configBody().astronomy.lunar_events&&configBody().astronomy.timezone==='Europe/London'`))throw Error('Reload lost settings');
 await click('[data-module="astronomy"] button:last-child');
 await click('[data-astronomy-setting="current_moon"]');await click('[data-astronomy-setting="lunar_events"]');
 await click('label[for="preview-mode-edit"]');
 await waitFor(`document.querySelector('#preview-frame').textContent.includes('complete')`);
 if(!await evaluate(`editPreviewAnimationTimer===null`))throw Error('Empty astronomy preview is hot-looping');
 console.log(JSON.stringify({errors,saved,widths:[320,768,1280]}));
}finally{socket.close()}
})().catch(error=>{console.error(error.stack);process.exitCode=1});
