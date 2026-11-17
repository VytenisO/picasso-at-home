// The bed view: bed, paper, inset canvas, start point S, the drawing (move and resize), plotted progress and the
// carriage. Everything is in machine millimetres; the view maps mm to screen pixels (pan and zoom).
import { state, on, jobActive } from './store.js';
import { $ } from './ui.js';

let cv, ctx, W = 0, H = 0, dpr = 1;
const view = { s: 2, ox: 20, oy: 20, fitted: false };
let drag = null;            // {mode: pan|move|resize, ...}
let draft = null;           // box while dragging {x, y, w, h}
const pointers = new Map();
let pinch = null;
let onPlace = () => {};
let cache = { key: null, layers: [] };       // Path2D per layer for the preview strokes
let done = { key: null, n: 0, path: null };  // plotted strokes of the job preview
let colors = {};
let raf = 0;

export function initBed(placeCallback) {
  onPlace = placeCallback;
  cv = $('bed');
  ctx = cv.getContext('2d');
  new ResizeObserver(resize).observe(cv);
  cv.addEventListener('pointerdown', down);
  cv.addEventListener('pointermove', move);
  cv.addEventListener('pointerup', up);
  cv.addEventListener('pointercancel', up);
  cv.addEventListener('wheel', wheel, { passive: false });
  cv.addEventListener('keydown', key);
  $('zoomIn').onclick = () => zoomBy(1.25);
  $('zoomOut').onclick = () => zoomBy(0.8);
  $('zoomFit').onclick = fit;
  on(['me', 'settings', 'preview', 'previewStale', 'job', 'jobPreview', 'pos', 'current'], redraw);
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
  view.s = Math.min((W - 2 * pad) / m.bed[0], (H - 2 * pad) / m.bed[1]);
  view.ox = (W - m.bed[0] * view.s) / 2;
  view.oy = (H - m.bed[1] * view.s) / 2;
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

export function canvasRect() {
  const m = state.me.machine;
  const p = state.settings && state.settings.paper;
  const [px0, py0] = m.paper;
  const pw = p ? p.width : m.paper[2], ph = p ? p.height : m.paper[3];
  return { paper: [px0, py0, pw, ph], canvas: [px0 + m.margin, py0 + m.margin, px0 + pw - m.margin, py0 + ph - m.margin] };
}

// The drawing's box in mm: from the settings, with the aspect ratio of the previewed geometry.
export function designBox() {
  if (draft) return draft;
  const st = state.settings, pv = state.preview;
  if (!st || st.x === null || st.x === undefined || !pv || !pv.summary) return null;
  const b = pv.summary.bbox_machine_mm;
  const bw = b[2] - b[0], bh = b[3] - b[1];
  const w = st.w || bw;
  return { x: st.x, y: st.y, w, h: bw > 1e-6 ? w * bh / bw : bh };
}

function boxOutside(box) {
  const c = canvasRect().canvas;
  return box.x < c[0] - 0.5 || box.y < c[1] - 0.5 || box.x + box.w > c[2] + 0.5 || box.y + box.h > c[3] + 0.5;
}

function redraw() {
  if (!raf) raf = requestAnimationFrame(() => { raf = 0; render(); });
}

function buildLayers(pv) {
  const key = pv.plan_id || pv.file;
  if (cache.key === key) return cache.layers;
  const by = new Map();
  pv.strokes.forEach((s) => {
    if (!by.has(s.l)) by.set(s.l, new Path2D());
    const p = by.get(s.l);
    s.pts.forEach(([x, y], i) => (i ? p.lineTo(x, y) : p.moveTo(x, y)));
  });
  cache = { key, layers: [...by.entries()] };
  return cache.layers;
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

function layerColor(pv, l) {
  const ly = pv.summary && pv.summary.layers && pv.summary.layers[l];
  const c = ly && ly.color;
  return c && c !== '#000000' && c !== '#111111' ? c : css('--text');
}

function render() {
  if (!ctx || !state.me) return;
  const m = state.me.machine;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  ctx.setTransform(dpr * view.s, 0, 0, dpr * view.s, dpr * view.ox, dpr * view.oy);
  const px = 1 / view.s;
  // bed with a 10 mm grid
  ctx.fillStyle = css('--bed');
  ctx.fillRect(0, 0, m.bed[0], m.bed[1]);
  ctx.strokeStyle = css('--grid'); ctx.lineWidth = px;
  ctx.beginPath();
  for (let x = 10; x < m.bed[0]; x += 10) { ctx.moveTo(x, 0); ctx.lineTo(x, m.bed[1]); }
  for (let y = 10; y < m.bed[1]; y += 10) { ctx.moveTo(0, y); ctx.lineTo(m.bed[0], y); }
  ctx.stroke();
  const { paper, canvas } = canvasRect();
  ctx.fillStyle = css('--paper');
  ctx.fillRect(paper[0], paper[1], paper[2], paper[3]);
  ctx.strokeStyle = css('--line'); ctx.lineWidth = px;
  ctx.strokeRect(paper[0], paper[1], paper[2], paper[3]);
  ctx.setLineDash([4 * px, 3 * px]);
  ctx.strokeStyle = css('--canvas-line');
  ctx.strokeRect(canvas[0], canvas[1], canvas[2] - canvas[0], canvas[3] - canvas[1]);
  ctx.setLineDash([]);

  // the drawing
  const jp = state.jobPreview, job = state.job;
  const showJob = jp && job && job.kind === 'plot' && (jobActive() || (!state.preview && job.meta?.plan_id === jp.plan_id)) &&
    (!state.current || state.current.id === jp.file);
  const lw = Math.max(0.3, 1.2 * px);
  ctx.lineCap = 'round'; ctx.lineJoin = 'round';
  if (showJob) {
    ctx.globalAlpha = 0.28;
    for (const [l, p] of buildLayers(jp)) { ctx.strokeStyle = layerColor(jp, l); ctx.lineWidth = lw; ctx.stroke(p); }
    ctx.globalAlpha = 1;
    const n = job.progress ? job.progress.done : 0;
    ctx.strokeStyle = css('--done'); ctx.lineWidth = lw * 1.3;
    ctx.stroke(donePath(jp, n));
  } else if (state.preview && state.preview.strokes) {
    const pv = state.preview, b = pv.summary.bbox_machine_mm, box = designBox();
    const k = box && (b[2] - b[0]) > 1e-6 ? box.w / (b[2] - b[0]) : 1;
    ctx.save();
    if (box) { ctx.translate(box.x, box.y); ctx.scale(k, k); ctx.translate(-b[0], -b[1]); }
    const enabled = (state.settings && state.settings.layers) || [];
    for (const [l, p] of buildLayers(pv)) {
      ctx.globalAlpha = enabled[l] && enabled[l].enabled === false ? 0.15 : (state.previewStale ? 0.55 : 1);
      ctx.strokeStyle = layerColor(pv, l); ctx.lineWidth = lw / k;
      ctx.stroke(p);
    }
    ctx.restore();
    ctx.globalAlpha = 1;
    if (box && !jobActive()) {
      const bad = boxOutside(box);
      ctx.strokeStyle = bad ? css('--err') : css('--accent');
      ctx.lineWidth = 1.5 * px;
      ctx.setLineDash(bad ? [] : [6 * px, 4 * px]);
      ctx.strokeRect(box.x, box.y, box.w, box.h);
      ctx.setLineDash([]);
      ctx.fillStyle = ctx.strokeStyle;
      for (const [hx, hy] of corners(box)) ctx.fillRect(hx - 4 * px, hy - 4 * px, 8 * px, 8 * px);
      ctx.font = `${12 * px}px system-ui, sans-serif`;
      ctx.fillText(`${box.w.toFixed(1)} x ${box.h.toFixed(1)} mm${bad ? '  outside the canvas' : ''}`, box.x, box.y - 5 * px);
    }
  }

  // start point S
  const [sx, sy] = m.start;
  ctx.strokeStyle = css('--ok'); ctx.lineWidth = 2 * px;
  ctx.beginPath(); ctx.moveTo(sx, sy + 12 * px); ctx.lineTo(sx, sy); ctx.lineTo(sx + 12 * px, sy); ctx.stroke();
  ctx.fillStyle = css('--ok'); ctx.font = `bold ${12 * px}px system-ui, sans-serif`;
  ctx.fillText('S', sx + 4 * px, sy + 14 * px);

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
}

function corners(b) { return [[b.x, b.y], [b.x + b.w, b.y], [b.x + b.w, b.y + b.h], [b.x, b.y + b.h]]; }

function hit(px, py) {
  const box = designBox();
  if (!box || jobActive() || !state.preview || !state.preview.strokes) return null;
  const cs = corners(box);
  for (let i = 0; i < 4; i++) {
    const [cx, cy] = toPx(...cs[i]);
    if (Math.abs(cx - px) <= 12 && Math.abs(cy - py) <= 12) return { mode: 'resize', corner: i, box };
  }
  const [x, y] = toMm(px, py);
  if (x >= box.x && x <= box.x + box.w && y >= box.y && y <= box.y + box.h) return { mode: 'move', box };
  return null;
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
  const h = hit(px, py);
  const [mx, my] = toMm(px, py);
  drag = h ? { ...h, start: [mx, my] } : { mode: 'pan', start: [px, py], o: [view.ox, view.oy] };
  cv.classList.add('dragging');
}

function move(e) {
  const [px, py] = local(e);
  const [mx, my] = toMm(px, py);
  $('bedCoords').textContent = `${mx.toFixed(1)}, ${my.toFixed(1)} mm`;
  if (pointers.has(e.pointerId)) pointers.set(e.pointerId, [px, py]);
  if (pinch && pointers.size === 2) {
    const [a, b] = [...pointers.values()];
    const d = Math.hypot(a[0] - b[0], a[1] - b[1]);
    zoomBy(d / pinch.d, (a[0] + b[0]) / 2, (a[1] + b[1]) / 2);
    pinch.d = d;
    return;
  }
  if (!drag) { cv.style.cursor = hit(px, py) ? (hit(px, py).mode === 'move' ? 'move' : 'nwse-resize') : ''; return; }
  const snap = (v) => (e.shiftKey ? v : Math.round(v * 2) / 2);
  if (drag.mode === 'pan') {
    view.ox = drag.o[0] + px - drag.start[0];
    view.oy = drag.o[1] + py - drag.start[1];
    view.user = true;
  } else if (drag.mode === 'move') {
    const b = drag.box;
    draft = { ...b, x: snap(b.x + mx - drag.start[0]), y: snap(b.y + my - drag.start[1]) };
  } else {
    const b = drag.box, ar = b.h / b.w;
    const fx = drag.corner === 0 || drag.corner === 3 ? b.x + b.w : b.x;   // fixed corner
    const fy = drag.corner === 0 || drag.corner === 1 ? b.y + b.h : b.y;
    let w = Math.max(Math.abs(mx - fx), Math.abs(my - fy) / ar);
    w = Math.max(2, snap(w));
    const h = w * ar;
    draft = { x: mx < fx ? fx - w : fx, y: my < fy ? fy - h : fy, w, h };
    if (drag.corner === 0 || drag.corner === 3) draft.x = fx - w;
    if (drag.corner === 0 || drag.corner === 1) draft.y = fy - h;
  }
  redraw();
}

function up(e) {
  pointers.delete(e.pointerId);
  if (pointers.size < 2) pinch = null;
  cv.classList.remove('dragging');
  if (drag && draft && (drag.mode === 'move' || drag.mode === 'resize')) {
    const d = draft;
    draft = null;
    onPlace({ x: round(d.x), y: round(d.y), w: round(d.w) });
  }
  drag = null;
  redraw();
}

const round = (v) => Math.round(v * 100) / 100;

function wheel(e) {
  e.preventDefault();
  const [px, py] = local(e);
  zoomBy(e.deltaY < 0 ? 1.15 : 1 / 1.15, px, py);
}

function key(e) {
  if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key)) {
    const box = designBox();
    if (!box || jobActive()) return;
    e.preventDefault();
    const d = e.shiftKey ? 10 : 1;
    const dx = e.key === 'ArrowLeft' ? -d : e.key === 'ArrowRight' ? d : 0;
    const dy = e.key === 'ArrowUp' ? -d : e.key === 'ArrowDown' ? d : 0;
    onPlace({ x: round(box.x + dx), y: round(box.y + dy), w: round(box.w) });
  }
}
