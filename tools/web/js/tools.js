// Tools tab: test sheets (per pen), result recording (per pen) and the pen height test. Built from /api/tools.
import { post } from './api.js';
import { state, on, jobActive, prefs } from './store.js';
import { $, el, toast, ask, askText, schemaForm, formValues } from './ui.js';
import { openFile, refreshFiles } from './workspace.js';

export function initTools() {
  on(['tools', 'me'], render);
  on('job', () => $('toolList').querySelectorAll('button.run').forEach((b) => { b.disabled = jobActive(); }));
}

function render() {
  const list = $('toolList');
  if (!state.me) return;
  const pens = state.me.pens || {};
  list.replaceChildren(...state.tools.map((t) => {
    const form = el('form', { id: `tool-${t.id}` });
    schemaForm(form, t.params, pens);
    form.onsubmit = (e) => e.preventDefault();
    const label = t.kind === 'sheet' ? 'Make sheet' : t.kind === 'record' ? (t.params.some((p) => p.type === 'pen') ? 'Record for this pen' : 'Work it out') : 'Start';
    const btn = el('button', { class: 'primary run', type: 'button', text: label, onclick: () => run(t, form, btn) });
    return el('details', { open: t.kind === 'sheet' && t.id === 'stroke_sheet' },
      el('summary', { text: t.title }), el('p', { class: 'hint', text: t.help }), presetRow(t, form), form, el('div', { class: 'row' }, btn));
  }));
}

// Test presets: a tool's settings saved under a name (in this browser), to run the same test again.
function presetRow(t, form) {
  if (!t.params.length) return null;
  const all = prefs('toolPresets') || {};
  const mine = all[t.id] || {};
  const sel = el('select', { 'aria-label': `Presets for ${t.title}` }, el('option', { value: '', text: Object.keys(mine).length ? 'Presets...' : 'No presets yet' }),
    ...Object.keys(mine).map((n) => el('option', { value: n, text: n })));
  sel.onchange = () => {
    const v = mine[sel.value];
    if (v) schemaForm(form, t.params, state.me.pens || {}, v);
  };
  const save = el('button', { type: 'button', class: 'link', text: 'Save as preset', onclick: async () => {
    const name = await askText('Save the settings', `A name for these ${t.title} settings:`, sel.value || '', 'Save');
    if (!name) return;
    const a = prefs('toolPresets') || {};
    a[t.id] = { ...(a[t.id] || {}), [name.slice(0, 40)]: formValues(form, t.params) };
    prefs('toolPresets', a);
    render();
  } });
  const del = el('button', { type: 'button', class: 'link', text: 'Delete preset', disabled: !Object.keys(mine).length, onclick: () => {
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
  let confirm;
  if (t.kind === 'machine') {
    const ok = await ask(t.title, t.confirm + (state.me.sim ? ' (Simulator: nothing moves.)' : ''), 'Start');
    if (!ok) return;
    confirm = { ok: true };
  }
  btn.disabled = true;
  try {
    const r = await post('/api/tools/run', { tool: t.id, params, confirm });
    if (t.kind === 'sheet') {
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
      toast('Pen test running: use the buttons under Run.');
    }
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = jobActive(); }
}
