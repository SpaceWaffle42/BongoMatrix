// Times of day for a playlist entry (Timing > When it plays). The same rules as the player's
// Entry.plays_at (bongo/player.py): both are checked against tests/fixtures/times.json.
// A time is {from: "08:00", to: "10:00"}; none means all day; from after to runs past midnight.

export const MOST_TIMES = 4;
const DAY = 1440;
const pad = (n) => String(n).padStart(2, '0');
// (as strict as the Matrix is: bongo/config.py, _CLOCK)
export const mins = (t) => { const m = /^([01]\d|2[0-3]):([0-5]\d)$/.exec(t || ''); return m ? Number(m[1]) * 60 + Number(m[2]) : NaN; };
export const hhmm = (m) => `${pad(Math.floor((((m % DAY) + DAY) % DAY) / 60))}:${pad((((m % DAY) + DAY) % DAY) % 60)}`;
/** A time the Matrix will take: both ends set, and not the same. */
export const valid = (t) => Number.isFinite(mins(t?.from)) && Number.isFinite(mins(t?.to)) && mins(t.from) !== mins(t.to);

function inside(t, m) {
  const a = mins(t.from), b = mins(t.to);
  return a < b ? m >= a && m < b : m >= a || m < b;
}
/** Does an entry with these times play at this clock time ("13:25")? */
export function playsAt(times, clock) {
  const list = (times || []).filter(valid);
  return !list.length || list.some((t) => inside(t, mins(clock)));
}
/** When it next starts, for an entry that isn't playing now: "20:00". */
export function nextStart(times, clock) {
  const now = mins(clock);
  const starts = (times || []).filter(valid).map((t) => mins(t.from));
  if (!starts.length) return '';
  return hhmm(Math.min(...starts.map((s) => (s - now + DAY) % DAY || DAY)) + now);
}

/** Minute by minute: which of the day's 1440 minutes these times cover. */
function cover(times) {
  const on = new Uint8Array(DAY);
  for (const t of (times || []).filter(valid)) for (let m = mins(t.from); m !== mins(t.to); m = (m + 1) % DAY) on[m] = 1;
  return on;
}
/** The stretches of the day that are on, joined up: [[from, to], ...] in minutes, to up to 1440. One that
 *  runs past midnight comes last, with to past 1440. */
function stretches(on) {
  const out = [];
  for (let m = 0; m < DAY; m += 1) {
    if (on[m] && (m === 0 || !on[m - 1])) out.push([m, m]);
    if (on[m]) out[out.length - 1][1] = m + 1;
  }
  if (out.length > 1 && out[0][0] === 0 && out[out.length - 1][1] === DAY) {   // 22:00-24:00 and 00:00-02:00 are one night
    const first = out.shift();
    out[out.length - 1][1] = DAY + first[1];
  }
  return out;
}
const span = ([a, b]) => `${hhmm(a)}–${hhmm(b)}`;
const list = (items, last = 'and') => (items.length < 2 ? items.join('') : `${items.slice(0, -1).join(', ')} ${last} ${items[items.length - 1]}`);

/** The times as they play, overlaps joined: [{from, to, overnight}] in clock order. */
export function joined(times) {
  return stretches(cover(times)).map(([a, b]) => ({ from: hhmm(a), to: hhmm(b), a, b, overnight: b > DAY }));
}
/** "Plays 08:00–10:00 and 20:00–22:00. It doesn't play the rest of the day." */
export function sentence(times) {
  const on = cover(times);
  if (on.every((x) => x)) return 'Plays all day.';
  const parts = stretches(on).map((s) => span(s) + (s[1] > DAY ? ' (overnight)' : ''));
  return parts.length ? `Plays ${list(parts)}. It doesn't play the rest of the day.` : '';
}
/** The same for a screen reader: "08:00 to 10:00 and 20:00 to 22:00". */
export const spoken = (times) => list((times || []).filter(valid).map((t) => `${t.from} to ${t.to}`));
/** What a playlist card's tag says: the first time, and how many more. */
export const short = (times) => { const l = (times || []).filter(valid); return l.length ? `${l[0].from}–${l[0].to}${l.length > 1 ? ` +${l.length - 1}` : ''}` : 'All day'; };

/** The sleep hours as a time, if they're on and set (Settings > Sleep). */
export function sleepTime(config) {
  const s = config?.sleep;
  const t = s && s.enabled !== false ? { from: s.from, to: s.to } : null;
  return t && valid(t) ? t : null;
}
/** Do these times run into the sleep hours? */
export function meetsSleep(times, sleep) {
  if (!sleep) return false;
  const a = cover(times), b = cover([sleep]);
  return (times || []).some(valid) && a.some((x, m) => x && b[m]);
}
/** The parts of the waking day when nothing in the playlist plays: ["14:00–17:00"]. ``entries``: the times of
 *  each screen in rotation ([] for one that plays all day). */
export function gaps(entries, sleep) {
  if (!entries.length || entries.some((t) => !(t || []).some(valid))) return [];
  const off = new Uint8Array(DAY).fill(1);
  for (const t of entries) cover(t).forEach((x, m) => { if (x) off[m] = 0; });
  if (sleep) cover([sleep]).forEach((x, m) => { if (x) off[m] = 0; });
  return stretches(off).map(span);
}
/** "Nothing is set to play 14:00–17:00. The panel will be blank then." (or '' with no gap). */
export function gapWarning(entries, sleep) {
  const g = gaps(entries, sleep);
  return g.length ? `Nothing is set to play ${list(g, 'or')}. The panel will be blank then.` : '';
}

/** A new time, filled in ready: the next whole hour for 2 hours, or 2 hours after the last one ends. It never
 *  runs past midnight by itself; with no room left that late, it's the first 2 free hours after the sleep hours end (else from 06:00). */
export function suggest(times, clock, sleep) {
  const have = (times || []).filter(valid), on = cover(have);
  const free = (from) => from + 120 <= DAY && !on.slice(from, from + 120).some((x) => x);
  const lastEnd = have.map((t) => mins(t.to) || DAY).pop();
  const wake = sleep && valid(sleep) ? Math.ceil(mins(sleep.to) / 60) % 24 : 6;
  let from = lastEnd === undefined ? (Math.ceil(((mins(clock) || 0) + 1) / 60) * 60) % DAY : lastEnd + 120;
  if (lastEnd !== undefined && !free(from)) from = [...Array(24).keys()].map((k) => ((k + wake) % 24) * 60).find(free) ?? DAY - 120;   // from when it wakes, on round the day
  return { from: hhmm(from), to: hhmm(Math.min(from + 120, DAY)) };
}
/** From moved: To moves with it, keeping the length. */
export function moved(t, from) {
  const length = valid(t) ? (mins(t.to) - mins(t.from) + DAY) % DAY : 120;
  return { from, to: Number.isFinite(mins(from)) ? hhmm(mins(from) + length) : t.to };
}
