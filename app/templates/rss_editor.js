// RSS shares the dashboard's serializer, revision gate and Save lifecycle.
let rssSettings = initialRssSettings;
const rssDefaultFeeds = {{ rss_default_feeds|tojson }};
let rssCatalogue=null, rssStatus=null, rssDuration=null, rssTimingGeneration=0, rssTimingController=null;
const configBodyWithoutRSS=configBody;
configBody=function(previewMedia=false){return {...configBodyWithoutRSS(previewMedia),rss:structuredClone(rssSettings)}};
{
  const marker=document.createElement('div');marker.dataset.moduleEditor='rss';
  document.querySelector('#built-in-editor-templates').content.append(marker);
  for(const row of document.querySelectorAll('.module-row[data-module="rss"]')){
    const editor=marker.cloneNode();editor.className='module-editor';editor.hidden=true;row.closest('.module-item').append(editor);
  }
}
function rssChanged(){
  rssDuration=null;rssTimingGeneration++;rssTimingController?.abort();rssTimingController=null;rssRenderDuration();
  markMatrixDirty();globalThis.publishDeviceDraftMutation?.('rss',150);
  if(previewMode==='edit'&&previewModule==='rss')scheduleEditPreview(150);
}
function rssRenderDuration(){
  const text=rssDuration?`${rssDuration.duration_seconds.toFixed(2)} seconds · ${rssDuration.ready_count} prepared stories`:'Duration unavailable until stories are prepared. Test feeds or check filters.';
  for(const output of document.querySelectorAll('[data-rss-duration]'))output.textContent=text;
  for(const row of document.querySelectorAll('.module-row[data-module="rss"]')){
    const input=row.querySelector('.duration');input.readOnly=true;input.title='RSS runtime is derived from prepared stories, not this compatibility duration.';
    const output=row.querySelector('.physical-time');if(output)output.textContent=text;
  }
}
const syncPlaylistDurationsWithoutRSS=syncPlaylistDurations;
syncPlaylistDurations=function(){syncPlaylistDurationsWithoutRSS();rssRenderDuration()};
async function rssPrepare(settings,signal,isCurrent=()=>true){
  const body=JSON.stringify({base_revision:matrixLoadedRevision,settings});
  for(let attempt=0;attempt<24;attempt++){
    if(signal.aborted||!isCurrent())throw new DOMException('Superseded','AbortError');
    const result=await api('/api/scroll-timing/rss',{method:'POST',headers:{'Content-Type':'application/json'},body,signal});
    if(signal.aborted||!isCurrent())throw new DOMException('Superseded','AbortError');
    if(result.ready_count>0){rssDuration=result;rssRenderDuration();return result}
    await new Promise((resolve,reject)=>{
      const stop=()=>{clearTimeout(timer);reject(new DOMException('Superseded','AbortError'))};
      const timer=setTimeout(()=>{signal.removeEventListener('abort',stop);resolve()},250);
      signal.addEventListener('abort',stop,{once:true});
    });
  }
  throw new Error('No prepared eligible stories. Test feeds or check filters; empty feeds will skip.');
}
async function rssCalculate(){
  rssTimingController?.abort();const controller=new AbortController(),generation=++rssTimingGeneration;rssTimingController=controller;
  try{await rssPrepare(structuredClone(rssSettings),controller.signal,()=>generation===rssTimingGeneration)}
  catch(error){if(error.name!=='AbortError')for(const output of document.querySelectorAll('[data-rss-duration]'))output.textContent=error.message}
  finally{if(rssTimingController===controller)rssTimingController=null}
}
function rssFillPresets(select){
  const selected=select.value;select.replaceChildren();
  // Owner 2026-10-14: verified feeds are offered here (grouped by feed.group), not pre-added.
  const groups=new Map();
  for(const feed of rssCatalogue?.feeds||[]){
    if(!groups.has(feed.group)){const group=document.createElement('optgroup');group.label=feed.group||'Feeds';groups.set(feed.group,group);select.append(group)}
    const option=document.createElement('option');option.value=feed.url;option.textContent=feed.name;groups.get(feed.group).append(option);
  }
  if([...select.options].some(option=>option.value===selected))select.value=selected;
}
function rssRenderStatus(){
  for(const target of document.querySelectorAll('[data-rss-status]')){
    target.replaceChildren();
    const note=document.createElement('p');note.textContent=rssStatus?.explanation||'Saved feed status (refresh every 30 minutes).';target.append(note);
    for(const feed of rssStatus?.feeds||[]){
      const p=document.createElement('p'),name=feed.title||feed.url,time=feed.last_success?new Date(feed.last_success*1000).toLocaleString():'unknown';
      p.textContent=feed.status==='stale'?`Could not update ${name}; showing cache from ${time}`:feed.status==='expired'?`${name}: Cache expired; module will skip`: `${name}: ${feed.status} · Last refreshed: ${time}`;
      target.append(p);
      if(feed.warning){const details=document.createElement('details'),summary=document.createElement('summary'),text=document.createElement('p');summary.textContent='Details';text.textContent=feed.warning;details.append(summary,text);target.append(details)}
    }
  }
}
async function rssRefreshStatus(){
  try{rssStatus=await api('/api/rss/status');rssCatalogue=rssStatus.presets;for(const select of document.querySelectorAll('[data-rss-presets]'))rssFillPresets(select);rssRenderStatus()}
  catch(error){for(const target of document.querySelectorAll('[data-rss-status]'))target.textContent='Feed status unavailable: '+error.message}
}
function renderRSSEditors(){for(const editor of document.querySelectorAll('.module-editor[data-module-editor="rss"]'))renderRSSEditor(editor)}
const rssHelpText={
  count:'Maximum stories in one module pass, not per feed. Eligible stories rotate across feeds; fewer may appear if filters or empty feeds leave too few stories. Previously seen stories rotate back after the unseen set is exhausted.',
  include:'Comma-separated whole words, matched without case sensitivity against headline, summary and categories. Any include word is enough. Blank includes everything. Example: science, space. Phrases and punctuation are not supported.',
  exclude:'Any matching exclude word removes a story, even if it matches Include. Matching uses whole words without case sensitivity in headline, summary and categories. Example: sport, football. Blank excludes nothing.',
  headline_mode:'Headline first: a long headline scrolls across once while the start of the summary waits, then the summary scrolls up. Together: the summary keeps looping upward while the headline scrolls across, and the story ends when the headline is back at its start.',
  frame_delay:'Seconds between one-pixel scroll steps. Drag the slider or type a value: 0.025 is fastest, 0.1 is slowest (0.05 = normal). Story length and font affect the total runtime.',
  layout:'Automatic shows a small site banner (for example BBC: red with BBC on it) at the start of the headline; story photos are not used. Text only hides the banner.',
  headline_font:'Installed BDF font for headlines. Inspect the 64×32 Edit Preview for clipping; exact clipping measurements are not available.',
  summary_font:'Installed BDF font for story summaries. A larger font can increase scroll duration. Inspect Edit Preview for clipping.',
  headline_colour:'Colour of headline text in the prepared story.',summary_colour:'Colour of summary text in the prepared story.',background_colour:'Background colour behind text and any thumbnail.'
};
function rssControl(parent,key,label,type='text',options=null){
  const wrapper=document.createElement('label');wrapper.textContent=label;
  const input=document.createElement(options?'select':'input');input.dataset.rssSetting=key;
  if(options)for(const [value,text] of options){const option=document.createElement('option');option.value=value;option.textContent=text;input.append(option)}else input.type=type;
  if(options&&!options.some(([value])=>String(value)===String(rssSettings[key]))){const saved=document.createElement('option');saved.value=rssSettings[key];saved.textContent=String(rssSettings[key])+' (saved value)';input.append(saved)}
  input.value=Array.isArray(rssSettings[key])?rssSettings[key].join(', '):rssSettings[key];
  input.addEventListener(options?'change':'input',()=>{
    rssSettings[key]=['include','exclude'].includes(key)?input.value.split(',').map(value=>value.trim()).filter(Boolean):['count','frame_delay','thumbnail_speed'].includes(key)?Number(input.value):input.value;rssChanged();
  });wrapper.append(input);const cell=document.createElement('div');cell.className='ux-control';cell.append(wrapper);if(rssHelpText[key])editorHelp(cell,key,label,rssHelpText[key]);parent.append(cell);return input;
}
// Owner 2026-10-15: scroll speed is a slider (left = fastest 0.025 s, right = slowest 0.1 s)
// with an editable number beside it; both stay in sync and save the step delay in seconds.
const RSS_FASTEST_DELAY=.025,RSS_SLOWEST_DELAY=.1;
function rssSpeedControl(parent){
  const cell=document.createElement('div');cell.className='ux-control';
  const wrapper=document.createElement('label');wrapper.textContent='Scroll speed (seconds per step; smaller is faster)';
  const row=document.createElement('div');row.className='rss-speed-row';row.style.cssText='display:flex;align-items:center;gap:8px';
  const fast=document.createElement('span');fast.textContent='Fast';fast.className='hint';
  const slow=document.createElement('span');slow.textContent='Slow';slow.className='hint';
  const slider=document.createElement('input');slider.type='range';slider.min=RSS_FASTEST_DELAY;slider.max=RSS_SLOWEST_DELAY;slider.step=.001;slider.style.flex='1';slider.style.minWidth='0';slider.setAttribute('aria-label','Scroll speed slider');
  const number=document.createElement('input');number.type='number';number.min=RSS_FASTEST_DELAY;number.max=RSS_SLOWEST_DELAY;number.step=.001;number.dataset.rssSetting='frame_delay';number.style.width='6.5em';number.setAttribute('aria-label','Scroll speed in seconds');
  const clamp=value=>Math.min(RSS_SLOWEST_DELAY,Math.max(RSS_FASTEST_DELAY,value));
  const show=value=>{slider.value=clamp(value);number.value=value};
  show(Number(rssSettings.frame_delay)||.05);
  const apply=value=>{if(!Number.isFinite(value))return;rssSettings.frame_delay=Math.round(clamp(value)*1000)/1000;rssChanged()};
  slider.addEventListener('input',()=>{number.value=slider.value;apply(Number(slider.value))});
  number.addEventListener('input',()=>{const value=Number(number.value);if(number.value!==''&&Number.isFinite(value)){slider.value=clamp(value);if(value>=RSS_FASTEST_DELAY&&value<=RSS_SLOWEST_DELAY)apply(value)}});
  number.addEventListener('change',()=>{const value=Number(number.value);const next=Number.isFinite(value)&&number.value!==''?clamp(value):Number(rssSettings.frame_delay)||.05;apply(next);show(rssSettings.frame_delay)});
  row.append(fast,slider,slow,number);wrapper.append(row);cell.append(wrapper);
  if(rssHelpText.frame_delay)editorHelp(cell,'frame_delay','Scroll speed',rssHelpText.frame_delay);
  parent.append(cell);return number;
}
// Friendly feed title: catalogue name when known, else the site's host name.
function rssFeedName(feed){
  const known=(rssCatalogue?.feeds||[]).find(item=>item.url===feed);if(known)return (known.group?.startsWith('BBC')?'BBC · ':'')+known.name;
  try{const url=new URL(feed);return url.hostname.replace(/^(www|feeds)\./,'')+(url.pathname.length>1?' · '+url.pathname.replace(/\/(rss\.xml)?$/,'').split('/').filter(Boolean).slice(-1)[0]||'':'')}catch(_){return feed}
}
function rssAddFeed(value){
  if(rssSettings.feeds.includes(value)){setMessage('Feed is already added.',true);return}
  if(rssSettings.feeds.length>=20){setMessage('At most 20 feeds are supported.',true);return}
  rssSettings.feeds.push(value);rssChanged();renderRSSEditors();
}
function renderRSSEditor(container){
  editorCloseHelp(container);container.replaceChildren();container.style.minWidth='0';
  const note=document.createElement('p');note.textContent='Pick news sources, then press Preview to see it on the 64×32 display. Save applies changes.';container.append(note);
  const feeds=editorSection(container,'Feeds'),selection=editorSection(container,'Story selection'),playback=editorSection(container,'Playback'),appearance=editorSection(container,'Appearance',true),actions=editorSection(container,'Status and actions');
  const quick=document.createElement('div');quick.className='editor-quick-actions';quick.append(makeButton('Preview my changes',()=>editorDraftPreview('rss')));container.insertBefore(quick,feeds.parentElement);
  const fields=document.createElement('div');fields.className='editor-fields';selection.append(fields);
  const count=rssControl(fields,'count','Stories to show each time (1–50)','number');count.min=1;count.max=50;
  editorAdvanced(rssControl(fields,'include','Only show stories with these words (comma-separated)').closest('.ux-control'));editorAdvanced(rssControl(fields,'exclude','Hide stories with these words (comma-separated)').closest('.ux-control'));
  rssControl(fields,'headline_font','Headline font','text',installedFonts.map(font=>[font,font]));
  rssControl(fields,'summary_font','Summary font','text',installedFonts.map(font=>[font,font]));
  rssControl(fields,'layout','Story layout','text',[['auto','Site banner + text'],['text','Text only']]);
  rssControl(fields,'headline_colour','Headline colour','color');
  rssControl(fields,'summary_colour','Summary colour','color');
  rssControl(fields,'background_colour','Background colour','color');
  rssControl(fields,'headline_mode','Scrolling','text',[['once','Headline first, then summary'],['continuous','Summary loops while headline scrolls']]);
  rssSpeedControl(fields);
  const appearanceFields=document.createElement('div');appearanceFields.className='editor-fields';appearance.append(appearanceFields);
  const playbackFields=document.createElement('div');playbackFields.className='editor-fields';playback.append(playbackFields);
  for(const key of ['headline_font','summary_font','layout','headline_colour','summary_colour','background_colour'])appearanceFields.append(fields.querySelector(`[data-rss-setting="${key}"]`).closest('.ux-control'));
  for(const key of ['headline_font','summary_font','layout'])editorAdvanced(appearanceFields.querySelector(`[data-rss-setting="${key}"]`).closest('.ux-control'));
  for(const key of ['headline_mode','frame_delay'])playbackFields.append(fields.querySelector(`[data-rss-setting="${key}"]`).closest('.ux-control'));
  editorAdvanced(editorParagraph(appearance,'Fonts are the display’s pixel fonts. Check the preview for text that is cut off.'));
  // Owner: left = default presets dropdown with Add preset below; right = custom RSS URL with Add custom RSS below.
  const feedFields=document.createElement('div');feedFields.className='rss-add-grid';feeds.append(feedFields);
  const presetCol=document.createElement('div');presetCol.className='rss-add-col';presetCol.dataset.rssAddPresetCol='';
  const presets=document.createElement('select');presets.id='rss-presets';presets.dataset.rssPresets='';rssFillPresets(presets);
  const presetLabel=document.createElement('label');presetLabel.htmlFor=presets.id;presetLabel.textContent='Default presets';
  const addPreset=makeButton('Add preset',()=>{if(presets.value)rssAddFeed(presets.value)});addPreset.dataset.rssAddPreset='';
  presetCol.append(presetLabel,presets,addPreset);
  const customCol=document.createElement('div');customCol.className='rss-add-col ux-control';customCol.dataset.rssAddCustomCol='';
  const url=document.createElement('input');url.type='url';url.id='rss-custom-url';url.dataset.rssUrl='';url.placeholder='https://example.com/rss.xml';
  const urlLabel=document.createElement('label');urlLabel.htmlFor=url.id;urlLabel.textContent='Custom RSS feed address';
  const add=makeButton('Add custom RSS',()=>{const value=url.value.trim();if(!value||!url.checkValidity())return url.reportValidity();rssAddFeed(value)});add.dataset.rssAdd='';
  const urlHead=document.createElement('div');urlHead.className='rss-add-head';urlHead.append(urlLabel);
  customCol.append(urlHead,url,add);
  feedFields.append(presetCol,customCol);
  editorHelp(urlHead,'feed-url','Custom RSS feed address','Use the publisher’s public RSS or Atom feed address, not its homepage or an article link. Example: https://feeds.bbci.co.uk/news/rss.xml. Adding a feed changes this draft only. Private/local network sources are not allowed.');
  editorAdvanced(editorParagraph(feeds,'Default presets: BBC headlines and UK nations, plus world and sport feeds that were checked live. Pick one and press Add preset.'));
  rssSettings.feeds.forEach((feed,index)=>{
    const row=document.createElement('fieldset');row.dataset.rssRow='';row.className='editor-fields';row.style.minWidth='0';
    const name=document.createElement('legend');name.className='visually-hidden';name.textContent=rssFeedName(feed);row.append(name);
    const title=document.createElement('strong');title.className='rss-feed-title';title.textContent=rssFeedName(feed);title.title=feed;row.append(title);
    const enabledLabel=document.createElement('label');enabledLabel.textContent='Use this feed';
    const enabled=document.createElement('input');enabled.type='checkbox';enabled.dataset.rssEnabled='';enabled.checked=!rssSettings.disabled_feeds.includes(feed);enabled.setAttribute('aria-label','Enable feed '+feed);
    enabled.addEventListener('change',()=>{rssSettings.disabled_feeds=rssSettings.disabled_feeds.filter(url=>url!==feed);if(!enabled.checked)rssSettings.disabled_feeds.push(feed);rssChanged()});enabledLabel.prepend(enabled);enabledLabel.classList.add('editor-check');const enabledCell=document.createElement('div');enabledCell.className='ux-control';enabledCell.append(enabledLabel);row.append(enabledCell);
    editorHelp(enabledCell,'feed-enabled','Use this feed','Unchecked feeds retain their URL and order but are not fetched or selected for playback after Save. This checkbox changes the draft only; saved playback is unchanged until Save. Test feed still works for diagnostics.');
    const result=document.createElement('p');result.style.overflowWrap='anywhere';result.setAttribute('role','status');
    const runTest=async()=>{
      if(test.disabled||!test.isConnected)return;
      test.disabled=true;result.textContent='Testing feed…';
      try{const value=await api('/api/rss/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:feed})});
        result.textContent=`${value.title} · ${value.count} items · Newest: ${value.newest||'unknown'} · Image hints: ${value.image_available?'available (not decoded)':'none'} · ${(value.warnings||[]).join('; ')}`;
      }catch(error){result.textContent='Feed rejected: '+error.message}finally{test.disabled=false}
    };const test=makeButton('Test feed',runTest);test.rssRunTest=runTest;test.dataset.rssTest='';test.setAttribute('aria-label','Test feed '+feed);row.append(test,result);editorAdvanced(test);
    for(const [step,label,key] of [[-1,'Move up','rssUp'],[1,'Move down','rssDown']]){
      const move=makeButton(label,()=>{const next=index+step;[rssSettings.feeds[index],rssSettings.feeds[next]]=[rssSettings.feeds[next],rssSettings.feeds[index]];rssChanged();renderRSSEditors()});
      move.dataset[key]='';move.disabled=index+step<0||index+step>=rssSettings.feeds.length;move.setAttribute('aria-label',label+' '+feed);row.append(move);editorAdvanced(move);
    }
    const remove=makeButton('Delete feed',()=>{rssSettings.feeds.splice(index,1);rssSettings.disabled_feeds=rssSettings.disabled_feeds.filter(url=>url!==feed);rssChanged();renderRSSEditors()},'btn danger');remove.dataset.rssDelete='';remove.setAttribute('aria-label','Delete feed '+feed);row.append(remove);feeds.append(row);
  });
  editorAdvanced(actions.parentElement);
  editorParagraph(actions,'Saved feed health below is separate from the unsaved draft. Refresh reads saved status; Test feeds diagnoses the draft URLs. Calculate and Preview use draft settings.');
  const status=document.createElement('div');status.dataset.rssStatus='';status.setAttribute('role','status');status.style.overflowWrap='anywhere';actions.append(status,makeButton('Refresh feed status',rssRefreshStatus));
  editorHelp(actions,'refresh','Refresh, cache and staleness','Saved feeds refresh in the background every 30 minutes. Refresh feed status only reads that status; it does not force a fetch. Stale means an update failed and a usable cached copy remains. Expired cache is not played; empty or unusable feeds skip.');
  const testAll=makeButton('Test feeds',async()=>{testAll.disabled=true;try{for(const button of container.querySelectorAll('[data-rss-test]')){if(!container.isConnected)break;await button.rssRunTest()}}finally{testAll.disabled=false}});testAll.dataset.rssTestAll='';feeds.append(testAll);editorAdvanced(testAll);
  editorAdvanced(editorHelp(feeds,'test','Test feeds','Fetch and validate each draft feed URL, including unchecked feeds, without saving configuration. Reports item count and publisher image hints; an image hint does not prove decoding will succeed. This is a diagnostic, not a guarantee of prepared playback.'));
  const duration=document.createElement('p');duration.dataset.rssDuration='';duration.setAttribute('role','status');actions.append(duration,makeButton('Calculate prepared duration',rssCalculate),makeButton('Preview unsaved draft',()=>editorDraftPreview('rss')));
  editorHelp(actions,'duration','Computed timing','Duration comes from prepared eligible stories, fonts, scrolling and thumbnail timing, not the playlist compatibility duration. Change settings, then Calculate again. No prepared stories means duration is unavailable; check feeds and filters.');
  const reset=makeButton('Reset draft to defaults',()=>{
    if(!confirm('Replace this RSS draft with defaults? Nothing is saved and playback/cache are unchanged.'))return;
    rssSettings={feeds:structuredClone(rssDefaultFeeds),disabled_feeds:[],refresh_seconds:1800,count:5,include:[],exclude:[],headline_font:'6x9',summary_font:'5x8',frame_delay:.05,headline_mode:'continuous',thumbnail_speed:100,layout:'auto',headline_colour:'#eea62b',summary_colour:'#ffffff',background_colour:'#000000'};
    rssChanged();renderRSSEditors();
  },'btn danger');reset.dataset.rssResetDraft='';
  const resetAll=makeButton('Reset to defaults and clear caches',async()=>{
    if(resetAll.disabled||!confirm('Reset this RSS draft to defaults and clear shared feed/thumbnail caches and seen stories now? Saved configuration is unchanged until Save; playback continues using saved feeds.'))return;
    resetAll.disabled=true;const draft=JSON.stringify(rssSettings);
    rssTimingGeneration++;rssTimingController?.abort();rssTimingController=null;
    if(previewMode==='edit'&&previewModule==='rss')cancelEditPreview();
    try{
      const result=await api('/api/rss/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
      if(JSON.stringify(rssSettings)!==draft){setMessage('Caches and seen stories cleared. Newer draft edits were kept; defaults were not applied.');return}
      rssSettings=structuredClone(result.defaults);rssChanged();renderRSSEditors();
      setMessage('Caches and seen stories cleared. RSS draft restored to defaults; Save to apply settings. Saved playback remains enabled or disabled as configured.');
      await rssRefreshStatus();
    }catch(error){setMessage('RSS reset failed: '+error.message,true)}finally{resetAll.disabled=false}
  },'btn danger');resetAll.dataset.rssResetAll='';
  const resetNote=document.createElement('p');resetNote.textContent='Draft reset does not clear cache or seen stories. Reset to defaults clears shared caches immediately but does not save configuration. Disabled feeds keep their position and URL but are not fetched or selected for playback. Test feed remains available for diagnostics.';
  actions.append(reset,resetAll,resetNote);
  editorHelp(actions,'reset','Reset and clear caches','Reset draft to defaults only replaces unsaved settings. Reset to defaults and clear caches also immediately clears shared feed/thumbnail caches and seen-story history. Neither saves configuration. Both ask for confirmation; Save is needed to apply default settings.');
  rssRenderStatus();rssRenderDuration();if(!rssCatalogue)rssRefreshStatus();
  editorAdvancedToggle(container,'rss');
}
window.addEventListener('beforeunload',()=>{rssTimingGeneration++;rssTimingController?.abort()},{once:true});
