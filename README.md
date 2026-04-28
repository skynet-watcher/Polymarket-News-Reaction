## Polymarket News-Reaction (Paper Trading MVP)

Sprint tasks for Chad: see [`CHAD_SPRINT.md`](CHAD_SPRINT.md) (includes a **solo overnight** checklist). Status UI / Lucy notes: [`LUCY_STATUS_UI_HANDOFF.md`](LUCY_STATUS_UI_HANDOFF.md). Source repo: [github.com/skynet-watcher/Polymarket-News-Reaction](https://github.com/skynet-watcher/Polymarket-News-Reaction).

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
