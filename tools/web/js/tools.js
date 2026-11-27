// Pen tests dialog: test sheets (per pen), result recording (per pen) and the pen height test. Built from /api/tools.
import { post } from './api.js';
import { state, on, jobActive, prefs } from './store.js';
import { $, el, toast, ask, askText, schemaForm, formValues } from './ui.js';
import { openFile, refreshFiles } from './workspace.js';

export function initTools() {
  on(['tools', 'me'], render);
  on('job', () => $('toolList').querySelectorAll('button.run').forEach((b) => { b.disabled = jobActive(); }));
}

// One test at a time: pick it from the list, its settings and button show below.
const KIND = { sheet: 'Test sheets', record: 'Record results', machine: 'On the machine' };
function render() {
  const list = $('toolList');
  if (!state.me) return;
  const pens = state.me.pens || {};
  const want = prefs('tool');
  const cur = state.tools.some((t) => t.id === want) ? want : (state.tools.find((t) => t.id === 'stroke_sheet') || state.tools[0] || {}).id;
  const pick = el('select', { 'aria-label': 'Test' }, ...Object.entries(KIND).map(([k, label]) => {
    const tools = state.tools.filter((t) => t.kind === k);
    return tools.length ? el('optgroup', { label }, ...tools.map((t) => el('option', { value: t.id, text: t.title }))) : null;
  }));
  pick.value = cur || '';
  const panes = state.tools.map((t) => {
    const form = el('form', { id: `tool-${t.id}` });
    schemaForm(form, t.params, pens);
    form.onsubmit = (e) => e.preventDefault();
    const label = t.kind === 'sheet' ? 'Add to the bed' : t.kind === 'record' ? (t.params.some((p) => p.type === 'pen') ? 'Save for this pen' : 'Calculate') : 'Start';
    const btn = el('button', { class: 'primary run', type: 'button', text: label, onclick: () => run(t, form, btn) });
    btn.disabled = jobActive();
    return el('div', { class: 'tool-pane', 'data-tool': t.id, hidden: t.id !== cur },
      el('p', { class: 'hint', text: t.help }), presetRow(t, form), form, el('div', { class: 'actions' }, btn));
  });
  pick.onchange = () => { prefs('tool', pick.value); for (const p of panes) p.hidden = p.dataset.tool !== pick.value; };
  list.replaceChildren(el('label', { class: 'field' }, el('span', { text: 'Test' }), pick), ...panes);
}

// Opens the dialog on one test (the setup guide's pen height step).
export function showTool(id) {
  const d = $('testsDlg');
  if (!d.open) d.showModal();
  const sel = $('toolList').querySelector('select');
  if (sel && [...sel.options].some((o) => o.value === id)) { sel.value = id; sel.dispatchEvent(new Event('change')); }
}

// Test presets: a tool's settings saved under a name (in this browser), to run the same test again.
function presetRow(t, form) {
  if (!t.params.length) return null;
  const all = prefs('toolPresets') || {};
  const mine = all[t.id] || {};
  const sel = el('select', { 'aria-label': `Presets for ${t.title}` }, el('option', { value: '', text: Object.keys(mine).length ? 'Presets...' : 'No presets' }),
    ...Object.keys(mine).map((n) => el('option', { value: n, text: n })));
  sel.onchange = () => {
    const v = mine[sel.value];
    if (v) schemaForm(form, t.params, state.me.pens || {}, v);
  };
  const save = el('button', { type: 'button', class: 'link', text: 'Save preset', onclick: async () => {
    const name = await askText('Save the settings', `A name for these ${t.title} settings:`, sel.value || '', 'Save');
    if (!name) return;
    const a = prefs('toolPresets') || {};
    a[t.id] = { ...(a[t.id] || {}), [name.slice(0, 40)]: formValues(form, t.params) };
    prefs('toolPresets', a);
    render();
  } });
  const del = el('button', { type: 'button', class: 'link', text: 'Delete', disabled: !Object.keys(mine).length, onclick: () => {
    if (!sel.value) return;
    const a = prefs('toolPresets') || {};
    delete (a[t.id] || {})[sel.value];
    prefs('toolPresets', a);
    render();
  } });
  return el('div', { class: 'row' }, sel, save, del);
}

async function run(t, form, btn) {
  const params = formValues(form, t.params);
  if (t.params.some((p) => p.type === 'pen') && !params.pen) {
    toast('Choose the pen type first. Results are valid for that pen only.', true);
    return;
  }
  const confirm = t.kind === 'machine' ? { ok: true } : undefined;      // no start dialog
  btn.disabled = true;
  try {
    const r = await post('/api/tools/run', { tool: t.id, params, confirm });
    if (t.kind === 'sheet') {
      $('testsDlg').close();
      toast(params.pen ? `Sheet for pen ${params.pen} added. Plot it with that pen only.` : 'Sheet added.');
      await refreshFiles();
      if (state.doc && state.doc.objects.length) {
        const D = await import('./doc.js');
        D.addFile(r.file.id, { corner: true });           // into a free corner of the sheet, beside what is there
      } else openFile(r.file.id);
    } else if (t.kind === 'record' && !params.pen) {
      await ask(t.title, String(r.output || '').trim().slice(-1200), 'OK');
    } else if (t.kind === 'record') {
      toast(`Recorded for pen ${params.pen} only.`);
    } else {
      $('testsDlg').close();
      toast('Pen test running: use the toolbar buttons.');
    }
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = jobActive(); }
}
