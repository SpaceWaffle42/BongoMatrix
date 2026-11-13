// Help guides: a Help button that opens a module's step-by-step setup guide (module.help, from the
// Matrix: see bongo/module.py). Text in `backticks` is something to copy, with its own Copy button.
import { h, icon, copyRaw } from './ui.js';
import { href } from './app.js';

/** A "Help" button for a module's guide (null when the module has none). */
export function helpButton(guide, { label = 'Help', cls = 'btn ghost small-btn' } = {}) {
  if (!guide) return null;
  return h('button', { type: 'button', class: cls, 'aria-haspopup': 'dialog', onclick: () => openHelp(guide) },
    icon('help'), label);
}

/** Copy one chip's text. Feedback goes on its own button (a toast would sit under the open dialog),
 * and is read out by the dialog's live region. If the browser won't copy, the text is selected instead. */
async function copy(text, chip, btn) {
  const say = (msg) => { const live = chip.closest('dialog')?.querySelector('.help-live'); if (live) live.textContent = msg; };
  clearTimeout(btn.reset);
  if (await copyRaw(text, chip)) {
    btn.textContent = 'Copied ✓'; say(`Copied ${text}`);
  } else {
    const range = document.createRange();
    range.selectNodeContents(chip.querySelector('.help-code-text'));
    const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
    btn.textContent = 'Selected: copy it'; say('Selected. Use your browser\'s Copy, or Ctrl+C.');
  }
  btn.reset = setTimeout(() => { btn.textContent = 'Copy'; }, 2500);
}

/** One line of guide text: plain words, with each `backticked` part as a copyable chip. */
function line(text) {
  return text.split('`').map((part, k) => {
    if (k % 2 === 0) return part;
    // break points after / ? & = only, so a long address wraps at sensible places and stays readable
    // (<wbr> adds nothing to what's selected; the Copy button copies the text itself anyway)
    // (no regex lookbehind here: iPhones before iOS 16.4 can't load a file that has one)
    const bits = part.replace(/[/?&=]/g, '$&\n').split('\n').filter(Boolean).flatMap((b, j) => (j ? [h('wbr'), b] : [b]));
    const chip = h('span', { class: 'help-code' }, h('span', { class: 'help-code-text mono' }, ...bits));
    const btn = h('button', { type: 'button', class: 'help-copy', 'aria-label': `Copy ${part}`, title: 'Copy', onclick: () => copy(part, chip, btn) }, 'Copy');
    chip.append(btn);
    return chip;
  });
}

export function openHelp(guide) {
  const here = location.pathname;
  const links = (guide.links || []).filter((l) => !(l.route && (here === l.route || location.hash === `#${l.route}`)));
  const dlg = h('dialog', { class: 'dialog wide help', 'aria-labelledby': 'help-title' },
    h('div', { class: 'stack' },
      h('div', { class: 'row between' }, h('h2', { class: 'h2', id: 'help-title' }, guide.title),
        h('button', { type: 'button', class: 'btn ghost', 'aria-label': 'Close', onclick: () => dlg.close() }, icon('x'))),
      guide.intro ? h('p', { class: 'muted small flush' }, guide.intro) : null,
      h('ol', { class: 'help-steps' }, ...guide.steps.map((s) => h('li', null, ...line(s)))),
      links.length ? h('div', { class: 'row gap-s wrap' }, ...links.map((l) => l.url
        ? h('a', { class: 'btn small-btn', href: l.url, target: '_blank', rel: 'noopener noreferrer' }, l.label, ' ↗')
        : h('a', { class: 'btn small-btn', href: href(l.route), 'data-route': l.route, onclick: () => dlg.close() }, l.label))) : null,
      guide.tips?.length ? h('div', { class: 'stack gap-s' }, h('h3', { class: 'h3 flush' }, 'Good to know'),
        h('ul', { class: 'help-tips' }, ...guide.tips.map((s) => h('li', null, ...line(s))))) : null,
      h('p', { class: 'sr-only help-live', 'aria-live': 'polite' }),
      h('div', { class: 'row end' }, h('button', { type: 'button', class: 'btn primary', onclick: () => dlg.close() }, 'Done'))));
  const away = () => dlg.close();   // going back or to another page closes the guide too
  window.addEventListener('popstate', away);
  window.addEventListener('hashchange', away);
  dlg.addEventListener('close', () => {
    window.removeEventListener('popstate', away);
    window.removeEventListener('hashchange', away);
    dlg.remove();
  });
  document.body.append(dlg);
  if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
  return dlg;
}
