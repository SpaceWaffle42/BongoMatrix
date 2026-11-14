// A pick-an-icon control that shows each 8x8 icon beside its name (a plain <select> can't show pictures).
import { h } from './ui.js';

let iconSet = null;
/** The 8x8 icons (static/icons.json, made by tools/build_icons.py): { name: { label, events, png } }. */
export function icons() {
  if (!iconSet) iconSet = fetch(new URL('../icons.json', import.meta.url)).then((r) => r.json()).catch(() => ({}));
  return iconSet;
}

export const iconImg = (set, name, size) => (name && set[name]
  ? h('img', { class: 'px-icon', src: set[name].png, alt: '', width: size, height: size }) : h('span', { class: 'px-none', 'aria-hidden': 'true', style: `width:${size}px;height:${size}px` }));

let open = null;   // the one picker open at a time
document.addEventListener('click', (ev) => { if (open && !open.contains(ev.target)) open.close(); });

/** ``options``: [[value, label, group?], ...] where a value that's an icon's name shows its picture ('' or
 *  'none' show a blank). ``onpick(value)`` is called with the choice; the button shows it at once. */
export function iconPicker({ id, value, options, label, onpick, key }) {
  const box = h('div', { class: 'iconpick' });
  const button = h('button', { type: 'button', class: 'input iconpick-btn', id, 'aria-haspopup': 'listbox', 'aria-expanded': 'false', 'data-key': key || null });
  const search = h('input', { class: 'input', type: 'search', placeholder: 'Search icons', 'aria-label': 'Search icons', autocomplete: 'off' });
  const list = h('div', { class: 'iconpick-list', role: 'listbox', tabindex: '-1', 'aria-label': label || 'Icons' });
  const panel = h('div', { class: 'iconpick-panel', hidden: true }, search, list);
  let set = {}, current = value ?? '';
  const name = (v) => options.find((o) => o[0] === v)?.[1] ?? v;
  const show = () => {
    button.replaceChildren(iconImg(set, current, 16), h('span', { class: 'iconpick-name' }, name(current) || 'Pick an icon'), h('span', { class: 'iconpick-caret', 'aria-hidden': 'true' }, '▾'));
    button.setAttribute('aria-label', `${label || 'Icon'}: ${name(current) || 'none picked'}`);   // read with what's picked, like a select
  };
  const fill = () => {
    const q = search.value.trim().toLowerCase();
    const shown = options.filter((o) => !q || String(o[1]).toLowerCase().includes(q));
    const groups = [...new Set(shown.map((o) => o[2] || ''))];
    list.replaceChildren(...groups.map((g) => h('div', { role: 'group', 'aria-label': g || 'Choices' },
      g ? h('div', { class: 'iconpick-group', 'aria-hidden': 'true' }, g) : null,
      shown.filter((o) => (o[2] || '') === g).map(([v, l]) => h('button', { type: 'button', role: 'option', class: 'iconpick-item', tabindex: '-1', 'aria-selected': String(v === current), 'data-v': v,
        onclick: () => { current = v; show(); box.close(true); onpick(v); } }, iconImg(set, v, 24), h('span', null, l))))));
    if (!shown.length) list.append(h('p', { class: 'tiny muted flush', style: 'padding:8px' }, 'No icon by that name.'));
  };
  box.close = (focus) => {
    panel.hidden = true; button.setAttribute('aria-expanded', 'false');
    if (open === box) open = null;
    if (focus) button.focus();
  };
  const openIt = () => {
    if (open && open !== box) open.close();
    open = box; panel.hidden = false; button.setAttribute('aria-expanded', 'true');
    search.value = ''; fill();
    const picked = list.querySelector('[aria-selected="true"]');
    if (picked) list.scrollTop = picked.offsetTop - list.offsetTop - 40;   // the list itself, not the page
    panel.scrollIntoView({ block: 'nearest' });   // clear of the phone's tab bar (scroll-margin)
    search.focus({ preventScroll: true });
  };
  button.addEventListener('click', (ev) => { ev.stopPropagation(); panel.hidden ? openIt() : box.close(); });
  search.addEventListener('input', fill);
  panel.addEventListener('click', (ev) => { if (!ev.target.closest('button, input')) ev.preventDefault(); });   // inside a <label>: a click on a heading mustn't press the button
  panel.addEventListener('mousedown', (ev) => { if (ev.target !== list && !ev.target.closest('input')) ev.preventDefault(); });   // (the list itself: its scrollbar)   // focus stays in the search (Safari doesn't focus a clicked button)
  box.addEventListener('focusout', (ev) => { if (!panel.hidden && !box.contains(ev.relatedTarget)) box.close(); });   // tabbed away
  box.addEventListener('keydown', (ev) => {
    if (panel.hidden) return;
    if (ev.key === 'Escape') { ev.preventDefault(); box.close(true); return; }
    if (ev.key !== 'ArrowDown' && ev.key !== 'ArrowUp') return;
    ev.preventDefault();
    const items = [...list.querySelectorAll('.iconpick-item')];
    const at = items.indexOf(document.activeElement);
    const next = ev.key === 'ArrowDown' ? items[Math.min(items.length - 1, at + 1)] : at <= 0 ? search : items[at - 1];
    next?.focus();
  });
  search.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); list.querySelector('.iconpick-item')?.click(); } });
  box.value = () => current;
  box.append(button, panel);
  show();
  icons().then((s) => { set = s; show(); if (!panel.hidden) fill(); });
  return box;
}
