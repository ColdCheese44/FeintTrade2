"""
Options chain data + contract selection — FeintTrade.

Long calls/puts ONLY (no selling premium). The free Alpaca options feed (indicative)
returns quotes but NOT greeks/IV, so contract selection uses MONEYNESS as a delta proxy
(slightly-OTM ~= 0.40 delta) and surfaces the real strike / %-OTM / bid-ask / premium so
the model can apply the SOP's delta/IV judgment. Premium-at-risk = mid price x 100
(OPTION_CONTRACT_MULTIPLIER). Everything degrades gracefully to "" / None on any error so
a data blip never blocks a cycle.

CLI:
  python scripts/options.py brief                # chain brief for prompt injection
  python scripts/options.py pick NVDA call       # one selected contract (JSON)
"""

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT / "scripts"))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from common import (  # noqa: E402
    load_options_config, make_http_session, now_mt, OPTION_CONTRACT_MULTIPLIER,
)

HEADERS = {"APCA-API-KEY-ID": os.getenv("APCA_API_KEY_ID"),
           "APCA-API-SECRET-KEY": os.getenv("APCA_API_SECRET_KEY")}
BROKER  = os.getenv("APCA_BASE_URL", "https://paper-api.alpaca.markets")
DATA    = "https://data.alpaca.markets"
_HTTP   = make_http_session()
TIMEOUT = 15


def _spot(underlying: str):
    """Latest underlying share price."""
    try:
        r = _HTTP.get(f"{DATA}/v2/stocks/{underlying}/snapshot",
                      headers=HEADERS, params={"feed": "iex"}, timeout=TIMEOUT)
        r.raise_for_status()
        s = r.json()
        return (s.get("latestTrade") or {}).get("p") or (s.get("dailyBar") or {}).get("c")
    except Exception:
        return None


def _contracts(underlying: str, opt_type: str, cfg: dict, spot: float) -> list:
    """Active `opt_type` ('call'/'put') contracts within the DTE window + a strike band."""
    today = now_mt().date()
    params = {
        "underlying_symbols": underlying, "type": opt_type, "status": "active",
        "expiration_date_gte": (today + timedelta(days=int(cfg.get("min_dte", 3)))).isoformat(),
        "expiration_date_lte": (today + timedelta(days=int(cfg.get("max_dte", 7)))).isoformat(),
        "limit": 500,
    }
    if spot:
        params["strike_price_gte"] = round(spot * 0.85, 2)
        params["strike_price_lte"] = round(spot * 1.15, 2)
    try:
        r = _HTTP.get(f"{BROKER}/v2/options/contracts", headers=HEADERS, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json().get("option_contracts") or []
    except Exception:
        return []


def _quote(symbol: str) -> dict:
    """Latest bid/ask for one OCC symbol (indicative feed)."""
    try:
        r = _HTTP.get(f"{DATA}/v1beta1/options/quotes/latest",
                      headers=HEADERS, params={"symbols": symbol, "feed": "indicative"}, timeout=TIMEOUT)
        r.raise_for_status()
        return (r.json().get("quotes") or {}).get(symbol, {}) or {}
    except Exception:
        return {}


def _dte(exp: str) -> int:
    try:
        return (date.fromisoformat(exp) - now_mt().date()).days
    except Exception:
        return 999


def select_contract(underlying: str, direction: str, cfg: dict = None, spot: float = None):
    """
    Pick one slightly-OTM, liquid contract for `direction` ('call' or 'put').
    Returns a dict (symbol, strike, expiry, dte, bid/ask/mid, premium_per_contract, pct_otm)
    or None. Moneyness stands in for delta since the free feed has no greeks.
    """
    cfg = cfg or load_options_config()
    spot = spot or _spot(underlying)
    if not spot:
        return None
    opt_type = "call" if str(direction).lower().startswith("c") else "put"
    contracts = _contracts(underlying, opt_type, cfg, spot)
    if not contracts:
        return None

    by_exp: dict[str, list] = {}
    for c in contracts:
        by_exp.setdefault(c.get("expiration_date"), []).append(c)
    target_dte = (int(cfg.get("min_dte", 3)) + int(cfg.get("max_dte", 7))) // 2
    best_exp = min(by_exp, key=lambda e: abs(_dte(e) - target_dte))
    exp_contracts = by_exp[best_exp]

    # Slightly-OTM strike (~0.40 delta proxy): ~1 strike out of the money.
    strikes = sorted(float(c["strike_price"]) for c in exp_contracts)
    if opt_type == "call":
        otm = [k for k in strikes if k >= spot] or strikes
        target_strike = otm[min(1, len(otm) - 1)]
    else:
        otm = [k for k in strikes if k <= spot] or strikes
        target_strike = otm[max(0, len(otm) - 2)]
    chosen = min(exp_contracts, key=lambda c: abs(float(c["strike_price"]) - target_strike))

    sym = chosen["symbol"]
    q = _quote(sym)
    bid, ask = q.get("bp"), q.get("ap")
    mid = round((bid + ask) / 2, 2) if (bid and ask) else (ask or bid)
    if not mid:
        return None
    strike = float(chosen["strike_price"])
    return {
        "symbol": sym, "underlying": underlying, "type": opt_type,
        "strike": strike, "expiry": best_exp, "dte": _dte(best_exp),
        "spot": round(spot, 2), "bid": bid, "ask": ask, "mid": mid,
        "premium_per_contract": round(mid * OPTION_CONTRACT_MULTIPLIER, 2),
        "pct_otm": round((strike - spot) / spot * 100, 2),
    }


def options_brief(cfg: dict = None) -> str:
    """Chain brief for prompt injection: per underlying, a slightly-OTM call AND put."""
    cfg = cfg or load_options_config()
    if not cfg.get("enabled"):
        return ""
    lines = [
        "=== OPTIONS CHAIN (weekday focus — long calls/puts only) ===",
        (f"Guardrails: slightly-OTM (~delta {cfg['min_delta']}-{cfg['max_delta']}), "
         f"DTE {cfg['min_dte']}-{cfg['max_dte']}, <= ${cfg['max_premium_per_trade']:,.0f} premium/trade, "
         f"<= {cfg['max_per_underlying_pct']}%/underlying, <= {cfg['max_total_exposure_pct']}% total. "
         f"Exits: +{cfg['profit_target_pct']}% / {cfg['stop_loss_pct']}% / close <= {cfg['close_at_dte']} DTE."),
        "WEEKDAY FOCUS: prioritize a qualifying long option over the equivalent share/ETF trade. "
        "To order: use the OCC symbol, side \"buy\", qty = whole CONTRACTS, limit_price = premium per "
        "share (at/just through the ask), setup_type \"options_directional\". Premium-at-risk = qty x "
        "price x 100; caps $5k/trade, 10%/underlying, 30% total (system-enforced). Exits are automatic: "
        "+100% / -50% / close at <=1 DTE. Pick CALL or PUT by the name's own CONFIRMED momentum; skip in "
        "chop. Fall back to shares/ETFs only if no option qualifies.",
        "",
    ]
    for u in cfg.get("underlyings", []):
        spot = _spot(u)
        if not spot:
            lines.append(f"{u}: (no quote)")
            continue
        lines.append(f"{u} spot ${spot:,.2f}:")
        for label, direction in (("CALL", "call"), ("PUT", "put")):
            c = select_contract(u, direction, cfg, spot)
            if c:
                lines.append(
                    f"  {label} {c['symbol']} K${c['strike']:g} {c['dte']}DTE "
                    f"bid/ask ${c['bid']}/${c['ask']} mid ${c['mid']} "
                    f"(~${c['premium_per_contract']:,.0f}/contract, {c['pct_otm']:+.1f}% OTM)"
                )
            else:
                lines.append(f"  {label}: (no liquid contract)")
    return "\n".join(lines)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "brief"
    if cmd == "pick" and len(sys.argv) >= 4:
        print(json.dumps(select_contract(sys.argv[2].upper(), sys.argv[3]), indent=2, default=str))
    else:
        print(options_brief())
