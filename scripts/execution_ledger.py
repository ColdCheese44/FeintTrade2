"""Durable execution events and current order-state projections.

The event table is append-only and is the audit trail. ``orders`` is a rebuildable
projection used to answer the operational question that matters before submitting:
does this symbol already have an order whose final outcome is unknown?
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    from common import normalize_symbol as _normalize_symbol
except ImportError:
    def _normalize_symbol(symbol, asset_class=None):
        return str(symbol or "").upper()


ROOT = Path(__file__).parent.parent
DB_PATH = Path(
    os.getenv("FEINTTRADE_EXECUTION_DB", ROOT / "data" / "execution_events.sqlite")
)

TERMINAL_STATUSES = {
    "filled",
    "canceled",
    "cancelled",
    "rejected",
    "expired",
    "done_for_day",
    "stopped",
    "suspended",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS execution_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            event_ts TEXT NOT NULL,
            client_order_id TEXT,
            broker_order_id TEXT,
            symbol TEXT,
            side TEXT,
            status TEXT,
            payload_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_execution_events_client
            ON execution_events(client_order_id, sequence);
        CREATE INDEX IF NOT EXISTS idx_execution_events_symbol
            ON execution_events(symbol, sequence);

        CREATE TABLE IF NOT EXISTS orders (
            client_order_id TEXT PRIMARY KEY,
            broker_order_id TEXT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL,
            limit_price REAL,
            status TEXT NOT NULL,
            filled_qty REAL NOT NULL DEFAULT 0,
            filled_avg_price REAL,
            submitted_at TEXT,
            updated_at TEXT NOT NULL,
            last_error TEXT,
            context_json TEXT NOT NULL DEFAULT '{}',
            learning_applied_qty REAL NOT NULL DEFAULT 0,
            learning_applied_notional REAL NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_orders_symbol_status
            ON orders(symbol, status);
        CREATE INDEX IF NOT EXISTS idx_orders_broker_id
            ON orders(broker_order_id);
        """
    )
    existing_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(orders)").fetchall()
    }
    migrations = {
        "context_json": "ALTER TABLE orders ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}'",
        "learning_applied_qty": (
            "ALTER TABLE orders ADD COLUMN learning_applied_qty REAL NOT NULL DEFAULT 0"
        ),
        "learning_applied_notional": (
            "ALTER TABLE orders ADD COLUMN learning_applied_notional REAL NOT NULL DEFAULT 0"
        ),
    }
    for column, statement in migrations.items():
        if column not in existing_columns:
            conn.execute(statement)
    return conn


def normalize_status(status) -> str:
    value = str(status or "unknown").strip().lower()
    return "canceled" if value == "cancelled" else value


def is_terminal(status) -> bool:
    return normalize_status(status) in TERMINAL_STATUSES


def append_event(
    event_type: str,
    *,
    client_order_id: str | None = None,
    broker_order_id: str | None = None,
    symbol: str | None = None,
    side: str | None = None,
    status: str | None = None,
    payload: dict | None = None,
) -> int:
    """Append one immutable event and return its monotonically increasing sequence."""
    with _connect() as conn:
        return _append_event_conn(
            conn,
            event_type,
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            symbol=symbol,
            side=side,
            status=status,
            payload=payload,
        )


def _append_event_conn(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    client_order_id: str | None = None,
    broker_order_id: str | None = None,
    symbol: str | None = None,
    side: str | None = None,
    status: str | None = None,
    payload: dict | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO execution_events (
            event_id, event_type, event_ts, client_order_id, broker_order_id,
            symbol, side, status, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            event_type,
            _now(),
            client_order_id,
            broker_order_id,
            symbol,
            side,
            normalize_status(status) if status else None,
            json.dumps(payload or {}, default=str, sort_keys=True),
        ),
    )
    return int(cursor.lastrowid)


def record_intent(
    client_order_id: str,
    *,
    symbol: str,
    side: str,
    qty: float,
    limit_price: float,
    payload: dict | None = None,
    context: dict | None = None,
) -> None:
    """Persist an order intent before the network request is attempted."""
    now = _now()
    with _connect() as conn:
        _append_event_conn(
            conn,
            "order.submit_started",
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            status="submit_started",
            payload=payload,
        )
        conn.execute(
            """
            INSERT INTO orders (
                client_order_id, symbol, side, qty, limit_price, status,
                submitted_at, updated_at, context_json
            ) VALUES (?, ?, ?, ?, ?, 'submit_started', ?, ?, ?)
            ON CONFLICT(client_order_id) DO UPDATE SET
                symbol=excluded.symbol,
                side=excluded.side,
                qty=excluded.qty,
                limit_price=excluded.limit_price,
                context_json=excluded.context_json,
                updated_at=excluded.updated_at
            """,
            (
                client_order_id,
                symbol,
                side,
                qty,
                limit_price,
                now,
                now,
                json.dumps(context or {}, default=str, sort_keys=True),
            ),
        )


def record_snapshot(
    order: dict,
    *,
    event_type: str = "order.snapshot",
    client_order_id: str | None = None,
) -> None:
    """Append a broker snapshot and update the current order projection."""
    client_id = client_order_id or order.get("client_order_id")
    if not client_id:
        return
    symbol = _normalize_symbol(order.get("symbol") or "", order.get("asset_class"))
    side = str(order.get("side") or "").lower()
    status = normalize_status(order.get("status"))
    broker_id = order.get("id") or order.get("order_id")
    qty = _as_float(order.get("qty"))
    limit_price = _as_float(order.get("limit_price"))
    filled_qty = _as_float(order.get("filled_qty")) or 0.0
    filled_avg_price = _as_float(order.get("filled_avg_price"))
    now = _now()

    with _connect() as conn:
        _append_event_conn(
            conn,
            event_type,
            client_order_id=client_id,
            broker_order_id=broker_id,
            symbol=symbol,
            side=side,
            status=status,
            payload=order,
        )
        conn.execute(
            """
            INSERT INTO orders (
                client_order_id, broker_order_id, symbol, side, qty, limit_price,
                status, filled_qty, filled_avg_price, submitted_at, updated_at,
                context_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')
            ON CONFLICT(client_order_id) DO UPDATE SET
                broker_order_id=COALESCE(excluded.broker_order_id, orders.broker_order_id),
                symbol=CASE WHEN orders.symbol != '' THEN orders.symbol ELSE excluded.symbol END,
                side=CASE WHEN orders.side != '' THEN orders.side ELSE excluded.side END,
                qty=COALESCE(excluded.qty, orders.qty),
                limit_price=COALESCE(excluded.limit_price, orders.limit_price),
                status=excluded.status,
                filled_qty=excluded.filled_qty,
                filled_avg_price=COALESCE(excluded.filled_avg_price, orders.filled_avg_price),
                submitted_at=COALESCE(orders.submitted_at, excluded.submitted_at),
                updated_at=excluded.updated_at,
                last_error=NULL
            """,
            (
                client_id,
                broker_id,
                symbol,
                side,
                qty,
                limit_price,
                status,
                filled_qty,
                filled_avg_price,
                order.get("submitted_at") or order.get("created_at") or now,
                now,
            ),
        )


def record_error(
    client_order_id: str,
    *,
    error: str,
    ambiguous: bool,
    symbol: str,
    side: str,
    payload: dict | None = None,
) -> None:
    """Record either a definitive rejection or an unresolved submission outcome."""
    status = "unknown" if ambiguous else "rejected"
    with _connect() as conn:
        _append_event_conn(
            conn,
            "order.submit_ambiguous" if ambiguous else "order.rejected",
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            status=status,
            payload={"error": error, **(payload or {})},
        )
        conn.execute(
            """
            UPDATE orders
               SET status = ?, updated_at = ?, last_error = ?
             WHERE client_order_id = ?
            """,
            (status, _now(), error, client_order_id),
        )


def get_unresolved_orders(symbol: str | None = None, side: str | None = None) -> list[dict]:
    """Return projected orders that have not reached a definitive terminal state."""
    clauses = []
    params: list[str] = []
    if symbol:
        clauses.append("symbol = ?")
        params.append(str(symbol).upper())
    if side:
        clauses.append("side = ?")
        params.append(str(side).lower())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM orders {where} ORDER BY updated_at DESC", params
        ).fetchall()
    return [dict(row) for row in rows if not is_terminal(row["status"])]


def has_unresolved_order(symbol: str, side: str | None = None) -> tuple[bool, dict | None]:
    orders = get_unresolved_orders(symbol=symbol, side=side)
    return (bool(orders), orders[0] if orders else None)


def get_order(client_order_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
    return dict(row) if row else None


def recent_events(limit: int = 100) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM execution_events ORDER BY sequence DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    return [dict(row) for row in rows]


def get_unapplied_fills() -> list[dict]:
    """Return fill deltas that have not yet been reflected in the learning store."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM orders
             WHERE filled_qty > learning_applied_qty + 0.000000001
               AND filled_avg_price IS NOT NULL
             ORDER BY updated_at, client_order_id
            """
        ).fetchall()
    pending = []
    for raw in rows:
        row = dict(raw)
        total_qty = float(row.get("filled_qty") or 0)
        total_notional = total_qty * float(row.get("filled_avg_price") or 0)
        prior_qty = float(row.get("learning_applied_qty") or 0)
        prior_notional = float(row.get("learning_applied_notional") or 0)
        delta_qty = total_qty - prior_qty
        delta_notional = max(0.0, total_notional - prior_notional)
        row["delta_qty"] = delta_qty
        row["delta_price"] = (delta_notional / delta_qty) if delta_qty > 0 else None
        try:
            row["context"] = json.loads(row.get("context_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            row["context"] = {}
        pending.append(row)
    return pending


def mark_learning_applied(client_order_id: str, filled_qty: float, filled_avg_price: float) -> None:
    """Advance the learning cursor only after the corresponding write succeeds."""
    qty = float(filled_qty)
    notional = qty * float(filled_avg_price)
    with _connect() as conn:
        conn.execute(
            """
            UPDATE orders
               SET learning_applied_qty = ?, learning_applied_notional = ?, updated_at = ?
             WHERE client_order_id = ?
            """,
            (qty, notional, _now(), client_order_id),
        )
        _append_event_conn(
            conn,
            "order.learning_applied",
            client_order_id=client_order_id,
            status="applied",
            payload={"filled_qty": qty, "filled_avg_price": float(filled_avg_price)},
        )


def _as_float(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
