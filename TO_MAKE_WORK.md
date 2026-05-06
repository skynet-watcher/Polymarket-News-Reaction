# To Make Work — Pre-Trading Readiness Handoff

> Written for an experienced developer picking this up cold.
> Stack: FastAPI + Python, SQLAlchemy async, Postgres (Vercel) / SQLite (local), Jinja2 + TailwindCSS.
> Deployed on Vercel (serverless Python functions, 60-second max execution, no persistent threads).

---

## The Core Problem

The app is built correctly but is **not running continuously**. On Vercel, there are no background threads — everything is driven by cron-triggered HTTP endpoints. The current `vercel.json` only schedules two crons:

- `/api/cron/pipeline` — once daily at 8am (sync → poll → process → settle, all in one call)
- `/api/cron/settle` — once daily at 8pm

That's it. The app effectively does nothing 22+ hours a day. The manual buttons in the nav bar (`Sync markets`, `Poll news`, etc.) are the only thing keeping it alive, which is not a real system.

Everything needed to fix this already exists in code. The cron endpoints are built and secured. The jobs work. This is purely a **configuration and wiring problem**.

---

## What Needs to Happen

### 1. Switch to Per-Minute Crons (Highest Priority)

**Requirement: Vercel Pro plan** ($20/month). Hobby plan only supports daily crons. Without Pro, use [cron-job.org](https://cron-job.org) as a free external cron service — the CRON_SECRET auth already supports it.

Update `vercel.json` to replace the current 2 crons with these 4:

```json
"crons": [
  { "path": "/api/cron/sync",     "schedule": "* * * * *" },
  { "path": "/api/cron/poll",     "schedule": "* * * * *" },
  { "path": "/api/cron/settle",   "schedule": "*/5 * * * *" },
  { "path": "/api/cron/pipeline", "schedule": "0 4 * * *" }
]
```

**What each does** (all endpoints exist in `app/routers/crons.py`):

| Endpoint | Jobs | Why this cadence |
|---|---|---|
| `/api/cron/sync` | `sync_markets` | Captures price snapshots and market resolution (`winning_outcome`) as quickly as possible |
| `/api/cron/poll` | `poll_news` → `process_candidates` | Gets news within 1 minute of publication; processes candidates while they're still within the 30-minute article age window |
| `/api/cron/settle` | `settle_trades` | Settles OPEN trades at T+24h and on market resolution; every 5 min is fast enough |
| `/api/cron/pipeline` | All four jobs | Full daily sweep as a safety net to catch anything missed |

**Why sync and poll must both run every minute:** `process_candidates` only acts on articles published within the last 30 minutes (`max_article_age_minutes = 30` in settings). If the cron runs less frequently than that window, articles expire before they're processed. Running every minute ensures the pipeline catches articles while they're still fresh.

**Vercel 60-second timeout:** Each job is already throttled for Vercel in code (`_on_vercel()` checks). `process_candidates` processes 3 articles, 30 markets, 2 LLM calls max per invocation — it fits in ~20-30 seconds. `sync_markets` fetches 25 events + 50 markets + up to 10 CLOB snapshots — also fits. These limits are fine because the minute cadence compensates for per-run volume.

---

### 2. Remove (or Collapse) the Manual Buttons from the Nav

**File:** `app/templates/base.html`, lines 18–46.

The seven nav buttons (`Sync markets`, `Poll news`, `Process candidates`, `Backfill lags`, `Signal metrics`, `Lag ranks`, `Settle trades`) need to go. They are developer tools that make the UI look like a science project and create the false impression that a human needs to operate the system.

**Options (choose one):**
- Remove them entirely from `base.html` and add a hidden `GET /admin` page that renders them (protected by `verify_bearer_secret` or a simple env-var check)
- Keep them but move them to the `/health` or `/settings` page only, not the global nav
- Gate them behind a `?debug=1` query param toggle

The dashboard's "System status" panel already shows job health with last-run times — that's the right UX for an automated system. The buttons belong in a dev/ops panel, not the main nav.

---

### 3. Wire Settlement to Market Resolution

Settlement already works correctly for the **resolved path** (`SETTLED_RESOLVED`): when `sync_markets` runs and the Gamma API returns a `winner` field, it sets `market.winning_outcome`, and the next `settle_trades` run picks it up and settles at 0/1.

The **gap** is purely frequency: with daily crons, a market that resolves at 9am won't have its trades settled until 8pm. With per-minute sync and per-5-minute settlement, resolution-to-settlement latency drops to under 10 minutes.

**No code changes needed here** — just the cron frequency fix above.

For the T+24h path (`SETTLED_T24H`): this requires a price snapshot to exist at the T+24h mark. With per-minute syncs, price snapshots accumulate continuously and this path will work reliably. Today, with daily syncs, T+24h trades stay OPEN indefinitely because no snapshot exists at the target time.

---

### 4. Data Bootstrapping Strategy

The lag/signal analytics (`signal_metrics`, `lag_ranks`) are computed over historical data. They will return empty or meaningless results until there's a corpus of settled trades and price history. Here is the realistic timeline:

**Day 0 (First Deploy)**
1. Verify Postgres is linked and `OPENAI_API_KEY` is set in Vercel env vars
2. Deploy with the updated `vercel.json`
3. Confirm the first cron runs by checking the Vercel Functions log
4. Manually trigger `/api/cron/sync` and `/api/cron/poll` once via curl to seed initial data (don't wait for the cron):
   ```
   curl -H "Authorization: Bearer <CRON_SECRET>" https://your-app.vercel.app/api/cron/sync
   curl -H "Authorization: Bearer <CRON_SECRET>" https://your-app.vercel.app/api/cron/poll
   ```
5. Confirm markets are in DB and articles are appearing in `/news`

**Day 1–3 (Accumulation)**
- Crons run every minute; price snapshots and articles accumulate
- Signals are generated and paper trades opened
- Check `/signals` for ACT signals — if all ABSTAIN/REJECT, see §5 below

**Day 3–7 (First Settlement)**
- T+24h trades begin settling automatically
- Any market that resolved will show `SETTLED_RESOLVED` trades
- Run lag backfill once manually after ~3 days: `POST /api/lag-measurements/backfill`
- Signal metrics and lag ranks begin populating after backfill

**Day 7+ (Meaningful Analytics)**
- Lag ranks become useful once multiple settled trades per market exist
- At this point, enable `lag_focus_top_n` in settings to focus candidate matching on top-ranked laggy markets

---

### 5. Article Age Window vs. Cron Timing

`max_article_age_minutes = 30` (in `app/settings.py`) means `process_candidates` ignores articles older than 30 minutes. This is correct trading logic (stale news shouldn't drive entries), but it assumes the cron is reliably firing every minute.

**Risk:** if Vercel has a cold-start delay or a cron misfire, articles can age out before being processed. Two mitigations:

**Option A (simple):** Bump `MAX_ARTICLE_AGE_MINUTES=45` in Vercel env vars to give a small buffer. This is a one-line env var change, no code required.

**Option B (better):** In `process_candidates.py` line 128, make the cutoff based on `fetched_at` rather than `published_at` for articles where `published_at` may be stale (some RSS feeds report old dates). This is a small code change but improves reliability.

---

### 6. Signal Metrics and Lag Ranks — No Action Required Yet

`signal_metrics` and `lag_ranks` run in the lag pipeline loop (locally: hourly; on Vercel: not currently scheduled). They're analytics jobs, not signal-generation jobs. They don't need to run every minute.

Add a daily cron for the lag pipeline once there's enough data to make it meaningful (after ~Day 7):

```json
{ "path": "/api/cron/lag-pipeline", "schedule": "0 6 * * *" }
```

This endpoint doesn't exist yet — needs to be added to `app/routers/crons.py`. It should call `compute_lag.run_backfill`, `signal_metrics.run_backfill`, and `lag_rank.run` in sequence (the same pattern as `/api/cron/pipeline`). The underlying job functions already exist in `app/jobs/`.

---

## Summary Checklist

These are the tasks in priority order. Everything below is required before the app can trade autonomously.

### P0 — Must ship before anything else

- [ ] **Upgrade Vercel to Pro** (or configure cron-job.org as external cron service). Without this, per-minute crons cannot run. Nothing else matters until this is resolved.

- [ ] **Update `vercel.json`** to add per-minute crons for `/api/cron/sync` and `/api/cron/poll`, and a per-5-minute cron for `/api/cron/settle`. Remove or demote the daily `/api/cron/pipeline` to a safety net at off-peak hours. (See §1 above for the exact JSON.)

- [ ] **Verify news sources are seeded and active.** The `live_feeds.py` auto-seeder runs on startup (`auto_seed_news_feeds = True`), but confirm via `/settings` that sources are present and `active=1`. If all sources are inactive, `poll_news` inserts zero articles and the whole pipeline is inert. Must include at least: AP, Reuters, BBC, Guardian. (AP/Reuters in particular need `source_tier='HARD'`.)

### P1 — Required for reliable operation

- [ ] **Add `/api/cron/lag-pipeline` endpoint** in `app/routers/crons.py`. Follow the pattern in the existing `/api/cron/poll` handler. Add to `vercel.json` on a daily schedule. This is ~15 lines of code.

- [ ] **Remove manual job buttons from the global nav** in `app/templates/base.html`. Move them to a developer-only panel (e.g., `/settings` page or a hidden `/admin` route). The nav should show status, not controls.

- [ ] **Confirm `OPENAI_API_KEY` is set** in Vercel environment variables. Without it, `process_candidates` skips all LLM calls and generates zero signals. The System Health page at `/health` should surface this clearly.

### P2 — Quality of life once P0/P1 are done

- [ ] **Set `MAX_ARTICLE_AGE_MINUTES=45`** in Vercel env vars as a buffer against cold-start delays (see §5).

- [ ] **Cold-start manual run:** after first deploy with new crons, trigger sync and poll once manually via curl with `CRON_SECRET` to avoid waiting for the first automatic fire.

- [ ] **Verify first settle:** after 24+ hours of operation, check `/trades` for any `SETTLED_T24H` or `SETTLED_RESOLVED` entries. If all trades remain `OPEN`, check that price snapshots exist at the T+24h target (query `price_snapshots` table). If no snapshots exist, the sync cron is not firing.

- [ ] **After ~7 days:** run lag backfill manually, then enable `lag_focus_top_n` in settings to improve candidate quality.

---

## Key Files for Reference

| File | What it does |
|---|---|
| `vercel.json` | Cron schedule and Vercel config — **main thing to change** |
| `app/routers/crons.py` | The cron HTTP endpoints called by Vercel |
| `app/jobs/sync_markets.py` | Fetches markets + price snapshots from Polymarket Gamma/CLOB |
| `app/jobs/poll_news.py` | Fetches articles from RSS news sources |
| `app/jobs/process_candidates.py` | Matches articles to markets, runs LLM, creates signals and paper trades |
| `app/jobs/settle_trades.py` | Settles OPEN trades at resolution or T+24h |
| `app/jobs/compute_lag.py` | Computes lag measurements (analytics, not trading) |
| `app/jobs/signal_metrics.py` | Computes per-signal metrics (analytics) |
| `app/jobs/lag_rank.py` | Ranks markets by lag score (analytics) |
| `app/settings.py` | All tunable parameters — override via Vercel env vars |
| `app/templates/base.html` | Global nav with the manual buttons to remove/relocate |
| `.env.vercel.example` | Required and optional env vars for Vercel deployment |

---

## What "Functionally Ready to Trade" Looks Like

The system is ready when, without any human intervention:

1. `/news` shows articles published in the last 60 minutes
2. `/signals` shows new ACT signals every few hours
3. `/trades` shows new OPEN paper trades
4. OPEN trades transition to `SETTLED_T24H` or `SETTLED_RESOLVED` automatically
5. The System status panel on the dashboard shows all jobs green with last-run times in the last 2 minutes

---

_Eric Jacobsen — 2026-05-06_
