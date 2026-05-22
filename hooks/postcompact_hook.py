#!/usr/bin/env python3
"""PostCompact hook — ingests Claude Code compact summary into MayringCoder memory.

Called by ~/.claude/settings.json hooks.PostCompact.
Receives {"summary": "..."} as JSON via stdin.
Always exits 0 — never blocks the compact flow.

Auth: reads JWT from ~/.config/mayring/hook.jwt (user token from app.linn.games/mayring/watcher).
On 401: tries to refresh via app.linn.games/api/mayring/refresh-token (7d sliding window).
"""
import datetime
import hashlib
import json
import os
import sys
import zoneinfo

_TZ = zoneinfo.ZoneInfo("Europe/Berlin")
import urllib.error
import urllib.request

_JWT_FILE = os.path.expanduser("~/.config/mayring/hook.jwt")
_LARAVEL_URL = os.environ.get("LARAVEL_INTERNAL_URL", "https://app.linn.games").rstrip("/")
_API_URL = os.environ.get("MAYRING_API_URL", "https://mcp.linn.games").rstrip("/")


def _read_token() -> str:
    try:
        with open(_JWT_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def _save_token(token: str) -> None:
    try:
        os.makedirs(os.path.dirname(_JWT_FILE), exist_ok=True)
        with open(_JWT_FILE, "w") as f:
            f.write(token)
    except Exception:
        pass


def _refresh_token(old_token: str) -> str:
    try:
        req = urllib.request.Request(
            f"{_LARAVEL_URL}/api/mayring/refresh-token",
            headers={"Authorization": f"Bearer {old_token}"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=10)
        new_token = json.loads(resp.read()).get("token", "")
        if new_token:
            _save_token(new_token)
        return new_token
    except Exception:
        return ""


def _post(token: str, payload: bytes) -> int:
    req = urllib.request.Request(
        f"{_API_URL}/memory/put",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=15)
        return 200
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


data = json.loads(sys.stdin.read())
summary = data.get("summary", "")
if not summary.strip():
    sys.exit(0)

token = _read_token()
if not token:
    sys.exit(0)

ts = datetime.datetime.now(_TZ).strftime("%Y-%m-%d-%H%M%S")
sid = f"conversation_summary:compact-{ts}-{hashlib.sha256(summary[:64].encode()).hexdigest()[:8]}"
payload = json.dumps({
    "source_id": sid,
    "source_type": "conversation_summary",
    "content": summary,
    "categorize": True,
}).encode()

status = _post(token, payload)
if status == 401:
    fresh = _refresh_token(token)
    if fresh:
        _post(fresh, payload)
