// The rows of the Open dialog (window.OpenList): a picture of each saved drawing, a filter, and Rename,
// Duplicate and Delete on your own (Duplicate on the built-in ones too). Plain DOM; what each button does is the page's business (hooks).
//
// The pictures are small PNGs the Matrix draws (GET /api/animator/projects/<name>/thumb.png?v=<rev>, the
// drawing's own size, its first frame with something in it, on its own background). Only the rows on
// screen ask for theirs, and a folded section's rows never do, so opening the list costs the list and a
// screenful of pictures, not a hundred drawings. The page scales each up by a whole number, crisp.
window.OpenList = (function () {
  const BOX_W = 96, BOX_H = 48;   // the picture's box (css .project .thumb): 8 x 8 icons up to 128 x 64 scenes

  // the picture's size on screen: the biggest whole-number multiple that fits the box; too big for that
  // (a 128 x 64 scene), a whole-number fraction, so every pixel is the same size or none is drawn twice
  function fit(w, h, bw = BOX_W, bh = BOX_H) {
    const up = Math.floor(Math.min(bw / w, bh / h));
    if (up >= 1) return { w: w * up, h: h * up };
    let k = 2; while (w / k > bw || h / k > bh) k++;
    return { w: w / k, h: h / k };
  }

  // typing "icon cal" finds icon_calendar_birthday: every word has to be in the name (or the text), any order
  const norm = (s) => String(s).toLowerCase().replace(/[_\-\s]+/g, ' ').trim();
  const matches = (text, query) => { const t = norm(text); return norm(query).split(' ').filter(Boolean).every((w) => t.includes(w)); };

  // the list in its groups: yours, the built-in ones, and the two big sets of built-in icons, folded away
  const isIcon = (p) => p.built_in && p.name.startsWith('icon_'), isWeather = (p) => p.built_in && p.name.startsWith('weather_');
  const isTree = (p) => p.built_in && p.name.startsWith('tree_');
  const GROUPS = [
    { title: 'Yours', pick: (p) => !p.built_in, mine: true },
    { title: 'Built in', pick: (p) => p.built_in && !isIcon(p) && !isWeather(p) && !isTree(p) },
    { title: 'Built in: icons for Calendar and Countdown', pick: isIcon, how: 'Change one, keep it 8 × 8 and save it under the same name: the Matrix shows yours (its first frame).' },
    { title: 'Built in: weather icons', pick: isWeather, how: 'One drawing for each size a screen uses. Change one, keep its size and save it under the same name: the Matrix shows yours, with its frames and their timings.' },
    { title: 'Built in: Seasonal tree clock', pick: isTree, how: 'The two trees and their visitors. A tree keeps its layers by name (Wood, Leaves in light-to-shade greens, Blossom, Fruit, and the hidden Perch where the robin stands): change one and save it under the same name, and the clock shows yours.' },
  ];

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };

  // draws the list into `body` (replacing what's there). o: { query, opened: Set of fold titles left open,
  // root: what scrolls, url(p): the picture's address, hooks: { open, rename, duplicate, remove }(p) }.
  // Returns { count } (rows shown), and keeps `o.opened` up to date as folds are opened and closed.
  function build(body, list, o) {
    body.replaceChildren();
    const q = norm(o.query || ''), seen = q ? (p) => matches(p.name, q) : () => true;
    const loader = 'IntersectionObserver' in window ? new IntersectionObserver((es) => es.forEach((e) => {
      if (!e.isIntersecting) return;
      loader.unobserve(e.target); e.target.src = e.target.dataset.src;
    }), { root: o.root || null, rootMargin: '150px' }) : null;   // a bit early, so a picture is there as its row scrolls in
    let count = 0;
    for (const g of GROUPS) {
      const all = list.filter(g.pick), some = all.filter(seen);
      if (!all.length || (q && !some.length)) continue;
      count += some.length;
      const rows = el('div', 'projects'), head = `${g.title} · ${q ? `${some.length} of ${all.length}` : all.length}`;
      if (g.how) {
        const fold = el('details'), sum = el('summary', 'fold'), h = el('h3', '', head);
        sum.append(h); fold.dataset.title = g.title;
        fold.open = q ? true : o.opened.has(g.title);   // a filter opens what it found; clearing it puts the folds back
        fold.addEventListener('toggle', () => { if (!q) o.opened[fold.open ? 'add' : 'delete'](g.title); });
        fold.append(sum, el('p', 'muted', g.how), rows); body.append(fold);
      } else { body.append(el('h3', '', head), rows); }
      for (const p of some) rows.append(row(p, g, o, loader));
    }
    return { count };
  }

  function row(p, g, o, loader) {
    const r = el('div', 'project'), pick = el('button', 'pick'), hooks = o.hooks;
    r.dataset.name = p.name; pick.type = 'button';
    const thumb = el('span', 'thumb'), img = new Image(), size = fit(p.width, p.height);
    img.alt = ''; img.width = size.w; img.height = size.h; img.dataset.src = o.url(p); img.decoding = 'async';
    img.onerror = () => { img.style.visibility = 'hidden'; };   // a drawing that can't be drawn: an empty box, still openable
    thumb.append(img);
    if (loader) loader.observe(img); else img.src = img.dataset.src;
    const text = el('span', 'txt'), note = [`${p.width} × ${p.height}`];
    if (p.replaces_built_in) note.push('replaces the built-in');
    text.append(el('b', '', p.name), el('span', 'about', note.join(' · ')));
    pick.append(thumb, text);
    pick.title = p.replaces_built_in ? `Your version of the built-in "${p.name}": the Matrix draws yours wherever a screen uses that one` : '';
    pick.onclick = () => hooks.open(p);
    r.append(pick);
    {   // yours: Rename, Duplicate and Delete; a built-in one: Duplicate, which makes a copy that's yours (council R7)
      const acts = el('div', 'acts');
      for (const [label, fn, danger] of g.mine ? [['Rename', hooks.rename], ['Duplicate', hooks.duplicate], ['Delete', hooks.remove, true]] : [['Duplicate', hooks.duplicate]]) {
        const b = el('button', 'btn', label); b.type = 'button'; b.setAttribute('aria-label', `${label} ${p.name}`);
        if (danger) b.style.color = 'var(--danger)';
        b.onclick = () => fn(p); acts.append(b);
      }
      r.append(acts);
    }
    return r;
  }

  // a filter for a list of cards (the Presets gallery): hides what doesn't match, and a heading with nothing under it
  function filterCards(root, query, card, text, heading) {
    let shown = 0;
    root.querySelectorAll(card).forEach((c) => { const hit = matches(text(c), query); c.hidden = !hit; if (hit) shown++; });
    root.querySelectorAll(heading).forEach((h) => { h.hidden = !h.nextElementSibling || ![...h.nextElementSibling.querySelectorAll(card)].some((c) => !c.hidden); h.nextElementSibling.hidden = h.hidden; });
    return shown;
  }

  return { fit, matches, build, filterCards, GROUPS, BOX_W, BOX_H };
})();
