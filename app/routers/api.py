from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Optional, Union

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.dashboard_data import get_dashboard_snapshot
from app.db import SessionLocal, get_session
from app.job_status import build_system_status, run_tracked_job
from app.settings import settings
from app.util import now_utc
from app.jobs import backtest_news_reactions, compute_lag, lag_rank, process_candidates, poll_news, settle_trades, signal_metrics, sync_markets
from app.live_feeds import ensure_live_news_sources
from sqlalchemy import desc, select

from app.models import LagMeasurement, LagThresholdCrossing, SignalDriftWindow


router = APIRouter()


@router.get("/system-status")
async def get_system_status(session: AsyncSession = Depends(get_session)) -> list[dict]:
    """Dashboard job/data freshness rows (green/yellow/red)."""
    return await build_system_status(session)


@router.get("/stream/dashboard", response_model=None)
async def dashboard_event_stream() -> Union[StreamingResponse, JSONResponse]:
    """Server-sent events: periodic dashboard JSON for live UI updates (paper MVP)."""
    if not settings.dashboard_sse_enabled:
        return JSONResponse({"ok": False, "error": "sse_disabled"}, status_code=503)

    async def events():
        while True:
            try:
                async with SessionLocal() as session:
                    snap = await get_dashboard_snapshot(session)
                yield f"data: {json.dumps(snap)}\n\n"
            except Exception:
                yield f"data: {json.dumps({'error': 'snapshot_failed'})}\n\n"
            await asyncio.sleep(max(1.0, settings.dashboard_sse_interval_seconds))

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "application/json" in accept


def _job_response(request: Request, payload: dict, redirect_url: str) -> Union[JSONResponse, RedirectResponse]:
    if _wants_json(request):
        return JSONResponse(payload)
    return RedirectResponse(url=redirect_url, status_code=303)


@router.post("/jobs/seed_live_feeds", response_model=None)
async def job_seed_live_feeds(request: Request, session: AsyncSession = Depends(get_session)) -> Union[JSONResponse, RedirectResponse]:
    """Re-run default RSS upsert (see `app/live_feeds.py`)."""
    out = await run_tracked_job(session, "seed_live_feeds", lambda: ensure_live_news_sources(session))
    return _job_response(request, out, "/settings")


@router.post("/jobs/sync_markets", response_model=None)
async def job_sync_markets(request: Request, session: AsyncSession = Depends(get_session)) -> Union[JSONResponse, RedirectResponse]:
    out = await run_tracked_job(session, "sync_markets", lambda: sync_markets.run(session))
    return _job_response(request, out, "/markets")


@router.post("/jobs/poll_news", response_model=None)
async def job_poll_news(request: Request, session: AsyncSession = Depends(get_session)) -> Union[JSONResponse, RedirectResponse]:
    out = await run_tracked_job(session, "poll_news", lambda: poll_news.run(session))
    return _job_response(request, out, "/news")


@router.post("/jobs/process_candidates", response_model=None)
async def job_process_candidates(request: Request, session: AsyncSession = Depends(get_session)) -> Union[JSONResponse, RedirectResponse]:
    out = await run_tracked_job(session, "process_candidates", lambda: process_candidates.run(session))
    return _job_response(request, out, "/signals")


@router.post("/jobs/settle_trades", response_model=None)
async def job_settle_trades(request: Request, session: AsyncSession = Depends(get_session)) -> Union[JSONResponse, RedirectResponse]:
    out = await run_tracked_job(session, "settle_trades", lambda: settle_trades.run(session))
    return _job_response(request, out, "/trades")


@router.post("/jobs/backtest_news_reactions", response_model=None)
async def job_backtest_news_reactions(
    request: Request,
    session: AsyncSession = Depends(get_session),
    since_hours: int = Query(72, ge=1, le=24 * 30),
    max_articles: int = Query(50, ge=1, le=500),
    min_snapshot_coverage: int = Query(3, ge=1, le=100),
) -> Union[JSONResponse, RedirectResponse]:
    out = await run_tracked_job(
        session,
        "backtest_news_reactions",
        lambda: backtest_news_reactions.run(
            session,
            since_hours=since_hours,
            max_articles=max_articles,
            min_snapshot_coverage=min_snapshot_coverage,
        ),
    )
    return _job_response(request, out, "/analysis/backtests")


@router.post("/lag-measurements/backfill", response_model=None)
async def backfill_lag_measurements(
    request: Request,
    background_tasks: BackgroundTasks,
) -> Union[JSONResponse, RedirectResponse]:
    """Queue lag backfill in a background task so the browser request returns immediately."""

    async def _run_lag_backfill() -> None:
        async with SessionLocal() as session:
            await run_tracked_job(session, "lag_backfill", lambda: compute_lag.run_backfill(session))

    background_tasks.add_task(_run_lag_backfill)
    return _job_response(
        request,
        {
            "ok": True,
            "accepted": True,
            "job": "lag_backfill",
            "message": "Lag backfill started in the background; watch System status for progress.",
        },
        "/analysis/lags",
    )


@router.get("/export/summary")
async def export_summary(session: AsyncSession = Depends(get_session)) -> dict[str, object]:
    """JSON snapshot for operators: dashboard counts + system status rows (paste into notes)."""
    snap = await get_dashboard_snapshot(session)
    rows = await build_system_status(session)
    return {
        "ok": True,
        "generated_at": now_utc().isoformat(),
        "realtime_paper_quickstart": settings.realtime_paper_quickstart,
        "dashboard": snap,
        "system_status": rows,
    }


@router.post("/jobs/compute_signal_metrics", response_model=None)
async def job_compute_signal_metrics(request: Request, session: AsyncSession = Depends(get_session)) -> Union[JSONResponse, RedirectResponse]:
    out = await run_tracked_job(session, "signal_metrics", lambda: signal_metrics.run_backfill(session, limit=200))
    return _job_response(request, out, "/analysis")


@router.post("/jobs/compute_lag_ranks", response_model=None)
async def job_compute_lag_ranks(request: Request, session: AsyncSession = Depends(get_session)) -> Union[JSONResponse, RedirectResponse]:
    out = await run_tracked_job(session, "lag_ranks", lambda: lag_rank.run(session))
    return _job_response(request, out, "/analysis/laggy-markets")


@router.get("/lag-measurements")
async def get_lag_measurements(
    session: AsyncSession = Depends(get_session),
    limit: int = Query(500, ge=1, le=5000),
    category: Optional[str] = None,
    implied_outcome: Optional[str] = Query(None, pattern="^(YES|NO)$"),
    price_lag_status: Optional[str] = None,
    clean_only: bool = False,
    since: Optional[str] = None,
) -> list[dict]:
    """
    Returns lag measurements with their drift windows and crossings.
    Intended for dashboard use; stable-enough JSON for review.
    """
    q = select(LagMeasurement)
    if category is not None:
        q = q.where(LagMeasurement.category == category)
    if implied_outcome is not None:
        q = q.where(LagMeasurement.implied_outcome == implied_outcome)
    if price_lag_status is not None:
        q = q.where(LagMeasurement.price_lag_status == price_lag_status)
    if clean_only:
        q = q.where(LagMeasurement.clean_signal == True)  # noqa: E712
    if since is not None:
        try:
            since_dt = dt.datetime.fromisoformat(since.replace("Z", "+00:00"))
            q = q.where(LagMeasurement.created_at >= since_dt)
        except Exception:
            pass

    lms = (await session.execute(q.order_by(desc(LagMeasurement.created_at)).limit(limit))).scalars().all()
    if not lms:
        return []

    ids = [lm.id for lm in lms]
    crossings = (
        await session.execute(select(LagThresholdCrossing).where(LagThresholdCrossing.lag_measurement_id.in_(ids)))
    ).scalars().all()
    drifts = (
        await session.execute(select(SignalDriftWindow).where(SignalDriftWindow.lag_measurement_id.in_(ids)))
    ).scalars().all()

    cross_by = {}
    for c in crossings:
        cross_by.setdefault(c.lag_measurement_id, []).append(
            {
                "threshold_type": c.threshold_type,
                "threshold_label": c.threshold_label,
                "threshold_value": c.threshold_value,
                "crossed": c.crossed,
                "lag_seconds": c.lag_seconds,
                "crossed_at": c.crossed_at.isoformat() if c.crossed_at else None,
            }
        )

    drift_by = {}
    for d in drifts:
        drift_by.setdefault(d.lag_measurement_id, []).append(
            {
                "direction": d.direction,
                "window_minutes": d.window_minutes,
                "observed_price": d.observed_price,
                "move_from_p0": d.move_from_p0,
                "observed_at": d.observed_at.isoformat() if d.observed_at else None,
            }
        )

    out = []
    for lm in lms:
        out.append(
            {
                "id": lm.id,
                "signal_id": lm.signal_id,
                "market_id": lm.market_id,
                "signal_time": lm.signal_time.isoformat(),
                "implied_outcome": lm.implied_outcome,
                "category": lm.category,
                "source_tier": lm.source_tier,
                "source_name": lm.source_name,
                "confidence": lm.confidence,
                "verifier_confidence": lm.verifier_confidence,
                "p0": lm.p0,
                "yes_mid_at_signal": lm.yes_mid_at_signal,
                "spread_at_signal": lm.spread_at_signal,
                "liquidity_at_signal": lm.liquidity_at_signal,
                "sufficient_liquidity": lm.sufficient_liquidity,
                "spread_ok": lm.spread_ok,
                "price_within_range": lm.price_within_range,
                "clean_signal": lm.clean_signal,
                "stale_signal": lm.stale_signal,
                "leaky_signal": lm.leaky_signal,
                "price_lag_status": lm.price_lag_status,
                "closure_lag_seconds": lm.closure_lag_seconds,
                "hard_source_lag_seconds": lm.hard_source_lag_seconds,
                "soft_to_hard_source_lag_seconds": lm.soft_to_hard_source_lag_seconds,
                "crossings": cross_by.get(lm.id, []),
                "drift_windows": drift_by.get(lm.id, []),
                "created_at": lm.created_at.isoformat(),
            }
        )
    return out
