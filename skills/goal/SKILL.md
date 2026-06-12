---
name: mayring-coder:goal
description: Zeigt offene Tasks und kategorisiert den aktuellen Prompt via Mayring-Methode (pi_categorize). Legt konkrete Schritte als native Todos an — task_capture-Hook spiegelt sie automatisch nach /tasks.
---

# /goal — Mayring-Kategorie + native Todos

## Ablauf (PFLICHT, in dieser Reihenfolge)

### Schritt 1 — Offene Tasks zeigen
```python
mcp__plugin_mayring-coder_memory-agents__task_list(status="open")
```
Zeige die zurückgegebenen Tasks als geordnete Liste.
Format: `- [status] Titel`

### Schritt 2 — Aktuellen Prompt kategorisieren (die EINE Methode)
```python
mcp__plugin_mayring-coder_memory-agents__pi_categorize(
    text="<aktueller User-Prompt / Task-Beschreibung>",
    task="Was ist das Hauptziel dieser Session? Welche Mayring-Kategorie trifft zu?",
)
```
Das Ergebnis liefert in EINEM Schritt:
- `label` → die EINE Mayring-Kategorie (embedding-gematcht)
- `match` → `deductive` | `dedup` | `inductive`
- `paraphrase` → Kernaussage des Prompts
- `generalize` → die generalisierte Kategorie-Ebene

### Schritt 3 — Konkrete Schritte als native Todos anlegen
Leite aus dem kategorisierten Prompt die konkreten Umsetzungsschritte ab und schreibe sie via `TodoWrite`:
```python
TodoWrite(todos=[
    {"content": "<konkreter Schritt 1>", "status": "pending", "activeForm": "<-ing Form>"},
    {"content": "<konkreter Schritt 2>", "status": "pending", "activeForm": "<-ing Form>"},
])
```
Der `task_capture`-PostToolUse-Hook spiegelt diese Todos idempotent nach `/tasks`.
Abhaken passiert automatisch wenn Claude den Todo-Status auf `completed` setzt.

### Schritt 4 — Zusammenfassung ausgeben
```
## Erfasst

**Mayring-Kategorie:** <label> (<match>)
**Native Todos:** <N angelegt>

**Abgeleitet:** <paraphrase>

**Offene Tasks vorher:** <N>
```

## Was NICHT passiert

- Kein Memory-Ingest von Goals via `/memory/put` — das wäre redundant.
- Das Session-Goal fließt automatisch als `task=`-Parameter in den micro-batch
  (canonical-goal-Anchoring im Stop-Hook) — ohne manuellen Ingest.

## Wann dieser Skill NICHT nötig ist

- Bei trivialen Prompts (< 20 Wörter)
- Nach `/compact` → PostCompact-Hook ingested die Summary bereits
- Wenn der User explizit sagt "kein Goal"

## Fehlerfälle

- `pi_categorize` schlägt fehl → trotzdem Todos anlegen, ohne `category_labels`
- `task_list` schlägt fehl → Fehler LAUT melden, weitermachen (soft-fail nur bei der Liste)
