// A module's own settings page (/modules/<id>): for what is the same for every screen of a module that comes with
// the Matrix. The module declares it (``Module.settings_page``: a title and the script that draws it); this file
// only finds the module and hands its script the page, so nothing here knows what any module is for.
import { h, toast } from './ui.js';
import { go, moduleById } from './app.js';

/** A script a page may name: a bare file name (bongo/module.py checks the same on the Matrix). */
export const PAGE_SCRIPT = /^[a-z0-9_-]+\.js$/;
export const validPage = (page) => !!page && typeof page.script === 'string' && PAGE_SCRIPT.test(page.script);

/** Where a module's settings page is. */
export const pageOf = (id) => `/modules/${id}`;

/** The settings page a screen of ``mod`` in ``style`` leads to: its module's own, or the one its style links to
 *  (``page_links``). { mod, page } or null. */
export function pageFor(mod, style) {
  const owner = mod?.settings_page ? mod : moduleById(mod?.page_links?.[style]);
  return owner && validPage(owner.settings_page) ? { mod: owner, page: owner.settings_page } : null;
}

export async function render(view, match) {
  const mod = moduleById(match[1]);
  const page = mod?.settings_page;
  if (!validPage(page)) { toast('That module has no settings page', 'error'); go('/settings', { replace: true }); return; }
  view.classList.add(`${mod.id}-page`);
  try { await (await import(`./${page.script}`)).render(view, mod); }
  catch (err) {
    console.error(err);
    view.replaceChildren(h('div', { class: 'card offline' }, h('h1', { class: 'h2' }, "This page didn't load"),
      h('p', { class: 'muted' }, "The Matrix didn't answer just then. Reload to try again; anything you've saved is safe."),
      h('button', { class: 'btn primary', type: 'button', onclick: () => location.reload() }, 'Reload')));
  }
}
