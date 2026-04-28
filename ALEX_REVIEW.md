# Alex's Architecture Review — Polymarket Paper-Trading App
*April 28, 2026*

---

## Corrections to Initial Assumptions

**Position sizing is implemented** — `app/paper_economics.py` is solid. Notional sizing, CLOB depth-walking (VWAP), slippage, 50% fill threshold rejection, entry/winning fees. Good work Lucy.

**Execution modeling is implemented** — `walk_asks_buy` / `walk_bids_sell` are in place. The orderbook simulation is meaningful, not flat-price fantasy.

**Settlement is still T+24h mark-to-market** — `settle_trades.py` does not call the `app/resolution/` adapters. The ResolutionAdapter ABC and 8 concrete adapters are built but completely unwired. This is the single biggest gap.

---

## What the Code Actually Reveals

### 1. The `smoke_mkt` Contamination Problem

The fixture market `smoke_mkt` was the **only market receiving PriceSnapshots** before the tokenId fix, because real markets had `token_ids_json = null`. This means:

- Any backtest run before the fix is effectively analyzing `smoke_mkt` — a fake market.
- Lag measurements, drift windows, market lag scores from the pre-fix period are noise.
- Confirm whether `smoke_mkt` has `is_active = False` or is filtered out of analytics queries before treating any historical analysis as valid.

### 2. Settlement Is Financially Fictional

`settle_trades.py` settles at the market price 24 hours after trade entry. On Polymarket, a YES contract that resolves YES pays **$1.00**. A contract that resolves NO pays **$0.00**. Mark-to-market at T+24h can show a 10-cent gain on a trade that will eventually pay $0. The `ResolutionAdapter` package is the fix — it just needs to be called.

The correct settlement logic:
```
if market.resolved:
    use ResolutionAdapter outcome → WIN ($1.00/share) or LOSS ($0.00/share)
else:
    mark-to-market at current mid (interim P&L only, not final)
```

### 3. `_ensure_column()` Is a Time Bomb

`app/init_db.py` patches the live schema by adding missing columns on every startup. When a column gets *renamed* or a *type changes*, `_ensure_column()` silently does nothing and the app either crashes on query or silently reads nulls. There is no migration history. If the live SQLite file gets corrupted or needs to be rebuilt, there is no way to reconstruct the schema. Alembic is needed before the data becomes irreplaceable.

### 4. The Lag Pipeline Is an Island

`app/lag_config.py`, `app/core/market_classifier.py`, lag measurements, lag threshold crossings, market lag scores — all of this runs and populates the DB and displays on the `/analysis/lags` and `/analysis/laggy-markets` pages. But **none of it feeds back into gating or the signal pipeline**. `gating.py` does not consult `MarketLagScore`. The lag system is a research dashboard, not an adaptive signal system. If that is intentional for now, `lag_config.py` and `market_classifier.py` are misleadingly named — they sound like they affect live behavior.

### 5. LLM Cost Has No Guard

`batch_relevance_screen()` fires one GPT-4o-mini call per 8 markets per article. With 30 candidate markets per article and 10+ articles per poll cycle, that is 4+ LLM calls per article. On a high-volume news day (election night, major announcement), you can hit 200+ calls per run every 9 minutes. There is no `MAX_LLM_CALLS_PER_RUN` setting, no cost log table, and no circuit breaker. This will produce a surprise bill.

### 6. The Conservative Profile Will Silence the System

The **Conservative** threshold profile requires `DIRECT` evidence only. Most news articles will be classified as `PRELIMINARY` or `INDIRECT` by the interpreter, resulting in constant `WEAK_EVIDENCE` rejections and near-zero live trades. This is likely why the sprint notes mention adding a "Research" profile. Until that profile exists and is selected as default, the live pipeline is nearly inert.

### 7. Backtest Fill Price Is Optimistic

In `backtest_news_reactions.py`, simulated trades use `p0` (price at signal time from PriceSnapshot) as fill price with no slippage applied. The live pipeline uses the CLOB depth walk with slippage. The backtest will systematically show better fills than live would have achieved — a meaningful research bias that overstates backtest P&L.

---

## Jobs for Chad

### C1 — Data Integrity Audit *(Do this before anything else)*

Establish a clean data baseline before running any more analysis:

1. Identify the exact timestamp of the tokenId fix commit
2. Query how many `PriceSnapshot` rows exist before that timestamp, and which `market_id` values — if it is all `smoke_mkt`, those rows are useless for research
3. Query how many `PaperTrade` and `NewsSignal` rows predate the fix
4. Decide with Eric: truncate or tag pre-fix rows as `data_quality = PRE_FIX`
5. Confirm live snapshots are now flowing for real markets — log the first real market tokenId and its snapshot count
6. Document the data cutover date in `README.md`

### C2 — Wire ResolutionAdapters into settle_trades.py *(Highest-value task)*

The infrastructure is built in `app/resolution/`. Chad just needs to call it:

1. In `settle_trades.py`, after the T+24h check, call `ResolutionAdapter.resolve(market)`
2. If resolved: settle at $1.00/share (YES win) or $0.00/share (NO win) — do not use mid price
3. If not yet resolved: keep as interim mark-to-market
4. Tag trades `SETTLED_WIN`, `SETTLED_LOSS`, or `SETTLED_OPEN`
5. Add `resolution_source` field to `PaperTrade` to record which adapter resolved it

### C3 — Add "Research" Threshold Profile

The conservative profile is too strict to generate live trades under normal conditions:

1. Add a `research` profile to `threshold_profiles_seed.py`: allow INDIRECT evidence, `min_confidence = 0.55`, `max_article_age_minutes = 240`
2. Make it the default selected profile in `RuntimeSetting`
3. Verify it generates signals on the `/signals` page within one poll cycle after enabling

### C4 — Funnel Stats and /analysis/funnel Page *(Sprint B)*

Already planned. Complete this after C1–C3 so the funnel reflects clean data and real trade activity. The rejection breakdown (TOO_OLD / WEAK_EVIDENCE / LOW_CONFIDENCE / etc.) will immediately confirm whether C3 is working.

### C5 — LLM Cost Guard

Add to `app/settings.py`:
```python
MAX_LLM_CALLS_PER_RUN: int = 50
```
Pass this as a hard cap inside `batch_relevance_screen()`. Log total calls and estimated cost (GPT-4o-mini is approximately $0.15 per 1M input tokens) in the job response JSON. Surface cumulative estimated cost on the dashboard. One-hour task that prevents a potentially large surprise bill.

---

## Jobs for Lucy

### L1 — Alembic Setup *(Urgent, before data becomes irreplaceable)*

1. Install Alembic, init with current schema as baseline migration `0001_initial.py`
2. Replace `_ensure_column()` calls with proper migration scripts going forward
3. Document the data cutover date (pre/post tokenId fix) in the baseline migration comment
4. One-time setup that costs a day and protects the project

### L2 — Wire or Quarantine the Lag Pipeline

Make a clear decision on `lag_config.py` and `market_classifier.py`:

- **Option A (Wire it):** Have `gating.py` check `MarketLagScore` — if a market has a high lag score (moves fast after news), reduce the `min_confidence` requirement for that market. This is the long-term goal.
- **Option B (Quarantine it — recommended now):** Move `lag_config.py` and `market_classifier.py` to `app/experimental/` and add a module-level comment: *"Not connected to live pipeline — research use only."*

Option B is the right call now. Option A adds complexity before the lag data is trustworthy.

### L3 — Fix Backtest Slippage Bias

In `backtest_news_reactions.py`, apply the same slippage model as the live pipeline. Minimum fix: add `+ 0.01` (1 cent) to `p0` for BUY_YES and subtract for BUY_NO, matching the live `fill_price` calculation. Document that this is approximate but removes the directional bias. The current behavior systematically overstates backtest returns.

### L4 — P&L Truth Test *(Most important test in the repo)*

Write one pytest integration test that verifies settlement math end-to-end:

1. Seed a market with a known tokenId and two PriceSnapshots (entry price + resolution price)
2. Create a `NewsSignal` and `PaperTrade` (BUY_YES, entry at $0.60)
3. Mark the market as resolved YES
4. Run `settle_trades` logic
5. Assert `pnl_dollars ≈ (1.00 - 0.60) × contracts - fees`

This test does not exist. Until it does, you cannot trust the settlement math.

### L5 — Filter `smoke_mkt` from Analytics

Add `WHERE market_id != 'smoke_mkt'` (or `WHERE markets.is_fixture = False`) to:
- All analytics queries (lag measurements, signal metrics, lag ranks)
- The `/analysis/lags` and `/analysis/laggy-markets` page queries
- Dashboard summary counts

Better long-term: add `is_fixture: bool = False` to the `Market` model and filter on that field everywhere.

---

## The Three Gates to Real-World Validity

Before any P&L number is meaningful, three things must be true:

**Gate 1 — Clean data baseline** (Chad C1): Confirm when real price data started flowing. Tag or remove pre-fix noise. Prerequisite for everything else.

**Gate 2 — Real settlement** (Chad C2 + Lucy L4): Trades must settle at actual resolution outcomes, not T+24h mid prices. The infrastructure exists. Wire it in.

**Gate 3 — Trades actually firing** (Chad C3): If the live pipeline runs on Conservative profile and rejects everything as WEAK_EVIDENCE, there is nothing to analyze. The Research profile must be active before a meaningful sample of live trades accumulates.

When all three gates are clear: run two weeks of live paper trading on the Research profile. At 30+ resolved trades, compute win rate and average EV per trade. Compare to the Polymarket base rate (random YES/NO at $0.50 is a coinflip). If the system's win rate is statistically above 50% on resolved contracts, the signal has edge. That is the real test.

---

## Do Not Touch Right Now

- The two-stage matcher architecture — it is correct
- `app/paper_economics.py` — the CLOB simulation is solid
- The gating framework structure — correct, just needs the Research profile
- The backtest data models — well designed
- `app/resolution/` adapters — built correctly, just need to be called from `settle_trades.py`
