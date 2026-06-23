"""
Deterministic order-sizing guardrail — FeintTrade.
Run: python -m pytest tests/test_deterministic_sizing.py -v

Regression for the TQQQ over-size bug (2026-06-12): the model's own reasoning said
"Size: $91,473 × 40% × 0.55 (score 5-6) = $20,124 / $78.18 ≈ 257 shares. Correcting:
qty=257." but its JSON emitted "qty": 461 and 461 shares were placed — the conviction/
score factor was deterministic in _research_autobuy but NOT enforced for normal model
orders. _execute_orders now clamps every buy to equity × max_alloc × regime ×
conviction_factor(score) before place_order. These tests prove qty 461 (score 6, implied
cap 257) can never be placed.
"""

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import trade
from common import conviction_factor


# ── conviction_factor: single source of truth ────────────────────────────────

def test_conviction_factor_buckets():
    assert conviction_factor(10) == 1.00
    assert conviction_factor(9) == 1.00
    assert conviction_factor(8) == 0.85
    assert conviction_factor(7) == 0.85
    assert conviction_factor(6) == 0.55
    assert conviction_factor(5) == 0.55
    assert conviction_factor(4) == 0.30
    assert conviction_factor(1) == 0.30


def test_conviction_factor_non_numeric_returns_default():
    assert conviction_factor(None) is None
    assert conviction_factor("nope", default=1.0) == 1.0
    assert conviction_factor(6.4) == 0.55          # rounds to 6
    assert conviction_factor("7") == 0.85          # numeric string ok


# ── deterministic_position_qty_cap: pure arithmetic ──────────────────────────

def test_cap_includes_conviction_factor():
    """The TQQQ scenario: 91,473 × 40% × 0.55 / 78.18 ≈ 257 (with a 1% safety margin)."""
    qty_cap, headroom = trade.deterministic_position_qty_cap(
        78.18, 91_473, 40, regime_mult=1.0, conviction_factor=0.55,
    )
    assert abs(headroom - 91_473 * 0.40 * 0.55) < 1.0
    assert 250 <= qty_cap <= 257            # never above the 257 implied cap


def test_cap_subtracts_existing_long_exposure():
    qty_cap, headroom = trade.deterministic_position_qty_cap(
        100.0, 100_000, 20, regime_mult=1.0, conviction_factor=1.0, existing_long_mv=15_000,
    )
    assert abs(headroom - 5_000) < 1e-6     # 20% cap = 20k, minus 15k existing
    assert abs(qty_cap - 49.5) < 1e-6       # 5k/100 = 50, × 0.99 safety


def test_cap_zero_when_existing_fills_allocation():
    qty_cap, headroom = trade.deterministic_position_qty_cap(
        100.0, 100_000, 20, conviction_factor=0.55, existing_long_mv=99_999,
    )
    assert qty_cap == 0.0 and headroom == 0.0


def test_cap_zero_on_bad_inputs():
    assert trade.deterministic_position_qty_cap(0, 100_000, 40) == (0.0, 0.0)
    assert trade.deterministic_position_qty_cap(50, 0, 40) == (0.0, 0.0)
    assert trade.deterministic_position_qty_cap("x", 100_000, 40) == (0.0, 0.0)


# ── Integration: the 461 → must-not-place regression ──────────────────────────

def _wire_orchestrator(monkeypatch, tmp_path, placed):
    """Hermetic _execute_orders: mock the broker + side-effecting helpers."""
    import learning
    monkeypatch.setattr(learning, "OPEN_TRADES", tmp_path / "open_trades.json")
    monkeypatch.setattr(learning, "TRADE_LOG", tmp_path / "trade_log.jsonl")
    monkeypatch.setattr(learning, "PERF_CACHE", tmp_path / "performance.json")

    orch = importlib.import_module("scripts.orchestrator")
    monkeypatch.setattr(orch.trade, "place_order",
                        lambda sym, qty, side, price, **kwargs:
                        placed.append((sym, float(qty), side, price)) or {"id": "ok"})
    monkeypatch.setattr(orch.trade, "get_order_fill", lambda *a, **k: (0.0, None, "new"))
    monkeypatch.setattr(orch.trade, "check_duplicate_entry", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(orch, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(orch, "record_session_entry", lambda *a, **k: None)
    monkeypatch.setattr(orch, "check_daily_stop", lambda *a, **k: {"soft_stop": False, "hard_stop": False})
    monkeypatch.setattr(orch, "kill_active", lambda: False)
    monkeypatch.setattr(orch, "loss_streak_lockout_enforced", lambda: False)
    return orch


def test_low_score_buy_is_hard_blocked(monkeypatch, tmp_path):
    """SOP: a BUY scored below the minimum-to-enter is WATCH/SKIP, not a small position.
    A score-2 order must be rejected before place_order (conviction_factor only shrinks it)."""
    placed = []
    orch = _wire_orchestrator(monkeypatch, tmp_path, placed)
    events = []
    orch._execute_orders(
        [{"symbol": "NVDA", "qty": 50, "side": "buy", "limit_price": 100.0,
          "setup_type": "ema_vwap_cross", "score": 2, "conviction": 2, "reasoning": "weak"}],
        account={"equity": 100_000, "cash": 100_000, "last_equity": 100_000},
        positions=[], symbol_limits={"NVDA": 30},
        regime={"regime": "BULL", "multiplier": 1.0},
        setup_types={"NVDA": "ema_vwap_cross"}, collect_events=events,
    )
    assert not placed, "a score-2 buy must never reach place_order"
    assert any(e["status"] == "rejected" and "below the minimum" in e["message"] for e in events)


def test_qualifying_score_buy_passes_low_score_gate(monkeypatch, tmp_path):
    """A score-8 buy clears the low-score gate (≥ any min_buy_score 5/6) and is placed."""
    placed = []
    orch = _wire_orchestrator(monkeypatch, tmp_path, placed)
    orch._execute_orders(
        [{"symbol": "NVDA", "qty": 50, "side": "buy", "limit_price": 100.0,
          "setup_type": "ema_vwap_cross", "score": 8, "conviction": 8, "reasoning": "strong"}],
        account={"equity": 100_000, "cash": 100_000, "last_equity": 100_000},
        positions=[], symbol_limits={"NVDA": 30},
        regime={"regime": "BULL", "multiplier": 1.0},
        setup_types={"NVDA": "ema_vwap_cross"}, collect_events=[],
    )
    assert placed and placed[0][0] == "NVDA"


def test_equity_buy_skipped_when_market_closed_crypto_allowed(monkeypatch, tmp_path):
    """On a market holiday/weekend (broker clock closed), a non-crypto BUY can't fill and is
    skipped; crypto trades 24/7 and still goes through. Guards the Juneteenth churn bug."""
    placed = []
    orch = _wire_orchestrator(monkeypatch, tmp_path, placed)
    monkeypatch.setattr(orch.trade, "equities_open_now", lambda *a, **k: False)  # holiday
    monkeypatch.setattr(orch, "_get_snapshot", lambda s: {})                      # no crypto re-price net call
    events = []
    orch._execute_orders(
        [{"symbol": "NVDA", "qty": 10, "side": "buy", "limit_price": 100.0,
          "setup_type": "ema_vwap_cross", "score": 8, "conviction": 8},
         {"symbol": "BTC/USD", "qty": 0.01, "side": "buy", "limit_price": 60000.0,
          "setup_type": "crypto_scored", "score": 8, "conviction": 8}],
        account={"equity": 100_000, "cash": 100_000, "last_equity": 100_000},
        positions=[], symbol_limits={"NVDA": 30, "BTC/USD": 35},
        regime={"regime": "BULL", "multiplier": 1.0},
        setup_types={"NVDA": "ema_vwap_cross", "BTC/USD": "crypto_scored"},
        collect_events=events,
    )
    placed_syms = [p[0] for p in placed]
    assert "NVDA" not in placed_syms                      # equity buy skipped (market closed)
    assert "BTC/USD" in placed_syms                       # crypto still trades 24/7
    assert any(e["symbol"] == "NVDA" and e["status"] == "skipped" and "market is closed" in e["message"]
               for e in events)


def test_disabled_setup_buy_is_hard_blocked(monkeypatch, tmp_path):
    """A setup_type in trading_style.disabled_setups cannot open a new position, even
    with a strong score. Promotes the learning STOP-SETUP recommendation to a guardrail
    (momentum_breakout = the entire -$3,334 drawdown). Sells/proven setups unaffected."""
    placed = []
    orch = _wire_orchestrator(monkeypatch, tmp_path, placed)
    monkeypatch.setattr(orch, "trading_style",
                        lambda: {"disabled_setups": ["momentum_breakout"]})
    events = []
    orch._execute_orders(
        [{"symbol": "SOXL", "qty": 100, "side": "buy", "limit_price": 250.0,
          "setup_type": "momentum_breakout", "score": 9, "conviction": 9}],
        account={"equity": 1_000_000, "cash": 1_000_000, "last_equity": 1_000_000},
        positions=[], symbol_limits={"SOXL": 30},
        regime={"regime": "BULL", "multiplier": 1.0},
        setup_types={"SOXL": "momentum_breakout"}, collect_events=events,
    )
    assert not placed, "a disabled-setup buy must never reach place_order"
    assert any(e["status"] == "rejected" and "disabled" in e["message"].lower() for e in events)

    # A proven setup is NOT blocked.
    placed.clear()
    orch._execute_orders(
        [{"symbol": "FAS", "qty": 100, "side": "buy", "limit_price": 140.0,
          "setup_type": "bb_squeeze_breakout", "score": 9, "conviction": 9}],
        account={"equity": 1_000_000, "cash": 1_000_000, "last_equity": 1_000_000},
        positions=[], symbol_limits={"FAS": 30},
        regime={"regime": "BULL", "multiplier": 1.0},
        setup_types={"FAS": "bb_squeeze_breakout"}, collect_events=[],
    )
    assert placed and placed[0][0] == "FAS"


def test_pump_and_dump_setup_is_hard_blocked(monkeypatch, tmp_path):
    """Pump-and-dump wording is manipulation risk, never an executable entry setup."""
    placed = []
    orch = _wire_orchestrator(monkeypatch, tmp_path, placed)
    events = []
    orch._execute_orders(
        [{"symbol": "NVDA", "qty": 10, "side": "buy", "limit_price": 100.0,
          "setup_type": "pump_and_dump", "score": 10, "conviction": 10}],
        account={"equity": 100_000, "cash": 100_000, "last_equity": 100_000},
        positions=[], symbol_limits={"NVDA": 30},
        regime={"regime": "BULL", "multiplier": 1.0},
        setup_types={"NVDA": "pump_and_dump"}, collect_events=events,
    )

    assert not placed
    assert any("pump-and-dump" in e["message"] for e in events)


def test_scalp_setup_requires_score_8(monkeypatch, tmp_path):
    """Scalping/day-trading labels are allowed only as high-conviction setups."""
    placed = []
    orch = _wire_orchestrator(monkeypatch, tmp_path, placed)
    events = []
    orch._execute_orders(
        [{"symbol": "SPY", "qty": 10, "side": "buy", "limit_price": 500.0,
          "setup_type": "scalp_liquidity", "score": 7, "conviction": 7}],
        account={"equity": 100_000, "cash": 100_000, "last_equity": 100_000},
        positions=[], symbol_limits={"SPY": 15},
        regime={"regime": "BULL", "multiplier": 1.0},
        setup_types={"SPY": "scalp_liquidity"}, collect_events=events,
    )

    assert not placed
    assert any("score >= 8" in e["message"] for e in events)

    events.clear()
    orch._execute_orders(
        [{"symbol": "SPY", "qty": 10, "side": "buy", "limit_price": 500.0,
          "setup_type": "scalp_liquidity", "score": 8, "conviction": 8}],
        account={"equity": 100_000, "cash": 100_000, "last_equity": 100_000},
        positions=[], symbol_limits={"SPY": 15},
        regime={"regime": "BULL", "multiplier": 1.0},
        setup_types={"SPY": "scalp_liquidity"}, collect_events=events,
    )

    assert placed and placed[-1][0] == "SPY"


def test_real_config_disables_momentum_breakout(monkeypatch, tmp_path):
    """The shipped watchlist.json must keep momentum_breakout disabled (it was the
    entire realized drawdown). Guards against an accidental re-enable in config."""
    placed = []
    orch = _wire_orchestrator(monkeypatch, tmp_path, placed)  # uses REAL trading_style()
    events = []
    orch._execute_orders(
        [{"symbol": "SOXL", "qty": 50, "side": "buy", "limit_price": 250.0,
          "setup_type": "momentum_breakout", "score": 9, "conviction": 9}],
        account={"equity": 1_000_000, "cash": 1_000_000, "last_equity": 1_000_000},
        positions=[], symbol_limits={"SOXL": 30},
        regime={"regime": "BULL", "multiplier": 1.0},
        setup_types={"SOXL": "momentum_breakout"}, collect_events=events,
    )
    assert not placed, "real config must keep momentum_breakout blocked"


def test_setup_size_multiplier_shrinks_losing_setup(monkeypatch, tmp_path):
    """trading_style.setup_size_multiplier shrinks a historically-losing setup before the
    clamps. momentum_breakout 0.5 -> a 100-share buy becomes 50 (data-driven risk scaling)."""
    placed = []
    orch = _wire_orchestrator(monkeypatch, tmp_path, placed)
    monkeypatch.setattr(orch, "trading_style",
                        lambda: {"setup_size_multiplier": {"momentum_breakout": 0.5}})
    orch._execute_orders(
        [{"symbol": "SOXL", "qty": 100, "side": "buy", "limit_price": 250.0,
          "setup_type": "momentum_breakout", "score": 8, "conviction": 8}],
        account={"equity": 1_000_000, "cash": 1_000_000, "last_equity": 1_000_000},
        positions=[], symbol_limits={"SOXL": 30},
        regime={"regime": "BULL", "multiplier": 1.0},
        setup_types={"SOXL": "momentum_breakout"}, collect_events=[],
    )
    assert placed and abs(placed[0][1] - 50.0) < 1e-6      # 100 × 0.5

    # A setup NOT in the map is unscaled.
    placed.clear()
    orch._execute_orders(
        [{"symbol": "FAS", "qty": 100, "side": "buy", "limit_price": 140.0,
          "setup_type": "bb_squeeze_breakout", "score": 8, "conviction": 8}],
        account={"equity": 1_000_000, "cash": 1_000_000, "last_equity": 1_000_000},
        positions=[], symbol_limits={"FAS": 30},
        regime={"regime": "BULL", "multiplier": 1.0},
        setup_types={"FAS": "bb_squeeze_breakout"}, collect_events=[],
    )
    assert placed and abs(placed[0][1] - 100.0) < 1e-6     # unlisted -> 1.0


def test_oversized_model_buy_is_clamped_not_placed(monkeypatch, tmp_path):
    placed = []
    orch = _wire_orchestrator(monkeypatch, tmp_path, placed)

    events = []
    orch._execute_orders(
        [{"symbol": "TQQQ", "qty": 461, "side": "buy", "limit_price": 78.18,
          "setup_type": "ema_vwap_cross", "conviction": 6, "score": 6,
          "reasoning": "equity × 40% × 0.55 = $20,124 / $78.18 ≈ 257. Correcting: qty=257."}],
        account={"equity": 91_473, "cash": 91_473, "last_equity": 91_473},
        positions=[],
        symbol_limits={"TQQQ": 40},
        regime={"regime": "BULL", "multiplier": 1.0},
        setup_types={"TQQQ": "ema_vwap_cross"},
        collect_events=events,
    )

    assert placed, "the order should be CLAMPED and placed, not dropped"
    sym, qty, side, price = placed[0]
    assert sym == "TQQQ" and side == "buy"
    assert qty != 461, "the un-sized model qty must never reach place_order"
    assert qty <= 257, f"qty {qty} exceeds the score-6 conviction cap of ~257"
    assert any(e.get("status") == "clamped" for e in events), \
        f"a 'clamped' event should be recorded; got {events}"


def test_full_conviction_order_not_reduced(monkeypatch, tmp_path):
    """A score-9 (conviction 1.0) order within the alloc cap is left intact — the clamp
    only ever REDUCES an over-sized order, never shrinks a properly-sized one."""
    placed = []
    orch = _wire_orchestrator(monkeypatch, tmp_path, placed)

    # Isolate the conviction clamp from the setup-size multiplier (use a setup with mult 1.0).
    monkeypatch.setattr(orch, "trading_style", lambda: {})
    # 20% of 100k at $100 = 200 shares, exactly the conviction-1.0 allocation for a 20% cap.
    orch._execute_orders(
        [{"symbol": "PLTR", "qty": 200, "side": "buy", "limit_price": 100.0,
          "setup_type": "ema_vwap_cross", "score": 9}],
        account={"equity": 100_000, "cash": 100_000, "last_equity": 100_000},
        positions=[],
        symbol_limits={"PLTR": 20},
        regime={"regime": "BULL", "multiplier": 1.0},
        setup_types={"PLTR": "ema_vwap_cross"},
        collect_events=[],
    )
    assert placed and abs(placed[0][1] - 200) < 1e-6, \
        f"score-9 order within cap should be unchanged, got {placed}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
