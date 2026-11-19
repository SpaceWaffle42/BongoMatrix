// Fingers on the Animator: two fingers pinch to zoom and drag to move round a zoomed-in canvas (and draw nothing);
// the "Hold = right-click" key (rub out, or on Pick or a swatch set the second colour); a long press on a swatch for
// the second colour. Loaded before animator.js, which asks Fingers.alt() wherever it asks for the right button and
// tells Fingers.strokeDone() when a stroke ends. The editor's globals (zoom, undo, fitBoard...) are read when a
// finger comes down, never at load.
(() => {
  const $ = (id) => document.getElementById(id);
  const store = { set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* not kept */ } }, get: (k) => { try { return localStorage.getItem(k); } catch { return null; } } };
  const F = { hold: false, latched: false };
  F.alt = () => F.hold || F.latched;

  // -- the key: held down, the other thumb draws as the right button would; a tap is for the next stroke only --
  let downAt = 0, drewMeanwhile = false;
  const paint = () => { const k = $('altkey'); if (!k) return; const on = F.alt(); k.classList.toggle('on', on); k.setAttribute('aria-pressed', String(on)); };
  F.strokeDone = () => {
    if (F.hold) drewMeanwhile = true;
    else if (F.latched) { F.latched = false; paint(); }
  };
  function key() {
    const k = $('altkey');
    k.addEventListener('pointerdown', (e) => { e.preventDefault(); F.hold = true; downAt = performance.now(); drewMeanwhile = false; try { k.setPointerCapture(e.pointerId); } catch { /* gone */ } paint(); });
    k.addEventListener('pointerup', () => { F.hold = false; if (performance.now() - downAt < 250 && !drewMeanwhile) F.latched = !F.latched; paint(); });
    k.addEventListener('pointercancel', () => { F.hold = false; paint(); });
    k.addEventListener('contextmenu', (e) => e.preventDefault());
  }

  // -- two fingers on the canvas ------------------------------------------------------------------------
  // A second finger within 120 ms of the first, or before the first has moved a canvas pixel, takes back the first
  // one's stroke (no pixel is left); otherwise that stroke is kept as it is. Then the pinch zooms (1 to 8 times,
  // about the point between the fingers) and the two together move the canvas. Nothing is drawn until every finger
  // is up; on lifting, the zoom settles on the nearest step of the zoom list
  const fingers = new Map();   // pointerId -> { x, y }
  let first = null, gesture = null;
  const mid = () => { const [a, b] = [...fingers.values()]; return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, d: Math.hypot(a.x - b.x, a.y - b.y) || 1 }; };
  function anchor(fx, fy, at) {   // keep the canvas point (fx, fy as fractions of the board) under the screen point `at`
    const w = $('board-wrap'), r = board.getBoundingClientRect();
    w.scrollLeft += r.left + fx * r.width - at.x; w.scrollTop += r.top + fy * r.height - at.y;
  }
  function begin(e) {
    const young = performance.now() - first.t < 120 || !first.moved;
    if (young) cancelStroke(first.mark); else if (busy()) strokeEnd();
    const m = mid(), r = board.getBoundingClientRect();
    gesture = { z0: zoom, d0: m.d, fx: (m.x - r.left) / r.width, fy: (m.y - r.top) / r.height, last: m };
    e.stopPropagation(); e.preventDefault();
  }
  function move() {
    const m = mid(); gesture.last = m;
    const z = Math.max(1, Math.min(8, gesture.z0 * m.d / gesture.d0));
    if (Math.abs(z - zoom) > 0.001) { zoom = z; fitBoard(); }
    anchor(gesture.fx, gesture.fy, m);   // at Fit nothing scrolls, so moving does nothing
  }
  function end() {
    const opts = [...$('zoom').options].map((o) => Number(o.value)), near = opts.reduce((a, b) => (Math.abs(b - zoom) < Math.abs(a - zoom) ? b : a));
    $('zoom').value = String(near); zoom = near; fitBoard(); anchor(gesture.fx, gesture.fy, gesture.last); renderBoard();
    gesture = null;
    if (!document.body.classList.contains('pinched')) { document.body.classList.add('pinched'); store.set('animator.pinched', '1'); }   // the "Two fingers" card has done its job
  }
  function twoFingers() {
    const well = $('board-wrap');
    well.addEventListener('pointerdown', (e) => {
      if (e.pointerType !== 'touch') return;
      fingers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (gesture) { e.stopPropagation(); e.preventDefault(); return; }   // a third finger, or one back down mid-gesture: still no drawing
      if (fingers.size === 1) first = { id: e.pointerId, t: performance.now(), x: e.clientX, y: e.clientY, moved: false, mark: undo[undo.length - 1] || null };
      else if (fingers.size === 2) begin(e);
    }, true);
    well.addEventListener('pointermove', (e) => {
      if (e.pointerType !== 'touch' || !fingers.has(e.pointerId)) return;
      fingers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (first && e.pointerId === first.id && !first.moved) { const r = board.getBoundingClientRect(); first.moved = Math.abs(e.clientX - first.x) >= r.width / W || Math.abs(e.clientY - first.y) >= r.height / H; }
      if (gesture) { if (fingers.size >= 2) move(); e.stopPropagation(); e.preventDefault(); }
    }, true);
    const lift = (e) => {
      if (e.pointerType !== 'touch') return;
      fingers.delete(e.pointerId);
      if (!gesture) { if (!fingers.size) first = null; return; }
      e.stopPropagation(); e.preventDefault();
      if (!fingers.size) { end(); first = null; }
    };
    well.addEventListener('pointerup', lift, true);
    well.addEventListener('pointercancel', lift, true);
  }

  // -- a long press on a swatch: the second colour (a phone has no right button) -------------------------
  function swatches() {
    let press = null, suppress = null, touchAt = 0;
    const isSwatch = (t) => t.closest?.('.swatches button, .qcol button:not(.main):not(.second):not(.all)');
    document.addEventListener('pointerdown', (e) => {
      if (e.pointerType !== 'touch') return;
      touchAt = performance.now();
      const b = isSwatch(e.target); if (!b) return;
      press = { b, x: e.clientX, y: e.clientY, timer: setTimeout(() => { setColour2(b.title); toast('Second colour'); suppress = b; press = null; }, 450) };
    }, true);
    document.addEventListener('pointermove', (e) => { if (press && Math.hypot(e.clientX - press.x, e.clientY - press.y) >= 8) { clearTimeout(press.timer); press = null; } }, true);
    const done = () => { if (press) { clearTimeout(press.timer); press = null; } };
    document.addEventListener('pointerup', done, true); document.addEventListener('pointercancel', done, true);
    document.addEventListener('click', (e) => { if (suppress && suppress.contains(e.target)) { e.stopPropagation(); e.preventDefault(); suppress = null; } }, true);   // the press that set it isn't also a tap
    document.addEventListener('contextmenu', (e) => { if (performance.now() - touchAt < 1500 && isSwatch(e.target)) { e.preventDefault(); e.stopPropagation(); } }, true);   // on touch the long press does it
  }

  F.init = () => {
    key(); twoFingers(); swatches();
    // a tap near the canvas's edge must land on the canvas: Chrome moves a tap to a nearby button (the header's ≡) unless
    // what's under the finger answers taps itself, and a canvas with pointer listeners only doesn't count
    for (const el of [board, $('board-wrap')]) el.addEventListener('click', () => {});
    if (store.get('animator.pinched') === '1') document.body.classList.add('pinched');
  };
  window.AnimatorTouch = F;
})();
