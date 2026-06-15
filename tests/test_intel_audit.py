"""
Tests for the decision-intelligence audit (blocker predictiveness + report). Hermetic:
log records + outcomes are passed in; no data files or network.

Run: python -B -m pytest tests/test_intel_audit.py -q
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import intel_audit as ia


def test_blocker_predictiveness_verdicts():
    log = (
        [{"action": "skip", "decision_id": f"d{i}", "blockers": ["price_below_intraday_vwap_5.7"]}
         for i in range(20)]
        + [{"action": "watch", "decision_id": f"w{i}", "blockers": ["bb_squeeze_released_bearish"]}
           for i in range(20)]
        + [{"action": "buy", "decision_id": "b1", "blockers": ["price_below_vwap"]}]   # not skip → ignored
    )
    outcomes = {}
    for i in range(20):
        outcomes[f"d{i}"] = {"24h": {"return_pct": -3.0, "max_up_pct": 1.0}}   # blocked losers
    for i in range(20):
        outcomes[f"w{i}"] = {"24h": {"return_pct": +2.0, "max_up_pct": 6.0}}   # blocked winners
    outcomes["b1"] = {"24h": {"return_pct": 5.0}}

    rows = ia.blocker_predictiveness(log_records=log, outcomes=outcomes, min_count=10)
    by = {r["blocker"]: r for r in rows}
    assert by["price_below_vwap"]["count"] == 20
    assert by["price_below_vwap"]["verdict"].startswith("🟢")     # protective (-3%)
    assert by["squeeze_bearish"]["verdict"].startswith("🔴")      # over-restrictive (+2%)
    assert "b1" not in {r.get("decision_id") for r in rows}        # buy excluded


def test_min_count_filter_excludes_rare_blockers():
    log = [{"action": "skip", "decision_id": "d1", "blockers": ["rsi_overbought"]}]
    outcomes = {"d1": {"24h": {"return_pct": -1.0, "max_up_pct": 0}}}
    assert ia.blocker_predictiveness(log_records=log, outcomes=outcomes, min_count=10) == []


def test_format_report_renders():
    a = {
        "lookback_days": 30, "total_candidates": 100, "evaluated": 50,
        "by_action": [("skip", {"count": 10, "avg_primary_return_pct": -2.0}),
                      ("buy", {"count": 5, "avg_primary_return_pct": -5.0})],
        "by_setup": [("crypto_scored", {"count": 20, "avg_primary_return_pct": -3.0})],
        "bad_buys": [], "missed": [], "blockers_on_winners": [],
        "blocker_predictiveness": [{"blocker": "squeeze_bearish", "count": 20, "avg_return_pct": 1.4,
                                    "avg_max_up_pct": 5.7, "verdict": "🔴 over-restrictive"}],
        "brief_lines": ["header"],
    }
    md = ia.format_report(a)
    assert "Decision-Intelligence Audit" in md
    assert "skip" in md and "over-restrictive" in md


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
