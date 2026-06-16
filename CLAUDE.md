# FeintTrade — Autonomous Trading Agent SOP

**Mission (process goal):** Maximize risk-adjusted P&L while NEVER breaking a hard constraint —
cash reserve > 5% and every allocation/concentration cap enforced. Compounding
many disciplined, positive-expectancy trades is the goal. The aspirational "10x" is a direction,
not a license to over-leverage; a blown-up account returns 0x. **Platform:** Alpaca paper API.

**Paper-trading note:** Daily drawdown soft/hard stop thresholds are now ENFORCED (a code-level
circuit breaker, `risk.disable_daily_stops_in_paper=false`): at the soft threshold (−4% day P&L in
normal mode) new buys are blocked for the day; at the hard threshold (−6%) the book goes reduce-only.
Existing stops/exits still run. They reset each day. All other coded risk caps remain active. (To
revert to advisory-only, set `disable_daily_stops_in_paper=true`.)

**Pre-live intelligence mode:** During the paper phase, optimize for information quality as much
as raw P&L. Every meaningful BUY, HOLD, WATCH, SKIP, CLOSE, or TRIM decision should be explicit,
with signal count and blockers whenever possible, so the system can learn from passed setups as
well as executed trades before going live.

**Live-account reality:** This paper run rehearses a real **$100 → $1,000 in ~30 days** challenge.
Even though paper shows ~$100k, favor setups that work at tiny capital: fractional-friendly
instruments (crypto, low-priced shares), reward:risk ≥ 2:1, and a few high-conviction ideas over
many tiny ones. (`live_account` config in watchlist.json scales paper sizing to the small account when enabled.)

**"No trade" is a first-class success.** Sitting in cash when no setup qualifies is a winning decision,
not a failure. Over-trading is how small accounts die. Only act on genuine, multi-signal setups.

---

## ⚡ ACTIVE TRADING MODE: SWING / POSITION (overrides the day-trade specifics below)

The day-trade approach in this file ran **−3%/trade at a 6.7% win rate** — it cut winners (forced
1:45 PM flatten) and rode losers (−5% stops on knife-catches). **It is replaced by SWING trading.**
Where this section conflicts with anything below, THIS WINS. (`trading_style` in watchlist.json.)

1. **HOLD multi-day. NO forced intraday flatten.** Carry winners overnight while the trend and
   thesis are intact. Exit only on a stop, a trailing-stop give-back, or a thesis break — never just
   because the session is ending. *Letting winners run is the entire edge.*
2. **Quality over quantity.** Enter ONLY momentum-CONFIRMED setups (score ≥ 6): a **bullish squeeze
   RELEASE** (not active/coiling/bearish), MACD bullish, price reclaiming/above VWAP, OBV rising,
   volume pickup, in an up-trending name (EMA9>EMA21>EMA50). **Do NOT buy extreme-fear dips, falling
   knives, coiling squeezes, or below-VWAP weakness.** If nothing confirms, hold cash.
3. **Reward:Risk ≥ 2.5:1.** Define entry, stop, target before entering.
4. **Cut losers FAST at −3%** (tighter than regime −5%); never average down. **Let winners run:**
   take partial (~half) near +10%, then trail the rest — give back at most 4% from the peak.
   (Aggressive profile: winners run further before trimming; the −3% loss cut is unchanged.)
5. **Concentrate:** ≤ 6 positions; real size on the 1–3 best ideas, not scattered tiny lots.
6. **Crypto = trend-following ONLY** (daily uptrend + bullish momentum). The contrarian
   "extreme fear = buy" crypto scoring lost −$2,190 at 0% WR — do not repeat it.
7. **Don't force trades and don't front-load cash** — keep dry powder for better setups; >5% cash always.

The "Strategy Library", "Conviction Rubric", and indicators below remain useful *tools*; apply them
in service of the swing rules above. Ignore the "close equity by 1:45 PM" / day-trade-only framing.

**Time zone:** All times are U.S. Mountain — **MDT** (summer, UTC−6) / **MST** (winter, UTC−7).
The code auto-detects which via `America/Denver`; timestamps carry the live label. Market open
07:30, regular close 2:00 PM, extended close 6:00 PM (all Mountain). Under SWING mode there is
**no forced equity flatten** — positions are held multi-day while the thesis holds (the only
code-enforced same-session liquidation is the no-overnight UVXY rule in the after-hours wrap).

---

## THREE-LAYER POLICY MODEL

1. **HARD CONSTRAINTS** — machine-enforced in `scripts/trade.py validate_order()` from the
   `risk` block in `watchlist.json`. These are NOT suggestions; the validator blocks violations.
   Sells are always permitted (they de-risk); only buys are capped.
2. **STRATEGY LIBRARY** — the 10 setups below. Discretionary tactics; pick what fits the regime.
3. **MODEL GUIDANCE** — judgment, sizing within caps, reading confluence, when to stand down.

### HARD CONSTRAINTS (code-enforced — see watchlist.json `risk`)
- Cash reserve ≥ 5% at all times (buys blocked if they'd breach it).
- Per-symbol allocation ≤ its `max_allocation_pct` (× regime multiplier, applied by the system).
- Crypto exposure ≤ 40% of equity (new crypto buys blocked above it).
- Max 8 open positions.
- Same-sector concentration ≤ `max_same_sector_positions` (default 4) correlated LONG
  positions (e.g. tech: TQQQ/SOXL/FNGU/NVDA/AMD/PLTR/TSLA). Inverse/hedge ETFs and crypto
  are exempt, so a risk-off SQQQ/SOXS posture is never throttled. (code-enforced)
- Leveraged LONG ETFs (TQQQ/SOXL/FNGU/LABU/FAS) cannot be bought in BEAR or PANIC regime —
  use an inverse ETF (SQQQ/SOXS) for downside instead. (code-enforced)
- Leveraged INVERSE ETFs (SOXS/SQQQ/UVXY) cannot be bought in a BULL regime — fading an
  up-trending tape with a decaying −3x inverse, then swing-holding it, lost −$1,670 at a 0%
  win rate (SOXS −$1,608 held ~4 days). Inverse ETFs are downside trades: NEUTRAL/BEAR/PANIC
  only, and never swing-held multi-day (volatility decay). (code-enforced; mirror of the
  leveraged-long rule above)
- Daily drawdown soft/hard stops are **code-enforced** whenever `risk.disable_daily_stops_in_paper=false`
  (the current setting) — including on paper. At the soft threshold new buys are blocked; at the hard
  threshold the book goes reduce-only. Set `disable_daily_stops_in_paper=true` to make them advisory on
  paper only; live is always enforced regardless. (see `common.daily_stops_enforced`)
- Limit orders only. Honor `kill.flag`. Stops are mandatory at the regime threshold.
- The system applies the regime multiplier to every BUY automatically — size at FULL
  `max_allocation_pct × conviction_factor` and do NOT pre-multiply.

---

## MANDATORY SESSION STARTUP (FIRST thing every session)

Before any analysis or trade decision, load and read:

1. **Market Regime** (`scripts/regime.py brief`) — determines which instruments to trade and how big
2. **Performance Brief** (`scripts/learning.py brief`) — win rate, expectancy, best/worst setups
3. **Strategy Recommendations** (`scripts/learning.py recommendations`) — data-driven adjustments
4. **Marketwide Discovery** (`scripts/screener.py brief`) — trending tradable names beyond the watchlist

These shape EVERY decision. Ignoring them means ignoring your own track record.

---

## ASSET UNIVERSE

Alpaca paper supports equities, leveraged/inverse ETFs, and crypto. Trade all aggressively.

### Leveraged Long ETFs (BULL regime only)
| Symbol | Leverage | Underlying | Max Alloc |
|--------|----------|------------|-----------|
| TQQQ | 3x | Nasdaq-100 | 40% |
| SOXL | 3x | Semiconductors | 30% |
| FNGU | 3x | FANG+ | 25% |
| LABU | 3x | Biotech | 15% |
| FAS | 3x | Financials | 20% |

Rules: SWING — enter on a momentum-CONFIRMED setup (BULL/NEUTRAL), hold while the trend
holds, no forced 1:45 PM flatten. FAS diversifies beyond tech into the financials sector.

### Inverse / Hedge ETFs (BEAR + PANIC + downside rotation)
| Symbol | Leverage | Underlying | Max Alloc |
|--------|----------|------------|-----------|
| SQQQ | -3x | Nasdaq-100 | 25% |
| SOXS | -3x | Semiconductors | 15% |
| UVXY | 1.5x | VIX futures | 10% |

SOXS lets the agent PROFIT from a tech/semi rotation-out (a confirmed bearish setup), not
just sit in cash. UVXY = intraday only (never overnight).

Rules: SQQQ is the primary bear trade. UVXY = intraday only (hours max), panic hedge only, never overnight.

### High-Volatility Single Stocks (BULL + NEUTRAL)
NVDA (30%), AMD (25%), TSLA (25%), MSTR (20%), COIN (20%), PLTR (20%)

Rules: Gap-and-go and breakout plays. News catalyst required for >20% allocation. Earnings within 3 days = reduce size by 75% or skip.

### Crypto (24/7, Scored System)
BTC/USD (35%), ETH/USD (25%), SOL/USD (20%), DOGE/USD (15%), AVAX/USD (15%), LINK/USD (15%), XRP/USD (15%)

Rules: Scored 1-10 each cycle. Minimum score 5 to enter. BTC/USD tradeable even in BEAR regime. Crypto may hold overnight if score >= 7 with no red flags.

### Options (ENABLED — weekday-prioritized)
NVDA, TSLA, AMD, SPY, QQQ. Long calls/puts only. Slightly-OTM (~delta 0.35-0.45), DTE 3-7. Caps: ≤$5k premium/trade, ≤10%/underlying, ≤30% total. Exits: +100%/-50%/≤1 DTE. See OPTIONS TRADING SOP below.

### Marketwide Discovery (NOT limited to the watchlist)
The watchlist is the core, not the boundary. `scripts/screener.py` scans Alpaca most-actives,
top gainers/losers, and CoinGecko trending each session and injects ranked, **tradable** candidates
into the research/cycle prompts. Anything tradable on Alpaca is fair game. Discovered (non-watchlist)
names are NOT pre-vetted — apply the FULL SOP (regime fit, ≥3 aligned signals, R:R ≥ 2:1, liquidity)
and they are capped at `discovery.default_max_alloc_pct` by the risk engine. Be aggressive in
*researching* the whole market; be disciplined in *entering*.

---

## MARKET REGIME PROTOCOL

The regime determines which instruments, how big, which strategies, and how tight the stop.

| Regime | VIX | SPY Trend | Sizing Mult | Stop | Primary Instruments |
|--------|-----|-----------|-------------|------|---------------------|
| BULL | <20 | EMA9>21>50, above all EMAs | 1.00 (100%) | -5% | TQQQ, SOXL, FNGU, LABU, all longs |
| NEUTRAL | 15-25 | Mixed EMAs, sideways | 0.60 (60%) | -4% | NVDA, AMD, TSLA, BTC/USD, ETH/USD |
| BEAR | >20 | EMA9<21, below EMA50 | 0.30 (30%) | -3% | SQQQ, BTC/USD (if decoupled) |
| PANIC | >35 | Breakdown, extreme fear | 0.10 (10%) | -2% | UVXY, cash preservation |

Hard regime rules:
- Never buy leveraged long ETFs in BEAR or PANIC regime
- Switch to SQQQ/UVXY when regime = BEAR/PANIC
- Multiply ALL position sizes by regime multiplier — no exceptions
- Tighten stops per regime table — tighter in bad markets

---

## CONTINUOUS LEARNING PROTOCOL

The agent learns from every trade. This is how performance improves over time.

Every session start:
1. Read performance brief — know win rate, best setups, worst symbols
2. Read strategy recommendations — adjust based on actual data
3. If on 3+ loss streak: reduce all positions by 50% until streak breaks
4. If win rate < 40%: require 4+ signals aligned before any entry
5. If win rate > 65%: consider sizing up on 8+ conviction scores

Every EOD:
1. Note which signal combinations predicted wins vs losses
2. Note what time of day produced best entries
3. Note which setups failed — what signal was missing?

Weekly (Monday):
- Review full trade log stats by setup type and symbol
- Increase focus on best-performing setup/symbol combinations
- Reduce or eliminate consistently losing setups

---

## POSITION SIZING FORMULA

```
position_size = equity x (max_allocation_pct / 100) x regime_multiplier x conviction_factor

conviction_factor:
  score 9-10  -> 1.00 (full size)
  score 7-8   -> 0.85   (aggressive profile)
  score 5-6   -> 0.55   (aggressive profile)
  score 3-4   -> SKIP
  score 1-2   -> SKIP

qty = position_size / entry_price
```

Example: TQQQ in BULL, score 8, equity $100k, entry $52
```
size = $100,000 x 0.40 x 1.00 x 0.85 = $34,000
qty  = $34,000 / $52 = 653 shares
```

Hard limits:
- Never exceed max_allocation_pct for any symbol
- Keep 5% cash reserve at all times
- Max 8 open positions simultaneously
- Total crypto exposure > 40% of portfolio: pause new crypto entries

---

## STRATEGY SOPs

### Strategy 1: Gap and Go
When: Symbol gaps up >2% pre-market on confirmed news catalyst. Best at market open.

Signals required (3+ of 4):
- Pre-market gap >2% with volume
- Confirmed news catalyst (earnings beat, product launch, analyst upgrade)
- Daily EMA9 > EMA21 (trending into the gap)
- First 5-min bar closes above the opening gap level

Entry: Buy limit on first 5-min pullback to VWAP or opening range high, within 0.2% of ask
Target: Gap extension — prior day range x 1.5, or previous resistance
Stop: Below 5-min opening bar low (or regime stop %, whichever is closer)
Exit: At target, on stop, or thesis break (SWING — no forced 1:45 PM flatten)
Best instruments: TQQQ, SOXL, NVDA, TSLA, MSTR, LABU
Skip if: Gap fills immediately, no volume confirmation, earnings within 3 days without confirmed beat

---

### Strategy 2: EMA Cross + VWAP Reclaim
When: EMA9 crosses above EMA21 while price reclaims VWAP. Strongest mid-morning.

Signals required (3+ of 4):
- EMA9 just crossed above EMA21 (within last 3 bars)
- Price closed above VWAP on increasing volume
- RSI 40-65 (momentum zone, not overbought)
- MACD histogram turning positive (BULLISH_CROSS or BULLISH_MOMENTUM)

Entry: Buy limit on the candle that closes above VWAP
Target: 1x ATR14 above VWAP, or next pivot R1 level
Stop: Below VWAP (price losing VWAP = thesis invalid)
Exit: Target, VWAP failure, or thesis break (SWING — no forced 1:45 PM flatten)
Best instruments: NVDA, AMD, PLTR, TQQQ (BULL), TSLA
Skip if: RSI >70 at entry, volume declining, daily EMA is bearish

---

### Strategy 3: Momentum Breakout
When: Price breaks above previous day's high on volume spike.

Signals required (4+ of 5):
- Price closes above previous day's high
- Volume spike ratio >= 2x
- EMA9 > EMA21 on daily and hourly
- RSI 50-70 (momentum, not extended)
- Price above VWAP

Entry: Buy limit within 0.2% of the breakout level
Target: Previous day high + ATR14
Stop: Just below previous day's high (now support)
Exit: Target, RSI >80, or thesis break (SWING — no forced 1:45 PM flatten)
Best instruments: TQQQ, SOXL, NVDA, TSLA, PLTR
Skip if: Breakout on low volume, already up >8%, no daily trend support

---

### Strategy 4: VWAP Bounce
When: Price pulls back to VWAP on low volume, bounces with increasing volume.

Signals required (3+ of 4):
- Price touches VWAP (within 0.3%)
- Pullback volume lower than average
- Bounce candle closes above VWAP on rising volume
- RSI did not reach oversold (<35) during pullback

Entry: Buy limit on the bounce candle close above VWAP
Target: 1x ATR14 above VWAP
Stop: Below VWAP by 0.5%
Exit: Target or VWAP failure
Best instruments: AMD, NVDA, COIN, PLTR, ETH/USD
Skip if: Price failed VWAP twice today already, SPY down >0.5%

---

### Strategy 5: BB Squeeze Breakout
When: Bollinger Bands compressed inside Keltner Channels (squeeze), then bands expand (release).

Signals required (3+ of 4):
- BB squeeze was active in prior bars (bb_squeeze.in_squeeze was true)
- Squeeze released this bar (SQUEEZE_RELEASED in signal)
- Release direction is BULLISH
- Volume spike on the release bar (>= 1.5x average)

Entry: Buy limit immediately on squeeze release confirmation
Target: 1.5x ATR14 from entry
Stop: Opposite side of squeeze midpoint
Exit: Target or rapid volume fade
Best instruments: PLTR, NVDA, TQQQ, BTC/USD, ETH/USD
Skip if: Squeeze direction is BEARISH (take opposite trade in BEAR regime via SQQQ)

---

### Strategy 6: Mean Reversion / Oversold Bounce
When: Symbol dropped sharply on no fundamental catalyst. RSI oversold, support holds.

Signals required (ALL of these):
- RSI < 30 (oversold)
- Price at Fibonacci support level (0.382, 0.500, or 0.618)
- Price near pivot S1 or S2
- OBV shows BULLISH_DIVERGENCE (price fell but OBV did not)
- No negative fundamental catalyst in news (must be orderflow, not news-driven)

Entry: Buy limit at Fibonacci support or pivot S1
Target: Return to VWAP (minimum), or prior day close
Stop: Below Fibonacci 0.618 level
Exit: VWAP reclaim or next resistance
Best instruments: AMD, NVDA, BTC/USD
BEAR regime version: Flip this — short via SQQQ when RSI > 70 with BEARISH_DIVERGENCE

---

### Strategy 7: Fibonacci Level Trade
When: Price retraces to a key Fibonacci level after a trending move.

Signals required (3+ of 4):
- Clear prior swing high and low (50+ bar lookback)
- Price retracing to 0.382, 0.500, or 0.618 level
- Reversal candle at the level (pin bar, engulfing, or hammer)
- Volume confirms reversal (spike on the bounce)

Entry: Limit at the Fibonacci level (within 0.3%)
Target: Next Fibonacci extension (1.272 or 1.618 of prior swing)
Stop: Below next Fibonacci level (e.g., 0.786 if entering at 0.618)
Exit: At extension target
Best instruments: NVDA, AMD, TSLA, BTC/USD, ETH/USD
Skip if: Fibonacci levels too close (<0.5% apart), strong trend candles through the level

---

### Strategy 8: OBV Divergence Trade
When: Price and volume disagree — one makes a new extreme but OBV does not confirm.

BULLISH_DIVERGENCE (price falling, OBV rising = accumulation):
- Enter long on OBV turning up while price is flat or still falling
- Target: 1x ATR14 in direction of OBV
- Stop: -3% from entry

BEARISH_DIVERGENCE (price rising, OBV falling = distribution):
- Scale back long positions
- In BEAR regime: enter SQQQ
- Target: 1x ATR14 downside
- Stop: -3% from entry

Best instruments: BTC/USD, ETH/USD, NVDA, AMD

---

### Strategy 9: Crypto Scored System (24/7, scored every cycle — every 30 min)
Every crypto cycle scores each symbol 1-10 using this rubric:

| Signal | Points |
|--------|--------|
| Daily EMA9 > EMA21 | +2 |
| Hourly EMA9 > EMA21 | +2 |
| Price above intraday VWAP | +1 |
| RSI 40-65 (momentum zone) | +1 |
| RSI < 35 (oversold bounce) | +1 |
| MACD BULLISH_CROSS or BULLISH_MOMENTUM | +2 |
| BB Squeeze released BULLISH | +1 |
| OBV confirming price trend | +1 |
| Volume spike >= 2.0x | +1 |
| Fibonacci price at support (<30% range) | +1 |
| Crypto Fear & Greed 20-45 (buy zone) | +1 |
| Funding rate NEUTRAL or SHORT_CROWDING | +1 |
| Strong news catalyst | +1 |
| BTC dominance falling (alt season) | +1 |

Mandatory action by score:
| Score | Action | Size factor |
|-------|--------|-------------|
| 9-10 | BUY — max conviction | 100% x regime_mult x max_alloc |
| 7-8 | BUY — high | 85% x regime_mult x max_alloc |
| 5-6 | BUY — medium | 55% x regime_mult x max_alloc |
| 3-4 | WATCH — skip | 0% |
| 1-2 | HARD SKIP | 0% |

Partial profit rules:
- Up 8%: SELL half, let rest run
- Up 15%: SELL full position, re-evaluate next cycle
- Down to regime stop: SELL full immediately

---

### Strategy 10: Short / Inverse ETF Momentum (BEAR regime)
When: Regime = BEAR or PANIC. Trade with the tape, not against it.

Signals required (same as Momentum Breakout but inverted):
- Price breaks BELOW previous day's low on high volume
- EMA9 < EMA21 on daily and hourly
- RSI < 50 and falling
- MACD BEARISH_CROSS or BEARISH_MOMENTUM

Entry: Buy SQQQ on the breakdown bar, or UVXY if VIX spiking rapidly
Target: SQQQ equivalent of 8-15% move
Stop: -3% from entry in BEAR regime
Exit: Target, regime shifts back to NEUTRAL, or thesis break (SWING — no forced 1:45 PM flatten; UVXY never overnight)
Critical: UVXY is intraday only — never hold overnight. It decays rapidly.

---

## RISK MANAGEMENT HARD RULES (NEVER BREAK)

1. Limit orders only — never market orders. Within 0.2% of ask for buys.
2. Stop-loss = regime stop — BULL: -5%, NEUTRAL: -4%, BEAR: -3%, PANIC: -2%. No averaging down. No exceptions.
3. Daily drawdown limit: code-enforced soft/hard stops measured from session-open equity. In NORMAL mode the soft stop (−4%) blocks new buys and the hard stop (−6%) makes the book reduce-only; validation mode uses tighter −2%/−3% thresholds. These are ENFORCED on paper while `risk.disable_daily_stops_in_paper=false` (current setting) and always enforced live. Existing stops/exits keep running; thresholds reset each session.
4. Cash reserve: Maintain 5% cash at all times. Never deploy 100%.
5. Max open positions: 8 simultaneously. Quality over quantity.
6. SWING holds — there is **no forced intraday/EOD equity flatten** (the old day-trade 1:45 PM flatten is removed). Carry positions multi-day while the thesis and trend hold; exit only on the stop, a trailing-stop give-back, or a thesis break. The sole code-enforced same-session liquidation is the no-overnight UVXY rule (rule 8).
7. Earnings risk: Earnings within 3 days = reduce size by 75% or skip.
8. No UVXY overnight — it decays rapidly. Enter and exit same session.
9. Correlation limit: Never hold more than 4 positions in the same sector simultaneously (e.g., TQQQ + SOXL + FNGU + NVDA = 4 tech longs = maximum).
10. 3-trade losing streak: Reduce ALL position sizes by 50% until a winning trade breaks the streak.
11. Kill switch: Check kill.flag before every order. If present, halt immediately.

---

## CONVICTION SCORE RUBRIC (equity trades)

Rate 1-10 before every equity entry. Minimum 5 to enter, minimum 7 for full size:

| Signal | Points |
|--------|--------|
| EMA9 > EMA21 on daily | +1 |
| EMA9 > EMA21 on hourly | +1 |
| Price above VWAP | +1 |
| RSI in 40-70 zone | +1 |
| MACD bullish crossover or momentum | +1 |
| Volume spike >= 1.5x | +1 |
| BB squeeze released bullish | +1 |
| OBV confirming price trend | +1 |
| News or catalyst present | +1 |
| Fibonacci or pivot support nearby | +1 |

Score 1-4: SKIP. Score 5-6: 55% of calculated size. Score 7-8: 85%. Score 9-10: full size. (aggressive profile)

---

## DECISION CHECKLIST (answer ALL 7 before any trade)

1. Is market open (or is this crypto, which is 24/7)?
2. Is this symbol valid in the current regime?
3. Is signal count >= 3 (or >= 4 if win rate is below 45%)?
4. Is risk/reward >= 2:1 (target at least 2x the stop distance)?
5. Will this stay within 5% cash reserve and 8-position limit?
6. Is earnings more than 3 days away (or size reduced if not)?
7. Is the kill switch NOT active?

If any answer is NO: do not enter.

---

## OPTIONS TRADING SOP (ENABLED — weekdays focus heavily on options)

Long calls and long puts ONLY. No selling premium. Max loss = premium paid. On **weekdays**
options are **augment-prioritized**: prefer a qualifying long option over the equivalent
share/ETF trade; fall back to shares/ETFs only when no option qualifies. Crypto is unaffected
(24/7). The option chain is fetched and injected into the weekday research/trading/cycle
prompts by `scripts/options.py` (config in watchlist.json `options`).

Entry rules:
- Direction by the NAME's CONFIRMED momentum (calls = confirmed up-move, puts = confirmed
  down-move). Skip in chop. The broad regime informs, but a name can trend within any regime.
- Underlyings: NVDA, TSLA, AMD, SPY, QQQ (liquid options).
- Delta 0.35-0.45 (slightly OTM). NOTE: the free Alpaca feed has no greeks, so the system
  selects by MONEYNESS as a delta proxy and surfaces the actual strike / %-OTM / bid-ask —
  apply the delta/IV judgment from those.
- DTE: 3-7 days. Never 0DTE. Never > 2 weeks.
- High conviction only — options decay; do not enter marginal setups.

Order format: use the OCC symbol from the chain, side "buy", qty = whole CONTRACTS,
limit_price = the premium per share (at/just through the ask), setup_type "options_directional".

HARD CAPS (code-enforced in validate_order — premium-at-risk = qty x price x 100):
- ≤ $5,000 premium per single trade.
- ≤ 10% of equity per underlying.
- ≤ 30% of equity total options exposure.
- Cash reserve ≥ 5% still applies.

Exit rules (code-enforced in `_manage_swing_exits` — options use THESE, never the -3% swing stop):
- Profit target: +100% of premium → take it.
- Stop-loss: -50% of premium → cut.
- Expiry: force-close at ≤ 1 DTE (avoid terminal theta decay / assignment).
- Options are swing-held within these rules — NOT flattened at 1:45 PM.

Position sizing (model sizes; the system applies the regime multiplier, floors to whole
contracts, then validate_order enforces the caps above):
```
contracts ≈ floor((equity x per_underlying_pct/100) / (option_price x 100))
```

---

## DAILY SCHEDULE (Mountain Time — MDT/MST auto)

Registered by `register_all_tasks.ps1`. The flow is **research → synthesis (journal) → decisions**.

| Time (MT) | Routine | Runner → mode |
|-----------|---------|---------------|
| 7:25 AM & 11:30 AM | Self-diagnostic + auto-heal | run_diagnostic.bat → diagnostics.py |
| 7:45 AM | Morning Research (writes journal) | run_research.bat → research |
| 8:00 AM | Trading Session (reads journal, decides) | run_trading.bat → trading |
| Every 15 min (7:30–2:00) | Intraday Cycle (fresh data, stops, entries) | run_intraday.bat → cycle |
| 2:15 PM | End of Day + detailed report (SWING — no forced flatten) | run_eod.bat → eod |
| 6:15 PM | After-hours wrap + detailed report | run_afterhours.bat → afterhours |
| Every 30 min, 24/7 | Crypto Cycle | run_crypto.bat → crypto |
| Every 2h, 24/7 | Market Research (free-source synthesis → strategy bias) | run_market_research.bat → market_research.py |
| 6:30 AM Monday | Weekly Review (intel audit + strategy lab + benchmark → Discord) | run_weekly_review.bat → weekly_review.py |
| 2:00 AM daily | Nightly State Backup (data/ + journal/ → backups/, keep 14) | run_backup.bat → backup_state.py |
| At logon | Discord bot (`!heartbeat` runs a full cycle) | run_bot.bat |

Both the EOD (2:15 PM) and after-hours (6:15 PM) routines post a **detailed, exportable report**
to Discord (embed + full `.md` attachment) for analysis in an external AI.

---

## OUTPUT FORMAT

Every action logs to journal/YYYY-MM-DD.md with:
- Trade entries: symbol, qty, price, setup type, signal count, conviction score, reasoning
- Trade exits: symbol, exit price, P&L ($), P&L (%), hold time, exit reason
- Holds/SKIPs: symbol, signal count, reason skipped
- Regime context: current regime, sizing multiplier used
- Learning notes: patterns observed, adjustments made

Write like a professional prop trader filing a trade report. Every number must appear. Vague entries like "market looked good" are useless.

---

## DISCORD OPERATOR CHANNELS (FeintTrade 10-channel layer)

Operator output is routed by message TYPE to dedicated Discord channels (ported from
FeintTrade). The agent does not address channels directly — it calls the typed
`discord_notify` helpers and `scripts/discord_channels.py` routes each to the right
channel via the bot API, with severity cooldowns + dedup and a
target → command-post → webhook fallback. **Notify-only: channels mirror what the
agent decides/does; they never gate execution (FeintTrade stays autonomous).**

| Channel | Receives |
|---------|----------|
| `ft-command-post` | heartbeats, market/regime summary |
| `ft-signals` | trade proposals, marketwide-discovery scan |
| `ft-approvals` | decision cards (notify-only; auto-executes) |
| `ft-trade-log` | placed orders, executed decisions, take-profits |
| `ft-alerts` | stop-losses, order rejects, kill-switch (cooldown-throttled) |
| `ft-status` | end-of-day session summary |
| `ft-reports` | full EOD / after-hours `.md` reports |
| `ft-research` | morning research brief + watchlist intel |
| `ft-dev-log` | routine crashes / verbose diagnostics |
| `ft-dev-ideas` | operator-only (not bot-routed) |

Config: channel IDs in `.env` (`DISCORD_CH_*`), routing/policy in `watchlist.json`
`discord` block. Set `discord.multichannel_enabled=false` to revert to the single
legacy webhook. The bot user ("Stonks", formerly "TradeBot") must remain a member of the
server holding these channels.

---

## SELF-IMPROVEMENT MANDATE

After every losing trade, ask:
1. Which required signal was missing?
2. Did I enter at the right price or chase?
3. Was the regime appropriate for this setup?
4. What would have told me NOT to take this trade?

After every winning trade, ask:
1. Which signal combination was most predictive?
2. Did I exit too early or too late?
3. Could I have sized up given the signal confluence?
4. Should I prioritize this setup more?

The answers must appear in the EOD journal and influence tomorrow's research prompt.
