// An Animator project as a small text file: everything needed to open a drawing again exactly as it was
// (its size, layers, frames, timings and colours). The Matrix reads the same file to draw a sprite
// (bongo/animator.py), so the built-in art lives in the repository as text and can be edited here.
//
//   { "format": "bongo-animation", "version": 1, "name": "moon", "width": 16, "height": 16,
//     "background": "#000000", "loop": "loop", "palette": ["#fff4de", ...],
//     "layers": [{ "name": "Drawing", "on": true, "opacity": 1, "mode": "normal", "shared": false }],
//     "frames": [{ "ms": 90, "cels": ["64*. 3 3 12*a ..."] }, { "ms": 90, "cels": ["="] }] }
//
// A cel is one layer's pixels in one frame, row by row, as runs: "12*a" is twelve pixels of palette colour a
// (a number in base 36), "." is an empty pixel, and "=" alone means "the same as the frame before".
(function () {
  const FORMAT = 'bongo-animation', VERSION = 1, MAX_SIDE = 128, MAX_FRAMES = 300, MAX_LAYERS = 8;
  const MAX_PIXELS = 300 * 64 * 32;   // width x height x frames: a bigger canvas gets fewer frames (the Matrix's limit too)
  const maxFrames = (w, h) => Math.min(MAX_FRAMES, Math.floor(MAX_PIXELS / (w * h)));
  const MODES = { normal: 'Normal', light: 'Adds light' };

  function encodeCel(px, index) {
    const out = [];
    for (let i = 0; i < px.length;) {
      let j = i + 1; while (j < px.length && px[j] === px[i]) j++;
      const token = px[i] ? index.get(px[i]).toString(36) : '.';
      out.push(j - i > 1 ? `${j - i}*${token}` : token); i = j;
    }
    return out.join(' ');
  }
  const TOKEN = /^(?:([1-9][0-9]{0,5})\*)?(\.|[0-9a-z]{1,4})$/;   // "12*a", "a" or "."; bongo/animator.py reads the same
  function decodeCel(text, palette, n) {
    const px = [];
    for (const token of text.split(' ')) {
      if (!token) continue;
      const m = TOKEN.exec(token);
      if (!m) throw new Error('a layer has pixels the file doesn\'t describe');
      const count = Number(m[1] || 1), c = m[2] === '.' ? null : palette[parseInt(m[2], 36)];
      if (c === undefined) throw new Error('a layer uses a colour the file doesn\'t list');
      if (px.length + count > n) throw new Error('a layer has pixels that don\'t fit its size');
      for (let k = 0; k < count; k++) px.push(c);
    }
    if (px.length !== n) throw new Error('a layer has too few pixels for its size');
    return px;
  }

  // doc: { name, w, h, bg, loop, layers: [{ name, on, opacity, mode, shared }], frames: [{ ms, cels: [px] }] }
  function encode(doc) {
    const palette = [...new Set(doc.frames.flatMap((f) => f.cels.flatMap((c) => c)).filter(Boolean))].sort();
    const index = new Map(palette.map((c, i) => [c, i]));
    let before = null;
    const frames = doc.frames.map((f) => {
      const cels = f.cels.map((c) => encodeCel(c, index)), out = cels.map((c, k) => (before && before[k] === c ? '=' : c));
      before = cels;
      return { ms: f.ms, cels: out };
    });
    return { format: FORMAT, version: VERSION, name: doc.name, width: doc.w, height: doc.h, background: doc.bg, loop: doc.loop, palette,
      layers: doc.layers.map((l) => ({ name: l.name, on: l.on !== false, opacity: l.opacity ?? 1, mode: l.mode || 'normal', shared: !!l.shared })), frames };
  }
  // as text: a line per frame, so a change to one frame is one changed line
  function stringify(file) {
    const { frames, ...head } = file;
    return JSON.stringify(head).slice(0, -1) + ',"frames":[\n' + frames.map((f) => JSON.stringify(f)).join(',\n') + '\n]}\n';
  }

  function decode(file) {
    if (!file || file.format !== FORMAT) throw new Error('that isn\'t an Animator project');
    if (file.version !== VERSION) throw new Error('that project was made by a newer Animator');
    const w = file.width, h = file.height, side = (v) => Number.isInteger(v) && v >= 1 && v <= MAX_SIDE;
    if (!side(w) || !side(h)) throw new Error(`a project is 1 to ${MAX_SIDE} pixels each way`);
    if (!Array.isArray(file.palette) || file.palette.some((c) => typeof c !== 'string' || !/^#[0-9a-f]{6}$/i.test(c))) throw new Error('its colours aren\'t ones the Animator knows');
    const palette = file.palette.map((c) => c.toLowerCase());
    const layers = (file.layers || []).map((l, k) => ({ id: k + 1, name: String(l.name || `Layer ${k + 1}`).slice(0, 24), on: l.on !== false,
      opacity: Number.isFinite(Number(l.opacity ?? 1)) ? Math.max(0, Math.min(1, Number(l.opacity ?? 1))) : 1, mode: MODES[l.mode] ? l.mode : 'normal', shared: !!l.shared }));
    if (!layers.length || layers.length > MAX_LAYERS) throw new Error(`a project has 1 to ${MAX_LAYERS} layers`);
    if (!Array.isArray(file.frames) || !file.frames.length) throw new Error(`a project has 1 to ${MAX_FRAMES} frames`);
    if (file.frames.length > maxFrames(w, h)) throw new Error(`at ${w} × ${h} an animation can have up to ${maxFrames(w, h)} frames`);
    let before = null;
    const frames = file.frames.map((f) => {
      if (!Array.isArray(f.cels) || f.cels.length !== layers.length) throw new Error('a frame doesn\'t have every layer');
      const cels = f.cels.map((c, k) => {
        if (c === '=') { if (!before) throw new Error('the first frame can\'t repeat one before it'); return before[k].slice(); }
        return decodeCel(String(c), palette, w * h);
      });
      before = cels;
      if (f.ms !== undefined && (typeof f.ms !== 'number' || !Number.isFinite(f.ms))) throw new Error('a frame needs a time in ms');
      return { ms: Math.max(20, Math.min(5000, Math.round(f.ms ?? 90))), cels };
    });
    layers.forEach((l, k) => { if (l.shared) frames.forEach((f) => { f.cels[k] = frames[0].cels[k]; }); });   // one cel, on every frame
    return { name: String(file.name || 'animation'), w, h, bg: /^#[0-9a-f]{6}$/i.test(file.background || '') ? file.background.toLowerCase() : '#000000',
      loop: ['loop', 'pingpong', 'once'].includes(file.loop) ? file.loop : 'loop', layers, frames };
  }

  window.AnimatorProject = { FORMAT, VERSION, MAX_SIDE, MAX_FRAMES, MAX_PIXELS, MAX_LAYERS, MODES, maxFrames, encode, decode, stringify, encodeCel, decodeCel };
})();
