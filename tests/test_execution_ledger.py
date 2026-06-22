from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest
import requests


ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import execution_ledger as ledger
import trade


@pytest.fixture(autouse=True)
def isolated_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "execution.sqlite")


def test_client_order_id_is_stable_within_decision_window():
    first = datetime(2026, 6, 21, 13, 46, tzinfo=timezone.utc)
    second = datetime(2026, 6, 21, 13, 58, tzinfo=timezone.utc)
    next_window = datetime(2026, 6, 21, 14, 1, tzinfo=timezone.utc)

    a = trade.make_client_order_id("BTC/USD", 0.01, "buy", 65000, "crypto_scored|8", first)
    b = trade.make_client_order_id("BTC/USD", 0.01, "buy", 65000, "crypto_scored|8", second)
    c = trade.make_client_order_id("BTC/USD", 0.01, "buy", 65000, "crypto_scored|8", next_window)

    assert a == b
    assert a != c
    assert len(a) <= 48


def test_ledger_keeps_append_only_events_and_current_projection():
    ledger.record_intent(
        "ft-test",
        symbol="NVDA",
        side="buy",
        qty=2,
        limit_price=100,
        payload={"source": "test"},
    )
    ledger.record_snapshot({
        "id": "broker-1",
        "client_order_id": "ft-test",
        "symbol": "NVDA",
        "side": "buy",
        "qty": "2",
        "limit_price": "100",
        "filled_qty": "2",
        "filled_avg_price": "99.9",
        "status": "filled",
    })

    projected = ledger.get_order("ft-test")
    assert projected["status"] == "filled"
    assert projected["filled_qty"] == 2
    assert ledger.get_unresolved_orders() == []
    events = ledger.recent_events()
    assert [event["event_type"] for event in events] == ["order.snapshot", "order.submit_started"]


def test_incremental_fill_cursor_returns_only_new_quantity():
    ledger.record_intent(
        "ft-partial",
        symbol="NVDA",
        side="buy",
        qty=1,
        limit_price=102,
        context={"learning_managed": True, "setup_type": "ema_vwap_cross"},
    )
    ledger.record_snapshot({
        "id": "broker-partial",
        "client_order_id": "ft-partial",
        "symbol": "NVDA",
        "side": "buy",
        "qty": "1",
        "filled_qty": "0.4",
        "filled_avg_price": "100",
        "status": "partially_filled",
    })

    first = ledger.get_unapplied_fills()[0]
    assert first["delta_qty"] == pytest.approx(0.4)
    assert first["delta_price"] == pytest.approx(100)
    assert first["context"]["learning_managed"] is True
    ledger.mark_learning_applied("ft-partial", 0.4, 100)
    assert ledger.get_unapplied_fills() == []

    ledger.record_snapshot({
        "id": "broker-partial",
        "client_order_id": "ft-partial",
        "symbol": "NVDA",
        "side": "buy",
        "qty": "1",
        "filled_qty": "1",
        "filled_avg_price": "101",
        "status": "filled",
    })
    final = ledger.get_unapplied_fills()[0]
    assert final["delta_qty"] == pytest.approx(0.6)
    assert final["delta_price"] == pytest.approx(61 / 0.6)


def test_ambiguous_submit_is_not_repeated(monkeypatch):
    calls = {"post": 0}

    class MissingOrder:
        status_code = 404

        def raise_for_status(self):
            return None

    def fail_post(*args, **kwargs):
        calls["post"] += 1
        raise requests.Timeout("response lost")

    monkeypatch.setattr(trade._HTTP, "post", fail_post)
    monkeypatch.setattr(trade._HTTP, "get", lambda *args, **kwargs: MissingOrder())

    first = trade.place_order("NVDA", 1, "buy", 100, intent_key="ema_vwap_cross|8")
    second = trade.place_order("NVDA", 1, "buy", 100, intent_key="ema_vwap_cross|8")

    assert first["ambiguous"] is True
    assert second["ambiguous"] is True
    assert calls["post"] == 1
    pending = ledger.get_unresolved_orders("NVDA")
    assert len(pending) == 1
    assert pending[0]["status"] == "unknown"


def test_timeout_recovers_order_by_client_id(monkeypatch):
    class FoundOrder:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "broker-2",
                "client_order_id": client_id,
                "symbol": "NVDA",
                "side": "buy",
                "qty": "1",
                "limit_price": "100",
                "filled_qty": "0",
                "status": "new",
            }

    now = datetime(2026, 6, 21, 15, 1, tzinfo=timezone.utc)
    client_id = trade.make_client_order_id("NVDA", 1, "buy", 100, "ema_vwap_cross|8", now)
    monkeypatch.setattr(trade._HTTP, "post", lambda *a, **k: (_ for _ in ()).throw(requests.Timeout()))
    monkeypatch.setattr(trade._HTTP, "get", lambda *a, **k: FoundOrder())

    result = trade.place_order(
        "NVDA", 1, "buy", 100, client_order_id=client_id, intent_key="ema_vwap_cross|8"
    )

    assert result["id"] == "broker-2"
    assert result["_reconciled"] is True
    assert ledger.get_order(client_id)["status"] == "new"
