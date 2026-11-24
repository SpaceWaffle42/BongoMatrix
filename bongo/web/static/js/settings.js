// Settings: safe area (with a big LED preview), device, splash, sleep, paired devices.
import { api, get, del, post, put, uploadMedia } from './api.js';
import { h, icon, toast, fail, clone, same, clamp, humanize, ago, toggle } from './ui.js';
import { FrameLoop } from './led.js';
import { mediaThumb, mediaTitle, previewButton } from './mediaview.js';
import { S, onLeave, go, saveConfig, moduleById } from './app.js';
import { helpButton, creditLine } from './help.js';
import { placePicker } from './editor.js';
import { lookEditor, keepState, comingUpCard, icons, NAMES as COLOUR_NAMES } from './calendarlook.js';
import { importBox, importedMore, addons, paintStatus } from './modimport.js';
import { moduleCards } from './modsettings.js';
import { DEMO } from './api.js';
import * as deeplink from './deeplink.js';
import { brightnessBox } from './daylight.js';
import { backupCard } from './backup.js';
import { storageBox } from './storage.js';

/** The cards, by a name that never changes (their order is saved on the Matrix: config ``settings_layout``). */
const CARDS = [['safe_area', () => safeAreaCard()], ['device', () => deviceCard()], ['modules', () => modulesCard()],
  ['location', () => locationCard()], ['wifi', () => wifiCard()], ['pet', () => petCard()], ['calendars', () => calendarsCard()],
  ['spotify', () => spotifyCard()], ['weather', () => weatherCard()], ['update', () => updateCard()], ['devices', () => devicesCard()], ['sleep', () => sleepCard()],
  ['splash', () => splashCard()], ['media', () => mediaCard()], ['coming_up', () => comingUpCard(Object.keys(COLOUR_NAMES))],
  ['backup', () => backupCard()]];
/** As it was before cards could be moved: the safe area across the top, two columns, Coming up across the bottom. */
const DEFAULT_LAYOUT = { top: ['safe_area'], left: ['device', 'modules', 'location', 'wifi', 'pet', 'calendars', 'spotify', 'weather', 'update'],
  right: ['devices', 'sleep', 'splash', 'media', 'backup'], full: ['coming_up'] };
const ZONES = ['top', 'left', 'right', 'full'];
const ZONE_NAMES = { top: 'Across the top', left: 'Left column', right: 'Right column', full: 'Across the bottom' };

/** The saved layout, made whole: every card exactly once (a card added in an update goes where it is by default). */
export function layoutOf(saved) {
  const ids = CARDS.map(([id]) => id), out = { top: [], left: [], right: [], full: [] }, seen = new Set();
  for (const z of ZONES) for (const id of (Array.isArray(saved?.[z]) ? saved[z] : [])) if (ids.includes(id) && !seen.has(id)) { out[z].push(id); seen.add(id); }
  for (const z of ZONES) for (const id of DEFAULT_LAYOUT[z]) if (!seen.has(id)) { out[z].push(id); seen.add(id); }
  return out;
}

export function render(view) {
  const built = Object.fromEntries(CARDS.map(([id, make]) => [id, make()]));
  const holders = { top: h('div', { class: 'stack gap-20' }), left: h('div', { class: 'stack gap-20 min0' }),
    right: h('div', { class: 'stack gap-20 min0' }), full: h('div', { class: 'stack gap-20' }) };
  const cards = [];                                   // the phone's one column, in the order shown (the jump bar follows it)
  const bar = jumpBar(cards);
  const place = (layout) => {                         // the same cards, moved: nothing typed in them is lost
    for (const z of ZONES) holders[z].replaceChildren(...layout[z].map((id) => built[id]));
    holders.top.hidden = !layout.top.length; holders.full.hidden = !layout.full.length;   // an empty place adds no gap
    bar.follow(ZONES.flatMap((z) => layout[z]).flatMap((id) => built[id].jump?.() || [built[id]]));   // (Modules: its card, then each imported module's own)
  };
  place(layoutOf(S.config.settings_layout));
  const page = h('div', { class: 'view-part' },
    h('div', { class: 'page-head' }, h('div', { class: 'row between wrap gap-s' }, h('h1', { class: 'h1' }, 'Settings'),
      h('button', { type: 'button', class: 'btn ghost small-btn', onclick: () => rearrange(view, page, built, place) }, icon('grip'), 'Rearrange')),
    h('p', { class: 'muted flush' }, 'Each card saves on its own.')),
    bar, holders.top, h('div', { class: 'settings-grid' }, holders.left, holders.right), holders.full);
  view.append(page);
}

/** Rearrange: the cards as a short list of names in four places. Drag one by its handle, or use its arrows
 *  (they carry on into the next place at either end); Save puts the page that way on every phone and computer.
 *  The page itself is only hidden meanwhile, so anything typed in a card is kept. */
function rearrange(view, page, built, place) {
  const name = (id) => built[id].getAttribute('aria-label') || id;
  const shown = (id) => !built[id].hidden;            // a card this Matrix doesn't show (Wi-Fi) isn't listed, but keeps its place
  let layout = layoutOf(S.config.settings_layout), reset = false;
  const said = h('p', { class: 'sr-only', 'aria-live': 'polite' });
  const zones = {};
  const where = (id) => ZONES.find((z) => layout[z].includes(id));
  const tell = (id) => { const z = where(id), list = layout[z].filter(shown); said.textContent = `${name(id)}: ${ZONE_NAMES[z]}, ${list.indexOf(id) + 1} of ${list.length}`; };
  const focusOn = (id, act) => {
    const item = zones[where(id)].querySelector(`[data-id="${id}"]`);
    const want = item?.querySelector(`[data-act="${act}"]`);
    (want && !want.disabled ? want : item?.querySelector('[data-act]:not(:disabled)'))?.focus();
  };
  const order = () => ZONES.flatMap((z) => layout[z].filter(shown).map((id) => [z, id]));
  const step = (id, by) => {                          // one place up or down, into the next zone at either end
    const z = where(id), list = layout[z].filter(shown), at = list.indexOf(id);
    const zi = ZONES.indexOf(z);
    layout[z] = layout[z].filter((x) => x !== id);
    if (by < 0 && at > 0) layout[z].splice(layout[z].indexOf(list[at - 1]), 0, id);
    else if (by > 0 && at < list.length - 1) layout[z].splice(layout[z].indexOf(list[at + 1]) + 1, 0, id);
    else if (by < 0) layout[ZONES[zi - 1]].push(id);
    else layout[ZONES[zi + 1]].unshift(id);
    reset = false; paint(); tell(id); focusOn(id, by < 0 ? 'up' : 'down');
  };
  const row = (id) => {
    const all = order(), k = all.findIndex(([, x]) => x === id);
    const handle = h('button', { type: 'button', class: 'handle', tabindex: '-1', 'aria-hidden': 'true', title: 'Drag to move' }, icon('grip'));
    const item = h('li', { class: 'arrange-item', 'data-id': id }, handle, h('span', { class: 'strong grow min0' }, name(id)),
      h('button', { type: 'button', class: 'mini-btn', 'data-act': 'up', 'aria-label': `Move ${name(id)} up`, disabled: k === 0, onclick: () => step(id, -1) }, '↑'),
      h('button', { type: 'button', class: 'mini-btn', 'data-act': 'down', 'aria-label': `Move ${name(id)} down`, disabled: k === all.length - 1, onclick: () => step(id, 1) }, '↓'));
    handle.addEventListener('pointerdown', (ev) => drag(ev, item, id));
    return item;
  };
  const paint = () => {
    for (const z of ZONES) {
      const ids = layout[z].filter(shown);
      zones[z].replaceChildren(...ids.map(row), ...(ids.length ? [] : [h('li', { class: 'arrange-empty muted small' }, 'Drag a card here')]));
    }
  };
  const read = () => {                                 // the lists as they stand, with the hidden cards kept where they were
    const now = Object.fromEntries(ZONES.map((z) => [z, [...zones[z].querySelectorAll('.arrange-item')].map((x) => x.dataset.id)]));
    for (const z of ZONES) layout[z].forEach((id, k) => { if (!shown(id)) now[z].splice(Math.min(k, now[z].length), 0, id); });
    return now;
  };
  function drag(ev, item, id) {   // like the playlist: the item follows the pointer from place to place
    if (ev.button !== 0) return;
    ev.preventDefault();
    const before = JSON.stringify(layout);
    item.classList.add('dragging'); document.body.classList.add('drag-active');
    let raf = 0, x = ev.clientX, y = ev.clientY;
    const put = () => {
      const zone = ZONES.map((z) => zones[z]).find((ul) => { const r = ul.getBoundingClientRect(); return x >= r.left && x <= r.right && y >= r.top - 24 && y <= r.bottom + 24; });
      if (!zone) return;
      const others = [...zone.querySelectorAll('.arrange-item')].filter((o) => o !== item);
      const next = others.find((o) => { const r = o.getBoundingClientRect(); return y < r.top + r.height / 2; });
      zone.querySelector('.arrange-empty')?.remove();
      if (item.parentNode !== zone || item.nextSibling !== (next || null)) zone.insertBefore(item, next || null);
    };
    const edge = () => {                               // near the top or bottom: scroll, and keep placing under the finger
      const bottom = document.getElementById('tabbar')?.getBoundingClientRect().top || innerHeight;   // above the phone's tab bar
      const by = y < 80 ? -12 : y > Math.min(innerHeight, bottom) - 80 ? 12 : 0;
      if (by) { window.scrollBy(0, by); put(); }
      raf = requestAnimationFrame(edge);
    };
    raf = requestAnimationFrame(edge);
    const onMove = (e) => { if (e.pointerId === ev.pointerId) { x = e.clientX; y = e.clientY; put(); } };
    const stop = (cancelled) => {
      cancelAnimationFrame(raf);
      window.removeEventListener('pointermove', onMove); window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', up); window.removeEventListener('keydown', esc, true);
      item.classList.remove('dragging'); document.body.classList.remove('drag-active');
      layout = cancelled ? JSON.parse(before) : read();
      if (!cancelled) reset = false;
      paint(); tell(id); focusOn(id, 'down');
    };
    const up = () => stop(false);
    const esc = (e) => { if (e.key === 'Escape') { e.preventDefault(); stop(true); } };
    window.addEventListener('pointermove', onMove); window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', up); window.addEventListener('keydown', esc, true);
  }
  for (const z of ZONES) zones[z] = h('ul', { class: 'arrange-zone', 'aria-label': ZONE_NAMES[z] });
  const box = h('div', { class: 'view-part' });
  const close = () => { box.remove(); page.hidden = false; };
  const save = h('button', { type: 'button', class: 'btn primary' }, 'Save layout');
  save.onclick = async () => {
    const keep = reset ? null : layout;               // the usual order: nothing saved, so it follows future updates
    if (await saveConfig({ settings_layout: keep }, 'Settings layout')) { place(layoutOf(keep)); close(); window.scrollTo({ top: 0 }); }
  };
  const usual = h('button', { type: 'button', class: 'btn ghost', onclick: () => { layout = layoutOf(null); reset = true; paint(); said.textContent = 'Back to the usual order: press Save layout to keep it'; } }, 'Usual order');
  const cancel = h('button', { type: 'button', class: 'btn ghost', onclick: () => { close(); window.scrollTo({ top: 0 }); } }, 'Cancel');
  const zoneCard = (z, cls, note) => h('section', { class: `card stack gap-s ${cls}` }, h('h2', { class: 'h2' }, ZONE_NAMES[z]), note ? h('p', { class: 'tiny muted flush' }, note) : null, zones[z]);
  paint();
  box.append(
    h('div', { class: 'page-head' }, h('h1', { class: 'h1' }, 'Rearrange Settings'),
      h('p', { class: 'muted flush' }, 'Drag a card by its handle, or use the arrows. On a phone the page is one column, in this order: top, left, right, bottom.')),
    h('div', { class: 'arrange-grid' },
      zoneCard('top', 'arrange-full', 'Full width on a computer.'), zoneCard('left', ''), zoneCard('right', ''),
      zoneCard('full', 'arrange-full', 'Full width on a computer: for wide cards like Coming up.')),
    h('div', { class: 'row end gap-s wrap', style: 'margin-top:16px' }, usual, cancel, save), said);
  page.hidden = true;
  view.append(box);
  window.scrollTo({ top: 0 });
}

/** On a phone Settings is one long column of cards: a row of their names stays under the top bar.
 *  Tap one to scroll to its card; the row marks the card you are reading. */
function jumpBar(cards) {
  const top = () => document.getElementById('topbar').offsetHeight;
  const bar = h('nav', { class: 'jump-bar', 'aria-label': 'Jump to a section' });
  const chip = (card) => h('button', { type: 'button', class: 'chip', onclick: () => {
    const y = card.getBoundingClientRect().top + window.scrollY - top() - bar.offsetHeight - 8;
    window.scrollTo({ top: y, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  } }, card.getAttribute('aria-label'));
  const made = new Map();
  const chips = cards.map((card) => { made.set(card, chip(card)); return made.get(card); });
  bar.append(...chips);
  let at = -1;
  /** The cards in a new order (Settings > Rearrange): the chips follow. */
  bar.follow = (order) => {
    cards.splice(0, cards.length, ...order);
    chips.splice(0, chips.length, ...order.map((card) => { if (!made.has(card)) made.set(card, chip(card)); return made.get(card); }));
    bar.replaceChildren(...chips);
    at = -1; requestAnimationFrame(mark);
  };
  const mark = () => {
    if (!bar.offsetParent) return;   // wide screens have no bar
    bar.style.top = `${top()}px`;
    chips.forEach((c, k) => { c.hidden = cards[k].hidden; });   // a card the Matrix doesn't have (Wi-Fi) has no chip
    const line = top() + bar.offsetHeight + 24;
    const shown = cards.map((c, k) => [c, k]).filter(([c]) => !c.hidden);
    if (!shown.length) return;
    let now = shown[0][1];
    for (const [c, k] of shown) if (c.getBoundingClientRect().top <= line) now = k;
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2) now = shown[shown.length - 1][1];   // the last card may never reach the top
    if (now === at) return;
    at = now;
    chips.forEach((c, k) => c.setAttribute('aria-current', String(k === now)));
    bar.scrollTo({ left: chips[now].offsetLeft - (bar.clientWidth - chips[now].offsetWidth) / 2, behavior: 'smooth' });
  };
  window.addEventListener('scroll', mark, { passive: true });
  window.addEventListener('resize', mark);
  const tick = setInterval(mark, 1000);   // cards load and change height after the page is drawn
  onLeave(() => { window.removeEventListener('scroll', mark); window.removeEventListener('resize', mark); clearInterval(tick); });
  requestAnimationFrame(mark);
  return bar;
}

// The whole display: chained panels side by side, parallel chains stacked (as bongo/safe_area.py sizes frames).
function displaySize(p) {
  return { width: (p?.cols || 64) * (p?.chain_length || 1), height: (p?.rows || 32) * (p?.parallel || 1) };
}

function safeAreaCard() {
  const panel = displaySize(S.config.panel);
  let saved = { top: 0, right: 0, bottom: 0, left: 0, ...S.config.safe_area };
  const sa = clone(saved);
  const canvas = h('canvas', { class: 'led-canvas', role: 'img', 'aria-label': 'Live panel with the hidden border tinted red and the visible area outlined' });
  const loop = new FrameLoop(canvas, { path: () => '/api/frame.png', interval: 250,
    led: () => ({ safeArea: sa, panel, tintHidden: true, showSafe: true }) });
  onLeave(() => loop.stop());
  const size = h('span', { class: 'mono strong' });
  const saveBtn = h('button', { type: 'button', class: 'btn primary' }, 'Save safe area');
  const revertBtn = h('button', { type: 'button', class: 'btn ghost' }, 'Revert');
  const inputs = {};
  const limit = (side) => (side === 'top' || side === 'bottom'
    ? panel.height - 8 - sa[side === 'top' ? 'bottom' : 'top'] : panel.width - 8 - sa[side === 'left' ? 'right' : 'left']);
  const paint = () => {
    for (const [side, inp] of Object.entries(inputs)) { inp.value = sa[side]; inp.max = limit(side); }
    size.textContent = `${panel.width - sa.left - sa.right} × ${panel.height - sa.top - sa.bottom}`;
    const dirty = !same(sa, saved);
    saveBtn.disabled = !dirty; revertBtn.hidden = !dirty;
    loop.redraw();
  };
  const setSide = (side, v) => { sa[side] = clamp(Math.round(Number(v) || 0), 0, Math.max(0, limit(side))); paint(); };
  const stepper = (side, short) => {
    const inp = h('input', { class: 'input num center', type: 'number', id: `sa-${side}`, min: 0, step: 1, inputmode: 'numeric' });
    inp.addEventListener('change', () => setSide(side, inp.value));
    inputs[side] = inp;
    return h('div', { class: 'stepper' }, h('label', { for: `sa-${side}` }, h('span', { class: 'mono strong' }, short), ` ${humanize(side)}`),
      h('div', { class: 'stepper-row' },
        h('button', { type: 'button', class: 'btn icon-btn', 'aria-label': `Less ${side} margin`, onclick: () => setSide(side, sa[side] - 1) }, '−'),
        inp,
        h('button', { type: 'button', class: 'btn icon-btn', 'aria-label': `More ${side} margin`, onclick: () => setSide(side, sa[side] + 1) }, '+')));
  };
  saveBtn.onclick = async () => { if (await saveConfig({ safe_area: clone(sa) }, 'Safe area')) { saved = clone(sa); paint(); toast('Safe area applied to the panel.', 'success', 4000); } };
  revertBtn.onclick = () => { Object.assign(sa, saved); paint(); };
  const el = h('section', { class: 'card stack span-2', 'aria-label': 'Safe area' },
    h('div', { class: 'row between wrap' },
      h('div', null, h('h2', { class: 'h2' }, 'Safe area'), h('p', { class: 'muted small flush' }, 'If a frame or bezel covers the edge of the panel, mask those LEDs off. Modules then draw inside the amber outline.')),
      h('div', { class: 'small soft' }, 'Visible ', size, ' LEDs')),
    h('div', { class: 'led-frame' }, canvas),
    h('div', { class: 'legend-row small muted' }, h('span', { class: 'key key-hidden' }), 'Hidden LEDs', h('span', { class: 'key key-safe' }), 'Visible area'),
    h('div', { class: 'stepper-grid' }, stepper('top', 'T'), stepper('right', 'R'), stepper('bottom', 'B'), stepper('left', 'L')),
    h('div', { class: 'row gap-s end' }, revertBtn, saveBtn));
  paint();
  return el;
}

function deviceCard() {
  const c = S.config;
  const name = h('input', { class: 'input', id: 'set-name', maxlength: 40, value: c.name || S.state?.name || 'Bongo Matrix' });
  const tz = h('input', { class: 'input', id: 'set-tz', value: c.timezone || '', placeholder: 'Europe/London', list: 'tz-list', spellcheck: 'false', autocomplete: 'off' });
  let zones = [];
  try { zones = Intl.supportedValuesOf('timeZone'); } catch { /* older browser: free text */ }
  const here = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone; } catch { return ''; } })();
  const bright = brightnessBox(c);   // the daytime brightness, and Follow daylight (js/daylight.js)
  // the name it answers to on the network, as the Matrix makes it (bongo/netname.py host_name)
  const net = S.state?.network || {};
  const hostOf = (v) => {
    const t = v.trim();
    const slug = t.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 63).replace(/-+$/, '');
    return !t || t === (net.default_name || 'Bongo Matrix') || !slug || slug === 'matrix' ? `matrix-${net.suffix || '0000'}` : slug;
  };
  const host = h('span', { class: 'mono strong' });
  const where = h('p', { class: 'muted tiny flush' }, 'On your network: ', host, net.address ? ` · ${net.address}` : '');
  const showHost = () => { host.textContent = `${hostOf(name.value)}.local`; };
  name.addEventListener('input', showHost); showHost();
  const hubPic = h('input', { type: 'checkbox', id: 'set-hub-pic', checked: c.web?.hub_picture !== false });
  const notice = h('input', { type: 'checkbox', id: 'set-power-notice', checked: c.power_notice === true });
  // this Matrix's logo on the hub: the same blocks in one colour, so each Matrix is easy to tell apart
  const BADGES = ['blue', 'red', 'tan', 'pink', 'yellow', 'green'];
  const auto = BADGES.includes(net.auto_badge) ? net.auto_badge : null;
  const badge = h('select', { class: 'input', id: 'set-badge' }, h('option', { value: '' }, auto ? `Chosen for you (${auto[0].toUpperCase() + auto.slice(1)})` : 'Chosen for you'), BADGES.map((b) => h('option', { value: b, selected: c.web?.badge === b }, b[0].toUpperCase() + b.slice(1))));
  const badgeLook = h('img', { alt: '', width: 32, height: 32, onerror: (e) => { e.target.hidden = true; } });
  const showBadge = () => { const b = badge.value || auto; badgeLook.hidden = !b; if (b) badgeLook.src = `logo.png?size=32&colour=${b}`; };
  badge.addEventListener('change', showBadge); showBadge();
  const save = h('button', { type: 'button', class: 'btn primary' }, 'Save');
  save.onclick = () => {
    if (tz.value && zones.length && !zones.includes(tz.value)) { toast(`“${tz.value}” isn't a time zone this browser knows. Use a name like Europe/London.`, 'error'); tz.focus(); return; }
    const { brightness, daylight } = bright.value();
    saveConfig({ name: name.value.trim() || 'Bongo Matrix', timezone: tz.value.trim() || null, daylight,
      panel: { ...c.panel, brightness }, web: { hub_picture: hubPic.checked, badge: badge.value || null }, power_notice: notice.checked }, 'Device settings');
  };
  return h('section', { class: 'card stack', 'aria-label': 'Device' }, h('h2', { class: 'h2' }, 'Device'),
    h('label', { class: 'field-col', for: 'set-name' }, 'Display name', name, where),
    h('label', { class: 'field-col', for: 'set-tz' }, 'Time zone', tz,
      here && here !== tz.value ? h('button', { type: 'button', class: 'link small left', onclick: () => { tz.value = here; } }, `Use this browser's: ${here}`) : null),
    h('datalist', { id: 'tz-list' }, zones.map((z) => h('option', { value: z }))),
    bright.el,
    h('p', { class: 'muted tiny flush' }, `${displaySize(c.panel).width}×${displaySize(c.panel).height} panel · ${c.panel?.hardware_mapping || ''}`),
    h('label', { class: 'row gap-s', for: 'set-hub-pic' }, hubPic, h('span', null, 'Show what it\'s playing on the hub')),
    h('p', { class: 'muted tiny flush' }, 'The hub (matrix.local) shows a live picture of each Matrix to anyone on this Wi-Fi, including pushed messages. Its name and what\'s playing always show.'),
    h('label', { class: 'field-col', for: 'set-badge' }, "This Matrix's logo colour on the hub", h('span', { class: 'badge-pick' }, badge, badgeLook)),
    storageBox(S.state?.storage, S.state?.last_start),
    h('label', { class: 'row gap-s', for: 'set-power-notice' }, notice, h('span', null, 'Say when it comes back after losing power')),
    h('p', { class: 'muted tiny flush' }, 'Shows "Back after a power cut" on the panel for 10 seconds when the Matrix starts again after losing power. '
      + "Off by default: if it's switched off at the wall every night, you'd see it every morning. Logs notes it either way."),
    h('div', { class: 'row end' }, save),
    panelTimingBox(),
    restartBox());
}

/** Advanced: how the panel is driven. Both settings are read once when the display starts, so a change waits
 *  for a restart; saving says so. */
function panelTimingBox() {
  const p = S.config.panel || {};
  const SLOW = { 0: '0 · none', 1: '1 · Pi Zero, Pi 2', 2: '2 · Pi Zero 2, Pi 3', 3: '3', 4: '4 · Pi 4', 5: '5' };
  const slow = h('select', { class: 'input', id: 'set-slowdown' },
    Object.entries(SLOW).map(([v, label]) => h('option', { value: v, selected: Number(v) === (p.gpio_slowdown ?? 4) ? '' : null }, label)));
  const limit = h('input', { class: 'input', id: 'set-refresh', type: 'number', inputmode: 'numeric', min: 0, max: 1000, step: 10,
    value: p.limit_refresh || '', placeholder: 'No limit' });
  const note = h('p', { class: 'small flush warn', role: 'status', hidden: true });
  const save = h('button', { type: 'button', class: 'btn primary' }, 'Save panel timing');
  save.onclick = async () => {
    const hz = limit.value.trim() === '' ? 0 : Number(limit.value);
    if (!Number.isInteger(hz) || hz < 0 || hz > 1000) { toast('Refresh rate limit: a whole number from 0 to 1000 (0 or empty for no limit).', 'error'); limit.focus(); return; }
    const was = S.config.panel || {};
    const changed = Number(slow.value) !== (was.gpio_slowdown ?? 4) || hz !== (was.limit_refresh || 0);
    save.disabled = true;
    const ok = await saveConfig({ panel: { ...was, gpio_slowdown: Number(slow.value), limit_refresh: hz } }, 'Panel timing');
    save.disabled = false;
    if (ok && changed) {
      note.textContent = 'Saved. These settings need a restart to take effect: open Restart below and choose "Restart the display".';
      note.hidden = false;
    }
  };
  return h('details', { class: 'item-more' },
    h('summary', null, 'Advanced: panel timing'),
    h('div', { class: 'stack-s' },
      h('p', { class: 'muted tiny flush' }, 'For a panel that flickers or shows garbage. Changes here need a restart of the display to take effect.'),
      h('label', { class: 'field-col', for: 'set-slowdown' }, 'GPIO slowdown', slow),
      h('p', { class: 'muted tiny flush' }, 'How much the Matrix slows its signals for the panel. Too high and the panel refreshes slowly, which shows as flicker; too low and it shows garbage. Go down one step at a time.'),
      h('label', { class: 'field-col', for: 'set-refresh' }, 'Refresh rate limit (Hz)', limit),
      h('p', { class: 'muted tiny flush' }, 'Holds the panel to a steady refresh rate. Leave empty for no limit. If the flicker is uneven, try 120, then lower.'),
      note,
      h('div', { class: 'row end' }, save)));
}

/** Every module, with an On/Off switch like the playlist's. Off: its screens are skipped in the playlist and
 *  overlays and it isn't offered in Add a module; its screens and settings are kept for switching back on.
 *  Splash, sleep and "Show on the Matrix" have their own switches and keep working. */
function modulesCard() {
  const uses = (id) => [...(S.config.playlist || []), ...(S.config.overlays || [])].filter((e) => e.module === id).length;
  const screens = (n) => `${n} screen${n === 1 ? '' : 's'}`;
  const note = (m, on) => {
    const n = uses(m.id);
    const background = imported[m.id]?.background;   // an imported module that keeps working with none of its screens showing
    if (!m.default_style) return on ? 'On: it works in the background (it has no screen)' : 'Off: not running';
    if (on) return (n ? `Used by ${screens(n)}` : 'Not in the playlist') + (background ? ', and working in the background' : '');
    return (n ? `Off: hidden from Add a module, its ${screens(n)} skipped` : 'Off: hidden from Add a module') + (background ? ', not running' : '');
  };
  let busy = false;   // one save at a time, so two quick taps can't undo each other
  let imported = {};  // id -> what /api/addons says of it
  const list = h('div', { class: 'module-list' });
  const loose = h('p', { class: 'tiny warn flush', hidden: true },
    "Imported modules aren't kept apart on this computer (it isn't a Matrix): the sites they list aren't enforced here.");
  const row = (m) => {
    const on = !(S.config.modules_off || []).includes(m.id);
    const status = h('span', { class: 'muted tiny' }, note(m, on));
    const r = h('div', { class: `module-row${on ? '' : ' off'}`, 'data-module': m.id });
    const sw = toggle(on, on ? 'On' : 'Off', async (next) => {
      if (busy) { sw.setAttribute('aria-pressed', String(!next)); return; }
      busy = true;
      const all = [...list.querySelectorAll('.switch')];
      all.forEach((b) => b.setAttribute('aria-disabled', 'true'));   // not disabled: that would drop the keyboard's focus
      const off = new Set(S.config.modules_off || []);
      if (next) off.delete(m.id); else off.add(m.id);
      const label = sw.querySelector('.switch-label');
      label.textContent = next ? 'On' : 'Off';
      const saved = await saveConfig({ modules_off: [...off].sort() }, next ? `${m.name} on` : `${m.name} off`);
      all.forEach((b) => b.removeAttribute('aria-disabled'));
      busy = false;
      const now = saved ? next : !next;     // the Matrix said no (say, it was the last one on): put the switch back
      sw.setAttribute('aria-pressed', String(now));
      label.textContent = now ? 'On' : 'Off';
      r.classList.toggle('off', !now);
      status.textContent = note(m, now);
      if (saved && m.settings_card) own.refresh([m.id]);   // its own card says whether it's running
      if (saved && imported[m.id]?.background) { const line = r.querySelector('.module-status'); if (line) paintStatus(line, null); }
    });
    sw.setAttribute('aria-label', m.name);
    const info = imported[m.id];
    r.append(sw, h('div', { class: 'module-text' }, h('span', { class: 'strong' }, m.name, m.proposed ? h('span', { class: 'tag' }, 'Proposed') : null,
      m.imported ? h('span', { class: 'tag amber' }, 'Imported') : null),
    m.description ? h('span', { class: 'muted small' }, m.description) : null, status,
    ...(m.imported && info ? importedMore(m, info, changed, (id, name, opts) => importer.update(id, name, opts)) : [])));
    return r;
  };
  const draw = () => {
    const again = keepState(list);
    list.replaceChildren(...(S.modules || []).map(row));
    again();
  };
  const show = (res) => {
    imported = Object.fromEntries(res.modules.map((i) => [i.id, i]));
    loose.hidden = res.enforced !== false || !res.modules.length;
    draw();
  };
  function changed(res, removed) {
    if (removed) S.modules = (S.modules || []).filter((m) => m.id !== removed);
    show(res);
    if (removed) { own.refresh(); importer.button.focus(); }   // its row has gone: focus somewhere sensible
  }
  const own = moduleCards();   // a card of its own for each imported module with settings of its own (js/modsettings.js)
  const importer = importBox(async (id) => {
    try { S.modules = await get('/api/modules'); } catch { /* the list below catches up on the next load */ }
    own.refresh([id]);          // (installed or updated: its card is made again from what this version declares)
    show(await addons(true));
    const added = list.querySelector(`[data-module="${CSS.escape(id)}"]`);
    if (added) { added.classList.add('flash'); added.querySelector('.switch')?.focus(); added.scrollIntoView({ block: 'nearest' }); }
  });
  draw();
  addons().then(show);
  // the status lines of modules working in the background, kept fresh (only those lines: nothing else is redrawn)
  const fresh = setInterval(async () => {
    if (document.hidden || !Object.values(imported).some((i) => i.background)) return;
    const res = await addons(true);
    for (const i of res.modules) {
      const line = list.querySelector(`[data-module="${CSS.escape(i.id)}"] .module-status`);
      if (line) paintStatus(line, (S.config.modules_off || []).includes(i.id) ? null : i.status);
    }
  }, 10000);
  onLeave(() => clearInterval(fresh));
  const card = h('section', { class: 'card stack', 'aria-label': 'Modules', id: 'modules' },
    h('div', { class: 'row between wrap gap-s' }, h('h2', { class: 'h2' }, 'Modules'), importer.button),
    h('p', { class: 'muted small flush' }, "Switch a module off to leave it out: its screens are skipped in the playlist and overlays, and it isn't offered in Add a module. Its screens and settings are kept for when you switch it back on. Splash, sleep and Show on the Matrix have their own switches."),
    importer.panel, loose, list);
  // one place on the page (and in Rearrange) for Modules and the cards of the imported modules under it
  const all = h('div', { class: 'stack gap-20 min0', 'aria-label': 'Modules' }, card, own.box);
  all.jump = () => [card, ...own.cards()];   // ...each with its own name in the phone's jump bar
  return all;
}

/** Where the Matrix is: every weather and sky screen uses it, unless one is given its own place. */
function locationCard() {
  const home = S.config.location;
  let pick = null;
  const save = h('button', { type: 'button', class: 'btn primary', disabled: true }, 'Save');
  const picker = placePicker('set-location', home?.name || '', () => {}, (p) => { pick = p; save.disabled = false; });
  const says = (where) => (where ? `Weather, sunrise and moon screens show ${where.name}. Give one its own place in its settings.`
    : 'Not set yet: weather and sky screens use their own place (London unless changed). Pick where the Matrix is and they all use it.');
  const desc = h('p', { class: 'muted small flush' }, says(home));
  save.onclick = async () => {
    if (pick && await saveConfig({ location: { name: pick.label, latitude: pick.latitude, longitude: pick.longitude } }, 'Location')) {
      desc.textContent = says(S.config.location); save.disabled = true;
    }
  };
  return h('section', { class: 'card stack', 'aria-label': 'Location', id: 'location' }, h('h2', { class: 'h2' }, 'Location'),
    desc,
    h('label', { class: 'field-col', for: 'set-location' }, 'Town or city', picker),
    h('div', { class: 'row end' }, save));
}

/** Restart, tucked away in a closed section and never in one tap: pick what to restart, then say so
 * again on the page (bongo/power.py). The display software is back in about 10 s, the Pi in about a minute. */
function restartBox() {
  const KINDS = {
    display: ['Restart the display', 'Restart the display software? The panel and this page go quiet for about 10 seconds.', 'Restarting the display…', 'Yes, restart the display'],
    device: ['Restart the Matrix', 'Restart the whole Matrix (the Pi)? The panel and this page go quiet for about a minute.', 'Restarting the Matrix…', 'Yes, restart the Matrix'],
  };
  const ask = h('div', { class: 'stack-s', role: 'group', 'aria-label': 'Confirm restart', hidden: true });
  const status = h('p', { class: 'small flush muted', 'aria-live': 'polite' });
  const pick = h('div', { class: 'row gap-s wrap' });
  // after a restart: wait until it has gone (or long enough), then reload as soon as it answers again
  const comeBack = (what, quiet) => {
    const t0 = Date.now(), gone = what === 'display' ? 5000 : 20000;
    let wentAway = false;
    const tick = async () => {
      if (Date.now() - t0 > 180000) {
        status.className = 'small flush danger';
        status.textContent = "Still not back. Check it's switched on and on the Wi-Fi, then reload this page.";
        return;
      }
      try {
        const r = await fetch('/api/pair/status', { cache: 'no-store', signal: AbortSignal.timeout(4000) });   // a Pi that's down can hang a request
        if (!r.ok) throw new Error(r.status);
        if (wentAway || Date.now() - t0 > gone) { location.reload(); return; }
      } catch { wentAway = true; }
      setTimeout(tick, 3000);
    };
    setTimeout(tick, quiet);
  };
  const confirmFor = (what, opener) => {
    const [, question, doing, sure] = KINDS[what];
    const yes = h('button', { type: 'button', class: 'btn danger-btn' }, sure);
    const no = h('button', { type: 'button', class: 'btn ghost' }, 'Cancel');
    no.onclick = () => { ask.hidden = true; pick.hidden = false; opener.focus(); };
    yes.onclick = async () => {
      yes.disabled = no.disabled = true;
      try {
        await post('/api/restart', { what });
        ask.hidden = true; status.className = 'small flush warn'; status.textContent = `${doing} This page reconnects when it's back.`;
        status.tabIndex = -1; status.focus();
        comeBack(what, 2000);
      } catch (err) { status.className = 'small flush danger'; status.textContent = err.message; ask.hidden = true; pick.hidden = false; opener.focus(); }
    };
    ask.replaceChildren(h('p', { class: 'small flush strong' }, question), h('div', { class: 'row gap-s wrap' }, yes, no));
    pick.hidden = true; ask.hidden = false; no.focus();
  };
  pick.append(...Object.entries(KINDS).map(([what, [label]]) =>
    h('button', { type: 'button', class: 'btn', onclick: (ev) => confirmFor(what, ev.currentTarget) }, `${label}…`)));
  return h('details', { class: 'item-more' },
    h('summary', null, 'Restart'),
    h('div', { class: 'stack-s' },
      h('p', { class: 'muted tiny flush' }, 'Restarting the display is enough for panel size and wiring changes. Restart the whole Matrix if it seems stuck.'),
      pick, ask, status));
}

/** The networks the Matrix remembers and rejoins by itself, one row each, with Forget (a dev network, an old
 * router): asked first, in the row itself. ``wired``: on a cable, so this page stays up whatever is forgotten.
 * ``reload`` null: an older Matrix that can't forget. */
function savedNetworks(saved, reload, wired) {
  const heading = h('span', { class: 'field-label', id: 'saved-nets-label' }, 'Saved networks');
  const status = h('p', { class: 'small flush danger', role: 'alert', hidden: true });
  let i = 0;
  const row = (s) => {
    const extra = s.names.filter((n) => n !== s.ssid);   // Raspberry Pi Imager's "preconfigured", say
    const line = h('div', { class: 'saved-net row between gap-s' },
      h('span', { class: 'grow min0 saved-net-name' }, s.ssid, extra.length ? h('span', { class: 'muted tiny' }, ` (saved as ${extra.join(', ')})`) : null),
      s.current ? h('span', { class: 'tag' }, 'Connected') : null);
    const item = h('div', { class: 'stack-s', role: 'listitem' }, line);
    if (!reload) return item;
    const forget = h('button', { type: 'button', class: 'btn small-btn', 'aria-label': `Forget ${s.ssid}` }, 'Forget');
    const ask = h('div', { class: 'stack-s', role: 'group', hidden: true });
    const cancel = () => { ask.hidden = true; forget.hidden = false; forget.focus(); };
    ask.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); cancel(); } });
    forget.onclick = () => {
      const question = h('p', { class: 'small flush', id: `forget-q-${i++}` }, s.current
        ? `Forget ${s.ssid}? The Matrix is on it now: it leaves straight away and joins another saved network in range, or opens setup (within 5 minutes if other saved networks are just out of range).${wired ? '' : ' This page goes offline until it\'s back on Wi-Fi.'}`
        : `Forget ${s.ssid}? The Matrix won't join it again unless you add it from setup.`);
      const yes = h('button', { type: 'button', class: 'btn small-btn danger-btn' }, 'Forget');
      const no = h('button', { type: 'button', class: 'btn small-btn ghost' }, 'Cancel');
      ask.setAttribute('aria-describedby', question.id);
      no.onclick = cancel;
      yes.onclick = async () => {
        yes.disabled = no.disabled = true;
        try {
          await post('/api/network/forget', { ssid: s.ssid });
          toast(s.current && !wired   // (a toast: the card is drawn afresh)
            ? `Forgot ${s.ssid}. The Matrix is leaving it: this page comes back once it's on another saved network.`
            : `Forgot ${s.ssid}. The Matrix won't join it again.`, s.current ? 'info' : 'success', 8000);
          await reload();
          document.getElementById('saved-nets-label')?.focus();   // (the row it was in has gone)
        } catch (e) { status.hidden = false; status.textContent = e.message; cancel(); }
      };
      ask.replaceChildren(question, h('div', { class: 'row gap-s wrap' }, yes, no));
      forget.hidden = true; ask.hidden = false; no.focus();
    };
    line.append(forget);
    item.append(ask);
    return item;
  };
  heading.tabIndex = -1;
  return h('div', { class: 'stack-s' }, heading,
    h('p', { class: 'muted tiny flush' }, 'The Matrix rejoins these by itself. Forget one it shouldn’t use any more, such as a dev network or an old router.'),
    h('div', { class: 'saved-nets', role: 'list', 'aria-labelledby': heading.id }, ...saved.map(row)),
    status);
}

/** Wi-Fi: which network it's on, its saved networks (with Forget), and "Reopen setup" for a new router or
 * password (bongo/net.py). */
function wifiCard() {
  const body = h('div', { class: 'stack gap-s' }, h('p', { class: 'muted small flush' }, 'Loading…'));
  const card = h('section', { class: 'card stack', 'aria-label': 'Wi-Fi' }, h('h2', { class: 'h2' }, 'Wi-Fi'), body);
  const STATES = { connected: 'Connected', connecting: 'Looking for a saved network', setup: 'Setup is open', starting: 'Starting' };
  const show = (n) => {
    if (!n.managed) { card.hidden = true; return; }   // not a Pi running NetworkManager (a laptop, the demo)
    const what = n.wired ? 'On a cable' : n.ssid ? `On ${n.ssid}` : STATES[n.state] || n.state;
    const rows = [h('div', { class: 'row between wrap' }, h('span', { class: 'strong' }, what), h('span', { class: 'muted small' }, STATES[n.state] || ''))];
    if (n.hotspot) rows.push(h('p', { class: 'small flush' }, 'Join ', h('span', { class: 'mono strong' }, n.hotspot.ssid), ' with password ', h('span', { class: 'mono strong' }, n.hotspot.password), ' to choose a network.'));
    const saved = n.saved_networks || (n.saved || []).map((name) => ({ ssid: name, names: [name], current: false }));   // (an older Matrix: names only)
    if (saved.length) rows.push(savedNetworks(saved, n.saved_networks ? load : null, n.wired));
    if (!(n.bands || []).includes('5')) rows.push(h('p', { class: 'muted tiny flush' }, 'This Matrix only has 2.4 GHz Wi-Fi (a Pi Zero): a 5 GHz-only network won\'t show up. Use your router\'s 2.4 GHz band or a USB Wi-Fi adapter.'));
    const reopen = h('button', { type: 'button', class: 'btn' }, 'Reopen setup');
    reopen.onclick = async () => {
      reopen.disabled = true;
      try { await post('/api/network/setup', {}); toast('Setup is open: join the Matrix-Setup network the panel shows', 'success', 8000); load(); }
      catch (e) { toast(e.message, 'error'); reopen.disabled = false; }
    };
    if (n.state !== 'setup') rows.push(h('div', { class: 'stack gap-s' },
      h('p', { class: 'muted tiny flush' }, 'New router or Wi-Fi password? Reopen setup: the Matrix leaves this network and shows a setup network to join from your phone. This dashboard goes offline until it\'s back on Wi-Fi.'),
      h('div', { class: 'row end' }, reopen)));
    body.replaceChildren(...rows);
  };
  const load = () => get('/api/network').then(show).catch(() => { card.hidden = true; });
  load();
  return card;
}

/** The Matrix's one pet: which animal and its name, shared by the Pet module, Wander and the Pet clock. */
const PET_NAME = /^[A-Za-z0-9 '.!?-]{1,12}$/;

function petCard() {
  const pet = { kind: 'cat', name: 'Mochi', coat: '', collar: '', dress_up: true, ...(S.config.pet || {}) };
  const kinds = S.state?.pet_kinds?.length ? S.state.pet_kinds : ['cat'];
  const kind = h('select', { class: 'input', id: 'pet-kind' }, kinds.map((k) => h('option', { value: k, selected: k === pet.kind || null }, humanize(k))));
  const name = h('input', { class: 'input', id: 'pet-name', maxlength: 12, value: pet.name, autocomplete: 'off', spellcheck: 'false' });
  const coatName = h('span', { class: 'mono strong' });
  const grid = h('div', { class: 'coat-grid', role: 'radiogroup', 'aria-label': 'Coat' });
  const coatRow = h('div', { class: 'stack gap-s', hidden: true }, h('div', { class: 'row between' }, h('span', null, 'Coat'), coatName), grid);
  let catalogue = [];
  let coat = pet.coat;
  let dressUp = pet.dress_up !== false;
  const collar = h('select', { class: 'input', id: 'pet-collar' });
  const wardrobe = h('div', { class: 'grid-2', hidden: true },
    h('label', { class: 'field-col', for: collar.id }, 'Collar', collar),
    h('div', { class: 'field-col' }, 'Seasonal outfits',
      toggle(dressUp, 'Dress up for Christmas and Valentine’s', (v) => { dressUp = v; })));
  const pick = (id) => {
    coat = id;
    for (const b of grid.children) b.setAttribute('aria-checked', String(b.dataset.coat === id));
    coatName.textContent = grid.querySelector(`[data-coat="${CSS.escape(id)}"]`)?.title || '';
  };
  const showCoats = () => {
    const entry = catalogue.find((k) => k.kind === kind.value);
    const coats = entry?.coats || [];
    coatRow.hidden = coats.length < 2;
    grid.replaceChildren(...coats.map((c) => h('button', {
      type: 'button', class: 'coat', role: 'radio', title: c.name, 'aria-label': c.name, 'data-coat': c.id, onclick: () => pick(c.id),
    }, c.face ? h('img', { src: c.face, alt: '' }) : h('span', { class: 'small' }, c.name))));
    pick(coats.some((c) => c.id === coat) ? coat : (entry?.default || coats[0]?.id || ''));
    const colours = entry?.collars || [];
    wardrobe.hidden = !colours.length;
    collar.replaceChildren(h('option', { value: '' }, 'No collar'),
      ...colours.map((c) => h('option', { value: c, selected: c === pet.collar || null }, humanize(c))));
  };
  kind.addEventListener('change', () => { coat = ''; showCoats(); });
  get('/api/pets').then((r) => { catalogue = r.kinds || []; showCoats(); }).catch(() => { /* older Pi: no coats to pick */ });
  const save = h('button', { type: 'button', class: 'btn primary' }, 'Save');
  save.onclick = () => {
    const n = name.value.trim();
    if (!PET_NAME.test(n)) { toast("Names are 1 to 12 letters, digits or spaces (and ' . ! ? -), so they fit her name tag", 'error'); name.focus(); return; }
    const outfits = wardrobe.hidden ? {} : { ...(collar.value ? { collar: collar.value } : {}), dress_up: dressUp };
    saveConfig({ pet: { kind: kind.value, name: n, ...(coat ? { coat } : {}), ...outfits } }, 'Pet');
  };
  return h('section', { class: 'card stack', 'aria-label': 'Pet' },
    h('div', null, h('h2', { class: 'h2' }, 'Pet'),
      h('p', { class: 'muted small flush' }, 'One pet per Matrix: the Pet screens, the Wander overlay and the Pet clock all show this one.')),
    h('div', { class: 'grid-2' },
      h('label', { class: 'field-col', for: kind.id }, 'Animal', kind),
      h('label', { class: 'field-col', for: name.id }, 'Name', name)),
    coatRow,
    wardrobe,
    h('div', { class: 'row end' }, save));
}

/** Linked calendars (bongo/calendars.py): Google / iPhone calendars read from their links, shown on every
 * Calendar screen. A link is sent once and never shown again: it works like a password. */
function calendarsCard() {
  const list = h('div', { class: 'stack gap-s' }, h('p', { class: 'muted small flush' }, 'Loading…'));
  const formBox = h('div');
  const card = h('section', { class: 'card stack', 'aria-label': 'Calendars', id: 'calendars' },
    h('div', { class: 'row between wrap gap-s' },
      h('div', null, h('h2', { class: 'h2' }, 'Calendars'),
        h('p', { class: 'muted small flush' }, 'Show events from your Google or iPhone calendar on the Calendar screens. Read-only: the Matrix never changes your calendar.')),
      helpButton(moduleById('calendar')?.help, { label: 'Step-by-step help' })), list, formBox,
    h('p', { class: 'sr-only', 'aria-live': 'polite', id: 'cal-said' }));
  const KIND = { google: 'Google', icloud: 'iCloud', outlook: 'Outlook', other: 'Calendar link' };
  const NAMES = COLOUR_NAMES;
  const drafts = {};   // a word half-typed in a calendar's "icons by word", kept while the list is drawn again
  let iconSet = {};
  icons().then((set) => { iconSet = set; if (last) show(last); });
  const say = (msg) => { card.querySelector('#cal-said').textContent = msg; };
  let busy = false, last = null, form = null;
  const act = async (btn, job, done) => {
    if (busy) return false;
    busy = true; const was = btn.textContent; btn.disabled = true; btn.textContent = 'Working…';
    try { show(await job()); if (done) { toast(done, 'success'); say(done); } return true; }
    catch (e) { toast(e.message, 'error', 10000); say(e.message); return false; }
    finally { busy = false; btn.disabled = false; btn.textContent = was; }
  };
  const state = (c) => {
    if (c.waiting) return h('span', { class: 'muted small' }, 'Checking…');
    if (c.error && !c.ok) return h('span', { class: 'danger small' }, `Can't read it: ${c.error}`);
    return h('span', { class: c.error ? 'warn small' : 'muted small' }, `${c.count} event${c.count === 1 ? '' : 's'} · checked ${ago(c.checked)}`,
      c.error ? ` · the last try failed (${c.error}), so it's showing the copy from ${ago(c.ok)}` : '');
  };
  const row = (c) => {
    const remove = h('button', { type: 'button', class: 'btn ghost danger-btn small-btn' }, 'Remove');
    const cancel = h('button', { type: 'button', class: 'btn ghost small-btn' }, 'Cancel');
    const yes = h('button', { type: 'button', class: 'btn danger-btn small-btn' }, 'Yes, remove');
    const sure = h('div', { class: 'row gap-s wrap', hidden: true, role: 'group', 'aria-label': `Remove ${c.name}?` },
      h('span', { class: 'small' }, `Remove ${c.name}? Its events leave the Matrix; your calendar itself isn't touched.`), yes, cancel);
    remove.onclick = () => { sure.hidden = false; remove.hidden = true; yes.focus(); };
    cancel.onclick = () => { sure.hidden = true; remove.hidden = false; remove.focus(); };
    yes.onclick = () => act(yes, () => del(`/api/calendars/${c.id}`), `${c.name} removed`);
    const onOff = toggle(c.on !== false, `Show ${c.name}`, (on) => api('PATCH', `/api/calendars/${c.id}`, { on }).then(show)
      .catch((e) => { toast(e.message, 'error', 8000); if (last) show(last); }), { showLabel: false });   // back as it was
    return h('li', { class: 'stack gap-xs', 'data-cal': c.id },
      h('div', { class: 'row between wrap gap-s' },
        h('div', { class: 'row gap-s min0 wrap' }, h('span', { style: `display:inline-block;width:12px;height:12px;border-radius:50%;flex:none;background:${c.colour}`, 'aria-hidden': 'true' }),
          h('span', { class: 'strong' }, c.name), h('span', { class: 'muted small' }, KIND[c.kind] || KIND.other),
          c.only ? h('span', { class: 'tag', title: 'Only events with these words' }, `only: ${c.only}`) : null,
          c.busy ? h('span', { class: 'tag' }, 'private as Busy') : null),
        h('div', { class: 'row gap-s' }, onOff, remove)),
      state(c), sure, lookEditor(c, iconSet, last?.colours || Object.keys(NAMES), drafts, show));
  };
  const makeForm = (s) => {   // made once, so a half-filled form survives the list updating
    const name = h('input', { class: 'input', id: 'cal-name', placeholder: 'Family', maxlength: 30, autocomplete: 'off' });
    const link = h('input', { class: 'input mono', id: 'cal-link', type: 'password', placeholder: 'https://calendar.google.com/… or webcal://…',
      autocomplete: 'off', spellcheck: 'false', 'aria-describedby': 'cal-link-hint', 'data-lpignore': 'true', 'data-1p-ignore': 'true' });
    const colour = h('select', { class: 'input', id: 'cal-colour' }, ...s.colours.map((c) => h('option', { value: c }, NAMES[c] || c)));
    colour.value = s.colours[s.calendars.length % s.colours.length];
    const only = h('input', { class: 'input', id: 'cal-only', placeholder: 'birthday, bins', maxlength: 80, autocomplete: 'off' });
    const busyBox = h('input', { type: 'checkbox', id: 'cal-busy' });
    const add = h('button', { type: 'button', class: 'btn primary' }, 'Add calendar');
    add.onclick = async () => {
      const ok = await act(add, () => post('/api/calendars', { name: name.value, url: link.value, colour: colour.value, only: only.value, busy: busyBox.checked }), 'Calendar added');
      if (ok) { name.value = link.value = only.value = ''; busyBox.checked = false; colour.value = s.colours[(last?.calendars.length || 0) % s.colours.length]; }
    };
    return h('details', { class: 'item-more', open: s.calendars.length === 0 || null },
      h('summary', null, 'Add a calendar'),
      h('div', { class: 'stack gap-s', style: 'margin-top:8px' },
        h('label', { class: 'field-col', for: name.id }, 'Name', name),
        h('label', { class: 'field-col', for: link.id }, 'Calendar link', link),
        h('p', { class: 'tiny muted flush', id: 'cal-link-hint' }, "Google: Settings › your calendar › Integrate calendar › Secret address in iCal format. iPhone: Calendars › (i) › Public Calendar › Share Link. It's hidden once added."),
        h('div', { class: 'row gap-s wrap' }, h('label', { class: 'field-col', for: colour.id }, 'Colour', colour),
          h('label', { class: 'field-col grow', for: only.id }, 'Only events with (optional)', only)),
        h('label', { class: 'row gap-s small', for: busyBox.id }, busyBox, h('span', null, 'Show private events as Busy')),
        h('div', { class: 'row end' }, add)));
  };
  let poll = null;
  onLeave(() => clearTimeout(poll));
  const show = (s) => {
    last = s;
    clearTimeout(poll);
    if (s.calendars?.some((c) => c.waiting)) poll = setTimeout(() => get('/api/calendars').then(show).catch(() => {}), 3000);   // until the checks finish
    const focusId = document.activeElement?.closest?.('#calendars li')?.dataset.cal;
    const again = keepState(list);   // open sections and focus inside a calendar's colour and icons
    if (!s.ready) {
      list.replaceChildren(h('p', { class: 'warn small flush' }, 'This needs an update first: press Update in Settings › Software, then come back.'));
      return;
    }
    const check = h('button', { type: 'button', class: 'btn ghost small-btn' }, 'Check now');
    check.onclick = () => act(check, () => post('/api/calendars/refresh', {}), 'Checking every calendar now');
    list.replaceChildren(...[
      s.calendars.length ? h('ul', { class: 'stack gap-s', style: 'list-style:none;padding:0;margin:0' }, ...s.calendars.map(row)) : h('p', { class: 'muted small flush' }, 'No calendars yet.'),
      s.calendars.length ? h('div', { class: 'row between wrap gap-s' }, h('span', { class: 'tiny muted' }, 'Checked again every 15 minutes, when a Calendar screen comes round.'), check) : null].filter(Boolean));
    again();
    if (focusId && !list.contains(document.activeElement)) {
      const onOff = list.querySelector(`li[data-cal="${focusId}"] .switch`);   // keep keyboard focus on that calendar
      if (onOff) onOff.focus();
      else { const head = card.querySelector('h2'); head.tabIndex = -1; head.focus(); }   // that calendar was removed
    }
    window.dispatchEvent(new Event('calendars-changed'));   // Coming up follows
    if (!form) { form = makeForm(s); formBox.replaceChildren(form); }
    form.hidden = s.calendars.length >= s.most;
    formBox.querySelector('.cal-full')?.remove();
    if (form.hidden) formBox.append(h('p', { class: 'muted small flush cal-full' }, `That's ${s.most}, the most it takes.`));
  };
  get('/api/calendars').then(show).catch(() => list.replaceChildren(h('p', { class: 'muted small flush' }, "Couldn't load the calendars.")));
  const edited = () => get('/api/calendars').then(show).catch(() => {});   // a colour changed in Coming up
  window.addEventListener('calendar-edited', edited);
  onLeave(() => window.removeEventListener('calendar-edited', edited));
  return card;
}

/** Spotify: sign in once so the Spotify screens can show what's playing. A Matrix has no https
 * address of its own, so the sign-in ends on an address that doesn't load; its URL is pasted back here. */
function spotifyCard() {
  const body = h('div', { class: 'stack gap-s' }, h('p', { class: 'muted small flush' }, 'Loading…'));
  const card = h('section', { class: 'card stack', 'aria-label': 'Spotify' },
    h('div', { class: 'row between wrap gap-s' },
      h('div', null, h('h2', { class: 'h2' }, 'Spotify'),
        h('p', { class: 'muted small flush' }, 'Sign in once so the Spotify screens can show what you\'re playing.')),
      helpButton(moduleById('spotify')?.help, { label: 'Step-by-step help' })), body);
  const show = (s) => {
    if (s.connected) {
      const off = h('button', { type: 'button', class: 'btn ghost danger-btn' }, 'Sign out');
      off.onclick = async () => { try { await del('/api/spotify'); toast('Signed out of Spotify'); load(); } catch (e) { fail("Couldn't sign out")(e); } };
      body.replaceChildren(h('div', { class: 'row between wrap' }, h('span', { class: 'strong' }, 'Connected'), off));
      return;
    }
    const id = h('input', { class: 'input mono', id: 'spotify-id', placeholder: 'Client ID', autocomplete: 'off', spellcheck: 'false' });
    const pasted = h('input', { class: 'input mono', id: 'spotify-url', placeholder: `${s.redirect}?code=…`, autocomplete: 'off', spellcheck: 'false' });
    const go = h('button', { type: 'button', class: 'btn' }, 'Sign in with Spotify');
    const done = h('button', { type: 'button', class: 'btn primary' }, 'Connect');
    go.onclick = async () => {
      try { const { url } = await post('/api/spotify/start', { client_id: id.value }); window.open(url, '_blank', 'noopener'); pasted.focus(); }
      catch (e) { toast(e.message, 'error', 8000); }
    };
    done.onclick = async () => {
      try { await post('/api/spotify/finish', { url: pasted.value }); toast('Spotify connected', 'success'); load(); }
      catch (e) { toast(e.message, 'error', 10000); }
    };
    body.replaceChildren(
      h('ol', { class: 'small stack gap-s', style: 'margin:0;padding-left:18px' },
        h('li', null, 'At developer.spotify.com make an app (Web API). Add the redirect URI ', h('span', { class: 'mono' }, s.redirect), ' and copy its Client ID. (Spotify needs the app owner to have Premium.)'),
        h('li', null, 'Paste the Client ID, then sign in and approve.'),
        h('li', null, 'Your browser then goes to an address that doesn\'t load. That\'s expected: copy it from the address bar, paste it below and press Connect.')),
      h('label', { class: 'field-col', for: id.id }, 'Client ID', h('div', { class: 'row gap-s wrap' }, id, go)),
      h('label', { class: 'field-col', for: pasted.id }, 'The address it went to', h('div', { class: 'row gap-s wrap' }, pasted, done)));
  };
  const load = () => get('/api/spotify').then(show).catch(() => body.replaceChildren(h('p', { class: 'muted small flush' }, "Couldn't load Spotify's status.")));
  load();
  return card;
}

/** Weather: the OpenWeather API key, entered once for every Weather screen. The Matrix keeps it to itself
 * (data/openweather.json, not the config) and only ever says whether there is one and whether it works. */
function weatherCard() {
  const body = h('div', { class: 'stack gap-s' }, h('p', { class: 'muted small flush' }, 'Loading…'));
  const card = h('section', { class: 'card stack', id: 'weather', 'aria-label': 'Weather' },
    h('div', { class: 'row between wrap gap-s' },
      h('div', null, h('h2', { class: 'h2' }, 'Weather'),
        h('p', { class: 'muted small flush' }, 'Your OpenWeather API key, for every Weather screen.')),
      helpButton(moduleById('weather')?.help, { label: 'Step-by-step help' })), body);
  const status = (s) => {
    if (!s.saved) return h('div', { class: 'note trouble', role: 'status' }, icon('warn'), h('div', { class: 'stack gap-s' },
      h('strong', null, 'Add your OpenWeather API key'),
      h('span', null, 'It\'s free. Until there is one, the Weather screens are skipped. Step-by-step help shows you how to get one.')));
    if (s.trouble) return h('div', { class: 'note trouble', role: 'status' }, icon('warn'), h('div', { class: 'stack gap-s' },
      h('strong', null, 'Your OpenWeather API key isn\'t working'),
      h('span', null, 'A new key can take up to two hours to start working: the Weather screens are skipped until it does, '
        + 'then come back by themselves. Still not working after that? Copy it again from My API keys, whole, and save it again.')));
    return h('p', { class: 'small flush row gap-s' }, h('span', { class: 'dot ok', 'aria-hidden': 'true' }), h('span', null, 'API key saved. The Weather screens use it.'));
  };
  const show = (s) => {
    const input = h('input', { class: 'input mono grow', id: 'weather-key', type: 'password', autocomplete: 'off', spellcheck: 'false',
      placeholder: s.saved ? 'Paste a new key' : 'Paste your API key', 'aria-label': 'OpenWeather API key' });
    const save = h('button', { type: 'submit', class: 'btn primary' }, 'Save');
    const form = h('form', { class: 'stack gap-s' }, h('label', { class: 'field-col', for: input.id }, 'OpenWeather API key',
      h('div', { class: 'row gap-s wrap' }, input, save)));
    form.onsubmit = async (ev) => {
      ev.preventDefault();
      save.disabled = true;
      try {
        const res = await put('/api/weather', { key: input.value });
        if (res.problems?.length) toast(res.problems.join(' '), 'error', 10000);
        else toast('OpenWeather API key saved', 'success');
        show(res);
      } catch (e) { toast(e.message, 'error', 8000); save.disabled = false; }
    };
    const off = s.saved ? h('button', { type: 'button', class: 'btn ghost danger-btn' }, 'Remove key') : null;
    if (off) off.onclick = async () => { try { show(await del('/api/weather')); toast('OpenWeather API key removed'); } catch (e) { fail("Couldn't remove the key")(e); } };
    body.replaceChildren(status(s), form, h('div', { class: 'row between wrap gap-s' },
      creditLine(moduleById('weather')?.help?.credit), off));
  };
  get('/api/weather').then(show).catch(() => body.replaceChildren(h('p', { class: 'muted small flush' }, "Couldn't load the Weather settings.")));
  return card;
}

const SCREEN_TYPES =/\.(gif|png|jpe?g|webp|bmp|mp4|mov|webm|mkv|avi|m4v)$/i;   // bongo/media.py IMAGES | VIDEOS

/** Software updates: what's running, check and pull the latest, and (advanced) where updates come from. */
const UPDATE_WORDS = {
  requested: 'Update requested…', fetching: 'Downloading…', installing: 'Installing…', restarting: 'Restarting…',
  updated: 'Updated', up_to_date: 'Already up to date', rolled_back: 'Rolled back', failed: 'Update failed', busy: 'Busy',
};
const UPDATE_RUNNING = new Set(['requested', 'fetching', 'installing', 'restarting']);

/** Bring a card to the top of the page and keep it there while the rest of the page settles: the cards above it
 *  load one by one and grow, on a slow Matrix for several seconds. It ends the moment you scroll, tap or press a
 *  key (a scroll that isn't the page moving the card is yours), or leave the page. */
function bringToTop(card) {
  let mine = true, setY = 0, placed = 0, watch = null;
  const where = () => card.getBoundingClientRect().top + window.scrollY;   // the card's place on the page
  const put = () => {
    if (!mine || !card.isConnected) return;
    const bar = document.querySelector('.jump-bar');
    placed = where();
    window.scrollTo({ top: Math.max(0, placed - document.getElementById('topbar').offsetHeight - (bar?.offsetHeight || 0) - 8), behavior: 'auto' });
    setY = window.scrollY;
  };
  const scrolled = () => { if (mine && Math.abs(window.scrollY - setY) > 2 && Math.abs(where() - placed) < 1) stop(); };   // you moved, not the card
  const timers = [150, 500, 1200, 2500, 5000, 9000].map((ms) => setTimeout(put, ms));   // (a browser with no ResizeObserver)
  function stop() {
    mine = false;
    watch?.disconnect(); timers.forEach(clearTimeout);
    window.removeEventListener('scroll', scrolled);
    for (const ev of ['wheel', 'touchstart', 'pointerdown', 'keydown']) window.removeEventListener(ev, stop);
  }
  for (const ev of ['wheel', 'touchstart', 'pointerdown', 'keydown']) window.addEventListener(ev, stop, { passive: true });
  window.addEventListener('scroll', scrolled, { passive: true });
  if (window.ResizeObserver) {   // told before the page is drawn again, so the card never visibly jumps
    watch = new ResizeObserver(put);
    watch.observe(card.closest('.view-part') || document.body);
  }
  put();
  onLeave(stop);
}

function updateCard() {
  const card = h('section', { class: 'card stack', 'aria-label': 'Software' }, h('h2', { class: 'h2' }, 'Software'),
    h('p', { class: 'muted small flush' }, 'Loading…'));
  let info = null, poll = null;
  // Opened by the hub's Update… link (/settings#update): the page goes straight to this card, and if a new version
  // is ready the card asks to confirm it (deeplink.js). Not now, or starting the update, ends that
  let asked = deeplink.asked(location.hash, DEMO), arrived = false;
  let focusNext = false;   // the question has just gone (Not now, Update now): focus moves to the card's first button
  const stopPoll = () => { clearTimeout(poll); poll = null; };
  onLeave(stopPoll);

  const when = (iso) => { try { return new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }); } catch { return iso; } };
  async function refresh(quiet) {
    try { info = await get('/api/update'); }
    catch (err) { if (!quiet) card.replaceChildren(card.firstChild, h('p', { class: 'danger small' }, `Couldn't read the version: ${err.message}`)); return; }
    paint();
    if (asked && !arrived) { arrived = true; bringToTop(card); }
    const st = info.status?.state;
    stopPoll();
    if (UPDATE_RUNNING.has(st)) poll = setTimeout(() => refresh(true), 3000);  // the Matrix restarts mid-update: keep asking
  }

  function paint() {
    const v = info.version, st = info.status;
    const running = st && UPDATE_RUNNING.has(st.state);
    // the card is drawn again every 3 s while an update runs: whatever had the focus has it again afterwards
    const held = card.contains(document.activeElement) ? (document.activeElement.dataset.key || document.activeElement.id || null) : null;
    const result = h('p', { class: 'small flush', 'aria-live': 'polite' });
    const check = h('button', { type: 'button', class: 'btn', 'data-key': 'check' }, 'Check for updates');
    const pull = h('button', { type: 'button', class: 'btn primary', 'data-key': 'pull', disabled: running || null }, running ? 'Updating…' : 'Pull update');
    check.onclick = async () => {
      check.disabled = true; result.className = 'small flush muted'; result.textContent = 'Checking…';
      try {
        const r = await post('/api/update/check');
        result.className = 'small flush' + (r.available ? ' warn' : ' muted');
        result.textContent = r.available ? `A new version is ready (${r.latest.slice(0, 8)}).` : 'You have the latest version.';
      } catch (err) { result.className = 'small flush danger'; result.textContent = err.message; }
      check.disabled = false;
    };
    const start = async () => {   // (one way to start an update, whichever button asked)
      try { await post('/api/update'); toast('Update started', 'success'); refresh(); }
      catch (err) { toast(err.message, 'error'); }
    };
    pull.onclick = async () => {
      if (!confirm('Pull the latest version and restart the Matrix? The panel and this page go quiet for about a minute. If the new version won\'t start, the old one comes back by itself.')) return;
      start();
    };
    const stLine = st ? h('p', { class: 'small flush ' + (st.state === 'rolled_back' || st.state === 'failed' ? 'danger' : running ? 'warn' : 'muted') },
      `${UPDATE_WORDS[st.state] || st.state}: ${st.message} · ${when(st.at)}`) : null;
    const ready = asked ? deeplink.ready(info, S.state?.name) : null;
    if (ready) {   // from the hub's Update…: just the question. Update now does what Pull update does; Not now leaves the usual card
      const now = h('button', { type: 'button', class: 'btn primary' }, 'Update now');
      const later = h('button', { type: 'button', class: 'btn' }, 'Not now');
      const line = h('p', { class: 'flush', id: 'update-ready', tabindex: '-1', style: 'outline:none' }, ready.line);   // (focused below, never by Tab)
      const trouble = h('p', { class: 'danger small flush', role: 'alert', hidden: true });
      now.onclick = async () => {
        line.focus({ preventScroll: true });   // (a disabled button can't keep the focus)
        now.disabled = later.disabled = true; now.textContent = 'Starting…'; trouble.hidden = true;
        // the question (and #update on the address) stays until the Matrix has accepted the update: see deeplink.js
        const went = await deeplink.updateNow(post, get);
        if (went.unpaired || !card.isConnected) return;   // off to pair again (and back to this question), or the page has gone
        if (went.done) {   // under way: the usual card shows its progress
          asked = false; focusNext = true;
          if (went.mine) toast('Update started', 'success');
          await refresh();
          if (focusNext) { focusNext = false; const head = card.querySelector('h2'); head.tabIndex = -1; head.style.outline = 'none'; head.focus({ preventScroll: true }); }   // (it couldn't be read)
          return;
        }
        now.disabled = later.disabled = false; now.textContent = 'Update now';
        trouble.textContent = went.why; trouble.hidden = false;   // why, here in the card, and the buttons are back to try again
        now.focus({ preventScroll: true });
      };
      later.onclick = () => { asked = false; deeplink.forget(); focusNext = true; paint(); };
      card.replaceChildren(...[
        h('h2', { class: 'h2' }, 'Software'),
        line,
        st && (st.state === 'rolled_back' || st.state === 'failed') ? stLine : null,   // the last try, if it went wrong
        h('div', { class: 'row gap-s wrap', role: 'group', 'aria-labelledby': 'update-ready' }, now, later),
        trouble,
        h('p', { class: 'muted small flush' }, ready.note)].filter(Boolean));
      line.focus({ preventScroll: true });   // read out by a screen reader; Tab goes on to Update now
      return;
    }
    if (info.known?.available === true && typeof info.known.latest === 'string' && !running) {   // what the daily look found
      result.className = 'small flush warn'; result.textContent = `A new version is ready (${info.known.latest.slice(0, 8)}).`;
    } else if (asked && info.known?.available === false && !running) {   // the hub was a moment behind: nothing to do
      result.className = 'small flush muted'; result.textContent = 'You have the latest version.';
    }
    card.replaceChildren(...[
      h('h2', { class: 'h2' }, 'Software'),
      h('div', { class: 'stack-xs' },
        h('span', { class: 'field-label' }, 'Running'),
        v ? h('span', { class: 'mono small' }, `${v.short} · ${when(v.date)}`) : h('span', { class: 'muted small' }, 'Unknown (not a git checkout)'),
        v ? h('span', { class: 'muted tiny' }, v.subject) : null),
      stLine,
      h('div', { class: 'row gap-s wrap' }, check, pull), result,
      sourceBox()].filter(Boolean));  // no status line until the first update
    if (focusNext) { focusNext = false; check.focus({ preventScroll: true }); }   // (not left on nothing when the question goes)
    else if (held) {
      const again = card.querySelector(`[data-key="${held}"]`) || [...card.querySelectorAll('[id]')].find((el) => el.id === held);
      (again && !again.disabled ? again : check).focus({ preventScroll: true });   // (Pull update is off while one runs)
    }
  }

  function sourceBox() {
    const url = h('input', { class: 'input mono', id: 'upd-url', value: info.url, spellcheck: 'false', autocomplete: 'off' });
    const branch = h('input', { class: 'input mono', id: 'upd-branch', value: info.branch, spellcheck: 'false', autocomplete: 'off' });
    const save = h('button', { type: 'button', class: 'btn' }, 'Save source');
    const reset = h('button', { type: 'button', class: 'link small' }, 'Use the default');
    reset.onclick = () => { url.value = info.default_url; branch.value = info.default_branch; };
    save.onclick = async () => {
      const u = url.value.trim(), b = branch.value.trim() || info.default_branch;
      const isDefault = u === info.default_url && b === info.default_branch;
      if (u !== info.url && !isDefault && !confirm(
        'Change where updates come from?\n\nWhatever is at this address will be installed and run on this Matrix with full access to it. ' +
        'Only use a repository you control or completely trust. A wrong address can stop updates working, or worse.')) return;
      const ok = await saveConfig({ update: isDefault ? null : { url: u, branch: b } }, 'Update source');
      if (ok) refresh(true);
    };
    return h('details', { class: 'item-more', open: card.querySelector('details')?.open || null },   // (stays open across a redraw)
      h('summary', { 'data-key': 'source' }, 'Advanced: update source'),
      h('div', { class: 'stack-s' },
        h('p', { class: 'small flush warn' }, 'Only change this if you know why. Updates from this address run on the Matrix with full access.'),
        h('label', { class: 'field-col', for: url.id }, 'Repository address', url),
        h('label', { class: 'field-col', for: branch.id }, 'Branch', branch),
        h('div', { class: 'row between' }, reset, save)));
  }

  refresh();
  return card;
}

/** The files a screen can use: the built-in ones made for it (splash_* or sleep_*) and everything
 * you uploaded, whatever it's called (bongo/media.py for_screen). An upload with a built-in file's
 * name is a leftover copy of one, so only the built-in one is listed, and shipped(name) points a
 * saved uploaded/ name at its built-in twin. usable(name) says whether a saved name can stay. */
function screenFiles(lib, screen) {
  const builtIn = lib.folders['built-in'].filter((f) => f.screen === screen && SCREEN_TYPES.test(f.name));
  const twins = new Set(builtIn.map((f) => f.file));   // this screen's only: a copy of another's is just yours
  const uploaded = lib.folders.uploaded.filter((f) => !twins.has(f.file) && SCREEN_TYPES.test(f.name));
  const shipped = (n) => (n && n.startsWith('uploaded/') && twins.has(n.slice(9)) ? `built-in/${n.slice(9)}` : n);
  const usable = (n) => n.startsWith('uploaded/') || (screen === 'splash' ? ['splash_', 'boot_'] : [`${screen}_`]).some((p) => n.split('/').pop().startsWith(p));
  return { builtIn, uploaded, shipped, usable };
}

/** One file in a screen's list (Splash, Sleep, the boot screen): its tick box or radio button, a moving
 * thumbnail, its name with the real file name under it, and a Preview button. */
function screenRow(f, input, opts) {
  const gone = f.file.endsWith('(missing)');
  const thumb = gone ? h('span', { class: 'media-thumb empty' }) : mediaThumb(f.name, opts())[0];
  return h('div', { class: 'screen-source' },
    h('label', { class: 'screen-pick', for: input.id }, input, thumb,
      h('span', { class: 'screen-name grow min0' },
        h('span', { class: 'screen-title' }, gone ? 'Missing file' : mediaTitle(f.name)),
        h('span', { class: 'mono tiny muted screen-file' }, f.file.match(/[^_-]*[_-]?/g).filter(Boolean).flatMap((part, k) => (k ? [h('wbr'), part] : [part])))),   // the file name wraps after a _ or -, not mid-word
      f.kind === 'video' ? h('span', { class: 'tag' }, 'Video') : null),
    gone ? null : previewButton(f.name, opts, { compact: true }));
}

/** Built-in and uploaded files under their own headings, so it's clear which is which. */
function screenSections(row, missing, builtIn, uploaded) {
  const section = (title, list) => (list.length ? [h('div', { class: 'screen-divider', role: 'presentation' }, `${title} · ${list.length}`), ...list.map(row)] : []);
  return [...section('Missing', missing), ...section('Built in', builtIn), ...section('Your media', uploaded)];
}

/** Pick the files a Splash or Sleep screen draws from (built-in screens and your own media), plus speed and fit. */
function screenPicker(rule, idPrefix, screen) {
  rule.sources ||= [];
  const grid = h('div', { class: 'screen-sources', role: 'group', 'aria-label': 'Files' }, h('p', { class: 'muted small flush' }, 'Loading media…'));
  const speed = h('input', { class: 'input num', type: 'number', id: `${idPrefix}-speed`, min: 10, max: 400, step: 10, value: rule.speed ?? 100 });
  speed.addEventListener('input', () => { rule.speed = clamp(Number(speed.value) || 100, 10, 400); });
  const fit = h('select', { class: 'input', id: `${idPrefix}-fit` }, ['fit', 'fill', 'pixel'].map((f) => h('option', { value: f, selected: (rule.fit || 'fit') === f }, humanize(f))));
  fit.addEventListener('change', () => { rule.fit = fit.value; });
  (async () => {
    try {
      const lib = await get('/api/media');
      const { builtIn, uploaded, shipped, usable } = screenFiles(lib, screen);
      const files = [...builtIn, ...uploaded];
      const known = new Set(files.map((f) => f.name));
      rule.sources = [...new Set(rule.sources.map((n) => shipped(n)).filter(usable))];
      const missing = rule.sources.filter((n) => !known.has(n)).map((n) => ({ name: n, file: `${n} (missing)` }));
      const opts = () => ({ fit: rule.fit || 'fit', speed: (rule.speed ?? 100) / 100 });
      let i = 0;
      const card = (f) => {
        const box = h('input', { type: 'checkbox', id: `${idPrefix}-src-${i++}`, checked: rule.sources.includes(f.name) });
        box.addEventListener('change', () => {
          rule.sources = box.checked ? [...new Set([...rule.sources, f.name])] : rule.sources.filter((n) => n !== f.name);
        });
        return screenRow(f, box, opts);
      };
      grid.replaceChildren(...screenSections(card, missing, builtIn, uploaded));
      if (!files.length && !missing.length) grid.replaceChildren(h('p', { class: 'muted small flush' }, 'Nothing to pick from yet. Upload an image, GIF or video in the Media library.'));
    } catch (err) { grid.replaceChildren(h('p', { class: 'danger small' }, `Couldn't list media: ${err.message}`)); }
  })();
  return h('div', { class: 'stack-s' },
    h('span', { class: 'field-label' }, 'Files to pick from'), grid,
    h('div', { class: 'grid-3' },
      h('label', { class: 'field-col', for: speed.id }, 'Speed %', speed),
      h('label', { class: 'field-col', for: fit.id }, 'Fit', fit)));
}

function splashCard() {
  const start = S.config.splash;
  let on = !!start && start.enabled !== false;
  const rule = clone(start) || { sources: ['built-in/splash_loading_squares.gif'], speed: 100, fit: 'fit', boot: null };
  rule.boot ??= null;
  const inPlaylist = (S.config.playlist || []).filter((e) => e.module === 'splash').length;
  const body = h('div', { class: 'stack' }, screenPicker(rule, 'sp', 'splash'),
    h('p', { class: 'muted tiny flush' }, 'Each time a Splash item comes round in the playlist it shows the next file in a random order, so every one plays once before any repeats, and never the same one twice running. A GIF or video plays once through (a video up to 15 s); a still stays up about as long as your GIFs (7 s if there are none).'),
    h('p', { class: 'small flush' + (inPlaylist ? ' muted' : ' warn') }, inPlaylist
      ? `In the playlist ${inPlaylist === 1 ? 'once' : `${inPlaylist} times`}. Add more, or move it, on the Playlist page.`
      : 'Not in the playlist yet: add a Splash item on the Playlist page so it plays.'),
    bootPicker(rule));
  body.hidden = !on;
  const save = h('button', { type: 'button', class: 'btn primary' }, 'Save');
  save.onclick = () => {
    if (on && !rule.sources.length) { toast('Tick at least one file for the splash', 'error'); return; }
    // switched off: kept, so switching it back on brings back the files, speed, fit and boot screen
    const files = rule.sources.length ? rule.sources : start?.sources?.length ? start.sources : ['built-in/splash_loading_squares.gif'];
    saveConfig({ splash: { ...rule, sources: on ? rule.sources : files, enabled: on } }, 'Splash');
  };
  return h('section', { class: 'card stack', 'aria-label': 'Splash' },
    h('div', { class: 'row between wrap' }, h('div', null, h('h2', { class: 'h2' }, 'Splash'), h('p', { class: 'muted small flush' }, 'Plays wherever you put a Splash item in the playlist.')),
      toggle(on, 'Use splash screens', (v) => { on = v; body.hidden = !v; })),
    body, h('div', { class: 'row end' }, save));
}

/** The boot screen: one Splash file played once when the Matrix starts, or none. The same list as
 * Splash's and Sleep's (thumbnail, name, Preview), with one choice instead of tick boxes. */
function bootPicker(rule) {
  const grid = h('div', { class: 'screen-sources', role: 'radiogroup', 'aria-label': 'Boot screen' }, h('p', { class: 'muted small flush' }, 'Loading media…'));
  (async () => {
    try {
      const lib = await get('/api/media');
      const { builtIn, uploaded, shipped } = screenFiles(lib, 'splash');
      rule.boot = shipped(rule.boot);
      const known = new Set([...builtIn, ...uploaded].map((f) => f.name));
      const missing = rule.boot && !known.has(rule.boot) ? [{ name: rule.boot, file: `${rule.boot} (missing)` }] : [];
      const boots = [...builtIn].sort((a, b) => (b.file.startsWith('boot_') - a.file.startsWith('boot_')));   // boot screens first
      const opts = () => ({ fit: rule.fit || 'fit', speed: (rule.speed ?? 100) / 100 });
      let i = 0;
      const pick = (name) => {
        const radio = h('input', { type: 'radio', name: 'sp-boot', id: `sp-boot-${i++}`, value: name ?? '', checked: (rule.boot ?? null) === name });
        radio.addEventListener('change', () => { if (radio.checked) rule.boot = name; });
        return radio;
      };
      const none = pick(null);
      const nothing = h('div', { class: 'screen-source' },
        h('label', { class: 'screen-pick', for: none.id }, none, h('span', { class: 'media-thumb empty' }),
          h('span', { class: 'screen-name grow min0' }, h('span', { class: 'screen-title' }, 'None'),
            h('span', { class: 'tiny muted' }, 'Start straight into the playlist'))));
      grid.replaceChildren(nothing, ...screenSections((f) => screenRow(f, pick(f.name), opts), missing, boots, uploaded));
    } catch (err) { grid.replaceChildren(h('p', { class: 'danger small' }, `Couldn't list media: ${err.message}`)); }   // (saving keeps whatever was set)
  })();
  return h('div', { class: 'stack-s' },
    h('span', { class: 'field-label' }, 'Boot screen'), grid,
    h('p', { class: 'muted tiny flush' }, 'Plays once when the Matrix starts, before the playlist: a built-in boot screen or splash, or anything of your own, whether or not it’s ticked in the list above. It plays at the Speed and Fit above.'));
}

function sleepCard() {
  const s = S.config.sleep;
  let on = !!s && s.enabled !== false;
  const sleep = clone(s) || { from: '23:00', to: '07:00', brightness: 15, fade: 5, curve: 'smooth', sources: ['built-in/sleep_default.gif'], speed: 100, fit: 'fit' };
  sleep.fade ??= 5; sleep.curve ??= 'smooth';
  delete sleep.playlist;
  const from = h('input', { class: 'input', type: 'time', id: 'sl-from', value: sleep.from });
  const to = h('input', { class: 'input', type: 'time', id: 'sl-to', value: sleep.to });
  const bright = h('input', { class: 'input num', type: 'number', id: 'sl-b', min: 1, max: 100, value: sleep.brightness ?? 15 });
  const body = h('div', { class: 'stack' },
    h('div', { class: 'grid-3' },
      h('label', { class: 'field-col', for: 'sl-from' }, 'From', from),
      h('label', { class: 'field-col', for: 'sl-to' }, 'Until', to),
      h('label', { class: 'field-col', for: 'sl-b' }, 'Brightness %', bright)),
    fadeEditor(sleep, () => S.config.panel?.brightness ?? 80, () => Number(bright.value) || 15, bright),
    screenPicker(sleep, 'sl', 'sleep'),
    h('p', { class: 'muted tiny flush' }, 'Each night one of the ticked files is picked at random and stays up until morning.'));
  body.hidden = !on;
  const save = h('button', { type: 'button', class: 'btn primary' }, 'Save');
  save.onclick = () => {
    if (on && (!from.value || !to.value)) { toast('Set both times', 'error'); return; }
    if (on && from.value === to.value) { toast(`Sleep can't start and end at ${from.value}: change one`, 'error'); return; }   // (it would sleep all day)
    if (on && !sleep.sources.length) { toast('Tick at least one file for the sleep screen', 'error'); return; }
    // switched off: kept (times, brightness, fade, files), so switching it back on brings them back
    const pair = (a, b) => (a && b && a !== b ? { from: a, to: b } : null);   // (the Matrix won't take From == Until)
    const keep = on ? {} : { ...(pair(from.value, to.value) || pair(s?.from, s?.to) || { from: '23:00', to: '07:00' }),
      sources: sleep.sources.length ? sleep.sources : s?.sources?.length ? s.sources : ['built-in/sleep_default.gif'] };
    saveConfig({ sleep: { ...sleep, from: from.value, to: to.value, brightness: clamp(Number(bright.value) || 15, 1, 100), ...keep, enabled: on } }, 'Sleep schedule');
  };
  return h('section', { class: 'card stack', 'aria-label': 'Sleep' },
    h('div', { class: 'row between wrap' }, h('div', null, h('h2', { class: 'h2' }, 'Sleep'), h('p', { class: 'muted small flush' }, 'Dims the panel overnight and shows a calm screen.')),
      toggle(on, 'Sleep schedule', (v) => { on = v; body.hidden = !v; })),
    body, h('div', { class: 'row end' }, save));
}

// Day <-> night brightness glide: how long, and the shape, with a graph of night falling and
// morning coming back. Mirrors bongo/fade.py.
const CURVES = {
  smooth: ['Smooth', 'gentle at both ends', (t) => t * t * (3 - 2 * t)],
  linear: ['Linear', 'constant speed', (t) => t],
  ease_in: ['Ease in', 'slow start, quick finish', (t) => t * t],
  ease_out: ['Ease out', 'quick start, slow finish', (t) => 1 - (1 - t) * (1 - t)],
  perceptual: ['Even to the eye', 'steps look the same size to you', null],
};
function levelAt(a, b, t, curve) {
  if (curve === 'perceptual') { const la = Math.log(Math.max(a, 1)), lb = Math.log(Math.max(b, 1)); return Math.exp(la + (lb - la) * t); }
  return a + (b - a) * (CURVES[curve] || CURVES.smooth)[2](t);
}
function fadeEditor(rule, dayLevel, nightLevel, nightInput) {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 200 64'); svg.setAttribute('preserveAspectRatio', 'none'); svg.setAttribute('class', 'fade-graph'); svg.setAttribute('role', 'img');
  const secs = h('input', { class: 'input num', type: 'number', id: 'sl-fade', min: 0, max: 600, step: 0.5, value: rule.fade });
  const pick = h('select', { class: 'input', id: 'sl-curve' },
    ...Object.entries(CURVES).map(([k, [label]]) => h('option', { value: k, selected: k === rule.curve }, label)));
  const hint = h('p', { class: 'muted tiny flush' });
  const draw = () => {
    const a = dayLevel(), b = nightLevel(), y = (v) => 60 - (v / 100) * 56, pts = [];
    for (let i = 0; i <= 40; i++) pts.push(`${(i / 40) * 96 + 2},${y(levelAt(a, b, i / 40, rule.curve)).toFixed(1)}`);
    for (let i = 0; i <= 40; i++) pts.push(`${(i / 40) * 96 + 102},${y(levelAt(b, a, i / 40, rule.curve)).toFixed(1)}`);
    svg.replaceChildren();
    const line = (x1, y1, x2, y2, cls) => { const l = document.createElementNS(NS, 'line'); Object.entries({ x1, y1, x2, y2, class: cls }).forEach(([k, v]) => l.setAttribute(k, v)); svg.append(l); };
    line(100, 2, 100, 62, 'fade-mid');
    const p = document.createElementNS(NS, 'polyline'); p.setAttribute('points', pts.join(' ')); p.setAttribute('class', 'fade-line'); svg.append(p);
    svg.setAttribute('aria-label', `Brightness ${a}% to ${b}% at night and back in the morning, over ${rule.fade} seconds, ${CURVES[rule.curve][0]}`);
    hint.textContent = Number(rule.fade) > 0
      ? `Going to sleep: ${a}% → ${b}% over ${rule.fade} s, ${CURVES[rule.curve][1]}. Waking up glides back the same way.`
      : 'No fade: the brightness changes straight away.';
  };
  secs.oninput = () => { rule.fade = clamp(Number(secs.value) || 0, 0, 600); draw(); };
  pick.onchange = () => { rule.curve = pick.value; draw(); };
  nightInput.addEventListener('input', draw);
  draw();
  return h('div', { class: 'stack-s' },
    h('div', { class: 'grid-2' },
      h('label', { class: 'field-col', for: 'sl-fade' }, 'Fade (seconds)', secs),
      h('label', { class: 'field-col', for: 'sl-curve' }, 'Fade curve', pick)),
    h('div', { class: 'fade-box' }, svg, h('div', { class: 'row between tiny muted' }, h('span', null, 'Night falls'), h('span', null, 'Morning'))),
    hint);
}

function devicesCard() {
  const list = h('ul', { class: 'devices', 'aria-live': 'polite' }, h('li', { class: 'muted' }, 'Loading…'));
  const load = async () => {
    try {
      const devices = await get('/api/devices');
      list.replaceChildren(...devices.map((d) => h('li', { class: 'device' },
        h('div', { class: 'grow' }, h('div', { class: 'strong' }, d.name, d.me ? h('span', { class: 'tag amber' }, 'This browser') : null),
          h('div', { class: 'tiny muted' }, `Paired ${ago(d.paired)} · last seen ${ago(d.seen)}`)),
        h('button', { type: 'button', class: 'btn ghost danger-btn', 'aria-label': `Remove ${d.name}`, onclick: () => remove(d) }, 'Remove'))));
      if (!devices.length) list.replaceChildren(h('li', { class: 'muted' }, 'No paired devices.'));
    } catch (err) { list.replaceChildren(h('li', { class: 'danger' }, `Couldn't load devices: ${err.message}`)); }
  };
  const remove = async (d) => {
    if (!confirm(d.me ? 'Unpair this browser? You will need a new PIN to get back in.' : `Unpair “${d.name}”? It will need a new PIN.`)) return;
    try {
      const res = await del(`/api/devices/${encodeURIComponent(d.id)}`);
      if (!res?.ok) toast('That device was already gone');
      if (d.me) { S.config = null; go('/pair', { replace: true }); return; }
      toast(`Removed ${d.name}`); load();
    } catch (err) { fail("Couldn't remove it")(err); }
  };
  load();
  return h('section', { class: 'card stack', 'aria-label': 'Paired devices' },
    h('div', null, h('h2', { class: 'h2' }, 'Paired devices'), h('p', { class: 'muted small flush' }, 'Browsers that can control this Matrix.')), list);
}

const mb = (n) => (n < 1048576 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1048576).toFixed(n < 10485760 ? 1 : 0)} MB`);

// Media uploads: one queue for the whole page, so it carries on, and is still there with its reasons, if you leave
// Settings and come back. ``card``: the Media library card on the page now (its queue list, library and status line).
const UP = { queue: [], uploading: false, added: 0, failed: 0, card: null };
/** Upload what's waiting, one file at a time; more can be added while it runs. */
async function pumpUploads() {
  if (UP.uploading) return;
  UP.uploading = true;
  for (let next = UP.queue.find((q) => q.state === 'waiting'); next; next = UP.queue.find((q) => q.state === 'waiting')) {
    next.state = 'uploading';
    const left = UP.queue.filter((q) => q.state === 'waiting').length;
    UP.card?.says(`Uploading ${next.file.name}${left ? `, ${left} more waiting` : ''}`);
    UP.card?.paint();
    try { await uploadMedia(next.file); UP.added += 1; UP.queue.splice(UP.queue.indexOf(next), 1); }
    catch (err) { next.state = 'failed'; next.why = err.message; UP.failed += 1; }
    UP.card?.paint();
    await UP.card?.load();   // each one shows in the library as it lands
  }
  UP.uploading = false;
  const { added, failed } = UP;   // this run's, not ones left in the list from before
  UP.added = UP.failed = 0;
  if (added) toast(`Added ${added} file${added === 1 ? '' : 's'}`, 'success');
  if (failed) toast(`${failed} file${failed === 1 ? " wasn't" : "s weren't"} added. Settings › Media library says why.`, 'error', 8000);
  if (added || failed) UP.card?.says(`Finished: ${added} added${failed ? `, ${failed} not added` : ''}`);
}
window.addEventListener('beforeunload', (ev) => { if (UP.uploading) { ev.preventDefault(); ev.returnValue = ''; } });   // closing the tab would stop them

function mediaCard() {
  const list = h('ul', { class: 'devices' }, h('li', { class: 'muted' }, 'Loading…'));   // the toasts say what changed
  const usage = h('p', { class: 'muted tiny flush' });
  const file = h('input', { type: 'file', class: 'sr', id: 'media-upload', multiple: true, tabindex: '-1', 'aria-hidden': 'true', accept: 'image/*,video/*,.gif,.webp,.mkv' });
  const add = h('button', { type: 'button', class: 'btn' }, icon('upload'), 'Choose files…');
  // drag files in (or choose several): they wait in a queue and go to the Matrix one after another (UP, above)
  let limit = 0;   // the biggest file the Matrix takes
  const queueList = h('ul', { class: 'devices upload-queue', 'aria-label': 'Files to upload', hidden: true });
  const queueSays = h('p', { class: 'sr', role: 'status' });
  const clear = h('button', { type: 'button', class: 'btn ghost left', hidden: true, onclick: () => {
    UP.queue = UP.queue.filter((q) => q.state !== 'failed'); paintQueue(); add.focus({ preventScroll: true });
  } }, 'Clear the ones not added');
  const drop = h('div', { class: 'drop-zone' },
    h('p', { class: 'flush' }, h('span', { class: 'drop-words' }, 'Drag images and videos here, or'), add),
    h('p', { class: 'muted tiny flush' }, 'As many as you like: they upload one after another.'));
  const rows = new Map();   // one row per queued file, kept and updated in place, so nothing jumps under a finger
  const paintQueue = () => {
    queueList.hidden = !UP.queue.length;
    for (const [q, li] of rows) if (!UP.queue.includes(q)) { li.remove(); rows.delete(q); }
    for (const q of UP.queue) {
      if (!rows.has(q)) {
        rows.set(q, h('li', null,
          h('div', { class: 'grow min0' }, h('div', { class: 'strong mono small name', title: q.file.name }, q.file.name), h('div', { class: 'upload-note' })),
          h('button', { type: 'button', class: 'btn ghost icon-btn', onclick: () => { UP.queue.splice(UP.queue.indexOf(q), 1); paintQueue(); add.focus({ preventScroll: true }); } }, icon('x'))));
        queueList.append(rows.get(q));
      }
      const li = rows.get(q), failed = q.state === 'failed';
      li.className = `device upload-row ${q.state}`;
      const note = li.querySelector('.upload-note');
      note.className = `upload-note tiny ${failed ? 'danger' : 'muted'}`;
      note.textContent = failed ? q.why   // the Matrix's own words: "Couldn't add x.txt: .txt isn't supported..."
        : `${mb(q.file.size)} · ${q.state === 'uploading' ? 'Uploading…' : 'Waiting'}`;
      const x = li.querySelector('button');
      if (q.state === 'uploading' && document.activeElement === x) add.focus({ preventScroll: true });   // (it's about to go)
      x.hidden = q.state === 'uploading';
      x.setAttribute('aria-label', `${failed ? 'Dismiss' : "Don't upload"} ${q.file.name}`);
    }
    clear.hidden = UP.queue.filter((q) => q.state === 'failed').length < 2;
  };
  const enqueue = (list) => {
    const picks = [...(list || [])];   // (a copy: the browser's own list empties when the input is cleared)
    if (!picks.length) return;
    for (const f of picks) {
      const big = limit && f.size > limit;
      if (big) UP.failed += 1;
      UP.queue.push(big ? { file: f, state: 'failed', why: `Couldn't add ${f.name}: it's ${mb(f.size)}, and files can be up to ${mb(limit)}.` } : { file: f, state: 'waiting' });
    }
    paintQueue();
    pumpUploads();
  };
  // pick several (or all) and delete them together
  const picked = new Set();
  let files = [], busy = false;
  const all = h('input', { type: 'checkbox', class: 'check', id: 'media-all' });
  const delPicked = h('button', { type: 'button', class: 'btn ghost danger-btn' });
  const bar = h('div', { class: 'row between wrap gap-s media-bar' },
    h('label', { class: 'row gap-s small', for: 'media-all' }, all, 'Select all'), delPicked);
  const sync = () => {
    for (const n of [...picked]) if (!files.some((f) => f.name === n)) picked.delete(n);
    const several = files.length >= 2;   // ticking is for two or more files; one file has its own Delete
    if (!several) picked.clear();
    const n = picked.size;
    all.checked = n > 0 && n === files.length;
    all.indeterminate = n > 0 && n < files.length;
    delPicked.disabled = busy || !n;
    if (!busy) delPicked.replaceChildren(icon('trash'), n ? `Delete ${n} selected` : 'Delete selected');
    bar.hidden = !several;
    list.classList.toggle('media-single', !several);
    all.disabled = busy;
    list.querySelectorAll('li[data-name]').forEach((li) => {
      const on = picked.has(li.dataset.name);
      li.classList.toggle('picked', on);
      li.querySelector('.media-pick input').checked = on;
      li.querySelectorAll('.media-pick input, .media-del').forEach((el) => { el.disabled = busy; });
    });
    add.disabled = busy;   // (more can be queued while others upload)
  };
  const row = (f) => {
    const box = h('input', { type: 'checkbox', class: 'check', 'aria-label': `Select ${f.file}`,
      onchange: () => { if (box.checked) picked.add(f.name); else picked.delete(f.name); sync(); } });
    const li = h('li', { class: 'device media-row', 'data-name': f.name },
      h('label', { class: 'media-pick' }, box),   // a bigger area to tap than the box itself
      mediaThumb(f.name)[0],
      h('div', { class: 'grow min0' }, h('div', { class: 'strong mono small name', title: f.file }, f.file), h('div', { class: 'tiny muted' }, `${f.kind} · ${mb(f.bytes)}`)),
      h('div', { class: 'media-actions' },
        previewButton(f.name),
        h('button', { type: 'button', class: 'btn ghost danger-btn media-del', 'aria-label': `Delete ${f.file}`, onclick: () => remove([f]) }, 'Delete')));
    return li;
  };
  const load = async () => {
    try {
      const lib = await get('/api/media');
      files = lib.folders.uploaded;
      limit = lib.limits.file;
      usage.textContent = `${mb(lib.used)} of ${mb(lib.limits.library)} used · up to ${mb(lib.limits.file)} per file · ${lib.folders['built-in'].length} built-in files`;
      // the rows are rebuilt: keep the focus on the same control of the same file (a file landing mustn't drop it)
      const held = list.contains(document.activeElement) ? document.activeElement : null, heldRow = held?.closest('li[data-name]');
      const at = heldRow ? [heldRow.dataset.name, [...heldRow.querySelectorAll('input, button, a')].indexOf(held)] : null;
      list.replaceChildren(...files.map(row));
      if (at) (list.querySelector(`li[data-name="${CSS.escape(at[0])}"]`)?.querySelectorAll('input, button, a')[at[1]] || add).focus({ preventScroll: true });
      if (!files.length) list.replaceChildren(h('li', { class: 'muted' }, 'Nothing uploaded yet. Images, GIFs and short videos work.'));
    } catch (err) { files = []; list.replaceChildren(h('li', { class: 'danger' }, `Couldn't load media: ${err.message}`)); }
    sync();
  };
  /** Delete these files, one after another; ones still in use are kept and said why. */
  const remove = async (chosen) => {
    const names = chosen.map((f) => f.file);
    const ask = names.length === 1 ? `Delete ${names[0]}? This can't be undone.`
      : `Delete these ${names.length} files? This can't be undone.\n\n${names.slice(0, 8).join('\n')}${names.length > 8 ? `\n…and ${names.length - 8} more` : ''}`;
    if (busy || !confirm(ask)) return;   // one batch at a time
    busy = true; sync();
    const kept = [];
    let gone = 0;
    for (const [k, f] of chosen.entries()) {
      if (chosen.length > 1) delPicked.replaceChildren(icon('trash'), `Deleting ${k + 1} of ${chosen.length}…`);
      try { await del(`/api/media/${f.name.split('/').map(encodeURIComponent).join('/')}`); gone += 1; picked.delete(f.name); }
      catch (err) {
        if (err.status === 404) { gone += 1; picked.delete(f.name); continue; }   // already gone: that's what was wanted
        kept.push(err.message.includes(f.name) ? err.message.replace(f.name, f.file) : `${f.file}: ${err.message}`);
      }
    }
    busy = false;
    if (gone) toast(gone === 1 && chosen.length === 1 ? `Deleted ${names[0]}` : `Deleted ${gone} file${gone === 1 ? '' : 's'}`, 'success');
    if (kept.length) toast(`Couldn't delete ${kept.length === 1 ? 'one file' : `${kept.length} files`}. ${kept.join(' · ')}`, 'error', 12000);
    await load();
    add.focus({ preventScroll: true });   // the rows were redrawn: put focus somewhere steady
  };
  delPicked.addEventListener('click', () => remove(files.filter((f) => picked.has(f.name))));
  all.addEventListener('change', () => { picked.clear(); if (all.checked) files.forEach((f) => picked.add(f.name)); sync(); });
  add.addEventListener('click', () => file.click());
  file.addEventListener('change', () => { enqueue(file.files); file.value = ''; });
  sync();
  load();
  UP.card = { paint: paintQueue, load: () => load(), says: (text) => { queueSays.textContent = text; } };   // this card shows the queue now
  paintQueue();   // (uploads from before you left Settings are still here)
  const card = h('section', { class: 'card stack', 'aria-label': 'Media library', 'data-drop': '' },
    h('div', null, h('h2', { class: 'h2' }, 'Media library'), h('p', { class: 'muted small flush' }, 'Your uploaded images and videos. Preview one to see it as the panel draws it, or show it on the Matrix. Tick several to delete them together.')),
    drop, file, queueList, clear, queueSays, bar, list, usage);
  // files dragged anywhere onto the card are taken (dragged text or links are left alone)
  const carriesFiles = (ev) => [...(ev.dataTransfer?.types || [])].includes('Files');
  card.addEventListener('dragenter', (ev) => { if (!carriesFiles(ev)) return; ev.preventDefault(); drop.classList.add('over'); });
  card.addEventListener('dragover', (ev) => { if (!carriesFiles(ev)) return; ev.preventDefault(); ev.dataTransfer.dropEffect = 'copy'; drop.classList.add('over'); });
  card.addEventListener('dragleave', (ev) => { if (!card.contains(ev.relatedTarget)) drop.classList.remove('over'); });
  card.addEventListener('drop', (ev) => { if (!carriesFiles(ev)) return; ev.preventDefault(); drop.classList.remove('over'); enqueue(ev.dataTransfer.files); });
  return card;
}
