// The open drawing: its settings (placement, speeds, paper, layers), automatic previews, the placement form and the
// layer list. Settings live on the server (saved with every preview), so a reload or another device sees the same.
import { api, post } from './api.js';
import { state, set, on, jobActive, prefs } from './store.js';
import { $, el, toast } from './ui.js';
import { canvasRect } from './bed.js';

let timer = null;
let seq = 0;

export function initWorkspace() {
  const f = $('placeForm');
  f.addEventListener('change', fromForm);
  f.addEventListener('submit', (e) => e.preventDefault());
  $('placeTL').onclick = () => placeAt('tl');
  $('placeCenter').onclick = () => placeAt('center');
  $('placeFit').onclick = () => placeAt('fit');
  on(['settings', 'preview', 'me'], fillForm);
  on(['settings', 'preview', 'current', 'job'], renderLayers);
  on(['job'], () => { f.querySelectorAll('input').forEach((i) => { i.disabled = jobActive(); }); });
}

export async function openFile(id, { quiet = false } = {}) {
  const entry = state.files.find((f) => f.id === id);
  if (!entry) { if (!quiet) toast('That file is no longer in the library.', true); return; }
  const saved = entry.settings || {};
  const pens = state.me.pens || {};
  // Test sheets belong to one pen: preset it on every layer.
  let layers = saved.layers || [];
  if (entry.pen && !layers.length && entry.layers) {
    layers = entry.layers.map((l) => ({ key: l.key, enabled: true, pen: entry.pen, speed: null, pressure: null }));
  }
  set({ current: { id, name: entry.name, entry }, preview: null, previewStale: true,
    settings: { x: saved.x ?? null, y: saved.y ?? null, w: saved.w ?? null,
      speed: saved.speed ?? state.me.limits.speed.default, travel_speed: saved.travel_speed ?? state.me.limits.travel_speed.default,
      fill: saved.fill || 0, fill_angle: saved.fill_angle ?? 45,
      paper: saved.paper || null, layers: layers.filter((l) => !l.pen || pens[l.pen]) } });
  prefs('lastFile', id);
  $('bedTitle').textContent = entry.name + (entry.pen ? `  (test sheet for pen ${entry.pen})` : '');
  if (!jobActive()) await runPreview();
}

export function changeSettings(patch, { now = false } = {}) {
  if (!state.settings || jobActive()) return;
  seq++;                                   // an older preview still in flight must not overwrite this change
  set({ settings: { ...state.settings, ...patch }, previewStale: true });
  clearTimeout(timer);
  timer = setTimeout(runPreview, now ? 0 : 500);
}

export async function runPreview() {
  clearTimeout(timer);
  if (!state.current) return null;
  const my = ++seq;
  set({ previewing: true });
  try {
    const r = await post('/api/preview', { id: state.current.id, settings: state.settings });
    if (my !== seq) return null;           // a newer preview superseded this one
    const layers = r.settings.layers.map((l) => ({ ...l }));
    set({ preview: r, previewStale: false, settings: { ...state.settings, ...r.settings, layers } });
    return r;
  } catch (e) {
    if (my === seq) {
      set({ preview: { ok: false, problems: [e.message], error: true }, previewStale: false });
      toast(e.message, true);
    }
    return null;
  } finally {
    if (my === seq) set({ previewing: false });
  }
}

function fillForm() {
  const f = $('placeForm'), st = state.settings, me = state.me;
  if (!me) return;
  const L = me.limits;
  $('speedLbl').textContent = `Drawing (mm/s, max ${L.speed.max})`;
  $('travelLbl').textContent = `Pen up (mm/s, max ${L.travel_speed.max})`;
  f.speed.min = L.speed.min; f.speed.max = L.speed.max;
  f.travel_speed.min = L.travel_speed.min; f.travel_speed.max = L.travel_speed.max;
  const [px0, py0, pw, ph] = me.machine.paper;
  $('paperHint').textContent = `Configured sheet: ${pw} x ${ph} mm with its top-left at (${px0}, ${py0}) mm on the bed and a ` +
    `${me.machine.margin} mm margin. The corner stays where it is; only the size can change here.`;
  if (!st) return;
  const set_ = (name, v) => { if (document.activeElement !== f[name]) f[name].value = v ?? ''; };
  set_('x', st.x); set_('y', st.y); set_('w', st.w);
  set_('speed', st.speed); set_('travel_speed', st.travel_speed);
  set_('fill', st.fill || 0); set_('fill_angle', st.fill_angle ?? 45);
  f.paper_custom.checked = !!st.paper;
  set_('paper_w', st.paper ? st.paper.width : pw);
  set_('paper_h', st.paper ? st.paper.height : ph);
  f.paper_w.disabled = f.paper_h.disabled = !st.paper;
  const pv = state.preview;
  if (pv && pv.summary) {
    const b = pv.summary.bbox_machine_mm;
    const c = canvasRect().canvas;
    $('placeHint').textContent = `Size ${(b[2] - b[0]).toFixed(1)} x ${(b[3] - b[1]).toFixed(1)} mm. Canvas: ` +
      `x ${c[0].toFixed(0)}-${c[2].toFixed(0)}, y ${c[1].toFixed(0)}-${c[3].toFixed(0)} mm.`;
  }
}

function fromForm(e) {
  const f = $('placeForm');
  const num = (n) => (f[n].value === '' ? null : Number(f[n].value));
  const patch = { x: num('x'), y: num('y'), w: num('w'), speed: num('speed'), travel_speed: num('travel_speed'),
    fill: num('fill') || 0, fill_angle: num('fill_angle') ?? 45 };
  if (patch.fill && (patch.fill < 0.2 || patch.fill > 50)) {
    toast('Fill spacing must be 0 (off) or between 0.2 and 50 mm.', true);
    f.fill.value = state.settings.fill || 0;
    return;
  }
  const L = state.me.limits;
  for (const [k, lim] of [['speed', L.speed], ['travel_speed', L.travel_speed]]) {
    if (patch[k] === null || patch[k] < lim.min || patch[k] > lim.max) {
      toast(`${k === 'speed' ? 'Drawing' : 'Pen-up'} speed must be between ${lim.min} and ${lim.max} mm/s.`, true);
      f[k].value = state.settings[k];
      return;
    }
  }
  if (e && e.target === f.paper_custom && f.paper_custom.checked) {
    const [, , pw, ph] = state.me.machine.paper;
    patch.paper = { width: pw, height: ph };
  } else {
    patch.paper = f.paper_custom.checked ? { width: num('paper_w'), height: num('paper_h') } : null;
  }
  changeSettings(patch);
}

function placeAt(where) {
  const pv = state.preview;
  if (!pv || !pv.summary || !state.settings) return;
  const c = canvasRect().canvas, b = pv.summary.bbox_machine_mm;
  const ar = (b[3] - b[1]) / Math.max(1e-6, b[2] - b[0]);
  let w = state.settings.w || (b[2] - b[0]);
  if (where === 'fit') w = Math.min(c[2] - c[0], (c[3] - c[1]) / ar);
  const h = w * ar;
  const r2 = (v) => Math.round(v * 100) / 100;
  if (where === 'tl') changeSettings({ x: c[0], y: c[1], w: r2(w) }, { now: true });
  else changeSettings({ x: r2((c[0] + c[2] - w) / 2), y: r2((c[1] + c[3] - h) / 2), w: r2(w) }, { now: true });
}

// ------------------------------------------------------------------ layers
function renderLayers() {
  const list = $('layerList');
  const pv = state.preview;
  const fileLayers = (pv && pv.file_layers) || (state.current && state.current.entry.layers) || [];
  $('layerEmpty').hidden = fileLayers.length > 0;
  if (!fileLayers.length) { list.replaceChildren(); return; }
  if (list.contains(document.activeElement)) return;       // do not rebuild under the operator's cursor
  const st = state.settings || {};
  const L = state.me.limits;
  const pens = state.me.pens || {};
  const locked = jobActive();
  list.replaceChildren(...fileLayers.map((fl, i) => {
    const s = (st.layers || []).find((x) => x.key === fl.key) || { key: fl.key, enabled: true };
    const upd = (patch) => {
      const cur = (state.settings && state.settings.layers) || [];
      const layers = fileLayers.map((x) => ({ ...(cur.find((y) => y.key === x.key) || { key: x.key, enabled: true, pen: null, speed: null, pressure: null }) }));
      Object.assign(layers[i], patch);
      changeSettings({ layers });
    };
    const pen = el('select', { 'aria-label': `Pen for layer ${i + 1}`, disabled: locked, onchange: (e) => {
      const p = pens[e.target.value] || {};
      upd({ pen: e.target.value || null, ...(p.speed ? { speed: p.speed } : {}), ...(p.pressure !== undefined ? { pressure: p.pressure } : {}) });
    } }, el('option', { value: '', text: 'Pen: not set' }),
      Object.entries(pens).map(([n, p]) => el('option', { value: n, text: p.colour ? `${n} (${p.colour})` : n })));
    pen.value = s.pen || '';
    const num = (key, lim, ph, label) => {
      const i2 = el('input', { type: 'number', min: lim.min, max: lim.max, step: key === 'pressure' ? 0.05 : 1,
        placeholder: ph, 'aria-label': label, disabled: locked, title: `${label}: ${lim.min} to ${lim.max}` });
      i2.value = s[key] ?? '';
      i2.onchange = () => {
        const v = i2.value === '' ? null : Number(i2.value);
        if (v !== null && (v < lim.min || v > lim.max)) {
          toast(`${label} must be between ${lim.min} and ${lim.max}.`, true); i2.value = s[key] ?? ''; return;
        }
        upd({ [key]: v });
      };
      return el('label', { class: 'field' }, el('span', { text: label }), i2);
    };
    const on_ = el('input', { type: 'checkbox', disabled: locked, 'aria-label': `Plot layer ${i + 1}` });
    on_.checked = s.enabled !== false;
    on_.onchange = () => upd({ enabled: on_.checked });
    const swatch = el('span', { class: 'swatch' });
    swatch.style.background = fl.color || '#888';
    return el('div', { class: 'layer' + (s.enabled === false ? ' off' : '') },
      el('div', { class: 'layer-head' }, on_, swatch, el('b', { text: `${i + 1}. ${fl.name}` }),
        el('span', { class: 'spacer' }), el('small', { text: `${fl.strokes} strokes` })),
      el('div', { class: 'layer-grid' }, el('label', { class: 'field' }, el('span', { text: 'Pen' }), pen),
        num('speed', L.speed, String(st.speed ?? L.speed.default), 'Speed (mm/s)'),
        num('pressure', { min: 0, max: L.pressure.max }, 'from file', 'Pressure')));
  }));
}

export async function refreshFiles() {
  try {
    const r = await api('/api/files');
    set({ files: r.files });
    if (state.current) {
      const e = r.files.find((f) => f.id === state.current.id);
      if (!e) set({ current: null, preview: null, settings: null });
      else state.current.entry = e;
    }
  } catch (e) { toast(e.message, true); }
}
