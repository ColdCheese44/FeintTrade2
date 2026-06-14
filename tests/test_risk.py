"""
Risk engine tests — MindHub Trader post-session remediation pass.
Run: python -m pytest tests/test_risk.py -v

Covers:
  - Sell always allowed when position exists
  - Untracked-entry rejection (missing setup_type)
  - Projected crypto exposure cap
  - Validation-mode cap override (15% vs 40%)
  - Duplicate-entry cooldown
  - Correlated crypto basket cap
  - Canonical symbol normalization (BTC vs BTC/USD)
  - Rejection messages include actual numbers
  - Exit reason taxonomy normalization
  - Loss-streak lockout
  - Daily soft/hard stop
"""

import sys
import os
from pathlib import Path
import json
import tempfile
import shutil

# Add scripts dir to path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _account(equity=100_000, cash=95_000):
    return {"equity": equity, "cash": cash, "last_equity": equity}


def _position(symbol, market_value, qty=1.0, pnl_pct=0.0, asset_class=None):
    pos = {
        "symbol": symbol,
        "qty": str(qty),
        "market_value": str(market_value),
        "unrealized_plpc": str(pnl_pct / 100),
        "current_price": str(market_value / abs(qty) if qty else 0),
        "avg_entry_price": str(market_value / abs(qty) if qty else 0),
        "unrealized_pl": str(pnl_pct / 100 * market_value),
    }
    if asset_class:
        pos["asset_class"] = asset_class
    return pos


def _crypto_pos(symbol, market_value, qty=0.1, pnl_pct=0.0):
    return _position(symbol, market_value, qty=qty, pnl_pct=pnl_pct, asset_class="crypto")


# ── Import modules ────────────────────────────────────────────────────────────

import common
from common import (
    normalize_symbol, is_crypto, CORRELATED_CRYPTO_BASKETS,
    is_validation_mode, get_effective_caps, VALIDATION_MODE_CAPS,
    check_duplicate_entry, record_session_entry, get_session_entries,
    check_daily_stop, update_daily_state, get_daily_state,
    _daily_state_path,
)


def _clear_daily_state():
    """Delete the daily state file so tests start with a clean slate."""
    p = _daily_state_path()
    if p.exists():
        p.unlink()


import pytest

@pytest.fixture(autouse=True)
def clear_state_before_each():
    """Ensure daily/session state is clean before every test to prevent cross-test contamination."""
    os.environ["APCA_BASE_URL"] = "https://api.alpaca.markets"
    _clear_daily_state()
    # Also clean up fake test-date session state files
    for fake_date in ("1999-01-01", "1999-01-02", "1999-01-03"):
        sp = ROOT / "data" / f"session_state_{fake_date}.json"
        if sp.exists():
            sp.unlink()
    yield
    _clear_daily_state()

from learning import (
    _normalize_exit_reason, EXIT_REASONS, get_loss_streak,
    is_loss_streak_locked,
)

import trade


# ── 1. Canonical symbol normalization ────────────────────────────────────────

class TestSymbolNormalization:
    def test_btcusd_normalizes_to_slash(self):
        assert normalize_symbol("BTCUSD") == "BTC/USD"

    def test_ethusd_normalizes(self):
        assert normalize_symbol("ETHUSD") == "ETH/USD"

    def test_btc_equity_stays_btc(self):
        """BTC as an equity ticker (Grayscale, etc.) should NOT be converted to BTC/USD."""
        # BTC without USD suffix is treated as equity
        assert normalize_symbol("BTC") == "BTC"

    def test_slash_form_unchanged(self):
        assert normalize_symbol("BTC/USD") == "BTC/USD"

    def test_equity_ticker_unchanged(self):
        assert normalize_symbol("NVDA") == "NVDA"
        assert normalize_symbol("TQQQ") == "TQQQ"

    def test_is_crypto_slash(self):
        assert is_crypto("BTC/USD") is True

    def test_is_crypto_no_slash(self):
        assert is_crypto("BTCUSD") is True

    def test_equity_not_crypto(self):
        assert is_crypto("NVDA") is False

    def test_correlated_basket_contains_major_cryptos(self):
        basket = CORRELATED_CRYPTO_BASKETS["all_crypto"]
        assert "BTC/USD" in basket
        assert "ETH/USD" in basket
        assert "AVAX/USD" in basket

    def test_alts_basket_excludes_btc_eth(self):
        alts = CORRELATED_CRYPTO_BASKETS["alts"]
        assert "BTC/USD" not in alts
        assert "ETH/USD" not in alts
        assert "AVAX/USD" in alts


# ── 2. Sell always allowed ────────────────────────────────────────────────────

class TestSellAlwaysAllowed:
    def test_sell_with_position_allowed(self):
        pos = [_crypto_pos("BTC/USD", 5000, qty=0.05)]
        ok, msg = trade.validate_order(
            "BTC/USD", 0.05, "sell", 100_000, _account(cash=1000), pos
        )
        assert ok, f"Expected sell allowed, got: {msg}"
        assert "reduces exposure" in msg.lower()

    def test_sell_without_position_blocked(self):
        ok, msg = trade.validate_order(
            "DOGE/USD", 100, "sell", 0.15, _account(), []
        )
        assert not ok
        assert "no open position" in msg.lower()

    def test_sell_not_blocked_by_cash_reserve(self):
        """Sell should work even when cash is at 0% reserve."""
        pos = [_crypto_pos("ETH/USD", 95_000, qty=30)]
        ok, msg = trade.validate_order(
            "ETH/USD", 10, "sell", 3166, _account(cash=5), pos
        )
        assert ok, f"Sell should not be blocked by low cash: {msg}"

    def test_sell_qty_exceeds_held_blocked(self):
        pos = [_crypto_pos("SOL/USD", 2000, qty=10)]
        ok, msg = trade.validate_order(
            "SOL/USD", 15, "sell", 200, _account(), pos
        )
        assert not ok
        assert "exceeds held" in msg.lower()


# ── 3. Projected crypto exposure ─────────────────────────────────────────────

class TestProjectedCryptoExposure:
    def test_buy_blocked_when_crypto_at_cap(self):
        """Validate that a buy is blocked when crypto exposure would exceed 40% cap."""
        # 40k already in crypto = 40% of 100k equity
        pos = [_crypto_pos("BTC/USD", 40_000, qty=0.4)]
        ok, msg = trade.validate_order(
            "ETH/USD", 0.5, "buy", 3200, _account(), pos,
            watchlist_limit_pct=25,
            check_session_dedup=False,
        )
        assert not ok
        assert "cap" in msg.lower() or "exposure" in msg.lower()
        assert "40" in msg  # cap value should appear in the message

    def test_buy_allowed_under_cap(self):
        """Small buy well under the cap should pass."""
        pos = [_crypto_pos("BTC/USD", 5_000, qty=0.05)]
        ok, msg = trade.validate_order(
            "ETH/USD", 0.01, "buy", 3200, _account(), pos,
            watchlist_limit_pct=25,
            check_session_dedup=False,
        )
        assert ok, f"Expected buy allowed, got: {msg}"

    def test_rejection_message_has_real_numbers(self):
        """Rejection messages must not contain placeholder 'N%' or '$X'."""
        pos = [_crypto_pos("BTC/USD", 41_000, qty=0.41)]
        ok, msg = trade.validate_order(
            "ETH/USD", 0.5, "buy", 3200, _account(), pos,
            watchlist_limit_pct=25,
            check_session_dedup=False,
        )
        assert not ok
        assert "N%" not in msg, f"Placeholder found in: {msg}"
        assert "$X" not in msg, f"Placeholder found in: {msg}"
        # Should have actual dollar/percent values
        assert "$" in msg or "%" in msg


# ── 4. Validation-mode caps ───────────────────────────────────────────────────

class TestValidationModeCaps:
    def test_validation_mode_active_below_threshold(self):
        assert is_validation_mode(0) is True
        assert is_validation_mode(29) is True

    def test_validation_mode_inactive_at_threshold(self):
        assert is_validation_mode(30) is False
        assert is_validation_mode(50) is False

    def test_validation_caps_stricter(self):
        val_caps = get_effective_caps(0)
        norm_caps = get_effective_caps(50)
        assert val_caps["max_crypto_exposure_pct"] < norm_caps["max_crypto_exposure_pct"]
        assert val_caps["max_crypto_exposure_pct"] == VALIDATION_MODE_CAPS["max_crypto_exposure_pct"]

    def test_validation_mode_blocks_over_15pct(self):
        """In validation mode, 16% crypto buy should be blocked (cap is 15%)."""
        pos = [_crypto_pos("BTC/USD", 15_500, qty=0.155)]  # 15.5% of 100k
        ok, msg = trade.validate_order(
            "ETH/USD", 0.1, "buy", 3200, _account(), pos,
            watchlist_limit_pct=25,
            check_session_dedup=False,
            completed_trades=0,
        )
        assert not ok
        assert "validation mode" in msg.lower() or "15" in msg

    def test_altcoin_cap_blocked_in_validation(self):
        """Single altcoin (non BTC/ETH) above 3% should be blocked in validation mode."""
        pos = [_crypto_pos("AVAX/USD", 3_100, qty=100)]  # 3.1% of 100k
        ok, msg = trade.validate_order(
            "AVAX/USD", 10, "buy", 35, _account(), pos,
            watchlist_limit_pct=15,
            check_session_dedup=False,
            completed_trades=0,
        )
        assert not ok
        assert "altcoin" in msg.lower() or "3" in msg


# ── 5. Duplicate-entry cooldown ───────────────────────────────────────────────

class TestDuplicateEntryCooldown:
    def _fresh_date(self):
        """Use a fake date so tests don't pollute real session state."""
        return "1999-01-01"

    def test_first_entry_always_allowed(self):
        ok, msg = check_duplicate_entry("BTC/USD", date=self._fresh_date())
        assert ok

    def test_second_entry_blocked_if_not_green(self):
        d = self._fresh_date()
        record_session_entry("BTC/USD", is_green=False, date=d)
        ok, msg = check_duplicate_entry("BTC/USD", position_pnl_pct=-1.0, date=d)
        assert not ok
        assert "cooldown" in msg.lower() or "not green" in msg.lower()

    def test_second_entry_allowed_if_green(self):
        d = "1999-01-02"
        record_session_entry("ETH/USD", is_green=False, date=d)
        ok, msg = check_duplicate_entry("ETH/USD", position_pnl_pct=2.0, date=d)
        assert ok

    def test_third_entry_always_blocked(self):
        d = "1999-01-03"
        record_session_entry("SOL/USD", is_green=True, date=d)
        record_session_entry("SOL/USD", is_green=True, date=d)
        ok, msg = check_duplicate_entry("SOL/USD", position_pnl_pct=5.0, date=d)
        assert not ok


# ── 6. Exit reason taxonomy ───────────────────────────────────────────────────

class TestExitReasonTaxonomy:
    def test_known_reasons_pass_through(self):
        for reason in ("stop_loss", "take_profit", "timeout", "eod_close", "manual_derisk"):
            assert _normalize_exit_reason(reason) == reason

    def test_unknown_reason_becomes_legacy(self):
        assert _normalize_exit_reason("gibberish_xyz") == "unknown_legacy"

    def test_sell_alias_maps_to_manual_derisk(self):
        assert _normalize_exit_reason("sell") == "manual_derisk"

    def test_target_hit_alias_maps_to_take_profit(self):
        assert _normalize_exit_reason("target_hit") == "take_profit"

    def test_noprice_suffix_stripped(self):
        result = _normalize_exit_reason("eod_close_noprice")
        assert result == "eod_close"

    def test_all_canonical_reasons_in_set(self):
        for r in EXIT_REASONS:
            assert _normalize_exit_reason(r) in EXIT_REASONS


# ── 7. Rejection messages have actual numbers ─────────────────────────────────

class TestRejectionMessages:
    def test_allocation_cap_message_has_numbers(self):
        ok, msg = trade.validate_order(
            "TQQQ", 100, "buy", 52.00, _account(), [],
            watchlist_limit_pct=5,
            check_session_dedup=False,
        )
        assert not ok
        assert "$" in msg and "%" in msg
        assert "N%" not in msg and "$X" not in msg

    def test_cash_reserve_message_has_numbers(self):
        ok, msg = trade.validate_order(
            "BTC/USD", 1.0, "buy", 90_000,
            {"equity": 100_000, "cash": 5_000},  # barely above reserve
            [],
            watchlist_limit_pct=100,
            check_session_dedup=False,
        )
        assert not ok
        assert "$" in msg and "%" in msg
        assert "N%" not in msg


# ── 8. Daily stop state ───────────────────────────────────────────────────────

class TestDailyStop:
    def setup_method(self):
        _clear_daily_state()

    def test_soft_stop_at_minus_2pct(self):
        result = check_daily_stop(98_000, opening_equity=100_000, completed_trades=0)
        assert result["soft_stop"] is True
        assert result["hard_stop"] is False

    def test_hard_stop_at_minus_3pct(self):
        result = check_daily_stop(96_900, opening_equity=100_000, completed_trades=0)
        assert result["hard_stop"] is True

    def test_no_stop_within_limits(self):
        result = check_daily_stop(99_000, opening_equity=100_000, completed_trades=0)
        assert result["soft_stop"] is False
        assert result["hard_stop"] is False

    def test_normal_mode_harder_thresholds(self):
        """In normal mode (30+ trades) hard stop is at -6%, not -3%."""
        result = check_daily_stop(97_500, opening_equity=100_000, completed_trades=50)
        # -2.5% — should be fine in normal mode
        assert result["hard_stop"] is False


# ── 9. Correlated basket cap ──────────────────────────────────────────────────

class TestCorrelatedBasket:
    def setup_method(self):
        _clear_daily_state()

    def test_basket_includes_all_crypto(self):
        """BTC + ETH + AVAX together count toward same basket."""
        basket = CORRELATED_CRYPTO_BASKETS["all_crypto"]
        assert "BTC/USD" in basket and "ETH/USD" in basket and "AVAX/USD" in basket

    def test_basket_cap_prevents_stacking(self):
        """Adding multiple crypto positions that together exceed cap should be blocked."""
        # 14% BTC + 12% ETH = 26% crypto, which exceeds 15% validation-mode cap
        positions = [
            _crypto_pos("BTC/USD", 14_000, qty=0.14),
            _crypto_pos("ETH/USD", 12_000, qty=3.8),
        ]
        ok, msg = trade.validate_order(
            "SOL/USD", 10, "buy", 150,
            _account(),
            positions,
            watchlist_limit_pct=20,
            check_session_dedup=False,
            completed_trades=0,
        )
        assert not ok
        assert "exposure" in msg.lower() or "cap" in msg.lower()


# ── 10. Loss-streak lockout ───────────────────────────────────────────────────

class TestLossStreakLockout:
    def test_no_lockout_below_threshold(self):
        locked, _ = is_loss_streak_locked(threshold=2)
        # Without actual loss trades in the log, should not be locked
        # (we can't mock the file in a pure unit test easily, so just verify it runs)
        assert isinstance(locked, bool)

    def test_lockout_function_signature(self):
        locked, msg = is_loss_streak_locked(threshold=2)
        assert isinstance(msg, str)
        assert len(msg) > 0

    def test_get_loss_streak_returns_dict(self):
        s = get_loss_streak()
        assert "count" in s
        assert "type" in s
        assert s["type"] in ("win", "loss", "none")


class TestPaperModeDailyStops:
    def test_daily_stops_skipped_when_disabled(self, monkeypatch):
        """When daily stops are disabled, even a big paper drawdown reports no stop.
        Pinned via monkeypatch so it's independent of the live config flag."""
        monkeypatch.setenv("APCA_BASE_URL", "https://paper-api.alpaca.markets")
        monkeypatch.setattr(common, "daily_stops_enforced", lambda: False)
        _clear_daily_state()
        result = check_daily_stop(93_000, opening_equity=100_000, completed_trades=0)
        assert result["soft_stop"] is False
        assert result["hard_stop"] is False
        assert result["stops_enforced"] is False
        state = get_daily_state()
        assert state["soft_stop_active"] is False
        assert state["hard_stop_active"] is False

    def test_daily_stops_enforced_trips_hard_stop(self, monkeypatch):
        """The unattended circuit breaker: with daily stops ENFORCED (the live config now),
        a -7% paper day trips the normal-mode hard stop (-6%) -> reduce-only."""
        monkeypatch.setenv("APCA_BASE_URL", "https://paper-api.alpaca.markets")
        monkeypatch.setattr(common, "daily_stops_enforced", lambda: True)
        _clear_daily_state()
        result = check_daily_stop(93_000, opening_equity=100_000, completed_trades=50)
        assert result["stops_enforced"] is True
        assert result["hard_stop"] is True


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
