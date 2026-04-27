from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.settings import settings

logger = logging.getLogger(__name__)


def _engine_kwargs() -> dict:
    kw: dict = {"echo": False}
    # SQLite: longer busy wait + WAL reduces "database is locked" when the snapshot loop
    # and manual "Sync markets" run at the same time.
    if "sqlite" in settings.database_url:
        kw["connect_args"] = {"timeout": 60.0}
    return kw


engine: AsyncEngine = create_async_engine(settings.database_url, **_engine_kwargs())

if "sqlite" in settings.database_url:

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragma(dbapi_connection, connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.close()

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    """
    Request-scoped session. On client disconnect or ASGI cancel (common during long jobs),
    the SQLite connection may already be torn down; closing the session then raises
    OperationalError / \"no active connection\". Those are benign — log at debug only.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        try:
            await session.close()
        except Exception:
            logger.debug("AsyncSession.close skipped error (often client disconnect)", exc_info=True)

