// The Animator's editor (animator.html). The drawing is a canvas of W x H pixels (the panel, or a small one
// for a sprite), a list of layers that every frame has, and frames that keep their own pixels per layer.
// The pixel work is in animator-tools.js, Spread across frames in animator-motion.js, and the project file
// (what Save keeps and Open reads) in animator-project.js.
const T = window.PixelTools, Motion = window.AnimatorMotion, Project = window.AnimatorProject;
const PRESET_W = 64, PRESET_H = 32;   // the presets are always drawn for the 64 x 32 panel
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
function roomFor(n) { const most = Project.maxFrames(W, H); if (frames.length + n <= most) return true; toast(`An animation this size can have up to ${most} frames`); return false; }

// -- the canvas size ------------------------------------------------------------------------
function setSize(w, h) {
  W = w; H = h; T.size(w, h);
  S = Math.max(4, Math.floor(1280 / Math.max(W, H)));
  board.width = W * S; board.height = H * S; preview.width = W; preview.height = H;
  const led = preview.parentElement;
  led.style.aspectRatio = `${W} / ${H}`; led.style.width = `${Math.min(100, 50 * W / H)}%`;
  led.style.setProperty('--w', W); led.style.setProperty('--h', H);
  board.setAttribute('aria-label', `Drawing canvas, ${W} by ${H} pixels`);
  fitBoard(); sizePicker();
}
// the board fills the width (or, for a squarer canvas, most of the window's height), times the zoom
function fitBoard() { board.style.width = `calc(min(100%, ${(62 * W / H).toFixed(2)}vh) * ${zoom})`; board.style.maxWidth = 'none'; board.style.minWidth = `${Math.min(W * 6, 240)}px`; }
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
$('csize').onchange = () => {
  let v = $('csize').value;
  if (v === 'custom') {
    const typed = window.prompt(`Width x height in pixels, each from 1 to ${Project.MAX_SIDE}. For example: 28x28`, `${W}x${H}`);
    if (typed === null) { sizePicker(); return; }
    const nums = typed.trim().split(/[^0-9]+/).filter(Boolean);
    if (nums.length !== 2) { toast('Type two numbers, like 28x28'); sizePicker(); return; }
    v = nums.join('x');
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
function flat(f) { f.px = T.compose(f.cels, layers, bg); }
function flatAll() { frames.forEach(flat); }
function oneLayer(list) {   // frames from a preset or an imported GIF: one layer each
  layers = [newLayer(nextId(), 'Drawing')]; al = 0;   // a new id: a frame copied before must not take it for its own layer
  list.forEach((f) => { f.cels = [f.px.slice()]; });
  return list;
}
// the selected layer of the current frame, shaped like a frame so every tool can draw on it
const target = { get px() { return frames[cur].cels[al]; }, set px(v) { if (layers[al].shared) frames.forEach((f) => { f.cels[al] = v; }); else frames[cur].cels[al] = v; } };
const PRESETS = window.ANIMATOR_PRESETS || [];
const preset = (id) => PRESETS.find((p) => p.id === id);
// the presets draw with the tools at the panel's 64 x 32, whatever size the canvas is just now
function atPanel(draw) { const w = T.W, h = T.H; T.size(PRESET_W, PRESET_H); try { return draw(); } finally { T.size(w, h); } }
function copyFrames(list) { return list.map((f) => ({ px: f.px.slice(), ms: f.ms, ...(f.cels ? { cels: f.cels.map((c) => c.slice()) } : {}) })); }
function starter() { return oneLayer(preset('ball') ? copyFrames(atPanel(() => preset('ball').make())) : [{ px: empty(), ms: 90 }]); }

// -- undo ---------------------------------------------------------------------------------
// each step is kept as text: the size, the layers and their pixels (the picture is rebuilt from them); an
// empty cel is 0 and a shared layer's cel is written once (1 on the other frames)
const state = (whole = false) => JSON.stringify({ ...(whole ? { whole: true, name: $('name').value, loop: $('loop').value, savedAs } : {}), w: W, h: H, bg, cur, layers, al,
  frames: frames.map((f, i) => ({ ms: f.ms, cels: f.cels.map((c, k) => (layers[k].shared && i ? 1 : c.some(Boolean) ? c : 0)) })) });
const UNDO_CHARS = 120e6;   // ~240 MB of history at most, however big the animation (or 80 steps)
let savedAs = null;   // the drawing this one was opened from or last saved as: saving over any other asks first
let redoWas = [];
function unsnapshot() { undo.pop(); redo = redoWas; }   // the step just taken turned out to change nothing
function snapshot(whole = false) {   // whole: this step replaces the drawing (a preset, Open, Import), so its name goes with it
  if (sel) sel.piece = sel.base = null;   // a piece being moved is put down where it is before anything else changes
  undo.push(state(whole)); redoWas = redo; redo = []; dirty = true;
  let total = undo.reduce((n, u) => n + u.length, 0);
  while (undo.length > 1 && (undo.length > 80 || total > UNDO_CHARS)) total -= undo.shift().length;
}
function restore(s) {
  const o = JSON.parse(s);
  if (o.w !== W || o.h !== H) setSize(o.w, o.h);
  frames = o.frames.map((f) => ({ ms: f.ms, px: empty(), cels: f.cels.map((c) => (c === 0 || c === 1 ? empty() : c)) }));
  layers = o.layers; al = Math.min(o.al, layers.length - 1); cur = Math.min(o.cur, frames.length - 1);
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
  if (ghost) {
    ctx.globalAlpha = 0.22;
    ghost.px.forEach((c, i) => { if (c) { ctx.fillStyle = c; ctx.fillRect((i % W) * scale, Math.floor(i / W) * scale, scale, scale); } });
    ctx.globalAlpha = 1;
  }
  f.px.forEach((c, i) => { if (c) { ctx.fillStyle = c; ctx.fillRect((i % W) * scale, Math.floor(i / W) * scale, scale, scale); } });
}
// pixels for putImageData: much quicker than drawing a square each. `alpha`: empty pixels stay see-through
function toImage(pixels, w, h, { percent = 100, alpha = false } = {}) {
  const img = new ImageData(w, h), back = T.parse(T.dimmed(bg, percent));
  pixels.forEach((c, i) => { const o = i * 4, v = c ? T.parse(T.dimmed(c, percent)) : back;
    img.data[o] = v[0]; img.data[o + 1] = v[1]; img.data[o + 2] = v[2]; img.data[o + 3] = c || !alpha ? 255 : 0; });
  return img;
}

let hover = null, dragStart = null, dragEnd = null;
function renderBoard() {
  flat(frames[cur]);   // the selected layer may just have changed
  const f = frames[cur];
  drawFrame(bx, f, S, onion && cur > 0 ? frames[cur - 1] : (onion && frames.length > 1 ? frames[frames.length - 1] : null));
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
  if (tool === 'text' && hover && (placing || !coarse)) {   // where the text would land
    bx.globalAlpha = 0.6; bx.fillStyle = colour;
    T.text($('ttext').value, hover[0], hover[1], font, (i) => bx.fillRect((i % W) * S, Math.floor(i / W) * S, S, S));
    bx.globalAlpha = 1;
  }
  const box = marquee ? rectOf(marquee.from, marquee.to) : sel;
  if (box) {
    const at = [box.x * S + line / 2, box.y * S + line / 2, box.w * S - line, box.h * S - line];   // drawn inside the area, so it shows at the canvas edge too
    bx.lineWidth = line; bx.strokeStyle = '#000'; bx.strokeRect(...at);
    bx.setLineDash([line * 3, line * 3]); bx.strokeStyle = '#fff'; bx.strokeRect(...at);
    bx.setLineDash([]); bx.lineDashOffset = 0; bx.lineWidth = 1;
  }
  if (hover && tool !== 'text') { bx.strokeStyle = '#fff'; bx.strokeRect(hover[0] * S + .5, hover[1] * S + .5, S - 1, S - 1); }
}
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
const busy = () => !!(dragStart || drawing || moving || marquee || selDrag || placing || panning);
const coarse = window.matchMedia('(pointer: coarse)').matches;   // a finger: no hover, so text is placed where you let go
let placing = null, panning = null;
board.addEventListener('pointerdown', (e) => {
  if (tool === 'pan') { if (e.pointerType === 'mouse') { const w = $('board-wrap'); panning = { x: e.clientX, y: e.clientY, left: w.scrollLeft, top: w.scrollTop }; try { board.setPointerCapture(e.pointerId); } catch { /* the pointer's already gone */ } } return; }   // a finger scrolls it itself
  try { board.setPointerCapture(e.pointerId); } catch { /* the pointer's already gone */ }
  const [x, y] = cell(e), f = target;
  if (tool === 'pick') { const c = frames[cur].px[y * W + x]; if (c) (e.button === 2 ? setColour2 : setColour)(c); setTool('pen'); return; }
  if (tool === 'select' && !(sel && inSel(x, y))) { sel = null; marquee = { from: [x, y], to: [x, y], moved: false }; renderBoard(); return; }   // a new area: nothing drawn yet
  if (!layers[al].on) { toast(`"${layers[al].name}" is hidden: show it to draw on it`); return; }
  if (tool === 'select') { selDrag = { from: [x, y], at: [sel.x, sel.y] }; return; }
  if (tool === 'text') { if ($('ttext').value) { placing = { erase: e.button === 2 }; hover = [x, y]; renderBoard(); } else toast('Type the text first, in the box above the canvas'); return; }
  snapshot();
  if (tool === 'fill') { fill(f, x, y, e.button === 2 ? null : colour); renderAll(); return; }
  if (DRAG.has(tool)) { dragStart = [x, y]; dragBase = f.px.slice(); dragErase = e.button === 2; dragTo(x, y); renderBoard(); return; }
  if (tool === 'shift') { moving = { from: [x, y], px: f.px.slice() }; dragBase = moving.px; return; }
  drawing = true; last = [x, y]; stroke = tool === 'glow' ? { base: new Map(), k: new Map() } : new Set(); under = belowLayer();
  if (BRUSHED.has(tool)) brushAt(f, x, y); else setPx(f, x, y, tool === 'erase' || e.button === 2 ? null : colour);
  renderBoard();
});
board.addEventListener('pointermove', (e) => {
  if (panning) { const w = $('board-wrap'); w.scrollLeft = panning.left - (e.clientX - panning.x); w.scrollTop = panning.top - (e.clientY - panning.y); return; }
  const [x, y] = cell(e); hover = [x, y]; $('pos').textContent = `x ${x}, y ${y}`;
  if (marquee) { marquee.to = [clamp(x, 0, W - 1), clamp(y, 0, H - 1)]; marquee.moved ||= x !== marquee.from[0] || y !== marquee.from[1]; }
  else if (selDrag) { if (x !== selDrag.from[0] || y !== selDrag.from[1] || sel.piece) { lift(); moveSel(selDrag.at[0] + x - selDrag.from[0], selDrag.at[1] + y - selDrag.from[1]); } }
  else if (dragStart) dragTo(x, y);
  else if (moving) { target.px = T.shifted(moving.px, x - moving.from[0], y - moving.from[1]); }
  else if (drawing) {
    const cells = lineCells(last[0], last[1], x, y).slice(1);
    if (BRUSHED.has(tool)) cells.forEach(([a, b]) => brushAt(target, a, b));
    else cells.forEach(([a, b]) => setPx(target, a, b, tool === 'erase' || e.buttons === 2 ? null : colour));
    last = [x, y];
  }
  renderBoard();
});
function strokeEnd(e) {   // a finished stroke, or one the browser cancelled (an edge swipe, an alert)
  panning = null;
  if (placing) {   // the text goes where the pointer was let go
    if (e && e.type === 'pointerup' && hover) { const was = target.px, p = was.slice(), erase = placing.erase; T.text($('ttext').value, hover[0], hover[1], font, (i) => { p[i] = erase ? null : colour; }); if (p.some((c, i) => c !== was[i])) { snapshot(); target.px = p; } }
    placing = null; if (coarse) hover = null;
  }
  if (marquee) { sel = marquee.moved ? { ...rectOf(marquee.from, marquee.to), piece: null, base: null, cur, al } : null; marquee = null; }
  if (dragBase && target.px.every((c, i) => c === dragBase[i])) unsnapshot();   // nothing changed: no undo step
  dragStart = dragEnd = dragBase = stroke = under = selDrag = null;
  drawing = false; moving = null; renderAll();
}
board.addEventListener('pointerup', strokeEnd);
board.addEventListener('pointercancel', strokeEnd);
board.addEventListener('pointerleave', () => { hover = null; $('pos').textContent = 'x –, y –'; renderBoard(); });
board.addEventListener('contextmenu', (e) => e.preventDefault());

// -- the selection --------------------------------------------------------------------------
// sel: the area ({ x, y, w, h }) on one layer of one frame. Until it's moved it is only an outline. Moving,
// flipping or turning it lifts its pixels off the layer: `piece` is what was lifted and `base` the layer
// without it, so the layer is always base + piece wherever the piece is now. Anything else that changes the
// drawing puts the piece down first (snapshot does that).
let sel = null, marquee = null, selDrag = null, selClip = null, lastCopied = null;   // lastCopied: 'area' or 'frame', whichever Ctrl+V should paste
const rectOf = (a, b) => ({ x: Math.min(a[0], b[0]), y: Math.min(a[1], b[1]), w: Math.abs(a[0] - b[0]) + 1, h: Math.abs(a[1] - b[1]) + 1 });
const inSel = (x, y) => x >= sel.x && y >= sel.y && x < sel.x + sel.w && y < sel.y + sel.h;
function lift() {
  if (sel.piece) return;
  snapshot();
  sel.piece = T.lifted(target.px, sel); sel.base = T.cleared(target.px, sel);
}
function moveSel(x, y) { sel.x = x; sel.y = y; delete sel.mid; target.px = T.placed(sel.base, sel.piece, x, y); }
function needSel() { if (sel && layers[al].on) return true; toast(sel ? `"${layers[al].name}" is hidden: show it first` : 'Select an area first: drag across it with the select tool'); return false; }
function changeSel(change) {   // flip or turn the piece about its middle
  if (!needSel()) return;
  lift();
  const [cx, cy] = sel.mid || [sel.x + sel.w / 2, sel.y + sel.h / 2];   // its true middle, kept between turns so four of them come home
  sel.piece = change(sel.piece); sel.w = sel.piece.w; sel.h = sel.piece.h;
  moveSel(Math.round(cx - sel.w / 2), Math.round(cy - sel.h / 2)); sel.mid = [cx, cy]; renderAll();
}
function copySel() { if (!needSel()) return false; selClip = { ...(sel.piece ? { ...sel.piece, px: sel.piece.px.slice() } : T.lifted(target.px, sel)), x: sel.x, y: sel.y }; lastCopied = 'area'; toast('Area copied'); return true; }
function deleteSel() {
  if (!needSel()) return;
  if (sel.piece) target.px = sel.base; else { snapshot(); target.px = T.cleared(target.px, sel); }
  sel = null; renderAll();
}
function pasteSel() {
  if (!selClip) return toast('Copy or cut an area first');
  if (!layers[al].on) return toast(`"${layers[al].name}" is hidden: show it to paste onto it`);
  const at = sel ? [sel.x, sel.y] : [clamp(selClip.x, 0, W - 1), clamp(selClip.y, 0, H - 1)];   // where it was copied from
  setTool('select'); snapshot();
  sel = { x: at[0], y: at[1], w: selClip.w, h: selClip.h, piece: { px: selClip.px.slice(), w: selClip.w, h: selClip.h }, base: target.px.slice(), cur, al };
  moveSel(sel.x, sel.y); renderAll(); toast('Pasted: drag it into place');
}
function selectAll() { setTool('select'); sel = { x: 0, y: 0, w: W, h: H, piece: null, base: null, cur, al }; renderBoard(); }
$('selall').onclick = selectAll;
$('selcopy').onclick = copySel;
$('selcut').onclick = () => { if (copySel()) { deleteSel(); toast('Area cut'); } };
$('selpaste').onclick = pasteSel;
$('selfliph').onclick = () => changeSel((p) => T.mirrored(p, true));
$('selflipv').onclick = () => changeSel((p) => T.mirrored(p, false));
$('selturn').onclick = () => changeSel(T.turned);
$('seldel').onclick = deleteSel;

// -- colour -----------------------------------------------------------------------------
function setColour(c) { colour = c; $('colour').value = c; $('hex').textContent = c.toUpperCase(); paintSwatches(); }
function setColour2(c) { colour2 = c; $('colour2').value = c; }
$('colour2').addEventListener('input', (e) => setColour2(e.target.value));
$('swap').onclick = () => { const a = colour; setColour(colour2); setColour2(a); };
$('colour').addEventListener('input', (e) => setColour(e.target.value));
function swatch(c, holder) {
  const b = document.createElement('button'); b.style.background = c; b.title = c; b.setAttribute('aria-label', `Colour ${c}`);
  if (c.toLowerCase() === colour.toLowerCase()) b.className = 'sel';
  b.onclick = () => { setColour(c); if (tool === 'erase' || tool === 'pick') setTool('pen'); };
  b.oncontextmenu = (e) => { e.preventDefault(); setColour2(c); };   // right-click: the second colour
  holder.append(b);
}
function paintSwatches() {
  $('swatches').replaceChildren(); PALETTE.forEach((c) => swatch(c, $('swatches')));
  const used = [...new Set(frames.flatMap((f) => f.px).filter(Boolean))].slice(0, 16);
  $('used').replaceChildren(); used.forEach((c) => swatch(c, $('used')));
}

// -- tools ------------------------------------------------------------------------------
const NAMES = { pen: 'Pencil', erase: 'Eraser', fill: 'Fill', line: 'Line', rect: 'Rectangle', ellipse: 'Circle and oval',
  gradient: 'Gradient', blur: 'Blur', shade: 'Lighten and darken', glow: 'Glow', pick: 'Pick colour', text: 'Text', select: 'Select', shift: 'Shift drawing', pan: 'Move around' };
function setTool(t) {
  if (busy()) return;   // not mid-stroke: the shape you're drawing stays the shape you started
  if (t !== 'select') sel = null;
  tool = t; document.querySelectorAll('[data-tool]').forEach((b) => { b.classList.toggle('on', b.dataset.tool === t); b.setAttribute('aria-pressed', String(b.dataset.tool === t)); });
  board.style.touchAction = t === 'pan' ? 'auto' : 'none'; board.style.cursor = t === 'pan' ? 'grab' : 'crosshair'; $('toolname').textContent = `${NAMES[t]} · ${layers[al].name}`;
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
toggle('onion', () => onion, (v) => onion = v);
toggle('grid', () => grid, (v) => grid = v);
toggle('safe', () => showSafe, (v) => showSafe = v);

// -- text: Tiny is built in; the Matrix's own fonts are fetched the first time each is picked -------
let font = T.TINY;
const fonts = new Map([['', T.TINY]]);
$('tfont').replaceChildren(new Option('Tiny (3 × 5)', ''));
$('ttext').addEventListener('input', renderBoard);
$('tfont').onchange = async () => {
  const name = $('tfont').value;
  try { if (!fonts.has(name)) fonts.set(name, await api('GET', `/api/animator/fonts/${encodeURIComponent(name)}`)); }
  catch (e) { toast(e.message); if ($('tfont').value === name) $('tfont').value = ''; }
  font = fonts.get($('tfont').value) || T.TINY;   // whatever is picked now: another may have been chosen while this one was on its way
  renderBoard();
};

// -- frames -----------------------------------------------------------------------------
function renderFrames() {
  const box = $('frames'); box.replaceChildren();
  frames.forEach((f, i) => {
    const el = document.createElement('div'); el.className = 'frame' + (i === cur ? ' sel' : '');
    const c = document.createElement('canvas'); c.width = W; c.height = H; c.getContext('2d').putImageData(toImage(f.px, W, H), 0, 0);
    c.style.width = `${Math.max(4, Math.min(100, 50 * W / H))}%`; c.style.maxHeight = '106px'; c.style.objectFit = 'contain';
    const meta = document.createElement('div'); meta.className = 'meta';
    const n = document.createElement('span'); n.textContent = String(i + 1).padStart(2, '0');
    const ms = document.createElement('input'); ms.className = 'input'; ms.type = 'number'; ms.min = 20; ms.max = 5000; ms.step = 10; ms.value = f.ms; ms.title = 'How long this frame shows (ms)';
    ms.setAttribute('aria-label', `Frame ${i + 1}: how long it shows, in ms`);
    ms.onclick = (e) => e.stopPropagation(); ms.onchange = () => { snapshot(); f.ms = clamp(Number(ms.value) || 90, 20, 5000); ms.value = f.ms; };
    const unit = document.createElement('span'); unit.textContent = 'ms';
    const r = document.createElement('span'); r.append(ms, ' ', unit);
    meta.append(n, r); el.append(c, meta);
    el.onclick = () => { cur = i; renderAll(); };
    box.append(el);
  });
  const add = document.createElement('button'); add.className = 'add'; add.textContent = '+'; add.title = 'New blank frame';
  add.onclick = () => { if (!roomFor(1)) return; snapshot(); frames.splice(cur + 1, 0, blank()); cur++; renderAll(); };
  box.append(add);
  // keep the selected frame visible by scrolling the strip sideways only, never the page
  const picked = box.children[cur];
  if (picked) {
    const l = picked.offsetLeft - box.offsetLeft, r = l + picked.offsetWidth;
    if (l < box.scrollLeft) box.scrollLeft = l;
    else if (r > box.scrollLeft + box.clientWidth) box.scrollLeft = r - box.clientWidth;
  }
  $('size').textContent = `${W} × ${H} · ${frames.length} frame${frames.length === 1 ? '' : 's'}`;
}
// a frame copied whole: its own layers are copies, a shared layer is the shared one still
const cloneFrame = (f) => ({ ms: f.ms, px: f.px.slice(), cels: f.cels.map((c, k) => (layers[k].shared ? c : c.slice())) });
$('dup').onclick = () => { if (!roomFor(1)) return; snapshot(); frames.splice(cur + 1, 0, cloneFrame(frames[cur])); cur++; renderAll(); };
$('del').onclick = () => { if (frames.length < 2) return toast('An animation needs at least one frame'); snapshot(); frames.splice(cur, 1); cur = Math.max(0, cur - 1); renderAll(); };
$('copy').onclick = () => { lastCopied = 'frame'; clip = { ...JSON.parse(JSON.stringify(frames[cur])), ids: layers.map((l) => l.id), w: W, h: H }; toast(`Frame ${cur + 1} copied`); };
$('paste').onclick = () => {
  if (!clip) return toast(selClip ? 'Pick the Select tool to paste the area you copied, or copy a frame first' : 'Copy a frame first'); if (clip.w !== W || clip.h !== H) return toast('That frame was copied from a canvas of another size'); if (!roomFor(1)) return; snapshot();
  const f = JSON.parse(JSON.stringify(clip)), ids = f.ids; delete f.ids; delete f.w; delete f.h;
  if (ids.every((id) => layers.some((l) => l.id === id))) f.cels = layers.map((l) => (ids.includes(l.id) ? f.cels[ids.indexOf(l.id)] : empty()));   // each layer back on its own layer, wherever it is now
  else {   // a layer it came from is gone: paste it flat onto this layer (or, if this one is the same on every frame, the nearest that isn't)
    const own = !layers[al].shared ? al : layers.findIndex((l) => !l.shared);
    if (own < 0) { unsnapshot(); return toast('Every layer here is the same on every frame: untick that on one to paste a frame'); }
    f.cels = layers.map((_, k) => (k === own ? f.px.slice() : empty()));
  }
  frames.splice(cur + 1, 0, f); cur++; relink(); renderAll();
};
$('setall').onclick = () => { const ms = clamp(Number($('allms').value) || 90, 20, 5000); snapshot(); frames.forEach((f) => { f.ms = ms; }); renderFrames(); toast(`Every frame now shows for ${ms} ms`); };
// clearing or flipping one frame leaves a layer that's the same on every frame alone
const ownCels = (change) => { snapshot(); frames[cur].cels = frames[cur].cels.map((c, k) => (layers[k].shared ? c : change(c))); renderAll(); };
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
  spreadMode = mode;
  $('sphint').textContent = `Takes ${sel ? 'the selected area' : 'everything'} on "${layers[al].name}" and changes it a little more on each frame. It replaces what's on this layer in the frames it fills (Undo brings that back).`;
}
let spreadMode = 'move', spreadN = 8;
$('spmode').onchange = spreadFields;
$('spread').onclick = () => {
  if (layers[al].shared) return toast(`"${layers[al].name}" is the same on every frame: untick that to spread it across frames`);
  if (!layers[al].on) return toast(`"${layers[al].name}" is hidden: show it first`);
  if (!target.px.some(Boolean)) return toast(`There's nothing on "${layers[al].name}" in this frame to spread`);
  spreadFields(); openDialog($('spreaddlg'));
};
$('spread-close').onclick = () => $('spreaddlg').close();
$('spgo').onclick = () => {
  const num = (id, lo, hi, or) => clamp(Number.isFinite(Number($(id).value)) && $(id).value !== '' ? Number($(id).value) : or, lo, hi);
  const mode = $('spmode').value, n = Math.round(num('spn', 2, 120, 8)), area = sel ? { x: sel.x, y: sel.y, w: sel.w, h: sel.h } : null;
  if (!roomFor(Math.max(0, cur + n - frames.length))) return;
  if (mode === 'cycle' && Motion.colours(sel ? T.lifted(target.px, sel).px : target.px).length < 2) return toast('Cycling needs at least two colours');
  snapshot();   // puts down a piece being moved, so what's spread is what you see
  const src = target.px.slice(), easing = $('spease').value;
  const list = mode === 'move' ? Motion.move(src, { dx: Math.round(num('spdx', -128, 128, 0)), dy: Math.round(num('spdy', -128, 128, 0)), n, easing, area })
    : mode === 'fade' ? Motion.fade(src, { from: num('spfrom', 0, 200, 100), to: num('spto', 0, 200, 0), n, easing, area })
    : Motion.cycle(src, { n, order: $('sporder').value, step: $('spback').checked ? -1 : 1, area });
  while (frames.length < cur + n) frames.push(cloneFrame(frames[frames.length - 1]));   // new frames carry on from the last one
  list.forEach((p, k) => { frames[cur + k].cels[al] = p; });
  sel = null; $('spreaddlg').close(); renderAll();
  toast(`Filled frames ${cur + 1} to ${cur + n}: Undo takes it back`);
};

// -- layers -----------------------------------------------------------------------------
function rename() {
  const l = layers[al], n = (window.prompt('Layer name', l.name) || '').trim().slice(0, 24);
  if (n && n !== l.name) { snapshot(); l.name = n; }
  renderLayers(true);
}
function renderLayers(focus = false) {
  const box = $('layers'); box.replaceChildren();
  layers.map((l, k) => [l, k]).reverse().forEach(([l, k]) => {   // top layer first, as it stacks
    const row = document.createElement('div');
    const notes = [l.shared ? 'all' : '', l.mode === 'light' ? 'light' : '', l.opacity < 1 ? `${Math.round(l.opacity * 100)}%` : ''].filter(Boolean).join(' · ');
    const said = [l.shared ? 'same on every frame' : '', l.mode === 'light' ? 'adds light' : '', l.opacity < 1 ? `${Math.round(l.opacity * 100)}% opacity` : ''].filter(Boolean).join(', ');
    row.className = 'layer' + (k === al ? ' sel' : '') + (l.on ? '' : ' hidden');
    row.setAttribute('aria-current', String(k === al)); row.setAttribute('aria-label', `${l.name}${said ? `, ${said}` : ''}${l.on ? '' : ', hidden'}${k === al ? ', selected' : ''}`); row.tabIndex = 0;
    const eye = document.createElement('button'); eye.className = 'eye'; eye.type = 'button';
    eye.innerHTML = l.on ? EYE_OPEN : EYE_SHUT; eye.title = l.on ? 'Hide this layer' : 'Show this layer';
    eye.setAttribute('aria-label', `${l.on ? 'Hide' : 'Show'} ${l.name}`);
    eye.onclick = (e) => { e.stopPropagation(); snapshot(); l.on = !l.on; renderAll(); $('layers').querySelectorAll('.eye')[layers.length - 1 - k]?.focus(); };
    const c = document.createElement('canvas'); c.width = W; c.height = H; c.style.width = `${Math.round(Math.min(48, 24 * W / H))}px`; c.style.height = '24px';
    drawFrame(c.getContext('2d'), { px: frames[cur].cels[k] }, 1);
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
  if (!sliding) $('lopacity').value = Math.round(layers[al].opacity * 100);
  $('lmode').value = layers[al].mode; $('lshared').checked = layers[al].shared;
}
const MAX_LAYERS = Project.MAX_LAYERS;
const EYE_OPEN = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M2 12s3.6-6 10-6 10 6 10 6-3.6 6-10 6S2 12 2 12z" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="12" cy="12" r="3" fill="currentColor"/></svg>';
const EYE_SHUT = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M2 12s3.6-6 10-6 10 6 10 6-3.6 6-10 6S2 12 2 12z" fill="none" stroke="currentColor" stroke-width="2" opacity=".5"/><path d="M4 20 20 4" stroke="currentColor" stroke-width="2"/></svg>';
$('laddu').onclick = () => {
  if (layers.length >= MAX_LAYERS) return toast(`Up to ${MAX_LAYERS} layers`);
  snapshot();
  let n = layers.length + 1; while (layers.some((l) => l.name === `Layer ${n}`)) n++;
  layers.splice(al + 1, 0, newLayer(nextId(), `Layer ${n}`));
  frames.forEach((f) => f.cels.splice(al + 1, 0, empty())); al++; renderAll();
};
function moveLayer(d) {
  const j = al + d; if (j < 0 || j >= layers.length) return; snapshot();
  [layers[al], layers[j]] = [layers[j], layers[al]];
  frames.forEach((f) => { [f.cels[al], f.cels[j]] = [f.cels[j], f.cels[al]]; }); al = j; renderAll();
}
$('lrename').onclick = rename;
$('lup').onclick = () => moveLayer(1); $('ldown').onclick = () => moveLayer(-1);
$('lmerge').onclick = () => {   // this layer laid over the one below, with its own opacity and light, in every frame (the one below keeps its settings)
  if (al === 0) return;
  if (!layers[al - 1].on) return toast(`Show "${layers[al - 1].name}" first to merge into it`);
  if (!layers[al].on) toast(`"${layers[al].name}" was hidden, so nothing of it was added (Undo brings it back)`);
  snapshot();
  const pair = [{ ...layers[al - 1], opacity: 1, mode: 'normal' }, layers[al]], shared = layers[al - 1].shared && layers[al].shared;
  const merged = new Map();   // two layers that are both the same on every frame merge into one that still is
  frames.forEach((f) => { const key = shared ? 0 : f; if (!merged.has(key)) merged.set(key, T.compose([f.cels[al - 1], f.cels[al]], pair, bg)); f.cels.splice(al - 1, 2, merged.get(key)); });
  layers[al - 1].shared = shared; layers.splice(al, 1); al--; renderAll();
};
$('ldel').onclick = () => {
  if (layers.length < 2) return; snapshot();
  const name = layers[al].name;
  frames.forEach((f) => f.cels.splice(al, 1)); layers.splice(al, 1); al = Math.max(0, al - 1); renderAll();
  toast(`Deleted "${name}" from every frame: Undo brings it back`);
};
let sliding = false;   // one undo step for a whole drag of the opacity slider
$('lopacity').addEventListener('input', () => { if (!sliding) { snapshot(); sliding = true; } layers[al].opacity = clamp(Number($('lopacity').value), 0, 100) / 100; renderBoard(); renderLayers(); });
$('lopacity').addEventListener('change', () => { sliding = false; renderAll(); });
$('lmode').onchange = () => { snapshot(); layers[al].mode = $('lmode').value; renderAll(); };
$('lshared').onchange = () => {
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
const isWhole = (s) => s.startsWith('{"whole":true');
$('undo').onclick = () => { if (busy()) return; if (undo.length) { const s = undo.pop(); redo.push(state(isWhole(s))); restore(s); } };
$('redo').onclick = () => { if (busy()) return; if (redo.length) { const s = redo.pop(); undo.push(state(isWhole(s))); restore(s); } };

// -- preview playback -------------------------------------------------------------------
let playing = true, pi = 0, dir = 1, timer = null;
const show = (f) => px.putImageData(toImage(f.px, W, H, { percent: dim }), 0, 0);
function tick() {
  if (!frames.length) return;
  const mode = $('loop').value, n = frames.length;
  let wait = 0;
  if (mode === 'pingpong' && n > 2) { if (pi + dir < 0 || pi + dir >= n) dir = -dir; pi += dir; }
  else if (mode === 'once' && pi + 1 >= n) { pi = 0; wait = 1200; }   // the preview pauses, then shows it again
  else pi = (pi + 1) % n;
  pi = clamp(pi, 0, n - 1); show(frames[pi]);
  const speed = clamp(Number($('speed').value) || 100, 10, 400) / 100;
  timer = setTimeout(tick, frames[pi].ms / speed + wait);
}
$('play').onclick = () => { playing = !playing; $('play').textContent = playing ? '❚❚ Pause' : '▶ Play'; clearTimeout(timer); if (playing) tick(); else show(frames[cur]); };
function dimPicker(sleep) {   // how it looks dimmed: the sleep screen's own brightness is in the list
  const list = [...new Set([100, 50, 25, sleep, 5])].filter((v) => v >= 1 && v <= 100).sort((a, b) => b - a);
  $('dim').replaceChildren(...list.map((v) => new Option(v === 100 ? 'Off' : v === sleep ? `${v}% (Sleep)` : `${v}%`, v)));
  $('dim').value = String(dim);
}
$('dim').onchange = () => { dim = Number($('dim').value) || 100; if (!playing) show(frames[cur]); };

// -- talking to the Matrix ----------------------------------------------------------------
// ?demo (the demo dashboard, no Matrix behind it): everything works except what needs one
const DEMO = new URLSearchParams(location.search).has('demo');
function toast(t) { const el = $('toast'); el.textContent = t; el.style.display = 'block'; clearTimeout(toast.t); toast.t = setTimeout(() => el.style.display = 'none', 2600); }
async function api(method, path, body) {
  const init = { method, credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } };
  if (body instanceof FormData) { init.headers['X-Bongo-Upload'] = '1'; init.body = body; }
  else if (method !== 'GET') { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body ?? {}); }
  let res;
  try { res = await fetch(path, init); } catch { throw new Error("Can't reach the Matrix. Is it switched on and on this network?"); }
  let data = {};
  try { data = await res.json(); } catch { /* no body */ }
  if (!res.ok) throw new Error(data.error || (res.status === 401 ? 'This browser is not paired with the Matrix' : `${res.status} error`));
  return data;
}
async function send(extra) {
  if (DEMO) return { name: `uploaded/${(extra.name || 'animation').replace(/[^A-Za-z0-9_-]+/g, '_')}.gif`, demo: true };
  return api('POST', '/api/animator', { width: W, height: H, frames: frames.map((f) => ({ px: f.px, ms: f.ms })), loop: $('loop').value, background: bg, ...extra });
}
$('show').onclick = async () => {
  $('show').disabled = true;
  try { await send({ show: true }); toast(DEMO ? 'This is the demo: on your Matrix it plays there for 15 seconds' : 'Playing on the Matrix for 15 seconds'); }
  catch (e) { toast(e.message); } finally { $('show').disabled = false; }
};
// the drawing as a project: what Save keeps on the Matrix and Open reads back, layers and all
const projectName = () => ($('name').value.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '_').replace(/^[_-]+/, '').slice(0, 40).replace(/[_-]+$/, '') || 'animation');
const projectFile = () => Project.encode({ name: projectName(), w: W, h: H, bg, loop: $('loop').value, layers, frames });
async function keep() {
  const name = projectName();
  if (DEMO) return name;
  if (name !== savedAs && (await api('GET', '/api/animator/projects')).projects.some((p) => p.name === name && !p.built_in)
      && !window.confirm(`There's already a drawing called "${name}". Replace it with this one?`)) return null;
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
    const { name } = await send({ name: $('name').value || 'animation' });
    const file = name.replace('uploaded/', '');
    if (DEMO) { toast(`This is the demo: on your Matrix it saves ${file} to your media library`); return; }
    try { if (await keep() !== null) dirty = false; toast(`Saved ${file} to your media library`); }
    catch (e) { toast(`Saved ${file} to your media library, but not the drawing to open again: ${e.message}`); }
  } catch (e) { toast(e.message); } finally { $('save').disabled = false; }
};
function openDialog(dlg) { if (dlg.open) return; if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', ''); }
function load(d, what) {   // a project (from the Matrix or a file) replaces the drawing; Undo brings the old one back
  snapshot(true); sel = null;
  setSize(d.w, d.h);
  // new ids (a file numbers its layers 1, 2...): a frame copied from the old drawing then pastes flat, not onto a stranger's layer
  const fresh = d.layers.map((l) => ({ ...l, id: nextId() }));
  layers = fresh; frames = d.frames.map((f) => ({ ms: f.ms, px: empty(), cels: f.cels }));
  al = layers.length - 1; cur = 0; pi = 0; bg = d.bg; $('bg').value = bg; $('loop').value = d.loop; $('name').value = d.name;
  savedAs = null; renderAll(); toast(`Opened ${what}: Undo brings back what was here`);
}
$('open').onclick = async () => {
  const body = $('open-body'); body.replaceChildren();
  const say = (t) => { const p = document.createElement('p'); p.className = 'muted'; p.textContent = t; body.append(p); };
  openDialog($('opendlg'));
  if (DEMO) return say('This is the demo. On your own Matrix, everything you save is listed here to open again. "Open a drawing file…" under More works here too.');
  let list;
  try { list = (await api('GET', '/api/animator/projects')).projects; } catch (e) { return say(e.message); }
  if (!list.length) return say('Nothing saved yet. Save keeps a drawing here, with its layers, to open again.');
  // the 8 x 8 icons (bongo/icons.py) and the weather icons (modules/weather/look.py) are many, so each set
  // folds away under its own heading
  const isIcon = (p) => p.built_in && p.name.startsWith('icon_'), isWeather = (p) => p.built_in && p.name.startsWith('weather_');
  const HOW = new Map([
    [isIcon, 'Change one, keep it 8 × 8 and save it under the same name: the Matrix shows yours (its first frame).'],
    [isWeather, 'One drawing for each size a screen uses. Change one, keep its size and save it under the same name: the Matrix shows yours, with its frames and their timings.'],
  ]);
  for (const [title, mine, pick] of [['Yours', true, (p) => !p.built_in], ['Built in', false, (p) => p.built_in && !isIcon(p) && !isWeather(p)],
    ['Built in: icons for Calendar and Countdown', false, isIcon], ['Built in: weather icons', false, isWeather]]) {
    const some = list.filter(pick);
    if (!some.length) continue;
    const rows = document.createElement('div'); rows.className = 'projects';
    if (HOW.has(pick)) {
      const fold = document.createElement('details'), sum = document.createElement('summary');
      const h = document.createElement('h3'); h.textContent = `${title} · ${some.length}`; sum.append(h); sum.className = 'fold';
      const how = document.createElement('p'); how.className = 'muted'; how.textContent = HOW.get(pick);
      fold.append(sum, how, rows); body.append(fold);
    } else {
      const h = document.createElement('h3'); h.textContent = `${title} · ${some.length}`; body.append(h);
      body.append(rows);
    }
    some.forEach((p) => {
      const row = document.createElement('div'); row.className = 'project';
      const b = document.createElement('button'); b.type = 'button'; b.className = 'pick';
      const name = document.createElement('b'); name.textContent = p.name;
      const about = document.createElement('span'); about.textContent = `${p.width} × ${p.height}${p.replaces_built_in ? ' · your version of a built-in one' : ''}`;
      b.append(name, about);
      b.onclick = async () => {
        try { const d = Project.decode(await api('GET', `/api/animator/projects/${p.name}`)); $('opendlg').close(); load(d, `"${p.name}"`); savedAs = mine ? p.name : null; dirty = false; }
        catch (e) { toast(e.message); }
      };
      row.append(b);
      if (mine) {
        const del = document.createElement('button'); del.type = 'button'; del.className = 'btn'; del.textContent = 'Delete'; del.style.color = 'var(--danger)';
        del.setAttribute('aria-label', `Delete ${p.name}`);
        del.onclick = async () => {
          if (!window.confirm(p.replaces_built_in ? `Delete your version of "${p.name}"? The built-in one comes back.` : `Delete "${p.name}"? This can't be undone.`)) return;
          try { await api('DELETE', `/api/animator/projects/${p.name}`); $('open').onclick(); } catch (e) { toast(e.message); }
        };
        row.append(del);
      }
      rows.append(row);
    });
  }
};
$('open-close').onclick = () => $('opendlg').close();
for (const id of ['opendlg', 'spreaddlg']) $(id).addEventListener('click', (e) => { if (e.target === $(id)) $(id).close(); });   // a click on the backdrop
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
  try { load(Project.decode(JSON.parse(await file.text())), file.name); }
  catch (e) { toast(e instanceof SyntaxError ? `${file.name} isn't an Animator project` : `Couldn't open ${file.name}: ${e.message}`); }
};
document.addEventListener('click', (e) => { if ($('more').open && !$('more').contains(e.target)) closeMore(); });
$('more').addEventListener('toggle', () => {   // the menu opens leftwards from its button, unless that would run off the screen
  const list = $('more').querySelector('.menu-list'); list.style.right = '0'; list.style.left = 'auto';
  if ($('more').open && list.getBoundingClientRect().left < 8) { list.style.right = 'auto'; list.style.left = '0'; }
});

// -- keys -------------------------------------------------------------------------------
document.addEventListener('keydown', (e) => {
  if (document.querySelector('dialog[open]')) return;   // an open dialog has the keyboard
  if (busy()) return;   // mid-stroke: nothing changes the frame under the pointer
  if (e.target.tagName === 'BUTTON' && (e.key === ' ' || e.key === 'Enter')) return;   // a focused button's own key
  const ctrl = e.ctrlKey || e.metaKey, k = (e.key || '').toLowerCase();
  if (ctrl && !e.altKey && k === 's') { e.preventDefault(); closeMore(); $('keep').click(); return; }   // wherever the cursor is, the name box included
  if ($('more').open) { if (e.key === 'Escape') { closeMore(); $('more').querySelector('summary').focus(); } return; }   // the open menu has the keyboard
  if (e.altKey || (ctrl && !'zycvxas'.includes(k))) return;   // the browser's own shortcuts stay its own
  const typing = e.target.tagName === 'SELECT' || e.target.tagName === 'SUMMARY' || (e.target.tagName === 'INPUT' && !['checkbox', 'range', 'color'].includes(e.target.type));
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
  else if (area && arrow) { e.preventDefault(); if (needSel()) { lift(); moveSel(sel.x + arrow[0], sel.y + arrow[1]); renderAll(); } }
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

function renderAll() { flatAll(); renderFrames(); renderLayers(); renderBoard(); paintSwatches(); }

$('brush').onchange = () => { brush = Number($('brush').value) || 1; };
let tinting = false;   // one undo step for a whole go at the colour picker
$('bg').addEventListener('input', () => { if (!tinting) { snapshot(); tinting = true; } bg = $('bg').value; renderAll(); });
$('bg').addEventListener('change', () => { tinting = false; });
// the presets gallery: each one plays as a little live thumbnail; picking one replaces the frames
let galleryTimer = 0, galleryWatch = null;
const thumbCache = new Map(), builtFrames = new Map();
function presetFrames(id) { if (!builtFrames.has(id)) builtFrames.set(id, atPanel(() => preset(id).make())); return builtFrames.get(id); }
const shot = (f) => ({ img: toImage(f.px, PRESET_W, PRESET_H), ms: f.ms });
function openGallery() {
  const body = $('gallery-body');
  if (!body.childElementCount) {
    const groups = [...new Set(PRESETS.map((p) => p.group))];
    groups.forEach((g) => {
      const h = document.createElement('h3'); h.textContent = g; body.append(h);
      const picks = document.createElement('div'); picks.className = 'picks'; body.append(picks);
      PRESETS.filter((p) => p.group === g).forEach((p) => {
        const b = document.createElement('button'); b.type = 'button'; b.className = 'pick'; b.dataset.id = p.id;
        const c = document.createElement('canvas'); c.width = PRESET_W; c.height = PRESET_H;
        const name = document.createElement('b'); name.textContent = p.name;
        const about = document.createElement('span'); about.textContent = p.about;
        b.append(c, name, about);
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
  const cached = (id) => { if (!thumbCache.has(id)) thumbCache.set(id, { make: atPanel(() => preset(id).peek()), shots: [] }); return thumbCache.get(id); };
  // each tick builds a few more thumbnail frames (about 6 ms' worth, cards on screen only); a card plays once it's whole
  const step = (now) => atPanel(() => {
    const until = performance.now() + 6;
    for (const t of thumbs) {                 // first, a still for every card on screen, so none sits black
      if (!t.seen || performance.now() > until) continue;
      const th = cached(t.id);
      if (!th.shots.length) th.shots.push(shot(th.make.frameAt(0)));
      if (t.k < 0) t.ctx.putImageData(th.shots[0].img, 0, 0);
    }
    for (const t of thumbs) {
      if (!t.seen) continue;
      const th = cached(t.id);
      while (th.shots.length < th.make.length && performance.now() < until) {
        th.shots.push(shot(th.make.frameAt(th.shots.length)));
        if (th.shots.length === 1) t.ctx.putImageData(th.shots[0].img, 0, 0);
      }
      if (th.shots.length < th.make.length) continue;
      if (t.k >= 0 && now - t.at < th.shots[t.k].ms) continue;
      t.at = now; t.k = (t.k + 1) % th.shots.length; t.ctx.putImageData(th.shots[t.k].img, 0, 0);
    }
    galleryTimer = requestAnimationFrame(step);
  });
  galleryTimer = requestAnimationFrame(step);
  openDialog($('gallery'));
}
function choose(p) {
  closeGallery();
  snapshot(true); sel = null;
  if (W !== PRESET_W || H !== PRESET_H) setSize(PRESET_W, PRESET_H);   // a preset is a whole panel's worth
  frames = oneLayer(copyFrames(presetFrames(p.id))); cur = 0; pi = 0;
  $('name').value = p.id === 'blank' ? 'animation' : p.id; savedAs = null;
  renderAll(); toast(`Started from ${p.name}: Undo brings your frames back`);
}
function closeGallery() { cancelAnimationFrame(galleryTimer); const dlg = $('gallery'); if (dlg.open) dlg.close(); }
$('presets').onclick = openGallery;
$('gallery-close').onclick = closeGallery;
$('gallery').addEventListener('close', () => { cancelAnimationFrame(galleryTimer); galleryWatch?.disconnect(); });
$('gallery').addEventListener('click', (e) => { if (e.target === $('gallery')) closeGallery(); });   // a click on the backdrop
$('import').onclick = () => { closeMore(); if (DEMO) return toast('This is the demo. On your own Matrix, Import turns a GIF or picture into frames'); $('importfile').click(); };
$('importfile').onchange = async () => {
  const file = $('importfile').files[0]; $('importfile').value = '';
  if (!file) return;
  const form = new FormData(), w = W, h = H; form.append('file', file); form.append('width', w); form.append('height', h);   // fitted to this canvas
  try {
    const data = await api('POST', '/api/animator/import', form);
    if (w !== W || h !== H) return toast('The canvas changed size while that was importing: import it again');
    snapshot(true); sel = null; savedAs = null; frames = oneLayer(data.frames.map((f) => ({ ...f, ms: clamp(f.ms, 20, 5000) }))); cur = 0; pi = 0;
    $('name').value = file.name.replace(/\.[^.]+$/, '').replace(/[^A-Za-z0-9_-]+/g, '_').slice(0, 40) || 'animation';
    renderAll(); toast(`Imported ${data.note || `${frames.length} frame${frames.length === 1 ? '' : 's'}`}: Undo brings your old ones back`);
  } catch (e) { toast(e.message); }
};
window.addEventListener('beforeunload', (e) => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });   // unsaved work
window.bongoUnsaved = () => dirty;   // ...and the dashboard asks before it takes this frame away (app.js)

// -- start --------------------------------------------------------------------------------
setSize(W, H); dimPicker(15); setTool('pen'); frames = starter(); renderAll(); tick();
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
