// Animator presets: ready-made animations to start from (Animator > Presets). Each one is drawn here
// in code, frame by frame, on the 64 x 32 panel, with the same tools the editor has (js/animator-tools.js:
// gradients, blur, glows, ovals, brushes, lighten and darken). v2's starter templates are remastered here
// (the ideas only: v2's code isn't used) alongside many more. A preset is { id, name, group, make() -> frames },
// where a frame is { px: Array(64*32) of '#rrggbb' or null (nothing drawn), ms }. Looping presets close
// their loop: the frame after the last is the first again (tests/test_web_static.py checks it).
(function () {
  const T = window.PixelTools;
  let W = 64, H = 32;   // the canvas being drawn on: set from PixelTools at the start of every make() (see P), so a preset draws at any size
  const TAU = Math.PI * 2;

  // -- a little drawing kit -------------------------------------------------------------------
  const { hex, parse, mix } = T;
  const dim = (c, k) => hex(...parse(c).map((v) => v * k));
  function hsv(h, s, v) {   // h in turns (0..1)
    h = ((h % 1) + 1) % 1; const i = Math.floor(h * 6), f = h * 6 - i;
    const p = v * (1 - s), q = v * (1 - f * s), t = v * (1 - (1 - f) * s);
    const [r, g, b] = [[v, t, p], [q, v, p], [p, v, t], [p, q, v], [t, p, v], [v, p, q]][i % 6];
    return hex(r * 255, g * 255, b * 255);
  }
  function rng(seed) { let s = (seed * 7919 + 12345) % 2147483647 || 1; const r = () => (s = (s * 16807) % 2147483647) / 2147483647; r(); return r; }
  const tri = (u) => { u = ((u % 1) + 1) % 1; return u < 0.5 ? u * 2 : 2 - u * 2; };   // 0 -> 1 -> 0 over one period
  const wave = (i, n, k = 1, ph = 0) => Math.sin(TAU * k * i / n + ph);                  // repeats exactly every n frames
  const RAINBOW = ['#ff3b30', '#ff8a3d', '#ffe066', '#4ade80', '#2ec4ff', '#3b6bff', '#8a5cff'];

  class Frame {
    constructor(ms = 90) { this.px = new Array(W * H).fill(null); this.ms = ms; }
    set(x, y, c) { x = Math.round(x); y = Math.round(y); if (x >= 0 && x < W && y >= 0 && y < H && c) this.px[y * W + x] = c; }
    get(x, y) { return x >= 0 && x < W && y >= 0 && y < H ? this.px[y * W + x] : null; }
    rect(x0, y0, w, h, c) { x0 = Math.round(x0); y0 = Math.round(y0); for (let y = y0; y < y0 + h; y++) for (let x = x0; x < x0 + w; x++) this.set(x, y, c); }
    disc(cx, cy, r, c, ry = r) { for (let y = Math.floor(cy - ry); y <= cy + ry; y++) for (let x = Math.floor(cx - r); x <= cx + r; x++) if (((x - cx) / r) ** 2 + ((y - cy) / ry) ** 2 <= 1) this.set(x, y, c); }
    line(x0, y0, x1, y1, c) { T.lineCells(Math.round(x0), Math.round(y0), Math.round(x1), Math.round(y1)).forEach(([x, y]) => this.set(x, y, c)); }
    sprite(rows, x0, y0, colours, k = 1) {   // rows of characters; colours maps a character to a colour
      x0 = Math.round(x0); y0 = Math.round(y0);
      rows.forEach((row, y) => [...row].forEach((ch, x) => { const c = colours[ch]; if (c) for (let a = 0; a < k; a++) for (let b = 0; b < k; b++) this.set(x0 + x * k + a, y0 + y * k + b, c); }));
    }
    text(s, x0, y0, c, k = 1) { let x = Math.round(x0); for (const ch of s.toUpperCase()) { const g = FONT[ch] || FONT[' ']; this.sprite(g, x, y0, { X: c }, k); x += (g[0].length + 1) * k; } return x; }
    // the editor's tools
    oval(x0, y0, x1, y1, c, filled = true) { T.ellipseCells([Math.round(x0), Math.round(y0)], [Math.round(x1), Math.round(y1)], filled).forEach(([x, y]) => this.set(x, y, c)); }
    stroke(x0, y0, x1, y1, c, size = 2, shape = 'round') {
      T.lineCells(Math.round(x0), Math.round(y0), Math.round(x1), Math.round(y1)).forEach(([x, y]) => T.stamp(x, y, size, shape, (i) => { this.px[i] = c; }));
    }
    grad(a, b, stops, opts = {}) { T.gradient(this.px, a, b, stops, opts); }
    sky(stops, y0 = 0, y1 = H - 1) { for (let y = y0; y <= y1; y++) { const c = T.stopColour(stops, y1 > y0 ? (y - y0) / (y1 - y0) : 0); for (let x = 0; x < W; x++) this.px[y * W + x] = c; } }   // rows y0..y1 shaded top to bottom
    blur(passes = 1, keepEmpty = false) { T.blurAll(this.px, passes, '#000000', keepEmpty); }
    shade(x, y, size, amount, shape = 'round') { T.shadeAt(this.px, Math.round(x), Math.round(y), size, shape, amount); }
    // draw on a layer of its own, soften it with the blur, then add it as light (glows, beams, bokeh)
    light(draw, passes = 2, k = 1) { const g = new Frame(); draw(g); if (passes) g.blur(passes); T.addLight(this.px, g.px, k); }
    // draw on a layer of its own (so gradients and blur only touch it), then lay it over the frame
    layer(draw) { const g = new Frame(); draw(g); T.over(this.px, g.px); }
  }
  const textWidth = (s, k = 1) => [...s.toUpperCase()].reduce((w, ch) => w + ((FONT[ch] || FONT[' '])[0].length + 1) * k, -k);
  // n frames from draw(frame, i, n); `frameAt(i)` draws any frame, so a test can check frame n is frame 0 again
  let lazy = false;   // set by peek(): frames() then hands back how to draw each frame, without drawing them all
  const frames = (n, ms, draw) => {
    const w = W, h = H;   // a frame drawn later (the gallery's thumbnails) is still drawn on the canvas this one was made for
    const at = (i) => { W = w; H = h; const f = new Frame(typeof ms === 'function' ? ms(i % n) : ms); draw(f, i, n); return f; };
    if (lazy) return { length: n, frameAt: at };
    const out = Array.from({ length: n }, (_, i) => at(i));
    Object.defineProperty(out, 'frameAt', { value: at });
    return out;
  };

  const FONT = T.FONT;   // the 3 x 5 Tiny font (capitals, digits and a little punctuation): js/animator-tools.js

  // -- from v2, remastered ----------------------------------------------------------------
  function blank() { return [new Frame()]; }

  function bouncingBall() {   // there and back in two bounces: a shaded ball that squashes on the floor, its shadow growing as it falls
    return frames(22, (i) => (i % 11 === 0 ? 110 : 65), (f, i, n) => {
      f.sky(['#0c1430', '#000000'], 0, 27);
      f.sky(['#2b3f8f', '#0a1030'], 28, 31);
      const p = (i % 11) / 11, land = i % 11 === 0, x = Math.round(10 + 44 * tri(i / n));
      const cy = Math.round(land ? 24.6 : 23.5 - 17 * (1 - (2 * p - 1) ** 2)) + (land ? 0.5 : 0), rx = land ? 5.2 : 4.4, ry = land ? 3.4 : 4.4;
      const sw = 2 + (cy - 6) / 4.5;                                  // the shadow: wider the nearer the ball
      f.oval(x - sw, 28, x + sw, 29, dim('#2b3f8f', 0.5));
      f.disc(x, cy, rx, '#ff7a2f', ry);
      f.grad([x - 2, Math.round(cy) - 2], [x + 4, Math.round(cy) + 4], ['#ffe39a', '#ff7a2f', '#a8360e'], { type: 'radial', area: 'region', from: [x, Math.round(cy)] });
      f.set(x - 2, cy - 2, '#fff6d8');
    });
  }

  function pixelHeart() {   // a heart that beats twice, then rests: drawn smooth at any size, shaded, with a glow on each beat
    const beat = [1, 1.18, 1.3, 1.12, 1.24, 1.08, 1, 1, 1, 1];
    return frames(beat.length, (i) => (i < 6 ? 90 : 150), (f, i) => {
      const s = beat[i % beat.length], k = (s - 1) / 0.3;
      const inHeart = (x, y) => { const u = (x - 31.5) / (8.5 * s), v = -(y - 15) / (8 * s) + 0.25; return (u * u + v * v - 1) ** 3 - u * u * v ** 3 <= 0; };
      if (k > 0.3) f.light((g) => { for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) if (inHeart(x, y)) g.set(x, y, dim('#ff2f5a', 0.45 * k)); }, 3);
      for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) if (inHeart(x, y)) f.set(x, y, '#ff2f5a');
      f.grad([26, 9], [40, 26], ['#ffb0c2', '#ff2f5a', '#a0102e'], { type: 'radial', area: 'region', from: [31, 16] });
    });
  }

  function starfield() {   // flying through stars: they stream out from the middle, brightening and glowing as they near
    const r = rng(11), st = Array.from({ length: 70 }, () => [r() * 2 - 1, r() * 2 - 1, r()]);
    return frames(24, 70, (f, i, n) => {
      f.light((g) => st.forEach(([sx, sy, z0]) => {
        const z = 1 - ((z0 + i / n) % 1); if (z > 0.3) return;
        const k = 0.18 / Math.max(z, 0.06); g.set(W / 2 + sx * W * 1.5 * k, H / 2 + sy * H * 1.5 * k, dim('#9ab8ff', 0.8));
      }), 1);
      st.forEach(([sx, sy, z0]) => {
        const z = 1 - ((z0 + i / n) % 1), k = 0.18 / Math.max(z, 0.06);
        const x = W / 2 + sx * W * 1.5 * k, y = H / 2 + sy * H * 1.5 * k, b = Math.min(1, (1 - z) * 1.4);
        f.set(x, y, hex(150 + 105 * b, 160 + 95 * b, 200 + 55 * b));
        if (z < 0.25) f.set(x - Math.sign(sx), y - Math.sign(sy), dim('#c8d6ff', b * 0.6));   // the streak trails behind
      });
    });
  }

  function digitalRain() {   // falling code: glowing heads fading to deep green, every column back where it began after 48 frames
    const r = rng(5), span = H + 16, cols = Array.from({ length: Math.round(W * 3 / 8) }, () => [Math.floor(r() * W), Math.floor(r() * span), 1 + Math.floor(r() * 2), 5 + Math.floor(r() * 9)]);
    return frames(span, 70, (f, i) => {
      const heads = [];
      cols.forEach(([x, y0, v, len]) => {
        const head = (y0 + i * v) % span;
        for (let t = 0; t < len; t++) { const y = head - t; if (y < 0 || y >= H) continue;
          f.set(x, y, t === 0 ? '#e8ffe8' : t < 2 ? '#6dff8a' : mix('#22b04a', '#062a12', t / len)); }
        if (head < H) heads.push([x, head]);
      });
      f.light((g) => heads.forEach(([x, y]) => g.set(x, y, '#3aff6a')), 1, 0.9);
    });
  }

  function fireworks() {   // rockets climb with a trail and burst into glowing sparks that fall and fade; bursts carry over the loop
    const shells = [[14, 8, 0, 0.02], [44, 6, 10, 0.6], [30, 11, 20, 0.35], [46, 12, 30, 0.85], [17, 10, 38, 0.15]];
    return frames(48, 70, (f, i, n) => {
      f.sky(['#000000', '#0a0e24'], 0, 31);
      const sparks = [];
      shells.forEach(([x, top, start, hue]) => {
        const t = ((i - start) % n + n) % n;
        if (t < 6) { const y = 31 - (31 - top) * t / 6; f.set(x, y, '#fff2c0'); f.set(x, y + 1, '#ffb04a'); f.set(x, y + 2, '#7a3a10'); return; }
        const age = t - 6; if (age > 13) return;
        const fade = Math.pow(1 - age / 13, 0.6);
        for (let a = 0; a < 24; a++) {
          const ang = a / 24 * TAU, d = 2 + age * 1.1 * (0.2 + 0.8 * rng(a * 7 + start + 1)()), at = (dd, ag) => [x + Math.cos(ang) * dd, top + Math.sin(ang) * dd * 0.8 + ag * ag * 0.05];
          if (fade < 0.12) continue;                                    // spent: gone, never darker than the sky
          sparks.push([...at(d, age), hsv(hue + (a % 2) * 0.06, 0.75, fade)]);
          if (age > 0 && fade > 0.3) sparks.push([...at(d - 1.1, age - 1), hsv(hue, 0.6, fade * 0.4)]);
          if (age < 4) sparks.push([x + Math.cos(ang) * d * 0.5, top + Math.sin(ang) * d * 0.4, '#fffbe8']);
        }
        if (age < 4) for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) sparks.push([x + dx, top + dy, '#fffbe8']);   // a bright heart
      });
      f.light((g) => sparks.forEach(([x, y, c]) => g.set(x, y, c)), 1, 1.4);
      sparks.forEach(([x, y, c]) => f.set(x, y, c));
      for (let x = 0; x < W; x++) f.set(x, 31, '#141826');
    });
  }

  // -- scenes -----------------------------------------------------------------------------
  function oceanWaves() {   // a sunset sea: waves rolling in towards you, the sun glowing on the horizon, gulls drifting
    return frames(24, 90, (f, i, n) => {
      f.sky(['#1a1040', '#6a2a6a', '#ff8a5a'], 0, 15);
      f.light((g) => g.disc(32, 15, 6.5, '#ff9a4a'), 3);
      f.disc(32, 15, 6.5, '#ffd27a');
      for (let y = 16; y < H; y++) for (let x = 0; x < W; x++) {
        const crest = Math.sin((y - 16) * 1.1 - TAU * 2 * i / n + Math.sin(x / 7) * 0.7) > 0.8;
        const glint = crest && Math.abs(x - 32) < 8 - (y - 16) * 0.3;
        f.set(x, y, glint ? '#ffd27a' : crest ? mix('#7ab6e6', '#3a6aa8', (y - 16) / 16) : mix('#245a9a', '#0b1d3a', (y - 16) / 15));
      }
      [[12, 5], [46, 3]].forEach(([gx, gy], k) => { const x = gx + 3 * wave(i, n, 1, k * 2), up = (i + k * 3) % 6 < 3;
        f.set(x - 1, gy + (up ? 0 : 1), '#1a1020'); f.set(x, gy + 1, '#1a1020'); f.set(x + 1, gy + (up ? 0 : 1), '#1a1020'); });
    });
  }

  function snowfall() {   // snow drifting down over a little house, its window glowing warm on the snow, smoke curling up
    const r = rng(17), speeds = [0.5, 1, 1.5];
    const flakes = Array.from({ length: 45 }, (_, k) => [r() * W, r() * H, speeds[k % 3], r() * 6]);
    return frames(64, 90, (f, i, n) => {
      const skyAt = (y) => mix('#0e1638', '#2a3a6a', y / 31);
      f.sky(['#0e1638', '#2a3a6a']);
      for (let x = 0; x < W; x++) for (let y = 27 + Math.round(Math.sin(x / 9)); y < H; y++) f.set(x, y, mix('#dfe8f5', '#9aaac8', (y - 26) / 6));
      f.rect(38, 18, 14, 9, '#5a3a2a');
      for (let y = 12; y < 18; y++) f.line(45 - (y - 11), y, 45 + (y - 11), y, '#7a2a2a');
      f.stroke(37, 18, 45, 10, '#e6eef8', 2); f.stroke(45, 10, 53, 18, '#e6eef8', 2);
      f.rect(50, 12, 2, 4, '#3a2a22');
      const lit = 0.85 + 0.15 * wave(i, n, 4);
      f.light((g) => { g.rect(42, 21, 3, 3, dim('#ffb84a', lit)); g.oval(39, 27, 47, 29, dim('#a86a20', lit)); }, 2, 0.8);
      f.rect(42, 21, 3, 3, mix('#ffb84a', '#ffd27a', lit)); f.rect(48, 21, 3, 5, '#2a1a12');
      for (let k = 0; k < 5; k++) { const p = ((i / n) * 2 + k / 5) % 1, x = 51 + Math.sin(p * 6 + k) * 1.5, y = 11 - p * 10; f.set(x, y, mix('#b8c4d8', skyAt(y), p)); }
      flakes.forEach(([x0, y0, v, ph]) => { const big = v === 1.5;
        const x = (x0 + Math.sin(TAU * i / n * 2 + ph) * 1.5 + W) % W, y = (y0 + i * v) % H;
        f.set(x, y, big ? '#ffffff' : '#c8d4ea'); if (big) f.set(x + 1, y, '#9aaac8'); });
    });
  }

  function campfire() {   // a crackling fire under the stars, its glow lighting the logs and the ground, sparks rising
    const r = rng(23), stars = Array.from({ length: 18 }, () => [Math.floor(r() * W), Math.floor(r() * 16)]);
    return frames(16, 80, (f, i, n) => {
      f.sky(['#05070f', '#1a1222'], 0, 24);
      f.sky(['#20160f', '#120c08'], 25, 31);
      stars.forEach(([x, y], k) => f.set(x, y, (k * 5 + i) % 16 < 2 ? '#ffffff' : '#5a6688'));
      const fr = rng(100 + i % n), flame = new Frame();
      for (let y = 10; y < 28; y++) {
        const half = Math.max(0, (y - 10) * 0.55 - (y > 24 ? (y - 24) * 1.5 : 0)) + fr() * 1.6;
        for (let x = Math.round(32 - half); x <= 32 + half; x++) {
          const heat = 1 - Math.abs(x - 32) / (half + 1) * 0.7 - (28 - y) / 26;
          flame.set(x + Math.round(Math.sin(y / 3 + TAU * 3 * i / n) * 0.8), y, heat > 0.55 ? '#fff2a0' : heat > 0.3 ? '#ffb43a' : heat > 0.05 ? '#ff5a1e' : '#a8200e');
        }
      }
      const k = 0.8 + 0.2 * wave(i, n, 3);
      f.light((g) => { g.oval(14, 22, 50, 31, dim('#a8400e', k)); g.oval(24, 12, 40, 28, dim('#ff6a1e', k * 0.5)); }, 3, 0.7);
      f.stroke(22, 29, 42, 25, '#6a3e22', 2); f.stroke(22, 25, 42, 29, '#5a321c', 2);
      f.shade(24, 26, 3, 0.25); f.shade(40, 26, 3, 0.25);
      T.over(f.px, flame.px);
      for (let s = 0; s < 4; s++) { const p = ((i / n) + s / 4) % 1; f.set(32 + Math.sin(p * 9 + s * 2) * 4, 12 - p * 12, dim('#ffb43a', 1 - p)); }
    });
  }

  function aquarium() {   // fish swimming through sunbeams past swaying weed, bubbles rising
    const fishes = [[0, 8, '#ff8a3d', 1, 2], [40, 15, '#ffe066', -1, 1], [20, 22, '#4ad0ff', 1, 3]];   // x, y, colour, way, crossings per loop
    return frames(120, 90, (f, i, n) => {
      f.sky(['#2a8ac8', '#1a6aa8', '#06223f']);
      f.light((g) => [8, 26, 44].forEach((x0, k) => { const x = x0 + 3 * wave(i, n, 1, k); g.stroke(x, 0, x - 10, 28, dim('#3a8ab8', 0.5), 3); }), 2, 0.6);
      for (let x = 0; x < W; x++) for (let y = 29; y < H; y++) f.set(x, y, (x * 7 + y) % 5 ? '#c9a86a' : '#a8875a');
      f.oval(10, 27, 18, 30, '#6a6a7a'); f.shade(12, 28, 2, 0.3);
      [[6, 0], [52, 2], [58, 1]].forEach(([x, ph]) => { for (let y = 12; y < 29; y++) f.set(x + Math.sin(y / 3 + TAU * i / n + ph) * 1.4, y, y % 3 ? '#2f9a4a' : '#45c060'); });
      fishes.forEach(([x0, y, c, dir, laps]) => {
        const p = (x0 / 76 + laps * i / n) % 1, x = Math.round(dir > 0 ? -6 + 76 * p : W + 6 - 76 * p), tail = Math.floor(i / 3) % 2;
        f.oval(x - 3, y - 2, x + 3, y + 2, c); f.shade(x, y + 2, 3, -0.3); f.shade(x, y - 1, 2, 0.25);
        f.set(x + 2 * dir, y - 1, '#101820');
        f.set(x - 4 * dir, y - 1 + tail, c); f.set(x - 4 * dir, y + 1 - tail, c); f.set(x - 3 * dir, y, c);
      });
      for (let k = 0; k < 6; k++) { const p = ((i / n) * 5 + k / 6) % 1; f.set(46 + (k % 3) * 2 + Math.sin(p * 8), 28 - p * 28, '#bfe9ff'); }
    });
  }

  function rocketLaunch() {   // 3, 2, 1, ignition: lift-off on a glowing plume, the smoke billowing out
    return frames(30, (i) => (i < 3 ? 800 : 70), (f, i) => {
      const lift = Math.max(0, i - 5), y = Math.round(15 - lift * lift * 0.12);
      f.sky(['#03050c', mix('#18264a', '#03050c', Math.min(1, lift / 25))]);
      [[5, 4], [58, 7], [20, 2], [44, 10], [12, 14], [52, 18]].forEach(([x, s]) => f.set(x, (s + lift * 0.8) % H, '#c8d2ff'));
      if (i >= 3) {   // each puff billows out and up from the pad, then the smoke clears before the loop
        const smoke = new Frame(), age = i - 3, clear = Math.min(1, (29 - i) / 6);
        for (let k = 0; k < 14; k++) { const r = rng(k + 1), side = r() < 0.5 ? -1 : 1, v = 0.4 + r(), lift = r();
          const reach = 8 + 12 * r(), sx = 32 + side * Math.min(reach, age * v * 1.4), sy = 28.5 - lift * 2 - age * 0.35 * (0.5 + lift), rad = Math.min(6, 1.2 + age * 0.2);
          if (clear > 0) smoke.disc(sx, sy, rad, mix('#03050c', mix('#9aa1ac', '#5a606a', r()), clear * Math.max(0.3, 1 - age / 40))); }
        smoke.blur(1, true); T.over(f.px, smoke.px);
      }
      if (y > -16) {
        if (i >= 3) {
          const plume = 6 + Math.min(lift, 8);
          f.light((g) => g.oval(29, y + 11, 35, y + 12 + plume, '#ff8a3a'), 2);
          for (let k = 0; k < plume; k++) { const r = rng(i * 13 + k); f.set(30 + r() * 4, y + 12 + k, k < 2 ? '#fff2a0' : k < 5 ? '#ffb43a' : '#ff5a1e'); }
        }
        f.layer((g) => { g.rect(29, y, 6, 12, '#e8ecf2'); g.grad([29, y], [35, y], ['#ffffff', '#9aa1b8'], { area: 'drawn' }); });
        f.rect(29, y, 6, 2, '#ff4a3a'); f.set(31, y - 1, '#ff4a3a'); f.set(32, y - 1, '#ff4a3a'); f.set(31, y - 2, '#ff4a3a'); f.set(32, y - 2, '#ff4a3a');
        f.disc(31.5, y + 5, 1.3, '#4ad0ff'); f.rect(27, y + 9, 2, 3, '#ff4a3a'); f.rect(35, y + 9, 2, 3, '#ff4a3a');
      }
      if (i < 8) for (let x = 20; x < 44; x++) f.set(x, 27, '#3a4050');
      if (i < 3) { const d = String(3 - i); f.text(d, 10, 8, '#ffb547', 3); }
    });
  }

  function lavaLamp() {   // warm glowing blobs rising, merging and sinking in a shaded glass
    const blobs = [[0, 0.0, 3.5], [3, 0.35, 2.8], [-3, 0.7, 3.0], [1, 0.55, 2.2]];
    return frames(36, 90, (f, i, n) => {
      const glass = (x, y) => Math.abs(x - 32) < 7 + y * 0.12 && y > 1 && y < 29;
      const hot = [];
      for (let y = 0; y < H; y++) for (let x = 20; x < 44; x++) {
        if (!glass(x, y)) continue;
        let field = 0;
        blobs.forEach(([dx, ph, r]) => { const by = 4 + 22 * (0.5 - 0.5 * Math.cos((i / n + ph) * TAU)); field += r * r / ((x - 32 - dx - Math.sin(TAU * i / n + ph * 9)) ** 2 + (y - by) ** 2 + 0.5); });
        f.set(x, y, field > 1 ? (field > 2.2 ? '#ffd06a' : '#ff6a3a') : mix('#3a0a3a', '#8a1a4a', y / 31));
        if (field > 1) hot.push([x, y]);
      }
      f.light((g) => hot.forEach(([x, y]) => g.set(x, y, '#5a1a10')), 2);
      for (let y = 2; y < 29; y++) { const e = Math.round(7 + y * 0.12); f.shade(32 - e + 1, y, 1, 0.18); }
      f.rect(25, 29, 15, 3, '#9aa1ac'); f.rect(27, 0, 11, 2, '#9aa1ac'); f.grad([25, 0], [40, 0], ['#d0d6e0', '#5a606a'], { area: 'region', from: [30, 30] });
    });
  }

  function aurora() {   // soft northern lights rippling over a black ridge of trees
    return frames(32, 90, (f, i, n) => {
      f.sky(['#02040c', '#0a1530'], 0, 24);
      f.light((g) => { for (let y = 0; y < 24; y++) for (let x = 0; x < W; x++) {
        const band = wave(x, 44, 1, TAU * i / n) * 3 + 9 + Math.sin(x / 3.1 - TAU * 2 * i / n) * 1.2;
        const k = Math.max(0, 1 - Math.abs(y - band) / 5) * (0.6 + 0.4 * Math.sin(x / 4 + TAU * 3 * i / n));
        if (k > 0) g.set(x, y, dim(hsv(0.38 + 0.12 * Math.sin(x / 13 + TAU * i / n), 0.7, 1), k));
      } }, 1);
      [3, 17, 30, 41, 55, 60].forEach((x, k) => f.set(x, (k * 3 + 2) % 7, (k + i) % 8 < 2 ? '#ffffff' : '#9aa6c8'));
      for (let x = 0; x < W; x++) { const top = 21 + 2 * Math.abs((x % 7) - 3) + ((x / 7 | 0) * 5 % 3); for (let y = top; y < H; y++) f.px[y * W + x] = null; }   // a ridge of pines, 7 wide
    });
  }

  function shootingStars() {   // a still night: a glowing crescent moon, twinkling stars, a shooting star streaking across
    const r = rng(41), stars = Array.from({ length: 40 }, () => [Math.floor(r() * W), Math.floor(r() * 26), r() * 6]);
    return frames(30, 90, (f, i, n) => {
      f.sky(['#020410', '#0c1838']);
      stars.forEach(([x, y, ph]) => f.set(x, y, dim('#d8e2ff', 0.35 + 0.65 * (0.5 + 0.5 * wave(i, n, 2, ph)))));
      const moon = (g, c) => { for (let y = 1; y < 12; y++) for (let x = 46; x < 58; x++) if (Math.hypot(x - 52, y - 6) <= 4.5 && Math.hypot(x - 54, y - 4.8) > 3.8) g.set(x, y, c); };
      f.light((g) => moon(g, '#5a5a48'), 2); moon(f, '#f2f0d8');
      [[4, 0], [18, 1]].forEach(([start, k]) => { const t = i - start; if (t < 0 || t > 8) return;
        const hx = 8 + k * 10 + t * 5, hy = 3 + t * 2.2;
        f.light((g) => g.stroke(hx - 10, hy - 4.4, hx, hy, '#4a5a8a', 1), 1);
        for (let s = 0; s <= 10; s++) f.set(hx - s, hy - s * 0.44, dim('#ffffff', 1 - s / 11));
      });
      for (let x = 0; x < W; x++) for (let y = 29 + Math.round(Math.sin(x / 6)); y < H; y++) f.set(x, y, '#0a1018');
    });
  }

  function lighthouse() {   // a lighthouse sweeping a soft beam over a night sea
    return frames(24, 90, (f, i, n) => {
      f.sky(['#060a1a', '#16244a'], 0, 21);
      for (let y = 22; y < H; y++) for (let x = 0; x < W; x++) f.set(x, y, (x + y * 3 + i + Math.round(2 * Math.sin(x / 5))) % 8 === 0 ? '#3a5a8a' : mix('#12244a', '#081428', (y - 22) / 9));
      const ang = TAU * i / n, bx = Math.cos(ang);
      const glow = 0.15 + 0.85 * (Math.sin(ang) + 1) / 2;   // brightest towards you, faint as it swings away
      if (Math.abs(bx) >= 0.25) f.light((g) => {
        for (let d = 3; d < 52; d++) for (let s = -d * 0.15; s <= d * 0.15; s++) g.set(14.5 + Math.sign(bx) * d * Math.min(1, Math.abs(bx) * 1.3), 7 + s, dim('#fff2b0', 0.5 * glow * (1 - d / 52)));
      }, 1);
      f.layer((g) => { g.rect(12, 9, 5, 15, '#e8e8e8'); g.grad([12, 0], [17, 0], ['#ffffff', '#8a8a96'], { area: 'drawn' }); });
      f.rect(12, 12, 5, 2, '#d23a2a'); f.rect(12, 17, 5, 2, '#d23a2a'); f.rect(16, 12, 1, 2, '#8a1a12'); f.rect(16, 17, 1, 2, '#8a1a12');
      f.rect(11, 6, 7, 3, '#2a2a33'); f.rect(13, 7, 3, 1, '#fff6c8'); f.rect(10, 24, 9, 3, '#3a3a40');
      const towards = Math.abs(bx) < 0.25 && Math.sin(ang) > 0;   // pointing at you: the lamp flashes
      f.light((g) => (towards ? g.disc(14.5, 7, 3, '#fff2b0') : g.rect(13, 7, 3, 1, '#a89a50')), towards ? 2 : 1);
    });
  }

  function rainyWindow() {   // rain running down the glass, the city's lights a soft blur behind
    const r = rng(29), lights = Array.from({ length: 22 }, () => [r() * W, 8 + r() * 22, hsv(r() < 0.3 ? 0.55 + r() * 0.1 : r() * 0.11, 0.8, 0.85), 1.2 + r() * 1.6, r() * TAU]);
    const drops = Array.from({ length: 16 }, () => [Math.floor(r() * W), Math.floor(r() * 36), 1 + Math.floor(r() * 2)]);
    return frames(36, 90, (f, i) => {
      f.sky(['#0a0e1c', '#1a1e30']);
      f.light((g) => lights.forEach(([x, y, c, s, ph]) => g.disc(x, y, s, dim(c, 0.75 + 0.25 * wave(i, 36, 1, ph)))), 2);
      drops.forEach(([x, y0, v]) => { const y = (y0 + i * v) % 36;
        f.set(x, y, '#c8d8f0'); f.set(x, y - 1, '#7a8aa8'); f.set(x, y - 2, '#3a4560'); f.shade(x, y + 1, 1, 0.2); });
      for (let x = 0; x < W; x++) f.set(x, 0, '#2a2e3a');
      for (let y = 0; y < H; y++) f.set(31, y, '#2a2e3a');
    });
  }

  // -- patterns -----------------------------------------------------------------------------
  function plasma() {   // the classic demo-scene plasma, cycling colours
    return frames(30, 70, (f, i, n) => {
      const t = TAU * i / n;
      for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
        const v = Math.sin(x / 6 + t) + Math.sin(y / 4 - t) + Math.sin((x + y) / 9 + t) + Math.sin(Math.hypot(x - W / 2, y - H / 2) / 5 - t);
        f.set(x, y, hsv(v / 8 + i / n, 0.8, 0.85));
      }
    });
  }

  function rainbowWave() {   // rainbow stripes waving across the panel
    return frames(24, 70, (f, i, n) => {
      for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) f.set(x, y, hsv((x + Math.sin(y / 5 + TAU * i / n) * 6) / 64 - i / n, 0.85, 0.9));
    });
  }

  function tunnel() {   // diving through a ringed tunnel, darkening into the distance
    return frames(16, 60, (f, i) => {
      for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
        const d = Math.hypot((x - W / 2) / 2, y - H / 2) + 0.01, a = Math.atan2(y - H / 2, (x - W / 2) / 2);
        const ring = Math.floor(40 / d + i / 4) % 2, seg = Math.floor((a / Math.PI + 1) * 6 + 40 / d * 0.3) % 2;
        f.set(x, y, dim(ring ^ seg ? '#3a9aff' : '#a23aff', Math.max(0, Math.min(1, (d - 6) / 10))));
      }
      f.light((g) => g.oval(W / 2 - 6, H / 2 - 3, W / 2 + 6, H / 2 + 3, '#2a1a5a'), 2);
    });
  }

  function ripple() {   // drops falling into still water, two soft rings spreading from each
    const drops = [[20 / 64 * W, 14 / 32 * H, 0], [44 / 64 * W, 18 / 32 * H, 8], [32 / 64 * W, 10 / 32 * H, 16]];
    return frames(24, 80, (f, i, n) => {
      for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
        let h = 0;
        drops.forEach(([cx, cy, t0]) => { const age = (i - t0 + n) % n, d = Math.hypot((x - cx) / 1.6, y - cy);
          [0, 3].forEach((lag) => { const a = age - lag; if (a > 0 && Math.abs(d - a * 1.1) < 0.9) h += (1 - age / n) * (lag ? 0.5 : 1); }); });
        f.set(x, y, mix('#0a2a4a', '#9adcff', Math.min(1, h)));
      }
      f.blur(1);
    });
  }

  function spiral() {   // a hypnotic spiral turning, its arms shading from gold to pink
    return frames(16, 70, (f, i) => {
      for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
        const a = Math.atan2(y - (H - 1) / 2, x - (W - 1) / 2), d = Math.hypot(x - (W - 1) / 2, y - (H - 1) / 2);
        if (Math.floor((a / Math.PI + 1) * 3 + d / 3 - i / 4) % 2) f.set(x, y, '#ffb547');
        else f.set(x, y, '#2a1a4a');
      }
      const arms = new Frame(); arms.px = f.px.map((c) => (c === '#ffb547' ? c : null));
      arms.grad([Math.floor((W - 1) / 2), Math.floor((H - 1) / 2)], [Math.floor((W - 1) / 2), 0], ['#fff0b0', '#ffb547', '#ff5ccf'], { type: 'radial', area: 'drawn' });
      T.over(f.px, arms.px);
    });
  }

  function equaliser() {   // music bars dancing, coloured green to red as they rise, glowing peaks that fall slowly
    const n = 32, bars = Math.floor(W / 4), up = H / 32, level = (b, i) => Math.round(up * (6 + 10 * Math.abs(wave(i, n, 3, b * 0.8) * Math.cos(TAU * 2 * i / n + b * 0.37)) + (b % 4 === 0 ? Math.round(8 * Math.max(0, 1 - ((i + b) % 8) / 3)) : 0)));
    const peaks = new Array(bars).fill(0), held = [];
    for (let pass = 0; pass < 2; pass++) for (let i = 0; i < n; i++) {   // run twice so the peaks are settled when the loop starts
      for (let b = 0; b < bars; b++) peaks[b] = Math.max(level(b, i), peaks[b] - 1);
      if (pass) held[i] = peaks.slice();
    }
    return frames(n, 80, (f, i) => {
      const k = i % n;
      for (let b = 0; b < bars; b++) f.rect(b * 4, H - level(b, k), 3, level(b, k), '#4ade80');
      f.grad([0, H - 1], [0, Math.round(6 * up)], ['#4ade80', '#ffe066', '#ff3b30'], { area: 'drawn' });
      f.light((g) => { for (let b = 0; b < bars; b++) g.rect(b * 4, H - 1 - held[k][b], 3, 1, '#6a6a6a'); }, 1);
      for (let b = 0; b < bars; b++) f.rect(b * 4, H - 1 - held[k][b], 3, 1, '#ffffff');
    });
  }

  function gameOfLife() {   // Conway's Life: two spaceships cruising round a wrap-around board past blinking oscillators, fading trails behind
    const n = 128;
    let g = new Uint8Array(W * H);
    const put = (rows, ox, oy) => rows.forEach((row, y) => [...row].forEach((ch, x) => { if (ch === 'X') g[((oy + y + H) % H) * W + (ox + x + W) % W] = 1; }));
    const lwss = ['X..X.', '....X', 'X...X', '.XXXX'];
    put(lwss, 2, 3); put(lwss, 34, 21);
    put(['XXX'], 8, 13); put(['.XXX', 'XXX.'], 24, 12); put(['XX..', 'XX..', '..XX', '..XX'], 42, 11); put(['XXX'], 56, 14);
    put(['XXX'], 14, 29); put(['XXX'], 46, 29);
    const gens = [g];
    for (let k = 0; k < n; k++) {
      const next = new Uint8Array(W * H);
      for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) { let s = 0;
        for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) if (dx || dy) s += g[((y + dy + H) % H) * W + (x + dx + W) % W];
        const v = g[y * W + x]; next[y * W + x] = s === 3 || (v && s === 2) ? 1 : 0; }
      g = next; gens.push(g);
    }
    return frames(n, 150, (f, i) => {
      const now = gens[i], before = gens[i === 0 ? n - 1 : i - 1];   // gens[n] is gens[0] again (the test checks)
      f.light((l) => now.forEach((v, k) => { if (v) l.px[k] = dim(hsv(0.5 + (k % W) / W * 0.3, 0.6, 1), 0.5); }), 1);
      before.forEach((v, k) => { if (v && !now[k]) f.px[k] = dim(hsv(0.5 + (k % W) / W * 0.3, 0.6, 1), 0.3); });
      now.forEach((v, k) => { if (v) f.px[k] = hsv(0.5 + (k % W) / W * 0.3, 0.6, 1); });
    });
  }

  // -- games --------------------------------------------------------------------------------
  function paddleBall() {   // two paddles knocking a glowing ball back and forth, a trail behind it
    const n = 40, ball = (i) => [Math.round(3 + 56 * tri(i / n)), Math.round(30 * tri(i / (n / 2) + 0.25))];
    return frames(n, 60, (f, i) => {
      for (let y = 0; y < H; y += 2) f.set(32, y, '#3b4048');
      f.text('3', 22, 2, '#5a606a'); f.text('2', 38, 2, '#5a606a');
      const [bx, by] = ball(i);
      [3, 2, 1].forEach((k) => { const [tx, ty] = ball(i - k); f.rect(tx, ty, 2, 2, dim('#ffe066', 0.5 - k * 0.13)); });
      f.light((g) => g.rect(bx, by, 2, 2, '#a89030'), 1);
      const w = tri(i / n), at = (k) => Math.min(26, Math.max(0, Math.round(by * k + 15 * (1 - k)) - 2));   // w: 0 with the ball at the left, 1 at the right
      f.rect(2, at(1 - w), 1, 6, '#ffffff');
      f.rect(61, at(w), 1, 6, '#ffffff');
      f.rect(bx, by, 2, 2, '#ffe066');
    });
  }

  function brickBreaker() {   // a ball bouncing off a shaded wall, knocking bricks out; it's caught on the paddle while the wall rebuilds, then served again
    const colours = ['#ff4a5a', '#ff9a3a', '#ffe066', '#4ade80', '#2ec4ff'], S0 = { x: 30, y: 27, dx: 1, dy: -1 };
    const brickAt = (x, y) => { const c = Math.floor((x - 2) / 6), r = Math.floor((y - 3) / 3); return x >= 2 && (x - 2) % 6 < 5 && y >= 3 && (y - 3) % 3 < 2 && r < 5 && c < 10 ? r * 10 + c : -1; };
    const states = [], gone = new Set(); let b = { ...S0 }, pad = 25;
    for (let t = 0; t < 400; t++) {                       // play until enough bricks are out and the ball comes down to the paddle
      states.push({ ...b, pad, gone: [...gone], spark: -1 });
      if (t > 50 && gone.size >= 10 && b.y === 26 && b.dy > 0) break;
      let nx = b.x + b.dx, ny = b.y + b.dy;
      if (nx < 1 || nx > 62) { b.dx = -b.dx; nx = b.x + b.dx; }
      if (ny < 1) { b.dy = 1; ny = b.y + 1; }
      const hit = brickAt(nx, ny);
      if (hit >= 0 && !gone.has(hit)) { gone.add(hit); b.dy = -b.dy; ny = b.y + b.dy; }
      if (ny > 27) { b.dy = -1; ny = 26; }
      b = { ...b, x: nx, y: ny }; pad = Math.max(0, Math.min(54, b.x - 5));
    }
    const caught = states[states.length - 1], order = [...caught.gone];
    let x = caught.x + caught.dx;                          // caught: the ball rides the paddle back to the middle
    const steps = Math.max(Math.abs(x - S0.x), order.length + 2);
    for (let k = 0; k < steps; k++) {
      x += Math.sign(S0.x - x);
      const left = order.slice(0, Math.max(0, order.length - k - 1));
      states.push({ x, y: 27, pad: Math.max(0, Math.min(54, x - 5)), gone: left, spark: order.length - k - 1 >= 0 ? order[order.length - k - 1] : -1 });
    }
    const n = states.length;
    return frames(n, 45, (f, i) => {
      const st = states[i % n], out = new Set(st.gone);
      f.sky(['#0a0e1c', '#000000']);
      for (let k = 0; k < 50; k++) if (!out.has(k)) {
        const bx = 2 + (k % 10) * 6, by = 3 + Math.floor(k / 10) * 3, c = colours[Math.floor(k / 10)];
        if (k === st.spark) { f.light((g) => g.rect(bx, by, 5, 2, '#a0a0a0'), 1); f.rect(bx, by, 5, 2, '#ffffff'); continue; }
        f.rect(bx, by, 5, 1, mix(c, '#ffffff', 0.35)); f.rect(bx, by + 1, 5, 1, mix(c, '#000000', 0.25));
      }
      f.rect(st.pad, 28, 10, 1, '#ffffff');
      f.light((g) => g.set(st.x, st.y, '#8a8a8a'), 1); f.set(st.x, st.y, '#ffffff');
    });
  }

  function fallingBlocks() {   // pieces dropping into a well and stacking up until two lines clear and it empties again
    const C = 3, X0 = 17, Y0 = 1;
    const pieces = [
      [[[0, 8], [0, 9], [1, 9], [2, 9]], '#3b6bff'], [[[1, 8], [2, 8], [3, 8], [3, 9]], '#ff8a3d'],
      [[[4, 8], [5, 8], [4, 9], [5, 9]], '#ffe066'], [[[6, 9], [7, 9], [8, 9], [9, 9]], '#2ec4ff'], [[[6, 8], [7, 8], [8, 8], [9, 8]], '#4ade80'],
    ];
    const plan = []; let t = 0;
    pieces.forEach(([cells, c]) => { const drop = Math.min(...cells.map(([, r]) => r)) + 2; plan.push({ cells, c, start: t, drop }); t += drop + 1; });
    const flash = t, n = t + 5;
    return frames(n, (i) => (i >= flash ? 110 : 75), (f, i) => {
      for (let y = 0; y < H; y++) { f.set(X0 - 1, y, '#3b4048'); f.set(X0 + 10 * C, y, '#3b4048'); }
      if (i >= n) return;                                     // after the loop: the empty well it started with
      const cell = (cx, cy, c) => { if (cy < 0) return; const x = X0 + cx * C, y = Y0 + cy * C;
        f.rect(x, y, C, C, c); f.rect(x, y, C, 1, mix(c, '#ffffff', 0.4)); f.rect(x, y, 1, C, mix(c, '#ffffff', 0.4));
        f.rect(x, y + C - 1, C, 1, mix(c, '#000000', 0.35)); f.rect(x + C - 1, y + 1, 1, C - 1, mix(c, '#000000', 0.35)); };
      if (i >= flash + 3) return;                             // cleared
      plan.forEach(({ cells, c, start, drop }) => {
        if (i < start) return;
        const off = Math.max(0, drop - (i - start));
        cells.forEach(([cx, cy]) => cell(cx, cy - off, i >= flash ? (i % 2 ? '#ffffff' : c) : c));
      });
      if (i >= flash) f.light((g) => g.rect(X0, Y0 + 8 * C, 10 * C, 2 * C, '#5a5a5a'), 2);
    });
  }

  function snake() {   // a snake slithering round to eat glowing apples, growing as it goes, then blinking and starting again
    const path = []; for (let x = 4; x < 60; x++) path.push([x, 8]); for (let y = 8; y < 24; y++) path.push([59, y]); for (let x = 59; x > 4; x--) path.push([x, 24]); for (let y = 24; y > 8; y--) path.push([4, y]);
    return frames(54, (i) => (i >= 48 ? 140 : 70), (f, i) => {
      for (let x = 0; x < W; x++) { f.set(x, 0, '#1f3a1f'); f.set(x, 31, '#1f3a1f'); }
      const step = Math.min(i, 47), head = (step * 3) % path.length, len = 8 + Math.floor(step / 12) * 3;
      if (i < 48) { const apple = path[(Math.floor(i / 12) * 36 + 30) % path.length];
        f.light((g) => g.disc(apple[0] - 0.5, apple[1] - 0.5, 1.6, '#8a2010'), 2);
        f.rect(apple[0] - 1, apple[1] - 1, 2, 2, '#ff4a3a'); f.set(apple[0] - 1, apple[1] - 1, '#ffb0a0'); f.set(apple[0], apple[1] - 2, '#4ade80'); }
      if (i >= 48 && i % 2 === 0) return;
      for (let k = len - 1; k >= 0; k--) { const [x, y] = path[(head - k + path.length) % path.length]; f.rect(x - 1, y - 1, 2, 2, k === 0 ? '#c8ff6a' : mix('#4ade80', '#1f7a3a', k / len)); }
    });
  }

  // -- occasions ----------------------------------------------------------------------------
  function scroller(message, look, bob = 2) {   // big letters scrolling across, coloured by a gradient tool
    const w = textWidth(message, 3);
    return () => frames(Math.ceil((w + W) / 2), 50, (f, i) => {
      const letters = new Frame();
      let x = W - i * 2;
      [...message.toUpperCase()].forEach((ch, k) => { x = letters.text(ch, x, 8 + Math.round(Math.sin(i / 3 + k) * bob), '#ffffff', 3); });
      look(letters, i);
      f.light((g) => { g.px = letters.px.map((c) => (c ? dim(c, 0.35) : null)); }, 1);
      T.over(f.px, letters.px);
    });
  }

  function birthdayCake() {   // a shaded cake with glowing candle flames, HAPPY BIRTHDAY above
    return frames(16, 110, (f, i, n) => {
      f.layer((g) => { g.text('HAPPY', 32 - textWidth('HAPPY') / 2, 1, '#fff'); g.text('BIRTHDAY', 32 - textWidth('BIRTHDAY') / 2, 7, '#fff');
        g.px = g.px.map((c, k) => (c ? hsv((k % W) / W - i / n, 0.6, 1) : null)); });   // a rainbow sliding through the letters
      f.rect(20, 23, 24, 8, '#f2c9a0'); f.grad([20, 23], [20, 31], ['#ffe0c0', '#c8906a'], { area: 'region', from: [30, 28] });
      f.rect(20, 23, 24, 2, '#ff8ac0'); f.rect(18, 31, 28, 1, '#c8c8d0');
      for (let x = 20; x < 44; x += 3) f.set(x, 25 + (x % 2), '#ff8ac0');
      [24, 29, 34, 39].forEach((x, k) => {
        f.rect(x, 18, 1, 5, ['#4ad0ff', '#ffe066', '#4ade80', '#ff8a3d'][k]);
        const flick = (i + k * 3) % 4, fx = x + (flick === 1 ? 1 : flick === 3 ? -1 : 0) * 0.5;
        f.light((g) => g.disc(fx, 15.5, 1.2, '#c86a10'), 1, 0.5);
        f.set(x, 17, '#ff8a2a'); f.set(x, 16, '#ffb43a'); f.set(x - 1, 16, '#ff8a2a'); f.set(Math.round(fx), 15, '#fff2a0'); if (flick !== 2) f.set(Math.round(fx), 14, '#ffe8a0');
      });
    });
  }

  function christmasTree() {   // a shaded tree with glowing twinkling lights, a shining star and gentle snow
    const r = rng(53), flakes = Array.from({ length: 22 }, () => [r() * W, r() * 30, r() * 6]);
    return frames(30, 120, (f, i, n) => {
      f.sky(['#06102a', '#142a4a']);
      f.layer((g) => { for (let y = 4; y < 27; y++) { const half = 1 + (y - 4) * 0.55 - ((y - 4) % 6) * 0.25; for (let x = Math.round(32 - half); x <= 32 + half; x++) g.set(x, y, '#1f7a3a'); }
        g.grad([26, 6], [40, 26], ['#3ab060', '#1f7a3a', '#0a3a1a'], { area: 'drawn' }); });
      f.rect(30, 27, 4, 3, '#6a3e22'); f.sky(['#e6eef8', '#b8c8e0'], 30, 31);
      const lights = [[32, 8], [30, 12], [35, 13], [28, 17], [33, 18], [37, 19], [26, 22], [31, 23], [36, 24], [40, 24]];
      const colourOf = (k) => ['#ffe066', '#ff4a5a', '#4ad0ff'][(k + Math.floor(i / 5)) % 3];
      f.light((g) => { lights.forEach(([x, y], k) => g.set(x, y, dim(colourOf(k), 0.8))); g.disc(32, 3, 2, i % 6 < 3 ? '#a89030' : '#605010'); }, 2);
      lights.forEach(([x, y], k) => f.set(x, y, colourOf(k)));
      f.set(32, 3, '#fff6b0'); f.set(31, 3, '#ffe066'); f.set(33, 3, '#ffe066'); f.set(32, 2, '#ffe066'); f.set(32, 4, '#ffe066');
      if (i % 6 < 3) { f.set(30, 3, '#fff6b0'); f.set(34, 3, '#fff6b0'); f.set(32, 1, '#fff6b0'); }
      flakes.forEach(([x, y0, ph]) => f.set(x + Math.sin(TAU * i / n + ph), (y0 + i) % 30, '#ffffff'));
    });
  }

  function pumpkin() {   // a glowing jack-o'-lantern on a dark night, a bat flapping across the moon
    return frames(20, 100, (f, i, n) => {
      f.sky(['#120a1e', '#2a1438']);
      f.light((g) => g.disc(52.5, 6.5, 4.5, '#4a4430'), 2); f.disc(52.5, 6.5, 4, '#f2e6c0'); f.set(51, 5, '#d8cca0'); f.set(54, 8, '#e0d4ac');
      const glow = (i % 5 === 2) ? '#ffd06a' : (i % 4 ? '#ffb43a' : '#ffe08a');
      f.light((g) => g.oval(17, 13, 47, 31, dim('#7a3008', i % 5 === 2 ? 0.8 : 0.6)), 3);   // its glow on the night, outside it
      f.oval(21, 12, 43, 30, '#d8661a');
      f.grad([24, 13], [44, 30], ['#ff9a40', '#d8661a', '#7a2e08'], { type: 'radial', area: 'region', from: [32, 21] });   // lit from the top left
      f.line(26, 14, 26, 28, '#a84a12'); f.line(38, 14, 38, 28, '#a84a12'); f.rect(31, 10, 2, 3, '#3a6a2a');
      const face = (c) => { f.sprite(['X.....X', 'XX...XX'], 29, 17, { X: c }); f.sprite(['X.....X', 'XX.X.XX', '.XXXXX.'], 29, 24, { X: c }); };
      for (const [dx, dy] of [[-1, 0], [1, 0], [0, -1], [0, 1]]) { f.sprite(['X.....X', 'XX...XX'], 29 + dx, 17 + dy, { X: '#5a2008' }); f.sprite(['X.....X', 'XX.X.XX', '.XXXXX.'], 29 + dx, 24 + dy, { X: '#5a2008' }); }
      face(glow === '#ffb43a' ? '#fff0a0' : glow === '#ffd06a' ? '#ffffff' : '#fff6c0');
      const bx = (i * (W + 10) / n) % (W + 10) - 5, by = 6 + Math.round(wave(i, n, 2)), up = i % 2;
      f.sprite(up ? ['X.....X', '.XX.XX.', '..XXX..'] : ['.......', '.XX.XX.', 'X.XXX.X'], bx - 3, by - 1, { X: '#a08cc0' });
      for (let y = 0; y < 12; y++) for (let x = 46; x < 60; x++) if (f.get(x, y) === '#a08cc0' && (x - 52.5) ** 2 + (y - 6.5) ** 2 <= 4.5 ** 2) f.set(x, y, '#140a1a');   // a silhouette where it crosses the moon
    });
  }

  function floatingHearts() {   // glowing hearts drifting up at different depths and fading
    const r = rng(61), shape = ['XX.XX', 'XXXXX', '.XXX.', '..X..'];
    const hearts = Array.from({ length: 10 }, (_, k) => [4 + r() * (W - 12), r(), [0.5, 1, 1.5][k % 3], r() * 6, k % 3 === 2 ? 2 : 1]);
    return frames(64, 60, (f, i, n) => {
      f.sky(['#0a0208', '#1a0614']);
      const draw = (g, glow) => hearts.forEach(([x, y0, v, ph, k]) => {
        const p = (y0 + i / n * v * 2) % 1, y = H + 2 - p * (H + 2 + 4 * k);
        g.sprite(shape, x + Math.sin(TAU * i / n + ph) * 2, y, { X: dim(hsv(0.95, k === 2 ? 0.8 : 0.7, k === 2 ? 1 : 0.9), (1 - p * 0.5) * (glow ? 0.5 : 1)) }, k);
      });
      f.light((g) => draw(g, true), 2); draw(f, false);
    });
  }

  function confetti() {   // confetti tumbling down in every colour, catching the light as it turns
    const span = H + 6, count = Math.round(60 * W * H / 2048), r = rng(71), bits = Array.from({ length: count }, (_, k) => [r() * W, k * span / count, 1 + (k % 2), r() * 6, hsv(r(), 0.75, 1), k % 2]);
    return frames(span, 70, (f, i, n) => bits.forEach(([x, y0, v, ph, c, ph2]) => {
      const y = (y0 + i * v) % span - 3, flip = (i + ph2) % 2, xx = x + Math.sin(TAU * i / n * 2 + ph) * 2;
      const lit = flip ? mix(c, '#ffffff', 0.3) : mix(c, '#000000', 0.25);
      f.set(xx, y, lit); if (flip) f.set(xx + 1, y, lit); else f.set(xx, y + 1, lit);
    }));
  }

  function countdown() {   // 3, 2, 1, GO! with a glowing ring draining round each number
    return frames(32, 125, (f, i) => {
      const n = Math.floor(i / 8), t = (i % 8) / 8;
      if (n < 3) {
        const label = String(3 - n);
        f.layer((g) => { for (let a = 0; a < 64; a++) if (a / 64 >= t) { const ang = a / 64 * TAU - Math.PI / 2; g.stroke(32 + Math.cos(ang) * 15, 16 + Math.sin(ang) * 14.5, 32 + Math.cos(ang) * 15, 16 + Math.sin(ang) * 14.5, '#fff', 2); }
          g.grad([32, 16], [32, 2], ['#ff5ccf', '#ffb547', '#ff5ccf'], { type: 'conic', area: 'drawn' }); });
        f.light((g) => g.text(label, 32 - textWidth(label, 4) / 2, 6, '#5a5a5a', 4), 1);
        f.text(label, 32 - textWidth(label, 4) / 2, 6, '#ffffff', 4);
      } else {
        f.light((g) => g.text('GO!', 32 - textWidth('GO!', 4) / 2, 6, '#2a7a3a', 4), 2);
        f.text('GO!', 32 - textWidth('GO!', 4) / 2, 6, (i % 2) ? '#4ade80' : '#c8ff6a', 4);
      }
    });
  }

  // -- icons and loaders ------------------------------------------------------------------
  function spinner() {   // a smooth loading spinner: a bright glowing head with a fading tail behind it
    return frames(12, 70, (f, i, n) => {
      const arm = (g, k, c) => { const ang = (k / n) * TAU; g.stroke(32 + Math.cos(ang) * 6.5, 16 + Math.sin(ang) * 6.5, 32 + Math.cos(ang) * 10, 16 + Math.sin(ang) * 10, c, 2); };
      for (let k = 0; k < n; k++) { const b = ((i - k + n) % n) / n; arm(f, k, dim('#ffb547', 0.12 + 0.88 * (1 - b) ** 2)); }
      f.light((g) => arm(g, i, '#8a5a1a'), 2);
      arm(f, i, '#ffe0a0');
    });
  }

  function loadingBar() {   // a glowing bar filling with a shimmer, then "READY"
    return frames(36, (i) => (i === 35 ? 600 : 70), (f, i) => {
      const done = Math.min(1, i / 30), label = done < 1 ? 'LOADING' : 'READY';
      f.text(label, 32 - textWidth(label) / 2, 7, done < 1 ? '#9aa1ac' : '#4ade80');
      f.rect(8, 16, 48, 1, '#3b4048'); f.rect(8, 22, 48, 1, '#3b4048'); f.rect(7, 17, 1, 5, '#3b4048'); f.rect(56, 17, 1, 5, '#3b4048');
      const w = Math.round(48 * done);
      if (w) {
        f.layer((g) => { g.rect(8, 17, w, 5, '#fff'); g.grad([8, 0], [56, 0], ['#ff8a3d', '#ffb547', '#ffe066'], { area: 'drawn' });
          for (let y = 17; y < 22; y++) for (let x = 0; x < w; x++) if (Math.abs(x - (i * 3) % 60 + 6) < 3) g.set(8 + x, y, '#fff2d0'); });
        f.light((g) => g.rect(8, 17, w, 5, '#4a3010'), 2);
      }
      const pct = `${Math.round(done * 100)}%`; f.text(pct, 32 - textWidth(pct) / 2, 25, '#ffffff');
    });
  }

  function hourglass() {   // sand running through a glass hourglass, which then turns over
    const sand = (top, y) => mix('#ffe39a', '#c8902a', top ? y / 12 : 1 - y / 12);
    return frames(30, (i) => (i < 22 ? 110 : 70), (f, k, n) => {
      const i = k % n, t = Math.min(1, i / 22), turning = i >= 22;   // the turn ends upside down, full on top: frame 0 again
      if (!turning) {
        for (let y = 0; y < 12; y++) { const half = 9 - y * 0.75;
          f.line(32 - half, 3 + y, 32 + half, 3 + y, y >= 12 * t ? sand(true, y) : '#1a2030');
          f.line(32 - half, 28 - y, 32 + half, 28 - y, y < 12 * t ? sand(false, y) : '#1a2030'); }
        for (let y = 3; y < 15; y++) f.shade(32 - (9 - (y - 3) * 0.75) + 1, y, 1, 0.25);
        if (t < 1) for (let y = 15; y < 28 - 12 * t; y++) if ((y + i) % 2) f.set(32, y, '#ffcf6a');
        f.rect(21, 2, 23, 1, '#c8a06a'); f.rect(21, 29, 23, 1, '#c8a06a');
      } else {                          // it turns over, shrinking a little so the corners stay on the panel
        const p = (i - 21) / 9, q = p * p * (3 - 2 * p), ang = q * Math.PI, c = Math.cos(ang), sn = Math.sin(ang), s = 1 - 0.3 * Math.sin(q * Math.PI);   // eased: it starts and ends gently
        const rot = (x, y) => [32 + (x * c - y * sn) * s, 16 + (x * sn + y * c) * s];
        for (let y = -12; y <= 12; y += 0.5) { const half = Math.abs(y) * 0.75; for (let x = -half; x <= half; x += 0.5) {
          const [px, py] = rot(x, y); f.set(px, py, y > 1.2 ? mix(sand(false, 12 - y), sand(true, 12 - y), q) : '#1a2030'); } }   // the sand ends shaded as the top bulb
        [[-11, -13], [-11, 13]].forEach(([x0, y0]) => { for (let x = 0; x <= 22; x += 0.5) { const [px, py] = rot(x0 + x, y0); f.set(px, py, '#c8a06a'); } });
      }
    });
  }

  function heartbeat() {   // a glowing heart-monitor trace sweeping across, the heart beating with each spike
    const beat = (x) => { const p = ((x % 32) + 32) % 32; return p === 10 ? -2 : p === 11 ? 9 : p === 12 ? -12 : p === 13 ? 6 : p === 14 ? -1 : p > 18 && p < 23 ? -2 * Math.sin((p - 18) / 4 * Math.PI) : 0; };
    return frames(32, 60, (f, i) => {
      for (let y = 0; y < H; y += 4) for (let x = 0; x < W; x += 4) f.set(x, y, '#0c2a1a');
      const head = (i * 2) % W, trace = (g, k) => { for (let x = 0; x < W; x++) { const age = (head - x + W) % W; if (age > 40) continue;
        const c = dim('#4ade80', (1 - age / 40) * k); g.set(x, 18 + beat(x), c); if (x > 0 && age < 40) g.line(x - 1, 18 + beat(x - 1), x, 18 + beat(x), c); } };
      f.light((g) => trace(g, 0.5), 1); trace(f, 1);
      f.light((g) => g.disc(head, 18 + beat(head), 1.5, '#4a8a5a'), 1); f.set(head, 18 + beat(head), '#e8ffe8');
      const on = ((i - 6 + 16) % 16) < 3;
      if (on) f.light((g) => g.sprite(['X.X', 'XXX', '.X.'], 50, 2, { X: '#8a2030' }), 1);
      f.sprite(['X.X', 'XXX', '.X.'], 50, 2, { X: on ? '#ff4a5a' : '#7a2a2a' }); f.text('72', 55, 1, '#4ade80');
    });
  }

  function sunnyDay() {   // a weather icon: a glowing shaded sun, its rays turning, a soft cloud drifting by
    return frames(24, 100, (f, i, n) => {
      f.light((g) => g.disc(26, 16, 9, '#8a6010'), 3);
      for (let k = 0; k < 8; k++) { const ang = k / 8 * TAU + i / n * Math.PI / 2; f.stroke(26 + Math.cos(ang) * 9.5, 16 + Math.sin(ang) * 9.5, 26 + Math.cos(ang) * 12.5, 16 + Math.sin(ang) * 12.5, '#ffd23a', 2); }
      f.disc(26, 16, 6.5, '#ffd23a'); f.grad([23, 13], [31, 21], ['#fff6c0', '#ffd23a', '#ff9a1a'], { type: 'radial', area: 'region', from: [26, 16] });
      const cx = Math.round(40 + wave(i, n) * 4);
      f.layer((g) => { g.disc(cx, 21, 5.5, '#fff', 4.5); g.disc(cx + 6, 22, 4.5, '#fff', 3.5); g.disc(cx - 5, 23, 3.5, '#fff', 2.5); g.rect(cx - 7, 23, 16, 3, '#fff');
        g.grad([0, 17], [0, 26], ['#ffffff', '#b8c4d4'], { area: 'drawn' }); });
    });
  }

  function rainCloud() {   // a weather icon: a shaded grey cloud with rain falling and the odd flash of lightning
    const bolt = ['..XX', '.XX.', 'XXXX', '..XX', '.XX.', '.X..', 'X...'];
    return frames(48, 90, (f, i) => {
      const flash = i === 31 || i === 33;
      f.layer((g) => { g.disc(28, 11, 7.5, '#fff', 5.5); g.disc(37, 12, 6.5, '#fff', 4.5); g.disc(21, 13, 4.5, '#fff', 3.5); g.rect(18, 13, 25, 4, '#fff');
        g.grad([0, 6], [0, 17], flash ? ['#ffffff', '#c8d0ff'] : ['#aab4c8', '#5a6478'], { area: 'drawn' }); });
      for (let k = 0; k < 7; k++) { const x = 20 + k * 3.5, y = 18 + ((i * 2 + k * 5) % 16); if (y < H) { f.set(x, y, '#4ad0ff'); f.set(x, y - 1, '#2a7aaa'); } }
      if (flash) { f.light((g) => g.sprite(bolt, 28, 17, { X: '#8a7a20' }, 2), 2); f.sprite(bolt, 28, 17, { X: '#ffe066' }, 2); }
    });
  }

  function wifiBars() {   // signal arcs lighting up one after another, glowing when lit
    return frames(5, (i) => (i === 4 ? 500 : 220), (f, i) => {
      const arcs = (g, lit, c) => { for (let k = 0; k < 4; k++) { const on = k <= i - 1 || i === 4; if (on !== lit) continue; const r = 4 + k * 6;
        for (let a = -0.75; a <= 0.75; a += 0.02) { const ang = -Math.PI / 2 + a; for (let w = 0; w < 2; w++) g.set(32 + Math.cos(ang) * (r - w), 26 + Math.sin(ang) * (r - w), c); } } };
      arcs(f, false, '#1a2a3a'); f.light((g) => arcs(g, true, '#1a5a7a'), 1); arcs(f, true, '#2ec4ff');
      f.disc(32, 26, 1.6, '#2ec4ff');
    });
  }

  // -- boot screens ---------------------------------------------------------------------------
  // The splash wall's block (3 x 3, dark spot in the middle) in its eight colours: [face, middle]
  const WALL = { B: ['#639bff', '#5b6ee1'], R: ['#d95763', '#ac3232'], T: ['#d9a066', '#df7126'], P: ['#d77bba', '#d95763'],
    Y: ['#fbf236', '#d9a066'], N: ['#8f563b', '#663931'], V: ['#306082', '#3f3f74'], G: ['#6abe30', '#4b692f'] };
  // letters three blocks wide and five tall (I is one wide); N and M are drawn so they can't be mistaken
  const NAME = { B: ['XX.', 'X.X', 'XX.', 'X.X', 'XX.'], O: ['XXX', 'X.X', 'X.X', 'X.X', 'XXX'], N: ['XX.', 'X.X', 'X.X', 'X.X', 'X.X'],
    G: ['XXX', 'X..', 'X.X', 'X.X', 'XXX'], M: ['X.X', 'XXX', 'XXX', 'X.X', 'X.X'], A: ['XXX', 'X.X', 'XXX', 'X.X', 'X.X'],
    T: ['XXX', '.X.', '.X.', '.X.', '.X.'], R: ['XX.', 'X.X', 'XX.', 'X.X', 'X.X'], I: ['X', 'X', 'X', 'X', 'X'], X: ['X.X', 'X.X', '.X.', 'X.X', 'X.X'] };
  // Reveal: a wall of blocks falls in, every block bright; then the blocks round the letters dim and the word is
  // left lit. BONGO, then MATRIX. Each letter is one colour; the blocks round it are random wall colours, never
  // the colour of a letter they touch, nor of the block beside or above them. Plays once (a boot screen).
  function reveal() {
    const COLS = 21, ROWS = 10, Y0 = 1, STEP = 40, PULL = 0.0011, TOP = 0.15;   // px per ms squared; never more than 6 px a frame, so a block can be followed
    const fallen = (ms) => { const tc = TOP / PULL; return ms < tc ? 0.5 * PULL * ms * ms : 0.5 * PULL * tc * tc + TOP * (ms - tc); };
    const wall = (word, colours, seed) => {
      const at = new Map(), k = (c, r) => c + ',' + r;
      let col = Math.floor((COLS - (word.length - 1 + [...word].reduce((w, ch) => w + NAME[ch][0].length, 0))) / 2);
      [...word].forEach((ch, n) => {
        NAME[ch].forEach((row, r) => [...row].forEach((on, c) => { if (on === 'X') at.set(k(col + c, 2 + r), { c: col + c, r: 2 + r, key: colours[n], letter: true }); }));
        col += NAME[ch][0].length + 1;
      });
      const r01 = rng(seed);
      for (let r = 0; r < ROWS; r++) for (let c = 0; c < COLS; c++) {
        if (at.has(k(c, r))) continue;
        const no = new Set([at.get(k(c - 1, r))?.key, at.get(k(c, r - 1))?.key]);
        for (let dc = -1; dc <= 1; dc++) for (let dr = -1; dr <= 1; dr++) { const b = at.get(k(c + dc, r + dr)); if (b && b.letter) no.add(b.key); }
        const free = Object.keys(WALL).filter((key) => !no.has(key));
        at.set(k(c, r), { c, r, key: free[Math.floor(r01() * free.length)], letter: false });
      }
      return [...at.values()];
    };
    const out = [];
    // one frame: every block at y(block) (null: not here yet), the blocks round the letters `lit` bright (1 to 0.3)
    const add = (cells, ms, y, lit = 1) => {
      const f = new Frame(ms);
      for (const b of cells) {
        const yy = y(b);
        if (yy === null || yy >= H) continue;
        const [face, mid] = WALL[b.key], full = b.letter || lit > 0.95;
        f.rect(b.c * 3, yy, 3, 3, full ? face : dim(face, lit));
        f.set(b.c * 3 + 1, yy + 1, full ? mid : mix(dim(mid, lit), dim(face, lit * 0.55), (1 - lit) / 0.7));   // dimmed, the middle ends a shade under its face
      }
      out.push(f);
      return f;
    };
    [['BONGO', 'BRGPT', 1100], ['MATRIX', 'YBRGTP', 2500]].forEach(([word, colours, hold], n) => {
      const cells = wall(word, colours, 7 + n), home = (b) => Y0 + b.r * 3;
      // falls in, bottom row first, left to right. A block a pixel short of home waits two short, so its last
      // step is never a one-pixel tick
      const drop = (t) => (b) => {
        const since = t - ((ROWS - 1 - b.r) * 60 + b.c * 12);
        if (since < 0) return null;
        const y = Math.round(fallen(since)) - 5;
        return y >= home(b) ? home(b) : y === home(b) - 1 ? y - 1 : y;
      };
      for (let t = 2 * STEP; ; t += STEP) { const y = drop(t); add(cells, STEP, y); if (cells.every((b) => y(b) === home(b))) break; }
      out[out.length - 1].ms = 350;
      for (let i = 1; i <= 8; i++) add(cells, 50, home, 1 - 0.7 * i / 8);     // the rest of the wall dims; the word stays lit
      out[out.length - 1].ms = hold;
      // ...and drops away off the bottom, the same pull and the same top speed
      const leave = (t) => (b) => { const since = t - ((ROWS - 1 - b.r) * 40 + b.c * 8); return since < 0 ? home(b) : home(b) + Math.round(fallen(since)); };
      for (let t = STEP; ; t += STEP) { const y = leave(t); add(cells, STEP, y, 0.3); if (cells.every((b) => y(b) >= H)) break; }
      out[out.length - 1].ms = n ? 200 : 240;                                  // a clear beat of black before the next word (or the loop)
    });
    return out;
  }

  // peek(): { length, frameAt(i) } so the gallery can build a thumbnail a frame at a time
  const peek = (make) => () => { lazy = true; try { const r = make(); return Array.isArray(r) ? { length: r.length, frameAt: (i) => r[i] } : r; } finally { lazy = false; } };
  const onCanvas = (make) => (...a) => { W = T.W; H = T.H; return make(...a); };   // make() draws at the size PixelTools is set to
  const P = (id, name, group, make, about, loops = true) => ({ id, name, group, make: onCanvas(make), about, loops, peek: peek(onCanvas(make)) });
  // More boot screens, all built from the wall's blocks on its 21 x 10 grid. Each plays once and ends on black,
  // so the playlist starts from a dark panel.
  const BC = 21, BR = 10, BY0 = 1;
  const ease = (u) => { u = Math.max(0, Math.min(1, u)); return u * u * (3 - 2 * u); };
  const fadeOut = (i, n, k = 8) => Math.min(1, Math.max(0, (n - 2 - i) / k));   // 1, then down to 0 over the last frames (the last is black)
  // one block with its top-left at pixel (x, y): a 3 x 3 face with the darker spot in the middle; `hot` 0..1 whitens it
  const blk = (f, x, y, key, a = 1, hot = 0) => {
    if (a <= 0.02) return;
    const [face, mid] = WALL[key];
    f.rect(x, y, 3, 3, dim(mix(face, '#ffffff', hot), a)); f.set(x + 1, y + 1, dim(mix(mid, '#ffffff', hot), a));
  };
  const cell = (f, c, r, key, a = 1, hot = 0) => blk(f, c * 3, BY0 + r * 3, key, a, hot);
  // a word in block letters, centred, its top on row `top`: Map 'c,r' -> colour key (one colour a letter)
  const blockWord = (word, colours, top = 2) => {
    const at = new Map();
    let col = Math.floor((BC - (word.length - 1 + [...word].reduce((w, ch) => w + NAME[ch][0].length, 0))) / 2);
    [...word].forEach((ch, n) => { NAME[ch].forEach((row, r) => [...row].forEach((on, c) => { if (on === 'X') at.set((col + c) + ',' + (top + r), colours[n]); })); col += NAME[ch][0].length + 1; });
    return at;
  };
  const eachCell = (at, fn) => at.forEach((key, k) => { const [c, r] = k.split(',').map(Number); fn(c, r, key); });
  const WORDS = [['BONGO', 'BRGPT'], ['MATRIX', 'YBRGTP']];
  const KEYS = ['R', 'T', 'Y', 'G', 'B', 'V', 'P', 'N'];

  function bootBuild() {   // BONGO is laid a letter at a time, each block landing white hot; a front crosses, knocking its columns out and laying MATRIX behind it; the last block rings the whole word
    const MS = 40, out = [], drop = (t) => (t <= 0 ? 0 : t <= 6 ? Math.round(0.5 * t * t) : 18 + 6 * (t - 6));   // never more than 6 px a frame
    const go = (t) => (t <= -2 ? 0 : t <= 0 ? -1 : drop(t));                       // a column lifts a pixel before it drops: the wind-up
    const spell = (w) => {                                                           // the word's blocks, each with its letter and its place in that letter (bottom row first)
      const cells = []; let col = Math.floor((BC - (WORDS[w][0].length - 1 + [...WORDS[w][0]].reduce((s, ch) => s + NAME[ch][0].length, 0))) / 2);
      [...WORDS[w][0]].forEach((ch, n) => {
        let k = 0;
        for (let r = 4; r >= 0; r--) [...NAME[ch][r]].forEach((on, c) => { if (on === 'X') cells.push({ c: col + c, r: 2 + r, key: WORDS[w][1][n], n, k: k++ }); });
        col += NAME[ch][0].length + 1;
      });
      return cells;
    };
    const A = spell(0), B = spell(1);
    A.forEach((b) => { b.t = 1 + b.n * 3 + b.k * 1.2; });                          // the letters go up together, each a beat behind the one before
    const T1 = Math.ceil(Math.max(...A.map((b) => b.t))) + 6 + 22;                 // BONGO stands still for 0.88 s
    B.forEach((b) => { b.t = T1 + 5 + b.c * 0.75 + (6 - b.r) * 1.3; });            // behind the front, each column from the bottom
    const DONE = Math.ceil(Math.max(...B.map((b) => b.t))) + 2, T2 = DONE + 8 + 22, END = T2 + 18;
    const lay = (f, b, i, ring) => {                                                // a block comes down the last few pixels and lands hot
      const age = i - b.t;
      if (age < 0) return;
      blk(f, b.c * 3, BY0 + b.r * 3 - (age < 1 ? 5 : age < 2 ? 2 : 0), b.key, 1, Math.max(ring, age < 2 ? 0.8 : 0.8 * (1 - (age - 2) / 4)));
    };
    for (let i = 0; i < END; i++) {
      const f = new Frame(MS);
      A.forEach((b) => { const dy = go(i - T1 - b.c * 0.75), y = BY0 + b.r * 3 + dy; if (dy === 0) lay(f, b, i, 0); else if (y < H) blk(f, b.c * 3, y, b.key); });
      const ring = i >= DONE ? Math.max(0, 0.75 * (1 - (i - DONE) / 8)) : 0;       // the last block home rings the whole word
      B.forEach((b) => { const dy = go(i - T2 - b.c * 0.5), y = BY0 + b.r * 3 + dy; if (dy === 0) lay(f, b, i, ring); else if (y < H) blk(f, b.c * 3, y, b.key); });
      out.push(f);
    }
    out.push(new Frame(200));
    return out;
  }

  function bootRain() {   // blocks rain down and the ones that land on BONGO stay; it lets go as MATRIX rains in, all but the top of the I: one last drop, and the word rings
    const MS = 40, out = [], drop = (t) => (t <= 0 ? 0 : t <= 6 ? Math.round(0.5 * t * t) : 18 + 6 * (t - 6));
    const go = (t) => (t <= -2 ? 0 : t <= 0 ? -1 : drop(t));
    const r01 = rng(11), FALL = 5;
    const shower = (w, t0, spread, step) => {                                       // when each block of the word lands: a column stacks from the bottom
      const at = blockWord(...WORDS[w]), lands = [];
      for (let c = 0; c < BC; c++) { let t = t0 + r01() * spread; for (let r = BR - 1; r >= 0; r--) if (at.has(c + ',' + r)) { lands.push({ c, r, key: at.get(c + ',' + r), t }); t += 2 + r01() * step; } }
      return lands;
    };
    const strays = (t0, t1, n) => Array.from({ length: n }, () => ({ c: Math.floor(r01() * BC), t: t0 + r01() * Math.max(1, t1 - t0), key: 'RTYGBP'[Math.floor(r01() * 6)] }));   // the six bright colours: the rain is dimmed, and brown and navy would be lost
    const A = shower(0, 2, 8, 3), lastA = Math.max(...A.map((b) => b.t));
    const T1 = Math.ceil(lastA) + 4 + 22;                                           // BONGO stands still for 0.88 s
    const letGo = Array.from({ length: BC }, () => r01() * 7), B = shower(1, T1 + 8, 7, 2.5);
    const dot = B.find((b) => b.c === 16 && b.r === 2);                             // the top of the I comes last, on its own
    const lastB = Math.max(...B.filter((b) => b !== dot).map((b) => b.t));
    dot.t = Math.ceil(lastB) + 13; dot.slow = true;
    const RING = dot.t, T2 = RING + 8 + 22, leave = Array.from({ length: BC }, () => r01() * 6), END = T2 + 16;
    const loose = [...strays(0, lastA - 9, 20), ...strays(T1, lastB - 9, 18)];
    for (let i = 0; i < END; i++) {
      const f = new Frame(MS);
      loose.forEach((b) => { const y = Math.round((i - b.t) * 4) - 3; if (i >= b.t && y < H) blk(f, b.c * 3, y, b.key, 0.5); });   // the rain that misses falls through, dimmer
      const flight = (b) => BY0 + b.r * 3 - Math.max(0, Math.round((b.t - i) * (b.slow ? 3 : FALL)));
      const hot = (b) => (i < b.t ? 0 : Math.max(0, 0.8 * (1 - (i - b.t) / 4)));
      A.forEach((b) => { const y = flight(b) + go(i - T1 - letGo[b.c]); if (y > -3 && y < H) blk(f, b.c * 3, y, b.key, 1, hot(b)); });
      const ring = i >= RING ? Math.max(0, 0.8 * (1 - (i - RING) / 8)) : 0;
      B.forEach((b) => { const y = flight(b) + go(i - T2 - leave[b.c]); if (y > -3 && y < H) blk(f, b.c * 3, y, b.key, 1, Math.max(ring, hot(b))); });
      out.push(f);
    }
    out.push(new Frame(200));
    return out;
  }

  function bootWave() {   // a wave of blocks rolls across, lifting them as it passes, and leaves BONGO; rolls back and leaves MATRIX; a ripple runs through it, and the last wave takes it away
    const MS = 40, out = [], words = WORDS.map(([w, c]) => blockWord(w, c));
    const V = 1.25, CROSS = Math.ceil((BC - 1 + (BR - 1) * 0.6) / V) + 1, TAIL = 6, HOLD = 22;   // the front moves 1.25 blocks a frame
    const T0 = 1, T1 = T0 + CROSS + TAIL + HOLD, RIP = T1 + CROSS + TAIL + HOLD, T2 = RIP + 18, END = T2 + CROSS + TAIL - 2;
    const lift = (behind) => (behind < 1.25 ? 2 : behind < 2.5 ? 1 : 0);            // the crest carries a block up two pixels and sets it down again
    for (let i = 0; i < END; i++) {
      const f = new Frame(MS), pass = i < T1 ? 0 : i < T2 ? 1 : 2, t = i - [T0, T1, T2][pass];
      for (let r = 0; r < BR; r++) for (let c = 0; c < BC; c++) {
        const d = (pass === 1 ? BC - 1 - c : c) + r * 0.6, behind = t * V - d;      // how far the front has gone past this block
        const old = pass ? words[pass - 1].get(c + ',' + r) : null, next = pass < 2 ? words[pass].get(c + ',' + r) : null;
        const x = c * 3, y = BY0 + r * 3;
        if (behind < 0) {
          if (old) blk(f, x, y, old);
          continue;
        }
        const glow = Math.max(0, 1 - behind / (TAIL * V));
        const rb = pass === 1 ? (i - RIP) * 2.2 - (c + r * 0.6) : -1;               // the ripple: a quick bright hop through MATRIX
        if (next) blk(f, x, y - lift(behind) - (rb >= 0 && rb < 2.2 ? 1 : 0), next, 1, Math.max(glow * 0.6, rb >= 0 ? 0.75 * (1 - rb / 5) : 0));
        else blk(f, x, y - lift(behind), old || KEYS[(c + r) % 8], glow, glow > 0.8 ? 0.3 : 0);
      }
      out.push(f);
    }
    out.push(new Frame(200));
    return out;
  }

  function bootSpiral() {   // one red block; blocks wind out from it until the wall is full and unwind to leave BONGO; they wind out again and unwind to leave MATRIX, which at the end winds back in to that first block
    const order = [];
    let c = 9, r = 4, dc = 1, dr = 0, run = 1, done = 0, turns = 0;
    while (order.length < BC * BR && done < 4000) {
      if (c >= 0 && c < BC && r >= 0 && r < BR) order.push([c, r]);
      c += dc; r += dr; done++;
      if (--run === 0) { [dc, dr] = [-dr, dc]; turns++; run = Math.ceil(turns / 2 + 0.5); }
    }
    const MS = 40, out = [], N = order.length, words = WORDS.map(([w, cs]) => blockWord(w, cs));
    const wound = (t) => 1 + 2 * t + 0.2 * t * t;                                   // it starts slowly and gathers speed
    let FILL = 0; while (wound(FILL) < N) FILL++;
    const SEED = 9, PER = 10, AGAIN = 15, GO = Math.ceil(N / PER);
    const FULL = SEED + FILL, UNWIND = FULL + 4, BONGO = UNWIND + GO, REWIND = BONGO + 26, FULL2 = REWIND + Math.ceil(N / AGAIN), UNWIND2 = FULL2 + 3,
      LEFT = UNWIND2 + GO, GLINT = LEFT + 4, OUT = GLINT + 30, GONE = OUT + Math.ceil(N / 14), END = GONE + 6;
    out.push(new Frame(MS));
    for (let i = 1; i <= END; i++) {
      const f = new Frame(MS), second = i >= REWIND;
      const shown = i < SEED ? 1 : wound(i - SEED), cleared = ((second ? i - UNWIND2 : i - UNWIND)) * PER;
      const back = (i - REWIND) * AGAIN, glint = (i - GLINT) * 14, eaten = (i - OUT) * 14;
      order.forEach(([cc, rr], k) => {
        if (k >= shown) return;
        const at = cc + ',' + rr, key = words[second ? 1 : 0].get(at), gone = N - 1 - k < cleared;   // unwinds from the outside in
        let hot = i < SEED ? 0.8 * (1 - (i - 1) / (SEED - 1)) : i < FULL ? Math.max(0, 1 - (shown - k) / 16) * 0.7 : 0;
        if (second && i < UNWIND2 && k >= back) {                                   // winding out again: BONGO stands until the wall reaches it
          if (words[0].has(at)) cell(f, cc, rr, words[0].get(at));
          return;
        }
        if (second && i < UNWIND2) hot = Math.max(hot, 0.7 * Math.max(0, 1 - (back - k) / (AGAIN * 2)));
        if (gone && !key) return;
        if (k > 0 && N - 1 - k < eaten) return;                                     // at the end the word winds back in to the first block
        if (gone) hot = Math.max(hot, 0.7 * (1 - (cleared - (N - 1 - k)) / (PER * 4)));
        if (i >= GLINT && i < OUT) hot = Math.max(hot, glint >= k ? 0.8 * (1 - (glint - k) / 56) : 0);   // a glint runs out along the spiral
        if (k === 0 && i >= GONE) hot = Math.min(0.9, (i - GONE + 1) * 0.3);       // the first block is the last: it flares and is gone
        if (k === 0 && i >= END) return;
        cell(f, cc, rr, gone ? key : KEYS[(k >> 1) % 8], 1, Math.max(0, hot));
      });
      out.push(f);
    }
    out[out.length - 1].ms = 200;
    return out;
  }

  function bootCurtain() {   // red curtains light up, a tug runs along them and they draw back on BONGO; it is flown out and MATRIX flown in; they close and go dark
    const out = [], MS = 50;
    const words = WORDS.map(([w, c]) => blockWord(w, c));
    // one frame: `lit` pairs of curtain columns alight, counted from the middle; the curtains drawn back `gap` px each side;
    // the words flown up `fly` px (MATRIX hangs 30 px under BONGO); `tug`: the curtain column (0 outermost) that catches the light
    const add = (ms, lit, gap, fly = 0, glint = 0, tug = -1) => {
      const f = new Frame(ms);
      if (gap > 0) words.forEach((at, w) => eachCell(at, (c, r, key) => { const y = BY0 + r * 3 + w * 30 - fly; if (y > -3 && y < H) blk(f, c * 3, y, key, 1, glint); }));
      for (let c = 0; c < 11; c++) {
        const x = c * 3 - gap;                                                     // pleats: every other column a shade darker
        if (x <= -3 || 10 - c >= lit) continue;
        for (let r = 0; r < BR; r++) for (const xx of [x, 60 - x]) { f.rect(xx, BY0 + r * 3, 3, 3, '#000000'); blk(f, xx, BY0 + r * 3, 'R', c % 2 ? 0.7 : 1, c === tug || c === tug + 1 ? 0.35 : 0); }
      }
      out.push(f);
    };
    add(100, 0, 0);
    [2, 4, 6, 8, 10, 11].forEach((lit) => add(MS, lit, 0));                         // the house lights find the curtain, from the middle out
    add(200, 11, 0);
    for (let c = 0; c <= 10; c += 2) add(MS, 11, 0, 0, 0, c);                       // a tug runs in from the wings
    add(100, 11, 0);
    for (let k = 1; k <= 14; k++) add(MS, 11, Math.round(30 * ease(k / 14)));       // and they draw back on BONGO
    [0.5, 0.3, 0.15].forEach((g) => add(MS, 11, 30, 0, g));
    add(900, 11, 30);
    for (let k = 1; k <= 12; k++) add(MS, 11, 30 + Math.min(3, k), Math.round(30 * ease(k / 12)));   // scene change: the curtains clear the stage for the wider word
    [0.5, 0.3, 0.15].forEach((g) => add(MS, 11, 33, 30, g));
    add(1200, 11, 33, 30);
    for (let k = 1; k <= 12; k++) add(MS, 11, Math.round(33 * (1 - ease(k / 12))), 30);   // curtain
    add(200, 11, 0, 30);
    [10, 8, 6, 4, 2].forEach((lit) => add(MS, lit, 0, 30));                         // lights down, from the wings in
    add(200, 0, 0, 30);
    return out;
  }

  function bootLoading() {   // a bar fills and lights BONGO above it; a second pass turns it to MATRIX, column by column; a third clears the panel
    const out = [], MS = 50, BAR = ['R', 'T', 'Y', 'G', 'B', 'P'];
    const words = WORDS.map(([w, c]) => blockWord(w, c, 1));
    // one frame: columns left of `fill` are loaded, left of `swap` are MATRIX, left of `clear` are gone; `head` is the hot column
    const add = (ms, fill, swap = 0, clear = 0, head = -1, glint = 0, a = 1) => {
      const f = new Frame(ms);
      for (let c = clear; c < BC; c++) {
        const at = words[c < swap ? 1 : 0], hot = c === head ? 0.6 : glint;
        for (let r = 1; r <= 5; r++) { const key = at.get(c + ',' + r); if (key) cell(f, c, r, key, (c < fill ? 1 : 0.3) * a, c < fill ? hot : 0); }
        if (c < fill) cell(f, c, 8, BAR[Math.floor(c / 3) % BAR.length], a, hot); else cell(f, c, 8, 'V', 0.5 * a);
      }
      out.push(f);
    };
    add(100, 0, 0, BC);
    add(60, 0, 0, 0, -1, 0, 0.5); add(250, 0);                                      // the empty track, the name waiting above it
    for (let c = 1; c <= 16; c++) add(55, c, 0, 0, c - 1);
    add(250, 16);                                                                   // it thinks about it...
    for (let c = 17; c <= BC; c++) add(40, c, 0, 0, c - 1);                         // ...and finishes in a rush
    [0.6, 0.4, 0.2].forEach((g) => add(MS, BC, 0, 0, -1, g));
    add(900, BC);
    for (let c = 1; c <= BC; c++) add(45, BC, c, 0, c - 1);                         // the second pass: BONGO turns to MATRIX
    [0.5, 0.3, 0.15].forEach((g) => add(MS, BC, BC, 0, -1, g));
    add(1200, BC, BC);
    for (let c = 1; c < BC; c++) add(35, BC, BC, c, c);                             // the third clears it
    add(200, BC, BC, BC);
    return out;
  }

  function bootAUnfold() {   // switch-on: a dot, a line, and the line unfolds into BONGO; folds, changes colour, unfolds into MATRIX; then folds to the line, the dot, and is gone
    const MS = 40, out = [], MID = 4;
    const W2 = WORDS.map(([w, c]) => {
      const at = blockWord(w, c), col = new Array(BC).fill(null);
      eachCell(at, (cc, r, key) => { col[cc] = key; });
      const fill = col.slice();                                                      // the gaps take the colour of the letter beside them, so the line is whole
      for (let cc = 1; cc < BC; cc++) if (!fill[cc]) fill[cc] = fill[cc - 1];
      for (let cc = BC - 2; cc >= 0; cc--) if (!fill[cc]) fill[cc] = fill[cc + 1];
      return { at, col, fill };
    });
    const add = (draw) => { const f = new Frame(MS); draw(f); out.push(f); };
    const line = (f, w, gaps = 1, hot = () => 0, span = 10) => { for (let c = 10 - span; c <= 10 + span; c++) if (W2[w].col[c]) cell(f, c, MID, W2[w].col[c], 1, hot(c)); else cell(f, c, MID, W2[w].fill[c], gaps, hot(c)); };
    const open = (f, w, u, hot = 0) => eachCell(W2[w].at, (c, r, key) => {         // u: 0 folded on the middle row, 1 open; past 1 the outer rows run a pixel over
      const d = r - MID;
      blk(f, c * 3, BY0 + MID * 3 + Math.round(d * 3 * Math.min(u, 1)) + (u > 1 && Math.abs(d) === 2 ? Math.sign(d) : 0), key, 1, hot);
    });
    const UNFOLD = [0.25, 0.6, 0.9, 1.1, 1], FOLD = [1.1, 0.9, 0.6, 0.25, 0];
    add(() => {});
    for (let k = 0; k < 6; k++) add((f) => cell(f, 10, MID, W2[0].fill[10], 1, 0.8 * (1 - k / 5)));        // the dot
    for (let k = 1; k <= 10; k++) add((f) => line(f, 0, 1, (c) => (Math.abs(c - 10) === k ? 0.5 : 0), k));  // the line runs out from it
    for (let k = 0; k < 3; k++) add((f) => line(f, 0));
    WORDS.forEach((_, w) => {
      UNFOLD.forEach((u, k) => add((f) => { if (k < 2) line(f, w, 0.6 - 0.3 * k); open(f, w, u); }));     // the gaps go out as the letters open
      for (let k = 0; k < 5; k++) add((f) => open(f, w, 1, 0.5 * (1 - k / 5)));
      for (let k = 0; k < (w ? 24 : 22); k++) add((f) => open(f, w, 1));
      FOLD.forEach((u) => add((f) => open(f, w, u)));
      if (w === 0) {
        add((f) => line(f, 0, 0.5)); add((f) => line(f, 0));
        for (let k = 0; k <= 10; k++) add((f) => { line(f, 0); line(f, 1, 1, (c) => (Math.abs(c - 10) === k ? 0.8 : 0), k); });   // the new colours run out from the middle
        for (let k = 0; k < 2; k++) add((f) => line(f, 1));
      }
    });
    for (let k = 0; k < 2; k++) add((f) => line(f, 1, 0));
    for (let k = 9; k >= 1; k--) add((f) => line(f, 1, 0, (c) => (Math.abs(c - 10) === k ? 0.4 : 0), k));   // the line runs back in
    for (let k = 0; k < 3; k++) add((f) => cell(f, 10, MID, W2[1].fill[10]));
    for (let k = 1; k <= 3; k++) add((f) => cell(f, 10, MID, W2[1].fill[10], 1, 0.3 * k));                 // the dot flares
    out.push(new Frame(200));
    return out;
  }

  function bootCBeat() {
    const out = [], words = WORDS.map(([w, c]) => blockWord(w, c));
    const word = (f, at, dy, hot = 0) => eachCell(at, (c, r, key) => { const y = BY0 + r * 3 + dy; if (y > -3 && y < H) blk(f, c * 3, y, key, 1, hot); });
    const echo = (f, at, s) => {                                                   // each letter's colour, a row further down each frame
      const foot = new Map(); eachCell(at, (c, r, key) => { if (r === 6) foot.set(c, key); });
      foot.forEach((key, c) => { if (s < 3) cell(f, c, 7 + s, key, 0.7); if (s > 0 && s < 4) cell(f, c, 6 + s, key, 0.35); });
    };
    const push = (ms, draw) => { const f = new Frame(ms); if (draw) draw(f); out.push(f); };
    const IN = [-30, -28, -24, -18, -12, -6], BOUNCE = [[0, 0.6], [-2, 0.4], [-3, 0.2], [-2, 0.1]];   // never more than 6 px a frame
    push(120);
    words.forEach((at, w) => {
      IN.forEach((dy) => push(40, (f) => { if (w) word(f, words[0], dy + 30); word(f, at, dy); }));   // the word before rolls out below as this one rolls in
      BOUNCE.forEach(([dy, hot], s) => push(40, (f) => { word(f, at, dy, hot); echo(f, at, s); }));
      push(w ? 1000 : 880, (f) => word(f, at, 0));                                 // the word, still
    });
    [2, 6, 12, 18, 24].forEach((dy) => push(40, (f) => word(f, words[1], dy)));     // third beat: it rolls away
    push(200);
    return out;
  }

  // Round wall: the logo is the first thing and the last. Its twelve blocks fly in from the four sides and click
  // into the small round wall; it opens from the middle into BONGO, turns over into MATRIX, closes back to the
  // logo, and the blocks fly off the way they came.

  function bootCLogo() {
    const out = [], words = WORDS.map(([w, c]) => blockWord(w, c));
    const push = (ms, draw) => { const f = new Frame(ms); if (draw) draw(f); out.push(f); };
    // the logo's blocks on the grid (columns 8 to 11, rows 3 to 6), clockwise from the top left, each with the side it comes from
    const LOGO = [[9, 3, 'B', 0, -1], [10, 3, 'V', 0, -1], [11, 4, 'Y', 1, 0], [11, 5, 'G', 1, 0], [10, 6, 'B', 0, 1], [9, 6, 'N', 0, 1], [8, 5, 'B', -1, 0], [8, 4, 'T', -1, 0],
      [9, 4, 'N', -1, 0], [10, 4, 'B', 1, 0], [10, 5, 'T', 1, 0], [9, 5, 'R', -1, 0]];
    const logo = new Map(LOGO.map(([c, r, key]) => [c + ',' + r, key]));
    const lands = (k) => 6 + Math.round(k * 1.5), LAST = lands(LOGO.length - 1);
    const fly = (f, i) => LOGO.forEach(([c, r, key, dx, dy], k) => {                // i frames in: 6 px a frame until it is home
      const away = Math.max(0, lands(k) - i) * 6, x = c * 3 + dx * away, y = BY0 + r * 3 + dy * away;
      if (x > -3 && x < W && y > -3 && y < H) blk(f, x, y, key, 1, away ? 0 : Math.max(0, 0.8 - (i - lands(k)) * 0.3));
    });
    // a front opening from the middle (u from 0 to 11 columns): `inside` behind it, `outside` ahead, the front itself hot
    const SPAN = 11, wipe = (f, inside, outside, u) => {
      const seen = new Set([...inside.keys(), ...outside.keys()]);
      seen.forEach((k) => {
        const [c, r] = k.split(',').map(Number), d = Math.abs(c - 9.5), at = d < u ? inside : outside, key = at.get(k);
        if (key) cell(f, c, r, key, 1, d < u && d >= u - 1.5 ? 0.6 : 0);
      });
    };
    push(80);
    for (let i = 1; i <= LAST + 2; i++) push(40, (f) => fly(f, i));
    push(420, (f) => fly(f, 99));                                                   // the logo, whole
    for (let u = 1; u <= SPAN + 1; u++) push(40, (f) => wipe(f, words[0], logo, u));
    push(900, (f) => wipe(f, words[0], logo, 99));                                  // BONGO
    for (let u = 1; u <= SPAN + 1; u++) push(40, (f) => wipe(f, words[1], words[0], u));
    push(1100, (f) => wipe(f, words[1], words[0], 99));                             // MATRIX
    for (let u = SPAN; u >= 0; u--) push(40, (f) => { wipe(f, words[1], new Map(), u); eachCell(logo, (c, r, key) => { if (Math.abs(c - 9.5) >= u) cell(f, c, r, key, 1, u ? 0 : 0.6); }); });
    push(40, (f) => eachCell(logo, (c, r, key) => cell(f, c, r, key, 1, 0.3)));
    push(560, (f) => fly(f, 99));                                                   // the last beat: the logo again
    for (let i = LAST; i >= 0; i--) push(40, (f) => fly(f, i));                     // and off the way they came
    push(200);
    return out;
  }

  // Shine (the quiet one): nothing moves but light. BONGO comes up a letter at a time; a shine crosses the panel
  // and the name behind it is MATRIX; a second shine crosses and leaves the panel dark.

  function bootCShine() {
    const out = [], words = WORDS.map(([w, c]) => blockWord(w, c)), none = new Map();
    const push = (ms, draw) => { const f = new Frame(ms); if (draw) draw(f); out.push(f); };
    const letter = [];                                                             // which letter of BONGO a column belongs to
    { let col = 1; [...'BONGO'].forEach((ch, n) => { for (let k = 0; k < 3; k++) letter[col + k] = n; col += 4; }); }
    const END = BC + 4.5 + 3, shine = (f, before, after, u) => {                    // the band's front is at column u on the top row, leaning back as it goes down
      for (let r = 0; r < BR; r++) for (let c = 0; c < BC; c++) {
        const d = u - (c + r * 0.5), k = c + ',' + r, key = (d >= 1 ? after : before).get(k);
        const band = d >= 0 && d < 2;
        if (key) cell(f, c, r, key, 1, band ? 0.75 : 0);
        else if (band) { const was = (d >= 1 ? before : after).get(k); if (was) cell(f, c, r, was, 1, 0.75); else if (d >= 0.5 && d < 1.5) cell(f, c, r, 'T', 0.5, 0.8); }
      }
    };
    push(100);
    for (let i = 0; i < 12; i++) push(50, (f) => eachCell(words[0], (c, r, key) => { const a = [0, 0.3, 0.55, 0.8, 1][Math.max(0, Math.min(4, i + 1 - letter[c] * 2))]; cell(f, c, r, key, a); }));
    push(850, (f) => shine(f, words[0], none, -9));                                 // BONGO
    for (let u = 0; u <= END; u += 1.5) push(40, (f) => shine(f, words[0], words[1], u));
    push(1050, (f) => shine(f, none, words[1], 99));                                // MATRIX
    for (let u = 0; u <= END; u += 1.5) push(40, (f) => shine(f, words[1], none, u));
    push(200);
    return out;
  }

  function bootBUnfold() {   // switched on like an old television: a dot, a line, and the line opens into BONGO; it shuts to the line and opens into MATRIX; then line, dot, dark
    const out = [], MS = 45, MID = 4, HOT = 0.75;
    const words = WORDS.map(([w, colours]) => {                                    // each word by column: its colour there and its rows
      const cols = new Map();
      eachCell(blockWord(w, colours, 2), (c, r, key) => { if (!cols.has(c)) cols.set(c, { key, rows: [] }); cols.get(c).rows.push(r); });
      cols.forEach((col) => col.rows.sort((p, q) => Math.abs(q - MID) - Math.abs(p - MID)));
      return cols;
    });
    // one frame: the line reaching `span` columns either side of the middle, opened `s` (0: a line, 1: the word) into word `w`
    const add = (ms, w, span, s = 0, a = 1) => {
      const f = new Frame(ms);
      for (let c = 10 - span; c <= 10 + span; c++) {
        const col = words[w].get(c);
        if (s === 0 || !col) { if (s < 0.3) cell(f, c, MID, 'Y', a * (s ? 0.6 : 1), HOT); continue; }
        col.rows.forEach((r) => blk(f, c * 3, BY0 + MID * 3 + Math.round((r - MID) * 3 * s), col.key, a, HOT * (1 - s)));
      }
      out.push(f);
    };
    const open = (w, span) => { for (let k = 1; k <= 7; k++) add(MS, w, span, ease(k / 7)); };
    const shut = (w, span) => { for (let k = 1; k <= 5; k++) add(MS, w, span, 1 - (k / 5) ** 2); };
    add(100, 0, -1);
    add(MS, 0, 0, 0, 0.5); add(135, 0, 0);                                          // the dot
    [1, 2, 4, 6, 8, 9].forEach((span) => add(MS, 0, span));                         // the line
    add(90, 0, 9);
    open(0, 9);
    out[out.length - 1].ms = 900;
    shut(0, 9);
    add(90, 0, 9); add(90, 1, 10);                                                  // the line reaches the edges: MATRIX is wider
    open(1, 10);
    out[out.length - 1].ms = 1200;
    shut(1, 10);
    add(90, 1, 10);
    [8, 6, 4, 2, 1, 0].forEach((span) => add(MS, 1, span));                         // line, dot,
    add(135, 1, 0); add(MS, 1, 0, 0, 0.5);
    add(200, 1, -1);                                                                // dark
    return out;
  }

  function bootAReels() {   // each letter is a reel of blocks: they spin down and lock one by one on BONGO, spin on and lock on MATRIX, the last reel late; then the word rings
    const MS = 40, out = [], HOME = BY0 + 2 * 3, r01 = rng(23);
    const drop = (t) => (t <= 0 ? 0 : t <= 6 ? Math.round(0.5 * t * t) : 18 + 6 * (t - 6));
    const rem = (k) => (k <= 0 ? 0 : k <= 6 ? [0, -1, 1, 3, 6, 9, 13][k] : 13 + 4 * (k - 6));   // px still to run k frames before a reel locks: it slows, runs a pixel past and comes back
    const spell = (w) => {
      let col = Math.floor((BC - (WORDS[w][0].length - 1 + [...WORDS[w][0]].reduce((s, ch) => s + NAME[ch][0].length, 0))) / 2);
      return [...WORDS[w][0]].map((ch, n) => { const L = { c0: col, rows: NAME[ch], wide: NAME[ch][0].length, key: WORDS[w][1][n] }; col += L.wide + 1; return L; });
    };
    const A = spell(0), B = spell(1);
    A.forEach((L, n) => { L.start = 1 + 2 * n; L.lock = 13 + 4 * n; });
    const T1 = A[4].lock + 4 + 22;                                                   // BONGO stands still for 0.88 s
    A.forEach((L, n) => { L.out = T1 + 2 * n; });
    B.forEach((L, n) => { L.start = T1 + 5 + 2 * n; L.lock = T1 + 17 + 4 * n + (n === 5 ? 6 : 0); });     // the last reel keeps you waiting
    const RING = B[5].lock, T2 = RING + 8 + 22, END = T2 + 16;
    B.forEach((L, n) => { L.out = T2 + n; });
    [...A, ...B].forEach((L) => {                                                    // the strip that runs ahead of the letter, four clear rows below it: bright blocks in any colour but its own
      const n = Math.max(0, Math.floor((rem(L.lock - L.start) - HOME - 27) / 3)), keys = [...'RTYGBP'].filter((k) => k !== L.key);
      L.strip = Array.from({ length: n }, () => Array.from({ length: L.wide }, () => keys[Math.floor(r01() * keys.length)]));
    });
    const letter = (f, L, y, hot) => L.rows.forEach((row, r) => [...row].forEach((on, c) => { const yy = y + r * 3; if (on === 'X' && yy > -3 && yy < H) blk(f, (L.c0 + c) * 3, yy, L.key, 1, hot); }));
    for (let i = 0; i < END; i++) {
      const f = new Frame(MS), ring = i >= RING ? Math.max(0, 0.8 * (1 - (i - RING) / 8)) : 0;
      [A, B].forEach((word, w) => word.forEach((L) => {
        if (i < L.start) return;
        const gone = i - L.out, y = HOME - rem(L.lock - i) + (gone < 0 ? 0 : gone < 2 ? -1 : drop(gone - 1));   // spinning out: a pixel up, then away
        L.strip.forEach((row, j) => { const yy = y + 27 + j * 3; if (yy > -3 && yy < H) row.forEach((key, c) => blk(f, (L.c0 + c) * 3, yy, key, 0.6)); });
        letter(f, L, y, Math.max(w ? ring : 0, i >= L.lock ? Math.max(0, 0.6 * (1 - (i - L.lock) / 4)) : 0));
      }));
      out.push(f);
    }
    out.push(new Frame(200));
    return out;
  }

  function bootBBounce() {   // a hot block bounces along the panel: each letter of BONGO lights where it lands; it bounces back and turns them to MATRIX; a third pass knocks them down
    const out = [], MS = 40, TOP = 4, REST = BY0 + (TOP - 1) * 3, HIGH = 9;
    const fall = (t) => (t <= 0 ? 0 : t <= 6 ? Math.round(0.5 * t * t) : 18 + 6 * (t - 6));
    const letters = WORDS.map(([w, colours]) => {                                  // each letter: its blocks, its colour and where the ball lands on it
      let col = Math.floor((BC - (w.length - 1 + [...w].reduce((n, ch) => n + NAME[ch][0].length, 0))) / 2);
      return [...w].map((ch, n) => {
        const wide = NAME[ch][0].length, cells = [];
        NAME[ch].forEach((row, r) => [...row].forEach((on, c) => { if (on === 'X') cells.push([col + c, TOP + r]); }));
        const one = { key: colours[n], cells, x: (col + (wide >> 1)) * 3 }; col += wide + 1; return one;
      });
    });
    let up = [];                                                                    // the letters showing: { one, hit: the frame it was struck, down: the frame it was knocked away }
    const add = (ms, ball) => {
      const f = new Frame(ms), i = out.length;
      up = up.filter((e) => e.down === undefined || BY0 + TOP * 3 + fall(i - e.down) < H);
      for (const e of up) {
        const age = i - e.hit, dy = e.down === undefined ? (age < 2 ? 1 : 0) : fall(i - e.down);   // it dips a pixel under the blow
        e.one.cells.forEach(([c, r]) => blk(f, c * 3, BY0 + r * 3 + dy, e.one.key, 1, 0.8 * Math.max(0, 1 - age / 4)));
      }
      if (ball) blk(f, ball[0], ball[1], 'Y', 1, 0.7);
      out.push(f);
    };
    // the ball comes in from off the panel, lands on each stop in turn (strike(j) as it does) and bounces on off the far side
    const pass = (stops, per, strike) => {
      const dir = Math.sign(stops[stops.length - 1] - stops[0]), xs = [stops[0] - dir * 12, ...stops, stops[stops.length - 1] + dir * 12];
      for (let j = 0; j + 1 < xs.length; j++) for (let k = 1; k <= per; k++) {
        const u = k / per;
        if (k === per && j < stops.length) strike(j);
        add(MS, [Math.round(xs[j] + (xs[j + 1] - xs[j]) * u), REST - Math.round(HIGH * 4 * u * (1 - u))]);
      }
    };
    const [bongo, matrix] = letters;
    add(100, null);
    pass(bongo.map((l) => l.x), 6, (j) => up.push({ one: bongo[j], hit: out.length }));
    add(800, null);
    pass(matrix.map((l) => l.x).reverse(), 6, (j) => {                             // back the other way: X takes the last O's place, R takes G's...
      const n = matrix.length - 1 - j, old = bongo[n === 5 ? 4 : n === 4 ? -1 : n];
      up = up.filter((e) => e.one !== old); up.push({ one: matrix[n], hit: out.length });
    });
    add(1050, null);
    pass(matrix.map((l) => l.x), 5, (j) => { const e = up.find((x) => x.one === matrix[j]); e.hit = e.down = out.length; });   // and once more, knocking each one down
    while (up.length) add(MS, null);
    out[out.length - 1].ms = 200;
    return out;
  }

  const PRESETS = [
    P('blank', 'Blank', 'From v2, remastered', blank, 'An empty frame to start from.', false),
    P('ball', 'Bouncing ball', 'From v2, remastered', bouncingBall, 'A shaded ball bouncing there and back, squashing as it lands.'),
    P('heart', 'Pixel heart', 'From v2, remastered', pixelHeart, 'A glowing heart that beats twice and rests.'),
    P('stars', 'Starfield', 'From v2, remastered', starfield, 'Flying through stars.'),
    P('rain', 'Digital rain', 'From v2, remastered', digitalRain, 'Glowing green code falling down the panel.'),
    P('fireworks', 'Fireworks', 'From v2, remastered', fireworks, 'Rockets climb and burst into glowing colour.'),
    P('ocean', 'Sunset sea', 'Scenes', oceanWaves, 'Waves rolling in under a glowing setting sun.'),
    P('snow', 'Snowy night', 'Scenes', snowfall, 'Snow falling over a little house, its window glowing.'),
    P('campfire', 'Campfire', 'Scenes', campfire, 'A crackling fire lighting up the night.'),
    P('aquarium', 'Aquarium', 'Scenes', aquarium, 'Fish swimming through sunbeams past swaying weed.'),
    P('rocket', 'Rocket launch', 'Scenes', rocketLaunch, '3, 2, 1, then lift-off on a glowing plume.', false),
    P('lava', 'Lava lamp', 'Scenes', lavaLamp, 'Warm glowing blobs rising and sinking.'),
    P('aurora', 'Northern lights', 'Scenes', aurora, 'Soft aurora rippling over the trees.'),
    P('night', 'Shooting stars', 'Scenes', shootingStars, 'A crescent moon, twinkling stars, a shooting star.'),
    P('lighthouse', 'Lighthouse', 'Scenes', lighthouse, 'A soft beam sweeping over a night sea.'),
    P('window', 'Rainy window', 'Scenes', rainyWindow, 'Rain on the glass, blurred city lights behind.'),
    P('plasma', 'Plasma', 'Patterns', plasma, 'Classic flowing colours.'),
    P('rainbow', 'Rainbow wave', 'Patterns', rainbowWave, 'Rainbow stripes waving across.'),
    P('tunnel', 'Tunnel', 'Patterns', tunnel, 'Diving through a ringed tunnel.'),
    P('ripple', 'Ripples', 'Patterns', ripple, 'Drops falling into still water.'),
    P('spiral', 'Spiral', 'Patterns', spiral, 'A turning spiral, gold to pink.'),
    P('eq', 'Equaliser', 'Patterns', equaliser, 'Music bars dancing, green to red.'),
    P('life', 'Game of Life', 'Patterns', gameOfLife, 'Spaceships cruising past blinking oscillators.'),
    P('paddle', 'Paddle ball', 'Games', paddleBall, 'Two paddles and a glowing ball.'),
    P('bricks', 'Brick breaker', 'Games', brickBreaker, 'A ball smashing through a wall that rebuilds.'),
    P('blocks', 'Falling blocks', 'Games', fallingBlocks, 'Pieces stacking up until two lines clear.'),
    P('snake', 'Snake', 'Games', snake, 'A snake eating apples and growing.', false),
    P('hello', 'Hello!', 'Occasions', scroller('HELLO!', (g) => g.grad([0, 0], [63, 0], RAINBOW, { area: 'drawn' })), 'Big rainbow letters scrolling by.'),
    P('welcome', 'Welcome home', 'Occasions', scroller('WELCOME HOME', (g) => g.grad([0, 7], [0, 24], ['#ffe39a', '#ffb547', '#ff6a2a'], { area: 'drawn' }), 1), 'A warm welcome scrolling by.'),
    P('birthday', 'Birthday cake', 'Occasions', birthdayCake, 'Glowing candles, HAPPY BIRTHDAY above.'),
    P('xmas', 'Christmas tree', 'Occasions', christmasTree, 'Glowing lights and gentle snow.'),
    P('pumpkin', 'Pumpkin', 'Occasions', pumpkin, 'A glowing jack-o\'-lantern and a bat.'),
    P('hearts', 'Floating hearts', 'Occasions', floatingHearts, 'Glowing hearts drifting up.'),
    P('confetti', 'Confetti', 'Occasions', confetti, 'Confetti tumbling down.'),
    P('countdown', '3, 2, 1, GO!', 'Occasions', countdown, 'A countdown with draining rings.', false),
    P('spinner', 'Spinner', 'Icons and loaders', spinner, 'A smooth loading spinner.'),
    P('loading', 'Loading bar', 'Icons and loaders', loadingBar, 'A bar filling up, then READY.', false),
    P('hourglass', 'Hourglass', 'Icons and loaders', hourglass, 'Sand running through, then it turns over.'),
    P('ecg', 'Heartbeat', 'Icons and loaders', heartbeat, 'A glowing heart-monitor trace.'),
    P('sun', 'Sunny', 'Icons and loaders', sunnyDay, 'The sun turning, a cloud drifting by.'),
    P('storm', 'Rain cloud', 'Icons and loaders', rainCloud, 'Rain and the odd flash of lightning.'),
    P('wifi', 'Signal', 'Icons and loaders', wifiBars, 'Signal arcs lighting up.', false),
    // boot: tools/build_sleep.py also writes each as resources/media/built-in/boot_<boot>.gif, a built-in boot screen
    Object.assign(P('boot-reveal', 'Reveal', 'Boot screens', reveal, 'A wall of blocks falls in, then dims to leave BONGO, then MATRIX.', false), { boot: 'reveal' }),
    Object.assign(P('boot-c-logo', 'Round wall', 'Boot screens', bootCLogo, 'Blocks fly in to build the logo, which opens into BONGO, then MATRIX, and closes again.', false), { boot: 'round_wall' }),
    Object.assign(P('boot-c-beat', 'Drum beat', 'Boot screens', bootCBeat, 'BONGO lands with a bounce, then MATRIX, on three even beats.', false), { boot: 'drum_beat' }),
    Object.assign(P('boot-a-unfold', 'Unfold', 'Boot screens', bootAUnfold, 'A dot, a line, and the line unfolds into BONGO, then MATRIX, and folds away to the dot.', false), { boot: 'unfold' }),
    Object.assign(P('boot-b-unfold', 'Switch on', 'Boot screens', bootBUnfold, 'A dot, a line, and the line opens into BONGO, then MATRIX; then line, dot, dark.', false), { boot: 'switch_on' }),
    Object.assign(P('boot-c-shine', 'Shine', 'Boot screens', bootCShine, 'BONGO lights up, a shine crosses and leaves MATRIX, then crosses again and leaves the dark.', false), { boot: 'shine' }),
    Object.assign(P('boot-build', 'Block by block', 'Boot screens', bootBuild, 'BONGO goes up block by block; a front knocks it out and lays MATRIX behind it.', false), { boot: 'block_by_block' }),
    Object.assign(P('boot-rain', 'Block rain', 'Boot screens', bootRain, 'Blocks rain down and the ones that land on the name stay; one last drop tops the I.', false), { boot: 'block_rain' }),
    Object.assign(P('boot-wave', 'Rainbow wave', 'Boot screens', bootWave, 'A wave of blocks rolls across and leaves BONGO, rolls back and leaves MATRIX, then takes it away.', false), { boot: 'rainbow_wave' }),
    Object.assign(P('boot-spiral', 'Spiral', 'Boot screens', bootSpiral, 'Blocks wind out from one red block and unwind to leave BONGO, wind out again for MATRIX, and wind back in.', false), { boot: 'spiral' }),
    Object.assign(P('boot-curtain', 'Curtains', 'Boot screens', bootCurtain, 'Red curtains draw back on BONGO, MATRIX is flown in, and the curtains close.', false), { boot: 'curtains' }),
    Object.assign(P('boot-loading', 'Loading bar', 'Boot screens', bootLoading, 'A bar fills and lights BONGO, a second pass turns it to MATRIX, a third clears it.', false), { boot: 'loading_bar' }),
    Object.assign(P('boot-a-reels', 'Reels', 'Boot screens', bootAReels, 'Reels of blocks spin and lock a letter at a time on BONGO, then MATRIX.', false), { boot: 'reels' }),
    Object.assign(P('boot-b-bounce', 'Bouncing block', 'Boot screens', bootBBounce, 'A hot block bounces along and lights BONGO, bounces back for MATRIX, then knocks it down.', false), { boot: 'bouncing_block' }),
  ];
  window.ANIMATOR_PRESETS = PRESETS;
  // the drawing kit, for more presets in their own files (js/animator-sleep.js: the built-in sleep screens)
  window.AnimatorKit = { W, H, TAU, Frame, frames, hsv, rng, tri, wave, dim, textWidth, P };
})();
