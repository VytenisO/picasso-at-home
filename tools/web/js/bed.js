// The bed view: bed, paper, inset canvas, start point S, the objects on the bed (select, move, resize, rotate),
// plotted progress and the carriage. Everything is in machine millimetres; the view maps mm to screen pixels.
//
// While the bed is being edited each object is drawn from its drawing's own strokes through its transform, so a
// drag is instant. Once a preview is fresh the planner's exact strokes are drawn instead (fills, simplification).
import { state, set, on, jobActive, prefs } from './store.js';
import { $ } from './ui.js';
import { drawInk, inkMode, setInkMode } from './ink.js';
import * as G from './geom.js';
import * as D from './doc.js';

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
  cv.addEventListener('contextmenu', (e) => e.preventDefault());
  $('zoomIn').onclick = () => zoomBy(1.25);
  $('zoomOut').onclick = () => zoomBy(0.8);
  $('zoomFit').onclick = fit;
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
  // bed with a 10 mm grid
  ctx.fillStyle = css('--bed');
  ctx.fillRect(0, 0, m.bed[0], m.bed[1]);
  ctx.strokeStyle = css('--grid'); ctx.lineWidth = px;
  ctx.beginPath();
  for (let x = 10; x < m.bed[0]; x += 10) { ctx.moveTo(x, 0); ctx.lineTo(x, m.bed[1]); }
  for (let y = 10; y < m.bed[1]; y += 10) { ctx.moveTo(0, y); ctx.lineTo(m.bed[0], y); }
  ctx.stroke();
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
        drawInk(ctx, pv, { px, layers, pens, css, alpha: 0.16 });
        drawInk(ctx, subset(pv, k), { px, layers, pens, css });
      } else drawInk(ctx, pv, { px, layers, pens, css });
      if (travelOn() || state.scrub !== null) drawTravel(pv, k, px);
    } else {
      for (const o of objects()) drawObject(o, { px, layers, pens });
    }
    if (editable()) overlays(px);
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
  const step = Math.max(n / (12 * 60), 0.25);           // the whole plan in about 12 s, at least 15 strokes/s
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

function drawObject(o, { px, layers, pens }) {
  const n = state.naturals[o.file];
  if (!n) {           // geometry still loading: the box only
    ctx.setLineDash([2 * px, 2 * px]); ctx.strokeStyle = css('--muted'); ctx.lineWidth = px;
    poly(G.corners(o)); ctx.stroke(); ctx.setLineDash([]);
    return;
  }
  const mt = G.matrix(o, n.geom);
  const k = Math.sqrt(Math.abs(mt[0] * mt[3] - mt[1] * mt[2])) || 1;
  ctx.save();
  ctx.transform(...mt);
  drawInk(ctx, n, { px: px / k, layers, pens, css, alpha: state.previewStale || draft ? 0.75 : 1, lineScale: k });
  ctx.restore();
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
    ctx.setLineDash(isSel ? [] : [5 * px, 4 * px]);
    poly(G.corners(o)); ctx.stroke();
    ctx.setLineDash([]);
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
    const label = one
      ? `${G.r2(one.w)} x ${G.r2(one.h)} mm${one.rot ? `, ${G.r2(one.rot)} deg` : ''}`
      : `${sel.length} objects, ${G.r2(b.w)} x ${G.r2(b.h)} mm`;
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
  const pan = { mode: 'pan', start: [px, py], o: [view.ox, view.oy] };
  if (e.button === 1 || e.button === 2 || !editable()) { drag = pan; cv.classList.add('dragging'); return; }
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
    cv.classList.add('dragging');
    return;
  }
  // empty bed: a finger pans, a mouse or pen draws a selection box
  drag = e.pointerType === 'touch' ? pan : { mode: 'marquee', start: [mx, my], add: e.shiftKey || e.ctrlKey || e.metaKey };
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
  if (!drag) {
    if (!editable()) return;
    const h = hitHandle(px, py), o = h ? null : hitObject(px, py);
    cv.style.cursor = h ? cursorFor(h) : o ? 'move' : '';
    const hv = o ? o.id : null;
    if (hv !== hover) { hover = hv; redraw(); }
    return;
  }
  drag.moved = drag.moved || Math.hypot(mx - drag.start[0], my - drag.start[1]) * view.s > 3;
  if (drag.mode === 'pan') {
    view.ox = drag.o[0] + px - drag.start[0];
    view.oy = drag.o[1] + py - drag.start[1];
    view.user = true;
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

const snapHalf = (v) => Math.round(v * 2) / 2;

function dragMove(sel, mx, my, e) {
  let dx = mx - drag.start[0], dy = my - drag.start[1];
  if (!e.altKey) {           // the selection's corner lands on the 0.5 mm grid
    const b = G.bounds(drag.orig.filter((o) => drag.sel.includes(o.id)));
    dx = snapHalf(b.x0 + dx) - b.x0; dy = snapHalf(b.y0 + dy) - b.y0;
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
  if (!e.altKey) { if (hx) w = Math.max(0.5, snapHalf(w)); if (hy) h = Math.max(0.5, snapHalf(h)); }
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
  pointers.delete(e.pointerId);
  if (pointers.size < 2) pinch = null;
  cv.classList.remove('dragging');
  const d = drag;
  drag = null;
  if (d && d.mode === 'marquee') {
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
  if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key) || !editable() || !state.sel.length) return;
  e.preventDefault();
  const d = e.shiftKey ? 10 : e.altKey ? 0.1 : 1;
  D.nudge(e.key === 'ArrowLeft' ? -d : e.key === 'ArrowRight' ? d : 0, e.key === 'ArrowUp' ? -d : e.key === 'ArrowDown' ? d : 0);
}
