import importlib


orchestrator = importlib.import_module("scripts.orchestrator")


def test_extract_decision_payload_from_fenced_json():
    text = """```json
    {
      "summary": "No trade",
      "orders": [],
      "holds": [{"symbol": "BTC/USD", "reasoning": "Hold"}],
      "candidates": [{"symbol": "BTC/USD", "action": "HOLD"}]
    }
    ```"""

    payload = orchestrator.extract_decision_payload(text, "crypto")

    assert payload["summary"] == "No trade"
    assert payload["orders"] == []
    assert payload["holds"][0]["symbol"] == "BTC/USD"


def test_extract_decision_payload_from_raw_json_without_fence():
    text = """
    Analysis first.
    {"summary":"Watch only","orders":[],"holds":[{"symbol":"ETH/USD","reasoning":"Wait"}],"candidates":[]}
    More analysis after.
    """

    payload = orchestrator.extract_decision_payload(text, "trading")

    assert payload["summary"] == "Watch only"
    assert payload["holds"][0]["symbol"] == "ETH/USD"


def test_extract_decision_payload_repairs_non_json_text(monkeypatch):
    text = "BTC/USD HOLD. ETH/USD HOLD. No new entries."

    def fake_ask_model(prompt, system_text, routine="json_repair"):
        assert routine == "json_repair"
        return (
            '{"summary":"No new entries","orders":[],"holds":[{"symbol":"BTC/USD","reasoning":"Hold"}],'
            '"candidates":[{"symbol":"BTC/USD","action":"HOLD"}],"closes":[]}'
        )

    monkeypatch.setattr(orchestrator, "ask_model", fake_ask_model)

    payload = orchestrator.extract_decision_payload(text, "crypto")

    assert payload["summary"] == "No new entries"
    assert payload["holds"][0]["symbol"] == "BTC/USD"
    assert payload["closes"] == []


def test_extract_decision_payload_salvages_close_without_qty():
    text = """```json
    {
      "summary": "Trim risk",
      "orders": [{"symbol": "ETH/USD", "side": "sell", "reasoning": "Exit fully"}],
      "holds": [],
      "candidates": []
    }
    ```"""

    payload = orchestrator.extract_decision_payload(text, "cycle")

    assert payload["orders"] == []
    assert payload["closes"][0]["symbol"] == "ETH/USD"
