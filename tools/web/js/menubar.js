// The menu bar (File, Edit, Arrange, Tools, Machine, Help), Preferences, simple mode, rebound keys and "What's
// this?" help. Menu items press the controls that already exist, so a command behaves the same from either place
// (including being disabled while a job runs).
import { state, prefs } from './store.js';
import { $, el, toast } from './ui.js';
import { SHORTCUTS } from './shortcuts.js';
import { showWs } from './ws.js';

const press = (sel, ws = 'plot') => () => {
  if (ws && state.ws !== ws) showWs(ws);
  const n = document.querySelector(sel);
  if (!n) return;
  if (n.disabled) { toast('Not available right now (a job may be running, or nothing is selected).', true); return; }
  n.closest('details')?.setAttribute('open', '');
  if (n.tagName === 'INPUT' && n.type !== 'checkbox') n.focus(); else n.click();
};
const tab = (name, then) => () => { showWs('plot'); $('tabBtn-' + name).click(); if (then) then(); };
const focus = (sel, t) => tab(t, () => { const n = document.querySelector(sel); n?.closest('details')?.setAttribute('open', ''); n?.scrollIntoView({ block: 'center' }); n?.focus(); });

const MENUS = [
  ['File', [
    ['Upload a drawing...', press('#fileInput')],
    ['Prepare an image or model', () => showWs('prepare')],
    null,
    ['Save the bed as a project', focus('#projName', 'library')],
    ['Projects...', focus('#projList', 'library')],
    ['Export the plan as PNG', press('#exportPng')],
    ['Export the bed', press('#exportBtn')],
    null,
    ['Back up settings', press('#bkDownload')],
    ['Restore settings...', press('#bkRestore')],
  ]],
  ['Edit', [
    ['Undo', press('#undoBtn'), 'Ctrl+Z'], ['Redo', press('#redoBtn'), 'Ctrl+Shift+Z'], null,
    ['Duplicate', press('#xfBox [data-act=dup]'), 'Ctrl+D'], ['Remove from the bed', press('#xfBox [data-act=del]'), 'Del'], null,
    ['Preferences...', () => openPrefs()],
    ['Reset the panel layout', () => import('./ws.js').then((m) => m.resetPanels())],
  ]],
  ['Arrange', [
    ['Group', press('#xfBox [data-act=group]'), 'Ctrl+G'], ['Ungroup', press('#xfBox [data-act=ungroup]'), 'Ctrl+Shift+G'],
    ['Lock or unlock', press('#xfBox [data-act=lock]')], null,
    ['Turn right', press('#xfBox [data-act=rot90]'), 'R'], ['Turn left', press('#xfBox [data-act=rot-90]'), 'Shift+R'],
    ['Mirror left to right', press('#xfBox [data-act=mirx]'), 'H'], ['Mirror top to bottom', press('#xfBox [data-act=miry]'), 'V'], null,
    ['Align left', press('#xfBox [data-act=left]')], ['Centre horizontally', press('#xfBox [data-act=hcenter]')],
    ['Align right', press('#xfBox [data-act=right]')], ['Align top', press('#xfBox [data-act=top]')],
    ['Centre vertically', press('#xfBox [data-act=vcenter]')], ['Align bottom', press('#xfBox [data-act=bottom]')], null,
    ['Fit in the canvas', press('#xfBox [data-act=fit]')], ['Array...', press('#xfBox [data-act=array]')],
  ]],
  ['Tools', [
    ['Text...', press('#textTool')], ['Shape...', press('#shapeTool')], null,
    ['Test sheets and calibration', tab('tools')], ['Live writing', focus('#liveSection', 'tools')], null,
    ['Preview', press('#previewBtn'), 'P'], ['Simulate', press('#simBtn')], ['Trace the frame', press('#traceBtn')],
  ]],
  ['Machine', [
    ['Machine control', tab('machine')], ['Check the plotter', press('#hwCheck', null)],
    ['Doctor (what is wrong?)', press('#hwDoctor', null)], null,
    ['Pens', focus('#penList', 'machine')], ['Calibration (steps/mm)', focus('#calLen', 'machine')],
    ['Paper corner and size', focus('#paperForm', 'machine')], ['Saved positions', focus('#posList', 'machine')],
    ['Macros', focus('#macroList', 'machine')],
  ]],
  ['Help', [
    ['How plotting works, shortcuts', () => $('helpDlg').showModal(), '?'],
    ["What's this? (then click a control)", () => whatsThis(), 'Shift+F1'],
    ['Setup guide (first run)', () => openWizard()],
    ['Download the log', () => { location.href = '/api/log/download'; }],
    ['Diagnostic bundle', () => { location.href = '/api/diagnostics'; }],
  ]],
];

function buildMenuBar() {
  const bar = $('menuBar');
  let open = null;
  const close = () => { if (open) { open.btn.setAttribute('aria-expanded', 'false'); open.list.hidden = true; open = null; } };
  const show = (m) => { close(); m.btn.setAttribute('aria-expanded', 'true'); m.list.hidden = false; open = m; };
  const menus = MENUS.map(([name, items]) => {
    const btn = el('button', { type: 'button', class: 'menu-top', 'aria-haspopup': 'true', 'aria-expanded': 'false', text: name });
    const list = el('div', { class: 'menu-list', role: 'menu', hidden: true }, ...items.map((it) => it ? el('button', {
      type: 'button', role: 'menuitem', onclick: () => { close(); it[1](); } }, el('span', { text: it[0] }), it[2] ? el('kbd', { text: it[2] }) : null) : el('hr')));
    const m = { btn, list };
    btn.onclick = (e) => { e.stopPropagation(); if (open === m) close(); else show(m); };
    btn.onpointerenter = () => { if (open && open !== m) show(m); };
    return m;
  });
  bar.replaceChildren(...MENUS.map((_, i) => el('div', { class: 'menu' }, menus[i].btn, menus[i].list)));
  document.addEventListener('pointerdown', (e) => { if (open && !bar.contains(e.target)) close(); });
  bar.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && open) { const b = open.btn; close(); b.focus(); e.stopPropagation(); }
    if ((e.key === 'ArrowDown') && open) { e.preventDefault(); const items = [...open.list.querySelectorAll('button:not([disabled])')]; const i = items.indexOf(document.activeElement); (items[i + 1] || items[0]).focus(); }
    if ((e.key === 'ArrowUp') && open) { e.preventDefault(); const items = [...open.list.querySelectorAll('button:not([disabled])')]; const i = items.indexOf(document.activeElement); (items[i - 1] || items[items.length - 1]).focus(); }
  });
}

// ---- simple mode: these controls are for people who know they want them
const ADVANCED = ['#placeForm [name=dedupe]', '#placeForm [name=by_object]', '#placeForm [name=simplify]',
  '#placeForm [name=corner_starts]', '#placeForm [name=inner_first]', '#placeForm [name=overlaps]',
  '#placeForm [name=approach]', '#placeForm [name=sweep]', '#placeForm [name=trace_repeat]', '#placeForm [name=keep_motors]',
  '.mc-more', '#rawForm', '#lineTools', '#bkDownload', '#hwDoctor'];
function applySimple(on) {
  document.body.classList.toggle('simple', on);
  for (const s of ADVANCED) {
    for (const n of document.querySelectorAll(s)) (n.closest('label, .field-row, details') || n).classList.add('adv');
  }
}

// ---- rebinding: a custom key stands in for a built-in one, which then does nothing
const comboOf = (e) => [e.ctrlKey || e.metaKey ? 'Ctrl' : '', e.altKey ? 'Alt' : '', e.shiftKey && e.key.length > 1 ? 'Shift' : '',
  e.key.length === 1 ? e.key.toUpperCase() : e.key].filter(Boolean).join('+');
const binds = () => prefs('keys') || {};            // {built-in combo: custom combo}
function initRebind() {
  window.addEventListener('keydown', (e) => {
    if (e.__rebound) return;
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
    const b = binds(), c = comboOf(e);
    if (c === 'Shift+F1') { e.preventDefault(); whatsThis(); return; }
    const from = Object.keys(b).find((k) => b[k] === c);
    if (from) {
      e.preventDefault(); e.stopImmediatePropagation();
      const parts = from.split('+'), key = parts[parts.length - 1];
      const ev = new KeyboardEvent('keydown', { key: key.length === 1 && !parts.includes('Shift') ? key.toLowerCase() : key, bubbles: true, cancelable: true,
        ctrlKey: parts.includes('Ctrl'), shiftKey: parts.includes('Shift'), altKey: parts.includes('Alt') });
      ev.__rebound = true;
      (t || document).dispatchEvent(ev);
    } else if (b[c]) { e.preventDefault(); e.stopImmediatePropagation(); }        // moved away: the old key is free
  }, true);
}

// ---- "What's this?": the next click on a control shows its explanation instead of doing anything
function whatsThis() {
  document.body.classList.add('whats-this');
  toast("Click any control to see what it does. Esc cancels.");
  const done = () => { document.body.classList.remove('whats-this'); document.removeEventListener('click', onClick, true); document.removeEventListener('keydown', onKey, true); };
  const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); done(); } };
  const onClick = (e) => {
    e.preventDefault(); e.stopPropagation();
    done();
    // a disabled control gets no clicks (CSS lets them through to its parent): find it under the pointer
    const off = [...document.querySelectorAll('[disabled]')].find((x) => {
      const r = x.getBoundingClientRect();
      return r.width && e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
    });
    const n = (off || e.target).closest('[title], label, button, summary, select, input, h2, h3');
    const lab = n && (n.closest('label') || n);
    const text = (n && n.getAttribute('title')) || (lab && lab.getAttribute('title')) ||
      (lab && lab.parentElement && lab.parentElement.querySelector('.hint')?.textContent);
    const name = (lab && (lab.querySelector('span')?.textContent || lab.textContent) || '').trim().slice(0, 80);
    import('./ui.js').then(({ ask }) => ask(name || 'This control', text || 'No help text for this one yet; see Help for the overview.', 'OK'));
  };
  setTimeout(() => { document.addEventListener('click', onClick, true); document.addEventListener('keydown', onKey, true); }, 0);
}

// ---- Preferences
function openPrefs() {
  const d = $('prefsDlg'), v = { grid: 10, snapGrid: false, dir: false, nudge: 1, snapPx: 7, lockGuides: false, units: 'mm', ...(prefs('view') || {}) };
  const f = $('prefsForm');
  f.grid.value = String(v.grid); f.snapGrid.checked = v.snapGrid; f.dir.checked = v.dir; f.nudge.value = String(v.nudge);
  f.snapPx.value = String(v.snapPx); f.lockGuides.checked = v.lockGuides; f.units.value = v.units || 'mm';
  f.simple.checked = !!prefs('simple'); f.startWs.value = prefs('startWs') || '';
  renderKeys();
  d.showModal();
}

function renderKeys() {
  const b = binds();
  const rows = SHORTCUTS.flatMap((r) => (r.keys || []).map(([k]) => ({ k: k.join('+'), what: r.what })))
    .filter((r, i, a) => a.findIndex((x) => x.k === r.k) === i && !['1', '2', '?', 'Esc'].includes(r.k));
  $('prefsKeys').replaceChildren(...rows.map((r) => {
    const cur = b[r.k];
    const btn = el('button', { type: 'button', class: 'icon', text: cur || r.k, title: 'Click, then press the new key' });
    btn.onclick = () => {
      btn.textContent = 'press a key...';
      const grab = (e) => {
        if (['Control', 'Shift', 'Alt', 'Meta'].includes(e.key)) return;
        e.preventDefault(); e.stopImmediatePropagation();
        window.removeEventListener('keydown', grab, true);
        if (e.key === 'Escape') { renderKeys(); return; }
        const c = comboOf(e), all = binds();
        const clash = Object.entries(all).find(([k, x]) => x === c && k !== r.k) ||
          (rows.some((x) => x.k === c && x.k !== r.k && !all[x.k]) && [c]);
        if (clash) { toast(`${c} is already used.`, true); renderKeys(); return; }
        if (c === r.k) delete all[r.k]; else all[r.k] = c;
        prefs('keys', all); renderKeys();
      };
      window.addEventListener('keydown', grab, true);
    };
    return el('tr', {}, el('td', { text: r.what }), el('td', {}, btn,
      cur ? el('button', { type: 'button', class: 'link', text: 'reset', onclick: () => { const a = binds(); delete a[r.k]; prefs('keys', a); renderKeys(); } }) : null));
  }));
}

function initPrefs() {
  const f = $('prefsForm');
  f.onsubmit = (e) => {
    e.preventDefault();
    prefs('view', { ...(prefs('view') || {}), grid: Number(f.grid.value), snapGrid: f.snapGrid.checked, dir: f.dir.checked, nudge: Number(f.nudge.value),
      snapPx: Number(f.snapPx.value), lockGuides: f.lockGuides.checked, units: f.units.value });
    prefs('simple', f.simple.checked);
    prefs('startWs', f.startWs.value || null);
    applySimple(f.simple.checked);
    $('prefsDlg').close();
    toast('Preferences saved. Grid and snapping apply now; the View menu shows the same settings.');
    window.dispatchEvent(new Event('plotter-prefs'));
  };
  $('prefsCancel').onclick = () => $('prefsDlg').close();
  $('prefsKeysReset').onclick = () => { prefs('keys', {}); renderKeys(); };
}

// ---- first-run setup guide: each step explains, and its button takes you to the control that does it
const WIZ = [
  ['Connect', 'Plug the plotter in by USB and switch it on. Check asks the board for its firmware and shows whether the server can talk to it.',
    'Check the plotter', async () => {
      const { api } = await import('./api.js');
      try {
        const r = await api('/api/plotter');
        return r.ok ? `Found${r.port ? ' on ' + r.port : ''}: ${r.firmware || r.message || 'the board answers'}.` : `Not found: ${r.message || 'no answer'}. Check the cable and power.`;
      } catch (e) { return e.message; }
    }],
  ['Park at S', "The plotter has no switches, so it knows where it is only from where you leave it. Switch the motors off (Machine, Release), push the carriage by hand to the paper's top-left corner, pen up. That point is S; every run starts and ends there.",
    'Open Machine control', () => { closeWiz(); tab('machine')(); }],
  ['Calibrate the distances', 'Steps per mm decide whether 100 mm on screen is 100 mm on paper. Under Machine, Calibration: draw a test line, measure it with a ruler, type the length; repeat for Y. Do it once per machine, and again if a belt is changed.',
    'Open Calibration', () => { closeWiz(); focus('#calLen', 'machine')(); }],
  ['Pen height', "Pen up must clear the paper and pen down must touch it lightly. Tools, Pen height test moves only the pen; a too-low pen-down can gouge the paper, so set it by hand with a pen mounted.",
    'Open the pen height test', () => { closeWiz(); tab('tools')(); setTimeout(() => {
      const d = [...document.querySelectorAll('#toolList details')].find((x) => /pen height/i.test(x.textContent));
      if (d) { d.open = true; d.scrollIntoView({ block: 'center' }); }
    }, 100); }],
  ['Paper and a first plot', 'Under Machine, Paper, set the sheet (or measure it with the pen). Then Library, upload a drawing, Preview, Simulate to watch it on the screen, and Plot.',
    'Open Paper', () => { closeWiz(); focus('#paperForm', 'machine')(); }],
];
let wizAt = 0;
function closeWiz() { $('wizDlg').close(); prefs('wizardSeen', true); }
function renderWiz() {
  const [head, text, act] = WIZ[wizAt];
  $('wizStepNo').textContent = `Step ${wizAt + 1} of ${WIZ.length}`;
  $('wizHead').textContent = head; $('wizText').textContent = text; $('wizAct').textContent = act;
  $('wizResult').textContent = '';
  $('wizBack').disabled = wizAt === 0;
  $('wizNext').textContent = wizAt === WIZ.length - 1 ? 'Done' : 'Next';
}
export function openWizard(at = 0) {
  wizAt = at; renderWiz();
  if (!$('wizDlg').open) $('wizDlg').showModal();
}
function initWizard() {
  $('wizAct').onclick = async () => { const r = await WIZ[wizAt][3](); if (typeof r === 'string') $('wizResult').textContent = r; };
  $('wizBack').onclick = () => { wizAt = Math.max(0, wizAt - 1); renderWiz(); };
  $('wizNext').onclick = () => { if (wizAt === WIZ.length - 1) closeWiz(); else { wizAt++; renderWiz(); } };
  $('wizClose').onclick = closeWiz;
}
// opens by itself once per browser on a machine that has never been calibrated
export function maybeWizard() {
  const c = state.me && state.me.machine_cfg;
  if (c && !c.measured && !prefs('wizardSeen')) openWizard();
}

export function initMenuBar() {
  initWizard();
  buildMenuBar();
  initPrefs();
  initRebind();
  applySimple(!!prefs('simple'));
}
export { openPrefs, whatsThis };
