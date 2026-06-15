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
                        lambda sym, qty, side, price: placed.append((sym, float(qty), side, price)) or {"id": "ok"})
    monkeypatch.setattr(orch.trade, "get_order_fill", lambda *a, **k: (0.0, None, "new"))
    monkeypatch.setattr(orch.trade, "check_duplicate_entry", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(orch, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(orch, "record_session_entry", lambda *a, **k: None)
    monkeypatch.setattr(orch, "check_daily_stop", lambda *a, **k: {"soft_stop": False, "hard_stop": False})
    monkeypatch.setattr(orch, "kill_active", lambda: False)
    monkeypatch.setattr(orch, "loss_streak_lockout_enforced", lambda: False)
    return orch


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

    # 20% of 100k at $100 = 200 shares, exactly the conviction-1.0 allocation for a 20% cap.
    orch._execute_orders(
        [{"symbol": "PLTR", "qty": 200, "side": "buy", "limit_price": 100.0,
          "setup_type": "momentum_breakout", "score": 9}],
        account={"equity": 100_000, "cash": 100_000, "last_equity": 100_000},
        positions=[],
        symbol_limits={"PLTR": 20},
        regime={"regime": "BULL", "multiplier": 1.0},
        setup_types={"PLTR": "momentum_breakout"},
        collect_events=[],
    )
    assert placed and abs(placed[0][1] - 200) < 1e-6, \
        f"score-9 order within cap should be unchanged, got {placed}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
