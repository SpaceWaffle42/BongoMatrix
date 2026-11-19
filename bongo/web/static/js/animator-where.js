// "See it where it's used": the real Matrix screens that draw a built-in sprite, drawn with the drawing as it is now
// (not saved) beside the built-in one. The Matrix does the drawing (POST /api/animator/projects/<name>/where-used,
// bongo/sprite_preview.py); this only asks and shows the pictures in the #whereused dialog.
//
//   await WhereUsed.show({ name, file, api })   // file: the project (Project.encode); resolves when the dialog is closed
(function () {
  const $ = (id) => document.getElementById(id);
  let running = 0;   // a newer ask replaces an older one still being drawn

  function say(text, busy) { const p = $('where-status'); p.textContent = text || ''; p.hidden = !text; $('whereused').setAttribute('aria-busy', busy ? 'true' : 'false'); }
  function figure(label, src, w, h) {
    const f = document.createElement('figure'), cap = document.createElement('figcaption'), img = document.createElement('img');
    cap.textContent = label; img.src = src; img.alt = `${label}: how the screen looks`; img.width = w; img.height = h;
    f.append(cap, img); return f;
  }
  function draw(result) {
    const body = $('where-body'), notes = $('where-notes');
    body.replaceChildren(); notes.replaceChildren();
    for (const s of result.screens) {
      const sec = document.createElement('section'), h = document.createElement('h3'), pair = document.createElement('div');
      h.textContent = `${s.module_name} · ${s.style_name}`; pair.className = 'pair';
      pair.append(figure('Built in', s.built_in, s.width, s.height), figure('Yours', s.yours, s.width, s.height));
      sec.append(h, pair); body.append(sec);
    }
    for (const n of result.notes || []) { const p = document.createElement('p'); p.textContent = n; notes.append(p); }
    notes.hidden = !notes.children.length;
  }

  function show({ name, file, api }) {
    const dlg = $('whereused'), mine = ++running;
    $('where-title').textContent = `Where "${name}" is used`;
    $('where-body').replaceChildren(); $('where-notes').replaceChildren(); $('where-notes').hidden = true;
    say('Asking the Matrix which screens draw this…', true);
    const closed = new Promise((resolve) => { dlg.addEventListener('close', resolve, { once: true }); });
    if (!dlg.open) { if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', ''); }
    $('where-close').focus();
    (async () => {
      try {
        const known = (await api('GET', '/api/animator/projects')).projects.find((p) => p.name === name);
        if (!known || !(known.built_in || known.replaces_built_in)) { if (mine === running) say(`"${name}" isn't one of the Matrix's built-in drawings, so no screen draws it until you give it a built-in's name. (Open… lists the built-in ones.)`); return; }
        const result = await api('POST', `/api/animator/projects/${name}/where-used`, file);
        if (mine !== running) return;
        draw(result);
        say(result.screens.length ? 'Drawn as the Matrix would draw them, at your panel\'s size and safe area. Nothing is saved.' : 'Nothing to show.');
      } catch (e) { if (mine === running) say(e.message); }
    })();
    return closed;
  }
  $('where-close').onclick = () => $('whereused').close();
  $('whereused').addEventListener('click', (e) => { if (e.target === $('whereused')) $('whereused').close(); });   // the backdrop
  window.WhereUsed = { show };
})();
