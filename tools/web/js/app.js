// Boot, login, live events, tabs, theme and keyboard shortcuts. The server owns the job state; this page is a view
// of it and can be reloaded or opened on another device at any time.
import { api, post, connectEvents, setUnauthorizedHandler } from './api.js';
import { state, set, on, jobActive, prefs } from './store.js';
import { $, toast } from './ui.js';
import { initBed, fit, zoomIn, zoomOut, themeChanged, toggleTravel, zoomSelection, togglePanels } from './bed.js';
import { initDoc } from './doc.js';
import { initWorkspace, openFile, refreshFiles, runPreview, initTextTool } from './workspace.js';
import * as D from './doc.js';
import { initRun, pause, resume, stop } from './run.js';
import { initLibrary, setObjectPaste } from './library.js';
import { renderShortcutList, tagTooltips } from './shortcuts.js';
import { initTools } from './tools.js';
import { initLive } from './live.js';
import { initConsole, addEvents, resetConsole } from './console.js';
import { initWs, showWs } from './ws.js';
import { initPrepare, prepFit, prepZoom, prepKey } from './prepare.js';
import { initMachine, reloadConfig } from './machine.js';
import { initMenuBar, maybeWizard } from './menubar.js';
import { initTasks, loadTasks, onTaskEvent } from './tasks.js';
import { initChrome, showTab } from './chrome.js';

let stream = null;

async function boot() {
  setUnauthorizedHandler(showLogin);
  let me;
  try { me = await api('/api/me'); }
  catch (e) { document.body.textContent = e.message; return; }
  if (me.auth_required && !me.auth) return showLogin();
  $('loginView').hidden = true;
  $('appView').hidden = false;
  initOnce();
  await loadServerInfo(me);
  await syncState();
  fit();
}

// Everything that can differ between server runs (simulator or hardware, machine, pens, catalogue).
async function loadServerInfo(me) {
  set({ me });
  setTimeout(maybeWizard, 500);
  $('logoutBtn').hidden = !me.auth_required;
  $('modeBadge').textContent = me.sim ? 'Simulator' : 'Hardware';
  $('modeBadge').className = 'badge ' + (me.sim ? 'sim' : 'hw');
  $('modeBadge').title = me.sim ? 'Server runs in simulator mode: nothing ever moves.' : 'Jobs move the real plotter.';
  const [conv, tools] = await Promise.all([api('/api/converters'), api('/api/tools')]);
  set({ converters: conv.converters, tools: tools.tools });
  await refreshFiles();
}

let inited = false;
function initOnce() {
  if (inited) return;
  inited = true;
  initChrome();
  initBed();
  initDoc();
  initWorkspace();
  initTextTool();
  initRun();
  initLibrary();
  renderShortcutList();
  api('/api/about').then((a) => {
    $('aboutLine').textContent = `Version ${a.app || 'unknown'}; Python ${a.python}; pyserial ${a.pyserial || 'missing'}, ` +
      `shapely ${a.shapely || 'missing'}, Pillow ${a.pillow || 'missing'}; firmware ${a.firmware || 'not checked yet'}` +
      (a.sim ? '; simulator' : '') + '.';
  }).catch(() => {});
  tagTooltips();
  setObjectPaste(D.paste);
  initTools();
  initLive();
  initConsole();
  initTabs();
  initWs();
  initTasks();
  initPrepare();
  initMachine();
  initTheme();
  initKeys();
  initMenuBar();
  on('job', followJob);
}

// Snapshot + console history, then the live stream from that point on.
async function syncState() {
  let st = await api('/api/state');
  if (state.me && st.epoch !== state.me.epoch) {
    // The server restarted (maybe in another mode, with another calibration): reload everything it serves.
    await loadServerInfo(await api('/api/me'));
    set({ preview: null, previewStale: true, jobPreview: null, naturals: {} });
    state.doc = null;
    toast('The server restarted. The view was reloaded.');
    st = await api('/api/state');
  }
  applyState(st);
  const log = await api('/api/log?since=0');
  resetConsole();
  addEvents(log.events);
  if (st.job && st.job.kind === 'plot') loadJobPreview();
  if (!state.doc) {
    await D.loadDoc();
    // First visit since the bed holds several drawings: put the last opened one on it.
    const last = prefs('lastFile');
    if (!state.doc.objects.length && last && state.files.some((f) => f.id === last) && !jobActive()) openFile(last);
  }
  if (stream) stream.reopen(st.epoch, st.seq);
  else stream = connectEvents({ epoch: st.epoch, seq: st.seq, onEvent, onLink: (link) => set({ link, lastEventAt: Date.now() }) });
}

function applyState(st) {
  set({ job: st.job, position: st.position, pot: st.pot || null, lastEventAt: Date.now() });
}

async function loadJobPreview() {
  try { set({ jobPreview: await api('/api/job/preview') }); } catch (e) { /* no preview yet */ }
}

let lastJobId = null;
function followJob() {
  const j = state.job;
  if (!j) return;
  if (j.id !== lastJobId && j.kind === 'plot' && j.state !== 'starting') { lastJobId = j.id; loadJobPreview(); }
  if (!jobActive()) {
    set({ pos: null });
    if (state.doc && state.doc.objects.length && !state.preview && !state.previewing) runPreview();
  }
}

function onEvent(ev) {
  state.lastEventAt = Date.now();
  switch (ev.type) {
    case 'log': addEvents([ev]); break;
    case 'state': set({ job: ev.job, position: ev.position, lastEventAt: Date.now() }); break;
    case 'pot': set({ pot: ev.pot || null }); break;
    case 'pos': set({ pos: { x: ev.x, y: ev.y, pen: ev.pen } }); break;
    case 'files': refreshFiles(); break;
    case 'doc': D.onDocEvent(ev); break;
    case 'config': reloadConfig().then(() => { if (state.doc && state.doc.objects.length) { set({ previewStale: true }); runPreview(); } }); break;
    case 'task': onTaskEvent(ev.task); break;
    case 'reset': syncState().catch((e) => toast(e.message, true)); loadTasks(); break;
    default: set({ lastEventAt: Date.now() });
  }
}

// ----------------------------------------------------------------- login
function showLogin() {
  $('loginStop').onclick = async () => {
    try { const r = await post('/api/job/stop', {}); toast(r.message || 'Stop requested.'); }
    catch (e) { toast(e.message, true); }
  };
  // Mid-session (expired cookie) the workspace stays visible: Stop works without a session.
  const midSession = inited && !$('appView').hidden;
  $('loginView').classList.toggle('floating', midSession);
  $('loginNote').hidden = !midSession;
  if (!midSession) $('appView').hidden = true;
  $('loginView').hidden = false;
  if (!midSession) $('loginPw').focus();
  $('loginForm').onsubmit = async (e) => {
    e.preventDefault();
    try {
      await post('/api/login', { password: $('loginPw').value });
      $('loginErr').hidden = true;
      $('loginPw').value = '';
      $('loginView').hidden = true;
      if (midSession) syncState().catch((err) => toast(err.message, true)); else boot();
    } catch (err) { $('loginErr').textContent = err.message; $('loginErr').hidden = false; }
  };
}
$('logoutBtn') && ($('logoutBtn').onclick = async () => { await post('/api/logout', {}); showLogin(); });

// ------------------------------------------------------------- phone views
// On a narrow screen one thing shows at a time: the bed, a side panel (with one of its tabs) or the console.
function initTabs() {
  const btns = [...document.querySelectorAll('.mobile-tabs button')];
  for (const b of btns) {
    b.onclick = () => {
      document.body.dataset.view = b.dataset.view;
      if (b.dataset.tab) showTab(b.dataset.tab);
      for (const x of btns) x.setAttribute('aria-selected', String(x === b));
      if (b.dataset.view === 'bed') requestAnimationFrame(fit);
    };
  }
  // A job starting brings its status into view.
  let was = false;
  on('job', () => { const now = jobActive(); if (now && !was) showTab('status'); was = now; });
}

function initTheme() {
  const order = ['system', 'light', 'dark'];
  const apply = (t) => {
    if (t === 'system') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
    $('themeBtn').title = `Theme: ${t}. Click for ${order[(order.indexOf(t) + 1) % 3]}`;
    themeChanged();
  };
  let cur = prefs('theme') || 'system';
  apply(cur);
  $('themeBtn').onclick = () => { cur = order[(order.indexOf(cur) + 1) % 3]; prefs('theme', cur); apply(cur); };
  $('helpBtn').onclick = () => $('helpDlg').showModal();
  $('helpClose').onclick = () => $('helpDlg').close();
}

function initKeys() {
  document.addEventListener('keydown', (e) => {
    const t = e.target;
    const typing = t && (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
    if (e.key === 'S' && e.shiftKey && !e.ctrlKey && !e.metaKey && !typing) { e.preventDefault(); stop(); return; }
    if (state.ws === 'prepare' && !document.querySelector('dialog[open]') && prepKey(e)) return;
    if (state.ws === 'plot' && !typing && !document.querySelector('dialog[open]') && docKey(e)) return;
    if (typing || e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || document.querySelector('dialog[open]')) return;
    const prep = state.ws === 'prepare';
    if (e.key === '?') { $('helpDlg').showModal(); e.preventDefault(); }
    else if (e.key === '1') showWs('prepare');
    else if (e.key === '2') showWs('plot');
    else if ((e.key === 'p' || e.key === 'P') && !prep) { if (!jobActive()) runPreview(); }
    else if ((e.key === 't' || e.key === 'T') && !prep) toggleTravel();
    else if ((e.key === 'z' || e.key === 'Z') && !prep) zoomSelection();
    else if (e.key === '\\' && !prep) togglePanels();
    else if (e.key === ' ' && !e.repeat && !(t && t.closest && t.closest('button, a, summary, [role=button], [role=tab]'))) {
      e.preventDefault();
      if (state.job && state.job.state === 'running') pause();
      else if (state.job && state.job.state === 'paused') resume();
    }
    else if (e.key === '+' || e.key === '=') (prep ? prepZoom(1.25) : zoomIn());
    else if (e.key === '-') (prep ? prepZoom(0.8) : zoomOut());
    else if (e.key === '0') (prep ? prepFit() : fit());
  });
}

// Editing the bed: undo/redo, clipboard, select all, delete. Returns true when the key was used.
function docKey(e) {
  const mod = e.ctrlKey || e.metaKey, k = e.key.toLowerCase();
  if (mod && k === 'z') { e.preventDefault(); if (e.shiftKey) D.redo(); else D.undo(); return true; }
  if (mod && k === 'y') { e.preventDefault(); D.redo(); return true; }
  if (jobActive()) return false;
  if (mod && k === 'a') { e.preventDefault(); D.selectAll(); return true; }
  if (mod && k === 'd') { e.preventDefault(); D.duplicateSel(); return true; }
  if (mod && k === 'g') { e.preventDefault(); if (e.shiftKey) D.ungroup(); else D.group(); return true; }
  if (!mod && !e.altKey && state.sel.length && (k === 'r' || k === 'h' || k === 'v')) {
    e.preventDefault();
    if (k === 'r') D.rotateSel(e.shiftKey ? -90 : 90); else D.mirrorSel(k === 'h' ? 'x' : 'y');
    return true;
  }
  if (mod && k === 'c' && state.sel.length && !String(window.getSelection())) { D.copySel(); return true; }
  if (mod && k === 'x' && state.sel.length) { e.preventDefault(); D.cutSel(); return true; }
  if (mod && e.shiftKey && k === 'v') { e.preventDefault(); D.pasteInPlace(); return true; }
  if (mod && k === 'v') return true;      // the paste event decides: an SVG or image from outside, else copied objects
  if (mod && k === 'i') { e.preventDefault(); D.invertSel(); return true; }
  if (!mod && (e.key === 'Delete' || e.key === 'Backspace') && state.sel.length) { e.preventDefault(); D.removeSel(); return true; }
  if (!mod && e.key === 'Escape' && state.sel.length) { D.select([]); return true; }
  return false;
}

boot();
