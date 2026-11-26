// The hub's Update… link: <a Matrix>/settings#update opens Settings at the Software card with the update ready
// to confirm. The link says where to go and nothing else; pairing comes first if this browser hasn't paired.
// Only words and addresses here (no page), so the tests can run it; settings.js draws the card.

export const UPDATE = '#update';
const RUNNING = new Set(['requested', 'fetching', 'installing', 'restarting']);

/** The address to come back to once this browser has paired: the page, and the #part that names a place on it.
 *  (The demo keeps its page after the #, so it has no such part.) */
export function afterPairing(page, hash, demo = false) {
  return page + (!demo && /^#[a-z_-]{1,30}$/.test(hash || '') ? hash : '');
}

/** Did the address ask for the update (the hub's Update… link)? */
export const asked = (hash, demo = false) => !demo && hash === UPDATE;

/** Take #update off the address (Not now, or the update has started), so a reload doesn't ask again. */
export function forget(loc = location, hist = history) {
  if (loc.hash === UPDATE) hist.replaceState(null, '', loc.pathname + loc.search);
}

/** What the Software card asks when it is opened from the hub: null when there is nothing to confirm (it is up to
 *  date, nothing is known yet, or an update is already under way), and the card is then the usual one.
 *  ``info`` is /api/update: the running version and ``known``, what the Matrix's own daily look found. */
export function ready(info, name) {
  const v = info?.version, known = info?.known;
  if (!v?.short || known?.available !== true || typeof known.latest !== 'string' || RUNNING.has(info.status?.state)) return null;
  return { line: `${name || 'This Matrix'} is on ${info.label || 'this version'} (${v.short}). A new version (${known.latest.slice(0, 8)}) is ready.`,
    note: "The panel and this page go quiet for about a minute. If the new version won't start, it goes back to the old one by itself." };
}

/** Update now, pressed on the question. ``post`` and ``get`` are the dashboard's own (api.js). Resolves to
 *    { done: true, mine }      an update is under way (this press started it, or another tab or phone had already:
 *                              mine is false, and ``info`` is its progress): the question is over, and only now is
 *                              #update taken off the address;
 *    { done: false, why }      it didn't start: still asking, with why in words;
 *    { done: false, unpaired } this browser's pairing has run out: api.js is already on its way to the pairing
 *                              page, and #update is still on the address, so pairing comes back to the question. */
export async function updateNow(post, get, forgetIt = forget) {
  try { await post('/api/update'); }
  catch (err) {
    if (err?.status === 401) return { done: false, unpaired: true };
    if (err?.status === 409) {   // already under way (someone else pressed it), or the Matrix is restarting
      let info = null;
      try { info = await get('/api/update'); } catch { /* can't tell: say what the Matrix said */ }
      if (RUNNING.has(info?.status?.state)) { forgetIt(); return { done: true, mine: false, info }; }
    }
    const said = String(err?.message || "The Matrix didn't answer").trim();
    return { done: false, why: `The update didn't start. ${said}${/[.?!]$/.test(said) ? '' : '.'}` };
  }
  forgetIt();
  return { done: true, mine: true };
}

/** Where pairing may send the browser afterwards: a page of this dashboard, with at most a plain #part. Anything
 *  else (another site's address, a script, the pairing page itself) is the Now page. */
export function safeAfter(after) {
  return typeof after === 'string' && /^\/(?!\/)(?!pair(?:[/#?]|$))[a-z0-9/_-]*(#[a-z_-]{1,30})?$/.test(after) ? after : '/';
}
