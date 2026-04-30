from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.settings import settings

logger = logging.getLogger(__name__)


import re as _re


def _resolve_database_url() -> tuple[str, bool]:
    """
    Normalise the database URL for the correct async driver.

    Returns (url, needs_ssl) where needs_ssl=True means the original URL
    contained sslmode=require (Vercel Postgres always does).

    Problems solved:
    1. Vercel Postgres gives  postgres://  — SQLAlchemy asyncpg needs
       postgresql+asyncpg://
    2. asyncpg does NOT accept the libpq  ?sslmode=require  query param —
       it uses a Python ssl object passed via connect_args.  We strip
       sslmode from the URL and pass ssl='require' in connect_args instead.
    """
    url = settings.database_url

    # Step 1: fix driver prefix
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+asyncpg" not in url:
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]

    # Step 2: strip sslmode (asyncpg rejects it), record whether SSL is needed
    needs_ssl = bool(_re.search(r"[?&]sslmode=require", url, _re.I))
    url = _re.sub(r"[?&]sslmode=[^&]*", "", url)
    url = url.rstrip("?&")

    return url, needs_ssl


_DATABASE_URL, _POSTGRES_SSL = _resolve_database_url()


def _engine_kwargs() -> dict:
    kw: dict = {"echo": False}
    if "sqlite" in _DATABASE_URL:
        # SQLite: longer busy wait + WAL reduces "database is locked" when the
        # snapshot loop and manual "Sync markets" run at the same time.
        kw["connect_args"] = {"timeout": 60.0}
    else:
        # Postgres / asyncpg: single connection per Lambda invocation avoids
        # exhausting Vercel Postgres connection limits.  pool_pre_ping detects
        # stale connections on reuse (important after Lambda warm-start gaps).
        kw["pool_size"] = 1
        kw["max_overflow"] = 0
        kw["pool_pre_ping"] = True
        if _POSTGRES_SSL:
            # Pass ssl as a connect_arg — asyncpg understands this, not sslmode
            kw["connect_args"] = {"ssl": "require"}
    return kw


engine: AsyncEngine = create_async_engine(_DATABASE_URL, **_engine_kwargs())

if "sqlite" in _DATABASE_URL:  # SQLite-only pragma hook — skipped on Postgres

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

