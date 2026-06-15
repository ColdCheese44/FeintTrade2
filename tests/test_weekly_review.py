"""
Weekly review chaining — FeintTrade.
Run: python -m pytest tests/test_weekly_review.py -v

weekly_review.run() chains intel_audit.post / strategy_lab.post / replay.post_report,
isolating each so one failing section never blocks the others.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import weekly_review
import intel_audit
import strategy_lab
import replay


def _stub_all(monkeypatch, calls):
    monkeypatch.setattr(intel_audit, "post", lambda *a, **k: calls.append("intel"))
    monkeypatch.setattr(strategy_lab, "post", lambda *a, **k: calls.append("lab"))
    monkeypatch.setattr(replay, "post_report", lambda *a, **k: calls.append("replay"))


def test_dry_run_does_not_post(monkeypatch):
    calls = []
    _stub_all(monkeypatch, calls)
    res = weekly_review.run(do_post=False)
    assert calls == []
    assert all(v == "ok" for v in res.values())


def test_run_posts_all_sections(monkeypatch):
    calls = []
    _stub_all(monkeypatch, calls)
    res = weekly_review.run(do_post=True)
    assert set(calls) == {"intel", "lab", "replay"}
    assert res == {"intel_audit": "ok", "strategy_lab": "ok", "replay": "ok"}


def test_one_section_failure_does_not_block_others(monkeypatch):
    calls = []

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(intel_audit, "post", boom)
    monkeypatch.setattr(strategy_lab, "post", lambda *a, **k: calls.append("lab"))
    monkeypatch.setattr(replay, "post_report", lambda *a, **k: calls.append("replay"))

    res = weekly_review.run(do_post=True)
    assert "failed" in res["intel_audit"]
    assert res["strategy_lab"] == "ok" and res["replay"] == "ok"
    assert set(calls) == {"lab", "replay"}                # the other two still ran


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
