from __future__ import annotations

from app import db


def test_resolve_database_url_uses_vercel_postgres_fallback(monkeypatch) -> None:
    monkeypatch.setattr(db.settings, "database_url", "sqlite+aiosqlite:///./data.db")
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("POSTGRES_URL_NON_POOLING", "postgres://u:p@host/db?sslmode=require&connect_timeout=10")

    url, needs_ssl = db._resolve_database_url()

    assert url == "postgresql+asyncpg://u:p@host/db?connect_timeout=10"
    assert needs_ssl is True


def test_resolve_database_url_preserves_explicit_database_url(monkeypatch) -> None:
    monkeypatch.setattr(db.settings, "database_url", "sqlite+aiosqlite:///./local.db")
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("POSTGRES_URL_NON_POOLING", "postgres://u:p@host/db?sslmode=require")

    url, needs_ssl = db._resolve_database_url()

    assert url == "sqlite+aiosqlite:///./local.db"
    assert needs_ssl is False
