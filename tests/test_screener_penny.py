"""
Tests for the penny-stock manipulation/liquidity guards in screener.discover()
(ported from FeintTrade). Hermetic: all data sources are monkeypatched.

Run: python -m pytest tests/test_screener_penny.py -v
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import screener as s


@pytest.fixture
def patched(monkeypatch):
    cfg = {
        "enabled": True, "max_candidates": 24, "min_dollar_volume": 1_000_000,
        "min_price": 1.0, "max_price": 2000, "default_max_alloc_pct": 12,
        "penny_risk": {"low_price_ceiling": 5.0, "exclude_pump_gain_pct": 60.0,
                       "caution_gain_pct": 25.0, "min_share_volume": 300_000},
    }
    monkeypatch.setattr(s, "_cfg", lambda: cfg)
    monkeypatch.setattr(s, "_watchlist_syms", lambda: set())
    monkeypatch.setattr(s, "_asset_ok", lambda sym: True)
    monkeypatch.setattr(s, "_coingecko_trending", lambda: [])
    monkeypatch.setattr(s, "_most_actives", lambda top=20: ["PUMP", "THIN", "CAUT", "NORM"])
    monkeypatch.setattr(s, "_movers", lambda top=20: ([("PUMP", 80.0), ("CAUT", 30.0), ("NORM", 5.0)], []))
    snaps = {
        "PUMP": {"price": 2.0, "volume": 5_000_000},    # $10M dv, +80% → pump → excluded
        "THIN": {"price": 4.0, "volume": 280_000},      # $1.12M dv, <300k shares → thin → excluded
        "CAUT": {"price": 3.0, "volume": 2_000_000},    # $6M dv, +30% → caution flag, score penalty
        "NORM": {"price": 50.0, "volume": 1_000_000},   # $50M dv, +5% → normal
    }
    monkeypatch.setattr(s, "_stock_snapshots", lambda syms: snaps)
    return cfg


def test_pump_and_thin_pennies_excluded(patched):
    d = s.discover()
    syms = {c["symbol"] for c in d["candidates"]}
    assert "PUMP" not in syms       # parabolic low-priced spike dropped
    assert "THIN" not in syms       # thin share volume at low price dropped
    assert d["filters"]["penny_excluded"] == 2


def test_caution_penny_flagged_and_penalized(patched):
    d = s.discover()
    cands = {c["symbol"]: c for c in d["candidates"]}
    assert "CAUT" in cands and cands["CAUT"]["penny_caution"] is True
    assert "penny pump caution" in cands["CAUT"]["reason"]
    # actives(+2) + gainer(+3) - caution(2) = 3
    assert cands["CAUT"]["score"] == 3


def test_normal_name_unaffected(patched):
    d = s.discover()
    cands = {c["symbol"]: c for c in d["candidates"]}
    assert "NORM" in cands and cands["NORM"]["penny_caution"] is False
    assert cands["NORM"]["score"] == 5   # actives(+2) + gainer(+3)


def test_brief_reports_filtered_count(patched):
    brief = s.get_discovery_brief()
    assert "Filtered 2" in brief and "manipulation risk" in brief


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
