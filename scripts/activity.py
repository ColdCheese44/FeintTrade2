"""
Structured activity log — one chronological, append-only record of everything the
system does, for the operator and future sessions to reference back to.

Writes JSON lines to logs/activity.jsonl (local; gitignored). Every entry is
{ts, event, summary, details?}. Logging must NEVER raise into the caller, so all
failures are swallowed.

Read it back with e.g.:
    python scripts/activity.py tail 40
    python scripts/activity.py grep stop_loss
"""

import json
import sys
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    _MT = ZoneInfo("America/Denver")
except Exception:                       # pragma: no cover
    _MT = None

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "activity.jsonl"


def log(event: str, summary: str, **details) -> None:
    """Append one event. event=category (e.g. 'discord_post','trade','routine_error'),
    summary=short human line, details=structured extras. Never raises."""
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(_MT) if _MT else datetime.now()
        rec = {"ts": ts.isoformat(timespec="seconds"), "event": event, "summary": str(summary)[:500]}
        clean = {k: v for k, v in details.items() if v is not None}
        if clean:
            rec["details"] = clean
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _read() -> list:
    try:
        return [json.loads(line) for line in LOG.read_text(encoding="utf-8").splitlines() if line.strip()]
    except Exception:
        return []


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tail"
    rows = _read()
    if cmd == "grep" and len(sys.argv) > 2:
        needle = sys.argv[2].lower()
        rows = [r for r in rows if needle in json.dumps(r, default=str).lower()]
    n = int(sys.argv[2]) if cmd == "tail" and len(sys.argv) > 2 else 30
    for r in rows[-n:]:
        d = f"  {json.dumps(r.get('details'), ensure_ascii=False)}" if r.get("details") else ""
        print(f"{r.get('ts','')}  [{r.get('event','')}] {r.get('summary','')}{d}")
    print(f"\n({len(rows)} total events in {LOG})")
