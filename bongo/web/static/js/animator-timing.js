// What the Matrix does with an animation's times, so the Animator's preview can show the same (window.PanelTiming).
// Each rule mirrors the Python that does it; tests/test_animator_timing.py runs both against each other:
//   save: bongo/animator.py gif() and encode_gif() (frame times 20 to 10000 ms, a repeated frame merged into the one
//         before it, "there and back" written out, then each time rounded to a hundredth of a second: GIF can't hold more)
//   play: bongo/modules/media/__init__.py Fit.render (draws 20 times a second, at moments 50 ms apart, and shows the
//         frame whose time covers that moment: so a frame is shown for a whole number of 50 ms, and a time that isn't one
//         comes out sometimes shorter, sometimes longer, right on average) and bongo/media.py frames_played (the first 300 frames)
(function () {
  const TICK = 50;               // ms between the Matrix's drawings (Media's fps = 20)
  const MOST_FRAMES = 300;       // frames of a file the Media module plays (bongo/media.py MAX_FRAMES)
  const MAX_DECODE = 50e6;       // width x height x frames it decodes (bongo/media.py MAX_DECODE)
  const SHOW_SECONDS = 15;       // "Show on Matrix" plays this long (bongo/web/__init__.py)
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  // one frame's time as it is saved: whole ms, 20 to 10000 (gif() in bongo/animator.py)...
  const saved = (ms) => clamp(Math.floor((Number(ms) || 0) + 0.5), 20, 10000);
  // ...and as a GIF holds it: hundredths of a second, the nearest (encode_gif)
  const gifMs = (ms) => Math.max(10, 10 * Math.floor(ms / 10 + 0.5));
  const same = (a, b) => { if (a === b) return true; for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false; return true; };

  // The frames as the Matrix plays them. frames: [{ px, ms }] (px: each frame's flattened pixels, to find repeats).
  // Returns { seq: the Animator frame each played frame starts with, ends: when each played frame ends (ms),
  // total (one pass, ms), count (frames in the GIF), played (of those, the ones the Matrix plays) }.
  function timeline(frames, mode, w, h) {
    let order = frames.map((_, i) => i);
    if (mode === 'pingpong' && order.length > 2) order = order.concat(order.slice(1, -1).reverse());   // pictures[-2:0:-1]
    const merged = [];
    for (const i of order) {
      const last = merged[merged.length - 1];
      if (last && frames[last.i].px && frames[i].px && same(frames[last.i].px, frames[i].px)) last.ms = Math.min(655350, last.ms + saved(frames[i].ms));
      else merged.push({ i, ms: saved(frames[i].ms) });
    }
    const played = Math.max(1, Math.min(merged.length, MOST_FRAMES, Math.floor(MAX_DECODE / Math.max(1, w * h))));
    let total = 0;
    const kept = merged.slice(0, played), ends = kept.map((m) => (total += gifMs(m.ms)));
    return { seq: kept.map((m) => m.i), ends, total, count: merged.length, played };
  }

  // Which frame the Matrix is showing at its n-th drawing (elapsed = n * 50 ms, a hair late as a real clock is), at
  // a speed of 1 for 100%. once: a GIF that doesn't loop stays on its last frame when it has played through.
  function frameAt(tl, elapsed, speed = 1, once = false) {
    let t = Math.floor(elapsed * speed);
    const done = once && t >= tl.total;
    if (done) return { frame: tl.seq[tl.seq.length - 1], done };
    t %= tl.total;
    return { frame: tl.seq[tl.ends.findIndex((e) => t < e)], done };
  }

  // the line beside the frames: "22 frames · 1.5 s" and, only when it matters, what the Matrix will do differently
  function summary(frames, mode, w, h, tl = timeline(frames, mode, w, h)) {
    const n = frames.length, secs = (ms) => `${(ms / 1000).toFixed(1)} s`;
    const parts = [`${n} frame${n === 1 ? '' : 's'}`, mode === 'pingpong' && n > 2 ? `${secs(tl.total)} there and back` : secs(tl.total)];
    if (tl.played < tl.count) parts.push(`The Matrix plays only the first ${tl.played} of ${tl.count} frames.`);
    if (tl.total > SHOW_SECONDS * 1000) parts.push(`Show on Matrix plays the first ${SHOW_SECONDS} s.`);
    if (frames.some((f) => saved(f.ms) % TICK)) parts.push('The Matrix draws every 50 ms, so times that aren’t a multiple of 50 look slightly uneven.');
    return parts;
  }

  window.PanelTiming = { TICK, MOST_FRAMES, SHOW_SECONDS, saved, gifMs, timeline, frameAt, summary };
})();
