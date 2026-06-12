#!/usr/bin/env python3
"""PostCompact hook — ingests the Claude Code compact summary into MayringCoder.

The compact recap is the consolidated session summary. Ingested once via the
shared robust /memory/put path with categorize=True. The shared path handles
401-refresh, 5xx-retry and queue-on-failure.

Receives {"summary": "..."} as JSON via stdin. Always exits 0 (never blocks
the compact flow). Auth: JWT from ~/.config/mayring/hook.jwt.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
import zoneinfo

_TZ = zoneinfo.ZoneInfo("Europe/Berlin")
_JWT_FILE = os.path.expanduser("~/.config/mayring/hook.jwt")
_API_URL = os.environ.get("MAYRING_API_URL", "https://mcp.linn.games").rstrip("/")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _memory_put import put_memory
except ImportError:
    # Standalone-snapshot fallback: single-shot POST, no retry/queue/refresh.
    def put_memory(content, source_id, source_type, token, *,
                   categorize=True, **_k):  # type: ignore[misc]
        body = {"source_id": source_id, "source_type": source_type,
                "content": content, "categorize": categorize}
        try:
            req = urllib.request.Request(
                f"{_API_URL}/memory/put", data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {token}"}, method="POST")
            urllib.request.urlopen(req, timeout=15)
            return 200
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            return 0


def _read_token() -> str:
    try:
        with open(_JWT_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def ingest_recap(summary: str, token: str, *, now: datetime.datetime | None = None) -> int:
    """Ingest one compact recap into Memory. Returns HTTP status."""
    summary = (summary or "").strip()
    if not summary or not token:
        return 0
    ts = (now or datetime.datetime.now(_TZ)).strftime("%Y-%m-%d-%H%M%S")
    sid = f"conversation_summary:compact-{ts}-{hashlib.sha256(summary[:64].encode()).hexdigest()[:8]}"
    return put_memory(summary, sid, "conversation_summary", token, categorize=True)


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return
    token = _read_token()
    if not token:
        return
    ingest_recap(data.get("summary", ""), token)


if __name__ == "__main__":
    main()
