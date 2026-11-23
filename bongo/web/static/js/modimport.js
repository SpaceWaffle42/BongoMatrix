// Settings > Modules: importing a module someone else made (a GitHub link, another link such as a GitLab project
// or a .zip link, with warnings: bongo/linkfetch.py; or a .zip), picking from a repository of several modules, and
// what each imported module adds to its row: where it came from, the sites it can reach, and Remove (bongo/addons.py).
import { api, get, post } from './api.js';
import { h, icon, toast, humanize } from './ui.js';
import { S, onLeave, takeTheirs, dirtyCount, normalise } from './app.js';

const STEPS = [['fetch', 'Fetching it'], ['check', 'Checking its files'], ['try', 'Trying each of its looks on a test screen']];
const inFolder = (s) => (s.folder ? `/${s.folder}` : '');
const sourceText = (s) => (!s ? '' : s.kind === 'github' ? `github.com/${s.repo}${inFolder(s)}${s.ref_kind === 'branch' ? ` (${s.ref})` : ''}`
  : s.kind === 'gitlab' ? `${String(s.url || '').replace(/^https?:\/\//, '')}${inFolder(s)}${['branch', 'tag'].includes(s.ref_kind) ? ` (${s.ref})` : ''}`
    : s.kind === 'link' ? `${s.host}/${s.name}${inFolder(s)}` : `${s.name || 'a .zip'}${s.folder ? ` (${s.folder})` : ''}`);
const isGitHub = (url) => /^https?:\/\/(www\.)?github\.com(\/|$)/i.test(url);
/** Where a module comes from, with what's true of it (not GitHub, the home network, http): before a fetch and on the review. */
function sourceWarning(label, warnings, id) {
  return h('div', { class: 'error-box source-warn', id, role: 'note' }, h('strong', { style: 'display:block' }, label),
    h('ul', { class: 'warn-list' }, warnings.map((w) => h('li', null, w.text))));
}

/** The Import panel: paste a link (or pick a .zip), wait while it's fetched and tried, look, trust, install.
 *  ``done(id)`` runs after an install. Returns { button, panel, update }. */
export function importBox(done) {
  let job = null, timer = null, run = 0, saving = false;   // ``run`` goes up on every start and cancel: an older answer is ignored
  const panel = h('div', { class: 'import-panel stack', hidden: true, role: 'region', 'aria-label': 'Import a module', id: 'import-panel' });
  const title = h('h3', { class: 'h2', tabindex: '-1' }, 'Import a module');
  const cancel = h('button', { type: 'button', class: 'btn ghost small-btn' }, 'Cancel');
  const body = h('div', { class: 'stack' });
  panel.append(h('div', { class: 'row between' }, title, cancel), body);
  const button = h('button', { type: 'button', class: 'btn primary', 'aria-expanded': 'false', 'aria-controls': 'import-panel' }, '+ Import module');

  const forgetJob = (j) => { if (j?.token) api('DELETE', `/api/addons/check/${j.token}`, {}).catch(() => {}); };
  let queue = null;   // several modules picked from one repository: { jobs, at, installed, missed } (each reviewed in turn)
  const stop = () => {
    clearTimeout(timer); timer = null; run += 1; forgetJob(job); job = null;
    if (queue) { queue.jobs.slice(queue.at + 1).forEach(forgetJob); queue = null; }
  };
  const close = () => {
    if (saving) return;                 // installing: it finishes (and says so) first
    stop(); panel.hidden = true; button.setAttribute('aria-expanded', 'false'); button.focus(); };
  cancel.onclick = close;
  panel.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') { ev.preventDefault(); close(); } });
  onLeave(stop);
  // the tab closed or reloaded partway through: the import (and any modules still to review) is cancelled, so the
  // Matrix frees their space now rather than in an hour (best effort: the page is going)
  window.addEventListener('pagehide', () => {
    const left = [job, ...(queue ? queue.jobs.slice(queue.at + 1) : [])].filter((j) => j?.token && !saving);
    for (const j of left) fetch(`/api/addons/check/${j.token}`, { method: 'DELETE', keepalive: true, credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: '{}' }).catch(() => {});
  });

  // a link that isn't GitHub: what it is and its warnings (/api/addons/source), the tick, and an access token
  let place = null;   // { url, said, token: '' } for the link in the box
  function askLink(problem = '', value = '', needsToken = false) {
    title.textContent = 'Import a module';
    const url = h('input', { class: 'input mono', id: 'import-url', type: 'text', inputmode: 'url', placeholder: 'https://github.com/someone/their-module',
      spellcheck: 'false', autocomplete: 'off', autocapitalize: 'off', value });
    const file = h('input', { type: 'file', accept: '.zip,application/zip', class: 'sr-only', id: 'import-zip', tabindex: '-1' });
    const pick = h('button', { type: 'button', class: 'link' }, 'upload a .zip');
    pick.onclick = () => file.click();
    file.onchange = () => { if (file.files[0]) begin(null, file.files[0]); };
    const check = h('button', { type: 'submit', class: 'btn primary' }, 'Check');
    const warnBox = h('div', { class: 'stack-s', hidden: true });
    const link = () => { const v = url.value.trim(); return !v || /^https?:\/\//i.test(v) ? v : `https://${v}`; };
    let ok = null;      // the tick, while a warning is shown
    let token = null;   // the access token box
    const shown = () => place && place.url === link();
    url.addEventListener('input', () => {   // another link: its warnings are looked at again
      if (!shown()) { warnBox.hidden = true; ok = token = null; check.textContent = 'Check'; check.disabled = false; }
    });
    const warn = (info, openToken) => {
      ok = h('input', { type: 'checkbox', class: 'check', id: 'import-source-ok', 'aria-describedby': 'import-source' });
      token = info.token ? h('input', { class: 'input mono', id: 'import-token', type: 'password', autocomplete: 'off', spellcheck: 'false',
        autocapitalize: 'off', placeholder: 'glpat-…', value: place.token || '' }) : null;
      ok.onchange = () => { check.disabled = !ok.checked; };
      check.textContent = 'Fetch'; check.disabled = true;
      const said = sourceWarning(info.label, info.warnings, 'import-source');
      // a token typed for an http link: one more warning in the same list, under the same tick (ticked again for it)
      const tokenWarning = info.token_warning ? h('li', { id: 'import-token-warning' }, info.token_warning.text) : null;
      const sayToken = () => {
        if (!tokenWarning) return;
        const typed = Boolean(token.value.trim());
        if (typed && !tokenWarning.isConnected) {
          said.querySelector('ul').append(tokenWarning);
          if (ok.checked) { ok.checked = false; check.disabled = true; }
        } else if (!typed && tokenWarning.isConnected) tokenWarning.remove();
      };
      if (token) token.addEventListener('input', sayToken);
      warnBox.replaceChildren(said,
        h('label', { class: 'row gap-s', for: 'import-source-ok', style: 'align-items:flex-start' }, ok,
          h('span', { class: 'small' }, 'I understand. Fetch it from here.')),
        token ? h('details', { class: 'item-more', open: openToken || place.token ? '' : null },
          h('summary', null, 'Private project? Add an access token'),
          h('div', { class: 'stack-s', style: 'margin-top:6px' },
            h('label', { class: 'field-col', for: 'import-token' }, 'Access token (optional)', token),
            h('p', { class: 'tiny muted flush' }, `A read-only project or personal token (read_api, or read_repository). It's used only to download from ${info.host} and check it for updates, sent only there (as GitLab's PRIVATE-TOKEN header), and kept on the Matrix: never shown again. Removing the last module from this project forgets it.`),
            info.plain ? h('p', { class: 'tiny warn flush' }, 'This address isn\'t encrypted: a token typed here is added to the warnings above, and the tick covers it too.') : null)) : null);
      sayToken();
      warnBox.hidden = false;
      if (openToken && token) token.focus(); else ok.focus();
    };
    const form = h('form', { class: 'stack-s', onsubmit: async (ev) => {
      ev.preventDefault();
      const v = link();
      if (!v) { url.focus(); return; }
      if (isGitHub(v)) { place = null; begin(v); return; }
      if (shown() && ok) {                   // ticked: fetch, with the token if one was typed
        if (!ok.checked) { ok.focus(); return; }
        place.token = token ? token.value.trim() : '';
        begin(v, null, null, { accept_source: true, ...(place.token ? { access_token: place.token } : {}) });
        return;
      }
      check.disabled = true;
      try {
        const info = await post('/api/addons/source', { url: v });
        if (info.kind === 'github') { place = null; begin(v); return; }
        place = { url: v, said: info, token: '' };
        warn(info, false);
      } catch (e) { check.disabled = false; body.querySelector('.error-box[role="alert"]')?.remove(); form.after(h('p', { class: 'error-box', role: 'alert' }, said(e.message))); }
    } },
      h('label', { class: 'field-label', for: 'import-url' }, 'Link'),
      h('div', { class: 'row gap-s' }, url, check),
      h('p', { class: 'tiny muted flush' }, 'A GitHub repo, release or branch: the usual place for modules. Other links (a GitLab project, your own too, or a link to a .zip) work at your own risk: you\'ll see what that means before anything is fetched. Or ', pick, ' someone sent you.', file),
      warnBox);
    body.replaceChildren(form, ...(problem ? [h('p', { class: 'error-box', role: 'alert' }, problem)] : []));
    if (place && place.url === link() && !isGitHub(link())) warn(place.said, needsToken);   // back after a failed fetch: the tick again
    else url.focus();
  }

  const said = (message) => { const m = String(message || 'something went wrong').replace(/\.$/, ''); return m.charAt(0).toUpperCase() + m.slice(1) + '.'; };

  let updating = null;   // { id, name } while an update runs: a refused token asks for a new one here
  async function begin(url, zip, updateOf, extra = {}) {
    stop();
    if (!updateOf) updating = null;
    const mine = run;
    title.focus();                      // the form is going: keep focus (and Escape) in the panel
    progress(null, url || zip?.name || '');
    let started;
    try {
      if (updateOf) started = await post(`/api/addons/${updateOf}/update`, extra);
      else if (url) started = await post('/api/addons/check', { url, ...extra });
      else { const f = new FormData(); f.append('file', zip, zip.name); started = await api('POST', '/api/addons/upload', f); }
    } catch (e) {
      if (mine !== run) return;
      if (updating) askToken(said(e.message)); else askLink(said(e.message), url || '');
      return;
    }
    if (mine !== run) { forgetJob(started); return; }   // cancelled meanwhile
    job = started;
    poll(url, mine);
  }

  async function poll(url, mine) {
    let now;
    try { now = await get(`/api/addons/check/${job.token}`); }
    catch (e) { if (mine === run) askLink(said(e.message), url || ''); return; }
    if (mine !== run) return;
    job = now;
    if (job.state === 'working') { progress(job.step, job.source); timer = setTimeout(() => poll(url, mine), 600); return; }
    if (job.state === 'failed') {
      const why = said(job.message), token = job.needs_token;
      if (queue) { failedInQueue(why); return; }
      job = null;
      if (updating && token) { askToken(`Couldn't update it. ${why}`); return; }
      askLink(`Couldn't import that. ${why}`, url || '', token); return;
    }
    if (job.state === 'choose') { choose(url, mine); return; }
    review();
  }

  let steps = null, stepAt = -2;
  function progress(step, source) {
    const at = Math.max(0, STEPS.findIndex(([k]) => k === step));
    if (!steps || !body.contains(steps)) {     // made once per import, so a screen reader hears each step
      stepAt = -2;
      steps = h('ol', { class: 'import-steps', 'aria-live': 'polite' });
      body.replaceChildren(h('p', { class: 'small muted flush mono', style: 'overflow-wrap:anywhere' }, source), steps);
    }
    if (at === stepAt) return;
    stepAt = at;
    steps.replaceChildren(...STEPS.map(([, label], k) => h('li', { class: k < at ? 'done' : k === at ? 'now' : '', 'aria-current': k === at ? 'step' : null },
      h('span', { class: 'dot', 'aria-hidden': 'true' }), label, k < at ? h('span', { class: 'sr-only' }, ' (done)') : null)));
  }

  /** A repository of several modules: the owner ticks which (none to start with), and each gets its own review. */
  function choose(url, mine) {
    const parent = job, choices = parent.choices || [];
    const ticks = [];
    const go = h('button', { type: 'submit', class: 'btn primary', value: 'go', disabled: true }, 'Review selected');
    const count = h('span', { class: 'tiny muted', 'aria-live': 'polite' }, 'None ticked');
    const changed = () => { const n = ticks.filter((t) => t.checked).length; go.disabled = !n; count.textContent = n ? `${n} ticked` : 'None ticked'; };
    const rows = choices.map((c, k) => {
      const id = `pick-${k}`;
      if (c.problem) {
        return h('li', { class: 'pick-row off' }, h('input', { type: 'checkbox', class: 'check', id, disabled: true }),
          h('label', { class: 'stack-xs min0', for: id }, h('span', { class: 'strong mono small' }, c.folder),
            h('span', { class: 'tiny warn' }, `Can't be imported: ${said(c.problem)}`)));
      }
      const tick = h('input', { type: 'checkbox', class: 'check', id, value: c.folder, onchange: changed });
      ticks.push(tick);
      const status = c.installed == null ? null : c.newer ? h('span', { class: 'tag amber' }, `Update: ${c.installed} to ${c.version}`)
        : h('span', { class: 'tag' }, c.installed === c.version ? 'Installed' : `Installed: ${c.installed}`);
      return h('li', { class: 'pick-row' }, tick,
        h('label', { class: 'stack-xs min0', for: id },
          h('span', { class: 'row gap-s wrap' }, h('span', { class: 'strong' }, c.name), status),
          h('span', { class: 'tiny muted' }, `${c.version} · by ${c.author} · ${c.folder}/`),
          c.description ? h('span', { class: 'small' }, c.description) : null,
          h('span', { class: 'tiny muted' }, c.sites.length ? `Sites it can reach: ${c.sites.join(', ')}` : 'Reaches no sites'),
          c.abilities.length ? h('span', { class: 'tiny' }, `It also asks to: ${c.abilities.map((a) => a.title.charAt(0).toLowerCase() + a.title.slice(1)).join('; ')}.`) : null));
    });
    const dlg = h('dialog', { class: 'dialog wide pick-dialog', 'aria-labelledby': 'pick-title' },
      h('form', { method: 'dialog', class: 'stack' },
        h('h2', { class: 'h2', id: 'pick-title' }, 'Choose modules to import'),
        h('p', { class: 'muted small flush', style: 'overflow-wrap:anywhere' }, `${choices.length} modules in ${sourceText(parent.origin)}. Tick the ones you want: each gets its own review and test screen before anything is installed.`),
        h('ul', { class: 'pick-list' }, rows),
        h('div', { class: 'row between gap-s wrap' }, count,
          h('div', { class: 'row gap-s' }, h('button', { type: 'button', class: 'btn ghost', onclick: () => dlg.close('') }, 'Cancel'), go))));
    dlg.addEventListener('close', async () => {
      const folders = ticks.filter((t) => t.checked).map((t) => t.value);
      const yes = dlg.returnValue === 'go' && folders.length;
      dlg.remove();
      if (mine !== run) return;
      if (!yes) { stop(); askLink('', url || ''); return; }
      let res;
      try { res = await post(`/api/addons/check/${parent.token}/choose`, { folders }); }
      catch (e) { if (mine === run) { stop(); askLink(said(e.message), url || ''); } return; }
      if (mine !== run) { res.jobs.forEach(forgetJob); return; }
      job = null;
      queue = { jobs: res.jobs, at: -1, installed: [], missed: [], url };
      next();
    });
    document.body.append(dlg);
    dlg.returnValue = '';
    if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
    progress('check', parent.source);
  }

  /** The next of the modules picked from a repository, or the end of them. */
  function next() {
    const q = queue;
    q.at += 1;
    if (q.at >= q.jobs.length) {
      queue = null; job = null; run += 1;
      panel.hidden = true; button.setAttribute('aria-expanded', 'false');
      const got = q.installed.map((i) => i.name), missed = q.missed;
      toast(`${got.length ? `Installed: ${got.join(', ')}.` : 'Nothing was installed.'}${missed.length ? ` Not installed: ${missed.join(', ')}.` : ''}`, got.length ? 'success' : 'info', 8000);
      if (q.installed.length) done(q.installed[q.installed.length - 1].id);
      else button.focus();
      return;
    }
    job = q.jobs[q.at];
    title.textContent = `Module ${q.at + 1} of ${q.jobs.length}`;
    title.focus();
    progress(null, job.source);
    poll(q.url, run);
  }

  const ofQueue = () => (queue ? ` (${queue.at + 1} of ${queue.jobs.length})` : '');
  function failedInQueue(why) {
    const name = job.manifest?.name || 'This module';
    queue.missed.push(name);
    job = null;
    const last = queue.at + 1 >= queue.jobs.length;
    const on = h('button', { type: 'button', class: 'btn primary', onclick: next }, last ? 'Done' : 'Next module');
    title.textContent = `Couldn't import it${ofQueue()}`;
    body.replaceChildren(h('p', { class: 'error-box', role: 'alert' }, `${name} wasn't installed. ${why}`),
      h('p', { class: 'small muted flush' }, 'The other modules you picked are still on their way.'), h('div', { class: 'row end' }, on));
    on.focus();
  }

  function review() {
    const m = job.manifest, mine = run;
    title.textContent = (job.updating ? `Update ${m.name} to ${m.version}` : m.name) + ofQueue();
    const asks = job.abilities || [];   // what it asks for beyond drawing its screens (bongo/abilities.py), in plain words
    const home = asks.some((a) => a.key === 'home_address');
    const trust = h('input', { type: 'checkbox', class: 'check', id: 'import-trust',
      'aria-describedby': [asks.length ? 'import-asks' : '', m.sites.length ? 'import-risk' : '', job.origin?.warnings?.length ? 'import-from' : ''].filter(Boolean).join(' ') || null });
    const install = h('button', { type: 'button', class: 'btn primary', disabled: true }, job.updating ? 'Update' : 'Install');
    trust.onchange = () => { install.disabled = !trust.checked; };
    install.onclick = async () => {
      install.disabled = true; saving = true;
      const updating = job.updating;
      try {
        const screens = job.styles.length > 0;
        const res = await post('/api/addons/install', { token: job.token, trust: true, author: m.author, abilities: asks.map((a) => a.key) });
        if (mine !== run) return;
        if (queue) { queue.installed.push({ id: res.installed, name: res.name }); job = null; saving = false; next(); return; }
        job = null; run += 1; panel.hidden = true; button.setAttribute('aria-expanded', 'false');
        toast(updating ? `${res.name} updated` : screens ? `${res.name} installed. It's in Add a module now.` : `${res.name} installed. Its settings are under Modules.`, 'success');
        done(res.installed);
      } catch (e) { toast(said(e.message), 'error', 8000); install.disabled = !trust.checked; }
      finally { saving = false; }
    };
    const from = ['github', 'gitlab'].includes(job.origin?.kind)
      ? h('a', { href: job.origin.url, target: '_blank', rel: 'noopener' }, sourceText(job.origin)) : sourceText(job.origin);
    const warned = job.origin?.warnings?.length ? job.origin.warnings : null;   // not GitHub: said again by the tick
    const skip = queue ? h('button', { type: 'button', class: 'btn ghost', onclick: () => { forgetJob(job); queue.missed.push(m.name); job = null; next(); } },
      queue.at + 1 >= queue.jobs.length ? 'Skip this one' : 'Skip, next module') : null;
    const sites = m.sites.length ? h('ul', { class: 'site-list' }, m.sites.map((s) => h('li', { class: 'mono' }, s)))
      : h('span', null, 'None: it works without the internet');
    const apart = job.enforced === false
      ? "It's code that runs on this computer. This computer can't keep it apart from the rest, so here it could see your settings and files and reach any site."
      : job.only_listed === false
        ? "It's code that runs on your Matrix. It runs on its own, apart from the rest: it can't see your settings, linked calendars or other files. You've let this module reach any site, and your home network; that stays as it is."
        : home
          ? "It's code that runs on your Matrix. It runs on its own, apart from the rest: it can't see your settings, linked calendars or other files. It can only reach the sites listed above, and of your home network only the one device you name in its settings."
          : "It's code that runs on your Matrix. It runs on its own, apart from the rest: it can't see your settings, linked calendars or other files, and it can only reach the sites listed above, never your home network.";
    const askList = asks.length ? h('div', { class: 'stack-s', id: 'import-asks' },
      h('strong', { class: 'small' }, 'It also asks to:'),
      h('ul', { class: 'ask-list' }, asks.map((a) => h('li', null, h('span', { class: 'small' }, a.title),
        a.detail ? h('span', { class: 'tiny muted' }, a.detail) : null)))) : null;
    body.replaceChildren(h('div', { class: 'import-review' },
      h('div', { class: 'min0' }, job.preview ? h('img', { class: 'import-preview', src: job.preview, alt: `${m.name} on a test screen` })
        : h('p', { class: 'import-preview no-screen small muted flush' }, 'No screen of its own'),
      h('p', { class: 'tiny muted flush' }, job.preview ? 'Drawn on this Matrix just now, on a test screen.' : 'It works in the background: there is nothing to draw.')),
      h('div', { class: 'stack-s min0' },
        m.description ? h('p', { class: 'small flush' }, m.description) : null,
        h('dl', { class: 'import-facts' },
          h('dt', null, 'Made by'), h('dd', null, m.author),
          h('dt', null, 'Version'), h('dd', null, m.version),
          h('dt', null, 'From'), h('dd', null, from),
          h('dt', null, 'Looks'), h('dd', null, job.styles.length ? `${job.styles.length} (${job.styles.map(humanize).join(', ')})` : 'None'),
          h('dt', null, 'Sites it can reach'), h('dd', null, sites)))),
      ...(m.sites.some((x) => x.startsWith('*.')) ? [h('p', { class: 'error-box' }, `A site starting “*.” means any name under it: ${m.sites.filter((x) => x.startsWith('*.')).map((x) => x.slice(2)).join(', ')}. Only trust that if the whole of each belongs to one company you know.`)] : []),
      ...(job.new_sites?.length ? [h('p', { class: 'error-box' }, `This version also wants to reach: ${job.new_sites.join(', ')}`)] : []),
      askList,
      ...(job.new_abilities?.length ? [h('p', { class: 'error-box' }, `New in this version, it asks to: ${job.new_abilities.map((a) => a.title.charAt(0).toLowerCase() + a.title.slice(1)).join('; ')}.`)] : []),
      h('div', { class: 'note' }, icon('info'), h('div', null, apart)),
      ...(m.sites.length ? [h('p', { class: 'small flush', id: 'import-risk' }, h('strong', null, 'Shared-service risk: '),
        "if a module lists a big service that many websites share, it may also reach those other websites. The Matrix can't tell them apart.")] : []),
      warned ? sourceWarning(`From ${job.origin.url}${job.origin.folder ? ` (its ${job.origin.folder} folder)` : ''}`, warned, 'import-from') : null,
      h('label', { class: 'row gap-s', for: 'import-trust', style: 'align-items:flex-start' }, trust,
        h('span', { class: 'small' }, `I trust ${m.author}${warned ? ' and where it comes from' : ''}${m.sites.length ? ' and accept the shared-service risk' : ''}. It's their code that will run${asks.length ? ', and it may do what is listed above' : ''}.`)),
      h('div', { class: 'row end gap-s wrap' }, skip, install));
    title.focus();
  }

  button.onclick = () => {
    if (!panel.hidden) { title.focus(); return; }
    panel.hidden = false; button.setAttribute('aria-expanded', 'true');
    askLink();
  };
  /** Update an imported module: the same steps, from where it was installed. */
  /** An update from a private GitLab project whose kept token wasn't accepted: a new one, then the same update. */
  function askToken(problem = '') {
    const { id, name, source } = updating;
    stop();
    title.textContent = `Update ${name}`;
    const token = h('input', { class: 'input mono', id: 'update-token', type: 'password', autocomplete: 'off', spellcheck: 'false', autocapitalize: 'off', placeholder: 'glpat-…' });
    const go = h('button', { type: 'submit', class: 'btn primary' }, 'Try again');
    const plain = /^http:/i.test(source?.url || '');
    const form = h('form', { class: 'stack-s', onsubmit: (ev) => {
      ev.preventDefault();
      const v = token.value.trim();
      if (!v) { token.focus(); return; }
      begin(null, null, id, { access_token: v });
      title.textContent = `Update ${name}`;
    } },
      problem ? h('p', { class: 'error-box', role: 'alert' }, problem) : null,
      h('p', { class: 'small flush' }, `${name} comes from a private GitLab project${source?.host ? ` on ${source.host}` : ''}, and the access token kept for it wasn't accepted (it may have expired or been revoked). Make a new read-only token in GitLab (read_api, or read_repository) and paste it here.`),
      h('label', { class: 'field-col', for: 'update-token' }, 'Access token', h('div', { class: 'row gap-s' }, token, go)),
      h('p', { class: 'tiny muted flush' }, `It replaces the one kept for this project once the update installs. It's sent only to ${source?.host || 'that GitLab'} (as GitLab's PRIVATE-TOKEN header), never shown again.`),
      plain ? h('p', { class: 'error-box' }, 'This address isn\'t encrypted: the access token is sent as plain text, and anyone on your network could read it.') : null);
    body.replaceChildren(form);
    token.focus();
  }

  const update = (id, name, opts = {}) => {
    if (saving) return;
    panel.hidden = false; button.setAttribute('aria-expanded', 'true');
    panel.scrollIntoView({ block: 'nearest' });
    updating = { id, name, source: opts.source };
    if (opts.token) { askToken(); return; }
    begin(null, null, id);
    title.textContent = `Update ${name}`;
  };
  return { button, panel, update };
}

/** An imported module's row extras: where it's from, its sites, the sites switch (with its warning) and Remove.
 *  ``changed(list)`` takes the new /api/addons answer. */
export function importedMore(m, info, changed, onUpdate) {
  const uses = [...(S.config.playlist || []), ...(S.config.overlays || [])].filter((e) => e.module === m.id).length;
  const meta = h('span', { class: 'muted tiny', style: 'overflow-wrap:anywhere' }, `${info.version} · by ${info.author}${info.source ? ` · from ${sourceText(info.source)}` : ''}${info.source?.private ? ' · with an access token' : ''}`,
    info.only_listed ? null : h('span', { class: 'tag amber' }, 'Any site'));
  const asks = info.abilities || [];   // what it was allowed at import, beyond drawing its screens
  const said = info.background ? h('span', { class: 'tiny row gap-s module-status', role: 'status', hidden: true }) : null;   // its background task's own words
  if (said) paintStatus(said, (S.config.modules_off || []).includes(m.id) ? null : info.status);
  const box = h('details', { class: 'item-more', 'data-key': `addon:${m.id}` }, h('summary', null, asks.length ? 'What it can do, and removing it' : 'Sites, and removing it'));
  const can = asks.length ? h('div', { class: 'stack-s' }, h('span', { class: 'small' }, 'You let it:'),
    h('ul', { class: 'ask-list' }, asks.map((a) => h('li', null, h('span', { class: 'small' }, a.title), a.detail ? h('span', { class: 'tiny muted' }, a.detail) : null)))) : null;
  const sitesLine = h('p', { class: 'small flush', style: 'overflow-wrap:anywhere' }, info.sites.length ? `Can reach: ${info.sites.join(', ')}` : 'Reaches no sites: it works without the internet.');
  const only = h('input', { type: 'checkbox', class: 'check', id: `only-${m.id}`, 'data-key': `only:${m.id}`, checked: info.only_listed });
  const ask = h('div', { class: 'stack-s', hidden: true });
  const save = async (value) => {
    try { changed(await api('PATCH', `/api/addons/${m.id}`, { only_listed: value })); toast(value ? `${m.name}: only its own sites` : `${m.name} can reach any site`, value ? 'success' : 'info'); }
    catch (e) { toast(e.message, 'error', 8000); only.checked = !value; }
  };
  only.onchange = () => {
    if (only.checked) { ask.hidden = true; save(true); return; }
    only.checked = true;          // not until it's confirmed below
    const yes = h('button', { type: 'button', class: 'btn danger-btn small-btn' }, 'Allow any site');
    const no = h('button', { type: 'button', class: 'btn ghost small-btn' }, 'Keep it to its sites');
    no.onclick = () => { ask.hidden = true; only.focus(); };
    yes.onclick = () => { ask.hidden = true; only.checked = false; only.focus(); save(false); };
    ask.replaceChildren(h('div', { class: 'error-box', role: 'alert' },
      h('strong', null, `Let ${m.name} reach any site?`),
      h('p', { class: 'flush', style: 'margin-top:6px' }, `It could then reach any site on the internet, and any device on your home network (Wi-Fi or wired): your router, computers, phones, cameras and printers, and other programs on this Matrix that use its network. It could send them whatever it shows or makes. It still can't see your settings or files.`),
      h('p', { class: 'flush', style: 'margin-top:6px' }, `Only do this if you trust ${info.author} completely, and the module needs a site it doesn't list.`)),
    h('div', { class: 'row gap-s wrap' }, no, yes));
    ask.hidden = false;
    no.focus();
  };
  const blocked = info.blocked?.length ? h('p', { class: 'tiny warn flush' }, `Tried to reach (and stopped): ${info.blocked.join(', ')}`) : null;
  const confirmRemove = h('div', { class: 'row gap-s wrap', hidden: true });
  const remove = h('button', { type: 'button', class: 'btn danger-btn small-btn' }, 'Remove');
  remove.onclick = () => {
    const yes = h('button', { type: 'button', class: 'btn danger-btn small-btn' }, `Yes, remove ${m.name}`);
    const no = h('button', { type: 'button', class: 'btn ghost small-btn', onclick: () => { confirmRemove.hidden = true; remove.hidden = false; remove.focus(); } }, 'Keep it');
    yes.onclick = async () => {
      yes.disabled = true;
      try {
        const res = await api('DELETE', `/api/addons/${m.id}`, {});
        const clean = !dirtyCount() && !S.theirs;   // (edits here, or another device's save not taken yet: keep the
        S.draft = (S.draft || []).filter((e) => e.module !== m.id);   // old mark, so a later Apply still asks)
        if (res.screens_removed && clean) {
          await takeTheirs().catch(() => {});
          S.draft = (S.config.playlist || []).map(normalise);
        }
        toast(`${m.name} removed${res.screens_removed ? `, and its ${res.screens_removed} screen${res.screens_removed === 1 ? '' : 's'}` : ''}`, 'success');
        changed(res, m.id);
      } catch (e) { toast(e.message, 'error', 8000); yes.disabled = false; }
    };
    confirmRemove.replaceChildren(h('span', { class: 'small' }, uses ? `Its ${uses} screen${uses === 1 ? ' is' : 's are'} taken out of the playlist too.` : 'Remove it from this Matrix?'), no, yes);
    confirmRemove.hidden = false; remove.hidden = true; no.focus();
  };
  box.append(h('div', { class: 'stack-s', style: 'margin-top:4px' }, can, sitesLine,
    info.sites.length || !info.only_listed ? h('label', { class: 'row gap-s', for: `only-${m.id}`, style: 'align-items:flex-start' }, only,
      h('span', { class: 'small' }, 'Only allow the sites it lists', h('span', { class: 'tiny muted', style: 'display:block' },
        info.only_listed ? (asks.some((a) => a.key === 'home_address') ? 'On: it can reach nothing else, and of your home network only the one device named in its settings.'
          : 'On: it can reach nothing else, and never your home network.') : 'Off: it can reach any site, and your home network.'))) : null,
    ask, blocked, h('div', { class: 'row gap-s wrap' }, remove, confirmRemove)));
  const upd = info.update ? h('button', { type: 'button', class: 'btn small-btn', style: 'align-self:flex-start', onclick: () => onUpdate(m.id, m.name, { source: info.source }) },
    `Update to ${info.update.version}`) : null;
  // its private GitLab project no longer accepts the token kept for it: updates can't be looked for until a new one
  const signIn = info.sign_in ? h('div', { class: 'row gap-s wrap', style: 'align-items:center' },
    h('span', { class: 'tiny warn' }, 'Needs sign-in: its GitLab project no longer accepts the access token kept for it, so updates can\'t be checked.'),
    h('button', { type: 'button', class: 'btn small-btn', onclick: () => onUpdate(m.id, m.name, { token: true, source: info.source }) }, 'Add a new token')) : null;
  return [meta, said, upd, signIn, box];
}

/** A module's status line on its row: the square dot (green: fine; amber: something to look at) and its own words. */
export function paintStatus(el, status) {
  el.hidden = !status;
  el.replaceChildren(...(status ? [h('span', { class: `dot ${status.ok ? 'ok' : 'warn'}`, 'aria-hidden': 'true' }), h('span', { style: 'overflow-wrap:anywhere' }, status.text)] : []));
}

/** The imported modules (/api/addons), once per page. */
let cache = null;
export const addons = (fresh = false) => (cache && !fresh ? cache : (cache = get('/api/addons').catch(() => ({ modules: [], enforced: true }))));
export const forget = () => { cache = null; };
