// Implemented local cards only. Native Save and preview serialize this same draft.
let astronomySettings = initialAstronomySettings;
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
    // Same as the other automatic modules: visible, read-only box holding the calculated seconds.
    const input=row.querySelector('.duration');input.hidden=false;input.readOnly=true;
    input.title='Runtime is calculated from the prepared Moon cards, hold times, and scroll speed';
    if(astronomyTiming?.status==='ready'){const value=Math.min(3600,Math.max(.1,astronomyTiming.duration_seconds)).toFixed(2);if(input.value!==value)input.value=value}
    row.querySelector('.physical-time').textContent='Physical time · '+astronomyTimingText();
  }
};
// Same as the other modules: worked out automatically and shown as "N.N s".
function astronomyTimingText() {
  // Not in use: show the final text from the first render so the row never shifts after load.
  if(!astronomyTiming)return astronomyInUse()?'Calculating…':'Worked out when switched on';
  if(astronomyTiming.status==='ready')return oneDecimalSeconds(Math.round(astronomyTiming.duration_seconds*1000));
  if(astronomyTiming.status==='preparing')return 'Calculating…';
  if(astronomyTiming.status==='idle')return 'Worked out when switched on';
  if(['disabled','empty'].includes(astronomyTiming.status))return 'Skipped (nothing to show)';
  return 'Unavailable';
}
function astronomyRenderTiming(detail='') {
  for(const note of document.querySelectorAll('[data-astronomy-timing]'))note.textContent=astronomyTimingText();
  for(const note of document.querySelectorAll('[data-astronomy-timing-detail]'))note.textContent=detail;
  syncPlaylistDurations();
}
let astronomyTimingDebounce=null;
function astronomyInUse() {
  const row=document.querySelector('.module-row[data-module="astronomy"]'),editor=document.querySelector('.module-editor[data-module-editor="astronomy"]');
  return !!(row?.querySelector('input.enabled')?.checked||(editor&&!editor.hidden));
}
function scheduleAstronomyTiming(delay=300) {
  clearTimeout(astronomyTimingDebounce);astronomyTimingDebounce=setTimeout(calculateAstronomy,delay);
}
{
  const marker=document.createElement('div');marker.dataset.moduleEditor='astronomy';
  document.querySelector('#built-in-editor-templates').content.append(marker);
  for(const row of document.querySelectorAll('.module-row[data-module="astronomy"]')) {
    const editor=marker.cloneNode();editor.className='module-editor';editor.hidden=true;
    row.closest('.module-item').append(editor);
    // Work the time out as soon as Astronomy is switched on or its editor opens.
    row.querySelector('input.enabled')?.addEventListener('change',()=>scheduleAstronomyTiming(0));
    new MutationObserver(()=>{if(!editor.hidden&&!astronomyTiming?.duration_seconds)scheduleAstronomyTiming(0)}).observe(editor,{attributes:true,attributeFilter:['hidden']});
  }
}
function astronomyChanged() {
  astronomyTiming=null;astronomyTimingGeneration++;astronomyTimingRequest?.abort();
  astronomyRenderTiming();markMatrixDirty();scheduleAstronomyTiming();
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
  editorFollowWeatherLabel(document.querySelector('[data-countdown-setting="timezone"]'));
  astronomyChanged();
}
const resetBuiltInWithoutAstronomy = resetBuiltInModule;
resetBuiltInModule = function(module) {
  resetBuiltInWithoutAstronomy(module);
  if(module==='weather')astronomyWeatherChanged();
};
async function calculateAstronomy() {
  if(!matrixEditorsReady){scheduleAstronomyTiming();return}
  // Like the other playlist modules: only work it out when Astronomy is in use.
  // Not in use: idle, and the row already says so, so don't re-sync the playlist (it disturbs Splash/Sleep pickers).
  if(!astronomyInUse()){const shown=astronomyTiming===null||astronomyTiming.status==='idle';astronomyTiming={status:'idle'};if(!shown)astronomyRenderTiming();return}
  const generation=++astronomyTimingGeneration;astronomyTimingRequest?.abort();
  const request=new AbortController();astronomyTimingRequest=request;
  const dependency=astronomyWeatherDependency();
  astronomyLastWeatherDependency=dependency;
  const payload={base_revision:matrixLoadedRevision,settings:structuredClone(astronomySettings),
    weather:structuredClone(configBody().weather),preview_instance:previewInstance};
  const current=()=>!request.signal.aborted&&generation===astronomyTimingGeneration&&dependency===astronomyWeatherDependency();
  astronomyTiming=null;astronomyRenderTiming();
  try {
    for(let attempt=0;attempt<300;attempt++) {
      let result;
      try {
        result=await api('/api/scroll-timing/astronomy',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify(payload),signal:request.signal});
      } catch(error) {
        if(error.status!==429)throw error;
        await new Promise(resolve=>setTimeout(resolve,1000));if(!current())return;continue;
      }
      if(!current())return;
      astronomyTiming=result;
      let detail=result.error||'';
      if(result.clipping?.some(card=>card.some(Boolean)))detail+=' Font clipping: choose a smaller installed font.';
      if(result.status==='location_required')detail+=' No timezone known: choose the Weather location from search again, or pick a timezone here.';
      if(['disabled','empty'].includes(result.status))detail+=' Nothing to show, so the display skips to the next module.';
      astronomyRenderTiming(detail.trim());
      if(result.status!=='preparing')return;
      await new Promise(resolve=>setTimeout(resolve,100));
      if(!current())return;
    }
    astronomyTiming={status:'error'};astronomyRenderTiming('Preparation is taking too long; change a setting to try again.');
  } catch(error) {if(generation===astronomyTimingGeneration&&error.name!=='AbortError'){astronomyTiming={status:'error'};astronomyRenderTiming(error.message)}}
}
// The blank timezone option means "same as Weather"; label it with Weather's current zone.
function astronomyFollowWeatherOption(select) {
  // Read the Weather field directly: editors render before configBody() is ready.
  editorFollowWeatherLabel(select);
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
  actions.append(makeButton('Preview my changes',()=>editorDraftPreview('astronomy')));
  // Same readout as the other modules' calculated runtime (e.g. Weather's "15.5 s").
  const timingRow=document.createElement('p'),timingLabel=document.createElement('span'),timing=document.createElement('strong'),timingDetail=document.createElement('p');
  timingLabel.textContent='Time on screen: ';timing.dataset.astronomyTiming='';timing.setAttribute('role','status');timing.textContent=astronomyTimingText();
  timingDetail.dataset.astronomyTimingDetail='';timingDetail.className='editor-note';
  timingRow.append(timingLabel,timing);actions.append(timingRow,timingDetail);
  const details=document.createElement('details'),summary=document.createElement('summary'),text=document.createElement('p');
  summary.textContent='How the Moon is calculated';editorAdvanced(details);
  text.textContent='Eclipses, supermoon/blue-moon labels, meteor showers and planetary events are not implemented. No provider is connected. Phase time is the global instant; countdown targets the next local observing window, not a phase duration. Moon picture is a pixel-art schematic drawn in Aseprite (waxing lit on the right), not sky-oriented. Place search/map remains unavailable in this local slice. NASA/JPL DE440s, Skyfield 1.54, checksum verified in background before use; supported dates 2000–2100. Bundled time tables; future UTC/UT1 uncertainty. Moon >10° and Sun <−18° for at least 10 continuous minutes. No terrain or weather visibility model. No event catalogue or updater installed. Production static-data/dependency delivery remains blocked.';
  details.append(summary,text);actions.append(details);
  editorAdvancedToggle(container,'astronomy');
}
window.addEventListener('beforeunload',()=>astronomyTimingRequest?.abort(),{once:true});
