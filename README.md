# FeintTrade

Autonomous, multi-asset **paper** trading agent on the Alpaca API. Claude makes BUY/SELL/HOLD
decisions every cycle across equities, leveraged/inverse ETFs, and 24/7 crypto. A Streamlit
dashboard, a Discord bot, and a desktop shell wrap it. **Paper only — not financial advice.**

> Trading policy lives in **[CLAUDE.md](CLAUDE.md)**. Hard risk limits live in
> **[watchlist.json](watchlist.json)** `risk{}` and are enforced in code by `scripts/trade.py`.

## Architecture

```
app.py            Desktop shell (pywebview): embeds the dashboard, "Discord/Alpaca" tab
dashboard.py      Streamlit UI (localhost:8501): clock, tickers, charts, positions+strategy, AI chat
bot.py            FeintTrade Command Center bot — !commands, non-blocking; !heartbeat runs a full live cycle
scripts/
  orchestrator.py research | trading | cycle | eod | afterhours | crypto | report
  common.py       symbol normalization (BTCUSD→BTC/USD), MDT/MST time, risk config, sessions
  trade.py        order placement + SIDE-AWARE validation (hard constraints)
  execution_ledger.py  SQLite order events, idempotency, reconciliation, fill cursors
  research.py     bars, indicators (EMA/RSI/MACD/BB/ATR/OBV/pivots/fib/squeeze), snapshot
  enrichment.py   external data (news, macro, fundamentals, funding, fear/greed, ...)
  regime.py       BULL/NEUTRAL/BEAR/PANIC from SPY EMAs + VIX → sizing multiplier + stop
  screener.py     marketwide discovery (Alpaca movers/most-actives + CoinGecko trending)
  learning.py     trade log, scale-ins, partial exits, win rate / expectancy / profit factor
  report.py       detailed EOD + after-hours reports → Discord (embed + .md) for external AI
  diagnostics.py  scheduled self-check + safe auto-heal
  discord_*.py    webhook notifications + bot command handlers
data/             trade_log.jsonl, open_trades.json, performance.json (gitignored)
journal/          daily markdown journals       reports/  exportable session reports
```

## Data flow per cycle
`regime + performance + discovery briefs` → gather fresh bars/indicators/snapshot/news →
Claude decides (JSON) → `trade.validate_order` (hard constraints, single regime multiplier) →
durable order intent → Alpaca limit order → broker reconciliation → confirmed fills update
`learning` exactly once → journal + Discord.

## Run it
```powershell
pip install -r requirements.txt
python app.py                              # desktop app (starts the dashboard)
# or just the dashboard:
streamlit run dashboard.py
# register the full Mountain-Time schedule (once, as Administrator):
powershell -ExecutionPolicy Bypass -File register_all_tasks.ps1
# manual one-offs:
python scripts/orchestrator.py cycle
python scripts/diagnostics.py run
python scripts/orchestrator.py report now
```

## Key behaviors
- **Side-aware risk:** sells always allowed (they de-risk); buys enforce cash reserve (5%),
  per-symbol allocation, max 8 positions, and the 40% crypto cap — with clear rejection reasons.
- **Regime multiplier applied once**, in `trade`/orchestrator; prompts size at full allocation.
- **Idempotent execution:** every order has a deterministic client ID; ambiguous submissions
  remain blocked until broker reconciliation, and requested quantity is never treated as a fill.
- **"No trade" is a success.** Over-trading is the small-account killer.
- **$100 → $1,000 live rehearsal:** `live_account` config can scale paper sizing to the real
  small account; prompts always factor the small-capital reality.
- **All times Mountain (MDT/MST auto via `America/Denver`).**

See **[docs/INTEGRATIONS.md](docs/INTEGRATIONS.md)** for FeintTrade/API analysis and what's next.
