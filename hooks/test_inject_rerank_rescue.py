"""Out-of-context rescue: the inject gate must honor the reranker's score_final,
not just the raw cosine. A chunk the reranker elevated (feedback/recency) must
survive even when its raw vector similarity is below the cosine floor.
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

_mod_path = Path(__file__).parent / "memory_inject.py"
sys.path.insert(0, str(_mod_path.parent))
spec = importlib.util.spec_from_file_location("memory_inject", _mod_path)
mi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mi)


def _lens(max_sim=0.0, score_finals=()):
    return {
        "diagnostics": {"vector_stage": f"ok(max_score={max_sim:.3f},matches=3)"},
        "results": [{"score_final": s} for s in score_finals],
    }


def test_strong_cosine_not_weak():
    assert mi._lens_weak(_lens(max_sim=0.55, score_finals=(0.6,))) is False


def test_weak_cosine_and_weak_rerank_is_weak():
    # below both floors → genuinely no context
    assert mi._lens_weak(_lens(max_sim=0.30, score_finals=(0.20, 0.10))) is True


def test_weak_cosine_but_strong_rerank_rescued():
    # THE fix: cosine 0.30 < 0.4, but reranker lifted it to 0.62 (feedback+recency)
    # → must NOT be considered weak (chunk survives the gate)
    with patch.object(mi, "_MIN_RERANK_SCORE", 0.45):
        assert mi._lens_weak(_lens(max_sim=0.30, score_finals=(0.62,))) is False


def test_rerank_just_below_floor_stays_weak():
    with patch.object(mi, "_MIN_RERANK_SCORE", 0.45):
        assert mi._lens_weak(_lens(max_sim=0.30, score_finals=(0.44,))) is True


def test_max_rerank_handles_missing_and_malformed():
    assert mi._max_rerank({}) == 0.0
    assert mi._max_rerank({"results": [{"score_final": None}, {"score_final": "x"}]}) == 0.0
    assert mi._max_rerank({"results": [{"score_final": 0.3}, {"score_final": 0.7}]}) == 0.7


def test_max_sim_parses_diagnostic():
    assert mi._max_sim(_lens(max_sim=0.512)) == 0.512
    assert mi._max_sim({}) == 0.0


def test_gate_additive_never_worse_than_cosine_only():
    """Any lens that passed the OLD cosine-only gate must still pass."""
    strong_cosine = _lens(max_sim=0.50, score_finals=())  # no results, but cosine strong
    assert mi._lens_weak(strong_cosine) is False  # old gate would inject → still injects
