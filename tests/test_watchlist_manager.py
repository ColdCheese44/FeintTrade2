"""
Tests for the auto-updating watchlist (watchlist_manager). Hermetic: state file in
tmp_path, config patched, discovery + 'today' passed in (never touches screener/network).

Run: python -B -m pytest tests/test_watchlist_manager.py -q
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import watchlist_manager as wm


@pytest.fixture
def wlm(monkeypatch, tmp_path):
    monkeypatch.setattr(wm, "STATE", tmp_path / "dyn.json")
    cfg = {"enabled": True, "promote_min_appearances": 3, "promote_min_score": 4,
           "demote_after_days": 4, "max_active": 3, "tracker_window_days": 14}
    monkeypatch.setattr(wm, "_cfg", lambda: cfg)
    return cfg


def _disc(*syms_scores, **extra):
    return {"candidates": [{"symbol": s, "score": sc, "type": "equity", **extra}
                           for s, sc in syms_scores]}


def test_promotes_after_min_appearances(wlm):
    for day in ("2026-06-10", "2026-06-11"):
        ch = wm.update(_disc(("AAPL", 6)), today=day)
        assert "AAPL" not in ch["active"]          # not yet (needs 3 appearances)
    ch = wm.update(_disc(("AAPL", 6)), today="2026-06-12")
    assert "AAPL" in ch["promoted"] and "AAPL" in ch["active"]


def test_low_score_never_promoted(wlm):
    ch = {}
    for day in ("2026-06-10", "2026-06-11", "2026-06-12", "2026-06-13"):
        ch = wm.update(_disc(("LOW", 2)), today=day)   # score 2 < min 4
    assert "LOW" not in ch["active"]


def test_penny_caution_never_promoted(wlm):
    ch = {}
    for day in ("2026-06-10", "2026-06-11", "2026-06-12", "2026-06-13"):
        ch = wm.update(_disc(("PUMP", 9), penny_caution=True), today=day)
    assert "PUMP" not in ch["active"]


def test_demotes_when_stale(wlm):
    for day in ("2026-06-10", "2026-06-11", "2026-06-12"):
        wm.update(_disc(("NVDA", 7)), today=day)
    assert "NVDA" in wm.active_symbols()
    # 5 days later NVDA hasn't appeared (cutoff = 06-13) -> demoted
    ch = wm.update(_disc(("AMD", 7)), today="2026-06-17")
    assert "NVDA" in ch["demoted"] and "NVDA" not in ch["active"]


def test_cap_enforced_keeps_highest_score(wlm):
    for day in ("2026-06-10", "2026-06-11", "2026-06-12"):
        ch = wm.update(_disc(("A", 9), ("B", 8), ("C", 7), ("D", 6)), today=day)
    assert len(ch["active"]) == 3            # max_active
    assert "D" not in ch["active"]           # lowest score dropped by the cap
    assert {"A", "B", "C"} == set(ch["active"])


def test_brief_reflects_active(wlm):
    for day in ("2026-06-10", "2026-06-11", "2026-06-12"):
        wm.update(_disc(("TSLA", 8)), today=day)
    b = wm.brief()
    assert "TSLA" in b and "AUTO-WATCHLIST" in b


def test_disabled_is_noop(wlm, monkeypatch):
    monkeypatch.setattr(wm, "_cfg", lambda: {**wlm, "enabled": False})
    ch = wm.update(_disc(("X", 9)), today="2026-06-10")
    assert ch["promoted"] == [] and ch["active"] == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
