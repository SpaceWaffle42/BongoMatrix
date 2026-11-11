// Small DOM + formatting helpers shared by every view.

/** h('button', {class: 'btn', onclick}, 'Label') -> element. Booleans set properties; aria-* need strings. */
export function h(tag, props, ...kids) {
  const el = tag === 'svg' ? document.createElementNS('http://www.w3.org/2000/svg', tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className = v;
    else if (k === 'html') el.innerHTML = v;  // only ever used with our own static icon strings
    else if (k in el && !k.includes('-') && typeof v !== 'string') el[k] = v;
    else if (k === 'value' || k === 'checked') el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : String(kid));
  }
  return el;
}

const P = (d, extra = '') => `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" ${extra}>${d}</svg>`;
const ICONS = {
  now: P('<rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M6 14h.01M10 14h.01"/>'),
  playlist: P('<path d="M3 6h18M3 12h12M3 18h8"/>'),
  upload: P('<path d="M12 16V4M7 9l5-5 5 5M4 20h16"/>'),
  settings: P('<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1"/>'),
  logs: P('<path d="M4 6h16M4 12h16M4 18h10"/>'),
  pause: '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>',
  play: '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true"><path d="M7 4l13 8-13 8z"/></svg>',
  next: P('<path d="m5 4 10 8-10 8zM19 5v14"/>'),
  grip: '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden="true"><circle cx="9" cy="6" r="1.5"/><circle cx="15" cy="6" r="1.5"/><circle cx="9" cy="12" r="1.5"/><circle cx="15" cy="12" r="1.5"/><circle cx="9" cy="18" r="1.5"/><circle cx="15" cy="18" r="1.5"/></svg>',
  left: P('<path d="m15 18-6-6 6-6"/>'),
  right: P('<path d="m9 18 6-6-6-6"/>'),
  edit: P('<path d="M4 20h4L19 9l-4-4L4 16z"/>'),
  trash: P('<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>'),
  plus: P('<path d="M12 5v14M5 12h14"/>'),
  check: P('<path d="M5 12.5l4.5 4.5L19 7.5"/>'),
  search: P('<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>'),
  info: P('<circle cx="12" cy="12" r="9"/><path d="M12 8h.01M11 12h1v4h1"/>'),
  x: P('<path d="M6 6l12 12M18 6 6 18"/>'),
};
export const icon = (name, cls = 'ico') => h('span', { class: cls, html: ICONS[name] || '' });

let toastBox;
export function toast(message, kind = 'info', ms = 3800) {
  toastBox ||= document.getElementById('toasts');
  const el = h('div', { class: `toast ${kind}`, role: kind === 'error' ? 'alert' : null }, message);
  toastBox.append(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 300); }, ms);
}
export const fail = (what) => (err) => { if (err?.status !== 401) toast(`${what}: ${err?.message || err}`, 'error', 6000); };

export const clone = (v) => (v === undefined ? v : JSON.parse(JSON.stringify(v)));
export const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
export const humanize = (s) => String(s || '').replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
const pad = (n, w = 2) => String(n).padStart(w, '0');

/** 72.4 -> "1:12.4" (tenths) or "1:12" */
export function dur(sec, tenths = false) {
  sec = Math.max(0, Number(sec) || 0);
  const m = Math.floor(sec / 60), s = sec - m * 60;
  return tenths ? `${m}:${s.toFixed(1).padStart(4, '0')}` : `${m}:${pad(Math.floor(s))}`;
}
export const mmss = (sec) => `${pad(Math.floor(Math.max(0, sec) / 60))}:${pad(Math.floor(Math.max(0, sec) % 60))}`;
export const clock = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
export const clockSec = (d) => `${clock(d)}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
export function uptime(sec) {
  const d = Math.floor(sec / 86400), hrs = Math.floor(sec / 3600) % 24, m = Math.floor(sec / 60) % 60;
  return d ? `${d}d ${hrs}h` : hrs ? `${hrs}h ${m}m` : `${m}m`;
}
export function ago(iso) {
  const t = typeof iso === 'number' ? iso * 1000 : Date.parse(iso);
  if (!t) return '—';
  const s = (Date.now() - t) / 1000;
  if (s < 90) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400 * 2) return `${Math.round(s / 3600)} h ago`;
  return new Date(t).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
}

export const reducedMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;

export const store = {
  get(k, d) { try { const v = localStorage.getItem(`bongo.${k}`); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(`bongo.${k}`, JSON.stringify(v)); } catch { /* private mode: ignore */ } },
};

export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const ta = h('textarea', { style: 'position:fixed;opacity:0' }); ta.value = text;
    document.body.append(ta); ta.select();
    try { document.execCommand('copy'); } finally { ta.remove(); }
  }
  toast('Copied');
}

/** Toggle switch: a real button with aria-pressed. */
export function toggle(on, label, onchange, { showLabel = true } = {}) {
  const btn = h('button', { type: 'button', class: 'switch', 'aria-pressed': String(!!on), 'aria-label': showLabel ? null : label },
    h('span', { class: 'switch-track', 'aria-hidden': 'true' }, h('span', { class: 'switch-knob' })),
    showLabel ? h('span', { class: 'switch-label' }, label) : null);
  btn.addEventListener('click', () => {
    const next = btn.getAttribute('aria-pressed') !== 'true';
    btn.setAttribute('aria-pressed', String(next));
    onchange(next);
  });
  return btn;
}

/** Segmented control: buttons with aria-pressed, one active. */
export function segmented(options, value, onchange, label) {
  const group = h('div', { class: 'seg', role: 'group', 'aria-label': label });
  for (const opt of options) {
    const [val, text] = Array.isArray(opt) ? opt : [opt, humanize(opt)];
    group.append(h('button', {
      type: 'button', 'aria-pressed': String(same(val, value)),
      onclick: () => { group.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', 'false'));
        group.children[options.indexOf(opt)].setAttribute('aria-pressed', 'true'); onchange(val); },
    }, text));
  }
  return group;
}

export function card(label, ...kids) {
  return h('section', { class: 'card', 'aria-label': label }, kids);
}
