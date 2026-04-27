from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.init_db import init_db


def test_init_db_backfills_recent_sqlite_columns() -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        """
                        CREATE TABLE markets (
                          id VARCHAR PRIMARY KEY,
                          question TEXT NOT NULL,
                          outcomes_json JSON NOT NULL
                        )
                        """
                    )
                )
                await conn.execute(
                    text(
                        """
                        CREATE TABLE news_signals (
                          id VARCHAR PRIMARY KEY,
                          market_id VARCHAR NOT NULL,
                          article_id VARCHAR NOT NULL
                        )
                        """
                    )
                )

            await init_db(engine)

            async with engine.connect() as conn:
                market_cols = {
                    row[1] for row in (await conn.execute(text("PRAGMA table_info(markets)"))).fetchall()
                }
                signal_cols = {
                    row[1] for row in (await conn.execute(text("PRAGMA table_info(news_signals)"))).fetchall()
                }
                job_status_cols = {
                    row[1] for row in (await conn.execute(text("PRAGMA table_info(job_statuses)"))).fetchall()
                }

            assert {"market_type", "is_control_market", "manipulation_risk_flag"}.issubset(market_cols)
            assert "signal_source_type" in signal_cols
            assert {"job_name", "status", "started_at", "last_success_at", "last_duration_ms", "last_error"}.issubset(job_status_cols)
        finally:
            await engine.dispose()

    asyncio.run(_run())
