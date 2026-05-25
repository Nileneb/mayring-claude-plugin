"""PostToolUse capture: mirror the agent's Task* tool calls into MayringCoder
/tasks (idempotent via external_id) so the IGIO-Lens intervention column shows
the real work todos. Best-effort, never blocks the tool call."""
import json
import os
import sys
import urllib.error
import urllib.request

_API = os.getenv("MAYRING_API_URL", "https://mcp.linn.games").rstrip("/")
_JWT_FILE = os.path.expanduser("~/.config/mayring/hook.jwt")
_TODO_TOOLS = {"TaskCreate", "TaskUpdate"}
_TIMEOUT = 3.0

_STATUS_MAP = {
    "pending": "open",
    "in_progress": "in_progress",
    "completed": "done",
}


def _read_token() -> str:
    try:
        with open(_JWT_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _post(method: str, path: str, body: dict, token: str) -> None:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{_API}{path}",
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT):
            pass
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        sys.stderr.write(f"[task_capture] {method} {path} failed: {e}\n")


def handle(payload: dict) -> None:
    tool_name = payload.get("tool_name")
    if tool_name not in _TODO_TOOLS:
        return
    token = _read_token()
    if not token:
        return

    if tool_name == "TaskCreate":
        ti = payload.get("tool_input") or {}
        tr = payload.get("tool_response") or {}
        subject = (ti.get("subject") or "").strip()
        task_id = (tr.get("task") or {}).get("id") or ""
        if not subject:
            return
        _post("POST", "/tasks", {
            "title": subject[:200],
            "created_by": "agent",
            "tags": "agent",
            "external_id": task_id or None,
        }, token)

    elif tool_name == "TaskUpdate":
        ti = payload.get("tool_input") or {}
        tr = payload.get("tool_response") or {}
        task_id = ti.get("taskId") or ""
        raw_status = (
            ti.get("status")
            or (tr.get("statusChange") or {}).get("to")
            or ""
        ).strip()
        mapped = _STATUS_MAP.get(raw_status)
        if not task_id or not mapped:
            return
        _post("PATCH", f"/tasks/by-external/{task_id}", {"status": mapped}, token)


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return
    try:
        handle(payload)
    except Exception as e:
        sys.stderr.write(f"[task_capture] crashed: {type(e).__name__}: {e}\n")


if __name__ == "__main__":
    main()
