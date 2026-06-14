"""
Multi-channel Discord router (FeintTrade 10-channel operator layer, ported to Python).

FeintTrade historically posted everything to a single Discord webhook. FeintTrade's
operator UX routes each message TYPE to a dedicated channel (command-post, signals,
trade-log, alerts, status, reports, research, approvals, dev-log, dev-ideas) with
severity-based cooldowns and dedup so the server stays scannable.

This module is the routing + posting core:

  • channel IDs come from .env  (DISCORD_CH_* — non-secret snowflakes)
  • posting uses DISCORD_BOT_TOKEN via the bot REST API (one token, many channels;
    a webhook can only target one channel)
  • routing / policy come from watchlist.json  "discord"  block
  • every post falls back: target channel -> command_post -> DISCORD_WEBHOOK_URL,
    so a missing channel or offline bot never loses the message
  • alert policy (severityForAlertType + cooldown + dedup) is ported from
    FeintTrade src/discord/alertPolicy.ts; state lives in data/discord_alert_state.json

discord_notify.py builds the embeds and calls post()/post_file() with a msg_type;
this module decides the channel and whether policy allows the post.

Set discord.multichannel_enabled=false in watchlist.json to revert to the old
single-webhook behavior with zero code changes.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)

try:
    import activity
except Exception:  # pragma: no cover - logging must never break notifications
    activity = None

BOT_TOKEN   = os.getenv("DISCORD_BOT_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
API_BASE    = "https://discord.com/api/v10"

# Logical channel name -> .env key holding its snowflake ID.
_CHANNEL_ENV = {
    "command_post": "DISCORD_CH_COMMAND_POST",
    "approvals":    "DISCORD_CH_APPROVALS",
    "alerts":       "DISCORD_CH_ALERTS",
    "reports":      "DISCORD_CH_REPORTS",
    "signals":      "DISCORD_CH_SIGNALS",
    "trade_log":    "DISCORD_CH_TRADE_LOG",
    "status":       "DISCORD_CH_STATUS",
    "research":     "DISCORD_CH_RESEARCH",
    "dev_log":      "DISCORD_CH_DEV_LOG",
    "dev_ideas":    "DISCORD_CH_DEV_IDEAS",
    "watchlist":    "DISCORD_CH_WATCHLIST",
    "training":     "DISCORD_CH_TRAINING",
}

# Human-readable purpose per channel (test messages, dashboard panel, !channels).
_PURPOSE = {
    "command_post": "🧭 Primary hub — heartbeats, market/regime summaries, and where you type commands",
    "approvals":    "🗳️ Decision cards — what the agent is about to auto-execute (notify-only FYI/override)",
    "alerts":       "🚨 Risk events — stop-losses, order rejects, kill-switch (cooldown-throttled)",
    "reports":      "📄 Full EOD / after-hours reports (embed + downloadable .md)",
    "signals":      "📡 Trade proposals + marketwide-discovery scan output",
    "trade_log":    "🧾 Audit trail — every placed order and executed decision",
    "status":       "📊 Automated daily session summary (equity · P&L · positions)",
    "research":     "🔬 Morning research brief + watchlist intel",
    "dev_log":      "🛠️ Verbose diagnostics + routine crash traces",
    "dev_ideas":    "💡 Operator-only collaboration space (not bot-routed)",
    "watchlist":    "📋 Live watchlist + auto-update promotions (formerly #mindhub)",
    "training":     "📚 Learn-as-you-go — plain-English lessons + graphic cards explaining each trade decision",
}

_STATE_FILE = ROOT / "data" / "discord_alert_state.json"

# Defaults mirror FeintTrade's alertPolicy: CRITICAL 5 min, WARNING 15 min cooldown.
_DEFAULT_COOLDOWN_MIN = {"critical": 5, "warning": 15, "notice": 30, "info": 0}
_DEFAULT_DEDUP_SECS   = 60

_cfg_cache = {"ts": 0.0, "val": None}


# ── Config ──────────────────────────────────────────────────────────────────────

def _cfg() -> dict:
    """The watchlist.json 'discord' block, cached ~10s. Empty dict if unavailable."""
    now = time.time()
    if _cfg_cache["val"] is not None and now - _cfg_cache["ts"] < 10:
        return _cfg_cache["val"]
    try:
        full = json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))
        val = full.get("discord", {}) or {}
    except Exception:
        val = {}
    _cfg_cache.update(ts=now, val=val)
    return val


def multichannel_enabled() -> bool:
    """True when bot-API routing is active and usable. Falls back to webhook otherwise."""
    return bool(_cfg().get("multichannel_enabled", True)) and bool(BOT_TOKEN)


def channel_id(logical: str) -> str:
    """Snowflake for a logical channel name, or '' if not configured."""
    return os.getenv(_CHANNEL_ENV.get(logical, ""), "").strip()


def channel_for_type(msg_type: str) -> str:
    """Logical channel a message TYPE routes to (watchlist routing map). Default command_post."""
    return _cfg().get("routing", {}).get(msg_type, "command_post")


def _resolve_channel(msg_type: str) -> tuple[str, str]:
    """
    Resolve (logical_name, channel_id) for a msg_type, honoring disabled_channels and
    the channel_fallback chain. Returns ('', '') when nothing usable is configured
    (caller then drops to the webhook).
    """
    cfg = _cfg()
    disabled = set(cfg.get("disabled_channels", []) or [])
    fallback = cfg.get("channel_fallback", {}) or {}
    logical = channel_for_type(msg_type)

    seen: set[str] = set()
    while logical and logical not in seen:
        seen.add(logical)
        if logical not in disabled:
            cid = channel_id(logical)
            if cid:
                return logical, cid
        nxt = fallback.get(logical) or ("command_post" if logical != "command_post" else "")
        logical = nxt
    # Last resort: command_post if it has an id.
    cid = channel_id("command_post")
    return ("command_post", cid) if cid else ("", "")


# ── Alert policy (ported from FeintTrade alertPolicy.ts) ─────────────────────────

def severity_for(msg_type: str) -> str:
    """critical | warning | notice | info — from config severity_by_type, default info."""
    return _cfg().get("severity_by_type", {}).get(msg_type, "info")


def _cooldown_secs(severity: str) -> int:
    table = {**_DEFAULT_COOLDOWN_MIN, **(_cfg().get("cooldown_min", {}) or {})}
    return int(table.get(severity, 0)) * 60


def _read_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"updated_at": None, "alerts": {}}


def _write_state(state: dict) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Prune buckets untouched for > 1 day to keep the file small.
        cutoff = time.time() - 86400
        alerts = state.get("alerts", {})
        state["alerts"] = {k: v for k, v in alerts.items()
                           if (v.get("last_seen_at") or 0) >= cutoff}
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def _bucket(msg_type: str, dedup_key: str | None) -> str:
    return f"{msg_type}|{dedup_key}" if dedup_key else msg_type


def _quiet_now(msg_type: str) -> bool:
    """True if msg_type is in quiet-hours scope and the clock is inside the quiet window."""
    q = _cfg().get("quiet_hours", {}) or {}
    if not q.get("enabled") or msg_type not in set(q.get("applies_to", []) or []):
        return False
    start, end = int(q.get("start_hour", 22)), int(q.get("end_hour", 6))
    h = datetime.now().hour
    return (start <= h or h < end) if start > end else (start <= h < end)


def should_post(msg_type: str, dedup_key: str | None = None) -> tuple[bool, str]:
    """
    Apply severity cooldown + dedup. Returns (allowed, reason). INFO types with no
    dedup_key are always allowed (trades/proposals must never be throttled).
    """
    if _quiet_now(msg_type):
        return False, "quiet hours"
    severity = severity_for(msg_type)
    cooldown = _cooldown_secs(severity)
    dedup_secs = int(_cfg().get("dedup_window_secs", _DEFAULT_DEDUP_SECS)) if dedup_key else 0
    suppress_window = max(cooldown, dedup_secs)
    if suppress_window <= 0:
        return True, "ready"
    bucket = _bucket(msg_type, dedup_key)
    rec = _read_state().get("alerts", {}).get(bucket)
    if rec:
        last = rec.get("last_posted_at") or 0
        if last and (time.time() - last) < suppress_window:
            return False, ("cooldown" if cooldown >= dedup_secs else "duplicate")
    return True, "ready"


def _record(msg_type: str, dedup_key: str | None, severity: str, posted: bool) -> None:
    state = _read_state()
    alerts = state.setdefault("alerts", {})
    bucket = _bucket(msg_type, dedup_key)
    rec = alerts.get(bucket, {})
    now = time.time()
    rec["severity"] = severity
    rec["last_seen_at"] = now
    if posted:
        rec["last_posted_at"] = now
        rec["posted_count"] = rec.get("posted_count", 0) + 1
    else:
        rec["suppressed_count"] = rec.get("suppressed_count", 0) + 1
    alerts[bucket] = rec
    _write_state(state)


# ── Transport ───────────────────────────────────────────────────────────────────

def _bot_headers() -> dict:
    return {"Authorization": f"Bot {BOT_TOKEN}"}


def _post_bot_json(cid: str, payload: dict) -> bool:
    """POST an embed/content message to a channel via the bot API. One 429 retry."""
    if not (BOT_TOKEN and cid):
        return False
    url = f"{API_BASE}/channels/{cid}/messages"
    for attempt in range(2):
        try:
            r = requests.post(url, headers=_bot_headers(), json=payload, timeout=10)
            if r.status_code == 429 and attempt == 0:
                time.sleep(min(float(r.headers.get("Retry-After", 1)) or 1, 5))
                continue
            r.raise_for_status()
            return True
        except Exception as e:
            if attempt == 0 and "429" in str(e):
                continue
            print(f"Discord bot post failed (channel {cid}): {e}")
            return False
    return False


def _post_bot_file(cid: str, filename: str, content: str, payload: dict) -> bool:
    if not (BOT_TOKEN and cid):
        return False
    url = f"{API_BASE}/channels/{cid}/messages"
    try:
        files = {"files[0]": (filename, content.encode("utf-8", errors="replace"), "text/markdown")}
        r = requests.post(url, headers=_bot_headers(),
                          data={"payload_json": json.dumps(payload)}, files=files, timeout=20)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"Discord bot file post failed (channel {cid}): {e}")
        return False


def _post_bot_image(cid: str, filename: str, image_bytes: bytes, payload: dict) -> bool:
    if not (BOT_TOKEN and cid):
        return False
    url = f"{API_BASE}/channels/{cid}/messages"
    try:
        files = {"files[0]": (filename, image_bytes, "image/png")}
        r = requests.post(url, headers=_bot_headers(),
                          data={"payload_json": json.dumps(payload)}, files=files, timeout=20)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"Discord bot image post failed (channel {cid}): {e}")
        return False


def _post_webhook_json(payload: dict) -> bool:
    if not WEBHOOK_URL:
        return False
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"Discord webhook post failed: {e}")
        return False


def _post_webhook_file(filename: str, content: str, payload: dict) -> bool:
    if not WEBHOOK_URL:
        return False
    try:
        files = {"file": (filename, content.encode("utf-8", errors="replace"), "text/markdown")}
        r = requests.post(WEBHOOK_URL, data={"payload_json": json.dumps(payload)},
                          files=files, timeout=20)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"Discord webhook file post failed: {e}")
        return False


# ── Public API ──────────────────────────────────────────────────────────────────

def post(msg_type: str, embed: dict | None = None, content: str | None = None,
         dedup_key: str | None = None) -> bool:
    """
    Route an embed/content message to the channel mapped from msg_type, applying
    alert policy and the channel->command_post->webhook fallback. Returns True if
    delivered (or intentionally suppressed by policy — caller need not care).
    """
    payload: dict = {}
    if embed is not None:
        payload["embeds"] = [embed]
    if content:
        payload["content"] = content[:1900]
    if not payload:
        return False

    allowed, _reason = should_post(msg_type, dedup_key)
    if not allowed:
        _record(msg_type, dedup_key, severity_for(msg_type), posted=False)
        return True  # deliberately suppressed — not an error

    delivered = False
    logical = None
    if multichannel_enabled():
        logical, cid = _resolve_channel(msg_type)
        if cid:
            delivered = _post_bot_json(cid, payload)
        if not delivered and logical != "command_post":
            cp = channel_id("command_post")
            if cp:
                delivered = _post_bot_json(cp, payload)
    if not delivered:
        delivered = _post_webhook_json(payload)

    _record(msg_type, dedup_key, severity_for(msg_type), posted=delivered)
    if activity:
        activity.log("discord_post", f"{msg_type} -> #{(logical or 'webhook').replace('_', '-')}",
                     delivered=delivered,
                     title=((embed or {}).get("title") if embed else (content or "")[:80]))
    return delivered


def post_file(msg_type: str, filename: str, content: str, embed: dict | None = None) -> bool:
    """Route a file attachment (e.g. a full report) with the same fallback chain."""
    payload = {"embeds": [embed]} if embed else {}
    delivered = False
    logical = None
    if multichannel_enabled():
        logical, cid = _resolve_channel(msg_type)
        if cid:
            delivered = _post_bot_file(cid, filename, content, payload)
        if not delivered and logical != "command_post":
            cp = channel_id("command_post")
            if cp:
                delivered = _post_bot_file(cp, filename, content, payload)
    if not delivered:
        delivered = _post_webhook_file(filename, content, payload)
    if activity:
        activity.log("discord_file", f"{msg_type} file -> #{(logical or 'webhook').replace('_', '-')}",
                     delivered=delivered, filename=filename)
    return delivered


def post_image(msg_type: str, filename: str, image_bytes: bytes, embed: dict | None = None,
               dedup_key: str | None = None) -> bool:
    """Route an image attachment (e.g. a teaching card) with embed + alert policy + the
    channel->command_post fallback. Honors cooldown/dedup so cards don't spam."""
    allowed, _reason = should_post(msg_type, dedup_key)
    if not allowed:
        _record(msg_type, dedup_key, severity_for(msg_type), posted=False)
        return True
    payload = {"embeds": [embed]} if embed else {}
    delivered = False
    logical = None
    if multichannel_enabled():
        logical, cid = _resolve_channel(msg_type)
        if cid:
            delivered = _post_bot_image(cid, filename, image_bytes, payload)
        if not delivered and logical != "command_post":
            cp = channel_id("command_post")
            if cp:
                delivered = _post_bot_image(cp, filename, image_bytes, payload)
    _record(msg_type, dedup_key, severity_for(msg_type), posted=delivered)
    if activity:
        activity.log("discord_image", f"{msg_type} image -> #{(logical or 'webhook').replace('_', '-')}",
                     delivered=delivered, filename=filename)
    return delivered


def health() -> dict:
    """Config snapshot for diagnostics / the dashboard."""
    return {
        "multichannel_enabled": multichannel_enabled(),
        "bot_token_present": bool(BOT_TOKEN),
        "webhook_present": bool(WEBHOOK_URL),
        "channels": {name: bool(channel_id(name)) for name in _CHANNEL_ENV},
    }


def broadcast_test(note: str = "") -> dict:
    """
    Post a test embed to EVERY configured channel (bypasses routing + alert policy so
    each one is exercised directly) and return per-channel delivery status. Powers the
    dashboard Test button and the Discord !test command.
    """
    results: dict = {}
    ts = datetime.now(timezone.utc).isoformat()
    for logical in _CHANNEL_ENV:
        cid = channel_id(logical)
        if not cid:
            results[logical] = {"configured": False, "ok": False, "detail": "no channel id"}
            continue
        embed = {
            "title": f"📌 #{logical.replace('_', '-')}",
            "description": (f"{_PURPOSE.get(logical, '—')}\n\n"
                            f"✅ *Channel wired & reachable — FeintTrade is posting here.*"
                            + (f"\n\n{note}" if note else "")),
            "color": 0x3498db,
            "timestamp": ts,
        }
        ok = _post_bot_json(cid, {"embeds": [embed]})
        results[logical] = {"configured": True, "ok": ok, "channel_id": cid}
    if activity:
        ok_n = sum(1 for r in results.values() if r.get("ok"))
        activity.log("broadcast_test", f"posted purpose cards to {ok_n}/{len(results)} channels",
                     note=note or None)
    return results


def health_check() -> dict:
    """
    Full health snapshot for diagnostics / dashboard / !channels: config presence,
    per-channel GET reachability (no posting), and recent alert-state counters.
    """
    info = health()
    reach: dict = {}
    if BOT_TOKEN:
        for logical in _CHANNEL_ENV:
            cid = channel_id(logical)
            if not cid:
                reach[logical] = "unconfigured"
                continue
            try:
                r = requests.get(f"{API_BASE}/channels/{cid}", headers=_bot_headers(), timeout=8)
                reach[logical] = "ok" if r.ok else f"http {r.status_code}"
            except Exception:
                reach[logical] = "error"
    info["reachability"] = reach
    info["alert_state"] = _read_state().get("alerts", {})
    return info


if __name__ == "__main__":
    import sys
    if "--broadcast" in sys.argv:
        print(json.dumps(broadcast_test("Manual broadcast test from CLI."), indent=2))
    elif "--health" in sys.argv:
        print(json.dumps(health_check(), indent=2))
    else:
        print(json.dumps(health(), indent=2))
