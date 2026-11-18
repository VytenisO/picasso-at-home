#!/usr/bin/env python3
"""Train the v2 handwriting model.

  python train.py --samples DIR --prompts prompts.json --out runs/pre  --steps 20000 --mix 0.2,0.45,0.35
  python train.py --samples DIR --prompts prompts.json --out runs/ft   --steps 6000 --mix 0.05,0.35,0.6 --lr 3e-4 --init runs/pre/best.pt

--mix is the share of Hershey, stitched and real sequences per batch. Batches are generated on the fly with fresh
augmentation in DataLoader workers. Validation is the per-point NLL of the held-out real samples (domain REAL, no
augmentation); the best checkpoint by that number is kept as best.pt. Nothing here talks to the network.
"""
import argparse
import json
import math
import os
import pathlib
import random
import sys
import time

os.environ.setdefault("WANDB_DISABLED", "true")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np
import torch
from torch.utils.data import DataLoader, IterableDataset

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import corpus as corpus_mod  # noqa: E402
import data as D  # noqa: E402
from model import Synth, nll  # noqa: E402

MAX_T = 900


class World:
    """Everything a worker needs to make training sequences."""

    def __init__(self, samples_dir, prompts):
        samples = D.load_samples(samples_dir, prompts)
        self.charset = D.build_charset(samples)
        self.idx = {c: i + 1 for i, c in enumerate(self.charset)}
        self.train, self.held = D.split(samples)
        self.held_words = D.heldout_words(self.held)
        self.real_short = [s for s in self.train if len(s["text"]) <= 12]
        self.real_long = [s for s in self.train if len(s["text"]) > 12]
        self.line_words = D.words_from_lines(self.train)
        self.real_words = [s for s in self.train if s["section"] == "words"] + self.line_words
        self.real_short += self.line_words
        self.glyphs = D.glyphs_from_samples(self.train)
        self.hershey = D.hershey_glyphs(self.charset)
        train_words = [w.strip(".,;:!?\"'()") for s in self.train if s["section"] in ("words", "sentences")
                       for w in s["text"].split()]
        self.corpus = corpus_mod.Corpus(extra_words=[w for w in train_words if w], exclude=self.held_words)

    def text_ok(self, text, glyphs):
        return all(c == " " or c in glyphs for c in text) and all(c in self.idx for c in text)

    def make(self, rng, domain, long):
        if domain == D.REAL:
            if long and rng.random() < 0.6:
                text, st = D.compose([rng.choice(self.real_words) for _ in range(rng.randint(2, 5))], rng)
                return text, D.augment(st, rng, 0.8)
            s = rng.choice(self.real_long if long else self.real_short)
            st = D.augment(s["strokes"], rng, 1.0)
            return s["text"], st
        glyphs = self.glyphs if domain == D.STITCHED else self.hershey
        if not glyphs:
            return None
        for _ in range(10):
            text = self.corpus.text(rng, 5 if long else 2)
            if long and len(text) < 12:
                continue
            if not long and len(text) > 14:
                continue
            if self.text_ok(text, glyphs):
                st = D.stitch(text, glyphs, rng)
                if st:
                    return text, D.augment(st, rng, 0.8)
        return None


class Batches(IterableDataset):
    def __init__(self, world, batch, mix, p_long, seed):
        self.w, self.batch, self.mix, self.p_long, self.seed = world, batch, mix, p_long, seed

    def __iter__(self):
        wi = torch.utils.data.get_worker_info()
        seed = self.seed * 1000 + (wi.id if wi else 0)
        rng = random.Random(seed)
        np.random.seed(seed % (2 ** 32))
        while True:
            long = rng.random() < self.p_long
            items = []
            while len(items) < self.batch:
                dom = rng.choices([0, 1, 2], weights=self.mix)[0]
                r = self.w.make(rng, dom, long)
                if r is None:
                    continue
                text, st = r
                seq, yabs = D.encode(st, D.SPACING * rng.uniform(0.88, 1.12), phase=0.5, jitter=0.004)
                if len(seq) > MAX_T:
                    continue
                items.append((text, seq, yabs, dom))
            yield collate(items, self.w.idx)


def collate(items, idx):
    B = len(items)
    T = max(len(s) for _, s, _, _ in items)
    items = [(t + D.END, s, y, d) for t, s, y, d in items]
    U = max(len(t) for t, _, _, _ in items)
    inp = np.zeros((B, T, 4), np.float32)
    tgt = np.zeros((B, T, 4), np.float32)
    mask = np.zeros((B, T), np.float32)
    text = np.zeros((B, U), np.int64)
    tmask = np.zeros((B, U), np.float32)
    dom = np.zeros(B, np.int64)
    for i, (t, seq, yabs, d) in enumerate(items):
        n = len(seq)
        tgt[i, :n] = seq
        inp[i, 1:n, :3] = seq[:n - 1, :3]
        inp[i, 1:n, 3] = yabs[:n - 1]
        mask[i, :n] = 1
        text[i, :len(t)] = [idx[c] for c in t]
        tmask[i, :len(t)] = 1
        dom[i] = d
    return {k: torch.from_numpy(v) for k, v in
            dict(inp=inp, tgt=tgt, mask=mask, text=text, tmask=tmask, dom=dom).items()}


def eval_set(world, samples):
    items = []
    for s in samples:
        seq, yabs = D.encode(s["strokes"], D.SPACING)
        items.append((s["text"], seq, yabs, D.REAL))
    return items


@torch.no_grad()
def evaluate(model, items, idx, device, bs=16):
    model.eval()
    tot, n = 0.0, 0.0
    items = sorted(items, key=lambda x: len(x[1]))
    for i in range(0, len(items), bs):
        b = {k: v.to(device) for k, v in collate(items[i:i + bs], idx).items()}
        o = model(b["inp"], b["dom"], b["text"], b["tmask"])
        m = b["mask"].sum().item()
        tot += nll(model, o, b["tgt"], b["mask"]).item() * m
        n += m
    model.train()
    return tot / n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", required=True)
    ap.add_argument("--prompts")
    ap.add_argument("--out", required=True)
    ap.add_argument("--steps", type=int, default=20000)
    ap.add_argument("--batch", type=int, default=48)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--lr-min", type=float, default=3e-5)
    ap.add_argument("--hidden", type=int, default=400)
    ap.add_argument("--dropout", type=float, default=0.25)
    ap.add_argument("--mix", default="0.2,0.45,0.35")
    ap.add_argument("--p-long", type=float, default=0.25)
    ap.add_argument("--init")
    ap.add_argument("--eval-every", type=int, default=500)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--keep-every", type=int, default=0, help="also keep stepN.pt every N steps (pick by renders)")
    a = ap.parse_args()

    torch.manual_seed(a.seed)
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    log = open(out / "train.log", "a", buffering=1)

    def say(*x):
        line = " ".join(str(v) for v in x)
        print(line, flush=True)
        log.write(line + "\n")

    world = World(a.samples, a.prompts)
    say(f"charset ({len(world.charset)}): {world.charset!r}")
    say(f"train samples {len(world.train)} (short {len(world.real_short)}, long {len(world.real_long)}), held out "
        f"{len(world.held)}: {[s['text'] for s in world.held]}")
    say(f"real words cut from sentence lines: {len(world.line_words)}")
    say(f"stitch glyphs {len(world.glyphs)}, hershey glyphs {len(world.hershey)}, corpus words {len(world.corpus.words)}")
    missing = [c for c in world.charset if c not in (" ", D.END) and c not in world.glyphs]
    say(f"chars without a stitch glyph: {missing!r}")

    model = Synth(len(world.charset), hidden=a.hidden, dropout=a.dropout).to(dev)
    start_step = 0
    if a.init:
        ck = torch.load(a.init, map_location=dev)
        assert ck["charset"] == world.charset, "charset differs from the init checkpoint"
        model.load_state_dict(ck["model"])
        say(f"init from {a.init} (step {ck.get('step')}, val {ck.get('val')})")
    nparams = sum(p.numel() for p in model.parameters())
    say(f"parameters {nparams}")
    opt = torch.optim.Adam(model.parameters(), lr=a.lr)
    sched = torch.optim.lr_scheduler.LambdaLR(
        opt, lambda s: (a.lr_min / a.lr) + (1 - a.lr_min / a.lr) * 0.5 * (1 + math.cos(math.pi * min(1.0, s / a.steps)))
        if s > 200 else max(0.05, s / 200))

    mix = [float(v) for v in a.mix.split(",")]
    dl = DataLoader(Batches(world, a.batch, mix, a.p_long, a.seed), batch_size=None, num_workers=a.workers,
                    prefetch_factor=4, persistent_workers=True)
    val_items = eval_set(world, world.held)
    tr_items = eval_set(world, [s for s in world.train if s["section"] in ("words", "sentences")][:40])
    meta = dict(charset=world.charset, spacing=D.SPACING, scale=D.SCALE, heldout=[s["text"] for s in world.held],
                heldout_ids=sorted({s["pid"] for s in world.held}), mix=mix, args=vars(a))
    best = float("inf")
    t0 = time.time()
    run = 0.0
    it = iter(dl)
    for step in range(1, a.steps + 1):
        b = {k: v.to(dev, non_blocking=True) for k, v in next(it).items()}
        o = model(b["inp"], b["dom"], b["text"], b["tmask"])
        loss = nll(model, o, b["tgt"], b["mask"])
        opt.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 3.0)
        opt.step()
        sched.step()
        run = 0.98 * run + 0.02 * loss.item() if step > 1 else loss.item()
        if step % 100 == 0:
            say(f"step {step} loss {run:.3f} lr {sched.get_last_lr()[0]:.2e} {time.time() - t0:.0f}s "
                f"mem {torch.cuda.max_memory_allocated() / 2**30:.1f}G" if dev == "cuda" else f"step {step} loss {run:.3f}")
        if step % a.eval_every == 0 or step == a.steps:
            v = evaluate(model, val_items, world.idx, dev)
            tr = evaluate(model, tr_items, world.idx, dev)
            ck = dict(model=model.state_dict(), cfg=model.cfg, charset=world.charset, step=step, val=v, train_real=tr,
                      meta=meta, seconds=time.time() - t0 + (0 if not a.init else 0), params=nparams)
            torch.save(ck, out / "last.pt")
            if a.keep_every and step % a.keep_every == 0:
                torch.save(ck, out / f"step{step}.pt")
            mark = ""
            if v < best:
                best = v
                torch.save(ck, out / "best.pt")
                mark = " *"
            say(f"EVAL step {step} val_heldout {v:.3f} train_real {tr:.3f}{mark}")
    say(f"done {a.steps} steps in {time.time() - t0:.0f}s, best val {best:.3f}")


if __name__ == "__main__":
    main()
