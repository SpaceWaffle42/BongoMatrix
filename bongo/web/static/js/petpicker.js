// The Matrix's one pet, picked: the animal, then its shape (the dog has several), then its colour (coat ids are
// "shape.colour"), its name and what it wears. Used by the Pet page (petpage.js); nothing else knows about it.
import { h, humanize, toggle } from './ui.js';

export const PET_NAME = /^[A-Za-z0-9 '.!?-]{1,12}$/;
export const NAME_EMPTY = 'Give your pet a name';
export const NAME_RULE = "Names are 1 to 12 letters, digits or spaces (and ' . ! ? -), so they fit its name tag";

/** The name the Matrix gives this pet: the one saved (if it's a usable one), else its animal's own (the cat is Mochi, the
 *  dog Biscuit...), as ``pets.of`` does. ``kinds``: /api/pets' list; before it arrives only the cat's is known. */
export function petName(saved, kinds) {
  const n = typeof saved?.name === 'string' ? saved.name.trim() : '';
  if (PET_NAME.test(n)) return n;
  const kind = saved?.kind || 'cat';
  return kinds?.find((k) => k.kind === kind)?.name || (kind === 'cat' ? 'Mochi' : '');
}

/** The pet the Matrix really has for what is saved, as ``pets.of`` works it out: an animal it has no art for is the cat, a
 *  colour or accessory the animal doesn't have is its default (none), a name it can't draw is the animal's own. Until the
 *  animals' list arrives (``kinds`` empty, or only names) what can't be checked is left as saved. */
export function effectivePet(saved, kinds) {
  const s = saved || {}, list = kinds || [];
  const kind = list.length && !list.some((k) => k.kind === s.kind) ? 'cat' : (s.kind || 'cat');
  const e = list.find((k) => k.kind === kind);
  const known = !!e?.coats?.length;
  return { kind, name: petName({ name: s.name, kind }, list),
    coat: known ? (e.coats.some((c) => c.id === s.coat) ? s.coat : e.default) : (s.coat || ''),
    collar: known ? ((e.collars || []).includes(s.collar) ? s.collar : '') : (s.collar || ''),
    dress_up: s.dress_up !== false };
}

/** The picker for a pet ({kind, name, coat, collar, dress_up}; what is saved in the config's ``pet``).
 *  ``kinds``: /api/pets' list (until it arrives, the names the Matrix lists in its state: no pictures).
 *  ``onChange()`` runs after every change. Returns ``nodes`` (to put in a card), ``read()`` (``{ pet, error }``:
 *  what Save sends, or why it can't yet), ``setKinds(list)`` and ``entry()`` (the picked animal's catalogue entry). */
export function petPicker(saved, kinds, onChange = () => {}) {
  const pet = { coat: '', collar: '', ...(saved || {}), ...{ kind: effectivePet(saved, kinds).kind } };
  let nameBlank = !PET_NAME.test(String(pet.name || '').trim());   // none saved: its animal's own name, until you type one
  let catalogue = kinds;
  let kind = pet.kind;
  let coat = pet.coat;
  let collarPick = pet.collar || '';
  let dressUp = pet.dress_up !== false;
  const name = h('input', { class: 'input', id: 'pet-name', maxlength: 12, value: nameBlank ? '' : pet.name, autocomplete: 'off', spellcheck: 'false' });
  const nameError = h('span', { class: 'tiny danger row gap-xs field-error', id: 'pet-name-error', role: 'alert', hidden: true });
  const tiles = (label) => h('div', { class: 'pet-tiles', role: 'radiogroup', 'aria-label': label });
  const animals = tiles('Animal');
  const shapes = tiles('Shape');
  const colours = h('div', { class: 'coat-grid', role: 'radiogroup', 'aria-label': 'Colour' });
  const chosen = { animal: h('span', { class: 'mono strong' }), shape: h('span', { class: 'mono strong' }), colour: h('span', { class: 'mono strong' }) };
  const row = (label, said, box) => h('div', { class: 'stack gap-s' }, h('div', { class: 'row between' }, h('span', null, label), said), box);
  const shapeRow = row('Shape', chosen.shape, shapes);
  const colourRow = row('Colour', chosen.colour, colours);
  const collar = h('select', { class: 'input', id: 'pet-collar', onchange: () => { collarPick = collar.value; onChange(); } });
  const wardrobe = h('div', { class: 'grid-2', hidden: true },
    h('label', { class: 'field-col', for: collar.id }, 'Accessory', collar),
    h('div', { class: 'field-col' }, 'Seasonal outfits',
      toggle(dressUp, 'Dress up for Christmas and Valentine’s', (v) => { dressUp = v; onChange(); })));
  const entry = (k) => catalogue.find((e) => e.kind === k) || catalogue[0];
  const shapeOf = (id) => (/^[a-z_]+\.[a-z_]+$/.test(id || '') ? id.split('.')[0] : '');
  const tile = (attrs, face, label, on) => h('button', {
    type: 'button', class: 'pet-tile', role: 'radio', 'aria-checked': String(on), 'aria-label': label, title: label, ...attrs,
  }, face ? h('img', { src: face, alt: '' }) : h('span', { class: 'pet-tile-blank' }), h('span', { class: 'small' }, label));
  const show = () => {
    const e = entry(kind);
    kind = e.kind;
    const groups = catalogue.filter((k, i) => catalogue.findIndex((o) => (o.group || o.kind) === (k.group || k.kind)) === i);   // each animal's first kind
    animals.replaceChildren(...groups.map((g) => tile({ 'data-group': g.group || g.kind, onclick: () => pickKind(g.kind) },
      g.face, g.group_title || g.title, (g.group || g.kind) === (e.group || e.kind))));
    chosen.animal.textContent = e.group_title || e.title;
    if (nameBlank) name.value = petName({ kind: e.kind }, catalogue);
    const siblings = catalogue.filter((k) => (k.group || k.kind) === (e.group || e.kind));
    const options = siblings.flatMap((k) => {
      const shapesOf = [...new Set(k.coats.map((c) => c.shape || ''))];
      return (shapesOf.length ? shapesOf : ['']).map((sh) => ({ kind: k, shape: sh, first: k.coats.find((c) => (c.shape || '') === sh) }));
    });
    const current = shapeOf(coat) || (e.coats.find((c) => c.id === e.default)?.shape || '');
    shapes.replaceChildren(...options.map((o) => {
      const label = o.kind.title + (options.filter((x) => x.kind === o.kind).length > 1 && o.shape ? ` (${humanize(o.shape)})` : '');
      return tile({ 'data-kind': o.kind.kind, 'data-shape': o.shape, onclick: () => pickShape(o.kind.kind, o.shape) },
        o.first?.face || o.kind.face, label, o.kind.kind === kind && o.shape === current);
    }));
    shapeRow.hidden = options.length < 2;
    chosen.shape.textContent = e.title;
    const inShape = e.coats.filter((c) => (c.shape || '') === current);
    const list = inShape.length ? inShape : e.coats;
    if (list.length && !list.some((c) => c.id === coat)) coat = list.some((c) => c.id === e.default) ? e.default : (list[0]?.id || '');
    colours.replaceChildren(...list.map((c) => h('button', {
      type: 'button', class: 'coat', role: 'radio', title: c.name, 'aria-label': c.name, 'data-coat': c.id,
      'aria-checked': String(c.id === coat), onclick: () => { coat = c.id; show(); onChange(); },
    }, c.face ? h('img', { src: c.face, alt: '' }) : h('span', { class: 'small' }, c.name))));
    colourRow.hidden = list.length < 2;
    chosen.colour.textContent = list.find((c) => c.id === coat)?.name || '';
    const have = e.collars || [];
    if (e.coats.length && !have.includes(collarPick)) collarPick = '';   // (one this animal doesn't have: none)
    wardrobe.hidden = !have.length;
    collar.replaceChildren(h('option', { value: '' }, 'None'),
      ...have.map((c) => h('option', { value: c, selected: c === collarPick || null }, `${humanize(c)} ${e.accessory || 'collar'}`)));
  };
  const pickKind = (k) => {
    const was = entry(kind);
    const next = entry(k);
    if (!name.value.trim() || name.value.trim() === petName({ kind: was.kind }, catalogue)) { name.value = petName({ kind: next.kind }, catalogue) || name.value; nameBlank = false; }   // its name, unless you gave one
    kind = next.kind; coat = next.default || ''; collarPick = ''; show(); onChange();   // (another animal's accessory isn't this one's)
  };
  const pickShape = (k, sh) => {
    if (k !== kind) pickKind(k);
    const e = entry(kind);
    coat = (e.coats.find((c) => c.id === e.default && (c.shape || '') === sh) || e.coats.find((c) => (c.shape || '') === sh))?.id || coat;
    show(); onChange();
  };
  const nameProblem = (msg) => {   // the reason, on the field itself (an empty or unusable name can't be saved)
    nameError.hidden = !msg; nameError.textContent = msg || '';
    name.classList.toggle('bad', !!msg);
    if (msg) { name.setAttribute('aria-invalid', 'true'); name.setAttribute('aria-describedby', nameError.id); } else { name.removeAttribute('aria-invalid'); name.removeAttribute('aria-describedby'); }
  };
  name.addEventListener('input', () => { nameBlank = false; nameProblem(null); onChange(); });
  show();
  return {
    nodes: [row('Animal', chosen.animal, animals), shapeRow, colourRow,
      h('label', { class: 'field-col', for: name.id }, 'Name', name, nameError), wardrobe],
    entry: () => entry(kind),
    setKinds(list) { if (list?.length) { catalogue = list; show(); onChange(); } },
    nameProblem,
    focusName: () => name.focus(),
    /** What Save sends (the whole ``pet``), or ``error``: why it can't be saved yet. ``pet`` is always usable for a
     *  preview: a name that doesn't fit is left out (the Matrix then uses the animal's own). */
    read() {
      const n = name.value.trim();
      const outfits = wardrobe.hidden ? {} : { ...(collarPick ? { collar: collarPick } : {}), dress_up: dressUp };
      const out = { kind, ...(PET_NAME.test(n) ? { name: n } : {}), ...(coat ? { coat } : {}), ...outfits };
      return { pet: out, error: PET_NAME.test(n) ? null : (n ? NAME_RULE : NAME_EMPTY) };
    },
  };
}
