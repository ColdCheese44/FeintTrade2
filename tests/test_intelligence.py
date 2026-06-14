import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from intelligence import compute_intelligence_summary


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def test_summary_flags_missed_skip_and_blocker():
    records = [
        {
            "decision_id": "skip1",
            "timestamp": _now_iso(),
            "symbol": "BTC/USD",
            "asset_type": "crypto",
            "action": "skip",
            "setup_type": "crypto_watch",
            "decision_price": 100.0,
            "reasoning": "Below VWAP and no catalyst.",
            "blockers": ["below_vwap", "no_catalyst"],
        }
    ]
    outcomes = {
        "skip1": {
            "24h": {
                "return_pct": 4.2,
                "max_up_pct": 6.5,
                "max_down_pct": -1.1,
            }
        }
    }

    summary = compute_intelligence_summary(records=records, outcomes=outcomes, lookback_days=365)

    assert summary["evaluated_candidates"] == 1
    assert summary["missed_opportunities"][0]["symbol"] == "BTC/USD"
    assert summary["blockers_on_missed_winners"][0]["blocker"] == "below_vwap"


def test_summary_flags_false_positive_buy():
    records = [
        {
            "decision_id": "buy1",
            "timestamp": _now_iso(),
            "symbol": "TQQQ",
            "asset_type": "equity",
            "action": "buy",
            "setup_type": "gap_and_go",
            "decision_price": 50.0,
            "reasoning": "Momentum breakout.",
            "blockers": [],
        }
    ]
    outcomes = {
        "buy1": {
            "1d": {
                "return_pct": -3.6,
                "max_up_pct": 0.4,
                "max_down_pct": -4.1,
            }
        }
    }

    summary = compute_intelligence_summary(records=records, outcomes=outcomes, lookback_days=365)

    assert summary["evaluated_candidates"] == 1
    assert summary["bad_buy_candidates"][0]["symbol"] == "TQQQ"
    assert summary["by_action"]["buy"]["avg_primary_return_pct"] == -3.6
