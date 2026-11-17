"""Paths and settings. One Settings object is built at startup and passed around."""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TOOLS = ROOT / "tools"
WEB = TOOLS / "web"

# Limits mirrored from plot_svg.py so the UI can show them and reject early. plot_svg.py checks again.
sys.path.insert(0, str(TOOLS))
import plot_svg  # noqa: E402

LIMITS = {
    "speed": {"min": 1.0, "max": plot_svg.MAX_SPEED, "default": 15.0, "unit": "mm/s"},
    "travel_speed": {"min": 1.0, "max": plot_svg.MAX_TRAVEL_SPEED, "default": 100.0, "unit": "mm/s"},
    "pressure": {"min": 0.0, "max": plot_svg.MAX_PRESSURE, "default": None, "unit": "fraction"},
    "chunk_seconds": plot_svg.CHUNK_SECONDS,
    "edge_margin": plot_svg.EDGE_MARGIN,
}


class Settings:
    """Everything the server needs to know about where things live and how it runs."""

    def __init__(self, data_dir=None, machine=None, pens=None, web_config=None, sim=False, sim_speed=20.0,
                 python=None):
        data = pathlib.Path(data_dir) if data_dir else ROOT
        self.root = ROOT
        self.input = data / "input"
        self.results = data / "results"
        self.cache = self.results / "web"
        self.manifest = self.input / "manifest.json"
        self.machine_path = pathlib.Path(machine) if machine else ROOT / "config" / "machine.json"
        self.pens_path = pathlib.Path(pens) if pens else ROOT / "config" / "pens.json"
        self.web_config = pathlib.Path(web_config) if web_config else ROOT / "config" / "web.json"
        self.sim = bool(sim)                 # every job runs plot_svg.py --sim; the serial port is never touched
        self.sim_speed = float(sim_speed)    # real-time factor for simulated runs
        self.python = python or sys.executable
        for d in (self.input, self.results, self.cache):
            d.mkdir(parents=True, exist_ok=True)

    def machine(self):
        return json.loads(self.machine_path.read_text())

    def pens(self):
        try:
            data = json.loads(self.pens_path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def bed(self):
        """Bed, paper and inset canvas in machine mm, for the UI and for validation."""
        return self.bed_from(self.machine_path.read_bytes())

    def bed_from(self, machine_bytes):
        cfg = json.loads(machine_bytes)
        travel = cfg.get("travel_mm") or {}
        p = cfg.get("paper") or {}
        bx, by = float(travel.get("x", 368.0)), float(travel.get("y", 227.5))
        x0, y0 = float(p.get("x0", 0.0)), float(p.get("y0", 0.0))
        w, h = float(p.get("width", bx)), float(p.get("height", by))
        m = max(0.0, float(p.get("margin_mm", 0.0)))
        return {"bed": [bx, by], "paper": [x0, y0, w, h], "margin": m,
                "canvas": [x0 + m, y0 + m, x0 + w - m, y0 + h - m], "start": [x0, y0],
                "edge_margin": plot_svg.EDGE_MARGIN, "servo": cfg.get("servo"),
                "calibrated": cfg.get("measured")}
