"""
Tests for the Strategy Lab what-if simulator. Hermetic: synthetic rows / records +
outcomes; no data files or network.

Run: python -B -m pytest tests/test_strategy_lab.py -q
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import strategy_lab as sl


def _row(action="buy", setup="x", conv=5, signals=4, ret=0.0):
    return {"action": action, "setup": setup, "conv": conv, "signals": signals,
            "asset_type": "equity", "blockers": [], "ret": ret}


def test_threshold_sweep():
    rows = [_row(conv=3, ret=-5), _row(conv=8, ret=4), _row(conv=9, ret=6)]
    by = {r["cutoff"]: r for r in sl.threshold_sweep(rows, "conv", "buy")}
    assert by[1]["n"] == 3 and abs(by[1]["avg_ret"] - (5 / 3)) < 0.02
    assert by[8]["n"] == 2 and by[8]["avg_ret"] == 5.0


def test_setup_edge_verdicts():
    rows = [_row(setup="good", ret=2) for _ in range(5)] + [_row(setup="bad", ret=-6) for _ in range(5)]
    se = {r["setup"]: r for r in sl.setup_edge(rows, min_n=4)}
    assert se["good"]["verdict"].startswith("🟢") and se["good"]["avg_ret"] == 2.0
    assert se["bad"]["verdict"].startswith("🔴")


def test_best_cutoff_prefers_positive():
    sweep = [{"cutoff": 1, "n": 20, "avg_ret": -3}, {"cutoff": 7, "n": 10, "avg_ret": 1.5}]
    assert sl.best_cutoff(sweep, min_n=8)["cutoff"] == 7


def test_recommendations_flags_conviction():
    rows = ([_row(action="buy", conv=9, ret=3) for _ in range(10)]
            + [_row(action="buy", conv=2, ret=-8) for _ in range(10)])
    assert any("conviction" in r.lower() for r in sl.recommendations(rows))


def test_join_rows_requires_outcome():
    records = [{"decision_id": "d1", "action": "buy", "setup_type": "x", "conviction": 7, "signal_count": 5},
               {"decision_id": "d2", "action": "buy", "setup_type": "x"}]   # no outcome
    rows = sl.join_rows(records=records, outcomes={"d1": {"24h": {"return_pct": 3.0}}})
    assert len(rows) == 1 and rows[0]["ret"] == 3.0 and rows[0]["conv"] == 7


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
