"""Stabile Geräte-Identität + best-effort Device↔Cloud-Reporting.

mayring-claude-plugin#5 (Companion: MayringCoder#274). Das Plugin schickt eine
stabile ``device_id`` als ``X-Device-Id``-Header auf allen Cloud-Calls, damit die
Cloud Hook-Telemetrie, Worker-Registry und Write-Job-Routing nach
``(workspace_id aus JWT, device_id aus Header)`` schlüsseln kann.

device_id ist orthogonal zum JWT (JWT = User/Workspace, Header = Gerät) und wird
einmal generiert + persistiert — analog zum mayring-pi-agent ``worker_id``.

ALLE Netz-Calls hier sind BEST-EFFORT: Registrierung/Reporting/Heartbeat dürfen
einen Hook NIEMALS blockieren (der Cloud-Endpoint #274 ist evtl. noch nicht
deployt). Fehler werden geschluckt — das ist die dokumentierte Design-Constraint,
kein verstecktes Error-Swallowing.
"""
from __future__ import annotations

import json
import os
import platform
import socket
import urllib.error
import urllib.request
import uuid
from pathlib import Path

_API_URL = os.environ.get("MAYRING_API_URL", "https://mcp.linn.games").rstrip("/")
_JWT_FILE = Path(os.path.expanduser("~/.config/mayring/hook.jwt"))
_DEVICE_ID_FILE = Path(os.path.expanduser("~/.config/mayring/device_id"))
_TIMEOUT = 5.0


def device_id() -> str:
    """Stabile device_id; einmal generiert + in ~/.config/mayring/device_id persistiert."""
    try:
        if _DEVICE_ID_FILE.exists():
            existing = _DEVICE_ID_FILE.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    except OSError:
        pass

    new_id = f"dev_{uuid.uuid4().hex[:16]}"
    try:
        _DEVICE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DEVICE_ID_FILE.write_text(new_id, encoding="utf-8")
    except OSError:
        pass  # nicht persistierbar → flüchtige id für diese Session, Funktion bleibt
    return new_id


def device_headers() -> dict[str, str]:
    """Header zum Mergen in bestehende Cloud-Call-Header."""
    return {"X-Device-Id": device_id()}


def capabilities() -> list[str]:
    """Capabilities dieses Geräts. 'write' nur wenn der lokale Pi-Worker es erlaubt.

    Quelle: PI_WORKER_CAPABILITIES (komma-separiert, wie mayring-pi-agent
    pi_worker.py). Default: read-only.
    """
    raw = os.environ.get("PI_WORKER_CAPABILITIES", "")
    caps = [c.strip().lower() for c in raw.split(",") if c.strip()]
    return caps or ["read"]


def _read_token() -> str | None:
    try:
        token = _JWT_FILE.read_text(encoding="utf-8").strip()
        return token or None
    except OSError:
        return None


def _post(path: str, body: dict, token: str | None) -> None:
    """Best-effort POST mit device-Header. Schluckt JEDEN Fehler.

    WHY(#5): optionale Telemetrie — der Cloud-Endpoint (#274) ist evtl. noch
    nicht deployt; ein Fehler hier darf den Hook NIEMALS blockieren.
    """
    if token is None:
        token = _read_token()
    if not token:
        return
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{_API_URL}{path}",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            **device_headers(),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            resp.read()
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        pass


def register_device(token: str | None = None) -> None:
    """Einmalige (idempotente) Geräte-Registrierung. Best-effort."""
    _post(
        "/devices/register",
        {
            "device_id": device_id(),
            "name": socket.gethostname(),
            "os": platform.platform(),
            "capabilities": capabilities(),
        },
        token,
    )


def report_hook_event(hook_type: str, token: str | None = None, summary: str = "") -> None:
    """Ein Hook-Firing melden (UserPromptSubmit/Stop/SessionStart). Best-effort."""
    _post(
        "/hooks/events",
        {"device_id": device_id(), "hook_type": hook_type, "summary": summary[:200]},
        token,
    )


def heartbeat(token: str | None = None) -> None:
    """last_seen aktualisieren. Best-effort."""
    _post("/devices/heartbeat", {"device_id": device_id()}, token)
