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
import hashlib
import os
import sys
from datetime import datetime, timezone
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
    load_live_account,
    load_options_config,
    load_risk,
    make_http_session,
    normalize_symbol,
    OPTION_CONTRACT_MULTIPLIER,
    option_underlying,
    sector_for,
)
import execution_ledger as ledger  # noqa: E402

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


_CLOCK_CACHE = {"ts": 0.0, "val": None}


def equities_open_now(ttl: int = 60, default=True) -> bool:
    """
    Authoritative 'can equities trade right now?' from the Alpaca clock (cached ~`ttl`s).
    Unlike common.market_phase() — which is purely time-based and HOLIDAY-BLIND (it labels
    a market holiday like Juneteenth as REGULAR) — the broker clock knows holidays and the
    real session bounds. Returns `default` (True, fail-open) if the clock can't be reached,
    so a transient network blip never silently halts trading.
    """
    import time as _t
    now = _t.time()
    if _CLOCK_CACHE["val"] is not None and now - _CLOCK_CACHE["ts"] < ttl:
        return _CLOCK_CACHE["val"]
    try:
        is_open = bool(get_market_status().get("is_open"))
        _CLOCK_CACHE.update(ts=now, val=is_open)
        return is_open
    except Exception:
        prev = _CLOCK_CACHE["val"]
        return prev if prev is not None else default


def make_client_order_id(symbol, qty, side, limit_price, intent_key=None, now=None):
    """Build an Alpaca-safe idempotency key for one 15-minute decision window."""
    current = now or datetime.now(timezone.utc)
    bucket_minute = (current.minute // 15) * 15
    bucket = current.replace(minute=bucket_minute, second=0, microsecond=0)
    raw = "|".join([
        bucket.isoformat(),
        normalize_symbol(symbol),
        str(side).lower(),
        f"{float(qty):.8f}",
        f"{float(limit_price):.5f}",
        str(intent_key or ""),
    ])
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"ft-{bucket:%Y%m%dT%H%M}-{digest}"


def get_order_by_client_id(client_order_id):
    """Fetch one Alpaca order using the caller-assigned idempotency key."""
    response = _HTTP.get(
        f"{BASE_URL}/v2/orders:by_client_order_id",
        headers=HEADERS,
        params={"client_order_id": client_order_id},
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    order = response.json()
    order.setdefault("client_order_id", client_order_id)
    ledger.record_snapshot(order, event_type="order.reconciled", client_order_id=client_order_id)
    return order


def unresolved_order_for(symbol, side):
    """Return a conflicting unresolved local order, if one exists."""
    normalized = normalize_symbol(symbol)
    # A pending sell must not block an emergency buy-to-cover. A pending buy blocks
    # every additional buy, and any unresolved order blocks a new exposure-increasing buy.
    query_side = str(side).lower() if str(side).lower() == "sell" else None
    return ledger.has_unresolved_order(normalized, side=query_side)


def place_order(
    symbol,
    qty,
    side,
    limit_price=None,
    client_order_id=None,
    intent_key=None,
    intent_context=None,
):
    """
    Place a buy or sell LIMIT order. Returns Alpaca JSON on success or
    {"error": "..."} on failure without raising.

    LIMIT-ONLY is a hard SOP rule. A missing/invalid limit price is rejected here at the
    lowest layer rather than silently downgraded to a market order — so no caller (incl. the
    `trade.py order` CLI) can ever submit a market order and breach the rule.
    """
    try:
        lp = float(limit_price) if limit_price is not None else 0.0
    except (TypeError, ValueError):
        lp = 0.0
    if lp <= 0:
        return {"error": "Limit price required — market orders are not permitted (limit-only SOP).",
                "submitted": {"symbol": symbol, "qty": str(qty), "side": side}}
    crypto = is_crypto(symbol)
    order_symbol = normalize_symbol(symbol)
    order_data = {
        "symbol": order_symbol,
        "qty": str(qty),
        "side": side,
        "type": "limit",
        "time_in_force": "gtc" if crypto else "day",
    }
    if limit_price:
        decimals = 5 if crypto else 2
        order_data["limit_price"] = str(round(float(limit_price), decimals))

    client_order_id = client_order_id or make_client_order_id(
        order_symbol, qty, side, limit_price, intent_key=intent_key
    )
    order_data["client_order_id"] = client_order_id

    existing = ledger.get_order(client_order_id)
    if existing:
        if not ledger.is_terminal(existing.get("status")):
            try:
                recovered = get_order_by_client_id(client_order_id)
                if recovered:
                    recovered["_reconciled"] = True
                    return recovered
            except Exception:
                pass
            return {
                "error": "Order intent is already unresolved; submission was not repeated.",
                "ambiguous": True,
                "client_order_id": client_order_id,
                "submitted": order_data,
            }
        return {
            "error": f"Order intent already finalized as {existing.get('status')}; submission was not repeated.",
            "duplicate": True,
            "client_order_id": client_order_id,
            "submitted": order_data,
        }

    conflict, pending = unresolved_order_for(order_symbol, side)
    if conflict:
        return {
            "error": (
                f"Unresolved {pending.get('side')} order already exists for {order_symbol} "
                f"({pending.get('status')}, client_order_id={pending.get('client_order_id')})."
            ),
            "ambiguous": True,
            "client_order_id": pending.get("client_order_id"),
            "submitted": order_data,
        }

    ledger.record_intent(
        client_order_id,
        symbol=order_symbol,
        side=str(side).lower(),
        qty=float(qty),
        limit_price=float(order_data["limit_price"]),
        payload=order_data,
        context=intent_context,
    )

    headers = {**HEADERS, "Content-Type": "application/json"}
    url = f"{BASE_URL}/v2/orders"
    try:
        response = _HTTP.post(url, headers=headers, json=order_data, timeout=15)
        if response.status_code >= 400:
            if response.status_code >= 500 or response.status_code == 422:
                try:
                    recovered = get_order_by_client_id(client_order_id)
                    if recovered:
                        recovered["_reconciled"] = True
                        return recovered
                except Exception:
                    pass
            try:
                msg = response.json().get("message", response.text)
            except Exception:
                msg = response.text
            ambiguous = response.status_code >= 500
            error = f"Alpaca {response.status_code}: {msg}"
            ledger.record_error(
                client_order_id,
                error=error,
                ambiguous=ambiguous,
                symbol=order_symbol,
                side=side,
                payload={"status_code": response.status_code},
            )
            return {
                "error": error,
                "ambiguous": ambiguous,
                "client_order_id": client_order_id,
                "submitted": order_data,
            }
        result = response.json()
        result.setdefault("client_order_id", client_order_id)
        ledger.record_snapshot(result, event_type="order.accepted", client_order_id=client_order_id)
        return result
    except Exception as exc:
        try:
            recovered = get_order_by_client_id(client_order_id)
            if recovered:
                recovered["_reconciled"] = True
                return recovered
        except Exception:
            pass
        ledger.record_error(
            client_order_id,
            error=str(exc),
            ambiguous=True,
            symbol=order_symbol,
            side=side,
        )
        return {
            "error": str(exc),
            "ambiguous": True,
            "client_order_id": client_order_id,
            "submitted": order_data,
        }


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


def reconcile_orders(include_broker_orders=True):
    """Refresh local order projections from Alpaca without mutating broker state."""
    seen = set()
    refreshed = 0
    errors = []

    if include_broker_orders:
        try:
            response = _HTTP.get(
                f"{BASE_URL}/v2/orders",
                headers=HEADERS,
                params={"status": "all", "limit": 500, "direction": "desc"},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            recent_orders = response.json()
            for order in recent_orders if isinstance(recent_orders, list) else []:
                client_id = str(order.get("client_order_id") or "")
                if not client_id.startswith("ft-"):
                    continue
                ledger.record_snapshot(order, event_type="order.reconciled")
                seen.add(client_id)
                refreshed += 1
        except Exception as exc:
            errors.append(f"recent-orders: {exc}")

    for pending in ledger.get_unresolved_orders():
        client_id = pending.get("client_order_id")
        if not client_id or client_id in seen:
            continue
        try:
            order = get_order_by_client_id(client_id)
            if order:
                refreshed += 1
        except Exception as exc:
            errors.append(f"{client_id}: {exc}")

    unresolved = ledger.get_unresolved_orders()
    return {
        "refreshed": refreshed,
        "unresolved": len(unresolved),
        "orders": unresolved,
        "errors": errors,
    }


def cancel_stale_orders(max_age_seconds=90):
    """
    Cancel open orders resting unfilled longer than max_age_seconds. Our orders are
    meant to fill immediately (marketable limits); one still open across a cycle was
    priced off data that has since moved — cancel it so a dangling GTC order (esp. a
    crypto buy) can't fill LATER into a move we no longer want.

    Returns a list of dicts, one per cancelled order:
        {"symbol", "side", "qty", "limit_price", "desc"}
    Structured fields let the caller reconcile legacy entry tracking and distinguish
    a confirmed cancellation from a cancellation request that is still pending.
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
                client_id = o.get("client_order_id")
                if client_id:
                    ledger.append_event(
                        "order.cancel_requested",
                        client_order_id=client_id,
                        broker_order_id=o.get("id"),
                        symbol=normalize_symbol(o.get("symbol", ""), o.get("asset_class")),
                        side=str(o.get("side") or "").lower(),
                        status=o.get("status") or "unknown",
                        payload=o,
                    )
                final = o
                try:
                    check = _HTTP.get(
                        f"{BASE_URL}/v2/orders/{o['id']}",
                        headers=HEADERS,
                        timeout=REQUEST_TIMEOUT,
                    )
                    if check.status_code == 200:
                        final = check.json()
                        ledger.record_snapshot(final, event_type="order.cancel_result")
                except Exception:
                    pass
                cancelled.append({
                    "symbol":      o.get("symbol"),
                    "side":        str(o.get("side") or "").lower(),
                    "qty":         o.get("qty"),
                    "limit_price": o.get("limit_price"),
                    "client_order_id": client_id,
                    "status":      ledger.normalize_status(final.get("status")),
                    "filled_qty":  float(final.get("filled_qty") or 0),
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
    the window, returns (0.0, None, last_status); the order remains unresolved in the
    execution ledger until reconciliation or cancellation. Network errors are swallowed
    here because the durable order state remains fail-closed.
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
                ledger.record_snapshot(o, event_type="order.fill_poll")
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


def deterministic_position_qty_cap(price, equity, alloc_pct, regime_mult=1.0,
                                   conviction_factor=1.0, existing_long_mv=0.0,
                                   safety=0.99):
    """
    Maximum BUY quantity allowed under the documented POSITION SIZING formula — the
    deterministic guardrail behind orchestrator._execute_orders. Pure arithmetic (no
    I/O), so it is trivially unit-testable.

        notional_cap = equity × alloc_pct/100 × regime_mult × conviction_factor
        qty_cap      = max(0, notional_cap − existing_long_mv) / price × safety

    `existing_long_mv` is the symbol's current LONG market value so a scale-in only
    fills the remaining headroom (never stacks a second full-size tranche). `safety`
    (<1) leaves a sliver so a re-priced marketable limit can't tip the fill back over
    the cap. Returns (qty_cap, headroom_notional). A non-positive price/equity (or a
    zero-conviction factor) returns (0.0, 0.0).
    """
    try:
        price = float(price)
        equity = float(equity)
    except (TypeError, ValueError):
        return 0.0, 0.0
    if price <= 0 or equity <= 0:
        return 0.0, 0.0
    notional_cap = equity * (float(alloc_pct) / 100.0) * float(regime_mult) * float(conviction_factor)
    headroom = max(0.0, notional_cap - max(0.0, float(existing_long_mv)))
    return (headroom / price) * float(safety), headroom


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

    # ── Minimum order size (live-sim) ─────────────────────────────────────────────
    # Skip dust orders below live_account.min_order_usd. This binds on BUYS only
    # (sells de-risk and are never blocked). `order_value` is the FINAL notional —
    # the orchestrator scales qty by regime × live_scale BEFORE calling, so a live-sim
    # order scaled below the floor is rejected here rather than placed as dust. Policy
    # is SKIP, never round up (rounding up would breach the very sizing it enforces).
    # Missing/zero config -> no floor (load_live_account defaults min_order_usd to 1.0).
    min_order_usd = float((load_live_account() or {}).get("min_order_usd", 0) or 0)
    if min_order_usd > 0 and order_value < min_order_usd - 1e-9:
        return False, (
            f"BUY BLOCKED - order notional ${order_value:,.2f} is below the "
            f"live_account.min_order_usd ${min_order_usd:,.2f} minimum ({sym_norm}); skipped."
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
