from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from app.models import Market, NewsArticle
from app.settings import settings

logger = logging.getLogger(__name__)


async def batch_relevance_screen(
    article: NewsArticle,
    markets: list[Market],
) -> list[dict[str, Any]]:
    """
    Stage-2 LLM relevance screen.

    Sends one LLM call per batch of ``settings.matcher_llm_batch_size`` markets
    and returns a list of dicts::

        {"market_id": str, "score": float, "reason": str}

    Only called when an OpenAI key is configured AND the candidate list is
    non-empty.  Falls back to returning all candidates with score=1.0 when the
    LLM is unavailable, so the pipeline degrades gracefully.
    """
    if not markets:
        return []

    if not settings.openai_api_key:
        # No LLM: pass all candidates through so keyword matches still work.
        return [{"market_id": m.id, "score": 1.0, "reason": "no-llm fallback"} for m in markets]

    batch_size = max(1, settings.matcher_llm_batch_size)
    results: list[dict[str, Any]] = []

    for i in range(0, len(markets), batch_size):
        batch = markets[i : i + batch_size]
        try:
            batch_results = await _llm_relevance_batch(article, batch)
            results.extend(batch_results)
        except Exception:
            logger.exception(
                "batch_relevance_screen LLM call failed for article=%s batch_start=%d; passing batch through",
                article.id,
                i,
            )
            # On error, pass the batch through rather than dropping it.
            results.extend(
                [{"market_id": m.id, "score": 1.0, "reason": "llm-error fallback"} for m in batch]
            )

    return results


async def _llm_relevance_batch(
    article: NewsArticle,
    markets: list[Market],
) -> list[dict[str, Any]]:
    """
    Single LLM call: score a batch of markets for relevance to one article.

    Prompt asks the model to score each market 0.0–1.0 and return a JSON array.
    Uses ``gpt-4o-mini`` for speed and cost efficiency.
    """
    numbered = "\n".join(
        f'{idx + 1}. [id:{m.id}] {m.question}'
        for idx, m in enumerate(markets)
    )

    # Truncate article body to keep prompt compact (RSS summaries are short anyway).
    body_preview = (article.body or "")[:600].strip()

    prompt = f"""You are a prediction-market relevance filter.

Score each market question below for how directly the article affects or could resolve it.

Scoring guide:
  0.0 = completely unrelated
  0.3 = tangentially related (same general topic, different specific question)
  0.6 = probably relevant — article could move this market's price
  0.9 = highly relevant — article directly addresses the market's resolution criteria
  1.0 = article resolves or near-resolves the market

Article headline: {article.title}
Article summary: {body_preview}

Markets to score:
{numbered}

Return ONLY a JSON array — one object per market, in the same order:
[{{"id": "<market id from [id:...]>", "score": 0.0, "reason": "one sentence"}}]""".strip()

    body = {
        "model": settings.openai_model_interpreter,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }

    trust = settings.http_trust_env and not settings.http_disable_env_proxy
    async with httpx.AsyncClient(base_url=settings.openai_base_url, timeout=30.0, trust_env=trust) as client:
        r = await client.post(
            "/chat/completions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json=body,
        )
        r.raise_for_status()
        data = r.json()
        text = _extract_response_text(data)

    # The model sometimes wraps the array in {"results": [...]} — unwrap if needed.
    parsed = json.loads(text)
    if isinstance(parsed, dict):
        # find the first list value
        for v in parsed.values():
            if isinstance(v, list):
                parsed = v
                break
        else:
            parsed = []

    out: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        market_id = str(item.get("id", "")).strip()
        try:
            score = float(item.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        out.append({"market_id": market_id, "score": score, "reason": item.get("reason", "")})

    return out


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
    # Build optional context lines so the LLM has the full resolution picture.
    extra_lines: list[str] = []
    if market.rules_text:
        extra_lines.append(f"Resolution rules:\n{market.rules_text}")
    if market.resolution_source_text:
        extra_lines.append(f"Authoritative resolution source: {market.resolution_source_text}")
    if market.end_date:
        extra_lines.append(f"Market closes: {market.end_date.date().isoformat()}")
    extra_context = ("\n\n" + "\n\n".join(extra_lines)) if extra_lines else ""

    prompt = f"""You are interpreting whether a news article resolves or materially affects a prediction market.
Return ONLY valid JSON.

Market question:
{market.question}

Market resolution criteria:
{market.description or "(not specified)"}
{extra_context}

Allowed outcomes: YES, NO, UNKNOWN

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
}}""".strip()

    body = {
        "model": settings.openai_model_interpreter,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }

    trust = settings.http_trust_env and not settings.http_disable_env_proxy
    async with httpx.AsyncClient(base_url=settings.openai_base_url, timeout=60.0, trust_env=trust) as client:
        r = await client.post(
            "/chat/completions",
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
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }

    trust = settings.http_trust_env and not settings.http_disable_env_proxy
    async with httpx.AsyncClient(base_url=settings.openai_base_url, timeout=60.0, trust_env=trust) as client:
        r = await client.post(
            "/chat/completions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json=body,
        )
        r.raise_for_status()
        data = r.json()
        text = _extract_response_text(data)
        return json.loads(text)


def _extract_response_text(data: dict[str, Any]) -> str:
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return json.dumps({})

