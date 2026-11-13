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

  // n frames of the layer with its piece moved from where it is to (dx, dy) away
  function move(px, { dx = 0, dy = 0, n = 8, easing = 'steady', area = null } = {}) {
    const s = split(px, area);
    return steps(n, easing).map((t) => T.placed(s.rest, s.piece, s.x + Math.round(dx * t), s.y + Math.round(dy * t)));
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

  window.AnimatorMotion = { EASES, ORDERS, ease, move, fade, cycle, colours };
})();
