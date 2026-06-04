#!/usr/bin/env python3
"""C2 — projekt-gefärbte Statusline für Claude Code.

Liest das statusLine-JSON auf stdin (cwd/model), löst das Repo des aktuellen
Verzeichnisses (git remote → canonical owner/name) auf und färbt einen Punkt +
den Projektnamen in der C1-Gruppenfarbe. Die Farbe kommt aus dem lokalen Cache
``~/.cache/mayring/project_colors.json`` (von ``_session_ctx.refresh_project_colors``
befüllt) — KEIN API-Call pro Render-Tick. Reiner stdlib-Code, fail-soft: ohne
Cache/Farbe/Repo wird neutral gerendert, nie ein Fehler an Claude Code.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

_CACHE = os.path.expanduser("~/.cache/mayring/project_colors.json")
_RESET = "\033[0m"
_DIM = "\033[2m"


def _git_remote(cwd: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=1.5,
        )
        return r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _canonical_repo_ref(remote: str | None) -> str:
    """Mirror of mayring-core canonical_repo_ref for the github case: a github
    remote (https or ssh) → lowercased ``owner/name`` slug, matching the
    source_ref keys /projects stores. Non-github → lowercased, .git stripped."""
    if not remote:
        return ""
    # name may contain dots (e.g. app.linn.games); only strip a trailing .git
    m = re.search(r"github\.com[:/]+([^/]+)/(.+?)(?:\.git)?/?$", remote.strip(), re.I)
    if m:
        return f"{m.group(1).lower()}/{m.group(2).lower()}"
    return remote.strip().lower().removesuffix(".git")


def _rgb(hex_color: str | None) -> tuple[int, int, int] | None:
    h = (hex_color or "").lstrip("#")
    if len(h) != 6:
        return None
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return None


def _short_cwd(cwd: str) -> str:
    home = os.path.expanduser("~")
    if cwd == home:
        return "~"
    if cwd.startswith(home + os.sep):
        return "~/" + os.path.relpath(cwd, home)
    return cwd


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        data = {}

    ws = data.get("workspace") or {}
    cwd = ws.get("current_dir") or data.get("cwd") or os.getcwd()
    model_obj = data.get("model") or {}
    model = model_obj.get("display_name") or model_obj.get("id") or ""

    ref = _canonical_repo_ref(_git_remote(cwd))
    color = name = None
    try:
        with open(_CACHE, encoding="utf-8") as f:
            entry = (json.load(f).get("projects") or {}).get(ref) or {}
        color, name = entry.get("color"), entry.get("name")
    except (OSError, ValueError):
        pass

    name = name or (ref.split("/")[-1] if ref else os.path.basename(cwd) or "—")

    rgb = _rgb(color)
    if rgb:
        c = f"\033[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
        project = f"{c}● {name}{_RESET}"   # ● filled dot + name, in group color
    else:
        project = f"○ {name}"               # ○ hollow dot when no group color

    parts = [project]
    if model:
        parts.append(f"{_DIM}{model}{_RESET}")
    parts.append(f"{_DIM}{_short_cwd(cwd)}{_RESET}")
    sys.stdout.write(" · ".join(parts))


if __name__ == "__main__":
    main()
