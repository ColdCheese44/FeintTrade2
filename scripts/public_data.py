"""
Free public-API market data (from github.com/public-apis/public-apis).

Used as resilience FALLBACKS and a NEW macro signal. Every source here is free and
requires NO API key (verified reachable from this environment):

  • Coinbase spot      (api.coinbase.com)        — crypto price
  • CoinGecko simple   (api.coingecko.com)       — crypto price fallback
  • Frankfurter / ECB  (api.frankfurter.app)     — FX rates → a free USD-strength (DXY proxy)

Crypto price: Coinbase → CoinGecko fallback. (Binance was geo-blocked 451 and Stooq 404'd
from here, so equities stay on the existing Alpaca→yfinance path.) Everything fails soft
to None / {} — this is supplementary, never a hard dependency.
"""

import requests

_TIMEOUT = 8
_H = {"User-Agent": "FeintTrade/1.0"}

_CG_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "DOGE": "dogecoin",
    "AVAX": "avalanche-2", "LINK": "chainlink", "XRP": "ripple", "LTC": "litecoin",
    "BCH": "bitcoin-cash", "UNI": "uniswap", "ADA": "cardano", "MATIC": "matic-network",
}

# Baseline USD/x rates (~2024) to index the USD-strength proxy against.
_FX_BASELINE = {"EUR": 0.92, "GBP": 0.79, "JPY": 150.0, "CAD": 1.36, "AUD": 1.52, "CHF": 0.90}


def _base(symbol) -> str:
    s = str(symbol).upper().replace("/", "")
    for q in ("USDT", "USDC", "USD"):
        if s.endswith(q):
            return s[:-len(q)]
    return s


def coinbase_price(symbol):
    try:
        b = _base(symbol)
        r = requests.get(f"https://api.coinbase.com/v2/prices/{b}-USD/spot", timeout=_TIMEOUT, headers=_H)
        r.raise_for_status()
        return float(r.json()["data"]["amount"])
    except Exception:
        return None


def coingecko_price(symbol):
    cid = _CG_IDS.get(_base(symbol))
    if not cid:
        return None
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                         params={"ids": cid, "vs_currencies": "usd"}, timeout=_TIMEOUT, headers=_H)
        r.raise_for_status()
        return float(r.json()[cid]["usd"])
    except Exception:
        return None


def crypto_price(symbol):
    """Free crypto spot price (Coinbase → CoinGecko). None if both fail."""
    return coinbase_price(symbol) or coingecko_price(symbol)


def fx_rates(base="USD", quotes=("EUR", "GBP", "JPY", "CAD", "AUD", "CHF")) -> dict:
    try:
        r = requests.get("https://api.frankfurter.app/latest",
                         params={"from": base, "to": ",".join(quotes)}, timeout=_TIMEOUT, headers=_H)
        r.raise_for_status()
        return r.json().get("rates", {})
    except Exception:
        return {}


def usd_strength(rates=None):
    """Free DXY-proxy: mean of USD vs majors, indexed to 100 at the baseline. Higher =
    stronger USD (a risk-off headwind for risk assets). None if unavailable.
    Pass `rates` for hermetic tests."""
    rates = rates if rates is not None else fx_rates()
    if not rates:
        return None
    ratios = [rates[k] / _FX_BASELINE[k] for k in _FX_BASELINE if k in rates and _FX_BASELINE[k]]
    if not ratios:
        return None
    return round(sum(ratios) / len(ratios) * 100, 1)


def macro_brief(strength=None) -> str:
    """One-line USD-strength macro signal for prompt injection. Empty when unavailable."""
    s = strength if strength is not None else usd_strength()
    if s is None:
        return ""
    bias = ("risk-OFF headwind (strong USD)" if s >= 103
            else "risk-ON tailwind (weak USD)" if s <= 98 else "neutral USD")
    return f"USD strength index ~{s} (baseline 100) → {bias}."


def providers_status() -> dict:
    return {
        "coinbase": coinbase_price("BTC/USD") is not None,
        "coingecko": coingecko_price("BTC/USD") is not None,
        "frankfurter": bool(fx_rates()),
    }


if __name__ == "__main__":
    print("BTC (Coinbase):", coinbase_price("BTC/USD"))
    print("ETH (CoinGecko):", coingecko_price("ETH/USD"))
    print("USD strength:", usd_strength(), "·", macro_brief())
    print("providers:", providers_status())
