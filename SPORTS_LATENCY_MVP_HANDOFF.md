# Sports Settlement Latency MVP

Core question: after an independent sports source showed Final, was Polymarket still tradable before market settlement, and what would paper PnL have been?

This branch is paper-only. No real orders. No private trading keys. `trading_enabled = false` is enforced as a safety constant in code.

## Build Order

This implementation is the hybrid first slice:

1. Vercel UI/API/crons, schema, manual controls, and REST fallback.
2. Worker-ready timestamp/data model.
3. Persistent WebSocket worker deployment next.

## Two-Clock Model

The app stores three clocks:

- `independent_source_observed_final_at`: when this server observed an independent source say Final. This is the only timestamp used for paper-trading claims.
- `independent_source_reported_final_at`: source-owned timestamp when present. This is analytical only.
- `polymarket_sports_ws_final_at`: when Polymarket's sports data layer knew.
- `polymarket_market_resolved_at`: when the Polymarket market settled.

Derived metrics:

- `tradable_window_observed_ms = T2 - T0_observed`
- `tradable_window_reported_ms = T2 - T0_reported`
- `polymarket_internal_delay_ms = T2 - T1`

Polymarket Sports WS is market-side evidence only. It never triggers a paper trade.

## MVP Scope

Included:

- NBA, NHL, MLB
- Same-day game-result/moneyline markets
- Independent source observations
- Market-side timestamp fields
- Paper trades and missed-window simulations
- Event timeline and bakeoff dashboard

Excluded:

- NFL/CFB until a free source above ESPN confidence `0.9` is identified
- Soccer, tennis, esports
- Props, spreads, totals, futures, series markets
- SofaScore/FotMob adapters
- Real-money trading

## Vercel Crons

Configured in `vercel.json`:

- `/api/cron/sports/build-watchlist` daily at midnight UTC
- `/api/cron/sports/poll` every minute
- `/api/cron/sports/settle` every 5 minutes

Manual controls are available from the Dev tools menu and the Sports Watchlist page.

## High-Priority Next Infrastructure Step

Deploy the persistent WebSocket worker in `workers/sports_ws` on Railway, Fly.io, Render, or a small VPS.

The worker must:

- Hold long-lived connections to Polymarket Sports WS and market WS.
- Read active `condition_id` values from `sports_watchlist`.
- Refresh subscriptions every 5 minutes.
- Write market-side observations and settlement events to Postgres.
- Append reconnect/gap events to `watched_event_log`.

This worker is intentionally a timestamp collector only. It should not contain paper-trade business logic.

## Overnight Run Notes — 2026-05-10

Chad left a local overnight backstop running because the Vercel preview is still behind deployment protection (`401` from the preview URL without Vercel login).

Running locally:

- Sports collector LaunchAgent: `/Users/eric/Library/LaunchAgents/com.eric.polymarket.sports-latency-loop.plist`
- Local UI LaunchAgent: `/Users/eric/Library/LaunchAgents/com.eric.polymarket.local-ui.plist`
- Keep-awake LaunchAgent: `/Users/eric/Library/LaunchAgents/com.eric.polymarket.keepawake.plist`
- Local dashboard: `http://127.0.0.1:8000/`
- Collector log: `logs/sports_latency_launchd.err.log`
- UI log: `logs/local_ui_launchd.err.log`

Fixes pushed on `sports-latency-mvp`:

- `2f1fa1e` guards watchlist times by actual game time instead of market-created `startDate`.
- `586313a` normalizes naive/aware datetimes before settlement metrics.
- `e3c2043` forces the local collector to run settlement checks immediately after restart.

Two early paper trades were invalidated in local SQLite because they were created before these guards and matched stale final scores against future same-team markets. They remain in the audit trail as `INVALIDATED`, not deleted.
