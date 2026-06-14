"""
Detailed session report — MindHub Trader.

Produces a deep, structured end-of-session report and posts it to Discord (concise
embed + full report as a downloadable .md attachment) and to reports/ on disk. The
report is designed to be pasted into an external AI for analysis and refinement that
then feeds back into Claude Code.

Sessions:
  eod         — 2:15 PM MT, after the equity close
  afterhours  — 6:15 PM MT, after extended hours
  now         — ad-hoc snapshot

It answers, with numbers:
  • What is the book, and which STRATEGY is each position running?
  • Every order today and its status — and WHY orders were rejected (tallied).
  • Learning stats: win rate, expectancy, profit factor, best/worst setups.
  • Regime context and what to watch next session.

CLI: python scripts/report.py [eod|afterhours|now]
"""

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT / "scripts"))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from common import (  # noqa: E402
    now_mt, now_mt_str, today_mt, mt_tz_label, normalize_positions,
    normalize_symbol, is_crypto, load_watchlist, load_risk, market_phase,
)

try:
    import discord_notify as dn
except Exception:
    dn = None
try:
    from regime import detect_regime
except Exception:
    def detect_regime(): return {"regime": "UNKNOWN", "multiplier": 0.6, "vix": None}
try:
    import learning
except Exception:
    learning = None
try:
    import intelligence
except Exception:
    intelligence = None

ALPACA_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_BASE_URL", "https://paper-api.alpaca.markets")
HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

REPORTS_DIR = ROOT / "reports"
REPORTS_DIR.mkdir(exist_ok=True)
STARTING_CAPITAL = 100_000.0


# ── Data ─────────────────────────────────────────────────────────────────────
def _get(path, params=None):
    r = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params or {}, timeout=15)
    r.raise_for_status()
    return r.json()


def _open_trades():
    p = ROOT / "data" / "open_trades.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _todays_orders():
    """All orders submitted today (MT)."""
    try:
        orders = _get("/v2/orders", {"status": "all", "limit": 200, "nested": "true"})
    except Exception:
        return []
    today = today_mt()
    out = []
    for o in orders:
        ts = (o.get("submitted_at") or o.get("created_at") or "")[:10]
        if ts == today:
            out.append(o)
    return out


def _rejection_tally():
    """
    Parse today's agent.log for OUR validator/broker rejections and tally by reason.
    This is what actually answers 'why are so many orders rejected'.
    """
    log = ROOT / "agent.log"
    if not log.exists():
        return Counter(), 0
    today = today_mt()
    counter = Counter()
    total = 0
    try:
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.startswith(today):
                continue
            if "Order rejected:" not in line:
                continue
            reason = line.split("Order rejected:", 1)[1]
            # cut off the trailing order dict / dash separators / decode artifacts
            reason = re.split(r"\s*[—–\-]\s*\{|\s+\{|�", reason)[0].strip(" -–—�")
            reason = re.sub(r"\$[\d,]+", "$X", reason)
            reason = re.sub(r"[\d.]+%", "N%", reason)
            if reason:
                counter[reason[:80]] += 1
                total += 1
    except Exception:
        pass
    return counter, total


def _daily_state():
    """Load today's daily risk state (from common.py)."""
    try:
        from common import get_daily_state
        return get_daily_state()
    except Exception:
        return {}


def gather():
    account = _get("/v2/account")
    positions = normalize_positions(_get("/v2/positions"))
    orders = _todays_orders()
    regime = detect_regime()
    open_ctx = _open_trades()
    rej_counter, rej_total = _rejection_tally()
    daily_state = _daily_state()
    stats = {}
    intel = {}
    if learning:
        try:
            stats = learning.compute_stats()
        except Exception:
            stats = {}
    if intelligence:
        try:
            intelligence.refresh_intelligence()
            intel = intelligence.get_intelligence_summary()
        except Exception:
            intel = {}
    return {
        "account": account, "positions": positions, "orders": orders,
        "regime": regime, "open_ctx": open_ctx,
        "rejections": rej_counter, "rejection_total": rej_total, "stats": stats,
        "daily_state": daily_state,
        "intelligence": intel,
    }


# ── Position strategy mapping ───────────────────────────────────────────────────
def _strategy_for(sym, open_ctx):
    """The setup a position was entered under, else its watchlist-eligible strategies."""
    norm = normalize_symbol(sym)
    ctx = open_ctx.get(norm) or open_ctx.get(sym)
    if ctx and ctx.get("setup_type"):
        conv = ctx.get("conviction")
        held_since = (ctx.get("timestamp_entry") or "")[:16].replace("T", " ")
        extra = f" (conv {conv}, since {held_since})" if conv else ""
        return f"{ctx['setup_type']}{extra}"
    for s in load_watchlist().get("watchlist", []):
        if normalize_symbol(s["symbol"]) == norm:
            strats = ", ".join(s.get("strategies", [])) or "—"
            return f"untracked entry · eligible: {strats}"
    return "untracked"


# ── Report text ──────────────────────────────────────────────────────────────
def build_text(session_type, data):
    a = data["account"]
    positions = data["positions"]
    orders = data["orders"]
    regime = data["regime"]
    open_ctx = data["open_ctx"]
    stats = data["stats"]
    intel = data.get("intelligence") or {}
    daily_state = data.get("daily_state", {})
    risk = load_risk()

    equity = float(a.get("equity", 0))
    cash = float(a.get("cash", 0))
    last_eq = float(a.get("last_equity", equity))
    day_pnl = equity - last_eq
    day_pnl_pct = (day_pnl / last_eq * 100) if last_eq else 0
    total_pnl = equity - STARTING_CAPITAL
    total_pnl_pct = total_pnl / STARTING_CAPITAL * 100
    invested = float(a.get("position_market_value", equity - cash) or 0)
    crypto_mv = sum(float(p.get("market_value", 0) or 0)
                    for p in positions if is_crypto(p.get("symbol", ""), p.get("asset_class")))

    title_map = {
        "eod":        "END-OF-DAY REPORT (equity close)",
        "afterhours": "AFTER-HOURS REPORT (extended close)",
        "marketopen": "MARKET OPEN SUMMARY (overnight crypto recap)",
        "now":        "SESSION SNAPSHOT",
    }
    L = []
    L.append(f"# MINDHUB TRADER — {title_map.get(session_type, 'SESSION REPORT')}")
    L.append(f"_{now_mt().strftime('%A, %B %d %Y · %H:%M')} {mt_tz_label()} · phase={market_phase()}_")
    L.append("")
    L.append("## 1. Account & P&L")
    L.append(f"- Equity: **${equity:,.2f}**  |  Cash: ${cash:,.2f}  |  Invested: ${invested:,.2f}")
    L.append(f"- Day P&L: **${day_pnl:+,.2f} ({day_pnl_pct:+.2f}%)**")
    L.append(f"- Since inception (${STARTING_CAPITAL:,.0f}): **${total_pnl:+,.2f} ({total_pnl_pct:+.2f}%)**")
    L.append(f"- Crypto exposure: ${crypto_mv:,.0f} = {(crypto_mv/equity*100 if equity else 0):.1f}% of equity "
             f"(cap {risk.get('max_crypto_exposure_pct')}%)")
    cash_pct = cash / equity * 100 if equity else 0
    L.append(f"- Cash reserve: {cash_pct:.1f}% (min {risk.get('cash_reserve_pct')}%)"
             + ("  ⚠️ BELOW RESERVE" if cash_pct < risk.get('cash_reserve_pct', 5) else ""))
    dd_flag = " ⚠️ DRAWDOWN HALT TRIGGERED" if day_pnl_pct <= -risk.get("max_daily_drawdown_pct", 6) else ""
    L.append(f"- Daily drawdown limit: {risk.get('max_daily_drawdown_pct')}%{dd_flag}")

    # ── Intraday risk timeline ───────────────────────────────────────────────
    max_crypto_intraday = daily_state.get("max_intraday_crypto_pct", crypto_mv / equity * 100 if equity else 0)
    max_drawdown_intraday = daily_state.get("max_intraday_drawdown_pct", day_pnl_pct)
    cap_breaches = daily_state.get("cap_breaches", [])
    untracked_count = daily_state.get("untracked_entry_count", 0)
    loss_lockout = daily_state.get("loss_streak_lockout", False)
    soft_stop_hit = daily_state.get("soft_stop_active", False)
    hard_stop_hit = daily_state.get("hard_stop_active", False)

    L.append("")
    L.append("### Intraday Risk Timeline")
    L.append(f"- Max intraday crypto exposure: **{max_crypto_intraday:.1f}%** "
             f"(cap {risk.get('max_crypto_exposure_pct')}%) "
             + ("  🚨 BREACH" if max_crypto_intraday > risk.get('max_crypto_exposure_pct', 40) else "  ✅"))
    L.append(f"- Max intraday drawdown: **{max_drawdown_intraday:+.2f}%** "
             + ("  ⚠️" if max_drawdown_intraday < -2 else "  ✅"))
    if soft_stop_hit or hard_stop_hit:
        stop_type = "HARD STOP" if hard_stop_hit else "SOFT STOP"
        L.append(f"- Daily stop triggered: **{stop_type}** ⚠️")
    else:
        L.append("- Daily stops: Not triggered ✅")
    if cap_breaches:
        L.append(f"- Cap breach events: {len(cap_breaches)}")
        for b in cap_breaches:
            L.append(f"    - {b.get('type')} at {b.get('pnl_pct', '?')}% P&L (at {b.get('ts', '?')[:16]})")
    else:
        L.append("- Cap breaches: None ✅")
    if untracked_count:
        L.append(f"- Untracked-entry blocks: **{untracked_count}** ⚠️ (buys rejected for missing setup_type)")
    else:
        L.append("- Untracked-entry blocks: 0 ✅")
    if loss_lockout:
        L.append("- Loss-streak lockout: **ACTIVE** ⚠️")

    L.append("")
    L.append("## 2. Market Regime")
    L.append(f"- Regime: **{regime.get('regime')}** · sizing ×{regime.get('multiplier',0.6)*100:.0f}% "
             f"· stop {regime.get('stop_loss_pct','?')}% · VIX {regime.get('vix','?')}")
    if regime.get("signals"):
        L.append(f"- Signals: {', '.join(regime['signals'][:6])}")

    L.append("")
    L.append(f"## 3. Open Positions ({len(positions)}) — with active strategy")
    if positions:
        for p in sorted(positions, key=lambda x: float(x.get("unrealized_pl", 0) or 0)):
            sym = p["symbol"]
            qty = float(p.get("qty", 0) or 0)
            entry = float(p.get("avg_entry_price", 0) or 0)
            curr = float(p.get("current_price", 0) or 0)
            mv = float(p.get("market_value", 0) or 0)
            pl = float(p.get("unrealized_pl", 0) or 0)
            plpc = float(p.get("unrealized_plpc", 0) or 0) * 100
            flag = "  🔴STOP-WATCH" if plpc <= regime.get("stop_loss_pct", -5) else ""
            L.append(f"- **{sym}** {qty:g} @ ${entry:,.4f} → ${curr:,.4f} "
                     f"| MV ${mv:,.0f} | P&L ${pl:+,.2f} ({plpc:+.2f}%){flag}")
            L.append(f"    strategy: {_strategy_for(sym, open_ctx)}")
    else:
        L.append("- Flat. No open positions.")

    L.append("")
    L.append("## 4. Orders Today")
    if orders:
        status_counts = Counter(o.get("status", "?") for o in orders)
        L.append(f"- {len(orders)} orders: " + ", ".join(f"{k}={v}" for k, v in status_counts.items()))
        for o in orders[:40]:
            t = (o.get("submitted_at") or "")[11:16]
            side = (o.get("side") or "").upper()
            lp = o.get("limit_price")
            fa = o.get("filled_avg_price")
            px = f"@${float(fa):,.4f}(fill)" if fa else (f"@${float(lp):,.4f}" if lp else "")
            L.append(f"  - `{t}` {side} {o.get('qty')} {o.get('symbol')} {px} → **{o.get('status')}**")
    else:
        L.append("- No orders submitted today.")

    L.append("")
    L.append("## 5. Rejected-Order Analysis (why orders didn't go through)")
    if data["rejection_total"]:
        L.append(f"- {data['rejection_total']} validator rejections today, grouped by reason:")
        for reason, n in data["rejections"].most_common():
            L.append(f"  - ×{n} — {reason}")
        L.append("- Note: post-fix, SELL orders are never blocked by cash-reserve/allocation; "
                 "these reasons should now be buy-side caps (allocation/cash/crypto-cap) only.")
    else:
        L.append("- No validator rejections logged today. ✅")

    L.append("")
    L.append("## 6. Learning Stats (all-time)")
    if stats and stats.get("total_trades"):
        L.append(f"- Trades: {stats['total_trades']} | Win rate: {stats['win_rate']}% | "
                 f"Profit factor: {stats.get('profit_factor','N/A')} | "
                 f"Expectancy/trade: {stats.get('expectancy_pct','?')}%")
        L.append(f"- Avg win +{stats['avg_win_pct']}% | Avg loss {stats['avg_loss_pct']}% | "
                 f"Total realized P&L ${stats['total_pnl']:+,.2f}")
        streak = stats.get("current_streak", {})
        L.append(f"- Current streak: {streak.get('count')}× {streak.get('type')}")
        by_setup = stats.get("by_setup", {})
        if by_setup:
            ranked = sorted(by_setup.items(), key=lambda x: x[1]["total_pnl"], reverse=True)
            L.append("- By setup: " + " | ".join(
                f"{k} {v['win_rate']}%WR ${v['total_pnl']:+,.0f}" for k, v in ranked[:6]))
    else:
        L.append("- No completed trades recorded yet (loop now persists every exit going forward).")

    L.append("")
    L.append("## 7. Decision Intelligence (recent candidate outcomes)")
    if intel.get("evaluated_candidates"):
        for line in intel.get("brief_lines", []):
            if line.startswith("==="):
                continue
            L.append(f"- {line}")
        if intel.get("missed_opportunities"):
            miss = intel["missed_opportunities"][0]
            blockers = ", ".join(miss.get("blockers", [])) or "none noted"
            L.append(f"- Biggest missed opportunity: {miss.get('symbol')} {miss.get('max_up_pct', 0):+.2f}% after SKIP/WATCH | blockers: {blockers}")
        if intel.get("bad_buy_candidates"):
            bad = intel["bad_buy_candidates"][0]
            L.append(f"- Worst false-positive buy: {bad.get('symbol')} {bad.get('return_pct', 0):+.2f}% over {bad.get('horizon')}")
    else:
        L.append("- Not enough evaluated candidate decisions yet. Structured opportunity scoring will populate over the next sessions.")

    L.append("")
    L.append("## 8. Next-Session Watch")
    try:
        from screener import get_discovery_brief
        L.append("```")
        L.append(get_discovery_brief())
        L.append("```")
    except Exception:
        L.append("- Discovery scanner unavailable.")

    L.append("")
    L.append("## 9. Machine-Readable Appendix (for external AI)")
    L.append("```json")
    L.append(json.dumps({
        "session": session_type,
        "timestamp_mt": now_mt_str(),
        "equity": equity, "cash": cash, "day_pnl": round(day_pnl, 2),
        "day_pnl_pct": round(day_pnl_pct, 2),
        "total_pnl_pct": round(total_pnl_pct, 2),
        "crypto_exposure_pct_closing": round(crypto_mv / equity * 100, 1) if equity else 0,
        "max_intraday_crypto_pct": round(daily_state.get("max_intraday_crypto_pct", 0), 1),
        "max_intraday_drawdown_pct": round(daily_state.get("max_intraday_drawdown_pct", 0), 2),
        "soft_stop_hit": daily_state.get("soft_stop_active", False),
        "hard_stop_hit": daily_state.get("hard_stop_active", False),
        "cap_breaches": daily_state.get("cap_breaches", []),
        "untracked_entry_count": daily_state.get("untracked_entry_count", 0),
        "loss_streak_lockout": daily_state.get("loss_streak_lockout", False),
        "regime": regime.get("regime"), "vix": regime.get("vix"),
        "open_positions": [{"symbol": p["symbol"], "qty": float(p.get("qty", 0) or 0),
                            "pnl_pct": round(float(p.get("unrealized_plpc", 0) or 0) * 100, 2),
                            "strategy": _strategy_for(p["symbol"], open_ctx)} for p in positions],
        "order_count": len(orders),
        "rejections_total": data["rejection_total"],
        "rejections": dict(data["rejections"]),
        "stats": {k: stats.get(k) for k in
                  ("total_trades", "win_rate", "profit_factor", "expectancy_pct", "total_pnl")} if stats else {},
        "intelligence": {
            "evaluated_candidates": intel.get("evaluated_candidates", 0),
            "total_candidates": intel.get("total_candidates", 0),
            "missed_opportunities": intel.get("missed_opportunities", [])[:3],
            "bad_buy_candidates": intel.get("bad_buy_candidates", [])[:3],
            "blockers_on_missed_winners": intel.get("blockers_on_missed_winners", [])[:3],
        },
    }, indent=2, default=str))
    L.append("```")
    L.append("")
    L.append("_Paste this report into your analysis AI; bring its refinements back to Claude Code._")
    return "\n".join(L)


def post(session_type="now"):
    data = gather()
    text = build_text(session_type, data)

    fname = f"{today_mt()}-{session_type}.md"
    out_path = REPORTS_DIR / fname
    out_path.write_text(text, encoding="utf-8")

    a = data["account"]
    equity = float(a.get("equity", 0))
    last_eq = float(a.get("last_equity", equity))
    day_pnl = equity - last_eq
    icon = "📈" if day_pnl >= 0 else "📉"
    summary = (f"**{session_type.upper()}** · {now_mt_str('%H:%M')} {mt_tz_label()}\n"
               f"Equity **${equity:,.2f}** · Day P&L **${day_pnl:+,.2f}** · "
               f"{len(data['positions'])} positions · {len(data['orders'])} orders · "
               f"{data['rejection_total']} rejections\n"
               f"Full detailed report attached ⬇️ (export to your analysis AI).")
    if dn:
        try:
            dn.send_file(fname, text, title=f"{icon} MindHub {session_type.upper()} Report", description=summary)
        except Exception as e:
            print(f"Discord post failed: {e}")
    print(f"Report written → {out_path}")
    return out_path


if __name__ == "__main__":
    session = sys.argv[1] if len(sys.argv) > 1 else "now"
    post(session)
