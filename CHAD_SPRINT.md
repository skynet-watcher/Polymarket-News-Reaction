# Chad — Sprint: Real Data by Tomorrow

> Written by **Alex** (reviewer/architect), 2026-04-28.
> Eric's goal: see real market matches, real signals, and real paper trades by morning.
> Pull `main` before you start — several critical fixes landed today.

---

## Context: what just changed (read this first)

1. **tokenIds bug found again at the field-name layer.** Every real Polymarket market had `token_ids_json = null` — CLOB price fetches silently skipped, zero snapshots ever written for real markets. Commit `8e53418` added the `/markets` merge path, but Gamma exposes usable CLOB tokens as `clobTokenIds` (often a JSON string), not only `tokenIds`. Chad fixed that locally; first sync after pulling should populate token IDs and snapshots should start flowing.

2. **Two-stage matcher deployed.** Keyword pre-filter at 0.15 threshold + batched LLM relevance screen. Signals were near-zero before; should be much higher now.

3. **Backtest trade marking.** `PaperTrade.trade_source` is now `LIVE` or `BACKTEST`. Backtest cases now record `signal_action`.

Full details: **Engineering Log** section in `README.md`.

---

## Sprint shape

| Block | When | Hours |
|---|---|---|
| **Block A: Health check + source expansion** | First thing, tonight | ~2h |
| **Block B: Signal funnel UI** | After A | ~2h |
| **Block C: UI polish (backtest + trades)** | After B | ~2h |
| **Block D: Research profile + threshold tuning** | After C | ~1h |

If you only have 4 hours: do **A** completely, then **B**, then whatever of **C** fits.

---

## Block A — Health check + news source expansion

### A1. Pull, restart, verify snapshots are flowing

```bash
git pull origin main
make run   # or make run-realtime for faster cadence
```

Then from the dashboard or curl:
```bash
# Trigger a fresh sync
curl -s -X POST http://127.0.0.1:8000/api/jobs/sync_markets

# Wait ~30s, then check snapshot count (should be > 6 and growing)
sqlite3 data.db "SELECT COUNT(*), MAX(timestamp) FROM price_snapshots WHERE market_id != 'smoke_mkt';"

# Check that token IDs are now populated
sqlite3 data.db "SELECT COUNT(*) FROM markets WHERE json_type(token_ids_json)='array' AND json_array_length(token_ids_json)>0 AND active=1 AND closed=0;"
```

**Done when:** snapshot count for non-fixture markets is growing after each sync.

**Status:** fixed locally by Chad. `sync_markets` now parses Gamma `clobTokenIds` and JSON-encoded arrays for both outcomes and token IDs; tests cover the snapshot path. Full sync also has a bounded CLOB probe cap (`SYNC_CLOB_SNAPSHOT_LIMIT`, default 50) so a manual sync cannot spend forever on hundreds of orderbooks.

---

### A2. Reactivate Reuters + fix source tiers

Reuters is in the DB but disabled (`active=0`) and tagged `WIRE` (old tier, not recognised by the matcher). Fix via the Settings UI or directly:

```bash
sqlite3 data.db "UPDATE news_sources SET active=1, source_tier='HARD' WHERE domain='reuters.com';"
```

Also tag AP and Bloomberg if/when added as `HARD` — these are authoritative wires that should get higher weight in the lag module later.

**Done when:** Reuters shows `active=1` and `source_tier='HARD'` in the Settings sources list.

---

### A3. Add high-signal news sources

Current sources are mostly general world news. Polymarket skews heavily toward US politics, crypto, finance, and sports. Add these — all have reliable RSS feeds:

**Must-add (high overlap with Polymarket market universe):**

| Source | Domain | RSS URL | Tier |
|---|---|---|---|
| Associated Press | apnews.com | `https://feeds.apnews.com/rss/topnews` | `HARD` |
| AP — Politics | apnews.com | `https://feeds.apnews.com/rss/politics` | `HARD` |
| AP — Business | apnews.com | `https://feeds.apnews.com/rss/business` | `HARD` |
| CNBC — Top News | cnbc.com | `https://www.cnbc.com/id/100003114/device/rss/rss.html` | `SOFT` |
| ESPN — Top Headlines | espn.com | `https://www.espn.com/espn/rss/news` | `SOFT` |
| Crypto Slate | cryptoslate.com | `https://cryptoslate.com/feed/` | `SOFT` |
| CoinDesk | coindesk.com | `https://www.coindesk.com/arc/outboundfeeds/rss/` | `SOFT` |
| The Hill | thehill.com | `https://thehill.com/feed/` | `SOFT` |
| Fox News — Politics | foxnews.com | `https://feeds.foxnews.com/foxnews/politics` | `SOFT` |

**Add via Settings UI** (Sources section) or insert directly:
```sql
INSERT OR IGNORE INTO news_sources (name, domain, rss_url, source_tier, polling_interval_minutes, active)
VALUES
  ('AP — Top News',    'apnews.com',      'https://feeds.apnews.com/rss/topnews',              'HARD', 5, 1),
  ('AP — Politics',   'apnews.com',      'https://feeds.apnews.com/rss/politics',              'HARD', 5, 1),
  ('AP — Business',   'apnews.com',      'https://feeds.apnews.com/rss/business',              'HARD', 5, 1),
  ('CNBC',            'cnbc.com',        'https://www.cnbc.com/id/100003114/device/rss/rss.html','SOFT', 5, 1),
  ('ESPN',            'espn.com',        'https://www.espn.com/espn/rss/news',                 'SOFT', 10, 1),
  ('CoinDesk',        'coindesk.com',    'https://www.coindesk.com/arc/outboundfeeds/rss/',    'SOFT', 5, 1),
  ('CryptoSlate',     'cryptoslate.com', 'https://cryptoslate.com/feed/',                      'SOFT', 5, 1),
  ('The Hill',        'thehill.com',     'https://thehill.com/feed/',                          'SOFT', 5, 1),
  ('Fox News Politics','foxnews.com',    'https://feeds.foxnews.com/foxnews/politics',         'SOFT', 5, 1);
```

> **Note:** some of these feeds may have moved or rate-limit aggressively. Test each with `curl -I <rss_url>` first. If a feed returns 4xx, skip it and note it here.

**Also check:** `apnews.com` appears twice in the insert (same domain, different paths). SQLite's `UNIQUE` constraint is on `domain`, so only the first AP row will insert. If you want multiple AP feeds, you'll need to either relax that constraint or use subdomains. For now, just use the top-news feed — it's the most comprehensive.

**Done when:** `SELECT COUNT(*) FROM news_sources WHERE active=1;` returns 15+, and a `poll_news` run inserts new articles.

---

### A4. Verify Gamma near-resolution params

`_fetch_near_resolution_markets` in `sync_markets.py` passes `end_date_min` and `end_date_max` to Gamma. These are guessed param names — verify they work:

```bash
# Check if the sweep is returning anything after sync
sqlite3 data.db "
  SELECT COUNT(*) FROM markets
  WHERE end_date <= datetime('now', '+48 hours')
  AND end_date >= datetime('now')
  AND active=1 AND closed=0;"
```

If count is 0 after a sync, the params are wrong. Check the Gamma `/markets` API and fix the param names in `_fetch_near_resolution_markets`. If you can't find the right params, comment out the `end_date_min`/`end_date_max` filter and instead post-filter results in Python — it's worth getting near-resolution markets even if we have to over-fetch.

**Done when:** the above query returns > 0 markets after a sync.

**Status:** docs-confirmed by Chad. Polymarket Gamma `/markets` documents `end_date_min` and `end_date_max` as valid query parameters.

---

## Block B — Signal funnel visibility

Right now there's no way to see *where* signals are dropping off. We need a funnel view to diagnose whether the matcher, the LLM screen, or the gating is the bottleneck.

### B1. Add match stats to `process_candidates` job response + System status

In `process_candidates.py`, the `run()` function already returns `signals_created`, `signals_processed`, `trades_created`. Add:

- `keyword_candidates_total` — sum of keyword stage candidates across all articles
- `llm_screened` — total that passed to batch_relevance_screen
- `llm_passed` — total that cleared `matcher_llm_min_relevance`
- `signals_skipped_duplicate` — already-existing signals that were skipped

Return these in the job response dict. Then surface them on System status alongside the existing counts.

**Done when:** the `process_candidates` job result shows the funnel numbers, and at least one number is non-zero.

---

### B2. Signal funnel page at `/analysis/funnel`

New page (small template, one query). Shows for the last 7 days:

- Articles polled per day (bar chart or table)
- Keyword candidates generated
- LLM screen passes
- Signals created
- ACT / ABSTAIN / REJECT breakdown
- Paper trades created

This doesn't need to be pretty — a plain HTML table is fine. The goal is to see at a glance whether the pipeline is producing anything.

Route: `GET /analysis/funnel`
Template: `app/templates/funnel.html`
Query: aggregate from `news_articles`, `news_signals`, `paper_trades` by day.

**Done when:** `/analysis/funnel` loads and shows non-zero numbers.

---

### B3. Show rejection reasons on the signals page

The signals page currently shows `action` but not `rejection_reason`. Add a tooltip or expandable column showing why a signal was ABSTAIN / REJECT_*. This is the fastest way to see if the gating thresholds are too tight.

**Done when:** rejection reasons are visible on `/signals` for at least one rejected signal.

---

## Block C — UI polish (backtest + trades)

### C1. `[BACKTEST]` badge on trades page

`PaperTrade.trade_source` is `LIVE` or `BACKTEST`. On `/trades`, add a small muted badge:
```html
{% if trade.trade_source == 'BACKTEST' %}
  <span class="badge badge-secondary">BACKTEST</span>
{% endif %}
```
Link `backtest_case_id` to the relevant case on `/analysis/backtests` if it exists.

**Done when:** backtest trades show a distinct badge; live trades show nothing different.

**Status:** done locally by Chad.

---

### C2. Backtest run selector

The `/analysis/backtests` page already fetches `runs` but only shows the latest. Add a simple `<select>` or list on the page so you can view any past run's cases. The backend already passes `runs` to the template — it's template-only work.

**Done when:** clicking a past run loads its cases.

**Status:** done locally by Chad.

---

### C3. `signal_action` filter on backtests

Add a filter row or tab on `/analysis/backtests` for:
- All cases
- ACT (live trade was made)
- Non-ACT (missed opportunities — most interesting for research)

Use `BacktestCase.signal_action` which is now populated. Query-side, just add a `WHERE signal_action = ?` clause when the filter is selected.

**Done when:** user can filter backtest cases to show only "missed" signals.

**Status:** done locally by Chad; current UI filters exact `signal_action` values, including `NONE`.

---

## Block D — Threshold tuning

### D1. Add a "research" threshold profile

The `conservative` profile (`min_confidence=0.90`, `min_verifier_confidence=0.85`) is correct for live paper trading but too tight to see research signals. Add a `research` profile in the seed/settings that lets more signals through for analysis without pretending they're high-quality trades.

In `app/threshold_profiles_seed.py` (or wherever profiles are seeded), add:

```python
{
    "id": "research",
    "label": "Research (high recall, low precision)",
    "min_liquidity": 500.0,
    "max_spread": 0.15,
    "min_relevance": 0.40,      # LLM screen already at 0.50; this is signal-level
    "min_confidence": 0.60,
    "min_verifier_confidence": 0.55,
    "max_article_age_minutes": 120,
    "allow_indirect_evidence": True,
    "paper_size_multiplier": 0.5,   # half size — it's research, not conviction
},
```

**Done when:** `research` appears in the Settings threshold profile dropdown. Switch to it, run `process_candidates`, see more ACT signals.

---

### D2. Verify end-to-end: article → signal → trade

After all the above, run a full cycle manually:

```bash
curl -s -X POST http://127.0.0.1:8000/api/jobs/poll_news
curl -s -X POST http://127.0.0.1:8000/api/jobs/process_candidates
```

Then check:
```bash
sqlite3 data.db "
SELECT a.title, m.question, s.action, s.confidence, s.rejection_reason
FROM news_signals s
JOIN news_articles a ON a.id = s.article_id
JOIN markets m ON m.id = s.market_id
WHERE s.created_at >= datetime('now', '-1 hour')
ORDER BY s.created_at DESC LIMIT 20;"
```

Look for:
- Non-zero signal count → matcher is working
- `action = 'ACT'` rows → thresholds are passing at least some signals
- If all `ABSTAIN`/`REJECT_*` → loosen threshold profile or check LLM key

**Done when:** at least one `ACT` signal in the last hour.

---

## Acceptance: "real data by morning"

Eric wants to open the dashboard tomorrow and see:

- [ ] Snapshot count growing (> 100 non-fixture snapshots in DB)
- [ ] Articles from real sources (AP, BBC, Guardian etc.) in `/news`
- [ ] Signals on `/signals` — mix of ACT / ABSTAIN, real market questions
- [ ] At least one paper trade on `/trades` (LIVE source)
- [ ] `/analysis/funnel` shows the pipeline running
- [ ] `/analysis/backtests` has at least one run with cases

---

## When you finish a block

Update **Chad — completed** at the bottom with date + one line per item. Push to `main`.

---

## Chad — completed (log)

- **2026-04-27 —** P0 item 1: `.git` repaired; `origin` → `https://github.com/skynet-watcher/Polymarket-News-Reaction.git`; `main` pushed / tracked.
- **2026-04-28 —** Hands-off realtime paper: `REALTIME_PAPER_QUICKSTART`, `make run-realtime`, README runbook/soak/SSE/proxy, snapshot loop heartbeat, async lag backfill, `GET /api/export/summary`, System status shows last job duration + row links, dashboard JS parity.
- **2026-04-28 —** Phase 1 news reaction backtester: `BacktestRun`, `BacktestCase`, `BacktestEventLog`, `POST /api/jobs/backtest_news_reactions`, `/analysis/backtests`, JSONL audit logs. 52 tests pass.
- **2026-04-28 —** No-paper-trades diagnosis: real markets still had JSON `null` token IDs; `sync_markets` now reads Gamma `clobTokenIds`, existing SQLite DBs backfill backtest trade columns, `/trades` badges BACKTEST rows, and `/analysis/backtests` supports run/action filtering.

---

_Alex — 2026-04-28. Eric's call on priorities; this is the fastest path to real data I can see._
