"""Unit tests for task_capture.py PostToolUse hook.
Uses importlib to load the module directly (same pattern as test_session_ctx_router.py).
"""
import importlib.util
from pathlib import Path
from unittest.mock import patch

_mod_path = Path(__file__).parent / "task_capture.py"
spec = importlib.util.spec_from_file_location("task_capture", _mod_path)
tc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tc)


def test_taskcreate_posts_to_tasks():
    payload = {
        "tool_name": "TaskCreate",
        "tool_input": {"subject": "fix the widget", "description": "details"},
        "tool_response": {"task": {"id": "harness-7", "subject": "fix the widget"}},
    }
    calls = []
    with patch.object(tc, "_read_token", return_value="jwt"), \
         patch.object(tc, "_post", side_effect=lambda *a, **k: calls.append(a)):
        tc.handle(payload)
    assert calls, "TaskCreate must POST /tasks"
    method, path, body = calls[0][0], calls[0][1], calls[0][2]
    assert method == "POST"
    assert path == "/tasks"
    assert body["title"] == "fix the widget"
    assert body["external_id"] == "harness-7"
    assert body["created_by"] == "agent"


def test_taskupdate_completed_patches_by_external():
    payload = {
        "tool_name": "TaskUpdate",
        "tool_input": {"taskId": "harness-7", "status": "completed"},
        "tool_response": {"taskId": "harness-7", "statusChange": {"from": "in_progress", "to": "completed"}},
    }
    calls = []
    with patch.object(tc, "_read_token", return_value="jwt"), \
         patch.object(tc, "_post", side_effect=lambda *a, **k: calls.append(a)):
        tc.handle(payload)
    assert calls, "TaskUpdate must PATCH /tasks/by-external/{id}"
    method, path, body = calls[0][0], calls[0][1], calls[0][2]
    assert method == "PATCH"
    assert path == "/tasks/by-external/harness-7"
    assert body["status"] == "done"


def test_non_todo_tool_is_noop():
    calls = []
    with patch.object(tc, "_read_token", return_value="jwt"), \
         patch.object(tc, "_post", side_effect=lambda *a, **k: calls.append(a)):
        tc.handle({"tool_name": "Bash", "tool_input": {}, "tool_response": {}})
    assert calls == []


def test_no_token_is_silent_skip():
    with patch.object(tc, "_read_token", return_value=""):
        # must not raise
        tc.handle({
            "tool_name": "TaskCreate",
            "tool_input": {"subject": "x"},
            "tool_response": {"task": {"id": "1"}},
        })
