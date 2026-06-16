"""
Trading teaching engine → #ft-training-post.

For every meaningful trade decision (proposal / research), post a beginner-friendly
LESSON plus a generated graphic card explaining WHY the agent did what it did — and a
generalizable takeaway — regardless of how the trade turns out. The goal is to help the
operator learn trading by watching real decisions in plain language.

Cards are drawn with Pillow (no matplotlib needed). Lessons are rule-based templates
keyed off action / setup / regime / signals, so they are free, deterministic, and
testable. discord_channels.post_image() ships the PNG + embed to #ft-training-post.
"""

import io
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── Lesson content (plain-English) ──────────────────────────────────────────────

ACTION_TITLE = {
    "BUY":   "Why we're BUYING {sym}",
    "ADD":   "Why we're ADDING to {sym}",
    "HOLD":  "Why we're HOLDING {sym}",
    "TRIM":  "Why we're TRIMMING {sym}",
    "CLOSE": "Why we're CLOSING {sym}",
    "SELL":  "Why we're SELLING {sym}",
    "SKIP":  "Why we're SKIPPING {sym}",
    "NO_TRADE": "Why we're sitting in CASH",
}

# NOTE: matched by substring (see _setup_key), first key wins — so the SPECIFIC keys
# (gap/scored/inverse/short/panic) are listed BEFORE the generic momentum/breakout, or a
# name like "short_momentum" would match "momentum" and lose its inverse-ETF lesson.
SETUP_EXPLAIN = {
    "gap": "Gap-and-go: the stock opened well above yesterday's close on a fresh catalyst and kept running. We buy strength that HOLDS the gap — a gap that fills back in is a trap.",
    "scored": "Crypto scored system: we tally ~14 trend/momentum/sentiment signals and only buy at ≥5/10. A quant-style checklist removes emotion and 'this feels like a bottom' guessing.",
    "inverse": "Inverse ETF: it RISES when the market falls. We buy it only on its OWN confirmed uptrend in a non-bull tape — never to 'catch' a one-day dip while the broader trend is still up (that quietly bled the book).",
    "short": "Short-momentum (via inverse ETF): profiting from a CONFIRMED downtrend by trading the direction the tape is actually going. These are -3x decay instruments — short holds only.",
    "panic": "Panic hedge (UVXY): a short-term fear-spike trade that decays fast. It is INTRADAY ONLY — never held overnight, or theta/decay eats the gain.",
    "squeeze": "A 'squeeze' is when price coils into very low volatility (Bollinger Bands inside Keltner Channels). When it RELEASES with volume it often runs — we only act on a bullish release, never the coil.",
    "ema": "EMA cross + VWAP reclaim: the short-term trend turns up AND price trades above its volume-weighted average — buyers are in control.",
    "vwap": "VWAP is the day's 'fair price'. Trading above it = buyers in control; reclaiming it after a dip is a classic continuation cue.",
    "breakout": "A breakout clears a prior high on heavy volume. Volume is the lie-detector — no volume, no trust.",
    "momentum": "Momentum: price + indicators agree in one direction. We trade WITH the tape, not against it.",
    "reversion": "Oversold bounce: price fell too far, too fast with no bad news, and is snapping back toward fair value. We still need a stop — knives cut.",
    "fib": "Fibonacci levels mark where pullbacks often pause (0.382 / 0.5 / 0.618). We look for a reversal candle there, not a blind catch.",
    "obv": "OBV (On-Balance Volume) tracks whether volume confirms price. Price up + OBV up = real buying; price up + OBV down = distribution (a warning).",
    "options": "A long call/put is a leveraged, defined-risk bet on direction — max loss is the premium, and theta (time decay) works against us, so we keep DTE short and exits tight (+100% / -50%).",
}

# "What to watch next" — turns each decision into an actionable management plan so the
# operator learns HOW a trade is run, not just why it was opened.
MANAGE = {
    "BUY":   "Now watch: price must hold above the stop and keep making higher lows. Plan — take partial profit near +10%, then trail the rest, giving back at most ~4% from the peak.",
    "ADD":   "We only add because the base lot is GREEN. Watch the COMBINED size against the per-symbol cap; the stop stays where the original thesis breaks.",
    "HOLD":  "Doing nothing IS the trade. Watch the trailing stop and the thesis — we exit only if one breaks, never out of boredom.",
    "TRIM":  "Half is off the table, locking gains. The runner now works toward the next target on a trailed stop — let it breathe.",
    "CLOSE": "Risk is off. Don't 'revenge trade' to win it back — wait for the next clean, confirmed setup.",
    "SELL":  "Exit complete. Log WHY (stop / target / thesis break) so the next decision learns from this one.",
    "SKIP":  "Keep it on the watchlist. If it confirms next cycle (volume + reclaim), it graduates from 'watch' to a real entry.",
    "NO_TRADE": "Cash is dry powder, not a missed opportunity. The best entries come to those who wait for confluence.",
}

# Common mistake each action guards against — the anti-pattern that kills accounts.
PITFALL = {
    "BUY":   "Common mistake: chasing an extended move with no stop room, or sizing by FOMO instead of conviction.",
    "ADD":   "Common mistake: averaging DOWN into a loser to 'lower the average' — that's how accounts blow up.",
    "HOLD":  "Common mistake: selling a winner early out of fear, then watching it run without you.",
    "TRIM":  "Common mistake: dumping the WHOLE position at the first green and capping your best trades.",
    "CLOSE": "Common mistake: moving the stop lower to 'give it room.' Hope is not a strategy.",
    "SELL":  "Common mistake: exiting on emotion mid-bar instead of at the planned level.",
    "SKIP":  "Common mistake: forcing a marginal trade out of boredom. Over-trading is the #1 small-account killer.",
    "NO_TRADE": "Common mistake: inventing a setup because you feel you 'should' be trading. No edge = no trade.",
}

# Rotating mini-glossary so every training post also teaches one core term.
GLOSSARY = [
    ("VWAP", "Volume-Weighted Average Price — the day's 'fair value'. Above it = buyers in control."),
    ("ATR", "Average True Range — the typical move size. Used to set stops wide enough for the volatility."),
    ("OBV", "On-Balance Volume — a running volume tally that confirms whether buying/selling backs a price move."),
    ("RSI", "Relative Strength Index (0–100). >70 overbought, <30 oversold — momentum context, not a standalone trigger."),
    ("MACD", "Moving Average Convergence Divergence — a trend/momentum oscillator; a bullish cross = momentum turning up."),
    ("Squeeze", "Bollinger Bands inside Keltner Channels = coiled volatility. The RELEASE (with volume) is the tradeable move."),
    ("Slippage", "The gap between your expected fill and the real one. Limit orders cap it; market orders don't."),
    ("Drawdown", "A peak-to-trough drop in equity. Surviving drawdowns — via stops and sizing — is what keeps you in the game."),
    ("Expectancy", "Average $ per trade over many trades = (win% × avg win) − (loss% × avg loss). Positive expectancy is the whole goal."),
    ("Liquidity", "How easily you can get in/out without moving price. Thin liquidity is dangerous — especially on the EXIT."),
    ("R:R", "Reward-to-Risk — $ you're playing for vs $ you'd lose at the stop. We want ≥2:1 so being right half the time still wins."),
    ("Theta", "Time decay on options — premium you lose each day just from the clock ticking. Why we keep DTE short."),
]


def _glossary_term(seed=None):
    import datetime as _dt
    idx = (seed if seed is not None else _dt.datetime.now().hour) % len(GLOSSARY)
    return GLOSSARY[idx]

LESSON = {
    "BUY":   "Risk first — we define the STOP before entry and only take setups paying ≥2× what we risk. Conviction sizes the bet.",
    "ADD":   "Adding to a winner is fine; adding to a loser (averaging down) is how accounts blow up. We never do the latter.",
    "HOLD":  "Letting winners run is the whole edge. We don't sell just because we're up — we trail the stop and let the trend work.",
    "TRIM":  "Booking partial profit removes risk while leaving upside. You never go broke taking gains.",
    "CLOSE": "Cutting losers fast (regime stop) is how small accounts survive. The first loss is the cheapest one.",
    "SELL":  "Exits are decided by the plan — stop hit, target hit, or thesis broken — not by emotion.",
    "SKIP":  "No trade is a position. Passing when a setup doesn't qualify beats forcing a low-quality trade.",
    "NO_TRADE": "Patience pays. Over-trading is how small accounts die — we wait for multi-signal, confirmed setups.",
}

# Rotating general-wisdom tips so repetitive stances (no-trade / hold, posted hourly on
# the crypto cycle) stay educational instead of showing the same line every time.
GENERAL_TIPS = [
    "Position size is your #1 risk control — never bet more than you can afford to lose on one idea.",
    "The trend is your friend until it bends. Trade WITH momentum, not against it.",
    "Cash is a position. Sitting out a choppy market is itself a decision.",
    "Cut losers fast, let winners run — the opposite of what feels natural.",
    "A 2:1 reward:risk lets you be wrong half the time and still come out ahead.",
    "Volume confirms price. A breakout on light volume is usually a trap.",
    "Regime matters more than any single setup — size down when volatility is high.",
    "Judge your process, not the outcome — a good decision can still lose, and that's OK.",
    "FOMO is expensive. There is always another trade.",
    "Define your exit BEFORE you enter — entries are optional, exits are not.",
    "Scale into winners, never into losers. Adding to red is the classic account-killer.",
    "Your stop is a contract with yourself. Moving it to avoid a loss breaks the whole system.",
    "Leverage cuts both ways and 3x ETFs DECAY — they're short-hold tools, not investments.",
    "The market can stay irrational longer than you can stay solvent. Respect position size.",
    "Boredom is not a signal. The urge to 'do something' has lost more money than bad analysis.",
    "Liquidity matters most on the way OUT — never size into something you can't exit cleanly.",
]


def _rotating_tip(seed=None) -> str:
    import datetime as _dt
    idx = (seed if seed is not None else _dt.datetime.now().hour) % len(GENERAL_TIPS)
    return GENERAL_TIPS[idx]

REGIME_NOTE = {
    "BULL":  "BULL regime → full sizing, leveraged longs allowed.",
    "NEUTRAL": "NEUTRAL regime → ~60% sizing, pick only the best setups.",
    "BEAR":  "BEAR regime → small size, favor inverse ETFs/cash, no leveraged longs.",
    "PANIC": "PANIC regime → capital preservation, minimal exposure.",
}


def _norm_action(action: str) -> str:
    a = (action or "").upper()
    return a if a in ACTION_TITLE else ("NO_TRADE" if a in ("", "HOLD_CASH", "WAIT", "PASS") else a)


def _setup_key(setup_type: str) -> str:
    s = (setup_type or "").lower()
    for k in SETUP_EXPLAIN:
        if k in s:
            return k
    return ""


def lesson_for(d: dict) -> dict:
    """Build a teaching lesson from a decision dict. Keys used (all optional):
    symbol, action, setup_type, regime, conviction, signal_count, entry, stop, target, reasoning."""
    sym = d.get("symbol", "the market")
    action = _norm_action(d.get("action", "NO_TRADE"))
    title = ACTION_TITLE.get(action, f"{action} {sym}").format(sym=sym)

    bits = []
    sk = _setup_key(d.get("setup_type", ""))
    if sk:
        bits.append(SETUP_EXPLAIN[sk])
    sc = d.get("signal_count")
    if sc:
        bits.append(f"{sc} signals lined up before we acted — confluence beats any single indicator.")
    rr = _rr(d)
    if rr:
        bits.append(f"Reward:risk ≈ {rr:.1f}:1 — we risk a little to make a lot.")
    regime = (d.get("regime") or "").upper()
    if regime in REGIME_NOTE:
        bits.append(REGIME_NOTE[regime])
    explain = " ".join(bits) or (d.get("reasoning") or "")[:240]

    tip = _rotating_tip()
    # Repetitive stances (no-trade / hold) rotate through general tips so the training
    # channel keeps teaching something new instead of the same line every cycle.
    lesson_txt = tip if action in ("NO_TRADE", "HOLD") else LESSON.get(action, LESSON["NO_TRADE"])
    g_term, g_def = _glossary_term()
    return {
        "action": action,
        "title": title,
        "explain": explain.strip(),
        "lesson": lesson_txt,
        "manage": MANAGE.get(action, ""),
        "pitfall": PITFALL.get(action, ""),
        "tip": tip,
        "glossary_term": g_term,
        "glossary_def": g_def,
        "rr": rr,
    }


def _rr(d: dict):
    try:
        entry, stop, target = float(d["entry"]), float(d["stop"]), float(d["target"])
        risk = abs(entry - stop)
        return abs(target - entry) / risk if risk else None
    except Exception:
        return None


# ── Graphic card (Pillow) ────────────────────────────────────────────────────────

BG = (13, 17, 23); PANEL = (22, 27, 34); FG = (240, 246, 252); MUTE = (139, 148, 158)
COLORS = {"BUY": (46, 204, 113), "ADD": (46, 204, 113), "HOLD": (52, 152, 219),
          "TRIM": (230, 126, 34), "CLOSE": (231, 76, 60), "SELL": (231, 76, 60),
          "SKIP": (120, 130, 140), "NO_TRADE": (120, 130, 140)}


def _font(size, bold=False):
    from PIL import ImageFont
    for name in (["arialbd.ttf", "segoeuib.ttf"] if bold else ["arial.ttf", "segoeui.ttf"]) + ["DejaVuSans.ttf"]:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def make_card(lesson: dict) -> bytes:
    """Render a dark teaching card to PNG bytes. No emoji in the image (PIL fonts
    don't render them); emoji live in the Discord embed text instead."""
    from PIL import Image, ImageDraw
    W, H = 840, 580
    img = Image.new("RGB", (W, H), BG)
    dr = ImageDraw.Draw(img)
    accent = COLORS.get(lesson["action"], MUTE)

    dr.rectangle([0, 0, W, 8], fill=accent)
    dr.text((32, 30), "FEINTTRADE  ·  TRADING LESSON", font=_font(16, True), fill=MUTE)
    for i, line in enumerate(_wrap(dr, lesson["title"], _font(34, True), W - 64)[:2]):
        dr.text((32, 58 + i * 40), line, font=_font(34, True), fill=FG)

    # action badge
    badge = lesson["action"].replace("_", " ")
    bf = _font(20, True)
    bw = dr.textlength(badge, font=bf) + 28
    dr.rounded_rectangle([W - bw - 32, 34, W - 32, 70], radius=10, fill=accent)
    dr.text((W - bw - 18, 40), badge, font=bf, fill=BG)

    # "What's happening" panel
    y = 150
    dr.rounded_rectangle([32, y, W - 32, y + 150], radius=12, fill=PANEL)
    dr.text((48, y + 14), "WHAT'S HAPPENING", font=_font(15, True), fill=accent)
    ef = _font(20)
    for i, line in enumerate(_wrap(dr, lesson["explain"] or "—", ef, W - 96)[:5]):
        dr.text((48, y + 42 + i * 22), line, font=ef, fill=FG)

    # R:R strip (if available)
    y2 = y + 168
    if lesson.get("rr"):
        dr.text((48, y2), f"REWARD : RISK  ≈  {lesson['rr']:.1f} : 1", font=_font(18, True), fill=(46, 204, 113))
        bx = 360
        dr.rectangle([bx, y2 + 4, bx + min(int(lesson['rr'] * 60), 380), y2 + 18], fill=(46, 204, 113))
        dr.rectangle([bx, y2 + 22, bx + 60, y2 + 36], fill=(231, 76, 60))

    # "What to watch next" — the management plan (teaches HOW a trade is run)
    ny = y2 + (44 if lesson.get("rr") else 6)
    if lesson.get("manage"):
        dr.text((48, ny), "WHAT TO WATCH NEXT", font=_font(14, True), fill=(96, 165, 250))
        mf = _font(17)
        for i, line in enumerate(_wrap(dr, lesson["manage"], mf, W - 96)[:2]):
            dr.text((48, ny + 22 + i * 21), line, font=mf, fill=FG)

    # Lesson bar
    ly = H - 150
    dr.rounded_rectangle([32, ly, W - 32, ly + 112], radius=12, fill=(30, 38, 48))
    dr.text((48, ly + 12), "LESSON", font=_font(15, True), fill=(241, 196, 15))
    lf = _font(20, True)
    for i, line in enumerate(_wrap(dr, lesson["lesson"], lf, W - 96)[:3]):
        dr.text((48, ly + 36 + i * 22), line, font=lf, fill=FG)

    dr.text((32, H - 22), "Paper trading · educational only · not financial advice", font=_font(13), fill=MUTE)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── Post to #ft-training-post ─────────────────────────────────────────────────────

def teach(decision: dict, dedup_key: str | None = None, cycle_id: str = "") -> bool:
    """Build a lesson + card from a decision dict and post to #ft-training-post."""
    lesson = lesson_for(decision)
    try:
        png = make_card(lesson)
    except Exception:
        png = None
    emoji = {"BUY": "🟢", "ADD": "🟢", "HOLD": "🔵", "TRIM": "🟠",
             "CLOSE": "🔴", "SELL": "🔴", "SKIP": "⚪", "NO_TRADE": "⚪"}.get(lesson["action"], "📚")
    base = f"**{lesson['explain']}**\n\n" if lesson["explain"] else ""
    desc = f"{base}📖 **Lesson:** {lesson['lesson']}"
    if lesson.get("rr"):
        desc += f"\n\n⚖️ **Reward:Risk ≈ {lesson['rr']:.1f}:1** — risk a little to make a lot."
    if lesson.get("manage"):
        desc += f"\n\n🎯 **What to watch next:** {lesson['manage']}"
    if lesson.get("pitfall"):
        desc += f"\n\n⚠️ **{lesson['pitfall']}**"   # already begins 'Common mistake: …'
    if lesson.get("tip") and lesson["tip"] != lesson["lesson"]:
        desc += f"\n\n💡 **Tip:** {lesson['tip']}"
    if lesson.get("glossary_term"):
        desc += f"\n\n📘 **Term — {lesson['glossary_term']}:** {lesson['glossary_def']}"
    embed = {
        "title": f"{emoji} 📚 {lesson['title']}",
        "description": desc[:3500],
        "color": (COLORS.get(lesson["action"], MUTE)[0] << 16) + (COLORS.get(lesson["action"], MUTE)[1] << 8) + COLORS.get(lesson["action"], MUTE)[2],
    }
    if cycle_id:
        embed["footer"] = {"text": f"🔗 cycle {cycle_id}"}
    dk = dedup_key or f"teach:{lesson['action']}:{decision.get('symbol', '')}"
    try:
        import discord_channels as dch
        if png:
            embed["image"] = {"url": "attachment://lesson.png"}
            return dch.post_image("training", "lesson.png", png, embed=embed, dedup_key=dk)
        return dch.post("training", embed=embed, dedup_key=dk)
    except Exception:
        return False


def teach_from_payload(payload: dict, regime: str = "", cycle_id: str = "") -> bool:
    """Pick the most instructive decision from a proposal payload and teach it.
    Teaches the lead order if any, else the stand-pat (cash) stance."""
    payload = payload or {}
    orders = [o for o in payload.get("orders", []) if isinstance(o, dict)]
    closes = [c for c in payload.get("closes", []) if isinstance(c, dict)]
    lead = None
    if orders:
        o = max(orders, key=lambda x: x.get("conviction", x.get("score", 0)) or 0)
        lead = {"symbol": o.get("symbol"), "action": str(o.get("side", "buy")).upper().replace("BUY", "BUY"),
                "setup_type": o.get("setup_type"), "conviction": o.get("conviction"),
                "signal_count": (o.get("signals") or {}).get("signal_count"),
                "entry": o.get("limit_price"), "stop": o.get("stop"), "target": o.get("target"),
                "regime": regime, "reasoning": o.get("reasoning")}
    elif closes:
        c = closes[0]
        lead = {"symbol": c.get("symbol"), "action": "CLOSE", "regime": regime,
                "reasoning": c.get("reasoning")}
    else:
        lead = {"symbol": "the market", "action": "NO_TRADE", "regime": regime,
                "reasoning": (payload.get("summary") or "")}
    return teach(lead, cycle_id=cycle_id)


if __name__ == "__main__":
    # Demo: render a sample card to a file for visual inspection.
    sample = {"symbol": "NVDA", "action": "BUY", "setup_type": "squeeze_breakout",
              "regime": "BULL", "signal_count": 7, "entry": 205, "stop": 199, "target": 220}
    out = ROOT / "logs" / "sample_lesson.png"
    out.parent.mkdir(exist_ok=True)
    out.write_bytes(make_card(lesson_for(sample)))
    print("wrote", out)
