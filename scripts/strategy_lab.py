"""
Strategy Lab — what-if simulator over the agent's OWN decision history (READ-ONLY).

The intel audit says WHAT's wrong; this says WHAT TO CHANGE. It replays decision_log.jsonl
joined with decision_outcomes.json (forward 1h/4h/24h returns) under configurable
thresholds/filters, so the strategy is refined on evidence rather than vibes.

Headline questions it answers (the data shows buys average ~-5.6%, so this matters):
  • What CONVICTION / SIGNAL-COUNT cutoff would make BUYS profitable?
  • Which SETUPS have a positive forward edge (keep) vs negative (drop)?
  • Ranked, concrete recommendations to apply.

Changes NOTHING about live trading — it only analyses history and suggests. Pure functions
accept records/outcomes for hermetic tests.
"""

import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOG = DATA / "decision_log.jsonl"
OUTCOMES = DATA / "decision_outcomes.json"


def _conv(rec: dict):
    """Unified 1-10 conviction metric (equities use conviction, crypto uses score)."""
    c = rec.get("conviction")
    return c if c is not None else rec.get("score")


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


def join_rows(horizon: str = "24h", records=None, outcomes=None) -> list:
    """Decisions joined with their forward return. Pass records/outcomes for tests."""
    if outcomes is None:
        try:
            outcomes = json.loads(OUTCOMES.read_text(encoding="utf-8"))
        except Exception:
            outcomes = {}
    records = records if records is not None else _iter_log()
    rows = []
    for r in records:
        did = r.get("decision_id")
        o = (outcomes.get(did) or {}).get(horizon) if did else None
        if not o or o.get("return_pct") is None:
            continue
        rows.append({
            "action": str(r.get("action", "")).lower(),
            "setup": r.get("setup_type", "unknown"),
            "conv": _conv(r),
            "signals": r.get("signal_count"),
            "asset_type": r.get("asset_type"),
            "blockers": r.get("blockers") or [],
            "ret": float(o["return_pct"]),
        })
    return rows


def threshold_sweep(rows: list, field: str = "conv", action: str = "buy") -> list:
    """For each cutoff 1..10, the avg forward return of `action` decisions whose `field`
    is >= cutoff. Reveals where (if anywhere) raising the gate turns the action positive."""
    acts = [r for r in rows if r["action"] == action and r.get(field) is not None]
    out = []
    for t in range(1, 11):
        sub = [r["ret"] for r in acts if r[field] >= t]
        if sub:
            out.append({"cutoff": t, "n": len(sub), "avg_ret": round(sum(sub) / len(sub), 2)})
    return out


def setup_edge(rows: list, action: str = None, min_n: int = 4) -> list:
    """Per-setup avg forward return + verdict (keep positive edge, drop negative)."""
    agg = defaultdict(lambda: {"n": 0, "sum": 0.0})
    for r in rows:
        if action and r["action"] != action:
            continue
        a = agg[r["setup"]]
        a["n"] += 1
        a["sum"] += r["ret"]
    res = []
    for setup, a in agg.items():
        if a["n"] < min_n:
            continue
        avg = a["sum"] / a["n"]
        verdict = "🟢 keep" if avg > 0.5 else ("🔴 drop" if avg < -3 else "⚪ marginal")
        res.append({"setup": setup, "n": a["n"], "avg_ret": round(avg, 2), "verdict": verdict})
    res.sort(key=lambda x: x["avg_ret"])
    return res


def best_cutoff(sweep: list, min_n: int = 8):
    """The lowest cutoff whose avg return is positive with a usable sample, else None."""
    for row in sweep:
        if row["n"] >= min_n and row["avg_ret"] > 0:
            return row
    # else the cutoff with the highest avg return (still informative)
    usable = [r for r in sweep if r["n"] >= min_n] or sweep
    return max(usable, key=lambda r: r["avg_ret"]) if usable else None


def recommendations(rows: list = None) -> list:
    rows = rows if rows is not None else join_rows()
    recs = []
    buys = [r for r in rows if r["action"] == "buy"]
    if buys:
        overall = sum(r["ret"] for r in buys) / len(buys)
        cs = threshold_sweep(rows, "conv", "buy")
        bc = best_cutoff(cs)
        if bc and bc["avg_ret"] > overall + 1:
            recs.append(f"🎯 Raise the BUY conviction gate to ≥{bc['cutoff']}: those buys averaged "
                        f"{bc['avg_ret']:+.1f}% (n={bc['n']}) vs {overall:+.1f}% for all buys.")
        ss = threshold_sweep(rows, "signals", "buy")
        bs = best_cutoff(ss)
        if bs and bs["avg_ret"] > overall + 1:
            recs.append(f"🎯 Require ≥{bs['cutoff']} signals on buys: averaged {bs['avg_ret']:+.1f}% "
                        f"(n={bs['n']}) vs {overall:+.1f}% overall.")
    for s in setup_edge(rows)[:3]:
        if s["verdict"].startswith("🔴"):
            recs.append(f"🔴 Consider dropping setup `{s['setup']}` — {s['avg_ret']:+.1f}% over {s['n']} decisions.")
    for s in [x for x in setup_edge(rows) if x["verdict"].startswith("🟢")][:2]:
        recs.append(f"🟢 Lean into `{s['setup']}` — {s['avg_ret']:+.1f}% over {s['n']} decisions.")
    if not recs:
        recs.append("No high-confidence edge found yet — keep gathering paper data.")
    return recs


def format_report(rows: list = None) -> str:
    rows = rows if rows is not None else join_rows()
    L = ["# 🧪 Strategy Lab — what-if over the agent's own decisions (24h forward)",
         f"_{len(rows)} decisions with a measured outcome._", "",
         "## Recommendations"]
    L += [f"- {r}" for r in recommendations(rows)]
    cs = threshold_sweep(rows, "conv", "buy")
    if cs:
        L.append("\n## BUY conviction sweep (avg fwd return if gate ≥ cutoff)")
        L += [f"- ≥{r['cutoff']}: {r['avg_ret']:+.2f}% (n={r['n']})" for r in cs]
    se = setup_edge(rows)
    if se:
        L.append("\n## Setup edge (worst → best)")
        L += [f"- `{s['setup']}` {s['avg_ret']:+.2f}% (n={s['n']}) {s['verdict']}" for s in se[:12]]
    L.append("\n> Read-only — apply changes manually after review. Small samples are directional, not gospel.")
    return "\n".join(L)


def discord_embed(rows: list = None) -> dict:
    rows = rows if rows is not None else join_rows()
    return {"title": "🧪 Strategy Lab — how to make the book profitable",
            "description": "\n".join(f"• {r}" for r in recommendations(rows))[:4000],
            "color": 0x1abc9c,
            "footer": {"text": f"{len(rows)} evaluated decisions · read-only what-if"}}


def post() -> bool:
    try:
        import discord_channels as dch
        return dch.post_file("report", "strategy_lab.md", format_report(), embed=discord_embed())
    except Exception as e:
        print(f"post failed: {e}")
        return False


if __name__ == "__main__":
    import sys
    print(format_report())
    if len(sys.argv) > 1 and sys.argv[1] == "post":
        print("\nposted:", post())
