"""
Marketwide discovery scanner — FeintTrade.

Surfaces trending, liquid, TRADABLE symbols beyond the static watchlist so the
agent can hunt the whole market, not just 19 names. Sources:
  • Alpaca most-active stocks         (/v1beta1/screener/stocks/most-actives)
  • Alpaca top movers (gainers/losers) (/v1beta1/screener/stocks/movers)
  • CoinGecko trending coins           (mapped to Alpaca crypto pairs)

Every candidate is filtered for Alpaca tradability, a dollar-volume floor, and a
sane price band (from watchlist.json "discovery" config), then ranked. Output is
injected into research/cycle prompts and is available via CLI.

CLI:
  python screener.py brief        # formatted text for Claude prompts
  python screener.py discover     # full ranked JSON
"""

import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)

sys.path.insert(0, str(ROOT / "scripts"))
from common import load_watchlist, normalize_symbol  # noqa: E402

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ALPACA_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_BASE_URL", "https://paper-api.alpaca.markets")
DATA_URL = "https://data.alpaca.markets"
HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

# CoinGecko id -> Alpaca pair (only pairs Alpaca actually lists)
_CG_TO_ALPACA = {
    "bitcoin": "BTC/USD", "ethereum": "ETH/USD", "solana": "SOL/USD",
    "dogecoin": "DOGE/USD", "avalanche-2": "AVAX/USD", "chainlink": "LINK/USD",
    "ripple": "XRP/USD", "litecoin": "LTC/USD", "bitcoin-cash": "BCH/USD",
    "uniswap": "UNI/USD", "aave": "AAVE/USD", "polkadot": "DOT/USD",
    "shiba-inu": "SHIB/USD", "the-graph": "GRT/USD", "maker": "MKR/USD",
}


def _cfg():
    return load_watchlist().get("discovery", {}) or {}


def _watchlist_syms():
    return {normalize_symbol(s["symbol"]) for s in load_watchlist().get("watchlist", [])}


# ── Alpaca asset tradability ────────────────────────────────────────────────────
_asset_cache = {}


def _asset_ok(symbol):
    """True if symbol is an active, tradable Alpaca asset. Cached per run."""
    if symbol in _asset_cache:
        return _asset_cache[symbol]
    ok = False
    try:
        r = requests.get(f"{BASE_URL}/v2/assets/{symbol}", headers=HEADERS, timeout=8)
        if r.ok:
            a = r.json()
            ok = a.get("tradable", False) and a.get("status") == "active"
    except Exception:
        ok = False
    _asset_cache[symbol] = ok
    return ok


# ── Sources ─────────────────────────────────────────────────────────────────────
def _most_actives(top=20):
    try:
        r = requests.get(f"{DATA_URL}/v1beta1/screener/stocks/most-actives",
                         headers=HEADERS, params={"by": "volume", "top": top}, timeout=12)
        r.raise_for_status()
        return [m.get("symbol") for m in r.json().get("most_actives", []) if m.get("symbol")]
    except Exception:
        return []


def _movers(top=20):
    try:
        r = requests.get(f"{DATA_URL}/v1beta1/screener/stocks/movers",
                         headers=HEADERS, params={"top": top}, timeout=12)
        r.raise_for_status()
        d = r.json()
        gainers = [(m.get("symbol"), m.get("percent_change")) for m in d.get("gainers", [])]
        losers  = [(m.get("symbol"), m.get("percent_change")) for m in d.get("losers", [])]
        return gainers, losers
    except Exception:
        return [], []


def _stock_snapshots(symbols):
    """Latest price + daily volume for a batch of stock symbols."""
    if not symbols:
        return {}
    try:
        r = requests.get(f"{DATA_URL}/v2/stocks/snapshots", headers=HEADERS,
                         params={"symbols": ",".join(symbols[:80]), "feed": "iex"}, timeout=15)
        r.raise_for_status()
        out = {}
        for sym, snap in r.json().items():
            db = snap.get("dailyBar") or {}
            lt = (snap.get("latestTrade") or {}).get("p") or db.get("c")
            vol = db.get("v")
            if lt:
                out[sym] = {"price": float(lt), "volume": float(vol or 0)}
        return out
    except Exception:
        return {}


def _coingecko_trending():
    try:
        r = requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=12)
        r.raise_for_status()
        out = []
        for c in r.json().get("coins", []):
            item = c.get("item", {})
            cid = item.get("id")
            pair = _CG_TO_ALPACA.get(cid)
            if pair:
                out.append({"symbol": pair, "name": item.get("name"),
                            "rank": item.get("market_cap_rank")})
        return out
    except Exception:
        return []


# ── Discovery ─────────────────────────────────────────────────────────────────
def discover():
    """Return ranked candidate symbols NOT already on the watchlist."""
    cfg = _cfg()
    if not cfg.get("enabled", True):
        return {"enabled": False, "candidates": []}

    min_dv   = cfg.get("min_dollar_volume", 20_000_000)
    min_px   = cfg.get("min_price", 1.5)
    max_px   = cfg.get("max_price", 2000)
    max_n    = cfg.get("max_candidates", 12)
    own      = _watchlist_syms()

    penny         = cfg.get("penny_risk", {}) or {}
    penny_ceiling = penny.get("low_price_ceiling", 5.0)
    pump_gain     = penny.get("exclude_pump_gain_pct", 60.0)
    caution_gain  = penny.get("caution_gain_pct", 25.0)
    min_share_vol = penny.get("min_share_volume", 300_000)

    actives = _most_actives()
    gainers, losers = _movers()
    gain_map = {s: p for s, p in gainers}
    loss_map = {s: p for s, p in losers}

    stock_syms = list(dict.fromkeys(actives + list(gain_map) + list(loss_map)))
    snaps = _stock_snapshots(stock_syms)

    candidates = []
    excluded_penny = 0
    for sym in stock_syms:
        if not sym or normalize_symbol(sym) in own:
            continue
        snap = snaps.get(sym)
        if not snap:
            continue
        price, vol = snap["price"], snap["volume"]
        dollar_vol = price * vol
        if price < min_px or price > max_px or dollar_vol < min_dv:
            continue
        if not _asset_ok(sym):
            continue
        day_chg = gain_map.get(sym, loss_map.get(sym))
        # Penny manipulation/liquidity guards (ported from FeintTrade): a cheap name
        # spiking hard on the day is pump-and-dump-prone; a low-priced name on thin
        # share volume is untrustworthy. Drop those outright; flag the merely-elevated.
        is_penny = price < penny_ceiling
        if is_penny and ((day_chg is not None and day_chg >= pump_gain) or vol < min_share_vol):
            excluded_penny += 1
            continue
        reasons = []
        score = 0
        if sym in actives:
            score += 2; reasons.append("high volume")
        if sym in gain_map:
            score += 3; reasons.append(f"gainer {gain_map[sym]:+.1f}%")
        if sym in loss_map:
            score += 1; reasons.append(f"loser {loss_map[sym]:+.1f}% (reversal watch)")
        penny_caution = bool(is_penny and day_chg is not None and day_chg >= caution_gain)
        if penny_caution:
            score -= 2; reasons.append("⚠️ penny pump caution")
        candidates.append({
            "symbol": sym, "type": "equity", "price": round(price, 2),
            "dollar_volume": round(dollar_vol), "day_change_pct": day_chg,
            "score": score, "reason": ", ".join(reasons) or "active",
            "penny_caution": penny_caution,
        })

    # Crypto trending (already-known liquid pairs; tradability assumed for mapped set)
    for c in _coingecko_trending():
        if normalize_symbol(c["symbol"]) in own:
            continue
        candidates.append({
            "symbol": c["symbol"], "type": "crypto", "price": None,
            "score": 2, "reason": f"CoinGecko trending ({c.get('name')})",
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return {
        "enabled": True,
        "default_max_alloc_pct": cfg.get("default_max_alloc_pct", 12),
        "filters": {"min_dollar_volume": min_dv, "price_band": [min_px, max_px],
                    "penny_excluded": excluded_penny},
        "candidates": candidates[:max_n],
    }


def get_discovery_brief():
    """Formatted candidate list for injection into Claude prompts."""
    try:
        d = discover()
    except Exception as e:
        return f"=== MARKETWIDE DISCOVERY: unavailable ({e}) ==="
    if not d.get("enabled"):
        return "=== MARKETWIDE DISCOVERY: disabled ==="
    cands = d.get("candidates", [])
    if not cands:
        return "=== MARKETWIDE DISCOVERY ===\nNo qualifying trending names right now — trade the core watchlist."
    lines = [
        "=== MARKETWIDE DISCOVERY (trending, tradable, beyond the watchlist) ===",
        "These are NOT pre-vetted. Apply the FULL SOP (regime fit, 3+ signals, R:R>=2:1) before any entry.",
        f"Unlisted names are capped at {d.get('default_max_alloc_pct')}% allocation by the risk engine.",
    ]
    pe = d.get("filters", {}).get("penny_excluded", 0)
    if pe:
        lines.append(f"⚠️ Filtered {pe} low-priced pump/illiquid name(s) for manipulation risk.")
    lines.append("")
    for c in cands:
        if c["type"] == "equity":
            lines.append(f"  {c['symbol']:<6} ${c.get('price','?'):<8} "
                         f"${c.get('dollar_volume',0)/1e6:.0f}M vol | {c['reason']}")
        else:
            lines.append(f"  {c['symbol']:<9} (crypto) | {c['reason']}")
    return "\n".join(lines)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "brief"
    if cmd == "brief":
        print(get_discovery_brief())
    elif cmd == "discover":
        print(json.dumps(discover(), indent=2))
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
