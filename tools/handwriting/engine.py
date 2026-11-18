"""Compatibility shim: engine lives in the handwriting-synth project (see synth.py for where it is looked up)."""
try:
    from . import synth
except ImportError:          # imported as a top-level module with tools/handwriting on sys.path
    import synth

_m = synth.load("engine")
globals().update({k: v for k, v in vars(_m).items() if not k.startswith("__")})

if __name__ == "__main__" and hasattr(_m, "main"):
    raise SystemExit(_m.main())
