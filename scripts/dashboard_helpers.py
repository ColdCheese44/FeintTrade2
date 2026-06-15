"""
Pure, testable helpers for the Streamlit dashboard (NO streamlit import here, so they
can be unit-tested without launching the UI). Keep display logic that derives from
config/state in here rather than hardcoded in dashboard.py.
"""


def format_research_banner(rm: dict) -> str:
    """
    Caps line for the research-mode banner, derived from the ACTUAL research_mode config
    (fixes the old hardcoded 'positions 15 · crypto 60% · buy score ≥4' drift).
    """
    rm = rm or {}
    pos = rm.get("max_open_positions", "?")
    crypto = rm.get("max_crypto_exposure_pct", "?")
    score = rm.get("min_buy_score", "?")
    relaxed = []
    if rm.get("disable_loss_streak_lockout"):
        relaxed.append("lockout off")
    if rm.get("disable_validation_mode"):
        relaxed.append("validation caps off")
    if rm.get("relax_dedup"):
        relaxed.append("dedup relaxed")
    if rm.get("disable_force_autobuy") is False:
        relaxed.append("force-autobuy ON")
    relaxed_txt = " · ".join(relaxed) if relaxed else "standard caps"
    return f"{relaxed_txt} · positions {pos} · crypto {crypto}% · buy score ≥{score}"


def _is_crypto_sym(sym, asset_class=None):
    return "/" in str(sym) or str(asset_class or "").lower() == "crypto"


def risk_budget(caps: dict, account: dict, positions: list) -> dict:
    """Risk usage vs caps — cash reserve, open positions, crypto exposure, sector cap.
    Pure: caps from get_effective_caps, account/positions from Alpaca."""
    caps = caps or {}
    equity = float((account or {}).get("equity", 0) or 0)
    cash = float((account or {}).get("cash", 0) or 0)
    positions = positions if isinstance(positions, list) else []
    n = len(positions)
    reserve_pct = caps.get("cash_reserve_pct", 5)
    cash_req = equity * reserve_pct / 100
    crypto_mv = sum(float(p.get("market_value", 0) or 0) for p in positions
                    if _is_crypto_sym(p.get("symbol"), p.get("asset_class")))
    crypto_used = (crypto_mv / equity * 100) if equity else 0
    crypto_cap = caps.get("max_crypto_exposure_pct", 40)
    max_pos = caps.get("max_open_positions", 8)
    return {
        "cash_reserve": {"required": round(cash_req, 2), "available": round(cash, 2),
                         "pct_req": reserve_pct, "ok": cash >= cash_req},
        "positions": {"used": n, "allowed": max_pos, "ok": n <= max_pos},
        "crypto": {"used_pct": round(crypto_used, 1), "cap_pct": crypto_cap, "ok": crypto_used <= crypto_cap},
        "sector_cap": caps.get("max_same_sector_positions", 4),
    }


def _trade_age(trade_id_or_ts):
    import datetime
    import re
    if not trade_id_or_ts:
        return None
    s = str(trade_id_or_ts)
    m = re.search(r"(\d{8})_(\d{6})", s)
    try:
        if m:
            dt = datetime.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        else:
            dt = datetime.datetime.fromisoformat(s.replace("Z", "").split("+")[0])
    except Exception:
        return None
    delta = datetime.datetime.now() - dt
    if delta.days > 0:
        return f"{delta.days}d {delta.seconds // 3600}h"
    return f"{delta.seconds // 3600}h"


def position_console(positions, open_trades=None, peaks=None, stop_pct=3.0) -> list:
    """Enriched per-position rows for the operator console: live P&L + entry thesis
    (setup/conviction/signals/regime), peak P&L, stop, and trade age. Pure."""
    open_trades = open_trades or {}
    peaks = peaks or {}
    rows = []
    for p in (positions if isinstance(positions, list) else []):
        sym = p.get("symbol", "?")
        ot = open_trades.get(sym) or open_trades.get(str(sym).replace("/", "")) or {}
        pk = peaks.get(sym) or peaks.get(str(sym).replace("/", "")) or {}
        entry = float(p.get("avg_entry_price", 0) or 0)
        rows.append({
            "symbol": sym,
            "qty": p.get("qty"),
            "entry": entry,
            "current": float(p.get("current_price", 0) or 0),
            "pnl_pct": round(float(p.get("unrealized_plpc", 0) or 0) * 100, 2),
            "pnl_usd": round(float(p.get("unrealized_pl", 0) or 0), 2),
            "setup": ot.get("setup_type", "untracked"),
            "conviction": ot.get("conviction"),
            "signals": (ot.get("signals") or {}).get("signal_count"),
            "regime_at_entry": ot.get("market_regime"),
            "peak_pct": pk.get("peak"),
            "partialed": bool(pk.get("partialed", False)),
            "stop_pct": -abs(stop_pct),
            "stop_price": round(entry * (1 - abs(stop_pct) / 100), 4) if entry else None,
            "age": _trade_age(ot.get("trade_id") or ot.get("timestamp")),
        })
    return rows


def freshness_label(age_seconds, stale_after=120):
    """('🟢 live' | '🟡 stale' | '🔴 unavailable', color) for a data-fetch age in seconds.
    age_seconds None => unavailable."""
    if age_seconds is None:
        return ("🔴 unavailable", "#ff4d6d")
    if age_seconds <= stale_after:
        return ("🟢 live", "#00d4aa")
    if age_seconds <= stale_after * 5:
        return ("🟡 stale", "#f59e0b")
    return ("🔴 old", "#ff4d6d")
