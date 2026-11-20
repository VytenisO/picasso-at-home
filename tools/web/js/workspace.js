// The Plot side panels: the Arrange panel (objects on the bed, transform fields, align, mirror, turn), the job
// settings (speeds, fill, paper), the layer list and saved projects. The document itself lives in doc.js.
import { api, post } from './api.js';
import { armPlace, armPoly, armFree } from './bed.js';
import { state, set, on, jobActive, prefs } from './store.js';
import { $, el, toast, ask, askText } from './ui.js';
import * as G from './geom.js';
import * as D from './doc.js';
import { hiddenLayers, toggleLayerHidden } from './ink.js';

export const runPreview = D.runPreview;

const ANCHORS = [[0, 0], [0.5, 0], [1, 0], [0, 0.5], [0.5, 0.5], [1, 0.5], [0, 1], [0.5, 1], [1, 1]];
const ANCHOR_NAMES = ['top-left', 'top', 'top-right', 'left', 'centre', 'right', 'bottom-left', 'bottom', 'bottom-right'];
let anchor = prefs('anchor') || [0, 0];

export function initWorkspace() {
  const f = $('placeForm');
  f.addEventListener('change', fromJobForm);
  f.addEventListener('submit', (e) => e.preventDefault());
  const xf = $('xfForm');
  xf.addEventListener('change', fromXfForm);
  xf.addEventListener('submit', (e) => e.preventDefault());
  $('anchorGrid').replaceChildren(...ANCHORS.map((a, i) => el('button', { type: 'button', role: 'radio',
    title: `Reference point: ${ANCHOR_NAMES[i]}`, 'aria-label': ANCHOR_NAMES[i], onclick: () => setAnchor(a) })));
  for (const b of $('xfBox').querySelectorAll('[data-act]')) b.onclick = () => act(b.dataset.act);
  $('undoBtn').onclick = D.undo;
  initHistory();
  $('lineTools').onchange = (e) => { const t = e.target.value; e.target.value = ''; if (t) lineTool(t); };
  $('redoBtn').onclick = D.redo;
  $('projForm').onsubmit = async (e) => {
    e.preventDefault();
    try { await D.saveProject($('projName').value); } catch (err) { toast(err.message, true); }
  };
  $('bedClear').onclick = async () => {
    if (!state.doc.objects.length) return;
    if (await ask('Clear the bed', 'Take every drawing off the bed? They stay in the Library, and Undo brings them back.', 'Clear')) D.clearBed();
  };
  on(['doc', 'preview', 'me'], fillJobForm);
  on(['doc', 'sel', 'job', 'naturals', 'units'], renderArrange);
  on(['doc', 'preview', 'job'], renderLayers);
  on(['doc', 'sel', 'files', 'job'], renderObjects);
  on(['projects', 'doc', 'job'], renderProjects);
  on(['doc', 'history', 'job'], renderTitle);
  on(['job'], () => { f.querySelectorAll('input, select').forEach((i) => { i.disabled = jobActive(); }); });
  setAnchor(anchor);
}

// The Library's "open": select the drawing if it is on the bed already, else put it there. ("Add" always adds.)
// The selection's reference point (Arrange's anchor), for Machine's "To the selection". -> [x, y] or null.
export function selectionAnchor() {
  const sel = D.selected();
  if (!sel.length) return null;
  return G.anchorPoint(G.bounds(sel), anchor).map(G.r2);
}

export async function openFile(id) {
  const there = state.doc ? state.doc.objects.filter((o) => o.file === id) : [];
  if (there.length) { D.select(there.map((o) => o.id)); return there[0]; }
  return D.addFile(id);
}

function renderTitle() {
  const d = state.doc;
  const n = d ? d.objects.length : 0;
  $('bedTitle').textContent = !n ? 'The bed is empty'
    : (d.name ? d.name + ': ' : '') + (n === 1 ? D.fileName(d.objects[0]) : `${n} objects`);
  $('undoBtn').disabled = !D.canUndo() || jobActive();
  $('redoBtn').disabled = !D.canRedo() || jobActive();
  $('histBtn').disabled = !D.canUndo() || jobActive();
}

// The undo history as a menu: pick a step to go back to just before it.
function initHistory() {
  const btn = $('histBtn'), menu = $('histMenu');
  const show = (open) => {
    menu.hidden = !open; btn.setAttribute('aria-expanded', String(open));
    if (!open) return;
    const fmt = (t) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const h = D.history();
    menu.replaceChildren(el('p', { class: 'hint', text: 'Back to just before:' }), ...h.slice(0, 60).map((e, n) =>
      el('button', { type: 'button', role: 'menuitem', onclick: () => { show(false); D.undoTo(n + 1); } },
        el('span', { text: e.what }), el('small', { text: fmt(e.t) }))));
  };
  btn.onclick = (e) => { e.stopPropagation(); show(menu.hidden); };
  document.addEventListener('pointerdown', (e) => { if (!menu.hidden && !menu.contains(e.target) && e.target !== btn) show(false); });
  menu.addEventListener('keydown', (e) => { if (e.key === 'Escape') { show(false); btn.focus(); } });
}

// ------------------------------------------------------------------ arrange
function setAnchor(a) {
  anchor = a;
  prefs('anchor', a);
  const i = ANCHORS.findIndex((x) => x[0] === a[0] && x[1] === a[1]);
  [...$('anchorGrid').children].forEach((b, k) => b.setAttribute('aria-checked', String(k === i)));
  $('toAnchor').textContent = `To canvas ${ANCHOR_NAMES[i]}`;
  renderArrange();
}

function renderArrange() {
  const sel = D.selected();
  const box = $('xfBox'), xf = $('xfForm');
  box.hidden = !sel.length;
  if (!sel.length) return;
  const locked = jobActive();
  box.querySelectorAll('input, button').forEach((x) => { x.disabled = locked; });
  const b = G.bounds(sel);
  const [ax, ay] = G.anchorPoint(b, anchor);
  const one = sel.length === 1 ? sel[0] : null;
  const val = (name, v) => { if (document.activeElement !== xf[name]) xf[name].value = v; };
  val('x', G.r2(ax)); val('y', G.r2(ay));
  // One unturned object: its own size. Otherwise: the bounds of what is selected.
  const own = one && one.rot % 90 === 0;
  val('w', G.r2(own && one.rot % 180 === 0 ? one.w : own ? one.h : b.w));
  val('h', G.r2(own && one.rot % 180 === 0 ? one.h : own ? one.w : b.h));
  val('rot', one ? G.r2(one.rot) : '');
  xf.rot.disabled = locked || !one;
  xf.rot.placeholder = one ? '' : 'one only';
  $('alignTo').textContent = sel.length > 1 ? 'Align to each other' : 'Align to canvas';
  $('shapeRow').hidden = !(state.me && state.me.features && state.me.features.shapes);
  renderInk(sel, locked);
  const allLocked = sel.every((o) => o.locked);
  $('lockBtn').textContent = allLocked ? 'Unlock' : 'Lock';
  box.querySelector('[data-act=group]').disabled = locked || sel.length < 2;
  box.querySelector('[data-act=ungroup]').disabled = locked || !sel.some((o) => o.grp);
  $('measureLine').textContent = measure(sel);
  for (const k of ['union', 'subtract', 'intersect']) box.querySelector(`[data-act=${k}]`).disabled = locked || sel.length < 2;
  for (const k of ['distx', 'disty', 'distcx', 'distcy']) box.querySelector(`[data-act=${k}]`).disabled = locked || sel.length < 3;
  for (const k of ['abutx', 'abuty', 'samew', 'sameh']) box.querySelector(`[data-act=${k}]`).disabled = locked || sel.length < 2;
}

// Paths and line length of the selection, from the drawings' natural geometry and each object's transform.
function measure(sel) {
  let paths = 0, len = 0, missing = false;
  for (const o of sel) {
    const n = state.naturals[o.file];
    if (!n) { missing = true; continue; }
    const [a, b, c, d] = G.matrix(o, n.geom);
    for (const s of n.strokes || []) {
      paths++;
      for (let i = 1; i < s.pts.length; i++) {
        const dx = s.pts[i][0] - s.pts[i - 1][0], dy = s.pts[i][1] - s.pts[i - 1][1];
        len += Math.hypot(a * dx + c * dy, b * dx + d * dy);
      }
    }
  }
  if (!paths) return missing ? '' : 'No lines.';
  return `${paths} path${paths === 1 ? '' : 's'}, ${len >= 1000 ? `${(len / 1000).toFixed(2)} m` : G.fmtLen(len)} of line` +
    (sel.some((o) => o.locked) ? '; locked' : '') + (sel.some((o) => o.grp) ? '; grouped' : '');
}

function fromXfForm(e) {
  const xf = $('xfForm');
  const name = e.target.name;
  if (name === 'lock' || !name) return;
  const v = G.parseLength(xf[name].value);
  if (!Number.isFinite(v)) { toast(`Cannot read "${xf[name].value}". Type a number, arithmetic (100/3) or a unit (2in, 5cm).`, true); renderArrange(); return; }
  D.editSel((objs) => {
    const b = G.bounds(objs);
    const [ax, ay] = G.anchorPoint(b, anchor);
    if (name === 'x' || name === 'y') {
      const dx = name === 'x' ? v - ax : 0, dy = name === 'y' ? v - ay : 0;
      for (const o of objs) { o.x += dx; o.y += dy; }
    } else if (name === 'w' || name === 'h') {
      if (v < 0.1) { toast('Width and height must be at least 0.1 mm.', true); return false; }
      const cur = name === 'w' ? b.w : b.h;
      if (cur < 1e-6) return false;
      const k = v / cur;
      const keep = xf.lock.checked;
      G.scaleAbout(objs, ax, ay, name === 'w' || keep ? k : 1, name === 'h' || keep ? k : 1);
    } else if (name === 'rot' && objs.length === 1) {
      const o = objs[0];
      const before = G.bounds([o]);
      const [px, py] = G.anchorPoint(before, [0.5, 0.5]);
      G.rotateAbout([o], px, py, v - o.rot);
    }
  }, { coalesce: 'xf-' + name });
  renderArrange();
}

function act(a) {
  if (a === 'rot90') D.rotateSel(90);
  else if (a === 'rot-90') D.rotateSel(-90);
  else if (a === 'mirx') D.mirrorSel('x');
  else if (a === 'miry') D.mirrorSel('y');
  else if (a === 'distx') D.distribute('x');
  else if (a === 'distcx') D.distributeCentres('x');
  else if (a === 'distcy') D.distributeCentres('y');
  else if (a === 'abutx') D.abut('x');
  else if (a === 'abuty') D.abut('y');
  else if (a === 'samew') D.sameSize('w');
  else if (a === 'sameh') D.sameSize('h');
  else if (a === 'circle') openCircle();
  else if (a === 'disty') D.distribute('y');
  else if (a === 'anchor') D.toCanvasAnchor(anchor);
  else if (a === 'fit') D.fitCanvas();
  else if (a === 'dup') D.duplicateSel();
  else if (a === 'array') openArray();
  else if (['union', 'subtract', 'intersect'].includes(a)) shapeOp(a);
  else if (a === 'join') joinLines();
  else if (a === 'reverse') shapeOp('reverse');
  else if (a === 'split') breakApart();
  else if (a === 'ink-own') D.editSel((objs) => { for (const o of objs) delete o.ink; }, { allowLocked: true });
  else if (a === 'group') D.group();
  else if (a === 'ungroup') D.ungroup();
  else if (a === 'lock') D.toggleLock();
  else if (a === 'offset') openOffset();
  else if (a === 'del') D.removeSel();
  else D.align(a);
}

function renderObjects() {
  const list = $('objList');
  const d = state.doc;
  const objs = d ? d.objects : [];
  $('objEmpty').hidden = objs.length > 0;
  const locked = jobActive();
  list.replaceChildren(...objs.map((o, i) => {
    const sel = state.sel.includes(o.id);
    const name = el('button', { class: 'link', text: `${i + 1}. ${D.fileName(o)}`, title: 'Select (Shift: add to the selection)',
      onclick: (e) => D.select(o.id, { add: e.shiftKey || e.ctrlKey || e.metaKey, toggle: e.shiftKey || e.ctrlKey || e.metaKey }) });
    const size = el('small', { class: 'muted', text: `${G.r2(o.w)} x ${G.r2(o.h)}${o.rot ? `, ${G.r2(o.rot)} deg` : ''}${G.outside(o) ? ', outside' : ''}` });
    const mv = (dir, label, title) => el('button', { class: 'icon', text: label, title, disabled: locked,
      onclick: () => { D.select(o.id); D.reorder(dir); } });
    return el('li', { class: (sel ? 'selected' : '') + (G.outside(o) ? ' bad' : '') }, name, size,
      el('span', { class: 'spacer' }),
      mv('down', '▲', 'Plot earlier (within each pen)'), mv('up', '▼', 'Plot later (within each pen)'));
  }));
}

// Join replaces the selection with one drawing whose touching ends are joined; it says how many strokes went.
async function joinLines() {
  const sel = D.selected();
  if (D.lockedSel()) return;
  const t = await askText('Join lines', 'Ends this close (mm) are joined, in the same colour only. Where three or more ends meet nothing is joined.', '0.2', 'Join');
  if (t === null) return;
  const tol = Number(t);
  if (!(tol >= 0.01 && tol <= 5)) { toast('The join distance must be 0.01 to 5 mm.', true); return; }
  try {
    const r = await post('/api/shapes', { op: 'join', objects: sel, tolerance: tol });
    await refreshFiles();
    await D.addFile(r.file.id, { at: [r.x, r.y], replace: sel.map((o) => o.id) });
    toast(r.after < r.before ? `${r.before} strokes became ${r.after}.` : 'No touching ends found: nothing was joined.');
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ line tools (Arrange, Line tools...)
// Server-side edits of the selected objects' lines: the result is a new drawing that takes their place (Undo
// brings them back). A few need clicks on the bed first.
async function lineOp(op, extra = {}, { add = false } = {}) {
  const sel = D.selected();
  if (!add && D.lockedSel()) return;
  try {
    const r = await post('/api/shapes', { op, objects: sel, ...extra });
    await refreshFiles();
    await D.addFile(r.file.id, { at: [r.x, r.y], replace: add ? [] : (r.replace || sel.map((o) => o.id)) });
  } catch (e) { toast(e.message, true); }
}

function clicks(n, prompts, done) {
  const pts = [];
  const next = () => {
    toast(prompts[pts.length]);
    armPlace((x, y) => { pts.push([x, y]); if (pts.length < n) next(); else done(pts); });
  };
  next();
}

const selCentre = () => { const b = G.bounds(D.selected()); return [b.x0 + b.w / 2, b.y0 + b.h / 2]; };

async function lineTool(t) {
  const sel = D.selected();
  if (!sel.length) { toast('Select the objects first.', true); return; }
  const ask = async (title, text, def) => { const v = await askText(title, text, def, 'OK'); return v === null ? null : G.parseLength(v); };
  if (t === 'close') { const v = await ask('Close gaps', 'Close strokes whose ends are this close (mm):', '1'); if (v > 0) lineOp('close', { tolerance: v }); }
  else if (t === 'smooth') lineOp('smooth', { passes: 2 });
  else if (t === 'fillet') { const v = await ask('Round corners', 'Corner radius (mm), for closed shapes:', '2'); if (v > 0) lineOp('fillet', { radius: v }); }
  else if (t === 'hull') lineOp('hull', {}, { add: true });
  else if (t === 'cut-out' || t === 'cut-in') {
    if (sel.length < 2) { toast('Select the cutter first, then Shift-click the objects to cut.', true); return; }
    lineOp('cut', { keep: t === 'cut-in' ? 'inside' : 'outside' });
  } else if (t === 'penwidth') {
    const st = state.doc.settings, pens = state.me.pens || {};
    const pen = (st.layers || []).map((l) => pens[l.pen]).find((p) => p && p.width_mm);
    const v = await ask('Pen width', 'Closed shapes move inward by half this line width, so the inked edge lands on the drawn one (mm):', String(pen ? pen.width_mm : 0.5));
    if (v > 0) lineOp('penwidth', { width: v });
  } else if (t === 'trim') {
    toast('Click the piece of line to remove (it is cut where lines cross).');
    armPlace((x, y) => lineOp('trim', { x, y, tolerance: 4 / 1 }));
  } else if (t === 'shear') {
    const v = await ask('Shear', 'Slant to the right by this angle (degrees, negative: left):', '15');
    if (v === null || !Number.isFinite(v) || Math.abs(v) >= 80) return;
    const k = Math.tan(v * Math.PI / 180), [, cy] = selCentre();
    lineOp('affine', { m: [1, 0, -k, 1, k * cy, 0] });
  } else if (t === 'mirror-line') {
    clicks(2, ['Click the first point of the mirror line.', 'Click the second point.'], ([p, q]) => {
      const th = Math.atan2(q[1] - p[1], q[0] - p[0]), a = Math.cos(2 * th), b = Math.sin(2 * th);
      lineOp('affine', { m: [a, b, b, -a, p[0] - a * p[0] - b * p[1], p[1] - b * p[0] + a * p[1]] });
    });
  } else if (t === 'true-size') {
    clicks(2, ['Click the first of two points whose real distance you know.', 'Click the second point.'], async ([p, q]) => {
      const d = Math.hypot(q[0] - p[0], q[1] - p[1]);
      const v = await ask('True size', `The points are ${G.r2(d)} mm apart. Their real distance (mm):`, String(G.r2(d)));
      if (!(v > 0) || d < 0.01) return;
      const k = v / d;
      lineOp('affine', { m: [k, 0, 0, k, p[0] - k * p[0], p[1] - k * p[1]] });
    });
  } else if (t === 'taper') openTaper();
  else if (t === 'along') copyAlong();
  else if (t === 'pack') packCanvas();
}

function openTaper() {
  const d = $('taperDlg'), f = $('taperForm');
  d.returnValue = '';
  d.onclose = () => {
    if (d.returnValue !== 'ok') return;
    const b = G.bounds(D.selected()), cx = b.x0 + b.w / 2, cy = b.y0 + b.h / 2;
    const tw = b.w * Number(f.top.value) / 200, bw = b.w * Number(f.bottom.value) / 200;
    const lh = b.h * Number(f.left.value) / 200, rh = b.h * Number(f.right.value) / 200;
    lineOp('warp', { quad: [[cx - tw, cy - lh], [cx + tw, cy - rh], [cx + bw, cy + rh], [cx - bw, cy + lh]] });
  };
  d.showModal();
}

// Copy along a path: the first object selected is the path; the others are copied evenly along it.
async function copyAlong() {
  const sel = D.selected();
  const path = sel.find((o) => o.id === state.sel[0]), items = sel.filter((o) => o !== path);
  if (!path || !items.length) { toast('Select the path first, then Shift-click what to copy along it.', true); return; }
  const n = state.naturals[path.file];
  if (!n || !n.strokes.length) return;
  const m = G.matrix(path, n.geom);
  const pts = n.strokes.reduce((a, s) => (s.pts.length > a.length ? s.pts : a), []).map(([x, y]) => [m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]]);
  const t = await askText('Copy along a path', 'How many copies along the path (its longest line)?', '8', 'Place');
  const count = Math.round(Number(t));
  if (!(count >= 2 && count <= 60)) return;
  const seg = pts.slice(1).map((p, i) => Math.hypot(p[0] - pts[i][0], p[1] - pts[i][1]));
  const total = seg.reduce((a, b) => a + b, 0);
  const closed = Math.hypot(pts[0][0] - pts[pts.length - 1][0], pts[0][1] - pts[pts.length - 1][1]) < 0.2;
  const at = (dist) => {
    for (let i = 0; i < seg.length; i++) {
      if (dist <= seg[i] || i === seg.length - 1) {
        const f = seg[i] ? Math.min(1, dist / seg[i]) : 0, [a, b] = [pts[i], pts[i + 1]];
        return [[a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f], Math.atan2(b[1] - a[1], b[0] - a[0]) * 180 / Math.PI];
      }
      dist -= seg[i];
    }
    return [pts[0], 0];
  };
  const ib = G.bounds(items), ic = [ib.x0 + ib.w / 2, ib.y0 + ib.h / 2];
  const placed = [];
  for (let k = 0; k < count; k++) {
    const [[x, y], ang] = at(total * k / (closed ? count : count - 1));
    placed.push({ dx: x - ic[0], dy: y - ic[1], ang, cx: x, cy: y });
  }
  D.placeCopies(items, placed);
}

// Pack: the selection laid out in rows on the canvas from its top-left, tallest first, 3 mm apart.
function packCanvas() {
  const c = G.canvasRect().canvas, gap = 3;
  D.editSel((objs) => {
    const bs = objs.map((o) => ({ o, b: G.bounds([o]) })).sort((p, q) => q.b.h - p.b.h);
    let x = c[0], y = c[1], row = 0;
    for (const p of bs) {
      if (x + p.b.w > c[2] && x > c[0]) { x = c[0]; y += row + gap; row = 0; }
      p.o.x += x - p.b.x0; p.o.y += y - p.b.y0;
      x += p.b.w + gap; row = Math.max(row, p.b.h);
    }
    if (y + row > c[3]) toast('They do not all fit on the canvas: the last row runs past the bottom.', true);
  });
}

// Break apart: the selection is replaced by one object per stroke, as one undoable edit.
async function breakApart() {
  const sel = D.selected();
  if (D.lockedSel()) return;
  try {
    const r = await post('/api/shapes', { op: 'split', objects: sel });
    await refreshFiles();
    await D.replaceWithPieces(sel.map((o) => o.id), r.pieces);
    toast(`${r.pieces.length} pieces.`);
  } catch (e) { toast(e.message, true); }
}

function setInk(hex) {
  D.editSel((objs) => { for (const o of objs) o.ink = hex.toLowerCase(); }, { allowLocked: true });
}

// The ink colours on the bed now (from the last preview), as one-click targets for the selection.
function renderInk(sel, locked) {
  const pv = state.preview;
  const cols = [...new Set(((pv && pv.file_layers) || []).map((l) => (l.color || '').toLowerCase()).filter((c) => /^#[0-9a-f]{6}$/.test(c)))];
  const cur = new Set(sel.map((o) => o.ink || ''));
  $('inkSwatches').replaceChildren(...cols.map((c) => {
    const b = el('button', { type: 'button', class: 'swatch-btn', title: `Draw in ${c} (its layer and pen)`, 'aria-label': `Colour ${c}`,
      'aria-pressed': String(cur.size === 1 && cur.has(c)), disabled: locked, onclick: () => setInk(c) });
    b.style.background = c;
    return b;
  }));
  const one = cur.size === 1 ? [...cur][0] : '';
  if (document.activeElement !== $('inkPick')) $('inkPick').value = one || '#000000';
  $('xfBox').querySelector('[data-act=ink-own]').disabled = locked || !sel.some((o) => o.ink);
}

// Union, subtract, intersect replace the selection with the result (Undo brings it back); offset adds an outline.
async function shapeOp(op, extra = {}) {
  const sel = D.selected();
  if (D.lockedSel()) return;
  try {
    const r = await post('/api/shapes', { op, objects: sel, ...extra });
    await refreshFiles();
    await D.addFile(r.file.id, { at: [r.x, r.y], replace: op === 'offset' ? [] : sel.map((o) => o.id) });
  } catch (e) { toast(e.message, true); }
}

function openOffset() {
  const d = $('offsetDlg'), f = $('offsetForm');
  d.returnValue = '';
  d.onclose = () => {
    if (d.returnValue !== 'ok') return;
    shapeOp('offset', { distance: Number(f.distance.value), join: f.join.value });
  };
  d.showModal();
}

function openCircle() {
  const d = $('circleDlg'), f = $('circleForm');
  d.returnValue = '';
  d.onclose = () => {
    if (d.returnValue !== 'ok') return;
    D.circularSel(Math.round(Math.max(2, Math.min(36, Number(f.n.value) || 6))), Math.max(1, Math.min(200, Number(f.r.value) || 30)), f.turn.checked);
  };
  d.showModal();
}

function openArray() {
  const d = $('arrayDlg'), f = $('arrayForm');
  const b = G.bounds(D.selected());
  const c = G.canvasRect().canvas;
  const note = () => {
    const cols = Number(f.cols.value), rows = Number(f.rows.value), gx = Number(f.gx.value), gy = Number(f.gy.value);
    const w = cols * b.w + (cols - 1) * gx, h = rows * b.h + (rows - 1) * gy;
    const fits = b.x0 + w <= c[2] + 0.5 && b.y0 + h <= c[3] + 0.5;
    $('arrayNote').textContent = `${cols * rows} in all, ${G.r2(w)} x ${G.r2(h)} mm` + (fits ? '.' : ': runs past the canvas from here.');
    $('arrayNote').className = 'hint' + (fits ? '' : ' warn-text');
  };
  f.oninput = note;
  note();
  d.returnValue = '';
  d.onclose = () => {
    if (d.returnValue !== 'ok') return;
    const n = (k, lo, hi) => Math.max(lo, Math.min(hi, Number(f[k].value) || 0));
    D.arraySel(Math.round(n('cols', 1, 20)), Math.round(n('rows', 1, 20)), n('gx', -100, 200), n('gy', -100, 200),
      n('stagger', -200, 200), f.alt.checked);
  };
  d.showModal();
}

// The bed's text tool: click where the text goes, type it, and it is added to the library and placed there.
export function initTextTool() {
  initShapeTool();
  const btn = $('textTool');
  btn.onclick = () => {
    const on = btn.getAttribute('aria-pressed') !== 'true';
    btn.setAttribute('aria-pressed', String(on));
    armPlace(on ? placeText : null);
    if (on) toast('Click on the bed where the text should start.');
  };
  on('job', () => { btn.disabled = jobActive(); });
}

// Shape tool: the dialog first, then a click (or clicks, for a line) on the bed.
function initShapeTool() {
  const btn = $('shapeTool'), d = $('shapeDlg'), f = $('shapeForm');
  const showFor = () => { for (const x of f.querySelectorAll('[data-for]')) x.hidden = !x.dataset.for.split(' ').includes(f.kind.value); };
  f.kind.onchange = showFor;
  const saved = prefs('shapeTool');
  if (saved) for (const [k, v] of Object.entries(saved)) if (f[k]) f[k].value = v;
  showFor();
  btn.onclick = () => {
    if (btn.getAttribute('aria-pressed') === 'true') { btn.setAttribute('aria-pressed', 'false'); armPlace(null); armPoly(null); return; }
    d.returnValue = '';
    d.onclose = () => {
      if (d.returnValue !== 'ok') return;
      const p = { kind: f.kind.value, w: Number(f.w.value), h: Number(f.h.value), r: Number(f.r.value), n: Number(f.n.value), inner: Number(f.inner.value) };
      prefs('shapeTool', p);
      btn.setAttribute('aria-pressed', 'true');
      const make = async (body, at) => {
        btn.setAttribute('aria-pressed', 'false');
        try {
          const r = await post('/api/shape_new', body);
          await refreshFiles();
          await D.addFile(r.file.id, { at: r.at || at });
        } catch (e) { toast(e.message, true); }
      };
      if (p.kind === 'free') {
        toast('Drag on the bed to draw.');
        armFree((pts) => { if (pts) make({ kind: 'poly', pts, closed: false }); else btn.setAttribute('aria-pressed', 'false'); });
      } else if (p.kind === 'poly') {
        toast('Click the points. Enter or double-click finishes; Esc cancels.');
        armPoly((pts, closed) => { if (pts) make({ kind: 'poly', pts, closed }); else btn.setAttribute('aria-pressed', 'false'); });
      } else {
        toast('Click on the bed where the top-left corner goes.');
        armPlace((x, y) => make(p, [x, y]));
      }
    };
    d.showModal();
  };
  on('job', () => { btn.disabled = jobActive(); });
}

function placeText(x, y) {
  $('textTool').setAttribute('aria-pressed', 'false');
  const d = $('textDlg'), f = $('textForm');
  f.text.value = '';
  d.returnValue = '';
  d.onclose = async () => {
    if (d.returnValue !== 'ok' || !f.text.value.trim()) return;
    try {
      const r = await post('/api/text_single', { text: f.text.value, font: f.font.value, xheight: Number(f.xheight.value),
        align: f.align.value });
      await refreshFiles();
      await D.addFile(r.file.id, { at: [x, y] });
    } catch (e) { toast(e.message, true); }
  };
  d.showModal();
  f.text.focus();
}

// ------------------------------------------------------------------ job settings
function fillJobForm() {
  const f = $('placeForm'), me = state.me;
  if (!me || !state.doc) return;
  const st = state.doc.settings;
  const L = me.limits;
  $('speedLbl').textContent = `Drawing (mm/s, max ${L.speed.max})`;
  $('travelLbl').textContent = `Pen up (mm/s, max ${L.travel_speed.max})`;
  f.speed.min = L.speed.min; f.speed.max = L.speed.max;
  f.travel_speed.min = L.travel_speed.min; f.travel_speed.max = L.travel_speed.max;
  const [px0, py0, pw, ph] = me.machine.paper;
  $('paperHint').textContent = `Configured sheet: ${pw} x ${ph} mm with its top-left at (${px0}, ${py0}) mm on the bed and a ` +
    `${me.machine.margin} mm margin. The corner stays where it is; only the size can change here.`;
  const set_ = (name, v) => { if (document.activeElement !== f[name]) f[name].value = v ?? ''; };
  set_('speed', st.speed); set_('travel_speed', st.travel_speed);
  set_('fill', st.fill || 0); set_('fill_angle', st.fill_angle ?? 45);
  set_('copies', st.copies || 1);
  if (document.activeElement !== f.finish) f.finish.value = st.finish || 'start';
  if (document.activeElement !== f.sweep) f.sweep.value = st.sweep || 'none';
  f.optimize.checked = st.optimize !== false;
  f.dedupe.checked = !!st.dedupe;
  f.by_object.checked = !!st.by_object;
  f.keep_motors.checked = !!st.keep_motors;
  set_('simplify', st.simplify ?? 0.05);
  f.corner_starts.checked = !!st.corner_starts;
  if (document.activeElement !== f.trace_shape) f.trace_shape.value = st.trace_shape || 'box';
  f.trace_repeat.checked = !!st.trace_repeat;
  f.sim_speed_ui.value = String(prefs('simSpeed') || '');
  f.inner_first.checked = !!st.inner_first;
  set_('approach', st.approach || 0); set_('start_delay', st.start_delay || 0); set_('end_delay', st.end_delay || 0);
  f.paper_custom.checked = !!st.paper;
  set_('paper_w', st.paper ? st.paper.width : pw);
  set_('paper_h', st.paper ? st.paper.height : ph);
  f.paper_w.disabled = f.paper_h.disabled = !st.paper || jobActive();
  const sz = st.paper ? `${st.paper.width}x${st.paper.height}` : '';
  f.paper_preset.value = [...f.paper_preset.options].some((o) => o.value === sz) ? sz : '';
}

function fromJobForm(e) {
  const f = $('placeForm');
  const num = (n) => (f[n].value === '' ? null : Number(f[n].value));
  const patch = { speed: num('speed'), travel_speed: num('travel_speed'), fill: num('fill') || 0, fill_angle: num('fill_angle') ?? 45,
    optimize: f.optimize.checked, dedupe: f.dedupe.checked, finish: f.finish.value === 'start' ? null : f.finish.value,
    sweep: f.sweep.value === 'none' ? null : f.sweep.value, by_object: f.by_object.checked || null,
    keep_motors: f.keep_motors.checked || null };
  const simp = num('simplify');
  if (simp === null || !(simp >= 0 && simp <= 2)) { toast('Simplify: 0 to 2 mm.', true); f.simplify.value = st.simplify ?? 0.05; return; }
  patch.simplify = Math.abs(simp - 0.05) < 1e-9 ? null : simp;
  patch.corner_starts = f.corner_starts.checked || null;
  patch.trace_shape = f.trace_shape.value === 'box' ? null : f.trace_shape.value;
  patch.trace_repeat = f.trace_repeat.checked || null;
  prefs('simSpeed', f.sim_speed_ui.value ? Number(f.sim_speed_ui.value) : null);
  patch.inner_first = f.inner_first.checked || null;
  for (const [k, hi, label] of [['approach', 10, 'Approach'], ['start_delay', 60, 'Delay before'], ['end_delay', 60, 'Delay after']]) {
    const v = num(k) || 0;
    if (!(v >= 0 && v <= hi)) { toast(`${label}: 0 to ${hi}.`, true); f[k].value = st[k] || 0; return; }
    patch[k] = v || null;
  }
  const st = state.doc.settings;
  const cp = num('copies');
  if (!Number.isInteger(cp) || cp < 1 || cp > 50) {
    toast('Copies: a whole number from 1 to 50.', true);
    f.copies.value = st.copies || 1;
    return;
  }
  patch.copies = cp > 1 ? cp : null;
  if (patch.fill && (patch.fill < 0.2 || patch.fill > 50)) {
    toast('Fill spacing must be 0 (off) or between 0.2 and 50 mm.', true);
    f.fill.value = st.fill || 0;
    return;
  }
  const L = state.me.limits;
  for (const [k, lim] of [['speed', L.speed], ['travel_speed', L.travel_speed]]) {
    if (patch[k] === null || patch[k] < lim.min || patch[k] > lim.max) {
      toast(`${k === 'speed' ? 'Drawing' : 'Pen-up'} speed must be between ${lim.min} and ${lim.max} mm/s.`, true);
      f[k].value = st[k];
      return;
    }
  }
  if (e && e.target === f.paper_preset) {
    if (!f.paper_preset.value) return;
    const [w, h] = f.paper_preset.value.split('x').map(Number);
    patch.paper = { width: w, height: h };
  } else if (e && e.target === f.paper_custom && f.paper_custom.checked) {
    const [, , pw, ph] = state.me.machine.paper;
    patch.paper = { width: pw, height: ph };
  } else {
    patch.paper = f.paper_custom.checked ? { width: num('paper_w'), height: num('paper_h') } : null;
  }
  D.setSettings(patch, { coalesce: 'job-' + (e && e.target.name) });
}

// ------------------------------------------------------------------ layers
// One card per ink layer, in plot order: name, pen, speed, pressure, passes, on/off, and arrows to change the pen
// order. Below them, the filled shapes per fill colour: job default, hatch, contour rings or off.
function layerRows() {
  const pv = state.preview;
  const fileLayers = (pv && pv.file_layers) || [];
  const st = state.doc.settings;
  const rows = fileLayers.map((fl, i) => ({ fl, i, s: (st.layers || []).find((x) => x.key === fl.key) || { key: fl.key, enabled: true } }));
  const rank = (r) => (r.s.rank === null || r.s.rank === undefined ? r.i : r.s.rank);
  return rows.sort((a, b) => rank(a) - rank(b) || a.i - b.i);
}

function renderLayers() {
  const list = $('layerList');
  const pv = state.preview;
  const fileLayers = (pv && pv.file_layers) || [];
  $('layerEmpty').hidden = fileLayers.length > 0;
  $('layerEmpty').textContent = state.doc && state.doc.objects.length ? 'Checking the layers...' : 'Add a drawing to see its layers.';
  if (list.contains(document.activeElement) || $('fillList').contains(document.activeElement)) return;
  renderFills();
  if (!fileLayers.length) { list.replaceChildren(); return; }
  const L = state.me.limits;
  const pens = state.me.pens || {};
  const locked = jobActive();
  const rows = layerRows();
  // every change rewrites the whole list, in plot order, so ranks stay 0..n-1
  const upd = (key, patch, coalesce) => D.edit((d) => {
    const cur = d.settings.layers || [];
    d.settings.layers = rows.map((r, n) => ({ ...(cur.find((y) => y.key === r.fl.key) || { key: r.fl.key, enabled: true, pen: null, speed: null, pressure: null }), rank: n }));
    Object.assign(d.settings.layers.find((x) => x.key === key), patch);
  }, { coalesce });
  const move = (n, dir) => D.edit((d) => {
    const cur = d.settings.layers || [];
    const order = rows.map((r) => r.fl.key);
    const j = n + dir;
    if (j < 0 || j >= order.length) return false;
    [order[n], order[j]] = [order[j], order[n]];
    d.settings.layers = order.map((k, r) => ({ ...(cur.find((y) => y.key === k) || { key: k, enabled: true, pen: null, speed: null, pressure: null }), rank: r }));
  });
  list.replaceChildren(...rows.map(({ fl, s }, n) => {
    // choosing a pen clears the layer's own speed and pressure: it follows the pen profile (empty fields show it)
    const pen = el('select', { 'aria-label': `Pen for layer ${n + 1}`, disabled: locked, onchange: (e) => {
      upd(fl.key, { pen: e.target.value || null, speed: null, pressure: null });
    } }, el('option', { value: '', text: 'Pen: not set' }),
      Object.entries(pens).map(([nm, p]) => el('option', { value: nm, text: p.colour ? `${nm} (${p.colour})` : nm })));
    pen.value = s.pen || '';
    const prof = s.pen ? pens[s.pen] || {} : {};
    const num = (key, lim, ph, label) => {
      if (prof[key] !== undefined && prof[key] !== null) ph = `${prof[key]} (pen)`;
      const i2 = el('input', { type: 'number', min: lim.min, max: lim.max, step: key === 'pressure' ? 0.05 : 1,
        placeholder: ph, 'aria-label': label, disabled: locked, title: `${label}: ${lim.min} to ${lim.max}` });
      i2.value = s[key] ?? '';
      i2.onchange = () => {
        const v = i2.value === '' ? null : Number(i2.value);
        if (v !== null && (v < lim.min || v > lim.max)) {
          toast(`${label} must be between ${lim.min} and ${lim.max}.`, true); i2.value = s[key] ?? ''; return;
        }
        upd(fl.key, { [key]: v });
      };
      return el('label', { class: 'field' }, el('span', { text: label }), i2);
    };
    const passes = el('select', { 'aria-label': `Passes for layer ${n + 1}`, disabled: locked,
      title: 'Draw every stroke this many times (darker lines). An open stroke is drawn back over itself.',
      onchange: (e) => upd(fl.key, { passes: Number(e.target.value) }) },
    [1, 2, 3, 4, 5].map((k) => el('option', { value: k, text: k === 1 ? '1 pass' : `${k} passes` })));
    passes.value = String(s.passes || 1);
    const on_ = el('input', { type: 'checkbox', disabled: locked, 'aria-label': `Plot layer ${n + 1}` });
    on_.checked = s.enabled !== false;
    on_.onchange = () => upd(fl.key, { enabled: on_.checked });
    const swatch = el('span', { class: 'swatch' });
    swatch.style.background = fl.color || '#888';
    const name = el('input', { class: 'layer-name', maxlength: 40, placeholder: fl.name, 'aria-label': `Name of layer ${n + 1}`,
      disabled: locked, title: 'A name for this layer (shown at the pen change)' });
    name.value = s.name || '';
    name.onchange = () => upd(fl.key, { name: name.value.trim() || null }, 'name-' + fl.key);
    const arrow = (dir, txt, title) => el('button', { class: 'icon', type: 'button', text: txt, title, disabled: locked || n + dir < 0 || n + dir >= rows.length,
      onclick: () => move(n, dir) });
    const hidden = hiddenLayers().has(fl.key);
    const eye = el('button', { type: 'button', class: 'icon eye', text: hidden ? 'Show' : 'Hide', 'aria-pressed': String(hidden),
      title: 'Hide or show this layer on the bed only: it still plots', 'aria-label': `${hidden ? 'Show' : 'Hide'} layer ${n + 1} in the view`,
      onclick: () => { toggleLayerHidden(fl.key); set({ naturals: state.naturals }); renderLayers(); } });
    return el('div', { class: 'layer' + (s.enabled === false ? ' off' : '') + (hidden ? ' hidden-view' : '') },
      el('div', { class: 'layer-head' }, on_, swatch, el('b', { text: `${n + 1}.` }), name, eye,
        arrow(-1, '▲', 'Plot this pen earlier'), arrow(1, '▼', 'Plot this pen later')),
      el('div', { class: 'layer-grid' }, el('label', { class: 'field' }, el('span', { text: 'Pen' }), pen),
        num('speed', L.speed, String(state.doc.settings.speed ?? L.speed.default), 'Speed (mm/s)'),
        num('pressure', { min: 0, max: L.pressure.max }, 'from file', 'Pressure')),
      el('div', { class: 'layer-foot' }, passes, el('small', { class: 'muted', text: `${fl.strokes} strokes` }),
        el('button', { type: 'button', class: 'link', text: 'Select', title: 'Select the objects that draw in this layer',
          onclick: () => selectLayer(fl) }),
        el('button', { type: 'button', class: 'link', text: 'Copy', title: 'Copy pen, speed, pressure and passes', disabled: locked,
          onclick: () => { prefs('layerClip', { pen: s.pen || null, speed: s.speed ?? null, pressure: s.pressure ?? null, passes: s.passes || 1 }); toast('Layer settings copied.'); renderLayers(); } }),
        el('button', { type: 'button', class: 'link', text: 'Paste', title: 'Paste the copied pen, speed, pressure and passes', disabled: locked || !prefs('layerClip'),
          onclick: () => { const c = prefs('layerClip'); if (c) upd(fl.key, { pen: c.pen && pens[c.pen] ? c.pen : null, speed: c.speed, pressure: c.pressure, passes: c.passes }); } })),
      layerMore(fl, s, locked, upd));
  }));
  list.append(el('button', { type: 'button', id: 'legendBtn', text: 'Add a pen legend', disabled: locked,
    title: 'Single-line text listing each layer and its pen, placed at the bottom-left of the canvas', onclick: () => addLegend(rows) }));
}

// The less common pen behaviours of a layer, folded away: dashes, dwell, overdraw past the seam, pressure per pass.
function layerMore(fl, s, locked, upd) {
  const open = !!(s.dash || s.dwell_ms || s.overdraw || s.pressure_step);
  const f = (k, label, min, max, step, title, get = (v) => v, put = (v) => v) => {
    const i = el('input', { type: 'number', min, max, step, disabled: locked, placeholder: 'off', title, 'aria-label': `${label}, layer ${fl.name}` });
    const cur = get(s[k]);
    i.value = cur ?? '';
    i.onchange = () => {
      if (i.value === '') { upd(fl.key, { [k]: null }); return; }
      const v = Number(i.value);
      if (!(v >= min && v <= max)) { toast(`${label}: ${min} to ${max}.`, true); i.value = cur ?? ''; return; }
      upd(fl.key, { [k]: put(v) });
    };
    return el('label', { class: 'field', title }, el('span', { text: label }), i);
  };
  const dashOn = el('input', { type: 'number', min: 0.2, max: 50, step: 0.1, placeholder: 'off', disabled: locked, 'aria-label': 'Dash drawn (mm)' });
  const dashOff = el('input', { type: 'number', min: 0.2, max: 50, step: 0.1, placeholder: 'off', disabled: locked, 'aria-label': 'Dash gap (mm)' });
  if (s.dash) { dashOn.value = s.dash[0]; dashOff.value = s.dash[1]; }
  const setDash = () => {
    if (dashOn.value === '' || dashOff.value === '') { if (dashOn.value === '' && dashOff.value === '') upd(fl.key, { dash: null }); return; }
    const a = Number(dashOn.value), b = Number(dashOff.value);
    if (!(a >= 0.2 && a <= 50 && b >= 0.2 && b <= 50)) { toast('Dashes: 0.2 to 50 mm each.', true); return; }
    upd(fl.key, { dash: [a, b] });
  };
  dashOn.onchange = dashOff.onchange = setDash;
  return el('details', { class: 'layer-more', open },
    el('summary', { text: 'More: dashes, dwell, overdraw, pressure per pass' }),
    el('div', { class: 'field-row' },
      el('label', { class: 'field', title: 'Draw this long, then skip this long (pen taps)' }, el('span', { text: 'Dash drawn' }), dashOn),
      el('label', { class: 'field' }, el('span', { text: 'Dash gap (mm)' }), dashOff)),
    el('div', { class: 'field-row' },
      f('dwell_ms', 'Dwell (ms)', 0, 3000, 50, 'Wait with the pen down at each stroke start and end: ink flow for dip pens and brushes'),
      f('overdraw', 'Overdraw (mm)', 0, 30, 0.5, 'Closed shapes go on past their start by this much: closes the seam on felt tips'),
      f('pressure_step', 'Per pass', -0.4, 0.4, 0.05, 'With several passes: each pass this much lighter (positive) or heavier (negative)')));
}

// A legend: one line per enabled layer in plot order, as single-line text at the bottom-left of the canvas.
async function addLegend(rows) {
  const lines = rows.filter(({ s }) => s.enabled !== false).map(({ fl, s }, n) =>
    `${n + 1}. ${s.name || fl.name}: ${s.pen || 'pen not set'}${s.speed ? `, ${s.speed} mm/s` : ''}`);
  if (!lines.length) return;
  const xh = 3;
  try {
    const r = await post('/api/text_single', { text: lines.join('\n'), font: 'Hershey Sans', xheight: xh, align: 'left' });
    await refreshFiles();
    const c = G.canvasRect().canvas;
    await D.addFile(r.file.id, { at: [c[0] + 2, Math.max(c[1], c[3] - lines.length * xh * 3 - 2)] });
    toast('Legend added at the bottom-left of the canvas. Move it where it fits.');
  } catch (e) { toast(e.message, true); }
}

// Objects with any line in this layer's colour (their own colour, or the one they were moved to).
function selectLayer(fl) {
  const c = (fl.color || '').toLowerCase();
  const ids = state.doc.objects.filter((o) => {
    if (o.ink) return o.ink === c;
    const n = state.naturals[o.file];
    return !!(n && (n.layers || []).some((l) => (l.color || '').toLowerCase() === c));
  }).map((o) => o.id);
  if (!ids.length) { toast('No object draws in this layer.', true); return; }
  D.select(ids);
}

function renderFills() {
  const box = $('fillList');
  const pv = state.preview;
  const inks = (pv && pv.summary && pv.summary.fill_inks) || [];
  $('fillSection').hidden = !inks.length;
  if (!inks.length) { box.replaceChildren(); return; }
  const locked = jobActive();
  const st = state.doc.settings;
  const cur = (key) => (st.fills || []).find((f) => f.key === key) || { key, mode: 'default' };
  const upd = (key, patch) => D.edit((d) => {
    const all = (d.settings.fills || []).filter((f) => f.key !== key);
    const f = { ...cur(key), ...patch };
    if (f.mode !== 'default') all.push(f);
    d.settings.fills = all;
  });
  const deflt = st.fill ? `job default: hatch ${st.fill} mm` : 'job default: not filled';
  box.replaceChildren(...inks.map((ink) => {
    const f = cur(ink.key);
    const sw = el('span', { class: 'swatch' });
    sw.style.background = ink.color;
    const mode = el('select', { 'aria-label': `Fill for ${ink.color}`, disabled: locked, onchange: (e) => {
      const m = e.target.value;
      upd(ink.key, m === 'hatch' || m === 'contour' ? { mode: m, spacing: f.spacing || 1, angle: f.angle ?? 45 } : { mode: m });
    } }, el('option', { value: 'default', text: deflt }), el('option', { value: 'hatch', text: 'Hatch' }),
      el('option', { value: 'contour', text: 'Contour rings' }), el('option', { value: 'off', text: 'Outline only' }));
    mode.value = f.mode || 'default';
    const parts = [el('div', { class: 'layer-head' }, sw, el('b', { text: ink.color }), el('span', { class: 'spacer' }),
      el('small', { class: 'muted', text: `${ink.shapes} shape${ink.shapes === 1 ? '' : 's'}` })), mode];
    if (f.mode === 'hatch' || f.mode === 'contour') {
      const n = (k, label, min, max, step, dv) => {
        const i = el('input', { type: 'number', min, max, step, disabled: locked, 'aria-label': label });
        i.value = f[k] ?? dv;
        i.onchange = () => {
          const v = Number(i.value);
          if (!(v >= min && v <= max)) { toast(`${label}: ${min} to ${max}.`, true); i.value = f[k] ?? dv; return; }
          upd(ink.key, { [k]: v });
        };
        return el('label', { class: 'field' }, el('span', { text: label }), i);
      };
      const row = [n('spacing', 'Spacing (mm)', 0.2, 50, 0.1, 1)];
      if (f.mode === 'hatch') row.push(n('angle', 'Angle (deg)', 0, 180, 5, 45));
      parts.push(el('div', { class: 'field-row' }, ...row));
      const row2 = [n('inset', 'Inset (mm)', 0, 5, 0.1, 0), n('bridge', 'Bridge (mm)', 0, 5, 0.1, 0)];
      if (f.mode === 'hatch') row2.unshift(n('passes', 'Passes', 1, 5, 1, 1), n('angle_step', 'Turn per pass', 0, 180, 5, 0));
      parts.push(el('div', { class: 'field-row', title: 'Inset: fill lines stop short of the edge (blobs at their ends stay inside). Bridge: fill lines whose ends are this close are drawn as one, pen down across. Passes: the hatch again, turned each time.' }, ...row2));
      const chk = (k, label, title, dv) => {
        const c = el('input', { type: 'checkbox', disabled: locked });
        c.checked = f[k] === undefined ? dv : !!f[k];
        c.onchange = () => upd(ink.key, { [k]: c.checked });
        return el('label', { class: 'check inline', title }, c, ' ', label);
      };
      if (f.mode === 'hatch') {
        parts.push(el('div', { class: 'row' }, chk('cross', 'Cross-hatch', 'A second pass at right angles', false),
          chk('zigzag', 'Back and forth', 'Join the lines into zigzags (fewer pen lifts). Off: every line the same way.', true)));
      }
      parts.push(el('div', { class: 'row' }, chk('own_layer', 'Own layer', 'The fill plots as a layer of its own, so it can take its own pen (a marker for the fill, a fineliner for the outline)', false)));
    }
    return el('div', { class: 'layer fill-ink' }, ...parts);
  }));
}

// ------------------------------------------------------------------ projects
function renderProjects() {
  const list = $('projList');
  const locked = jobActive();
  if (state.doc && document.activeElement !== $('projName')) $('projName').value = state.doc.name || '';
  list.replaceChildren(...(state.projects || []).map((p) => el('li', {},
    el('div', {}, el('div', { class: 'name' }, el('button', { class: 'link', text: p.name, disabled: locked,
      title: 'Open: replaces what is on the bed (Undo brings it back)', onclick: () => D.openProject(p.slug).catch((e) => toast(e.message, true)) })),
    el('div', { class: 'meta' }, `${p.objects} object${p.objects === 1 ? '' : 's'}, saved ${(p.saved || '').replace('T', ' ')}`)),
    el('div', { class: 'row' }, el('button', { class: 'icon', text: 'Delete', 'aria-label': `Delete project ${p.name}`, onclick: async () => {
      if (!(await ask('Delete project', `Delete the project ${p.name}? The drawings stay in the Library.`, 'Delete'))) return;
      try { await D.deleteProject(p.slug); } catch (e) { toast(e.message, true); }
    } })))));
  $('bedClear').disabled = locked || !(state.doc && state.doc.objects.length);
}

export async function refreshFiles() {
  try {
    const r = await api('/api/files');
    set({ files: r.files });
  } catch (e) { toast(e.message, true); }
}
