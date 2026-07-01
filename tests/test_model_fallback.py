"""
Guards ask_model's transient-error fallback chain.

A persistent HTTP 500 / api_error on the primary model (the 2026-06-16 incident:
two cycles died on `Error code: 500 ... 'type': 'api_error'`) must roll over to the
configured fallback model instead of failing the whole unattended cycle. Before the
fix the `recoverable` token list covered 503/529/overloaded but NOT 500-class errors,
so a primary-model outage aborted the routine.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import orchestrator  # noqa: E402


class _FakeUsage:
    input_tokens = 10
    output_tokens = 5


class _FakeContent:
    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeContent(text)]
        self.usage = _FakeUsage()


def _make_fake_anthropic(fail_models, raise_exc):
    """Return a fake anthropic.Anthropic whose messages.create() raises for any
    model in `fail_models` and otherwise echoes which model answered."""
    class _FakeMessages:
        def create(self, model, **kwargs):
            if model in fail_models:
                raise raise_exc
            return _FakeResponse(f"answered-by:{model}")

    class _FakeClient:
        def __init__(self, *a, **kw):
            self.messages = _FakeMessages()

    return _FakeClient


def test_persistent_500_falls_back_to_alternate_model(monkeypatch):
    primary = orchestrator._route_model("research")
    fallbacks = orchestrator._route_fallback_models("research", primary)
    assert fallbacks, "need at least one fallback model for this test"

    exc = Exception(
        "Error code: 500 - {'type': 'error', 'error': "
        "{'type': 'api_error', 'message': 'Internal server error'}}"
    )
    monkeypatch.setattr(
        orchestrator.anthropic, "Anthropic",
        _make_fake_anthropic(fail_models={primary}, raise_exc=exc),
    )
    monkeypatch.setattr(orchestrator, "_log_usage", lambda *a, **k: None)

    out = orchestrator.ask_model("p", "s", routine="research")
    assert out == f"answered-by:{fallbacks[0]}"


def test_non_recoverable_error_still_raises(monkeypatch):
    primary = orchestrator._route_model("research")
    exc = ValueError("invalid request: bad parameter")  # not in recoverable list
    monkeypatch.setattr(
        orchestrator.anthropic, "Anthropic",
        _make_fake_anthropic(fail_models={primary}, raise_exc=exc),
    )
    monkeypatch.setattr(orchestrator, "_log_usage", lambda *a, **k: None)

    try:
        orchestrator.ask_model("p", "s", routine="research")
    except ValueError:
        pass
    else:
        raise AssertionError("non-recoverable error should propagate, not fall back")
