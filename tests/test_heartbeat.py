"""Local heartbeat persistence must not require or spam Discord."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import heartbeat


def test_write_heartbeat_uses_repo_file_and_can_skip_discord(monkeypatch, tmp_path):
    target = tmp_path / "heartbeat.json"
    monkeypatch.setattr(heartbeat, "HEARTBEAT_FILE", target)

    returned = heartbeat.write_heartbeat("ok", "crypto-complete", notify=False)

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert returned == payload
    assert payload["status"] == "ok"
    assert payload["notes"] == "crypto-complete"
    assert payload["timestamp"]
