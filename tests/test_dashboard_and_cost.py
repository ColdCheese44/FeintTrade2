"""
Tests for api_cost (spend), dashboard_helpers (banner/freshness), and test_report (parse).
All pure + hermetic — records/now/sample-output are passed in; no files/network.

Run: python -B -m pytest tests/test_dashboard_and_cost.py -q
"""

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import api_cost
import dashboard_helpers as dh
import test_report


# ── api_cost ─────────────────────────────────────────────────────────────────────

def test_spend_summary_buckets():
    recs = [
        {"ts": "2026-06-14 10:00 MDT", "routine": "cycle", "cost_usd": 0.02},
        {"ts": "2026-06-14 09:00 MDT", "routine": "crypto", "cost_usd": 0.01},
        {"ts": "2026-06-10 09:00 MDT", "routine": "cycle", "cost_usd": 0.05},   # same month
        {"ts": "2026-05-30 09:00 MDT", "routine": "eod", "cost_usd": 1.00},     # prior month
    ]
    s = api_cost.spend_summary(records=recs, monthly_budget=10, now=datetime(2026, 6, 14, 12))
    assert abs(s["today"] - 0.03) < 1e-9
    assert abs(s["month"] - 0.08) < 1e-9
    assert abs(s["all_time"] - 1.08) < 1e-9
    assert s["calls_today"] == 2
    assert s["budget"] == 10 and abs(s["budget_remaining"] - 9.92) < 1e-9
    assert "cycle" in s["by_routine"]


def test_spend_fund_soon_flag():
    recs = [{"ts": "2026-06-01 00:00 MDT", "routine": "x", "cost_usd": 9.0}]
    s = api_cost.spend_summary(records=recs, monthly_budget=10, now=datetime(2026, 6, 5, 12))
    assert s["fund_soon"] is True


def test_format_brief_mentions_no_balance_api():
    s = api_cost.spend_summary(records=[{"ts": "2026-06-14 10:00 MDT", "routine": "c", "cost_usd": 0.5}],
                               now=datetime(2026, 6, 14, 12))
    out = api_cost.format_brief(s)
    assert "spend" in out.lower() and "balance API" in out


# ── dashboard_helpers ─────────────────────────────────────────────────────────────

def test_research_banner_from_config():
    rm = {"max_open_positions": 6, "max_crypto_exposure_pct": 50, "min_buy_score": 5,
          "disable_loss_streak_lockout": True, "disable_validation_mode": True, "relax_dedup": True}
    txt = dh.format_research_banner(rm)
    assert "positions 6" in txt and "crypto 50%" in txt and "buy score ≥5" in txt
    assert "lockout off" in txt and "15" not in txt   # no stale hardcoded values


def test_freshness_label():
    assert dh.freshness_label(10)[0].startswith("🟢")
    assert dh.freshness_label(300)[0].startswith("🟡")
    assert dh.freshness_label(None)[0].startswith("🔴")


# ── test_report parsing ───────────────────────────────────────────────────────────

def test_test_report_parse_and_embed():
    sample = (
        "tests/test_a.py::test_one PASSED                  [ 10%]\n"
        "tests/test_a.py::test_two FAILED                  [ 20%]\n"
        "tests/test_b.py::test_three PASSED                [ 30%]\n"
        "==== short test summary ====\n"
    )
    files = test_report.parse(sample)
    assert files["test_a.py"] == [("test_one", "PASSED"), ("test_two", "FAILED")]
    embed, ok, total, passed, failed = test_report.build_embed(files)
    assert (total, passed, failed, ok) == (3, 2, 1, False)
    assert "2/3" in embed["title"] and embed["color"] == 0xe74c3c


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
