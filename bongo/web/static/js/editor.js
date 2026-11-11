// Module editor: style picker + a form generated from /api/modules, with a live preview.
import { previewPath, get, uploadMedia } from './api.js';
import { h, icon, toast, clone, same, humanize, toggle, segmented, store, reducedMotion } from './ui.js';
import { FrameLoop } from './led.js';
import { openPreview } from './mediaview.js';
import { S, onLeave, go, href, moduleById, styleOf, entryName, applyDraft, draftChanged } from './app.js';

/** Settings for a style: shared ones (unless the style redefines them) and the style's own. */
export function schemaFor(mod, style) {
  const own = mod?.styles?.[style]?.settings || [];
  const names = new Set(own.map((f) => f.name));
  return { shared: (mod?.settings || []).filter((f) => !names.has(f.name)), own };
}

export function render(view, match) {
  const i = Number(match[1]);
  if (!S.draft?.[i]) { toast('That playlist entry no longer exists'); go('/', { replace: true }); return; }
  const e = clone(S.draft[i]);
  const original = clone(e);
  const mod = moduleById(e.module);
  let showSafe = store.get('editorSafe', true);

  const edited = h('span', { class: 'pill-warn', hidden: true }, 'Edited');
  const head = h('div', { class: 'editor-head' },
    h('div', { class: 'row gap-s wrap' },
      h('a', { href: href('/playlist'), 'data-route': '/playlist', class: 'crumb' }, icon('left'), 'Playlist'),
      h('span', { class: 'crumb-sep', 'aria-hidden': 'true' }, '/'),
      h('h1', { class: 'h1 small-h' }, entryName(e)), edited),
    h('div', { class: 'row gap-s wrap' },
      h('button', { type: 'button', class: 'btn ghost', onclick: cancel }, 'Cancel'),
      h('button', { type: 'button', class: 'btn', onclick: keepDraft, title: 'Keep these edits in the playlist draft without sending them yet' }, 'Save to draft'),
      h('button', { type: 'button', class: 'btn primary', onclick: apply }, 'Apply to Matrix')));
  view.append(head);
  if (!mod) {
    view.append(h('div', { class: 'card stack' }, h('p', { class: 'error-box' }, `The module “${e.module}” isn't installed on this Matrix, so it can't be edited.`),
      h('button', { type: 'button', class: 'btn', onclick: () => go('/playlist') }, 'Back to the playlist')));
    return;
  }

  const problems = h('div', { class: 'error-box', role: 'alert', hidden: true });
  const styles = h('div', { class: 'style-grid' });
  const sections = h('div', { class: 'stack' });
  let thumbs = [];
  onLeave(() => thumbs.forEach((t) => t.stop()));

  // -- style picker ------------------------------------------------------------------------
  function paintStyles() {
    thumbs.forEach((t) => t.stop()); thumbs = [];
    const card = (name) => {
      const canvas = h('canvas', { class: 'led-canvas mini', 'aria-hidden': 'true' });
      const btn = h('button', { type: 'button', class: 'style-card', 'data-style': name, 'aria-pressed': String(styleOf(e) === name),
        onclick: () => pickStyle(name) }, h('span', { class: 'led-frame mini' }, canvas), h('span', null, humanize(name)));
      thumbs.push(new FrameLoop(canvas, { interval: Infinity,
        path: () => previewPath({ module: e.module, style: name, settings: validFor(name, e.settings) }) }));
      return btn;
    };
    // styles with a group (a Clock's Digital, Word, Analogue...) sit under that heading, in first-seen order
    const groups = new Map();
    Object.entries(mod.styles).forEach(([name, s]) => {
      const g = s.group || '';
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(name);
    });
    if (groups.size === 1 && groups.has('')) styles.replaceChildren(...Object.keys(mod.styles).map(card));
    else styles.replaceChildren(...[...groups].map(([g, names]) => h('div', { class: 'style-group' },
      g ? h('h3', { class: 'style-group-head' }, g) : null, h('div', { class: 'style-grid' }, ...names.map(card)))));
    styles.className = groups.size === 1 && groups.has('') ? 'style-grid' : 'style-groups';
  }
  const validFor = (style, settings) => {
    const { shared, own } = schemaFor(mod, style);
    const fields = Object.fromEntries([...shared, ...own].map((f) => [f.name, f]));
    // a setting the new style lacks, or a choice it doesn't offer (Metro's palettes on Classic), is dropped
    const fits = (f, v) => f.type !== 'choice' || (Array.isArray(v) ? v.every((x) => f.choices.includes(x)) : f.choices.includes(v));
    return Object.fromEntries(Object.entries(settings).filter(([k, v]) => fields[k] && fits(fields[k], v)));
  };
  function pickStyle(name) {
    if (styleOf(e) === name) return;
    const kept = validFor(name, e.settings);
    const dropped = Object.keys(e.settings).filter((k) => !(k in kept));
    const { shared, own } = schemaFor(mod, name);
    const has = new Set([...shared, ...own].map((f) => f.name));
    const reset = dropped.filter((k) => has.has(k)), gone = dropped.filter((k) => !has.has(k));
    e.style = name;
    e.settings = kept;
    if (gone.length) toast(`${humanize(name)} has no ${gone.map(humanize).join(', ')}; ${gone.length > 1 ? 'those were' : 'that was'} dropped.`);
    if (reset.length) toast(`${reset.map(humanize).join(', ')} went back to ${humanize(name)}'s default${reset.length > 1 ? 's' : ''}.`);
    styles.querySelectorAll('.style-card').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.style === name)));
    paintFields(); changed();
  }

  // -- generated form ----------------------------------------------------------------------
  function paintFields() {
    const { shared, own } = schemaFor(mod, styleOf(e));
    const seconds = h('input', { class: 'input', type: 'number', id: 'f-seconds', min: 1, max: 3600, step: 'any', value: e.seconds });
    seconds.addEventListener('input', () => { const v = Number(seconds.value); if (v > 0) { e.seconds = v; changed(); } });
    sections.replaceChildren(
      h('section', { class: 'card stack', 'aria-label': 'Timing' }, h('h2', { class: 'h2' }, 'Timing'),
        h('div', { class: 'grid-2' },
          e.module === 'splash'  // its length comes from the picked file: one pass of a GIF
            ? h('div', { class: 'field-col' }, h('span', null, 'Length'), h('span', { class: 'muted small' }, 'One pass of the picked Splash file. Choose the files in Settings › Splash.'))
            : h('label', { class: 'field-col', for: 'f-seconds' }, 'Seconds on the panel', seconds),
          h('div', { class: 'field-col' }, h('span', null, 'Rotation'), toggle(e.enabled, 'Enabled', (v) => { e.enabled = v; changed(); })))),
      ...[basicCard([...shared, ...own].filter((f) => !f.advanced)),
        advancedCard(shared.filter((f) => f.advanced), own.filter((f) => f.advanced))].filter(Boolean));  // none for Splash
  }
  const counter = (fields) => {
    const count = h('span', { class: 'tiny pill-warn changed-count' });
    count.recount = () => { const n = fields.filter((f) => differsFrom(f, e.settings)).length; count.textContent = `${n} changed`; count.hidden = !n; };
    count.recount();
    return count;
  };
  /** The settings most people change, always open. */
  function basicCard(fields) {
    if (!fields.length) return null;
    const count = counter(fields);
    const sec = h('section', { class: 'card stack', 'aria-label': 'Settings' },
      h('div', { class: 'row between wrap' }, h('h2', { class: 'h2' }, `${mod.name} settings`), count),
      h('div', { class: 'fields' }, fields.map(fieldRow)));
    sec.recount = count.recount;
    return sec;
  }
  /** Everything else, folded away until asked for. Shows how many are changed even when closed. */
  function advancedCard(shared, own) {
    const all = [...shared, ...own];
    if (!all.length) return null;
    const count = counter(all);
    let open = store.get('editorAdvanced', false);
    const body = h('div', { class: 'stack' },
      shared.length ? h('div', { class: 'stack-s' }, h('h3', { class: 'eyebrow flush' }, 'Every style'), h('div', { class: 'fields' }, shared.map(fieldRow))) : null,
      own.length ? h('div', { class: 'stack-s' }, h('h3', { class: 'eyebrow flush' }, `${humanize(styleOf(e))} only`), h('div', { class: 'fields' }, own.map(fieldRow))) : null);
    const btn = h('button', { type: 'button', class: 'disclosure', 'aria-expanded': String(open) },
      h('span', { class: 'disclosure-mark', 'aria-hidden': 'true' }, open ? '−' : '+'),
      h('span', { class: 'h2' }, 'Advanced settings'),
      h('span', { class: 'muted small' }, `${all.length} more: fonts, sizes, timings, colours`));
    body.hidden = !open;
    btn.addEventListener('click', () => {
      open = !open; store.set('editorAdvanced', open);
      body.hidden = !open; btn.setAttribute('aria-expanded', String(open));
      btn.querySelector('.disclosure-mark').textContent = open ? '−' : '+';
    });
    const sec = h('section', { class: 'card stack', 'aria-label': 'Advanced settings' }, h('div', { class: 'row between wrap' }, btn, count), body);
    sec.recount = count.recount;
    return sec;
  }

  function fieldRow(f) {
    const row = h('div', { class: ['items', 'palettes', 'media', 'place'].includes(f.type) || f.swatches ? 'field wide' : 'field' });
    const id = `f-${f.name}`;
    const cur = () => (f.name in e.settings ? e.settings[f.name] : f.default);
    const differs = () => differsFrom(f, e.settings);
    const set = (v, repaint = false) => {
      if (isDefault(f, v)) delete e.settings[f.name]; else e.settings[f.name] = v;
      row.classList.toggle('changed', differs());
      changed();
      if (repaint) paint();
    };
    const paint = () => {
      row.classList.toggle('changed', differs());
      row.replaceChildren(
        h('div', { class: 'field-head' },
          h('label', { class: 'field-label', for: id, id: `${id}-l` }, humanize(f.name)),
          h('span', { class: 'changed-mark' }, 'changed'),
          h('button', { type: 'button', class: 'link reset', 'aria-label': `Reset ${humanize(f.name)} to default`,
            onclick: () => { delete e.settings[f.name]; changed(); paint(); } }, 'Reset')),
        control(f, id, cur(), set, {
          // a place search also fills in the coordinates, then the form is redrawn to show them
          picked: (place) => {
            e.settings[f.latitude] = place.latitude; e.settings[f.longitude] = place.longitude;
            paintFields(); changed();
          },
        }),
        h('div', { class: 'tiny muted' }, `Default: ${showDefault(f)}`));
    };
    paint();
    return row;
  }

  // -- preview -------------------------------------------------------------------------------
  const canvas = h('canvas', { class: 'led-canvas', role: 'img', 'aria-label': `Draft preview of ${entryName(e)}` });
  const err = h('p', { class: 'error-box small', role: 'status', hidden: true });
  const safeBtn = h('button', { type: 'button', class: 'chip', 'aria-pressed': String(showSafe) }, 'Safe area');
  const playBtn = h('button', { type: 'button', class: 'chip', 'aria-pressed': String(!reducedMotion()) }, 'Animate');
  let t0 = Date.now() / 1000, frames = 0;
  const preview = new FrameLoop(canvas, {
    interval: reducedMotion() ? Infinity : 120,
    path: () => previewPath({ ...e, style: styleOf(e) }, t0 + (frames++) * 0.12),
    led: () => ({ safeArea: S.config?.safe_area, panel: S.state?.panel, showSafe }),
    onError: (x) => { err.hidden = !x || x.status === 401; err.textContent = x ? `Preview failed: ${x.message}` : ''; },
  });
  onLeave(() => preview.stop());
  safeBtn.onclick = () => { showSafe = !showSafe; store.set('editorSafe', showSafe); safeBtn.setAttribute('aria-pressed', String(showSafe)); preview.redraw(); };
  playBtn.onclick = () => {
    const on = playBtn.getAttribute('aria-pressed') !== 'true';
    playBtn.setAttribute('aria-pressed', String(on));
    preview.o.interval = on ? 120 : Infinity;
    preview.refresh();
  };
  const sa = S.config?.safe_area || {};
  const aside = h('section', { class: 'editor-preview', 'aria-label': 'Preview' },
    h('div', { class: 'card stack' },
      h('div', { class: 'row between wrap' }, h('h2', { class: 'h2' }, 'Draft preview'), h('div', { class: 'row gap-s' }, playBtn, safeBtn)),
      h('div', { class: 'led-frame' }, canvas), err,
      h('p', { class: 'muted tiny flush mono' }, `Safe area T${sa.top || 0} R${sa.right || 0} B${sa.bottom || 0} L${sa.left || 0} · `,
        h('a', { href: href('/settings'), 'data-route': '/settings' }, 'change'))),
    h('div', { class: 'note' }, icon('info'), h('span', null, 'Drawn by the Matrix itself, frame by frame, with your unsaved settings. Nothing changes on the panel until you apply.')));

  view.append(h('div', { class: 'editor-grid' },
    h('div', { class: 'editor-form stack' }, problems,
      h('section', { class: 'card stack', 'aria-label': 'Style' }, h('h2', { class: 'h2' }, 'Style'),
        mod.description ? h('p', { class: 'muted small flush' }, mod.description) : null,
        mod.proposed ? h('div', { class: 'note' }, icon('info'), h('span', null, 'Proposed module: not built yet. The previews are prototypes, and saving it here only works in the demo.')) : null,
        styles),
      sections),
    aside));
  let thumbTimer = 0;
  paintStyles(); paintFields(); changed();

  function changed() {
    edited.hidden = same(e, original);
    sections.querySelectorAll('section').forEach((s) => s.recount?.());
    if (!Number.isFinite(preview.o.interval)) preview.refresh();
    clearTimeout(thumbTimer);
    thumbTimer = setTimeout(() => thumbs.forEach((t) => t.refresh()), 800);
  }
  function cancel() {
    if (!same(e, original) && !confirm('Throw away your edits to this module?')) return;
    go('/playlist');
  }
  function keepDraft() { S.draft[i] = clone(e); draftChanged(); go('/playlist'); }
  async function apply() {
    S.draft[i] = clone(e);
    draftChanged();
    if (await applyDraft()) { go('/playlist'); return; }
    const mine = S.problems.filter((p) => !/^playlist\[\d+\]/.test(p) || p.startsWith(`playlist[${i}]`));
    problems.hidden = !S.problems.length;
    problems.replaceChildren(h('strong', null, "The Matrix didn't accept this:"), h('ul', null, (mine.length ? mine : S.problems).map((p) => h('li', null, p))));
    problems.scrollIntoView({ block: 'nearest' });
  }
}

const isDefault = (f, v) => same(v, f.default) || (f.type === 'colour' && String(v).toLowerCase() === String(f.default).toLowerCase());
/** A setting counts as changed only when its value differs from the default (an explicit default is not a change). */
const differsFrom = (f, settings) => f.name in settings && !isDefault(f, settings[f.name]);
const showDefault = (f) => (f.default == null ? 'Auto' : typeof f.default === 'object' ? (Array.isArray(f.default) ? `${f.default.length} item${f.default.length === 1 ? '' : 's'}` : 'object')
  : typeof f.default === 'boolean' ? (f.default ? 'On' : 'Off') : String(f.default) || '(empty)');

function rangeFor(f) {
  const d = Number(f.default), n = f.name;
  if (/speed/.test(n)) return f.type === 'int' ? { min: 1, max: Math.max(100, d * 3), step: 1 } : { min: 0, max: Math.max(4, d * 4), step: 0.05 };
  if (f.type === 'number' && d >= 0 && d <= 1) return { min: 0, max: 1, step: 0.05 };
  if (/brightness|glow|halo|opacity/.test(n)) return { min: 0, max: Math.max(1, d * 2), step: 0.05 };
  return null;
}

/** One input for one setting, by schema type. `set(value, repaint)` stores it. */
function control(f, id, v, set, hooks = {}) {
  const T = f.type;
  if (T === 'place') return placePicker(id, v, set, hooks.picked);
  if (T === 'bool') { const t = toggle(v, v ? 'On' : 'Off', (x) => set(x, true)); t.id = id; return t; }
  if (T === 'int' || T === 'number') return numberInput(f, id, v, set);
  if (T === 'media') return mediaPicker(id, v, set);
  if (T === 'palettes') return paletteList(id, v, set);
  if (T === 'items') return itemList(f, id, v, set);
  if (T === 'colour') {
    const hex = h('input', { class: 'input mono hex', value: v, maxlength: 7, 'aria-label': `${humanize(f.name)} hex value`, spellcheck: 'false' });
    const pick = h('input', { type: 'color', class: 'swatch-input', id, value: /^#[0-9a-f]{6}$/i.test(v) ? v : '#000000' });
    pick.addEventListener('input', () => { hex.value = pick.value.toUpperCase(); set(hex.value); });
    hex.addEventListener('input', () => { if (/^#[0-9a-f]{6}$/i.test(hex.value)) { pick.value = hex.value; set(hex.value.toUpperCase()); } });
    return h('div', { class: 'row gap-s' }, pick, hex);
  }
  if (T === 'choice' && (Array.isArray(f.default) || (f.default === 'all' && f.choices.includes('all')))) return multiChoice(f, id, v, set);
  if (T === 'choice' && !Array.isArray(f.default)) {
    if (f.choices.length <= 4) { const s = segmented(f.choices, v, (x) => set(x)); s.setAttribute('aria-labelledby', `${id}-l`); return s; }
    const sel = h('select', { class: 'input', id }, f.choices.map((c) => h('option', { value: c, selected: c === v }, humanize(c))));
    sel.addEventListener('change', () => set(sel.value));
    return sel;
  }
  if (T === 'text') {
    const inp = h('input', { class: 'input', id, value: v ?? '', spellcheck: 'false' });
    inp.addEventListener('input', () => set(inp.value));
    return inp;
  }
  if (T === 'auto') {
    const auto = v == null;
    const t = toggle(auto, 'Auto', (on) => set(on ? null : 0, true));
    t.id = id;
    return h('div', { class: 'row gap-s wrap' }, t, auto ? h('span', { class: 'muted small' }, 'Worked out automatically')
      : numberInput({ ...f, type: 'number' }, `${id}-n`, v, set, `${humanize(f.name)} value`));
  }
  if (T === 'list' && Array.isArray(f.default) && f.default.length && f.default.every((x) => typeof x === 'string')) return chips(f, id, v, set);
  return jsonArea(f, id, v, set);
}

/** Tick any number of choices (e.g. which palettes are in rotation). "all" is its own chip when offered.
 * With ``f.groups`` (value -> heading) the chips sit under their headings (holidays by pack), each
 * heading saying how many are ticked; ``f.labels`` (value -> words) names them. */
function multiChoice(f, id, v, set) {
  const options = f.choices.filter((c) => c !== 'all');
  const name = (c) => f.labels?.[c] ?? humanize(c);
  const box = h('div', { class: f.swatches ? 'chip-grid palette-grid' : f.groups ? 'stack-s' : 'chip-grid', role: 'group', id, 'aria-labelledby': `${id}-l` });
  const opened = new Set(), seen = new Set();  // which groups are open survives a repaint
  const paint = (value) => {
    const all = value === 'all';
    const picked = new Set(all ? [] : value || []);
    const toggle = (c) => {
      const now = new Set(all ? options : picked);
      if (now.has(c)) now.delete(c); else now.add(c);
      const next = options.filter((o) => now.has(o));
      set(next); paint(next);
    };
    const chip = (c) => h('button', { type: 'button', class: f.swatches?.[c] ? 'chip palette-chip' : 'chip', 'aria-pressed': String(all || picked.has(c)),
      title: f.swatches ? name(c) : null,  // the full name when a long one is cut short
      onclick: () => toggle(c) }, f.swatches?.[c] ? swatch(f.swatches[c]) : null, h('span', { class: 'chip-label' }, name(c)));
    if (f.groups) {
      const heads = [...new Set(options.map((c) => f.groups[c] || 'Other'))];
      box.replaceChildren(...heads.map((head) => {
        const mine = options.filter((c) => (f.groups[c] || 'Other') === head);
        const ticked = mine.filter((c) => all || picked.has(c)).length;
        if (!seen.has(head)) { seen.add(head); if (ticked) opened.add(head); }  // start with the used groups open
        const d = h('details', { class: 'chip-group', open: opened.has(head) || null },
          h('summary', null, h('span', null, head), h('span', { class: 'muted small' }, ticked ? `${ticked} of ${mine.length}` : `${mine.length}`)),
          h('div', { class: 'chip-grid' }, mine.map(chip)));
        d.addEventListener('toggle', () => { if (d.open) opened.add(head); else opened.delete(head); });
        return d;
      }));
      return;
    }
    box.replaceChildren(
      ...(f.choices.includes('all') ? [h('button', { type: 'button', class: 'chip', 'aria-pressed': String(all),
        onclick: () => { const next = all ? [] : 'all'; set(next); paint(next); } }, 'All')] : []),
      ...options.map(chip));
  };
  paint(v);
  return box;
}

/** Your own palettes: a name and three colours each, like v2. Shared by every style of the module. */
const PALETTE_PARTS = [['background', 'Background'], ['wax', 'Wax'], ['highlight', 'Highlight']];
/** Background | wax | highlight, drawn the same way for built-in and custom palettes. */
const paint3 = (el, [bg, wax, hi]) => { el.style.background = `linear-gradient(90deg, ${bg} 0 34%, ${wax} 34% 72%, ${hi} 72%)`; };
const swatch = (colours) => { const s = h('span', { class: 'palette-swatch small', 'aria-hidden': 'true' }); paint3(s, colours); return s; };
function paletteList(id, v, set) {
  let list = clone(Array.isArray(v) ? v : []);
  const box = h('div', { class: 'stack-s', id });
  const save = () => set(clone(list), false);
  const paint = () => {
    box.replaceChildren(
      ...list.map((p, i) => {
        const tile = h('div', { class: 'palette-swatch', 'aria-hidden': 'true' });
        const tint = () => paint3(tile, [p.background, p.wax, p.highlight]);
        tint();
        const name = h('input', { class: 'input', id: `${id}-${i}-name`, value: p.name || '', maxlength: 24, 'aria-label': `Palette ${i + 1} name` });
        name.addEventListener('input', () => { p.name = name.value; save(); });
        const colours = PALETTE_PARTS.map(([key, label]) => {
          const pick = h('input', { type: 'color', class: 'swatch-input', id: `${id}-${i}-${key}`, value: /^#[0-9a-f]{6}$/i.test(p[key]) ? p[key] : '#000000' });
          pick.addEventListener('input', () => { p[key] = pick.value.toUpperCase(); tint(); save(); });
          return h('label', { class: 'palette-colour tiny muted', for: pick.id }, pick, label);
        });
        const on = toggle(p.enabled !== false, 'In rotation', (x) => { p.enabled = x; save(); });
        const remove = h('button', { type: 'button', class: 'btn ghost danger-btn', 'aria-label': `Remove palette ${p.name || i + 1}` }, 'Remove');
        remove.addEventListener('click', () => { list.splice(i, 1); save(); paint(); });
        return h('div', { class: 'palette-row' }, tile,
          h('div', { class: 'stack-s grow' }, name, h('div', { class: 'row gap-s wrap' }, ...colours)),
          h('div', { class: 'stack-s end' }, on, remove));
      }),
      ...(list.length ? [] : [h('p', { class: 'muted small flush' }, 'No custom palettes yet. Each one is a name and three colours.')]),
      h('div', null, addBtn));
  };
  const addBtn = h('button', { type: 'button', class: 'btn' }, '+ Add palette');
  addBtn.addEventListener('click', () => {
    list.push({ name: `My palette ${list.length + 1}`, background: '#000000', wax: '#FF5A1F', highlight: '#FFB547', enabled: true });
    save(); paint();
    box.querySelector(`#${id}-${list.length - 1}-name`)?.select();
  });
  paint();
  return box;
}

/** A list of entries (notices, events...) as small forms, from the module's ``item_fields``.
 *  Fields marked ``more`` are tucked under each entry's "More options". Empty fields are left out. */
const DAY_NAMES = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
function itemList(f, id, v, set) {
  const list = clone(Array.isArray(v) ? v : []);
  const box = h('div', { class: 'stack-s', id });
  const save = () => set(clone(list.map((it) => Object.fromEntries(Object.entries(it).filter(([, x]) => x !== '' && x != null && !(Array.isArray(x) && !x.length))))), false);
  const label = humanize(f.name).replace(/s$/, '').toLowerCase();
  const input = (fd, it, fid) => {
    const value = it[fd.name] ?? '';
    const put = (x) => { it[fd.name] = x; save(); };
    if (fd.type === 'longtext') {
      const ta = h('textarea', { class: 'input', id: fid, rows: 2, placeholder: fd.placeholder || '' }); ta.value = value;
      ta.addEventListener('input', () => put(ta.value)); return ta;
    }
    if (fd.type === 'days') {
      const picked = new Set(Array.isArray(value) ? value.map((d) => String(d).toLowerCase().slice(0, 3)) : []);
      return h('div', { class: 'chip-grid', role: 'group', id: fid }, DAY_NAMES.map((d) => {
        const b = h('button', { type: 'button', class: 'chip', 'aria-pressed': String(picked.has(d)) }, humanize(d));
        b.addEventListener('click', () => { picked.has(d) ? picked.delete(d) : picked.add(d); b.setAttribute('aria-pressed', String(picked.has(d))); put(DAY_NAMES.filter((x) => picked.has(x))); });
        return b;
      }));
    }
    if (fd.type === 'choice') {  // options: [[value, label, group?], ...]; '' means "not set"
      const option = ([v, l]) => h('option', { value: v, selected: String(value) === v || null }, l);
      const groups = [...new Set(fd.options.map((o) => o[2]).filter(Boolean))];
      const sel = h('select', { class: 'input', id: fid }, fd.options.filter((o) => !o[2]).map(option),
        groups.map((g) => h('optgroup', { label: g }, fd.options.filter((o) => o[2] === g).map(option))));
      sel.addEventListener('change', () => put(sel.value));
      return sel;
    }
    if (fd.type === 'colour') {
      const on = !!value;
      const pick = h('input', { type: 'color', class: 'swatch-input', id: fid, value: /^#[0-9a-f]{6}$/i.test(value) ? value : '#FFB547', disabled: !on });
      pick.addEventListener('input', () => put(pick.value.toUpperCase()));
      return h('div', { class: 'row gap-s' }, toggle(on, on ? 'Own colour' : 'Module colour', (x) => { pick.disabled = !x; put(x ? pick.value.toUpperCase() : null); }), pick);
    }
    const type = { date: 'date', time: 'time' }[fd.type] || 'text';
    const inp = h('input', { class: 'input', id: fid, type, placeholder: fd.placeholder || '', value: type === 'date' ? String(value).slice(0, 10) : value });
    inp.addEventListener('input', () => put(inp.value));
    return inp;
  };
  const paint = () => {
    box.replaceChildren(
      ...list.map((it, i) => {
        const main = f.fields.filter((fd) => !fd.more), more = f.fields.filter((fd) => fd.more);
        const cell = (fd) => { const fid = `${id}-${i}-${fd.name}`; return h('label', { class: 'field-col', for: fid }, fd.label || humanize(fd.name), input(fd, it, fid)); };
        const extra = more.length ? h('details', { class: 'item-more', open: more.some((fd) => it[fd.name] != null && it[fd.name] !== '') },
          h('summary', null, 'More options (dates, days, times)'), h('div', { class: 'grid-2' }, more.map(cell))) : null;
        const remove = h('button', { type: 'button', class: 'btn ghost danger-btn', 'aria-label': `Remove ${label} ${i + 1}` }, 'Remove');
        remove.addEventListener('click', () => { list.splice(i, 1); save(); paint(); });
        return h('div', { class: 'item-card stack-s' },
          h('div', { class: 'row between' }, h('span', { class: 'eyebrow' }, `${humanize(label)} ${i + 1}`), remove),
          h('div', { class: main.length > 1 && !main.some((fd) => fd.type === 'longtext') ? 'grid-2' : 'stack-s' }, main.map(cell)), extra);
      }),
      ...(list.length ? [] : [h('p', { class: 'muted small flush' }, `Nothing here yet.`)]),
      h('div', null, add));
  };
  const add = h('button', { type: 'button', class: 'btn' }, `+ Add ${label}`);
  add.addEventListener('click', () => { list.push({}); paint(); box.querySelector(`#${id}-${list.length - 1}-${f.fields[0].name}`)?.focus(); });
  paint();
  return box;
}

/** Type a town and pick it from a list of real places (as in v2): the name and its latitude and
 *  longitude are set together, so the location is always one that exists. Typing alone saves nothing. */
function placePicker(id, v, set, picked) {
  const input = h('input', { class: 'input', id, value: v ?? '', autocomplete: 'off', spellcheck: 'false', role: 'combobox',
    'aria-autocomplete': 'list', 'aria-expanded': 'false', 'aria-controls': `${id}-list`, placeholder: 'Start typing a town or city' });
  const list = h('ul', { class: 'place-list', id: `${id}-list`, role: 'listbox', hidden: true });
  const note = h('span', { class: 'tiny muted', role: 'status' }, v ? `Using ${v}` : '');
  let results = [], active = -1, timer = 0, seq = 0;
  const close = () => { list.hidden = true; input.setAttribute('aria-expanded', 'false'); active = -1; };
  const choose = (p) => {
    input.value = p.label; close();
    note.textContent = `Using ${p.label} (${p.latitude.toFixed(2)}, ${p.longitude.toFixed(2)})`;
    set(p.label); picked?.(p);
  };
  const paint = () => {
    list.replaceChildren(...results.map((p, i) => {
      const li = h('li', { role: 'option', id: `${id}-opt-${i}`, class: 'place-option', 'aria-selected': String(i === active) },
        h('span', { class: 'strong' }, p.name), h('span', { class: 'muted small' }, [p.region, p.country].filter(Boolean).join(', ')));
      li.addEventListener('mousedown', (ev) => { ev.preventDefault(); choose(p); });
      return li;
    }));
    list.hidden = !results.length; input.setAttribute('aria-expanded', String(!list.hidden));
    input.setAttribute('aria-activedescendant', active >= 0 ? `${id}-opt-${active}` : '');
  };
  input.addEventListener('input', () => {
    clearTimeout(timer);
    const q = input.value.trim();
    note.textContent = q && q !== v ? 'Pick a place from the list to use it.' : (v ? `Using ${v}` : '');
    if (q.length < 2) { results = []; paint(); return; }
    timer = setTimeout(async () => {
      const mine = ++seq;
      try {
        const res = await get(`/api/places?q=${encodeURIComponent(q)}`);
        if (mine !== seq) return;
        results = res.places || []; active = results.length ? 0 : -1; paint();
        if (!results.length) note.textContent = `No places called “${q}” found. Try another spelling.`;
      } catch (err) { if (mine === seq) note.textContent = err.message; }
    }, 250);
  });
  input.addEventListener('keydown', (ev) => {
    if (list.hidden) return;
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault(); active = (active + (ev.key === 'ArrowDown' ? 1 : -1) + results.length) % results.length; paint();
    } else if (ev.key === 'Enter' && active >= 0) { ev.preventDefault(); choose(results[active]); }
    else if (ev.key === 'Escape') close();
  });
  input.addEventListener('blur', () => setTimeout(close, 100));
  return h('div', { class: 'place-picker stack-s' }, h('div', { class: 'place-box' }, input, list), note);
}

/** Pick a file from the media library (built-in/ and uploaded/), or upload a new one. */
function mediaPicker(id, v, set) {
  const sel = h('select', { class: 'input', id });
  const file = h('input', { type: 'file', class: 'sr', id: `${id}-up`, accept: 'image/*,video/*,.gif,.webp,.mkv' });
  const upBtn = h('button', { type: 'button', class: 'btn' }, icon('upload'), 'Upload…');
  const note = h('span', { class: 'muted tiny', role: 'status' });
  const fill = async (want) => {
    try {
      const lib = await get('/api/media');
      const group = (label, files) => h('optgroup', { label }, files.map((f) => h('option', { value: f.name, selected: f.name === want }, `${f.file}${f.kind === 'video' ? ' (video)' : ''}`)));
      const known = [...lib.folders['built-in'], ...lib.folders.uploaded].some((f) => f.name === want);
      sel.replaceChildren(
        ...(known ? [] : [h('option', { value: want, selected: true }, `${want} (missing)`)]),
        group('Uploaded', lib.folders.uploaded), group('Built-in', lib.folders['built-in']));
    } catch (err) { note.textContent = `Couldn't list media: ${err.message}`; }
  };
  sel.addEventListener('change', () => set(sel.value));
  upBtn.addEventListener('click', () => file.click());
  file.addEventListener('change', async () => {
    const pick = file.files?.[0];
    if (!pick) return;
    upBtn.disabled = true;
    note.textContent = `Uploading ${pick.name}…`;
    try {
      const { name } = await uploadMedia(pick);
      note.textContent = '';
      toast(`Added ${name.replace('uploaded/', '')}`, 'success');
      await fill(name);
      set(name);
    } catch (err) { note.textContent = ''; toast(err.message, 'error', 8000); }
    finally { upBtn.disabled = false; file.value = ''; }
  });
  fill(v);
  const look = h('button', { type: 'button', class: 'btn', onclick: () => openPreview(sel.value) }, icon('play'), 'Preview');
  return h('div', { class: 'stack-s' }, h('div', { class: 'row gap-s wrap' }, sel, look, upBtn, file), note);
}

function numberInput(f, id, v, set, label) {
  const int = f.type === 'int';
  const num = h('input', { class: 'input num', type: 'number', id, value: v ?? 0, step: int ? 1 : 'any', inputmode: int ? 'numeric' : 'decimal', 'aria-label': label });
  const r = rangeFor(f);
  const rng = r && h('input', { type: 'range', class: 'range grow', min: r.min, max: r.max, step: int ? 1 : r.step, value: v ?? 0, 'aria-label': `${humanize(f.name)} slider` });
  num.addEventListener('input', () => {
    if (num.value === '') return;
    const x = int ? Math.round(Number(num.value)) : Number(num.value);
    if (Number.isFinite(x)) { set(x); if (rng) rng.value = x; }
  });
  rng?.addEventListener('input', () => { num.value = rng.value; set(int ? Math.round(Number(rng.value)) : Number(rng.value)); });
  return rng ? h('div', { class: 'row gap-s' }, rng, num) : num;
}

function chips(f, id, v, set) {
  const list = Array.isArray(v) ? [...v] : [];
  const known = new Map((f.suggestions || []).map((x) => [x.value, x]));
  const inp = h('input', { class: 'input grow', id, placeholder: f.suggestions ? 'Or paste a web address and press Enter' : 'Add an item and press Enter', spellcheck: 'false' });
  const add = () => { const x = inp.value.trim(); if (!x || list.includes(x)) return; set([...list, x], true); };
  inp.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); add(); } });
  const label = (x) => known.get(x)?.name;
  return h('div', { class: 'stack-s' },
    h('ul', { class: 'chips' }, list.map((x, k) => h('li', { class: 'chip-item' },
      label(x) ? h('span', { class: 'chip-text named', title: x }, label(x)) : h('span', { class: 'chip-text', title: x }, x),
      h('button', { type: 'button', class: 'chip-x', 'aria-label': `Remove ${label(x) || x}`, onclick: () => set(list.filter((_, j) => j !== k), true) }, icon('x'))))),
    f.suggestions ? quickAdd(f, list, set) : null,
    h('div', { class: 'row gap-s' }, inp, h('button', { type: 'button', class: 'btn', onclick: add }, 'Add')));
}

/** Ready-made values (e.g. news feeds) as tap-to-add chips, grouped; tapping an added one removes it. */
const quickOpen = new Set();  // field names whose Quick add stays open while the form redraws
function quickAdd(f, list, set) {
  const suggestions = f.suggestions;
  const groups = new Map();
  for (const x of suggestions) groups.set(x.group || '', [...(groups.get(x.group || '') || []), x]);
  const picked = suggestions.filter((x) => list.includes(x.value)).length;
  const box = h('details', { class: 'item-more quick-add', open: quickOpen.has(f.name) || null },
    h('summary', null, `Quick add (${suggestions.length} ready-made${picked ? `, ${picked} in use` : ''})`),
    h('div', { class: 'stack-s' }, [...groups].map(([group, xs]) => h('div', { class: 'stack-xs' },
      group ? h('div', { class: 'tiny muted' }, group) : null,
      h('div', { class: 'chip-grid' }, xs.map((x) => {
        const on = list.includes(x.value);
        return h('button', { type: 'button', class: 'chip', 'aria-pressed': String(on), title: x.value,
          onclick: () => set(on ? list.filter((y) => y !== x.value) : [...list, x.value], true) }, on ? icon('check') : icon('plus'), x.name);
      }))))));
  box.addEventListener('toggle', () => { if (box.open) quickOpen.add(f.name); else quickOpen.delete(f.name); });
  return box;
}

function jsonArea(f, id, v, set) {
  const ta = h('textarea', { class: 'input mono json', id, rows: Math.min(14, JSON.stringify(v ?? null, null, 2).split('\n').length + 1), spellcheck: 'false' });
  ta.value = JSON.stringify(v ?? null, null, 2);
  const msg = h('span', { class: 'tiny danger', 'aria-live': 'polite' });
  ta.addEventListener('input', () => {
    let x;
    try { x = JSON.parse(ta.value); } catch { msg.textContent = 'Not valid JSON yet'; return; }
    const want = f.type === 'list' ? Array.isArray(x) : f.type === 'object' ? x && typeof x === 'object' && !Array.isArray(x) : true;
    if (!want) { msg.textContent = f.type === 'list' ? 'Needs to be a list: [ … ]' : 'Needs to be an object: { … }'; return; }
    msg.textContent = '';
    set(x);
  });
  return h('div', { class: 'stack-s' }, ta, msg);
}
