// Implemented local cards only. Native Save and preview serialize this same draft.
let astronomySettings = {{ config.astronomy|tojson }};
let astronomyTiming = null, astronomyTimingGeneration = 0, astronomyTimingRequest = null;
const animateWithoutAstronomy = animateEditPreview;
animateEditPreview = function(generation, controller, complete) {
  if(previewModule==='astronomy'&&editPreviewCurrent(generation)&&complete&&!editPreviewFrames.length&&!editPreviewAhead) {
    editPreviewSession=null;editPreviewToken=null;editPreviewController=null;
    document.querySelector('#preview-frame').textContent='64 × 32 · Unsaved draft complete';
    return;
  }
  return animateWithoutAstronomy(generation,controller,complete);
};
const configBodyWithoutAstronomy = configBody;
configBody = function(previewMedia=false) {
  return {...configBodyWithoutAstronomy(previewMedia), astronomy:structuredClone(astronomySettings)};
};
const syncDurationsWithoutAstronomy = syncPlaylistDurations;
syncPlaylistDurations = function() {
  syncDurationsWithoutAstronomy();
  for(const row of document.querySelectorAll('.module-row[data-module="astronomy"]')) {
    const input=row.querySelector('.duration');input.hidden=true;input.readOnly=true;
    input.closest('label').childNodes[0].textContent='Prepared card duration ';
    row.querySelector('.physical-time').textContent=astronomyTiming?`${astronomyTiming.status} · ${astronomyTiming.duration_seconds.toFixed(2)} s`:'Not calculated · use Astronomy editor';
  }
};
{
  const marker=document.createElement('div');marker.dataset.moduleEditor='astronomy';
  document.querySelector('#built-in-editor-templates').content.append(marker);
  for(const row of document.querySelectorAll('.module-row[data-module="astronomy"]')) {
    const editor=marker.cloneNode();editor.className='module-editor';editor.hidden=true;
    row.closest('.module-item').append(editor);
  }
}
function astronomyChanged() {
  astronomyTiming=null;astronomyTimingGeneration++;astronomyTimingRequest?.abort();
  for(const note of document.querySelectorAll('[data-astronomy-timing]'))note.textContent='Draft changed; calculate prepared duration again.';
  syncPlaylistDurations();markMatrixDirty();
  globalThis.publishDeviceDraftMutation?.('astronomy',150);
  if(previewMode==='edit'&&previewModule==='astronomy')scheduleEditPreview(150);
}
function astronomyWeatherDependency() {
  const weather=configBody().weather;
  return astronomySettings.location_source==='weather'?JSON.stringify([weather.latitude,weather.longitude,weather.location]):'';
}
let astronomyLastWeatherDependency;
function astronomyWeatherChanged() {
  const dependency=astronomyWeatherDependency();
  if(dependency===astronomyLastWeatherDependency)return;
  astronomyLastWeatherDependency=dependency;
  if(astronomySettings.location_source==='weather')astronomyChanged();
}
const resetBuiltInWithoutAstronomy = resetBuiltInModule;
resetBuiltInModule = function(module) {
  resetBuiltInWithoutAstronomy(module);
  if(module==='weather')astronomyWeatherChanged();
};
async function calculateAstronomy(container) {
  const generation=++astronomyTimingGeneration;astronomyTimingRequest?.abort();
  const request=new AbortController();astronomyTimingRequest=request;
  const dependency=astronomyWeatherDependency();
  astronomyLastWeatherDependency=dependency;
  const payload={base_revision:matrixLoadedRevision,settings:structuredClone(astronomySettings),
    weather:structuredClone(configBody().weather),preview_instance:previewInstance};
  const note=container.querySelector('[data-astronomy-timing]');
  note.textContent='Preparing offline cards…';
  try {
    for(let attempt=0;attempt<300;attempt++) {
      const result=await api('/api/scroll-timing/astronomy',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(payload),signal:request.signal});
      if(generation!==astronomyTimingGeneration||dependency!==astronomyWeatherDependency())return;
      astronomyTiming=result;syncPlaylistDurations();
      note.textContent=`${result.status} · ${result.ready_count} cards · ${result.duration_seconds.toFixed(2)} s. ${result.error||''}`;
      if(result.clipping?.some(card=>card.some(Boolean)))note.textContent+=' Font clipping: choose a smaller installed font.';
      if(result.status==='location_required')note.textContent+=' Enter an explicit IANA timezone; none is inferred from weather.';
      if(result.status!=='preparing')return;
      await new Promise(resolve=>setTimeout(resolve,100));
      if(request.signal.aborted||generation!==astronomyTimingGeneration)return;
    }
    note.textContent='Preparation still pending; retry Calculate.';
  } catch(error) {if(generation===astronomyTimingGeneration&&error.name!=='AbortError')note.textContent=error.message;}
}
function renderAstronomyEditor(container) {
  container.replaceChildren();
  const note=document.createElement('p');note.dataset.astronomyNote='';
  note.textContent='Offline current Moon and locally observable new/full moons only. Eclipses, supermoon/blue-moon labels, meteor showers and planetary events are not implemented. No provider is connected. Phase time is the global instant; countdown targets the next local observing window, not a phase duration. Moon icon is schematic, not sky-oriented. Weather coordinates have no timezone: enter it explicitly. Place search/map remains unavailable in this local slice. Blank timezone safely skips playback.';
  container.append(note);
  const fields=document.createElement('div');fields.className='editor-fields';container.append(fields);
  function control(key,label,type='text',options=null,min=null,max=null) {
    const wrapper=document.createElement('label');wrapper.textContent=label;
    const input=document.createElement(options?'select':'input');input.dataset.astronomySetting=key;
    if(options)for(const value of options){const option=document.createElement('option');option.value=value;option.textContent=value;input.append(option)}
    else input.type=type;
    if(type==='checkbox')input.checked=astronomySettings[key];else input.value=astronomySettings[key];
    if(min!==null)input.min=min;if(max!==null)input.max=max;if(type==='number')input.step=key.includes('days')||key==='event_limit'?'1':'any';
    input.addEventListener(type==='checkbox'||options?'change':'input',()=>{
      astronomySettings[key]=type==='checkbox'?input.checked:type==='number'?Number(input.value):input.value;
      astronomyChanged();
    });
    wrapper.append(input);fields.append(wrapper);
  }
  control('current_moon','Current Moon','checkbox');control('lunar_events','Local new/full Moon events','checkbox');
  control('location_source','Coordinate source','text',['weather','override']);
  control('timezone','Explicit IANA timezone (for either source)');
  control('location_label','Override place label');
  control('latitude','Override latitude','number',null,-90,90);control('longitude','Override longitude','number',null,-180,180);
  control('horizon_days','Horizon (local dates including today)','number',null,1,366);
  control('event_limit','Event cards per pass','number',null,1,50);
  control('moon_hold_seconds','Moon hold seconds','number',null,1,300);control('event_hold_seconds','Event hold seconds','number',null,1,300);
  control('frame_delay','Scroll frame delay seconds','number',null,.01,1);
  control('title_font','Title font','text',installedFonts);control('detail_font','Detail font','text',installedFonts);
  control('countdown_font','Countdown font','text',['auto',...installedFonts]);
  for(const key of ['background','text_colour','moon_colour','accent'])control(key,key.replaceAll('_',' '),'color');
  const calculate=makeButton('Calculate prepared duration',()=>calculateAstronomy(container));calculate.dataset.astronomyCalculate='';container.append(calculate);
  const timing=document.createElement('p');timing.dataset.astronomyTiming='';timing.setAttribute('role','status');timing.textContent='Not calculated. All content disabled safely yields to the next module.';container.append(timing);
  const details=document.createElement('details'),summary=document.createElement('summary'),text=document.createElement('p');
  summary.textContent='Offline source and limitations';
  text.textContent='NASA/JPL DE440s, Skyfield 1.54, checksum verified in background before use; supported dates 2000–2100. Bundled time tables; future UTC/UT1 uncertainty. Moon >10° and Sun <−18° for at least 10 continuous minutes. No terrain or weather visibility model. No event catalogue or updater installed. Production static-data/dependency delivery remains blocked.';
  details.append(summary,text);container.append(details);
}
window.addEventListener('beforeunload',()=>astronomyTimingRequest?.abort(),{once:true});
