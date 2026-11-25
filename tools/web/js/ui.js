// Small DOM helpers shared by the modules: element builder, toast, confirm prompt, formatting, schema forms.

export const $ = (id) => document.getElementById(id);

export function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, '');
    else e.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) e.append(c instanceof Node ? c : String(c));
  return e;
}

let toastTimer = null;
export function toast(msg, isErr = false) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast' + (isErr ? ' err' : '');
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, isErr ? 7000 : 3500);
}

export function ask(title, text, okLabel = 'OK') {
  return new Promise((resolve) => {
    const d = $('askDlg');
    $('askTitle').textContent = title;
    $('askText').textContent = text;
    $('askOk').textContent = okLabel;
    $('askInput').hidden = true;
    d.returnValue = '';
    d.onclose = () => resolve(d.returnValue === 'ok');
    d.showModal();
  });
}

// Like ask, with a one-line text field. -> the text, or null when cancelled.
export function askText(title, text, value = '', okLabel = 'OK') {
  return new Promise((resolve) => {
    const d = $('askDlg'), inp = $('askInput');
    $('askTitle').textContent = title;
    $('askText').textContent = text;
    $('askOk').textContent = okLabel;
    inp.hidden = false;
    inp.value = value;
    inp.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); d.close('ok'); } };
    d.returnValue = '';
    d.onclose = () => { inp.hidden = true; resolve(d.returnValue === 'ok' ? inp.value : null); };
    d.showModal();
    inp.focus();
    inp.select();
  });
}

export function fmtTime(s) {
  if (s === null || s === undefined || !isFinite(s)) return '-';
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h} h ${String(m).padStart(2, '0')} min` : `${m} min ${String(sec).padStart(2, '0')} s`;
}
export const mm = (v, d = 1) => (v === null || v === undefined || !isFinite(v)) ? '-' : Number(v).toFixed(d);

// Build a form from a params schema (converters and tools share it). pens: {name: profile}.
export function schemaForm(form, params, pens, values = {}) {
  form.replaceChildren();
  for (const p of params) {
    const id = `${form.id}-${p.key}`;
    let input;
    if (p.section) form.append(el('h3', { class: 'form-section', text: p.section }));
    if (p.type === 'hidden') {          // carried with the form, edited elsewhere (e.g. by clicking the preview)
      form.append(el('input', { id, name: p.key, type: 'hidden', value: values[p.key] ?? p.default ?? '' }));
      continue;
    }
    if (p.type === 'select') {
      input = el('select', { id, name: p.key }, p.options.map(([v, label]) => el('option', { value: v, text: label })));
      input.value = values[p.key] ?? p.default ?? '';
    } else if (p.type === 'pen') {
      input = el('select', { id, name: p.key, required: true },
        el('option', { value: '', text: 'Choose the pen type...' }),
        Object.entries(pens || {}).map(([name, pr]) => el('option', { value: name, text: pr.colour ? `${name} (${pr.colour})` : name })));
      input.value = values[p.key] ?? '';
    } else if (p.type === 'bool') {
      input = el('input', { id, name: p.key, type: 'checkbox' });
      input.checked = !!(values[p.key] ?? p.default);
      form.append(el('label', { class: 'check' }, input, ' ', p.label));
      if (p.help) form.append(el('p', { class: 'hint', text: p.help }));
      continue;
    } else if (p.type === 'number') {
      input = el('input', { id, name: p.key, type: 'number', min: p.min, max: p.max, step: p.step || 'any',
        placeholder: p.optional ? 'not recorded' : undefined });
      const v = values[p.key] ?? p.default;
      input.value = v === null || v === undefined ? '' : v;
      if (p.slider) {
        // A slider for feel, the number box for exact values. Only the number box carries the name.
        const num = input;
        const range = el('input', { type: 'range', min: p.min, max: p.max, step: p.step || 'any', tabindex: '-1',
          'aria-hidden': 'true' });
        range.value = num.value;
        range.oninput = () => { num.value = range.value; num.dispatchEvent(new Event('input', { bubbles: true })); };
        range.onchange = () => num.dispatchEvent(new Event('change', { bubbles: true }));
        num.addEventListener('input', () => { range.value = num.value; });
        num.classList.add('slider-num');
        const unit = p.unit ? ` (${p.unit})` : '';
        form.append(el('label', { class: 'field slider', for: id }, el('span', { text: p.label + unit }),
          el('div', { class: 'slider-row' }, range, num)));
        if (p.help) form.append(el('p', { class: 'hint', text: p.help }));
        continue;
      }
    } else if (p.type === 'textarea') {
      input = el('textarea', { id, name: p.key, rows: 5, maxlength: p.maxlength || 4000, placeholder: p.placeholder });
      input.value = values[p.key] ?? p.default ?? '';
    } else {
      input = el('input', { id, name: p.key, maxlength: p.maxlength || 80, placeholder: p.placeholder,
        list: p.list === 'fonts' ? 'fontList' : undefined });
      input.value = values[p.key] ?? p.default ?? '';
      if (p.list === 'fonts') {           // a font field gets the font browser next to it
        const inp = input;
        form.append(el('label', { class: 'field', for: id }, el('span', { text: p.label }),
          el('div', { class: 'row' }, inp, el('button', { type: 'button', class: 'icon', text: 'Browse', onclick: () => openFontBrowser(inp) }))));
        if (p.help) form.append(el('p', { class: 'hint', text: p.help }));
        continue;
      }
    }
    const unit = p.unit ? ` (${p.unit})` : '';
    const range = p.type === 'number' && p.min !== undefined ? ` ${p.min}-${p.max}` : '';
    form.append(el('label', { class: 'field', for: id }, el('span', { text: p.label + unit + (range ? ',' + range : '') }), input));
    if (p.help) form.append(el('p', { class: 'hint', text: p.help }));
  }
}

// Font browser: every font the server has, searchable, favourites first (per browser).
export function openFontBrowser(input) {
  const d = $('fontDlg'), list = $('fontBrowse'), q = $('fontSearch');
  const names = [...document.querySelectorAll('#fontList option')].map((o) => o.value);
  const favs = () => { try { return JSON.parse(localStorage.getItem('plotter.fontFavs') || '[]'); } catch (e) { return []; } };
  const render = () => {
    const f = favs(), s = q.value.trim().toLowerCase();
    const shown = names.filter((n) => !s || n.toLowerCase().includes(s)).sort((a, b) => (f.includes(b) - f.includes(a)) || a.localeCompare(b));
    list.replaceChildren(...shown.slice(0, 300).map((n) => el('li', {},
      el('button', { type: 'button', class: 'icon fav', text: f.includes(n) ? '\u2605' : '\u2606', 'aria-label': `Favourite ${n}`,
        onclick: () => { const g = favs(); const i = g.indexOf(n); if (i >= 0) g.splice(i, 1); else g.push(n);
          try { localStorage.setItem('plotter.fontFavs', JSON.stringify(g)); } catch (e) { /* private mode */ } render(); } }),
      el('span', { text: n }),
      el('button', { type: 'button', class: 'icon', text: 'Use', onclick: () => {
        input.value = n; input.dispatchEvent(new Event('input', { bubbles: true })); input.dispatchEvent(new Event('change', { bubbles: true })); d.close();
      } }))));
    if (!shown.length) list.replaceChildren(el('li', { class: 'muted', text: 'No font matches.' }));
  };
  q.value = '';
  q.oninput = render;
  render();
  d.showModal();
  q.focus();
}

export function formValues(form, params) {
  const out = {};
  for (const p of params) {
    const input = form.elements[p.key];
    if (!input) continue;
    if (p.type === 'bool') out[p.key] = input.checked;
    else if (p.type === 'number') out[p.key] = input.value === '' ? null : Number(input.value);
    else out[p.key] = input.value;
  }
  return out;
}
