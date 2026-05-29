"""Unit tests for the Stop-hook session-goal capture (Session→IGIO #2).

Loads stop_hook.py directly (same importlib pattern as test_task_capture.py).
"""
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_mod_path = Path(__file__).parent / "stop_hook.py"
sys.path.insert(0, str(_mod_path.parent))
spec = importlib.util.spec_from_file_location("stop_hook", _mod_path)
sh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sh)


def _transcript(tmp_path, conditions):
    p = tmp_path / "t.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for cond, met in conditions:
            f.write(json.dumps({"type": "goal_status", "met": met, "condition": cond}) + "\n")
            f.write(json.dumps({"type": "user", "message": {"content": "x"}}) + "\n")
    return str(p)


def test_latest_session_goal_returns_last(tmp_path):
    tp = _transcript(tmp_path, [("first goal", False), ("second goal", False)])
    assert sh.latest_session_goal(tp) == "second goal"


def test_latest_session_goal_empty_when_none(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"type": "user", "message": {"content": "x"}}) + "\n", encoding="utf-8")
    assert sh.latest_session_goal(str(p)) == ""


def test_capture_posts_goal_with_igio_hint(tmp_path):
    tp = _transcript(tmp_path, [("make IGIO reflect the session", False)])
    posted = {}

    def _fake_urlopen(req, timeout=None):
        posted["url"] = req.full_url
        posted["body"] = json.loads(req.data.decode())
        return MagicMock()

    with patch.object(sh, "_SESSION_GOAL_STATE", str(tmp_path / "state.json")), \
         patch.object(sh, "device_headers", lambda: {}), \
         patch.object(sh.urllib.request, "urlopen", side_effect=_fake_urlopen):
        sh.capture_session_goal({"transcript_path": tp, "session_id": "s1"}, "jwt")

    assert posted["url"].endswith("/memory/put")
    assert posted["body"]["igio_hint"] == "goal"          # sets axis directly
    assert posted["body"]["content"] == "make IGIO reflect the session"
    assert posted["body"]["source_id"].startswith("session_goal:")
    assert posted["body"]["source_type"] == "session_goal"


def test_capture_dedups_unchanged_goal(tmp_path):
    tp = _transcript(tmp_path, [("same goal", False)])
    calls = []

    def _fake_urlopen(req, timeout=None):
        calls.append(1)
        return MagicMock()

    with patch.object(sh, "_SESSION_GOAL_STATE", str(tmp_path / "state.json")), \
         patch.object(sh, "device_headers", lambda: {}), \
         patch.object(sh.urllib.request, "urlopen", side_effect=_fake_urlopen):
        sh.capture_session_goal({"transcript_path": tp, "session_id": "s1"}, "jwt")
        sh.capture_session_goal({"transcript_path": tp, "session_id": "s1"}, "jwt")

    assert len(calls) == 1  # unchanged goal → only posted once (per-session marker)


def test_capture_noop_without_goal(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"type": "user", "message": {"content": "x"}}) + "\n", encoding="utf-8")
    calls = []
    with patch.object(sh, "_SESSION_GOAL_STATE", str(tmp_path / "state.json")), \
         patch.object(sh, "device_headers", lambda: {}), \
         patch.object(sh.urllib.request, "urlopen", side_effect=lambda *a, **k: calls.append(1)):
        sh.capture_session_goal({"transcript_path": str(p), "session_id": "s1"}, "jwt")
    assert not calls  # no goal_status → no POST
