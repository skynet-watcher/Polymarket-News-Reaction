from __future__ import annotations

from app import db


def test_resolve_database_url_uses_vercel_postgres_fallback(monkeypatch) -> None:
    monkeypatch.setattr(db.settings, "database_url", "sqlite+aiosqlite:///./data.db")
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv(
        "POSTGRES_URL_NON_POOLING",
        "postgres://u:p@host/db?sslmode=require&connect_timeout=10&channel_binding=require",
    )

    url, connect_args = db._resolve_database_url()

    assert url == "postgresql+asyncpg://u:p@host/db"
    assert connect_args == {"ssl": True, "timeout": 10.0}


def test_resolve_database_url_preserves_explicit_database_url(monkeypatch) -> None:
    monkeypatch.setattr(db.settings, "database_url", "sqlite+aiosqlite:///./local.db")
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("POSTGRES_URL_NON_POOLING", "postgres://u:p@host/db?sslmode=require")

    url, connect_args = db._resolve_database_url()

    assert url == "sqlite+aiosqlite:///./local.db"
    assert connect_args == {}
