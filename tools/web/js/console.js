// Console: server and plot output with levels, filter, search, copy and an auto-scroll that never fights the
// reader (scrolling up pauses it; "Jump to latest" resumes). Lines are appended in one batch per frame.
import { $, el, toast } from './ui.js';
import { prefs } from './store.js';

const RANK = { debug: 0, info: 1, warn: 2, error: 3 };
const MAX_KEEP = 5000, MAX_DOM = 1500;
let lines = [];          // all events received, by seq
let seen = new Set();
let queue = [];
let raf = 0;
let minLevel = 'info', term = '';
let clearedAt = 0;

export function initConsole() {
  const body = $('conBody');
  minLevel = prefs('conLevel') || 'info';
  $('conLevel').value = minLevel;
  $('conLevel').onchange = (e) => { minLevel = e.target.value; prefs('conLevel', minLevel); rebuild(); };
  $('conSearch').oninput = (e) => { term = e.target.value.toLowerCase(); rebuild(); };
  $('conAuto').onchange = () => { if ($('conAuto').checked) toBottom(); };
  $('conJump').onclick = () => { $('conAuto').checked = true; toBottom(); };
  $('conCopy').onclick = copy;
  $('conClear').onclick = () => { clearedAt = lines.length ? lines[lines.length - 1].seq : 0; rebuild(); };
  $('conToggle').onclick = () => {
    const pane = $('consolePane');
    pane.classList.toggle('collapsed');
    $('conToggle').setAttribute('aria-expanded', String(!pane.classList.contains('collapsed')));
    prefs('conCollapsed', pane.classList.contains('collapsed'));
  };
  if (prefs('conCollapsed')) { $('consolePane').classList.add('collapsed'); $('conToggle').setAttribute('aria-expanded', 'false'); }
  body.addEventListener('scroll', () => {
    const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 30;
    if (!atBottom && $('conAuto').checked && !body.dataset.programmatic) $('conAuto').checked = false;
    $('conJump').hidden = atBottom;
    delete body.dataset.programmatic;
  });
}

export function addEvents(evs) {
  for (const ev of evs) {
    if (seen.has(ev.seq)) continue;
    seen.add(ev.seq);
    lines.push(ev);
    queue.push(ev);
  }
  if (lines.length > MAX_KEEP) {
    const drop = lines.splice(0, lines.length - MAX_KEEP);
    for (const d of drop) seen.delete(d.seq);
  }
  if (!raf) raf = requestAnimationFrame(flush);
}

export function resetConsole() { lines = []; seen = new Set(); queue = []; rebuild(); }

const match = (ev) => ev.seq > clearedAt && RANK[ev.level] >= RANK[minLevel] && (!term || ev.text.toLowerCase().includes(term));

function row(ev) {
  const t = new Date(ev.t * 1000).toTimeString().slice(0, 8);
  return el('div', { class: `log-line ${ev.level}` }, el('span', { class: 't', text: t }),
    el('span', { class: 'lv', text: ev.level === 'error' ? 'ERR' : ev.level.toUpperCase() }), el('span', { text: ev.text }));
}

function flush() {
  raf = 0;
  const box = $('conLines');
  const frag = document.createDocumentFragment();
  for (const ev of queue) if (match(ev)) frag.append(row(ev));
  queue = [];
  box.append(frag);
  while (box.childElementCount > MAX_DOM) box.firstElementChild.remove();
  count();
  if ($('conAuto').checked) toBottom();
}

function rebuild() {
  const box = $('conLines');
  const shown = lines.filter(match).slice(-MAX_DOM);
  box.replaceChildren(...shown.map(row));
  queue = [];
  count();
  if ($('conAuto').checked) toBottom();
}

function count() {
  const n = $('conLines').childElementCount;
  $('conCount').textContent = `${n} line${n === 1 ? '' : 's'}`;
}

function toBottom() {
  const body = $('conBody');
  body.dataset.programmatic = '1';
  body.scrollTop = body.scrollHeight;
  $('conJump').hidden = true;
}

async function copy() {
  const text = lines.filter(match).map((ev) => `${new Date(ev.t * 1000).toISOString()} ${ev.level.toUpperCase()} [${ev.src}] ${ev.text}`).join('\n');
  try { await navigator.clipboard.writeText(text); toast('Copied the shown lines.'); }
  catch (e) {
    // clipboard needs a secure context; fall back to a selectable textarea
    const ta = el('textarea', { rows: 10 }); ta.value = text;
    document.body.append(ta); ta.select();
    try { document.execCommand('copy'); toast('Copied the shown lines.'); } catch (e2) { toast('Copy failed: select the text by hand.', true); }
    ta.remove();
  }
}
