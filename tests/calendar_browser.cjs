(async()=>{
// Owner 2026-10-13: Event Countdown becomes a calendar with holiday packs, per-year date edits,
// default holiday icons (cake, pumpkin, tree...) and birthday confetti.
const debugPort=process.argv[1],appPort=process.argv[2],sleep=ms=>new Promise(r=>setTimeout(r,ms));
const endpoint=await fetch(`http://127.0.0.1:${debugPort}/json/new`,{method:'PUT'}).then(r=>r.json());
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map(),errors=[];let id=0;
socket.onmessage=e=>{const m=JSON.parse(e.data);if(m.id&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result)}else if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails.exception?.description||m.params.exceptionDetails.text)};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>new Promise((resolve,reject)=>{const request=++id;pending.set(request,{resolve,reject});socket.send(JSON.stringify({id:request,method,params}))});
const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw Error(JSON.stringify(result.exceptionDetails));return result.result.value};
const waitFor=async(expression,message)=>{for(let i=0;i<200;i++){try{if(await evaluate(expression))return}catch(_){}await sleep(50)}throw Error((message||'timeout')+': '+expression)};
const assert=async(expression,message)=>{if(!await evaluate(expression))throw Error(message)};
const click=async selector=>{const point=await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)throw Error('Missing control '+${JSON.stringify(selector)});el.scrollIntoView({block:'center',behavior:'instant'});const r=el.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()`);await send('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...point});await send('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...point})};
const change=async(selector,value)=>evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)throw Error('Missing '+${JSON.stringify(selector)});if(el.type==='checkbox')el.checked=${JSON.stringify(value)};else el.value=${JSON.stringify(value)};el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
try{
 await send('Runtime.enable');await send('Page.enable');
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/pair`});await waitFor(`document.querySelector('form')`);
 await evaluate(`fetch('/pair',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({device_name:'Calendar browser',pin:'123456'})}).then(()=>true)`);
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/`});await waitFor(`typeof matrixEditorsReady!=='undefined'&&matrixEditorsReady`);
 await assert(`document.querySelector('.module-row[data-module="event_countdown"] .module-name').textContent.trim()==='Calendar'`,'playlist label is not Calendar');
 await click('[data-module="event_countdown"] .module-actions button:last-child');
 // Month grid.
 await waitFor(`document.querySelectorAll('[data-calendar-grid] [data-calendar-day]').length>=28`,'calendar month grid missing');
 const month=await evaluate(`document.querySelector('[data-calendar-title]').textContent`);
 // Packs are visible in Simple mode, all off by default.
 const packs=await evaluate(`[...document.querySelectorAll('[data-calendar-pack]')].map(b=>({pack:b.dataset.calendarPack,on:b.checked,visible:b.checkVisibility(),label:b.closest('label').textContent.trim()}))`);
 for(const want of ['christian','islam','judaism','hinduism','buddhism','sikhism'])if(!packs.find(p=>p.pack===want&&p.visible&&!p.on))throw Error('pack switch missing/visible/off: '+want+' '+JSON.stringify(packs));
 // Turning a pack on shows its holidays (with icons) in the grid.
 await change('[data-calendar-pack="christian"]',true);
 await evaluate(`calendarShowMonth(2026,11),true`);
 await waitFor(`[...document.querySelectorAll('[data-calendar-day="2026-12-25"] [data-calendar-event]')].some(e=>e.textContent.includes('Christmas Day')&&e.querySelector('canvas[data-icon="tree"]'))`,'Christmas with tree icon missing from 25 Dec');
 await assert(`configBody().event_countdown.packs.christian===true&&matrixHasUnsavedDraft()`,'pack switch not in draft');
 // Edit a holiday date for one year from inside the pack.
 await click('[data-calendar-pack-edit="christian"]');
 // Reviewer r2: the Year select shows exactly one year.
 await waitFor(`!!document.querySelector('[data-calendar-dates-year="christian"]')`,'pack date editor missing');
 await change('[data-calendar-dates-year="christian"]','2027');
 await waitFor(`!!document.querySelector('[data-holiday-date="christian.easter"][data-year="2027"]')&&!document.querySelector('[data-holiday-date][data-year="2028"]')&&!document.querySelector('[data-holiday-date][data-year="2026"]')`,'year select does not filter');
 await assert(`document.querySelector('[data-holiday-date="christian.easter"][data-year="2027"]').value==='2027-03-28'`,'calculated Easter 2027 not shown');
 await change('[data-holiday-date="christian.easter"][data-year="2027"]','2027-04-04');
 await assert(`configBody().event_countdown.holiday_dates['christian.easter']['2027']==='2027-04-04'`,'edited date not in draft');
 await click('[data-holiday-reset="christian.easter"][data-year="2027"]');
 await assert(`!configBody().event_countdown.holiday_dates['christian.easter']&&document.querySelector('[data-holiday-date="christian.easter"][data-year="2027"]').value==='2027-03-28'`,'reset to calculated date failed');
 await change('[data-holiday-show="christian.ash_wednesday"]',false);
 await assert(`configBody().event_countdown.hidden_holidays.includes('christian.ash_wednesday')`,'hiding a holiday failed');
 // Reviewer r2 blocker: an override must survive Save (not only reset-then-save).
 await change('[data-calendar-dates-year="christian"]','2026');
 await change('[data-holiday-date="christian.christmas"][data-year="2026"]','2026-12-26');
 // Birthday quick-add asks for the date first; uses the cake icon, all day, every year.
 await click('[data-calendar-add-birthday]');
 await change('[data-calendar-birthday-date]','1990-05-14');
 await click('[data-calendar-birthday-confirm]');
 await assert(`(()=>{const e=countdownSettings.events.at(-1);return e.icon==='birthday'&&e.annual&&e.all_day&&e.title==='My birthday'&&e.start==='1990-05-14'})()`,'birthday quick add wrong');
 // Save and reload keep everything.
 await click('#save-config');await waitFor(`!matrixHasUnsavedDraft()`,'save failed');
 const saved=await evaluate(`fetch('/api/config').then(r=>r.json()).then(c=>c.event_countdown)`);
 if(!saved.packs.christian||!saved.hidden_holidays.includes('christian.ash_wednesday')||saved.events.at(-1).icon!=='birthday'||saved.holiday_dates['christian.christmas']?.['2026']!=='2026-12-26')throw Error('save mismatch '+JSON.stringify(saved));
 // Mobile: no horizontal overflow; grid fits.
 for(const width of [390,1280]){
  await send('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:width<500});await sleep(150);
  const o=await evaluate(`(()=>{const g=document.querySelector('[data-calendar-grid]').getBoundingClientRect();return {page:document.documentElement.scrollWidth-window.innerWidth,right:g.right-window.innerWidth}})()`);
  if(o.page>0||o.right>1)throw Error('overflow at '+width+' '+JSON.stringify(o));
 }
 if(errors.length)throw Error('page errors '+JSON.stringify(errors));
 console.log(JSON.stringify({month,packs:packs.length}));
}finally{socket.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
