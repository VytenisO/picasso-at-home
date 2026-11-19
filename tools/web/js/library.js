// Library: the drawings ready to place. A finished SVG is added here as it is; anything that needs work (images,
// 3D models, text, an SVG to clean up) goes through Prepare.
import { api, post } from './api.js';
import { state, on, jobActive, prefs } from './store.js';
import { $, el, toast, ask, askText } from './ui.js';
import { openFile, refreshFiles } from './workspace.js';
import { addFile } from './doc.js';
import * as D from './doc.js';
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
  on(['files', 'doc', 'job'], renderFiles);
  on('files', loadTrash);
  const lp = { q: '', kind: '', sort: 'new', ...(prefs('libView') || {}) };
  $('libSearch').value = lp.q; $('libSort').value = lp.sort;
  const keep = () => { prefs('libView', { q: $('libSearch').value, kind: $('libKind').value, sort: $('libSort').value }); renderFiles(); };
  $('libSearch').oninput = keep; $('libKind').onchange = keep; $('libSort').onchange = keep;
  $('libKind').dataset.want = lp.kind;
  $('replaceInput').onchange = async () => {
    const f = $('replaceInput').files[0], id = $('replaceInput').dataset.id;
    $('replaceInput').value = '';
    if (!f || !id) return;
    try {
      await api(`/api/files/replace?id=${encodeURIComponent(id)}`, { method: 'POST', raw: f });
      toast('Drawing replaced. Objects on the bed keep their place and width.');
      await refreshFiles();
    } catch (e) { toast(e.message, true); }
  };
  $('trashEmpty').onclick = async () => {
    if (!(await ask('Empty the trash', 'Delete every drawing in the trash for good? This cannot be undone.', 'Delete for good'))) return;
    try { await post('/api/files/purge', { all: true }); loadTrash(); } catch (e) { toast(e.message, true); }
  };
  $('trashBox').addEventListener('toggle', loadTrash);
  document.addEventListener('paste', onPaste);
  loadTrash();
}

// Ctrl+V with an SVG (as text) or an image on the system clipboard: SVG into the library and onto the bed, an image
// to Prepare. Anything else is left to the bed's own paste of copied objects.
export function onPaste(e) {
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
  if (document.querySelector('dialog[open]')) return;
  const dt = e.clipboardData;
  if (!dt) return;
  const file = [...dt.files].find((f) => /^image\//.test(f.type));
  const text = dt.getData('text/plain') || '';
  if (file) {
    e.preventDefault();
    const name = file.name && file.name !== 'image.png' ? file.name : `pasted-${Date.now() % 100000}.${(file.type.split('/')[1] || 'png').replace('svg+xml', 'svg')}`;
    const f = new File([file], name, { type: file.type });
    if (/svg/.test(file.type)) choose(f); else { showWs('prepare'); prepareFile(f); }
    return;
  }
  if (/^\s*(<\?xml[^>]*>\s*)?(<!--[\s\S]*?-->\s*)*<svg[\s>]/i.test(text)) {
    e.preventDefault();
    choose(new File([text], `pasted-${Date.now() % 100000}.svg`, { type: 'image/svg+xml' }));
    return;
  }
  if (state.ws === 'plot') { e.preventDefault(); D_paste(); }
}

let D_paste = () => {};
export function setObjectPaste(fn) { D_paste = fn; }

async function loadTrash() {
  let r;
  try { r = await api('/api/files/trash'); } catch (e) { return; }
  const files = r.files || [];
  $('trashCount').textContent = files.length ? `(${files.length})` : '(empty)';
  $('trashEmpty').disabled = !files.length;
  if (!$('trashBox').open) return;
  $('trashList').replaceChildren(...files.map((f) => el('li', { class: 'trashed' },
    el('img', { src: `/api/files/svg?id=${encodeURIComponent(f.id)}`, alt: '', loading: 'lazy' }),
    el('div', {}, el('div', { class: 'name', text: f.name }), el('div', { class: 'meta', text: `deleted ${String(f.trashed).replace('T', ' ')}` })),
    el('div', { class: 'row' },
      el('button', { class: 'icon', text: 'Restore', onclick: async () => {
        try { await post('/api/files/restore', { id: f.id }); await refreshFiles(); toast(`${f.name} is back in the library.`); } catch (e) { toast(e.message, true); }
      } }),
      el('button', { class: 'icon danger-text', text: 'Delete for good', onclick: async () => {
        if (!(await ask('Delete for good', `Delete ${f.name} for good? This cannot be undone.`, 'Delete'))) return;
        try { await post('/api/files/purge', { id: f.id }); loadTrash(); } catch (e) { toast(e.message, true); }
      } })))));
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
  const kinds = [...new Set(state.files.map((f) => f.kind).filter(Boolean))].sort();
  const ksel = $('libKind'), want = ksel.value || ksel.dataset.want || '';
  ksel.replaceChildren(el('option', { value: '', text: 'All kinds' }), ...kinds.map((k) => el('option', { value: k, text: KIND[k] || k })));
  ksel.value = kinds.includes(want) ? want : '';
  delete ksel.dataset.want;
  const q = $('libSearch').value.trim().toLowerCase();
  let files = state.files.filter((f) => (!q || f.name.toLowerCase().includes(q)) && (!ksel.value || f.kind === ksel.value));
  const sort = $('libSort').value;
  files = files.slice().sort(sort === 'name' ? (a, b) => a.name.localeCompare(b.name)
    : sort === 'old' ? (a, b) => String(a.uploaded).localeCompare(String(b.uploaded))
      : (a, b) => String(b.uploaded).localeCompare(String(a.uploaded)));
  $('fileEmpty').hidden = files.length > 0;
  $('fileEmpty').textContent = state.files.length ? 'No drawing matches the search.' : 'Nothing here yet. Add a drawing above, or paste an SVG or image (Ctrl+V).';
  const locked = jobActive();
  list.replaceChildren(...files.map((f) => {
    const sel = !!(state.doc && state.doc.objects.some((o) => o.file === f.id));
    const open = el('button', { class: 'link', text: f.name, title: sel ? 'On the bed: select it' : 'Put it on the bed',
      disabled: locked, onclick: () => openFile(f.id) });
    const add = el('button', { class: 'icon', text: 'Add', disabled: locked,
      title: sel ? 'Put another copy on the bed' : 'Put it on the bed', onclick: () => addFile(f.id) });
    const tags = [f.kind && el('span', { class: 'tag', text: KIND[f.kind] || f.kind }),
      f.pen && el('span', { class: 'tag', text: `pen ${f.pen}` })];
    const prep = el('button', { class: 'icon', text: 'Prepare', title: f.recipe ? 'Reopen its recipe in Prepare' : 'Open it in Prepare',
      onclick: () => { showWs('prepare'); prepareFromLibrary(f.id); } });
    const ren = el('button', { class: 'icon', text: 'Rename', 'aria-label': `Rename ${f.name}`, onclick: async () => {
      const stem = f.name.replace(/\.[^.]+$/, '');
      const name = await askText('Rename drawing', 'Letters, digits, dot, dash and underscore; other characters become _.', stem, 'Rename');
      if (name === null || name.trim() === stem) return;
      try { await post('/api/files/rename', { id: f.id, name }); await refreshFiles(); } catch (e) { toast(e.message, true); }
    } });
    const del = el('button', { class: 'icon', text: 'Delete', 'aria-label': `Delete ${f.name}`, disabled: locked, onclick: async () => {
      const on = state.doc ? state.doc.objects.filter((o) => o.file === f.id).length : 0;
      if (!(await ask('Delete drawing', `Move ${f.name} to the trash?` + (on === 1 ? ' Its object leaves the bed.' : on ? ` Its ${on} objects leave the bed.` : '') +
        ' It can be restored from the Trash for 30 days.', 'Move to trash'))) return;
      // the objects go first, as a bed edit: after a restore, Undo puts them back
      if (on) D.edit((d) => { d.objects = d.objects.filter((o) => o.file !== f.id); });
      try { await post('/api/files/delete', { id: f.id }); await refreshFiles(); toast(`${f.name} moved to the trash.`); } catch (e) { toast(e.message, true); }
    } });
    const rep = ['svg', 'shape', 'text'].includes(f.kind) ? el('button', { class: 'icon', text: 'Replace', disabled: locked,
      title: 'Load a new SVG into this drawing: objects on the bed keep their place', 'aria-label': `Replace ${f.name}`,
      onclick: () => { $('replaceInput').dataset.id = f.id; $('replaceInput').click(); } }) : null;
    const dl = el('a', { class: 'btn icon', href: `/api/files/orig?id=${encodeURIComponent(f.id)}`, text: 'Save',
      title: 'Download the original file' });
    return el('li', { class: sel ? 'selected' : '' },
      el('img', { src: `/api/files/svg?id=${encodeURIComponent(f.id)}`, alt: '', loading: 'lazy' }),
      el('div', {}, el('div', { class: 'name' }, open), el('div', { class: 'meta' }, ...tags, ' ', f.uploaded ? f.uploaded.replace('T', ' ') : '')),
      el('div', { class: 'row' }, add, prep, dl, ren, rep, del));
  }));
}
