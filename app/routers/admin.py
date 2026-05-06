"""
Admin endpoints: data reset scopes for observing the app from a clean state.

All endpoints require Authorization: Bearer <CRON_SECRET> (same secret as cron endpoints).

Scopes
------
trades    — wipe paper_trades only
pipeline  — wipe trades + signals + articles + price_snapshots + all analytics
full      — everything above + markets + news_sources (true cold start)

Always preserved: threshold_profiles, runtime_settings, job_statuses, bias_hypotheses
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.live_feeds import ensure_live_news_sources
from app.models import (
    BacktestCase,
    BacktestEventLog,
    BacktestRun,
    LagMeasurement,
    LagScoreSnapshot,
    LagThresholdCrossing,
    Market,
    MarketLagScore,
    NewsArticle,
    NewsSignal,
    NewsSource,
    PaperTrade,
    PriceSnapshot,
    ResolutionSourceMapping,
    SignalDriftWindow,
    SignalMetrics,
)

logger = logging.getLogger(__name__)
router = APIRouter()

VALID_SCOPES = {"trades", "pipeline", "full"}


@router.post("/admin/reset")
async def admin_reset(
    scope: str,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """
    Reset app data.  scope = trades | pipeline | full
    """
    if scope not in VALID_SCOPES:
        raise HTTPException(status_code=400, detail=f"scope must be one of {sorted(VALID_SCOPES)}")

    counts: dict[str, int] = {}

    # ── analytics (common to pipeline + full) ──────────────────────────────
    if scope in ("pipeline", "full"):
        for model in (
            LagScoreSnapshot,
            LagThresholdCrossing,
            SignalDriftWindow,
            BacktestEventLog,
            BacktestCase,
            BacktestRun,
            SignalMetrics,
            MarketLagScore,
            LagMeasurement,
        ):
            result = await session.execute(delete(model))
            counts[model.__tablename__] = result.rowcount  # type: ignore[attr-defined]

    # ── trades (all scopes) ────────────────────────────────────────────────
    result = await session.execute(delete(PaperTrade))
    counts[PaperTrade.__tablename__] = result.rowcount  # type: ignore[attr-defined]

    # ── pipeline data (pipeline + full) ───────────────────────────────────
    if scope in ("pipeline", "full"):
        for model in (NewsSignal, PriceSnapshot, NewsArticle):
            result = await session.execute(delete(model))
            counts[model.__tablename__] = result.rowcount  # type: ignore[attr-defined]

    # ── markets + sources (full only) ─────────────────────────────────────
    # Use TRUNCATE CASCADE rather than sequential DELETEs.  Cron jobs run every
    # minute and can insert new price_snapshots between our DELETE price_snapshots
    # and DELETE markets (both within the same transaction), causing a FK violation.
    # TRUNCATE CASCADE is a single atomic operation that handles FK order for us.
    if scope == "full":
        _full_tables = (
            "lag_score_snapshots",
            "lag_threshold_crossings",
            "signal_drift_windows",
            "backtest_event_logs",
            "backtest_cases",
            "backtest_runs",
            "signal_metrics",
            "market_lag_scores",
            "lag_measurements",
            "paper_trades",
            "news_signals",
            "price_snapshots",
            "news_articles",
            "resolution_source_mappings",
            "markets",
            "news_sources",
        )
        await session.execute(text(f"TRUNCATE {', '.join(_full_tables)} RESTART IDENTITY CASCADE"))
        counts = {t: -1 for t in _full_tables}  # TRUNCATE doesn't return row counts

    await session.commit()

    # Re-seed news sources immediately so polls don't silently no-op after a full reset.
    if scope == "full":
        seed = await ensure_live_news_sources(session)
        counts["_seeded_sources"] = seed.get("added", 0)

    logger.info("admin_reset scope=%s deleted=%s", scope, counts)
    return JSONResponse({"ok": True, "scope": scope, "deleted": counts})
