// Ink renderer shared by Prepare and Plot. It draws plan data (the strokes of a plot_svg.py dry run, machine mm,
// one layer index per stroke) the way the pen will: the pen's colour, and in ink mode the pen's real line width.
// Both workspaces call drawInk with the same data, so what Prepare shows is what Plot plots.
import { prefs } from './store.js';

const DEFAULT_WIDTH_MM = 0.5;     // a pen profile without width_mm is drawn as a fine ballpoint
const cache = new WeakMap();      // plan data -> [[layer, Path2D]]

export function layerPaths(pv) {
  let hit = cache.get(pv);
  if (hit) return hit;
  const by = new Map();
  for (const s of pv.strokes || []) {
    if (!by.has(s.l)) by.set(s.l, new Path2D());
    const p = by.get(s.l);
    s.pts.forEach(([x, y], i) => (i ? p.lineTo(x, y) : p.moveTo(x, y)));
    if (s.pts.length === 1) p.lineTo(s.pts[0][0] + 0.01, s.pts[0][1]);
  }
  hit = [...by.entries()];
  cache.set(pv, hit);
  return hit;
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
  return { color: readable(hex, css), raw: hex || '#111111', width: (pen && Number(pen.width_mm)) || DEFAULT_WIDTH_MM,
    off: set.enabled === false, pen: set.pen || null, name: set.name || ly.title || ly.name || `Layer ${l + 1}` };
}

export const inkMode = () => prefs('ink') !== false;
export function setInkMode(on) { prefs('ink', !!on); }

// px: one screen pixel in mm. alpha: overall opacity. dim(l): extra fade for a layer (switched off, etc).
export function drawInk(ctx, pv, { px, layers, pens, css, alpha = 1, ink = inkMode(), lineScale = 1 }) {
  ctx.save();
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  for (const [l, path] of layerPaths(pv)) {
    const k = layerInk(pv, l, layers, pens, css);
    ctx.globalAlpha = alpha * (k.off ? 0.15 : ink ? 0.92 : 1);
    ctx.strokeStyle = k.color;
    ctx.lineWidth = (ink ? Math.max(k.width, 0.8 * px) : Math.max(0.3, 1.2 * px)) / lineScale;
    ctx.stroke(path);
  }
  ctx.restore();
}
