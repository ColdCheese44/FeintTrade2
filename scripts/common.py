"""
Shared utilities for FeintTrade — the single source of truth for:
  • Symbol normalization (Alpaca returns BTCUSD; we standardize on BTC/USD)
  • Mountain Time with correct MDT/MST labeling (zoneinfo America/Denver)
  • Risk config loading from watchlist.json
  • Market-session helpers and data-freshness checks
  • Validation-mode detection and caps (< 30 completed trades)
  • Correlated crypto basket accounting
  • Session entry deduplication
  • Daily stop-state management

Import this everywhere instead of re-implementing is_crypto() / now_mt().
"""

import json
import os
import re
from datetime import datetime, date, timezone, timedelta
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except Exception:
    Retry = None

ROOT = Path(__file__).parent.parent


def make_http_session(total: int = 4, backoff: float = 0.5) -> requests.Session:
    """
    A requests.Session that rides out transient DNS/connection blips (e.g. a VPN
    tunnel reconnecting) by retrying with exponential backoff. Only IDEMPOTENT
    methods are retried (urllib3 default: GET/HEAD/DELETE/etc.) — POST is NOT, so
    order placement can never be double-submitted by a retry. Falls back to a
    plain session if urllib3's Retry is unavailable.
    """
    session = requests.Session()
    if Retry is not None:
        retry = Retry(
            total=total, connect=total, read=total, status=total,
            backoff_factor=backoff,
            status_forcelist=(429, 500, 502, 503, 504),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    return session

# ── Time zones ──────────────────────────────────────────────────────────────
try:
    from zoneinfo import ZoneInfo
    MT = ZoneInfo("America/Denver")     # auto MDT/MST
    ET = ZoneInfo("America/New_York")
except Exception:                        # pragma: no cover - fallback
    MT = timezone(timedelta(hours=-6))
    ET = timezone(timedelta(hours=-4))


def now_mt() -> datetime:
    """Timezone-aware current time in Mountain Time."""
    return datetime.now(MT)


def mt_tz_label() -> str:
    """'MDT' during daylight saving, 'MST' otherwise."""
    try:
        return now_mt().tzname() or "MT"
    except Exception:
        return "MT"


def now_mt_str(fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Formatted MT timestamp with the correct MDT/MST suffix, e.g. '2026-06-02 14:15 MDT'."""
    n = now_mt()
    return f"{n.strftime(fmt)} {n.tzname()}"


def today_mt() -> str:
    return now_mt().strftime("%Y-%m-%d")


# ── Symbol normalization ──────────────────────────────────────────────────────
# Crypto base assets tradable on Alpaca (USD pairs). Used to detect BTCUSD-style
# symbols that Alpaca returns from /v2/positions without a slash.
CRYPTO_BASES = {
    "BTC", "ETH", "SOL", "DOGE", "AVAX", "LINK", "XRP", "LTC", "BCH", "UNI",
    "AAVE", "DOT", "MATIC", "SHIB", "ADA", "XLM", "SUSHI", "YFI", "GRT", "MKR",
    "CRV", "BAT", "DAI", "TRX", "USDT", "USDC", "PEPE",
    # Note: DOT was duplicated above — removed the second occurrence
}


def normalize_symbol(symbol: str, asset_class: str | None = None) -> str:
    """
    Canonical internal form. Crypto -> 'BASE/USD'. Equities -> uppercase ticker.
    Handles Alpaca's slashless crypto symbols (BTCUSD -> BTC/USD).
    """
    if not symbol:
        return symbol
    s = str(symbol).strip().upper()
    if "/" in s:
        return s
    crypto = (asset_class == "crypto")
    if not crypto and s.endswith("USD") and len(s) > 3 and s[:-3] in CRYPTO_BASES:
        crypto = True
    if crypto and s.endswith("USD") and len(s) > 3:
        return f"{s[:-3]}/USD"
    return s


def is_crypto(symbol: str, asset_class: str | None = None) -> bool:
    """True for crypto pairs. Works on both 'BTC/USD' and Alpaca's 'BTCUSD'."""
    if asset_class == "crypto":
        return True
    s = str(symbol or "").upper()
    if "/" in s:
        return True
    if s.endswith("USD") and len(s) > 3 and s[:-3] in CRYPTO_BASES:
        return True
    return False


# ── Options (OCC symbol) ──────────────────────────────────────────────────────
# OCC format: <UNDERLYING><YYMMDD><C|P><STRIKE*1000 in 8 digits>
# e.g. NVDA260608C00150000 = NVDA 2026-06-08 Call $150.00
_OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")
OPTION_CONTRACT_MULTIPLIER = 100  # one contract = 100 shares of premium exposure


def is_option(symbol: str) -> bool:
    """True for an OCC option symbol like 'NVDA260608C00150000'."""
    return bool(_OCC_RE.match(str(symbol or "").upper().strip()))


def option_underlying(symbol: str) -> str | None:
    """Underlying ticker from an OCC option symbol, else None."""
    m = _OCC_RE.match(str(symbol or "").upper().strip())
    return m.group(1) if m else None


def option_parts(symbol: str) -> dict | None:
    """Parse an OCC symbol into {underlying, expiry: date, type: 'call'|'put', strike: float}."""
    m = _OCC_RE.match(str(symbol or "").upper().strip())
    if not m:
        return None
    u, yy, mm, dd, cp, strike = m.groups()
    try:
        exp = date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        return None
    return {"underlying": u, "expiry": exp,
            "type": "call" if cp == "C" else "put", "strike": int(strike) / 1000.0}


def option_dte(symbol: str):
    """Calendar days to expiry for an OCC option symbol, or None if not an option."""
    parts = option_parts(symbol)
    if not parts:
        return None
    return (parts["expiry"] - now_mt().date()).days


def normalize_positions(positions: list) -> list:
    """
    Rewrite every position's 'symbol' to canonical form, preserving the original
    Alpaca symbol under 'raw_symbol'. Call immediately after fetching positions.
    """
    if not isinstance(positions, list):
        return positions
    out = []
    for p in positions:
        if not isinstance(p, dict):
            continue
        q = dict(p)
        q["raw_symbol"] = p.get("symbol")
        q["symbol"] = normalize_symbol(p.get("symbol", ""), p.get("asset_class"))
        out.append(q)
    return out


# ── Risk configuration ─────────────────────────────────────────────────────────
_DEFAULT_RISK = {
    "cash_reserve_pct": 5,
    "max_daily_drawdown_pct": 6,
    "max_open_positions": 8,
    "max_crypto_exposure_pct": 40,
    "max_same_sector_positions": 3,
    "default_unlisted_max_alloc_pct": 10,
    "loss_streak_throttle": 3,
    "disable_daily_stops_in_paper": False,
}


def load_watchlist() -> dict:
    try:
        return json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_risk() -> dict:
    """Risk limits from watchlist.json 'risk' block, with top-level + default fallbacks."""
    wl = load_watchlist()
    risk = dict(_DEFAULT_RISK)
    # Back-compat: honor any top-level keys that older configs used
    for k in _DEFAULT_RISK:
        if k in wl:
            risk[k] = wl[k]
    risk.update(wl.get("risk", {}) or {})
    return risk


_DEFAULT_OPTIONS = {
    "weekday_focus": True,
    "max_total_exposure_pct": 30,   # premium-at-risk across ALL option positions
    "max_per_underlying_pct": 10,   # premium-at-risk per underlying
    "max_premium_per_trade": 5000,  # absolute $ premium cap on a single option order
    "target_delta": 0.40, "min_delta": 0.35, "max_delta": 0.45,
    "min_dte": 3, "max_dte": 7, "max_iv_rank": 50,
    "profit_target_pct": 100,       # take profit at +100% of premium
    "stop_loss_pct": -50,           # cut at -50% of premium
    "close_at_dte": 1,              # force-close at <= this many days to expiry
    "underlyings": ["NVDA", "TSLA", "AMD", "SPY", "QQQ"],
}


def load_options_config() -> dict:
    """Options config from watchlist.json 'options' block (+ defaults). 'enabled' is the
    top-level options_enabled flag so a single switch turns the whole feature on/off."""
    wl = load_watchlist()
    cfg = dict(_DEFAULT_OPTIONS)
    cfg.update(wl.get("options", {}) or {})
    cfg["enabled"] = bool(wl.get("options_enabled", False))
    return cfg


def options_enabled() -> bool:
    return bool(load_options_config().get("enabled"))


def alpaca_base_url() -> str:
    """Configured Alpaca API base URL."""
    return str(os.getenv("APCA_BASE_URL", "https://paper-api.alpaca.markets") or "").strip()


def is_paper_trading() -> bool:
    """True when the configured Alpaca endpoint is the paper environment."""
    return "paper-api" in alpaca_base_url().lower()


def daily_stops_enforced() -> bool:
    """
    Daily soft/hard stops remain active unless this project explicitly disables
    them while connected to Alpaca paper trading.
    """
    risk = load_risk()
    if is_paper_trading() and risk.get("disable_daily_stops_in_paper", False):
        return False
    return True


# ── Research / data-collection mode (paper-only, reversible) ──────────────────
RESEARCH_MODE_STATE = ROOT / "data" / "research_mode_state.json"


def research_mode_enabled() -> bool:
    """
    Raw on/off intent (watchlist.json default, overridden by the dashboard's runtime
    state file) — WITHOUT the paper gate. Use for the toggle's displayed position.
    """
    enabled = bool((load_watchlist().get("research_mode", {}) or {}).get("enabled"))
    try:
        if RESEARCH_MODE_STATE.exists():
            override = json.loads(RESEARCH_MODE_STATE.read_text(encoding="utf-8"))
            if "enabled" in override:
                enabled = bool(override["enabled"])
    except Exception:
        pass
    return enabled


def research_mode() -> dict:
    """
    Return the research-mode override block when it is ACTIVE, else {}.
    Config (caps/flags + default `enabled`) lives in watchlist.json; the dashboard
    toggle writes a tiny runtime override file that takes precedence. Active only on
    a paper endpoint — pointing APCA_BASE_URL at live auto-restores all safeguards
    regardless of the flag. The full safeguard infrastructure is always retained.
    """
    rm = dict(load_watchlist().get("research_mode", {}) or {})
    rm["enabled"] = research_mode_enabled()
    return rm if (rm["enabled"] and is_paper_trading()) else {}


def research_mode_active() -> bool:
    return bool(research_mode())


def set_research_mode(enabled: bool) -> bool:
    """
    Toggle research mode at runtime (used by the dashboard). Writes only a small
    state file — watchlist.json (the config of record) is never rewritten. Returns
    the new state.
    """
    RESEARCH_MODE_STATE.parent.mkdir(exist_ok=True)
    RESEARCH_MODE_STATE.write_text(
        json.dumps({"enabled": bool(enabled), "ts": now_mt_str()}, indent=2),
        encoding="utf-8",
    )
    return bool(enabled)


def loss_streak_lockout_enforced() -> bool:
    """
    Loss-streak lockout stays active unless research mode explicitly disables it
    (paper-only). Infrastructure is retained for live and later test phases.
    """
    if research_mode().get("disable_loss_streak_lockout"):
        return False
    return True


def force_autobuy_enabled() -> bool:
    """
    Whether the research-mode code-level auto-buy is allowed to force a trade when
    the model proposes none. Default OFF — forcing trades proved -EV (it generated
    losing knife-catch entries). Set research_mode.disable_force_autobuy=false to
    re-enable for pure data collection.
    """
    return not research_mode().get("disable_force_autobuy", True)


# ── Trading style (swing vs day) ──────────────────────────────────────────────
def trading_style() -> dict:
    """The active trading-style block (swing params etc.) from watchlist.json."""
    return load_watchlist().get("trading_style", {}) or {}


def swing_mode_active() -> bool:
    """True when configured for swing/position trading (hold multi-day, no flatten)."""
    return str(trading_style().get("mode", "")).lower() == "swing"


def swing_stop_pct() -> float:
    """Loss-cut threshold (negative %). Swing uses a tighter cut than the regime -5%."""
    try:
        return float(trading_style().get("swing_stop_pct", -3.0))
    except (TypeError, ValueError):
        return -3.0


def load_live_account() -> dict:
    """
    Live-trading sizing profile. When enabled, the agent should size as if the
    account holds `starting_capital` (e.g. $100) even though paper shows ~$100k,
    so paper behavior mirrors the real $100 -> $1,000 challenge.
    """
    wl = load_watchlist()
    la = {
        "enabled": False,
        "starting_capital": 100.0,
        "target_capital": 1000.0,
        "target_days": 30,
        "min_order_usd": 1.0,
    }
    la.update(wl.get("live_account", {}) or {})
    return la


def effective_equity(account: dict) -> float:
    """
    The equity figure used for position sizing. In live-sim mode this is the
    small-account capital so percentage allocations translate to real-world size.
    """
    la = load_live_account()
    if la.get("enabled"):
        return float(la.get("starting_capital", 100.0))
    try:
        return float(account.get("equity", 100000))
    except Exception:
        return 100000.0


# ── Market session (Mountain Time aware) ────────────────────────────────────────
# Regular US equity session: 07:30–14:00 MT. Extended: 02:00–07:30 (pre), 14:00–18:00 (after).

def market_phase() -> str:
    """REGULAR | PRE_MARKET | AFTER_HOURS | CLOSED — purely time-based (MT)."""
    n = now_mt()
    if n.weekday() >= 5:
        return "CLOSED"
    h = n.hour + n.minute / 60.0
    if 7.5 <= h < 14.0:
        return "REGULAR"
    if 2.0 <= h < 7.5:
        return "PRE_MARKET"
    if 14.0 <= h < 18.0:
        return "AFTER_HOURS"
    return "CLOSED"


def minutes_to_close() -> int | None:
    """Minutes until 14:00 MT equity close, or None if not in the regular session."""
    n = now_mt()
    if market_phase() != "REGULAR":
        return None
    close = n.replace(hour=14, minute=0, second=0, microsecond=0)
    return max(0, int((close - n).total_seconds() // 60))


def minutes_to_afterhours_close() -> int | None:
    """Minutes until 18:00 MT after-hours close, or None if outside 07:30–18:00."""
    n = now_mt()
    if market_phase() not in ("REGULAR", "AFTER_HOURS"):
        return None
    close = n.replace(hour=18, minute=0, second=0, microsecond=0)
    return max(0, int((close - n).total_seconds() // 60))


# ── Correlated crypto baskets ────────────────────────────────────────────────
# All crypto symbols count toward the global basket cap.
# BTC/ETH are treated as "major" — alts are subject to a tighter per-alt cap.
CORRELATED_CRYPTO_BASKETS = {
    "all_crypto": {
        "BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "AVAX/USD",
        "LINK/USD", "XRP/USD", "LTC/USD", "BCH/USD", "ADA/USD",
        "MATIC/USD", "SHIB/USD", "DOT/USD", "UNI/USD",
    },
    "alts": {  # non-BTC/ETH — tighter cap in validation mode
        "SOL/USD", "DOGE/USD", "AVAX/USD", "LINK/USD", "XRP/USD",
        "LTC/USD", "BCH/USD", "ADA/USD", "MATIC/USD", "SHIB/USD",
        "DOT/USD", "UNI/USD",
    },
}


# ── Same-direction sector concentration (HARD RULE #9) ───────────────────────
# Correlated, SAME-DIRECTION long equities/ETFs: holding several is one bet wearing
# different tickers — a sector drawdown hits them together. The risk engine caps the
# number of distinct OPEN positions per sector at risk.max_same_sector_positions
# (default 3; the CLAUDE.md example is TQQQ+SOXL+NVDA = 3 tech longs = max).
# Inverse/hedge ETFs (SQQQ/SOXS/UVXY) are DELIBERATELY unmapped — they are the
# opposite trade and offset long exposure, so a risk-off posture is never throttled.
# Crypto has its own exposure cap. Unmapped / discovery names aren't constrained here
# (we don't block what we can't classify).
SECTOR_MAP = {
    # High-beta tech / semis / growth — these sell off together in a risk-off move.
    "TQQQ": "tech", "SOXL": "tech", "FNGU": "tech",
    "NVDA": "tech", "AMD": "tech", "PLTR": "tech", "TSLA": "tech",
    # Crypto-proxy equities track crypto risk appetite together.
    "MSTR": "crypto_equity", "COIN": "crypto_equity",
    # Single-name leveraged sector ETFs (own buckets).
    "LABU": "biotech", "FAS": "financials",
}


def sector_for(symbol: str, asset_class: str | None = None) -> str | None:
    """Correlated-long sector bucket for the concentration cap, or None if unmapped
    (crypto, inverse/hedge ETFs, and discovery names are intentionally unmapped)."""
    return SECTOR_MAP.get(normalize_symbol(symbol, asset_class))


# ── Validation-mode caps (active until 30 completed trades) ──────────────────
VALIDATION_MODE_CAPS = {
    "max_crypto_exposure_pct":     15,   # vs 40% normal
    "max_single_crypto_pct":        8,   # per-symbol crypto cap
    "max_altcoin_exposure_pct":     3,   # any one alt (non-BTC/ETH)
    "max_daily_loss_soft_pct":      2,   # soft stop: -2% → disable new buys
    "max_daily_loss_hard_pct":      3,   # hard stop: -3% → reduce-only mode
    "max_new_crypto_per_session":   2,   # max new crypto entries per session
    "max_adds_per_symbol":          1,   # max scale-ins per symbol per session
    "completed_trades_threshold":  30,   # exit validation mode after N trades
}

NORMAL_MODE_CAPS = {
    "max_daily_loss_soft_pct":      4,   # softer in normal mode
    "max_daily_loss_hard_pct":      6,   # normal daily drawdown limit
}


def is_validation_mode(completed_trades: int = None) -> bool:
    """True when completed_trades < threshold — use tighter caps."""
    if research_mode().get("disable_validation_mode"):
        return False
    if completed_trades is None:
        try:
            perf = json.loads((ROOT / "data" / "performance.json").read_text(encoding="utf-8"))
            completed_trades = perf.get("total_trades", 0)
        except Exception:
            completed_trades = 0
    threshold = VALIDATION_MODE_CAPS["completed_trades_threshold"]
    return completed_trades < threshold


def get_effective_caps(completed_trades: int = None) -> dict:
    """Return the active risk caps depending on validation mode.
    Validation-mode overrides are loaded from watchlist.json 'validation_mode' block
    (with VALIDATION_MODE_CAPS as fallback defaults).
    """
    risk = load_risk()
    wl_vm = load_watchlist().get("validation_mode", {}) or {}

    # Merge: watchlist config overrides defaults
    vm_caps = dict(VALIDATION_MODE_CAPS)
    vm_caps.update(wl_vm)

    if is_validation_mode(completed_trades):
        caps = dict(risk)
        caps.update(vm_caps)
        caps["validation_mode"] = True
    else:
        caps = dict(risk)
        caps.update(NORMAL_MODE_CAPS)
        caps["validation_mode"] = False

    # Aggressive research-mode overlay (paper-only): widen the hard caps so the
    # agent can propose and execute more for data collection. Cash reserve and
    # per-symbol watchlist allocations are intentionally left untouched.
    rm = research_mode()
    if rm:
        for key in ("max_open_positions", "max_crypto_exposure_pct",
                    "max_single_crypto_pct", "max_altcoin_exposure_pct", "min_buy_score"):
            if rm.get(key) is not None:
                caps[key] = rm[key]
        caps["research_mode"] = True
    else:
        caps["research_mode"] = False
    return caps


# ── Session entry deduplication ──────────────────────────────────────────────

def _session_state_path(date: str = None) -> Path:
    return ROOT / "data" / f"session_state_{date or today_mt()}.json"


def get_session_entries(date: str = None) -> dict:
    """Load today's session entry tracker. Returns {symbol: {count, first_ts, green}}."""
    p = _session_state_path(date)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def record_session_entry(symbol: str, is_green: bool = False, date: str = None):
    """Record that we entered (or added to) a position this session."""
    symbol = normalize_symbol(symbol)
    entries = get_session_entries(date)
    if symbol not in entries:
        entries[symbol] = {"count": 0, "first_ts": datetime.now().isoformat(), "last_green": is_green}
    entries[symbol]["count"] += 1
    entries[symbol]["last_green"] = is_green
    entries[symbol]["last_ts"] = datetime.now().isoformat()
    p = _session_state_path(date)
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def check_duplicate_entry(symbol: str, position_pnl_pct: float = None,
                          date: str = None) -> tuple:
    """
    Returns (allowed: bool, reason: str).
    Rules:
      - 0 previous entries → always allowed (initial entry)
      - 1 previous entry → allowed only if current position is green (+0.3%)
      - 2+ previous entries → blocked
    """
    symbol = normalize_symbol(symbol)
    entries = get_session_entries(date)
    rec = entries.get(symbol)
    if rec is None:
        return True, "Initial entry — no prior session entries"
    count = rec.get("count", 0)

    # Research mode: allow many more re-entries per symbol to gather data, still
    # capped so a single name can't monopolize the book.
    rm = research_mode()
    if rm.get("relax_dedup"):
        cap = int(rm.get("max_entries_per_symbol", 5))
        if count >= cap:
            return False, f"Research-mode dedup cap: {count} entries in {symbol} this session (max {cap})"
        return True, f"Research mode — re-entry allowed ({count} prior entr{'y' if count == 1 else 'ies'})"

    if count >= 2:
        return False, (f"Duplicate-entry cooldown: {count} entries in {symbol} this session "
                       f"(max 1 initial + 1 add allowed)")
    if count == 1:
        if position_pnl_pct is not None and position_pnl_pct >= 0.3:
            return True, f"Scale-in allowed — {symbol} position green ({position_pnl_pct:+.1f}%)"
        return False, (f"Scale-in blocked — {symbol} already entered this session and position "
                       f"is not green (P&L: {position_pnl_pct:+.1f}% if known). "
                       f"Add only allowed when existing position is profitable.")
    return True, "Entry allowed"


# ── Daily P&L stop state ─────────────────────────────────────────────────────

def _daily_state_path() -> Path:
    return ROOT / "data" / f"daily_state_{today_mt()}.json"


def get_daily_state() -> dict:
    """Load or initialize today's daily risk state."""
    p = _daily_state_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "date": today_mt(),
        "opening_equity": None,
        "max_intraday_crypto_pct": 0.0,
        "max_intraday_drawdown_pct": 0.0,
        "soft_stop_active": False,
        "hard_stop_active": False,
        "cap_breaches": [],
        "untracked_entry_count": 0,
        "ambiguous_symbol_count": 0,
        "loss_streak_lockout": False,
    }


def update_daily_state(**kwargs):
    """Update and save daily state."""
    state = get_daily_state()
    state.update(kwargs)
    p = _daily_state_path()
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def check_daily_stop(equity: float, opening_equity: float = None,
                     completed_trades: int = None) -> dict:
    """
    Check whether daily P&L stops are triggered.
    Returns dict with 'soft_stop', 'hard_stop', 'day_pnl_pct', 'message'.
    """
    caps = get_effective_caps(completed_trades)
    state = get_daily_state()

    # Determine the authoritative opening equity:
    # 1. Use state file if it has one (the real opening snapshot from session start)
    # 2. Fall back to the parameter passed in
    # 3. Fall back to current equity (i.e. no drawdown)
    if state.get("opening_equity") is not None:
        opening_equity = float(state["opening_equity"])
    elif opening_equity is None:
        opening_equity = equity
        update_daily_state(opening_equity=opening_equity)
    else:
        # caller passed explicit opening_equity and no state exists yet → persist it
        update_daily_state(opening_equity=opening_equity)

    day_pnl_pct = ((equity - opening_equity) / opening_equity * 100) if opening_equity else 0
    soft_thresh = -abs(caps.get("max_daily_loss_soft_pct", 4))
    hard_thresh = -abs(caps.get("max_daily_loss_hard_pct", 6))

    # Update max intraday drawdown
    current_max_dd = state.get("max_intraday_drawdown_pct", 0.0)
    if day_pnl_pct < current_max_dd:
        update_daily_state(max_intraday_drawdown_pct=day_pnl_pct)

    stops_enforced = daily_stops_enforced()
    soft_stop = day_pnl_pct <= soft_thresh
    hard_stop = day_pnl_pct <= hard_thresh

    if not stops_enforced:
        update_daily_state(
            soft_stop_active=False,
            hard_stop_active=False,
        )
        if hard_stop:
            msg = (f"Paper-trading advisory: day P&L {day_pnl_pct:.2f}% breached the "
                   f"{hard_thresh:.0f}% hard-stop threshold, but daily stops are disabled "
                   f"for paper trading.")
        elif soft_stop:
            msg = (f"Paper-trading advisory: day P&L {day_pnl_pct:.2f}% breached the "
                   f"{soft_thresh:.0f}% soft-stop threshold, but daily stops are disabled "
                   f"for paper trading.")
        else:
            msg = f"Day P&L: {day_pnl_pct:+.2f}% - paper-trading daily stops are disabled."
        return {
            "soft_stop": False,
            "hard_stop": False,
            "day_pnl_pct": round(day_pnl_pct, 3),
            "opening_equity": opening_equity,
            "soft_thresh": soft_thresh,
            "hard_thresh": hard_thresh,
            "message": msg,
            "validation_mode": caps.get("validation_mode", True),
            "stops_enforced": False,
            "paper_trading": is_paper_trading(),
        }

    if soft_stop or hard_stop:
        update_daily_state(
            soft_stop_active=soft_stop,
            hard_stop_active=hard_stop,
        )
        if hard_stop:
            cap_type = "HARD"
            msg = (f"HARD STOP triggered: day P&L {day_pnl_pct:.2f}% ≤ {hard_thresh:.0f}% — "
                   f"reduce-only mode, no new buy/open orders permitted.")
        else:
            cap_type = "SOFT"
            msg = (f"SOFT STOP triggered: day P&L {day_pnl_pct:.2f}% ≤ {soft_thresh:.0f}% — "
                   f"new buy/open orders disabled for today.")
        breaches = state.get("cap_breaches", [])
        if not any(b.get("type") == cap_type for b in breaches):
            breaches.append({"type": cap_type, "pnl_pct": round(day_pnl_pct, 3),
                             "ts": datetime.now().isoformat()})
            update_daily_state(cap_breaches=breaches)
    else:
        msg = f"Day P&L: {day_pnl_pct:+.2f}% — within limits."

    return {
        "soft_stop": soft_stop,
        "hard_stop": hard_stop,
        "day_pnl_pct": round(day_pnl_pct, 3),
        "opening_equity": opening_equity,
        "soft_thresh": soft_thresh,
        "hard_thresh": hard_thresh,
        "message": msg,
        "validation_mode": caps.get("validation_mode", True),
        "stops_enforced": True,
        "paper_trading": is_paper_trading(),
    }


def get_completed_trade_count_safe() -> int:
    """Safe wrapper for dashboard — never raises, returns 0 on any failure."""
    try:
        log = ROOT / "data" / "trade_log.jsonl"
        if not log.exists():
            return 0
        count = 0
        for line in log.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    import json as _j
                    t = _j.loads(line)
                    if t.get("outcome"):
                        count += 1
                except Exception:
                    pass
        return count
    except Exception:
        return 0


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "info"
    if cmd == "info":
        print(json.dumps({
            "now_mt": now_mt_str(),
            "tz": mt_tz_label(),
            "phase": market_phase(),
            "minutes_to_close": minutes_to_close(),
            "risk": load_risk(),
            "live_account": load_live_account(),
        }, indent=2))
    elif cmd == "normalize":
        for s in sys.argv[2:]:
            print(f"{s} -> {normalize_symbol(s)} (crypto={is_crypto(s)})")
