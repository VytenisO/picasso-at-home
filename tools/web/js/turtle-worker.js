// Runs pasted turtletoy.net code and returns the lines it draws. The server sends this file with a CSP that blocks
// every network request (default-src 'none'), and the globals that reach the network are removed before the code
// runs, so a pasted script can draw and compute but cannot talk to the plotter server.
'use strict';

const LIMIT = 100;                  // Turtletoy's canvas: -100..100 on both axes, y down
const MAX_POINTS = 500000;

(() => {
  // Defence in depth on top of the CSP: drop network and storage entry points, on self and up its prototype chain.
  const names = ['fetch', 'XMLHttpRequest', 'WebSocket', 'EventSource', 'importScripts', 'Worker', 'SharedWorker',
    'indexedDB', 'caches', 'BroadcastChannel', 'WebTransport', 'navigator'];
  for (let o = self; o; o = Object.getPrototypeOf(o)) {
    for (const n of names) {
      try { Object.defineProperty(o, n, { value: undefined, writable: false, configurable: false }); } catch (e) { /* not there */ }
    }
  }
})();

const post = self.postMessage.bind(self);
let paths = [], cur = null, points = 0, full = false;

function addPoint(p) {
  if (points >= MAX_POINTS) { full = true; return; }
  cur.push(Math.round(p[0] * 1000) / 1000, Math.round(p[1] * 1000) / 1000);
  points++;
}

// Liang-Barsky: the part of a segment inside the canvas, or null.
function clip(x0, y0, x1, y1) {
  let t0 = 0, t1 = 1;
  const dx = x1 - x0, dy = y1 - y0;
  for (const [p, q] of [[-dx, x0 + LIMIT], [dx, LIMIT - x0], [-dy, y0 + LIMIT], [dy, LIMIT - y0]]) {
    if (p === 0) { if (q < 0) return null; continue; }
    const r = q / p;
    if (p < 0) { if (r > t1) return null; if (r > t0) t0 = r; } else { if (r < t0) return null; if (r < t1) t1 = r; }
  }
  // exact ends when not cut, so the next segment joins this stroke
  return [t0 === 0 ? [x0, y0] : [x0 + t0 * dx, y0 + t0 * dy], t1 === 1 ? [x1, y1] : [x0 + t1 * dx, y0 + t1 * dy]];
}

function line(x0, y0, x1, y1) {
  if (full || ![x0, y0, x1, y1].every(Number.isFinite)) return;
  const c = clip(x0, y0, x1, y1);
  if (!c) { cur = null; return; }
  const [a, b] = c;
  const n = cur ? cur.length : 0;
  if (!cur || Math.abs(cur[n - 2] - a[0]) > 1e-3 || Math.abs(cur[n - 1] - a[1]) > 1e-3) {
    cur = [];
    paths.push(cur);
    addPoint(a);
  }
  addPoint(b);
  if (b[0] !== x1 || b[1] !== y1) cur = null;        // left the canvas: the next piece starts a new stroke
}

const Canvas = { setpenopacity() {}, width: 200, height: 200 };

class Turtle {
  constructor(x = 0, y = 0) {
    if (Array.isArray(x)) [x, y] = x;
    this._x = x; this._y = y; this._a = 0; this._down = true; this._full = 360;
  }
  clone() { const t = new Turtle(this._x, this._y); t._a = this._a; t._down = this._down; t._full = this._full; return t; }
  _rad(a) { return a * 2 * Math.PI / this._full; }
  forward(d) {
    const r = this._rad(this._a);
    this.goto(this._x + d * Math.cos(r), this._y + d * Math.sin(r));
  }
  fd(d) { this.forward(d); }
  backward(d) { this.forward(-d); }
  bk(d) { this.forward(-d); }
  back(d) { this.forward(-d); }
  right(a) { this._a += a; }
  rt(a) { this.right(a); }
  left(a) { this._a -= a; }
  lt(a) { this.left(a); }
  penup() { this._down = false; cur = null; }
  pu() { this.penup(); }
  up() { this.penup(); }
  pendown() { this._down = true; }
  pd() { this.pendown(); }
  down() { this.pendown(); }
  isdown() { return this._down; }
  isDown() { return this._down; }
  goto(x, y) {
    if (Array.isArray(x)) [x, y] = x;
    if (this._down) line(this._x, this._y, x, y); else cur = null;
    this._x = x; this._y = y;
  }
  setpos(x, y) { this.goto(x, y); }
  setposition(x, y) { this.goto(x, y); }
  jump(x, y) { const d = this._down; this.penup(); this.goto(x, y); if (d) this.pendown(); }
  setx(x) { this.goto(x, this._y); }
  sety(y) { this.goto(this._x, y); }
  setheading(a) { this._a = a; }
  seth(a) { this.setheading(a); }
  heading() { return this._a; }
  h() { return this._a; }
  home() { this.goto(0, 0); this._a = 0; }
  position() { return [this._x, this._y]; }
  pos() { return this.position(); }
  xcor() { return this._x; }
  ycor() { return this._y; }
  x() { return this._x; }
  y() { return this._y; }
  towards(x, y) {
    if (Array.isArray(x)) [x, y] = x;
    return Math.atan2(y - this._y, x - this._x) * this._full / (2 * Math.PI);
  }
  distance(x, y) { if (Array.isArray(x)) [x, y] = x; return Math.hypot(x - this._x, y - this._y); }
  degrees(full = 360) { this._a = this._a / this._full * full; this._full = full; }
  radians() { this.degrees(2 * Math.PI); }
  toradians(a) { return this._rad(a); }
  // Same as Python's turtle.circle: the centre is radius units to the left; negative radius turns the other way.
  circle(radius, extent = this._full, steps) {
    const frac = Math.abs(extent) / this._full;
    if (!steps) steps = 1 + Math.floor(Math.min(11 + Math.abs(radius) / 6, 59) * frac);
    let w = extent / steps, w2 = w / 2;
    let l = 2 * radius * Math.sin(this._rad(w2));
    if (radius < 0) { l = -l; w = -w; w2 = -w2; }
    this.left(w2);
    for (let i = 0; i < steps; i++) { this.forward(l); this.left(w); }
    this.left(-w2);
  }
}

// Where in the pasted code an error happened: new Function adds two lines before the body.
function where(e) {
  const m = /<anonymous>:(\d+):(\d+)/.exec(String(e && e.stack));
  return m ? Number(m[1]) - 2 : null;
}

self.onmessage = (ev) => {
  const { code, seconds = 30 } = ev.data;
  paths = []; cur = null; points = 0; full = false;
  const t0 = Date.now();
  let iters = 0, timedOut = false;
  try {
    const make = new Function('Turtle', 'Canvas', `${code}\n;return typeof walk === 'function' ? walk : null;`);
    const walk = make(Turtle, Canvas);
    if (walk) {
      for (let i = 0; ; i++) {
        if (!walk(i, 0)) break;
        iters = i + 1;
        if (full) break;
        if ((i & 255) === 0) {
          const ms = Date.now() - t0;
          if (ms > seconds * 1000) { timedOut = true; break; }
          if ((i & 4095) === 0) post({ type: 'progress', iters: i, points, ms });
        }
      }
    }
  } catch (e) {
    post({ type: 'error', message: String(e && e.message || e), line: where(e), points });
    return;
  }
  post({ type: 'done', paths: paths.filter((p) => p.length >= 4), iters, points, full, timedOut, ms: Date.now() - t0 });
};
