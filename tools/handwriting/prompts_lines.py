#!/usr/bin/env python3
"""Build a prompt list of running-text lines for handwriting capture (stdlib only).

  prompts_lines.py                      500 lines, half English, half Norwegian, into handwriting/prompts-lines.json
  prompts_lines.py --count 300 --max-chars 34

Why lines: the models that write convincingly (Graves-style LSTM, transformers) train on whole lines, so they learn word
joins, spacing, capitals and punctuation in context, not just isolated words. This picks, from a large pool of short
modern sentences, the ones that add the most new coverage: every three-character combination (including word starts and
ends and punctuation), each capital letter, each digit, and æ ø å, each wanted a few times. The first lines are the most
useful, so stopping early still leaves a well covered set.

Sections: en-01, no-01, en-02, no-02, ... of 50 lines each (a block is about 20 minutes of writing), so the capture page's
section menu doubles as a break marker. One ruled line per prompt: use the size slider so the longest line fits.

Source: sentences from Tatoeba.org (CC BY 2.0 FR). The downloaded dumps are cached under handwriting/corpus/ (git-ignored,
not redistributed); the prompt file only carries the chosen sentences and records the source.
"""
import argparse
import bz2
import collections
import heapq
import json
import math
import pathlib
import random
import re
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
URL = "https://downloads.tatoeba.org/exports/per_language/{c}/{c}_sentences.tsv.bz2"
LANGS = {"en": "eng", "no": "nob"}
OK_EN = re.compile(r"^[A-Za-z0-9 .,!?:;'\-]+$")
OK_NO = re.compile(r"^[A-Za-zÆØÅæøå0-9 .,!?:;'\-]+$")
SPECIAL = "æøåÆØÅ"
BAD = re.compile(r"\b(fuck|shit|bitch|cunt|dick|porn|sex|rape|kill|suicide|nazi|faen|helvete|fitte|knulle|dritt)\w*", re.I)


def load(code, cache):
    f = cache / f"{code}_sentences.tsv.bz2"
    if not f.exists():
        cache.mkdir(parents=True, exist_ok=True)
        print(f"downloading {URL.format(c=code)} ...", file=sys.stderr)
        urllib.request.urlretrieve(URL.format(c=code), f)
    out = []
    with bz2.open(f, "rt", encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 3:
                out.append(parts[2].strip())
    return out


def features(text):
    """-> {feature: target count}. Case-sensitive start-of-word letters; trigrams are lowercase with padding."""
    low = " " + text.lower() + " "
    f = {}
    for i in range(len(low) - 2):
        f["t:" + low[i:i + 3]] = 3
    for m in re.finditer(r"\b([A-ZÆØÅ])(\w)", text):
        f["c2:" + m.group(1) + m.group(2).lower()] = 2
        f["c1:" + m.group(1)] = 6
    for d in set(re.findall(r"\d", text)):
        f["d:" + d] = 6
    for s in set(ch for ch in text if ch in SPECIAL):
        f["s:" + s] = 8
    return f


def pick(pool, count, rng, name_cap):
    """Lazy greedy: take the sentence with the most remaining coverage per character."""
    feats = {s: features(s) for s in pool}
    have = collections.Counter()

    def gain(s):
        g = sum(max(0, k - have[f]) / k for f, k in feats[s].items())
        return g / math.sqrt(len(s))

    heap = [(-gain(s), rng.random(), s) for s in pool]
    heapq.heapify(heap)
    chosen, names = [], 0
    while heap and len(chosen) < count:
        g, r, s = heapq.heappop(heap)
        cur = -gain(s)
        if heap and cur > heap[0][0] + 1e-12:         # stale: gains only ever fall, so re-queue with the true value
            heapq.heappush(heap, (cur, r, s))
            continue
        if re.search(r"\b(Tom|Mary|Maria|John)\b", s):
            if names >= name_cap:
                continue
            names += 1
        chosen.append(s)
        for f in feats[s]:
            have[f] += 1
    return chosen, have, feats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=500, help="lines in total, split evenly English and Norwegian")
    ap.add_argument("--min-chars", type=int, default=22)
    ap.add_argument("--max-chars", type=int, default=38, help="longest line (one ruled line on the capture page)")
    ap.add_argument("--block", type=int, default=50, help="lines per section")
    ap.add_argument("--cache", default=str(ROOT / "handwriting" / "corpus"))
    ap.add_argument("--out", default=str(ROOT / "handwriting" / "prompts-lines.json"))
    ap.add_argument("--seed", type=int, default=3)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    per = a.count // 2
    picked = {}
    for lang, code in LANGS.items():
        ok = OK_NO if lang == "no" else OK_EN
        pool = sorted({s for s in load(code, pathlib.Path(a.cache))
                       if a.min_chars <= len(s) <= a.max_chars and ok.match(s) and not BAD.search(s)
                       and s[0].isupper() and s[-1] in ".!?" and s.count(" ") >= 3})
        print(f"{lang}: {len(pool)} candidate sentences", file=sys.stderr)
        chosen, have, feats = pick(pool, per, rng, name_cap=max(6, per // 25))
        picked[lang] = chosen
        tri = [f for f in have if f.startswith("t:")]
        print(f"{lang}: {len(chosen)} lines, {len(tri)} distinct 3-char combinations, "
              f"{sum(1 for f in tri if have[f] >= 3)} of them seen 3 or more times", file=sys.stderr)

    prompts, n = [], 0
    blocks = math.ceil(per / a.block)
    for b in range(blocks):
        for lang in ("en", "no"):
            for text in picked[lang][b * a.block:(b + 1) * a.block]:
                n += 1
                prompts.append({"id": f"r{n:03d}", "section": f"{lang}-{b + 1:02d}", "text": text, "lines": 1})
    doc = {"version": 1, "langs": list(LANGS), "kind": "running-text lines",
           "source": "Tatoeba.org sentences, CC BY 2.0 FR (https://tatoeba.org)", "prompts": prompts}
    out = pathlib.Path(a.out)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    chars = sum(len(p["text"]) for p in prompts)
    print(f"wrote {out}: {len(prompts)} lines, {chars} characters, about {chars // 6} words", file=sys.stderr)


if __name__ == "__main__":
    main()
