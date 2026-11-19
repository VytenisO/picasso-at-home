// Prepare workspace: source -> recipe of plugin steps (adjust, method, clean up) -> plot-ready drawing, previewed in
// ink on the sheet with the same renderer Plot uses. The server owns the steps (GET /api/prepare/steps): adding a
// step there adds it here, forms included. The recipe is kept in this browser, so a reload resumes the work.
import { api, post } from './api.js';
import { state, on, prefs, jobActive } from './store.js';
import { $, el, toast, schemaForm, formValues, fmtTime, ask, askText } from './ui.js';
import { drawInk, inkMode, setInkMode } from './ink.js';
import { openFile, refreshFiles } from './workspace.js';
import { showWs } from './ws.js';
import { SAMPLE, parseControls, applyValues, runTurtle, toSvg, titleOf } from './turtle.js';

const STAGE = {
  adjust: { title: 'Adjust the image', add: 'Add an adjustment', hint: 'Applied in order, before tracing.' },
  method: { title: 'Method', hint: '' },
  clean: { title: 'Clean up', add: 'Add a clean-up step', hint: 'Work on the drawn lines, in order.' },
};

let steps = [], byId = {}, kinds = {}, maxBytes = 12 * 1024 * 1024;
const P = {
  source: null,         // {id, name, kind, px, bytes}
  list: [],             // [{uid, step, on, params}]
  memo: {},             // method id -> last params, so switching back restores them
  settings: { fill: 0, fill_angle: 45, layers: [] },
  name: '', replace: null, replaceName: '',
  turtle: null,         // {code, values, size, src}: turtletoy code that made the source (src = its source id)
  result: null, error: null, busy: false, stale: false,
};
let uid = 1, gen = 0, timer = null;
let cv, ctx, W = 0, H = 0, dpr = 1, raf = 0;
const view = { s: 2, ox: 0, oy: 0, user: false };
let mode = 'result';
const images = new Map();

// ------------------------------------------------------------------ init
export async function initPrepare() {
  cv = $('prepCanvas');
  ctx = cv.getContext('2d');
  new ResizeObserver(resize).observe(cv);
  cv.addEventListener('pointerdown', panStart);
  cv.addEventListener('pointermove', panMove);
  cv.addEventListener('pointerup', panEnd);
  cv.addEventListener('pointercancel', panEnd);
  cv.addEventListener('wheel', (e) => { e.preventDefault(); const [x, y] = local(e); zoomBy(e.deltaY < 0 ? 1.15 : 1 / 1.15, x, y); }, { passive: false });
  $('prepZoomIn').onclick = () => zoomBy(1.25);
  $('prepZoomOut').onclick = () => zoomBy(0.8);
  $('prepZoomFit').onclick = prepFit;
  const inkBtn = $('prepInk');
  const showInk = () => { inkBtn.setAttribute('aria-pressed', String(inkMode())); inkBtn.textContent = inkMode() ? 'Ink' : 'Lines'; };
  inkBtn.onclick = () => { setInkMode(!inkMode()); showInk(); redraw(); };
  on('ws', () => { showInk(); if (state.ws === 'prepare') requestAnimationFrame(resize); });
  showInk();
  for (const b of $('prepModes').querySelectorAll('button')) b.onclick = () => setMode(b.dataset.mode);
  $('prepFade').oninput = redraw;
  $('prepFile').onchange = (e) => { const f = e.target.files[0]; e.target.value = ''; if (f) prepareFile(f); };
  $('prepFillForm').addEventListener('change', onFill);
  $('prepFillForm').addEventListener('submit', (e) => e.preventDefault());
  $('prepName').oninput = () => { P.name = $('prepName').value; save(); };
  $('prepSend').onclick = () => send(true);
  $('prepSaveNew').onclick = () => send(false, true);
  $('prepReset').onclick = reset;
  initDrop();
  // The sheet and ink colours come from the theme: redraw when it changes.
  new MutationObserver(redraw).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', redraw);
  on(['job', 'ws'], jobDot);
  on('me', () => { renderLayers(); redraw(); });
  let swapSeen = null;
  on('job', () => {
    const j = state.job;
    const swap = j && j.state === 'paused' && j.pause && j.pause.kind === 'swap' ? j.id + (j.pause.label || '') : null;
    if (swap && swap !== swapSeen && state.ws === 'prepare') toast('The plot is waiting for a pen change. Switch to Plot.');
    swapSeen = swap;
  });
  try {
    const r = await api('/api/prepare/steps');
    steps = r.steps; kinds = r.kinds; maxBytes = r.max_bytes || maxBytes;
    byId = Object.fromEntries(steps.map((s) => [s.id, s]));
    $('prepFile').accept = Object.values(kinds).flat().join(',');
  } catch (e) { toast(e.message, true); }
  restore();
  initPresets();
}

// ------------------------------------------------------------------ presets
// Named recipes on the server: the list shows the ones made for the current kind of source.
let presets = [];
async function initPresets() {
  $('presetApply').onclick = () => {
    const p = presets.find((x) => x.name === $('presetSel').value);
    if (!p) return;
    P.list = p.steps.filter((s) => byId[s.step] && fits(byId[s.step])).map((s) => ({ uid: uid++, step: s.step, on: s.on !== false, params: s.params || {} }));
    P.settings = { ...P.settings, fill: p.settings.fill || 0, fill_angle: p.settings.fill_angle ?? 45 };
    P.result = null; P.error = null;
    renderAll(); save(); schedule(0);
    toast(`Preset ${p.name} applied.`);
  };
  $('presetSave').onclick = async () => {
    const cur = $('presetSel').value;
    const name = await askText('Save as a preset', 'A name for this recipe. A preset with the same name is replaced.', cur || '', 'Save');
    if (name === null || !name.trim()) return;
    try {
      const r = await post('/api/prepare/presets/save', { name, kind: kindNow(), steps: P.list.map(({ step, on: o, params }) => ({ step, on: o, params })),
        settings: { fill: P.settings.fill, fill_angle: P.settings.fill_angle } });
      presets = r.presets;
      renderPresets(r.preset.name);
      toast(`Preset ${r.preset.name} saved.`);
    } catch (e) { toast(e.message, true); }
  };
  $('presetDel').onclick = async () => {
    const name = $('presetSel').value;
    if (!name || !(await ask('Delete preset', `Delete the preset ${name}?`, 'Delete'))) return;
    try { presets = (await post('/api/prepare/presets/delete', { name })).presets; renderPresets(); } catch (e) { toast(e.message, true); }
  };
  try { presets = (await api('/api/prepare/presets')).presets; } catch (e) { presets = []; }
  renderPresets();
}

function renderPresets(pick) {
  const box = $('prepPresets');
  const has = !!methodOf();
  const mine = presets.filter((p) => p.kind === kindNow());
  box.hidden = !has && !mine.length;
  const sel = $('presetSel'), was = pick || sel.value;
  sel.replaceChildren(...(mine.length ? mine.map((p) => el('option', { value: p.name, text: p.name }))
    : [el('option', { value: '', text: 'No presets for this kind of source' })]));
  if (mine.some((p) => p.name === was)) sel.value = was;
  sel.disabled = $('presetApply').disabled = $('presetDel').disabled = !mine.length;
  $('presetSave').disabled = !has;
}

function restore() {
  const saved = prefs('prep');
  if (saved && Array.isArray(saved.list)) {
    Object.assign(P, { source: saved.source || null, memo: saved.memo || {}, name: saved.name || '',
      replace: saved.replace || null, replaceName: saved.replaceName || '',
      settings: { fill: 0, fill_angle: 45, layers: [], ...(saved.settings || {}) }, turtle: saved.turtle || null });
    P.list = saved.list.filter((s) => byId[s.step]).map((s) => ({ ...s, uid: uid++ }));
  }
  renderAll();
  if (methodOf()) schedule(0);
}

function save() {
  prefs('prep', { source: P.source, list: P.list.map(({ step, on: o, params }) => ({ step, on: o, params })),
    memo: P.memo, settings: P.settings, name: P.name, replace: P.replace, replaceName: P.replaceName, turtle: P.turtle });
}

// ------------------------------------------------------------------ public entry points
export async function prepareFile(file) {
  if (file.size > maxBytes) { toast(`${file.name} is larger than ${Math.round(maxBytes / 1048576)} MB.`, true); return; }
  setBusy(`Reading ${file.name}...`);
  P.turtle = null;
  try {
    const r = await api('/api/prepare/source', { method: 'POST', raw: file, headers: { 'X-Filename': encodeURIComponent(file.name) } });
    useSource(r.source, { name: file.name.replace(/\.[^.]+$/, '') });
  } catch (e) { setBusy(''); toast(e.message, true); }
}

export async function prepareFromLibrary(id) {
  setBusy('Opening...');
  try {
    const r = await post('/api/prepare/from_library', { file: id });
    P.turtle = null;
    P.source = r.source;
    P.list = r.recipe.steps.filter((s) => byId[s.step]).map((s) => ({ uid: uid++, step: s.step, on: s.on !== false, params: s.params || {} }));
    P.settings = { fill: r.settings.fill || 0, fill_angle: r.settings.fill_angle ?? 45, layers: r.settings.layers || [] };
    P.name = r.name; P.replace = r.replace; P.replaceName = r.name;
    P.result = null; P.error = null; view.user = false;
    renderAll(); save(); schedule(0);
  } catch (e) { setBusy(''); toast(e.message, true); }
}

function useSource(src, { name }) {
  const prevKind = P.source ? P.source.kind : (methodOf() && !byId[methodOf().step].accepts.length ? 'none' : null);
  P.source = src;
  if (prevKind !== src.kind || !methodOf()) {
    // A new kind of source: keep the clean-up steps, rebuild the rest around the default method.
    const clean = P.list.filter((s) => byId[s.step].stage === 'clean');
    const def = { image: 'contour', svg: 'svg', mesh: 'mesh' }[src.kind];
    P.list = [{ uid: uid++, step: def, on: true, params: P.memo[def] || {} }, ...clean];
  }
  P.name = name; P.replace = null; P.replaceName = '';
  P.result = null; P.error = null; view.user = false;
  renderAll(); save(); schedule(0);
}

function startGenerator(id) {
  P.source = null;
  P.list = [{ uid: uid++, step: id, on: true, params: P.memo[id] || {} },
    ...P.list.filter((s) => byId[s.step].stage === 'clean')];
  P.name = byId[id].title.toLowerCase().replace(/\s+/g, '-');
  P.replace = null; P.replaceName = '';
  P.result = null; P.error = null; view.user = false;
  renderAll(); save(); schedule(0);
}

function reset() {
  Object.assign(P, { source: null, list: [], settings: { fill: 0, fill_angle: 45, layers: [] }, name: '',
    replace: null, replaceName: '', result: null, error: null, turtle: null });
  if (turtleRun) turtleRun.stop();
  turtleSig = '';
  gen++;
  setBusy('');
  renderAll(); save();
}

// ------------------------------------------------------------------ turtletoy code
// The code runs in a worker without network access; the lines become an SVG source, so the method, clean-up, fill
// and pens work on it as on any drawing. The editor is built once and kept, so redraws do not lose the cursor.
let turtleBox = null, turtleRun = null, turtleSig = '';

function startTurtle() {
  P.turtle = P.turtle || { code: SAMPLE, values: {}, size: 180, src: null };
  if (P.source && P.source.id !== P.turtle.src) P.turtle.src = null;
  renderSource(); save();
  $('turtleCode').focus();
  if (!P.turtle.src) turtleDraw();
}

function turtleEditor() {
  if (!turtleBox) {
    const code = el('textarea', { id: 'turtleCode', class: 'code', rows: 16, spellcheck: 'false', wrap: 'off',
      'aria-label': 'Turtletoy code', autocomplete: 'off' });
    let t = null;
    code.oninput = () => {
      P.turtle.code = code.value; save();
      clearTimeout(t);
      t = setTimeout(() => turtleControls(), 400);
    };
    const size = el('input', { id: 'turtleSize', type: 'number', min: 20, max: 400, step: 1 });
    size.onchange = () => {
      P.turtle.size = Math.min(400, Math.max(20, Math.round(Number(size.value)) || 180));
      size.value = P.turtle.size; save(); turtleDraw();
    };
    turtleBox = el('div', { class: 'turtle' },
      el('div', { class: 'row turtle-head' }, el('b', { text: 'Turtletoy code' }), el('span', { class: 'spacer' }),
        el('a', { href: 'https://turtletoy.net/syntax', target: '_blank', rel: 'noopener noreferrer', text: 'API' }),
        el('button', { type: 'button', class: 'link', text: 'Remove', onclick: reset })),
      code,
      el('form', { id: 'turtleForm', class: 'step-form', onsubmit: (e) => e.preventDefault() }),
      el('label', { class: 'field', for: 'turtleSize' }, el('span', { text: 'Size on paper (mm): the 200 x 200 canvas' }), size),
      el('div', { class: 'row' },
        el('button', { type: 'button', id: 'turtleDraw', class: 'primary', text: 'Draw', title: 'Run the code (Ctrl+Enter)', onclick: () => turtleDraw() }),
        el('button', { type: 'button', id: 'turtleStop', text: 'Stop', hidden: true, onclick: () => turtleRun && turtleRun.stop() })),
      el('p', { id: 'turtleStatus', class: 'hint', 'aria-live': 'polite' }),
      el('p', { class: 'hint', text: 'Runs here in the browser, cut off from the network, for at most 30 s. Pen opacity is '
        + 'ignored: every line is drawn at full ink, so scripts that build tone from many faint lines plot very dark.' }));
  }
  const code = turtleBox.querySelector('#turtleCode');
  if (code.value !== P.turtle.code) code.value = P.turtle.code;
  turtleBox.querySelector('#turtleSize').value = P.turtle.size;
  turtleControls();
  return turtleBox;
}

// Sliders from the code's "// min= max= step=" comments; rebuilt only when the set of controls changes.
function turtleControls() {
  if (!P.turtle) return;
  const form = turtleBox.querySelector('#turtleForm');
  const controls = parseControls(P.turtle.code);
  const sig = JSON.stringify(controls.map(({ key, min, max, step, options }) => [key, min, max, step, options]));
  if (sig === turtleSig) return;
  turtleSig = sig;
  const values = {};
  for (const c of controls) values[c.key] = P.turtle.values[c.key] ?? c.default;
  schemaForm(form, controls, null, values);
  let t = null;
  const take = (ms) => {
    Object.assign(P.turtle.values, formValues(form, controls)); save();
    clearTimeout(t); t = setTimeout(() => turtleDraw(), ms);
  };
  form.oninput = () => take(500);
  form.onchange = () => take(150);
}

async function turtleDraw() {
  if (!P.turtle) return;
  if (turtleRun) turtleRun.stop();
  const status = $('turtleStatus'), stopBtn = $('turtleStop');
  const code = applyValues(P.turtle.code, parseControls(P.turtle.code), P.turtle.values);
  const run = runTurtle(code, { seconds: 30, onProgress: (m) => {
    status.textContent = `Running: ${m.iters} steps, ${m.points} points, ${(m.ms / 1000).toFixed(1)} s`;
  } });
  turtleRun = run;
  stopBtn.hidden = false;
  status.textContent = 'Running...';
  let r;
  try {
    r = await run.promise;
  } catch (e) {
    if (turtleRun === run) { status.textContent = e.message; stopBtn.hidden = true; turtleRun = null; }
    return;
  }
  if (turtleRun !== run) return;
  turtleRun = null;
  stopBtn.hidden = true;
  if (!r.paths.length) { status.textContent = 'The code ran but drew nothing inside the canvas (-100 to 100).'; return; }
  const notes = [r.timedOut && 'stopped at the 30 s limit', r.full && 'stopped at 500,000 points'].filter(Boolean);
  status.textContent = `${r.paths.length} lines, ${r.points} points, ${(r.ms / 1000).toFixed(1)} s`
    + (notes.length ? ` (${notes.join(', ')})` : '');
  const name = titleOf(P.turtle.code);
  try {
    const up = await api('/api/prepare/source', { method: 'POST', raw: new Blob([toSvg(r.paths, P.turtle.size)], { type: 'image/svg+xml' }),
      headers: { 'X-Filename': encodeURIComponent(`${name}.svg`) } });
    if (!P.turtle) return;
    const keep = P.source && P.source.id === P.turtle.src && methodOf();
    P.turtle.src = up.source.id;
    if (keep) {           // a redraw: same recipe, new lines; the name follows the code's title unless typed over
      if (!P.name || P.name === P.turtle.title) P.name = name;
      P.turtle.title = name;
      P.source = up.source; P.result = null; P.error = null;
      renderAll(); save(); schedule(0);
    } else { P.turtle.title = name; useSource(up.source, { name }); }
  } catch (e) { status.textContent = e.message; }
}

// ------------------------------------------------------------------ recipe model
const kindNow = () => (P.source ? P.source.kind : 'none');
const methodOf = () => P.list.find((s) => byId[s.step] && byId[s.step].stage === 'method');
const ORDER = { adjust: 0, method: 1, clean: 2 };
const byStage = (list) => list.map((s, i) => [s, i]).sort((a, b) => ORDER[byId[a[0].step].stage] - ORDER[byId[b[0].step].stage] || a[1] - b[1]).map(([s]) => s);
const fits = (step) => (step.accepts.length ? step.accepts.includes(kindNow()) : kindNow() === 'none');

function recipe() {
  return { source: P.source ? P.source.id : null, steps: P.list.map(({ step, on: o, params }) => ({ step, on: o, params })) };
}

function schedule(ms = 350) {
  clearTimeout(timer);
  P.stale = true;
  redraw();
  if (!methodOf()) return;
  timer = setTimeout(run, ms);
}

async function run() {
  clearTimeout(timer);
  const my = ++gen;
  setBusy('Working...');
  try {
    const r = await post('/api/prepare/run', { recipe: recipe(), settings: { fill: P.settings.fill, fill_angle: P.settings.fill_angle } });
    if (my !== gen) return;
    P.result = r; P.error = null; P.stale = false;
    if (!view.user) fitSoon();
    setBusy(r.ms < 60 ? 'from cache' : `${(r.ms / 1000).toFixed(1)} s`, true);
  } catch (e) {
    if (my !== gen) return;
    P.error = e.message; P.stale = false;
    setBusy('');
  }
  renderResult();
  redraw();
}

function setBusy(text, done = false) {
  const b = $('prepBusy');
  b.textContent = text;
  b.classList.toggle('working', !!text && !done);
}

// ------------------------------------------------------------------ left: source and recipe
function renderAll() {
  renderSource();
  renderRecipe();
  if ($('presetSel')) renderPresets();
  renderResult();
  $('prepName').value = P.name;
  redraw();
}

function renderSource() {
  const box = $('prepSource');
  const generators = steps.filter((s) => s.stage === 'method' && !s.accepts.length);
  const m = methodOf();
  if (P.turtle && (!P.source || P.source.id === P.turtle.src)) { box.replaceChildren(turtleEditor()); return; }
  if (!P.source) {
    const drop = el('div', { class: 'drop', tabindex: '0', role: 'button', 'aria-label': 'Choose a file to prepare',
      onclick: () => $('prepFile').click(),
      onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('prepFile').click(); } } },
    'Drop an image, SVG or 3D model, or ', el('b', { text: 'choose one' }),
    el('br'), el('small', { text: Object.values(kinds).flat().join(' ') }));
    const gen_ = el('div', { class: 'gen-row' }, el('span', { class: 'muted', text: 'or start from' }),
      ...generators.map((g) => el('button', { type: 'button', class: m && m.step === g.id ? 'chip on' : 'chip', text: g.title,
        'aria-pressed': String(!!(m && m.step === g.id)), onclick: () => startGenerator(g.id) })),
      el('button', { type: 'button', class: 'chip', text: 'Turtletoy code', title: 'Paste a script from turtletoy.net',
        onclick: startTurtle }));
    box.replaceChildren(drop, gen_);
    return;
  }
  const s = P.source;
  const thumb = s.kind === 'mesh' ? el('div', { class: 'thumb glyph', text: '3D' })
    : el('img', { class: 'thumb', alt: '', src: `/api/prepare/file?kind=source&id=${s.id}` });
  const facts = [s.kind === 'image' ? 'image' : s.kind === 'svg' ? 'SVG drawing' : '3D model',
    s.px ? `${s.px[0]} x ${s.px[1]} px` : '', `${Math.max(1, Math.round(s.bytes / 1024))} KB`].filter(Boolean).join(' · ');
  box.replaceChildren(el('div', { class: 'source-card' }, thumb,
    el('div', { class: 'source-meta' }, el('div', { class: 'name', text: s.name, title: s.name }), el('div', { class: 'muted', text: facts }),
      el('div', { class: 'row' },
        el('button', { type: 'button', class: 'link', text: 'Replace', title: 'Same recipe, another file', onclick: () => $('prepFile').click() }),
        el('button', { type: 'button', class: 'link', text: 'Remove', onclick: reset })))));
}

function renderRecipe() {
  const box = $('prepRecipe');
  const m = methodOf();
  if (!m && !P.source) { box.replaceChildren(); return; }
  const parts = [];
  let n = 0;
  for (const stage of ['adjust', 'method', 'clean']) {
    const avail = steps.filter((s) => s.stage === stage && fits(s));
    if (!avail.length) continue;
    n++;
    const cards = stage === 'method' ? [methodCard(avail)]
      : P.list.filter((s) => byId[s.step].stage === stage).map((s, i, arr) => stepCard(s, i, arr.length));
    let add = null;
    if (stage !== 'method') {
      add = el('select', { class: 'add-step', 'aria-label': STAGE[stage].add, onchange: (e) => {
        const id = e.target.value;
        if (!id) return;
        P.list = byStage([...P.list, { uid: uid++, step: id, on: true, params: {} }]);
        renderRecipe(); save(); schedule(0);
      } }, el('option', { value: '', text: `+ ${STAGE[stage].add}...` }), avail.map((s) => el('option', { value: s.id, text: s.title })));
    }
    parts.push(el('div', { class: 'stage' },
      el('div', { class: 'stage-head' }, el('span', { class: 'stage-n', text: String(n) }), el('h2', { text: STAGE[stage].title })),
      STAGE[stage].hint && (stage !== 'method') && !cards.length ? el('p', { class: 'hint', text: STAGE[stage].hint }) : null,
      ...cards, add));
  }
  box.replaceChildren(...parts);
}

function methodCard(avail) {
  const cur = methodOf();
  const sel = el('select', { 'aria-label': 'Method', onchange: (e) => {
    const id = e.target.value;
    if (cur) P.memo[cur.step] = cur.params;
    const item = { uid: uid++, step: id, on: true, params: P.memo[id] || {} };
    P.list = byStage([...P.list.filter((x) => x !== cur), item]);
    renderRecipe(); renderSource(); save(); schedule(0);
  } });
  if (!cur) sel.append(el('option', { value: '', text: 'Choose a method...' }));
  const groups = {};
  for (const s of avail) (groups[s.group || ''] ||= []).push(s);
  for (const [g, list] of Object.entries(groups)) {
    const opts = list.map((s) => el('option', { value: s.id, text: s.title }));
    sel.append(...(g && Object.keys(groups).length > 1 ? [el('optgroup', { label: g }, opts)] : opts));
  }
  if (cur) sel.value = cur.step;
  const card = el('div', { class: 'step method' }, el('label', { class: 'field' }, el('span', { text: 'Turn it into lines with' }), sel));
  if (cur) {
    const def = byId[cur.step];
    if (def.help) card.append(el('p', { class: 'hint', text: def.help }));
    card.append(stepForm(cur, def));
  }
  return card;
}

function stepCard(s, i, count) {
  const def = byId[s.step];
  const toggle = el('input', { type: 'checkbox', 'aria-label': `Use ${def.title}`, title: 'Use this step' });
  toggle.checked = s.on !== false;
  toggle.onchange = () => { s.on = toggle.checked; card.classList.toggle('off', !s.on); save(); schedule(0); };
  const move = (d) => {
    const same = P.list.filter((x) => byId[x.step].stage === def.stage);
    const j = i + d;
    if (j < 0 || j >= same.length) return;
    const a = P.list.indexOf(same[i]), b = P.list.indexOf(same[j]);
    [P.list[a], P.list[b]] = [P.list[b], P.list[a]];
    renderRecipe(); save(); schedule(0);
  };
  const open = s.open !== false;
  const head = el('div', { class: 'step-head' }, toggle,
    el('button', { type: 'button', class: 'step-title', text: def.title, 'aria-expanded': String(open), disabled: !def.params.length,
      onclick: () => { s.open = !open; renderRecipe(); } }),
    el('span', { class: 'spacer' }),
    el('button', { type: 'button', class: 'icon mini', text: '\u2191', title: 'Move up', 'aria-label': `Move ${def.title} up`, disabled: i === 0, onclick: () => move(-1) }),
    el('button', { type: 'button', class: 'icon mini', text: '\u2193', title: 'Move down', 'aria-label': `Move ${def.title} down`, disabled: i === count - 1, onclick: () => move(1) }),
    el('button', { type: 'button', class: 'icon mini', text: '\u00d7', title: 'Remove', 'aria-label': `Remove ${def.title}`,
      onclick: () => { P.list.splice(P.list.indexOf(s), 1); renderRecipe(); save(); schedule(0); } }));
  const card = el('div', { class: 'step' + (s.on === false ? ' off' : '') }, head);
  if (def.params.length && open) {
    if (def.help) card.append(el('p', { class: 'hint', text: def.help }));
    card.append(stepForm(s, def));
  } else if (!def.params.length && def.help) card.append(el('p', { class: 'hint', text: def.help }));
  return card;
}

function stepForm(s, def) {
  const form = el('form', { id: `prep-${s.uid}`, class: 'step-form' });
  schemaForm(form, def.params, state.me && state.me.pens, s.params);
  const take = (ms) => { s.params = formValues(form, def.params); save(); schedule(ms); };
  form.addEventListener('input', (e) => take(e.target.type === 'text' || e.target.tagName === 'TEXTAREA' ? 700 : 350));
  form.addEventListener('change', () => take(150));
  form.onsubmit = (e) => e.preventDefault();
  return form;
}

// ------------------------------------------------------------------ right: result, pens, save
function renderResult() {
  const r = P.result, err = P.error;
  $('prepError').hidden = !err;
  $('prepError').textContent = err || '';
  $('prepEmpty').hidden = !!(methodOf() || P.source);
  const has = !!r && !err;
  $('prepSend').disabled = !has;
  $('prepSaveNew').disabled = !has;
  const dl = $('prepDownload');
  if (has) { dl.href = `/api/prepare/file?kind=svg&key=${r.key}&download=1`; dl.removeAttribute('aria-disabled'); }
  else { dl.removeAttribute('href'); dl.setAttribute('aria-disabled', 'true'); }
  $('prepSend').textContent = P.replace ? 'Update and open in Plot' : 'Send to Plot';
  $('prepReplaceNote').hidden = !P.replace;
  $('prepReplaceNote').textContent = P.replace ? `Updates ${P.replaceName || 'the drawing'} in the library and keeps its place on the bed. Save as new keeps the old one.` : '';
  const f = $('prepFillForm');
  if (document.activeElement !== f.fill) f.fill.value = P.settings.fill || 0;
  if (document.activeElement !== f.fill_angle) f.fill_angle.value = P.settings.fill_angle ?? 45;
  for (const b of $('prepModes').querySelectorAll('button')) {
    const m = b.dataset.mode;
    b.disabled = (m === 'mask' && !(r && r.mask)) || (m !== 'result' && !sideUrl(m === 'mask' ? 'mask' : 'source'));
  }
  if (mode !== 'result' && $('prepModes').querySelector(`[data-mode="${mode}"]`).disabled) setMode('result');
  else setMode(mode);
  if (!r) {
    $('prepSize').textContent = '-';
    $('prepMethod').textContent = '';
    $('prepKv').replaceChildren();
    $('prepFit').hidden = true;
    $('prepProblems').hidden = true;
    renderLayers();
    return;
  }
  const s = r.summary, b = s.bbox_machine_mm;
  const w = b[2] - b[0], h = b[3] - b[1];
  $('prepSize').textContent = `${w.toFixed(1)} x ${h.toFixed(1)} mm`;
  $('prepMethod').textContent = byId[r.method] ? byId[r.method].title : '';
  const rows = [
    ['Strokes', s.strokes.toLocaleString()],
    ['Points', s.points.toLocaleString()],
    ['Pen down', `${(s.drawing_mm / 1000).toFixed(2)} m`],
    ['Pen up', `${(s.travel_mm / 1000).toFixed(2)} m, ${s.lifts.toLocaleString()} lifts`],
    ['Time', `about ${fmtTime(s.est_seconds)}`],
    ['Ink layers', s.layers.length > 1 ? `${s.layers.length} (${s.layers.length - 1} pen change${s.layers.length > 2 ? 's' : ''})` : '1'],
  ];
  $('prepKv').replaceChildren(...rows.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })]));
  const fit = $('prepFit');
  fit.hidden = r.fit >= 0.999;
  fit.className = 'note warn';
  if (r.fit < 0.999) {
    const c = state.me.machine.canvas;
    fit.textContent = `Larger than the canvas (${(c[2] - c[0]).toFixed(0)} x ${(c[3] - c[1]).toFixed(0)} mm). ` +
      `Plot shrinks it to ${Math.floor(r.fit * 100)} % to fit, or set a smaller width in the method.`;
  }
  const pr = $('prepProblems');
  pr.hidden = !r.problems.length;
  pr.replaceChildren(...r.problems.map((p) => el('li', { text: p })));
  renderLayers();
}

function renderLayers() {
  const box = $('prepLayers');
  const r = P.result;
  if (!r || !state.me) { box.replaceChildren(el('p', { class: 'hint', text: 'Pens appear once there is a result.' })); return; }
  const pens = state.me.pens || {};
  $('prepPenHint').hidden = r.layers.length < 2;
  box.replaceChildren(...r.layers.map((ly) => {
    const set = P.settings.layers.find((x) => x.key === ly.key) || {};
    const pen = pens[set.pen];
    const sw = el('span', { class: 'swatch' });
    sw.style.background = (pen && pen.colour_hex) || ly.color || '#111';
    const sel = el('select', { 'aria-label': `Pen for ${ly.name}`, onchange: () => {
      const rest = P.settings.layers.filter((x) => x.key !== ly.key);
      P.settings.layers = [...rest, { key: ly.key, pen: sel.value || null }];
      save(); renderLayers(); redraw();
    } }, el('option', { value: '', text: 'Choose in Plot' }),
    Object.entries(pens).map(([n, p]) => el('option', { value: n, text: p.colour ? `${n} (${p.colour})` : n })));
    sel.value = set.pen || '';
    return el('div', { class: 'pen-row' }, sw, el('div', { class: 'pen-name' }, el('b', { text: ly.name }), el('small', { text: `${ly.strokes} strokes` })), sel);
  }));
}

function onFill() {
  const f = $('prepFillForm');
  const fill = Number(f.fill.value) || 0, angle = f.fill_angle.value === '' ? 45 : Number(f.fill_angle.value);
  if (fill && (fill < 0.2 || fill > 50)) { toast('Fill spacing must be 0 (off) or between 0.2 and 50 mm.', true); f.fill.value = P.settings.fill; return; }
  if (angle < 0 || angle > 180) { toast('Fill angle must be between 0 and 180 degrees.', true); f.fill_angle.value = P.settings.fill_angle; return; }
  P.settings.fill = fill; P.settings.fill_angle = angle;
  save(); schedule(0);
}

let sending = false;
async function send(open, asNew = false) {
  if (sending || !P.result) return;
  sending = true;
  $('prepSend').disabled = $('prepSaveNew').disabled = true;
  try {
    const keys = new Set(P.result.layers.map((l) => l.key));
    const r = await post('/api/prepare/save', { recipe: recipe(), name: P.name, replace: asNew ? null : P.replace,
      settings: { ...P.settings, layers: P.settings.layers.filter((l) => keys.has(l.key)) } });
    P.replace = r.file.id; P.replaceName = r.file.name.replace(/\.svg$/, '');
    save();
    await refreshFiles();
    if (open) {
      if (jobActive()) {
        toast(`${r.file.name} saved. A job is running: add it to the bed from the Library when it ends.`);
      } else {
        showWs('plot');
        await openFile(r.file.id);
        toast(`${r.file.name} is on the bed.`);
      }
    } else toast(`${r.file.name} saved to the library.`);
  } catch (e) { toast(e.message, true); }
  finally { sending = false; renderResult(); }
}

// ------------------------------------------------------------------ preview canvas
function setMode(m) {
  mode = m;
  $('prepStage').dataset.mode = m;
  for (const b of $('prepModes').querySelectorAll('button')) b.setAttribute('aria-pressed', String(b.dataset.mode === m));
  $('prepFadeBox').hidden = m !== 'overlay';
  const side = m === 'compare' || m === 'mask';
  if (side) {
    const url = sideUrl(m === 'mask' ? 'mask' : 'source');
    const img = $('prepSideImg');
    if (url && img.getAttribute('src') !== url) img.src = url;
    const s = P.source;
    $('prepSideCap').textContent = m === 'mask' ? 'Dark = counted as ink at this cutoff'
      : P.result && P.result.image ? 'Source after the adjustments' : s ? s.name : '';
  }
  requestAnimationFrame(resize);
}

function sideUrl(which) {
  const r = P.result;
  if (which === 'mask') return r && r.mask ? `/api/prepare/file?kind=mask&key=${r.mask}` : null;
  if (r && r.image) return `/api/prepare/file?kind=png&key=${r.image}`;
  if (P.source && (P.source.kind === 'svg' || P.source.kind === 'image')) return `/api/prepare/file?kind=source&id=${P.source.id}`;
  return null;
}

function image(url) {
  let im = images.get(url);
  if (!im) {
    im = new Image();
    im.onload = redraw;
    im.src = url;
    images.set(url, im);
    if (images.size > 24) images.delete(images.keys().next().value);
  }
  return im.complete && im.naturalWidth ? im : null;
}

function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#888'; }

function resize() {
  if (!cv) return;
  const r = cv.getBoundingClientRect();
  if (!r.width) return;
  dpr = window.devicePixelRatio || 1;
  W = r.width; H = r.height;
  cv.width = Math.max(1, Math.round(W * dpr)); cv.height = Math.max(1, Math.round(H * dpr));
  if (!view.user) prepFit(); else redraw();
}

function bounds() {
  const m = state.me.machine, [px, py, pw, ph] = m.paper;
  let b = [px, py, px + pw, py + ph];
  const r = P.result;
  if (r) {
    const d = r.summary.bbox_machine_mm;
    b = [Math.min(b[0], d[0]), Math.min(b[1], d[1]), Math.max(b[2], d[2]), Math.max(b[3], d[3])];
  }
  return b;
}

export function prepFit() {
  if (!state.me || !W) return;
  const b = bounds(), pad = 28;
  view.s = Math.min((W - 2 * pad) / (b[2] - b[0]), (H - 2 * pad) / (b[3] - b[1]));
  view.ox = (W - (b[2] - b[0]) * view.s) / 2 - b[0] * view.s;
  view.oy = (H - (b[3] - b[1]) * view.s) / 2 - b[1] * view.s;
  view.user = false;
  redraw();
}
function fitSoon() { requestAnimationFrame(prepFit); }

export function prepZoom(f) { zoomBy(f); }
function zoomBy(f, cx = W / 2, cy = H / 2) {
  const s2 = Math.max(0.3, Math.min(120, view.s * f));
  view.ox = cx - (cx - view.ox) * (s2 / view.s);
  view.oy = cy - (cy - view.oy) * (s2 / view.s);
  view.s = s2; view.user = true;
  redraw();
}

function redraw() { if (!raf) raf = requestAnimationFrame(() => { raf = 0; render(); }); }

function render() {
  if (!ctx || !state.me || !W) return;
  const m = state.me.machine;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  ctx.setTransform(dpr * view.s, 0, 0, dpr * view.s, dpr * view.ox, dpr * view.oy);
  const px = 1 / view.s;
  const [x0, y0, pw, ph] = m.paper;
  // the sheet, with a soft shadow, and the dashed canvas the drawing must stay inside
  ctx.save();
  ctx.shadowColor = 'rgba(0,0,0,.18)'; ctx.shadowBlur = 14; ctx.shadowOffsetY = 3;
  ctx.fillStyle = css('--paper');
  ctx.fillRect(x0, y0, pw, ph);
  ctx.restore();
  ctx.strokeStyle = css('--line'); ctx.lineWidth = px;
  ctx.strokeRect(x0, y0, pw, ph);
  const c = m.canvas;
  ctx.setLineDash([4 * px, 3 * px]);
  ctx.strokeStyle = css('--canvas-line');
  ctx.strokeRect(c[0], c[1], c[2] - c[0], c[3] - c[1]);
  ctx.setLineDash([]);
  const r = P.result;
  if (r) {
    const pl = r.placement;
    if (mode === 'overlay') {
      const im = image(sideUrl('source') || '');
      if (im) {
        // On white, like the photo itself, so it reads on a dark-mode sheet too.
        ctx.globalAlpha = Number($('prepFade').value) / 100;
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(pl.origin[0], pl.origin[1], pl.size[0], pl.size[1]);
        ctx.drawImage(im, pl.origin[0], pl.origin[1], pl.size[0], pl.size[1]);
        ctx.globalAlpha = 1;
      }
    }
    drawInk(ctx, r, { px, layers: P.settings.layers, pens: state.me.pens || {}, css, alpha: P.stale || P.error ? 0.45 : 1 });
    const b = r.summary.bbox_machine_mm;
    if (r.fit < 0.999) {
      ctx.strokeStyle = css('--warn'); ctx.lineWidth = 1.5 * px;
      ctx.setLineDash([6 * px, 4 * px]);
      ctx.strokeRect(b[0], b[1], b[2] - b[0], b[3] - b[1]);
      ctx.setLineDash([]);
    }
    ctx.fillStyle = css('--muted');
    ctx.font = `${11 * px}px system-ui, sans-serif`;
    ctx.fillText(`${(b[2] - b[0]).toFixed(1)} x ${(b[3] - b[1]).toFixed(1)} mm`, b[0], b[1] - 5 * px);
  }
}

// pan with a drag; coordinates under the pointer in paper mm
let pan = null;
function local(e) { const b = cv.getBoundingClientRect(); return [e.clientX - b.left, e.clientY - b.top]; }
function panStart(e) { cv.setPointerCapture(e.pointerId); const [x, y] = local(e); pan = { x, y, ox: view.ox, oy: view.oy }; cv.classList.add('dragging'); }
function panMove(e) {
  const [x, y] = local(e);
  if (state.me) {
    const [px0, py0] = state.me.machine.paper;
    $('prepCoords').textContent = `${((x - view.ox) / view.s - px0).toFixed(1)}, ${((y - view.oy) / view.s - py0).toFixed(1)} mm on the sheet`;
  }
  if (!pan) return;
  view.ox = pan.ox + x - pan.x; view.oy = pan.oy + y - pan.y; view.user = true;
  redraw();
}
function panEnd() { pan = null; cv.classList.remove('dragging'); }

// ------------------------------------------------------------------ drop anywhere, job indicator, keys
function initDrop() {
  const ov = $('dropOverlay');
  let depth = 0;
  const files = (e) => e.dataTransfer && [...e.dataTransfer.types].includes('Files');
  window.addEventListener('dragenter', (e) => { if (state.ws !== 'prepare' || !files(e)) return; depth++; ov.hidden = false; });
  window.addEventListener('dragleave', () => { if (depth && --depth === 0) ov.hidden = true; });
  window.addEventListener('dragover', (e) => { if (state.ws === 'prepare' && files(e)) e.preventDefault(); });
  window.addEventListener('drop', (e) => {
    if (state.ws !== 'prepare' || !files(e)) return;
    e.preventDefault(); depth = 0; ov.hidden = true;
    if (e.dataTransfer.files[0]) prepareFile(e.dataTransfer.files[0]);
  });
}

function jobDot() {
  const j = state.job, dot = $('wsJobDot');
  dot.hidden = !(jobActive() && state.ws === 'prepare');
  dot.classList.toggle('attn', !!(j && j.state === 'paused'));
}

export function prepKey(e) {
  if (e.target && e.target.id === 'turtleCode' && (e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    e.preventDefault(); turtleDraw(); return true;
  }
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); send(true); return true; }
  return false;
}
