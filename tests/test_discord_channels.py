"""
Tests for the FeintTrade 10-channel Discord router (discord_channels) and the
typed-helper routing in discord_notify.

Hermetic: transport (_post_bot_json / _post_webhook_json) and config are patched, so
nothing touches the network or the real .env. State lives in tmp_path.

Run: python -m pytest tests/test_discord_channels.py -v
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import discord_channels as dch
import discord_notify as dn


@pytest.fixture
def router(monkeypatch, tmp_path):
    """A hermetic router: controlled config + channel IDs, captured transport, temp state."""
    cfg = {
        "multichannel_enabled": True,
        "routing": {
            "heartbeat": "command_post", "market_summary": "command_post",
            "proposal": "signals", "approval_card": "approvals",
            "trade": "trade_log", "decision": "trade_log",
            "stop_loss": "alerts", "take_profit": "trade_log",
            "order_rejected": "alerts", "kill": "alerts", "alert": "alerts",
            "status": "status", "report": "reports", "research": "research",
            "signals": "signals", "dev_log": "dev_log",
        },
        "channel_fallback": {
            "alerts": "command_post", "research": "reports", "reports": "command_post",
            "signals": "command_post", "trade_log": "command_post",
            "status": "command_post", "approvals": "command_post", "dev_log": "command_post",
        },
        "disabled_channels": [],
        "severity_by_type": {"kill": "critical", "order_rejected": "warning",
                             "stop_loss": "warning", "alert": "warning", "status": "notice"},
        "cooldown_min": {"critical": 5, "warning": 15, "notice": 30, "info": 0},
        "dedup_window_secs": 60,
        "quiet_hours": {"enabled": False, "start_hour": 22, "end_hour": 6, "applies_to": ["status"]},
    }
    ids = {"command_post": "100", "approvals": "200", "alerts": "300", "reports": "400",
           "signals": "500", "trade_log": "600", "status": "700", "research": "800",
           "dev_log": "900", "dev_ideas": "1000"}
    posts = []
    monkeypatch.setattr(dch, "_cfg", lambda: cfg)
    monkeypatch.setattr(dch, "activity", None)   # keep tests from writing the activity log
    monkeypatch.setattr(dch, "BOT_TOKEN", "test-token")
    monkeypatch.setattr(dch, "WEBHOOK_URL", "https://example/webhook")
    monkeypatch.setattr(dch, "_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(dch, "channel_id", lambda name: ids.get(name) or "")
    monkeypatch.setattr(dch, "_post_bot_json",
                        lambda cid, payload: posts.append(("bot", cid, payload)) or True)
    monkeypatch.setattr(dch, "_post_webhook_json",
                        lambda payload: posts.append(("webhook", None, payload)) or True)
    return cfg, ids, posts


# ── Channel resolution + fallback ────────────────────────────────────────────────

def test_command_center_name_and_env_alias(monkeypatch):
    monkeypatch.setenv("DISCORD_CH_COMMAND_POST", "legacy-id")
    monkeypatch.setenv("DISCORD_CH_COMMAND_CENTER", "center-id")

    assert dch.channel_id("command_post") == "center-id"
    assert dch.display_channel_name("command_post") == "command-center"
    assert dch.display_channel_name("trade_log") == "trade-log"


def test_command_center_falls_back_to_legacy_env(monkeypatch):
    monkeypatch.delenv("DISCORD_CH_COMMAND_CENTER", raising=False)
    monkeypatch.setenv("DISCORD_CH_COMMAND_POST", "legacy-id")

    assert dch.channel_id("command_post") == "legacy-id"


def test_routes_to_mapped_channel(router):
    _cfg, ids, posts = router
    dch.post("stop_loss", embed={"title": "x"})
    assert posts[-1][:2] == ("bot", ids["alerts"])


def test_research_routes_to_research(router):
    _cfg, ids, posts = router
    dch.post("research", embed={"title": "x"})
    assert posts[-1][:2] == ("bot", ids["research"])


def test_unknown_type_defaults_to_command_post(router):
    _cfg, ids, posts = router
    dch.post("totally_unknown_type", embed={"title": "x"})
    assert posts[-1][:2] == ("bot", ids["command_post"])


def test_disabled_channel_falls_back(router):
    cfg, ids, posts = router
    cfg["disabled_channels"] = ["alerts"]
    dch.post("stop_loss", embed={"title": "x"})
    assert posts[-1][:2] == ("bot", ids["command_post"])  # alerts→command_post fallback


def test_missing_channel_id_follows_fallback_chain(router):
    cfg, ids, posts = router
    ids["research"] = ""               # research id missing → fallback to reports
    dch.post("research", embed={"title": "x"})
    assert posts[-1][:2] == ("bot", ids["reports"])


def test_failed_bot_post_falls_back_to_command_post(router, monkeypatch):
    _cfg, ids, posts = router

    def bot(cid, payload):
        posts.append(("bot", cid, payload))
        return cid != ids["alerts"]      # alerts channel "fails", command_post succeeds

    monkeypatch.setattr(dch, "_post_bot_json", bot)
    dch.post("stop_loss", embed={"title": "x"})
    assert [p[1] for p in posts] == [ids["alerts"], ids["command_post"]]


def test_multichannel_disabled_uses_webhook(router):
    cfg, _ids, posts = router
    cfg["multichannel_enabled"] = False
    dch.post("trade", embed={"title": "x"})
    assert posts[-1][0] == "webhook"


def test_webhook_fallback_is_logged_as_actual_transport(router, monkeypatch):
    _cfg, _ids, posts = router
    events = []

    class CaptureActivity:
        @staticmethod
        def log(event, summary, **details):
            events.append((event, summary, details))

    monkeypatch.setattr(dch, "activity", CaptureActivity())
    monkeypatch.setattr(dch, "_post_bot_json", lambda cid, payload: False)
    dch.post("research", embed={"title": "x"})

    assert posts[-1][0] == "webhook"
    event, summary, details = events[-1]
    assert event == "discord_post"
    assert "#webhook" in summary
    assert details["transport"] == "webhook_fallback"
    assert details["requested_channel"] == "research"


def test_image_falls_back_to_webhook(router, monkeypatch):
    _cfg, _ids, posts = router
    monkeypatch.setattr(dch, "_post_bot_image", lambda *a, **k: False)
    monkeypatch.setattr(
        dch,
        "_post_webhook_image",
        lambda filename, image_bytes, payload: posts.append(
            ("webhook_image", filename, image_bytes, payload)
        ) or True,
    )
    assert dch.post_image("training", "lesson.png", b"png", embed={"title": "x"})
    assert posts[-1][:3] == ("webhook_image", "lesson.png", b"png")


# ── Alert policy: severity, cooldown, dedup, quiet hours ─────────────────────────

def test_severity_for_type(router):
    assert dch.severity_for("kill") == "critical"
    assert dch.severity_for("stop_loss") == "warning"
    assert dch.severity_for("trade") == "info"       # default


def test_info_type_always_posts(router):
    _cfg, _ids, posts = router
    for _ in range(3):
        dch.post("trade", embed={"title": "x"})       # info, cooldown 0
    assert len(posts) == 3


def test_warning_cooldown_suppresses_repeat(router):
    _cfg, _ids, posts = router
    dch.post("alert", embed={"title": "x"}, dedup_key="vpn_down")
    dch.post("alert", embed={"title": "x"}, dedup_key="vpn_down")   # within 15 min → suppressed
    assert len(posts) == 1


def test_warning_distinct_keys_both_post(router):
    _cfg, _ids, posts = router
    dch.post("alert", embed={"title": "x"}, dedup_key="vpn_down")
    dch.post("alert", embed={"title": "x"}, dedup_key="feed_stale")
    assert len(posts) == 2


def test_dedup_suppresses_identical_info_within_window(router):
    _cfg, _ids, posts = router
    dch.post("trade", embed={"title": "x"}, dedup_key="trade:NVDA:1")
    dch.post("trade", embed={"title": "x"}, dedup_key="trade:NVDA:1")  # identical within 60s
    assert len(posts) == 1


def test_quiet_hours_suppress_scoped_type(router):
    cfg, _ids, posts = router
    import datetime as _dt
    h = _dt.datetime.now().hour                       # window that always contains "now"
    cfg["quiet_hours"] = {"enabled": True, "start_hour": h, "end_hour": (h + 1) % 24,
                          "applies_to": ["status"]}
    allowed, reason = dch.should_post("status")
    assert not allowed and reason == "quiet hours"
    # A non-scoped type is unaffected.
    assert dch.should_post("trade")[0] is True


# ── discord_notify typed helpers route to the right msg_type ─────────────────────

@pytest.fixture
def captured_types(monkeypatch):
    seen = []
    monkeypatch.setattr(dch, "post",
                        lambda msg_type, embed=None, content=None, dedup_key=None:
                        seen.append((msg_type, dedup_key)) or True)
    monkeypatch.setattr(dch, "post_file",
                        lambda msg_type, filename, content, embed=None:
                        seen.append((msg_type, "file")) or True)
    monkeypatch.setattr(dn, "dch", dch)
    return seen


def test_notify_helpers_route_to_expected_channels(captured_types):
    dn.heartbeat("cycle", "ok", "n")
    dn.trade_placed({"symbol": "NVDA", "side": "buy", "qty": 1, "limit_price": 1}, {"status": "ok"})
    dn.order_rejected({"symbol": "AMD", "side": "buy", "qty": 1, "limit_price": 1}, "reason")
    dn.stop_loss_alert("TSLA", -3.0, {"avg_entry_price": 1, "current_price": 1, "unrealized_pl": -1})
    dn.kill_activated("manual")
    dn.eod_summary({"equity": 1, "cash": 1}, [], 1.0)
    dn.research_brief("t", "b")
    dn.signals_card("t", "b")
    dn.market_summary("BULL", "s")
    dn.approval_card("cycle", [{"symbol": "NVDA", "side": "buy", "qty": 1, "limit_price": 1}])
    dn.dev_log("boom", "error")
    dn.send_file("r.md", "body", title="Report")
    types = [t for t, _ in captured_types]
    assert types == ["heartbeat", "trade", "order_rejected", "stop_loss", "kill", "status",
                     "research", "signals", "market_summary", "approval_card", "dev_log", "report"]


def test_stop_loss_carries_symbol_dedup_key(captured_types):
    dn.stop_loss_alert("NVDA", -3.0, {"avg_entry_price": 1, "current_price": 1, "unrealized_pl": -1})
    assert captured_types[-1] == ("stop_loss", "stop_loss:NVDA")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
