"""
Dynamic-watchlist analysis merge — FeintTrade.
Run: python -m pytest tests/test_data_universe.py -v

A name the marketwide scanner auto-promotes must be ANALYZED with full indicators, not
just listed in the discovery brief. _data_universe() merges the static watchlist with the
auto-promoted dynamic symbols (bounded by discovery.max_analyzed_dynamic) so gather_*_data
fetches their bars/VWAP/etc.
"""
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def _orch(monkeypatch, active):
    orch = importlib.import_module("scripts.orchestrator")
    import watchlist_manager
    monkeypatch.setattr(watchlist_manager, "active_symbols", lambda: active)
    return orch


def test_data_universe_merges_promoted_names(monkeypatch):
    orch = _orch(monkeypatch, ["DFTX", "AAVE/USD"])
    uni = orch._data_universe()
    by_sym = {s["symbol"]: s for s in uni}
    assert "DFTX" in by_sym and "AAVE/USD" in by_sym
    # Promoted entries are tagged + given the default discovery alloc.
    assert by_sym["DFTX"]["source"] == "auto_discovery"
    assert by_sym["DFTX"]["type"] == "equity"
    assert by_sym["AAVE/USD"]["type"] == "crypto"
    assert by_sym["AAVE/USD"]["max_allocation_pct"] > 0


def test_data_universe_crypto_only_filters(monkeypatch):
    orch = _orch(monkeypatch, ["DFTX", "AAVE/USD"])
    syms = [s["symbol"] for s in orch._data_universe(crypto_only=True)]
    assert "AAVE/USD" in syms and "DFTX" not in syms


def test_data_universe_respects_cap(monkeypatch):
    orch = _orch(monkeypatch, [f"SYM{i}" for i in range(20)])
    promoted = [s for s in orch._data_universe() if s.get("source") == "auto_discovery"]
    cap = orch.load_watchlist().get("discovery", {}).get("max_analyzed_dynamic", 6)
    assert len(promoted) <= cap


def test_data_universe_dedupes_against_static(monkeypatch):
    orch = _orch(monkeypatch, [])
    static_syms = [s["symbol"] for s in orch.load_watchlist()["watchlist"]]
    a_static = static_syms[0]
    orch2 = _orch(monkeypatch, [a_static])     # promote a name already on the static list
    syms = [s["symbol"] for s in orch2._data_universe()]
    assert syms.count(a_static) == 1           # not duplicated


def test_data_universe_survives_manager_failure(monkeypatch):
    orch = importlib.import_module("scripts.orchestrator")
    import watchlist_manager
    def _boom():
        raise RuntimeError("state file unreadable")
    monkeypatch.setattr(watchlist_manager, "active_symbols", _boom)
    # Must fall back to the static watchlist, never crash the data gather.
    uni = orch._data_universe()
    assert uni and all(s.get("source") != "auto_discovery" for s in uni)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
