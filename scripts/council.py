"""
Council mode — multi-agent second opinion (OPT-IN, paper-only, OFF by default).

On high-conviction setups, convene a small "trading firm": specialized Haiku analysts
(technical, catalyst, risk) each return a 1-10 score + reasoning, then a deterministic
rule-based synthesis combines them into a recommendation (BUY / WATCH / SKIP) with a
hard risk veto. The LLM only does per-analyst scoring — synthesis is pure + testable.

ADVISORY: this posts a second opinion to #ft-research and logs it; it does NOT change
autonomous execution. Runs only when watchlist.json council.enabled=true (default false),
so it can never affect the live rig until the operator opts in. Cost ≈ 3 × Haiku call.

CLI / command:
    python scripts/council.py NVDA           # convene on a symbol, print verdict
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ANALYSTS = {
    "technical": ("You are the TECHNICAL analyst at a trading desk. Judge ONLY price action, "
                  "trend (EMAs/VWAP), momentum (RSI/MACD), and volume. End with 'Score: N/10' "
                  "where 10 = textbook bullish setup, 1 = clearly bearish/no setup."),
    "catalyst": ("You are the CATALYST & SENTIMENT analyst. Judge news, catalysts, and market "
                 "sentiment/regime for this name. End with 'Score: N/10' where 10 = strong "
                 "bullish catalyst/sentiment, 1 = negative or none."),
    "risk": ("You are the RISK MANAGER. Judge downside risk, volatility, liquidity, and "
             "reward:risk for this name RIGHT NOW. You can veto trades. End with 'Score: N/10' "
             "where 10 = low risk / great R:R, 1 = dangerous (veto)."),
}


def _cfg() -> dict:
    try:
        return json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8")).get("council", {}) or {}
    except Exception:
        return {}


def enabled() -> bool:
    return bool(_cfg().get("enabled", False))


def min_conviction() -> int:
    return int(_cfg().get("min_conviction", 8))


def _ask_claude(role: str, system: str, prompt: str) -> str:
    import anthropic
    client = anthropic.Anthropic(max_retries=2, timeout=30)
    model = _cfg().get("analyst_model", "claude-haiku-4-5-20251001")
    resp = client.messages.create(model=model, max_tokens=350, system=system,
                                   messages=[{"role": "user", "content": prompt}])
    return resp.content[0].text


def _parse_score(text: str):
    if not text:
        return None
    t = text.lower()
    m = re.search(r"score\D{0,8}(\d{1,2})", t) or re.search(r"\b(\d{1,2})\s*/\s*10\b", t)
    if m:
        return max(1, min(int(m.group(1)), 10))
    return None


def synthesize(analysts: dict) -> dict:
    """Pure rule-based synthesis of the analyst scores (deterministic, testable)."""
    tech = analysts.get("technical", {}).get("score")
    cat = analysts.get("catalyst", {}).get("score")
    risk = analysts.get("risk", {}).get("score")
    bull = [s for s in (tech, cat) if s is not None]
    avg = sum(bull) / len(bull) if bull else 0.0
    if risk is not None and risk <= 3:
        rec, rationale = "SKIP", "🛑 Risk manager veto — risk too high."
    elif avg >= 7 and (risk is None or risk >= 5):
        rec, rationale = "BUY", "Technical + catalyst strong and risk acceptable."
    elif avg >= 5:
        rec, rationale = "WATCH", "Mixed signals — wait for stronger confirmation."
    else:
        rec, rationale = "SKIP", "Insufficient conviction across the desk."
    return {"recommendation": rec, "conviction": round(avg), "avg_score": round(avg, 1),
            "risk_score": risk, "rationale": rationale}


def convene(symbol: str, context: str = "", ask_fn=None) -> dict:
    """Run the analysts (LLM) + synthesize (rule-based). ask_fn injectable for tests."""
    ask_fn = ask_fn or _ask_claude
    analysts = {}
    for role, system in ANALYSTS.items():
        try:
            text = ask_fn(role, system, f"Ticker: {symbol}\n\n{context}".strip())
            analysts[role] = {"score": _parse_score(text), "reasoning": (text or "").strip()[:400]}
        except Exception as e:
            analysts[role] = {"score": None, "reasoning": f"(analyst failed: {e})"}
    return {"symbol": symbol, "analysts": analysts, "synthesis": synthesize(analysts)}


def format_embed(v: dict) -> dict:
    syn = v["synthesis"]
    color = {"BUY": 0x2ecc71, "WATCH": 0xf1c40f, "SKIP": 0x95a5a6}.get(syn["recommendation"], 0x95a5a6)
    fields = []
    for role, a in v["analysts"].items():
        sc = a.get("score")
        fields.append({"name": f"{role.title()} — {sc if sc is not None else '?'}/10",
                       "value": (a.get("reasoning") or "—")[:300], "inline": False})
    return {"title": f"🏛️ Council verdict — {v['symbol']}: {syn['recommendation']} (avg {syn['avg_score']}/10)",
            "description": f"_{syn['rationale']}_ · risk {syn.get('risk_score', '?')}/10\n"
                           "Second opinion only — does not change autonomous execution.",
            "color": color, "fields": fields[:25]}


def post(v: dict) -> bool:
    try:
        import discord_channels as dch
        return dch.post("research", embed=format_embed(v), dedup_key=f"council:{v['symbol']}")
    except Exception:
        return False


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "SPY"
    verdict = convene(sym)
    print(json.dumps(verdict, indent=2))
