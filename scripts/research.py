"""Market data and account research helpers."""

import os
import sys
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from common import is_crypto, normalize_positions, make_http_session  # noqa: E402

ALPACA_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_BASE_URL", "https://paper-api.alpaca.markets")
REQUEST_TIMEOUT = 15

# Retry-resilient session — survives transient DNS/VPN-reconnect blips on data fetches.
_HTTP = make_http_session()

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}


def get_bars(symbol, timeframe="1Day", limit=60):
    """
    Fetch the MOST RECENT `limit` daily bars — equities or crypto.

    Alpaca returns bars ASCENDING from `start`, capped at the request limit, so a
    small limit with a wide start window yields the OLDEST bars (for 7-day/week
    crypto that left the data ~1 month stale). We fetch the whole window and slice
    the newest `limit` so daily EMA/MACD/RSI reflect current prices.
    """
    # window must hold at least `limit` bars + EMA50 history on both 5-day (equity)
    # and 7-day (crypto) weeks; fetch generously then keep the most recent `limit`.
    start = (datetime.now(timezone.utc) - timedelta(days=max(150, limit * 2))).strftime("%Y-%m-%d")
    fetch_limit = max(limit * 4, 400)
    if is_crypto(symbol):
        url = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
        params = {"symbols": symbol, "timeframe": timeframe, "limit": fetch_limit, "start": start}
        response = _HTTP.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        bars = response.json().get("bars", {}).get(symbol, [])
        return {"bars": bars[-limit:]}
    else:
        url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
        params = {
            "timeframe": timeframe, "limit": fetch_limit, "start": start,
            "adjustment": "raw", "feed": "iex",
        }
        response = _HTTP.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        bars = response.json().get("bars", [])
        return {"bars": bars[-limit:]}


def get_hourly_bars(symbol, limit=48):
    """Fetch the MOST RECENT `limit` 1-hour bars — intermediate timeframe context."""
    # Use a TIGHT, near-now start window so Alpaca returns the recent bars in a single
    # page. A wide start made Alpaca paginate and return only the OLDEST page (bars from
    # WEEKS before 'now') — which corrupted hourly indicators AND broke intelligence
    # outcome-maturation (no bars after the decision to score against). Timestamp-precise
    # start; window sized to just hold `limit` bars (crypto is 24/7; equity ~7 trh/day).
    now = datetime.now(timezone.utc)
    if is_crypto(symbol):
        start = (now - timedelta(hours=limit + 18)).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        start = (now - timedelta(days=max(8, limit // 4))).strftime("%Y-%m-%dT%H:%M:%SZ")
    fetch_limit = limit + 80
    if is_crypto(symbol):
        url = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
        params = {"symbols": symbol, "timeframe": "1Hour", "limit": fetch_limit, "start": start}
        response = _HTTP.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        bars = response.json().get("bars", {}).get(symbol, [])[-limit:]
        return {"bars": bars, "indicators": _hourly_indicators(bars)}
    else:
        url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
        params = {"timeframe": "1Hour", "limit": fetch_limit, "start": start, "adjustment": "raw", "feed": "iex"}
        response = _HTTP.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        bars = response.json().get("bars", [])[-limit:]
        return {"bars": bars, "indicators": _hourly_indicators(bars)}


def _hourly_indicators(bars):
    """RSI and EMA on 1-hour closes for intermediate trend context."""
    if not bars:
        return {}
    closes = [b["c"] for b in bars]
    return {
        "ema9_1h":  calc_ema(closes, 9),
        "ema21_1h": calc_ema(closes, 21),
        "rsi14_1h": calc_rsi(closes),
        "vwap_1h":  calc_vwap(bars[-8:]),  # last 8 hours
        "trend":    "BULLISH" if (calc_ema(closes, 9) or 0) > (calc_ema(closes, 21) or 0) else "BEARISH",
    }


def get_intraday_bars(symbol, timeframe="5Min", limit=78):
    """Fetch intraday bars — equities (today's session) or crypto (last 78 bars)."""
    if is_crypto(symbol):
        url = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
        params = {"symbols": symbol, "timeframe": timeframe, "limit": limit}
        response = _HTTP.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        bars = data.get("bars", {}).get(symbol, [])
        return {"bars": bars, "vwap": calc_vwap(bars)}
    else:
        url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
        start = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        params = {
            "timeframe": timeframe,
            "limit": limit,
            "start": start,
            "adjustment": "raw",
            "feed": "iex",
        }
        response = _HTTP.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()


def get_account():
    """Get current portfolio status."""
    url = f"{BASE_URL}/v2/account"
    response = _HTTP.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_positions():
    """Get all open positions."""
    url = f"{BASE_URL}/v2/positions"
    response = _HTTP.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return normalize_positions(response.json())


def get_news(symbol):
    """Get recent news for a symbol."""
    url = "https://data.alpaca.markets/v1beta1/news"
    params = {"symbols": symbol, "limit": 5, "sort": "desc"}
    response = _HTTP.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_snapshot(symbol):
    """
    Freshest available price for a symbol (latest trade), plus day open/prev close.
    Used to ANCHOR the live price so prompts never mix a stale daily close with an
    intraday quote. Works for both equities and crypto.
    """
    try:
        if is_crypto(symbol):
            url = "https://data.alpaca.markets/v1beta3/crypto/us/snapshots"
            r = _HTTP.get(url, headers=HEADERS, params={"symbols": symbol}, timeout=10)
            r.raise_for_status()
            snap = r.json().get("snapshots", {}).get(symbol, {})
        else:
            url = f"https://data.alpaca.markets/v2/stocks/{symbol}/snapshot"
            r = _HTTP.get(url, headers=HEADERS, params={"feed": "iex"}, timeout=10)
            r.raise_for_status()
            snap = r.json()
        lt = (snap.get("latestTrade") or {}).get("p")
        lq = snap.get("latestQuote") or {}
        db = snap.get("dailyBar") or {}
        pdb = snap.get("prevDailyBar") or {}
        price = lt or db.get("c")
        prev_close = pdb.get("c")
        day_open = db.get("o")
        return {
            "symbol": symbol,
            "price": price,
            "bid": lq.get("bp"),
            "ask": lq.get("ap"),
            "day_open": day_open,
            "prev_close": prev_close,
            "day_change_pct": round((price - prev_close) / prev_close * 100, 2)
            if price and prev_close else None,
        }
    except Exception as e:
        return {"symbol": symbol, "price": None, "error": str(e)}


def calc_ema(prices, period):
    """Exponential moving average."""
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 4)


def calc_rsi(prices, period=14):
    """RSI — identifies overbought (>70) and oversold (<30) conditions."""
    if len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def calc_macd(prices, fast=12, slow=26, signal=9):
    """MACD line, signal line, histogram, and crossover direction."""
    if len(prices) < slow + signal:
        return None
    ema_fast = calc_ema(prices, fast)
    ema_slow = calc_ema(prices, slow)
    if ema_fast is None or ema_slow is None:
        return None
    # Build full MACD line history for signal EMA
    macd_line = []
    for i in range(slow - 1, len(prices)):
        ef = calc_ema(prices[:i + 1], fast)
        es = calc_ema(prices[:i + 1], slow)
        if ef and es:
            macd_line.append(ef - es)
    if len(macd_line) < signal:
        return None
    signal_line = calc_ema(macd_line, signal)
    macd_val = macd_line[-1]
    histogram = round(macd_val - signal_line, 6) if signal_line else None
    prev_hist = round(macd_line[-2] - calc_ema(macd_line[:-1], signal), 6) if len(macd_line) >= signal + 1 else None
    crossover = None
    if histogram is not None and prev_hist is not None:
        if histogram > 0 and prev_hist <= 0:
            crossover = "BULLISH_CROSS"
        elif histogram < 0 and prev_hist >= 0:
            crossover = "BEARISH_CROSS"
        elif histogram > prev_hist:
            crossover = "BULLISH_MOMENTUM"
        else:
            crossover = "BEARISH_MOMENTUM"
    return {
        "macd": round(macd_val, 6),
        "signal": round(signal_line, 6) if signal_line else None,
        "histogram": histogram,
        "crossover": crossover,
    }


def calc_bollinger_bands(prices, period=20, std_dev=2):
    """Bollinger Bands — upper, middle, lower, and % bandwidth."""
    if len(prices) < period:
        return None
    recent = prices[-period:]
    middle = sum(recent) / period
    variance = sum((p - middle) ** 2 for p in recent) / period
    std = variance ** 0.5
    upper = round(middle + std_dev * std, 4)
    lower = round(middle - std_dev * std, 4)
    middle = round(middle, 4)
    price = prices[-1]
    pct_b = round((price - lower) / (upper - lower), 4) if upper != lower else 0.5
    bandwidth = round((upper - lower) / middle * 100, 2)
    position = "ABOVE_UPPER" if price > upper else "BELOW_LOWER" if price < lower else "INSIDE"
    return {
        "upper": upper,
        "middle": middle,
        "lower": lower,
        "pct_b": pct_b,       # >1 = above upper band, <0 = below lower band
        "bandwidth_pct": bandwidth,
        "price_position": position,
    }


def calc_vwap(bars):
    """VWAP from intraday bars."""
    cumulative_tp_vol = 0
    cumulative_vol = 0
    for b in bars:
        typical_price = (b["h"] + b["l"] + b["c"]) / 3
        cumulative_tp_vol += typical_price * b["v"]
        cumulative_vol += b["v"]
    if cumulative_vol == 0:
        return None
    return round(cumulative_tp_vol / cumulative_vol, 4)


def calc_atr(bars, period=14):
    """Average True Range — measures volatility."""
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        high, low, prev_close = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    recent_trs = trs[-period:]
    return round(sum(recent_trs) / len(recent_trs), 4)


def detect_volume_spike(bars, lookback=10):
    """Flag if today's volume is >2x the recent average."""
    if len(bars) < lookback + 1:
        return None
    avg_vol = sum(b["v"] for b in bars[-(lookback + 1):-1]) / lookback
    today_vol = bars[-1]["v"]
    if avg_vol == 0:
        return None
    ratio = round(today_vol / avg_vol, 2)
    return {"today_volume": today_vol, "avg_volume": round(avg_vol), "ratio": ratio, "spike": ratio >= 2.0}


def calc_pivot_points(bars: list) -> dict | None:
    """Classic pivot points (P, S1/S2/S3, R1/R2/R3) from prior session's H/L/C."""
    if len(bars) < 2:
        return None
    prev = bars[-2]
    high, low, close = prev["h"], prev["l"], prev["c"]
    pivot = round((high + low + close) / 3, 4)
    r1 = round(2 * pivot - low, 4)
    s1 = round(2 * pivot - high, 4)
    r2 = round(pivot + (high - low), 4)
    s2 = round(pivot - (high - low), 4)
    r3 = round(high + 2 * (pivot - low), 4)
    s3 = round(low - 2 * (high - pivot), 4)
    current = bars[-1]["c"]
    return {
        "pivot": pivot, "r1": r1, "r2": r2, "r3": r3,
        "s1": s1, "s2": s2, "s3": s3,
        "price_vs_pivot": "ABOVE" if current > pivot else "BELOW",
        "nearest_resistance": r1 if current < r1 else r2,
        "nearest_support": s1 if current > s1 else s2,
        "distance_to_r1_pct": round((r1 - current) / current * 100, 2),
        "distance_to_s1_pct": round((current - s1) / current * 100, 2),
    }


def calc_obv(bars: list) -> dict | None:
    """On-Balance Volume — reveals whether volume confirms or diverges from price."""
    if len(bars) < 5:
        return None
    obv = 0
    obv_series = [0]
    for i in range(1, len(bars)):
        if bars[i]["c"] > bars[i - 1]["c"]:
            obv += bars[i]["v"]
        elif bars[i]["c"] < bars[i - 1]["c"]:
            obv -= bars[i]["v"]
        obv_series.append(obv)

    recent_obv    = obv_series[-5:]
    obv_trend     = "RISING"  if recent_obv[-1] > recent_obv[0] else "FALLING"
    price_trend   = "RISING"  if bars[-1]["c"]  > bars[-5]["c"] else "FALLING"

    divergence = None
    if price_trend == "RISING" and obv_trend == "FALLING":
        divergence = "BEARISH_DIVERGENCE"
    elif price_trend == "FALLING" and obv_trend == "RISING":
        divergence = "BULLISH_DIVERGENCE"

    return {
        "current_obv": obv,
        "obv_trend": obv_trend,
        "price_trend": price_trend,
        "divergence": divergence,
        "signal": divergence or f"CONFIRMED_{price_trend}",
    }


def detect_bb_squeeze(bars: list, bb_period: int = 20, kc_mult: float = 1.5) -> dict | None:
    """
    Bollinger Band Squeeze (TTM Squeeze variant).
    Squeeze = BB inside Keltner Channels = volatility coiling before a big move.

    A genuine RELEASE is a TRANSITION: the prior bar was squeezing and this bar is not.
    Labeling every non-squeezing bar "SQUEEZE_RELEASED" (the old behavior) made a name
    that was never coiling read as a fresh release on every bar — firing the bullish
    squeeze-release signal (and SOP Strategy 5's core trigger) on plain wide-band
    trending. We now require the squeeze→no-squeeze transition; bands that are simply
    wide report NO_SQUEEZE.
    """
    if len(bars) < bb_period + 2:
        return None

    closes = [b["c"] for b in bars]
    highs  = [b["h"] for b in bars]
    lows   = [b["l"] for b in bars]

    def _bands_at(end: int):
        """BB + Keltner bands for the bb_period window ending at index `end` (exclusive)."""
        cs  = closes[end - bb_period:end]
        mid = sum(cs) / bb_period
        std = (sum((c - mid) ** 2 for c in cs) / bb_period) ** 0.5
        bb_u, bb_l = mid + 2 * std, mid - 2 * std
        # True range uses each bar's PRIOR close, so the window starts one bar in.
        trs = [
            max(highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]))
            for i in range(end - bb_period, end)
        ]
        atr = sum(trs) / len(trs)
        return bb_u, bb_l, mid + kc_mult * atr, mid - kc_mult * atr

    bb_upper, bb_lower, kc_upper, kc_lower = _bands_at(len(bars))
    in_squeeze = bb_upper < kc_upper and bb_lower > kc_lower

    # Was the PRIOR bar squeezing? Release = the squeeze→no-squeeze transition.
    p_bb_u, p_bb_l, p_kc_u, p_kc_l = _bands_at(len(bars) - 1)
    was_squeezing = p_bb_u < p_kc_u and p_bb_l > p_kc_l
    released = was_squeezing and not in_squeeze

    # Momentum: compare last 2 bars relative to midpoint of recent range
    range_hi = max(highs[-bb_period:])
    range_lo = min(lows[-bb_period:])
    val      = closes[-1] - (range_hi + range_lo) / 2
    prev_val = closes[-2] - (max(highs[-bb_period - 1:-1]) + min(lows[-bb_period - 1:-1])) / 2
    momentum = "BULLISH" if val > prev_val else "BEARISH"

    if in_squeeze:
        signal = "SQUEEZE_ACTIVE — coiling, wait for release"
    elif released:
        signal = f"SQUEEZE_RELEASED_{momentum} — momentum building"
    else:
        signal = f"NO_SQUEEZE_{momentum} — bands wide, no fresh coil"

    return {
        "in_squeeze":         in_squeeze,
        "was_squeezing":      was_squeezing,
        "released":           released,
        "bb_width":           round(bb_upper - bb_lower, 4),
        "kc_width":           round(kc_upper - kc_lower, 4),
        "momentum_direction": momentum,
        "signal":             signal,
    }


def calc_fibonacci(bars: list, lookback: int = 50) -> dict | None:
    """
    Fibonacci retracement/extension levels from recent swing high/low.
    Key levels: 0.236, 0.382, 0.500, 0.618, 0.786.
    """
    if not bars:
        return None
    recent     = bars[-min(lookback, len(bars)):]
    swing_high = max(b["h"] for b in recent)
    swing_low  = min(b["l"] for b in recent)
    diff       = swing_high - swing_low
    current    = bars[-1]["c"]

    if diff == 0:
        return None

    levels = {
        "0.0":   round(swing_low, 4),
        "0.236": round(swing_low + 0.236 * diff, 4),
        "0.382": round(swing_low + 0.382 * diff, 4),
        "0.500": round(swing_low + 0.500 * diff, 4),
        "0.618": round(swing_low + 0.618 * diff, 4),
        "0.786": round(swing_low + 0.786 * diff, 4),
        "1.0":   round(swing_high, 4),
    }

    below   = {k: v for k, v in levels.items() if v <= current}
    above   = {k: v for k, v in levels.items() if v > current}
    near_s  = max(below.values()) if below else swing_low
    near_r  = min(above.values()) if above else swing_high
    pct_rng = round((current - swing_low) / diff * 100, 1)

    return {
        "swing_high":        swing_high,
        "swing_low":         swing_low,
        "levels":            levels,
        "nearest_support":   near_s,
        "nearest_resistance": near_r,
        "pct_of_range":      pct_rng,
        "dist_to_resistance_pct": round((near_r - current) / current * 100, 2),
        "dist_to_support_pct":    round((current - near_s) / current * 100, 2),
        "zone_label":        (
            "NEAR_SUPPORT" if pct_rng < 30 else
            "MID_RANGE"    if pct_rng < 70 else
            "NEAR_RESISTANCE"
        ),
    }


def calc_moving_averages(bars_data):
    """Full technical indicator suite from daily bars."""
    bars = bars_data.get("bars") or []
    closes = [b["c"] for b in bars]
    result = {}

    result["ema9"]  = calc_ema(closes, 9)
    result["ema21"] = calc_ema(closes, 21)
    result["ema50"] = calc_ema(closes, 50)

    if len(closes) >= 20:
        result["ma20"] = round(sum(closes[-20:]) / 20, 4)
    if len(closes) >= 50:
        result["ma50"] = round(sum(closes[-50:]) / 50, 4)

    if closes:
        result["latest_close"] = closes[-1]
        result["prev_close"] = closes[-2] if len(closes) >= 2 else None
        if result["prev_close"]:
            result["day_change_pct"] = round((closes[-1] - closes[-2]) / closes[-2] * 100, 2)

    result["rsi14"]        = calc_rsi(closes)
    result["macd"]         = calc_macd(closes)
    result["bollinger"]    = calc_bollinger_bands(closes)
    result["atr14"]        = calc_atr(bars)
    result["volume_spike"] = detect_volume_spike(bars)
    result["pivot_points"] = calc_pivot_points(bars)
    result["obv"]          = calc_obv(bars)
    result["bb_squeeze"]   = detect_bb_squeeze(bars)
    result["fibonacci"]    = calc_fibonacci(bars)

    if result.get("ema9") and result.get("ema21"):
        result["ema_signal"] = "BULLISH" if result["ema9"] > result["ema21"] else "BEARISH"

    # Composite bullish signal count (0-10 scale)
    signals = 0
    if result.get("ema_signal") == "BULLISH":                                                          signals += 1
    if result.get("rsi14") and 40 <= result["rsi14"] <= 70:                                            signals += 1
    if result.get("rsi14") and result["rsi14"] < 35:                                                   signals += 1  # oversold bounce
    if result.get("macd", {}) and result["macd"].get("crossover") in ("BULLISH_CROSS", "BULLISH_MOMENTUM"): signals += 1
    if result.get("bollinger", {}) and result["bollinger"].get("price_position") == "ABOVE_UPPER":     signals += 1
    if result.get("volume_spike", {}) and result["volume_spike"].get("spike"):                         signals += 1
    if result.get("obv", {}) and result["obv"].get("signal", "").startswith("BULLISH"):                signals += 1
    if result.get("bb_squeeze", {}) and "SQUEEZE_RELEASED_BULLISH" in result["bb_squeeze"].get("signal", ""): signals += 1
    if result.get("fibonacci", {}) and result["fibonacci"].get("zone_label") == "NEAR_SUPPORT":       signals += 1
    if result.get("pivot_points", {}) and result["pivot_points"].get("price_vs_pivot") == "ABOVE":    signals += 1
    result["bullish_signal_count"] = signals

    return result


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "account"
    symbol = sys.argv[2] if len(sys.argv) > 2 else None

    if action == "bars" and symbol:
        data = get_bars(symbol)
        mas = calc_moving_averages(data)
        data["moving_averages"] = mas
        print(json.dumps(data))
    elif action == "hourly" and symbol:
        print(json.dumps(get_hourly_bars(symbol)))
    elif action == "intraday" and symbol:
        data = get_intraday_bars(symbol)
        if "vwap" not in data:
            data["vwap"] = calc_vwap(data.get("bars") or [])
        print(json.dumps(data))
    elif action == "news" and symbol:
        print(json.dumps(get_news(symbol)))
    elif action == "snapshot" and symbol:
        print(json.dumps(get_snapshot(symbol)))
    elif action == "positions":
        print(json.dumps(get_positions()))
    elif action == "account":
        print(json.dumps(get_account()))
    else:
        print(json.dumps({"error": f"Unknown action: {action}"}))
        sys.exit(1)
