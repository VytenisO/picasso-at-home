// Writing pad: what you draw with a pen, finger or mouse is drawn on the paper by the live session.
// Point mode streams the points while you write; stroke mode sends each stroke after the pen lifts.
// The pad shows a window of the paper canvas, anchored at its top-left; a narrower window means a bigger pad scale.
import { post } from './api.js';
import { state, on, jobActive } from './store.js';
import { $, el, toast } from './ui.js';

const COORD_DECIMALS = 2;
const FLUSH_MS = 40;          // point mode: batch the points of this long into one request
const MAX_POINTS = 1000;      // per request (the server accepts 2000; this keeps a request small)
const ctx = () => $('padCanvas').getContext('2d');

let ink = [];                 // strokes written on the pad (pad px), only for display
let live = null;              // the stroke being written: {pts: [[mmx, mmy, t]], sent: n}
let t0 = 0;
let chain = Promise.resolve();
let flushTimer = 0;
let sawPen = false;
let region = null;            // {x, y, w, h} in machine mm

export function initPad() {
  $('padOpen').onclick = open;
  $('padClose').onclick = () => { $('padDlg').close(); };
  $('padDlg').addEventListener('close', () => { if (live) { const s = live; live = null; if (s.mode === 'point') send('end', [s.pts[s.pts.length - 1]], s.job); } });
  $('padClear').onclick = () => { ink = []; draw(); };
  $('padWidth').onchange = layout;
  $('padMode').onchange = () => { try { localStorage.setItem('plotter.padMode', $('padMode').value); } catch (e) { /* private mode */ } };
  const c = $('padCanvas');
  c.addEventListener('pointerdown', down);
  c.addEventListener('pointermove', move);
  c.addEventListener('pointerup', up);
  c.addEventListener('pointercancel', up);
  c.addEventListener('contextmenu', (e) => e.preventDefault());
  window.addEventListener('resize', () => { if ($('padDlg').open) layout(); });
  on(['job', 'liveStrokes'], render);
}

function open() {
  const j = state.job;
  if (!j || j.kind !== 'live' || j.state !== 'running') { toast('Start live writing first.', true); return; }
  const cv = state.me.machine.canvas;
  const full = cv[2] - cv[0];
  const widths = [...new Set([full, 200, 150, 100, 60].filter((w) => w <= full + 1e-6))];
  $('padWidth').replaceChildren(...widths.map((w) => el('option', { value: String(w), text: w === full ? `${full.toFixed(0)} (the whole page)` : String(w) })));
  $('padWidth').value = String(widths.includes(100) ? 100 : full);
  try { const m = localStorage.getItem('plotter.padMode'); if (m) $('padMode').value = m; } catch (e) { /* private mode */ }
  ink = [];
  $('padDlg').showModal();
  layout();
}

function layout() {
  const cv = state.me.machine.canvas;
  const W = Number($('padWidth').value) || (cv[2] - cv[0]);
  const H = W * (cv[3] - cv[1]) / (cv[2] - cv[0]);
  region = { x: cv[0], y: cv[1], w: W, h: H };
  const c = $('padCanvas');
  const dlg = $('padDlg').querySelector('.pad-dlg');
  const availW = Math.max(300, dlg.clientWidth - 32);
  const availH = Math.max(200, window.innerHeight - 260);
  const s = Math.min(availW / W, availH / H);
  const cssW = Math.round(W * s), cssH = Math.round(H * s);
  const dpr = window.devicePixelRatio || 1;
  c.style.width = cssW + 'px'; c.style.height = cssH + 'px';
  c.width = Math.round(cssW * dpr); c.height = Math.round(cssH * dpr);
  ctx().setTransform(dpr, 0, 0, dpr, 0, 0);
  ink = [];
  draw();
  render();
}

function toMm(e) {
  const c = $('padCanvas'), r = c.getBoundingClientRect();
  const f = (v) => Math.round(v * 10 ** COORD_DECIMALS) / 10 ** COORD_DECIMALS;
  return [f(region.x + (e.clientX - r.left) / r.width * region.w), f(region.y + (e.clientY - r.top) / r.height * region.h),
    Math.round(e.timeStamp - t0)];
}

function toPx(mmx, mmy) {
  const c = $('padCanvas');
  return [(mmx - region.x) / region.w * c.clientWidth, (mmy - region.y) / region.h * c.clientHeight];
}

function draw() {
  const c = $('padCanvas'), g = ctx();
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  g.clearRect(0, 0, c.clientWidth, c.clientHeight);
  g.fillStyle = css('--paper') || '#fff';
  g.fillRect(0, 0, c.clientWidth, c.clientHeight);
  g.lineCap = 'round'; g.lineJoin = 'round';
  // what the plotter has drawn so far (it lags behind the pad): a light trace
  g.strokeStyle = css('--done') || '#2a7'; g.globalAlpha = 0.45; g.lineWidth = 2;
  for (const s of state.liveStrokes || []) {
    g.beginPath();
    s.pts.forEach(([x, y], i) => { const [px, py] = toPx(x, y); if (i) g.lineTo(px, py); else g.moveTo(px, py); });
    g.stroke();
  }
  g.globalAlpha = 1;
  g.strokeStyle = css('--text') || '#111'; g.lineWidth = 1.6;
  for (const s of ink) {
    g.beginPath();
    s.forEach(([x, y], i) => { const [px, py] = toPx(x, y); if (i) g.lineTo(px, py); else g.moveTo(px, py); });
    if (s.length === 1) { const [px, py] = toPx(s[0][0], s[0][1]); g.lineTo(px + 0.1, py); }
    g.stroke();
  }
}

// ---- sending: one ordered chain, so the server sees the points in the order they were written
function send(phase, pts, jobId) {
  const j = state.job;
  // a stroke belongs to the session it started in: never let it reach a newer one
  if (!j || j.kind !== 'live' || j.id !== jobId || !pts.length) return;
  chain = chain.then(() => post('/api/live/send', { job: jobId, cmd: 'mirror', phase, pts }))
    .catch((e) => { toast(e.message, true); });
}

function flush() {
  flushTimer = 0;
  if (!live || $('padMode').value !== 'point') return;
  const pts = live.pts.slice(live.sent, live.sent + MAX_POINTS);
  if (pts.length) { live.sent += pts.length; send('move', pts, live.job); }
  if (live.sent < live.pts.length) flushTimer = setTimeout(flush, FLUSH_MS);
}

function accept(e) {
  if (e.pointerType === 'pen') { sawPen = true; return true; }
  if (e.pointerType === 'touch') return !(sawPen || $('padPenOnly').checked);
  return e.button === 0;
}

function down(e) {
  if (!accept(e) || live) return;
  const j = state.job;
  if (!j || j.kind !== 'live' || j.state !== 'running') { toast('The live session is not running.', true); return; }
  e.preventDefault();
  $('padCanvas').setPointerCapture(e.pointerId);
  t0 = e.timeStamp;
  const p = toMm(e);
  live = { pts: [p], sent: 0, mode: $('padMode').value, job: j.id, pointer: e.pointerId };
  ink.push([p]);
  if (live.mode === 'point') { live.sent = 1; send('start', [p], live.job); }
  draw();
}

function move(e) {
  if (!live || e.pointerId !== live.pointer) return;      // one pointer at a time: a palm cannot alter a pen stroke
  e.preventDefault();
  const evs = e.getCoalescedEvents ? e.getCoalescedEvents() : [];
  for (const c of (evs.length ? evs : [e])) {
    const p = toMm(c);
    const last = live.pts[live.pts.length - 1];
    if (Math.abs(p[0] - last[0]) < 0.02 && Math.abs(p[1] - last[1]) < 0.02) continue;
    live.pts.push(p);
    ink[ink.length - 1].push(p);
  }
  draw();
  if (live.mode === 'point' && !flushTimer) flushTimer = setTimeout(flush, FLUSH_MS);
}

function up(e) {
  if (!live || e.pointerId !== live.pointer) return;
  const s = live;
  live = null;
  clearTimeout(flushTimer); flushTimer = 0;
  if (s.mode === 'point') {
    // everything not yet sent goes with the end of the stroke (the last point is always included)
    for (let i = s.sent; i < s.pts.length - 1; i += MAX_POINTS) send('move', s.pts.slice(i, Math.min(i + MAX_POINTS, s.pts.length - 1)), s.job);
    send('end', [s.pts[s.pts.length - 1]], s.job);
  } else {
    let pts = s.pts;
    if (pts.length > MAX_POINTS) {          // thin evenly, keeping both ends
      const k = Math.ceil(pts.length / MAX_POINTS);
      pts = pts.filter((_, i) => i % k === 0 || i === pts.length - 1);
    }
    send('stroke', pts, s.job);
  }
}

// ---- status line and closing with the session
function render() {
  const dlg = $('padDlg');
  if (!dlg.open) return;
  const j = state.job;
  if (!j || j.kind !== 'live' || !jobActive()) { dlg.close(); live = null; return; }
  const l = j.live || {};
  $('padInfo').textContent = `${l.strokes || 0} stroke(s) drawn. Waiting: ${l.pending_points || 0} point(s)` +
    (j.meta && j.meta.sport ? '. SPORT MODE: up to 100 mm/s, steps may be lost.' : `. The plotter draws at most ${$('liveSpeed').value} mm/s, so it lags behind a hand.`);
  draw();
}
