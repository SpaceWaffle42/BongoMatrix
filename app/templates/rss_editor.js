// RSS shares the dashboard's serializer, revision gate and Save lifecycle.
let rssSettings = {{ config.rss|tojson }};
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
  for(const [label,names] of [['BBC headlines',['Top Stories','World','UK']],['BBC UK nations',['England','Northern Ireland','Scotland','Wales']]]){
    const group=document.createElement('optgroup');group.label=label;
    for(const feed of rssCatalogue?.feeds||[])if(names.includes(feed.name)){
      const option=document.createElement('option');option.value=feed.url;option.textContent=feed.name;group.append(option);
    }select.append(group);
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
function rssControl(parent,key,label,type='text',options=null){
  const wrapper=document.createElement('label');wrapper.textContent=label;
  const input=document.createElement(options?'select':'input');input.dataset.rssSetting=key;
  if(options)for(const [value,text] of options){const option=document.createElement('option');option.value=value;option.textContent=text;input.append(option)}else input.type=type;
  input.value=Array.isArray(rssSettings[key])?rssSettings[key].join(', '):rssSettings[key];
  input.addEventListener(options?'change':'input',()=>{
    rssSettings[key]=['include','exclude'].includes(key)?input.value.split(',').map(value=>value.trim()).filter(Boolean):['count','frame_delay','thumbnail_speed'].includes(key)?Number(input.value):input.value;rssChanged();
  });wrapper.append(input);parent.append(wrapper);return input;
}
function rssAddFeed(value){
  if(rssSettings.feeds.includes(value)){setMessage('Feed is already added.',true);return}
  if(rssSettings.feeds.length>=20){setMessage('At most 20 feeds are supported.',true);return}
  rssSettings.feeds.push(value);rssChanged();renderRSSEditors();
}
function renderRSSEditor(container){
  container.replaceChildren();container.style.minWidth='0';
  const note=document.createElement('p');note.textContent='RSS / Atom · 30-minute refresh · Auto layout: 16×16 image when usable; otherwise full-width text. City and county BBC presets are not available in the verified catalogue. Colours and manual layout overrides are not available in this saved contract.';container.append(note);
  const fields=document.createElement('div');fields.className='editor-fields';container.append(fields);
  const count=rssControl(fields,'count','Stories per pass (1–50; default 5)','number');count.min=1;count.max=50;
  rssControl(fields,'include','Include whole words (comma-separated)');rssControl(fields,'exclude','Exclude whole words (comma-separated)');
  rssControl(fields,'headline_font','Headline font','text',installedFonts.map(font=>[font,font]));
  rssControl(fields,'summary_font','Summary font','text',installedFonts.map(font=>[font,font]));
  rssControl(fields,'frame_delay','Overview scroll speed','text',[[.1,'Slow'],[.05,'Normal'],[.025,'Fast']]);
  rssControl(fields,'headline_mode','Headline scrolling','text',[['once','One pass, then restore and freeze'],['continuous','Continuous until summary completes']]);
  const fontNote=document.createElement('p');fontNote.textContent='Fonts are installed BDF fonts. Exact clipping measurements are not yet available; inspect the shared 64×32 Edit Preview.';container.append(fontNote);
  const advanced=document.createElement('details'),summary=document.createElement('summary');summary.textContent='Advanced';advanced.append(summary);
  const speed=rssControl(advanced,'thumbnail_speed','Thumbnail animation speed (%)','number');speed.min=10;speed.max=400;speed.step=.1;container.append(advanced);
  const presetLabel=document.createElement('label');presetLabel.textContent='Verified BBC preset';const presets=document.createElement('select');presets.dataset.rssPresets='';rssFillPresets(presets);presetLabel.append(presets);fields.append(presetLabel,makeButton('Add BBC preset',()=>{if(presets.value)rssAddFeed(presets.value)}));
  const urlLabel=document.createElement('label');urlLabel.textContent='Custom RSS or Atom feed URL';
  const url=document.createElement('input');url.type='url';url.dataset.rssUrl='';urlLabel.append(url);fields.append(urlLabel);
  const add=makeButton('Add feed',()=>{const value=url.value.trim();if(!value||!url.checkValidity())return url.reportValidity();rssAddFeed(value)});add.dataset.rssAdd='';fields.append(add);
  rssSettings.feeds.forEach((feed,index)=>{
    const row=document.createElement('fieldset');row.dataset.rssRow='';row.className='editor-fields';row.style.minWidth='0';
    const name=document.createElement('legend');name.textContent=`Feed ${index+1} of ${rssSettings.feeds.length}: ${feed}`;name.style.overflowWrap='anywhere';row.append(name);
    const result=document.createElement('p');result.style.overflowWrap='anywhere';result.setAttribute('role','status');
    const runTest=async()=>{
      if(test.disabled||!test.isConnected)return;
      test.disabled=true;result.textContent='Testing feed…';
      try{const value=await api('/api/rss/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:feed})});
        result.textContent=`${value.title} · ${value.count} items · Newest: ${value.newest||'unknown'} · Image hints: ${value.image_available?'available (not decoded)':'none'} · ${(value.warnings||[]).join('; ')}`;
      }catch(error){result.textContent='Feed rejected: '+error.message}finally{test.disabled=false}
    };const test=makeButton('Test feed',runTest);test.rssRunTest=runTest;test.dataset.rssTest='';test.setAttribute('aria-label','Test feed '+feed);row.append(test,result);
    for(const [step,label,key] of [[-1,'Move up','rssUp'],[1,'Move down','rssDown']]){
      const move=makeButton(label,()=>{const next=index+step;[rssSettings.feeds[index],rssSettings.feeds[next]]=[rssSettings.feeds[next],rssSettings.feeds[index]];rssChanged();renderRSSEditors()});
      move.dataset[key]='';move.disabled=index+step<0||index+step>=rssSettings.feeds.length;move.setAttribute('aria-label',label+' '+feed);row.append(move);
    }
    const remove=makeButton('Delete feed',()=>{rssSettings.feeds.splice(index,1);rssChanged();renderRSSEditors()},'btn danger');remove.dataset.rssDelete='';remove.setAttribute('aria-label','Delete feed '+feed);row.append(remove);container.append(row);
  });
  const status=document.createElement('div');status.dataset.rssStatus='';status.setAttribute('role','status');status.style.overflowWrap='anywhere';container.append(status,makeButton('Refresh feed status',rssRefreshStatus));
  const testAll=makeButton('Test feeds',async()=>{testAll.disabled=true;try{for(const button of container.querySelectorAll('[data-rss-test]')){if(!container.isConnected)break;await button.rssRunTest()}}finally{testAll.disabled=false}});testAll.dataset.rssTestAll='';container.append(testAll);
  const duration=document.createElement('p');duration.dataset.rssDuration='';duration.setAttribute('role','status');container.append(duration,makeButton('Calculate prepared duration',rssCalculate));
  const reset=makeButton('Reset draft to defaults',()=>{
    if(!confirm('Replace this RSS draft with defaults? Nothing is saved and playback/cache are unchanged.'))return;
    rssSettings={feeds:['https://feeds.bbci.co.uk/news/rss.xml'],refresh_seconds:1800,count:5,include:[],exclude:[],headline_font:'5x7',summary_font:'4x6',frame_delay:.05,headline_mode:'once',thumbnail_speed:100};
    rssChanged();renderRSSEditors();
  },'btn danger');reset.dataset.rssResetDraft='';
  const resetAll=makeButton('Reset defaults and clear caches (unavailable)',()=>{});resetAll.disabled=true;resetAll.dataset.rssResetAll='';
  const resetNote=document.createElement('p');resetNote.textContent='Full cache reset is unavailable pending safe playback lifecycle support. Draft reset does not clear cache or seen stories. Individual disabled feed rows cannot be retained by the current saved URL-list contract; delete a feed to stop using it.';
  container.append(reset,resetAll,resetNote);
  rssRenderStatus();rssRenderDuration();if(!rssCatalogue)rssRefreshStatus();
}
window.addEventListener('beforeunload',()=>{rssTimingGeneration++;rssTimingController?.abort()},{once:true});
