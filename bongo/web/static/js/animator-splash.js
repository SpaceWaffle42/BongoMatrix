// The built-in splash screens, as Animator presets ("Splash screens"): any of them can be opened and changed in
// the Animator, and tools/build_sleep.py turns these same drawings into resources/media/built-in/splash_*_squares.gif.
// Every one builds the same picture, the Bongo Matrix tile wall (LAYOUT: 16 x 8 tiles in eight DawnBringer
// colours, each a 3 x 3 tile with a darker centre on a 4 px grid), holds it, and takes it apart again, each in
// its own way. Frames are 20 ms (50 fps); identical frames are merged into one longer frame.
// These were tools/build_splash.py until 14 Nov 2026 and draw exactly what it drew, pixel for pixel: that is why
// rounding and the random numbers below work as Python's do (tests/test_build_splash.py holds them to the files).
(function () {
  const { W, H, Frame, P } = window.AnimatorKit;
  const COLS = 16, ROWS = 8, PITCH = 4;
  const STEP = 20;      // ms per frame
  const HOLD = 2200;    // ms the finished wall is held
  const TAIL = 200;     // ms of black at the end before it loops

  // [face, centre] for each tile colour (DawnBringer 32)
  const TILES = {
    B: ['#639bff', '#5b6ee1'], R: ['#d95763', '#ac3232'], T: ['#d9a066', '#df7126'], P: ['#d77bba', '#d95763'],
    Y: ['#fbf236', '#d9a066'], N: ['#8f563b', '#663931'], V: ['#306082', '#3f3f74'], G: ['#6abe30', '#4b692f'],
    W: ['#ffffff', '#cbdbfc'],   // white: Tetris's line-clear flash, balls, the snake's head
    K: ['#454558', '#22222c'],   // card back: a dim grey, unlike any wall colour
  };
  const LAYOUT = ['PGVRTYBNYPGTNVRB', 'TNBYVGVRBYTRBPGT', 'BRTGBRPTGPBNTYBR', 'YNBPRPNBYGRYGVTP',
    'PTVGYVNYBNVRPBNY', 'RBYTNBPGTPRBRTRB', 'TGPBRTYBRVGVYBNT', 'BRVNTGPYNBYTRVGP'];
  const CELLS = [];
  for (let r = 0; r < ROWS; r++) for (let c = 0; c < COLS; c++) CELLS.push([c, r]);
  const CX = (COLS - 1) / 2, CY = (ROWS - 1) / 2;
  const key = (c, r) => c + ',' + r;
  const home = (c, r) => [c * PITCH, r * PITCH];
  const easeOut = (u) => 1 - (1 - u) ** 3;
  const easeIn = (u) => u ** 3;
  const easeIO = (u) => (u < 0.5 ? 4 * u ** 3 : 1 - (-2 * u + 2) ** 3 / 2);
  const clamp = (u) => (u < 0 ? 0 : u > 1 ? 1 : u);
  const round = (x) => { const f = Math.floor(x), d = x - f; return d > 0.5 ? f + 1 : d < 0.5 ? f : f % 2 === 0 ? f : f + 1; };   // halves go to the even number
  const hyp = (a, b) => Math.sqrt(a * a + b * b);
  const mod = (a, n) => ((a % n) + n) % n;
  const range = (a, b, step = 1) => { const out = []; for (let t = a; step > 0 ? t < b : t > b; t += step) out.push(t); return out; };

  // the same numbers as Python's random.Random(seed): the Mersenne Twister
  function Random(seed) {
    const mt = new Uint32Array(624);
    let at = 624;
    mt[0] = 19650218;
    for (let i = 1; i < 624; i++) { const x = mt[i - 1] ^ (mt[i - 1] >>> 30); mt[i] = Math.imul(1812433253, x) + i; }
    let i = 1;
    for (let k = 624; k; k--) {
      const x = mt[i - 1] ^ (mt[i - 1] >>> 30); mt[i] = (mt[i] ^ Math.imul(x, 1664525)) + seed;
      if (++i >= 624) { mt[0] = mt[623]; i = 1; }
    }
    for (let k = 623; k; k--) {
      const x = mt[i - 1] ^ (mt[i - 1] >>> 30); mt[i] = (mt[i] ^ Math.imul(x, 1566083941)) - i;
      if (++i >= 624) { mt[0] = mt[623]; i = 1; }
    }
    mt[0] = 0x80000000;
    const u32 = () => {
      if (at >= 624) {
        for (let k = 0; k < 624; k++) {
          const y = (mt[k] & 0x80000000) | (mt[(k + 1) % 624] & 0x7fffffff);
          mt[k] = mt[(k + 397) % 624] ^ (y >>> 1) ^ (y & 1 ? 0x9908b0df : 0);
        }
        at = 0;
      }
      let y = mt[at++];
      y ^= y >>> 11; y ^= (y << 7) & 0x9d2c5680; y ^= (y << 15) & 0xefc60000; y ^= y >>> 18;
      return y >>> 0;
    };
    const random = () => { const a = u32() >>> 5, b = u32() >>> 6; return (a * 67108864 + b) / 9007199254740992; };
    const below = (n) => { const bits = 32 - Math.clz32(n); let v = u32() >>> (32 - bits); while (v >= n) v = u32() >>> (32 - bits); return v; };
    return {
      random, below,
      uniform: (a, b) => a + (b - a) * random(),
      choice: (list) => list[below(list.length)],
      shuffle: (list) => { for (let k = list.length - 1; k > 0; k--) { const j = below(k + 1); [list[k], list[j]] = [list[j], list[k]]; } },
      sample: (list) => { const pool = [...list], n = pool.length, out = []; for (let k = 0; k < n; k++) { const j = below(n - k); out.push(pool[j]); pool[j] = pool[n - k - 1]; } return out; },   // all of them, in a random order
    };
  }

  // -- drawing ---------------------------------------------------------------------------------
  // sprites: [x, y, colour key, size]; size 3 is a whole tile, 2 the tile edge-on (a one-pixel bar), 1 a dot (its
  // face colour) in the tile's centre. Size 0 with key "W" is a single white pixel (a ball or a bullet).
  // Positions are the tile's top-left, in pixels.
  function draw(sprites) {
    const px = new Array(W * H).fill(null);
    const set = (x, y, c) => { if (x >= 0 && x < W && y >= 0 && y < H) px[y * W + x] = c; };
    for (const [fx, fy, k, size] of sprites) {
      const [face, centre] = TILES[k], x = round(fx), y = round(fy);
      if (size === 0) set(x, y, face);
      else if (size >= 3) { for (let dy = 0; dy < 3; dy++) for (let dx = 0; dx < 3; dx++) set(x + dx, y + dy, dx === 1 && dy === 1 ? centre : face); }
      else if (size === 2) { for (let dy = 0; dy < 3; dy++) set(x + 1, y + dy, face); }   // edge-on, mid-flip
      else if (size >= 1) set(x + 1, y + 1, face);
    }
    return px;
  }
  const wall = () => CELLS.map(([c, r]) => [...home(c, r), LAYOUT[r][c], 3]);

  // -- the generic engine: every tile has its own start, length and path -------------------------
  // plan(c, r) -> [start ms, length ms, path(u, waiting) -> [x, y, size] or null]
  function perTile(plan) {
    const plans = CELLS.map(([c, r]) => [c, r, ...plan(c, r)]);
    const end = Math.max(...plans.map(([, , s, d]) => s + d));
    return range(0, Math.trunc(end) + STEP, STEP).map((t) => {
      const sprites = [];
      for (const [c, r, s, d, path] of plans) {
        const u = d ? clamp((t - s) / d) : t >= s ? 1 : 0;
        const spot = path(u, t < s);
        if (spot) sprites.push([spot[0], spot[1], LAYOUT[r][c], spot[2]]);
      }
      return sprites;
    });
  }
  // a path for building in: from src (pixels) to home; hidden before it starts. pop: a dot, then the tile
  const arrive = (src, ease = easeOut, pop = false) => (c, r) => {
    const [hx, hy] = home(c, r), [sx, sy] = pop ? [hx, hy] : src(c, r);
    return (u, waiting) => {
      if (waiting) return null;
      if (pop) return [hx, hy, u < 0.5 ? 1 : 3];
      const e = ease(u);
      return [sx + (hx - sx) * e, sy + (hy - sy) * e, 3];
    };
  };
  // a path for taking apart: from home to dst; gone once it has arrived
  const leave = (dst, ease = easeIn, pop = false) => (c, r) => {
    const [hx, hy] = home(c, r), [tx, ty] = pop ? [hx, hy] : dst(c, r);
    return (u, waiting) => {
      if (waiting) return [hx, hy, 3];
      if (u >= 1) return null;
      if (pop) return [hx, hy, u < 0.5 ? 3 : 1];
      const e = ease(u);
      return [hx + (tx - hx) * e, hy + (ty - hy) * e, 3];
    };
  };
  const POP_IN = arrive(null, easeOut, true), POP_OUT = leave(null, easeIn, true);
  // order(c, r) -> start ms; every tile takes `length` ms along path(c, r)
  const plan = (order, length, path) => (c, r) => [order(c, r), length, path(c, r)];
  const dist = (c, r) => hyp(c - CX, (r - CY) * 2);   // the panel is twice as wide as tall

  // -- the splash screens ----------------------------------------------------------------------
  function breathing() {
    // dots open out from the middle into tiles, then breathe back into dots and fade
    return [perTile(plan((c, r) => dist(c, r) * 70, 260, POP_IN)),
      perTile(plan((c, r) => (dist(0, 0) - dist(c, r)) * 70, 260, POP_OUT))];
  }

  function burst() {
    // a ring opens from the centre, tiles popping in as it passes; then they fly out radially
    const away = (c, r) => { const [hx, hy] = home(c, r), a = Math.atan2((r - CY) * 2, c - CX + 1e-6); return [hx + Math.cos(a) * 36, hy + Math.sin(a) * 36]; };
    return [perTile(plan((c, r) => dist(c, r) * 90, 200, POP_IN)),
      perTile(plan((c, r) => (dist(0, 0) - dist(c, r)) * 70, 700, leave(away)))];
  }

  function cascade() {
    // columns pour in from the top, the middle first, in a V; then fall out the bottom
    return [perTile(plan((c, r) => Math.abs(c - CX) * 110 + (ROWS - 1 - r) * 70, 420, arrive((c, r) => [home(c, r)[0], -6 - (ROWS - r) * 4], easeOut))),
      perTile(plan((c, r) => Math.abs(c - CX) * 110 + (ROWS - 1 - r) * 60, 420, leave((c, r) => [home(c, r)[0], H + 4 + r * 2])))];
  }

  function diagonalWave() {
    // a diagonal sweep from the top-left corner, then the same sweep takes them away
    const d = (c, r) => (c + r * 2) * 55;
    return [perTile(plan(d, 200, POP_IN)), perTile(plan(d, 200, POP_OUT))];
  }

  function domino() {
    // rows drop in from the top, stacking up from the floor; then the columns topple off the bottom, left to right
    // (the bottom row lands first, so nothing falls through tiles already in place)
    return [perTile(plan((c, r) => (ROWS - 1 - r) * 200 + c * 18, 360, arrive((c, r) => [home(c, r)[0], -4]))),
      perTile(plan((c, r) => c * 110 + (ROWS - 1 - r) * 25, 380, leave((c, r) => [home(c, r)[0], H + 6])))];
  }

  function implosion() {
    // tiles rush in from all sides towards the middle; then blow back out
    const edge = (c, r) => { const [hx, hy] = home(c, r), a = Math.atan2((r - CY) * 2, c - CX + 1e-6); return [hx + Math.cos(a) * 24, hy + Math.sin(a) * 24]; };
    return [perTile(plan((c, r) => (dist(0, 0) - dist(c, r)) * 90, 600, arrive(edge))),
      perTile(plan((c, r) => dist(c, r) * 60, 600, leave(edge)))];
  }

  function loading() {
    // a loading bar runs along the middle row, then the wall opens up and down from it, each row
    // sliding out of the one before; taking apart, the rows fold back into the bar, which runs out
    const mid = Math.floor(ROWS / 2), bar = 70, row = 80;
    const gap = row + 40;           // a row sets off only once the one before it has landed
    const opened = COLS * bar + 60;
    const nearer = (r) => (r > mid ? r - 1 : r + 1);
    const opening = (c, r) => (r === mid ? [c * bar, 120, POP_IN(c, r)]
      : [opened + (Math.abs(r - mid) - 1) * gap, row, arrive((cc) => home(cc, nearer(r)))(c, r)]);
    const far = Math.max(mid, ROWS - 1 - mid);
    const closing = (c, r) => (r === mid ? [far * gap + 60 + c * 60, 120, POP_OUT(c, r)]
      : [(far - Math.abs(r - mid)) * gap, row, leave((cc) => home(cc, nearer(r)))(c, r)]);
    return [perTile(opening), perTile(closing)];
  }

  function reverseGravity() {
    // tiles shoot up from below and settle, the middle columns first in an arch; then fly off the top
    return [perTile(plan((c, r) => Math.abs(c - CX) * 90 + r * 60, 500, arrive((c, r) => [home(c, r)[0], H + 4 + r * 3]))),
      perTile(plan((c, r) => Math.abs(c - CX) * 80 + r * 50, 500, leave((c, r) => [home(c, r)[0], -8 - (ROWS - r) * 4])))];
  }

  function snake() {
    // a white-headed snake slithers along the rows, laying the wall behind it; then it comes back round and
    // eats it up, row by row
    const path = [];
    for (let r = 0; r < ROWS; r++) for (const c of (r % 2 === 0 ? range(0, COLS) : range(COLS - 1, -1, -1))) path.push([c, r]);
    const step = 14;   // ms per cell
    const frames = (laying) => range(0, path.length * step + step, STEP).map((t) => {
      const h = Math.min(path.length, Math.floor(t / step));   // cells the head has passed
      const sprites = [];
      path.forEach(([c, r], i) => { if ((i < h) === laying) sprites.push([...home(c, r), LAYOUT[r][c], 3]); });
      if (h < path.length) sprites.push([...home(...path[h]), 'W', 3]);
      return sprites;
    });
    return [frames(true), frames(false)];
  }

  function spiral() {
    // laid along a spiral from the outer edge into the middle; then unwound the same way
    const rank = {};
    let n = 0, top = 0, left = 0, bottom = ROWS - 1, right = COLS - 1;
    const put = (c, r) => { rank[key(c, r)] = n++; };
    while (top <= bottom && left <= right) {
      for (let c = left; c <= right; c++) put(c, top);
      for (let r = top + 1; r <= bottom; r++) put(right, r);
      if (top < bottom) for (let c = right - 1; c >= left; c--) put(c, bottom);
      if (left < right) for (let r = bottom - 1; r > top; r--) put(left, r);
      top++; left++; bottom--; right--;
    }
    return [perTile(plan((c, r) => rank[key(c, r)] * 15, 180, POP_IN)), perTile(plan((c, r) => rank[key(c, r)] * 15, 180, POP_OUT))];
  }

  function tetris() {
    // A real game: tetrominoes spawn at the top, get steered across and drop a row at a time, stacking from the
    // floor into the wall. Then it's cleared like Tetris: the bottom line flashes, vanishes, and everything above
    // falls down a row, until the screen is empty.
    const rng = Random(7);
    const variants = [   // cells as [dx, dy]: I, O, T, L, S and Z, each in its rotations
      [[0, 0], [1, 0], [2, 0], [3, 0]], [[0, 0], [0, 1], [0, 2], [0, 3]],
      [[0, 0], [1, 0], [0, 1], [1, 1]],
      [[0, 0], [1, 0], [2, 0], [1, 1]], [[1, 0], [0, 1], [1, 1], [2, 1]], [[0, 0], [0, 1], [1, 1], [0, 2]], [[1, 0], [0, 1], [1, 1], [1, 2]],
      [[0, 0], [0, 1], [1, 1], [2, 1]], [[2, 0], [0, 1], [1, 1], [2, 1]], [[0, 0], [1, 0], [2, 0], [0, 1]], [[0, 0], [1, 0], [2, 0], [2, 1]],
      [[1, 0], [2, 0], [0, 1], [1, 1]], [[0, 0], [0, 1], [1, 1], [1, 2]],
      [[0, 0], [1, 0], [1, 1], [2, 1]], [[1, 0], [0, 1], [1, 1], [0, 2]],
    ];
    // fill the wall from the floor up, always the lowest free cell first, with a piece that rests on what's
    // already there (so the order is one a player could really have played)
    const filled = new Set(), pieces = [];
    const supported = (cells) => cells.every(([c, r]) => r === ROWS - 1 || filled.has(key(c, r + 1)) || cells.some(([cc, rr]) => cc === c && rr === r + 1));
    while (filled.size < CELLS.length) {
      let c0 = -1, r0 = -1;
      for (const [c, r] of CELLS) if (!filled.has(key(c, r)) && (r > r0 || (r === r0 && c < c0))) { c0 = c; r0 = r; }
      let cells = null;
      for (const v of rng.sample(variants)) {
        const fits = [];
        for (const [ax, ay] of v) {   // put each of the piece's cells on the target in turn
          const at = v.map(([dx, dy]) => [c0 + dx - ax, r0 + dy - ay]);
          if (at.every(([c, r]) => c >= 0 && c < COLS && r >= 0 && r < ROWS && !filled.has(key(c, r)))
              && supported(at) && Math.max(...at.map(([, r]) => r)) === r0) fits.push(at);
        }
        if (fits.length) { cells = rng.choice(fits); break; }
      }
      if (!cells) cells = [[c0, r0]];   // nothing fits this gap: a single tile drops in
      cells.forEach(([c, r]) => filled.add(key(c, r)));
      pieces.push(cells);
    }

    const SPAWN = 70, SHIFT_MS = 40, DROP_MS = 40;   // whole frames, so the drops are even
    const owner = {};
    pieces.forEach((cells, i) => cells.forEach(([c, r]) => { owner[key(c, r)] = i; }));
    const timeline = [], landed = [];   // per piece: [[t, dx, dy]] offsets from its final cells, in whole rows/columns
    pieces.forEach((cells, i) => {
      const cs = cells.map(([c]) => c), rs = cells.map(([, r]) => r);
      const left = Math.min(...cs), width = Math.max(...cs) - left + 1;
      let t = 0, dx = Math.floor((COLS - width) / 2) - left, dy = -(Math.max(...rs) + 1);   // just above the screen
      const steps = [[t, dx, dy]];
      while (dx || dy) {   // steer across first while falling slowly, then drop
        if (dx) {
          dx -= dx > 0 ? 1 : -1;
          if (dy < -1 && rng.random() < 0.5) dy += 1;
          t += SHIFT_MS;
        } else { dy += 1; t += DROP_MS; }
        steps.push([t, dx, dy]);
      }
      // it may only land once everything it rests on has landed
      const under = new Set(cells.map(([c, r]) => owner[key(c, r + 1)]).filter((o) => o !== undefined && o !== i));
      const start = Math.max(i * SPAWN, Math.max(0, ...[...under].map((j) => landed[j])) + 2 * STEP - t);
      timeline.push(steps.map(([st, sx, sy]) => [start + st, sx, sy]));
      landed.push(start + t);
    });

    const end = Math.max(...timeline.map((steps) => steps[steps.length - 1][0])) + 200;
    const building = range(0, end, STEP).map((t) => {
      const sprites = [];
      pieces.forEach((cells, i) => {
        const steps = timeline[i];
        if (t < steps[0][0]) return;
        const [, dx, dy] = steps.filter((s) => s[0] <= t).pop();
        cells.forEach(([c, r]) => sprites.push([...home(c + dx, r + dy), LAYOUT[r][c], 3]));
      });
      return sprites;
    });

    // line clears, bottom first: the line flashes and goes, everything above falls one row
    let rows = [...LAYOUT];   // null for an empty line
    const taking = [];
    const tiles = (flash = null, fallingAbove = null, fall = 0) => {
      const out = [];
      rows.forEach((line, r) => {
        if (line === null) return;
        const y = r * PITCH + (fallingAbove !== null && r < fallingAbove ? fall * PITCH : 0);
        [...line].forEach((k, c) => out.push([c * PITCH, y, r === flash ? 'W' : k, 3]));
      });
      return out;
    };
    while (rows.some(Boolean)) {
      const bottom = rows.reduce((b, line, r) => (line ? r : b), -1);
      for (let blink = 0; blink < 4; blink++) for (let k = 0; k < 60 / STEP; k++) taking.push(tiles(blink % 2 === 0 ? bottom : null));
      rows[bottom] = null;
      for (const t of range(0, 100, STEP)) taking.push(tiles(null, bottom, easeIn(t / 100)));
      rows = [null, ...rows.slice(0, bottom), ...rows.slice(bottom + 1)];
    }
    return [building, taking];
  }

  function vine() {
    // a vine grows from the bottom middle, branching through the wall; then withers back to its root
    const root = [Math.floor(COLS / 2), ROWS - 1];
    const depth = { [key(...root)]: 0 }, todo = [root];
    const rng = Random(3);
    while (todo.length) {
      const [c, r] = todo.shift();
      const nbrs = [[c + 1, r], [c - 1, r], [c, r - 1], [c, r + 1]];
      rng.shuffle(nbrs);
      for (const [nc, nr] of nbrs) {
        if (nc >= 0 && nc < COLS && nr >= 0 && nr < ROWS && !(key(nc, nr) in depth)) {
          depth[key(nc, nr)] = depth[key(c, r)] + 1 + (rng.random() < 0.25 ? 1 : 0);
          todo.push([nc, nr]);
        }
      }
    }
    const most = Math.max(...Object.values(depth));
    return [perTile(plan((c, r) => depth[key(c, r)] * 90, 220, POP_IN)),
      perTile(plan((c, r) => (most - depth[key(c, r)]) * 90, 220, POP_OUT))];
  }

  function vortex() {
    // tiles swirl in from outside, spiralling down onto their places; then spin away
    const swirl = (inward) => (c, r) => {
      const [hx, hy] = home(c, r);
      const a0 = Math.atan2((hy - CY * PITCH) * 2, hx - CX * PITCH + 1e-6);
      const r0 = hyp(hx - CX * PITCH, (hy - CY * PITCH) * 2);
      return (u, waiting) => {
        if (waiting) return inward ? null : [hx, hy, 3];
        if (!inward && u >= 1) return null;
        const k = inward ? 1 - easeOut(u) : easeIn(u);
        const a = a0 + k * 2.4 * (inward ? 1 : -1), rad = r0 + k * 24;
        return [CX * PITCH + Math.cos(a) * rad, CY * PITCH + Math.sin(a) * rad / 2, 3];
      };
    };
    return [perTile(plan((c, r) => (dist(0, 0) - dist(c, r)) * 80, 900, swirl(true))),
      perTile(plan((c, r) => dist(c, r) * 70, 900, swirl(false)))];
  }

  function waterfall() {
    // it pours in from the top-left: each column fills up from the bottom as the flow moves right;
    // then it drains out of the bottom the same way
    const flow = (c, r) => c * 95 + (ROWS - 1 - r) * 40;
    return [perTile(plan(flow, 360, arrive((c, r) => [home(c, r)[0], -4]))),
      perTile(plan(flow, 360, leave((c, r) => [home(c, r)[0], H + 4])))];
  }

  function weave() {
    // rows slide in from alternate sides, from the middle row outwards; then slide back out.
    // Every tile glides at the same steady speed, 4 px behind the one ahead, so they never bunch
    const PX = 5;   // ms per pixel: 4 px a frame, so every tile is on whole pixels and never touches the next
    const steady = (u) => u;
    const rank = (c, r) => (r % 2 === 0 ? c : COLS - 1 - c);   // the tile with furthest to go leads
    const into = (c, r) => {
      const sx = r % 2 ? -8 : W + 4;
      return [Math.abs(r - CY + 0.5) * 240 + rank(c, r) * 4 * PX, Math.abs(sx - home(c, r)[0]) * PX, arrive((cc, rr) => [sx, home(cc, rr)[1]], steady)(c, r)];
    };
    const out = (c, r) => {
      const tx = r % 2 ? W + 4 : -8;
      const lead = COLS - 1 - rank(c, r);                       // the tile nearest its exit leads
      return [(CY + 0.5 - Math.abs(r - CY + 0.5)) * 240 + lead * 4 * PX, Math.abs(tx - home(c, r)[0]) * PX, leave((cc, rr) => [tx, home(cc, rr)[1]], steady)(c, r)];
    };
    return [perTile(into), perTile(out)];
  }

  function zigzag() {
    // columns fill left to right, down one and up the next; then empty the same way
    const rank = (c, r) => c * ROWS + (c % 2 === 0 ? r : ROWS - 1 - r);
    return [perTile(plan((c, r) => rank(c, r) * 16, 120, POP_IN)), perTile(plan((c, r) => rank(c, r) * 16, 120, POP_OUT))];
  }

  function rubik() {
    // a scrambled wall pops in, then rows and columns slide like a Rubik's cube until it's solved;
    // taking apart, it scrambles again and the tiles blink out
    const rng = Random(11);
    const moves = [];
    for (let n = 0; n < 6; n++) {
      const kind = rng.random() < 0.5 ? 'row' : 'col';
      const i = rng.below(kind === 'row' ? ROWS : COLS);
      moves.push([kind, i, rng.choice([-1, 1])]);
    }
    const apply = (grid, [kind, i, d]) => {
      const g = grid.map((line) => [...line]);
      const turned = (a) => (d > 0 ? [a[a.length - 1], ...a.slice(0, -1)] : [...a.slice(1), a[0]]);
      if (kind === 'row') g[i] = turned(g[i]);
      else turned(g.map((line) => line[i])).forEach((k, r) => { g[r][i] = k; });
      return g.map((line) => line.join(''));
    };
    // scramble = the moves applied to the solved wall; solving plays their undos backwards
    let grid = [...LAYOUT];
    for (const m of moves) grid = apply(grid, m);
    const scrambled = grid;
    const solve = [...moves].reverse().map(([kind, i, d]) => [kind, i, -d]);
    const sliding = (g, [kind, i, d], u) => {
      const e = u * PITCH * d;   // steady: eased 4 px moves mostly round to standing still
      const out = [];
      for (const [c, r] of CELLS) {
        let [x, y] = home(c, r);
        if (kind === 'row' && r === i) {
          x = mod(x + e, COLS * PITCH);
          if (x > (COLS - 1) * PITCH) out.push([x - COLS * PITCH, y, g[r][c], 3]);   // wrapping round: draw it at both ends
        } else if (kind === 'col' && c === i) {
          y = mod(y + e, ROWS * PITCH);
          if (y > (ROWS - 1) * PITCH) out.push([x, y - ROWS * PITCH, g[r][c], 3]);
        }
        out.push([x, y, g[r][c], 3]);
      }
      return out;
    };
    const rng2 = Random(5), per = 120;
    const popIn = CELLS.map(() => rng2.random() * 700);
    const building = range(0, 900, STEP).map((t) => CELLS.map(([c, r], n) => [...home(c, r), scrambled[r][c], t - popIn[n] > 100 ? 3 : 1]).filter((s, n) => t >= popIn[n]));
    for (const m of solve) {
      for (const t of range(0, per, STEP)) building.push(sliding(grid, m, t / per));
      grid = apply(grid, m);
    }
    const taking = [];
    for (const m of moves) {
      for (const t of range(0, per, STEP)) taking.push(sliding(grid, m, t / per));
      grid = apply(grid, m);
    }
    const last = grid, popOut = CELLS.map(() => rng2.random() * 700);
    for (const t of range(0, 900, STEP)) taking.push(CELLS.map(([c, r], n) => [...home(c, r), last[r][c], popOut[n] - t > 100 ? 3 : 1]).filter((s, n) => t < popOut[n]));
    return [building, taking];
  }

  function ripple() {
    // a drop lands in the middle: rings of tiles bob up into place; then a second drop washes them away
    const bob = (arriving) => (c, r) => {
      const [hx, hy] = home(c, r);
      return (u, waiting) => {
        if (waiting) return arriving ? null : [hx, hy, 3];
        if (!arriving && u >= 1) return null;
        const lift = Math.sin(Math.PI * u);   // a pixel: any more and it touches the row above
        return [hx, hy - lift, arriving ? (u < 0.25 ? 1 : 3) : (u < 0.75 ? 3 : 1)];
      };
    };
    return [perTile(plan((c, r) => dist(c, r) * 85, 420, bob(true))), perTile(plan((c, r) => dist(c, r) * 85, 420, bob(false)))];
  }

  function shuffle() {
    // the tiles start jumbled across the wall and slide into their places; then jumble and vanish.
    // Each tile starts swapped with a neighbour (side by side or one above the other, in a checker of 2 x 2
    // blocks), so every path is one short hop that's easy to follow
    const rng = Random(21);
    const jumble = (c, r) => ((Math.floor(c / 2) + Math.floor(r / 2)) % 2 === 0 ? [c ^ 1, r] : [c, r ^ 1]);
    const later = {};
    for (const [c, r] of CELLS) later[key(c, r)] = rng.random() * 500;
    const into = (c, r) => {
      const [sx, sy] = home(...jumble(c, r)), [hx, hy] = home(c, r);
      return [later[key(c, r)], 1400, (u, waiting) => {
        if (waiting) return null;
        if (u < 0.15) return [sx, sy, u < 0.07 ? 1 : 3];
        const e = easeIO((u - 0.15) / 0.85);
        return [sx + (hx - sx) * e, sy + (hy - sy) * e, 3];
      }];
    };
    const out = (c, r) => {
      const [hx, hy] = home(c, r), [tx, ty] = home(...jumble(c, r));
      return [500 - later[key(c, r)], 1400, (u, waiting) => {
        if (waiting) return [hx, hy, 3];
        if (u >= 1) return null;
        if (u > 0.85) return [tx, ty, u < 0.93 ? 3 : 1];
        const e = easeIO(u / 0.85);
        return [hx + (tx - hx) * e, hy + (ty - hy) * e, 3];
      }];
    };
    return [perTile(into), perTile(out)];
  }

  function digitalRain() {
    // streams pour down every column, a white head leading, stacking up the wall from the floor;
    // then each column drains away downwards
    const rng = Random(8);
    const delay = range(0, COLS).map(() => rng.random() * 900);
    const speed = range(0, COLS).map(() => rng.uniform(55, 80));   // ms per row
    const falling = (c, r) => {
      const [hx, hy] = home(c, r);
      return [delay[c] + (ROWS - 1 - r) * speed[c] * 1.4, (r + 2) * speed[c],   // bottom tile first
        (u, waiting) => (waiting ? null : [hx, -4 + (hy + 4) * u, 3])];
    };
    const draining = (c, r) => [delay[c] * 0.8, 700, leave((cc, rr) => [home(cc, rr)[0], home(cc, rr)[1] + H + 4])(c, r)];
    const building = perTile(falling);
    // the white head: the first tile still falling in each column
    building.forEach((sprites, i) => {
      const t = i * STEP;
      for (let c = 0; c < COLS; c++) {
        let first = null;
        for (let r = 0; r < ROWS; r++) { const [s, d] = falling(c, r); if (s <= t && t < s + d && (!first || s < first[0])) first = [s, d, r]; }
        if (first) sprites.push([c * PITCH, -4 + (home(c, first[2])[1] + 4) * clamp((t - first[0]) / first[1]), 'W', 3]);
      }
    });
    return [building, perTile(draining)];
  }

  function cardFlip() {
    // every tile is a card dealt face down; they flip over in a wave to show the wall, and flip
    // back and are swept away at the end. A size of -1 is the card's back
    const card = (c, r, size) => [...home(c, r), size === -1 ? 'K' : LAYOUT[r][c], size === -1 ? 3 : size];
    const building = range(0, 900 + 1300, STEP).map((t) => {
      const sprites = [];
      for (const [c, r] of CELLS) {
        if (t < (c + r) * 22) continue;   // not dealt yet
        const s = 700 + (c + r * 2) * 40, u = clamp((t - s) / 160);
        sprites.push(card(c, r, t < s || u < 0.4 ? -1 : u < 0.6 ? 2 : 3));
      }
      return sprites;
    });
    const taking = range(0, 1400, STEP).map((t) => {
      const sprites = [];
      for (const [c, r] of CELLS) {
        const s = (COLS - 1 - c + (ROWS - 1 - r) * 2) * 40, u = clamp((t - s) / 220);
        if (t < s) sprites.push(card(c, r, 3));
        else if (u < 1) sprites.push(card(c, r, u < 0.3 ? 2 : u < 0.75 ? -1 : 1));
      }
      return sprites;
    });
    return [building, taking];
  }

  function breakout() {
    // the wall is laid brick by brick, row by row; then a paddle and ball knock it down, and
    // what's left crumbles away
    const building = perTile(plan((c, r) => r * 170 + c * 9, 160, POP_IN));
    const bricks = new Map(CELLS.map(([c, r]) => [key(c, r), [c, r, LAYOUT[r][c]]]));
    const taking = [], falling = [];
    const standing = () => [...bricks.values()].map(([c, r, k]) => [...home(c, r), k, 3]);
    const bat = (paddle) => range(-3, 4).map((dx) => [paddle + dx, 31, 'W', 0]);
    // the bottom row drops out first, to make room for the paddle
    for (const t of range(0, 360, STEP)) {
      taking.push([...standing().filter(([, y]) => y < (ROWS - 1) * PITCH),
        ...range(0, COLS).map((c) => [home(c, ROWS - 1)[0], home(c, ROWS - 1)[1] + easeIn(t / 360) * 8, LAYOUT[ROWS - 1][c], 3])]);
    }
    for (let c = 0; c < COLS; c++) bricks.delete(key(c, ROWS - 1));
    let bx = 30, by = 26, vx = 0.9, vy = -0.75, paddle = 30;
    for (let n = 0; n < Math.trunc(2000 / STEP); n++) {
      for (let half = 0; half < 2; half++) {   // two physics steps a frame
        let nx = bx + vx, ny = by + vy;
        if (nx < 0 || nx > W - 1) { vx = -vx; nx = bx; }
        if (ny < 0) { vy = -vy; ny = by; }
        if (ny >= 30 && vy > 0) {
          vy = -Math.abs(vy);
          vx = Math.max(-1.2, Math.min(1.2, vx + (bx - paddle) * 0.05));
          ny = by;
        }
        const cc = Math.floor(nx / PITCH), cr = Math.floor(ny / PITCH);
        if (bricks.has(key(cc, cr))) {
          falling.push([cc, cr, bricks.get(key(cc, cr))[2], taking.length]);
          bricks.delete(key(cc, cr));
          if (Math.floor(bx / PITCH) !== cc) vx = -vx; else vy = -vy;
          nx = bx; ny = by;
        }
        bx = nx; by = ny;
      }
      paddle += Math.max(-1.4, Math.min(1.4, bx - paddle));
      const sprites = standing();
      for (const [c, r, k, at] of falling) {   // a hit brick shrinks to a spark and is gone
        const age = taking.length - at;
        if (age < 4) sprites.push([...home(c, r), k, age < 2 ? 2 : 1]);
      }
      sprites.push([bx, by, 'W', 0], ...bat(paddle));
      taking.push(sprites);
      if (!bricks.size) break;
    }
    const left = [...bricks.values()];
    for (const t of range(0, 1300, STEP)) {   // the rest crumbles
      taking.push([...left.map(([c, r, k]) => [home(c, r)[0], home(c, r)[1] + easeIn(clamp((t - (ROWS - r) * 60 - c * 8) / 500)) * 40, k, 3]),
        ...(t < 500 ? bat(paddle) : [])]);
    }
    return [building, taking];
  }

  function invaders() {
    // the wall marches down from the top like space invaders, side to side a step at a time;
    // then a cannon picks a few off and the rest march away off the bottom
    const beat = 160, steps = [], building = [], taking = [];
    let dy = -(ROWS * PITCH), side = 0, n = 0;
    while (dy < 0) {
      steps.push([side, dy]);
      if (++n % 3 === 0) dy += PITCH; else side = side === 0 ? 2 : 0;
    }
    steps.push([0, 0]);
    for (const [sx, sy] of steps) {
      const sprites = CELLS.map(([c, r]) => [home(c, r)[0] + sx, home(c, r)[1] + sy, LAYOUT[r][c], 3]);
      for (let k = 0; k < beat / STEP; k++) building.push(sprites);
    }
    const rng = Random(4);
    const alive = new Map(CELLS.map(([c, r]) => [key(c, r), [c, r, LAYOUT[r][c]]]));
    const rest = () => [...alive.values()].map(([c, r, k]) => [...home(c, r), k, 3]);
    for (let shot = 0; shot < 6; shot++) {
      const col = rng.below(COLS);
      const target = Math.max(-1, ...[...alive.values()].filter(([c]) => c === col).map(([, r]) => r));
      for (const y of range(31, target >= 0 ? home(col, target)[1] + 2 : -1, -2)) {
        taking.push([...rest(), [col * PITCH + 1, y, 'W', 0], [col * PITCH, 31, 'W', 0], [col * PITCH + 1, 31, 'W', 0], [col * PITCH + 2, 31, 'W', 0]]);
      }
      if (target >= 0) {
        const k = alive.get(key(col, target))[2];
        alive.delete(key(col, target));
        for (const size of [2, 1]) taking.push([...rest(), [...home(col, target), k, size]]);
      }
    }
    let sx = 0;
    for (let m = 1; m < 10; m++) {
      sx = sx === 0 ? 2 : 0;
      const sprites = [...alive.values()].map(([c, r, k]) => [home(c, r)[0] + sx, home(c, r)[1] + m * PITCH, k, 3]);
      for (let k = 0; k < beat / STEP; k++) taking.push(sprites);
    }
    return [building, taking];
  }

  // -- one splash: build, hold, take apart, a moment of black ------------------------------------
  const lit = (px) => px.some(Boolean);
  const same = (a, b) => a.every((c, i) => c === b[i]);
  function film(make) {
    const [building, taking] = make().map((part) => part.map(draw));
    // no dead air: tiles still off-screen at the start, or already gone at the end, show nothing
    while (building.length && !lit(building[0])) building.shift();
    while (taking.length && !lit(taking[taking.length - 1])) taking.pop();
    // and the finished wall is shown for exactly HOLD: frames that already look like it belong to the hold
    const full = draw(wall()), black = draw([]);
    while (building.length && same(building[building.length - 1], full)) building.pop();
    while (taking.length && same(taking[0], full)) taking.shift();
    const merged = [];   // identical neighbours become one longer frame
    for (const [px, ms] of [[black, 100], ...building.map((p) => [p, STEP]), [full, HOLD], ...taking.map((p) => [p, STEP]), [black, TAIL]]) {
      if (merged.length && same(merged[merged.length - 1][0], px)) merged[merged.length - 1][1] += ms;
      else merged.push([px, ms]);
    }
    return merged;
  }
  // drawn once and kept: each call hands out fresh frames, and it begins and ends on black, so the loop closes
  const preset = (make) => {
    let kept = null;
    return () => {
      kept = kept || film(make);
      const at = (i) => { const [px, ms] = kept[i % kept.length], f = new Frame(ms); f.px = px.slice(); return f; };
      const out = kept.map((_, i) => at(i));
      Object.defineProperty(out, 'frameAt', { value: at });
      return out;
    };
  };

  // splash: tools/build_sleep.py writes it as resources/media/built-in/splash_<splash>_squares.gif (the same names
  // as ever, so saved settings still find them). Invaders is an idea that was never picked: a preset, but no file.
  const S = (id, name, make, about, built = true) => Object.assign(P('splash-' + id.replace(/_/g, '-'), name, 'Splash screens', preset(make), about), built ? { splash: id } : {});
  window.ANIMATOR_PRESETS.push(
    S('loading', 'Loading bar', loading, 'A bar runs along the middle, then the wall opens out from it.'),
    S('breathing', 'Breathing', breathing, 'Dots open out from the middle into tiles, then breathe back.'),
    S('burst', 'Burst', burst, 'A ring of tiles opens from the centre, then they fly out.'),
    S('cascade', 'Cascade', cascade, 'Columns pour in from the top in a V, then fall out the bottom.'),
    S('diagonal_wave', 'Diagonal wave', diagonalWave, 'A sweep from the top-left corner, and another takes it away.'),
    S('domino', 'Domino', domino, 'Rows drop in and stack up, then the columns topple away.'),
    S('implosion', 'Implosion', implosion, 'Tiles rush in from all sides, then blow back out.'),
    S('reverse_gravity', 'Reverse gravity', reverseGravity, 'Tiles shoot up from below in an arch, then fly off the top.'),
    S('rubik', 'Rubik', rubik, 'A scrambled wall slides its rows and columns until it is solved.'),
    S('snake', 'Snake', snake, 'A snake lays the wall behind it, then comes back and eats it.'),
    S('spiral', 'Spiral', spiral, 'Laid along a spiral into the middle, then unwound.'),
    S('tetris', 'Tetris', tetris, 'Pieces drop and stack into the wall, then it clears line by line.'),
    S('vine', 'Vine', vine, 'A vine grows from the bottom through the wall, then withers back.'),
    S('vortex', 'Vortex', vortex, 'Tiles swirl in onto their places, then spin away.'),
    S('waterfall', 'Waterfall', waterfall, 'It pours in from the top-left and drains out the same way.'),
    S('weave', 'Weave', weave, 'Rows slide in from alternate sides, then slide back out.'),
    S('zigzag', 'Zigzag', zigzag, 'Columns fill down one and up the next, then empty.'),
    S('ripple', 'Ripple', ripple, 'A drop lands and rings of tiles bob into place.'),
    S('shuffle', 'Shuffle', shuffle, 'Jumbled tiles hop into their places, then jumble and vanish.'),
    S('digital_rain', 'Digital rain', digitalRain, 'Streams pour down every column and stack up the wall.'),
    S('card_flip', 'Card flip', cardFlip, 'Cards are dealt face down and flip over in a wave.'),
    S('breakout', 'Breakout', breakout, 'The wall is laid brick by brick, then a ball knocks it down.'),
    S('invaders', 'Invaders', invaders, 'The wall marches down like space invaders; a cannon picks a few off.', false),
  );
})();
