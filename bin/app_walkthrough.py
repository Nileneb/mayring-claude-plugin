#!/usr/bin/env python3
"""Autonomous read-only walkthrough of a logged-in web app (app.linn.games).

Visits a seed route list + links discovered from the dashboard, and for each page
captures a screenshot plus JS-console errors, network responses >=400, the final
HTTP status, and Laravel/Livewire error markers in the DOM. Writes a JSON report
+ PNGs that Claude then reads and analyses.

SAFETY: read-only. It only navigates (GET) and, at most, clicks elements that are
clearly non-destructive (tabs). It NEVER clicks/submits anything matching the
destructive deny-list (logout, delete, pay, submit, save, …) and never fills+submits
forms. No state change, no money, no logout.

Auth: a persistent browser profile. Run `--login` once (headful) to sign in via
GitHub; the session persists in the profile dir, so later headless runs reuse it.

Usage:
    app_walkthrough.py --login        # one-time headful login, persists session
    app_walkthrough.py                # headless walkthrough → /tmp/app-walkthrough/
    app_walkthrough.py --base URL --routes /a,/b --out DIR
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_DEFAULT = "https://app.linn.games"
STATE_FILE = Path.home() / ".cache" / "app-walkthrough" / "state.json"
OUT_DEFAULT = Path("/tmp/app-walkthrough")

# Seed routes (the main authed pages). Link-discovery on /dashboard adds the rest.
SEED_ROUTES = [
    "/dashboard",
    "/mayring/memory",
    "/mayring/pi-agent",
    "/mayring/igio",
    "/mayring/plugin",
    "/credits",
    "/credits/usage",
    "/paper-search",
    "/admin",
    "/settings/profile",
]

# Never navigate to / click anything matching these (destructive or session-ending).
DENY = re.compile(
    r"(logout|log-out|abmelden|sign-?out|delete|löschen|loeschen|entfernen|remove|"
    r"aufladen|checkout|bezahl|payment|/pay|invoice|export|dsgvo|impersonate|"
    r"\bsubmit\b|absenden|speichern|/save|destroy|cancel-subscription)",
    re.I,
)

LOGIN_MARKERS = ("/login", "/auth/", "/register", "/pending-approval", "github.com")


def _is_logged_in(page) -> bool:
    url = page.url or ""
    return not any(m in url for m in LOGIN_MARKERS)


def _dismiss_cookiebanner(page) -> None:
    """Accept the cookie consent once so it stops overlaying screenshots.
    Accepting cookies is non-destructive; everything else stays read-only."""
    for sel in ("button:has-text('Akzeptieren')", "button:has-text('Accept')",
                "button:has-text('Zustimmen')"):
        try:
            btn = page.locator(sel)
            if btn.count():
                btn.first.click(timeout=2000)
                page.wait_for_timeout(400)
                return
        except Exception:
            pass


def cmd_login(base: str) -> int:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(base + "/dashboard", wait_until="domcontentloaded")
        print("→ Log in via the opened window (GitHub OAuth). Waiting up to 180s …",
              file=sys.stderr)
        try:
            # success = we land on an authed page (no login/oauth marker) and it settles
            page.wait_for_url(
                lambda u: not any(m in u for m in LOGIN_MARKERS),
                timeout=180_000,
            )
            page.wait_for_timeout(2000)
            ok = _is_logged_in(page)
        except Exception:
            ok = _is_logged_in(page)
        if ok:
            # storage_state persists ALL cookies (incl. the session-only Laravel
            # cookie). A persistent_context drops session-only cookies on close,
            # so headless re-runs landed on the login page ("NOT_LOGGED_IN").
            ctx.storage_state(path=str(STATE_FILE))
        browser.close()
    print("LOGIN_OK" if ok else "LOGIN_FAILED")
    return 0 if ok else 1


def _discover_links(page, base: str) -> list[str]:
    hrefs = page.eval_on_selector_all(
        "a[href]", "els => els.map(e => e.getAttribute('href'))"
    )
    paths: list[str] = []
    for h in hrefs or []:
        if not h:
            continue
        if h.startswith(base):
            h = h[len(base):]
        if not h.startswith("/") or h.startswith("//"):
            continue
        if DENY.search(h):
            continue
        # skip param routes we can't safely fill (e.g. /recherche/{id}) unless concrete
        paths.append(h.split("#")[0])
    return paths


def _capture(page, route: str, base: str, out: Path) -> dict:
    rec: dict = {"route": route, "console_errors": [], "network_errors": [],
                 "status": None, "dom_flags": [], "error": None, "screenshot": None}
    console: list[str] = []
    neterr: list[dict] = []
    page.on("console", lambda m: console.append(f"{m.type}: {m.text}")
            if m.type in ("error",) else None)
    page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))

    def _on_resp(resp):
        try:
            if resp.status >= 400:
                neterr.append({"url": resp.url, "status": resp.status})
        except Exception:
            pass
    page.on("response", _on_resp)
    try:
        resp = page.goto(base + route, wait_until="networkidle", timeout=25_000)
        status = resp.status if resp else None
        rec["status"] = status
        page.wait_for_timeout(1200)  # let Livewire hydrate
        # Error detection — STATUS + precise title/h1/selector signals only.
        # (v1 substring-matched "419"/"exception" in the body → false positives on
        # healthy pages. Match real error pages by their heading/title instead.)
        if status and status >= 400:
            rec["dom_flags"].append(f"http_{status}")
        title = (page.title() or "").strip()
        h1 = ""
        try:
            if page.locator("h1").count():
                h1 = (page.locator("h1").first.inner_text(timeout=1000) or "").strip()
        except Exception:
            pass
        ERR = re.compile(
            r"(whoops|server error|page expired|forbidden|not found|too many requests|"
            r"service unavailable|\b419\b|\b500\b|\b503\b)", re.I)
        if ERR.search(title) or ERR.search(h1):
            rec["dom_flags"].append(f"error_page:{(title or h1)[:48]}")
        try:
            if page.locator(".exception-summary, .ignition, [data-ignition]").count():
                rec["dom_flags"].append("laravel_exception_page")
        except Exception:
            pass
        if not _is_logged_in(page):
            rec["dom_flags"].append("redirected_to_login")
        shot = out / ("page_" + re.sub(r"[^a-z0-9]+", "_", route.strip("/").lower() or "root") + ".png")
        page.screenshot(path=str(shot), full_page=True)
        rec["screenshot"] = str(shot)
    except Exception as e:  # one bad page must not abort the run
        rec["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    rec["console_errors"] = console[:20]
    rec["network_errors"] = neterr[:20]
    return rec


def cmd_walk(base: str, seed: list[str], out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        print("NOT_LOGGED_IN: run with --login first", file=sys.stderr)
        return 2
    results: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(storage_state=str(STATE_FILE),
                                  viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()
        # auth check + link discovery from dashboard
        page.goto(base + "/dashboard", wait_until="networkidle", timeout=25_000)
        if not _is_logged_in(page):
            browser.close()
            print("NOT_LOGGED_IN: session expired — run --login again", file=sys.stderr)
            return 2
        _dismiss_cookiebanner(page)
        discovered = _discover_links(page, base)
        routes, seen = [], set()
        for r in seed + discovered:
            if r not in seen:
                seen.add(r); routes.append(r)
        print(f"→ {len(routes)} routes to walk", file=sys.stderr)
        for r in routes:
            print(f"  visiting {r}", file=sys.stderr)
            results.append(_capture(page, r, base, out))
        browser.close()
    report = out / "report.json"
    report.write_text(json.dumps({"base": base, "count": len(results),
                                  "pages": results}, indent=2))
    # terse stderr summary
    bad = [r for r in results if r["error"] or r["console_errors"]
           or r["network_errors"] or r["dom_flags"] or (r["status"] or 200) >= 400]
    print(f"WALK_DONE: {len(results)} pages, {len(bad)} with findings → {report}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--base", default=BASE_DEFAULT)
    ap.add_argument("--routes", default="")
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    a = ap.parse_args()
    if a.login:
        return cmd_login(a.base)
    seed = [r.strip() for r in a.routes.split(",") if r.strip()] or SEED_ROUTES
    return cmd_walk(a.base, seed, Path(a.out))


if __name__ == "__main__":
    raise SystemExit(main())
