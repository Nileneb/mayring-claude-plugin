"""PostToolUse capture (SPIKE): log the raw payload for the agent's todo tools
so we learn the exact schema (tool_name + where id/title/status live) before
building the real capture. Replaced by the full implementation in Task 4 of
docs/superpowers/plans/2026-05-25-igio-intervention-todos.md."""
import datetime
import json
import os
import sys

_TODO_TOOLS = {"TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TodoWrite"}
_LOG = os.path.expanduser("~/.config/mayring/task_capture_spike.log")


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return
    if payload.get("tool_name") not in _TODO_TOOLS:
        return
    try:
        os.makedirs(os.path.dirname(_LOG), exist_ok=True)
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(datetime.datetime.now().isoformat() + " " + json.dumps(payload) + "\n")
    except OSError as e:
        sys.stderr.write(f"[task_capture spike] log failed: {e}\n")


if __name__ == "__main__":
    main()
