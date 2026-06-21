"""
Trade Learning System — FeintTrade.

Tracks every trade entry/exit with full signal context, computes performance
statistics, and generates a performance brief for injection into Claude prompts.
The agent reads this brief at the start of every session to continuously improve.

Data files (auto-created):
  data/trade_log.jsonl   — append-only trade log (one JSON object per line)
  data/open_trades.json  — live map: symbol -> pending entry context
  data/performance.json  — cached stats, refreshed each EOD
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT     = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

TRADE_LOG   = DATA_DIR / "trade_log.jsonl"
OPEN_TRADES = DATA_DIR / "open_trades.json"
PERF_CACHE  = DATA_DIR / "performance.json"

# A tracked entry younger than this is NOT auto-closed by the reconciler: cycles
# fetch positions, THEN place buys, then reconcile against that pre-order snapshot,
# so a just-filled buy is momentarily "missing". Real sells log their own exit at the
# point of sale, so this only suppresses fabricated 0-hold round-trips (the phantom
# "$61k BTC buy" wins). Well below the 15-min cycle cadence, so real closes (entered a
# prior cycle, hence older) are still detected.
_RECONCILE_MIN_AGE_SEC = 300

sys.path.insert(0, str(ROOT / "scripts"))
try:
    from common import normalize_symbol, is_option, OPTION_CONTRACT_MULTIPLIER
except Exception:
    def normalize_symbol(s, asset_class=None):  # fallback
        return s

    def is_option(s):  # fallback
        return False

    OPTION_CONTRACT_MULTIPLIER = 100


def _last_fill_price(symbol: str):
    """
    Most recent FILL price for a symbol from Alpaca account activities — used to
    compute accurate realized P&L when a position is detected as closed without an
    explicit exit price. Returns float or None.
    """
    try:
        import requests
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=True)
        base = os.getenv("APCA_BASE_URL", "https://paper-api.alpaca.markets")
        headers = {
            "APCA-API-KEY-ID": os.getenv("APCA_API_KEY_ID"),
            "APCA-API-SECRET-KEY": os.getenv("APCA_API_SECRET_KEY"),
        }
        r = requests.get(f"{base}/v2/account/activities/FILL",
                         headers=headers, params={"direction": "desc", "page_size": 100}, timeout=10)
        r.raise_for_status()
        want = normalize_symbol(symbol)
        for a in r.json():
            if normalize_symbol(a.get("symbol", "")) == want and a.get("price"):
                return float(a["price"])
    except Exception:
        pass
    return None


# ── Internal helpers ──────────────────────────────────────────────────────────

def _load_open_trades() -> dict:
    if OPEN_TRADES.exists():
        try:
            return json.loads(OPEN_TRADES.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_open_trades(data: dict):
    OPEN_TRADES.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_trade_log() -> list:
    if not TRADE_LOG.exists():
        return []
    trades = []
    for line in TRADE_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                trades.append(json.loads(line))
            except Exception:
                pass
    return trades


def _append_trade(trade: dict):
    with open(TRADE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(trade, default=str) + "\n")


def _classify_asset(symbol: str) -> str:
    if "/" in symbol:
        return "crypto"
    if len(symbol) >= 10 and symbol[-1].isdigit():
        return "options"
    if symbol in ("TQQQ", "SOXL", "FNGU", "LABU", "NAIL", "FAS",
                  "SQQQ", "SOXS", "UVXY", "SPXU", "TECS"):
        return "leveraged_etf"
    return "equity"


def _time_of_day() -> str:
    h = datetime.now().hour
    if h < 9:      return "pre_market"
    elif h < 11:   return "open"
    elif h < 13:   return "midday"
    else:          return "close"


# ── Entry logging ─────────────────────────────────────────────────────────────

def log_entry(
    symbol: str,
    side: str,
    qty: float,
    price: float,
    setup_type: str = "unknown",
    conviction: int = 5,
    signals: dict = None,
    regime: str = "NEUTRAL",
    vix: float = None,
    notes: str = "",
):
    """
    Call immediately after an order is successfully placed.
    Stores context in open_trades.json to be matched on exit.
    """
    symbol = normalize_symbol(symbol)
    qty = float(qty)
    price = float(price)
    open_trades = _load_open_trades()
    existing = open_trades.get(symbol)

    # Scale-in: accumulate qty and weighted-average the entry price (don't overwrite)
    if existing and existing.get("side", "buy") == side:
        old_qty = float(existing.get("qty", 0))
        old_px  = float(existing.get("entry_price", price))
        new_qty = old_qty + qty
        wavg    = (old_qty * old_px + qty * price) / new_qty if new_qty else price
        existing.update({
            "qty":         round(new_qty, 10),
            "entry_price": round(wavg, 8),
            "setup_type":  setup_type or existing.get("setup_type", "unknown"),
            "conviction":  conviction,
            "signals":     signals or existing.get("signals", {}),
            "scale_ins":   int(existing.get("scale_ins", 0)) + 1,
            "last_add":    datetime.now().isoformat(),
            "notes":       notes or existing.get("notes", ""),
        })
        open_trades[symbol] = existing
        _save_open_trades(open_trades)
        return existing

    entry = {
        "trade_id":        f"{symbol.replace('/','')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        "symbol":          symbol,
        "side":            side,
        "qty":             qty,
        "entry_price":     price,
        "setup_type":      setup_type,
        "conviction":      conviction,
        "signals":         signals or {},
        "market_regime":   regime,
        "vix":             vix,
        "asset_type":      _classify_asset(symbol),
        "time_of_day":     _time_of_day(),
        "day_of_week":     datetime.now().strftime("%A"),
        "timestamp_entry": datetime.now().isoformat(),
        "scale_ins":       0,
        "notes":           notes,
    }
    open_trades[symbol] = entry
    _save_open_trades(open_trades)
    return entry


def forget_unfilled_entry(symbol: str, live_position_symbols=None) -> bool:
    """
    Drop a tracked BUY entry that was logged on broker ACCEPTANCE but never became a
    live position — e.g. a non-marketable limit buy that trade.cancel_stale_orders()
    later cancelled while still unfilled. Without this, the orphaned open_trades.json
    entry is fabricated into a phantom round-trip exit by detect_and_log_exits() once it
    ages past the recency guard (the fake "$61k BTC buy" +9–10% wins).

    Called by the orchestrator right after trade.cancel_stale_orders() returns a
    cancelled BUY. trade.py deliberately does NOT import learning, so the orchestrator
    (which imports both) owns this reconciliation — keeping the module boundary clean.

    Safety guards:
      - Only BUY-side entries are removed (a sell de-risks and logs its own exit).
      - If live_position_symbols is provided and the symbol IS held, the entry belongs
        to a real position (e.g. a scale-in whose base lot filled) — leave it untouched.

    Returns True if an orphan entry was removed, else False.
    """
    symbol = normalize_symbol(symbol)
    if live_position_symbols is not None:
        held = {normalize_symbol(s) for s in live_position_symbols}
        if symbol in held:
            return False
    open_trades = _load_open_trades()
    entry = open_trades.get(symbol)
    if not entry or str(entry.get("side", "buy")).lower() != "buy":
        return False
    del open_trades[symbol]
    _save_open_trades(open_trades)
    return True


# ── Exit reason taxonomy ─────────────────────────────────────────────────────
EXIT_REASONS = {
    "stop_loss",          # hit the regime stop-loss threshold
    "take_profit",        # hit the defined target
    "trailing_stop",      # trailing stop triggered
    "timeout",            # eod / session time limit
    "manual_derisk",      # operator-initiated de-risk
    "thesis_invalidated", # invalidating signal detected
    "system_correction",  # system forced liquidation (e.g. cap breach)
    "unknown_legacy",     # historical records without a known reason
    "eod_close",          # end-of-day equity flatten
    "partial_profit",     # partial take-profit (up 8%)
    "crypto_cycle_exit",  # crypto cycle detected the close
    "intraday_exit",      # intraday check detected the close
    "overnight",          # overnight session detection
    "sell",               # generic sell (legacy alias → maps to manual_derisk)
    "target_hit",         # explicit target hit (alias for take_profit)
    "cycle_exit",         # generic cycle exit
    "pre_session_check",  # pre-session exit detection
    "reconciled_partial", # tracked lot shrank to match the broker (late/external partial fill)
}

_REASON_ALIASES = {
    "sell":            "manual_derisk",
    "target_hit":      "take_profit",
    "cycle_exit":      "system_correction",
    "pre_session_check": "system_correction",
    "intraday_exit_noprice": "intraday_exit",
    "eod_close_noprice":     "eod_close",
    "overnight_or_prior_session": "overnight",
}


def _normalize_exit_reason(reason: str) -> str:
    """Return a canonical exit reason; fall back to 'unknown_legacy' for unrecognized values."""
    r = str(reason or "unknown_legacy").strip().lower()
    r = _REASON_ALIASES.get(r, r)
    if r.endswith("_noprice"):
        base = r[:-len("_noprice")]
        r = _REASON_ALIASES.get(base, base if base in EXIT_REASONS else "unknown_legacy")
    if r not in EXIT_REASONS:
        return "unknown_legacy"
    return r


def log_exit(
    symbol: str,
    exit_price: float,
    exit_reason: str = "unknown_legacy",
    notes: str = "",
    qty: float = None,
    entry_price: float = None,
):
    """
    Record a (possibly partial) exit. Matches against open_trades, computes P&L on
    the exited quantity, appends the completed trade to trade_log.jsonl, and either
    reduces the open lot (partial) or removes it (full close).

    entry_price: broker cost-basis fallback. If the symbol has no tracked entry
    (tracking desync, or a manual/external buy), the caller can pass the position's
    avg_entry_price so the completed trade is still recorded with real P&L instead of
    being silently dropped.

    exit_reason options:
      target_hit | stop_loss | eod_close | partial_profit | manual | regime_change | sell
    """
    symbol = normalize_symbol(symbol)
    open_trades = _load_open_trades()
    entry = open_trades.get(symbol)
    if not entry:
        if entry_price and qty:
            entry = {
                "trade_id":        f"{symbol.replace('/','')}_reco_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                "symbol":          symbol,
                "side":            "buy",
                "qty":             float(qty),
                "entry_price":     float(entry_price),
                "setup_type":      "reconstructed",
                "conviction":      None,
                "signals":         {},
                "market_regime":   None,
                "vix":             None,
                "asset_type":      _classify_asset(symbol),
                "time_of_day":     _time_of_day(),
                "day_of_week":     datetime.now().strftime("%A"),
                "timestamp_entry": datetime.now().isoformat(),
            }
        else:
            return None  # No open entry tracked and no cost-basis fallback

    entry_price = float(entry["entry_price"])
    side = entry.get("side", "buy")
    held = float(entry.get("qty", 0))
    exit_price = float(exit_price)
    sell_qty = held if qty is None else min(float(qty), held)
    if sell_qty <= 0:
        return None

    entry_ts = datetime.fromisoformat(entry["timestamp_entry"])
    exit_ts = datetime.now()
    hold_minutes = round((exit_ts - entry_ts).total_seconds() / 60, 1)

    # Options trade in contracts of 100 shares — dollar P&L must use the 100x multiplier
    # (percent P&L is a ratio, so it's unaffected).
    mult = OPTION_CONTRACT_MULTIPLIER if is_option(symbol) else 1
    if side == "buy":
        pnl_dollar = (exit_price - entry_price) * sell_qty * mult
        pnl_pct    = (exit_price - entry_price) / entry_price * 100 if entry_price else 0
    else:  # short
        pnl_dollar = (entry_price - exit_price) * sell_qty * mult
        pnl_pct    = (entry_price - exit_price) / entry_price * 100 if entry_price else 0

    outcome = "win" if pnl_pct > 0.05 else "loss" if pnl_pct < -0.05 else "breakeven"
    partial = sell_qty < held - 1e-9

    canonical_reason = _normalize_exit_reason(exit_reason)

    trade = {
        **entry,
        "qty":            round(sell_qty, 10),
        "exit_price":     exit_price,
        "pnl_dollar":     round(pnl_dollar, 2),
        "pnl_pct":        round(pnl_pct, 3),
        "hold_minutes":   hold_minutes,
        "outcome":        outcome,
        "exit_reason":    canonical_reason,
        "exit_reason_raw": exit_reason,
        "partial":        partial,
        "timestamp_exit": exit_ts.isoformat(),
        "exit_notes":     notes,
    }
    _append_trade(trade)

    if partial:
        entry["qty"] = round(held - sell_qty, 10)
        open_trades[symbol] = entry
        _save_open_trades(open_trades)
    elif symbol in open_trades:          # reconstructed entries aren't in open_trades
        del open_trades[symbol]
        _save_open_trades(open_trades)
    return trade


def reconcile_untracked_positions(current_positions: list) -> list:
    """
    Reverse reconciliation: backfill a tracked entry for any position HELD at the
    broker but ABSENT from open_trades.json.

    detect_and_log_exits() only handles the tracked→held direction (a tracked lot
    that closed/shrank). The opposite — a live position that was never tracked
    (its entry was opened via a path that skipped log_entry, or it is a residual
    after a logged exit) — is invisible to the learning loop. Its eventual close
    then logs as a context-free "reconstructed" exit (setup_type/regime/entry
    lost), so the setup-level stats that drive strategy under-count the real book.

    Anchor a tracked entry from broker truth (avg_entry_price + live qty) tagged
    setup_type="reconstructed_entry" so the exit attributes cleanly with correct
    P&L. timestamp_entry is set to now (the true entry time is unknown), so
    hold-time for these is approximate — `reconstructed: True` flags that. Real
    entries always log richer context at the point of purchase; this only catches
    the few that slip through. Returns the list of backfilled symbols.
    """
    if not current_positions:
        return []
    open_trades = _load_open_trades()
    backfilled = []
    for p in current_positions:
        sym = normalize_symbol(p.get("symbol", ""), p.get("asset_class"))
        if not sym or sym in open_trades:
            continue
        try:
            qty = abs(float(p.get("qty", 0) or 0))
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            continue
        try:
            px = float(p.get("avg_entry_price", 0) or p.get("entry_price", 0) or 0)
        except (TypeError, ValueError):
            px = 0.0
        if px <= 0:
            px = _last_fill_price(sym) or 0.0
        if px <= 0:
            continue  # can't anchor an entry without a price
        open_trades[sym] = {
            "trade_id":        f"{sym.replace('/','')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_recon",
            "symbol":          sym,
            "side":            "buy",
            "qty":             qty,
            "entry_price":     px,
            "setup_type":      "reconstructed_entry",
            "conviction":      None,
            "signals":         {},
            "market_regime":   "UNKNOWN",
            "vix":             None,
            "asset_type":      _classify_asset(sym),
            "time_of_day":     _time_of_day(),
            "day_of_week":     datetime.now().strftime("%A"),
            "timestamp_entry": datetime.now().isoformat(),
            "scale_ins":       0,
            "notes":           "backfilled from broker — held but untracked (entry log skipped/residual)",
            "reconstructed":   True,
        }
        backfilled.append(sym)
    if backfilled:
        _save_open_trades(open_trades)
    return backfilled


def detect_and_log_exits(current_positions: list, exit_reason: str = "eod_close",
                         price_lookup: dict = None) -> list:
    """
    Compare current positions against open_trades. Any tracked symbol no longer in
    positions has closed — record it with real P&L (from price_lookup, else the most
    recent Alpaca fill). Falls back to entry price only if no fill data exists.
    Returns the list of symbols detected as closed.
    """
    open_trades = _load_open_trades()
    if not open_trades:
        return []

    # GUARD: an empty positions list almost always means a failed/empty fetch, not
    # that the whole book vanished at once. Concluding "everything exited" here would
    # wipe tracked entries and make live positions "untracked" (corrupting the learning
    # loop + logging fake exits). Real sells log their own exits at the point of sale,
    # so require a non-empty snapshot before auto-detecting closes.
    if not current_positions:
        return []

    current_syms = {normalize_symbol(p.get("symbol", ""), p.get("asset_class"))
                    for p in current_positions}
    # Live held qty per symbol — the broker is the source of truth for size.
    current_qty = {}
    for p in current_positions:
        s = normalize_symbol(p.get("symbol", ""), p.get("asset_class"))
        try:
            current_qty[s] = current_qty.get(s, 0.0) + abs(float(p.get("qty", 0) or 0))
        except (TypeError, ValueError):
            pass
    price_lookup = {normalize_symbol(k): v for k, v in (price_lookup or {}).items()}
    closed = []

    def _recent(entry):
        ts = entry.get("last_add") or entry.get("timestamp_entry")
        if not ts:
            return False
        try:
            return 0 <= (datetime.now() - datetime.fromisoformat(ts)).total_seconds() < _RECONCILE_MIN_AGE_SEC
        except Exception:
            return False

    for sym in list(open_trades.keys()):
        entry = open_trades.get(sym, {})
        if sym in current_syms:
            # Position still open — but reconcile a tracked lot LARGER than the live held
            # qty. A sell that was accepted-but-unfilled inside the order-poll window (then
            # filled later), or an external/partial fill, shrinks the broker position WITHOUT
            # logging an exit, leaving open_trades over-counting (the FAS 53-vs-11 bug). Log
            # the missing partial at the real mark and shrink tracked qty to the broker truth.
            try:
                tracked = float(entry.get("qty", 0) or 0)
            except (TypeError, ValueError):
                tracked = 0.0
            live = current_qty.get(sym, 0.0)
            gap = tracked - live
            if live > 0 and gap > max(1e-6, tracked * 0.005) and not _recent(entry):
                px = price_lookup.get(sym) or _last_fill_price(sym) or entry.get("entry_price")
                if px:
                    # NOT a full close — the symbol is still held, so it is deliberately
                    # NOT added to `closed`; this only realigns the tracked lot + logs P&L.
                    log_exit(sym, float(px), "reconciled_partial",
                             notes=f"reconciled tracked {tracked:g} → live {live:g} (late/external partial)",
                             qty=gap)
            continue
        # GUARD: a position opened moments ago can be absent from a snapshot captured
        # before the order filled (the cycle fetches positions, THEN buys, then
        # reconciles against that stale list). Don't fabricate a 0-hold round-trip for
        # it — this is what produced the phantom "$61k BTC buy" wins (entry + exit in
        # the same second). Real sells log their own exit at the point of sale.
        if _recent(entry):
            continue
        px = price_lookup.get(sym) or _last_fill_price(sym)
        if px:
            log_exit(sym, float(px), exit_reason, notes="auto-detected close")
        else:
            log_exit(sym, float(entry.get("entry_price", 0)),
                     exit_reason + "_noprice", notes="exit price unavailable")
        closed.append(sym)

    return closed


# ── Performance stats ─────────────────────────────────────────────────────────

def _collapse_to_positions(trades: list) -> list:
    """Collapse partial-exit rows into one record per position (by trade_id).

    The trade log appends one row per (partial) exit, so a single scaled-out
    position becomes many rows — counting each as a separate "trade" inflates
    win rate and trade counts (a 1-position FAS scale-out read as "7 trades,
    100% WR"; a 3-tranche SOXS loss read as 3 losses). Performance stats and
    recommendations must be position-level: one round-trip = one trade.

    Rows are grouped by trade_id; dollar P&L sums, percent P&L is
    notional-weighted across tranches (entry_price x qty, x100 for options),
    the outcome is recomputed from the net (same +/-0.05% bands as log_exit),
    and hold time is the longest tranche. Rows without a trade_id (legacy or
    stubbed) each stand alone, so existing behavior is preserved. Chronological
    order (by final exit timestamp) is kept for streak/recent-N logic.
    """
    groups, standalone = {}, []
    order = []
    for t in trades:
        tid = t.get("trade_id")
        if not tid:
            standalone.append([t])
            continue
        if tid not in groups:
            groups[tid] = []
            order.append(tid)
        groups[tid].append(t)

    positions = []
    for rows in [groups[tid] for tid in order] + standalone:
        if len(rows) == 1:
            positions.append(rows[0])
            continue
        rows_sorted = sorted(rows, key=lambda r: r.get("timestamp_exit") or "")
        last = rows_sorted[-1]
        net_dollar = sum(r.get("pnl_dollar", 0) or 0 for r in rows_sorted)
        mult = OPTION_CONTRACT_MULTIPLIER if is_option(last.get("symbol", "")) else 1
        notional = sum((r.get("entry_price", 0) or 0) * (r.get("qty", 0) or 0) * mult
                       for r in rows_sorted)
        net_pct = (round(net_dollar / notional * 100, 3) if notional
                   else round(sum(r.get("pnl_pct", 0) or 0 for r in rows_sorted), 3))
        outcome = "win" if net_pct > 0.05 else "loss" if net_pct < -0.05 else "breakeven"
        positions.append({
            **last,
            "pnl_dollar":   round(net_dollar, 2),
            "pnl_pct":      net_pct,
            "outcome":      outcome,
            "partial":      False,
            "hold_minutes": max((r.get("hold_minutes", 0) or 0) for r in rows_sorted),
            "tranches":     len(rows_sorted),
        })

    positions.sort(key=lambda p: p.get("timestamp_exit") or "")
    return positions


def compute_stats(trades: list = None) -> dict:
    """Full performance statistics from the trade log (position-level)."""
    if trades is None:
        trades = _load_trade_log()

    # Collapse partial-exit rows so one scaled-out position counts as one trade,
    # not one trade per tranche (see _collapse_to_positions).
    trades = _collapse_to_positions(trades)

    completed = [t for t in trades if t.get("outcome") and t.get("pnl_pct") is not None]
    if not completed:
        return {"total_trades": 0, "message": "No completed trades yet."}

    wins       = [t for t in completed if t["outcome"] == "win"]
    losses     = [t for t in completed if t["outcome"] == "loss"]
    total      = len(completed)
    win_rate   = len(wins) / total * 100 if total else 0
    avg_win    = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss   = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    total_pnl  = sum(t.get("pnl_dollar", 0) for t in completed)

    gross_profit = sum(t.get("pnl_dollar", 0) for t in wins if t.get("pnl_dollar", 0) > 0)
    gross_loss   = abs(sum(t.get("pnl_dollar", 0) for t in losses if t.get("pnl_dollar", 0) < 0))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None

    # Expectancy per trade (% terms): what you expect to make on an average trade.
    wr = len(wins) / total if total else 0
    expectancy_pct = round(wr * avg_win + (1 - wr) * avg_loss, 3)

    # Per-setup stats
    setup_map = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "trades": 0})
    for t in completed:
        s = setup_map[t.get("setup_type", "unknown")]
        s["trades"] += 1
        s["pnl"] += t.get("pnl_dollar", 0)
        s["wins" if t["outcome"] == "win" else "losses"] += 1

    by_setup = {
        k: {
            "win_rate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0,
            "trades":   v["trades"],
            "total_pnl": round(v["pnl"], 2),
        }
        for k, v in setup_map.items()
    }

    # Per-symbol stats
    sym_map = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "trades": 0})
    for t in completed:
        s = sym_map[t.get("symbol", "?")]
        s["trades"] += 1
        s["pnl"] += t.get("pnl_dollar", 0)
        s["wins" if t["outcome"] == "win" else "losses"] += 1

    by_symbol = {
        k: {
            "win_rate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0,
            "trades":   v["trades"],
            "total_pnl": round(v["pnl"], 2),
        }
        for k, v in sym_map.items()
    }

    # Per-regime stats
    regime_map = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "trades": 0})
    for t in completed:
        s = regime_map[t.get("market_regime", "UNKNOWN")]
        s["trades"] += 1
        s["pnl"] += t.get("pnl_dollar", 0)
        s["wins" if t["outcome"] == "win" else "losses"] += 1

    by_regime = {
        k: {
            "win_rate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0,
            "trades":   v["trades"],
            "total_pnl": round(v["pnl"], 2),
        }
        for k, v in regime_map.items()
    }

    # Per-time-of-day stats
    tod_map = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "trades": 0})
    for t in completed:
        s = tod_map[t.get("time_of_day", "unknown")]
        s["trades"] += 1
        s["pnl"] += t.get("pnl_dollar", 0)
        s["wins" if t["outcome"] == "win" else "losses"] += 1

    by_time_of_day = {
        k: {
            "win_rate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0,
            "trades":   v["trades"],
            "total_pnl": round(v["pnl"], 2),
        }
        for k, v in tod_map.items()
    }

    # Streak detection
    streak, streak_type = 0, None
    for t in reversed(completed):
        if t["outcome"] not in ("win", "loss"):
            continue
        if streak_type is None:
            streak_type = t["outcome"]
        if t["outcome"] == streak_type:
            streak += 1
        else:
            break

    # Recent 10
    recent = completed[-10:]
    recent_wins = sum(1 for t in recent if t["outcome"] == "win")

    # Hold time
    hold_times = [t.get("hold_minutes", 0) for t in completed if t.get("hold_minutes")]
    avg_hold_minutes = round(sum(hold_times) / len(hold_times), 1) if hold_times else None

    return {
        "total_trades":       total,
        "win_rate":           round(win_rate, 1),
        "avg_win_pct":        round(avg_win, 2),
        "avg_loss_pct":       round(avg_loss, 2),
        "profit_factor":      profit_factor,
        "expectancy_pct":     expectancy_pct,
        "total_pnl":          round(total_pnl, 2),
        "avg_hold_minutes":   avg_hold_minutes,
        "best_trade":         max(completed, key=lambda t: t.get("pnl_pct", 0)),
        "worst_trade":        min(completed, key=lambda t: t.get("pnl_pct", 0)),
        "current_streak":     {"count": streak, "type": streak_type or "none"},
        "recent_10_win_rate": round(recent_wins / len(recent) * 100, 1) if recent else 0,
        "by_setup":           by_setup,
        "by_symbol":          by_symbol,
        "by_regime":          by_regime,
        "by_time_of_day":     by_time_of_day,
        "updated_at":         datetime.now().isoformat(),
    }


def update_performance() -> dict:
    """Recompute and cache performance stats. Call at EOD."""
    stats = compute_stats()
    PERF_CACHE.write_text(json.dumps(stats, indent=2, default=str), encoding="utf-8")
    return stats


def get_performance_brief() -> str:
    """
    Returns a formatted string to inject into Claude prompts.
    This is the agent's self-knowledge — it reads its own history to improve.
    """
    # Use the EOD cache only when it is at least as fresh as the trade log; otherwise
    # trades logged since the last refresh (e.g. overnight crypto exits) would be
    # invisible and the agent would read a stale "0 trades" brief. compute_stats() is
    # cheap, so recompute whenever the log has moved past the cache.
    stats = None
    if PERF_CACHE.exists():
        try:
            cache_fresh = (not TRADE_LOG.exists()
                           or PERF_CACHE.stat().st_mtime >= TRADE_LOG.stat().st_mtime)
            if cache_fresh:
                stats = json.loads(PERF_CACHE.read_text(encoding="utf-8"))
        except Exception:
            stats = None
    if stats is None:
        stats = compute_stats()

    if stats.get("total_trades", 0) == 0:
        return "=== PERFORMANCE BRIEF ===\nNo completed trades yet. Begin with default strategy parameters."

    lines = [
        f"=== AGENT PERFORMANCE BRIEF (as of {stats.get('updated_at','?')[:10]}) ===",
        f"Total trades: {stats['total_trades']} | Overall win rate: {stats['win_rate']}% | Profit factor: {stats.get('profit_factor', 'N/A')}",
        f"Avg win: +{stats['avg_win_pct']}% | Avg loss: {stats['avg_loss_pct']}% | Expectancy/trade: {stats.get('expectancy_pct','?')}% | Total P&L: ${stats['total_pnl']:+,.2f}",
        f"Avg hold time: {stats.get('avg_hold_minutes','?')} min | Recent 10-trade win rate: {stats['recent_10_win_rate']}%",
        f"Current streak: {stats['current_streak']['count']}x {stats['current_streak']['type']}",
        "",
        "--- BY SETUP TYPE ---",
    ]
    for setup, s in sorted(stats.get("by_setup", {}).items(), key=lambda x: x[1]["total_pnl"], reverse=True):
        lines.append(f"  {setup:<28} {s['win_rate']:>5.1f}% WR | {s['trades']:>3} trades | ${s['total_pnl']:>+9,.2f}")

    lines.append("\n--- BY SYMBOL ---")
    for sym, s in sorted(stats.get("by_symbol", {}).items(), key=lambda x: x[1]["total_pnl"], reverse=True):
        lines.append(f"  {sym:<12} {s['win_rate']:>5.1f}% WR | {s['trades']:>3} trades | ${s['total_pnl']:>+9,.2f}")

    lines.append("\n--- BY MARKET REGIME ---")
    for regime, s in stats.get("by_regime", {}).items():
        lines.append(f"  {regime:<10} {s['win_rate']:>5.1f}% WR | {s['trades']:>3} trades | ${s['total_pnl']:>+9,.2f}")

    lines.append("\n--- BY TIME OF DAY ---")
    for tod, s in stats.get("by_time_of_day", {}).items():
        lines.append(f"  {tod:<12} {s['win_rate']:>5.1f}% WR | {s['trades']:>3} trades | ${s['total_pnl']:>+9,.2f}")

    best  = stats.get("best_trade", {})
    worst = stats.get("worst_trade", {})
    if best:
        lines.append(f"\nBest trade:  {best.get('symbol')} {best.get('pnl_pct', 0):+.2f}% | {best.get('setup_type')} | {best.get('timestamp_entry','')[:10]}")
    if worst:
        lines.append(f"Worst trade: {worst.get('symbol')} {worst.get('pnl_pct', 0):+.2f}% | {worst.get('setup_type')} | {worst.get('timestamp_entry','')[:10]}")

    return "\n".join(lines)


def get_strategy_recommendations() -> str:
    """
    Data-driven strategy adjustments derived from trade history.
    Injected into research prompts so the agent learns from its mistakes.
    """
    trades = _load_trade_log()
    if len(trades) < 3:
        return "LEARNING: Fewer than 3 completed trades — insufficient data. Execute default SOP."

    stats = compute_stats(trades)
    recs = ["=== STRATEGY RECOMMENDATIONS (data-driven) ==="]

    wr = stats.get("win_rate", 0)
    pf = stats.get("profit_factor")
    streak = stats.get("current_streak", {})

    if wr < 35:
        recs.append("⚠️  Win rate below 35% — REQUIRE 4+ signals aligned before entering (raise bar).")
    elif wr < 45:
        recs.append("⚠️  Win rate below 45% — tighten entry criteria, skip marginal setups.")
    elif wr > 65:
        recs.append("✅ Win rate >65% — strategy working. Consider sizing up on 8+ conviction scores.")

    if pf and pf < 1.0:
        recs.append("⚠️  Profit factor <1.0 — cut losses faster. Use -3% stop instead of -5%.")
    elif pf and pf > 2.0:
        recs.append("✅ Profit factor >2.0 — excellent. Let winners run slightly longer before scaling out.")

    if streak.get("type") == "loss" and streak.get("count", 0) >= 3:
        recs.append(f"🛑 {streak['count']}-trade losing streak — reduce ALL position sizes by 50% until streak breaks.")
    elif streak.get("type") == "loss" and streak.get("count", 0) == 2:
        recs.append("⚠️  2 consecutive losses — be selective. Only enter 8+ conviction setups next trade.")

    # Best/worst setups
    setup_perf = stats.get("by_setup", {})
    if setup_perf:
        sorted_setups = sorted(setup_perf.items(), key=lambda x: x[1]["total_pnl"], reverse=True)
        best_s = sorted_setups[0]
        worst_s = sorted_setups[-1]
        if best_s[1]["trades"] >= 3:
            recs.append(f"📈 BEST SETUP: '{best_s[0]}' — {best_s[1]['win_rate']}% WR, ${best_s[1]['total_pnl']:+,.2f}. PRIORITIZE this setup.")
        if worst_s[1]["trades"] >= 3 and worst_s[1]["total_pnl"] < -100:
            # Escalate from advisory to a hard STOP when a setup is the dominant, repeated
            # loss source (≥5 trades, sub-40% WR, ≤ -$1k). Data-driven + self-updating — no
            # hardcoded setup name, so it can't overfit or go stale. (momentum_breakout hit
            # this bar: 33% WR over 9 trades, -$3.3k, incl. a -5% SOXL gap-through-stop.)
            w = worst_s[1]
            if w["trades"] >= 5 and w["win_rate"] < 40 and w["total_pnl"] <= -1000:
                recs.append(f"🛑 STOP SETUP: '{worst_s[0]}' is the dominant loss source — "
                            f"{w['win_rate']}% WR over {w['trades']} trades, ${w['total_pnl']:+,.2f}. "
                            f"Do NOT open new trades with this setup; use a proven one "
                            f"(e.g. '{best_s[0]}') instead.")
            else:
                recs.append(f"📉 WORST SETUP: '{worst_s[0]}' — losing ${abs(w['total_pnl']):,.2f}. REDUCE size or skip.")

    # Best/worst symbols
    sym_perf = stats.get("by_symbol", {})
    if sym_perf:
        sorted_syms = sorted(sym_perf.items(), key=lambda x: x[1]["total_pnl"], reverse=True)
        if sorted_syms[0][1]["trades"] >= 4 and sorted_syms[0][1]["total_pnl"] > 0:
            recs.append(f"📈 BEST SYMBOL: {sorted_syms[0][0]} — {sorted_syms[0][1]['win_rate']}% WR. Consider higher allocation within rules.")
        if sorted_syms[-1][1]["total_pnl"] < -200 and sorted_syms[-1][1]["trades"] >= 4:
            recs.append(f"📉 WORST SYMBOL: {sorted_syms[-1][0]} — losing. Avoid or reduce to min size until pattern improves.")

    # Best time of day
    tod_perf = stats.get("by_time_of_day", {})
    if tod_perf:
        best_tod = max(tod_perf.items(), key=lambda x: x[1]["win_rate"])
        if best_tod[1]["trades"] >= 3 and best_tod[1]["total_pnl"] > 0:
            recs.append(f"⏰ BEST TIME: '{best_tod[0]}' has {best_tod[1]['win_rate']}% WR — prioritize entries in this window.")

    if len(recs) == 1:
        recs.append("✅ Performance within normal parameters. Continue current strategy execution.")

    return "\n".join(recs)


# ── Loss-streak lockout helpers ──────────────────────────────────────────────

def get_loss_streak() -> dict:
    """Return current streak info: {'count': N, 'type': 'loss'|'win'|'none'}.

    Position-level: a scaled-out winner/loser is one streak entry, not one per
    partial-exit tranche (otherwise the loss-streak lockout could trip on the
    partials of a single losing position).
    """
    trades = _collapse_to_positions(_load_trade_log())
    completed = [t for t in trades if t.get("outcome") in ("win", "loss")]
    streak, stype = 0, "none"
    for t in reversed(completed):
        if stype == "none":
            stype = t["outcome"]
        if t["outcome"] == stype:
            streak += 1
        else:
            break
    return {"count": streak, "type": stype}


def is_loss_streak_locked(threshold: int = 2) -> tuple:
    """
    Returns (locked: bool, message: str).
    Locked when current consecutive-loss streak >= threshold.
    Default threshold = 2 (from validation SOP).
    """
    s = get_loss_streak()
    if s["type"] == "loss" and s["count"] >= threshold:
        return True, (
            f"LOSS-STREAK LOCKOUT — {s['count']} consecutive losses. "
            f"New entries disabled until manual review clears the lockout. "
            f"You can override by calling clear_loss_streak_lockout() or !resume in Discord."
        )
    return False, f"Streak: {s['count']}x {s['type']}"


def clear_loss_streak_lockout():
    """Write a sentinel that clears the lockout for the current session."""
    DATA_DIR.mkdir(exist_ok=True)
    lock_file = DATA_DIR / "loss_streak_lockout_cleared.json"
    lock_file.write_text(
        json.dumps({"cleared_at": datetime.now().isoformat(), "cleared": True}, indent=2),
        encoding="utf-8",
    )


def lockout_manually_cleared() -> bool:
    """True if the operator has cleared the lockout this session."""
    lock_file = DATA_DIR / "loss_streak_lockout_cleared.json"
    if not lock_file.exists():
        return False
    try:
        data = json.loads(lock_file.read_text(encoding="utf-8"))
        cleared_at = datetime.fromisoformat(data.get("cleared_at", ""))
        from common import today_mt
        return cleared_at.strftime("%Y-%m-%d") == today_mt()
    except Exception:
        return False


def get_completed_trade_count() -> int:
    """Count completed trades (for validation-mode detection)."""
    trades = _load_trade_log()
    return len([t for t in trades if t.get("outcome")])


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Prevent UnicodeEncodeError on Windows cp1252 consoles when output contains emoji.
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    cmd = sys.argv[1] if len(sys.argv) > 1 else "brief"
    if cmd == "brief":
        print(get_performance_brief())
    elif cmd == "stats":
        print(json.dumps(compute_stats(), indent=2, default=str))
    elif cmd == "update":
        s = update_performance()
        print(json.dumps(s, indent=2, default=str))
    elif cmd == "recommendations":
        print(get_strategy_recommendations())
    elif cmd == "open":
        print(json.dumps(_load_open_trades(), indent=2))
    elif cmd == "log_entry":
        sym, side, qty, price = sys.argv[2], sys.argv[3], float(sys.argv[4]), float(sys.argv[5])
        log_entry(sym, side, qty, price, setup_type="test")
        print(f"Logged entry: {sym} {side} {qty} @ {price}")
    elif cmd == "log_exit":
        sym, price, reason = sys.argv[2], float(sys.argv[3]), sys.argv[4] if len(sys.argv) > 4 else "manual"
        result = log_exit(sym, price, reason)
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
