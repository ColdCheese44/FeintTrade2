"""
Tests for replay.py — the benchmark/backtest report (vs buy-and-hold + no-trade).
Hermetic: the trade log and SPY fetch are monkeypatched.

Run: python -m pytest tests/test_replay.py -v
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import replay


def test_max_drawdown():
    # equity 100 → 110 → 105 → 130: worst dip is 110→105 = 4.55%.
    trades = [{"pnl_dollar": 10}, {"pnl_dollar": -5}, {"pnl_dollar": 25}]
    assert replay.max_drawdown(trades, 100) == round(5 / 110 * 100, 2)


def test_benchmark_report(monkeypatch):
    fake = [
        {"outcome": "win", "pnl_pct": 2.0, "pnl_dollar": 200,
         "timestamp_exit": "2026-06-09T10:00:00"},
        {"outcome": "loss", "pnl_pct": -3.0, "pnl_dollar": -300,
         "timestamp_exit": "2026-06-10T10:00:00"},
    ]
    monkeypatch.setattr(replay.learning, "_load_trade_log", lambda: fake)
    monkeypatch.setattr(replay, "spy_buy_and_hold_pct", lambda s, e: 0.5)
    r = replay.benchmark_report(starting_capital=100_000)
    assert r["trades"] == 2
    assert r["total_pnl"] == -100.0
    assert r["agent_return_pct"] == -0.1
    assert r["buy_and_hold_spy_pct"] == 0.5
    assert r["alpha_vs_spy"] == round(-0.1 - 0.5, 2)
    assert r["no_trade_pct"] == 0.0
    assert r["window"] == "2026-06-09 → 2026-06-10"
    assert "Replay / Benchmark" in replay.format_report(r)


def test_spy_unavailable_is_graceful(monkeypatch):
    fake = [{"outcome": "loss", "pnl_pct": -1.0, "pnl_dollar": -100,
             "timestamp_exit": "2026-06-09T10:00:00"}]
    monkeypatch.setattr(replay.learning, "_load_trade_log", lambda: fake)
    monkeypatch.setattr(replay, "spy_buy_and_hold_pct", lambda s, e: None)
    r = replay.benchmark_report(starting_capital=100_000)
    assert r["buy_and_hold_spy_pct"] is None
    assert r["alpha_vs_spy"] is None
    assert "n/a" in replay.format_report(r)


def test_empty_report(monkeypatch):
    monkeypatch.setattr(replay.learning, "_load_trade_log", lambda: [])
    r = replay.benchmark_report()
    assert r["trades"] == 0
    assert "no completed trades" in replay.format_report(r).lower()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
