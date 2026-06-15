"""
Tests for public_data (free no-key market APIs). Hermetic: pure functions get rates
passed in; fetchers are monkeypatched. No network.

Run: python -B -m pytest tests/test_public_data.py -q
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import public_data as pdm


def test_base_symbol_normalization():
    assert pdm._base("BTC/USD") == "BTC"
    assert pdm._base("ETHUSD") == "ETH"
    assert pdm._base("SOL/USDT") == "SOL"
    assert pdm._base("doge/usd") == "DOGE"


def test_usd_strength_index():
    assert pdm.usd_strength(rates=dict(pdm._FX_BASELINE)) == 100.0          # at baseline
    strong = {k: v * 1.1 for k, v in pdm._FX_BASELINE.items()}
    assert pdm.usd_strength(rates=strong) == 110.0                          # 10% stronger USD
    assert pdm.usd_strength(rates={}) is None


def test_macro_brief_bias(monkeypatch):
    assert "risk-OFF" in pdm.macro_brief(strength=105)
    assert "risk-ON" in pdm.macro_brief(strength=96)
    assert "neutral" in pdm.macro_brief(strength=100)
    monkeypatch.setattr(pdm, "usd_strength", lambda rates=None: None)
    assert pdm.macro_brief() == ""          # no data → empty (no live fetch in tests)


def test_crypto_price_falls_back_to_coingecko(monkeypatch):
    monkeypatch.setattr(pdm, "coinbase_price", lambda s: None)
    monkeypatch.setattr(pdm, "coingecko_price", lambda s: 1234.5)
    assert pdm.crypto_price("BTC/USD") == 1234.5


def test_cached_fx_rates_avoids_refetch(monkeypatch, tmp_path):
    """Second call within TTL must read the cache, not re-hit the network."""
    monkeypatch.setattr(pdm, "_CACHE", tmp_path / "fx.json")
    calls = {"n": 0}

    def _fake_fetch(base="USD", quotes=()):
        calls["n"] += 1
        return dict(pdm._FX_BASELINE)

    monkeypatch.setattr(pdm, "fx_rates", _fake_fetch)
    r1 = pdm._cached_fx_rates()
    r2 = pdm._cached_fx_rates()
    assert r1 == r2 == dict(pdm._FX_BASELINE)
    assert calls["n"] == 1                       # only ONE live fetch despite two calls


def test_cached_fx_rates_serves_stale_when_network_down(monkeypatch, tmp_path):
    """If the live fetch fails but a (possibly stale) cache exists, serve it."""
    cache = tmp_path / "fx.json"
    cache.write_text('{"ts": 0, "rates": {"EUR": 0.92}}', encoding="utf-8")  # ts=0 → stale
    monkeypatch.setattr(pdm, "_CACHE", cache)
    monkeypatch.setattr(pdm, "fx_rates", lambda *a, **k: {})                 # network down
    assert pdm._cached_fx_rates() == {"EUR": 0.92}


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
