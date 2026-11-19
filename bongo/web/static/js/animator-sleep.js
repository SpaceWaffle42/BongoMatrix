// The built-in sleep screens, drawn with the animator's own tools (js/animator-tools.js, through the preset
// kit in js/animator-presets.js): glows, gradients, blur and layers. They join the presets as "Sleep screens",
// so any of them can be opened and changed in the Animator, and tools/build_sleep.py turns these same
// drawings into resources/media/built-in/sleep_*.gif.
// Every one is a calm, seamless six-second loop at 20 fps (120 frames of 50 ms): the frame after the last
// is the first again. Mostly black (the panel is dim at night and black is off), soft light, nothing sudden.
(function () {
  const { W, H, TAU, Frame, frames, hsv, rng, wave, dim, P } = window.AnimatorKit;
  const T = window.PixelTools;
  const { hex, mix } = T;
  const N = 120, MS = 50, LOOP = N * MS;
  // draw(frame, u 0..1, t in ms); the frame after the last is drawn as the first, exactly
  const loop = (draw) => frames(N, MS, (f, i, n) => draw(f, (i % n) / n, (i % n) * MS));
  const ease = (u) => { u = Math.max(0, Math.min(1, u)); return u * u * (3 - 2 * u); };
  const breath = (u, k = 1, ph = 0) => 0.5 - 0.5 * Math.cos(TAU * (k * u + ph));     // 0 -> 1 -> 0, k times a loop

  // -- shared pieces -----------------------------------------------------------------------------
  const Z = {
    4: ['XXXX', '..X.', '.X..', 'XXXX'],
    5: ['XXXXX', '...X.', '..X..', '.X...', 'XXXXX'],
    7: ['XXXXXXX', '.....X.', '....X..', '...X...', '..X....', '.X.....', 'XXXXXXX'],
  };
  const ZZZ = ['#d6ccff', '#9fb6ff'];            // moonlit lavender to sky blue, top-left to bottom-right
  // a Z at (x, y), `v` 0..1 how bright: a soft glow behind, a gradient across the letter
  // `add`: light it onto what's there instead of covering it, so two Zs fading into each other sum
  function zed(f, x, y, size, v, stops = ZZZ, glow = 0.7, add = false) {
    if (v <= 0.01) return;
    const rows = Z[size], w = rows[0].length, h = rows.length, s = stops.map((c) => dim(c, v));
    x = Math.round(x); y = Math.round(y);
    f.light((g) => g.sprite(rows, x, y, { X: dim(stops[1], v * 0.55) }), 2, glow);
    const letter = (g) => { g.sprite(rows, x, y, { X: s[0] }); g.grad([x, y], [x + w - 1, y + h - 1], s, { area: 'drawn' }); };
    if (add) f.light(letter, 0, 1); else f.layer(letter);
  }
  // a soft round light: `r` its radius, brightest in the middle
  function glowDot(f, x, y, colour, v, r = 1.6) {
    f.light((g) => { for (let dy = -3; dy <= 3; dy++) for (let dx = -3; dx <= 3; dx++) {
      const k = Math.max(0, 1 - Math.hypot(dx + (Math.round(x) - x), dy + (Math.round(y) - y)) / (r + 0.5));
      if (k > 0) g.set(Math.round(x) + dx, Math.round(y) + dy, dim(colour, v * k * k));
    } }, 1, 1);
  }
  // twinkling stars that stay where they are: [x, y, phase, speed]
  function starsOf(seed, count, maxY = H - 6) {
    const r = rng(seed), out = [];
    for (let tries = 0; out.length < count && tries < count * 50; tries++) {   // at least 4 px apart, so none clump
      const s = [Math.floor(r() * W), Math.floor(r() * maxY), r(), 1 + Math.floor(r() * 3)];
      if (out.every(([x, y]) => Math.hypot(x - s[0], y - s[1]) >= 4)) out.push(s);
    }
    return out;
  }
  function twinkle(f, stars, u, lo = 0.12, hi = 0.42, colour = '#cfd8ff') {
    stars.forEach(([x, y, ph, k]) => f.set(x, y, dim(colour, lo + (hi - lo) * breath(u, k, ph))));
  }
  // a row of pine trees along the bottom, black against the sky: [x, height] each
  function pines(f, trees, colour = '#040a08') {
    trees.forEach(([x0, h]) => { for (let y = 0; y < h; y++) {
      const half = Math.floor((y % 3 + y / 3) * 0.7);   // tiers: wider as it goes down
      for (let x = x0 - half; x <= x0 + half; x++) f.set(x, H - h + y, colour);
    } });
  }
  // anti-aliased disc light: how much of pixel (x, y) is inside the circle (4 x 4 samples)
  function cover(x, y, inside) {
    let n = 0;
    for (let sy = 0.125; sy < 1; sy += 0.25) for (let sx = 0.125; sx < 1; sx += 0.25) n += inside(x + sx, y + sy) ? 1 : 0;
    return n / 16;
  }

  // -- the Zs ------------------------------------------------------------------------------------
  function classic() {   // Z, Z Z, Z Z Z, then they fade from the left
    return loop((f, u, t) => {
      for (let k = 0; k < 3; k++) {
        const on = 900 + k * 900, off = 3900 + k * 600;
        zed(f, 17 + k * 13, 13 - k, 5, ease((t - on) / 300) * (1 - ease((t - off) / 400)));
      }
    });
  }

  function typewriter() {   // Z Z Z typed out beside a blinking cursor, held, then faded
    const keys = [700, 1400, 2200];   // each lands as the cursor blinks off, so it never flashes
    return loop((f, u, t) => {
      const fade = 1 - ease((t - 4800) / 700);
      keys.forEach((at, k) => { if (t >= at) zed(f, 18 + k * 10, 12, 5, 0.95 * fade * ease((t - at) / 100)); });
      const typed = keys.filter((at) => t >= at).length;
      if (t >= 200 && t < 4800 && Math.floor((t - 200) / 400) % 2 === 0) {   // starts dark, so the loop closes
        const x = 18 + typed * 10;
        f.light((g) => g.rect(x, 18, 4, 1, '#7a5a30'), 1, 1);
        f.rect(x, 18, 4, 1, '#ffd79a');
      }
    });
  }

  function bubbles() {   // Zs in bubbles float up and away, bubble and Z fading together
    return loop((f, u) => {
      for (let k = 0; k < 3; k++) {
        const p = (u + k / 3) % 1, x = 12 + Math.round(p * 40), y = 24 - Math.round((x - 12) * 22 / 40), v = Math.sin(Math.PI * p);   // y follows x: one pixel at a time
        const ring = (g, c) => g.oval(x - 3, y - 3, x + 7, y + 7, c, false);
        f.light((g) => ring(g, dim('#4a6aa8', v)), 1, 0.9);
        f.layer((g) => { ring(g, dim('#7fa8e8', 0.75 * v)); g.set(x - 1, y - 1, dim('#e8f2ff', v)); g.set(x - 2, y + 1, dim('#b8d0ff', 0.6 * v)); });
        zed(f, x, y, 5, v, ZZZ, 0.6);
      }
    });
  }

  function growing() {   // a Z rises and grows, small to big, fading as it goes; three take turns
    return loop((f, u) => {
      for (let k = 0; k < 3; k++) {
        const p = (u + k / 3) % 1, v = Math.sin(Math.PI * p), cy = Math.round(28 - p * 22), cx = 24 + Math.round((28 - cy) * 18 / 22);   // x follows y, so it climbs evenly
        const w5 = ease((p - 0.40) / 0.08), w7 = ease((p - 0.70) / 0.08);   // each size fades into the next
        [[4, 1 - w5], [5, w5 * (1 - w7)], [7, w7]].forEach(([size, w]) => zed(f, cx - (size - 1) / 2, cy - (size - 1) / 2, size, v * w, ZZZ, 0.7, true));
      }
    });
  }

  function spiral() {   // Zs float out from the middle on a slow spiral, growing fainter, never overlapping
    return loop((f, u) => {
      for (let k = 0; k < 4; k++) {
        const p = (u + k / 4) % 1, a = TAU * p * 0.9, rad = 3 + p * 16;
        zed(f, 30 + Math.cos(a) * rad * 1.6, 14 + Math.sin(a) * rad * 0.7, 5, Math.sin(Math.PI * Math.min(1, p * 1.3)));
      }
    });
  }

  // -- soft lights ------------------------------------------------------------------------------
  function dots() {   // three soft stars, a slow wave of light passing along them
    const star = ['.x.', 'xXx', '.x.'];
    return loop((f, u) => {
      for (let k = 0; k < 3; k++) {
        const v = 0.25 + 0.75 * breath(u, 2, -k / 6), x = 22 + k * 9, y = 15;
        glowDot(f, x + 1, y + 1, '#ffd98a', 0.75 * v, 2.6);
        f.sprite(star, x, y, { X: dim('#fff2cc', v), x: dim('#ffc870', 0.6 * v) });
      }
    });
  }

  function breathingRing() {   // a ring breathes: wider and brighter in, smaller and dimmer out
    return loop((f, u) => {
      const b = breath(u), r = 6 + 4 * b, v = 0.45 + 0.4 * b, c = mix('#5fc8ff', '#a98bff', b);
      const ring = (g, colour, width) => { for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
        const k = Math.max(0, 1 - Math.abs(Math.hypot(x - 31.5, y - 15.5) - r) / width);
        if (k > 0.02) g.set(x, y, dim(colour, k));
      } };
      f.light((g) => ring(g, dim(c, 0.5 * v), 2.2), 1, 1);
      f.light((g) => ring(g, dim(c, v), 0.9 + 0.3 * (1 - b)), 0, 1);   // a touch wider when small, so it never beads
    });
  }

  function fireflies() {   // fireflies drift on slow loops over dark grass, each glowing in its own time
    const r = rng(4), flies = Array.from({ length: 11 }, (_, k) => [6 + k * 5 + r() * 3, 4 + r() * 20, 2 + r() * 4, 1 + r() * 3,
      1 + Math.floor(r() * 2), 1 + Math.floor(r() * 3), r(), 1 + Math.floor(r() * 3)]);
    const grass = Array.from({ length: W }, (_, x) => 2 + Math.round(1.2 + Math.sin(x * 1.7) + Math.sin(x * 0.53) * 0.8));
    return loop((f, u) => {
      for (let x = 0; x < W; x++) for (let y = H - grass[x]; y < H; y++) f.set(x, y, mix('#123a1c', '#081a0e', (y - (H - grass[x])) / 4));
      flies.forEach(([cx, cy, ax, ay, fx, fy, ph, k]) => {
        const x = cx + ax * Math.sin(TAU * (fx * u + ph)), y = cy + ay * Math.sin(TAU * (fy * u + ph * 2));
        const v = 0.15 + 0.85 * Math.max(0, Math.sin(TAU * (k * u + ph))) ** 3;
        glowDot(f, x, y, '#b8ff5a', 0.7 * v, 2.4);
        f.set(x, y, mix('#5a7a2a', '#f2ffb0', v));
      });
    });
  }

  // -- the moon ---------------------------------------------------------------------------------
  function moonHammock() {   // a crescent moon rocks gently while Zs drift up out of it
    const stars = starsOf(31, 9).filter(([x, y]) => x > 40 || y < 4);
    return loop((f, u) => {
      twinkle(f, stars, u, 0.1, 0.35);
      const cx = 24 + Math.sin(TAU * u) * 1.2, inMoon = (x, y) => (x - cx) ** 2 + (y - 16) ** 2 <= 49 && (x - cx - 3) ** 2 + (y - 14) ** 2 > 36;
      const moon = new Frame();
      for (let y = 7; y < 26; y++) for (let x = 14; x < 36; x++) { const k = cover(x, y, inMoon); if (k > 0) moon.set(x, y, dim(mix('#fff4cc', '#e2b866', (y - 8) / 17), k)); }
      f.light((g) => { g.px = moon.px.map((c) => (c ? dim(c, 0.45) : null)); }, 3, 0.9);
      T.addLight(f.px, moon.px, 1);
      for (let k = 0; k < 2; k++) { const p = (u * 2 + k / 2) % 1, y = Math.round(14 - p * 12); zed(f, 31 + Math.round((14 - y) * 10 / 12), y, 4, 0.9 * Math.sin(Math.PI * p)); }
    });
  }

  function moonPhases() {   // the moon goes through its phases, full to new and back, beside a twinkling star
    return loop((f, u) => {
      const k = Math.cos(TAU * u), cx = 26, cy = 16, r = 7;
      let lit = 0;
      const moon = new Frame();
      for (let y = cy - r; y <= cy + r; y++) for (let x = cx - r; x <= cx + r; x++) {
        const dx = x - cx, dy = y - cy;
        if (dx * dx + dy * dy > r * r + 2) continue;
        const half = Math.sqrt(Math.max(0, r * r + 2 - dy * dy));
        const reach = (half + 0.6) * k, clamp01 = (v) => Math.max(0, Math.min(1, v));
        const edge = u < 0.5 ? reach - dx : dx + reach;   // waning shrinks to the left, waxing grows from the right
        const l = Math.min(clamp01(half + 0.5 - Math.abs(dx)), clamp01(edge + 0.5));   // soft on both rims, so full and new never twitch
        lit += l;
        moon.set(x, y, mix('#141826', mix('#f6eed2', '#cdbf96', (dy + r) / (2 * r)), l));
      }
      const full = lit / (Math.PI * r * r);
      f.light((g) => g.disc(cx, cy, r + 1, dim('#6a6448', 0.6 * full)), 3, 1);
      T.over(f.px, moon.px);
      const tw = breath(u, 3);
      glowDot(f, 44, 22, '#c8d4ff', 0.5 * tw, 2);
      f.sprite(['.x.', 'xXx', '.x.'], 43, 21, { X: dim('#f0f4ff', 0.3 + 0.6 * tw), x: dim('#a8b8ff', 0.2 + 0.4 * tw) });
      f.set(14, 6, dim('#cfd8ff', 0.2 + 0.3 * breath(u, 2, 0.3)));
    });
  }

  // -- night scenes -----------------------------------------------------------------------------
  function countingSheep() {   // sheep trot in, leap the fence and trot out under a little moon
    const stars = starsOf(12, 10, 14);
    const fleece = ['.o.oo.', 'oooooo', 'oooooo', '.oooo.'];   // woolly on top
    return loop((f, u, t) => {
      twinkle(f, stars, u, 0.12, 0.32);
      const moon = new Frame(), inMoon = (x, y) => (x - 9) ** 2 + (y - 6) ** 2 <= 12.5 && (x - 10.6) ** 2 + (y - 4.8) ** 2 > 9;
      for (let y = 1; y < 11; y++) for (let x = 4; x < 14; x++) { const k = cover(x, y, inMoon); if (k > 0) moon.set(x, y, dim('#f4ecc8', k)); }
      f.light((g) => { g.px = moon.px.map((c) => (c ? dim(c, 0.4) : null)); }, 2, 1);
      T.addLight(f.px, moon.px, 1);
      for (let y = 28; y < H; y++) for (let x = 0; x < W; x++) f.set(x, y, mix('#1c4426', '#0a1a10', (y - 28) / 3));
      f.layer((g) => {   // the fence: two posts and two rails
        g.rect(31, 21, 1, 7, '#7a5a3c'); g.rect(35, 21, 1, 7, '#7a5a3c');
        g.rect(30, 22, 7, 1, '#9a7450'); g.rect(30, 25, 7, 1, '#8a6646');
      });
      for (let k = 0; k < 2; k++) {
        const p = ((u + k / 2) % 1) * 2 - 0.5;   // -0.5 .. 1.5: one sheep crossing at a time, 3 s each
        if (p < 0 || p > 1) continue;
        const x = Math.round(-9 + p * 74), jump = Math.min(1, Math.max(0, (p - 0.40) / 0.26));
        const hop = jump > 0 && jump < 1 ? Math.sin(Math.PI * jump) * 10 : 0, y = Math.round(23 - hop);
        const step = Math.floor(t / 250) % 2;
        const legs = hop > 1 ? [0, 5] : step ? [1, 4] : [2, 5];    // stretched mid-leap, else trotting
        f.layer((g) => {
          g.sprite(fleece, x, y, { o: '#e6e6ee' });
          g.grad([x, y], [x + 5, y + 3], ['#ffffff', '#b4b4c4'], { area: 'drawn' });
        });
        f.layer((g) => {
          const down = hop > 1 ? 0 : step;   // her head nods as she trots
          g.rect(x + 6, y + down, 2, 2, '#6a6a78'); g.set(x + 5, y + down, '#7a7a88');   // head, and an ear lying back
          g.set(x + 7, y + down, '#2a2a32');                                              // her eye
          legs.forEach((dx) => g.set(x + dx, y + 4, '#7a7a88'));
          if (hop <= 1) legs.forEach((dx) => { const far = dx < 3 ? (dx === 1 ? 2 : 1) : (dx === 4 ? 5 : 4); g.set(x + far, y + 4, '#4a4a56'); });   // the far legs, a gap between back and front
        });
      }
    });
  }

  function shootingStar() {   // a quiet sky of twinkling stars over the hills; once a loop a star shoots across
    const stars = starsOf(9, 26, 24);
    return loop((f, u, t) => {
      twinkle(f, stars, u, 0.18, 0.7);
      for (let x = 0; x < W; x++) { const top = 28 + Math.round(Math.sin(x / 9) * 1.5 + Math.sin(x / 4.3) * 0.6);
        for (let y = top; y < H; y++) f.set(x, y, mix('#141c3a', '#070a18', (y - top) / 4)); }
      const p = (t % LOOP) / 1100;
      if (p <= 1) {
        const hx = -6 + p * 62, hy = 2 + p * 14, fade = Math.min(1, p / 0.15) * (1 - p ** 4);
        f.light((g) => g.stroke(hx - 12, hy - 3.1, hx, hy, dim('#5a74c8', fade), 1), 1, 1);
        const trail = new Frame();
        for (let s = 0; s < 14; s++) trail.set(hx - s, hy - s * 0.26, dim(mix('#9ab0ff', '#ffffff', 1 - s / 14), fade * (1 - s / 14)));
        T.addLight(f.px, trail.px, 1);
        glowDot(f, hx, hy, '#ffffff', 0.6 * fade, 1.6);
      }
    });
  }

  function aurora() {   // the Northern lights preset, made for the night: one ribbon rippling slowly from green to teal,
    // a few stars, and the preset's ridge of pines black against a thin glow along the treetops. The sky stays black (no glow over
    // the whole panel), the ribbon is narrow with a clean edge (no faint fringe for a dim panel to flicker on), and
    // the glow holds still, so all that moves is the light itself.
    const stars = starsOf(27, 4, 4);
    const r = rng(12), trees = [];   // pines of a few heights, as the preset's ridge but less even, so they read as a forest
    for (let x = 1 + Math.floor(r() * 3); x < W + 4; x += 6 + Math.floor(r() * 3)) trees.push([x, 5 + Math.floor(r() * 5)]);
    const ridge = Array.from({ length: W }, (_, x) => Math.min(29, ...trees.map(([cx, h]) => 29 - h + 2 * Math.abs(x - cx))));
    const FLOOR = 22;   // below this a colour is left off: on a dim panel it would only shimmer at random
    return loop((f, u) => {
      for (let x = 0; x < W; x++) for (let d = 1; d <= 2; d++) f.set(x, ridge[x] - d, dim('#2c8a90', 0.53 - 0.11 * d));   // a thin glow along the treetops
      f.light((g) => { for (let x = 0; x < W; x++) {
        const centre = 8 + 3 * Math.sin(TAU * (x / 44 + u)) + 0.8 * Math.sin(x / 5 - TAU * u);
        const shimmer = 0.74 + 0.26 * Math.sin(x / 4 + TAU * 2 * u);   // rays drifting along it
        const colour = hsv(0.39 + 0.09 * Math.sin(x / 13 + TAU * u), 0.75, 1);
        for (let y = 0; y < ridge[x]; y++) {
          const k = Math.max(0, 1 - Math.abs(y - centre) / 4), v = 0.8 * k * k * (3 - 2 * k) * shimmer;
          if (v > 0.18) g.set(x, y, dim(colour, v));
        }
      } }, 1, 0.85);
      stars.forEach(([x, y, ph, k]) => {   // behind the ribbon: a star fades out as the light passes over it, never adds to it
        const here = f.get(x, y), lit = here ? Math.max(...[1, 3, 5].map((j) => parseInt(here.slice(j, j + 2), 16))) : 0;
        const v = (0.22 + 0.3 * breath(u, k, ph)) * Math.max(0, 1 - lit / 40);
        if (v > 0) f.set(x, y, here ? mix(here, '#cfd8ff', v * (1 - lit / 255)) : dim('#cfd8ff', v));
      });
      for (let i = 0; i < W * H; i++) {
        const c = f.px[i], y = Math.floor(i / W);
        if (y >= ridge[i % W] || (c && Math.max(...[1, 3, 5].map((k) => parseInt(c.slice(k, k + 2), 16))) < FLOOR)) f.px[i] = null;
      }
    });
  }

  function rain() {   // soft rain falling onto a puddle, rings spreading where the drops land
    const r = rng(2), drops = Array.from({ length: 20 }, (_, k) => [Math.floor(k * 3.2 + r() * 2), r(), 2 + Math.floor(r() * 2)]);   // one a column-pair apart
    return loop((f, u) => {
      for (let y = H - 2; y < H; y++) for (let x = 0; x < W; x++) f.set(x, y, dim(mix('#2a3a66', '#1a2648', y - (H - 2)), 0.7 + 0.3 * breath(u, 2, x / 64 + y / 7)));
      drops.forEach(([x0, ph, speed]) => {
        const q = (u * speed + ph) % 1, y = q * (H + 8) - 3, x = x0;   // falls, then (past the puddle) its ring spreads
        if (y < H - 2) {
          f.light((g) => g.set(x, y, '#2a3e6a'), 1, 1);
          for (let s = 0; s < 4; s++) f.set(x, y - s, mix('#7096d0', '#22305a', s / 4));
        } else {
          const age = (y - (H - 2)) / 6;   // 0 .. 1
          if (age <= 1) { const w = 1 + age * 3, v = 0.8 * (1 - age);
            [[-w, H - 2], [w, H - 2], [-w * 0.6, H - 1], [w * 0.6, H - 1]].forEach(([dx, yy]) => f.set(x + dx, yy, mix(f.get(Math.round(x + dx), yy) || '#000000', '#a8c8ff', v))); }
        }
      });
    });
  }

  function candle() {   // a little candle, its flame flickering gently, a warm glow breathing around it
    return loop((f, u) => {
      const flick = 0.9 + 0.06 * Math.sin(TAU * 3 * u) + 0.04 * Math.sin(TAU * 5 * u + 1.3);
      const sway = Math.sin(TAU * 2 * u) * 0.5 + Math.sin(TAU * 4 * u + 0.7) * 0.15;
      f.light((g) => { g.oval(18, 4, 46, 30, dim('#6a3010', 0.8 * flick)); g.oval(24, 8, 40, 24, dim('#a8501a', 0.6 * flick)); }, 3, 1);
      f.light((g) => g.oval(20, 28, 44, 31, dim('#5a2a0a', flick)), 1, 1);   // light pooling on the table
      f.layer((g) => {
        g.rect(29, 19, 7, 11, '#e0d4bc');
        g.grad([29, 19], [35, 19], ['#e8dcc4', '#cbbd9e', '#8e7c5e'], { area: 'drawn' });
        g.set(30, 20, '#efe6d2'); g.rect(33, 20, 1, 3, '#ddd0b6');   // a drip
        g.set(32, 18, '#2a2018');                                    // the wick
      });
      const flame = new Frame();
      for (let y = 9; y <= 17; y++) {
        const p = (y - 9) / 8, fx = 32 + sway * (1 - 0.8 * p), half = Math.sin(Math.PI * Math.min(1, p * 1.1)) * 2.1 * flick;
        for (let x = Math.floor(fx - half - 1); x <= Math.ceil(fx + half + 1); x++) {
          const k = 1 - Math.abs(x - fx) / (half + 0.6);
          if (k <= 0) continue;
          const heat = k * (0.4 + 0.6 * p);
          const c = mix(mix('#c8401a', '#ffc65a', ease(heat / 0.5)), '#fff6c8', ease((heat - 0.5) / 0.3));
          flame.set(x, y, dim(c, Math.min(1, k * 1.6)));   // its edges fade out, so it never pops
        }
      }
      f.light((g) => { g.px = flame.px.map((c) => (c ? dim(c, 0.6) : null)); }, 2, 0.9);
      T.addLight(f.px, flame.px, 1);
    });
  }

  function snow() {   // snow drifts down and settles on the bank, melting into it
    const bank = Array.from({ length: W }, (_, x) => 1 + (Math.sin(x / 5) + Math.sin(x / 2.3)) * 0.5);
    const r = rng(6), flakes = Array.from({ length: 26 }, (_, k) => [3 + k * 2.3 + r() * 1.5, r(), 1 + Math.floor(r() * 2), 1 + r() * 1.5]);
    const rest = (x) => Math.floor(31 - bank[Math.max(0, Math.min(W - 1, Math.round(x)))]) - 1;   // the row just above the bank
    flakes.forEach((fl) => {   // where each one touches down, worked out once, so a resting flake never hops
      const [x0, ph, , sway] = fl, at = (q) => x0 + Math.sin(TAU * (q * 2 + ph)) * sway;
      let q = 0; while (q < 1 && q * 34 - 2 < rest(at(q))) q += 1 / 680;
      fl.push(Math.round(at(q)), rest(Math.round(at(q))));
    });
    const trees = [[8, 10], [13, 7], [50, 11], [56, 8]];
    return loop((f, u) => {
      pines(f, trees, '#1a2a50');
      for (let x = 0; x < W; x++) for (let y = Math.floor(31 - bank[x]); y < H; y++) f.set(x, y, mix('#7f8db0', '#46526e', (y - 29) / 3));
      f.light((g) => { for (let x = 0; x < W; x++) g.set(x, Math.floor(31 - bank[x]) - 1, '#141a2c'); }, 1, 1);
      flakes.forEach(([x0, ph, speed, sway, xLand, topLand]) => {
        const p = (u * speed + ph) % 1;
        let x = x0 + Math.sin(TAU * (p * 2 + ph)) * sway, y = p * 34 - 2, v = speed === 2 ? 0.95 : 0.65;
        if (y >= topLand) { v *= 1 - ease((y - topLand) / (32 - topLand)); x = xLand; y = topLand; }   // resting, melting away
        if (v > 0.02) f.set(x, y, mix(f.get(Math.round(x), Math.round(y)) || '#000000', '#f2f6ff', v));   // over what's there, never darker
      });
    });
  }

  // id: the built-in file it becomes (resources/media/built-in/sleep_<id>.gif)
  const S = (id, name, make, about) => Object.assign(P('sleep-' + id, name, 'Sleep screens', make, about), { sleep: id });
  window.ANIMATOR_PRESETS.push(
    S('default', 'Z Z Z', classic, 'Z, Z Z, Z Z Z, then they fade.'),
    S('typewriter', 'Typed Zs', typewriter, 'Z Z Z typed out beside a blinking cursor.'),
    S('bubble', 'Z bubbles', bubbles, 'Zs in bubbles floating up and away.'),
    S('tiny_big', 'Growing Zs', growing, 'Zs rising and growing as they go.'),
    S('spiral', 'Z spiral', spiral, 'Zs drifting out on a slow spiral.'),
    S('dots', 'Three stars', dots, 'A slow wave of light along three soft stars.'),
    S('breathing_ring', 'Breathing ring', breathingRing, 'A glowing ring breathing in and out.'),
    S('fireflies', 'Fireflies', fireflies, 'Fireflies glowing over dark grass.'),
    S('moon_hammock', 'Rocking moon', moonHammock, 'A crescent moon rocking, Zs drifting up.'),
    S('moon_phases', 'Moon phases', moonPhases, 'The moon waning and waxing beside a star.'),
    S('counting_sheep', 'Counting sheep', countingSheep, 'Sheep leaping a fence under a little moon.'),
    S('shooting_star', 'Shooting star', shootingStar, 'Twinkling stars over the hills, one shooting by.'),
    S('aurora', 'Aurora', aurora, 'Northern lights rippling slowly over the pines.'),
    S('rain', 'Rain', rain, 'Soft rain rippling a puddle.'),
    S('candle', 'Candle', candle, 'A flickering candle and its warm glow.'),
    S('snow', 'Snow', snow, 'Snow drifting down onto a bank.'),
  );
})();
