# FeintTrade2 — Claude Code subagents

Project-specific subagents for working on this autonomous Alpaca **paper**-trading system.
Claude auto-delegates based on each agent's `description`, or invoke one explicitly:
"use the strategy-analyst to …". They run in their own context with a scoped toolset.

| Agent | Use it for | Model |
|-------|-----------|-------|
| **strategy-analyst** | Trade-log / performance analysis; find what makes & loses money; propose & implement evidence-based, test-backed strategy/config adjustments. | opus |
| **risk-guardrail-auditor** | Audit/harden the machine-enforced risk controls (validate_order caps, sizing clamps, regime gates, stops, accounting). Verifies via mocks/tests. | opus |
| **trade-debugger** | Reproduce bugs from `data/` + `agent.log`, trace the orchestrator/learning loop, fix the root cause, add regression tests. | sonnet |
| **discord-comms** | Routing + embed builders + teaching cards + `!status`/bot; make outputs accurate/scannable. Capture-the-embed, never posts live. | sonnet |
| **ops-scheduler** | Windows task registrars, run_*.bat, diagnostics, backups, CI, model config; keep cadence ↔ docs in sync. Edits scripts, never runs them. | sonnet |

## Shared guardrails (baked into every agent)
- **Never** run order-placing routines (`orchestrator.py cycle|trading|crypto|research|eod|afterhours|marketopen`), `trade.py order`, or task-registration `.ps1` scripts — they mutate the brokerage/account or the OS scheduler.
- Verify with **mocks/stubs/tests and temp-file copies**, never the live broker or live Discord.
- Hard risk controls may be **tightened, never loosened**. Secrets/`.env` are never printed.
- Finish with `python -m pytest -q` + `pyflakes` on changed files.

Authority for trading rules is `CLAUDE.md` (the SOP); config of record is `watchlist.json`.
