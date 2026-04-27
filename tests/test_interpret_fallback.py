from __future__ import annotations

import asyncio
import datetime as dt

import httpx

from app.core import interpret
from app.models import Market, NewsArticle


def test_interpret_and_verify_falls_back_when_llm_call_fails(monkeypatch) -> None:
    async def _boom(*args, **kwargs):
        request = httpx.Request("POST", "https://example.test/responses")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    async def _run() -> None:
        monkeypatch.setattr(interpret.settings, "openai_api_key", "test-key")
        monkeypatch.setattr(interpret, "_openai_interpret", _boom)

        market = Market(id="m1", question="Will Candidate win?", outcomes_json=["Yes", "No"])
        article = NewsArticle(
            id="a1",
            source_id=1,
            source_domain="example.test",
            source_tier="SOFT",
            url="https://example.test/a1",
            title="Official results announced",
            body="Official results were announced.",
            published_at=dt.datetime.now(dt.timezone.utc),
            content_hash="hash",
        )

        interp, ver = await interpret.interpret_and_verify(market, article)
        assert interp["interpreted_outcome"] == "UNKNOWN"
        assert "LLM_CALL_FAILED" in ver["risk_flags"]
        assert ver["should_block_trade"] is True

    asyncio.run(_run())
