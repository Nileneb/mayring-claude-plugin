"""Unit tests for postcompact_hook — compact summary ingested into Memory."""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

_mod_path = Path(__file__).parent / "postcompact_hook.py"
sys.path.insert(0, str(_mod_path.parent))
spec = importlib.util.spec_from_file_location("postcompact_hook", _mod_path)
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)


def test_recap_ingested_with_categorize():
    posted = {}

    def _fake_put(content, source_id, source_type, token, *,
                  categorize=True, **_k):
        posted.update(content=content, source_id=source_id,
                      source_type=source_type, categorize=categorize)
        return 200

    with patch.object(pc, "put_memory", side_effect=_fake_put):
        rc = pc.ingest_recap("session did X and Y", "tok")

    assert rc == 200
    assert "igio_hint" not in posted
    assert posted["source_type"] == "conversation_summary"
    assert posted["source_id"].startswith("conversation_summary:compact-")
    assert posted["categorize"] is True
    assert posted["content"] == "session did X and Y"


def test_empty_recap_no_post():
    calls = []
    with patch.object(pc, "put_memory", side_effect=lambda *a, **k: calls.append(1)):
        assert pc.ingest_recap("   ", "tok") == 0
        assert pc.ingest_recap("real", "") == 0
    assert not calls


def test_source_id_stable_for_same_summary_and_time():
    import datetime
    posted = []
    fixed = datetime.datetime(2026, 5, 29, 12, 0, 0, tzinfo=pc._TZ)
    with patch.object(pc, "put_memory",
                      side_effect=lambda c, sid, *a, **k: posted.append(sid) or 200):
        pc.ingest_recap("same text", "tok", now=fixed)
        pc.ingest_recap("same text", "tok", now=fixed)
    assert posted[0] == posted[1]  # deterministic id (ts + content hash)
