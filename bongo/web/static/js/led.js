// LED-panel rendering: draws a 64x32 frame as round dots, and loops that keep a canvas fresh.
import { image } from './api.js';

const scratch = document.createElement('canvas');
const sctx = scratch.getContext('2d', { willReadFrequently: true });

/**
 * Draw `img` (64x32 ImageBitmap/Image/canvas, or null for "no signal") on `canvas` as LED dots.
 * safeArea {top,right,bottom,left}: showSafe outlines the visible area (dashed amber),
 * tintHidden washes the masked-off border red so you can see which LEDs go dark.
 */
export function drawLed(canvas, img, { safeArea, showSafe = false, tintHidden = false, panel } = {}) {
  const W = panel?.width || 64, H = panel?.height || 32;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const cssW = canvas.clientWidth;
  if (!cssW) return;  // not laid out (hidden tab); next tick will draw
  const cw = Math.round(cssW * dpr), ch = Math.round(cw * H / W);
  if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
  const ctx = canvas.getContext('2d');
  const p = cw / W, r = Math.max(0.5, p * 0.4), small = p < 5;
  ctx.fillStyle = '#050506';
  ctx.fillRect(0, 0, cw, ch);

  let px = null;
  if (img) {
    if (scratch.width !== W || scratch.height !== H) { scratch.width = W; scratch.height = H; }
    sctx.clearRect(0, 0, W, H);
    sctx.drawImage(img, 0, 0, W, H);
    px = sctx.getImageData(0, 0, W, H).data;
  }
  const dot = (x, y) => {
    if (small) ctx.rect(x * p + p * 0.1, y * p + p * 0.1, p * 0.8, p * 0.8);
    else { ctx.moveTo(x * p + p / 2 + r, y * p + p / 2); ctx.arc(x * p + p / 2, y * p + p / 2, r, 0, Math.PI * 2); }
  };
  // Unlit LEDs in one path, lit ones one by one.
  ctx.beginPath();
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    const i = (y * W + x) * 4;
    if (!px || px[i] + px[i + 1] + px[i + 2] < 10) dot(x, y);
  }
  ctx.fillStyle = '#121317';
  ctx.fill();
  if (px) {
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
      const i = (y * W + x) * 4;
      if (px[i] + px[i + 1] + px[i + 2] < 10) continue;
      ctx.beginPath(); dot(x, y);
      ctx.fillStyle = `rgb(${px[i]},${px[i + 1]},${px[i + 2]})`;
      ctx.fill();
    }
  }
  if (safeArea && (showSafe || tintHidden)) {
    const { top = 0, right = 0, bottom = 0, left = 0 } = safeArea;
    const x0 = left * p, y0 = top * p, w = (W - left - right) * p, hgt = (H - top - bottom) * p;
    if (tintHidden && (top || right || bottom || left)) {
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, 0, cw, ch);
      ctx.rect(x0, y0, w, hgt);
      ctx.fillStyle = 'rgba(255, 70, 85, 0.38)';
      ctx.fill('evenodd');
      ctx.restore();
    }
    const lw = Math.max(1.5, p * 0.18);
    ctx.save();
    ctx.setLineDash([p * 0.9, p * 0.6]);
    ctx.lineWidth = lw;
    ctx.strokeStyle = '#FFB547';
    ctx.strokeRect(x0 + lw / 2, y0 + lw / 2, w - lw, hgt - lw);
    ctx.restore();
  }
}

/**
 * Repeatedly fetches a frame and draws it. Only one request is in flight at a time, it stops while
 * the canvas is off screen or the tab is hidden, and `interval: Infinity` draws once.
 * opts: path() -> url, interval ms, led() -> drawLed options, onError(err|null)
 */
export class FrameLoop {
  constructor(canvas, opts) {
    this.canvas = canvas; this.o = opts; this.visible = true; this.timer = 0; this.alive = true; this.last = null;
    this.failures = 0;
    if ('IntersectionObserver' in window) {
      this.io = new IntersectionObserver(([e]) => { this.visible = e.isIntersecting; if (this.visible) this.kick(); });
      this.io.observe(canvas);
    }
    this.onVis = () => this.kick();
    document.addEventListener('visibilitychange', this.onVis);
    this.kick();
  }
  kick() {
    if (!this.alive || this.busy || this.timer) return;
    this.timer = setTimeout(() => this.tick(), 0);
  }
  async tick() {
    this.timer = 0;
    if (!this.alive || document.hidden || !this.visible || !this.canvas.isConnected) {
      if (!this.canvas.isConnected && this.drawn) this.stop();
      return;
    }
    this.busy = true;
    const started = performance.now();
    let wait = this.o.interval;
    try {
      const img = await image(this.o.path());
      if (!this.alive) return;
      this.last?.close?.();
      this.last = img;
      this.redraw();
      this.failures = 0;
      this.o.onError?.(null);
    } catch (err) {
      this.failures++;
      this.o.onError?.(err);
      if (err?.status === 401) { this.stop(); return; }
      wait = Math.min(10000, this.o.interval * 2 ** Math.min(this.failures, 5));
    } finally {
      this.busy = false;
    }
    if (this.again) { this.again = false; wait = 0; }
    this.drawn = true;
    if (Number.isFinite(wait) && this.alive) {
      this.timer = setTimeout(() => this.tick(), Math.max(16, wait - (performance.now() - started)));
    }
  }
  /** Draw the last frame again (e.g. the safe-area toggle changed). */
  redraw() { drawLed(this.canvas, this.last, this.o.led?.() || {}); }
  /** Fetch again now (e.g. settings changed while drawing once). */
  refresh() { if (this.busy) { this.again = true; return; } clearTimeout(this.timer); this.timer = 0; this.kick(); }
  stop() {
    this.alive = false;
    clearTimeout(this.timer);
    this.io?.disconnect();
    document.removeEventListener('visibilitychange', this.onVis);
    this.last?.close?.();
  }
}
