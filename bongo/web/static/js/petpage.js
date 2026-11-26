// The Pet page (/modules/pet): the Matrix's one pet. Animal, colour, name and outfits (saved with this page's own
// Save, never with Apply to Matrix), every screen that shows it, and a preview at the panel's real size.
// Pet needs, Decor and Playdates have their place here but are not built yet: they can't be pressed.
import { get } from './api.js';
import { h, icon, humanize, reducedMotion, store } from './ui.js';
import { FrameLoop } from './led.js';
import { petPicker, effectivePet } from './petpicker.js';
import { S, go, href, moduleById, styleOf, onLeave, saveConfig, path } from './app.js';

const savedPet = () => ({ kind: 'cat', ...(S.config.pet || {}) });

/** The words on this module's row in Settings > Modules: the pet's name, and that the Pet clock still uses it. A pet
 *  saved with no name has its animal's own (the dog's Biscuit), which only the Matrix's list of animals knows. */
export async function rowNote(on) {
  const saved = savedPet();
  const usable = /^[A-Za-z0-9 '.!?-]{1,12}$/.test(String(saved.name || '').trim());
  const kinds = usable || saved.kind === 'cat' ? [] : await get('/api/pets').then((r) => r.kinds).catch(() => []);   // (its animal's own name: only the Matrix knows it)
  const name = effectivePet(saved, kinds).name || 'Your pet';
  return on ? `Your pet: ${name}` : `Your pet: ${name}. The Pet clock still uses ${name}.`;
}

/** Every screen that shows the pet, from the playlist as it is in the draft (so Edit opens the right one). */
function screensShowingPet() {
  const place = moduleById('pet')?.settings?.find((f) => f.name === 'place')?.labels || {};
  const rows = [];
  (S.draft || []).forEach((e, i) => {
    if (e.module === 'pet') rows.push({ i, e, name: `Pet · ${humanize(styleOf(e))}`, note: `#${i + 1} · ${place[e.settings?.place || 'house'] || 'Home'}` });
    else if (e.module === 'clock' && styleOf(e) === 'pet') rows.push({ i, e, name: 'Clock · Pet', note: `#${i + 1} · visits the time` });
  });
  return rows.map((r) => ({ ...r, off: r.e.enabled === false }));
}

const soonCard = (title, tag, said, body) => h('section', { class: 'card stack soon', 'aria-label': title, 'aria-disabled': 'true' },
  h('div', { class: 'row between wrap gap-s' }, h('h2', { class: 'h2' }, title), h('span', { class: 'tag soon-tag' }, tag)),
  h('p', { class: 'muted small flush' }, said),
  h('div', { class: 'soon-body', 'aria-hidden': 'true', inert: true }, ...body));
const dead = (cls, text) => h('button', { type: 'button', class: cls, disabled: true, tabindex: -1 }, text);
const deadSwitch = (text) => h('button', { type: 'button', class: 'switch', 'aria-pressed': 'false', disabled: true, tabindex: -1 },
  h('span', { class: 'switch-track', 'aria-hidden': 'true' }, h('span', { class: 'switch-knob' })), h('span', { class: 'switch-label' }, text));

function comingSoon() {
  const bar = (label, pct) => [h('span', null, label), h('span', { class: 'need-bar' }, h('span', { style: `width:${pct}%` }))];
  return [
    soonCard('Pet needs', 'Coming soon',
      'Your pet will look after its own food, play, cleaning and sleep. A switch here will let you look after them yourself instead.', [
        deadSwitch('Look after my pet myself'),
        h('div', { class: 'need-bars' }, ...bar('Food', 70), ...bar('Play', 45), ...bar('Clean', 80), ...bar('Sleep', 60)),
        h('div', { class: 'row gap-s wrap' }, dead('btn', 'Feed'), dead('btn', 'Play'), dead('btn', 'Brush')),
        h('p', { class: 'tiny muted flush' }, 'The bars and buttons will appear when the switch is on.')]),
    soonCard('Decor', 'Coming soon',
      "You will be able to choose what furnishes your pet's rooms. It will show in the preview, in the room size picked there.", [
        h('div', { class: 'slot-grid' }, ...['Bed', 'Bowl', 'Toy', 'Post', 'Wallpaper', 'Floor'].map((n) => h('div', { class: 'slot' }, n)),
          h('div', { class: 'slot' }, 'Hideout', h('small', null, 'optional'))),
        h('div', { class: 'field-col' }, 'Theme', h('div', { class: 'chip-grid' },
          ...['Classic', 'Cosy', 'Modern', 'Garden', 'Reset'].map((n, k) => h('button', { type: 'button', class: 'chip', 'aria-pressed': String(!k), disabled: true, tabindex: -1 }, n)))),
        h('div', { class: 'row gap-s wrap' }, dead('btn', 'Show on the Matrix'), h('span', { class: 'tiny muted' }, 'for 30 seconds'))]),
    soonCard('Playdates', 'Coming soon',
      "Pets from the other Matrixes in your household will be able to visit yours, by invite only.", [
        deadSwitch('Accept household invites automatically'),
        h('p', { class: 'tiny muted flush' }, 'Up to 3 pets on screen at once, for 30 minutes at most. Your household’s Matrixes will be listed here.')]),
  ];
}

export async function render(view) {
  const back = S.back && S.back.to !== path() ? S.back : null;   // where this page was reached from, if from a screen's editor
  S.back = null;
  const panel = S.state?.panel || { width: 64, height: 32 };
  const stubs = (S.state?.pet_kinds?.length ? S.state.pet_kinds : ['cat']).map((k) => ({ kind: k, title: humanize(k), group: k, group_title: humanize(k), coats: [], collars: [], rooms: [] }));
  let kinds = stubs;
  let room = store.get('petRoom', 'small');
  let t0 = Date.now() / 1000, frames = 0;   // the preview's moment: it moves on a little each frame

  const heading = h('h2', { class: 'h2' }, 'Your pet');
  const edited = h('span', { class: 'pill-warn', hidden: true }, 'Not saved yet');
  const picker = petPicker(S.config.pet, kinds, () => { paintDirty(); preview.refresh?.(); paintRoom(); });
  const save = h('button', { type: 'button', class: 'btn primary' }, 'Save');
  const sameAsSaved = () => {
    const { pet } = picker.read(), was = effectivePet(savedPet(), kinds);   // (what the Matrix really has, as it works it out)
    return pet.kind === was.kind && (pet.name || '') === was.name && (pet.coat || '') === (was.coat || '')
      && (pet.collar || '') === (was.collar || '') && (pet.dress_up !== false) === was.dress_up;
  };
  function paintDirty() { edited.hidden = !known || sameAsSaved(); }   // (before the animals' list arrives, what the picker can show isn't all there is)
  save.onclick = async () => {
    const { pet, error } = picker.read();
    if (error) { picker.nameProblem(error); picker.focusName(); return; }
    save.disabled = true;
    const ok = await saveConfig({ pet }, 'Pet');
    save.disabled = false;
    if (ok) { leaving.hidden = true; paintDirty(); paintNames(); }
  };
  const leaving = h('div', { class: 'note leave-ask', role: 'alertdialog', 'aria-modal': 'true', 'aria-labelledby': 'leave-ask-text', hidden: true });
  const yours = h('section', { class: 'card stack', 'aria-label': 'Your pet' },
    h('div', { class: 'row between wrap gap-s' }, heading, edited), ...picker.nodes, leaving, h('div', { class: 'row end' }, save));
  // Going to another page (Edit, the crumbs, the menu) with the pet picked but not saved asks first, in the page
  let okToLeave = false, trigger = null, stay = null, leave = null;
  const closeAsk = (back) => { leaving.hidden = true; if (back) trigger?.focus(); };
  const ask = (ev) => {
    const link = ev.target.closest?.('a[data-route], a.crumb');
    if (!link || okToLeave || edited.hidden || ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
    ev.preventDefault(); ev.stopPropagation();
    trigger = link;
    stay = h('button', { type: 'button', class: 'btn small-btn', onclick: () => closeAsk(true) }, 'Stay');
    leave = h('button', { type: 'button', class: 'btn small-btn', onclick: () => { okToLeave = true; closeAsk(false); link.click(); } }, 'Leave without saving');
    leaving.replaceChildren(icon('info'), h('span', { class: 'grow', id: 'leave-ask-text' }, "You picked a pet but haven't saved it. Leave without saving?"), h('span', { class: 'row gap-s' }, stay, leave));
    leaving.hidden = false;
    leaving.scrollIntoView({ block: 'center' });
    stay.focus({ preventScroll: true });
  };
  const keys = (e) => {   // while the question is up: Escape is Stay, and Tab goes round its two buttons and no further
    if (leaving.hidden) return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeAsk(true); }
    else if (e.key === 'Tab') { e.preventDefault(); (document.activeElement === stay ? leave : stay).focus(); }
  };
  document.addEventListener('keydown', keys, true);
  onLeave(() => document.removeEventListener('keydown', keys, true));
  document.addEventListener('click', ask, true);
  onLeave(() => document.removeEventListener('click', ask, true));

  // -- where it appears ------------------------------------------------------------------------------------
  const whereHead = h('h2', { class: 'h2' });
  const where = () => {
    const rows = screensShowingPet();
    const wander = (S.config.overlays || []).some((o) => o.module === 'pet');
    const mini = (r) => {
      const canvas = h('canvas', { class: 'led-canvas mini', 'aria-hidden': 'true' });
      canvas.style.aspectRatio = `${panel.width} / ${panel.height}`;
      const lp = new FrameLoop(canvas, { interval: Infinity, led: () => ({ panel }),
        path: () => previewUrl({ module: r.e.module, style: styleOf(r.e), settings: r.e.settings || {} }, null, Date.now() / 1000) });
      onLeave(() => lp.stop());
      return h('span', { class: 'led-frame mini' }, canvas);
    };
    const line = (r) => h('div', { class: 'where-row' }, mini(r),
      h('span', { class: 'style-row-text grow min0' }, h('span', { class: 'strong' }, r.name, r.off ? h('span', { class: 'tag tag-gap' }, 'Off') : null), h('span', { class: 'muted small' }, r.note)),
      h('a', { class: 'btn small-btn', href: href(`/edit/${r.i}`), 'data-route': `/edit/${r.i}`, 'aria-label': `Edit ${r.name}` }, 'Edit'));
    const list = h('div', { class: 'where-list' }, ...rows.map(line),
      wander ? h('div', { class: 'where-row' }, h('span', { class: 'style-row-text grow min0' },
        h('span', { class: 'strong' }, 'Wander', h('span', { class: 'tag tag-gap' }, 'Overlay')), h('span', { class: 'muted small' }, 'Walks across your other screens')),
      ) : null,
      !rows.length && !wander ? h('p', { class: 'muted small flush' }, 'No screen shows your pet yet. Add a Pet screen to the playlist.') : null);
    const count = `${rows.length} screen${rows.length === 1 ? '' : 's'}${wander ? ' and Wander' : ''}`;
    return { list, count };
  };
  const w = where();
  const wherePhone = h('details', { class: 'card where-fold where-phone' }, h('summary', null, h('span', { class: 'h2' }, whereHead.cloneNode(true)), h('span', { class: 'muted small' }, `${w.count} ›`)), h('div', { style: 'margin-top:8px' }, where().list));
  const whereDesk = h('section', { class: 'card stack where-desk', 'aria-label': 'Where it appears' }, whereHead, w.list);
  function paintNames() {
    const n = effectivePet(savedPet(), kinds).name || 'your pet';
    whereHead.textContent = `Where ${n} appears`;
    wherePhone.querySelector('summary .h2').textContent = `Where ${n} appears`;
    whereDesk.setAttribute('aria-label', `Where ${n} appears`);
  }

  // -- preview ----------------------------------------------------------------------------------------------
  const canvas = h('canvas', { class: 'led-canvas', role: 'img', 'aria-label': 'Preview of your pet at home' });
  canvas.style.aspectRatio = `${panel.width} / ${panel.height}`;
  const err = h('p', { class: 'error-box small', role: 'status', hidden: true });
  const seg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Room size' });
  const roomNote = h('p', { class: 'tiny soft flush room-note', 'aria-live': 'polite' });
  const offered = () => (picker.entry().rooms?.length ? picker.entry().rooms : []);
  const roomNow = () => offered().find((r) => r.size === room) || offered()[0] || null;
  function previewUrl(entry, pet, t = t0 + (frames++) * 0.12) {
    const q = new URLSearchParams({ module: entry.module, style: entry.style || '', settings: JSON.stringify(entry.settings || {}), t: t.toFixed(3) });
    if (pet) q.set('pet', JSON.stringify(pet));
    return `/api/preview.png?${q}`;
  }
  let fails = 0;
  let known = false;   // the Matrix's list of animals has arrived (or can't)
  const preview = new FrameLoop(canvas, {
    interval: reducedMotion() ? Infinity : 120,
    path: () => previewUrl({ module: 'pet', style: 'life', settings: { place: roomNow()?.place || 'house' } }, picker.read().pet),
    led: () => ({ panel }),
    // one frame that fails is nothing to see: the last good one stays and the loop tries again; only a run of them is said
    onError: (x) => { fails = x ? fails + 1 : 0; err.hidden = fails < 3 || x?.status === 401; err.textContent = fails >= 3 ? `Preview failed: ${x.message}` : ''; },
  });
  onLeave(() => preview.stop());
  function paintRoom() {
    const sizes = offered(), now = roomNow();
    seg.replaceChildren(...sizes.map((r) => h('button', { type: 'button', 'aria-pressed': String(r === now), onclick: () => { room = r.size; store.set('petRoom', room); paintRoom(); preview.refresh(); } },
      r.label, h('small', null, `${r.width}×${r.height}`))));
    seg.hidden = !sizes.length;
    if (!now) { roomNote.textContent = ''; return; }
    const smaller = now.width < panel.width || now.height < panel.height, bigger = now.width > panel.width || now.height > panel.height;
    roomNote.textContent = bigger ? 'This room is bigger than the panel: the view follows your pet around it.'
      : smaller ? `A ${now.label.toLowerCase()} room (${now.width}×${now.height}) is smaller than this ${panel.width}×${panel.height} panel, so it fills only part of it for now.` : '';
  }
  const aside = h('section', { class: 'editor-preview pet-preview', 'aria-label': 'Preview', style: `--ratio:${panel.width / panel.height}` },
    h('div', { class: 'card stack' },
      h('div', { class: 'row between wrap preview-head' }, h('h2', { class: 'h2' }, 'Preview'), h('span', { class: 'muted tiny mono' }, `Panel ${panel.width}×${panel.height}`)),
      h('div', { class: 'led-frame' }, canvas), err,
      h('div', { class: 'field-head-row' }, h('span', null, 'Room size'), h('span', { class: 'tiny muted' }, "the pet's home, not the panel")),
      seg, roomNote,
      h('p', { class: 'muted tiny flush preview-why' }, "Drawn by the Matrix itself, with the pet you've picked, before you save.")));

  // -- the page ---------------------------------------------------------------------------------------------
  const crumbs = h('div', { class: 'row gap-s wrap' },
    h('a', { href: href('/settings'), 'data-route': '/settings', class: 'crumb' }, icon('left'), 'Settings'),
    h('span', { class: 'crumb-sep', 'aria-hidden': 'true' }, '/'),
    h('a', { class: 'crumb', href: href('/settings') + '#modules', onclick: (ev) => { ev.preventDefault(); go('/settings'); setTimeout(() => document.getElementById('modules')?.scrollIntoView(), 50); } }, 'Modules'),
    h('span', { class: 'crumb-sep', 'aria-hidden': 'true' }, '/'),
    h('h1', { class: 'h1 small-h' }, 'Pet'));
  const comeBack = back ? h('a', { class: 'btn ghost small-btn', href: href(back.to), 'data-route': back.to, onclick: () => { S.kept = true; } }, icon('left'), back.label) : null;
  view.append(h('div', { class: 'editor-head' }, crumbs, comeBack),
    h('p', { class: 'muted flush', style: 'margin:-8px 0 16px' }, 'One pet per Matrix. Everything here is shared by every Pet screen, the Pet clock and the Wander overlay. Each part saves with its own Save button.'),
    h('div', { class: 'editor-grid' },
      h('div', { class: 'editor-form stack' }, yours, h('div', null, whereDesk, wherePhone), ...comingSoon()),
      aside));
  paintNames(); paintRoom(); paintDirty();
  get('/api/pets').then((r) => { if (r.kinds?.length) { kinds = r.kinds; picker.setKinds(kinds); paintRoom(); paintNames(); } })
    .catch(() => { /* an older Matrix: names only, no pictures or room sizes */ })
    .finally(() => { known = true; paintDirty(); });
}
