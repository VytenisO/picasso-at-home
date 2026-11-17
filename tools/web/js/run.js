// Status panel, run controls, the start confirmation and the pen-swap prompt.
import { post, ApiError } from './api.js';
import { state, set, on, jobActive } from './store.js';
import { $, el, toast, fmtTime, mm } from './ui.js';
import { runPreview } from './workspace.js';

const TITLES = {
  idle: 'Idle', starting: 'Starting', running: 'Plotting', pausing: 'Pausing', paused: 'Paused',
  stopping: 'Stopping', returning: 'Returning to start', complete: 'Finished', stopped: 'Stopped',
  interrupted: 'Interrupted', error: 'Error',
};
const DOT = { running: 'busy', starting: 'busy', returning: 'busy', pausing: 'warn', paused: 'warn', stopping: 'warn',
  complete: 'ok', stopped: 'warn', interrupted: 'warn', error: 'err' };

export function initRun() {
  $('previewBtn').onclick = previewNow;
  $('simBtn').onclick = () => start('simulate');
  $('traceBtn').onclick = () => start('trace');
  $('plotBtn').onclick = () => start('plot');
  $('pauseBtn').onclick = pause;
  $('continueBtn').onclick = resume;
  $('swapContinue').onclick = resume;
  $('returnBtn').onclick = returnToStart;
  $('swapReturn').onclick = returnToStart;
  $('stopBtn').onclick = stop;
  $('stopTop').onclick = stop;
  $('swapStop').onclick = stop;
  $('ptToggle').onclick = () => pentest('toggle');
  $('ptSweep').onclick = () => pentest('sweep');
  $('ptQuit').onclick = () => pentest('quit');
  // Modal dialogs make the page behind them inert, so each carries its own Stop while a job runs.
  document.querySelectorAll('.dlg-stop').forEach((b) => { b.onclick = stop; });
  on('job', () => document.querySelectorAll('.dlg-stop').forEach((b) => { b.hidden = !jobActive(); }));
  on(['job', 'position', 'link', 'me', 'lastEventAt'], renderStatus);
  on(['job', 'preview', 'previewStale', 'previewing', 'current', 'me'], renderRun);
  setInterval(renderStale, 2000);
}

export function title(job) { return job ? TITLES[job.state] || job.state : 'Idle'; }
export function dotClass(job) { return job ? DOT[job.state] || '' : ''; }

function nextStep(job) {
  if (!job || !jobActive()) {
    if (state.position && state.position.state !== 'at_start')
      return 'Before the next run: motors off, slide the carriage by hand to S (the paper\'s top-left), pen up.';
    if (!state.current) return 'Open a drawing from the Library.';
    if (!state.preview || state.previewStale) return 'Preview to check the plan.';
    if (!state.preview.ok) return 'Fix the problems listed under Run, then preview again.';
    return 'Ready. Start plot when the carriage is parked at S with the pen up.';
  }
  switch (job.state) {
    case 'paused': return job.pause && job.pause.kind === 'swap'
      ? 'Change the pen, check it is up and seated, then press Continue.'
      : 'Press Continue to carry on, Return to start, or Stop.';
    case 'stopping': return 'Wait: the move already sent is finishing. Switch the plotter off for an instant stop.';
    case 'pausing': return 'The plotter finishes the current stroke and lifts the pen.';
    case 'returning': return 'Finishing the stroke, then travelling back to S.';
    default: return 'Watch the plot. Space pauses at the end of a stroke; Shift+S stops.';
  }
}

function renderStatus() {
  const job = state.job;
  const active = jobActive();
  $('stTitle').textContent = job ? (job.kind === 'pentest' ? 'Pen test: ' : '') + title(job) : 'Idle';
  $('stDot').className = 'state-dot ' + dotClass(job);
  $('stMsg').textContent = job ? job.message || '' : '';
  $('stNext').textContent = nextStep(job);
  $('statePill').textContent = job ? title(job) : 'Idle';
  $('statePill').className = 'pill ' + (dotClass(job) || '');
  $('stopTop').hidden = !active;
  const pr = job && job.progress;
  $('stProgress').hidden = !(pr && job.kind === 'plot');
  if (pr) {
    const pct = Math.round(pr.fraction * 100);
    $('stBar').firstElementChild.style.width = pct + '%';
    $('stBar').setAttribute('aria-valuenow', pct);
    $('stPct').textContent = `${pct}%  (${pr.done} of ${pr.strokes} strokes)`;
    const simNote = job.sim ? ' (simulated time)' : '';
    $('stTimes').textContent = `elapsed ${fmtTime(pr.elapsed_s)}, left about ${fmtTime(pr.eta_s)}${simNote}`;
  }
  const kv = $('stKv');
  const rows = [];
  if (job) {
    rows.push(['Job', `${job.kind === 'pentest' ? 'pen test' : job.mode}${job.sim ? ' (simulator)' : ''}${job.meta && job.meta.name ? ': ' + job.meta.name : ''}`]);
    const ly = job.layer;
    if (ly) rows.push(['Layer', `${ly.index + 1}: ${ly.label}${ly.pen ? ', pen ' + ly.pen : ''}`]);
    if (ly) rows.push(['Speed', `${mm(ly.speed, 0)} mm/s drawing`]);
    if (ly && ly.pressure !== null && ly.pressure !== undefined) rows.push(['Pressure', String(ly.pressure)]);
    rows.push(['Pen', job.pen === 'down' ? 'down' : 'up']);
    if (job.pos) rows.push(['Carriage', `${mm(job.pos.x)}, ${mm(job.pos.y)} mm (commanded)`]);
  }
  kv.replaceChildren(...rows.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })]));
  const p = state.position;
  const pn = $('stPos');
  pn.className = 'note ' + (p && p.state === 'at_start' ? 'ok' : p && p.state === 'unknown' ? 'err' : 'warn');
  if (active && job.kind === 'plot') {
    pn.className = 'note';
    pn.textContent = 'Carriage: under the job\'s control. The position above is the commanded one (no readback).';
  } else pn.textContent = p ? `Carriage: ${p.state === 'at_start' ? 'at S' : p.state === 'moved' ? 'away from S' : 'position unknown'}. ${p.note || ''}` : '';
  // pen swap prompt over the bed
  const swap = job && job.state === 'paused' && job.pause;
  $('swapBanner').hidden = !swap;
  if (swap) {
    const pz = job.pause;
    if (pz.kind === 'swap') {
      $('swapTitle').textContent = `Pen change: ${pz.label}`;
      $('swapText').textContent = (pz.pen ? `Put in the ${pz.pen} pen` : 'Put in the pen') +
        ` for layer ${pz.layer_index + 1} (${pz.label}). The pen is up and the motors are on: do not push the carriage. ` +
        'Change only the pen, then press Continue.';
    } else {
      $('swapTitle').textContent = 'Paused';
      $('swapText').textContent = 'The pen is up and the motors stay on. Do not push the carriage. Continue, return to S, or stop.';
    }
    $('swapContinue').disabled = !!pz.resuming;
    $('swapContinue').textContent = pz.resuming ? 'Resuming...' : 'Continue';
  }
  renderStale();
}

function renderStale() {
  const n = $('stStale');
  const age = (Date.now() - state.lastEventAt) / 1000;
  const bad = state.link !== 'live' || (state.lastEventAt && age > 35);
  n.hidden = !bad;
  if (bad) n.textContent = state.link === 'live'
    ? `No news from the server for ${Math.round(age)} s. The view may be out of date.`
    : 'Not connected to the server. Reconnecting; the plotter carries on (or stops) on its own.';
  $('linkPill').textContent = state.link === 'live' ? 'live' : state.link;
  $('linkPill').className = 'pill ' + (state.link === 'live' && !bad ? 'ok' : state.link === 'offline' ? 'err' : 'warn');
}

function renderRun() {
  const job = state.job, active = jobActive();
  const pv = state.preview, stale = state.previewStale;
  const isPentest = active && job.kind === 'pentest';
  $('idleButtons').hidden = active;
  $('jobButtons').hidden = !active || isPentest;
  $('pentestButtons').hidden = !isPentest;
  const ready = !!(pv && pv.ok && !stale && !state.previewing && state.current);
  $('previewBtn').disabled = !state.current || state.previewing;
  $('previewBtn').textContent = state.previewing ? 'Checking...' : 'Preview';
  for (const b of ['simBtn', 'traceBtn', 'plotBtn']) $(b).disabled = !ready;
  if (active && !isPentest) {
    $('pauseBtn').disabled = job.state !== 'running';
    $('continueBtn').disabled = job.state !== 'paused' || (job.pause && job.pause.resuming);
    $('returnBtn').disabled = !['running', 'pausing', 'paused'].includes(job.state);
    $('stopBtn').disabled = job.state === 'stopping';
  }
  const ps = $('previewState'), list = $('problemList'), kv = $('planKv');
  list.hidden = true;
  kv.replaceChildren();
  ps.hidden = kv.hidden = active;
  if (active) return;
  if (!state.current) { ps.className = 'note'; ps.textContent = 'Open a drawing, then preview it.'; return; }
  if (state.previewing) { ps.className = 'note'; ps.textContent = 'Checking the plan...'; }
  else if (!pv || stale) { ps.className = 'note warn'; ps.textContent = 'Settings changed. Preview to check again.'; }
  else if (!pv.ok) {
    ps.className = 'note err'; ps.textContent = 'Not ready to plot:';
    list.hidden = false;
    list.replaceChildren(...(pv.problems || []).map((p) => el('li', { text: p })));
  } else { ps.className = 'note ok'; ps.textContent = 'Plan checked. Ready to plot.'; }
  if (pv && pv.summary) {
    const s = pv.summary, b = s.bbox_machine_mm;
    const enabled = s.layers.filter((l) => l.enabled !== false);
    const rows = [
      ['Size', `${mm(b[2] - b[0])} x ${mm(b[3] - b[1])} mm at (${mm(b[0])}, ${mm(b[1])})`],
      ['Time', `about ${fmtTime(s.est_seconds)}`],
      ['Strokes', `${s.strokes}, ${mm(s.drawing_mm / 1000, 2)} m of line`],
      ['Layers', enabled.map((l) => l.name + (l.pen ? ` (${l.pen})` : '')).join(', ') + (s.swaps ? `; ${s.swaps} pen change${s.swaps > 1 ? 's' : ''}` : '')],
      ['Speeds', `${s.speed} mm/s drawing, ${s.travel_speed} mm/s pen up`],
    ];
    kv.replaceChildren(...rows.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })]));
  }
}

async function previewNow() {
  const r = await runPreview();
  if (r && r.ok) toast('Plan checked: ready.');
}

// ----------------------------------------------------------- start flow
async function start(mode) {
  let pv = state.preview;
  if (!pv || state.previewStale) pv = await runPreview();
  if (!pv || !pv.ok) { toast('The plan has problems: see Run.', true); return; }
  if (mode !== 'simulate') {
    const ok = await confirmStart(mode, pv);
    if (!ok) return;
    // The dialog may have been open a while: check again, and start only if it is still the plan confirmed.
    const confirmed = pv.plan_id;
    pv = await runPreview();
    if (!pv || !pv.ok) { toast('The plan changed and has problems now. Nothing was started.', true); return; }
    if (pv.plan_id !== confirmed) {
      toast('The drawing, settings or calibration changed while the dialog was open. Nothing was started: check and start again.', true);
      return;
    }
  }
  try {
    const r = await post('/api/job/start', { id: pv.file, settings: pv.settings, plan_id: pv.plan_id, mode,
      confirm: mode === 'simulate' ? undefined : { parked: true } });
    toast(mode === 'simulate' ? 'Simulation started: nothing moves.' : 'Started.');
    return r;
  } catch (e) {
    toast(e.message, true);
    if (e instanceof ApiError && e.status === 409) runPreview();
  }
}

function confirmStart(mode, pv) {
  return new Promise((resolve) => {
    const d = $('confirmDlg'), m = state.me.machine, s = pv.summary;
    const first = s.layers.find((l) => l.enabled !== false);
    $('confirmTitle').textContent = mode === 'trace' ? 'Trace the frame (pen up)' : `Start plot${state.me.sim ? ' (simulator)' : ''}`;
    const b = s.bbox_machine_mm;
    const rows = [['Drawing', state.current.name],
      ['Area', `${mm(b[2] - b[0])} x ${mm(b[3] - b[1])} mm at (${mm(b[0])}, ${mm(b[1])}) mm`]];
    if (mode === 'plot') {
      rows.push(['Time', `about ${fmtTime(s.est_seconds)}`]);
      rows.push(['Pens', s.layers.filter((l) => l.enabled !== false).map((l, i) => `${i + 1}. ${l.name}: ${l.pen || 'pen not set'}, ${l.speed} mm/s` +
        (l.pressure !== null && l.pressure !== undefined ? `, pressure ${l.pressure}` : '')).join('; ')]);
      if (s.swaps) rows.push(['Pen changes', `${s.swaps}: the plot pauses and asks for each one`]);
    } else rows.push(['Moves', 'Pen up around the drawing\'s frame and back to S. Nothing is drawn.']);
    $('confirmKv').replaceChildren(...rows.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })]));
    const checks = [
      `The plotter is on and the carriage is parked by hand at S, the paper's top-left (${(s.start_mm || m.start)[0]}, ${(s.start_mm || m.start)[1]}) mm. It was slid there with the motors off.`,
      mode === 'plot' ? `The pen is up and the first pen is in${first && first.pen ? `: ${first.pen}` : ''}.` : 'The pen is up.',
      'The paper is taped down, nothing is in the carriage\'s way, and the power switch is within reach.',
    ];
    const box = $('confirmChecks');
    box.replaceChildren(...checks.map((t, i) => el('label', { class: 'check' }, el('input', { type: 'checkbox', id: `cc${i}` }), ' ', t)));
    const go = $('confirmGo');
    go.textContent = mode === 'trace' ? 'Start trace' : 'Start plot';
    go.disabled = true;
    box.onchange = () => { go.disabled = ![...box.querySelectorAll('input')].every((c) => c.checked); };
    d.returnValue = '';
    d.onclose = () => resolve(d.returnValue === 'ok');
    d.showModal();
  });
}

// ------------------------------------------------------------- controls
async function control(path, body) {
  try { await post(path, body); } catch (e) { toast(e.message, true); }
}
export function pause() { if (state.job && state.job.state === 'running') control('/api/job/pause', { job: state.job.id }); }
export function resume() {
  const j = state.job;
  if (j && j.state === 'paused' && j.pause) control('/api/job/continue', { job: j.id, pause: j.pause.id });
}
function returnToStart() { if (state.job) control('/api/job/return', { job: state.job.id }); }
export function stop() { if (jobActive()) control('/api/job/stop', { job: state.job.id }); }
function pentest(cmd) { if (state.job) control('/api/pentest', { job: state.job.id, cmd }); }
