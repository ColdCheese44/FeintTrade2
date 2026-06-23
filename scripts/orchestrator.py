"""
Autonomous trading orchestrator — FeintTrade.
Called by Windows Task Scheduler.

Usage:
  python scripts/orchestrator.py research   # 7:45 AM MT
  python scripts/orchestrator.py trading    # 8:00 AM MT
  python scripts/orchestrator.py intraday   # every 15 min during session
  python scripts/orchestrator.py eod        # 2:15 PM MT
  python scripts/orchestrator.py crypto     # hourly 24/7
"""

import anthropic
import subprocess
import json
import os
import re
import sys
import logging
from pathlib import Path
from dotenv import load_dotenv

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)

# Defensive: an empty/blank ANTHROPIC_AUTH_TOKEN in the environment makes the SDK
# emit an illegal 'Authorization: Bearer ' header (httpx LocalProtocolError, which
# the SDK reports as a misleading "Connection error"). Scheduled tasks run clean,
# but a manual/interactive shell may inherit one — drop it so x-api-key auth is used.
if not (os.getenv("ANTHROPIC_AUTH_TOKEN") or "").strip():
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

JOURNAL_DIR = ROOT / "journal"
JOURNAL_DIR.mkdir(exist_ok=True)
KILL_FLAG = ROOT / "kill.flag"

logging.basicConfig(
    filename=ROOT / "agent.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

sys.path.insert(0, str(ROOT / "scripts"))
try:
    import discord_notify as dn
except Exception:
    dn = None

try:
    from learning import (
        log_entry, log_exit, detect_and_log_exits, forget_unfilled_entry,
        get_performance_brief, get_strategy_recommendations,
        update_performance, get_loss_streak, is_loss_streak_locked,
        lockout_manually_cleared, get_completed_trade_count,
    )
    LEARNING_ENABLED = True
except Exception as e:
    log.warning(f"Learning module unavailable: {e}")
    LEARNING_ENABLED = False
    def log_entry(*a, **kw): pass
    def log_exit(*a, **kw): pass
    def detect_and_log_exits(*a, **kw): return []
    def forget_unfilled_entry(*a, **kw): return False
    def get_performance_brief(): return ""
    def get_strategy_recommendations(): return ""
    def update_performance(): return {}
    def get_loss_streak(): return {"count": 0, "type": "none"}
    def is_loss_streak_locked(*a): return (False, "learning unavailable")
    def lockout_manually_cleared(): return False
    def get_completed_trade_count(): return 0

try:
    from regime import detect_regime, get_regime_brief
    REGIME_ENABLED = True
except Exception as e:
    log.warning(f"Regime module unavailable: {e}")
    REGIME_ENABLED = False
    def detect_regime(): return {"regime": "NEUTRAL", "multiplier": 0.6, "stop_loss_pct": -4.0}
    def get_regime_brief(): return "Regime detection unavailable — default NEUTRAL."

try:
    from intelligence import (
        get_intelligence_brief,
        get_intelligence_summary,
        log_decision_batch,
        refresh_intelligence,
    )
    INTELLIGENCE_ENABLED = True
except Exception as e:
    log.warning(f"Intelligence module unavailable: {e}")
    INTELLIGENCE_ENABLED = False
    def get_intelligence_brief(*a, **kw): return ""
    def get_intelligence_summary(*a, **kw): return {}
    def log_decision_batch(*a, **kw): return []
    def refresh_intelligence(*a, **kw): return {}

import trade
from common import (
    normalize_symbol, normalize_positions, is_crypto as _is_crypto,
    now_mt_str, now_mt as now_mt_dt, today_mt, load_risk, load_live_account,
    market_phase,
    get_daily_state, update_daily_state, check_daily_stop,
    record_session_entry, get_effective_caps,
    loss_streak_lockout_enforced, research_mode_active,
    force_autobuy_enabled, swing_mode_active, swing_stop_pct, trading_style,
    is_option, option_dte, load_options_config, options_enabled,
    conviction_factor,
)

try:
    from screener import get_discovery_brief
except Exception as e:
    log.warning(f"Screener unavailable: {e}")
    def get_discovery_brief(): return ""

try:
    from research import get_snapshot as _get_snapshot
except Exception as e:
    log.warning(f"Snapshot fetch unavailable: {e}")
    def _get_snapshot(sym): return {}

try:
    from market_research import get_market_research_brief
except Exception as e:
    log.warning(f"Market research module unavailable: {e}")
    def get_market_research_brief(): return ""

try:
    import report as session_report
except Exception as e:
    log.warning(f"Report module unavailable: {e}")
    session_report = None

try:
    from strategy_playbook import strategy_prompt_brief, validate_setup_for_entry
except Exception as e:
    log.warning(f"Strategy playbook unavailable: {e}")
    def strategy_prompt_brief(): return ""
    def validate_setup_for_entry(*a, **k): return (True, "ok")


# ── Helpers ───────────────────────────────────────────────────────────────────

def today():
    return today_mt()

def journal_path():
    return JOURNAL_DIR / f"{today()}.md"


def _read_text_lossy(path: Path) -> str:
    """Read legacy text files that may contain mixed Windows encodings."""
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
    return text


def read_journal_text() -> str:
    p = journal_path()
    if not p.exists():
        return ""
    return _read_text_lossy(p)


def write_journal_text(content: str):
    journal_path().write_text(content, encoding="utf-8", newline="\n")


def append_journal_text(section: str):
    existing = read_journal_text()
    write_journal_text(existing + section)

def run(script, *args):
    cmd = ["python", str(ROOT / "scripts" / script)] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    if result.returncode != 0:
        raise RuntimeError(f"{script} {args} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)

def safe_run(script, *args):
    try:
        return run(script, *args)
    except Exception as e:
        log.warning(f"safe_run {script} {args}: {e}")
        return {"error": str(e)}

def load_system():
    return (ROOT / "CLAUDE.md").read_text(encoding="utf-8", errors="replace")

def load_watchlist():
    return json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))

def is_crypto(symbol, asset_class=None):
    return _is_crypto(symbol, asset_class)


_LEVERAGED_LONG_TYPES = {"leveraged_etf"}


def _leveraged_long_symbols() -> set:
    """Watchlist symbols flagged as leveraged LONG ETFs (TQQQ/SOXL/FNGU/LABU/FAS).
    Inverse ETFs are a separate 'inverse_etf' type and are NOT included."""
    try:
        return {normalize_symbol(s["symbol"])
                for s in load_watchlist().get("watchlist", [])
                if s.get("type") in _LEVERAGED_LONG_TYPES}
    except Exception:
        return set()


def _regime_blocks_leveraged_long(sym: str, regime_name: str) -> bool:
    """SOP hard rule: never buy leveraged LONG ETFs in BEAR or PANIC regime — they decay
    hard against a down/panic tape. Inverse/hedge ETFs are NOT leveraged longs, so the
    risk-off posture (SQQQ/SOXS/UVXY) is untouched."""
    return (str(regime_name or "").upper() in ("BEAR", "PANIC")
            and normalize_symbol(sym) in _leveraged_long_symbols())


_INVERSE_ETF_TYPES = {"inverse_etf"}


def _inverse_etf_symbols() -> set:
    """Watchlist symbols flagged as leveraged INVERSE/hedge ETFs (SOXS/SQQQ/UVXY)."""
    try:
        return {normalize_symbol(s["symbol"])
                for s in load_watchlist().get("watchlist", [])
                if s.get("type") in _INVERSE_ETF_TYPES}
    except Exception:
        return set()


def _regime_blocks_inverse_etf(sym: str, regime_name: str) -> bool:
    """Data-driven hard rule (added 2026-06-15 from the trade log). NEVER buy a leveraged
    inverse/hedge ETF (SOXS/SQQQ/UVXY) in a BULL regime. Buying a -3x inverse as a
    'momentum long' to fade a one-day dip in an up-trending tape — then swing-holding it
    for days — lost -$1,670 at a 0% win rate (SOXS -$1,608 held ~4 days, SQQQ -$63), the
    single largest loss cluster in the book. That is fighting the tape on a decaying
    instrument. Inverse ETFs are downside tools: permitted only when the headline regime
    is NOT BULL (NEUTRAL/BEAR/PANIC). Mirror of _regime_blocks_leveraged_long."""
    return (str(regime_name or "").upper() == "BULL"
            and normalize_symbol(sym) in _inverse_etf_symbols())


# ── API config helpers ────────────────────────────────────────────────────────

def _api_cfg() -> dict:
    return load_watchlist().get("api_config", {})

def _route_model(routine: str) -> str:
    return _api_cfg().get("models", {}).get(routine, "claude-opus-4-8")

def _route_max_tokens(routine: str) -> int:
    return _api_cfg().get("max_tokens", {}).get(routine, 4096)

def _route_fallback_models(routine: str, primary: str) -> list[str]:
    configured = _api_cfg().get("fallback_models", {}).get(routine)
    if isinstance(configured, list) and configured:
        fallbacks = [m for m in configured if m and m != primary]
        if fallbacks:
            return fallbacks
    defaults = ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
    return [m for m in defaults if m != primary]


# ── Usage logging ─────────────────────────────────────────────────────────────
USAGE_LOG = ROOT / "logs" / "api_usage.jsonl"

# Approximate per-token prices in USD (update if Anthropic changes pricing)
_PRICES = {
    "claude-opus-4-8":              {"input": 15.0,  "output": 75.0},
    "claude-sonnet-4-6":            {"input":  3.0,  "output": 15.0},
    "claude-haiku-4-5-20251001":    {"input":  0.80, "output":  4.0},
}

def validate_model_config(models: dict | None = None) -> dict:
    """
    OFFLINE smoke-test of the configured Claude model IDs (watchlist.json
    api_config.models) against the local pricing table (_PRICES) — the single source
    of price data. Does NOT call the Anthropic API or place any order, so it is safe
    to run anytime: `python scripts/orchestrator.py validate-models`.

    A configured model with no _PRICES entry would silently bill at the Opus fallback
    rate (see _log_usage), so it is flagged. Returns:
      {"ok": bool, "models": {routine: {"model", "priced"}}, "unpriced": [...],
       "priced_models": [...]}.
    To verify the IDs against your actual Anthropic account/SDK, do a manual
    non-trading probe, e.g.:
        python -c "import anthropic; c=anthropic.Anthropic(); \
                   print(c.messages.create(model='claude-opus-4-8', max_tokens=1, \
                   messages=[{'role':'user','content':'ping'}]).model)"
    """
    if models is None:
        try:
            from common import load_watchlist
            models = (load_watchlist().get("api_config") or {}).get("models") or {}
        except Exception:
            models = {}
    out, unpriced = {}, []
    for routine, model in (models or {}).items():
        priced = model in _PRICES
        out[routine] = {"model": model, "priced": priced}
        if not priced and model not in unpriced:
            unpriced.append(model)
    return {"ok": not unpriced, "models": out, "unpriced": unpriced,
            "priced_models": sorted(_PRICES.keys())}


def _log_usage(routine: str, model: str, input_tokens: int, output_tokens: int):
    """Append one usage record. Non-blocking — never raises."""
    try:
        p = _PRICES.get(model, {"input": 15.0, "output": 75.0})
        cost = (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000
        record = {
            "ts": now_mt_str(),
            "routine": routine,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": round(cost, 6),
        }
        USAGE_LOG.parent.mkdir(exist_ok=True)
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        log.info(f"API usage [{routine}] {model}: {input_tokens}in/{output_tokens}out "
                 f"${cost:.4f}")
    except Exception:
        pass


def ask_model(prompt: str, system_text: str, routine: str = "cycle") -> str:
    """
    Route-aware Claude call. Uses the model and max_tokens defined in
    watchlist.json api_config for the given routine. Logs token usage.
    Falls back to cheaper configured models when the preferred model is
    temporarily unavailable or inaccessible so scheduled tasks can still finish.
    """
    model = _route_model(routine)
    max_tokens = _route_max_tokens(routine)
    # max_retries lets the SDK ride out transient connection blips / 5xx with
    # exponential backoff instead of aborting the whole (unattended) cycle.
    client = anthropic.Anthropic(max_retries=4, timeout=90.0)
    models_to_try = [model] + _route_fallback_models(routine, model)
    system_text = structured_system_text(system_text, routine)

    for idx, candidate_model in enumerate(models_to_try):
        try:
            response = client.messages.create(
                model=candidate_model,
                max_tokens=max_tokens,
                system=system_text,
                messages=[{"role": "user", "content": prompt}],
            )
            usage = getattr(response, "usage", None)
            in_tok  = getattr(usage, "input_tokens", 0) if usage else 0
            out_tok = getattr(usage, "output_tokens", 0) if usage else 0
            if idx > 0:
                log.warning(f"Model fallback used for {routine}: {model} -> {candidate_model}")
            _log_usage(routine, candidate_model, in_tok, out_tok)
            return response.content[0].text
        except Exception as e:
            msg = str(e).lower()
            recoverable = any(
                token in msg for token in (
                    "credit balance is too low",
                    "overloaded",
                    "rate limit",
                    "temporarily unavailable",
                    "connection error",
                    "timeout",
                    "timed out",
                    "internal server error",
                    "api_error",
                    "500",
                    "502",
                    "503",
                    "529",
                )
            )
            if idx < len(models_to_try) - 1 and recoverable:
                next_model = models_to_try[idx + 1]
                log.warning(
                    f"Claude call failed for {routine} on {candidate_model}; "
                    f"retrying with fallback {next_model}: {e}"
                )
                continue
            raise


def ask_claude(prompt: str, system_text: str, routine: str = "research") -> str:
    """Legacy alias — routes through ask_model for backwards compatibility."""
    return ask_model(prompt, system_text, routine)


def structured_system_text(base_system: str, routine: str) -> str:
    if routine not in {"trading", "crypto", "cycle", "json_repair"}:
        return base_system

    if routine == "trading":
        schema = '{"summary":"","orders":[],"holds":[],"candidates":[]}'
    elif routine in {"crypto", "cycle"}:
        schema = '{"summary":"","orders":[],"holds":[],"closes":[],"candidates":[]}'
    else:
        schema = "Output one valid JSON object only with the requested schema."

    supplement = f"""

STRUCTURED OUTPUT OVERRIDE:
- For this routine, structured output accuracy is more important than prose style.
- Start with the JSON immediately. Do not put any heading, intro sentence, table, or explanation before it.
- Emit exactly one JSON object matching the requested schema.
- After the JSON, keep any additional explanation short: flat bullets or short paragraphs only.
- Do not use markdown tables anywhere in the response.
- Do not repeat the schema instructions back to the user.
- If there are no trades, return empty lists and explain the no-trade posture briefly.
- If a close/exit is intended but qty is unknown, place it in "closes" rather than "orders".
- Required schema for this routine: {schema}
"""
    return base_system + supplement


def _is_decision_payload(payload: dict, routine: str | None = None) -> bool:
    if not isinstance(payload, dict):
        return False
    decision_keys = {"orders", "holds", "closes", "candidates", "summary"}
    if not any(key in payload for key in decision_keys):
        return False
    if routine == "trading":
        return "orders" in payload or "candidates" in payload or "holds" in payload
    if routine in {"crypto", "cycle"}:
        return "orders" in payload or "candidates" in payload or "holds" in payload or "closes" in payload
    return True


def _iter_json_objects(text: str):
    decoder = json.JSONDecoder()
    idx = 0
    length = len(text)
    while idx < length:
        ch = text[idx]
        if ch != "{":
            idx += 1
            continue
        try:
            payload, end = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            idx += 1
            continue
        yield payload
        idx += end


def _normalize_decision_payload(payload: dict, routine: str) -> dict:
    normalized = dict(payload or {})

    def _as_list(key: str) -> list:
        value = normalized.get(key)
        if isinstance(value, list):
            return value
        if value in (None, ""):
            return []
        return [value]

    normalized["orders"] = [
        item for item in _as_list("orders")
        if isinstance(item, dict) and item.get("symbol")
    ]
    normalized["holds"] = [
        item for item in _as_list("holds")
        if isinstance(item, dict) and item.get("symbol")
    ]
    normalized["closes"] = [
        item for item in _as_list("closes")
        if isinstance(item, dict) and item.get("symbol")
    ]
    normalized["candidates"] = [
        item for item in _as_list("candidates")
        if isinstance(item, dict) and item.get("symbol")
    ]
    normalized["summary"] = str(normalized.get("summary", "") or "")

    # Salvage close-only instructions that were accidentally returned in orders
    # without a qty. Those should be treated as full closes, not invalid orders.
    salvage_orders = []
    for order in normalized["orders"]:
        side = str(order.get("side", "")).lower()
        has_qty = order.get("qty") not in (None, "", 0, "0")
        if side == "sell" and not has_qty:
            normalized["closes"].append({
                "symbol": order.get("symbol"),
                "reasoning": order.get("reasoning", ""),
                "setup_type": order.get("setup_type", "close"),
                "conviction": order.get("conviction"),
                "score": order.get("score"),
            })
            continue
        salvage_orders.append(order)
    normalized["orders"] = salvage_orders

    if routine == "trading":
        normalized.setdefault("holds", [])
    if routine in {"crypto", "cycle"}:
        normalized.setdefault("closes", [])
    return normalized


def _repair_decision_payload(text: str, routine: str) -> dict | None:
    repair_prompt = f"""Convert the following trading-analysis text into ONE strict JSON object only.

Routine: {routine}

Rules:
- Output valid JSON only. No markdown fences. No commentary.
- Keep only the schema fields that belong to this routine.
- Preserve symbols, actions, prices, conviction, score, signal_count, blockers, and reasoning when present.
- If the text clearly says no trade / hold only, return empty orders and populate holds/candidates accordingly.
- If a sell/close instruction has no qty, put it in "closes" instead of "orders".

Schemas:
- trading: {{"summary":"","orders":[],"holds":[],"candidates":[]}}
- crypto:  {{"summary":"","orders":[],"holds":[],"candidates":[],"closes":[]}}
- cycle:   {{"summary":"","orders":[],"holds":[],"closes":[],"candidates":[]}}

Text:
{text}
"""
    try:
        repaired_text = ask_model(
            repair_prompt,
            "You convert trading analysis into strict machine-readable JSON. Output JSON only.",
            routine="json_repair",
        )
    except Exception as e:
        log.warning(f"JSON repair call failed for {routine}: {e}")
        return None

    for payload in _iter_json_objects(repaired_text):
        if _is_decision_payload(payload, routine):
            return _normalize_decision_payload(payload, routine)
    log.warning(f"JSON repair produced no valid payload for {routine}")
    return None


def _salvage_truncated_json(text: str, routine: str) -> dict | None:
    """
    Best-effort recovery of a single decision object that was truncated mid-output
    (model hit max_tokens). Walks the first JSON object, remembers the last point
    where a brace/bracket validly closed, drops the partial tail, and re-balances
    the open structures so the completed-so-far decision still parses. Free and
    instant — tried before the LLM repair fallback. If this ever fires, the real
    fix is a higher max_tokens for the routine (see watchlist.json api_config).
    """
    brace = text.find("{")
    if brace < 0:
        return None
    s = text[brace:]
    stack: list[str] = []
    in_str = esc = False
    safe_idx = -1
    safe_stack: list[str] | None = None
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
            safe_idx = i + 1          # a structure just closed — safe place to cut
            safe_stack = list(stack)
    if safe_idx < 0 or not safe_stack:
        return None                    # nothing closed yet, or already complete
    candidate = s[:safe_idx].rstrip()
    candidate += "".join(reversed(safe_stack))
    try:
        payload = json.loads(candidate)
    except Exception:
        return None
    if isinstance(payload, dict) and _is_decision_payload(payload, routine):
        log.warning(f"Recovered a TRUNCATED {routine} decision via local salvage "
                    f"— raise api_config.max_tokens[{routine}] to avoid data loss.")
        return _normalize_decision_payload(payload, routine)
    return None


def extract_decision_payload(decision_text: str, routine: str) -> dict | None:
    for match in re.finditer(r"```json\s*(.*?)\s*```", decision_text, re.DOTALL | re.IGNORECASE):
        try:
            payload = json.loads(match.group(1))
        except Exception:
            continue
        if _is_decision_payload(payload, routine):
            return _normalize_decision_payload(payload, routine)

    for payload in _iter_json_objects(decision_text):
        if _is_decision_payload(payload, routine):
            return _normalize_decision_payload(payload, routine)

    salvaged = _salvage_truncated_json(decision_text, routine)
    if salvaged:
        return salvaged

    return _repair_decision_payload(decision_text, routine)


def summarize_news(articles: list, symbol: str) -> str:
    """
    Haiku pre-pass: condense raw news articles to 2-sentence summaries before
    feeding to the main model. Cuts ~2,000 tokens per symbol from research prompts.
    Returns a short plaintext summary or empty string on failure.
    """
    cfg = _api_cfg()
    if not cfg.get("use_news_prepass", True) or not articles:
        return ""
    max_articles = cfg.get("news_prepass_max_articles", 3)
    snippets = []
    for a in articles[:max_articles]:
        title = a.get("title") or a.get("headline") or ""
        desc  = a.get("description") or a.get("content") or ""
        if title:
            snippets.append(f"- {title}: {desc[:300]}")
    if not snippets:
        return ""
    try:
        prompt = (
            f"Summarize these news items for {symbol} in 1-2 sentences total. "
            f"Focus on: catalyst, direction, magnitude. Be terse.\n\n"
            + "\n".join(snippets)
        )
        result = ask_model(prompt, "You are a financial news summarizer. Be extremely concise.",
                           routine="news_summary")
        return result.strip()
    except Exception as e:
        log.warning(f"News pre-pass failed for {symbol}: {e}")
        return ""

def kill_active():
    return KILL_FLAG.exists()

def now_mt():
    return now_mt_str()

def get_positions_norm():
    """Fetch live positions and normalize symbols (BTCUSD -> BTC/USD) at ingress."""
    pos = safe_run("research.py", "positions")
    return normalize_positions(pos) if isinstance(pos, list) else []

# Routines that post a !status snapshot to #ft-command-center when they finish (every
# cycle/trade/research/etc.). Utility routines (report/usage/validate-models) are excluded.
_STATUS_ROUTINES = {"research", "trading", "intraday", "cycle",
                    "eod", "afterhours", "marketopen", "crypto"}


def _notify(fn_name, *args, **kwargs):
    if dn:
        try:
            getattr(dn, fn_name)(*args, **kwargs)
        except Exception as e:
            log.warning(f"Discord notify '{fn_name}' failed: {e}")


def _notify_proposal(routine: str, payload: dict, regime: dict | None = None):
    """
    Post the agent's proposed decision (orders/closes/leaning candidates) to
    Discord as a 'trade proposal' alert. Fires from every decision routine right
    after the payload is parsed — so the operator sees intent in real time, even
    on stand-pat cycles. Gated by watchlist notifications config to avoid spam:
    by default posts only when there is something actionable to propose.
    """
    if not dn or not payload:
        return
    try:
        cfg = load_watchlist().get("notifications", {}) or {}
        if not cfg.get("proposal_alerts", True):
            return
        orders = payload.get("orders") or []
        closes = payload.get("closes") or []
        actionable_candidates = [
            c for c in (payload.get("candidates") or [])
            if isinstance(c, dict) and str(c.get("action", "")).upper() in {"BUY", "ADD", "TRIM", "CLOSE"}
        ]
        has_action = bool(orders or closes or actionable_candidates)
        if not has_action and not cfg.get("proposal_alerts_when_idle", False):
            return
        regime_label = (regime or {}).get("regime", "")
        cycle_id = payload.get("cycle_id") or f"{routine}-{now_mt_dt().strftime('%H%M%S')}"
        payload["cycle_id"] = cycle_id   # shared by the executed + training posts for audit
        _notify("decision_proposal", routine, payload, regime_label, "", cycle_id)
        # Notify-only decision card to #ft-approvals (autonomous — FYI/override surface).
        if orders or closes:
            _notify("approval_card", routine, orders, regime_label)
        # Teaching card → #ft-training-post: a plain-English lesson on the lead decision,
        # regardless of outcome (throttled by the training channel cooldown).
        try:
            import teaching
            teaching.teach_from_payload(payload, regime_label, cycle_id)
        except Exception as te:
            log.warning(f"Teaching post failed for {routine}: {te}")
        # Optional council second opinion on high-conviction buys — triple-gated
        # (enabled + auto_on_proposals + conviction >= threshold). OFF by default;
        # advisory only, never affects autonomous execution.
        try:
            import council
            if council.enabled() and council._cfg().get("auto_on_proposals"):
                for o in orders:
                    if (str(o.get("side", "")).lower() == "buy"
                            and (o.get("conviction") or 0) >= council.min_conviction()):
                        council.post(council.convene(o.get("symbol", ""), o.get("reasoning", "")))
                        break
        except Exception as ce:
            log.warning(f"Council convene failed for {routine}: {ce}")
    except Exception as e:
        log.warning(f"Proposal notification failed for {routine}: {e}")


def _notify_decision_executed(routine: str, payload: dict,
                              orders_placed: list | None = None,
                              closes_placed: list | None = None,
                              execution_events: list | None = None,
                              regime: dict | None = None):
    """
    Post the final decision outcome after execution so Discord gets the actual
    result, not only the pre-trade proposal.
    """
    if not dn:
        return
    try:
        cfg = load_watchlist().get("notifications", {}) or {}
        if not cfg.get("decision_alerts", True):
            return
        # Only post the executed-outcome alert when something ACTUALLY executed —
        # otherwise it just duplicates the proposal alert on every idle cycle (noise).
        # Rejections are already surfaced individually via order_rejected.
        if not (orders_placed or closes_placed):
            return
        regime_label = (regime or {}).get("regime", "")
        _notify(
            "decision_executed",
            routine,
            payload,
            orders_placed or [],
            closes_placed or [],
            execution_events or [],
            regime_label,
            "",
            payload.get("cycle_id", ""),
        )
    except Exception as e:
        log.warning(f"Decision-executed notification failed for {routine}: {e}")


def _notify_research_outputs(analysis: str, ctx: dict, regime: dict | None = None):
    """
    Fan the morning research out to the dedicated operator channels (once per run,
    not per cycle): research brief → #ft-research, regime headline → #ft-command-center,
    marketwide-discovery scan → #ft-signals.
    """
    if not dn:
        return
    try:
        regime = regime or {}
        regime_label = regime.get("regime", "")
        if analysis:
            _notify("research_brief", f"Morning Research — {today_mt()}", analysis[:1800])
        mult = regime.get("multiplier", 0.6)
        summary = (f"Regime **{regime_label}** · sizing {mult * 100:.0f}% of normal · "
                   f"stop {abs(regime.get('stop_loss_pct', -5)):.0f}%")
        _notify("market_summary", regime_label, summary)
        disc = (ctx or {}).get("discovery_brief") or ""
        if disc.strip():
            _notify("signals_card", "Marketwide Discovery", disc[:1800])
    except Exception as e:
        log.warning(f"Research-output notifications failed: {e}")


def _confirm_order_fill(result: dict, requested_qty: float, requested_price: float) -> tuple:
    """
    Return (filled_qty, fill_price, status) for a just-submitted order.

    Alpaca can accept a limit order that never fills. Learning logs must reflect
    fills, not acceptance, or a canceled exit can become a fake completed trade.
    """
    order_id = result.get("id") if isinstance(result, dict) else None
    if not order_id:
        return 0.0, None, "no_order_id"
    try:
        filled_qty, fill_price, status = trade.get_order_fill(order_id)
    except Exception as e:
        log.warning(f"Fill confirmation failed for order {order_id}: {e}")
        return 0.0, None, "fill_check_failed"
    if filled_qty > 0:
        return float(filled_qty), float(fill_price or requested_price), status or "filled"
    return 0.0, None, status or "unfilled"


def _research_autobuy(payload: dict, account: dict, positions: list,
                      symbol_limits: dict, min_buy_score: int) -> list:
    """
    Research-mode safety net: the model frequently proposes scores >= the buy bar
    but parks them in candidates/holds instead of placing orders. When research
    mode is on and the model placed NO buy orders, synthesize a BUY for the single
    highest-scoring qualifying candidate so a real trade (and its data) happens and
    is associated with the proposal.

    Conservative: prefers a NEW symbol; only adds to a held name if it is GREEN
    (never averages down). Sizing mirrors the prompt formula; _execute_orders then
    applies the regime multiplier and validates caps/cash, so hard limits still bind.
    """
    if not research_mode_active() or not payload:
        return []
    if not force_autobuy_enabled():
        return []  # forcing trades proved -EV — quality-gated swing relies on the model
    if any(str(o.get("side", "buy")).lower() == "buy"
           for o in (payload.get("orders") or []) if isinstance(o, dict)):
        return []  # model already wants to buy — don't interfere

    def _score(c):
        try:
            return int(float(c.get("score")))
        except (TypeError, ValueError):
            return None

    equity = float(account.get("equity", 100000) or 100000)
    held = {normalize_symbol(p.get("symbol", ""), p.get("asset_class")): p for p in positions}

    qualifying = []
    for c in (payload.get("candidates") or []):
        if not isinstance(c, dict):
            continue
        sc = _score(c)
        if sc is None or sc < min_buy_score:
            continue
        if str(c.get("action", "")).upper() in ("SELL", "CLOSE", "TRIM"):
            continue
        sym = normalize_symbol(c.get("symbol", ""))
        ref = c.get("reference_price") or c.get("latest_price")
        if not sym or not ref:
            continue
        pos = held.get(sym)
        is_held = pos is not None
        if is_held and float(pos.get("unrealized_plpc", 0) or 0) < 0:
            continue  # don't average down a losing held position
        qualifying.append((sc, is_held, sym, float(ref), c))

    if not qualifying:
        return []
    # Prefer new symbols, then highest score.
    qualifying.sort(key=lambda t: (t[1], -t[0]))
    sc, is_held, sym, ref, c = qualifying[0]
    conv = conviction_factor(sc, default=0.30)  # aggressive profile (shared single source)
    alloc_pct = float(symbol_limits.get(sym, load_risk().get("default_unlisted_max_alloc_pct", 10)))
    qty = round((equity * (alloc_pct / 100.0) * conv) / ref, 8)
    if qty <= 0:
        return []
    order = {
        "symbol": sym, "side": "buy", "qty": qty,
        "limit_price": round(ref * 1.002, 5 if is_crypto(sym) else 2),
        "score": sc, "conviction": sc,
        "setup_type": c.get("setup_type") or ("crypto_scored" if is_crypto(sym) else "research_autobuy"),
        "reasoning": f"[research auto-exec] score {sc} >= {min_buy_score} bar but model held; "
                     f"forcing entry for data. {str(c.get('reasoning',''))[:140]}",
    }
    log.info(f"Research auto-buy: {sym} qty {qty} @ {order['limit_price']} (score {sc}, "
             f"{'add-green' if is_held else 'new'})")
    return [order]


def _load_context() -> dict:
    """
    Load regime + performance context to inject into every Claude prompt.
    This is the continuous learning loop — Claude reads its own history.
    """
    ctx = {}
    try:
        ctx["regime_brief"] = get_regime_brief()
        ctx["regime"]       = detect_regime()
    except Exception as e:
        ctx["regime_brief"] = f"Regime detection failed: {e}"
        ctx["regime"]       = {"regime": "NEUTRAL", "multiplier": 0.6, "stop_loss_pct": -4.0}

    try:
        ctx["performance_brief"] = get_performance_brief()
        ctx["recommendations"]   = get_strategy_recommendations()
    except Exception:
        ctx["performance_brief"] = ""
        ctx["recommendations"]   = ""

    try:
        if INTELLIGENCE_ENABLED:
            refresh_intelligence()
        ctx["intelligence_brief"] = get_intelligence_brief()
    except Exception as e:
        log.warning(f"Intelligence refresh failed: {e}")
        ctx["intelligence_brief"] = ""

    # Small-account ($100 -> $1,000) rehearsal context — injected into every prompt
    try:
        la = load_live_account()
        on = la.get("enabled")
        ctx["live_brief"] = (
            "=== LIVE-ACCOUNT REHEARSAL ($100 -> $1,000 in 30 days) ===\n"
            f"This paper run rehearses a REAL ${la.get('starting_capital',100):.0f} -> "
            f"${la.get('target_capital',1000):.0f} challenge. Even though paper equity shows ~$96k, "
            "bias selection toward setups that survive at tiny capital: fractional-friendly instruments "
            "(crypto, low-priced shares), reward:risk >= 2:1, and avoid names where one share is a huge "
            "fraction of a $100 account. When small, 1-2 high-conviction ideas beat many tiny ones. "
            + ("LIVE-SIM SIZING IS ON — orders are scaled down to the real small account."
               if on else
               "LIVE-SIM SIZING is OFF — paper sizes on full equity, but still weigh the small-account reality in selection.")
        )
        ctx["performance_brief"] = (ctx.get("performance_brief") or "") + "\n\n" + ctx["live_brief"]
    except Exception:
        ctx["live_brief"] = ""

    try:
        ctx["discovery_brief"] = get_discovery_brief()
    except Exception:
        ctx["discovery_brief"] = ""
    try:
        import watchlist_manager
        _wb = watchlist_manager.brief()
        if _wb:
            ctx["discovery_brief"] = (ctx.get("discovery_brief", "") + "\n\n" + _wb).strip()
    except Exception:
        pass

    # Market-research brief (hourly free-source synthesis) — macro/sector/crypto
    # context + concrete strategy adjustments. Injected into every decision prompt.
    try:
        ctx["market_research_brief"] = get_market_research_brief()
        if ctx["market_research_brief"]:
            ctx["performance_brief"] = (ctx.get("performance_brief") or "") + "\n\n" + ctx["market_research_brief"]
    except Exception:
        ctx["market_research_brief"] = ""

    # Free USD-strength macro signal (Frankfurter/ECB FX via public_data) — risk-on/off context.
    try:
        import public_data
        _mb = public_data.macro_brief()
        if _mb:
            ctx["performance_brief"] = (ctx.get("performance_brief") or "") + "\n" + _mb
    except Exception:
        pass

    # Weekday options chain — only on trading weekdays, only when options are enabled, and
    # only during/near equity hours (options can't trade when the market is closed). Lives
    # in its own ctx key so it's injected into the EQUITY decision prompts (research /
    # trading / cycle) but NOT the 24/7 crypto cycle. Each fetch hits the chain for the
    # configured underlyings, so it's gated tightly to avoid needless API calls.
    try:
        if options_enabled() and now_mt_dt().weekday() < 5 and market_phase() in ("REGULAR", "PRE_MARKET"):
            from options import options_brief as _opt_brief
            ctx["options_brief"] = _opt_brief() or ""
        else:
            ctx["options_brief"] = ""
    except Exception as e:
        log.warning(f"Options brief unavailable: {e}")
        ctx["options_brief"] = ""

    # Active trading STRATEGY directive — swing/quality (replaces the old day-trade
    # churn). Injected into every decision prompt. This is the post-redesign core:
    # the prior force-trade/day-trade approach ran -3%/trade (6.7% win rate).
    try:
        if swing_mode_active() or research_mode_active():
            caps = get_effective_caps()
            min_score = caps.get("min_buy_score", 6)
            ts = trading_style()
            stop = ts.get("swing_stop_pct", -3.0)
            min_rr = ts.get("min_reward_risk", 2.0)
            partial = ts.get("partial_profit_pct", 10.0)
            trail_arm = ts.get("trail_arm_pct", 5.0)
            trail_give = ts.get("trail_giveback_pct", 4.0)
            ctx["strategy_brief"] = (
                "=== ACTIVE STRATEGY: SWING / POSITION TRADING (overrides the old day-trade SOP) ===\n"
                "GOAL: flip expectancy strongly positive, then let winners compound toward $100->$1,000. "
                "The previous day-trade-churn approach lost -3%/trade (6.7% win rate) by cutting winners "
                "and riding losers. Trade the OPPOSITE way:\n"
                f"1. QUALITY OVER QUANTITY — 'no trade' is a winning decision. Only enter on a "
                f"MOMENTUM-CONFIRMED setup scoring >= {min_score}/10: a BULLISH squeeze RELEASE (not an "
                "active/coiling or bearish squeeze), MACD bullish, price RECLAIMING/above VWAP, rising "
                "OBV, and a volume pickup, in an up-trending name (EMA9>EMA21>EMA50). Do NOT buy "
                "'extreme fear' dips, falling knives, coiling squeezes, or below-VWAP weakness. If "
                "nothing confirms, hold cash — that is correct.\n"
                f"2. REWARD:RISK >= {min_rr}:1 REQUIRED. Define entry, stop, and target before entering; "
                "skip anything that doesn't clear it.\n"
                "3. NO FORCED INTRADAY FLATTEN. This is SWING trading — HOLD positions multi-day/overnight "
                "while the thesis and trend are intact. Do NOT close winners just because it is late in "
                "the session. Let winners RUN.\n"
                f"4. LET WINNERS RUN, CUT LOSERS FAST. Hard stop at {stop:.0f}% (tighter than the regime "
                f"-5%) — sell a loser immediately at {stop:.0f}%, no averaging down, ever. On winners: take "
                f"PARTIAL profit (~half) near +{partial:.0f}%, then TRAIL the rest — once up +{trail_arm:.0f}% "
                f"give back at most {trail_give:.0f}% from the peak before exiting. The runner is where the "
                "compounding comes from.\n"
                f"5. CONCENTRATE — at most {caps.get('max_open_positions', 5)} positions; put real size on the "
                "1-3 best ideas rather than scattering tiny lots. Quality names, bigger conviction.\n"
                "6. INVERSE ETFs = DOWNSIDE TRADES, NOT BULL-MARKET DIP-FADES. When the broad tape is CONFIRMED "
                "bearish AND the headline regime is NOT BULL (NEUTRAL/BEAR/PANIC), an inverse ETF (SOXS for "
                "semis/tech, SQQQ for the Nasdaq; UVXY intraday only) is a valid momentum trade — it rises as the "
                "market falls. But the TRADE LOG is explicit: buying SOXS/SQQQ as a 'momentum long' to fade a "
                "one-day dip in a BULL tape, then swing-holding it, lost -$1,670 at a 0% win rate (SOXS -$1,608 "
                "held ~4 days) — fighting the tape on a -3x DECAYING instrument. So: do NOT buy inverse ETFs in a "
                "BULL regime (code now BLOCKS it), and NEVER swing-hold a leveraged inverse ETF for days — they "
                "lose to volatility decay; if you take one, it is a short-duration trade. In a bull tape with no "
                "confirmed long setup, the correct move is CASH, not a counter-trend inverse.\n"
                "7. CRYPTO = TREND-FOLLOWING ONLY. Buy crypto only when the DAILY trend is up (EMA9>EMA21, "
                "price above) AND momentum confirms (squeeze released bullish / MACD bullish). Never buy a "
                "coiling/bearish squeeze on the 'extreme fear is contrarian' thesis — that lost -$2,190 at "
                "0% win rate.\n"
                "8. HARD RULES UNCHANGED: LIMIT orders only, keep >=5% cash (do NOT front-load the book — "
                "leave dry powder for better setups later), respect the crypto-exposure cap, only order "
                "Alpaca-tradeable symbols."
            )
            expanded = strategy_prompt_brief()
            if expanded:
                ctx["strategy_brief"] += "\n\n" + expanded
            ctx["performance_brief"] = (ctx.get("performance_brief") or "") + "\n\n" + ctx["strategy_brief"]
            # Replace the (now mostly empty) defensive recs with the strategy stance.
            ctx["recommendations"] = (
                "=== STRATEGY STANCE ===\n"
                "Swing/quality mode. The 0% win rate came from forced low-conviction entries and a forced "
                "intraday flatten — both are now removed. Enter only confirmed momentum setups, hold "
                "winners, cut losers fast. Patience and selectivity are the edge."
            )
        else:
            ctx["strategy_brief"] = ""
    except Exception:
        ctx["strategy_brief"] = ""

    return ctx


def _execute_orders(order_data: list, account: dict, positions: list, symbol_limits: dict,
                    regime: dict, setup_types: dict = None,
                    collect_events: list | None = None) -> list:
    """
    Validate and execute a list of order dicts from Claude's JSON block.

    The regime sizing multiplier is applied HERE and only here (the single source
    of truth) — prompts instruct Claude to size at FULL max_allocation and let the
    system scale. Buys are scaled by regime × live-account factor; sells are clamped
    to the held quantity so the book can always de-risk. Logs entries/exits to the
    learning system. Returns the list of orders actually placed.
    """
    orders_placed = []
    risk = load_risk()
    caps = get_effective_caps()
    live = load_live_account()
    real_equity = float(account.get("equity", 100000) or 100000)
    multiplier  = float(regime.get("multiplier", 0.6) or 0.6)
    live_scale  = (float(live.get("starting_capital", 100)) / real_equity) \
        if live.get("enabled") and real_equity else 1.0

    # ── Pre-flight: check daily P&L stop ─────────────────────────────────────
    last_eq = float(account.get("last_equity", real_equity) or real_equity)
    stop_info = check_daily_stop(real_equity, last_eq)
    if stop_info.get("hard_stop"):
        msg = stop_info["message"]
        log.warning(f"Hard stop active — blocking all new entries. {msg}")
        print(f"  🛑 HARD STOP — {msg}")
        _notify("alert", f"🛑 HARD STOP: {msg}")
        # Still process sells (de-risk only)
        order_data = [o for o in order_data if str(o.get("side","buy")).lower() == "sell"]

    elif stop_info.get("soft_stop"):
        msg = stop_info["message"]
        log.warning(f"Soft stop active — blocking new buys. {msg}")
        print(f"  ⚠️ SOFT STOP — {msg}")
        _notify("alert", f"⚠️ SOFT STOP: {msg}")
        order_data = [o for o in order_data if str(o.get("side","buy")).lower() == "sell"]

    # ── Pre-flight: check loss-streak lockout ─────────────────────────────────
    # Research mode (paper) disables the lockout via loss_streak_lockout_enforced().
    if LEARNING_ENABLED and loss_streak_lockout_enforced() and not lockout_manually_cleared():
        locked, lock_msg = is_loss_streak_locked(
            threshold=caps.get("loss_streak_lockout_threshold", 2)
        )
        if locked:
            log.warning(f"Loss-streak lockout — blocking all new entries. {lock_msg}")
            print(f"  🔒 LOCKOUT — {lock_msg}")
            _notify("alert", f"🔒 LOSS-STREAK LOCKOUT: {lock_msg}")
            order_data = [o for o in order_data if str(o.get("side","buy")).lower() == "sell"]

    for order in order_data:
        sym   = normalize_symbol(order.get("symbol", ""))
        side  = str(order.get("side", "buy")).lower()
        qty   = float(order.get("qty", 0) or 0)
        price = float(order.get("limit_price", 0) or 0)

        if not sym or not price:
            print(f"  SKIP — missing symbol or price: {order}")
            if collect_events is not None:
                collect_events.append({
                    "symbol": sym or order.get("symbol", ""),
                    "side": side,
                    "status": "skipped",
                    "message": "missing symbol or price",
                })
            continue

        # ── Require setup_type for buys (blocks untracked entries) ───────────
        setup_type = (setup_types or {}).get(sym) or order.get("setup_type", "")
        if side == "buy" and (not setup_type or setup_type.lower() in ("unknown", "")):
            msg = (f"BUY BLOCKED — {sym} missing required setup_type. "
                   f"Every buy/open order must include a recognized setup_type "
                   f"(e.g. crypto_scored, gap_and_go, ema_vwap_cross). "
                   f"Untracked entries are not permitted.")
            log.warning(f"Untracked entry blocked: {msg}")
            print(f"  REJECTED (untracked): {sym} — {msg}")
            _notify("order_rejected", {**order, "symbol": sym, "qty": qty}, msg)
            # Count untracked entries for EOD report
            daily = get_daily_state()
            update_daily_state(untracked_entry_count=daily.get("untracked_entry_count", 0) + 1)
            if collect_events is not None:
                collect_events.append({
                    "symbol": sym,
                    "side": side,
                    "status": "rejected",
                    "message": msg,
                })
            continue

        # ── Data-driven hard rule: block setups the track record says to STOP ──────
        # learning.get_strategy_recommendations() escalates a dominant loss-source setup
        # to "🛑 STOP SETUP", but that only reaches the model via the prompt — which it
        # has repeatedly ignored (momentum_breakout: 10% WR over 10 trades, -$3,334, the
        # ENTIRE realized drawdown). Promote the STOP to a code guardrail: any setup_type
        # listed in trading_style.disabled_setups cannot open a new position. Proven
        # setups (bb_squeeze_breakout, ema_vwap_cross) are unaffected. Sells are never
        # blocked (de-risking). Re-enable by removing it from the config list.
        if side == "buy":
            try:
                _disabled = {
                    str(s).strip().lower()
                    for s in (trading_style().get("disabled_setups", []) or [])
                }
            except (TypeError, ValueError, AttributeError):
                _disabled = set()
            if setup_type.lower() in _disabled:
                msg = (f"BUY BLOCKED — setup '{setup_type}' is disabled (net-losing track "
                       f"record; see learning STOP-SETUP recommendation). Use a proven setup "
                       f"(bb_squeeze_breakout / ema_vwap_cross). Re-enable in "
                       f"trading_style.disabled_setups once its win rate recovers.")
                log.warning(msg)
                print(f"  REJECTED (disabled-setup): {sym} — {msg}")
                _notify("order_rejected", {**order, "symbol": sym, "qty": qty}, msg)
                if collect_events is not None:
                    collect_events.append({
                        "symbol": sym, "side": side, "status": "rejected", "message": msg,
                    })
                continue

        # ── SOP hard rule: no leveraged LONG ETFs in BEAR/PANIC (was prompt-only) ──
        if side == "buy" and _regime_blocks_leveraged_long(sym, regime.get("regime", "")):
            msg = (f"BUY BLOCKED — {sym} is a leveraged LONG ETF; not permitted in "
                   f"{regime.get('regime')} regime (it decays against a down/panic tape). "
                   f"Use an inverse ETF (SQQQ/SOXS) to trade the downside instead.")
            log.warning(msg)
            print(f"  REJECTED (regime): {sym} — {msg}")
            _notify("order_rejected", {**order, "symbol": sym, "qty": qty}, msg)
            if collect_events is not None:
                collect_events.append({
                    "symbol": sym, "side": side, "status": "rejected", "message": msg,
                })
            continue

        # ── Data-driven hard rule (2026-06-15): no leveraged INVERSE ETFs in BULL ──
        # Fading an up-trending tape with a decaying -3x inverse (SOXS/SQQQ), then
        # swing-holding it, lost -$1,670 at a 0% win rate. Inverse ETFs are downside
        # tools — only buy them when the regime is NOT BULL (NEUTRAL/BEAR/PANIC).
        if side == "buy" and _regime_blocks_inverse_etf(sym, regime.get("regime", "")):
            msg = (f"BUY BLOCKED — {sym} is a leveraged INVERSE ETF; not bought in a "
                   f"{regime.get('regime')} regime. Fading an up-trending tape with a "
                   f"decaying -3x inverse and holding it lost -$1,670 at 0% WR. Trade "
                   f"inverse ETFs only when the regime is not BULL (NEUTRAL/BEAR/PANIC).")
            log.warning(msg)
            print(f"  REJECTED (regime): {sym} — {msg}")
            _notify("order_rejected", {**order, "symbol": sym, "qty": qty}, msg)
            if collect_events is not None:
                collect_events.append({
                    "symbol": sym, "side": side, "status": "rejected", "message": msg,
                })
            continue

        # ── Low-conviction hard gate (SOP: 3-4 = WATCH, 1-2 = HARD SKIP) ──────────
        # conviction_factor() only SHRINKS an under-scored buy (0.30× for <5); it must
        # never let a sub-threshold BUY execute as a small position. If the model attaches
        # an explicit score/conviction BELOW the minimum-to-enter, reject it outright.
        # (Scoreless buys are left to the setup_type gate + conviction clamp — the model is
        # instructed to always score, and we don't want to block legit reconstructed/forced
        # entries that carry no score.)
        if side == "buy":
            _score = order.get("score", order.get("conviction"))
            try:
                _sv = int(round(float(_score))) if _score not in (None, "") else None
            except (TypeError, ValueError):
                _sv = None
            _min_score = int(caps.get("min_buy_score", 6) or 6)
            if _sv is not None and _sv < _min_score:
                msg = (f"BUY BLOCKED — {sym} score {_sv} is below the minimum-to-enter "
                       f"{_min_score} (SOP: 3-4 = WATCH, 1-2 = HARD SKIP). No low-conviction entries.")
                log.warning(msg)
                print(f"  REJECTED (low-score): {sym} — {msg}")
                _notify("order_rejected", {**order, "symbol": sym, "qty": qty}, msg)
                if collect_events is not None:
                    collect_events.append({
                        "symbol": sym, "side": side, "status": "rejected", "message": msg,
                    })
                continue

        # ── Equity/option market-closed gate (holiday/weekend/after-hours) ───────
        # The broker clock is authoritative (market_phase() is time-based and HOLIDAY-BLIND
        # — it called Juneteenth "open"). A non-crypto BUY when equities are closed can't
        # fill; it just rests until the next cycle cancels it as stale, churning API + order
        # spam. Skip it here so EVERY routine is protected. Crypto trades 24/7 — never gated.
        if side == "buy":
            setup_ok, setup_msg = validate_setup_for_entry(setup_type, score=_sv)
            if not setup_ok:
                msg = f"BUY BLOCKED - {sym} setup '{setup_type}' rejected: {setup_msg}"
                log.warning(msg)
                print(f"  REJECTED (strategy-playbook): {sym} - {msg}")
                _notify("order_rejected", {**order, "symbol": sym, "qty": qty}, msg)
                if collect_events is not None:
                    collect_events.append({
                        "symbol": sym, "side": side, "status": "rejected", "message": msg,
                    })
                continue

        if side == "buy" and not _is_crypto(sym) and not trade.equities_open_now():
            msg = (f"BUY SKIPPED — {sym}: equity/options market is closed "
                   f"(broker clock; holiday/weekend/after-hours). Order would not fill.")
            log.info(msg)
            print(f"  SKIP (market closed): {sym}")
            if collect_events is not None:
                collect_events.append({
                    "symbol": sym, "side": side, "status": "skipped", "message": msg,
                })
            continue

        if side == "buy":
            # Per-setup risk multiplier (data-driven; trading_style.setup_size_multiplier).
            # Shrinks historically-losing setups (e.g. momentum_breakout = the entire realized
            # drawdown) before regime/live scaling. Unlisted setups default to 1.0 (no change).
            setup_mult = 1.0
            try:
                _sm = trading_style().get("setup_size_multiplier", {}) or {}
                setup_mult = float(_sm.get(setup_type, 1.0) or 1.0)
            except (TypeError, ValueError):
                setup_mult = 1.0
            scale = multiplier * live_scale * setup_mult
            # Options trade in WHOLE contracts; equities/crypto can be fractional.
            if is_option(sym):
                qty = int(qty * scale)
            else:
                qty = round(qty * scale, 8)
            if setup_mult != 1.0:
                log.info(f"Setup-size multiplier {setup_mult:g} applied to {sym} ({setup_type}) buy")
            if qty <= 0:
                print(f"  SKIP {sym}: scaling produced zero qty")
                if collect_events is not None:
                    collect_events.append({
                        "symbol": sym,
                        "side": side,
                        "status": "skipped",
                        "message": "scaling produced zero qty",
                    })
                continue
            # Crypto: re-price the limit off the LIVE ask so the order is MARKETABLE at
            # submission and fills immediately. The model's limit is built from a lagging
            # bar close; Alpaca paper never re-matches a resting order, so a non-marketable
            # limit rests forever (the stuck-BTC bug). A buy limit fills at the ask, so
            # 0.5% through it = immediate fill with no overpay.
            if _is_crypto(sym):
                try:
                    snap = _get_snapshot(sym) or {}
                    ask = float(snap.get("ask") or snap.get("price") or 0)
                    if ask > 0:
                        price = round(ask * 1.005, 5)
                except Exception:
                    pass
        else:  # sell — clamp to the position we actually hold
            pos = next((p for p in positions
                        if normalize_symbol(p.get("symbol", ""), p.get("asset_class")) == sym), None)
            if pos is not None:
                held = abs(float(pos.get("qty", 0) or 0))
                if qty <= 0 or qty > held:
                    qty = held

        sym_limit = symbol_limits.get(sym, risk.get("default_unlisted_max_alloc_pct", 10))
        regime_adj_limit = sym_limit * multiplier if side == "buy" else 100.0

        # Right-size an oversized BUY to the per-symbol allocation cap instead of
        # rejecting it outright — keeps trades flowing for research/automation while
        # still honoring the cap. (Cash-reserve and crypto-cap checks remain in
        # validate_order, so hard limits can still legitimately block.) Options are
        # exempt — their premium-based caps are enforced in validate_order, and the
        # share-allocation math here doesn't apply to contracts.
        if side == "buy" and price > 0 and not is_option(sym):
            # Right-size to the per-symbol cap on the TOTAL resulting position: subtract
            # the existing LONG exposure so a scale-in fills only the remaining headroom
            # instead of stacking another full-cap tranche (which validate_order now
            # rejects). Shorts (negative mv) count as 0 — a buy there reduces risk.
            existing_long_mv = sum(
                max(0.0, float(p.get("market_value", 0) or 0))
                for p in positions
                if normalize_symbol(p.get("symbol", ""), p.get("asset_class")) == sym
            )
            # (1) HARD per-symbol allocation cap (the symbol's max_allocation_pct × regime).
            cap_value = real_equity * (regime_adj_limit / 100.0)
            headroom = cap_value - existing_long_mv
            if headroom <= 0:
                log.info(f"Skipping {sym} buy — already at/over {regime_adj_limit:.1f}% per-symbol "
                         f"cap (existing ${existing_long_mv:,.2f} >= cap ${cap_value:,.2f})")
                if collect_events is not None:
                    collect_events.append({
                        "symbol": sym, "side": side, "status": "skipped",
                        "message": f"already at {regime_adj_limit:.1f}% per-symbol cap",
                    })
                continue
            max_qty = headroom / price
            if qty > max_qty:
                clamped = round(max_qty * 0.99, 8)
                log.info(f"Clamping {sym} buy qty {qty} -> {clamped} to fit "
                         f"{regime_adj_limit:.1f}% per-symbol cap")
                if collect_events is not None:
                    collect_events.append({
                        "symbol": sym, "side": side, "status": "clamped",
                        "message": f"qty {qty} -> {clamped} to fit {regime_adj_limit:.1f}% cap",
                    })
                qty = clamped

            # (2) DETERMINISTIC CONVICTION/SCORE SIZING CAP — the model is told to size at
            #     equity × max_alloc × conviction_factor(score); enforce that factor here so
            #     a JSON qty that ignores its own conviction sizing can't place an oversized
            #     position (the TQQQ score-6 order that reasoned "qty=257" but emitted 461).
            #     The hard alloc cap above stays the outer bound; this is a tighter, soft
            #     sizing cap that NEVER raises qty. No usable score -> 1.0 (no reduction).
            conv = conviction_factor(order.get("score", order.get("conviction")), default=1.0)
            if conv is not None and conv < 1.0 - 1e-9:
                conv_qty, conv_headroom = trade.deterministic_position_qty_cap(
                    price, real_equity, sym_limit, regime_mult=multiplier,
                    conviction_factor=conv, existing_long_mv=existing_long_mv,
                )
                score_val = order.get("score", order.get("conviction"))
                if conv_qty <= 0:
                    log.info(
                        f"SIZING SKIP {sym} buy — existing ${existing_long_mv:,.2f} already fills the "
                        f"score-{score_val} conviction allocation (conv={conv:.2f} × "
                        f"{regime_adj_limit:.1f}% cap); no headroom for an add."
                    )
                    if collect_events is not None:
                        collect_events.append({
                            "symbol": sym, "side": side, "status": "skipped",
                            "message": (f"conviction sizing (score {score_val}, conv {conv:.2f}) "
                                        f"leaves no headroom over existing ${existing_long_mv:,.0f}"),
                        })
                    continue
                if qty > conv_qty + 1e-9:
                    clamped = round(conv_qty, 8)
                    req_notional = qty * price
                    det_notional = conv_qty * price
                    log.warning(
                        f"SIZING CLAMP {sym} buy: requested qty {qty:g} (${req_notional:,.0f}) exceeds "
                        f"deterministic conviction cap {clamped:g} (${det_notional:,.0f}) "
                        f"[score={score_val}, conv={conv:.2f}, max_alloc={sym_limit:g}%, regime_mult={multiplier:g}]; "
                        f"clamping to {clamped:g}."
                    )
                    if collect_events is not None:
                        collect_events.append({
                            "symbol": sym, "side": side, "status": "clamped",
                            "message": (f"qty {qty:g} -> {clamped:g} to fit score-{score_val} conviction "
                                        f"sizing (conv {conv:.2f} × {sym_limit:g}% × regime {multiplier:g})"),
                        })
                    qty = clamped

        # Get position P&L for duplicate-entry check
        pos_obj = next((p for p in positions
                        if normalize_symbol(p.get("symbol",""), p.get("asset_class")) == sym), None)
        pos_pnl = float(pos_obj.get("unrealized_plpc", 0) or 0) * 100 if pos_obj else None

        ok, msg = trade.validate_order(
            sym, qty, side, price, account, positions,
            regime_adj_limit, risk,
            position_pnl_pct=pos_pnl,
            completed_trades=get_completed_trade_count() if LEARNING_ENABLED else None,
        )
        if not ok:
            log.warning(f"Order rejected: {msg} — {order}")
            print(f"  REJECTED: {side.upper()} {sym} — {msg}")
            _notify("order_rejected", {**order, "symbol": sym, "qty": qty}, msg)
            if collect_events is not None:
                collect_events.append({
                    "symbol": sym,
                    "side": side,
                    "status": "rejected",
                    "message": msg,
                })
            continue

        if kill_active():
            log.warning("Kill switch detected mid-session — halting.")
            _notify("alert", "Kill switch detected mid-session — orders halted.")
            if collect_events is not None:
                collect_events.append({
                    "symbol": sym,
                    "side": side,
                    "status": "halted",
                    "message": "kill switch detected mid-session",
                })
            break

        log.info(f"Placing order: {side} {qty} {sym} @ {price} (mult={multiplier}, live_scale={live_scale})")
        result = trade.place_order(
            sym,
            qty,
            side,
            price,
            intent_key=f"{setup_type}|{order.get('score', order.get('conviction', ''))}",
            intent_context={
                "learning_managed": True,
                "setup_type": setup_type or "unknown",
                "conviction": order.get("conviction", order.get("score", 5)),
                "signals": order.get("signals", {}),
                "regime": regime.get("regime", "NEUTRAL"),
                "vix": regime.get("vix"),
                "notes": order.get("reasoning", ""),
                "exit_reason": order.get("exit_reason") or order.get("setup_type", "sell"),
            },
        )
        if isinstance(result, dict) and result.get("error"):
            err = str(result["error"])
            ambiguous = bool(result.get("ambiguous"))
            # Non-tradeable symbol (common with exotic discovery tickers) — skip
            # quietly instead of firing a scary "rejected" alert.
            not_tradeable = any(s in err.lower() for s in
                                ("not active", "not found", "not tradable",
                                 "not tradeable", "is not allowed", "asset not"))
            # Non-tradeable symbols are an expected discovery artifact, not an error —
            # log at WARNING so they don't inflate the diagnostic ERROR count.
            if ambiguous:
                log.warning(f"Order outcome unresolved: {err} — {order}")
                print(f"  UNRESOLVED: {side.upper()} {sym} — {err}")
                _notify(
                    "alert",
                    f"Order outcome unresolved for {sym}; no retry will be submitted until reconciliation. {err}",
                )
            else:
                (log.warning if not_tradeable else log.error)(f"Order failed: {err} — {order}")
                print(f"  {'SKIP (not tradeable)' if not_tradeable else 'FAILED'}: {side.upper()} {sym} — {err}")
            if not not_tradeable and not ambiguous:
                _notify("order_rejected", {**order, "symbol": sym, "qty": qty}, f"Broker error: {err}")
            if collect_events is not None:
                collect_events.append({
                    "symbol": sym,
                    "side": side,
                    "status": "unresolved" if ambiguous else (
                        "not_tradeable" if not_tradeable else "broker_error"
                    ),
                    "message": err,
                })
            continue

        placed = {**order, "symbol": sym, "qty": qty, "alpaca_response": result}
        orders_placed.append(placed)
        _notify("trade_placed", {**order, "symbol": sym, "qty": qty}, result)
        print(f"  ORDER: {side.upper()} {qty} {sym} @ ${price}")

        event_status = "placed"
        event_message = "order accepted"
        if side == "buy":
            # Confirm the fill before tracking: poll the order briefly so the learning
            # log records the REAL filled qty/avg price, not the requested values
            # (fixes partial-fill qty drift). Accepted-but-unfilled orders stay in the
            # execution ledger and are reconciled on the next routine. Requested qty is
            # never treated as a fill.
            fill_qty, fill_px = 0.0, None
            fill_status = str(result.get("status") or "unknown")
            order_id = result.get("id") if isinstance(result, dict) else None
            if order_id:
                try:
                    fill_qty, fill_px, fill_status = trade.get_order_fill(order_id)
                except Exception:
                    pass
            if fill_qty > 0:
                fill_px = fill_px or float(price)
                record_session_entry(sym, is_green=False)
                log_entry(
                    symbol=sym, side=side, qty=fill_qty, price=fill_px,
                    setup_type=setup_type or "unknown",
                    conviction=order.get("conviction", 5),
                    signals=order.get("signals", {}),
                    regime=regime.get("regime", "NEUTRAL"),
                    vix=regime.get("vix"),
                    notes=order.get("reasoning", ""),
                )
                client_id = result.get("client_order_id")
                if client_id:
                    trade.ledger.mark_learning_applied(client_id, fill_qty, fill_px)
                event_status = "filled" if fill_qty >= float(qty) - 1e-9 else "partially_filled"
                event_message = f"filled {fill_qty:g} @ ${fill_px}"
                placed["filled_qty"] = fill_qty
                placed["filled_avg_price"] = fill_px
                placed["fill_status"] = fill_status
            else:
                event_status = "placed_unfilled"
                event_message = f"order accepted but not filled yet ({fill_status})"
                placed["fill_status"] = fill_status
                log.warning(f"Buy order accepted but unfilled: {sym} {qty} @ {price} ({fill_status})")
        else:
            filled_qty, fill_px, fill_status = _confirm_order_fill(result, qty, price)
            if filled_qty > 0:
                event_status = "filled" if filled_qty >= float(qty) - 1e-9 else "partially_filled"
                event_message = f"filled {filled_qty:g} @ ${fill_px}"
                placed["filled_qty"] = filled_qty
                placed["filled_avg_price"] = fill_px
                placed["fill_status"] = fill_status
                try:
                    log_exit(
                        sym,
                        fill_px,
                        exit_reason=order.get("exit_reason") or order.get("setup_type", "sell"),
                        notes=order.get("reasoning", ""),
                        qty=filled_qty,
                    )
                    client_id = result.get("client_order_id") if isinstance(result, dict) else None
                    if client_id:
                        trade.ledger.mark_learning_applied(client_id, filled_qty, fill_px)
                except Exception:
                    pass
            else:
                event_status = "placed_unfilled"
                event_message = f"order accepted but not filled yet ({fill_status})"
                placed["fill_status"] = fill_status
                log.warning(f"Sell order accepted but unfilled: {sym} {qty} @ {price} ({fill_status})")
        if collect_events is not None:
            collect_events.append({
                "symbol": sym,
                "side": side,
                "status": event_status,
                "message": event_message,
            })

    return orders_placed


def _apply_reconciled_fills() -> list:
    """Apply delayed or incremental broker fills to learning exactly once."""
    if not LEARNING_ENABLED:
        return []
    applied = []
    for fill in trade.ledger.get_unapplied_fills():
        context = fill.get("context") or {}
        if not context.get("learning_managed"):
            continue
        qty = float(fill.get("delta_qty") or 0)
        price = float(fill.get("delta_price") or fill.get("filled_avg_price") or 0)
        if qty <= 0 or price <= 0:
            continue
        symbol = normalize_symbol(fill.get("symbol", ""))
        side = str(fill.get("side") or "").lower()
        try:
            if side == "buy":
                log_entry(
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    price=price,
                    setup_type=context.get("setup_type") or "reconciled_entry",
                    conviction=context.get("conviction", 5),
                    signals=context.get("signals") or {},
                    regime=context.get("regime") or "NEUTRAL",
                    vix=context.get("vix"),
                    notes=context.get("notes") or "Delayed fill applied during reconciliation",
                )
                record_session_entry(symbol, is_green=False)
            elif side == "sell":
                log_exit(
                    symbol,
                    price,
                    exit_reason=context.get("exit_reason") or "reconciled_exit",
                    notes=context.get("notes") or "Delayed fill applied during reconciliation",
                    qty=qty,
                )
            else:
                continue
            trade.ledger.mark_learning_applied(
                fill["client_order_id"],
                float(fill.get("filled_qty") or qty),
                float(fill.get("filled_avg_price") or price),
            )
            applied.append({"symbol": symbol, "side": side, "qty": qty, "price": price})
            log.info(f"Applied reconciled fill to learning: {side} {qty:g} {symbol} @ {price:g}")
        except Exception as exc:
            log.exception(f"Could not apply reconciled fill {fill.get('client_order_id')}: {exc}")
    return applied


def _cancel_stale_orders(positions=None) -> list:
    """
    Cancel orders resting unfilled past the stale threshold, then drop orphaned
    entry-tracking for any cancelled BUY that never became a live position.

    A buy is logged to open_trades.json on broker ACCEPTANCE (not fill), so an
    accepted-then-cancelled limit would otherwise be fabricated into a phantom
    round-trip exit by detect_and_log_exits() once it ages past the recency guard.
    Reconciling here closes that orphan-entry loop (trade.py stays free of any learning
    import; the orchestrator owns the cross-module reconciliation).

    Returns the human-readable descriptions of cancelled orders (for logging).
    """
    cancelled = trade.cancel_stale_orders()
    if not cancelled:
        return []
    held = {normalize_symbol(p.get("symbol", ""), p.get("asset_class"))
            for p in (positions or [])}
    for c in cancelled:
        if (isinstance(c, dict) and c.get("side") == "buy"
                and float(c.get("filled_qty") or 0) <= 0
                and str(c.get("status") or "canceled").lower() in ("canceled", "cancelled")):
            try:
                forget_unfilled_entry(c.get("symbol"), held)
            except Exception:
                pass
    return [c.get("desc", str(c)) if isinstance(c, dict) else str(c) for c in cancelled]


# ── Data gathering ─────────────────────────────────────────────────────────────

def _data_universe(crypto_only: bool = False) -> list:
    """Static watchlist entries PLUS auto-promoted dynamic symbols, so a name the
    marketwide scanner promoted gets ANALYZED with full indicators — not merely listed in
    the discovery brief. Bounded by discovery.max_analyzed_dynamic to keep the fetch cheap.
    Sizing/risk for these non-watchlist names is already handled by validate_order's
    default discovery cap, so they only need a type + the default alloc here."""
    wl = load_watchlist()
    entries = list(wl.get("watchlist", []))
    have = {normalize_symbol(s.get("symbol", "")) for s in entries}
    try:
        import watchlist_manager
        disc = wl.get("discovery", {}) or {}
        default_alloc = disc.get("default_max_alloc_pct", 12)
        max_dyn = int(disc.get("max_analyzed_dynamic", 6) or 0)
        for sym in (watchlist_manager.active_symbols() or [])[:max_dyn]:
            if not sym or normalize_symbol(sym) in have:
                continue
            have.add(normalize_symbol(sym))
            entries.append({
                "symbol": sym,
                "type": "crypto" if is_crypto(sym) else "equity",
                "max_allocation_pct": default_alloc,
                "regimes": ["BULL", "NEUTRAL"],
                "strategies": [],
                "source": "auto_discovery",
            })
    except Exception as e:
        log.warning(f"Dynamic-watchlist merge skipped: {e}")
    if crypto_only:
        return [s for s in entries if is_crypto(s.get("symbol", ""))]
    return entries


def gather_market_data() -> dict:
    """Pull all data for every watchlist symbol (+ auto-promoted discoveries) + macro."""
    watchlist = load_watchlist()
    research  = {}

    for s in _data_universe():
        symbol = s["symbol"]
        log.info(f"Fetching: {symbol}")
        bars     = safe_run("research.py", "bars", symbol)
        if bars.get("error"):
            log.warning(f"Skipping {symbol} — bars fetch failed: {bars['error']}")
            continue
        intraday = safe_run("research.py", "intraday", symbol)
        intraday_bars = intraday.get("bars") or []
        hourly   = safe_run("research.py", "hourly", symbol)
        alpaca_news = safe_run("research.py", "news", symbol)

        raw_news = safe_run("enrichment.py", "news", symbol)
        if isinstance(raw_news, list):
            raw_articles = raw_news
        elif isinstance(raw_news, dict):
            raw_articles = raw_news.get("articles") or raw_news.get("news") or []
        else:
            raw_articles = []
        news_summary = summarize_news(raw_articles, symbol)

        entry = {
            "type":            s.get("type", "equity"),
            "max_alloc_pct":   s.get("max_allocation_pct", 10),
            "regime_filter":   s.get("regimes", []),
            "strategies":      s.get("strategies", []),
            "moving_averages": bars.get("moving_averages", {}),
            "recent_bars":     (bars.get("bars") or [])[-3:],    # 3 bars instead of 5
            "intraday_vwap":   intraday.get("vwap"),
            "intraday_bars":   intraday_bars[-6:],               # 6 bars instead of 12
            "hourly_indicators": hourly.get("indicators", {}),
            "latest_price":    intraday_bars[-1]["c"] if intraday_bars else bars.get("moving_averages", {}).get("latest_close"),
            "alpaca_news":     [{"headline": n["headline"]} for n in (alpaca_news.get("news") or [])[:2]],
            "news_summary":    news_summary,                     # Haiku-compressed news
        }
        if not is_crypto(symbol):
            entry["fundamentals"]      = safe_run("enrichment.py", "fundamentals", symbol)
            entry["sentiment"]         = safe_run("enrichment.py", "sentiment", symbol)
            entry["earnings_calendar"] = safe_run("enrichment.py", "earnings", symbol)
            # sec_filings removed from research — adds ~500 tokens per equity, low signal value
        research[symbol] = entry

    account   = run("research.py", "account")
    positions = get_positions_norm()
    macro     = safe_run("enrichment.py", "macro")
    fear_greed = safe_run("enrichment.py", "feargreed")
    crypto_fg  = safe_run("enrichment.py", "cryptofg")
    coingecko  = safe_run("enrichment.py", "coingecko")
    funding    = safe_run("enrichment.py", "funding")
    social     = safe_run("enrichment.py", "wsb")

    return {
        "date":              today(),
        "account":           account,
        "positions":         positions,
        "watchlist":         watchlist,
        "macro_context":     macro,
        "fear_greed_index":  fear_greed,
        "crypto_fear_greed": crypto_fg,
        "coingecko_global":  coingecko,
        "funding_rates":     funding,
        "social_sentiment":  social,
        "research":          research,
    }


def gather_crypto_data() -> dict:
    """Pull full indicator suite for crypto symbols (+ auto-promoted crypto discoveries)."""
    account   = run("research.py", "account")
    positions = get_positions_norm()
    research  = {}

    for s in _data_universe(crypto_only=True):
        symbol = s["symbol"]
        if not is_crypto(symbol):
            continue
        log.info(f"Crypto fetch: {symbol}")
        bars     = safe_run("research.py", "bars", symbol)
        if bars.get("error"):
            log.warning(f"Skipping {symbol} — bars failed: {bars['error']}")
            continue
        hourly   = safe_run("research.py", "hourly", symbol)
        intraday = safe_run("research.py", "intraday", symbol)
        intraday_bars = intraday.get("bars") or []
        raw_news = safe_run("enrichment.py", "news", symbol)
        if isinstance(raw_news, list):
            raw_articles = raw_news
        elif isinstance(raw_news, dict):
            raw_articles = raw_news.get("articles") or raw_news.get("news") or []
        else:
            raw_articles = []
        news_summary = summarize_news(raw_articles, symbol)

        # Indicators-only payload — no raw bar lists in crypto cycles (saves ~8k tokens/call)
        daily = bars.get("moving_averages", {})
        hourly_ind = hourly.get("indicators", {})
        research[symbol] = {
            "max_allocation_pct": s["max_allocation_pct"],
            # Key indicators only (not full bar arrays)
            "ema9":           daily.get("ema9"),
            "ema21":          daily.get("ema21"),
            "ema9_1h":        hourly_ind.get("ema9_1h"),
            "ema21_1h":       hourly_ind.get("ema21_1h"),
            "rsi14":          daily.get("rsi14"),
            "rsi14_1h":       hourly_ind.get("rsi14_1h"),
            # calc_moving_averages nests these — macd/obv/volume_spike are dicts, NOT
            # flat "*_signal"/"*_trend"/"*_ratio" keys. The old flat lookups always read
            # None, so the crypto SCORED system (Strategy 9) saw null MACD (+2), OBV (+1),
            # and volume-spike (+1) every cycle — up to 4 scoring points silently dropped.
            "macd_signal":    (daily.get("macd") or {}).get("crossover"),
            "bb_squeeze":     daily.get("bb_squeeze", {}),
            "obv_trend":      (daily.get("obv") or {}).get("obv_trend"),
            "volume_spike":   (daily.get("volume_spike") or {}).get("ratio"),
            "atr14":          daily.get("atr14"),
            "intraday_vwap":  intraday.get("vwap"),
            "latest_price":   intraday_bars[-1]["c"] if intraday_bars else daily.get("latest_close"),
            "news_summary":   news_summary,
        }

    return {
        "timestamp":         now_mt(),
        "account":           account,
        "positions":         [p for p in positions if is_crypto(p.get("symbol", ""), p.get("asset_class"))],
        "all_positions":     positions,
        "crypto_fear_greed": safe_run("enrichment.py", "cryptofg"),
        "coingecko_global":  safe_run("enrichment.py", "coingecko"),
        "funding_rates":     safe_run("enrichment.py", "funding"),
        "research":          research,
    }


# ── Routine 1: Morning Research (7:45 AM MT) ──────────────────────────────────

def run_research():
    log.info("=== Morning Research started ===")

    # Auto-updating watchlist: promote recurring marketwide-discovery winners, demote
    # stale ones, and post changes to #ft-watchlist — the agent's universe grows itself.
    try:
        import watchlist_manager
        watchlist_manager.run_and_post()
    except Exception as e:
        log.warning(f"Watchlist auto-update failed: {e}")

    ctx = _load_context()
    regime = ctx["regime"]

    status = safe_run("trade.py", "status")
    if not status.get("is_open"):
        log.info("Market closed — running crypto-only research.")

    data = gather_market_data()
    positions = data["positions"]

    # Detect exits from last session
    closed = detect_and_log_exits(positions, exit_reason="overnight_or_prior_session")
    if closed:
        log.info(f"Detected closed positions: {closed}")

    prompt = f"""Run the morning research routine. Today is {today()}.

{ctx['regime_brief']}

{ctx['performance_brief']}

{ctx['recommendations']}

{ctx.get('intelligence_brief', '')}

{ctx.get('discovery_brief', '')}

{ctx.get('options_brief', '')}

Market data:
{json.dumps(data, indent=2)}

Write a complete journal entry. Format:

# Trade Journal — {today()}

## Regime & Context
- Current regime: {regime.get('regime')} | Sizing multiplier: {regime.get('multiplier', 0.6)*100:.0f}% of normal
- Performance context: (summarize key stats from brief above)
- Strategy adjustments: (summarize recommendations above)

## Portfolio Status
- Cash, equity, P&L, open positions. Flag any position down {abs(regime.get('stop_loss_pct', -5)):.0f}%+ (stop-loss required immediately).

## Market Sentiment
- CNN Fear & Greed score, trend, and implication
- Crypto Fear & Greed + BTC dominance + CoinGecko trending
- VIX level and volatility regime interpretation
- Funding rates: flag EXTREME_LONG (fade risk) or EXTREME_SHORT (squeeze setup)
- Macro: Fed rate, 10Y yield, CPI, unemployment — risk asset bias today?

## Symbol Analysis
For EVERY watchlist symbol that is valid in the current regime, run through:
- **Price action**: latest price, gap %, ATR14 expected range
- **Trend**: EMA9 vs EMA21, price vs VWAP, pivot point position
- **Momentum**: RSI14 zone, MACD crossover signal, Bollinger Band position
- **Volume**: spike ratio — confirming or diverging?
- **OBV**: confirming or diverging from price?
- **BB Squeeze**: squeeze active? Release direction?
- **Fibonacci**: nearest support/resistance level — how close?
- **News/catalyst**: earnings risk? Major catalyst?
- **Setup rating**: HIGH / MEDIUM / LOW / SKIP with quantified signal count (X/10)

Skip symbols NOT in regime's preferred instrument list — note why.

## Top Setups Ranked
Top 2-3 setups with:
1. Setup type (from SOP)
2. Entry trigger (specific price or condition)
3. Target price (+X%) and stop price (-X%, per regime rules)
4. Position size formula: equity × max_alloc_pct × conviction_factor / entry_price
   (size at FULL allocation — the system automatically scales every order by the
    {regime.get('regime')} regime multiplier of {regime.get('multiplier', 0.6)*100:.0f}%. Do NOT pre-multiply.)
5. Signal count and conviction score (1-10)
6. Risk/reward ratio (minimum 2:1 required)

Be specific. Every number must appear. Vague analysis is useless."""

    analysis = ask_model(prompt, load_system(), routine="research")
    write_journal_text(analysis)
    _notify_research_outputs(analysis, ctx, regime)
    log.info(f"Research written → {journal_path()}")
    print(f"Research complete → {journal_path()}")


# ── Routine 2: Trading Session (8:00 AM MT) ───────────────────────────────────

def run_trading():
    log.info("=== Trading Session started ===")

    if kill_active():
        msg = "Kill switch active — trading session skipped."
        log.warning(msg)
        _notify("alert", msg)
        print(msg)
        return

    ctx = _load_context()
    regime = ctx["regime"]

    status = safe_run("trade.py", "status")
    if not status.get("is_open"):
        log.info("Market closed — equities only skipped; crypto continues.")

    if not journal_path().exists():
        log.error("No research journal found.")
        print("ERROR: No journal — run research routine first.")
        sys.exit(1)

    account   = run("research.py", "account")
    positions = get_positions_norm()
    watchlist = load_watchlist()
    symbol_limits = {s["symbol"]: s["max_allocation_pct"] for s in watchlist["watchlist"]}

    # Enforce swing exits BEFORE the model decides — via the SAME single-source logic every
    # other routine uses (_manage_swing_exits: hard stop at swing_stop_pct ≈ -3%, trailing
    # stop, partial profit). The old bespoke loop here cut only at the looser REGIME stop
    # (-5% in BULL), so a -4% loser would be cut by the 15-min cycle but ridden by the
    # morning session — exactly the drift this removes.
    exit_actions, _ = _manage_swing_exits(positions, note_prefix="Trading ")
    if exit_actions:
        for a in exit_actions:
            print(f"  {a}")
        log.info(f"Swing exits (trading session): {exit_actions}")
        positions = get_positions_norm()
        account   = run("research.py", "account")

    # Detect exits from learning system
    detect_and_log_exits(positions, exit_reason="pre_session_check")

    prompt = f"""Run the trading session. Today is {today()}.

{ctx['regime_brief']}

{ctx['performance_brief']}

{ctx['recommendations']}

{ctx.get('intelligence_brief', '')}

{ctx.get('options_brief', '')}

Morning research:
{read_journal_text()}

Current account:
{json.dumps(account, indent=2)}

Current positions:
{json.dumps(positions, indent=2)}

Watchlist + limits:
{json.dumps(watchlist, indent=2)}

=== TRADING INSTRUCTIONS ===

Regime: {regime.get('regime')}. SIZE AT FULL max_allocation_pct × conviction_factor.
The system AUTOMATICALLY scales every order by the {regime.get('multiplier', 0.6)*100:.0f}% regime multiplier — do NOT pre-multiply or you will under-size.
Stop-loss: swing hard stop {swing_stop_pct():.0f}% (tighter than the regime {regime.get('stop_loss_pct', -5.0):.0f}%) — already code-enforced above on existing positions via the swing-exit manager (hard stop + trailing stop + partial).
"No trade" is a valid, fully-acceptable outcome. Only act on genuine 3+ signal setups.

For each symbol in today's research:
1. Check if it qualifies under current regime (see preferred_instruments).
2. If HIGH/MEDIUM setup: decide BUY with specific trigger, target, stop.
3. If LOW/SKIP: explicitly state why with the signal count and blockers.
4. Position size (qty) = equity × max_alloc_pct × conviction_factor / entry_price. (system applies regime multiplier)
5. Limit order only — within 0.2% of ask.
6. SWING trading — HOLD winners multi-day/overnight while the trend and thesis hold; do NOT flatten at 1:45 PM. Exit a position only on its stop, a trailing-stop give-back, or a thesis break.
7. Minimum risk/reward: {trading_style().get('min_reward_risk', 2.0):g}:1. Define entry, stop, and target before entering.
8. Every researched symbol MUST appear once in "candidates" with action BUY, HOLD, WATCH, or SKIP.

For crypto (24/7): use time_in_force=gtc, fractional qty, scored system applies.

Output requirements:
- Start with the JSON block immediately.
- Do not put headings, tables, or prose before the JSON.
- After the JSON, give at most 8 short bullets total. No markdown tables.

Return JSON block FIRST:
```json
{{
  "summary": "2-3 sentence summary of today's best setups, skips, and risk posture.",
  "orders": [
    {{
      "symbol": "TQQQ",
      "qty": 12,
      "side": "buy",
      "limit_price": 52.15,
      "setup_type": "gap_and_go",
      "conviction": 8,
      "signals": {{"ema_bullish": true, "vwap_above": true, "volume_spike": true, "signal_count": 7}},
      "reasoning": "EMA9>EMA21, above VWAP, volume 2.8x, gap +3.2% with catalyst — gap_and_go setup, score 8/10"
    }}
  ],
  "holds": [
    {{"symbol": "AMD", "reasoning": "RSI 72 overbought, no VWAP reclaim — SKIP"}}
  ],
  "candidates": [
    {{
      "symbol": "TQQQ",
      "action": "BUY",
      "reference_price": 52.15,
      "setup_type": "gap_and_go",
      "conviction": 8,
      "signal_count": 7,
      "blockers": [],
      "reasoning": "Qualified gap-and-go with 7 aligned signals."
    }},
    {{
      "symbol": "AMD",
      "action": "SKIP",
      "reference_price": 171.20,
      "setup_type": "watch_only",
      "conviction": 3,
      "signal_count": 2,
      "blockers": ["overbought", "no_vwap_reclaim"],
      "reasoning": "Only 2 signals; overbought and no reclaim."
    }}
  ]
}}
```

Then provide concise bullets covering only the key buys, holds, and skips."""

    decision_text = ask_model(prompt, load_system(), routine="trading")
    log.info("Trading decision received from Claude")

    orders_placed = []
    execution_events = []
    decision_payload = extract_decision_payload(decision_text, "trading")
    if decision_payload:
        _auto = _research_autobuy(decision_payload, account, positions, symbol_limits,
                                  get_effective_caps().get("min_buy_score", 4))
        if _auto:
            decision_payload.setdefault("orders", []).extend(_auto)
        _notify_proposal("trading", decision_payload, regime)
        try:
            order_data = decision_payload
            setup_types = {o["symbol"]: o.get("setup_type", "unknown") for o in order_data.get("orders", [])}
            orders_placed = _execute_orders(
                order_data.get("orders", []), account, positions,
                symbol_limits, regime, setup_types, execution_events,
            )
            log_decision_batch(
                "trading",
                order_data,
                account=account,
                regime=regime,
                execution_events=execution_events,
            )
            _notify_decision_executed(
                "trading",
                order_data,
                orders_placed,
                [],
                execution_events,
                regime,
            )
        except Exception as e:
            log.error(f"Order execution error: {e}")
            print(f"Order execution error: {e}")
    else:
        log.warning("No JSON order block found")

    section = (
        f"\n\n## Trades Executed — {now_mt()}\n\n{decision_text}\n\n"
        f"### Order Confirmations\n```json\n{json.dumps(orders_placed, indent=2, default=str)}\n```\n"
    )
    append_journal_text(section)

    print(f"Trading complete — {len(orders_placed)} order(s) → {journal_path()}")


# ── Swing exit management (trailing stops / partial profits) ──────────────────
_PEAKS_FILE = ROOT / "data" / "position_peaks.json"


def _load_peaks() -> dict:
    try:
        return json.loads(_PEAKS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_peaks(peaks: dict, live_symbols: set, managed_classes: set):
    # Drop closed positions, but ONLY within the asset class(es) this call actually
    # managed. _manage_swing_exits runs against SUBSETS of the book — run_crypto passes
    # crypto-only, run_intraday passes equity-only, run_cycle passes all — over the ONE
    # shared peaks file. Pruning every key not in this call's live_symbols let the hourly
    # crypto cycle wipe every equity trailing-peak record (and vice-versa); the next cycle
    # then rebuilt each peak from current pnl, resetting the trailing stop to a LOWER peak
    # and holding winners past the "give back ≤3% from peak" exit. So a key survives if
    # it's still live OR its asset class wasn't touched by this call.
    def _managed(sym: str) -> bool:
        return ("crypto" if _is_crypto(sym) else "equity") in managed_classes
    pruned = {k: v for k, v in peaks.items()
              if k in live_symbols or not _managed(k)}
    try:
        _PEAKS_FILE.parent.mkdir(exist_ok=True)
        _PEAKS_FILE.write_text(json.dumps(pruned, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning(f"peak-state save failed: {e}")


def _swing_exit_decision(sym: str, pnl_pct: float, peaks: dict) -> tuple:
    """
    Swing exit rules for one position. Returns (action, fraction, reason) where
    action is 'stop' | 'trail' | 'partial' | None. Updates the peak record in-place.
      - hard stop:  pnl <= swing_stop_pct  -> sell ALL (cut losers fast)
      - partial:    pnl >= partial_profit_pct, once -> sell HALF (lock some, let rest run)
      - trailing:   after peak >= trail_arm_pct, give back trail_giveback_pct -> sell ALL
    """
    ts = trading_style()
    stop = float(ts.get("swing_stop_pct", -3.0))
    partial_at = float(ts.get("partial_profit_pct", 10.0))
    trail_arm = float(ts.get("trail_arm_pct", 5.0))
    trail_give = float(ts.get("trail_giveback_pct", 4.0))

    rec = peaks.get(sym) or {"peak": pnl_pct, "partialed": False}
    rec["peak"] = max(float(rec.get("peak", pnl_pct)), pnl_pct)
    peaks[sym] = rec

    if pnl_pct <= stop:
        return ("stop", 1.0, f"hard stop {pnl_pct:.1f}% <= {stop:.0f}%")
    if rec["peak"] >= trail_arm and pnl_pct <= rec["peak"] - trail_give:
        return ("trail", 1.0,
                f"trailing stop: gave back to {pnl_pct:.1f}% from peak {rec['peak']:.1f}%")
    if pnl_pct >= partial_at and not rec.get("partialed"):
        rec["partialed"] = True
        return ("partial", 0.5, f"partial profit at +{pnl_pct:.1f}%")
    return (None, 0.0, "")


def _option_exit_decision(sym: str, pnl_pct: float) -> tuple:
    """
    Exit rules for a LONG option (calls/puts). Returns (action, reason) where action is
    'target' | 'stop' | None. Options swing ±50%+, so the -3% swing stop does NOT apply —
    they use the options-config thresholds instead:
      - <= close_at_dte days to expiry -> close (avoid terminal theta decay / assignment)
      - pnl <= stop_loss_pct           -> cut (premium can decay to 0)
      - pnl >= profit_target_pct       -> take profit
    """
    opt = load_options_config()
    dte = option_dte(sym)
    if dte is not None and dte <= int(opt.get("close_at_dte", 1)):
        return ("stop", f"{dte} DTE <= {opt.get('close_at_dte', 1)} — close before expiry")
    stop = float(opt.get("stop_loss_pct", -50))
    target = float(opt.get("profit_target_pct", 100))
    if pnl_pct <= stop:
        return ("stop", f"option stop {pnl_pct:+.0f}% <= {stop:.0f}%")
    if pnl_pct >= target:
        return ("target", f"option target {pnl_pct:+.0f}% >= +{target:.0f}%")
    return (None, "")


def _minutes_to_equity_close():
    """Minutes until the equity session closes per the Alpaca clock, or None if the
    market is closed or the clock can't be read. Used to gate the pre-close leveraged
    flatten — None (unreachable) fails SAFE: no forced flatten, the normal stop still
    applies."""
    try:
        clock = trade.get_market_status()
        if not clock.get("is_open"):
            return None
        nc = clock.get("next_close")
        if not nc:
            return None
        from datetime import datetime, timezone
        close_dt = datetime.fromisoformat(str(nc).replace("Z", "+00:00"))
        return (close_dt - datetime.now(timezone.utc)).total_seconds() / 60.0
    except Exception:
        return None


def _leveraged_overnight_flatten_due(sym: str, pnl_pct: float, mins_to_close) -> bool:
    """True when a RED leveraged-LONG ETF should be flattened before the close instead of
    carried overnight. A 3x leveraged long has decay + amplified gap risk overnight (the
    SOP already force-liquidates UVXY overnight for the same reason); carrying one while
    underwater is the dominant avg-loss driver (losers gap past the -3% stop overnight).
    Only fires: leveraged longs (TQQQ/SOXL/FNGU/LABU/FAS), red beyond the configured
    buffer, in the final pre-close window. Winners/flat and non-leveraged names: never.
    Config-gated (trading_style.flatten_red_leveraged_before_close, default on)."""
    ts = trading_style()
    if not ts.get("flatten_red_leveraged_before_close", True):
        return False
    if mins_to_close is None:
        return False
    window = float(ts.get("leveraged_close_window_min", 15))
    if not (0 <= mins_to_close <= window):
        return False
    loss_limit = float(ts.get("leveraged_overnight_loss_pct", -0.5))
    if pnl_pct > loss_limit:
        return False
    return normalize_symbol(sym) in _leveraged_long_symbols()


def _manage_swing_exits(positions: list, note_prefix: str = "") -> tuple:
    """
    CODE-ENFORCED swing exits on every open position (equity AND crypto): hard stop,
    trailing stop, and partial profit. Runs at the start of every cycle so losers are
    cut and winners trailed by code — never dependent on the model remembering. Sells
    clamp to held qty in trade.py, so this is safe to run alongside the crypto cycle.
    This is the ONE place the swing-exit rules live; every routine delegates here so the
    paths can't drift (they once held the same unlogged-partial bug in two copies).
    `note_prefix` tags the learning-log exit notes with the calling routine (e.g.
    "Intraday ") for provenance; it does not change any exit logic.
    Additionally flattens a RED leveraged-long ETF in the final pre-close window so a 3x
    decaying instrument is not carried overnight to gap past its stop (see
    _leveraged_overnight_flatten_due). Returns (action_strings, closed_normalized_symbols).
    """
    actions, closed = [], set()
    if not positions:
        return actions, closed
    peaks = _load_peaks()
    live_symbols = set()
    managed_classes = set()                      # asset classes this call actually manages
    # Equity orders only fill when the BROKER says the market is open. Use the Alpaca clock
    # (holiday-aware), not the time-based market_phase() which labels a holiday as REGULAR
    # and would fire equity exits that can't fill (then get swept as stale).
    equity_open = trade.equities_open_now()
    # Computed once per cycle (not per-position) — minutes until the equity close, for the
    # pre-close leveraged-long flatten. None when closed/unreachable (fails safe).
    mins_to_close = _minutes_to_equity_close() if equity_open else None
    for p in positions:
        sym = p.get("symbol")
        if not sym:
            continue
        pnl_pct = float(p.get("unrealized_plpc", 0) or 0) * 100
        curr = float(p.get("current_price", 0) or 0)
        qty = abs(float(p.get("qty", 0) or 0))
        if qty <= 0 or curr <= 0:
            continue
        live_symbols.add(sym)
        crypto = _is_crypto(sym, p.get("asset_class"))
        managed_classes.add("crypto" if crypto else "equity")
        # Don't fire EQUITY exits outside regular hours — the order won't fill (leaving a
        # stuck after-hours order) and _swing_exit_decision would prematurely flip the
        # partial/peak state. Crypto trades 24/7, so it's always managed. (Checked BEFORE
        # _swing_exit_decision so the peak state isn't mutated for a skipped equity.)
        if not crypto and not equity_open:
            continue

        # ── Options: long calls/puts use +target / -stop / expiry exits, NOT the -3% swing ──
        if is_option(sym):
            oaction, oreason = _option_exit_decision(sym, pnl_pct)
            if not oaction:
                continue
            o_qty = int(qty)                     # options trade in whole contracts
            o_price = round(curr * 0.95, 2)      # marketable through the wide option spread
            try:
                result = run("trade.py", "order", sym, str(o_qty), "sell", str(o_price))
                if isinstance(result, dict) and result.get("error"):
                    actions.append(f"OPTION-EXIT {sym} order failed: {result['error']}")
                    log.warning(f"Option exit order failed {sym}: {result['error']}")
                    continue
                filled_qty, fill_px, fill_status = _confirm_order_fill(result, o_qty, o_price)
                if filled_qty <= 0:
                    actions.append(f"OPTION-EXIT {sym} accepted but unfilled ({fill_status}); no exit logged")
                    log.warning(f"Option exit accepted but unfilled: {sym} {o_qty} @ {o_price} ({fill_status})")
                    continue
                reason_code = "take_profit" if oaction == "target" else "stop_loss"
                label = "OPT-TARGET" if oaction == "target" else "OPT-STOP"
                actions.append(f"{label} {sym} {pnl_pct:+.0f}% → sold {filled_qty} @ ${fill_px} ({oreason})")
                log.info(f"Option exit: {label} {sym} {pnl_pct:+.0f}% ({oreason})")
                _notify("take_profit_alert" if oaction == "target" else "stop_loss_alert",
                        sym, pnl_pct, p)
                log_exit(sym, fill_px, reason_code, notes=f"{note_prefix}{oreason}", qty=filled_qty,
                         entry_price=float(p.get("avg_entry_price", 0) or 0))
                if filled_qty >= o_qty - 1e-9:
                    closed.add(normalize_symbol(sym))
                    live_symbols.discard(sym)
            except Exception as e:
                log.error(f"option exit failed {sym}: {e}")
            continue

        action, frac, reason = _swing_exit_decision(sym, pnl_pct, peaks)
        # Pre-close override: a RED leveraged-long ETF that the normal rules would otherwise
        # CARRY (action is None) gets flattened so it isn't held overnight to gap past its
        # stop. Only escalates a non-exit into a full stop; never downgrades an existing
        # stop/trail/partial. Logged as stop_loss with a clear reason.
        if not action and not crypto and _leveraged_overnight_flatten_due(sym, pnl_pct, mins_to_close):
            action, frac, reason = ("stop", 1.0,
                f"pre-close flatten: red leveraged long {pnl_pct:+.1f}% not carried overnight (gap/decay guard)")
        if not action:
            continue
        sell_qty = qty if frac >= 1.0 else (round(qty * frac, 8) if crypto else max(1, int(qty * frac)))
        sell_price = round(curr * 0.997, 5 if crypto else 2)
        try:
            result = run("trade.py", "order", sym, str(sell_qty), "sell", str(sell_price))
            label = {"stop": "STOP-LOSS", "trail": "TRAIL-STOP", "partial": "PARTIAL"}[action]
            if isinstance(result, dict) and result.get("error"):
                actions.append(f"{label} {sym} order failed: {result['error']}")
                log.warning(f"Swing exit order failed {sym}: {result['error']}")
                if action == "partial":
                    peaks.get(sym, {})["partialed"] = False
                continue
            requested_sell_qty = sell_qty
            filled_qty, fill_px, fill_status = _confirm_order_fill(result, requested_sell_qty, sell_price)
            if filled_qty <= 0:
                actions.append(f"{label} {sym} order accepted but unfilled ({fill_status}); no exit logged")
                log.warning(f"Swing exit accepted but unfilled: {label} {sym} {requested_sell_qty} @ {sell_price} ({fill_status})")
                if action == "partial":
                    peaks.get(sym, {})["partialed"] = False
                continue
            fully_filled = filled_qty >= requested_sell_qty - 1e-9
            sell_qty = filled_qty
            sell_price = fill_px or sell_price
            actions.append(f"{label} {sym} {pnl_pct:+.1f}% → sold {sell_qty} @ ${sell_price} ({reason})")
            log.info(f"Swing exit: {label} {sym} {pnl_pct:+.1f}% ({reason})")
            if action in ("stop", "trail"):
                _notify("stop_loss_alert", sym, pnl_pct, p)
                log_exit(sym, sell_price, "stop_loss" if action == "stop" else "trailing_stop",
                         notes=f"{note_prefix}{reason}", qty=sell_qty,
                         entry_price=float(p.get("avg_entry_price", 0) or 0))
                if fully_filled:
                    closed.add(normalize_symbol(sym))
                    peaks.pop(sym, None)
                    live_symbols.discard(sym)
            elif action == "partial":
                # Record the partial so the tracked open_trades qty is reduced in step
                # with the position. Skipping it left the FULL entry tracked while only
                # half remained: the eventual close then logged a partial of the inflated
                # qty and left a residual orphan that detect_and_log_exits() fabricated
                # into a phantom round-trip win — and the partial profit itself went
                # entirely unrecorded in the learning log.
                log_exit(sym, sell_price, "partial_profit",
                         notes=f"{note_prefix}{reason}", qty=sell_qty,
                         entry_price=float(p.get("avg_entry_price", 0) or 0))
        except Exception as e:
            log.error(f"swing exit {action} failed {sym}: {e}")
    _save_peaks(peaks, live_symbols, managed_classes)
    return actions, closed


# ── Routine 3: Intraday Check (every 15 min) ──────────────────────────────────

def run_intraday():
    """
    Lightweight 15-minute check (SWING mode):
    1. Hard-stop losers fast (tighter swing stop), TRAIL winners, partial at target
    2. Hold winners — NO forced intraday flatten
    3. Sync exit detection for learning module
    """
    log.info("=== Intraday check ===")

    if kill_active():
        return

    status = safe_run("trade.py", "status")
    if not status.get("is_open"):
        return  # Skip during market-closed periods for equities

    positions = get_positions_norm()
    if not isinstance(positions, list):
        return

    # Equities only here — crypto is managed by the hourly crypto cycle (run_crypto).
    # Managing it here too would double-manage it (intraday runs 4×/hour). Delegate to
    # the SINGLE canonical exit path so the intraday rules can't drift from the
    # cycle/crypto paths (they once held the same unlogged-partial bug in two copies).
    # _manage_swing_exits owns the shared peak-state load/save and the "Intraday " tag
    # only labels the learning-log notes — the exit logic is identical everywhere.
    equity_positions = [p for p in positions
                        if not is_crypto(p.get("symbol", ""), p.get("asset_class"))]
    actions, _ = _manage_swing_exits(equity_positions, note_prefix="Intraday ")

    # Sync learning system on the FULL pre-exit snapshot — symbols just sold above still
    # appear here, so detect_and_log_exits sees them as live and won't double-log; it
    # only reconciles names that closed outside this path.
    detect_and_log_exits(positions, exit_reason="intraday_exit")

    if actions:
        log.info(f"Intraday actions: {actions}")
        for a in actions:
            print(f"  ACTION: {a}")
        intraday_block = f"\n\n### Intraday Check — {now_mt()}\n"
        intraday_block += "".join(f"- {a}\n" for a in actions)
        append_journal_text(intraday_block)
    else:
        log.info("Intraday check: no actions required")


# ── Routine 4: Crypto Cycle (hourly) ─────────────────────────────────────────

def run_crypto():
    """Hourly crypto research + trade cycle with scored conviction system."""
    log.info("=== Crypto cycle started ===")

    if kill_active():
        msg = "Kill switch active — crypto cycle skipped."
        log.warning(msg)
        _notify("alert", msg)
        print(msg)
        return

    ctx    = _load_context()
    regime = ctx["regime"]
    data   = gather_crypto_data()
    account = data["account"]
    positions = data["all_positions"]

    # Clear any unfilled orders resting from a prior cycle BEFORE deciding — they were
    # priced off data that has since moved; a dangling GTC crypto buy could otherwise
    # fill later into a move we no longer want (this was the stuck $61k BTC buy).
    stale = _cancel_stale_orders(positions)
    if stale:
        log.info(f"Cancelled {len(stale)} stale unfilled order(s): {stale}")

    # CODE-ENFORCED swing exits on CRYPTO positions every hour (24/7) — overnight
    # this is the only stop backstop, so a crash can't blow past -3% if the model
    # misses it. Equities are managed by run_cycle during market hours.
    crypto_positions = [p for p in positions if is_crypto(p.get("symbol", ""), p.get("asset_class"))]
    exit_actions, closed_syms = _manage_swing_exits(crypto_positions)
    if exit_actions:
        for a in exit_actions:
            print(f"  EXIT: {a}")
        append_journal_text(f"\n### Crypto swing exits — {now_mt()}\n" + "".join(f"- {a}\n" for a in exit_actions))
        positions = [p for p in positions
                     if normalize_symbol(p.get("symbol", ""), p.get("asset_class")) not in closed_syms]

    watchlist = load_watchlist()
    symbol_limits = {s["symbol"]: s["max_allocation_pct"] for s in watchlist["watchlist"]}
    equity = float(account.get("equity", 100000))

    # Concentration check — force an unwind when the book is over the crypto cap.
    risk = load_risk()
    crypto_cap = risk.get("max_crypto_exposure_pct", 40)
    crypto_mv = sum(float(p.get("market_value", 0) or 0) for p in positions
                    if is_crypto(p.get("symbol", ""), p.get("asset_class")))
    crypto_pct = (crypto_mv / equity * 100) if equity else 0
    concentration_note = (
        f"\n🚨 CONCENTRATION BREACH — crypto is {crypto_pct:.0f}% of equity vs the {crypto_cap}% HARD CAP.\n"
        f"Step 0 — MANDATORY DE-RISK FIRST: SELL to bring crypto back toward {crypto_cap}%. Trim the weakest "
        f"names first (lowest score / biggest loss / weakest trend) — put them in \"orders\" with side:\"sell\" "
        f"and the full or partial qty. Do NOT add ANY new crypto until back within the cap. This overrides "
        f"normal scoring.\n"
        if crypto_pct > crypto_cap + 0.5 else ""
    )

    prompt = f"""Crypto trading cycle — {data['timestamp']} MT

{ctx['regime_brief']}

{ctx['performance_brief']}

{ctx.get('intelligence_brief', '')}

Portfolio: ${equity:,.2f}

=== MARKET CONTEXT ===
Crypto Fear & Greed: {json.dumps(data.get('crypto_fear_greed'))}
CoinGecko: {json.dumps(data.get('coingecko_global'))}
Funding rates: {json.dumps(data.get('funding_rates'))}

=== OPEN POSITIONS ===
{json.dumps([{k: p[k] for k in ("symbol","qty","market_value","unrealized_plpc","current_price") if k in p} for p in positions], indent=2)}

=== SYMBOL DATA (indicators only — no raw bars) ===
Fields: ema9/ema21 (daily), ema9_1h/ema21_1h (hourly), rsi14, rsi14_1h, macd_signal, bb_squeeze, obv_trend, volume_spike, atr14, intraday_vwap, latest_price, news_summary
{json.dumps(data['research'], indent=2)}

=== TASK ===

**Regime: {regime.get('regime')} — sizing multiplier {regime.get('multiplier', 0.6)*100:.0f}%**
Crypto exposure now: {crypto_pct:.0f}% of equity (hard cap {crypto_cap}%).
{concentration_note}
Step 1 — MANDATORY stop-loss check (swing stop is TIGHTER than the regime stop — cut losers fast):
For every open position: if unrealized_plpc <= {min(regime.get('stop_loss_pct', -5.0), swing_stop_pct())/100:.2f}, SELL immediately. No exceptions, no averaging down.

Step 2 — Score each crypto symbol 1-10. ONLY a TREND-FOLLOWING long counts: require daily EMA9>EMA21 AND momentum confirmation (squeeze RELEASED bullish or MACD bullish). Do NOT buy active/coiling or bearish squeezes on an 'extreme fear' thesis — that setup lost -$2,190 at 0% win rate. Points:
- Daily EMA9 > EMA21: +2
- Hourly EMA9 > EMA21: +2
- Price above intraday VWAP: +1
- RSI 40-65: +1
- RSI < 35: +1
- MACD BULLISH_CROSS or BULLISH_MOMENTUM: +2
- BB Squeeze released BULLISH: +1
- OBV confirming price trend: +1
- Volume spike ratio >= 2.0: +1
- Crypto F&G score 20-45: +1
- Funding NEUTRAL or SHORT_CROWDING: +1
- Fibonacci support confluence: +1
- Strong news catalyst: +1
- BTC dominance falling: +1

Step 3 — Mandatory sizing by score (conviction_factor × max_alloc_pct).
The system AUTOMATICALLY applies the {regime.get('regime')} regime multiplier of {regime.get('multiplier', 0.6)*100:.0f}% — do NOT pre-multiply.
- Score 9-10: BUY at 1.00 × max_alloc_pct
- Score 7-8: BUY at 0.85 × max_alloc_pct
- Score 5-6: BUY at 0.55 × max_alloc_pct
- Score 3-4: WATCH only
- Score 1-2: SKIP

Step 4 — Winners (SWING — let them run, do NOT cap them):
- up ~+{trading_style().get('partial_profit_pct', 10):.0f}%: SELL about HALF to lock partial profit, let the rest run
- then TRAIL the remainder — only exit if it gives back >{trading_style().get('trail_giveback_pct', 4):.0f}% from its peak (after arming at +{trading_style().get('trail_arm_pct', 5):.0f}%), or the daily trend breaks (EMA9<EMA21). Do NOT auto-sell a winner just because it is up a lot; a strong trend can run much further — that is where the compounding comes from.

Step 5 — Calculate orders:
- qty = (equity × alloc_factor) / latest_price, rounded to 5 decimals
- Buy limit = latest_price × 1.005 | Sell limit = latest_price × 0.995  (wider buffer so the order is MARKETABLE and actually fills — latest_price is a bar close that lags the live ask in fast markets; a tight 0.2% limit rests unfilled)
- time_in_force = gtc
- Every scored symbol must appear once in "candidates" with action BUY, WATCH, HOLD, SKIP, CLOSE, or TRIM.

Output requirements:
- Start with the JSON immediately.
- Do not put any heading, title, or scoring table before the JSON.
- After the JSON, use at most 8 short bullets. No markdown tables.

Return JSON FIRST:
```json
{{
  "summary": "2-3 sentence summary of the crypto posture and best/weakest scores.",
  "orders": [
    {{
      "symbol": "BTC/USD", "qty": 0.0142, "side": "buy", "limit_price": 70190.00,
      "score": 8, "setup_type": "crypto_scored",
      "signals": {{"ema_daily": true, "ema_hourly": true, "vwap_above": true, "rsi_zone": true, "signal_count": 8}},
      "reasoning": "EMA daily+hourly (+4), VWAP (+1), RSI 52 (+1), MACD bullish (+2) = 8/10"
    }}
  ],
  "holds": [
    {{"symbol": "SOL/USD", "score": 3, "reasoning": "Below VWAP, bearish hourly EMA, no catalyst — SKIP"}}
  ],
  "candidates": [
    {{
      "symbol": "BTC/USD",
      "action": "BUY",
      "reference_price": 70190.00,
      "setup_type": "crypto_scored",
      "score": 8,
      "signal_count": 8,
      "blockers": [],
      "reasoning": "High-conviction crypto score with 8 aligned signals."
    }},
    {{
      "symbol": "SOL/USD",
      "action": "SKIP",
      "reference_price": 154.30,
      "setup_type": "crypto_watch",
      "score": 3,
      "signal_count": 3,
      "blockers": ["below_vwap", "bearish_hourly_ema", "no_catalyst"],
      "reasoning": "Only 3 points and still below VWAP."
    }}
  ]
}}
```

Then provide concise bullets summarizing the strongest score, weakest score, and any hold/trim rationale."""

    decision_text = ask_model(prompt, load_system(), routine="crypto")
    log.info("Crypto decision received")

    orders_placed = []
    execution_events = []
    decision_payload = extract_decision_payload(decision_text, "crypto")
    if decision_payload:
        _auto = _research_autobuy(decision_payload, account, positions, symbol_limits,
                                  get_effective_caps().get("min_buy_score", 4))
        if _auto:
            decision_payload.setdefault("orders", []).extend(_auto)
        _notify_proposal("crypto", decision_payload, regime)
        try:
            order_data = decision_payload

            # HARD TREND GATE: block crypto BUYs in a confirmed DAILY downtrend
            # (EMA9 < EMA21). The strategy is trend-following-ONLY, but the model keeps
            # trying to catch falling knives on the "extreme fear" thesis (e.g. BTC at
            # RSI 15, EMA9 far below EMA21) — exactly the -$2,190 / 0%-WR losing pattern.
            research_ind = data.get("research") or {}
            kept, blocked = [], []
            for o in order_data.get("orders", []):
                if str(o.get("side", "buy")).lower() == "buy":
                    nsym = normalize_symbol(o.get("symbol", ""))
                    ind = research_ind.get(o.get("symbol")) or research_ind.get(nsym) or {}
                    e9, e21 = ind.get("ema9"), ind.get("ema21")
                    if e9 is not None and e21 is not None and float(e9) < float(e21):
                        blocked.append(o.get("symbol"))
                        continue
                kept.append(o)
            if blocked:
                log.info(f"Crypto trend gate: blocked downtrend buy(s) {blocked} (daily EMA9<EMA21)")
                order_data["orders"] = kept

            price_lookup = {
                normalize_symbol(sym): (
                    payload.get("latest_price")
                    or payload.get("intraday_vwap")
                )
                for sym, payload in (data.get("research") or {}).items()
                if isinstance(payload, dict)
            }
            for pos in positions:
                price_lookup[normalize_symbol(pos.get("symbol", ""), pos.get("asset_class"))] = float(
                    pos.get("current_price", 0) or 0
                )
            # Regime multiplier is applied once, in _execute_orders (single source of truth).
            orders_placed = _execute_orders(
                order_data.get("orders", []), account, positions,
                symbol_limits, regime,
                {o["symbol"]: o.get("setup_type", "crypto_scored") for o in order_data.get("orders", [])},
                execution_events,
            )
            log_decision_batch(
                "crypto",
                order_data,
                account=account,
                regime=regime,
                price_lookup=price_lookup,
                execution_events=execution_events,
            )
            _notify_decision_executed(
                "crypto",
                order_data,
                orders_placed,
                [],
                execution_events,
                regime,
            )
        except Exception as e:
            log.error(f"Crypto order error: {e}")
            print(f"Crypto order error: {e}")
    else:
        log.warning("No JSON block in crypto decision")

    detect_and_log_exits(positions, exit_reason="crypto_cycle_exit")

    block = (
        f"\n\n---\n## Crypto Cycle — {data['timestamp']} MT | Regime: {regime.get('regime')}\n\n"
        f"{decision_text}\n\n"
        f"### Orders Placed\n```json\n{json.dumps(orders_placed, indent=2, default=str)}\n```\n"
    )
    append_journal_text(block)

    print(f"Crypto cycle done — {len(orders_placed)} order(s) → {journal_path()}")


# ── Routine: On-demand Cycle (!heartbeat + 15-min tasks) ─────────────────────

def run_cycle():
    """
    Gathers fresh live data, enforces stop-losses, evaluates new entries,
    appends a timestamped block to today's journal, and prints a condensed
    summary to stdout (captured by the Discord bot).

    Used by: !heartbeat command, 15-min intraday task.
    """
    log.info("=== Manual cycle started ===")

    if kill_active():
        msg = "Kill switch active — cycle skipped."
        print(msg)
        return

    ctx    = _load_context()
    regime = ctx["regime"]

    account   = run("research.py", "account")
    positions = get_positions_norm()
    if not isinstance(positions, list):
        positions = []

    # Clear unfilled orders resting from a prior cycle BEFORE deciding (priced off
    # stale data; a dangling limit shouldn't fill later into a move we no longer want).
    stale = _cancel_stale_orders(positions)
    if stale:
        log.info(f"Cancelled {len(stale)} stale unfilled order(s): {stale}")

    # CODE-ENFORCED swing exits FIRST — cut losers / trail winners every cycle,
    # independent of the model. Then drop closed names from the decision set.
    exit_actions, closed_syms = _manage_swing_exits(positions)
    if exit_actions:
        for a in exit_actions:
            print(f"  EXIT: {a}")
        append_journal_text(f"\n### Swing exits — {now_mt()}\n" + "".join(f"- {a}\n" for a in exit_actions))
        positions = [p for p in positions
                     if normalize_symbol(p.get("symbol", ""), p.get("asset_class")) not in closed_syms]

    # If equities are CLOSED today (broker clock — holiday/weekend), the costly equity
    # decision below would only place orders that can't fill (then get cancelled next
    # cycle). Crypto is handled by the 30-min run_crypto, and swing exits already ran above,
    # so stop here to save the API spend instead of churning all day on a holiday.
    if not trade.equities_open_now():
        log.info("run_cycle: equities closed (broker clock) — skipping the equity decision "
                 "(crypto handled by run_crypto).")
        print("  Equities closed — no new cycle decision (crypto runs on its own schedule).")
        return

    watchlist     = load_watchlist()
    symbol_limits = {s["symbol"]: s["max_allocation_pct"] for s in watchlist["watchlist"]}
    equity        = float(account.get("equity", 100_000))
    cash          = float(account.get("cash", 0))
    last_eq       = float(account.get("last_equity", equity))
    day_pnl       = equity - last_eq

    # Track max intraday crypto exposure for EOD report
    crypto_mv = sum(float(p.get("market_value", 0) or 0) for p in positions
                    if _is_crypto(p.get("symbol",""), p.get("asset_class")))
    crypto_pct_now = (crypto_mv / equity * 100) if equity else 0
    daily_state = get_daily_state()
    if crypto_pct_now > daily_state.get("max_intraday_crypto_pct", 0):
        update_daily_state(max_intraday_crypto_pct=round(crypto_pct_now, 2))

    # Focus fresh data pull on regime-valid + open-position symbols, PLUS the
    # two-sided hedges (inverse ETFs) EVERY cycle. Inverse ETFs rise when the market
    # falls, so buying them is a normal momentum LONG — this lets the agent profit
    # from a bearish/rotating tape instead of sitting in cash when the long universe
    # is all red (exactly the situation that was costing us trades).
    regime_syms = set(regime.get("preferred_instruments", []))
    open_syms   = {p["symbol"] for p in positions}
    hedge_syms  = {s["symbol"] for s in watchlist["watchlist"] if s.get("type") == "inverse_etf"}
    focus_syms  = regime_syms | open_syms | hedge_syms

    quick_data = {}
    for s in watchlist["watchlist"]:
        if s["symbol"] not in focus_syms:
            continue
        sym      = s["symbol"]
        bars     = safe_run("research.py", "bars", sym)
        if bars.get("error"):
            continue
        intraday = safe_run("research.py", "intraday", sym)
        idb      = intraday.get("bars") or []
        news     = safe_run("enrichment.py", "news", sym)
        # enrichment.py news returns a list; research.py news returns {"news":[...]}
        if isinstance(news, list):
            headlines = [n.get("title") or n.get("headline") or "" for n in news[:2]]
        elif isinstance(news, dict):
            articles  = news.get("articles") or news.get("news") or []
            headlines = [a.get("title") or a.get("headline") or "" for a in articles[:2]]
        else:
            headlines = []
        snap = safe_run("research.py", "snapshot", sym)
        live_price = snap.get("price") if isinstance(snap, dict) else None
        quick_data[sym] = {
            "type":              s.get("type", "equity"),
            "max_alloc_pct":     s.get("max_allocation_pct", 10),
            "regimes":           s.get("regimes", []),
            "LIVE_PRICE":        live_price,          # freshest trade price — use THIS for decisions
            "live_bid_ask":      [snap.get("bid"), snap.get("ask")] if isinstance(snap, dict) else None,
            "day_change_pct":    snap.get("day_change_pct") if isinstance(snap, dict) else None,
            "intraday_vwap":     intraday.get("vwap"),
            "daily_indicators":  bars.get("moving_averages", {}),  # DAILY timeframe — trend context only, NOT live price
            "recent_intraday_bars": idb[-6:],
            "news":              headlines,
        }

    # ── Step 1: Mandatory stop-loss enforcement ──────────────────────────────
    stop_actions = []
    stop_thresh  = regime.get("stop_loss_pct", -5.0)
    for p in positions:
        pnl_pct = float(p.get("unrealized_plpc", 0)) * 100
        sym     = p["symbol"]
        if pnl_pct <= stop_thresh:
            curr  = float(p.get("current_price", 0))
            qty   = abs(float(p.get("qty", 0)))
            sprc  = round(curr * 0.998, 5 if is_crypto(sym) else 2)
            result = trade.place_order(sym, qty, "sell", sprc)
            if isinstance(result, dict) and result.get("error"):
                stop_actions.append(f"STOP FAILED {sym}: {result['error']}")
            else:
                filled_qty, fill_px, fill_status = _confirm_order_fill(result, qty, sprc)
                if filled_qty > 0:
                    stop_actions.append(f"SOLD {sym} {filled_qty:g} @ ${fill_px} (stop {pnl_pct:.1f}%)")
                    log_exit(sym, fill_px, "stop_loss", notes=f"cycle check {pnl_pct:.1f}%",
                             qty=filled_qty, entry_price=float(p.get("avg_entry_price", 0) or 0))
                    _notify("stop_loss_alert", sym, pnl_pct, p)
                else:
                    stop_actions.append(f"STOP ORDER UNFILLED {sym}: accepted but not filled ({fill_status})")
                    log.warning(f"Stop-loss order accepted but unfilled: {sym} {qty:g} @ {sprc} ({fill_status})")

    if stop_actions:
        positions = get_positions_norm()

    # ── Step 2: Claude decision on fresh data ────────────────────────────────
    journal_snippet = ""
    if journal_path().exists():
        journal_snippet = read_journal_text()[-2000:]

    prompt = f"""Manual trading cycle — {now_mt()}

{ctx['regime_brief']}

{ctx['performance_brief']}

{ctx['recommendations']}

{ctx.get('intelligence_brief', '')}

{ctx.get('discovery_brief', '')}

{ctx.get('options_brief', '')}

Portfolio: ${equity:,.2f} | Cash: ${cash:,.2f} | Day P&L: ${day_pnl:+,.2f}

Open positions:
{json.dumps([{k: p[k] for k in ("symbol","qty","market_value","unrealized_plpc","current_price") if k in p} for p in positions], indent=2)}

FRESH market data (just pulled):
{json.dumps(quick_data, indent=2)}

DATA NOTE: "LIVE_PRICE" is the freshest trade price — use it for all entry/exit/size math.
"daily_indicators" (EMA/RSI/MACD/etc.) are computed on the DAILY timeframe for trend
context only; their "latest_close" can lag intraday and is NOT the live price. There is
no contradiction — pair the daily trend with LIVE_PRICE. Trade on this.

Stop-losses already executed this cycle: {stop_actions or 'None'}

Today's journal context (last 2000 chars):
{journal_snippet}

TASK — fresh eyes on live data:
1. For every open position: should we hold, scale out, or close?
2. For every symbol in the fresh data: does it meet 3+ entry signals from the SOP?
3. Size new orders at FULL max_alloc × conviction_factor — the system auto-applies the
   {regime.get('regime')} regime multiplier ({regime.get('multiplier', 0.6)*100:.0f}%). Do NOT pre-multiply.
4. Limit orders only, within 0.2% of current price. "No trade" is a valid outcome.
5. To trim or exit, put the symbol in "closes" (full exit) or an "orders" entry with side:"sell" (partial).
6. Every symbol in the fresh data must appear once in "candidates" with action BUY, HOLD, WATCH, SKIP, CLOSE, or TRIM.

Output requirements:
- Start with the JSON immediately.
- Do not put headings, tables, or prose before the JSON.
- After the JSON, use at most 8 short bullets. No markdown tables.

Return JSON FIRST:
```json
{{
  "orders": [
    {{"symbol": "TQQQ", "qty": 10, "side": "buy", "limit_price": 52.15,
      "setup_type": "ema_vwap_cross", "conviction": 7,
      "reasoning": "EMA9>EMA21, above VWAP, volume 2x — 7/10"}}
  ],
  "holds": [
    {{"symbol": "AMD", "reasoning": "RSI 71 overbought — hold existing, no new entry"}}
  ],
  "closes": [
    {{"symbol": "SOXL", "reasoning": "Target hit +9.2% — closing rest of position"}}
  ],
  "summary": "Plain-English 2-3 sentence summary of what was decided and why.",
  "candidates": [
    {{
      "symbol": "TQQQ",
      "action": "BUY",
      "reference_price": 52.15,
      "setup_type": "ema_vwap_cross",
      "conviction": 7,
      "signal_count": 7,
      "blockers": [],
      "reasoning": "Qualified long with 7 aligned signals."
    }},
    {{
      "symbol": "AMD",
      "action": "HOLD",
      "reference_price": 171.20,
      "setup_type": "position_management",
      "conviction": 5,
      "signal_count": 2,
      "blockers": ["overbought"],
      "reasoning": "Existing position can be held, but no fresh entry edge."
    }},
    {{
      "symbol": "SOXL",
      "action": "CLOSE",
      "reference_price": 67.10,
      "setup_type": "target_hit",
      "conviction": 8,
      "signal_count": 6,
      "blockers": [],
      "reasoning": "Target hit; risk/reward on remainder no longer favorable."
    }}
  ]
}}
```

Then provide concise bullets covering only the key actions and top blockers."""

    decision_text = ask_model(prompt, load_system(), routine="cycle")

    # ── Step 3: Execute orders ───────────────────────────────────────────────
    orders_placed = []
    closes_placed = []
    summary_text  = ""
    execution_events = []

    decision_payload = extract_decision_payload(decision_text, "cycle")
    if decision_payload:
        _auto = _research_autobuy(decision_payload, account, positions, symbol_limits,
                                  get_effective_caps().get("min_buy_score", 4))
        if _auto:
            decision_payload.setdefault("orders", []).extend(_auto)
        _notify_proposal("cycle", decision_payload, regime)
        try:
            data = decision_payload
            summary_text = data.get("summary", "")
            price_lookup = {
                normalize_symbol(sym): (
                    payload.get("LIVE_PRICE")
                    or payload.get("latest_price")
                    or payload.get("intraday_vwap")
                )
                for sym, payload in quick_data.items()
                if isinstance(payload, dict)
            }
            for pos in positions:
                price_lookup[normalize_symbol(pos.get("symbol", ""), pos.get("asset_class"))] = float(
                    pos.get("current_price", 0) or 0
                )

            # New entries
            orders_placed = _execute_orders(
                data.get("orders", []), account, positions,
                symbol_limits, regime,
                {o["symbol"]: o.get("setup_type", "cycle") for o in data.get("orders", [])},
                execution_events,
            )

            # Closes / exits
            for c in data.get("closes", []):
                sym = normalize_symbol(c["symbol"])
                pos = next((p for p in positions if p["symbol"] == sym), None)
                if not pos:
                    continue
                curr = float(pos.get("current_price", 0))
                qty  = abs(float(pos.get("qty", 0)))
                sprc = round(curr * 0.998, 5 if is_crypto(sym) else 2)
                if qty <= 0:
                    continue  # nothing to close
                result = trade.place_order(sym, qty, "sell", sprc)
                if isinstance(result, dict) and result.get("error"):
                    err = str(result["error"])
                    # "insufficient balance" on a CLOSE means the position is already
                    # gone (e.g. sold earlier this cycle) — that IS the desired end
                    # state, so treat it as benign, not an ERROR.
                    already_gone = any(s in err.lower() for s in
                                       ("insufficient balance", "not enough", "available: 0", "403"))
                    (log.info if already_gone else log.error)(f"Close {sym}: {err}")
                    closes_placed.append(
                        (f"ALREADY FLAT {sym}" if already_gone else f"CLOSE FAILED {sym}: {err}"))
                    execution_events.append({
                        "symbol": sym,
                        "side": "sell",
                        "status": "already_closed" if already_gone else "broker_error",
                        "message": err,
                    })
                    if not already_gone:
                        _notify("order_rejected", {
                            "symbol": sym,
                            "side": "sell",
                            "qty": qty,
                            "limit_price": sprc,
                            "reasoning": c.get("reasoning", ""),
                        }, f"Broker error: {err}")
                else:
                    filled_qty, fill_px, fill_status = _confirm_order_fill(result, qty, sprc)
                    if filled_qty <= 0:
                        closes_placed.append(f"CLOSE ORDER UNFILLED {sym}: accepted but not filled ({fill_status})")
                        execution_events.append({
                            "symbol": sym,
                            "side": "sell",
                            "status": "placed_unfilled",
                            "message": f"{c.get('reasoning', '') or 'manual cycle close'} | accepted but not filled ({fill_status})",
                        })
                        log.warning(f"Close order accepted but unfilled: {sym} {qty:g} @ {sprc} ({fill_status})")
                        _notify("trade_placed", {
                            "symbol": sym,
                            "side": "sell",
                            "qty": qty,
                            "limit_price": sprc,
                            "reasoning": c.get("reasoning", ""),
                        }, result)
                        continue
                    close_status = "filled" if filled_qty >= qty - 1e-9 else "partially_filled"
                    qty = filled_qty
                    sprc = fill_px or sprc
                    closes_placed.append(f"CLOSED {sym} {qty:g} @ ${sprc} — {c.get('reasoning','')}")
                    execution_events.append({
                        "symbol": sym,
                        "side": "sell",
                        "status": close_status,
                        "message": f"{c.get('reasoning', '') or 'manual cycle close'} | fill confirmed",
                    })
                    _notify("trade_placed", {
                        "symbol": sym,
                        "side": "sell",
                        "qty": qty,
                        "limit_price": sprc,
                        "reasoning": c.get("reasoning", ""),
                    }, result)
                    log_exit(sym, sprc, "target_hit", notes=c.get("reasoning", ""),
                             qty=qty, entry_price=float(pos.get("avg_entry_price", 0) or 0))

            log_decision_batch(
                "cycle",
                data,
                account=account,
                regime=regime,
                price_lookup=price_lookup,
                execution_events=execution_events,
            )
            _notify_decision_executed(
                "cycle",
                data,
                orders_placed,
                closes_placed,
                execution_events,
                regime,
            )

        except Exception as e:
            log.error(f"Cycle JSON parse error: {e}")

    detect_and_log_exits(positions, exit_reason="cycle_exit")

    # ── Step 4: Append to journal ────────────────────────────────────────────
    block = (
        f"\n\n---\n## Cycle — {now_mt()} | Regime: {regime.get('regime')}\n\n"
        f"{decision_text}\n\n"
        f"### Orders Placed\n```json\n{json.dumps(orders_placed, indent=2, default=str)}\n```\n"
    )
    if closes_placed or stop_actions:
        block += "### Exits\n" + "\n".join(f"- {x}" for x in stop_actions + closes_placed) + "\n"
    append_journal_text(block)

    # ── Step 5: Print condensed summary (captured by Discord bot) ────────────
    pos_lines = []
    for p in (positions if isinstance(positions, list) else []):
        pnl  = float(p.get("unrealized_plpc", 0)) * 100
        icon = "🟢" if pnl >= 0 else "🔴"
        sym  = p.get("symbol", "?")
        pos_lines.append(f"{icon} **{sym}** {pnl:+.1f}%")

    summary_lines = [
        f"**Regime:** {regime.get('regime')} (×{regime.get('multiplier',0.6):.0%} sizing) | **VIX:** {regime.get('vix','?')}",
        f"**Portfolio:** ${equity:,.2f} | **Cash:** ${cash:,.2f} | **Day P&L:** ${day_pnl:+,.2f}",
        f"**Positions checked:** {', '.join(pos_lines) if pos_lines else 'None'}",
    ]
    if stop_actions:
        summary_lines.append("**🛑 Stop-losses:** " + " | ".join(stop_actions))
    if closes_placed:
        summary_lines.append("**✅ Closed:** " + " | ".join(closes_placed))
    if orders_placed:
        summary_lines.append("**📥 Orders placed:**")
        for o in orders_placed:
            summary_lines.append(f"  • {o['side'].upper()} {o['qty']} **{o['symbol']}** @ ${o['limit_price']}")
    else:
        summary_lines.append("**No new entries** — no qualifying setups met criteria")
    if summary_text:
        summary_lines.append(f"\n💬 {summary_text}")

    output = "\n".join(summary_lines)
    print(output)
    log.info(f"Cycle complete — {len(orders_placed)} orders, {len(stop_actions)} stops.")


# ── Routine 5: End of Day (2:15 PM MT) ───────────────────────────────────────

def run_eod():
    log.info("=== End-of-Day started ===")

    account   = run("research.py", "account")
    positions = get_positions_norm()
    ctx       = _load_context()
    regime    = ctx["regime"]

    # Log exits for learning system before EOD
    closed = detect_and_log_exits(positions if isinstance(positions, list) else [], "eod_close")
    if closed:
        log.info(f"EOD exit detection: {closed}")

    existing = read_journal_text() if journal_path().exists() else "No journal today."
    equity   = float(account.get("equity", 0))
    last_eq  = float(account.get("last_equity", equity))
    day_pnl  = equity - last_eq
    day_pnl_pct = (day_pnl / last_eq * 100) if last_eq else 0

    # Finalize daily state for report
    daily_state = get_daily_state()
    update_daily_state(
        soft_stop_active=daily_state.get("soft_stop_active", False),
        hard_stop_active=daily_state.get("hard_stop_active", False),
    )

    prompt = f"""End-of-day routine for {today()}.

Current regime: {regime.get('regime')} (VIX: {regime.get('vix')})
Equity: ${equity:,.2f} | Day P&L: ${day_pnl:+,.2f} ({day_pnl_pct:+.2f}%)
Final positions: {json.dumps(positions, indent=2)}

Existing journal:
{existing[-3000:]}

Append ONLY these sections (do not repeat existing content):

## End-of-Day Summary — {today()}

### P&L Summary
- Day P&L: ${day_pnl:+,.2f} ({day_pnl_pct:+.2f}%)
- Open positions carried overnight (SWING — equities are intentionally held multi-day while the thesis/trend holds, plus crypto 24/7; note which and why. No forced equity flatten.)
- Positions closed today (symbol, entry, exit, P&L, setup type used)

### What Worked Today
- Which setups performed? Which signal combinations were most predictive?
- What would you do the same way tomorrow?

### What to Improve
- Any stop-losses triggered? Were entries mistimed?
- Did the regime sizing rule help or hurt?
- Specific adjustments for tomorrow based on today's results.

### Tomorrow's Watchlist
- Top 3 symbols to watch at open
- Specific price levels and conditions to monitor

### Learning Notes
- Any new pattern observed today worth tracking?
- Signal combinations that fired but didn't work — what was missing?

Return only these new sections."""

    reflection = ask_model(prompt, load_system(), routine="eod")

    append_journal_text(f"\n\n{reflection}\n")

    # Update performance stats cache
    try:
        update_performance()
        log.info("Performance stats updated")
    except Exception as e:
        log.warning(f"Performance update failed: {e}")

    _notify("eod_summary", account, positions, day_pnl)
    # Benchmark vs buy-and-hold / no-trade baselines -> #ft-reports
    try:
        import replay
        replay.post_report()
    except Exception as e:
        log.warning(f"Benchmark report failed: {e}")
    # Decision-intelligence audit (what the agent gets right/wrong) -> #ft-reports
    try:
        import intel_audit
        intel_audit.post()
    except Exception as e:
        log.warning(f"Intel audit failed: {e}")
    # Detailed, exportable end-of-day report -> Discord (embed + attached .md)
    if session_report:
        try:
            session_report.post("eod")
        except Exception as e:
            log.warning(f"EOD report failed: {e}")
    subprocess.run(["python", str(ROOT / "scripts" / "heartbeat.py"), "ok", "eod-complete"], cwd=ROOT)
    subprocess.run(["python", str(ROOT / "scripts" / "notify.py"), str(journal_path())], cwd=ROOT)

    log.info("End-of-day complete.")
    print(f"EOD complete → {journal_path()}")


# ── Routine 6: After-Hours Wrap (6:15 PM MT) ─────────────────────────────────

def run_afterhours():
    """
    Post-extended-hours wrap. Enforces the no-UVXY-overnight rule, syncs the
    learning loop, refreshes performance stats, and posts the detailed after-hours
    report to Discord for export to an external analysis AI.
    """
    log.info("=== After-hours wrap started ===")
    positions = get_positions_norm()

    # Hard rule: UVXY decays — never hold it overnight. Liquidate, CONFIRM the fill, and
    # log the exit explicitly. The old code logged via a PRE-sell snapshot, so the exit was
    # either missed or (now that detect reconciles shrunk lots) only caught a cycle later.
    uvxy_closed = False
    for p in positions:
        if normalize_symbol(p.get("symbol", "")) == "UVXY":
            qty = abs(float(p.get("qty", 0) or 0))
            curr = float(p.get("current_price", 0) or 0)
            if qty > 0:
                res = trade.place_order("UVXY", qty, "sell", round(curr * 0.997, 2))
                log.warning(f"After-hours UVXY liquidation: {res}")
                _notify("alert", f"UVXY liquidated after-hours (no overnight hold rule): {qty} @ ~${curr:.2f}")
                if isinstance(res, dict) and not res.get("error"):
                    filled_qty, fill_px, _ = _confirm_order_fill(res, qty, curr)
                    if filled_qty > 0:
                        log_exit("UVXY", fill_px, "system_correction",
                                 notes="no-overnight UVXY liquidation", qty=filled_qty,
                                 entry_price=float(p.get("avg_entry_price", 0) or 0))
                        uvxy_closed = True

    # Refresh the snapshot after the liquidation so the trailing learning sync reconciles
    # against the POST-sell book, not the stale pre-sell one.
    if uvxy_closed:
        positions = get_positions_norm()

    detect_and_log_exits(positions, "afterhours_close")
    try:
        update_performance()
    except Exception as e:
        log.warning(f"Performance update failed: {e}")

    if session_report:
        try:
            session_report.post("afterhours")
        except Exception as e:
            log.warning(f"After-hours report failed: {e}")
    subprocess.run(["python", str(ROOT / "scripts" / "heartbeat.py"), "ok", "afterhours-complete"], cwd=ROOT)
    log.info("After-hours wrap complete.")
    print("After-hours wrap complete.")


# ── Usage summary ─────────────────────────────────────────────────────────────

def _print_usage_summary(days: int = 7):
    """Print a cost summary from logs/api_usage.jsonl."""
    if not USAGE_LOG.exists():
        print("No usage log found yet.")
        return
    from collections import defaultdict
    records = []
    for line in USAGE_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except Exception:
                pass
    if not records:
        print("No usage records.")
        return

    today_str = today_mt()
    today_records = [r for r in records if r.get("ts", "").startswith(today_str)]
    all_records = records[-500:]  # last 500 calls

    def summarize(recs):
        by_routine = defaultdict(lambda: {"calls": 0, "input": 0, "output": 0, "cost": 0.0})
        total_cost = 0.0
        for r in recs:
            k = f"{r.get('routine','?')} ({r.get('model','?')[:20]})"
            by_routine[k]["calls"] += 1
            by_routine[k]["input"] += r.get("input_tokens", 0)
            by_routine[k]["output"] += r.get("output_tokens", 0)
            by_routine[k]["cost"] += r.get("cost_usd", 0)
            total_cost += r.get("cost_usd", 0)
        return by_routine, total_cost

    today_by, today_total = summarize(today_records)
    all_by, all_total = summarize(all_records)

    print(f"\n{'='*60}")
    print(f"API USAGE SUMMARY  [{today_str}]")
    print(f"{'='*60}")
    print(f"\nTODAY  ({len(today_records)} calls, ${today_total:.4f} total):")
    for k, v in sorted(today_by.items(), key=lambda x: -x[1]["cost"]):
        print(f"  {k:<45} ×{v['calls']:>3} calls  "
              f"{v['input']:>7,}in  {v['output']:>6,}out  ${v['cost']:.4f}")

    print(f"\nALL-TIME RECENT ({len(all_records)} calls, ${all_total:.4f} total):")
    for k, v in sorted(all_by.items(), key=lambda x: -x[1]["cost"]):
        print(f"  {k:<45} ×{v['calls']:>3} calls  "
              f"{v['input']:>7,}in  {v['output']:>6,}out  ${v['cost']:.4f}")
    print(f"{'='*60}\n")


# ── Routine 7: Market Open Summary (7:30 AM MT) ───────────────────────────────

def run_marketopen():
    """
    Posts a morning-open summary covering overnight crypto activity.
    Runs at 7:30 AM MT — before research (7:45) and trading (8:00) so the
    trader has a clear overnight picture before decisions are made.

    This is the 1st of 3 daily reports:
      07:30 — Market Open (overnight crypto + positions)
      14:15 — EOD (equity session wrap)
      18:15 — After-Hours (extended hours + final crypto)
    """
    log.info("=== Market Open Summary started ===")

    account   = run("research.py", "account")
    positions = get_positions_norm()

    # Cut any overnight-identified stop at the FIRST action of the session. A position
    # that breached its swing stop after-hours — typically a leveraged ETF that drifted
    # or gapped while equities were closed — should be flattened the instant the market
    # opens, not whenever the first intraday cycle happens to run. _manage_swing_exits is
    # idempotent (execution ledger + sells clamp to held qty) and no-ops when the broker
    # clock says equities are still closed, so this is safe to run here unconditionally.
    try:
        mo_exits, _ = _manage_swing_exits(positions, note_prefix="MarketOpen ")
        if mo_exits:
            log.info(f"Market-open swing exits executed: {mo_exits}")
            positions = get_positions_norm()           # refresh the book after flattening
    except Exception as e:
        log.warning(f"Market-open swing-exit pass failed: {e}")

    ctx       = _load_context()
    regime    = ctx["regime"]

    equity    = float(account.get("equity", 0))
    last_eq   = float(account.get("last_equity", equity))
    overnight_pnl     = equity - last_eq
    overnight_pnl_pct = (overnight_pnl / last_eq * 100) if last_eq else 0

    # Crypto-specific summary
    crypto_positions = [p for p in (positions or []) if _is_crypto(p.get("symbol",""), p.get("asset_class"))]
    equity_positions = [p for p in (positions or []) if not _is_crypto(p.get("symbol",""), p.get("asset_class"))]

    risk       = load_risk()
    crypto_mv  = sum(float(p.get("market_value", 0) or 0) for p in crypto_positions)
    crypto_pct = (crypto_mv / equity * 100) if equity else 0

    # Brief Claude summary (Sonnet — no need for Opus on a status report)
    crypto_data = gather_crypto_data()
    fg     = crypto_data.get("crypto_fear_greed", {})
    cgecko = crypto_data.get("coingecko_global", {})

    # Build position JSON OUTSIDE the f-string — nesting dict literals inside an
    # f-string replacement field is brace-ambiguous ({{...}} becomes a set wrapping
    # a dict -> "unhashable type: dict"). Compute here, interpolate a clean string.
    crypto_pos_json = json.dumps([{
        "symbol": p.get("symbol"),
        "qty": p.get("qty"),
        "entry": p.get("avg_entry_price"),
        "current": p.get("current_price"),
        "pnl_pct": round(float(p.get("unrealized_plpc", 0) or 0) * 100, 2),
        "pnl_usd": round(float(p.get("unrealized_pl", 0) or 0), 2),
    } for p in crypto_positions], indent=2)
    equity_pos_json = json.dumps([{
        "symbol": p.get("symbol"),
        "pnl_pct": round(float(p.get("unrealized_plpc", 0) or 0) * 100, 2),
    } for p in equity_positions], indent=2)

    prompt = f"""Market open morning summary — {now_mt()}

{ctx['regime_brief']}

{ctx['performance_brief']}

{ctx.get('intelligence_brief', '')}

=== OVERNIGHT RECAP ===
Equity: ${equity:,.2f} | Overnight P&L: ${overnight_pnl:+,.2f} ({overnight_pnl_pct:+.2f}%)
Crypto exposure: ${crypto_mv:,.0f} = {crypto_pct:.1f}% of portfolio (cap {risk.get('max_crypto_exposure_pct')}%)

Open crypto positions ({len(crypto_positions)}):
{crypto_pos_json}

Equity positions carried overnight ({len(equity_positions)}):
{equity_pos_json}

Crypto Fear & Greed: {json.dumps(fg)}
CoinGecko global: {json.dumps(cgecko)}

=== TASK ===
Write a concise market-open brief (3-4 paragraphs) covering:
1. Overnight crypto P&L — what happened, which positions moved most
2. Current portfolio posture — crypto exposure vs cap, any stops at risk
3. Key levels to watch at open — what price action matters in the first 30 min
4. Go/no-go assessment — are we positioned correctly for the morning session?

Be direct and actionable. Every number must appear. Flag anything requiring immediate action."""

    summary_text = ask_model(prompt, load_system(), routine="eod")

    # Write to journal
    section = (
        f"\n\n---\n## Market Open Summary — {now_mt()} | Regime: {regime.get('regime')}\n\n"
        f"{summary_text}\n"
    )
    append_journal_text(section)

    # Update performance & detect exits from overnight
    detect_and_log_exits(positions or [], exit_reason="overnight")

    # Post to Discord (embed + .md attachment via report module)
    if session_report:
        try:
            session_report.post("marketopen")
        except Exception as e:
            log.warning(f"Market open report failed: {e}")

    # Heartbeat
    subprocess.run(["python", str(ROOT / "scripts" / "heartbeat.py"), "ok", "marketopen-complete"], cwd=ROOT)

    log.info("Market open summary complete.")
    print(f"Market open summary → {journal_path()}")
    print(summary_text[:500])


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    routine = sys.argv[1] if len(sys.argv) > 1 else None

    # Side-effect-free offline check — no trading machinery, no activity logging.
    if routine == "validate-models":
        _res = validate_model_config()
        print(json.dumps(_res, indent=2))
        if not _res["ok"]:
            print(f"\n⚠️  Unpriced model IDs (would bill at Opus fallback rate): "
                  f"{', '.join(_res['unpriced'])}")
        sys.exit(0 if _res["ok"] else 1)

    if routine in _STATUS_ROUTINES:
        try:
            trade.ledger.append_event("routine.started", payload={"routine": routine})
            reconciliation = trade.reconcile_orders(include_broker_orders=True)
            applied_fills = _apply_reconciled_fills()
            if applied_fills:
                log.info(f"Applied {len(applied_fills)} delayed fill(s) during reconciliation")
            if reconciliation.get("unresolved"):
                message = (
                    f"Execution reconciliation found {reconciliation['unresolved']} unresolved "
                    f"order(s); conflicting submissions remain blocked."
                )
                log.warning(message)
                _notify("alert", message)
            if reconciliation.get("errors"):
                log.warning(f"Execution reconciliation warnings: {reconciliation['errors']}")
        except Exception as e:
            log.warning(f"Execution reconciliation unavailable: {e}")

    try:
        import activity as _activity
    except Exception:
        _activity = None
    if _activity and routine:
        _activity.log("routine_start", routine)
    _routine_ok = True
    try:
        if routine == "research":
            run_research()
        elif routine == "trading":
            run_trading()
        elif routine == "intraday":
            run_intraday()
        elif routine == "cycle":
            run_cycle()
        elif routine == "eod":
            run_eod()
        elif routine == "afterhours":
            run_afterhours()
        elif routine == "marketopen":
            run_marketopen()
        elif routine == "crypto":
            run_crypto()
        elif routine == "report":
            if session_report:
                session_report.post(sys.argv[2] if len(sys.argv) > 2 else "now")
        elif routine == "usage":
            _print_usage_summary()
        else:
            print("Usage: python scripts/orchestrator.py [research|trading|intraday|cycle|eod|afterhours|marketopen|crypto|report|usage|validate-models]")
            sys.exit(1)
        if _activity and routine:
            _activity.log("routine_done", routine)
    except Exception as e:
        _routine_ok = False
        log.exception(f"Routine '{routine}' failed: {e}")
        _notify("dev_log", f"Routine '{routine}' failed: {e}", "error")
        if _activity and routine:
            _activity.log("routine_error", f"{routine}: {e}", error=str(e)[:200])
        print(f"FATAL ERROR in '{routine}': {e}")
    finally:
        if routine in _STATUS_ROUTINES:
            try:
                trade.ledger.append_event(
                    "routine.completed" if _routine_ok else "routine.failed",
                    payload={"routine": routine},
                )
            except Exception as e:
                log.warning(f"Could not record routine execution event: {e}")
        # Keep the local liveness signal current for every autonomous trading routine.
        # The status card below is already the Discord pulse, so this write is local-only.
        if routine in _STATUS_ROUTINES:
            try:
                from heartbeat import write_heartbeat
                write_heartbeat(
                    "ok" if _routine_ok else "error",
                    f"{routine}-{'complete' if _routine_ok else 'failed'}",
                    notify=False,
                )
            except Exception as e:
                log.warning(f"Could not write routine heartbeat for '{routine}': {e}")
        # Post the !status snapshot to #ft-command-center on EVERY trading routine — including
        # failures — so the channel is a live pulse (equity / day P&L / cash / positions)
        # even when a routine crashed. Best-effort; never masks the routine's exit code.
        if routine in _STATUS_ROUTINES:
            _notify("status_update", routine,
                    note=("" if _routine_ok else "⚠️ routine ERRORED this run — see #ft-dev-log"))
    if not _routine_ok:
        sys.exit(1)
