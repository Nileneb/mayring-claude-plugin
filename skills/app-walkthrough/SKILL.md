---
name: mayring-coder:app-walkthrough
description: Autonomously walk through the whole logged-in web app (app.linn.games), screenshot every page, and surface bugs. Use when the user wants a full UI sweep, an end-to-end check after changes, "durchspielen / durchklicken", or to hunt for broken pages, console/network errors, or visual glitches across the app. Read-only and safe — never submits, deletes, pays, or logs out.
---

# /app-walkthrough — autonomous read-only UI sweep

Drives a headless Chromium (Playwright) through the app, capturing per page:
screenshot · JS-console errors · network responses ≥400 · HTTP status ·
Laravel/Livewire error pages. Writes a JSON report + PNGs that **you then read
and analyse** — the script flags technical errors; you catch visual/semantic
bugs (duplicate cards, empty states, broken layout) by reading the screenshots.

**Safety invariant:** read-only. Only GET-navigation + at most accepting the
cookie banner. A deny-list blocks anything matching logout/delete/pay/submit/
save/export. No state change, no money, no logout. Do NOT relax this.

## Paths

```bash
WV="$HOME/.cache/app-walkthrough/venv"
SCRIPT="$(ls "$HOME"/.claude/plugins/cache/*/mayring-coder/*/bin/app_walkthrough.py 2>/dev/null | sort | tail -1)"
```

## Steps

1. **Bootstrap once** (skip if `$WV/bin/playwright` exists):
   ```bash
   python3 -m venv "$WV" && "$WV/bin/pip" install -q playwright \
     && "$WV/bin/playwright" install chromium chromium-headless-shell
   ```

2. **Ensure a session** (skip if `~/.cache/app-walkthrough/profile` exists and a
   later walkthrough doesn't print `NOT_LOGGED_IN`). The login is headful — it
   opens a browser window; tell the user to sign in via GitHub, the script
   detects success and persists the session for all future headless runs:
   ```bash
   DISPLAY="${DISPLAY:-:1}" "$WV/bin/python" "$SCRIPT" --login
   ```
   Run this in the background and wait for `LOGIN_OK`.

3. **Walk** (headless, no interaction):
   ```bash
   "$WV/bin/python" "$SCRIPT" --out /tmp/app-walkthrough
   ```
   Run in the background; it prints `WALK_DONE: N pages, M with findings`.

4. **Analyse** `/tmp/app-walkthrough/report.json`:
   - Read it; list pages where `error`, `console_errors`, `network_errors`,
     `dom_flags`, or `status>=400` are non-empty.
   - **Read the screenshots** of every flagged page AND a sample of clean ones —
     the detector cannot see visual bugs (duplicated/empty widgets, misaligned
     layout, wrong text). This visual pass is the point of the tool.
   - Report a prioritised bug list with evidence (route + screenshot path +
     what's wrong). Separate real bugs from expected behaviour (a 403 on an
     admin-only route is not necessarily a bug).

## Options

- `--base URL` — target a different host (default `https://app.linn.games`).
- `--routes /a,/b` — override the seed route list (link-discovery still adds more).
- `--out DIR` — output directory (default `/tmp/app-walkthrough`).

If a walkthrough prints `NOT_LOGGED_IN`, the persisted session expired — re-run
step 2.
