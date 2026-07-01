"""
Discord !status command — FeintTrade.
Run: python -m pytest tests/test_discord_commands.py -v

!status used to show only a position COUNT; it now lists the actual holdings (purchases)
and recent orders (executed approvals/buys). Hermetic: the subprocess `run` wrapper and
open-trades read are stubbed; no network.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import discord_commands as dc


def _fake_run(account, positions, clock, orders):
    def _run(script, *args):
        if script == "research.py" and args[:1] == ("account",):
            return account
        if script == "research.py" and args[:1] == ("positions",):
            return positions
        if script == "trade.py" and args[:1] == ("status",):
            return clock
        if script == "trade.py" and args[:1] == ("orders",):
            return orders
        return {}
    return _run


def test_status_lists_holdings_and_recent_orders(monkeypatch, tmp_path):
    monkeypatch.setattr(dc, "KILL_FLAG", tmp_path / "kill.flag")        # deterministic: clear
    monkeypatch.setattr(dc, "_open_trades", lambda: {"FAS": {"setup_type": "bb_squeeze_breakout"}})
    monkeypatch.setattr(dc, "run", _fake_run(
        account={"equity": "100000", "cash": "60000", "last_equity": "101000"},
        positions=[{"symbol": "FAS", "qty": "6", "current_price": "153.88",
                    "unrealized_pl": "72.05", "unrealized_plpc": "0.085", "avg_entry_price": "141"}],
        clock={"is_open": False},
        orders=[{"side": "buy", "qty": "6", "symbol": "FAS", "status": "filled",
                 "filled_avg_price": "141.00", "limit_price": "141.00"},
                {"side": "sell", "qty": "5", "symbol": "FAS", "status": "filled", "limit_price": "152.23"}],
    ))
    out = dc.cmd_status()
    assert "Open Positions: 1" in out
    assert "Holdings (purchases)" in out
    assert "FAS" in out and "bb_squeeze_breakout" in out          # the purchase + its setup
    assert "+8.5%" in out
    assert "Recent orders" in out
    assert "BUY 6 FAS" in out and "FILLED" in out                  # the executed approval/buy


def test_status_no_positions_shows_cash(monkeypatch, tmp_path):
    monkeypatch.setattr(dc, "KILL_FLAG", tmp_path / "kill.flag")
    monkeypatch.setattr(dc, "run", _fake_run(
        account={"equity": "100000", "cash": "100000", "last_equity": "100000"},
        positions=[], clock={"is_open": True}, orders=[],
    ))
    out = dc.cmd_status()
    assert "Open Positions: 0" in out
    assert "sitting in cash" in out


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
