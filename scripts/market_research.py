"""
Hourly market-research engine — FeintTrade.

Pulls a wide read of the market from FREE sources only (no paid APIs) and
synthesizes it into a concise research brief + concrete strategy adjustments that
the trading routines read on every cycle. This is the "continuously develop and
refine strategies" loop: macro/sector/crypto context → actionable bias.

FREE sources used:
  • yfinance  — indices (SPY/QQQ/IWM/DIA), VIX, 11 sector ETFs, 10y/2y yields,
                breadth, and recent news headlines
  • CoinGecko — global market cap, BTC dominance, trending coins (no key)
  • alternative.me — crypto Fear & Greed (no key)

CLI:
  python scripts/market_research.py          # gather + synthesize + write brief
  python scripts/market_research.py data      # print raw gathered data (no model)

Writes:
  data/market_research.md   — human/agent-readable brief (read by the orchestrator)
  data/market_research.json — structured snapshot + metadata

The orchestrator imports ONLY get_market_research_brief() (a file read), so there
is no circular import and a cycle never blocks on this module.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)

# An empty ANTHROPIC_AUTH_TOKEN makes the SDK send an illegal 'Bearer ' header.
if not (os.getenv("ANTHROPIC_AUTH_TOKEN") or "").strip():
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

sys.path.insert(0, str(ROOT / "scripts"))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from common import now_mt_str, mt_tz_label, make_http_session
except Exception:
    def now_mt_str(fmt="%Y-%m-%d %H:%M"): return datetime.now().strftime(fmt)
    def mt_tz_label(): return "MT"
    def make_http_session(): return requests.Session()

BRIEF_FILE = ROOT / "data" / "market_research.md"
DATA_FILE = ROOT / "data" / "market_research.json"
BRIEF_FILE.parent.mkdir(exist_ok=True)

_HTTP = make_http_session()

SECTOR_ETFS = {
    "XLK": "Technology", "XLF": "Financials", "XLE": "Energy", "XLV": "Healthcare",
    "XLY": "Cons. Disc.", "XLI": "Industrials", "XLP": "Cons. Staples",
    "XLU": "Utilities", "XLB": "Materials", "XLRE": "Real Estate", "XLC": "Comm. Svcs",
}
INDICES = {"SPY": "S&P 500", "QQQ": "Nasdaq 100", "IWM": "Russell 2000", "DIA": "Dow"}


# ── Free data gathering ───────────────────────────────────────────────────────
def _yf_quote_block(tickers: dict) -> dict:
    """Last price, 1d % change, and trend vs 20/50-day SMA for a set of tickers."""
    out = {}
    try:
        import yfinance as yf
        data = yf.download(list(tickers), period="3mo", interval="1d",
                           progress=False, group_by="ticker", threads=True)
        for sym in tickers:
            try:
                df = data[sym] if sym in data.columns.get_level_values(0) else None
                if df is None or df.empty:
                    continue
                closes = df["Close"].dropna()
                if len(closes) < 2:
                    continue
                last = float(closes.iloc[-1])
                prev = float(closes.iloc[-2])
                chg = (last - prev) / prev * 100 if prev else 0.0
                sma20 = float(closes.tail(20).mean()) if len(closes) >= 20 else last
                sma50 = float(closes.tail(50).mean()) if len(closes) >= 50 else last
                out[sym] = {
                    "name": tickers[sym],
                    "price": round(last, 2),
                    "chg_pct": round(chg, 2),
                    "above_sma20": last > sma20,
                    "above_sma50": last > sma50,
                    "pct_from_sma50": round((last - sma50) / sma50 * 100, 1) if sma50 else 0.0,
                }
            except Exception:
                continue
    except Exception as e:
        out["_error"] = str(e)[:120]
    return out


def _yf_vix_and_rates() -> dict:
    out = {}
    try:
        import yfinance as yf
        for sym, key in (("^VIX", "vix"), ("^TNX", "us10y"), ("^IRX", "us13w")):
            try:
                h = yf.Ticker(sym).history(period="5d")
                if not h.empty:
                    out[key] = round(float(h["Close"].dropna().iloc[-1]), 2)
            except Exception:
                continue
    except Exception as e:
        out["_error"] = str(e)[:120]
    return out


def _yf_news(tickers=("SPY", "QQQ", "NVDA", "BTC-USD"), limit=8) -> list:
    headlines = []
    try:
        import yfinance as yf
        for t in tickers:
            try:
                for n in (yf.Ticker(t).news or [])[:3]:
                    title = (n.get("content", {}) or {}).get("title") or n.get("title")
                    if title and title not in headlines:
                        headlines.append(title)
            except Exception:
                continue
    except Exception:
        pass
    return headlines[:limit]


def _coingecko() -> dict:
    out = {}
    try:
        g = _HTTP.get("https://api.coingecko.com/api/v3/global", timeout=12).json().get("data", {})
        out["total_mcap_usd"] = g.get("total_market_cap", {}).get("usd")
        out["mcap_chg_24h_pct"] = round(g.get("market_cap_change_percentage_24h_usd", 0) or 0, 2)
        out["btc_dominance_pct"] = round(g.get("market_cap_percentage", {}).get("btc", 0) or 0, 2)
        out["eth_dominance_pct"] = round(g.get("market_cap_percentage", {}).get("eth", 0) or 0, 2)
    except Exception as e:
        out["_error"] = str(e)[:120]
    try:
        tr = _HTTP.get("https://api.coingecko.com/api/v3/search/trending", timeout=12).json()
        out["trending"] = [c["item"]["symbol"].upper() for c in tr.get("coins", [])[:7]]
    except Exception:
        out["trending"] = []
    return out


def _crypto_fear_greed() -> dict:
    try:
        d = _HTTP.get("https://api.alternative.me/fng/?limit=1", timeout=12).json()["data"][0]
        return {"value": int(d["value"]), "label": d["value_classification"]}
    except Exception as e:
        return {"_error": str(e)[:120]}


def gather_free_market_data() -> dict:
    indices = _yf_quote_block(INDICES)
    sectors = _yf_quote_block(SECTOR_ETFS)
    rates = _yf_vix_and_rates()
    green_sectors = [s for s, v in sectors.items() if isinstance(v, dict) and v.get("chg_pct", 0) > 0]
    breadth = round(len(green_sectors) / max(1, len([v for v in sectors.values() if isinstance(v, dict)])) * 100, 0)
    return {
        "timestamp_mt": now_mt_str(),
        "indices": indices,
        "sectors": sectors,
        "sector_breadth_pct_green": breadth,
        "vix_and_rates": rates,
        "crypto": _coingecko(),
        "crypto_fear_greed": _crypto_fear_greed(),
        "headlines": _yf_news(),
    }


# ── Synthesis (cheap model) ───────────────────────────────────────────────────
def _model_cfg():
    try:
        cfg = json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8")).get("api_config", {})
        model = cfg.get("models", {}).get("market_research", "claude-haiku-4-5-20251001")
        max_tokens = cfg.get("max_tokens", {}).get("market_research", 1100)
        return model, max_tokens
    except Exception:
        return "claude-haiku-4-5-20251001", 1100


def synthesize(data: dict) -> str:
    """Turn the raw free-data snapshot into a concise, actionable research brief."""
    model, max_tokens = _model_cfg()
    prompt = f"""You are the market-research analyst for a SWING trading agent chasing aggressive
compounding on a small account. Synthesize the FREE-source market snapshot below into a tight,
actionable brief. Be concrete and decisive; no hedging filler.

SNAPSHOT (all free sources):
{json.dumps(data, indent=2, default=str)}

Write the brief with EXACTLY these short sections (markdown, no preamble):

## Macro Read
- Risk-on vs risk-off in one line (indices trend vs SMA20/50, VIX, yields). Name the regime.

## Sector Rotation
- Which 1-2 sectors are leading and which are lagging today; what that implies for our names
  (TQQQ/SOXL/FNGU=tech, LABU=biotech, NVDA/AMD=semis, etc.).

## Crypto Posture
- BTC dominance + market-cap trend + Fear&Greed → is crypto in a trend worth following or chopping?
  Remember: we only buy crypto on a CONFIRMED daily uptrend, never on 'extreme fear' alone.

## Today's Strategy Adjustments (the part the trader reads)
- 3-5 bullet, specific tilts for TODAY: which instruments/sectors to favor or avoid, whether to
  press or stay patient, any level/catalyst to watch. Tie each to the data above.

Keep the whole brief under ~350 words."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"),
                                     max_retries=4, timeout=90.0)
        resp = client.messages.create(
            model=model, max_tokens=max_tokens,
            system="You are a sharp, concise macro/market analyst. Output only the requested markdown.",
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        return f"_(Synthesis unavailable: {e}. Raw breadth {data.get('sector_breadth_pct_green')}% green, VIX {data.get('vix_and_rates', {}).get('vix')}.)_"


def run() -> str:
    data = gather_free_market_data()
    brief_body = synthesize(data)
    header = f"# Market Research Brief — {now_mt_str('%Y-%m-%d %H:%M')}\n"
    full = header + "\n" + brief_body + "\n"
    BRIEF_FILE.write_text(full, encoding="utf-8")
    DATA_FILE.write_text(json.dumps({"generated_mt": now_mt_str(), "data": data}, indent=2, default=str),
                         encoding="utf-8")
    print(f"Market research written → {BRIEF_FILE}")
    return full


# ── Reader (imported by the orchestrator) ─────────────────────────────────────
def get_market_research_brief(max_age_minutes: int = 180) -> str:
    """
    Return the latest research brief for prompt injection, or "" if missing/stale.
    Stale guard avoids feeding the trader an old macro read if the hourly task stalls.
    """
    try:
        if not BRIEF_FILE.exists():
            return ""
        meta = json.loads(DATA_FILE.read_text(encoding="utf-8")) if DATA_FILE.exists() else {}
        gen = meta.get("generated_mt")
        if gen:
            try:
                age = (datetime.now() - datetime.strptime(gen[:16], "%Y-%m-%d %H:%M")).total_seconds() / 60
                if age > max_age_minutes:
                    return ""
            except Exception:
                pass
        text = BRIEF_FILE.read_text(encoding="utf-8", errors="replace").strip()
        return ("=== HOURLY MARKET RESEARCH (free-source synthesis) ===\n" + text) if text else ""
    except Exception:
        return ""


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "data":
        print(json.dumps(gather_free_market_data(), indent=2, default=str))
    else:
        run()
