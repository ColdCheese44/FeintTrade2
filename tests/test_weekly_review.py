"""
Weekly review chaining — FeintTrade.
Run: python -m pytest tests/test_weekly_review.py -v

weekly_review.run() chains the analytics engines, isolating each so one failing section
never blocks the others. Live (do_post=True) calls each module's POST fn; dry
(do_post=False) calls each module's COMPUTE fn (runs the analysis, no Discord).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import weekly_review
import intel_audit
import strategy_lab
import replay


def _stub_all(monkeypatch, posts, computes):
    # post (live) callables
    monkeypatch.setattr(intel_audit, "post", lambda *a, **k: posts.append("intel"))
    monkeypatch.setattr(strategy_lab, "post", lambda *a, **k: posts.append("lab"))
    monkeypatch.setattr(replay, "post_report", lambda *a, **k: posts.append("replay"))
    # compute (dry) callables
    monkeypatch.setattr(intel_audit, "audit", lambda *a, **k: computes.append("intel"))
    monkeypatch.setattr(strategy_lab, "format_report", lambda *a, **k: computes.append("lab"))
    monkeypatch.setattr(replay, "benchmark_report", lambda *a, **k: computes.append("replay"))


def test_dry_run_computes_but_does_not_post(monkeypatch):
    posts, computes = [], []
    _stub_all(monkeypatch, posts, computes)
    res = weekly_review.run(do_post=False)
    assert posts == []                                   # nothing posted to Discord
    assert set(computes) == {"intel", "lab", "replay"}   # but every analysis ran
    assert all(v == "ok" for v in res.values())


def test_live_run_posts_all_sections(monkeypatch):
    posts, computes = [], []
    _stub_all(monkeypatch, posts, computes)
    res = weekly_review.run(do_post=True)
    assert set(posts) == {"intel", "lab", "replay"}
    assert computes == []                                # live path does not call compute fns
    assert res == {"intel_audit": "ok", "strategy_lab": "ok", "replay": "ok"}


def test_one_section_failure_does_not_block_others(monkeypatch):
    posts, computes = [], []
    _stub_all(monkeypatch, posts, computes)

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(intel_audit, "post", boom)

    res = weekly_review.run(do_post=True)
    assert "failed" in res["intel_audit"]
    assert res["strategy_lab"] == "ok" and res["replay"] == "ok"
    assert set(posts) == {"lab", "replay"}               # the other two still ran


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
