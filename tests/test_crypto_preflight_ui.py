from __future__ import annotations

import asyncio
import datetime as dt

from starlette.requests import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base, JobStatus
from app.routers.ui import crypto_preflight_page


def _request(path: str = "/analysis/crypto-preflight") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
            "client": ("testclient", 50000),
        }
    )


def test_crypto_preflight_empty_state_uses_successful_zero_result_scan() -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        try:
            async with Session() as session:
                session.add(
                    JobStatus(
                        job_name="crypto_preflight",
                        label="Crypto preflight",
                        status="SUCCESS",
                        last_success_at=dt.datetime(2026, 5, 8, 17, 19, tzinfo=dt.timezone.utc),
                    )
                )
                await session.commit()

                response = await crypto_preflight_page(_request(), session)
                body = response.body.decode("utf-8")

                assert "Last scan: 2026-05-08 17:19 UTC" in body
                assert "No matching crypto Up/Down markets found" in body
                assert "No markets scanned yet" not in body
        finally:
            await engine.dispose()

    asyncio.run(_run())
