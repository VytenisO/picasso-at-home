// The bed document: the objects on the bed, the job settings, the selection, undo/redo, the clipboard and saved
// projects. The server keeps the working document (saved with every preview), so a reload or another device sees
// the same bed. Every change goes through edit(), which records undo and schedules a preview.
import { api, post } from './api.js';
import { state, set, on, jobActive, prefs } from './store.js';
import { toast } from './ui.js';
import * as G from './geom.js';

export const CLIENT = Math.random().toString(36).slice(2, 10);
const undoStack = [], redoStack = [];
const MAX_UNDO = 200;
let timer = null, seq = 0, lastCoalesce = null, pasteN = 0;

const newId = () => Math.random().toString(16).slice(2, 10);
const snap = () => JSON.stringify({ name: state.doc.name, objects: state.doc.objects, settings: state.doc.settings });

export const selected = () => (state.doc ? state.doc.objects.filter((o) => state.sel.includes(o.id)) : []);
export const fileName = (o) => (state.files.find((f) => f.id === o.file) || {}).name || 'missing drawing';
export const canUndo = () => undoStack.length > 0;
export const canRedo = () => redoStack.length > 0;

export function initDoc() {
  on('files', refreshNaturals);
}

// ------------------------------------------------------------------ editing
// fn(draft) changes a copy of the document; returning false cancels. coalesce: edits with the same key within
// 1.5 s are one undo step (typing in a field, holding an arrow key).
export function edit(fn, { coalesce = null, sel = null } = {}) {
  if (!state.doc) return false;
  if (jobActive()) { toast('A job is running: the bed is locked until it ends.', true); return false; }
  const before = snap();
  const d = structuredClone(state.doc);
  if (fn(d) === false) return false;
  if (JSON.stringify({ name: d.name, objects: d.objects, settings: d.settings }) === before) return false;
  // A resume point belongs to one plot order: any change to the bed or its planning drops it.
  if (state.startAt) set({ startAt: 0 });
  const now = Date.now();
  if (!(coalesce && lastCoalesce && lastCoalesce.key === coalesce && now - lastCoalesce.t < 1500)) {
    undoStack.push({ s: before, t: now, what: describe(before, d) });
    if (undoStack.length > MAX_UNDO) undoStack.shift();
  }
  lastCoalesce = coalesce ? { key: coalesce, t: now } : null;
  redoStack.length = 0;
  apply(d, sel);
  return true;
}

function apply(d, sel = null) {
  const ids = new Set(d.objects.map((o) => o.id));
  set({ doc: d, sel: (sel || state.sel).filter((id) => ids.has(id)), previewStale: true, history: undoStack.length });
  schedule();
}

function schedule(now = false) {
  seq++;                                   // an older preview still in flight must not overwrite this change
  clearTimeout(timer);
  pending = true;
  timer = setTimeout(runPreview, now ? 0 : 600);
}

// Leaving the page within the preview delay would drop the last change: send it on the way out.
let pending = false;
window.addEventListener('pagehide', () => {
  const d = state.doc;
  if (!pending || !d) return;
  try {
    fetch('/api/doc', { method: 'POST', keepalive: true, credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: d.name, objects: d.objects, settings: { ...d.settings, start_at: state.startAt || 0 }, client: CLIENT }) });
  } catch (e) { /* the page is going anyway */ }
});

function restore(s) {
  lastCoalesce = null;
  apply({ ...state.doc, ...JSON.parse(s) });
}

export function undo() {
  if (!undoStack.length || jobActive()) return;
  redoStack.push(snap());
  restore(undoStack.pop().s);
}

export function redo() {
  if (!redoStack.length || jobActive()) return;
  undoStack.push({ s: snap(), t: Date.now(), what: 'Redone step' });
  restore(redoStack.pop());
}

export function setSettings(patch, opts = {}) {
  return edit((d) => { Object.assign(d.settings, patch); }, opts);
}

export function select(ids, { add = false, toggle = false } = {}) {
  let s = add ? [...state.sel] : [];
  // a grouped object brings its whole group
  const objs = state.doc ? state.doc.objects : [];
  const all = [];
  for (const id of [].concat(ids)) {
    const o = objs.find((x) => x.id === id);
    const mates = o && o.grp ? objs.filter((x) => x.grp === o.grp).map((x) => x.id) : [id];
    for (const m of mates) if (!all.includes(m)) all.push(m);
  }
  for (const id of all) {
    if (toggle && s.includes(id)) s = s.filter((x) => x !== id);
    else if (!s.includes(id)) s.push(id);
  }
  set({ sel: s });
}

export function selectAll() { if (state.doc) select(state.doc.objects.filter(onPage).map((o) => o.id)); }

// ------------------------------------------------------------------ pages: sheets of one project
// Each object has a page (0 when unset); the bed shows and edits one page at a time, the plot holds for the
// next sheet between pages. settings.pages keeps empty pages too.
export const curPage = () => Math.min(state.page || 0, pageCount() - 1);
export const onPage = (o) => (o.page || 0) === curPage();
export function pageCount() {
  if (!state.doc) return 1;
  return Math.max(state.doc.settings.pages || 1, ...state.doc.objects.map((o) => (o.page || 0) + 1));
}
export function showPage(n) { set({ page: Math.max(0, Math.min(n, pageCount() - 1)), sel: [] }); }
export function addPage() {
  const n = pageCount();
  if (n >= 49) { toast('At most 49 pages.', true); return; }
  edit((d) => { d.settings.pages = n + 1; });
  showPage(n);
}
export function removePage() {
  const p = curPage();
  if (pageCount() <= 1) return;
  if (state.doc.objects.some(onPage)) { toast('Move or remove the drawings on this page first.', true); return; }
  edit((d) => {
    for (const o of d.objects) if ((o.page || 0) > p) { o.page -= 1; if (!o.page) delete o.page; }
    d.settings.pages = Math.max(1, (d.settings.pages || 1) - 1) > 1 ? Math.max(1, (d.settings.pages || 1) - 1) : null;
  });
  showPage(Math.max(0, p - 1));
}
// Tile one object that is larger than the canvas: one page per tile, each a copy moved so its part sits on the
// canvas and cut to it. Neighbouring tiles share an overlap strip for lining the sheets up.
export function tileSel(overlap) {
  const o = selected()[0];
  if (!o || state.sel.length !== 1) { toast('Select one drawing to tile.', true); return; }
  if (o.rot % 360) { toast('Tiling works on an unturned drawing: turn it back to 0 first.', true); return; }
  const c = G.canvasRect().canvas, cw = c[2] - c[0], ch = c[3] - c[1];
  if (overlap * 2 >= Math.min(cw, ch)) { toast('The overlap is too large for the canvas.', true); return; }
  const sx = cw - overlap, sy = ch - overlap;
  const nx = Math.max(1, Math.ceil((o.w - overlap) / sx)), ny = Math.max(1, Math.ceil((o.h - overlap) / sy));
  if (nx * ny === 1) { toast('It fits on one sheet already.'); return; }
  // on the current page when the drawing was alone on the last one, else on new pages after the others
  const alone = curPage() === pageCount() - 1 && !state.doc.objects.some((x) => x.id !== o.id && onPage(x));
  const first = alone ? curPage() : pageCount();
  if (first + nx * ny > 49 || state.doc.objects.length - 1 + nx * ny > 64) { toast(`${nx * ny} tiles is too many: make it smaller.`, true); return; }
  edit((d) => {
    d.objects = d.objects.filter((x) => x.id !== o.id);
    let k = 0;
    for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
      const rx = i * sx, ry = j * sy;                       // the tile's corner inside the drawing
      const rw = Math.min(cw, o.w - rx), rh = Math.min(ch, o.h - ry);
      const t = { ...o, id: newId(), x: G.r2(c[0] - rx), y: G.r2(c[1] - ry), page: first + k || undefined, locked: true,
        clip: [c[0], c[1], G.r2(c[0] + rw), G.r2(c[1] + rh)] };
      delete t.g0;
      d.objects.push(t);
      k++;
    }
    d.settings.pages = first + nx * ny;
  });
  showPage(first);
  toast(`${nx} x ${ny} tiles on pages ${first + 1} to ${first + nx * ny}, ${overlap} mm overlap. Each is locked so it stays lined up.`);
}

export function moveSelToPage(n) {
  editSel((objs, d) => {
    for (const o of objs) { if (n) o.page = n; else delete o.page; }
    if (n + 1 > (d.settings.pages || 1)) d.settings.pages = n + 1;
  }, { allowLocked: true });
  showPage(n);
}

// ------------------------------------------------------------------ server
export async function loadDoc() {
  const r = await api('/api/doc');
  set({ projects: r.projects });
  await adopt(r.doc, { record: false });
}

// Take a document from the server (another device, a project). It can be undone like any edit.
async function adopt(d, { record = true } = {}) {
  await ensureNaturals(d.objects);
  for (const o of d.objects) refit(o);
  if (record && state.doc) { undoStack.push({ s: snap(), t: Date.now(), what: 'Bed replaced (project or another device)' }); redoStack.length = 0; }
  set({ doc: { name: d.name || '', objects: d.objects, settings: d.settings }, sel: [], preview: null, previewStale: true,
    history: undoStack.length });
  schedule(true);
}

export function onDocEvent(ev) {
  if (ev.client === CLIENT) return;
  // Someone else changed the bed. Mid-drag or mid-typing the change waits for the next event.
  api('/api/doc').then((r) => {
    if (r.doc.client === CLIENT) return;
    adopt(r.doc).then(() => toast('The bed was changed on another device.'));
  }).catch(() => {});
}

// Plot from stroke n of the order (0 = the start). Not an undo step and not saved with the bed.
export function setStartAt(n) {
  set({ startAt: Math.max(0, n | 0), scrub: null, previewStale: true });
  schedule(true);
}

export async function runPreview() {
  clearTimeout(timer);
  pending = false;
  const d = state.doc;
  if (!d) return null;
  const my = ++seq;
  const body = { name: d.name, objects: d.objects, settings: { ...d.settings, start_at: state.startAt || 0 } };
  if (!d.objects.length || jobActive()) {
    // nothing to check (or the bed is locked): keep the server's copy up to date anyway
    post('/api/doc', { ...body, client: CLIENT }).catch(() => {});
    if (!d.objects.length) set({ preview: null, previewStale: false });
    return null;
  }
  set({ previewing: true });
  try {
    const r = await post('/api/preview', { doc: body, client: CLIENT });
    if (my !== seq) return null;           // a newer change superseded this preview
    // The server's layer list is the truth: settings for every ink the bed has now. Not an undo step.
    set({ preview: r, previewStale: false, doc: { ...state.doc, settings: { ...state.doc.settings, layers: r.settings.layers } } });
    return r;
  } catch (e) {
    if (my === seq) {
      set({ preview: { ok: false, problems: [e.message], error: true }, previewStale: false });
      toast(e.message, true);
    }
    return null;
  } finally {
    if (my === seq) set({ previewing: false });
  }
}

// ------------------------------------------------------------------ drawings (natural geometry)
async function natural(id, force = false) {
  if (!force && state.naturals[id]) return state.naturals[id];
  const n = await api(`/api/files/natural?id=${encodeURIComponent(id)}`);
  state.naturals[id] = n;
  set({ naturals: state.naturals });
  return n;
}

async function ensureNaturals(objs) {
  const ids = [...new Set(objs.map((o) => o.file))].filter((id) => !state.naturals[id]);
  await Promise.all(ids.map((id) => natural(id).catch(() => null)));
}

// A drawing changed in the library (Prepare's Update keeps its id): keep each object's width and take the new
// drawing's proportions, so it is never stretched by the change.
function refit(o) {
  const n = state.naturals[o.file];
  if (!n) return false;
  const g = n.geom;
  if (o.g === g.svg_hash) return false;
  if (o.g && g.raw_w > 1e-6) o.h = G.r2(Math.max(0.1, o.w * g.raw_h / g.raw_w));
  o.g = g.svg_hash;
  return true;
}

async function refreshNaturals() {
  if (!state.doc) return;
  const files = [...new Set(state.doc.objects.map((o) => o.file))];
  const changed = [];
  await Promise.all(files.map(async (id) => {
    const entry = state.files.find((f) => f.id === id);
    const have = state.naturals[id];
    if (!entry || !have || (entry.geom && entry.geom.svg_hash === have.geom.svg_hash)) return;
    const n = await natural(id, true).catch(() => null);
    if (n && n.geom.svg_hash !== have.geom.svg_hash) changed.push(id);
  }));
  if (!changed.length) return;
  const d = structuredClone(state.doc);
  for (const o of d.objects) if (changed.includes(o.file)) refit(o);
  set({ doc: d, previewStale: true });
  schedule();
}

// ------------------------------------------------------------------ adding and arranging
// Put a library drawing on the bed at natural size (shrunk to fit the canvas), next to what is there.
export async function addFile(id, { select: pick = true, at = null, replace = [], corner = false } = {}) {
  const entry = state.files.find((f) => f.id === id);
  if (!entry) { toast('That file is no longer in the library.', true); return null; }
  if (jobActive()) { toast('A job is running: add it to the bed when it ends.', true); return null; }
  let n;
  try { n = await natural(id); } catch (e) { toast(e.message, true); return null; }
  const g = n.geom, c = G.canvasRect().canvas;
  let w = Math.max(g.raw_w, 0.5), h = Math.max(g.raw_h, 0.5);
  const fit = Math.min(1, (c[2] - c[0]) / w, (c[3] - c[1]) / h);
  w *= fit; h *= fit;
  const o = { id: newId(), file: id, x: c[0], y: c[1], w: G.r2(w), h: G.r2(h), rot: 0, fx: false, fy: false, g: g.svg_hash };
  if (curPage()) o.page = curPage();
  const s = entry.settings || {};
  const others = state.doc.objects.filter(onPage);
  if (!others.length && s.x !== null && s.x !== undefined && s.w) {
    // the drawing's place from before the bed held several objects
    o.x = s.x; o.y = s.y; o.w = G.r2(s.w); o.h = G.r2(g.raw_w > 1e-6 ? s.w * g.raw_h / g.raw_w : h);
  } else if (others.length) {
    const b = G.bounds(others), gap = 5;
    if (b.x1 + gap + o.w <= c[2]) { o.x = G.r2(b.x1 + gap); o.y = G.r2(Math.max(c[1], b.y0)); }
    else if (b.y1 + gap + o.h <= c[3]) { o.x = c[0]; o.y = G.r2(b.y1 + gap); }
  }
  if (at) { o.x = G.r2(at[0]); o.y = G.r2(at[1]); }
  if (corner && others.length) {
    // the first canvas corner (bottom right, bottom left, top right, top left) where it overlaps nothing
    const hit = (x, y) => others.some((q) => { const b = G.bounds([q]); return x < b.x1 && x + o.w > b.x0 && y < b.y1 && y + o.h > b.y0; });
    const spots = [[c[2] - o.w, c[3] - o.h], [c[0], c[3] - o.h], [c[2] - o.w, c[1]], [c[0], c[1]]];
    const free = spots.find(([x, y]) => !hit(x, y));
    if (free) { o.x = G.r2(free[0]); o.y = G.r2(free[1]); }
    else toast('No free corner: placed after the other drawings. Move it before plotting.', true);
  }
  edit((d) => {
    if (replace.length) {          // a shape result takes the place (and plot order) of what it was made from
      const at_ = d.objects.findIndex((x) => replace.includes(x.id));
      d.objects = d.objects.filter((x) => !replace.includes(x.id));
      d.objects.splice(Math.max(0, at_), 0, o);
    } else d.objects.push(o);
    if (entry.pen) {
      // A test sheet belongs to one pen: preset it on the sheet's layers that have no pen yet.
      for (const ly of n.layers) {
        const cur = d.settings.layers.find((x) => x.key === ly.key);
        if (!cur) d.settings.layers.push({ key: ly.key, enabled: true, pen: entry.pen, speed: null, pressure: null });
        else if (!cur.pen) cur.pen = entry.pen;
      }
    }
  }, { sel: pick ? [o.id] : null });
  prefs('lastFile', id);
  return o;
}

export function removeSel() {
  if (lockedSel()) return;
  const ids = state.sel;
  if (!ids.length) return;
  edit((d) => { d.objects = d.objects.filter((o) => !ids.includes(o.id)); }, { sel: [] });
}

export function clearBed() {
  edit((d) => { d.objects = []; d.name = ''; }, { sel: [] });
}

// Break apart: new objects for library pieces at their bed positions, in place of the originals (one undo step).
export async function replaceWithPieces(ids, pieces) {
  const objs = pieces.map((p) => ({ id: newId(), file: p.file.id, x: G.r2(p.x), y: G.r2(p.y), w: G.r2(Math.max(0.1, p.w)),
    h: G.r2(Math.max(0.1, p.h)), rot: 0, fx: false, fy: false }));
  await ensureNaturals(objs);
  for (const o of objs) { const n = state.naturals[o.file]; if (n) o.g = n.geom.svg_hash; }
  edit((d) => {
    const at = d.objects.findIndex((x) => ids.includes(x.id));
    d.objects = d.objects.filter((x) => !ids.includes(x.id));
    d.objects.splice(Math.max(0, at), 0, ...objs);
  }, { sel: objs.map((o) => o.id) });
}

// ------------------------------------------------------------------ selection helpers
// Tab / Shift+Tab: the next or previous object in the list (a group counts as one step).
export function selectNext(dir = 1) {
  const objs = state.doc ? state.doc.objects : [];
  if (!objs.length) return;
  const cur = objs.findIndex((o) => o.id === state.sel[dir > 0 ? state.sel.length - 1 : 0]);
  let i = cur < 0 ? (dir > 0 ? 0 : objs.length - 1) : (cur + dir + objs.length) % objs.length;
  const g = objs[cur] && objs[cur].grp;
  for (let n = 0; g && n < objs.length && objs[i].grp === g; n++) i = (i + dir + objs.length) % objs.length;
  select(objs[i].id);
}

export function invertSel() {
  if (!state.doc) return;
  select(state.doc.objects.filter((o) => !state.sel.includes(o.id)).map((o) => o.id));
}

// Objects whose drawing has an open stroke, or whose box is smaller than n mm both ways.
export function selectWhere(kind, n = 5) {
  if (!state.doc) return 0;
  const ids = state.doc.objects.filter((o) => {
    if (kind === 'small') return o.w < n && o.h < n;
    const nat = state.naturals[o.file];
    return !!(nat && (nat.strokes || []).some((s) => {
      const p = s.pts, a = p[0], b = p[p.length - 1];
      return p.length < 3 || Math.hypot(a[0] - b[0], a[1] - b[1]) > 0.2;
    }));
  }).map((o) => o.id);
  select(ids);
  return ids.length;
}

export async function pasteInPlace() {
  const clip = prefs('clipboard');
  if (!Array.isArray(clip) || !clip.length) return;
  const known = clip.filter((o) => state.files.some((f) => f.id === o.file));
  if (!known.length) { toast('The copied drawings are no longer in the library.', true); return; }
  await ensureNaturals(known);
  const copies = regroup(known.map((o) => ({ ...o, id: newId(), locked: undefined, page: curPage() || undefined })));
  edit((d) => { d.objects.push(...copies); }, { sel: copies.map((o) => o.id) });
}

// Same width or height as the first object selected, each keeping its own proportions only if asked.
export function sameSize(dim, keep = false) {
  const ref = selected().find((o) => o.id === state.sel[0]);
  if (!ref) return;
  editSel((objs) => {
    if (objs.length < 2) return false;
    for (const o of objs) {
      if (o === ref || o.id === ref.id) continue;
      const [cx, cy] = G.center(o);
      if (dim === 'w') { const k = ref.w / o.w; o.w = ref.w; if (keep) o.h *= k; }
      else { const k = ref.h / o.h; o.h = ref.h; if (keep) o.w *= k; }
      G.setCenter(o, cx, cy);
    }
  });
}

// Equal steps between the objects' centres (three or more).
export function distributeCentres(axis) {
  editSel((objs) => {
    if (objs.length < 3) return false;
    const cs = objs.map((o) => ({ o, c: G.center(o)[axis === 'x' ? 0 : 1] })).sort((p, q) => p.c - q.c);
    const step = (cs[cs.length - 1].c - cs[0].c) / (cs.length - 1);
    cs.forEach((p, i) => { const d = cs[0].c + i * step - p.c; if (axis === 'x') p.o.x += d; else p.o.y += d; });
  });
}

// Move together: side by side with no gap, in their current order, from the first one's edge.
export function abut(axis) {
  editSel((objs) => {
    if (objs.length < 2) return false;
    const bs = objs.map((o) => ({ o, b: G.bounds([o]) }));
    const k0 = axis === 'x' ? 'x0' : 'y0', k1 = axis === 'x' ? 'x1' : 'y1';
    bs.sort((p, q) => p.b[k0] - q.b[k0]);
    let at = bs[0].b[k1];
    for (const p of bs.slice(1)) {
      const d = at - p.b[k0];
      if (axis === 'x') p.o.x += d; else p.o.y += d;
      at = p.b[k1] + d;
    }
  });
}

// Copies around a centre: n in all, the centre radius mm below the selection's middle (so the original sits at
// the top); turn: each copy turned with its angle.
export function circularSel(n, radius, turn) {
  const src = selected();
  if (!src.length || n < 2) return;
  const b = G.bounds(src);
  const cx = b.x0 + b.w / 2, cy = b.y0 + b.h / 2 + radius;
  const copies = [];
  for (let k = 1; k < n; k++) {
    const set = src.map((o) => ({ ...structuredClone(o), id: newId(), locked: undefined }));
    const rots = set.map((o) => o.rot);
    G.rotateAbout(set, cx, cy, 360 * k / n);
    if (!turn) set.forEach((o, i) => { o.rot = rots[i]; });      // moved round the circle, still upright
    for (const o of set) { o.x = G.r2(o.x); o.y = G.r2(o.y); }
    copies.push(...regroup(set));
  }
  if (copies.length + state.doc.objects.length > 64) { toast('At most 64 objects on the bed.', true); return; }
  edit((d) => { d.objects.push(...copies); }, { sel: [...src.map((o) => o.id), ...copies.map((o) => o.id)] });
}

// Copies of objs moved by each placement's (dx, dy) and turned by its angle about its point (copy along a path).
export function placeCopies(objs, placed) {
  const copies = [];
  for (const p of placed) {
    const set = objs.map((o) => ({ ...structuredClone(o), id: newId(), locked: undefined, x: o.x + p.dx, y: o.y + p.dy }));
    if (p.ang) G.rotateAbout(set, p.cx, p.cy, p.ang);
    for (const o of set) { o.x = G.r2(o.x); o.y = G.r2(o.y); }
    copies.push(...regroup(set));
  }
  if (copies.length + state.doc.objects.length > 64) { toast('At most 64 objects on the bed.', true); return; }
  edit((d) => { d.objects.push(...copies); }, { sel: copies.map((o) => o.id) });
}

// Undo history: what each step was, newest first. undoTo(n) steps back n times.
export const history = () => undoStack.map((e, i) => ({ i, what: e.what, t: e.t })).reverse();
export function undoTo(n) { for (let k = 0; k < n; k++) undo(); }

function describe(before, d) {
  const b = JSON.parse(before);
  const nb = b.objects.length, na = d.objects.length;
  if (na > nb) return `Added ${na - nb} object${na - nb > 1 ? 's' : ''}`;
  if (na < nb) return `Removed ${nb - na} object${nb - na > 1 ? 's' : ''}`;
  if (JSON.stringify(b.objects) !== JSON.stringify(d.objects)) {
    const moved = d.objects.filter((o, i) => JSON.stringify(o) !== JSON.stringify(b.objects[i])).length;
    return `Changed ${moved} object${moved > 1 ? 's' : ''}`;
  }
  if (b.name !== d.name) return 'Renamed the bed';
  return 'Job or layer settings';
}

// Copies form their own groups: a group id maps to a new one, so a copy never joins the original's group.
function regroup(objs) {
  const map = {};
  for (const o of objs) if (o.grp) o.grp = map[o.grp] || (map[o.grp] = newId());
  return objs;
}

export function duplicateSel(offset = 5) {
  const src = selected();
  if (!src.length) return;
  const copies = regroup(src.map((o) => ({ ...structuredClone(o), id: newId(), x: G.r2(o.x + offset), y: G.r2(o.y + offset), locked: undefined })));
  edit((d) => { d.objects.push(...copies); }, { sel: copies.map((o) => o.id) });
}

export function copySel() {
  const src = selected();
  if (!src.length) return false;
  prefs('clipboard', src);
  pasteN = 0;
  toast(`Copied ${src.length} object${src.length > 1 ? 's' : ''}.`);
  return true;
}

export function cutSel() { if (copySel()) removeSel(); }

export async function paste() {
  const clip = prefs('clipboard');
  if (!Array.isArray(clip) || !clip.length) return;
  const known = clip.filter((o) => state.files.some((f) => f.id === o.file));
  if (!known.length) { toast('The copied drawings are no longer in the library.', true); return; }
  await ensureNaturals(known);
  pasteN++;
  const copies = regroup(known.map((o) => ({ ...o, id: newId(), x: G.r2(o.x + 5 * pasteN), y: G.r2(o.y + 5 * pasteN), locked: undefined, page: curPage() || undefined })));
  edit((d) => { d.objects.push(...copies); }, { sel: copies.map((o) => o.id) });
}

// Move the selection by (dx, dy) mm.
export function nudge(dx, dy) {
  const ids = state.sel;
  if (!ids.length || lockedSel()) return;
  edit((d) => { for (const o of d.objects) if (ids.includes(o.id)) { o.x = G.r2(o.x + dx); o.y = G.r2(o.y + dy); } },
    { coalesce: 'nudge' });
}

// True (and says so) when the selection has a locked object: it may not move, resize, turn or go.
export function lockedSel() {
  if (!selected().some((o) => o.locked)) return false;
  toast('The selection has a locked object. Unlock it first (Arrange, Unlock).', true);
  return true;
}

export function group() {
  const ids = state.sel;
  if (ids.length < 2) { toast('Select two or more objects to group.', true); return; }
  const g = newId();
  editSel((objs) => { for (const o of objs) o.grp = g; }, { allowLocked: true });
}

export function ungroup() {
  editSel((objs) => { if (!objs.some((o) => o.grp)) return false; for (const o of objs) delete o.grp; }, { allowLocked: true });
}

export function toggleLock() {
  const sel = selected();
  if (!sel.length) return;
  const lock = !sel.every((o) => o.locked);
  editSel((objs) => { for (const o of objs) { if (lock) o.locked = true; else delete o.locked; } }, { allowLocked: true });
}

// Run fn(objs, d) on the selected objects of a draft. Refused when one is locked, unless allowLocked (colour,
// grouping, locking itself).
export function editSel(fn, opts = {}) {
  const ids = state.sel;
  if (!ids.length) return false;
  const { allowLocked = false, ...eopts } = opts;
  if (!allowLocked && lockedSel()) return false;
  opts = eopts;
  return edit((d) => {
    const objs = d.objects.filter((o) => ids.includes(o.id));
    if (fn(objs, d) === false) return false;
    for (const o of objs) { o.x = G.r2(o.x); o.y = G.r2(o.y); o.w = G.r2(o.w); o.h = G.r2(o.h); }
  }, opts);
}

// Align: with one object, to the canvas; with several, to the selection's own bounds.
export function align(kind) {
  editSel((objs) => {
    const ref = objs.length > 1 ? G.bounds(objs) : (() => { const c = G.canvasRect().canvas; return { x0: c[0], y0: c[1], x1: c[2], y1: c[3] }; })();
    for (const o of objs) {
      const b = G.bounds([o]);
      let dx = 0, dy = 0;
      if (kind === 'left') dx = ref.x0 - b.x0;
      if (kind === 'right') dx = ref.x1 - b.x1;
      if (kind === 'hcenter') dx = (ref.x0 + ref.x1) / 2 - (b.x0 + b.x1) / 2;
      if (kind === 'top') dy = ref.y0 - b.y0;
      if (kind === 'bottom') dy = ref.y1 - b.y1;
      if (kind === 'vcenter') dy = (ref.y0 + ref.y1) / 2 - (b.y0 + b.y1) / 2;
      o.x += dx; o.y += dy;
    }
  });
}

// Equal gaps between three or more objects, left to right or top to bottom.
export function distribute(axis) {
  editSel((objs) => {
    if (objs.length < 3) return false;
    const bs = objs.map((o) => ({ o, b: G.bounds([o]) }));
    const k0 = axis === 'x' ? 'x0' : 'y0', k1 = axis === 'x' ? 'x1' : 'y1';
    bs.sort((p, q) => p.b[k0] - q.b[k0]);
    const span = bs[bs.length - 1].b[k1] - bs[0].b[k0];
    const total = bs.reduce((s, p) => s + (p.b[k1] - p.b[k0]), 0);
    const gap = (span - total) / (bs.length - 1);
    let at = bs[0].b[k0];
    for (const p of bs) {
      const d = at - p.b[k0];
      if (axis === 'x') p.o.x += d; else p.o.y += d;
      at += (p.b[k1] - p.b[k0]) + gap;
    }
  });
}

// Put the selection's anchor point on the same anchor point of the canvas (top-left on top-left, centre on centre).
export function toCanvasAnchor(anchor) {
  editSel((objs) => {
    const b = G.bounds(objs), c = G.canvasRect().canvas;
    const cb = { x0: c[0], y0: c[1], w: c[2] - c[0], h: c[3] - c[1] };
    const [tx, ty] = G.anchorPoint(cb, anchor), [sx, sy] = G.anchorPoint(b, anchor);
    for (const o of objs) { o.x += tx - sx; o.y += ty - sy; }
  });
}

// Scale the selection to fill the canvas (keeping proportions) and centre it.
export function fitCanvas() {
  editSel((objs) => {
    const b = G.bounds(objs), c = G.canvasRect().canvas;
    const k = Math.min((c[2] - c[0]) / b.w, (c[3] - c[1]) / b.h);
    G.scaleAbout(objs, b.x0, b.y0, k, k);
    const nb = G.bounds(objs);
    for (const o of objs) { o.x += (c[0] + c[2]) / 2 - (nb.x0 + nb.x1) / 2; o.y += (c[1] + c[3]) / 2 - (nb.y0 + nb.y1) / 2; }
  });
}

// Copies of the selection in a grid: cols x rows, gx / gy mm between the copies' bounds.
// stagger: odd rows shifted across by this many mm; alt: every other copy (chequerboard) mirrored left to right.
export function arraySel(cols, rows, gx, gy, stagger = 0, alt = false) {
  const src = selected();
  if (!src.length || cols * rows < 2) return;
  const b = G.bounds(src);
  const ids = src.map((o) => o.id);
  const copies = [];
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      if (!r && !c) continue;
      const set = src.map((o) => ({ ...structuredClone(o), id: newId(), locked: undefined,
        x: G.r2(o.x + c * (b.w + gx) + (r % 2 ? stagger : 0)), y: G.r2(o.y + r * (b.h + gy)) }));
      if (alt && (r + c) % 2) {
        const sb = G.bounds(set);
        G.mirror(set, 'x', sb.x0 + sb.w / 2, sb.y0 + sb.h / 2);
      }
      copies.push(...regroup(set));
    }
  }
  if (copies.length + state.doc.objects.length > 64) { toast('At most 64 objects on the bed.', true); return; }
  edit((d) => { d.objects.push(...copies); }, { sel: [...ids, ...copies.map((o) => o.id)] });
}

export function rotateSel(deg) {
  editSel((objs) => { const b = G.bounds(objs); G.rotateAbout(objs, b.x0 + b.w / 2, b.y0 + b.h / 2, deg); });
}

export function mirrorSel(axis) {
  editSel((objs) => { const b = G.bounds(objs); G.mirror(objs, axis, b.x0 + b.w / 2, b.y0 + b.h / 2); });
}

// Plot order: the objects later in the list are drawn later within each pen.
export function reorder(dir) {
  const ids = state.sel;
  if (!ids.length) return;
  edit((d) => {
    const pick = d.objects.filter((o) => ids.includes(o.id)), rest = d.objects.filter((o) => !ids.includes(o.id));
    if (dir === 'first') d.objects = [...pick, ...rest];
    else if (dir === 'last') d.objects = [...rest, ...pick];
    else {
      const arr = d.objects.slice();
      const order = dir === 'up' ? [...arr.keys()].reverse() : [...arr.keys()];
      for (const i of order) {
        const j = dir === 'up' ? i + 1 : i - 1;
        if (ids.includes(arr[i].id) && j >= 0 && j < arr.length && !ids.includes(arr[j].id)) [arr[i], arr[j]] = [arr[j], arr[i]];
      }
      d.objects = arr;
    }
  });
}

// ------------------------------------------------------------------ projects
export async function saveProject(name, template = false) {
  await runPreview();                       // the server's copy must be this bed
  const r = await post('/api/projects/save', { name, template });
  set({ projects: r.projects, doc: { ...state.doc, name: r.doc.name } });
  toast(`Saved the bed as "${r.doc.name}".`);
}

export async function openProject(slug) {
  if (jobActive()) { toast('A job is running: open the project when it ends.', true); return; }
  const r = await post('/api/projects/open', { slug });
  await adopt(r.doc);
  set({ projects: (await api('/api/doc')).projects });
  toast(`Opened "${r.doc.name || 'a copy of the template'}"` + (r.missing ? `. ${r.missing} drawing${r.missing > 1 ? 's are' : ' is'} no longer in the library and was left out.` : '.'),
    !!r.missing);
  const notes = r.doc.settings && r.doc.settings.notes;
  if (notes) {
    const { ask } = await import('./ui.js');
    ask('Project notes', notes, 'OK');
  }
}

export async function deleteProject(slug) {
  const r = await post('/api/projects/delete', { slug });
  set({ projects: r.projects });
}
