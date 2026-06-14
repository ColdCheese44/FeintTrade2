"""
Market Regime Detection — FeintTrade.

Determines current market state based on SPY trend (EMA9/21/50),
VIX level, and fear/greed context. Outputs regime + position sizing
multiplier for injection into every Claude prompt.

Regimes:
  BULL    — clear uptrend, low VIX. Full allocation permitted.
  NEUTRAL — mixed signals, moderate VIX. Reduce size, pick best setups.
  BEAR    — downtrend, elevated VIX. Inverse ETFs or cash. Avoid longs.
  PANIC   — VIX >=35, extreme fear. Capital preservation. Minimal exposure.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ALPACA_KEY    = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY")
HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}

# Position sizing multiplier: multiply symbol's max_allocation_pct by this
REGIME_MULTIPLIERS = {
    "BULL":    1.00,
    "NEUTRAL": 0.60,
    "BEAR":    0.30,
    "PANIC":   0.10,
}

# Strategies valid in each regime
REGIME_STRATEGIES = {
    "BULL":    ["gap_and_go", "momentum_breakout", "ema_vwap_cross", "vwap_bounce", "bb_squeeze_breakout"],
    "NEUTRAL": ["vwap_bounce", "ema_vwap_cross", "mean_reversion", "bb_squeeze_breakout", "fibonacci_level"],
    "BEAR":    ["mean_reversion", "oversold_bounce", "short_momentum", "inverse_etf_momentum"],
    "PANIC":   ["oversold_bounce", "panic_hedge"],
}

# Instrument preferences per regime
REGIME_INSTRUMENTS = {
    "BULL":    ["TQQQ", "SOXL", "FNGU", "LABU", "NVDA", "AMD", "TSLA", "COIN", "PLTR", "BTC/USD", "ETH/USD", "SOL/USD"],
    "NEUTRAL": ["NVDA", "AMD", "TSLA", "PLTR", "MSTR", "BTC/USD", "ETH/USD"],
    "BEAR":    ["SQQQ", "UVXY", "BTC/USD"],   # Shorts + hedge + BTC only if decoupled
    "PANIC":   ["UVXY"],                        # Volatility hedge + cash
}

# Stop-loss tightness by regime
REGIME_STOPS = {
    "BULL":    -5.0,   # Standard stop
    "NEUTRAL": -4.0,
    "BEAR":    -3.0,   # Tighter
    "PANIC":   -2.0,   # Very tight
}


def _calc_ema(prices: list, period: int) -> float | None:
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 4)


def _get_spy_data() -> dict:
    """Fetch SPY daily bars; compute EMA9/21/50 trend signals."""
    try:
        url   = "https://data.alpaca.markets/v2/stocks/SPY/bars"
        start = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
        r = requests.get(url, headers=HEADERS, params={
            "timeframe": "1Day", "limit": 120, "start": start,
            "adjustment": "raw", "feed": "iex",
        }, timeout=12)
        r.raise_for_status()
        bars   = r.json().get("bars", [])
        if not bars:
            return {"error": "No SPY bars returned"}

        closes = [b["c"] for b in bars]
        highs  = [b["h"] for b in bars]
        lows   = [b["l"] for b in bars]
        ema9   = _calc_ema(closes, 9)
        ema21  = _calc_ema(closes, 21)
        ema50  = _calc_ema(closes, 50)
        latest = closes[-1]
        prev   = closes[-2] if len(closes) >= 2 else latest

        # 5-day momentum
        mom5  = round((closes[-1] - closes[-6]) / closes[-6] * 100, 2) if len(closes) >= 6 else None
        # 20-day momentum
        mom20 = round((closes[-1] - closes[-21]) / closes[-21] * 100, 2) if len(closes) >= 21 else None

        # 52-week hi/lo context
        hi52  = max(highs[-252:]) if len(highs) >= 252 else max(highs)
        lo52  = min(lows[-252:])  if len(lows) >= 252  else min(lows)
        pct_from_hi = round((latest - hi52) / hi52 * 100, 2)
        pct_from_lo = round((latest - lo52) / lo52 * 100, 2)

        return {
            "latest":              latest,
            "day_change_pct":      round((latest - prev) / prev * 100, 2),
            "ema9":                ema9,
            "ema21":               ema21,
            "ema50":               ema50,
            "ema9_above_ema21":    (ema9 or 0) > (ema21 or 0),
            "ema21_above_ema50":   (ema21 or 0) > (ema50 or 0),
            "price_above_ema50":   latest > (ema50 or 0),
            "pct_above_ema9":      round((latest - ema9) / ema9 * 100, 2) if ema9 else None,
            "pct_above_ema21":     round((latest - ema21) / ema21 * 100, 2) if ema21 else None,
            "pct_above_ema50":     round((latest - ema50) / ema50 * 100, 2) if ema50 else None,
            "momentum_5d":         mom5,
            "momentum_20d":        mom20,
            "pct_from_52w_high":   pct_from_hi,
            "pct_from_52w_low":    pct_from_lo,
        }
    except Exception as e:
        return {"error": str(e)}


def _get_vix() -> float | None:
    """VIX level from FRED (primary) or yfinance (fallback)."""
    try:
        fred_key = os.getenv("FRED_API_KEY")
        if fred_key:
            r = requests.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={"series_id": "VIXCLS", "api_key": fred_key, "file_type": "json",
                        "sort_order": "desc", "limit": 3},
                timeout=10,
            )
            r.raise_for_status()
            for o in r.json().get("observations", []):
                if o.get("value") and o["value"] != ".":
                    return round(float(o["value"]), 2)
    except Exception:
        pass
    try:
        import yfinance as yf
        hist = yf.Ticker("^VIX").history(period="5d")
        if not hist.empty:
            return round(float(hist["Close"].iloc[-1]), 2)
    except Exception:
        pass
    return None


def _get_market_breadth() -> dict:
    """
    Approximate market breadth using SPY vs IWM (small caps).
    If IWM > SPY performance → broad participation (bull signal).
    """
    try:
        syms = "SPY,IWM,QQQ"
        r = requests.get(
            "https://data.alpaca.markets/v2/stocks/snapshots",
            headers=HEADERS,
            params={"symbols": syms, "feed": "iex"},
            timeout=10,
        )
        r.raise_for_status()
        snaps = r.json()
        result = {}
        for sym in ["SPY", "IWM", "QQQ"]:
            snap = snaps.get(sym, {})
            dp   = snap.get("dailyBar", {})
            lp   = snap.get("latestTrade", {}).get("p") or dp.get("c")
            op   = dp.get("o")
            if lp and op and float(op) > 0:
                result[sym] = round((float(lp) - float(op)) / float(op) * 100, 2)
        return result
    except Exception:
        return {}


def detect_regime() -> dict:
    """
    Full regime detection. Returns regime dict with all signals.
    """
    spy    = _get_spy_data()
    vix    = _get_vix()
    breadth = _get_market_breadth()

    # SPY trend is the backbone of the score (4 of the EMA/price points). If the SPY
    # fetch failed, every spy.get("ema*…") below reads None → falsy → silently piles up
    # bear_pts (2+1+2=5), flipping the regime to BEAR on a transient DATA BLIP rather
    # than the market. BEAR is not a safe default: it BANS leveraged longs, cuts sizing
    # to 30%, and swaps the preferred set toward inverse/hedge names — an active wrong-way
    # call driven by missing data. Fail instead to the documented safe default (NEUTRAL),
    # still honoring a clear VIX panic reading if we have one.
    if spy.get("error"):
        forced = "PANIC" if (vix and vix >= 35) else "NEUTRAL"
        note = f"SPY data unavailable ({spy['error']}) — defaulting to {forced} (no trend read)"
        return {
            "regime":               forced,
            "multiplier":           REGIME_MULTIPLIERS[forced],
            "stop_loss_pct":        REGIME_STOPS[forced],
            "bull_points":          0,
            "bear_points":          0,
            "vix":                  vix,
            "spy":                  spy,
            "breadth":              breadth,
            "signals":              [note],
            "active_strategies":    REGIME_STRATEGIES[forced],
            "preferred_instruments": REGIME_INSTRUMENTS[forced],
            "sizing_rule":          f"Scale ALL positions to {REGIME_MULTIPLIERS[forced]*100:.0f}% of max_allocation_pct",
            "stop_rule":            f"Use {REGIME_STOPS[forced]:.0f}% stop-loss in {forced} regime",
            "timestamp":            datetime.now().isoformat(),
            "data_warning":         note,
        }

    # Scoring model: bull_pts vs bear_pts
    bull_pts = 0
    bear_pts = 0
    signals  = []

    # ── SPY EMA signals ──
    if spy.get("ema9_above_ema21"):
        bull_pts += 2
        signals.append("SPY EMA9>EMA21 ✅")
    else:
        bear_pts += 2
        signals.append("SPY EMA9<EMA21 ❌")

    if spy.get("ema21_above_ema50"):
        bull_pts += 1
        signals.append("SPY EMA21>EMA50 ✅")
    else:
        bear_pts += 1
        signals.append("SPY EMA21<EMA50 ❌")

    if spy.get("price_above_ema50"):
        bull_pts += 1
        signals.append("SPY above EMA50 ✅")
    else:
        bear_pts += 2
        signals.append("SPY below EMA50 ❌")

    # ── SPY momentum ──
    mom5 = spy.get("momentum_5d", 0) or 0
    if mom5 > 1.5:
        bull_pts += 1
        signals.append(f"SPY 5d momentum +{mom5}% ✅")
    elif mom5 < -2:
        bear_pts += 1
        signals.append(f"SPY 5d momentum {mom5}% ❌")

    # ── Market breadth ──
    iwm_chg = breadth.get("IWM", 0)
    spy_chg = breadth.get("SPY", 0)
    if iwm_chg > 0 and spy_chg > 0:
        bull_pts += 1
        signals.append(f"Broad rally: SPY+{spy_chg}% IWM+{iwm_chg}% ✅")
    elif iwm_chg < 0 and spy_chg < 0:
        bear_pts += 1
        signals.append(f"Broad selloff: SPY{spy_chg}% IWM{iwm_chg}% ❌")

    # ── VIX signals ──
    if vix is not None:
        if vix >= 40:
            bear_pts += 6
            signals.append(f"VIX {vix} EXTREME PANIC ❌❌")
        elif vix >= 30:
            bear_pts += 4
            signals.append(f"VIX {vix} HIGH FEAR ❌")
        elif vix >= 25:
            bear_pts += 2
            signals.append(f"VIX {vix} elevated ❌")
        elif vix >= 20:
            bear_pts += 1
            signals.append(f"VIX {vix} moderate")
        elif vix <= 15:
            bull_pts += 2
            signals.append(f"VIX {vix} low — complacency/bull ✅")
        else:
            bull_pts += 1
            signals.append(f"VIX {vix} normal ✅")

    # ── Regime determination ──
    if vix and vix >= 35:
        regime = "PANIC"
    elif bear_pts >= bull_pts + 3:
        regime = "BEAR"
    elif bull_pts >= bear_pts + 2:
        regime = "BULL"
    else:
        regime = "NEUTRAL"

    return {
        "regime":               regime,
        "multiplier":           REGIME_MULTIPLIERS[regime],
        "stop_loss_pct":        REGIME_STOPS[regime],
        "bull_points":          bull_pts,
        "bear_points":          bear_pts,
        "vix":                  vix,
        "spy":                  spy,
        "breadth":              breadth,
        "signals":              signals,
        "active_strategies":    REGIME_STRATEGIES[regime],
        "preferred_instruments": REGIME_INSTRUMENTS[regime],
        "sizing_rule":          f"Scale ALL positions to {REGIME_MULTIPLIERS[regime]*100:.0f}% of max_allocation_pct",
        "stop_rule":            f"Use {REGIME_STOPS[regime]:.0f}% stop-loss in {regime} regime",
        "timestamp":            datetime.now().isoformat(),
    }


def get_regime_brief() -> str:
    """Formatted regime summary for injection into Claude prompts."""
    try:
        r = detect_regime()
    except Exception as e:
        return f"=== REGIME DETECTION FAILED: {e} ===\nDefault to NEUTRAL regime — 60% sizing."

    spy = r.get("spy", {})
    breadth = r.get("breadth", {})

    lines = [
        f"=== MARKET REGIME: {r['regime']} ===",
        f"Score: {r['bull_points']} bull pts vs {r['bear_points']} bear pts",
        "",
        f"SIZING RULE:    {r['sizing_rule']}",
        f"STOP-LOSS RULE: {r['stop_rule']}",
        "",
        f"SPY: ${spy.get('latest','?')} | Day: {spy.get('day_change_pct','?')}%",
        f"     EMA9 {'+' if spy.get('ema9_above_ema21') else '<'} EMA21 | EMA21 {'+' if spy.get('ema21_above_ema50') else '<'} EMA50 | {'above' if spy.get('price_above_ema50') else 'BELOW'} EMA50",
        f"     5d momentum: {spy.get('momentum_5d','?')}% | 20d: {spy.get('momentum_20d','?')}%",
        f"     From 52w high: {spy.get('pct_from_52w_high','?')}%",
        f"VIX: {r.get('vix','?')}",
        "Breadth: " + " | ".join(f"{k} {v:+.2f}%" for k, v in breadth.items()),
        "",
        f"Active strategies: {', '.join(r['active_strategies'])}",
        f"Preferred instruments: {', '.join(r['preferred_instruments'])}",
        "",
        "Signal breakdown:",
    ]
    for sig in r.get("signals", []):
        lines.append(f"  {sig}")

    return "\n".join(lines)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "brief"
    if cmd == "brief":
        print(get_regime_brief())
    elif cmd == "detect":
        print(json.dumps(detect_regime(), indent=2, default=str))
    elif cmd == "multiplier":
        r = detect_regime()
        print(json.dumps({"regime": r["regime"], "multiplier": r["multiplier"]}, indent=2))
    else:
        print(f"Unknown: {cmd}")
        sys.exit(1)
