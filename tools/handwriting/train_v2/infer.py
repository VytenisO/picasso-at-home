"""numpy-only inference for the v2 model (no torch needed to write). Loads model.npz + model.json from export.py.

The math mirrors model.Synth step by step (PyTorch LSTM gate order i, f, g, o).
"""
import json
import pathlib

import numpy as np


def _sig(x):
    return 1.0 / (1.0 + np.exp(-x))


def _lstm(x, h, c, Wih, Whh, b):
    g = Wih @ x + Whh @ h + b
    i, f, gg, o = np.split(g, 4)
    c = _sig(f) * c + _sig(i) * np.tanh(gg)
    h = _sig(o) * np.tanh(c)
    return h, c


class NumpySynth:
    def __init__(self, model_dir):
        d = pathlib.Path(model_dir)
        meta = json.loads((d / "model.json").read_text(encoding="utf-8"))
        self.cfg, self.charset, self.scale = meta["cfg"], meta["charset"], meta["scale"]
        self.idx = {ch: i + 1 for i, ch in enumerate(self.charset)}
        self.end = "" if "" in self.charset else ""      # end-of-text token (models from round 2 on)
        self.writable = set(self.charset) - {""}
        z = np.load(d / "model.npz")
        p = {k: z[k].astype(np.float64) for k in z.files}
        self.char_emb = p["char.weight"]
        self.dom_emb = p["dom.weight"]
        self.c1 = (p["cell1.weight_ih"], p["cell1.weight_hh"], p["cell1.bias_ih"] + p["cell1.bias_hh"])
        self.l2 = (p["lstm2.weight_ih_l0"], p["lstm2.weight_hh_l0"], p["lstm2.bias_ih_l0"] + p["lstm2.bias_hh_l0"])
        self.l3 = (p["lstm3.weight_ih_l0"], p["lstm3.weight_hh_l0"], p["lstm3.bias_ih_l0"] + p["lstm3.bias_hh_l0"])
        self.win_W, self.win_b = p["win.weight"], p["win.bias"]
        self.out_W, self.out_b = p["out.weight"], p["out.bias"]
        self.H, self.M, self.K = self.cfg["hidden"], self.cfg["mixtures"], self.cfg["window"]

    def run(self, text, domain=2, bias=1.0, rng=None, max_steps=None, teacher=None):
        """Sample offsets for text. domain may be fractional: 1.6 blends the stitched-letters (1) and real (2) styles. With teacher=[T,4] inputs (dx, dy, eos, yabs) it returns the raw outputs instead,
        for checking against the torch model."""
        rng = rng or np.random.default_rng()
        if self.end:
            text = text + self.end
        idx = [self.idx[c] for c in text]
        U = len(idx)
        te = np.concatenate([self.char_emb[idx], np.zeros((1, self.char_emb.shape[1]))], 0)   # [U+1, E]
        u = np.arange(U + 1, dtype=np.float64)
        lo = int(np.floor(domain))
        f = float(domain) - lo
        de = self.dom_emb[lo] if f == 0 else (1 - f) * self.dom_emb[lo] + f * self.dom_emb[lo + 1]
        H, M, K = self.H, self.M, self.K
        h1 = c1 = h2 = c2 = h3 = c3 = np.zeros(H)
        w = np.zeros(te.shape[1])
        kappa = np.zeros(K)
        prev = np.zeros(4)
        y = 0.0
        out, raw = [], []
        steps = len(teacher) if teacher is not None else (max_steps or 45 * U + 60)
        for t in range(steps):
            if teacher is not None:
                prev = teacher[t]
            x = np.concatenate([prev, de])
            h1, c1 = _lstm(np.concatenate([x, w]), h1, c1, *self.c1)
            pw = np.exp(np.clip(self.win_W @ h1 + self.win_b, -10, 8))
            a, b, k = pw[:K], pw[K:2 * K], pw[2 * K:]
            kappa = kappa + k / 20.0
            phi = (a[:, None] * np.exp(-b[:, None] * (kappa[:, None] - u[None, :]) ** 2)).sum(0)
            if teacher is not None:
                phi = phi.copy()
                phi[U] = 0.0
            w = phi @ te
            h2, c2 = _lstm(np.concatenate([x, w, h1]), h2, c2, *self.l2)
            h3, c3 = _lstm(np.concatenate([x, w, h2]), h3, c3, *self.l3)
            o = self.out_W @ np.concatenate([h1, h2, h3]) + self.out_b
            if teacher is not None:
                raw.append(o)
                continue
            e, end, o = o[0], o[1], o[2:]
            pi = o[:M] * (1 + bias)
            mu1, mu2 = o[M:2 * M], o[2 * M:3 * M]
            s1 = np.exp(np.maximum(o[3 * M:4 * M] - bias, -6))
            s2 = np.exp(np.maximum(o[4 * M:5 * M] - bias, -6))
            rho = np.tanh(o[5 * M:]) * 0.99
            pi = np.exp(pi - pi.max())
            pi /= pi.sum()
            j = rng.choice(M, p=pi)
            n1, n2 = rng.standard_normal(2)
            dx = mu1[j] + s1[j] * n1
            dy = mu2[j] + s2[j] * (rho[j] * n1 + np.sqrt(max(1e-6, 1 - rho[j] ** 2)) * n2)
            eos = float(rng.random() < _sig(e))
            y += dy
            out.append((dx, dy, eos))
            prev = np.array([dx, dy, eos, y * self.scale])
            if _sig(end) > 0.5:
                out[-1] = (dx, dy, 1.0)
                return np.array(out), True
        if teacher is not None:
            return np.array(raw)
        return np.array(out), False
