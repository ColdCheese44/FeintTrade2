"""
Per-symbol allocation cap on scale-ins — MindHub Trader.
Run: python -m pytest tests/test_alloc_cap_scalein.py -v

validate_order()'s per-symbol allocation cap used to measure only the NEW order's
value against the cap. With the green scale-in allowance, an equity already near its
max_allocation_pct could take a second near-cap add and reach ~2x the cap — bypassing
a hard constraint (the crypto single/alt caps already projected the combined position;
equities had no equivalent). The fix measures the TOTAL resulting LONG position
(existing exposure + order). Existing SHORT exposure counts as 0 so a buy-to-cover is
never blocked by the long cap.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import trade


def _account(equity=100_000, cash=99_000):
    return {"equity": equity, "cash": cash, "last_equity": equity}


def _equity_pos(symbol, market_value, qty):
    px = market_value / abs(qty) if qty else 0
    return {
        "symbol": symbol, "qty": str(qty), "market_value": str(market_value),
        "current_price": str(px), "avg_entry_price": str(px),
        "unrealized_plpc": "0.0", "asset_class": "us_equity",
    }


def test_scalein_blocked_when_total_exceeds_cap():
    """Held 9% in NVDA; a second 9% add would total 18% > the 10% cap → blocked,
    and the message names the combined (existing + order) projection."""
    pos = [_equity_pos("NVDA", 9_000, qty=90)]   # 9% of 100k
    ok, msg = trade.validate_order(
        "NVDA", 90, "buy", 100, _account(), pos,
        watchlist_limit_pct=10, check_session_dedup=False,
    )
    assert not ok
    assert "18.00%" in msg and "10.0% cap" in msg
    assert "existing $9,000.00 + order $9,000.00" in msg


def test_scalein_allowed_within_remaining_headroom():
    """Held 5%; a 3% add totals 8% < the 10% cap → allowed."""
    pos = [_equity_pos("NVDA", 5_000, qty=50)]
    ok, msg = trade.validate_order(
        "NVDA", 30, "buy", 100, _account(), pos,
        watchlist_limit_pct=10, check_session_dedup=False,
    )
    assert ok, msg


def test_initial_entry_behaviour_unchanged():
    """With no existing position the projection equals the order alone: exactly at the
    cap passes, just over is blocked (and the message keeps the order-only phrasing)."""
    ok_at, _ = trade.validate_order(
        "TQQQ", 100, "buy", 100, _account(), [],
        watchlist_limit_pct=10, check_session_dedup=False,
    )
    assert ok_at                                   # 10% == 10% cap → allowed

    ok_over, msg = trade.validate_order(
        "TQQQ", 110, "buy", 100, _account(), [],
        watchlist_limit_pct=10, check_session_dedup=False,
    )
    assert not ok_over
    assert "order $11,000.00" in msg and "existing" not in msg


def test_buy_to_cover_short_not_blocked_by_long_cap():
    """A symbol held SHORT (negative market_value) contributes 0 to the long cap, so a
    buy (which reduces the short) is not blocked by this check."""
    pos = [_equity_pos("SQQQ", -8_000, qty=-400)]  # short, negative mv
    ok, msg = trade.validate_order(
        "SQQQ", 30, "buy", 100, _account(), pos,
        watchlist_limit_pct=10, check_session_dedup=False,
    )
    assert ok, msg


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
