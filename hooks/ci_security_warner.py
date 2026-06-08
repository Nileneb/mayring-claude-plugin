#!/usr/bin/env python3
"""UserPromptSubmit hook: config-driven repo-watch — surfaces new CI fails,
security alerts, Dependabot alerts, open PRs and assigned issues across a
configurable set of repos, as a warning-block at the start of the prompt.

User-Auftrag (2026-05-11): "wenn 1. irgendwas in der ci/cd pipeline scheitert
2. im security/quality bereich etwas aufploppt → IMMER bei dir eine meldung
erscheint, ich NICHT erst prompten muss". 2026-05-12 generalisiert (#243):
nicht mehr 2 hardcoded repos × 3 fixe checks, sondern config-driven repos ×
beliebige check-typen (ci · code_scanning · dependabot · pulls · issues).

100% "ohne user-prompt" geht nicht (claude-code session ist user-getrieben).
Aber: dieser hook läuft VOR jedem prompt → bei rotem CI / neuen alerts / neuen
PRs injiziert er einen warning-block am promptanfang.

Config: ~/.config/mayring/watch_repos.json (optional) —
    {"repos": {"owner/name": ["ci","code_scanning","dependabot","pulls","issues"], ...}}
Fehlt die Datei → eingebauter Default (MayringCoder + app.linn.games, ci+sec+dep).

State-cache: ~/.config/mayring/ci_security_state.json — "neu seit letztem
check"-detection pro (repo, check-typ), sonst spammt der hook bei jedem prompt.

Output: NUR wenn etwas Neues — sonst silent skip.

TODO(#243, v2): neue events zusätzlich ins Memory persistieren
(`source_id=watch:<repo>:<type>:<id>`), damit man sie von claude.ai aus
abfragen kann. v1 macht nur den inline-warning-block.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_STATE_FILE = Path(os.path.expanduser("~/.config/mayring/ci_security_state.json"))
_CONFIG_FILE = Path(os.path.expanduser("~/.config/mayring/watch_repos.json"))

# Built-in default when ~/.config/mayring/watch_repos.json is absent.
# WHY(2026-06-04, pipeline-consolidation): KEINE hardcoded Repos mehr. Die Watch-Liste
# ist jetzt ausschließlich dashboard-/server-gesteuert (GET /stats/watch-repos) bzw. per
# lokaler ~/.config/mayring/watch_repos.json. Hardcoded Defaults pollten Repos, die der
# User nie aktiviert hatte → Fremd-Repo-Rauschen im Prompt ("Logs in fremde Agents
# gespült", User-Eskalation). Nur noch beobachten, was explizit aktiviert wurde.
_DEFAULT_WATCH: dict[str, list[str]] = {}

# WHY(2026-05-11): "Automatic Dependency Submission" ist GitHubs auto-
# dependency-graph-job (kein file im repo) — läuft auf jeden push inkl.
# ephemeral feature-branches, aber wir squash-merge + delete-branch sofort →
# "couldn't find remote ref" → fail. Harmlos, aber rauscht. Rausfiltern.
_IGNORE_WORKFLOW_SUBSTRINGS = (
    "automatic dependency submission",
    "dependency submission",
)


def _gh(args: list[str], timeout: float = 8.0) -> dict | list | None:
    """Run gh CLI silently, return parsed JSON or None on failure."""
    try:
        out = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout) if out.stdout.strip() else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def _fetch_server_watch() -> dict[str, list[str]]:
    """Active watched repos managed via the dashboard (GET /stats/watch-repos).

    Server is the source of truth for user-added repos; best-effort (network/JWT
    failure → {} so the built-in default still applies). Returns slug → alert-types.
    """
    import urllib.request
    api = os.getenv("MAYRING_API_URL", "https://mcp.linn.games").rstrip("/")
    jwt_path = os.getenv("MAYRING_JWT_FILE", os.path.expanduser("~/.config/mayring/hook.jwt"))
    try:
        token = Path(jwt_path).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if not token:
        return {}
    try:
        req = urllib.request.Request(
            f"{api}/stats/watch-repos",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:  # nosec B310
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}
    known = set(_CHECKS)
    out: dict[str, list[str]] = {}
    for r in (data.get("repos") or []):
        if r.get("active") and r.get("repo_slug"):
            keep = [t for t in (r.get("alerts") or []) if t in known] or ["ci"]
            out[str(r["repo_slug"])] = keep
    return out


def _load_watch_config() -> dict[str, list[str]]:
    """repo-slug → check-types. Fully dashboard/server-driven: the server-managed list
    (GET /stats/watch-repos) UNION an optional local ~/.config/mayring/watch_repos.json.
    No hardcoded defaults (see _DEFAULT_WATCH) → only explicitly-activated repos are polled."""
    # Base: local config file if present + valid, else empty (server-driven only).
    base: dict[str, list[str]] = dict(_DEFAULT_WATCH)
    try:
        with open(_CONFIG_FILE) as f:
            cfg = json.load(f)
        repos = cfg.get("repos")
        if isinstance(repos, dict) and repos:
            known = set(_CHECKS)
            out: dict[str, list[str]] = {}
            for slug, types in repos.items():
                if isinstance(types, list):
                    keep = [t for t in types if t in known]
                    if keep:
                        out[str(slug)] = keep
            if out:
                base = out
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    # UNION the server-managed (dashboard) repos — server wins on overlap.
    merged = dict(base)
    merged.update(_fetch_server_watch())
    return merged


def _load_state() -> dict:
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except OSError:
        pass


def _seen(state: dict, key: str, repo: str) -> list:
    return state.setdefault(key, {}).setdefault(repo, [])


# --------------------------------------------------------------------------
# Individual checks — each returns warning lines (and mutates `state`)
# --------------------------------------------------------------------------

def _check_ci(repo: str, state: dict, events: list[dict]) -> list[str]:
    """New CI failures since last check (limit 30 — active repos burn through
    a smaller window before the hook sees them).

    CI is NOT pushed to the server here: the GitHub-Action → /repo-events pipeline
    already records repo_ci in hook_events. This check stays in-prompt only (live
    view); `events` is accepted for a uniform check signature but left untouched."""
    runs = _gh(["run", "list", "--repo", repo, "--limit", "30",
                "--json", "databaseId,name,conclusion,status,event"])
    if not runs:
        return []
    warnings: list[str] = []
    seen = _seen(state, "failed_runs", repo)
    new_ids = []
    for r in runs:
        if r.get("conclusion") == "failure" and r.get("status") == "completed":
            rid = str(r.get("databaseId"))
            if rid in seen:
                continue
            new_ids.append(rid)
            if any(sub in str(r.get("name", "")).lower() for sub in _IGNORE_WORKFLOW_SUBSTRINGS):
                continue  # record-as-seen but don't surface
            warnings.append(f"- **{repo} CI**: `{r.get('name')}` FAILED "
                            f"(run {rid}, trigger={r.get('event')})")
    if new_ids:
        state["failed_runs"][repo] = (seen + new_ids)[-50:]
    return warnings


def _check_security(repo: str, state: dict, events: list[dict]) -> list[str]:
    """New open code-scanning (CodeQL) alerts since last check.

    Like CI, not pushed here — code_scanning_alert reaches the server via the
    GitHub-Action webhook → /repo-events (repo_security). In-prompt live view only."""
    alerts = _gh(["api", f"repos/{repo}/code-scanning/alerts?state=open",
                  "--jq", "[.[] | {n:.number, rule:.rule.id, severity:.rule.severity}]"])
    if alerts is None:
        return []
    cur = sorted(a.get("n") for a in alerts if a.get("n"))
    prev = state.setdefault("alert_counts", {}).get(repo, [])
    new = [n for n in cur if n not in prev]
    state["alert_counts"][repo] = cur
    if not new:
        return []
    sev = {n: next((a["severity"] for a in alerts if a.get("n") == n), "?") for n in new}
    return [f"- **{repo} Security**: {len(new)} NEUE code-scanning alert(s): "
            + ", ".join(f"#{n}({s})" for n, s in sev.items())]


def _check_dependabot(repo: str, state: dict, events: list[dict]) -> list[str]:
    """New open Dependabot alerts (vulnerable deps) since last check."""
    alerts = _gh(["api", f"repos/{repo}/dependabot/alerts?state=open&per_page=100",
                  "--jq", "[.[] | {n:.number, pkg:.dependency.package.name, "
                          "sev:.security_advisory.severity, url:.html_url}]"])
    if alerts is None:
        return []
    cur = sorted(a.get("n") for a in alerts if a.get("n") is not None)
    prev = state.setdefault("dependabot_counts", {}).get(repo, [])
    new = [n for n in cur if n not in prev]
    state["dependabot_counts"][repo] = cur
    if not new:
        return []
    by_n = {a["n"]: a for a in alerts}
    parts = [f"#{n} {by_n.get(n, {}).get('pkg', '?')}({by_n.get(n, {}).get('sev', '?')})" for n in new]
    for n in new:
        a = by_n.get(n, {})
        events.append({
            "hook_type": "repo_dependabot", "repo": repo, "number": n,
            "severity": a.get("sev"),
            "summary": f"{a.get('pkg', '?')} ({a.get('sev', '?')})",
            "url": a.get("url") or f"https://github.com/{repo}/security/dependabot",
        })
    return [f"- **{repo} Dependabot**: {len(new)} NEUE alert(s): {', '.join(parts)} "
            f"→ https://github.com/{repo}/security/dependabot"]


def _check_pulls(repo: str, state: dict, events: list[dict]) -> list[str]:
    """New open PRs since last check (so a freshly-opened PR — e.g. a
    Dependabot bump or a Copilot fix — surfaces without you having to look)."""
    prs = _gh(["pr", "list", "--repo", repo, "--state", "open", "--limit", "40",
               "--json", "number,title,isDraft,author,url"])
    if prs is None:
        return []
    seen = _seen(state, "open_prs", repo)
    new = [p for p in prs if str(p.get("number")) not in seen]
    # state = exactly the currently-open set (so closed/merged PRs drop out)
    state["open_prs"][repo] = [str(p.get("number")) for p in prs]
    if not new:
        return []
    parts = []
    for p in new:
        draft = " (draft)" if p.get("isDraft") else ""
        author = (p.get("author") or {}).get("login", "?")
        parts.append(f"#{p.get('number')} \"{str(p.get('title', ''))[:60]}\" [{author}]{draft}")
        events.append({
            "hook_type": "repo_pull", "repo": repo, "number": p.get("number"),
            "summary": f"{str(p.get('title', ''))[:120]} [{author}]{draft}",
            "url": p.get("url") or "",
        })
    return [f"- **{repo} PRs**: {len(new)} neue open PR(s): " + " · ".join(parts)]


def _check_issues(repo: str, state: dict, events: list[dict]) -> list[str]:
    """New issues assigned to you since last check (off by default — add
    "issues" to a repo's watch-list to enable)."""
    issues = _gh(["issue", "list", "--repo", repo, "--state", "open",
                  "--assignee", "@me", "--limit", "40", "--json", "number,title,url"])
    if issues is None:
        return []
    seen = _seen(state, "assigned_issues", repo)
    new = [i for i in issues if str(i.get("number")) not in seen]
    state["assigned_issues"][repo] = [str(i.get("number")) for i in issues]
    if not new:
        return []
    parts = [f"#{i.get('number')} \"{str(i.get('title', ''))[:60]}\"" for i in new]
    for i in new:
        events.append({
            "hook_type": "repo_issue", "repo": repo, "number": i.get("number"),
            "summary": str(i.get("title", ""))[:120], "url": i.get("url") or "",
        })
    return [f"- **{repo} Issues** (assigned): {len(new)} neu: " + " · ".join(parts)]


_CHECKS = {
    "ci": _check_ci,
    "code_scanning": _check_security,
    "dependabot": _check_dependabot,
    "pulls": _check_pulls,
    "issues": _check_issues,
}

# Escalate to a loud, blocking directive once a failure has stood this many
# consecutive prompts unresolved.
_ESCALATE_AFTER = 3


def _standing_ci(repo: str, state: dict) -> tuple[list[str], int]:
    """Currently-red CI: the LATEST completed run per workflow that ended in failure.

    WHY(false-positive-blindness 2026-06-08): the old _check_ci surfaced each failed
    run id exactly ONCE (state-cache de-dup) then went silent — so a workflow that
    stays red prompt after prompt vanished from view after its first appearance, and
    a persistent outage looked resolved. A standing failure must STAY surfaced until
    it goes green. We track a per-workflow red-streak so a lingering failure gets
    louder, not quieter. Returns (lines, max_streak)."""
    runs = _gh(["run", "list", "--repo", repo, "--limit", "40",
                "--json", "databaseId,name,workflowName,workflowDatabaseId,"
                          "conclusion,status,event,headBranch"])
    streaks = state.setdefault("red_streak", {}).setdefault(repo, {})
    if runs is None:
        return [], 0
    # WHY(stale-red-after-rename 2026-06-08): key the "latest run per workflow" by the
    # STABLE workflowDatabaseId, NOT the per-run display name. When a workflow gains a
    # `name:` field its runs' display name flips (path → name); keyed by name the old
    # path-named failures get their OWN bucket that no later success ever supersedes →
    # a workflow that is green for hours still shows ROT forever (OlD-mcp build-image).
    # gh normalises workflowName to the current name even on old runs, so it is a safe
    # display label; the id is the identity.
    latest_per_wf: dict[str, dict] = {}
    for r in runs:  # gh returns newest-first → first seen per workflow id is the latest
        if r.get("status") != "completed":
            continue
        key = str(r.get("workflowDatabaseId") or r.get("name", ""))
        if key not in latest_per_wf:
            latest_per_wf[key] = r
    lines: list[str] = []
    max_streak = 0
    still_red: dict[str, int] = {}
    for key, r in latest_per_wf.items():
        nm = str(r.get("workflowName") or r.get("name", ""))
        if any(sub in nm.lower() for sub in _IGNORE_WORKFLOW_SUBSTRINGS):
            continue
        if r.get("conclusion") == "failure":
            streak = int(streaks.get(key, 0)) + 1
            still_red[key] = streak
            max_streak = max(max_streak, streak)
            since = f" — seit {streak} prompt(s) ROT" if streak > 1 else ""
            lines.append(f"- 🔴 **{repo} CI**: `{nm}` ist aktuell ROT "
                         f"(run {r.get('databaseId')}, {r.get('event')}){since}")
    # red_streak = exactly the currently-red set (greens drop out → streak resets)
    state["red_streak"][repo] = still_red
    return lines, max_streak


def _standing_smoke_issues(repo: str, state: dict) -> tuple[list[str], int]:
    """Open `smoke-failure` issues filed by the post-deploy smoke. These persist
    until closed (the smoke auto-closes them when prod is green), so surfacing the
    open count every prompt is the right standing signal — not a one-shot 'new'."""
    issues = _gh(["issue", "list", "--repo", repo, "--label", "smoke-failure",
                  "--state", "open", "--limit", "30", "--json", "number,title"])
    if not issues:
        state.setdefault("smoke_open_streak", {})[repo] = 0
        return [], 0
    streak = int(state.setdefault("smoke_open_streak", {}).get(repo, 0)) + 1
    state["smoke_open_streak"][repo] = streak
    newest = max(issues, key=lambda i: int(i.get("number") or 0))
    since = f" — seit {streak} prompt(s) offen" if streak > 1 else ""
    return ([f"- 🔴 **{repo} Smoke**: {len(issues)} offene `smoke-failure`-Issue(s){since}; "
             f"neuste #{newest.get('number')} \"{str(newest.get('title',''))[:70]}\""],
            streak)


def _post_notifications(events: list[dict]) -> None:
    """Hook-A: persist net-new findings (dependabot/pull/issue) to the server so they
    surface in the Ampel dashboard (POST /stats/notifications/ingest). Best-effort:
    no JWT, server down, or pre-deploy 404 → silent skip (the in-prompt warning is the
    live view regardless). ci/security are NOT here — the GitHub-Action pipeline records
    those already, so re-POSTing would duplicate them."""
    if not events:
        return
    import urllib.request
    api = os.getenv("MAYRING_API_URL", "https://mcp.linn.games").rstrip("/")
    jwt_path = os.getenv("MAYRING_JWT_FILE", os.path.expanduser("~/.config/mayring/hook.jwt"))
    try:
        token = Path(jwt_path).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return
    if not token:
        return
    body = json.dumps({"events": events}).encode()
    req = urllib.request.Request(
        f"{api}/stats/notifications/ingest", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=4)  # nosec B310
    except Exception:
        # Best-effort: the local state-cache already advanced, so a missed POST is
        # acceptable (next genuinely-new finding re-POSTs). Loud failure would spam
        # every prompt during a deploy window.
        pass


def main() -> int:
    watch = _load_watch_config()
    state = _load_state()
    warnings: list[str] = []
    events: list[dict] = []  # Hook-A: net-new findings to persist server-side

    standing: list[str] = []  # currently-red state — surfaced EVERY prompt until green
    max_streak = 0

    for repo, types in watch.items():
        for t in types:
            fn = _CHECKS.get(t)
            if fn is None:
                continue
            try:
                warnings.extend(fn(repo, state, events))
            except Exception:
                # one flaky check must not kill the whole hook — but DON'T
                # swallow silently: surface it so a broken check is visible.
                warnings.append(f"- **{repo} {t}**: watch-check raised (see hook) — investigate")
        # Standing red-state checks run for every watched repo that watches CI —
        # independent of the net-new state-cache so a persistent failure never
        # silently drops out of view.
        if "ci" in types:
            try:
                ci_lines, ci_streak = _standing_ci(repo, state)
                standing.extend(ci_lines)
                max_streak = max(max_streak, ci_streak)
            except Exception:
                standing.append(f"- **{repo} CI**: standing-check raised — investigate")
            try:
                sm_lines, sm_streak = _standing_smoke_issues(repo, state)
                standing.extend(sm_lines)
                max_streak = max(max_streak, sm_streak)
            except Exception:
                standing.append(f"- **{repo} Smoke**: standing-check raised — investigate")

    _save_state(state)
    _post_notifications(events)

    if standing:
        if max_streak >= _ESCALATE_AFTER:
            print(f"## 🚨🚨 PROD ROT seit {max_streak} PROMPTS — JETZT FIXEN, "
                  "nicht weiterarbeiten\n")
        else:
            print("## 🔴 Prod-Status ROT (steht bis grün)\n")
        for s in standing:
            print(s)
        print("")

    if warnings:
        print("## ⚠️ Repo-Watch (neu seit letztem prompt)\n")
        for w in warnings:
            print(w)
        print("")

    return 0


if __name__ == "__main__":
    sys.exit(main())
