// The bed view: bed, paper, inset canvas, start point S, the objects on the bed (select, move, resize, rotate),
// plotted progress and the carriage. Everything is in machine millimetres; the view maps mm to screen pixels.
//
// While the bed is being edited each object is drawn from its drawing's own strokes through its transform, so a
// drag is instant. Once a preview is fresh the planner's exact strokes are drawn instead (fills, simplification).
import { state, set, on, jobActive, prefs } from './store.js';
import { $, el, toast, askText } from './ui.js';
import { drawInk, inkMode, setInkMode, hiddenLayers } from './ink.js';
import * as G from './geom.js';
import * as D from './doc.js';
import { post } from './api.js';

let cv, ctx, W = 0, H = 0, dpr = 1;
const view = { s: 2, ox: 20, oy: 20, fitted: false };
let drag = null;            // {mode: pan|move|scale|rotate|marquee, ...}
let draft = null;           // the objects while dragging
const pointers = new Map();
let pinch = null;
let done = { key: null, n: 0, path: null };  // plotted strokes of the job preview
let colors = {};
let raf = 0;
let hover = null;
const HANDLE = 9, ROT_PX = 26;
let playing = 0;
let bedClick = () => false;        // the Machine tab's "click to go"
let placeHook = null;              // one click on the bed, for the text tool

export function armPlace(fn) { placeHook = fn; cv.style.cursor = fn ? 'crosshair' : ''; }
window.__bedToMm = (px, py) => toMm(px, py);      // for dropping library drawings onto the bed
let textEditHook = null;
export function setTextEdit(fn) { textEditHook = fn; }

// Space held down (with no job to pause): a drag pans the bed.
let spaceHeld = false;
window.addEventListener('keydown', (e) => {
  const t = e.target;
  if (e.key !== ' ' || e.repeat || (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT'))) return;
  if (state.job && ['running', 'paused'].includes(state.job.state)) return;
  if (t === cv || (t && t.closest && t.closest('#bedPane'))) { spaceHeld = true; if (cv) cv.style.cursor = 'grab'; }
});
window.addEventListener('keyup', (e) => { if (e.key === ' ' && spaceHeld) { spaceHeld = false; if (cv) cv.style.cursor = ''; } });

// Marching dashes: the selection outline moves while something is selected.
let dashPhase = 0;
setInterval(() => {
  if (!cv || !state.sel.length || document.visibilityState !== 'visible' || state.ws !== 'plot' || drag) return;
  dashPhase = (dashPhase + 1) % 14;
  redraw();
}, 140);

// Line by clicks: each click adds a point (0.5 mm grid, Shift: 45 degree steps); clicking the first point closes,
// a double-click or Enter finishes, Esc cancels, Backspace drops the last point. done(pts, closed), or done(null).
let polyHook = null, polyPts = [];
export function armPoly(fn) {
  polyHook = fn; polyPts = [];
  cv.style.cursor = fn ? 'crosshair' : '';
  if (fn) cv.focus();
  redraw();
}
// Freehand: one drag draws one line, smoothed (Douglas-Peucker, 0.3 mm) when the button comes up.
let freeHook = null, freePts = null;
export function armFree(fn) { freeHook = fn; freePts = null; cv.style.cursor = fn ? 'crosshair' : ''; }
function rdp(pts, tol) {
  if (pts.length < 3) return pts;
  const [a, b] = [pts[0], pts[pts.length - 1]];
  const L = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1e-9;
  let best = -1, bd = 0;
  for (let i = 1; i < pts.length - 1; i++) {
    const d = Math.abs((b[0] - a[0]) * (a[1] - pts[i][1]) - (a[0] - pts[i][0]) * (b[1] - a[1])) / L;
    if (d > bd) { bd = d; best = i; }
  }
  if (bd <= tol) return [a, b];
  return [...rdp(pts.slice(0, best + 1), tol).slice(0, -1), ...rdp(pts.slice(best), tol)];
}

function polyPoint(mx, my, e) {
  let [x, y] = [snapHalf(mx), snapHalf(my)];
  const last = polyPts[polyPts.length - 1];
  if (last && e && e.shiftKey) {
    const dx = mx - last[0], dy = my - last[1];
    const a = Math.round(Math.atan2(dy, dx) / (Math.PI / 4)) * (Math.PI / 4), L = Math.hypot(dx, dy);
    x = G.r2(last[0] + L * Math.cos(a)); y = G.r2(last[1] + L * Math.sin(a));
  }
  return [x, y];
}
function polyFinish(closed) {
  const f = polyHook, pts = polyPts;
  polyHook = null; polyPts = []; cv.style.cursor = '';
  redraw();
  if (!f) return;
  if (pts.length < 2) { f(null); return; }
  f(pts, closed);
}

export function setBedClick(fn, jog) { bedClick = fn; if (jog) jogHook = jog; }
let jogHook = () => false;
let sub = { pv: null, k: -1, obj: null };    // the first k strokes of the plan, for the scrubber

export function initBed() {
  cv = $('bed');
  ctx = cv.getContext('2d');
  new ResizeObserver(resize).observe(cv);
  cv.addEventListener('pointerdown', down);
  cv.addEventListener('pointermove', move);
  cv.addEventListener('pointerup', up);
  cv.addEventListener('pointercancel', up);
  cv.addEventListener('wheel', wheel, { passive: false });
  cv.addEventListener('keydown', key);
  cv.addEventListener('dblclick', (e) => {
    if (polyHook && polyPts.length >= 2) { polyFinish(false); return; }
    if (!editable() || placeHook || freeHook) return;
    const [px, py] = local(e);
    const o = hitObject(px, py);
    if (o && textEditHook) textEditHook(o);
  });
  cv.addEventListener('contextmenu', (e) => e.preventDefault());
  cv.addEventListener('pointerleave', () => { pointerMm = null; status(); redraw(); });
  document.addEventListener('pointerdown', (e) => { if (menu && !menu.contains(e.target)) closeMenu(); }, true);
  document.addEventListener('keydown', (e) => { if (menu && e.key === 'Escape') { closeMenu(); e.stopPropagation(); } }, true);
  window.addEventListener('blur', closeMenu);
  on(['sel', 'doc'], status);
  $('zoomIn').onclick = () => zoomBy(1.25);
  $('zoomOut').onclick = () => zoomBy(0.8);
  $('zoomFit').onclick = fit;
  initViewMenu();
  const inkBtn = $('inkBtn');
  const showInk = () => { inkBtn.setAttribute('aria-pressed', String(inkMode())); inkBtn.textContent = inkMode() ? 'Ink' : 'Lines'; };
  inkBtn.onclick = () => { setInkMode(!inkMode()); showInk(); redraw(); };
  showInk();
  on(['me', 'doc', 'sel', 'naturals', 'preview', 'previewStale', 'job', 'jobPreview', 'pos', 'liveStrokes', 'scrub'], redraw);
  initScrub();
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { colors = {}; redraw(); });
}

export function themeChanged() { colors = {}; redraw(); }

function css(name) {
  if (!colors[name]) colors[name] = getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#888';
  return colors[name];
}

function resize() {
  const r = cv.getBoundingClientRect();
  dpr = window.devicePixelRatio || 1;
  W = r.width; H = r.height;
  cv.width = Math.max(1, Math.round(W * dpr)); cv.height = Math.max(1, Math.round(H * dpr));
  if (!view.user) fit(); else redraw();      // keep the operator's own zoom; otherwise refit
}

export function fit() {
  const m = state.me && state.me.machine;
  if (!m || !W) return;
  const pad = 24;
  view.s = Math.min((W - RULER - 2 * pad) / m.bed[0], (H - RULER - 2 * pad) / m.bed[1]);
  view.ox = RULER + (W - RULER - m.bed[0] * view.s) / 2;
  view.oy = RULER + (H - RULER - m.bed[1] * view.s) / 2;
  view.fitted = true;
  view.user = false;
  redraw();
}

function zoomBy(f, cx = W / 2, cy = H / 2) {
  const s2 = Math.max(0.3, Math.min(80, view.s * f));
  view.ox = cx - (cx - view.ox) * (s2 / view.s);
  view.oy = cy - (cy - view.oy) * (s2 / view.s);
  view.s = s2;
  view.user = true;
  redraw();
}
export const zoomIn = () => zoomBy(1.25), zoomOut = () => zoomBy(0.8);

const toMm = (px, py) => [(px - view.ox) / view.s, (py - view.oy) / view.s];
const toPx = (x, y) => [view.ox + x * view.s, view.oy + y * view.s];

export function redraw() {
  if (!raf) raf = requestAnimationFrame(() => { raf = 0; render(); });
}

function donePath(jp, n) {
  const key = jp.plan_id;
  if (done.key !== key || n < done.n) done = { key, n: 0, path: new Path2D() };
  for (let k = done.n; k < Math.min(n, jp.order.length); k++) {
    const s = jp.strokes[jp.order[k]];
    if (s) s.pts.forEach(([x, y], i) => (i ? done.path.lineTo(x, y) : done.path.moveTo(x, y)));
  }
  done.n = Math.max(done.n, Math.min(n, jp.order.length));
  return done.path;
}

const objects = () => draft || (state.doc ? state.doc.objects : []);
const editable = () => !jobActive() && !!state.doc;

function render() {
  if (!ctx || !state.me) return;
  const m = state.me.machine;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  ctx.setTransform(dpr * view.s, 0, 0, dpr * view.s, dpr * view.ox, dpr * view.oy);
  const px = 1 / view.s;
  // bed with the grid (View); a grid finer than 4 px on screen is skipped
  ctx.fillStyle = css('--bed');
  ctx.fillRect(0, 0, m.bed[0], m.bed[1]);
  const gs = viewPrefs().grid;
  if (gs > 0 && gs * view.s >= 4) {
    ctx.strokeStyle = css('--grid'); ctx.lineWidth = px;
    ctx.beginPath();
    for (let x = gs; x < m.bed[0]; x += gs) { ctx.moveTo(x, 0); ctx.lineTo(x, m.bed[1]); }
    for (let y = gs; y < m.bed[1]; y += gs) { ctx.moveTo(0, y); ctx.lineTo(m.bed[0], y); }
    ctx.stroke();
  }
  const { paper, canvas } = G.canvasRect();
  ctx.fillStyle = css('--paper');
  ctx.fillRect(paper[0], paper[1], paper[2], paper[3]);
  ctx.strokeStyle = css('--line'); ctx.lineWidth = px;
  ctx.strokeRect(paper[0], paper[1], paper[2], paper[3]);
  ctx.setLineDash([4 * px, 3 * px]);
  ctx.strokeStyle = css('--canvas-line');
  ctx.strokeRect(canvas[0], canvas[1], canvas[2] - canvas[0], canvas[3] - canvas[1]);
  ctx.setLineDash([]);

  const jp = state.jobPreview, job = state.job;
  const showJob = jp && job && job.kind === 'plot' && job.meta?.plan_id === jp.plan_id && jobActive();
  const lw = Math.max(0.3, 1.2 * px);
  ctx.lineCap = 'round'; ctx.lineJoin = 'round';
  const pens = state.me.pens || {};
  const layers = state.doc && state.doc.settings.layers;
  const hide = hiddenLayers();
  const liveOn = job && job.kind === 'live' && jobActive();
  if (showJob) {
    drawInk(ctx, jp, { px, layers: jp.settings && jp.settings.layers, pens, css, alpha: 0.28 });
    const n = job.progress ? job.progress.done : 0;
    ctx.strokeStyle = css('--done'); ctx.lineWidth = lw * 1.3;
    ctx.stroke(donePath(jp, n));
  } else if (!liveOn && state.doc) {
    const pv = state.preview;
    if (pv && pv.strokes && pv.doc && !state.previewStale && !draft) {
      const order = pv.order || [];
      const k = state.scrub === null ? order.length : state.scrub;
      if (state.scrub !== null || (pv.summary && pv.summary.start_at > 0)) {
        // the plan so far, over a faint whole (skipped strokes of a resume stay faint)
        drawInk(ctx, pv, { px, layers, pens, css, alpha: 0.16, hide });
        drawInk(ctx, subset(pv, k), { px, layers, pens, css, hide });
      } else drawInk(ctx, pv, { px, layers, pens, css, hide });
      if (travelOn() || state.scrub !== null) drawTravel(pv, k, px);
      if (viewPrefs().dir) drawDirection(pv, k, px, hide);
    } else {
      for (const o of objects()) drawObject(o, { px, layers, pens, hide });
    }
    if (editable()) overlays(px);
  }
  if (state.doc) drawGuides(px);
  if (freeHook && freePts && freePts.length > 1) {
    ctx.strokeStyle = css('--accent'); ctx.lineWidth = 1.5 * px;
    ctx.beginPath(); freePts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y))); ctx.stroke();
  }
  if (polyHook && polyPts.length) {        // the line being clicked, with a rubber band to the pointer
    ctx.strokeStyle = css('--accent'); ctx.lineWidth = 1.5 * px;
    ctx.beginPath();
    polyPts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    if (pointerMm) ctx.lineTo(...polyPoint(pointerMm[0], pointerMm[1], lastPointerEvent));
    ctx.stroke();
    ctx.fillStyle = css('--accent');
    for (const [x, y] of polyPts) ctx.fillRect(x - 2.5 * px, y - 2.5 * px, 5 * px, 5 * px);
  }

  // start point S
  const [sx, sy] = m.start;
  ctx.strokeStyle = css('--ok'); ctx.lineWidth = 2 * px;
  ctx.beginPath(); ctx.moveTo(sx, sy + 12 * px); ctx.lineTo(sx, sy); ctx.lineTo(sx + 12 * px, sy); ctx.stroke();
  ctx.fillStyle = css('--ok'); ctx.font = `bold ${12 * px}px system-ui, sans-serif`;
  ctx.fillText('S', sx + 4 * px, sy + 14 * px);

  if (job && job.kind === 'live' && state.liveStrokes && state.liveStrokes.length && (jobActive() || !state.doc?.objects.length)) {
    ctx.strokeStyle = css('--done'); ctx.lineWidth = lw; ctx.globalAlpha = 1;
    for (const s of state.liveStrokes) {
      ctx.beginPath();
      s.pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
      ctx.stroke();
    }
  }
  // carriage (commanded position)
  const pos = state.pos || (job && jobActive() ? job.pos : null);
  if (pos && pos.x !== null && pos.x !== undefined) {
    const r = 6 * px;
    ctx.beginPath(); ctx.arc(pos.x, pos.y, r, 0, Math.PI * 2);
    ctx.lineWidth = 2 * px; ctx.strokeStyle = css('--err'); ctx.stroke();
    if (pos.pen === 'down') { ctx.fillStyle = css('--err'); ctx.fill(); }
    ctx.beginPath(); ctx.moveTo(pos.x - 2 * r, pos.y); ctx.lineTo(pos.x + 2 * r, pos.y);
    ctx.moveTo(pos.x, pos.y - 2 * r); ctx.lineTo(pos.x, pos.y + 2 * r); ctx.lineWidth = px; ctx.stroke();
  }
  drawRulers();
}

// ------------------------------------------------------------------ plan scrubber and travel moves
const travelOn = () => prefs('travel') === true;

export function toggleTravel() {
  prefs('travel', !travelOn());
  $('travelBtn').setAttribute('aria-pressed', String(travelOn()));
  redraw();
}

function subset(pv, k) {
  if (sub.pv !== pv || sub.k !== k) {
    sub = { pv, k, obj: { strokes: pv.order.slice(0, k).map((i) => pv.strokes[i]).filter(Boolean), summary: pv.summary } };
  }
  return sub.obj;
}

// Pen-up moves of the first k strokes: from S, between strokes, and back to S once the plan is complete.
function drawTravel(pv, k, px) {
  const order = pv.order || [], start = (pv.summary && pv.summary.start_mm) || state.me.machine.start;
  let cur = start;
  ctx.beginPath();
  for (let n = 0; n < k; n++) {
    const s = pv.strokes[order[n]];
    if (!s) continue;
    ctx.moveTo(cur[0], cur[1]); ctx.lineTo(s.pts[0][0], s.pts[0][1]);
    cur = s.pts[s.pts.length - 1];
  }
  if (k >= order.length) { ctx.moveTo(cur[0], cur[1]); ctx.lineTo(start[0], start[1]); }
  ctx.save();
  ctx.strokeStyle = css('--warn'); ctx.lineWidth = Math.max(0.15, px); ctx.globalAlpha = 0.8;
  ctx.setLineDash([3 * px, 2.5 * px]);
  ctx.stroke();
  ctx.restore();
  if (state.scrub !== null && k > 0) {        // where the pen is after stroke k
    ctx.beginPath(); ctx.arc(cur[0], cur[1], 4 * px, 0, Math.PI * 2);
    ctx.fillStyle = css('--accent'); ctx.fill();
  }
}

function initScrub() {
  const range = $('scrubRange');
  $('travelBtn').onclick = toggleTravel;
  $('travelBtn').setAttribute('aria-pressed', String(travelOn()));
  range.oninput = () => { stopPlay(); const k = Number(range.value); set({ scrub: k >= Number(range.max) ? null : k }); };
  $('playBtn').onclick = () => (playing ? stopPlay() : play());
  $('scrubStart').onclick = () => {
    if (state.scrub) { const k = (state.startAt || 0) + state.scrub; stopPlay(); D.setStartAt(k); }
  };
  on('preview', () => { stopPlay(); if (state.scrub !== null) set({ scrub: null }); });
  on(['preview', 'previewStale', 'job', 'doc', 'scrub', 'startAt'], renderScrub);
}

function renderScrub() {
  const pv = state.preview;
  const ok = !!(pv && pv.order && pv.doc && !state.previewStale && !jobActive() && state.doc && state.doc.objects.length);
  $('scrubBar').hidden = !ok;
  if (!ok) return;
  const n = pv.order.length, k = state.scrub === null ? n : state.scrub;
  const range = $('scrubRange');
  range.max = String(n);
  range.value = String(k);
  const s0 = state.startAt || 0;
  $('scrubLabel').textContent = s0 ? `${s0 + k} of ${s0 + n} (from ${s0 + 1})` : `${k} of ${n} strokes`;
  $('scrubStart').disabled = !k || k >= n;
}

function play() {
  const pv = state.preview;
  if (!pv || !pv.order) return;
  const n = pv.order.length;
  let acc = state.scrub === null || state.scrub >= n ? 0 : state.scrub;
  // the whole plan in about 12 s (at least 15 strokes/s), or a multiple of the estimated real plotting time
  const sp = $('playSpeed').value, est = (pv.summary && pv.summary.est_seconds) || 0;
  const step = sp === 'fit' || !est ? Math.max(n / (12 * 60), 0.25) : n / est * Number(sp) / 60;
  $('playBtn').textContent = 'Pause';
  const tick = () => {
    acc += step;
    const k = Math.min(n, Math.floor(acc));
    set({ scrub: k >= n ? null : k });
    if (k >= n) { stopPlay(); return; }
    playing = requestAnimationFrame(tick);
  };
  playing = requestAnimationFrame(tick);
}

function stopPlay() {
  if (playing) cancelAnimationFrame(playing);
  playing = 0;
  $('playBtn').textContent = 'Play';
}

// ------------------------------------------------------------------ rulers, guides, snapping, menu, status
const RULER = 18;                  // px, top and left
let snapLines = [];                // alignment hints while dragging: [{axis, at}]
let pointerMm = null;

const guides = () => (state.doc && state.doc.settings.guides) || [];

function rulerSteps() {
  const s = view.s;
  const label = [5, 10, 20, 50, 100, 200].find((v) => v * s >= 42) || 200;
  const minor = [1, 2, 5, 10, 20, 50].find((v) => v * s >= 5 && label % v === 0) || label;
  return { minor, label };
}

function drawRulers() {
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const { minor, label } = rulerSteps();
  ctx.fillStyle = css('--panel');
  ctx.fillRect(0, 0, W, RULER); ctx.fillRect(0, 0, RULER, H);
  const sel = state.doc ? G.bounds(objects().filter((o) => state.sel.includes(o.id))) : null;
  if (sel) {                         // the selection's extent on both rulers
    ctx.fillStyle = css('--info-bg');
    const [ax, ay] = toPx(sel.x0, sel.y0), [bx, by] = toPx(sel.x1, sel.y1);
    ctx.fillRect(Math.max(RULER, ax), 0, bx - Math.max(RULER, ax), RULER);
    ctx.fillRect(0, Math.max(RULER, ay), RULER, by - Math.max(RULER, ay));
  }
  ctx.strokeStyle = css('--muted'); ctx.fillStyle = css('--muted'); ctx.lineWidth = 1;
  ctx.font = '10px system-ui, sans-serif';
  ctx.beginPath();
  const [mx0] = toMm(RULER, 0), [mx1] = toMm(W, 0);
  for (let v = Math.ceil(mx0 / minor) * minor; v <= mx1; v += minor) {
    const x = Math.round(toPx(v, 0)[0]) + 0.5;
    const big = v % label === 0;
    ctx.moveTo(x, RULER); ctx.lineTo(x, RULER - (big ? 9 : 4));
    if (big) ctx.fillText(String(v), x + 2, 9);
  }
  const [, my0] = toMm(0, RULER), [, my1] = toMm(0, H);
  ctx.save();
  for (let v = Math.ceil(my0 / minor) * minor; v <= my1; v += minor) {
    const y = Math.round(toPx(0, v)[1]) + 0.5;
    const big = v % label === 0;
    ctx.moveTo(RULER, y); ctx.lineTo(RULER - (big ? 9 : 4), y);
    if (big) {
      ctx.save(); ctx.translate(9, y + 2); ctx.rotate(-Math.PI / 2); ctx.fillText(String(v), 0, 0); ctx.restore();
    }
  }
  ctx.stroke();
  ctx.restore();
  if (pointerMm) {                   // where the pointer is
    ctx.strokeStyle = css('--accent'); ctx.beginPath();
    const [px, py] = toPx(...pointerMm);
    ctx.moveTo(px + 0.5, 0); ctx.lineTo(px + 0.5, RULER); ctx.moveTo(0, py + 0.5); ctx.lineTo(RULER, py + 0.5); ctx.stroke();
  }
  ctx.fillStyle = css('--panel'); ctx.fillRect(0, 0, RULER, RULER);
  ctx.strokeStyle = css('--line'); ctx.beginPath();
  ctx.moveTo(0, RULER + 0.5); ctx.lineTo(W, RULER + 0.5); ctx.moveTo(RULER + 0.5, 0); ctx.lineTo(RULER + 0.5, H); ctx.stroke();
}

// Guides (never plotted) and the alignment hints of the drag in progress, in mm.
function drawGuides(px) {
  const m = state.me.machine;
  ctx.save();
  ctx.lineWidth = px;
  ctx.strokeStyle = css('--guide'); ctx.setLineDash([6 * px, 4 * px]);
  ctx.beginPath();
  const gl = drag && (drag.mode === 'guide' || drag.mode === 'guide-new') && drag.at !== null
    ? guides().map((g, i) => (i === drag.index ? { ...g, at: drag.at } : g)).concat(drag.mode === 'guide-new' ? [{ axis: drag.axis, at: drag.at }] : [])
    : guides();
  for (const g of gl) {
    if (g.axis === 'x') { ctx.moveTo(g.at, -1000); ctx.lineTo(g.at, m.bed[1] + 1000); }
    else { ctx.moveTo(-1000, g.at); ctx.lineTo(m.bed[0] + 1000, g.at); }
  }
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.strokeStyle = css('--snap');
  ctx.beginPath();
  for (const s of snapLines) {
    if (s.axis === 'x') { ctx.moveTo(s.at, -1000); ctx.lineTo(s.at, m.bed[1] + 1000); }
    else { ctx.moveTo(-1000, s.at); ctx.lineTo(m.bed[0] + 1000, s.at); }
  }
  ctx.stroke();
  ctx.restore();
}

// Lines a dragged selection snaps to: canvas and paper edges and centres, guides, and the other objects' edges
// and centres.
function snapTargets(exclude) {
  const { paper, canvas: c } = G.canvasRect();
  const xs = [c[0], (c[0] + c[2]) / 2, c[2], paper[0], paper[0] + paper[2]];
  const ys = [c[1], (c[1] + c[3]) / 2, c[3], paper[1], paper[1] + paper[3]];
  for (const g of guides()) (g.axis === 'x' ? xs : ys).push(g.at);
  for (const o of state.doc.objects) {
    if (exclude.includes(o.id)) continue;
    const b = G.bounds([o]);
    xs.push(b.x0, (b.x0 + b.x1) / 2, b.x1);
    ys.push(b.y0, (b.y0 + b.y1) / 2, b.y1);
  }
  return { xs, ys };
}

// The best snap of any of the candidate positions to any target within tol: -> [shift, target] or null.
function bestSnap(cands, targets, tol) {
  let best = null;
  for (const c of cands) for (const t of targets) {
    const d = t - c;
    if (Math.abs(d) <= tol && (!best || Math.abs(d) < Math.abs(best[0]))) best = [d, t];
  }
  return best;
}

function hitGuide(px, py) {
  if (px < RULER || py < RULER || viewPrefs().lockGuides) return -1;
  const gl = guides();
  for (let i = gl.length - 1; i >= 0; i--) {
    const g = gl[i], [gx, gy] = toPx(g.at, g.at);
    if (g.axis === 'x' ? Math.abs(gx - px) <= 4 : Math.abs(gy - py) <= 4) return i;
  }
  return -1;
}

function setGuides(list) {
  D.edit((d) => { d.settings.guides = list.map((g) => ({ axis: g.axis, at: G.r2(g.at) })); });
}

export function zoomTo(b, pad = 40) {
  if (!b || !W) return;
  const w = Math.max(b.x1 - b.x0, 5), h = Math.max(b.y1 - b.y0, 5);
  view.s = Math.max(0.3, Math.min(80, Math.min((W - RULER - 2 * pad) / w, (H - RULER - 2 * pad) / h)));
  view.ox = RULER + (W - RULER) / 2 - (b.x0 + w / 2) * view.s;
  view.oy = RULER + (H - RULER) / 2 - (b.y0 + h / 2) * view.s;
  view.user = true;
  redraw();
}

export function zoomSelection() {
  const sel = state.doc ? state.doc.objects.filter((o) => state.sel.includes(o.id)) : [];
  if (sel.length) zoomTo(G.bounds(sel));
}

function status() {
  const sel = state.doc ? state.doc.objects.filter((o) => state.sel.includes(o.id)) : [];
  const parts = [];
  if (pointerMm) parts.push(`${G.fmtLen(pointerMm[0], false)}, ${G.fmtLen(pointerMm[1])}`);
  if (sel.length) {
    const b = G.bounds(sel);
    parts.push(`${sel.length > 1 ? sel.length + ' selected: ' : ''}${G.fmtLen(b.x0, false)}, ${G.fmtLen(b.y0, false)}  ${G.fmtLen(b.w, false)} x ${G.fmtLen(b.h)}`);
  }
  parts.push(`${Math.round(view.s / ((state.me && state.me.machine) ? 96 / 25.4 : 1) * 100)}%`);
  $('bedCoords').textContent = parts.join('   ');
}

// ---- context menu
let menu = null;

// The start point for the closed shapes of the selected object: kept as fractions of its box, so it moves with it.
function setSeam(mm) {
  const o = D.selected()[0];
  if (!o) return;
  if (mm && (mm[0] < o.x || mm[0] > o.x + o.w || mm[1] < o.y || mm[1] > o.y + o.h)) {
    toast('Right-click inside the object, where its outlines should start.', true); return;
  }
  D.editSel((objs) => {
    if (mm) objs[0].seam = [(mm[0] - o.x) / o.w, (mm[1] - o.y) / o.h];
    else delete objs[0].seam;
  }, { allowLocked: true });
  toast(mm ? 'Closed outlines of this object start at their point nearest here.' : 'Start point cleared.');
}

function closeMenu() { if (menu) { menu.remove(); menu = null; } }

function openMenu(clientX, clientY, mm) {
  closeMenu();
  const sel = state.sel.length > 0;
  const gi = hitGuide(...toPx(...mm));
  const item = (text, fn, ok = true, key = '') => el('button', { type: 'button', role: 'menuitem', disabled: !ok,
    onclick: () => { closeMenu(); fn(); } }, el('span', { text }), key ? el('kbd', { text: key }) : null);
  const sep = () => el('hr');
  const smaller = async () => {
    const t = await askText('Select small objects', 'Select every object smaller than this both ways (mm):', '5', 'Select');
    if (t !== null && Number(t) > 0) toast(`${D.selectWhere('small', Number(t))} selected.`);
  };
  const tail = [sep(), item('Invert the selection', () => D.invertSel(), true, 'Ctrl+I'),
    item('Select objects with open lines', () => toast(`${D.selectWhere('open')} selected.`)),
    item('Select small objects...', smaller), item('Paste in place', () => D.pasteInPlace(), !!prefs('clipboard'), 'Ctrl+Shift+V'),
    sel ? item('Save the selection as clip art...', async () => {
      const name = await askText('Clip art', 'A name: it goes into the Library (kind clip art); drag it onto the bed to use it.', 'clip art', 'Save');
      if (!name) return;
      try {
        const r = await post('/api/clipart', { objects: D.selected(), name });
        const { refreshFiles } = await import('./workspace.js');
        await refreshFiles();
        toast(`${r.file.name} saved.`);
      } catch (e) { toast(e.message, true); }
    }) : null];
  const items = sel ? [
    item('Duplicate', () => D.duplicateSel(), true, 'Ctrl+D'), item('Copy', () => D.copySel(), true, 'Ctrl+C'),
    item('Cut', () => D.cutSel(), true, 'Ctrl+X'), item('Paste', () => D.paste(), !!prefs('clipboard'), 'Ctrl+V'),
    item('Remove from the bed', () => D.removeSel(), true, 'Del'), sep(),
    item('Turn left', () => D.rotateSel(-90)), item('Turn right', () => D.rotateSel(90)),
    item('Mirror H', () => D.mirrorSel('x')), item('Mirror V', () => D.mirrorSel('y')), sep(),
    item('Plot earlier', () => D.reorder('down')), item('Plot later', () => D.reorder('up')), sep(),
    item('Fit canvas', () => D.fitCanvas()), item('Zoom to selection', zoomSelection, true, 'Z'),
    item('Start closed shapes here', () => setSeam(mm), state.sel.length === 1),
    item('Clear the start point', () => setSeam(null), state.sel.length === 1 && !!(D.selected()[0] || {}).seam), ...tail,
  ] : [
    item('Paste', () => D.paste(), !!prefs('clipboard'), 'Ctrl+V'), item('Select all', () => D.selectAll(), true, 'Ctrl+A'), sep(),
    item('Vertical guide here', () => setGuides([...guides(), { axis: 'x', at: mm[0] }])),
    item('Horizontal guide here', () => setGuides([...guides(), { axis: 'y', at: mm[1] }])),
    gi >= 0 ? item('Remove this guide', () => setGuides(guides().filter((_, i) => i !== gi))) : null,
    item('Remove all guides', () => setGuides([]), guides().length > 0), sep(),
    item('Fit the bed', fit, true, '0'), ...tail,
  ];
  menu = el('div', { class: 'ctx-menu', role: 'menu' }, ...items.filter(Boolean));
  document.body.append(menu);
  const r = menu.getBoundingClientRect();
  menu.style.left = `${Math.min(clientX, innerWidth - r.width - 4)}px`;
  menu.style.top = `${Math.min(clientY, innerHeight - r.height - 4)}px`;
  menu.querySelector('button:not(:disabled)')?.focus();
}

function drawObject(o, { px, layers, pens, hide }) {
  const n = state.naturals[o.file];
  if (!n) {           // geometry still loading: the box only
    ctx.setLineDash([2 * px, 2 * px]); ctx.strokeStyle = css('--muted'); ctx.lineWidth = px;
    poly(G.corners(o)); ctx.stroke(); ctx.setLineDash([]);
    return;
  }
  const mt = G.matrix(o, n.geom);
  const k = Math.sqrt(Math.abs(mt[0] * mt[3] - mt[1] * mt[2])) || 1;
  const only = state.doc.settings.only;
  const out = Array.isArray(only) && only.length && only.some((id) => state.doc.objects.some((x) => x.id === id)) && !only.includes(o.id);
  ctx.save();
  ctx.transform(...mt);
  drawInk(ctx, o.ink ? inked(n, o.ink) : n, { px: px / k, layers, pens, css, hide,
    alpha: (state.previewStale || draft ? 0.75 : 1) * (out ? 0.4 : 1), lineScale: k });
  ctx.restore();
}

// The drawing with every layer in one colour (an object moved to another layer). Kept per colour, so the ink
// renderer's path cache holds.
const inkedCache = new WeakMap();
function inked(n, ink) {
  let m = inkedCache.get(n);
  if (!m) { m = new Map(); inkedCache.set(n, m); }
  if (!m.has(ink)) {
    m.set(ink, { ...n, layers: (n.layers || []).map((l) => ({ ...l, color: ink, key: `${ink}|${String(l.key || '').split('|')[1] || ''}` })) });
  }
  return m.get(ink);
}

function poly(pts) {
  ctx.beginPath();
  pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.closePath();
}

// Boxes, selection handles, size label, marquee.
function overlays(px) {
  const objs = objects();
  const sel = objs.filter((o) => state.sel.includes(o.id));
  for (const o of objs) {
    const isSel = state.sel.includes(o.id), bad = G.outside(o);
    if (!isSel && !bad && hover !== o.id) continue;
    ctx.strokeStyle = bad ? css('--err') : css('--accent');
    ctx.lineWidth = (isSel ? 1.5 : 1) * px;
    ctx.setLineDash(isSel ? [4 * px, 3 * px] : [5 * px, 4 * px]);
    ctx.lineDashOffset = isSel ? -dashPhase * px : 0;
    poly(G.corners(o)); ctx.stroke();
    ctx.setLineDash([]); ctx.lineDashOffset = 0;
  }
  // groups: a dotted box around each group that is selected or under the pointer
  const groups = new Set(objs.filter((o) => o.grp && (state.sel.includes(o.id) || hover === o.id)).map((o) => o.grp));
  for (const g of groups) {
    const gb = G.bounds(objs.filter((o) => o.grp === g));
    ctx.strokeStyle = css('--accent'); ctx.lineWidth = px; ctx.setLineDash([2 * px, 3 * px]);
    ctx.strokeRect(gb.x0 - 3 * px, gb.y0 - 3 * px, gb.w + 6 * px, gb.h + 6 * px); ctx.setLineDash([]);
  }
  if (sel.length) {
    const b = G.bounds(sel), bad = sel.some(G.outside);
    ctx.fillStyle = bad ? css('--err') : css('--accent');
    if (sel.length > 1) {
      ctx.setLineDash([6 * px, 4 * px]); ctx.strokeStyle = ctx.fillStyle; ctx.lineWidth = px;
      ctx.strokeRect(b.x0, b.y0, b.w, b.h); ctx.setLineDash([]);
    }
    for (const h of handles(sel)) {
      const s = (h.kind === 'rotate' ? 10 : 8) * px;
      if (h.kind === 'rotate') {
        ctx.beginPath(); ctx.moveTo(...h.from); ctx.lineTo(h.x, h.y); ctx.strokeStyle = ctx.fillStyle; ctx.lineWidth = px; ctx.stroke();
        ctx.beginPath(); ctx.arc(h.x, h.y, s / 2, 0, Math.PI * 2); ctx.fill();
      } else ctx.fillRect(h.x - s / 2, h.y - s / 2, s, s);
    }
    ctx.font = `${12 * px}px system-ui, sans-serif`;
    const one = sel.length === 1 ? sel[0] : null;
    const label = (one
      ? `${G.fmtLen(one.w, false)} x ${G.fmtLen(one.h)}${one.rot ? `, ${G.r2(one.rot)} deg` : ''}`
      : `${sel.length} objects, ${G.fmtLen(b.w, false)} x ${G.fmtLen(b.h)}`) + (sel.some((o) => o.locked) ? '  (locked)' : '');
    ctx.fillText(label + (bad ? '  outside the canvas' : ''), b.x0, b.y0 - (one && !one.rot ? 5 : 5 + ROT_PX) * px);
  }
  if (drag && drag.mode === 'marquee' && drag.rect) {
    const r = drag.rect;
    ctx.fillStyle = css('--accent'); ctx.globalAlpha = 0.08;
    ctx.fillRect(r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0); ctx.globalAlpha = 1;
    ctx.strokeStyle = css('--accent'); ctx.lineWidth = px;
    ctx.setLineDash(r.touch ? [4 * px, 3 * px] : []);
    ctx.strokeRect(r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0); ctx.setLineDash([]);
  }
}

// Handles of the selection in mm. One object: 8 on its own (rotated) box plus a rotate handle. Several: the 4
// corners of their bounds (uniform scale).
function handles(sel) {
  const out = [];
  if (sel.some((o) => o.locked)) return out;       // locked: no resize or turn
  if (sel.length === 1) {
    const o = sel[0];
    for (const [hx, hy] of [[-1, -1], [1, -1], [1, 1], [-1, 1], [0, -1], [1, 0], [0, 1], [-1, 0]]) {
      const [x, y] = G.fromLocal(o, hx * o.w / 2, hy * o.h / 2);
      out.push({ kind: 'scale', hx, hy, x, y });
    }
    const [x, y] = G.fromLocal(o, 0, -o.h / 2 - ROT_PX / view.s);
    out.push({ kind: 'rotate', x, y, from: G.fromLocal(o, 0, -o.h / 2) });
  } else {
    const b = G.bounds(sel);
    for (const [hx, hy] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) {
      out.push({ kind: 'scale', hx, hy, x: b.x0 + (hx + 1) / 2 * b.w, y: b.y0 + (hy + 1) / 2 * b.h, group: true });
    }
  }
  return out;
}

function hitHandle(px, py) {
  const sel = objects().filter((o) => state.sel.includes(o.id));
  if (!sel.length) return null;
  for (const h of handles(sel)) {
    const [hx, hy] = toPx(h.x, h.y);
    if (Math.abs(hx - px) <= HANDLE && Math.abs(hy - py) <= HANDLE) return h;
  }
  return null;
}

function hitObject(px, py) {
  const [x, y] = toMm(px, py);
  const objs = objects();
  for (let i = objs.length - 1; i >= 0; i--) if (G.contains(objs[i], x, y, 4 / view.s)) return objs[i];
  return null;
}

function cursorFor(h) {
  if (!h) return '';
  if (h.kind === 'rotate') return 'grab';
  const o = state.doc.objects.find((x) => x.id === state.sel[0]);
  let ang = Math.atan2(h.hy, h.hx) * 180 / Math.PI + (h.group ? 0 : (o ? o.rot : 0));
  ang = ((ang % 180) + 180) % 180;
  return ['ew-resize', 'nwse-resize', 'ns-resize', 'nesw-resize'][Math.round(ang / 45) % 4];
}

function local(e) { const r = cv.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }

function down(e) {
  cv.focus();
  cv.setPointerCapture(e.pointerId);
  const [px, py] = local(e);
  pointers.set(e.pointerId, [px, py]);
  if (pointers.size === 2) {
    const [a, b] = [...pointers.values()];
    pinch = { d: Math.hypot(a[0] - b[0], a[1] - b[1]) };
    drag = null; draft = null;
    return;
  }
  const [mx, my] = toMm(px, py);
  closeMenu();
  if (e.button === 0 && spaceHeld) { drag = { mode: 'pan', start: [px, py], o: [view.ox, view.oy] }; cv.classList.add('dragging'); return; }
  if (e.button === 0 && placeHook) { const f = placeHook; placeHook = null; cv.style.cursor = ''; f(mx, my); return; }
  if (e.button === 0 && freeHook) { freePts = [[G.r2(mx), G.r2(my)]]; cv.setPointerCapture(e.pointerId); return; }
  if (e.button === 0 && polyHook) {
    const p = polyPoint(mx, my, e);
    const first = polyPts[0];
    if (first && polyPts.length >= 3 && Math.hypot(p[0] - first[0], p[1] - first[1]) * view.s < 8) { polyFinish(true); return; }
    const last = polyPts[polyPts.length - 1];
    if (!last || last[0] !== p[0] || last[1] !== p[1]) polyPts.push(p);
    redraw();
    return;
  }
  if (e.button === 0 && bedClick(mx, my)) return;
  const pan = { mode: 'pan', start: [px, py], o: [view.ox, view.oy], menu: e.button === 2 };
  if (e.button === 1 || e.button === 2 || !editable()) { drag = pan; cv.classList.add('dragging'); return; }
  // rulers: drag out a guide; an existing guide: move it (drop it on a ruler to remove it)
  if ((px < RULER || py < RULER) && !(px < RULER && py < RULER)) {
    drag = { mode: 'guide-new', axis: py < RULER ? 'y' : 'x', at: null, start: [mx, my] };
    return;
  }
  const gi = hitGuide(px, py);
  if (gi >= 0 && !hitHandle(px, py) && !hitObject(px, py)) {
    drag = { mode: 'guide', index: gi, axis: guides()[gi].axis, at: guides()[gi].at, start: [mx, my] };
    return;
  }
  const h = hitHandle(px, py);
  if (h) {
    const sel = state.doc.objects.filter((o) => state.sel.includes(o.id));
    drag = { mode: h.kind, h, start: [mx, my], orig: structuredClone(state.doc.objects), sel: sel.map((o) => o.id),
      bounds: G.bounds(sel) };
    cv.classList.add('dragging');
    return;
  }
  const o = hitObject(px, py);
  if (o) {
    if (e.shiftKey || e.ctrlKey || e.metaKey) D.select(o.id, { add: true, toggle: true });
    else if (!state.sel.includes(o.id)) D.select(o.id);
    drag = { mode: 'move', start: [mx, my], orig: structuredClone(state.doc.objects), sel: [...state.sel], hit: o.id,
      add: e.shiftKey || e.ctrlKey || e.metaKey };
    if (!state.sel.includes(o.id)) drag = null;       // toggled off: nothing to drag
    else if (state.doc.objects.some((x) => state.sel.includes(x.id) && x.locked)) drag = null;   // locked: select only
    cv.classList.add('dragging');
    return;
  }
  // empty bed: a finger pans, a mouse or pen draws a selection box
  drag = e.pointerType === 'touch' ? pan : { mode: 'marquee', start: [mx, my], add: e.shiftKey || e.ctrlKey || e.metaKey };
  cv.classList.add('dragging');
}

let lastPointerEvent = null;
function move(e) {
  const [px, py] = local(e);
  const [mx, my] = toMm(px, py);
  pointerMm = [mx, my];
  lastPointerEvent = { shiftKey: e.shiftKey };
  if (polyHook) { redraw(); status(); return; }
  if (freeHook && freePts) {
    const last = freePts[freePts.length - 1];
    if (Math.hypot(mx - last[0], my - last[1]) >= 0.2) { freePts.push([G.r2(mx), G.r2(my)]); redraw(); }
    return;
  }
  status();
  if (pointers.has(e.pointerId)) pointers.set(e.pointerId, [px, py]);
  if (pinch && pointers.size === 2) {
    const [a, b] = [...pointers.values()];
    const d = Math.hypot(a[0] - b[0], a[1] - b[1]);
    zoomBy(d / pinch.d, (a[0] + b[0]) / 2, (a[1] + b[1]) / 2);
    pinch.d = d;
    return;
  }
  if (!drag) {
    redraw();                       // the pointer marks on the rulers
    if (!editable() || placeHook) return;
    const h = hitHandle(px, py), o = h ? null : hitObject(px, py);
    const g = !h && !o ? hitGuide(px, py) : -1;
    cv.style.cursor = h ? cursorFor(h) : o ? 'move' : g >= 0 ? (guides()[g].axis === 'x' ? 'col-resize' : 'row-resize')
      : px < RULER || py < RULER ? 'copy' : '';
    const hv = o ? o.id : null;
    if (hv !== hover) { hover = hv; redraw(); }
    return;
  }
  drag.moved = drag.moved || Math.hypot(mx - drag.start[0], my - drag.start[1]) * view.s > 3;
  if (drag.mode === 'pan') {
    view.ox = drag.o[0] + px - drag.start[0];
    view.oy = drag.o[1] + py - drag.start[1];
    view.user = true;
  } else if (drag.mode === 'guide' || drag.mode === 'guide-new') {
    let at = drag.axis === 'x' ? mx : my;
    if (!e.altKey) at = snapHalf(at);
    drag.at = at;
    drag.off = px < RULER || py < RULER;        // over a ruler: dropping removes it
  } else if (drag.mode === 'marquee') {
    const [x0, y0] = drag.start;
    // left to right: whole objects inside; right to left: anything touched
    drag.rect = { x0: Math.min(x0, mx), y0: Math.min(y0, my), x1: Math.max(x0, mx), y1: Math.max(y0, my), touch: mx < x0 };
  } else if (drag.moved) {
    draft = structuredClone(drag.orig);
    const sel = draft.filter((o) => drag.sel.includes(o.id));
    if (drag.mode === 'move') dragMove(sel, mx, my, e);
    else if (drag.mode === 'rotate') dragRotate(sel[0], mx, my, e);
    else if (drag.h.group) dragGroupScale(sel, mx, my);
    else dragScale(sel[0], mx, my, e);
  }
  redraw();
}

// Free moves and resizes land on this step: the grid when View says so, else 0.5 mm.
const snapStep = () => { const v = viewPrefs(); return v.snapGrid && v.grid > 0 ? v.grid : 0.5; };
const snapHalf = (v) => { const s = snapStep(); return Math.round(v / s) * s; };

// ------------------------------------------------------------------ view menu
export const viewPrefs = () => ({ grid: 10, snapGrid: false, dir: false, nudge: 1, snapPx: 7, lockGuides: false, ...(prefs('view') || {}) });

function initViewMenu() {
  const btn = $('viewBtn'), menu = $('viewMenu');
  window.addEventListener('plotter-prefs', () => { loadView(); redraw(); status(); set({ units: viewPrefs().units || 'mm' }); });
  loadView();
  const save = () => { prefs('view', { grid: Number($('vGrid').value), snapGrid: $('vSnapGrid').checked, dir: $('vDir').checked,
    nudge: Number($('vNudge').value), snapPx: Number($('vSnapPx').value), lockGuides: $('vLockGuides').checked,
    units: $('vUnits').value }); redraw(); status(); set({ units: $('vUnits').value }); };
  initViewMenuRest(btn, menu, save);
}

function loadView() {
  const v = viewPrefs();
  $('vGrid').value = String(v.grid);
  $('vSnapGrid').checked = v.snapGrid;
  $('vDir').checked = v.dir;
  $('vNudge').value = String(v.nudge);
  $('vSnapPx').value = String(v.snapPx);
  $('vLockGuides').checked = v.lockGuides;
  $('vUnits').value = v.units || 'mm';
}

function initViewMenuRest(btn, menu, save) {
  for (const id of ['vGrid', 'vSnapGrid', 'vDir', 'vNudge', 'vSnapPx', 'vLockGuides', 'vUnits']) $(id).onchange = save;
  const show = (open) => { menu.hidden = !open; btn.setAttribute('aria-expanded', String(open)); };
  btn.onclick = (e) => { e.stopPropagation(); show(menu.hidden); };
  document.addEventListener('pointerdown', (e) => { if (!menu.hidden && !menu.contains(e.target) && e.target !== btn) show(false); });
  menu.addEventListener('keydown', (e) => { if (e.key === 'Escape') { show(false); btn.focus(); } });
  $('vPanels').onclick = () => { togglePanels(); show(false); };
  setPanels(!!prefs('noPanels'));
}

function setPanels(off) {
  $('plotWs').classList.toggle('no-panels', off);
  $('vPanels').textContent = off ? 'Show side panels' : 'Hide side panels';
  requestAnimationFrame(() => window.dispatchEvent(new Event('resize')));
}

export function togglePanels() {
  const off = !$('plotWs').classList.contains('no-panels');
  prefs('noPanels', off);
  setPanels(off);
}

// Each stroke's start (dot) and direction (an arrowhead halfway along), in plot order, up to stroke k.
function drawDirection(pv, k, px, hide) {
  const order = (pv.order || []).slice(0, k);
  if (order.length > 4000) return;
  const sl = (pv.summary && pv.summary.layers) || [];
  ctx.save();
  ctx.fillStyle = ctx.strokeStyle = css('--accent');
  ctx.lineWidth = 1.2 * px;
  const a = 5 * px;
  for (const i of order) {
    const s = pv.strokes[i];
    if (!s || s.pts.length < 2 || (hide.size && sl[s.l] && hide.has(sl[s.l].key))) continue;
    const [x0, y0] = s.pts[0];
    ctx.beginPath(); ctx.arc(x0, y0, 2 * px, 0, Math.PI * 2); ctx.fill();
    const j = Math.max(1, Math.floor(s.pts.length / 2));
    const [ax, ay] = s.pts[j - 1], [bx, by] = s.pts[j];
    const L = Math.hypot(bx - ax, by - ay);
    if (L < 1e-6) continue;
    const ux = (bx - ax) / L, uy = (by - ay) / L, mx = (ax + bx) / 2, my = (ay + by) / 2;
    ctx.beginPath();
    ctx.moveTo(mx - a * ux + a * 0.6 * uy, my - a * uy - a * 0.6 * ux);
    ctx.lineTo(mx, my);
    ctx.lineTo(mx - a * ux - a * 0.6 * uy, my - a * uy + a * 0.6 * ux);
    ctx.stroke();
  }
  ctx.restore();
}

function dragMove(sel, mx, my, e) {
  let dx = mx - drag.start[0], dy = my - drag.start[1];
  snapLines = [];
  if (!e.altKey) {
    // Edges and centre snap to the canvas, the paper, guides and other objects; otherwise the corner lands on
    // the 0.5 mm grid. Alt: no snapping at all.
    const b = G.bounds(drag.orig.filter((o) => drag.sel.includes(o.id)));
    const t = drag.targets || (drag.targets = snapTargets(drag.sel));
    const tol = (viewPrefs().snapPx ?? 7) / view.s;
    const sx = bestSnap([b.x0 + dx, (b.x0 + b.x1) / 2 + dx, b.x1 + dx], t.xs, tol);
    const sy = bestSnap([b.y0 + dy, (b.y0 + b.y1) / 2 + dy, b.y1 + dy], t.ys, tol);
    if (sx) { dx += sx[0]; snapLines.push({ axis: 'x', at: sx[1] }); } else dx = snapHalf(b.x0 + dx) - b.x0;
    if (sy) { dy += sy[0]; snapLines.push({ axis: 'y', at: sy[1] }); } else dy = snapHalf(b.y0 + dy) - b.y0;
  }
  for (const o of sel) { o.x += dx; o.y += dy; }
}

function dragRotate(o, mx, my, e) {
  const [cx, cy] = G.center(o);
  let ang = Math.atan2(my - cy, mx - cx) * 180 / Math.PI + 90;
  if (!e.shiftKey) ang = Math.round(ang / 15) * 15;      // Shift: any angle
  o.rot = G.norm(ang);
}

// One object: the opposite handle stays put. Corners keep the proportions unless Shift is held.
function dragScale(o, mx, my, e) {
  const o0 = drag.orig.find((x) => x.id === o.id);
  const { hx, hy } = drag.h;
  const [u, v] = G.toLocal(o0, mx, my);
  const fu = -hx * o0.w / 2, fv = -hy * o0.h / 2;
  let w = hx ? Math.max(0.5, hx * (u - fu)) : o0.w;
  let h = hy ? Math.max(0.5, hy * (v - fv)) : o0.h;
  if (hx && hy && !e.shiftKey) {
    const k = Math.max(w / o0.w, h / o0.h);
    w = o0.w * k; h = o0.h * k;
  }
  // an unturned box resized by an edge (or a corner with Shift) snaps its moving edges to what moves snap to
  let sx = false, sy = false;
  if (!e.altKey && !o0.rot && (!(hx && hy) || e.shiftKey) && (viewPrefs().snapPx ?? 7) > 0) {
    const t = drag.targets || (drag.targets = snapTargets(drag.sel));
    const tol = (viewPrefs().snapPx ?? 7) / view.s;
    if (hx) {
      const fixed = hx > 0 ? o0.x : o0.x + o0.w, mv = fixed + hx * w;
      const s = bestSnap([mv], t.xs, tol);
      if (s) { w = Math.max(0.5, Math.abs(mv + s[0] - fixed)); snapLines.push({ axis: 'x', at: s[1] }); sx = true; }
    }
    if (hy) {
      const fixed = hy > 0 ? o0.y : o0.y + o0.h, mv = fixed + hy * h;
      const s = bestSnap([mv], t.ys, tol);
      if (s) { h = Math.max(0.5, Math.abs(mv + s[0] - fixed)); snapLines.push({ axis: 'y', at: s[1] }); sy = true; }
    }
  }
  if (!e.altKey) { if (hx && !sx) w = Math.max(0.5, snapHalf(w)); if (hy && !sy) h = Math.max(0.5, snapHalf(h)); }
  const cu = hx ? fu + hx * w / 2 : 0, cv_ = hy ? fv + hy * h / 2 : 0;
  const [cx, cy] = G.fromLocal(o0, cu, cv_);
  o.w = w; o.h = h;
  G.setCenter(o, cx, cy);
}

function dragGroupScale(sel, mx, my) {
  const b = drag.bounds, { hx, hy } = drag.h;
  const fx = hx < 0 ? b.x1 : b.x0, fy = hy < 0 ? b.y1 : b.y0;
  const k = Math.max(0.01, Math.max(Math.abs(mx - fx) / b.w, Math.abs(my - fy) / b.h));
  G.scaleAbout(sel, fx, fy, k, k);
}

function up(e) {
  if (freeHook && freePts) {
    const f = freeHook, pts = rdp(freePts, 0.3);
    freeHook = null; freePts = null; cv.style.cursor = '';
    pointers.delete(e.pointerId);
    redraw();
    f(pts.length >= 2 ? pts : null);
    return;
  }
  pointers.delete(e.pointerId);
  if (pointers.size < 2) pinch = null;
  cv.classList.remove('dragging');
  const d = drag;
  drag = null;
  snapLines = [];
  if (d && d.mode === 'pan' && d.menu && !d.moved) {
    const [px, py] = local(e);
    const mm = toMm(px, py);
    const o = editable() ? hitObject(px, py) : null;
    if (o && !state.sel.includes(o.id)) D.select(o.id);
    if (!o && state.sel.length) D.select([]);
    if (editable()) openMenu(e.clientX, e.clientY, mm);
  } else if (d && (d.mode === 'guide' || d.mode === 'guide-new')) {
    if (d.at !== null) {
      const list = guides().slice();
      if (d.mode === 'guide') { if (d.off) list.splice(d.index, 1); else list[d.index] = { axis: d.axis, at: d.at }; }
      else if (!d.off) list.push({ axis: d.axis, at: d.at });
      setGuides(list);
    }
  } else if (d && d.mode === 'marquee') {
    if (d.rect && d.moved) {
      const r = d.rect;
      const hits = state.doc.objects.filter((o) => {
        const b = G.bounds([o]);
        return r.touch ? b.x1 >= r.x0 && b.x0 <= r.x1 && b.y1 >= r.y0 && b.y0 <= r.y1
          : b.x0 >= r.x0 && b.x1 <= r.x1 && b.y0 >= r.y0 && b.y1 <= r.y1;
      });
      D.select(hits.map((o) => o.id), { add: d.add });
    } else if (!d.add) D.select([]);
  } else if (d && d.mode === 'move' && !d.moved && !d.add && state.sel.length > 1) {
    D.select(d.hit);                     // a plain click inside a group selects just that object
  } else if (d && draft && d.moved && d.mode !== 'pan') {
    const objs = draft;
    draft = null;
    D.edit((doc) => {
      doc.objects = objs.map((o) => ({ ...o, x: G.r2(o.x), y: G.r2(o.y), w: G.r2(o.w), h: G.r2(o.h) }));
    });
  }
  draft = null;
  redraw();
}

function wheel(e) {
  e.preventDefault();
  const [px, py] = local(e);
  zoomBy(e.deltaY < 0 ? 1.15 : 1 / 1.15, px, py);
}

function key(e) {
  if (e.key === 'Tab' && editable() && !polyHook) { e.preventDefault(); D.selectNext(e.shiftKey ? -1 : 1); return; }
  if (polyHook) {
    if (e.key === 'Enter') { e.preventDefault(); polyFinish(false); }
    else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); polyPts = []; polyFinish(false); }
    else if (e.key === 'Backspace') { e.preventDefault(); e.stopPropagation(); polyPts.pop(); redraw(); }
    return;
  }
  if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key)) return;
  if (state.job && state.job.kind === 'machine' && jobActive()) {      // machine control: the arrows jog
    e.preventDefault();
    jogHook(e.key === 'ArrowLeft' ? -1 : e.key === 'ArrowRight' ? 1 : 0, e.key === 'ArrowUp' ? -1 : e.key === 'ArrowDown' ? 1 : 0);
    return;
  }
  if (!editable() || !state.sel.length) return;
  e.preventDefault();
  const step = viewPrefs().nudge || 1;
  const d = e.shiftKey ? step * 10 : e.altKey ? step / 10 : step;
  D.nudge(e.key === 'ArrowLeft' ? -d : e.key === 'ArrowRight' ? d : 0, e.key === 'ArrowUp' ? -d : e.key === 'ArrowDown' ? d : 0);
}
