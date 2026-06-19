"""
FeintTrade2 safety guard — Claude Code PreToolUse hook (Bash).

Hard-blocks commands that could place/cancel real (paper) orders, run a live trading
decision routine, force-push, push to main, or (re)register Windows scheduled tasks —
for EVERY Claude session in this repo, including the headless daily maintenance run
(where a permissions deny-list is bypassed but hooks are still evaluated).

Reads the tool call JSON on stdin; exits 2 (block, message to the model) on a match,
else exits 0. Fails OPEN (exit 0) on any internal error so it can never wedge the agent.

Deliberately does NOT block `orchestrator.py crypto|research` (the operator allow-lists
those for interactive testing) or a normal `git push` to a feature branch.
"""
import json
import re
import sys

# (pattern, why) — matched against the Bash command string.
BLOCKED = [
    (r"orchestrator\.py\s+(cycle|trading|eod|afterhours|marketopen)\b",
     "live trading-decision routine (places paper orders)"),
    (r"\btrade\.py\s+order\b",                "direct order placement"),
    (r"\.place_order\s*\(",                   "direct place_order() call"),
    (r"\bcancel_all_orders\b",                "mass order cancel"),
    (r"git\s+push\b[^\n]*(--force|\s-f\b)",   "force-push"),
    (r"git\s+push\b[^\n]*\bmain\b",           "push to main (use the feature branch/PR)"),
    (r"register_[A-Za-z0-9_]+\.ps1",          "Windows Task Scheduler (re)registration"),
    (r"Unregister-ScheduledTask|schtasks\s+/(create|delete|change)",
     "Windows Task Scheduler mutation"),
]


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # fail open — never block on a parse error
    cmd = str((data.get("tool_input") or {}).get("command", ""))
    if not cmd:
        sys.exit(0)
    for pattern, why in BLOCKED:
        try:
            if re.search(pattern, cmd):
                sys.stderr.write(
                    f"BLOCKED by FeintTrade guard ({why}). Command: {cmd[:160]}\n"
                    "Paper-trading safety: Claude must not run live routines, place/cancel "
                    "orders, force-push, push to main, or touch the Windows scheduler. "
                    "Analyze/debug/fix only; commit to the feature branch.\n")
                sys.exit(2)
        except re.error:
            continue
    sys.exit(0)


if __name__ == "__main__":
    main()
