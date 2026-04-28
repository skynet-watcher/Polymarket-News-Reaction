## Polymarket News-Reaction (Paper Trading MVP)

Sprint tasks for Chad: see [`CHAD_SPRINT.md`](CHAD_SPRINT.md) (includes a **solo overnight** checklist). Status UI / Lucy notes: [`LUCY_STATUS_UI_HANDOFF.md`](LUCY_STATUS_UI_HANDOFF.md). Source repo: [github.com/skynet-watcher/Polymarket-News-Reaction](https://github.com/skynet-watcher/Polymarket-News-Reaction).

---

## Engineering Log

> Maintained by **Alex** (reviewer/architect). Most recent entry first.
> Chad and Lucy: read this before starting new work — it covers bugs fixed, architectural decisions, and things that look wrong but aren't.

---

### 2026-04-28 — Critical: real markets never got price snapshots (`token_ids_json` was always null)

**Symptom:** Dashboard showed "last price sync" as hours ago even after a successful `sync_markets` run. Only the `smoke_mkt` fixture market ever had `PriceSnapshot` rows.

**Root cause:** The Gamma `/events` endpoint (used as the primary market fetch) does **not** return `tokenIds`. Without a token ID, the CLOB bid/ask fetch is skipped and no `PriceSnapshot` is written. All 11k+ real markets landed in the DB with `token_ids_json = null` on every sync.

**Fix (`app/jobs/sync_markets.py` — commit `8e53418`):** `_fetch_all_markets_unified` now always runs **both** the `/events` path (volume-sorted, richer metadata) **and** the `/markets` path (provides `tokenIds`). Results are merged: `/events` seeds the list, `/markets` patches in `tokenIds` and fills other gaps. The fixture fallback still fires when **both** primary paths fail (403/5xx).

**What this means for you:**
- First sync after this fix will populate `token_ids_json` for all markets that have it.
- CLOB price fetches and snapshots will start working for real markets.
- If you have an old `data.db`, trigger **Sync markets** once manually and watch snapshots tick up.

---

### 2026-04-28 — Backtest: trade marking and signal_action added

**Context:** Chad built the `backtest_news_reactions` job. Review identified two gaps.

**Gaps fixed (commit `c3577ec`):**

1. **`PaperTrade.trade_source`** (`LIVE` | `BACKTEST`, default `LIVE`) — every trade now carries its origin. All existing rows default to `LIVE`. Backtest-simulated trades are tagged `BACKTEST` and carry a `backtest_case_id` FK back to the `BacktestCase` that generated them.

2. **`BacktestCase.signal_action`** — records what the live pipeline actually did for this signal (`ACT` / `ABSTAIN` / `CANDIDATE` / `REJECT_*`). Without this field there was no way to separate "trades we made" from "opportunities we missed" in the backtest UI.

3. **Trade simulation logic** — for signals where the live pipeline did **not** ACT, the backtest now simulates a `BACKTEST`-tagged `PaperTrade` using `p0` as the historical fill price (`mode: "backtest_top_of_book"` in `execution_context_json`). For ACT signals, a `LIVE_TRADE_EXISTS` audit event is emitted instead of duplicating the trade.

**What this means for you:**
- The trades UI should filter/badge by `trade_source` so BACKTEST trades are visually distinct.
- Settlement (`settle_trades`) will settle BACKTEST trades normally — that's intentional; they need to mark-to-market just like live ones.
- Chad: if you add UI for trades, add a `[BACKTEST]` badge when `trade_source == "BACKTEST"`.

---

### 2026-04-28 — Near-resolution market sweep added to sync

**Problem:** Markets resolving within 24–48h tend to have thin liquidity and rank low in volume-sorted fetches. They're often the highest-velocity trading opportunities but were invisible to both the matcher and the backtester.

**Fix (`app/jobs/sync_markets.py` — same commit as tokenIds fix):** `_fetch_near_resolution_markets()` queries Gamma for up to 50 markets with `end_date` within the next 48 hours, regardless of liquidity rank. These are unioned into the main market list on every sync. Failures in this sweep are swallowed silently (it's additive — the main sync still completes).

---

### 2026-04-28 — Two-stage news→market matching pipeline

**Problem:** The original keyword matcher used a single `min_relevance = 0.75` threshold on raw token overlap. Almost nothing reached the LLM because the RSS body is only ~50–150 words and Polymarket question language is abstract. The system was producing near-zero signals.

**Fix (`app/core/matcher.py`, `app/core/interpret.py`, `app/jobs/process_candidates.py` — commit `4614b0b`):**

Stage 1 — keyword pre-filter (permissive, `MATCHER_KEYWORD_MIN_RELEVANCE = 0.15`):
- Article **title words weighted 3×** body words.
- **Entity alias expansion**: `fed` → `federal reserve`, `btc` → `bitcoin`, `potus` → `president`, etc. (30+ aliases in `ENTITY_ALIASES`).
- **Stop-word filter**: removes `will`, `would`, `said`, `the`, `for`, etc. — boilerplate noise that inflated false matches.
- Hard gate: zero token overlap → skip without any LLM call.

Stage 2 — `batch_relevance_screen()` in `interpret.py`:
- Single `gpt-4o-mini` call per batch of 8 markets: "score each 0–1 for relevance to this article."
- Only markets scoring ≥ `MATCHER_LLM_MIN_RELEVANCE = 0.50` create `NewsSignal` rows and proceed to interpret+verify.
- Degrades gracefully (pass-through) when no API key is set.

**Tuning knobs** (all in `.env`):

| Variable | Default | Notes |
|---|---|---|
| `MATCHER_KEYWORD_MIN_RELEVANCE` | `0.15` | Pre-filter floor — lower = more LLM calls |
| `MATCHER_KEYWORD_MAX_CANDIDATES` | `30` | Cap per article before LLM screen |
| `MATCHER_MARKET_LIMIT` | `100` | Markets loaded per run |
| `MATCHER_LLM_BATCH_SIZE` | `8` | Markets per relevance-screen call |
| `MATCHER_LLM_MIN_RELEVANCE` | `0.50` | Minimum LLM score to promote to interpret+verify |

**Also:** `_openai_interpret` prompt now includes `rules_text`, `resolution_source_text`, and `end_date` where non-null — gives the LLM full resolution context, not just the question.

---

This is a **paper-trading only** research MVP that:

- Syncs active Polymarket markets (public endpoints only)
- Polls **whitelisted** RSS news sources
- Creates candidate market/news matches
- Interprets + verifies signals (high-confidence or abstain)
- Simulates trades with conservative fill assumptions
- Provides a lightweight dashboard

### Guardrails

- **No real trading**: no wallets, no keys, no authenticated endpoints.
- **Whitelisted sources only**: everything else is rejected.
- **Act only on high confidence**: interpreter + verifier gates; otherwise abstain.

---

## Chad — next sprint jobs

> Current owner: **Chad**. Work these in order until paper trading/data collection is visibly healthy.

### Questions to answer whenever paper trades are at zero

1. **Is the app alive?** `/healthz` should be green and System status should show recent `sync_markets`, `poll_news`, and `process_candidates` success.
2. **Do real markets have CLOB token IDs?** `markets.token_ids_json` must contain real token arrays, not JSON `null`; without this, no real price snapshots can be written.
3. **Are price snapshots moving?** `price_snapshots` should grow after market sync. If it stays flat, debug Gamma token fields and CLOB `/book` responses before touching trading thresholds.
4. **Is news fresh enough for the active threshold profile?** The default conservative profile only processes recent articles; old articles will not generate new trades.
5. **Are signals reaching ACT?** If most rows are `ABSTAIN` with `NOT_DIRECT_EVIDENCE`, the blocker is evidence/LLM confidence rather than order execution.
6. **Is the threshold profile too tight for exploration?** Conservative is research-safe but slow. Use balanced/aggressive only for paper experiments.
7. **Did ACT produce an orderbook-backed fill?** An ACT signal can still create no trade when the CLOB book is missing, spread is too wide, or price/liquidity gates fail.
8. **Are trades LIVE or BACKTEST?** Backtest-generated trades are intentionally marked `BACKTEST`; live pipeline trades should remain `LIVE`.

### Current priority list

1. **Price-feed health:** keep validating Gamma `clobTokenIds` parsing and CLOB snapshot counts after each sync.
2. **Trade visibility:** keep BACKTEST badges distinct from LIVE paper trades on `/trades`.
3. **Backtest research UX:** keep `/analysis/backtests` useful for missed-opportunity slicing by run and `signal_action`.
4. **Candidate diagnostics:** add a small UI/export summary for recent candidate counts, ACT count, rejection reasons, and missing-orderbook trade skips.
5. **Tuning pass:** once real snapshots are flowing, run a 1–3 hour paper-only soak on balanced/aggressive and record which gate blocks trades most often.
6. **Lucy handoff:** update this section and `CHAD_SPRINT.md` after every tested chunk; keep `.env`, DB files, logs, and keys out of git.

## Open items for Chad

> Alex: these are ready to pick up — no blockers, no design decisions needed.

### 1. UI — badge BACKTEST trades on the trades page
The `PaperTrade.trade_source` field is now `LIVE` or `BACKTEST`. The trades list in the UI should show a muted `[BACKTEST]` badge next to any row where `trade_source == "BACKTEST"` so they're visually distinct from live paper trades. The `backtest_case_id` FK is also there if you want to link directly to the relevant backtest case.

**Status:** done locally by Chad; `/trades` now shows a muted `BACKTEST` badge in the side column.

### 2. UI — `/analysis/backtests` run selector
The backtests page always shows the **latest** run. The `runs` list is already fetched and passed to the template but not used for navigation. Add a simple dropdown or list on the left so users can click any past run and see its cases. No backend changes needed — just template work.

**Status:** done locally by Chad; recent runs are now clickable and the selected run is highlighted.

### 3. Backtest: add `signal_action` filter to the cases table
The `BacktestCase.signal_action` field (`ACT` / `ABSTAIN` / `CANDIDATE` / `REJECT_*`) is now populated. On the backtests page, add a filter row or column so users can isolate "missed opportunities" (non-ACT cases) vs "trades we made" (ACT cases). This is the core research view.

**Status:** done locally by Chad; the cases table now has an action column plus filter chips per selected run.

### 4. Check Gamma `end_date_min` / `end_date_max` param names
The near-resolution market sweep (`_fetch_near_resolution_markets` in `sync_markets.py`) passes `end_date_min` and `end_date_max` to the Gamma `/markets` endpoint. These are guesses at the param names — confirm they're correct by checking a live response or the Gamma API docs. If they're wrong, the sweep silently returns nothing (it catches errors). Fix the param names if needed.

**Status:** confirmed from Polymarket Gamma docs: `end_date_min` and `end_date_max` are valid `/markets` query parameters.

### 5. CHAD_SPRINT.md — mark completed items
Several items in `CHAD_SPRINT.md` are done but not marked. Do a pass and check off what's shipped.

**Status:** in progress; Chad is updating it alongside this sprint chunk.

---

## Quickstart

1. Create a virtualenv and install deps:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Add `.env` in the project root with your OpenAI key (recommended for interpretation + verification):

```bash
echo 'OPENAI_API_KEY=sk-...' >> .env
```

3. Run (hands-off defaults are in `app/settings.py`; override with env vars if needed):

```bash
make run
```

Or development with reload:

```bash
make run-dev
```

Open `http://127.0.0.1:8000` (or your host’s IP if you used `make run`).

### Hands-off defaults (token-light, “something in a few hours”)

These background loops start automatically with the app unless an interval is set to `0` (as in `tests/conftest.py`):

| Setting | Default | Role |
|--------|---------|------|
| `SNAPSHOT_INTERVAL_SECONDS` | **120** | Full market sync cadence (seconds). |
| `BACKGROUND_POLL_NEWS_INTERVAL_SECONDS` | **600** | RSS poll every **10 minutes**. |
| `BACKGROUND_PROCESS_CANDIDATES_INTERVAL_SECONDS` | **540** | Match + LLM pipeline every **9 minutes**. |
| `BACKGROUND_LAG_PIPELINE_INTERVAL_SECONDS` | **3600** | Lag backfill + signal metrics + lag ranks hourly. |
| `BACKGROUND_SETTLE_INTERVAL_SECONDS` | **3600** | Paper settlement pass hourly. |
| `LLM_MAX_CONCURRENCY` | **2** | Caps parallel OpenAI calls per candidate batch. |

Example overrides:

```bash
export BACKGROUND_PROCESS_CANDIDATES_INTERVAL_SECONDS=900
make run
```

Watch **System status** on `/` and use **Settings → threshold profile** if you want more ACTs (e.g. `balanced` / `aggressive`).

### Realtime paper (overnight / hands-off)

For **faster** news → candidate → paper cycles without hand-tuning every env var, use either:

```bash
make run-realtime
```

or in `.env`:

```bash
REALTIME_PAPER_QUICKSTART=1
```

That **caps** RSS poll (≤120s), candidate processing (≤60s), full Gamma snapshot (≤60s), and tightens adaptive floors when you hold **open** paper near resolution (`app/realtime_policy.py`). It uses more Polymarket + OpenAI quota than the defaults.

### Environment reference

| Variable | Meaning |
|----------|---------|
| `PAPER_TRADE_NOTIONAL_USD` | Target **$** notional per simulated trade (default **10**). |
| `POLYMARKET_ENTRY_FEE_RATE` | Taker-style fee on that notional at open (default **0.003** = 0.3%). |
| `POLYMARKET_WINNING_PROFIT_FEE_RATE` | Fee on **positive** settlement PnL (default **0.02** = 2%). |
| `DATABASE_URL` | Default `sqlite+aiosqlite:///./data.db` (project dir). |
| `OPENAI_API_KEY` | Optional but required for interpret/verify; without it, candidates stall at LLM steps. |
| `REALTIME_PAPER_QUICKSTART` | `1` = faster cadence (see above). |
| `BACKGROUND_*_INTERVAL_SECONDS` | `0` disables that background loop; see `.env.example`. |
| `LLM_MAX_CONCURRENCY` | Parallel candidate workers; set `1` if you see `database is locked`. |
| `SYNC_CLOB_SNAPSHOT_LIMIT` | Max CLOB orderbook probes per full market sync (default **50**) so Sync markets stays bounded. |
| `CLOB_ORDERBOOK_TIMEOUT_SECONDS` | Per-orderbook timeout during snapshot sync (default **5s**). |
| `TRADING_ENABLED` | Must stay `false` for this MVP (paper only). |
| `DASHBOARD_SSE_ENABLED` | Live dashboard counts via `/api/stream/dashboard`. |

Copy `.env.example` → `.env` and edit; never commit `.env` (gitignored).

### SQLite backup / restore

The app uses a single file DB when `DATABASE_URL` points at `.../data.db`. To snapshot while stopped:

```bash
cp data.db "backup-$(date +%Y%m%d%H%M).db"
```

Restore: stop the app, replace `data.db`, start again.

### First hour checklist

1. `make run` or `make run-realtime`.
2. `curl -s http://127.0.0.1:8000/healthz`
3. Open `/` — confirm **System status** rows appear; use top nav **Sync markets** / **Poll news** once if all red on a cold DB.
4. Within one news + candidate cycle, **articles** and **signals** counts should move; **paper trades** only after an `ACT` + gates pass.
5. Optional: `curl -s http://127.0.0.1:8000/api/export/summary` for a JSON paste of counts + job freshness.

### Long soak (4–24h) protocol

- **Watch:** `/` System status (green/yellow/red), disk use for `data.db*`, terminal logs.
- **Expect:** hourly lag pipeline + settlement ticks; first lag rows may stay red until enough signals exist.
- **Restart if:** runaway memory (rare); unrecoverable HTTP failures; or SQLite lock storms after lowering `LLM_MAX_CONCURRENCY`.
- **Known incident (SQLite):** under parallel LLM + single-writer SQLite, you may see `database is locked`. Mitigation: `LLM_MAX_CONCURRENCY=1`, restart, and avoid firing multiple heavy jobs manually at once.

### Reverse proxy + SSE

For `/api/stream/dashboard`, disable response buffering and allow long-lived connections, e.g. **nginx:**

```nginx
location /api/stream/dashboard {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_read_timeout 3600s;
}
```

### Next steps to go “live” on one machine

1. **Use a stable working tree** — `git clone https://github.com/skynet-watcher/Polymarket-News-Reaction.git` (or `git pull` if you already have it) so `main` matches the team remote.
2. **Create `.env`** with at least `OPENAI_API_KEY` (you already have one) and optionally `DATABASE_URL` if you don’t want `./data.db` in the project directory.
3. **Start once with `make run`** — confirm `http://127.0.0.1:8000/healthz` returns `{"ok":"true"}`.
4. **Open `/`** — within **~10–15 minutes** (or **~2–5 minutes** with `REALTIME_PAPER_QUICKSTART=1` / `make run-realtime`) you should see news polling and candidate processing advance in **System status** (green/yellow/red). If everything is red with no data, click **Sync markets** / **Poll news** once from the header or POST the job URLs (see Jobs below).
5. **Leave it running** — over **1–3 hours**, expect new **articles**, **signals**, and occasional **paper trades** if the LLM + gates pass (use a looser threshold profile to see more activity).
6. **Lag / ranks** — first hourly lag pipeline run may still show red until backfill produces rows; that’s normal on a fresh DB.
7. **If SQLite locks** — set `LLM_MAX_CONCURRENCY=1` in the environment and restart.
8. **Exposing beyond localhost** — put TLS + reverse proxy in front; for SSE (`/api/stream/dashboard`), disable buffering on that route (e.g. nginx `proxy_buffering off`).

---

## Jobs

- `POST /api/jobs/sync_markets`
- `POST /api/jobs/poll_news`
- `POST /api/jobs/process_candidates`
- `POST /api/jobs/settle_trades`
- `POST /api/jobs/backtest_news_reactions?since_hours=72&max_articles=50&min_snapshot_coverage=3`
- `POST /api/lag-measurements/backfill` (returns immediately; job runs in the **background** — watch System status)
- `GET /api/export/summary` — JSON snapshot (counts + system status rows) for logs or chat paste

All jobs are designed to be **idempotent**.

### Backtesting news reactions

Use **Analysis → Backtests** or `POST /api/jobs/backtest_news_reactions` to measure how quickly markets moved after article publication using only locally stored `price_snapshots`.

Phase 1 logs:

- news polling delay: `NewsArticle.fetched_at - NewsArticle.published_at`
- signal delay: `NewsSignal.created_at - NewsArticle.published_at`
- hours to resolution: `Market.end_date - NewsArticle.published_at`
- p0 near publication
- fixed post-publication windows: 1m, 5m, 15m, 30m, 1h, 4h, 24h
- first +5pt / +10pt move
- max 24h move
- whether the first +5pt move happened before the article was fetched
- coverage status: `GOOD`, `SPARSE`, or `NO_DATA`

Every run writes queryable DB rows and mirrors structured audit events to:

```text
logs/backtests/backtest_<run_id>.jsonl
```

The JSONL logs are local runtime artifacts and are ignored by Git.

### Upgrade note: source tiers

This MVP does not ship Alembic migrations. Existing `news_sources.source_tier` / `news_articles.source_tier` rows may still contain older tier labels after upgrading.

**Operators should re-save sources in Settings** (or update rows manually) so tiers align with the current scheme: `SOFT`, `HARD`, `RESOLUTION_SOURCE`.
