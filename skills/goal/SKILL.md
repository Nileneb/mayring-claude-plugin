---
name: mayring-coder:goal
description: Extrahiert Mayring-Kategorien + IGIO-Goals aus dem aktuellen Task und speichert sie in Memory. Automatisch verdrahtet mit WIKI_V2 über den IGIO-Classifier. Nutze diesen Skill wenn du Ziele aus dem aktuellen Prompt ableiten oder bestehende Workspace-Ziele anzeigen willst.
---

# /goal — IGIO Goal Extraction + Wiki_V2 Feed

## Was dieser Skill tut

Dieser Skill schließt die Lücke zwischen User-Prompt und IGIO/Wiki_V2-Pipeline.
Er ruft `pi_categorize` (die EINE Mayring-Methode) auf, extrahiert die **goal**-Axis
und schreibt das Ergebnis direkt in Memory — sofort sichtbar, kein Cron-Warten.

`/goal` = `pi_categorize` + IGIO-Axis-Klassifikation. Es gibt EINE Kategorisierungs-
Methode (immer mixed, ein Codebook, domänenunabhängig) — `pi_categorize` liefert
Kategorie **und** Paraphrase/Generalisierung in einem Schritt.

## Ablauf (PFLICHT, in dieser Reihenfolge)

### Schritt 1 — Aktuelle Workspace-Ziele anzeigen
```python
mcp__claude_ai_Memory__search_memory(
    query="goal objective aim target we want to achieve",
    workspace_id="<workspace_slug>",
    top_k=8,
)
```
Zeige die zurückgegebenen Chunks mit `igio_axis=goal` als geordnete Liste.
Format: `**[Datum]** Ziel: <text>`

### Schritt 2 — Aktuellen Prompt kategorisieren (die EINE Methode)
```python
mcp__plugin_mayring-coder_memory-agents__pi_categorize(
    text="<aktueller User-Prompt / Task-Beschreibung>",
    task="Was ist das Hauptziel dieser Session? Welche Mayring-Kategorie trifft zu?",
)
```
Das Ergebnis liefert in EINEM Schritt (kein separates summarize mehr):
- `label` → die EINE Mayring-Kategorie (embedding-gematcht: trifft Bestand statt Duplikat)
- `match` → `deductive` (bestehende getroffen) | `dedup` | `inductive` (neu gebildet)
- `paraphrase` → Kernaussage des Prompts
- `generalize` → die generalisierte Kategorie-Ebene
- Bestimme die IGIO-Axis: ist der Prompt primär ein **goal** (Anstreben), **issue** (Problem), **intervention** (Umsetzung), oder **outcome** (Ergebnis)?

### Schritt 3 — Als Goal in Memory speichern
`source_id` selbst bauen: `goal:<YYYY-MM-DD>:<kurz-slug aus paraphrase>`.
```python
mcp__claude_ai_Memory__ingest(
    source="<paraphrase aus Schritt 2>",
    source_id="goal:<YYYY-MM-DD>:<kurz-slug>",
    workspace_id="<workspace_slug>",
    metadata={
        "igio_axis": "goal",
        "igio_confidence": 0.9,
        "category_labels": "<label aus Schritt 2>",
        "session_source": "goal_skill",
    },
)
```

### Schritt 4 — Zusammenfassung ausgeben

Zeige dem User:
```
## Ziel erfasst ✓

**Mayring-Kategorie:** <label> (<match>)
**IGIO-Axis:** goal
**Memory-ID:** goal:2026-05-15:xyz

**Abgeleitet:** <paraphrase>

**Bestehende Workspace-Ziele:** <N>
```

## Wann dieser Skill NICHT nötig ist

- Bei trivialen Prompts (< 20 Wörter) → stop_hook macht IGIO fast-hints automatisch
- Nach `/compact` → PostCompact-Hook ingested die Summary bereits
- Wenn der User explizit sagt "kein Goal speichern"

## Fehlerfälle

- `pi_categorize` schlägt fehl → trotzdem Schritt 3 ohne `category_labels` ausführen
- `ingest` schlägt fehl → Fehler LAUT melden, NICHT still schlucken (Pattern #3)
