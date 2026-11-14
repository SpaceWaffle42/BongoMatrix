// Settings: safe area (with a big LED preview), device, splash, sleep, paired devices.
import { api, get, del, post, uploadMedia } from './api.js';
import { h, icon, toast, fail, clone, same, clamp, humanize, ago, toggle } from './ui.js';
import { FrameLoop } from './led.js';
import { mediaThumb, previewButton, openPreview } from './mediaview.js';
import { S, onLeave, go, saveConfig, moduleById } from './app.js';
import { helpButton } from './help.js';
import { placePicker } from './editor.js';

export function render(view) {
  view.append(h('div', { class: 'page-head' }, h('h1', { class: 'h1' }, 'Settings'),
    h('p', { class: 'muted flush' }, 'Each card saves on its own.')),
  h('div', { class: 'settings-grid' }, safeAreaCard(), h('div', { class: 'stack gap-20' }, deviceCard(), locationCard(), wifiCard(), petCard(), calendarsCard(), spotifyCard(), updateCard(), devicesCard()), h('div', { class: 'stack gap-20' }, sleepCard(), splashCard(), mediaCard())));
}

function safeAreaCard() {
  const panel = { width: S.config.panel?.cols || 64, height: S.config.panel?.rows || 32 };
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
  const bright = h('input', { type: 'range', class: 'range', id: 'set-bright', min: 1, max: 100, value: c.panel?.brightness ?? 80 });
  const bVal = h('span', { class: 'mono strong' }, `${bright.value}%`);
  bright.addEventListener('input', () => { bVal.textContent = `${bright.value}%`; });
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
    saveConfig({ name: name.value.trim() || 'Bongo Matrix', timezone: tz.value.trim() || null,
      panel: { ...c.panel, brightness: Number(bright.value) }, web: { hub_picture: hubPic.checked, badge: badge.value || null } }, 'Device settings');
  };
  return h('section', { class: 'card stack', 'aria-label': 'Device' }, h('h2', { class: 'h2' }, 'Device'),
    h('label', { class: 'field-col', for: 'set-name' }, 'Display name', name, where),
    h('label', { class: 'field-col', for: 'set-tz' }, 'Time zone', tz,
      here && here !== tz.value ? h('button', { type: 'button', class: 'link small left', onclick: () => { tz.value = here; } }, `Use this browser's: ${here}`) : null),
    h('datalist', { id: 'tz-list' }, zones.map((z) => h('option', { value: z }))),
    h('label', { class: 'field-col', for: 'set-bright' }, h('span', { class: 'row between' }, h('span', null, 'Panel brightness (daytime)'), bVal), bright),
    h('p', { class: 'muted tiny flush' }, `${c.panel?.cols || 64}×${c.panel?.rows || 32} panel · ${c.panel?.hardware_mapping || ''}`),
    h('label', { class: 'row gap-s', for: 'set-hub-pic' }, hubPic, h('span', null, 'Show what it\'s playing on the hub')),
    h('p', { class: 'muted tiny flush' }, 'The hub (matrix.local) shows a live picture of each Matrix to anyone on this Wi-Fi, including pushed messages. Its name and what\'s playing always show.'),
    h('label', { class: 'field-col', for: 'set-badge' }, "This Matrix's logo colour on the hub", h('span', { class: 'badge-pick' }, badge, badgeLook)),
    h('div', { class: 'row end' }, save),
    restartBox());
}

/** Restart, tucked away in a closed section and never in one tap: pick what to restart, then say so
 * again on the page (bongo/power.py). The display software is back in about 10 s, the Pi in about a minute. */
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

/** Wi-Fi: which network it's on, and "Reopen setup" for a new router or password (bongo/net.py). */
function wifiCard() {
  const body = h('div', { class: 'stack gap-s' }, h('p', { class: 'muted small flush' }, 'Loading…'));
  const card = h('section', { class: 'card stack', 'aria-label': 'Wi-Fi' }, h('h2', { class: 'h2' }, 'Wi-Fi'), body);
  const STATES = { connected: 'Connected', connecting: 'Looking for a saved network', setup: 'Setup is open', starting: 'Starting' };
  const show = (n) => {
    if (!n.managed) { card.hidden = true; return; }   // not a Pi running NetworkManager (a laptop, the demo)
    const what = n.wired ? 'On a cable' : n.ssid ? `On ${n.ssid}` : STATES[n.state] || n.state;
    const rows = [h('div', { class: 'row between wrap' }, h('span', { class: 'strong' }, what), h('span', { class: 'muted small' }, STATES[n.state] || ''))];
    if (n.hotspot) rows.push(h('p', { class: 'small flush' }, 'Join ', h('span', { class: 'mono strong' }, n.hotspot.ssid), ' with password ', h('span', { class: 'mono strong' }, n.hotspot.password), ' to choose a network.'));
    if (n.saved?.length) rows.push(h('p', { class: 'muted tiny flush' }, `Saved: ${n.saved.join(', ')}`));
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
  const NAMES = { '#5AC8FA': 'Blue', '#FF6FA8': 'Pink', '#4ADE80': 'Green', '#FFB547': 'Amber', '#B98CFF': 'Purple', '#FF7A59': 'Orange', '#ECEEF1': 'White' };
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
      state(c), sure);
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
    if (!s.ready) {
      list.replaceChildren(h('p', { class: 'warn small flush' }, 'This needs an update first: press Update in Settings › Software, then come back.'));
      return;
    }
    const check = h('button', { type: 'button', class: 'btn ghost small-btn' }, 'Check now');
    check.onclick = () => act(check, () => post('/api/calendars/refresh', {}), 'Checking every calendar now');
    list.replaceChildren(...[
      s.calendars.length ? h('ul', { class: 'stack gap-s', style: 'list-style:none;padding:0;margin:0' }, ...s.calendars.map(row)) : h('p', { class: 'muted small flush' }, 'No calendars yet.'),
      s.calendars.length ? h('div', { class: 'row between wrap gap-s' }, h('span', { class: 'tiny muted' }, 'Checked again every 15 minutes, when a Calendar screen comes round.'), check) : null].filter(Boolean));
    const again = focusId && list.querySelector(`li[data-cal="${focusId}"] .switch`);   // keep keyboard focus on that calendar
    if (again) again.focus();
    else if (focusId) { const head = card.querySelector('h2'); head.tabIndex = -1; head.focus(); }   // that calendar was removed
    if (!form) { form = makeForm(s); formBox.replaceChildren(form); }
    form.hidden = s.calendars.length >= s.most;
    formBox.querySelector('.cal-full')?.remove();
    if (form.hidden) formBox.append(h('p', { class: 'muted small flush cal-full' }, `That's ${s.most}, the most it takes.`));
  };
  get('/api/calendars').then(show).catch(() => list.replaceChildren(h('p', { class: 'muted small flush' }, "Couldn't load the calendars.")));
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

const SCREEN_TYPES = /\.(gif|png|jpe?g|webp|bmp|mp4|mov|webm|mkv|avi|m4v)$/i;   // bongo/media.py IMAGES | VIDEOS

/** Software updates: what's running, check and pull the latest, and (advanced) where updates come from. */
const UPDATE_WORDS = {
  requested: 'Update requested…', fetching: 'Downloading…', installing: 'Installing…', restarting: 'Restarting…',
  updated: 'Updated', up_to_date: 'Already up to date', rolled_back: 'Rolled back', failed: 'Update failed', busy: 'Busy',
};
const UPDATE_RUNNING = new Set(['requested', 'fetching', 'installing', 'restarting']);

function updateCard() {
  const card = h('section', { class: 'card stack', 'aria-label': 'Software' }, h('h2', { class: 'h2' }, 'Software'),
    h('p', { class: 'muted small flush' }, 'Loading…'));
  let info = null, poll = null;
  const stopPoll = () => { clearTimeout(poll); poll = null; };
  onLeave(stopPoll);

  const when = (iso) => { try { return new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }); } catch { return iso; } };
  async function refresh(quiet) {
    try { info = await get('/api/update'); }
    catch (err) { if (!quiet) card.replaceChildren(card.firstChild, h('p', { class: 'danger small' }, `Couldn't read the version: ${err.message}`)); return; }
    paint();
    const st = info.status?.state;
    stopPoll();
    if (UPDATE_RUNNING.has(st)) poll = setTimeout(() => refresh(true), 3000);  // the Matrix restarts mid-update: keep asking
  }

  function paint() {
    const v = info.version, st = info.status;
    const running = st && UPDATE_RUNNING.has(st.state);
    const result = h('p', { class: 'small flush', 'aria-live': 'polite' });
    const check = h('button', { type: 'button', class: 'btn' }, 'Check for updates');
    const pull = h('button', { type: 'button', class: 'btn primary', disabled: running || null }, running ? 'Updating…' : 'Pull update');
    check.onclick = async () => {
      check.disabled = true; result.className = 'small flush muted'; result.textContent = 'Checking…';
      try {
        const r = await post('/api/update/check');
        result.className = 'small flush' + (r.available ? ' warn' : ' muted');
        result.textContent = r.available ? `A new version is ready (${r.latest.slice(0, 8)}).` : 'You have the latest version.';
      } catch (err) { result.className = 'small flush danger'; result.textContent = err.message; }
      check.disabled = false;
    };
    pull.onclick = async () => {
      if (!confirm('Pull the latest version and restart the Matrix? The panel and this page go quiet for about a minute. If the new version won\'t start, the old one comes back by itself.')) return;
      try { await post('/api/update'); toast('Update started', 'success'); refresh(); }
      catch (err) { toast(err.message, 'error'); }
    };
    const stLine = st ? h('p', { class: 'small flush ' + (st.state === 'rolled_back' || st.state === 'failed' ? 'danger' : running ? 'warn' : 'muted') },
      `${UPDATE_WORDS[st.state] || st.state}: ${st.message} · ${when(st.at)}`) : null;
    card.replaceChildren(...[
      h('h2', { class: 'h2' }, 'Software'),
      h('div', { class: 'stack-xs' },
        h('span', { class: 'field-label' }, 'Running'),
        v ? h('span', { class: 'mono small' }, `${v.short} · ${when(v.date)}`) : h('span', { class: 'muted small' }, 'Unknown (not a git checkout)'),
        v ? h('span', { class: 'muted tiny' }, v.subject) : null),
      stLine,
      h('div', { class: 'row gap-s wrap' }, check, pull), result,
      sourceBox()].filter(Boolean));  // no status line until the first update
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
    return h('details', { class: 'item-more' },
      h('summary', null, 'Advanced: update source'),
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
  const usable = (n) => n.startsWith('uploaded/') || n.split('/').pop().startsWith(`${screen}_`);
  return { builtIn, uploaded, shipped, usable };
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
        const gone = f.file.endsWith('(missing)');
        const thumb = gone ? h('span', { class: 'media-thumb empty' }) : mediaThumb(f.name, opts())[0];
        return h('div', { class: 'screen-source' },
          h('label', { class: 'screen-pick', for: box.id }, box, thumb,
            h('span', { class: 'mono small name grow min0', title: f.file }, f.file),
            f.kind === 'video' ? h('span', { class: 'tag' }, 'Video') : null),
          gone ? null : previewButton(f.name, opts));
      };
      // built-in and uploaded files under their own headings, so it's clear which is which
      const section = (title, list) => (list.length ? [h('div', { class: 'screen-divider', role: 'presentation' }, `${title} · ${list.length}`), ...list.map(card)] : []);
      grid.replaceChildren(...section('Missing', missing), ...section('Built in', builtIn), ...section('Your media', uploaded));
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
    h('p', { class: 'muted tiny flush' }, 'Each time a Splash item comes round in the playlist it shows the next file from a shuffled list, so every one plays before any repeats. A GIF or video plays once through (a video up to 15 s); a still stays up about as long as your GIFs (7 s if there are none).'),
    h('p', { class: 'small flush' + (inPlaylist ? ' muted' : ' warn') }, inPlaylist
      ? `In the playlist ${inPlaylist === 1 ? 'once' : `${inPlaylist} times`}. Add more, or move it, on the Playlist page.`
      : 'Not in the playlist yet: add a Splash item on the Playlist page so it plays.'),
    bootPicker(rule));
  body.hidden = !on;
  const save = h('button', { type: 'button', class: 'btn primary' }, 'Save');
  save.onclick = () => {
    if (on && !rule.sources.length) { toast('Tick at least one file for the splash', 'error'); return; }
    saveConfig({ splash: on ? { ...rule, enabled: true } : null }, 'Splash');
  };
  return h('section', { class: 'card stack', 'aria-label': 'Splash' },
    h('div', { class: 'row between wrap' }, h('div', null, h('h2', { class: 'h2' }, 'Splash'), h('p', { class: 'muted small flush' }, 'Plays wherever you put a Splash item in the playlist.')),
      toggle(on, 'Use splash screens', (v) => { on = v; body.hidden = !v; })),
    body, h('div', { class: 'row end' }, save));
}

/** The boot screen: one Splash file played once when the Matrix starts, or none. */
function bootPicker(rule) {
  const sel = h('select', { class: 'input', id: 'sp-boot' }, h('option', { value: '' }, 'None (start straight into the playlist)'));
  sel.addEventListener('change', () => { rule.boot = sel.value || null; });
  (async () => {
    try {
      const lib = await get('/api/media');
      const { builtIn, uploaded, shipped } = screenFiles(lib, 'splash');
      rule.boot = shipped(rule.boot);
      const option = (f) => h('option', { value: f.name, selected: f.name === rule.boot || null }, `${f.file}${f.kind === 'video' ? ' (video)' : ''}`);
      const group = (label, list) => (list.length ? h('optgroup', { label }, list.map(option)) : null);
      const missing = rule.boot && ![...builtIn, ...uploaded].some((f) => f.name === rule.boot) ? [{ name: rule.boot, file: `${rule.boot} (missing)` }] : [];
      sel.append(...[group('Missing', missing), group('Built in', builtIn), group('Your media', uploaded)].filter(Boolean));
    } catch { /* the list stays at "None"; saving keeps whatever was set */ }
  })();
  const opts = () => ({ fit: rule.fit || 'fit', speed: (rule.speed ?? 100) / 100 });
  const preview = h('button', { type: 'button', class: 'btn small-btn' }, 'Preview');
  preview.onclick = () => { if (rule.boot) openPreview(rule.boot, opts()); else toast('Pick a boot screen first'); };
  return h('div', { class: 'stack-s' },
    h('label', { class: 'field-col', for: sel.id }, 'Boot screen', h('div', { class: 'row gap-s' }, sel, preview)),
    h('p', { class: 'muted tiny flush' }, 'Plays once when the Matrix starts, before the playlist: a built-in splash or anything of your own, ticked above or not.'));
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
    if (on && !sleep.sources.length) { toast('Tick at least one file for the sleep screen', 'error'); return; }
    saveConfig({ sleep: on ? { ...sleep, enabled: true, from: from.value, to: to.value, brightness: clamp(Number(bright.value) || 15, 1, 100) } : null }, 'Sleep schedule');
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

function mediaCard() {
  const list = h('ul', { class: 'devices' }, h('li', { class: 'muted' }, 'Loading…'));   // the toasts say what changed
  const usage = h('p', { class: 'muted tiny flush' });
  const file = h('input', { type: 'file', class: 'sr', id: 'media-upload', accept: 'image/*,video/*,.gif,.webp,.mkv' });
  const add = h('button', { type: 'button', class: 'btn' }, icon('upload'), 'Upload…');
  // pick several (or all) and delete them together
  const picked = new Set();
  let files = [], busy = false, uploading = false;
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
    add.disabled = busy || uploading;
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
      usage.textContent = `${mb(lib.used)} of ${mb(lib.limits.library)} used · up to ${mb(lib.limits.file)} per file · ${lib.folders['built-in'].length} built-in files`;
      list.replaceChildren(...files.map(row));
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
  file.addEventListener('change', async () => {
    const pick = file.files?.[0];
    if (!pick) return;
    uploading = true; sync();
    try { const { name } = await uploadMedia(pick); toast(`Added ${name.replace('uploaded/', '')}`, 'success'); }
    catch (err) { toast(err.message, 'error', 8000); }
    finally { uploading = false; file.value = ''; await load(); }
  });
  sync();
  load();
  return h('section', { class: 'card stack', 'aria-label': 'Media library' },
    h('div', { class: 'row between wrap' },
      h('div', null, h('h2', { class: 'h2' }, 'Media library'), h('p', { class: 'muted small flush' }, 'Your uploaded images and videos. Preview one to see it as the panel draws it, or show it on the Matrix. Tick several to delete them together.')),
      h('div', null, add, file)),
    bar, list, usage);
}
