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

### Next steps to go “live” on one machine

1. **Use a stable working tree** — `git clone https://github.com/skynet-watcher/Polymarket-News-Reaction.git` (or `git pull` if you already have it) so `main` matches the team remote.
2. **Create `.env`** with at least `OPENAI_API_KEY` (you already have one) and optionally `DATABASE_URL` if you don’t want `./data.db` in the project directory.
3. **Start once with `make run`** — confirm `http://127.0.0.1:8000/healthz` returns `{"ok":"true"}`.
4. **Open `/`** — within **~10–15 minutes** you should see news polling and candidate processing advance in **System status** (green/yellow/red). If everything is red with no data, click **Sync markets** / **Poll news** once from Settings or POST the job URLs (see Jobs below).
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
- `POST /api/lag-measurements/backfill`

All jobs are designed to be **idempotent**.

### Upgrade note: source tiers

This MVP does not ship Alembic migrations. Existing `news_sources.source_tier` / `news_articles.source_tier` rows may still contain older tier labels after upgrading.

**Operators should re-save sources in Settings** (or update rows manually) so tiers align with the current scheme: `SOFT`, `HARD`, `RESOLUTION_SOURCE`.

