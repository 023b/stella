"""Guards: no personal details in the tree, the workflow builds and stays wired, the helper's text clean-up works."""
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helper"))


def files():
    for p in ROOT.rglob("*"):
        if p.is_file() and ".git" not in p.parts and p.suffix in {".py", ".js", ".json", ".md", ".sh", ".css", ".service", ".txt"}:
            yield p


def test_no_personal_data():
    bad = re.compile(r"\barun\b|radon|lancer|aviate|/home/|100\.\d+\.\d+\.\d+|@gmail|guildford|memos|surrey|jobtrack|pizero|caramel", re.I)
    hits = [f"{p.relative_to(ROOT)}: {m.group(0)}" for p in files()
            if p.name != "test_clean.py" for m in [bad.search(p.read_text(errors="ignore"))] if m]
    assert not hits, hits


def test_workflow_is_built_and_wired():
    subprocess.run([sys.executable, "-I", str(ROOT / "n8n" / "build_flow.py")], check=True, capture_output=True)
    wf = json.loads((ROOT / "n8n" / "stella-voice.json").read_text())
    names = {n["name"] for n in wf["nodes"]}
    for src, outs in wf["connections"].items():
        assert src in names
        for out in outs["main"]:
            for link in out:
                assert link["node"] in names
    hook = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.webhook")
    assert hook["parameters"]["path"] == "stella/voice"


def test_full_flow_is_wired():
    wf = json.loads((ROOT / "n8n" / "full" / "stella-voice-full.json").read_text())
    names = {n["name"] for n in wf["nodes"]}
    assert len(names) == len(wf["nodes"]) > 200
    for src, outs in wf["connections"].items():
        assert src in names
        for out in outs["main"]:
            for link in out:
                assert link["node"] in names


def test_wake_word_is_dropped():
    from stella_helper import clean
    assert clean(" Hey Stella, what's the time?") == "what's the time?"
    assert clean("Stella open files") == "open files"
    assert clean("tell me about stellar winds") == "tell me about stellar winds"
