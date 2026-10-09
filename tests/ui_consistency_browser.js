(async()=>{const {default:assert}=await import('node:assert/strict');
const debugPort=process.argv[1],appPort=process.argv[2],sleep=ms=>new Promise(r=>setTimeout(r,ms));
const endpoint=await fetch(`http://127.0.0.1:${debugPort}/json/new`,{method:'PUT'}).then(r=>r.json());
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map(),errors=[];let id=0;
socket.onmessage=e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result)}else if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails.exception?.description||m.params.exceptionDetails.text)};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>new Promise((resolve,reject)=>{const request=++id;pending.set(request,{resolve,reject});socket.send(JSON.stringify({id:request,method,params}))});
const evaluate=async expression=>{const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value};
const until=async expression=>{const end=Date.now()+10000;while(Date.now()<end){try{if(await evaluate(expression))return}catch(_){/* navigation in progress */}await sleep(25)}throw Error('timeout '+expression)};
// Measures the shared visual contract in docs/UI_CONSISTENCY.md across every dashboard module.
const audit=`(()=>{const shown=e=>e.offsetParent!==null;
 const tiles=[...document.querySelectorAll('.media-card,.palette-group,.module-item,.palette-choice,.paired-device-row,.screen-media-choice,.custom-module-card,.custom-lava-card')].filter(shown);
 const kinds=new Set(tiles.map(e=>e.className.split(' ')[0]));
 const cards=[...document.querySelectorAll('.card')].filter(shown);
 const actionButtons=[...document.querySelectorAll('.media-actions .btn,.screen-media-card-actions .btn')].filter(shown);
 const notes=[...document.querySelectorAll('.card-note,.editor-note,.media-group-note,.screen-media-summary')].filter(shown);
 const checks=[...document.querySelectorAll('label:has(>input[id^=screen-enabled-])')].map(l=>{const a=l.querySelector('input').getBoundingClientRect(),b=l.getBoundingClientRect();return Math.abs((a.top+a.height/2)-(b.top+b.height/2))});
 const library={};for(const c of [...document.querySelectorAll('#media .media-card')].filter(shown)){const a=c.querySelector('.media-actions'),k=a.children.length;(library[k]||=new Set()).add(Math.round(a.getBoundingClientRect().height))}
 const disabled=[...document.querySelectorAll('.btn:disabled')].filter(shown).map(e=>Number(getComputedStyle(e).opacity));
 return {tileKinds:[...kinds],tileRadii:[...new Set(tiles.map(e=>getComputedStyle(e).borderRadius))],tileBackgrounds:[...new Set(tiles.map(e=>getComputedStyle(e).backgroundColor))],
  cardRadii:[...new Set(cards.map(e=>getComputedStyle(e).borderRadius))],buttonHeights:[...new Set(actionButtons.map(e=>Math.round(e.getBoundingClientRect().height)))],buttonMinHeights:[...new Set(actionButtons.map(e=>getComputedStyle(e).minHeight))],
  noteSizes:[...new Set(notes.map(e=>getComputedStyle(e).fontSize))],checkboxOffsets:checks,libraryActionHeights:Object.fromEntries(Object.entries(library).map(([k,v])=>[k,[...v]])),disabledOpacity:[...new Set(disabled)],
  overflow:document.documentElement.scrollWidth>innerWidth}})()`;
const report={errors,viewports:{}};
try{
 await send('Runtime.enable');await send('Page.enable');
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/pair`});await until("document.querySelector('form')&&document.readyState==='complete'");
 await evaluate("fetch('/pair',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({device_name:'UI consistency browser',pin:'123456'})}).then(r=>r.ok)");
 for(const [width,height,mobile] of [[1440,900,false],[390,844,true]]){
  await send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile});
  await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/`});await until(`innerWidth===${width}&&document.readyState==='complete'`);
  await until('typeof matrixEditorsReady!=="undefined"&&matrixEditorsReady&&screenMediaCatalog&&document.querySelector("#media .media-card")&&document.querySelector(".screen-media-choice")');
  await evaluate("document.querySelectorAll('details').forEach(d=>d.open=true)");await sleep(100);
  const r=await evaluate(audit);report.viewports[width]=r;
  assert.ok(r.tileKinds.length>=4,'audit must cover several module tile kinds: '+r.tileKinds);
  assert.deepEqual(r.tileRadii,['8px'],'tile radius differs between modules');
  assert.equal(r.tileBackgrounds.length,1,'tile surface differs between modules: '+r.tileBackgrounds);
  assert.deepEqual(r.cardRadii,['10px'],'card radius differs');
  const control=width>600?36:44;assert.deepEqual(r.buttonMinHeights,[control+'px'],'action buttons must share the control height token: '+r.buttonMinHeights);assert.ok(r.buttonHeights.every(h=>h>=control),'action button below control height: '+r.buttonHeights);
  assert.deepEqual(r.noteSizes,['12px'],'note sizes differ: '+r.noteSizes);
  assert.ok(r.checkboxOffsets.length===2&&r.checkboxOffsets.every(v=>v<=2),'checkbox labels must be inline: '+r.checkboxOffsets);
  assert.ok(Object.keys(r.libraryActionHeights).length>=2&&Object.values(r.libraryActionHeights).every(v=>v.length===1),'library cards with the same actions differ in height: '+JSON.stringify(r.libraryActionHeights));
  assert.ok(r.disabledOpacity.length>0&&r.disabledOpacity.every(v=>v<.6),'disabled buttons must look disabled');
  assert.equal(r.overflow,false,'horizontal overflow at '+width);
 }
 console.log(JSON.stringify(report));
}finally{socket.close()}
})().catch(error=>{console.error(error?.stack||error);process.exitCode=1});
