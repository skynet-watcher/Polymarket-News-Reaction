from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal, engine, get_session
from app.job_status import run_tracked_background_job
from app.init_db import init_db
from app.live_feeds import ensure_live_news_sources
from app.models import NewsSource, RuntimeSetting
from app.threshold_context import DEFAULT_PROFILE_ID, RUNTIME_KEY_THRESHOLD_PROFILE
from app.threshold_profiles_seed import ensure_default_threshold_profiles
from app import background_loops
from app.jobs import sync_markets, settle_trades
from app.realtime_policy import invested_hours_to_resolution, next_snapshot_tick_sleep_seconds
from app.settings import settings
from app.routers import api, ui
from app.util import format_lag_seconds

logger = logging.getLogger(__name__)

# Monotonic anchor for full Gamma sync vs CLOB-only refresh (see _snapshot_once).
_snapshot_last_full: list[float] = [0.0]


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
            except Exception:
                logger.exception("background snapshot loop failed")
            await asyncio.sleep(sleep_s)

    asyncio.create_task(_snapshot_loop())

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
    return {"ok": "true"}


app.include_router(api.router, prefix="/api")
app.include_router(ui.router)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

