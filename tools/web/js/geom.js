// Geometry of bed objects, the same maths as tools/plotweb/document.py. An object is {x, y, w, h, rot, fx, fy}: its
// box before rotation in machine mm, turned `rot` degrees clockwise about the box centre, mirrored inside the box.
// The drawing's own geometry box (natural size, document mm) is stretched to fill it.
import { state } from './store.js';

const RAD = Math.PI / 180;

// Matrix [a, b, c, d, e, f] from the drawing's document mm to machine mm.
export function matrix(o, g) {
  const bw = g.raw_w, bh = g.raw_h;
  let sx = bw > 1e-6 ? o.w / bw : 1;
  let sy = bh > 1e-6 ? o.h / bh : sx;
  if (bw <= 1e-6) sx = sy;
  if (o.fx) sx = -sx;
  if (o.fy) sy = -sy;
  const co = Math.cos(o.rot * RAD), si = Math.sin(o.rot * RAD);
  const cx = o.x + o.w / 2, cy = o.y + o.h / 2;
  const mx = g.raw_x + bw / 2, my = g.raw_y + bh / 2;
  const a = co * sx, b = si * sx, c = -si * sy, d = co * sy;
  return [a, b, c, d, cx - a * mx - c * my, cy - b * mx - d * my];
}

export function center(o) { return [o.x + o.w / 2, o.y + o.h / 2]; }

// The rotated box's corners: top-left, top-right, bottom-right, bottom-left (before rotation).
export function corners(o) {
  const co = Math.cos(o.rot * RAD), si = Math.sin(o.rot * RAD);
  const [cx, cy] = center(o);
  return [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([kx, ky]) => {
    const dx = kx * o.w / 2, dy = ky * o.h / 2;
    return [cx + co * dx - si * dy, cy + si * dx + co * dy];
  });
}

// Axis-aligned bounds {x0, y0, x1, y1, w, h} of objects (their rotated boxes).
export function bounds(objs) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const o of objs) for (const [x, y] of corners(o)) {
    x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y);
  }
  if (!Number.isFinite(x0)) return null;
  return { x0, y0, x1, y1, w: x1 - x0, h: y1 - y0 };
}

// Point (machine mm) in the object's local frame: [u, v] from the box centre, before rotation.
export function toLocal(o, x, y) {
  const co = Math.cos(o.rot * RAD), si = Math.sin(o.rot * RAD);
  const [cx, cy] = center(o);
  const dx = x - cx, dy = y - cy;
  return [co * dx + si * dy, -si * dx + co * dy];
}

export function fromLocal(o, u, v) {
  const co = Math.cos(o.rot * RAD), si = Math.sin(o.rot * RAD);
  const [cx, cy] = center(o);
  return [cx + co * u - si * v, cy + si * u + co * v];
}

export function contains(o, x, y, pad = 0) {
  const [u, v] = toLocal(o, x, y);
  return Math.abs(u) <= o.w / 2 + pad && Math.abs(v) <= o.h / 2 + pad;
}

// Move an object so its centre is at (cx, cy).
export function setCenter(o, cx, cy) { o.x = cx - o.w / 2; o.y = cy - o.h / 2; }

// Anchor point of a bounds box: ax, ay in {0, 0.5, 1}.
export const anchorPoint = (b, [ax, ay]) => [b.x0 + ax * b.w, b.y0 + ay * b.h];

// Scale objects about a point by kx, ky. Exact for boxes turned a multiple of 90 degrees; other angles take the
// mean factor (a stretch along the bed's axes is a shear for them, which an object cannot hold).
export function scaleAbout(objs, px, py, kx, ky) {
  for (const o of objs) {
    const r = ((o.rot % 180) + 180) % 180;
    let lx, ly;
    if (r < 1e-6 || 180 - r < 1e-6) [lx, ly] = [kx, ky];
    else if (Math.abs(r - 90) < 1e-6) [lx, ly] = [ky, kx];
    else lx = ly = Math.sqrt(kx * ky);
    const [cx, cy] = center(o);
    o.w = Math.max(0.1, o.w * lx); o.h = Math.max(0.1, o.h * ly);
    setCenter(o, px + (cx - px) * kx, py + (cy - py) * ky);
  }
}

export function rotateAbout(objs, px, py, deg) {
  const co = Math.cos(deg * RAD), si = Math.sin(deg * RAD);
  for (const o of objs) {
    const [cx, cy] = center(o);
    const dx = cx - px, dy = cy - py;
    setCenter(o, px + co * dx - si * dy, py + si * dx + co * dy);
    o.rot = norm(o.rot + deg);
  }
}

// Mirror objects across a vertical (axis 'x') or horizontal line through (px, py).
export function mirror(objs, axis, px, py) {
  for (const o of objs) {
    const [cx, cy] = center(o);
    if (axis === 'x') { setCenter(o, 2 * px - cx, cy); o.fx = !o.fx; }
    else { setCenter(o, cx, 2 * py - cy); o.fy = !o.fy; }
    o.rot = norm(-o.rot);
  }
}

export const norm = (deg) => Math.round((((deg % 360) + 360) % 360) * 1e6) / 1e6;
export const r2 = (v) => Math.round(v * 100) / 100;

// Paper and inset canvas in machine mm, with the job's paper size if it has one.
export function canvasRect() {
  const m = state.me.machine;
  const p = state.doc && state.doc.settings && state.doc.settings.paper;
  const [px0, py0] = m.paper;
  const pw = p ? p.width : m.paper[2], ph = p ? p.height : m.paper[3];
  return { paper: [px0, py0, pw, ph], canvas: [px0 + m.margin, py0 + m.margin, px0 + pw - m.margin, py0 + ph - m.margin] };
}

export function outside(o) {
  const c = canvasRect().canvas, b = bounds([o]);
  return b.x0 < c[0] - 0.5 || b.y0 < c[1] - 0.5 || b.x1 > c[2] + 0.5 || b.y1 > c[3] + 0.5;
}
