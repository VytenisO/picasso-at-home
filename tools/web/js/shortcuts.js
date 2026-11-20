// One table for the keyboard. The help dialog's list and the key shown in each control's tooltip both come from
// it, so they cannot drift apart. The handlers live where the work is (app.js, bed.js, prepare.js); a key added
// there gets a row here.
import { $, el } from './ui.js';

// rows: keys (each a combination: an array of key names) with the control each one presses, or a text label for
// pointer gestures; what it does.
export const SHORTCUTS = [
  { keys: [[['1'], null], [['2'], null]], what: 'Prepare, Plot' },
  { keys: [[['P'], '#previewBtn']], what: 'Preview (Plot)' },
  { keys: [[['T'], '#travelBtn']], what: 'Show or hide the pen-up moves (Plot)' },
  { keys: [[['Ctrl', 'Enter'], '#prepSend']], what: 'Send to Plot (Prepare); in Turtletoy code: draw' },
  { keys: [[['Space'], '#pauseBtn'], [['Space'], '#continueBtn']], what: 'Pause / continue' },
  { keys: [[['Shift', 'S'], '#stopBtn']], what: 'Stop' },
  { label: 'Arrows', what: 'Move the selection 1 mm (Shift: 10 mm, Alt: 0.1 mm), with the bed focused; under Machine control they jog' },
  { keys: [[['Ctrl', 'Z'], '#undoBtn'], [['Ctrl', 'Shift', 'Z'], '#redoBtn']], what: 'Undo, redo' },
  { keys: [[['Ctrl', 'C'], null], [['Ctrl', 'X'], null], [['Ctrl', 'V'], null], [['Ctrl', 'D'], '#xfBox [data-act=dup]'], [['Ctrl', 'A'], null]],
    what: 'Copy, cut, paste (also an SVG or picture from another program), duplicate, select all' },
  { keys: [[['Delete'], '#xfBox [data-act=del]']], what: 'Remove the selection from the bed (the drawing stays in the Library)' },
  { keys: [[['R'], '#xfBox [data-act=rot90]'], [['Shift', 'R'], '#xfBox [data-act=rot-90]']], what: 'Turn the selection 90 degrees right, left' },
  { keys: [[['H'], '#xfBox [data-act=mirx]'], [['V'], '#xfBox [data-act=miry]']], what: 'Mirror the selection left to right, top to bottom' },
  { keys: [[['Ctrl', 'G'], '#xfBox [data-act=group]'], [['Ctrl', 'Shift', 'G'], '#xfBox [data-act=ungroup]']], what: 'Group, ungroup' },
  { keys: [[['Z'], null]], what: 'Zoom to the selection' },
  { keys: [[['Tab'], null], [['Shift', 'Tab'], null]], what: 'Select the next, previous object (bed focused)' },
  { keys: [[['Ctrl', 'I'], null]], what: 'Invert the selection' },
  { keys: [[['Ctrl', 'Shift', 'V'], null]], what: 'Paste in place (where it was copied from)' },
  { label: 'Space + drag', what: 'Pan the bed (when no job is running)' },
  { keys: [[['+'], '#zoomIn'], [['-'], '#zoomOut'], [['0'], '#zoomFit']], what: 'Zoom in, out, fit' },
  { keys: [[['\\'], '#vPanels']], what: 'Hide or show the side panels (Plot)' },
  { keys: [[['?'], null]], what: 'This help' },
  { keys: [[['Esc'], null]], what: 'Close a dialog or menu (never stops a job)' },
  { label: 'Shift-click, drag a box', what: 'Select several. A box dragged left to right takes what is fully inside; right to left, anything it touches' },
  { label: 'Shift while dragging', what: 'A corner: stretch freely. The turn handle: any angle (else 15 degree steps)' },
  { label: 'Alt while dragging', what: 'No snapping (else edges and centres snap to the canvas, paper, guides and objects, or to the grid)' },
  { label: 'Rulers', what: 'Drag a guide out of a ruler; drag it back onto the ruler to remove it. Right-click for more' },
];

const combo = (k) => k.join('+');

// The help dialog's list.
export function renderShortcutList() {
  const dl = $('keyList');
  if (!dl) return;
  dl.replaceChildren(...SHORTCUTS.flatMap((r) => {
    const dt = r.label ? el('dt', { text: r.label })
      : el('dt', {}, ...[...new Map(r.keys.map(([k]) => [combo(k), k])).values()]      // each combination once
        .flatMap((k, i) => [...(i ? [', '] : []), ...k.flatMap((p, j) => [...(j ? ['+'] : []), el('kbd', { text: p })])]));
    return [dt, el('dd', { text: r.what })];
  }));
}

// " (Ctrl+Z)" on the tooltip of every control a key presses; an older hand-written key in brackets is replaced.
export function tagTooltips(root = document) {
  for (const r of SHORTCUTS) {
    for (const [k, sel] of r.keys || []) {
      if (!sel) continue;
      for (const node of root.querySelectorAll(sel)) {
        const base = (node.getAttribute('title') || node.textContent.trim()).replace(/\s*\((?:[^()]{1,3}|Ctrl\+[^()]+|Shift\+[^()]+|Space)\)$/, '');
        node.setAttribute('title', `${base} (${combo(k)})`);
      }
    }
  }
}
