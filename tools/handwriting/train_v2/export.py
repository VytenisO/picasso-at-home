#!/usr/bin/env python3
"""Checkpoint (.pt) -> model.npz + model.json for the numpy engine, and check that numpy and torch agree.

  python export.py runs/ft1/best.pt OUTDIR [--samples DIR --prompts FILE]   (samples are only used for the check)
"""
import argparse
import json
import pathlib
import sys

import numpy as np
import torch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import data as D  # noqa: E402
from infer import NumpySynth  # noqa: E402
from model import Synth  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ckpt")
    ap.add_argument("out")
    ap.add_argument("--samples")
    ap.add_argument("--prompts")
    ap.add_argument("--pretrain", help="the pretraining checkpoint the fine-tune started from (for meta.json)")
    ap.add_argument("--style", type=float, default=1.0, help="recommended style value, recorded in meta.json")
    ap.add_argument("--bias", type=float, default=1.0, help="recommended bias value, recorded in meta.json")
    a = ap.parse_args()
    ck = torch.load(a.ckpt, map_location="cpu")
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    np.savez(out / "model.npz", **{k: v.float().numpy() for k, v in ck["model"].items()})
    meta = dict(cfg=ck["cfg"], charset=ck["charset"], scale=ck["meta"]["scale"], spacing=ck["meta"]["spacing"],
                step=ck["step"], val=ck["val"])
    (out / "model.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    stages = []
    for name, path in (("pretrain", a.pretrain), ("finetune", a.ckpt)):
        if not path:
            continue
        c = ck if path == a.ckpt else torch.load(path, map_location="cpu")
        m = c["meta"]
        stages.append(dict(stage=name if a.pretrain else "train", checkpoint=str(path), step=c["step"],
                           seconds=round(c.get("seconds", 0)), val_heldout_nll=round(c["val"], 4),
                           train_real_nll=round(c.get("train_real", float("nan")), 4), mix=m.get("mix"),
                           args=m.get("args")))
    full = dict(model="v2: Graves-style LSTM + attention window + MDN, trained from scratch on this writer",
                parameters=ck.get("params"), cfg=ck["cfg"], charset=ck["charset"].replace(D.END, ""),
                stages=stages, total_train_seconds=sum(s["seconds"] for s in stages),
                heldout_prompt_ids=ck["meta"]["heldout_ids"], heldout_texts=ck["meta"]["heldout"],
                recommended=dict(style=a.style, bias=a.bias),
                loss_note="per-point NLL (offset density + end-of-stroke + end-of-text), lower is better; "
                          "held-out NLL rises during training while samples improve (memorisation of the 199 "
                          "real samples), so checkpoints were chosen from renders, not from this number")
    (out / "meta.json").write_text(json.dumps(full, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out / 'model.npz'}, model.json and meta.json (step {ck['step']}, val {ck['val']:.3f})")

    if a.samples:
        model = Synth(**ck["cfg"])
        model.load_state_dict(ck["model"])
        model.eval()
        s = next(x for x in D.load_samples(a.samples, a.prompts) if x["section"] == "words")
        seq, yabs = D.encode(s["strokes"])
        inp = np.zeros((len(seq), 4), np.float32)
        inp[1:, :3] = seq[:-1, :3]
        inp[1:, 3] = yabs[:-1]
        idx = {c: i + 1 for i, c in enumerate(ck["charset"])}
        with torch.no_grad():
            o = model(torch.from_numpy(inp)[None], torch.tensor([2]), torch.tensor([[idx[c] for c in s["text"] + (D.END if D.END in idx else "")]]),
                      torch.ones(1, len(s["text"]) + (1 if D.END in idx else 0)))[0].numpy()
        r = NumpySynth(out).run(s["text"], teacher=inp.astype(np.float64))
        err = float(np.abs(o - r).max())
        print(f"numpy vs torch on {s['text']!r}: max abs diff {err:.2e}")
        if err > 1e-3:
            raise SystemExit("numpy inference does not match torch")


if __name__ == "__main__":
    main()
