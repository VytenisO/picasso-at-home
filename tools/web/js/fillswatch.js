// A fill's look before any preview runs: a square and a circle filled the way plot_svg fills them (lines, zigzag,
// cross-hatch, rings, auto by darkness), in the pen's colour and width, the outline on or off, lighter for a
// lighter touch. Approximate on purpose: the bed's Preview shows the real strokes.

const PX = 6;                                   // canvas px per mm
const SQ = { x: 2, y: 2, s: 20 }, CI = { cx: 37, cy: 12, r: 10 };
const W = 49, H = 24;                           // mm

// Fill methods as shown in the editor, and the settings each one writes (plotjob.validate_fills).
export const METHODS = [
  ['none', 'None', { mode: 'off' }],
  ['lines', 'Lines', { mode: 'hatch', zigzag: false, cross: false }],
  ['zigzag', 'Zigzag', { mode: 'hatch', zigzag: true, cross: false }],
  ['cross', 'Cross-hatch', { mode: 'hatch', zigzag: false, cross: true }],
  ['rings', 'Concentric rings', { mode: 'contour' }],
  ['auto', 'Auto (by darkness)', { mode: 'auto' }],
];
export function methodOf(f) {
  if (!f || f.mode === 'off') return 'none';
  if (f.mode === 'contour') return 'rings';
  if (f.mode === 'auto') return 'auto';
  if (f.cross) return 'cross';
  return f.zigzag === false ? 'lines' : 'zigzag';
}

const lum = (hex) => {
  const h = (hex || '#000000').replace('#', '');
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};

// Hatch segments of one shape at a spacing and angle, in order across the shape, alternate ones reversed for zigzag.
function hatch(shape, sp, deg, zigzag) {
  const t = deg * Math.PI / 180, d = [Math.cos(t), Math.sin(t)], n = [-d[1], d[0]];
  const c = shape.r ? [shape.cx, shape.cy] : [shape.x + shape.s / 2, shape.y + shape.s / 2];
  const R = shape.r || shape.s * Math.SQRT1_2;
  const segs = [];
  for (let k = Math.ceil(-R / sp); k * sp <= R; k++) {
    const o = k * sp, p = [c[0] + n[0] * o, c[1] + n[1] * o];
    let a, b;
    if (shape.r) {
      const h = Math.sqrt(Math.max(0, R * R - o * o));
      if (h < 0.1) continue;
      a = -h; b = h;
    } else {                                    // Liang-Barsky against the square
      a = -1e9; b = 1e9;
      for (const [pk, dk, lo, hi] of [[p[0], d[0], shape.x, shape.x + shape.s], [p[1], d[1], shape.y, shape.y + shape.s]]) {
        if (Math.abs(dk) < 1e-12) { if (pk < lo || pk > hi) { a = 1; b = 0; } continue; }
        const t1 = (lo - pk) / dk, t2 = (hi - pk) / dk;
        a = Math.max(a, Math.min(t1, t2)); b = Math.min(b, Math.max(t1, t2));
      }
      if (b - a < 0.2) continue;
    }
    const s = [[p[0] + d[0] * a, p[1] + d[1] * a], [p[0] + d[0] * b, p[1] + d[1] * b]];
    segs.push(zigzag && segs.length % 2 ? s.reverse() : s);
  }
  return segs;
}

function rings(shape, sp) {
  const out = [];
  for (let k = 1; ; k++) {
    const i = k * sp;
    if (shape.r) {
      if (shape.r - i < 0.15) break;
      out.push({ circle: shape.r - i });
    } else {
      if (shape.s - 2 * i < 0.3) break;
      const x0 = shape.x + i, y0 = shape.y + i, s = shape.s - 2 * i;
      out.push([[x0, y0], [x0 + s, y0], [x0 + s, y0 + s], [x0, y0 + s], [x0, y0]]);
    }
  }
  return out;
}

const shorten = (seg, by) => {
  const [a, b] = seg, L = Math.hypot(b[0] - a[0], b[1] - a[1]);
  if (L <= 2 * by + 0.2) return null;
  const u = [(b[0] - a[0]) / L, (b[1] - a[1]) / L];
  return [[a[0] + u[0] * by, a[1] + u[1] * by], [b[0] - u[0] * by, b[1] - u[1] * by]];
};

// cfg: the fill settings (validate_fills keys); ink: the fill colour; pen: {colour, width_mm} or null;
// pressure: the layer's pressure fraction (0 = the saved pen-down) or null.
export function drawSwatch(cv, cfg, ink, pen, pressure) {
  const dpr = window.devicePixelRatio || 1;
  cv.width = W * PX * dpr; cv.height = H * PX * dpr;
  cv.style.width = `${W * PX}px`; cv.style.height = `${H * PX}px`;
  const ctx = cv.getContext('2d');
  ctx.setTransform(PX * dpr, 0, 0, PX * dpr, 0, 0);
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, W, H);
  const colour = (pen && pen.colour && /^#[0-9a-f]{6}$/i.test(pen.colour) ? pen.colour : null) || ink || '#000000';
  const width = Math.max(1 / PX, (pen && pen.width_mm) || 0.4);
  ctx.strokeStyle = colour;
  ctx.lineCap = 'round'; ctx.lineJoin = 'round';
  const base = 1 - (pressure || 0);
  const touchOf = (pct) => Math.min(1, Math.max(0.1, base * (1 + (pct || 0) / 100)));
  const m = cfg.mode || 'off';
  const fillLines = [];
  for (const shape of [SQ, CI]) {
    if (m === 'contour') { fillLines.push(...rings(shape, cfg.spacing || 1)); continue; }
    if (m !== 'hatch' && m !== 'auto') continue;
    let sp = cfg.spacing || 1, cross = !!cfg.cross;
    if (m === 'auto') {
      const dark = Math.max(0, Math.min(1, 1 - lum(ink)));
      if (dark < 0.1) continue;
      sp /= dark;
      cross = cfg.cross !== false && dark >= 0.75;
    }
    const passes = m === 'hatch' ? (cfg.passes || 1) : 1;
    for (let p = 0; p < passes; p++) {
      const ang = (cfg.angle ?? 45) + p * (cfg.angle_step || 0);
      const zz = m === 'hatch' ? cfg.zigzag !== false : true;
      for (const a of cross ? [ang, ang + 90] : [ang]) {
        let segs = hatch(shape, sp, a, zz);
        if (cfg.inset) segs = segs.map((s) => shorten(s, cfg.inset)).filter(Boolean);
        if (zz && segs.length) {                // one stroke: each line joined to the next
          const run = [];
          for (const s of segs) run.push(...s);
          fillLines.push(run);
        } else fillLines.push(...segs);
      }
    }
  }
  const stroke = (pts) => {
    ctx.beginPath();
    if (pts.circle !== undefined) ctx.arc(CI.cx, CI.cy, pts.circle, 0, Math.PI * 2);
    else pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.stroke();
  };
  ctx.lineWidth = width;
  ctx.globalAlpha = touchOf(cfg.pressure_pct);
  // rings of the circle carry their radius; rings of the square are polylines
  for (const l of fillLines) stroke(l);
  if (cfg.outline !== false) {
    ctx.globalAlpha = touchOf(0);
    stroke([[SQ.x, SQ.y], [SQ.x + SQ.s, SQ.y], [SQ.x + SQ.s, SQ.y + SQ.s], [SQ.x, SQ.y + SQ.s], [SQ.x, SQ.y]]);
    stroke({ circle: CI.r });
  }
  ctx.globalAlpha = 1;
}
