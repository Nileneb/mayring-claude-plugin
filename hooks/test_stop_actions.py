"""Write-leak fix: the actual WORK of a turn (tool_use — Edit/Bash/Write) must
reach memory, not just the prose. _flatten_content strips tool blocks, so the
conversation_summary (and the recency-lane that surfaces it) was prose-only.

extract_turn_actions renders a compact action list since the last user prompt;
_capture_turns folds it into the assistant turn before the micro-batch POST.
"""
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

_mod_path = Path(__file__).parent / "stop_hook.py"
sys.path.insert(0, str(_mod_path.parent))
spec = importlib.util.spec_from_file_location("stop_hook", _mod_path)
sh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sh)


def _write(tmp_path, rows):
    p = tmp_path / "t.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return str(p)


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _assistant(text, tool_uses=()):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for name, inp in tool_uses:
        content.append({"type": "tool_use", "name": name, "input": inp})
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def _tool_result(out):
    # Claude Code puts tool_result blocks in a USER message (no text)
    return {"type": "user", "message": {"role": "user",
            "content": [{"type": "tool_result", "content": out}]}}


def test_render_tool_use_picks_primary_target():
    assert sh._render_tool_use({"name": "Edit", "input": {"file_path": "src/foo.py"}}) == "Edit: src/foo.py"
    assert sh._render_tool_use({"name": "Bash", "input": {"command": "pytest -q"}}) == "Bash: pytest -q"
    assert sh._render_tool_use({"name": "Read", "input": {}}) == "Read"
    assert sh._render_tool_use({"name": ""}) is None


def test_extract_actions_collects_tool_use(tmp_path):
    tp = _write(tmp_path, [
        _user("fix the bug"),
        _assistant("let me look", [("Read", {"file_path": "a.py"})]),
        _tool_result("file contents"),
        _assistant("now edit", [("Edit", {"file_path": "a.py"}), ("Bash", {"command": "pytest"})]),
        _assistant("done"),
    ])
    actions = sh.extract_turn_actions(tp)
    assert "Read: a.py" in actions
    assert "Edit: a.py" in actions
    assert "Bash: pytest" in actions


def test_extract_actions_resets_on_new_user_prompt(tmp_path):
    tp = _write(tmp_path, [
        _user("first task"),
        _assistant("", [("Write", {"file_path": "old.py"})]),
        _user("second task"),                       # ← reset window here
        _assistant("", [("Edit", {"file_path": "new.py"})]),
    ])
    actions = sh.extract_turn_actions(tp)
    assert actions == ["Edit: new.py"]               # only since the last prompt
    assert "Write: old.py" not in actions


def test_tool_result_user_turn_does_not_reset(tmp_path):
    tp = _write(tmp_path, [
        _user("do it"),
        _assistant("", [("Edit", {"file_path": "x.py"})]),
        _tool_result("ok"),                          # no text → must NOT reset
        _assistant("", [("Bash", {"command": "make"})]),
    ])
    actions = sh.extract_turn_actions(tp)
    assert actions == ["Edit: x.py", "Bash: make"]


def test_capture_turns_folds_actions_into_assistant_content(tmp_path):
    tp = _write(tmp_path, [
        _user("please refactor"),
        _assistant("I refactored it", [("Edit", {"file_path": "core.py"})]),
    ])
    posted = {}

    def _fake_post(turns, session_id, slug, token, igio_hint=None):
        posted["turns"] = turns
        return 200

    with patch.object(sh, "_post_micro_batch", side_effect=_fake_post), \
         patch.object(sh, "_workspace_slug", lambda: "bene"):
        sh._capture_turns({"transcript_path": tp, "session_id": "s1"}, "jwt")

    assistant_content = posted["turns"][1]["content"]
    assert "Edit: core.py" in assistant_content       # the WORK is now captured
    assert "Aktionen" in assistant_content


def test_capture_turns_no_actions_unchanged(tmp_path):
    tp = _write(tmp_path, [
        _user("just a question"),
        _assistant("here is the answer, no tools used"),
    ])
    posted = {}
    with patch.object(sh, "_post_micro_batch",
                      side_effect=lambda turns, *a, **k: posted.update(turns=turns) or 200), \
         patch.object(sh, "_workspace_slug", lambda: "bene"):
        sh._capture_turns({"transcript_path": tp, "session_id": "s1"}, "jwt")
    assert "Aktionen" not in posted["turns"][1]["content"]
