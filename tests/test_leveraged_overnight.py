"""
Pre-close leveraged-long flatten — FeintTrade.
Run: python -m pytest tests/test_leveraged_overnight.py -v

A 3x leveraged-long ETF (TQQQ/SOXL/FNGU/LABU/FAS) carries decay + amplified gap risk
overnight; realized losses averaged ~-8.4% vs the -3% stop because leveraged losers
gapped past the stop overnight. In the final pre-close window, _manage_swing_exits
flattens a RED leveraged long that the normal rules would otherwise carry — but never a
winner, a non-leveraged name, or when the broker clock can't be read (fails safe).
"""
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import learning


@pytest.fixture
def L(tmp_path, monkeypatch):
    monkeypatch.setattr(learning, "OPEN_TRADES", tmp_path / "open_trades.json")
    monkeypatch.setattr(learning, "TRADE_LOG", tmp_path / "trade_log.jsonl")
    monkeypatch.setattr(learning, "PERF_CACHE", tmp_path / "performance.json")
    return learning


def _cfg():
    return {"swing_stop_pct": -3.0, "partial_profit_pct": 10.0, "trail_arm_pct": 5.0,
            "trail_giveback_pct": 4.0, "flatten_red_leveraged_before_close": True,
            "leveraged_overnight_loss_pct": -0.5, "leveraged_close_window_min": 15}


# ── Unit: the gate ────────────────────────────────────────────────────────────

def test_flatten_gate_matrix(monkeypatch):
    orch = importlib.import_module("scripts.orchestrator")
    monkeypatch.setattr(orch, "trading_style", _cfg)
    due = orch._leveraged_overnight_flatten_due
    assert due("TQQQ", -2.0, 10) is True      # red leveraged, in window
    assert due("TQQQ", -2.0, 30) is False     # outside the pre-close window
    assert due("TQQQ", 1.0, 10) is False      # green — winners ride
    assert due("TQQQ", -0.2, 10) is False     # within the -0.5% buffer (essentially flat)
    assert due("NVDA", -5.0, 10) is False     # not leveraged (its own -3% stop handles it)
    assert due("TQQQ", -2.0, None) is False   # clock unreachable → fail safe, no flatten


def test_flatten_gate_disabled_by_config(monkeypatch):
    orch = importlib.import_module("scripts.orchestrator")
    cfg = _cfg()
    cfg["flatten_red_leveraged_before_close"] = False
    monkeypatch.setattr(orch, "trading_style", lambda: cfg)
    assert orch._leveraged_overnight_flatten_due("TQQQ", -2.0, 10) is False


# ── Integration: _manage_swing_exits ──────────────────────────────────────────

def _wire(orch, monkeypatch, tmp_path, orders, mins=10):
    monkeypatch.setattr(orch, "_PEAKS_FILE", tmp_path / "peaks.json")
    monkeypatch.setattr(orch, "trading_style", _cfg)
    monkeypatch.setattr(orch.trade, "equities_open_now", lambda *a, **k: True)
    monkeypatch.setattr(orch, "_minutes_to_equity_close", lambda: mins)
    monkeypatch.setattr(orch, "_notify", lambda *a, **k: None)

    def _run(script, *args):
        if script == "trade.py" and args and args[0] == "order":
            orders.append(args)
        return {"id": "stub"}
    monkeypatch.setattr(orch, "run", _run)
    monkeypatch.setattr(orch, "_confirm_order_fill",
                        lambda result, q, p: (float(q), float(p), "filled"))


def test_red_leveraged_flattened_in_window(L, tmp_path, monkeypatch):
    """TQQQ at -2% (ABOVE the -3% stop, so normal rules would CARRY it) is flattened in
    the pre-close window and logged as a stop."""
    orch = importlib.import_module("scripts.orchestrator")
    orders = []
    _wire(orch, monkeypatch, tmp_path, orders, mins=10)
    L.log_entry("TQQQ", "buy", 100, 86, setup_type="swing_momentum")

    tqqq = {"symbol": "TQQQ", "qty": "100", "current_price": "84.3",
            "unrealized_plpc": "-0.02", "avg_entry_price": "86"}    # -2%, above -3% stop
    actions, closed = orch._manage_swing_exits([tqqq])

    assert any(o[1] == "TQQQ" and o[3] == "sell" for o in orders), "TQQQ should be flattened"
    assert "TQQQ" in closed
    log = L._load_trade_log()
    assert log and log[0]["symbol"] == "TQQQ"
    assert "stop_loss" in (log[0]["exit_reason"], log[0]["exit_reason_raw"])


def test_red_leveraged_held_outside_window(L, tmp_path, monkeypatch):
    """The SAME -2% TQQQ is HELD when we're not near the close — the normal -3% stop
    hasn't tripped, so nothing fires."""
    orch = importlib.import_module("scripts.orchestrator")
    orders = []
    _wire(orch, monkeypatch, tmp_path, orders, mins=120)     # far from close
    L.log_entry("TQQQ", "buy", 100, 86, setup_type="swing_momentum")

    tqqq = {"symbol": "TQQQ", "qty": "100", "current_price": "84.3",
            "unrealized_plpc": "-0.02", "avg_entry_price": "86"}
    orch._manage_swing_exits([tqqq])
    assert not any(o[1] == "TQQQ" for o in orders)


def test_red_nonleveraged_not_flattened(L, tmp_path, monkeypatch):
    """A red NON-leveraged name above its stop is never touched by the leveraged guard."""
    orch = importlib.import_module("scripts.orchestrator")
    orders = []
    _wire(orch, monkeypatch, tmp_path, orders, mins=10)
    L.log_entry("NVDA", "buy", 10, 100, setup_type="ema_vwap_cross")

    nvda = {"symbol": "NVDA", "qty": "10", "current_price": "98",
            "unrealized_plpc": "-0.02", "avg_entry_price": "100"}   # -2%, not leveraged
    orch._manage_swing_exits([nvda])
    assert not any(o[1] == "NVDA" for o in orders)


def test_winning_leveraged_not_flattened(L, tmp_path, monkeypatch):
    """A leveraged long in PROFIT rides even in the pre-close window (winners run)."""
    orch = importlib.import_module("scripts.orchestrator")
    orders = []
    _wire(orch, monkeypatch, tmp_path, orders, mins=5)
    L.log_entry("SOXL", "buy", 50, 270, setup_type="bb_squeeze_breakout")

    soxl = {"symbol": "SOXL", "qty": "50", "current_price": "278",
            "unrealized_plpc": "0.03", "avg_entry_price": "270"}    # +3%, green
    orch._manage_swing_exits([soxl])
    assert not any(o[1] == "SOXL" for o in orders)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
