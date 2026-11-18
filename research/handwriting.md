# Handwriting: text in, connected cursive out

Goal: take text (a file, an argument, or lines typed live in a terminal) and have the plotter write it in human-looking,
connected cursive that follows human stroke order. Researched 2026-11-17. Nothing here is built yet.

## What "following human strokes" requires

The output must be *online* handwriting: ordered pen trajectories (x, y, pen-up/down), the way a hand moves, not outlines.
A whole cursive word should be one continuous stroke, with pen lifts only between words, at i-dots and t-bars. This matches
the plotter well: fewer lifts, less pen play, faster. Outline fonts (TrueType) are wrong for this: the plotter would draw
every glyph twice around its edge. `tools/svg_vector.py` already outlines text for technical drawings; handwriting needs a
different source of strokes.

## Options

| Option | Connected | Human-like | Effort | Notes |
|---|---|---|---|---|
| **A. Hershey single-stroke fonts** ("Script 1-stroke", "Script 2-stroke", "Cursive") | Script: mostly | Low: every `a` identical | Hours | Public-domain-style licence with attribution. In Inkscape via Evil Mad Scientist's Hershey Text; pure-Python renderers exist. Letters are not truly joined in all scripts; each glyph is a fixed stroke list. |
| **B. Commercial single-line handwriting fonts** (Quantum Enterprises "Scriptalizer", singlelinefonts.com) | Yes | Medium to good, with letter variants | Cost, $130+ for the best | Evil Mad Scientist forum: only one product offers automatic glyph variants "at any price"; cursive variants are the hard part. Licence and format fit unknown. |
| **C. Graves-style neural synthesis** (RNN + mixture density network, trained on IAM On-Line) | Yes, natively | High: real stroke order, per-sample variation, optional "style" priming | Days | `sjvasquez/handwriting-synthesis` has pretrained weights and writes SVG, but needs TensorFlow 1.x and is described as hard to run; `otuva/handwriting-synthesis` is a TF2 migration; PyTorch ports exist. Inference is a small LSTM, fine on a CPU. IAM data is licensed for non-commercial research. Output is only as neat as the model: a bit messy, which is part of its charm. |
| **D. Transformer ink models** (TrInk, DiffInk, "Cursive Transformer" 2025) | Yes | High | Weeks | Newer and better in papers. The Cursive Transformer is trained on only about 3,500 words, which is encouraging for a model of your own hand, but I found no confirmed released code or weights. Treat as future work. |
| **E. Your own handwriting as a font** | Yes | Highest, it is you | Days | Record strokes on a tablet or phone (pen trajectory with timing), segment into letter and ligature units, assemble with joins and variation. Or fine-tune C/D on a few thousand of your own words. |

## Recommendation

Build in phases, each one useful alone:

1. **A with humanisation (first, small).** `tools/handwrite.py` reads text from an argument, a file or stdin; lays it out on
   the paper canvas (width, margins, line spacing, word wrap) using a Hershey script font; applies per-glyph jitter
   (slant, size, baseline drift, spacing), smooths with a spline, and writes an SVG with one stroke per word that `plot_svg.py`
   already plots. Uses the per-stroke `data-pressure` we added to vary line weight along a word, so it reads less uniform.
2. **C, behind the same interface.** A `--engine neural` option: a pre-trained Graves model, run once to produce strokes
   for each line. Isolate it in an optional dependency (own venv) so the plotter code does not need TensorFlow or PyTorch.
   Same layout and output code as phase 1.
3. **E later**, if the result matters enough to use your own hand.

The interface stays the same across engines: text in, SVG of strokes out, so the web UI can offer a "Handwriting" tool
with an engine selector.

## Constraints that come from this machine

- **Pen play.** The BIC loses short strokes and circle closures (see the stroke test). Cursive loops (`e`, `l`) are small,
  so letter size has a floor: x-height of about 4 to 5 mm until the stroke test says smaller works. Run
  `stroke_test.py --pen bic` before choosing a size. The loops in cursive close easier with pressure, so the data-pressure
  lever is useful here.
- **No acceleration ramp.** Dense, smooth points are fine. Resample the stroke to about 0.3 to 0.5 mm spacing so each
  `SM` is a sensible step, and apply curve smoothing before simplification (the RDP tolerance of 0.05 mm is fine).
- **Speed.** Drawing is limited to 40 mm/s. Normal handwriting speed is about 15 to 25 mm/s on this unit, which gives
  roughly one line of 60 characters in about a minute and a half at 5 mm x-height.
- **Calibration.** Steps per mm (34.9 / 35.5) has about 2% uncertainty; for text this only changes size, not legibility.
- **Pen lifts** cost about 0.65 s each. One stroke per word keeps this to one lift per word, which is the main time saver.

## Live terminal input

A one-shot SVG plot cannot take live input: `plot_svg.py` plots a finished file and returns the carriage to the start.
Live mode needs a long-lived session that keeps the `Plotter` and its tracked position, reads a line at a time, generates
strokes for it and draws it, then waits for the next line. This also fits the web UI: a session API (start, write line,
finish, stop). Sensible v1: line-buffered, the cursor stays at the end of the last line, a blank line ends the session, and
Ctrl-C raises the pen and releases the motors as it does now. Because firmware 2.0.1 has no position readback, a live
session must be started with the carriage parked on the paper's top-left, same as a plot.

## Open questions

- Which hand do you want: a fixed neat cursive (option A or B), a model's average human hand (C), or yours (E)?
- Paper size and what the text is for (notes, letters, place cards)? This decides the size and the page layout.
- Does a pen that has 0.3 to 0.5 mm of play suit this, or should handwriting use the marker or a finer pen? The stroke
  test, run per pen type, answers it.

## Sources

- Graves, Generating Sequences With Recurrent Neural Networks: https://arxiv.org/abs/1308.0850
- Distill, Four Experiments in Handwriting with a Neural Network: https://distill.pub/2016/handwriting/
- sjvasquez/handwriting-synthesis (TF1, SVG output): https://github.com/sjvasquez/handwriting-synthesis
- otuva/handwriting-synthesis (TF2 migration): https://github.com/otuva/handwriting-synthesis
- The Cursive Transformer: https://arxiv.org/abs/2504.00051
- TrInk: https://arxiv.org/html/2508.21098v1 and DiffInk: https://arxiv.org/abs/2509.23624
- Evil Mad Scientist, Hershey Text for Inkscape: https://www.evilmadscientist.com/2011/hershey-text-an-Inkscape-extension-for-engraving-fonts
- Evil Mad Scientist forum, single-stroke handwriting fonts: https://www.evilmadscientist.com/?p=28198

## Decision (2026-11-17): your own hand

You will write the samples on a tablet, so the target is option E. The plan has three programs; only the first exists.

### 1. Capture (built): `tools/handwriting/capture_server.py`
A small web server plus a tablet page. It shows one prompt at a time over ruled guide lines (baseline, x-height,
ascender, descender; size adjustable), records every pen point with position, time and pressure, and saves one JSON
file per prompt under `handwriting/writers/<name>/samples/` (gitignored: personal data, never push it). Undo, clear, skip, back, deep links
(`/?id=w012`), resume where you stopped, and replacing a sample keeps the old one as `.prev.json`.
Run `python3 tools/handwriting/capture_server.py --host tailscale` and open the printed address on the tablet.

**Several writers.** Each person is a writer with their own samples and (later) model, all under the gitignored `handwriting/writers/<name>/`. Make one with `python3 tools/handwriting/writers.py new NAME`, list with `writers.py list`, see what is still missing with `writers.py status NAME`. One capture server serves everyone: a writer opens `http://<host>:8765/w/<name>/` (the list is at `/w/`; `--writer NAME` picks who is at `/`). Live writing takes `--writer NAME`, and the web UI lists every writer that has samples under Writing. Samples and models never go to git or any remote: `.gitignore` covers `handwriting/writers/`, `handwriting/samples/`, `model/` directories and the usual model file extensions, and a test checks it.
`tools/handwriting/prompts.py` builds the prompts: 126 short everyday words (English and Norwegian) chosen so every
letter pair that occurs in the lists is written at least once, 12 two-line sentences (pangrams, digits,
punctuation), each letter three times, each capital three times, symbols. About 200 prompts, roughly 45 to 60
minutes. More words: `prompts.py --max-words 1500 --words mylist.txt`; existing samples stay valid as long as a
prompt keeps its text (the server notices when a rebuilt list changes the meaning of an id).

### 2. Synthesis (next): text in, strokes out, in your hand
How much it can do depends on how much you write:
- **About 150 words (the first pass):** replay. A word you wrote is plotted as you wrote it, normalised to a common
  x-height, baseline and slant. Unseen words need stitching from letter pairs, which needs the strokes aligned to
  letters. Doable with heuristics (pen lifts, x-minima, letter widths from the isolated letters) but the joins will
  be rough. Good enough to test the whole chain on the plotter.
- **About 1,500 to 3,500 words (several sessions):** train a small model (the Cursive Transformer paper reaches
  realistic cursive from about 3,500 words). It learns the alignment, joins and your variation by itself and writes
  any text. Training is a few hours on a GPU, inference is cheap. This is the version that "writes like you".
The interface is the same either way: `Engine.strokes(text) -> list of strokes in mm`, so the plotting side does not
change when the engine improves. Per-point pressure from the tablet maps onto the plotter's per-stroke pressure.

### 3. Live plot backend (built, sim-tested only): `tools/live_plot.py`
Built as v1 with the replay engine (`tools/handwriting/engine.py`): words you wrote replay as written, other words are stitched
from your single letters without joins. Stdin lines or `--text/--file`, `:newpage`, `:quit`, `--json` events. Not yet built: the
local socket for the web UI (its job runner can feed stdin meanwhile), word-by-word writing, slant normalisation. Run
`python3 tools/live_plot.py --sim` first; the real plotter asks for WRITE after the park check. Original design:
A long-lived process, separate from `plot_svg.py`, because live writing needs the tracked carriage position to
survive between lines. Design:
- Owns one `plot_svg.Plotter` (so the same ceilings, step tracking and shutdown rules) and the page: paper rectangle,
  margins, line height, cursor position. Started with the carriage parked at the paper's top-left, same confirmation
  as a plot; refuses anything off the paper or past the page bottom.
- Input: lines from a terminal (stdin), and the same commands over a local socket for the web UI. Commands: write text,
  new line, new page (pause for a paper change), pen up, stop. A line is written when you press Enter; an option writes
  word by word.
- Output: JSON events (position, pen, line started and finished, error), the same style as the `__POS__` lines the
  web UI already reads, so Opus's redesigned UI can show a live tracker without a second protocol.
- Each written line is also saved as strokes plus the text, so a session is reproducible and can be re-plotted.
- Safety is unchanged: Ctrl-C raises the pen, waits for the move in flight, releases the motors. After a stop or a
  crash the position is unknown (no readback), so a new session means re-parking, as today.

### Open questions for you
- How much writing time are you willing to spend: one hour now (replay, to prove the chain), or the several hours the
  trained model wants? The capture tool supports both; start with the first pass.
- Language mix: English, Norwegian, or both? The word lists and letters include both (æ, ø, å).
- Which pen will draw the result? The BIC's play limits the smallest loops, so the x-height you write at on the tablet
  should be the size you will plot at; the page sets that with its Size control (stored with each sample).


## Training a model on your own hand: existing work (researched 2026-11-18)

Correction to the table above: the Cursive Transformer does have released code.

| Project | What | Data needed | Fit |
|---|---|---|---|
| [greydanus/cursivetransformer](https://github.com/greydanus/cursivetransformer) (paper arXiv 2504.00051) | GPT-style model over tokenised pen offsets (polar tokens), 442k parameters, trained from scratch on one writer's words. Includes `collect.html` for recording and a 3,500-word example set. | 3,500 words from one writer (about an hour per 100 words on a trackpad); augmentation: shear, scale, point dropping | Best match. Data is `(x, y, pen_down)` points plus the word, which our capture samples already contain. No licence in the repo, so private use only. Training took a few hours on an A100 (Colab or Paperspace), a few dollars. |
| [X-rayLaser/pytorch-handwriting-synthesis-toolkit](https://github.com/X-rayLaser/pytorch-handwriting-synthesis-toolkit) | Graves 2013 (LSTM + attention + mixture density) in PyTorch, MIT, pretrained checkpoints (IAM, English), custom data through a provider class | Strokes plus transcripts; no documented fine-tuning or style priming | Fallback. Pretrained weights are English only (no æ, ø, å). |
| [awei669/DiffInk](https://github.com/awei669/DiffInk) (ICLR 2026) | Latent diffusion over whole lines, style from a short reference | Trained on Chinese CASIA data | Not a fit for Latin or Norwegian text. |

Privacy: training in a cloud notebook uploads the handwriting. Train on a machine you control (a local GPU), or accept the upload knowingly. The model and samples stay under `handwriting/writers/<name>/` and out of git.


### sjvasquez/handwriting-synthesis, read in detail (2026-11-18)

The repo behind the "Stuff Made Here" handwriting-robot video ([repo](https://github.com/sjvasquez/handwriting-synthesis)). Graves 2013, LSTM with attention and a 20-component mixture output, pretrained on IAM On-Line (English).

- **Requirements:** `tensorflow==1.6.0` pinned, so a Python 3.6 era environment. The TF2 port [otuva/handwriting-synthesis](https://github.com/otuva/handwriting-synthesis) is archived (April 2025). No licence in either repo. Last push to the original January 2024.
- **Character set:** 73 symbols only (`drawing.py`): a-z, A-Z without Q, X and Z, digits, space and `! " # ' ( ) , - . : ; ?`. No æ, ø, å, no `/ @ % &`. Lines are at most 75 characters. The set is fixed by the trained weights (the attention reads a one-hot of these characters), so Norwegian needs a model trained with a larger alphabet; IAM has no such letters either.
- **Style:** the "priming" feeds a recorded stroke sequence (`styles/style-N-strokes.npy`, offsets x, y, end-of-stroke, up to 1200 points) plus its text in front of the new line, and the model continues in that style. 13 styles ship (IAM writers). Own handwriting would mean writing our own `.npy` pair in the same normalised format; the code does not offer that, and how faithfully a single priming sample carries over is untested.
- **Neatness:** a bias per line (higher = neater).
- **Training:** needs the IAM On-Line database (registration, non-commercial), `prepare_data.py`, then `rnn.py`: "a couple of days on a single Tesla K80". The Hand class has a warm start from the shipped checkpoint, so fine-tuning on own samples is plausible but untested.
- **Output:** pen offsets, drawn to SVG by `demo.py` (denoise, align). Our engines want strokes in mm, so a thin adapter would replace the SVG step.
