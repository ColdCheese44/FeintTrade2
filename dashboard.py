"""MindHub Trader — Streamlit dashboard."""

import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import anthropic
import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env", override=True)

ALPACA_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_BASE_URL", "https://paper-api.alpaca.markets")
HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

sys.path.insert(0, str(ROOT / "scripts"))

# Retry-resilient HTTP session so a brief VPN/DNS blip doesn't crash the dashboard.
try:
    from common import make_http_session
    _HTTP = make_http_session()
except Exception:
    _HTTP = requests

try:
    import discord_channels as _dch
except Exception:
    _dch = None

try:
    from common import (
        now_mt, mt_tz_label, market_phase, minutes_to_close,
        normalize_symbol, is_crypto, load_risk, load_watchlist as _load_wl,
        is_validation_mode, get_completed_trade_count_safe,
        research_mode_active, research_mode_enabled, set_research_mode,
    )
except ImportError:
    try:
        from common import (
            now_mt, mt_tz_label, market_phase, minutes_to_close,
            normalize_symbol, is_crypto, load_risk, load_watchlist as _load_wl,
        )
        def is_validation_mode(*a): return True
        def get_completed_trade_count_safe(): return 0
        try:
            from common import research_mode_active, research_mode_enabled, set_research_mode
        except Exception:
            def research_mode_active(): return False
            def research_mode_enabled(): return False
            def set_research_mode(enabled): return enabled
    except Exception:
        from datetime import datetime as _dt
        def now_mt(): return _dt.now()
        def mt_tz_label(): return "MT"
        def market_phase(): return "UNKNOWN"
        def minutes_to_close(): return None
        def normalize_symbol(s, a=None): return s
        def is_crypto(s, a=None): return "/" in str(s)
        def load_risk(): return {}
        def _load_wl(): return {}
        def is_validation_mode(*a): return True
        def get_completed_trade_count_safe(): return 0
        def research_mode_active(): return False
        def research_mode_enabled(): return False
        def set_research_mode(enabled): return enabled

# ── Dark-humor / Stonks flavor (learn trading without it feeling like learning) ──
STONKS_WISDOM = [
    ("Buy the dip.", "Adding to a position as price falls toward support — only works if your thesis is intact. Otherwise it's 'catching a falling knife.'"),
    ("Diamond hands 💎🙌", "Holding through volatility instead of panic-selling. Noble until it's a bagholding cope."),
    ("This is fine. 🔥", "What every trader says at -8%. The pros set the stop BEFORE they need the meme."),
    ("Stonks only go up 📈", "Survivorship bias in one phrase. Markets are mean-reverting until they aren't."),
    ("Time in the market > timing the market.", "...unless you're a day-trading degenerate, in which case: respect your stops."),
    ("The trend is your friend 🤝", "Until the end when it bends. Trade WITH the regime, not against it."),
    ("Cut losses short, let winners run.", "The entire game in six words. Everything else is decoration."),
    ("Be fearful when others are greedy.", "Fear & Greed at 'Extreme Greed'? That's the music slowing down."),
    ("Bears make money, bulls make money, pigs get slaughtered. 🐷", "Greed (oversizing, no stop) is the #1 account killer."),
    ("Risk comes from not knowing what you're doing.", "— Buffett. Position size is the only thing you fully control."),
    ("No position is also a position. 🧘", "Cash is a trade. 'No setup' is a valid, profitable decision."),
    ("Leverage is a hell of a drug. 💊", "3x ETFs decay. They are intraday tools, not investments. Respect the close."),
]

try:
    from assets.stonks_b64 import STONKS_B64 as _STONKS_B64
except Exception:
    _STONKS_B64 = None

def _stonks_html():
    """
    Faint full-page background watermark of the stonks meme — centered, large, and
    very low opacity so it's noticeable but never competes with the data. Painted on
    the app container's ::before pseudo-element (above the dark base, behind all
    content). Content is lifted to z-index:1 so nothing is obscured.
    """
    if _STONKS_B64:
        bg = f"url('data:image/jpeg;base64,{_STONKS_B64}')"
    else:
        bg = "none"
    return f"""
<style>
/* Make the app container see-through so the watermark (below) shows through the
   dark base set on html/body, while staying behind the content. */
[data-testid="stAppViewContainer"] {{ background: transparent !important; }}
[data-testid="stAppViewContainer"]::before {{
  content: "";
  position: fixed;
  inset: 0;
  background-image: {bg};
  background-repeat: no-repeat;
  background-position: center 44%;
  background-size: min(74vw, 860px);
  opacity: 0.06;                 /* faint but noticeable */
  filter: grayscale(0.2) contrast(1.05);
  pointer-events: none;
  user-select: none;
  z-index: 0;
}}
/* Keep all real content above the watermark. */
[data-testid="stMain"], [data-testid="stHeader"],
[data-testid="stAppViewContainer"] .block-container {{ position: relative; z-index: 1; }}
</style>"""

st.set_page_config(
    page_title="MindHub Trader",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─── GLOBAL CSS ─────────────────────────────────────────────────────────────

st.markdown("""
<style>
/* ── Base theme ── */
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600;700&family=Inter:wght@300;400;500;600;700;800&display=swap');

html, body, [data-testid="stAppViewContainer"] {
  background: #080c10 !important;
  color: #e2e8f0 !important;
  font-family: 'Inter', sans-serif !important;
}

/* ── Sidebar ── */
[data-testid="stSidebar"] {
  background: #0d1117 !important;
  border-right: 1px solid #1e2733 !important;
}

/* ── Tab styling ── */
[data-testid="stTabs"] [role="tab"] {
  color: #64748b !important;
  font-weight: 600 !important;
  font-size: 0.85rem !important;
  letter-spacing: 0.06em !important;
  text-transform: uppercase !important;
  padding: 8px 18px !important;
  border-radius: 4px 4px 0 0 !important;
  border: none !important;
  background: transparent !important;
}
[data-testid="stTabs"] [role="tab"][aria-selected="true"] {
  color: #00d4aa !important;
  background: #0d1f1a !important;
  border-bottom: 2px solid #00d4aa !important;
}

/* ── Metric cards ── */
[data-testid="stMetric"] {
  background: #0d1117 !important;
  border: 1px solid #1e2733 !important;
  border-radius: 8px !important;
  padding: 14px 18px !important;
}
[data-testid="stMetricLabel"] { color: #64748b !important; font-size: 0.72rem !important; text-transform: uppercase; letter-spacing: 0.08em; }
[data-testid="stMetricValue"] { color: #f1f5f9 !important; font-size: 1.5rem !important; font-weight: 700 !important; font-family: 'JetBrains Mono', monospace !important; }
[data-testid="stMetricDelta"] svg { display: none; }

/* ── Dataframe ── */
[data-testid="stDataFrame"] { border: 1px solid #1e2733 !important; border-radius: 8px; overflow: hidden; }

/* ── Buttons ── */
[data-testid="baseButton-secondary"] {
  background: #0d1117 !important;
  border: 1px solid #1e2733 !important;
  color: #94a3b8 !important;
  border-radius: 6px !important;
  font-size: 0.8rem !important;
}
[data-testid="baseButton-secondary"]:hover {
  border-color: #00d4aa !important;
  color: #00d4aa !important;
}

/* ── Selectbox / slider ── */
[data-testid="stSelectbox"] > div, [data-testid="stSlider"] { background: transparent !important; }

/* ── Scrollbar ── */
::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-track { background: #080c10; }
::-webkit-scrollbar-thumb { background: #1e2733; border-radius: 4px; }

/* ── Hide Streamlit chrome, keep sidebar toggle ── */
#MainMenu, footer, header { visibility: hidden; }
[data-testid="stDecoration"] { display: none; }

/* Native Streamlit sidebar expand button — keep visible and style to match theme */
[data-testid="collapsedControl"] {
  visibility: visible !important;
  display: flex !important;
  background: #0a1a14 !important;
  border-right: 2px solid #00d4aa !important;
}

/* ── Card utility ── */
.mh-card {
  background: #0d1117;
  border: 1px solid #1e2733;
  border-radius: 10px;
  padding: 18px 22px;
  margin-bottom: 12px;
}
.mh-card-title {
  font-size: 0.65rem;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: #475569;
  margin-bottom: 12px;
}

/* ── Neon badge ── */
.badge-green { color: #00d4aa; font-weight: 700; }
.badge-red   { color: #ff4d6d; font-weight: 700; }
.badge-yellow{ color: #fbbf24; font-weight: 700; }
.badge-blue  { color: #60a5fa; font-weight: 700; }
.mono { font-family: 'JetBrains Mono', monospace; }
</style>
""", unsafe_allow_html=True)

# ─── DATA FUNCTIONS ──────────────────────────────────────────────────────────

def load_watchlist():
    path = ROOT / "watchlist.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}

@st.cache_data(ttl=60)
def get_account():
    try:
        r = _HTTP.get(f"{BASE_URL}/v2/account", headers=HEADERS, timeout=12)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}  # degrade gracefully on a transient VPN/DNS blip — never crash the page

@st.cache_data(ttl=30)
def get_positions():
    try:
        r = _HTTP.get(f"{BASE_URL}/v2/positions", headers=HEADERS, timeout=12)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []

@st.cache_data(ttl=300)
def get_portfolio_history(period="1M", timeframe="1D"):
    try:
        r = _HTTP.get(
            f"{BASE_URL}/v2/account/portfolio/history",
            headers=HEADERS,
            params={"period": period, "timeframe": timeframe},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}

@st.cache_data(ttl=60)
def get_orders():
    try:
        r = _HTTP.get(
            f"{BASE_URL}/v2/orders",
            headers=HEADERS,
            params={"status": "all", "limit": 50},
            timeout=12,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return []

@st.cache_data(ttl=30)
def get_market_status():
    try:
        r = _HTTP.get(f"{BASE_URL}/v2/clock", headers=HEADERS, timeout=12)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}

@st.cache_data(ttl=15)
def get_latest_prices():
    wl = load_watchlist()
    equities = [s["symbol"] for s in wl.get("watchlist", []) if s.get("type") != "crypto"]
    cryptos  = [s["symbol"] for s in wl.get("watchlist", []) if s.get("type") == "crypto"]
    prices = {}

    if equities:
        try:
            r = _HTTP.get(
                "https://data.alpaca.markets/v2/stocks/snapshots",
                headers=HEADERS,
                params={"symbols": ",".join(equities), "feed": "iex"},
                timeout=10,
            )
            r.raise_for_status()
            for sym, data in r.json().items():
                dp = data.get("dailyBar", {})
                lp = data.get("latestTrade", {}).get("p") or dp.get("c")
                op = dp.get("o")
                prices[sym] = {"price": lp, "open": op, "type": "equity",
                               "high": dp.get("h"), "low": dp.get("l"), "volume": dp.get("v")}
        except Exception:
            pass

    if cryptos:
        try:
            r = _HTTP.get(
                "https://data.alpaca.markets/v1beta3/crypto/us/latest/trades",
                headers=HEADERS,
                params={"symbols": ",".join(cryptos)},
                timeout=10,
            )
            r.raise_for_status()
            trades = r.json().get("trades", {})
            r2 = _HTTP.get(
                "https://data.alpaca.markets/v1beta3/crypto/us/bars",
                headers=HEADERS,
                params={"symbols": ",".join(cryptos), "timeframe": "1Day", "limit": 2},
                timeout=10,
            )
            r2.raise_for_status()
            bars = r2.json().get("bars", {})
            for sym in cryptos:
                trade_price = trades.get(sym, {}).get("p")
                sym_bars = bars.get(sym, [])
                open_price = sym_bars[-1].get("o") if sym_bars else None
                prices[sym] = {"price": trade_price, "open": open_price, "type": "crypto"}
        except Exception:
            pass

    return prices

@st.cache_data(ttl=3600)
def get_macro():
    try:
        from enrichment import get_macro as _macro
        return _macro()
    except Exception as e:
        return {"error": str(e)}

@st.cache_data(ttl=1800)
def get_fear_greed():
    try:
        from enrichment import get_fear_greed as _fg
        return _fg()
    except Exception as e:
        return {"error": str(e)}

@st.cache_data(ttl=60)
def get_trending_data():
    items = []
    try:
        r = _HTTP.get("https://api.coingecko.com/api/v3/search/trending", timeout=10)
        r.raise_for_status()
        for coin in r.json().get("coins", [])[:6]:
            c = coin["item"]
            chg = c.get("data", {}).get("price_change_percentage_24h", {}).get("usd")
            price_str = c.get("data", {}).get("price", "")
            items.append({
                "sym": c["symbol"].upper(), "price_str": price_str or "—",
                "chg_pct": round(chg, 2) if chg is not None else None, "tag": "TRENDING",
            })
    except Exception:
        pass
    try:
        r = _HTTP.get(
            "https://data.alpaca.markets/v1beta1/screener/stocks/most-actives",
            headers=HEADERS,
            params={"by": "volume", "top": 8},
        )
        r.raise_for_status()
        active_syms = [s["symbol"] for s in r.json().get("most_actives", [])[:8]]
        if active_syms:
            rs = _HTTP.get(
                "https://data.alpaca.markets/v2/stocks/snapshots",
                headers=HEADERS,
                params={"symbols": ",".join(active_syms), "feed": "iex"},
            )
            rs.raise_for_status()
            snaps = rs.json()
            for sym in active_syms:
                snap = snaps.get(sym, {})
                dp   = snap.get("dailyBar", {})
                lp   = snap.get("latestTrade", {}).get("p") or dp.get("c")
                op   = dp.get("o")
                chg  = (float(lp) - float(op)) / float(op) * 100 if lp and op and float(op) > 0 else None
                items.append({
                    "sym": sym, "price_str": fmt_price(float(lp)) if lp else "—",
                    "chg_pct": round(chg, 2) if chg is not None else None, "tag": "ACTIVE",
                })
    except Exception:
        pass
    return items

@st.cache_data(ttl=300)
def get_sector_performance():
    sector_etfs = {
        "Tech": "XLK", "Energy": "XLE", "Finance": "XLF", "Health": "XLV",
        "Consumer": "XLY", "Utilities": "XLU", "Industrials": "XLI",
        "Materials": "XLB", "Real Estate": "XLRE", "Comm Svcs": "XLC",
    }
    try:
        syms = ",".join(sector_etfs.values())
        r = _HTTP.get(
            "https://data.alpaca.markets/v2/stocks/snapshots",
            headers=HEADERS,
            params={"symbols": syms, "feed": "iex"},
        )
        r.raise_for_status()
        snaps = r.json()
        results = {}
        for name, etf in sector_etfs.items():
            snap = snaps.get(etf, {})
            dp = snap.get("dailyBar", {})
            lp = snap.get("latestTrade", {}).get("p") or dp.get("c")
            op = dp.get("o")
            chg = (float(lp) - float(op)) / float(op) * 100 if lp and op and float(op) > 0 else None
            results[name] = {"etf": etf, "price": lp, "chg_pct": round(chg, 2) if chg is not None else None}
        return results
    except Exception:
        return {}

@st.cache_data(ttl=120)
def get_market_movers():
    gainers, losers = [], []
    try:
        rg = _HTTP.get(
            "https://data.alpaca.markets/v1beta1/screener/stocks/most-actives",
            headers=HEADERS,
            params={"by": "trade_count", "top": 20},
        )
        rg.raise_for_status()
        active_syms = [s["symbol"] for s in rg.json().get("most_actives", [])]
        if active_syms:
            rs = _HTTP.get(
                "https://data.alpaca.markets/v2/stocks/snapshots",
                headers=HEADERS,
                params={"symbols": ",".join(active_syms), "feed": "iex"},
            )
            rs.raise_for_status()
            snaps = rs.json()
            moves = []
            for sym in active_syms:
                snap = snaps.get(sym, {})
                dp   = snap.get("dailyBar", {})
                lp   = snap.get("latestTrade", {}).get("p") or dp.get("c")
                op   = dp.get("o")
                vol  = dp.get("v", 0)
                if lp and op and float(op) > 0:
                    chg = (float(lp) - float(op)) / float(op) * 100
                    moves.append({"sym": sym, "price": float(lp), "chg_pct": round(chg, 2), "volume": vol})
            moves.sort(key=lambda x: x["chg_pct"], reverse=True)
            gainers = moves[:5]
            losers  = list(reversed(moves[-5:]))
    except Exception:
        pass
    return gainers, losers

def get_journal_entries():
    journal_dir = ROOT / "journal"
    if not journal_dir.exists():
        return []
    return sorted(
        [f for f in journal_dir.glob("*.md") if f.name != "TEMPLATE.md"],
        reverse=True,
    )

# ─── FORMATTING HELPERS ───────────────────────────────────────────────────────

def fmt_price(price):
    if price is None:
        return "—"
    if price < 1:
        return f"${price:.5f}"
    if price < 100:
        return f"${price:.3f}"
    return f"${price:,.2f}"

def pct_color(v):
    if v is None:
        return "#64748b"
    return "#00d4aa" if v >= 0 else "#ff4d6d"

def pct_arrow(v):
    if v is None:
        return ""
    return "▲" if v >= 0 else "▼"

# ─── TICKER BARS ─────────────────────────────────────────────────────────────

def ticker_span(label, price_str, chg_pct=None, tag=None):
    color = pct_color(chg_pct)
    chg_str = f"{pct_arrow(chg_pct)} {chg_pct:+.2f}%" if chg_pct is not None else "—"
    tag_html = f'<span style="color:#475569;font-size:0.68em;margin-left:3px">[{tag}]</span>' if tag else ""
    return (
        f'<span style="margin:0 28px;white-space:nowrap">'
        f'<span style="color:#94a3b8;font-weight:600;font-size:0.82em;letter-spacing:0.05em">{label}</span>{tag_html} '
        f'<span style="color:#f1f5f9;font-family:\'JetBrains Mono\',monospace;font-size:0.85em">{price_str}</span> '
        f'<span style="color:{color};font-size:0.78em;font-weight:600">{chg_str}</span>'
        f'</span>'
    )

def render_ticker_bar(spans, uid, bg="#0d1117", border="#1e2733", label_text=None, accent="#1e2733"):
    if not spans:
        return
    content = "".join(spans * 4)
    duration = max(25, len(spans) * 5)
    label_html = (
        f'<div style="font-size:0.58em;font-weight:700;letter-spacing:0.14em;'
        f'color:#334155;text-transform:uppercase;margin-bottom:2px;padding-left:4px">'
        f'◈ {label_text}</div>'
    ) if label_text else ""
    st.markdown(f"""
{label_html}
<style>
@keyframes scroll-{uid} {{ 0% {{ transform:translateX(0); }} 100% {{ transform:translateX(-25%); }} }}
.tw-{uid} {{ width:100%;overflow:hidden;background:{bg};border-radius:6px;
  padding:7px 0;margin-bottom:8px;border:1px solid {border};
  border-left:3px solid {accent}; }}
.tt-{uid} {{ display:inline-flex;white-space:nowrap;
  animation:scroll-{uid} {duration}s linear infinite; }}
.tt-{uid}:hover {{ animation-play-state:paused; }}
</style>
<div class="tw-{uid}"><div class="tt-{uid}">{content}</div></div>
""", unsafe_allow_html=True)

def render_tickers():
    prices = get_latest_prices()
    wl_spans = []
    for sym, data in prices.items():
        price  = data.get("price")
        open_p = data.get("open")
        chg    = (price - open_p) / open_p * 100 if price and open_p and open_p > 0 else None
        wl_spans.append(ticker_span(sym, fmt_price(price), chg))
    render_ticker_bar(wl_spans, "watchlist", label_text="WATCHLIST", accent="#00d4aa")

    positions = get_positions() if ALPACA_KEY else []
    if isinstance(positions, list) and positions:
        h_spans = []
        for p in positions:
            sym = p.get("symbol","")
            price = float(p.get("current_price") or 0)
            pnl_pct = float(p.get("unrealized_plpc") or 0) * 100
            qty = float(p.get("qty") or 0)
            h_spans.append(ticker_span(sym, fmt_price(price), pnl_pct, tag=f"{qty:g}x"))
        render_ticker_bar(h_spans, "holdings", bg="#080e0c", border="#0f2a20", label_text="HOLDINGS", accent="#00d4aa")
    else:
        render_ticker_bar([ticker_span("NO POSITIONS", "—")], "holdings",
                          bg="#080e0c", border="#0f2a20", label_text="HOLDINGS", accent="#00d4aa")

    trending = get_trending_data()
    if trending:
        t_spans = [ticker_span(t["sym"], t["price_str"], t["chg_pct"], tag=t["tag"]) for t in trending]
        render_ticker_bar(t_spans, "trending", bg="#080c14", border="#151e30", label_text="TRENDING/ACTIVE", accent="#60a5fa")

def render_alerts_ticker():
    alerts = []
    try:
        fg = get_fear_greed()
        score = fg.get("score")
        rating = fg.get("rating", "").replace("_", " ").title()
        if isinstance(score, (int, float)):
            if score <= 25:
                alerts.append(("🔴 EXTREME FEAR", f"Fear & Greed: {score} ({rating}) — panic conditions", "#ff4d6d"))
            elif score <= 40:
                alerts.append(("🟠 FEAR", f"Fear & Greed: {score} ({rating}) — bearish sentiment", "#fb923c"))
            elif score >= 75:
                alerts.append(("🟡 EXTREME GREED", f"Fear & Greed: {score} ({rating}) — overheated", "#fbbf24"))
    except Exception:
        pass
    try:
        macro = get_macro()
        vix = macro.get("vix", {}).get("value")
        if vix and float(vix) >= 30:
            alerts.append(("⚡ HIGH VIX", f"VIX at {vix} — elevated volatility, widen stops", "#fb923c"))
    except Exception:
        pass
    try:
        prices = get_latest_prices()
        for sym, data in prices.items():
            price = data.get("price")
            open_p = data.get("open")
            if price and open_p and open_p > 0:
                chg = (price - open_p) / open_p * 100
                if chg >= 5:
                    alerts.append(("🚀 MOVER", f"{sym} +{chg:.1f}% today — momentum signal", "#00d4aa"))
                elif chg <= -5:
                    alerts.append(("📉 DROP", f"{sym} {chg:+.1f}% today — stop-loss risk", "#ff4d6d"))
    except Exception:
        pass
    try:
        positions = get_positions()
        for p in (positions if isinstance(positions, list) else []):
            pnl_pct = float(p.get("unrealized_plpc", 0)) * 100
            if pnl_pct <= -4:
                sym = p.get("symbol", "?")
                alerts.append(("🛑 STOP WARNING", f"{sym} {pnl_pct:.1f}% — approaching 5% stop", "#ff4d6d"))
    except Exception:
        pass
    if not alerts:
        alerts.append(("✅ ALL CLEAR", "No alerts — all systems nominal", "#00d4aa"))

    items = []
    for label, msg, color in alerts:
        items.append(
            f'<span style="margin:0 40px;white-space:nowrap">'
            f'<span style="color:{color};font-weight:700;font-size:0.78em;letter-spacing:0.05em">{label}</span>'
            f'<span style="color:#64748b;font-size:0.75em;margin:0 6px">|</span>'
            f'<span style="color:#94a3b8;font-size:0.78em">{msg}</span>'
            f'</span>'
        )
    content = "".join(items * 4)
    duration = max(35, len(alerts) * 10)
    st.markdown(f"""
<div style="font-size:0.58em;font-weight:700;letter-spacing:0.14em;color:#334155;text-transform:uppercase;margin-bottom:2px;padding-left:4px">◈ ALERTS</div>
<style>
@keyframes scroll-alerts {{ 0% {{ transform:translateX(0); }} 100% {{ transform:translateX(-25%); }} }}
.tw-alerts {{ width:100%;overflow:hidden;background:#0e0812;border-radius:6px;
  padding:7px 0;margin-bottom:14px;border:1px solid #2d1a1a;border-left:3px solid #ff4d6d; }}
.tt-alerts {{ display:inline-flex;white-space:nowrap;animation:scroll-alerts {duration}s linear infinite; }}
.tt-alerts:hover {{ animation-play-state:paused; }}
</style>
<div class="tw-alerts"><div class="tt-alerts">{content}</div></div>
""", unsafe_allow_html=True)

# ─── FEAR & GREED GAUGE ───────────────────────────────────────────────────────

def render_fear_greed_gauge(score, label="—"):
    if score is None:
        score = 50
    # Semicircle SVG gauge
    angle = (score / 100) * 180 - 90  # -90 = full fear, +90 = full greed
    rad = math.radians(angle)
    cx, cy, r = 100, 90, 70
    needle_x = cx + r * math.cos(rad)
    needle_y = cy - r * math.sin(rad)  # SVG y is inverted

    if score <= 25:
        color = "#ff4d6d"
    elif score <= 45:
        color = "#fb923c"
    elif score <= 55:
        color = "#fbbf24"
    elif score <= 75:
        color = "#34d399"
    else:
        color = "#00d4aa"

    gauge_html = f"""
<div style="text-align:center;padding:8px 0">
<svg viewBox="0 0 200 110" width="200" height="110">
  <!-- Background arc segments -->
  <path d="M 30 90 A 70 70 0 0 1 170 90" fill="none" stroke="#1e2733" stroke-width="14" stroke-linecap="round"/>
  <!-- Colored arc based on score -->
  <path d="M 30 90 A 70 70 0 0 1 {cx + 70*math.cos(math.radians(-90 + (score/100)*180))} {cy - 70*math.sin(math.radians(-90 + (score/100)*180))}"
        fill="none" stroke="{color}" stroke-width="14" stroke-linecap="round" opacity="0.9"/>
  <!-- Needle -->
  <line x1="{cx}" y1="{cy}" x2="{needle_x:.1f}" y2="{needle_y:.1f}"
        stroke="#f1f5f9" stroke-width="2.5" stroke-linecap="round"/>
  <circle cx="{cx}" cy="{cy}" r="5" fill="{color}"/>
  <!-- Score text -->
  <text x="100" y="108" text-anchor="middle" fill="{color}" font-size="22" font-weight="700" font-family="JetBrains Mono">{score}</text>
</svg>
<div style="font-size:0.7rem;color:#64748b;margin-top:-4px;letter-spacing:0.08em;text-transform:uppercase">{label}</div>
</div>
"""
    return gauge_html

# ─── SECTOR HEATMAP ──────────────────────────────────────────────────────────

def render_sector_heatmap(sectors: dict):
    if not sectors:
        st.info("Sector data unavailable")
        return
    cells = []
    for name, data in sectors.items():
        chg = data.get("chg_pct")
        if chg is None:
            bg, fg = "#1e2733", "#64748b"
        elif chg >= 2:
            bg, fg = "#0f2a1e", "#00d4aa"
        elif chg >= 0.5:
            bg, fg = "#0a1f15", "#34d399"
        elif chg >= 0:
            bg, fg = "#0a1a10", "#6ee7b7"
        elif chg >= -0.5:
            bg, fg = "#1a0a0a", "#fca5a5"
        elif chg >= -2:
            bg, fg = "#250a0a", "#f87171"
        else:
            bg, fg = "#2d0808", "#ff4d6d"
        chg_str = f"{chg:+.2f}%" if chg is not None else "—"
        cells.append(
            f'<div style="background:{bg};border:1px solid {fg}22;border-radius:8px;padding:10px 6px;text-align:center;min-width:80px">'
            f'<div style="color:#94a3b8;font-size:0.62rem;font-weight:600;text-transform:uppercase;letter-spacing:0.06em">{name}</div>'
            f'<div style="color:{fg};font-size:0.9rem;font-weight:700;font-family:\'JetBrains Mono\',monospace;margin-top:4px">{chg_str}</div>'
            f'<div style="color:#475569;font-size:0.6rem;margin-top:2px">{data.get("etf","")}</div>'
            f'</div>'
        )
    grid = "".join(cells)
    st.markdown(f"""
<div style="display:flex;flex-wrap:wrap;gap:6px;padding:4px 0">{grid}</div>
""", unsafe_allow_html=True)

# ─── POSITION HEATMAP CARDS ──────────────────────────────────────────────────

# Plain-English, slightly cheeky one-liners so hovering a position teaches the setup.
STRATEGY_BLURBS = {
    "gap_and_go": "Gapped up on news at the open and kept running. Momentum chad. 🏃",
    "momentum_breakout": "Broke above prior-day high on volume. Buying strength, not hope.",
    "ema_vwap_cross": "Fast EMA crossed up + reclaimed VWAP. The 'trend just turned' play.",
    "vwap_bounce": "Pulled back to VWAP and bounced. Buying the dip with a rule.",
    "bb_squeeze_breakout": "Bollinger bands coiled tight, then popped. Volatility unleashed.",
    "mean_reversion": "Oversold snap-back. Catching the knife — carefully, with a stop.",
    "oversold_bounce": "RSI<30 relief rally. Dead-cat or real? The stop decides.",
    "fibonacci_level": "Bounced off a Fib retracement. Math-wizard support.",
    "obv_divergence": "Volume disagreed with price. Smart money tell.",
    "crypto_scored": "Passed the 14-signal crypto score (≥5/10). Quant-ish conviction. 🤖",
    "short_momentum": "Riding the dump via inverse ETF. Bears eating. 🐻",
    "inverse_etf_momentum": "SQQQ/short play — making money while it bleeds.",
    "panic_hedge": "UVXY fear hedge. Intraday only — it decays like milk. 🥛",
    "stop_loss": "Forced exit — thesis broke. No shame, just discipline.",
}


@st.cache_data(ttl=30)
def _open_trades_map():
    p = ROOT / "data" / "open_trades.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _strategy_for_position(sym: str):
    """(setup_label, blurb) for a held symbol — from the learning log, else watchlist-eligible."""
    norm = normalize_symbol(sym)
    ot = _open_trades_map()
    ctx = ot.get(norm) or ot.get(sym)
    if ctx and ctx.get("setup_type"):
        s = ctx["setup_type"]
        return s, STRATEGY_BLURBS.get(s, "Active strategy in play.")
    for w in _load_wl().get("watchlist", []):
        if normalize_symbol(w["symbol"]) == norm:
            strats = w.get("strategies", [])
            if strats:
                return f"eligible: {strats[0]}", STRATEGY_BLURBS.get(strats[0], "Watchlist-eligible setup.")
    return "untracked", "Entered before strategy tracking — manage by the rules."


def _classify_position(sym: str) -> str:
    """Classify a position symbol into equity / crypto / options / other."""
    if "/" in sym:
        return "crypto"
    # Options: OCC format like NVDA240119C00800000 (>= 15 chars, ends in digits, has C or P)
    if len(sym) >= 10 and any(c in sym for c in ("C", "P")) and sym[-1].isdigit():
        return "options"
    return "equity"


def _position_card(p: dict) -> str:
    sym     = p.get("symbol", "")
    qty     = float(p.get("qty") or 0)
    entry   = float(p.get("avg_entry_price") or 0)
    curr    = float(p.get("current_price") or 0)
    mv      = float(p.get("market_value") or 0)
    pnl     = float(p.get("unrealized_pl") or 0)
    pnl_pct = float(p.get("unrealized_plpc") or 0) * 100
    side    = p.get("side", "long")

    if pnl_pct >= 5:
        bg, accent = "#071a10", "#00d4aa"
    elif pnl_pct >= 0:
        bg, accent = "#061510", "#34d399"
    elif pnl_pct >= -3:
        bg, accent = "#180a0a", "#f87171"
    else:
        bg, accent = "#220606", "#ff4d6d"

    warn_html = (
        '<div style="color:#ff4d6d;font-size:0.65rem;font-weight:700;margin-top:4px;letter-spacing:0.06em">⚠️ STOP-LOSS NEAR</div>'
        if pnl_pct <= -4 else ""
    )
    qty_label = f"{qty:g} {'contracts' if _classify_position(sym) == 'options' else 'units' if '/' in sym else 'shares'}"

    setup_label, blurb = _strategy_for_position(sym)
    strat_html = (
        f'<div style="margin-top:8px;padding-top:8px;border-top:1px solid {accent}22">'
        f'<span style="color:{accent};font-size:0.62rem;font-weight:700;letter-spacing:0.08em;text-transform:uppercase">🎯 {setup_label}</span>'
        f'<div style="color:#64748b;font-size:0.66rem;margin-top:2px;line-height:1.3">{blurb}</div></div>'
    )

    return f"""
<div style="background:{bg};border:1px solid {accent}33;border-left:3px solid {accent};
  border-radius:10px;padding:14px 16px;min-width:190px;flex:1">
  <div style="display:flex;justify-content:space-between;align-items:flex-start">
    <div>
      <div style="color:#f1f5f9;font-size:1.05rem;font-weight:700;letter-spacing:0.04em">{sym}</div>
      <div style="color:#475569;font-size:0.68rem;margin-top:2px">{qty_label} · entry {fmt_price(entry)} · {side}</div>
    </div>
    <div style="text-align:right">
      <div style="color:{accent};font-size:1rem;font-weight:700;font-family:'JetBrains Mono',monospace">{pnl_pct:+.2f}%</div>
      <div style="color:{accent};font-size:0.73rem;font-family:'JetBrains Mono',monospace">{pnl:+,.2f}</div>
    </div>
  </div>
  <div style="margin-top:10px;display:flex;justify-content:space-between">
    <div style="color:#94a3b8;font-size:0.75rem">Current<br><span style="color:#f1f5f9;font-family:'JetBrains Mono',monospace;font-weight:600">{fmt_price(curr)}</span></div>
    <div style="color:#94a3b8;font-size:0.75rem">Mkt Value<br><span style="color:#f1f5f9;font-family:'JetBrains Mono',monospace;font-weight:600">{fmt_price(mv)}</span></div>
  </div>
  {strat_html}
  {warn_html}
</div>"""


def _section_header(label: str, icon: str, color: str, count: int, total_pnl: float):
    pnl_color = "#00d4aa" if total_pnl >= 0 else "#ff4d6d"
    return f"""
<div style="display:flex;align-items:center;gap:10px;margin:18px 0 8px">
  <span style="color:{color};font-size:1rem">{icon}</span>
  <span style="color:{color};font-weight:700;font-size:0.8rem;letter-spacing:0.1em;text-transform:uppercase">{label}</span>
  <span style="background:{color}22;color:{color};font-size:0.65rem;font-weight:700;
    padding:2px 8px;border-radius:20px;letter-spacing:0.06em">{count} position{'s' if count != 1 else ''}</span>
  <span style="color:{pnl_color};font-size:0.75rem;font-family:'JetBrains Mono',monospace;margin-left:auto">
    {total_pnl:+,.2f} unrealized
  </span>
</div>"""


def render_position_cards(positions: list):
    if not positions:
        st.markdown('<div style="color:#475569;font-size:0.85rem;padding:20px;text-align:center">No open positions</div>', unsafe_allow_html=True)
        return

    # Group by type
    groups: dict[str, list] = {"equity": [], "crypto": [], "options": [], "other": []}
    for p in positions:
        groups[_classify_position(p.get("symbol", ""))].append(p)
    # Fallback bucket
    for p in positions:
        t = _classify_position(p.get("symbol", ""))
        if t not in groups:
            groups["other"].append(p)

    type_meta = {
        "equity":  ("EQUITIES",  "📈", "#60a5fa"),
        "crypto":  ("CRYPTO",    "₿",  "#a78bfa"),
        "options": ("OPTIONS",   "⚙️", "#fb923c"),
        "other":   ("OTHER",     "◈",  "#94a3b8"),
    }

    any_shown = False
    for key, (label, icon, color) in type_meta.items():
        bucket = groups.get(key, [])
        if not bucket:
            continue
        any_shown = True
        total_pnl = sum(float(p.get("unrealized_pl") or 0) for p in bucket)
        st.markdown(_section_header(label, icon, color, len(bucket), total_pnl), unsafe_allow_html=True)
        cards_html = "".join(_position_card(p) for p in bucket)
        st.markdown(f'<div style="display:flex;flex-wrap:wrap;gap:10px">{cards_html}</div>', unsafe_allow_html=True)

    if not any_shown:
        st.markdown('<div style="color:#475569;font-size:0.85rem;padding:20px;text-align:center">No open positions</div>', unsafe_allow_html=True)

# ─── MOVERS TABLE ─────────────────────────────────────────────────────────────

def render_movers(gainers, losers):
    def mover_row(m):
        c = "#00d4aa" if m["chg_pct"] >= 0 else "#ff4d6d"
        return (
            f'<tr>'
            f'<td style="color:#f1f5f9;font-weight:600;padding:6px 10px">{m["sym"]}</td>'
            f'<td style="color:#94a3b8;font-family:\'JetBrains Mono\',monospace;padding:6px 10px">{fmt_price(m["price"])}</td>'
            f'<td style="color:{c};font-weight:700;font-family:\'JetBrains Mono\',monospace;padding:6px 10px">{m["chg_pct"]:+.2f}%</td>'
            f'</tr>'
        )

    g_col, l_col = st.columns(2)
    with g_col:
        rows = "".join(mover_row(m) for m in gainers)
        st.markdown(f"""
<div class="mh-card">
<div class="mh-card-title">▲ TOP GAINERS</div>
<table style="width:100%;border-collapse:collapse;font-size:0.85rem">
{rows if rows else '<tr><td style="color:#475569;padding:8px">No data</td></tr>'}
</table></div>""", unsafe_allow_html=True)

    with l_col:
        rows = "".join(mover_row(m) for m in losers)
        st.markdown(f"""
<div class="mh-card">
<div class="mh-card-title">▼ TOP LOSERS</div>
<table style="width:100%;border-collapse:collapse;font-size:0.85rem">
{rows if rows else '<tr><td style="color:#475569;padding:8px">No data</td></tr>'}
</table></div>""", unsafe_allow_html=True)

# ─── MARKET STATUS HEADER ─────────────────────────────────────────────────────

def _countdown_html():
    """Market phase + live countdown to the relevant close, MDT/MST aware."""
    phase = market_phase()
    n = now_mt()
    if phase == "REGULAR":
        mins = minutes_to_close() or 0
        h, m = divmod(mins, 60)
        label = "● MARKET OPEN"
        color = "#00d4aa"
        sub = f"{h}h {m:02d}m to close (2:00 PM) · equity flatten 1:45 PM"
    elif phase == "PRE_MARKET":
        open_dt = n.replace(hour=7, minute=30, second=0, microsecond=0)
        mins = max(0, int((open_dt - n).total_seconds() // 60)); h, m = divmod(mins, 60)
        label, color = "◐ PRE-MARKET", "#fbbf24"
        sub = f"{h}h {m:02d}m to open (7:30 AM)"
    elif phase == "AFTER_HOURS":
        close_dt = n.replace(hour=18, minute=0, second=0, microsecond=0)
        mins = max(0, int((close_dt - n).total_seconds() // 60)); h, m = divmod(mins, 60)
        label, color = "◑ AFTER-HOURS", "#fbbf24"
        sub = f"{h}h {m:02d}m to extended close (6:00 PM)"
    else:
        label, color = "● MARKET CLOSED", "#ff4d6d"
        sub = "Crypto trades 24/7 🟢"
    return label, color, sub


def _validation_badge():
    """Small badge showing validation mode status and completed trade count."""
    try:
        n_trades = get_completed_trade_count_safe()
        threshold = 30
        in_vm = is_validation_mode(n_trades)
        if in_vm:
            return (
                f'<span title="Validation mode: {n_trades}/{threshold} completed trades to unlock full caps" '
                f'style="background:#1a0e2a;border:1px solid #a78bfa44;color:#a78bfa;'
                f'font-size:0.62rem;font-weight:700;letter-spacing:0.06em;padding:2px 8px;'
                f'border-radius:20px;margin-left:10px;cursor:help">'
                f'🔒 VALIDATION {n_trades}/{threshold}</span>'
            )
        return (
            '<span style="background:#0a1a14;border:1px solid #00d4aa44;color:#00d4aa;'
            'font-size:0.62rem;font-weight:700;letter-spacing:0.06em;padding:2px 8px;'
            'border-radius:20px;margin-left:10px">✅ FULL MODE</span>'
        )
    except Exception:
        return ""


def _today_cost_badge():
    """Tiny API cost badge from today's usage log."""
    try:
        log = ROOT / "logs" / "api_usage.jsonl"
        if not log.exists():
            return ""
        today = now_mt().strftime("%Y-%m-%d")
        total = 0.0
        for line in log.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    r = json.loads(line)
                    if r.get("ts", "").startswith(today):
                        total += r.get("cost_usd", 0)
                except Exception:
                    pass
        if total == 0:
            return ""
        return (
            f'<span title="Today\'s Claude API spend" '
            f'style="background:#0a1014;border:1px solid #fbbf2444;color:#fbbf24;'
            f'font-size:0.62rem;font-weight:700;letter-spacing:0.06em;padding:2px 8px;'
            f'border-radius:20px;margin-left:8px">🤖 ${total:.3f}/day</span>'
        )
    except Exception:
        return ""


def render_header():
    n = now_mt()
    tz = mt_tz_label()
    label, color, sub = _countdown_html()
    time_str = n.strftime("%H:%M:%S") + f" {tz}"
    date_str = n.strftime("%A, %B %d %Y")
    vm_badge = _validation_badge()
    cost_badge = _today_cost_badge()

    st.markdown(f"""
<div style="display:flex;align-items:center;justify-content:space-between;
  background:#0d1117;border:1px solid #1e2733;border-radius:10px;padding:14px 22px;margin-bottom:12px">
  <div style="display:flex;align-items:center;flex-wrap:wrap;gap:4px">
    <span style="color:#f1f5f9;font-size:1.5rem;font-weight:800;letter-spacing:-0.01em">⚡ MINDHUB</span>
    <span style="color:#00d4aa;font-size:1.5rem;font-weight:300;margin-left:4px">TRADER</span>
    <span style="color:#334155;font-size:0.7rem;margin-left:12px;font-family:'JetBrains Mono',monospace">PAPER</span>
    {vm_badge}{cost_badge}
  </div>
  <div style="text-align:right">
    <div><span style="color:{color};font-weight:700;font-size:0.9rem">{label}</span></div>
    <div style="color:{color};font-size:0.72rem;font-family:'JetBrains Mono',monospace;margin-top:2px">{sub}</div>
    <div style="color:#475569;font-size:0.78rem;font-family:'JetBrains Mono',monospace;margin-top:3px">
      🕐 {time_str} · {date_str}</div>
  </div>
</div>
""", unsafe_allow_html=True)


def render_vibe_bar(account):
    """Stonks meme strip — turns the day's P&L into trader-brain flavor + a rotating
    dark-humor trading lesson (learning that doesn't feel like learning)."""
    try:
        eq = float(account.get("equity", 0))
        last = float(account.get("last_equity", eq))
        pnl = eq - last
        pct = (pnl / last * 100) if last else 0
    except Exception:
        pnl, pct = 0, 0

    if pct >= 3:      mood, emo, c = "STONKS. Absolutely up only.", "📈🚀", "#00d4aa"
    elif pct >= 0.5:  mood, emo, c = "Green day. Stay humble.", "📈😎", "#34d399"
    elif pct > -0.5:  mood, emo, c = "Crab market. Sideways and bored.", "🦀", "#94a3b8"
    elif pct > -3:    mood, emo, c = "Mild pain. This is fine.", "🔥🐶", "#fbbf24"
    else:             mood, emo, c = "Not stonks. Respect your stops.", "📉💀", "#ff4d6d"

    # rotate the lesson by 30s slot so it changes but isn't frantic
    idx = int(now_mt().timestamp() // 30) % len(STONKS_WISDOM)
    tip, lesson = STONKS_WISDOM[idx]

    st.markdown(f"""
<div style="display:flex;align-items:center;gap:14px;background:linear-gradient(90deg,{c}14,transparent);
  border:1px solid {c}33;border-left:3px solid {c};border-radius:8px;padding:8px 16px;margin-bottom:14px">
  <span style="font-size:1.3rem">{emo}</span>
  <span style="color:{c};font-weight:700;font-size:0.85rem">{mood}</span>
  <span style="color:#475569;font-size:0.8rem">Day {pnl:+,.0f} ({pct:+.2f}%)</span>
  <span style="margin-left:auto;color:#94a3b8;font-size:0.78rem" title="{lesson}">
    💡 <b style="color:#cbd5e1">{tip}</b> <span style="color:#475569">— {lesson}</span></span>
</div>
""", unsafe_allow_html=True)

# ─── PORTFOLIO EQUITY CHART ──────────────────────────────────────────────────

def render_equity_chart():
    try:
        history = get_portfolio_history()
        timestamps = history.get("timestamp", [])
        equities = history.get("equity", [])
        profit_loss = history.get("profit_loss", [])

        if timestamps and equities:
            df = pd.DataFrame({
                "Date": [datetime.fromtimestamp(t) for t in timestamps],
                "Equity": [float(e) if e else None for e in equities],
                "P&L": [float(p) if p else None for p in (profit_loss or [None]*len(timestamps))],
            }).dropna(subset=["Equity"])

            try:
                st.line_chart(df.set_index("Date")[["Equity"]], height=280,
                              use_container_width=True, color=["#00d4aa"])
            except TypeError:
                st.line_chart(df.set_index("Date")[["Equity"]], height=280,
                              use_container_width=True)
        else:
            st.info("Portfolio history not yet available.")
    except Exception as e:
        st.info(f"Portfolio chart unavailable: {e}")

# ─── TRADINGVIEW CHART ────────────────────────────────────────────────────────

TV_SYMBOLS = {
    "BTC/USD":  "BINANCE:BTCUSDT",
    "ETH/USD":  "BINANCE:ETHUSDT",
    "SOL/USD":  "BINANCE:SOLUSDT",
    "DOGE/USD": "BINANCE:DOGEUSDT",
    "AVAX/USD": "BINANCE:AVAXUSDT",
    "LINK/USD": "BINANCE:LINKUSDT",
    "XRP/USD":  "BINANCE:XRPUSDT",
    "NVDA":     "NASDAQ:NVDA",
    "AMD":      "NASDAQ:AMD",
    "TSLA":     "NASDAQ:TSLA",
    "MSTR":     "NASDAQ:MSTR",
    "COIN":     "NASDAQ:COIN",
    "PLTR":     "NYSE:PLTR",
    "TQQQ":     "NASDAQ:TQQQ",
    "SOXL":     "NASDAQ:SOXL",
    "SQQQ":     "NASDAQ:SQQQ",
    "SPY":      "AMEX:SPY",
    "QQQ":      "NASDAQ:QQQ",
}

def render_tv_chart(sym_key, interval, height):
    tv_symbol = TV_SYMBOLS.get(sym_key, "BINANCE:BTCUSDT")
    tv_html = f"""
<div class="tradingview-widget-container" style="height:{height}px;width:100%">
  <div id="tv_chart" style="height:100%;width:100%"></div>
  <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
  <script type="text/javascript">
  new TradingView.widget({{
    "autosize": true,
    "symbol": "{tv_symbol}",
    "interval": "{interval}",
    "timezone": "America/Denver",
    "theme": "dark",
    "style": "1",
    "locale": "en",
    "toolbar_bg": "#0d1117",
    "hide_top_toolbar": false,
    "hide_legend": false,
    "allow_symbol_change": true,
    "save_image": true,
    "studies": [
      "RSI@tv-basicstudies",
      "MACD@tv-basicstudies",
      "BB@tv-basicstudies",
      "Volume@tv-basicstudies"
    ],
    "container_id": "tv_chart"
  }});
  </script>
</div>
"""
    st.components.v1.html(tv_html, height=height + 20, scrolling=False)

def render_tv_screener():
    html = """
<div class="tradingview-widget-container" style="height:490px;width:100%">
  <div class="tradingview-widget-container__widget" style="height:100%;width:100%"></div>
  <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-screener.js" async>
  {
    "width": "100%",
    "height": 490,
    "defaultColumn": "overview",
    "defaultScreen": "most_capitalized",
    "market": "america",
    "showToolbar": true,
    "colorTheme": "dark",
    "locale": "en",
    "isTransparent": true
  }
  </script>
</div>
"""
    st.components.v1.html(html, height=510, scrolling=False)

def render_tv_economic_calendar():
    html = """
<div class="tradingview-widget-container" style="height:450px;width:100%">
  <div class="tradingview-widget-container__widget" style="height:100%;width:100%"></div>
  <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-events.js" async>
  {
    "colorTheme": "dark",
    "isTransparent": true,
    "width": "100%",
    "height": 450,
    "locale": "en",
    "importanceFilter": "-1,0,1",
    "countryFilter": "us"
  }
  </script>
</div>
"""
    st.components.v1.html(html, height=470, scrolling=False)

def render_tv_market_overview():
    html = """
<div class="tradingview-widget-container" style="height:400px;width:100%">
  <div class="tradingview-widget-container__widget" style="height:100%;width:100%"></div>
  <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-market-overview.js" async>
  {
    "colorTheme": "dark",
    "dateRange": "1D",
    "showChart": true,
    "locale": "en",
    "largeChartUrl": "",
    "isTransparent": true,
    "showSymbolLogo": false,
    "showFloatingTooltip": false,
    "width": "100%",
    "height": 400,
    "tabs": [
      {
        "title": "Indices",
        "symbols": [
          {"s": "FOREXCOM:SPXUSD","d": "S&P 500 Index"},
          {"s": "FOREXCOM:NSXUSD","d": "Nasdaq 100"},
          {"s": "FOREXCOM:DJI","d": "Dow Jones"},
          {"s": "INDEX:VIX","d": "Volatility Index"}
        ],
        "originalTitle": "Indices"
      },
      {
        "title": "Crypto",
        "symbols": [
          {"s": "BINANCE:BTCUSDT","d": "Bitcoin"},
          {"s": "BINANCE:ETHUSDT","d": "Ethereum"},
          {"s": "BINANCE:SOLUSDT","d": "Solana"},
          {"s": "BINANCE:DOGEUSDT","d": "Dogecoin"}
        ]
      },
      {
        "title": "Watchlist",
        "symbols": [
          {"s": "NASDAQ:TQQQ"},
          {"s": "NASDAQ:SOXL"},
          {"s": "NASDAQ:NVDA"},
          {"s": "NASDAQ:AMD"},
          {"s": "NASDAQ:MSTR"}
        ]
      }
    ]
  }
  </script>
</div>
"""
    st.components.v1.html(html, height=420, scrolling=False)

def render_tv_heatmap():
    html = """
<div class="tradingview-widget-container" style="height:500px;width:100%">
  <div class="tradingview-widget-container__widget" style="height:100%;width:100%"></div>
  <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-stock-heatmap.js" async>
  {
    "exchanges": [],
    "dataSource": "SPX500",
    "grouping": "sector",
    "blockSize": "market_cap_basic",
    "blockColor": "change",
    "locale": "en",
    "symbolUrl": "",
    "colorTheme": "dark",
    "hasTopBar": true,
    "isDataSetEnabled": false,
    "isZoomEnabled": true,
    "hasSymbolTooltip": true,
    "isMonoSize": false,
    "width": "100%",
    "height": 500
  }
  </script>
</div>
"""
    st.components.v1.html(html, height=520, scrolling=False)

def render_crypto_market():
    html = """
<div class="tradingview-widget-container" style="height:450px;width:100%">
  <div class="tradingview-widget-container__widget" style="height:100%;width:100%"></div>
  <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-crypto-coins-heatmap.js" async>
  {
    "dataSource": "Crypto",
    "blockSize": "market_cap_calc",
    "blockColor": "change",
    "locale": "en",
    "colorTheme": "dark",
    "width": "100%",
    "height": 450,
    "hasTopBar": true
  }
  </script>
</div>
"""
    st.components.v1.html(html, height=470, scrolling=False)

# ─── RISK / ALLOCATION BAR ────────────────────────────────────────────────────

def render_allocation_bar(account_data, positions_data):
    if not account_data:
        return
    try:
        equity = float(account_data.get("equity", 0))
        cash = float(account_data.get("cash", 0))
        cash_pct = cash / equity * 100 if equity > 0 else 100

        pos_map = {}
        if isinstance(positions_data, list):
            for p in positions_data:
                sym = p.get("symbol", "?")
                pct = abs(float(p.get("market_value", 0))) / equity * 100 if equity > 0 else 0
                pos_map[sym] = pct

        colors = ["#00d4aa", "#60a5fa", "#a78bfa", "#fb923c", "#f472b6", "#34d399", "#fbbf24"]
        segs = []
        legend_items = []
        for i, (sym, pct) in enumerate(pos_map.items()):
            col = colors[i % len(colors)]
            segs.append(f'<div style="width:{pct:.1f}%;background:{col};height:100%;min-width:2px" title="{sym} {pct:.1f}%"></div>')
            legend_items.append(f'<span style="margin-right:14px;font-size:0.72rem;color:{col};font-weight:600">■ {sym} {pct:.1f}%</span>')

        cash_col = "#1e2733"
        segs.append(f'<div style="flex:1;background:{cash_col};height:100%" title="Cash {cash_pct:.1f}%"></div>')
        legend_items.append(f'<span style="margin-right:14px;font-size:0.72rem;color:#475569;font-weight:600">■ Cash {cash_pct:.1f}%</span>')

        bar = "".join(segs)
        legend = "".join(legend_items)

        st.markdown(f"""
<div class="mh-card">
<div class="mh-card-title">ALLOCATION MAP</div>
<div style="display:flex;height:18px;border-radius:6px;overflow:hidden;width:100%">{bar}</div>
<div style="margin-top:10px">{legend}</div>
</div>
""", unsafe_allow_html=True)
    except Exception:
        pass

# ─── ORDERS TABLE ─────────────────────────────────────────────────────────────

def render_orders_table(orders):
    if not isinstance(orders, list) or not orders:
        st.info("No orders yet.")
        return
    rows = []
    for o in orders[:30]:
        lp = o.get("limit_price")
        fp = o.get("filled_avg_price")
        side = (o.get("side") or "").upper()
        side_color = "#00d4aa" if side == "BUY" else "#ff4d6d"
        status = (o.get("status") or "").upper()
        st_color = {"FILLED": "#00d4aa", "CANCELED": "#64748b", "REJECTED": "#ff4d6d"}.get(status, "#fbbf24")
        rows.append(f"""
<tr style="border-bottom:1px solid #1e2733">
  <td style="color:#64748b;font-family:'JetBrains Mono',monospace;font-size:0.75rem;padding:7px 10px">{o.get("created_at","")[:19].replace("T"," ")}</td>
  <td style="color:#f1f5f9;font-weight:600;padding:7px 10px">{o.get("symbol","?")}</td>
  <td style="color:{side_color};font-weight:700;padding:7px 10px">{side}</td>
  <td style="color:#94a3b8;font-family:'JetBrains Mono',monospace;padding:7px 10px">{o.get("qty","—")}</td>
  <td style="color:#94a3b8;font-family:'JetBrains Mono',monospace;padding:7px 10px">{"${:,.2f}".format(float(lp)) if lp else "—"}</td>
  <td style="color:#94a3b8;font-family:'JetBrains Mono',monospace;padding:7px 10px">{"${:,.2f}".format(float(fp)) if fp else "—"}</td>
  <td style="color:{st_color};font-size:0.75rem;font-weight:600;padding:7px 10px">{status}</td>
</tr>""")
    st.markdown(f"""
<div style="overflow-x:auto">
<table style="width:100%;border-collapse:collapse;font-size:0.83rem">
<thead>
<tr style="border-bottom:2px solid #1e2733">
  <th style="color:#475569;text-align:left;padding:7px 10px;font-weight:600;font-size:0.7rem;letter-spacing:0.08em;text-transform:uppercase">Time</th>
  <th style="color:#475569;text-align:left;padding:7px 10px;font-weight:600;font-size:0.7rem;letter-spacing:0.08em;text-transform:uppercase">Symbol</th>
  <th style="color:#475569;text-align:left;padding:7px 10px;font-weight:600;font-size:0.7rem;letter-spacing:0.08em;text-transform:uppercase">Side</th>
  <th style="color:#475569;text-align:left;padding:7px 10px;font-weight:600;font-size:0.7rem;letter-spacing:0.08em;text-transform:uppercase">Qty</th>
  <th style="color:#475569;text-align:left;padding:7px 10px;font-weight:600;font-size:0.7rem;letter-spacing:0.08em;text-transform:uppercase">Limit</th>
  <th style="color:#475569;text-align:left;padding:7px 10px;font-weight:600;font-size:0.7rem;letter-spacing:0.08em;text-transform:uppercase">Filled</th>
  <th style="color:#475569;text-align:left;padding:7px 10px;font-weight:600;font-size:0.7rem;letter-spacing:0.08em;text-transform:uppercase">Status</th>
</tr>
</thead>
<tbody>{"".join(rows)}</tbody>
</table>
</div>
""", unsafe_allow_html=True)

# ─── MAIN APP ─────────────────────────────────────────────────────────────────

# Tickers at top
render_tickers()
render_alerts_ticker()

# Header
render_header()

if "show_chat" not in st.session_state:
    st.session_state.show_chat = False

# Action row: refresh + AI chat toggle (a REAL Streamlit button — always works)
c_ref, c_ai, c_time = st.columns([1.4, 1.4, 5])
with c_ref:
    if st.button("⟳  Refresh Data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
with c_ai:
    chat_label = "✕  Close AI Chat" if st.session_state.show_chat else "⚡  Ask Claude AI"
    if st.button(chat_label, use_container_width=True, type="primary"):
        st.session_state.show_chat = not st.session_state.show_chat
        st.rerun()

# ── Fetch once for all tabs ──
try:
    account  = get_account()
    equity   = float(account.get("equity", 0))
    cash     = float(account.get("cash", 0))
    last_eq  = float(account.get("last_equity", equity))
    invested = equity - cash
    day_pnl  = equity - last_eq
    day_pnl_pct = (day_pnl / last_eq * 100) if last_eq else 0
    start_eq = 100_000.0
    total_pnl = equity - start_eq
    total_pnl_pct = total_pnl / start_eq * 100
except Exception:
    account = {}
    equity = cash = last_eq = invested = day_pnl = day_pnl_pct = total_pnl = total_pnl_pct = 0

try:
    positions = get_positions()
    if not isinstance(positions, list):
        positions = []
except Exception:
    positions = []

try:
    orders = get_orders()
except Exception:
    orders = []

# ── Stonks vibe bar (fun + a rotating dark-humor trading lesson) ──
render_vibe_bar(account)

# ── Portfolio metric strip ──
m1, m2, m3, m4, m5, m6 = st.columns(6)
m1.metric("Portfolio", f"${equity:,.2f}", f"{day_pnl:+,.2f}")
m2.metric("Cash", f"${cash:,.2f}", f"{cash/equity*100:.1f}% avail" if equity else "—")
m3.metric("Invested", f"${invested:,.2f}", f"{invested/equity*100:.1f}%" if equity else "—")
m4.metric("Day P&L", f"${day_pnl:+,.2f}", f"{day_pnl_pct:+.2f}%")
m5.metric("Total P&L", f"${total_pnl:+,.2f}", f"{total_pnl_pct:+.2f}% vs start")
m6.metric("Open Positions", str(len(positions)), f"{len(positions)} active")

st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

# ── Safeguards / Research-mode toggle ──────────────────────────────────────────
try:
    _rm_enabled = bool(research_mode_enabled())
except Exception:
    _rm_enabled = False
sg_l, sg_r = st.columns([1, 4])
with sg_l:
    _rm_new = st.toggle(
        "🔬 Research mode",
        value=_rm_enabled,
        key="research_mode_toggle",
        help=("PAPER-ONLY. ON = safeguards relaxed (aggressive) to maximize research, "
              "proposals, and signal→outcome data. OFF = full safeguards enforced. "
              "Infrastructure is always retained; a live endpoint auto-restores every guard."),
    )
    if _rm_new != _rm_enabled:
        try:
            set_research_mode(_rm_new)
            st.toast(f"Research mode {'ENABLED — safeguards relaxed' if _rm_new else 'DISABLED — safeguards enforced'}")
        except Exception as _e:
            st.error(f"Could not update research mode: {_e}")
        st.rerun()
with sg_r:
    if research_mode_active():
        st.markdown(
            "<div style='padding:7px 12px;border-radius:8px;background:rgba(245,158,11,0.12);"
            "border:1px solid rgba(245,158,11,0.4)'>"
            "<span style='color:#f59e0b;font-weight:800'>⚠ SAFEGUARDS RELAXED (data-collection)</span>"
            "<span style='color:#94a3b8'> &nbsp;— lockout off · validation caps off · dedup relaxed · "
            "positions 15 · crypto 60% · buy score ≥4. &nbsp;<b>Kept:</b> 5% cash, limit-only, stops, regime rules.</span>"
            "</div>", unsafe_allow_html=True)
    elif _rm_enabled and not research_mode_active():
        st.markdown(
            "<div style='padding:7px 12px;border-radius:8px;background:rgba(59,130,246,0.10);"
            "border:1px solid rgba(59,130,246,0.4)'>"
            "<span style='color:#3b82f6;font-weight:800'>RESEARCH MODE FLAGGED, BUT INACTIVE</span>"
            "<span style='color:#94a3b8'> &nbsp;— live endpoint detected; all safeguards remain enforced.</span>"
            "</div>", unsafe_allow_html=True)
    else:
        st.markdown(
            "<div style='padding:7px 12px;border-radius:8px;background:rgba(34,197,94,0.10);"
            "border:1px solid rgba(34,197,94,0.4)'>"
            "<span style='color:#22c55e;font-weight:800'>✓ SAFEGUARDS ENFORCED</span>"
            "<span style='color:#94a3b8'> &nbsp;— full risk controls active (lockout, validation caps, dedup, hard caps).</span>"
            "</div>", unsafe_allow_html=True)

st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)

# ── Tabs ──
tab_overview, tab_charts, tab_screener, tab_heatmaps, tab_portfolio, tab_journal, tab_discord = st.tabs([
    "  OVERVIEW  ", "  CHARTS  ", "  SCREENER  ", "  HEATMAPS  ", "  PORTFOLIO  ", "  JOURNAL  ", "  DISCORD  "
])

# ════════════════════ TAB: OVERVIEW ════════════════════

with tab_overview:
    left, right = st.columns([3, 1])

    with left:
        # Market overview widget
        st.markdown('<div class="mh-card-title">◈ MARKET OVERVIEW</div>', unsafe_allow_html=True)
        render_tv_market_overview()

        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

        # Sector performance
        st.markdown('<div class="mh-card-title">◈ SECTOR PERFORMANCE (1D)</div>', unsafe_allow_html=True)
        with st.spinner("Loading sectors..."):
            sectors = get_sector_performance()
        render_sector_heatmap(sectors)

        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

        # Market movers
        st.markdown('<div class="mh-card-title">◈ MARKET MOVERS</div>', unsafe_allow_html=True)
        with st.spinner("Loading movers..."):
            gainers, losers = get_market_movers()
        render_movers(gainers, losers)

    with right:
        # Fear & Greed gauge
        st.markdown('<div class="mh-card-title">◈ FEAR & GREED</div>', unsafe_allow_html=True)
        try:
            fg = get_fear_greed()
            fg_score = fg.get("score", 50)
            fg_label = fg.get("rating", "").replace("_", " ").title()
            fg_prev  = fg.get("prev_close", fg_score)
            delta_fg = round(float(fg_score) - float(fg_prev), 1) if fg_prev else None
        except Exception:
            fg_score, fg_label, delta_fg = 50, "Unknown", None

        st.markdown(render_fear_greed_gauge(fg_score, fg_label), unsafe_allow_html=True)
        if delta_fg is not None:
            d_col = "#00d4aa" if delta_fg >= 0 else "#ff4d6d"
            st.markdown(f'<div style="text-align:center;color:{d_col};font-size:0.75rem;margin-top:-6px">{delta_fg:+.1f} vs yesterday</div>', unsafe_allow_html=True)

        st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)

        # Macro snapshot
        st.markdown('<div class="mh-card-title">◈ MACRO SNAPSHOT</div>', unsafe_allow_html=True)
        try:
            macro = get_macro()
            macro_items = [
                ("Fed Funds", "fed_funds_rate", "%"),
                ("CPI", "cpi", "%"),
                ("Unemploy.", "unemployment_rate", "%"),
                ("10Y Yield", "treasury_10y_yield", "%"),
                ("VIX", "vix", ""),
            ]
            for label, key, unit in macro_items:
                d = macro.get(key, {})
                val = d.get("value", "—")
                prev = d.get("prev_value")
                if val != "—" and prev:
                    delta = float(val) - float(prev)
                    color = "#ff4d6d" if delta > 0 and key in ["cpi","vix","unemployment_rate"] else "#00d4aa" if delta <= 0 and key in ["cpi","vix","unemployment_rate"] else "#00d4aa" if delta > 0 else "#ff4d6d"
                    d_str = f'<span style="color:{color};font-size:0.65rem">{delta:+.2f}</span>'
                else:
                    d_str = ""
                st.markdown(f"""
<div style="display:flex;justify-content:space-between;align-items:center;
  padding:6px 0;border-bottom:1px solid #1e2733">
  <span style="color:#64748b;font-size:0.75rem">{label}</span>
  <span style="color:#f1f5f9;font-family:'JetBrains Mono',monospace;font-size:0.8rem;font-weight:600">{val}{unit} {d_str}</span>
</div>""", unsafe_allow_html=True)
        except Exception:
            st.info("Macro data unavailable")

        st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)

        # Economic calendar (mini)
        st.markdown('<div class="mh-card-title">◈ ECONOMIC CALENDAR</div>', unsafe_allow_html=True)
        render_tv_economic_calendar()


# ════════════════════ TAB: CHARTS ════════════════════

with tab_charts:
    ctrl1, ctrl2, ctrl3 = st.columns([3, 1, 1])
    with ctrl1:
        selected_sym = st.selectbox("Symbol", options=list(TV_SYMBOLS.keys()), index=0, label_visibility="collapsed")
    with ctrl2:
        interval = st.selectbox(
            "Interval", options=["1", "5", "15", "60", "240", "D"], index=2,
            format_func=lambda x: {"1":"1m","5":"5m","15":"15m","60":"1h","240":"4h","D":"1D"}.get(x, x),
            label_visibility="collapsed",
        )
    with ctrl3:
        chart_height = st.select_slider("Height", options=[450, 550, 650, 750, 900], value=550, label_visibility="collapsed")

    render_tv_chart(selected_sym, interval, chart_height)


# ════════════════════ TAB: SCREENER ════════════════════

with tab_screener:
    scr_l, scr_r = st.columns([3, 2])
    with scr_l:
        st.markdown('<div class="mh-card-title">◈ STOCK SCREENER (TradingView)</div>', unsafe_allow_html=True)
        render_tv_screener()
    with scr_r:
        st.markdown('<div class="mh-card-title">◈ WATCHLIST SNAPSHOT</div>', unsafe_allow_html=True)
        prices = get_latest_prices()
        wl = load_watchlist()
        wl_syms = {s["symbol"]: s for s in wl.get("watchlist", [])}
        rows = []
        for sym, data in prices.items():
            price  = data.get("price")
            open_p = data.get("open")
            chg    = (price - open_p) / open_p * 100 if price and open_p and float(open_p) > 0 else None
            sym_info = wl_syms.get(sym, {})
            c = pct_color(chg)
            rows.append(f"""
<tr style="border-bottom:1px solid #1e2733">
  <td style="color:#f1f5f9;font-weight:700;padding:8px 10px">{sym}</td>
  <td style="color:#94a3b8;font-size:0.75rem;padding:8px 10px">{sym_info.get("description","")[:22]}</td>
  <td style="color:#f1f5f9;font-family:'JetBrains Mono',monospace;padding:8px 10px">{fmt_price(price)}</td>
  <td style="color:{c};font-weight:700;font-family:'JetBrains Mono',monospace;padding:8px 10px">{"" if chg is None else f"{pct_arrow(chg)} {chg:+.2f}%"}</td>
  <td style="color:#475569;font-size:0.72rem;padding:8px 10px">{sym_info.get("max_allocation_pct","—")}% max</td>
</tr>""")
        st.markdown(f"""
<table style="width:100%;border-collapse:collapse;font-size:0.83rem">
<thead><tr style="border-bottom:2px solid #1e2733">
  <th style="color:#475569;text-align:left;padding:7px 10px;font-size:0.7rem;letter-spacing:0.08em;text-transform:uppercase">Symbol</th>
  <th style="color:#475569;text-align:left;padding:7px 10px;font-size:0.7rem;letter-spacing:0.08em;text-transform:uppercase">Name</th>
  <th style="color:#475569;text-align:left;padding:7px 10px;font-size:0.7rem;letter-spacing:0.08em;text-transform:uppercase">Price</th>
  <th style="color:#475569;text-align:left;padding:7px 10px;font-size:0.7rem;letter-spacing:0.08em;text-transform:uppercase">Change</th>
  <th style="color:#475569;text-align:left;padding:7px 10px;font-size:0.7rem;letter-spacing:0.08em;text-transform:uppercase">Alloc</th>
</tr></thead>
<tbody>{"".join(rows) if rows else "<tr><td colspan='5' style='color:#475569;padding:14px'>No data</td></tr>"}</tbody>
</table>""", unsafe_allow_html=True)

        st.markdown("<div style='height:20px'></div>", unsafe_allow_html=True)
        st.markdown('<div class="mh-card-title">◈ TOP MOVERS</div>', unsafe_allow_html=True)
        g2, l2 = get_market_movers()
        render_movers(g2, l2)


# ════════════════════ TAB: HEATMAPS ════════════════════

with tab_heatmaps:
    st.markdown('<div class="mh-card-title">◈ S&P 500 SECTOR HEATMAP</div>', unsafe_allow_html=True)
    render_tv_heatmap()

    st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

    h_left, h_right = st.columns(2)
    with h_left:
        st.markdown('<div class="mh-card-title">◈ CRYPTO MARKET HEATMAP</div>', unsafe_allow_html=True)
        render_crypto_market()
    with h_right:
        st.markdown('<div class="mh-card-title">◈ SECTOR PERFORMANCE (ALPACA LIVE)</div>', unsafe_allow_html=True)
        sectors = get_sector_performance()
        render_sector_heatmap(sectors)

        st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
        st.markdown('<div class="mh-card-title">◈ TRENDING CRYPTO (COINGECKO)</div>', unsafe_allow_html=True)
        trending = get_trending_data()
        if trending:
            for t in trending[:6]:
                c = pct_color(t.get("chg_pct"))
                st.markdown(f"""
<div style="display:flex;justify-content:space-between;align-items:center;
  padding:6px 2px;border-bottom:1px solid #1e2733">
  <span style="color:#f1f5f9;font-weight:600">{t["sym"]}</span>
  <span style="color:#64748b;font-size:0.72rem">{t.get("tag","")}</span>
  <span style="color:{c};font-family:'JetBrains Mono',monospace;font-weight:700">
    {pct_arrow(t.get("chg_pct"))} {f"{t['chg_pct']:+.2f}%" if t.get("chg_pct") is not None else "—"}
  </span>
</div>""", unsafe_allow_html=True)


# ════════════════════ TAB: PORTFOLIO ════════════════════

with tab_portfolio:
    st.markdown('<div class="mh-card-title">◈ EQUITY CURVE</div>', unsafe_allow_html=True)
    render_equity_chart()

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    render_allocation_bar(account, positions)

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    st.markdown('<div class="mh-card-title">◈ OPEN POSITIONS</div>', unsafe_allow_html=True)
    render_position_cards(positions)

    st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
    st.markdown('<div class="mh-card-title">◈ ORDER HISTORY</div>', unsafe_allow_html=True)
    render_orders_table(orders)


# ════════════════════ TAB: JOURNAL ════════════════════

with tab_journal:
    entries = get_journal_entries()
    if entries:
        j_left, j_right = st.columns([1, 3])
        with j_left:
            dates = [f.stem for f in entries]
            selected = st.radio("Date", dates, label_visibility="collapsed")
        with j_right:
            selected_file = next((f for f in entries if f.stem == selected), None)
            if selected_file:
                raw = selected_file.read_bytes()
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    content = raw.decode("windows-1252", errors="replace")
                # Render inside a styled wrapper, then markdown separately so
                # headers/bold/tables all work correctly (injecting raw md as HTML breaks them)
                st.markdown("""
<div style="background:#0d1117;border:1px solid #1e2733;border-radius:10px;
  padding:16px 22px;margin-bottom:4px">
<span style="color:#00d4aa;font-size:0.7rem;font-weight:700;letter-spacing:0.1em;text-transform:uppercase">
  ◈ JOURNAL ENTRY</span></div>""", unsafe_allow_html=True)
                st.markdown(content)
    else:
        st.info("No journal entries yet. The agent writes one each trading day.")


# ════════════════════ TAB: DISCORD ════════════════════

with tab_discord:
    st.markdown('<div class="mh-card-title">◈ DISCORD CHANNEL HEALTH</div>', unsafe_allow_html=True)
    if _dch is None:
        st.error("discord_channels module unavailable — check scripts/ on the path.")
    else:
        try:
            hc = _dch.health_check()
        except Exception as e:
            hc = None
            st.error(f"Health check failed: {e}")

        if hc:
            c1, c2, c3 = st.columns(3)
            c1.metric("Multichannel", "ON" if hc.get("multichannel_enabled") else "OFF (webhook)")
            c2.metric("Bot token", "present" if hc.get("bot_token_present") else "missing")
            c3.metric("Webhook fallback", "present" if hc.get("webhook_present") else "missing")

            reach = hc.get("reachability", {})
            purpose = getattr(_dch, "_PURPOSE", {})
            rows = [{
                "Channel": f"#{name.replace('_', '-')}",
                "Purpose": purpose.get(name, "—"),
                "Configured": "✓" if configured else "✗",
                "Reachable": reach.get(name, "—"),
            } for name, configured in hc.get("channels", {}).items()]
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

            state = hc.get("alert_state", {})
            if state:
                with st.expander("Alert-policy activity (cooldown / dedup counters)"):
                    srows = [{
                        "Bucket": k,
                        "Severity": v.get("severity", ""),
                        "Posted": v.get("posted_count", 0),
                        "Suppressed": v.get("suppressed_count", 0),
                    } for k, v in state.items()]
                    st.dataframe(pd.DataFrame(srows), hide_index=True, use_container_width=True)

        st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
        note = st.text_input("Optional test note", "",
                             placeholder="Appears inside each test message")
        if st.button("🧪 Test all channels", type="primary"):
            with st.spinner("Posting a test message to every channel..."):
                try:
                    results = _dch.broadcast_test(note or "Dashboard Test button.")
                except Exception as e:
                    results = None
                    st.error(f"Broadcast failed: {e}")
            if results:
                ok = sum(1 for r in results.values() if r.get("ok"))
                (st.success if ok == len(results) else st.warning)(
                    f"Delivered to {ok}/{len(results)} channels.")
                rr = [{
                    "Channel": f"#{k.replace('_', '-')}",
                    "Delivered": "✓" if v.get("ok") else "✗",
                    "Detail": v.get("detail", "") or v.get("channel_id", ""),
                } for k, v in results.items()]
                st.dataframe(pd.DataFrame(rr), hide_index=True, use_container_width=True)


# ─── SIDEBAR — CLAUDE CHAT ───────────────────────────────────────────────────

def build_chat_context():
    ctx = {}
    try:
        ctx["account"]   = get_account()
        ctx["positions"] = get_positions()
        ctx["prices"]    = get_latest_prices()
        ctx["watchlist"] = load_watchlist()
    except Exception:
        pass
    try:
        today = now_mt().strftime("%Y-%m-%d")
        jpath = ROOT / "journal" / f"{today}.md"
        if jpath.exists():
            raw = jpath.read_bytes()
            try:
                ctx["todays_journal"] = raw.decode("utf-8")[-3000:]
            except UnicodeDecodeError:
                ctx["todays_journal"] = raw.decode("windows-1252", errors="replace")[-3000:]
    except Exception:
        pass
    return ctx


@st.cache_data(ttl=120)
def _sop_brief():
    """Load the real SOP (CLAUDE.md) + hard risk config so the dashboard chat shares
    ONE policy source with the orchestrator — no rogue second trading brain."""
    try:
        sop = (ROOT / "CLAUDE.md").read_text(encoding="utf-8", errors="replace")
    except Exception:
        sop = ""
    return sop[:6000], load_risk()


def render_chat_panel(account, positions):
    """In-page Claude advisor. Reliable st.button toggle — no sidebar/JS dependency."""
    sop, risk = _sop_brief()
    eq = float(account.get("equity", 0) or 0)
    c  = float(account.get("cash", 0) or 0)

    st.markdown(f"""
<div style="background:#0a1a14;border:1px solid #00d4aa55;border-radius:10px;padding:10px 16px;margin:6px 0 10px">
  <span style="color:#00d4aa;font-weight:700;font-size:0.95rem">⚡ CLAUDE — Trading Advisor</span>
  <span style="color:#475569;font-size:0.72rem;margin-left:8px">
    same SOP &amp; hard-risk rules as the live agent · equity ${eq:,.0f} · cash ${c:,.0f}</span>
</div>
""", unsafe_allow_html=True)

    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = []

    for msg in st.session_state.chat_messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    cc1, cc2 = st.columns([1, 1])
    with cc1:
        if st.button("↺ Clear chat", use_container_width=True, key="clearchat"):
            st.session_state.chat_messages = []
            st.rerun()
    with cc2:
        if st.button("✕ Close chat", use_container_width=True, key="closechat"):
            st.session_state.show_chat = False
            st.rerun()

    prompt = st.chat_input("Ask about positions, setups, the SOP, risk, what to do next...")
    if prompt:
        st.session_state.chat_messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        ctx = build_chat_context()
        prc = ctx.get("prices", {})
        price_lines = "\n".join(
            f"  {sym}: ${d['price']:,.4f}" if d.get("price") else f"  {sym}: N/A"
            for sym, d in prc.items()
        )
        position_lines = json.dumps(positions, indent=2) if positions else "No open positions."
        journal_snippet = ctx.get("todays_journal", "No journal entry yet today.")

        system = f"""You are Claude, the trading advisor embedded in MindHub Trader. You operate under the
EXACT SAME policy as the autonomous agent — never recommend anything the agent's hard rules forbid.

=== HARD RISK RULES (code-enforced; never advise breaking these) ===
{json.dumps(risk, indent=2)}
- Limit orders only. Sells are always allowed (they de-risk). Buys must keep the cash reserve,
  per-symbol allocation cap, max open positions, and crypto-exposure cap.
- Honor the kill switch and the loss-streak throttle. "No trade" is a valid, good answer.

=== STANDARD OPERATING PROCEDURE (excerpt of CLAUDE.md) ===
{sop}

=== LIVE PORTFOLIO ({now_mt().strftime('%Y-%m-%d %H:%M')} {mt_tz_label()}) ===
- Equity: ${eq:,.2f} | Cash: ${c:,.2f} | Invested: ${eq - c:,.2f}

OPEN POSITIONS:
{position_lines}

CURRENT PRICES:
{price_lines}

TODAY'S JOURNAL (last 3000 chars):
{journal_snippet}

Be specific and quantitative. For any trade idea include entry, target, stop, size, AND confirm it
passes the hard rules above. Aggressive but disciplined — the rules are not optional."""

        with st.chat_message("assistant"):
            placeholder = st.empty()
            full_response = ""
            try:
                # Use the model configured for chat in watchlist.json api_config,
                # defaulting to Sonnet (fast, cheap, more than adequate for Q&A)
                _wl = load_watchlist()
                _chat_model = (_wl.get("api_config", {}).get("models", {})
                               .get("chat", "claude-sonnet-4-6"))
                client = anthropic.Anthropic()
                msgs_for_api = [{"role": m["role"], "content": m["content"]} for m in st.session_state.chat_messages]
                with client.messages.stream(
                    model=_chat_model,
                    max_tokens=1024,
                    system=system,
                    messages=msgs_for_api,
                ) as stream:
                    for text in stream.text_stream:
                        full_response += text
                        placeholder.markdown(full_response + "▌")
                placeholder.markdown(full_response)
            except Exception as e:
                full_response = f"Error: {e}"
                placeholder.error(full_response)

        st.session_state.chat_messages.append({"role": "assistant", "content": full_response})


# Render the chat inline when toggled open (button lives in the top action row).
if st.session_state.get("show_chat"):
    render_chat_panel(account, positions)

# ─── Footer ──────────────────────────────────────────────────────────────────

# Stonks man — real meme image, fixed corner, always visible, never in the way
st.markdown(_stonks_html(), unsafe_allow_html=True)

st.markdown("""
<div style="text-align:center;padding:24px 0 8px;color:#334155;font-size:0.68rem;letter-spacing:0.08em">
  MINDHUB TRADER · PAPER TRADING ONLY · ALPACA PAPER API · NOT FINANCIAL ADVICE · STONKS ONLY GO UP 📈
</div>
""", unsafe_allow_html=True)

# ── Auto-refresh every 15s — but PAUSE while the chat is open so typing isn't interrupted ──
if not st.session_state.get("show_chat"):
    time.sleep(15)
    st.rerun()
