"""
Concentration + regime hard-rule enforcement — FeintTrade.
Run: python -m pytest tests/test_concentration_and_regime.py -v

Two documented HARD RULES were never actually enforced in code:

  #9  Correlation cap: "Never hold more than 3 positions in the same sector" — the
      risk block even claims it's enforced, but no code read max_same_sector_positions.

  Regime rule: "Never buy leveraged long ETFs in BEAR or PANIC regime" — this lived
      only in the prompt, so the model could buy TQQQ into a downtrend with no backstop.

These tests pin both. The sector cap counts only same-direction LONG positions; inverse/
hedge ETFs and crypto are unmapped, so a risk-off posture (SQQQ/SOXS) is never throttled.
"""

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import trade
from common import sector_for


@pytest.fixture(autouse=True)
def paper_env(monkeypatch):
    # Paper endpoint → daily stops are advisory/disabled, so they can't mask the checks.
    monkeypatch.setenv("APCA_BASE_URL", "https://paper-api.alpaca.markets")


def _account(equity=100_000, cash=95_000):
    return {"equity": equity, "cash": cash, "last_equity": equity}


def _pos(symbol, market_value, qty, asset_class=None):
    px = market_value / abs(qty) if qty else 0
    p = {"symbol": symbol, "qty": str(qty), "market_value": str(market_value),
         "current_price": str(px), "avg_entry_price": str(px), "unrealized_plpc": "0.0"}
    if asset_class:
        p["asset_class"] = asset_class
    return p


# ── Sector taxonomy sanity ────────────────────────────────────────────────────

def test_sector_map_classification():
    assert sector_for("NVDA") == "tech" and sector_for("TQQQ") == "tech"
    assert sector_for("FAS") == "financials" and sector_for("LABU") == "biotech"
    # Inverse/hedge ETFs and crypto are intentionally unmapped (never throttled).
    assert sector_for("SQQQ") is None and sector_for("SOXS") is None and sector_for("UVXY") is None
    assert sector_for("BTC/USD") is None and sector_for("BTCUSD") is None


# ── HARD RULE #9: same-sector concentration cap ───────────────────────────────

def _three_tech():
    return [_pos("NVDA", 10_000, 100), _pos("AMD", 8_000, 100), _pos("TQQQ", 12_000, 60)]


# Pin the cap explicitly so these mechanism tests are independent of the tunable
# live config (risk.max_same_sector_positions, which the aggressive profile set to 4).
_CAP3 = {"max_same_sector_positions": 3}


def test_fourth_same_sector_long_blocked():
    ok, msg = trade.validate_order(
        "PLTR", 10, "buy", 50, _account(), _three_tech(),
        watchlist_limit_pct=20, risk=_CAP3, check_session_dedup=False,
    )
    assert not ok
    assert "same-sector" in msg.lower() and "tech" in msg
    assert "NVDA" in msg and "AMD" in msg and "TQQQ" in msg


def test_third_same_sector_long_allowed():
    two_tech = [_pos("NVDA", 10_000, 100), _pos("AMD", 8_000, 100)]
    ok, msg = trade.validate_order(
        "TQQQ", 5, "buy", 60, _account(), two_tech,
        watchlist_limit_pct=40, risk=_CAP3, check_session_dedup=False,
    )
    assert ok, msg


def test_scale_in_to_held_sector_name_allowed():
    """Adding to a name already held doesn't open a new position, so the count cap
    must not block it (only allocation/other caps may)."""
    ok, msg = trade.validate_order(
        "NVDA", 1, "buy", 100, _account(), _three_tech(),
        watchlist_limit_pct=30, check_session_dedup=False,
    )
    assert ok, msg


def test_inverse_etf_never_blocked_by_sector_cap():
    """A risk-off SQQQ buy alongside 3 tech longs must NOT be blocked by the sector cap
    (inverse ETFs are unmapped — they offset, not stack)."""
    ok, msg = trade.validate_order(
        "SQQQ", 10, "buy", 20, _account(), _three_tech(),
        watchlist_limit_pct=25, check_session_dedup=False,
    )
    assert ok, msg


def test_crypto_unaffected_by_sector_cap():
    ok, msg = trade.validate_order(
        "BTC/USD", 0.001, "buy", 50_000, _account(), _three_tech(),
        watchlist_limit_pct=35, check_session_dedup=False, completed_trades=50,
    )
    assert ok, msg


# ── Regime rule: no leveraged LONG ETFs in BEAR/PANIC ─────────────────────────

def test_regime_blocks_leveraged_long(monkeypatch):
    orch = importlib.import_module("scripts.orchestrator")
    monkeypatch.setattr(orch, "load_watchlist", lambda: {"watchlist": [
        {"symbol": "TQQQ", "type": "leveraged_etf"},
        {"symbol": "SOXL", "type": "leveraged_etf"},
        {"symbol": "NVDA", "type": "equity"},
        {"symbol": "SQQQ", "type": "inverse_etf"},
    ]})

    assert orch._regime_blocks_leveraged_long("TQQQ", "BEAR") is True
    assert orch._regime_blocks_leveraged_long("TQQQ", "PANIC") is True
    assert orch._regime_blocks_leveraged_long("SOXL", "bear") is True   # case-insensitive
    # Permitted regimes:
    assert orch._regime_blocks_leveraged_long("TQQQ", "BULL") is False
    assert orch._regime_blocks_leveraged_long("TQQQ", "NEUTRAL") is False
    # Not a leveraged long → never blocked by this rule:
    assert orch._regime_blocks_leveraged_long("NVDA", "BEAR") is False   # plain equity
    assert orch._regime_blocks_leveraged_long("SQQQ", "PANIC") is False  # inverse ETF (the right downside tool)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
