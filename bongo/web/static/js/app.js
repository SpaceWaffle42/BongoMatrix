// Shell: shared state, routing, header/nav, polling and the playlist draft.
import { DEMO, hooks, get, put } from './api.js';
import { h, icon, toast, fail, clone, same } from './ui.js';

/** Shared state. `draft` is the locally edited playlist; `config.playlist` is what the Matrix has. */
export const S = { state: null, config: null, modules: null, draft: null, problems: [], online: true, polledAt: 0,
  revs: null };   // revs: the Matrix's mark for each config section as this page loaded it (see putConfig)
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
  } })],
];

// -- routing -------------------------------------------------------------------------------
export const path = () => (DEMO ? decodeURIComponent(location.hash.slice(1)) || '/' : location.pathname) || '/';
export const href = (p) => (DEMO ? `#${p}` : p);
export function go(p, { replace = false } = {}) {
  if (p === path()) { render(); return; }
  if (DEMO) { replace ? location.replace(`#${p}`) : (location.hash = p); return; } // hashchange renders
  history[replace ? 'replaceState' : 'pushState'](null, '', p);
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
  const mod = await route[1]();
  if (seq !== renderSeq) return;
  document.body.dataset.route = p.startsWith('/edit') ? '/edit' : p;
  document.body.classList.toggle('pairing', p === '/pair');
  paintNav(p);
  paintHeader();
  view.replaceChildren();
  view.scrollTop = 0;
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
    href: href(to), class: cls, 'data-route': to, 'aria-current': active === to ? 'page' : null,
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
window.addEventListener('popstate', () => { if (!DEMO) render(); });
window.addEventListener('hashchange', () => { if (DEMO) render(); });

// -- data ----------------------------------------------------------------------------------
export function normalise(entry) {
  return { module: entry.module, style: entry.style || null, seconds: Number(entry.seconds ?? 30),
    settings: clone(entry.settings || {}), enabled: entry.enabled !== false };
}
export async function loadAll() {
  // the marks first: if another device saves in between, they are older than the config, and the next
  // save here asks rather than overwrites (the safe way round)
  const revs = await get('/api/config/revisions').catch(() => null);
  const [config, modules, state] = await Promise.all([get('/api/config'), get('/api/modules'), get('/api/state')]);
  S.revs = revs;
  S.config = config; S.modules = modules; S.state = state; S.online = true; S.polledAt = performance.now();
  if (!S.draft) S.draft = (config.playlist || []).map(normalise);
  paintHeader();
}
export const moduleById = (id) => S.modules?.find((m) => m.id === id);
export const styleOf = (e) => e.style || moduleById(e.module)?.default_style || '';
export const entryName = (e) => moduleById(e?.module)?.name || humanId(e?.module);
const humanId = (id) => String(id || '?').replace(/_/g, ' ').replace(/\b./g, (c) => c.toUpperCase());

export const dirtyCount = () => {
  const saved = (S.config?.playlist || []).map(normalise), d = S.draft || [];
  if (saved.length !== d.length) return Math.max(1, Math.abs(saved.length - d.length) + d.filter((e, i) => saved[i] && !same(e, saved[i])).length);
  return d.filter((e, i) => !same(e, saved[i])).length;
};
export function draftChanged() { S.problems = []; paintHeader(); emit('draft'); }

const SECTIONS = { playlist: 'playlist', safe_area: 'safe area', sleep: 'sleep schedule', splash: 'splash', pet: 'pet',
  location: 'location', update: 'update source', name: 'name', timezone: 'time zone' };
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
    if (!confirm(`The ${what} on the Matrix changed since this page loaded it (saved from another phone or tab).\n\nOK: save yours over it.\nCancel: leave theirs. Reload this page to see it.`)) {
      throw Object.assign(new Error('Not saved: it was changed on another device. Reload to see the latest.'), { status: 409, kept: true });
    }
    res = await put('/api/config', part);
  }
  if (res?.revisions) S.revs = res.revisions;
  return res;
}

export async function applyDraft() {
  const playlist = S.draft.map((e) => ({ module: e.module, style: e.style || undefined, seconds: e.seconds,
    settings: e.settings, enabled: e.enabled }));
  try {
    const res = await putConfig({ playlist });
    S.config.playlist = clone(playlist);
    S.problems = [];
    toast(res?.restart_needed ? 'Saved. Panel size and wiring changes take effect after a restart (Settings > Device > Restart).' : 'Playlist applied', 'success', 5000);
    paintHeader(); emit('draft');
    return true;
  } catch (err) {
    if (err.status === 422) { S.problems = err.data?.problems || [err.message]; toast('The Matrix rejected the playlist. See the problems listed.', 'error'); emit('draft'); }
    else if (err.kept) toast(err.message, 'error', 6000);
    else fail("Couldn't save")(err);
    return false;
  }
}
export function discardDraft() {
  S.draft = (S.config?.playlist || []).map(normalise);
  S.problems = [];
  paintHeader(); emit('draft');
  toast('Changes discarded');
}
/** Save part of the config (safe area, schedule...). Returns true on success. */
export async function saveConfig(part, what = 'Settings') {
  try {
    const res = await putConfig(part);
    Object.assign(S.config, clone(part));
    if (res?.restart_needed) toast('Saved. Panel size and wiring changes take effect after a restart (Settings > Device > Restart).', 'success', 8000);
    else toast(`${what} saved`, 'success');
    return true;
  } catch (err) {
    if (err.kept) toast(err.message, 'error', 6000);
    else fail(`Couldn't save ${what.toLowerCase()}`)(err);
    return false;
  }
}

function paintHeader() {
  const n = dirtyCount();
  const bar = document.getElementById('draft-bar');
  bar.hidden = !n || path() === '/pair';
  document.getElementById('draft-count').textContent = `${n} unsaved change${n === 1 ? '' : 's'}`;
  document.getElementById('device-name').textContent = path() === '/pair' ? 'Not paired' : S.state?.name || 'Bongo Matrix';
  document.getElementById('device-host').textContent = DEMO ? 'demo' : location.host;
  const dot = document.getElementById('device-dot');
  dot.className = `dot ${path() === '/pair' ? '' : S.online ? 'ok' : 'bad'}`;
  document.getElementById('device-pill').title = S.online ? 'Connected' : 'Not responding';
}

// -- polling ---------------------------------------------------------------------------------
async function poll() {
  if (!document.hidden && S.config && path() !== '/pair') {
    try { S.state = await get('/api/state'); S.online = true; S.polledAt = performance.now(); }
    catch (err) { S.online = err.status !== 0 ? S.online : false; }
    paintHeader();
    emit('state');
  }
  setTimeout(poll, 1000);
}

// -- boot ------------------------------------------------------------------------------------
hooks.unauthorized = () => {
  if (path() === '/pair') return;
  S.config = null;
  sessionStorageSet('bongo.after', path());
  go('/pair', { replace: true });
};
function sessionStorageSet(k, v) { try { sessionStorage.setItem(k, v); } catch { /* ignore */ } }

document.getElementById('draft-apply').addEventListener('click', applyDraft);
document.getElementById('draft-discard').addEventListener('click', discardDraft);
document.querySelector('.brand').setAttribute('href', href('/'));
window.addEventListener('beforeunload', (e) => { if (dirtyCount()) { e.preventDefault(); e.returnValue = ''; } });
if (DEMO) document.getElementById('demo-badge').hidden = false;

(async function boot() {
  try {
    const { paired } = await get('/api/pair/status');
    if (!paired && path() !== '/pair') { sessionStorageSet('bongo.after', path()); go('/pair', { replace: true }); poll(); return; }
  } catch (err) {
    showOffline(document.getElementById('view'), err);
    setTimeout(boot, 5000);
    return;
  }
  render();
  poll();
})();
