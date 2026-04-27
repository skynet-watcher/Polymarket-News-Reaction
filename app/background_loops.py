"""
Best-effort asyncio loops for hands-off ingestion and research jobs.

Intervals come from ``app.settings``; ``0`` disables a loop. Failures are logged
and the loop continues (same pattern as the existing snapshot task).
"""

from __future__ import annotations

import asyncio
import logging

from app.db import SessionLocal
from app.job_status import run_tracked_job
from app.jobs import compute_lag, lag_rank, poll_news, process_candidates, signal_metrics
from app.realtime_policy import invested_hours_to_resolution, next_poll_news_sleep_seconds, next_process_candidates_sleep_seconds

logger = logging.getLogger(__name__)


async def _sleep_stagger(base_seconds: float, jitter_seconds: float) -> None:
    await asyncio.sleep(max(5.0, base_seconds + jitter_seconds))


async def run_poll_news_loop(interval_seconds: int) -> None:
    """Fetch RSS on a fixed interval (first run after a short stagger)."""
    await _sleep_stagger(12.0, 0.0)
    while True:
        sleep_s = max(60, interval_seconds)
        try:
            async with SessionLocal() as session:
                out = await run_tracked_job(session, "poll_news", lambda: poll_news.run(session))
                logger.info("background poll_news: %s", out)
            async with SessionLocal() as session:
                has_open, hours = await invested_hours_to_resolution(session)
                sleep_s = next_poll_news_sleep_seconds(base_seconds=interval_seconds, has_open=has_open, hours=hours)
        except Exception:
            logger.exception("background poll_news failed")
        await asyncio.sleep(max(30, sleep_s))


async def run_process_candidates_loop(interval_seconds: int) -> None:
    """Match / interpret / gate / paper-trade on a fixed interval."""
    await _sleep_stagger(40.0, 0.0)
    while True:
        sleep_s = max(60, interval_seconds)
        try:
            async with SessionLocal() as session:
                out = await run_tracked_job(session, "process_candidates", lambda: process_candidates.run(session))
                logger.info("background process_candidates: %s", out)
            async with SessionLocal() as session:
                has_open, hours = await invested_hours_to_resolution(session)
                sleep_s = next_process_candidates_sleep_seconds(
                    base_seconds=interval_seconds, has_open=has_open, hours=hours
                )
        except Exception:
            logger.exception("background process_candidates failed")
        await asyncio.sleep(max(15, sleep_s))


async def run_lag_pipeline_loop(interval_seconds: int) -> None:
    """Backfill lag measurements, signal metrics, and lag ranks (heavy)."""
    await _sleep_stagger(90.0, 0.0)
    while True:
        try:
            async with SessionLocal() as session:
                lag_out = await run_tracked_job(session, "lag_backfill", lambda: compute_lag.run_backfill(session))
            async with SessionLocal() as session:
                met_out = await run_tracked_job(
                    session, "signal_metrics", lambda: signal_metrics.run_backfill(session, limit=200)
                )
            async with SessionLocal() as session:
                rank_out = await run_tracked_job(session, "lag_ranks", lambda: lag_rank.run(session))
            logger.info(
                "background lag pipeline: lag=%s metrics=%s ranks=%s",
                lag_out,
                met_out,
                rank_out,
            )
        except Exception:
            logger.exception("background lag pipeline failed")
        await asyncio.sleep(max(300, interval_seconds))
