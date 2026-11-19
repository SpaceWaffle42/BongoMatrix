// The Animator's page layout (layout C): the icons the editor draws with, the popovers, the Preview window and
// Play, the layers drawer and rail, the frame header's buttons, and (on a phone) the tabs and sheets. Loaded
// before animator.js, which calls init() at the start and the hooks below; nothing here draws pixels or changes
// the drawing except through the editor's own buttons. The editor's globals (frames, cur, playing, show...) are
// read when a hook runs, never at load.
(() => {
  const $ = (id) => document.getElementById(id);
  const SVG = (inner, size = 20) => `<svg class="i" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`;
  const ICONS = {
    play: '<path d="M7 4.5v15l12-7.5z" fill="currentColor"/>',
    pause: '<path d="M7.5 5v14M16.5 5v14" stroke-width="3"/>',
    eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.8"/>',
    eyeoff: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z" opacity=".5"/><path d="M4 20L20 4"/>',
    lock: '<rect x="5" y="10.5" width="14" height="10"/><path d="M8 10.5V7.5a4 4 0 018 0v3"/>',
    unlock: '<rect x="5" y="10.5" width="14" height="10"/><path d="M8 10.5V7.5a4 4 0 017.6-1.7"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  };
  const icon = (name, size) => SVG(ICONS[name], size);
  const store = { get: (k) => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode: not kept */ } } };
  const PHONE = window.matchMedia('(max-width: 600px), (max-height: 500px) and (orientation: landscape)');

  // -- popovers: one open at a time; an outside click or Escape closes it, focus goes back to what opened it --
  let open = null;   // { panel, anchor }
  function place(panel, anchor) {
    const r = anchor.getBoundingClientRect(), w = panel.offsetWidth, h = panel.offsetHeight, m = 8;
    let top = r.bottom + 6; if (top + h > innerHeight - m && r.top - h - 6 >= m) top = r.top - h - 6;   // above, when there's no room below (the timeline)
    panel.style.left = `${Math.max(m, Math.min(r.left, innerWidth - w - m))}px`;
    panel.style.top = `${Math.max(m, Math.min(top, innerHeight - h - m))}px`;
  }
  function popover(anchor, panel) {
    if (open && open.panel === panel) return close();
    close(false); $('more').open = false;
    panel.classList.add('open'); open = { panel, anchor };
    anchor.setAttribute('aria-expanded', 'true');
    if (PHONE.matches) { panel.style.left = panel.style.top = ''; sheets(); } else place(panel, anchor);
    return true;
  }
  function close(focus = true) {
    if (!open) return false;
    const { panel, anchor } = open; open = null;
    panel.classList.remove('open'); anchor.setAttribute('aria-expanded', 'false');
    if (focus && panel.contains(document.activeElement)) anchor.focus();
    sheets();
    return true;
  }
  document.addEventListener('pointerdown', (e) => { if (open && !open.panel.contains(e.target) && !open.anchor.contains(e.target)) close(false); }, true);
  window.addEventListener('resize', () => { if (open && open.panel.classList.contains('open') && !open.panel.closest('.bsheet')) place(open.panel, open.anchor); });

  // a toolbar toggle with options (onion skin, tracing picture): a click toggles; holding it (400 ms), a right-click
  // or the corner triangle opens its options
  let held = null;
  function withOptions(button, panel) {
    let timer = 0;
    const opts = () => { held = button; if (PHONE.matches) sheet('viewsheet', true); else popover(button, panel); };   // a phone: its options are in View…
    button.addEventListener('pointerdown', (e) => { if (e.button !== 0) return; clearTimeout(timer); timer = setTimeout(opts, 400); });
    for (const t of ['pointerup', 'pointerleave', 'pointercancel']) button.addEventListener(t, () => clearTimeout(timer));
    button.addEventListener('contextmenu', (e) => { e.preventDefault(); clearTimeout(timer); opts(); });
  }
  document.addEventListener('click', (e) => {   // (capture: before the button's own click toggles it)
    const car = e.target.closest?.('.car[data-opens]');
    if (held && held.contains(e.target)) { e.stopPropagation(); e.preventDefault(); held = null; return; }   // the click that ends a hold
    held = null;
    if (car) { e.stopPropagation(); e.preventDefault(); popover(car.parentElement, $(car.dataset.opens)); }
  }, true);

  // -- the Preview window and Play ---------------------------------------------------------------------
  // on: Play loops in the window docked in the timeline while you draw. Off: Play plays on the canvas in the
  // panel look (the same #preview canvas, laid over the board) and drawing waits until it stops
  let pvOn = true;
  function preview(on) {
    pvOn = on; document.body.classList.toggle('pv-on', on); $('pvtog').setAttribute('aria-pressed', String(on));
    store.set('animator.preview', on ? '1' : '0');
    document.querySelectorAll('[data-pv]').forEach((b) => { b.classList.toggle('on', b.dataset.pv === (on ? '1' : '0')); });
  }
  function togglePreview() {
    preview(!pvOn);
    if (typeof playing === 'undefined' || !playing) return;
    if (pvOn) overCanvas(false);          // playing on the canvas: it carries on in the window instead
    else $('play').click();               // the window going away while it plays: it stops (the canvas stays yours)
  }
  function overCanvas(on) {
    document.body.classList.toggle('playing-canvas', on);
    const led = $('led');
    if (!on) { led.style.left = led.style.top = led.style.width = led.style.height = ''; return; }
    const wr = $('board-wrap').getBoundingClientRect(), br = $('board').getBoundingClientRect(), zoomed = $('board-wrap').classList.contains('zoomed');
    const r = zoomed ? { left: wr.left + 10, top: wr.top + 10, width: wr.width - 20, height: wr.height - 20 } : br;   // zoomed in: the whole frame, fitted in the well
    Object.assign(led.style, { left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px` });
  }
  function played(on) {
    $('play').innerHTML = icon(on ? 'pause' : 'play'); $('play').setAttribute('aria-label', on ? 'Pause' : 'Play');
    overCanvas(on && !pvOn); shownPx = null;
  }
  window.addEventListener('resize', () => { if (document.body.classList.contains('playing-canvas')) overCanvas(true); });

  // -- what follows the frames: the readout by Play, the scrubber, the Preview when stopped, the notes --------
  let shownPx = null, shownBg = '';
  function framesShown() {
    const n = frames.length, set = (el, k, v) => { if (el[k] !== v) el[k] = v; };   // (only what changed: the strip redraws on every stroke)
    set($('fpos'), 'innerHTML', `${cur + 1} <small>/ ${n}</small>`);
    set($('scrub').firstElementChild.style, 'width', `${(100 * (cur + 1)) / n}%`);
    set($('fnote'), 'title', $('fnote').textContent);
    set($('ftotal'), 'title', `How many frames, and how long one play-through lasts on the Matrix${$('fnote').hidden ? '' : `. ${$('fnote').textContent}`}`);
    if (!playing && (shownPx !== frames[cur].px || shownBg !== bg + dim)) { show(frames[cur]); shownPx = frames[cur].px; shownBg = bg + dim; }   // stopped: the Preview shows this frame
    if ($('timepop').classList.contains('open') && document.activeElement !== $('fms')) $('fms').value = frames[cur].ms;
    phoneFrames();
  }
  function showNote(anchor) { if ($('fnote').hidden) return; $('notetext').textContent = $('fnote').textContent; popover(anchor, $('notepop')); }

  // -- the layers: a rail of thumbnails by the canvas, and the drawer with the list and its buttons ------------
  function rail() {   // (the buttons are kept and repainted: renderLayers runs on every stroke)
    const rows = [...$('layers').children], box = $('lthumbs');
    while (box.children.length < rows.length) {
      const b = document.createElement('button'); b.type = 'button'; b.className = 'lth';
      const name = document.createElement('span'), bd = document.createElement('i'); bd.className = 'bd'; bd.innerHTML = icon('lock', 12);
      b.append(document.createElement('canvas'), name, bd); box.append(b);
    }
    while (box.children.length > rows.length) box.lastChild.remove();
    rows.forEach((row, i) => {
      const k = layers.length - 1 - i, l = layers[k], src = row.querySelector('canvas'), b = box.children[i], [c, name, bd] = b.children;
      b.className = 'lth' + (k === al ? ' sel' : '') + (l.on ? '' : ' off');
      const title = `${l.name}${isLocked(k) ? ' (locked)' : ''}${l.on ? '' : ' (hidden)'}`;
      if (b.title !== title) { b.title = title; b.setAttribute('aria-label', `Layer ${title}`); name.textContent = l.name; }
      b.setAttribute('aria-pressed', String(k === al)); bd.hidden = !isLocked(k);
      if (c.width !== src.width || c.height !== src.height) { c.width = src.width; c.height = src.height; }
      const cel = frames[cur].cels[k];
      if (b.cel !== cel || b.w !== src.width || b.bg !== bg) { const g = c.getContext("2d"); g.clearRect(0, 0, c.width, c.height); g.drawImage(src, 0, 0); b.cel = cel; b.w = src.width; b.bg = bg; }   // only a picture that changed
      b.onclick = () => row.click();
    });
    $('lrailadd').disabled = $('laddu').disabled;
  }
  function drawer(on) {
    $('layerdrawer').classList.toggle('open', on); $('layersbtn').setAttribute('aria-expanded', String(on));
    store.set('animator.layers', on ? '1' : '0');
  }

  // -- the picture to trace: the toolbar toggle shows whether it's on ----------------------------------
  function traced() {
    const t = typeof tracing !== 'undefined' ? tracing : null, on = !!(t && t.on);
    $('tracebtn').classList.toggle('on', on); $('tracebtn').setAttribute('aria-pressed', String(on)); document.body.classList.toggle('has-trace', !!t);
    if (!t && open && open.panel === $('traceopts')) close(false);
  }

  // -- the phone: one DOM, its pieces moved into the panel's tabs and the sheets, and back on a wide screen ------
  const MOVES = [   // [what, where it goes on a phone, before what there (else at the end)]
    ['toolbar', 'dk-draw', 'qcol'], ['opts', 'dk-draw', 'qcol'], ['pal', 'dk-colour'], ['layerdrawer', 'dk-layers'],
    ['fwrap', 'dk-frames', 'fhint'], ['fnote', 'dk-frames', 'fhint'],
    ['fr-dup', 'fact'], ['fr-rev', 'fact'], ['fr-time', 'fact'], ['fmore', 'fact'], ['fr-del', 'fact'],
    ['zoomw', 'board-wrap'], ['namew', 'fileslot'], ['csizew', 'fileslot'], ['presets', 'fileslot'], ['size', 'phname'], ['show', 'savemenu', 'keep'],
    ['grid', 'vtiles'], ['safe', 'vtiles'], ['centre', 'vtiles'], ['tracebtn', 'vtiles'], ['onionopts', 'vslot'], ['traceopts', 'vslot'],
    ['spread', 'fmphone'], ['loopw', 'fmphone'], ['playopts', 'fmphone'],
  ];
  const homes = new Map();   // each one's place on a wide screen: a marker just before it
  let laidPhone = null, tab = 'draw';
  function arrange() {
    const phone = PHONE.matches;
    if (phone === laidPhone) return;
    laidPhone = phone; closeAll();
    if (!homes.size) MOVES.forEach(([id]) => { const el = $(id), m = document.createComment(id); el.before(m); homes.set(id, m); });
    for (const [id, to, before] of MOVES) {
      const el = $(id);
      if (phone) { if (before) $(to).insertBefore(el, $(before)); else $(to).append(el); }
      else homes.get(id).after(el);
    }
    setTab(tab);
    if (typeof frames !== 'undefined' && frames.length) { fitBoard(); renderFrames(); }
  }
  function setTab(t) {
    tab = t;
    for (const d of ['draw', 'colour', 'layers', 'frames']) document.body.classList.toggle(`d-${d}`, PHONE.matches && d === t);
    document.querySelectorAll('[data-dock]').forEach((b) => { b.classList.toggle('on', b.dataset.dock === t); b.setAttribute('aria-pressed', String(b.dataset.dock === t)); });
    if (t === 'frames' && typeof renderFrames === 'function' && typeof frames !== 'undefined' && frames.length) renderFrames();   // the grid's cards come into view: paint them, scroll to this one
  }
  // the sheets (≡, Save, View) and a phone's popovers come up from the bottom over a scrim; a tap on it closes them
  function sheet(id, on) { $(id).classList.toggle('open', on); sheets(); }
  function sheets() {
    const any = $('more').open || $('savemenu').classList.contains('open') || $('viewsheet').classList.contains('open') || (!!open && PHONE.matches);
    document.body.classList.toggle('sheet-open', any && PHONE.matches);
  }
  function closeAll() { close(false); $('more').open = false; $('savemenu').classList.remove('open'); $('savebtn').setAttribute('aria-expanded', 'false'); $('viewsheet').classList.remove('open'); sheets(); }
  // the transport row's strip of small frames, around this one: a tap picks one, a sideways drag steps through them
  const thumbs = new WeakMap();
  let miniFirst = null;   // the first frame the mini strip shows
  function phoneFrames() {
    if (!PHONE.matches) return;
    $('phnm').textContent = $('name').value;
    const n = frames.length, total = $('ftotal').textContent.split('·')[1] || '';
    $('prd').innerHTML = `${cur + 1} / ${n}<br>${total.trim()}`;
    const box = $('mini'), w = box.clientWidth, slots = Math.max(1, Math.min(n, 1 + Math.floor((w - 46) / 32)));
    const keep = typeof FS !== 'undefined' && FS.holding && miniFirst != null;   // a frame held or dragged in it: the strip stays put under the finger
    const first = miniFirst = Math.max(0, Math.min(n - slots, keep ? miniFirst : cur - Math.floor(slots / 2)));
    while (box.children.length < slots) { const c = document.createElement('canvas'); box.append(c); }
    while (box.children.length > slots) box.lastChild.remove();
    [...box.children].forEach((c, j) => {
      const i = first + j, f = frames[i]; c.dataset.i = i; c.className = i === cur ? 'c' : '';
      if (c.width !== W || c.height !== H) { c.width = W; c.height = H; c.px = null; }
      if (c.px !== f.px || c.bg !== bg) { let img = thumbs.get(f.px); if (!img || img.bg !== bg) { img = toImage(f.px, W, H); img.bg = bg; thumbs.set(f.px, img); } c.getContext('2d').putImageData(img, 0, 0); c.px = f.px; c.bg = bg; }
    });
  }
  function miniStrip() {
    let start = null;
    const box = $('mini');
    box.addEventListener('pointerdown', (e) => { start = { x: e.clientX, cur, moved: false }; try { box.setPointerCapture(e.pointerId); } catch { /* gone */ } });
    box.addEventListener('pointermove', (e) => {
      if (start && typeof FS !== 'undefined' && FS.holding) start = null;   // held still: js/animator-frames.js drags the frame instead
      if (!start) return;
      const steps = Math.round((start.x - e.clientX) / 24);   // drag left: later frames come in from the right
      if (steps) start.moved = true;
      const to = Math.max(0, Math.min(frames.length - 1, start.cur + steps));
      if (to !== cur) { cur = to; renderAll(); }
    });
    box.addEventListener('pointerup', (e) => {
      if (start && !start.moved) { const c = document.elementFromPoint(e.clientX, e.clientY); if (c && c.parentElement === box) { cur = Number(c.dataset.i); renderAll(); } }
      start = null;
    });
    box.addEventListener('pointercancel', () => { start = null; });
    box.addEventListener('click', () => {});   // (so a tap on it isn't moved to Play or the Preview switch beside it)
  }
  // the Draw tab's quick colours: main, second, the last five picked, all colours; and the first 8 in this animation
  let recent = [];
  function quick() {
    if (!PHONE.matches || typeof colour === 'undefined') return;
    const q = $('qcol'), chip = (c, cls, label) => { const b = document.createElement('button'); b.type = 'button'; b.className = cls; b.style.background = c; b.title = label; b.setAttribute('aria-label', label); b.onclick = () => setTab('colour'); return b; };
    q.replaceChildren(chip(colour, 'main', `Main colour ${colour}: all colours`), chip(colour2, 'second', `Second colour ${colour2}: all colours`));
    recent.filter((c) => c !== colour && c !== colour2).slice(0, 5).forEach((c) => swatch(c, q));
    const shown = () => [colour, colour2, ...[...q.querySelectorAll('button')].map((b) => b.title)].map((c) => c.toLowerCase());
    for (const c of PALETTE) { if (q.children.length >= 7) break; if (!shown().includes(c)) swatch(c, q); }   // (the palette fills in until five have been picked)
    const all = document.createElement('button'); all.type = 'button'; all.className = 'all'; all.innerHTML = SVG('<path d="M12 3a9 9 0 100 18c1.4 0 2-1 1.6-2.2-.5-1.4.3-2.8 1.8-2.8H18a3 3 0 003-3c0-5.5-4-10-9-10z"/><circle cx="7.5" cy="11" r="1.3" fill="currentColor"/><circle cx="10.5" cy="7" r="1.3" fill="currentColor"/><circle cx="15.5" cy="7.5" r="1.3" fill="currentColor"/>', 22);
    all.title = 'All colours'; all.setAttribute('aria-label', 'All colours'); all.onclick = () => setTab('colour'); q.append(all);
    $('qused').replaceChildren(); used.slice(0, 8).forEach((c) => swatch(c, $('qused')));
  }
  function revealPhone(el) {
    if (!el.closest('#filemenu, #savemenu, #viewsheet, .pop.open')) closeAll();   // another sheet in the way: put it away first
    const dk = el.closest('.dk'); if (dk) setTab(dk.id.slice(3));
    if (el.closest('#viewsheet')) sheet('viewsheet', true);
    if (el.closest('#savemenu')) { $('savemenu').classList.add('open'); sheets(); }
    if (el.closest('#fmenu') && !$('fmenu').classList.contains('open')) popover($('fmore'), $('fmenu'));
    else { const pop = el.closest('.pop'); if (pop && !el.closest('#viewsheet') && !pop.classList.contains('open')) popover($(ANCHOR[pop.id]), pop); }
    if (el.closest('#filemenu')) $('more').open = true;
    sheets();
  }

  // open whatever holds a control (a menu, a popover, the drawer; on a phone its tab or sheet), so it can be seen
  // and used: for tests and the acceptance walk, and for the phone's links ("Change a colour..." in the Draw tab)
  const ANCHOR = { onionopts: 'onion', traceopts: 'tracebtn', playopts: 'playgear', timepop: 'ftime', fmenu: 'fmore', helppop: 'help', notepop: 'ftotal' };
  function reveal(id) {
    const el = typeof id === 'string' ? $(id) : id;
    if (!el) return false;
    if (PHONE.matches) { revealPhone(el); return !!el.offsetParent; }
    if (el.closest('#filemenu')) $('more').open = true;
    if (el.closest('#savemenu') && getComputedStyle($('savebtn')).display !== 'none') { $('savemenu').classList.add('open'); $('savebtn').setAttribute('aria-expanded', 'true'); }
    const pop = el.closest('.pop');
    if (pop && !pop.classList.contains('open')) popover($(ANCHOR[pop.id]), pop);
    if (el.closest('#layerdrawer')) drawer(true);
    const pal = el.closest('#used') ? 'used' : el.closest('#swatches') ? 'all' : null;
    if (pal) document.querySelector(`[data-pal=${pal}]`).click();   // the colour strip shows one of the two at a time
    return !!el.offsetParent;
  }

  function init() {
    // the Preview: on by default on a wide screen, off on a phone; remembered
    const kept = store.get('animator.preview');
    preview(kept === '1' || kept === '0' ? kept === '1' : !PHONE.matches);
    $('pvtog').onclick = togglePreview;
    $('led').onclick = () => $('play').click();   // a click on the playing picture stops it (and draws nothing)
    // File: any item closes the menu
    $('filemenu').addEventListener('click', (e) => { if (e.target.closest('button')) setTimeout(() => { $('more').open = false; }); });
    $('csize').addEventListener('change', () => { $('more').open = false; });   // (a phone: it's in the ≡ sheet)
    // Save ▾ (at 1160 px and below): Save drawing and Save + add to library
    const saveMenu = (on) => { $('savemenu').classList.toggle('open', on); $('savebtn').setAttribute('aria-expanded', String(on)); sheets(); };
    $('savebtn').onclick = () => saveMenu(!$('savemenu').classList.contains('open'));
    $('savemenu').addEventListener('click', (e) => { if (e.target.closest('button')) saveMenu(false); });
    document.addEventListener('pointerdown', (e) => { if ($('savemenu').classList.contains('open') && !$('savegrp').contains(e.target)) saveMenu(false); }, true);
    // help, the onion skin, the tracing picture, play options, timing, the frame menu
    $('help').onclick = () => popover($('help'), $('helppop'));
    withOptions($('onion'), $('onionopts'));
    withOptions($('tracebtn'), $('traceopts'));
    $('tracebtn').onclick = () => { if (typeof tracing === 'undefined' || !tracing) { $('tracefile').click(); return; } $('traceon').checked = !$('traceon').checked; $('traceon').onchange(); };
    $('traceanother').onclick = () => { close(false); $('tracefile').click(); };
    $('playgear').onclick = () => popover($('playgear'), $('playopts'));
    $('ftime').onclick = () => { $('fms').value = frames[cur].ms; popover($('ftime'), $('timepop')); };
    $('fms').onchange = () => { const box = document.querySelectorAll('#frames .frame input')[cur]; if (!box) return; box.value = $('fms').value; box.dispatchEvent(new Event('change')); $('fms').value = frames[cur].ms; };
    $('fmore').onclick = () => popover($('fmore'), $('fmenu'));
    $('fmenu').addEventListener('click', (e) => { if (e.target.closest('.mi')) close(false); });
    $('fnote').onclick = () => showNote($('fnote'));
    $('ftotal').onclick = () => showNote($('ftotal'));
    // the frame header: add, the frame either side; the picked frames' buttons (#fr-*) are js/animator-frames.js's
    $('fadd').onclick = () => $('frames').querySelector('.add')?.click();
    $('fprev').onclick = () => { cur = (cur - 1 + frames.length) % frames.length; renderAll(); };
    $('fnext').onclick = () => { cur = (cur + 1) % frames.length; renderAll(); };
    // the colour strip: the palette, or the colours in this animation
    document.querySelectorAll('[data-pal]').forEach((b) => { b.onclick = () => { $('pal').classList.toggle('show-used', b.dataset.pal === 'used'); document.querySelectorAll('[data-pal]').forEach((x) => { x.classList.toggle('on', x === b); x.setAttribute('aria-pressed', String(x === b)); }); }; });
    // the layers drawer: opened from the rail, remembered
    drawer(store.get('animator.layers') === '1');
    $('layersbtn').onclick = () => drawer(!$('layerdrawer').classList.contains('open'));
    $('layersclose').onclick = () => drawer(false);
    $('lrailadd').onclick = () => $('laddu').click();
    $('frames').addEventListener('wheel', (e) => { const f = $('frames'); if (f.scrollWidth > f.clientWidth && Math.abs(e.deltaY) > Math.abs(e.deltaX)) { f.scrollLeft += e.deltaY; e.preventDefault(); } }, { passive: false });   // a mouse wheel scrolls the strip sideways
    // Escape closes a popover, the Save menu or the drawer, before the editor sees it
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape' || document.querySelector('dialog[open]')) return;
      if (close()) { e.stopPropagation(); return; }
      if ($('viewsheet').classList.contains('open')) { sheet('viewsheet', false); e.stopPropagation(); return; }
      if ($('savemenu').classList.contains('open')) { saveMenu(false); $('savebtn').focus(); e.stopPropagation(); return; }
      if ($('layerdrawer').classList.contains('open') && !PHONE.matches && !(e.target.closest && e.target.closest('#layers input'))) { drawer(false); e.stopPropagation(); }
    }, true);
    if (new URLSearchParams(location.search).has('demo')) $('demo-note').hidden = false;
    // the phone: tabs, sheets, the strip of small frames, quick colours, the name in the header
    arrange(); PHONE.addEventListener('change', arrange);
    document.querySelectorAll('[data-dock]').forEach((b) => { b.onclick = () => setTab(b.dataset.dock); });
    document.querySelectorAll('[data-pv]').forEach((b) => { b.onclick = () => { if ((b.dataset.pv === '1') !== pvOn) togglePreview(); }; });
    $('viewbtn').onclick = () => sheet('viewsheet', true);
    $('viewx').onclick = () => sheet('viewsheet', false);
    $('filex').onclick = () => { $('more').open = false; };
    $('savex').onclick = () => saveMenu(false);
    $('more').addEventListener('toggle', () => { $('filettl').textContent = $('name').value; sheets(); });
    const scrim = document.createElement('div'); scrim.id = 'scrim'; document.body.append(scrim);
    scrim.addEventListener('pointerdown', (e) => { e.preventDefault(); closeAll(); });
    if (window.top !== window) { $('leave').hidden = false; $('leave').onclick = () => window.parent.postMessage({ type: 'animator-leave' }, location.origin); }   // in the dashboard: its own unsaved-work question asks first
    $('name').addEventListener('input', () => { $('phnm').textContent = $('name').value; });
    $('qrecolour').onclick = () => $('recolour').click();
    miniStrip();
    new MutationObserver(() => { const c = $('hex').textContent.toLowerCase(); recent = [c, ...recent.filter((x) => x !== c)].slice(0, 8); quick(); }).observe($('hex'), { childList: true, characterData: true, subtree: true });
    new MutationObserver(quick).observe($('used'), { childList: true });
    $('colour2').addEventListener('input', quick);
    let shownTool = '';   // body.t-<tool> for the phone's Draw tab; changed only when the tool does (a class on body restyles the page)
    new MutationObserver(() => { if (typeof tool === 'undefined' || tool === shownTool) return; document.body.classList.remove(`t-${shownTool}`); shownTool = tool; document.body.classList.add(`t-${tool}`); }).observe($('toolname'), { childList: true, characterData: true, subtree: true });
        played(pvOn);
    return pvOn;
  }

  const timeOne = (from) => { $('fms').value = frames[cur].ms; popover(from || $('fr-time'), $('timepop')); };   // Time… with one frame chosen
  window.AnimatorLayout = { icon, timeOne, init, reveal, played, togglePreview, framesShown, rail, traced, popover, close, help: () => popover($('help'), $('helppop')), phone: () => PHONE.matches };
})();
