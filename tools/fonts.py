"""Font lookup for the plotter: fonts bundled in fonts/ first, then whatever fontconfig knows. Needs fonttools."""
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUNDLED = ROOT / "fonts"
_cache = {}


def _names(path):
    from fontTools.ttLib import TTFont
    f = TTFont(str(path), lazy=True, fontNumber=0)
    try:
        n = f["name"]
        fam = n.getDebugName(16) or n.getDebugName(1)
        sub = (n.getDebugName(17) or n.getDebugName(2) or "").lower()
        return fam, ("bold" in sub), ("italic" in sub or "oblique" in sub)
    finally:
        f.close()


def bundled():
    """-> [{family, bold, italic, file}] for fonts/*.ttf and *.otf."""
    if "bundled" not in _cache:
        out = []
        for p in sorted(BUNDLED.glob("*")):
            if p.suffix.lower() in (".ttf", ".otf"):
                try:
                    fam, b, i = _names(p)
                    out.append({"family": fam, "bold": b, "italic": i, "file": str(p)})
                except Exception:  # noqa: BLE001
                    continue
        _cache["bundled"] = out
    return _cache["bundled"]


def system_families():
    if "system" not in _cache:
        try:
            out = subprocess.check_output(["fc-list", ":", "family"], text=True, encoding="utf-8", errors="replace", timeout=10)
            fams = {ln.split(",")[0].strip() for ln in out.splitlines() if ln.strip()}
        except Exception:  # noqa: BLE001
            fams = set()
        _cache["system"] = sorted(fams, key=str.lower)
    return _cache["system"]


def families():
    """Names for a picker: bundled first, then installed."""
    b = sorted({f["family"] for f in bundled()}, key=str.lower)
    return b + [s for s in system_families() if s not in b]


def find(family, bold=False, italic=False):
    """-> path of a font file for the family, or None. A bundled family wins over an installed one."""
    fam = (family or "").split(",")[0].strip().strip("'\"")
    want = fam.lower()
    cands = [f for f in bundled() if f["family"].lower() == want]
    if cands:
        cands.sort(key=lambda f: (f["bold"] != bold, f["italic"] != italic))
        return cands[0]["file"]
    return None
