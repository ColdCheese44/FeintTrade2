"""Supplementary research: yfinance, NewsAPI, Finnhub, FRED, CNN Fear & Greed, SEC EDGAR."""

import os
import sys
import json
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

EDGAR_HEADERS = {"User-Agent": "MindHub Trader brendan.t.dodd@gmail.com"}

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

NEWSAPI_KEY = os.getenv("NEWSAPI_API_KEY")
FRED_KEY = os.getenv("FRED_API_KEY")
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")

# Human-readable names for NewsAPI queries (ticker searches return poor results)
COMPANY_NAMES = {
    "TQQQ": "Nasdaq technology market QQQ",
    "SOXL": "semiconductor stocks SOX",
    "NVDA": "Nvidia",
    "AMD": "AMD Advanced Micro Devices",
    "MSTR": "MicroStrategy Bitcoin",
    "BTC/USD": "Bitcoin cryptocurrency",
    "ETH/USD": "Ethereum cryptocurrency",
    "SOL/USD": "Solana cryptocurrency",
    "DOGE/USD": "Dogecoin cryptocurrency",
    "AVAX/USD": "Avalanche AVAX cryptocurrency",
}


def get_fundamentals(symbol):
    """yfinance: P/E, market cap, analyst target, earnings date, beta, short ratio."""
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        earnings_ts = info.get("earningsTimestamp")
        earnings_date = (
            datetime.fromtimestamp(earnings_ts, tz=timezone.utc).strftime("%Y-%m-%d")
            if earnings_ts else None
        )
        return {
            "pe_ratio_trailing": info.get("trailingPE"),
            "pe_ratio_forward": info.get("forwardPE"),
            "market_cap": info.get("marketCap"),
            "week_52_high": info.get("fiftyTwoWeekHigh"),
            "week_52_low": info.get("fiftyTwoWeekLow"),
            "analyst_target_mean": info.get("targetMeanPrice"),
            "analyst_recommendation": info.get("recommendationKey"),
            "earnings_date": earnings_date,
            "revenue_growth_yoy": info.get("revenueGrowth"),
            "profit_margin": info.get("profitMargins"),
            "beta": info.get("beta"),
            "short_ratio": info.get("shortRatio"),
            "long_name": info.get("longName"),
        }
    except Exception as e:
        return {"error": str(e)}


def get_finnhub_sentiment(symbol):
    """Finnhub: buzz score and bullish/bearish sentiment percentages."""
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/news-sentiment",
            params={"symbol": symbol, "token": FINNHUB_KEY},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        return {
            "buzz_score": data.get("buzz", {}).get("buzz"),
            "articles_this_week": data.get("buzz", {}).get("articlesInLastWeek"),
            "bullish_pct": data.get("sentiment", {}).get("bullishPercent"),
            "bearish_pct": data.get("sentiment", {}).get("bearishPercent"),
            "company_news_score": data.get("companyNewsScore"),
            "sector_avg_bullish_pct": data.get("sectorAverageBullishPercent"),
        }
    except Exception as e:
        return {"error": str(e)}


def get_finnhub_recommendations(symbol):
    """Finnhub: latest analyst buy/hold/sell counts."""
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/stock/recommendation",
            params={"symbol": symbol, "token": FINNHUB_KEY},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data:
            latest = data[0]
            return {
                "period": latest.get("period"),
                "strong_buy": latest.get("strongBuy"),
                "buy": latest.get("buy"),
                "hold": latest.get("hold"),
                "sell": latest.get("sell"),
                "strong_sell": latest.get("strongSell"),
            }
        return {}
    except Exception as e:
        return {"error": str(e)}


def get_earnings_calendar(symbol):
    """Finnhub: next earnings date and EPS estimate."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        future = (datetime.now(timezone.utc) + timedelta(days=90)).strftime("%Y-%m-%d")
        r = requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={"symbol": symbol, "from": today, "to": future, "token": FINNHUB_KEY},
            timeout=10,
        )
        r.raise_for_status()
        earnings = r.json().get("earningsCalendar", [])
        if earnings:
            nxt = earnings[0]
            days = (datetime.strptime(nxt["date"], "%Y-%m-%d") - datetime.now()).days if nxt.get("date") else None
            return {
                "next_earnings_date": nxt.get("date"),
                "eps_estimate": nxt.get("epsEstimate"),
                "days_until_earnings": days,
            }
        return {"next_earnings_date": None, "days_until_earnings": None}
    except Exception as e:
        return {"error": str(e)}


def get_newsapi(symbol):
    """NewsAPI: 5 most recent articles for a symbol."""
    try:
        query = COMPANY_NAMES.get(symbol, symbol)
        r = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": query,
                "apiKey": NEWSAPI_KEY,
                "pageSize": 5,
                "sortBy": "publishedAt",
                "language": "en",
            },
            timeout=10,
        )
        r.raise_for_status()
        articles = r.json().get("articles", [])
        return [
            {
                "source": a.get("source", {}).get("name"),
                "title": a.get("title"),
                "description": a.get("description"),
                "published": a.get("publishedAt", "")[:10],
            }
            for a in articles
        ]
    except Exception as e:
        return {"error": str(e)}


def get_macro():
    """FRED: Fed funds rate, CPI, unemployment, 10Y treasury, VIX."""
    series = {
        "fed_funds_rate": "FEDFUNDS",
        "cpi": "CPIAUCSL",
        "unemployment_rate": "UNRATE",
        "treasury_10y_yield": "GS10",
        "vix": "VIXCLS",
    }
    results = {}
    for name, series_id in series.items():
        try:
            r = requests.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    "series_id": series_id,
                    "api_key": FRED_KEY,
                    "sort_order": "desc",
                    "limit": 2,
                    "file_type": "json",
                },
                timeout=10,
            )
            r.raise_for_status()
            obs = r.json().get("observations", [])
            if obs:
                results[name] = {
                    "value": obs[0].get("value"),
                    "date": obs[0].get("date"),
                    "prev_value": obs[1].get("value") if len(obs) > 1 else None,
                }
        except Exception as e:
            results[name] = {"error": str(e)}
    return results


def get_sec_filings(symbol):
    """SEC EDGAR: 5 most recent 8-K/10-Q/10-K filings for a symbol."""
    try:
        # Resolve ticker → CIK using EDGAR's ticker map
        tickers_r = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=EDGAR_HEADERS,
            timeout=10,
        )
        tickers_r.raise_for_status()
        tickers = tickers_r.json()
        cik = None
        for entry in tickers.values():
            if entry.get("ticker", "").upper() == symbol.upper():
                cik = str(entry["cik_str"]).zfill(10)
                break
        if not cik:
            return {"error": f"CIK not found for {symbol}"}

        # Fetch submission history
        sub_r = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers=EDGAR_HEADERS,
            timeout=10,
        )
        sub_r.raise_for_status()
        filings = sub_r.json().get("filings", {}).get("recent", {})

        forms = filings.get("form", [])
        dates = filings.get("filingDate", [])
        descriptions = filings.get("primaryDocDescription", [])
        accessions = filings.get("accessionNumber", [])

        target_forms = {"8-K", "10-Q", "10-K", "10-K/A", "10-Q/A"}
        results = []
        for form, date, desc, acc in zip(forms, dates, descriptions, accessions):
            if form in target_forms:
                results.append({
                    "form": form,
                    "date": date,
                    "description": desc or form,
                    "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form}&dateb=&owner=include&count=5",
                })
                if len(results) == 5:
                    break

        return {"cik": cik, "recent_filings": results}
    except Exception as e:
        return {"error": str(e)}


OKX_SYMBOLS = {
    "BTC/USD":  "BTC-USD-SWAP",
    "ETH/USD":  "ETH-USD-SWAP",
    "SOL/USD":  "SOL-USD-SWAP",
    "DOGE/USD": "DOGE-USD-SWAP",
    "AVAX/USD": "AVAX-USD-SWAP",
}


def get_funding_rates():
    """OKX perpetual funding rates — shows crowded long/short positioning.
    Positive rate: longs pay shorts (bullish crowding, squeeze risk).
    Negative rate: shorts pay longs (bearish crowding, potential bounce).
    Extreme values (>0.1% or <-0.1%) signal overextended positioning.
    """
    results = {}
    for symbol, okx_sym in OKX_SYMBOLS.items():
        try:
            r = requests.get(
                "https://www.okx.com/api/v5/public/funding-rate",
                params={"instId": okx_sym},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json().get("data", [])
            if data:
                latest    = float(data[0]["fundingRate"]) * 100
                next_raw  = data[0].get("nextFundingRate", "")
                next_rate = float(next_raw) * 100 if next_raw else None
                results[symbol] = {
                    "funding_rate_pct":      round(latest, 4),
                    "next_funding_rate_pct": round(next_rate, 4) if next_rate is not None else None,
                    "next_funding_time":     data[0].get("nextFundingTime"),
                    "signal": (
                        "EXTREME_LONG_CROWDING"  if latest >  0.1  else
                        "LONG_CROWDING"          if latest >  0.05 else
                        "EXTREME_SHORT_CROWDING" if latest < -0.1  else
                        "SHORT_CROWDING"         if latest < -0.05 else
                        "NEUTRAL"
                    ),
                    "interpretation": (
                        "Longs very crowded — squeeze risk, consider fading"    if latest >  0.1  else
                        "Longs crowded — be cautious adding long exposure"       if latest >  0.05 else
                        "Shorts very crowded — potential short squeeze catalyst" if latest < -0.1  else
                        "Shorts crowded — bounce setup if catalyst appears"      if latest < -0.05 else
                        "Neutral positioning — no crowding signal"
                    ),
                }
        except Exception as e:
            results[symbol] = {"error": str(e)}
    return results


CRYPTOCOMPARE_COINS = {
    "BTC/USD":  "BTC",
    "ETH/USD":  "ETH",
    "SOL/USD":  "SOL",
    "DOGE/USD": "DOGE",
    "AVAX/USD": "AVAX",
}


def get_wsb_sentiment():
    """CryptoCompare social stats — Reddit + Twitter volume and sentiment for crypto.
    Free tier, no API key required for basic social data.
    """
    results = {}
    for symbol, coin in CRYPTOCOMPARE_COINS.items():
        try:
            r = requests.get(
                "https://min-api.cryptocompare.com/data/social/coin/latest",
                params={"coinId": coin, "extraParams": "MindHubTrader"},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json().get("Data", {})
            reddit  = data.get("Reddit", {})
            twitter = data.get("Twitter", {})
            results[symbol] = {
                "reddit_posts_per_hour":    reddit.get("posts_per_hour"),
                "reddit_comments_per_hour": reddit.get("comments_per_hour"),
                "reddit_active_users":      reddit.get("active_users"),
                "twitter_followers":        twitter.get("followers"),
                "twitter_statuses_per_day": twitter.get("statuses_per_day"),
            }
        except Exception as e:
            results[symbol] = {"error": str(e)}
    return results


def get_crypto_fear_greed():
    """Alternative.me Crypto Fear & Greed Index — more relevant than CNN for crypto."""
    try:
        r = requests.get(
            "https://api.alternative.me/fng/?limit=3",
            timeout=10,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            return {"error": "no data"}
        current = data[0]
        prev    = data[1] if len(data) > 1 else {}
        week    = data[2] if len(data) > 2 else {}
        score = int(current.get("value", 0))
        trend = "IMPROVING" if score > int(prev.get("value", score)) else "WORSENING"
        return {
            "score":           score,
            "rating":          current.get("value_classification"),
            "prev_day_score":  int(prev.get("value", 0)) if prev else None,
            "prev_week_score": int(week.get("value", 0)) if week else None,
            "trend":           trend,
            "interpretation":  (
                "EXTREME_BUY_ZONE" if score <= 20 else
                "BUY_ZONE"         if score <= 40 else
                "NEUTRAL"          if score <= 60 else
                "SELL_ZONE"        if score <= 80 else
                "EXTREME_SELL_ZONE"
            ),
        }
    except Exception as e:
        return {"error": str(e)}


def get_coingecko():
    """CoinGecko: BTC dominance, global market cap, trending coins, top movers."""
    try:
        global_r = requests.get(
            "https://api.coingecko.com/api/v3/global",
            timeout=10,
        )
        global_r.raise_for_status()
        gdata = global_r.json().get("data", {})

        trending_r = requests.get(
            "https://api.coingecko.com/api/v3/search/trending",
            timeout=10,
        )
        trending_r.raise_for_status()
        trending = [
            {
                "name":   c["item"]["name"],
                "symbol": c["item"]["symbol"],
                "rank":   c["item"]["market_cap_rank"],
                "price_change_24h": c["item"].get("data", {}).get("price_change_percentage_24h", {}).get("usd"),
            }
            for c in trending_r.json().get("coins", [])[:5]
        ]

        mcap_pct = gdata.get("market_cap_percentage", {})
        return {
            "total_market_cap_usd":    gdata.get("total_market_cap", {}).get("usd"),
            "total_volume_24h_usd":    gdata.get("total_volume", {}).get("usd"),
            "market_cap_change_24h":   round(gdata.get("market_cap_change_percentage_24h_usd", 0), 2),
            "btc_dominance":           round(mcap_pct.get("btc", 0), 2),
            "eth_dominance":           round(mcap_pct.get("eth", 0), 2),
            "trending_coins":          trending,
        }
    except Exception as e:
        return {"error": str(e)}


def get_fear_greed():
    """CNN Fear & Greed Index — current score, rating, and trend."""
    try:
        r = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        r.raise_for_status()
        fg = r.json().get("fear_and_greed", {})
        return {
            "score": round(float(fg.get("score", 0)), 1),
            "rating": fg.get("rating"),
            "prev_close": round(float(fg.get("previous_close", 0)), 1),
            "prev_1_week": round(float(fg.get("previous_1_week", 0)), 1),
            "prev_1_month": round(float(fg.get("previous_1_month", 0)), 1),
        }
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    symbol = sys.argv[2] if len(sys.argv) > 2 else None

    dispatch = {
        "fundamentals":    lambda: get_fundamentals(symbol),
        "sentiment":       lambda: get_finnhub_sentiment(symbol),
        "recommendations": lambda: get_finnhub_recommendations(symbol),
        "earnings":        lambda: get_earnings_calendar(symbol),
        "news":            lambda: get_newsapi(symbol),
        "sec":             lambda: get_sec_filings(symbol),
        "macro":           get_macro,
        "feargreed":       get_fear_greed,
        "cryptofg":        get_crypto_fear_greed,
        "coingecko":       get_coingecko,
        "funding":         get_funding_rates,
        "wsb":             get_wsb_sentiment,
    }

    fn = dispatch.get(action)
    if fn and (symbol or action in ("macro", "feargreed", "cryptofg", "coingecko", "funding", "wsb")):
        print(json.dumps(fn()))
    else:
        print(json.dumps({"error": f"Unknown action or missing symbol: {action}"}))
        sys.exit(1)
