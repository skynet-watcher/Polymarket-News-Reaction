from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from app.models import Market, NewsArticle
from app.settings import settings


def _fallback_interpret(market: Market, article: NewsArticle) -> dict[str, Any]:
    # Conservative default for MVP: abstain unless trivially resolutive phrases appear.
    text = f"{article.title}\n{article.body}".lower()
    q = market.question.lower()

    direct_markers = [
        "declared the winner",
        "official results",
        "announced",
        "confirms",
        "confirmed",
        "has won",
        "wins",
        "declares",
    ]
    has_direct = any(p in text for p in direct_markers)
    relevance = 0.4 + (0.3 if any(w in text for w in q.split()[:6]) else 0.0)

    if has_direct:
        return {
            "market_relevance": min(0.95, relevance + 0.3),
            "interpreted_outcome": "UNKNOWN",
            "evidence_type": "DIRECT",
            "supporting_excerpt": (article.title or "")[:200],
            "confidence": 0.6,
            "should_act": False,
            "reason": "Rule-based fallback: direct marker found but outcome mapping is uncertain without an LLM.",
        }

    return {
        "market_relevance": relevance,
        "interpreted_outcome": "UNKNOWN",
        "evidence_type": "NONE",
        "supporting_excerpt": None,
        "confidence": 0.1,
        "should_act": False,
        "reason": "Rule-based fallback: insufficient direct evidence.",
    }


def _verifier_after_llm_failure(interpretation: dict[str, Any]) -> dict[str, Any]:
    """When OpenAI/HTTP fails after an API key is configured — block safely."""
    return {
        "verifier_agrees": False,
        "risk_flags": ["LLM_CALL_FAILED"],
        "corrected_outcome": interpretation.get("interpreted_outcome", "UNKNOWN"),
        "confidence": 0.0,
        "should_block_trade": True,
        "reason": "LLM request failed; using conservative fallback.",
    }


def _fallback_verify(_: Market, __: NewsArticle, interpretation: dict[str, Any]) -> dict[str, Any]:
    # Skeptical verifier: usually blocks in fallback mode.
    should_block = interpretation.get("confidence", 0.0) < 0.95
    return {
        "verifier_agrees": not should_block,
        "risk_flags": ["NO_LLM_CONFIGURED"] if settings.openai_api_key is None else [],
        "corrected_outcome": interpretation.get("interpreted_outcome", "UNKNOWN"),
        "confidence": 0.2 if should_block else 0.9,
        "should_block_trade": should_block,
        "reason": "Fallback verifier blocks trades without LLM verification.",
    }


async def interpret_and_verify(market: Market, article: NewsArticle) -> tuple[dict[str, Any], dict[str, Any]]:
    if not settings.openai_api_key:
        interp = _fallback_interpret(market, article)
        ver = _fallback_verify(market, article, interp)
        return interp, ver

    try:
        interp = await _openai_interpret(market, article)
    except (httpx.HTTPError, json.JSONDecodeError):
        interp = _fallback_interpret(market, article)
        return interp, _verifier_after_llm_failure(interp)

    try:
        ver = await _openai_verify(market, article, interp)
    except (httpx.HTTPError, json.JSONDecodeError):
        ver = _verifier_after_llm_failure(interp)
    return interp, ver


async def interpret_and_verify_with_timeout(
    market: Market, article: NewsArticle, *, timeout_seconds: float
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Same as interpret_and_verify but caps wall time (outer bound on LLM + HTTP)."""
    if not settings.openai_api_key:
        return await interpret_and_verify(market, article)
    try:
        return await asyncio.wait_for(interpret_and_verify(market, article), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        interp = _fallback_interpret(market, article)
        return interp, _verifier_after_llm_failure(interp)


async def _openai_interpret(market: Market, article: NewsArticle) -> dict[str, Any]:
    prompt = f"""
You are interpreting whether a news article resolves or materially affects a prediction market.
Return ONLY valid JSON.

Market question:
{market.question}

Market resolution criteria:
{market.description or ""}

Allowed outcomes:
YES, NO, UNKNOWN

Article title:
{article.title}

Article body:
{article.body}

Return schema:
{{
  "market_relevance": 0.0,
  "interpreted_outcome": "YES|NO|UNKNOWN",
  "evidence_type": "DIRECT|INDIRECT|PRELIMINARY|SPECULATIVE|NONE",
  "supporting_excerpt": "...",
  "confidence": 0.0,
  "should_act": true,
  "reason": "..."
}}
""".strip()

    body = {
        "model": settings.openai_model_interpreter,
        "input": prompt,
        "response_format": {"type": "json_object"},
    }

    trust = settings.http_trust_env and not settings.http_disable_env_proxy
    async with httpx.AsyncClient(base_url=settings.openai_base_url, timeout=60.0, trust_env=trust) as client:
        r = await client.post(
            "/responses",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json=body,
        )
        r.raise_for_status()
        data = r.json()
        text = _extract_response_text(data)
        return json.loads(text)


async def _openai_verify(market: Market, article: NewsArticle, interpretation: dict[str, Any]) -> dict[str, Any]:
    prompt = f"""
You are a skeptical verifier. Your job is to find reasons the previous interpretation may be wrong.
Return ONLY valid JSON.

Market question:
{market.question}

Resolution criteria:
{market.description or ""}

Article title:
{article.title}

Article body:
{article.body}

Proposed interpretation JSON:
{json.dumps(interpretation)}

Return schema:
{{
  "verifier_agrees": true,
  "risk_flags": [],
  "corrected_outcome": "YES|NO|UNKNOWN",
  "confidence": 0.0,
  "should_block_trade": false,
  "reason": "..."
}}
""".strip()

    body = {
        "model": settings.openai_model_verifier,
        "input": prompt,
        "response_format": {"type": "json_object"},
    }

    trust = settings.http_trust_env and not settings.http_disable_env_proxy
    async with httpx.AsyncClient(base_url=settings.openai_base_url, timeout=60.0, trust_env=trust) as client:
        r = await client.post(
            "/responses",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json=body,
        )
        r.raise_for_status()
        data = r.json()
        text = _extract_response_text(data)
        return json.loads(text)


def _extract_response_text(data: dict[str, Any]) -> str:
    # Responses API: output[...].content[...].text
    out = data.get("output") or []
    for item in out:
        content = item.get("content") or []
        for c in content:
            if c.get("type") in ("output_text", "text") and "text" in c:
                return c["text"]
    # fallback
    return json.dumps({})

