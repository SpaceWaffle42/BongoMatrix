// Spread across frames: the Animator's helper for the tedious part of hand animation. It takes one layer's
// pixels and gives back the same thing for each of n frames, a step further each time: moved, faded, or with
// its colours cycled. The results are ordinary frames to touch up by hand; there is no timeline and nothing
// in between pixels (a move always lands on whole pixels).
(function () {
  const T = window.PixelTools;
  const EASES = { steady: 'Steady', in: 'Start slow', out: 'End slow', both: 'Slow at both ends' };
  const ease = (kind, t) => kind === 'in' ? t * t : kind === 'out' ? 1 - (1 - t) * (1 - t) : kind === 'both' ? (t < 0.5 ? 2 * t * t : 1 - 2 * (1 - t) * (1 - t)) : t;
  // 0..1 for each of n frames (the first is 0, the last 1)
  const steps = (n, kind) => Array.from({ length: n }, (_, k) => (n > 1 ? ease(kind, k / (n - 1)) : 0));
  // the part being changed and what stays put: all of the layer, or only what's inside the area
  function split(px, area) {
    if (!area) return { rest: new Array(px.length).fill(null), piece: { px: px.slice(), w: T.W, h: T.H }, x: 0, y: 0 };
    return { rest: T.cleared(px, area), piece: T.lifted(px, area), x: area.x, y: area.y };
  }

  // n frames of the layer with its piece moved from where it is to (dx, dy) away. With `wrap`, what goes off
  // one side comes in on the other: round the canvas, or round the selected area (so a strip can scroll inside
  // itself). `trip` (a key of TRIPS) is one full trip round in that direction, spread so that the frame after
  // the last would be the first again: a seamless loop, in even steps when n divides the distance.
  function move(px, { dx = 0, dy = 0, n = 8, easing = 'steady', area = null, wrap = false, trip = null } = {}) {
    if (trip && TRIPS[trip]) {
      const b = box(area), [sx, sy] = TRIPS[trip];
      const at = (d, t) => Math.min(Math.round(d * t), d - 1);   // an eased last frame can round to the whole trip: that would be the first frame twice
      return loopSteps(n, easing).map((t) => wrapped(px, area, b, sx * at(b.w, t), sy * at(b.h, t)));
    }
    if (wrap) { const b = box(area); return steps(n, easing).map((t) => wrapped(px, area, b, Math.round(dx * t), Math.round(dy * t))); }
    const s = split(px, area);
    return steps(n, easing).map((t) => T.placed(s.rest, s.piece, s.x + Math.round(dx * t), s.y + Math.round(dy * t)));
  }
  const TRIPS = { right: [1, 0], left: [-1, 0], down: [0, 1], up: [0, -1], downright: [1, 1], downleft: [-1, 1], upright: [1, -1], upleft: [-1, -1] };
  // 0..1 for each of n frames of a loop: the first is 0 and the one after the last would be 1 (the first again)
  const loopSteps = (n, kind) => Array.from({ length: n }, (_, k) => ease(kind, k / n));
  // what a wrapped move goes round: the area (the part of it on the canvas), or the whole canvas
  function box(area) {
    if (!area) return { x: 0, y: 0, w: T.W, h: T.H };
    const x = Math.max(0, area.x), y = Math.max(0, area.y);
    return { x, y, w: Math.max(1, Math.min(T.W, area.x + area.w) - x), h: Math.max(1, Math.min(T.H, area.y + area.h) - y) };
  }
  // the layer with what's in box b moved (ox, oy), round its edges: every pixel lands somewhere, none twice
  function wrapped(px, area, b, ox, oy) {
    const out = area ? T.cleared(px, b) : new Array(px.length).fill(null), mod = (v, m) => ((v % m) + m) % m;
    for (let y = 0; y < b.h; y++) for (let x = 0; x < b.w; x++) {
      const c = px[(b.y + y) * T.W + b.x + x];
      if (c) out[(b.y + mod(y + oy, b.h)) * T.W + b.x + mod(x + ox, b.w)] = c;
    }
    return out;
  }
  // how a full trip of `dist` pixels over n frames steps: even (n divides it), or by lo or hi pixels a frame;
  // `even` lists the frame counts (2 to most) that would step evenly
  function pace(dist, n, most = 120) {
    const lo = Math.floor(dist / n), hi = Math.ceil(dist / n);
    const even = []; for (let k = 2; k <= Math.min(most, dist); k++) if (dist % k === 0) even.push(k);
    return { lo, hi, steady: lo === hi, even };
  }

  // brightness: up to 100% dims towards black, above it lightens towards white
  function lit(c, percent) {
    const k = Math.max(0, percent) / 100;
    return k <= 1 ? T.hex(...T.parse(c).map((v) => v * k)) : T.mix(c, '#ffffff', Math.min(1, k - 1));
  }
  // n frames of the layer with its piece going from `from`% to `to`% brightness (a pixel faded to black stays
  // drawn, as black: it still covers what's under it)
  function fade(px, { from = 100, to = 0, n = 8, easing = 'steady', area = null } = {}) {
    const s = split(px, area);
    return steps(n, easing).map((t) => {
      const percent = from + (to - from) * t;
      return T.placed(s.rest, { ...s.piece, px: s.piece.px.map((c) => (c ? lit(c, percent) : null)) }, s.x, s.y);
    });
  }

  const ORDERS = { seen: 'In the order they appear, left to right', light: 'Darkest to lightest', hue: 'Round the colour wheel' };
  const luma = (c) => { const [r, g, b] = T.parse(c); return 0.299 * r + 0.587 * g + 0.114 * b; };
  function hue(c) {
    const [r, g, b] = T.parse(c), max = Math.max(r, g, b), d = max - Math.min(r, g, b);
    if (!d) return -1;   // greys first
    return (max === r ? ((g - b) / d + 6) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4) / 6;
  }
  // the piece's colours in the order they cycle through
  function colours(px, order = 'seen') {
    const seen = [...new Set(px.filter(Boolean))];
    if (order === 'hue') return seen.sort((a, b) => hue(a) - hue(b) || luma(a) - luma(b));
    return order === 'seen' ? seen : seen.sort((a, b) => luma(a) - luma(b) || (a < b ? -1 : 1));
  }
  // n frames with every colour of the piece stepping on to the next one each frame (`step` -1 runs it the
  // other way). With n = how many colours there are, the frame after the last is the first again.
  function cycle(px, { n = 0, order = 'seen', step = 1, area = null } = {}) {
    const s = split(px, area), list = colours(s.piece.px, order), m = list.length, at = new Map(list.map((c, i) => [c, i]));
    return Array.from({ length: n || m || 1 }, (_, k) =>
      T.placed(s.rest, { ...s.piece, px: s.piece.px.map((c) => (c ? list[((at.get(c) + k * step) % m + m) % m] : null)) }, s.x, s.y));
  }

  window.AnimatorMotion = { EASES, ORDERS, TRIPS, ease, move, box, pace, fade, cycle, colours };
})();
