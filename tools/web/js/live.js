// Live writing: start a session, send lines, new page, finish. The server owns the session (a job like any other,
// so Stop, the console and the position tracker work as usual); this panel is a view of it.
import { api, post } from './api.js';
import { state, set, on, jobActive } from './store.js';
import { $, el, toast } from './ui.js';
import { confirmDialog } from './run.js';
import { initPad } from './pad.js';

let fetched = '';
let version = 0;      // a stroke response is applied only if no newer request was made

export function initLive() {
  $('liveStart').onclick = () => start(false);
  $('liveSim').onclick = () => start(true);
  $('liveForm').onsubmit = (e) => { e.preventDefault(); send('text'); };
  $('liveNewPage').onclick = () => send('newpage');
  $('liveContinue').onclick = () => send('continue');
  $('liveFinish').onclick = () => send('quit');
  api('/api/fonts').then(fillFonts).catch(() => {});
  initPad();
  on(['job', 'me'], render);
}

function fillFonts(r) {
  const opts = [];
  if (r.hand_samples > 0) opts.push(el('option', { value: '', text: `My handwriting (${r.hand_samples} samples)` }));
  const bundled = new Set(r.bundled);
  for (const f of r.fonts) opts.push(el('option', { value: f, text: bundled.has(f) ? `${f} (bundled)` : f }));
  $('liveFont').replaceChildren(...opts);
}

async function start(simulate) {
  const m = state.me.machine;
  const font = $('liveFont').value;
  const simulated = simulate || state.me.sim;
  const rows = [['Writing', font || 'your own handwriting'], ['Letter height', `${$('liveSize').value} mm`],
    ['Speed', `${$('liveSpeed').value} mm/s`], ['Mode', simulated ? 'Simulator: nothing moves' : 'Real plotter']];
  const sport = $('liveSport').checked;
  const checks = [
    `The plotter is on and the carriage is parked by hand at S, the paper's top-left (${m.start[0]}, ${m.start[1]}) mm. It was slid there with the motors off.`,
    'The pen is up and seated.',
    "The paper is taped down, nothing is in the carriage's way, and the power switch is within reach.",
  ];
  if (sport) {
    rows.push(['SPORT MODE', 'The pad drives the plotter at up to 100 mm/s, above the tested 40 mm/s']);
    checks.push('I understand sport mode can make the motors lose steps. I will check the carriage and park it again afterwards.');
  }
  if (!(await confirmDialog(`Start live writing${simulated ? ' (simulator)' : ''}`, rows, checks, 'Start'))) return;
  try {
    version++;
    set({ liveStrokes: [] });
    fetched = '';
    await post('/api/live/start', { font, xheight: Number($('liveSize').value), speed: Number($('liveSpeed').value),
      mode: simulate ? 'simulate' : 'plot', sport, confirm: { parked: true, sport } });
  } catch (e) { toast(e.message, true); }
}

async function send(cmd) {
  const j = state.job;
  if (!j || j.kind !== 'live') return;
  const input = $('liveText');
  if (cmd === 'text' && !input.value.trim()) return;
  try {
    await post('/api/live/send', { job: j.id, cmd, text: input.value });
    if (cmd === 'text') input.value = '';
  } catch (e) { toast(e.message, true); }
  input.focus();
}

function render() {
  const j = state.job;
  const live = !!(j && j.kind === 'live' && jobActive());
  if (!live) $('liveContinue').hidden = true;
  $('liveSession').hidden = !live;
  $('liveStartForm').hidden = live;
  $('liveStart').disabled = $('liveSim').disabled = jobActive();
  if (!j || j.kind !== 'live') return;
  const l = j.live || {};
  $('liveSportNote').hidden = !(j.meta && j.meta.sport);
  $('liveInfo').textContent = `Page ${l.page || 1}, ${l.lines || 0} line(s) written` + (l.busy ? ', writing now...' : '') +
    (l.last ? `. Last: ${l.last}` : '');
  const hold = !!l.paper_change;      // after New page nothing is drawn until the paper is changed and Continue is pressed
  $('liveContinue').hidden = !hold;
  $('liveWrite').disabled = j.state !== 'running' || hold;
  $('liveNewPage').disabled = j.state !== 'running' || hold;
  $('liveFinish').disabled = j.state !== 'running';
  const key = `${j.id}:${l.page}:${l.lines}`;
  if (key !== fetched) {
    const v = ++version;
    api('/api/live/strokes').then((r) => {
      if (v !== version) return;      // a newer request was made meanwhile
      fetched = key;
      set({ liveStrokes: r.strokes });
    }).catch(() => {});
  }
}
