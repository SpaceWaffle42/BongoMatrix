// Talks to the Flask API, or to the in-browser fake when the page URL has ?demo.

// ?demo, or a published copy of the demo that sets window.BONGO_DEMO before loading
export const DEMO = window.BONGO_DEMO === true || new URLSearchParams(location.search).has('demo');
let demoBackend;
const demo = () => (demoBackend ||= import('./demo.js').then((m) => m.start()));

export class ApiError extends Error {
  constructor(status, message, data) { super(message); this.status = status; this.data = data; }
}

/** Set by app.js: called whenever any request comes back 401. */
export const hooks = { unauthorized() {} };

/** ms a request may take before it's given up. One that never answered (a half-open Wi-Fi connection, a stalled
 *  Pi) used to wait for ever: the status poll stopped for good with the dot still green, and Apply said nothing. */
export const limits = { read: 15000, change: 45000, upload: 15 * 60000 };

function timed(path, init, ms) {
  const stop = new AbortController();
  const timer = setTimeout(() => stop.abort(), ms);
  return fetch(path, { ...init, signal: stop.signal }).finally(() => clearTimeout(timer));
}
const SLOW = "The Matrix didn't answer in time. Check it's on and on this network, then try again.";

function send(method, path, body) {
  if (DEMO) return demo().then((d) => d.fetch(method, path, body));
  const init = { method, credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } };
  let ms = limits.read;
  if (body instanceof FormData) {  // file upload: multipart, marked with the header the server checks
    init.headers['X-Bongo-Upload'] = '1';
    init.body = body;
    ms = limits.upload;
  } else if (method !== 'GET') {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body ?? {});
    ms = limits.change;            // (a save may check a new feed, which takes seconds)
  }
  return timed(path, init, ms);
}

export async function api(method, path, body) {
  let res;
  try { res = await send(method, path, body); }
  catch (err) { throw new ApiError(0, err?.name === 'AbortError' ? SLOW : "Can't reach the Matrix. Is it switched on and on this network?"); }
  let data = null;
  try { data = await res.json(); } catch { /* empty or non-JSON body */ }
  if (res.status === 401 && !path.startsWith('/api/pair')) {
    hooks.unauthorized();
    throw new ApiError(401, 'This browser is not paired');
  }
  if (!res.ok) {
    const msg = data?.error || (data?.problems || []).join(' · ') || `${res.status} ${res.statusText || 'error'}`;
    throw new ApiError(res.status, msg, data);
  }
  return data;
}
export const get = (path) => api('GET', path);
export const post = (path, body) => api('POST', path, body);
export const put = (path, body) => api('PUT', path, body);
export const del = (path) => api('DELETE', path, {});
/** Add a file to the media library (uploaded/). Resolves to { name: 'uploaded/…' }. */
export const uploadMedia = (file) => { const f = new FormData(); f.append('file', file, file.name); return api('POST', '/api/media', f); };

/** A 64x32 frame as something drawImage() accepts, or null if there is none yet (404). */
export async function image(path) {
  if (DEMO) return (await demo()).image(path);
  let res;
  try { res = await timed(path, { credentials: 'same-origin', cache: 'no-store' }, limits.read); }
  catch { throw new ApiError(0, 'Offline'); }
  if (res.status === 401) { hooks.unauthorized(); throw new ApiError(401, 'This browser is not paired'); }
  if (res.status === 404) return null;
  if (!res.ok) {
    let data = null;
    try { data = await res.json(); } catch { /* not JSON */ }
    throw new ApiError(res.status, data?.error || `${res.status}`, data);
  }
  const blob = await res.blob();
  if (window.createImageBitmap) return createImageBitmap(blob);
  return new Promise((resolve, reject) => {
    const img = new Image(), url = URL.createObjectURL(blob);
    img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new ApiError(0, 'Bad image')); };
    img.src = url;
  });
}

export function previewPath(entry, t = Date.now() / 1000) {
  const q = new URLSearchParams({ module: entry.module, style: entry.style || '',
    settings: JSON.stringify(entry.settings || {}), t: t.toFixed(3) });
  return `/api/preview.png?${q}`;
}
