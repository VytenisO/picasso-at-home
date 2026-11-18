"""Writers: separate handwriting per person, and none of it can reach git. No hardware."""
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "handwriting"))
import writers  # noqa: E402


@pytest.fixture()
def base(tmp_path, monkeypatch):
    monkeypatch.setattr(writers, "BASE", tmp_path / "handwriting")
    (tmp_path / "handwriting").mkdir()
    return tmp_path / "handwriting"


def test_names():
    for ok in ("anna", "me", "a1_b-c"):
        assert writers.valid(ok)
    for bad in ("", "Anna", "../x", "a/b", "1a", "a b", "x" * 33, None, 5):
        assert not writers.valid(bad)
    with pytest.raises(ValueError):
        writers.samples_dir("../etc")


def test_writers_are_separate(base):
    writers.create("anna")
    writers.create("bob")
    (writers.samples_dir("anna") / "w001.json").write_text("{}")
    assert writers.count("anna") == 1 and writers.count("bob") == 0
    assert [w["name"] for w in writers.list_writers()] == ["anna", "bob"]
    assert writers.model_dir("bob") != writers.model_dir("anna")


def test_status_counts_only_matching_prompts(base):
    writers.create("anna")
    (base / "prompts.json").write_text(json.dumps({"prompts": [
        {"id": "w001", "text": "hello", "section": "words"}, {"id": "w002", "text": "world", "section": "words"}]}))
    (writers.samples_dir("anna") / "w001.json").write_text(json.dumps({"text": "hello"}))
    (writers.samples_dir("anna") / "w002.json").write_text(json.dumps({"text": "stale"}))
    s = writers.status("anna")
    assert s["words"]["done"] == 1 and s["words"]["total"] == 2 and s["words"]["missing"] == ["world"]


def test_legacy_samples_belong_to_default(base):
    (base / "samples").mkdir()
    (base / "samples" / "w001.json").write_text("{}")
    assert writers.samples_dir("default") == base / "samples" and writers.count("default") == 1
    assert writers.samples_dir("anna") == base / "writers" / "anna" / "samples"


def test_handwriting_data_is_git_ignored():
    for rel in ("handwriting/writers/anna/samples/w001.json", "handwriting/writers/anna/model/weights.pt",
                "handwriting/samples/w001.json", "handwriting/model/net.safetensors", "anywhere/x.pth"):
        r = subprocess.run(["git", "check-ignore", "-q", rel], cwd=ROOT)
        assert r.returncode == 0, f"{rel} is not ignored by git"
    tracked = subprocess.run(["git", "ls-files", "handwriting"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert tracked in ([], ["handwriting/prompts.json"]), tracked      # only the shared prompt list may be tracked
