// ?demo: an in-browser stand-in for the Flask API, so the dashboard can be reviewed without a Pi.
// Frames come from sprite sheets pre-rendered by the real module code (static/demo/index.json, PNG data URIs, 8 frames each).
import { ApiError } from './api.js';

const DEMO_DIR = new URL('../demo/', import.meta.url);
const FRAME_SECONDS = 0.6;
const now = () => Date.now() / 1000;
const iso = (secAgo) => new Date(Date.now() - secAgo * 1000).toISOString().replace(/\.\d+Z$/, '+00:00');
const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
// The Pi reports sleep_until as naive local time, like datetime.isoformat().
const naiveLocal = (d) => new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

export async function start() {
  const [modules, index] = await Promise.all([
    fetch(new URL('./demo-modules.json', import.meta.url)).then((r) => r.json()),
    // Sprite sheets are generated for published demos and aren't in the repo; without them the demo shows no frames.
    fetch(new URL('index.json', DEMO_DIR)).then((r) => r.json()).catch(() => ({ frames: 8, sheets: {} })),
  ]);
  const sheets = {};
  const sheet = (key) => (sheets[key] ||= new Promise((resolve) => {
    if (!index.sheets[key]) { resolve(null); return; }
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => resolve(null);
    img.src = new URL(index.sheets[key], DEMO_DIR).href;
  }));
  Object.keys(index.sheets).forEach(sheet);

  const config = {
    name: 'Living room', timezone: 'Europe/London',
    panel: { rows: 32, cols: 64, hardware_mapping: 'adafruit-hat-pwm', gpio_slowdown: 4, brightness: 80 },
    safe_area: { top: 0, right: 0, bottom: 0, left: 0 },
    web: { enabled: true, host: '0.0.0.0', port: 5000 },
    playlist: [
      { module: 'lava_lamp', style: 'metro', seconds: 30, settings: { palettes: ['magma'] }, enabled: true },
      { module: 'weather', style: 'overview', seconds: 20, settings: {}, enabled: true },
      { module: 'countdown', style: 'flip', seconds: 15, settings: {}, enabled: true },
      { module: 'announcements', style: 'banner', seconds: 20, settings: { items: [{ title: 'Bin day', text: 'Green bin goes out tonight' }] }, enabled: true },
      { module: 'rss', style: 'ticker', seconds: 30, settings: { feeds: ['https://feeds.bbci.co.uk/news/rss.xml'] }, enabled: true },
      { module: 'text', style: 'scroll', seconds: 15, settings: { text: 'Welcome home!' }, enabled: false },
    ],
    splash: { sources: ['built-in/splash_loading_squares.gif'], speed: 100, fit: 'fit', enabled: true },
    sleep: { from: '23:00', to: '07:00', brightness: 15, sources: ['built-in/sleep_default.gif'], speed: 100, fit: 'fit', enabled: true },
  };
  const BUILT_IN = ['splash_loading_squares.gif', 'sleep_default.gif'];
  const uploads = [{ name: 'uploaded/garden-timelapse.mp4', file: 'garden-timelapse.mp4', bytes: 8650000, kind: 'video' }];
  const devices = [
    { id: 'd3m0a1b2', name: 'This browser (demo)', paired: iso(86400 * 41), seen: iso(5), me: true },
    { id: 'f00dcafe', name: "Billy's phone", paired: iso(86400 * 37), seen: iso(3600 * 2) },
    { id: 'beadfeed', name: 'Kitchen tablet', paired: iso(86400 * 99), seen: iso(86400 * 1.2) },
  ];
  let paired = new URLSearchParams(location.search).get('demo') !== 'pair';
  let tries = 5;
  const started = now() - 3600 * 26.4;
  const live = { idx: 0, started: now() - 12.3, paused: false, frozen: 0, brightness: null, sleepUntil: null, queue: [], push: null };
  const logs = seedLogs();

  // -- the pretend player ------------------------------------------------------------------
  const enabled = () => config.playlist.filter((e) => e.enabled !== false);
  const describe = (e) => (e ? { module: e.module, style: e.style ?? null, seconds: e.seconds, settings: e.settings || {} } : null);
  function advance() {
    const list = enabled(), t = now();
    if (live.push && t - live.started >= live.push.seconds) { live.push = null; live.started = t; }
    if (!live.push && live.queue.length) { live.push = live.queue.shift(); live.started = t; }
    if (live.push || live.paused || !list.length) return;
    live.idx %= list.length;
    let guard = 0;
    while (t - live.started >= list[live.idx].seconds && guard++ < 500) {
      live.started += list[live.idx].seconds;
      live.idx = (live.idx + 1) % list.length;
      log('INFO', 'player', `showing ${list[live.idx].module}/${list[live.idx].style || 'default'} for ${list[live.idx].seconds} s`);
    }
  }
  const current = () => { advance(); const list = enabled(); return live.push || (list.length ? list[live.idx % list.length] : null); };
  function status() {
    const list = enabled(), e = current();
    const elapsed = live.paused ? live.frozen : now() - live.started;
    return {
      entry: describe(e), next: describe(live.push ? list[live.idx % list.length] : list[(live.idx + 1) % (list.length || 1)]),
      elapsed: Math.round(elapsed * 10) / 10, paused: live.paused, brightness: live.brightness,
      sleep_until: live.sleepUntil, pushes: live.queue.length,
    };
  }
  function skip() { live.push = null; live.idx = (live.idx + 1) % (enabled().length || 1); live.started = now(); live.frozen = 0; }

  // -- validation like bongo.config.validate ---------------------------------------------------
  function check(e, where) {
    const m = modules.find((x) => x.id === e?.module);
    if (!m) return `${where}: unknown module '${e?.module}'`;
    const style = e.style || m.default_style;
    if (!m.styles[style]) return `${where}: ${m.id}: no style '${style}' (have ${JSON.stringify(Object.keys(m.styles).sort())})`;
    const fields = {};
    [...m.settings, ...m.styles[style].settings].forEach((f) => { fields[f.name] = f; });
    const unknown = Object.keys(e.settings || {}).filter((k) => !fields[k]).sort();
    if (unknown.length) return `${where}: ${m.id}: unknown setting(s) [${unknown.map((k) => `'${k}'`).join(', ')}]`;
    for (const [k, v] of Object.entries(e.settings || {})) {
      const f = fields[k];
      const ok = { bool: typeof v === 'boolean', int: Number.isInteger(v), number: typeof v === 'number', text: typeof v === 'string',
        colour: typeof v === 'string' && /^#[0-9a-fA-F]{6}$/.test(v), list: Array.isArray(v), object: !!v && typeof v === 'object' && !Array.isArray(v),
        choice: Array.isArray(v) ? v.every((x) => (f.choices || []).includes(x)) : (f.choices || []).includes(v), auto: true }[f.type];
      if (ok === false) return `${where}: ${m.id}: ${k}: expected ${f.type === 'choice' ? `one of ${JSON.stringify(f.choices)}` : `a ${f.type}`}, got ${JSON.stringify(v)}`;
    }
    return null;
  }
  function validate(c) {
    const problems = (c.playlist || []).map((e, i) => check(e, `playlist[${i}]`));
    if (c.splash && !(c.splash.sources || []).length) problems.push('splash.sources: pick at least one image or GIF');
    if (c.sleep && !(c.sleep.sources || []).length) problems.push('sleep.sources: pick at least one image or GIF');
    const sa = c.safe_area || {};
    if (64 - (sa.left || 0) - (sa.right || 0) < 1 || 32 - (sa.top || 0) - (sa.bottom || 0) < 1) problems.push('safe_area leaves no visible pixels');
    return problems.filter(Boolean);
  }

  // -- logs ------------------------------------------------------------------------------------
  function log(level, source, message, trace = '', t = now()) {
    logs.push({ t, level, source, message, trace });
    if (logs.length > 500) logs.shift();
  }
  setInterval(() => {
    const r = Math.random();
    if (r < 0.5) log('DEBUG', 'panel', `frame time ${(9 + Math.random() * 4).toFixed(1)} ms`);
    else if (r < 0.8) log('INFO', 'weather', 'Open-Meteo refresh ok · next in 10 min');
    else if (r < 0.95) log('INFO', 'rss', 'fetched 20 headlines from feeds.bbci.co.uk');
    else log('WARNING', 'rss', 'feed slow to respond (4.2 s)');
  }, 7000);

  // -- request handler ---------------------------------------------------------------------------
  async function handle(method, path, body) {
    await wait(40 + Math.random() * 80);
    const url = new URL(path, location.origin), p = url.pathname;
    if (p === '/api/pair/status') return json({ paired });
    if (p === '/api/pair/show') return json({ ok: true, seconds: 120 });
    if (p === '/api/pair') {
      const pin = String(body?.pin || '');
      if (tries <= 0) return json({ error: 'Too many wrong PINs. Pairing is paused for 60 s.' }, 403);
      if (!/^\d{6}$/.test(pin) || pin === '000000') { tries--; return json({ error: `That PIN didn't match. ${tries} tries left.` }, 403); }
      paired = true; devices[0].name = String(body?.name || 'Browser').slice(0, 40);
      log('INFO', 'pairing', `paired ${devices[0].name}`);
      return json({ ok: true });
    }
    if (!paired) return json({ error: 'not paired' }, 401);
    if (p === '/api/state') {
      return json({ live: status(), panel: { width: 64, height: 32 }, safe_area: { ...config.safe_area },
        uptime: Math.round(now() - started), name: config.name });
    }
    if (p === '/api/control') {
      const a = body?.action;
      if (a === 'next') skip();
      else if (a === 'pause') { live.frozen = now() - live.started; live.paused = true; }
      else if (a === 'resume') { live.started = now() - live.frozen; live.paused = false; }
      else if (a === 'sleep') { live.sleepUntil = naiveLocal(new Date(Date.now() + Math.max(1, Number(body.minutes) || 480) * 60000)); }
      else if (a === 'wake') live.sleepUntil = null;
      else if (a === 'brightness') live.brightness = body.value == null ? null : Math.max(1, Math.min(100, Math.round(body.value)));
      else return json({ error: `unknown action '${a}'` }, 400);
      return json({ live: status() });
    }
    if (p === '/api/push') {
      const text = String(body?.text || '').trim().slice(0, 200);
      if (!text) return json({ error: 'nothing to show' }, 400);
      live.queue.push({ module: 'announcements', style: 'banner', seconds: Math.max(3, Math.min(120, Number(body.seconds) || 15)),
        settings: { items: [{ title: String(body.title || '').slice(0, 40), text }] } });
      live.push = null; live.started = now() - 999;  // interrupt now
      log('INFO', 'web', `pushed message “${text.slice(0, 40)}”`);
      return json({ ok: true, live: status() });
    }
    if (p === '/api/modules') return json(modules);
    if (p === '/api/config' && method === 'GET') return json(config);
    if (p === '/api/config' && method === 'PUT') {
      if (!body || typeof body !== 'object' || Array.isArray(body)) return json({ error: 'config must be an object' }, 400);
      const merged = { ...config, ...JSON.parse(JSON.stringify(body)) };
      const problems = validate(merged);
      if (problems.length) return json({ problems }, 422);
      Object.keys(config).forEach((k) => { if (!(k in merged)) delete config[k]; });
      Object.assign(config, merged);
      for (const k of ['splash', 'sleep']) if (config[k] == null) delete config[k];
      log('INFO', 'config', `config saved by ${devices[0].name}`);
      return json({ ok: true, restart_needed: false });
    }
    if (p === '/api/media' && method === 'GET') {
      const used = uploads.reduce((n, f) => n + f.bytes, 0);
      return json({ folders: { 'built-in': BUILT_IN.map((f) => ({ name: `built-in/${f}`, file: f, bytes: 40960, kind: 'image', screen: f.split('_')[0] })), uploaded: uploads },
        used, limits: { file: 52428800, library: 1073741824 }, types: ['.gif', '.jpg', '.mp4', '.png', '.webp'] });
    }
    if (p === '/api/media' && method === 'POST') {
      const f = body?.get?.('file');
      if (!f) return json({ error: 'choose a file to upload' }, 400);
      if (!/\.(png|jpe?g|gif|webp|bmp|mp4|mov|webm|mkv|avi|m4v)$/i.test(f.name)) return json({ error: `Couldn't add ${f.name}: that type isn't supported.` }, 422);
      const clean = f.name.toLowerCase().replace(/[^a-z0-9._-]+/g, '-');
      uploads.push({ name: `uploaded/${clean}`, file: clean, bytes: f.size, kind: /\.(mp4|mov|webm|mkv|avi|m4v)$/i.test(clean) ? 'video' : 'image' });
      return json({ ok: true, name: `uploaded/${clean}` }, 201);
    }
    if (p.startsWith('/api/media/') && method === 'DELETE') {
      const name = decodeURIComponent(p.slice('/api/media/'.length)), k = uploads.findIndex((f) => f.name === name);
      if (k < 0) return json({ error: name.startsWith('built-in/') ? "built-in files can't be deleted" : 'no such file' }, name.startsWith('built-in/') ? 403 : 404);
      uploads.splice(k, 1);
      return json({ ok: true });
    }
    if (p === '/api/places') {  // a few real places so the location search can be tried in the demo
      const q = (url.searchParams.get('q') || '').toLowerCase();
      const all = [['Cheltenham', 'England', 'United Kingdom', 'GB', 51.9, -2.0783], ['Chester', 'England', 'United Kingdom', 'GB', 53.1934, -2.8931],
        ['Chelmsford', 'England', 'United Kingdom', 'GB', 51.7356, 0.4685], ['London', 'England', 'United Kingdom', 'GB', 51.5085, -0.1257],
        ['London', 'Ontario', 'Canada', 'CA', 42.9834, -81.233], ['Bath', 'England', 'United Kingdom', 'GB', 51.3794, -2.3656],
        ['Paris', 'Île-de-France', 'France', 'FR', 48.8534, 2.3488], ['Paris', 'Texas', 'United States', 'US', 33.6609, -95.5555]];
      return json({ places: all.filter(([n]) => n.toLowerCase().startsWith(q)).map(([name, region, country, cc, latitude, longitude]) =>
        ({ label: `${name}, ${region}, ${cc}`, name, region, country, latitude, longitude })) });
    }
    if (p === '/api/devices') return json(devices);
    if (p.startsWith('/api/devices/') && method === 'DELETE') {
      const id = decodeURIComponent(p.split('/').pop()), k = devices.findIndex((d) => d.id === id);
      if (k < 0) return json({ ok: false });
      if (devices[k].me) paired = false;
      devices.splice(k, 1);
      return json({ ok: true });
    }
    if (p === '/api/logs') {
      const since = Number(url.searchParams.get('since') || 0);
      return json(logs.filter((r) => r.t > since));
    }
    return json({ error: 'not found' }, 404);
  }

  // -- frames --------------------------------------------------------------------------------------
  async function frame(key, t, { dim = 1 } = {}) {
    const img = await sheet(key);
    if (!img) return null;
    const c = document.createElement('canvas');
    c.width = 64; c.height = 32;
    const ctx = c.getContext('2d');
    ctx.fillStyle = '#000'; ctx.fillRect(0, 0, 64, 32);
    const k = Math.floor(t / FRAME_SECONDS) % index.frames;
    const sa = config.safe_area;
    ctx.imageSmoothingEnabled = false;
    ctx.globalAlpha = dim;  // the real module would draw into the smaller canvas; scaling is close enough here
    ctx.drawImage(img, k * 64, 0, 64, 32, sa.left, sa.top, 64 - sa.left - sa.right, 32 - sa.top - sa.bottom);
    return c;
  }
  async function image(path) {
    await wait(10);
    const url = new URL(path, location.origin);
    if (!paired) throw new ApiError(401, 'This browser is not paired');
    if (url.pathname === '/api/frame.png') {
      if (live.sleepUntil) return frame('lava_lamp/classic', now(), { dim: 0.18 });
      const e = current();
      if (!e) return null;
      const m = modules.find((x) => x.id === e.module);
      return frame(`${e.module}/${e.style || m?.default_style}`, now(), { dim: live.brightness == null ? 1 : 0.35 + 0.65 * (live.brightness / 100) });
    }
    if (url.pathname === '/api/preview.png') {
      const q = url.searchParams, m = modules.find((x) => x.id === q.get('module'));
      if (!m) throw new ApiError(404, 'unknown module');
      let settings;
      try { settings = JSON.parse(q.get('settings') || '{}'); } catch { throw new ApiError(422, 'settings is not JSON'); }
      const problem = check({ module: m.id, style: q.get('style') || null, settings }, 'preview');
      if (problem) throw new ApiError(422, problem.replace(/^preview: /, ''));
      return frame(`${m.id}/${q.get('style') || m.default_style}`, Number(q.get('t')) || now());
    }
    return null;
  }

  return { fetch: handle, image };
}

function seedLogs() {
  const out = [], t0 = Date.now() / 1000;
  const push = (ago, level, source, message, trace = '') => out.push({ t: t0 - ago, level, source, message, trace });
  for (let s = 3600; s > 0; s -= 37 + ((s * 7) % 23)) {
    const r = (s * 13) % 10;
    if (r < 4) push(s, 'INFO', 'player', ['showing lava_lamp/metro for 30 s', 'showing weather/overview for 20 s', 'showing countdown/flip for 15 s', 'showing rss/ticker for 30 s'][s % 4]);
    else if (r < 7) push(s, 'DEBUG', 'panel', `frame time ${(9 + (s % 5)).toFixed(1)} ms`);
    else if (r < 9) push(s, 'INFO', 'weather', 'Open-Meteo refresh ok · next in 10 min');
    else push(s, 'INFO', 'rss', 'fetched 20 headlines from feeds.bbci.co.uk');
  }
  push(1190, 'WARNING', 'weather', 'Cached conditions are 31 min old');
  push(1150, 'ERROR', 'weather', 'Open-Meteo request failed; showing cached conditions',
    'Traceback (most recent call last):\n  File "bongo/modules/weather/data.py", line 88, in refresh\n    reading = parse(fetch_json(request_url(settings)))\n  File "bongo/modules/weather/data.py", line 41, in fetch_json\n    with urllib.request.urlopen(url, timeout=8) as r:\nTimeoutError: The read operation timed out');
  push(1100, 'ERROR', 'weather', 'Open-Meteo request failed; showing cached conditions',
    'Traceback (most recent call last):\n  File "bongo/modules/weather/data.py", line 88, in refresh\nTimeoutError: The read operation timed out');
  push(1040, 'WARNING', 'weather', 'Cached conditions are 33 min old');
  push(400, 'WARNING', 'rss', 'feed slow to respond (5.1 s)');
  push(120, 'INFO', 'config', "config saved by Billy's phone");
  return out.sort((a, b) => a.t - b.t);
}
