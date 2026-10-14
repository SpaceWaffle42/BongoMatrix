(async()=>{
// Owner 2026-10-13 reviewer findings (ux-review/r1-opus, r2-sonnet, r3-haiku): date entry, birthday
// date-first, upcoming jumps, per-holiday hide, preset icons in the list, contrast, playlist notice,
// delete undo, touch sizes.
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
const typeText=async text=>{for(const ch of text)await send('Input.dispatchKeyEvent',{type:'char',text:ch})};
const inputOnly=async(selector,value)=>evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});el.value=${JSON.stringify(value)};el.dispatchEvent(new Event('input',{bubbles:true}));return true})()`);
const change=async(selector,value)=>evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)throw Error('Missing '+${JSON.stringify(selector)});if(el.type==='checkbox')el.checked=${JSON.stringify(value)};else el.value=${JSON.stringify(value)};el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
const ed='[data-module-editor="event_countdown"]',last='countdownSettings.events.at(-1)';
const card=async()=>`[data-countdown-id="${await evaluate(last+'.id')}"]`;
try{
 await send('Runtime.enable');await send('Page.enable');
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/pair`});await waitFor(`document.querySelector('form')`);
 await evaluate(`fetch('/pair',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({device_name:'Calendar review',pin:'123456'})}).then(()=>true)`);
 await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 await send('Page.navigate',{url:`http://127.0.0.1:${appPort}/`});await waitFor(`typeof matrixEditorsReady!=='undefined'&&matrixEditorsReady`);
 await click('[data-module="event_countdown"] .module-actions button:last-child');
 await waitFor(`document.querySelectorAll('[data-calendar-grid] [data-calendar-day]').length>=28`);
 // A. Calendar not in the playlist: the editor says so and offers a switch.
 await evaluate(`(()=>{const box=document.querySelector('.module-row[data-module="event_countdown"] input.enabled');if(box.checked){box.click()}return true})()`);
 await waitFor(`!!document.querySelector('${ed} [data-calendar-playlist-off]')?.checkVisibility()`,'no "not in playlist" notice');
 await click(`${ed} [data-calendar-playlist-on]`);
 await waitFor(`document.querySelector('.module-row[data-module="event_countdown"] input.enabled').checked&&!document.querySelector('${ed} [data-calendar-playlist-off]')?.checkVisibility()`,'playlist switch did not enable Calendar');
 // B. Birthday asks for the date first: nothing is added until a date is chosen.
 const before=await evaluate('countdownSettings.events.length');
 await click('[data-calendar-add-birthday]');
 await waitFor(`!!document.querySelector('[data-calendar-birthday-date]')?.checkVisibility()`,'birthday date prompt missing');
 if(await evaluate('countdownSettings.events.length')!==before)throw Error('birthday added before a date was chosen');
 await assert(`document.querySelector('[data-calendar-birthday-confirm]').disabled`,'birthday Add enabled with no date');
 await change('[data-calendar-birthday-date]','1988-01-09');await change('[data-calendar-birthday-name]','Priya');
 await click('[data-calendar-birthday-confirm]');
 await assert(`(()=>{const e=${last};return e.title==="Priya's birthday"&&e.start==='1988-01-09'&&e.annual&&e.all_day&&e.icon==='birthday'})()`,'birthday from prompt wrong');
 // Same title+date again warns instead of silently duplicating.
 await click('[data-calendar-add-birthday]');await change('[data-calendar-birthday-date]','1988-01-09');await change('[data-calendar-birthday-name]','Priya');
 await click('[data-calendar-birthday-confirm]');
 await waitFor(`/already/i.test(document.querySelector('[data-calendar-birthday-error]')?.textContent||'')`,'duplicate birthday not flagged');
 if(await evaluate(`countdownSettings.events.filter(e=>e.title==="Priya's birthday").length`)!==1)throw Error('duplicate birthday added');
 // C. Event date: native picker writes ISO; UK dd/mm/yyyy is understood; garbage is flagged inline.
 await evaluate(`calendarShowMonth(2031,2),true`);await click('[data-calendar-day="2031-03-14"]');
 await waitFor(`!!document.querySelector('[data-calendar-day-panel]')`);
 // Phone: the tapped day's panel is brought into view.
 await waitFor(`(()=>{const r=document.querySelector('[data-calendar-day-panel]').getBoundingClientRect();return r.top>=0&&r.top<innerHeight-120})()`,'day panel not scrolled into view on phone');
 await click('[data-calendar-day-panel] [data-calendar-preset="haircut"]');
 const c=await card();
 // Preset icon is a real option in the Icon list.
 await assert(`document.querySelector('${c} [data-countdown-field="icon"]').value==='scissors'`,'preset icon missing from Icon list');
 for(const icon of ['scissors','clock','book','plane','ball','tooth'])await assert(`!!document.querySelector('${c} [data-countdown-field="icon"] option[value="${icon}"]')`,'icon option missing '+icon);
 await assert(`document.querySelector('${c} [data-countdown-picker]')?.type==='date'&&document.querySelector('${c} [data-countdown-picker]').value==='2031-03-14'`,'no native date picker');
 await change(`${c} [data-countdown-picker]`,'2031-03-18');
 await assert(`${last}.start==='2031-03-18'&&document.querySelector('${c} [data-countdown-field="start"]').value==='2031-03-18'`,'picker did not set date');
 await change(`${c} [data-countdown-field="start"]`,'26/10/2031');
 await assert(`${last}.start==='2031-10-26'`,'UK date not converted');
 await change(`${c} [data-countdown-field="start"]`,'next tuesday');
 await assert(`document.querySelector('${c} [data-countdown-field="start"]').getAttribute('aria-invalid')==='true'&&/date/i.test(document.querySelector('${c} [data-countdown-date-error]')?.textContent||'')`,'invalid date not flagged inline');
 await assert(`/Haircut/.test(document.querySelector('${c} > summary').textContent)&&!/Invalid Date/.test(document.querySelector('${c} > summary').textContent)`,'summary shows Invalid Date');
 // Save names the event with the bad date and does not send.
 await click('#save-config');
 await waitFor(`/Haircut/.test(document.querySelector('#message')?.textContent||'')&&matrixHasUnsavedDraft()`,'save error does not name the event');
 await change(`${c} [data-countdown-field="start"]`,'2031-03-18');
 await assert(`document.querySelector('${c} [data-countdown-field="start"]').getAttribute('aria-invalid')!=='true'`,'fixed date still invalid');
 // Owner 2026-10-14: titles centred by default; alignment is an Advanced option.
 await assert(`(()=>{const s=document.querySelector('${ed} [data-countdown-setting="title_align"]');return s&&s.value==='center'&&s.closest('[data-advanced]')&&[...s.options].map(o=>o.textContent).join()==='Centre,Left,Right'})()`,'title alignment not an Advanced option defaulting to Centre');
 await change(`${ed} [data-countdown-setting="title_align"]`,'right');
 await assert(`countdownSettings.title_align==='right'&&matrixHasUnsavedDraft()`,'title alignment change not recorded');
 await change(`${ed} [data-countdown-setting="title_align"]`,'center');
 // Owner 2026-10-14: the end date is entered and shown exactly like the start date.
 await assert(`(()=>{const s=document.querySelector('${c} [data-countdown-field="start"]'),e=document.querySelector('${c} [data-countdown-field="end"]'),p=document.querySelector('${c} [data-countdown-picker="end"]');return e.placeholder===s.placeholder&&e.type===s.type&&p?.type==='date'&&p.value===''})()`,'end date not formatted like start');
 await change(`${c} [data-countdown-field="end"]`,'20/03/2031');
 await assert(`${last}.end==='2031-03-20'&&document.querySelector('${c} [data-countdown-field="end"]').value==='2031-03-20'&&document.querySelector('${c} [data-countdown-picker="end"]').value==='2031-03-20'`,'UK end date not converted');
 await change(`${c} [data-countdown-picker="end"]`,'2031-03-21');
 await assert(`${last}.end==='2031-03-21'&&document.querySelector('${c} [data-countdown-field="end"]').value==='2031-03-21'`,'end picker did not set date');
 await change(`${c} [data-countdown-field="end"]`,'whenever');
 await assert(`document.querySelector('${c} [data-countdown-field="end"]').getAttribute('aria-invalid')==='true'&&/date/i.test(document.querySelector('${c} [data-countdown-date-error="end"]')?.textContent||'')`,'invalid end date not flagged inline');
 await change(`${c} [data-countdown-field="end"]`,'');
 await assert(`${last}.end===null&&document.querySelector('${c} [data-countdown-field="end"]').getAttribute('aria-invalid')!=='true'&&!document.querySelector('${c} [data-countdown-date-error="end"]').textContent`,'blank end date not optional');
 // Round 2 (r1 N1/N5): a typed time switches an all-day event to timed; D/M/YY means 20YY.
 await change(`${c} [data-countdown-field="start"]`,'18/03/31 16:15');
 await assert(`${last}.start==='2031-03-18T16:15'&&${last}.all_day===false`,'typed time on all-day event was dropped '+JSON.stringify(await evaluate(`[${last},countdownParseDate('18/03/31 16:15',true),countdownParseDate('18/03/31 16:15',false),document.querySelector('${c} [data-countdown-field="start"]').value]`)));
 await click(`${c} [data-countdown-field="all_day"]`);
 await assert(`${last}.all_day===true&&${last}.start==='2031-03-18'`,'all-day toggle back failed');
 // Round 2 (r3 N1): the same preset on the same day opens the existing event instead of duplicating.
 const before2=await evaluate('countdownSettings.events.length');
 await evaluate(`calendarShowMonth(2031,2),true`);await click('[data-calendar-day="2031-03-18"]');
 await click(`[data-calendar-day-panel] [data-calendar-preset="haircut"]`);
 await assert(`countdownSettings.events.length===${before2}&&/already/i.test(document.querySelector('#message')?.textContent||'')`,'duplicate preset was added silently');
 // Round 2 (r2 N2): the day-tap Birthday preset asks for the birth year (no "turns 1").
 const n0=await evaluate('countdownSettings.events.length');
 await evaluate(`calendarShowMonth(2031,10),true`);await click('[data-calendar-day="2031-11-18"]');
 await click(`[data-calendar-day-panel] [data-calendar-preset="birthday"]`);
 await waitFor(`!!document.querySelector('[data-calendar-birthday-date]')?.checkVisibility()&&/-11-18$/.test(document.querySelector('[data-calendar-birthday-date]').value)`,'day-tap Birthday does not open the date-of-birth prompt');
 if(await evaluate('countdownSettings.events.length')!==n0)throw Error('day-tap Birthday added before the birth year');
 await change('[data-calendar-birthday-name]','Mum');await change('[data-calendar-birthday-date]','1958-11-18');await click('[data-calendar-birthday-confirm]');
 await assert(`${last}.title==="Mum's birthday"&&${last}.start==='1958-11-18'`,'Mum birthday from day tap wrong');
 // Today marker contrast (r2 #20).
 const today=await evaluate(`(()=>{const n=document.querySelector('.calendar-day.today .calendar-number')||(calendarShowMonth(...calendarTodayIso().split('-').slice(0,2).map((v,i)=>i?v-1:+v)),document.querySelector('.calendar-day.today .calendar-number'));const c=getComputedStyle(n).color.match(/[\\d.]+/g).slice(0,3).map(Number);const l=c.map(v=>{v/=255;return v<=.03928?v/12.92:((v+.055)/1.055)**2.4});return .2126*l[0]+.7152*l[1]+.0722*l[2]})()`);
 if((today+.05)/(0.005+.05)<4.5)throw Error('today number contrast '+today);
 // D. Add event: all-day today, name focused.
 await click('[data-countdown-add]');
 await waitFor(`document.activeElement?.dataset.countdownField==='title'`,'Add event does not focus the name');
 await assert(`${last}.all_day===true&&/^\\d{4}-\\d{2}-\\d{2}$/.test(${last}.start)`,'Add event is not all-day');
 // E. Delete has undo.
 const doomed=await evaluate(last+'.id');
 await click(`[data-countdown-id="${doomed}"] [data-countdown-delete]`);
 await waitFor(`!countdownSettings.events.some(e=>e.id==='${doomed}')&&!!document.querySelector('[data-countdown-undo]')?.checkVisibility()`,'no undo after delete');
 await click('[data-countdown-undo]');
 await waitFor(`countdownSettings.events.some(e=>e.id==='${doomed}')`,'undo did not restore');
 await waitFor(`document.activeElement?.closest('[data-countdown-id]')?.dataset.countdownId==='${doomed}'`,'undo loses focus (r1 N6)');
 // Day-panel Edit opens the card with its name focused (r2#8).
 await evaluate(`calendarShowMonth(2031,2),true`);await click('[data-calendar-day="2031-03-18"]');
 await click(`[data-calendar-day-panel] [data-calendar-edit]`);
 await waitFor(`document.activeElement?.dataset.countdownField==='title'`,'Edit does not focus the event name');
 // F. Upcoming always jumps to the date in the grid and selects it.
 await change('[data-calendar-pack="general"]',true);
 const target=await evaluate(`[...document.querySelectorAll('[data-calendar-agenda] [data-agenda-item]')].find(i=>i.dataset.eventId)?.dataset.date`);
 await click(`[data-calendar-agenda] [data-agenda-item][data-date="${target}"]`);
 await waitFor(`document.querySelector('[data-calendar-day="${target}"]')?.getAttribute('aria-selected')==='true'&&document.querySelector('[data-calendar-day-panel]')?.dataset.calendarDayPanel==='${target}'`,'agenda own-event jump does not select the day');
 // Upcoming hover/focus keeps readable contrast.
 const contrast=await evaluate(`(()=>{const b=document.querySelector('[data-calendar-agenda] [data-agenda-item]');b.focus();const parse=s=>s.match(/[\\d.]+/g).slice(0,3).map(Number);const lum=c=>{const [r,g,b]=c.map(v=>{v/=255;return v<=.03928?v/12.92:((v+.055)/1.055)**2.4});return .2126*r+.7152*g+.0722*b};
   let bg=getComputedStyle(b).backgroundColor,el=b;while(/rgba\\(0, 0, 0, 0\\)|transparent/.test(bg)&&el.parentElement){el=el.parentElement;bg=getComputedStyle(el).backgroundColor}
   const fg=getComputedStyle(b.querySelector('span')).color,[l1,l2]=[lum(parse(fg)),lum(parse(bg))].sort((a,b)=>b-a);return (l1+.05)/(l2+.05)})()`);
 if(contrast<4.5)throw Error('agenda focus contrast '+contrast);
 // G. Hide a holiday: one switch per holiday, "every year", stays in sync.
 await evaluate(`document.querySelector('.calendar-packs').open=true,true`);
 await click('[data-calendar-pack-edit="general"]');
 await waitFor(`!!document.querySelector('[data-holiday-show="general.halloween"]')`);
 await assert(`document.querySelectorAll('[data-holiday-show="general.halloween"]').length===1&&/every year/i.test(document.querySelector('[data-holiday-show="general.halloween"]').closest('label').textContent)`,'hide switch not one-per-holiday');
 await assert(`/General/.test(document.querySelector('[data-calendar-pack-edit="general"]').getAttribute('aria-label')||'')`,'Dates button does not name its pack');
 // H. Override date: typing does not commit partial values nor re-render; out of range rejected.
 await change('[data-calendar-dates-year="general"]','2027');
 const sel='[data-holiday-date="general.halloween"][data-year="2027"]';
 await inputOnly(sel,'0002-10-31');
 await assert(`!countdownSettings.holiday_dates['general.halloween']`,'partial year committed on input');
 await change(sel,'1109-10-31');
 await assert(`!countdownSettings.holiday_dates['general.halloween']&&document.querySelector('${sel}').getAttribute('aria-invalid')==='true'`,'out-of-range override accepted');
 // Round 2 (r1 N3): leaving an invalid field shows the value that will be saved again.
 await evaluate(`document.querySelector('${sel}').dispatchEvent(new Event('blur')),true`);
 await assert(`document.querySelector('${sel}').value==='2027-10-31'&&document.querySelector('${sel}').getAttribute('aria-invalid')!=='true'`,'invalid override still shown after blur');
 await evaluate(`document.querySelector('${sel}').focus(),true`);
 await change(sel,'2027-11-01');
 await assert(`countdownSettings.holiday_dates['general.halloween']['2027']==='2027-11-01'&&document.activeElement===document.querySelector('${sel}')`,'valid override lost focus or not saved');
 await click('[data-holiday-reset="general.halloween"][data-year="2027"]');
 await assert(`!countdownSettings.holiday_dates['general.halloween']&&document.activeElement===document.querySelector('${sel}')`,'Use calculated drops focus (r2 N4)');
 // I. Phone touch targets >= 44px for calendar controls; +N badge does not cover the day number.
 const small=await evaluate(`[...document.querySelectorAll('${ed} .calendar-presets .btn, ${ed} [data-calendar-pack-edit], ${ed} [data-calendar-add-birthday], ${ed} [data-countdown-add], ${ed} [data-countdown-delete], ${ed} .calendar-head .btn')].filter(b=>b.checkVisibility()).map(b=>[b.textContent.trim(),Math.round(b.getBoundingClientRect().height)]).filter(([,h])=>h<44)`);
 if(small.length)throw Error('small touch targets '+JSON.stringify(small));
 await evaluate(`(()=>{calendarAddEvent({title:'Dentist',start:'2031-03-18',icon:'tooth'},false);calendarShowMonth(2031,2);return true})()`);
 const overlap=await evaluate(`(()=>{const cell=document.querySelector('[data-calendar-day="2031-03-18"]'),a=cell.querySelector('.calendar-number').getBoundingClientRect(),b=cell.querySelector('[data-calendar-more]').getBoundingClientRect();const r=document.createRange();r.selectNodeContents(cell.querySelector('.calendar-number'));const t=r.getBoundingClientRect();return !(b.left>=t.right||b.right<=t.left||b.top>=t.bottom||b.bottom<=t.top)})()`);
 if(overlap)throw Error('+N badge overlaps the day number');
 // Round 2 (r1 #5): on phone a day tap brings the presets above the sticky Save bar.
 await evaluate(`scrollTo(0,0),calendarShowMonth(2031,2),true`);
 await click('[data-calendar-day="2031-03-04"]');
 await waitFor(`(()=>{const p=document.querySelector('[data-calendar-day-panel] .calendar-presets')?.getBoundingClientRect(),bar=document.querySelector('.sticky-save')?.getBoundingClientRect();return p&&bar&&p.top>=0&&p.bottom<=bar.top})()`,'phone presets hidden behind Save bar after day tap');
 if(await evaluate('document.documentElement.scrollWidth-innerWidth')>0)throw Error('phone horizontal overflow');
 if(errors.length)throw Error('page errors '+JSON.stringify(errors));
 console.log(JSON.stringify({ok:true,contrast}));
}finally{socket.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
