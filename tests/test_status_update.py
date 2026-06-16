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
                [{"symbol": "FAS"}, {"symbol": "TQQQ"}])
    dn.status_update("cycle")
    assert len(cap) == 1
    mt, embed = cap[0]
    assert mt == "status_update"
    assert "Status" in embed["title"] and "cycle" in embed["title"]
    names = " ".join(f["name"] for f in embed["fields"])
    assert "Portfolio" in names and "Day P&L" in names and "Cash" in names and "Open Positions" in names
    vals = {f["name"]: f["value"] for f in embed["fields"]}
    assert "$95,158.00" in next(v for k, v in vals.items() if "Portfolio" in k)
    assert next(v for k, v in vals.items() if "Open Positions" in k) == "2"
    # Down day (95158 < 95600), not killed -> ORANGE
    assert embed["color"] == dn.ORANGE


def test_status_update_up_day_is_green(monkeypatch, tmp_path):
    cap = _wire(monkeypatch, tmp_path,
                {"equity": "101000", "cash": "60000", "last_equity": "100000"}, [])
    dn.status_update("crypto")
    _, embed = cap[0]
    assert embed["color"] == dn.GREEN
    # no positions -> "0"
    assert next(f["value"] for f in embed["fields"] if "Open Positions" in f["name"]) == "0"


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
