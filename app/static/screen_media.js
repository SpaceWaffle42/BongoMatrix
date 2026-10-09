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
// crypto.randomUUID only exists in secure contexts (HTTPS/localhost); LAN HTTP needs getRandomValues.
function screenRandomHex() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID().replaceAll('-', '');
  return [...crypto.getRandomValues(new Uint8Array(16))].map(byte => byte.toString(16).padStart(2, '0')).join('');
}
function screenNewId(role) {
  let id; do { id=role+'_'+screenRandomHex(); }
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
// Splash/Sleep accept only GIF and still images, so Video FPS is retained in the saved row but not shown.
const SCREEN_LAYOUT_FIELDS = [
  ['position', 'Position', [['x', 'X (0–63)'], ['y', 'Y (0–31)']]],
  ['size', 'Size', [['width', 'Width (1–64)'], ['height', 'Height (1–32)'], ['keep_aspect', 'Keep aspect ratio']]],
  ['playback', 'Playback', [['playback_speed_percent', 'Speed (%)']]],
];
let screenMediaDialogSerial = 0;
function screenLayoutSummary(layout) {
  const full = layout.x === 0 && layout.y === 0 && layout.width === 64 && layout.height === 32;
  const parts = [full ? 'Full screen' : layout.width + '×' + layout.height + ' at ' + layout.x + ', ' + layout.y];
  if (!layout.keep_aspect) parts.push('stretched');
  if (layout.playback_speed_percent !== 100) parts.push(layout.playback_speed_percent + '% speed');
  return parts.join(' · ');
}
// Mirrors MatrixFramework._prepare_image: keep_aspect fits inside the box without enlarging; otherwise stretches.
function screenLayoutDrawSize(layout, media) {
  const sourceWidth = Number(media.width) || 64, sourceHeight = Number(media.height) || 32;
  if (!layout.keep_aspect) return [layout.width, layout.height];
  const scale = Math.min(1, layout.width / sourceWidth, layout.height / sourceHeight);
  return [Math.max(1, Math.round(sourceWidth * scale)), Math.max(1, Math.round(sourceHeight * scale))];
}
function screenMediaCard(role, media, row) {
  const card = document.createElement('div'); card.className = 'palette-choice screen-media-choice';
  card.dataset.source = media.name; if (row) card.dataset.rowId = row.dataset.itemId;
  const mediaUrl = '/media/' + media.name.split('/').map(encodeURIComponent).join('/');
  const label = document.createElement('label'), check = document.createElement('input');
  check.type = 'checkbox'; check.className = 'screen-media-selected';
  check.checked = Boolean(row?.querySelector('.enabled').checked && screenSourceInput(row, media.name)?.checked);
  const image = document.createElement('img'); image.alt = ''; image.width = 64; image.height = 32;
  image.className = 'screen-media-thumbnail';
  lazyMediaPreviews.register(image, mediaUrl);
  const title = document.createElement('span'); title.textContent = media.name;
  label.append(check, image, title); card.append(label);
  const summary = document.createElement('p'); summary.className = 'screen-media-summary';
  summary.textContent = 'Select to set up layout'; card.append(summary);
  const actions = document.createElement('div'); actions.className = 'screen-media-card-actions';
  const edit = makeButton('Edit', () => openDialog());
  edit.classList.add('screen-media-edit');
  // Layout editing is offered only for selected (enabled) cards; unselecting closes an open editor.
  function syncSelection() {
    edit.disabled = !row || !check.checked;
    if (edit.disabled && dialog?.open) dialog.close();
  }
  card.syncScreenSelection = syncSelection;
  edit.setAttribute('aria-label', 'Edit layout for ' + media.name);
  const inspect = makeButton('Preview', () => {
    if (!row) return;
    previewMediaItemId = row.dataset.itemId; selectEditPreviewModule(role, false);
    requestPreviewMode('edit'); scheduleEditPreview(0);
  });
  inspect.classList.add('palette-inspect'); inspect.disabled = !row;
  inspect.setAttribute('aria-label', 'Inspect ' + media.name + ' in Edit Preview');
  actions.append(edit, inspect); card.append(actions);
  let dialog = null, stageImage = null, stageBox = null;
  function refreshLayout() {
    if (!row) return;
    const layout = mediaPlaylistLayout(row);
    summary.textContent = screenLayoutSummary(layout);
    if (!dialog) return;
    const [drawWidth, drawHeight] = screenLayoutDrawSize(layout, media);
    Object.assign(stageBox.style, {left: layout.x / 64 * 100 + '%', top: layout.y / 32 * 100 + '%',
      width: layout.width / 64 * 100 + '%', height: layout.height / 32 * 100 + '%'});
    Object.assign(stageImage.style, {left: layout.x / 64 * 100 + '%', top: layout.y / 32 * 100 + '%',
      width: drawWidth / 64 * 100 + '%', height: drawHeight / 32 * 100 + '%'});
  }
  card.refreshScreenLayout = refreshLayout;
  function openDialog() {
    if (!row || !dialog) return;
    stageImage.src = mediaUrl; refreshLayout();
    if (!dialog.open) dialog.showModal();
  }
  function setLayout(values) {
    for (const [field, value] of Object.entries(values)) {
      const control = dialog.querySelector('[data-media-layout="' + field + '"]');
      if (!control || String(control.value) === String(value)) continue;
      control.value = value; control.dispatchEvent(new Event('input', {bubbles: true}));
    }
  }
  function centred(layout) {
    const [drawWidth, drawHeight] = screenLayoutDrawSize(layout, media);
    return {x: Math.max(0, Math.floor((64 - drawWidth) / 2)), y: Math.max(0, Math.floor((32 - drawHeight) / 2))};
  }
  function buildDialog(original) {
    const serial = ++screenMediaDialogSerial, element = document.createElement('dialog');
    element.className = 'screen-media-dialog';
    element.setAttribute('aria-labelledby', 'screen-media-dialog-title-' + serial);
    const shell = document.createElement('div'); shell.className = 'screen-media-dialog-shell';
    const head = document.createElement('div'); head.className = 'screen-media-dialog-head';
    const heading = document.createElement('h2'); heading.id = 'screen-media-dialog-title-' + serial; heading.textContent = media.name;
    const note = document.createElement('p'); note.textContent = (role === 'splash' ? 'Splash' : 'Sleep') + ' screen layout · changes apply when saved';
    head.append(heading, note);
    const frame = document.createElement('div'); frame.className = 'screen-media-frame';
    frame.setAttribute('role', 'img'); frame.setAttribute('aria-label', 'Layout preview on the 64 by 32 Matrix');
    stageImage = document.createElement('img'); stageImage.className = 'screen-media-stage-image'; stageImage.alt = '';
    stageImage.src = lazyMediaPlaceholder;
    stageBox = document.createElement('div'); stageBox.className = 'screen-media-box';
    frame.append(stageImage, stageBox);
    const presets = document.createElement('div'); presets.className = 'screen-media-presets';
    presets.append(
      makeButton('Fill screen', () => setLayout({x: 0, y: 0, width: 64, height: 32})),
      makeButton('Centre', () => setLayout(centred(mediaPlaylistLayout(row)))),
      makeButton('Original size', () => {
        const size = {width: Math.min(64, Number(media.width) || 64), height: Math.min(32, Number(media.height) || 32)};
        setLayout({...size, ...centred({...mediaPlaylistLayout(row), ...size})});
      }));
    const fields = document.createElement('div'); fields.className = 'screen-media-fields';
    for (const [group, legendText, entries] of SCREEN_LAYOUT_FIELDS) {
      if (group === 'playback' && media.kind !== 'gif') continue;
      const fieldset = document.createElement('fieldset'), legend = document.createElement('legend');
      fieldset.dataset.layoutGroup = group; legend.textContent = legendText; fieldset.append(legend);
      for (const [field, text] of entries) {
        const source = original.querySelector('[data-media-layout="' + field + '"]');
        if (!source) continue;
        const control = source.cloneNode(true), wrapper = document.createElement('label');
        control.checked = source.checked; control.value = source.value;
        wrapper.className = control.type === 'checkbox' ? 'screen-media-check' : '';
        if (control.type === 'checkbox') wrapper.append(control, document.createTextNode(text));
        else wrapper.append(document.createTextNode(text), control);
        fieldset.append(wrapper);
      }
      if (group === 'playback') {
        const hint = document.createElement('p'); hint.className = 'screen-media-hint';
        hint.textContent = '100% plays the GIF at its native speed.';
        fieldset.append(hint);
      }
      fields.append(fieldset);
    }
    const footer = document.createElement('div'); footer.className = 'screen-media-dialog-actions';
    const reset = makeButton('Reset layout', () => {
      resetMediaPlaylistItem(original.querySelector('button'));
      for (const control of element.querySelectorAll('[data-media-layout]')) {
        const target = original.querySelector('[data-media-layout="' + control.dataset.mediaLayout + '"]');
        if (control.type === 'checkbox') control.checked = target.checked; else control.value = target.value;
      }
      for (const sibling of document.querySelectorAll('.screen-media-choice[data-row-id="' + row.dataset.itemId + '"]')) sibling.refreshScreenLayout?.();
      screenChoiceChanged(role);
    }, 'btn danger');
    const previewOnMatrix = makeButton('Preview on Matrix', () => {
      element.close(); inspect.click();
      document.querySelector('#matrix-preview')?.scrollIntoView({behavior: 'smooth', block: 'center'});
    });
    const done = makeButton('Done', () => element.close(), 'btn primary'); done.classList.add('screen-media-done');
    const end = document.createElement('div'); end.className = 'screen-media-dialog-end'; end.append(previewOnMatrix, done);
    footer.append(reset, end);
    shell.append(head, frame, presets, fields, footer); element.append(shell);
    element.addEventListener('click', event => { if (event.target === element) element.close(); });
    // Release the preview image when closed so off-screen media does not keep decoding.
    element.addEventListener('close', () => { stageImage.src = lazyMediaPlaceholder; });
    return element;
  }
  function bindRow(owner) {
    if (card.dataset.rowId === owner.dataset.itemId && dialog) return;
    row = owner; card.dataset.rowId = row.dataset.itemId; inspect.disabled = false; syncSelection();
    const original = row.closest('.module-item').querySelector('.media-playlist-editor');
    if (dialog) { if (dialog.open) dialog.close(); dialog.remove(); }
    dialog = buildDialog(original);
    for (const control of dialog.querySelectorAll('[data-media-layout]')) control.addEventListener('input', () => {
      const target = original.querySelector('[data-media-layout="' + control.dataset.mediaLayout + '"]');
      if (control.type === 'checkbox') target.checked = control.checked; else target.value = control.value;
      // Existing multi-source rows retain shared layout semantics; mirror sibling cards without rerender/focus loss.
      for (const sibling of document.querySelectorAll('.screen-media-choice[data-row-id="' + row.dataset.itemId + '"] [data-media-layout="' + control.dataset.mediaLayout + '"]')) {
        if (sibling.type === 'checkbox') sibling.checked = control.checked; else sibling.value = control.value;
      }
      for (const sibling of document.querySelectorAll('.screen-media-choice[data-row-id="' + row.dataset.itemId + '"]')) sibling.refreshScreenLayout?.();
      previewMediaItemId = row.dataset.itemId; selectEditPreviewModule(role); screenChoiceChanged(role);
    });
    card.append(dialog); refreshLayout();
  }
  card.bindScreenRow = bindRow;
  if (row) bindRow(row);
  syncSelection();
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
    card.syncScreenSelection?.();
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
