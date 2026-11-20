// The Machine tab: manual control of the carriage (a "machine" job running tools/jog.py), calibration of steps/mm,
// the paper on the bed, and the pen library. Moving the carriage is a job like any other: one at a time, Stop
// always works, and the position is counted from S because the firmware cannot report it.
import { api, post } from './api.js';
import { state, set, on, jobActive, prefs } from './store.js';
import { $, el, toast, ask, askText } from './ui.js';
import { confirmDialog } from './run.js';
import { setBedClick } from './bed.js';
import { selectionAnchor } from './workspace.js';

const STEPS = [0.1, 1, 10, 50];
let step = prefs('jogStep') || 10;
let armed = false;

export const machineRunning = () => !!(state.job && state.job.kind === 'machine' && state.job.state === 'running' &&
  state.job.machine && state.job.machine.ready);

function showHw(h) {
  if (!h) return;
  if (h.port === 'simulator') { $('hwInfo').textContent = 'Simulator: no plotter attached.'; $('hwInfo').className = 'hint'; return; }
  const fw = h.firmware ? `Firmware ${h.firmware}` : 'Firmware not reported';
  $('hwInfo').textContent = h.ok ? `${fw} on ${h.port || 'USB'}, checked at ${h.at}.` : `Not answering (${h.at}): ${h.message}`;
  $('hwInfo').className = h.ok ? 'hint' : 'note err';
}

// ------------------------------------------------------------------ port, positions, macros, raw, lifetime
async function loadPorts() {
  try {
    const r = await api('/api/plotter/ports');
    const sel = $('hwPort');
    sel.replaceChildren(el('option', { value: '', text: 'Find it by its USB id (automatic)' }),
      ...r.ports.map((p) => el('option', { value: p.device, text: `${p.device}${p.id ? ` (${p.id})` : ''}${p.description ? `, ${p.description}` : ''}` })));
    if (r.chosen && !r.ports.some((p) => p.device === r.chosen)) sel.append(el('option', { value: r.chosen, text: `${r.chosen} (not connected now)` }));
    sel.value = r.chosen || '';
  } catch (e) { /* not available */ }
}

let positions = [], macros = [];
async function loadLists() {
  try { positions = (await api('/api/positions')).positions; macros = (await api('/api/macros')).macros; } catch (e) { return; }
  renderLists();
}

function renderLists() {
  const run = machineRunning();
  $('posList').replaceChildren(...(positions.length ? positions.map((p) => el('li', {},
    el('span', { text: `${p.name}: ${p.x}, ${p.y} mm` }),
    el('button', { type: 'button', class: 'icon', text: 'Go', disabled: !run, onclick: () => send('goto', { x: p.x, y: p.y }) }),
    el('button', { type: 'button', class: 'icon', text: 'Delete', onclick: async () => {
      positions = (await post('/api/positions/delete', { name: p.name })).positions; renderLists();
    } }))) : [el('li', { class: 'muted', text: 'None saved yet.' })]));
  $('macroList').replaceChildren(...(macros.length ? macros.map((m) => el('li', {},
    el('span', { text: m.name }),
    el('button', { type: 'button', class: 'icon', text: 'Run', disabled: !run, onclick: () => runMacro(m) }),
    el('button', { type: 'button', class: 'icon', text: 'Edit', onclick: () => { $('macroName').value = m.name; $('macroText').value = m.text; } }),
    el('button', { type: 'button', class: 'icon', text: 'Delete', onclick: async () => {
      macros = (await post('/api/macros/delete', { name: m.name })).macros; renderLists();
    } }))) : [el('li', { class: 'muted', text: 'None yet.' })]));
  const job = state.job, m = (job && job.machine) || {};
  $('rawOut').textContent = (m.raw || []).join('\n');
}

// A macro: one command per line. GOTO x y, MOVE dx dy, PEN UP|DOWN, HOME, TICK dx dy, SPEED v, WAIT ms.
async function runMacro(m) {
  const lines = String(m.text).split('\n').map((l) => l.trim()).filter((l) => l && !l.startsWith('#'));
  for (const [n, line] of lines.entries()) {
    const [w, ...a] = line.split(/\s+/);
    const v = a.map(Number);
    const W = w.toUpperCase();
    let ok = true;
    if (W === 'GOTO' && v.length === 2) await send('goto', { x: v[0], y: v[1] });
    else if (W === 'MOVE' && v.length === 2) await send('move', { dx: v[0], dy: v[1] });
    else if (W === 'TICK' && v.length === 2) await send('tick', { dx: v[0], dy: v[1] });
    else if (W === 'PEN' && /^(UP|DOWN)$/i.test(a[0] || '')) await send('pen', { pen: a[0].toLowerCase() });
    else if (W === 'HOME') await send('home');
    else if (W === 'SPEED' && v.length === 1) await send('speed', { speed: v[0] });
    else if (W === 'WAIT' && v.length === 1 && v[0] >= 0 && v[0] <= 60000) await new Promise((r) => setTimeout(r, v[0]));
    else ok = false;
    if (!ok) { toast(`Macro ${m.name}, line ${n + 1}: not understood (${line}).`, true); return; }
  }
  toast(`Macro ${m.name} sent.`);
}

async function loadLifetime() {
  try {
    const t = await api('/api/stats');
    const pens = Object.entries(t.pen_mm || {}).sort((a, b) => b[1] - a[1]).map(([p, mm]) => `${p} ${(mm / 1000).toFixed(1)} m`).join(', ');
    $('lifeInfo').textContent = `${t.jobs || 0} real jobs${t.since ? ` since ${t.since}` : ''}, ${((t.seconds || 0) / 3600).toFixed(1)} h plotting, ` +
      `${(t.strokes || 0).toLocaleString()} strokes.` + (pens ? ` Line drawn per pen: ${pens}.` : '');
  } catch (e) { /* not available */ }
}

export function initMachine() {
  on('me', () => showHw(state.me && state.me.plotter));
  loadPorts(); loadLists(); loadLifetime();
  on('job', () => { renderLists(); const j = state.job; if (j && !jobActive()) loadLifetime(); });
  $('hwPortSet').onclick = async () => {
    try { await post('/api/plotter/port', { port: $('hwPort').value || null }); toast('Port saved.'); loadPorts(); } catch (e) { toast(e.message, true); }
  };
  $('posSave').onclick = async () => {
    const pos = state.pos || (state.job && state.job.pos);
    if (!pos) { toast('Take control first: the position comes from the carriage.', true); return; }
    const name = await askText('Save this position', `The carriage is at ${pos.x.toFixed(1)}, ${pos.y.toFixed(1)} mm. A name:`, '', 'Save');
    if (!name) return;
    try { positions = (await post('/api/positions/save', { name, x: pos.x, y: pos.y })).positions; renderLists(); } catch (e) { toast(e.message, true); }
  };
  $('macroSave').onclick = async () => {
    try { macros = (await post('/api/macros/save', { name: $('macroName').value, text: $('macroText').value })).macros; renderLists(); toast('Macro saved.'); }
    catch (e) { toast(e.message, true); }
  };
  $('rawForm').onsubmit = (e) => {
    e.preventDefault();
    const t = $('rawText').value.trim();
    if (t) send('raw', { text: t });
  };
  $('mcToSel').onclick = () => {
    const p = selectionAnchor();
    if (!p) { toast('Select an object on the bed first.', true); return; }
    send('goto', { x: p[0], y: p[1] });
  };
  $('hwDoctor').onclick = async () => {
    $('hwDoctor').disabled = true;
    try {
      const r = await api('/api/plotter/doctor');
      const items = [...r.advice.map((a) => el('li', { text: a })),
        el('li', { class: 'muted', text: `Server user ${r.user || '?'}${r.groups.length ? ` in ${r.groups.join(', ')}` : ''}.` }),
        el('li', { class: 'muted', text: r.ports.length ? 'Serial ports: ' + r.ports.map((p) => `${p.device} (${p.id || 'no USB id'}${p.ebb ? ', the plotter' : ''})`).join('; ') : 'No serial ports.' })];
      if (r.answer) items.push(el('li', { class: 'muted', text: `Board answered: ${r.answer}` }));
      $('hwReport').replaceChildren(...items);
      $('hwReport').hidden = false;
      if (state.me) set({ me: { ...state.me, plotter: (await api('/api/me')).plotter } });
    } catch (e) { toast(e.message, true); } finally { $('hwDoctor').disabled = false; }
  };
  $('bkRestore').onclick = () => $('bkFile').click();
  $('bkFile').onchange = async () => {
    const f = $('bkFile').files[0];
    $('bkFile').value = '';
    if (!f) return;
    let b;
    try { b = JSON.parse(await f.text()); } catch (e) { toast('That file is not JSON.', true); return; }
    if (!b || b.kind !== 'pen-plotter-settings') { toast('That is not a settings backup from this plotter.', true); return; }
    const m = b.machine || {}, cur = state.me && state.me.machine;
    const parts = [];
    if (m.steps_per_mm) parts.push(`machine: ${m.steps_per_mm.x} / ${m.steps_per_mm.y} steps/mm, servo up ${m.servo && m.servo.pen_up}, down ${m.servo && m.servo.pen_down}` +
      (cur && cur.steps_per_mm ? ` (now ${cur.steps_per_mm.x} / ${cur.steps_per_mm.y})` : ''));
    parts.push(`${Object.keys(b.pens || {}).length} pens`, `${Object.keys(b.projects || {}).length} projects`);
    if (!(await ask('Restore settings', `From ${b.saved || 'an unknown date'}: ${parts.join('; ')}. The current files are kept as backups. ` +
      'Wrong servo values can strain the pen lift: check the pen height afterwards.', 'Restore'))) return;
    try {
      const r = await post('/api/restore', { bundle: b, parts: ['machine', 'pens', 'pen_map', 'projects'] });
      set({ me: r.me });
      toast(`Restored: ${r.restored.join(', ')}.`);
    } catch (e) { toast(e.message, true); }
  };
  $('hwCheck').onclick = async () => {
    $('hwCheck').disabled = true;
    try { showHw(await api('/api/plotter')); } catch (e) { toast(e.message, true); } finally { $('hwCheck').disabled = false; }
  };
  $('mcTake').onclick = () => take(false);
  $('mcSim').onclick = () => take(true);
  $('mcSteps').replaceChildren(...STEPS.map((s) => el('button', { type: 'button', text: `${s} mm`, 'aria-pressed': String(s === step),
    onclick: () => { step = s; prefs('jogStep', s); renderControl(); } })));
  for (const b of document.querySelectorAll('#mcPad [data-jog]')) {
    b.onclick = () => { const [dx, dy] = b.dataset.jog.split(',').map(Number); jog(dx, dy); };
  }
  $('mcHome').onclick = () => send('home');
  $('mcPenUp').onclick = () => send('pen', { pen: 'up' });
  $('mcPenDown').onclick = () => send('pen', { pen: 'down' });
  $('mcTouch').onclick = () => {
    const p = Number($('mcTouchP').value);
    if (!(p >= 0 && p <= 0.9)) { toast('Pressure: 0 (heaviest) to 0.9.', true); return; }
    send('touch', { pressure: p, ms: 800 });
  };
  $('mcGoForm').onsubmit = (e) => {
    e.preventDefault();
    const x = Number($('mcGoX').value), y = Number($('mcGoY').value);
    if ($('mcGoX').value === '' || $('mcGoY').value === '' || !Number.isFinite(x) || !Number.isFinite(y)) {
      toast('Type X and Y in mm.', true); return;
    }
    send('goto', { x, y });
  };
  $('mcClick').onclick = () => { armed = !armed; renderControl(); };
  $('mcSpeed').onchange = () => {
    const v = Number($('mcSpeed').value);
    if (!(v >= 1 && v <= 200)) { toast('Jog speed: 1 to 200 mm/s.', true); return; }
    send('speed', { speed: v });
  };
  $('mcSetS').onclick = async () => {
    if (await ask('Set S here', 'Is the pen tip exactly over the paper\'s top-left corner now? The carriage position becomes S, ' +
      'and the next plot starts from here.', 'Set S here')) send('sets');
  };
  $('mcFinishS').onclick = () => send('quit');
  $('mcFinishHere').onclick = () => send('quit', { here: true });
  // calibration
  $('calX').onclick = () => calTicks('x');
  $('calY').onclick = () => calTicks('y');
  $('calSaveX').onclick = () => calSave('x');
  $('calSaveY').onclick = () => calSave('y');
  $('paperForm').onsubmit = savePaper;
  $('penNew').onclick = () => editPen(null);
  setBedClick((x, y) => {
    if (!armed || !machineRunning()) return false;
    armed = false;
    send('goto', { x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10 });
    renderControl();
    return true;
  }, jog);
  on(['job', 'position', 'pos'], renderControl);
  on(['job', 'me'], renderCal);
  on(['me', 'job'], renderPaper);
  on(['me', 'job'], renderPens);
}

// ------------------------------------------------------------------ control
async function take(sim) {
  if (!sim) {
    const m = state.me.machine;
    const ok = await confirmDialog('Take control of the carriage', [['Start', `S, the paper's top-left (${m.start[0]}, ${m.start[1]}) mm`]], [
      'The carriage is parked by hand at S with the motors off. It was slid there with the motors off.',
      'The pen is up.',
      'Nothing is in the carriage\'s way, and the power switch is within reach.',
    ], 'Take control');
    if (!ok) return;
  }
  try {
    await post('/api/machine/start', { mode: sim ? 'simulate' : 'plot', confirm: sim ? undefined : { parked: true } });
  } catch (e) { toast(e.message, true); }
}

async function send(cmd, args = {}) {
  if (!machineRunning()) { toast('Take control first.', true); return; }
  try { await post('/api/machine/send', { job: state.job.id, cmd, args }); } catch (e) { toast(e.message, true); }
}

// Arrow keys on the bed and the pad buttons: one step in a direction.
export function jog(dx, dy) {
  if (!machineRunning()) return false;
  send('move', { dx: dx * step, dy: dy * step });
  return true;
}

function renderControl() {
  const run = machineRunning();
  const job = state.job;
  const other = jobActive() && !(job && job.kind === 'machine');
  $('mcIdle').hidden = run;
  $('mcLive').hidden = !run;
  $('mcTake').disabled = $('mcSim').disabled = jobActive();
  $('mcTakeNote').textContent = other ? 'A job is running: take control when it has ended.' : '';
  const p = state.position;
  const pill = $('mcPos');
  const pos = state.pos || (job && jobActive() ? job.pos : null);
  if (run && pos) {
    pill.className = 'pill busy';
    pill.textContent = `Carriage ${pos.x.toFixed(1)}, ${pos.y.toFixed(1)} mm, pen ${pos.pen || 'up'}`;
  } else {
    pill.className = 'pill ' + (p && p.state === 'at_start' ? 'ok' : p && p.state === 'unknown' ? 'err' : 'warn');
    pill.textContent = !p ? 'Position unknown' : p.state === 'at_start' ? 'At S' : p.state === 'unknown' ? 'Position unknown'
      : `Away from S${p.at ? ` (${p.at[0].toFixed(0)}, ${p.at[1].toFixed(0)} mm)` : ''}`;
  }
  if (!run) return;
  for (const b of $('mcSteps').children) b.setAttribute('aria-pressed', String(b.textContent === `${step} mm`));
  $('mcClick').setAttribute('aria-pressed', String(armed));
  $('mcClick').textContent = armed ? 'Click on the bed...' : 'Click to go';
  const m = job.machine || {};
  if (document.activeElement !== $('mcSpeed') && m.speed) $('mcSpeed').value = m.speed;
  const down = pos && pos.pen === 'down';
  $('mcSetS').disabled = !!down;
  $('mcPenUp').classList.toggle('primary', !!down);
}

// ------------------------------------------------------------------ calibration
async function calTicks(axis) {
  const L = Number($('calLen').value);
  if (!(L >= 20 && L <= 300)) { toast('Test length: 20 to 300 mm.', true); return; }
  if (!machineRunning()) { toast('Take control first (Control, above), with paper under the pen.', true); return; }
  const tick = axis === 'x' ? { dx: 0, dy: 4 } : { dx: 4, dy: 0 };
  const move = axis === 'x' ? { dx: L, dy: 0 } : { dx: 0, dy: L };
  for (const [cmd, args] of [['tick', tick], ['move', move], ['tick', tick], ['move', { dx: -move.dx, dy: -move.dy }]]) {
    await send(cmd, args);
  }
  toast(`Two ticks ${L} mm apart along ${axis.toUpperCase()}. Measure between them, then type the result.`);
}

async function calSave(axis) {
  const commanded = Number($('calLen').value), measured = Number($(`calMeas${axis.toUpperCase()}`).value);
  if (!(measured > 0)) { toast('Type the measured distance in mm.', true); return; }
  const cur = state.me.machine.steps_per_mm || {};
  const ok = await ask(`Save ${axis.toUpperCase()} calibration`,
    `${commanded} mm commanded measured ${measured} mm: steps/mm on ${axis.toUpperCase()} changes from ${cur[axis]} to about ` +
    `${(cur[axis] * commanded / measured).toFixed(3)}. Every drawing is checked again with the new value. The old file is kept as a backup.`, 'Save');
  if (!ok) return;
  try {
    const r = await post('/api/machine/calibrate', { axis, commanded, measured });
    set({ me: r.me });
    toast(`${axis.toUpperCase()} steps/mm: ${r.old} -> ${r.new}.`);
    $(`calMeas${axis.toUpperCase()}`).value = '';
  } catch (e) { toast(e.message, true); }
}

function renderCal() {
  const m = state.me && state.me.machine;
  if (!m) return;
  const sp = m.steps_per_mm || {};
  $('calNow').textContent = `Now: X ${sp.x} steps/mm, Y ${sp.y} steps/mm` + (m.calibrated ? `, measured ${m.calibrated}` : '') + '.';
  const busy = jobActive();
  $('calSaveX').disabled = $('calSaveY').disabled = busy;
  $('calX').disabled = $('calY').disabled = !machineRunning();
}

// ------------------------------------------------------------------ paper
function renderPaper() {
  const m = state.me && state.me.machine;
  if (!m) return;
  const f = $('paperForm');
  const [x0, y0, w, h] = m.paper;
  for (const [k, v] of [['x0', x0], ['y0', y0], ['width', w], ['height', h], ['margin_mm', m.margin]]) {
    if (document.activeElement !== f[k]) f[k].value = v;
  }
  f.querySelector('button').disabled = jobActive();
  const sv = m.servo || {};
  $('servoNote').textContent = `Pen servo: up ${sv.pen_up}, down ${sv.pen_down}. These are set by hand with a pen mounted ` +
    '(Tools, Pen height test, and config/machine.json), never here: a wrong pen-down value can gouge the paper.';
}

async function savePaper(e) {
  e.preventDefault();
  const f = $('paperForm');
  const paper = Object.fromEntries(['x0', 'y0', 'width', 'height', 'margin_mm'].map((k) => [k, Number(f[k].value)]));
  try {
    const r = await post('/api/machine/paper', { paper });
    set({ me: r.me });
    toast('Paper saved. Drawings are checked again against the new canvas.');
  } catch (err) { toast(err.message, true); }
}

// ------------------------------------------------------------------ pens
function renderPens() {
  const pens = (state.me && state.me.pens) || {};
  const list = $('penList');
  if (list.contains(document.activeElement)) return;
  list.replaceChildren(...Object.entries(pens).map(([name, p]) => {
    const sw = el('span', { class: 'swatch' });
    sw.style.background = p.colour_hex || 'transparent';
    const facts = [p.colour, p.width_mm ? `${p.width_mm} mm` : 'width not set', p.speed ? `${p.speed} mm/s` : null,
      p.pressure !== undefined && p.pressure !== null ? `pressure ${p.pressure}` : null].filter(Boolean).join(', ');
    return el('li', {}, el('div', { class: 'pen-head' }, sw, el('b', { text: name }), el('span', { class: 'spacer' }),
      el('button', { class: 'icon', text: 'Edit', type: 'button', onclick: () => editPen(name) })),
    el('div', { class: 'meta', text: facts }),
    p.description ? el('div', { class: 'meta', text: p.description }) : null);
  }));
}

function editPen(name) {
  const p = name ? state.me.pens[name] : {};
  const box = $('penEdit');
  const field = (label, input) => el('label', { class: 'field' }, el('span', { text: label }), input);
  const inp = (k, attrs = {}) => { const i = el('input', { name: k, ...attrs }); i.value = p[k] ?? ''; return i; };
  const hex = el('input', { type: 'color', name: 'colour_hex' });
  hex.value = p.colour_hex || '#1a1a1a';
  const noHex = el('input', { type: 'checkbox', name: 'no_hex' });
  noHex.checked = !p.colour_hex;
  const nameIn = inp('pen_name', { maxlength: 30, pattern: '[a-z0-9][a-z0-9_-]*', required: true });
  nameIn.value = name || '';
  const f = el('form', { class: 'pen-form' },
    field('Name (lowercase)', nameIn),
    field('Description', inp('description', { maxlength: 400 })),
    el('div', { class: 'field-row' },
      field('Colour name', inp('colour', { maxlength: 40 })),
      el('div', { class: 'field' }, el('span', { text: 'Ink colour' }), el('div', { class: 'row' }, hex,
        el('label', { class: 'check inline' }, noHex, ' none')))),
    el('div', { class: 'field-row' },
      field('Line width (mm)', inp('width_mm', { type: 'number', min: 0.05, max: 10, step: 0.05 })),
      field('Speed (mm/s)', inp('speed', { type: 'number', min: 1, max: state.me.limits.speed.max, step: 1 })),
      field('Pressure', inp('pressure', { type: 'number', min: 0, max: state.me.limits.pressure.max, step: 0.05 }))),
    field('Notes', inp('notes', { maxlength: 400 })),
    el('div', { class: 'row' }, el('button', { class: 'primary', type: 'submit', text: name ? 'Save' : 'Add pen' }),
      el('button', { type: 'button', text: 'Cancel', onclick: () => box.replaceChildren() }),
      el('span', { class: 'spacer' }),
      name ? el('button', { type: 'button', class: 'danger-text', text: 'Delete', onclick: async () => {
        if (!(await ask('Delete pen', `Delete the pen profile ${name} and its recorded test results? Layers that use it lose their pen.`, 'Delete'))) return;
        try { const r = await post('/api/pens/delete', { name }); set({ me: r.me }); box.replaceChildren(); } catch (e) { toast(e.message, true); }
      } }) : null));
  f.onsubmit = async (e) => {
    e.preventDefault();
    const num = (k) => (f[k].value === '' ? null : Number(f[k].value));
    const pen = { description: f.description.value, colour: f.colour.value, colour_hex: f.no_hex.checked ? null : f.colour_hex.value,
      width_mm: num('width_mm'), speed: num('speed'), pressure: num('pressure'), notes: f.notes.value };
    try {
      const r = await post('/api/pens/save', { name: nameIn.value, pen, rename_from: name && name !== nameIn.value ? name : undefined });
      set({ me: r.me });
      box.replaceChildren();
      toast(`Pen ${nameIn.value} saved.`);
    } catch (err) { toast(err.message, true); }
  };
  box.replaceChildren(el('h3', { text: name ? `Edit ${name}` : 'New pen' }), f);
  nameIn.focus();
}

export async function reloadConfig() {
  try { set({ me: await api('/api/me') }); } catch (e) { /* the next event retries */ }
}
