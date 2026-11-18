// Library: the drawings ready to place. A finished SVG is added here as it is; anything that needs work (images,
// 3D models, text, an SVG to clean up) goes through Prepare.
import { api, post } from './api.js';
import { state, on, jobActive } from './store.js';
import { $, el, toast, ask } from './ui.js';
import { openFile, refreshFiles } from './workspace.js';
import { showWs } from './ws.js';
import { prepareFile, prepareFromLibrary } from './prepare.js';

export function initLibrary() {
  const drop = $('drop'), input = $('fileInput');
  drop.onclick = () => input.click();
  drop.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } };
  drop.ondragover = (e) => { e.preventDefault(); e.stopPropagation(); drop.classList.add('over'); };
  drop.ondragleave = () => drop.classList.remove('over');
  drop.ondrop = (e) => {
    e.preventDefault(); e.stopPropagation(); drop.classList.remove('over');
    if (e.dataTransfer.files[0]) choose(e.dataTransfer.files[0]);
  };
  input.onchange = () => { if (input.files[0]) choose(input.files[0]); input.value = ''; };
  $('toPrepare').onclick = () => showWs('prepare');
  api('/api/fonts').then((r) => { $('fontList').replaceChildren(...r.fonts.map((f) => el('option', { value: f }))); }).catch(() => {});
  on(['files', 'current', 'job'], renderFiles);
}

// An SVG goes in as it is; everything else is handed to Prepare.
async function choose(file) {
  const ext = '.' + (file.name.split('.').pop() || '').toLowerCase();
  if (ext !== '.svg') { showWs('prepare'); prepareFile(file); return; }
  try {
    const r = await api('/api/files/upload', { method: 'POST', raw: file,
      headers: { 'X-Filename': encodeURIComponent(file.name), 'X-Params': encodeURIComponent('{}') } });
    toast(`${r.file.name} added.`);
    await refreshFiles();
    if (!jobActive()) openFile(r.file.id);
  } catch (e) { toast(e.message, true); }
}

const KIND = { prepared: 'prepared', raster: 'image', svg: 'svg', mesh: '3D', text: 'text', sheet: 'test sheet' };

function renderFiles() {
  const list = $('fileList');
  $('fileEmpty').hidden = state.files.length > 0;
  const locked = jobActive();
  list.replaceChildren(...state.files.map((f) => {
    const sel = state.current && state.current.id === f.id;
    const open = el('button', { class: 'link', text: f.name, title: 'Open on the bed', disabled: locked && !sel,
      onclick: () => openFile(f.id) });
    const tags = [f.kind && el('span', { class: 'tag', text: KIND[f.kind] || f.kind }),
      f.pen && el('span', { class: 'tag', text: `pen ${f.pen}` })];
    const prep = el('button', { class: 'icon', text: 'Prepare', title: f.recipe ? 'Reopen its recipe in Prepare' : 'Open it in Prepare',
      onclick: () => { showWs('prepare'); prepareFromLibrary(f.id); } });
    const del = el('button', { class: 'icon', text: 'Delete', 'aria-label': `Delete ${f.name}`, onclick: async () => {
      if (!(await ask('Delete drawing', `Delete ${f.name} from the library? This cannot be undone.`, 'Delete'))) return;
      try { await post('/api/files/delete', { id: f.id }); await refreshFiles(); } catch (e) { toast(e.message, true); }
    } });
    const dl = el('a', { class: 'btn icon', href: `/api/files/orig?id=${encodeURIComponent(f.id)}`, text: 'Save',
      title: 'Download the original file' });
    return el('li', { class: sel ? 'selected' : '' },
      el('img', { src: `/api/files/svg?id=${encodeURIComponent(f.id)}`, alt: '', loading: 'lazy' }),
      el('div', {}, el('div', { class: 'name' }, open), el('div', { class: 'meta' }, ...tags, ' ', f.uploaded ? f.uploaded.replace('T', ' ') : '')),
      el('div', { class: 'row' }, prep, dl, del));
  }));
}
