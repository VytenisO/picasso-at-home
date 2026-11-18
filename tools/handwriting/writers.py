#!/usr/bin/env python3
"""Writers: several people (or several styles) each with their own handwriting samples and model. Stdlib only.

  writers.py list                 the writers and how many samples each has
  writers.py new NAME             make a writer (lowercase letters, digits, - and _, at most 32 characters)
  writers.py status NAME          what NAME has written, per prompt section, and what is still missing

Layout, all under handwriting/ at the repo root, which is kept out of git on purpose (it is somebody's handwriting; a model
trained on it can forge it). Never commit it, never push it:

  handwriting/prompts.json                     the shared prompt list (tools/handwriting/prompts.py)
  handwriting/writers/<name>/samples/*.json    what <name> wrote on the capture page
  handwriting/writers/<name>/model/            trained model files for <name>, when there is a model

The writer "default" also finds samples in the older handwriting/samples/ if that directory exists.
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
BASE = ROOT / "handwriting"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SAMPLE_GLOB = "[a-z][0-9][0-9][0-9].json"
DEFAULT = "default"


def valid(name):
    return isinstance(name, str) and bool(NAME_RE.fullmatch(name))


def _check(name):
    if not valid(name):
        raise ValueError("a writer name is lowercase letters, digits, - and _, starting with a letter, at most 32 characters")


def writer_dir(name):
    _check(name)
    return BASE / "writers" / name


def samples_dir(name):
    d = writer_dir(name) / "samples"
    legacy = BASE / "samples"
    if name == DEFAULT and not d.exists() and legacy.is_dir():
        return legacy
    return d


def model_dir(name):
    return writer_dir(name) / "model"


def create(name):
    _check(name)
    samples_dir(name).mkdir(parents=True, exist_ok=True)
    return samples_dir(name)


def count(name):
    d = samples_dir(name)
    return len(list(d.glob(SAMPLE_GLOB))) if d.is_dir() else 0


def list_writers():
    names = set()
    root = BASE / "writers"
    if root.is_dir():
        names.update(p.name for p in root.iterdir() if p.is_dir() and valid(p.name))
    if (BASE / "samples").is_dir():
        names.add(DEFAULT)
    return [{"name": n, "samples": count(n)} for n in sorted(names)]


def status(name, prompts_path=None):
    """Per prompt section: how many prompts there are and how many this writer has written."""
    prompts_path = pathlib.Path(prompts_path or BASE / "prompts.json")
    if not prompts_path.exists():
        raise FileNotFoundError(f"{prompts_path} not found; run tools/handwriting/prompts.py first")
    prompts = json.loads(prompts_path.read_text(encoding="utf-8"))["prompts"]
    d = samples_dir(name)
    done = set()
    for p in prompts:
        f = d / f"{p['id']}.json"
        if f.exists():
            try:
                if json.loads(f.read_text(encoding="utf-8")).get("text") == p["text"]:
                    done.add(p["id"])
            except (OSError, ValueError):
                pass
    sections = {}
    for p in prompts:
        s = sections.setdefault(p["section"], {"total": 0, "done": 0, "missing": []})
        s["total"] += 1
        if p["id"] in done:
            s["done"] += 1
        else:
            s["missing"].append(p["text"].replace("\n", " / "))
    return sections


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("list", "new", "status"):
        print(__doc__)
        return 2
    try:
        if argv[0] == "list":
            ws = list_writers()
            for w in ws:
                print(f"{w['name']:<20} {w['samples']} sample(s)")
            if not ws:
                print("no writers yet; make one with: writers.py new NAME")
        elif argv[0] == "new" and len(argv) == 2:
            print(f"samples go to {create(argv[1])}")
        elif argv[0] == "status" and len(argv) == 2:
            _check(argv[1])
            for sec, s in status(argv[1]).items():
                print(f"{sec:<10} {s['done']:>3} of {s['total']:<3}" + (f"  next: {', '.join(s['missing'][:6])}" if s["missing"] else "  complete"))
        else:
            print(__doc__)
            return 2
    except (ValueError, OSError) as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
