"""
Typed Discord notifications for the trading agent.

Builds the embeds (colors, fields, portfolio-P&L context) and routes each one to
its dedicated channel via discord_channels (the FeintTrade 10-channel operator
layer). When multichannel is disabled or the bot token is absent, discord_channels
falls back to the single DISCORD_WEBHOOK_URL, preserving the original behavior.
"""

import json
import os
import time
import requests
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env", override=True)

try:
    import discord_channels as dch
except Exception:  # pragma: no cover - notifications must never crash a trading cycle
    dch = None

GREEN  = 0x2ecc71
RED    = 0xe74c3c
ORANGE = 0xe67e22
BLUE   = 0x3498db
GREY   = 0x95a5a6

# ── Portfolio P&L context (shown on every trade decision) ─────────────────────
_ALPACA_KEY    = os.getenv("APCA_API_KEY_ID")
_ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY")
_BASE_URL      = os.getenv("APCA_BASE_URL", "https://paper-api.alpaca.markets")
_STARTING_CAPITAL = 100_000.0
_pnl_cache = {"ts": 0.0, "val": None}


def _portfolio_pnl():
    """(equity, day_pct, all_time_pct) from Alpaca, cached ~30s. None on failure."""
    now = time.time()
    if _pnl_cache["val"] and now - _pnl_cache["ts"] < 30:
        return _pnl_cache["val"]
    try:
        r = requests.get(
            f"{_BASE_URL}/v2/account",
            headers={"APCA-API-KEY-ID": _ALPACA_KEY, "APCA-API-SECRET-KEY": _ALPACA_SECRET},
            timeout=8,
        )
        r.raise_for_status()
        a = r.json()
        equity  = float(a.get("equity", 0) or 0)
        last_eq = float(a.get("last_equity", equity) or equity)
        day_pct = (equity - last_eq) / last_eq * 100 if last_eq else 0.0
        total   = (equity - _STARTING_CAPITAL) / _STARTING_CAPITAL * 100
        val = (equity, day_pct, total)
        _pnl_cache.update(ts=now, val=val)
        return val
    except Exception:
        return _pnl_cache["val"]


_acct_cache = {"ts": 0.0, "val": None}
_pos_cache  = {"ts": 0.0, "val": None}
_HEADERS_RO = {"APCA-API-KEY-ID": _ALPACA_KEY, "APCA-API-SECRET-KEY": _ALPACA_SECRET}


def _fetch_account():
    """Full Alpaca account dict, cached ~20s. {} on failure."""
    now = time.time()
    if _acct_cache["val"] is not None and now - _acct_cache["ts"] < 20:
        return _acct_cache["val"]
    try:
        r = requests.get(f"{_BASE_URL}/v2/account", headers=_HEADERS_RO, timeout=8)
        r.raise_for_status()
        _acct_cache.update(ts=now, val=r.json())
        return _acct_cache["val"]
    except Exception:
        return _acct_cache["val"] or {}


def _fetch_positions():
    """Live Alpaca positions list, cached ~20s. [] on failure."""
    now = time.time()
    if _pos_cache["val"] is not None and now - _pos_cache["ts"] < 20:
        return _pos_cache["val"]
    try:
        r = requests.get(f"{_BASE_URL}/v2/positions", headers=_HEADERS_RO, timeout=8)
        r.raise_for_status()
        _pos_cache.update(ts=now, val=r.json())
        return _pos_cache["val"]
    except Exception:
        return _pos_cache["val"] or []


def _pnl_field():
    """Discord field with day + all-time portfolio %, or None if unavailable."""
    p = _portfolio_pnl()
    if not p:
        return None
    equity, day_pct, total = p
    return {
        "name": "📊 Portfolio",
        "value": f"${equity:,.0f}  ·  Day {day_pct:+.2f}%  ·  All-time {total:+.2f}%",
        "inline": False,
    }


def _with_pnl(fields):
    """Append the portfolio-P&L field to an embed's field list."""
    fields = list(fields or [])
    pf = _pnl_field()
    if pf:
        fields.append(pf)
    return fields


def _build_embed(title, description, color=GREY, fields=None, footer=None):
    embed = {
        "title": title,
        "description": description[:4000] if description else "",
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if fields:
        embed["fields"] = fields
    if footer:
        embed["footer"] = {"text": str(footer)[:2048]}
    return embed


def send(title, description, color=GREY, fields=None, msg_type="info", dedup_key=None, footer=None):
    """
    Build an embed and route it to the channel mapped from msg_type (see the
    watchlist.json 'discord.routing' map). Falls back to command_post then the
    webhook. msg_type defaults to a general post when a caller doesn't specify one.
    """
    embed = _build_embed(title, description, color, fields, footer)
    if dch:
        dch.post(msg_type, embed=embed, dedup_key=dedup_key)


def send_file(filename, content, title=None, description="", color=BLUE, msg_type="report"):
    """
    Post an embed plus an attached text file (the full detailed report, ready to
    download and paste into an external AI). Routes to the reports channel by
    default; delivers the summary embed alone if the file upload can't go through.
    """
    embed = _build_embed(title or filename, (description or "")[:4000], color)
    if not dch:
        return
    if not dch.post_file(msg_type, filename, content, embed=embed):
        dch.post(msg_type, embed=embed)


# ---------------------------------------------------------------------------
# Typed notification helpers
# ---------------------------------------------------------------------------

def heartbeat(routine, status="ok", notes=""):
    color = GREEN if status == "ok" else RED
    icon  = "✅" if status == "ok" else "❌"
    send(
        msg_type="heartbeat",
        title=f"{icon} Heartbeat — {routine}",
        description=(notes or f"{routine} completed at {datetime.now().strftime('%H:%M MT')}")
                    + ("" if status == "ok" else "\n⚠️ Status not OK — check #ft-dev-log."),
        color=color,
        fields=_with_pnl(None),   # show live equity + day/all-time P&L at a glance
    )


def trade_placed(order, result):
    side  = str(order.get("side", "")).upper()
    color = GREEN if side == "BUY" else RED
    icon  = "🟢" if side == "BUY" else "🔴"
    sym   = order.get("symbol", "?")
    px    = order.get("limit_price")
    setup = order.get("setup_type") or "—"
    conv  = order.get("conviction", order.get("score"))
    sig   = (order.get("signals") or {}).get("signal_count")

    fields = [
        {"name": "Setup",  "value": str(setup),                         "inline": True},
        {"name": "Status", "value": result.get("status", "submitted"),  "inline": True},
    ]
    quality = []
    if conv not in (None, ""):
        quality.append(f"conviction {conv}/10")
    if sig not in (None, ""):
        quality.append(f"{sig} signals")
    if quality:
        fields.insert(1, {"name": "Quality", "value": " · ".join(quality), "inline": True})

    # Risk plan (stop / target / R:R) when the model supplied it — the most important
    # numbers on a trade post: where we're wrong and what we're playing for.
    stop   = order.get("stop") or order.get("stop_price")
    target = order.get("target") or order.get("target_price")
    if stop or target:
        rp = []
        if stop:
            rp.append(f"🛑 stop {_fmt_price(stop)}")
        if target:
            rp.append(f"🎯 target {_fmt_price(target)}")
        try:
            if stop and target and px:
                rr = abs(float(target) - float(px)) / abs(float(px) - float(stop))
                rp.append(f"R:R {rr:.1f}:1")
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        fields.append({"name": "Risk plan", "value": "  ·  ".join(rp), "inline": False})

    send(
        msg_type="trade",
        title=f"{icon} {side} {order.get('qty')} {sym} @ {_fmt_price(px)}"[:240],
        description=(order.get("reasoning") or "")[:700],
        color=color,
        fields=_with_pnl(fields),
    )


def order_rejected(order, reason):
    send(
        msg_type="order_rejected",
        title=f"⛔ Order Rejected — {order.get('symbol')}",
        description=f"**Reason:** {reason}\n**Order:** {order.get('side','').upper()} {order.get('qty')} @ ${order.get('limit_price')}",
        color=ORANGE,
        fields=_with_pnl(None),
    )


def _fmt_price(value):
    try:
        return f"${float(value):,.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "—"


def decision_proposal(routine, payload, regime_label="", title_prefix="", cycle_id=""):
    """
    Post the agent's PROPOSED decision for a cycle (what it intends to do),
    independent of execution. This is the 'trade proposal' alert — it fires even
    when the agent proposes a close/hold so the operator sees its intent in
    real time, not only confirmed fills. Execution outcomes still arrive via
    trade_placed / order_rejected afterward.
    """
    payload = payload or {}
    orders = [o for o in payload.get("orders", []) if isinstance(o, dict)]
    closes = [c for c in payload.get("closes", []) if isinstance(c, dict)]
    candidates = [c for c in payload.get("candidates", []) if isinstance(c, dict)]
    summary = (payload.get("summary") or "").strip()

    def _conv(o):
        for k in ("conviction", "score"):
            if o.get(k) not in (None, ""):
                return f" · conv {o.get(k)}"
        return ""

    def _rr_extra(o, px):
        stop = o.get("stop") or o.get("stop_price")
        target = o.get("target") or o.get("target_price")
        if not stop or not px:
            return ""
        parts = [f"stop {_fmt_price(stop)}"]
        if target:
            parts.append(f"tgt {_fmt_price(target)}")
            try:
                rr = abs(float(target) - float(px)) / abs(float(px) - float(stop))
                if rr:
                    parts.append(f"R:R {rr:.1f}")
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        return "  ·  " + " · ".join(parts)

    order_lines = []
    for o in orders[:8]:
        side = str(o.get("side", "buy")).upper()
        px = o.get("limit_price", o.get("reference_price"))
        order_lines.append(
            f"{'🟢' if side == 'BUY' else '🔴'} {side} {o.get('qty', '?')} "
            f"{o.get('symbol', '?')} @ {_fmt_price(px)}{_conv(o)}{_rr_extra(o, px)}"
        )
    close_lines = [
        f"⏹️ CLOSE {c.get('symbol', '?')}"
        + (f" — {c.get('reasoning', '')[:60]}" if c.get("reasoning") else "")
        for c in closes[:8]
    ]

    # Candidates the agent is leaning toward acting on (buy/add/trim/close).
    actionable = [
        c for c in candidates
        if str(c.get("action", "")).upper() in {"BUY", "ADD", "TRIM", "CLOSE"}
    ]
    watch_lines = []
    for c in actionable[:6]:
        sc = c.get("score", c.get("conviction"))
        sc_txt = f" ({sc})" if sc not in (None, "") else ""
        watch_lines.append(f"{str(c.get('action','')).upper()} {c.get('symbol','?')}{sc_txt}")

    fields = []
    if order_lines:
        fields.append({"name": f"Proposed Orders ({len(orders)})",
                       "value": "\n".join(order_lines)[:1024], "inline": False})
    if close_lines:
        fields.append({"name": f"Proposed Closes ({len(closes)})",
                       "value": "\n".join(close_lines)[:1024], "inline": False})
    if watch_lines:
        fields.append({"name": "Leaning toward",
                       "value": ", ".join(watch_lines)[:1024], "inline": False})

    color = GREEN if orders else (RED if closes else GREY)
    head = f"{title_prefix}📋 Trade Proposal — {routine.upper()}"
    if regime_label:
        head += f" · {regime_label}"

    # At-a-glance TL;DR stance so the post is scannable in a second (was a wall of text).
    if orders:
        syms = ", ".join(f"{str(o.get('side', 'buy')).upper()} {o.get('symbol', '?')}" for o in orders[:4])
        tldr = f"🟢 **{len(orders)} new trade{'s' if len(orders) != 1 else ''}** — {syms}"
    elif closes:
        tldr = (f"🔴 **{len(closes)} exit{'s' if len(closes) != 1 else ''}** — "
                + ", ".join(c.get('symbol', '?') for c in closes[:4]))
    elif watch_lines:
        tldr = f"🟡 **Watching {len(actionable)} setup{'s' if len(actionable) != 1 else ''}** — none confirmed, no entries"
    else:
        tldr = "⚪ **No new trades** — holding positions, waiting for a confirmed setup"

    body = (summary or "No actionable trades proposed this cycle.").strip()
    send(
        msg_type="proposal",
        title=head[:240],
        description=f"{tldr}\n\n{body}"[:1800],
        color=color,
        fields=_with_pnl(fields),
        footer=f"🔗 cycle {cycle_id}" if cycle_id else None,
    )


def decision_executed(routine, payload, orders_placed=None, closes_placed=None,
                      execution_events=None, regime_label="", title_prefix="", cycle_id=""):
    """
    Post the FINAL decision outcome for a cycle after execution has been attempted.
    This complements the proposal alert with what actually happened: orders placed,
    closes executed, and any rejects/skips encountered during execution.
    """
    payload = payload or {}
    orders_placed = [o for o in (orders_placed or []) if isinstance(o, dict)]
    closes_placed = [str(c) for c in (closes_placed or []) if str(c).strip()]
    execution_events = [e for e in (execution_events or []) if isinstance(e, dict)]
    summary = (payload.get("summary") or "").strip()

    order_lines = []
    for o in orders_placed[:8]:
        side = str(o.get("side", "buy")).upper()
        px = o.get("limit_price", o.get("reference_price"))
        order_lines.append(
            f"{'🟢' if side == 'BUY' else '🔴'} {side} {o.get('qty', '?')} "
            f"{o.get('symbol', '?')} @ {_fmt_price(px)}"
        )

    status_counts = {}
    for event in execution_events:
        status = str(event.get("status", "") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    fields = []
    if order_lines:
        fields.append({
            "name": f"Orders Executed ({len(orders_placed)})",
            "value": "\n".join(order_lines)[:1024],
            "inline": False,
        })
    if closes_placed:
        fields.append({
            "name": f"Closes / Exits ({len(closes_placed)})",
            "value": "\n".join(closes_placed)[:1024],
            "inline": False,
        })
    if status_counts:
        parts = []
        for key in ("placed", "already_closed", "rejected", "broker_error",
                    "not_tradeable", "skipped", "clamped", "halted"):
            count = status_counts.get(key)
            if count:
                parts.append(f"{count} {key.replace('_', ' ')}")
        if parts:
            fields.append({
                "name": "Execution Result",
                "value": " | ".join(parts)[:1024],
                "inline": False,
            })

    acted = bool(orders_placed or closes_placed or status_counts.get("placed") or status_counts.get("already_closed"))
    color = GREEN if acted else GREY
    head = f"{title_prefix}⚡ Trade Decision — {routine.upper()}"
    if regime_label:
        head += f" · {regime_label}"

    description = summary
    if not description:
        if acted:
            description = "Execution completed."
        elif execution_events:
            description = "Decision reviewed, but nothing actionable was executed."
        else:
            description = "No actionable trades were executed this cycle."

    send(
        msg_type="decision",
        title=head[:240],
        description=description[:1500],
        color=color,
        fields=_with_pnl(fields),
        footer=f"🔗 cycle {cycle_id}" if cycle_id else None,
    )


def _exit_fields(position):
    qty = abs(float(position.get("qty", 0) or 0))
    return [
        {"name": "Entry",   "value": f"${float(position.get('avg_entry_price', 0) or 0):,.2f}", "inline": True},
        {"name": "Current", "value": f"${float(position.get('current_price', 0) or 0):,.2f}",   "inline": True},
        {"name": "Qty",     "value": f"{qty:g}",                                                 "inline": True},
        {"name": "Realized P&L", "value": f"${float(position.get('unrealized_pl', 0) or 0):+,.2f}", "inline": True},
    ]


def stop_loss_alert(symbol, pnl_pct, position):
    pl = float(position.get("unrealized_pl", 0) or 0)
    send(
        msg_type="stop_loss",
        dedup_key=f"stop_loss:{symbol}",
        title=f"🛑 Stop-Loss — {symbol} {pnl_pct:+.1f}% (${pl:+,.0f})",
        description=(f"**{symbol}** hit its stop at **{pnl_pct:+.1f}%** (**${pl:+,.2f}**) — closing the position. "
                     "Cutting losers fast is how the account survives; the first loss is the cheapest one."),
        color=RED,
        fields=_with_pnl(_exit_fields(position)),
    )


def take_profit_alert(symbol, pnl_pct, position):
    pl = float(position.get("unrealized_pl", 0) or 0)
    send(
        msg_type="take_profit",
        dedup_key=f"take_profit:{symbol}",
        title=f"✅ Take-Profit — {symbol} {pnl_pct:+.1f}% (${pl:+,.0f})",
        description=(f"**{symbol}** up **{pnl_pct:+.1f}%** (**${pl:+,.2f}**) — banking the gain / trimming. "
                     "You never go broke taking profits; the runner stays on a trailed stop."),
        color=GREEN,
        fields=_with_pnl(_exit_fields(position)),
    )


def kill_activated(source="manual"):
    send(
        msg_type="kill",
        title="🛑 KILL SWITCH ACTIVATED",
        description=f"All trading halted. Source: **{source}**\nAll open orders cancelled.\nSend `!resume` to re-enable trading.",
        color=RED,
    )


def kill_deactivated():
    send(
        msg_type="kill",
        title="✅ Kill Switch Cleared",
        description="Trading resumed. Agent will trade normally on next cycle.",
        color=GREEN,
    )


def eod_summary(account, positions, day_pnl):
    equity = float(account.get("equity", 0) or 0)
    cash   = float(account.get("cash", 0) or 0)
    invested = equity - cash
    last_eq = equity - day_pnl
    day_pct = (day_pnl / last_eq * 100) if last_eq else 0.0
    positions = positions if isinstance(positions, list) else []
    n = len(positions)
    color  = GREEN if day_pnl >= 0 else RED
    icon   = "📈" if day_pnl >= 0 else "📉"

    def _plpc(p):
        try:
            return float(p.get("unrealized_plpc", 0) or 0) * 100
        except (TypeError, ValueError):
            return 0.0

    greens = [p for p in positions if _plpc(p) >= 0]
    reds   = [p for p in positions if _plpc(p) < 0]

    fields = [
        {"name": "Portfolio", "value": f"${equity:,.2f}", "inline": True},
        {"name": "Cash",      "value": (f"${cash:,.2f} ({cash/equity*100:.0f}%)" if equity else f"${cash:,.2f}"),
         "inline": True},
        {"name": "Invested",  "value": f"${invested:,.2f}", "inline": True},
    ]
    if positions:
        fields.append({"name": f"Open Positions ({n})",
                       "value": f"🟢 {len(greens)} up · 🔴 {len(reds)} down", "inline": False})
        best  = max(positions, key=_plpc)
        worst = min(positions, key=_plpc)
        fields.append({"name": "Best / Worst (open)",
                       "value": (f"🟢 {best.get('symbol', '?')} {_plpc(best):+.1f}%"
                                 f"   ·   🔴 {worst.get('symbol', '?')} {_plpc(worst):+.1f}%"),
                       "inline": False})
    else:
        fields.append({"name": "Open Positions",
                       "value": "None — sitting in cash (a valid, disciplined decision).", "inline": False})

    send(
        msg_type="status",
        title=f"{icon} End of Day — {'+' if day_pnl >= 0 else '−'}${abs(day_pnl):,.2f} ({day_pct:+.2f}%)",
        description=(f"Closed the session **{'up' if day_pnl >= 0 else 'down'} ${abs(day_pnl):,.2f}** "
                     f"({day_pct:+.2f}%). Full report in #ft-reports."),
        color=color,
        fields=fields,
    )


def alert(message, color=ORANGE):
    send(title="⚠️ Agent Alert", description=message, color=color, msg_type="alert")


def _status_updates_enabled() -> bool:
    """Config gate: discord.command_post_status_updates (default True). Lets the operator
    silence the per-cycle status feed without a code change."""
    try:
        cfg = json.loads((_ROOT / "watchlist.json").read_text(encoding="utf-8"))
        return bool((cfg.get("discord") or {}).get("command_post_status_updates", True))
    except Exception:
        return True


def status_update(routine, account=None, positions=None, note=""):
    """
    Post the `!status` snapshot — portfolio equity, day P&L, cash, open positions, and
    market/kill state — to #ft-command-post after a routine/cycle/trade so the channel is a
    live pulse of the book. Pass account/positions if the caller already has them to skip an
    Alpaca round-trip; otherwise they're fetched (cached ~20s). Gated by
    discord.command_post_status_updates.
    """
    if not _status_updates_enabled():
        return
    account = account if isinstance(account, dict) and account else _fetch_account()
    positions = positions if isinstance(positions, list) else _fetch_positions()

    equity  = float((account or {}).get("equity", 0) or 0)
    cash    = float((account or {}).get("cash", 0) or 0)
    last_eq = float((account or {}).get("last_equity", equity) or equity)
    day_pnl = equity - last_eq
    day_pct = (day_pnl / last_eq * 100) if last_eq else 0.0
    n_pos   = len(positions) if isinstance(positions, list) else 0

    killed = (_ROOT / "kill.flag").exists()
    try:
        from common import is_crypto as _is_crypto, market_phase, normalize_symbol as _norm
        phase = market_phase()
    except Exception:
        phase = ""
        _norm = None
        _is_crypto = lambda symbol, asset_class=None: "/" in str(symbol)
    # market_phase() is time-based and HOLIDAY-BLIND (it called Juneteenth "REGULAR"). Use
    # the broker clock to correct the label during would-be regular hours. eq_open is None
    # when the clock can't be reached (fail-open to the time-based label).
    eq_open = None
    try:
        import trade as _trade
        eq_open = _trade.equities_open_now()
    except Exception:
        eq_open = None
    if killed:
        state = "🛑 KILL SWITCH ACTIVE — trading halted"
    elif phase == "REGULAR":
        state = "🟢 Market Open" if eq_open is not False else "🔴 Market Closed (holiday) · crypto 24/7"
    elif phase in ("PRE_MARKET", "AFTER_HOURS"):
        state = f"🟡 {phase.replace('_', ' ').title()} · crypto 24/7"
    else:
        state = "🔴 Market Closed · crypto 24/7"

    scope_note = ""
    if routine == "crypto":
        crypto_count = sum(
            1 for p in positions
            if _is_crypto(p.get("symbol", ""), p.get("asset_class"))
        )
        scope_note = f"Crypto holdings: {crypto_count} · full account snapshot shown"

    invested = equity - cash
    cash_pct = (cash / equity * 100) if equity else 0.0
    color = RED if killed else (GREEN if day_pnl >= 0 else ORANGE)
    fields = [
        {"name": "💰 Portfolio",      "value": f"${equity:,.2f}",                       "inline": True},
        {"name": "📊 Day P&L",        "value": f"${day_pnl:+,.2f} ({day_pct:+.2f}%)",   "inline": True},
        {"name": "💵 Cash",           "value": f"${cash:,.2f} ({cash_pct:.0f}%)",       "inline": True},
    ]

    # 🛒 Holdings — list the ACTUAL positions, not just a count, so each per-cycle card
    # reflects what was bought/sold (the count alone made buys/sells invisible).
    if positions:
        held_lines = []
        for p in positions[:8]:
            sym  = _norm(p.get("symbol", ""), p.get("asset_class")) if _norm else p.get("symbol", "?")
            qty  = float(p.get("qty", 0) or 0)
            curr = float(p.get("current_price", 0) or 0)
            ppc  = float(p.get("unrealized_plpc", 0) or 0) * 100
            icon = "🟢" if ppc >= 0 else "🔴"
            held_lines.append(f"{icon} {sym} {qty:g} @ ${curr:,.2f} ({ppc:+.1f}%)")
        held_value = "\n".join(held_lines)
        if n_pos > 8:
            held_value += f"\n… +{n_pos - 8} more"
    else:
        held_value = "none — sitting in cash"
    fields.append({"name": f"🛒 Holdings ({n_pos}) · invested ${invested:,.0f}",
                   "value": held_value[:1024], "inline": False})

    send(
        msg_type="status_update",
        title=f"📟 Portfolio Status · after {routine}",
        description=(state + (f"\n{scope_note}" if scope_note else "")
                     + (f"\n{note}" if note else "")),
        color=color,
        fields=fields,
    )


# ---------------------------------------------------------------------------
# FeintTrade operator emissions — feed the remaining dedicated channels
# ---------------------------------------------------------------------------

def research_brief(title, body):
    """Research / intel brief → #ft-research."""
    send(
        title=f"🔬 {title}"[:240],
        description=(body or "")[:3500],
        color=BLUE,
        msg_type="research",
    )


def market_summary(regime_label, summary, fields=None):
    """Concise market/regime summary → #ft-command-post (scannable in 5 seconds)."""
    send(
        title=f"🧭 Market Summary — {regime_label}"[:240],
        description=(summary or "")[:2000],
        color=BLUE,
        fields=fields,
        msg_type="market_summary",
    )


def signals_card(title, body):
    """Pre-decision scan / marketwide-discovery signals → #ft-signals."""
    send(
        title=f"📡 {title}"[:240],
        description=(body or "")[:3500],
        color=BLUE,
        msg_type="signals",
    )


def approval_card(routine, orders, regime_label=""):
    """
    Notify-only card of the actionable decisions the agent is auto-executing →
    #ft-approvals. FeintTrade stays fully autonomous, so this is an at-a-glance
    operator feed / override surface, not an execution gate.
    """
    orders = [o for o in (orders or []) if isinstance(o, dict)]
    lines = []
    for o in orders[:10]:
        side = str(o.get("side", "buy")).upper()
        lines.append(
            f"{'🟢' if side == 'BUY' else '🔴'} {side} {o.get('qty', '?')} "
            f"{o.get('symbol', '?')} @ {_fmt_price(o.get('limit_price', o.get('reference_price')))}"
        )
    if not lines:
        return
    head = f"🗳️ Decision Card — {routine.upper()}"
    if regime_label:
        head += f" · {regime_label}"
    send(
        title=head[:240],
        description=("Auto-executing (notify-only). React 👎 to flag for manual review.\n\n"
                     + "\n".join(lines))[:1500],
        color=GREEN,
        fields=_with_pnl(None),
        msg_type="approval_card",
    )


def dev_log(message, level="debug"):
    """Verbose debug / diagnostic output → #ft-dev-log (deduped to curb spam)."""
    color = RED if level == "error" else GREY
    send(
        title=f"🧰 dev-log · {level}",
        description=str(message)[:3500],
        color=color,
        msg_type="dev_log",
        dedup_key=f"dev:{level}:{str(message)[:80]}",
    )


def watchlist_update(promoted, demoted, active):
    """Auto-watchlist promotions/demotions → #ft-watchlist."""
    promoted = list(promoted or [])
    demoted = list(demoted or [])
    parts = []
    if promoted:
        parts.append(f"🟢 **Promoted** ({len(promoted)}): " + ", ".join(promoted))
    if demoted:
        parts.append(f"🔻 **Demoted** ({len(demoted)}): " + ", ".join(demoted))
    parts.append(f"📋 **Active auto-watchlist** ({len(active or [])}): "
                 + (", ".join(active) if active else "— core watchlist only"))
    send(
        title="📋 Watchlist Updated",
        description=("Names that keep showing up in marketwide discovery with quality get "
                     "auto-added so research isn't limited to the static list.\n\n" + "\n".join(parts))[:2000],
        color=GREEN if promoted else (ORANGE if demoted else BLUE),
        msg_type="watchlist",
    )
