let countdownSettings = {{ config.event_countdown|tojson }};
let countdownPreviewInfo = null;
// Holiday packs and their offline-calculated dates (server), plus the panel's icon pixels.
const holidayCatalogue = {{ holiday_catalogue|tojson }};
const countdownIconArt = {{ countdown_icon_art|tojson }};
const countdownIconNames = {calendar:'Calendar',birthday:'Birthday cake',anniversary:'Heart (anniversary)',launch:'Rocket',holiday:'Holiday',opening:'Door (opening)',
  tree:'Christmas tree',pumpkin:'Pumpkin',egg:'Easter egg',cross:'Cross',dove:'Dove',star:'Star',heart:'Heart',firework:'Fireworks',crescent:'Crescent moon',
  lantern:'Lantern',star_of_david:'Star of David',menorah:'Menorah',shofar:'Shofar',diya:'Diya lamp',holi:'Holi colours',lotus:'Lotus',khanda:'Khanda',leaf:'Leaf'};
let calendarMonth = null; // {year, month 0-11} shown in the grid
function countdownIcon(name,scale=2) {
  const canvas=document.createElement('canvas');canvas.width=canvas.height=8;canvas.dataset.icon=name;canvas.className='calendar-icon';
  canvas.style.width=canvas.style.height=8*scale+'px';canvas.setAttribute('aria-hidden','true');
  const context=canvas.getContext('2d');(countdownIconArt[name]||countdownIconArt.calendar).forEach((row,y)=>row.forEach((colour,x)=>{if(colour){context.fillStyle=colour;context.fillRect(x,y,1,1)}}));
  return canvas;
}
function calendarTodayParts() {
  try{return Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:countdownSettings.timezone,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date()).map(p=>[p.type,Number(p.value)]))}
  catch(_){const d=new Date();return {year:d.getFullYear(),month:d.getMonth()+1,day:d.getDate()}}
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
function calendarShowMonth(year,month) {calendarMonth={year,month};for(const grid of document.querySelectorAll('[data-calendar-root]'))renderCalendarGrid(grid)}
function renderCalendarGrid(root) {
  root.replaceChildren();
  const today=calendarTodayParts();calendarMonth??={year:today.year,month:today.month-1};
  const {year,month}=calendarMonth,head=document.createElement('div');head.className='calendar-head';
  const title=document.createElement('strong');title.dataset.calendarTitle='';title.textContent=new Date(Date.UTC(year,month,1)).toLocaleDateString(undefined,{month:'long',year:'numeric',timeZone:'UTC'});
  const previous=makeButton('‹',()=>calendarShowMonth(month?year:year-1,month?month-1:11));previous.setAttribute('aria-label','Previous month');
  const next=makeButton('›',()=>calendarShowMonth(month===11?year+1:year,(month+1)%12));next.setAttribute('aria-label','Next month');
  const todayButton=makeButton('Today',()=>calendarShowMonth(today.year,today.month-1));
  head.append(previous,title,next,todayButton);root.append(head);
  const grid=document.createElement('div');grid.className='calendar-grid';grid.dataset.calendarGrid='';grid.setAttribute('role','grid');
  for(const name of ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']){const cell=document.createElement('div');cell.className='calendar-weekday';cell.textContent=name;grid.append(cell)}
  const first=new Date(Date.UTC(year,month,1)),offset=(first.getUTCDay()+6)%7,length=new Date(Date.UTC(year,month+1,0)).getUTCDate();
  for(let i=0;i<offset;i++){const blank=document.createElement('div');blank.className='calendar-day empty';grid.append(blank)}
  const events=calendarEventsByDay(year);
  for(let day=1;day<=length;day++){
    const date=`${year}-${String(month+1).padStart(2,'0')}-${String(day).padStart(2,'0')}`,cell=document.createElement('div');
    cell.className='calendar-day';cell.dataset.calendarDay=date;cell.setAttribute('role','gridcell');
    if(year===today.year&&month===today.month-1&&day===today.day)cell.classList.add('today');
    const number=document.createElement('span');number.className='calendar-number';number.textContent=day;cell.append(number);
    for(const entry of events.get(date)||[]){
      const chip=document.createElement('div');chip.className='calendar-event'+(entry.holiday?' holiday':'')+(entry.enabled?'':' off');chip.dataset.calendarEvent=entry.holiday||entry.own?.id||'';
      chip.title=entry.title;const label=document.createElement('span');label.textContent=entry.title;chip.append(countdownIcon(entry.icon||'calendar'),label);cell.append(chip);
    }
    cell.addEventListener('dblclick',()=>calendarAddEvent({start:date}));
    grid.append(cell);
  }
  root.append(grid);
  const hint=document.createElement('p');hint.className='editor-note';hint.textContent='Double-click a day to add an event on it. Holidays from switched-on packs show automatically.';root.append(hint);
}
function calendarAddEvent(changes={}) {
  const bytes=crypto.getRandomValues(new Uint8Array(12)),id='event_'+Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');
  countdownSettings.events.push({id,title:'New event',start:countdownLocalDate(new Date().toISOString()),end:null,all_day:true,annual:false,enabled:true,now_minutes:15,accent:null,icon:'calendar',...changes});
  countdownChanged();renderCountdownEditors();
}
function renderHolidayPacks(container) {
  const group=document.createElement('details');group.className='editor-group calendar-packs';group.open=true;
  const summary=document.createElement('summary');summary.textContent='Holiday packs';group.append(summary);
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
    edit.dataset.calendarPackEdit=packId;edit.setAttribute('aria-expanded','false');
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
  const fill=()=>{
    panel.dataset.year=yearSelect.value;table.replaceChildren();
    for(const year of years.length>2?[Number(yearSelect.value),Number(yearSelect.value)+1]:years)for(const item of pack.holidays){
      const calculated=item.dates[String(year)]||[],edited=countdownSettings.holiday_dates?.[item.key]?.[String(year)];
      if(!calculated.length)continue;
      const line=document.createElement('div');line.className='calendar-date-row';
      const show=document.createElement('input');show.type='checkbox';show.dataset.holidayShow=item.key;show.checked=!countdownSettings.hidden_holidays.includes(item.key);show.setAttribute('aria-label','Show '+item.name);
      show.addEventListener('change',()=>{const hidden=new Set(countdownSettings.hidden_holidays);show.checked?hidden.delete(item.key):hidden.add(item.key);countdownSettings.hidden_holidays=[...hidden];countdownChanged();for(const grid of document.querySelectorAll('[data-calendar-root]'))renderCalendarGrid(grid)});
      const name=document.createElement('span');name.className='calendar-date-name';name.append(countdownIcon(item.icon,1.5),document.createTextNode(` ${item.name} ${year}`));
      const input=document.createElement('input');input.type='date';input.dataset.holidayDate=item.key;input.dataset.year=year;input.value=edited||calculated[0];
      input.min=`${year-1}-12-01`;input.max=`${year+1}-01-31`;input.setAttribute('aria-label',`${item.name} ${year} date`);
      const status=document.createElement('small');status.textContent=edited?'Edited':'Calculated';
      const reset=makeButton('Use calculated',()=>{const dates=countdownSettings.holiday_dates[item.key];if(dates){delete dates[String(year)];if(!Object.keys(dates).length)delete countdownSettings.holiday_dates[item.key]}countdownChanged();fill();for(const grid of document.querySelectorAll('[data-calendar-root]'))renderCalendarGrid(grid)});
      reset.dataset.holidayReset=item.key;reset.dataset.year=year;reset.disabled=!edited;
      input.addEventListener('change',()=>{
        if(!input.value||input.value===calculated[0]){reset.click();return}
        (countdownSettings.holiday_dates[item.key]??={})[String(year)]=input.value;countdownChanged();status.textContent='Edited';reset.disabled=false;
        for(const grid of document.querySelectorAll('[data-calendar-root]'))renderCalendarGrid(grid);
      });
      line.append(show,name,input,status,reset);table.append(line);
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
    timeZone:countdownSettings.timezone,year:'numeric',month:'2-digit',day:'2-digit'
  }).formatToParts(new Date(value)).map(part=>[part.type,part.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}
function renderCountdownEditor(container) {
  container.replaceChildren();
  const note=document.createElement('p');
  note.textContent='Your calendar: birthdays, holidays and other days. The display counts down to the next one. Switch on holiday packs below, or add your own events. Save applies changes.';
  container.append(note);
  const calendar=document.createElement('div');calendar.className='calendar';calendar.dataset.calendarRoot='';container.append(calendar);renderCalendarGrid(calendar);
  const quick=document.createElement('div');quick.className='editor-toolbar';
  const birthday=makeButton('Add my birthday',()=>{const t=calendarTodayParts();calendarAddEvent({title:'My birthday',icon:'birthday',annual:true,all_day:true,start:`${t.year}-${String(t.month).padStart(2,'0')}-${String(t.day).padStart(2,'0')}`})});
  birthday.dataset.calendarAddBirthday='';quick.append(birthday);container.append(quick);
  editorParagraph(container,'Birthdays (cake icon) show confetti on the day.').className='editor-note';
  renderHolidayPacks(container);
  const details=document.createElement('details');details.className='editor-group';const summary=document.createElement('summary');summary.textContent='How dates and the progress bar work';
  const detailText=document.createElement('p');detailText.textContent='Dates use the time zone below (UTC by default). For all-day events, the end date is the day after the last day. If a clock change makes a time happen twice, the first one is used; times skipped by a clock change are rejected. Long titles scroll once, then stay still. The progress bar fills over the 30 days before the event (full more than 30 days out, empty at NOW). Google Calendar is not connected.';
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
  const layout=control(fields,countdownSettings,'layout','Style','text',['large','icon','progress'],'setting');
  for(const [value,text] of [['large','Big countdown'],['icon','Icon and countdown'],['progress','Countdown with progress bar']])layout.querySelector(`option[value="${value}"]`).textContent=text;
  for(const [key,text] of [['background','Background colour'],['text_colour','Text colour'],['accent','Highlight colour']])control(fields,countdownSettings,key,text,'color',null,'setting');
  editorAdvanced(field(control(fields,countdownSettings,'timezone','Time zone','text',null,'setting')));
  editorAdvanced(field(control(fields,countdownSettings,'title_font','Title font','text',installedFonts,'setting')));
  editorAdvanced(field(control(fields,countdownSettings,'countdown_font','Countdown font','text',['auto',...installedFonts],'setting')));
  const bar=control(fields,countdownSettings,'progress_height','Progress bar thickness (1–4 pixels)','number',null,'setting');bar.min=1;bar.max=4;editorAdvanced(field(bar));
  for(const edge of ['top','right','bottom','left']){const input=control(fields,countdownSettings.insets,edge,'Space from '+edge+' edge (0–8 pixels)','number',null,'setting');input.min=0;input.max=8;editorAdvanced(field(input));}
  const warning=document.createElement('p');warning.dataset.countdownWarning='';warning.setAttribute('role','status');container.append(warning);renderCountdownWarning();
  const add=makeButton('Add event',()=>{
    let parts;
    try{parts=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:countdownSettings.timezone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date()).map(part=>[part.type,part.value]));}
    catch(_){setMessage('Enter a valid Matrix event timezone before adding an event.',true);return;}
    const start=`${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
    const bytes=crypto.getRandomValues(new Uint8Array(12));
    const id='event_'+Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');
    countdownSettings.events.push({id,title:'New event',start,end:null,all_day:false,annual:false,enabled:true,now_minutes:15,accent:null,icon:'calendar'});
    countdownChanged();renderCountdownEditors();
  });add.dataset.countdownAdd='';container.append(add);
  countdownSettings.events.forEach((event,index)=>{
    const card=document.createElement('fieldset');card.className='countdown-event editor-fields';card.dataset.countdownId=event.id;
    const legend=document.createElement('legend');legend.textContent=(event.title||'Event '+(index+1));card.append(legend);
    control(card,event,'title','Event name');
    const start=control(card,event,'start','Date (and time, e.g. 2026-12-25 or 2026-12-25T18:00)');start.placeholder='YYYY-MM-DD';
    const icon=control(card,event,'icon','Icon','text',Object.keys(countdownIconNames));
    for(const option of icon.options)option.textContent=countdownIconNames[option.value];
    let iconPreview=countdownIcon(event.icon,2);icon.closest('label').append(iconPreview);
    icon.addEventListener('change',()=>{const next=countdownIcon(icon.value,2);iconPreview.replaceWith(next);iconPreview=next});
    control(card,event,'enabled','Show this event','checkbox');
    control(card,event,'all_day','All day','checkbox');
    control(card,event,'annual','Every year','checkbox');
    editorAdvanced(field(control(card,event,'end','End date (optional; the day after it finishes)')));
    const minutes=control(card,event,'now_minutes','Show “NOW” for (minutes)','number');minutes.min=1;minutes.max=10080;editorAdvanced(field(minutes));
    const accent=control(card,event,'accent','Own highlight colour (e.g. #FF8800; blank uses the shared one)');accent.placeholder='Shared colour';editorAdvanced(field(accent));
    const artwork=control(card,event,'artwork','Picture from media library (icon style; up to 8×8)','text',['',...basicMediaChoices()]);editorAdvanced(field(artwork));
    artwork.options[0].textContent='Use the icon above';
    if(event.artwork&&!basicMediaChoices().includes(event.artwork)){
      const missing=document.createElement('option');missing.value=event.artwork;missing.textContent=event.artwork+' (missing)';artwork.append(missing);artwork.value=event.artwork;
    }
    const remove=makeButton('Delete event',()=>{countdownSettings.events.splice(index,1);countdownChanged();renderCountdownEditors()},'btn danger');remove.dataset.countdownDelete='';card.append(remove);
    const up=makeButton('Move event up',()=>{if(index){const events=countdownSettings.events;[events[index-1],events[index]]=[events[index],events[index-1]];countdownChanged();renderCountdownEditors()}});up.disabled=index===0;card.append(up);editorAdvanced(up);
    container.append(card);
  });
  editorAdvancedToggle(container,'event_countdown');
}
