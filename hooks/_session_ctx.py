#!/usr/bin/env python3
"""Shared session-context for the Mayring hooks (v2 Phase 2).

`session_start.py` writes ``~/.cache/mayring/session_ctx.json`` once per
session from the DB-codebook API (Phase 1). `memory_inject.py` reads it per
prompt instead of parsing a hardcoded local ``universal.yaml`` — that path only
ever existed on one dev machine, so the codebook silently fell back to a 10-word
stub on every other device (exactly the "hooks vom Gerät" breakage).

Fail-soft everywhere: API down or file missing/stale → bundled YAML → minimal
hardcoded set. The hook never breaks because the codebook could not load.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

SESSION_CTX_PATH = os.path.expanduser("~/.cache/mayring/session_ctx.json")
CTX_TTL = 6 * 3600  # re-fetch when older than 6h
DEFAULT_SLUG = os.environ.get("MAYRING_CODEBOOK_SLUG", "universal")

_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
# YAML fallbacks: bundled-in-plugin first (device-portable), then the legacy
# dev path, then a flat newline list. Only hit when the API is unreachable.
_YAML_FALLBACKS = [
    os.path.join(_HOOK_DIR, "..", "codebooks", "universal.yaml"),
    "/home/nileneb/Desktop/MayringCoder/codebooks/profiles/universal.yaml",
    os.path.expanduser("~/.config/mayring/categories.txt"),
]
_MINIMAL = [
    "api", "data_access", "domain", "infrastructure", "auth",
    "config", "utils", "testing", "frontend", "deployment",
]


def _api() -> str:
    return os.environ.get("MAYRING_API_URL", "https://mcp.linn.games").rstrip("/")


def _get_json(url: str, token: str, timeout: float = 6.0) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_codebook(token: str, slug: str = DEFAULT_SLUG) -> dict:
    """GET /codebooks/{slug} + /codebooks/{id}/categories?status=active.

    Raises on transport/HTTP error — callers wrap in try/except and fall back.
    """
    api = _api()
    cb = _get_json(f"{api}/codebooks/{slug}", token)
    cid = cb["id"]
    cats = _get_json(f"{api}/codebooks/{cid}/categories?status=active", token)
    return {
        "slug": cb.get("slug", slug),
        "id": cid,
        "version": cb.get("version"),
        "categories": [
            {
                "name": c["name"],
                "igio_axis": c.get("igio_axis"),
                "description": c.get("description", ""),
                "evidence_count": c.get("evidence_count", 0),
            }
            for c in cats.get("categories", [])
            if c.get("name")
        ],
        "fetched_at": time.time(),
    }


def write_session_ctx(token: str, slug: str = DEFAULT_SLUG) -> dict | None:
    """Fetch the codebook and persist it. Best-effort — returns ctx or None."""
    try:
        ctx = fetch_codebook(token, slug)
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None
    try:
        os.makedirs(os.path.dirname(SESSION_CTX_PATH), exist_ok=True)
        with open(SESSION_CTX_PATH, "w", encoding="utf-8") as f:
            json.dump(ctx, f)
    except OSError:
        pass
    return ctx


def read_session_ctx(max_age: float = CTX_TTL) -> dict | None:
    """Read the cached ctx; None if missing, unparsable, or stale."""
    try:
        with open(SESSION_CTX_PATH, encoding="utf-8") as f:
            ctx = json.load(f)
    except (OSError, ValueError):
        return None
    if max_age and (time.time() - float(ctx.get("fetched_at", 0))) > max_age:
        return None
    return ctx


def _yaml_categories() -> list[str]:
    for p in _YAML_FALLBACKS:
        p = os.path.abspath(p)
        if not os.path.exists(p):
            continue
        try:
            content = open(p, encoding="utf-8").read()
        except OSError:
            continue
        cats: list[str] = []
        in_cats = False
        for line in content.splitlines():
            s = line.strip()
            if s.startswith("categories:"):
                in_cats = True
                continue
            if in_cats:
                if s.startswith("- "):
                    cat = s[2:].strip().rstrip(":")
                    if cat and not cat.startswith("#"):
                        cats.append(cat)
                elif s and not s.startswith("#") and ":" in s:
                    break
        if cats:
            return cats
    return []


def load_active_categories(token: str = "") -> list[str]:
    """Category names for prompt-categorize. Source priority:
      1. DB-codebook via session_ctx.json (any device, Phase 1)
      2. fetch+cache once if session_start has not run yet (token given)
      3. bundled/legacy YAML
      4. minimal hardcoded set
    Never returns empty.
    """
    ctx = read_session_ctx()
    if ctx is None and token:
        ctx = write_session_ctx(token)
    if ctx and ctx.get("categories"):
        return [c["name"] for c in ctx["categories"] if c.get("name")]
    return _yaml_categories() or list(_MINIMAL)


# --- Project Router (Slice 1) ----------------------------------------------

_IMPERATIVES = re.compile(
    r"\b(implementier\w*|implement|fix|repariere?|debug\w*|analysier\w*|analyze|"
    r"refactor\w*|erstell\w*|create|add|füge|baue?|build|teste?|test|deploy\w*|"
    r"migrier\w*|migrate|untersuch\w*|investigate|optimier\w*|optimize|review|"
    r"prüf\w*|check|schreib\w*|write|entferne?|remove|delete|lösch\w*|"
    r"update|aktualisier\w*)\b", re.IGNORECASE)


def _git_remote(cwd: str | None = None) -> str | None:
    """`git -C <cwd> remote get-url origin`, fail-soft → None."""
    try:
        r = subprocess.run(
            ["git", "-C", cwd or os.getcwd(), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=2)
        out = r.stdout.strip()
        return out or None
    except (OSError, subprocess.SubprocessError):
        return None


def route_project(token: str, cwd_remote: str | None, prompt: str) -> dict:
    """POST /projects/route, fail-soft → {project_id: None}."""
    api = _api()
    body = json.dumps({"cwd_remote": cwd_remote, "prompt": (prompt or "")[:600]}).encode()
    req = urllib.request.Request(
        f"{api}/projects/route", data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return {"project_id": None, "name": None, "mode": "unknown",
                "confidence": 0.0, "reason": "route-unreachable"}


def derive_task(prompt: str, project_name: str = "", goal: str = "") -> str:
    """Mayring-Selektionskriterium: Imperativ + Objekt, mit Projekt/Goal-Kontext.
    Regex-first; leerer String wenn nichts Sinnvolles (caller fällt zurück)."""
    p = (prompt or "").strip()
    if len(p) < 12:
        return ""
    m = _IMPERATIVES.search(p)
    seed = p[m.start():m.start() + 100].split("\n")[0].strip() if m else ""
    if not seed and goal:
        seed = goal[:80]
    if not seed:
        return ""
    prefix = f"{project_name}: " if project_name else ""
    return (prefix + seed)[:140]


def write_session_ctx_field(key: str, value) -> None:
    """Merge a single field into session_ctx.json (best-effort, TTL-agnostic)."""
    ctx = read_session_ctx(max_age=0) or {}
    ctx[key] = value
    try:
        os.makedirs(os.path.dirname(SESSION_CTX_PATH), exist_ok=True)
        with open(SESSION_CTX_PATH, "w", encoding="utf-8") as f:
            json.dump(ctx, f)
    except OSError:
        pass


# --- C2: Statusline project colors (read-only cache of C1 group colors) ------

PROJECT_COLORS_PATH = os.path.expanduser("~/.cache/mayring/project_colors.json")


def refresh_project_colors(token: str, max_age: float = 300.0) -> dict | None:
    """Cache ``{canonical_repo_ref: {color, name}}`` from GET /projects (C1's
    group_color via LEFT JOIN) so the statusline (C2) can colour the current repo
    WITHOUT an API call per render tick. TTL-skip when fresh; best-effort — any
    failure leaves the existing cache untouched and returns None. The keys are the
    canonical source_ref form that /projects already stores (github → owner/name),
    which the statusline mirrors from the cwd's git remote."""
    if not token:
        return None
    try:
        if (time.time() - os.path.getmtime(PROJECT_COLORS_PATH)) < max_age:
            return None  # still fresh — skip the call
    except OSError:
        pass  # missing/unreadable → (re)build
    try:
        data = _get_json(f"{_api()}/projects", token)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    colors = {
        p["repo"]: {"color": p.get("group_color") or "", "name": p.get("name") or ""}
        for p in data.get("projects", [])
        if p.get("repo")
    }
    try:
        os.makedirs(os.path.dirname(PROJECT_COLORS_PATH), exist_ok=True)
        with open(PROJECT_COLORS_PATH, "w", encoding="utf-8") as f:
            json.dump({"projects": colors, "fetched_at": time.time()}, f)
    except OSError:
        pass
    return colors
