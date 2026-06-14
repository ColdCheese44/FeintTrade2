"""
Options foundation tests — FeintTrade.
Run: python -m pytest tests/test_options.py -v

Covers the safety-critical options plumbing: OCC-symbol classification, premium-based
validation caps (per-trade / per-underlying / total / cash reserve / disabled), the
options-specific exit rules (+target / -stop / expiry, NOT the -3% swing stop), and the
100x contract multiplier in learning P&L.
"""

import importlib
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import common
import trade
import learning

NVDA_CALL = "NVDA260612C00207500"   # NVDA 2026-06-12 Call $207.50
SPY_PUT = "SPY260612P00733000"      # SPY 2026-06-12 Put $733.00


# ── Classification ─────────────────────────────────────────────────────────────

def test_is_option():
    assert common.is_option(NVDA_CALL) and common.is_option(SPY_PUT)
    for not_opt in ("NVDA", "BTC/USD", "BTCUSD", "TQQQ", "SPY", ""):
        assert not common.is_option(not_opt)


def test_option_parts_and_underlying():
    assert common.option_underlying(NVDA_CALL) == "NVDA"
    assert common.option_underlying("NVDA") is None
    p = common.option_parts(NVDA_CALL)
    assert p["underlying"] == "NVDA" and p["type"] == "call"
    assert abs(p["strike"] - 207.5) < 1e-9 and p["expiry"] == date(2026, 6, 12)
    assert common.option_parts(SPY_PUT)["type"] == "put"


def test_option_dte():
    assert common.option_dte("NVDA") is None
    assert isinstance(common.option_dte(NVDA_CALL), int)


# ── Premium-based validation ────────────────────────────────────────────────────

def _acct(equity=100_000, cash=99_000):
    return {"equity": equity, "cash": cash, "last_equity": equity}


def _opt_pos(sym, market_value, qty=1):
    return {"symbol": sym, "qty": str(qty), "market_value": str(market_value),
            "current_price": "1", "avg_entry_price": "1"}


@pytest.fixture
def opt_cfg(monkeypatch):
    monkeypatch.setenv("APCA_BASE_URL", "https://paper-api.alpaca.markets")  # daily stops off
    cfg = {"enabled": True, "max_total_exposure_pct": 30,
           "max_per_underlying_pct": 10, "max_premium_per_trade": 5000}
    monkeypatch.setattr(trade, "load_options_config", lambda: cfg)
    return cfg


def test_option_buy_validated_under_caps(opt_cfg):
    # 5 contracts @ $3.98 = $1,990 premium: under $5k/trade, 10%/underlying, 30% total.
    ok, msg = trade.validate_order(NVDA_CALL, 5, "buy", 3.98, _acct(), [], check_session_dedup=False)
    assert ok, msg
    assert "premium" in msg.lower()


def test_option_per_trade_premium_cap(opt_cfg):
    # 20 @ $3.98 = $7,960 > $5,000 per-trade cap.
    ok, msg = trade.validate_order(NVDA_CALL, 20, "buy", 3.98, _acct(), [], check_session_dedup=False)
    assert not ok and "per-trade" in msg


def test_option_per_underlying_cap(opt_cfg):
    # $9,000 NVDA option premium already held + $1,990 -> $10,990 > 10% ($10,000).
    pos = [_opt_pos("NVDA260612C00210000", 9_000, qty=30)]
    ok, msg = trade.validate_order(NVDA_CALL, 5, "buy", 3.98, _acct(), pos, check_session_dedup=False)
    assert not ok and "per-underlying" in msg


def test_option_total_exposure_cap(opt_cfg):
    # $29,000 in SPY options (different underlying, so per-underlying for NVDA is fine) +
    # $1,990 -> $30,990 > 30% ($30,000) total.
    pos = [_opt_pos("SPY260612C00740000", 29_000, qty=80)]
    ok, msg = trade.validate_order(NVDA_CALL, 5, "buy", 3.98, _acct(), pos, check_session_dedup=False)
    assert not ok and "total options" in msg.lower()


def test_option_buy_blocked_when_disabled(monkeypatch):
    monkeypatch.setenv("APCA_BASE_URL", "https://paper-api.alpaca.markets")
    monkeypatch.setattr(trade, "load_options_config", lambda: {"enabled": False})
    ok, msg = trade.validate_order(NVDA_CALL, 1, "buy", 3.98, _acct(), [], check_session_dedup=False)
    assert not ok and "disabled" in msg.lower()


# ── Options-specific exits ──────────────────────────────────────────────────────

def test_option_exit_decision(monkeypatch):
    orch = importlib.import_module("scripts.orchestrator")
    monkeypatch.setattr(orch, "load_options_config",
                        lambda: {"profit_target_pct": 100, "stop_loss_pct": -50, "close_at_dte": 1})
    monkeypatch.setattr(orch, "option_dte", lambda s: 5)   # plenty of room
    assert orch._option_exit_decision(NVDA_CALL, 120)[0] == "target"
    assert orch._option_exit_decision(NVDA_CALL, -60)[0] == "stop"
    assert orch._option_exit_decision(NVDA_CALL, 30)[0] is None
    # A -10% wiggle that would trip the equity -3% swing stop must NOT exit an option.
    assert orch._option_exit_decision(NVDA_CALL, -10)[0] is None
    # Expiry override: <= close_at_dte forces a close regardless of P&L.
    monkeypatch.setattr(orch, "option_dte", lambda s: 1)
    assert orch._option_exit_decision(NVDA_CALL, 30)[0] == "stop"


# ── 100x contract multiplier in P&L ─────────────────────────────────────────────

def test_log_exit_option_dollar_multiplier(tmp_path, monkeypatch):
    monkeypatch.setattr(learning, "OPEN_TRADES", tmp_path / "ot.json")
    monkeypatch.setattr(learning, "TRADE_LOG", tmp_path / "tl.jsonl")
    monkeypatch.setattr(learning, "PERF_CACHE", tmp_path / "pf.json")
    learning.log_entry(NVDA_CALL, "buy", 2, 3.00, setup_type="options")
    t = learning.log_exit(NVDA_CALL, 5.00, "take_profit", qty=2)
    # (5-3) premium/share x 2 contracts x 100 = $400 dollar P&L; +66.7% return.
    assert abs(t["pnl_dollar"] - 400.0) < 1e-6
    assert abs(t["pnl_pct"] - 66.667) < 0.05
    assert t["asset_type"] == "options"


# ── Regression: options-brief weekday gate must use a datetime, not the string clock ──

def test_now_mt_dt_supports_weekday():
    """
    The weekday options-brief gate in orchestrator._load_context() calls
    now_mt_dt().weekday(). A regression where it used the journal-formatting now_mt()
    (which returns a 'YYYY-MM-DD HH:MM MDT' string) raised
    "'str' object has no attribute 'weekday'" on EVERY weekday cycle, silently
    disabling options end-to-end. Pin the contract so it can't quietly come back:
    now_mt() -> str (formatter), now_mt_dt() -> datetime exposing .weekday().
    """
    orch = importlib.import_module("scripts.orchestrator")
    assert isinstance(orch.now_mt(), str)              # journal/log formatter
    dt = orch.now_mt_dt()
    assert isinstance(dt, datetime)                    # real datetime for time math
    assert 0 <= dt.weekday() <= 6                      # the call that used to throw


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
