// Settings: safe area (with a big LED preview), device, splash, sleep, paired devices.
import { get, del, uploadMedia } from './api.js';
import { h, icon, toast, fail, clone, same, clamp, humanize, ago, toggle } from './ui.js';
import { FrameLoop } from './led.js';
import { mediaThumb, previewButton, openPreview } from './mediaview.js';
import { S, onLeave, go, saveConfig } from './app.js';

export function render(view) {
  view.append(h('div', { class: 'page-head' }, h('h1', { class: 'h1' }, 'Settings'),
    h('p', { class: 'muted flush' }, 'Each card saves on its own.')),
  h('div', { class: 'settings-grid' }, safeAreaCard(), h('div', { class: 'stack gap-20' }, deviceCard(), devicesCard()), h('div', { class: 'stack gap-20' }, sleepCard(), splashCard(), mediaCard())));
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
  const save = h('button', { type: 'button', class: 'btn primary' }, 'Save');
  save.onclick = () => {
    if (tz.value && zones.length && !zones.includes(tz.value)) { toast(`“${tz.value}” isn't a time zone this browser knows. Use a name like Europe/London.`, 'error'); tz.focus(); return; }
    saveConfig({ name: name.value.trim() || 'Bongo Matrix', timezone: tz.value.trim() || null,
      panel: { ...c.panel, brightness: Number(bright.value) } }, 'Device settings');
  };
  return h('section', { class: 'card stack', 'aria-label': 'Device' }, h('h2', { class: 'h2' }, 'Device'),
    h('label', { class: 'field-col', for: 'set-name' }, 'Display name', name),
    h('label', { class: 'field-col', for: 'set-tz' }, 'Time zone', tz,
      here && here !== tz.value ? h('button', { type: 'button', class: 'link small left', onclick: () => { tz.value = here; } }, `Use this browser's: ${here}`) : null),
    h('datalist', { id: 'tz-list' }, zones.map((z) => h('option', { value: z }))),
    h('label', { class: 'field-col', for: 'set-bright' }, h('span', { class: 'row between' }, h('span', null, 'Panel brightness (daytime)'), bVal), bright),
    h('p', { class: 'muted tiny flush' }, `${c.panel?.cols || 64}×${c.panel?.rows || 32} panel · ${c.panel?.hardware_mapping || ''}`),
    h('div', { class: 'row end' }, save));
}

const SCREEN_TYPES = /\.(gif|png|jpe?g|webp)$/i;

/** Pick the files a Splash or Sleep screen draws from (GIFs and stills, like v2), plus speed and fit. */
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
      // built-in files serve one screen each (splash_* or sleep_*); uploads can go on either
      const files = [...lib.folders.uploaded, ...lib.folders['built-in'].filter((f) => f.screen === screen)].filter((f) => SCREEN_TYPES.test(f.name));
      const known = new Set(files.map((f) => f.name));
      // a built-in file meant for the other screen (or no screen) is dropped, never offered here
      rule.sources = rule.sources.filter((n) => known.has(n) || !n.startsWith('built-in/'));
      const missing = rule.sources.filter((n) => !known.has(n)).map((n) => ({ name: n, file: `${n} (missing)` }));
      grid.replaceChildren(...[...missing, ...files].map((f, i) => {
        const box = h('input', { type: 'checkbox', id: `${idPrefix}-src-${i}`, checked: rule.sources.includes(f.name) });
        box.addEventListener('change', () => {
          rule.sources = box.checked ? [...new Set([...rule.sources, f.name])] : rule.sources.filter((n) => n !== f.name);
        });
        const opts = () => ({ fit: rule.fit || 'fit', speed: (rule.speed ?? 100) / 100 });
        const thumb = f.file.endsWith('(missing)') ? h('span', { class: 'media-thumb empty' }) : mediaThumb(f.name, opts())[0];
        return h('div', { class: 'screen-source' },
          h('label', { class: 'screen-pick', for: box.id }, box, thumb,
            h('span', { class: 'stack-xs grow min0' }, h('span', { class: 'mono small name', title: f.file }, f.file),
              h('span', { class: 'tag' }, f.name.startsWith('built-in/') ? 'built-in' : 'uploaded'))),
          f.file.endsWith('(missing)') ? null : previewButton(f.name, opts));
      }));
      if (!files.length && !missing.length) grid.replaceChildren(h('p', { class: 'muted small flush' }, 'No GIFs or images yet. Upload some in the Media library.'));
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
    h('p', { class: 'muted tiny flush' }, 'Each time a Splash item comes round in the playlist it shows the next file from a shuffled list, so every one plays before any repeats. A GIF plays once through; a still stays up about as long as your GIFs (7 s if there are none).'),
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
      const files = [...lib.folders['built-in'].filter((f) => f.screen === 'splash'), ...lib.folders.uploaded].filter((f) => SCREEN_TYPES.test(f.name));
      if (rule.boot && !files.some((f) => f.name === rule.boot)) files.unshift({ name: rule.boot, file: `${rule.boot} (missing)` });
      sel.append(...files.map((f) => h('option', { value: f.name, selected: f.name === rule.boot || null }, f.file)));
    } catch { /* the list stays at "None"; saving keeps whatever was set */ }
  })();
  const opts = () => ({ fit: rule.fit || 'fit', speed: (rule.speed ?? 100) / 100 });
  const preview = h('button', { type: 'button', class: 'btn small-btn' }, 'Preview');
  preview.onclick = () => { if (rule.boot) openPreview(rule.boot, opts()); else toast('Pick a boot screen first'); };
  return h('div', { class: 'stack-s' },
    h('label', { class: 'field-col', for: sel.id }, 'Boot screen', h('div', { class: 'row gap-s' }, sel, preview)),
    h('p', { class: 'muted tiny flush' }, 'Plays once when the Matrix starts, before the playlist. It can be any Splash file, ticked above or not.'));
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
  const list = h('ul', { class: 'devices', 'aria-live': 'polite' }, h('li', { class: 'muted' }, 'Loading…'));
  const usage = h('p', { class: 'muted tiny flush' });
  const file = h('input', { type: 'file', class: 'sr', id: 'media-upload', accept: 'image/*,video/*,.gif,.webp,.mkv' });
  const add = h('button', { type: 'button', class: 'btn' }, icon('upload'), 'Upload…');
  const load = async () => {
    try {
      const lib = await get('/api/media');
      usage.textContent = `${mb(lib.used)} of ${mb(lib.limits.library)} used · up to ${mb(lib.limits.file)} per file · ${lib.folders['built-in'].length} built-in files`;
      list.replaceChildren(...lib.folders.uploaded.map((f) => h('li', { class: 'device' },
        mediaThumb(f.name)[0],
        h('div', { class: 'grow min0' }, h('div', { class: 'strong mono small name', title: f.file }, f.file), h('div', { class: 'tiny muted' }, `${f.kind} · ${mb(f.bytes)}`)),
        previewButton(f.name),
        h('button', { type: 'button', class: 'btn ghost danger-btn', 'aria-label': `Delete ${f.file}`, onclick: () => remove(f) }, 'Delete'))));
      if (!lib.folders.uploaded.length) list.replaceChildren(h('li', { class: 'muted' }, 'Nothing uploaded yet. Images, GIFs and short videos work.'));
    } catch (err) { list.replaceChildren(h('li', { class: 'danger' }, `Couldn't load media: ${err.message}`)); }
  };
  const remove = async (f) => {
    if (!confirm(`Delete ${f.file}? This can't be undone.`)) return;
    try { await del(`/api/media/${f.name.split('/').map(encodeURIComponent).join('/')}`); toast(`Deleted ${f.file}`); load(); }
    catch (err) { fail("Couldn't delete it")(err); }
  };
  add.addEventListener('click', () => file.click());
  file.addEventListener('change', async () => {
    const pick = file.files?.[0];
    if (!pick) return;
    add.disabled = true;
    try { const { name } = await uploadMedia(pick); toast(`Added ${name.replace('uploaded/', '')}`, 'success'); load(); }
    catch (err) { toast(err.message, 'error', 8000); }
    finally { add.disabled = false; file.value = ''; }
  });
  load();
  return h('section', { class: 'card stack', 'aria-label': 'Media library' },
    h('div', { class: 'row between wrap' },
      h('div', null, h('h2', { class: 'h2' }, 'Media library'), h('p', { class: 'muted small flush' }, 'Your uploaded images and videos. Preview one to see it as the panel draws it, or show it on the Matrix.')),
      h('div', null, add, file)),
    list, usage);
}
