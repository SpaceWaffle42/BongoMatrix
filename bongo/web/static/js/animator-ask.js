// In-page questions, so the Animator never uses the browser's own pop-ups (window.prompt / confirm / alert),
// which look alien on a phone and inside the dashboard's iframe, and which some browsers suppress.
//
//   if (await ask({ title: 'Delete "cat"?', text: "This can't be undone.", ok: 'Delete', danger: true })) ...   // true or false
//   const name = await ask.input({ title: 'Rename', value: 'cat', validate: (v) => (v ? '' : 'Type a name') }) // text, or null
//
// Options: title, text (plain text, line breaks kept), ok, cancel, danger (a risky choice: Cancel has the
// focus, so a stray Enter never does it), more (the words of a third button, between Cancel and OK: it answers the text 'more'). Input also takes label, value, placeholder, maxLength and
// validate(value) -> a message to show and stay open, or nothing. Enter does the OK button (never when
// it's a danger one), Escape, Cancel and a click outside cancel. Questions asked together are asked in turn.
// Built on <dialog>; its own small style is added once, from the page's colour variables.
(function () {
  let line = Promise.resolve(), styled = false, seq = 0;
  const CSS = `
dialog.ask { width:min(420px, calc(100vw - 24px)); max-width:none; padding:0; background:var(--card,#1b1b1f); color:var(--text,#eee); border:1px solid var(--line-2,#444); border-radius:0; font:14px var(--ui,system-ui,sans-serif); }
dialog.ask::backdrop { background:rgba(0,0,0,.7); }
dialog.ask .ask-body { padding:16px; display:flex; flex-direction:column; gap:10px; }
dialog.ask h2 { margin:0; font-size:16px; font-weight:650; overflow-wrap:anywhere; }
dialog.ask p { margin:0; color:var(--soft,var(--muted,#bbb)); line-height:1.4; white-space:pre-line; overflow-wrap:anywhere; }
dialog.ask label { color:var(--muted,#aaa); font-size:12px; }
dialog.ask input { background:var(--well,#111); border:1px solid var(--line-2,#444); color:var(--text,#eee); padding:9px 10px; font:14px var(--mono,monospace); border-radius:0; min-height:40px; width:100%; box-sizing:border-box; }
dialog.ask input[aria-invalid=true] { border-color:var(--danger,#e5534b); }
dialog.ask .ask-err { color:var(--danger,#e5534b); font-size:13px; min-height:1.2em; }
dialog.ask .ask-err:empty { display:none; }
dialog.ask .ask-btns { display:flex; gap:8px; justify-content:flex-end; flex-wrap:wrap; }
dialog.ask button { min-height:40px; padding:8px 16px; font:600 14px var(--ui,system-ui,sans-serif); border:1px solid var(--line-2,#444); background:var(--raise,#2a2a30); color:var(--text,#eee); cursor:pointer; border-radius:0; }
dialog.ask button:hover { border-color:var(--muted,#aaa); }
dialog.ask button.ask-ok { background:var(--amber,#e8a33d); border-color:var(--amber,#e8a33d); color:var(--ink,#111); }
dialog.ask button.ask-ok.danger { background:var(--danger,#e5534b); border-color:var(--danger,#e5534b); color:var(--ink,#111); }
dialog.ask button:focus-visible, dialog.ask input:focus-visible { outline:2px solid var(--amber,#e8a33d); outline-offset:2px; }
@media (max-width:480px) { dialog.ask .ask-btns button { flex:1 1 40%; } }`;

  function make(o, input) {
    if (!styled) { const s = document.createElement('style'); s.textContent = CSS; document.head.append(s); styled = true; }
    const id = `ask${++seq}`;
    const dlg = document.createElement('dialog'); dlg.className = 'ask';
    dlg.setAttribute('role', o.danger ? 'alertdialog' : 'dialog'); dlg.setAttribute('aria-labelledby', `${id}t`);
    const body = document.createElement('div'); body.className = 'ask-body';
    const h = document.createElement('h2'); h.id = `${id}t`; h.textContent = o.title || '';
    body.append(h);
    if (o.text) { const p = document.createElement('p'); p.id = `${id}d`; p.textContent = o.text; body.append(p); dlg.setAttribute('aria-describedby', p.id); }
    let field = null, err = null;
    if (input) {
      if (o.label) { const l = document.createElement('label'); l.htmlFor = `${id}i`; l.textContent = o.label; body.append(l); }
      field = document.createElement('input'); field.id = `${id}i`; field.type = 'text'; field.value = o.value ?? ''; field.autocomplete = 'off';
      field.spellcheck = false; field.autocapitalize = 'off';
      if (o.placeholder) field.placeholder = o.placeholder;
      if (o.maxLength) field.maxLength = o.maxLength;
      if (!o.label) field.setAttribute('aria-label', o.title || 'Answer');
      err = document.createElement('div'); err.className = 'ask-err'; err.id = `${id}e`; err.setAttribute('role', 'alert');
      field.setAttribute('aria-describedby', err.id);
      body.append(field, err);
    }
    const no = document.createElement('button'); no.type = 'button'; no.textContent = o.cancel || 'Cancel';
    const yes = document.createElement('button'); yes.type = 'button'; yes.className = `ask-ok${o.danger ? ' danger' : ''}`; yes.textContent = o.ok || 'OK';
    const btns = document.createElement('div'); btns.className = 'ask-btns'; btns.append(no);
    let more = null; if (o.more) { more = document.createElement('button'); more.type = 'button'; more.className = 'ask-more'; more.textContent = o.more; btns.append(more); }
    btns.append(yes);
    body.append(btns); dlg.append(body);
    return { dlg, field, err, no, yes, more };
  }

  function run(o, input) {
    return new Promise((resolve) => {
      const { dlg, field, err, no, yes, more } = make(o, input), before = document.activeElement;
      let done = false;
      const finish = (value) => {
        if (done) return; done = true;
        if (dlg.close && dlg.open) dlg.close();
        dlg.remove();
        if (before && before.isConnected && before.focus) before.focus();
        resolve(value);
      };
      const cancelled = input ? null : false;
      const accept = () => {
        if (!input) return finish(true);
        const text = field.value, bad = o.validate ? o.validate(text) : '';
        if (bad) { err.textContent = bad; field.setAttribute('aria-invalid', 'true'); field.focus(); return; }
        finish(text);
      };
      yes.onclick = accept; no.onclick = () => finish(cancelled);
      if (more) more.onclick = () => finish('more');
      if (field) field.oninput = () => { err.textContent = ''; field.removeAttribute('aria-invalid'); };
      dlg.addEventListener('cancel', (e) => { e.preventDefault(); finish(cancelled); });   // Escape
      dlg.addEventListener('keydown', (e) => {
        e.stopPropagation();   // the editor's own keys stay out of a question
        if (e.key === 'Escape') { e.preventDefault(); finish(cancelled); }
        else if (e.key === 'Enter' && !e.isComposing && e.target.tagName !== 'BUTTON' && (input || !o.danger)) { e.preventDefault(); accept(); }
      });
      dlg.addEventListener('click', (e) => { if (e.target === dlg) finish(cancelled); });   // the backdrop
      document.body.append(dlg);
      if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
      (field || (o.danger ? no : yes)).focus();   // a risky question starts on Cancel
      if (field) field.select();
    });
  }

  const queue = (o, input) => { const p = line.then(() => run(o || {}, input)); line = p.catch(() => {}); return p; };
  window.ask = (o) => queue(o, false);
  window.ask.input = (o) => queue(o, true);
})();
