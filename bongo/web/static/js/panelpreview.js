// Preview on the panel: the editor's switch that puts the unsaved draft on the real panel for a while.
// The hold is the Matrix's (bongo/panelpreview.py): this page renews it every few seconds, so a closed tab,
// a locked phone or a lost network gives the panel back by itself.
import { DEMO, api, post, put } from './api.js';
import { h, icon, toast, toggle } from './ui.js';
import { S, bus, problemOf } from './app.js';

const DEBOUNCE = 500;   // ms after the last change before the draft goes to the panel
const RENEW = 10;       // s between renewals, unless the Matrix says otherwise (it gives up after 30 without one)
const AGAIN = 2000;     // ms before a draft that didn't get through (the network, for a moment) is sent again

const SHOWING = 'The panel is showing your unsaved changes. It goes back to the playlist when you Save, Apply or Cancel, '
  + '30 seconds after you leave this page, or after 5 minutes without a change.';

/** What the notice adds while the Matrix is asleep: a preview is never brighter than the panel is right now. */
function asleepWords() {
  const L = S.state?.live || {};
  if (!(L.asleep || L.sleep_until)) return '';
  const level = L.brightness == null ? 'the sleep brightness' : 'the brightness set on the Now page';
  return L.entry ? ` The Matrix is asleep, so the preview shows at ${level}.`
    : ` The Matrix is asleep with its panel dark: the preview lights it at ${level}, and it goes dark again afterwards.`;
}

/**
 * ``draft()``: the screen as a save would send it. ``index``: its place in the playlist (the Matrix words a
 * problem by it). ``onState(on)``: told when the panel starts or stops showing the draft.
 * Returns { toggle, notices, changed(), stop() }: the switch for the editor's header, the notices to go
 * under it, what to call after every edit and what to call on the way out (Save, Cancel, any other page).
 */
export function panelPreview({ draft, index, onState = () => {} }) {
  let token = null;        // this page's hold on the panel (null: not previewing)
  let wanted = false;      // where the switch was last put (it can be flipped back while the Matrix is still answering)
  let starting = false, sending = false, again = false, left = false;
  let tried = '';          // the last draft sent, good or not: the same one isn't sent twice
  let timer = 0, renewer = 0;

  const words = h('span');
  const banner = h('div', { class: 'preview-banner', role: 'status', hidden: true }, h('span', { class: 'sq', 'aria-hidden': 'true' }), words);
  const note = h('div', { class: 'note preview-note', role: 'status', hidden: true });
  const problems = h('div', { class: 'error-box', role: 'alert', hidden: true });
  const notices = h('div', { class: 'stack-s preview-notices', hidden: true }, banner, note, problems);
  const sw = toggle(false, 'Preview on the panel', (on) => { wanted = on; if (on) start(false); else stop(); });
  sw.classList.add('panel-switch');

  const show = () => { notices.hidden = banner.hidden && note.hidden && problems.hidden; };
  const paintBanner = () => { words.textContent = SHOWING + asleepWords(); };
  const setOn = (on) => { sw.setAttribute('aria-pressed', String(on)); banner.hidden = !on; if (on) paintBanner(); show(); onState(on); };
  function say(text, button) {     // a plain note under the header (someone else has the panel; why a preview ended)
    note.hidden = !text;
    note.replaceChildren(...(text ? [icon('info'), h('span', { class: 'grow' }, text), button].filter(Boolean) : []));
    show();
  }
  function refuse(lead, list) {    // the Matrix's usual words for what a save wouldn't take
    problems.hidden = !list?.length;
    problems.replaceChildren(...(list?.length ? [h('strong', null, lead), h('ul', null, list.map((p) => h('li', null, problemOf(p).text)))] : []));
    show();
  }
  const quiet = () => { clearTimeout(timer); clearTimeout(renewer); timer = renewer = 0; again = false; };

  async function start(takeOver) {
    if (DEMO) { toast('The demo has no panel to preview on.'); setOn(false); return; }
    if (starting) return;
    starting = true;
    sw.setAttribute('aria-disabled', 'true');
    say(''); refuse('', []);
    const body = draft(), text = JSON.stringify(body);
    try {
      const res = await post('/api/panel-preview', { entry: body, index, take_over: takeOver });
      if (left || !wanted) { release(res.token); setOn(false); return; }   // (left, or switched off again, while the Matrix answered)
      token = res.token; tried = text;
      setOn(true);
      plan(res);
      if (JSON.stringify(draft()) !== text) changed();   // (edited meanwhile)
    } catch (err) {
      setOn(false);
      if (left || err.status === 401) return;
      if (err.data?.held) {
        say(err.message, h('button', { type: 'button', class: 'btn small-btn', onclick: () => { wanted = true; start(true); } }, 'Take over'));
      } else if (err.data?.problems) refuse('Fix this first, then preview on the panel:', err.data.problems);
      else say(`Couldn't preview on the panel: ${err.message}`);
    } finally { starting = false; sw.removeAttribute('aria-disabled'); }
  }

  /** After an edit: the draft goes to the panel half a second after the last one. */
  function changed() {
    if (!token) return;
    clearTimeout(timer);
    timer = setTimeout(send, DEBOUNCE);
  }

  async function send() {
    if (!token) return;
    if (sending) { again = true; return; }
    const mine = token, body = draft(), text = JSON.stringify(body);
    if (text === tried) return;
    sending = true;
    try {
      const res = await put('/api/panel-preview', { token: mine, entry: body, index });
      if (token !== mine) return;
      tried = text;
      refuse('', []);
      plan(res);
    } catch (err) {
      if (token !== mine) return;
      if (err.status === 410 || err.status === 403) ended(err);   // (403: not this browser's hold)
      else if (err.status === 422 || err.status === 409) {   // not valid (yet): the panel keeps the last good draft
        tried = text;
        refuse('The panel is still showing your last good draft. The Matrix wouldn\'t take this one:', err.data?.problems || [err.message]);
      } else if (err.status !== 401) { clearTimeout(timer); timer = setTimeout(send, AGAIN); }   // (the renewals say if it's over)
    } finally {
      sending = false;
      if (again && token) { again = false; send(); }
    }
  }

  async function renew() {
    const mine = token;
    if (!mine) return;
    try {
      const res = await post('/api/panel-preview/renew', { token: mine });
      if (token === mine) plan(res);
    } catch (err) {
      if (token !== mine) return;
      if (err.status === 410 || err.status === 403) ended(err);   // (403: not this browser's hold)
      else if (err.status !== 401) plan();   // offline for a moment: keep asking; the Matrix says when it's over
    }
  }
  function plan(res) {
    clearTimeout(renewer);
    if (!token) return;
    let ms = (res?.renew || RENEW) * 1000;
    if (res && res.idle_left * 1000 < ms) ms = res.idle_left * 1000 + 300;   // the five minutes are nearly up: be told at once
    renewer = setTimeout(renew, ms);
  }

  /** The Matrix ended it (taken over, five minutes without a change, out of touch, the screen failed): say why. */
  function ended(err) {
    token = null;
    quiet();
    refuse('', []);
    setOn(false);
    say(err.message);
  }

  const release = (which) => api('DELETE', '/api/panel-preview', { token: which }).catch(() => { /* it lapses by itself */ });

  /** Give the panel back now (the switch, or leaving the editor any way at all). */
  function stop() {
    const mine = token;
    token = null;
    quiet();
    say(''); refuse('', []);
    setOn(false);
    if (mine) release(mine);
  }

  const onPoll = () => { if (token) paintBanner(); };   // asleep or awake since: the notice says how bright it shows
  const onBack = () => { if (token && !document.hidden) renew(); };   // back from a locked phone or another tab: still held?
  bus.addEventListener('state', onPoll);
  document.addEventListener('visibilitychange', onBack);

  return { toggle: sw, notices, changed,
    stop: () => { left = true; bus.removeEventListener('state', onPoll); document.removeEventListener('visibilitychange', onBack); stop(); } };
}
