#!/usr/bin/env python3
"""Start conversation_watcher.py if not already running.

Called by ~/.claude/settings.json hooks.UserPromptSubmit.
Idempotent via PID file — safe to run on every prompt.
Always exits 0.
"""
import os
import subprocess
import sys
from pathlib import Path

_REPO = Path("/home/nileneb/Desktop/MayringCoder")
_WATCHER = _REPO / "tools/conversation_watcher.py"
_PYTHON = _REPO / ".venv/bin/python"
_TOKEN_FILE = Path.home() / ".config/mayring/hook.jwt"
_PID_FILE = Path.home() / ".cache/mayryngcoder/watcher.pid"
_LOG_FILE = Path.home() / ".cache/mayryngcoder/watcher.log"


def _is_running() -> bool:
    if not _PID_FILE.exists():
        return False
    try:
        pid = int(_PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, ValueError, OSError):
        return False


def _start() -> None:
    env = os.environ.copy()
    if _TOKEN_FILE.exists():
        env["MAYRING_JWT"] = _TOKEN_FILE.read_text().strip()

    python_path = _PYTHON if _PYTHON.exists() else Path(sys.executable)
    if not python_path.is_file():
        return
    if not _WATCHER.is_file():
        return

    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    cmd: list[str] = [str(python_path), str(_WATCHER)]
    with open(_LOG_FILE, "a") as log:
        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=str(_REPO),
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    _PID_FILE.write_text(str(proc.pid))


if _WATCHER.exists() and not _is_running():
    _start()
