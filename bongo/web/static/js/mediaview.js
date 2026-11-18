// See a media file before picking it: a small LED thumbnail drawn by the real Media module, and a
// preview dialog that animates it at panel size and can put it on the Matrix itself for a few seconds.
import { post, previewPath } from './api.js';
import { h, icon, toast, segmented } from './ui.js';
import { FrameLoop } from './led.js';
import { S } from './app.js';

const FITS = ['fit', 'fill', 'pixel'];
const entry = (name, fit, speed) => ({ module: 'media', style: fit, settings: { file: name, speed } });
const short = (name) => name.replace(/^(built-in|uploaded)\//, '');
/** A name to read: "built-in/splash_breakout_squares.gif" -> "Breakout squares", "my-cat.mp4" -> "My cat" */
export const mediaTitle = (name) => {
  const words = short(name).replace(/\.[a-z0-9]+$/i, '').replace(/^(splash|sleep|boot)_/, '').replace(/[_-]+/g, ' ').trim();
  return words ? words[0].toUpperCase() + words.slice(1) : short(name);
};

/** A tiny still of `name` as the panel draws it. Returns [element, loop] so the caller can stop it. */
export function mediaThumb(name, { fit = 'fit', speed = 1 } = {}) {
  const canvas = h('canvas', { class: 'led-canvas mini', 'aria-hidden': 'true' });
  const loop = new FrameLoop(canvas, { interval: Infinity, path: () => previewPath(entry(name, fit, speed)) });
  return [h('span', { class: 'led-frame mini media-thumb' }, canvas), loop];
}

/** A button that opens the preview for `name`; opts() gives the current { fit, speed }. */
export function previewButton(name, opts = () => ({}), { compact = false } = {}) {
  return h('button', { type: 'button', class: `btn ghost ${compact ? 'icon-btn small-icon' : 'small-btn'}`, 'aria-label': `Preview ${short(name)}`,
    title: compact ? 'Preview' : null,
    onclick: (ev) => { ev.preventDefault(); ev.stopPropagation(); openPreview(name, opts()); } }, icon('play'), compact ? null : 'Preview');
}

/** Animated, full-size preview of one file, with a button to show it on the Matrix for 15 s. */
export function openPreview(name, { fit = 'fit', speed = 1 } = {}) {
  const canvas = h('canvas', { class: 'led-canvas', role: 'img', 'aria-label': `Preview of ${short(name)}` });
  const err = h('p', { class: 'error-box small', role: 'status', hidden: true });
  const t0 = Date.now() / 1000;
  let frames = 0;
  const loop = new FrameLoop(canvas, {
    interval: 100,
    path: () => previewPath(entry(name, fit, speed), t0 + (frames++) * 0.1),
    led: () => ({ safeArea: S.config?.safe_area, panel: S.state?.panel, showSafe: false }),
    onError: (x) => { err.hidden = !x || x.status === 401; err.textContent = x ? `Preview failed: ${x.message}` : ''; },
  });
  const send = h('button', { type: 'button', class: 'btn primary' }, icon('now'), 'Show on Matrix (15 s)');
  send.onclick = async () => {
    send.disabled = true;
    try {
      await post('/api/push', { file: name, fit, speed, seconds: 15 });
      toast(`Showing ${short(name)} on the Matrix for 15 seconds`, 'success');
    } catch (x) { toast(`Couldn't show it: ${x.message}`, 'error', 8000); }
    finally { send.disabled = false; }
  };
  const dlg = h('dialog', { class: 'dialog wide', 'aria-label': `Preview ${short(name)}` },
    h('div', { class: 'stack' },
      h('div', { class: 'row between' }, h('h2', { class: 'h2 truncate', title: short(name) }, short(name)),
        h('button', { type: 'button', class: 'btn ghost', 'aria-label': 'Close', onclick: () => dlg.close() }, icon('x'))),
      h('span', { class: 'led-frame' }, canvas), err,
      h('div', { class: 'field-col' }, h('span', null, 'Fit'),
        segmented(FITS, fit, (v) => { fit = v; loop.refresh(); }, 'Fit')),
      h('p', { class: 'muted small flush' }, 'Drawn by the Matrix’s own code at its real size. Show it on the Matrix to check it on the LEDs.'),
      h('div', { class: 'row gap-s end wrap' }, send)));
  dlg.addEventListener('close', () => { loop.stop(); dlg.remove(); });
  document.body.append(dlg);
  if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
  requestAnimationFrame(() => loop.refresh());
  return dlg;
}
