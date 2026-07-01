"""
Model-ID / pricing config smoke test — FeintTrade.
Run: python -m pytest tests/test_model_config.py -v

Offline check (no Anthropic API calls): every model ID configured in watchlist.json
api_config.models must have a price entry in orchestrator._PRICES (the single source of
price data), else it would silently bill at the Opus fallback rate. Also verifies the
api_cost spend summary degrades gracefully when no monthly budget is configured.
"""

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def test_configured_models_all_priced():
    orch = importlib.import_module("scripts.orchestrator")
    res = orch.validate_model_config()
    assert res["ok"], f"Unpriced configured model IDs: {res['unpriced']}"
    assert res["models"], "expected at least one configured model"
    assert all(v["priced"] for v in res["models"].values())


def test_unpriced_model_is_flagged():
    orch = importlib.import_module("scripts.orchestrator")
    res = orch.validate_model_config({"trading": "claude-opus-4-8", "x": "made-up-model"})
    assert not res["ok"]
    assert "made-up-model" in res["unpriced"]
    assert res["models"]["trading"]["priced"] is True
    assert res["models"]["x"]["priced"] is False


def test_api_cost_no_budget_does_not_crash():
    ac = importlib.import_module("api_cost")
    s = ac.spend_summary(
        records=[{"ts": "2026-06-15", "cost_usd": 0.5, "routine": "trading"}],
        monthly_budget=None,
    )
    assert "budget" not in s            # absent budget -> no budget gauge, no crash
    assert s["all_time"] == 0.5
    # format_brief must render the informational no-budget line.
    assert "No monthly budget" in ac.format_brief(s)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
