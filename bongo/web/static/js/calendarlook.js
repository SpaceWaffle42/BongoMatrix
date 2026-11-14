// Settings > Calendars: each linked calendar's colour and icons, and the Coming up card: everything the
// Calendar screens will show, in their colours (bongo/modules/calendar/preview.py).
import { api, get } from './api.js';
import { h, toast, toggle } from './ui.js';
import { S, onLeave, saveConfig } from './app.js';
import { icons, iconPicker } from './iconpick.js';

export { icons };

const pic = (set, name, size) => (name && set[name] ? h('img', { class: 'px-icon', src: set[name].png, alt: set[name].label, width: size, height: size }) : null);
const choices = (set) => Object.keys(set).map((n) => [n, set[n].label, set[n].events ? 'For events' : 'Holidays']);
export const NAMES = { '#5AC8FA': 'Blue', '#FF6FA8': 'Pink', '#4ADE80': 'Green', '#FFB547': 'Amber', '#B98CFF': 'Purple', '#FF7A59': 'Orange', '#ECEEF1': 'White' };

/** A row of colour swatches; ``key`` keeps keyboard focus on the one picked when the row is drawn again. */
export function swatches(colours, current, label, key, onpick) {
  return h('div', { class: 'swatches', role: 'group', 'aria-label': label }, colours.map((c) => h('button', {
    type: 'button', class: 'swatch-btn', style: `background:${c}`, 'aria-label': NAMES[c] || c, title: NAMES[c] || c,
    'aria-pressed': String(c.toUpperCase() === String(current).toUpperCase()), 'data-key': `${key}:${c}`, onclick: () => onpick(c) })));
}

/** One linked calendar's colour and icons, in a closed section under its row. ``drafts`` keeps a half-typed
 *  word across redraws; ``show`` takes the calendar list a change sends back. */
export function lookEditor(c, set, colours, drafts, show) {
  const look = c.icons || { mode: 'guess', one: null, rules: [] };
  const patch = (data, done) => api('PATCH', `/api/calendars/${c.id}`, data).then((s) => { show(s); if (done) toast(done, 'success'); })
    .catch((e) => toast(e.message, 'error', 8000));
  const mode = h('select', { class: 'input', 'aria-label': `Icons for ${c.name}`, 'data-key': `${c.id}:mode`,
    onchange: () => patch({ icons: { ...look, mode: mode.value, one: mode.value === 'one' ? (look.one || 'star') : look.one } }) },
  h('option', { value: 'guess' }, 'Guess from each title'), h('option', { value: 'one' }, 'The same icon for every event'), h('option', { value: 'none' }, 'No icons'));
  mode.value = look.mode;
  const picker = look.mode === 'one' ? h('details', { class: 'item-more', 'data-key': `${c.id}:pick` },
    h('summary', null, h('span', { class: 'row gap-s' }, 'Icon:', pic(set, look.one, 16), set[look.one]?.label || look.one || '', h('span', { class: 'tiny muted' }, '· change'))),
    h('div', { class: 'icon-pick', role: 'group', 'aria-label': `Icon for every ${c.name} event` },
      Object.keys(set).map((n) => h('button', { type: 'button', title: set[n].label, 'aria-label': set[n].label, 'aria-pressed': String(n === look.one),
        'data-key': `${c.id}:icon:${n}`, onclick: () => patch({ icons: { ...look, one: n } }) }, pic(set, n, 24))))) : null;
  const word = h('input', { class: 'input', placeholder: 'a word, e.g. dentist', 'aria-label': 'Word in the title', maxlength: 30,
    value: drafts[c.id] || '', 'data-key': `${c.id}:word`, oninput: () => { drafts[c.id] = word.value; } });
  const which = iconPicker({ id: `${c.id}-which`, value: 'heart', options: choices(set), label: 'Icon for that word', onpick: () => {}, key: `${c.id}:which` });
  const said = h('span', { class: 'tiny muted', 'aria-live': 'polite' });
  const add = (ev) => {
    ev.preventDefault();
    const w = word.value.trim().toLowerCase();
    if (!w) { said.textContent = 'Type a word first.'; word.focus(); return; }
    if (look.rules.some(([x]) => x === w)) { said.textContent = `“${w}” already has an icon.`; return; }
    drafts[c.id] = '';
    patch({ icons: { ...look, rules: [...look.rules, [w, which.value()]] } }, `Titles with “${w}” now show ${set[which.value()]?.label || which.value()}`);
  };
  const rules = h('details', { class: 'item-more', 'data-key': `${c.id}:rules`, open: look.rules.length > 0 },
    h('summary', null, `Icons by word${look.rules.length ? ` (${look.rules.length})` : ''}`),
    h('p', { class: 'tiny muted flush' }, 'A word in the title picks the icon first, whatever is chosen above. "bin" also catches "bins".'),
    h('ul', { class: 'stack gap-xs', style: 'list-style:none;padding:0;margin:6px 0' }, look.rules.map(([w, n], k) => h('li', { class: 'row gap-s wrap small' },
      h('span', null, `Titles with “${w}”`), h('span', { 'aria-hidden': 'true' }, '→'), pic(set, n, 16), h('span', null, set[n]?.label || n),
      h('button', { type: 'button', class: 'btn ghost small-btn', 'aria-label': `Remove the icon for ${w}`, 'data-key': `${c.id}:rm:${w}`,
        onclick: () => patch({ icons: { ...look, rules: look.rules.filter((_, j) => j !== k) } }, `Removed the icon for “${w}”`)
          .then(() => document.querySelector(`[data-key="${CSS.escape(`${c.id}:word`)}"]`)?.focus()) }, 'Remove')))),
    h('form', { class: 'row gap-s wrap', onsubmit: add }, word, which, h('button', { type: 'submit', class: 'btn small-btn' }, 'Add'), said));
  return h('details', { class: 'item-more', 'data-key': `${c.id}:look` },
    h('summary', null, 'Colour and icons'),
    h('div', { class: 'stack gap-s' },
      h('div', { class: 'field-col' }, h('span', null, 'Colour'), swatches(colours, c.colour, `Colour for ${c.name}`, `${c.id}:colour`, (col) => patch({ colour: col }))),
      h('label', { class: 'field-col' }, 'Icons', mode), picker, rules));
}

/** Before a list is drawn again: what's open and what has focus, by data-key; after: put them back. */
export function keepState(root) {
  const open = [...root.querySelectorAll('details[open][data-key]')].map((d) => d.dataset.key);
  const focus = document.activeElement?.closest?.('[data-key]')?.dataset.key;
  return () => {
    for (const k of open) { const d = root.querySelector(`details[data-key="${CSS.escape(k)}"]`); if (d) d.open = true; }
    if (focus) root.querySelector(`[data-key="${CSS.escape(focus)}"]`)?.focus();
  };
}

const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const dateOf = (s) => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };
const dayName = (s) => dateOf(s).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
const PLAIN = '#ECEEF1';

/** Coming up: a month grid and a list of everything the Calendar screens will show over the next 2 or 8
 *  weeks, in the colours the panel uses. Colour coding and the colours of your dates and holidays save here. */
export function comingUpCard(colours) {
  const shown = {};   // source id -> false when hidden here (this page only)
  let data = null, set = {}, weeks = 8, month = null, picked = null;
  const sub = h('p', { class: 'muted small flush' });
  const code = toggle(false, 'Colour code', async (on) => {
    const look = { ...(S.config.calendar_colours || {}), code: on };
    if (!await saveConfig({ calendar_colours: look }, on ? 'Colour coding on' : 'Colour coding off')) code.setAttribute('aria-pressed', String(!on));
    load();
  });
  const legend = h('div', { class: 'cal-legend', role: 'group', 'aria-label': 'Show in this preview' });
  const colourRows = h('div', { class: 'stack gap-s' });
  const colourBox = h('details', { class: 'item-more' }, h('summary', null, 'Change colours'), colourRows);
  const monthName = h('strong', { 'aria-live': 'polite' });
  const prev = h('button', { type: 'button', class: 'btn small-btn', 'aria-label': 'Previous month', onclick: () => { month.setMonth(month.getMonth() - 1); grid(); } }, '‹');
  const next = h('button', { type: 'button', class: 'btn small-btn', 'aria-label': 'Next month', onclick: () => { month.setMonth(month.getMonth() + 1); grid(); } }, '›');
  const gridBox = h('div', { class: 'cal-grid' });
  const count = h('strong');
  const agenda = h('div', { class: 'cal-agenda', role: 'region', tabindex: 0, 'aria-label': 'Upcoming events' });
  const seg = h('div', { class: 'seg cal-seg', role: 'group', 'aria-label': 'How far ahead' }, [2, 8].map((n) => h('button', { type: 'button', 'aria-pressed': String(n === weeks),
    onclick: (ev) => { weeks = n; picked = null; seg.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b === ev.currentTarget))); load(); } }, `${n} weeks`)));
  const side = h('div', { class: 'stack gap-xs min0' }, h('div', { class: 'row between' }, count, seg), agenda);
  const body = h('div', { class: 'cal-wrap' },
    h('div', { class: 'min0' }, h('div', { class: 'row between' }, prev, monthName, next), gridBox, h('p', { class: 'tiny muted flush' }, 'Tap a day to see it in the list.')), side);
  const card = h('section', { class: 'card stack', 'aria-label': 'Coming up', id: 'coming-up' },
    h('div', { class: 'row between wrap gap-s' }, h('div', null, h('h2', { class: 'h2' }, 'Coming up'), sub), code),
    h('p', { class: 'tiny muted flush' }, 'Colour code: each Calendar screen shows a date in the colour of where it comes from: its linked calendar, your dates, or holidays. Off: each screen uses its own colours.'),
    legend, colourBox, body);

  const src = (id) => data.sources.find((s) => s.id === id);
  const colourOf = (i) => (data.code ? i.colour : PLAIN);
  const visible = () => data.items.filter((i) => shown[i.source] !== false && (src(i.source)?.on !== false));
  const today = () => data.today;
  const end = () => { const d = dateOf(data.today); d.setDate(d.getDate() + data.days); return iso(d); };

  function paint() {
    code.setAttribute('aria-pressed', String(!!data.code));
    sub.textContent = data.screens ? `The next ${weeks} weeks of everything your Calendar screens show: linked calendars, the dates you added on Calendar screens, and the holidays ticked there.`
      : 'No Calendar screen is in the playlist yet: add one, and its dates and holidays show here with your linked calendars.';
    legend.replaceChildren(h('span', { class: 'tiny muted' }, 'Show in this preview:'), ...data.sources.map((s) => {
      const off = s.on === false;
      return h('button', { type: 'button', class: 'chip', 'aria-pressed': String(shown[s.id] !== false && !off), disabled: off, 'data-src': s.id,
        onclick: () => { shown[s.id] = shown[s.id] === false; paint(); } }, h('span', { class: 'cal-dot', style: `background:${data.code ? s.colour : PLAIN}` }), off ? `${s.name} (off)` : s.name);
    }));
    const again = keepState(colourRows);
    colourRows.replaceChildren(...data.sources.map((s) => h('div', { class: 'row gap-s wrap' }, h('span', { class: 'small', style: 'min-width:110px' }, s.name),
      swatches(colours, s.colour, `Colour for ${s.name}`, `cu:${s.id}`, (c) => recolour(s, c)))));
    again();
    grid(); list();
  }
  async function recolour(s, c) {
    if (s.linked) {   // the Calendars card draws it again, and then this follows (calendars-changed)
      try { await api('PATCH', `/api/calendars/${s.id}`, { colour: c }); } catch (e) { toast(e.message, 'error', 8000); }
      window.dispatchEvent(new Event('calendar-edited'));
      return;
    }
    await saveConfig({ calendar_colours: { ...(S.config.calendar_colours || {}), [s.id]: c } }, `${s.name} colour`);
    load();
  }
  function grid() {
    const focused = document.activeElement?.dataset?.date;
    monthName.textContent = month.toLocaleDateString('en-GB', { month: 'long', year: 'numeric' });
    const first = dateOf(data.today); first.setDate(1);
    const last = dateOf(end()); last.setDate(1);
    prev.disabled = month <= first; next.disabled = month >= last;
    const lead = (month.getDay() + 6) % 7, days = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate();
    const vis = visible();
    const cells = [['Mo', 'Monday'], ['Tu', 'Tuesday'], ['We', 'Wednesday'], ['Th', 'Thursday'], ['Fr', 'Friday'], ['Sa', 'Saturday'], ['Su', 'Sunday']].map(([n, full]) => h('div', { class: 'cal-dow', title: full }, n));
    for (let k = 0; k < lead; k++) cells.push(h('div', { class: 'cal-day out', 'aria-hidden': 'true' }));
    for (let n = 1; n <= days; n++) {
      const day = iso(new Date(month.getFullYear(), month.getMonth(), n));
      const items = vis.filter((i) => i.date === day);
      const inRange = day >= today() && day < end();
      cells.push(h('button', { type: 'button', class: `cal-day${day < today() ? ' past' : ''}${day === today() ? ' today' : ''}`, disabled: !inRange, 'data-date': day,
        'aria-pressed': String(picked === day), 'aria-label': `${dateOf(day).toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' })}: ${items.length ? items.map((i) => i.title).join(', ') : 'nothing on'}`,
        onclick: () => { picked = day; grid(); list(); reveal(day); } },
      h('span', { class: 'n' }, String(n)), h('span', { class: 'cal-chips', 'aria-hidden': 'true' }, items.slice(0, 6).map((i) => h('span', { class: 'cal-dot', style: `background:${colourOf(i)}` })))));
    }
    gridBox.replaceChildren(...cells);
    if (focused) gridBox.querySelector(`[data-date="${focused}"]`)?.focus();
  }
  function list() {
    const vis = visible();
    count.textContent = `${vis.length} event${vis.length === 1 ? '' : 's'}`;
    const out = []; let last = null;
    const empty = picked && !vis.some((i) => i.date === picked);
    const nothing = () => [h('div', { class: 'cal-head', 'data-day': picked }, dayName(picked)), h('p', { class: 'cal-nothing' }, 'Nothing on this day.')];
    for (const i of vis) {
      if (empty && picked < i.date && (!last || last < picked)) out.push(...nothing());
      if (i.date !== last) out.push(h('div', { class: `cal-head${i.date === today() ? ' today' : ''}`, 'data-day': i.date }, i.date === today() ? 'Today' : dayName(i.date)));
      last = i.date;
      out.push(h('div', { class: 'cal-ev' }, h('span', { class: 'bar', style: `background:${colourOf(i)}` }),
        pic(set, i.icon, 32) || h('span', { class: 'noicon', 'aria-hidden': 'true' }),
        h('span', { class: 't' }, i.title), h('span', { class: 'when mono' }, i.time || 'All day')));
    }
    if (empty && !out.some((el) => el.dataset?.day === picked)) out.push(...nothing());
    if (!out.length) out.push(h('p', { class: 'cal-nothing' }, 'Nothing coming up from what is shown.'));
    agenda.replaceChildren(...out);
  }
  function reveal(day) {
    const head = agenda.querySelector(`[data-day="${day}"]`);
    if (!head) return;
    agenda.scrollTop = 0;   // a stuck header reports where it's stuck: measure from the top
    agenda.scrollTop = head.offsetTop - agenda.offsetTop;
    const r = head.getBoundingClientRect(), item = head.nextElementSibling?.getBoundingClientRect();
    if (r.top < 0 || (item ? item.bottom : r.bottom) > innerHeight) window.scrollBy({ top: r.top - 80 });
  }
  let pending = null;
  function soon() { clearTimeout(pending); pending = setTimeout(load, 300); }   // several changes at once: one fetch
  async function load() {
    try {
      [data, set] = await Promise.all([get(`/api/calendars/upcoming?days=${weeks * 7}`), icons()]);
    } catch (e) {
      agenda.replaceChildren(h('p', { class: 'cal-nothing' }, "Couldn't load what's coming up."));
      return;
    }
    const first = dateOf(data.today); first.setDate(1);
    const last = dateOf(end()); last.setDate(1);
    if (!month || month < first) month = first;          // keep the grid inside the range shown
    else if (month > last) month = last;
    paint();
  }
  window.addEventListener('calendars-changed', soon);
  onLeave(() => { window.removeEventListener('calendars-changed', soon); clearTimeout(pending); });
  load();
  return card;
}
