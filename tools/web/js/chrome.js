// Page furniture shared by every view: hover tooltips, drop-down buttons, tab sets, and reveal(), which brings any
// control into view whatever holds it (a workspace, a tab, a collapsed section, a dialog).
import { prefs } from './store.js';

// ---- tooltips: every title becomes a styled tip after a short hover or on keyboard focus. The title moves to
// data-tip so the browser's own tooltip does not show as well; scripts may keep setting title at any time.
const KEY = /\s*\(((?:Ctrl|Shift|Alt)\+[^()]+|[^()\s]{1,3}|Space|Delete|Esc)\)$/;
let tip = null, timer = 0, cur = null;

function adopt(n) {
  const t = n.getAttribute('title');
  if (t !== null) {
    if (t) n.dataset.tip = t;
    n.removeAttribute('title');
    if (t && !n.getAttribute('aria-label') && !n.textContent.trim()) n.setAttribute('aria-label', t.replace(KEY, ''));
  }
  return n.dataset.tip;
}
export const tipText = (n) => n && (n.getAttribute('title') || n.dataset.tip || '');

function place(n) {
  const text = adopt(n);
  if (!text || !n.isConnected) return;
  const m = text.match(KEY);
  tip.replaceChildren(document.createTextNode(m ? text.slice(0, m.index) : text));
  if (m) { const k = document.createElement('kbd'); k.textContent = m[1]; tip.append(k); }
  tip.hidden = false;
  const r = n.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
  const below = r.bottom + 6 + h < innerHeight;
  tip.style.left = `${Math.max(6, Math.min(innerWidth - w - 6, r.left + r.width / 2 - w / 2))}px`;
  tip.style.top = `${below ? r.bottom + 6 : Math.max(6, r.top - h - 6)}px`;
}
function hide() { clearTimeout(timer); cur = null; if (tip) tip.hidden = true; }
function arm(n, delay) {
  if (n === cur) return;
  hide();
  if (!n || !adopt(n)) return;
  cur = n;
  timer = setTimeout(() => place(n), delay);
}

function initTips() {
  tip = document.createElement('div');
  tip.className = 'tip';
  tip.setAttribute('role', 'tooltip');
  tip.hidden = true;
  document.body.append(tip);
  const target = (e) => e.target && e.target.closest && e.target.closest('[title], [data-tip]');
  document.addEventListener('pointerover', (e) => { if (e.pointerType !== 'touch') arm(target(e), 450); });
  document.addEventListener('pointerout', (e) => { if (cur && !cur.contains(e.relatedTarget)) hide(); });
  document.addEventListener('focusin', (e) => { const n = target(e); if (n && n.matches(':focus-visible')) arm(n, 250); });
  document.addEventListener('focusout', hide);
  for (const ev of ['pointerdown', 'keydown', 'wheel', 'scroll']) document.addEventListener(ev, hide, true);
  // Touch has no hover: a long press shows the tip.
  let press = 0;
  document.addEventListener('touchstart', (e) => {
    const n = target(e);
    clearTimeout(press);
    if (n) press = setTimeout(() => { cur = n; place(n); }, 600);
  }, { passive: true });
  document.addEventListener('touchend', () => clearTimeout(press), { passive: true });
}

// ---- drop-down buttons: <div class="mbtn"><button class="mbtn-top">..</button><div class="mbtn-list" hidden>..</div></div>.
// The items are ordinary buttons with their own handlers; picking one closes the list.
let openList = null;
function closeMenus() {
  if (!openList) return;
  openList.hidden = true;
  openList.parentElement.querySelector('.mbtn-top')?.setAttribute('aria-expanded', 'false');
  openList = null;
}
function initMenus() {
  document.addEventListener('click', (e) => {
    const top = e.target.closest && e.target.closest('.mbtn-top');
    if (top) {
      const list = top.parentElement.querySelector('.mbtn-list');
      const was = openList === list;
      closeMenus();
      if (!was && !top.disabled) {
        list.hidden = false;
        top.setAttribute('aria-expanded', 'true');
        openList = list;
        list.classList.remove('flip');
        if (list.getBoundingClientRect().right > innerWidth - 4) list.classList.add('flip');
        list.querySelector('button:not(:disabled), select, input')?.focus({ preventScroll: true });
      }
      return;
    }
    if (openList && e.target.closest && e.target.closest('.mbtn-list') === openList && e.target.closest('button')) {
      setTimeout(closeMenus, 0);
    }
  });
  document.addEventListener('pointerdown', (e) => {
    if (openList && !openList.parentElement.contains(e.target)) closeMenus();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !openList) return;
    const top = openList.parentElement.querySelector('.mbtn-top');
    closeMenus();
    top?.focus();
    e.stopPropagation();
  }, true);
}

// ---- tab sets: <nav role="tablist" data-tabs="key"> with buttons id="tabBtn-x" aria-controls="tab-x". The shown
// tab is remembered per set.
export function showTab(name) {
  const btn = document.getElementById('tabBtn-' + name);
  if (!btn) return;
  const list = btn.closest('[role=tablist]');
  for (const b of list.querySelectorAll('[role=tab]')) {
    const on = b === btn;
    b.setAttribute('aria-selected', String(on));
    b.tabIndex = on ? 0 : -1;
    const p = document.getElementById(b.getAttribute('aria-controls'));
    if (p) p.hidden = !on;
  }
  if (list.dataset.tabs) prefs('tab.' + list.dataset.tabs, name);
  window.dispatchEvent(new CustomEvent('plotter-tab', { detail: name }));
}
const tabName = (b) => b.id.replace(/^tabBtn-/, '');
function initTabs() {
  for (const list of document.querySelectorAll('[role=tablist][data-tabs]')) {
    const tabs = [...list.querySelectorAll('[role=tab]')];
    for (const b of tabs) b.addEventListener('click', () => showTab(tabName(b)));
    list.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      const i = tabs.findIndex((b) => b.getAttribute('aria-selected') === 'true');
      const n = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
      showTab(tabName(n));
      n.focus();
    });
    const saved = prefs('tab.' + list.dataset.tabs);
    showTab(tabs.some((b) => tabName(b) === saved) ? saved : tabName(tabs[0]));
  }
}

// ---- reveal: open every container of a control (workspace, tab, section, dialog) and focus it.
export async function reveal(target, { focus = true } = {}) {
  const n = typeof target === 'string' ? document.querySelector(target) : target;
  if (!n) return null;
  const ws = n.closest('#prepWs, #plotWs');
  if (ws && ws.hidden) (await import('./ws.js')).showWs(ws.id === 'prepWs' ? 'prepare' : 'plot');
  for (let p = n; p; p = p.parentElement) {
    if (p.getAttribute && p.getAttribute('role') === 'tabpanel' && p.hidden) {
      const b = document.querySelector(`[aria-controls="${p.id}"]`);
      if (b) showTab(tabName(b));
    }
    if (p.tagName === 'DETAILS' && p !== n) p.open = true;
    if (p.tagName === 'DIALOG' && !p.open) p.showModal();
  }
  const side = n.closest('.workspace > .panel');
  if (side && matchMedia('(max-width: 900px)').matches) document.body.dataset.view = side.classList.contains('left') ? 'left' : 'right';
  if (focus) {
    n.scrollIntoView({ block: 'nearest' });
    if (n.matches('input, select, textarea, button, summary, [tabindex]')) n.focus({ preventScroll: true });
  }
  return n;
}

// Dialogs: [data-close] closes its dialog, [data-open="dialogId"] opens one, [data-reveal="selector"] closes its
// dialog and brings that control into view.
function initDialogs() {
  document.addEventListener('click', (e) => {
    const c = e.target.closest && e.target.closest('[data-close]');
    if (c) { c.closest('dialog')?.close(); return; }
    const r = e.target.closest && e.target.closest('[data-reveal]');
    if (r) { r.closest('dialog')?.close(); reveal(r.dataset.reveal); return; }
    const o = e.target.closest && e.target.closest('[data-open]');
    if (o) { const d = document.getElementById(o.dataset.open); if (d && !d.open) d.showModal(); }
  });
}

export function initChrome() {
  initTips();
  initMenus();
  initTabs();
  initDialogs();
}
