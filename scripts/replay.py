"""
Replay / benchmark performance report (ported from FeintTrade's replay reports).

FeintTrade's learning.py already computes win rate, expectancy, and profit factor. This
adds the comparison FeintTrade emphasized but FeintTrade lacked: realized P&L measured
against benchmark baselines — **buy-and-hold (SPY)** and **no-trade (cash)** — over the
actual trading window, plus **max drawdown** and **payoff ratio**. The summary posts to
#ft-reports.

CLI:
    python scripts/replay.py            # print the benchmark report
    python scripts/replay.py post       # print AND post it to #ft-reports
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import learning

try:
    from common import load_live_account
except Exception:                       # pragma: no cover
    def load_live_account():
        return {}


def _starting_capital() -> float:
    """Capital base for the realized-return %: the small-account size in live-sim
    mode, otherwise the $100k paper start."""
    la = load_live_account() or {}
    if la.get("enabled"):
        return float(la.get("starting_capital", 100.0))
    return 100_000.0


def _completed() -> list:
    """Completed trades with a P&L and exit timestamp, oldest → newest."""
    trades = learning._load_trade_log()
    c = [t for t in trades
         if t.get("outcome") and t.get("pnl_pct") is not None and t.get("timestamp_exit")]
    c.sort(key=lambda t: t["timestamp_exit"])
    return c


def max_drawdown(completed: list, starting: float) -> float:
    """Peak-to-trough drawdown (%) of the realized equity curve."""
    eq = peak = starting
    mdd = 0.0
    for t in completed:
        eq += t.get("pnl_dollar", 0) or 0
        peak = max(peak, eq)
        if peak > 0:
            mdd = max(mdd, (peak - eq) / peak)
    return round(mdd * 100, 2)


def spy_buy_and_hold_pct(start_date: str, end_date: str):
    """SPY total return (%) over [start_date, end_date], or None if unavailable."""
    try:
        import yfinance as yf
        h = yf.Ticker("SPY").history(start=start_date, end=end_date or None)
        if len(h) >= 2:
            return round((h["Close"].iloc[-1] / h["Close"].iloc[0] - 1) * 100, 2)
    except Exception:
        pass
    return None


def benchmark_report(starting_capital: float = None) -> dict:
    """Realized performance vs buy-and-hold (SPY) and no-trade (cash) baselines."""
    completed = _completed()
    if not completed:
        return {"trades": 0, "message": "No completed trades yet."}
    starting = starting_capital or _starting_capital()
    total_pnl = sum(t.get("pnl_dollar", 0) or 0 for t in completed)
    agent_return = round(total_pnl / starting * 100, 2) if starting else 0.0
    start_date = completed[0]["timestamp_exit"][:10]
    end_date = completed[-1]["timestamp_exit"][:10]
    bh = spy_buy_and_hold_pct(start_date, end_date)
    stats = learning.compute_stats(completed)
    avg_loss = stats.get("avg_loss_pct") or 0
    payoff = round(abs((stats.get("avg_win_pct") or 0) / avg_loss), 2) if avg_loss else None
    return {
        "trades":               len(completed),
        "window":               f"{start_date} → {end_date}",
        "agent_return_pct":     agent_return,
        "total_pnl":            round(total_pnl, 2),
        "buy_and_hold_spy_pct": bh,
        "no_trade_pct":         0.0,
        "alpha_vs_spy":         round(agent_return - bh, 2) if bh is not None else None,
        "max_drawdown_pct":     max_drawdown(completed, starting),
        "win_rate":             stats.get("win_rate"),
        "profit_factor":        stats.get("profit_factor"),
        "payoff_ratio":         payoff,
        "expectancy_pct":       stats.get("expectancy_pct"),
    }


def format_report(r: dict) -> str:
    if r.get("trades", 0) == 0:
        return "**Replay / Benchmark** — no completed trades yet."
    bh = r["buy_and_hold_spy_pct"]
    alpha = r.get("alpha_vs_spy")
    if alpha is None:
        verdict = "SPY data unavailable — compared to cash only."
    elif alpha >= 0:
        verdict = f"✅ Beating buy-and-hold by {alpha:+.2f}%."
    else:
        verdict = f"⚠️ Lagging buy-and-hold by {alpha:+.2f}%."
    lines = [
        f"**Replay / Benchmark** — {r['window']} · {r['trades']} trades",
        f"📈 Agent realized: **{r['agent_return_pct']:+.2f}%**  (${r['total_pnl']:+,.2f})",
        f"🟰 Buy & hold SPY: {bh:+.2f}%" if bh is not None else "🟰 Buy & hold SPY: n/a",
        "💵 No-trade (cash): 0.00%",
        f"📊 Win {r['win_rate']}% · PF {r['profit_factor']} · payoff {r['payoff_ratio']} · "
        f"expectancy {r['expectancy_pct']}%",
        f"📉 Max drawdown: -{r['max_drawdown_pct']}%",
        verdict,
    ]
    return "\n".join(lines)


def post_report() -> bool:
    """Post the benchmark summary to #ft-reports."""
    body = format_report(benchmark_report())
    try:
        import discord_notify as dn
        dn.send(title="📊 Replay / Benchmark Report", description=body, msg_type="report")
        return True
    except Exception as e:
        print(f"Report post failed: {e}")
        return False


if __name__ == "__main__":
    print(format_report(benchmark_report()))
    if len(sys.argv) > 1 and sys.argv[1] == "post":
        print("posted:", post_report())
