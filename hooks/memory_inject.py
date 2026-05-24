#!/usr/bin/env python3
"""UserPromptSubmit hook: inject relevant memory chunks into the prompt.

Three /search calls per prompt give a multi-lens view:
  - generic semantic search (current task)        [primary]
  - ambient_snapshot lens (project-level context)
  - conversation_summary lens (what was decided/done before)

The queries run SEQUENTIALLY, primary first, each with a strict timeout. They
used to run concurrently, but the single-worker API serialises concurrent
searches into 9s timeouts — sequential lets the primary lens reliably return
(see _run_lenses). Cheap-fail when JWT missing or API down.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time as _time
import urllib.request
import urllib.error

JWT_FILE = os.path.expanduser("~/.config/mayring/hook.jwt")
API = os.getenv("MAYRING_API_URL", "https://mcp.linn.games").rstrip("/")

# Device↔Cloud-Kanal (#5): X-Device-Id auf Cloud-Calls + best-effort Hook-Report.
# no-op-Fallback wenn das Sister-Modul fehlt (alter Snapshot).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _device import device_headers, report_hook_event
except ImportError:
    def device_headers() -> dict:  # type: ignore[misc]
        return {}

    def report_hook_event(*_a, **_k) -> None:  # type: ignore[misc]
        pass

# Phase 2: Codebook-Kategorien aus der DB (Phase 1, via session_ctx.json) statt
# aus einer hardcoded lokalen universal.yaml. Fail-soft → YAML/Minimal-Fallback.
try:
    from _session_ctx import (
        load_active_categories as _ctx_load_categories,
        route_project as _route_project,
        _git_remote,
        derive_task as _derive_task,
        read_session_ctx as _read_session_ctx,
        write_session_ctx_field as _write_session_ctx_field,
    )
except ImportError:
    def _ctx_load_categories(token: str = "") -> list:  # type: ignore[misc]
        return []

    def _route_project(*_a, **_k) -> dict:  # type: ignore[misc]
        return {"project_id": None, "name": None, "mode": "unknown",
                "reason": "no-module"}

    def _git_remote(*_a, **_k):  # type: ignore[misc]
        return None

    def _derive_task(*_a, **_k) -> str:  # type: ignore[misc]
        return ""

    def _read_session_ctx(*_a, **_k):  # type: ignore[misc]
        return None

    def _write_session_ctx_field(*_a, **_k) -> None:  # type: ignore[misc]
        return None

# WHY(v2-pinned-sources): User-Auftrag — "WIE BEKOMMEN WIR DEIN
# NUTZLOSES SELBST ERSTELLTES FEEDBACK IN JEDEN SCHEISS PROMPT ALS
# KONTEXT?". Hier: ein file-Pfad-Liste die UNABHÄNGIG vom search-result
# bei jedem prompt als pinned-block injektet wird. Inhalt sind die
# master-audit-files (frust-patterns, regeln, V2-spec) — der LLM-Advisor
# hat damit immer die User-Constraints im Kopf.
PINNED_FILES_CONFIG = os.path.expanduser("~/.config/mayring/pinned_files.json")
PINNED_DEFAULT_FILES = [
    "/home/nileneb/Desktop/MayringCoder/docs/v2-master-audit.md",
    "/home/nileneb/Desktop/MayringCoder/docs/v2-frustration-patterns.md",
    "/home/nileneb/Desktop/MayringCoder/docs/v2-workspaces-spec.md",
]
PINNED_CHAR_BUDGET = 1500  # gesamt — gekürzt sonst frisst es den prompt

# Persistent state for the Stop hook. The injected chunk_id list does NOT
# survive in the user-turn content of the JSONL transcript — Claude Code
# treats hook output as prompt prefix, not user-typed text. So the Stop
# hook has no way to read what was injected from the transcript alone;
# we drop a small state file here that the Stop hook picks up.
INJECT_STATE_DIR = os.path.expanduser("~/.config/mayring/inject-state")


# WHY(v2-stufe2.2): silent-skip-counter trackt deploy-window-Skips, damit
# chronische Hook-Failures nicht permanent unsichtbar bleiben.
def _record_silent_skip(reason: str) -> None:
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from _silent_skip_counter import record_silent_skip  # noqa: PLC0415
        record_silent_skip(reason=reason)
    except (ImportError, OSError) as e:
        # Counter ist best-effort — aber wir schweigen nicht ganz: log to stderr.
        print(f"[memory_inject] could not record silent skip: {e}", file=sys.stderr)

# Per-request timeout budget. Was 4.0s but the hybrid search auto-activates
# the PI-advisor LLM stage when the scope-filter returns >10 candidates,
# which is normal for any populated workspace — that stage adds 2-4s on top
# of vector + symbolic. Sub-4s timeouts caused every prompt to fall through
# to "Suche fehlgeschlagen" even though the API itself was healthy.
TIMEOUT = 9.0           # per-request
GLOBAL_TIMEOUT = 12.0   # whole hook (3 lenses run sequentially, primary first)
TOP_K_PRIMARY = 4
TOP_K_LENS = 2          # per ambient/conv lens
CHAR_BUDGET = 1800      # per call → ~5400 total max
MIN_PROMPT_LEN = 12     # skip 1-word commands like "ls"


def _load_token() -> str:
    try:
        with open(JWT_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def _read_payload() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return {}


def _extract_prompt(payload: dict) -> str:
    prompt = (
        payload.get("user_message")
        or payload.get("message")
        or payload.get("prompt")
        or ""
    )
    return str(prompt).strip()


def _search(
    query: str, token: str, *, top_k: int = TOP_K_PRIMARY,
    source_type: str | None = None, char_budget: int = CHAR_BUDGET,
    category_hint: list[str] | None = None,
    project_id: str | None = None, task_context: str = "",
) -> dict:
    """Run one /memory/search lens.

    Returns either the parsed JSON response, or a synthetic dict with a
    `_hook_error` key that surfaces the failure mode in the prompt block.
    Silent ``return None`` on every exception is exactly how this hook
    masked a 4s timeout for weeks — never again.
    """
    body_dict: dict = {
        "query": query[:600],
        "top_k": top_k,
        "include_text": True,
        "char_budget": char_budget,
        # WHY(v2-llm-advisor-on): User-Auftrag — "GIBT ES EINEN GUTEN GRUND,
        # EINEN LLM ADVISOR FÜR DUMME AI ZU HABEN??? ICH SAGE JA". Vorher
        # default-disabled wegen 9s-budget; jetzt enabled mit kleinem
        # qwen3.5:2b-Modell + top_k=5 (≤5s Budget). Der Advisor kennt
        # die User-Regeln (KISS, no-legacy, no-silent) aus den
        # always-injected pinned sources und nutzt sie als task_context.
        "llm_prefilter": True,
        "llm_prefilter_model": os.environ.get(
            "MAYRING_LLM_ADVISOR_MODEL", "qwen3.5:2b",
        ),
    }
    if source_type:
        body_dict["source_type"] = source_type
    if category_hint:
        body_dict["category_hint"] = category_hint
    if project_id:
        body_dict["project"] = project_id
    if task_context:
        body_dict["task_context"] = task_context
    body = json.dumps(body_dict).encode()
    req = urllib.request.Request(
        f"{API}/memory/search",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            **device_headers(),
        },
    )
    # Retry on 502/503/504 — common during MayringCoder deploy windows
    # (~30s nginx returns 502 while uvicorn restarts). Without retry,
    # every UserPromptSubmit during a deploy fails the hook → no Memory
    # Context injection → user sees "kein Kontext mehr injiziert".
    # 3 attempts with backoff; total ≤ TIMEOUT - 1s budget so we don't
    # exceed the per-request timeout.
    last_err: str = ""
    last_status: int = 0
    # 4 attempts, exponentielles Backoff: 1.0 + 2.0 + 4.0 = 7s wait
    # zwischen request 1 und 4 → bridge typische 30s-Deploy-Windows
    # bei mayring-stack-restart (Container-stop + start + healthcheck).
    backoff = 1.0
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} from /memory/search"
            last_status = e.code
            if e.code in (502, 503, 504) and attempt < 3:
                _time.sleep(backoff)
                backoff *= 2
                continue
            return {"_hook_error": last_err, "_status": last_status}
        except TimeoutError:
            return {"_hook_error": f"TIMEOUT after {TIMEOUT}s — server is slow or down"}
        except urllib.error.URLError as e:
            last_err = f"URLError: {e.reason}"
            if attempt < 3:
                _time.sleep(backoff)
                backoff *= 2
                continue
            return {"_hook_error": last_err}
        except OSError as e:
            return {"_hook_error": f"OSError {e.errno}: {e.strerror}"}
        except ValueError as e:
            return {"_hook_error": f"JSON parse error: {e}"}
    return {"_hook_error": last_err or "unknown"}


_CATEGORIZE_OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
_CATEGORIZE_MODEL = os.environ.get("MAYRING_PROMPT_CATEGORIZE_MODEL", "mistral:7b-instruct")
_CATEGORIZE_TIMEOUT = float(os.environ.get("MAYRING_PROMPT_CATEGORIZE_TIMEOUT", "4"))
# WHY(2026-05-10 multi-category-prompt): cache categories pro repo-profile in
# memory zum prompt-categorize-call. List wird einmal pro hook-process geladen.
_CATEGORIZE_CACHED: dict | None = None
_MIN_PROMPT_SIM_THRESHOLD = 0.4   # darunter → "no prior context"


def _load_active_categories(token: str = "") -> list[str]:
    """Codebook-Kategorien als Domäne für prompt-categorize.

    Delegiert an _session_ctx.load_active_categories: DB-Codebook (Phase 1,
    via session_ctx.json, gerätunabhängig) → YAML → Minimal-Set. Ersetzt den
    früheren hardcoded Pfad /home/nileneb/Desktop/MayringCoder/.../universal.yaml,
    der nur auf der dev-Maschine existierte.
    """
    global _CATEGORIZE_CACHED
    if _CATEGORIZE_CACHED is not None:
        return _CATEGORIZE_CACHED
    _CATEGORIZE_CACHED = _ctx_load_categories(token) or [
        "api", "data_access", "domain", "infrastructure", "auth",
        "config", "utils", "testing", "frontend", "deployment",
    ]
    return _CATEGORIZE_CACHED


def _categorize_prompt(prompt: str, token: str = "") -> list[str]:
    """Ask mistral:7b-instruct welche kategorien der user-prompt berührt.

    Returns 1..3 kategorien aus _load_active_categories(). Bei timeout/
    error: leere liste (caller fällt zurück auf unifiltrierte search).
    """
    if not prompt or len(prompt) < MIN_PROMPT_LEN:
        return []
    cats = _load_active_categories(token)
    if not cats:
        return []
    cat_list = ", ".join(cats[:60])  # truncate auf ~60 zum prompt-budget
    body = json.dumps({
        "model": _CATEGORIZE_MODEL,
        "prompt": (
            f"User-prompt:\n{prompt[:600]}\n\n"
            f"Verfügbare Kategorien: {cat_list}\n\n"
            "Wähle 1-3 Kategorien die der prompt INHALTLICH berührt. "
            "Ein prompt kann mehrere themen enthalten — gib ALLE relevanten an. "
            "Antworte NUR mit kommaseparierten kategorie-namen aus der liste, "
            "kein erklärtext. Beispiel: api, auth\n\nAntwort:"
        ),
        "stream": False,
        "options": {"num_predict": 32, "temperature": 0.1},
        "think": False,
    }).encode()
    req = urllib.request.Request(
        f"{_CATEGORIZE_OLLAMA}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_CATEGORIZE_TIMEOUT) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
        # Best-effort — Ollama unerreichbar / langsam → kein cat-filter
        sys.stderr.write(
            f"[memory_inject] prompt-categorize fail ({type(exc).__name__}) "
            f"→ ungefiltert\n"
        )
        return []
    raw = (payload.get("response") or "").strip().lower()
    # ',' or whitespace separated
    valid = {c.lower() for c in cats}
    matched: list[str] = []
    for token in re.split(r"[,;\n]+", raw):
        token = token.strip().strip(".").strip()
        if token in valid and token not in matched:
            matched.append(token)
        if len(matched) >= 3:
            break
    return matched


def _multi_lens_search(query: str, token: str, *,
                       category_hint: list[str] | None = None,
                       project_id: str | None = None,
                       task_context: str = "") -> dict[str, dict]:
    """Run the three lens-searches SEQUENTIALLY (primary first); one entry per lens.

    Each value is either a real search response or a `{_hook_error: ...}`
    sentinel. Lenses that don't fit the GLOBAL_TIMEOUT budget keep their
    pre-seeded sentinel. Sequential (not concurrent) because the single-worker
    API serialises concurrent searches into timeouts — see the loop comment.
    """
    lenses: dict[str, dict] = {
        "primary":      {"category_hint": category_hint, "project_id": project_id,
                          "task_context": task_context},
        "ambient":      {"source_type": "ambient_snapshot", "top_k": TOP_K_LENS,
                          "char_budget": 1000, "project_id": project_id},
        "conversation": {"source_type": "conversation_summary", "top_k": TOP_K_LENS,
                          "char_budget": 1000, "project_id": project_id},
    }
    results: dict[str, dict] = {n: {"_hook_error": "lens did not complete in time"}
                                for n in lenses}
    # WHY(api-saturation 2026-05-24): the cloud API is a single uvicorn worker.
    # Firing all three lenses CONCURRENTLY made them serialise on that one worker
    # to 16-30s and ALL three blew the 9s per-request timeout → the user got zero
    # memory. Run them SEQUENTIALLY in priority order instead: 'primary'
    # (insertion-first) gets the worker alone (~4s < timeout) and reliably
    # returns; the secondary snapshot lenses consume whatever GLOBAL_TIMEOUT
    # budget is left. Proper fix is a multi-worker API (capacity spec) — this is
    # the interim client-side mitigation.
    _deadline = _time.monotonic() + GLOBAL_TIMEOUT
    for name, kwargs in lenses.items():
        if _time.monotonic() >= _deadline:
            break  # out of budget → keep the pre-seeded sentinel for the rest
        try:
            results[name] = _search(query, token, **kwargs)
        except Exception as exc:
            results[name] = {"_hook_error": f"{type(exc).__name__}: {exc}"}
    return results


def _write_inject_state(
    session_id: str,
    chunk_pairs: list[tuple[str, str]],
    chunk_texts: dict[str, str] | None = None,
    user_prompt: str = "",
) -> None:
    """Persist injected-chunk meta + text so the Stop hook can LLM-judge them.

    Path: ~/.config/mayring/inject-state/<session_id>.json. Stop hook
    reads + deletes after rating. Best-effort, never raises.

    WHY(2026-05-10 strukturfix-feedback): vorher persistierten wir nur
    (chunk_id, source_id) und der Stop-Hook ratete via Pfad-Match → bias
    (codebook.yaml etc. fälschlich positive, web.php fälschlich negative).
    Mit chunk-text + user-prompt im state kann der Stop-Hook einen lokalen
    LLM-judge fragen ob der CHUNK-INHALT in der Antwort genutzt wurde —
    inhaltliches signal statt namens-match.
    """
    if not session_id or not chunk_pairs:
        return
    try:
        os.makedirs(INJECT_STATE_DIR, exist_ok=True)
        path = os.path.join(INJECT_STATE_DIR, f"{session_id}.json")
        chunks_payload = []
        for c, s in chunk_pairs:
            entry = {"chunk_id": c, "source_id": s}
            t = (chunk_texts or {}).get(c)
            if t:
                # 600 chars: enough für LLM-judge, klein genug damit
                # 8 chunks × 600 + prompt < 8k context-fenster.
                entry["text"] = t[:600]
            chunks_payload.append(entry)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "chunks": chunks_payload,
                    "user_prompt": user_prompt[:600],
                },
                f,
            )
    except OSError:
        pass


# WHY(observability): silent skip bei deploy-windows. Mayring-API restartet
# bei jedem deploy für ~30s. Ohne 5xx-skip schreibt der Hook bei jedem
# UserPromptSubmit einen lauten Memory-fehler-Block ins Prompt — aber
# alle 3 lenses 502 ist kein User-actionable Fehler. Bei Mix (1× 502 + 2×
# OK oder ein 4xx) bleibt der laute Block, weil das ist eine echte
# Konfig-Anomalie. CHANGE WITH CARE — bei zu lauter silence verlieren wir
# echte Probleme; bei zu sturem laut nervt der deploy-window jeden user.
def _render_pinned_block() -> str:
    """V2-pinned-lens: User-Auftrag-Inhalte (master-audit, frust-patterns,
    spec) IMMER injizieren — search-result-unabhängig.

    Liest entweder aus ~/.config/mayring/pinned_files.json (User-Override)
    oder aus PINNED_DEFAULT_FILES. Fail-soft: fehlende Files werden
    übersprungen, hook bricht NIE deswegen ab.
    """
    files = PINNED_DEFAULT_FILES
    try:
        if os.path.exists(PINNED_FILES_CONFIG):
            cfg = json.loads(open(PINNED_FILES_CONFIG).read())
            files = cfg.get("files") or PINNED_DEFAULT_FILES
    except (OSError, json.JSONDecodeError):
        pass

    snippets: list[str] = []
    remaining = PINNED_CHAR_BUDGET
    for fp in files:
        if remaining <= 100:
            break
        try:
            text = open(fp).read()
        except OSError:
            continue
        # nur die ersten ~remaining/N chars pro file
        per_file = max(300, remaining // max(1, len(files) - len(snippets)))
        snippet = text[:per_file].rstrip()
        snippets.append(f"#### {os.path.basename(fp)}\n{snippet}")
        remaining -= len(snippet)
    if not snippets:
        return ""
    return (
        "### Pinned User-Constraints (master-audit + spec, IMMER aktiv)\n\n"
        + "\n\n".join(snippets)
    )


def main() -> None:
    payload = _read_payload()
    prompt = _extract_prompt(payload)
    if len(prompt) < MIN_PROMPT_LEN:
        return

    token = _load_token()
    if not token:
        return

    report_hook_event("UserPromptSubmit", token, summary=prompt)  # best-effort (#5)

    # Pinned-block IMMER vorab — auch wenn search server down ist, sollen
    # die User-Constraints (master-audit, frust-patterns, spec) sichtbar sein.
    pinned_block = _render_pinned_block()
    pinned_prefix = (pinned_block + "\n\n---\n\n") if pinned_block else ""

    # WHY(2026-05-10 prompt-categorize): vor der search erst kategorien
    # extrahieren. Ein prompt kann mehrere themen berühren (auth + caching +
    # deployment) → wir geben sie als hint an die search weiter damit
    # treffer in passenden kategorien hoch-gerankt werden. Bei timeout/leer
    # einfach ohne hint suchen (ungefilterter fallback).
    prompt_categories = _categorize_prompt(prompt, token)

    # Project Router (Slice 1): einmal pro Session routen, in session_ctx.json
    # cachen, project_id + task an die Suche durchreichen.
    ctx = _read_session_ctx() or {}
    active = ctx.get("active_project")
    # Re-route bei fehlendem ODER transient gescheitertem (route-unreachable)
    # Cache — sonst friert ein Cold-Start (9s-Deploy-Window) das Routing für die
    # ganze Session auf "workspace-weit" ein.
    if active is None or (active or {}).get("reason") == "route-unreachable":
        active = _route_project(token, _git_remote(), prompt)
        if (active or {}).get("reason") != "route-unreachable":
            _write_session_ctx_field("active_project", active)
    project_id = (active or {}).get("project_id")
    task = _derive_task(prompt, (active or {}).get("name") or "")
    if active and project_id:
        proj_line = (f"📁 Projekt: {active.get('name') or project_id} "
                     f"({active.get('mode', '?')}, conf={active.get('confidence', 0)} "
                     f"· {active.get('reason', '?')})"
                     + (f" · task={task}" if task else "") + "\n\n")
    else:
        proj_line = f"📁 Projekt: — (workspace-weit, {(active or {}).get('mode', '?')})\n\n"
    pinned_prefix = proj_line + pinned_prefix

    results = _multi_lens_search(prompt, token, category_hint=prompt_categories or None,
                                 project_id=project_id, task_context=task)
    primary = results.get("primary") or {}
    if "_hook_error" in primary:
        # Sonderfall: deploy-typische 5xx (502/503/504) sind transient
        # — der Stack restartet gerade, in 10s ist alles wieder gut.
        # Kein lauter warning-block, weil das den User pro prompt
        # nervt und keine Aktion erfordert.
        all_5xx = all(
            (r or {}).get("_status") in (502, 503, 504)
            for r in results.values() if "_hook_error" in (r or {})
        )
        if all_5xx:
            # Silent skip auf API-Side, ABER pinned-block trotzdem injizieren
            # damit User-Constraints im prompt sind auch wenn search down ist.
            _record_silent_skip(reason="all_5xx")
            if pinned_block:
                print(pinned_block)
            return
        # Sonst: laut, weil der Fehler eine Aktion braucht (4xx, parse,
        # OSError, timeout). Lists ALL three lens errors at once.
        errs = [
            f"  - {lens}: {(r or {}).get('_hook_error', 'no response')}"
            for lens, r in results.items()
            if (r or {}).get("_hook_error")
        ]
        print(
            pinned_prefix
            + "## Memory: Hook konnte Memory nicht laden\n"
            f"_API={API}_  _prompt[:50]={prompt[:50]!r}_\n"
            + "\n".join(errs)
            + "\n\n_Wenn dieser Block wiederholt erscheint: API-Healthcheck "
              "(`curl https://mcp.linn.games/health`) prüfen oder Plugin neu "
              "laden (`/reload-plugins`)._"
        )
        return

    primary_ctx = (primary.get("prompt_context") or "").strip()
    primary_diag = (primary.get("diagnostics") or {}).get("vector_stage", "?")

    # WHY(2026-05-10 soft-skip): wenn ALLE max_sim-werte schwach sind, ist
    # die search rauschen. Lieber explizit "kein prior context" ausgeben
    # statt das LLM mit halbrelevanten chunks zu vergiften. Hard-block ist
    # falsch — Opus arbeitet weiter, nur ohne kontext-bias.
    def _max_sim(r: dict) -> float:
        diag = (r or {}).get("diagnostics") or {}
        vs = diag.get("vector_stage") or ""
        m = re.search(r"max_score=([0-9.]+)", vs if isinstance(vs, str) else "")
        if m:
            try:
                return float(m.group(1))
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    all_weak = all(
        _max_sim(r) < _MIN_PROMPT_SIM_THRESHOLD
        for r in (primary, results.get("ambient"), results.get("conversation"))
        if r and "_hook_error" not in r
    )
    cat_hint = (f" · kategorien={','.join(prompt_categories)}"
                if prompt_categories else "")

    if not primary_ctx or all_weak:
        print(
            pinned_prefix
            + f"## Memory: _No prior context — Thema neu_\n"
            f"_diag: {primary_diag}{cat_hint}_\n"
            f"_max_sim<{_MIN_PROMPT_SIM_THRESHOLD} bei allen lenses — keine "
            f"halb-relevanten chunks injiziert, damit nichts den fokus verwischt._"
        )
        return

    sections: list[str] = [
        "### Code/Findings (semantic search)",
        f"_diag: {primary_diag}{cat_hint}_",
        primary_ctx,
    ]

    ambient = results.get("ambient") or {}
    if ambient and "_hook_error" not in ambient:
        ambient_ctx = (ambient.get("prompt_context") or "").strip()
        if ambient_ctx:
            sections.append("\n### Ambient Snapshot (Projekt-Kontext)")
            sections.append(ambient_ctx)

    conv = results.get("conversation") or {}
    if conv and "_hook_error" not in conv:
        conv_ctx = (conv.get("prompt_context") or "").strip()
        if conv_ctx:
            sections.append("\n### Vorherige Sessions / Decisions")
            sections.append(conv_ctx)

    # Pair each chunk with its source_id + capture text so the Stop hook
    # can LLM-judge whether the chunk's content was actually used in the
    # answer. Format is parsed by stop_hook._CHUNK_LINE_RE — keep stable.
    seen_ids: set[str] = set()
    chunk_pairs: list[tuple[str, str]] = []
    chunk_texts: dict[str, str] = {}
    for r in (primary, ambient, conv):
        if not r or "_hook_error" in r:
            continue
        for chunk in (r.get("results") or []):
            cid = chunk.get("chunk_id", "")
            sid = chunk.get("source_id", "")
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                chunk_pairs.append((cid, sid))
                txt = chunk.get("text") or ""
                if txt:
                    chunk_texts[cid] = txt

    # Persist for the Stop hook (transcript doesn't capture this block).
    _write_inject_state(
        payload.get("session_id", ""),
        chunk_pairs[:8],
        chunk_texts=chunk_texts,
        user_prompt=prompt,
    )

    chunk_id_hint = ""
    if chunk_pairs:
        chunk_id_hint = (
            "\n\n_Injected chunks (auto-feedback by Stop hook):_\n"
            + "\n".join(f"- `{cid}` : `{sid}`" for cid, sid in chunk_pairs[:8])
        )

    print(
        pinned_prefix
        + f"## Memory-Kontext für diesen Prompt\n\n"
        + "\n\n".join(sections)
        + chunk_id_hint
    )


if __name__ == "__main__":
    main()
