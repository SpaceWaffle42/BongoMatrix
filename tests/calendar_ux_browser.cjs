(async()=>{
// Owner 2026-10-13: researched calendar layout (ux-review/calendar-research.md). Single tap (not
// double-click) on a day, quick presets, Upcoming agenda with ages, "Coming up next", square phone
// cells with +N, holidays summary row, APG keyboard grid, collapsible event cards.
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
const ed='[data-module-editor="event_countdown"]';
try{
 await send('Runtime.enable');await send('Page.enable');
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/pair`});await waitFor(`document.querySelector('form')`);
 await evaluate(`fetch('/pair',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({device_name:'Calendar UX',pin:'123456'})}).then(()=>true)`);
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/`});await waitFor(`typeof matrixEditorsReady!=='undefined'&&matrixEditorsReady`);
 await click('[data-module="event_countdown"] .module-actions button:last-child');
 await waitFor(`document.querySelectorAll('[data-calendar-grid] [data-calendar-day]').length>=28`,'grid missing');
 // 1. No double-click instruction anywhere; tapping is the way in.
 await assert(`!document.querySelector('${ed}').textContent.includes('Double-click')`,'double-click hint still shown');
 // 2. APG grid: exactly one day in the Tab order, days are named with date and events.
 await assert(`document.querySelectorAll('[data-calendar-day][tabindex="0"]').length===1`,'roving tabindex missing');
 await assert(`[...document.querySelectorAll('[data-calendar-day]')].every(d=>/\\d{4}/.test(d.getAttribute('aria-label')||''))`,'day cells lack descriptive labels');
 // Jump to a fixed month so the assertions are date-independent.
 await evaluate(`calendarShowMonth(2031,2),true`);
 await waitFor(`!!document.querySelector('[data-calendar-day="2031-03-14"]')`);
 // 3. Single click on a day selects it and opens that day's panel with presets.
 await click('[data-calendar-day="2031-03-14"]');
 await waitFor(`document.querySelector('[data-calendar-day="2031-03-14"]').getAttribute('aria-selected')==='true'`,'day not selected by single click');
 await assert(`!!document.querySelector('[data-calendar-day-panel]')?.checkVisibility()&&document.querySelector('[data-calendar-day-panel]').textContent.includes('14')`,'day panel missing');
 for(const preset of ['birthday','anniversary','appointment','haircut','school','trip','other'])
   await assert(`!!document.querySelector('[data-calendar-day-panel] [data-calendar-preset="${preset}"]')?.checkVisibility()`,'preset missing: '+preset);
 // 4. Haircut preset adds an event on that date with the scissors icon and focuses its name.
 await click('[data-calendar-day-panel] [data-calendar-preset="haircut"]');
 await assert(`(()=>{const e=countdownSettings.events.at(-1);return e.title==='Haircut'&&e.icon==='scissors'&&e.start.startsWith('2031-03-14')&&!e.annual})()`,'haircut preset wrong');
 await waitFor(`document.activeElement?.dataset.countdownField==='title'&&document.activeElement.closest('[data-countdown-id]').dataset.countdownId===countdownSettings.events.at(-1).id`,'new event name not focused');
 await assert(`document.querySelector('[data-calendar-day="2031-03-14"] canvas[data-icon="scissors"]')`,'haircut icon not on its day');
 // 5. Birthday preset for someone else: opens the date-of-birth prompt on that day (reviewer round 2).
 await click('[data-calendar-day="2031-03-20"]');
 await click('[data-calendar-day-panel] [data-calendar-preset="birthday"]');
 await change('[data-calendar-birthday-name]','Mum');await change('[data-calendar-birthday-date]','1968-03-20');
 await click('[data-calendar-birthday-confirm]');
 await assert(`(()=>{const e=countdownSettings.events.at(-1);return e.icon==='birthday'&&e.annual&&e.all_day&&e.title==="Mum's birthday"&&e.start==='1968-03-20'})()`,'birthday preset wrong');
 // 6. Keyboard: arrows move focus by day/week, Enter selects.
 await evaluate(`document.querySelector('[data-calendar-day="2031-03-20"]').focus(),true`);
 await key('ArrowRight','ArrowRight');
 await waitFor(`document.activeElement?.dataset.calendarDay==='2031-03-21'`,'ArrowRight did not move focus');
 await key('ArrowDown','ArrowDown');
 await waitFor(`document.activeElement?.dataset.calendarDay==='2031-03-28'`,'ArrowDown did not move a week');
 await key('Enter','Enter');
 await waitFor(`document.querySelector('[data-calendar-day="2031-03-28"]').getAttribute('aria-selected')==='true'`,'Enter did not select');
 // 7. Upcoming agenda lists own events and holidays in date order; birthdays with a year show the age.
 await change('[data-calendar-pack="general"]',true);
 await waitFor(`document.querySelectorAll('[data-calendar-agenda] [data-agenda-item]').length>=3`,'agenda missing');
 const agenda=await evaluate(`[...document.querySelectorAll('[data-calendar-agenda] [data-agenda-item]')].map(i=>({date:i.dataset.date,text:i.textContent}))`);
 for(let i=1;i<agenda.length;i++)if(agenda[i].date<agenda[i-1].date)throw Error('agenda not sorted '+JSON.stringify(agenda));
 if(!agenda.some(i=>i.text.includes("Mum's birthday")&&/turns \d+/.test(i.text)))throw Error('agenda lacks age for birthday '+JSON.stringify(agenda));
 // Clicking an agenda item jumps to its date and selects it (reviewer round 1: consistent for events and holidays).
 const agendaId=await evaluate('countdownSettings.events.at(-1).id');
 await click(`[data-calendar-agenda] [data-agenda-item][data-event-id="${agendaId}"]`);
 await waitFor(`(()=>{const d=document.querySelector('[data-calendar-agenda] [data-agenda-item][data-event-id="${agendaId}"]')?.dataset.date;return d&&document.querySelector('[data-calendar-day-panel]')?.dataset.calendarDayPanel===d})()`,'agenda item does not select its date');
 // 8. "Coming up next" names the first upcoming item.
 await assert(`document.querySelector('[data-calendar-next]')?.textContent.includes(document.querySelector('[data-calendar-agenda] [data-agenda-item]').dataset.title)`,'coming up next missing');
 // 9. Holidays collapse into a summary row naming switched-on packs.
 await assert(`/General/.test(document.querySelector('.calendar-packs > summary').textContent)`,'holiday summary does not list switched-on packs');
 // 10. Event cards are collapsible with icon, name and date in the summary.
 await assert(`[...document.querySelectorAll('${ed} details.countdown-event > summary')].length===countdownSettings.events.length`,'event cards not collapsible');
 // 11. Phone layout: square-ish cells, compact grid, overflow badge, no horizontal scroll.
 await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await sleep(200);
 await evaluate(`(()=>{for(const t of ['Dentist','Swimming','Parents evening'])calendarAddEvent({title:t,start:'2031-03-14',icon:'calendar'},false);calendarShowMonth(2031,2);return true})()`);
 const phone=await evaluate(`(()=>{const d=document.querySelector('[data-calendar-day="2031-03-10"]').getBoundingClientRect(),g=document.querySelector('[data-calendar-grid]').getBoundingClientRect(),busy=document.querySelector('[data-calendar-day="2031-03-14"]');
   return {w:d.width,h:d.height,grid:g.height,more:busy.querySelector('[data-calendar-more]')?.textContent||'',overflow:document.documentElement.scrollWidth-innerWidth}})()`);
 if(phone.h>phone.w*1.25||phone.grid>380||phone.overflow>0||!/\+\d/.test(phone.more))throw Error('phone layout '+JSON.stringify(phone));
 if(phone.w<40)throw Error('day cells below touch size '+JSON.stringify(phone));
 // Desktop: grid and agenda sit side by side.
 await send('Emulation.setDeviceMetricsOverride',{width:1400,height:900,deviceScaleFactor:1,mobile:false});await sleep(200);
 const desk=await evaluate(`(()=>{const g=document.querySelector('[data-calendar-grid]').getBoundingClientRect(),a=document.querySelector('[data-calendar-agenda]').getBoundingClientRect();return {gr:g.right,al:a.left,gt:g.top,at:a.top,ab:a.bottom}})()`);
 if(!(desk.al>=desk.gr-1&&desk.at<desk.gt+200))throw Error('desktop not side by side '+JSON.stringify(desk));
 // Save keeps the events.
 await click('#save-config');await waitFor(`!matrixHasUnsavedDraft()`,'save failed');
 const saved=await evaluate(`fetch('/api/config').then(r=>r.json()).then(c=>c.event_countdown)`);
 if(!saved.events.some(e=>e.icon==='scissors')||!saved.events.some(e=>e.title==="Mum's birthday"))throw Error('save mismatch');
 if(errors.length)throw Error('page errors '+JSON.stringify(errors));
 console.log(JSON.stringify({agenda:agenda.length,phone,events:saved.events.length}));
}finally{socket.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
