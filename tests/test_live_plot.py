import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
LIVE = ROOT / "tools" / "live_plot.py"
GUIDE = {"x_height": 40, "ascender": 40, "descender": 32, "pitch": 150, "baselines": [120], "left": 24}


def stroke(pts, t0=0):
    return [[x, y, t0 + i * 10, 0.5] for i, (x, y) in enumerate(pts)]


def sample(pid, text, section, strokes):
    return {"version": 1, "id": pid, "text": text, "section": section, "lines": 1, "guide": GUIDE,
            "canvas": {"w": 800, "h": 200, "dpr": 1}, "strokes": strokes}


def make_samples(d):
    d.mkdir(parents=True, exist_ok=True)
    box = lambda x: stroke([(x, 120), (x, 80), (x + 30, 80), (x + 30, 120)])
    items = [sample("w001", "hi", "words", [box(30), box(80)]),
             sample("l001", "a a a", "letters", [box(30), box(130), box(230)]),
             sample("l002", "b b b", "letters", [box(30), box(130), box(230)])]
    for s in items:
        (d / f"{s['id']}.json").write_text(json.dumps(s))


def run(tmp_path, *args):
    make_samples(tmp_path / "samples")
    return subprocess.run([sys.executable, str(LIVE), "--sim", "--json", "--samples", str(tmp_path / "samples"),
                           "--prompts", str(tmp_path / "none.json"), *args],
                          capture_output=True, text=True, timeout=120)


def events(r):
    return [json.loads(ln) for ln in r.stdout.splitlines() if ln.startswith("{")]


def test_replay_and_stitch(tmp_path):
    r = run(tmp_path, "--text", "hi ab zz")
    ev = events(r)
    assert r.returncode == 0, r.stdout + r.stderr
    kinds = [e["event"] for e in ev]
    assert kinds[0] == "session" and kinds[-1] == "done"
    assert any(e["event"] == "warning" and "z" in e["message"] for e in ev)
    done = [e for e in ev if e["event"] == "line_done"]
    assert done and ev[-1]["end_world_steps"] == [0, 0]


def test_page_full_refuses_everything(tmp_path):
    text = "\n".join(["hi"] * 80).replace("\n", "\n")
    r = run(tmp_path, "--text", text)
    ev = events(r)
    assert any(e["event"] == "page_full" for e in ev)
    assert ev[-1]["event"] == "done" and ev[-1]["end_world_steps"] == [0, 0]


def test_speed_ceiling(tmp_path):
    r = run(tmp_path, "--text", "hi", "--speed", "50")
    assert r.returncode != 0


def test_standard_font(tmp_path):
    r = subprocess.run([sys.executable, str(LIVE), "--sim", "--json", "--font", "Comic Neue", "--text", "Hello there"],
                       capture_output=True, text=True, timeout=120)
    ev = events(r)
    assert r.returncode == 0, r.stdout + r.stderr
    assert ev[-1]["event"] == "done" and ev[-1]["end_world_steps"] == [0, 0]
    assert any(e["event"] == "line_done" for e in ev)


def control(cmds, wait=60, font="Comic Neue"):
    """Run live_plot.py --control --sim with the given stdin lines (stdin stays open until the process ends)."""
    import time
    p = subprocess.Popen([sys.executable, str(LIVE), "--sim", "--control", "--font", font],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    for c in cmds:
        p.stdin.write(c + "\n")
        p.stdin.flush()
    t0 = time.time()
    try:
        out, _ = p.communicate(timeout=wait)
    except subprocess.TimeoutExpired:
        p.kill()
        raise
    return [json.loads(ln) for ln in out.splitlines() if ln.startswith("{")], time.time() - t0


def test_control_text_is_literal():
    ev, _ = control(["TEXT :newpage", "TEXT :quit", "TEXT hi", "QUIT"])
    kinds = [e["event"] for e in ev]
    assert "new_page" not in kinds
    assert [e["text"] for e in ev if e["event"] == "line_done"] == [":newpage", ":quit", "hi"]
    assert ev[-1]["event"] == "done" and ev[-1]["status"] == "complete"


def test_new_page_holds_until_continue():
    ev, _ = control(["NEWPAGE", "TEXT hi", "QUIT"])
    kinds = [e["event"] for e in ev]
    assert "new_page" in kinds and "line_done" not in kinds      # nothing drawn while waiting for the paper
    ev, _ = control(["NEWPAGE", "TEXT hi", "CONTINUE", "QUIT"])
    kinds = [e["event"] for e in ev]
    assert kinds.index("new_page") < kinds.index("resumed") < kinds.index("line_done")


def test_stop_wakes_an_idle_session():
    ev, dt = control(["STOP"], wait=20)
    assert ev[-1]["event"] == "done" and ev[-1]["status"] == "interrupted"
    assert dt < 10


def test_eof_without_quit_stops():
    import subprocess as sp
    p = sp.Popen([sys.executable, str(LIVE), "--sim", "--control", "--font", "Comic Neue"], stdin=sp.PIPE, stdout=sp.PIPE, text=True)
    out, _ = p.communicate("", timeout=20)
    assert '"interrupted"' in out
