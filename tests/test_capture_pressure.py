"""The capture page reports what the pen sent (pressure_info); the server stores it small and typed."""
import json
import pathlib
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "handwriting"))
import capture_server as cs  # noqa: E402


def test_clean_pressure():
    assert cs.clean_pressure(None) is None
    assert cs.clean_pressure("x") is None
    ok = cs.clean_pressure({"real": 1, "type": "pen", "min": 0.02, "max": 0.91, "levels": 31, "junk": "x" * 99})
    assert ok == {"real": True, "type": "pen", "min": 0.02, "max": 0.91, "levels": 31}
    bad = cs.clean_pressure({"real": False, "type": "touch" * 9, "min": float("nan"), "max": "0.5", "levels": True})
    assert bad["min"] is None and bad["max"] is None and bad["levels"] == 0 and len(bad["type"]) <= 16


def test_save_roundtrip(tmp_path):
    prompts = tmp_path / "p.json"
    prompts.write_text(json.dumps({"prompts": [{"id": "r001", "section": "en-01", "text": "Hello there.", "lines": 1}]}))
    stores = cs.Stores(str(prompts), "default", str(tmp_path / "s"))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cs.make_handler(stores))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        body = {"pointer": "pen", "strokes": [[[1, 2, 0, 0.1], [3, 4, 10, 0.8]]], "guide": {"x_height": 48, "baselines": [100]},
                "canvas": {"w": 10, "h": 10, "dpr": 1}, "pressure_info": {"real": True, "type": "pen", "min": 0.1, "max": 0.8, "levels": 12}}
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/api/sample/r001", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        assert urllib.request.urlopen(req).status == 200
        saved = json.loads((tmp_path / "s" / "r001.json").read_text())
        assert saved["pressure_info"] == {"real": True, "type": "pen", "min": 0.1, "max": 0.8, "levels": 12}
        assert saved["strokes"][0][1] == [3, 4, 10, 0.8]
    finally:
        srv.shutdown()
