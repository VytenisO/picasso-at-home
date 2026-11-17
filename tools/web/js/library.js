// Library: upload with a converter form built from the server's schema, conversion preview, file list, text art.
import { api, post } from './api.js';
import { state, on, jobActive } from './store.js';
import { $, el, toast, ask, schemaForm, formValues } from './ui.js';
import { openFile, refreshFiles } from './workspace.js';

let pending = null;   // {file, conv}

export function initLibrary() {
  const drop = $('drop'), input = $('fileInput');
  drop.onclick = () => input.click();
  drop.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } };
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add('over'); };
  drop.ondragleave = () => drop.classList.remove('over');
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove('over'); if (e.dataTransfer.files[0]) choose(e.dataTransfer.files[0]); };
  input.onchange = () => { if (input.files[0]) choose(input.files[0]); input.value = ''; };
  $('convCancel').onclick = () => { pending = null; $('convBox').hidden = true; };
  $('convPreviewBtn').onclick = convPreview;
  let t = null;
  $('convForm').addEventListener('change', () => {
    if (pending && pending.conv.previewable) { clearTimeout(t); t = setTimeout(convPreview, 400); }
  });
  $('convAddBtn').onclick = convAdd;
  $('taPreview').onclick = () => textArt(false);
  $('taAdd').onclick = () => textArt(true);
  $('tpPreview').onclick = () => textPage(false);
  $('tpAdd').onclick = () => textPage(true);
  api('/api/fonts').then((r) => { $('fontList').replaceChildren(...r.fonts.map((f) => el('option', { value: f }))); }).catch(() => {});
  on(['files', 'current', 'job'], renderFiles);
  on('converters', () => {
    const exts = state.converters.flatMap((c) => c.exts);
    $('dropTypes').textContent = exts.join(' ') + ' (max 12 MB)';
    input.accept = exts.join(',');
  });
}

function choose(file) {
  const ext = '.' + (file.name.split('.').pop() || '').toLowerCase();
  const conv = state.converters.find((c) => c.exts.includes(ext));
  if (!conv) { toast(`${file.name}: this type cannot be plotted. Use ${state.converters.flatMap((c) => c.exts).join(', ')}.`, true); return; }
  pending = { file, conv };
  $('convBox').hidden = false;
  $('convTitle').textContent = `${file.name}: ${conv.title}`;
  $('convHelp').textContent = conv.help || '';
  schemaForm($('convForm'), conv.params, state.me.pens);
  $('convImg').hidden = true;
  $('convMask').hidden = true;
  $('convStats').textContent = '';
  $('convPreviewBtn').hidden = !conv.previewable;
  if (conv.previewable) convPreview();
}

function headers() {
  const params = formValues($('convForm'), pending.conv.params);
  return { 'X-Filename': encodeURIComponent(pending.file.name), 'X-Params': encodeURIComponent(JSON.stringify(params)) };
}

let convGen = 0;
async function convPreview() {
  if (!pending) return;
  const gen = ++convGen, mine = pending;
  $('convPreviewBtn').disabled = true;
  $('convStats').textContent = 'Converting...';
  try {
    const r = await api('/api/files/convert_preview', { method: 'POST', raw: pending.file, headers: headers() });
    if (gen !== convGen || pending !== mine) return;     // a newer preview or another file took over
    const img = $('convImg');
    img.src = URL.createObjectURL(new Blob([r.svg], { type: 'image/svg+xml' }));
    img.hidden = false;
    const mask = $('convMask');
    mask.hidden = !r.mask;
    if (r.mask) mask.src = 'data:image/png;base64,' + r.mask;
    const s = r.stats || {};
    $('convStats').textContent = [s.paths !== undefined ? `${s.paths} paths` : '', s.points !== undefined ? `${s.points} points` : '',
      s.width_mm ? `${Number(s.width_mm).toFixed(0)} x ${Number(s.height_mm || 0).toFixed(0)} mm` : ''].filter(Boolean).join(', ');
  } catch (e) { $('convStats').textContent = ''; toast(e.message, true); }
  finally { $('convPreviewBtn').disabled = false; }
}

async function convAdd() {
  if (!pending) return;
  $('convAddBtn').disabled = true;
  try {
    const r = await api('/api/files/upload', { method: 'POST', raw: pending.file, headers: headers() });
    pending = null;
    $('convBox').hidden = true;
    toast(`${r.file.name} added.`);
    await refreshFiles();
    if (!jobActive()) openFile(r.file.id);
  } catch (e) { toast(e.message, true); }
  finally { $('convAddBtn').disabled = false; }
}

async function textPage(add) {
  const f = $('tpForm');
  const body = { text: f.text.value, font: f.font.value, xheight: Number(f.xheight.value), bold: f.bold.checked,
    italic: f.italic.checked, add };
  try {
    const r = await post('/api/text_page', body);
    const info = $('tpInfo');
    const s = r.stats;
    info.textContent = `${s.lines} line(s), ${s.width_mm} x ${s.height_mm} mm` + (s.missing ? `. Not in this font: ${s.missing}` : '');
    info.hidden = false;
    if (add) { toast('Text added.'); await refreshFiles(); if (!jobActive()) openFile(r.file.id); return; }
    const img = $('tpImg');
    img.src = URL.createObjectURL(new Blob([r.svg], { type: 'image/svg+xml' }));
    img.hidden = false;
  } catch (e) { toast(e.message, true); }
}

async function textArt(add) {
  const f = $('taForm');
  const body = { text: f.text.value, size: Number(f.size.value), bend: Number(f.bend.value), effect: f.effect.value,
    font: f.font.value, bold: f.bold.checked, add };
  try {
    const r = await post('/api/text_art', body);
    if (add) { toast('Text added.'); await refreshFiles(); if (!jobActive()) openFile(r.file.id); return; }
    const img = $('taImg');
    img.src = URL.createObjectURL(new Blob([r.svg], { type: 'image/svg+xml' }));
    img.hidden = false;
  } catch (e) { toast(e.message, true); }
}

function renderFiles() {
  const list = $('fileList');
  $('fileEmpty').hidden = state.files.length > 0;
  const locked = jobActive();
  list.replaceChildren(...state.files.map((f) => {
    const sel = state.current && state.current.id === f.id;
    const open = el('button', { class: 'link', text: f.name, title: 'Open on the bed', disabled: locked && !sel,
      onclick: () => openFile(f.id) });
    const tags = [f.kind && el('span', { class: 'tag', text: f.kind }), f.pen && el('span', { class: 'tag', text: `pen ${f.pen}` })];
    const del = el('button', { class: 'icon', text: 'Delete', 'aria-label': `Delete ${f.name}`, onclick: async () => {
      if (!(await ask('Delete drawing', `Delete ${f.name} from the library? This cannot be undone.`, 'Delete'))) return;
      try { await post('/api/files/delete', { id: f.id }); await refreshFiles(); } catch (e) { toast(e.message, true); }
    } });
    const dl = el('a', { class: 'btn icon', href: `/api/files/orig?id=${encodeURIComponent(f.id)}`, text: 'Save',
      title: 'Download the original file' });
    return el('li', { class: sel ? 'selected' : '' },
      el('img', { src: `/api/files/svg?id=${encodeURIComponent(f.id)}`, alt: '', loading: 'lazy' }),
      el('div', {}, el('div', { class: 'name' }, open), el('div', { class: 'meta' }, ...tags, ' ', f.uploaded ? f.uploaded.replace('T', ' ') : '')),
      el('div', { class: 'row' }, dl, del));
  }));
}
