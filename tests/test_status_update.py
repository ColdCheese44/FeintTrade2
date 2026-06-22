"""
Command-post status snapshot — FeintTrade.
Run: python -m pytest tests/test_status_update.py -v

discord_notify.status_update() posts the !status info (equity, day P&L, cash, positions,
market/kill state) to #ft-command-post after every routine. Hermetic: transport + Alpaca
fetchers are stubbed, kill-flag dir is redirected to tmp.
"""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import discord_notify as dn


def _wire(monkeypatch, tmp_path, account, positions, enabled=True):
    cap = []
    monkeypatch.setattr(dn, "dch", types.SimpleNamespace(
        post=lambda mt, embed=None, dedup_key=None: cap.append((mt, embed)),
        post_file=lambda *a, **k: True))
    monkeypatch.setattr(dn, "_fetch_account", lambda: account)
    monkeypatch.setattr(dn, "_fetch_positions", lambda: positions)
    monkeypatch.setattr(dn, "_status_updates_enabled", lambda: enabled)
    monkeypatch.setattr(dn, "_ROOT", tmp_path)        # no kill.flag here -> deterministic
    return cap


def test_status_update_posts_status_fields(monkeypatch, tmp_path):
    cap = _wire(monkeypatch, tmp_path,
                {"equity": "95158", "cash": "51000", "last_equity": "95600"},
                [{"symbol": "FAS", "qty": "6", "current_price": "150", "unrealized_plpc": "0.05"},
                 {"symbol": "TQQQ", "qty": "10", "current_price": "78", "unrealized_plpc": "-0.02"}])
    dn.status_update("cycle")
    assert len(cap) == 1
    mt, embed = cap[0]
    assert mt == "status_update"
    assert "Status" in embed["title"] and "cycle" in embed["title"]
    names = " ".join(f["name"] for f in embed["fields"])
    assert "Portfolio" in names and "Day P&L" in names and "Cash" in names and "Holdings" in names
    vals = {f["name"]: f["value"] for f in embed["fields"]}
    assert "$95,158.00" in next(v for k, v in vals.items() if "Portfolio" in k)
    # Holdings field lists the actual positions (not just a count), named "Holdings (2) …"
    held_name, held_val = next((k, v) for k, v in vals.items() if "Holdings" in k)
    assert "(2)" in held_name
    assert "FAS" in held_val and "TQQQ" in held_val
    # Down day (95158 < 95600), not killed -> ORANGE
    assert embed["color"] == dn.ORANGE


def test_status_update_includes_failure_note(monkeypatch, tmp_path):
    """On a routine error the __main__ finally-block passes a note; it must appear in the
    posted snapshot (item #6: status pulse on every routine, including failures)."""
    cap = _wire(monkeypatch, tmp_path,
                {"equity": "95158", "cash": "51000", "last_equity": "95600"}, [])
    dn.status_update("cycle", note="⚠️ routine ERRORED this run — see #ft-dev-log")
    _, embed = cap[0]
    assert "routine ERRORED" in embed["description"]


def test_status_update_up_day_is_green(monkeypatch, tmp_path):
    cap = _wire(monkeypatch, tmp_path,
                {"equity": "101000", "cash": "60000", "last_equity": "100000"}, [])
    dn.status_update("crypto")
    _, embed = cap[0]
    assert embed["color"] == dn.GREEN
    assert "Portfolio Status" in embed["title"] and "after crypto" in embed["title"]
    assert "Crypto holdings: 0" in embed["description"]
    assert "full account snapshot" in embed["description"]
    # no positions -> Holdings (0) with the cash message
    held = next(f for f in embed["fields"] if "Holdings" in f["name"])
    assert "(0)" in held["name"] and "cash" in held["value"].lower()


def test_crypto_status_counts_only_crypto_holdings(monkeypatch, tmp_path):
    cap = _wire(
        monkeypatch,
        tmp_path,
        {"equity": "100000", "cash": "50000", "last_equity": "100000"},
        [
            {"symbol": "AMD", "asset_class": "us_equity"},
            {"symbol": "BTCUSD", "asset_class": "crypto"},
        ],
    )
    dn.status_update("crypto")
    _, embed = cap[0]
    assert "Crypto holdings: 1" in embed["description"]


def test_status_card_shows_holiday_when_clock_closed(monkeypatch, tmp_path):
    """During would-be regular hours, if the broker clock says equities are CLOSED (a
    holiday like Juneteenth that the time-based market_phase mislabels REGULAR), the card
    must say 'Market Closed (holiday)', not 'Market Open'."""
    cap = _wire(monkeypatch, tmp_path,
                {"equity": "92043", "cash": "63819", "last_equity": "92043"}, [])
    monkeypatch.setattr(dn, "_status_updates_enabled", lambda: True)
    import common
    monkeypatch.setattr(common, "market_phase", lambda: "REGULAR")
    import trade
    monkeypatch.setattr(trade, "equities_open_now", lambda *a, **k: False)
    dn.status_update("cycle")
    _, embed = cap[0]
    assert "Market Closed (holiday)" in embed["description"]
    assert "Market Open" not in embed["description"]


def test_equities_open_now_caches_and_fails_open(monkeypatch):
    """trade.equities_open_now(): one clock call per TTL window; fail-open if unreachable."""
    monkeypatch.undo()                       # drop the conftest pin to test the real fn
    import trade
    trade._CLOCK_CACHE.update(ts=0.0, val=None)
    calls = []
    monkeypatch.setattr(trade, "get_market_status", lambda: (calls.append(1) or {"is_open": False}))
    assert trade.equities_open_now() is False
    assert trade.equities_open_now() is False
    assert len(calls) == 1                    # second call served from cache
    trade._CLOCK_CACHE.update(ts=0.0, val=None)

    def _boom():
        raise RuntimeError("network")
    monkeypatch.setattr(trade, "get_market_status", _boom)
    assert trade.equities_open_now(default=True) is True   # fail-open, no cache


def test_status_update_respects_disable_flag(monkeypatch, tmp_path):
    cap = _wire(monkeypatch, tmp_path, {"equity": "100"}, [], enabled=False)
    dn.status_update("cycle")
    assert cap == []


def test_status_update_passed_account_skips_fetch(monkeypatch, tmp_path):
    cap = _wire(monkeypatch, tmp_path, {"equity": "0"}, [])  # fetcher would return equity 0

    def _boom():
        raise AssertionError("should not fetch when account is passed")
    monkeypatch.setattr(dn, "_fetch_account", _boom)

    dn.status_update("eod", account={"equity": "200000", "cash": "100000", "last_equity": "190000"},
                     positions=[{"symbol": "BTC/USD"}])
    _, embed = cap[0]
    assert "$200,000.00" in next(f["value"] for f in embed["fields"] if "Portfolio" in f["name"])


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
