#!/usr/bin/env python3
"""Stop hook — captures every turn pair AND auto-rates injected chunks.

Two responsibilities, both fire-and-forget (exit 0 always):

1. **Turn capture** — POST the last user/assistant pair to
   /conversation/micro-batch, server-side summariser dedups via
   `conversation:<workspace>:<session>`. Closes the gap between /compact
   events; Memory sees every completed turn.

2. **Auto-feedback** — `memory_inject` (UserPromptSubmit hook) writes a
   block of `chk_xxx : source_id` lines into the prompt context. After
   the assistant has answered, Stop parses those lines back out and
   classifies each chunk:

       positive  → the source's path/basename appears in the assistant's
                   answer (≥5 chars, the path was actually used)
       negative  → injected but never referenced

   That's the auto-feedback that should have run on every memory
   injection from the start. Heuristic, not perfect — but a real signal
   instead of nothing, and it costs zero LLM calls.

Workspace slug derives from CWD basename.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

_JWT_FILE = os.path.expanduser("~/.config/mayring/hook.jwt")
_API_URL = os.environ.get("MAYRING_API_URL", "https://mcp.linn.games").rstrip("/")
_TIMEOUT = 10  # micro-batch summarises a turn pair on the server (LLM call)

# Device↔Cloud-Kanal (#5): X-Device-Id auf Cloud-Calls + best-effort Hook-Report.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _device import device_headers, report_hook_event
except ImportError:
    def device_headers() -> dict:  # type: ignore[misc]
        return {}

    def report_hook_event(*_a, **_k) -> None:  # type: ignore[misc]
        pass

# C3: read the session's resolved project (written by memory_inject's router) +
# the cwd remote, so the micro-batch can stamp X-Project-Id + origin_ref. Best-
# effort: any failure → no stamp (chunks stay global), never breaks the hook.
try:
    from _session_ctx import read_session_ctx as _read_session_ctx, _git_remote
except ImportError:
    def _read_session_ctx(*_a, **_k):  # type: ignore[misc]
        return None

    def _git_remote(*_a, **_k):  # type: ignore[misc]
        return None


def _project_stamp() -> tuple[str | None, str]:
    """(project_id, origin_ref) for the current session, fail-soft → (None, '')."""
    try:
        active = (_read_session_ctx(max_age=0) or {}).get("active_project") or {}
        return active.get("project_id"), (_git_remote() or "")
    except Exception:
        return None, ""

try:
    from _memory_put import put_memory
except ImportError:
    # Standalone-snapshot fallback: single-shot POST, no retry/queue/refresh.
    def put_memory(content, source_id, source_type, token, *, igio_hint=None,
                   categorize=True, **_k):  # type: ignore[misc]
        body = {"source_id": source_id, "source_type": source_type,
                "content": content, "categorize": categorize}
        if igio_hint:
            body["igio_hint"] = igio_hint
        try:
            req = urllib.request.Request(
                f"{_API_URL}/memory/put", data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {token}", **device_headers()},
                method="POST")
            urllib.request.urlopen(req, timeout=_TIMEOUT)
            return 200
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            return 0

# Local fallback queue — when /memory/feedback fails after retries (deploy
# window, network hiccup, ANY 5xx), the entry gets appended here.
# session_start.py::_drain_feedback_queue() replays everything to REST on
# the next SessionStart. Successful replay → entry removed. 4xx → dropped
# (no retry will fix it). 5xx → stays for next session.
_FEEDBACK_QUEUE = os.path.expanduser("~/.config/mayring/feedback_queue.jsonl")
# Same idea for /conversation/micro-batch — when the server is down or
# slow, capture-events get queued here. Schema: full POST body of the
# micro-batch endpoint. Drain in session_start::_drain_ingest_queue.
_INGEST_QUEUE = os.path.expanduser("~/.config/mayring/ingest_queue.jsonl")
               # — was 5s, frequently hit the deadline mid-summary and silently
               # dropped the turn. 10s buys headroom without blocking Stop.

_MAX_TURN_CHARS = 4000      # truncate per-turn content fed to the server
_TURN_PAIR_LIMIT = 2        # one user + one assistant turn

_AUTO_FEEDBACK_LIMIT = 8    # max chunks to rate per turn
_PATH_KEY_MIN_LEN = 5       # avoid spurious matches on tiny basenames

# memory_inject persists per-session (chunk_id, source_id) pairs in this
# directory because the inject block isn't part of the user-turn content
# in the transcript JSONL — without the file, the Stop hook would never
# see what was injected. See memory_inject._write_inject_state.
_INJECT_STATE_DIR = os.path.expanduser("~/.config/mayring/inject-state")

# Per-session marker of the last session-goal we ingested → avoids re-POSTing an
# unchanged /goal every turn. Advances ONLY on a successful POST, so a failed POST
# is automatically retried next turn (the goal is still active in the transcript).
_SESSION_GOAL_STATE = os.path.expanduser("~/.config/mayring/session-goal-state.json")

# Legacy regex kept for transcripts that DO contain the block inline
# (e.g. paste-ins, debugging). New canonical source is the state file.
_CHUNK_LINE_RE = re.compile(r"`(chk_[a-f0-9]{16})`\s*:\s*`([^`]+)`")


def _read_token() -> str:
    try:
        with open(_JWT_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def _read_payload() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return {}


def _workspace_slug() -> str:
    return os.path.basename(os.getcwd()).lower() or "default"


def extract_last_turn_pair(transcript_path: str) -> list[dict]:
    """Read the JSONL transcript and return [last_user_turn, last_assistant_turn].

    Each entry is a dict with `role`, `content`, `timestamp`. Skips meta-rows
    (`type` not in {"user","assistant"}). Content is flattened from Claude
    Code's structured `message.content` (list of blocks) to plain text.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return []
    last_user: dict | None = None
    last_assistant: dict | None = None
    try:
        with open(transcript_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                t = row.get("type")
                if t not in ("user", "assistant"):
                    continue
                msg = row.get("message") or {}
                role = msg.get("role") or t
                content = _flatten_content(msg.get("content"))
                if not content.strip():
                    continue
                turn = {
                    "role": role,
                    "content": content[:_MAX_TURN_CHARS],
                    "timestamp": row.get("timestamp", ""),
                }
                if role == "user":
                    last_user = turn
                elif role == "assistant":
                    last_assistant = turn
    except OSError:
        return []
    out = []
    if last_user:
        out.append(last_user)
    if last_assistant:
        out.append(last_assistant)
    return out[-_TURN_PAIR_LIMIT:]


def _flatten_content(content) -> str:
    """Coerce Claude Code's structured content to a flat string.

    Accepts: str | list[dict|str] | None. Tool-use/tool-result blocks are
    skipped — their JSON args are noisy and rarely useful for memory.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    parts.append(str(block.get("text", "")))
                elif btype in ("thinking", "redacted_thinking"):
                    continue
        return "\n".join(p for p in parts if p)
    return str(content)


_ACTION_LIMIT = 30   # max tool-action lines folded into one turn capture
# Keys probed (in order) for the human-meaningful target of a tool call.
_TOOL_TARGET_KEYS = ("file_path", "command", "path", "pattern", "url",
                     "query", "description", "prompt")


def _render_tool_use(block: dict) -> str | None:
    """Compact one tool_use block to ``Name: target`` (e.g. ``Edit: src/foo.py``).

    WHY(write-leak): the raw tool args/outputs are huge + get categorically
    dropped server-side (conversation_filter diff-skip). A compact action line
    captures WHAT was done without tripping that filter — and feeds the
    recency-lane real substance instead of prose-only."""
    name = (block.get("name") or "").strip()
    if not name:
        return None
    inp = block.get("input") or {}
    target = ""
    if isinstance(inp, dict):
        for k in _TOOL_TARGET_KEYS:
            v = inp.get(k)
            if v:
                target = str(v)
                break
    target = target.replace("\n", " ").strip()[:120]
    return f"{name}: {target}" if target else name


def _has_text_block(content) -> bool:
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and b.get("type") == "text" and str(b.get("text", "")).strip()
            for b in content
        )
    return False


def extract_turn_actions(transcript_path: str) -> list[str]:
    """Tool-Aktionen (Edit/Bash/Write/…) seit dem letzten User-Prompt.

    Das ist die EIGENTLICHE Arbeit eines Turns, die _flatten_content strippt.
    Reset bei jedem neuen User-PROMPT (Text-Turn); tool_result-only-User-Turns
    (kein Text) resetten NICHT, sonst ginge die Aktionskette pro Tool-Roundtrip
    verloren. Gekappt auf die letzten _ACTION_LIMIT."""
    if not transcript_path or not os.path.exists(transcript_path):
        return []
    actions: list[str] = []
    try:
        with open(transcript_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if row.get("type") not in ("user", "assistant"):
                    continue
                msg = row.get("message") or {}
                role = msg.get("role") or row.get("type")
                content = msg.get("content")
                if role == "user" and _has_text_block(content):
                    actions = []  # new prompt → window resets to "since last prompt"
                    continue
                if role == "assistant" and isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            rendered = _render_tool_use(block)
                            if rendered:
                                actions.append(rendered)
    except OSError:
        return []
    return actions[-_ACTION_LIMIT:]


_IGIO_FAST_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b(ziel|goal|goals|objective|objectives|wir\s+wollen|we\s+want|anstreben|vorhaben|soll\s+sein)\b", re.I), "goal"),
    (re.compile(r"\b(bug|fehler|problem|issue|broken|regression|traceback|error|exception|kaputt|falsch)\b", re.I), "issue"),
    (re.compile(r"\b(implementier|refactor|bauen|build|fix|deploy|migrate|schreib|erstell|ändere|update)\b", re.I), "intervention"),
    (re.compile(r"\b(ergebnis|result|outcome|test\s+grün|tests?\s+pass|fertig|done|abgeschlossen|deployed)\b", re.I), "outcome"),
)
_IGIO_PRIORITY = ("issue", "goal", "intervention", "outcome")


def _igio_fast_hint(text: str) -> str | None:
    """Regex-only IGIO axis detection — no LLM, <1ms. Returns axis or None."""
    if not text:
        return None
    scores: dict[str, int] = {}
    for pattern, axis in _IGIO_FAST_PATTERNS:
        scores[axis] = scores.get(axis, 0) + len(pattern.findall(text))
    if not any(scores.values()):
        return None
    best_score = max(scores.values())
    for axis in _IGIO_PRIORITY:
        if scores.get(axis, 0) == best_score:
            return axis
    return None


def _post_micro_batch(turns: list[dict], session_id: str, workspace_slug: str, token: str,
                      igio_hint: str | None = None, project_id: str | None = None,
                      origin_ref: str = "", task: str = "") -> int:
    body_dict: dict = {
        "turns": turns,
        "session_id": session_id,
        "workspace_slug": workspace_slug,
    }
    if igio_hint:
        body_dict["igio_hint"] = igio_hint
    # B.2 (goal→category): thread the active session-/goal as the categorize task so the
    # server anchors this conversation's inductive categories to the canonical goal
    # (core B.1 upsert_canonical_goal). Empty = unchanged (categories stay goal-less).
    if task:
        body_dict["task"] = task
    # C3: stamp the session's project so the server links these conversation
    # chunks (producer B). origin_ref = the cwd's canonical repo URL (nested-repo
    # aware). X-Project-Id header mirrors the X-Device-Id pattern; without it the
    # micro-batch chunks stay global (no link) — exactly the pre-C3 behaviour.
    if origin_ref:
        body_dict["origin_ref"] = origin_ref
    _proj_headers = {"X-Project-Id": project_id} if project_id else {}
    payload = json.dumps(body_dict).encode()
    req = urllib.request.Request(
        f"{_API_URL}/conversation/micro-batch",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}",
                 **device_headers(), **_proj_headers},
        method="POST",
    )
    # Retry on 502/503/504 + queue on persistent failure. Same pattern
    # as _post_feedback. Without this, every deploy window dropped a
    # turn-pair from Memory.
    import time as _time
    body_str = payload.decode("utf-8")
    backoff = 0.6
    for attempt in range(3):
        try:
            urllib.request.urlopen(req, timeout=_TIMEOUT)
            return 200
        except urllib.error.HTTPError as e:
            if e.code in (502, 503, 504) and attempt < 2:
                _time.sleep(backoff)
                backoff *= 2
                continue
            if 400 <= e.code < 500:
                sys.stderr.write(
                    f"[stop_hook] micro-batch HTTP {e.code} (dropped, no retry)\n"
                )
                return e.code
            _enqueue_ingest(body_str, f"http_{e.code}")
            sys.stderr.write(
                f"[stop_hook] micro-batch HTTP {e.code} → queued for replay\n"
            )
            return e.code
        except TimeoutError:
            if attempt < 2:
                _time.sleep(backoff)
                backoff *= 2
                continue
            _enqueue_ingest(body_str, "timeout")
            sys.stderr.write(
                f"[stop_hook] micro-batch TIMEOUT → queued for replay\n"
            )
            return 0
        except Exception as exc:
            if attempt < 2:
                _time.sleep(backoff)
                backoff *= 2
                continue
            _enqueue_ingest(body_str, type(exc).__name__)
            sys.stderr.write(
                f"[stop_hook] micro-batch {type(exc).__name__} → queued for replay\n"
            )
            return 0
    return 0


def _enqueue_ingest(body_json: str, reason: str) -> None:
    """Append a failed micro-batch payload to the local queue."""
    try:
        os.makedirs(os.path.dirname(_INGEST_QUEUE), exist_ok=True)
        # Wrap with metadata so the drain can decide if it's still relevant.
        entry = json.dumps({
            "body": body_json,
            "queued_at": __import__("time").time(),
            "reason": reason,
        })
        with open(_INGEST_QUEUE, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except OSError as exc:
        sys.stderr.write(f"[stop_hook] could not enqueue ingest: {exc}\n")


def extract_injected_chunks(user_text: str) -> list[tuple[str, str]]:
    """Legacy fallback: parse `(chunk_id, source_id)` pairs from inline block.

    The user-turn content in the JSONL transcript does NOT contain the
    inject block (Claude Code prefixes hook output to the prompt, but
    only the typed text lands as message.content). The canonical path
    is now ``read_inject_state(session_id)``; this regex is kept as a
    fallback for transcripts that *do* embed the block (e.g. for tests
    or pasted excerpts).
    """
    seen: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for cid, sid in _CHUNK_LINE_RE.findall(user_text or ""):
        if cid in seen:
            continue
        seen.add(cid)
        pairs.append((cid, sid))
    return pairs


def read_inject_state(session_id: str) -> dict:
    """Read inject-state: chunks (with text) + user_prompt.

    Schema (since 2026-05-10):
        {
          "chunks": [{"chunk_id": ..., "source_id": ..., "text": ...?}, ...],
          "user_prompt": "..."
        }

    Legacy state (vor strukturfix): only chunk_id+source_id, no text/prompt
    → caller fällt zurück auf path-match-heuristik.
    """
    if not session_id:
        return {"chunks": [], "user_prompt": ""}
    path = os.path.join(_INJECT_STATE_DIR, f"{session_id}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return {"chunks": [], "user_prompt": ""}
    out_chunks: list[dict] = []
    for entry in data.get("chunks", []):
        cid = (entry or {}).get("chunk_id")
        if not cid:
            continue
        out_chunks.append({
            "chunk_id": cid,
            "source_id": (entry or {}).get("source_id", ""),
            "text": (entry or {}).get("text", ""),
        })
    return {
        "chunks": out_chunks,
        "user_prompt": data.get("user_prompt", "") or "",
    }


def clear_inject_state(session_id: str) -> None:
    if not session_id:
        return
    path = os.path.join(_INJECT_STATE_DIR, f"{session_id}.json")
    try:
        os.remove(path)
    except OSError:
        pass


_JUDGE_ENDPOINT_TIMEOUT = float(os.environ.get("MAYRING_JUDGE_TIMEOUT", "45"))
# WHY(2026-05-28): telemetry label for the queue-routed judge. The /pi/judge-feedback
# endpoint pins this model on its PiJob (src/api/routes/memory.py). Re-added after the
# direct-Ollama→queue refactor (e358319) removed the old constant but left the reference
# at the meta-build site → NameError crashed _auto_feedback whenever the judge returned
# scores → NO feedback was ever posted from the CLI (silent broken loop).
_JUDGE_MODEL = "mistral:7b-instruct"


def _judge_chunks_via_queue(
    chunks: list[dict],
    user_prompt: str,
    assistant_text: str,
    token: str,
    *,
    api_url: str = _API_URL,
    timeout: float = _JUDGE_ENDPOINT_TIMEOUT,
) -> dict[str, str] | None:
    """Rate chunk usage via the cloud /pi/judge-feedback endpoint.

    WHY(2026-05-28): the judge used to POST Ollama DIRECTLY from this hook,
    bypassing the server-side PiQueue → no bounded concurrency, hammered the
    personal GPU on every Stop. Now it routes through the queue (kind='judge',
    no memory aug, PI_CONCURRENCY-bounded). Returns {chunk_id: '1'..'5'} or None
    (→ caller falls back to the path-match heuristic). Fire-and-forget safe.
    """
    chunks_with_text = [c for c in chunks if c.get("text")]
    if not chunks_with_text or not assistant_text:
        return None
    body = json.dumps({
        "user_prompt": (user_prompt or "")[:500],
        "assistant_text": assistant_text[:1500],
        "chunks": [
            {"chunk_id": c["chunk_id"], "text": (c.get("text") or "")[:500]}
            for c in chunks_with_text
        ],
    }).encode()
    req = urllib.request.Request(
        f"{api_url}/pi/judge-feedback",
        data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}", **device_headers()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        scores = data.get("scores") or {}
        return {str(k): str(v) for k, v in scores.items()} or None
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
        sys.stderr.write(
            f"[stop_hook] queue-judge fail ({type(exc).__name__}) — fallback path-match\n"
        )
        return None


def classify_chunk_relevance(source_id: str, assistant_text: str) -> str:
    """LEGACY path-match — fallback when LLM-judge unavailable.

    Returns rating-string oder "skip". WHY rating + skip statt yes/no:
    siehe _judge_chunks_with_llm. Pfad-match ist nur eine grobe heuristik
    weshalb wir die ratings konservativ vergeben:
      "4" — path/basename appears (mid-strong evidence chunk wurde genutzt)
      "2" — substanzielle antwort (>=200 chars) ohne match — wahrscheinlich
            irrelevant aber nicht eindeutig schädlich
      "skip" — unklar (kurze antwort ohne match, leere inputs)
    """
    if not source_id or not assistant_text:
        return "skip"
    path_key = source_id.rsplit(":", 1)[-1]
    if path_key and len(path_key) >= _PATH_KEY_MIN_LEN and path_key in assistant_text:
        return "4"
    basename = path_key.rsplit("/", 1)[-1] if "/" in path_key else path_key
    if basename and len(basename) >= _PATH_KEY_MIN_LEN and basename in assistant_text:
        return "4"
    return "2" if len(assistant_text) >= 200 else "skip"


def _enqueue_feedback(chunk_id: str, signal: str, reason: str) -> None:
    """Append a failed feedback entry to the local queue for later replay.
    session_start.py::_drain_feedback_queue() ships these on next session.

    Schema is the SAME as the queue created by `bin/mayring-feedback` so
    both paths share a single drain implementation:
      {"chunk_id":"...", "signal":"1"|"2"|"3"|"4"|"5", "metadata":{...}}
    """
    try:
        os.makedirs(os.path.dirname(_FEEDBACK_QUEUE), exist_ok=True)
        entry = json.dumps({
            "chunk_id": chunk_id,
            "signal": signal,
            "metadata": {"queued_by": "stop_hook", "reason": reason},
        })
        with open(_FEEDBACK_QUEUE, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except OSError as exc:
        sys.stderr.write(
            f"[stop_hook] could not enqueue feedback {chunk_id}/{signal}: {exc}\n"
        )


def _post_feedback(
    chunk_id: str,
    signal: str,
    token: str,
    metadata: dict | None = None,
) -> None:
    """POST /memory/feedback with retry on 502/503/504 (deploy windows).

    On retry exhaustion or persistent network error: enqueue locally so
    next SessionStart's drain can replay. Only 4xx errors are dropped
    permanently (no retry will fix a malformed payload or unknown
    chunk).

    metadata persistiert task-context (issue #90: feedback-matrix nach
    task) + judging-method, damit reranker-training weiß ob das signal
    aus path-match (rauschig) oder LLM-judge (sauberer) stammt.
    """
    import time as _time
    body: dict = {"chunk_id": chunk_id, "signal": signal}
    if metadata:
        body["metadata"] = metadata
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{_API_URL}/memory/feedback",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}", **device_headers()},
        method="POST",
    )
    backoff = 0.6
    for attempt in range(3):
        try:
            urllib.request.urlopen(req, timeout=_TIMEOUT)
            return
        except urllib.error.HTTPError as e:
            if e.code in (502, 503, 504) and attempt < 2:
                _time.sleep(backoff)
                backoff *= 2
                continue
            if 400 <= e.code < 500:
                # 4xx = chunk gone or invalid signal — replay won't help
                sys.stderr.write(
                    f"[stop_hook] feedback POST {chunk_id}/{signal}: "
                    f"HTTP {e.code} (dropped, no retry)\n"
                )
                return
            # 5xx after retries → enqueue
            _enqueue_feedback(chunk_id, signal, f"http_{e.code}")
            sys.stderr.write(
                f"[stop_hook] feedback POST {chunk_id}/{signal}: "
                f"HTTP {e.code} → queued for replay\n"
            )
            return
        except (urllib.error.URLError, OSError) as e:
            if attempt < 2:
                _time.sleep(backoff)
                backoff *= 2
                continue
            _enqueue_feedback(chunk_id, signal, type(e).__name__)
            sys.stderr.write(
                f"[stop_hook] feedback POST {chunk_id}/{signal}: "
                f"{type(e).__name__} → queued for replay\n"
            )
            return
        except Exception as exc:
            _enqueue_feedback(chunk_id, signal, type(exc).__name__)
            sys.stderr.write(
                f"[stop_hook] feedback POST {chunk_id}/{signal}: "
                f"{type(exc).__name__}: {exc} → queued for replay\n"
            )
            return


def _capture_turns(payload: dict, token: str) -> list[dict]:
    """Best-effort: ingest the last user/assistant turn pair into Memory.

    Returns the extracted turn pair so the auto-feedback step can reuse it
    without re-reading the transcript file.
    """
    transcript_path = payload.get("transcript_path", "")
    session_id = payload.get("session_id", "") or "unknown"
    if not transcript_path:
        return []
    turns = extract_last_turn_pair(transcript_path)
    if len(turns) < 2:
        return turns
    # Write-leak fix: fold the turn's tool actions into the assistant content so
    # the conversation_summary (and the recency-lane) carries the actual WORK,
    # not just prose. Budget so the action block always survives truncation.
    actions = extract_turn_actions(transcript_path)
    if actions and turns[1].get("role") == "assistant":
        # PREPEND (not append): the server summariser truncates each turn to the
        # first ~500 chars (_format_turns). Actions are the substance we must not
        # lose, so they lead; prose is what gets cut if anything.
        action_block = "## Aktionen (Tools)\n" + "\n".join(f"- {a}" for a in actions)
        budget = max(0, _MAX_TURN_CHARS - len(action_block) - 2)
        turns[1]["content"] = action_block + "\n\n" + turns[1].get("content", "")[:budget]
    user_text = turns[0].get("content", "")
    igio_hint = _igio_fast_hint(user_text)
    _proj_id, _origin_ref = _project_stamp()
    # B.2: the native session-/goal (same source as capture_session_goal) becomes the
    # categorize task so the conversation's categories are anchored to the canonical goal.
    goal = latest_session_goal(transcript_path)
    _post_micro_batch(turns, session_id, _workspace_slug(), token, igio_hint=igio_hint,
                      project_id=_proj_id, origin_ref=_origin_ref, task=goal)
    return turns


def _auto_feedback(turns: list[dict], session_id: str, token: str) -> None:
    """Rate every chunk that memory_inject announced for this prompt.

    Source of pairs: the per-session state file written by memory_inject
    (transcript content does NOT contain the inject block). Falls back
    to inline-block parsing of the user turn for back-compat / tests.
    Always clears the state file after rating so a missed Stop event
    doesn't double-rate on the next session.
    """
    if len(turns) < 2:
        return
    state = read_inject_state(session_id)
    chunks = state.get("chunks") or []
    user_prompt_state = state.get("user_prompt", "")
    if not chunks:
        # Fallback for legacy/test paths that do embed the block inline
        user_text = turns[0].get("content", "")
        legacy_pairs = extract_injected_chunks(user_text)
        chunks = [{"chunk_id": c, "source_id": s, "text": ""} for c, s in legacy_pairs]
        user_prompt_state = user_text
    if not chunks:
        return
    chunks = chunks[:_AUTO_FEEDBACK_LIMIT]
    assistant_text = turns[1].get("content", "")
    user_text = user_prompt_state or turns[0].get("content", "")

    # Try LLM-judge first (inhaltlicher signal). Only works wenn jeder
    # chunk text mitgebracht hat (state-format ≥2026-05-10) UND ollama
    # erreichbar ist. Fallback ist die path-match-heuristik.
    judged = _judge_chunks_via_queue(chunks, user_text, assistant_text, token)

    posted = 0
    skipped = 0
    method = "llm_judge" if judged else "path_match"
    judge_model = _JUDGE_MODEL if judged else None
    task_short = (user_text or "")[:200].replace("\n", " ")

    for entry in chunks:
        cid = entry.get("chunk_id")
        sid = entry.get("source_id", "")
        if not cid:
            continue
        if judged is not None:
            signal = judged.get(cid)
            if signal not in ("1", "2", "3", "4", "5"):
                skipped += 1
                continue
        else:
            signal = classify_chunk_relevance(sid, assistant_text)
            if signal == "skip":
                skipped += 1
                continue
        meta = {"task": task_short, "method": method, "source_id": sid[:200]}
        if judge_model:
            meta["judge_model"] = judge_model
        _post_feedback(cid, signal, token, metadata=meta)
        posted += 1

    sys.stderr.write(
        f"[stop_hook] auto_feedback: posted {posted}, skipped {skipped} "
        f"(method={method})\n"
    )
    clear_inject_state(session_id)


def latest_session_goal(transcript_path: str) -> str:
    """Claudes natives /goal aus dem Transcript: die letzte ``goal_status``-Zeile
    (strukturiertes Feld ``condition``, KEIN Prosa-Scraping). "" wenn kein Goal aktiv."""
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    goal = ""
    try:
        with open(transcript_path, encoding="utf-8") as f:
            for line in f:
                if '"goal_status"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if row.get("type") == "goal_status":
                    cond = (row.get("condition") or "").strip()
                    if cond:
                        goal = cond  # keep the latest
    except OSError:
        return ""
    return goal


def _read_goal_state() -> dict:
    try:
        with open(_SESSION_GOAL_STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_goal_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_SESSION_GOAL_STATE), exist_ok=True)
        with open(_SESSION_GOAL_STATE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


def capture_session_goal(payload: dict, token: str) -> None:
    """Erfasst Claudes natives Session-`/goal` → Mayring goal-Achse (igio_axis='goal').

    Das native Goal ist kein Hook-Feld; es liegt als strukturierte ``goal_status``-Zeile
    im Transcript. Wir ingesten die aktuelle condition als goal-Chunk via /memory/put —
    ``igio_hint='goal'`` setzt die Achse direkt (bypassed Classifier+SKIP-Gate) und nutzt
    /memory/goals + IGIO-Lens + Injection unverändert wieder. Idempotent: source_id =
    hash(goal); per-session-Marker verhindert Re-POST eines unveränderten Goals.

    Läuft über den geteilten put_memory-Pfad (gleicher robuste /memory/put wie der
    Recap). enqueue=False: der per-turn-Marker IST der Retry-Mechanismus des Goals
    (advance nur bei 200) — Queueing würde beim nächsten Turn doppelt senden."""
    import hashlib

    transcript_path = payload.get("transcript_path", "")
    session_id = payload.get("session_id", "") or "unknown"
    goal = latest_session_goal(transcript_path)
    if not goal:
        return
    ghash = hashlib.sha256(goal.encode("utf-8")).hexdigest()[:16]
    state = _read_goal_state()
    if state.get(session_id) == ghash:
        return  # unverändert diese Session

    status = put_memory(goal, f"session_goal:{ghash}", "session_goal", token,
                        igio_hint="goal", categorize=False, enqueue=False)
    if status == 200:
        state[session_id] = ghash          # nur bei Erfolg → Retry-next-turn
        _write_goal_state(state)
        sys.stderr.write(f"[stop_hook] session-goal → goal axis: {goal[:60]!r}\n")
    else:
        sys.stderr.write(f"[stop_hook] session-goal POST status={status} (retry next turn)\n")


def main() -> None:
    token = _read_token()
    if not token:
        sys.stderr.write(f"[stop_hook] no token at {_JWT_FILE}; skipping\n")
        return
    report_hook_event("Stop", token)  # best-effort device telemetry (#5)
    payload = _read_payload()
    session_id = payload.get("session_id", "") or "unknown"
    try:
        turns = _capture_turns(payload, token)
    except Exception as exc:
        sys.stderr.write(f"[stop_hook] capture_turns crashed: {type(exc).__name__}: {exc}\n")
        turns = []
    try:
        _auto_feedback(turns, session_id, token)
    except Exception as exc:
        sys.stderr.write(f"[stop_hook] auto_feedback crashed: {type(exc).__name__}: {exc}\n")
    try:
        capture_session_goal(payload, token)
    except Exception as exc:
        sys.stderr.write(f"[stop_hook] capture_session_goal crashed: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
