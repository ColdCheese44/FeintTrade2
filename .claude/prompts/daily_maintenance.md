# FeintTrade2 — Daily autonomous maintenance run

You are running **headless and unattended** as the daily maintenance pass for FeintTrade2,
an autonomous Alpaca **paper**-trading system. Work end-to-end, then stop. Be decisive but
conservative. Use the project's subagents for the specialized work.

## HARD SAFETY RULES (never violate)
- **NEVER** run order-placing or live routines: `orchestrator.py cycle|trading|crypto|research|eod|afterhours|marketopen`, any `trade.py order`, or `register_*.ps1` (they mutate the brokerage account or the OS scheduler). The live scheduler already runs trading; you only analyze/debug/fix.
- Verify with **mocks/stubs, temp-file copies, and tests** — never against the live broker, and **never post test traffic to live Discord** (read-only `health_check` only).
- Risk controls may be **tightened, never loosened**. Never print `.env`/secrets. Never force-push, never merge to `main`.
- If a fix is risky, ambiguous, or not test-verified, **do NOT change code** — report it instead.

## Do these, in order

1. **Analyze the trade logs** — delegate to the `strategy-analyst` agent. Break down
   `data/trade_log.jsonl` by setup/symbol/regime/exit-reason (count, win rate, P&L). Note
   any new, mechanically-explainable, sufficiently-sampled edge or leak. Confirm the
   learning loop (`learning.get_strategy_recommendations`) reflects reality.

2. **Debug & troubleshoot** — delegate to the `trade-debugger` agent.
   - `python scripts/diagnostics.py check` (read-only) — resolve anything not HEALTHY that you can.
   - Triage `agent.log` by the **latest** occurrence date (not raw count); investigate today's ERROR/WARNING lines.
   - `python -m compileall app.py bot.py dashboard.py scripts tests` and `python -m pytest -q`.
   - `python -m pyflakes` on any files you touch.
   - Reproduce any real bug from `data/` with temp copies before fixing.

3. **Verify Discord comms** — delegate to the `discord-comms` agent.
   - `python scripts/discord_channels.py --health` — confirm bot token + webhook present and every channel reachable.
   - Confirm every emitted `msg_type` resolves to a configured channel (`_resolve_channel`), incl. `status_update → command_post`.
   - Spot-check that the per-cycle status card and recent posts reflect live state (read-only).

4. **Autofix** — for each CLEAR, mechanically-justified, test-verified issue: make the
   minimal fix, add/extend a regression test, and confirm `pytest -q` + `pyflakes` are green.
   Prefer self-updating, data-driven levers (strengthen prompts/recommendations/config)
   over hardcoding. Leave anything uncertain for human review.

## Finish
- If you changed code: ensure the full suite passes, commit to the **current branch** with a
  clear message (end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`), and `git push`.
  If nothing needed fixing, do NOT commit.
- **Keep a review-ready PR open.** After pushing, if the current branch is NOT `main` and has
  no open PR, open one against `main`: `gh pr create --base main --fill` (or a concise
  title/body). If a PR already exists, the push already updated it — do nothing. Never merge
  it; the operator reviews. (Never push to `main` or force-push — the guard hook blocks both.)
- Post a concise (≤8-line) summary of findings + actions to Discord **#ft-reports** via:
  `python -c "import sys; sys.path.insert(0,'scripts'); import discord_notify as dn; dn.send(title='🛠️ Daily Maintenance', description='<your summary>', msg_type='report')"`
- Print the same summary as your final message.
