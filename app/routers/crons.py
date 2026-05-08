"""
Cron-callable GET endpoints.

Vercel (and external cron services like cron-job.org) call GET requests on a schedule.
These endpoints run the same job functions as the POST /api/jobs/* buttons.

Security: requests must include  Authorization: Bearer <CRON_SECRET>
Set CRON_SECRET in your Vercel environment variables.
Vercel sets this header automatically for its own cron invocations.
For cron-job.org: add a custom header  Authorization: Bearer <your-secret>
If CRON_SECRET is not set in env, the check is skipped (useful during local dev).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.job_status import run_tracked_job
from app.jobs import compute_lag, lag_rank, poll_news, process_candidates, settle_trades, signal_metrics, sync_markets, watchlist_monitor
from app.security import verify_bearer_secret

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/cron/pipeline")
async def cron_pipeline(
    session: AsyncSession = Depends(get_session),
    _: None = Depends(verify_bearer_secret),
) -> JSONResponse:
    """
    Full pipeline in sequence: sync markets → poll news → process candidates → settle.
    Called by Vercel daily cron.  Returns a JSON summary of each step.
    """
    results: dict[str, object] = {}
    for name, fn in [
        ("sync_markets", sync_markets.run),
        ("poll_news", poll_news.run),
        ("process_candidates", process_candidates.run),
        ("settle_trades", settle_trades.run),
    ]:
        try:
            out = await run_tracked_job(session, name, lambda f=fn: f(session))
            results[name] = {"ok": out.get("ok", True)}
        except Exception as exc:
            await session.rollback()
            logger.exception("cron_pipeline: %s failed", name)
            results[name] = {"ok": False, "error": str(exc)}
    return JSONResponse({"ok": True, "steps": results})


@router.get("/cron/settle")
async def cron_settle(
    session: AsyncSession = Depends(get_session),
    _: None = Depends(verify_bearer_secret),
) -> JSONResponse:
    """Settlement-only pass — runs twice a day to catch same-day market resolutions."""
    out = await run_tracked_job(session, "settle_trades", lambda: settle_trades.run(session))
    return JSONResponse(out)


@router.get("/cron/sync")
async def cron_sync(
    session: AsyncSession = Depends(get_session),
    _: None = Depends(verify_bearer_secret),
) -> JSONResponse:
    """Sync markets only — lightweight, safe to call every few minutes from cron-job.org."""
    out = await run_tracked_job(session, "sync_markets", lambda: sync_markets.run(session))
    return JSONResponse(out)


@router.get("/cron/poll")
async def cron_poll(
    session: AsyncSession = Depends(get_session),
    _: None = Depends(verify_bearer_secret),
) -> JSONResponse:
    """Poll news + process candidates — the signal generation half of the pipeline."""
    results: dict[str, object] = {}
    for name, fn in [
        ("poll_news", poll_news.run),
        ("process_candidates", process_candidates.run),
        ("watchlist_monitor", lambda s=session: watchlist_monitor.run(s, max_trades=3)),
    ]:
        try:
            out = await run_tracked_job(session, name, lambda f=fn: f(session))
            results[name] = {"ok": out.get("ok", True)}
        except Exception as exc:
            await session.rollback()
            logger.exception("cron_poll: %s failed", name)
            results[name] = {"ok": False, "error": str(exc)}
    return JSONResponse({"ok": True, "steps": results})


@router.get("/cron/lag-pipeline")
async def cron_lag_pipeline(
    session: AsyncSession = Depends(get_session),
    _: None = Depends(verify_bearer_secret),
) -> JSONResponse:
    """Daily lag analytics: backfill lag measurements → signal metrics → lag ranks."""
    results: dict[str, object] = {}
    for name, fn in [
        ("lag_backfill", lambda s=session: compute_lag.run_backfill(s)),
        ("signal_metrics", lambda s=session: signal_metrics.run_backfill(s, limit=200)),
        ("lag_ranks", lambda s=session: lag_rank.run(s)),
    ]:
        try:
            out = await run_tracked_job(session, name, fn)
            results[name] = {"ok": out.get("ok", True)}
        except Exception as exc:
            await session.rollback()
            logger.exception("cron_lag_pipeline: %s failed", name)
            results[name] = {"ok": False, "error": str(exc)}
    return JSONResponse({"ok": True, "steps": results})
