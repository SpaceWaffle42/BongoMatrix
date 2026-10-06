/* Grouped views borrow owning v4 row source pools and layout controls.
 * No Canvas serializer, Save lifecycle, or media ownership is replaced. */
let screenMediaCatalog = null;
let screenMediaCatalogGeneration = 0;
const screenMediaBaseConfigBody = configBody;
configBody = function(previewMedia = false) {
  const body = screenMediaBaseConfigBody(previewMedia);
  body.screen_enabled = Object.fromEntries(['splash', 'sleep'].map(role =>
    [role, document.querySelector('#screen-enabled-' + role).checked]));
  return body;
};
function screenRows(role) { return mediaPlaylistRows(role); }
function screenSourceInput(row, source) {
  return [...row.closest('.module-item').querySelectorAll('[data-media-source]')]
    .find(input => input.dataset.mediaSource === source);
}
function screenRowForSource(role, source) {
  return screenRows(role).find(row => screenSourceInput(row, source)?.checked);
}
function screenItem(row) {
  const sources = mediaPlaylistSources(row);
  return {id:row.dataset.itemId,module:row.dataset.module,duration:Number(row.dataset.compatDuration),
    enabled:row.querySelector('.enabled').checked,sources,source:sources[0]||'',layout:mediaPlaylistLayout(row)};
}
function screenNewId(role) {
  let id; do { id=role+'_'+crypto.randomUUID().replaceAll('-',''); }
  while (['splash','sleep'].flatMap(screenRows).some(row=>row.dataset.itemId===id));
  return id;
}
function normalizeScreenPools() {
  const rows=['splash','sleep'].flatMap(screenRows),items=rows.map(screenItem);
  const extra=items.reduce((count,item)=>count+Math.max(0,item.sources.length-1),0);
  if(document.querySelectorAll('#playlist .module-row').length+extra>256) {
    setMessage('Independent choices would exceed the 256-row playlist limit. Existing pools retained.',true);return false;
  }
  const changes=[];
  for(let index=0;index<rows.length;index++) {
    const item=items[index];if(item.sources.length<2)continue;
    // Non-prefix built-ins in this catalog are saved-only choices, not
    // independently admissible selections. Keep their existing owning ID.
    const retainedIndex=item.sources.findIndex(source=>screenMediaCatalog?.[item.module]?.choices.some(
      media=>media.name===source&&media.built_in&&!source.startsWith('built-in/'+item.module)));
    const anchor=retainedIndex<0?0:retainedIndex,before=[],after=[];
    item.sources.forEach((source,sourceIndex)=>{
      if(sourceIndex===anchor)return;
      const wrapper=createPlaylistRow({...item,id:screenNewId(item.module),sources:[source],source,layout:structuredClone(item.layout)});
      wrapper.hidden=true;(sourceIndex<anchor?before:after).push(wrapper);
    });changes.push({row:rows[index],source:item.sources[anchor],before,after});
  }
  // Preflight and detached construction finish before touching any pool.
  for(const {row,source,before,after} of changes) {
    for(const input of row.closest('.module-item').querySelectorAll('[data-media-source]'))input.checked=input.dataset.mediaSource===source;
    row.closest('.module-item').before(...before);
    row.closest('.module-item').after(...after);
  }
  if(changes.length)markMatrixDirty();return true;
}
function materializeScreenChoice(role, source) {
  if (!matrixEditorsReady || !screenMediaCatalog?.[role]?.choices.some(item => item.name === source)) return null;
  const existing=screenRowForSource(role,source);
  if(existing&&mediaPlaylistSources(existing).length>1){setMessage('Independent choices would exceed the 256-row playlist limit. Existing pools retained.',true);return null;}
  if(existing)return existing;
  if(document.querySelectorAll('#playlist .module-row').length>=256){setMessage('The playlist supports up to 256 rows.',true);return null;}
  const wrapper=createPlaylistRow({id:screenNewId(role),module:role,duration:role==='splash'?8:60,enabled:true,
    sources:[source],source,layout:structuredClone(builtInDefaults[role])});
  wrapper.hidden=true;document.querySelector('#playlist').append(wrapper);return wrapper.querySelector('.module-row');
}
function screenChoiceChanged(role) {
  markMatrixDirty();
  globalThis.publishDeviceDraftMutation?.(role, 150);
}
function selectScreenMediaChoice(role, source) {
  if (!matrixEditorsReady || !screenMediaCatalog?.[role]) {
    setMessage('Media choices are still loading. Retry selection when the grouped selector is ready.', true); return false;
  }
  if (!screenMediaCatalog[role].choices.some(item => item.name === source)) {
    setMessage('That media file is not available in the ' + role + ' selector.', true); return false;
  }
  const row = materializeScreenChoice(role, source);
  if (!row) return false;
  row.querySelector('.enabled').checked = true;
  updateScreenMediaChecks(role);
  previewMediaItemId = row.dataset.itemId; selectEditPreviewModule(role, false);
  screenChoiceChanged(role);
  const section = document.querySelector('[data-screen-role="' + role + '"]');
  section.scrollIntoView({behavior:'smooth',block:'center'});
  setMessage(source + ' selected for random ' + role + ' rotation. Save configuration to apply.');
  return true;
}
function setScreenMediaSelection(role, mode) {
  if (!matrixEditorsReady || !screenMediaCatalog?.[role]?.choices.length) return;
  const catalog = screenMediaCatalog[role], sources = mode === 'all'
    ? catalog.choices.map(item => item.name) : [catalog.default];
  if (sources.some(source => !source)) { setMessage('No usable default is available. Existing choices are retained.', true); return; }
  const missing=sources.filter(source=>!screenRowForSource(role,source));
  if(document.querySelectorAll('#playlist .module-row').length+missing.length>256) {
    setMessage('Selecting these choices would exceed the 256-row playlist limit.',true);return;
  }
  const targets = sources.map(source => screenRowForSource(role, source) || materializeScreenChoice(role, source));
  if (targets.some(row => !row)) return;
  for (const row of screenRows(role)) {
    const source=mediaPlaylistSources(row)[0];
    if(catalog.choices.some(media=>media.name===source))row.querySelector('.enabled').checked=sources.includes(source);
  }
  updateScreenMediaChecks(role); screenChoiceChanged(role);
}
function screenMediaCard(role, media, row) {
  const card = document.createElement('div'); card.className = 'palette-choice screen-media-choice';
  card.dataset.source = media.name; if (row) card.dataset.rowId = row.dataset.itemId;
  const label = document.createElement('label'), check = document.createElement('input');
  check.type = 'checkbox'; check.className = 'screen-media-selected';
  check.checked = Boolean(row?.querySelector('.enabled').checked && screenSourceInput(row, media.name)?.checked);
  const image = document.createElement('img'); image.alt = ''; image.width = 64; image.height = 32;
  image.className = 'screen-media-thumbnail'; image.src = '/media/' + media.name.split('/').map(encodeURIComponent).join('/');
  const title = document.createElement('span'); title.textContent = media.name;
  label.append(check, image, title); card.append(label);
  const inspect = makeButton('Inspect ' + media.name + ' in Edit Preview', () => {
    if (!row) return;
    previewMediaItemId = row.dataset.itemId; selectEditPreviewModule(role, false);
    requestPreviewMode('edit'); scheduleEditPreview(0);
  });
  inspect.classList.add('palette-inspect'); inspect.disabled = !row; card.append(inspect);
  function bindRow(owner) {
    if (card.dataset.rowId === owner.dataset.itemId && card.querySelector('.media-playlist-editor')) return;
    row = owner; card.dataset.rowId = row.dataset.itemId; inspect.disabled = false;
    const original = row.closest('.module-item').querySelector('.media-playlist-editor');
    const editor = original.cloneNode(true); editor.hidden = false; editor.removeAttribute('data-media-item-editor');
    editor.querySelector('.media-source-options').remove(); editor.querySelector('.editor-toolbar').remove();
    for (const control of editor.querySelectorAll('[data-media-layout]')) control.addEventListener('input', () => {
      const target = original.querySelector('[data-media-layout="' + control.dataset.mediaLayout + '"]');
      if (control.type === 'checkbox') target.checked = control.checked; else target.value = control.value;
      // Existing multi-source rows retain shared layout semantics; mirror sibling cards without rerender/focus loss.
      for (const sibling of document.querySelectorAll('.screen-media-choice[data-row-id="' + row.dataset.itemId + '"] [data-media-layout="' + control.dataset.mediaLayout + '"]')) {
        if (sibling.type === 'checkbox') sibling.checked = control.checked; else sibling.value = control.value;
      }
      previewMediaItemId = row.dataset.itemId; selectEditPreviewModule(role); screenChoiceChanged(role);
    });
    card.append(editor, makeButton('Reset layout', () => {
      resetMediaPlaylistItem(original.querySelector('button'));
      for(const control of editor.querySelectorAll('[data-media-layout]')) {
        const target=original.querySelector('[data-media-layout="'+control.dataset.mediaLayout+'"]');
        if(control.type==='checkbox')control.checked=target.checked;else control.value=target.value;
      }
      screenChoiceChanged(role);
    }));
  }
  card.bindScreenRow = bindRow;
  if (row) bindRow(row);
  check.addEventListener('change', () => {
    if (!matrixEditorsReady) { renderScreenMediaRole(role); return; }
    if (!row) row = materializeScreenChoice(role, media.name);
    if (!row) { check.checked = false; return; }
    const input = screenSourceInput(row, media.name);
    row.querySelector('.enabled').checked = check.checked;
    const usable = screenRows(role).some(candidate => candidate.querySelector('.enabled').checked &&
      mediaPlaylistSources(candidate).some(source => screenMediaCatalog[role].choices.some(item => item.name === source)));
    if (!usable) {
      const source = screenMediaCatalog[role].default;
      const target = source && (screenRowForSource(role, source) || materializeScreenChoice(role, source));
      if (target) { screenSourceInput(target, source).checked = true; target.querySelector('.enabled').checked = true; }
      else { input.checked = true; row.querySelector('.enabled').checked = true; }
    }
    updateScreenMediaChecks(role);
    screenChoiceChanged(role);
  });
  return card;
}
function updateScreenMediaChecks(role) {
  const section=document.querySelector('[data-screen-role="'+role+'"]');
  for(const card of section.querySelectorAll('.screen-media-choice')) {
    const row=card.dataset.rowId
      ? screenRows(role).find(row=>row.dataset.itemId===card.dataset.rowId)
      : screenRowForSource(role,card.dataset.source);
    if(row)card.bindScreenRow(row);
    card.querySelector('.screen-media-selected').checked=Boolean(row?.querySelector('.enabled').checked);
  }
  for(const group of section.querySelectorAll('[data-screen-group]')) {
    const checks=[...group.querySelectorAll('.screen-media-selected')];
    group.querySelector('.palette-count').textContent=checks.filter(input=>input.checked).length+' / '+checks.length+' selected';
  }
}
function renderScreenMediaRole(role) {
  const section = document.querySelector('[data-screen-role="' + role + '"]'), catalog = screenMediaCatalog[role];
  for (const group of section.querySelectorAll('[data-screen-group]')) {
    const grid = group.querySelector('.palette-grid'),scroll=grid.scrollTop; grid.replaceChildren();
    for (const media of catalog.choices.filter(item => Boolean(item.built_in) === (group.dataset.screenGroup === 'built-in'))) {
      const rows = screenRows(role).filter(row => screenSourceInput(row, media.name)?.checked);
      if (rows.length) for (const row of rows) grid.append(screenMediaCard(role, media, row));
      else grid.append(screenMediaCard(role, media, null));
    }
    grid.scrollTop=scroll;
    const cards = [...grid.querySelectorAll('.screen-media-selected')];
    group.querySelector('.palette-count').textContent = cards.filter(input => input.checked).length + ' / ' + cards.length + ' selected';
  }
  section.querySelector('.screen-media-state').textContent = catalog.choices.length
    ? 'Keep at least one media choice selected. Whole-screen Off retains choices and layouts.'
    : 'No eligible media available. Existing choices are retained.';
  for (const button of section.querySelectorAll('[data-screen-action]')) button.disabled = !matrixEditorsReady || !catalog.choices.length;
}
async function loadScreenMediaCatalog() {
  const generation = ++screenMediaCatalogGeneration;
  try {
    const catalog = await api('/api/screen-media');
    if (generation !== screenMediaCatalogGeneration || matrixSyncStopped) return;
    screenMediaCatalog = catalog;
    if(!normalizeScreenPools())return;
    for (const role of ['splash', 'sleep']) renderScreenMediaRole(role);
  } catch (error) {
    if (generation !== screenMediaCatalogGeneration) return;
    for (const section of document.querySelectorAll('[data-screen-role]')) section.querySelector('.screen-media-state').textContent = 'Unable to load eligible media. Existing choices are retained. ' + error.message;
  }
}
const screenMediaBaseRefresh = refreshMedia;
refreshMedia = async function() { await screenMediaBaseRefresh(); await loadScreenMediaCatalog(); };
const screenMediaBaseRemoval=reconcileMediaRemoval;
reconcileMediaRemoval=function(name){const changed=screenMediaBaseRemoval(name);if(changed)loadScreenMediaCatalog();return changed;};
for (const role of ['splash', 'sleep']) {
  for (const row of screenRows(role)) row.closest('.module-item').hidden = true;
  document.querySelector('#screen-enabled-' + role).addEventListener('change', () => screenChoiceChanged(role));
}
if (matrixEditorsReady && !matrixDirty) matrixSavedSnapshot = matrixCurrentSnapshot();
loadScreenMediaCatalog();
