"""
Trailing-peak state isolation tests — MindHub Trader.
Run: python -m pytest tests/test_peak_state_isolation.py -v

_manage_swing_exits() persists each open position's running peak unrealized-pnl to ONE
shared file (data/position_peaks.json) so the swing trailing stop ("give back at most 3%
from the peak") is measured from the true high-water mark across cycles.

The bug this guards: the routines call _manage_swing_exits with DIFFERENT SUBSETS of the
book — run_crypto passes crypto-only, run_intraday passes equity-only, run_cycle passes
all — but _save_peaks used to rewrite the whole file keeping ONLY the symbols in the
current call. So the hourly crypto cycle wiped every equity trailing-peak record (and a
manual intraday run would wipe crypto's). The next cycle then rebuilt each peak from the
position's CURRENT pnl — a LOWER peak — which loosens the trailing stop and holds winners
past where the SOP intends to trail them out.

Fix: _save_peaks prunes closed positions ONLY within the asset class(es) the call actually
managed (classified via is_crypto on the stored key). A crypto-only call leaves equity
peaks alone, and vice-versa, while still dropping genuinely-closed names in its own class.
"""

import importlib
import json
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


def _wire(orch, monkeypatch, tmp_path):
    """
    Stand _manage_swing_exits up against stubs: shared peaks file in tmp, deterministic
    swing thresholds, market open (so equities are managed), and a recording broker stub
    so any swing exit is captured instead of hitting trade.py. Returns the peaks-file Path
    and the captured-orders list.
    """
    peaks_file = tmp_path / "peaks.json"
    orders = []

    monkeypatch.setattr(orch, "_PEAKS_FILE", peaks_file)
    monkeypatch.setattr(orch, "trading_style", _swing_thresholds)
    monkeypatch.setattr(orch, "market_phase", lambda: "REGULAR")
    monkeypatch.setattr(orch, "_notify", lambda *a, **k: None)

    def _run(script, *args):
        if script == "trade.py" and args and args[0] == "order":
            orders.append(args)
        return {"id": "stub"}
    monkeypatch.setattr(orch, "run", _run)
    monkeypatch.setattr(orch.trade, "get_order_fill", lambda *a, **k: (10.0, 106.18, "filled"))

    return peaks_file, orders


def _seed_peaks(peaks_file, mapping):
    peaks_file.write_text(json.dumps(mapping), encoding="utf-8")


def _read_peaks(peaks_file):
    return json.loads(peaks_file.read_text(encoding="utf-8"))


def test_managing_crypto_does_not_delete_equity_peak(L, tmp_path, monkeypatch):
    """The hourly crypto cycle hands _manage_swing_exits crypto-only positions. It must
    preserve a tracked EQUITY's peak record untouched, while still pruning a genuinely
    closed CRYPTO name from its own asset class."""
    orch = importlib.import_module("scripts.orchestrator")
    peaks_file, orders = _wire(orch, monkeypatch, tmp_path)

    _seed_peaks(peaks_file, {
        "NVDA":    {"peak": 12.0, "partialed": True},   # equity — NOT managed here → survive
        "ETH/USD": {"peak":  9.0, "partialed": False},  # crypto, now closed → prune
        "BTC/USD": {"peak":  4.0, "partialed": False},  # crypto, live → survive + update
    })

    # Crypto-only call (what run_crypto passes), BTC mildly up → no exit fires.
    btc = {"symbol": "BTC/USD", "asset_class": "crypto", "qty": "0.1",
           "current_price": "50000", "unrealized_plpc": "0.055",
           "avg_entry_price": "47393"}
    actions, closed = orch._manage_swing_exits([btc])

    assert actions == [] and closed == set()            # no exit on a +5.5% holder
    assert orders == []

    peaks = _read_peaks(peaks_file)
    # The equity peak is fully preserved — value intact, not reset from current pnl.
    assert peaks["NVDA"] == {"peak": 12.0, "partialed": True}
    # The live crypto survives and its peak advanced (4.0 → 5.5).
    assert abs(peaks["BTC/USD"]["peak"] - 5.5) < 1e-9
    # A closed crypto in the managed class is still pruned (the prune itself still works).
    assert "ETH/USD" not in peaks


def test_managing_equity_does_not_delete_crypto_peak(L, tmp_path, monkeypatch):
    """Symmetric guard: a (manual) intraday-style equity-only call must preserve a tracked
    CRYPTO's peak record, while pruning a genuinely closed EQUITY name."""
    orch = importlib.import_module("scripts.orchestrator")
    peaks_file, orders = _wire(orch, monkeypatch, tmp_path)

    _seed_peaks(peaks_file, {
        "BTC/USD": {"peak": 8.0, "partialed": True},    # crypto — NOT managed here → survive
        "AMD":     {"peak": 7.0, "partialed": False},   # equity, now closed → prune
        "NVDA":    {"peak": 3.0, "partialed": False},   # equity, live → survive + update
    })

    # Equity-only call (what run_intraday passes), NVDA mildly up → no exit fires.
    nvda = {"symbol": "NVDA", "asset_class": "us_equity", "qty": "10",
            "current_price": "104", "unrealized_plpc": "0.04",
            "avg_entry_price": "100"}
    actions, closed = orch._manage_swing_exits([nvda])

    assert actions == [] and closed == set()
    assert orders == []

    peaks = _read_peaks(peaks_file)
    # The crypto peak is fully preserved.
    assert peaks["BTC/USD"] == {"peak": 8.0, "partialed": True}
    # The live equity survives and its peak advanced (3.0 → 4.0).
    assert abs(peaks["NVDA"]["peak"] - 4.0) < 1e-9
    # A closed equity in the managed class is still pruned.
    assert "AMD" not in peaks


def test_crypto_cycle_does_not_loosen_equity_trailing_stop(L, tmp_path, monkeypatch):
    """End-to-end behavioral proof. An equity is armed at a +10% peak. A crypto-only cycle
    runs in between (as run_crypto does hourly), then the equity gives back to +6.5%. With
    the peak preserved, that 3.5% give-back from the 10% peak TRIPS the trailing stop and
    the equity is sold. Under the old wipe-everything _save_peaks the crypto cycle would
    have erased the 10% peak, the equity peak would rebuild from +6.5%, and NO trail would
    fire — the winner held loose, exactly the loosening this fix prevents."""
    orch = importlib.import_module("scripts.orchestrator")
    peaks_file, orders = _wire(orch, monkeypatch, tmp_path)

    # NVDA armed at a 10% high-water mark; BTC tracked low.
    _seed_peaks(peaks_file, {
        "NVDA":    {"peak": 10.0, "partialed": True},
        "BTC/USD": {"peak":  1.0, "partialed": False},
    })

    # Step A: hourly crypto-only cycle, BTC up a touch → no exit, but must NOT touch NVDA.
    btc = {"symbol": "BTC/USD", "asset_class": "crypto", "qty": "0.1",
           "current_price": "50000", "unrealized_plpc": "0.02",
           "avg_entry_price": "49019"}
    orch._manage_swing_exits([btc])
    assert _read_peaks(peaks_file)["NVDA"]["peak"] == 10.0   # peak survived the crypto cycle

    # Step B: equity-only cycle; NVDA has given back from +10% to +6.5% (a 3.5% give-back).
    nvda = {"symbol": "NVDA", "asset_class": "us_equity", "qty": "10",
            "current_price": "106.5", "unrealized_plpc": "0.065",
            "avg_entry_price": "100"}
    actions, closed = orch._manage_swing_exits([nvda])

    # Trailing stop trips off the PRESERVED 10% peak → equity is sold and closed.
    assert any("TRAIL-STOP NVDA" in a for a in actions), actions
    assert "NVDA" in closed
    assert any(o[1] == "NVDA" and o[3] == "sell" for o in orders)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
