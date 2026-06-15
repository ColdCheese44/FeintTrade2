"""
live_account.min_order_usd enforcement — FeintTrade.
Run: python -m pytest tests/test_min_order_usd.py -v

validate_order() receives the FINAL (regime × live_scale)-scaled notional from
_execute_orders, so a live-sim order scaled below live_account.min_order_usd is rejected
(skipped, never rounded up) here. Sells are never blocked. Missing config => no floor.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import trade


def _account(equity=100_000, cash=99_000):
    return {"equity": equity, "cash": cash, "last_equity": equity}


@pytest.fixture(autouse=True)
def _no_daily_stop(monkeypatch):
    """Isolate from any real daily-stop state so we test only the min-order gate."""
    monkeypatch.setattr(trade, "get_daily_state", lambda: {})


def test_buy_below_min_order_usd_skipped(monkeypatch):
    monkeypatch.setattr(trade, "load_live_account", lambda: {"min_order_usd": 10.0})
    ok, msg = trade.validate_order("NVDA", 0.05, "buy", 100, _account(), [],
                                   watchlist_limit_pct=30, check_session_dedup=False)
    assert not ok
    assert "min_order_usd" in msg and "below" in msg.lower()
    assert "$5.00" in msg            # actual notional appears


def test_buy_equal_to_min_allowed(monkeypatch):
    monkeypatch.setattr(trade, "load_live_account", lambda: {"min_order_usd": 10.0})
    ok, msg = trade.validate_order("NVDA", 0.1, "buy", 100, _account(), [],
                                   watchlist_limit_pct=30, check_session_dedup=False)
    assert ok, msg            # $10.00 == $10.00 minimum -> allowed


def test_buy_above_min_allowed(monkeypatch):
    monkeypatch.setattr(trade, "load_live_account", lambda: {"min_order_usd": 10.0})
    ok, msg = trade.validate_order("NVDA", 0.5, "buy", 100, _account(), [],
                                   watchlist_limit_pct=30, check_session_dedup=False)
    assert ok, msg            # $50.00 -> allowed


def test_missing_min_order_usd_does_not_block(monkeypatch):
    """No min_order_usd key -> no floor -> a tiny order still validates (back-compat)."""
    monkeypatch.setattr(trade, "load_live_account", lambda: {})
    ok, msg = trade.validate_order("NVDA", 0.05, "buy", 100, _account(), [],
                                   watchlist_limit_pct=30, check_session_dedup=False)
    assert ok, msg


def test_min_order_usd_never_blocks_a_sell(monkeypatch):
    """De-risking is sacred: a tiny sell is allowed even under a huge min_order_usd."""
    monkeypatch.setattr(trade, "load_live_account", lambda: {"min_order_usd": 1_000.0})
    pos = [{"symbol": "NVDA", "qty": "10", "market_value": "950",
            "current_price": "95", "asset_class": "us_equity"}]
    ok, msg = trade.validate_order("NVDA", 1, "sell", 95, _account(), pos)
    assert ok, msg


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
