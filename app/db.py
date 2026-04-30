from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.settings import settings

logger = logging.getLogger(__name__)


def _configured_database_url() -> str:
    """
    Resolve DB URL across local and Vercel naming conventions.

    Local/dev uses DATABASE_URL via Settings. Vercel Postgres commonly injects
    POSTGRES_URL_* variables when a database is connected to the project, and
    may not provide DATABASE_URL unless the operator adds it manually.
    """
    configured = (settings.database_url or "").strip()
    if configured and configured != "sqlite+aiosqlite:///./data.db":
        return configured

    if os.environ.get("VERCEL"):
        for key in ("POSTGRES_URL_NON_POOLING", "POSTGRES_URL", "POSTGRES_PRISMA_URL"):
            value = (os.environ.get(key) or "").strip()
            if value:
                return value

    return configured or "sqlite+aiosqlite:///./data.db"


def _resolve_database_url() -> tuple[str, dict[str, Any]]:
    """
    Normalise the database URL for the correct async driver.

    Returns (url, connect_args). Provider DSNs often include libpq query params;
    asyncpg wants the important ones as keyword arguments instead.

    Problems solved:
    1. Vercel Postgres gives  postgres://  — SQLAlchemy asyncpg needs
       postgresql+asyncpg://
    2. Keep non-sslmode query params intact while moving sslmode into
       connect_args. Regex stripping can corrupt URLs when sslmode is the first
       of several query params.
    """
    url = _configured_database_url()

    # Step 1: fix driver prefix
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+asyncpg" not in url:
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]

    # Only strip sslmode from Postgres URLs.  urlunsplit cannot round-trip
    # SQLite URLs because it drops one of the three leading slashes
    # (sqlite+aiosqlite:///./data.db → sqlite+aiosqlite:/./data.db).
    if url.startswith("sqlite"):
        return url, {}

    parts = urlsplit(url)
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept_pairs: list[tuple[str, str]] = []
    connect_args: dict[str, Any] = {}
    for key, value in query_pairs:
        key_lower = key.lower()
        if key_lower == "sslmode":
            sslmode = value.lower()
            if sslmode in {"require", "verify-ca", "verify-full"}:
                # asyncpg 0.30 expects bool or SSLContext here, not the libpq
                # string "require".
                connect_args["ssl"] = True
            elif sslmode == "disable":
                connect_args["ssl"] = False
        elif key_lower == "connect_timeout":
            try:
                connect_args["timeout"] = float(value)
            except ValueError:
                logger.warning("Ignoring invalid Postgres connect_timeout=%r", value)
        elif key_lower == "channel_binding":
            # Some managed Postgres URLs include libpq's channel_binding option;
            # asyncpg.connect has no matching kwarg, so passing it through makes
            # Lambda cold starts fail before the app can serve diagnostics.
            continue
        else:
            kept_pairs.append((key, value))

    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept_pairs), parts.fragment))

    return url, connect_args


_DATABASE_URL, _POSTGRES_CONNECT_ARGS = _resolve_database_url()


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
        connect_args = dict(_POSTGRES_CONNECT_ARGS)
        if os.environ.get("VERCEL") and "timeout" not in connect_args:
            # Fail fast enough that /healthz can report the problem instead of
            # the whole serverless invocation hanging until Vercel kills it.
            connect_args["timeout"] = 5.0
        if connect_args:
            kw["connect_args"] = connect_args
    return kw


def database_runtime_summary() -> dict[str, str]:
    """Safe, non-secret DB diagnostics for /healthz."""
    return {
        "database_scheme": urlsplit(_DATABASE_URL).scheme,
        "database_is_postgres": str(_DATABASE_URL.startswith("postgresql+asyncpg")).lower(),
        "database_ssl": str(bool(_POSTGRES_CONNECT_ARGS.get("ssl"))).lower(),
        "database_timeout_s": str(_engine_kwargs().get("connect_args", {}).get("timeout", "")),
    }


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
