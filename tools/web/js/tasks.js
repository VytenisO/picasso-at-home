// Background tasks (slow traces, shading measurements): the server runs them one at a time and reports each
// change as a "task" event. This keeps the list, shows what runs in the top bar with its progress and time
// left, lists the recent ones with how long they took, and lets other modules wait for one to finish.
import { api, post } from './api.js';
import { $, el, toast } from './ui.js';

const tasks = new Map();          // id -> task, as the server reports it
const waiters = new Map();        // id -> [{ resolve, onUpdate }]
let popOpen = false;
let tick = null;
let lastEvent = 0;

export async function initTasks() {
  $('taskPill').onclick = () => setPop(!popOpen);
  document.addEventListener('pointerdown', (e) => {
    if (popOpen && !$('taskPop').contains(e.target) && e.target !== $('taskPill')) setPop(false);
  });
  document.addEventListener('keydown', (e) => { if (popOpen && e.key === 'Escape') setPop(false); });
  await loadTasks();
}

export async function loadTasks() {
  try {
    const r = await api('/api/tasks');
    tasks.clear();
    for (const t of r.tasks) tasks.set(t.id, { ...t, seen: Date.now() });
  } catch (e) { /* the pill stays as it was */ }
  for (const id of waiters.keys()) settle(id);
  render();
}

export function onTaskEvent(t) {
  lastEvent = Date.now();
  tasks.set(t.id, { ...t, seen: Date.now() });
  settle(t.id);
  render();
}

export function taskById(id) { return tasks.get(id) || null; }

// -> Promise of the finished task. onUpdate(task) on every progress report until then.
export function waitTask(id, onUpdate) {
  return new Promise((resolve) => {
    const list = waiters.get(id) || [];
    list.push({ resolve, onUpdate });
    waiters.set(id, list);
    settle(id);
    render();
  });
}

export async function cancelTask(id) {
  try { await post('/api/tasks/cancel', { id }); }
  catch (e) { toast(e.message, true); }
}

function settle(id) {
  const t = tasks.get(id), list = waiters.get(id);
  if (!t || !list) return;
  for (const w of list) if (w.onUpdate) w.onUpdate(live(t));
  if (t.state === 'queued' || t.state === 'running') return;
  waiters.delete(id);
  for (const w of list) w.resolve(t);
}

// Elapsed and time left move on between events: extrapolate from when the event arrived.
export function live(t) {
  if (t.state !== 'running') return t;
  const dt = (Date.now() - t.seen) / 1000;
  return { ...t, elapsed: t.elapsed + dt, eta: t.eta == null ? null : Math.max(0, t.eta - dt) };
}

export function fmtDur(s) {
  if (s == null || !isFinite(s)) return '';
  s = Math.round(s);
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, '0')} s`;
  return `${Math.floor(s / 3600)} h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')} min`;
}

// One line about a task: what it is doing, how far, how long so far and how long to go.
export function taskLine(t) {
  t = live(t);
  if (t.state === 'queued') return `Waiting${t.position > 1 ? ` (${t.position - 1} ahead)` : ''}` + (t.eta != null ? `, about ${fmtDur(t.eta)} once started` : '');
  if (t.state === 'running') {
    return `${t.stage}, ${Math.floor(t.frac * 100)} %, ${fmtDur(t.elapsed)}` + (t.eta != null ? `, about ${fmtDur(t.eta)} left` : '');
  }
  if (t.state === 'done') return `Done in ${fmtDur(t.elapsed)}`;
  if (t.state === 'cancelled') return `Cancelled after ${fmtDur(t.elapsed)}`;
  return `Failed: ${t.error || 'unknown error'}`;
}

function active() { return [...tasks.values()].filter((t) => t.state === 'running' || t.state === 'queued'); }

function render() {
  const pill = $('taskPill');
  const act = active();
  const run = act.find((t) => t.state === 'running');
  const recent = [...tasks.values()].length;
  pill.hidden = !recent;
  pill.classList.toggle('busy', !!act.length);
  if (run) {
    const t = live(run);
    pill.textContent = `${Math.floor(t.frac * 100)} %` + (t.eta != null ? ` · ${fmtDur(t.eta)} left` : '') + (act.length > 1 ? ` · +${act.length - 1}` : '');
    pill.title = `${run.title}: ${taskLine(run)}. Click for all tasks.`;
    pill.style.setProperty('--p', `${Math.floor(t.frac * 100)}%`);
  } else if (act.length) {
    pill.textContent = `${act.length} waiting`;
    pill.title = 'Tasks waiting to run. Click for all tasks.';
    pill.style.setProperty('--p', '0%');
  } else {
    pill.textContent = 'Tasks';
    pill.title = 'Recent background tasks and how long they took';
    pill.style.setProperty('--p', '0%');
  }
  if (popOpen) renderPop();
  // keep the times moving while something runs (or someone waits); ask the server when the live stream is
  // quiet, so a dropped connection never leaves a wait hanging
  if ((act.length || waiters.size) && !tick) tick = setInterval(poll, 1000);
  if (!act.length && !waiters.size && tick) { clearInterval(tick); tick = null; }
}

let polling = false;
async function poll() {
  if (Date.now() - lastEvent > 3000 && !polling) {
    polling = true;
    lastEvent = Date.now();
    try { await loadTasks(); } finally { polling = false; }
    return;
  }
  render();
}

function setPop(open) {
  popOpen = open;
  $('taskPop').hidden = !open;
  $('taskPill').setAttribute('aria-expanded', String(open));
  if (open) renderPop();
}

function renderPop() {
  const list = [...tasks.values()].sort((a, b) => (b.queued_at || 0) - (a.queued_at || 0));
  const rows = list.map((t) => {
    const lt = live(t);
    const busy = t.state === 'running' || t.state === 'queued';
    const fill = el('span');
    fill.style.width = `${Math.floor(lt.frac * 100)}%`;        // CSSOM: the page's CSP forbids style attributes
    const bar = el('div', { class: 'task-bar' }, fill);
    return el('li', { class: `task ${t.state}` },
      el('div', { class: 'task-head' }, el('b', { text: t.title }),
        busy ? el('button', { type: 'button', class: 'link danger-text', text: 'Cancel', onclick: () => cancelTask(t.id) }) : null),
      busy ? bar : null,
      el('small', { class: 'muted', text: taskLine(t) + (t.started_at ? ` · started ${new Date(t.started_at * 1000).toLocaleTimeString()}` : '') }));
  });
  $('taskList').replaceChildren(...(rows.length ? rows : [el('li', { class: 'muted', text: 'No tasks yet.' })]));
}
