(async()=>{const {default:assert}=await import('node:assert/strict');
const debugPort=process.argv[1],appPort=process.argv[2],sleep=ms=>new Promise(r=>setTimeout(r,ms));
const endpoint=await fetch(`http://127.0.0.1:${debugPort}/json/new`,{method:'PUT'}).then(r=>r.json());
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map(),errors=[];let id=0;
socket.onmessage=e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result)}else if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails.exception?.description||m.params.exceptionDetails.text)};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>new Promise((resolve,reject)=>{const request=++id;pending.set(request,{resolve,reject});socket.send(JSON.stringify({id:request,method,params}))});
const evaluate=async expression=>{const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value};
const until=async expression=>{const end=Date.now()+10000;while(Date.now()<end){if(await evaluate(expression))return;await sleep(25)}throw Error('timeout '+expression)};
const click=async selector=>{await evaluate(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({block:'center',behavior:'instant'})`);await until(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e||e.disabled)return false;const r=e.getBoundingClientRect(),h=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return r.width>0&&r.y>=0&&r.bottom<=innerHeight&&(h===e||e.contains(h))})()`);const p=await evaluate(`(()=>{const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2}})()`);await send('Input.dispatchMouseEvent',{type:'mousePressed',...p,button:'left',clickCount:1});await send('Input.dispatchMouseEvent',{type:'mouseReleased',...p,button:'left',clickCount:1})};
const layout=rowId=>evaluate(`configBody().playlist.find(r=>r.id===${JSON.stringify(rowId)})`);
const report={errors,roles:{}};
try{
 await send('Runtime.enable');await send('Page.enable');
 // Plain-HTTP LAN dashboards are not secure contexts, so crypto.randomUUID is absent there.
 await send('Page.addScriptToEvaluateOnNewDocument',{source:'try{Object.defineProperty(Crypto.prototype,"randomUUID",{value:undefined,configurable:true})}catch(_){}'});
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/pair`});await until("document.querySelector('form')&&document.readyState==='complete'");
 await evaluate("fetch('/pair',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({device_name:'Screen media editor browser',pin:'123456'})}).then(r=>r.ok)");
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/`});
 await until('typeof matrixEditorsReady!=="undefined"&&matrixEditorsReady&&typeof screenMediaCatalog!=="undefined"&&screenMediaCatalog!==null&&document.querySelector(".screen-media-choice")');
 report.random_uuid_absent=await evaluate("typeof crypto.randomUUID!=='function'");
 for(const role of ['splash','sleep']){
  const card=`#screen-media-${role} .screen-media-choice[data-source="editor_upload.gif"]`;
  assert.equal(await evaluate(`document.querySelectorAll(${JSON.stringify(card+' > .media-playlist-editor')}).length`),0,'layout fields must not render inline on the card');
  assert.equal(await evaluate(`document.querySelector(${JSON.stringify(card+' .screen-media-edit')}).disabled`),true,'unselected upload has no owning row yet');
  await click(card+' .screen-media-selected');
  await until(`document.querySelector(${JSON.stringify(card)}).dataset.rowId`);
  const rowId=await evaluate(`document.querySelector(${JSON.stringify(card)}).dataset.rowId`);
  assert.match(rowId,new RegExp('^'+role+'_[0-9a-f]{32}$'),'insecure-context row id shape');
  await click(card+' .screen-media-edit');
  await until(`document.querySelector(${JSON.stringify(card+' dialog[open]')})`);
  assert.match(await evaluate(`document.querySelector(${JSON.stringify(card+' .screen-media-stage-image')}).getAttribute('src')`),/^\/media\/editor_upload\.gif$/,'dialog preview loads media on open');
  await click(card+' dialog[open] [data-media-layout="x"]');
  await send('Input.dispatchKeyEvent',{type:'keyDown',key:'a',code:'KeyA',windowsVirtualKeyCode:65,modifiers:2});await send('Input.dispatchKeyEvent',{type:'keyUp',key:'a',code:'KeyA',windowsVirtualKeyCode:65,modifiers:2});
  await send('Input.insertText',{text:'16'});
  const edited=await layout(rowId);
  assert.equal(edited.layout.x,16);assert.deepEqual(edited.sources,['editor_upload.gif']);assert.equal(edited.enabled,true);
  assert.match(await evaluate(`document.querySelector(${JSON.stringify(card+' .screen-media-summary')}).textContent`),/at 16, 0/,'card summary reflects edit');
  await evaluate(`[...document.querySelectorAll(${JSON.stringify(card+' dialog[open] button')})].find(b=>b.textContent==='Reset layout').click()`);
  assert.equal((await layout(rowId)).layout.x,0,'reset restores defaults');
  assert.equal(await evaluate(`document.querySelector(${JSON.stringify(card+' dialog[open] [data-media-layout="x"]')}).value`),'0','reset refreshes dialog fields');
  await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
  await until(`!document.querySelector(${JSON.stringify(card+' dialog[open]')})`);
  // The dialog close event is queued after the open attribute is removed; wait for the release.
  await until(`document.querySelector(${JSON.stringify(card+' .screen-media-stage-image')}).getAttribute('src').startsWith('data:')`);
  const editDisabled=`document.querySelector(${JSON.stringify(card+' .screen-media-edit')}).disabled`;
  await click(card+' .screen-media-edit');await until(`document.querySelector(${JSON.stringify(card+' dialog[open]')})`);
  await evaluate(`(()=>{const c=document.querySelector(${JSON.stringify(card+' .screen-media-selected')});c.checked=false;c.dispatchEvent(new Event('change',{bubbles:true}))})()`);
  await until(editDisabled);
  assert.equal(await evaluate(`document.querySelector(${JSON.stringify(card+' .palette-inspect')}).disabled`),false,'Preview stays enabled when the card is unselected');
  assert.equal(await evaluate(`Boolean(document.querySelector(${JSON.stringify(card+' dialog[open]')}))`),false,'unselecting closes the open editor');
  await click(card+' .screen-media-selected');await until('!'+editDisabled);
  report.roles[role]={row_id_ok:true,edited_x:edited.layout.x,edit_follows_selection:true};
 }
 await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await until('innerWidth===390');
 const phone='#screen-media-sleep .screen-media-choice[data-source="editor_upload.gif"]';
 await click(phone+' .screen-media-edit');await until(`document.querySelector(${JSON.stringify(phone+' dialog[open]')})`);
 report.phone_dialog_fits=await evaluate(`(()=>{const r=document.querySelector(${JSON.stringify(phone+' dialog[open]')}).getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight})()`);
 report.phone_overflow=await evaluate('document.documentElement.scrollWidth>innerWidth');
 console.log(JSON.stringify(report));
}finally{socket.close()}
})().catch(error=>{console.error(error?.stack||error);process.exitCode=1});
