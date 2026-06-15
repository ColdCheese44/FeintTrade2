"""
Decision-intelligence audit — READ-ONLY analysis (changes nothing about trading).

Two jobs:
  1. Surface the buried data/intelligence_summary.json (by_action, by_setup, missed
     winners, bad buys, blockers-on-winners) into a readable report / Discord post /
     dashboard view.
  2. Add per-blocker PREDICTIVENESS by joining decision_log.jsonl (blockers on
     skip/watch decisions) with decision_outcomes.json (forward 1h/4h/24h returns):
     for each blocker, the avg realized return of the name it blocked. Positive ⇒ it
     blocked WINNERS (over-restrictive, review candidate); negative ⇒ it blocked
     LOSERS (protective, keep). This quantifies which blockers are predictive vs noise
     so the operator refines the strategy on evidence, not vibes.

CLI:
    python scripts/intel_audit.py            # print the report
    python scripts/intel_audit.py post        # print + post to #ft-reports
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SUMMARY = DATA / "intelligence_summary.json"
LOG = DATA / "decision_log.jsonl"
OUTCOMES = DATA / "decision_outcomes.json"

_SKIP_ACTIONS = ("skip", "watch", "hold_watch", "watch_stop")

# Map verbose blocker strings to short canonical keys for grouping.
_BLOCKER_KEYS = [
    ("price_below_vwap", ("below_vwap", "below_intraday_vwap", "not_above_vwap")),
    ("ema9_below_ema21", ("ema9", "ema_signal_bearish", "ema_below")),
    ("squeeze_bearish", ("squeeze_released_bearish", "bearish_squeeze", "no_bullish_squeeze")),
    ("obv_falling", ("obv_confirmed_falling", "obv_falling", "obv_bearish")),
    ("macd_bearish", ("macd_bearish", "macd_bear")),
    ("rsi_block", ("rsi",)),
    ("no_volume", ("no_volume", "volume_below", "low_volume", "volume_spike")),
    ("fib_no_trigger", ("fib", "fibonacci")),
    ("earnings_risk", ("earnings",)),
    ("regime_block", ("regime", "bear_regime", "panic")),
    ("price_fade", ("fade", "fading", "rejected")),
]


def _canon_blocker(b: str) -> str:
    s = str(b).lower()
    for key, needles in _BLOCKER_KEYS:
        if any(n in s for n in needles):
            return key
    return s[:36]


def load_summary() -> dict:
    try:
        return json.loads(SUMMARY.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _iter_log(path: Path = None):
    path = path or LOG
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except Exception:
                        continue
    except Exception:
        return


def blocker_predictiveness(horizon: str = "24h", min_count: int = 15,
                           log_records=None, outcomes=None) -> list:
    """Per-blocker avg forward return on the skip/watch decisions it appeared on.
    Pass log_records/outcomes for hermetic tests; else reads the data files."""
    if outcomes is None:
        try:
            outcomes = json.loads(OUTCOMES.read_text(encoding="utf-8"))
        except Exception:
            outcomes = {}
    records = log_records if log_records is not None else _iter_log()

    agg = defaultdict(lambda: {"n": 0, "sum_ret": 0.0, "sum_up": 0.0})
    for rec in records:
        if str(rec.get("action", "")).lower() not in _SKIP_ACTIONS:
            continue
        did = rec.get("decision_id")
        o = (outcomes.get(did) or {}).get(horizon) if did else None
        if not o or o.get("return_pct") is None:
            continue
        ret = float(o["return_pct"])
        up = float(o.get("max_up_pct", 0) or 0)
        for b in (rec.get("blockers") or []):
            a = agg[_canon_blocker(b)]
            a["n"] += 1
            a["sum_ret"] += ret
            a["sum_up"] += up

    rows = []
    for b, a in agg.items():
        if a["n"] < min_count:
            continue
        avg_ret = a["sum_ret"] / a["n"]
        avg_up = a["sum_up"] / a["n"]
        if avg_ret > 0.5:
            verdict = "🔴 over-restrictive"
        elif avg_ret < -1.0:
            verdict = "🟢 protective"
        else:
            verdict = "⚪ neutral"
        rows.append({"blocker": b, "count": a["n"], "avg_return_pct": round(avg_ret, 2),
                     "avg_max_up_pct": round(avg_up, 2), "verdict": verdict})
    rows.sort(key=lambda r: -r["count"])
    return rows


def audit() -> dict:
    """Structured audit data for the dashboard / Discord / report."""
    s = load_summary()
    by_action = sorted((s.get("by_action") or {}).items(),
                       key=lambda kv: kv[1].get("avg_primary_return_pct", 0))
    by_setup = sorted((s.get("by_setup") or {}).items(),
                      key=lambda kv: kv[1].get("avg_primary_return_pct", 0))
    return {
        "generated_at": s.get("generated_at"),
        "lookback_days": s.get("lookback_days"),
        "total_candidates": s.get("total_candidates"),
        "evaluated": s.get("evaluated_candidates"),
        "by_action": by_action,
        "by_setup": by_setup,
        "bad_buys": s.get("bad_buy_candidates") or [],
        "missed": s.get("missed_opportunities") or [],
        "blockers_on_winners": s.get("blockers_on_missed_winners") or [],
        "blocker_predictiveness": blocker_predictiveness(),
        "brief_lines": s.get("brief_lines") or [],
    }


def format_report(a: dict = None) -> str:
    a = a or audit()
    if not a.get("by_action") and not a.get("brief_lines"):
        return "# Decision-Intelligence Audit\n\nNo evaluated decisions yet."
    L = [f"# 🧠 Decision-Intelligence Audit ({a.get('lookback_days', '?')}-day window)",
         f"_{a.get('total_candidates', '?')} candidates logged · {a.get('evaluated', '?')} evaluated · "
         f"generated {str(a.get('generated_at'))[:19]}_", ""]
    L.append("## Forward return by ACTION (worst → best)")
    for act, st in a["by_action"]:
        L.append(f"- **{act}** ({st.get('count')}×): {st.get('avg_primary_return_pct'):+.2f}% avg")
    L.append("\n## Forward return by SETUP (worst → best)")
    for setup, st in a["by_setup"][:10]:
        L.append(f"- **{setup}** ({st.get('count')}×): {st.get('avg_primary_return_pct'):+.2f}% avg")
    if a["blocker_predictiveness"]:
        L.append("\n## Blocker predictiveness (on skipped/watched names, 24h)")
        L.append("_Positive avg = blocked a winner (review); negative = blocked a loser (keep)._")
        for r in a["blocker_predictiveness"][:12]:
            L.append(f"- `{r['blocker']}` ({r['count']}×): {r['avg_return_pct']:+.2f}% avg "
                     f"(max-up {r['avg_max_up_pct']:+.2f}%) — {r['verdict']}")
    if a["missed"]:
        L.append("\n## Biggest missed winners (passed, then ran)")
        for m in a["missed"][:5]:
            L.append(f"- {m.get('symbol')} — passed at ${m.get('decision_price')}: {m.get('reasoning', '')[:90]}")
    if a["bad_buys"]:
        L.append("\n## Worst false-positive buys")
        for b in a["bad_buys"][:5]:
            L.append(f"- {b.get('symbol')} ({b.get('setup_type')}): bought at ${b.get('decision_price')}")
    L.append("\n> Read-only analysis. Do not blindly loosen blockers — review the 🔴 over-restrictive "
             "ones case-by-case (max-up can spike then fade).")
    return "\n".join(L)


def discord_embed(a: dict = None) -> dict:
    a = a or audit()
    fields = []
    if a["by_action"]:
        fields.append({"name": "📉 Return by action (worst→best)",
                       "value": "\n".join(f"{act}: {st.get('avg_primary_return_pct'):+.2f}% ({st.get('count')}×)"
                                          for act, st in a["by_action"][:6])[:1024], "inline": True})
    if a["blocker_predictiveness"]:
        fields.append({"name": "🚧 Blockers (24h fwd)",
                       "value": "\n".join(f"{r['verdict'][:2]} {r['blocker']} {r['avg_return_pct']:+.1f}%"
                                          for r in a["blocker_predictiveness"][:6])[:1024], "inline": True})
    if a["brief_lines"]:
        fields.append({"name": "Summary",
                       "value": "\n".join(str(x) for x in a["brief_lines"][1:5])[:1024], "inline": False})
    return {"title": f"🧠 Decision-Intelligence Audit — {a.get('lookback_days', '?')}d",
            "description": "What the agent keeps getting right/wrong — read-only, to guide refinement.",
            "color": 0x9b59b6, "fields": fields[:25]}


def post() -> bool:
    body = format_report()
    try:
        import discord_channels as dch
        return dch.post_file("report", "intel_audit.md", body, embed=discord_embed())
    except Exception as e:
        print(f"post failed: {e}")
        return False


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "post":
        print(format_report())
        print("\nposted:", post())
    else:
        print(format_report())
