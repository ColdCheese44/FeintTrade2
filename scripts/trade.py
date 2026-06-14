"""Order placement, validation, and market status helpers.

Validation is side-aware: sells reduce exposure and are never blocked by the
cash-reserve or allocation rules. Buys run through layered validation:

  Layer 1 - Per-symbol allocation cap
  Layer 2 - Cash reserve check
  Layer 3 - Max open positions
  Layer 4 - Projected crypto exposure (current positions + pending orders + proposed)
  Layer 5 - Correlated crypto basket cap
  Layer 6 - Validation-mode caps (active until 30 completed trades)
  Layer 7 - Duplicate-entry cooldown
  Layer 8 - Daily stop check (soft/hard)

All rejection messages include actual numbers.
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)

sys.path.insert(0, str(ROOT / "scripts"))
from common import (  # noqa: E402
    CORRELATED_CRYPTO_BASKETS,
    check_duplicate_entry,
    daily_stops_enforced,
    get_daily_state,
    get_effective_caps,
    is_crypto,
    is_option,
    load_options_config,
    load_risk,
    make_http_session,
    normalize_symbol,
    OPTION_CONTRACT_MULTIPLIER,
    option_underlying,
    sector_for,
)

ALPACA_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_BASE_URL", "https://paper-api.alpaca.markets")
REQUEST_TIMEOUT = 15

# Retry-resilient session (rides out VPN tunnel reconnects / transient DNS blips).
# Idempotent GET/DELETE are retried; the order POST is NOT, to avoid double-submits.
_HTTP = make_http_session()

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}


def get_market_status():
    """Check if the market is open."""
    url = f"{BASE_URL}/v2/clock"
    response = _HTTP.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def place_order(symbol, qty, side, limit_price=None):
    """
    Place a buy or sell limit order. Returns Alpaca JSON on success or
    {"error": "..."} on failure without raising.
    """
    crypto = is_crypto(symbol)
    order_symbol = normalize_symbol(symbol)
    order_data = {
        "symbol": order_symbol,
        "qty": str(qty),
        "side": side,
        "type": "limit" if limit_price else "market",
        "time_in_force": "gtc" if crypto else "day",
    }
    if limit_price:
        decimals = 5 if crypto else 2
        order_data["limit_price"] = str(round(float(limit_price), decimals))

    headers = {**HEADERS, "Content-Type": "application/json"}
    url = f"{BASE_URL}/v2/orders"
    try:
        response = _HTTP.post(url, headers=headers, json=order_data, timeout=15)
        if response.status_code >= 400:
            try:
                msg = response.json().get("message", response.text)
            except Exception:
                msg = response.text
            return {"error": f"Alpaca {response.status_code}: {msg}", "submitted": order_data}
        return response.json()
    except Exception as exc:
        return {"error": str(exc), "submitted": order_data}


def cancel_all_orders():
    """Cancel all open orders."""
    url = f"{BASE_URL}/v2/orders"
    response = _HTTP.delete(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    return {"status_code": response.status_code}


def get_orders(status="all"):
    """Get recent orders."""
    url = f"{BASE_URL}/v2/orders"
    params = {"status": status, "limit": 50}
    response = _HTTP.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def cancel_stale_orders(max_age_seconds=90):
    """
    Cancel open orders resting unfilled longer than max_age_seconds. Our orders are
    meant to fill immediately (marketable limits); one still open across a cycle was
    priced off data that has since moved — cancel it so a dangling GTC order (esp. a
    crypto buy) can't fill LATER into a move we no longer want.

    Returns a list of dicts, one per cancelled order:
        {"symbol", "side", "qty", "limit_price", "desc"}
    The structured fields let the caller reconcile entry-tracking — a BUY is logged to
    open_trades.json on ACCEPTANCE, so a cancelled-unfilled buy leaves an orphan entry
    that must be dropped (see orchestrator._cancel_stale_orders). `desc` is the
    human-readable summary for logging. (trade.py deliberately does NOT import learning;
    the orchestrator owns the reconciliation to keep the module boundary clean.)
    """
    from datetime import datetime, timezone
    try:
        orders = _HTTP.get(f"{BASE_URL}/v2/orders", headers=HEADERS,
                           params={"status": "open", "limit": 100}, timeout=REQUEST_TIMEOUT).json()
    except Exception:
        return []
    now = datetime.now(timezone.utc)
    cancelled = []
    for o in orders if isinstance(orders, list) else []:
        try:
            sub = (o.get("submitted_at") or o.get("created_at") or "").replace("Z", "+00:00")
            ts = datetime.fromisoformat(sub) if sub else now
            if (now - ts).total_seconds() < max_age_seconds:
                continue
            r = _HTTP.delete(f"{BASE_URL}/v2/orders/{o['id']}", headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if r.status_code in (200, 204):
                cancelled.append({
                    "symbol":      o.get("symbol"),
                    "side":        str(o.get("side") or "").lower(),
                    "qty":         o.get("qty"),
                    "limit_price": o.get("limit_price"),
                    "desc":        f"{o.get('side')} {o.get('qty')} {o.get('symbol')} @ ${o.get('limit_price')}",
                })
        except Exception:
            continue
    return cancelled


def get_order_fill(order_id, timeout_seconds=4.0, poll_interval=0.75):
    """
    Poll an order until it fills (or reaches a terminal state) or the timeout elapses.
    Returns (filled_qty: float, filled_avg_price: float|None, status: str).

    Marketable limit orders fill within a second or two, so a short poll captures the
    real fill — letting the caller record the entry at the ACTUAL filled qty/avg price
    instead of the requested values (fixes partial-fill qty drift). If nothing fills in
    the window, returns (0.0, None, last_status); the caller falls back to the requested
    values and a still-resting order is later swept by cancel_stale_orders(). Network
    errors are swallowed (best-effort confirmation, never blocks order flow).
    """
    import time
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    last_status = "unknown"
    filled_qty, avg_price = 0.0, None
    terminal = {"filled", "canceled", "cancelled", "rejected", "expired",
                "done_for_day", "stopped", "suspended"}
    while True:
        try:
            r = _HTTP.get(f"{BASE_URL}/v2/orders/{order_id}", headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                o = r.json()
                last_status = o.get("status", last_status)
                filled_qty = float(o.get("filled_qty") or 0)
                if o.get("filled_avg_price"):
                    avg_price = float(o["filled_avg_price"])
                if last_status in terminal:
                    break
        except Exception:
            pass
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_interval)
    return filled_qty, avg_price, last_status


def _crypto_exposure(positions):
    return sum(
        float(p.get("market_value", 0) or 0)
        for p in positions
        if is_crypto(p.get("symbol", ""), p.get("asset_class"))
    )


def _pending_crypto_notional() -> float:
    """
    Sum the notional value of open crypto buy orders so exposure caps account
    for pending fills.
    """
    try:
        url = f"{BASE_URL}/v2/orders"
        response = _HTTP.get(
            url,
            headers=HEADERS,
            params={"status": "open", "limit": 100},
            timeout=10,
        )
        response.raise_for_status()
        total = 0.0
        for order in response.json():
            if order.get("side", "").lower() != "buy":
                continue
            symbol = normalize_symbol(order.get("symbol", ""), order.get("asset_class"))
            if not is_crypto(symbol):
                continue
            qty = float(order.get("qty") or order.get("filled_qty") or 0)
            price = float(order.get("limit_price") or order.get("stop_price") or 0)
            if qty and price:
                total += qty * price
        return total
    except Exception:
        return 0.0


def validate_order(symbol, qty, side, current_price, account, positions,
                   watchlist_limit_pct=10, risk=None, position_pnl_pct=None,
                   check_session_dedup=True, completed_trades=None):
    """
    Pre-flight checks. Returns (ok: bool, message: str).

    account: dict with 'equity' and 'cash' (or a bare numeric string - legacy).
    positions: list of live position dicts (symbol may be raw or normalized).
    position_pnl_pct: unrealized P&L percent of any existing position in this symbol.
    check_session_dedup: set False to skip duplicate-entry cooldown.
    completed_trades: override for validation-mode detection.
    """
    side = str(side).lower()
    qty = float(qty)
    price = float(current_price)
    risk = risk or load_risk()
    caps = get_effective_caps(completed_trades)
    validation_mode = caps.get("validation_mode", True)

    if isinstance(account, dict):
        equity = float(account.get("equity", 100000) or 100000)
        cash = float(account.get("cash", equity) or equity)
    else:
        equity = float(account)
        cash = equity

    positions = positions or []
    sym_norm = normalize_symbol(symbol)
    # Options trade in CONTRACTS of 100 shares, so premium-at-risk = qty x price x 100.
    order_value = qty * price * (OPTION_CONTRACT_MULTIPLIER if is_option(symbol) else 1)

    if side == "sell":
        pos = next(
            (p for p in positions
             if normalize_symbol(p.get("symbol", ""), p.get("asset_class")) == sym_norm),
            None,
        )
        if pos is None:
            return False, f"SELL BLOCKED - no open position in {sym_norm}"
        held = abs(float(pos.get("qty", 0) or 0))
        if held <= 0:
            return False, f"SELL BLOCKED - zero quantity held in {sym_norm}"
        if qty - held > max(held * 0.001, 1e-6):
            return False, f"SELL BLOCKED - sell qty {qty:g} exceeds held {held:g} for {sym_norm}"
        return True, "Sell validated - reduces exposure"

    if qty <= 0:
        return False, f"BUY BLOCKED - computed qty is zero for {sym_norm}"

    if daily_stops_enforced():
        daily = get_daily_state()
        if daily.get("hard_stop_active"):
            hard_limit = caps.get("max_daily_loss_hard_pct", 3)
            return False, (
                f"BUY BLOCKED - HARD STOP active (day P&L hit -{hard_limit:.0f}% limit). "
                f"Reduce-only mode until next session."
            )
        if daily.get("soft_stop_active"):
            soft_limit = caps.get("max_daily_loss_soft_pct", 2)
            return False, (
                f"BUY BLOCKED - SOFT STOP active (day P&L hit -{soft_limit:.0f}% limit). "
                f"No new buy/open orders permitted today."
            )

    # ── Options (long calls/puts) — premium-based caps, not the equity/crypto rules ──
    if is_option(symbol):
        opt = load_options_config()
        if not opt.get("enabled"):
            return False, f"BUY BLOCKED - options trading disabled (options_enabled=false) for {sym_norm}"
        underlying = option_underlying(symbol) or sym_norm
        max_trade = float(opt.get("max_premium_per_trade", 5000))
        if order_value > max_trade + 1e-6:
            return False, (f"BUY BLOCKED - option premium ${order_value:,.2f} exceeds per-trade cap "
                           f"${max_trade:,.0f} ({sym_norm})")
        reserve_frac = risk.get("cash_reserve_pct", 5) / 100.0
        max_spend = cash - equity * reserve_frac
        if order_value > max_spend + 1e-6:
            return False, (f"BUY BLOCKED - option premium ${order_value:,.2f} would breach the "
                           f"{reserve_frac*100:.0f}% cash reserve. Spendable ${max(0, max_spend):,.2f}.")
        per_und_cap = float(opt.get("max_per_underlying_pct", 10)) / 100.0 * equity
        existing_und = sum(abs(float(p.get("market_value", 0) or 0)) for p in positions
                           if is_option(p.get("symbol", ""))
                           and option_underlying(p.get("symbol", "")) == underlying)
        if existing_und + order_value > per_und_cap + 1e-6:
            return False, (f"BUY BLOCKED - {underlying} options would reach "
                           f"${existing_und + order_value:,.2f} > {opt.get('max_per_underlying_pct')}% "
                           f"per-underlying cap (${per_und_cap:,.2f})")
        total_cap = float(opt.get("max_total_exposure_pct", 30)) / 100.0 * equity
        existing_total = sum(abs(float(p.get("market_value", 0) or 0)) for p in positions
                             if is_option(p.get("symbol", "")))
        if existing_total + order_value > total_cap + 1e-6:
            return False, (f"BUY BLOCKED - total options exposure would reach "
                           f"${existing_total + order_value:,.2f} > {opt.get('max_total_exposure_pct')}% "
                           f"cap (${total_cap:,.2f})")
        held_syms = {normalize_symbol(p.get("symbol", ""), p.get("asset_class")) for p in positions}
        max_pos = int(risk.get("max_open_positions", 8))
        if sym_norm not in held_syms and len(held_syms) >= max_pos:
            return False, f"BUY BLOCKED - max open positions ({max_pos}) reached; close something first."
        return True, f"Option buy validated - ${order_value:,.2f} premium ({underlying})"

    # Per-symbol allocation cap — measured on the TOTAL resulting LONG position
    # (existing exposure + this order), not just the new tranche. Checking only
    # order_value let a green scale-in stack a second near-cap add on top of an
    # existing lot and carry a symbol to ~2x its max_allocation_pct, bypassing a hard
    # constraint. (The crypto single/alt caps below already project the combined
    # position; this closes the same gap for every symbol.) Existing SHORT exposure
    # (negative market_value) counts as 0 so a buy-to-COVER is never blocked here.
    existing_long_mv = sum(
        max(0.0, float(p.get("market_value", 0) or 0))
        for p in positions
        if normalize_symbol(p.get("symbol", ""), p.get("asset_class")) == sym_norm
    )
    projected_alloc_pct = ((existing_long_mv + order_value) / equity) * 100 if equity else 999
    if projected_alloc_pct > watchlist_limit_pct + 1e-6:
        detail = (f"order ${order_value:,.2f}" if existing_long_mv <= 0
                  else f"existing ${existing_long_mv:,.2f} + order ${order_value:,.2f}")
        return False, (
            f"BUY BLOCKED - {sym_norm} projected allocation {projected_alloc_pct:.2f}% exceeds "
            f"{watchlist_limit_pct:.1f}% cap "
            f"({detail} vs equity ${equity:,.2f})"
        )

    reserve_frac = risk.get("cash_reserve_pct", 5) / 100.0
    max_spend = cash - equity * reserve_frac
    if order_value > max_spend + 1e-6:
        projected_cash = cash - order_value
        projected_reserve_pct = (projected_cash / equity * 100) if equity else 0
        return False, (
            f"BUY BLOCKED - cash reserve would breach {reserve_frac*100:.0f}% minimum. "
            f"Cash: ${cash:,.2f} | Order: ${order_value:,.2f} | "
            f"Spendable: ${max(0, max_spend):,.2f} | "
            f"Projected reserve: {projected_reserve_pct:.2f}% (min {reserve_frac*100:.0f}%)"
        )

    held_syms = {
        normalize_symbol(p.get("symbol", ""), p.get("asset_class")) for p in positions
    }
    max_pos = int(risk.get("max_open_positions", 8))
    if sym_norm not in held_syms and len(held_syms) >= max_pos:
        return False, (
            f"BUY BLOCKED - max open positions ({max_pos}) reached with "
            f"{len(held_syms)} positions. Close something first."
        )

    # Correlation / concentration cap (HARD RULE #9) — limit distinct OPEN positions in
    # one same-direction sector so several correlated longs aren't really one oversized
    # bet (e.g. TQQQ + SOXL + NVDA = 3 tech longs = max). Adding to a symbol already held
    # is fine (no new position); only OPENING a new name in a capped sector is blocked.
    # Inverse/hedge ETFs and crypto are unmapped (sector_for -> None), so de-risking and
    # the crypto book are never affected.
    sym_sector = sector_for(sym_norm)
    if sym_sector:
        max_sector = int(risk.get("max_same_sector_positions", 3))
        held_in_sector = {
            normalize_symbol(p.get("symbol", ""), p.get("asset_class"))
            for p in positions
            if abs(float(p.get("qty", 0) or 0)) > 0
            and sector_for(p.get("symbol", ""), p.get("asset_class")) == sym_sector
        }
        if sym_norm not in held_in_sector and len(held_in_sector) >= max_sector:
            return False, (
                f"BUY BLOCKED - {sym_norm} would open position #{len(held_in_sector) + 1} in the "
                f"'{sym_sector}' sector; max {max_sector} same-sector positions (correlation cap). "
                f"Already held: {', '.join(sorted(held_in_sector))}"
            )

    if is_crypto(symbol):
        current_crypto_mv = _crypto_exposure(positions)
        pending_crypto_notional = _pending_crypto_notional()
        projected_crypto = current_crypto_mv + pending_crypto_notional + order_value
        projected_crypto_pct = (projected_crypto / equity * 100) if equity else 999

        effective_crypto_cap = caps.get("max_crypto_exposure_pct", 40)
        if projected_crypto_pct > effective_crypto_cap + 1e-6:
            suffix = " [VALIDATION MODE - normal cap 40%]" if validation_mode else ""
            current_crypto_pct = (current_crypto_mv / equity * 100) if equity else 0
            return False, (
                f"BUY BLOCKED - projected crypto exposure {projected_crypto_pct:.2f}% "
                f"exceeds {effective_crypto_cap:.0f}% cap{suffix}. "
                f"Current: ${current_crypto_mv:,.2f} ({current_crypto_pct:.1f}%) | "
                f"Pending orders: ${pending_crypto_notional:,.2f} | "
                f"This order: ${order_value:,.2f}"
            )

        if validation_mode:
            single_cap = caps.get("max_single_crypto_pct", 8)
            pos_obj = next(
                (p for p in positions
                 if normalize_symbol(p.get("symbol", ""), p.get("asset_class")) == sym_norm),
                None,
            )
            existing_mv = float(pos_obj.get("market_value", 0) or 0) if pos_obj else 0
            projected_single = (existing_mv + order_value) / equity * 100 if equity else 999
            if projected_single > single_cap + 1e-6:
                return False, (
                    f"BUY BLOCKED - {sym_norm} projected single-crypto exposure "
                    f"{projected_single:.2f}% exceeds {single_cap:.0f}% cap [VALIDATION MODE]. "
                    f"Current position: ${existing_mv:,.2f} | This order: ${order_value:,.2f}"
                )

            alts = CORRELATED_CRYPTO_BASKETS.get("alts", set())
            if sym_norm in alts:
                alt_cap = caps.get("max_altcoin_exposure_pct", 3)
                if projected_single > alt_cap + 1e-6:
                    return False, (
                        f"BUY BLOCKED - {sym_norm} is an altcoin. Projected exposure "
                        f"{projected_single:.2f}% exceeds {alt_cap:.0f}% altcoin cap [VALIDATION MODE]. "
                        f"Current: ${existing_mv:,.2f} | This order: ${order_value:,.2f}"
                    )

        basket_mv = sum(
            float(p.get("market_value", 0) or 0)
            for p in positions
            if normalize_symbol(p.get("symbol", ""), p.get("asset_class"))
            in CORRELATED_CRYPTO_BASKETS["all_crypto"]
        )
        basket_projected_pct = (
            (basket_mv + pending_crypto_notional + order_value) / equity * 100
            if equity else 999
        )
        basket_cap = effective_crypto_cap
        if basket_projected_pct > basket_cap + 1e-6:
            return False, (
                f"BUY BLOCKED - correlated crypto basket would reach {basket_projected_pct:.2f}% "
                f"(cap {basket_cap:.0f}%). "
                f"Basket size: ${basket_mv:,.2f} | Pending: ${pending_crypto_notional:,.2f} | "
                f"This order: ${order_value:,.2f}"
            )

    if check_session_dedup:
        allowed, dedup_msg = check_duplicate_entry(sym_norm, position_pnl_pct)
        if not allowed:
            return False, f"BUY BLOCKED - {dedup_msg}"

    mode_label = "validation mode" if caps.get("validation_mode") else "normal mode"
    return True, f"Buy validated ({mode_label})"


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"

    if action == "status":
        print(json.dumps(get_market_status()))

    elif action == "order":
        if len(sys.argv) < 5:
            print(json.dumps({"error": "Usage: trade.py order <symbol> <qty> <side> [limit_price]"}))
            sys.exit(1)
        symbol = sys.argv[2]
        qty = sys.argv[3]
        side = sys.argv[4]
        limit_price = sys.argv[5] if len(sys.argv) > 5 else None
        print(json.dumps(place_order(symbol, qty, side, limit_price)))

    elif action == "cancel":
        print(json.dumps(cancel_all_orders()))

    elif action == "orders":
        status_filter = sys.argv[2] if len(sys.argv) > 2 else "all"
        print(json.dumps(get_orders(status_filter)))

    elif action == "validate":
        if len(sys.argv) < 6:
            print(json.dumps({"error": "Usage: trade.py validate <symbol> <qty> <side> <price> [account_json] [positions_json] [limit_pct]"}))
            sys.exit(1)
        symbol = sys.argv[2]
        qty = sys.argv[3]
        side = sys.argv[4]
        price = sys.argv[5]
        acct_arg = sys.argv[6] if len(sys.argv) > 6 else "100000"
        try:
            account = json.loads(acct_arg)
        except Exception:
            account = acct_arg
        positions = json.loads(sys.argv[7]) if len(sys.argv) > 7 else []
        watchlist_limit = float(sys.argv[8]) if len(sys.argv) > 8 else 10
        ok, msg = validate_order(symbol, qty, side, price, account, positions, watchlist_limit)
        print(json.dumps({"valid": ok, "message": msg}))

    else:
        print(json.dumps({"error": f"Unknown action: {action}"}))
        sys.exit(1)
