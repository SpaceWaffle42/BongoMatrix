let countdownSettings = {{ config.event_countdown|tojson }};
let countdownPreviewInfo = null;
// Holiday packs and their offline-calculated dates (server), plus the panel's icon pixels.
const holidayCatalogue = {{ holiday_catalogue|tojson }};
const countdownIconArt = {{ countdown_icon_art|tojson }};
const countdownIconNames = {calendar:'Calendar',birthday:'Birthday cake',anniversary:'Heart (anniversary)',launch:'Rocket',holiday:'Holiday',opening:'Door (opening)',
  tree:'Christmas tree',pumpkin:'Pumpkin',egg:'Easter egg',cross:'Cross',dove:'Dove',star:'Star',heart:'Heart',firework:'Fireworks',crescent:'Crescent moon',
  lantern:'Lantern',star_of_david:'Star of David',menorah:'Menorah',shofar:'Shofar',diya:'Diya lamp',holi:'Holi colours',lotus:'Lotus',khanda:'Khanda',leaf:'Leaf',
  scissors:'Scissors (haircut)',clock:'Clock (appointment)',book:'Book (school)',plane:'Plane (trip)',ball:'Ball (sport)',tooth:'Tooth (dentist)'};
let calendarMonth = null; // {year, month 0-11} shown in the grid
let calendarPacksOpen = null; // remembered open state of the Holiday packs section
let calendarUndo = null; // {event,index} kept briefly after Delete event
let calendarAgendaLimit = 8; // Upcoming shows 8, "Show more" reveals up to a year ahead
// Accept ISO (2026-12-25, 2026-12-25T18:00) and UK day-first dates (25/12/2026, 25.12.2026 18:00).
function countdownParseDate(text,allDay) {
  const value=String(text||'').trim();
  const real=(y,m,d)=>{const t=new Date(Date.UTC(y,m-1,d));return y>=1&&y<=9997&&t.getUTCFullYear()===y&&t.getUTCMonth()===m-1&&t.getUTCDate()===d};
  let match=value.match(/^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?)?$/);
  if(match){if(!real(+match[1],+match[2],+match[3]))return null;if(allDay)return match[4]?null:value;return match[4]?value:value+'T00:00'}
  match=value.match(/^(\d{1,2})[\/.\-](\d{1,2})[\/.\-](\d{4}|\d{2})(?:[ ,T]+(\d{1,2}):(\d{2}))?$/);
  if(match&&match[3].length===2)match[3]='20'+match[3];
  if(!match||!real(+match[3],+match[2],+match[1]))return null;
  const iso=`${match[3]}-${match[2].padStart(2,'0')}-${match[1].padStart(2,'0')}`;
  if(allDay)return match[4]?null:iso;
  const hour=Number(match[4]??0),minute=Number(match[5]??0);if(hour>23||minute>59)return null;
  return `${iso}T${String(hour).padStart(2,'0')}:${String(minute).padStart(2,'0')}`;
}
function countdownInvalidEvent() {
  return countdownSettings.events.find(event=>!countdownParseDate(event.start,event.all_day)||(event.end&&!countdownParseDate(event.end,event.all_day)));
}
function calendarPlaylistBox() {return document.querySelector('.module-row[data-module="event_countdown"] input.enabled')}
function renderCalendarPlaylistNotice() {
  const on=!!calendarPlaylistBox()?.checked;
  for(const notice of document.querySelectorAll('[data-calendar-playlist-off]'))notice.hidden=on;
}
document.addEventListener('change',event=>{if(event.target===calendarPlaylistBox())renderCalendarPlaylistNotice()});
function calendarOpenBirthdayPrompt(date='') {
  const box=document.querySelector('[data-calendar-birthday-prompt]');if(!box)return;
  box.hidden=false;box.querySelector('[data-calendar-birthday-error]').textContent='';
  const input=box.querySelector('[data-calendar-birthday-date]');input.value=date;
  box.querySelector('[data-calendar-birthday-confirm]').disabled=!date;
  box.querySelector('[data-calendar-birthday-hint]').textContent=date?'Change the year to the year they were born so the calendar can show their age.':'';
  box.scrollIntoView({block:'center',behavior:'instant'});box.querySelector('[data-calendar-birthday-name]').focus();
}
function renderBirthdayPrompt(container) {
  const box=document.createElement('div');box.className='calendar-birthday-prompt';box.hidden=true;box.dataset.calendarBirthdayPrompt='';
  const heading=document.createElement('strong');heading.textContent='Add a birthday';
  const nameLabel=document.createElement('label');nameLabel.textContent='Whose birthday? (leave blank for yours)';
  const name=document.createElement('input');name.type='text';name.dataset.calendarBirthdayName='';name.placeholder='e.g. Mum';nameLabel.append(name);
  const dateLabel=document.createElement('label');dateLabel.textContent='Date of birth (the year lets the calendar show their age)';
  const date=document.createElement('input');date.type='date';date.dataset.calendarBirthdayDate='';dateLabel.append(date);
  const hint=document.createElement('p');hint.className='editor-note';hint.dataset.calendarBirthdayHint='';dateLabel.append(hint);
  const error=document.createElement('p');error.className='editor-note calendar-error';error.dataset.calendarBirthdayError='';error.setAttribute('role','alert');
  const confirm=makeButton('Add birthday',()=>{
    const start=countdownParseDate(date.value,true),who=name.value.trim(),title=who?`${who}'s birthday`.replace(/s's birthday$/,"s' birthday"):'My birthday';
    if(!start){error.textContent='Choose the date of birth first.';return}
    if(countdownSettings.events.some(event=>event.title===title&&countdownLocalDate(event.start).slice(5)===start.slice(5))){error.textContent=`${title} is already in your calendar on that day.`;return}
    box.hidden=true;const id=calendarAddEvent({title,icon:'birthday',annual:true,all_day:true,start},false);
    setMessage(`${title} added. Save to keep it.`);document.querySelector(`[data-countdown-id="${id}"] > summary`)?.focus();
  });confirm.dataset.calendarBirthdayConfirm='';confirm.disabled=true;
  date.addEventListener('input',()=>{confirm.disabled=!date.value;error.textContent=''});date.addEventListener('change',()=>{confirm.disabled=!date.value;error.textContent=''});
  name.addEventListener('input',()=>{error.textContent=''});
  const cancel=makeButton('Cancel',()=>{box.hidden=true});
  const actions=document.createElement('div');actions.className='editor-toolbar';actions.append(confirm,cancel);
  box.append(heading,nameLabel,dateLabel,error,actions);container.append(box);
  return box;
}
function countdownIcon(name,scale=2) {
  const canvas=document.createElement('canvas');canvas.width=canvas.height=8;canvas.dataset.icon=name;canvas.className='calendar-icon';
  canvas.style.width=canvas.style.height=8*scale+'px';canvas.setAttribute('aria-hidden','true');
  const context=canvas.getContext('2d');(countdownIconArt[name]||countdownIconArt.calendar).forEach((row,y)=>row.forEach((colour,x)=>{if(colour){context.fillStyle=colour;context.fillRect(x,y,1,1)}}));
  return canvas;
}
function calendarTodayParts() {
  try{return Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:editorEffectiveZone(countdownSettings.timezone),year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date()).map(p=>[p.type,Number(p.value)]))}
  catch(_){const d=new Date();return {year:d.getUTCFullYear(),month:d.getUTCMonth()+1,day:d.getUTCDate()}}
}
function holidayDatesFor(key,year) {
  const edited=countdownSettings.holiday_dates?.[key]?.[String(year)];
  if(edited)return [edited];
  for(const pack of Object.values(holidayCatalogue))for(const item of pack.holidays)if(item.key===key)return item.dates[String(year)]||[];
  return [];
}
// Every event shown on a given day: own events (incl. yearly ones) and holidays from switched-on packs.
function calendarEventsByDay(year) {
  const days=new Map(),add=(date,entry)=>{if(!days.has(date))days.set(date,[]);days.get(date).push(entry)};
  for(const event of countdownSettings.events){
    if(!event.start)continue;const date=countdownLocalDate(event.start);
    if(event.annual){const [_,m,d]=date.split('-');const last=new Date(Date.UTC(year,Number(m),0)).getUTCDate();add(`${year}-${m}-${String(Math.min(Number(d),last)).padStart(2,'0')}`,{title:event.title,icon:event.icon,own:event,enabled:event.enabled})}
    else if(date.startsWith(year+'-'))add(date,{title:event.title,icon:event.icon,own:event,enabled:event.enabled});
  }
  for(const [packId,pack] of Object.entries(holidayCatalogue)){
    if(!countdownSettings.packs?.[packId])continue;
    for(const item of pack.holidays){
      if(countdownSettings.hidden_holidays.includes(item.key))continue;
      for(const date of holidayDatesFor(item.key,year))add(date,{title:item.name,icon:item.icon,holiday:item.key,enabled:true});
    }
  }
  return days;
}
let calendarSelected = null; // 'YYYY-MM-DD' chosen in the grid (single tap or Enter)
let calendarFocus = null;    // day holding the roving tabindex
let calendarOpenEvent = null; // event card to keep open across re-renders
const calendarPresets = [
  {id:'birthday',label:'Birthday',icon:'birthday',title:'Birthday',annual:true},
  {id:'anniversary',label:'Anniversary',icon:'anniversary',title:'Anniversary',annual:true},
  {id:'appointment',label:'Appointment',icon:'clock',title:'Appointment',annual:false},
  {id:'haircut',label:'Haircut',icon:'scissors',title:'Haircut',annual:false},
  {id:'school',label:'School',icon:'book',title:'School',annual:false},
  {id:'trip',label:'Trip',icon:'plane',title:'Trip',annual:false},
  {id:'other',label:'Other',icon:'calendar',title:'New event',annual:false},
];
const calendarIso=(y,m,d)=>`${y}-${String(m).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
const calendarTodayIso=()=>{const t=calendarTodayParts();return calendarIso(t.year,t.month,t.day)};
const calendarAddDays=(iso,days)=>{const d=new Date(iso+'T00:00:00Z');d.setUTCDate(d.getUTCDate()+days);return d.toISOString().slice(0,10)};
const calendarLongDate=(iso,options={weekday:'long',day:'numeric',month:'long',year:'numeric'})=>new Date(iso+'T00:00:00Z').toLocaleDateString(undefined,{...options,timeZone:'UTC'});
function calendarRerender() {for(const root of document.querySelectorAll('[data-calendar-root]'))renderCalendarGrid(root)}
function calendarShowMonth(year,month) {calendarMonth={year,month};calendarRerender()}
// Birthdays win the single phone icon, then own events, then holidays.
const calendarRank=entry=>entry.own?.icon==='birthday'?0:entry.own?1:2;
function calendarDayEntries(events,date) {return [...(events.get(date)||[])].sort((a,b)=>calendarRank(a)-calendarRank(b))}
function calendarAge(entry,date) {
  // Age only when a yearly birthday was entered with its real birth year.
  if(entry.own?.icon!=='birthday'||!entry.own.annual)return null;
  const born=Number(countdownLocalDate(entry.own.start).slice(0,4)),year=Number(date.slice(0,4));
  return year>born?year-born:null;
}
function calendarUpcoming(limit=8) {
  const today=calendarTodayIso(),year=Number(today.slice(0,4)),items=[];
  for(const y of [year,year+1])for(const [date,entries] of calendarEventsByDay(y))if(date>=today)for(const entry of entries)if(entry.enabled)items.push({date,entry});
  items.sort((a,b)=>a.date.localeCompare(b.date)||calendarRank(a.entry)-calendarRank(b.entry));
  return items.slice(0,limit);
}
function calendarRelative(date) {
  const days=Math.round((Date.parse(date+'T00:00:00Z')-Date.parse(calendarTodayIso()+'T00:00:00Z'))/86400000);
  return days===0?'today':days===1?'tomorrow':`in ${days} days`;
}
function calendarEntryText(entry,date) {const age=calendarAge(entry,date);return age?`${entry.title} (turns ${age})`:entry.title}
function calendarSelect(date,{focus=false}={}) {
  calendarSelected=date;calendarFocus=date;
  const [y,m]=date.split('-').map(Number);
  if(!calendarMonth||calendarMonth.year!==y||calendarMonth.month!==m-1)calendarMonth={year:y,month:m-1};
  calendarRerender();
  if(focus)document.querySelector(`[data-calendar-day="${date}"]`)?.focus();
}
function calendarMoveFocus(from,days) {
  const date=calendarAddDays(from,days),[y,m]=date.split('-').map(Number);
  calendarFocus=date;
  if(calendarMonth.year!==y||calendarMonth.month!==m-1){calendarMonth={year:y,month:m-1};calendarRerender()}
  else for(const cell of document.querySelectorAll('[data-calendar-day]'))cell.tabIndex=cell.dataset.calendarDay===date?0:-1;
  document.querySelector(`[data-calendar-day="${date}"]`)?.focus();
}
function calendarKey(event,date) {
  const moves={ArrowRight:1,ArrowLeft:-1,ArrowDown:7,ArrowUp:-7};
  if(event.key in moves){event.preventDefault();calendarMoveFocus(date,moves[event.key]);return}
  const weekday=(new Date(date+'T00:00:00Z').getUTCDay()+6)%7;
  if(event.key==='Home'){event.preventDefault();calendarMoveFocus(date,-weekday);return}
  if(event.key==='End'){event.preventDefault();calendarMoveFocus(date,6-weekday);return}
  if(event.key==='PageDown'||event.key==='PageUp'){
    event.preventDefault();const [y,m,d]=date.split('-').map(Number),step=(event.key==='PageDown'?1:-1)*(event.shiftKey?12:1);
    const target=new Date(Date.UTC(y,m-1+step,1)),last=new Date(Date.UTC(target.getUTCFullYear(),target.getUTCMonth()+1,0)).getUTCDate();
    const next=calendarIso(target.getUTCFullYear(),target.getUTCMonth()+1,Math.min(d,last));
    calendarFocus=next;calendarMonth={year:target.getUTCFullYear(),month:target.getUTCMonth()};calendarRerender();
    document.querySelector(`[data-calendar-day="${next}"]`)?.focus();return;
  }
  if(event.key==='Enter'||event.key===' '){event.preventDefault();calendarSelect(date,{focus:true})}
}
function calendarOpenCard(id) {
  calendarOpenEvent=id;
  const card=document.querySelector(`[data-countdown-id="${CSS.escape(id)}"]`);
  if(card){card.open=true;const title=card.querySelector('[data-countdown-field="title"]');card.scrollIntoView({block:'center',behavior:'instant'});title?.focus({preventScroll:true})}
}
function renderCalendarGrid(root) {
  root.replaceChildren();
  const today=calendarTodayParts(),todayIso=calendarTodayIso();calendarMonth??={year:today.year,month:today.month-1};
  const {year,month}=calendarMonth;
  // Coming up next: what the panel counts down to.
  const upcoming=calendarUpcoming(calendarAgendaLimit),next=document.createElement('p');next.className='calendar-next';next.dataset.calendarNext='';
  if(upcoming.length){const first=upcoming[0];next.append(countdownIcon(first.entry.icon||'calendar',2),document.createTextNode(` Coming up next: ${calendarEntryText(first.entry,first.date)}, ${calendarRelative(first.date)}`))}
  else next.textContent='Nothing coming up yet. Tap a day to add an event, or switch on a holiday pack.';
  root.append(next);
  const layout=document.createElement('div');layout.className='calendar-layout';root.append(layout);
  const monthBox=document.createElement('div');monthBox.className='calendar-month';layout.append(monthBox);
  const head=document.createElement('div');head.className='calendar-head';
  const title=document.createElement('strong');title.dataset.calendarTitle='';title.setAttribute('aria-live','polite');
  title.textContent=new Date(Date.UTC(year,month,1)).toLocaleDateString(undefined,{month:'long',year:'numeric',timeZone:'UTC'});
  const previous=makeButton('‹',()=>calendarShowMonth(month?year:year-1,month?month-1:11));previous.setAttribute('aria-label','Previous month');
  const nextMonth=makeButton('›',()=>calendarShowMonth(month===11?year+1:year,(month+1)%12));nextMonth.setAttribute('aria-label','Next month');
  const todayButton=makeButton('Today',()=>calendarSelect(todayIso,{focus:true}));
  head.append(previous,title,nextMonth,todayButton);monthBox.append(head);
  const grid=document.createElement('div');grid.className='calendar-grid';grid.dataset.calendarGrid='';grid.setAttribute('role','grid');grid.setAttribute('aria-label',title.textContent);
  const weekdays=document.createElement('div');weekdays.setAttribute('role','row');weekdays.className='calendar-row calendar-weekdays';
  for(const [short,full] of [['Mo','Monday'],['Tu','Tuesday'],['We','Wednesday'],['Th','Thursday'],['Fr','Friday'],['Sa','Saturday'],['Su','Sunday']]){
    const cell=document.createElement('div');cell.className='calendar-weekday';cell.setAttribute('role','columnheader');cell.setAttribute('aria-label',full);cell.textContent=short;weekdays.append(cell);
  }
  grid.append(weekdays);
  const first=new Date(Date.UTC(year,month,1)),offset=(first.getUTCDay()+6)%7,length=new Date(Date.UTC(year,month+1,0)).getUTCDate();
  const monthPrefix=calendarIso(year,month+1,1).slice(0,8);
  if(!calendarFocus||!calendarFocus.startsWith(monthPrefix))calendarFocus=calendarSelected?.startsWith(monthPrefix)?calendarSelected:todayIso.startsWith(monthPrefix)?todayIso:monthPrefix+'01';
  const events=calendarEventsByDay(year);
  let row=null;
  for(let index=0;index<offset+length;index++){
    if(index%7===0){row=document.createElement('div');row.className='calendar-row';row.setAttribute('role','row');grid.append(row)}
    if(index<offset){const blank=document.createElement('div');blank.className='calendar-day calendar-blank';blank.setAttribute('role','gridcell');row.append(blank);continue}
    const day=index-offset+1,date=calendarIso(year,month+1,day),entries=calendarDayEntries(events,date);
    const cell=document.createElement('div');cell.className='calendar-day';cell.dataset.calendarDay=date;cell.setAttribute('role','gridcell');
    cell.tabIndex=date===calendarFocus?0:-1;cell.setAttribute('aria-selected',String(date===calendarSelected));
    if(date===todayIso){cell.classList.add('today');cell.setAttribute('aria-current','date')}
    if(entries.length)cell.classList.add('busy');
    cell.setAttribute('aria-label',calendarLongDate(date)+(entries.length?`, ${entries.length} event${entries.length>1?'s':''}: ${entries.map(e=>calendarEntryText(e,date)).join(', ')}`:', no events'));
    const number=document.createElement('span');number.className='calendar-number';number.textContent=day;cell.append(number);
    entries.forEach((entry,position)=>{
      const chip=document.createElement('div');chip.className='calendar-event'+(entry.holiday?' holiday':'')+(entry.enabled?'':' off')+(position?' extra':'');
      chip.dataset.calendarEvent=entry.holiday||entry.own?.id||'';chip.title=calendarEntryText(entry,date);
      const label=document.createElement('span');label.textContent=entry.title;chip.append(countdownIcon(entry.icon||'calendar'),label);cell.append(chip);
    });
    if(entries.length>1){const more=document.createElement('small');more.className='calendar-more';more.dataset.calendarMore='';more.textContent='+'+(entries.length-1);more.setAttribute('aria-hidden','true');cell.append(more)}
    cell.addEventListener('click',()=>{calendarSelect(date,{focus:true});calendarRevealDayPanel()});
    cell.addEventListener('keydown',event=>calendarKey(event,date));
    row.append(cell);
  }
  monthBox.append(grid);
  const legend=document.createElement('p');legend.className='editor-note';legend.textContent='Tap a day to see it and add an event. Holidays from switched-on packs show automatically (outlined).';monthBox.append(legend);
  const side=document.createElement('div');side.className='calendar-side';layout.append(side);
  if(calendarSelected)side.append(renderCalendarDayPanel(calendarSelected,calendarEventsByDay(Number(calendarSelected.slice(0,4)))));
  const agenda=document.createElement('section');agenda.className='calendar-agenda';agenda.dataset.calendarAgenda='';
  const agendaTitle=document.createElement('h4');agendaTitle.textContent='Upcoming';agenda.append(agendaTitle);
  const list=document.createElement('ul');agenda.append(list);
  for(const {date,entry} of upcoming){
    const item=document.createElement('li');
    const button=document.createElement('button');button.type='button';button.className='calendar-agenda-item';
    Object.assign(button.dataset,{agendaItem:'',date,title:entry.title});if(entry.own)button.dataset.eventId=entry.own.id;if(entry.holiday)button.dataset.holiday=entry.holiday;
    const when=document.createElement('small');when.textContent=`${calendarLongDate(date,{weekday:'short',day:'numeric',month:'short'})} · ${calendarRelative(date)}`;
    const name=document.createElement('span');name.textContent=calendarEntryText(entry,date);
    button.append(countdownIcon(entry.icon||'calendar',2),name,when);
    button.addEventListener('click',()=>{calendarSelect(date,{focus:true});const panel=document.querySelector('[data-calendar-day-panel]');panel?.scrollIntoView({block:'nearest',behavior:'instant'})});
    item.append(button);list.append(item);
  }
  if(!upcoming.length){const empty=document.createElement('p');empty.className='editor-note';empty.textContent='No upcoming events.';agenda.append(empty)}
  if(upcoming.length>=calendarAgendaLimit||calendarAgendaLimit>8){
    const more=makeButton(calendarAgendaLimit>8?'Show fewer':'Show more',()=>{calendarAgendaLimit=calendarAgendaLimit>8?8:60;calendarRerender();document.querySelector('[data-calendar-agenda-more]')?.focus()});
    more.dataset.calendarAgendaMore='';agenda.append(more);
  }
  side.append(agenda);
}
function renderCalendarDayPanel(date,events) {
  const panel=document.createElement('section');panel.className='calendar-day-panel';panel.dataset.calendarDayPanel=date;panel.setAttribute('aria-label','Selected day');panel.setAttribute('aria-live','polite');
  const heading=document.createElement('h4');heading.textContent=calendarLongDate(date);panel.append(heading);
  const entries=calendarDayEntries(events,date);
  if(entries.length){
    const list=document.createElement('ul');list.className='calendar-day-list';
    for(const entry of entries){
      const item=document.createElement('li');item.append(countdownIcon(entry.icon||'calendar',2),document.createTextNode(' '+calendarEntryText(entry,date)+' '));
      if(entry.own){const edit=makeButton('Edit',()=>calendarOpenCard(entry.own.id));edit.dataset.calendarEdit=entry.own.id;item.append(edit)}
      else{const small=document.createElement('small');small.textContent=countdownSettings.holiday_dates?.[entry.holiday]?.[date.slice(0,4)]?'holiday · edited date':'holiday';item.append(small)}
      list.append(item);
    }
    panel.append(list);
  } else editorParagraph(panel,'Nothing on this day yet.').className='editor-note';
  const label=document.createElement('p');label.className='calendar-preset-label';label.textContent='Add on this day:';panel.append(label);
  const presets=document.createElement('div');presets.className='calendar-presets';
  for(const preset of calendarPresets){
    const button=makeButton('',()=>{
      // Same preset on the same day: open the event that is already there instead of a silent duplicate.
      const existing=countdownSettings.events.find(event=>event.title===preset.title&&event.icon===preset.icon&&countdownLocalDate(event.start)===date);
      if(existing){calendarOpenCard(existing.id);setMessage(`${preset.title} is already on this day, so it was opened instead of added again.`);return}
      // Birthdays need the real birth year for "turns N": open the date-of-birth prompt on this day.
      if(preset.id==='birthday'){calendarOpenBirthdayPrompt(date);return}
      calendarAddEvent({title:preset.title,icon:preset.icon,annual:preset.annual,all_day:true,start:date});
    });
    button.dataset.calendarPreset=preset.id;button.append(countdownIcon(preset.icon,2),document.createTextNode(' '+preset.label));presets.append(button);
  }
  panel.append(presets);
  return panel;
}
// Keep the selected day's presets visible above the sticky Save bar (phones stack the panel under the grid).
function calendarRevealDayPanel() {
  const presets=document.querySelector('[data-calendar-day-panel] .calendar-presets'),bar=document.querySelector('.sticky-save');
  if(!presets)return;const limit=(bar?bar.getBoundingClientRect().top:innerHeight)-8,box=presets.getBoundingClientRect();
  if(box.bottom>limit)scrollBy({top:box.bottom-limit,behavior:'instant'});
}
function calendarAddEvent(changes={},focus=true) {
  const bytes=crypto.getRandomValues(new Uint8Array(12)),id='event_'+Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');
  countdownSettings.events.push({id,title:'New event',start:countdownLocalDate(new Date().toISOString()),end:null,all_day:true,annual:false,enabled:true,now_minutes:15,accent:null,icon:'calendar',...changes});
  if(focus)calendarOpenEvent=id;
  countdownChanged();renderCountdownEditors();
  if(focus){const input=document.querySelector(`[data-countdown-id="${id}"] [data-countdown-field="title"]`);if(input){input.scrollIntoView({block:'center',behavior:'instant'});input.focus();input.select()}}
  return id;
}
function renderHolidayPacks(container) {
  const group=document.createElement('details');group.className='editor-group calendar-packs';
  const on=Object.entries(holidayCatalogue).filter(([id])=>countdownSettings.packs?.[id]).map(([,pack])=>pack.name);
  group.open=calendarPacksOpen??!on.length;group.addEventListener('toggle',()=>{calendarPacksOpen=group.open});
  const summary=document.createElement('summary');summary.textContent='Holiday packs: '+(on.length?on.join(', '):'none switched on')+(group.open?'':' (tap to choose)');group.append(summary);
  group.addEventListener('toggle',()=>{summary.textContent='Holiday packs: '+(on.length?on.join(', '):'none switched on')+(group.open?'':' (tap to choose)')});
  editorParagraph(group,'Switch on the holidays you celebrate. Dates are worked out automatically for every year, offline. You can change a date for one year if your community marks it on a different day.');
  const list=document.createElement('div');list.className='calendar-pack-list';group.append(list);
  for(const [packId,pack] of Object.entries(holidayCatalogue)){
    const row=document.createElement('div');row.className='calendar-pack';
    const label=document.createElement('label');label.className='editor-check';
    const box=document.createElement('input');box.type='checkbox';box.dataset.calendarPack=packId;box.checked=!!countdownSettings.packs?.[packId];
    box.addEventListener('change',()=>{countdownSettings.packs[packId]=box.checked;countdownChanged();renderCountdownEditors()});
    const name=document.createElement('span');name.textContent=pack.name;label.append(box,name);
    const icons=document.createElement('span');icons.className='calendar-pack-icons';for(const icon of [...new Set(pack.holidays.map(item=>item.icon))].slice(0,4))icons.append(countdownIcon(icon,1.5));
    const edit=makeButton('Dates',()=>{const panel=row.querySelector('.calendar-pack-dates');panel.hidden=!panel.hidden;edit.setAttribute('aria-expanded',String(!panel.hidden));if(!panel.hidden)renderPackDates(panel,packId)});
    edit.dataset.calendarPackEdit=packId;edit.setAttribute('aria-expanded','false');edit.setAttribute('aria-label',`${pack.name} dates`);
    const panel=document.createElement('div');panel.className='calendar-pack-dates';panel.hidden=true;
    row.append(label,icons,edit,panel);list.append(row);
  }
  container.append(group);
}
function renderPackDates(panel,packId) {
  const pack=holidayCatalogue[packId];panel.replaceChildren();
  editorParagraph(panel,pack.note);
  const today=calendarTodayParts(),years=Object.keys(pack.holidays[0]?.dates||{}).map(Number).filter(year=>year>=today.year);
  const yearSelect=document.createElement('select');yearSelect.dataset.calendarDatesYear=packId;
  for(const year of years){const option=document.createElement('option');option.value=year;option.textContent=year;yearSelect.append(option)}
  yearSelect.value=String(panel.dataset.year||years[0]);
  const yearLabel=document.createElement('label');yearLabel.textContent='Year ';yearLabel.append(yearSelect);panel.append(yearLabel);
  const table=document.createElement('div');table.className='calendar-date-table';panel.append(table);
  const rerenderGrids=()=>{for(const grid of document.querySelectorAll('[data-calendar-root]'))renderCalendarGrid(grid)};
  const fill=()=>{
    panel.dataset.year=yearSelect.value;table.replaceChildren();
    const shown=[Number(yearSelect.value)]; // the Year select filters to exactly one year
    for(const item of pack.holidays){
      if(!shown.some(year=>(item.dates[String(year)]||[]).length))continue;
      const block=document.createElement('div');block.className='calendar-date-holiday';
      // One switch per holiday: hiding applies to every year.
      const showLabel=document.createElement('label');showLabel.className='editor-check calendar-date-name';
      const show=document.createElement('input');show.type='checkbox';show.dataset.holidayShow=item.key;show.checked=!countdownSettings.hidden_holidays.includes(item.key);
      show.addEventListener('change',()=>{const hidden=new Set(countdownSettings.hidden_holidays);show.checked?hidden.delete(item.key):hidden.add(item.key);countdownSettings.hidden_holidays=[...hidden];block.classList.toggle('off',!show.checked);countdownChanged()});
      const showText=document.createElement('span');showText.textContent=` ${item.name} `;const every=document.createElement('small');every.textContent='(show every year)';
      showLabel.append(show,countdownIcon(item.icon,1.5),showText,every);block.append(showLabel);block.classList.toggle('off',!show.checked);
      for(const year of shown){
        const calculated=item.dates[String(year)]||[];if(!calculated.length)continue;
        const edited=countdownSettings.holiday_dates?.[item.key]?.[String(year)];
        const line=document.createElement('div');line.className='calendar-date-row';
        const yearText=document.createElement('span');yearText.className='calendar-date-year';yearText.textContent=year;
        const input=document.createElement('input');input.type='date';input.dataset.holidayDate=item.key;input.dataset.year=year;input.value=edited||calculated[0];
        input.min=`${year-1}-12-01`;input.max=`${year+1}-01-31`;input.setAttribute('aria-label',`${item.name} ${year} date`);
        const status=document.createElement('small');status.textContent=edited?'Edited':'Calculated';status.setAttribute('aria-live','polite');
        const reset=makeButton('Use calculated',()=>{const dates=countdownSettings.holiday_dates[item.key];if(dates){delete dates[String(year)];if(!Object.keys(dates).length)delete countdownSettings.holiday_dates[item.key]}input.value=calculated[0];input.removeAttribute('aria-invalid');status.textContent='Calculated';if(document.activeElement===reset)input.focus();reset.disabled=true;countdownChanged()});
        reset.dataset.holidayReset=item.key;reset.dataset.year=year;reset.disabled=!edited;reset.setAttribute('aria-label',`Use calculated ${item.name} ${year} date`);
        // Commit only complete, in-range dates on change; typing never re-renders this panel.
        input.addEventListener('change',()=>{
          const value=input.value;
          if(!value){input.value=countdownSettings.holiday_dates?.[item.key]?.[String(year)]||calculated[0];return}
          if(value<input.min||value>input.max){input.setAttribute('aria-invalid','true');status.textContent=`Pick a date near ${year}`;return}
          input.removeAttribute('aria-invalid');
          if(value===calculated[0]){reset.click();return}
          (countdownSettings.holiday_dates[item.key]??={})[String(year)]=value;status.textContent='Edited';reset.disabled=false;countdownChanged();
        });
        input.addEventListener('blur',()=>{if(input.getAttribute('aria-invalid')!=='true')return;input.value=countdownSettings.holiday_dates?.[item.key]?.[String(year)]||calculated[0];input.removeAttribute('aria-invalid');status.textContent=countdownSettings.holiday_dates?.[item.key]?.[String(year)]?'Edited':'Calculated'});
        line.append(yearText,input,status,reset);block.append(line);
      }
      table.append(block);
    }
  };
  yearSelect.addEventListener('change',fill);fill();
}
function syncCountdownMediaSelects() {
  const choices=basicMediaChoices();
  for(const select of document.querySelectorAll('select[data-countdown-field="artwork"]')){
    const current=select.value;
    select.replaceChildren();
    for(const value of ['',...choices]){
      const option=document.createElement('option');option.value=value;
      option.textContent=value||'Bundled type icon (no artwork)';select.append(option);
    }
    if(current&&!choices.includes(current)){
      const option=document.createElement('option');option.value=current;option.textContent=current+' (missing)';select.append(option);
    }
    select.value=current;
  }
}
function renderCountdownWarning() {
  const info=countdownPreviewInfo, messages=[];
  if(info){
    if(info.title_overflow)messages.push(`Title clips: ${info.title_width} × ${info.title_height} px in ${info.title_available_width} × ${info.title_available_height} px.`);
    if(info.title_scrolls)messages.push(`Title scrolls one pass: ${info.title_width} px across ${info.title_available_width} px, then restores.`);
    if(info.countdown_overflow)messages.push(`Countdown clips (${info.countdown_font}): ${info.countdown_width} × ${info.countdown_height} px in ${info.countdown_available_width} × ${info.countdown_available_height} px.`);
    if(!info.title_overflow&&!info.countdown_overflow)messages.push('Measured layout fits (horizontal title overflow scrolls).');
  }
  for(const warning of document.querySelectorAll('[data-countdown-warning]'))warning.textContent=info?messages.join(' '):(countdownSettings.events.length?'Press Preview to check that the title and countdown fit.':'');
}
function clearCountdownPreviewInfo() {countdownPreviewInfo=null;renderCountdownWarning();}
function updateCountdownPreviewInfo(info,generation) {
  if(!editPreviewCurrent(generation)||previewModule!=='event_countdown')return;
  countdownPreviewInfo=info||null;renderCountdownWarning();
}
// The same draft serializer is used by Save, Edit Preview and device preview.
const configBodyWithoutCountdown = configBody;
configBody = function(previewMedia=false) {
  return {...configBodyWithoutCountdown(previewMedia), event_countdown:structuredClone(countdownSettings)};
};
{
  const template = document.querySelector('#built-in-editor-templates').content;
  const marker = document.createElement('div');
  marker.dataset.moduleEditor = 'event_countdown';
  template.append(marker);
  for (const row of document.querySelectorAll('.module-row[data-module="event_countdown"]')) {
    const editor = marker.cloneNode();
    editor.className = 'module-editor'; editor.hidden = true;
    row.closest('.module-item').append(editor);
  }
}
function countdownChanged() {
  clearCountdownPreviewInfo();
  for(const grid of document.querySelectorAll('[data-calendar-root]'))renderCalendarGrid(grid);
  markMatrixDirty();
  globalThis.publishDeviceDraftMutation?.('event_countdown',150);
  if(previewMode==='edit' && previewModule==='event_countdown') scheduleEditPreview(150);
}
function renderCountdownEditors() {
  for(const editor of document.querySelectorAll('.module-editor[data-module-editor="event_countdown"]')) renderCountdownEditor(editor);
}
function countdownLocalDate(value) {
  // Date-only and naive timestamps are already local civil dates. Never let
  // the browser's timezone reinterpret them; only offset-bearing values are instants.
  if(!/T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value))return value.slice(0,10);
  const parts=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{
    timeZone:editorEffectiveZone(countdownSettings.timezone),year:'numeric',month:'2-digit',day:'2-digit'
  }).formatToParts(new Date(value)).map(part=>[part.type,part.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}
function renderCountdownEditor(container) {
  container.replaceChildren();
  const note=document.createElement('p');
  note.textContent='Your calendar: birthdays, holidays and other days. The display counts down to the next one. Switch on holiday packs below, or add your own events. Save applies changes.';
  container.append(note);
  const playlist=document.createElement('p');playlist.className='calendar-playlist-off';playlist.dataset.calendarPlaylistOff='';playlist.setAttribute('role','status');
  playlist.append(document.createTextNode('Calendar is not in the playlist yet, so it will not show on the display. '));
  const playlistOn=makeButton('Show it on the display',()=>{const box=calendarPlaylistBox();if(box&&!box.checked){box.click()}renderCalendarPlaylistNotice()});
  playlistOn.dataset.calendarPlaylistOn='';playlist.append(playlistOn);container.append(playlist);renderCalendarPlaylistNotice();
  const calendar=document.createElement('div');calendar.className='calendar';calendar.dataset.calendarRoot='';container.append(calendar);renderCalendarGrid(calendar);
  const quick=document.createElement('div');quick.className='editor-toolbar';
  const birthday=makeButton('Add a birthday',()=>calendarOpenBirthdayPrompt());
  birthday.dataset.calendarAddBirthday='';quick.append(birthday);container.append(quick);renderBirthdayPrompt(container);
  editorParagraph(container,'Birthdays (cake icon) show confetti on the day.').className='editor-note';
  renderHolidayPacks(container);
  const details=document.createElement('details');details.className='editor-group';const summary=document.createElement('summary');summary.textContent='How dates and the progress bar work';
  const detailText=document.createElement('p');detailText.textContent='Dates use the time zone below (the Weather location’s by default). For all-day events, the end date is the day after the last day. If a clock change makes a time happen twice, the first one is used; times skipped by a clock change are rejected. Long titles scroll once, then stay still. The progress bar fills over the 30 days before the event (full more than 30 days out, empty at NOW). Google Calendar is not connected.';
  details.append(summary,detailText);editorAdvanced(details);container.append(details);
  const fields=document.createElement('div');fields.className='editor-fields';container.append(fields);
  function control(parent,obj,key,label,type='text',options=null,scope='field') {
    const wrapper=document.createElement('label');wrapper.textContent=label;
    const input=key==='timezone'?editorTimezoneSelect(obj[key]):document.createElement(options?'select':'input');
    input.dataset[scope==='setting'?'countdownSetting':'countdownField']=key;
    if(options)for(const option of options){const el=document.createElement('option');el.value=option;el.textContent=option||'Shared accent';input.append(el)}
    else if(key!=='timezone')input.type=type;
    if(type==='checkbox')input.checked=obj[key];else input.value=obj[key]??'';
    input.addEventListener(type==='checkbox'||options||key==='timezone'?'change':'input',()=>{
      obj[key]=type==='checkbox'?input.checked:type==='number'?Number(input.value):input.value;
      if((key==='end'||key==='accent'||key==='artwork')&&input.value==='')obj[key]=null;
      if(key==='all_day'){
        for(const dateKey of ['start','end'])if(obj[dateKey]){
          obj[dateKey]=input.checked?countdownLocalDate(obj[dateKey]):obj[dateKey].slice(0,10)+'T00:00';
          parent.querySelector(`[data-countdown-field="${dateKey}"]`).value=obj[dateKey];
        }
        if(input.checked&&obj.end&&obj.end<=obj.start){
          const next=new Date(obj.start+'T00:00:00Z');next.setUTCDate(next.getUTCDate()+1);obj.end=next.toISOString().slice(0,10);
          parent.querySelector('[data-countdown-field="end"]').value=obj.end;
        }
      }
      countdownChanged();
    });
    if(type==='checkbox')wrapper.classList.add('editor-check');
    wrapper.append(input);parent.append(wrapper);return input;
  }
  const field=input=>input.closest('label');
  const layout=control(fields,countdownSettings,'layout','Style','text',['large','icon','progress','tearoff','flip','ring','hero','date'],'setting');
  for(const [value,text] of [['large','Big countdown'],['icon','Icon and countdown'],['progress','Countdown with progress bar'],['tearoff','Tear-off calendar page'],['flip','Flip clock (days, hours, minutes)'],['ring','Progress ring'],['hero','Big icon with sparkles'],['date','Date card']])layout.querySelector(`option[value="${value}"]`).textContent=text;
  for(const [key,text] of [['background','Background colour'],['text_colour','Text colour'],['accent','Highlight colour']])control(fields,countdownSettings,key,text,'color',null,'setting');
  const zoneSelect=control(fields,countdownSettings,'timezone','Time zone','text',null,'setting');editorFollowWeatherLabel(zoneSelect);editorAdvanced(field(zoneSelect));
  editorAdvanced(field(control(fields,countdownSettings,'title_font','Title font','text',installedFonts,'setting')));
  const titleAlign=control(fields,countdownSettings,'title_align','Title alignment','text',['center','left','right'],'setting');
  for(const [value,text] of [['center','Centre'],['left','Left'],['right','Right']])titleAlign.querySelector(`option[value="${value}"]`).textContent=text;
  editorAdvanced(field(titleAlign));
  editorAdvanced(field(control(fields,countdownSettings,'countdown_font','Countdown font','text',['auto',...installedFonts],'setting')));
  // Owner 2026-10-14: bar animations (Solid is one option). Shown in Simple mode with the Style.
  const barStyle=control(fields,countdownSettings,'progress_style','Bar animation','text',['solid','gradient','shimmer','pulse','stripes','segments','dots','rainbow'],'setting');
  for(const [value,text] of [['solid','Solid'],['gradient','Gradient'],['shimmer','Shimmer (light sweeps along)'],['pulse','Pulse (gently breathes)'],['stripes','Moving stripes'],['segments','Segments'],['dots','Dots'],['rainbow','Rainbow (colours flow)']])barStyle.querySelector(`option[value="${value}"]`).textContent=text;
  const barStyleField=field(barStyle);barStyleField.dataset.countdownBarStyle='';
  const syncBarStyle=()=>{barStyleField.hidden=countdownSettings.layout!=='progress'};syncBarStyle();layout.addEventListener('change',syncBarStyle);
  const bar=control(fields,countdownSettings,'progress_height','Progress bar thickness (1–4 pixels)','number',null,'setting');bar.min=1;bar.max=4;editorAdvanced(field(bar));
  for(const edge of ['top','right','bottom','left']){const input=control(fields,countdownSettings.insets,edge,'Space from '+edge+' edge (0–8 pixels)','number',null,'setting');input.min=0;input.max=8;editorAdvanced(field(input));}
  const warning=document.createElement('p');warning.dataset.countdownWarning='';warning.setAttribute('role','status');container.append(warning);renderCountdownWarning();
  const add=makeButton('Add event',()=>{
    let parts;
    try{parts=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:editorEffectiveZone(countdownSettings.timezone),year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date()).map(part=>[part.type,part.value]));}
    catch(_){setMessage('Enter a valid Matrix event timezone before adding an event.',true);return;}
    calendarAddEvent({title:'New event',all_day:true,start:`${parts.year}-${parts.month}-${parts.day}`});
  });add.dataset.countdownAdd='';container.append(add);
  if(calendarUndo){
    const undoBar=document.createElement('p');undoBar.className='calendar-undo';undoBar.setAttribute('role','status');
    undoBar.append(document.createTextNode(`Deleted “${calendarUndo.event.title}”. `));
    const undo=makeButton('Undo',()=>{const {event,index}=calendarUndo;calendarUndo=null;countdownSettings.events.splice(Math.min(index,countdownSettings.events.length),0,event);calendarOpenEvent=event.id;countdownChanged();renderCountdownEditors();document.querySelector(`[data-countdown-id="${CSS.escape(event.id)}"] [data-countdown-field="title"]`)?.focus()});
    undo.dataset.countdownUndo='';undoBar.append(undo);container.append(undoBar);
  }
  countdownSettings.events.forEach((event,index)=>{
    const card=document.createElement('details');card.className='countdown-event editor-fields';card.dataset.countdownId=event.id;
    card.open=calendarOpenEvent===event.id||countdownSettings.events.length===1;card.addEventListener('toggle',()=>{if(card.open)calendarOpenEvent=event.id;else if(calendarOpenEvent===event.id)calendarOpenEvent=null});
    const legend=document.createElement('summary');legend.className='countdown-event-summary';
    const summaryText=()=>{const ok=countdownParseDate(event.start,event.all_day),date=ok?countdownLocalDate(ok):'';if(!ok)return `${event.title||'Event '+(index+1)} · date needs fixing`;return `${event.title||'Event '+(index+1)}${date?' · '+(event.annual?calendarLongDate(date,{day:'numeric',month:'short'})+' every year':calendarLongDate(date,{day:'numeric',month:'short',year:'numeric'})):''}${event.enabled?'':' · hidden'}`};
    const summaryLabel=document.createElement('span');summaryLabel.textContent=summaryText();legend.append(countdownIcon(event.icon,2),summaryLabel);card.append(legend);
    card.addEventListener('input',()=>{summaryLabel.textContent=summaryText()});card.addEventListener('change',()=>{summaryLabel.textContent=summaryText();legend.querySelector('canvas').replaceWith(countdownIcon(event.icon,2))});
    control(card,event,'title','Event name');
    // Owner 2026-10-14: start and end dates share one entry format (typed UK date, picker, inline error).
    const dateEntry=(key,label,optional)=>{
      const input=control(card,event,key,label);input.placeholder='DD/MM/YYYY';input.autocomplete='off';
      const picker=document.createElement('input');picker.dataset.countdownPicker=key;picker.setAttribute('aria-label',`${event.title||'Event'} ${key==='end'?'end ':''}date picker`);
      const error=document.createElement('small');error.className='calendar-error';error.dataset.countdownDateError=key;error.setAttribute('role','alert');
      const sync=()=>{picker.type=event.all_day?'date':'datetime-local';const blank=optional&&!event[key];const ok=blank?null:countdownParseDate(event[key],event.all_day);picker.value=ok?(event.all_day?ok:ok.slice(0,16)):'';
        if(ok||blank)input.removeAttribute('aria-invalid');else input.setAttribute('aria-invalid','true');
        error.textContent=ok||blank?'':'Not a date the calendar understands. Use the picker, or type it like 25/12/2026'+(event.all_day?'.':' 18:00.')};
      // Typed dates are normalised to ISO when you leave the field (or press Enter).
      input.addEventListener('change',()=>{
        if(optional&&!input.value.trim()){event[key]=null;sync();return}
        // A typed time on an all-day event makes it a timed event rather than silently dropping the time.
        if(key==='start'&&event.all_day&&!countdownParseDate(input.value,true)&&countdownParseDate(input.value,false)){event.all_day=false;const box=card.querySelector('[data-countdown-field="all_day"]');if(box)box.checked=false}
        const ok=countdownParseDate(input.value,event.all_day);if(ok){const changed=ok!==event[key];event[key]=ok;input.value=ok;if(changed)countdownChanged()}sync()});
      picker.addEventListener('change',()=>{if(!picker.value)return;event[key]=picker.value;input.value=picker.value;sync();countdownChanged();card.dispatchEvent(new Event('change'))});
      input.closest('label').after(picker,error);sync();
      card.addEventListener('change',event2=>{if(event2.target?.dataset?.countdownField==='all_day')sync()});
      return {input,picker,error};
    };
    dateEntry('start','Date (pick it, or type e.g. 25/12/2026 or 25/12/2026 18:00)',false);
    const icon=control(card,event,'icon','Icon','text',Object.keys(countdownIconNames));
    for(const option of icon.options)option.textContent=countdownIconNames[option.value];
    let iconPreview=countdownIcon(event.icon,2);icon.closest('label').append(iconPreview);
    icon.addEventListener('change',()=>{const next=countdownIcon(icon.value,2);iconPreview.replaceWith(next);iconPreview=next});
    control(card,event,'enabled','Show this event','checkbox');
    control(card,event,'all_day','All day','checkbox');
    control(card,event,'annual','Every year','checkbox');
    {const end=dateEntry('end','End date (optional; the day after it finishes)',true);editorAdvanced(field(end.input),end.picker,end.error);}
    const minutes=control(card,event,'now_minutes','Show “NOW” for (minutes)','number');minutes.min=1;minutes.max=10080;editorAdvanced(field(minutes));
    const accent=control(card,event,'accent','Own highlight colour (e.g. #FF8800; blank uses the shared one)');accent.placeholder='Shared colour';editorAdvanced(field(accent));
    const artwork=control(card,event,'artwork','Picture from media library (Icon, Progress ring and Big icon styles; up to 8×8)','text',['',...basicMediaChoices()]);editorAdvanced(field(artwork));
    artwork.options[0].textContent='Use the icon above';
    if(event.artwork&&!basicMediaChoices().includes(event.artwork)){
      const missing=document.createElement('option');missing.value=event.artwork;missing.textContent=event.artwork+' (missing)';artwork.append(missing);artwork.value=event.artwork;
    }
    const remove=makeButton('Delete event',()=>{const [gone]=countdownSettings.events.splice(index,1);calendarUndo={event:gone,index};countdownChanged();renderCountdownEditors();container.querySelector('[data-countdown-undo]')?.focus()},'btn danger');remove.dataset.countdownDelete='';card.append(remove);
    const up=makeButton('Move event up',()=>{if(index){const events=countdownSettings.events;[events[index-1],events[index]]=[events[index],events[index-1]];countdownChanged();renderCountdownEditors()}});up.disabled=index===0;card.append(up);editorAdvanced(up);
    container.append(card);
  });
  editorAdvancedToggle(container,'event_countdown');
}
