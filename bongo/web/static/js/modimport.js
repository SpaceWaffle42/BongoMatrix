// Settings > Modules: importing a module someone else made (a public GitHub link or a .zip), and what each
// imported module adds to its row: where it came from, the sites it can reach, and Remove (bongo/addons.py).
import { api, get, post } from './api.js';
import { h, icon, toast, humanize } from './ui.js';
import { S, onLeave, takeTheirs, dirtyCount, normalise } from './app.js';

const STEPS = [['fetch', 'Fetching it'], ['check', 'Checking its files'], ['try', 'Trying each of its looks on a test screen']];
const sourceText = (s) => (!s ? '' : s.kind === 'github' ? `github.com/${s.repo}${s.ref_kind === 'branch' ? ` (${s.ref})` : ''}` : s.name || 'a .zip');

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
  const stop = () => { clearTimeout(timer); timer = null; run += 1; forgetJob(job); job = null; };
  const close = () => {
    if (saving) return;                 // installing: it finishes (and says so) first
    stop(); panel.hidden = true; button.setAttribute('aria-expanded', 'false'); button.focus(); };
  cancel.onclick = close;
  panel.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') { ev.preventDefault(); close(); } });
  onLeave(stop);

  function askLink(problem = '', value = '') {
    title.textContent = 'Import a module';
    const url = h('input', { class: 'input mono', id: 'import-url', type: 'text', inputmode: 'url', placeholder: 'https://github.com/someone/their-module',
      spellcheck: 'false', autocomplete: 'off', autocapitalize: 'off', value });
    const file = h('input', { type: 'file', accept: '.zip,application/zip', class: 'sr-only', id: 'import-zip', tabindex: '-1' });
    const pick = h('button', { type: 'button', class: 'link' }, 'upload a .zip');
    pick.onclick = () => file.click();
    file.onchange = () => { if (file.files[0]) begin(null, file.files[0]); };
    const check = h('button', { type: 'submit', class: 'btn primary' }, 'Check');
    const link = () => { const v = url.value.trim(); return !v || /^https?:\/\//i.test(v) ? v : `https://${v}`; };
    const form = h('form', { class: 'stack-s', onsubmit: (ev) => { ev.preventDefault(); if (link()) begin(link()); else url.focus(); } },
      h('label', { class: 'field-label', for: 'import-url' }, 'GitHub link'),
      h('div', { class: 'row gap-s' }, url, check),
      h('p', { class: 'tiny muted flush' }, 'A repo, a release or a branch link all work. Or ', pick, ' someone sent you.', file));
    body.replaceChildren(form, ...(problem ? [h('p', { class: 'error-box', role: 'alert' }, problem)] : []));
    url.focus();
  }

  const said = (message) => { const m = String(message || 'something went wrong').replace(/\.$/, ''); return m.charAt(0).toUpperCase() + m.slice(1) + '.'; };

  async function begin(url, zip, updateOf) {
    stop();
    const mine = run;
    title.focus();                      // the form is going: keep focus (and Escape) in the panel
    progress(null, url || zip?.name || '');
    let started;
    try {
      if (updateOf) started = await post(`/api/addons/${updateOf}/update`, {});
      else if (url) started = await post('/api/addons/check', { url });
      else { const f = new FormData(); f.append('file', zip, zip.name); started = await api('POST', '/api/addons/upload', f); }
    } catch (e) { if (mine === run) askLink(said(e.message), url || ''); return; }
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
    if (job.state === 'failed') { const why = said(job.message); job = null; askLink(`Couldn't import that. ${why}`, url || ''); return; }
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

  function review() {
    const m = job.manifest, mine = run;
    title.textContent = job.updating ? `Update ${m.name} to ${m.version}` : m.name;
    const trust = h('input', { type: 'checkbox', class: 'check', id: 'import-trust', 'aria-describedby': m.sites.length ? 'import-risk' : null });
    const install = h('button', { type: 'button', class: 'btn primary', disabled: true }, job.updating ? 'Update' : 'Install');
    trust.onchange = () => { install.disabled = !trust.checked; };
    install.onclick = async () => {
      install.disabled = true; saving = true;
      const updating = job.updating;
      try {
        const res = await post('/api/addons/install', { token: job.token, trust: true, author: m.author });
        if (mine !== run) return;
        job = null; run += 1; panel.hidden = true; button.setAttribute('aria-expanded', 'false');
        toast(updating ? `${res.name} updated` : `${res.name} installed. It's in Add a module now.`, 'success');
        done(res.installed);
      } catch (e) { toast(said(e.message), 'error', 8000); install.disabled = !trust.checked; }
      finally { saving = false; }
    };
    const from = job.origin?.kind === 'github'
      ? h('a', { href: job.origin.url, target: '_blank', rel: 'noopener' }, sourceText(job.origin)) : sourceText(job.origin);
    const sites = m.sites.length ? h('ul', { class: 'site-list' }, m.sites.map((s) => h('li', { class: 'mono' }, s)))
      : h('span', null, 'None: it works without the internet');
    const apart = job.enforced === false
      ? "It's code that runs on this computer. This computer can't keep it apart from the rest, so here it could see your settings and files and reach any site."
      : job.only_listed === false
        ? "It's code that runs on your Matrix. It runs on its own, apart from the rest: it can't see your settings, linked calendars or other files. You've let this module reach any site, and your home network; that stays as it is."
        : "It's code that runs on your Matrix. It runs on its own, apart from the rest: it can't see your settings, linked calendars or other files, and it can only reach the sites listed above, never your home network.";
    body.replaceChildren(h('div', { class: 'import-review' },
      h('div', { class: 'min0' }, h('img', { class: 'import-preview', src: job.preview, alt: `${m.name} on a test screen` }),
        h('p', { class: 'tiny muted flush' }, 'Drawn on this Matrix just now, on a test screen.')),
      h('div', { class: 'stack-s min0' },
        m.description ? h('p', { class: 'small flush' }, m.description) : null,
        h('dl', { class: 'import-facts' },
          h('dt', null, 'Made by'), h('dd', null, m.author),
          h('dt', null, 'Version'), h('dd', null, m.version),
          h('dt', null, 'From'), h('dd', null, from),
          h('dt', null, 'Looks'), h('dd', null, `${job.styles.length} (${job.styles.map(humanize).join(', ')})`),
          h('dt', null, 'Sites it can reach'), h('dd', null, sites)))),
      ...(m.sites.some((x) => x.startsWith('*.')) ? [h('p', { class: 'error-box' }, `A site starting “*.” means any name under it: ${m.sites.filter((x) => x.startsWith('*.')).map((x) => x.slice(2)).join(', ')}. Only trust that if the whole of each belongs to one company you know.`)] : []),
      ...(job.new_sites?.length ? [h('p', { class: 'error-box' }, `This version also wants to reach: ${job.new_sites.join(', ')}`)] : []),
      h('div', { class: 'note' }, icon('info'), h('div', null, apart)),
      ...(m.sites.length ? [h('p', { class: 'small flush', id: 'import-risk' }, h('strong', null, 'Shared-service risk: '),
        "if a module lists a big service that many websites share, it may also reach those other websites. The Matrix can't tell them apart.")] : []),
      h('label', { class: 'row gap-s', for: 'import-trust', style: 'align-items:flex-start' }, trust,
        h('span', { class: 'small' }, m.sites.length ? `I trust ${m.author} and accept the shared-service risk. It's their code that will run.` : `I trust ${m.author}. It's their code that will run.`)),
      h('div', { class: 'row end gap-s wrap' }, install));
    title.focus();
  }

  button.onclick = () => {
    if (!panel.hidden) { title.focus(); return; }
    panel.hidden = false; button.setAttribute('aria-expanded', 'true');
    askLink();
  };
  /** Update an imported module: the same steps, from where it was installed. */
  const update = (id, name) => {
    if (saving) return;
    panel.hidden = false; button.setAttribute('aria-expanded', 'true');
    panel.scrollIntoView({ block: 'nearest' });
    begin(null, null, id);
    title.textContent = `Update ${name}`;
  };
  return { button, panel, update };
}

/** An imported module's row extras: where it's from, its sites, the sites switch (with its warning) and Remove.
 *  ``changed(list)`` takes the new /api/addons answer. */
export function importedMore(m, info, changed, onUpdate) {
  const uses = [...(S.config.playlist || []), ...(S.config.overlays || [])].filter((e) => e.module === m.id).length;
  const meta = h('span', { class: 'muted tiny', style: 'overflow-wrap:anywhere' }, `${info.version} · by ${info.author}${info.source ? ` · from ${sourceText(info.source)}` : ''}`,
    info.only_listed ? null : h('span', { class: 'tag amber' }, 'Any site'));
  const box = h('details', { class: 'item-more', 'data-key': `addon:${m.id}` }, h('summary', null, 'Sites, and removing it'));
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
  box.append(h('div', { class: 'stack-s', style: 'margin-top:4px' }, sitesLine,
    info.sites.length || !info.only_listed ? h('label', { class: 'row gap-s', for: `only-${m.id}`, style: 'align-items:flex-start' }, only,
      h('span', { class: 'small' }, 'Only allow the sites it lists', h('span', { class: 'tiny muted', style: 'display:block' },
        info.only_listed ? 'On: it can reach nothing else, and never your home network.' : 'Off: it can reach any site, and your home network.'))) : null,
    ask, blocked, h('div', { class: 'row gap-s wrap' }, remove, confirmRemove)));
  const upd = info.update ? h('button', { type: 'button', class: 'btn small-btn', style: 'align-self:flex-start', onclick: () => onUpdate(m.id, m.name) },
    `Update to ${info.update.version}`) : null;
  return [meta, upd, box];
}

/** The imported modules (/api/addons), once per page. */
let cache = null;
export const addons = (fresh = false) => (cache && !fresh ? cache : (cache = get('/api/addons').catch(() => ({ modules: [], enforced: true }))));
export const forget = () => { cache = null; };
