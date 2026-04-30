from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal, database_runtime_summary, engine, get_session
from app.job_status import run_tracked_background_job
from app.init_db import init_db
from app.live_feeds import ensure_live_news_sources
from app.models import NewsSource, RuntimeSetting
from app.threshold_context import DEFAULT_PROFILE_ID, RUNTIME_KEY_THRESHOLD_PROFILE
from app.threshold_profiles_seed import ensure_default_threshold_profiles
from app import background_loops
from app.jobs import sync_markets, settle_trades, poll_news, process_candidates
from app.realtime_policy import invested_hours_to_resolution, next_snapshot_tick_sleep_seconds
from app.settings import settings
from app.routers import api, crons, ui
from app.util import format_lag_seconds

logger = logging.getLogger(__name__)

# Monotonic anchor for full Gamma sync vs CLOB-only refresh (see _snapshot_once).
_snapshot_last_full: list[float] = [0.0]
_snapshot_loop_tick: list[int] = [0]
STARTUP_STATE: dict[str, str] = {"status": "not_started", "error_type": "", "error": ""}


async def _snapshot_once(session: AsyncSession) -> dict[str, Any]:
    has_open, hours = await invested_hours_to_resolution(session)
    sleep_s = next_snapshot_tick_sleep_seconds(
        base_seconds=max(5, settings.snapshot_interval_seconds),
        has_open=has_open,
        hours=hours,
    )
    now_m = time.monotonic()
    need_full = (now_m - _snapshot_last_full[0]) >= max(5.0, float(settings.snapshot_interval_seconds))
    if need_full:
        out = await sync_markets.run(session)
        _snapshot_last_full[0] = time.monotonic()
    else:
        out = await sync_markets.refresh_open_position_markets(session)
    merged: dict[str, Any] = dict(out)
    merged["sleep_s"] = sleep_s
    return merged


app = FastAPI(title="Polymarket News-Reaction (Paper MVP)")

templates = Jinja2Templates(directory="app/templates")

templates.env.filters["format_lag"] = format_lag_seconds


@app.on_event("startup")
async def _startup() -> None:
    _on_vercel = bool(os.environ.get("VERCEL"))

    async def _one_time_init() -> None:
        await init_db(engine)
        async for session in get_session():
            if settings.auto_seed_news_feeds:
                await ensure_live_news_sources(session)
            await ensure_default_threshold_profiles(session)
            prof_row = await session.get(RuntimeSetting, RUNTIME_KEY_THRESHOLD_PROFILE)
            if prof_row is None:
                session.add(RuntimeSetting(key=RUNTIME_KEY_THRESHOLD_PROFILE, value=DEFAULT_PROFILE_ID))
                await session.commit()
            cnt = (await session.execute(select(func.count()).select_from(NewsSource))).scalar_one()
            # Demo source only if nothing configured (offline / first run without seed).
            if cnt == 0:
                session.add(
                    NewsSource(
                        name="Demo Wire (fixture)",
                        domain="demo-wire.example",
                        rss_url="https://demo-wire.example/rss",
                        source_tier="SOFT",
                        polling_interval_minutes=5,
                        active=True,
                    )
                )
                await session.commit()
            break

    try:
        STARTUP_STATE.update({"status": "running", "error_type": "", "error": ""})
        await _one_time_init()
        STARTUP_STATE.update({"status": "ok", "error_type": "", "error": ""})
    except Exception as exc:
        STARTUP_STATE.update(
            {
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            }
        )
        logger.exception("startup one-time init failed")
        if not _on_vercel:
            raise

    async def _snapshot_loop() -> None:
        # Full Gamma sync on `snapshot_interval_seconds`, with faster CLOB-only refresh for
        # markets that have OPEN paper trades when resolution is near (see realtime_policy).
        while True:
            sleep_s = float(max(5, settings.snapshot_interval_seconds))
            try:
                result = await run_tracked_background_job(
                    "sync_markets",
                    _snapshot_once,
                    session_factory=SessionLocal,
                )
                sleep_s = float(result.get("sleep_s", sleep_s))
                _snapshot_loop_tick[0] += 1
                n = settings.snapshot_loop_log_every_n_ticks
                if n > 0 and _snapshot_loop_tick[0] % n == 0:
                    logger.info(
                        "snapshot_loop heartbeat tick=%s next_sleep_s=%.1f full_sync_age_s=%.1f",
                        _snapshot_loop_tick[0],
                        sleep_s,
                        time.monotonic() - _snapshot_last_full[0],
                    )
            except Exception:
                logger.exception("background snapshot loop failed")
            await asyncio.sleep(sleep_s)

    # On Vercel (serverless) background asyncio tasks are pointless — the process
    # exits after each request.  Scheduling is handled by Vercel Cron + the
    # /api/cron/* endpoints instead.
    if _on_vercel:
        logger.info("startup: running on Vercel — background loops disabled, cron endpoints active")
        return

    asyncio.create_task(_snapshot_loop())

    async def _warm_start() -> None:
        """Run each job once at startup so Eric never has to click a button.
        Waits 5s for the DB/snapshot loop to settle, then sequences:
        markets → news → candidates → settle."""
        await asyncio.sleep(5)
        for job_name, job_fn in [
            ("sync_markets", sync_markets.run),
            ("poll_news", poll_news.run),
            ("process_candidates", process_candidates.run),
            ("settle_trades", settle_trades.run),
        ]:
            try:
                await run_tracked_background_job(job_name, job_fn, session_factory=SessionLocal)
                logger.info("warm_start: %s done", job_name)
            except Exception:
                logger.exception("warm_start: %s failed", job_name)

    asyncio.create_task(_warm_start())

    async def _settle_loop() -> None:
        while True:
            await asyncio.sleep(max(60, settings.background_settle_interval_seconds))
            try:
                await run_tracked_background_job(
                    "settle_trades",
                    settle_trades.run,
                    session_factory=SessionLocal,
                )
            except Exception:
                logger.exception("background settle loop failed")

    asyncio.create_task(_settle_loop())

    if settings.background_poll_news_interval_seconds > 0:
        asyncio.create_task(
            background_loops.run_poll_news_loop(settings.background_poll_news_interval_seconds)
        )
    if settings.background_process_candidates_interval_seconds > 0:
        asyncio.create_task(
            background_loops.run_process_candidates_loop(
                settings.background_process_candidates_interval_seconds
            )
        )
    if settings.background_lag_pipeline_interval_seconds > 0:
        asyncio.create_task(
            background_loops.run_lag_pipeline_loop(settings.background_lag_pipeline_interval_seconds)
        )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    is_vercel = bool(os.environ.get("VERCEL"))
    return {
        "ok": "true",
        "app": "polymarket-news-reaction",
        "build_marker": "fastapi-main-vercel",
        "runtime": "vercel" if is_vercel else "local",
        "vercel_env": os.environ.get("VERCEL_ENV", ""),
        "git_branch": os.environ.get("VERCEL_GIT_COMMIT_REF", "main"),
        "git_commit": os.environ.get("VERCEL_GIT_COMMIT_SHA", ""),
        "git_repo": "/".join(
            part
            for part in (
                os.environ.get("VERCEL_GIT_REPO_OWNER", ""),
                os.environ.get("VERCEL_GIT_REPO_SLUG", ""),
            )
            if part
        ),
        "deployment_url": os.environ.get("VERCEL_URL", ""),
        "production_url": os.environ.get("VERCEL_PROJECT_PRODUCTION_URL", ""),
        "startup_status": STARTUP_STATE["status"],
        "startup_error_type": STARTUP_STATE["error_type"],
        "startup_error": STARTUP_STATE["error"],
        **database_runtime_summary(),
    }


app.include_router(api.router, prefix="/api")
app.include_router(crons.router, prefix="/api")
app.include_router(ui.router)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
