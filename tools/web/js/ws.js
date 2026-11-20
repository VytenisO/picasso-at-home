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

export function initWs() {
  for (const b of document.querySelectorAll('.ws-switch button')) b.onclick = () => showWs(b.dataset.ws);
  window.addEventListener('hashchange', () => showWs(location.hash.slice(1)));
  const want = location.hash.slice(1);
  const first = prefs('startWs') || prefs('ws');
  showWs(want === 'prepare' || want === 'plot' ? want : first === 'prepare' ? 'prepare' : 'plot');
}
