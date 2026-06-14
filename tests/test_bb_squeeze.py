"""
BB-squeeze release-detection tests — FeintTrade.
Run: python -m pytest tests/test_bb_squeeze.py -v

detect_bb_squeeze() used to report SQUEEZE_RELEASED_<dir> for ANY bar that wasn't
currently in a squeeze — so a name with permanently wide bands (never coiling) read as
a fresh "bullish squeeze release" every bar, firing the +1 bullish signal
(research.calc_moving_averages) and SOP Strategy 5's core trigger on plain trending.
The fix requires a true squeeze→no-squeeze TRANSITION. These tests pin the three states:
NO_SQUEEZE (wide bands, never coiled), SQUEEZE_ACTIVE (coiling now), and a real RELEASE.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import research


def _bar(c, h=None, l=None, v=1000):
    return {"c": c, "h": c + 0.5 if h is None else h, "l": c - 0.5 if l is None else l, "v": v}


def test_wide_bands_report_no_squeeze_not_release():
    """A strong steady uptrend (wide BB, never coiled) must NOT be flagged as a squeeze
    release — and crucially must NOT contain the SQUEEZE_RELEASED_BULLISH substring the
    bullish-signal counter keys on."""
    bars = [_bar(100 + 2 * i) for i in range(25)]   # linear ramp → wide bands throughout
    r = research.detect_bb_squeeze(bars)

    assert r["in_squeeze"] is False
    assert r["released"] is False
    assert r["signal"].startswith("NO_SQUEEZE")
    # The exact contract the signal counter (research.py) checks:
    assert "SQUEEZE_RELEASED_BULLISH" not in r["signal"]


def test_tight_coil_reports_active_squeeze():
    """Flat closes with a small intrabar range keep BB inside Keltner → active squeeze."""
    bars = [_bar(100.0) for _ in range(25)]
    r = research.detect_bb_squeeze(bars)

    assert r["in_squeeze"] is True
    assert r["released"] is False
    assert r["signal"].startswith("SQUEEZE_ACTIVE")


def test_true_transition_reports_release():
    """A long coil (prior bar squeezing) that breaks out on the last bar (now wide) is a
    genuine release → SQUEEZE_RELEASED, and bullish given the upside break."""
    bars = [_bar(100.0) for _ in range(21)]          # 21 flat coiling bars
    bars.append(_bar(130.0))                          # decisive upside break on the last bar
    r = research.detect_bb_squeeze(bars)

    assert r["was_squeezing"] is True
    assert r["in_squeeze"] is False
    assert r["released"] is True
    assert r["signal"] == "SQUEEZE_RELEASED_BULLISH — momentum building"


def test_signal_count_no_longer_inflated_by_wide_bands():
    """End-to-end: the wide-band trend must not earn the +1 'squeeze released bullish'
    point in the composite bullish_signal_count."""
    bars = [_bar(100 + 2 * i) for i in range(60)]
    mas = research.calc_moving_averages({"bars": bars})
    # The squeeze release point specifically must be absent.
    assert "SQUEEZE_RELEASED_BULLISH" not in (mas.get("bb_squeeze") or {}).get("signal", "")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
