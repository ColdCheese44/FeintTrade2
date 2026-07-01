"""Write heartbeat.json and send a Discord ping."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEARTBEAT_FILE = ROOT / "heartbeat.json"


def write_heartbeat(status="ok", notes="", notify=True):
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "notes": notes,
    }
    with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    if notify:
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            import discord_notify as dn
            dn.heartbeat(notes or "agent", status=status)
        except Exception:
            pass

    return payload


if __name__ == "__main__":
    status = sys.argv[1] if len(sys.argv) > 1 else "ok"
    notes  = sys.argv[2] if len(sys.argv) > 2 else ""
    print(json.dumps(write_heartbeat(status, notes)))
