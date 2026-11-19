// News feeds with word filters: under the list of feeds, each feed's "Only stories that mention" and
// "Never show stories that mention" boxes (Module.word_filters; the matching is bongo/modules/rss/filters.py).
// A feed with no words in either box is saved as its plain address, as it always was; one with filters
// as { url, only, never }. The counts come from the feeds the Matrix has already fetched: one request for
// the whole list, half a second after the last key, and nothing is fetched because of it.
import { post } from './api.js';
import { h, icon } from './ui.js';

const MAX = 500;            // characters in one box (the Matrix refuses more)
const folds = new Map();    // feed address -> its filters open or shut, as last left (kept while the form redraws)
const WHY = {
  waiting: "This feed hasn't loaded on the Matrix yet, so there are no stories to count.",
  failed: "The Matrix couldn't load this feed just now, so there are no stories to count.",
  offline: "The Matrix is in offline mode and hasn't loaded this feed, so there are no stories to count.",
  demo: 'This is the demo, with no live feeds, so there are no stories to count.',
};

const unpack = (x) => (typeof x === 'string' ? { url: x, only: '', never: '' }
  : { url: String(x?.url ?? ''), only: String(x?.only ?? ''), never: String(x?.never ?? '') });
const pack = (e) => {
  const only = e.only.trim() ? e.only : '', never = e.never.trim() ? e.never : '';
  return only || never ? { url: e.url, ...(only ? { only } : {}), ...(never ? { never } : {}) } : e.url;
};
/** "a,, b ,A" -> "a, b": what the box shows once it's left (stray commas and spaces, and a word typed twice, go). */
const tidy = (text) => {
  const seen = new Set();
  return text.split(',').map((w) => w.trim().replace(/\s+/g, ' ')).filter((w) => w && !seen.has(w.toLowerCase()) && seen.add(w.toLowerCase())).join(', ');
};
const host = (url) => { try { return new URL(url).hostname.replace(/^www\./, '') || url; } catch { return url; } };

/** What the line under a feed's boxes says about its stories (``r``: that feed's part of the Matrix's answer). */
export function counted(r) {
  if (!r) return '';
  if (r.error) return `Couldn't count this feed's stories: ${r.error}`;
  if (!r.loaded) return WHY[r.why] || WHY.waiting;
  const n = r.total, k = r.shown;
  if (!n) return 'This feed has no stories right now.';
  if (n === 1) return `The 1 story in this feed right now ${k ? 'gets' : "doesn't get"} through.`;
  if (k === n) return `All ${n} stories in this feed right now get through.`;
  if (!k) return `None of the ${n} stories in this feed right now get through.`;
  return `${k} of the ${n} stories in this feed right now get${k === 1 ? 's' : ''} through.`;
}

/** The control for a list of feeds: ``chips`` draws the list itself (and Quick add), this adds each feed's filters. */
export function feedFilters(f, id, v, set, chips) {
  const feeds = (Array.isArray(v) ? v : []).map(unpack).filter((e) => e.url);
  const named = new Map((f.suggestions || []).map((x) => [x.value, x.name]));
  const save = () => set(feeds.map(pack), false);
  const list = chips(f, id, feeds.map((e) => e.url), (urls) => {   // a feed added or removed: the others keep their words
    const had = new Map(feeds.map((e) => [e.url, e]));
    set(urls.map((u) => pack(had.get(u) || { url: u, only: '', never: '' })), true);
  });
  const nothing = h('div', { class: 'note trouble', role: 'status', hidden: true }, icon('warn'),
    h('span', null, h('strong', null, 'No stories get through these filters.'), ' The screen says FILTERS HIDE ALL, then lets the next one on.'));
  const root = h('div', { class: 'stack-s feed-filters' }, list);
  if (!feeds.length) return root;

  let seq = 0, timer = 0, tries = 0;
  const cards = feeds.map((e, k) => {
    const fid = `${id}-filter-${k}`;
    const filtered = () => !!(e.only.trim() || e.never.trim());
    const name = h('h3', { class: 'h2', title: e.url }, named.get(e.url) || host(e.url));
    const state = h('span', { class: 'muted small' });
    const says = h('span', { id: `${fid}-says`, role: 'status' });
    const out = h('ul', { class: 'left-out small' });
    const outHead = h('summary');
    const left = h('details', { class: 'item-more', hidden: true }, outHead, out);
    let got = null;
    const box = (key, label, hint, eg) => {
      const inp = h('input', { class: 'input', id: `${fid}-${key}`, value: e[key], maxlength: MAX, placeholder: eg, autocomplete: 'off',
        autocapitalize: 'off', spellcheck: 'false', 'aria-describedby': says.id });
      inp.addEventListener('input', () => { e[key] = inp.value; save(); paint(); soon(); });
      inp.addEventListener('change', () => { const clean = tidy(inp.value); if (clean !== inp.value) { inp.value = e[key] = clean; save(); } });
      return h('label', { class: 'field-col', for: inp.id }, label, h('span', { class: 'muted tiny' }, hint), inp);
    };
    const paint = () => {   // the line beside the feed's name: what its filters do, at a glance
      state.textContent = !filtered() ? 'no filters' : got?.loaded && got.total ? `${got.shown} of ${got.total} get through` : 'filtered';
    };
    const show = (r) => {
      got = r;
      if (r?.loaded && r.name && !named.has(e.url)) name.textContent = r.name;   // the feed's own title, once known
      says.textContent = counted(r);
      const gone = r?.loaded ? r.total - r.shown : 0, more = gone - (r?.left_out?.length || 0);
      left.hidden = !gone;
      outHead.textContent = `Show what's left out (${gone})`;
      out.replaceChildren(...(r?.left_out || []).map((s) => h('li', null, s.title, s.never ? [' ', h('span', { class: 'tag' }, `never: ${s.never}`)] : null)),
        ...(more > 0 ? [h('li', { class: 'muted' }, `and ${more} more`)] : []));
      paint();
    };
    const open = folds.has(e.url) ? folds.get(e.url) : filtered() || feeds.length === 1;
    const card = h('details', { class: 'item-card item-more feed-filter', open: open || null },
      h('summary', null, name, state),
      h('div', { class: 'stack-s' },
        box('only', 'Only stories that mention', '(any of these words or phrases; empty: every story)', 'Cheltenham, Gloucestershire'),
        box('never', 'Never show stories that mention', '(any of these words or phrases)', "Love Island, I'm a Celebrity"),
        h('p', { class: 'muted tiny flush' }, 'Looks at the headline and summary, whole words, any case. Never show wins over Only. ', says),
        left));
    card.addEventListener('toggle', () => folds.set(e.url, card.open));
    paint();
    return { card, show };
  });

  /** Ask the Matrix what gets through (all the feeds in one go). A feed still loading is asked about again a few times. */
  async function ask() {
    if (!root.isConnected) return;   // the form was redrawn, or the page left
    const mine = ++seq;
    let res;
    try { res = await post(f.word_filters, { feeds: feeds.map(pack) }); }
    catch (err) {
      if (mine === seq && root.isConnected && err.status !== 401) { cards.forEach((c) => c.show({ error: err.message })); nothing.hidden = true; }
      return;
    }
    if (mine !== seq || !root.isConnected) return;   // (an older answer, after a newer question)
    cards.forEach((c, k) => c.show(res.feeds?.[k]));
    nothing.hidden = !res.nothing;
    if ((res.feeds || []).some((r) => !r.loaded && r.why === 'waiting') && tries++ < 10) timer = setTimeout(ask, 3000);
  }
  const soon = () => { clearTimeout(timer); tries = 0; timer = setTimeout(ask, 500); };
  timer = setTimeout(ask, 0);
  root.append(h('div', { class: 'tiny muted' }, 'Filters, feed by feed'), nothing, ...cards.map((c) => c.card));
  return root;
}
