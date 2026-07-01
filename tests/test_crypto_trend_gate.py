import importlib


def test_crypto_trend_gate_rejects_downtrend_buy_and_notifies(monkeypatch):
    orch = importlib.import_module("scripts.orchestrator")
    notified = []
    monkeypatch.setattr(orch, "_notify", lambda *a, **k: notified.append((a, k)))

    payload = {
        "orders": [
            {"symbol": "XRP/USD", "side": "buy", "qty": 0.4, "limit_price": 1.05},
            {"symbol": "BTC/USD", "side": "buy", "qty": 0.01, "limit_price": 70000},
            {"symbol": "ETH/USD", "side": "sell", "qty": 0.1, "limit_price": 3500},
        ]
    }
    events = []

    blocked = orch._apply_crypto_trend_gate(
        payload,
        {
            "XRP/USD": {"ema9": 1.062, "ema21": 1.107},
            "BTC/USD": {"ema9": 70500, "ema21": 69000},
            "ETH/USD": {"ema9": 3600, "ema21": 3700},
        },
        events,
    )

    assert blocked == ["XRP/USD"]
    assert [order["symbol"] for order in payload["orders"]] == ["BTC/USD", "ETH/USD"]
    assert events == [{
        "symbol": "XRP/USD",
        "side": "buy",
        "status": "rejected",
        "message": (
            "BUY BLOCKED - XRP/USD: crypto daily trend gate failed "
            "(EMA9 1.062 < EMA21 1.107). Proposal posted, "
            "but no broker order was submitted."
        ),
    }]
    assert notified
    args, _kwargs = notified[0]
    assert args[0] == "order_rejected"
    assert args[1]["symbol"] == "XRP/USD"
    assert "no broker order was submitted" in args[2]


def test_crypto_trend_gate_keeps_uptrend_buys(monkeypatch):
    orch = importlib.import_module("scripts.orchestrator")
    monkeypatch.setattr(orch, "_notify", lambda *a, **k: None)

    payload = {
        "orders": [
            {"symbol": "BTC/USD", "side": "buy", "qty": 0.01, "limit_price": 70000},
        ]
    }
    events = []

    blocked = orch._apply_crypto_trend_gate(
        payload,
        {"BTC/USD": {"ema9": 70500, "ema21": 69000}},
        events,
    )

    assert blocked == []
    assert payload["orders"][0]["symbol"] == "BTC/USD"
    assert events == []
