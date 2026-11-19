// Which canvas sizes each preset suits (Animator > Presets). Most presets are compositions drawn for the 64 x 32 panel:
// a house, a lighthouse, a game's playfield. A few are fields of colour or light that read the canvas size and fill any
// canvas. Every preset gets `sizes`:  { any: true, min: [w, h] }  (drawn at the canvas size, from min up)
//                                  or { only: [[64, 32]] }        (a 64 x 32 composition)
// A 64 x 32 composition on a bigger canvas is drawn at 64 x 32 and set in the middle (a sky or sea whose top or bottom row is one colour carries on up or down to the
// edge of the canvas); on a canvas exactly 2x, 3x... the panel each pixel becomes a block instead; on a smaller canvas it doesn't fit, and the page asks
// whether to switch the canvas to 64 x 32. The built-in boot, splash and sleep screens are all 64 x 32 compositions:
// tools/build_sleep.py draws them at 64 x 32 only, so they are 'only' here and nothing else needs a size.
(function () {
  const T = window.PixelTools, PANEL = [64, 32], MIN = [32, 16];
  // the presets that follow the canvas: they take their middle, their spread and their height from it
  const ANY = new Set(['blank', 'stars', 'rain', 'plasma', 'rainbow', 'tunnel', 'ripple', 'spiral', 'eq', 'hearts', 'confetti']);
  for (const p of window.ANIMATOR_PRESETS) p.sizes = ANY.has(p.id) ? { any: true, min: MIN } : { only: [PANEL] };

  // how a preset is drawn on a w x h canvas: 'native' (drawn at that size), 'scaled' (a whole number of times 64 x 32: each pixel a block), 'centred' (64 x 32, in the middle) or 'no' (won't fit)
  function fit(p, w, h) {
    const s = p.sizes || { only: [PANEL] };
    if (s.only ? s.only.some(([a, b]) => a === w && b === h) : w >= s.min[0] && h >= s.min[1]) return 'native';
    if (s.only && w > PANEL[0] && w % PANEL[0] === 0 && h === w / PANEL[0] * PANEL[1]) return 'scaled';   // exactly 2x, 3x...: every pixel becomes a block
    return s.only && w >= PANEL[0] && h >= PANEL[1] ? 'centred' : 'no';
  }
  // an edge row that is one colour all along (a sky, a sea) carries on, row after row, to the canvas's edge; anything else is left empty
  const flat = (px, from, n) => { for (let k = 1; k < n; k++) if (px[from + k] !== px[from]) return false; return !!px[from]; };
  // a w0 x h0 picture in the middle of w1 x h1 (the canvas's own background shows round it)
  function place(px, w0, h0, w1, h1) {
    const ox = (w1 - w0) >> 1, oy = (h1 - h0) >> 1, out = new Array(w1 * h1).fill(null);
    for (let y = 0; y < h1; y++) {
      const sy = y - oy, row = Math.max(0, Math.min(h0 - 1, sy));
      if (sy < 0 || sy >= h0) { if (!flat(px, row * w0, w0)) continue; }   // above or below the picture: only a flat edge row goes on
      for (let x = 0; x < w0; x++) if (ox + x >= 0 && ox + x < w1) out[y * w1 + ox + x] = px[row * w0 + x];
    }
    return out;
  }
  // each pixel of a 64 x 32 picture as a k x k block
  function scale(px, w0, h0, k) {
    const w1 = w0 * k, out = new Array(w1 * h0 * k);
    for (let y = 0; y < h0 * k; y++) { const row = ((y / k) | 0) * w0; for (let x = 0; x < w1; x++) out[y * w1 + x] = px[row + ((x / k) | 0)]; }
    return out;
  }
  const onPanel = (draw) => { const w = T.W, h = T.H; T.size(PANEL[0], PANEL[1]); try { return draw(); } finally { T.size(w, h); } };
  const at = (draw, w, h) => { const a = T.W, b = T.H; T.size(w, h); try { return draw(); } finally { T.size(a, b); } };
  // the frames of preset p on a w x h canvas (null if it won't fit), as p.make() gives them
  function build(p, w, h) {
    const how = fit(p, w, h);
    if (how === 'native') return at(() => p.make(), w, h);
    if (how === 'no') return null;
    const arrange = (px) => (how === 'scaled' ? scale(px, PANEL[0], PANEL[1], w / PANEL[0]) : place(px, PANEL[0], PANEL[1], w, h));
    const made = onPanel(() => p.make()), out = made.map((f) => ({ px: arrange(f.px), ms: f.ms }));
    Object.defineProperty(out, 'frameAt', { value: (i) => { const f = onPanel(() => made.frameAt(i)); return { px: arrange(f.px), ms: f.ms }; } });
    return out;
  }
  // the gallery's thumbnails: { length, frameAt(i) }, drawn a frame at a time
  function peek(p, w, h) {
    const how = fit(p, w, h), [dw, dh] = how === 'native' ? [w, h] : PANEL;   // the size it is drawn at
    const inner = at(() => p.peek(), dw, dh);
    const frame = (i) => at(() => inner.frameAt(i), dw, dh);
    if (how === 'native' || how === 'no') return { length: inner.length, frameAt: frame };   // ('no': shown at 64 x 32, as it is)
    return { length: inner.length, frameAt: (i) => { const f = frame(i); return { px: how === 'scaled' ? scale(f.px, PANEL[0], PANEL[1], w / PANEL[0]) : place(f.px, PANEL[0], PANEL[1], w, h), ms: f.ms }; } };
  }
  window.PresetSizes = { PANEL, MIN, fit, place, scale, build, peek, size: (p) => p.sizes };
})();
