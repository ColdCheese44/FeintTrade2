"""
Auto-updating watchlist.

The static watchlist.json is the core, not the boundary. This module watches the
marketwide-discovery scanner over time and PROMOTES recurring, high-quality candidates
into a persisted *dynamic* watchlist, DEMOTES stale ones, and surfaces the active set
to the decision prompts so the agent's universe keeps growing on its own.

It never rewrites watchlist.json — it maintains data/dynamic_watchlist.json only.
Promotions/demotions are posted to #ft-watchlist.

CLI:
    python scripts/watchlist_manager.py            # run one update + print active list
    python scripts/watchlist_manager.py show        # print active list only
"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "dynamic_watchlist.json"

DEFAULTS = {
    "enabled": True,
    "promote_min_appearances": 3,   # times seen in discovery before promotion
    "promote_min_score": 4,         # best discovery score required to promote
    "demote_after_days": 4,         # drop if not seen this long
    "max_active": 12,               # cap on dynamic symbols
    "tracker_window_days": 14,      # prune tracker entries older than this
}


def _cfg() -> dict:
    try:
        full = json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))
        return {**DEFAULTS, **(full.get("dynamic_watchlist") or {})}
    except Exception:
        return dict(DEFAULTS)


def _load() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"updated_at": None, "tracker": {}, "active": []}


def _save(state: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def update(discovery: dict | None = None, today: str | None = None) -> dict:
    """
    Ingest the current discovery candidates, update the rolling tracker, then promote
    and demote. Pure w.r.t. I/O except the state file — does NOT post to Discord.
    Returns {"promoted": [...], "demoted": [...], "active": [...]}.
    """
    cfg = _cfg()
    if not cfg.get("enabled", True):
        return {"promoted": [], "demoted": [], "active": _load().get("active", [])}
    if discovery is None:
        try:
            from screener import discover
            discovery = discover()
        except Exception:
            discovery = {"candidates": []}
    today = today or date.today().isoformat()

    state = _load()
    tracker = state.setdefault("tracker", {})
    active = set(state.get("active", []))

    # Ingest current candidates (skip penny-caution names — too manipulation-prone to promote).
    for c in discovery.get("candidates", []):
        sym = c.get("symbol")
        if not sym or c.get("penny_caution"):
            continue
        t = tracker.setdefault(sym, {"appearances": 0, "best_score": 0,
                                     "type": c.get("type", "equity"), "first_seen": today})
        t["appearances"] = t.get("appearances", 0) + 1
        t["best_score"] = max(t.get("best_score", 0), c.get("score", 0))
        t["last_seen"] = today
        t["last_reason"] = c.get("reason", "")
        t["type"] = c.get("type", t.get("type", "equity"))

    # Promote
    promoted = []
    for sym, t in tracker.items():
        if (sym not in active
                and t.get("appearances", 0) >= cfg["promote_min_appearances"]
                and t.get("best_score", 0) >= cfg["promote_min_score"]):
            active.add(sym)
            t["promoted_at"] = today
            promoted.append(sym)

    # Demote stale
    demoted = []
    cutoff = (date.fromisoformat(today) - timedelta(days=cfg["demote_after_days"])).isoformat()
    for sym in list(active):
        last = tracker.get(sym, {}).get("last_seen", "")
        if last and last < cutoff:
            active.discard(sym)
            demoted.append(sym)

    # Cap active (keep the highest best_score)
    if len(active) > cfg["max_active"]:
        ranked = sorted(active, key=lambda s: tracker.get(s, {}).get("best_score", 0), reverse=True)
        for sym in ranked[cfg["max_active"]:]:
            active.discard(sym)
            demoted.append(sym)

    # Prune ancient tracker entries (keep anything still active)
    wcut = (date.fromisoformat(today) - timedelta(days=cfg["tracker_window_days"])).isoformat()
    tracker = {s: t for s, t in tracker.items() if t.get("last_seen", "") >= wcut or s in active}

    state["tracker"] = tracker
    state["active"] = sorted(active)
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _save(state)
    return {"promoted": promoted, "demoted": demoted, "active": state["active"]}


def active_symbols() -> list:
    return _load().get("active", [])


def brief() -> str:
    """Formatted active dynamic watchlist for prompt injection (empty when none)."""
    state = _load()
    active = state.get("active", [])
    tracker = state.get("tracker", {})
    if not active:
        return ""
    lines = ["=== AUTO-WATCHLIST (promoted from recurring marketwide discovery — NOT pre-vetted; "
             "apply the FULL SOP, capped by the risk engine) ==="]
    for sym in active:
        t = tracker.get(sym, {})
        lines.append(f"  {sym} ({t.get('type', 'equity')}) — seen {t.get('appearances', '?')}x, "
                     f"best score {t.get('best_score', '?')}, {str(t.get('last_reason', ''))[:50]}")
    return "\n".join(lines)


def run_and_post() -> dict:
    """Run an update and post any promotions/demotions to #ft-watchlist. For the
    orchestrator/CLI. Returns the change set."""
    changes = update()
    if changes["promoted"] or changes["demoted"]:
        try:
            import discord_notify as dn
            dn.watchlist_update(changes["promoted"], changes["demoted"], changes["active"])
        except Exception:
            pass
        try:
            import activity
            activity.log("watchlist_update",
                         f"+{len(changes['promoted'])} -{len(changes['demoted'])} "
                         f"({len(changes['active'])} active)",
                         promoted=changes["promoted"] or None, demoted=changes["demoted"] or None)
        except Exception:
            pass
    return changes


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "show":
        print("Active dynamic watchlist:", active_symbols() or "(none)")
    else:
        ch = run_and_post()
        print(f"Promoted: {ch['promoted'] or '—'}")
        print(f"Demoted:  {ch['demoted'] or '—'}")
        print(f"Active:   {ch['active'] or '—'}")
