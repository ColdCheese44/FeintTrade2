"""
Intraday exit-consolidation tests — FeintTrade.
Run: python -m pytest tests/test_intraday_consolidation.py -v

run_intraday() used to carry its OWN copy of the swing-exit loop, which drifted from the
canonical _manage_swing_exits() and harbored the same unlogged-partial bug in both copies.
These tests pin the de-duplication: run_intraday must DELEGATE to the single canonical exit
path, handing it EQUITY-ONLY positions (crypto is the hourly crypto cycle's job, never the
intraday path's) tagged with the "Intraday " learning note, and still run the trailing
detect_and_log_exits() sync on the full snapshot.
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
    """Redirect learning's data files to a temp dir so tests never touch real data/."""
    monkeypatch.setattr(learning, "OPEN_TRADES", tmp_path / "open_trades.json")
    monkeypatch.setattr(learning, "TRADE_LOG", tmp_path / "trade_log.jsonl")
    monkeypatch.setattr(learning, "PERF_CACHE", tmp_path / "performance.json")
    return learning


def _swing_thresholds():
    return {"swing_stop_pct": -3.0, "partial_profit_pct": 6.0,
            "trail_arm_pct": 5.0, "trail_giveback_pct": 3.0}


def _wire_intraday(orch, monkeypatch, tmp_path, positions):
    """
    Stand run_intraday() up against stubs: market open, kill switch off, stubbed broker
    (`run`), notifier and journal; learning paths are already redirected by the L fixture.
    The canonical _manage_swing_exits is spied THROUGH (real logic still runs) so we can
    capture exactly what run_intraday handed it. Returns capture lists:
      (orders, journal, dle_calls, swing_calls)
    """
    orders, journal, dle_calls, swing_calls = [], [], [], []

    monkeypatch.setattr(orch, "_PEAKS_FILE", tmp_path / "peaks.json")
    monkeypatch.setattr(orch, "kill_active", lambda: False)
    monkeypatch.setattr(orch, "safe_run", lambda *a, **k: {"is_open": True})
    monkeypatch.setattr(orch, "get_positions_norm", lambda: positions)
    monkeypatch.setattr(orch, "trading_style", _swing_thresholds)
    monkeypatch.setattr(orch, "market_phase", lambda: "REGULAR")
    monkeypatch.setattr(orch, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(orch, "append_journal_text", lambda text: journal.append(text))

    def _run(script, *args):
        if script == "trade.py" and args and args[0] == "order":
            orders.append(args)
        return {"id": "stub"}
    monkeypatch.setattr(orch, "run", _run)
    monkeypatch.setattr(orch.trade, "get_order_fill", lambda *a, **k: (10.0, 95.0, "filled"))

    # Isolate the trailing learning sync (record it ran; don't exercise reconciliation).
    def _dle(positions, exit_reason="eod_close", **kw):
        dle_calls.append((positions, exit_reason))
        return []
    monkeypatch.setattr(orch, "detect_and_log_exits", _dle)

    # Spy-THROUGH the canonical exit fn: the real consolidated path runs end-to-end while
    # we record exactly what run_intraday passed (proving delegation + the equity filter).
    real_manage = orch._manage_swing_exits

    def _spy(positions, note_prefix=""):
        swing_calls.append((positions, note_prefix))
        return real_manage(positions, note_prefix=note_prefix)
    monkeypatch.setattr(orch, "_manage_swing_exits", _spy)

    return orders, journal, dle_calls, swing_calls


def test_swing_stop_binds_tighter_than_regime(monkeypatch):
    """run_trading now shares _manage_swing_exits, so morning-session stops fire at the
    SWING stop (~-3%) — not the looser regime stop (-5%). A -4% loser (which the old
    run_trading regime check would have RIDDEN) is cut; a -2% position is held."""
    orch = importlib.import_module("scripts.orchestrator")
    monkeypatch.setattr(orch, "trading_style", _swing_thresholds)   # swing_stop_pct = -3%
    act, frac, _ = orch._swing_exit_decision("NVDA", -4.0, {})
    assert act == "stop" and frac == 1.0
    act_hold, _, _ = orch._swing_exit_decision("NVDA", -2.0, {})
    assert act_hold is None


def test_run_intraday_routes_equity_exit_through_manage_swing_exits(L, tmp_path, monkeypatch):
    """An equity below the swing stop is exited via the ONE canonical path; a deep-
    underwater crypto position in the SAME snapshot is left untouched (crypto-cycle's job)."""
    orch = importlib.import_module("scripts.orchestrator")

    nvda = {"symbol": "NVDA", "qty": "10", "current_price": "95",
            "unrealized_plpc": "-0.05", "avg_entry_price": "100"}     # -5% → past -3% stop
    btc = {"symbol": "BTC/USD", "asset_class": "crypto", "qty": "0.1",
           "current_price": "54000", "unrealized_plpc": "-0.10",      # -10%, but crypto
           "avg_entry_price": "60000"}
    orders, journal, dle_calls, swing_calls = _wire_intraday(
        orch, monkeypatch, tmp_path, [nvda, btc])

    L.log_entry("NVDA", "buy", 10, 100, setup_type="breakout")

    orch.run_intraday()

    # 1. Delegated to the ONE canonical exit fn, exactly once...
    assert len(swing_calls) == 1
    passed_positions, note_prefix = swing_calls[0]
    # 2. ...with EQUITY-ONLY positions (crypto filtered out before the call)...
    assert [p["symbol"] for p in passed_positions] == ["NVDA"]
    # 3. ...tagged as the intraday routine for the learning log.
    assert note_prefix == "Intraday "

    # 4. Exactly one sell order, for the equity — never the crypto.
    assert len(orders) == 1
    assert orders[0][1] == "NVDA" and orders[0][3] == "sell"
    assert all(o[1] != "BTC/USD" for o in orders)

    # 5. The exit was logged through the consolidated path, carrying the Intraday tag.
    log = L._load_trade_log()
    assert len(log) == 1
    assert log[0]["symbol"] == "NVDA"
    assert "stop_loss" in (log[0]["exit_reason"], log[0]["exit_reason_raw"])
    assert log[0]["exit_notes"].startswith("Intraday ")

    # 6. The trailing learning sync still runs, on the FULL snapshot (both names).
    assert len(dle_calls) == 1
    synced_positions, reason = dle_calls[0]
    assert reason == "intraday_exit"
    assert {p["symbol"] for p in synced_positions} == {"NVDA", "BTC/USD"}

    # 7. Journal recorded the intraday action.
    body = "".join(journal)
    assert "### Intraday Check" in body and "STOP-LOSS NVDA" in body


def test_run_intraday_never_manages_crypto(L, tmp_path, monkeypatch):
    """A crypto position far past its stop must produce NO intraday action: the equity
    filter hands _manage_swing_exits an empty list, so no order, no exit, no journal —
    the crypto entry stays tracked for the hourly crypto cycle to manage."""
    orch = importlib.import_module("scripts.orchestrator")

    btc = {"symbol": "BTC/USD", "asset_class": "crypto", "qty": "0.1",
           "current_price": "48000", "unrealized_plpc": "-0.20",      # -20%, ignored here
           "avg_entry_price": "60000"}
    orders, journal, dle_calls, swing_calls = _wire_intraday(
        orch, monkeypatch, tmp_path, [btc])

    L.log_entry("BTC/USD", "buy", 0.1, 60000, setup_type="crypto_scored")

    orch.run_intraday()

    # Delegated once, but with an EMPTY equity list (crypto filtered out).
    assert len(swing_calls) == 1
    assert swing_calls[0][0] == []
    # No sell placed, no exit logged — crypto is untouched on the intraday path.
    assert orders == []
    assert L._load_trade_log() == []
    # The crypto entry is still tracked (the hourly crypto cycle owns it).
    assert "BTC/USD" in L._load_open_trades()
    # The trailing learning sync still ran, on the full snapshot.
    assert len(dle_calls) == 1 and dle_calls[0][1] == "intraday_exit"
    # No actions → no Intraday Check journal block.
    assert "### Intraday Check" not in "".join(journal)


def test_marketopen_sweeps_overnight_stops_before_summary(monkeypatch):
    """run_marketopen() must cut an overnight-identified stop at the FIRST action of the
    session — delegating to the canonical _manage_swing_exits with the live book and the
    'MarketOpen ' learning tag — BEFORE it does any of the heavy summary work. A leveraged
    loser that drifted below its stop after-hours should not wait for the first intraday
    cycle to run."""
    orch = importlib.import_module("scripts.orchestrator")

    tqqq = {"symbol": "TQQQ", "qty": "116", "current_price": "82",
            "unrealized_plpc": "-0.05", "avg_entry_price": "86"}     # -5% → past -3% stop
    monkeypatch.setattr(orch, "run", lambda *a, **k: {"equity": 100000, "last_equity": 100000})
    monkeypatch.setattr(orch, "get_positions_norm", lambda: [tqqq])

    swing_calls = []
    def _spy(positions, note_prefix=""):
        swing_calls.append((positions, note_prefix))
        return ([], set())
    monkeypatch.setattr(orch, "_manage_swing_exits", _spy)

    # Sentinel stops run_marketopen right after the swing-exit pass so we don't have to
    # stub the entire summary/Claude/report pipeline — and it proves the sweep runs FIRST.
    class _StopHere(Exception):
        pass
    def _ctx():
        raise _StopHere()
    monkeypatch.setattr(orch, "_load_context", _ctx)

    with pytest.raises(_StopHere):
        orch.run_marketopen()

    assert len(swing_calls) == 1, "market-open must run the canonical swing-exit pass exactly once"
    passed_positions, note_prefix = swing_calls[0]
    assert [p["symbol"] for p in passed_positions] == ["TQQQ"]
    assert note_prefix == "MarketOpen "


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
