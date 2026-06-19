# FeintTrade2 — Claude automation

Everything that lets Claude run this project safely and (semi-)autonomously.

## Pieces
- **`agents/`** — 5 scoped subagents (strategy-analyst, risk-guardrail-auditor,
  trade-debugger, discord-comms, ops-scheduler). Claude auto-delegates by description.
- **`prompts/daily_maintenance.md`** — the instruction set for the daily headless run.
- **`hooks/guard.py`** + **`settings.json`** — a `PreToolUse` safety hook that HARD-BLOCKS
  live-trading/order/force-push/push-to-main/scheduler-registration commands for **every**
  session in this repo (interactive *and* the headless task). Enforced even under
  `--dangerously-skip-permissions`, which a permission deny-list is not.
- **Daily task** (`run_claude_maintenance.bat`, Windows Scheduler 7:00 PM, **sonnet**) —
  headless `claude -p` runs `prompts/daily_maintenance.md`: analyze trade logs → debug →
  verify Discord → autofix (test-backed) → commit to the branch + push → **keep a PR open**
  (`gh pr create` if none) → post a summary to #ft-reports.
- **Weekly deep review** (`run_claude_weekly.bat`, Sunday 5:00 PM, **opus**) — headless
  `claude -p` runs `prompts/weekly_deep_review.md`: deep performance + decision-intelligence
  + setup/regime + risk-posture analysis, applies clear test-backed tuning, and writes bigger
  recommendations for human review → #ft-reports. (Cheap daily sonnet + powerful weekly opus.)
- **CI** (`.github/workflows/ci.yml`) — compile + pytest on push/PR.
- Override the model per run with `CLAUDE_MODEL` (daily) / `CLAUDE_WEEKLY_MODEL` (weekly).

## One-time activation note
On your next **interactive** Claude Code session in this repo, you'll get a one-time prompt
to **review/approve the new hook** (`guard.py`). Approve it once — after that it's active in
all sessions, including the unattended daily run. (The hook only *blocks*; it never edits.)

## What's enforced / allowed by the guard
- BLOCKED: `orchestrator.py cycle|trading|eod|afterhours|marketopen`, `trade.py order`,
  `place_order()`/`cancel_all_orders`, `git push --force`, `git push … main`, `register_*.ps1`,
  `schtasks`/`Unregister-ScheduledTask`.
- ALLOWED: `pytest`, `pyflakes`, `validate-models`, `diagnostics.py check`,
  `orchestrator.py crypto|research` (your testing allow-list), and `git push` to a feature branch.

## Maintenance / health
- CLI: keep current with `claude` auto-update (it self-updates by default).
- Verify the whole pipeline: `python -m pytest -q`, `python scripts/diagnostics.py check`,
  `python scripts/discord_channels.py --health`.
