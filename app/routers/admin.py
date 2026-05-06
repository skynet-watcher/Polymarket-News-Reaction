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
from app.security import verify_bearer_secret

logger = logging.getLogger(__name__)
router = APIRouter()

VALID_SCOPES = {"trades", "pipeline", "full"}


@router.post("/admin/reset")
async def admin_reset(
    scope: str,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(verify_bearer_secret),
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
    if scope == "full":
        for model in (ResolutionSourceMapping, Market, NewsSource):
            result = await session.execute(delete(model))
            counts[model.__tablename__] = result.rowcount  # type: ignore[attr-defined]

    await session.commit()

    logger.info("admin_reset scope=%s deleted=%s", scope, counts)
    return JSONResponse({"ok": True, "scope": scope, "deleted": counts})
