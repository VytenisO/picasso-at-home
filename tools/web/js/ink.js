// Ink renderer shared by Prepare and Plot. It draws plan data (the strokes of a plot_svg.py dry run, machine mm,
// one layer index per stroke) the way the pen will: the pen's colour, and in ink mode the pen's real line width.
// Both workspaces call drawInk with the same data, so what Prepare shows is what Plot plots.
import { prefs } from './store.js';

const DEFAULT_WIDTH_MM = 0.5;     // a pen profile without width_mm is drawn as a fine ballpoint
const cache = new WeakMap();      // plan data -> [[layer, Path2D]]

export function layerPaths(pv) {
  let hit = cache.get(pv);
  if (hit) return hit;
  const by = new Map();           // per layer and pen pressure: a lighter touch is drawn lighter
  for (const s of pv.strokes || []) {
    const key = `${s.l}|${s.p ?? ''}`;
    if (!by.has(key)) by.set(key, new Path2D());
    const p = by.get(key);
    s.pts.forEach(([x, y], i) => (i ? p.lineTo(x, y) : p.moveTo(x, y)));
    if (s.pts.length === 1) p.lineTo(s.pts[0][0] + 0.01, s.pts[0][1]);
  }
  hit = [...by.entries()].map(([k, path]) => { const [l, p] = k.split('|'); return [Number(l), path, p === '' ? null : Number(p)]; });
  cache.set(pv, hit);
  return hit;
}

// Huge drawings: the same paths with points closer than about one screen pixel dropped (each stroke keeps its ends),
// built once per zoom level. Below LOD_POINTS every point is drawn.
const LOD_POINTS = 150000;
const lodCache = new WeakMap();   // plan data -> Map(level -> paths)
const pointCount = new WeakMap();
function lodPaths(pv, px) {
  let n = pointCount.get(pv);
  if (n === undefined) { n = 0; for (const s of pv.strokes || []) n += s.pts.length; pointCount.set(pv, n); }
  const level = n < LOD_POINTS ? -1 : Math.max(0, Math.min(8, Math.floor(Math.log2(px / 0.05))));
  if (level < 0) return layerPaths(pv);
  let m = lodCache.get(pv);
  if (!m) { m = new Map(); lodCache.set(pv, m); }
  if (m.has(level)) return m.get(level);
  const tol = 0.05 * 2 ** level, t2 = tol * tol;
  const by = new Map();
  for (const s of pv.strokes || []) {
    const key = `${s.l}|${s.p ?? ''}`;
    if (!by.has(key)) by.set(key, new Path2D());
    const p = by.get(key), pts = s.pts;
    let [lx, ly] = pts[0];
    p.moveTo(lx, ly);
    for (let i = 1; i < pts.length; i++) {
      const [x, y] = pts[i];
      if (i < pts.length - 1 && (x - lx) ** 2 + (y - ly) ** 2 < t2) continue;
      p.lineTo(x, y); lx = x; ly = y;
    }
    if (pts.length === 1) p.lineTo(lx + 0.01, ly);
  }
  const out = [...by.entries()].map(([k, path]) => { const [l, pr] = k.split('|'); return [Number(l), path, pr === '' ? null : Number(pr)]; });
  m.set(level, out);
  return out;
}

// Dark inks are drawn in the theme's text colour so they stay visible on a dark-mode sheet.
function readable(hex, css) {
  if (!hex || !/^#[0-9a-f]{6}$/i.test(hex)) return css('--ink');
  const n = parseInt(hex.slice(1), 16);
  const lum = 0.2126 * (n >> 16) + 0.7152 * ((n >> 8) & 255) + 0.0722 * (n & 255);
  return lum < 60 ? css('--ink') : hex;
}

// Colour and width of one layer: the chosen pen wins over the drawing's own stroke colour.
export function layerInk(pv, l, layers, pens, css) {
  const ly = (pv.summary && pv.summary.layers && pv.summary.layers[l]) || (pv.layers && pv.layers[l]) || {};
  const set = (layers || []).find((x) => x && x.key === ly.key) || (layers || [])[l] || {};
  const pen = set.pen && pens ? pens[set.pen] : null;
  const hex = (pen && pen.colour_hex) || ly.color;
  return { key: ly.key, color: readable(hex, css), raw: hex || '#111111', width: (pen && Number(pen.width_mm)) || DEFAULT_WIDTH_MM,
    off: set.enabled === false, pen: set.pen || null, name: set.name || ly.title || ly.name || `Layer ${l + 1}` };
}

export const inkMode = () => prefs('ink') !== false;
// Layers hidden in the view (this browser only). They still plot.
export const hiddenLayers = () => new Set(prefs('hiddenLayers') || []);
export function toggleLayerHidden(key) {
  const h = hiddenLayers();
  if (h.has(key)) h.delete(key); else h.add(key);
  prefs('hiddenLayers', [...h]);
}
export function setInkMode(on) { prefs('ink', !!on); }

// px: one screen pixel in mm. alpha: overall opacity. dim(l): extra fade for a layer (switched off, etc).
export function drawInk(ctx, pv, { px, layers, pens, css, alpha = 1, ink = inkMode(), lineScale = 1, hide = null }) {
  ctx.save();
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  for (const [l, path, pr] of lodPaths(pv, px)) {
    const k = layerInk(pv, l, layers, pens, css);
    if (hide && hide.has(k.key)) continue;           // hidden in the view only; it still plots
    ctx.globalAlpha = alpha * (k.off ? 0.15 : ink ? 0.92 : 1) * (pr ? 1 - 0.7 * Math.min(1, pr / 0.9) : 1);
    ctx.strokeStyle = k.color;
    ctx.lineWidth = (ink ? Math.max(k.width, 0.8 * px) : Math.max(0.3, 1.2 * px)) / lineScale;
    ctx.stroke(path);
  }
  ctx.restore();
}
