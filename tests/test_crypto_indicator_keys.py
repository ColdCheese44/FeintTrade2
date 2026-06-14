"""
Crypto-cycle indicator wiring — MindHub Trader.
Run: python -m pytest tests/test_crypto_indicator_keys.py -v

gather_crypto_data() hand-picks a compact indicator payload for the crypto SCORED
system. It read flat keys (macd_signal / obv_trend / volume_spike_ratio) that
calc_moving_averages() never produces — those indicators are nested under macd / obv /
volume_spike. The flat lookups always returned None, so Strategy 9 scored crypto with
null MACD (+2), OBV (+1), and volume spike (+1) every cycle. This test wires
gather_crypto_data against a realistic calc_moving_averages payload and proves the three
indicators now arrive populated (and that bb_squeeze, which was already correct, still does).
"""

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import research  # the producer whose nested-key contract we depend on


# A daily indicator block exactly as research.calc_moving_averages() emits it.
def _daily_block():
    bars = [{"c": 100 + i, "h": 100 + i + 0.5, "l": 100 + i - 0.5, "v": 1000 + 10 * i}
            for i in range(60)]
    ma = research.calc_moving_averages({"bars": bars})
    # Sanity: the producer really does nest these (the contract gather_crypto_data reads).
    assert isinstance(ma.get("macd"), dict) and "crossover" in ma["macd"]
    assert isinstance(ma.get("obv"), dict) and "obv_trend" in ma["obv"]
    assert isinstance(ma.get("volume_spike"), dict) and "ratio" in ma["volume_spike"]
    return ma


def test_crypto_payload_populates_macd_obv_volume(monkeypatch):
    orch = importlib.import_module("scripts.orchestrator")
    ma = _daily_block()

    monkeypatch.setattr(orch, "load_watchlist",
                        lambda: {"watchlist": [{"symbol": "BTC/USD", "max_allocation_pct": 35}]})
    monkeypatch.setattr(orch, "get_positions_norm", lambda: [])
    monkeypatch.setattr(orch, "summarize_news", lambda *a, **k: "")
    monkeypatch.setattr(orch, "run",
                        lambda script, *a: {"equity": "100000", "cash": "95000"}
                        if (script, a[:1]) == ("research.py", ("account",)) else {})

    def _safe_run(script, *a):
        if script == "research.py":
            return {
                "bars":     {"moving_averages": ma},
                "hourly":   {"indicators": {"ema9_1h": 101, "ema21_1h": 100, "rsi14_1h": 55}},
                "intraday": {"bars": [{"c": 159.0, "h": 159.5, "l": 158.5, "v": 5}], "vwap": 158.7},
            }.get(a[0], {})
        if script == "enrichment.py":
            return [] if a[0] == "news" else {}
        return {}
    monkeypatch.setattr(orch, "safe_run", _safe_run)

    data = orch.gather_crypto_data()
    payload = data["research"]["BTC/USD"]

    # The three formerly-null indicators now arrive populated, matching the producer.
    assert payload["macd_signal"] == ma["macd"]["crossover"] is not None
    assert payload["obv_trend"] == ma["obv"]["obv_trend"] is not None
    assert payload["volume_spike"] == ma["volume_spike"]["ratio"] is not None
    # bb_squeeze was already wired correctly — keep it that way.
    assert payload["bb_squeeze"] == ma["bb_squeeze"]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
