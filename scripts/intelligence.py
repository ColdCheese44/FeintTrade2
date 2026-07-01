"""Decision intelligence for the pre-live paper-trading phase.

This module captures structured candidate decisions from each routine, evaluates
what happened after the decision, and produces a compact intelligence brief that
can be injected back into prompts and reports.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

DECISION_LOG = DATA_DIR / "decision_log.jsonl"
OUTCOME_DB = DATA_DIR / "decision_outcomes.json"
SUMMARY_CACHE = DATA_DIR / "intelligence_summary.json"
META_FILE = DATA_DIR / "intelligence_meta.json"

sys.path.insert(0, str(ROOT / "scripts"))
try:
    from common import is_crypto, is_option, normalize_symbol
except Exception:
    def normalize_symbol(symbol, asset_class=None):
        return str(symbol or "")

    def is_crypto(symbol, asset_class=None):
        return "/" in str(symbol or "")

    def is_option(symbol):  # type: ignore[misc]
        return False

try:
    from research import get_bars, get_hourly_bars
except Exception:
    get_bars = None
    get_hourly_bars = None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1_000_000_000_000:
            ts /= 1000.0
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def _load_records() -> list[dict]:
    if not DECISION_LOG.exists():
        return []
    records: list[dict] = []
    for line in DECISION_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            continue
    return records


def _append_record(record: dict):
    with open(DECISION_LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")


def _normalize_action(value: str) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "buy": "buy",
        "entry": "buy",
        "open": "buy",
        "scale_in": "buy",
        "add": "buy",
        "hold": "hold",
        "watch": "watch",
        "skip": "skip",
        "pass": "skip",
        "close": "close",
        "sell": "close",
        "trim": "trim",
        "scale_out": "trim",
        "reduce": "trim",
    }
    return aliases.get(text, text or "unknown")


def _normalize_blockers(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    blockers = []
    for item in value:
        text = str(item or "").strip()
        if text:
            blockers.append(text)
    return blockers


def _float_or_none(value):
    if value in (None, "", False):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _int_or_none(value):
    if value in (None, "", False):
        return None
    try:
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return None


def _signal_count(candidate: dict) -> int | None:
    explicit = _int_or_none(candidate.get("signal_count"))
    if explicit is not None:
        return explicit
    signals = candidate.get("signals") or {}
    if isinstance(signals, dict):
        explicit = _int_or_none(signals.get("signal_count"))
        if explicit is not None:
            return explicit
    score = _int_or_none(candidate.get("score"))
    return score


def _expected_side(action: str) -> str | None:
    if action == "buy":
        return "buy"
    if action in {"close", "trim"}:
        return "sell"
    return None


def _build_execution_lookup(execution_events: list[dict] | None) -> dict[tuple[str, str], list[dict]]:
    lookup: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for event in execution_events or []:
        symbol = normalize_symbol(event.get("symbol", ""))
        side = str(event.get("side", "")).lower()
        if symbol and side:
            lookup[(symbol, side)].append(event)
    return lookup


def _event_for_candidate(candidate: dict, execution_lookup: dict[tuple[str, str], list[dict]]) -> dict | None:
    symbol = normalize_symbol(candidate.get("symbol", ""))
    side = _expected_side(candidate.get("action", ""))
    if not symbol or not side:
        return None
    events = execution_lookup.get((symbol, side), [])
    return events.pop(0) if events else None


def _derive_candidates_from_payload(payload: dict, price_lookup: dict[str, float] | None) -> list[dict]:
    normalized_lookup = {
        normalize_symbol(symbol): float(price)
        for symbol, price in (price_lookup or {}).items()
        if _float_or_none(price) is not None
    }
    candidates = payload.get("candidates") or []
    if candidates:
        out = []
        for item in candidates:
            rec = dict(item)
            rec["symbol"] = normalize_symbol(rec.get("symbol", ""))
            if "reference_price" not in rec:
                rec["reference_price"] = (
                    rec.get("decision_price")
                    or rec.get("limit_price")
                    or normalized_lookup.get(rec["symbol"])
                )
            out.append(rec)
        return out

    derived: list[dict] = []
    for order in payload.get("orders", []):
        symbol = normalize_symbol(order.get("symbol", ""))
        side = str(order.get("side", "buy")).lower()
        action = "buy" if side == "buy" else "trim"
        derived.append({
            "symbol": symbol,
            "action": action,
            "setup_type": order.get("setup_type", "unknown"),
            "conviction": order.get("conviction"),
            "score": order.get("score"),
            "signal_count": _signal_count(order),
            "reasoning": order.get("reasoning", ""),
            "reference_price": order.get("limit_price") or normalized_lookup.get(symbol),
            "blockers": [],
        })
    for hold in payload.get("holds", []):
        symbol = normalize_symbol(hold.get("symbol", ""))
        action = _normalize_action(hold.get("action") or "hold")
        derived.append({
            "symbol": symbol,
            "action": action,
            "setup_type": hold.get("setup_type", "hold"),
            "conviction": hold.get("conviction"),
            "score": hold.get("score"),
            "signal_count": _signal_count(hold),
            "reasoning": hold.get("reasoning", ""),
            "reference_price": hold.get("reference_price") or normalized_lookup.get(symbol),
            "blockers": _normalize_blockers(hold.get("blockers")),
        })
    for close in payload.get("closes", []):
        symbol = normalize_symbol(close.get("symbol", ""))
        derived.append({
            "symbol": symbol,
            "action": "close",
            "setup_type": close.get("setup_type", "close"),
            "conviction": close.get("conviction"),
            "score": close.get("score"),
            "signal_count": _signal_count(close),
            "reasoning": close.get("reasoning", ""),
            "reference_price": close.get("reference_price") or normalized_lookup.get(symbol),
            "blockers": [],
        })
    return derived


def log_decision_batch(
    routine: str,
    decision_payload: dict,
    *,
    account: dict | None = None,
    regime: dict | None = None,
    price_lookup: dict[str, float] | None = None,
    execution_events: list[dict] | None = None,
    batch_timestamp=None,
) -> list[dict]:
    """Persist structured candidate decisions for later evaluation."""
    regime = regime or {}
    account = account or {}
    decision_ts = _parse_ts(batch_timestamp) or _now_utc()
    batch_id = f"{routine}_{decision_ts.strftime('%Y%m%d_%H%M%S')}"
    candidates = _derive_candidates_from_payload(decision_payload or {}, price_lookup)
    execution_lookup = _build_execution_lookup(execution_events)
    summary = str((decision_payload or {}).get("summary", "") or "").strip()

    records = []
    for idx, candidate in enumerate(candidates):
        symbol = normalize_symbol(candidate.get("symbol", ""))
        if not symbol:
            continue
        action = _normalize_action(candidate.get("action") or candidate.get("side") or "unknown")
        decision_price = _float_or_none(
            candidate.get("reference_price")
            or candidate.get("decision_price")
            or candidate.get("limit_price")
        )
        event = _event_for_candidate({"symbol": symbol, "action": action}, execution_lookup)
        record = {
            "decision_id": f"{batch_id}_{idx}_{symbol.replace('/', '')}",
            "batch_id": batch_id,
            "timestamp": decision_ts.isoformat(),
            "routine": routine,
            "symbol": symbol,
            "asset_type": (
                "crypto" if is_crypto(symbol)
                else "option" if (is_option(symbol) or symbol.upper().endswith("_OPTIONS"))
                else "equity"
            ),
            "action": action,
            "setup_type": candidate.get("setup_type", "unknown") or "unknown",
            "conviction": _int_or_none(candidate.get("conviction")),
            "score": _int_or_none(candidate.get("score")),
            "signal_count": _signal_count(candidate),
            "decision_price": decision_price,
            "reasoning": str(candidate.get("reasoning", "") or "").strip(),
            "blockers": _normalize_blockers(candidate.get("blockers")),
            "regime": regime.get("regime"),
            "regime_multiplier": _float_or_none(regime.get("multiplier")),
            "vix": _float_or_none(regime.get("vix")),
            "equity": _float_or_none(account.get("equity")),
            "cash": _float_or_none(account.get("cash")),
            "summary": summary,
            "execution_status": event.get("status") if event else None,
            "execution_message": event.get("message") if event else None,
        }
        records.append(record)
        _append_record(record)
    return records


def _bar_value(bar: dict, key: str, fallback: float | None = None) -> float | None:
    value = _float_or_none(bar.get(key))
    return fallback if value is None else value


def _future_bars_for_record(record: dict, bars: list[dict]) -> list[dict]:
    decision_ts = _parse_ts(record.get("timestamp"))
    if decision_ts is None:
        return []
    if record.get("asset_type") == "crypto":
        return [bar for bar in bars if (_parse_ts(bar.get("t")) or decision_ts) > decision_ts]
    decision_day = decision_ts.date()
    return [bar for bar in bars if (_parse_ts(bar.get("t")) or decision_ts).date() > decision_day]


def _horizons_for_asset(asset_type: str) -> list[tuple[str, timedelta, int]]:
    if asset_type == "crypto":
        return [
            ("1h", timedelta(hours=1), 1),
            ("4h", timedelta(hours=4), 4),
            ("24h", timedelta(hours=24), 24),
        ]
    return [
        ("1d", timedelta(days=1), 1),
        ("3d", timedelta(days=3), 3),
        ("5d", timedelta(days=5), 5),
    ]


def _fetch_eval_bars(symbol: str, asset_type: str) -> list[dict]:
    if asset_type == "crypto":
        if get_hourly_bars is None:
            return []
        data = get_hourly_bars(symbol, limit=96) or {}
        return data.get("bars") or []
    # Options (OCC symbols or _OPTIONS placeholders) have no equity bar endpoint;
    # skip bar fetch rather than issuing a guaranteed-400 request to Alpaca.
    # Also guard legacy records stored before the "option" asset_type was introduced,
    # which arrived with asset_type="equity" but an un-fetchable symbol.
    if asset_type == "option" or is_option(symbol) or symbol.upper().endswith("_OPTIONS"):
        return []
    if get_bars is None:
        return []
    data = get_bars(symbol, timeframe="1Day", limit=30) or {}
    return data.get("bars") or []


def _evaluate_record(record: dict, bars: list[dict], existing: dict | None = None) -> dict:
    """Evaluate forward outcomes for any matured horizons not yet scored."""
    existing = dict(existing or {})
    decision_ts = _parse_ts(record.get("timestamp"))
    decision_price = _float_or_none(record.get("decision_price"))
    if decision_ts is None or not decision_price or decision_price <= 0:
        return existing

    age = _now_utc() - decision_ts
    future_bars = _future_bars_for_record(record, bars)
    if not future_bars:
        return existing

    for label, min_age, bar_count in _horizons_for_asset(record.get("asset_type", "equity")):
        if label in existing:
            continue
        if age < min_age or len(future_bars) < bar_count:
            continue
        segment = future_bars[:bar_count]
        close = _bar_value(segment[-1], "c")
        highs = [_bar_value(bar, "h", close) for bar in segment if _bar_value(bar, "h", close) is not None]
        lows = [_bar_value(bar, "l", close) for bar in segment if _bar_value(bar, "l", close) is not None]
        if close is None:
            continue
        max_up = max(highs) if highs else close
        max_down = min(lows) if lows else close
        existing[label] = {
            "return_pct": round((close - decision_price) / decision_price * 100, 3),
            "max_up_pct": round((max_up - decision_price) / decision_price * 100, 3),
            "max_down_pct": round((max_down - decision_price) / decision_price * 100, 3),
            "evaluated_at": _now_utc().isoformat(),
        }
    return existing


def _threshold_for_record(record: dict) -> float:
    return 4.0 if record.get("asset_type") == "crypto" else 2.5


def _primary_horizon(record: dict) -> str:
    return "24h" if record.get("asset_type") == "crypto" else "1d"


def compute_intelligence_summary(
    records: list[dict] | None = None,
    outcomes: dict | None = None,
    *,
    lookback_days: int = 30,
) -> dict:
    """Aggregate structured decision outcomes into a prompt-friendly summary."""
    records = list(records if records is not None else _load_records())
    outcomes = dict(outcomes if outcomes is not None else _load_json(OUTCOME_DB, {}))
    now = _now_utc()
    cutoff = now - timedelta(days=lookback_days)

    recent_records = []
    for record in records:
        ts = _parse_ts(record.get("timestamp"))
        if ts and ts >= cutoff:
            recent_records.append(record)

    evaluated = []
    for record in recent_records:
        primary = outcomes.get(record.get("decision_id"), {}).get(_primary_horizon(record))
        if primary:
            evaluated.append((record, primary))

    by_action_values: dict[str, list[float]] = defaultdict(list)
    by_setup_values: dict[str, list[float]] = defaultdict(list)
    missed: list[dict] = []
    bad_buys: list[dict] = []
    premature_exits: list[dict] = []
    blocker_returns: dict[str, list[float]] = defaultdict(list)

    for record, primary in evaluated:
        action = record.get("action", "unknown")
        setup = record.get("setup_type", "unknown")
        return_pct = _float_or_none(primary.get("return_pct")) or 0.0
        max_up_pct = _float_or_none(primary.get("max_up_pct")) or return_pct
        threshold = _threshold_for_record(record)

        by_action_values[action].append(return_pct)
        by_setup_values[setup].append(return_pct)

        base = {
            "symbol": record.get("symbol"),
            "action": action,
            "setup_type": setup,
            "decision_price": record.get("decision_price"),
            "reasoning": record.get("reasoning", ""),
            "blockers": record.get("blockers", []),
            "return_pct": round(return_pct, 3),
            "max_up_pct": round(max_up_pct, 3),
            "timestamp": record.get("timestamp"),
            "horizon": _primary_horizon(record),
        }

        if action in {"skip", "watch"} and max_up_pct >= threshold:
            missed.append(base)
            for blocker in record.get("blockers", []):
                blocker_returns[blocker].append(max_up_pct)
        elif action == "buy" and return_pct <= -threshold:
            bad_buys.append(base)
        elif action in {"close", "trim"} and return_pct >= threshold:
            premature_exits.append(base)

    by_action = {
        action: {
            "count": len(values),
            "avg_primary_return_pct": round(sum(values) / len(values), 3),
        }
        for action, values in sorted(by_action_values.items())
        if values
    }

    by_setup = {}
    for setup, values in by_setup_values.items():
        if not values:
            continue
        by_setup[setup] = {
            "count": len(values),
            "avg_primary_return_pct": round(sum(values) / len(values), 3),
        }

    blockers = []
    for blocker, values in sorted(blocker_returns.items(), key=lambda item: len(item[1]), reverse=True):
        blockers.append({
            "blocker": blocker,
            "count": len(values),
            "avg_max_up_pct": round(sum(values) / len(values), 3),
        })

    missed.sort(key=lambda item: item["max_up_pct"], reverse=True)
    bad_buys.sort(key=lambda item: item["return_pct"])
    premature_exits.sort(key=lambda item: item["return_pct"], reverse=True)

    brief_lines = [
        f"=== DECISION INTELLIGENCE ({lookback_days}d window) ===",
        f"Structured candidates logged: {len(recent_records)} | Primary-horizon evaluations: {len(evaluated)}",
    ]
    if by_action.get("buy"):
        brief_lines.append(
            f"Buy candidates averaged {by_action['buy']['avg_primary_return_pct']:+.2f}% "
            f"over their primary horizon (n={by_action['buy']['count']})."
        )
    if missed:
        top = missed[0]
        blocker_text = f" blockers={', '.join(top['blockers'])}" if top.get("blockers") else ""
        brief_lines.append(
            f"Missed opportunities: {len(missed)} skip/watch decisions later ran >= threshold; "
            f"biggest miss {top['symbol']} {top['max_up_pct']:+.2f}% after pass.{blocker_text}"
        )
    if bad_buys:
        top = bad_buys[0]
        brief_lines.append(
            f"False positives: {len(bad_buys)} buy candidates lost >= threshold; "
            f"worst {top['symbol']} {top['return_pct']:+.2f}%."
        )
    if blockers:
        top = blockers[0]
        brief_lines.append(
            f"Recurring blocker on later winners: {top['blocker']} "
            f"(count={top['count']}, avg max-up {top['avg_max_up_pct']:+.2f}%)."
        )
    if len(brief_lines) == 2:
        brief_lines.append("Not enough evaluated decisions yet to produce reliable skip/buy intelligence.")

    summary = {
        "generated_at": now.isoformat(),
        "lookback_days": lookback_days,
        "total_candidates": len(recent_records),
        "evaluated_candidates": len(evaluated),
        "by_action": by_action,
        "by_setup": by_setup,
        "missed_opportunities": missed[:5],
        "bad_buy_candidates": bad_buys[:5],
        "premature_exits": premature_exits[:5],
        "blockers_on_missed_winners": blockers[:5],
        "brief_lines": brief_lines,
    }
    return summary


def refresh_intelligence(*, force: bool = False, max_pending: int = 120, min_refresh_minutes: int = 30) -> dict:
    """Refresh matured outcomes and rebuild the summary cache."""
    records = _load_records()
    meta = _load_json(META_FILE, {})
    last_refresh = _parse_ts(meta.get("last_refresh"))
    if (
        not force
        and last_refresh is not None
        and (_now_utc() - last_refresh) < timedelta(minutes=min_refresh_minutes)
        and meta.get("record_count") == len(records)
        and SUMMARY_CACHE.exists()
    ):
        return _load_json(SUMMARY_CACHE, {})

    outcomes = _load_json(OUTCOME_DB, {})
    bar_cache: dict[tuple[str, str], list[dict]] = {}
    updates = 0

    # Select records that are OLD ENOUGH to have at least their first horizon matured
    # but aren't fully scored yet, and process OLDEST-FIRST. The previous logic took the
    # newest `max_pending` records — which are always too young to have any outcome, so
    # nothing ever matured (0 evaluated despite thousands of candidates).
    now = _now_utc()
    pending = []
    for record in records:
        ts = _parse_ts(record.get("timestamp"))
        if ts is None:
            continue
        horizons = _horizons_for_asset(record.get("asset_type", "equity"))
        if not horizons or (now - ts) < horizons[0][1]:
            continue  # too young to have any outcome yet
        existing = outcomes.get(record.get("decision_id"), {})
        if len(existing) >= len(horizons):
            continue  # already fully scored across all horizons
        pending.append(record)
    pending_records = sorted(pending, key=lambda record: record.get("timestamp", ""))[:max_pending]
    for record in pending_records:
        symbol = normalize_symbol(record.get("symbol", ""))
        asset_type = record.get("asset_type", "equity")
        if not symbol:
            continue
        key = (symbol, asset_type)
        if key not in bar_cache:
            bar_cache[key] = _fetch_eval_bars(symbol, asset_type)
        decision_id = record.get("decision_id")
        existing = outcomes.get(decision_id, {})
        evaluated = _evaluate_record(record, bar_cache[key], existing)
        if evaluated != existing:
            outcomes[decision_id] = evaluated
            updates += 1

    if updates:
        _save_json(OUTCOME_DB, outcomes)

    summary = compute_intelligence_summary(records=records, outcomes=outcomes)
    _save_json(SUMMARY_CACHE, summary)
    _save_json(META_FILE, {
        "last_refresh": _now_utc().isoformat(),
        "record_count": len(records),
        "outcome_count": len(outcomes),
    })
    return summary


def get_intelligence_summary(*, refresh: bool = False) -> dict:
    if refresh:
        return refresh_intelligence()
    if SUMMARY_CACHE.exists():
        return _load_json(SUMMARY_CACHE, {})
    return compute_intelligence_summary()


def get_intelligence_brief(*, refresh: bool = False) -> str:
    summary = get_intelligence_summary(refresh=refresh)
    lines = summary.get("brief_lines") or []
    if lines:
        return "\n".join(lines)
    return "=== DECISION INTELLIGENCE ===\nNo evaluated decision data yet."


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "brief"
    if command == "refresh":
        print(json.dumps(refresh_intelligence(force=True), indent=2))
    elif command == "summary":
        print(json.dumps(get_intelligence_summary(), indent=2))
    else:
        print(get_intelligence_brief(refresh=command == "brief_refresh"))
