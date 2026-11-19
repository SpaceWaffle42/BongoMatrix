// Settings > Back up and restore (bongo/backup.py): one file with the show, and putting one back in two steps.
// Choosing a file changes nothing: the Matrix reads it and says what it would replace, what it would keep and
// what to know first; only Restore, in that review, writes anything, and then all of it or none.
import { DEMO, get, post } from './api.js';
import { h, toast } from './ui.js';
import { S, go, dirtyCount, problemLine } from './app.js';

const FILE = '/api/backup';
const kb = (n) => (n < 1048576 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1048576).toFixed(1).replace(/\.0$/, '')} MB`);
/** A problem the Matrix found in a backup, in words. "playlist[1]: ..." is the backup's second screen, not this page's. */
const problemWords = (p) => { const m = /^playlist\[(\d+)\]:?\s*([\s\S]*)$/.exec(p); return m ? `Screen ${Number(m[1]) + 1} of the backup: ${m[2]}` : problemLine(p); };
let done = null;   // a restore just made: the page is drawn afresh from the Matrix, and the new card says so once

export function backupCard() {
  const said = done; done = null;
  const last = h('p', { class: 'muted tiny flush', hidden: true });
  // shown while a backup would hold a web address at all: a key in the path of one looks like any other path
  const check = h('p', { class: 'muted tiny flush', hidden: true }, 'Check any web addresses with a key in them before sharing the file.');
  const box = h('div', { class: 'backup-box stack-s', hidden: true, role: 'region', 'aria-live': 'polite' });
  const file = h('input', { type: 'file', class: 'sr-only', id: 'backup-file', tabindex: '-1', 'aria-hidden': 'true', accept: '.json,application/json' });
  const pick = h('button', { type: 'button', class: 'btn' }, 'Restore from a file…');
  let most = 1048576, run = 0;   // ``run`` goes up with every file chosen and every Cancel: an older answer is ignored

  const noDemo = (ev) => { if (DEMO) { ev.preventDefault(); toast('The demo has no Matrix to back up'); } };
  const download = (label, cls = 'btn') => h('a', { class: cls, href: FILE, download: '', onclick: (ev) => {
    noDemo(ev);
    if (!DEMO) [1500, 5000].forEach((ms) => setTimeout(lastBackup, ms));   // the browser saves it; then the line under the buttons
  } }, label);

  async function lastBackup() {
    let st;
    try { st = await get(`${FILE}/status`); } catch { return; }   // (the line just stays as it is)
    most = st.most || most;
    check.hidden = !st.check_addresses;
    last.hidden = !st.last;
    if (st.last) last.textContent = `Last backup: ${st.last.file} (${kb(st.last.bytes)}), downloaded ${st.last.day}.`;
  }

  const show = (...kids) => { box.hidden = false; box.replaceChildren(...kids.flat().filter(Boolean)); };
  const close = () => { run += 1; box.hidden = true; box.replaceChildren(); file.value = ''; pick.disabled = false; pick.focus(); };
  const title = (text) => h('h3', { class: 'strong small flush backup-file', tabindex: '-1' }, text);
  const from = (b) => (b && (b.name || b.date || b.version) ? h('p', { class: 'small flush' }, 'From ',
    b.name ? h('b', null, b.name) : 'a Matrix with no name', b.date ? `, ${b.date}` : '', b.version ? `, version ${b.version}` : '', '.') : null);
  const focus = () => box.querySelector('h3')?.focus({ preventScroll: true });

  /** A file that can't be restored: why, in the Matrix's words. Nothing was changed. */
  function refuse(name, err, back) {
    const problems = err.data?.problems || [];
    show(title(`Can't restore ${name}`), from(err.data?.backup),
      h('p', { class: 'error-box', role: 'alert' }, err.message,
        problems.length ? h('ul', null, problems.map((p) => h('li', null, problemWords(p)))) : null),
      h('div', { class: 'row gap-s wrap' },
        back ? h('button', { type: 'button', class: 'btn', onclick: back }, 'Back') : h('button', { type: 'button', class: 'btn', onclick: () => { close(); file.click(); } }, 'Choose another file'),
        h('button', { type: 'button', class: 'btn ghost', onclick: close }, 'Close')));
    pick.disabled = false;
    focus();
  }

  async function chosen(f) {
    const mine = ++run;
    pick.disabled = true;
    if (f.size > most) { refuse(f.name, { message: `This file is too big to be a backup: a backup is never more than ${kb(most)}.` }); return; }
    show(h('p', { class: 'small muted flush backup-file' }, `Checking ${f.name}…`));
    let text, review;
    try { text = await f.text(); }
    catch { if (mine === run) refuse(f.name, { message: "This file couldn't be read on this device." }); return; }
    try { review = await post(`${FILE}/review`, { text }); }
    catch (err) { if (mine === run) refuse(f.name, err); return; }
    if (mine === run) ask(f.name, text, review);
  }

  /** Step two: what restoring replaces and keeps, and what to know first. Restore is the only thing that writes. */
  function ask(name, text, r) {
    const mine = run;
    const restore = h('button', { type: 'button', class: 'btn primary' }, 'Restore');
    const cancel = h('button', { type: 'button', class: 'btn ghost', onclick: close }, 'Cancel');
    const unsaved = dirtyCount();
    restore.onclick = async () => {
      restore.disabled = cancel.disabled = true; restore.textContent = 'Restoring…';
      let res;
      try { res = await post(`${FILE}/restore`, { text }); }
      catch (err) { if (mine === run) refuse(name, err, () => ask(name, text, r)); return; }
      done = { name, res };
      S.draft = null; S.config = null;   // everything this page knew is old now: it is fetched and drawn again
      go('/settings');
    };
    show(title(`Restore ${name}?`), from(r),
      h('p', { class: 'small flush' }, h('b', null, 'Replaces:'), ` the playlist (${r.screens} screen${r.screens === 1 ? '' : 's'}) and their settings, Splash, Sleep, the boot screen, location and time zone.`,
        h('br'), h('b', null, "Keeps this Matrix's own:"), ' name and badge colour, panel size and wiring, safe area, brightness, Wi-Fi, keys and sign-ins, paired browsers.'),
      r.warnings.map((w) => h('p', { class: 'warn small flush', 'data-kind': w.kind }, w.text)),
      unsaved ? h('p', { class: 'warn small flush' }, `This page has ${unsaved} unsaved playlist change${unsaved === 1 ? '' : 's'}: restoring discards ${unsaved === 1 ? 'it' : 'them'}.`) : null,
      r.notes.map((n) => h('p', { class: 'muted small flush' }, n)),
      h('div', { class: 'row gap-s wrap' }, restore, download('Download a backup of this Matrix first'), cancel),
      h('p', { class: 'muted tiny flush' }, 'A backup from a newer version than this Matrix is refused: update this Matrix first.'));
    pick.disabled = false;
    focus();
  }

  if (said) {
    const r = said.res;
    show(title(`Restored ${said.name}`),
      h('p', { class: 'small flush backup-done' }, `The playlist (${r.screens} screen${r.screens === 1 ? '' : 's'}), Splash, Sleep, the boot screen, location and time zone are now `,
        r.name ? h('b', null, `${r.name}'s`) : "the backup's", r.date ? `, as backed up on ${r.date}` : '', '. The panel is playing them.'),
      r.warnings.length ? h('p', { class: 'small flush strong' }, 'Still to do:') : null,
      r.warnings.map((w) => h('p', { class: 'warn small flush', 'data-kind': w.kind }, w.text)),
      h('div', { class: 'row gap-s wrap' }, h('button', { type: 'button', class: 'btn', onclick: close }, 'Done')));
  }

  box.addEventListener('keydown', (ev) => {   // Escape: as Cancel (not while a restore is being written)
    if (ev.key !== 'Escape' || box.querySelector('button:disabled')) return;
    ev.preventDefault(); close();
  });
  pick.onclick = () => { if (DEMO) { toast('The demo has no Matrix to restore to'); return; } file.click(); };
  file.onchange = () => { if (file.files[0]) chosen(file.files[0]); };
  if (!DEMO) lastBackup();
  const card = h('section', { class: 'card stack', id: 'backup', 'aria-label': 'Back up and restore' },
    h('div', null, h('h2', { class: 'h2' }, 'Back up and restore'),
      h('p', { class: 'muted small flush' }, "One file with the playlist and every screen's settings, Splash, Sleep and the boot screen, location and time zone. "
        + "Never in it: API keys (OpenWeather), Spotify's sign-in, private calendar links, module and add-on passwords and tokens, paired browsers, Wi-Fi, brightness. "
        + "Your media files aren't in it either.")),
    h('div', { class: 'row gap-s wrap' }, download('Download backup', 'btn primary'), pick, file),
    check, last, box);
  if (said) {   // the page was drawn again from the top: back to this card, clear of the bars, and again as the
    let mine = true;   // cards above it fill in and grow (until whoever is there scrolls for themselves)
    const leave = () => { mine = false; };
    ['wheel', 'touchstart', 'keydown'].forEach((type) => window.addEventListener(type, leave, { once: true, passive: true }));
    const back = () => {
      if (!mine || !card.isConnected) return;
      const bars = (document.getElementById('topbar')?.offsetHeight || 0) + (document.querySelector('.jump-bar')?.offsetHeight || 0);
      window.scrollTo({ top: card.getBoundingClientRect().top + window.scrollY - bars - 12 });
    };
    requestAnimationFrame(() => { back(); focus(); });
    [300, 900, 2000].forEach((ms) => setTimeout(back, ms));
  }
  return card;
}
