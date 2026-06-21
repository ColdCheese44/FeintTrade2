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


def test_small_win_classified_as_win_not_breakeven(monkeypatch, tmp_path):
    """pnl_pct = +0.15% is above the new 0.05% threshold — must be 'win', not 'breakeven'."""
    import json
    open_trades_file = tmp_path / "open_trades.json"
    trade_log_file = tmp_path / "trade_log.jsonl"
    trade_log_file.write_text("")
    open_trades_file.write_text(json.dumps({
        "TSLA": {
            "trade_id": "test_001", "symbol": "TSLA", "side": "buy", "qty": 10.0,
            "entry_price": 200.00, "setup_type": "test", "conviction": 7,
            "signals": {}, "market_regime": "BULL", "vix": 15,
            "asset_type": "equity", "time_of_day": "open", "day_of_week": "Friday",
            "timestamp_entry": "2026-06-20T10:00:00",
        }
    }))
    monkeypatch.setattr(learning, "OPEN_TRADES", open_trades_file)
    monkeypatch.setattr(learning, "TRADE_LOG", trade_log_file)
    monkeypatch.setattr(learning, "PERF_CACHE", tmp_path / "performance.json")
    # +0.15% exit (200.30 vs 200.00 entry) — above 0.05% threshold
    result = learning.log_exit("TSLA", exit_price=200.30, exit_reason="target_hit", qty=10.0)
    assert result is not None
    logged = learning._load_trade_log()
    assert len(logged) == 1
    assert logged[0]["outcome"] == "win", f"Expected 'win' for +0.15%, got {logged[0]['outcome']}"


def test_small_loss_classified_as_loss_not_breakeven(monkeypatch, tmp_path):
    """pnl_pct = -0.21% is below the -0.05% threshold — must be 'loss', not 'breakeven'."""
    import json
    open_trades_file = tmp_path / "open_trades.json"
    trade_log_file = tmp_path / "trade_log.jsonl"
    trade_log_file.write_text("")
    open_trades_file.write_text(json.dumps({
        "TSLA": {
            "trade_id": "test_002", "symbol": "TSLA", "side": "buy", "qty": 10.0,
            "entry_price": 200.00, "setup_type": "test", "conviction": 7,
            "signals": {}, "market_regime": "BULL", "vix": 15,
            "asset_type": "equity", "time_of_day": "open", "day_of_week": "Friday",
            "timestamp_entry": "2026-06-20T10:00:00",
        }
    }))
    monkeypatch.setattr(learning, "OPEN_TRADES", open_trades_file)
    monkeypatch.setattr(learning, "TRADE_LOG", trade_log_file)
    monkeypatch.setattr(learning, "PERF_CACHE", tmp_path / "performance.json")
    # -0.21% exit (199.58 vs 200.00 entry)
    result = learning.log_exit("TSLA", exit_price=199.58, exit_reason="stop_loss", qty=10.0)
    assert result is not None
    logged = learning._load_trade_log()
    assert len(logged) == 1
    assert logged[0]["outcome"] == "loss", f"Expected 'loss' for -0.21%, got {logged[0]['outcome']}"


def test_best_symbol_requires_4_trades_and_positive_pnl(monkeypatch):
    """BEST SYMBOL should not fire when sample < 4 trades, even with positive P&L."""
    trades = [_trade("bb_squeeze_breakout", 100, "win", symbol="SYM1") for _ in range(3)]
    monkeypatch.setattr(learning, "_load_trade_log", lambda: trades)
    out = learning.get_strategy_recommendations()
    assert "📈 BEST SYMBOL" not in out


def test_best_symbol_suppressed_on_negative_pnl(monkeypatch):
    """BEST SYMBOL must not fire even with 4+ trades if the symbol's net P&L is negative."""
    trades = ([_trade("bb_squeeze_breakout", -200, "loss", symbol="SYM1") for _ in range(3)]
              + [_trade("bb_squeeze_breakout", 50, "win", symbol="SYM1")])
    monkeypatch.setattr(learning, "_load_trade_log", lambda: trades)
    out = learning.get_strategy_recommendations()
    assert "📈 BEST SYMBOL: SYM1" not in out


def _trade_tod(tod, pnl, outcome):
    pct = 5.0 if outcome == "win" else -3.0
    return {"setup_type": "bb_squeeze_breakout", "symbol": "X",
            "pnl_dollar": pnl, "pnl_pct": pct, "outcome": outcome, "time_of_day": tod}


def test_best_tod_suppressed_when_negative_pnl(monkeypatch):
    """BEST TIME must not fire when the bucket has positive WR but negative total P&L."""
    # 'open' window: 3 trades, 66.7% WR but net -$100 (one -$250 loss, two +$75 wins)
    trades = ([_trade_tod("open", -250, "loss")]
              + [_trade_tod("open", 75, "win") for _ in range(2)])
    monkeypatch.setattr(learning, "_load_trade_log", lambda: trades)
    out = learning.get_strategy_recommendations()
    assert "⏰ BEST TIME: 'open'" not in out


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
