// Tools tab: test sheets (per pen), result recording (per pen) and the pen height test. Built from /api/tools.
import { post } from './api.js';
import { state, on, jobActive } from './store.js';
import { $, el, toast, ask, schemaForm, formValues } from './ui.js';
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
    const label = t.kind === 'sheet' ? 'Make sheet' : t.kind === 'record' ? 'Record for this pen' : 'Start';
    const btn = el('button', { class: 'primary run', type: 'button', text: label, onclick: () => run(t, form, btn) });
    return el('details', { open: t.kind === 'sheet' && t.id === 'stroke_sheet' },
      el('summary', { text: t.title }), el('p', { class: 'hint', text: t.help }), form, el('div', { class: 'row' }, btn));
  }));
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
      toast(`Sheet for pen ${params.pen} added. Plot it with that pen only.`);
      await refreshFiles();
      openFile(r.file.id);
    } else if (t.kind === 'record') {
      toast(`Recorded for pen ${params.pen} only.`);
    } else {
      toast(t.id === 'home' ? 'Homing: watch the carriage. Stop is in the top bar.' : 'Pen test running: use the buttons under Run.');
    }
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = jobActive(); }
}
