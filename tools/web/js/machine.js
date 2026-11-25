// The Machine tab: manual control of the carriage (a "machine" job running tools/jog.py), calibration of steps/mm,
// the paper on the bed, and the pen library. Moving the carriage is a job like any other: one at a time, Stop
// always works, and the position is counted from S because the firmware cannot report it.
import { api, post } from './api.js';
import { state, set, on, jobActive, prefs } from './store.js';
import { $, el, toast, ask, askText } from './ui.js';
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
  $('mcHomeSw').onclick = () => send('homesw');
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
  $('mcfgForm').onsubmit = saveMachineSettings;
  $('paperTL').onclick = () => paperCorner('tl');
  $('regAdd').onclick = regAdd;
  $('regA').onclick = () => regPoint(0);
  $('regB').onclick = () => regPoint(1);
  $('regUse').onclick = regUse;
  $('regClear').onclick = async () => { const D = await import('./doc.js'); D.edit((d) => { d.settings.register = null; }); regMeasured = [null, null]; };
  on(['doc', 'files', 'job'], renderReg);
  $('ctrPt').onclick = ctrPoint;
  $('ctrReset').onclick = () => { ctrPts = []; ctrAt = null; $('ctrNote').textContent = ''; $('ctrGo').disabled = $('ctrSave').disabled = true; };
  $('ctrGo').onclick = () => { $('mcGoX').value = ctrAt.x.toFixed(1); $('mcGoY').value = ctrAt.y.toFixed(1); $('mcGoForm').requestSubmit(); };
  $('ctrSave').onclick = async () => {
    const name = await askText('Save the centre', `${ctrAt.x.toFixed(1)}, ${ctrAt.y.toFixed(1)} mm. A name:`, 'centre', 'Save');
    if (!name) return;
    try { positions = (await post('/api/positions/save', { name, x: ctrAt.x, y: ctrAt.y })).positions; renderLists(); toast('Saved: Job, Start from can put a drawing\'s centre there.'); } catch (e) { toast(e.message, true); }
  };
  $('paperBR').onclick = () => paperCorner('br');
  $('penNew').onclick = () => editPen(null);
  $('penImport').onclick = () => $('penFile').click();
  $('penFile').onchange = async () => {
    const f = $('penFile').files[0];
    $('penFile').value = '';
    if (!f) return;
    let pens;
    try { pens = JSON.parse(await f.text()); } catch (e) { toast('That file is not JSON.', true); return; }
    const n = pens && typeof pens === 'object' ? Object.keys(pens).length : 0;
    if (!(await ask('Import pens', `${n} pen profile${n === 1 ? '' : 's'}: merged in, same names replaced. The current file is kept as a backup.`, 'Import'))) return;
    try { const r = await post('/api/pens/import', { pens }); set({ me: r.me }); toast(`${r.imported} pens imported.`); } catch (e) { toast(e.message, true); }
  };
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
  on(['me', 'job', 'pot'], renderPot);
  initPot();
}

// ------------------------------------------------------------------ control
async function take(sim) {
  // No start dialog: the carriage is assumed parked at S with the motors off and the pen up.
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
  $('mcHomeSw').hidden = !(state.me && state.me.features && state.me.features.homing);
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

// ------------------------------------------------------------------ machine settings (travel, directions, limits)
function renderMachineSettings() {
  const c = state.me && state.me.machine_cfg, f = $('mcfgForm');
  if (!c || f.contains(document.activeElement)) return;
  const L = c.limits || {};
  f.travel_x.value = c.travel.x; f.travel_y.value = c.travel.y;
  f.sign_x.value = String(c.signs.x); f.sign_y.value = String(c.signs.y);
  f.speed_max.value = L.speed_max ?? c.ceilings.speed; f.travel_max.value = L.travel_max ?? c.ceilings.travel_speed;
  f.speed_default.value = L.speed_default ?? 15; f.travel_default.value = L.travel_default ?? 100;
  f.speed_max.max = f.speed_default.max = c.ceilings.speed;
  f.travel_max.max = f.travel_default.max = c.ceilings.travel_speed;
  $('mcfgCeil').textContent = `The driver's ceilings: drawing ${c.ceilings.speed} mm/s, pen up ${c.ceilings.travel_speed} mm/s. ` +
    `Steps/mm (X ${c.steps_per_mm.x}, Y ${c.steps_per_mm.y}) change only through Calibration.`;
  f.querySelector('button[type=submit]').disabled = jobActive();
}

async function saveMachineSettings(e) {
  e.preventDefault();
  const f = $('mcfgForm'), c = state.me.machine_cfg;
  const machine = Object.fromEntries(['travel_x', 'travel_y', 'speed_max', 'travel_max', 'speed_default', 'travel_default']
    .map((k) => [k, Number(f[k].value)]));
  machine.sign_x = Number(f.sign_x.value); machine.sign_y = Number(f.sign_y.value);
  const flips = ['x', 'y'].filter((a) => machine['sign_' + a] !== c.signs[a]);
  if (flips.length && !await ask('Reverse an axis?', `Reversing ${flips.join(' and ').toUpperCase()} makes every move on that axis go ` +
    'the other way. Only do this if the carriage moves opposite to the arrows. Jog a short way afterwards to check before plotting.', 'Reverse')) return;
  try {
    const r = await post('/api/machine/settings', { machine });
    set({ me: r.me });
    toast('Machine settings saved. Preview again before plotting.');
  } catch (err) { toast(err.message, true); }
}

// ------------------------------------------------------------------ registration marks and the centre finder
const REG_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="8mm" height="8mm" viewBox="0 0 8 8">' +
  '<path d="M4 0 L4 8 M0 4 L8 4" stroke="#000" fill="none"/><circle cx="4" cy="4" r="2" stroke="#000" fill="none"/></svg>';
const REG_NAME = 'registration-mark';
let regMeasured = [null, null];
function regMarks() {
  const ids = new Set((state.files || []).filter((f) => f.name.startsWith(REG_NAME)).map((f) => f.id));
  return state.doc ? state.doc.objects.filter((o) => ids.has(o.file)) : [];
}
async function regAdd() {
  if (regMarks().length >= 2) { toast('There are registration marks on the bed already.'); return; }
  try {
    let f = (state.files || []).find((x) => x.name.startsWith(REG_NAME));
    if (!f) {
      f = (await api('/api/files/upload', { method: 'POST', raw: new Blob([REG_SVG], { type: 'image/svg+xml' }),
        headers: { 'X-Filename': `${REG_NAME}.svg`, 'X-Params': '{}' } })).file;
      const { refreshFiles } = await import('./workspace.js');
      await refreshFiles();
    }
    const D = await import('./doc.js');
    const c = state.me.machine.canvas;
    await D.addFile(f.id, { at: [c[0], c[1]] });
    await D.addFile(f.id, { at: [c[2] - 8, c[3] - 8] });
    toast('Two marks at opposite corners of the canvas. Plot them with the first pass.');
  } catch (e) { toast(e.message, true); }
}
function regPoint(n) {
  const p = carriage();
  if (!p) { toast('Take control first, then jog the pen tip onto the mark.', true); return; }
  regMeasured[n] = [p.x, p.y];
  renderReg();
}
function renderReg() {
  const marks = regMarks(), r = state.doc && state.doc.settings.register;
  const ready = marks.length >= 2 && regMeasured[0] && regMeasured[1];
  $('regUse').disabled = !ready || jobActive();
  $('regClear').disabled = !r || jobActive();
  const m = (p) => (p ? `${p[0].toFixed(1)}, ${p[1].toFixed(1)}` : 'not yet');
  $('regNote').textContent = (marks.length < 2 ? 'No registration marks on the bed. ' : '') +
    `Mark 1: ${m(regMeasured[0])}. Mark 2: ${m(regMeasured[1])}.` + (r ? ' This bed is registered.' : '');
}
async function regUse() {
  const [a, b] = regMarks();
  const ctr = (o) => [o.x + o.w / 2, o.y + o.h / 2];
  const D = await import('./doc.js');
  D.edit((d) => { d.settings.register = { from: [ctr(a), ctr(b)], to: [regMeasured[0], regMeasured[1]] }; });
  toast('Registered. Preview: the whole bed now follows the marks. Remove the marks from the bed if they should not be drawn again.');
}

let ctrPts = [], ctrAt = null;
function circle3(a, b, c) {
  const d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]));
  if (Math.abs(d) < 1e-6) return null;
  const s = (p) => p[0] * p[0] + p[1] * p[1];
  const x = (s(a) * (b[1] - c[1]) + s(b) * (c[1] - a[1]) + s(c) * (a[1] - b[1])) / d;
  const y = (s(a) * (c[0] - b[0]) + s(b) * (a[0] - c[0]) + s(c) * (b[0] - a[0])) / d;
  return { x, y, r: Math.hypot(a[0] - x, a[1] - y) };
}
function ctrPoint() {
  const p = carriage();
  if (!p) { toast('Take control first, then jog the pen tip to the edge.', true); return; }
  ctrPts = [...ctrPts.slice(-2), [p.x, p.y]];
  ctrAt = ctrPts.length === 3 ? circle3(...ctrPts) : null;
  $('ctrNote').textContent = ctrPts.length < 3 ? `${ctrPts.length} of 3 points.`
    : ctrAt ? `Centre ${ctrAt.x.toFixed(1)}, ${ctrAt.y.toFixed(1)} mm, diameter ${(2 * ctrAt.r).toFixed(1)} mm.`
      : 'The three points are in a line: spread them around the edge.';
  $('ctrGo').disabled = $('ctrSave').disabled = !ctrAt;
}

// ------------------------------------------------------------------ paper by measuring: jog to two corners
let paperTL = null;
function carriage() { return state.pos || (state.job && state.job.pos) || null; }
function paperCorner(which) {
  const p = carriage();
  if (!p) { toast('Take control first, then jog the pen tip onto the corner.', true); return; }
  const f = $('paperForm');
  if (which === 'tl') {
    paperTL = [p.x, p.y];
    f.x0.value = p.x.toFixed(1); f.y0.value = p.y.toFixed(1);
    $('paperMeasureNote').textContent = `Top-left at ${p.x.toFixed(1)}, ${p.y.toFixed(1)} mm. Now jog to the bottom-right corner.`;
  } else {
    const tl = paperTL || [Number(f.x0.value), Number(f.y0.value)];
    const w = p.x - tl[0], h = p.y - tl[1];
    if (w < 20 || h < 20) { toast('The bottom-right corner must be right of and below the top-left one.', true); return; }
    f.width.value = w.toFixed(1); f.height.value = h.toFixed(1);
    $('paperMeasureNote').textContent = `Sheet ${w.toFixed(1)} x ${h.toFixed(1)} mm. Check the margin, then Save paper.`;
  }
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
  renderMachineSettings();
  const sv = m.servo || {};
  $('servoNote').textContent = `Pen servo: up ${sv.pen_up}, down ${sv.pen_down}. These are set by hand with a pen mounted ` +
    '(Tools, Pen height test, and config/machine.json), never here: a wrong pen-down value can gouge the paper.';
}

// ------------------------------------------------------------- depth knob
function initPot() {
  const f = $('potForm');
  const grab = (k) => () => {
    const raw = state.pot && state.pot.raw;
    if (raw == null) { toast('No steady reading from the knob right now.', true); return; }
    f[k].value = raw;
  };
  $('potShallow').onclick = grab('adc_shallow');
  $('potDeep').onclick = grab('adc_deep');
  f.onsubmit = async (e) => {
    e.preventDefault();
    const pot = { enabled: f.enabled.checked, channel: 1, adc_shallow: Number(f.adc_shallow.value),
      adc_deep: Number(f.adc_deep.value), depth_max: Number(f.depth_max.value) };
    try { set({ me: (await post('/api/machine/pot', { pot })).me }); toast('Knob saved.'); } catch (err) { toast(err.message, true); }
  };
  $('potKeep').onclick = async () => {
    const v = state.pot && state.pot.pen_down;
    if (v == null) { toast('The knob has no good reading right now.', true); return; }
    if (!(await ask('Keep as saved pen-down', `Store ${v}, the knob's depth now, as the saved pen-down. Plans need a new preview.`, 'Keep'))) return;
    try { set({ me: (await post('/api/machine/pot/keep', {})).me }); } catch (err) { toast(err.message, true); }
  };
}

function renderPot() {
  const mc = state.me && state.me.machine_cfg;
  if (!mc || !mc.pen_pot) return;
  const p = mc.pen_pot, f = $('potForm'), k = state.pot;
  for (const n of ['adc_shallow', 'adc_deep', 'depth_max']) if (document.activeElement !== f[n] && !f[n].dataset.touched) f[n].value = p[n];
  if (document.activeElement !== f.enabled) f.enabled.checked = !!p.enabled;
  const busy = jobActive();
  for (const b of f.querySelectorAll('button')) b.disabled = busy;
  $('potLive').textContent = state.me.sim ? 'Simulator: the knob is not read.'
    : !k ? 'Waiting for a reading (the knob is read between jobs).'
      : !p.enabled ? `Off: the saved pen-down is used. The knob reads ${k.raw ?? 'unsteady'}.`
        : k.problem ? `Not used: ${k.problem}${k.raw != null ? ` (reads ${k.raw})` : ''}. The saved pen-down applies.`
          : `Knob reads ${k.adc}: pen-down ${k.pen_down}.`;
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
      p.pressure !== undefined && p.pressure !== null ? `pressure ${p.pressure}` : null,
      p.dwell_ms ? `dwell ${p.dwell_ms} ms` : null].filter(Boolean).join(', ');
    return el('li', {}, el('div', { class: 'pen-head' }, sw, el('b', { text: name }), el('span', { class: 'spacer' }),
      el('button', { class: 'icon', text: 'Edit', type: 'button', onclick: () => editPen(name) }),
      el('button', { class: 'icon', text: 'Duplicate', type: 'button', onclick: () => editPen(name, true) })),
    el('div', { class: 'meta', text: facts }),
    p.description ? el('div', { class: 'meta', text: p.description }) : null);
  }));
}

function editPen(name, copy = false) {
  const p = name ? state.me.pens[name] : {};
  if (copy) name = null;              // a duplicate: the same fields under a new name, saved as a new pen
  const box = $('penEdit');
  const field = (label, input) => el('label', { class: 'field' }, el('span', { text: label }), input);
  const inp = (k, attrs = {}) => { const i = el('input', { name: k, ...attrs }); i.value = p[k] ?? ''; return i; };
  const hex = el('input', { type: 'color', name: 'colour_hex' });
  hex.value = p.colour_hex || '#1a1a1a';
  const noHex = el('input', { type: 'checkbox', name: 'no_hex' });
  noHex.checked = !p.colour_hex;
  const nameIn = inp('pen_name', { maxlength: 30, pattern: '[a-z0-9][a-z0-9_-]*', required: true });
  nameIn.value = name || (copy ? `${Object.keys(state.me.pens).find((k) => state.me.pens[k] === p) || 'pen'}-2` : '');
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
    el('div', { class: 'field-row' },
      field('Dwell (ms)', inp('dwell_ms', { type: 'number', min: 0, max: 3000, step: 50,
        title: 'Wait with the pen down at each stroke start and end so the ink flows: dip pens, brushes, some gel pens. 0 for a ballpoint.' }))),
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
      width_mm: num('width_mm'), speed: num('speed'), pressure: num('pressure'), notes: f.notes.value,
      dwell_ms: num('dwell_ms') };
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
