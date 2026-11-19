// Pair view: ask the panel for a PIN, type it here, get the cookie.
import { DEMO, get, post } from './api.js';
import { h, toast, store } from './ui.js';
import { S, go } from './app.js';
import { drawLed } from './led.js';
import { UPDATE, safeAfter } from './deeplink.js';

/** A 64x32 picture of what the panel shows while pairing: PAIR over six blank digits. */
function pairFrame() {
  const c = document.createElement('canvas'); c.width = 64; c.height = 32;
  const x = c.getContext('2d');
  x.fillStyle = '#000'; x.fillRect(0, 0, 64, 32);
  x.fillStyle = '#8FB4FF';
  // P A I R in a 3x5 pixel font
  const glyphs = { P: ['111', '101', '111', '100', '100'], A: ['010', '101', '111', '101', '101'], I: ['111', '010', '010', '010', '111'], R: ['110', '101', '110', '101', '101'] };
  [...'PAIR'].forEach((ch, k) => glyphs[ch].forEach((row, y) => [...row].forEach((b, gx) => { if (b === '1') x.fillRect(24 + k * 4 + gx, 6 + y, 1, 1); })));
  x.fillStyle = '#FFE3B3';
  for (let k = 0; k < 6; k++) x.fillRect(9 + k * 7 + (k > 2 ? 4 : 0), 21, 5, 2);
  return c;
}

function guessName() {
  const ua = navigator.userAgent;
  const what = /iPhone/.test(ua) ? 'iPhone' : /iPad/.test(ua) ? 'iPad' : /Android/.test(ua) ? 'Android phone'
    : /Mac/.test(ua) ? 'Mac' : /Windows/.test(ua) ? 'Windows PC' : /Linux/.test(ua) ? 'Linux computer' : 'Browser';
  return store.get('deviceName', what);
}

/** Sent here by the hub's Update… link (this browser hadn't paired with this Matrix yet)? */
function forUpdate() {
  try { return (sessionStorage.getItem('bongo.after') || '').endsWith(UPDATE); } catch { return false; }
}

export function render(view) {
  const boxes = Array.from({ length: 6 }, (_, i) => h('input', {
    class: 'pin-box', inputmode: 'numeric', autocomplete: i ? 'off' : 'one-time-code', maxlength: 6,
    pattern: '[0-9]*', 'aria-label': `Digit ${i + 1} of 6`,
  }));
  const error = h('p', { class: 'error-box', role: 'alert', hidden: true });
  const name = h('input', { class: 'input', id: 'pair-name', maxlength: 40, value: guessName(), autocomplete: 'off' });
  const submit = h('button', { class: 'btn primary big', type: 'submit' }, 'Pair this browser');
  const pin = () => boxes.map((b) => b.value).join('');

  const fill = (digits, from = 0) => {
    digits = digits.replace(/\D/g, '').slice(0, 6 - from);
    [...digits].forEach((d, k) => { boxes[from + k].value = d; });
    boxes[Math.min(5, from + digits.length)].focus();
    paintBoxes();
    if (pin().length === 6 && name.value.trim()) submit.focus();
  };
  const paintBoxes = () => boxes.forEach((b) => b.classList.toggle('filled', !!b.value));
  boxes.forEach((box, i) => {
    box.addEventListener('input', () => {
      const v = box.value.replace(/\D/g, '');
      box.value = '';
      if (v) fill(v, i); else paintBoxes();
      error.hidden = true;
    });
    box.addEventListener('paste', (e) => {
      const text = (e.clipboardData || window.clipboardData)?.getData('text') || '';
      if (/\d/.test(text)) { e.preventDefault(); fill(text, text.replace(/\D/g, '').length >= 6 ? 0 : i); }
    });
    box.addEventListener('keydown', (e) => {
      if (e.key === 'Backspace' && !box.value && i) { boxes[i - 1].value = ''; boxes[i - 1].focus(); paintBoxes(); e.preventDefault(); }
      else if (e.key === 'ArrowLeft' && i) boxes[i - 1].focus();
      else if (e.key === 'ArrowRight' && i < 5) boxes[i + 1].focus();
    });
    box.addEventListener('focus', () => box.select());
  });

  const showBtn = h('button', { class: 'btn ghost', type: 'button' }, 'Show the PIN again');
  const showNote = h('p', { class: 'muted small', 'aria-live': 'polite' });
  const showPin = async (quiet = false) => {
    showBtn.disabled = true;
    try {
      const res = await post('/api/pair/show');
      showNote.textContent = `The panel shows a PIN for ${Math.round((res?.seconds || 120) / 60)} minutes.${DEMO ? ' (Demo: any 6 digits except 000000 work.)' : ''}`;
      boxes[0].focus();
    } catch (err) { if (!quiet) toast(`Couldn't ask the panel: ${err.message}`, 'error'); }
    finally { setTimeout(() => { showBtn.disabled = false; }, 5000); }
  };
  showBtn.addEventListener('click', () => showPin());
  (async () => {   // the PIN only works while it's on the panel, so put it up straight away...
    try { if ((await get('/api/pair/status')).paired) { go('/', { replace: true }); return; } }   // ...unless this browser is paired already
    catch { /* can't tell: ask for the PIN anyway */ }
    if (document.body.classList.contains('pairing')) showPin(true);   // (still on this page)
  })();

  const form = h('form', { class: 'pair-form', novalidate: true },
    h('div', null, h('div', { class: 'eyebrow' }, 'Step 2 · Enter it here'), h('h1', { class: 'h1' }, 'Pair this browser')),
    h('fieldset', { class: 'pin-set' }, h('legend', null, '6-digit PIN'),
      h('div', { class: 'pin-row' }, boxes.slice(0, 3), h('span', { class: 'pin-gap', 'aria-hidden': 'true' }, '–'), boxes.slice(3)),
      h('span', { class: 'muted small' }, 'Pasting the full PIN fills every box.')),
    h('label', { class: 'field-col', for: 'pair-name' }, 'Name this device', name),
    forUpdate() ? h('p', { class: 'muted small flush' }, 'After pairing, the update is ready to confirm.') : null,
    error, submit,
    h('div', { class: 'pair-alt' }, h('span', null, 'PIN gone from the panel?'), showBtn), showNote);

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (pin().length !== 6) { error.textContent = 'Type all 6 digits of the PIN shown on the panel.'; error.hidden = false; boxes.find((b) => !b.value)?.focus(); return; }
    if (!name.value.trim()) { error.textContent = 'Give this device a name so you can recognise it later.'; error.hidden = false; name.focus(); return; }
    submit.disabled = true; submit.textContent = 'Pairing…';
    try {
      await post('/api/pair', { pin: pin(), name: name.value.trim() });
      store.set('deviceName', name.value.trim());
      toast('Paired. Welcome!', 'success');
      let after = '/';
      try { after = sessionStorage.getItem('bongo.after') || '/'; sessionStorage.removeItem('bongo.after'); } catch { /* ignore */ }
      S.config = null; S.draft = null;
      go(safeAfter(after), { replace: true });   // (only ever a page of this dashboard)
    } catch (err) {
      error.textContent = err.status === 403 ? err.message : `Couldn't pair: ${err.message}`;
      error.hidden = false;
      boxes.forEach((b) => { b.value = ''; }); paintBoxes(); boxes[0].focus();
    } finally { submit.disabled = false; submit.textContent = 'Pair this browser'; }
  });

  const panel = h('canvas', { class: 'led-canvas', role: 'img', 'aria-label': 'The panel shows the word PAIR and a six-digit PIN' });
  view.append(h('div', { class: 'pair-wrap' },
    h('section', { class: 'pair-info', 'aria-label': 'What the panel shows' },
      h('div', { class: 'eyebrow' }, 'Step 1 · Look at the panel'),
      h('div', { class: 'led-frame' }, panel),
      h('p', { class: 'muted' }, 'The panel now shows a six-digit PIN for two minutes. Type it here. Each PIN works once.')),
    h('section', { class: 'card pair-card', 'aria-label': 'Pair this browser' }, form)),
  h('p', { class: 'pair-foot muted small' }, 'Pairing works on this local network only. This browser stays paired for up to a year.'));
  requestAnimationFrame(() => drawLed(panel, pairFrame()));
  setTimeout(() => boxes[0].focus(), 50);
}
