// Drawing aids for the Animator's board (animator.js): things that help you draw but are never part of the
// drawing. The onion skin (neighbouring frames shown faintly), the centre lines, text lined up on the canvas,
// and a picture to trace over. None of it touches the frames: it is painted on the board and nowhere else.
(function () {
  const T = window.PixelTools;

  // -- onion skin -------------------------------------------------------------------------------
  // previous frames in a warm rose, next ones in cyan: both bright on black and on LED colours, and told apart
  // at a glance (red against blue-green, which colour-blind eyes separate too)
  const ONION_TINTS = { prev: '#ff4d6d', next: '#3ddcff' };
  const ONION_SHOWS = { prev: 'Previous', next: 'Next', both: 'Both' };
  const ONION_STRENGTHS = { 0.2: 'Faint', 0.35: 'Medium', 0.5: 'Strong' };
  // which frames to ghost under frame `cur` of n: on a loop the first frame's previous is the last (it plays
  // just before it) and the last frame's next is the first; played once or there and back, the ends have none
  function onionFrames(cur, n, show, loop) {
    const out = [], round = loop === 'loop' && n > 1;
    const at = (d) => { const i = cur + d; return i >= 0 && i < n ? i : round ? (i + n) % n : null; };
    if (show === 'prev' || show === 'both') { const i = at(-1); if (i !== null && i !== cur) out.push({ i, tint: ONION_TINTS.prev }); }
    if (show === 'next' || show === 'both') { const i = at(1); if (i !== null && i !== cur) out.push({ i, tint: ONION_TINTS.next }); }
    return out;
  }
  // the frames either side ([{ px, tint }]) drawn faintly over the frame you're on (`here`), only where they
  // differ from it: a background that doesn't move gets no ghost and doesn't hide one, and what moved shows where
  // it was (or will be). Their own colours are pulled most of the way to their tint, so shapes keep a little
  // shading but where they're from is plain. Both at once go in one pass, half each where they overlap, so a
  // pixel is never covered by more than `k`: what you're drawing stays readable under them
  function ghosts(ctx, list, here, w, scale, k) {
    const seen = new Map(), tinted = (c, tint) => { const key = c + tint; if (!seen.has(key)) seen.set(key, T.mix(c, tint, 0.7)); return seen.get(key); };
    ctx.globalAlpha = k;
    for (let i = 0; i < here.length; i++) {
      const shown = list.filter((g) => g.px[i] && g.px[i] !== here[i]);
      if (!shown.length) continue;
      ctx.fillStyle = shown.length > 1 ? T.mix(tinted(shown[0].px[i], shown[0].tint), tinted(shown[1].px[i], shown[1].tint), 0.5) : tinted(shown[0].px[i], shown[0].tint);
      ctx.fillRect((i % w) * scale, Math.floor(i / w) * scale, scale, scale);
    }
    ctx.globalAlpha = 1;
  }
  // the onion choice kept between visits (a browser that won't keep it just starts with the usual)
  function loadOnion(fallback) {
    try { const o = JSON.parse(localStorage.getItem('animator.onion') || 'null'); if (o && ONION_SHOWS[o.show] && ONION_STRENGTHS[o.k]) return { on: !!o.on, show: o.show, k: o.k }; }
    catch { /* no storage here */ }
    return fallback;
  }
  function saveOnion(o) { try { localStorage.setItem('animator.onion', JSON.stringify(o)); } catch { /* no storage here */ } }

  // -- the middle, and lining text up -----------------------------------------------------------
  // the box that counts: the safe area when its guide is showing on a panel-sized canvas, else the canvas
  function guideBox(w, h, safe) {
    return safe ? { x: safe.left, y: safe.top, w: w - safe.left - safe.right, h: h - safe.top - safe.bottom } : { x: 0, y: 0, w, h };
  }
  // the middle of a box, in pixels from the canvas's left and top: a whole number falls between two pixels
  // (an even width), a half runs through the middle of one (an odd width)
  const middle = (b) => ({ x: b.x + b.w / 2, y: b.y + b.h / 2 });
  // the centre lines, drawn exactly where the middle is
  function centreLines(ctx, b, scale, line) {
    const m = middle(b);
    ctx.save(); ctx.strokeStyle = 'rgba(255,92,207,0.85)'; ctx.lineWidth = line; ctx.setLineDash([line * 4, line * 3]); ctx.beginPath();
    ctx.moveTo(m.x * scale, b.y * scale); ctx.lineTo(m.x * scale, (b.y + b.h) * scale);
    ctx.moveTo(b.x * scale, m.y * scale); ctx.lineTo((b.x + b.w) * scale, m.y * scale);
    ctx.stroke(); ctx.restore();
  }
  // how far below the first line's top each line goes: the font's own height and a pixel between lines (as
  // bongo/fonts.py's middle() lays lines on the Matrix)
  const lineGap = (font) => font.cap + (font.descent || 0) + 1;
  // where a line starts so its drawn pixels sit left, centred or right in the box. An odd number of pixels
  // can't sit exactly in the middle of an even box (or the other way round): the spare pixel always goes on
  // the right, so centred text leans left by half a pixel, never right
  function lineX(s, font, align, b) {
    const ink = T.textInk(s, font);
    if (!ink) return b.x;
    if (align === 'right') return b.x + b.w - 1 - ink.right;
    if (align === 'centre') return b.x + Math.floor((b.w - (ink.right - ink.left + 1)) / 2) - ink.left;
    return b.x - ink.left;
  }
  // write text that may run over several lines, with the top of the first line's capitals at row y. `align`
  // '' puts every line's start at x (where you tapped); left, centre or right line each up in the box
  function text(s, x, y, font, align, b, set) {
    String(s).split('\n').forEach((one, k) => T.text(one, align ? lineX(one, font, align, b) : x, y + k * lineGap(font), font, set));
  }

  // -- a picture to trace over ------------------------------------------------------------------
  // fitted inside the canvas whole (no part cut off), in the middle, at `k` strength. It's only ever painted on
  // the board, so it's never in a frame, the undo history, a saved file, the GIF or the preview
  const fitted = new WeakMap();   // the picture scaled to the board once per board size, not on every redraw
  function picture(ctx, img, w, h, k) {
    const iw = img.naturalWidth || img.width, ih = img.naturalHeight || img.height;
    if (!iw || !ih) return;
    let fit = fitted.get(img);
    if (!fit || fit.width !== w || fit.height !== h) {
      fit = document.createElement('canvas'); fit.width = w; fit.height = h;
      const f = Math.min(w / iw, h / ih), dw = iw * f, dh = ih * f, c = fit.getContext('2d');
      c.imageSmoothingEnabled = true; c.imageSmoothingQuality = 'high'; c.drawImage(img, (w - dw) / 2, (h - dh) / 2, dw, dh); fitted.set(img, fit);
    }
    ctx.save(); ctx.globalAlpha = k; ctx.drawImage(fit, 0, 0); ctx.restore();
  }

  window.AnimatorAids = { ONION_TINTS, ONION_SHOWS, ONION_STRENGTHS, onionFrames, ghosts, loadOnion, saveOnion, guideBox, middle, centreLines, lineGap, lineX, text, picture };
})();
