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
  for(const warning of document.querySelectorAll('[data-countdown-warning]'))warning.textContent=info?messages.join(' '):'Run Edit Preview to measure this draft’s title and countdown layout.';
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
  note.textContent='Local events work without an account. Dates use the Matrix timezone below (UTC by default). All-day end dates are exclusive. Ambiguous local times use the first occurrence; nonexistent DST times are rejected. Long titles scroll one measured pass with a 4 px gap, then restore and freeze. Progress shows a fixed 30-day window before the event (full beyond 30 days, empty at NOW), unchanged by playlist restarts. Google Calendar is not connected in this local-event version.';
  container.append(note);
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
    wrapper.append(input);parent.append(wrapper);return input;
  }
  control(fields,countdownSettings,'timezone','Matrix event timezone (IANA)','text',null,'setting');
  control(fields,countdownSettings,'layout','Layout','text',['large','icon','progress'],'setting');
  control(fields,countdownSettings,'title_font','Title font','text',installedFonts,'setting');
  control(fields,countdownSettings,'countdown_font','Countdown font','text',['auto',...installedFonts],'setting');
  for(const key of ['background','text_colour','accent'])control(fields,countdownSettings,key,key.replace('_',' '),'color',null,'setting');
  const bar=control(fields,countdownSettings,'progress_height','Progress height (1–4 px)','number',null,'setting');bar.min=1;bar.max=4;
  for(const edge of ['top','right','bottom','left']){const input=control(fields,countdownSettings.insets,edge,edge+' safe inset (0–8 px)','number',null,'setting');input.min=0;input.max=8;}
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
    const legend=document.createElement('legend');legend.textContent='Local event '+(index+1);card.append(legend);
    control(card,event,'enabled','Enabled','checkbox');
    control(card,event,'title','Event title');
    control(card,event,'start','Start (YYYY-MM-DD or YYYY-MM-DDTHH:MM)');
    control(card,event,'end','End (optional; exclusive)');
    control(card,event,'all_day','All day (date only)','checkbox');
    control(card,event,'annual','Repeat annually','checkbox');
    const minutes=control(card,event,'now_minutes','NOW duration (minutes)','number');minutes.min=1;minutes.max=10080;
    control(card,event,'accent','Accent override (#RRGGBB, blank = shared)');
    control(card,event,'icon','Bundled icon','text',['calendar','birthday','anniversary','launch','holiday','opening']);
    const artwork=control(card,event,'artwork','Library artwork (icon layout; first frame, up to 8 × 8 px)','text',['',...basicMediaChoices()]);
    artwork.options[0].textContent='Bundled type icon (no artwork)';
    if(event.artwork&&!basicMediaChoices().includes(event.artwork)){
      const missing=document.createElement('option');missing.value=event.artwork;missing.textContent=event.artwork+' (missing)';artwork.append(missing);artwork.value=event.artwork;
    }
    const remove=makeButton('Delete event',()=>{countdownSettings.events.splice(index,1);countdownChanged();renderCountdownEditors()},'btn danger');remove.dataset.countdownDelete='';card.append(remove);
    const up=makeButton('Move event up',()=>{if(index){const events=countdownSettings.events;[events[index-1],events[index]]=[events[index],events[index-1]];countdownChanged();renderCountdownEditors()}});up.disabled=index===0;card.append(up);
    container.append(card);
  });
}
