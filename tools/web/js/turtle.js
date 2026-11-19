// Turtletoy code -> lines. The code runs in turtle-worker.js (no network, time limit); this side reads the slider
// comments, puts the chosen values into the code, and turns the result into an SVG the size of the sheet area.

export const SAMPLE = `// Paste code from turtletoy.net here, or write your own (API: turtletoy.net/syntax).
// A comment like "// min=3 max=24 step=1" after a constant makes it a slider.
const petals = 9; // min=3 max=36 step=1
const size = 48; // min=10 max=98 step=1
const turtle = new Turtle();

function walk(i) {
  turtle.jump(0, 0);
  turtle.setheading(i * 360 / petals);
  turtle.circle(size);
  return i < petals - 1;
}
`;

const CONTROL = /^(\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*)(-?[\d.]+(?:e-?\d+)?)(\s*;?\s*\/\/(.*))$/;

// Turtletoy's slider syntax: `const n = 5; // min=1 max=10 step=1 (One, Two, ...)`; labels make a drop-down.
export function parseControls(code) {
  const out = [];
  code.split('\n').forEach((text, line) => {
    const m = CONTROL.exec(text);
    if (!m) return;
    const c = m[5];
    const num = (k) => { const r = new RegExp(`\\b${k}\\s*=\\s*(-?[\\d.]+(?:e-?\\d+)?)`).exec(c); return r ? Number(r[1]) : null; };
    const min = num('min'), max = num('max');
    if (min === null || max === null || !(max > min)) return;
    const step = num('step') || (max - min) / 100;
    const labels = /\(([^)]*)\)/.exec(c);
    const p = { key: m[2], label: m[2].replace(/([a-z])([A-Z])/g, '$1 $2').replace(/_/g, ' '), line, default: Number(m[3]),
      min, max, step };
    if (labels) {
      const names = labels[1].split(',').map((s) => s.trim());
      p.type = 'select';
      p.options = names.map((n, i) => [String(min + i * step), n || String(min + i * step)]);
      p.default = String(p.default);
    } else {
      p.type = 'number';
      p.slider = true;
    }
    out.push(p);
  });
  return out;
}

export function applyValues(code, controls, values) {
  const lines = code.split('\n');
  for (const p of controls) {
    const v = Number(values[p.key]);
    if (!Number.isFinite(v)) continue;
    lines[p.line] = lines[p.line].replace(CONTROL, (_, head, __, ___, tail) => `${head}${v}${tail}`);
  }
  return lines.join('\n');
}

// -> { promise, stop }. promise resolves to the worker's 'done' message or rejects with an Error (.line when known).
export function runTurtle(code, { seconds = 30, onProgress } = {}) {
  const w = new Worker('js/turtle-worker.js');
  let finish;
  const promise = new Promise((resolve, reject) => {
    const hard = setTimeout(() => finish(null, new Error(`Stopped: still running after ${seconds + 5} s (an endless loop?).`)), (seconds + 5) * 1000);
    finish = (msg, err) => {
      clearTimeout(hard);
      w.terminate();
      if (err) reject(err); else resolve(msg);
    };
    w.onmessage = (e) => {
      const m = e.data;
      if (m.type === 'progress') { if (onProgress) onProgress(m); return; }
      if (m.type === 'error') {
        const err = new Error(m.line ? `Line ${m.line}: ${m.message}` : m.message);
        err.line = m.line;
        finish(null, err);
        return;
      }
      finish(m);
    };
    w.onerror = (e) => { e.preventDefault(); finish(null, new Error(e.message || 'The code could not run.')); };
  });
  w.postMessage({ code, seconds });
  return { promise, stop: () => finish(null, new Error('Stopped.')) };
}

// Turtletoy's canvas is 200 x 200 units centred on 0; it becomes sizeMm square on paper.
export function toSvg(paths, sizeMm) {
  const d = paths.map((p) => {
    let s = `M${p[0]} ${p[1]}`;
    for (let i = 2; i < p.length; i += 2) s += `L${p[i]} ${p[i + 1]}`;
    return s;
  }).join('');
  const sw = (0.3 * 200 / sizeMm).toFixed(3);
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${sizeMm}mm" height="${sizeMm}mm" viewBox="-100 -100 200 200">`
    + `<path d="${d}" fill="none" stroke="#000000" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}

// A name for the drawing: the first comment line of the code, cleaned up.
export function titleOf(code) {
  const m = /^\s*\/\/\s*(.+)$/m.exec(code);
  const t = m && !/paste code|turtletoy\.net\/syntax/i.test(m[1]) ? m[1] : 'turtletoy';
  return t.replace(/[^\w\- ]+/g, ' ').trim().split(/\s+/).slice(0, 5).join('-').toLowerCase().slice(0, 60) || 'turtletoy';
}
