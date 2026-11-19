// Settings > Device: the panel's brightness, and Follow daylight (bongo/daylight.py). Off: one daytime
// brightness. On: Daytime, After dark and How gradual, with a graph of today's brightness that the Matrix
// works out itself (/api/daylight), so what is drawn here is what the panel does.
import { get } from './api.js';
import { h, toggle, clamp } from './ui.js';
import { S, onLeave } from './app.js';

/** How gradual, in a word: [up to, word], as bongo/daylight.py WORDS. */
export const WORDS = [[12, 'Quick'], [37, 'Fairly quick'], [62, 'Middle'], [87, 'Fairly gradual'], [100, 'Gradual']];
export const wordFor = (g) => WORDS.find(([top]) => g <= top)[1];
const MARKS = [[0, 'Quick'], [50, 'Middle'], [100, 'Gradual']];
const NS = 'http://www.w3.org/2000/svg';
const svgEl = (tag, attrs, text) => {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  if (text != null) el.textContent = text;
  return el;
};
const clock = (minute) => `${String(Math.floor(minute / 60) % 24).padStart(2, '0')}:${String(Math.floor(minute % 60)).padStart(2, '0')}`;

/** Today's brightness as a picture: the day's start to its end, the sleep hours shaded, a mark for now.
 *  ``d`` is /api/daylight's answer; ``width`` the room there is, in px (the text stays the same size).
 *  Along it a minute is a real minute: ``d.minutes`` is how long today is (23 or 25 hours when the clocks
 *  change) and ``d.hours`` where each hour by the clock falls, so an hour that is skipped or comes twice is
 *  drawn as the day really goes. */
export function drawCurve(svg, d, width) {
  const W = Math.max(240, Math.round(width || 440)), H = 176, L = 36, R = 10, T = 10, B = 24;
  const long = d.minutes || 1440, every = W < 400 ? 6 : 3;
  const X = (m) => L + (W - L - R) * m / long, Y = (v) => T + (H - T - B) * (1 - v / 100);
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.replaceChildren();
  for (const [from, to] of d.sleep || []) {
    svg.append(svgEl('rect', { x: X(from), y: T, width: X(to) - X(from), height: H - T - B, class: 'dl-sleep' }));
    if (d.sleep_level != null && X(to) - X(from) > 66 && d.sleep_level < 78) {
      svg.append(svgEl('text', { x: (X(from) + X(to)) / 2, y: T + 13, class: 'dl-text', 'text-anchor': 'middle' }, `Sleep ${d.sleep_level}%`));
    }
  }
  for (const v of [0, 25, 50, 75, 100]) {
    svg.append(svgEl('line', { x1: L, x2: W - R, y1: Y(v), y2: Y(v), class: 'dl-grid' }),
      svgEl('text', { x: L - 6, y: Y(v) + 4, class: 'dl-text', 'text-anchor': 'end' }, `${v}%`));
  }
  const hours = d.hours || Array.from({ length: 24 }, (_, k) => [k * 60, clock(k * 60)]);
  for (const [minute, says] of hours) {
    if (Number(says.slice(0, 2)) % every === 0 && says.endsWith(':00')) svg.append(svgEl('text', { x: X(minute), y: H - 7, class: 'dl-text', 'text-anchor': 'middle' }, says));
  }
  svg.append(svgEl('path', { class: 'dl-line', d: d.points.map(([m, v], i) => `${i ? 'L' : 'M'}${X(m).toFixed(1)},${Y(v).toFixed(1)}`).join(' ') }));
  if (d.now) {
    const x = X(clamp(d.now.minute, 0, long)), y = Y(d.now.level);
    // the words go beside the mark, on the side the line isn't climbing into (and never off the edge)
    const after = d.points.find(([m]) => m >= d.now.minute + 45), before = [...d.points].reverse().find(([m]) => m <= d.now.minute - 45);
    const rising = after && after[1] > d.now.level + 4, fell = before && before[1] > d.now.level + 4;
    const left = x > W - R - 70 || (rising && !fell && x > L + 70);
    svg.append(svgEl('line', { x1: x, x2: x, y1: T, y2: H - B, class: 'dl-now' }), svgEl('circle', { cx: x, cy: y, r: 4, class: 'dl-dot' }),
      svgEl('text', { x: x + (left ? -8 : 8), y: y > T + 30 ? y - 9 : y + 18, class: 'dl-text dl-now-text', 'text-anchor': left ? 'end' : 'start' },
        `Now ${Math.round(d.now.level)}%`));
  }
  const levels = d.points.map(([, v]) => v);
  svg.setAttribute('aria-label', `Today's brightness, from the start of the day to its end: from ${Math.round(Math.min(...levels))}% to ${Math.round(Math.max(...levels))}%`
    + (d.now ? `, ${Math.round(d.now.level)}% now, at ${d.now.clock || clock(d.now.minute)}` : ''));
}

/** The brightness part of the Device card. ``value()`` is what Save sends: {brightness, daylight}. */
export function brightnessBox(c) {
  const saved = { follow: false, after_dark: 35, gradual: 50, ...(c.daylight || {}) };
  const located = () => !!S.config?.location;
  let on = saved.follow === true;
  const day = h('input', { type: 'range', class: 'range', id: 'set-bright', min: 1, max: 100, value: c.panel?.brightness ?? 80 });
  const dark = h('input', { type: 'range', class: 'range', id: 'set-dark', min: 1, max: 100, value: Math.min(saved.after_dark, Number(day.value)) });
  const gradual = h('input', { type: 'range', class: 'range', id: 'set-gradual', min: 0, max: 100, value: saved.gradual, list: 'set-gradual-marks' });
  let wanted = Number(saved.after_dark) || 35;   // After dark as it was last set: Daytime dragged below it and back up gives it back
  const dayName = h('span'), dayVal = h('span', { class: 'mono strong' }), darkVal = h('span', { class: 'mono strong' }), word = h('span', { class: 'mono strong' });
  const capped = h('p', { class: 'muted tiny flush', hidden: true }, "After dark can't be brighter than daytime.");
  // the sleep brightness (Settings > Sleep) above After dark: the panel gets brighter at bedtime. Allowed; said quietly
  const brighter = h('p', { class: 'muted tiny flush', hidden: true });
  let sleepLevel = (() => { const s = S.config?.sleep; return s && s.enabled !== false && s.sources?.length && s.from ? s.brightness ?? null : null; })();
  let noPlace = false;   // the Matrix said it has no location to follow
  const need = h('p', { class: 'muted small flush', hidden: true }, 'Set a location first (',
    h('button', { type: 'button', class: 'link', onclick: () => document.getElementById('location')?.scrollIntoView({ behavior: 'smooth', block: 'center' }) }, 'Settings > Location'), ').');
  const svg = svgEl('svg', { class: 'dl-graph', role: 'img' });
  const sun = h('span'), box = h('div', { class: 'dl-box', hidden: true }, h('div', { class: 'row between tiny muted' }, h('span', null, 'Today'), sun), svg);
  const about = h('p', { class: 'muted tiny flush' });
  const more = h('div', { class: 'stack', hidden: true });
  // with no location yet it can still be switched on: the hint says what is missing, and it follows once there is one
  const sw = toggle(on, 'Follow daylight', (next) => { on = next; paint(); ask(); });
  let last = null, asked = 0, timer = 0;
  const show = () => { if (last && on && !box.hidden) drawCurve(svg, last, box.clientWidth - 22); };
  const ask = (wait = 0) => {   // the curve for the sliders as they stand, from the Matrix
    clearTimeout(timer);
    if (!on) return;
    timer = setTimeout(async () => {
      const mine = ++asked;
      let d = null, why = '';
      try { d = await get(`/api/daylight?day=${day.value}&dark=${dark.value}&gradual=${gradual.value}`); } catch (err) { why = err.message; }
      if (mine !== asked || !on) return;
      last = d && !d.needs_location && !d.trouble ? d : null;
      noPlace = !!d?.needs_location;
      if (d && 'sleep_level' in d) sleepLevel = d.sleep_level;
      box.hidden = !last;
      paint();
      about.textContent = last
        ? `From sunrise and sunset for ${d.place || 'your location'} (Settings > Location).`
          + (d.sleep_level != null ? ' Sleep hours keep their own brightness, and the panel rises gently over 30 minutes when they end.' : '')
          + ' A brightness set by hand on the Now playing page lasts until the next sunrise, sunset or sleep change.'
        : d?.needs_location ? 'Until then the panel keeps the daytime brightness.' : d?.trouble || why || "Couldn't draw today's brightness just now.";
      if (last) {
        sun.textContent = d.sunrise && d.sunset ? `Sunrise ${d.sunrise} · sunset ${d.sunset}`
          : d.sunrise ? `Sunrise ${d.sunrise}` : d.sunset ? `Sunset ${d.sunset}` : 'No sunrise or sunset today';
        show();
      }
    }, wait);
  };
  const said = () => gradual.setAttribute('aria-valuetext', wordFor(Number(gradual.value)));
  function paint() {
    dayName.textContent = on ? 'Daytime (midday)' : 'Daytime';
    dayVal.textContent = `${day.value}%`;
    darkVal.textContent = `${dark.value}%`;
    word.textContent = wordFor(Number(gradual.value));
    said();
    more.hidden = !on;
    need.hidden = !(on && (noPlace || !located()));   // only when it's on (or being switched on): off, nothing is missing
    if (on && !located() && !last) about.textContent = 'Until then the panel keeps the daytime brightness.';
    brighter.hidden = !(on && sleepLevel != null && sleepLevel > Number(dark.value));
    brighter.textContent = brighter.hidden ? '' : `Your sleep brightness (${sleepLevel}%) is brighter than After dark.`;
  }
  day.addEventListener('input', () => { dark.value = Math.min(wanted, Number(day.value)); capped.hidden = true; paint(); ask(120); });
  dark.addEventListener('input', () => {
    capped.hidden = Number(dark.value) <= Number(day.value);
    if (!capped.hidden) dark.value = day.value;   // never above Daytime: it stops there
    wanted = Number(dark.value); paint(); ask(120);
  });
  let dragging = false;   // dragging near a named point lands on it; the arrow keys reach everything between
  gradual.addEventListener('pointerdown', () => { dragging = true; });
  for (const done of ['pointerup', 'pointercancel', 'change', 'keydown']) gradual.addEventListener(done, () => { dragging = false; });
  gradual.addEventListener('input', () => {
    const near = dragging && MARKS.find(([m]) => Math.abs(m - Number(gradual.value)) <= 3);
    if (near) gradual.value = near[0];
    paint(); ask(120);
  });
  const marks = h('div', { class: 'range-marks', 'aria-hidden': 'true' }, MARKS.map(([x, label]) => h('button', {
    type: 'button', class: `range-mark${x === 0 ? ' start' : x === 100 ? ' end' : ''}`, tabindex: -1, style: `--at:${x / 100}`,
    onclick: () => { gradual.value = x; paint(); ask(); } }, label)));
  more.append(
    h('label', { class: 'field-col', for: 'set-dark' }, h('span', { class: 'row between' }, h('span', null, 'After dark'), darkVal), dark, capped),
    h('label', { class: 'field-col', for: 'set-gradual' }, h('span', { class: 'row between' }, h('span', null, 'How gradual'), word),
      h('div', { class: 'range-with-marks' }, gradual, h('datalist', { id: 'set-gradual-marks' }, MARKS.map(([x, label]) => h('option', { value: x, label }))), marks)),
    brighter, box, about);
  if (window.ResizeObserver) { const seen = new ResizeObserver(show); seen.observe(box); onLeave(() => seen.disconnect()); }
  const tick = setInterval(() => { if (on && document.body.contains(more)) ask(); }, 60000);   // the mark for now moves on
  onLeave(() => { clearInterval(tick); clearTimeout(timer); });
  paint(); ask();
  const el = h('div', { class: 'stack', role: 'group', 'aria-label': 'Panel brightness' },
    h('div', { class: 'row between wrap' }, h('span', { class: 'strong' }, 'Panel brightness'), sw),
    need,
    h('label', { class: 'field-col', for: 'set-bright' }, h('span', { class: 'row between' }, dayName, dayVal), day),
    more);
  return { el, value: () => ({ brightness: Number(day.value),
    daylight: { follow: on, after_dark: Number(dark.value), gradual: Number(gradual.value) } }) };
}
