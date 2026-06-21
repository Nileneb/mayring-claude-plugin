#!/usr/bin/env python3
"""SessionStart hook: idempotent bootstrap + Memory-Kontext-Injection.

Two-phase per session:
  1) Bootstrap (only when something is missing): create venv, install
     requirements-client.txt, run OAuth-PKCE for the hook JWT.
  2) Memory inject: query mcp.linn.games /search and print the result block
     so Claude sees relevant context for the user's first prompt.
"""
from __future__ import annotations

import hashlib
import glob
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error

# Module-level import des silent-skip-counter mit no-op-Fallback wenn das
# Sister-Modul nicht da ist (z.B. wenn der Hook standalone aus altem Snapshot
# läuft). Vermeidet sys.path-Manipulation pro Aufruf (Sourcery-suggestion #4)
# UND robust gegen fehlende dependency.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _silent_skip_counter import (
        recent_skip_count as _silent_skip_recent,
        reset_counter as _silent_skip_reset,
        should_warn as _silent_skip_should_warn,
    )
except ImportError:
    def _silent_skip_recent(window_hours: float = 24.0) -> int:  # type: ignore[misc]
        return 0

    def _silent_skip_reset() -> None:  # type: ignore[misc]
        pass

    def _silent_skip_should_warn(threshold: int = 5, window_hours: float = 24.0) -> bool:  # type: ignore[misc]
        return False

# Device↔Cloud-Kanal (#5): einmal pro Session registrieren + SessionStart melden.
try:
    from _device import register_device, report_hook_event, heartbeat
except ImportError:
    def register_device(*_a, **_k) -> None:  # type: ignore[misc]
        pass

    def report_hook_event(*_a, **_k) -> None:  # type: ignore[misc]
        pass

    def heartbeat(*_a, **_k) -> None:  # type: ignore[misc]
        pass

# Phase 2: einmal pro Session den DB-Codebook (Phase 1) nach session_ctx.json
# cachen, damit memory_inject pro Prompt nicht die hardcoded universal.yaml
# parst (die nur auf der dev-Maschine existierte).
try:
    from _session_ctx import write_session_ctx as _write_session_ctx
    from _session_ctx import refresh_project_colors as _refresh_project_colors
except ImportError:
    def _write_session_ctx(*_a, **_k):  # type: ignore[misc]
        return None

    def _refresh_project_colors(*_a, **_k):  # type: ignore[misc]
        return None


PLANS_DIR = os.path.expanduser("~/.claude/plans")
# C2: stable on-disk path for the statusline script so ~/.claude/settings.json's
# statusLine command target never changes across plugin updates. session_start
# keeps it in sync from the plugin copy.
STATUSLINE_STABLE = os.path.expanduser("~/.config/mayring/statusline.py")


def _sync_statusline(plugin_root: str) -> None:
    """Copy the plugin's statusline script to the stable path (best-effort)."""
    src = os.path.join(plugin_root, "statusline", "statusline.py")
    try:
        with open(src, encoding="utf-8") as f:
            content = f.read()
        os.makedirs(os.path.dirname(STATUSLINE_STABLE), exist_ok=True)
        # Only rewrite when changed — avoids needless disk churn each session.
        try:
            with open(STATUSLINE_STABLE, encoding="utf-8") as f:
                if f.read() == content:
                    return
        except OSError:
            pass
        with open(STATUSLINE_STABLE, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(STATUSLINE_STABLE, 0o755)
    except OSError:
        pass
TASK_CONTEXT_BUDGET = 800
JWT_FILE = os.path.expanduser("~/.config/mayring/hook.jwt")
FEEDBACK_QUEUE = os.path.expanduser("~/.config/mayring/feedback_queue.jsonl")
INGEST_QUEUE = os.path.expanduser("~/.config/mayring/ingest_queue.jsonl")
MAYRING_API = os.environ.get("MAYRING_API_URL", "https://mcp.linn.games").rstrip("/")


def _plugin_root() -> str:
    """Resolve the plugin's root directory.

    Prefers the runtime-provided CLAUDE_PLUGIN_ROOT; falls back to the
    grandparent of this file (claude-plugin/ when laid out via the marketplace).
    """
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env:
        return env
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _repo_root(plugin_root: str) -> str:
    """The repo containing src/api/local_mcp.py.

    Searches in order:
      1. plugin_root/.. (dev clone where claude-plugin/ lives in repo)
      2. $MAYRING_REPO_ROOT (explicit override)

    No more hardcoded path probing — auto-sync just no-ops on machines without
    a local clone, which is the right default. Set MAYRING_REPO_ROOT if you
    want plugin-file auto-sync to work.
    """
    parent = os.path.abspath(os.path.join(plugin_root, ".."))
    if os.path.isfile(os.path.join(parent, "src", "api", "local_mcp.py")):
        return parent
    env_root = os.environ.get("MAYRING_REPO_ROOT", "")
    if env_root and os.path.isfile(os.path.join(env_root, "src", "api", "local_mcp.py")):
        return os.path.abspath(env_root)
    return plugin_root


def _venv_python(venv_dir: str) -> str:
    return os.path.join(venv_dir, "bin", "python")


def _venv_is_healthy(venv_dir: str) -> bool:
    py = _venv_python(venv_dir)
    pip = os.path.join(venv_dir, "bin", "pip")
    if not (os.path.isfile(py) and os.path.isfile(pip)):
        return False
    real = os.path.realpath(py)
    return os.path.isfile(real)


def _requirements_stamp(requirements: str) -> str:
    with open(requirements, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _venv_deps_current(venv_dir: str, requirements: str) -> bool:
    # WHY: _venv_is_healthy() only checks that the interpreter binaries exist —
    # an empty venv (created but never `pip install`ed) passes it, so deps would
    # never land and hooks fall back to ambient (base) python. Stamp the venv
    # with the requirements hash and reinstall on drift.
    stamp = os.path.join(venv_dir, ".requirements-stamp")
    if not os.path.isfile(stamp):
        return False
    try:
        with open(stamp, "r", encoding="utf-8") as fh:
            return fh.read().strip() == _requirements_stamp(requirements)
    except OSError:
        return False


def _ensure_venv(plugin_root: str, repo_root: str) -> None:
    venv_dir = os.path.join(plugin_root, ".venv")
    requirements = os.path.join(repo_root, "requirements-client.txt")
    if not os.path.isfile(requirements):
        print(
            f"MayringCoder bootstrap: skipped (no {requirements}); is the marketplace clone complete?",
            file=sys.stderr,
        )
        return
    if _venv_is_healthy(venv_dir) and _venv_deps_current(venv_dir, requirements):
        return
    print(
        f"MayringCoder bootstrap: building isolated venv at {venv_dir} (one-time, ~30-60s)",
        file=sys.stderr,
    )
    try:
        # Trusted args: sys.executable is Python's own path; venv_dir derives
        # from CLAUDE_PLUGIN_ROOT (or this file's location). No user-controlled input.
        subprocess.run(  # nosec B603
            [sys.executable, "-m", "venv", "--clear", venv_dir],
            check=True,
            capture_output=True,
        )
        subprocess.run(  # nosec B603
            [os.path.join(venv_dir, "bin", "pip"), "install", "-q", "-r", requirements],
            check=True,
            capture_output=True,
        )
        with open(os.path.join(venv_dir, ".requirements-stamp"), "w", encoding="utf-8") as fh:
            fh.write(_requirements_stamp(requirements))
        print("MayringCoder bootstrap: venv ready", file=sys.stderr)
    except subprocess.CalledProcessError as e:
        print(
            f"MayringCoder bootstrap: venv setup failed — {e.stderr.decode(errors='ignore')[:300]}",
            file=sys.stderr,
        )


def _have_jwt() -> bool:
    return os.path.isfile(JWT_FILE) and os.path.getsize(JWT_FILE) > 0


def _ensure_jwt(repo_root: str, python_executable: str) -> None:
    if _have_jwt():
        return
    oauth_script = os.path.join(repo_root, "tools", "oauth_install.py")
    if not os.path.isfile(oauth_script):
        print(
            "MayringCoder bootstrap: JWT setup skipped (oauth_install.py missing in repo)",
            file=sys.stderr,
        )
        return
    print(
        "MayringCoder bootstrap: opening browser for hook JWT (OAuth PKCE)",
        file=sys.stderr,
    )
    try:
        # Trusted argv: python_executable resolves from venv-or-sys.executable,
        # oauth_script + JWT_FILE are module constants — no user input.
        subprocess.run(  # nosec B603
            [python_executable, oauth_script, "--jwt-file", JWT_FILE],
            check=True,
            timeout=180,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print(
            "MayringCoder bootstrap: JWT setup failed — run tools/oauth_install.py manually",
            file=sys.stderr,
        )


def _bootstrap_if_needed() -> None:
    plugin_root = _plugin_root()
    repo_root = _repo_root(plugin_root)
    _ensure_venv(plugin_root, repo_root)
    venv_dir = os.path.join(plugin_root, ".venv")
    python_executable = _venv_python(venv_dir) if _venv_is_healthy(venv_dir) else sys.executable
    _ensure_jwt(repo_root, python_executable)


def _load_token() -> str:
    token = os.getenv("MCP_SERVICE_TOKEN", "")
    if token:
        return token
    try:
        with open(JWT_FILE) as f:
            content = f.read().strip()
            if content:
                return content
    except OSError:
        pass
    for env_file in [
        os.path.expanduser("~/app.linn.games/.env.mayring"),
        os.path.expanduser("~/.env.mayring"),
    ]:
        try:
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("MCP_SERVICE_TOKEN="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def _latest_plan_context() -> str:
    try:
        files = glob.glob(os.path.join(PLANS_DIR, "*.md"))
        if not files:
            return ""
        latest = max(files, key=os.path.getmtime)
        with open(latest) as f:
            content = f.read()
        if "## Context" in content:
            after = content.split("## Context", 1)[1]
            if "\n## " in after:
                after = after.split("\n## ", 1)[0]
            return after.strip()[:TASK_CONTEXT_BUDGET]
        return content[:TASK_CONTEXT_BUDGET]
    except Exception:
        return ""


def _fetch_open_tasks(api: str, token: str, top_k: int = 5) -> list[dict]:
    """GET /tasks?status=open — returns non-completed tasks for the workspace."""
    req = urllib.request.Request(
        f"{api}/tasks?status=open",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
        tasks = data.get("tasks", [])
        return [t for t in tasks if (t.get("status") or "") != "completed"][:top_k]
    except Exception:
        return []


def _inject_memory(payload: dict) -> None:
    token = _load_token()
    if not token:
        return

    api = os.getenv("MAYRING_API_URL", "https://mcp.linn.games").rstrip("/")
    initial_message = (
        payload.get("message")
        or payload.get("prompt")
        or payload.get("transcript", [{}])[0].get("content", "")
        or "session start"
    )
    body_dict: dict = {
        "query": str(initial_message)[:300],
        "top_k": 5,
        "include_text": True,
        "char_budget": 3000,
    }
    task_context = _latest_plan_context()
    if task_context:
        body_dict["task_context"] = task_context

    body = json.dumps(body_dict).encode()
    req = urllib.request.Request(
        f"{api}/search",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    context = ""
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            result = json.loads(resp.read())
        context = result.get("prompt_context", "").strip()
    except Exception:
        pass

    tasks = _fetch_open_tasks(api, token)
    tasks_block = ""
    if tasks:
        lines = []
        for t in tasks:
            title = (t.get("title") or t.get("content") or "").strip()
            status = (t.get("status") or "pending").strip()
            if title:
                lines.append(f"- [{status}] {title[:200]}")
        if lines:
            tasks_block = "## Offene Tasks\n\n" + "\n".join(lines) + "\n\n"

    if context or tasks_block:
        print(
            f"{tasks_block}"
            f"## Automatisch injizierter Memory-Kontext\n\n{context}\n\n"
            "⚠️  PFLICHT: Gib am Ende jedes Tasks Feedback via "
            "`mcp__claude_ai_Memory__feedback` für jeden genutzten Chunk."
        )


def _sync_plugin_files_from_repo(plugin_root: str, repo_root: str) -> None:
    """Mirror claude-plugin/hooks/*.py and hooks.json from the local repo into
    the marketplace plugin cache so a fresh `git push` is reflected without
    requiring the user to run `/plugin update mayring-coder` manually.

    Only runs when:
      1. plugin_root != repo_root (i.e. we ARE the marketplace cache copy, not
         the dev copy itself), AND
      2. The repo has a newer mtime on at least one hook file.

    No-op when the hook is invoked from a dev clone (plugin_root == repo_root)
    or the repo isn't accessible.
    """
    if os.path.abspath(plugin_root) == os.path.abspath(repo_root):
        return
    src_dir = os.path.join(repo_root, "claude-plugin", "hooks")
    dst_dir = os.path.join(plugin_root, "hooks")
    if not os.path.isdir(src_dir) or not os.path.isdir(dst_dir):
        return
    import shutil
    synced: list[str] = []
    try:
        for name in os.listdir(src_dir):
            if not (name.endswith(".py") or name.endswith(".json")):
                continue
            src = os.path.join(src_dir, name)
            dst = os.path.join(dst_dir, name)
            try:
                src_mtime = os.path.getmtime(src)
                dst_mtime = os.path.getmtime(dst) if os.path.isfile(dst) else 0
                if src_mtime > dst_mtime + 1.0:  # 1s slack vs filesystem precision
                    shutil.copy2(src, dst)
                    synced.append(name)
            except OSError:
                continue
    except OSError:
        return
    if synced:
        print(f"MayringCoder plugin hooks synced from repo: {', '.join(synced)}", file=sys.stderr)


def _drain_feedback_queue() -> None:
    """Replay queued feedback events against REST /memory/feedback.

    Solves the second half of Issue #138: when the MCP session was dead,
    `mcp__claude_ai_Memory__feedback` calls failed silently and the
    intended signal was lost. The CLI fallback (`mayring-feedback`)
    appends to ~/.config/mayring/feedback_queue.jsonl; this drain runs
    on every SessionStart and ships the entries via REST (which is
    session-immune — it just reads the JWT from disk).

    Successful POSTs are removed from the queue. Failed POSTs stay so
    the next session can retry. Never blocks SessionStart on errors.
    """
    if not os.path.isfile(FEEDBACK_QUEUE):
        return
    try:
        with open(FEEDBACK_QUEUE, encoding="utf-8") as f:
            lines = [ln for ln in (l.strip() for l in f) if ln]
    except OSError:
        return
    if not lines:
        try:
            os.remove(FEEDBACK_QUEUE)
        except OSError:
            pass
        return
    try:
        with open(JWT_FILE, encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        return
    if not token:
        return

    remaining: list[str] = []
    posted = 0
    for raw in lines:
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue  # corrupted line, drop it
        body = json.dumps({
            "chunk_id": entry.get("chunk_id"),
            "signal": entry.get("signal"),
            "metadata": entry.get("metadata") or {},
        }).encode()
        req = urllib.request.Request(
            f"{MAYRING_API}/memory/feedback",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as _:
                posted += 1
        except urllib.error.HTTPError as e:
            # 4xx → client error (chunk gone, invalid signal): drop the
            # entry, no retry will fix it. 5xx → keep for next session.
            if 400 <= e.code < 500:
                continue
            remaining.append(raw)
        except Exception:
            remaining.append(raw)

    try:
        if remaining:
            with open(FEEDBACK_QUEUE, "w", encoding="utf-8") as f:
                f.write("\n".join(remaining) + "\n")
        else:
            os.remove(FEEDBACK_QUEUE)
    except OSError:
        pass

    if posted:
        print(
            f"MayringCoder feedback queue: replayed {posted} entries "
            f"({len(remaining)} pending)",
            file=sys.stderr,
        )


def _drain_ingest_queue() -> None:
    """Replay queued ingest payloads to their tagged endpoint.

    stop_hook (/conversation/micro-batch) and _memory_put (/memory/put)
    enqueue here when the server returns 5xx after retries. Each entry
    carries its own ``endpoint``; legacy entries without one default to
    /conversation/micro-batch (back-compat). Same drop-on-4xx /
    keep-on-5xx semantics as the feedback drain.
    """
    if not os.path.isfile(INGEST_QUEUE):
        return
    try:
        with open(INGEST_QUEUE, encoding="utf-8") as f:
            lines = [ln for ln in (l.strip() for l in f) if ln]
    except OSError:
        return
    if not lines:
        try:
            os.remove(INGEST_QUEUE)
        except OSError:
            pass
        return
    try:
        with open(JWT_FILE, encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        return
    if not token:
        return

    remaining: list[str] = []
    posted = 0
    for raw in lines:
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        body_json = entry.get("body")
        if not isinstance(body_json, str):
            continue
        endpoint = entry.get("endpoint") or "/conversation/micro-batch"
        body_bytes = body_json.encode("utf-8")
        req = urllib.request.Request(
            f"{MAYRING_API}{endpoint}",
            data=body_bytes,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as _:
                posted += 1
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                # 4xx → drop (malformed payload won't fix itself)
                continue
            remaining.append(raw)
        except Exception:
            remaining.append(raw)

    try:
        if remaining:
            with open(INGEST_QUEUE, "w", encoding="utf-8") as f:
                f.write("\n".join(remaining) + "\n")
        else:
            os.remove(INGEST_QUEUE)
    except OSError:
        pass

    if posted:
        print(
            f"MayringCoder ingest queue: replayed {posted} entries "
            f"({len(remaining)} pending)",
            file=sys.stderr,
        )


def _warn_if_silent_skips_accumulated() -> None:
    """V2 Stufe 2.2: zeige einmal pro Session den Stand des silent-skip-counters.

    Wenn memory_inject in 24h ≥5 silent-skips gemacht hat (alle 3 lenses
    5xx → kein lauter Block mehr, weil deploy-typisch), ist das Pattern
    chronisch und braucht User-Aktion. Banner wird gezeigt + counter
    reset, damit nicht jede Session denselben Banner zeigt.
    """
    if not _silent_skip_should_warn(threshold=5):
        return
    try:
        n = _silent_skip_recent(window_hours=24)
        print(
            f"## Memory-Hook: chronische silent-skips ({n}/24h)\n"
            f"_Der UserPromptSubmit-Hook hat zuletzt {n}× nichts injiziert "
            f"(alle 3 lens-searches → 5xx). Wenn das anhält: API healthcheck "
            f"`curl https://mcp.linn.games/health` oder `/reload-plugins`._"
        )
        _silent_skip_reset()
    except OSError as e:
        print(f"MayringCoder: skip-counter check failed — {e}", file=sys.stderr)


if __name__ == "__main__":
    plugin_root = _plugin_root()
    repo_root = _repo_root(plugin_root)
    _sync_plugin_files_from_repo(plugin_root, repo_root)
    _bootstrap_if_needed()
    register_device()  # einmal pro Session, idempotent, best-effort (#5)
    report_hook_event("SessionStart")
    heartbeat()
    _drain_feedback_queue()
    _drain_ingest_queue()
    _warn_if_silent_skips_accumulated()
    _write_session_ctx(_load_token())  # Phase 2: DB-codebook → session_ctx.json
    _sync_statusline(plugin_root)               # C2: keep stable statusline script fresh
    _refresh_project_colors(_load_token(), max_age=0)  # C2: prime colour cache for this session
    payload = json.loads(sys.stdin.read() or "{}")
    _inject_memory(payload)
