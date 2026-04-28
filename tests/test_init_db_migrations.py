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
                backtest_run_cols = {
                    row[1] for row in (await conn.execute(text("PRAGMA table_info(backtest_runs)"))).fetchall()
                }
                backtest_case_cols = {
                    row[1] for row in (await conn.execute(text("PRAGMA table_info(backtest_cases)"))).fetchall()
                }
                trade_cols = {
                    row[1] for row in (await conn.execute(text("PRAGMA table_info(paper_trades)"))).fetchall()
                }

            assert {"market_type", "is_control_market", "manipulation_risk_flag", "is_fixture"}.issubset(market_cols)
            assert "signal_source_type" in signal_cols
            assert {"job_name", "status", "started_at", "last_success_at", "last_duration_ms", "last_error"}.issubset(job_status_cols)
            assert {"id", "started_at", "status", "params_json", "summary_json"}.issubset(backtest_run_cols)
            assert {
                "id",
                "run_id",
                "article_id",
                "market_id",
                "hours_to_resolution",
                "signal_action",
                "price_windows_json",
                "coverage_status",
            }.issubset(backtest_case_cols)
            assert {"trade_source", "backtest_case_id"}.issubset(trade_cols)
        finally:
            await engine.dispose()

    asyncio.run(_run())
