// Selecting by colour and changing a colour, for the Animator's Select tool and its "Change a colour…" box.
// Pure: pixels in, pixels out (an array of W*H '#rrggbb' strings or null, as in animator-tools.js). An area is
// { x, y, w, h } with, when it isn't the whole box, a `mask` of w*h 1s and 0s saying which pixels are in it.
(function () {
  const T = window.PixelTools;
  // the smallest area holding the 1s of a whole-canvas mask; no mask when it fills its box; null when empty
  function areaOf(all) {
    const W = T.W, H = T.H; let x0 = W, y0 = H, x1 = -1, y1 = -1;
    for (let i = 0; i < W * H; i++) if (all[i]) { const x = i % W, y = Math.floor(i / W); x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y); }
    if (x1 < 0) return null;
    const w = x1 - x0 + 1, h = y1 - y0 + 1, mask = new Array(w * h).fill(0); let n = 0;
    for (let b = 0; b < h; b++) for (let a = 0; a < w; a++) if (all[(y0 + b) * W + x0 + a]) { mask[b * w + a] = 1; n++; }
    return n === w * h ? { x: x0, y: y0, w, h } : { x: x0, y: y0, w, h, mask };
  }
  // an area as a whole-canvas mask (what's off the canvas is dropped)
  function maskOf(r) {
    const W = T.W, H = T.H, all = new Array(W * H).fill(0);
    for (let b = 0; b < r.h; b++) for (let a = 0; a < r.w; a++) {
      const x = r.x + a, y = r.y + b;
      if (x >= 0 && y >= 0 && x < W && y < H && (!r.mask || r.mask[b * r.w + a])) all[y * W + x] = 1;
    }
    return all;
  }
  // the magic wand: the pixels of the colour at (x, y) that join it side by side (as Fill spreads), or with
  // `everywhere` every pixel of that colour. Empty counts as a colour.
  function wand(px, x, y, everywhere = false) {
    const W = T.W, H = T.H, c = px[y * W + x] || null, all = new Array(W * H).fill(0);
    if (everywhere) { for (let i = 0; i < W * H; i++) if ((px[i] || null) === c) all[i] = 1; return areaOf(all); }
    const stack = [[x, y]];
    while (stack.length) {
      const [a, b] = stack.pop(), i = b * W + a;
      if (a < 0 || b < 0 || a >= W || b >= H || all[i] || (px[i] || null) !== c) continue;
      all[i] = 1; stack.push([a + 1, b], [a - 1, b], [a, b + 1], [a, b - 1]);
    }
    return areaOf(all);
  }
  // everything on the canvas that isn't in the area (all of it when there's no area); null if nothing's left
  const inverted = (r) => areaOf(r ? maskOf(r).map((v) => 1 - v) : new Array(T.W * T.H).fill(1));
  // where to draw an area's outline: the edges between its pixels and the rest, as straight runs
  // [x0, y0, x1, y1] in pixels from its top-left corner (a box without a mask is its four sides)
  function outline(r) {
    const has = (a, b) => a >= 0 && b >= 0 && a < r.w && b < r.h && (!r.mask || !!r.mask[b * r.w + a]), out = [];
    for (let y = 0; y <= r.h; y++) {   // across: between row y - 1 and row y
      let from = -1;
      for (let x = 0; x <= r.w; x++) {
        const edge = x < r.w && has(x, y - 1) !== has(x, y);
        if (edge && from < 0) from = x; else if (!edge && from >= 0) { out.push([from, y, x, y]); from = -1; }
      }
    }
    for (let x = 0; x <= r.w; x++) {   // down: between column x - 1 and column x
      let from = -1;
      for (let y = 0; y <= r.h; y++) {
        const edge = y < r.h && has(x - 1, y) !== has(x, y);
        if (edge && from < 0) from = y; else if (!edge && from >= 0) { out.push([x, from, x, y]); from = -1; }
      }
    }
    return out;
  }

  // -- change a colour ----------------------------------------------------------------------------
  // `near`: also colours within this much of it on each of red, green and blue (0: that colour exactly)
  const close = (c, from, near) => {
    if (!c) return false;
    if (c.toLowerCase() === from) return true;
    if (!near) return false;
    const a = T.parse(c), b = T.parse(from);
    return Math.abs(a[0] - b[0]) <= near && Math.abs(a[1] - b[1]) <= near && Math.abs(a[2] - b[2]) <= near;
  };
  // one layer's pixels with `from` changed to `to` (inside `area` only, if given): the new pixels and how many changed
  function recoloured(px, from, to, { near = 0, area = null } = {}) {
    from = from.toLowerCase(); to = to.toLowerCase();
    const inside = area ? maskOf(area) : null, out = px.slice(); let n = 0;
    for (let i = 0; i < out.length; i++) if ((!inside || inside[i]) && close(out[i], from, near) && out[i].toLowerCase() !== to) { out[i] = to; n++; }
    return { px: out, n };
  }
  // over the drawing: this frame (frames[cur]) or every frame, this layer (layers[al]) or every layer. Hidden and
  // locked layers are left alone (`locked(k)`: the page keeps locks by layer id). A layer that's the same on every
  // frame is one drawing: with every frame it is changed (and counted) once and stays one array on every frame; with
  // this frame it is left alone (changing it would change every frame), and `shared` lists those layers' names. Only frames in scope are
  // changed: cels are never changed in place (they may be read-only, kept by Undo), each changed one is a new array.
  // Changes `frames` (their cels lists); gives how many pixels changed and how many layers were left alone.
  function recolourDrawing(frames, layers, from, to, { cur = 0, al = 0, allFrames = false, allLayers = false, near = 0, area = null, locked = (k) => !!layers[k].locked } = {}) {
    const done = new Map(), skip = (k) => !layers[k].on || locked(k), mine = (k) => allLayers || k === al; let n = 0;
    const skipped = layers.filter((l, k) => mine(k) && skip(k)).length;
    const shared = allFrames ? [] : layers.filter((l, k) => mine(k) && !skip(k) && l.shared).map((l) => l.name);
    (allFrames ? frames : [frames[cur]]).forEach((f) => {
      f.cels = f.cels.map((cel, k) => {
        if (!mine(k) || skip(k) || (!allFrames && layers[k].shared)) return cel;
        if (!done.has(cel)) { const r = recoloured(cel, from, to, { near, area }); n += r.n; done.set(cel, r.n ? r.px : cel); }
        return done.get(cel);
      });
    });
    const at = frames[allFrames ? 0 : cur];
    layers.forEach((l, k) => { if (l.shared) frames.forEach((f) => { f.cels[k] = at.cels[k]; }); });   // a shared cel stays one array on every frame
    return { n, skipped, shared };
  }

  window.AnimatorSelect = { areaOf, maskOf, wand, inverted, outline, recoloured, recolourDrawing };
})();
