// Now playing (hero preview, transport, quick controls, push) and the playlist filmstrip.
import { post, previewPath } from './api.js';
import { h, icon, toast, fail, same, humanize, dur, mmss, clock, reducedMotion, segmented, toggle, store } from './ui.js';
import { FrameLoop } from './led.js';
import { helpButton } from './help.js';
import { S, bus, onLeave, go, href, moduleById, styleOf, entryName, draftChanged, dirtyCount, applyDraft, discardDraft, normalise } from './app.js';

const COLOURS = { clock: '#FF7A3D', weather: '#5B9BE8', announcements: '#FFB547', calendar: '#E879B7',
  rss: '#8B95A5', text: '#A78BFA', astronomy: '#4ADE80', media: '#2DD4BF' };
export const colourOf = (id) => COLOURS[id] || `hsl(${[...String(id)].reduce((a, c) => a + c.charCodeAt(0) * 37, 0) % 360} 60% 62%)`;
const matches = (a, b) => a && b && a.module === b.module && (a.style || null) === (b.style || null) && same(a.settings || {}, b.settings || {});
const playingIndex = (list) => {
  const live = S.state?.live, i = live?.index;
  if (Number.isInteger(i) && list[i] && matches(list[i], live.entry)) return i;  // exact slot from the player
  return list.findIndex((e) => e.enabled !== false && matches(e, live?.entry));
};

export function render(view) {
  const live = () => S.state?.live || {};
  let showSafe = store.get('heroSafe', false);

  // -- hero ------------------------------------------------------------------------------
  const canvas = h('canvas', { class: 'led-canvas', role: 'img', 'aria-label': 'Live view of the panel' });
  const title = h('h1', { class: 'h1 hero-title' }, '…');
  const styleTag = h('span', { class: 'tag' });
  const bar = h('div', { class: 'progress-fill' });
  const elapsedEl = h('span'), nextEl = h('span');
  const pauseBtn = h('button', { type: 'button', class: 'btn icon-btn primary-light play' });
  const nextBtn = h('button', { type: 'button', class: 'btn icon-btn', 'aria-label': 'Next module' }, icon('next'));
  const editBtn = h('button', { type: 'button', class: 'btn ghost' }, 'Edit');
  const safeBtn = h('button', { type: 'button', class: 'chip', 'aria-pressed': String(showSafe) }, 'Safe area');
  const stateNote = h('p', { class: 'muted small hero-note', 'aria-live': 'polite' });
  const loop = new FrameLoop(canvas, { path: () => '/api/frame.png', interval: 125,
    led: () => ({ safeArea: S.state?.safe_area, panel: S.state?.panel, showSafe }) });
  onLeave(() => loop.stop());
  safeBtn.onclick = () => { showSafe = !showSafe; store.set('heroSafe', showSafe); safeBtn.setAttribute('aria-pressed', String(showSafe)); loop.redraw(); };

  pauseBtn.onclick = () => control({ action: live().paused ? 'resume' : 'pause' });
  nextBtn.onclick = () => control({ action: 'next' }, 'Skipping to the next module');
  editBtn.onclick = () => { const i = playingIndex(S.draft); if (i >= 0) go(`/edit/${i}`); };

  const hero = h('section', { class: 'card hero', 'aria-label': 'Now playing' },
    h('div', { class: 'row between wrap' },
      h('div', null, h('div', { class: 'eyebrow' }, 'On the panel now'), h('div', { class: 'row gap-s' }, title, styleTag)),
      safeBtn),
    h('div', { class: 'led-frame' }, canvas),
    h('div', { class: 'progress' }, h('div', { class: 'progress-track' }, bar),
      h('div', { class: 'row between mono small muted' }, elapsedEl, nextEl)),
    stateNote,
    h('div', { class: 'row between wrap' }, h('div', { class: 'row gap-s' }, pauseBtn, nextBtn), editBtn));

  // -- quick controls --------------------------------------------------------------------
  const bVal = h('span', { class: 'mono strong' });
  const slider = h('input', { type: 'range', min: 1, max: 100, id: 'bright', class: 'range' });
  let sliderTouched = 0, bTimer = 0;
  const bMode = h('div', { class: 'seg', role: 'group', 'aria-label': 'Brightness mode' });
  const autoBtn = h('button', { type: 'button' }, 'Auto'), manBtn = h('button', { type: 'button' }, 'Manual');
  bMode.append(autoBtn, manBtn);
  slider.addEventListener('input', () => {
    sliderTouched = Date.now(); bVal.textContent = `${slider.value}%`;
    autoBtn.setAttribute('aria-pressed', 'false'); manBtn.setAttribute('aria-pressed', 'true'); slider.classList.remove('dim');
    clearTimeout(bTimer);
    bTimer = setTimeout(() => control({ action: 'brightness', value: Number(slider.value) }), 200);
  });
  autoBtn.onclick = () => control({ action: 'brightness', value: null }, 'Brightness follows the schedule again');
  manBtn.onclick = () => control({ action: 'brightness', value: Number(slider.value) });
  const sleepBtn = h('button', { type: 'button', class: 'btn tile' });
  sleepBtn.onclick = () => {
    if (live().sleep_until) control({ action: 'wake' }, 'Waking the panel');
    else control({ action: 'sleep', minutes: sleepMinutes() }, 'Panel is going to sleep');
  };
  const quick = h('section', { class: 'card stack', 'aria-label': 'Quick controls' },
    h('h2', { class: 'h2' }, 'Quick controls'),
    h('label', { class: 'field-col', for: 'bright' }, h('span', { class: 'row between' }, h('span', null, 'Brightness'), bVal), slider),
    bMode, sleepBtn);

  // -- push a message --------------------------------------------------------------------
  const msg = h('input', { class: 'input', id: 'push-text', maxlength: 200, placeholder: "Dinner's ready!" });
  const ttl = h('input', { class: 'input', id: 'push-title', maxlength: 40, placeholder: 'Optional' });
  const secs = h('select', { class: 'input', 'aria-label': 'Show for' },
    [10, 15, 30, 60, 120].map((s) => h('option', { value: s, selected: s === 15 }, `${s} seconds`)));
  const pushForm = h('form', { class: 'card stack', 'aria-label': 'Push a message' },
    h('div', null, h('h2', { class: 'h2' }, 'Push a message'), h('p', { class: 'muted small flush' }, 'Interrupts the playlist, then resumes.')),
    h('label', { class: 'field-col', for: 'push-title' }, 'Title', ttl),
    h('label', { class: 'field-col', for: 'push-text' }, 'Message', msg),
    h('div', { class: 'row gap-s wrap' }, secs, h('button', { class: 'btn primary grow', type: 'submit' }, 'Show now')));
  pushForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!msg.value.trim()) { msg.focus(); toast('Type a message first', 'error'); return; }
    try {
      await post('/api/push', { text: msg.value.trim(), title: ttl.value.trim(), seconds: Number(secs.value) });
      toast('Message sent to the panel', 'success'); msg.value = '';
    } catch (err) { fail("Couldn't push the message")(err); }
  });

  // -- up next (phone "Now" tab) -----------------------------------------------------------
  const upNext = h('ul', { class: 'upnext' });
  const upCard = h('section', { class: 'card stack phone-only', 'aria-label': 'Up next' },
    h('div', { class: 'row between' }, h('h2', { class: 'h2' }, 'Up next'), h('a', { href: href('/playlist'), 'data-route': '/playlist', class: 'small' }, 'Playlist')), upNext);

  const playlist = renderPlaylist();
  view.append(h('div', { class: 'now-grid now-top' }, hero, h('div', { class: 'side-col' }, quick, pushForm, upCard)), playlist.el);

  // -- live updates ------------------------------------------------------------------------
  function paint() {
    const L = live(), e = L.entry;
    const asleep = !!L.sleep_until;
    title.textContent = asleep ? 'Sleeping' : e ? entryName(e) : 'Starting up…';
    styleTag.textContent = e ? humanize(styleOf(e)) : '';
    styleTag.hidden = !e || asleep;
    pauseBtn.replaceChildren(icon(L.paused ? 'play' : 'pause'));
    pauseBtn.setAttribute('aria-label', L.paused ? 'Resume rotation' : 'Pause rotation');
    const i = playingIndex(S.draft || []);
    editBtn.hidden = i < 0;
    editBtn.textContent = `Edit ${entryName(e)}`;
    stateNote.textContent = [L.paused && 'Rotation paused: this module stays on until you resume.',
      asleep && `Asleep until ${clock(new Date(L.sleep_until))}.`, L.pushes && `${L.pushes} message${L.pushes > 1 ? 's' : ''} queued.`].filter(Boolean).join(' ');
    if (Date.now() - sliderTouched > 2500 && document.activeElement !== slider) {
      const auto = L.brightness == null;
      slider.value = auto ? (S.config?.panel?.brightness ?? 80) : L.brightness;
      slider.classList.toggle('dim', auto);
      bVal.textContent = auto ? `Auto · ${slider.value}%` : `${slider.value}%`;
      autoBtn.setAttribute('aria-pressed', String(auto)); manBtn.setAttribute('aria-pressed', String(!auto));
    }
    sleepBtn.replaceChildren(h('span', { class: 'strong' }, asleep ? 'Wake now' : 'Sleep now'),
      h('span', { class: 'muted small' }, asleep ? `Asleep until ${clock(new Date(L.sleep_until))}` : `Until ${/^\d{1,2}:\d{2}$/.test(S.config?.sleep?.to || '') ? S.config.sleep.to : clock(new Date(Date.now() + sleepMinutes() * 60000))}`));
    const list = S.config?.playlist?.map(normalise).filter((x) => x.enabled) || [];
    const at = list.findIndex((x) => matches(x, e));
    const rest = at < 0 ? list : [...list.slice(at + 1), ...list.slice(0, at)];
    upNext.replaceChildren(...rest.slice(0, 4).map((x) => h('li', null, h('span', { class: 'swatch', style: `background:${colourOf(x.module)}` }),
      h('span', { class: 'grow' }, entryName(x), h('span', { class: 'muted small' }, ` · ${humanize(styleOf(x))}`)), h('span', { class: 'mono small muted' }, dur(x.seconds)))));
    tick();
    playlist.markPlaying();
  }
  function tick() {
    const L = live(), e = L.entry;
    const el = Math.min(e?.seconds || 0, (L.elapsed || 0) + (L.paused ? 0 : (performance.now() - S.polledAt) / 1000));
    bar.style.width = e?.seconds ? `${Math.min(100, (el / e.seconds) * 100)}%` : '0';
    elapsedEl.textContent = `${mmss(el)} / ${mmss(e?.seconds || 0)}`;
    nextEl.textContent = L.paused ? 'Paused' : L.next ? `Next: ${entryName(L.next)} in ${mmss((e?.seconds || 0) - el)}` : '';
  }
  const timer = setInterval(tick, 250);
  bus.addEventListener('state', paint);
  onLeave(() => { clearInterval(timer); bus.removeEventListener('state', paint); });
  paint();

  async function control(body, message) {
    try {
      const res = await post('/api/control', body);
      if (res?.live && S.state) { S.state.live = res.live; S.polledAt = performance.now(); paint(); }
      if (message) toast(message);
    } catch (err) { fail("The panel didn't respond")(err); }
  }
}

/** Minutes until the configured sleep "to" time (e.g. 07:00), else 8 hours. */
function sleepMinutes() {
  const to = S.config?.sleep?.to;
  if (!/^\d{1,2}:\d{2}$/.test(to || '')) return 480;
  const [hh, mm] = to.split(':').map(Number), now = new Date(), end = new Date(now);
  end.setHours(hh, mm, 0, 0);
  if (end <= now) end.setDate(end.getDate() + 1);
  return Math.max(1, Math.round((end - now) / 60000));
}

// -- playlist filmstrip --------------------------------------------------------------------------
function renderPlaylist() {
  const summary = h('span', { class: 'mono small soft' });
  const strip = h('div', { class: 'strip', role: 'list' });
  const scroller = h('div', { class: 'strip-scroll' }, strip);
  const share = h('div', { class: 'share-bar', role: 'img' });
  const legend = h('ul', { class: 'legend' });
  const loopLabel = h('span');
  const problems = h('div', { class: 'error-box', role: 'alert', hidden: true });
  const actions = h('div', { class: 'row gap-s wrap end' });
  const addDialog = buildAddDialog();
  let loops = [];
  onLeave(() => loops.forEach((l) => l.stop()));

  const el = h('section', { class: 'card stack', id: 'playlist', 'aria-label': 'Playlist' },
    h('div', { class: 'row between wrap align-end' },
      h('div', null, h('h2', { class: 'h2 big' }, 'Playlist'),
        h('p', { class: 'muted small flush' }, 'Drag the handle (or use the arrows) to reorder. Changes stay a draft until you apply them.')),
      h('div', { class: 'row gap-s wrap' }, summary,
        h('button', { type: 'button', class: 'btn', onclick: () => addDialog.open() }, icon('plus'), 'Add module'))),
    problems, scroller,
    h('div', null, share, h('div', { class: 'row between mono tiny muted mt-xs' }, h('span', null, '0:00'), loopLabel), legend),
    actions, addDialog.el);

  function paint() {
    loops.forEach((l) => l.stop()); loops = [];
    const d = S.draft || [];
    const on = d.filter((e) => e.enabled);
    const total = on.reduce((a, e) => a + Number(e.seconds || 0), 0);
    summary.textContent = `${on.length} on · loop ${dur(total, true)}`;
    loopLabel.textContent = `One full loop · ${dur(total, true)}`;
    const bad = new Map();
    for (const p of S.problems) { const m = /^playlist\[(\d+)\]:?\s*(.*)$/.exec(p); if (m) bad.set(Number(m[1]), m[2]); }
    problems.hidden = !S.problems.length;
    problems.replaceChildren(h('strong', null, "The Matrix didn't accept this playlist:"), h('ul', null, S.problems.map((p) => h('li', null, p))));
    strip.replaceChildren(...d.map((e, i) => cardFor(e, i, bad.get(i))),
      h('button', { type: 'button', class: 'add-tile', onclick: () => addDialog.open() }, icon('plus'), 'Add module'));
    share.setAttribute('aria-label', `Share of one loop: ${on.map((e) => `${entryName(e)} ${Math.round((e.seconds / (total || 1)) * 100)}%`).join(', ') || 'nothing enabled'}`);
    share.replaceChildren(...on.map((e) => h('div', { class: 'share-seg', title: `${entryName(e)} · ${dur(e.seconds, true)}`,
      style: `flex:${Number(e.seconds) || 0.01} 1 0;background:${colourOf(e.module)}` })));
    legend.replaceChildren(...on.map((e) => h('li', null, h('span', { class: 'swatch', style: `background:${colourOf(e.module)}` }),
      `${entryName(e)} `, h('span', { class: 'mono muted' }, `${Math.round((e.seconds / (total || 1)) * 100)}%`))));
    if (!on.length) share.append(h('div', { class: 'share-seg empty', style: 'flex:1' }));
    const n = dirtyCount();
    actions.replaceChildren(...(n ? [h('span', { class: 'pill-warn' }, `${n} unsaved change${n === 1 ? '' : 's'}`),
      h('button', { type: 'button', class: 'btn ghost', onclick: discardDraft }, 'Discard'),
      h('button', { type: 'button', class: 'btn primary', onclick: applyDraft }, 'Apply to Matrix')] : []));
    markPlaying();
  }

  function markPlaying() {
    const i = playingIndex(S.draft || []);
    strip.querySelectorAll('.pl-card').forEach((c) => {
      const on = Number(c.dataset.i) === i;
      c.classList.toggle('playing', on);
      c.querySelector('.badge-playing').hidden = !on;
    });
  }

  function cardFor(e, i, problem) {
    const canvas = h('canvas', { class: 'led-canvas mini', 'aria-hidden': 'true' });
    const mod = moduleById(e.module);
    const move = (to) => { const d = S.draft; const [x] = d.splice(i, 1); d.splice(to, 0, x); draftChanged(); requestAnimationFrame(() => strip.children[to]?.querySelector(to < i ? '.mv-up' : '.mv-down')?.focus()); };
    const handle = h('button', { type: 'button', class: 'handle', 'aria-label': `Drag to reorder ${entryName(e)}`, title: 'Drag to reorder' }, icon('grip'));
    const art = h('article', { class: `pl-card${e.enabled ? '' : ' off'}${problem ? ' bad' : ''}`, role: 'listitem', 'data-i': i,
      'aria-label': `${i + 1}. ${entryName(e)}` },
      h('div', { class: 'row between' }, handle, h('span', { class: 'badge-playing', hidden: true }, 'PLAYING'), h('span', { class: 'mono tiny muted' }, `#${i + 1}`)),
      h('div', { class: 'led-frame mini' }, canvas),
      h('div', { class: 'pl-text' }, h('span', { class: 'strong' }, entryName(e), mod?.proposed ? h('span', { class: 'tag' }, 'Proposed') : null),
        h('span', { class: 'mono tiny muted' }, `${humanize(styleOf(e))} · ${e.enabled ? dur(e.seconds, true) : 'skipped'}`)),
      problem ? h('p', { class: 'tiny danger flush' }, problem) : null,
      !mod ? h('p', { class: 'tiny danger flush' }, 'Module not installed') : null,
      toggle(e.enabled, e.enabled ? 'In rotation' : 'Off', (v) => { e.enabled = v; draftChanged(); }),
      h('div', { class: 'pl-actions' },
        h('button', { type: 'button', class: 'mini-btn mv-up', 'aria-label': 'Move earlier', disabled: i === 0, onclick: () => move(i - 1) }, icon('left')),
        h('button', { type: 'button', class: 'mini-btn mv-down', 'aria-label': 'Move later', disabled: i === S.draft.length - 1, onclick: () => move(i + 1) }, icon('right')),
        h('a', { class: 'mini-btn', href: href(`/edit/${i}`), 'data-route': `/edit/${i}`, 'aria-label': `Edit ${entryName(e)}` }, icon('edit')),
        h('button', { type: 'button', class: 'mini-btn danger', 'aria-label': `Remove ${entryName(e)}`, onclick: () => {
          S.draft.splice(i, 1); draftChanged(); toast(`Removed ${entryName(e)}. Discard to undo.`);
        } }, icon('trash'))));
    handle.addEventListener('pointerdown', (ev) => startDrag(ev, art, handle));
    if (mod) {
      loops.push(new FrameLoop(canvas, { path: () => previewPath(e), interval: reducedMotion() ? Infinity : 500 }));
    }
    return art;
  }

  function startDrag(ev, art, handle) {
    if (ev.button !== 0) return;
    ev.preventDefault();
    // Listen on the window: moving the card in the DOM drops pointer capture.
    art.classList.add('dragging');
    document.body.classList.add('drag-active');
    const from = Number(art.dataset.i);
    let raf = 0, lastX = ev.clientX;
    const scrollEdge = () => {
      const r = scroller.getBoundingClientRect();
      if (lastX < r.left + 48) scroller.scrollLeft -= 12; else if (lastX > r.right - 48) scroller.scrollLeft += 12;
      raf = requestAnimationFrame(scrollEdge);
    };
    raf = requestAnimationFrame(scrollEdge);
    const moveTo = (x) => {
      lastX = x;
      const cards = [...strip.querySelectorAll('.pl-card')].filter((c) => c !== art);
      const before = cards.find((c) => { const r = c.getBoundingClientRect(); return x < r.left + r.width / 2; });
      strip.insertBefore(art, before || strip.querySelector('.add-tile'));
    };
    const onMove = (e) => { if (e.pointerId === ev.pointerId) moveTo(e.clientX); };
    const end = () => {
      cancelAnimationFrame(raf);
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', end);
      window.removeEventListener('pointercancel', end);
      art.classList.remove('dragging');
      document.body.classList.remove('drag-active');
      const order = [...strip.querySelectorAll('.pl-card')].map((c) => Number(c.dataset.i));
      if (order.indexOf(from) !== from) { S.draft = order.map((k) => S.draft[k]); draftChanged(); }
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', end);
    window.addEventListener('pointercancel', end);
  }

  bus.addEventListener('draft', paint);
  onLeave(() => bus.removeEventListener('draft', paint));
  paint();
  return { el, markPlaying };
}

function buildAddDialog() {
  const modSel = h('select', { class: 'input', id: 'add-module' });
  const styleBox = h('div');
  const seconds = h('input', { class: 'input', type: 'number', min: 3, max: 3600, value: 30, id: 'add-seconds' });
  let style = null;
  const paintStyles = () => {
    const m = moduleById(modSel.value);
    style = m?.default_style || null;
    styleBox.replaceChildren(h('div', { class: 'field-col' }, h('span', null, 'Style'),
      segmented(Object.keys(m?.styles || {}), style, (v) => { style = v; }, 'Style')));
  };
  modSel.addEventListener('change', paintStyles);
  const dlg = h('dialog', { class: 'dialog', 'aria-labelledby': 'add-title' },
    h('form', { method: 'dialog', class: 'stack' },
      h('h2', { class: 'h2', id: 'add-title' }, 'Add a module'),
      h('label', { class: 'field-col', for: 'add-module' }, 'Module', modSel),
      h('p', { class: 'muted small flush', id: 'add-desc' }),
      h('div', { id: 'add-help' }),
      styleBox,
      h('label', { class: 'field-col', for: 'add-seconds' }, 'Seconds on the panel', seconds),
      h('div', { class: 'row gap-s end' },
        h('button', { type: 'button', class: 'btn ghost', onclick: () => dlg.close() }, 'Cancel'),
        h('button', { type: 'submit', class: 'btn primary', value: 'add' }, 'Add to playlist'))));
  modSel.addEventListener('change', () => {
    const m = moduleById(modSel.value);
    dlg.querySelector('#add-desc').textContent = m?.description || '';
    dlg.querySelector('#add-help').replaceChildren(...(m?.help ? [h('div', { class: 'note' }, icon('info'),
      h('div', { class: 'stack gap-s' }, h('span', null, m.help.required ? `${m.name} needs setting up once (about five minutes) before it shows anything. Done it already? You're all set.` : `Not sure what to put in? There's a step-by-step guide.`),
        h('div', null, helpButton(m.help, { label: 'Show me how' })))) ] : []));
  });
  dlg.addEventListener('close', () => {
    if (dlg.returnValue !== 'add') return;
    S.draft.push(normalise({ module: modSel.value, style, seconds: Math.max(3, Number(seconds.value) || 30), settings: {} }));
    draftChanged();
    toast(`Added ${entryName({ module: modSel.value })}. Apply to send it to the Matrix.`);
    requestAnimationFrame(() => document.querySelector('.pl-card:last-of-type')?.scrollIntoView({ behavior: reducedMotion() ? 'auto' : 'smooth', inline: 'center', block: 'nearest' }));
  });
  return {
    el: dlg,
    open() {
      modSel.replaceChildren(...(S.modules || []).map((m) => h('option', { value: m.id }, m.proposed ? `${m.name} (proposed)` : m.name)));
      modSel.dispatchEvent(new Event('change'));
      dlg.returnValue = '';
      if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
    },
  };
}
