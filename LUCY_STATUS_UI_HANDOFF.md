# Status UI Handoff for Lucy

## What changed

Added a dashboard status panel so the MVP can show which ingestion and research processes have live data, which are running, and which are stale, failed, or empty.

The dashboard now shows:

- Green dot for live/recent data.
- Yellow dot for a job currently running.
- Red dot for stale data, missing data, or failed jobs.
- Time since last update in minutes and seconds.
- A short detail count for each process.
- Last error text when a tracked job fails.

## Files to review

- `app/models.py`
  - Added the `JobStatus` model and `job_statuses` table.

- `app/job_status.py`
  - New status tracking module.
  - Records job state transitions.
  - Builds the dashboard status rows from job status plus real database freshness.

- `app/util.py`
  - Added `format_elapsed_since` for display strings like `3m 05s ago` and `never`.

- `app/routers/api.py`
  - Added `GET /api/system-status`.
  - Wrapped manual job endpoints with status tracking.

- `app/routers/ui.py`
  - Dashboard route now loads `system_status` and passes it to the template.

- `app/templates/dashboard.html`
  - Added the System Status panel.
  - Added a 15 second dashboard refresh so running/stale/live states update while the app is open.

- `app/main.py`
  - Background market sync and settlement loops now report job status.

- `app/background_loops.py`
  - Background news polling, candidate processing, lag backfill, signal metrics, and lag ranking now report job status.

- `tests/test_job_status.py`
  - Added coverage for elapsed-time formatting, success/failure tracking, and green/yellow/red dashboard status rows.

- `tests/test_init_db_migrations.py`
  - Added coverage to make sure `job_statuses` exists after database initialization/backfill.

## Processes shown on the dashboard

- Market sync
- News polling
- Candidate processing
- Paper trades
- Lag backfill
- Signal metrics
- Lag ranks
- Settlement

## Behavior notes

- Running jobs override their data state and show yellow.
- Failed tracked jobs show red and preserve the last error.
- Data freshness is based on the latest real database record for each area.
- Settlement uses the last successful settlement run when available.
- Paper trades are data-only, so they show live/stale based on the newest trade rather than a tracked job runner.

## Verification

These checks passed locally:

- Full test suite: `38 passed`
- Python compile check for `app` and `tests`
- In-process app smoke check for the main pages, `/api/system-status`, and core job redirect endpoints

## Push status

**2026-04-27 — Git is unblocked and pushed.**

The local repo now tracks GitHub:

```text
origin  https://github.com/skynet-watcher/Polymarket-News-Reaction.git
branch  main
```

The remote initially had a one-line README commit, so Chad fetched `origin/main`, merged the unrelated histories, kept the full local project README, verified `pytest` still passes (**41 passed**), and pushed `main`.

GitHub repo:

```text
https://github.com/skynet-watcher/Polymarket-News-Reaction
```

Local-only files remain ignored/untracked via root `.gitignore`: `.env`, SQLite files (`*.db`, `*.db-shm`, `*.db-wal`), and `Keys/`.

### GitHub access coordination

- Chad/Codex can reach the remote and confirmed `origin/main` exists at commit `962c43f8a7f046ec00767409e9bef92afad8371a`.
- Repo URL for everyone:

```text
https://github.com/skynet-watcher/Polymarket-News-Reaction
```

- Lucy should confirm she can access it from Cursor/Terminal:

```bash
git clone https://github.com/skynet-watcher/Polymarket-News-Reaction.git
cd Polymarket-News-Reaction
git status
```

- If Lucy needs to push changes, Eric must make sure her GitHub account has write access to `skynet-watcher/Polymarket-News-Reaction` or she should open a pull request from a fork/branch.
- Local-only runtime files are not in GitHub. Lucy will need her own `.env` and local SQLite data, or Eric can share sanitized fixtures separately.

---

## Lucy — backend / thresholds / realtime (this doc replaces `app/README.md`)

Chad’s status UI handoff above is the right place for the **System Status** panel and `/api/system-status`. Below is what I added on the **data path and live dashboard** side so we stay aligned.

### Threshold profiles (DB-driven paper gates)

- `ThresholdProfile` + `threshold_profiles_seed.py` (conservative / balanced / aggressive).
- `threshold_context.py` + `RuntimeSetting` key `threshold_profile_id`; startup seed in `main.py`.
- `decide_action` takes profile-driven age + indirect-evidence rules; `maybe_paper_trade` uses `paper_size_multiplier`.
- `process_candidates`, `compute_lag` leakage helper, Settings UI + dashboard line for active profile.

### Realtime MVP (paper-only, “invested” = open `PaperTrade`)

- `realtime_policy.py` — shorter poll / process / snapshot sleeps as `Market.end_date` approaches for markets with open paper positions.
- `sync_markets.refresh_open_position_markets` — CLOB-only refresh between full Gamma syncs.
- `main.py` snapshot loop alternates full sync vs that refresh; `background_loops.py` uses adaptive sleeps after each run.
- `process_candidates` — `interpret_and_verify_with_timeout`, parallel workers (`llm_max_concurrency`), per-worker sessions.
- `dashboard_data.py` + `GET /api/stream/dashboard` (SSE; **`response_model=None`** on that route for FastAPI).
- `poll_news` logs `duration_ms`; `process_candidates` returns timing breakdown in the job dict.

### Coordination note (Chad + Lucy UI)

- **Wired (Lucy, follow-up):** `GET /api/system-status`, all manual job POSTs use `run_tracked_job`, background snapshot/settle/poll/process/lag pipelines update `JobStatus`, dashboard **System status** panel + **15s `fetch`** refresh (no full-page reload, so SSE stays connected).
- SSE still updates summary + recent signals when `dashboard_sse_enabled` is true.

### Quick verification

- `pytest` (includes `test_job_status`, `test_threshold_profiles`, `test_realtime_policy`, etc.) should pass after merging both lines of work.

---

## Next job: Chad — ship **live paper trading** (hands-off)

Goal: leave the app running unattended and reliably get **paper fills** on real news + real market data (still **no real money**).

1. **Environment & process**
   - Document a minimal **production-ish runbook** in the root `README.md`: required env vars (`OPENAI_API_KEY` optional but recommended, `DATABASE_URL` if not default SQLite, background interval envs **greater than zero** for hands-off).
   - Add a **single command** or `Makefile` target: `run` = uvicorn with sensible defaults; optional `run-worker` if you later split processes.

2. **Reliability**
   - Run the stack for **24h** on a laptop or small VM: confirm **System status** stays green/acceptable for market sync + news + candidates; note any `database is locked` (if so, document lowering `LLM_MAX_CONCURRENCY`).
   - Confirm **adaptive polling** kicks in when you have an **OPEN** paper trade near **`Market.end_date`** (Settings threshold profile + lag focus as needed).

3. **Observability**
   - Optional: log aggregation or a one-page **“last error”** summary (job_status already stores `last_error` per job — could add a small `/api/system-status` detail or export).

4. **Explicit non-goals for this task**
   - No live CLOB execution; `trading_enabled` stays false until a dedicated execution + audit design exists.

When this is done, the MVP should be **demo-ready**: start server, open `/`, watch System status + signals/trades without babysitting job buttons.

---

## Collaboration protocol (Chad ↔ Lucy)

Lucy **cannot** poll this file every five minutes in the background; she only runs when someone opens Cursor and sends a message (or when you automate a reminder).

**Practical workflow**

1. **Chad** adds requests under *Requests for Lucy* below (dated, concrete).
2. **Eric** (or Chad) tells Lucy in chat: *“Check `LUCY_STATUS_UI_HANDOFF.md`”* or *“Do Chad’s items in the handoff.”*
3. **Lucy** implements, runs `pytest`, and records what she did under *Lucy — completed / notes*.

Optional: set a **system reminder** or calendar ping every 5 minutes to say “ask Lucy to read the handoff” — that’s the human/automation substitute for an always-on poller.

### Requests for Lucy (Chad)

_(Chad: add tasks here. Example: “2026-04-28 — Add X to README runbook.”)_

### Lucy — completed / notes

_(Lucy: mark items done, link PRs/commits, call out blockers.)_

- **2026-04-27 —** Wired `GET /api/system-status`, tracked manual + background jobs, dashboard System status panel + 15s fetch refresh; see git history / earlier session.
- **2026-04-27 —** Repaired incomplete `.git` (added `objects` layout + valid repo state), initial commit on `main`, `.gitignore` for local-only files; `pytest` 41 passed. Push blocked only on supplying `origin` URL + auth.
- **2026-04-27 — Chad:** Added GitHub remote `https://github.com/skynet-watcher/Polymarket-News-Reaction.git`, merged GitHub's initial README commit, kept the full local README, verified `pytest` 41 passed, and pushed `main` successfully.
