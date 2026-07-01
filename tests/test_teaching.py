"""
Tests for the teaching engine (lessons + graphic cards). Hermetic: no Discord/network;
make_card uses Pillow locally, teach() is monkeypatched in payload tests.

Run: python -B -m pytest tests/test_teaching.py -q
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import teaching


def test_lesson_for_buy_has_rr_and_content():
    lesson = teaching.lesson_for({"symbol": "NVDA", "action": "BUY", "setup_type": "squeeze_breakout",
                                  "regime": "BULL", "signal_count": 7,
                                  "entry": 205, "stop": 199, "target": 220})
    assert lesson["action"] == "BUY"
    assert "NVDA" in lesson["title"]
    assert lesson["explain"]                       # non-empty explanation
    assert "risk" in lesson["lesson"].lower()
    assert lesson["rr"] is not None and abs(lesson["rr"] - 2.5) < 0.01   # (220-205)/(205-199)


def test_lesson_for_no_trade():
    lesson = teaching.lesson_for({"action": "NO_TRADE"})
    assert lesson["action"] == "NO_TRADE"
    assert "patience" in lesson["lesson"].lower() or "cash" in lesson["title"].lower()


def test_no_trade_lesson_rotates_for_variety():
    tips = {teaching._rotating_tip(seed=h) for h in range(10)}
    assert len(tips) >= 5                               # rotation gives variety
    lesson = teaching.lesson_for({"action": "NO_TRADE"})
    assert lesson["tip"] in teaching.GENERAL_TIPS
    assert lesson["lesson"] in teaching.GENERAL_TIPS    # no-trade uses a rotating tip


def test_make_card_returns_png_bytes():
    png = teaching.make_card(teaching.lesson_for(
        {"symbol": "AMD", "action": "BUY", "entry": 100, "stop": 97, "target": 110}))
    assert isinstance(png, (bytes, bytearray))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"          # PNG magic header
    assert len(png) > 1000


def test_make_card_no_trade_renders():
    png = teaching.make_card(teaching.lesson_for({"action": "NO_TRADE"}))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_teach_from_payload_picks_lead_order(monkeypatch):
    captured = {}
    monkeypatch.setattr(teaching, "teach", lambda d, dedup_key=None, cycle_id="": captured.update(d) or True)
    payload = {"orders": [{"symbol": "TSLA", "side": "buy", "conviction": 8,
                           "setup_type": "breakout", "limit_price": 250}], "summary": "x"}
    teaching.teach_from_payload(payload, "BULL")
    assert captured.get("symbol") == "TSLA" and captured.get("action") == "BUY"
    assert captured.get("regime") == "BULL"


def test_teach_from_payload_no_trade(monkeypatch):
    captured = {}
    monkeypatch.setattr(teaching, "teach", lambda d, dedup_key=None, cycle_id="": captured.update(d) or True)
    teaching.teach_from_payload({"orders": [], "closes": [], "summary": "nothing qualifies"}, "NEUTRAL")
    assert captured.get("action") == "NO_TRADE"


# ── Expanded training content ─────────────────────────────────────────────────

def test_lesson_includes_manage_pitfall_and_glossary():
    lesson = teaching.lesson_for({"symbol": "NVDA", "action": "BUY", "setup_type": "squeeze",
                                  "regime": "BULL", "entry": 100, "stop": 97, "target": 110})
    assert lesson["manage"] and "watch" in lesson["manage"].lower()
    assert lesson["pitfall"].lower().startswith("common mistake")
    assert lesson["glossary_term"] and lesson["glossary_def"]


@pytest.mark.parametrize("setup,expect", [
    ("short_momentum", "inverse"),       # specific key must win over generic 'momentum'
    ("inverse_etf_momentum", "rises"),
    ("gap_and_go", "gap-and-go"),
    ("crypto_scored", "scored"),
    ("panic_hedge", "uvxy"),
])
def test_specific_setups_resolve_over_generic(setup, expect):
    lesson = teaching.lesson_for({"symbol": "X", "action": "BUY", "setup_type": setup})
    assert expect in lesson["explain"].lower()


def test_glossary_rotates():
    terms = {teaching._glossary_term(seed=h)[0] for h in range(len(teaching.GLOSSARY))}
    assert len(terms) >= 6


def test_close_action_has_management_and_pitfall():
    lesson = teaching.lesson_for({"symbol": "SOXS", "action": "CLOSE"})
    assert lesson["manage"] and lesson["pitfall"]
    png = teaching.make_card(lesson)              # taller card still renders for non-RR actions
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
