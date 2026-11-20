// The two workspaces: Prepare (make a drawing) and Plot (place and run it). Stop and the job state stay in the top
// bar in both, and a job keeps running whichever one is shown.
import { set, state, prefs } from './store.js';
import { $ } from './ui.js';

export function showWs(name) {
  if (name !== 'prepare' && name !== 'plot') return;
  if (state.ws === name) return;
  document.body.dataset.ws = name;
  $('prepWs').hidden = name !== 'prepare';
  $('plotWs').hidden = name !== 'plot';
  document.querySelector('.mobile-tabs').hidden = name !== 'plot';
  for (const b of document.querySelectorAll('.ws-switch button')) b.setAttribute('aria-selected', String(b.dataset.ws === name));
  prefs('ws', name);
  history.replaceState(null, '', '#' + name);
  set({ ws: name });
}

// Side panels: drag their inner edge to resize; the widths are kept per workspace. Reset puts them back.
const PANEL = { plotWs: [300, 320], prepWs: [340, 320] };
function applyPanels(id) {
  const w = prefs('panels') || {}, [l, r] = (w[id] || PANEL[id]);
  $(id).style.setProperty('--lw', `${l}px`);
  $(id).style.setProperty('--rw', `${r}px`);
}
export function resetPanels() {
  prefs('panels', null);
  for (const id of Object.keys(PANEL)) applyPanels(id);
  window.dispatchEvent(new Event('resize'));
}
function initResizers() {
  for (const id of Object.keys(PANEL)) {
    const ws = $(id);
    applyPanels(id);
    for (const side of ['l', 'r']) {
      const h = document.createElement('div');
      h.className = `resizer ${side}`;
      h.title = 'Drag to resize the panel; double-click for the default width';
      ws.append(h);
      h.addEventListener('pointerdown', (e) => {
        e.preventDefault();
        h.setPointerCapture(e.pointerId);
        h.classList.add('drag');
        const rect = ws.getBoundingClientRect();
        const move = (ev) => {
          const all = prefs('panels') || {}, cur = [...(all[id] || PANEL[id])];
          const px = side === 'l' ? ev.clientX - rect.left : rect.right - ev.clientX;
          cur[side === 'l' ? 0 : 1] = Math.round(Math.max(200, Math.min(rect.width * 0.4, px)));
          all[id] = cur;
          prefs('panels', all);
          applyPanels(id);
        };
        const up = () => { h.classList.remove('drag'); h.removeEventListener('pointermove', move); h.removeEventListener('pointerup', up); window.dispatchEvent(new Event('resize')); };
        h.addEventListener('pointermove', move);
        h.addEventListener('pointerup', up);
      });
      h.addEventListener('dblclick', () => {
        const all = prefs('panels') || {}, cur = [...(all[id] || PANEL[id])];
        cur[side === 'l' ? 0 : 1] = PANEL[id][side === 'l' ? 0 : 1];
        all[id] = cur; prefs('panels', all); applyPanels(id);
        window.dispatchEvent(new Event('resize'));
      });
    }
  }
}

export function initWs() {
  initResizers();
  for (const b of document.querySelectorAll('.ws-switch button')) b.onclick = () => showWs(b.dataset.ws);
  window.addEventListener('hashchange', () => showWs(location.hash.slice(1)));
  const want = location.hash.slice(1);
  const first = prefs('startWs') || prefs('ws');
  showWs(want === 'prepare' || want === 'plot' ? want : first === 'prepare' ? 'prepare' : 'plot');
}
