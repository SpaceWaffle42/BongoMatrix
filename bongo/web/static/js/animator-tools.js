// Pixel tools for the Animator, shared by the editor (animator.html) and the presets
// (animator-presets.js), so every preset is made with the same brushes, shapes, gradients and blur
// you draw with. Everything works on one frame's pixels: an array of W*H '#rrggbb' strings or null
// (nothing drawn: the background shows). W x H is the panel's 64 x 32 unless the editor picks a sprite
// size with size(); the presets always draw at 64 x 32.
(function () {
  let W = 64, H = 32;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const byte = (v) => clamp(Math.round(v), 0, 255);
  const hex = (r, g, b) => '#' + [r, g, b].map((v) => byte(v).toString(16).padStart(2, '0')).join('');
  const parse = (c) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));
  const mix = (a, b, k) => { const x = parse(a), y = parse(b); return hex(...x.map((v, i) => v + (y[i] - v) * k)); };
  const inside = (x, y) => x >= 0 && y >= 0 && x < W && y < H;

  // -- brush shapes: the cells a brush of this size covers, centred on (0, 0) ------------------
  const SHAPES = ['square', 'round', 'diamond', 'plus', 'horizontal', 'vertical', 'slash', 'spray', 'dither'];
  const SHAPE_NAMES = { square: 'Square', round: 'Round', diamond: 'Diamond', plus: 'Plus', horizontal: 'Flat (wide)',
    vertical: 'Flat (tall)', slash: 'Calligraphy', spray: 'Spray', dither: 'Dither' };
  function tip(size, shape, seed = 0) {
    const out = [], o = Math.floor((size - 1) / 2), r = size / 2;
    let s = (seed * 9301 + 49297) % 233280 || 1;
    const rnd = () => (s = (s * 9301 + 49297) % 233280) / 233280;
    for (let b = 0; b < size; b++) for (let a = 0; a < size; a++) {
      const dx = a - o, dy = b - o, cx = a - (size - 1) / 2, cy = b - (size - 1) / 2;
      const keep = {
        square: true,
        round: size <= 2 || cx * cx + cy * cy <= r * r - 0.5,
        diamond: Math.abs(cx) + Math.abs(cy) <= r,
        plus: Math.abs(cx) < 0.6 || Math.abs(cy) < 0.6 || size <= 2,
        horizontal: dy === 0,
        vertical: dx === 0,
        slash: Math.abs(cx + cy) < 0.8,
        spray: size === 1 || (cx * cx + cy * cy <= r * r && rnd() < 0.35),
        dither: true,
      }[shape] ?? true;
      if (keep) out.push([dx, dy]);
    }
    return out;
  }
  // stamp a brush at (x, y); `paint(i)` gets each covered pixel index. Dither only touches every other pixel.
  function stamp(x, y, size, shape, paint, seed) {
    tip(size, shape, seed).forEach(([dx, dy]) => {
      const a = x + dx, b = y + dy;
      if (!inside(a, b) || (shape === 'dither' && (a + b) % 2)) return;
      paint(b * W + a, a, b);
    });
  }

  // -- lines and shapes: the cells, then stroked with the brush ---------------------------------
  function lineCells(x0, y0, x1, y1) {
    const cells = []; let dx = Math.abs(x1 - x0), dy = -Math.abs(y1 - y0), sx = x0 < x1 ? 1 : -1, sy = y0 < y1 ? 1 : -1, e = dx + dy;
    for (;;) { cells.push([x0, y0]); if (x0 === x1 && y0 === y1) break; const e2 = 2 * e; if (e2 >= dy) { e += dy; x0 += sx; } if (e2 <= dx) { e += dx; y0 += sy; } }
    return cells;
  }
  function rectCells(a, b, filled) {
    const [x0, x1] = [Math.min(a[0], b[0]), Math.max(a[0], b[0])], [y0, y1] = [Math.min(a[1], b[1]), Math.max(a[1], b[1])], out = [];
    for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) if (filled || x === x0 || x === x1 || y === y0 || y === y1) out.push([x, y]);
    return out;
  }
  function ellipseCells(a, b, filled) {   // inside the box a-b
    const [x0, x1] = [Math.min(a[0], b[0]), Math.max(a[0], b[0])], [y0, y1] = [Math.min(a[1], b[1]), Math.max(a[1], b[1])];
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, rx = (x1 - x0) / 2 + 0.5, ry = (y1 - y0) / 2 + 0.5, out = [];
    const isIn = (x, y) => ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1;
    for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) {
      if (!isIn(x, y)) continue;
      if (filled || !isIn(x - 1, y) || !isIn(x + 1, y) || !isIn(x, y - 1) || !isIn(x, y + 1)) out.push([x, y]);
    }
    return out;
  }

  // -- gradient -----------------------------------------------------------------------------
  const BAYER = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5].map((v) => (v + 0.5) / 16);
  const GRADIENT_TYPES = { linear: 'Straight', mirror: 'Mirrored', radial: 'Round', diamond: 'Diamond', conic: 'Sweep' };
  const GRADIENT_STYLES = { smooth: 'Smooth', steps: 'Bands', dither: 'Dithered' };
  const GRADIENT_AREAS = { frame: 'Whole frame', region: 'One area (like fill)', colour: 'Same colour, anywhere', drawn: 'Only what\'s drawn', empty: 'Only empty pixels' };
  // where 0..1 along the gradient a pixel sits, for a drag from a to b
  function along(type, a, b, x, y) {
    const dx = b[0] - a[0], dy = b[1] - a[1], len = Math.hypot(dx, dy) || 1;
    if (type === 'radial') return Math.hypot(x - a[0], y - a[1]) / len;
    if (type === 'diamond') return (Math.abs(x - a[0]) + Math.abs(y - a[1])) / Math.max(1, Math.abs(dx) + Math.abs(dy));
    if (type === 'conic') return ((Math.atan2(y - a[1], x - a[0]) - Math.atan2(dy, dx)) / (Math.PI * 2) % 1 + 1) % 1;
    const t = ((x - a[0]) * dx + (y - a[1]) * dy) / (len * len);
    return type === 'mirror' ? Math.abs(t) : t;
  }
  // colour at 0..1 through a list of stops (two colours, or more for a rainbow)
  function stopColour(stops, t) {
    t = clamp(t, 0, 1) * (stops.length - 1);
    const i = Math.min(stops.length - 2, Math.floor(t));
    return mix(stops[i], stops[i + 1], t - i);
  }
  // mirror: the half the drag isn't in shows the exact reflection of the half it is in (each pixel still
  // checked against its own Where)
  function gradient(px, a, b, stops, { type = 'linear', style = 'smooth', steps = 4, area = 'frame', from = null, mirror = false } = {}) {
    let mask = null;
    const region = (start, into) => {   // the connected area of one colour, as fill finds it
      const target = px[start[1] * W + start[0]], stack = [start];
      while (stack.length) { const [x, y] = stack.pop(), i = y * W + x;
        if (!inside(x, y) || into.has(i) || px[i] !== target) continue;
        into.add(i); stack.push([x + 1, y], [x - 1, y], [x, y + 1], [x, y - 1]); }
    };
    if (area === 'region' && from) { mask = new Set(); region(from, mask); if (mirror) region([W - 1 - from[0], from[1]], mask); }
    const same = area === 'colour' && from ? px[from[1] * W + from[0]] : undefined;   // every pixel of the colour you start on
    const left = a[0] < W / 2;
    for (let y = 0; y < H; y++) for (let X = 0; X < W; X++) {
      const i = y * W + X, x = mirror && (X < W / 2) !== left ? W - 1 - X : X;   // where its colour is worked out
      if (mask && !mask.has(i)) continue;
      if (area === 'drawn' && !px[i]) continue;
      if (area === 'empty' && px[i]) continue;
      if (same !== undefined && px[i] !== same) continue;
      let t = clamp(along(type, a, b, x, y), 0, 1);
      if (style === 'steps') t = Math.min(steps - 1, Math.floor(t * steps)) / Math.max(1, steps - 1);
      if (style === 'dither') {   // ordered dither between the stops' bands: crisp pixels, no in-between colours
        const n = Math.max(2, steps), scaled = t * (n - 1), lo = Math.floor(scaled), frac = scaled - lo;
        t = Math.min(n - 1, lo + (frac > BAYER[(y % 4) * 4 + (x % 4)] ? 1 : 0)) / (n - 1);
      }
      px[i] = stopColour(stops, t);
    }
  }

  // -- blur and shade brushes -----------------------------------------------------------------
  // blur the pixels under a brush: each becomes the average of its 3x3 neighbourhood (empty pixels
  // count as the background), `strength` 0..1 how far it moves there
  // `done`: pixels already blurred (or shaded) in this stroke, so dragging doesn't pile it up
  // `below`: what shows under this layer (the layers beneath, flattened), so empty pixels blend with that
  function blurAt(px, x, y, size, shape, strength, background, done = null, seed = 0, below = null) {
    const before = px.slice(), under = (j) => (below && below[j]) || background;
    stamp(x, y, size, shape, (i, a, b) => {
      if (done) { if (done.has(i)) return; done.add(i); }
      let r = 0, g = 0, bl = 0, n = 0;
      for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
        if (!inside(a + dx, b + dy)) continue;
        const k = (b + dy) * W + a + dx, c = before[k], v = parse(c || under(k));
        r += v[0]; g += v[1]; bl += v[2]; n++;
      }
      const avg = hex(r / n, g / n, bl / n), now = before[i] || under(i);
      const out = mix(now, avg, strength);
      px[i] = !before[i] && out.toLowerCase() === under(i).toLowerCase() ? null : out;
    }, seed);
  }
  // lighten or darken what's under the brush (empty pixels stay empty)
  function shadeAt(px, x, y, size, shape, amount, seed, done = null) {
    stamp(x, y, size, shape, (i) => {
      if (!px[i] || (done && done.has(i))) return;
      if (done) done.add(i);
      px[i] = amount >= 0 ? mix(px[i], '#ffffff', amount) : mix(px[i], '#000000', -amount);
    }, seed);
  }
  // blur a whole frame: `passes` of a 3x3 average (empty pixels count as the background, and stay
  // empty where the result is the background). With keepEmpty, empty pixels aren't touched.
  function blurAll(px, passes = 1, background = '#000000', keepEmpty = false) {
    const bgc = parse(background), n = W * H;
    let cur = new Float32Array(n * 3);
    px.forEach((c, i) => { const v = c ? parse(c) : bgc; cur[i * 3] = v[0]; cur[i * 3 + 1] = v[1]; cur[i * 3 + 2] = v[2]; });
    for (let p = 0; p < passes; p++) {
      const next = new Float32Array(n * 3);
      for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
        let r = 0, g = 0, b = 0, k = 0;
        for (let dy = -1; dy <= 1; dy++) { const yy = y + dy; if (yy < 0 || yy >= H) continue;
          for (let dx = -1; dx <= 1; dx++) { const xx = x + dx; if (xx < 0 || xx >= W) continue;
            const j = (yy * W + xx) * 3; r += cur[j]; g += cur[j + 1]; b += cur[j + 2]; k++; } }
        const i = (y * W + x) * 3; next[i] = r / k; next[i + 1] = g / k; next[i + 2] = b / k;
      }
      cur = next;
    }
    for (let i = 0; i < n; i++) {
      if (keepEmpty && !px[i]) continue;
      const out = hex(cur[i * 3], cur[i * 3 + 1], cur[i * 3 + 2]);
      px[i] = out === hex(...bgc) ? null : out;
    }
  }
  // add light: each pixel of `src` brightens `dst` by its colour times k (an empty pixel adds nothing)
  function addLight(dst, src, k = 1, background = '#000000') {
    for (let i = 0; i < dst.length; i++) {
      if (!src[i]) continue;
      const a = parse(dst[i] || background), b = parse(src[i]);
      dst[i] = hex(a[0] + b[0] * k, a[1] + b[1] * k, a[2] + b[2] * k);
    }
  }
  // lay `src` over `dst`: drawn pixels replace, empty ones let `dst` show through
  function over(dst, src) { for (let i = 0; i < dst.length; i++) if (src[i]) dst[i] = src[i]; }
  // the glow brush: light of `colour` that fades from the centre out over `size` + 1 pixels. It only ever adds
  // light (a "screen" blend), so a drawing under it gets brighter, never tinted or darker. `stroke` keeps the
  // frame as it was and the strongest glow each pixel has had, so going over a spot never piles it up.
  function glowAt(px, x, y, size, colour, strength, background, stroke, below = null) {
    const r = size + 1, light = parse(colour);
    for (let dy = -r; dy <= r; dy++) for (let dx = -r; dx <= r; dx++) {
      const a = x + dx, b = y + dy, d = Math.hypot(dx, dy);
      if (!inside(a, b) || d > r + 0.5) continue;
      const i = b * W + a, k = strength * Math.max(0, 1 - d / (r + 1)) ** 1.3;
      if (!stroke.base.has(i)) stroke.base.set(i, px[i]);
      if (k <= (stroke.k.get(i) || 0)) continue;
      stroke.k.set(i, k);
      const was = stroke.base.get(i), under = parse(was || (below && below[i]) || background);
      const out = hex(...under.map((v, j) => 255 - (255 - v) * (255 - light[j] * k) / 255));
      px[i] = !was && out === hex(...under) ? null : out;   // too faint to show: stays empty
    }
  }
  // add a soft glow of `colour` around every lit pixel that's brighter than `threshold` (0..255)
  function glowAll(px, radius = 2, strength = 0.5, threshold = 160) {
    const lit = []; px.forEach((c, i) => { if (c && Math.max(...parse(c)) >= threshold) lit.push([i % W, Math.floor(i / W), c]); });
    const add = new Map();
    lit.forEach(([x, y, c]) => { for (let dy = -radius; dy <= radius; dy++) for (let dx = -radius; dx <= radius; dx++) {
      const a = x + dx, b = y + dy, d = Math.hypot(dx, dy); if (!d || d > radius + 0.3 || !inside(a, b)) continue;
      const k = strength * (1 - d / (radius + 1)), i = b * W + a, prev = add.get(i);
      if (!prev || prev[1] < k) add.set(i, [c, k]); } });
    add.forEach(([c, k], i) => { px[i] = mix(px[i] || '#000000', c, k); });
  }

  // -- whole-frame helpers ----------------------------------------------------------------
  function replaceColour(px, from, to) { for (let i = 0; i < px.length; i++) if (px[i] === from) px[i] = to; }
  function flipped(px, horizontal) {
    const out = new Array(W * H).fill(null);
    px.forEach((c, i) => { const x = i % W, y = Math.floor(i / W); out[horizontal ? y * W + (W - 1 - x) : (H - 1 - y) * W + x] = c; });
    return out;
  }
  function fill(px, x, y, c) {
    const target = px[y * W + x]; if (target === c) return;
    const stack = [[x, y]];
    while (stack.length) { const [a, b] = stack.pop();
      if (!inside(a, b) || px[b * W + a] !== target) continue;
      px[b * W + a] = c; stack.push([a + 1, b], [a - 1, b], [a, b + 1], [a, b - 1]); }
  }

  function shifted(px, dx, dy) {   // the whole drawing moved; what goes off the edge is dropped
    const out = new Array(W * H).fill(null);
    px.forEach((c, i) => { const x = i % W + dx, y = Math.floor(i / W) + dy; if (c && inside(x, y)) out[y * W + x] = c; });
    return out;
  }

  // -- canvas size ----------------------------------------------------------------------------
  function size(w, h) { W = w; H = h; api.W = w; api.H = h; }
  // a frame of w0 x h0 on a canvas of w1 x h1: the top-left corner stays put, what doesn't fit is dropped
  function resized(px, w0, h0, w1, h1) {
    const out = new Array(w1 * h1).fill(null);
    for (let y = 0; y < Math.min(h0, h1); y++) for (let x = 0; x < Math.min(w0, w1); x++) out[y * w1 + x] = px[y * w0 + x];
    return out;
  }

  // -- areas: what the selection tool lifts, moves, turns and puts back ---------------------------
  // an area is { x, y, w, h }; a piece is { px, w, h } (its own little frame). Either may have a `mask`: w*h of
  // 1 or 0, which pixels of the box are in it (picked by colour); without one the whole box is
  const inMask = (r, a, b) => !r.mask || r.mask[b * r.w + a];
  function lifted(px, r) {
    const piece = { px: new Array(r.w * r.h).fill(null), w: r.w, h: r.h, ...(r.mask ? { mask: r.mask.slice() } : {}) };
    for (let b = 0; b < r.h; b++) for (let a = 0; a < r.w; a++) if (inside(r.x + a, r.y + b) && inMask(r, a, b)) piece.px[b * r.w + a] = px[(r.y + b) * W + r.x + a];
    return piece;
  }
  function cleared(px, r) {
    const out = px.slice();
    for (let b = 0; b < r.h; b++) for (let a = 0; a < r.w; a++) if (inside(r.x + a, r.y + b) && inMask(r, a, b)) out[(r.y + b) * W + r.x + a] = null;
    return out;
  }
  function placed(px, piece, x, y) {   // drawn pixels of the piece replace what's there; its empty ones change nothing
    const out = px.slice();
    piece.px.forEach((c, i) => { const a = x + i % piece.w, b = y + Math.floor(i / piece.w); if (c && inside(a, b)) out[b * W + a] = c; });
    return out;
  }
  // a new w x h piece whose pixel (x, y) is the old one's pixel from(x, y), its mask moved the same way
  function remap(piece, w, h, from) {
    const out = { px: new Array(w * h).fill(null), w, h, ...(piece.mask ? { mask: new Array(w * h).fill(0) } : {}) };
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) { const j = from(x, y); out.px[y * w + x] = piece.px[j]; if (out.mask) out.mask[y * w + x] = piece.mask[j]; }
    return out;
  }
  const turned = (piece) => remap(piece, piece.h, piece.w, (x, y) => (piece.h - 1 - x) * piece.w + y);   // a quarter turn clockwise
  const mirrored = (piece, horizontal) => remap(piece, piece.w, piece.h, (x, y) => (horizontal ? y * piece.w + piece.w - 1 - x : (piece.h - 1 - y) * piece.w + x));
  // stretched or shrunk to w x h, nearest pixel (each takes the old pixel under its middle), so it stays crisp:
  // twice the size is every pixel doubled, and half of that is the original again
  const scaled = (piece, w, h) => remap(piece, w, h, (x, y) => Math.floor((y + 0.5) * piece.h / h) * piece.w + Math.floor((x + 0.5) * piece.w / w));

  // -- layers flattened into the picture ----------------------------------------------------------
  // cels: one frame's pixels per layer, bottom first. A layer is { on, opacity (0..1), mode }: 'normal' lays
  // its pixels over what's beneath (see-through by its opacity), 'light' adds them as light. Python does the
  // same sums in bongo/animator.py (flatten), so a sprite looks the same on the panel as it does here.
  function compose(cels, layers, background = '#000000', upTo = cels.length) {
    const out = new Array(cels[0] ? cels[0].length : W * H).fill(null), bgc = parse(background);
    for (let k = 0; k < upTo; k++) {
      const l = layers[k], cel = cels[k], o = l.opacity ?? 1;
      if (!l.on || o <= 0) continue;
      if (o >= 1 && l.mode !== 'light') { for (let i = 0; i < cel.length; i++) if (cel[i]) out[i] = cel[i]; continue; }
      for (let i = 0; i < cel.length; i++) {
        if (!cel[i]) continue;
        const a = out[i] ? parse(out[i]) : bgc, b = parse(cel[i]);
        const c = l.mode === 'light' ? hex(a[0] + b[0] * o, a[1] + b[1] * o, a[2] + b[2] * o)
                                     : hex(a[0] + (b[0] - a[0]) * o, a[1] + (b[1] - a[1]) * o, a[2] + (b[2] - a[2]) * o);
        if (out[i] || c !== background.toLowerCase()) out[i] = c;   // too faint to show on nothing: stays empty
      }
    }
    return out;
  }

  // -- text ---------------------------------------------------------------------------------------
  // a font is { cap, descent, glyphs: { 'A': [advance, x offset, rows above the baseline, width, [row bits]] } }:
  // what /api/animator/fonts/<name> sends for each of the Matrix's fonts. Tiny (3 x 5) is built in.
  const ROWS = {
    A: '.X.|X.X|XXX|X.X|X.X', B: 'XX.|X.X|XX.|X.X|XX.', C: '.XX|X..|X..|X..|.XX', D: 'XX.|X.X|X.X|X.X|XX.', E: 'XXX|X..|XX.|X..|XXX',
    F: 'XXX|X..|XX.|X..|X..', G: '.XX|X..|X.X|X.X|.XX', H: 'X.X|X.X|XXX|X.X|X.X', I: 'XXX|.X.|.X.|.X.|XXX', J: '..X|..X|..X|X.X|.X.',
    K: 'X.X|X.X|XX.|X.X|X.X', L: 'X..|X..|X..|X..|XXX', M: 'X...X|XX.XX|X.X.X|X...X|X...X', N: 'X..X|XX.X|X.XX|X..X|X..X',
    O: '.XX.|X..X|X..X|X..X|.XX.', P: 'XX.|X.X|XX.|X..|X..', Q: '.X.|X.X|X.X|X.X|.XX', R: 'XX.|X.X|XX.|X.X|X.X', S: '.XX|X..|.X.|..X|XX.',
    T: 'XXX|.X.|.X.|.X.|.X.', U: 'X.X|X.X|X.X|X.X|XXX', V: 'X.X|X.X|X.X|X.X|.X.', W: 'X...X|X...X|X.X.X|XX.XX|X...X', X: 'X.X|X.X|.X.|X.X|X.X',
    Y: 'X.X|X.X|.X.|.X.|.X.', Z: 'XXX|..X|.X.|X..|XXX', 0: 'XXX|X.X|X.X|X.X|XXX', 1: '.X.|XX.|.X.|.X.|XXX', 2: 'XX.|..X|.X.|X..|XXX',
    3: 'XX.|..X|.X.|..X|XX.', 4: 'X.X|X.X|XXX|..X|..X', 5: 'XXX|X..|XX.|..X|XX.', 6: '.XX|X..|XXX|X.X|XXX', 7: 'XXX|..X|.X.|.X.|.X.',
    8: 'XXX|X.X|XXX|X.X|XXX', 9: 'XXX|X.X|XXX|..X|XX.', ' ': '..|..|..|..|..', '!': 'X|X|X|.|X', '.': '.|.|.|.|X', "'": 'X|X|.|.|.',
    '-': '...|...|XXX|...|...', ':': '.|X|.|X|.', '?': 'XX.|..X|.X.|...|.X.', '%': 'XX..X|XX.X.|..X..|.X.XX|X..XX', '♥': 'X.X|XXX|XXX|.X.|...',
  };
  const FONT = {};   // the Tiny font as rows of X and . (the presets draw with these)
  Object.entries(ROWS).forEach(([k, v]) => { FONT[k] = v.split('|'); });
  const TINY = { cap: 5, descent: 0, upper: true, glyphs: {} };
  Object.entries(FONT).forEach(([k, rows]) => { TINY.glyphs[k] = [rows[0].length + 1, 0, 5, rows[0].length, rows.map((r) => parseInt(r.replace(/X/g, '1').replace(/\./g, '0'), 2))]; });
  const glyph = (font, ch) => font.glyphs[font.upper ? ch.toUpperCase() : ch] || font.glyphs['?'] || [0, 0, 0, 0, []];
  const textWidth = (s, font = TINY) => [...s].reduce((w, ch) => w + glyph(font, ch)[0], 0);
  // the columns s actually lights, from where it starts: { left, right }, or null if it lights none (spaces).
  // Not the advance widths: a letter's spacing after it (or a blank column in its bitmap) isn't drawn
  function textInk(s, font = TINY) {
    let x = 0, left = Infinity, right = -Infinity;
    for (const ch of s) {
      const [advance, xoff, , w, rows] = glyph(font, ch), bits = rows.reduce((a, r) => a | r, 0);
      for (let a = 0; a < w; a++) if (bits >> (w - 1 - a) & 1) { left = Math.min(left, x + xoff + a); right = Math.max(right, x + xoff + a); }
      x += advance;
    }
    return left <= right ? { left, right } : null;
  }
  // write s with the top of its capitals at (x, y); `set(i)` gets each pixel. Returns how wide it came out.
  function text(s, x, y, font, set) {
    const start = x, base = y + font.cap;
    for (const ch of s) {
      const [advance, xoff, top, w, rows] = glyph(font, ch);
      rows.forEach((bits, r) => { for (let a = 0; a < w; a++) if (bits >> (w - 1 - a) & 1) { const X = x + xoff + a, Y = base - top + r; if (inside(X, Y)) set(Y * W + X); } });
      x += advance;
    }
    return x - start;
  }

  // -- how it looks dimmed ------------------------------------------------------------------------
  // a colour as the panel shows it at `percent` brightness, brought back up so the lost steps are easy to
  // see: at 15% there are only 38 levels of each colour left, so soft dark gradients turn into bands
  function dimmed(c, percent) {
    if (percent >= 100) return c;
    const k = percent / 100;
    return hex(...parse(c).map((v) => Math.round(v * k) / k));
  }

  const api = { W, H, size, resized, hex, parse, mix, SHAPES, SHAPE_NAMES, tip, stamp, lineCells, rectCells, ellipseCells,
    GRADIENT_TYPES, GRADIENT_STYLES, GRADIENT_AREAS, gradient, stopColour, blurAt, shadeAt, glowAt, blurAll, glowAll, addLight, over,
    replaceColour, flipped, fill, shifted, lifted, cleared, placed, turned, mirrored, scaled, compose, FONT, TINY, text, textWidth, textInk, dimmed };
  window.PixelTools = api;
})();
