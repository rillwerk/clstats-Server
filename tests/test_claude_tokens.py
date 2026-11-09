"""Project attribution in scripts/claude_tokens.py (the Mac agent)."""

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "claude_tokens.py"


@pytest.fixture
def ct(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("claude_tokens", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    root = tmp_path / "Projects"
    for name in ("alpha", "beta"):
        (root / name / ".git").mkdir(parents=True)
    (root / ".secrets").mkdir()
    monkeypatch.setenv("PROJECT_ROOTS", str(root))
    mod.root = root
    return mod


def _assistant(n, cwd, tools=(), model="claude-opus-5-5"):
    content = [{"type": "tool_use", "name": name, "input": args} for name, args in tools]
    return {
        "type": "assistant",
        "cwd": cwd,
        "timestamp": f"2026-11-09T10:{n:02d}:00Z",
        "requestId": f"req{n}",
        "message": {
            "id": f"msg{n}", "model": model, "content": content,
            "usage": {"input_tokens": 1, "output_tokens": 10, "cache_read_input_tokens": 100},
        },
    }


def _parse(ct, tmp_path, entries):
    folder = tmp_path / "logs" / str(ct.root).replace("/", "-").replace(".", "-")
    folder.mkdir(parents=True)
    log = folder / "s.jsonl"
    log.write_text("".join(json.dumps(e) + "\n" for e in entries))
    rows = ct.parse_file(log, folder)
    return [rows[f"msg{i}:req{i}"][2] for i in range(len(entries))]


def test_tool_paths_name_the_project_and_earlier_answers_follow(ct, tmp_path):
    r = str(ct.root)
    projects = _parse(ct, tmp_path, [
        _assistant(0, r),
        _assistant(1, r, [("Bash", {"command": "ls"})]),
        _assistant(2, r, [("Read", {"file_path": f"{r}/alpha/src/main.py"})]),
        _assistant(3, r),
        _assistant(4, r, [("Bash", {"command": f"git -C {r}/beta log | head"})]),
        _assistant(5, r, [("Bash", {"command": f"cat {r}/.secrets/x"})]),
    ])
    assert projects == ["alpha", "alpha", "alpha", "alpha", "beta", "beta"]


def test_cd_relative_to_cwd_and_cwd_wins(ct, tmp_path):
    r = str(ct.root)
    projects = _parse(ct, tmp_path, [
        _assistant(0, r, [("Bash", {"command": "cd beta && git status"})]),
        _assistant(1, f"{r}/alpha", [("Edit", {"file_path": f"{r}/beta/x"})]),
    ])
    assert projects == ["beta", "alpha"]


def test_missing_folders_globs_and_outside_paths_are_ignored(ct, tmp_path):
    r = str(ct.root)
    projects = _parse(ct, tmp_path, [
        _assistant(0, r, [("Bash", {"command": f"cd ios; ls {r}/alpha*; cat /etc/hosts"})]),
        _assistant(1, r, [("Write", {"file_path": "/tmp/x.txt"}), ("Glob", {"path": f"{r}/README.md"})]),
    ])
    assert projects == [ct.NO_PROJECT, ct.NO_PROJECT]


def test_streamed_blocks_take_the_project_from_a_later_block(ct, tmp_path):
    r = str(ct.root)
    first = _assistant(0, r)
    second = _assistant(0, r, [("Read", {"file_path": f"{r}/beta/a.py"})])
    folder = tmp_path / "logs" / str(ct.root).replace("/", "-").replace(".", "-")
    folder.mkdir(parents=True)
    log = folder / "s.jsonl"
    log.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n")
    rows = ct.parse_file(log, folder)
    assert len(rows) == 1
    assert rows["msg0:req0"][2] == "beta"


def test_home_paths_are_expanded(ct, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    projects = _parse(ct, tmp_path, [
        _assistant(0, str(ct.root), [("Bash", {"command": "ls ~/Projects/alpha/src"})]),
    ])
    assert projects == ["alpha"]
