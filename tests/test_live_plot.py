import json
import pathlib
import subprocess
import sys

import pytest

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
    sys.path.insert(0, str(ROOT / "tools" / "handwriting"))
    import synth
    if not (synth.home() / "src" / "handwriting_synth").is_dir():
        pytest.skip("writing in a hand needs the handwriting-synth project, which is not on this machine")
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


def test_mirror_stroke_and_point_modes():
    import math
    circle = [[150 + 20 * math.cos(i / 10 * 2 * math.pi), 80 + 20 * math.sin(i / 10 * 2 * math.pi), i * 20] for i in range(11)]
    cmds = ["MIRROR stroke " + json.dumps(circle),
            "MIRROR start " + json.dumps([[100, 100, 0]]),
            "MIRROR move " + json.dumps([[110, 102, 10], [120, 105, 20]]),
            "MIRROR move " + json.dumps([[130, 110, 30], [140, 118, 40]]),
            "MIRROR end " + json.dumps([[150, 120, 50]]),
            "MIRROR stroke " + json.dumps([[1, 1, 0], [2, 2, 5]]),      # outside the canvas: clamped onto it
            "TEXT hi", "QUIT"]
    for extra in ([], ["--sport"]):
        ev, _ = control_args(cmds, extra)
        strokes = [e for e in ev if e["event"] == "mirror_stroke"]
        assert len(strokes) == 3 and ev[-1]["status"] == "complete"
        assert (ev[-1]["end_world_steps"] == [0, 0]) == (not extra)      # sport mode does not drive back to S
        for s in strokes:
            for x, y in s["strokes"][0]["pts"]:
                assert 70 - 0.01 <= x <= 337 + 0.01 and 21 - 0.01 <= y <= 201 + 0.01
        assert any(e["event"] == "line_done" for e in ev)             # text after the strokes


def test_mirror_bad_messages_are_ignored():
    ev, _ = control_args(["MIRROR move nonsense", "MIRROR bogus [[1,1]]", "MIRROR move [[1,1,1,1]]", "QUIT"], [])
    assert sum(e["event"] == "error" for e in ev) == 3 and ev[-1]["status"] == "complete"


def test_sport_speed_follows_the_hand_within_limits():
    sys.path.insert(0, str(ROOT / "tools"))
    sys.path.insert(0, str(ROOT / "tools" / "handwriting"))
    import types
    from mirror import Mirror, SPORT_MAX, SPORT_MIN
    s = types.SimpleNamespace(a=types.SimpleNamespace(sport=True, speed=15.0, pen_delay=250))
    m = Mirror(s)
    slow = m.speed([(0, 0, 0), (1, 0, 4000)])           # a slow, short move: the floor
    assert m.speed([(0, 0, 0), (10, 0, 4000)]) == 25.0   # a 10 mm backlog is caught up within 0.4 s
    assert slow == SPORT_MIN
    fast = m.speed([(0, 0, 0), (50, 0, 500)])           # 100 mm/s: at the ceiling
    assert fast == SPORT_MAX
    huge = m.speed([(0, 0, 0), (500, 0, 100)])          # 5000 mm/s of hand: still the ceiling
    assert huge == SPORT_MAX
    s.a.sport = False
    assert m.speed([(0, 0, 0), (500, 0, 100)]) == 15.0   # normal mode is the session speed


def control_args(cmds, extra):
    import time
    p = subprocess.Popen([sys.executable, str(LIVE), "--sim", "--control", "--font", "Comic Neue", *extra],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    out, _ = p.communicate(chr(10).join(cmds) + chr(10), timeout=60)
    return [json.loads(ln) for ln in out.splitlines() if ln.startswith("{")], 0


def test_mirror_order_is_strict():
    m = lambda ph, pts: "MIRROR " + ph + " " + json.dumps(pts)
    cmds = [m("start", [[100, 100, 0]]), m("move", [[110, 100, 10]]),
            m("start", [[200, 150, 0]]),                      # the first stroke never ended
            m("move", [[210, 150, 10], [220, 150, 20]]),
            m("move", [[230, 150, 30]]),                      # and this one never ends either
            "NEWPAGE", "TEXT hi", "CONTINUE", "TEXT ho", "QUIT"]
    ev, _ = control_args(cmds, [])
    st = [e["strokes"][0]["pts"] for e in ev if e["event"] == "mirror_stroke"]
    assert len(st) == 2
    assert st[0][0] == [100.0, 100.0] and st[0][-1] == [110.0, 100.0]       # not joined to the second stroke
    assert st[1][0] == [200.0, 150.0] and st[1][-1] == [230.0, 150.0]
    kinds = [e["event"] for e in ev]
    assert kinds.index("new_page") < kinds.index("resumed") < kinds.index("line_done")
    assert [e["text"] for e in ev if e["event"] == "line_done"] == ["hi", "ho"]
    assert ev[-1]["status"] == "complete" and ev[-1]["end_world_steps"] == [0, 0]


def test_quit_with_an_open_stroke_lifts_the_pen():
    m = lambda ph, pts: "MIRROR " + ph + " " + json.dumps(pts)
    ev, _ = control_args([m("start", [[100, 100, 0]]), m("move", [[120, 110, 10]]), "QUIT"], [])
    assert [e["event"] for e in ev].count("mirror_stroke") == 1 and ev[-1]["status"] == "complete"


def test_sport_refuses_new_page_and_does_not_return():
    ev, _ = control_args(["MIRROR stroke " + json.dumps([[100, 100, 0], [120, 100, 100]]), "NEWPAGE", "QUIT"], ["--sport"])
    assert not any(e["event"] == "new_page" for e in ev)
    assert any(e["event"] == "error" and "sport" in e["message"] for e in ev)


def test_open_stroke_is_ended_when_the_pad_goes_quiet():
    import subprocess as sp
    import time
    p = sp.Popen([sys.executable, str(LIVE), "--sim", "--control", "--font", "Comic Neue"], stdin=sp.PIPE, stdout=sp.PIPE, text=True)
    p.stdin.write("MIRROR start " + json.dumps([[100, 100, 0]]) + "\n")
    p.stdin.flush()
    t0 = time.time()
    seen = False
    for line in p.stdout:
        if '"mirror_stroke"' in line:
            seen = True
            break
        if time.time() - t0 > 30:
            break
    p.stdin.write("QUIT\n")
    p.stdin.flush()
    p.communicate(timeout=30)
    assert seen


def test_end_over_budget_is_kept():
    big = [[100 + i * 0.01, 100, i] for i in range(2000)]
    cmds = ["MIRROR start " + json.dumps([[100, 100, 0]])] + ["MIRROR move " + json.dumps(big)] * 3 + ["MIRROR end " + json.dumps(big), "QUIT"]
    ev, _ = control_args(cmds, [])
    assert sum(1 for e in ev if e["event"] == "mirror_stroke") >= 1
