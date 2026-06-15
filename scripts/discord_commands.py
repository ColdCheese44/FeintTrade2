"""
Discord bot command handler — called by your bot as a subprocess.

Your bot receives a Discord message, strips the command, and runs:
    python scripts/discord_commands.py !kill
    python scripts/discord_commands.py !status
    ...and sends the printed output back to Discord.

Commands:
    !status     portfolio equity, cash, day P&L, kill state
    !positions  open positions with live P&L
    !orders     last 10 orders
    !kill       emergency stop — halt trading, cancel all open orders
    !resume     clear kill switch, resume trading
    !cancel     cancel open orders once (does not halt future cycles)
    !journal    today's journal summary
    !help       list all commands
"""

import sys
import json
import subprocess
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

# Force UTF-8 output so emoji don't crash on Windows cp1252 terminals
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)

KILL_FLAG = ROOT / "kill.flag"

sys.path.insert(0, str(ROOT / "scripts"))
try:
    from common import normalize_symbol, is_crypto, load_risk, normalize_positions
except Exception:
    def normalize_symbol(s, a=None): return s
    def is_crypto(s, a=None): return "/" in str(s)
    def load_risk(): return {}
    def normalize_positions(p): return p


def run(script, *args):
    cmd = ["python", str(ROOT / "scripts" / script)] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    if result.returncode != 0:
        return {"error": result.stderr.strip()}
    try:
        return json.loads(result.stdout)
    except Exception:
        return {"raw": result.stdout.strip()}


def _open_trades():
    p = ROOT / "data" / "open_trades.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _read_text_lossy(path: Path) -> str:
    """Read legacy text files that may not be UTF-8 yet."""
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            return _sanitize_legacy_text(text)
        except UnicodeDecodeError:
            continue
    return _sanitize_legacy_text(raw.decode("utf-8", errors="replace"))


def _sanitize_legacy_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    repaired_lines = []
    for line in text.split("\n"):
        repaired = line
        if any(marker in line for marker in ("â", "Ã", "ð")):
            for encoding in ("cp1252", "latin-1"):
                try:
                    candidate = line.encode(encoding).decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    continue
                if candidate.count("â") + candidate.count("Ã") + candidate.count("ð") < line.count("â") + line.count("Ã") + line.count("ð"):
                    repaired = candidate
                    break
        repaired_lines.append(repaired)
    text = "\n".join(repaired_lines)
    replacements = {
        "\u0091": "'",
        "\u0092": "'",
        "\u0093": '"',
        "\u0094": '"',
        "\u0095": "*",
        "\u0096": "-",
        "\u0097": "—",
        "\u00a0": " ",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    mojibake = {
        "âœ…": "✅",
        "âŒ": "❌",
        "â": "❌",
        "âš ï¸": "⚠️",
        "âš ️": "⚠️",
        "ðŸŸ¢": "🟢",
        "ðŸ”´": "🔴",
        "ðŸ“Š": "📊",
        "ðŸ’°": "💰",
        "ðŸ’µ": "💵",
        "ðŸ“‹": "📋",
        "ðŸŽ¯": "🎯",
        "ðŸ›‘": "🛑",
        "ðŸ”’": "🔒",
        "ðŸ“ˆ": "📈",
        "ðŸ“‰": "📉",
        "â†’": "→",
        "Ã—": "×",
        "Â·": "·",
        "â€¢": "•",
        "â€“": "–",
        "â€”": "—",
        "â\x80\"": "—",
        "â€˜": "'",
        "â€™": "'",
        "â€œ": '"',
        "â€": '"',
        "â€": '"',
        "â“": "❓",
        "â›”": "⛔",
        "âœ…": "✅",
        "âœ": "✅",
    }
    for old, new in mojibake.items():
        text = text.replace(old, new)
    return text


def _journal_preview(content: str, max_chars: int = 1800) -> str:
    """
    Show the newest meaningful journal content, not the oldest header text.
    Also neutralize nested code fences so Discord formatting stays intact.
    """
    markers = ["\n## Market Open Summary", "\n## End-of-Day Summary", "\n## Cycle", "\n## Crypto Cycle", "\n## Trades Executed"]
    start = 0
    for marker in markers:
        idx = content.rfind(marker)
        if idx > start:
            start = idx + 1
    preview = content[start:] if start else content
    if len(preview) > max_chars:
        preview = preview[-max_chars:]
        newline = preview.find("\n")
        if newline != -1:
            preview = preview[newline + 1:]
    return preview.replace("```", "'''").strip()


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_status():
    account   = run("research.py", "account")
    positions = run("research.py", "positions")
    clock     = run("trade.py", "status")

    equity      = float(account.get("equity", 0))
    cash        = float(account.get("cash", 0))
    last_equity = float(account.get("last_equity", equity))
    day_pnl     = equity - last_equity
    pnl_pct     = (day_pnl / last_equity * 100) if last_equity else 0
    market_open = clock.get("is_open", False)
    killed      = KILL_FLAG.exists()
    pos_count   = len(positions) if isinstance(positions, list) else 0

    lines = [
        f"**FeintTrade Status** — {datetime.now().strftime('%H:%M MT')}",
        f"{'🛑 **KILL SWITCH ACTIVE**' if killed else ('🟢 Market Open' if market_open else '🔴 Market Closed')}",
        "",
        f"💰 Portfolio: **${equity:,.2f}**",
        f"📊 Day P&L: **${day_pnl:+,.2f}** ({pnl_pct:+.2f}%)",
        f"💵 Cash: ${cash:,.2f}",
        f"📋 Open Positions: {pos_count}",
    ]
    return "\n".join(lines)


def cmd_positions():
    positions = run("research.py", "positions")
    if not isinstance(positions, list) or not positions:
        return "📋 No open positions."
    positions = normalize_positions(positions)
    ot = _open_trades()

    lines = [f"**Open Positions** ({len(positions)})"]
    for p in positions:
        sym     = p.get("symbol", "?")
        pnl     = float(p.get("unrealized_pl", 0))
        pnl_pct = float(p.get("unrealized_plpc", 0)) * 100
        qty     = float(p.get("qty", 0))
        curr    = float(p.get("current_price", 0))
        entry   = float(p.get("avg_entry_price", 0))
        icon    = "🟢" if pnl >= 0 else "🔴"
        warn    = " ⚠️ STOP-LOSS" if pnl_pct <= -5 else ""
        ctx     = ot.get(sym) or {}
        strat   = ctx.get("setup_type", "untracked")
        lines.append(
            f"{icon} **{sym}** {qty:g} × ${curr:,.4f}"
            f" | Entry ${entry:,.4f}"
            f" | P&L ${pnl:+,.2f} ({pnl_pct:+.1f}%){warn}"
            f"\n    🎯 {strat}"
        )
    return "\n".join(lines)


def cmd_price(*args):
    if not args:
        return "Usage: `!price <SYMBOL>` (e.g. `!price NVDA` or `!price BTC/USD`)"
    sym = normalize_symbol(args[0].upper())
    snap = run("research.py", "snapshot", sym)
    if not isinstance(snap, dict) or not snap.get("price"):
        return f"❓ No price for **{sym}** ({snap.get('error', 'unknown')})"
    chg = snap.get("day_change_pct")
    arrow = "🟢▲" if (chg or 0) >= 0 else "🔴▼"
    bid, ask = snap.get("bid"), snap.get("ask")
    spread = f" | bid ${bid:,.4f} / ask ${ask:,.4f}" if bid and ask else ""
    return (f"**{sym}**  ${float(snap['price']):,.4f}  {arrow} "
            f"{chg:+.2f}% today{spread}" if chg is not None
            else f"**{sym}**  ${float(snap['price']):,.4f}{spread}")


def cmd_strategies():
    """Show each open position's active strategy + the regime's playbook."""
    positions = normalize_positions(run("research.py", "positions"))
    ot = _open_trades()
    lines = ["**Active Strategies on Positions**"]
    if isinstance(positions, list) and positions:
        for p in positions:
            sym = p.get("symbol", "?")
            ctx = ot.get(sym) or {}
            strat = ctx.get("setup_type", "untracked entry")
            conv = ctx.get("conviction")
            pnl_pct = float(p.get("unrealized_plpc", 0)) * 100
            extra = f" · conviction {conv}/10" if conv else ""
            lines.append(f"• **{sym}** ({pnl_pct:+.1f}%) → `{strat}`{extra}")
    else:
        lines.append("• No open positions.")
    reg = run("regime.py", "detect")
    if isinstance(reg, dict) and reg.get("regime"):
        lines.append(f"\n**Regime:** {reg['regime']} (×{reg.get('multiplier',0.6)*100:.0f}% sizing) — "
                     f"active: {', '.join(reg.get('active_strategies', [])[:5])}")
    return "\n".join(lines)


def cmd_buy(*args):
    if len(args) < 2:
        return "Usage: `!buy <SYMBOL> <QTY> [limit_price]`  (validated against risk rules)"
    return _manual_order("buy", args)


def cmd_sell(*args):
    if not args:
        return "Usage: `!sell <SYMBOL> [QTY]`  (omit QTY to close the whole position)"
    return _manual_order("sell", args)


def _manual_order(side, args):
    import trade
    sym = normalize_symbol(args[0].upper())
    account = run("research.py", "account")
    positions = normalize_positions(run("research.py", "positions"))
    snap = run("research.py", "snapshot", sym)
    price = snap.get("price") if isinstance(snap, dict) else None

    if side == "sell":
        pos = next((p for p in positions if p.get("symbol") == sym), None)
        if not pos:
            return f"❌ No open position in **{sym}** to sell."
        held = abs(float(pos.get("qty", 0)))
        qty = float(args[1]) if len(args) > 1 else held
        price = price or float(pos.get("current_price", 0))
        limit = round(price * 0.998, 5 if is_crypto(sym) else 2)
    else:
        if not price:
            return f"❌ Could not get a price for **{sym}**."
        qty = float(args[1])
        limit = float(args[2]) if len(args) > 2 else round(price * 1.002, 5 if is_crypto(sym) else 2)

    if KILL_FLAG.exists():
        return "🛑 Kill switch is active — clear it with `!resume` before trading."

    # per-symbol allocation cap from watchlist, default from risk config
    risk = load_risk()
    wl = json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))
    limits = {normalize_symbol(s["symbol"]): s["max_allocation_pct"] for s in wl.get("watchlist", [])}
    cap = limits.get(sym, risk.get("default_unlisted_max_alloc_pct", 10))

    ok, msg = trade.validate_order(sym, qty, side, limit, account, positions, cap, risk)
    if not ok:
        return f"⛔ **{side.upper()} {qty:g} {sym}** rejected: {msg}"
    result = trade.place_order(sym, qty, side, limit)
    if isinstance(result, dict) and result.get("error"):
        return f"❌ Broker rejected **{side.upper()} {qty:g} {sym}**: {result['error']}"
    return f"✅ Manual **{side.upper()} {qty:g} {sym}** @ ${limit} submitted (status {result.get('status','submitted')})."


def cmd_usage():
    """Show today's API cost breakdown from logs/api_usage.jsonl."""
    log_path = ROOT / "logs" / "api_usage.jsonl"
    if not log_path.exists():
        return "No API usage log found yet."
    try:
        import json as _json
        from collections import defaultdict
        from common import today_mt
        today = today_mt()
        records = []
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    r = _json.loads(line)
                    if r.get("ts", "").startswith(today):
                        records.append(r)
                except Exception:
                    pass
        if not records:
            return f"No API calls logged today ({today}) yet."
        by_routine = defaultdict(lambda: {"calls": 0, "cost": 0.0})
        total = 0.0
        for r in records:
            k = r.get("routine", "?")
            by_routine[k]["calls"] += 1
            by_routine[k]["cost"] += r.get("cost_usd", 0)
            total += r.get("cost_usd", 0)
        lines = [f"**API Usage Today ({today}) — ${total:.4f} total**"]
        for k, v in sorted(by_routine.items(), key=lambda x: -x[1]["cost"]):
            lines.append(f"  `{k}` ×{v['calls']} calls → ${v['cost']:.4f}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading usage log: {e}"


def cmd_report(*args):
    session = args[0] if args else "now"
    if session not in ("eod", "afterhours", "now"):
        session = "now"
    run("orchestrator.py", "report", session)
    return f"📊 Generating **{session}** report — posting the full detailed summary + attachment now."


def cmd_orders():
    orders = run("trade.py", "orders", "all")
    if not isinstance(orders, list) or not orders:
        return "📋 No recent orders."

    lines = ["**Recent Orders** (last 10)"]
    for o in orders[:10]:
        side  = o.get("side", "").upper()
        icon  = "🟢" if side == "BUY" else "🔴"
        lp    = f"@ ${float(o['limit_price']):,.2f}" if o.get("limit_price") else ""
        time  = o.get("created_at", "")[:16].replace("T", " ")
        lines.append(f"{icon} `{time}` {side} **{o['qty']} {o['symbol']}** {lp} — {o['status'].upper()}")
    return "\n".join(lines)


def cmd_kill():
    if KILL_FLAG.exists():
        return "🛑 Kill switch is already active. Use `!resume` to clear it."

    KILL_FLAG.write_text(
        f"Kill activated at {datetime.now().isoformat()} via Discord !kill command",
        encoding="utf-8",
    )
    run("trade.py", "cancel")

    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from discord_notify import kill_activated
        kill_activated(source="Discord `!kill` command")
    except Exception:
        pass

    return (
        "🛑 **KILL SWITCH ACTIVATED**\n"
        "All open orders cancelled. Trading halted.\n"
        "Send `!resume` to re-enable trading."
    )


def cmd_resume():
    actions = []

    if KILL_FLAG.exists():
        KILL_FLAG.unlink()
        actions.append("kill switch cleared")
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            from discord_notify import kill_deactivated
            kill_deactivated()
        except Exception:
            pass

    # Also clear any loss-streak lockout
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from learning import clear_loss_streak_lockout
        clear_loss_streak_lockout()
        actions.append("loss-streak lockout cleared")
    except Exception:
        pass

    if not actions:
        return "✅ No active stops or lockouts — trading is already running normally."

    return f"✅ **Resumed.** {' | '.join(a.capitalize() for a in actions)}. Agent will trade normally on next cycle."


def cmd_cancel():
    result = run("trade.py", "cancel")
    code = result.get("status_code", "done")
    return f"✅ All open orders cancelled (status {code}). Trading continues as normal — this was a one-time cancel."


def cmd_journal():
    today_str    = datetime.now().strftime("%Y-%m-%d")
    journal_file = ROOT / "journal" / f"{today_str}.md"
    if not journal_file.exists():
        return f"📓 No journal entry for {today_str} yet."

    content = _read_text_lossy(journal_file)
    # Heal legacy/mojibake encoding in place — but ONLY when sanitizing actually
    # changed the bytes. A plain read command shouldn't rewrite an already-clean file
    # (that surprise write churned the mtime on every !journal). Best-effort: never let
    # a write failure (read-only FS, lock) break the read command.
    try:
        _current = journal_file.read_text(encoding="utf-8")
    except Exception:
        _current = None
    if _current != content:
        try:
            journal_file.write_text(content, encoding="utf-8", newline="\n")
        except Exception:
            pass
    preview = _journal_preview(content)
    truncated = len(preview) < len(content)
    suffix = "\n(truncated — full journal in FeintTrade2/journal/)" if truncated else ""
    return f"**Journal — {today_str} (latest activity)**\n{preview}{suffix}"


def cmd_help():
    return (
        "**FeintTrade — Bot Commands**\n"
        "`!status` — portfolio equity, cash, day P&L, kill state\n"
        "`!positions` — open positions with live P&L + active strategy\n"
        "`!strategies` — the strategy running on each position + regime playbook\n"
        "`!price <SYM>` — live price (e.g. `!price NVDA`, `!price BTC/USD`)\n"
        "`!orders` — last 10 orders with status\n"
        "`!buy <SYM> <QTY> [limit]` — manual buy (validated against risk rules)\n"
        "`!sell <SYM> [QTY]` — manual sell / close (omit QTY = full position)\n"
        "`!report [eod|afterhours|now]` — post a detailed session report\n"
        "`!kill` — 🛑 emergency stop: halt trading, cancel open orders\n"
        "`!resume` — clear kill switch and resume trading\n"
        "`!cancel` — cancel open orders once (agent keeps running)\n"
        "`!journal` — today's journal summary\n"
        "`!heartbeat` — run a full live research + decision cycle (~2-3 min)\n"
        "`!channels` — Discord channel wiring + reachability\n"
        "`!test` — 🧪 post a test message to every channel + health check\n"
        "`!summary` — market/regime summary\n"
        "`!digest` — compact daily digest (P&L, positions, regime)\n"
        "`!research` — today's research brief\n"
        "`!benchmark` — realized P&L vs buy-and-hold + no-trade\n"
        "`!ask <question>` — ask the AI a trading/strategy question (learn as you go)\n"
        "`!usage` — today's API cost breakdown\n"
        "`!cost` — Anthropic API spend (month / projected / budget)\n"
        "`!tests` — run the test suite + post ✅/❌ per test to #ft-dev-log\n"
        "`!intel` — decision-intelligence audit (what the agent gets right/wrong)\n"
        "`!lab` — strategy lab: what-if recommendations to make the book profitable\n"
        "`!council <SYM>` — multi-agent analyst second opinion (advisory)\n"
        "`!quote <SYM>` — free public-API price (crypto) + USD-strength macro\n"
        "`!help` — this message"
    )


# ── FeintTrade operator commands (channel ops + on-demand briefs) ────────────────

def cmd_channels():
    """Channel wiring + per-channel reachability (the #ft-* layout health)."""
    try:
        import discord_channels as dch
    except Exception as e:
        return f"❌ discord_channels unavailable: {e}"
    hc = dch.health_check()
    reach = hc.get("reachability", {})
    purpose = getattr(dch, "_PURPOSE", {})
    head = (f"**Discord Channels** — multichannel "
            f"{'🟢 ON' if hc.get('multichannel_enabled') else '🔴 OFF'} · "
            f"bot {'✓' if hc.get('bot_token_present') else '✗'}")
    lines = [head]
    for name in hc.get("channels", {}):
        r = reach.get(name, "—")
        icon = "🟢" if r == "ok" else ("⚪" if r == "unconfigured" else "🔴")
        lines.append(f"{icon} `#{name.replace('_', '-')}` — {purpose.get(name, '')}")
    return "\n".join(lines)


def cmd_test():
    """Broadcast a test message to EVERY channel and report per-channel delivery."""
    try:
        import discord_channels as dch
    except Exception as e:
        return f"❌ discord_channels unavailable: {e}"
    results = dch.broadcast_test("Triggered via Discord `!test`.")
    ok = sum(1 for r in results.values() if r.get("ok"))
    lines = [f"🧪 **Channel broadcast** — delivered **{ok}/{len(results)}**"]
    for name, r in results.items():
        icon = "✅" if r.get("ok") else "❌"
        detail = "" if r.get("ok") else f" ({r.get('detail', 'fail')})"
        lines.append(f"{icon} #{name.replace('_', '-')}{detail}")
    return "\n".join(lines)


def cmd_summary():
    """Market / regime summary (the #ft-command-post headline, on demand)."""
    reg = run("regime.py", "detect")
    if not isinstance(reg, dict) or not reg.get("regime"):
        return "Could not detect the market regime right now."
    lines = [
        f"**Market Summary** — {datetime.now().strftime('%a %H:%M MT')}",
        f"🧭 Regime: **{reg['regime']}** · sizing {reg.get('multiplier', 0.6) * 100:.0f}% · "
        f"stop {abs(reg.get('stop_loss_pct', -5)):.0f}%",
    ]
    if reg.get("vix"):
        lines.append(f"📉 VIX: {reg['vix']}")
    active = reg.get("active_strategies") or []
    if active:
        lines.append("Playbook: " + ", ".join(str(s) for s in active[:5]))
    return "\n".join(lines)


def cmd_digest():
    """Compact daily digest — P&L, positions, regime (FeintTrade-style roll-up)."""
    account = run("research.py", "account")
    positions = normalize_positions(run("research.py", "positions"))
    reg = run("regime.py", "detect")
    equity = float(account.get("equity", 0))
    last = float(account.get("last_equity", equity))
    day = equity - last
    pct = (day / last * 100) if last else 0
    n = len(positions) if isinstance(positions, list) else 0
    regime = reg.get("regime", "?") if isinstance(reg, dict) else "?"
    lines = [
        f"**Daily Digest** — {datetime.now().strftime('%a %H:%M MT')}",
        f"💰 ${equity:,.0f} · Day **{pct:+.2f}%** · {n} positions · Regime {regime}",
    ]
    if isinstance(positions, list) and positions:
        up = [p for p in positions if float(p.get("unrealized_pl", 0)) >= 0]
        lines.append(f"🟢 {len(up)} up / 🔴 {n - len(up)} down")
        for p in sorted(positions, key=lambda x: -abs(float(x.get("unrealized_pl", 0))))[:3]:
            lines.append(f"• {p.get('symbol', '?')} {float(p.get('unrealized_plpc', 0)) * 100:+.1f}%")
    else:
        lines.append("No open positions — sitting in cash (a valid decision).")
    return "\n".join(lines)


def cmd_research():
    """Today's research brief (pulled from the journal's research section)."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    jf = ROOT / "journal" / f"{today_str}.md"
    if not jf.exists():
        return f"🔬 No research for {today_str} yet (morning research runs ~7:45 AM MT)."
    content = _read_text_lossy(jf)
    for marker in ("## Top Setups", "## Symbol Analysis", "## Market Sentiment"):
        idx = content.find(marker)
        if idx != -1:
            seg = content[idx:idx + 1700].replace("```", "'''").strip()
            return f"**Research — {today_str}**\n{seg}"
    return f"**Research — {today_str}**\n{content[:1700].replace('```', chr(39) * 3).strip()}"


def cmd_benchmark():
    """Realized performance vs buy-and-hold (SPY) + no-trade baselines."""
    try:
        import replay
        return replay.format_report(replay.benchmark_report())
    except Exception as e:
        return f"❌ Benchmark failed: {e}"


def cmd_ask(*args):
    """
    Free-form trading/strategy Q&A, answered by Claude with live FeintTrade context.
    Lets the operator ask follow-up/clarification questions about trades, setups, or
    strategy right in Discord — educational, paper-only framing (no new bot needed).
    """
    if not args:
        return ("Usage: `!ask <question>` — e.g. `!ask why are we holding TQQQ?`, "
                "`!ask what is a squeeze release?`, `!ask is now a good time to add crypto?`")
    question = " ".join(args)[:500]
    try:
        import anthropic
        account = run("research.py", "account")
        positions = normalize_positions(run("research.py", "positions"))
        reg = run("regime.py", "detect")
        regime = reg.get("regime", "?") if isinstance(reg, dict) else "?"
        pos = "; ".join(f"{p.get('symbol')} {float(p.get('unrealized_plpc', 0)) * 100:+.1f}%"
                        for p in positions) if isinstance(positions, list) and positions else "none"
        equity = float(account.get("equity", 0)) if isinstance(account, dict) else 0
        system = (
            "You are FeintTrade's trading tutor inside a Discord bot. Answer the operator's "
            "question clearly and concisely for someone LEARNING to trade — practical and "
            "educational, no fluff, under ~180 words. This is a PAPER account; frame answers as "
            "education, not personalized financial advice.\n"
            f"Live context: regime {regime}, equity ${equity:,.0f}, open positions: {pos}."
        )
        client = anthropic.Anthropic(max_retries=2, timeout=30)
        resp = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=500,
                                      system=system, messages=[{"role": "user", "content": question}])
        return f"**❓ {question}**\n\n{resp.content[0].text.strip()}"[:1900]
    except Exception as e:
        return f"❌ Couldn't answer right now ({e})."


def cmd_cost():
    """Anthropic API spend — today/month/projected + budget gauge."""
    try:
        import api_cost
        return api_cost.format_brief()
    except Exception as e:
        return f"❌ Cost unavailable: {e}"


def cmd_tests():
    """Run the test suite and post a per-test ✅/❌ report to #ft-dev-log."""
    try:
        import test_report
        rc = test_report.post_report(do_post=True)
        return f"🧪 Test report posted to #ft-dev-log (exit code {rc})."
    except Exception as e:
        return f"❌ Test run failed: {e}"


def cmd_intel():
    """Decision-intelligence audit — what the agent keeps getting right/wrong (read-only)."""
    try:
        import intel_audit
        a = intel_audit.audit()
        intel_audit.post()   # full report → #ft-reports
        lines = ["🧠 **Decision-Intelligence Audit** — full report posted to #ft-reports."]
        ba = a.get("by_action") or []
        if ba:
            lines.append("Return by action: " + " · ".join(
                f"{act} {st.get('avg_primary_return_pct', 0):+.1f}%" for act, st in ba[:5]))
        over = [r for r in (a.get("blocker_predictiveness") or []) if r["verdict"].startswith("🔴")]
        if over:
            lines.append("⚠️ Over-restrictive blockers to review: " + ", ".join(
                f"{r['blocker']} ({r['avg_return_pct']:+.1f}%)" for r in over[:4]))
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Intel audit failed: {e}"


def cmd_lab():
    """Strategy Lab — evidence-based what-if recommendations to make the book profitable."""
    try:
        import strategy_lab
        rows = strategy_lab.join_rows()
        strategy_lab.post()
        return ("🧪 **Strategy Lab** — full report posted to #ft-reports.\n"
                + "\n".join(f"• {r}" for r in strategy_lab.recommendations(rows)[:5]))
    except Exception as e:
        return f"❌ Strategy Lab failed: {e}"


def cmd_council(*args):
    """Convene the analyst council on a symbol — multi-agent second opinion (advisory)."""
    if not args:
        return ("Usage: `!council <SYMBOL>` — e.g. `!council NVDA`. A technical/catalyst/risk "
                "analyst panel gives a second opinion (advisory — does not change trades).")
    sym = normalize_symbol(args[0].upper())
    try:
        import council
        v = council.convene(sym, context=f"Give your honest read on {sym} right now for a swing trade.")
        council.post(v)
        syn = v["synthesis"]
        lines = [f"🏛️ **Council — {sym}: {syn['recommendation']}** "
                 f"(avg {syn['avg_score']}/10 · risk {syn.get('risk_score', '?')}/10)",
                 f"_{syn['rationale']}_"]
        for role, a in v["analysts"].items():
            lines.append(f"• {role.title()}: {a.get('score', '?')}/10")
        lines.append("Full panel posted to #ft-research.")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Council failed: {e}"


def cmd_quote(*args):
    """Free public-API price (crypto via Coinbase/CoinGecko) + the USD-strength macro signal."""
    if not args:
        return "Usage: `!quote <SYM>` — e.g. `!quote BTC/USD` (free crypto price + USD-strength macro)."
    sym = args[0].upper()
    try:
        import public_data
        out = []
        p = public_data.crypto_price(sym)
        if p:
            out.append(f"💰 **{sym}**: ${p:,.4f}".rstrip("0").rstrip("."))
        else:
            out.append(f"❓ No free price for **{sym}** — free no-key equity feeds are geo-blocked "
                       "here, so equities stay on Alpaca/yfinance. Try a crypto symbol like BTC/USD.")
        mb = public_data.macro_brief()
        if mb:
            out.append(f"🌎 {mb}")
        return "\n".join(out)
    except Exception as e:
        return f"❌ Quote failed: {e}"


# No-arg commands
COMMANDS = {
    "!status":     cmd_status,
    "!usage":      cmd_usage,
    "!positions":  cmd_positions,
    "!strategies": cmd_strategies,
    "!orders":     cmd_orders,
    "!kill":       cmd_kill,
    "!resume":     cmd_resume,
    "!cancel":     cmd_cancel,
    "!journal":    cmd_journal,
    "!channels":   cmd_channels,
    "!test":       cmd_test,
    "!summary":    cmd_summary,
    "!digest":     cmd_digest,
    "!research":   cmd_research,
    "!benchmark":  cmd_benchmark,
    "!cost":       cmd_cost,
    "!tests":      cmd_tests,
    "!intel":      cmd_intel,
    "!lab":        cmd_lab,
    "!help":       cmd_help,
    # !heartbeat is handled directly in bot.py (async, long-running)
}

# Commands that take arguments
ARG_COMMANDS = {
    "!price":   cmd_price,
    "!buy":     cmd_buy,
    "!sell":    cmd_sell,
    "!report":  cmd_report,
    "!ask":     cmd_ask,
    "!explain": cmd_ask,
    "!council": cmd_council,
    "!quote":   cmd_quote,
}


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "!help"
    args = sys.argv[2:]
    if command in ARG_COMMANDS:
        print(ARG_COMMANDS[command](*args))
    elif command in COMMANDS:
        print(COMMANDS[command]())
    else:
        print(f"Unknown command: `{command}`\nUse `!help` to see available commands.")
