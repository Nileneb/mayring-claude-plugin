"""Drain honors the per-entry endpoint (legacy entries → micro-batch)."""
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_mod_path = Path(__file__).parent / "session_start.py"
sys.path.insert(0, str(_mod_path.parent))
spec = importlib.util.spec_from_file_location("session_start", _mod_path)
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)


def _run_drain(tmp_path, entries):
    queue = tmp_path / "ingest.jsonl"
    queue.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    jwt = tmp_path / "hook.jwt"
    jwt.write_text("tok", encoding="utf-8")
    urls = []

    def _fake_urlopen(req, timeout=None):
        urls.append(req.full_url)
        return MagicMock(__enter__=lambda s: s, __exit__=lambda *a: False)

    with patch.object(ss, "INGEST_QUEUE", str(queue)), \
         patch.object(ss, "JWT_FILE", str(jwt)), \
         patch.object(ss, "MAYRING_API", "https://api.test"), \
         patch.object(ss.urllib.request, "urlopen", side_effect=_fake_urlopen):
        ss._drain_ingest_queue()
    return urls


def test_memory_put_entry_replays_to_memory_put(tmp_path):
    urls = _run_drain(tmp_path, [
        {"endpoint": "/memory/put", "body": json.dumps({"content": "x"})},
    ])
    assert urls == ["https://api.test/memory/put"]


def test_legacy_entry_without_endpoint_defaults_to_micro_batch(tmp_path):
    urls = _run_drain(tmp_path, [
        {"body": json.dumps({"turns": []})},  # no endpoint key (pre-migration)
    ])
    assert urls == ["https://api.test/conversation/micro-batch"]


def test_mixed_queue_routes_each_to_its_endpoint(tmp_path):
    urls = _run_drain(tmp_path, [
        {"body": json.dumps({"turns": []})},
        {"endpoint": "/memory/put", "body": json.dumps({"content": "recap"})},
    ])
    assert urls == [
        "https://api.test/conversation/micro-batch",
        "https://api.test/memory/put",
    ]
