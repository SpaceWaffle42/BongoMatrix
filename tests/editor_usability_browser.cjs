(async()=>{
const debugPort=process.argv[1],appPort=process.argv[2],sleep=ms=>new Promise(r=>setTimeout(r,ms));
const endpoint=await fetch(`http://127.0.0.1:${debugPort}/json/new`,{method:'PUT'}).then(r=>r.json());
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map(),errors=[];let id=0;
socket.onmessage=e=>{const m=JSON.parse(e.data);if(m.id&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result)}else if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails.text)};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>new Promise((resolve,reject)=>{const request=++id;pending.set(request,{resolve,reject});socket.send(JSON.stringify({id:request,method,params}))});
const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw Error(JSON.stringify(result.exceptionDetails));return result.result.value};
const waitFor=async expression=>{for(let i=0;i<200;i++){if(await evaluate(expression))return;await sleep(50)}throw Error('timeout: '+expression)};
const point=async selector=>evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)throw Error('Missing '+${JSON.stringify(selector)});el.scrollIntoView({block:'center',behavior:'instant'});const r=el.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;if(x<0||y<0||x>innerWidth||y>innerHeight||!el.contains(document.elementFromPoint(x,y)))throw Error('Occluded '+${JSON.stringify(selector)});return {x,y}})()`);
const click=async selector=>{const p=await point(selector);await send('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...p});await send('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...p})};
const change=async(selector,value)=>evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});el.value=${JSON.stringify(value)};el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
const assert=async(expression,message)=>{if(!await evaluate(expression))throw Error(message)};
const key=async(key,code,n)=>{await send('Input.dispatchKeyEvent',{type:'keyDown',key,code,windowsVirtualKeyCode:n,...(key==='Enter'?{text:'\r'}:{})});await send('Input.dispatchKeyEvent',{type:'keyUp',key,code,windowsVirtualKeyCode:n})};
try{
 await send('Runtime.enable');await send('Page.enable');
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/pair`});await waitFor(`!!document.querySelector('form')`);
 await evaluate(`fetch('/pair',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({device_name:'Editor UX browser',pin:'123456'})}).then(()=>true)`);
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/`});await waitFor(`typeof matrixEditorsReady!=='undefined'&&matrixEditorsReady`);
 const scenario='__SCENARIO__';let zones=null;
 if(scenario==='groups'){
  for(const [module,names] of [['rss',['Feeds','Story selection','Playback','Appearance','Status and actions']],['astronomy',['Content','Location','Playback','Appearance','Status and actions']]]){
   await click(`[data-module="${module}"] button:last-child`);
   await assert(`JSON.stringify([...document.querySelector('[data-module-editor="${module}"]').querySelectorAll(':scope > .editor-group > h3, :scope > .editor-group > summary')].map(e=>e.textContent))===${JSON.stringify(JSON.stringify(names))}`,module+' semantic grouped sections absent');
   const appearance=`[data-module-editor="${module}"] details.editor-group`;
   await assert(`!document.querySelector('${appearance}').open`,'Appearance must start collapsed');
   await click(appearance+' > summary');await assert(`document.querySelector('${appearance}').open`,'Native disclosure failed');
   await key('Enter','Enter',13);await assert(`!document.querySelector('${appearance}').open`,'Keyboard disclosure failed');
   for(const width of [390,768,1280]){
    await send('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
    await point(`[data-module-editor="${module}"] > .editor-group > h3`);
    await assert(`document.documentElement.scrollWidth<=document.documentElement.clientWidth`,'Editor overflow at '+width);
    const shot=await send('Page.captureScreenshot',{format:'png'});require('node:fs').writeFileSync('/tmp/matrix-editor-'+module+'-'+width+'.png',Buffer.from(shot.data,'base64'));
   }
  }
  await assert(`!document.querySelector('[data-astronomy-setting="latitude"]').checkVisibility()`,'Weather mode exposes irrelevant override fields');
  await change('[data-astronomy-setting="location_source"]','override');
  await assert(`document.querySelector('[data-astronomy-setting="latitude"]').checkVisibility()`,'Override coordinates unreachable');
  await change('[data-astronomy-setting="latitude"]','23');await change('[data-astronomy-setting="location_source"]','weather');await change('[data-astronomy-setting="location_source"]','override');
  await assert(`configBody().astronomy.latitude===23`,'Switching source erased override');
 }
 if(scenario==='zones'){
  await click('#save-config');await waitFor('!matrixHasUnsavedDraft()');
  await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/`});await waitFor(`typeof matrixEditorsReady!=='undefined'&&matrixEditorsReady`);
  await assert(`configBody().astronomy.timezone==='Etc/UTC'&&configBody().event_countdown.timezone==='Etc/UTC'`,'Saving changed untouched zone aliases');
  zones=await evaluate(`Array.from(document.querySelector('[data-astronomy-setting="timezone"]').options,o=>o.value).filter(Boolean)`);
  for(const [module,keyName,saved] of [['astronomy','astronomy','Etc/UTC'],['event_countdown','countdown','Etc/UTC']]){
   await click(`[data-module="${module}"] button:last-child`);
   const selector=`[data-${keyName}-setting="timezone"]`;
   await assert(`document.querySelector('${selector}').tagName==='SELECT'`,'Timezone remains free text');
   await assert(`document.querySelector('${selector}').value===${JSON.stringify(saved)}`,'Saved server-supported zone lost');
   await assert(`document.querySelector('${selector}').options.length>400&&[...document.querySelector('${selector}').options].some(o=>o.value==='UTC')`,'Incomplete authoritative zones');
   await evaluate(`document.querySelector('${selector}').focus()`);await key('Home','Home',36);await key('ArrowDown','ArrowDown',40);await key('Tab','Tab',9);
   await assert(`document.querySelector('${selector}').value==='UTC'&&configBody().${module}.timezone==='UTC'`,'Native selection did not update draft');
  }
  await click('#save-config');await waitFor('!matrixHasUnsavedDraft()');
  await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/`});await waitFor(`typeof matrixEditorsReady!=='undefined'&&matrixEditorsReady`);
  await assert(`configBody().astronomy.timezone==='UTC'&&configBody().event_countdown.timezone==='UTC'`,'Zone save/reload failed');
  await click('[data-module="astronomy"] button:last-child');await change('[data-astronomy-setting="timezone"]','');await click('[data-astronomy-calculate]');
  // Blank astronomy zone = follow Weather (default London location carries Europe/London).
  await waitFor(`!document.querySelector('[data-astronomy-timing]').textContent.includes('Preparing')&&!document.querySelector('[data-astronomy-timing]').textContent.includes('not calculated')`);
  await assert(`!document.querySelector('[data-astronomy-timing]').textContent.includes('location_required')`,'Blank zone did not follow Weather');
  await assert(`document.querySelector('[data-astronomy-setting="timezone"]').selectedOptions[0].textContent==='Same as Weather (Europe/London)'&&configBody().astronomy.timezone===''`,'Follow-Weather default not shown or override stored');
  await evaluate(`document.querySelector('#weather-timezone').value='';astronomyWeatherChanged();true`);
  await assert(`document.querySelector('[data-astronomy-setting="timezone"]').selectedOptions[0].textContent.startsWith('Same as Weather (unknown')`,'Unknown Weather zone not explained');
  await click('[data-astronomy-calculate]');
  await waitFor(`document.querySelector('[data-astronomy-timing]').textContent.includes('location_required')`);
 }
 if(scenario==='help'){
  await click('[data-module="rss"] button:last-child');await assert(`!!document.querySelector('[data-help="feed-enabled"]')`,'Contextual help missing');
  const selector='[data-help="feed-enabled"]',before=await evaluate('JSON.stringify(configBody().rss)');
  await assert(`document.querySelector('${selector}').type==='button'&&!document.querySelector('${selector}').closest('label')`,'Help toggles parent checkbox');
  await evaluate(`document.querySelector('[data-rss-enabled]').focus()`);await key('Tab','Tab',9);
  await assert(`document.activeElement===document.querySelector('${selector}')&&document.querySelector('${selector}').getAttribute('aria-expanded')==='true'`,'Keyboard focus help missing');
  await key('Escape','Escape',27);await assert(`document.querySelector('${selector}').getAttribute('aria-expanded')==='false'`,'Escape dismissal failed');
  for(const width of [390,768,1280]){
   await evaluate(`document.activeElement.blur()`);
   await send('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
   const p=await point(selector);await send('Input.dispatchMouseEvent',{type:'mouseMoved',...p});
   await assert(`document.querySelector('${selector}').getAttribute('aria-expanded')==='true'`,'Hover help missing');
   await assert(`(()=>{const button=document.querySelector('${selector}'),e=document.getElementById(button.getAttribute('aria-controls'));const r=e.getBoundingClientRect();return e.checkVisibility()&&r.left>=0&&r.right<=document.documentElement.clientWidth&&button.getBoundingClientRect().width<=48&&document.documentElement.scrollWidth<=innerWidth})()`,'Help clipped by scrollbar or not beside control at '+width);
   const shot=await send('Page.captureScreenshot',{format:'png'});require('node:fs').writeFileSync('/tmp/matrix-editor-help-'+width+'.png',Buffer.from(shot.data,'base64'));
   const helpPoint=await evaluate(`(()=>{const e=document.getElementById(document.querySelector('${selector}').getAttribute('aria-controls')),r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()`);
   await send('Input.dispatchMouseEvent',{type:'mouseMoved',...helpPoint});await sleep(200);
   await assert(`document.querySelector('${selector}').getAttribute('aria-expanded')==='true'`,'Hover help disappears when reading popup');
   await send('Input.dispatchMouseEvent',{type:'mouseMoved',x:1,y:400});
   await send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{...p,radiusX:2,radiusY:2}]});await send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
   await assert(`document.querySelector('${selector}').getAttribute('aria-expanded')==='true'`,'Tap help missing');
   await send('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,x:1,y:400});await send('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,x:1,y:400});
   await assert(`document.querySelector('${selector}').getAttribute('aria-expanded')==='false'`,'Outside dismissal failed');
  }
  await assert(`JSON.stringify(configBody().rss)===${JSON.stringify(before)}`,'Help changed RSS settings');
 }
 console.log(JSON.stringify({errors,scenario,zones}));
}finally{socket.close()}
})().catch(error=>{console.error(error.stack);process.exitCode=1});
