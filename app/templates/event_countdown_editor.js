let countdownSettings = {{ config.event_countdown|tojson }};
let countdownPreviewInfo = null;
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
  note.textContent='Count down to birthdays, holidays and other days. Add an event, pick its date, and press Preview to see it. Save applies changes.';
  container.append(note);
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
    control(card,event,'icon','Icon','text',['calendar','birthday','anniversary','launch','holiday','opening']);
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
