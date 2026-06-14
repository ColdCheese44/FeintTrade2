"""
Anthropic API spend tracker.

Anthropic exposes no public *balance* endpoint, so we track SPEND from the per-call
cost log (logs/api_usage.jsonl) and show today / month / all-time totals, a month
projection, and an optional budget gauge — enough to know when to fund the account.

Pure + testable: spend_summary() accepts an explicit records list. Used by the
dashboard spend panel and the !cost Discord command.
"""

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "api_usage.jsonl"


def _records() -> list:
    try:
        return [json.loads(line) for line in LOG.read_text(encoding="utf-8").splitlines() if line.strip()]
    except Exception:
        return []


def _monthly_budget() -> float | None:
    try:
        cfg = json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))
        b = (cfg.get("api_config") or {}).get("monthly_budget_usd")
        return float(b) if b else None
    except Exception:
        return None


def spend_summary(records: list | None = None, monthly_budget: float | None = None,
                  now: datetime | None = None) -> dict:
    """Summarize API spend. Pass `records`/`now` for hermetic tests."""
    recs = records if records is not None else _records()
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")
    budget = monthly_budget if monthly_budget is not None else _monthly_budget()

    today_total = month_total = all_total = 0.0
    calls_today = 0
    by_routine: dict = {}
    for r in recs:
        c = float(r.get("cost_usd", 0) or 0)
        ts = str(r.get("ts", ""))
        all_total += c
        if ts.startswith(month):
            month_total += c
            k = r.get("routine", "?")
            by_routine[k] = by_routine.get(k, 0.0) + c
        if ts.startswith(today):
            today_total += c
            calls_today += 1

    day = now.day or 1
    out = {
        "today": round(today_total, 4),
        "month": round(month_total, 2),
        "all_time": round(all_total, 2),
        "calls_today": calls_today,
        "projected_month": round(month_total / day * 30, 2),
        "by_routine": {k: round(v, 4) for k, v in sorted(by_routine.items(), key=lambda x: -x[1])},
    }
    if budget:
        out["budget"] = float(budget)
        out["budget_used_pct"] = round(month_total / budget * 100, 1) if budget else None
        out["budget_remaining"] = round(budget - month_total, 2)
        out["fund_soon"] = bool(month_total >= 0.8 * budget)
    return out


def format_brief(s: dict | None = None) -> str:
    s = s or spend_summary()
    lines = [f"💸 **Anthropic API spend** — today **${s['today']:.4f}** · "
             f"month **${s['month']:.2f}** · all-time **${s['all_time']:.2f}**",
             f"📈 Projected this month: ~${s.get('projected_month', 0):.2f}  ·  {s.get('calls_today', 0)} calls today"]
    if s.get("budget"):
        warn = " ⚠️ fund soon" if s.get("fund_soon") else ""
        lines.append(f"📊 Budget: ${s['month']:.2f} / ${s['budget']:.0f} "
                     f"({s.get('budget_used_pct', 0):.0f}% used, ${s.get('budget_remaining', 0):.2f} left){warn}")
    else:
        lines.append("ℹ️ No monthly budget set (api_config.monthly_budget_usd). "
                     "Anthropic has no balance API — check console.anthropic.com to fund.")
    if s.get("by_routine"):
        top = list(s["by_routine"].items())[:4]
        lines.append("Top routines (month): " + ", ".join(f"`{k}` ${v:.2f}" for k, v in top))
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_brief())
