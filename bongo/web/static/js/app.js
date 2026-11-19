// Shell: shared state, routing, header/nav, polling and the playlist draft.
import { DEMO, hooks, get, put } from './api.js';
import { h, icon, toast, fail, clone, same, humanize } from './ui.js';
import { afterPairing } from './deeplink.js';

/** Shared state. `draft` is the locally edited playlist; `config.playlist` is what the Matrix has. */
export const S = { state: null, config: null, modules: null, draft: null, problems: [], online: true, polledAt: 0,
  revs: null,     // revs: the Matrix's mark for each config section as this page loaded it (see putConfig)
  theirs: false };   // the playlist was changed on another phone or tab while this one had edits of its own (freshen)
export const bus = new EventTarget();
const emit = (type) => bus.dispatchEvent(new Event(type));

const NAV = [
  ['/', 'Now playing', 'Now', 'now'],
  ['/playlist', 'Playlist', 'Playlist', 'playlist'],
  ['/settings', 'Settings', 'Settings', 'settings'],
  ['/logs', 'Logs', 'Logs', 'logs'],
  ['/animator', 'Animator', 'Animator', 'edit'],
];
const ROUTES = [
  [/^\/(playlist)?$/, () => import('./now.js')],
  [/^\/edit\/(\d+)$/, () => import('./editor.js')],
  [/^\/settings$/, () => import('./settings.js')],
  [/^\/logs$/, () => import('./logs.js')],
  [/^\/pair$/, () => import('./pair.js')],
  [/^\/animator$/, async () => ({ render(view) {  // the animator page, framed in the dashboard
    const frame = h('iframe', { src: DEMO ? 'animator.html?demo' : 'animator.html', title: 'Animator', class: 'animator-frame' });   // the real one either way
    view.append(h('div', { class: 'stack' }, frame));
    // Its own "unsaved work" warning only fires when the whole page goes; tapping another tab just removed the
    // frame, and the drawing with it. So the dashboard asks the frame before it leaves this page
    mayLeave = () => {
      let unsaved = false;
      try { unsaved = !!frame.contentWindow?.bongoUnsaved?.(); } catch { /* not loaded yet */ }
      return !unsaved || confirm("Your drawing isn't saved. Leave the Animator and lose it?");
    };
    onLeave(() => { mayLeave = null; });
  } })],
];

// -- routing -------------------------------------------------------------------------------
export const path = () => (DEMO ? decodeURIComponent(location.hash.slice(1)) || '/' : location.pathname) || '/';
export const href = (p) => (DEMO ? `#${p}` : p);
let mayLeave = null;   // set by a page that can hold unsaved work of its own (the Animator): false keeps you there
export function go(p, { replace = false } = {}) {
  if (p === path()) { render(); return; }
  if (mayLeave && !mayLeave()) return;
  if (S.stale && !DEMO && !dirtyCount()) { location.assign(p); return; }   // updated since this page loaded: the next page comes fresh
  if (DEMO) { replace ? location.replace(`#${p}`) : (location.hash = p); return; } // hashchange renders
  history[replace ? 'replaceState' : 'pushState'](null, '', p);
  shownAt = path();   // (p may end in a #part: /settings#update, back from pairing)
  render();
}

let leaving = [];
/** Views register cleanups (stop loops, timers) here; they run when the route changes. */
export const onLeave = (fn) => leaving.push(fn);

let renderSeq = 0;
async function render() {
  const p = path();
  const seq = ++renderSeq;
  leaving.splice(0).forEach((fn) => { try { fn(); } catch { /* ignore */ } });
  const route = ROUTES.find(([re]) => re.test(p));
  const view = document.getElementById('view');
  if (!route) { go('/', { replace: true }); return; }
  if (p !== '/pair' && !S.config) {
    try { await loadAll(); } catch (err) { if (err.status !== 401) showOffline(view, err); return; }
  }
  let mod;
  try { mod = await route[1](); }
  catch (err) {
    // The page's script didn't arrive (the Matrix was restarting, a Wi-Fi blip). The browser remembers a failed
    // load, so trying this page again does nothing until the dashboard itself is loaded afresh: say so
    console.error(err);
    if (seq === renderSeq) {
      view.replaceChildren(h('div', { class: 'card offline' },
        h('h1', { class: 'h2' }, "This page didn't load"),
        h('p', { class: 'muted' }, "The Matrix didn't answer just then. Reload to try again; anything you've saved is safe."),
        h('button', { class: 'btn primary', type: 'button', onclick: () => location.reload() }, 'Reload')));
    }
    return;
  }
  if (seq !== renderSeq) return;
  document.body.dataset.route = p.startsWith('/edit') ? '/edit' : p;
  document.body.classList.toggle('pairing', p === '/pair');
  paintNav(p);
  paintHeader();
  view.replaceChildren();
  view.scrollTop = 0;
  if (S.theirs) freshen();   // (out of the editor: a playlist saved elsewhere meanwhile can come in now)
  try { mod.render(view, p.match(route[0])); }
  catch (err) { console.error(err); view.append(h('p', { class: 'error-box' }, `This page failed to draw: ${err.message}`)); }
  if (p === '/playlist' && matchMedia('(min-width: 720px)').matches) document.getElementById('playlist')?.scrollIntoView();
  else window.scrollTo(0, 0);
}

function showOffline(view, err) {
  view.replaceChildren(h('div', { class: 'card offline' },
    h('h1', { class: 'h2' }, "Can't reach the Matrix"),
    h('p', { class: 'muted' }, err.message),
    h('button', { class: 'btn primary', type: 'button', onclick: () => render() }, 'Try again')));
}

function paintNav(p) {
  const active = p.startsWith('/edit') ? '/playlist' : p;
  const link = ([to, long, short, ic], cls) => h('a', {
    href: href(to), class: cls, 'data-route': to, 'aria-current': active === to ? 'page' : null, title: cls === 'tab' ? null : long,   // (the name, where the side menu is only icons: the Animator)
  }, icon(ic), h('span', null, cls === 'tab' ? short : long));
  document.getElementById('sidenav').replaceChildren(...NAV.map((n) => link(n, 'side-link')));
  document.getElementById('tabbar').replaceChildren(...NAV.map((n) => link(n, 'tab')));
}

// History-mode links: keep navigation in the app.
document.addEventListener('click', (e) => {
  const a = e.target.closest?.('a[data-route]');
  if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  go(a.dataset.route);
});
let shownAt = path();   // the page on screen (the address has already changed when Back is pressed)
window.addEventListener('popstate', () => {
  if (DEMO) return;
  if (mayLeave && path() !== shownAt && !mayLeave()) { history.pushState(null, '', shownAt); return; }   // Back, declined: stay
  shownAt = path();
  render();
});
window.addEventListener('hashchange', () => { if (DEMO) render(); });
// the Animator's ≡ sheet on a phone: "Leave the Animator" (through go(), so its unsaved-work question still asks)
window.addEventListener('message', (e) => { if (e.origin === location.origin && e.data && e.data.type === 'animator-leave' && path() === '/animator') go('/'); });

// -- data ----------------------------------------------------------------------------------
export function normalise(entry) {
  return { module: entry.module, style: entry.style || null, seconds: Number(entry.seconds ?? 30),
    settings: clone(entry.settings || {}), enabled: entry.enabled !== false,
    times: clone(Array.isArray(entry.times) ? entry.times : []) };   // times of day it plays; none: all day
}
/** The Matrix's own clock ("13:25"): its time zone, not this phone's. */
export const matrixClock = () => S.state?.clock || `${String(new Date().getHours()).padStart(2, '0')}:${String(new Date().getMinutes()).padStart(2, '0')}`;
export async function loadAll() {
  // the marks first: if another device saves in between, they are older than the config, and the next
  // save here asks rather than overwrites (the safe way round)
  const revs = await get('/api/config/revisions').catch(() => null);
  const [config, modules, state] = await Promise.all([get('/api/config'), get('/api/modules'), get('/api/state')]);
  S.revs = revs;
  S.config = config; S.modules = modules; S.state = state; S.online = true; S.polledAt = S.stateAt = performance.now();
  if (!S.draft) S.draft = (config.playlist || []).map(normalise);
  paintHeader();
}
export const moduleById = (id) => S.modules?.find((m) => m.id === id);
/** Switched off in Settings > Modules: its screens are skipped and it isn't offered in Add a module. */
export const moduleOff = (id) => (S.config?.modules_off || []).includes(id);
export const styleOf = (e) => e.style || moduleById(e.module)?.default_style || '';
export const entryName = (e) => moduleById(e?.module)?.name || humanId(e?.module);
const humanId = (id) => String(id || '?').replace(/_/g, ' ').replace(/\b./g, (c) => c.toUpperCase());
/** A problem the Matrix found, in words. "playlist[0]: text: speed: 500 is outside 1 to 200" is the first
 *  screen's: { i: 0, text: 'Speed: 500 is outside 1 to 200', where: 'Screen #1 (Text)' }. */
/** A capital to start, unless it starts with an address (https://...), which stays as it is. */
const capital = (t) => (/^[a-z][\w+.-]*:\/\//.test(t) ? t : t.replace(/^./, (c) => c.toUpperCase()));
export function problemOf(p) {
  const m = /^playlist\[(\d+)\]:?\s*([\s\S]*)$/.exec(p);
  if (!m) return { i: null, text: p, where: '' };
  const i = Number(m[1]), e = S.draft?.[i];
  const text = m[2].replace(/^([\w-]+):(?!\/\/)\s*(?=\S)/, (all, id) => (id === e?.module ? '' : all))   // (its module's name is said already)
    .replace(/^([a-z][a-z0-9_]*):(?!\/\/)/, (all, key) => `${humanize(key)}:`);   // (not https://...)
  return { i, text: capital(text), where: e ? `Screen #${i + 1} (${entryName(e)})` : `Screen #${i + 1}` };
}
/** Any problem the Matrix found, in words: "Screen #2 (Text): ...", "Overlay #1: ...", "sleep.from: ..." is
 *  "From: ...", and "playlist: turn at least one entry on" is "Turn at least one entry on". */
export function problemLine(p) {
  const q = problemOf(p);
  if (q.where) return `${q.where}: ${q.text}`;
  const o = /^overlays\[(\d+)\]:?\s*([\s\S]*)$/.exec(p);
  if (o) return `Overlay #${Number(o[1]) + 1}: ${capital(o[2])}`;
  const k = /^([a-z_]+)((?:\.[a-z_]+)*):(?!\/\/)\s*([\s\S]*)$/.exec(p);
  if (!k) return p;
  if (k[1] === 'playlist' && !k[2]) return capital(k[3]);
  return `${humanize((k[2] ? k[2].split('.').pop() : k[1]))}: ${k[3]}`;
}

export const dirtyCount = () => {
  const saved = (S.config?.playlist || []).map(normalise), d = S.draft || [];
  if (saved.length !== d.length) return Math.max(1, Math.abs(saved.length - d.length) + d.filter((e, i) => saved[i] && !same(e, saved[i])).length);
  return d.filter((e, i) => !same(e, saved[i])).length;
};
export function draftChanged() { S.problems = []; paintHeader(); emit('draft'); }

const SECTIONS = { playlist: 'playlist', safe_area: 'safe area', sleep: 'sleep schedule', splash: 'splash', pet: 'pet',
  location: 'location', update: 'update source', name: 'name', timezone: 'time zone', modules_off: 'module switches' };
/** Save part of the config. If another phone or tab has saved the same part since this page loaded
 *  it, the Matrix says so (409) and we ask before replacing their work with ours. */
async function putConfig(part) {
  const base = {};
  if (S.revs) for (const key of Object.keys(part)) base[key] = S.revs[key] ?? '-';   // '-': it wasn't set when we loaded
  let res;
  try { res = await put('/api/config', Object.keys(base).length ? { ...part, _base: base } : part); }
  catch (err) {
    if (err.status !== 409) throw err;
    const what = (err.data?.conflict || []).map((k) => SECTIONS[k] || k.replace(/_/g, ' ')).join(', ') || 'settings';
    if (!confirm(`The ${what} on the Matrix was changed on another phone or tab since you started here.\n\nOK: replace it with this page's (the other change is lost).\nCancel: keep the other one (Discard then shows it).`)) {
      throw Object.assign(new Error(`Not saved: it was changed on another device. ${'playlist' in part ? 'Press Discard to see it.' : 'Reload to see the latest.'}`), { status: 409, kept: true });
    }
    res = await put('/api/config', part);
  }
  if (res?.revisions) {   // the marks of what this page sent: the others still say what this page has (freshen)
    if (!S.revs) S.revs = res.revisions;
    else for (const key of Object.keys(part)) S.revs[key] = res.revisions[key] ?? '-';
  }
  return res;
}

export async function applyDraft() {
  const playlist = S.draft.map((e) => ({ module: e.module, style: e.style || undefined, seconds: e.seconds,
    settings: e.settings, enabled: e.enabled, times: e.times?.length ? e.times : undefined }));
  try {
    const res = await putConfig({ playlist });
    S.config.playlist = clone(playlist);
    S.problems = []; S.theirs = false;
    toast(res?.restart_needed ? 'Saved. This needs a restart to take effect (Settings > Device > Restart).' : 'Playlist applied', 'success', 5000);
    paintHeader(); emit('draft');
    return true;
  } catch (err) {
    if (err.status === 422) { S.problems = err.data?.problems || [err.message]; toast('The Matrix rejected the playlist. See the problems listed.', 'error'); emit('draft'); }
    else if (err.kept) toast(err.message, 'error', 6000);
    else fail("Couldn't save")(err);
    return false;
  }
}
export async function discardDraft() {
  await takeTheirs().catch(() => {});   // back to what the Matrix has now (another device may have saved since)
  S.draft = (S.config?.playlist || []).map(normalise);
  S.problems = [];
  paintHeader(); emit('draft');
  toast('Changes discarded');
  if (path().startsWith('/edit')) go('/playlist', { replace: true });   // (the screen open there may not be in it now)
}

/** The playlist the Matrix has now, and its mark (the mark first: if a save lands in between, the next
 *  save here asks rather than overwrites). */
async function fetchTheirs() {
  const revs = await get('/api/config/revisions');
  const config = await get('/api/config');
  return { playlist: config.playlist, mark: revs.playlist };
}
function adopt(theirs) {   // (the playlist and its mark go together: a page never has the mark without the playlist)
  S.config.playlist = theirs.playlist;
  S.revs = { ...(S.revs || {}), playlist: theirs.mark };
  S.theirs = false;
}
export async function takeTheirs() { adopt(await fetchTheirs()); }

/** Keep this page's playlist the one the Matrix has. Another phone or tab may have saved since this one
 *  loaded it: with nothing changed here, this page simply shows the new one; with edits of its own (or a
 *  screen open in the editor), it says so in the bar instead, and Discard there loads theirs. Without this,
 *  a page left open shows an old playlist, and applying from it puts that old one back. */
let freshening = false;
export async function freshen() {
  if (freshening || document.hidden || !S.config || DEMO || path() === '/pair') return;
  freshening = true;
  try {
    if (!S.revs) { S.revs = await get('/api/config/revisions'); return; }   // (the first look failed: from now on)
    const revs = await get('/api/config/revisions');
    if (revs.playlist === S.revs.playlist) { if (S.theirs) { S.theirs = false; paintHeader(); emit('draft'); } return; }
    if (dirtyCount() || path().startsWith('/edit')) { if (!S.theirs) { S.theirs = true; paintHeader(); emit('draft'); } return; }
    const before = S.draft, was = JSON.stringify(S.draft);
    const theirs = await fetchTheirs();
    if (S.draft !== before || JSON.stringify(S.draft) !== was || path().startsWith('/edit')) { S.theirs = true; paintHeader(); emit('draft'); return; }   // (edited meanwhile)
    adopt(theirs);
    S.draft = (S.config.playlist || []).map(normalise);
    paintHeader(); emit('draft');
    toast('Playlist updated: it was changed on another device');
  } catch { /* offline for a moment: the next look will do */ }
  finally { freshening = false; }
}
/** Save part of the config (safe area, schedule...). Returns true on success. */
export async function saveConfig(part, what = 'Settings') {
  try {
    const res = await putConfig(part);
    Object.assign(S.config, clone(part));
    if (res?.restart_needed) toast('Saved. This needs a restart to take effect (Settings > Device > Restart).', 'success', 8000);
    else toast(`${what} saved`, 'success');
    return true;
  } catch (err) {
    if (err.data?.problems?.length) err.message = err.data.problems.map(problemLine).join(' · ');   // (in words, not config keys)
    if (err.kept) toast(err.message, 'error', 6000);
    else fail(`Couldn't save ${what.toLowerCase()}`)(err);
    return false;
  }
}

function paintHeader() {
  const n = dirtyCount();
  const bar = document.getElementById('draft-bar');
  bar.hidden = !(n || S.theirs) || path() === '/pair';
  document.getElementById('draft-apply').hidden = !n;   // (nothing changed here: applying could only put the old one back)
  document.getElementById('draft-count').textContent = S.theirs
    ? `Changed on another device${n ? ` · ${n} unsaved here` : ''}`   // (Discard loads theirs)
    : `${n} unsaved change${n === 1 ? '' : 's'}`;
  document.getElementById('device-name').textContent = path() === '/pair' ? 'Not paired' : S.state?.name || 'Bongo Matrix';
  document.getElementById('device-host').textContent = DEMO ? 'demo' : location.host;
  const dot = document.getElementById('device-dot');
  dot.className = `dot ${path() === '/pair' ? '' : S.online ? 'ok' : 'bad'}`;
  document.getElementById('device-pill').title = S.online ? 'Connected' : 'Not responding';
}

// -- polling ---------------------------------------------------------------------------------
async function poll() {
  if (!document.hidden && S.config && path() !== '/pair') {
    try { S.state = await get('/api/state'); S.online = true; S.polledAt = S.stateAt = performance.now(); updated(); }
    catch (err) { S.online = err.status !== 0 ? S.online : false; }
    paintHeader();
    emit('state');
  }
  if (++polls % 4 === 0) freshen();   // every few seconds: has another device saved the playlist?
  setTimeout(poll, 1000);
}
let polls = 0;

/** The Matrix's pages changed since this one loaded (an update): this tab has the old styles and may fetch new
 *  scripts, which don't fit together. With nothing unsaved it reloads; otherwise it says so, and the next page
 *  you open comes fresh (``go``). */
function updated() {
  const build = S.state?.build;
  if (!build) return;
  if (!S.build) { S.build = build; return; }
  if (build === S.build || S.stale) return;
  S.stale = true;
  const busy = dirtyCount() || path().startsWith('/edit') || path() === '/animator' || document.querySelector('dialog[open]');
  if (!busy) { location.reload(); return; }
  toast('The Matrix has been updated. Finish what you are doing, then reload this page for the new version.', 'info', 15000);
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) freshen(); });   // back to this tab

// A file dropped beside a drop zone mustn't make the browser open it and leave the dashboard (a drop zone
// is anything marked data-drop: Settings > Media library).
const draggingFiles = (ev) => [...(ev.dataTransfer?.types || [])].includes('Files');
window.addEventListener('dragover', (ev) => {
  if (!draggingFiles(ev)) return;
  ev.preventDefault();
  const over = ev.target.nodeType === 1 ? ev.target : ev.target.parentElement;   // (it can be the text inside)
  if (!over?.closest('[data-drop]')) ev.dataTransfer.dropEffect = 'none';
});
window.addEventListener('drop', (ev) => { if (draggingFiles(ev)) ev.preventDefault(); });

// -- boot ------------------------------------------------------------------------------------
/** The secret settings a screen keeps with it (Style.secrets), by name: never put in a preview's address (api.js). */
hooks.secrets = (entry) => {
  const mod = moduleById(entry?.module);
  return (mod?.styles?.[entry.style || mod?.default_style]?.settings || []).filter((f) => f.type === 'secret').map((f) => f.name);
};
hooks.unauthorized = () => {
  if (path() === '/pair') return;
  S.config = null;
  sessionStorageSet('bongo.after', whereNow());
  go('/pair', { replace: true });
};
/** Where to come back to after pairing: this page and its #part (the hub's Update… link is /settings#update). */
const whereNow = () => afterPairing(path(), location.hash, DEMO);
function sessionStorageSet(k, v) { try { sessionStorage.setItem(k, v); } catch { /* ignore */ } }

document.getElementById('draft-apply').addEventListener('click', applyDraft);
document.getElementById('draft-discard').addEventListener('click', discardDraft);
document.querySelector('.brand').setAttribute('href', href('/'));
window.addEventListener('beforeunload', (e) => { if (dirtyCount()) { e.preventDefault(); e.returnValue = ''; } });
// Back takes you off the page a dialog belongs to (the style picker, a preview): it goes too, instead of staying
// open over the next page, still editing a screen that's no longer there
const shutDialogs = () => document.querySelectorAll('dialog[open]').forEach((d) => { try { d.close(); } catch { /* already gone */ } });
window.addEventListener('popstate', shutDialogs);
window.addEventListener('hashchange', shutDialogs);
if (DEMO) document.getElementById('demo-badge').hidden = false;

(async function boot() {
  try {
    const { paired } = await get('/api/pair/status');
    if (!paired && path() !== '/pair') { sessionStorageSet('bongo.after', whereNow()); go('/pair', { replace: true }); poll(); return; }
    if (paired && path() === '/pair') history.replaceState(null, '', DEMO ? '#/' : '/');   // paired already: nothing to do there
  } catch (err) {
    showOffline(document.getElementById('view'), err);
    setTimeout(boot, 5000);
    return;
  }
  render();
  poll();
})();
