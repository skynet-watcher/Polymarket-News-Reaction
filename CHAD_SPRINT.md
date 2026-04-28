# Chad — next sprint backlog (big job list)

Lucy is handing this off: **you own execution**; items below are suggestions, priorities, and acceptance hints. Reorder as you like.

---

## Solo overnight (no Cursor / no Lucy)

Use this if you are heads-down and only have the repo + terminal.

1. **Repo:** `https://github.com/skynet-watcher/Polymarket-News-Reaction.git` — `git pull` on `main` before you start.
2. **Env:** `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt` — copy `.env.example` → `.env` and set at least `OPENAI_API_KEY` (optional but recommended for signals).
3. **Run:** `make run` (token-light defaults) or `make run-realtime` (faster paper cadence) — listens on `0.0.0.0:8000`. Quick checks: `curl -s http://127.0.0.1:8000/healthz` and open `/` for **System status**; optional `curl -s http://127.0.0.1:8000/api/export/summary`.
4. **If the app misbehaves:** see **Quickstart** + “Next steps” in root `README.md`; threshold tuning in **Settings**; SQLite lock → set `LLM_MAX_CONCURRENCY=1` in `.env` and restart (also in README).
5. **Before you commit:** `make test` (expect **44+** passed). Do **not** commit `.env`, `*.db*`, or `Keys/` (they are gitignored).
6. **Context on the dashboard / jobs:** `LUCY_STATUS_UI_HANDOFF.md` (behavior + endpoints). **Your backlog** stays in *this* file.

---

## P0 — Unblock “real repo” and shared truth

1. **Restore a valid git checkout**  
   - Replace or repair `.git` so `git status`, `git pull`, and `git push` work.  
   - **Done when:** another dev can clone and run `make run` from `main` (or your agreed default branch).  
   - **Status:** done for `main` at [Polymarket-News-Reaction](https://github.com/skynet-watcher/Polymarket-News-Reaction) — see *Chad — completed* below.

2. **Single “source of truth” for handoff**  
   - Keep `LUCY_STATUS_UI_HANDOFF.md` + this file; delete stale duplicates if any appear.  
   - Add a one-line pointer in root `README.md`: “See `CHAD_SPRINT.md` for current sprint tasks.”

3. **CI smoke (minimal)**  
   - GitHub Actions (or similar): `pip install -r requirements.txt`, `pytest`, optional `ruff`/`mypy` if you add configs.  
   - **Done when:** PRs get a green check without manual ritual.

---

## P0 — Ops / runbook (hands-off paper MVP)

4. **Production-ish runbook (expand README)**  
   - Document: required vs optional env vars, `.env` example **without secrets**, `make run` vs `make run-dev`, where `data.db` lives, backup/restore SQLite, “first hour” checklist.  
   - **Done when:** Eric can follow README only and get a stable dashboard.

5. **Long-run soak protocol**  
   - 4–24h run template: what to watch on System status, log files, disk growth, when to restart.  
   - Capture **one** real incident + resolution (e.g. `database is locked`, cancel during lag backfill).

6. **Job duration visibility**  
   - Surface **last duration** or “slow job” warning for `lag_backfill` / `process_candidates` (even a JSON field or extra row in System status). Helps explain yellow/red.

---

## P1 — UX / UI polish

7. **Lag backfill: don’t block the browser**  
   - **Status:** `POST /api/lag-measurements/backfill` now queues work via FastAPI `BackgroundTasks` and returns immediately; use System status to watch `lag_backfill`.  
   - **Done when:** user never has to keep a tab open for 10+ minutes for a routine backfill.

8. **System status: link to drill-down**  
   - Each row links to the relevant page (`/news`, `/signals`, `/analysis/lags`, etc.) or pre-filtered view.

9. **Empty states**  
   - Laggy markets, lags analysis, soft accuracy: short copy when `0` rows (“Run backfill after you have ACT signals…”).

10. **Settings: grouped sections + “danger zone”**  
    - Threshold profile, lag focus, feeds, mappings — collapsible or anchored headings.

---

## P1 — Reliability / data quality

11. **SQLite under parallel LLM**  
    - Document `LLM_MAX_CONCURRENCY=1` for flaky setups; consider queue-based candidate processing later (out of scope unless you want it).

12. **Idempotent / safe RSS**  
    - Lucy added URL dedupe vs shifting `published_at`; add a **regression test** that mirrors a real Guardian URL if you have a fixture dump.

13. **Market sync staleness**  
    - If snapshot loop fails silently, System status goes stale — add **heartbeat** log line or counter every N ticks so logs prove the loop is alive.

14. **Failed job “Retry” affordance**  
    - One-click POST from UI for the failed job name (or copyable `curl`).

---

## P2 — Observability

15. **Structured logging**  
    - JSON logs optional via env; include `job_name`, `duration_ms`, `outcome` for background loops.

16. **Export**  
    - `GET /api/system-status` already exists; add `GET /api/export/summary` (counts + last success per job) for Eric to paste into notes.

17. **SSE / proxy doc**  
    - Short nginx/Caddy snippet: `proxy_buffering off` for `/api/stream/dashboard`, timeouts.

---

## P2 — Product / research

18a. **News reaction backtester**
    - Phase 1 local snapshots only: measure publication→fetch/system delay and post-publication market movement.
    - **Status:** implemented locally for review: `BacktestRun`, `BacktestCase`, `BacktestEventLog`, `POST /api/jobs/backtest_news_reactions`, `/analysis/backtests`, and JSONL audit logs under `logs/backtests/`.

18. **Laggy markets: explain the score**  
    - Tooltip or `/analysis/laggy-markets` paragraph: what `combined_score` means, data prerequisites.

19. **Threshold profile presets in UI**  
    - Read-only table of numeric columns for `conservative` / `balanced` / `aggressive` so users don’t have to read seed code.

20. **Paper PnL sanity**  
    - On `/trades`, flag OPEN trades with no snapshot in X hours; link to “sync markets”.

---

## P3 — Future (don’t start unless P0–P1 clear)

21. **Postgres option**  
    - Docker-compose + `DATABASE_URL` for multi-writer / fewer SQLite edge cases.

22. **Auth on admin routes**  
    - If exposing beyond LAN: API key or basic auth on `POST /api/jobs/*`.

23. **Real execution**  
    - Explicitly **not** this sprint; keep `trading_enabled` false until audit + execution design exists.

---

## Suggested sprint shape

- **Week 1 focus:** items **1–6** + **7** + **14**.  
- **Week 2 focus:** **8–13**, **15–16**.  
- **Ongoing:** **18–20** as filler.

---

## When you finish a chunk

- Move done items to a **“Chad — completed”** section at the bottom of this file (date + one line).  
- Ping Eric / Lucy in chat: *“Chad sprint: closed items X,Y,Z in `CHAD_SPRINT.md`.”*

---

## Chad — completed (log)

- **2026-04-27 —** P0 item 1: `.git` repaired; `origin` → `https://github.com/skynet-watcher/Polymarket-News-Reaction.git`; `main` pushed / tracked. Local-only paths ignored: `.env`, `*.db*`, `Keys/`.
- **2026-04-28 —** Hands-off **realtime paper**: `REALTIME_PAPER_QUICKSTART` + `make run-realtime`, README runbook/soak/SSE/proxy, snapshot loop heartbeat, async lag backfill (P1 #7), `GET /api/export/summary`, System status shows last job duration + row links, dashboard JS parity.
- **2026-04-28 —** Phase 1 **news reaction backtester** implemented locally for review: local `price_snapshots` only, DB rows + JSONL event log, `/analysis/backtests`, and job endpoint with `since_hours`, `max_articles`, `min_snapshot_coverage`. Full suite: `52 passed`. Not pushed.

---

_Last filled by Lucy for Chad — no code in this commit path; implement at your pace._
