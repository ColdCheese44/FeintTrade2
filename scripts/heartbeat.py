"""Write heartbeat.json and send a Discord ping."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def write_heartbeat(status="ok", notes=""):
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "notes": notes,
    }
    with open("heartbeat.json", "w") as f:
        json.dump(payload, f, indent=2)

    try:
        root = Path(__file__).parent.parent
        sys.path.insert(0, str(root / "scripts"))
        import discord_notify as dn
        dn.heartbeat(notes or "agent", status=status)
    except Exception:
        pass

    print(json.dumps(payload))


if __name__ == "__main__":
    status = sys.argv[1] if len(sys.argv) > 1 else "ok"
    notes  = sys.argv[2] if len(sys.argv) > 2 else ""
    write_heartbeat(status, notes)
