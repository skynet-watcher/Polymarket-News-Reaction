"""
Regression: closed-market Gamma fetch is merged and `winning_outcome` is persisted.

Without the closed=true pass, resolved markets never refresh after they drop off the open feed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.jobs import sync_markets
from app.models import Base, Market, RuntimeSetting
from app.settings import settings

# Patching `app.jobs.sync_markets.httpx.AsyncClient` mutates `httpx.AsyncClient` on the
# shared httpx module — capture the real class before any patch for use in factories.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _gamma_handler_for_merge() -> httpx.MockTransport:
    """Same id in open + closed feeds; closed row must win (resolution fields)."""

    open_row = {
        "id": "m_merge",
        "question": "Will it rain?",
        "outcomes": ["Yes", "No"],
        "active": True,
        "closed": False,
    }
    closed_row = {
        "id": "m_merge",
        "question": "Will it rain?",
        "outcomes": ["Yes", "No"],
        "active": False,
        "closed": True,
        "winner": "YES",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if settings.polymarket_gamma_base_url not in url or "/markets" not in url:
            return httpx.Response(404, json={})
        qp = request.url.params
        if qp.get("closed") == "true":
            return httpx.Response(200, json=[closed_row])
        if qp.get("active") == "true" and qp.get("closed") == "false":
            return httpx.Response(200, json=[open_row])
        return httpx.Response(200, json=[])

    return httpx.MockTransport(handler)


def _gamma_handler_closed_only_and_clob_404() -> httpx.MockTransport:
    """Events-first sync: open /events empty; closed /events returns nested resolved market."""

    closed_row = {
        "id": "m_resolved_only",
        "question": "Resolved question?",
        "outcomes": ["Yes", "No"],
        "active": False,
        "closed": True,
        "winner": "NO",
        "tokenIds": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if settings.polymarket_clob_base_url in url:
            return httpx.Response(404, json={})
        if settings.polymarket_gamma_base_url not in url:
            return httpx.Response(404, json={})
        qp = request.url.params
        if "/events" in url:
            if qp.get("closed") == "true":
                return httpx.Response(200, json=[{"id": "evt_closed", "markets": [closed_row]}])
            return httpx.Response(200, json=[])
        if "/markets" in url:
            if qp.get("closed") == "true":
                return httpx.Response(200, json=[closed_row])
            if qp.get("active") == "true" and qp.get("closed") == "false":
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


def test_fetch_gamma_markets_fallback_prefers_closed_payload_for_shared_id():
    async def _run() -> None:
        transport = _gamma_handler_for_merge()
        async with httpx.AsyncClient(transport=transport) as client:
            merged = await sync_markets._fetch_gamma_open_and_closed_markets_fallback(client, limit=200)
        assert len(merged) == 1
        assert merged[0]["id"] == "m_merge"
        assert merged[0]["closed"] is True
        assert merged[0].get("winner") == "YES"

    asyncio.run(_run())


def test_sync_run_persists_winning_outcome_from_closed_fetch():
    async def _run() -> None:
        transport = _gamma_handler_closed_only_and_clob_404()

        def client_with_transport(**kwargs):
            kw = dict(kwargs)
            kw["transport"] = transport
            return _REAL_ASYNC_CLIENT(**kw)

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        session = factory()
        try:
            with patch("app.jobs.sync_markets.polymarket_async_client", client_with_transport):
                out = await sync_markets.run(session)
            assert out["upserted"] >= 1
            assert out.get("markets_source") == "live"
            src = await session.get(RuntimeSetting, "sync_markets_data_source")
            assert src is not None and src.value == "live"
            row = (await session.execute(select(Market).where(Market.id == "m_resolved_only"))).scalar_one()
            assert row.closed is True
            assert row.winning_outcome == "NO"
            assert row.active is False
        finally:
            await session.close()
            await engine.dispose()

    asyncio.run(_run())


def test_sync_run_updates_existing_row_when_resolution_arrives_on_closed_feed():
    """Market previously synced as open; later only appears on closed=true with winner."""

    async def _run() -> None:
        transport = _gamma_handler_closed_only_and_clob_404()

        def client_with_transport(**kwargs):
            kw = dict(kwargs)
            kw["transport"] = transport
            return _REAL_ASYNC_CLIENT(**kw)

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        session = factory()
        try:
            session.add(
                Market(
                    id="m_resolved_only",
                    question="Old title",
                    outcomes_json=["Yes", "No"],
                    active=True,
                    closed=False,
                    winning_outcome=None,
                    best_bid_yes=0.5,
                    best_ask_yes=0.52,
                )
            )
            await session.commit()

            with patch("app.jobs.sync_markets.polymarket_async_client", client_with_transport):
                await sync_markets.run(session)

            row = (await session.execute(select(Market).where(Market.id == "m_resolved_only"))).scalar_one()
            assert row.winning_outcome == "NO"
            assert row.closed is True
        finally:
            await session.close()
            await engine.dispose()

    asyncio.run(_run())


def test_sync_run_falls_back_to_fixture_when_gamma_returns_http_error():
    """403/5xx from Gamma must not crash sync; fixture markets keep the UI/jobs working."""

    def handler(request: httpx.Request) -> httpx.Response:
        if settings.polymarket_gamma_base_url in str(request.url):
            return httpx.Response(403, json={"error": "forbidden"})
        return httpx.Response(404, json={})

    async def _run() -> None:
        transport = httpx.MockTransport(handler)

        def client_with_transport(**kwargs):
            kw = dict(kwargs)
            kw["transport"] = transport
            return _REAL_ASYNC_CLIENT(**kw)

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        session = factory()
        try:
            with patch("app.jobs.sync_markets.polymarket_async_client", client_with_transport):
                out = await sync_markets.run(session)
            assert out["fetched"] >= 1
            assert out["upserted"] >= 1
            assert out.get("markets_source") == "fixture"
            src = await session.get(RuntimeSetting, "sync_markets_data_source")
            assert src is not None and src.value == "fixture"
            demo = (await session.execute(select(Market).where(Market.id == "demo_mkt_1"))).scalar_one_or_none()
            assert demo is not None
        finally:
            await session.close()
            await engine.dispose()

    asyncio.run(_run())
