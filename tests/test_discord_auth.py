"""
Optional operator allowlist for privileged Discord trading commands — FeintTrade.
Run: python -m pytest tests/test_discord_auth.py -v

Channel restriction (bot.py) is the baseline; discord_auth adds an OPTIONAL second factor
for !buy/!sell/!kill/!resume/!cancel. When no allowlist is configured, behavior is
unchanged (privileged commands allowed); when configured, only listed users/roles pass.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import discord_auth as da


# ── id parsing ────────────────────────────────────────────────────────────────

def test_parse_ids_handles_separators_and_junk():
    assert da._parse_ids("1, 2 3;4") == {1, 2, 3, 4}
    assert da._parse_ids("") == set()
    assert da._parse_ids(None) == set()
    assert da._parse_ids("abc, 5, <@7>") == {5}     # non-digit tokens ignored


def test_privileged_set():
    for c in ("!buy", "!sell", "!kill", "!resume", "!cancel"):
        assert da.is_privileged(c)
    for c in ("!status", "!positions", "!price", "!journal"):
        assert not da.is_privileged(c)


# ── authorization logic (explicit allowlists) ────────────────────────────────

def test_non_privileged_always_allowed():
    assert da.is_authorized("!status", author_id=999, allow_user_ids={1}, allow_role_ids=set())
    assert da.is_authorized("!price", author_id=None, allow_user_ids={1}, allow_role_ids=set())


def test_privileged_allowed_when_allowlist_empty():
    # Not configured -> preserve channel-only behavior (back-compat).
    assert da.is_authorized("!buy", author_id=999, allow_user_ids=set(), allow_role_ids=set())
    assert da.is_authorized("!kill", author_id=None, allow_user_ids=set(), allow_role_ids=set())


def test_privileged_user_allowlist():
    assert da.is_authorized("!buy", author_id=42, allow_user_ids={42}, allow_role_ids=set())
    assert not da.is_authorized("!buy", author_id=7, allow_user_ids={42}, allow_role_ids=set())


def test_privileged_role_allowlist():
    assert da.is_authorized("!sell", author_id=7, author_role_ids=[100, 200],
                            allow_user_ids=set(), allow_role_ids={200})
    assert not da.is_authorized("!sell", author_id=7, author_role_ids=[100],
                                allow_user_ids=set(), allow_role_ids={200})


# ── environment-driven config ─────────────────────────────────────────────────

def test_env_configured(monkeypatch):
    monkeypatch.setenv("DISCORD_OPERATOR_USER_IDS", "111 222")
    monkeypatch.delenv("DISCORD_OPERATOR_ROLE_IDS", raising=False)
    assert da.allowlist_configured()
    assert da.operator_user_ids() == {111, 222}
    assert da.is_authorized("!kill", author_id=111)
    assert not da.is_authorized("!kill", author_id=333)


def test_env_not_configured_preserves_behavior(monkeypatch):
    monkeypatch.delenv("DISCORD_OPERATOR_USER_IDS", raising=False)
    monkeypatch.delenv("DISCORD_OPERATOR_ROLE_IDS", raising=False)
    assert not da.allowlist_configured()
    assert da.is_authorized("!kill", author_id=333)          # unchanged when unconfigured


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
