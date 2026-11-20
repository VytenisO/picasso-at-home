// Status panel, run controls, the start confirmation and the pen-swap prompt.
import { api, post, ApiError } from './api.js';
import { state, set, on, jobActive, prefs } from './store.js';
import { $, el, toast, fmtTime, mm } from './ui.js';
import { runPreview } from './workspace.js';
import { fileName, setStartAt, setSettings } from './doc.js';
import * as G from './geom.js';

const TITLES = {
  idle: 'Idle', starting: 'Starting', running: 'Plotting', pausing: 'Pausing', paused: 'Paused',
  stopping: 'Stopping', returning: 'Returning to start', complete: 'Finished', stopped: 'Stopped',
  interrupted: 'Interrupted', error: 'Error',
};
const DOT = { running: 'busy', starting: 'busy', returning: 'busy', pausing: 'warn', paused: 'warn', stopping: 'warn',
  complete: 'ok', stopped: 'warn', interrupted: 'warn', error: 'err' };

export function initRun() {
  $('previewBtn').onclick = previewNow;
  $('checklistText').value = prefs('checklist') || '';
  $('checklistText').onchange = () => prefs('checklist', $('checklistText').value.slice(0, 2000));
  $('speedRange').oninput = () => { $('speedPct').textContent = `${$('speedRange').value}%`; };
  $('speedRange').onchange = async () => {
    try { await post('/api/job/speed', { job: state.job.id, factor: Number($('speedRange').value) / 100 }); } catch (e) { toast(e.message, true); }
  };
  on('job', () => {
    const j = state.job;
    const show = !!(j && j.kind === 'plot' && j.mode !== 'trace' && ['running', 'pausing', 'paused'].includes(j.state));
    $('speedLive').hidden = !show;
    if (show && document.activeElement !== $('speedRange')) {
      const v = Math.round((j.speed_factor || 1) * 100);
      $('speedRange').value = v; $('speedPct').textContent = `${v}%`;
    }
  });
  initNotify();
  $('histBox').addEventListener('toggle', loadHistory);
  on('job', () => { const j = state.job; if (j && !jobActive() && $('histBox').open) loadHistory(); });
  $('onlySelBtn').onclick = () => {
    const ids = state.sel.filter((id) => state.doc.objects.some((o) => o.id === id));
    if (!ids.length) { toast('Select the objects to plot first.', true); return; }
    setSettings({ only: ids });
  };
  $('onlyAll').onclick = () => setSettings({ only: null });
  $('exportBtn').onclick = exportPlot;
  $('compileBtn').onclick = (e) => exportPlot(e, '/api/job/compile', 'bed.ebb');
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
  on(['job', 'preview', 'previewStale', 'previewing', 'doc', 'me', 'startAt', 'units'], renderRun);
  $('startAtReset').onclick = () => setStartAt(0);
  $('resumeBtn').onclick = () => { const r = resumePoint(); if (r) setStartAt(r); };
  // a finished resumed plot: the next one starts from the beginning again
  on('job', () => { if (state.job && state.job.state === 'complete' && state.startAt) setStartAt(0); });
  setInterval(renderStale, 2000);
}

const hasBed = () => !!(state.doc && state.doc.objects.length);

export function title(job) {
  if (job && job.kind === 'machine' && job.state === 'running') return 'In control';
  return job ? TITLES[job.state] || job.state : 'Idle';
}
export function dotClass(job) { return job ? DOT[job.state] || '' : ''; }

function nextStep(job) {
  if (!job || !jobActive()) {
    if (state.position && state.position.state !== 'at_start')
      return 'Before the next run: motors off, slide the carriage by hand to S (the paper\'s top-left), pen up.';
    if (!hasBed()) return 'Add a drawing to the bed from the Library.';
    if (!state.preview || state.previewStale) return 'Preview to check the plan.';
    if (!state.preview.ok) return 'Fix the problems listed under Run, then preview again.';
    return 'Ready. Start plot when the carriage is parked at S with the pen up.';
  }
  if (job.kind === 'machine' && job.state === 'running') return 'Jog under Machine, Control. Finish there; Stop lifts the pen and releases the motors.';
  if (job.kind === 'live' && job.state === 'running') return 'Type a line under Tools, Live writing, and press Write. Finish returns to S; Stop finishes the move already sent, then lifts the pen.';
  switch (job.state) {
    case 'paused': return job.pause && job.pause.kind === 'swap'
      ? 'Change the pen, check it is up and seated, then press Continue.'
      : job.pause && (job.pause.kind === 'paper' || job.pause.kind === 'page')
        ? 'Put in fresh paper exactly where the last sheet was, then press Continue.'
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
  $('stTitle').textContent = job ? ({ pentest: 'Pen test: ', live: 'Live writing: ', machine: 'Machine control: ' }[job.kind] || '') + title(job) : 'Idle';
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
  if (active && ['plot', 'live', 'machine'].includes(job.kind)) {
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
    } else if (pz.kind === 'page') {
      $('swapTitle').textContent = `Next sheet: page ${pz.page + 1}`;
      $('swapText').textContent = `Page ${pz.page} is done and the carriage is back at S, pen up, motors on: do not push it. ` +
        `Take the sheet out, put the sheet for page ${pz.page + 1} in exactly the same place, then press Continue.`;
    } else if (pz.kind === 'paper') {
      $('swapTitle').textContent = `Paper change: copy ${pz.copy} of ${pz.copies}`;
      $('swapText').textContent = `Copy ${pz.copy - 1} is done and the carriage is back at S, pen up, motors on: do not push it. ` +
        'Take the sheet out, put fresh paper in exactly the same place, then press Continue.';
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
  const ready = !!(pv && pv.ok && !stale && !state.previewing && hasBed());
  $('previewBtn').disabled = !hasBed() || state.previewing;
  $('previewBtn').textContent = state.previewing ? 'Checking...' : 'Preview';
  for (const b of ['simBtn', 'traceBtn', 'plotBtn']) $(b).disabled = !ready;
  if (active && !isPentest) {
    const live = job.kind === 'live' || job.kind === 'machine';   // no pause or return: finish it or stop it
    $('pauseBtn').disabled = job.state !== 'running' || live;
    $('continueBtn').disabled = job.state !== 'paused' || (job.pause && job.pause.resuming);
    $('returnBtn').disabled = live || !['running', 'pausing', 'paused'].includes(job.state);
    $('stopBtn').disabled = job.state === 'stopping';
  }
  const ps = $('previewState'), list = $('problemList'), kv = $('planKv');
  list.hidden = true;
  kv.replaceChildren();
  ps.hidden = kv.hidden = active;
  const rp = active ? 0 : resumePoint();
  $('resumeBox').hidden = !rp || state.startAt === rp;
  if (rp) $('resumeText').textContent = `The last plot stopped after stroke ${rp} of this bed. Re-park the carriage at S, ` +
    `then resume at stroke ${rp + 1} (a stroke cut short by the stop is drawn whole).`;
  $('startAtBox').hidden = active || !state.startAt;
  const all = state.doc ? state.doc.objects : [];
  const only = ((state.doc && state.doc.settings.only) || []).filter((id) => all.some((o) => o.id === id));
  $('onlyBox').hidden = active || !only.length;
  if (only.length) $('onlyText').textContent = `Plotting only ${only.length} of ${all.length} object${all.length === 1 ? '' : 's'}; the rest are shown faded and stay on the bed. `;
  $('onlySelBtn').disabled = active || !state.sel.length;
  $('exportBtn').disabled = active || !all.length;
  $('compileBtn').disabled = active || !all.length;
  if (state.startAt) {
    const tot = pv && pv.summary && pv.summary.total_in_order;
    $('startAtText').textContent = `Starts at stroke ${state.startAt + 1}${tot ? ` of ${tot}` : ''}: the first ${state.startAt} are skipped. `;
  }
  if (active) return;
  if (!hasBed()) { ps.className = 'note'; ps.textContent = 'Add a drawing to the bed, then preview it.'; return; }
  if (state.previewing) { ps.className = 'note'; ps.textContent = 'Checking the plan...'; }
  else if (!pv || stale) { ps.className = 'note warn'; ps.textContent = 'Settings changed. Preview to check again.'; }
  else if (!pv.ok) {
    ps.className = 'note err'; ps.textContent = 'Not ready to plot:';
    list.hidden = false;
    list.replaceChildren(...(pv.problems || []).map((p) => el('li', { text: p })));
  } else { ps.className = 'note ok'; ps.textContent = 'Plan checked. Ready to plot.'; }
  if (pv && pv.summary) {
    const s = pv.summary, b = s.bbox_machine_mm;
    const enabled = s.layers.filter((l) => l.enabled !== false).sort((a, b) => (a.rank ?? 0) - (b.rank ?? 0));
    const fo = s.optimized && !s.start_at ? s.file_order : null;
    const rows = [
      ['Size', `${G.fmtLen(b[2] - b[0], false)} x ${G.fmtLen(b[3] - b[1])} at (${G.fmtLen(b[0], false)}, ${G.fmtLen(b[1], false)})`],
      ['Time', `about ${fmtTime(s.est_seconds)}` + (fo && fo.est_seconds - s.est_seconds > 1 ? ` (${fmtTime(fo.est_seconds)} in file order)` : '') +
        (pv.time_factor && Math.abs(pv.time_factor - 1) > 0.05 ? `; past plots ran ${Math.round(pv.time_factor * 100)}% of the estimate, so about ${fmtTime(s.est_seconds * pv.time_factor)}` : '') +
        (s.copies > 1 ? `; ${s.copies} copies, about ${fmtTime(s.est_seconds_all)} with no time for paper changes` : '')],
      ['Pen up', `${mm(s.travel_mm / 1000, 2)} m` + (fo ? ` (${mm(fo.travel_mm / 1000, 2)} m in file order)` : '') + `, ${s.lifts} lifts`],
      ['Strokes', `${s.start_at ? `${s.total_in_order - s.start_at} of ${s.total_in_order}` : s.strokes}, ${mm(s.drawing_mm / 1000, 2)} m of line` +
        (s.duplicates_dropped ? `; ${s.duplicates_dropped} repeated stroke${s.duplicates_dropped > 1 ? 's' : ''} skipped` : '')],
      ['Layers', enabled.map((l) => (l.title || l.name) + (l.pen ? ` (${l.pen})` : '') + (l.passes > 1 ? ` x${l.passes}` : '')).join(', ') + (s.swaps ? `; ${s.swaps} pen change${s.swaps > 1 ? 's' : ''}` : '')],
      ['Speeds', `${s.speed} mm/s drawing, ${s.travel_speed} mm/s pen up`],
    ];
    kv.replaceChildren(...rows.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })]));
  }
}

// Where a stopped plot of this same bed got to: the absolute stroke to resume at, or 0.
function resumePoint() {
  const j = state.job, pv = state.preview;
  if (!j || j.kind !== 'plot' || j.mode !== 'plot' || !['stopped', 'interrupted', 'error'].includes(j.state)) return 0;
  const done = j.progress ? j.progress.done : 0;
  if (!done || !pv || !pv.bed_key || !j.meta || j.meta.bed_key !== pv.bed_key) return 0;
  return (j.meta.start_at || 0) + done;
}

// ----------------------------------------------------------- notifications and history
// A browser notification (and a short tone) when a job needs a hand or ends. Off until asked for here.
let lastJobState = null;
function initNotify() {
  const n = $('notifyOn'), snd = $('soundOn');
  const can = 'Notification' in window;
  n.disabled = !can;
  if (!can) n.parentElement.title = 'This browser cannot show notifications (they need https or localhost).';
  n.checked = can && !!prefs('notify') && Notification.permission === 'granted';
  snd.checked = !!prefs('sound');
  n.onchange = async () => {
    if (n.checked && Notification.permission !== 'granted') {
      const p = await Notification.requestPermission().catch(() => 'denied');
      if (p !== 'granted') { n.checked = false; toast('Notifications were not allowed by the browser.', true); }
    }
    prefs('notify', n.checked);
  };
  snd.onchange = () => { prefs('sound', snd.checked); if (snd.checked) tone(); };
  on('job', () => {
    const j = state.job;
    const key = j ? `${j.id}:${j.state}:${j.pause ? j.pause.id : ''}` : null;
    if (key === lastJobState) return;
    const first = lastJobState === null;
    lastJobState = key;
    if (first || !j) return;
    let msg = null;
    if (j.state === 'paused' && j.pause && j.pause.kind === 'swap') msg = `Pen change: ${j.pause.label}${j.pause.pen ? ` (${j.pause.pen})` : ''}`;
    else if (j.state === 'paused' && j.pause && j.pause.kind === 'paper') msg = `Paper change: copy ${j.pause.copy} of ${j.pause.copies}`;
    else if (j.state === 'paused' && j.pause && j.pause.kind === 'page') msg = `Next sheet: page ${j.pause.page + 1}`;
    else if (['complete', 'stopped', 'error', 'interrupted'].includes(j.state)) msg = `${title(j)}: ${j.meta && j.meta.name ? j.meta.name : j.kind}`;
    if (!msg) return;
    if (prefs('sound')) tone();
    if (prefs('notify') && 'Notification' in window && Notification.permission === 'granted') {
      try { new Notification('Pen plotter', { body: msg, tag: 'plotter-job' }); } catch (e) { /* not allowed here */ }
    }
  });
}

function tone() {
  try {
    const ac = new (window.AudioContext || window.webkitAudioContext)();
    const o = ac.createOscillator(), g = ac.createGain();
    o.frequency.value = 880; g.gain.value = 0.08;
    o.connect(g); g.connect(ac.destination);
    o.start(); o.stop(ac.currentTime + 0.25);
    o.onended = () => ac.close();
  } catch (e) { /* no audio */ }
}

async function loadHistory() {
  if (!$('histBox').open) return;
  let r;
  try { r = await api('/api/history'); } catch (e) { return; }
  const fmtDate = (t) => new Date(t * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  $('histList').replaceChildren(...(r.jobs.length ? r.jobs.map((h) => el('li', {},
    el('b', { text: h.name || h.kind }), el('span', { class: `st-${h.state}`, text: h.state }),
    el('small', { class: 'muted', text: `${fmtDate(h.started)}, ${fmtTime(h.seconds)}${h.sim ? ', simulated' : ''}${h.kind !== 'plot' ? `, ${h.kind}` : ''}` }),
    el('small', { class: 'muted', text: h.total ? `${h.done} of ${h.total} strokes` : '' }))) : [el('li', { text: 'No jobs yet.' })]));
}

// Download exactly what will be plotted, as the server plans it now.
async function exportPlot(e, route = '/api/export_doc', fallback = 'plot.svg') {
  const d = state.doc;
  if (!d || !d.objects.length) { toast('The bed is empty.', true); return; }
  let r;
  try {
    r = await fetch(route, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ doc: { name: d.name, objects: d.objects, settings: { ...d.settings, start_at: 0 } } }) });
  } catch (e) { toast('The server cannot be reached.', true); return; }
  if (!r.ok) {
    let msg = `Export failed (${r.status})`;
    try { msg = (await r.json()).error || msg; } catch (e) { /* not JSON */ }
    toast(msg, true); return;
  }
  const name = (/filename="([^"]+)"/.exec(r.headers.get('Content-Disposition') || '') || [])[1] || fallback;
  if (route !== '/api/export_doc') toast('Compiled job saved. Run it with tools/run_compiled.py (it checks the calibration first).');
  const url = URL.createObjectURL(await r.blob());
  const a = el('a', { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
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
    const d = state.doc;
    const r = await post('/api/job/start', { doc: { name: d.name, objects: d.objects, settings: { ...d.settings, start_at: state.startAt || 0 } },
      plan_id: pv.plan_id, mode, confirm: mode === 'simulate' ? undefined : { parked: true }, sim_speed: prefs('simSpeed') || undefined });
    toast(mode === 'simulate' ? 'Simulation started: nothing moves.' : 'Started.');
    return r;
  } catch (e) {
    toast(e.message, true);
    if (e instanceof ApiError && e.status === 409) runPreview();
  }
}

function confirmStart(mode, pv) {
  return new Promise((resolve) => {
    const m = state.me.machine, s = pv.summary;
    const first = s.layers.filter((l) => l.enabled !== false).sort((a, b) => (a.rank ?? 0) - (b.rank ?? 0))[0];
    const title = mode === 'trace' ? 'Trace the frame (pen up)' : `Start plot${state.me.sim ? ' (simulator)' : ''}`;
    const b = s.bbox_machine_mm;
    const objs = state.doc.objects;
    const rows = [['Drawing', objs.length === 1 ? fileName(objs[0]) : `${objs.length} objects: ${[...new Set(objs.map(fileName))].join(', ')}`],
      ['Area', `${mm(b[2] - b[0])} x ${mm(b[3] - b[1])} mm at (${mm(b[0])}, ${mm(b[1])}) mm`]];
    if (mode === 'plot') {
      rows.push(['Time', `about ${fmtTime(s.est_seconds)}`]);
      rows.push(['Pens', s.layers.filter((l) => l.enabled !== false).sort((a, b) => (a.rank ?? 0) - (b.rank ?? 0)).map((l, i) => `${i + 1}. ${l.title || l.name}: ${l.pen || 'pen not set'}, ${l.speed} mm/s` +
        (l.pressure !== null && l.pressure !== undefined ? `, pressure ${l.pressure}` : '')).join('; ')]);
      if (s.swaps) rows.push(['Pen changes', `${s.swaps}: the plot pauses and asks for each one`]);
    } else rows.push(['Moves', 'Pen up around the drawing\'s frame and back to S. Nothing is drawn.']);
    const checks = [
      `The plotter is on and the carriage is parked by hand at S, the paper's top-left (${(s.start_mm || m.start)[0]}, ${(s.start_mm || m.start)[1]}) mm. It was slid there with the motors off.`,
      mode === 'plot' ? `The pen is up and the first pen is in${first && first.pen ? `: ${first.pen}` : ''}.` : 'The pen is up.',
      'The paper is taped down, nothing is in the carriage\'s way, and the power switch is within reach.',
      ...String(prefs('checklist') || '').split('\n').map((x) => x.trim()).filter(Boolean),
    ];
    return confirmDialog(title, rows, checks, mode === 'trace' ? 'Start trace' : 'Start plot').then(resolve);
  });
}

// The shared start dialog: a title, facts, the checklist that must all be ticked, and a go button.
export function confirmDialog(titleText, rows, checks, goLabel) {
  return new Promise((resolve) => {
    const d = $('confirmDlg');
    $('confirmTitle').textContent = titleText;
    $('confirmKv').replaceChildren(...rows.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })]));
    const box = $('confirmChecks');
    box.replaceChildren(...checks.map((t, i) => el('label', { class: 'check' }, el('input', { type: 'checkbox', id: `cc${i}` }), ' ', t)));
    const go = $('confirmGo');
    go.textContent = goLabel;
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
