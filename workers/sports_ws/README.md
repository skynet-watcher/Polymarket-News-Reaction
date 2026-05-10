# Sports WebSocket Worker

This worker is the next high-priority infrastructure step after the Vercel-first MVP is live.

Vercel serverless is not a reliable home for long-lived WebSocket clients. The worker should run on Railway, Fly.io, Render, or a small VPS and write into the same Postgres database as the Vercel app.

Responsibilities:

- Read active `condition_id` values from `sports_watchlist`.
- Subscribe to `wss://sports-api.polymarket.com/ws` for sports result messages.
- Subscribe to the Polymarket market WebSocket for market lifecycle events such as `market_resolved`.
- Write market-side rows into `source_observations`.
- Update `market_resolution_records.polymarket_sports_ws_final_at` and `market_resolution_records.polymarket_market_resolved_at`.
- Append `watched_event_log` rows for reconnects, gaps, final signals, and settlement events.
- Refresh subscriptions every 5 minutes so newly-added watchlist rows are picked up.

The worker intentionally owns no trading logic. It is only a timestamp collector and DB writer.

