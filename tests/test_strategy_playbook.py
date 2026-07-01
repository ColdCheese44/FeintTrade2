import strategy_playbook as sp


def test_prompt_brief_covers_article_families_and_feint_setups():
    brief = sp.strategy_prompt_brief()

    for family in ("swing", "volatility", "day_trading", "mean_reversion",
                   "sector_rotation", "macro", "bear_market", "gold", "forex"):
        assert family in brief
    for setup in ("long_hold_trend", "scalp_liquidity", "macro_risk_off",
                  "gold_macro_proxy", "pump_and_dump_avoidance"):
        assert setup in brief


def test_pump_and_dump_is_blocked_not_executable():
    assert sp.normalize_setup_type("pump and dump") == "pump_and_dump_avoidance"
    ok, msg = sp.validate_setup_for_entry("pump and dump", score=10)

    assert not ok
    assert "never executable" in msg


def test_scalping_requires_explicit_high_score():
    ok, msg = sp.validate_setup_for_entry("scalping", score=7)
    assert not ok
    assert "score >= 8" in msg

    ok, msg = sp.validate_setup_for_entry("scalping", score=8)
    assert ok
    assert msg == "ok"


def test_advisory_only_unsupported_families_do_not_enter():
    ok, msg = sp.validate_setup_for_entry("forex", score=10)
    assert not ok
    assert "advisory-only" in msg

    ok, msg = sp.validate_setup_for_entry("pairs_trading", score=10)
    assert not ok
    assert "advisory-only" in msg


def test_gold_proxy_is_executable_after_watchlist_support():
    ok, msg = sp.validate_setup_for_entry("gold_macro_proxy", score=8)
    assert ok
    assert msg == "ok"
