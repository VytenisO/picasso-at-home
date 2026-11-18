// One state object, change notifications by key. Modules read store.state and subscribe to what they draw.

const subs = new Map();

export const state = {
  me: null,            // /api/me: sim, machine (bed, paper, canvas, start), limits, pens
  ws: null,            // shown workspace: prepare | plot
  files: [],
  converters: [],
  tools: [],
  doc: null,           // the bed: {name, objects: [{id, file, x, y, w, h, rot, fx, fy, g}], settings}
  sel: [],             // ids of the selected objects
  naturals: {},        // file id -> the drawing at natural size: {geom, layers, strokes}
  projects: [],        // saved projects: [{slug, name, objects, saved}]
  history: 0,          // undo depth, for the buttons
  startAt: 0,          // plot from this stroke of the plan's order (resume); never saved with the bed
  scrub: null,         // preview scrubber position (strokes shown), null = all
  preview: null,       // last /api/preview result for the bed
  previewStale: true,  // settings changed since the preview
  previewing: false,
  job: null,           // runner snapshot job
  position: null,      // {state: at_start|moved|unknown, at, note}
  jobPreview: null,    // immutable preview of the running/last job (strokes, order)
  liveStrokes: [],     // strokes written on the current page of a live session (machine mm)
  pos: null,           // live carriage position {x, y, pen}
  link: 'connecting',
  lastEventAt: 0,
};

export function on(keys, fn) {
  for (const k of [].concat(keys)) {
    if (!subs.has(k)) subs.set(k, new Set());
    subs.get(k).add(fn);
  }
}

export function set(patch) {
  Object.assign(state, patch);
  const fns = new Set();
  for (const k of Object.keys(patch)) for (const fn of subs.get(k) || []) fns.add(fn);
  for (const fn of fns) fn(state);
}

export const ACTIVE = ['starting', 'running', 'pausing', 'paused', 'stopping', 'returning'];
export const jobActive = () => !!(state.job && ACTIVE.includes(state.job.state));

export function prefs(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem('plotter.' + key));
    localStorage.setItem('plotter.' + key, JSON.stringify(value));
  } catch (e) { return null; }
  return value;
}
