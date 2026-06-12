#!/usr/bin/env python3
"""Shared robust /memory/put path for the MayringCoder hooks.

ONE ingest path for every summary-class memory a hook writes: the compact
recap (= the session OUTCOME) and the session /goal. Before this module each
hook carried its own half-implementation:
  - postcompact_hook had token-refresh-on-401 but NO retry and NO local queue
    → every deploy-window 5xx silently dropped the recap ("Outcome verpufft").
  - stop_hook.capture_session_goal had a 1-shot 502 retry but no queue.

Consolidating gives all of them: retry(502/503/504) + refresh-on-401 + device
headers + local queue-on-persistent-failure (replayed by session_start on the
next SessionStart). The queue entry carries its own ``endpoint`` so the drain
replays to the right route (back-compat: a missing endpoint → micro-batch).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

_JWT_FILE = os.path.expanduser("~/.config/mayring/hook.jwt")
_INGEST_QUEUE = os.path.expanduser("~/.config/mayring/ingest_queue.jsonl")
_API_URL = os.environ.get("MAYRING_API_URL", "https://mcp.linn.games").rstrip("/")
_LARAVEL_URL = os.environ.get("LARAVEL_INTERNAL_URL", "https://app.linn.games").rstrip("/")
_TIMEOUT = 15

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _device import device_headers
except ImportError:
    def device_headers() -> dict:  # type: ignore[misc]
        return {}


def _save_token(token: str) -> None:
    try:
        os.makedirs(os.path.dirname(_JWT_FILE), exist_ok=True)
        with open(_JWT_FILE, "w") as f:
            f.write(token)
    except OSError:
        pass


def refresh_token(old_token: str, *, laravel_url: str = _LARAVEL_URL) -> str:
    """Mint a fresh hook JWT (7d sliding window) when the current one 401s."""
    try:
        req = urllib.request.Request(
            f"{laravel_url}/api/mayring/refresh-token",
            headers={"Authorization": f"Bearer {old_token}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            new_token = json.loads(resp.read()).get("token", "")
        if new_token:
            _save_token(new_token)
        return new_token
    except Exception:
        return ""


def _enqueue(endpoint: str, body_json: str, reason: str) -> None:
    """Append a failed POST to the shared ingest queue, tagged with its endpoint.

    session_start._drain_ingest_queue replays it on the next SessionStart.
    """
    try:
        os.makedirs(os.path.dirname(_INGEST_QUEUE), exist_ok=True)
        entry = json.dumps({
            "endpoint": endpoint,
            "body": body_json,
            "queued_at": time.time(),
            "reason": reason,
        })
        with open(_INGEST_QUEUE, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except OSError as exc:
        sys.stderr.write(f"[_memory_put] could not enqueue: {exc}\n")


def put_memory(
    content: str,
    source_id: str,
    source_type: str,
    token: str,
    *,
    categorize: bool = True,
    api_url: str = _API_URL,
    timeout: float = _TIMEOUT,
    enqueue: bool = True,
    allow_refresh: bool = True,
) -> int:
    """POST /memory/put with retry + refresh-on-401 + queue-on-failure.

    Returns the HTTP status: 200 on success, the 4xx code when dropped
    (no retry can fix a malformed/unknown payload), or 0 on 5xx/network
    failure after the body was (optionally) queued for replay. Never
    raises — safe to call from any hook.
    """
    if not content.strip() or not token:
        return 0
    body_dict: dict = {
        "source_id": source_id,
        "source_type": source_type,
        "content": content,
        "categorize": categorize,
    }
    body_json = json.dumps(body_dict)
    payload = body_json.encode()

    def _do(tok: str) -> None:
        req = urllib.request.Request(
            f"{api_url}/memory/put",
            data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {tok}", **device_headers()},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=timeout)

    backoff = 0.6
    for attempt in range(3):
        try:
            _do(token)
            return 200
        except urllib.error.HTTPError as e:
            if e.code == 401 and allow_refresh:
                fresh = refresh_token(token)
                if fresh:
                    token = fresh
                    allow_refresh = False
                    continue
                sys.stderr.write("[_memory_put] 401 + refresh failed (dropped)\n")
                return 401
            if e.code in (502, 503, 504) and attempt < 2:
                time.sleep(backoff)
                backoff *= 2
                continue
            if 400 <= e.code < 500:
                sys.stderr.write(f"[_memory_put] HTTP {e.code} (dropped, no retry)\n")
                return e.code
            if enqueue:
                _enqueue("/memory/put", body_json, f"http_{e.code}")
            sys.stderr.write(f"[_memory_put] HTTP {e.code} → queued for replay\n")
            return 0
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            if attempt < 2:
                time.sleep(backoff)
                backoff *= 2
                continue
            if enqueue:
                _enqueue("/memory/put", body_json, type(e).__name__)
            sys.stderr.write(f"[_memory_put] {type(e).__name__} → queued for replay\n")
            return 0
    return 0
