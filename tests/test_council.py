"""
Tests for council mode (multi-agent second opinion). Hermetic: the analyst LLM call is
injected via ask_fn; synthesis is pure. No network.

Run: python -B -m pytest tests/test_council.py -q
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import council


def test_parse_score():
    assert council._parse_score("Strong. Score: 8/10") == 8
    assert council._parse_score("I rate this 9/10 overall") == 9
    assert council._parse_score("Score: 15") == 10          # clamped to 10
    assert council._parse_score("no number") is None


def test_synthesize_buy():
    s = council.synthesize({"technical": {"score": 8}, "catalyst": {"score": 7}, "risk": {"score": 6}})
    assert s["recommendation"] == "BUY" and s["avg_score"] == 7.5


def test_synthesize_risk_veto():
    s = council.synthesize({"technical": {"score": 9}, "catalyst": {"score": 9}, "risk": {"score": 2}})
    assert s["recommendation"] == "SKIP" and "veto" in s["rationale"].lower()


def test_synthesize_watch():
    s = council.synthesize({"technical": {"score": 5}, "catalyst": {"score": 6}, "risk": {"score": 7}})
    assert s["recommendation"] == "WATCH"


def test_convene_with_mock_ask():
    answers = {"technical": "Score: 8/10", "catalyst": "Score: 7/10", "risk": "Score: 6/10"}
    v = council.convene("NVDA", "ctx", ask_fn=lambda role, system, prompt: answers[role])
    assert v["symbol"] == "NVDA"
    assert v["analysts"]["technical"]["score"] == 8
    assert v["synthesis"]["recommendation"] == "BUY"


def test_convene_handles_analyst_failure():
    def bad(role, system, prompt):
        raise RuntimeError("api down")
    v = council.convene("X", ask_fn=bad)
    assert all(a["score"] is None for a in v["analysts"].values())
    assert v["synthesis"]["recommendation"] == "SKIP"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
