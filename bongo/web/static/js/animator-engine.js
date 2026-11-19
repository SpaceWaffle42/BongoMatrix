// The Animator's bookkeeping underneath the editor (animator.js), so a 300-frame animation costs what changed,
// not what it holds:
//  - cels (one layer's pixels in one frame) are made read-only once Undo or a cache may keep them, so Undo
//    keeps a reference, not a copy. Drawing on one copies it first (writable() in animator.js does that via
//    `target`); a tool that changes a cel in place without that does nothing, so write through `target.px`,
//    or put a new array in `f.cels[k]`.
//  - each frame's flattened picture is kept by which cels it was made from, so only a changed frame is redone
//  - "In this animation" colours are counted per flattened picture, and only new pictures are counted
//  - a draft of unsaved work is kept in the browser (IndexedDB) and offered when the Animator opens again
(function () {
  // -- read-only cels ---------------------------------------------------------------------------------
  const ids = new WeakMap(); let idSeq = 0;
  const idOf = (a) => { let i = ids.get(a); if (!i) ids.set(a, (i = ++idSeq)); return i; };
  const EMPTY = new Map();   // one read-only empty cel per size, shared by every layer and frame with nothing on it
  const emptyCel = (n) => { let e = EMPTY.get(n); if (!e) EMPTY.set(n, (e = Object.freeze(new Array(n).fill(null)))); return e; };
  const blank = (c) => { for (let i = 0; i < c.length; i++) if (c[i]) return false; return true; };
  // every cel of every frame made read-only; an empty one becomes the shared empty cel (a shared layer's one
  // array stays one array). Cheap: only cels changed since last time are looked at.
  function settle(frames) {
    let swap = null;
    for (const f of frames) {
      const cels = f.cels;
      for (let k = 0; k < cels.length; k++) {
        const c = cels[k];
        if (Object.isFrozen(c)) continue;
        if (swap && swap.has(c)) { cels[k] = swap.get(c); continue; }
        if (blank(c)) (swap ||= new Map()).set(c, (cels[k] = emptyCel(c.length)));
        else Object.freeze(c);
      }
    }
  }

  // -- undo steps -----------------------------------------------------------------------------------------
  // A step holds the layers' settings and, per frame, its time and the cels it had (references: they're
  // read-only). A frame that didn't change keeps the record of the step before, so a pencil stroke on one
  // frame costs one cel and one small record. `bytes` is roughly what the step added to memory.
  const kept = new WeakSet(), records = new WeakMap();   // live frame -> its last record
  function record(f) {
    const r = records.get(f), cels = f.cels;
    if (r && r.ms === f.ms && r.cels.length === cels.length && r.cels.every((c, k) => c === cels[k])) return [r, 0];
    const n = Object.freeze({ ms: f.ms, cels: Object.freeze(cels.slice()) });
    records.set(f, n);
    return [n, 48 + 8 * cels.length];
  }
  function capture(doc) {   // doc: { w, h, bg, cur, al, layers, frames, ...anything else to keep (whole, name...) }
    settle(doc.frames);
    let bytes = 200 + 120 * doc.layers.length;
    const frames = doc.frames.map((f) => {
      const [r, b] = record(f); bytes += b;
      if (b) for (const c of f.cels) if (!kept.has(c)) { kept.add(c); bytes += 16 + 8 * c.length; }
      return r;
    });
    return { ...doc, layers: doc.layers.map((l) => ({ ...l })), frames, bytes };
  }
  // the frames and layers of a step, ready to edit: new frame objects and layer objects, the same cels
  function revive(step) {
    return { layers: step.layers.map((l) => ({ ...l })),
      frames: step.frames.map((r) => { const f = { ms: r.ms, px: null, cels: r.cels.slice() }; records.set(f, r); return f; }) };
  }

  // -- flattened pictures, kept by what they were made from ------------------------------------------------
  const flats = new Map(), FLATS = 3 * 300 * 64 * 32;   // key -> px, up to three full-size animations' worth of pixels; the oldest go first
  let flatPixels = 0;
  function flatKey(cels, layers, bg) {
    let k = `${bg}|${cels[0] ? cels[0].length : 0}`;
    for (let i = 0; i < cels.length; i++) {
      const l = layers[i], o = l.opacity ?? 1;
      if (!l.on || o <= 0) continue;
      if (!Object.isFrozen(cels[i])) return null;   // being drawn on: no key yet
      k += `|${idOf(cels[i])}:${o}:${l.mode}`;
    }
    return k;
  }
  // compose(cels, layers, bg) as T.compose does it, or the same picture made before. The array may be shared
  // with other frames that look the same: never change a frame's px in place.
  function flattened(cels, layers, bg, compose) {
    const key = flatKey(cels, layers, bg);
    if (key === null) return compose(cels, layers, bg);
    let px = flats.get(key);
    if (px) { flats.delete(key); flats.set(key, px); return px; }
    px = compose(cels, layers, bg); flats.set(key, px); flatPixels += px.length;
    while (flatPixels > FLATS && flats.size > 1) { const [old, p] = flats.entries().next().value; flats.delete(old); flatPixels -= p.length; }
    return px;
  }

  // -- "In this animation": the most used colours ---------------------------------------------------------
  // Pixels are counted per flattened picture (kept, so a picture is counted once however often it's shown)
  // and added up over the frames. Colours within FOLD of each other on red, green and blue count as one, shown
  // as its most used member, so a gradient takes a slot or two, not all sixteen. The sixteen are shown greys
  // first (dark to light), then round the colour wheel, so a colour coming or going moves the others little.
  const FOLD = 24;
  const counted = new WeakMap();
  function countOf(px) {
    let m = counted.get(px);
    if (!m) { m = new Map(); for (let i = 0; i < px.length; i++) { const c = px[i]; if (c) m.set(c, (m.get(c) || 0) + 1); } counted.set(px, m); }
    return m;
  }
  const rgb = (c) => [parseInt(c.slice(1, 3), 16), parseInt(c.slice(3, 5), 16), parseInt(c.slice(5, 7), 16)];
  function hueOrder(c) {
    const [r, g, b] = rgb(c), hi = Math.max(r, g, b), lo = Math.min(r, g, b), l = (hi + lo) / 2;
    if (hi - lo < 24) return [0, l];   // a grey
    const d = hi - lo, h = hi === r ? ((g - b) / d + 6) % 6 : hi === g ? (b - r) / d + 2 : (r - g) / d + 4;
    return [1 + Math.floor(h * 2), l];   // twelve hue bands, dark to light within each
  }
  function makeTally() {
    let total = new Map(), seen = new Map();   // colour -> pixels; picture -> how many frames show it
    const add = (px, times) => { for (const [c, n] of countOf(px)) { const v = (total.get(c) || 0) + n * times; if (v) total.set(c, v); else total.delete(c); } };
    return {
      update(pictures) {   // the frames' px now: only pictures not seen last time are counted
        const now = new Map();
        for (const p of pictures) if (p) now.set(p, (now.get(p) || 0) + 1);
        for (const [p, m] of seen) { const d = (now.get(p) || 0) - m; if (d) add(p, d); }
        for (const [p, m] of now) if (!seen.has(p)) add(p, m);
        seen = now;
      },
      top(n = 16) {
        const list = [...total].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1)), picks = [];
        for (const [c, count] of list) {
          const v = rgb(c), near = picks.find((p) => Math.abs(p.v[0] - v[0]) <= FOLD && Math.abs(p.v[1] - v[1]) <= FOLD && Math.abs(p.v[2] - v[2]) <= FOLD);
          if (near) near.n += count; else if (picks.length < n * 4) picks.push({ c, v, n: count });
        }
        return picks.sort((a, b) => b.n - a.n).slice(0, n).map((p) => p.c)
          .sort((a, b) => { const x = hueOrder(a), y = hueOrder(b); return x[0] - y[0] || x[1] - y[1] || (a < b ? -1 : 1); });
      },
      reset() { total = new Map(); seen = new Map(); },
    };
  }

  // -- a draft kept in the browser ------------------------------------------------------------------------
  // One record in IndexedDB ("bongo-animator", store "draft", key "draft", or "demo" in the demo): the browser keeps it per Matrix (its
  // address). Each cel is written as runs of colours, "12*ff8800 3*. a0b0c0", and a read-only cel's text is
  // kept, so a draft after one stroke encodes one cel. Everything fails quietly: no storage, no draft.
  const celText = new WeakMap();
  function encodeCel(c) {
    let s = Object.isFrozen(c) ? celText.get(c) : null;
    if (s) return s;
    const out = [];
    for (let i = 0; i < c.length;) {
      let j = i + 1; while (j < c.length && c[j] === c[i]) j++;
      const t = c[i] ? c[i].slice(1) : '.';
      out.push(j - i > 1 ? `${j - i}*${t}` : t); i = j;
    }
    s = out.join(' ');
    if (Object.isFrozen(c)) celText.set(c, s);
    return s;
  }
  function decodeCel(s, n) {
    const px = [];
    for (const t of s.split(' ')) {
      if (!t) continue;
      const star = t.indexOf('*'), k = star < 0 ? 1 : Number(t.slice(0, star)), v = star < 0 ? t : t.slice(star + 1);
      if (!(k >= 1) || !(v === '.' || /^[0-9a-f]{6}$/.test(v)) || px.length + k > n) throw new Error('a damaged draft');
      const c = v === '.' ? null : `#${v}`;
      for (let i = 0; i < k; i++) px.push(c);
    }
    if (px.length !== n) throw new Error('a damaged draft');
    return px;
  }
  // doc: { name, w, h, bg, loop, savedAs, cur, al, layers, frames } -> what's stored (a shared cel once)
  function pack(doc) {
    const index = new Map(), cels = [];
    const frames = doc.frames.map((f) => ({ ms: f.ms, cels: f.cels.map((c) => { if (!index.has(c)) { index.set(c, cels.length); cels.push(encodeCel(c)); } return index.get(c); }) }));
    return { v: 1, at: Date.now(), name: doc.name, w: doc.w, h: doc.h, bg: doc.bg, loop: doc.loop, savedAs: doc.savedAs ?? null, cur: doc.cur, al: doc.al,
      layers: doc.layers.map((l) => ({ name: l.name, on: l.on, opacity: l.opacity, mode: l.mode, shared: !!l.shared, ...(l.locked ? { locked: true } : {}) })), frames, cels };
  }
  function unpack(d) {
    if (!d || d.v !== 1 || !Array.isArray(d.frames) || !d.frames.length || !Array.isArray(d.layers) || !d.layers.length) throw new Error('a damaged draft');
    const n = d.w * d.h, made = d.cels.map((s) => Object.freeze(decodeCel(s, n)));
    const frames = d.frames.map((f) => {
      if (f.cels.length !== d.layers.length) throw new Error('a damaged draft');
      return { ms: f.ms, px: null, cels: f.cels.map((i) => { if (!made[i]) throw new Error('a damaged draft'); return made[i]; }) };
    });
    return { name: String(d.name || 'animation'), w: d.w, h: d.h, bg: d.bg, loop: d.loop, savedAs: d.savedAs, at: d.at,
      cur: Math.min(d.cur | 0, frames.length - 1), al: Math.min(d.al | 0, d.layers.length - 1), layers: d.layers.map((l) => ({ ...l })), frames };
  }
  let dbWait = null;
  function db() {
    if (!dbWait) dbWait = new Promise((ok, no) => {
      try {
        const req = indexedDB.open('bongo-animator', 1);
        req.onupgradeneeded = () => req.result.createObjectStore('draft');
        req.onsuccess = () => ok(req.result); req.onerror = () => no(req.error); req.onblocked = () => no(new Error('blocked'));
      } catch (e) { no(e); }
    });
    return dbWait;
  }
  async function store(mode, act) {
    const d = await db();
    return new Promise((ok, no) => {
      const tx = d.transaction('draft', mode), req = act(tx.objectStore('draft'));
      tx.oncomplete = () => ok(req.result); tx.onerror = tx.onabort = () => no(tx.error || new Error('not stored'));
    });
  }
  const quietly = (p, or) => Promise.race([p.catch(() => or), new Promise((ok) => setTimeout(() => ok(or), 4000))]);   // storage that never answers counts as none
  const Draft = {   // key: one draft per page kind ('draft', or 'demo' for the demo)
    read: (key) => quietly(store('readonly', (s) => s.get(key)).then((d) => (d ? unpack(d) : null)), null),
    write: (key, doc) => quietly(store('readwrite', (s) => s.put(pack(doc), key)).then(() => true), false),
    clear: (key) => quietly(store('readwrite', (s) => s.delete(key)).then(() => true), false),
    pack, unpack, encodeCel, decodeCel,
  };

  window.AnimatorEngine = { settle, emptyCel, capture, revive, flattened, idOf, makeTally, Draft, FOLD };
})();
