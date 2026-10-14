// Shared presentation only: existing serializers remain the source of draft state.
const editorTimezones = {{ editor_timezones|tojson }};
let editorElementSequence=0, editorActiveHelp=null;
// Owner 2026-10-14: all time defaults from the Weather location. A blank
// feature zone follows Weather; a chosen zone wins; unknown Weather = system clock.
function editorWeatherZone(){return document.querySelector('#weather-timezone')?.value||''}
function editorEffectiveZone(zone){return zone||editorWeatherZone()||undefined}
globalThis.editorEffectiveZone=editorEffectiveZone;
function editorFollowWeatherLabel(select){
  if(!select||!select.options.length||select.options[0].value!=='')return;
  const zone=editorWeatherZone();
  select.options[0].textContent=zone?`Same as Weather (${zone})`:'Same as Weather (unknown, using the system clock: choose Weather location again)';
}
function editorTimezoneSelect(value) {
  const select=document.createElement('select');
  const values=['', 'UTC', ...editorTimezones.filter(zone=>zone!=='UTC')];
  if(value&&!values.includes(value))values.push(value); // Keep saved server-supported aliases/custom zones verbatim.
  for(const zone of values){
    const option=document.createElement('option');option.value=zone;
    option.textContent=zone?zone==='UTC'?'Coordinated Universal Time (UTC)':`${zone.split('/').reverse().join(' · ').replaceAll('_',' ')} (${zone})`:'Select timezone';
    select.append(option);
  }
  select.value=value||'';return select;
}
function editorSection(container,title,collapsed=false) {
  const section=document.createElement(collapsed?'details':'section');section.className='editor-group ux-section';
  const heading=document.createElement(collapsed?'summary':'h3');heading.textContent=title;
  heading.id='editor-section-'+(++editorElementSequence);section.setAttribute('aria-labelledby',heading.id);
  const body=document.createElement('div');body.className='ux-section-body';section.append(heading,body);container.append(section);return body;
}
// Owner: every editor opens Simple; one remembered per-module toggle reveals Advanced.
// Presentation only: hidden controls keep their values and serializers are untouched.
const EDITOR_ADVANCED_KEY='matrix.editorAdvanced.v1';
function editorAdvancedPreference(module){try{return Boolean(JSON.parse(localStorage.getItem(EDITOR_ADVANCED_KEY)||'{}')[module])}catch(_){return false}}
function editorSetAdvancedPreference(module,on){try{const all=JSON.parse(localStorage.getItem(EDITOR_ADVANCED_KEY)||'{}');if(on)all[module]=true;else delete all[module];localStorage.setItem(EDITOR_ADVANCED_KEY,JSON.stringify(all))}catch(_){}}
function editorAdvanced(...elements){for(const element of elements)element?.setAttribute?.('data-advanced','');return elements[0]}
function editorAdvancedToggle(container,module){
  const bar=document.createElement('div');bar.className='editor-mode-bar';
  const label=document.createElement('label');label.className='editor-mode-toggle';
  const input=document.createElement('input');input.type='checkbox';input.dataset.advancedToggle=module;
  const apply=on=>{input.checked=on;container.toggleAttribute('data-show-advanced',on)};
  apply(editorAdvancedPreference(module));
  input.addEventListener('change',()=>{editorCloseHelp(container);editorSetAdvancedPreference(module,input.checked);apply(input.checked)});
  label.append(input,document.createTextNode(' Show advanced options'));
  const hint=document.createElement('span');hint.className='editor-note';hint.textContent='Simple shows the everyday settings. Advanced adds fine-tuning; nothing is lost when hidden.';
  bar.append(label,hint);container.prepend(bar);return input;
}
function editorParagraph(parent,text){const p=document.createElement('p');p.textContent=text;parent.append(p);return p}
function editorCloseHelp(container=null){
  if(!editorActiveHelp||(container&&!container.contains(editorActiveHelp.button)))return;
  const {button,panel,timer}=editorActiveHelp;clearTimeout(timer);panel.hidePopover();button.setAttribute('aria-expanded','false');editorActiveHelp=null;
}
function editorHelp(parent,key,label,text){
  const button=document.createElement('button');button.type='button';button.className='btn editor-help';button.textContent='?';button.dataset.help=key;
  button.setAttribute('aria-label','Help: '+label);button.setAttribute('aria-expanded','false');
  const panel=document.createElement('div');panel.className='editor-help-panel';panel.id='editor-help-'+(++editorElementSequence);panel.setAttribute('popover','manual');panel.setAttribute('role','tooltip');panel.textContent=text;
  button.setAttribute('aria-controls',panel.id);button.setAttribute('aria-describedby',panel.id);
  const show=()=>{
    if(editorActiveHelp?.button===button)return;
    editorCloseHelp();editorActiveHelp={button,panel,pinned:false};panel.style.width=Math.min(360,document.documentElement.clientWidth-16)+'px';panel.showPopover();button.setAttribute('aria-expanded','true');
    const r=button.getBoundingClientRect(),height=panel.getBoundingClientRect().height;
    panel.style.left=Math.max(8,Math.min(r.left,document.documentElement.clientWidth-panel.offsetWidth-8))+'px';
    panel.style.top=Math.max(8,Math.min(r.bottom+6,innerHeight-height-8))+'px';
  };
  button.addEventListener('mouseenter',show);button.addEventListener('focus',show);
  const leave=()=>{if(editorActiveHelp?.button!==button)return;clearTimeout(editorActiveHelp.timer);editorActiveHelp.timer=setTimeout(()=>{if(editorActiveHelp?.button===button&&!editorActiveHelp.pinned&&document.activeElement!==button&&!button.matches(':hover')&&!panel.matches(':hover'))editorCloseHelp()},150)};
  button.addEventListener('mouseleave',leave);panel.addEventListener('mouseleave',leave);
  button.addEventListener('blur',()=>{if(editorActiveHelp?.button===button&&!editorActiveHelp.pinned)editorCloseHelp()});
  button.addEventListener('click',event=>{event.stopPropagation();if(editorActiveHelp?.button===button&&editorActiveHelp.pinned)editorCloseHelp();else{show();editorActiveHelp.pinned=true}});
  parent.append(button,panel);return button;
}
document.addEventListener('keydown',event=>{if(event.key==='Escape')editorCloseHelp()});
document.addEventListener('pointerdown',event=>{if(editorActiveHelp&&!editorActiveHelp.button.contains(event.target)&&!editorActiveHelp.panel.contains(event.target))editorCloseHelp()});
window.addEventListener('resize',()=>editorCloseHelp());
function editorDraftPreview(module){selectEditPreviewModule(module,false);if(previewMode==='edit')scheduleEditPreview(0);else requestPreviewMode('edit')}
