# Backtest Harness — Implementation Plan
*Architecture: Alex | April 2026*

This document is the authoritative sequence for building the historical backtest system.
**Read the full plan before writing any code.** Work must follow the sprint order — each
sprint has a gate that must pass before the next sprint begins.

---

## Guiding principles

1. **Tier 1 before anything else.** Crypto markets with Binance candle times are the cleanest
   possible signal source. They prove the lag engine works before noise is introduced.

2. **Validate the API before writing infrastructure.** The entire plan depends on
   Polymarket's `/prices-history` CLOB endpoint. Chad must confirm it returns usable data
   for 10 resolved markets before any harness code is written.

3. **No look-ahead bias.** The signal event must be reconstructed *before* price movement
   is analysed. The harness must log `source_found_before_price_analysis: true/false` on
   every case. Any case where this is false is invalid.

4. **`timestampConfidence` gates everything.** `LOW` confidence signals are excluded from
   all primary analysis. This is not optional.

5. **Do not build `BiasAnnotator` or `HypothesisEvaluator` in this plan.** Those are a
   separate project. This plan delivers the data foundation they will eventually consume.

---

## Who owns what

| Area | Owner |
|---|---|
| DB models, schema migrations, `init_db.py` | **Lucy** |
| API adapters, job runners, price loaders | **Chad** |
| Test coverage for new models and jobs | both — see each sprint |

**Rule:** Lucy merges schema changes first. Chad branches off Lucy's merged schema commit.
This prevents the most common conflict: Chad writing a job against a model column that
Lucy hasn't landed yet.

---

## Tier overview

```
Tier 1  Fully structured — both outcome and signal time are structured data
Tier 2  Semi-structured — price history is clean, signal comes from official release
Tier 3  News reconstruction — signal discovered via broad web/news data
```

Tier 3 is **not in this plan.** It is scoped separately when Tiers 1 and 2 are proven.

---

## Spike — do this before Sprint 1 begins

**Owner: Chad. Time-box: 2 hours.**

Hit the Polymarket CLOB `/prices-history` endpoint for 10 resolved markets (mix of
crypto, sports, and one political market). Confirm:

- The endpoint returns a usable price series for resolved markets.
- The series covers the period from market open to resolution.
- Rate limits are acceptable for bulk historical fetching.
- Timestamps are UTC and reliable.

Write findings in a short note at the bottom of this file under **Spike Results**.
If the endpoint is unusable or severely rate-limited, stop and flag to Eric before
any further work. The architecture will need revision.

**Gate:** Spike results written and confirmed before Sprint 1 starts.

---

## Sprint 1 — Crypto control backtest

**Goal:** 100 BTC/ETH/SOL hourly markets, Binance candle signal times, Polymarket price
history. Validates the lag engine end-to-end with zero noise.

**Lucy delivers first:**

- [ ] Add `BacktestSignalEvent` model to `app/models.py`:

```
id                        str  PK
backtest_run_id           str  FK → backtest_runs.id
market_id                 str  FK → markets.id
source_name               str  (e.g. "binance", "sports_api", "tsa")
source_url                str  nullable
source_type               str  OFFICIAL_DATA | OFFICIAL_RELEASE | NEWS_ARTICLE | ARCHIVE
source_tier               str  HARD | SOFT
signal_time_utc           datetime
timestamp_confidence      str  HIGH | MEDIUM | LOW
timestamp_source_type     str  OFFICIAL_TIMESTAMP | ARTICLE_TIMESTAMP | APPROXIMATED
implied_outcome           str  YES | NO
title                     str  nullable
body_excerpt              str  nullable
event_reconstruction_method str
source_found_before_price_analysis  bool  default False
raw_source_json           JSON nullable
data_quality_notes        str  nullable
created_at                datetime
```

- [ ] Add migration in `init_db.py` for the new table.
- [ ] Add `price_history_json` nullable JSON column to `BacktestCase`
  (stores the raw `/prices-history` response for audit).
- [ ] Write one model-level test: seed a `BacktestSignalEvent`, assert all fields
  round-trip correctly through the ORM.

**Chad starts after Lucy's schema is merged:**

- [ ] Write `app/jobs/load_price_history.py`:
  - Calls Polymarket CLOB `/prices-history` for a given token ID and time range.
  - Returns a normalised list of `{timestamp, price}` dicts.
  - Respects rate limits (add `asyncio.sleep` between calls; start at 0.5s).
  - Returns `None` on non-200 or timeout — never raises.

- [ ] Extend `app/resolution/binance.py` for **historical** candle fetching:
  - Add `fetch_candle_at(symbol, timestamp)` method that returns the candle
    close/open at a specific UTC time.
  - Signal time = scheduled candle close time (e.g. top of each hour).
  - `timestampConfidence = HIGH`.

- [ ] Write `app/jobs/backtest_crypto_control.py`:
  - Selects resolved binary markets from the DB where `category = 'crypto'`
    or question matches BTC/ETH/SOL patterns.
  - For each market:
    1. Fetch price history via `load_price_history`.
    2. Fetch Binance candle at the relevant scheduled time.
    3. Create `BacktestSignalEvent` with `source_found_before_price_analysis = True`
       (candle close time is always before market price movement for the hour).
    4. Convert to `NewsSignal` with `signal_source_type = 'BACKTEST_BINANCE'`.
    5. Compute lag measurements using the existing `LagMeasurement` service.
    6. Store results under a `BacktestRun`.
  - Cap at 100 markets per run.
  - Skip any market where price history returns fewer than 10 data points.

- [ ] Add `POST /api/jobs/backtest_crypto_control` route to `app/routers/api.py`.

- [ ] Write tests covering:
  - `load_price_history` returns `None` gracefully on API error.
  - `backtest_crypto_control` skips markets with insufficient price history.
  - A synthetic end-to-end case: known p0, injected price series, assert lag10pt
    matches expected value.

**Sprint 1 gate (both):** Run 100 crypto markets. Inspect 10 manually. Lag calculations
must match what you would compute by hand from the Binance candle and price series.
Write results in **Sprint 1 Results** at the bottom of this file.

---

## Sprint 2 — Sports and TSA control backtest

**Depends on:** Sprint 1 gate passed.

**Goal:** 50 completed sports game markets + 20 TSA passenger-count markets. Tests
short-cycle resolution (sports) and official structured data (TSA).

**Lucy delivers first:**

- [ ] Add `source_name` index to `BacktestSignalEvent` if query performance requires it.
- [ ] Add `backtest_type` column to `BacktestRun`
  (`CRYPTO_CONTROL | SPORTS_CONTROL | TSA_CONTROL | OFFICIAL_RELEASE | NEWS_RECONSTRUCTION`)
  so runs can be filtered in the UI.
- [ ] Migration in `init_db.py`.

**Chad starts after Lucy's schema is merged:**

- [ ] Extend `app/resolution/sports.py` for historical signal fetching:
  - `fetch_final_score_at(game_id, game_end_time)` — returns official final score
    and game-end timestamp.
  - Signal time = official game-end time (from league API or ESPN feed).
  - `timestampConfidence = HIGH`.

- [ ] Extend `app/resolution/tsa.py` for historical signal fetching:
  - `fetch_throughput_for_date(date)` — returns TSA checkpoint throughput and
    the publication timestamp of that day's data.
  - `timestampConfidence = HIGH` if publication timestamp known, `MEDIUM` otherwise.

- [ ] Write `app/jobs/backtest_sports_control.py` (same pattern as crypto control).
- [ ] Write `app/jobs/backtest_tsa_control.py` (same pattern).
- [ ] Add routes for both to `app/routers/api.py`.
- [ ] Tests: one synthetic end-to-end per job.

**Sprint 2 gate:** 50 sports + 20 TSA markets run cleanly. No `source_found_before_price_analysis = False` cases in either batch. Write results in **Sprint 2 Results**.

---

## Sprint 3 — Official release backtest (Tier 2)

**Depends on:** Sprint 2 gate passed.

**Goal:** Court decisions, government actions, economic releases, earnings. These have
reliable official timestamps but require more source judgment than Tier 1.

**Market types in scope:**
- Supreme Court / court decision markets → `supremecourt.gov`, court docket pages
- Government action markets → White House, Federal Register, Treasury/OFAC
- Economic data markets → BLS, BEA, FRED
- Company earnings markets → SEC EDGAR, investor relations pages

**Lucy delivers first:**

- [ ] Add `source_review_status` column to `BacktestSignalEvent`:
  `PENDING | APPROVED | REJECTED`. Defaults to `PENDING`.
- [ ] Add `reviewed_by` and `reviewed_at` nullable columns.
- [ ] Migration in `init_db.py`.
- [ ] Add `/analysis/backtest-signal-review` UI page: lists PENDING events, lets
  a human approve or reject each one before it enters primary analysis.

**Chad starts after Lucy's schema is merged:**

- [ ] Write `app/resolution/official_release.py`:
  - Generic adapter for official release pages (court, government, economic).
  - Fetches the release page, extracts the publication timestamp.
  - Returns `timestampConfidence = HIGH` only if timestamp is machine-readable
    (not just "published today").
  - Falls back to `MEDIUM` otherwise.

- [ ] Write `app/jobs/backtest_official_release.py`:
  - Selects resolved markets in the relevant categories.
  - For each: fetches official source event, sets `source_review_status = PENDING`.
  - Does **not** compute lag or create `NewsSignal` until `source_review_status = APPROVED`.
  - A second pass (triggered manually) processes approved events into full backtest cases.

- [ ] Tests: mock official release page, assert timestamp extraction and
  confidence assignment work correctly.

**Sprint 3 gate:** 25 court/government markets run. All have `source_review_status`
set. At least 20 are approved and have full lag measurements. Write results in
**Sprint 3 Results**.

---

## Tier 3 — News reconstruction (GDELT / AP / Reuters)

**This tier is not in scope for this plan.** It is a separate project.

Do not begin Tier 3 work until:
- All three Sprint gates above are passed and written up.
- Eric has reviewed Sprint 1–3 results and confirmed the lag engine is producing
  trustworthy numbers.
- A separate scoping session has defined the GDELT integration boundaries.

Key constraints when Tier 3 is eventually built:
- Every GDELT candidate requires human review before it becomes an approved signal.
- `LOW` timestamp confidence signals are excluded from primary analysis permanently.
- The `source_found_before_price_analysis` log line is mandatory.

---

## Acceptance criteria (applies to all sprints)

A backtest case is valid only when all of these are true:

- Market metadata is stored and resolution source is known.
- Token price history is available with at least 10 data points.
- Signal timestamp is UTC.
- `timestampConfidence` is `HIGH` or `MEDIUM`.
- `source_found_before_price_analysis = True`.
- p0 is available (price at signal time).
- Spread and liquidity filters were applied.
- Lag result is auditable — raw price history stored in `price_history_json`.

---

## Commit discipline

To avoid stepping on each other:

1. Lucy opens a PR for schema changes. Chad does not touch models or `init_db.py`
   until that PR is merged.
2. Chad branches off `main` after Lucy's merge. Chad's job files import the new
   models but do not modify them.
3. Neither touches `app/routers/ui.py` or `app/routers/api.py` at the same time —
   coordinate who owns that file for each sprint.
4. Run `pytest` before every push. If tests fail, fix before pushing.
5. No force pushes to `main`.

---

## Results log

*(Chad and Lucy: fill these in as each gate is passed.)*

### Spike Results
- Date:
- Endpoint tested:
- Markets checked:
- Rate limit observed:
- Data quality notes:
- Decision (proceed / revise architecture):

### Sprint 1 Results
- Date completed:
- Markets run:
- Manual spot-checks passed:
- Lag calculation accuracy notes:
- Issues found:

### Sprint 2 Results
- Date completed:
- Sports markets run:
- TSA markets run:
- `source_found_before_price_analysis = False` count:
- Issues found:

### Sprint 3 Results
- Date completed:
- Markets run:
- Approved / rejected / pending breakdown:
- Issues found:
