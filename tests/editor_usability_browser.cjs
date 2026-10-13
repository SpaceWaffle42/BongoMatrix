(async()=>{
const debugPort=process.argv[1],appPort=process.argv[2],sleep=ms=>new Promise(r=>setTimeout(r,ms));
const endpoint=await fetch(`http://127.0.0.1:${debugPort}/json/new`,{method:'PUT'}).then(r=>r.json());
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map(),errors=[];let id=0;
socket.onmessage=e=>{const m=JSON.parse(e.data);if(m.id&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result)}else if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails.text)};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>new Promise((resolve,reject)=>{const request=++id;pending.set(request,{resolve,reject});socket.send(JSON.stringify({id:request,method,params}))});
const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw Error(JSON.stringify(result.exceptionDetails));return result.result.value};
const waitFor=async expression=>{for(let i=0;i<200;i++){if(await evaluate(expression))return;await sleep(50)}throw Error('timeout: '+expression)};
const point=async selector=>evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)throw Error('Missing '+${JSON.stringify(selector)});el.scrollIntoView({block:'center',behavior:'instant'});const r=el.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;if(x<0||y<0||x>innerWidth||y>innerHeight||!el.contains(document.elementFromPoint(x,y)))throw Error('Occluded '+${JSON.stringify(selector)}+' by '+(document.elementFromPoint(x,y)?.outerHTML||'nothing').slice(0,160)+' at '+[x,y,innerWidth,innerHeight]);return {x,y}})()`);
const click=async selector=>{const p=await point(selector);await send('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...p});await send('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...p})};
const change=async(selector,value)=>evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});el.value=${JSON.stringify(value)};el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
const assert=async(expression,message)=>{if(!await evaluate(expression))throw Error(message)};
const key=async(key,code,n)=>{await send('Input.dispatchKeyEvent',{type:'keyDown',key,code,windowsVirtualKeyCode:n,...(key==='Enter'?{text:'\r'}:{})});await send('Input.dispatchKeyEvent',{type:'keyUp',key,code,windowsVirtualKeyCode:n})};
try{
 await send('Runtime.enable');await send('Page.enable');
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/pair`});await waitFor(`!!document.querySelector('form')`);await send('Runtime.evaluate',{expression:"localStorage.setItem('matrix.editorAdvanced.v1',JSON.stringify(Object.fromEntries(['weather','openweather_overview','openweather_forecast','clock_lava','rss','astronomy','event_countdown'].map(m=>[m,true]))))"});
 await evaluate(`fetch('/pair',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({device_name:'Editor UX browser',pin:'123456'})}).then(()=>true)`);
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/`});await waitFor(`typeof matrixEditorsReady!=='undefined'&&matrixEditorsReady`);
 const scenario='__SCENARIO__';let zones=null;
 if(scenario==='simple'){
  // Owner: editors open Simple by default; an Advanced toggle reveals the rest. Plain-language labels.
  await send('Emulation.setDeviceMetricsOverride',{width:1280,height:900,deviceScaleFactor:1,mobile:false});
  await evaluate(`localStorage.removeItem('matrix.editorAdvanced.v1'),true`);await send('Page.reload');await sleep(300);await waitFor(`typeof matrixEditorsReady!=='undefined'&&matrixEditorsReady`);
  const draft=`JSON.stringify(configBody())`,before=await evaluate(draft);
  const cases=[['weather','[data-setting="now_x"]'],['openweather_overview','[data-setting="icon_x"]'],['openweather_forecast','[data-setting="time_y"]'],['clock_lava','[data-setting="field_threshold"]'],['rss','[data-rss-setting="include"]'],['astronomy','[data-astronomy-setting="horizon_days"]'],['event_countdown','[data-countdown-setting="top"]']];
  for(const [module,advanced] of cases){
   const editor=`[data-module-editor="${module}"]`;
   await click(`[data-module="${module}"] .module-actions button:last-child`);
   await waitFor(`!document.querySelector('${editor}').hidden`);
   const toggle=`${editor} [data-advanced-toggle]`;
   await assert(`(()=>{const t=document.querySelector('${toggle}');return !!t&&t.type==='checkbox'&&!t.checked&&t.closest('label').textContent.includes('Show advanced options')})()`,module+' lacks an unchecked Show advanced options toggle');
   const count=`[...document.querySelectorAll('${editor} input:not([data-advanced-toggle]),${editor} select,${editor} textarea')].filter(e=>e.checkVisibility()).length`;
   const simple=await evaluate(count);
   await assert(`!(document.querySelector('${editor} ${advanced}')?.checkVisibility())`,module+' shows advanced control '+advanced+' in Simple');
   await assert(`!/IANA|baseline|safe inset|#RRGGBB|exclusive|Frame delay|frame delay/.test([...document.querySelectorAll('${editor} label,${editor} summary,${editor} h3,${editor} legend')].filter(e=>e.checkVisibility()).map(e=>e.textContent).join(' '))`,module+' Simple view still shows jargon');
   await click(toggle);
   await waitFor(`!!document.querySelector('${editor} ${advanced}')?.checkVisibility()`);
   const full=await evaluate(count);
   if(!(full>simple))throw Error(module+' Advanced did not reveal more controls: '+simple+' -> '+full);
   await assert(`!/IANA|baseline|safe inset|#RRGGBB|exclusive/.test([...document.querySelectorAll('${editor} label,${editor} summary,${editor} h3,${editor} legend')].filter(e=>e.checkVisibility()).map(e=>e.textContent).join(' '))`,module+' Advanced view still shows jargon');
   await click(toggle);
   await assert(`!(document.querySelector('${editor} ${advanced}')?.checkVisibility())`,module+' toggle did not return to Simple');
  }
  // Preference persists per module across reloads; toggling never changes the draft.
  await click(`[data-module="rss"] .module-actions button:last-child`);await waitFor(`!document.querySelector('[data-module-editor="rss"]').hidden`);
  await click(`[data-module-editor="rss"] [data-advanced-toggle]`);
  if(await evaluate(draft)!==before)throw Error('Advanced toggle changed the draft');
  await send('Page.reload');await sleep(300);await waitFor(`typeof matrixEditorsReady!=='undefined'&&matrixEditorsReady`);
  await click(`[data-module="rss"] .module-actions button:last-child`);await waitFor(`!document.querySelector('[data-module-editor="rss"]').hidden`);
  await assert(`document.querySelector('[data-module-editor="rss"] [data-advanced-toggle]').checked&&document.querySelector('[data-module-editor="rss"] [data-rss-setting="include"]').checkVisibility()`,'RSS Advanced preference not remembered');
  await click(`[data-module="astronomy"] .module-actions button:last-child`);await waitFor(`!document.querySelector('[data-module-editor="astronomy"]').hidden`);
  await assert(`!document.querySelector('[data-module-editor="astronomy"] [data-advanced-toggle]').checked`,'Advanced preference leaked to another module');
  await evaluate(`localStorage.removeItem('matrix.editorAdvanced.v1'),true`);
 }
 if(scenario==='accordion'){
  // Owner: opening one module editor closes every other open editor.
  await send('Emulation.setDeviceMetricsOverride',{width:1280,height:900,deviceScaleFactor:1,mobile:false});
  const openEditors=`[...document.querySelectorAll('#playlist .module-editor')].filter(e=>!e.hidden)`;
  const rows=await evaluate(`[...document.querySelectorAll('#playlist > .module-item')].map((item,i)=>({i,hasEditor:!!item.querySelector('.module-editor')&&item.getBoundingClientRect().height>0})).filter(r=>r.hasEditor).map(r=>r.i)`);
  if(rows.length<4)throw Error('too few editors to test: '+rows.length+' '+await evaluate(`[...document.querySelectorAll('#playlist > *')].map(e=>e.className+':'+(e.querySelector('.module-row')?.dataset.module)+':'+e.getBoundingClientRect().height).join('|')`));
  await evaluate(`document.querySelector('#playlist').scrollIntoView()`);
  for(const index of rows){
   const button=`#playlist > .module-item:nth-child(${index+1}) .module-actions button:last-child`;
   if(!await evaluate(`document.querySelector('${button}').getBoundingClientRect().width>0`))throw Error('zero-size '+index+' '+await evaluate(`[...document.querySelectorAll('#playlist > *')].map(e=>(e.querySelector('.module-row')?.dataset.module)+':'+e.getBoundingClientRect().height+':'+getComputedStyle(e).display).join('|')+' sec:'+getComputedStyle(document.querySelector('#playlist').closest('section')||document.body).display`));
   await click(button);
   try{await waitFor(`!document.querySelector('#playlist > .module-item:nth-child(${index+1}) .module-editor').hidden`)}catch(e){throw Error('row '+index+' did not open; label='+await evaluate(`document.querySelector('${button}').textContent`)+' open='+await evaluate(`${openEditors}.map(e=>e.dataset.moduleEditor).join(',')`))}
   await assert(`${openEditors}.length===1`,'more than one editor open after opening row '+index);
   await assert(`[...document.querySelectorAll('#playlist .module-actions button:last-child')].filter(b=>b.textContent==='Close').length===1&&document.querySelector('${button}').textContent==='Close'`,'Edit/Close labels out of sync at row '+index);
   await assert(`document.querySelector('${button}').getAttribute('aria-expanded')==='true'`,'open editor button lacks aria-expanded=true at row '+index);
  }
  const last=`#playlist > .module-item:nth-child(${rows.at(-1)+1}) .module-actions button:last-child`;
  await click(last);
  await assert(`${openEditors}.length===0&&document.querySelector('${last}').getAttribute('aria-expanded')==='false'`,'Close did not close the editor');
 }
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
