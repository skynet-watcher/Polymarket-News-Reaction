## Polymarket News-Reaction (Paper Trading MVP)

Sprint tasks for Chad: see [`CHAD_SPRINT.md`](CHAD_SPRINT.md). Status UI / Lucy notes: [`LUCY_STATUS_UI_HANDOFF.md`](LUCY_STATUS_UI_HANDOFF.md).

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

2. Add `.env` in the project root. `OPENAI_API_KEY` is optional for app launch, but recommended for interpretation + verification:

```bash
cp .env.example .env  # if present, then edit values
```

Minimal example:

```dotenv
OPENAI_API_KEY=
DATABASE_URL=sqlite+aiosqlite:///./data.db
SNAPSHOT_INTERVAL_SECONDS=120
BACKGROUND_POLL_NEWS_INTERVAL_SECONDS=600
BACKGROUND_PROCESS_CANDIDATES_INTERVAL_SECONDS=540
BACKGROUND_LAG_PIPELINE_INTERVAL_SECONDS=3600
BACKGROUND_SETTLE_INTERVAL_SECONDS=3600
LLM_MAX_CONCURRENCY=2
TRADING_ENABLED=false
DASHBOARD_SSE_ENABLED=true
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

### Data files and backups

By default, SQLite data lives in the project root:

- `data.db`
- `data.db-shm`
- `data.db-wal`

For a quick backup while the app is stopped:

```bash
cp data.db data.db.backup
```

If the app is running with WAL enabled, stop it before copying or copy all three SQLite files together. To restore, stop the app, replace the SQLite files, then restart with `make run`.

Use `DATABASE_URL=sqlite+aiosqlite:////absolute/path/to/data.db` if you want the database outside the checkout.

### First hour checklist

1. **Use a stable working tree** — fix or reclone git if `.git` is incomplete so you can track changes.
2. **Create `.env`** with at least `OPENAI_API_KEY` (you already have one) and optionally `DATABASE_URL` if you don’t want `./data.db` in the project directory.
3. **Start once with `make run`** — confirm `http://127.0.0.1:8000/healthz` returns `{"ok":"true"}`.
4. **Open `/`** — within **~10–15 minutes** you should see news polling and candidate processing advance in **System status** (green/yellow/red). If everything is red with no data, click **Sync markets** / **Poll news** once from Settings or POST the job URLs (see Jobs below).
5. **Leave it running** — over **1–3 hours**, expect new **articles**, **signals**, and occasional **paper trades** if the LLM + gates pass (use a looser threshold profile to see more activity).
6. **Lag / ranks** — first hourly lag pipeline run may still show red until backfill produces rows; that’s normal on a fresh DB.
7. **If SQLite locks** — set `LLM_MAX_CONCURRENCY=1` in the environment and restart.
8. **Exposing beyond localhost** — put TLS + reverse proxy in front; for SSE (`/api/stream/dashboard`), disable buffering on that route (e.g. nginx `proxy_buffering off`).

### Soak protocol

Use this before trusting the MVP for unattended paper data collection.

1. Start from a valid git checkout and run `make run`.
2. Open `/` through `http://127.0.0.1:8000/`, not by opening template files directly.
3. Let it run for 4 hours first, then 24 hours once the short run is clean.
4. Watch **System status**:
   - Market sync should stay green or briefly yellow during sync.
   - News polling should advance within the configured interval.
   - Candidate processing may be yellow while OpenAI calls are running.
   - Lag backfill can take longer; check its last duration before assuming it is stuck.
   - Settlement should show a recent successful run once paper trades exist.
5. Record any red row’s `last_error` text in `LUCY_STATUS_UI_HANDOFF.md`.
6. If SQLite reports `database is locked`, restart with `LLM_MAX_CONCURRENCY=1`.
7. Check disk growth for `data.db` and logs after the run.
8. Keep `TRADING_ENABLED=false`; this sprint is paper-only.

---

## Jobs

- `POST /api/jobs/sync_markets`
- `POST /api/jobs/poll_news`
- `POST /api/jobs/process_candidates`
- `POST /api/jobs/settle_trades`
- `POST /api/lag-measurements/backfill`

All jobs are designed to be **idempotent**.

### Upgrade note: source tiers

This MVP does not ship Alembic migrations. Existing `news_sources.source_tier` / `news_articles.source_tier` rows may still contain older tier labels after upgrading.

**Operators should re-save sources in Settings** (or update rows manually) so tiers align with the current scheme: `SOFT`, `HARD`, `RESOLUTION_SOURCE`.
