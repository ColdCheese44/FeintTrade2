# FeintTrade — Integrations & Research Notes

External analysis requested by the operator: FeintTrade, the public API lists, and MindStudio.
This documents what was reviewed, what was adopted into FeintTrade, and what is queued next.

---

## 1. FeintTrade (`C:\Users\brend\FeintTrade`)

A TypeScript/Node, penny-stock, **PAPER_ONLY** research + approval-queue system. It does not
place orders (no broker execution path) and is human-approval gated. Different stack and purpose
than FeintTrade (autonomous multi-asset trader), so **no code was ported** — but several mature
*concepts* were, because they map directly onto this update's goals:

| FeintTrade concept | Adopted in FeintTrade as |
|---|---|
| Risk-first validation with explicit rejection reasons | `trade.py validate_order()` now returns descriptive, side-aware reasons; report tallies them |
| Market-data quality / freshness gates | `research.get_snapshot()` anchors a fresh `LIVE_PRICE`; cycle prompt separates daily-trend from live price |
| Replay/metrics: win rate, expectancy, profit factor, R | `learning.py` now computes expectancy; report surfaces profit factor + by-setup |
| Structured daily session summary to Discord | `report.py` EOD + after-hours detailed reports (embed + `.md` attachment) |
| `ops:scan:ready` readiness / preflight | `diagnostics.py` self-check + auto-heal, scheduled twice daily |
| Strategy metadata tagged on every record | learning log already tags `setup_type` + conviction + regime per trade |
| Proposal fingerprint / dedupe of repeated ideas | Partially: validator no longer rejects the same sell forever (root cause removed). A true per-cycle dedupe/cooldown on identical rejected orders is a good next step. |

**Not adopted (intentionally):** the approval-queue/human-gate model (FeintTrade is autonomous by
design), penny-stock screening specifics, and the Node toolchain.

---

## 2. public-apis/public-apis & cporter202/API-mega-list

Community indexes of free APIs. FeintTrade already uses the high-value finance/crypto/news/macro
sources (Alpaca, Finnhub, NewsAPI, FRED, CoinGecko, alternative.me, OKX, CryptoCompare, SEC EDGAR).
Worthwhile additions surfaced from these lists, by priority:

**High value / low effort**
- **Marketstack / Financial Modeling Prep / Polygon (free tier)** — a *secondary* equities bar
  source to cross-check Alpaca and cover symbols/feeds Alpaca's IEX feed lacks. Mitigates the
  stale-bar issue at its root.
- **Etherscan / Covalent / Bitquery** — on-chain crypto signals (whale transfers, DEX flow,
  stablecoin mints) to enrich the crypto score beyond price/funding/F&G.

**Medium value**
- **Financial Modeling Prep / Tiingo** — fundamentals + dividends/splits to complement yfinance.
- **Marketaux / GNews / Tiingo News** — additional news/sentiment breadth beyond NewsAPI's quota.
- **Twelve Data / Alpha Vantage** — economic + technical endpoints as FRED/Alpaca fallbacks.

**Implementation note:** add each as a `safe_run`-wrapped function in `enrichment.py` behind its
own `.env` key, so a missing key degrades gracefully (the existing pattern). Keep them advisory —
they feed the prompt/score, they never bypass the hard-constraint validator.

---

## 3. MindStudio (mindstudio.ai/blog)

An AI agent/workflow platform. Not a dependency to add, but its agent-engineering patterns are
directly applicable to how FeintTrade's autonomous loop is built:

- **Markdown context with conditional loading** — FeintTrade already does this (CLAUDE.md SOP +
  regime/perf/discovery briefs injected per routine). Keep briefs scoped and current.
- **Implement → verify → fix loop** — mirrored by the new `diagnostics.py` self-heal pass and the
  EOD/after-hours report → external-AI → Claude Code refinement loop the operator runs.
- **Smaller models as sub-agents for cost** — future: use Haiku for routine news summarization /
  screening to cut token cost, reserving Opus for the actual trade decision.
- **Structured artifact generation to prevent hallucination** — the report's machine-readable JSON
  appendix and the learning log are exactly this: ground decisions in persisted artifacts.

---

## Suggested next integrations (ranked)
1. Secondary equities price feed (Polygon/FMP) to fully kill stale-bar risk.
2. Per-cycle dedupe/cooldown so a blocked order isn't re-attempted every 15 min (noise control).
3. On-chain crypto signals (Etherscan/Covalent) feeding the crypto score.
4. Haiku-powered news pre-summarization sub-step to cut Opus token spend.
