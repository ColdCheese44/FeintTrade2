"""
Agent-health badge + default-timeout HTTP session — FeintTrade dashboard hardening.
Run: python -m pytest tests/test_agent_health.py -v

agent_health() drives the dashboard header's liveness badge from heartbeat.json plus a
24/7 activity pulse. make_http_session() now injects a default timeout so a timeout-less
call (several existed in dashboard.py) can never hang the page.
"""

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import common
import dashboard_helpers as dh


def _now():
    return dt.datetime(2026, 6, 15, 18, 0, tzinfo=dt.timezone.utc).timestamp()


def _hb(minutes_ago, status="ok", notes="crypto-complete"):
    ts = dt.datetime(2026, 6, 15, 18, 0, tzinfo=dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)
    return {"timestamp": ts.isoformat(), "status": status, "notes": notes}


# ── agent_health states ───────────────────────────────────────────────────────

def test_health_live_when_recent():
    h = dh.agent_health(_hb(10), now=_now())
    assert h["dot"] == "🟢" and h["label"] == "live"
    assert h["notes"] == "crypto-complete"


def test_health_quiet_between_fresh_and_warn():
    h = dh.agent_health(_hb(60), now=_now())     # 60m: past 40m fresh, under 2h warn
    assert h["dot"] == "🟡" and h["label"] == "quiet"


def test_health_stalled_when_old():
    h = dh.agent_health(_hb(180), now=_now())    # 3h > 2h warn
    assert h["dot"] == "🔴" and h["label"] == "stalled"


def test_health_error_status_overrides_freshness():
    h = dh.agent_health(_hb(2, status="error"), now=_now())
    assert h["dot"] == "🔴" and "error" in h["label"]


def test_health_no_signal_when_empty():
    h = dh.agent_health({}, last_activity_ts=None, now=_now())
    assert h["label"] == "no signal" and h["age"] == "—"


def test_health_uses_freshest_of_heartbeat_and_activity():
    # Heartbeat is stale (3h) but a recent activity pulse (5m) keeps it live.
    recent = _now() - 5 * 60
    h = dh.agent_health(_hb(180), last_activity_ts=recent, now=_now())
    assert h["dot"] == "🟢" and h["label"] == "live"


def test_fmt_age():
    assert dh._fmt_age(5) == "5s"
    assert dh._fmt_age(125) == "2m"
    assert dh._fmt_age(3 * 3600 + 300) == "3h 5m"
    assert dh._fmt_age(26 * 3600) == "1d 2h"
    assert dh._fmt_age(None) == "—"


# ── default-timeout session ───────────────────────────────────────────────────

def test_session_injects_default_timeout(monkeypatch):
    captured = {}

    def fake_request(self, *a, **k):
        captured.clear()
        captured.update(k)

        class _R:
            status_code = 200
        return _R()

    monkeypatch.setattr(common.requests.Session, "request", fake_request)
    s = common.make_http_session(default_timeout=7)

    s.get("https://example.test")
    assert captured.get("timeout") == 7          # injected when none passed

    s.get("https://example.test", timeout=3)
    assert captured.get("timeout") == 3          # explicit timeout wins


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
