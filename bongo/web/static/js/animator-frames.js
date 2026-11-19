// The frame strip: choosing several frames, acting on them together, and dragging frames to a new place
// (window.AnimatorFrames). Two parts:
//  - pure list work (indices in, a new order out), run under node by tests/test_animator_frames.py
//  - strip(host): the selection and the strip's mouse, touch and keys, wired by animator.js
// The selection is kept as the frame objects themselves, so adding, deleting or moving frames anywhere keeps it on
// the same pictures. It isn't part of the drawing and never takes an undo step, but each step keeps its indices (as
// it keeps the current frame), so Undo and Redo bring back the selection that went with the drawing. The current
// frame is always one of the selected.
(function () {
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  // -- the pure part --------------------------------------------------------------------------------------
  const order = (sel, n) => [...new Set(sel)].filter((i) => Number.isInteger(i) && i >= 0 && i < n).sort((a, b) => a - b);
  const span = (a, b) => { const lo = Math.min(a, b); return Array.from({ length: Math.abs(a - b) + 1 }, (_, k) => lo + k); };
  // copies of the selected, in order, straight after the last of them: the new list and where the copies are
  function duplicate(list, sel, clone) {
    const s = order(sel, list.length); if (!s.length) return null;
    const at = s[s.length - 1] + 1, copies = s.map((i) => clone(list[i]));
    return { list: [...list.slice(0, at), ...copies, ...list.slice(at)], picked: copies.map((_, k) => at + k) };
  }
  // the selected in reverse order among themselves, the others where they were; fewer than two selected: every frame
  function reverse(list, sel) {
    let s = order(sel, list.length); if (s.length < 2) s = list.map((_, i) => i);
    const out = list.slice(); s.forEach((i, k) => { out[i] = list[s[s.length - 1 - k]]; });
    return out;
  }
  // the selected taken out; null if that would leave no frame. cur: the frame before the first one taken (as Delete does)
  function remove(list, sel) {
    const s = order(sel, list.length); if (!s.length || s.length >= list.length) return null;
    const gone = new Set(s);
    return { list: list.filter((_, i) => !gone.has(i)), cur: Math.max(0, s[0] - 1) };
  }
  // the selected taken out and put back together, in order, at gap (0..n: before what is list[gap] now); null if
  // that changes nothing. at: where the first of them is now
  function moveTo(list, sel, gap) {
    const s = order(sel, list.length); if (!s.length) return null;
    const set = new Set(s), rest = list.filter((_, i) => !set.has(i));
    gap = clamp(gap | 0, 0, list.length);
    const at = gap - s.filter((i) => i < gap).length, out = [...rest.slice(0, at), ...s.map((i) => list[i]), ...rest.slice(at)];
    return out.every((f, i) => f === list[i]) ? null : { list: out, at };
  }
  // each selected frame one place along (d: -1 or 1), past the frame next to it; ones packed against the end stay
  function nudge(list, sel, d) {
    const s = order(sel, list.length), out = list.slice(), on = list.map(() => false); s.forEach((i) => { on[i] = true; });
    let moved = false;
    for (const i of d < 0 ? s : s.slice().reverse()) {
      const j = i + d; if (j < 0 || j >= out.length || on[j]) continue;
      [out[i], out[j]] = [out[j], out[i]]; on[i] = false; on[j] = true; moved = true;
    }
    return moved ? out : null;
  }
  // a frame's new time: set to one time, or made longer or shorter; 20 to 5000 ms, as a frame's own box takes
  const retime = (ms, how, v) => clamp(Math.round(how === 'add' ? ms + v : v), 20, 5000);
  // "4–6", "2, 5, 9–11": frame numbers (from 1), runs joined; long lists cut after a few runs
  function runs(sel, most = 3) {
    const s = order(sel, Infinity), out = [];
    for (let k = 0; k < s.length;) { let j = k; while (j + 1 < s.length && s[j + 1] === s[j] + 1) j++; out.push(j > k ? `${s[k] + 1}–${s[j] + 1}` : `${s[k] + 1}`); k = j + 1; }
    return out.length > most ? `${out.slice(0, most).join(', ')}…` : out.join(', ');
  }
  // what the Matrix will do with these times, in a line or two (empty when they all play exactly as set)
  function timeNote(times) {
    const P = window.PanelTiming, said = [], all = [...new Set(times)].sort((a, b) => a - b);
    if (!P) return '';
    const odd = all.filter((ms) => P.saved(ms) % P.TICK), cut = all.filter((ms) => P.gifMs(P.saved(ms)) !== ms);
    const list = (a) => (a.length > 2 ? `${a.slice(0, 2).join(' ms, ')} ms…` : `${a.join(' and ')} ms`);
    if (cut.length) said.push(`A GIF keeps hundredths of a second, so ${list(cut)} ${cut.length > 1 ? 'are' : 'is'} saved as ${list(cut.map((ms) => P.gifMs(P.saved(ms))))}.`);
    if (odd.length) { const ms = P.gifMs(P.saved(odd[0])), lo = Math.floor(ms / 50) * 50;
      said.push(`The Matrix draws every 50 ms, so ${ms} ms shows as ${lo || 50} or ${lo + 50} ms in turn, right on average${odd.length > 1 ? ' (the same for the other times that aren’t a multiple of 50)' : ''}.`); }
    return said.join(' ');
  }

  // -- the strip ------------------------------------------------------------------------------------------
  // host: { box, cards (the strip's cards, { el }), frames, cur (get and set), snapshot(), renderAll(), renderFrames(),
  //         toast(text), roomFor(n), clone(frame), busy(), openDialog(dlg), timeOne() (optional: Time… with one frame),
//         mini (optional: a phone's strip of small frames, canvases with data-i) }
  function strip(h) {
    let picked = new Set(), anchor = null, mode = false;   // the selected frames; where a Shift range starts; select mode (touch)
    const box = h.box, $ = (id) => document.getElementById(id);
    // what a screen reader is told about a drag (the count of selected frames is #fr-count, itself a live region)
    const live = document.createElement('span'); live.setAttribute('aria-live', 'polite'); live.className = 'fr-live';
    live.style.cssText = 'position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap';
    document.body.append(live);
    const say = (t) => { live.textContent = t; };
    const F = () => h.frames;
    const cardAt = (el) => h.cards.findIndex((k) => k.el === el);
    // the selection's frames as indices, in order (the current frame alone if it somehow isn't among them)
    function indices() {
      const fs = F(), out = [];
      if (picked.size === 1 && picked.has(fs[h.cur])) return [h.cur];
      fs.forEach((f, i) => { if (picked.has(f)) out.push(i); });
      return out.includes(h.cur) ? out : [h.cur];
    }
    const only = (f) => { picked = new Set([f]); anchor = f; };
    // called by renderFrames before it paints: frames that are gone drop out, the current frame is always in
    function sync() {
      const fs = F(), c = fs[h.cur];
      if (picked.size > 1 || !picked.has(c)) { const here = new Set(fs); for (const f of picked) if (!here.has(f)) picked.delete(f); }
      if (c && !picked.has(c)) only(c);
      if (!picked.has(anchor)) anchor = c;
    }
    function restore(ix) { const fs = F(); picked = new Set(order(ix || [], fs.length).map((i) => fs[i])); anchor = fs[h.cur]; sync(); }
    const many = () => picked.size > 1;
    // the extra classes of a frame's card (renderFrames sets the whole className)
    const cls = (f) => (picked.has(f) ? (drag ? ' picked lifting' : ' picked') : '');
    let said = '';
    function after() {   // renderFrames has painted: the count, the buttons' words, select mode
      const ix = indices(), n = F().length;
      box.classList.toggle('multi', many() || mode);
      $('fwrap')?.classList.toggle('s-range', many() || mode);   // the frame header shows the selection's buttons (layout C)
      const text = many() ? `${ix.length} selected (${runs(ix)})` : mode ? 'Tap frames to select them' : '';
      const out = $('fr-count'); if (out && text !== said) { said = text; out.textContent = text; }
      const t = (id, s) => { const b = $(id); if (b && b.title !== s) b.title = s; };
      const which = many() ? `the ${ix.length} selected frames` : `frame ${h.cur + 1}`;
      t('fr-dup', `Duplicate ${which}: the copies go after ${many() ? 'the last of them' : 'it'} (Ctrl+D)`);
      t('fr-rev', many() ? `Reverse the order of the ${ix.length} selected frames` : `Reverse the order of every frame (select some first to reverse just those)`);
      t('fr-time', `Set how long ${which} show${many() ? '' : 's'}, or make ${many() ? 'them' : 'it'} longer or shorter`);
      t('fr-del', n < 2 ? 'An animation needs at least one frame' : `Delete ${which} (Delete key)`);
    }
    const repaint = (curMoved) => (curMoved ? h.renderAll() : h.renderFrames());

    // -- choosing -------------------------------------------------------------------------------------------
    let quietUntil = 0;   // a click that ends a drag or a long press isn't a choice
    function click(i, e) {
      if (performance.now() < quietUntil || drag) return;
      const fs = F(), f = fs[i], was = h.cur; if (!f) return;
      const add = e.ctrlKey || e.metaKey || (mode && !e.shiftKey);
      if (e.shiftKey) {   // a range from where the last plain or Ctrl click was
        const a = fs.indexOf(anchor), r = span(a < 0 ? h.cur : a, i).map((j) => fs[j]);
        if (e.ctrlKey || e.metaKey) r.forEach((g) => picked.add(g)); else picked = new Set(r);
        h.cur = i;
      } else if (add) {
        if (picked.has(f) && picked.size > 1) {   // taken out: if it was the current frame, the nearest selected one is
          picked.delete(f);
          if (h.cur === i) { let best = -1; fs.forEach((g, j) => { if (picked.has(g) && (best < 0 || Math.abs(j - i) < Math.abs(best - i))) best = j; }); h.cur = best; }
        } else if (!picked.has(f)) { picked.add(f); h.cur = i; }
        anchor = f;
      } else { only(f); h.cur = i; }
      repaint(h.cur !== was);   // the board shows the current frame; the cards their ticks
    }
    function selectAll() { const fs = F(); picked = new Set(fs); if (!picked.has(anchor)) anchor = fs[h.cur]; h.renderFrames(); }
    function collapse() { mode = false; only(F()[h.cur]); h.renderFrames(); }
    function extend(d) {   // Shift+arrow: the range from the anchor grows or shrinks by one
      const fs = F(), j = h.cur + d; if (j < 0 || j >= fs.length) return;
      const a = Math.max(0, fs.indexOf(anchor)); picked = new Set(span(a, j).map((k) => fs[k])); h.cur = j; h.renderAll();
    }

    // -- acting on the selection: each one undo step ----------------------------------------------------------
    function place(list, curFrame, pick) {   // the new order in; the current frame and selection follow their frames
      h.frames = list; h.cur = Math.max(0, list.indexOf(curFrame)); if (pick) { picked = new Set(pick); anchor = pick[0]; }
      h.renderAll();
    }
    function dup() {
      const ix = indices(), fs = F(); if (!h.roomFor(ix.length)) return;
      h.snapshot(); const r = duplicate(fs, ix, h.clone), copies = r.picked.map((i) => r.list[i]);
      place(r.list, copies[ix.indexOf(h.cur)], copies);
      h.toast(ix.length > 1 ? `Duplicated ${ix.length} frames: the copies are frames ${runs(r.picked)}` : `Duplicated frame ${ix[0] + 1}`);
    }
    function rev() {
      const ix = indices(), fs = F(); if (fs.length < 2) return h.toast('There is only one frame');
      h.snapshot(); place(reverse(fs, ix), fs[h.cur]);
      h.toast(ix.length > 1 ? `Reversed frames ${runs(ix)}` : `Reversed every frame`);
    }
    function del() {
      const ix = indices(), fs = F(), r = remove(fs, ix);
      if (!r) return h.toast(fs.length < 2 ? 'An animation needs at least one frame' : 'An animation needs at least one frame, so not every frame can go: leave one out of the selection');
      h.snapshot(); place(r.list, r.list[r.cur], [r.list[r.cur]]);
      h.toast(ix.length > 1 ? `Deleted ${ix.length} frames: Undo brings them back` : `Deleted frame ${ix[0] + 1}: Undo brings it back`);
    }
    function moveSel(gap) {   // drag and drop; false if it changes nothing
      const fs = F(), r = moveTo(fs, indices(), gap); if (!r) return false;
      h.snapshot(); place(r.list, fs[h.cur]); return true;
    }
    function nudgeBy(d) {
      const fs = F(), out = nudge(fs, indices(), d); if (!out) return;
      h.snapshot(); place(out, fs[h.cur]);
    }
    // Time…: one time for every selected frame, or each made longer or shorter by the same
    function timeDialog() {
      const dlg = $('fr-timedlg'); if (!dlg) return;
      const ix = indices(), fs = F(), times = ix.map((i) => fs[i].ms);
      $('fr-time-title').textContent = ix.length > 1 ? `Time for ${ix.length} frames (${runs(ix)})` : `Time for frame ${ix[0] + 1}`;
      const all = [...new Set(times)];
      $('fr-time-now').textContent = all.length === 1 ? `Now: ${all[0]} ms${ix.length > 1 ? ' each' : ''}` : `Now: ${Math.min(...times)} to ${Math.max(...times)} ms`;
      $('fr-how').value = 'set'; $('fr-ms').value = String(all.length === 1 ? all[0] : Math.round(times.reduce((a, b) => a + b, 0) / times.length / 50) * 50 || 100);
      timeFields(); h.openDialog(dlg); $('fr-ms').focus(); $('fr-ms').select?.();
    }
    function planned() {   // the new times, or a message
      const how = $('fr-how').value, v = Number($('fr-ms').value), ix = indices(), fs = F();
      if (!Number.isFinite(v) || $('fr-ms').value === '') return { err: 'Type a time in ms' };
      if (how === 'set' && v < 20) return { err: 'A frame shows for at least 20 ms' };
      return { ix, times: ix.map((i) => retime(fs[i].ms, how, v)) };
    }
    function timeFields() {
      const how = $('fr-how').value, ms = $('fr-ms');
      if (how === 'add') { ms.min = -5000; ms.max = 5000; $('fr-ms-l').textContent = 'By'; $('fr-ms-u').textContent = 'ms (less than 0 makes them shorter)'; }
      else { ms.min = 50; ms.max = 5000; $('fr-ms-l').textContent = 'Each shows for'; $('fr-ms-u').textContent = 'ms'; }
      const p = planned(); $('fr-timeerr').textContent = '';
      $('fr-timenote').textContent = p.err ? '' : timeNote(p.times) + (how === 'add' && p.times.some((t, k) => t === 20 || t === 5000) ? ' Times stop at 20 ms and 5000 ms.' : '');
    }
    function timeGo() {
      const p = planned(); if (p.err) { $('fr-timeerr').textContent = p.err; return; }
      const fs = F(); $('fr-timedlg').close();
      if (p.ix.every((i, k) => fs[i].ms === p.times[k])) return h.toast('Those frames already show for that long');
      h.snapshot(); p.ix.forEach((i, k) => { fs[i].ms = p.times[k]; }); h.renderFrames();
      const one = new Set(p.times).size === 1;
      h.toast(`${p.ix.length > 1 ? `Frames ${runs(p.ix)}` : `Frame ${p.ix[0] + 1}`} now show${p.ix.length > 1 ? '' : 's'} for ${one ? `${p.times[0]} ms` : `${Math.min(...p.times)} to ${Math.max(...p.times)} ms`}`);
    }

    // -- drag to a new place --------------------------------------------------------------------------------
    // mouse: press and move. Touch: hold still on a frame for 300 ms (that also starts select mode), then move, so a
    // plain swipe still scrolls the strip. A line shows where they'll go; near either end the strip scrolls; Escape
    // puts them back.
    let press = null, drag = null, mark = null, lift = null;
    const HOLD = 300;
    const minis = () => (h.mini ? [...h.mini.children].filter((c) => c.dataset.i != null) : []);
    function gapAt(x, y) {
      if (drag && drag.mini) {   // the phone's mini strip: the small frame under the finger, its left or right half
        const cs = minis(); if (!cs.length) return drag.gap;
        for (const c of cs) { const r = c.getBoundingClientRect(); if (x < r.right + 1) return x < r.left + r.width / 2 ? Number(c.dataset.i) : Number(c.dataset.i) + 1; }
        return Number(cs[cs.length - 1].dataset.i) + 1;
      }
      const b = box.getBoundingClientRect(), n = F().length;
      const el = document.elementFromPoint(clamp(x, b.left + 2, b.right - 2), clamp(y, b.top + 4, b.bottom - 4));
      const card = el && el.closest && el.closest('.frame');
      if (card && box.contains(card)) { const i = cardAt(card); if (i >= 0) { const r = card.getBoundingClientRect(); return x < r.left + r.width / 2 ? i : i + 1; } }
      if (el && el.closest && el.closest('.add')) return n;
      return drag ? drag.gap : null;   // between two cards: where it was
    }
    function begin() {
      const fs = F(), f = fs[press.i];
      if (!picked.has(f)) { only(f); h.cur = press.i; h.renderAll(); }   // dragging a frame that isn't selected moves just it
      drag = { x: press.x, y: press.y, gap: press.i, since: 0, raf: 0, n: picked.size, touch: !press.mouse, mini: press.mini };
      if (press.mouse) try { box.setPointerCapture(press.id); } catch { /* gone already */ }
      box.classList.add('dragging');
      mark = document.createElement('div'); mark.className = 'fr-drop'; mark.setAttribute('aria-hidden', 'true');
      lift = document.createElement('div'); lift.className = 'fr-lift'; lift.setAttribute('aria-hidden', 'true');
      const from = press.mini ? minis().find((c) => Number(c.dataset.i) === press.i) : h.cards[press.i] && h.cards[press.i].el.querySelector('canvas');
      if (from) { const c = document.createElement('canvas'); c.width = from.width; c.height = from.height; c.getContext('2d').drawImage(from, 0, 0); lift.append(c); }
      if (drag.n > 1) { const b = document.createElement('b'); b.textContent = drag.n; lift.append(b); }
      document.body.append(mark, lift);
      h.renderFrames();   // the lifted cards fade
      say(`Moving ${drag.n > 1 ? `${drag.n} frames` : `frame ${press.i + 1}`}: let go where the line is, or press Escape`);
      drag.raf = requestAnimationFrame(step);
    }
    function step(t) {   // each animation frame while dragging: scroll near the ends, then the line
      if (!drag) return;
      if (drag.mini) { drag.gap = gapAt(drag.x, drag.y); show(); drag.raf = requestAnimationFrame(step); return; }   // (the mini strip shows the frames round this one: it doesn't scroll)
      const b = box.getBoundingClientRect(), edge = Math.min(56, b.width / 4);
      const sideways = box.scrollWidth > box.clientWidth, v = sideways
        ? (drag.x < b.left + edge ? -Math.min(1, (b.left + edge - drag.x) / edge) : drag.x > b.right - edge ? Math.min(1, (drag.x - (b.right - edge)) / edge) : 0)
        : (drag.y < b.top + edge ? -Math.min(1, (b.top + edge - drag.y) / edge) : drag.y > b.bottom - edge ? Math.min(1, (drag.y - (b.bottom - edge)) / edge) : 0);
      if (v) { drag.since ||= t; const speed = 6 + 40 * Math.min(3, (t - drag.since) / 1000); if (sideways) box.scrollLeft += v * speed; else box.scrollTop += v * speed; }
      else drag.since = 0;
      drag.gap = gapAt(drag.x, drag.y); show();
      drag.raf = requestAnimationFrame(step);
    }
    function show() {
      lift.style.transform = drag.touch ? `translate(${drag.x - 36}px, ${drag.y - 90}px)` : `translate(${drag.x + 12}px, ${drag.y - 30}px)`;   // above a finger, beside a pointer
      const n = F().length, g = drag.gap, at = (j) => (drag.mini ? minis().find((c) => Number(c.dataset.i) === j) : h.cards[j] && h.cards[j].el);
      const still = g == null || !moveTo(F(), indices(), g);   // dropped there, nothing would change
      let el = null, after = false;   // the line: before card g, else after the one before it (the last card; in the mini strip, the last shown)
      if (g != null) { el = g < n ? at(g) : null; if (!el) { el = at(g - 1); after = true; } }
      if (!el || still) { mark.style.display = 'none'; return; }
      const r = el.getBoundingClientRect(), b = (drag.mini ? h.mini : box).getBoundingClientRect(), x = after ? r.right + 1 : r.left - (drag.mini ? 2 : 5);
      if (x < b.left - 40 || x > b.right + 40) { mark.style.display = 'none'; return; }   // its card is scrolled away
      Object.assign(mark.style, { display: 'block', left: `${clamp(x, b.left, b.right - 3)}px`, top: `${r.top - 4}px`, height: `${r.height + 8}px` });
    }
    function end(drop) {   // drop: put them where the line is; otherwise back where they were
      const d = drag; cancelAnimationFrame(d.raf); drag = null;
      mark?.remove(); lift?.remove(); mark = lift = null; box.classList.remove('dragging');
      quietUntil = performance.now() + 400;
      const moved = drop && d.gap != null && moveSel(d.gap);
      if (!moved) h.renderFrames();
      say(moved ? `Moved to frame ${h.cur + 1}` : drop ? '' : 'Put back');
      return d;
    }
    function letGo() { if (press) clearTimeout(press.timer); press = null; }
    box.addEventListener('pointerdown', (e) => {
      if (drag || press || (e.pointerType === 'mouse' && e.button !== 0) || h.busy() || document.querySelector('dialog[open]')) return;
      const card = e.target.closest('.frame'); if (!card || e.target.closest('input')) return;
      const i = cardAt(card); if (i < 0) return;
      press = { id: e.pointerId, i, x: e.clientX, y: e.clientY, mouse: e.pointerType === 'mouse', held: false, timer: 0 };
      if (!press.mouse) press.timer = setTimeout(hold, HOLD);
    });
    // the phone's mini strip (by Play): a swipe steps through the frames (js/animator-layout.js); a finger held still on
    // a small frame for 300 ms lifts it (with the others chosen, if it's one of them) to drag to a new place
    h.mini?.addEventListener('pointerdown', (e) => {
      if (drag || press || e.pointerType === 'mouse' || h.busy() || document.querySelector('dialog[open]')) return;
      const c = e.target.closest && e.target.closest('canvas'); if (!c || c.dataset.i == null) return;
      press = { id: e.pointerId, i: Number(c.dataset.i), x: e.clientX, y: e.clientY, mouse: false, mini: true, held: false, timer: setTimeout(hold, HOLD) };
    });
    function hold() {   // a long press: select mode, with this frame selected, ready to be dragged
      if (!press) return;
      press.held = true;
      const fs = F(), f = fs[press.i];
      if (press.mini) {   // the mini strip: no select mode (the Frames tab isn't showing), just ready to drag
        if (!picked.has(f)) only(f); h.cur = press.i; h.renderAll(); navigator.vibrate?.(12);
        return say(`Move your finger to drag ${picked.size > 1 ? `the ${picked.size} chosen frames` : `frame ${press.i + 1}`}`);
      }
      if (!picked.has(f)) { if (mode) picked.add(f); else picked = new Set([f]); anchor = f; h.cur = press.i; }
      mode = true; h.renderAll(); navigator.vibrate?.(12);
      say(`${picked.size} selected: tap frames to add them, or move your finger to drag`);
    }
    window.addEventListener('pointermove', (e) => {
      if (!press || e.pointerId !== press.id) return;
      if (!drag) {
        const far = Math.hypot(e.clientX - press.x, e.clientY - press.y);
        if (!press.mouse && !press.held) { if (far > 10) letGo(); return; }   // a swipe before the hold: the strip scrolls
        if (far < 6) return;
        begin();
      }
      drag.x = e.clientX; drag.y = e.clientY;
    });
    window.addEventListener('pointerup', (e) => {
      if (!press || e.pointerId !== press.id) return;
      if (drag) { drag.x = e.clientX; drag.y = e.clientY; drag.gap = gapAt(drag.x, drag.y); end(true); }
      else if (press.held) quietUntil = performance.now() + 400;   // the long press's own tap isn't a toggle
      letGo();
    });
    window.addEventListener('pointercancel', (e) => { if (press && e.pointerId === press.id) { if (drag) end(false); letGo(); } });
    // once held, the finger moves the frames, not the strip (the first move after a still hold can still be stopped)
    box.addEventListener('touchmove', (e) => { if ((press && press.held) || drag) e.preventDefault(); }, { passive: false });
    box.addEventListener('contextmenu', (e) => { if (press && !press.mouse) e.preventDefault(); });   // a long press isn't a right-click here
    window.addEventListener('keydown', (e) => {   // Escape mid-drag puts them back (before anything else hears the key)
      if (e.key !== 'Escape' || !(drag || press)) return;
      e.preventDefault(); e.stopImmediatePropagation(); if (drag) end(false); letGo();
    }, true);

    // -- keys, when the strip has the keyboard ----------------------------------------------------------------
    box.tabIndex = 0;
    box.addEventListener('keydown', (e) => {
      if (e.target.tagName === 'INPUT' || document.querySelector('dialog[open]') || h.busy()) return;   // a time box's own keys
      const ctrl = e.ctrlKey || e.metaKey, k = (e.key || '').toLowerCase(), arrow = e.key === 'ArrowLeft' ? -1 : e.key === 'ArrowRight' ? 1 : 0;
      if (ctrl && !e.altKey && k === 'a') selectAll();
      else if (ctrl && !e.altKey && !e.shiftKey && k === 'd') dup();
      else if (!ctrl && !e.altKey && (e.key === 'Delete' || e.key === 'Backspace')) del();
      else if (e.altKey && !ctrl && arrow) nudgeBy(arrow);
      else if (e.shiftKey && !ctrl && !e.altKey && arrow) extend(arrow);
      else if (!ctrl && !e.altKey && !e.shiftKey && arrow) { const n = F().length; h.cur = (h.cur + arrow + n) % n; mode = false; only(F()[h.cur]); h.renderAll(); }
      else if (e.key === 'Escape' && (many() || mode)) collapse();
      else return;
      e.preventDefault(); e.stopPropagation();
    });
    const on = (id, fn) => { const b = $(id); if (b) b.addEventListener('click', fn); };
    on('fr-dup', dup); on('fr-rev', rev); on('fr-time', () => (many() || !h.timeOne ? timeDialog() : h.timeOne())); on('fr-del', del); on('fr-none', collapse);   // Time… for one frame: the Timing popover (this frame or every frame)
    on('fr-timego', timeGo); on('fr-time-close', () => $('fr-timedlg').close());
    $('fr-how')?.addEventListener('change', timeFields); $('fr-ms')?.addEventListener('input', timeFields);
    $('fr-ms')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); timeGo(); } });
    $('fr-timedlg')?.addEventListener('click', (e) => { if (e.target === $('fr-timedlg')) $('fr-timedlg').close(); });   // the backdrop
    return { sync, cls, after, indices, restore, click, selectAll, collapse, dup, rev, del, moveSel, nudgeBy, timeDialog,
      pick: (list) => { picked = new Set(list); anchor = list[0]; },
      get mode() { return mode; }, get dragging() { return !!drag; }, get holding() { return !!(drag || (press && press.held)); } };
  }

  window.AnimatorFrames = { order, span, duplicate, reverse, remove, moveTo, nudge, retime, runs, timeNote, strip };
})();
