// Logs: last-hour summary, per-minute chart, search, severity chips, follow live.
import { get } from './api.js';
import { h, icon, copyText, clockSec, clock, uptime, store } from './ui.js';
import { S, bus, onLeave } from './app.js';

const LEVELS = [['ERROR', 'Errors'], ['WARNING', 'Warnings'], ['INFO', 'Info'], ['DEBUG', 'Debug']];
const MAX = 2000;

export function render(view) {
  let rows = [], since = 0, timer = 0, fetching = false, alive = true;
  let follow = store.get('logsFollow', true);
  const shown = new Set(store.get('logsLevels', ['ERROR', 'WARNING', 'INFO']));
  const open = new Set();

  const errN = h('div', { class: 'big-num danger' }, '0'), warnN = h('div', { class: 'big-num warn' }, '0');
  const up = h('div', { class: 'big-num' }, '—');
  const chart = h('div', { class: 'bars', role: 'img' });
  const chartStart = h('span');
  const search = h('input', { class: 'input with-icon', type: 'search', id: 'log-search', placeholder: 'Search messages, sources, tracebacks…', value: store.get('logsQuery', '') });
  const chips = h('div', { class: 'row gap-xs wrap', role: 'group', 'aria-label': 'Severity' });
  const count = h('span', { class: 'small muted' });
  const followBox = h('input', { type: 'checkbox', id: 'log-follow', checked: follow, class: 'check' });
  const list = h('div', { class: 'log-list', role: 'log', 'aria-live': 'off' });

  view.append(
    h('div', { class: 'page-head' }, h('h1', { class: 'h1' }, 'Logs')),
    h('section', { class: 'card log-summary', 'aria-label': 'Last hour at a glance' },
      h('div', { class: 'row gap-l wrap' },
        h('div', null, h('div', { class: 'small muted' }, 'Errors · 1h'), errN),
        h('div', null, h('div', { class: 'small muted' }, 'Warnings · 1h'), warnN),
        h('div', null, h('div', { class: 'small muted' }, 'Service up'), up)),
      h('div', { class: 'chart-wrap' }, chart, h('div', { class: 'row between mono tiny muted' }, chartStart, h('span', null, 'entries per minute'), h('span', null, 'now')))),
    h('div', { class: 'log-tools' },
      h('label', { class: 'search', for: 'log-search' }, h('span', { class: 'sr' }, 'Search logs'), icon('search', 'ico search-ico'), search),
      chips),
    h('section', { class: 'log-box', 'aria-label': 'Log entries' },
      h('div', { class: 'log-bar' }, count,
        h('label', { class: 'row gap-s small soft', for: 'log-follow' }, h('span', { class: `dot ${follow ? 'ok' : ''}`, id: 'follow-dot' }), 'Follow live', followBox)),
      list));

  followBox.addEventListener('change', () => {
    follow = followBox.checked; store.set('logsFollow', follow);
    view.querySelector('#follow-dot').className = `dot ${follow ? 'ok' : ''}`;
    if (follow) poll();
  });
  let qTimer = 0;
  search.addEventListener('input', () => { clearTimeout(qTimer); qTimer = setTimeout(() => { store.set('logsQuery', search.value); paint(); }, 150); });

  async function poll() {
    clearTimeout(timer);
    if (fetching) return;
    fetching = true;
    try {
      const fresh = await get(`/api/logs?since=${since}`);
      if (fresh.length) {
        rows = rows.concat(fresh).slice(-MAX);
        since = Math.max(since, ...fresh.map((r) => r.t));
        paint();
      } else if (!rows.length) paint();
    } catch (err) {
      if (err.status === 401) return;
      count.textContent = `Couldn't load logs: ${err.message}`;
    } finally { fetching = false; }
    if (follow && alive) timer = setTimeout(poll, 2000);
  }

  function paintChips(counts) {
    chips.replaceChildren(
      h('button', { type: 'button', class: 'chip lvl', 'aria-pressed': String(shown.size === LEVELS.length),
        onclick: () => { LEVELS.forEach(([l]) => shown.add(l)); saveLevels(); } }, 'All', h('span', { class: 'mono tiny' }, rows.length)),
      ...LEVELS.map(([lvl, label]) => h('button', { type: 'button', class: `chip lvl lvl-${lvl}`, 'aria-pressed': String(shown.has(lvl)),
        onclick: () => { shown.has(lvl) ? shown.delete(lvl) : shown.add(lvl); saveLevels(); } },
      h('span', { class: 'lvl-dot', 'aria-hidden': 'true' }), label, h('span', { class: 'mono tiny' }, counts[lvl] || 0))));
  }
  const saveLevels = () => { store.set('logsLevels', [...shown]); paint(); };

  function paint() {
    const now = Date.now() / 1000, hourAgo = now - 3600;
    const counts = {};
    rows.forEach((r) => { counts[r.level] = (counts[r.level] || 0) + 1; });
    const lastHour = rows.filter((r) => r.t >= hourAgo);
    errN.textContent = lastHour.filter((r) => r.level === 'ERROR' || r.level === 'CRITICAL').length;
    warnN.textContent = lastHour.filter((r) => r.level === 'WARNING').length;
    // Per-minute bars, coloured by the worst level in that minute.
    const bins = Array.from({ length: 60 }, () => ({ n: 0, worst: 0 }));
    const rank = { DEBUG: 0, INFO: 0, WARNING: 1, ERROR: 2, CRITICAL: 2 };
    lastHour.forEach((r) => { const b = bins[Math.min(59, Math.floor((r.t - hourAgo) / 60))]; b.n++; b.worst = Math.max(b.worst, rank[r.level] || 0); });
    const peak = Math.max(1, ...bins.map((b) => b.n));
    chart.replaceChildren(...bins.map((b, k) => h('div', { class: `bar${b.worst === 2 ? ' err' : b.worst === 1 ? ' warn' : ''}`,
      style: `height:${b.n ? Math.max(8, (b.n / peak) * 100) : 4}%`, title: `${clock(new Date((hourAgo + k * 60) * 1000))} · ${b.n} entries` })));
    chart.setAttribute('aria-label', `Log entries per minute over the last hour: ${lastHour.length} in total, ${errN.textContent} errors, ${warnN.textContent} warnings`);
    chartStart.textContent = clock(new Date(hourAgo * 1000));
    paintChips(counts);
    const q = search.value.trim().toLowerCase();
    const level = (r) => (r.level === 'CRITICAL' ? 'ERROR' : r.level);
    const visible = rows.filter((r) => shown.has(level(r)) && (!q || `${r.message} ${r.source} ${r.trace}`.toLowerCase().includes(q))).reverse();
    count.textContent = `Showing ${Math.min(visible.length, 500)} of ${rows.length} entries${q ? ` · matching “${search.value.trim()}”` : ''}`;
    list.replaceChildren(...visible.slice(0, 500).map(row));
    if (!visible.length) list.append(h('p', { class: 'muted empty' }, rows.length ? 'Nothing matches these filters.' : 'No log entries yet.'));
  }

  function row(r) {
    const key = `${r.t}|${r.message}`;
    const lvl = r.level === 'CRITICAL' ? 'ERROR' : r.level;
    const trace = r.trace ? h('div', { class: 'trace', hidden: !open.has(key) },
      h('pre', null, r.trace),
      h('button', { type: 'button', class: 'btn small-btn', onclick: () => copyText(`${r.message}\n${r.trace}`) }, 'Copy traceback')) : null;
    const toggleBtn = r.trace ? h('button', { type: 'button', class: 'link tiny', 'aria-expanded': String(open.has(key)), onclick: (ev) => {
      const show = trace.hidden; trace.hidden = !show; ev.currentTarget.setAttribute('aria-expanded', String(show));
      ev.currentTarget.textContent = show ? 'Hide traceback' : 'Show traceback';
      show ? open.add(key) : open.delete(key);
    } }, open.has(key) ? 'Hide traceback' : 'Show traceback') : null;
    return h('div', { class: `log-row lvl-${lvl}` },
      h('span', { class: 'log-time' }, clockSec(new Date(r.t * 1000))),
      h('span', { class: `badge lvl-${lvl}` }, lvl === 'WARNING' ? 'WARN' : lvl),
      h('span', { class: 'log-src' }, r.source),
      h('div', { class: 'log-msg' }, h('span', null, r.message), toggleBtn, trace));
  }

  const paintUp = () => { up.textContent = S.state?.uptime != null ? uptime(S.state.uptime) : '—'; };
  bus.addEventListener('state', paintUp);
  const refresh = setInterval(() => { if (!follow) return; paint(); }, 30000);
  onLeave(() => { alive = false; clearTimeout(timer); clearInterval(refresh); bus.removeEventListener('state', paintUp); });
  paintUp();
  poll();
}
