// The Animator's editor (animator.html). The drawing is a canvas of W x H pixels (the panel, or a small one
// for a sprite), a list of layers that every frame has, and frames that keep their own pixels per layer.
// The pixel work is in animator-tools.js, Spread across frames in animator-motion.js, and the project file
// (what Save keeps and Open reads) in animator-project.js.
const T = window.PixelTools, Motion = window.AnimatorMotion, Project = window.AnimatorProject, PT = window.PanelTiming;
const E = window.AnimatorEngine;   // undo, the kept pictures, colour counts and the draft: js/animator-engine.js
const Aids = window.AnimatorAids;   // onion skin, centre lines, lined-up text, a picture to trace over (never part of the drawing)
const Layout = window.AnimatorLayout;   // the page's layout: icons, popovers, the Preview window, the phone's tabs (js/animator-layout.js)
const Fingers = window.AnimatorTouch;   // two fingers to zoom and move, the Hold = right-click key (js/animator-touch.js)
const alt = (e) => e.button === 2 || (e.buttons & 2) === 2 || Fingers.alt();   // the right button: rubs out, or on Pick sets the second colour
const PRESET_W = 64, PRESET_H = 32;   // the panel the presets were drawn for; js/animator-sizes.js says which suit other canvases
let W = 64, H = 32, S = 20;           // the canvas, and how many screen pixels a cell gets on the board
const PANEL = { w: 64, h: 32 };       // this Matrix's panel and safe area (asked for below)
let SAFE = { top: 1, right: 1, bottom: 1, left: 1 };
const PALETTE = ['#000000','#ffffff','#9aa1ac','#3b4048','#ff3b30','#ff8a3d','#ffb547','#ffe066',
                 '#4ade80','#1fbf8f','#2ec4ff','#3b6bff','#8a5cff','#ff5ccf','#8b5a2b','#f2c9a0'];
const $ = (id) => document.getElementById(id);
const board = $('board'), bx = board.getContext('2d');
const preview = $('preview'), px = preview.getContext('2d');
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
let frames = [], cur = 0, tool = 'pen', colour = '#ffb547', mirror = false, onion = true, grid = true, showSafe = true;
let undo = [], redo = [], brush = 1, clip = null, dirty = false, bg = '#000000', zoom = 1, dim = 100;
// how many frames fit: 300 of the panel, fewer of a bigger canvas (the server's limit too: bongo/animator.py)
function roomFor(n, say = toast) { const most = Project.maxFrames(W, H); if (frames.length + n <= most) return true; say(`An animation this size can have up to ${most} frames`); return false; }

// -- the canvas size ------------------------------------------------------------------------
function setSize(w, h) {
  W = w; H = h; T.size(w, h);
  S = Math.max(4, Math.floor(1280 / Math.max(W, H)));
  board.width = W * S; board.height = H * S; preview.width = W; preview.height = H;
  const led = preview.parentElement;
  led.style.aspectRatio = `${W} / ${H}`; led.style.width = `min(100cqw, 100cqh * ${W / H})`;   // as big as the Preview's box allows
  led.style.setProperty('--w', W); led.style.setProperty('--h', H);
  board.setAttribute('aria-label', `Drawing canvas, ${W} by ${H} pixels`);
  fitBoard(); sizePicker();
}
// the board fills its well (less a margin) at the canvas's own shape, times the zoom; zoomed in, the well scrolls
function fitBoard() { board.style.width = `calc(min(100cqw - var(--pad), (100cqh - var(--pad)) * ${W / H}) * ${zoom})`; board.style.height = 'auto'; $('board-wrap').classList.toggle('zoomed', zoom > 1); }
const SIZES = [[8, 8], [16, 16], [32, 32], [64, 32], [64, 64]];
function sizePicker() {
  const list = [[PANEL.w, PANEL.h, `Panel, ${PANEL.w} × ${PANEL.h}`], ...SIZES.filter(([w, h]) => w !== PANEL.w || h !== PANEL.h).map(([w, h]) => [w, h, `${w} × ${h}`])];
  if (!list.some(([w, h]) => w === W && h === H)) list.push([W, H, `${W} × ${H}`]);
  $('csize').replaceChildren(...list.map(([w, h, name]) => new Option(name, `${w}x${h}`)), new Option('Another size…', 'custom'));
  $('csize').value = `${W}x${H}`;
}
function resize(w, h) {   // the top-left corner stays put; Undo brings back what a smaller canvas cut off
  if (w === W && h === H) return;
  if (frames.length > Project.maxFrames(w, h)) { toast(`At ${w} × ${h} an animation can have up to ${Project.maxFrames(w, h)} frames: this one has ${frames.length}`); sizePicker(); return; }
  snapshot(); sel = null;
  const done = new Map();   // a cel shared by every frame is resized once, and stays shared
  frames.forEach((f) => { f.cels = f.cels.map((c) => { if (!done.has(c)) done.set(c, T.resized(c, W, H, w, h)); return done.get(c); }); });
  const cut = w < W || h < H;
  setSize(w, h); renderAll();
  toast(cut ? `The canvas is now ${w} × ${h}. Anything outside it was cut off: Undo brings it back` : `The canvas is now ${w} × ${h}`);
}
$('csize').onchange = async () => {
  let v = $('csize').value;
  if (v === 'custom') {
    const typed = await ask.input({ title: 'Canvas size', text: `Width and height in pixels, each from 1 to ${Project.MAX_SIDE}. For example: 28x28`, label: 'Width x height', value: `${W}x${H}`, ok: 'Resize',
      validate: (t) => (t.trim().split(/[^0-9]+/).filter(Boolean).length === 2 ? '' : 'Type two numbers, like 28x28') });
    if (typed === null) { sizePicker(); return; }
    v = typed.trim().split(/[^0-9]+/).filter(Boolean).join('x');
  }
  const [w, h] = v.split('x').map(Number);
  if (!(w >= 1 && w <= Project.MAX_SIDE && h >= 1 && h <= Project.MAX_SIDE)) { toast(`A canvas is 1 to ${Project.MAX_SIDE} pixels each way`); sizePicker(); return; }
  resize(w, h);
};
$('zoom').onchange = () => { zoom = Number($('zoom').value) || 1; fitBoard(); };
function zoomBy(d) { const o = [...$('zoom').options].map((x) => x.value), at = clamp(o.indexOf($('zoom').value) + d, 0, o.length - 1); $('zoom').value = o[at]; $('zoom').onchange(); }

// -- frames and layers ------------------------------------------------------------------------
// layers: the same list for every frame ({ id, name, on, opacity, mode, shared }); each frame keeps its own
// pixels per layer in `cels`, bottom first, and `px` is its shown layers flattened (what's previewed and
// saved). A shared layer has one cel that every frame points at: draw it once, it's on them all.
const newLayer = (id, name) => ({ id, name, on: true, opacity: 1, mode: 'normal', shared: false });
let layers = [newLayer(1, 'Drawing')], al = 0;
let layerSeq = 1;   // only ever goes up: an id still in the undo history or a copied frame is never reused
const nextId = () => (layerSeq = Math.max(layerSeq, ...layers.map((l) => l.id || 0)) + 1);
const empty = () => new Array(W * H).fill(null);
function relink() { layers.forEach((l, k) => { if (l.shared) frames.forEach((f) => { f.cels[k] = frames[0].cels[k]; }); }); }
function belowLayer() { return T.compose(frames[cur].cels, layers, bg, al); }   // what a brush on the selected layer sits over
function blank() { return { px: empty(), ms: 90, cels: layers.map((l, k) => (l.shared && frames.length ? frames[0].cels[k] : empty())) }; }
function flat(f) { f.px = E.flattened(f.cels, layers, bg, T.compose); }   // the same picture as before if its cels and layers haven't changed
function flatAll() { frames.forEach(flat); }
function oneLayer(list) {   // frames from a preset or an imported GIF: one layer each
  layers = [newLayer(nextId(), 'Drawing')]; al = 0;   // a new id: a frame copied before must not take it for its own layer
  list.forEach((f) => { f.cels = [f.px.slice()]; });
  return list;
}
// the selected layer of the current frame, shaped like a frame so every tool can draw on it
// (a cel kept by Undo is read-only: drawing on it copies it first, onto every frame for a shared layer)
function writable(k, f = frames[cur]) { const c = f.cels[k]; if (!Object.isFrozen(c)) return c; const v = c.slice(); if (layers[k].shared) frames.forEach((g) => { g.cels[k] = v; }); else f.cels[k] = v; return v; }
const target = { get px() { return writable(al); }, set px(v) { if (layers[al].shared) frames.forEach((f) => { f.cels[al] = v; }); else frames[cur].cels[al] = v; } };
const PRESETS = window.ANIMATOR_PRESETS || [];
const preset = (id) => PRESETS.find((p) => p.id === id);
// the presets draw with the tools at the panel's 64 x 32, whatever size the canvas is just now
function atPanel(draw) { const w = T.W, h = T.H; T.size(PRESET_W, PRESET_H); try { return draw(); } finally { T.size(w, h); } }
function copyFrames(list) { return list.map((f) => ({ px: f.px.slice(), ms: f.ms, ...(f.cels ? { cels: f.cels.map((c) => c.slice()) } : {}) })); }
function starter() { return oneLayer(preset('ball') ? copyFrames(atPanel(() => preset('ball').make())) : [{ px: empty(), ms: 90 }]); }

// -- undo ---------------------------------------------------------------------------------
// each step keeps the size, the layers' settings and every frame's time and cels. The cels are kept by reference
// (they're made read-only, see writable()), so a step costs about what changed: E.capture in animator-engine.js
const state = (whole = false) => E.capture({ ...(whole ? { whole: true, name: $('name').value, loop: $('loop').value, savedAs } : {}), w: W, h: H, bg, cur, layers, al, frames, fsel: FS.indices() });   // fsel: the selected frames (js/animator-frames.js), brought back with the step, never a step of its own
const UNDO_BYTES = 200e6;   // what the steps add up to at most (or 80 steps)
let savedAs = null;   // the drawing this one was opened from or last saved as: saving over any other asks first
let redoWas = [];
function unsnapshot() { undo.pop(); redo = redoWas; }   // the step just taken turned out to change nothing
function snapshot(whole = false) {   // whole: this step replaces the drawing (a preset, Open, Import), so its name goes with it
  if (sel) sel.piece = sel.base = null;   // a piece being moved is put down where it is before anything else changes
  undo.push(state(whole)); redoWas = redo; redo = []; dirty = true; draftSoon();
  let total = undo.reduce((n, u) => n + u.bytes, 0);
  while (undo.length > 1 && (undo.length > 80 || total > UNDO_BYTES)) total -= undo.shift().bytes;
}
function restore(s) {
  const o = s;
  if (o.w !== W || o.h !== H) setSize(o.w, o.h);
  ({ frames, layers } = E.revive(o)); frames.forEach((f) => { f.px = E.emptyCel(W * H); });   // flattened by renderAll below
  al = Math.min(o.al, layers.length - 1); cur = Math.min(o.cur, frames.length - 1); FS.restore(o.fsel);
  if (o.bg && o.bg !== bg) { bg = o.bg; $('bg').value = bg; }
  if (o.whole) { $('name').value = o.name; $('loop').value = o.loop; savedAs = o.savedAs; }
  relink(); sel = null; dirty = true; renderAll();
}

// -- drawing ----------------------------------------------------------------------------
let seed = 1;
// paint with the brush at (x, y); the mirror paints the same cells reflected
function setPx(f, x, y, c, size = brush) {
  const p = f.px;
  T.stamp(x, y, size, shape, (i, a, b) => {
    p[i] = c;
    const m = shape === 'dither' && W % 2 === 0 ? W - a : W - 1 - a;   // dither mirrors a column over (on an even width), so it keeps its checkerboard
    if (mirror && m < W) p[b * W + m] = c;
  }, seed++);
}
const lineCells = T.lineCells;
function fill(f, x, y, c) { T.fill(f.px, x, y, c); }
// a line, rectangle or oval from a to b: filled shapes are filled flat, outlines drawn with the brush
function drawShape(f, a, b, c) {
  if (tool === 'line') return T.lineCells(a[0], a[1], b[0], b[1]).forEach(([x, y]) => setPx(f, x, y, c));
  const cells = (tool === 'rect' ? T.rectCells : T.ellipseCells)(a, b, filled);
  if (filled) cells.forEach(([x, y]) => setPx(f, x, y, c, 1));
  (filled ? (tool === 'rect' ? T.rectCells : T.ellipseCells)(a, b, false) : cells).forEach(([x, y]) => setPx(f, x, y, c));
}
function drawGradient(f, a, b) {
  const stops = $('grainbow').checked ? ['#ff3b30', '#ff8a3d', '#ffe066', '#4ade80', '#2ec4ff', '#3b6bff', '#8a5cff'] : [colour, colour2];
  const opts = { type: $('gtype').value, style: $('gstyle').value, steps: Number($('gsteps').value), area: $('garea').value, from: a };
  T.gradient(f.px, a, b, stops, { ...opts, mirror });
}
function brushAt(f, x, y) {   // blur, shade and glow, at the pixel and (with the mirror on) its reflection
  if (tool === 'glow') {   // the halo is round and soft whatever brush shape is picked
    const at = (u) => T.glowAt(f.px, u, y, brush, colour, Number($('glowk').value), bg, stroke, under);
    at(x); if (mirror && W - 1 - x !== x) at(W - 1 - x);
    return;
  }
  const at = (u) => tool === 'blur' ? T.blurAt(f.px, u, y, brush, shape, Number($('blurk').value), bg, stroke, seed++, under)
                                    : T.shadeAt(f.px, u, y, brush, shape, Number($('shadek').value), seed++, stroke);
  at(x); if (mirror && W - 1 - x !== x) at(W - 1 - x);
}

function drawFrame(ctx, f, scale, ghost) {
  ctx.fillStyle = bg; ctx.fillRect(0, 0, W * scale, H * scale);
  if (ghost) ghost(ctx);   // what shows faintly under the drawing, on the board only: the onion skin, a picture to trace
  f.px.forEach((c, i) => { if (c) { ctx.fillStyle = c; ctx.fillRect((i % W) * scale, Math.floor(i / W) * scale, scale, scale); } });
}
// pixels for putImageData: much quicker than drawing a square each. `alpha`: empty pixels stay see-through
const rgbOf = new Map();   // colour (and dimming) -> [r, g, b], worked out once
function toImage(pixels, w, h, { percent = 100, alpha = false } = {}) {
  const img = new ImageData(w, h), d = img.data, back = T.parse(T.dimmed(bg, percent));
  const look = (c) => { const key = percent === 100 ? c : `${c}/${percent}`; let v = rgbOf.get(key);
    if (!v) { if (rgbOf.size > 65536) rgbOf.clear(); rgbOf.set(key, (v = T.parse(T.dimmed(c, percent)))); } return v; };
  for (let i = 0; i < pixels.length; i++) { const c = pixels[i], o = i * 4, v = c ? look(c) : back;
    d[o] = v[0]; d[o + 1] = v[1]; d[o + 2] = v[2]; d[o + 3] = c || !alpha ? 255 : 0; }
  return img;
}

let hover = null, dragStart = null, dragEnd = null;
function renderBoard() {
  flat(frames[cur]);   // the selected layer may just have changed
  const f = frames[cur];
  drawFrame(bx, f, S, boardUnder);
  if (onion) Aids.ghosts(bx, Aids.onionFrames(cur, frames.length, onionShow, $('loop').value).map(({ i, tint }) => ({ px: frames[i].px, tint })), f.px, W, S, onionK);
  if (tracing && tracing.on && tracing.over) Aids.picture(bx, tracing.img, W * S, H * S, tracing.k);
  if (grid) {
    bx.strokeStyle = 'rgba(255,255,255,0.07)'; bx.lineWidth = 1; bx.beginPath();
    for (let x = 1; x < W; x++) { bx.moveTo(x * S + .5, 0); bx.lineTo(x * S + .5, H * S); }
    for (let y = 1; y < H; y++) { bx.moveTo(0, y * S + .5); bx.lineTo(W * S, y * S + .5); }
    bx.stroke();
    bx.strokeStyle = 'rgba(255,255,255,0.16)'; bx.beginPath();
    for (let x = 8; x < W; x += 8) { bx.moveTo(x * S + .5, 0); bx.lineTo(x * S + .5, H * S); }
    for (let y = 8; y < H; y += 8) { bx.moveTo(0, y * S + .5); bx.lineTo(W * S, y * S + .5); }
    bx.stroke();
  }
  const line = Math.max(1, Math.round(2 * board.width / (board.clientWidth || board.width)));   // two screen pixels, whatever the canvas size and zoom
  if (mirror) { bx.strokeStyle = 'rgba(159,194,255,0.6)'; bx.setLineDash([8, 6]); bx.beginPath(); bx.moveTo(W * S / 2, 0); bx.lineTo(W * S / 2, H * S); bx.stroke(); bx.setLineDash([]); }
  if (showSafe && W === PANEL.w && H === PANEL.h) {   // the frame round the panel: only means something at the panel's size
    bx.strokeStyle = 'rgba(255,181,71,0.75)'; bx.setLineDash([10, 6]); bx.lineWidth = 2;
    bx.strokeRect(SAFE.left * S + 1, SAFE.top * S + 1, (W - SAFE.left - SAFE.right) * S - 2, (H - SAFE.top - SAFE.bottom) * S - 2);
    bx.setLineDash([]); bx.lineWidth = 1;
  }
  if (centre) Aids.centreLines(bx, guideBox(), S, line);
  if (tool === 'text' && hover && (placing || !coarse)) {   // where the text would land
    bx.globalAlpha = 0.6; bx.fillStyle = colour;
    Aids.text($('ttext').value, hover[0], hover[1], font, textAlign, guideBox(), (i) => bx.fillRect((i % W) * S, Math.floor(i / W) * S, S, S));
    bx.globalAlpha = 1;
  }
  const box = marquee ? rectOf(marquee.from, marquee.to) : sel;
  if (box) outlineSel(box, line);
  if (hover && tool !== 'text') { bx.strokeStyle = '#fff'; bx.strokeRect(hover[0] * S + .5, hover[1] * S + .5, S - 1, S - 1); }
}
// under the drawing on the board: a picture to trace, when it goes under (the onion skin goes over: see renderBoard)
function boardUnder(ctx) { if (tracing && tracing.on && !tracing.over) Aids.picture(ctx, tracing.img, W * S, H * S, tracing.k); }
// the box the centre lines and lined-up text go by: the safe area when its guide shows on a panel-sized canvas
const guideBox = () => Aids.guideBox(W, H, showSafe && W === PANEL.w && H === PANEL.h ? SAFE : null);
function cell(e) {
  const r = board.getBoundingClientRect();
  return [Math.floor((e.clientX - r.left) / r.width * W), Math.floor((e.clientY - r.top) / r.height * H)];
}
let drawing = false, last = null, moving = null;
const DRAG = new Set(['line', 'rect', 'ellipse', 'gradient']);
const BRUSHED = new Set(['blur', 'shade', 'glow']);
let dragBase = null, dragErase = false, stroke = null, under = null;   // under: what the selected layer sits over; stroke: pixels blurred or shaded so far, once each per stroke
// while dragging a shape or gradient, the frame is redrawn from how it was each move, so you see the real result
function dragTo(x, y) {
  const f = target; dragEnd = [x, y]; f.px = dragBase.slice();
  if (tool === 'gradient' && x === dragStart[0] && y === dragStart[1]) return;   // a gradient needs a drag: a click alone does nothing
  if (tool === 'gradient') drawGradient(f, dragStart, dragEnd); else drawShape(f, dragStart, dragEnd, dragErase ? null : colour);
}
const busy = () => !!(dragStart || drawing || moving || marquee || selDrag || sizing || placing || panning || FS.dragging);   // FS.dragging: frames being dragged in the strip
const coarse = window.matchMedia('(pointer: coarse)').matches;   // a finger: no hover, so text is placed where you let go
let placing = null, panning = null;
board.addEventListener('pointerdown', (e) => {
  if (tool === 'pan') { const w = $('board-wrap'); panning = { x: e.clientX, y: e.clientY, left: w.scrollLeft, top: w.scrollTop }; try { board.setPointerCapture(e.pointerId); } catch { /* the pointer's already gone */ } return; }   // a mouse or a finger
  try { board.setPointerCapture(e.pointerId); } catch { /* the pointer's already gone */ }
  const [x, y] = cell(e), f = target;
  if (tool === 'pick' && recolourPick) { const c = frames[cur].px[y * W + x], was = recolourPick.sel; setTool(was ? 'select' : 'pen'); if (was && was.cur === cur && was.al === al) sel = was; openRecolour(c); return; }   // the colour to change, for Change a colour… (and the selection back)
  if (tool === 'pick') { const c = frames[cur].px[y * W + x]; if (c) (alt(e) ? setColour2 : setColour)(c); setTool('pen'); return; }
  if (tool === 'select' && handleAt(e)) { startSizing(handleAt(e)); return; }   // a corner or edge of the selection: resize it
  if (tool === 'select' && !(sel && inSel(x, y)) && $('selby').value !== 'area') { sel = Sel.wand(target.px, clamp(x, 0, W - 1), clamp(y, 0, H - 1), $('selby').value === 'all'); if (sel) Object.assign(sel, { piece: null, base: null, cur, al }); renderBoard(); return; }   // by colour
  if (tool === 'select' && !(sel && inSel(x, y))) { sel = null; marquee = { from: [x, y], to: [x, y], moved: false }; renderBoard(); return; }   // a new area: nothing drawn yet
  if (!layers[al].on) { toast(`"${layers[al].name}" is hidden: show it to draw on it`); return; }
  if (refuseLocked()) return;
  if (tool === 'select') { selDrag = { from: [x, y], at: [sel.x, sel.y] }; return; }
  if (tool === 'text') { if ($('ttext').value) { placing = { erase: alt(e) }; hover = [x, y]; renderBoard(); } else toast('Type the text first, in the box above the canvas'); return; }
  snapshot();
  if (tool === 'fill') { fill(f, x, y, alt(e) ? null : colour); renderAll(); return; }
  if (DRAG.has(tool)) { dragStart = [x, y]; dragBase = f.px.slice(); dragErase = alt(e); dragTo(x, y); renderBoard(); return; }
  if (tool === 'shift') { moving = { from: [x, y], px: f.px.slice() }; dragBase = moving.px; return; }
  drawing = true; last = [x, y]; stroke = tool === 'glow' ? { base: new Map(), k: new Map() } : new Set(); under = belowLayer();
  if (BRUSHED.has(tool)) brushAt(f, x, y); else setPx(f, x, y, tool === 'erase' || alt(e) ? null : colour);
  renderBoard();
});
board.addEventListener('pointermove', (e) => {
  if (panning) { const w = $('board-wrap'); w.scrollLeft = panning.left - (e.clientX - panning.x); w.scrollTop = panning.top - (e.clientY - panning.y); return; }
  const [x, y] = cell(e); hover = [x, y]; $('pos').textContent = `x ${x}, y ${y}`;
  if (marquee) { marquee.to = [clamp(x, 0, W - 1), clamp(y, 0, H - 1)]; marquee.moved ||= x !== marquee.from[0] || y !== marquee.from[1]; }
  else if (sizing) sizeTo(e);
  else if (selDrag) { if (x !== selDrag.from[0] || y !== selDrag.from[1] || sel.piece) { lift(); moveSel(selDrag.at[0] + x - selDrag.from[0], selDrag.at[1] + y - selDrag.from[1]); } }
  else if (dragStart) dragTo(x, y);
  else if (moving) { target.px = T.shifted(moving.px, x - moving.from[0], y - moving.from[1]); }
  else if (drawing) {
    const cells = lineCells(last[0], last[1], x, y).slice(1);
    if (BRUSHED.has(tool)) cells.forEach(([a, b]) => brushAt(target, a, b));
    else cells.forEach(([a, b]) => setPx(target, a, b, tool === 'erase' || alt(e) ? null : colour));
    last = [x, y];
  }
  renderBoard();
});
function strokeEnd(e) {   // a finished stroke, or one the browser cancelled (an edge swipe, an alert)
  panning = null;
  if (sizing) { if (e && e.type === 'pointercancel') cancelSizing(); else endSizing(); }
  if (placing) {   // the text goes where the pointer was let go
    if (e && e.type === 'pointerup' && hover) { const was = target.px, p = was.slice(), erase = placing.erase; Aids.text($('ttext').value, hover[0], hover[1], font, textAlign, guideBox(), (i) => { p[i] = erase ? null : colour; }); if (p.some((c, i) => c !== was[i])) { snapshot(); target.px = p; } }
    placing = null; if (coarse) hover = null;
  }
  if (marquee) { sel = marquee.moved ? { ...rectOf(marquee.from, marquee.to), piece: null, base: null, cur, al } : null; marquee = null; }
  if (dragBase && target.px.every((c, i) => c === dragBase[i])) unsnapshot();   // nothing changed: no undo step
  dragStart = dragEnd = dragBase = stroke = under = selDrag = null;
  drawing = false; moving = null; renderAll(); Fingers.strokeDone();   // a Hold = right-click tap was for this stroke only
}
// a second finger came down at once (js/animator-touch.js): the first one's stroke is taken back, as if never drawn.
// mark: the undo step on top before it began
function cancelStroke(mark) {
  if (sizing) cancelSizing();
  placing = marquee = panning = null; dragStart = dragEnd = dragBase = stroke = under = selDrag = null; drawing = false; moving = null;
  const i = mark ? undo.indexOf(mark) + 1 : 0;
  if (undo.length > i) { const back = undo[i]; undo.length = i; redo = redoWas; restore(back); } else renderAll();
}
board.addEventListener('pointerup', strokeEnd);
board.addEventListener('pointercancel', strokeEnd);
board.addEventListener('pointerleave', () => { hover = null; $('pos').textContent = 'x –, y –'; renderBoard(); });
board.addEventListener('contextmenu', (e) => e.preventDefault());

// -- the selection --------------------------------------------------------------------------
// sel: the area ({ x, y, w, h }) on one layer of one frame. Until it's moved it is only an outline. Moving,
// flipping or turning it lifts its pixels off the layer: `piece` is what was lifted and `base` the layer
// without it, so the layer is always base + piece wherever the piece is now. Anything else that changes the
// drawing puts the piece down first (snapshot does that). An area picked by colour has a `mask` (animator-
// select.js) and every step keeps it with the piece. `orig` is the piece as lifted: resizing always scales that,
// so going bigger and smaller again never wears it down.
const Sel = window.AnimatorSelect;
let sel = null, marquee = null, selDrag = null, selClip = null, lastCopied = null;   // lastCopied: 'area' or 'frame', whichever Ctrl+V should paste
const rectOf = (a, b) => ({ x: Math.min(a[0], b[0]), y: Math.min(a[1], b[1]), w: Math.abs(a[0] - b[0]) + 1, h: Math.abs(a[1] - b[1]) + 1 });
const inSel = (x, y) => x >= sel.x && y >= sel.y && x < sel.x + sel.w && y < sel.y + sel.h && (!sel.mask || !!sel.mask[(y - sel.y) * sel.w + x - sel.x]);
function lift() {
  if (sel.piece) return;
  snapshot();
  sel.piece = sel.orig = T.lifted(target.px, sel); sel.base = T.cleared(target.px, sel);
}
function moveSel(x, y) { sel.x = x; sel.y = y; delete sel.mid; target.px = T.placed(sel.base, sel.piece, x, y); }
function needSel() { if (sel && layers[al].on) return true; toast(sel ? `"${layers[al].name}" is hidden: show it first` : 'Select an area first: drag across it with the select tool'); return false; }
function changeSel(change) {   // flip or turn the piece about its middle
  if (!needSel() || refuseLocked()) return;
  lift();
  const [cx, cy] = sel.mid || [sel.x + sel.w / 2, sel.y + sel.h / 2];   // its true middle, kept between turns so four of them come home
  sel.piece = change(sel.piece); sel.w = sel.piece.w; sel.h = sel.piece.h; sel.mask = sel.piece.mask; sel.orig = change(sel.orig);
  moveSel(Math.round(cx - sel.w / 2), Math.round(cy - sel.h / 2)); sel.mid = [cx, cy]; renderAll();
}
function copySel() { if (!needSel()) return false; selClip = { ...(sel.piece ? { ...sel.piece, px: sel.piece.px.slice(), ...(sel.piece.mask ? { mask: sel.piece.mask.slice() } : {}) } : T.lifted(target.px, sel)), x: sel.x, y: sel.y }; lastCopied = 'area'; toast('Area copied'); return true; }
function deleteSel() {
  if (!needSel() || refuseLocked()) return;
  if (sel.piece) target.px = sel.base; else { snapshot(); target.px = T.cleared(target.px, sel); }
  sel = null; renderAll();
}
function pasteSel() {
  if (!selClip) return toast('Copy or cut an area first');
  if (!layers[al].on) return toast(`"${layers[al].name}" is hidden: show it to paste onto it`);
  if (refuseLocked()) return;
  const at = sel ? [sel.x, sel.y] : [clamp(selClip.x, 0, W - 1), clamp(selClip.y, 0, H - 1)];   // where it was copied from
  setTool('select'); snapshot();
  const piece = { px: selClip.px.slice(), w: selClip.w, h: selClip.h, ...(selClip.mask ? { mask: selClip.mask.slice() } : {}) };
  sel = { x: at[0], y: at[1], w: selClip.w, h: selClip.h, mask: piece.mask, piece, orig: piece, base: target.px.slice(), cur, al };
  moveSel(sel.x, sel.y); renderAll(); toast('Pasted: drag it into place');
}
function selectAll() { setTool('select'); sel = { x: 0, y: 0, w: W, h: H, piece: null, base: null, cur, al }; renderBoard(); }
$('selall').onclick = selectAll;
$('selcopy').onclick = copySel;
$('selcut').onclick = () => { if (!refuseLocked() && copySel()) { deleteSel(); toast('Area cut'); } };
$('selpaste').onclick = pasteSel;
$('selfliph').onclick = () => changeSel((p) => T.mirrored(p, true));
$('selflipv').onclick = () => changeSel((p) => T.mirrored(p, false));
$('selturn').onclick = () => changeSel(T.turned);
$('seldel').onclick = deleteSel;
// the outline: black under white dashes, drawn just inside the area so it shows at the canvas edge too; an area
// picked by colour shows its true shape. With the Select tool, the handles on its corners and sides.
function outlineSel(box, line) {
  const e = (v, most) => (v <= 0 ? line / 2 : v >= most ? most - line / 2 : v);
  bx.beginPath();
  if (box.mask) Sel.outline(box).forEach(([x0, y0, x1, y1]) => { bx.moveTo(e((box.x + x0) * S, W * S), e((box.y + y0) * S, H * S)); bx.lineTo(e((box.x + x1) * S, W * S), e((box.y + y1) * S, H * S)); });
  else bx.rect(box.x * S + line / 2, box.y * S + line / 2, box.w * S - line, box.h * S - line);
  bx.lineWidth = line; bx.strokeStyle = '#000'; bx.stroke();
  bx.setLineDash([line * 3, line * 3]); bx.strokeStyle = '#fff'; bx.stroke();
  bx.setLineDash([]); bx.lineDashOffset = 0; bx.lineWidth = 1;
  if (tool !== 'select' || marquee || box !== sel) return;
  const k = board.width / (board.clientWidth || board.width), r = Math.min(coarse ? 5.5 : 4.5, handleReach(coarse) * 0.4) * k;   // in screen pixels, whatever the zoom (a finger grabs well round it)
  handles().forEach(([, , hx, hy]) => { bx.fillStyle = '#fff'; bx.strokeStyle = '#000'; bx.lineWidth = Math.max(1, k); bx.fillRect(hx * S - r, hy * S - r, 2 * r, 2 * r); bx.strokeRect(hx * S - r, hy * S - r, 2 * r, 2 * r); });
  bx.lineWidth = 1;
}
// resizing: eight handles, [which way x, which way y, where (in pixels)]; a finger gets a bigger one, but never
// so big that the middle of a small area can't be grabbed to move it
const handles = () => [-1, 0, 1].flatMap((dy) => [-1, 0, 1].filter((dx) => dx || dy).map((dx) => [dx, dy, sel.x + (dx + 1) / 2 * sel.w, sel.y + (dy + 1) / 2 * sel.h]));
const handleReach = (finger) => { const cw = board.getBoundingClientRect().width / W; return Math.max(5, Math.min(finger ? 22 : 10, Math.min(sel.w, sel.h) * cw / 3)); };
function handleAt(e) {   // on a selected pixel a handle reaches in only a little, so the area's edge can still be dragged to move it
  if (!sel || marquee || selDrag) return null;
  const [x, y] = cell(e), r = board.getBoundingClientRect(), cw = r.width / W, ch = r.height / H, full = handleReach(e.pointerType !== 'mouse'), reach = inSel(x, y) ? Math.min(full * 0.4, cw * 0.35) : full;
  let best = null, near = Infinity;
  handles().forEach(([dx, dy, hx, hy]) => { const d = Math.max(Math.abs(e.clientX - r.left - hx * cw), Math.abs(e.clientY - r.top - hy * ch)); if (d <= reach && d < near) { near = d; best = [dx, dy]; } });
  return best;
}
const MAX_PIECE = 256;   // a resized piece can be bigger than the canvas (what's off it is dropped when it's put down)
let sizing = null;
// one undo step per resize: the piece stays lifted, so the step is taken with it where it is now
function sizeStep() {
  if (!sel.piece) { lift(); return true; }   // lifting takes the step
  const keep = { piece: sel.piece, base: sel.base, orig: sel.orig }; snapshot(); Object.assign(sel, keep); return false;
}
function setPiece(w, h, x, y) {   // the piece at w x h, always scaled from how it was lifted
  const o = sel.orig;
  sel.piece = w === o.w && h === o.h ? o : T.scaled(o, w, h); sel.w = w; sel.h = h; sel.mask = sel.piece.mask; moveSel(x, y);
}
function startSizing([dx, dy]) {
  if (!needSel() || refuseLocked()) return;
  const was = { x: sel.x, y: sel.y, w: sel.w, h: sel.h, mid: sel.mid, piece: sel.piece, mask: sel.mask };
  sizing = { dx, dy, was, fresh: sizeStep() };
}
function sizeTo(e) {   // the side or corner being dragged follows the pointer; the opposite one stays put
  const r = board.getBoundingClientRect(), cw = r.width / W, fx = (e.clientX - r.left) / cw, fy = (e.clientY - r.top) / (r.height / H);
  const { dx, dy, was: b } = sizing, o = sel.orig;
  const fw = dx > 0 ? fx - b.x : dx < 0 ? b.x + b.w - fx : b.w, fh = dy > 0 ? fy - b.y : dy < 0 ? b.y + b.h - fy : b.h;
  // whole times its size (and half) pull it in when the pointer is close: within 12 screen pixels, or less on a small piece
  const snap = (f, n) => { const zone = Math.max(0.75, Math.min(n / 4, 12 / cw)), k = Math.max(1, Math.round(f / n));
    const at = [k * n, ...(n % 2 ? [] : [n / 2])].find((v) => Math.abs(f - v) < zone); return clamp(at ?? Math.round(f), 1, MAX_PIECE); };
  let w, h;
  if ($('selkeep').checked || e.shiftKey) {   // keep its shape: the same proportions as when it was lifted
    const k = dx && dy ? Math.max(fw / o.w, fh / o.h) : dx ? fw / o.w : fh / o.h, n = Math.max(1, Math.round(k));
    const m = Math.min(o.w, o.h), kk = Math.abs(k - n) * m < Math.max(0.75, Math.min(m / 4, 12 / cw)) ? n : k;
    w = clamp(Math.round(kk * o.w), 1, MAX_PIECE); h = clamp(Math.round(kk * o.h), 1, MAX_PIECE);
  } else { w = dx ? snap(fw, o.w) : b.w; h = dy ? snap(fh, o.h) : b.h; }
  const x = dx < 0 ? b.x + b.w - w : dx > 0 ? b.x : Math.round(b.x + (b.w - w) / 2), y = dy < 0 ? b.y + b.h - h : dy > 0 ? b.y : Math.round(b.y + (b.h - h) / 2);
  if (w !== sel.w || h !== sel.h || x !== sel.x || y !== sel.y) setPiece(w, h, x, y);
  $('pos').textContent = `${w} × ${h}${w % o.w || h % o.h || w / o.w !== h / o.h ? '' : ` · ×${w / o.w}`}`;   // its size as you drag
}
function endSizing() {
  const b = sizing.was, same = sel.w === b.w && sel.h === b.h && sel.x === b.x && sel.y === b.y;
  if (same) { unsnapshot(); if (sizing.fresh) sel.piece = sel.base = null; }   // nothing changed: no undo step, and an area that wasn't lifted isn't
  sizing = null; renderAll();
}
function cancelSizing() {   // Escape (or the browser taking the pointer away): back as it was, and no undo step
  const b = sizing.was;
  target.px = T.placed(sel.base, sizing.fresh ? sel.orig : b.piece, b.x, b.y);
  Object.assign(sel, { x: b.x, y: b.y, w: b.w, h: b.h, mask: b.mask, piece: b.piece }); if (b.mid) sel.mid = b.mid; else delete sel.mid;
  if (sizing.fresh) sel.piece = sel.base = null;   // it wasn't lifted before
  unsnapshot(); sizing = null; renderAll();
}
function scaleSel(up) {   // exactly twice or half the size, about its middle
  if (!needSel() || refuseLocked()) return;
  const w = up ? sel.w * 2 : Math.max(1, Math.round(sel.w / 2)), h = up ? sel.h * 2 : Math.max(1, Math.round(sel.h / 2));
  if (w > MAX_PIECE || h > MAX_PIECE) return toast(`That would be bigger than ${MAX_PIECE} pixels across`);
  if (w === sel.w && h === sel.h) return toast("It's as small as it goes");
  const [cx, cy] = sel.mid || [sel.x + sel.w / 2, sel.y + sel.h / 2];
  sizeStep(); setPiece(w, h, Math.round(cx - w / 2), Math.round(cy - h / 2)); sel.mid = [cx, cy]; renderAll();
}
$('selbig').onclick = () => scaleSel(true);
$('selsmall').onclick = () => scaleSel(false);
$('selinvert').onclick = () => {   // everything on this layer that isn't selected (a lifted piece is put down where it is first)
  setTool('select');
  const was = sel ? { x: sel.x, y: sel.y, w: sel.w, h: sel.h, mask: sel.mask } : null, area = Sel.inverted(was);
  if (!area) return toast('Everything is selected: there is nothing else to select');
  sel = { ...area, piece: null, base: null, cur, al }; renderBoard();
};
const SELBY = { area: 'Drag to select an area; drag inside it to move it, or its corners and sides to resize it',
  joined: 'Click a colour to select it where it joins up', all: 'Click a colour to select all of it on this layer' };
$('selby').onchange = () => { $('selhint').textContent = SELBY[$('selby').value]; };

// -- colour -----------------------------------------------------------------------------
function setColour(c) { colour = c; $('colour').value = c; $('hex').textContent = c.toUpperCase(); paintSwatches(); }
function setColour2(c) { colour2 = c; $('colour2').value = c; }
$('colour2').addEventListener('input', (e) => setColour2(e.target.value));
$('swap').onclick = () => { const a = colour; setColour(colour2); setColour2(a); };
$('colour').addEventListener('input', (e) => setColour(e.target.value));
function swatch(c, holder) {
  const b = document.createElement('button'); b.style.background = c; b.title = c; b.setAttribute('aria-label', `Colour ${c}`);
  if (c.toLowerCase() === colour.toLowerCase()) b.className = 'sel';
  b.onclick = () => { if (Fingers.alt()) { setColour2(c); Fingers.strokeDone(); return; } setColour(c); if (tool === 'erase' || tool === 'pick') setTool('pen'); };   // with Hold = right-click: the second colour
  b.oncontextmenu = (e) => { e.preventDefault(); setColour2(c); };   // right-click: the second colour
  holder.append(b);
}
// "In this animation": the 16 most used colours, near ones counted as one (E.makeTally in animator-engine.js).
// Counted when the browser is idle, and only pictures that changed are counted again.
const tally = E.makeTally(), idle = window.requestIdleCallback ? (f) => requestIdleCallback(f, { timeout: 400 }) : (f) => setTimeout(f, 60);
let used = [], counting = false;
function paintSwatches() {
  $('swatches').replaceChildren(); PALETTE.forEach((c) => swatch(c, $('swatches')));
  $('used').replaceChildren(); used.forEach((c) => swatch(c, $('used')));
  if (!counting) { counting = true; idle(() => { counting = false; tally.update(frames.map((f) => f.px)); const now = tally.top(16);
    if (now.join() !== used.join()) { used = now; $('used').replaceChildren(); used.forEach((c) => swatch(c, $('used'))); } }); }
}

// -- change a colour (js/animator-select.js) ---------------------------------------------------
let recolourPick = false;   // { sel }: the Pick colour tool is choosing the colour to change, then the box opens again with the selection
function recolourSwatch(c) { $('rcfrom').value = c; $('rcfrom-hex').textContent = c.toUpperCase(); }
function openRecolour(from) {
  recolourPick = false;
  if (from) recolourSwatch(from);
  const used = [...new Set(frames.flatMap((f) => f.cels.flat()).filter(Boolean))].slice(0, 32);   // on any layer, hidden under others too
  $('rcused').replaceChildren(...used.map((c) => { const b = document.createElement('button'); b.type = 'button'; b.style.background = c; b.title = c; b.setAttribute('aria-label', `Change ${c}`); b.onclick = () => recolourSwatch(c); return b; }));
  $('rcsel-l').hidden = !sel; $('rcsel').checked = !!sel; $('rcmsg').textContent = '';
  openDialog($('recolourdlg'));
}
$('recolour').onclick = () => { recolourSwatch(colour); $('rcto').value = colour2; openRecolour(); };
$('rcfrom').addEventListener('input', () => recolourSwatch($('rcfrom').value));
$('rcpick').onclick = () => {
  if (sel) sel.piece = sel.base = null;   // a lifted piece is put down where it is (the layer already shows it there)
  const was = sel; $('recolourdlg').close(); setTool('pick'); recolourPick = { sel: was }; toast('Click the colour to change');
};
$('recolour-close').onclick = () => $('recolourdlg').close();
$('rcgo').onclick = () => {
  const allLayers = $('rclayers').value === 'all', l = layers[al], say = (t) => { $('rcmsg').textContent = t; };   // in the dialog: a toast would sit behind it
  if (!allLayers && (!l.on || isLocked())) return say(`"${l.name}" is ${l.on ? 'locked' : 'hidden'}: ${l.on ? 'unlock' : 'show'} it first, or change every layer`);
  const area = sel && $('rcsel').checked ? { x: sel.x, y: sel.y, w: sel.w, h: sel.h, mask: sel.mask } : null;
  snapshot();   // puts down a piece being moved, so what's changed is what you see
  const { n, skipped, shared } = Sel.recolourDrawing(frames, layers, $('rcfrom').value, $('rcto').value, { cur, al, allFrames: $('rcframes').value === 'all', allLayers, near: $('rcnear').checked ? 24 : 0, area, locked: isLocked });
  // a layer that's the same on every frame is left alone by "This frame": changing it would change them all
  const left = shared.length ? `${shared.length > 1 ? `${shared.length} layers are` : `"${shared[0]}" is`} the same on every frame, so "This frame" leaves ${shared.length > 1 ? 'them' : 'it'} alone: choose "Every frame" to change ${shared.length > 1 ? 'them' : 'it'}` : '';
  if (!n) { unsnapshot(); return say(left ? `Nothing changed. ${left}.` : `No pixels of ${$('rcfrom').value.toUpperCase()} to change there`); }
  $('recolourdlg').close(); renderAll();
  toast(`Changed ${n} pixel${n === 1 ? '' : 's'}${skipped ? ` (hidden or locked layers left alone)` : ''}: Undo takes it back.${left ? ` ${left}.` : ''}`);
};

// -- tools ------------------------------------------------------------------------------
const NAMES = { pen: 'Pencil', erase: 'Eraser', fill: 'Fill', line: 'Line', rect: 'Rectangle', ellipse: 'Circle and oval',
  gradient: 'Gradient', blur: 'Blur', shade: 'Lighten and darken', glow: 'Glow', pick: 'Pick colour', text: 'Text', select: 'Select', shift: 'Shift drawing', pan: 'Move around' };
function setTool(t) {
  if (busy()) return;   // not mid-stroke: the shape you're drawing stays the shape you started
  if (t !== 'select') sel = null;
  if (t !== 'pick') recolourPick = false;
  tool = t; document.querySelectorAll('[data-tool]').forEach((b) => { b.classList.toggle('on', b.dataset.tool === t); b.setAttribute('aria-pressed', String(b.dataset.tool === t)); });
  board.style.cursor = t === 'pan' ? 'grab' : 'crosshair'; $('toolname').textContent = `${NAMES[t]} · ${layers[al].name}`;
  $('opts').querySelectorAll('[data-for]').forEach((el) => { el.hidden = !el.dataset.for.split(' ').includes(t); });
  $('gsteps-l').hidden = t !== 'gradient' || $('gstyle').value === 'smooth';
  $('ghint').textContent = $('gtype').value === 'conic' ? 'Drag to point where the colours meet' : 'Drag from the main colour to the second one';
  if (frames.length) renderBoard();
}
const options = (id, names) => $(id).replaceChildren(...Object.entries(names).map(([v, n]) => new Option(n, v)));
options('shape', T.SHAPE_NAMES); options('gtype', T.GRADIENT_TYPES); options('gstyle', T.GRADIENT_STYLES); options('garea', T.GRADIENT_AREAS);
options('lmode', Project.MODES); options('spease', Motion.EASES); options('sporder', Motion.ORDERS);
let shape = 'square', filled = false, colour2 = '#3b6bff';
$('shape').onchange = () => { shape = $('shape').value; };
$('filled').onchange = () => { filled = $('filled').checked; };
$('gstyle').onchange = $('gtype').onchange = () => setTool(tool);
document.querySelectorAll('[data-tool]').forEach((b) => { b.onclick = () => setTool(b.dataset.tool); b.setAttribute('aria-label', NAMES[b.dataset.tool]); });
function toggle(id, get, set) { $(id).setAttribute('aria-pressed', String(get())); $(id).onclick = () => { set(!get()); $(id).classList.toggle('on', get()); $(id).setAttribute('aria-pressed', String(get())); renderBoard(); }; }
toggle('mirror', () => mirror, (v) => mirror = v);
let onionShow = 'prev', onionK = 0.35, centre = false;   // the onion skin: which frames either side and how strongly (kept between visits)
({ on: onion, show: onionShow, k: onionK } = Aids.loadOnion({ on: onion, show: onionShow, k: onionK })); $('onion').classList.toggle('on', onion);
toggle('onion', () => onion, (v) => { onion = v; onionChanged(); });
toggle('grid', () => grid, (v) => grid = v);
toggle('safe', () => showSafe, (v) => showSafe = v);
toggle('centre', () => centre, (v) => centre = v);
options('onionshow', Aids.ONION_SHOWS); options('onionk', Aids.ONION_STRENGTHS);
function onionChanged() { $('onionshow').value = onionShow; $('onionk').value = String(onionK); Aids.saveOnion({ on: onion, show: onionShow, k: onionK }); }
$('onionshow').onchange = () => { onionShow = $('onionshow').value; onionChanged(); renderBoard(); };
$('onionk').onchange = () => { onionK = Number($('onionk').value); onionChanged(); renderBoard(); };
onionChanged();
$('loop').addEventListener('change', () => renderBoard());   // on a loop, the first frame's previous is the last

// -- text: Tiny is built in; the Matrix's own fonts are fetched the first time each is picked -------
let font = T.TINY;
const fonts = new Map([['', T.TINY]]);
$('tfont').replaceChildren(new Option('Tiny (3 × 5)', ''));
$('ttext').addEventListener('input', () => { $('ttext').rows = clamp($('ttext').value.split('\n').length, 1, 6); renderBoard(); });
let textAlign = '';   // '' where you tap; or left, centre or right in the canvas (or the safe area when it shows)
function alignButtons() {
  document.querySelectorAll('[data-align]').forEach((b) => { b.classList.toggle('on', b.dataset.align === textAlign); b.setAttribute('aria-pressed', String(b.dataset.align === textAlign)); });
  $('thint').textContent = `${coarse ? 'Tap' : 'Click'} ${textAlign ? 'the row where the top of the text goes' : 'where its top-left corner goes'}`;
}
document.querySelectorAll('[data-align]').forEach((b) => { b.onclick = () => { textAlign = textAlign === b.dataset.align ? '' : b.dataset.align; alignButtons(); renderBoard(); }; });
alignButtons();
$('tfont').onchange = async () => {
  const name = $('tfont').value;
  try { if (!fonts.has(name)) fonts.set(name, await api('GET', `/api/animator/fonts/${encodeURIComponent(name)}`)); }
  catch (e) { toast(e.message); if ($('tfont').value === name) $('tfont').value = ''; }
  font = fonts.get($('tfont').value) || T.TINY;   // whatever is picked now: another may have been chosen while this one was on its way
  renderBoard();
};

// -- frames -----------------------------------------------------------------------------
// one card per frame, kept between redraws: a card's picture is painted again only when its frame's picture
// changed, and only once it's scrolled into view, so one changed frame of 300 repaints one card
const cards = [], cardOf = new Map();
let cardSeen = null, addCard = null;
function paintCard(k) { if (k.drawn === k.want || !k.want) return; k.drawn = k.want; k.c.getContext('2d').putImageData(toImage(k.want, W, H), 0, 0); }
function makeCard(i) {
  const el = document.createElement('div'); el.className = 'frame';
  const c = document.createElement('canvas'); c.style.maxHeight = '106px'; c.style.objectFit = 'contain';
  const meta = document.createElement('div'); meta.className = 'meta';
  const n = document.createElement('span'); n.textContent = String(i + 1).padStart(2, '0');
  const ms = document.createElement('input'); ms.className = 'input'; ms.type = 'number'; ms.min = 50; ms.max = 5000; ms.step = 50; ms.title = 'How long this frame shows (ms)';   // the arrows step in 50s from 50 (the Matrix draws every 50 ms: an odd time goes to the next 50); any time from 20 can be typed
  ms.setAttribute('aria-label', `Frame ${i + 1}: how long it shows, in ms`);
  ms.onclick = (e) => e.stopPropagation(); ms.onchange = () => { const f = frames[i]; snapshot(); f.ms = clamp(Number(ms.value) || 90, 20, 5000); ms.value = f.ms; renderFrames(); };
  const unit = document.createElement('span'); unit.className = 'unit'; unit.textContent = 'ms';
  const tv = document.createElement('span'); tv.className = 'tv';   // the time as words, where there's no room for the box (a phone)
  const r = document.createElement('span'); r.append(ms, ' ', unit, tv);
  meta.append(n, r); el.append(c, meta);
  el.onclick = (e) => FS.click(i, e);   // one, a Shift range, Ctrl to add or take out (js/animator-frames.js)
  const k = { el, c, ms, unit, tv, t: null, drawn: null, want: null, size: '', bg: '', seen: !cardSeen };
  cardOf.set(el, k); cardSeen?.observe(el);
  return k;
}
function renderFrames() {
  const box = $('frames');
  FS.sync();   // the selection: frames gone drop out, the current one is always in
  if (!addCard) {
    if ('IntersectionObserver' in window) cardSeen = new IntersectionObserver((es) => es.forEach((e) => { const k = cardOf.get(e.target); if (k) { k.seen = e.isIntersecting; if (k.seen) paintCard(k); } }), { root: box, rootMargin: '240px' });   // (a row on a wide screen, a grid of rows on a phone)
    addCard = document.createElement('button'); addCard.className = 'add'; addCard.textContent = '+'; addCard.title = 'New blank frame';
    addCard.onclick = () => { if (!roomFor(1)) return; snapshot(); frames.splice(cur + 1, 0, blank()); cur++; renderAll(); };
    box.replaceChildren(addCard);
  }
  while (cards.length < frames.length) { const k = makeCard(cards.length); cards.push(k); box.insertBefore(k.el, addCard); }
  while (cards.length > frames.length) { const k = cards.pop(); cardSeen?.unobserve(k.el); cardOf.delete(k.el); k.el.remove(); }
  const size = `${W}x${H}`;
  cards.forEach((k, i) => {
    const f = frames[i];
    k.el.className = 'frame' + (i === cur ? ' sel' : '') + FS.cls(f);
    if (k.size !== size) { k.size = size; k.c.width = W; k.c.height = H; k.c.style.width = `${Math.max(4, Math.min(100, 50 * W / H))}%`; k.drawn = null; }
    if (k.bg !== bg) { k.bg = bg; k.drawn = null; }
    k.want = f.px; if (k.seen) paintCard(k);
    if (document.activeElement !== k.ms && k.ms.value !== String(f.ms)) k.ms.value = f.ms;
    if (k.t !== f.ms) {   // a GIF holds hundredths of a second: show what it'll really be
      k.t = f.ms; const shown = PT.gifMs(PT.saved(f.ms)), odd = shown !== f.ms;
      k.unit.title = odd ? `A GIF keeps hundredths of a second, so the Matrix shows this frame for ${shown} ms` : ''; k.ms.title = odd ? k.unit.title : 'How long this frame shows (ms)'; k.ms.style.borderColor = odd ? 'var(--amber)' : '';
      k.unit.textContent = odd ? `\u2192${shown}` : 'ms'; k.unit.classList.toggle('odd', odd); k.tv.textContent = odd ? `${f.ms}\u2192${shown}` : String(f.ms); k.tv.classList.toggle('odd', odd); k.tv.title = k.unit.title;   // 65\u219270: what the Matrix will show
    }
  });
  // keep the selected frame visible by scrolling the strip (sideways; down, as a phone's grid), never the page
  const picked = box.children[cur];
  if (picked) {
    const l = picked.offsetLeft - box.offsetLeft, r = l + picked.offsetWidth, t = picked.offsetTop - box.offsetTop, b = t + picked.offsetHeight;
    if (l < box.scrollLeft) box.scrollLeft = l;
    else if (r > box.scrollLeft + box.clientWidth) box.scrollLeft = r - box.clientWidth;
    if (t < box.scrollTop) box.scrollTop = t;
    else if (b > box.scrollTop + box.clientHeight) box.scrollTop = b - box.clientHeight;
  }
  $('size').textContent = `${W} × ${H} · ${frames.length} frame${frames.length === 1 ? '' : 's'}`;
  const [count, length, ...notes] = PT.summary(frames, $('loop').value, W, H, timing());   // how long it is, as the Matrix plays it
  $('ftotal').textContent = `${count} \u00b7 ${length}`; $('fnote').textContent = notes.join(' '); $('fnote').hidden = !notes.length;
  FS.after();   // the selection's count and buttons
  Layout.framesShown();   // the readouts by Play, the scrubber, the Preview when stopped
}
$('loop').addEventListener('change', () => { restart(); renderFrames(); });
// a frame copied whole: its own layers are copies, a shared layer is the shared one still
const cloneFrame = (f) => ({ ms: f.ms, px: f.px.slice(), cels: f.cels.map((c, k) => (layers[k].shared ? c : c.slice())) });
// choosing several frames, Duplicate / Reverse / Time… / Delete on them, and dragging frames to a new place: js/animator-frames.js
const FS = window.AnimatorFrames.strip({ box: $('frames'), cards, get frames() { return frames; }, set frames(v) { frames = v; }, get cur() { return cur; }, set cur(v) { cur = v; },
  snapshot: () => snapshot(), renderAll: () => renderAll(), renderFrames: () => renderFrames(), toast: (t) => toast(t), roomFor: (n) => roomFor(n), clone: (f) => cloneFrame(f), busy: () => busy(), openDialog: (d) => openDialog(d), timeOne: () => Layout.timeOne(), mini: $('mini') });
$('dup').onclick = () => { if (!roomFor(1)) return; snapshot(); frames.splice(cur + 1, 0, cloneFrame(frames[cur])); cur++; renderAll(); };
$('del').onclick = () => { if (frames.length < 2) return toast('An animation needs at least one frame'); snapshot(); frames.splice(cur, 1); cur = Math.max(0, cur - 1); renderAll(); };
$('copy').onclick = () => {   // every selected frame; its cels by reference (read-only once settled: drawing on one copies it)
  E.settle(frames); const ix = FS.indices(); lastCopied = 'frame';
  clip = { list: ix.map((i) => ({ ms: frames[i].ms, px: frames[i].px, cels: frames[i].cels.slice() })), ids: layers.map((l) => l.id), w: W, h: H };
  toast(ix.length > 1 ? `${ix.length} frames copied` : `Frame ${cur + 1} copied`);
};
$('paste').onclick = () => {   // after this frame, in the order they were copied; the pasted frames are then the selection
  if (!clip) return toast(selClip ? 'Pick the Select tool to paste the area you copied, or copy a frame first' : 'Copy a frame first'); if (clip.w !== W || clip.h !== H) return toast(`${clip.list.length > 1 ? 'Those frames were' : 'That frame was'} copied from a canvas of another size`); if (!roomFor(clip.list.length)) return; snapshot();
  const ids = clip.ids, back = ids.every((id) => layers.some((l) => l.id === id));   // each layer back on its own layer, wherever it is now
  const own = !layers[al].shared ? al : layers.findIndex((l) => !l.shared);   // a layer it came from is gone: paste it flat onto this layer (or, if this one is the same on every frame, the nearest that isn't)
  if (!back && own < 0) { unsnapshot(); return toast('Every layer here is the same on every frame: untick that on one to paste a frame'); }
  const made = clip.list.map((c) => ({ ms: c.ms, px: c.px.slice(), cels: back ? layers.map((l) => (ids.includes(l.id) ? c.cels[ids.indexOf(l.id)] : empty())) : layers.map((_, k) => (k === own ? c.px.slice() : empty())) }));
  frames.splice(cur + 1, 0, ...made); cur++; FS.pick(made); relink(); renderAll();
  if (made.length > 1) toast(`Pasted ${made.length} frames after frame ${cur}`);
};
$('setall').onclick = () => {
  const ms = clamp(Number($('allms').value) || 90, 20, 5000), shown = PT.gifMs(ms); snapshot(); frames.forEach((f) => { f.ms = ms; }); renderFrames();
  toast(`Every frame now shows for ${ms} ms${shown !== ms ? ` (a GIF keeps hundredths of a second: the Matrix shows ${shown} ms)` : ''}`);
};
// clearing or flipping one frame leaves a layer that's the same on every frame alone
const ownCels = (change) => { snapshot(); frames[cur].cels = frames[cur].cels.map((c, k) => (layers[k].shared || isLocked(k) ? c : change(c))); renderAll(); sayLocked(); };
$('clear').onclick = () => ownCels(() => empty());   // every layer of this frame
$('fliph').onclick = () => ownCels((c) => T.flipped(c, true));
$('flipv').onclick = () => ownCels((c) => T.flipped(c, false));

// -- spread across frames (js/animator-motion.js) ---------------------------------------------
function spreadFields() {
  const mode = $('spmode').value;
  $('spreaddlg').querySelectorAll('[data-sp]').forEach((el) => { el.hidden = !el.dataset.sp.split(' ').includes(mode); });
  const piece = sel ? T.lifted(target.px, sel).px : target.px;
  if (mode === 'cycle') { if (spreadMode !== 'cycle') spreadN = $('spn').value; $('spn').value = clamp(Motion.colours(piece, 'seen').length, 2, 120); }
  else if (spreadMode === 'cycle') $('spn').value = spreadN;   // back from Cycle: the number you had
  spreadMode = mode; wrapFields();
  $('sphint').textContent = `Takes ${sel ? 'the selected area' : 'everything'} on "${layers[al].name}" and changes it a little more on each frame. It replaces what's on this layer in the frames it fills (Undo brings that back).`;
}
let spreadMode = 'move', spreadN = 8;
$('spmode').onchange = spreadFields;
// wrapping round the edges: a full trip needs no distance, and says how evenly it steps
function wrapFields() {
  const wrap = $('spwrap').checked, trip = wrap && $('spmode').value === 'move' ? $('spway').value : '';
  if ($('spmode').value === 'move') { $('spreaddlg').querySelectorAll('.spwrapped').forEach((el) => { el.hidden = !wrap; }); $('spreaddlg').querySelectorAll('.spby').forEach((el) => { el.hidden = !!trip; }); }
  const round = wrap && sel && $('spmode').value === 'move' ? 'It goes round the edges of the selected area, not the canvas. ' : '';
  $('sptrip').hidden = !trip && !round; $('sptrip').textContent = round; if (!trip) return;
  const n = clamp(Math.round(Number($('spn').value) || 8), 2, 120), b = Motion.box(sel ? { x: sel.x, y: sel.y, w: sel.w, h: sel.h } : null), [sx, sy] = Motion.TRIPS[trip];
  const ways = [sx && Motion.pace(b.w, n), sy && Motion.pace(b.h, n)].filter(Boolean), even = ways.every((p) => p.steady);
  const by = ways.map((p) => (p.steady ? `${p.lo}` : `${p.lo} or ${p.hi}`)), step = by.length > 1 ? `${by[0]} across and ${by[1]} down` : `${by[0]} pixel${by[0] === '1' ? '' : 's'}`;
  const fits = ways.reduce((a, p) => a.filter((k) => p.even.includes(k)), ways[0].even).filter((k) => k <= Project.maxFrames(W, H));
  const near = fits.filter((k) => k >= 4).sort((a, b2) => Math.abs(a - n) - Math.abs(b2 - n)).slice(0, 3).sort((a, b2) => a - b2);
  $('sptrip').textContent = round + `The frame after the last is the first again, so it loops without a jump. ${n} frames: ${step} a frame` +
    (even ? ', evenly.' : `, so it steps unevenly.${near.length ? ` For even steps use ${near.length > 1 ? `${near.slice(0, -1).join(', ')} or ${near[near.length - 1]}` : near[0]} frames.` : ''}`) +
    ($('spease').value === 'steady' ? '' : ' With this pace it speeds up or slows down once a trip, at the join: Steady keeps one speed all the way round.');
}
for (const id of ['spwrap', 'spway', 'spease']) $(id).addEventListener('change', wrapFields);
$('spn').addEventListener('input', wrapFields);
$('spread').onclick = () => {
  if (layers[al].shared) return toast(`"${layers[al].name}" is the same on every frame: untick that to spread it across frames`);
  if (!layers[al].on) return toast(`"${layers[al].name}" is hidden: show it first`);
  if (refuseLocked()) return;
  if (!target.px.some(Boolean)) return toast(`There's nothing on "${layers[al].name}" in this frame to spread`);
  spreadFields(); $('sperr').textContent = ''; openDialog($('spreaddlg'));
};
$('spread-close').onclick = () => $('spreaddlg').close();
$('spgo').onclick = () => {
  const num = (id, lo, hi, or) => clamp(Number.isFinite(Number($(id).value)) && $(id).value !== '' ? Number($(id).value) : or, lo, hi);
  const mode = $('spmode').value, n = Math.round(num('spn', 2, 120, 8)), area = sel ? { x: sel.x, y: sel.y, w: sel.w, h: sel.h, mask: sel.mask } : null;
  const say = (t) => { $('sperr').textContent = t; };   // in the dialog: a toast would sit behind it
  if (refuseLocked(al, say) || !roomFor(Math.max(0, cur + n - frames.length), say)) return;
  if (mode === 'cycle' && Motion.colours(sel ? T.lifted(target.px, sel).px : target.px).length < 2) return say('Cycling needs at least two colours');
  snapshot();   // puts down a piece being moved, so what's spread is what you see
  const src = target.px.slice(), easing = $('spease').value;
  const wrap = $('spwrap').checked, trip = wrap ? $('spway').value || null : null;
  const list = mode === 'move' ? Motion.move(src, { dx: Math.round(num('spdx', -128, 128, 0)), dy: Math.round(num('spdy', -128, 128, 0)), n, easing, area, wrap, trip })
    : mode === 'fade' ? Motion.fade(src, { from: num('spfrom', 0, 200, 100), to: num('spto', 0, 200, 0), n, easing, area })
    : Motion.cycle(src, { n, order: $('sporder').value, step: $('spback').checked ? -1 : 1, area });
  while (frames.length < cur + n) frames.push(cloneFrame(frames[frames.length - 1]));   // new frames carry on from the last one
  list.forEach((p, k) => { frames[cur + k].cels[al] = p; });
  sel = null; $('spreaddlg').close(); renderAll();
  toast(`Filled frames ${cur + 1} to ${cur + n}: Undo takes it back`);
};

// -- layers -----------------------------------------------------------------------------
function rename() {   // the name becomes a box in the layers panel: Enter or leaving it keeps the new name, Escape doesn't
  const k = al, l = layers[k], span = $('layers').children[layers.length - 1 - k]?.querySelector('.name');
  if (!span) return;
  const box = document.createElement('input'); box.className = 'input name-edit'; box.value = l.name; box.maxLength = 24; box.setAttribute('aria-label', 'Layer name');
  box.style.cssText = 'flex:1;min-width:0;padding:2px 6px;font:13px var(--ui)';
  let done = false;
  const finish = (keep) => {
    if (done) return; done = true;
    const n = box.value.trim().slice(0, 24);
    if (keep && n && n !== l.name) { snapshot(); l.name = n; }
    renderLayers(true);
  };
  box.onkeydown = (e) => { e.stopPropagation(); if (e.key === 'Enter') { e.preventDefault(); finish(true); } else if (e.key === 'Escape') { e.preventDefault(); finish(false); } };
  box.onclick = box.ondblclick = (e) => e.stopPropagation();   // not a click on the row, which would draw the panel again
  box.onblur = () => finish(true);
  span.replaceWith(box); box.focus(); box.select();
}
function renderLayers(focus = false) {
  const box = $('layers'); box.replaceChildren();
  layers.map((l, k) => [l, k]).reverse().forEach(([l, k]) => {   // top layer first, as it stacks
    const row = document.createElement('div');
    const notes = [isLocked(k) ? 'locked' : '', l.shared ? 'all' : '', l.mode === 'light' ? 'light' : '', l.opacity < 1 ? `${Math.round(l.opacity * 100)}%` : ''].filter(Boolean).join(' · ');
    const said = [isLocked(k) ? 'locked' : '', l.shared ? 'same on every frame' : '', l.mode === 'light' ? 'adds light' : '', l.opacity < 1 ? `${Math.round(l.opacity * 100)}% opacity` : ''].filter(Boolean).join(', ');
    row.className = 'layer' + (k === al ? ' sel' : '') + (l.on ? '' : ' hidden');
    row.setAttribute('aria-current', String(k === al)); row.setAttribute('aria-label', `${l.name}${said ? `, ${said}` : ''}${l.on ? '' : ', hidden'}${k === al ? ', selected' : ''}`); row.tabIndex = 0;
    const eye = document.createElement('button'); eye.className = 'eye'; eye.type = 'button';
    eye.innerHTML = l.on ? EYE_OPEN : EYE_SHUT; eye.title = l.on ? 'Hide this layer' : 'Show this layer';
    eye.setAttribute('aria-label', `${l.on ? 'Hide' : 'Show'} ${l.name}`);
    eye.onclick = (e) => { e.stopPropagation(); snapshot(); l.on = !l.on; renderAll(); $('layers').querySelectorAll('.eye')[layers.length - 1 - k]?.focus(); };
    const c = document.createElement('canvas'); c.width = W; c.height = H; c.style.width = `${Math.round(Math.min(48, 24 * W / H))}px`; c.style.height = '24px';
    c.getContext('2d').putImageData(toImage(frames[cur].cels[k], W, H), 0, 0);
    const name = document.createElement('span'); name.className = 'name'; name.textContent = l.name; name.title = 'Double-click to rename';
    const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = notes; tag.title = said;
    row.append(eye, c, name, tag);
    row.onclick = () => { al = k; renderLayers(); renderBoard(); };
    row.onkeydown = (e) => {
      if (e.target !== row) return;   // keys on the eye button are its own
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); al = k; renderLayers(true); }
      else if (e.key === 'F2') { e.preventDefault(); al = k; rename(); }
      else if (e.key === 'ArrowUp' || e.key === 'ArrowDown') { e.preventDefault(); e.stopPropagation(); al = clamp(k + (e.key === 'ArrowUp' ? 1 : -1), 0, layers.length - 1); renderLayers(true); renderBoard(); }
    };
    row.ondblclick = () => { al = k; rename(); };
    box.append(row);
  });
  if (sel && (sel.al !== al || sel.cur !== cur)) sel = null;   // the area belongs to the layer and frame it was drawn on
  if (focus) box.querySelector('.layer.sel')?.focus();
  $('ldel').disabled = layers.length < 2; $('lmerge').disabled = al === 0 || !layers[al - 1].on;
  $('lmerge').title = al > 0 && !layers[al - 1].on ? `Show "${layers[al - 1].name}" to merge into it` : 'Merge this layer into the one below';
  $('laddu').disabled = layers.length >= MAX_LAYERS; $('laddu').title = layers.length >= MAX_LAYERS ? `${MAX_LAYERS} layers is the most` : 'New layer above this one';
  $('lopacity-v').textContent = `${Math.round(layers[al].opacity * 100)}%`;
  $('toolname').textContent = `${NAMES[tool]} · ${layers[al].name}`;   // which layer you're drawing on, by the board
  $('lup').disabled = al === layers.length - 1; $('ldown').disabled = al === 0;
  $('llock').classList.toggle('on', isLocked()); $('llock').setAttribute('aria-pressed', String(isLocked())); $('llock').title = isLocked() ? `Unlock "${layers[al].name}" so it can be changed` : `Lock "${layers[al].name}": nothing can draw on it or change it until it's unlocked`;
  if (!sliding) $('lopacity').value = Math.round(layers[al].opacity * 100);
  $('lmode').value = layers[al].mode; $('lshared').checked = layers[al].shared;
  Layout.rail();   // the thumbnails by the canvas
}
const MAX_LAYERS = Project.MAX_LAYERS;
const EYE_OPEN = Layout.icon('eye', 18), EYE_SHUT = Layout.icon('eyeoff', 18);
$('laddu').onclick = () => {
  if (layers.length >= MAX_LAYERS) return toast(`Up to ${MAX_LAYERS} layers`);
  snapshot();
  let n = layers.length + 1; while (layers.some((l) => l.name === `Layer ${n}`)) n++;
  layers.splice(al + 1, 0, newLayer(nextId(), `Layer ${n}`));
  frames.forEach((f) => f.cels.splice(al + 1, 0, empty())); al++; renderAll();
};
// locked layers, by id. Locking isn't an undo step (nor is it undone by one), so Undo never unlocks a layer;
// it's saved in the drawing file. A locked layer can't be drawn on, filled, shifted, pasted into, cleared,
// spread over, merged or deleted until it's unlocked
const lockedIds = new Set();
const isLocked = (k = al) => !!layers[k] && lockedIds.has(layers[k].id);
function refuseLocked(k = al, say = toast) { if (!isLocked(k)) return false; say(`"${layers[k].name}" is locked: unlock it to change it`); return true; }
function sayLocked() { const l = layers.filter((_, k) => isLocked(k)); if (l.length) toast(`${l.length > 1 ? `${l.length} layers are` : `"${l[0].name}" is`} locked, so left as ${l.length > 1 ? 'they were' : 'it was'}`); }
$('llock').onclick = () => {
  const id = layers[al].id, on = !lockedIds.has(id);
  if (on) lockedIds.add(id); else lockedIds.delete(id);
  dirty = true; draftSoon(); renderLayers(); toast(on ? `"${layers[al].name}" is locked: nothing can change it until you unlock it` : `"${layers[al].name}" is unlocked`);
};
function duplicateLayer() {   // the selected layer copied, on every frame, into a new one just above it
  if (layers.length >= MAX_LAYERS) return toast(`${MAX_LAYERS} layers is the most: merge or delete one to make room for a copy`);
  snapshot();
  const l = layers[al], copy = { ...l, id: nextId(), name: `${l.name.slice(0, 19)} copy` };
  const one = l.shared ? frames[0].cels[al].slice() : null;   // the same on every frame: one copy, that every frame shares
  frames.forEach((f) => f.cels.splice(al + 1, 0, one || f.cels[al].slice()));
  layers.splice(al + 1, 0, copy); al++; renderAll();
  toast(`Made "${copy.name}" above "${l.name}"`);
}
$('ldup').onclick = duplicateLayer;
function moveLayer(d) {
  const j = al + d; if (j < 0 || j >= layers.length) return; snapshot();
  [layers[al], layers[j]] = [layers[j], layers[al]];
  frames.forEach((f) => { [f.cels[al], f.cels[j]] = [f.cels[j], f.cels[al]]; }); al = j; renderAll();
}
$('lrename').onclick = rename;
$('lup').onclick = () => moveLayer(1); $('ldown').onclick = () => moveLayer(-1);
$('lmerge').onclick = () => {   // this layer laid over the one below, with its own opacity and light, in every frame (the one below keeps its settings)
  if (al === 0) return;
  if (refuseLocked(al) || refuseLocked(al - 1)) return;
  if (!layers[al - 1].on) return toast(`Show "${layers[al - 1].name}" first to merge into it`);
  if (!layers[al].on) toast(`"${layers[al].name}" was hidden, so nothing of it was added (Undo brings it back)`);
  snapshot();
  const pair = [{ ...layers[al - 1], opacity: 1, mode: 'normal' }, layers[al]], shared = layers[al - 1].shared && layers[al].shared;
  const merged = new Map();   // two layers that are both the same on every frame merge into one that still is
  frames.forEach((f) => { const key = shared ? 0 : f; if (!merged.has(key)) merged.set(key, T.compose([f.cels[al - 1], f.cels[al]], pair, bg)); f.cels.splice(al - 1, 2, merged.get(key)); });
  layers[al - 1].shared = shared; layers.splice(al, 1); al--; renderAll();
};
$('ldel').onclick = () => {
  if (layers.length < 2 || refuseLocked()) return; snapshot();
  const name = layers[al].name;
  frames.forEach((f) => f.cels.splice(al, 1)); layers.splice(al, 1); al = Math.max(0, al - 1); renderAll();
  toast(`Deleted "${name}" from every frame: Undo brings it back`);
};
let sliding = false;   // one undo step for a whole drag of the opacity slider
$('lopacity').addEventListener('input', () => { if (!sliding) { snapshot(); sliding = true; } layers[al].opacity = clamp(Number($('lopacity').value), 0, 100) / 100; renderBoard(); renderLayers(); });
$('lopacity').addEventListener('change', () => { sliding = false; renderAll(); });
$('lmode').onchange = () => { snapshot(); layers[al].mode = $('lmode').value; renderAll(); };
$('lshared').onchange = () => {
  if (refuseLocked()) { $('lshared').checked = layers[al].shared; return; }
  snapshot();
  const l = layers[al], here = frames[cur].cels[al];
  l.shared = $('lshared').checked;
  if (l.shared) frames.forEach((f) => { f.cels[al] = here; });   // this frame's drawing becomes every frame's
  else frames.forEach((f) => { f.cels[al] = here.slice(); });    // each frame gets its own copy to change
  renderAll();
  toast(l.shared ? `"${l.name}" now shows this frame's drawing on every frame (Undo brings the others back)` : `"${l.name}" is now its own drawing on each frame`);
};
function move(d) { const j = cur + d; if (j < 0 || j >= frames.length) return; snapshot(); [frames[cur], frames[j]] = [frames[j], frames[cur]]; cur = j; renderAll(); }
$('left').onclick = () => move(-1); $('right').onclick = () => move(1);
const isWhole = (s) => !!s.whole;
$('undo').onclick = () => { if (busy()) return; if (undo.length) { const s = undo.pop(); redo.push(state(isWhole(s))); restore(s); } };
$('redo').onclick = () => { if (busy()) return; if (redo.length) { const s = redo.pop(); undo.push(state(isWhole(s))); restore(s); } };

// -- preview playback -------------------------------------------------------------------
let playing = true, pi = 0, timer = null, tl = null;
let mpos = 0, lastTick = 0, holdUntil = 0;   // how far into the animation (ms, at the Matrix's speed), when it was last drawn, and until when a finished "Once" rests
const show = (f) => px.putImageData(toImage(f.px, W, H, { percent: dim }), 0, 0);
// the preview is drawn as the Matrix draws it (js/animator-timing.js): every 50 ms of real time, showing the frame whose time covers
// that moment. It goes by the clock, not by counting timer calls, which fire late (and not at all in a background tab).
// (worked out again only when a frame's picture or time changed: frames that look the same share one picture, E.idOf)
function timing() { const mode = $('loop').value, key = `${mode}|${W}x${H}|${frames.map((f) => `${E.idOf(f.px)}:${f.ms}`).join()}`; if (!tl || tl.key !== key) { tl = PT.timeline(frames, mode, W, H); tl.mode = mode; tl.key = key; } return tl; }
function restart() { mpos = 0; lastTick = 0; holdUntil = 0; tl = null; }
function tick() {
  if (!frames.length) return;
  const mode = $('loop').value, speed = clamp(Number($('speed').value) || 100, 10, 400) / 100, now = performance.now();
  if (!lastTick) { lastTick = now; tl = null; }
  if (holdUntil) { if (now < holdUntil) { timer = setTimeout(tick, holdUntil - now); return; } restart(); lastTick = now; }   // "Once": the preview rested on its last frame, now shows it again
  const ticks = Math.floor((now - lastTick) / PT.TICK);   // the Matrix's drawings since the last one (several if the page was away: it is just at the right place now)
  if (ticks > 0) { const before = mpos; mpos += ticks * PT.TICK * speed; lastTick += ticks * PT.TICK; if (Math.floor(mpos / timing().total) > Math.floor(before / tl.total)) tl = null; }
  const at = PT.frameAt(timing(), mpos + 0.5 * speed, 1, mode === 'once');
  pi = clamp(at.frame, 0, frames.length - 1); show(frames[pi]);
  if (at.done) holdUntil = now + 1200;
  timer = setTimeout(tick, Math.max(4, lastTick + PT.TICK - performance.now()));
}
$('play').onclick = () => { playing = !playing; Layout.played(playing); clearTimeout(timer); if (playing) { lastTick = 0; tick(); } else show(frames[cur]); };   // (play carries on from where it stopped)
function dimPicker(sleep) {   // how it looks dimmed: the sleep screen's own brightness is in the list
  const list = [...new Set([100, 50, 25, sleep, 5])].filter((v) => v >= 1 && v <= 100).sort((a, b) => b - a);
  $('dim').replaceChildren(...list.map((v) => new Option(v === 100 ? 'Off' : v === sleep ? `${v}% (Sleep)` : `${v}%`, v)));
  $('dim').value = String(dim);
}
$('dim').onchange = () => { dim = Number($('dim').value) || 100; if (!playing) show(frames[cur]); };

// -- talking to the Matrix ----------------------------------------------------------------
// ?demo (the demo dashboard, no Matrix behind it): everything works except what needs one
const DEMO = new URLSearchParams(location.search).has('demo');
function toast(t) { const el = $('toast'); el.textContent = t; el.style.display = 'block'; clearTimeout(toast.t); toast.t = setTimeout(() => el.style.display = 'none', Math.max(2600, t.length * 60)); }   // a long one stays long enough to read
async function api(method, path, body) {
  const init = { method, credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } };
  if (body instanceof FormData) { init.headers['X-Bongo-Upload'] = '1'; init.body = body; }
  else if (method !== 'GET') { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body ?? {}); }
  let res;
  try { res = await fetch(path, init); } catch { throw new Error("Can't reach the Matrix. Is it switched on and on this network?"); }
  let data = {};
  try { data = await res.json(); } catch { /* no body */ }
  if (!res.ok) throw Object.assign(new Error(data.error || (res.status === 401 ? 'This browser is not paired with the Matrix' : `${res.status} error`)), { exists: data.exists });   // exists: a name that's taken (rename, duplicate)
  return data;
}
// a GIF holds 256 colours: said quietly after Save to library and Show on Matrix when the animation uses more (the saved drawing keeps them all)
const colourNote = (r) => (r && r.reduced ? ` It uses ${r.colours} colours and a GIF holds 256, so soft shading may look slightly different on the Matrix.` : '');
async function send(extra) {
  if (DEMO) return { name: `uploaded/${(extra.name || 'animation').replace(/[^A-Za-z0-9_-]+/g, '_')}.gif`, demo: true };
  return api('POST', '/api/animator', { width: W, height: H, frames: frames.map((f) => ({ px: f.px, ms: f.ms })), loop: $('loop').value, background: bg, ...extra });
}
$('show').onclick = async () => {
  $('show').disabled = true;
  try { const r = await send({ show: true }); toast(DEMO ? 'This is the demo: on your Matrix it plays there for 15 seconds' : `Playing on the Matrix for 15 seconds.${colourNote(r)}`); }
  catch (e) { toast(e.message); } finally { $('show').disabled = false; }
};
// the drawing as a project: what Save keeps on the Matrix and Open reads back, layers and all
const cleanName = (t) => (t.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '_').replace(/^[_-]+/, '').slice(0, 40).replace(/[_-]+$/, '') || 'animation');   // as the Matrix cleans a name
const projectName = () => cleanName($('name').value);
const projectFile = () => Project.encode({ name: projectName(), w: W, h: H, bg, loop: $('loop').value, layers: layers.map((l) => ({ ...l, locked: lockedIds.has(l.id) })), frames });
async function keep() {
  const name = projectName();
  if (DEMO) return name;
  const known = name !== savedAs && (await api('GET', '/api/animator/projects')).projects.find((p) => p.name === name);
  if (known && !known.built_in && !await ask({ title: `Replace "${name}"?`, text: `There's already a drawing called "${name}".${known.replaces_built_in ? ' It is drawn instead of a built-in one on the Matrix.' : ''} Saving replaces it with this one, and what was saved is lost.`, ok: 'Replace it', cancel: 'Go back', danger: true, more: known.replaces_built_in ? 'See it where it\'s used' : undefined })) return null;
  if (known && known.built_in) {   // a built-in's name: modules would draw this one instead, so offer to see that first (js/animator-where.js)
    for (let again = true; again;) {
      const a = await ask({ title: `Replace the built-in "${name}"?`, text: `"${name}" is one of the Matrix's built-in drawings. Saved under that name, yours is drawn instead wherever a screen uses it (delete yours later and the built-in one comes back).`, ok: 'Use mine', cancel: 'Go back', more: "See it where it's used" });
      if (a === 'more') await WhereUsed.show({ name, file: projectFile(), api }); else if (!a) return null; else again = false;
    }
  }
  savedAs = (await api('PUT', `/api/animator/projects/${name}`, projectFile())).name;
  return savedAs;
}
$('keep').onclick = async () => {
  $('keep').disabled = true;
  try { const name = await keep(); if (name === null) return; dirty = false; toast(DEMO ? `This is the demo. On your own Matrix, Save keeps "${name}" so you can open it again` : `Saved as "${name}". Find it under Open…`); }
  catch (e) { toast(e.message); } finally { $('keep').disabled = false; }
};
$('save').onclick = async () => {
  $('save').disabled = true;
  try {
    const made = await send({ name: $('name').value || 'animation' }), { name } = made, note = colourNote(made);
    const file = name.replace('uploaded/', '');
    if (DEMO) { toast(`This is the demo: on your Matrix it saves ${file} to your media library`); return; }
    try { if (await keep() !== null) dirty = false; toast(`Saved ${file} to your media library.${note}`); }
    catch (e) { toast(`Saved ${file} to your media library, but not the drawing to open again: ${e.message}.${note}`); }
  } catch (e) { toast(e.message); } finally { $('save').disabled = false; }
};
function openDialog(dlg) { if (dlg.open) return; if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', ''); }
function load(d, what) {   // a project (from the Matrix or a file) replaces the drawing; Undo brings the old one back
  snapshot(true); sel = null;
  setSize(d.w, d.h);
  // new ids (a file numbers its layers 1, 2...): a frame copied from the old drawing then pastes flat, not onto a stranger's layer
  const fresh = d.layers.map(({ locked, ...l }) => { const id = nextId(); if (locked) lockedIds.add(id); return { ...l, id }; });   // locks are kept by id
  layers = fresh; frames = d.frames.map((f) => ({ ms: f.ms, px: empty(), cels: f.cels }));
  al = layers.length - 1; cur = 0; pi = 0; bg = d.bg; $('bg').value = bg; $('loop').value = d.loop; $('name').value = d.name;
  savedAs = null; renderAll(); toast(`Opened ${what}: Undo brings back what was here`);
}
// the Open list: asked for once per opening; the filter and the buttons redraw from it (js/animator-open.js)
let openList = [];
const openFolds = new Set();   // the folded sections left open
const openSay = (t) => { const p = $('open-status'); p.textContent = t || ''; p.hidden = !t; };   // in the dialog: a toast would sit behind it
function drawOpenList(focus) {
  const dlg = $('opendlg'), top = dlg.scrollTop, body = $('open-body');
  const { count } = OpenList.build(body, openList, { query: $('open-filter').value, opened: openFolds, root: dlg, url: (p) => `/api/animator/projects/${p.name}/thumb.png?v=${p.rev}`, hooks: { open: openProject, rename: renameProject, duplicate: duplicateProject, remove: deleteProject } });
  if (!count) { const m = document.createElement('p'); m.className = 'muted'; m.textContent = openList.length ? `Nothing is called "${$('open-filter').value.trim()}".` : 'Nothing saved yet. Save keeps a drawing here, with its layers, to open again.'; body.append(m); }
  dlg.scrollTop = top;
  if (focus) { const row = [...body.querySelectorAll('.project')].find((r) => r.dataset.name === focus); row?.scrollIntoView({ block: 'nearest' }); (row?.querySelector('.pick') || $('open-filter')).focus(); }
}
async function loadOpenList(focus) { openList = (await api('GET', '/api/animator/projects')).projects; drawOpenList(focus); }
$('open').onclick = async () => {
  const body = $('open-body'); body.replaceChildren(); openSay(''); $('open-filter').value = '';
  openDialog($('opendlg'));
  if (matchMedia('(pointer: coarse)').matches) $('open-close').focus();   // not the filter: that would raise a phone's keyboard
  const say = (t) => { const p = document.createElement('p'); p.className = 'muted'; p.textContent = t; body.append(p); };
  if (DEMO) return say('This is the demo. On your own Matrix, everything you save is listed here to open again. "Open a drawing file…" under File works here too.');
  try { await loadOpenList(); } catch (e) { say(e.message); }
};
$('open-filter').oninput = () => drawOpenList();
$('open-filter').onkeydown = (e) => { if (e.key === 'Escape' && e.target.value) { e.preventDefault(); e.target.value = ''; drawOpenList(); } };   // the first Escape clears the filter, the next closes
// opening a saved drawing over unsaved changes asks first (council R6); Undo still brings them back in this sitting
const loseChanges = (what) => !dirty || ask({ title: `Open ${what}?`, text: "This drawing has changes you haven't saved. Open another and lose them? (Undo brings them back while this page stays open.)", ok: 'Open it', cancel: 'Go back', danger: true });
async function openProject(p) {
  if (!await loseChanges(`"${p.name}"`)) return;
  try { const d = Project.decode(await api('GET', `/api/animator/projects/${p.name}`)); $('opendlg').close(); load(d, `"${p.name}"`); savedAs = p.built_in ? null : p.name; dirty = false; }
  catch (e) { openSay(e.message); }
}
// Rename and Duplicate: a name that's taken is asked about (the Matrix answers "taken" until told to replace)
async function copyProject(p, how, to) {
  const post = async (replace) => (await api('POST', `/api/animator/projects/${p.name}/${how}`, { to, replace })).name;
  const sure = (kind) => {
    const mine = kind === 'own', verb = how === 'rename' ? `"${p.name}" becomes "${to}"` : `"${to}" is replaced by a copy of "${p.name}"`;
    return ask(mine
      ? { title: `Replace "${to}"?`, text: `There's already a drawing called "${to}". ${verb[0].toUpperCase()}${verb.slice(1)}, and what was saved as "${to}" is lost.`, ok: 'Replace it', cancel: 'Go back', danger: true }
      : { title: `Replace the built-in "${to}"?`, text: `"${to}" is one of the Matrix's built-in drawings. Yours would be drawn instead wherever a screen uses it. Delete yours later and the built-in one comes back.`, ok: 'Use mine', cancel: 'Go back' });
  };
  if (to !== undefined) {   // asked about before anything is sent, from the list on screen; the Matrix says "taken" too, in case that list is out of date
    to = cleanName(to);
    const hit = to !== p.name && openList.find((q) => q.name === to);
    if (hit) return await sure(hit.built_in ? 'built_in' : 'own') ? post(true) : null;
  }
  try { return await post(false); }
  catch (e) { if (!e.exists) throw e; return await sure(e.exists) ? post(true) : null; }
}
async function renameProject(p) {
  const asked = await ask.input({ title: `Rename "${p.name}"`, text: `Letters, digits, - and _ (anything else becomes _).${p.replaces_built_in ? ' This replaces a built-in drawing: renamed, the built-in one comes back.' : ''}`, label: 'New name', value: p.name, maxLength: 40, ok: 'Rename',
    validate: (t) => (/[a-z0-9]/i.test(t) ? '' : 'Give it a name with at least one letter or digit') });
  if (asked === null) return;
  try {
    const name = await copyProject(p, 'rename', asked);
    if (name === null) return;
    if (savedAs === p.name) { savedAs = name; if (projectName() === p.name) $('name').value = name; }   // the drawing that's open follows its file
    await loadOpenList(name); openSay(`Renamed to "${name}"`);
  } catch (e) { openSay(e.message); }
}
async function duplicateProject(p) {
  try { const name = await copyProject(p, 'duplicate'); if (name === null) return; await loadOpenList(name); openSay(`Copied as "${name}"`); }
  catch (e) { openSay(e.message); }
}
async function deleteProject(p) {
  if (!await ask({ title: `Delete "${p.name}"?`, text: p.replaces_built_in ? 'The built-in one comes back.' : "This can't be undone.", ok: 'Delete', danger: true })) return;
  const rows = [...$('open-body').querySelectorAll('.project')], at = rows.findIndex((r) => r.dataset.name === p.name), next = (rows[at + 1] || rows[at - 1])?.dataset.name;
  try { await api('DELETE', `/api/animator/projects/${p.name}`); if (savedAs === p.name) savedAs = null; await loadOpenList(next); openSay(`Deleted "${p.name}"`); }
  catch (e) { openSay(e.message); }
}
$('open-close').onclick = () => $('opendlg').close();
for (const id of ['opendlg', 'spreaddlg', 'importdlg', 'recolourdlg']) $(id).addEventListener('click', (e) => { if (e.target === $(id)) $(id).close(); });   // a click on the backdrop
// More: files on this computer
const closeMore = () => { $('more').open = false; };
function download(name, blob) { const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name; document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 1000); }
$('savefile').onclick = () => { closeMore(); download(`${projectName()}.json`, new Blob([Project.stringify(projectFile())], { type: 'application/json' })); };
$('savepng').onclick = () => {   // every frame in reading order, see-through where nothing is drawn
  closeMore();
  const cols = Math.min(frames.length, Math.max(1, Math.floor(2048 / W)));   // rows of frames, so a long animation still fits a picture
  const c = document.createElement('canvas'); c.width = W * cols; c.height = H * Math.ceil(frames.length / cols);
  frames.forEach((f, i) => c.getContext('2d').putImageData(toImage(f.px, W, H, { alpha: true }), (i % cols) * W, Math.floor(i / cols) * H));
  c.toBlob((blob) => download(`${projectName()}.png`, blob), 'image/png');
};
$('openfile').onclick = () => { closeMore(); $('projectfile').click(); };
$('projectfile').onchange = async () => {
  const file = $('projectfile').files[0]; $('projectfile').value = '';
  if (!file) return;
  try { const d = Project.decode(JSON.parse(await file.text())); if (await loseChanges(file.name)) load(d, file.name); }
  catch (e) { toast(e instanceof SyntaxError ? `${file.name} isn't an Animator project` : `Couldn't open ${file.name}: ${e.message}`); }
};
// a picture to trace over (More): painted on the board only, at a strength, under or over the drawing. It never
// leaves this page, isn't kept anywhere and goes when the page does
let tracing = null;   // { img, url, k, over, on }
$('where').onclick = async () => { closeMore(); if (DEMO) return toast('This is the demo. On your own Matrix, this shows the screens that draw a built-in drawing, with yours in its place'); await WhereUsed.show({ name: projectName(), file: projectFile(), api }); };
$('trace').onclick = () => { closeMore(); $('tracefile').click(); };
$('tracefile').onchange = () => {
  const file = $('tracefile').files[0]; $('tracefile').value = '';
  if (!file) return;
  const url = URL.createObjectURL(file), img = new Image();
  img.onload = () => {
    if (tracing) URL.revokeObjectURL(tracing.url);
    $('tracewhere').value = 'over'; $('tracek').value = '45';   // a new picture starts over the drawing at part strength: under a filled-in drawing it would hide
    tracing = { img, url, k: Number($('tracek').value) / 100, over: true, on: true };
    $('traceon').checked = true; Layout.traced(true); renderBoard();
    toast('Draw over the picture: it stays on this page and is never part of the drawing');
  };
  img.onerror = () => { URL.revokeObjectURL(url); toast(`${file.name} isn't a picture this browser can show`); };
  img.src = url;
};
$('tracek').addEventListener('input', () => { if (tracing) { tracing.k = Number($('tracek').value) / 100; renderBoard(); } });
$('tracewhere').onchange = () => { if (tracing) { tracing.over = $('tracewhere').value === 'over'; renderBoard(); } };
$('traceon').onchange = () => { if (tracing) { tracing.on = $('traceon').checked; Layout.traced(true); renderBoard(); } };
$('tracedel').onclick = () => { if (tracing) URL.revokeObjectURL(tracing.url); tracing = null; Layout.traced(false); renderBoard(); };
document.addEventListener('click', (e) => { if ($('more').open && !$('more').contains(e.target)) closeMore(); });
$('more').addEventListener('toggle', () => {   // the menu opens leftwards from its button, unless that would run off the screen
  const list = $('more').querySelector('.menu-list'); list.style.right = '0'; list.style.left = 'auto';
  if ($('more').open && list.getBoundingClientRect().left < 8) { list.style.right = 'auto'; list.style.left = '0'; }
});

// -- keys -------------------------------------------------------------------------------
document.addEventListener('keydown', (e) => {
  if (document.querySelector('dialog[open]')) return;   // an open dialog has the keyboard
  if (sizing && e.key === 'Escape') { e.preventDefault(); cancelSizing(); return; }   // a resize being dragged: Escape puts it back
  if (busy()) return;   // mid-stroke: nothing changes the frame under the pointer
  if (e.target.tagName === 'BUTTON' && (e.key === ' ' || e.key === 'Enter')) return;   // a focused button's own key
  const ctrl = e.ctrlKey || e.metaKey, k = (e.key || '').toLowerCase();
  if (ctrl && !e.altKey && k === 's') { e.preventDefault(); closeMore(); $('keep').click(); return; }   // wherever the cursor is, the name box included
  if ($('more').open) { if (e.key === 'Escape') { closeMore(); $('more').querySelector('summary').focus(); } return; }   // the open menu has the keyboard
  if (e.altKey || (ctrl && !'zycvxas'.includes(k))) return;   // the browser's own shortcuts stay its own
  const typing = e.target.tagName === 'SELECT' || e.target.tagName === 'SUMMARY' || e.target.tagName === 'TEXTAREA' || (e.target.tagName === 'INPUT' && !['checkbox', 'range', 'color'].includes(e.target.type));
  if (typing) return;
  if (e.target.tagName === 'INPUT' && !ctrl && (e.key === ' ' || e.key.startsWith('Arrow'))) return;   // a tick box's or slider's own keys
  const area = tool === 'select' && sel;   // with an area selected, copy, paste and the arrows work on it, not on frames
  if (ctrl) {
    e.preventDefault();
    if (k === 'z') (e.shiftKey ? $('redo') : $('undo')).click();
    else if (k === 'y') $('redo').click();
    else if (k === 'a') selectAll();
    else if (k === 'x') { if (area) $('selcut').click(); }
    else if (k === 'c') (area ? $('selcopy') : $('copy')).click();
    else if (k === 'v') (lastCopied === 'area' ? $('selpaste') : $('paste')).click();   // whichever was copied last
    return;
  }
  const map = { p: 'pen', e: 'erase', f: 'fill', l: 'line', r: 'rect', c: 'ellipse', d: 'gradient', b: 'blur', s: 'shade', h: 'glow', i: 'pick', t: 'text', a: 'select', v: 'shift', n: 'pan' };
  const arrow = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[e.key];
  if (map[k]) setTool(map[k]);
  else if (k === '[' || k === ']') { const sizes = [...$('brush').options].map((o) => Number(o.value)), at = sizes.indexOf(brush);
    brush = sizes[clamp(at + (k === ']' ? 1 : -1), 0, sizes.length - 1)]; $('brush').value = String(brush); }
  else if (k === '+' || k === '=') zoomBy(1); else if (k === '-') zoomBy(-1);
  else if (k === 'x') $('swap').click();
  else if (k === 'm') $('mirror').click(); else if (k === 'o') $('onion').click(); else if (k === 'g') $('grid').click();
  else if (k === 'w') Layout.togglePreview(); else if (e.key === '?') Layout.help();   // the Preview window; the keys
  else if (area && arrow) { e.preventDefault(); if (needSel() && !refuseLocked()) { lift(); moveSel(sel.x + arrow[0], sel.y + arrow[1]); renderAll(); } }
  else if (e.key === ' ' && e.target.tagName === 'INPUT') return;
  else if (area && (e.key === 'Delete' || e.key === 'Backspace')) { e.preventDefault(); deleteSel(); }
  else if (e.key === 'Escape') { sel = null; renderBoard(); }
  else if (e.key === 'ArrowLeft') { cur = (cur - 1 + frames.length) % frames.length; renderAll(); }
  else if (e.key === 'ArrowRight') { cur = (cur + 1) % frames.length; renderAll(); }
  else if (e.key === ' ') { e.preventDefault(); $('play').click(); }
});

let keyAt = 0;
document.addEventListener('keydown', () => { keyAt = Date.now(); }, true);
document.addEventListener('change', (e) => { if (e.target.tagName === 'SELECT' && Date.now() - keyAt > 600) e.target.blur(); });   // a picker used with the mouse hands the keyboard back

function renderAll() { E.settle(frames); flatAll(); renderFrames(); renderLayers(); renderBoard(); paintSwatches(); draftSoon(); }   // settle: the cels just drawn on become read-only, so the next change copies

$('brush').onchange = () => { brush = Number($('brush').value) || 1; };
let tinting = false;   // one undo step for a whole go at the colour picker
$('bg').addEventListener('input', () => { if (!tinting) { snapshot(); tinting = true; } bg = $('bg').value; renderAll(); });
$('bg').addEventListener('change', () => { tinting = false; });
// the presets gallery: each one plays as a little live thumbnail; picking one replaces the frames
let galleryTimer = 0, galleryWatch = null;
const thumbCache = new Map(), builtFrames = new Map();
// a preset at the canvas's size (drawn at it, or set in the middle), or at 64 x 32 where it won't fit (then the page offers to switch)
const presetKey = (id) => `${id}@${W}x${H}`;
const presetSize = (p) => (PresetSizes.fit(p, W, H) === 'no' ? [PRESET_W, PRESET_H] : [W, H]);
function presetFrames(id) { const k = presetKey(id); if (!builtFrames.has(k)) builtFrames.set(k, PresetSizes.build(preset(id), W, H) || atPanel(() => preset(id).make())); return builtFrames.get(k); }
const shot = (f, [w, h]) => ({ img: toImage(f.px, w, h), ms: f.ms });
function openGallery() {
  const body = $('gallery-body');
  if ($('gallery-filter').value) { $('gallery-filter').value = ''; if (body.childElementCount) filterGallery(); }
  if (body.dataset.size !== `${W}x${H}`) {   // built for the canvas it is opened on: a new size gets new cards
    body.replaceChildren(); body.dataset.size = `${W}x${H}`;
    const groups = [...new Set(PRESETS.map((p) => p.group))];
    groups.forEach((g) => {
      const h = document.createElement('h3'); h.textContent = g; body.append(h);
      const picks = document.createElement('div'); picks.className = 'picks'; body.append(picks);
      PRESETS.filter((p) => p.group === g).forEach((p) => {
        const b = document.createElement('button'); b.type = 'button'; b.className = 'pick'; b.dataset.id = p.id;
        const [cw, ch] = presetSize(p), how = PresetSizes.fit(p, W, H);
        const c = document.createElement('canvas'); c.width = cw; c.height = ch; c.style.aspectRatio = `${cw} / ${ch}`;
        const name = document.createElement('b'); name.textContent = p.name;
        const about = document.createElement('span'); about.textContent = p.about;
        b.append(c, name, about);
        if (how !== 'native') {   // a 64 x 32 composition on another size: said on the card, where a phone shows it too
          const note = document.createElement('em'); note.className = 'fit';
          note.textContent = how === 'centred' ? `64 × 32 · set in the middle` : how === 'scaled' ? `64 × 32 · doubled` : `64 × 32 only`;
          b.title = how === 'centred' ? `Drawn for 64 × 32: on this ${W} × ${H} canvas it is set in the middle` : how === 'scaled' ? `Drawn for 64 × 32: on this ${W} × ${H} canvas every pixel becomes a block` : `Drawn for 64 × 32: it asks to switch the canvas to that size`;
          b.append(note);
        }
        b.onclick = () => choose(p);
        picks.append(b);
      });
    });
  }
  // thumbnails: each preset is built once (and kept), at most one per animation frame so opening stays
  // quick, and only the cards on screen are built and played
  const thumbs = [...body.querySelectorAll('.pick')].map((b) => ({ ctx: b.querySelector('canvas').getContext('2d'), card: b, id: b.dataset.id, k: -1, at: 0, seen: false }));
  galleryWatch?.disconnect();
  galleryWatch = 'IntersectionObserver' in window ? new IntersectionObserver((es) => es.forEach((e) => { const t = thumbs.find((x) => x.card === e.target); if (t) t.seen = e.isIntersecting; }), { root: $('gallery') }) : null;   // the dialog is what scrolls
  thumbs.forEach((t) => { if (galleryWatch) galleryWatch.observe(t.card); else t.seen = true; });
  const cached = (id) => { const k = presetKey(id); if (!thumbCache.has(k)) thumbCache.set(k, { make: PresetSizes.peek(preset(id), W, H), size: presetSize(preset(id)), shots: [] }); return thumbCache.get(k); };
  // each tick builds a few more thumbnail frames (about 6 ms' worth, cards on screen only); a card plays once it's whole
  const step = (now) => {
    const until = performance.now() + 6;
    for (const t of thumbs) {                 // first, a still for every card on screen, so none sits black
      if (!t.seen || performance.now() > until) continue;
      const th = cached(t.id);
      if (!th.shots.length) th.shots.push(shot(th.make.frameAt(0), th.size));
      if (t.k < 0) t.ctx.putImageData(th.shots[0].img, 0, 0);
    }
    for (const t of thumbs) {
      if (!t.seen) continue;
      const th = cached(t.id);
      while (th.shots.length < th.make.length && performance.now() < until) {
        th.shots.push(shot(th.make.frameAt(th.shots.length), th.size));
        if (th.shots.length === 1) t.ctx.putImageData(th.shots[0].img, 0, 0);
      }
      if (th.shots.length < th.make.length) continue;
      if (t.k >= 0 && now - t.at < th.shots[t.k].ms) continue;
      t.at = now; t.k = (t.k + 1) % th.shots.length; t.ctx.putImageData(th.shots[t.k].img, 0, 0);
    }
    galleryTimer = requestAnimationFrame(step);
  };
  galleryTimer = requestAnimationFrame(step);
  openDialog($('gallery'));
}
async function choose(p) {
  closeGallery();
  const how = PresetSizes.fit(p, W, H);   // 'native': drawn at the canvas's size; 'centred': a 64 x 32 picture set in the middle; 'no': too big for the canvas
  if (how === 'no' && !await ask({ title: `Switch to ${PRESET_W} × ${PRESET_H}?`, text: `"${p.name}" is drawn for the whole ${PRESET_W} × ${PRESET_H} panel and won't fit on a ${W} × ${H} canvas. Switch the canvas to ${PRESET_W} × ${PRESET_H}? (Undo brings back what's here.)`, ok: `Switch to ${PRESET_W} × ${PRESET_H}`, cancel: 'Go back' })) return;
  snapshot(true); sel = null;
  if (how === 'no') setSize(PRESET_W, PRESET_H);
  frames = oneLayer(copyFrames(presetFrames(p.id))); cur = 0; pi = 0;
  $('name').value = p.id === 'blank' ? 'animation' : p.id; savedAs = null;
  renderAll(); toast(`Started from ${p.name}${how === 'centred' ? `, set in the middle of the ${W} × ${H} canvas` : how === 'scaled' ? `, each pixel made ${W / PRESET_W} × ${W / PRESET_W} to fill the ${W} × ${H} canvas` : ''}: Undo brings your frames back`);
}
function closeGallery() { cancelAnimationFrame(galleryTimer); const dlg = $('gallery'); if (dlg.open) dlg.close(); }
$('presets').onclick = openGallery;
$('gallery-close').onclick = closeGallery;
function filterGallery() {   // typing narrows the 95 presets by name and group
  const shown = OpenList.filterCards($('gallery-body'), $('gallery-filter').value, '.pick', (c) => `${c.querySelector('b').textContent} ${c.closest('.picks').previousElementSibling.textContent}`, 'h3');
  let none = document.getElementById('gallery-none');   // made the first time it's needed
  if (!none) { none = document.createElement('p'); none.id = 'gallery-none'; none.className = 'muted'; none.textContent = 'No preset matches that.'; $('gallery-body').append(none); }
  none.hidden = shown > 0;
}
$('gallery-filter').oninput = filterGallery;
$('gallery-filter').onkeydown = (e) => { if (e.key === 'Escape' && e.target.value) { e.preventDefault(); e.target.value = ''; filterGallery(); } };
$('gallery').addEventListener('close', () => { cancelAnimationFrame(galleryTimer); galleryWatch?.disconnect(); });
$('gallery').addEventListener('click', (e) => { if (e.target === $('gallery')) closeGallery(); });   // a click on the backdrop
$('import').onclick = () => { closeMore(); if (DEMO) return toast('This is the demo. On your own Matrix, Import turns a GIF or picture into frames'); $('importfile').click(); };
// a picture chosen: ask how to bring it in (the defaults do what Import always did: replace the drawing, the whole picture, black left empty)
let importing = null;
const IM_HINTS = {
  replace: 'Everything here is replaced. Undo brings it back.',
  layer: 'A new layer above the selected one. A GIF\u2019s frames go along yours from this frame, keeping your times; if it\u2019s longer, frames are added at the end, and if it\u2019s shorter the layer is empty on the rest. A single picture goes on this frame.',
  frames: 'Added after this frame, on the selected layer, with their own times.',
};
function importHint() { $('imhint').textContent = IM_HINTS[$('imwhere').value]; $('imerr').textContent = ''; }
$('importfile').onchange = () => {
  const file = $('importfile').files[0]; $('importfile').value = '';
  if (!file) return;
  importing = file;
  const room = layers.length < MAX_LAYERS;
  $('imwhere').value = 'replace'; $('imfit').value = 'whole';
  $('imwhere').querySelector('[value=layer]').disabled = !room; $('imwhere').querySelector('[value=layer]').textContent = room ? 'A new layer on this frame' : `A new layer (${MAX_LAYERS} layers is the most)`;
  $('imblack').checked = !(/\.(jpe?g|bmp)$/i.test(file.name) || /^image\/(jpeg|bmp)/.test(file.type));   // a photo has real blacks and nothing see-through; a sprite's black is usually "nothing here"
  $('imfile').textContent = file.name; importHint(); openDialog($('importdlg'));
};
$('imwhere').onchange = importHint;
$('im-close').onclick = $('imcancel').onclick = () => $('importdlg').close();
$('imgo').onclick = async () => {
  const file = importing, w = W, h = H, where = $('imwhere').value, say = (t) => { $('imerr').textContent = t; };
  if (!file) return;
  const form = new FormData(); form.append('file', file); form.append('width', w); form.append('height', h);   // fitted to this canvas
  form.append('fit', $('imfit').value); form.append('black', $('imblack').checked ? 'empty' : 'keep');
  $('imgo').disabled = true; say('');
  try {
    const data = await api('POST', '/api/animator/import', form);
    if (w !== W || h !== H) return say('The canvas changed size while that was importing: import it again');
    const imported = data.frames.map((f) => ({ ...f, ms: clamp(f.ms, 20, 5000) }));
    const name = file.name.replace(/\.[^.]+$/, '').replace(/[^A-Za-z0-9_-]+/g, '_').slice(0, 40) || 'animation';
    if (where === 'replace') {
      snapshot(true); sel = null; savedAs = null; frames = oneLayer(imported); cur = 0; pi = 0; $('name').value = name;
      renderAll(); toast(`Imported ${data.note || `${frames.length} frame${frames.length === 1 ? '' : 's'}`}: Undo brings your old ones back`);
    } else {
      const plan = (where === 'layer' ? window.AnimatorImport.asLayer : window.AnimatorImport.asFrames)({ layers, frames, cur, al, imported, W, H, maxFrames: Project.maxFrames(W, H), maxLayers: MAX_LAYERS,
        name: name.replace(/_/g, ' ').slice(0, 24), newLayer: (n) => newLayer(nextId(), n), locked: isLocked });   // (a fresh layer id: see load())
      if (plan.error) return say(plan.error);   // nothing has been changed
      snapshot(); sel = null; layers = plan.layers; frames = plan.frames; cur = plan.cur; al = plan.al; relink(); pi = 0;
      renderAll();
      const n = imported.length, what = where === 'layer' ? `as a new layer${plan.added ? ` (and ${plan.added} new frame${plan.added === 1 ? '' : 's'} at the end)` : ''}` : `${n} frame${n === 1 ? '' : 's'} after frame ${plan.cur}`;
      toast(`Imported ${data.note ? `${data.note}, ` : ''}${where === 'layer' ? what : `as ${what}`}: Undo takes ${where === 'layer' || n === 1 ? 'it' : 'them'} away again`);
    }
    $('importdlg').close();
  } catch (e) { say(e.message); } finally { $('imgo').disabled = false; }
};
window.addEventListener('beforeunload', (e) => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });   // unsaved work
window.bongoUnsaved = () => dirty;   // ...and the dashboard asks before it takes this frame away (app.js)

// -- a draft: unsaved work kept in this browser (IndexedDB, js/animator-engine.js) ---------------------------
// written a second or two after the last change (never mid-stroke), and at once when the page is hidden (a phone
// may close it in the background); cleared once the drawing is saved. Offered when the Animator opens again.
const DRAFT = DEMO ? 'demo' : 'draft';   // the demo keeps its own, so it's never offered on the real page
// offering: until the draft (if any) is used or thrown away, nothing overwrites it. true while it's being offered; 'later'
// when the offer was put off (Escape or a click outside): the draft is kept and offered next time, and the first change that
// would replace it asks first (once); 'kept' when the answer was to keep it (this drawing then isn't kept in the browser)
let changedAt = 0, offering = true, haveDraft = false, offered = null;
function draftSoon() { changedAt = Date.now(); }
function keepDraft(now = false) {
  if (offering === 'later' && !now && dirty && changedAt && Date.now() - changedAt >= 1500 && !busy() && !document.querySelector('dialog[open]')) return replaceDraft();
  if (offering || busy()) return;
  if (!dirty) { changedAt = 0; if (haveDraft) { haveDraft = false; E.Draft.clear(DRAFT); } return; }   // saved
  if (!changedAt || (!now && Date.now() - changedAt < 1500)) return;
  changedAt = 0; E.settle(frames);
  E.Draft.write(DRAFT, { name: $('name').value, w: W, h: H, bg, loop: $('loop').value, savedAs, cur, al, layers: layers.map((l) => ({ ...l, locked: lockedIds.has(l.id) })), frames }).then((ok) => { haveDraft ||= ok; });
}
setInterval(keepDraft, 500);
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') keepDraft(true); });
window.addEventListener('pagehide', () => keepDraft(true));
async function offerDraft() {
  const d = await E.Draft.read(DRAFT);
  if (!d) { offering = false; return; }
  haveDraft = true;
  const when = new Date(d.at), day = when.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }), time = when.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  $('draft-text').textContent = `You have an unsaved drawing, "${d.name}", from ${day} ${time}${d.frames.length > 1 ? ` (${d.frames.length} frames)` : ''}.`;
  const pic = $('draft-pic'), back = T.parse(d.bg), px = T.compose(d.frames[d.cur].cels, d.layers, d.bg), img = new ImageData(d.w, d.h);
  px.forEach((c, i) => { const v = c ? T.parse(c) : back; img.data.set([v[0], v[1], v[2], 255], i * 4); });
  pic.width = d.w; pic.height = d.h; pic.style.aspectRatio = `${d.w} / ${d.h}`; pic.getContext('2d').putImageData(img, 0, 0);
  offered = { name: d.name, when: `${day} ${time}` };
  $('draft-use').onclick = () => { offering = false; $('draftdlg').close(); useDraft(d); };
  $('draft-drop').onclick = () => { offering = false; $('draftdlg').close(); haveDraft = false; E.Draft.clear(DRAFT); };
  openDialog($('draftdlg')); $('draft-use').focus();
}
// Escape or a click outside: decide later. The draft stays as it is and is offered again next time
$('draftdlg').addEventListener('close', () => { if (offering === true) offering = 'later'; });
$('draftdlg').addEventListener('click', (e) => { if (e.target === $('draftdlg')) $('draftdlg').close(); });   // the backdrop
async function replaceDraft() {   // the first change after "later" that would be kept in its place: ask, never replace it unasked
  offering = 'asking';
  const yes = await ask({ title: 'Keep this drawing in the browser instead?', ok: 'Keep this one instead', cancel: `Keep "${offered.name}"`, danger: true,
    text: `This browser keeps one unsaved drawing to carry on with, and it's "${offered.name}" from ${offered.when}. Keep this one instead and that one is thrown away. Keep "${offered.name}" and it's offered again next time, but what you draw now isn't kept in the browser until you save it.` });
  if (yes) { offering = false; changedAt = changedAt || Date.now(); keepDraft(true); } else offering = 'kept';
}
function useDraft(d) {   // the drawing comes back as it was, still unsaved, with an empty undo history (council R2)
  snapshot(true); sel = null;
  if (d.w !== W || d.h !== H) setSize(d.w, d.h);
  layers = d.layers.map(({ locked, ...l }) => { const id = nextId(); if (locked) lockedIds.add(id); return { ...newLayer(0, 'Drawing'), ...l, id }; });   // locks are kept by id
  frames = d.frames; cur = d.cur; al = d.al; pi = 0;
  if (/^#[0-9a-f]{6}$/.test(d.bg)) { bg = d.bg; $('bg').value = bg; }
  if (['loop', 'pingpong', 'once'].includes(d.loop)) $('loop').value = d.loop;
  $('name').value = d.name; savedAs = d.savedAs || null; dirty = true;
  renderAll(); undo = []; redo = []; redoWas = [];   // the undo history starts here (council R2)
  toast(`Carried on with "${d.name}". It isn't saved yet`);
}

// -- start --------------------------------------------------------------------------------
playing = Layout.init(); Fingers.init();   // plays at the start when the Preview window is on (js/animator-layout.js)
setSize(W, H); dimPicker(15); setTool('pen'); frames = starter(); renderAll(); if (playing) tick();
offerDraft();
if (DEMO) { $('demo-note').hidden = false; $('tfont').title = 'On your own Matrix, its fonts are listed here too'; }
// this Matrix's panel, safe area, sleep brightness and fonts (the demo keeps the usual ones)
if (!DEMO) (async () => {
  try {
    const s = await api('GET', '/api/state');
    PANEL.w = s.panel.width; PANEL.h = s.panel.height; SAFE = s.safe_area; sizePicker(); renderBoard();
  } catch { /* not paired, or no Matrix: the usual 64 x 32 with a pixel's margin */ }
  try { dimPicker(clamp(Math.round((await api('GET', '/api/config')).sleep?.brightness ?? 15), 1, 100)); } catch { /* the usual 15% */ }
  try { (await api('GET', '/api/animator/fonts')).fonts.forEach((name) => $('tfont').append(new Option(name, name))); } catch { /* Tiny alone */ }
})();
