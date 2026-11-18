"""Graves (2013) handwriting synthesis network, small: LSTM 1 with a soft Gaussian attention window over the text,
LSTMs 2 and 3 on top, mixture density output for the next pen offset plus end-of-stroke and end-of-text probabilities.
Sampling lives in infer.py (numpy), so writing needs no torch.

Additions for a tiny single-writer dataset:
  - a domain flag (Hershey / stitched / real) so synthetic pretraining data shares the alignment but not the style;
  - the pen's height above the baseline is an extra input, so the model knows where the line is and drifts less.
"""
import math

import torch
import torch.nn as nn
import torch.nn.functional as F

N_DOMAINS = 3


class Synth(nn.Module):
    def __init__(self, n_chars, hidden=400, mixtures=20, window=10, emb=64, dom_emb=8, dropout=0.1):
        super().__init__()
        self.cfg = dict(n_chars=n_chars, hidden=hidden, mixtures=mixtures, window=window, emb=emb, dom_emb=dom_emb,
                        dropout=dropout)
        self.H, self.M, self.K, self.E = hidden, mixtures, window, emb
        self.char = nn.Embedding(n_chars + 1, emb, padding_idx=0)       # index 0 = padding
        self.dom = nn.Embedding(N_DOMAINS, dom_emb)
        nin = 4 + dom_emb
        self.cell1 = nn.LSTMCell(nin + emb, hidden)
        self.win = nn.Linear(hidden, 3 * window)
        self.lstm2 = nn.LSTM(nin + emb + hidden, hidden, batch_first=True)
        self.lstm3 = nn.LSTM(nin + emb + hidden, hidden, batch_first=True)
        self.drop = nn.Dropout(dropout)
        self.out = nn.Linear(3 * hidden, 2 + 6 * mixtures)
        with torch.no_grad():
            self.win.bias[2 * window:].fill_(-1.0)       # slow attention start

    # one step of layer 1 + window
    def _step1(self, x, w, h, c, kappa, text_e, u, mask):
        h, c = self.cell1(torch.cat([x, w], -1), (h, c))
        p = self.win(h).clamp(-10, 8).exp()
        a, b, k = p.chunk(3, -1)
        kappa = kappa + k / 20.0
        phi = (a.unsqueeze(-1) * torch.exp(-b.unsqueeze(-1) * (kappa.unsqueeze(-1) - u) ** 2)).sum(1)   # [B,U]
        if mask is not None:
            phi = phi * mask
        w = torch.bmm(phi.unsqueeze(1), text_e).squeeze(1)
        return w, h, c, kappa, phi

    def forward(self, inp, dom, text, text_mask):
        B, T, _ = inp.shape
        U = text.shape[1]
        text_e = self.char(text)
        de = self.dom(dom).unsqueeze(1).expand(B, T, -1)
        x = torch.cat([inp, de], -1)
        u = torch.arange(U, device=inp.device, dtype=inp.dtype).view(1, 1, U)
        h = inp.new_zeros(B, self.H)
        c = inp.new_zeros(B, self.H)
        w = inp.new_zeros(B, self.E)
        kappa = inp.new_zeros(B, self.K)
        hs, ws = [], []
        for t in range(T):
            w, h, c, kappa, _ = self._step1(x[:, t], w, h, c, kappa, text_e, u, text_mask)
            hs.append(h)
            ws.append(w)
        h1 = torch.stack(hs, 1)
        W = torch.stack(ws, 1)
        h1d = self.drop(h1)
        h2, _ = self.lstm2(torch.cat([x, W, h1d], -1))
        h2d = self.drop(h2)
        h3, _ = self.lstm3(torch.cat([x, W, h2d], -1))
        return self.out(torch.cat([h1d, h2d, self.drop(h3)], -1))

    def split(self, o, bias=0.0):
        M = self.M
        e, end = o[..., 0], o[..., 1]
        o = o[..., 2:]
        pi = o[..., :M] * (1 + bias)
        mu1, mu2 = o[..., M:2 * M], o[..., 2 * M:3 * M]
        ls1 = (o[..., 3 * M:4 * M] - bias).clamp(min=-6)
        ls2 = (o[..., 4 * M:5 * M] - bias).clamp(min=-6)
        rho = torch.tanh(o[..., 5 * M:]) * 0.99
        return e, end, pi, mu1, mu2, ls1, ls2, rho


def nll(model, o, target, mask):
    """Mean negative log likelihood per point (offset + end-of-stroke)."""
    e, end, pi, mu1, mu2, ls1, ls2, rho = model.split(o)
    x1, x2, eos, fin = target[..., 0:1], target[..., 1:2], target[..., 2], target[..., 3]
    z1 = (x1 - mu1) / ls1.exp()
    z2 = (x2 - mu2) / ls2.exp()
    one = 1 - rho ** 2
    logn = -math.log(2 * math.pi) - ls1 - ls2 - 0.5 * torch.log(one) - (z1 ** 2 + z2 ** 2 - 2 * rho * z1 * z2) / (2 * one)
    lp = torch.logsumexp(F.log_softmax(pi, -1) + logn, -1)
    le = F.binary_cross_entropy_with_logits(e, eos, reduction="none")
    lf = F.binary_cross_entropy_with_logits(end, fin, reduction="none")
    loss = (-lp + le + lf) * mask
    return loss.sum() / mask.sum()
