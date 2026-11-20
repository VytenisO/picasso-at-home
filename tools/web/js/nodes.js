// Node editing and the grid deform: both edit an object's lines in bed millimetres through the bed's point
// editor, then send the result to the server (line tools "set" and "grid"), which makes a new drawing in its
// place. Undo brings the old one back.
import { post } from './api.js';
import { jobActive } from './store.js';
import { $, el, toast } from './ui.js';
import { setPointEdit } from './bed.js';
import * as D from './doc.js';

let bar = null;
function showBar(title, buttons) {
  bar?.remove();
  bar = el('div', { class: 'pedit-bar', role: 'toolbar', 'aria-label': title }, el('b', { text: title }),
    ...buttons.map(([t, fn, tip]) => el('button', { type: 'button', text: t, title: tip || '', onclick: fn })));
  $('bed').parentElement.append(bar);
}
function endEdit() { setPointEdit(null); bar?.remove(); bar = null; }

async function fetchStrokes(o) {
  const r = await post('/api/shapes', { op: 'strokes', objects: [o] });
  return r.strokes;
}

async function replaceWith(o, op, extra) {
  try {
    const r = await post('/api/shapes', { op, objects: [o], ...extra });
    const { refreshFiles } = await import('./workspace.js');
    await refreshFiles();
    await D.addFile(r.file.id, { at: [r.x, r.y], replace: [o.id] });
    return true;
  } catch (e) { toast(e.message, true); return false; }
}

function one() {
  const sel = D.selected();
  if (sel.length !== 1) { toast('Select one drawing.', true); return null; }
  if (jobActive()) { toast('A job is running.', true); return null; }
  if (sel[0].locked) { toast('It is locked: unlock it first.', true); return null; }
  return sel[0];
}

const dist2 = (a, b) => (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2;
// the nearest point on segment ab to p -> [t, point]
function onSeg(p, a, b) {
  const dx = b[0] - a[0], dy = b[1] - a[1], l = dx * dx + dy * dy;
  const t = l ? Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l)) : 0;
  return [t, [a[0] + t * dx, a[1] + t * dy]];
}
const closedOf = (pts) => pts.length > 3 && dist2(pts[0], pts[pts.length - 1]) < 1e-6;

// ------------------------------------------------------------------ node edit
export async function nodeEdit() {
  const o = one();
  if (!o) return;
  let strokes;
  try { strokes = await fetchStrokes(o); } catch (e) { toast(e.message, true); return; }
  const total = strokes.reduce((n, s) => n + s.pts.length, 0);
  if (total > 4000) { toast(`${total} nodes is too many to edit by hand: Simplify or Fit arcs first.`, true); return; }
  let sel = null;                      // {s, i}: stroke and node index
  const undo = [];
  const snap = () => { undo.push(JSON.stringify(strokes)); if (undo.length > 200) undo.shift(); };
  const node = () => sel && strokes[sel.s] && strokes[sel.s].pts[sel.i];

  const act = {
    del() {                            // delete the node; a stroke left with one point goes
      if (!node()) return toast('Click a node first.');
      snap();
      const st = strokes[sel.s], cl = closedOf(st.pts);
      st.pts.splice(sel.i, 1);
      if (cl && sel.i === 0) st.pts[st.pts.length - 1] = [...st.pts[0]];
      if (st.pts.length < 2) strokes.splice(sel.s, 1);
      sel = null;
    },
    brk() {                            // break the stroke at the node: two strokes (a closed one opens there)
      if (!node()) return toast('Click a node first.');
      snap();
      const st = strokes[sel.s], i = sel.i;
      if (closedOf(st.pts)) {
        const ring = st.pts.slice(0, -1);
        st.pts = [...ring.slice(i), ...ring.slice(0, i + 1)];
      } else if (i > 0 && i < st.pts.length - 1) {
        strokes.splice(sel.s + 1, 0, { c: st.c, pts: st.pts.slice(i).map((p) => [...p]) });
        st.pts = st.pts.slice(0, i + 1);
      }
      sel = null;
    },
    seg() {                            // delete the segment after the node
      if (!node()) return toast('Click the node before the segment to delete.');
      snap();
      const st = strokes[sel.s], i = sel.i;
      if (closedOf(st.pts)) {
        const ring = st.pts.slice(0, -1);
        st.pts = [...ring.slice(i + 1), ...ring.slice(0, i + 1)].map((p) => [...p]);
      } else {
        if (i >= st.pts.length - 1) return toast('That is the last node: there is no segment after it.');
        const a = st.pts.slice(0, i + 1), b = st.pts.slice(i + 1);
        strokes.splice(sel.s, 1, ...[a, b].filter((x) => x.length >= 2).map((pts) => ({ c: st.c, pts })));
      }
      sel = null;
    },
    smooth() {                         // corner -> curve: the node becomes a short rounded run (corner cutting)
      if (!node()) return toast('Click a node first.');
      const st = strokes[sel.s], i = sel.i, cl = closedOf(st.pts), n = st.pts.length;
      const pi = i > 0 ? i - 1 : cl ? n - 2 : -1, ni = i < n - 1 ? i + 1 : cl ? 1 : -1;
      if (pi < 0 || ni < 0) return toast('An end node has no corner to round.');
      snap();
      const p = st.pts[pi], c = st.pts[i], q = st.pts[ni];
      const lerp = (a, b, t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
      const run = [];
      for (let k = 0; k <= 6; k++) {   // a quadratic curve from the middle of one side to the middle of the other
        const t = k / 6, a = lerp(lerp(p, c, 0.5), c, t), b = lerp(c, lerp(c, q, 0.5), t);
        run.push(lerp(a, b, t));
      }
      st.pts.splice(i, 1, ...run);
      if (cl && i === 0) st.pts[st.pts.length - 1] = [...st.pts[0]];
      sel = null;
    },
    undo() { if (undo.length) { strokes = JSON.parse(undo.pop()); sel = null; } },
  };
  const redrawBar = () => setPointEdit(pe);
  const pe = {
    hideId: o.id,
    handles: () => strokes.flatMap((st, s) => st.pts.map((p, i) => ({ x: p[0], y: p[1], key: `${s}:${i}` }))
      .filter((h, i, arr) => !(closedOf(st.pts) && i === arr.length - 1))),
    down(h) {
      if (h) { const [s, i] = h.key.split(':').map(Number); sel = { s, i }; snap(); } else sel = null;
    },
    drag(key, mm, e) {
      const [s, i] = key.split(':').map(Number), st = strokes[s];
      const p = e.altKey ? mm : [Math.round(mm[0] * 20) / 20, Math.round(mm[1] * 20) / 20];
      const cl = closedOf(st.pts);
      st.pts[i] = p;
      if (cl && i === 0) st.pts[st.pts.length - 1] = [...p];      // a closed line stays closed
    },
    up() {},
    dbl(mm, tol) {                    // add a node on the nearest segment
      let best = null;
      strokes.forEach((st, s) => st.pts.forEach((a, i) => {
        if (i === st.pts.length - 1) return;
        const [, q] = onSeg(mm, a, st.pts[i + 1]), d = dist2(q, mm);
        if (!best || d < best.d) best = { s, i, q, d };
      }));
      if (!best || best.d > tol * tol * 4) return;
      snap();
      strokes[best.s].pts.splice(best.i + 1, 0, best.q.map((v) => Math.round(v * 1000) / 1000));
      sel = { s: best.s, i: best.i + 1 };
    },
    key(e) {
      if (e.key === 'Delete' || e.key === 'Backspace') { act.del(); return true; }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { act.undo(); return true; }
      if (e.key === 'Escape') { endEdit(); return true; }
      if (e.key === 'Enter') { finish(); return true; }
      return false;
    },
    draw(ctx, px) {
      ctx.save();
      ctx.lineWidth = 1.2 * px; ctx.strokeStyle = '#2f6fdd';
      for (const st of strokes) {
        ctx.beginPath();
        st.pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
        ctx.stroke();
      }
      const r = 3 * px;
      strokes.forEach((st, s) => st.pts.forEach(([x, y], i) => {
        const on = sel && sel.s === s && sel.i === i;
        ctx.fillStyle = on ? '#2f6fdd' : '#ffffff';
        ctx.strokeStyle = '#2f6fdd';
        ctx.fillRect(x - r, y - r, 2 * r, 2 * r); ctx.strokeRect(x - r, y - r, 2 * r, 2 * r);
      }));
      ctx.restore();
    },
  };
  async function finish() {
    const ok = strokes.filter((st) => st.pts.length >= 2);
    if (!ok.length) { toast('Nothing would be left.', true); return; }
    endEdit();
    await replaceWith(o, 'set', { strokes: ok });
  }
  const wrap = (fn) => () => { fn(); redrawBar(); };
  showBar('Edit nodes', [
    ['Delete node', wrap(act.del), 'Delete or Backspace'], ['Break here', wrap(act.brk), 'Split the line at the node'],
    ['Delete segment', wrap(act.seg), 'Remove the segment after the node'], ['Round corner', wrap(act.smooth), 'Make the corner at the node a curve'],
    ['Undo', wrap(act.undo), 'Ctrl+Z'], ['Done', finish, 'Enter'], ['Cancel', endEdit, 'Esc']]);
  setPointEdit(pe);
  $('bed').focus();
  toast('Drag a node to move it (Alt: no 0.05 mm snapping). Double-click a line to add a node. Enter when done.');
}

// ------------------------------------------------------------------ grid deform
export async function gridDeform(n = 4) {
  const o = one();
  if (!o) return;
  let strokes;
  try { strokes = await fetchStrokes(o); } catch (e) { toast(e.message, true); return; }
  const xs = strokes.flatMap((s) => s.pts.map((p) => p[0])), ys = strokes.flatMap((s) => s.pts.map((p) => p[1]));
  const x0 = Math.min(...xs), y0 = Math.min(...ys), w = Math.max(Math.max(...xs) - x0, 1e-6), h = Math.max(Math.max(...ys) - y0, 1e-6);
  const grid = Array.from({ length: n }, (_, i) => Array.from({ length: n }, (_, j) => [x0 + w * j / (n - 1), y0 + h * i / (n - 1)]));
  const warp = ([x, y]) => {           // the same bilinear map as the server
    const u = (x - x0) / w * (n - 1), v = (y - y0) / h * (n - 1);
    const i = Math.min(Math.floor(v), n - 2), j = Math.min(Math.floor(u), n - 2), fu = u - j, fv = v - i;
    const a = grid[i][j], b = grid[i][j + 1], c = grid[i + 1][j + 1], d = grid[i + 1][j];
    const tx = a[0] + (b[0] - a[0]) * fu, ty = a[1] + (b[1] - a[1]) * fu, bx = d[0] + (c[0] - d[0]) * fu, by = d[1] + (c[1] - d[1]) * fu;
    return [tx + (bx - tx) * fv, ty + (by - ty) * fv];
  };
  const pe = {
    hideId: o.id,
    handles: () => grid.flatMap((r, i) => r.map((p, j) => ({ x: p[0], y: p[1], key: `${i}:${j}` }))),
    down() {},
    drag(key, mm) { const [i, j] = key.split(':').map(Number); grid[i][j] = mm; },
    key(e) {
      if (e.key === 'Escape') { endEdit(); return true; }
      if (e.key === 'Enter') { apply(); return true; }
      return false;
    },
    draw(ctx, px) {
      ctx.save();
      ctx.lineWidth = 1.2 * px; ctx.strokeStyle = '#2f6fdd';
      for (const st of strokes) {          // a preview: each segment split so the curve shows
        ctx.beginPath();
        st.pts.forEach((p, i) => {
          if (i) {
            const q = st.pts[i - 1], k = Math.max(1, Math.ceil(Math.hypot(p[0] - q[0], p[1] - q[1]) / 2));
            for (let t = 1; t <= k; t++) ctx.lineTo(...warp([q[0] + (p[0] - q[0]) * t / k, q[1] + (p[1] - q[1]) * t / k]));
          } else ctx.moveTo(...warp(p));
        });
        ctx.stroke();
      }
      ctx.strokeStyle = 'rgba(47,111,221,.5)'; ctx.setLineDash([3 * px, 3 * px]);
      for (let i = 0; i < n; i++) {
        ctx.beginPath(); grid[i].forEach((p, j) => (j ? ctx.lineTo(...p) : ctx.moveTo(...p))); ctx.stroke();
        ctx.beginPath(); grid.forEach((r, k) => (k ? ctx.lineTo(...r[i]) : ctx.moveTo(...r[i]))); ctx.stroke();
      }
      ctx.setLineDash([]);
      const r = 3.5 * px;
      ctx.fillStyle = '#fff';
      for (const row of grid) for (const [x, y] of row) { ctx.beginPath(); ctx.arc(x, y, r, 0, 7); ctx.fill(); ctx.stroke(); }
      ctx.restore();
    },
  };
  async function apply() { endEdit(); await replaceWith(o, 'grid', { grid }); }
  showBar(`Deform (${n} x ${n} grid)`, [['Apply', apply, 'Enter'], ['Cancel', endEdit, 'Esc']]);
  setPointEdit(pe);
  $('bed').focus();
  toast('Drag the grid points; the drawing follows. Enter applies.');
}
export { endEdit };

// ------------------------------------------------------------------ bezier pen
// Click: a corner point. Press and drag: a smooth point whose handle follows the pointer (the other side mirrors
// it). Click the first point to close; Enter finishes; Backspace removes the last point; Esc cancels.
// done(points in bed mm, closed) or done(null) when cancelled.
export function bezierTool(done) {
  const A = [];                              // {p, h}: the point and its outgoing handle (null: a corner)
  const mirror = (a) => (a.h ? [2 * a.p[0] - a.h[0], 2 * a.p[1] - a.h[1]] : a.p);
  const segs = (closed) => {
    const out = [];
    for (let i = 0; i + 1 < A.length || (closed && i < A.length); i++) {
      const a = A[i], b = A[(i + 1) % A.length];
      out.push([a.p, a.h || a.p, mirror(b), b.p]);
    }
    return out;
  };
  const cubic = ([p0, p1, p2, p3], t) => {
    const u = 1 - t;
    return [0, 1].map((k) => u * u * u * p0[k] + 3 * u * u * t * p1[k] + 3 * u * t * t * p2[k] + t * t * t * p3[k]);
  };
  const finish = (closed) => {
    const S = segs(closed);
    if (!S.length) { toast('Place at least two points.', true); return; }
    const len = S.reduce((n, s) => n + Math.hypot(s[3][0] - s[0][0], s[3][1] - s[0][1]) + Math.hypot(s[1][0] - s[0][0], s[1][1] - s[0][1]), 0);
    const per = Math.max(2, Math.min(60, Math.floor(1900 / S.length), Math.ceil(len / S.length / 0.5)));
    const pts = [S[0][0]];
    for (const s of S) for (let k = 1; k <= per; k++) pts.push(cubic(s, k / per).map((v) => Math.round(v * 1000) / 1000));
    endEdit();
    done(pts, closed);
  };
  const pe = {
    handles: () => [...A.map((a, i) => ({ x: a.p[0], y: a.p[1], key: `a${i}` })),
      ...A.flatMap((a, i) => (a.h ? [{ x: a.h[0], y: a.h[1], key: `h${i}` }] : []))],
    down(h, mm) {
      if (h && h.key === 'a0' && A.length >= 3) { finish(true); return undefined; }
      if (h) return undefined;               // drag an existing point or handle
      A.push({ p: mm, h: null });
      return `h${A.length - 1}`;             // pressing and dragging pulls this point's handle out
    },
    drag(key, mm) {
      const i = Number(key.slice(1)), a = A[i];
      if (key[0] === 'h') a.h = Math.hypot(mm[0] - a.p[0], mm[1] - a.p[1]) < 0.3 ? null : mm;
      else {
        const dx = mm[0] - a.p[0], dy = mm[1] - a.p[1];
        a.p = mm;
        if (a.h) a.h = [a.h[0] + dx, a.h[1] + dy];
      }
    },
    up() {},
    key(e) {
      if (e.key === 'Enter') { finish(false); return true; }
      if (e.key === 'Backspace' || e.key === 'Delete') { A.pop(); return true; }
      if (e.key === 'Escape') { endEdit(); done(null); return true; }
      return false;
    },
    draw(ctx, px) {
      ctx.save();
      ctx.lineWidth = 1.2 * px; ctx.strokeStyle = '#2f6fdd';
      const S = segs(false);
      if (S.length) {
        ctx.beginPath(); ctx.moveTo(...S[0][0]);
        for (const s of S) ctx.bezierCurveTo(...s[1], ...s[2], ...s[3]);
        ctx.stroke();
      }
      ctx.lineWidth = px; ctx.strokeStyle = 'rgba(47,111,221,.6)';
      for (const a of A) {
        if (!a.h) continue;
        const m = mirror(a);
        ctx.beginPath(); ctx.moveTo(...m); ctx.lineTo(...a.h); ctx.stroke();
        ctx.beginPath(); ctx.arc(a.h[0], a.h[1], 2.5 * px, 0, 7); ctx.stroke();
      }
      ctx.fillStyle = '#fff'; ctx.strokeStyle = '#2f6fdd';
      const r = 3 * px;
      for (const a of A) { ctx.fillRect(a.p[0] - r, a.p[1] - r, 2 * r, 2 * r); ctx.strokeRect(a.p[0] - r, a.p[1] - r, 2 * r, 2 * r); }
      ctx.restore();
    },
  };
  showBar('Curve', [['Finish', () => finish(false), 'Enter'], ['Close and finish', () => finish(true)],
    ['Cancel', () => { endEdit(); done(null); }, 'Esc']]);
  setPointEdit(pe);
  $('bed').focus();
}
