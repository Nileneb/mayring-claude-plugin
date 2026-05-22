---
description: Persistiert offenen Plan/Tasks ins Memory, dann manuelles /clear
---

# /clear-but-keep-plan

Zweck: Conversation-Window leeren, ohne den Plan zu verlieren. Memory + Pi-Agent + UserPromptSubmit-Hook geben den Kontext beim nächsten Prompt zurück.

## Was du jetzt tust

1. **Plan-Snapshot extrahieren** — kondensiere den aktuellen Stand auf:
   - **Ziel**: 1 Satz, was die Session erreichen soll
   - **Status**: was ist fertig, was läuft, was blockiert
   - **Offene Tasks**: Liste der nächsten Schritte (Imperativ, je 1 Zeile)
   - **Wichtige Dateien**: Pfade die der nächste Run wieder anfassen muss
   - **Entscheidungen**: alles was nicht aus dem Code/Git ableitbar ist
   - **Fallen**: bekannte Stolpersteine die der nächste Run wissen muss

2. **Ins Memory schreiben** — ein Aufruf, kein Vorabzeigen:
   ```
   mcp__claude_ai_Memory__ingest(
     source="<der oben strukturierte Snapshot, ein zusammenhängender Markdown-Block>",
     source_id="plan-snapshot:<YYYY-MM-DD-HHMM>-<3-wort-thema-slug>",
     workspace_id="<repo-slug>"
   )
   ```
   Quittiere mit der zurückgegebenen `source_id`.

3. **User informieren** — gib genau diesen Block aus, sonst nichts:
   ```
   ✓ Plan gesichert als source_id=<id>
   → Drück jetzt /clear. Beim nächsten Prompt zieht memory_inject den Plan automatisch.
   ```

## Was du NICHT tust

- Kein /clear selbst auslösen — das muss der User. Slash-Commands können das Window nicht leeren.
- Kein Code ändern.
- Keine zusätzliche Erklärung. Eine Zeile Quittung reicht.
- Keine Plan-Wiedergabe vor dem ingest. Der ingest IST die Wiedergabe.

## Workspace-Slug ableiten

`pwd` Basename, lowercase. Beispiele:
- `/home/nileneb/Desktop/MayringCoder` → `mayringcoder`
- `/home/nileneb/code/app.linn.games` → `app.linn.games` oder `applinngames`

Wenn unklar, frag NICHT — wähle den Basename und mach weiter.
