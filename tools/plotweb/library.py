"""Uploaded files: the manifest (input/manifest.json), conversion on upload, per-file saved settings."""
import datetime
import json
import pathlib
import re
import secrets
import threading
import time
import xml.etree.ElementTree as ET

from . import converters

MAX_UPLOAD = 12 * 1024 * 1024
PUBLIC = ("id", "name", "kind", "bytes", "uploaded", "svg", "orig", "conversion", "style", "mask", "vector",
          "pen", "test", "settings", "layers", "geom", "method", "recipe")


def safe_name(name):
    name = pathlib.Path(name or "").name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "upload"
    return name[:100]


class Library:
    def __init__(self, settings, events):
        self.s = settings
        self.ev = events
        self.lock = threading.RLock()
        self.rev = 0

    # ---------------------------------------------------------- manifest
    def _load(self):
        try:
            data = json.loads(self.s.manifest.read_text())
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, entries):
        tmp = self.s.manifest.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, indent=2) + "\n")
        tmp.replace(self.s.manifest)
        self.rev += 1
        self.ev.emit("files", rev=self.rev)

    def list(self):
        with self.lock:
            return [{k: e.get(k) for k in PUBLIC} for e in self._load()]

    def get(self, ident):
        with self.lock:
            return next((e for e in self._load() if e.get("id") == ident), None)

    def update(self, ident, **fields):
        with self.lock:
            entries = self._load()
            for e in entries:
                if e.get("id") == ident:
                    e.update(fields)
                    self._save(entries)
                    return e
        return None

    def path(self, entry, field="svg"):
        name = entry.get(field)
        if not name:
            return None
        p = (self.s.input / name).resolve()
        if p.parent != self.s.input.resolve() or not p.is_file():
            return None
        return p

    def delete(self, ident):
        with self.lock:
            entries = self._load()
            found = next((e for e in entries if e.get("id") == ident), None)
            if not found:
                return False
            self._save([e for e in entries if e is not found])
        for field in ("svg", "orig", "mask"):
            p = self.path(found, field)
            if p:
                try:
                    p.unlink()
                except OSError:
                    pass
        return True

    # ------------------------------------------------------------ upload
    def _add(self, entry):
        with self.lock:
            entries = self._load()
            entries.insert(0, entry)
            self._save(entries)
        return entry

    def upload(self, raw, filename, params, canvas):
        """Store and convert an upload. -> entry. Raises ValueError with a plain message."""
        safe = safe_name(filename)
        ext = pathlib.Path(safe).suffix.lower()
        conv = converters.for_extension(ext)
        if not conv:
            raise ValueError(f"Files of type {ext or '(no extension)'} cannot be plotted. "
                             f"Supported: {', '.join(converters.all_extensions())}.")
        if not raw:
            raise ValueError("The file is empty.")
        if len(raw) > MAX_UPLOAD:
            raise ValueError(f"The file is larger than {MAX_UPLOAD // (1024 * 1024)} MB.")
        if ext == ".svg":
            try:
                ET.fromstring(raw)
            except ET.ParseError as ex:
                raise ValueError(f"This SVG is not valid XML ({ex}).")
        ident = f"{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"
        orig = f"{ident}_{safe}"
        svg = f"{ident}_{pathlib.Path(safe).stem}_plot.svg"
        (self.s.input / orig).write_bytes(raw)
        p = converters.clamp_params(conv.params, params)
        mask = self.s.input / f"{pathlib.Path(svg).stem}_mask.png"
        try:
            extra = conv.fn(self.s.input / orig, self.s.input / svg, p, {"canvas": canvas, "mask": str(mask)})
        except Exception as ex:  # noqa: BLE001
            for f in (self.s.input / orig, self.s.input / svg, mask):
                if f.is_file():
                    f.unlink()
            raise ValueError(f"Could not convert {safe}: {ex}")
        entry = {"id": svg, "name": safe, "kind": conv.id, "bytes": len(raw),
                 "uploaded": datetime.datetime.now().isoformat(timespec="seconds"),
                 "svg": svg, "orig": orig, "params": p}
        entry.update(extra or {})
        if entry.get("mask") and not (self.s.input / entry["mask"]).is_file():
            entry["mask"] = None
        return self._add(entry)

    def add_svg_text(self, svg_text, name, **extra):
        """Store a generated SVG (text art, test sheets) as a library entry."""
        ET.fromstring(svg_text.encode())
        ident = f"{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"
        safe = safe_name(name)
        svg = f"{ident}_{pathlib.Path(safe).stem}.svg"
        (self.s.input / svg).write_text(svg_text, encoding="utf-8")
        entry = {"id": svg, "name": safe, "kind": extra.pop("kind", "generated"),
                 "bytes": len(svg_text.encode()), "uploaded": datetime.datetime.now().isoformat(timespec="seconds"),
                 "svg": svg, "orig": svg}
        entry.update(extra)
        return self._add(entry)

    def add_prepared(self, svg_path, name, src_path, method, recipe, settings, replace=None):
        """Store a Prepare result: its SVG, a copy of its source as the original, the recipe and plot settings.

        With replace (an entry), that entry gets the new files and keeps its id, so Plot reopens it as before."""
        svg_text = pathlib.Path(svg_path).read_text(encoding="utf-8")
        ET.fromstring(svg_text.encode())
        safe = safe_name(name)
        stem = pathlib.Path(safe).stem if pathlib.Path(safe).suffix.lower() in (".svg", ".png", ".jpg") else safe
        stamp = f"{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"
        svg = f"{stamp}_{stem}_plot.svg"
        (self.s.input / svg).write_text(svg_text, encoding="utf-8")
        orig = svg
        if src_path:
            orig = f"{stamp}_{stem}_source{pathlib.Path(src_path).suffix.lower()}"
            (self.s.input / orig).write_bytes(pathlib.Path(src_path).read_bytes())
        fields = {"name": f"{stem}.svg", "kind": "prepared", "method": method, "bytes": len(svg_text.encode()),
                  "uploaded": datetime.datetime.now().isoformat(timespec="seconds"), "svg": svg, "orig": orig,
                  "recipe": recipe, "settings": settings, "geom": None, "layers_keys": None, "layers": None,
                  "mask": None}
        if not replace:
            return self._add(dict(fields, id=svg))
        with self.lock:
            entries = self._load()
            e = next((x for x in entries if x.get("id") == replace["id"]), None)
            if e is None:
                raise ValueError("The drawing to update is no longer in the library.")
            old = [self.path(e, f) for f in ("svg", "orig", "mask")]
            e.update(fields)
            self._save(entries)
        for p in old:
            if p and p.name not in (svg, orig):
                try:
                    p.unlink()
                except OSError:
                    pass
        return e

    def convert_preview(self, raw, filename, params, canvas):
        """Convert without storing. -> {svg, stats, mask(base64)}"""
        import base64
        safe = safe_name(filename)
        ext = pathlib.Path(safe).suffix.lower()
        conv = converters.for_extension(ext)
        if not conv or not conv.previewable:
            raise ValueError("Only images and 3D models have a conversion preview.")
        if not raw or len(raw) > MAX_UPLOAD:
            raise ValueError("The file is empty or too large.")
        token = secrets.token_hex(6)
        src = self.s.cache / f"conv_{token}{ext}"
        dst = self.s.cache / f"conv_{token}.svg"
        mask = self.s.cache / f"conv_{token}_mask.png"
        try:
            src.write_bytes(raw)
            p = converters.clamp_params(conv.params, params)
            extra = conv.fn(src, dst, p, {"canvas": canvas, "mask": str(mask)})
            out = {"svg": dst.read_text(encoding="utf-8"), "stats": (extra or {}).get("conversion")}
            if mask.is_file():
                out["mask"] = base64.b64encode(mask.read_bytes()).decode()
            return out
        except Exception as ex:  # noqa: BLE001
            raise ValueError(f"Preview failed: {ex}")
        finally:
            for f in (src, dst, mask):
                if f.is_file():
                    try:
                        f.unlink()
                    except OSError:
                        pass
