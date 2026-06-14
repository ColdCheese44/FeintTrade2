"""
Regime fallback tests — FeintTrade.
Run: python -m pytest tests/test_regime_fallback.py -v

detect_regime() scores SPY's EMA/price trend as the backbone of the bull/bear model.
When the SPY data fetch fails, _get_spy_data() returns {"error": ...} and every
spy.get("ema*…") read is None → falsy → the EMA checks silently pile up bear_pts
(2+1+2=5). That flipped the regime to BEAR on a transient DATA BLIP — which is NOT a
safe default: BEAR bans leveraged longs, cuts sizing to 30%, and swaps the preferred
instruments toward inverse/hedge names. These tests pin the fix: a SPY-data failure
falls back to the documented safe default (NEUTRAL), still honoring a clear VIX panic
reading, and a healthy SPY read still scores normally.
"""

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import regime as regime_mod


@pytest.fixture
def regime(monkeypatch):
    """Fresh import with breadth stubbed empty (its own fetch is irrelevant here)."""
    monkeypatch.setattr(regime_mod, "_get_market_breadth", lambda: {})
    return regime_mod


def test_spy_error_defaults_to_neutral_not_bear(regime, monkeypatch):
    """A SPY-data failure (with no VIX) must yield NEUTRAL — not the spurious BEAR the
    all-falsy EMA reads used to produce — and must NOT authorize inverse/leveraged names."""
    monkeypatch.setattr(regime, "_get_spy_data", lambda: {"error": "connection reset"})
    monkeypatch.setattr(regime, "_get_vix", lambda: None)

    r = regime.detect_regime()

    assert r["regime"] == "NEUTRAL"
    assert r["multiplier"] == 0.60
    assert r["bull_points"] == 0 and r["bear_points"] == 0
    # No leveraged long NOR inverse/hedge names get authorized off a data blip.
    assert "TQQQ" not in r["preferred_instruments"]
    assert "SQQQ" not in r["preferred_instruments"]
    assert "UVXY" not in r["preferred_instruments"]
    assert "data_warning" in r and "SPY data unavailable" in r["data_warning"]


def test_spy_error_with_panic_vix_still_panics(regime, monkeypatch):
    """Even without a SPY trend read, a clearly panic-level VIX should still force PANIC
    (capital preservation is always a safe call)."""
    monkeypatch.setattr(regime, "_get_spy_data", lambda: {"error": "timeout"})
    monkeypatch.setattr(regime, "_get_vix", lambda: 42.0)

    r = regime.detect_regime()

    assert r["regime"] == "PANIC"
    assert r["multiplier"] == 0.10
    assert r["preferred_instruments"] == ["UVXY"]


def test_healthy_spy_still_scores_bull(regime, monkeypatch):
    """Guard must not disturb the normal path: a clean bullish SPY read + low VIX still
    resolves to BULL with full sizing."""
    monkeypatch.setattr(regime, "_get_spy_data", lambda: {
        "ema9_above_ema21": True,
        "ema21_above_ema50": True,
        "price_above_ema50": True,
        "momentum_5d": 2.0,
    })
    monkeypatch.setattr(regime, "_get_vix", lambda: 14.0)
    monkeypatch.setattr(regime, "_get_market_breadth", lambda: {"SPY": 0.5, "IWM": 0.6, "QQQ": 0.4})

    r = regime.detect_regime()

    assert r["regime"] == "BULL"
    assert r["multiplier"] == 1.00
    assert r["bull_points"] > r["bear_points"]
    assert "TQQQ" in r["preferred_instruments"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
