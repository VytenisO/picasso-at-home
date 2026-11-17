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
