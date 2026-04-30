## Polymarket News-Reaction (Paper Trading MVP)

Sprint tasks for Chad: see [`CHAD_SPRINT.md`](CHAD_SPRINT.md) and [`ALEX_REVIEW.md`](ALEX_REVIEW.md). Status UI / Lucy notes: [`LUCY_STATUS_UI_HANDOFF.md`](LUCY_STATUS_UI_HANDOFF.md). Source repo: [github.com/skynet-watcher/Polymarket-News-Reaction](https://github.com/skynet-watcher/Polymarket-News-Reaction).

---

## What this app does

Polymarket News-Reaction is a **paper-trading research tool** that monitors Polymarket prediction markets and trades against them using news signals — no real money, no wallets, no authenticated Polymarket endpoints.

The pipeline:

1. **Sync markets** — pulls active Polymarket markets from the Gamma API and writes price snapshots from the CLOB orderbook
2. **Poll news** — fetches RSS feeds from whitelisted sources
3. **Process candidates** — two-stage matching: keyword pre-filter, then GPT-4o-mini relevance screen + interpret + verify
4. **Paper trade** — places a simulated $10 trade when confidence gates pass
5. **Settle** — marks trades as WIN/LOSS once the market resolves or after 24 hours (mark-to-market)
6. **Analyse** — lag analysis, signal accuracy, backtest replay, and the Crypto Preflight Scanner

Everything runs locally against SQLite, or remotely on Vercel + Postgres.

---

## Engineering Log

> Maintained by the team. Most recent entry first.
> Read this before starting new work — it covers bugs fixed, architectural decisions, and things that look wrong but aren't.

---

### 2026-04-30 — Two bugs fixed in Chad's hardening pass

**Context:** Chad's hardening sprint (`812d4dd`) introduced two regressions that were caught in review and fixed in `d9cdbee`.

**Bug 1 (CRITICAL — tests broken):** `urlunsplit` corrupts SQLite URLs. Python's `urlsplit` on `sqlite+aiosqlite:///./data.db` drops one of the three leading slashes on round-trip, producing `sqlite+aiosqlite:/./data.db` — a string SQLAlchemy can't parse. All 61 tests were failing. Fix: skip the `urlsplit`/`urlunsplit` path for SQLite URLs entirely; they never contain `sslmode` so there's nothing to strip.

**Bug 2 (CRITICAL — all UI buttons 401 on Vercel):** `verify_bearer_secret` was added as a dependency on every `POST /api/jobs/*` endpoint. When `CRON_SECRET` is set on Vercel, the browser's `fetch()` calls (no `Authorization` header) all receive 401 Unauthorized, making every manual job button non-functional. Fix: `verify_bearer_secret` stays only on `GET /api/cron/*` endpoints (called by external cron services) and the settings mutation `POST` routes in `ui.py`. The job `POST` endpoints used by the browser UI are open.

---

### 2026-04-30 — Chad Vercel hardening pass: DB startup, auth, settlement, RSS, crypto preflight

**Context:** Full pre-deploy review against commit `e10dd57` found several serverless/Postgres edges that could break on Vercel or under concurrent cron/manual runs.

**Vercel/Postgres startup fixes:**
- `app/db.py` now parses `DATABASE_URL` with `urllib.parse` instead of regex so URLs like `...?sslmode=require&connect_timeout=10` keep all non-SSL query params intact.
- `app/init_db.py` takes a Postgres advisory transaction lock before `Base.metadata.create_all()` so multiple cold starts do not race table/index creation on a fresh DB.
- `PriceSnapshot` now has a composite index on `(market_id, timestamp)`, matching the common "latest/nearest snapshot for market" queries used by settlement, lag, metrics, and UI.

**Operator security model:**
- New shared helper: `app/security.py`.
- `GET /api/cron/*` endpoints and settings mutation routes (`POST /settings/*`) use `Authorization: Bearer <CRON_SECRET>` when `CRON_SECRET` is configured.
- Local dev remains open if `CRON_SECRET` is unset.
- RSS source URLs added through Settings must be absolute public HTTPS URLs; localhost/private/link-local IP targets are rejected (SSRF guard).

**Reliability fixes:**
- `settle_trades` now batches eligible trades (`LIMIT 500`) and can settle from the first snapshot shortly after T+24h when the exact pre-cutoff snapshot is missing. Prevents valid paper trades from staying `OPEN` forever after sparse syncs.
- `cron_pipeline` and `cron_poll` explicitly roll back after a failed step before continuing with the shared request session.
- `backtest_news_reactions` no longer writes JSONL files on Vercel; DB `BacktestEventLog` rows remain the canonical audit trail. Local runs still mirror to `logs/backtests/*.jsonl`.

**Crypto preflight fixes:**
- `_upsert_profile` uses dialect-native `ON CONFLICT DO UPDATE` for SQLite/Postgres instead of select-then-insert.
- Candle parsing now lowers confidence when `endDate` is not aligned to the inferred Binance interval boundary, because some markets use trading cutoffs or delayed resolution timestamps rather than exact candle close times.

**RSS hardening:**
- RSS XML parsing uses an `lxml` parser with entity resolution disabled and network access disabled (XXE prevention).
- Feed polling validates stored source URLs before fetching (SSRF prevention).

---

### 2026-04-30 — Vercel deployment added

**What changed:**
- `api/index.py` — Vercel Python runtime entry point
- `vercel.json` — routes all traffic to FastAPI; 2 daily Hobby-plan crons (8 AM pipeline, 8 PM settlement)
- `app/db.py` — normalises `postgres://` → `postgresql+asyncpg://`; strips `sslmode` from URL and passes `ssl="require"` via `connect_args`; `pool_pre_ping=True`; `pool_size=1` for serverless
- `app/init_db.py` — skips SQLite PRAGMA migrations when dialect is Postgres; adds advisory lock for concurrent cold starts
- `app/main.py` — detects `VERCEL=1`, skips all background asyncio loops; registers `/api/cron/*` router
- `app/routers/crons.py` — GET endpoints for Vercel cron and cron-job.org: `/cron/pipeline`, `/cron/settle`, `/cron/sync`, `/cron/poll`
- `app/routers/api.py` — SSE dashboard stream returns 503 immediately on Vercel
- `requirements.txt` — added `asyncpg==0.30.0`
- `.python-version` — pins Python 3.11 for Vercel runtime
- `app/static/.gitkeep` — ensures static directory exists in repo
- `.env.vercel.example` — env var reference for Vercel dashboard setup

**Deploy steps:**
1. [vercel.com](https://vercel.com) → New Project → import `skynet-watcher/Polymarket-News-Reaction`
2. Storage → Create Database → Postgres → Connect to Project (auto-adds `DATABASE_URL`)
3. Settings → Environment Variables: add `OPENAI_API_KEY`, `CRON_SECRET` (any random string), `DASHBOARD_SSE_ENABLED=false`
4. Redeploy

**Hobby plan limits:** function timeout is 10 seconds; cron jobs run at most once per day. Manual buttons still work for on-demand runs. For more frequent automated runs, use [cron-job.org](https://cron-job.org) (free) pointing at `/api/cron/poll` with header `Authorization: Bearer <CRON_SECRET>`.

---

### 2026-04-30 — Crypto Market Preflight Scanner

**What it does:** Scans active Polymarket crypto markets, classifies each by rule family (Up/Down candle, daily comparison, price threshold, ATH), parses candle parameters for Up/Down markets, verifies the opening price against Binance klines, and checks YES/NO CLOB orderbook liquidity. Results stored in `crypto_market_profiles` table and displayed at `/analysis/crypto-preflight`.

**How to use:** Navigate to **⚡ Crypto Preflight** in the nav bar and click "Run preflight scan now".

**Status meanings:**
- ✅ **Ready** — parser confident, Binance confirms open price, both books liquid. Tradeable.
- ⏳ **Future candle** — candle hasn't opened yet; will become Ready once it does.
- 📭 **No orderbook** — parsed correctly but YES or NO book below $500 liquidity threshold.
- 🔍 **Needs review** — parser confidence < 75%; couldn't confidently extract asset or interval.
- ⛔ **Unsupported** — not an Up/Down candle market (price threshold, ATH, etc.).

**Key files:** `app/jobs/crypto_preflight.py`, `app/models.py` (`CryptoMarketProfile`), `app/templates/crypto_preflight.html`, `POST /api/jobs/crypto_preflight`.

---

### 2026-04-29 — Smoke test improvements

**BTC signal test** (`POST /api/jobs/btc_signal_test`): fetches live BTCUSDT from Binance, compares to a stored reference price in `RuntimeSetting`, fires a paper trade if the move exceeds `move_threshold_pct`. Set `force=true` to always fire. Bypasses news + LLM — use to verify the trade pipeline end-to-end.

**Bulk smoke test** (`POST /api/jobs/bulk_smoke_test?count=20`): places up to 50 paper trades across different markets in one shot. Selects markets with the soonest future `end_date` first so settlements appear quickly. Falls back to most-liquid markets when fewer than `count` future-dated markets exist. Both buttons are on **System Health → Smoke Tests**.

---

### 2026-04-28 — Alex review sprint: clean data, Research profile, LLM guard

**Data baseline:** Current local `data.db` has only `smoke_mkt` price snapshots before the CLOB token fix. Treat pre-fix lag/backtest analytics as contaminated fixture data.

**Research profile:** `research` is now seeded as the default profile for new installs: indirect evidence allowed, `min_confidence = 0.55`, `min_verifier_confidence = 0.55`, `max_article_age_minutes = 240`, half-size paper trades. Existing DBs keep their setting until changed in Settings.

**Cost guard:** `process_candidates` now has `MAX_LLM_CALLS_PER_RUN` (default `50`) and returns funnel/cost fields. Dashboard shows cumulative estimated LLM screen cost.

**UI diagnostics:** `/signals` now shows `rejection_reason` — whether the system is blocked by evidence type, confidence, age, spread, liquidity, or orderbook gaps.

---

### 2026-04-28 — Critical: real markets never got price snapshots (`token_ids_json` was always null)

**Symptom:** Dashboard showed "last price sync" as hours ago even after a successful `sync_markets` run. Only the `smoke_mkt` fixture market ever had `PriceSnapshot` rows.

**Root cause:** The Gamma `/events` endpoint does not return `tokenIds`. Without a token ID the CLOB bid/ask fetch is skipped and no `PriceSnapshot` is written.

**Fix:** `_fetch_all_markets_unified` now runs both the `/events` path (volume-sorted, richer metadata) and the `/markets` path (provides `tokenIds`). Results are merged. First sync after this fix will populate `token_ids_json` for all markets — trigger **Sync markets** once manually.

---

### 2026-04-28 — Two-stage news→market matching pipeline

Stage 1 — keyword pre-filter (`MATCHER_KEYWORD_MIN_RELEVANCE = 0.15`): article title words weighted 3×, entity alias expansion, stop-word filter. Zero overlap → skip, no LLM call.

Stage 2 — `batch_relevance_screen()`: single `gpt-4o-mini` call per batch of 8 markets. Only markets scoring ≥ `MATCHER_LLM_MIN_RELEVANCE = 0.50` proceed to interpret+verify.

| Variable | Default | Notes |
|---|---|---|
| `MATCHER_KEYWORD_MIN_RELEVANCE` | `0.15` | Pre-filter floor |
| `MATCHER_KEYWORD_MAX_CANDIDATES` | `30` | Cap per article before LLM screen |
| `MATCHER_MARKET_LIMIT` | `100` | Markets loaded per run |
| `MATCHER_LLM_BATCH_SIZE` | `8` | Markets per relevance-screen call |
| `MATCHER_LLM_MIN_RELEVANCE` | `0.50` | Minimum LLM score to promote |

---

## Quickstart (local — one command)

```bash
bash start.sh
```

That's it. `start.sh` creates a `.venv`, installs all deps, creates `app/static/`, copies `.env.example → .env` if missing, and starts the server. Open **http://localhost:8000**.

To keep your Mac awake overnight:

```bash
caffeinate -i bash start.sh
```

### Manual setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then add OPENAI_API_KEY
make run
```

### Hands-off defaults

Background loops start automatically unless an interval is set to `0`:

| Setting | Default | Role |
|--------|---------|------|
| `SNAPSHOT_INTERVAL_SECONDS` | **120** | Full market sync cadence |
| `BACKGROUND_POLL_NEWS_INTERVAL_SECONDS` | **600** | RSS poll every 10 minutes |
| `BACKGROUND_PROCESS_CANDIDATES_INTERVAL_SECONDS` | **540** | Match + LLM pipeline every 9 minutes |
| `BACKGROUND_LAG_PIPELINE_INTERVAL_SECONDS` | **3600** | Lag backfill + signal metrics hourly |
| `BACKGROUND_SETTLE_INTERVAL_SECONDS` | **3600** | Paper settlement pass hourly |
| `LLM_MAX_CONCURRENCY` | **2** | Parallel OpenAI calls per batch |

### Realtime paper (overnight)

```bash
make run-realtime
# or: REALTIME_PAPER_QUICKSTART=1 in .env
```

Caps RSS poll (≤120s), candidate processing (≤60s), snapshot interval (≤60s), and tightens adaptive floors near resolution. Uses more API quota.

### Environment reference

| Variable | Meaning |
|----------|---------|
| `DATABASE_URL` | Default `sqlite+aiosqlite:///./data.db`. Set to Vercel Postgres URL in production. |
| `OPENAI_API_KEY` | Required for interpret/verify; without it candidates stall at LLM steps. |
| `CRON_SECRET` | Bearer token protecting `/api/cron/*` and settings mutation endpoints. Required on Vercel; optional locally. |
| `DASHBOARD_SSE_ENABLED` | Set `false` on Vercel (serverless can't hold open connections). |
| `PAPER_TRADE_NOTIONAL_USD` | Target notional per simulated trade (default **$10**). |
| `POLYMARKET_ENTRY_FEE_RATE` | Taker-style fee at open (default **0.003** = 0.3%). |
| `POLYMARKET_WINNING_PROFIT_FEE_RATE` | Fee on positive settlement P&L (default **0.02** = 2%). |
| `REALTIME_PAPER_QUICKSTART` | `1` = faster cadence. |
| `LLM_MAX_CONCURRENCY` | Set `1` if you see `database is locked`. |
| `MAX_LLM_CALLS_PER_RUN` | Hard cap on GPT relevance-screen calls per run (default **50**). |
| `TRADING_ENABLED` | Must stay `false` — paper only. |

Copy `.env.example` → `.env` and edit. Never commit `.env` (gitignored).

---

## Pages

| URL | What you see |
|-----|-------------|
| `/` | Dashboard — counts, system status, recent signals |
| `/health` | System Health — traffic-light gates, smoke test buttons |
| `/markets` | Active Polymarket markets in the DB |
| `/news` | Fetched articles |
| `/signals` | Generated signals with rejection reasons |
| `/trades` | Paper trades with live P&L |
| `/analysis` | Lag analysis overview |
| `/analysis/backtests` | News reaction backtest runs |
| `/analysis/lags` | Per-signal lag measurements |
| `/analysis/laggy-markets` | Markets ranked by lag score |
| `/analysis/soft-accuracy` | Signal accuracy by source tier |
| `/analysis/crypto-preflight` | ⚡ Crypto Market Preflight Scanner |
| `/settings` | Threshold profiles, RSS sources, resolution mappings |

---

## Jobs

All jobs are idempotent and callable via the buttons in the UI or directly:

| Endpoint | What it does |
|----------|-------------|
| `POST /api/jobs/sync_markets` | Sync markets + price snapshots from Gamma/CLOB |
| `POST /api/jobs/poll_news` | Fetch RSS feeds |
| `POST /api/jobs/process_candidates` | Match articles to markets, LLM interpret+verify |
| `POST /api/jobs/settle_trades` | Settle open paper trades |
| `POST /api/jobs/backtest_news_reactions` | Replay news signals against stored snapshots |
| `POST /api/jobs/bulk_smoke_test?count=20` | Place N test paper trades across N markets |
| `POST /api/jobs/btc_signal_test?force=true` | Single BTC price-move paper trade (pipeline test) |
| `POST /api/jobs/crypto_preflight` | Run Crypto Market Preflight Scanner |
| `POST /api/lag-measurements/backfill` | Backfill lag measurements (runs in background) |
| `GET /api/export/summary` | JSON snapshot of counts + system status |

Browser buttons send no `Authorization` header and work without any secret. The cron endpoints (`GET /api/cron/*`) require `Authorization: Bearer $CRON_SECRET` when `CRON_SECRET` is set.

### Cron endpoints (Vercel / cron-job.org)

| Endpoint | What it does |
|----------|-------------|
| `GET /api/cron/pipeline` | sync → poll → process → settle (full daily run) |
| `GET /api/cron/settle` | Settlement pass only |
| `GET /api/cron/sync` | Market sync only (safe to call every few minutes) |
| `GET /api/cron/poll` | Poll news + process candidates |

---

## Vercel deployment

See [`.env.vercel.example`](.env.vercel.example) for the full environment variable reference.

**Deploy steps:**
1. [vercel.com](https://vercel.com) → New Project → import this repo
2. Storage → Create Database → Postgres → Connect to Project
3. Settings → Environment Variables: add `OPENAI_API_KEY`, `CRON_SECRET`, `DASHBOARD_SSE_ENABLED=false`
4. Redeploy

**Hobby plan cron schedule** (2 jobs/day max):
- 8:00 AM UTC — full pipeline (`/api/cron/pipeline`)
- 8:00 PM UTC — settlement pass (`/api/cron/settle`)

**For more frequent runs:** use [cron-job.org](https://cron-job.org) (free) with a custom `Authorization: Bearer <CRON_SECRET>` header pointing at `/api/cron/poll` every 15–30 minutes.

**10-second timeout:** Vercel Hobby functions time out at 10s. Short jobs (sync, settle, poll) fit. `process_candidates` with LLM calls may timeout under heavy load — use the Hobby plan for read-heavy dashboarding and trigger heavy jobs manually or via an external cron service.

---

## Backtesting

`POST /api/jobs/backtest_news_reactions?since_hours=72&max_articles=50&min_snapshot_coverage=3`

Replays stored news signals against local price snapshots. Measures:
- News polling delay, signal delay, hours to resolution
- `p0` near publication, price moves at 1m/5m/15m/30m/1h/4h/24h
- First +5pt / +10pt threshold crossing
- Whether the signal fired before the market moved

Results in the `BacktestRun` / `BacktestCase` tables and at `/analysis/backtests`. Local runs also write `logs/backtests/backtest_<run_id>.jsonl` (gitignored). JSONL mirroring is disabled on Vercel.

---

## SQLite backup / restore

```bash
cp data.db "backup-$(date +%Y%m%d%H%M).db"
```

Restore: stop the app, replace `data.db`, restart.

---

## First-hour checklist

1. `bash start.sh` (or `make run`)
2. `curl -s http://localhost:8000/healthz` → `{"ok":"true"}`
3. Open `/health` — System Health gates should all go green within one cycle
4. Within 10–15 minutes: articles and signals counts should move in System status
5. Paper trades appear after the first `ACT` signal clears all gates
6. Use **System Health → Smoke Tests** to place test trades instantly if you want to verify the pipeline right now

---

## Known issues / gotchas

- **SQLite locks under parallel LLM:** set `LLM_MAX_CONCURRENCY=1` and restart
- **Old `data.db` with null `token_ids_json`:** trigger **Sync markets** once — the merge pass will backfill token IDs and price snapshots will start flowing
- **Port 8000 already in use:** `pkill -9 -f uvicorn` then restart
- **Vercel 10s timeout:** heavy jobs (process_candidates with many LLM calls, crypto preflight scan) may timeout on Hobby — run them manually from the browser or upgrade to Pro for 60s functions
