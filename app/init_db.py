from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy import text

from app.models import Base


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Minimal migration support for SQLite: add new columns if missing.
        # (SQLAlchemy create_all does not alter existing tables.)
        await _ensure_column(conn, table="price_snapshots", column="spread", ddl="ALTER TABLE price_snapshots ADD COLUMN spread FLOAT")
        await _ensure_column(conn, table="price_snapshots", column="volume_24h", ddl="ALTER TABLE price_snapshots ADD COLUMN volume_24h FLOAT")
        await _ensure_column(conn, table="lag_measurements", column="eventual_move", ddl="ALTER TABLE lag_measurements ADD COLUMN eventual_move FLOAT")
        await _ensure_column(conn, table="markets", column="winning_outcome", ddl="ALTER TABLE markets ADD COLUMN winning_outcome VARCHAR")
        await _ensure_column(conn, table="lag_measurements", column="signal_correct", ddl="ALTER TABLE lag_measurements ADD COLUMN signal_correct BOOLEAN")
        await _ensure_column(conn, table="markets", column="resolution_source_text", ddl="ALTER TABLE markets ADD COLUMN resolution_source_text TEXT")
        await _ensure_column(conn, table="markets", column="rules_text", ddl="ALTER TABLE markets ADD COLUMN rules_text TEXT")
        await _ensure_column(conn, table="markets", column="enable_orderbook", ddl="ALTER TABLE markets ADD COLUMN enable_orderbook BOOLEAN DEFAULT 1")
        await _ensure_column(conn, table="markets", column="volume_24h", ddl="ALTER TABLE markets ADD COLUMN volume_24h FLOAT")
        await _ensure_column(conn, table="markets", column="market_type", ddl="ALTER TABLE markets ADD COLUMN market_type VARCHAR")
        await _ensure_column(
            conn, table="markets", column="is_control_market", ddl="ALTER TABLE markets ADD COLUMN is_control_market BOOLEAN DEFAULT 0"
        )
        await _ensure_column(
            conn,
            table="markets",
            column="manipulation_risk_flag",
            ddl="ALTER TABLE markets ADD COLUMN manipulation_risk_flag BOOLEAN DEFAULT 0",
        )
        await _ensure_column(
            conn, table="news_signals", column="signal_source_type", ddl="ALTER TABLE news_signals ADD COLUMN signal_source_type VARCHAR"
        )
        await _ensure_column(
            conn, table="paper_trades", column="execution_context_json", ddl="ALTER TABLE paper_trades ADD COLUMN execution_context_json JSON"
        )
        await _ensure_column(
            conn, table="job_statuses", column="last_duration_ms", ddl="ALTER TABLE job_statuses ADD COLUMN last_duration_ms INTEGER"
        )

        # SQLite cannot alter column nullability; if the lag_threshold_crossings.threshold_value
        # column was created as NOT NULL in an older run, rebuild the table once.
        await _sqlite_rebuild_lag_threshold_crossings_if_needed(conn)


async def _ensure_column(conn, *, table: str, column: str, ddl: str) -> None:
    try:
        res = await conn.execute(text(f"PRAGMA table_info({table})"))
        cols = {row[1] for row in res.fetchall()}
        if column not in cols:
            await conn.execute(text(ddl))
    except Exception:
        # Best-effort; on non-sqlite or unexpected states, skip.
        return


async def _sqlite_rebuild_lag_threshold_crossings_if_needed(conn) -> None:
    """
    Rebuild lag_threshold_crossings if threshold_value is NOT NULL (old schema).
    This enables storing NULL threshold_value when eventual thresholds aren't defined.
    """
    try:
        res = await conn.execute(text("PRAGMA table_info(lag_threshold_crossings)"))
        rows = res.fetchall()
        if not rows:
            return
        # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
        threshold_value = next((r for r in rows if r[1] == "threshold_value"), None)
        if threshold_value is None:
            return
        notnull = int(threshold_value[3] or 0)
        if notnull == 0:
            return

        await conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS lag_threshold_crossings_new (
              id VARCHAR PRIMARY KEY,
              lag_measurement_id VARCHAR NOT NULL,
              threshold_type VARCHAR NOT NULL,
              threshold_label VARCHAR NOT NULL,
              threshold_value FLOAT NULL,
              crossed BOOLEAN NOT NULL,
              lag_seconds FLOAT NULL,
              crossed_at DATETIME NULL,
              created_at DATETIME NOT NULL,
              CONSTRAINT uq_crossing_measurement_label UNIQUE (lag_measurement_id, threshold_label),
              FOREIGN KEY(lag_measurement_id) REFERENCES lag_measurements (id)
            )
            """
        ))
        await conn.execute(text(
            """
            INSERT INTO lag_threshold_crossings_new (
              id, lag_measurement_id, threshold_type, threshold_label, threshold_value,
              crossed, lag_seconds, crossed_at, created_at
            )
            SELECT
              id, lag_measurement_id, threshold_type, threshold_label, threshold_value,
              crossed, lag_seconds, crossed_at, created_at
            FROM lag_threshold_crossings
            """
        ))
        await conn.execute(text("DROP TABLE lag_threshold_crossings"))
        await conn.execute(text("ALTER TABLE lag_threshold_crossings_new RENAME TO lag_threshold_crossings"))
    except Exception:
        return
