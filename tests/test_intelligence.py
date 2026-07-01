import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from intelligence import compute_intelligence_summary, _fetch_eval_bars, log_decision_batch


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


# ── Regression: options symbols must NOT trigger a bar fetch against Alpaca ──


def test_fetch_eval_bars_skips_options_asset_type():
    """asset_type='option' must return [] without calling get_bars."""
    with patch("intelligence.get_bars") as mock_get_bars:
        result = _fetch_eval_bars("NVDA260608C00150000", "option")
    assert result == []
    mock_get_bars.assert_not_called()


def test_fetch_eval_bars_skips_occ_symbol_with_equity_asset_type():
    """Legacy records stored with asset_type='equity' but a real OCC symbol must also
    return [] (guards against the 400 Bad Request regression on NVDA_OPTIONSlike symbols)."""
    with patch("intelligence.get_bars") as mock_get_bars:
        result = _fetch_eval_bars("NVDA260608C00150000", "equity")
    assert result == []
    mock_get_bars.assert_not_called()


def test_fetch_eval_bars_skips_placeholder_options_symbol():
    """_OPTIONS placeholder symbols (emitted by model before OCC chain was wired)
    must also be silently skipped — not forwarded to Alpaca bars API."""
    with patch("intelligence.get_bars") as mock_get_bars:
        result = _fetch_eval_bars("NVDA_OPTIONS", "equity")
    assert result == []
    mock_get_bars.assert_not_called()


def test_log_decision_batch_classifies_occ_as_option(tmp_path, monkeypatch):
    """log_decision_batch must store asset_type='option' for OCC symbols,
    so they never reach the equity bar-fetch path in refresh_intelligence."""
    import intelligence as intel_mod
    monkeypatch.setattr(intel_mod, "DECISION_LOG", tmp_path / "decision_log.jsonl")
    monkeypatch.setattr(intel_mod, "OUTCOME_DB", tmp_path / "decision_outcomes.json")
    monkeypatch.setattr(intel_mod, "SUMMARY_CACHE", tmp_path / "intelligence_summary.json")
    monkeypatch.setattr(intel_mod, "META_FILE", tmp_path / "intelligence_meta.json")

    payload = {
        "orders": [
            {
                "symbol": "NVDA260608C00150000",
                "side": "buy",
                "setup_type": "options_directional",
                "limit_price": 3.50,
            }
        ]
    }
    records = log_decision_batch("trading", payload)
    assert len(records) == 1
    assert records[0]["asset_type"] == "option"


def test_log_decision_batch_classifies_placeholder_options_as_option(tmp_path, monkeypatch):
    """_OPTIONS placeholder symbols are also classified as asset_type='option'."""
    import intelligence as intel_mod
    monkeypatch.setattr(intel_mod, "DECISION_LOG", tmp_path / "decision_log.jsonl")
    monkeypatch.setattr(intel_mod, "OUTCOME_DB", tmp_path / "decision_outcomes.json")
    monkeypatch.setattr(intel_mod, "SUMMARY_CACHE", tmp_path / "intelligence_summary.json")
    monkeypatch.setattr(intel_mod, "META_FILE", tmp_path / "intelligence_meta.json")

    payload = {
        "orders": [
            {
                "symbol": "NVDA_OPTIONS",
                "side": "buy",
                "setup_type": "options_directional",
                "limit_price": 3.50,
            }
        ]
    }
    records = log_decision_batch("trading", payload)
    assert len(records) == 1
    assert records[0]["asset_type"] == "option"
