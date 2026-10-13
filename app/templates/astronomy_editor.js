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
  // A blank astronomy timezone follows Weather's, so a Weather zone change matters for either source.
  return JSON.stringify([astronomySettings.location_source==='weather'?[weather.latitude,weather.longitude,weather.location]:null,astronomySettings.timezone?null:weather.timezone]);
}
let astronomyLastWeatherDependency;
function astronomyWeatherChanged() {
  const dependency=astronomyWeatherDependency();
  if(dependency===astronomyLastWeatherDependency)return;
  astronomyLastWeatherDependency=dependency;
  astronomyFollowWeatherOption(document.querySelector('[data-astronomy-setting="timezone"]'));
  astronomyChanged();
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
      if(result.status==='location_required')note.textContent+=' No timezone known: choose the Weather location from search again, or pick a timezone here.';
      if(result.status!=='preparing')return;
      await new Promise(resolve=>setTimeout(resolve,100));
      if(request.signal.aborted||generation!==astronomyTimingGeneration)return;
    }
    note.textContent='Preparation still pending; retry Calculate.';
  } catch(error) {if(generation===astronomyTimingGeneration&&error.name!=='AbortError')note.textContent=error.message;}
}
// The blank timezone option means "same as Weather"; label it with Weather's current zone.
function astronomyFollowWeatherOption(select) {
  if(!select)return;
  // Read the Weather field directly: editors render before configBody() is ready.
  const zone=document.querySelector('#weather-timezone')?.value||'';
  select.options[0].textContent=zone?`Same as Weather (${zone})`:'Same as Weather (unknown: choose Weather location again)';
}
function renderAstronomyEditor(container) {
  editorCloseHelp(container);container.replaceChildren();
  const note=document.createElement('p');note.dataset.astronomyNote='';
  note.textContent='Shows tonight’s Moon and, if you like, the next new and full Moon. Works offline. Eclipses and other sky events are not implemented yet. Press Preview to see it; Save applies changes.';
  container.append(note);
  const content=editorSection(container,'Content'),location=editorSection(container,'Location'),playback=editorSection(container,'Playback'),appearance=editorSection(container,'Appearance',true),actions=editorSection(container,'Status and actions');
  editorAdvanced(location.parentElement,playback.parentElement);
  let fields=document.createElement('div');fields.className='editor-fields';content.append(fields);
  const switchFields=parent=>{fields=document.createElement('div');fields.className='editor-fields';parent.append(fields)};
  function control(key,label,type='text',options=null,min=null,max=null) {
    const wrapper=document.createElement('label');wrapper.textContent=label;
    const input=key==='timezone'?editorTimezoneSelect(astronomySettings[key]):document.createElement(options?'select':'input');input.dataset.astronomySetting=key;
    if(options)for(const value of options){const option=document.createElement('option');option.value=value;option.textContent=value;input.append(option)}
    else if(key!=='timezone')input.type=type;
    // 'view' is stored as 'simple'/'advanced' but edited as one checkbox.
    if(key==='view')input.checked=astronomySettings.view==='advanced';else if(type==='checkbox')input.checked=astronomySettings[key];else input.value=astronomySettings[key];
    if(min!==null)input.min=min;if(max!==null)input.max=max;if(type==='number')input.step=key.includes('days')||key==='event_limit'?'1':'any';
    input.addEventListener(type==='checkbox'||options||key==='timezone'?'change':'input',()=>{
      astronomySettings[key]=key==='view'?(input.checked?'advanced':'simple'):type==='checkbox'?input.checked:type==='number'?Number(input.value):input.value;
      astronomyChanged();
      if(key==='location_source')updateLocationFields();
    });
    if(type==='checkbox')wrapper.classList.add('editor-check');
    wrapper.append(input);fields.append(wrapper);return input;
  }
  control('current_moon','Show tonight’s Moon','checkbox');
  control('view','Show Moon details (% lit, rise/set, next phase)','checkbox');
  control('lunar_events','Show next new and full Moon','checkbox');
  switchFields(content);editorAdvanced(fields);
  control('horizon_days','Look ahead (days, including today)','number',null,1,366);
  control('event_limit','Most new/full Moon cards to show','number',null,1,50);
  switchFields(location);
  control('location_source','Location','text',['weather','override']);
  const source=fields.querySelector('[data-astronomy-setting="location_source"]');source.options[0].textContent='Same as Weather';source.options[1].textContent='A different place';
  control('timezone','Time zone');
  astronomyFollowWeatherOption(fields.querySelector('[data-astronomy-setting="timezone"]'));
  const locationNote=editorParagraph(location,'');
  switchFields(location);const overrides=fields;
  control('location_label','Place name');
  control('latitude','Latitude','number',null,-90,90);control('longitude','Longitude','number',null,-180,180);
  function updateLocationFields(){overrides.hidden=astronomySettings.location_source!=='override';locationNote.textContent=overrides.hidden?'Uses the coordinates selected in Weather. Timezone follows the Weather location unless you pick one.':'Enter decimal coordinates for this module only. Weather is unchanged. Timezone follows the Weather location unless you pick one.'}
  updateLocationFields();switchFields(playback);
  control('moon_hold_seconds','Show the Moon for (seconds)','number',null,1,300);control('event_hold_seconds','Show each new/full Moon card for (seconds)','number',null,1,300);
  control('frame_delay','Scroll step time (seconds; smaller is faster)','number',null,.01,1);
  editorParagraph(playback,'Total time on screen also includes any text that has to scroll.');
  switchFields(appearance);
  control('title_font','Title font','text',installedFonts);control('detail_font','Detail font','text',installedFonts);
  control('countdown_font','Countdown font','text',['auto',...installedFonts]);
  for(const key of ['title_font','detail_font','countdown_font'])editorAdvanced(fields.querySelector(`[data-astronomy-setting="${key}"]`).closest('label'));
  // Separate colours per text role (owner); text_colour remains as the legacy base value.
  for(const [key,label] of [['title_colour','Title colour'],['date_colour','Date colour'],['description_colour','Description colour'],['background','Background colour'],['moon_colour','Moon colour'],['accent','Event icon colour']])control(key,label,'color');
  editorParagraph(appearance,'Title: phase name or event name. Date: event date, next phase and countdown. Description: illumination, rise/set and visibility window.');
  const calculate=makeButton('Work out time on screen',()=>calculateAstronomy(container));calculate.dataset.astronomyCalculate='';actions.append(makeButton('Preview my changes',()=>editorDraftPreview('astronomy')),calculate);
  const timing=document.createElement('p');timing.dataset.astronomyTiming='';timing.setAttribute('role','status');timing.textContent='Draft not calculated. Select a timezone and content, then Calculate. All content disabled safely yields to the next module.';actions.append(timing);
  const details=document.createElement('details'),summary=document.createElement('summary'),text=document.createElement('p');
  summary.textContent='How the Moon is calculated';editorAdvanced(details);
  text.textContent='Eclipses, supermoon/blue-moon labels, meteor showers and planetary events are not implemented. No provider is connected. Phase time is the global instant; countdown targets the next local observing window, not a phase duration. Moon picture is a pixel-art schematic drawn in Aseprite (waxing lit on the right), not sky-oriented. Place search/map remains unavailable in this local slice. NASA/JPL DE440s, Skyfield 1.54, checksum verified in background before use; supported dates 2000–2100. Bundled time tables; future UTC/UT1 uncertainty. Moon >10° and Sun <−18° for at least 10 continuous minutes. No terrain or weather visibility model. No event catalogue or updater installed. Production static-data/dependency delivery remains blocked.';
  details.append(summary,text);actions.append(details);
  editorAdvancedToggle(container,'astronomy');
}
window.addEventListener('beforeunload',()=>astronomyTimingRequest?.abort(),{once:true});
