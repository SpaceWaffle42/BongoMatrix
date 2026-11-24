// The SD card and how the Matrix last started (power-loss protection, claude/power-loss-design.md): shared by
// Settings > Device, Logs and Now. The words never say "power cut": a Matrix switched off at the wall every
// night is working as it should.
import { h } from './ui.js';

const LOW = 500 * 1024 * 1024;   // under this free, Settings > Device and Now say so (uploads pause at 200 MB)
const size = (n) => (n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(n / 1e6))} MB`);

/** The nearly-full warning, or null while there's room. */
export function nearlyFull(st) {
  if (!st || !(st.free < LOW)) return null;
  return `The SD card is nearly full (${size(st.free)} left). New uploads pause at 200 MB: delete some in Media.`;
}

const HOW = { first: 'its first start', clean: 'a normal restart', crash: 'the display stopped unexpectedly',
  power: 'being switched off without shutting down' };

/** "07:58, after being switched off without shutting down" (with the day when it wasn't today). */
export function startWords(last) {
  if (!last) return '';
  const at = new Date(last.at);
  const today = at.toDateString() === new Date().toDateString();
  const when = Number.isNaN(at.getTime()) ? '' : `${today ? '' : `${at.toLocaleDateString([], { day: 'numeric', month: 'short' })} `}${at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}, `;
  return `${when}after ${HOW[last.how] || 'a restart'}`;
}

/** Settings > Device: the card's space, the last start and the 200 MB rule (or the warning, when it applies). */
export function storageBox(st, start) {
  if (!st) return null;
  const used = st.total ? Math.min(100, Math.round((100 * (st.total - st.free)) / st.total)) : 0;
  const warn = nearlyFull(st);
  return h('div', { class: 'stack-s storage-box' },
    h('div', { class: 'row between wrap' }, h('span', { class: 'strong' }, 'SD card'),
      h('span', { class: 'mono small soft' }, `${size(st.free)} free of ${size(st.total)}`)),
    h('div', { class: 'progress-track', role: 'img', 'aria-label': `${used}% of the SD card used` },
      h('div', { class: 'progress-fill', style: `width:${used}%` })),
    start?.last ? h('p', { class: 'small soft flush' }, `Last start: ${startWords(start.last)}`) : null,
    warn ? h('p', { class: 'warn small flush', role: 'status' }, warn)
      : h('p', { class: 'muted tiny flush' }, 'The Matrix keeps 200 MB free for settings, Wi-Fi and updates.'));
}

/** Logs: how this start came about, and the last 7 days. */
export function lastStartBox(start) {
  if (!start?.last) return null;
  const off = start.last.how === 'power', crash = start.last.how === 'crash';
  const head = off ? 'Switched off without shutting down' : crash ? 'The display stopped unexpectedly' : 'Started normally';
  const more = off ? "The power went off, or it was unplugged, rather than turned off from Settings. That's normal if it's switched off at the wall."
    : crash ? 'It started again by itself. If this keeps happening, download the logs and send them to whoever looks after your Matrix.' : '';
  return h('section', { class: 'card stack-s', 'aria-label': 'Last start' },
    h('span', { class: 'eyebrow' }, 'Last start'),
    h('div', { class: 'row gap-s' }, h('span', { class: `dot ${crash ? 'bad' : off ? 'warn' : 'ok'}` }), h('span', { class: 'strong' }, head)),
    h('span', { class: 'small muted' }, startWords(start.last).replace(/, after .*/, ''), more ? `. ${more}` : ''),
    h('span', { class: 'small muted' }, `Last ${start.days || 7} days: switched off ${start.off} time${start.off === 1 ? '' : 's'}, `
      + `${start.crash} crash${start.crash === 1 ? '' : 'es'}.`));
}
