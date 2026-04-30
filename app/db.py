from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.settings import settings

logger = logging.getLogger(__name__)


def _resolve_database_url() -> tuple[str, bool]:
    """
    Normalise the database URL for the correct async driver.

    Returns (url, needs_ssl) where needs_ssl=True means the original URL asked
    for SSL via sslmode=require/verify-ca/verify-full.

    Problems solved:
    1. Vercel Postgres gives  postgres://  — SQLAlchemy asyncpg needs
       postgresql+asyncpg://
    2. Keep non-sslmode query params intact while moving sslmode into
       connect_args. Regex stripping can corrupt URLs when sslmode is the first
       of several query params.
    """
    url = settings.database_url

    # Step 1: fix driver prefix
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+asyncpg" not in url:
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]

    # Only strip sslmode from Postgres URLs.  urlunsplit cannot round-trip
    # SQLite URLs because it drops one of the three leading slashes
    # (sqlite+aiosqlite:///./data.db → sqlite+aiosqlite:/./data.db).
    if url.startswith("sqlite"):
        return url, False

    parts = urlsplit(url)
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept_pairs: list[tuple[str, str]] = []
    sslmode = ""
    for key, value in query_pairs:
        if key.lower() == "sslmode":
            sslmode = value.lower()
        else:
            kept_pairs.append((key, value))

    needs_ssl = sslmode in {"require", "verify-ca", "verify-full"}
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept_pairs), parts.fragment))

    return url, needs_ssl


_DATABASE_URL, _POSTGRES_SSL = _resolve_database_url()


def _engine_kwargs() -> dict:
    kw: dict = {"echo": False}
    if _DATABASE_URL.startswith("sqlite"):
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

if _DATABASE_URL.startswith("sqlite"):  # SQLite-only pragma hook — skipped on Postgres

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
