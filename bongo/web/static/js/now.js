// Now playing (hero preview, transport, quick controls, push) and the playlist filmstrip.
import { post, previewPath } from './api.js';
import { h, icon, toast, fail, same, humanize, dur, mmss, reducedMotion, segmented, toggle, store } from './ui.js';
import { FrameLoop } from './led.js';
import { helpButton, troubleNote, creditLine } from './help.js';
import { S, bus, onLeave, go, href, moduleById, moduleOff, styleOf, entryName, draftChanged, dirtyCount, applyDraft, discardDraft, normalise, matrixClock, problemOf, problemLine } from './app.js';
import { playsAt, nextStart, short, spoken, gapWarning, sleepTime, mins, hhmm } from './times.js';

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
  const onNow = h('div', { class: 'eyebrow' }, 'On the panel now');   // (not while the editor is previewing a draft on the panel)
  const progress = h('div', { class: 'progress' }, h('div', { class: 'progress-track' }, bar),
    h('div', { class: 'row between mono small muted' }, elapsedEl, nextEl));
  // the module's own buttons (its ``actions``: an aquarium's Feed), only while it is the one on the panel
  const acts = h('div', { class: 'row gap-s wrap hero-acts', role: 'group', hidden: true });
  const actsSay = h('p', { class: 'sr', role: 'status' });
  let actsFor = '', actsBusy = false;
  const paintActs = (e, off) => {
    const m = e && !off ? moduleById(e.module) : null, list = m?.actions || [];
    const key = list.length ? `${m.id}:${list.map((a) => a.name).join()}` : '';
    if (key === actsFor) return;      // the same buttons: left alone, so nothing moves under a finger
    actsFor = key;
    if (acts.contains(document.activeElement)) pauseBtn.focus({ preventScroll: true });   // (they are about to change or go)
    acts.hidden = !list.length;
    if (list.length) acts.setAttribute('aria-label', `${m.name} buttons`);
    acts.replaceChildren(...list.map((a) => h('button', { type: 'button', class: 'btn', onclick: async () => {
      if (actsBusy) return;           // one press at a time (not disabled: that would drop the keyboard's focus)
      actsBusy = true; setTimeout(() => { actsBusy = false; }, 300);
      actsSay.textContent = '';
      try { await post('/api/act', { module: m.id, name: a.name }); setTimeout(() => { actsSay.textContent = `${a.label}: done`; }, 50); }
      catch (err) { fail(a.label)(err); }
    } }, a.label)));
  };
  const idleLink = h('a', { href: href('/playlist'), 'data-route': '/playlist', class: 'small idle-link', hidden: true }, 'Open the playlist');
  const loop = new FrameLoop(canvas, { path: () => '/api/frame.png', interval: 125,
    led: () => ({ safeArea: S.state?.safe_area, panel: S.state?.panel, showSafe }) });
  onLeave(() => loop.stop());
  safeBtn.onclick = () => { showSafe = !showSafe; store.set('heroSafe', showSafe); safeBtn.setAttribute('aria-pressed', String(showSafe)); loop.redraw(); };

  pauseBtn.onclick = () => control({ action: live().paused ? 'resume' : 'pause' });
  nextBtn.onclick = () => control({ action: 'next' }, 'Skipping to the next module');
  editBtn.onclick = () => { const i = playingIndex(S.draft); if (i >= 0) go(`/edit/${i}`); };

  const hero = h('section', { class: 'card hero', 'aria-label': 'Now playing' },
    h('div', { class: 'row between wrap' },
      h('div', null, onNow, h('div', { class: 'row gap-s' }, title, styleTag)),
      safeBtn),
    h('div', { class: 'led-frame' }, canvas),
    progress,
    stateNote, idleLink,
    h('div', { class: 'row between wrap' }, h('div', { class: 'row gap-s' }, pauseBtn, nextBtn), editBtn),
    acts, actsSay);   // (last: Pause and Next never move when these come and go)

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
    if (live().sleep_until || live().asleep) control({ action: 'wake' }, 'Waking the panel');
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
      toast(live().preview ? 'Message queued: it shows when the preview on the panel ends' : 'Message sent to the panel', 'success'); msg.value = ''; ttl.value = '';
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
    const asleep = !!L.sleep_until || !!L.asleep;   // Sleep now, or the sleep hours (Settings > Sleep)
    const sleepTo = sleepTime(S.config)?.to || '';   // (only while the sleep hours are switched on)
    // by the Matrix's clock, not this phone's: they can be in different time zones
    const wakesAt = L.sleep_hours && sleepTo ? sleepTo : L.sleep_until ? matrixIn((epoch(L.sleep_until) - matrixEpoch()) / 60000) : sleepTo;   // the later of the two
    // nothing is set to play now: every screen in rotation has times (Timing > When it plays), and none is now
    const now = matrixClock();
    const timed = (S.config?.playlist || []).map(normalise).filter((x) => x.enabled && !moduleOff(x.module) && x.times.length);
    const soonest = timed.map((x) => ({ x, at: nextStart(x.times, now) })).filter((n) => n.at)
      .sort((a, b) => ((a.at < now) - (b.at < now)) || a.at.localeCompare(b.at))[0];
    const idle = !!L.idle && !e && !asleep;
    title.textContent = asleep ? 'Sleeping' : idle ? (soonest ? `Nothing is set to play until ${soonest.at}` : 'Nothing is set to play') : e ? entryName(e) : 'Starting up…';
    idleLink.hidden = !idle;
    progress.hidden = idle || asleep;   // nothing on, or asleep: no time bar, and nothing to pause or skip
    pauseBtn.disabled = nextBtn.disabled = idle || asleep;
    pauseBtn.hidden = nextBtn.hidden = asleep;
    styleTag.textContent = e ? humanize(styleOf(e)) : '';
    styleTag.hidden = !e || asleep;
    paintActs(e, idle || (asleep && !e));
    pauseBtn.replaceChildren(icon(L.paused ? 'play' : 'pause'));
    pauseBtn.setAttribute('aria-label', L.paused ? 'Resume rotation' : 'Pause rotation');
    const i = playingIndex(S.draft || []);
    editBtn.hidden = i < 0;
    editBtn.textContent = `Edit ${entryName(e)}`;
    const stays = L.staying && !L.paused && e && `${entryName(e)} is live, so it stays on until it's over. Next moves on.`;   // (Style.stay)
    // Preview on the panel (the editor): the panel shows someone's unsaved draft; this picture stays the playlist underneath
    const pv = L.preview;
    onNow.textContent = pv ? 'Under a preview on the panel' : 'On the panel now';
    stateNote.textContent = [stays, pv && `The panel is showing a preview of ${entryName(pv)} from the editor${pv.by ? ` (${pv.by})` : ''}. The picture here is the playlist under it.`,
      idle && soonest && `${entryName(soonest.x)} plays next, at ${soonest.at}.`,
      L.paused && 'Rotation paused: this module stays on until you resume.',
      asleep && (wakesAt ? `Asleep until ${wakesAt}.` : 'Asleep.'), L.pushes && `${L.pushes} message${L.pushes > 1 ? 's' : ''} queued.`,
      // an imported module with the sleep or brightness control did it (bongo/controls.py): say which
      asleep && S.state?.set_by?.sleep && `Put to sleep by ${S.state.set_by.sleep}.`,
      S.state?.set_by?.brightness && `Brightness set by ${S.state.set_by.brightness}. A brightness set by hand stays until you switch back to Auto; with Follow daylight on (Settings > Device) it also ends by itself at the next sunrise, sunset or sleep change.`].filter(Boolean).join(' ');
    if (Date.now() - sliderTouched > 2500 && document.activeElement !== slider) {
      const auto = L.brightness == null;
      // Follow daylight (Settings > Device): Auto is wherever the daylight has it now, and one set by hand
      // here ends by itself at the next sunrise, sunset or sleep change
      const follows = S.config?.daylight?.follow === true && !!S.config?.location && L.level != null;
      slider.value = auto ? (follows ? L.level : S.config?.panel?.brightness ?? 80) : L.brightness;
      slider.classList.toggle('dim', auto);
      bVal.textContent = auto ? `Auto · ${slider.value}%`
        : L.brightness_ends ? `${slider.value}% until ${L.brightness_ends}` : `${slider.value}%`;   // (the Matrix's own words for the time)
      autoBtn.setAttribute('aria-pressed', String(auto)); manBtn.setAttribute('aria-pressed', String(!auto));
    }
    sleepBtn.replaceChildren(h('span', { class: 'strong' }, asleep ? 'Wake now' : 'Sleep now'),
      h('span', { class: 'muted small' }, asleep ? (wakesAt ? `Asleep until ${wakesAt}` : 'Asleep') : `Until ${sleepTo || matrixIn(sleepMinutes())}`));
    const list = S.config?.playlist?.map(normalise).filter((x) => x.enabled && !moduleOff(x.module) && playsAt(x.times, now)) || [];
    const at = list.findIndex((x) => matches(x, e));
    const rest = at < 0 ? list : [...list.slice(at + 1), ...list.slice(0, at)];
    upNext.replaceChildren(...rest.slice(0, 4).map((x) => h('li', null, h('span', { class: 'swatch', style: `background:${colourOf(x.module)}` }),
      h('span', { class: 'grow' }, entryName(x), h('span', { class: 'muted small' }, ` · ${humanize(styleOf(x))}`)), h('span', { class: 'mono small muted' }, dur(x.seconds)))));
    if (!rest.length) {   // one screen (or none) playing now: say so, rather than an empty card
      upNext.append(h('li', { class: 'muted small' }, list.length ? `Only ${entryName(list[0])} is in rotation, so it stays on.` : 'Nothing else is set to play now.'));
    }
    tick();
    playlist.markPlaying();
    playlist.markTimes();
  }
  function tick() {
    const L = live(), e = L.entry;
    const run = (L.elapsed || 0) + (L.paused ? 0 : (performance.now() - S.polledAt) / 1000);
    const el = Math.min(e?.seconds || 0, run);
    const staying = !!L.staying && !L.paused;   // the screen says it's live: it keeps the panel past its seconds
    bar.style.width = e?.seconds ? `${Math.min(100, (el / e.seconds) * 100)}%` : '0';
    elapsedEl.textContent = staying && run > el ? `${mmss(run)} on` : `${mmss(el)} / ${mmss(e?.seconds || 0)}`;
    const left = (e?.seconds || 0) - el;   // (none left: the screen is finishing a line that scrolls)
    nextEl.textContent = L.paused ? 'Paused' : staying ? "Live: staying on" : L.next ? `Next: ${entryName(L.next)} ${left >= 1 ? `in ${mmss(left)}` : 'in a moment'}` : '';
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

// The time on the Matrix, never this phone's clock (it can be in another time zone, or just wrong): the
// Matrix's own time when /api/state was polled ("now", ISO with its offset), plus the time since.
const ISO = /^(\d{4}-\d\d-\d\dT(\d\d):(\d\d):(\d\d)(?:\.(\d{1,3})\d*)?)([+-]\d\d:\d\d|Z)$/;
/** An ISO time as a moment (ms), with any microseconds cut to milliseconds. */
const epoch = (iso) => { const m = ISO.exec(iso || ''); return m ? Date.parse(`${m[1].replace(/\.\d+$/, '')}.${(m[5] || '0').padEnd(3, '0')}${m[6]}`) : Date.parse(iso); };
const since = () => performance.now() - (S.stateAt ?? performance.now());
/** The moment it is now, by the Matrix (ms). */
const matrixEpoch = () => { const t = epoch(S.state?.now); return Number.isFinite(t) ? t + since() : Date.now(); };
/** The time on the Matrix now, in minutes since its midnight (with seconds). */
function matrixMinutes() {
  const m = ISO.exec(S.state?.now || '');
  const at = m ? Number(m[2]) * 60 + Number(m[3]) + Number(m[4]) / 60 : mins(matrixClock());   // (an older Matrix: to the minute)
  return ((((at + since() / 60000) % 1440) + 1440) % 1440);
}
/** "07:00": the Matrix's clock this many minutes from now. */
const matrixIn = (minutes) => hhmm(Math.round(matrixMinutes() + minutes));
/** Minutes until the sleep hours end (e.g. 07:00) on the Matrix's clock, else 8 hours. */
function sleepMinutes() {
  const to = mins(sleepTime(S.config)?.to);
  if (!Number.isFinite(to)) return 480;
  const left = (((to - matrixMinutes()) % 1440) + 1440) % 1440;
  return left < 0.5 ? left + 1440 : left;
}

// -- playlist filmstrip --------------------------------------------------------------------------
function renderPlaylist() {
  const summary = h('span', { class: 'mono small soft' });
  const strip = h('div', { class: 'strip', role: 'list' });
  const scroller = h('div', { class: 'strip-scroll' }, strip);
  const shareBar = h('div', { class: 'share-bar', role: 'img' });
  const legend = h('ul', { class: 'legend' });
  const loopLabel = h('span');
  const problems = h('div', { class: 'error-box', role: 'alert', hidden: true });
  const gapNote = h('p', { class: 'gap-note', role: 'status', hidden: true });   // beside Apply: a time with nothing set to play
  const actions = h('div', { class: 'row gap-s wrap end' });
  let playingNow = '';   // which screens play at this time of day: the loop bar is redrawn when that changes
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
    h('div', null, shareBar, h('div', { class: 'row between mono tiny muted mt-xs' }, h('span', null, '0:00'), loopLabel), legend),
    gapNote, actions, addDialog.el);

  /** In rotation, and whether each plays at this time of day (by the Matrix's own clock). A screen that skips
   *  itself meanwhile (Module.trouble: Weather with no API key) doesn't play either. */
  const rotation = () => (S.draft || []).filter((e) => e.enabled && !moduleOff(e.module)).map((e) => {
    const skipped = !!S.state?.trouble?.[e.module];
    return { e, skipped, now: !skipped && playsAt(e.times, matrixClock()) };
  });

  /** The loop as it is now: its length, the share bar and its legend. A screen with times that isn't
   *  playing now is a thin hatched mark, "not now" in the legend, and not counted. */
  function paintLoop() {
    const all = rotation(), on = all.filter((r) => r.now).map((r) => r.e);
    const timed = all.some((r) => r.e.times.length);
    const total = on.reduce((a, e) => a + Number(e.seconds || 0), 0);
    const share = (e) => Math.round((e.seconds / (total || 1)) * 100);
    playingNow = all.map((r) => Number(r.now)).join('');
    summary.textContent = timed ? `${on.length} playing now · loop ${dur(total, true)}` : `${on.length} on · loop ${dur(total, true)}`;
    loopLabel.textContent = `${timed ? 'The loop now' : 'One full loop'} · ${dur(total, true)}`;
    const later = all.filter((r) => !r.now).map((r) => `${entryName(r.e)}${r.skipped ? ' (skipped)' : ''}`);
    shareBar.setAttribute('aria-label', `Share of ${timed ? 'the loop now' : 'one loop'}: ${on.map((e) => `${entryName(e)} ${share(e)}%`).join(', ') || 'nothing playing'}`
      + (later.length ? `. Not playing now: ${later.join(', ')}` : ''));
    const notNow = (skipped) => (skipped ? 'skipped' : 'not now');
    shareBar.replaceChildren(...all.map(({ e, now, skipped }) => (now
      ? h('div', { class: 'share-seg', title: `${entryName(e)} · ${dur(e.seconds, true)}`, style: `flex:${Number(e.seconds) || 0.01} 1 0;background:${colourOf(e.module)}` })
      : h('div', { class: 'share-seg later', title: `${entryName(e)} · ${notNow(skipped)}`, style: `background-color:${colourOf(e.module)}` }))));
    legend.replaceChildren(...all.map(({ e, now, skipped }) => h('li', null, h('span', { class: `swatch${now ? '' : ' later'}`, style: `background-color:${colourOf(e.module)}` }),
      `${entryName(e)} `, now ? h('span', { class: 'mono muted' }, `${share(e)}%`) : h('span', { class: 'muted' }, notNow(skipped)))));
    if (!all.length) shareBar.append(h('div', { class: 'share-seg empty', style: 'flex:1' }));
  }

  /** Each card's time tag, as the clock moves on: "All day", "12:00–14:00 +1" or "Not now, next 20:00". */
  function markTimes() {
    const now = matrixClock();
    strip.querySelectorAll('.pl-card').forEach((c) => {
      const e = S.draft?.[Number(c.dataset.i)], tag = c.querySelector('.when-tag');
      if (!e || !tag) return;
      const set = e.times.length > 0, plays = playsAt(e.times, now), counts = e.enabled && !moduleOff(e.module);
      const later = set && !plays && counts;
      c.classList.toggle('later', later);
      tag.classList.toggle('set', set && !later);
      tag.classList.toggle('not-now', later);
      const words = later ? ['Not now', `next ${nextStart(e.times, now)}`] : [short(e.times)];
      const text = tag.querySelector('.when-text');
      if (text.dataset.says !== words.join('|')) {
        text.dataset.says = words.join('|');
        text.replaceChildren(...[h('span', null, words[0]), words[1] && h('small', null, words[1])].filter(Boolean));
        tag.setAttribute('aria-label', set ? `${words.join(', ')}. ${entryName(e)} plays ${spoken(e.times)}. Change times` : `All day: ${entryName(e)} plays all day. Change times`);
      }
    });
    if (rotation().map((r) => Number(r.now)).join('') !== playingNow) paintLoop();
  }

  function paint() {
    loops.forEach((l) => l.stop()); loops = [];
    const d = S.draft || [];
    const on = d.filter((e) => e.enabled && !moduleOff(e.module));   // a module switched off in Settings is skipped too
    const bad = new Map();
    for (const p of S.problems) { const q = problemOf(p); if (q.i !== null) bad.set(q.i, q.text); }
    problems.hidden = !S.problems.length;
    problems.replaceChildren(h('strong', null, "The Matrix didn't accept this playlist:"), h('ul', null, S.problems.map((p) => h('li', null, problemLine(p)))));
    strip.replaceChildren(...d.map((e, i) => cardFor(e, i, bad.get(i))),
      h('button', { type: 'button', class: 'add-tile', onclick: () => addDialog.open() }, icon('plus'), 'Add module'));
    paintLoop();
    const gap = gapWarning(on.map((e) => e.times), sleepTime(S.config));
    gapNote.hidden = !gap;
    gapNote.replaceChildren(icon('warn'), h('span', null, gap));
    const n = dirtyCount();
    const said = S.theirs ? `Changed on another device${n ? ` · ${n} unsaved here` : ''}` : `${n} unsaved change${n === 1 ? '' : 's'}`;
    actions.replaceChildren(...(n || S.theirs ? [h('span', { class: 'pill-warn', title: S.theirs ? 'Discard shows the playlist the Matrix has now' : '' }, said),
      h('button', { type: 'button', class: 'btn ghost', onclick: discardDraft }, 'Discard'),
      ...(n ? [h('button', { type: 'button', class: 'btn primary', onclick: applyDraft }, 'Apply to Matrix')] : [])] : []));
    markPlaying();
    markTimes();
    if (Number.isInteger(S.backTo)) {   // back from the editor: to the card it was opened from
      const back = strip.querySelector(`.pl-card[data-i="${S.backTo}"]`);
      S.backTo = null;
      const control = back?.querySelector(S.backToTag ? '.when-tag' : `a.mini-btn`);   // the control it was opened by: the time tag, else the pencil
      S.backToTag = false;
      requestAnimationFrame(() => { back?.scrollIntoView({ inline: 'center', block: 'nearest' }); control?.focus({ preventScroll: true }); });
    }
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
    const modOff = moduleOff(e.module);
    const art = h('article', { class: `pl-card${e.enabled && !modOff ? '' : ' off'}${problem ? ' bad' : ''}`, role: 'listitem', 'data-i': i,
      'aria-label': `${i + 1}. ${entryName(e)}` },
      h('div', { class: 'row between' }, handle, h('span', { class: 'badge-playing', hidden: true }, 'PLAYING'), h('span', { class: 'mono tiny muted' }, `#${i + 1}`)),
      h('div', { class: 'led-frame mini' }, canvas),
      h('div', { class: 'pl-text' }, h('span', { class: 'strong' }, entryName(e), mod?.proposed ? h('span', { class: 'tag' }, 'Proposed') : null),
        h('span', { class: 'mono tiny muted' }, `${humanize(styleOf(e))} · ${e.enabled && !modOff ? dur(e.seconds, true) : 'skipped'}`)),
      modOff ? h('p', { class: 'tiny muted flush' }, `${mod?.name || e.module} is switched off in `,
        h('a', { href: href('/settings'), 'data-route': '/settings', onclick: () => setTimeout(() => document.getElementById('modules')?.scrollIntoView(), 200) }, 'Settings › Modules')) : null,
      problem ? h('p', { class: 'tiny danger flush' }, problem) : null,
      mod && e.enabled && !modOff ? troubleNote(mod, { short: true }) : null,   // (Weather with no API key: why it's skipped)
      !mod ? h('p', { class: 'tiny danger flush' }, 'Module not installed') : null,
      mod ? h('a', { class: 'when-tag', href: href(`/edit/${i}`), 'data-route': `/edit/${i}`, onclick: (ev) => { if (ev.button === 0 && !ev.ctrlKey && !ev.metaKey && !ev.shiftKey && !ev.altKey) S.toTimes = S.backToTag = true; } },
        icon('clock'), h('span', { class: 'when-text' })) : null,
      toggle(e.enabled, e.enabled ? (modOff ? 'In rotation · module off' : 'In rotation') : 'Off', (v) => { e.enabled = v; draftChanged(); }),
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
  return { el, markPlaying, markTimes };
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
        creditLine(m.help.credit),
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
      modSel.replaceChildren(...(S.modules || []).filter((m) => !moduleOff(m.id)).filter((m) => m.default_style).map((m) => h('option', { value: m.id }, m.proposed ? `${m.name} (proposed)` : m.name)));
      modSel.dispatchEvent(new Event('change'));
      dlg.returnValue = '';
      if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
    },
  };
}
