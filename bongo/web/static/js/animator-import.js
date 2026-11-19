// Import GIF or picture, "as a new layer" and "as new frames" (window.AnimatorImport). Replacing the drawing is in
// animator.js. Nothing here changes what it is given: each returns the new layers and frames, or { error } in plain words
// (and then nothing is changed at all). The pictures come from the server already fitted to the canvas (bongo/animator.py).
//   c: { layers, frames, cur, al, imported: [{ px, ms }], W, H, maxFrames, maxLayers, name, newLayer(name) }
(function () {
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const ms = (f) => clamp(Math.round(f.ms) || 90, 20, 5000);
  const frames = (n) => `${n} frame${n === 1 ? '' : 's'}`;

  // A new layer, above the selected one, with the picture on the current frame. A GIF's frames go along the frames from
  // the current one, one each, and keep those frames' own times. If it is longer than what is left, new frames are added
  // at the end (the last frame's other layers carry on under them, with the GIF's own times); if it is shorter, the layer
  // is empty on the rest.
  function asLayer(c) {
    const n = c.imported.length, empty = () => new Array(c.W * c.H).fill(null);
    if (c.layers.length >= c.maxLayers) return { error: `Up to ${c.maxLayers} layers: delete or merge one first, or bring it in as new frames` };
    const extra = Math.max(0, c.cur + n - c.frames.length), total = c.frames.length + extra;
    if (total > c.maxFrames) return { error: `That picture would need ${frames(total)} here and an animation this size can have up to ${c.maxFrames}. Start from an earlier frame, or bring in a shorter one` };
    const at = c.al + 1, layers = c.layers.slice(), layer = c.newLayer(c.name);
    layers.splice(at, 0, layer);
    const put = (cels, px) => { const out = cels.slice(); out.splice(at, 0, px); return out; };
    const out = c.frames.map((f, i) => ({ ms: f.ms, px: f.px, cels: put(f.cels, i >= c.cur && i - c.cur < n ? c.imported[i - c.cur].px.slice() : empty()) }));
    const last = c.frames[c.frames.length - 1];
    for (let j = 0; j < extra; j++) {
      const piece = c.imported[c.frames.length - c.cur + j];
      out.push({ ms: ms(piece), px: [], cels: put(last.cels.map((cel, k) => (c.layers[k].shared ? cel : cel.slice())), piece.px.slice()) });
    }
    return { layers, frames: out, cur: c.cur, al: at, added: extra };
  }

  // New frames after the current one, drawn on the selected layer (the nearest that isn't "the same on every frame"),
  // with their own times and nothing on the other layers.
  function asFrames(c) {
    const n = c.imported.length, empty = () => new Array(c.W * c.H).fill(null);
    const own = !c.layers[c.al].shared ? c.al : c.layers.findIndex((l) => !l.shared);
    if (own < 0) return { error: 'Every layer here is the same on every frame: untick that on one to bring frames in' };
    if (c.locked && c.locked(own)) return { error: `"${c.layers[own].name}" is locked: unlock it, or pick another layer, to bring frames in on it` };
    const room = c.maxFrames - c.frames.length;
    if (n > room) return { error: room > 0 ? `There is room for ${room} more frame${room === 1 ? '' : 's'} here and this picture has ${n}` : `An animation this size can have up to ${c.maxFrames} frames, and this one has them all` };
    const made = c.imported.map((f) => ({ ms: ms(f), px: [], cels: c.layers.map((l, k) => (k === own ? f.px.slice() : l.shared ? c.frames[0].cels[k] : empty())) }));
    return { layers: c.layers, frames: c.frames.slice(0, c.cur + 1).concat(made, c.frames.slice(c.cur + 1)), cur: c.cur + 1, al: own, added: n };
  }

  window.AnimatorImport = { asLayer, asFrames };
})();
