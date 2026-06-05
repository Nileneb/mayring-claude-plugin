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


def test_todowrite_posts_each_todo_and_patches_status():
    payload = {
        "tool_name": "TodoWrite",
        "tool_input": {"todos": [
            {"content": "wire reranker", "status": "pending", "activeForm": "wiring reranker"},
            {"content": "ship the fix", "status": "in_progress", "activeForm": "shipping the fix"},
        ]},
        "tool_response": {},
    }
    calls = []
    with patch.object(tc, "_read_token", return_value="jwt"), \
         patch.object(tc, "_post", side_effect=lambda *a, **k: calls.append(a)):
        tc.handle(payload)
    posts = [c for c in calls if c[0] == "POST"]
    patches = [c for c in calls if c[0] == "PATCH"]
    assert len(posts) == 2, "jede native Todo-Zeile -> POST /tasks"
    assert all(p[1] == "/tasks" for p in posts)
    assert {p[2]["title"] for p in posts} == {"wire reranker", "ship the fix"}
    assert all(p[2]["external_id"].startswith("todo:") for p in posts)
    # nur die in_progress-Zeile bekommt einen Status-PATCH (pending=open default)
    assert len(patches) == 1
    assert patches[0][1].startswith("/tasks/by-external/todo:")
    assert patches[0][2]["status"] == "in_progress"


def test_todowrite_skips_empty_content():
    payload = {"tool_name": "TodoWrite",
               "tool_input": {"todos": [{"content": "", "status": "pending"}]},
               "tool_response": {}}
    calls = []
    with patch.object(tc, "_read_token", return_value="jwt"), \
         patch.object(tc, "_post", side_effect=lambda *a, **k: calls.append(a)):
        tc.handle(payload)
    assert calls == []
