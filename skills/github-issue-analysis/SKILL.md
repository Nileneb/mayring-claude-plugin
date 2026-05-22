---
name: github-issue-analysis
description: Use when the user mentions a GitHub issue number, URL, or asks to work on/fix/implement/triage an issue — BEFORE starting any implementation or investigation
---

# GitHub Issue Analysis

## Overview

Blind code-diving before loading context wastes time and misses systemic risks.
Turbulence data reveals which files are structurally fragile — before you touch them.

**Core principle:** ALWAYS load issue context + memory + turbulence BEFORE writing code.

**Violating the letter of this process is violating the spirit of issue-driven development.**

## The Iron Law

```
NO IMPLEMENTATION WITHOUT CONTEXT LOAD FIRST
```

If you haven't completed Phase 1 (Context), you cannot propose code changes.

## When to Use

- User mentions `#<number>`, `issue/<number>`, or a GitHub issue URL
- User says "fix", "implement", "work on", "close", "tackle" + an issue reference
- Starting work on a feature or bug that has a GitHub issue

## The Four Phases

### Phase 1: Issue + Memory Context

**BEFORE writing a single line of code:**

1. **Load the issue**
   ```bash
   gh issue view <nummer> --repo <repo> --json number,title,body,labels,comments
   ```
   Extract: affected files/modules, error messages, reproduction steps, linked PRs.

2. **Search memory for related context**
   ```
   mcp__claude_ai_Memory__search_memory(
     query="<issue-titel + schlüsselbegriffe aus body>",
     workspace_id="<repo-slug>"
   )
   ```
   Note all returned `chunk_id`s — needed for Phase 4 feedback.

3. **State what you know**
   - What does the issue describe?
   - Which files/modules are affected?
   - Is there prior art in memory?

### Phase 2: Turbulence Check

**For every file mentioned in the issue:**

Check turbulence cache:
```bash
# MayringCoder issues:
cat /home/nileneb/Desktop/MayringCoder/cache/*turbulence*.json 2>/dev/null | \
  python3 -c "import json,sys; d=json.load(sys.stdin); \
  [print(f'{k}: score={v.get(\"turbulence_score\",0):.2f}') \
   for k,v in d.get('files',{}).items() if '<dateiname>' in k]"

# app.linn.games issues:
cat /home/nileneb/Desktop/MayringCoder/cache/nileneb-applinngames_turbulence.json 2>/dev/null | \
  python3 -c "import json,sys; d=json.load(sys.stdin); \
  [print(f'{k}: score={v.get(\"turbulence_score\",0):.2f}, smells={v.get(\"top_smells\",[])}') \
   for k,v in d.get('files',{}).items() if '<dateiname>' in k]"
```

Bewertung:
- `score > 0.5` → High Risk: besonders vorsichtig vorgehen, Tests zuerst
- `score 0.2–0.5` → Medium Risk: gründlich testen
- `score < 0.2` → Low Risk: normale Vorsicht

Falls kein Cache vorhanden: Hinweis ausgeben, trotzdem mit Phase 3 fortfahren.

### Phase 3: Triage-Zusammenfassung

Erstelle IMMER diese strukturierte Übersicht vor dem ersten Code-Edit:

```
## Triage — Issue #<n>: <titel>

**Typ:** <bug | feature | performance | security | refactor>
**Betroffene Dateien:** <liste>
**Turbulenz-Risiko:** <hoch | mittel | niedrig> (<datei>: <score>)
**Memory-Kontext:** <n> relevante Chunks geladen
**Empfehlung:** <1-2 Sätze: Einstiegspunkt, kritische Stellen, Teststrategie>
```

Diese Ausgabe dient als Alignment-Checkpoint: zeige sie, BEVOR du Code änderst.

### Phase 4: Abschluss — Memory schreiben

**Nach Abschluss der Arbeit am Issue:**

1. **Triage + Ergebnis ins Memory**
   ```
   mcp__claude_ai_Memory__conversation_ingest(
     turns=[{"role":"assistant",
             "content":"<Triage-Zusammenfassung + was wurde implementiert/geändert>",
             "timestamp":"<ISO>"}],
     session_id="github-issue-<repo-slug>-<nummer>",
     workspace_slug="<repo-slug>",
     presumarized="Issue #<n> (<titel>): <typ>, Dateien: <liste>, \
Turbulenz: <risiko>, gelöst durch: <1 Satz>"
   )
   ```

2. **Feedback für Memory-Chunks** (rating 1..5, seit 2026-05-10 kein binary mehr)
   ```
   mcp__claude_ai_Memory__feedback(
     chunk_id="<id>",
     signal="5",  # 4=wichtig, 5=primärquelle, 3=neutral, 2=kaum, 1=schadhaft
     metadata={"issue":"<nummer>","task":"<titel>"}
   )
   ```
   Irrelevante Chunks: `signal="2"` oder `"1"`.

## Red Flags — STOP

- Code öffnen bevor Phase 1 abgeschlossen ist
- "Das Issue ist einfach, kein Kontext nötig" — einfache Issues haben auch Kontext
- Turbulenz überspringen weil "nur kleine Änderung" — gerade kleine Änderungen in fragilen Dateien brechen alles
- Phase 4 weglassen — Memory bleibt dann leer für die nächste Session

## Workspace-Slug Konvention

`Nileneb/MayringCoder` → `mayringcoder`
`Nileneb/app.linn.games` → `applinngames` oder `app-linn-games`

Wenn unklar: `gh repo view --json nameWithOwner` im aktuellen Verzeichnis.
