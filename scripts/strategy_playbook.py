"""FeintTrade strategy taxonomy and entry guardrails.

This module gives the model a broader strategy vocabulary while keeping execution
boring and testable. It maps broad strategy families into FeintTrade's actual
Alpaca-supported universe and blocks manipulative or low-quality tactics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


ARTICLE_STRATEGY_FAMILIES = (
    "swing",
    "volatility",
    "s&p_500",
    "overnight",
    "day_trading",
    "mean_reversion",
    "nasdaq",
    "fixed_income",
    "candlestick",
    "treasuries_bonds",
    "technical_indicators",
    "russell_2000",
    "seasonality",
    "sector_rotation",
    "momentum",
    "trend_following",
    "connors_rsi2",
    "trend_reversal",
    "sentiment",
    "moving_average",
    "macro",
    "bear_market",
    "market_neutral",
    "breakout",
    "volatility_indicators",
    "oscillators",
    "price_action",
    "random_indicator_mix",
    "gold",
    "forex",
)


@dataclass(frozen=True)
class StrategySpec:
    setup_type: str
    family: str
    horizon: str
    instruments: str
    score_floor: int
    summary: str


STRATEGIES: tuple[StrategySpec, ...] = (
    StrategySpec(
        "long_hold_trend",
        "swing / trend_following / moving_average",
        "weeks-months",
        "SPY/QQQ proxies, liquid leaders, BTC/ETH when daily trend is intact",
        7,
        "Position-style trend hold: price above major averages, thesis intact, trail winners rather than flattening.",
    ),
    StrategySpec(
        "swing_momentum",
        "swing / momentum",
        "days-weeks",
        "liquid leaders, leveraged long ETFs in BULL only",
        6,
        "Core FeintTrade swing setup: EMA alignment, VWAP reclaim, OBV/volume confirmation, R:R >= 2:1.",
    ),
    StrategySpec(
        "day_trade_momentum",
        "day_trading / momentum",
        "same session",
        "high-liquidity equities/ETFs during regular market hours",
        8,
        "Intraday momentum only when liquid, high-volume, and high-conviction; never a boredom trade.",
    ),
    StrategySpec(
        "scalp_liquidity",
        "day_trading / scalping",
        "minutes",
        "very liquid equities/ETFs only, tight spread, regular session",
        8,
        "Tiny-target scalp with limit orders only; requires a clear catalyst, tight spread, and immediate invalidation.",
    ),
    StrategySpec(
        "overnight_momentum",
        "overnight",
        "close-to-next-open",
        "liquid equities/ETFs with confirmed catalyst; no UVXY",
        7,
        "Carry only when the close is strong, trend confirms, and gap risk is compensated by R:R.",
    ),
    StrategySpec(
        "volatility_breakout",
        "volatility / breakout / volatility_indicators",
        "hours-days",
        "liquid equities, ETFs, crypto majors",
        7,
        "Expansion trade after a volatility coil or ATR regime shift; avoid chasing parabolic low-liquidity names.",
    ),
    StrategySpec(
        "bb_squeeze_breakout",
        "volatility / breakout / technical_indicators",
        "days-weeks",
        "PLTR, NVDA, TQQQ, BTC/USD, ETH/USD",
        6,
        "Fresh squeeze release with bullish momentum and volume; active/coiling squeeze is watch-only.",
    ),
    StrategySpec(
        "ema_vwap_cross",
        "technical_indicators / moving_average",
        "hours-days",
        "liquid equities/ETFs",
        6,
        "Short-term EMA turn plus VWAP reclaim; use VWAP failure as thesis break.",
    ),
    StrategySpec(
        "mean_reversion",
        "mean_reversion / oscillators / connors_rsi2",
        "hours-days",
        "liquid names with no negative fundamental catalyst",
        7,
        "Oversold bounce only with support, OBV divergence, and no bad-news knife catch.",
    ),
    StrategySpec(
        "price_action_reversal",
        "candlestick / price_action / trend_reversal",
        "hours-days",
        "liquid equities/ETFs/crypto majors",
        7,
        "Hammer/pin/engulfing reversal at support with volume confirmation; not a blind catch.",
    ),
    StrategySpec(
        "sector_rotation",
        "sector_rotation / s&p_500 / nasdaq / russell_2000",
        "days-weeks",
        "ETFs and leaders in the strongest sector",
        6,
        "Rotate toward relative strength and away from laggards; avoid short-window whipsaw.",
    ),
    StrategySpec(
        "sentiment_contrarian",
        "sentiment / mean_reversion",
        "days-weeks",
        "broad liquid indices/ETFs or crypto majors",
        7,
        "Fear/greed contrarian setup only when price stabilizes; sentiment alone is never a buy.",
    ),
    StrategySpec(
        "macro_risk_on",
        "macro / fixed_income / treasuries_bonds",
        "days-weeks",
        "broad-market ETFs, leaders, BTC/ETH",
        6,
        "Risk-on posture when rates/USD/liquidity favor growth; trade via supported Alpaca assets, not bonds directly.",
    ),
    StrategySpec(
        "macro_risk_off",
        "macro / bear_market / fixed_income",
        "hours-days",
        "cash, SQQQ/SOXS in non-BULL regimes, UVXY intraday only",
        6,
        "Risk-off posture: reduce longs, use inverse ETFs only when the tape confirms downside.",
    ),
    StrategySpec(
        "market_neutral_pair",
        "market_neutral",
        "days-weeks",
        "advisory only until true pair execution exists",
        9,
        "Pairs/stat-arb idea is research-only for now because execution has no paired hedge order primitive.",
    ),
    StrategySpec(
        "gold_macro_proxy",
        "gold / macro",
        "days-weeks",
        "GLD/IAU/GDX-style liquid gold proxies when present in the watchlist",
        7,
        "Gold macro setup: small, incubated allocation only when USD/rates/risk-off context and price trend align.",
    ),
    StrategySpec(
        "forex_macro_proxy",
        "forex / macro",
        "days-weeks",
        "advisory only; Alpaca spot FX is not in this bot",
        8,
        "Forex knowledge informs USD/rates context only; do not emit unsupported FX orders.",
    ),
    StrategySpec(
        "pump_and_dump_avoidance",
        "fraud / manipulation risk",
        "avoid",
        "low-priced, thin, parabolic names",
        10,
        "Never participate in pump-and-dump behavior; filter, quarantine, or skip suspected manipulation.",
    ),
)


_ALIASES = {
    "position_trading": "long_hold_trend",
    "long_hold": "long_hold_trend",
    "long_holds": "long_hold_trend",
    "buy_and_hold": "long_hold_trend",
    "swing": "swing_momentum",
    "swing_trading": "swing_momentum",
    "day_trading": "day_trade_momentum",
    "daytrade": "day_trade_momentum",
    "scalp": "scalp_liquidity",
    "scalping": "scalp_liquidity",
    "volatility": "volatility_breakout",
    "breakout": "volatility_breakout",
    "oscillator_reversion": "mean_reversion",
    "rsi2": "mean_reversion",
    "connors_rsi2": "mean_reversion",
    "candlestick_reversal": "price_action_reversal",
    "price_action": "price_action_reversal",
    "rotation": "sector_rotation",
    "stock_sector_rotation": "sector_rotation",
    "risk_on_macro": "macro_risk_on",
    "risk_off_macro": "macro_risk_off",
    "bear_market": "macro_risk_off",
    "pairs_trade": "market_neutral_pair",
    "pairs_trading": "market_neutral_pair",
    "gold": "gold_macro_proxy",
    "forex": "forex_macro_proxy",
    "fx": "forex_macro_proxy",
    "pump_and_dump": "pump_and_dump_avoidance",
    "pump_dump": "pump_and_dump_avoidance",
    "pump-and-dump": "pump_and_dump_avoidance",
}

_BLOCKED_SETUPS = {
    "pump_and_dump",
    "pump_dump",
    "pump-and-dump",
    "pump_and_dump_avoidance",
    "market_manipulation",
}

_HIGH_SCORE_SETUPS = {"day_trade_momentum", "scalp_liquidity"}
_ADVISORY_ONLY = {"market_neutral_pair", "forex_macro_proxy"}


def normalize_setup_type(setup_type: str | None) -> str:
    raw = str(setup_type or "").strip().lower()
    if not raw:
        return ""
    normalized = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return _ALIASES.get(normalized, normalized)


def is_manipulation_setup(setup_type: str | None) -> bool:
    normalized = normalize_setup_type(setup_type)
    raw = str(setup_type or "").lower()
    return normalized in _BLOCKED_SETUPS or ("pump" in raw and "dump" in raw)


def _score_value(score) -> int | None:
    try:
        return int(round(float(score))) if score not in (None, "") else None
    except (TypeError, ValueError):
        return None


def validate_setup_for_entry(setup_type: str | None, *, score=None) -> tuple[bool, str]:
    """Return (allowed, reason) for opening a new position with this setup_type."""
    normalized = normalize_setup_type(setup_type)
    if not normalized:
        return False, "missing setup_type"

    if is_manipulation_setup(setup_type):
        return (
            False,
            "pump-and-dump/manipulation labels are never executable strategies; "
            "use pump_and_dump_avoidance as a skip/quarantine reason only.",
        )

    if normalized in _ADVISORY_ONLY:
        return (
            False,
            f"{normalized} is advisory-only until the required instrument/execution support exists.",
        )

    if normalized in _HIGH_SCORE_SETUPS:
        sv = _score_value(score)
        if sv is None or sv < 8:
            return (
                False,
                f"{normalized} requires explicit score >= 8 because intraday churn/scalping is high-risk.",
            )

    return True, "ok"


def strategy_prompt_brief() -> str:
    """Compact strategy brief injected into model prompts."""
    supported = "\n".join(
        f"- `{s.setup_type}` [{s.family}; {s.horizon}; score>={s.score_floor}]: {s.summary}"
        for s in STRATEGIES
    )
    article = ", ".join(ARTICLE_STRATEGY_FAMILIES)
    return (
        "=== EXPANDED STRATEGY PLAYBOOK ===\n"
        "Use the Robust Trader strategy families as vocabulary, but trade only FeintTrade-supported, "
        "Alpaca-tradable setups with tested guardrails.\n"
        f"Article families covered: {article}.\n\n"
        "Executable/advisory Feint mappings:\n"
        f"{supported}\n\n"
        "Critical safety: pump-and-dump is fraud/manipulation risk, not an entry tactic. "
        "Suspected pump names should be skipped/quarantined; never emit a BUY with pump-and-dump wording."
    )
