// Settings: a card of its own for each imported module that has settings of its own (bongo/abilities.py): the
// fields its bongo-module.json declares, the one home-network address it may connect to, and the line its
// background task reports. Nothing here knows what any module is for: the card is built from what it declared.
import { get, put } from './api.js';
import { h, toast, toggle } from './ui.js';
import { S, onLeave } from './app.js';

const EVERY = 5000;   // ms between looks at a module's status line

/** A secret: never sent back once saved, so it is only ever "saved" (with Replace and Clear) or a box to type in.
 *  ``value()``: undefined (leave it as it is), null (clear it) or what was typed. */
function secretField(f, id, changed) {
  const box = h('div', { class: 'secret-field' });
  let mode = f.set ? 'saved' : 'typing';   // saved | typing | clearing
  const input = h('input', { class: 'input', id, type: 'password', autocomplete: 'new-password', spellcheck: 'false', autocapitalize: 'off',
    maxlength: 200, oninput: changed });
  const paint = (focus) => {
    if (mode === 'saved') {
      const replace = h('button', { type: 'button', class: 'btn small-btn', onclick: () => { mode = 'typing'; paint(true); changed(); } }, 'Replace');
      const clear = h('button', { type: 'button', class: 'btn ghost small-btn', onclick: () => { mode = 'clearing'; paint(true); changed(); } }, 'Clear');
      replace.setAttribute('aria-label', `Replace ${f.label}`); clear.setAttribute('aria-label', `Clear ${f.label}`);
      box.replaceChildren(h('span', { class: 'secret-saved', id }, h('span', { 'aria-hidden': 'true' }, '••••••••'), h('span', { class: 'sr-only' }, 'Saved')), replace, clear);
      if (focus) replace.focus();
    } else if (mode === 'clearing') {
      const undo = h('button', { type: 'button', class: 'btn ghost small-btn', onclick: () => { mode = 'saved'; paint(true); changed(); } }, 'Keep it');
      box.replaceChildren(h('span', { class: 'secret-saved', id }, h('span', { class: 'tiny warn' }, 'Cleared when you save')), undo);
      if (focus) undo.focus();
    } else {
      input.value = '';
      input.placeholder = f.set ? 'Type the new one' : 'Not set';
      const back = f.set ? h('button', { type: 'button', class: 'btn ghost small-btn', onclick: () => { mode = 'saved'; paint(true); changed(); } }, 'Cancel') : null;
      box.replaceChildren(...[input, back].filter(Boolean));
      if (focus) input.focus();
    }
  };
  paint(false);
  return { el: box, value: () => (mode === 'clearing' ? null : mode === 'typing' && input.value ? input.value : undefined) };
}

/** One field as a control: { el (what goes in the form), value() (what to send, undefined: nothing), wide }. */
function control(f, id, changed) {
  if (f.type === 'secret') return secretField(f, id, changed);
  if (f.type === 'bool') {
    let on = !!f.value;
    return { el: toggle(on, f.label, (next) => { on = next; changed(); }), value: () => on, wide: true };
  }
  if (f.type === 'choice' || (f.type === 'port' && f.choices.length > 1)) {
    const sel = h('select', { class: 'input', id, onchange: changed }, f.choices.map((c) => h('option', { value: String(c), selected: c === f.value }, String(c))));
    return { el: sel, value: () => (f.type === 'port' ? Number(sel.value) : sel.value) };
  }
  if (f.type === 'port') return { el: h('input', { class: 'input mono', id, value: String(f.value), readonly: true, 'aria-readonly': 'true' }), value: () => f.value };
  if (f.type === 'int' || f.type === 'number') {
    const num = h('input', { class: 'input', id, type: 'number', inputmode: f.type === 'int' ? 'numeric' : 'decimal', min: f.limits[0], max: f.limits[1],
      step: f.type === 'int' ? 1 : 'any', value: String(f.value), oninput: changed });
    return { el: num, value: () => (num.value === '' ? f.value : Number(num.value)) };
  }
  const text = h('input', { class: `input${f.type === 'host' ? ' mono' : ''}`, id, type: 'text', value: f.value ?? '', maxlength: f.type === 'host' ? 253 : f.max || 100,
    spellcheck: 'false', autocomplete: 'off', autocapitalize: 'off', inputmode: f.type === 'host' ? 'url' : null,
    placeholder: f.type === 'host' ? 'A name or an IP address' : null, oninput: changed });
  return { el: text, value: () => text.value.trim() };
}

/** The status line: the dashboard's square dot (green: fine; amber: something to look at) and the module's own words. */
function statusLine(card) {
  if (!card.on) return h('p', { class: 'small flush row gap-s status-line' }, h('span', { class: 'dot', 'aria-hidden': 'true' }), h('span', null, "Switched off in Modules: it isn't running. Its settings are kept."));
  if (!card.background) return null;
  const s = card.status;
  if (!s) return h('p', { class: 'small flush row gap-s status-line muted' }, h('span', { class: 'dot', 'aria-hidden': 'true' }), h('span', null, 'Starting…'));
  return h('p', { class: 'small flush row gap-s status-line' }, h('span', { class: `dot ${s.ok ? 'ok' : 'warn'}`, 'aria-hidden': 'true' }), h('span', { style: 'overflow-wrap:anywhere' }, s.text));
}

/** What the gate last said of the module's home-network address: where the name led, or why it wasn't let through. */
function homeLine(card) {
  const address = card.fields.find((f) => f.type === 'host');
  if (!address || !card.on || !card.home || !address.value) return null;
  if (card.home.said) {   // (said once: a module that reports the reason itself has said it already)
    return (card.status?.text || '').includes(card.home.said) ? null : h('p', { class: 'tiny warn flush', style: 'overflow-wrap:anywhere' }, `Not connected: ${card.home.said}.`);
  }
  if (card.home.pin && card.home.pin !== address.value) return h('p', { class: 'tiny muted flush mono', style: 'overflow-wrap:anywhere' }, `${address.value} is ${card.home.pin}`);
  return null;
}

/** The card for module ``m`` (from /api/modules). It loads and saves by itself. */
export function moduleCard(m) {
  const body = h('div', { class: 'stack' }, h('p', { class: 'muted small flush' }, 'Loading…'));
  const card = h('section', { class: 'card stack module-card', 'aria-label': m.name, id: `module-${m.id}`, 'data-module-card': m.id },
    h('div', { class: 'row between wrap gap-s' }, h('h2', { class: 'h2' }, m.name), h('span', { class: 'tag' }, 'Imported')),
    m.description ? h('p', { class: 'muted small flush' }, m.description) : null, body);
  const live = h('div', { class: 'stack-s', role: 'status' });   // the status line and the address's note: kept fresh
  let timer = null, gone = false;
  const paintLive = (data) => live.replaceChildren(...[statusLine(data), homeLine(data)].filter(Boolean));

  function show(data) {
    const save = h('button', { type: 'submit', class: 'btn primary', disabled: true }, 'Save');
    const changed = () => { save.disabled = false; };
    const made = data.fields.map((f) => ({ f, ...control(f, `mod-${m.id}-${f.name}`, changed) }));
    const boxes = made.filter((c) => !c.wide).map((c) => h('div', { class: 'field-col' },
      h('label', { for: `mod-${m.id}-${c.f.name}` }, c.f.label), c.el, c.f.note ? h('span', { class: 'tiny muted' }, c.f.note) : null));
    const switches = made.filter((c) => c.wide).map((c) => h('div', { class: 'stack-s' }, c.el, c.f.note ? h('span', { class: 'tiny muted' }, c.f.note) : null));
    const form = h('form', { class: 'stack', novalidate: true },
      boxes.length ? h('div', { class: 'grid-2' }, boxes) : null, ...switches,
      made.length ? h('div', { class: 'row end' }, save) : null);
    form.onsubmit = async (ev) => {
      ev.preventDefault();
      const values = {};
      for (const c of made) { const v = c.value(); if (v !== undefined && !(c.f.type !== 'secret' && v === c.f.value)) values[c.f.name] = v; }
      save.disabled = true;
      try {
        const res = await put(`/api/addons/${m.id}/settings`, { values });
        toast(`${m.name} settings saved`, 'success');
        show(res);
      } catch (e) { toast(e.message, 'error', 8000); save.disabled = false; }
    };
    paintLive(data);
    const table = data.table ? [h('span', { class: 'field-label' }, data.table.title),
      h('div', { class: 'fact-list' }, data.table.rows.map(([what, tag]) => h('div', { class: 'fact-row' }, h('span', null, what), tag ? h('span', { class: 'muted' }, tag) : null)))] : [];
    body.replaceChildren(form, live, ...table, data.note ? h('p', { class: 'muted tiny flush' }, data.note) : null);
  }

  async function look() {   // the status line only: nothing typed in the form is touched
    if (gone) return;
    try { paintLive(await get(`/api/addons/${m.id}/settings`)); } catch { /* the next look will do */ }
    timer = setTimeout(look, EVERY);
  }
  get(`/api/addons/${m.id}/settings`).then((data) => { if (!gone) { show(data); timer = setTimeout(look, EVERY); } })
    .catch(() => body.replaceChildren(h('p', { class: 'muted small flush' }, `Couldn't load ${m.name}'s settings.`)));
  card.stop = () => { gone = true; clearTimeout(timer); };
  onLeave(card.stop);
  return card;
}

/** The cards for every imported module that has settings of its own, kept in step with ``S.modules``:
 *  { box (where they sit, under the Modules card), cards() (for the jump bar), refresh() (after an install,
 *  an update or a removal) }. A card already on the page is left as it is, so nothing typed in it is lost. */
export function moduleCards() {
  const box = h('div', { class: 'stack gap-20 min0', hidden: true });
  const made = new Map();   // id -> its card
  const refresh = (again = []) => {
    const want = (S.modules || []).filter((m) => m.imported && m.settings_card);
    for (const [id, card] of made) if (!want.some((m) => m.id === id) || again.includes(id)) { card.stop(); made.delete(id); }
    for (const m of want) if (!made.has(m.id)) made.set(m.id, moduleCard(m));
    box.replaceChildren(...want.map((m) => made.get(m.id)));
    box.hidden = !want.length;
  };
  refresh();
  return { box, cards: () => [...made.values()], refresh };
}
