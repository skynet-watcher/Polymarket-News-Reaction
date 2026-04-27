from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.job_status import build_system_status, run_tracked_job
from app.models import Base, JobStatus, Market, PriceSnapshot
from app.util import format_duration_ms, format_elapsed_since


def test_format_elapsed_since_minutes_seconds() -> None:
    now = dt.datetime(2026, 1, 1, 12, 5, 8, tzinfo=dt.timezone.utc)
    then = dt.datetime(2026, 1, 1, 12, 2, 3, tzinfo=dt.timezone.utc)
    assert format_elapsed_since(then, now=now) == "3m 05s ago"
    assert format_elapsed_since(None, now=now) == "never"
    assert format_duration_ms(65_200) == "1m 05s"
    assert format_duration_ms(None) == "n/a"


def test_tracked_job_records_success_and_failure() -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        try:
            async with Session() as session:
                out = await run_tracked_job(session, "sync_markets", lambda: _ok())
                assert out == {"ok": True}
                row = await session.get(JobStatus, "sync_markets")
                assert row is not None
                assert row.status == "SUCCESS"
                assert row.last_success_at is not None
                assert row.last_duration_ms is not None

            async with Session() as session:
                with pytest.raises(RuntimeError):
                    await run_tracked_job(session, "poll_news", lambda: _bad())
                row = await session.get(JobStatus, "poll_news")
                assert row is not None
                assert row.status == "FAILED"
                assert row.last_duration_ms is not None
                assert "RuntimeError" in (row.last_error or "")
        finally:
            await engine.dispose()

    async def _ok() -> dict:
        return {"ok": True}

    async def _bad() -> dict:
        raise RuntimeError("boom")

    asyncio.run(_run())


def test_build_system_status_marks_running_and_live_data() -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        try:
            async with Session() as session:
                now = dt.datetime.now(dt.timezone.utc)
                session.add(
                    Market(
                        id="m1",
                        question="Q?",
                        outcomes_json=["YES", "NO"],
                        updated_at=now,
                    )
                )
                session.add(
                    PriceSnapshot(
                        id="snap1",
                        market_id="m1",
                        timestamp=now,
                        mid_yes=0.5,
                    )
                )
                session.add(
                    JobStatus(
                        job_name="process_candidates",
                        label="Candidate processing",
                        status="RUNNING",
                        started_at=now,
                        last_duration_ms=61_000,
                    )
                )
                await session.commit()

                rows = await build_system_status(session)
                by_key = {r["key"]: r for r in rows}
                assert by_key["sync_markets"]["color"] == "green"
                assert by_key["process_candidates"]["color"] == "yellow"
                assert by_key["process_candidates"]["duration"].endswith("elapsed")
                assert by_key["lag_ranks"]["color"] == "red"
        finally:
            await engine.dispose()

    asyncio.run(_run())
