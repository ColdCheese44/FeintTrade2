"""
Entry-tracking integrity tests — FeintTrade.
Run: python -m pytest tests/test_entry_tracking.py -v

Guards the learning log against recording a position that never actually filled.

Background: log_entry() runs on broker ACCEPTANCE, not FILL (orchestrator buy path).
A non-marketable limit buy that is accepted but never fills writes an orphan entry into
open_trades.json. detect_and_log_exits() then sees the symbol isn't in live positions
and — once the orphan ages past the 5-min recency guard — fabricates a phantom
round-trip exit at the live mark (the fake "$61k BTC buy" +9–10% wins).

Fix 1 (covered here): when cancel_stale_orders() cancels an unfilled BUY, the
orchestrator calls learning.forget_unfilled_entry() to drop the orphan before it can
become a phantom. trade.py stays free of any learning import; the orchestrator owns the
reconciliation.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import learning


@pytest.fixture
def L(tmp_path, monkeypatch):
    """Redirect learning's data files to a temp dir so tests never touch real data/."""
    monkeypatch.setattr(learning, "OPEN_TRADES", tmp_path / "open_trades.json")
    monkeypatch.setattr(learning, "TRADE_LOG", tmp_path / "trade_log.jsonl")
    monkeypatch.setattr(learning, "PERF_CACHE", tmp_path / "performance.json")
    return learning


def _crypto_position(symbol):
    return {"symbol": symbol, "asset_class": "crypto"}


def _age_orphan(L, symbol, minutes):
    """Backdate a tracked entry's timestamp so it sits past the recency guard."""
    ot = L._load_open_trades()
    ot[symbol]["timestamp_entry"] = (datetime.now() - timedelta(minutes=minutes)).isoformat()
    L._save_open_trades(ot)


# ── 1. Core: accepted-but-unfilled buy leaves no phantom ──────────────────────

def test_accepted_unfilled_buy_leaves_no_phantom(L):
    # 1. A buy ACCEPTED by the broker logs an entry immediately (pre-fill behavior).
    L.log_entry("BTC/USD", "buy", 0.5, 61000, setup_type="crypto_scored")
    assert "BTC/USD" in L._load_open_trades()

    # 2. The limit never fills; cancel_stale_orders() cancels it. The orchestrator
    #    reconciles: BTC is NOT a live position, so the orphan entry is dropped.
    removed = L.forget_unfilled_entry("BTC/USD", live_position_symbols={"ETH/USD"})
    assert removed is True
    assert "BTC/USD" not in L._load_open_trades()

    # 3. A later reconcile cycle (non-empty snapshot) must NOT fabricate a phantom exit.
    closed = L.detect_and_log_exits(
        [_crypto_position("ETH/USD")], "cycle_exit",
        price_lookup={"BTC/USD": 67000},
    )
    assert closed == []
    assert L._load_trade_log() == []


# ── 2. Documents the bug Fix 1 closes ─────────────────────────────────────────

def test_unreconciled_old_orphan_becomes_phantom(L):
    """An OLD orphan (cancelled but never reconciled) IS fabricated into a phantom
    round-trip by detect_and_log_exits — the exact failure Fix 1 prevents."""
    L.log_entry("BTC/USD", "buy", 0.5, 61000, setup_type="crypto_scored")
    _age_orphan(L, "BTC/USD", minutes=30)  # past the 5-min recency guard

    closed = L.detect_and_log_exits(
        [_crypto_position("ETH/USD")], "cycle_exit",
        price_lookup={"BTC/USD": 67000},
    )
    assert closed == ["BTC/USD"]                       # phantom fabricated — the bug
    log = L._load_trade_log()
    assert len(log) == 1 and log[0]["symbol"] == "BTC/USD"


def test_reconcile_prevents_phantom_for_aged_orphan(L):
    """Fix 1 end-to-end: reconciling the cancelled buy prevents the phantom that
    test_unreconciled_old_orphan_becomes_phantom shows would otherwise fire."""
    L.log_entry("BTC/USD", "buy", 0.5, 61000, setup_type="crypto_scored")
    _age_orphan(L, "BTC/USD", minutes=30)

    assert L.forget_unfilled_entry("BTC/USD", live_position_symbols={"ETH/USD"}) is True

    closed = L.detect_and_log_exits(
        [_crypto_position("ETH/USD")], "cycle_exit",
        price_lookup={"BTC/USD": 67000},
    )
    assert closed == []
    assert L._load_trade_log() == []


# ── 3. Safety guards on forget_unfilled_entry ─────────────────────────────────

def test_forget_keeps_held_position(L):
    """Scale-in safety: a cancelled ADD must NOT drop tracking of a symbol that IS a
    live position (its base lot already filled)."""
    L.log_entry("BTC/USD", "buy", 0.5, 60000, setup_type="crypto_scored")
    removed = L.forget_unfilled_entry("BTC/USD", live_position_symbols={"BTC/USD"})
    assert removed is False
    assert "BTC/USD" in L._load_open_trades()


def test_forget_only_removes_buy_entries(L):
    """A short (sell-side) entry de-risks and logs its own exit — never dropped here."""
    L.log_entry("SQQQ", "sell", 10, 20, setup_type="inverse_momentum")
    removed = L.forget_unfilled_entry("SQQQ", live_position_symbols=set())
    assert removed is False
    assert "SQQQ" in L._load_open_trades()


def test_forget_no_entry_returns_false(L):
    assert L.forget_unfilled_entry("DOGE/USD", live_position_symbols=set()) is False


def test_forget_normalizes_symbol(L):
    """The cancelled Alpaca order symbol may be 'BTCUSD'; the tracked key is 'BTC/USD'."""
    L.log_entry("BTC/USD", "buy", 0.5, 61000, setup_type="crypto_scored")
    removed = L.forget_unfilled_entry("BTCUSD", live_position_symbols={"ETHUSD"})
    assert removed is True
    assert "BTC/USD" not in L._load_open_trades()


def test_forget_without_positions_arg_removes_buy(L):
    """When no positions snapshot is supplied, a tracked BUY orphan is still removed."""
    L.log_entry("SOL/USD", "buy", 10, 150, setup_type="crypto_scored")
    assert L.forget_unfilled_entry("SOL/USD") is True
    assert "SOL/USD" not in L._load_open_trades()


# ── 4. Orchestrator wiring: cancel_stale_orders → forget_unfilled_entry ────────

def test_orchestrator_cancel_stale_reconciles_orphan(L, monkeypatch):
    """The structured dict returned by trade.cancel_stale_orders() flows through the
    orchestrator helper and drops the orphan for the cancelled BUY."""
    import importlib
    orch = importlib.import_module("scripts.orchestrator")

    L.log_entry("BTC/USD", "buy", 0.5, 61000, setup_type="crypto_scored")

    # Fake the broker round-trip: one stale BUY cancelled, returned in structured form.
    monkeypatch.setattr(orch.trade, "cancel_stale_orders", lambda *a, **k: [
        {"symbol": "BTC/USD", "side": "buy", "qty": "0.5",
         "limit_price": "61000", "desc": "buy 0.5 BTC/USD @ $61000"}
    ])

    descs = orch._cancel_stale_orders(positions=[_crypto_position("ETH/USD")])
    assert descs == ["buy 0.5 BTC/USD @ $61000"]
    assert "BTC/USD" not in L._load_open_trades()


def test_orchestrator_cancel_stale_keeps_held_scale_in(L, monkeypatch):
    """If the cancelled BUY's symbol is still a live position (scale-in base lot
    filled), the orchestrator must NOT drop its tracking."""
    import importlib
    orch = importlib.import_module("scripts.orchestrator")

    L.log_entry("BTC/USD", "buy", 0.8, 60000, setup_type="crypto_scored")

    monkeypatch.setattr(orch.trade, "cancel_stale_orders", lambda *a, **k: [
        {"symbol": "BTC/USD", "side": "buy", "qty": "0.3",
         "limit_price": "60000", "desc": "buy 0.3 BTC/USD @ $60000"}
    ])

    orch._cancel_stale_orders(positions=[_crypto_position("BTC/USD")])
    assert "BTC/USD" in L._load_open_trades()


def test_orchestrator_cancel_stale_ignores_sell(L, monkeypatch):
    """A cancelled SELL must not touch entry-tracking (sells log their own exits)."""
    import importlib
    orch = importlib.import_module("scripts.orchestrator")

    L.log_entry("BTC/USD", "buy", 0.5, 61000, setup_type="crypto_scored")

    monkeypatch.setattr(orch.trade, "cancel_stale_orders", lambda *a, **k: [
        {"symbol": "BTC/USD", "side": "sell", "qty": "0.5",
         "limit_price": "67000", "desc": "sell 0.5 BTC/USD @ $67000"}
    ])

    orch._cancel_stale_orders(positions=[])
    assert "BTC/USD" in L._load_open_trades()


# ── 5. Partial profit is logged + leaves no orphan on final close ─────────────

def _swing_thresholds():
    return {"swing_stop_pct": -3.0, "partial_profit_pct": 6.0,
            "trail_arm_pct": 5.0, "trail_giveback_pct": 3.0}


def test_partial_profit_logged_and_no_orphan(L, tmp_path, monkeypatch):
    """A swing partial-profit sale must reduce the tracked qty AND record a
    partial_profit trade. Skipping it (the bug) left the full entry tracked, so the
    eventual close logged a partial of the inflated qty and left a residual orphan that
    detect_and_log_exits() fabricated into a phantom round-trip win."""
    import importlib
    orch = importlib.import_module("scripts.orchestrator")

    monkeypatch.setattr(orch, "_PEAKS_FILE", tmp_path / "peaks.json")
    monkeypatch.setattr(orch, "run", lambda *a, **k: {"id": "stub"})
    monkeypatch.setattr(orch.trade, "get_order_fill", lambda *a, **k: (0.5, 64000.0, "filled"))
    monkeypatch.setattr(orch, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(orch, "trading_style", _swing_thresholds)
    monkeypatch.setattr(orch, "market_phase", lambda: "REGULAR")

    # Tracked full-size entry; a crypto position now +7% (triggers the +6% partial).
    L.log_entry("BTC/USD", "buy", 1.0, 60000, setup_type="crypto_scored")
    up_pos = {"symbol": "BTC/USD", "asset_class": "crypto", "qty": "1.0",
              "current_price": "64200", "unrealized_plpc": "0.07",
              "avg_entry_price": "60000"}

    actions, closed = orch._manage_swing_exits([up_pos])

    assert any("PARTIAL" in a for a in actions)
    assert "BTC/USD" not in closed                       # position stays open
    log = L._load_trade_log()
    assert len(log) == 1 and log[0]["exit_reason"] == "partial_profit"
    assert log[0]["partial"] is True
    ot = L._load_open_trades()                           # tracked qty halved, entry kept
    assert "BTC/USD" in ot and abs(float(ot["BTC/USD"]["qty"]) - 0.5) < 1e-6

    # The remaining 0.5 now stops out → FULL close must leave NO orphan behind.
    down_pos = {"symbol": "BTC/USD", "asset_class": "crypto", "qty": "0.5",
                "current_price": "57000", "unrealized_plpc": "-0.05",
                "avg_entry_price": "60000"}
    actions2, closed2 = orch._manage_swing_exits([down_pos])

    assert "BTC/USD" in closed2
    assert "BTC/USD" not in L._load_open_trades()        # no residual orphan
    assert len(L._load_trade_log()) == 2                 # partial + full close

    # And a later reconcile finds nothing to fabricate.
    phantom = orch.detect_and_log_exits(
        [_crypto_position("ETH/USD")], "cycle_exit", price_lookup={"BTC/USD": 57000},
    )
    assert phantom == []
    assert len(L._load_trade_log()) == 2                 # still no phantom appended


def test_unfilled_partial_profit_does_not_log_or_reduce_open_trade(L, tmp_path, monkeypatch):
    """Broker acceptance is not an exit. If a partial-profit order rests unfilled, the
    learning log and tracked open quantity must stay unchanged."""
    import importlib
    orch = importlib.import_module("scripts.orchestrator")

    peaks_file = tmp_path / "peaks.json"
    monkeypatch.setattr(orch, "_PEAKS_FILE", peaks_file)
    monkeypatch.setattr(orch, "run", lambda *a, **k: {"id": "accepted_not_filled"})
    monkeypatch.setattr(orch.trade, "get_order_fill", lambda *a, **k: (0.0, None, "new"))
    monkeypatch.setattr(orch, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(orch, "trading_style", _swing_thresholds)
    monkeypatch.setattr(orch, "market_phase", lambda: "REGULAR")

    L.log_entry("BTC/USD", "buy", 1.0, 60000, setup_type="crypto_scored")
    up_pos = {"symbol": "BTC/USD", "asset_class": "crypto", "qty": "1.0",
              "current_price": "64200", "unrealized_plpc": "0.07",
              "avg_entry_price": "60000"}

    actions, closed = orch._manage_swing_exits([up_pos])

    assert closed == set()
    assert any("unfilled" in a for a in actions)
    assert L._load_trade_log() == []
    ot = L._load_open_trades()
    assert "BTC/USD" in ot and float(ot["BTC/USD"]["qty"]) == 1.0
    peaks = json.loads(peaks_file.read_text(encoding="utf-8"))
    assert peaks["BTC/USD"]["partialed"] is False


def test_execute_sell_unfilled_does_not_log_exit(L, monkeypatch):
    """The generic order executor must also distinguish accepted from filled sells."""
    import importlib
    orch = importlib.import_module("scripts.orchestrator")

    L.log_entry("NVDA", "buy", 10, 100, setup_type="manual")
    monkeypatch.setattr(orch, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(orch.trade, "place_order", lambda *a, **k: {"id": "sell_unfilled"})
    monkeypatch.setattr(orch.trade, "get_order_fill", lambda *a, **k: (0.0, None, "new"))

    events = []
    orders = orch._execute_orders(
        [{"symbol": "NVDA", "qty": 10, "side": "sell", "limit_price": 95,
          "setup_type": "manual", "reasoning": "test unfilled sell"}],
        account={"equity": 100000, "cash": 90000},
        positions=[{"symbol": "NVDA", "asset_class": "us_equity", "qty": "10",
                    "market_value": "950", "current_price": "95"}],
        symbol_limits={"NVDA": 30},
        regime={"regime": "BULL", "multiplier": 1.0},
        setup_types={"NVDA": "manual"},
        collect_events=events,
    )

    assert orders and orders[0]["fill_status"] == "new"
    assert events[-1]["status"] == "placed_unfilled"
    assert L._load_trade_log() == []
    assert "NVDA" in L._load_open_trades()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
