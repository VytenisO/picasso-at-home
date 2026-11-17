#!/usr/bin/env python3
"""Build the prompt list for handwriting capture. Stdlib only.

  prompts.py                       write handwriting/prompts.json (English + Norwegian words)
  prompts.py --langs en            English only
  prompts.py --words extra.txt     also cover the bigrams of the words in extra.txt (one per line)

Sections, in the order the capture page offers them:
  words      a small set of everyday words, picked greedily so every letter pair (and every start and end of a
             word) that occurs in the word lists is written at least once. This is the core of a cursive hand.
  sentences  short two-line sentences (pangrams, numbers, punctuation) for spacing, baseline drift, word joins.
  letters    each lowercase letter written three times, separated (a fallback for unseen letter combinations).
  capitals   each capital letter written three times, separated.
  symbols    digits and punctuation.

Text may contain "\\n": the capture page then draws that many ruled lines. Ids are stable for a given word list.
"""
import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

EN = """the and that have with this from they would there their what about which when make like time just know take
people into year your good some could them see other than then now look only come its over think also back after use
two how our work first well way even new want because any these give day most great between need large often hand
high place hold turn move live found study still learn plant cover food sun four thing light quick brown jumping lazy
dog writing plotter paper ink pencil morning evening winter summer mountain river forest ocean water window garden
kitchen bridge journey quiet gentle strange beautiful wonderful through thought enough together always never maybe
everything something nothing someone yellow purple orange silver golden zebra crazy jazz fix box wax quiz exact
example expect vivid voice whisper wrist knowledge bright night flight strength length square circle triangle number
letter word sentence paragraph hello thanks please welcome goodbye friend family mother father brother sister child
house school city country world music dance story dream hope love peace happy smile laugh sleep wake walk run jump fly
swim climb fall push pull open close start finish""".split()

NO = """og det som for ikke med har til den var jeg han hun vi de men kan skal vil fra av om når hvor hva hvem hvorfor
gjør være bli få ta gå komme se si vite tenke bo liten stor god ny gammel lang kort høy lav varm kald glad trist pen
rolig fin dag natt morgen kveld uke måned år tid sted hus hage skog fjell elv hav sjø vann snø regn sol vind vær
vinter sommer høst vår mat brød melk kaffe kake ost fisk smør kjøtt frukt kjære takk hei velkommen venn familie mor far
bror søster barn skole by land verden musikk historie drøm håp kjærlighet fred smil latter søvn øye hånd hjerte tegne
skrive lese papir blyant penn bok ord setning bokstav tall ærlig øyeblikk gjennom sammen alltid aldri kanskje noe
ingenting alle hver mellom under over etter før uten blant bjørn sjokolade fjord blåbær sær""".split()

SENTENCES = [
    "The quick brown fox jumps\nover the lazy dog.",
    "Pack my box with five\ndozen liquor jugs.",
    "How vexingly quick daft\nzebras jump!",
    "Sphinx of black quartz,\njudge my vow.",
    "A journey of a thousand\nmiles begins with a step.",
    "Vår sære Zulu fra badeøya\nspilte jo whist og quickstep.",
    "Blåbærsyltetøy er godt\npå brødskive.",
    "Jeg skriver for hånd\nmed en tegnemaskin.",
    "Meet at 14:30, room 7,\nbuilding 12 (second floor).",
    "Call 555-0123 or write:\nhello@example.com",
    "\"Yes,\" she said, \"but why?\"\nNo one knew; it rained all day.",
    "Price: 249,50 kr (25 % off)\n& free shipping!",
]

SYMBOLS = [". , ; :", "! ? ' \"", "( ) [ ] - /", "& @ # + = %", "0 1 2 3 4", "5 6 7 8 9"]


def bigrams(word):
    w = f"^{word}$"
    return {w[i:i + 2] for i in range(len(w) - 1)}


def pick_words(pool, limit):
    """Greedy cover: repeatedly take the word that adds the most unseen bigrams; shorter words win ties."""
    pool = sorted(set(pool))
    covered, chosen = set(), []
    while pool and len(chosen) < limit:
        best = max(pool, key=lambda w: (len(bigrams(w) - covered), -len(w), w))
        gain = bigrams(best) - covered
        if not gain:
            break
        covered |= gain
        chosen.append(best)
        pool.remove(best)
    return chosen, covered


def build(langs, extra, limit):
    pool = []
    if "en" in langs:
        pool += EN
    if "no" in langs:
        pool += NO
    pool += extra
    words, covered = pick_words(pool, limit)
    words.sort(key=lambda w: (len(w), w))   # short words first: an easy start, and ids stay stable per word list
    prompts = []
    for i, w in enumerate(words, 1):
        prompts.append({"id": f"w{i:03d}", "section": "words", "text": w, "lines": 1})
    for i, s in enumerate(SENTENCES, 1):
        prompts.append({"id": f"s{i:03d}", "section": "sentences", "text": s, "lines": s.count("\n") + 1})
    lower = "abcdefghijklmnopqrstuvwxyz" + ("æøå" if "no" in langs else "")
    for i, c in enumerate(lower, 1):
        prompts.append({"id": f"l{i:03d}", "section": "letters", "text": f"{c} {c} {c}", "lines": 1,
                        "note": "three separate letters, not joined"})
    for i, c in enumerate(lower.upper(), 1):
        prompts.append({"id": f"c{i:03d}", "section": "capitals", "text": f"{c} {c} {c}", "lines": 1,
                        "note": "three separate letters, not joined"})
    for i, s in enumerate(SYMBOLS, 1):
        prompts.append({"id": f"y{i:03d}", "section": "symbols", "text": s, "lines": 1,
                        "note": "separate marks, a little space between"})
    return prompts, len(covered)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--langs", default="en,no", help="word lists to cover: en, no (comma separated)")
    ap.add_argument("--words", help="extra word list, one word per line (lowercase letters)")
    ap.add_argument("--max-words", type=int, default=150, help="at most this many words")
    ap.add_argument("--out", default=str(ROOT / "handwriting" / "prompts.json"))
    a = ap.parse_args()
    langs = {x.strip() for x in a.langs.split(",") if x.strip()}
    if not langs <= {"en", "no"}:
        sys.exit("--langs: en and/or no")
    extra = []
    if a.words:
        extra = [w.strip().lower() for w in pathlib.Path(a.words).read_text(encoding="utf-8").split() if w.strip()]
    prompts, ncover = build(langs, extra, a.max_words)
    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"version": 1, "langs": sorted(langs), "prompts": prompts}, indent=1, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    by = {}
    for p in prompts:
        by[p["section"]] = by.get(p["section"], 0) + 1
    print(f"Wrote {out}: {len(prompts)} prompts " + ", ".join(f"{k} {v}" for k, v in by.items()) +
          f"; the words cover {ncover} distinct letter pairs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
