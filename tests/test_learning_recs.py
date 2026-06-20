"""
Data-driven strategy recommendations — FeintTrade.
Run: python -m pytest tests/test_learning_recs.py -v

get_strategy_recommendations() escalates the worst-setup signal from advisory
("REDUCE size or skip") to a hard "STOP SETUP" when a setup is the dominant, repeated
loss source (>=5 trades, <40% WR, <= -$1k). Self-updating from the trade log — no
hardcoded setup names. Hermetic: the trade log is stubbed.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import learning


def _trade(setup, pnl_dollar, outcome, symbol="X"):
    pct = 5.0 if outcome == "win" else (-3.0 if outcome == "loss" else 0.1)
    return {"setup_type": setup, "symbol": symbol, "pnl_dollar": pnl_dollar,
            "pnl_pct": pct, "outcome": outcome, "time_of_day": "open"}


def test_severe_setup_escalates_to_stop(monkeypatch):
    # momentum_breakout: 6 losses (-$1,800, 0% WR) -> severe -> STOP. bb_squeeze: 4 wins.
    trades = ([_trade("momentum_breakout", -300, "loss") for _ in range(6)]
              + [_trade("bb_squeeze_breakout", 100, "win") for _ in range(4)])
    monkeypatch.setattr(learning, "_load_trade_log", lambda: trades)
    out = learning.get_strategy_recommendations()
    assert "🛑 STOP SETUP: 'momentum_breakout'" in out
    assert "Do NOT open new trades with this setup" in out
    assert "bb_squeeze_breakout" in out               # names the proven setup to use instead
    assert "📉 WORST SETUP" not in out                 # escalated, not the advisory form


def test_mild_loss_stays_advisory(monkeypatch):
    # 3 trades, -$150 total -> below the severe bar -> advisory WORST SETUP, not STOP.
    trades = ([_trade("vwap_bounce", -50, "loss") for _ in range(3)]
              + [_trade("bb_squeeze_breakout", 100, "win") for _ in range(3)])
    monkeypatch.setattr(learning, "_load_trade_log", lambda: trades)
    out = learning.get_strategy_recommendations()
    assert "🛑 STOP SETUP" not in out
    assert "📉 WORST SETUP: 'vwap_bounce'" in out


def test_recommendations_utf8_safe(monkeypatch):
    # Regression: output must encode to UTF-8 without error (Windows cp1252 guard).
    trades = [_trade("momentum_breakout", -300, "loss") for _ in range(6)]
    monkeypatch.setattr(learning, "_load_trade_log", lambda: trades)
    out = learning.get_strategy_recommendations()
    assert isinstance(out, str)
    out.encode('utf-8')  # must not raise


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
