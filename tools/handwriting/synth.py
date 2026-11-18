"""Where the handwriting-synth project lives. It is its own repo (capture page, writers, the replay engine, model training);
the plotter only needs its writers and its engine at run time.

Looked up in this order: $HANDWRITING_SYNTH, then a sibling directory ../handwriting-synth. A machine without it (the plotter
host, say) still plots with fonts and the mirror; only writing in somebody's hand needs the project and that person's samples.
"""
import importlib
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


def home():
    return pathlib.Path(os.environ.get("HANDWRITING_SYNTH") or ROOT.parent / "handwriting-synth")


def load(name):
    """Import handwriting_synth.<name> from the project, or raise RuntimeError saying where it looked."""
    src = home() / "src"
    if not (src / "handwriting_synth").is_dir():
        raise RuntimeError(f"the handwriting-synth project was not found at {home()} (set HANDWRITING_SYNTH to its directory)")
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    return importlib.import_module("handwriting_synth." + name)


def prompts_path():
    return home() / "prompts" / "prompts.json"
