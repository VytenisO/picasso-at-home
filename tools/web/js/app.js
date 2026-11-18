// Boot, login, live events, tabs, theme and keyboard shortcuts. The server owns the job state; this page is a view
// of it and can be reloaded or opened on another device at any time.
import { api, post, connectEvents, setUnauthorizedHandler } from './api.js';
import { state, set, on, jobActive, prefs } from './store.js';
import { $, toast } from './ui.js';
import { initBed, fit, zoomIn, zoomOut, themeChanged, toggleTravel } from './bed.js';
import { initDoc } from './doc.js';
import { initWorkspace, openFile, refreshFiles, runPreview } from './workspace.js';
import * as D from './doc.js';
import { initRun, pause, resume, stop } from './run.js';
import { initLibrary } from './library.js';
import { initTools } from './tools.js';
import { initLive } from './live.js';
import { initConsole, addEvents, resetConsole } from './console.js';
import { initWs, showWs } from './ws.js';
import { initPrepare, prepFit, prepZoom, prepKey } from './prepare.js';

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
  initBed();
  initDoc();
  initWorkspace();
  initRun();
  initLibrary();
  initTools();
  initLive();
  initConsole();
  initTabs();
  initWs();
  initPrepare();
  initTheme();
  initKeys();
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
  set({ job: st.job, position: st.position, lastEventAt: Date.now() });
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
    case 'pos': set({ pos: { x: ev.x, y: ev.y, pen: ev.pen } }); break;
    case 'files': refreshFiles(); break;
    case 'doc': D.onDocEvent(ev); break;
    case 'reset': syncState().catch((e) => toast(e.message, true)); break;
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

// ------------------------------------------------------------- tabs, view
function initTabs() {
  const tabs = ['library', 'layers', 'tools'];
  const show = (name) => {
    for (const t of tabs) {
      $('tab-' + t).hidden = t !== name;
      $('tabBtn-' + t).setAttribute('aria-selected', String(t === name));
    }
    prefs('tab', name);
  };
  for (const t of tabs) $('tabBtn-' + t).onclick = () => show(t);
  $('leftPanel').querySelector('.tabs').addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    const i = tabs.findIndex((t) => !$('tab-' + t).hidden);
    const n = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
    show(n); $('tabBtn-' + n).focus();
  });
  show(prefs('tab') || 'library');
  document.querySelectorAll('.mobile-tabs button').forEach((b) => {
    b.onclick = () => {
      const v = b.dataset.view;
      document.body.dataset.view = v;
      document.querySelectorAll('.mobile-tabs button').forEach((x) => x.setAttribute('aria-selected', String(x === b)));
      if (v === 'library' || v === 'layers' || v === 'tools') show(v);
      if (v === 'bed') requestAnimationFrame(fit);
    };
  });
}

function initTheme() {
  const order = ['system', 'light', 'dark'];
  const apply = (t) => {
    if (t === 'system') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
    $('themeBtn').textContent = 'Theme: ' + t;
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
  if (mod && k === 'c' && state.sel.length && !String(window.getSelection())) { D.copySel(); return true; }
  if (mod && k === 'x' && state.sel.length) { e.preventDefault(); D.cutSel(); return true; }
  if (mod && k === 'v') { e.preventDefault(); D.paste(); return true; }
  if (!mod && (e.key === 'Delete' || e.key === 'Backspace') && state.sel.length) { e.preventDefault(); D.removeSel(); return true; }
  if (!mod && e.key === 'Escape' && state.sel.length) { D.select([]); return true; }
  return false;
}

boot();
