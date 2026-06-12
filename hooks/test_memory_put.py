"""Unit tests for the shared robust /memory/put path (_memory_put.py).

Covers the consolidation contract: 5xx retry then queue-with-endpoint,
401→refresh→retry, 4xx drop, device headers.
"""
import importlib.util
import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

_mod_path = Path(__file__).parent / "_memory_put.py"
sys.path.insert(0, str(_mod_path.parent))
spec = importlib.util.spec_from_file_location("_memory_put", _mod_path)
mp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mp)


def _http_error(code):
    return urllib.error.HTTPError("http://x/memory/put", code, "err", {}, None)


def test_success_returns_200_with_correct_body():
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        captured["auth"] = req.headers.get("Authorization")
        return MagicMock()

    with patch.object(mp, "device_headers", lambda: {"X-Device-Id": "dev1"}), \
         patch.object(mp.urllib.request, "urlopen", side_effect=_fake_urlopen):
        rc = mp.put_memory("the recap", "conversation_summary:c1",
                           "conversation_summary", "tok",
                           categorize=True)

    assert rc == 200
    assert captured["url"].endswith("/memory/put")
    assert "igio_hint" not in captured["body"]
    assert captured["body"]["content"] == "the recap"
    assert captured["body"]["categorize"] is True
    assert captured["auth"] == "Bearer tok"


def test_empty_content_or_token_short_circuits():
    with patch.object(mp.urllib.request, "urlopen", side_effect=AssertionError("should not POST")):
        assert mp.put_memory("   ", "s", "t", "tok") == 0
        assert mp.put_memory("real", "s", "t", "") == 0


def test_4xx_dropped_no_queue(tmp_path):
    queue = tmp_path / "ingest.jsonl"
    with patch.object(mp, "_INGEST_QUEUE", str(queue)), \
         patch.object(mp, "device_headers", lambda: {}), \
         patch.object(mp.urllib.request, "urlopen", side_effect=_http_error(422)):
        rc = mp.put_memory("x", "s", "t", "tok")
    assert rc == 422
    assert not queue.exists()  # 4xx → no retry, no queue


def test_5xx_retries_then_queues_with_endpoint(tmp_path):
    queue = tmp_path / "ingest.jsonl"
    attempts = []

    def _always_503(req, timeout=None):
        attempts.append(1)
        raise _http_error(503)

    with patch.object(mp, "_INGEST_QUEUE", str(queue)), \
         patch.object(mp, "device_headers", lambda: {}), \
         patch.object(mp.time, "sleep", lambda *_: None), \
         patch.object(mp.urllib.request, "urlopen", side_effect=_always_503):
        rc = mp.put_memory("x", "sid", "stype", "tok")

    assert rc == 0
    assert len(attempts) == 3  # retried twice
    entry = json.loads(queue.read_text().strip())
    assert entry["endpoint"] == "/memory/put"   # so the drain hits the right route
    body = json.loads(entry["body"])
    assert body["source_id"] == "sid"
    assert "igio_hint" not in body


def test_5xx_with_enqueue_false_does_not_queue(tmp_path):
    queue = tmp_path / "ingest.jsonl"
    with patch.object(mp, "_INGEST_QUEUE", str(queue)), \
         patch.object(mp, "device_headers", lambda: {}), \
         patch.object(mp.time, "sleep", lambda *_: None), \
         patch.object(mp.urllib.request, "urlopen", side_effect=_http_error(503)):
        rc = mp.put_memory("x", "s", "t", "tok", enqueue=False)
    assert rc == 0
    assert not queue.exists()


def test_401_refreshes_token_then_retries():
    seen_tokens = []

    def _fake_urlopen(req, timeout=None):
        tok = req.headers.get("Authorization")
        seen_tokens.append(tok)
        if tok == "Bearer old":
            raise _http_error(401)
        return MagicMock()

    with patch.object(mp, "device_headers", lambda: {}), \
         patch.object(mp, "refresh_token", lambda old, **k: "fresh"), \
         patch.object(mp.urllib.request, "urlopen", side_effect=_fake_urlopen):
        rc = mp.put_memory("x", "s", "t", "old")

    assert rc == 200
    assert seen_tokens == ["Bearer old", "Bearer fresh"]


def test_401_refresh_fails_returns_401():
    with patch.object(mp, "device_headers", lambda: {}), \
         patch.object(mp, "refresh_token", lambda old, **k: ""), \
         patch.object(mp.urllib.request, "urlopen", side_effect=_http_error(401)):
        rc = mp.put_memory("x", "s", "t", "old")
    assert rc == 401
