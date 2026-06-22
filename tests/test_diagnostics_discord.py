"""Discord delivery checks used by the scheduled diagnostics routine."""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import diagnostics


def _health(command="ok", reports="ok", webhook=True):
    return {
        "channels": {"command_post": True, "reports": True},
        "reachability": {"command_post": command, "reports": reports},
        "webhook_present": webhook,
    }


def test_discord_delivery_is_healthy_when_all_channels_are_reachable(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setattr(
        diagnostics,
        "dch",
        types.SimpleNamespace(health_check=lambda: _health()),
    )
    report = diagnostics.Report()
    diagnostics._check_discord_delivery(report)
    assert report.fail == []
    assert any("all 2 configured channels" in item for item in report.ok)


def test_discord_403_fails_diagnostics_and_explains_webhook_limit(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setattr(
        diagnostics,
        "dch",
        types.SimpleNamespace(health_check=lambda: _health("http 403", "http 403")),
    )
    report = diagnostics.Report()
    diagnostics._check_discord_delivery(report)
    assert any("commands cannot be received" in item for item in report.fail)
    assert any("multichannel routing is degraded" in item for item in report.fail)
    assert any("webhook fallback" in item.lower() for item in report.warn)
