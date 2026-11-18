# Handwriting model v2: trained from scratch on one writer

Text in, pen strokes out, in the writer's own hand. A small Graves (2013) synthesis network trained from scratch on the
capture samples (about 200 prompts), with synthetic pretraining data. Code only: samples, weights and renders never go in
this directory, in git, or to any remote (`handwriting/writers/` is git-ignored).

## Model, and why this one

LSTM with a soft Gaussian attention window over the text, two more LSTM layers, and a mixture density output (20
bivariate Gaussians for the next pen offset, plus end-of-stroke and end-of-text probabilities). Three layers of 400
units, about 3.7 M parameters. Graves-style, not a transformer, because:

- With about 200 samples (about 100k pen points), the inductive bias matters more than capacity. The monotonic attention
  window writes the text left to right, one character at a time. A transformer has to learn that alignment from data, and
  the Cursive Transformer needed about 3,500 words for it.
- It needs no tokenisation of the pen path, and sampling cost is small enough to run in plain numpy (`infer.py`). The
  plotter machine needs no torch.

Two additions for a tiny single-writer set:

- **Domain flag.** Training mixes three sources: (0) the Hershey "scripts" font, joined into cursive words; (1) words
  stitched from the writer's own isolated letters (the letters, capitals and symbols prompts), joined with smooth
  connectors, with i dots and t bars written after the word; (2) the real words and sentence lines. The flag tells the
  model which source a sequence comes from. Sources 0 and 1 give unlimited text for learning which strokes belong to
  which letter. Generation always uses flag 2, the real hand.
- **Height above the baseline as an input**, so the model knows where the line is and drifts less.

Data handling (`data.py`): strokes are normalised to x-height units (x from the left edge, y down from the baseline,
the capture convention used by `engine.py`), lightly smoothed, and resampled every 0.12 x-heights along the pen path.
Augmentation: random scale, x/y stretch, slant (shear ±0.12), rotation ±1.5°, baseline shift, resampling spacing and
phase, and a little jitter. Two-line sentences are split into one sample per line.

Held out from training (`data.HELDOUT_*`): every 10th word prompt (w005, w015, ... w125: 13 words) and sentence s008.
Their words are also removed from the synthetic text corpus, so they are unseen text, not just unseen samples.

## Files

| File | What |
|---|---|
| `data.py` | loading, line split, glyph extraction, stitching, Hershey glyphs, augmentation, encoding |
| `corpus.py` | word lists and pseudo-words for the synthetic samples |
| `model.py` | the network (torch), loss, torch sampler |
| `train.py` | training loop: on-the-fly batches, validation NLL on the held-out set, best.pt / last.pt |
| `export.py` | .pt -> `model.npz` + `model.json` for numpy inference, checks numpy matches torch |
| `infer.py` | numpy-only sampler |
| `generate.py` | `NeuralEngine` (same `word` / `layout` interface as `engine.ReplayEngine`) and the CLI |
| `evaluate.py` | evaluation sheets (held-out words, sentences, seen words, bias sweep) |
| `render.py` | PNG rendering |

## Run on the GPU box (robotlab)

```sh
cd ~/hw-train/v2
python3 -m venv venv
venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cu126
venv/bin/pip install numpy pillow Hershey-Fonts
# copy this directory to ~/hw-train/v2/code, then:
cd code
S=~/hw-train/data/writers/default/samples; P=~/hw-train/data/prompts.json
# 1. pretrain, synthetic heavy (about 2 h on the shared 4090, about 6 GB of GPU memory)
nohup ../venv/bin/python train.py --samples $S --prompts $P --out ../runs/pre2 --steps 16000 --batch 96     --workers 10 --eval-every 1000 --mix 0.15,0.5,0.35 > ../runs/pre2.out 2>&1 < /dev/null &
# 2. fine-tune, real heavy (step1000.pt was picked from the renders; 4000 steps took 27 min)
nohup ../venv/bin/python train.py --samples $S --prompts $P --out ../runs/ft1 --init ../runs/pre2/last.pt     --steps 4000 --batch 96 --workers 10 --eval-every 500 --keep-every 1000 --lr 2e-4 --lr-min 2e-5     --mix 0.05,0.35,0.6 > ../runs/ft1.out 2>&1 < /dev/null &
# 3. export (+ meta.json, numpy/torch check) and evaluate at the recommended style
../venv/bin/python export.py ../runs/ft1/step1000.pt ../final --pretrain ../runs/pre2/last.pt --style 0.75 --bias 1.0     --samples $S --prompts $P
../venv/bin/python evaluate.py --model ../final --samples $S --prompts $P --out ../final/renders --domain 1.75 --seed 7
```

The settings of both stages are recorded in `meta.json` next to the weights. Do not select checkpoints by the
held-out NLL (`val_heldout` in the log). It rises from step 1000 on while the samples keep improving, because the
model gets sharper on the 199 real samples. Judge checkpoints from the `evaluate.py` sheets.

## Write

```sh
python tools/handwriting/train_v2/generate.py "Hello there" --style 0.75   # default model: handwriting/writers/default/model/v2
python tools/handwriting/train_v2/generate.py "Hei på deg" --xh 5 --width 180 --bias 1.0 --seed 2 --out /tmp/hei
```

`--style` blends the two learned styles: 1.0 is the real hand only (most natural, more misspelled unseen words), 0
is the style learned from the isolated letters (stiffer and wider, but the most legible). The recommended value is
0.75. Writes `<out>.json` (lines of strokes in mm, as `ReplayEngine.layout` returns them) and `<out>.png`. `--bias` trades
variety for neatness: 0 samples the raw distribution, and about 1 to 1.5 is tidy. Unknown characters: `--on-missing
substitute` (the default) maps accents and typographic quotes and dashes to plain forms and reports this on stderr. A
character without a substitute is an error. `error` and `skip` are the other policies. The model writes word by word.
`layout` places the words and wraps lines.

From Python:

```python
sys.path.insert(0, "tools/handwriting/train_v2")
from generate import NeuralEngine
eng = NeuralEngine(style=0.75)              # model dir, bias, seed, style, on_missing, fixed_pressure
lines = eng.layout("some text", xh_mm=5, width_mm=180)
```
