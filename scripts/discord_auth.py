"""
Optional operator allowlist for privileged Discord trading commands — FeintTrade.

The baseline access control is the CHANNEL restriction in bot.py (the bot only acts on
messages in DISCORD_CH_COMMAND_POST). This module adds an OPTIONAL second factor for the
commands that mutate trading state — !buy, !sell, !kill, !resume, !cancel.

Config (in .env, both optional):
  DISCORD_OPERATOR_USER_IDS  = comma/space-separated Discord user IDs
  DISCORD_OPERATOR_ROLE_IDS  = comma/space-separated Discord role IDs

Behavior:
  • A non-privileged command (e.g. !status, !price) is ALWAYS allowed.
  • If NEITHER env var is set, the allowlist is "not configured" and privileged commands
    are allowed too — i.e. behavior is IDENTICAL to before (channel restriction only), so
    existing operations never break.
  • If EITHER is set, a privileged command is allowed only when the author's id is in the
    user allowlist OR one of the author's role ids is in the role allowlist. Otherwise it
    is denied (bot.py logs the attempt and replies without executing).

Pure + import-light (no `discord` dependency) so it is unit-testable in isolation.
"""

import os

# Commands that place/cancel orders or change the kill/lockout state. Channel
# restriction always applies; the allowlist below is an optional second factor.
PRIVILEGED_COMMANDS = {"!buy", "!sell", "!kill", "!resume", "!cancel"}


def _parse_ids(raw: str | None) -> set:
    """Parse a comma/space/semicolon-separated id list into a set of ints (ignores junk)."""
    if not raw:
        return set()
    out = set()
    for tok in raw.replace(",", " ").replace(";", " ").split():
        tok = tok.strip()
        if tok.isdigit():
            out.add(int(tok))
    return out


def operator_user_ids() -> set:
    return _parse_ids(os.getenv("DISCORD_OPERATOR_USER_IDS"))


def operator_role_ids() -> set:
    return _parse_ids(os.getenv("DISCORD_OPERATOR_ROLE_IDS"))


def allowlist_configured() -> bool:
    """True when at least one of the operator allowlists is configured."""
    return bool(operator_user_ids() or operator_role_ids())


def is_privileged(command: str) -> bool:
    return str(command or "").lower() in PRIVILEGED_COMMANDS


def is_authorized(command: str, author_id=None, author_role_ids=(),
                  allow_user_ids: set | None = None, allow_role_ids: set | None = None) -> bool:
    """
    Whether `command` from this author is allowed to execute.

    Non-privileged commands are always authorized. Privileged commands are authorized
    when the allowlist is not configured (back-compat) OR the author matches it. Pass
    `allow_user_ids`/`allow_role_ids` explicitly for tests; otherwise they are read from
    the environment.
    """
    if not is_privileged(command):
        return True

    users = operator_user_ids() if allow_user_ids is None else set(allow_user_ids)
    roles = operator_role_ids() if allow_role_ids is None else set(allow_role_ids)
    if not users and not roles:
        return True  # allowlist not configured -> preserve channel-only behavior

    try:
        aid = int(author_id) if author_id is not None else None
    except (TypeError, ValueError):
        aid = None
    role_ids = set()
    for r in (author_role_ids or ()):
        try:
            role_ids.add(int(r))
        except (TypeError, ValueError):
            continue

    if aid is not None and aid in users:
        return True
    if roles and role_ids & roles:
        return True
    return False
