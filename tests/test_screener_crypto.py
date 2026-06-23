"""
Full-crypto-universe discovery — FeintTrade.
Run: python -m pytest tests/test_screener_crypto.py -v

The screener must scan the WHOLE Alpaca tradable crypto universe for daily movers — not
just the ~7 hand-mapped CoinGecko-trending names — so "all security types" are actually
covered. Liquidity is relative (rank by prior-day dollar volume) because the paper feed's
absolute volume magnitudes are unreliable; stablecoin/pegged bases and stale pairs are
filtered out.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import screener


def _snap(price, vol, prev_dvol, chg):
    return {"price": price, "volume": vol, "prev_dollar_volume": prev_dvol,
            "day_change_pct": chg}


def test_crypto_movers_surfaces_liquid_movers_only(monkeypatch):
    snaps = {
        "BTC/USD":  _snap(100000, 5, 1e9, 0.5),    # very liquid, but not a mover
        "AAVE/USD": _snap(250, 10, 5e6, -4.2),     # liquid + real move → surfaced
        "SOL/USD":  _snap(150, 2, 2e7, 5.0),       # liquid + gainer → surfaced
        "PEPE/USD": _snap(1e-5, 0, 300, -8.0),     # huge move but illiquid + no trade today
    }
    monkeypatch.setattr(screener, "_tradable_crypto_usd", lambda: list(snaps))
    monkeypatch.setattr(screener, "_crypto_snapshots", lambda s: snaps)

    movers = screener._crypto_movers(top=8, min_move_pct=3.0, liquidity_top=20)
    syms = [m[0] for m in movers]
    assert "AAVE/USD" in syms and "SOL/USD" in syms
    assert "BTC/USD" not in syms      # liquid but didn't move enough
    assert "PEPE/USD" not in syms     # big move but stale (no volume today) + illiquid
    assert syms[0] == "SOL/USD"       # ranked by ABSOLUTE move (5.0 > 4.2)


def test_crypto_movers_respects_liquidity_top_cutoff(monkeypatch):
    # 30 pairs all moving +10%, but only the most-liquid `liquidity_top` are eligible —
    # an illiquid micro-cap that pumps is NOT surfaced.
    snaps = {f"A{i}/USD": _snap(1.0, 1.0, 1000 * (50 - i), 10.0) for i in range(30)}
    monkeypatch.setattr(screener, "_tradable_crypto_usd", lambda: list(snaps))
    monkeypatch.setattr(screener, "_crypto_snapshots", lambda s: snaps)

    movers = screener._crypto_movers(top=50, min_move_pct=3.0, liquidity_top=5)
    assert len(movers) == 5           # capped at the liquidity window, not all 30


def test_tradable_crypto_usd_excludes_stables_and_non_usd(monkeypatch):
    class _R:
        ok = True
        def json(self):
            return [
                {"symbol": "BTC/USD", "tradable": True},
                {"symbol": "USDT/USD", "tradable": True},   # stablecoin base → excluded
                {"symbol": "ETH/USDC", "tradable": True},   # not /USD → excluded
                {"symbol": "SOL/USD", "tradable": False},   # not tradable → excluded
                {"symbol": "AVAX/USD", "tradable": True},
            ]
    screener._crypto_universe_cache["syms"] = None           # reset per-run cache
    monkeypatch.setattr(screener.requests, "get", lambda *a, **k: _R())
    try:
        assert screener._tradable_crypto_usd() == ["BTC/USD", "AVAX/USD"]
    finally:
        screener._crypto_universe_cache["syms"] = None


def test_discover_includes_crypto_universe_movers(monkeypatch):
    # No equities/trending; only the universe scan produces a crypto candidate.
    monkeypatch.setattr(screener, "_most_actives", lambda *a, **k: [])
    monkeypatch.setattr(screener, "_movers", lambda *a, **k: ([], []))
    monkeypatch.setattr(screener, "_stock_snapshots", lambda s: {})
    monkeypatch.setattr(screener, "_coingecko_trending", lambda: [])
    monkeypatch.setattr(
        screener, "_crypto_movers",
        lambda **k: [("AAVE/USD", _snap(250, 10, 5e6, -4.2))])

    d = screener.discover()
    crypto = [c for c in d["candidates"] if c["type"] == "crypto"]
    assert crypto and crypto[0]["symbol"] == "AAVE/USD"
    assert "faller" in crypto[0]["reason"]


def test_discover_crypto_scan_can_be_disabled(monkeypatch):
    monkeypatch.setattr(screener, "_most_actives", lambda *a, **k: [])
    monkeypatch.setattr(screener, "_movers", lambda *a, **k: ([], []))
    monkeypatch.setattr(screener, "_stock_snapshots", lambda s: {})
    monkeypatch.setattr(screener, "_coingecko_trending", lambda: [])
    called = {"n": 0}
    def _boom(**k):
        called["n"] += 1
        return [("X/USD", _snap(1, 1, 1, 9))]
    monkeypatch.setattr(screener, "_crypto_movers", _boom)
    monkeypatch.setattr(screener, "_cfg",
                        lambda: {"enabled": True, "scan_crypto_universe": False})

    d = screener.discover()
    assert called["n"] == 0                                   # scan skipped when disabled
    assert not [c for c in d["candidates"] if c["type"] == "crypto"]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
