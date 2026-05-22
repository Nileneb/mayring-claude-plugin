"""Silent-skip-counter for memory_inject.py.

Spec: docs/v2-master-audit.md Section 7 Stufe 2.2.

Wenn der UserPromptSubmit-Hook während eines deploy-windows alle 3 lens-
searches mit 5xx zurückkommen, skippen wir den lauten warning-block (das
wäre nur noise — der User kann nichts dagegen tun). Aber wenn das ZU oft
passiert (≥5 Skips in 24h), ist es ein chronisches Problem das gemeldet
werden muss. Diese Datei trackt das.

Anti-Pattern: silent-skips dürfen nicht silent BLEIBEN. SessionStart-Hook
liest `should_warn()` und zeigt einmal pro Session "Hook hatte zuletzt N
silent skips" als sichtbarer banner.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


def _config_root() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "mayring"


def skip_log_path() -> Path:
    """File where skip-events are appended (JSON)."""
    return _config_root() / "silent-skip-counter.json"


def _load() -> dict:
    p = skip_log_path()
    if not p.exists():
        return {"events": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "events" not in data:
            return {"events": []}
        return data
    except (OSError, json.JSONDecodeError, ValueError):
        return {"events": []}


def _save(data: dict) -> None:
    p = skip_log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


def record_silent_skip(reason: str = "unknown") -> None:
    """Record one silent-skip event with current timestamp."""
    data = _load()
    data["events"].append({"ts": time.time(), "reason": reason})
    # Cap old entries — keep only last 100 to avoid unbounded growth.
    data["events"] = data["events"][-100:]
    _save(data)


def _parse_ts(evt: dict) -> float:
    """Defensive ts-parse: malformed JSON (manual edits, partial writes)
    would otherwise abort the whole counter. Bad rows count as 0 → expire."""
    try:
        return float(evt.get("ts", 0))
    except (TypeError, ValueError):
        return 0.0


def recent_skip_count(window_hours: float = 24.0) -> int:
    """Count skip-events within the last `window_hours`."""
    data = _load()
    cutoff = time.time() - window_hours * 3600
    return sum(
        1 for evt in data.get("events", [])
        if isinstance(evt, dict) and _parse_ts(evt) >= cutoff
    )


def should_warn(threshold: int = 5, window_hours: float = 24.0) -> bool:
    """True if recent_skip_count >= threshold."""
    return recent_skip_count(window_hours=window_hours) >= threshold


def reset_counter() -> None:
    """Clear the counter — used by SessionStart-Hook after showing warning."""
    p = skip_log_path()
    if p.exists():
        try:
            p.unlink()
        except OSError:
            # Datei lock-conflict (Windows) — wir loggen es laut, aber
            # das ist kein hard-error für den Hook-Flow.
            import sys
            print(
                f"[silent-skip-counter] could not delete {p}",
                file=sys.stderr,
            )
