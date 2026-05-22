# MayringCoder Plugin — Memory + Pi-Agent + Reranker

Memory-Server: `mcp.linn.games` (Claude.ai-Cloud-Profil-Connector,
nicht aus `.mcp.json`). Plugin liefert lokal: `memory-agents` MCP-Server
(Pi-Agent-Tools) + Hooks für UserPromptSubmit + Stop-Capture.

Workspace-ID kommt aus dem JWT — typischer Slug: `bene` (per email-slug
aus `benedikt.linn@code.berlin`). Service-Token-Calls landen unter
`workspace_id="system"`.

---

## 1. Memory-Pipeline-Workflow

### Sessionbeginn / neuer Task
Hook macht UserPromptSubmit-Suche automatisch. Manueller Call wenn die
Hook-Suche zu unspezifisch war:

```python
mcp__claude_ai_Memory__search_memory(query="<aktueller Task>", workspace_id="<slug>")
```

Die zurückgegebenen `chunk_id`s für späteres Feedback merken (Stop-Hook
bewertet sie auch automatisch wenn die source_id-Pfade in deiner
Antwort vorkommen).

### Nach `/compact`
```python
mcp__claude_ai_Memory__search_memory(query="<Task>", workspace_id="<slug>", compacted=True)
```

### Chunk-Feedback (PFLICHT nach jedem Task)
```python
# Rating 1..5 (binary positive/negative seit 2026-05-10 raus):
#   1 = schadhaft/irrelevant   3 = neutral   5 = primärquelle
mcp__claude_ai_Memory__feedback(chunk_id="...", signal="5", metadata={"task":"..."})
mcp__claude_ai_Memory__feedback(chunk_id="...", signal="2")  # kaum relevant
```

Stop-Hook macht Auto-Feedback per LLM-judge (mistral:7b-instruct), bewertet
inhaltliche Verwendung in der Antwort statt Pfad-Match. In unsicheren
Fällen kannst du explizit überschreiben mit einem höheren/niedrigeren Rating.

### Info ingesten (Mayring-Vorverarbeitung)
1. **Paraphrase** — Kernaussage ohne Füllwörter
2. **Generalisieren** — Meta-Kategorie (architecture / debug / config / decision)
3. **Reduzieren** — Ein Satz

```python
mcp__claude_ai_Memory__ingest(
  source="<reduzierter Kerninhalt>",
  source_id="<kategorie>:<YYYY-MM-DD-thema>",
  workspace_id="<slug>"
)
```

Quell-Typen: `session-memory:` · `architecture:` · `debug:` · `config:` ·
`session:` (für End-Of-Session-Zusammenfassungen) · `context:` (für
Datei/Thema-bezogene Erkenntnisse).

Nach jedem `git push` auf MayringCoder triggert die v2-chain auto-ingest.

---

## 2. Pi-Agent (#183 in-process Queue)

**Stand 2026-05-09**: Pi-Agent läuft über eine in-process `PiQueue` mit
2 Worker-Coroutines. `/pi-task` enqueued einen `PiJob`, awaited das
Future, gibt `{workspace_id, content}` zurück (backward-compat).

**MCP-Tool für Cloud-Pi-Tasks** (Subagent-Alternative):

```python
mcp__plugin_mayring-coder_memory-agents__pi_task(
  task="<Aufgabenbeschreibung>",
  repo_slug="<repo-slug>",
  timeout=180.0
)
```

### Welches Tool für welche Aufgabe — Entscheidungstabelle

| Use-Case | Tool | Warum |
|---|---|---|
| Konkrete Implementierung mit Memory-Kontext | `pi_task` (free-form) | lokales Ollama, ~$0, three.linn.games GPU |
| Find / locate / patch this bug | `pi_task` | scoped retrieval via repo_slug |
| Test-Loop iterieren | `pi_task` | |
| **Chunk(s) Mayring-kategorisieren (labels)** | `pi_categorize(text, task, codebook?, mode)` | gibt `{labels:[str]}` für den ganzen chunk. `task` = das Thema worauf untersucht wird (Mayring Selektionskriterium); `mode` = inductive/deductive/hybrid. Nutzt die kanonischen `prompts/mayring_{mode}.md` |
| **Mayring-kategorisieren MIT Textbeleg pro Kategorie** | `pi_mark_categories(text, task, codebook?)` | "Textmarker" — markiert konkrete Abschnitte + ordnet jedem eine Kategorie zu MIT paraphrase-begründung. `{markings:[{span:[start,end], excerpt, category, reasoning}]}`. Nutze das wenn die Kategorie nachvollziehbar am Text hängen soll (statt nur "chunk hat label x") |
| **Chunk-Relevanz zu einer Query bewerten** | `pi_judge_relevance` | ersetzt LLM-judge im stop_hook/rerank — 0..1 score pro chunk |
| **Text für Memory-Ingest reduzieren** | `pi_summarize_for_memory` | 3-step Mayring (paraphrase→generalize→reduce) + suggested_source_id |
| Architektur-Trajektorie einer Datei | `diff_history` | git log --follow -p → trajectory/obsolete/active |
| Code-Review / Multi-File-Refactor | Subagent (`mayring-coder:pi-subagent` ODER general-purpose, mit Pre-Fetch!) | mehr kontext-budget |
| Architektur-/Strategieentscheidung | Self (main session) | judgment call |
| Sensitive Secrets | Self (kein subprocess) | |

**Faustregel:** Wenn die Aufgabe in eines der spezialisierten Tools passt
(`pi_categorize` / `pi_judge_relevance` / `pi_summarize_for_memory`), nimm
das — die fokussierten Prompts + JSON-mode geben strukturierte Outputs,
die du direkt weiterverarbeiten kannst, ohne den Pi-Agent komplett zu
re-instruct. Nur wenn nichts passt → `pi_task` (free-form). Nur wenn
`pi_task` fail't ODER frontier-reasoning nötig → Claude-Subagent.

Asymmetric job-distribution (CLAUDE.md-Präferenz): 90% der categorize/
judge/summarize-calls können lokal laufen statt in Claude — pure
token-/latency-ersparnis bei gleichem memory-zugriff.

### Pi-Job-Klassen (#183 T1-T4)

`classify_pi_job(task, system_prompt)` mappt heuristisch:
- `mini` (< 500 chars total) → `phi3:3.8b` mit 30s timeout (wenn yaml-config classes-Block)
- `standard` → `mistral:7b-instruct` mit 240s
- `test` → opt-in via `kind_hint` (Anti-Gaming-Probe-Stub)

`/pi-jobs/stats` liefert p50/p95-Latenz pro job_class + fallback_rate.

### Wenn pi_task fehlschlägt
- `{"error": "Ollama nicht erreichbar"}` → Ollama fehlt lokal
- Tool nicht da → Plugin nicht enabled (`claude plugin list`, dann `/reload-plugins`)

---

## 3. Reranker-v2 (Issue #180/#184/#187)

**Stand 2026-05-09**: v2-Modell trainiert, sanity-gated, A/B-aktiv.

- `cache/rerank_default.txt` steuert: `v1` | `v2` | `auto` (50/50 hash-split)
- Sanity-Gate: `_load_model()` lehnt Modelle mit `v_w<0` oder `s_w<0` (oder `pt_w<0`/`re_w<0`) ab — fallback v1
- Trainings-Features: `(v, s, r, a, pt, re, igio_*)` — pt = predicted-topic-boost, re = rationale-edge-presence
- Training-Pipeline-Cron: täglich 07:30 UTC (`train-reranker.yml`)
- Auto-Rollout-Cron: 08:00 UTC, flippt default wenn ndcg-Uplift ≥25% bei n_v2 ≥30 queries

### Manueller Train-Run + Switch
```bash
ssh nileneb@u-server 'docker exec mayring-mayring-api-1 sh -c "cd /app && \
  PYTHONPATH=. python3 tools/export_retrieval_dataset.py --days 30 && \
  PYTHONPATH=. python3 tools/train_reranker.py"'

ssh nileneb@u-server 'docker exec mayring-mayring-api-1 sh -c "echo v2 > cache/rerank_default.txt"'
```

---

## 4. Rationale-Edges (Issue #185)

WHY-Marker im Code werden beim repo-analyze als `wiki_edges` mit
`type='rationale'` persistiert und ins `/memory/search`-Result als
`rationale_edges`-Block co-injected.

Format:

```python
# WHY(#issue, kategorie): freier text, multi-line ok wenn nächste
# zeile mit '# ' weitergeht. CHANGE WITH CARE wenn defensive.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\Z")
```

Targets: nur `Assign`/`FunctionDef`/`AsyncFunctionDef`/`ClassDef`
direkt nach dem Marker. Vor `for`/`if`/`try` → skip + warn.

---

## 5. IGIO Classifier

`igio_axis` (issue/goal/intervention/outcome) wird per Hintergrund-Cron
auf chunks gesetzt. Coverage > 80% Stand 2026-05-09. Test-Pending bis
ratio ≥ 50% (`smoke check_igio_axis_on_chunks` als Trigger).

Backfill manuell:
```bash
ssh nileneb@u-server 'TOK=$(grep ^MCP_SERVICE_TOKEN= ~/app.linn.games/.env | cut -d= -f2-); \
  curl -X POST "https://mcp.linn.games/stats/igio-backfill?limit=1500&min_confidence=0.4" \
       -H "Authorization: Bearer $TOK"'
```

---

## 6. Hooks (Plugin-managed)

- `UserPromptSubmit` (claude-plugin/hooks/memory_inject.py): 3 lens-search
  (primary + ambient + conversation), 4 retries bei 5xx, silent-skip
  während deploy-windows, persistiert chunk-IDs für Stop-Auto-Feedback.
- `Stop` hook (claude-plugin/hooks/stop_hook.py): liest inject-state,
  matcht source_ids gegen Assistant-Antwort, ratet jede Chunk-ID
  positive/negative.

Plugin-troubleshooting: `claude plugin list`, dann `/reload-plugins`.
Bei 5xx aus dem Hook-Log: API-Healthcheck (`curl https://mcp.linn.games/health`)
oder deploy-window abwarten (~30s).

---

## 7. Wichtige Dateien (für Subagent-Pre-Fetch-Queries)

- `src/memory/retrieval.py::search` — 4-stage Hybrid-Search
- `src/memory/predictive.py` — Markov-Transitions + path-traversal-defense
- `src/wiki_v2/rationale_parser.py` — WHY-marker-AST-parser
- `src/agents/pi_queue.py` — in-process PiQueue (#183 T2)
- `src/api/routes/memory.py::pi_task` — refactored zu queue (#183 T3)
- `src/api/routes/jobs.py::_run_with_v2_postingest` — v2-chain (ambient/predictive/images/rationale/overview→wiki)
- `tools/subagent_prefetch.py` — Memory-block-Generator für Subagent-Dispatch
- `tools/train_reranker.py` + `tools/export_retrieval_dataset.py` — Trainings-Pipeline mit pt + re Features
