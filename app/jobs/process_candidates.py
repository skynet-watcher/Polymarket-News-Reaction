from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Any, Optional

from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.gating import decide_action
from app.core.interpret import batch_relevance_screen, interpret_and_verify_with_timeout
from app.core.matcher import match_article_to_markets
from app.core.paper import maybe_paper_trade
from app.db import SessionLocal
from app.http_client import fetch_clob_orderbook, polymarket_async_client
from app.jobs.signal_metrics import compute_for_signal
from app.models import Market, MarketLagScore, NewsArticle, NewsSignal, PriceSnapshot, RuntimeSetting
from app.settings import settings
from app.threshold_context import resolve_trading_thresholds
from app.util import new_id, now_utc

logger = logging.getLogger(__name__)


async def _lag_focus_top_n(session: AsyncSession) -> int:
    row = await session.get(RuntimeSetting, "lag_focus_top_n")
    if row is None or not (row.value or "").strip():
        return settings.lag_focus_top_n
    try:
        return max(0, int(row.value.strip()))
    except ValueError:
        return settings.lag_focus_top_n


async def _process_one_candidate_signal(signal_id: str, semaphore: asyncio.Semaphore) -> dict[str, int]:
    async with semaphore:
        processed = 0
        trades = 0
        try:
            async with SessionLocal() as session:
                sig = await session.get(NewsSignal, signal_id)
                if sig is None or sig.action != "CANDIDATE":
                    return {"processed": 0, "trades": 0}
                market = await session.get(Market, sig.market_id)
                article = await session.get(NewsArticle, sig.article_id)
                if market is None or article is None:
                    return {"processed": 0, "trades": 0}

                tctx = await resolve_trading_thresholds(session)

                interpretation, verifier = await interpret_and_verify_with_timeout(
                    market,
                    article,
                    timeout_seconds=settings.llm_call_timeout_seconds,
                )

                sig.interpreted_outcome = interpretation.get("interpreted_outcome", "UNKNOWN")
                sig.evidence_type = interpretation.get("evidence_type", "NONE")
                sig.supporting_excerpt = interpretation.get("supporting_excerpt")
                sig.confidence = float(interpretation.get("confidence", 0.0))
                sig.raw_interpretation = interpretation

                sig.verifier_agrees = bool(verifier.get("verifier_agrees", False))
                sig.verifier_confidence = float(verifier.get("confidence", 0.0))
                sig.raw_verifier = verifier

                latest_snap = (
                    await session.execute(
                        select(PriceSnapshot)
                        .where(PriceSnapshot.market_id == market.id)
                        .order_by(PriceSnapshot.timestamp.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()

                action, reason = decide_action(
                    market=market,
                    article=article,
                    signal=sig,
                    max_spread=tctx.max_spread,
                    min_liquidity=tctx.min_liquidity,
                    min_confidence=tctx.min_confidence,
                    min_verifier_confidence=tctx.min_verifier_confidence,
                    max_article_age_minutes=tctx.max_article_age_minutes,
                    allow_indirect_evidence=tctx.allow_indirect_evidence,
                    snapshot=latest_snap,
                )

                sig.action = action
                sig.rejection_reason = reason

                if action == "ACT":
                    ob: Optional[dict[str, Any]] = None
                    async with polymarket_async_client() as http_client:
                        if market.enable_orderbook and market.token_ids_json:
                            tid = market.token_ids_json[0]
                            ob = await fetch_clob_orderbook(http_client, tid)
                        trade = maybe_paper_trade(
                            market=market,
                            signal=sig,
                            snapshot=latest_snap,
                            orderbook=ob,
                            paper_size_multiplier=tctx.paper_size_multiplier,
                        )
                    if trade is not None:
                        session.add(trade)
                        trades = 1
                    await compute_for_signal(session, signal=sig, article=article, market=market)

                processed = 1
                await session.commit()
        except Exception:
            logger.exception("candidate signal %s failed", signal_id)
        return {"processed": processed, "trades": trades}


async def run(session: AsyncSession) -> dict[str, Any]:
    t_run = time.perf_counter()
    tctx = await resolve_trading_thresholds(session)
    cutoff = now_utc() - dt.timedelta(minutes=tctx.max_article_age_minutes)
    articles = (
        await session.execute(
            select(NewsArticle)
            .where(NewsArticle.published_at >= cutoff)
            .order_by(desc(NewsArticle.published_at))
            .limit(100)
        )
    ).scalars().all()
    if not articles:
        return {
            "articles": 0,
            "signals_created": 0,
            "signals_processed": 0,
            "trades_created": 0,
            "duration_ms": int((time.perf_counter() - t_run) * 1000),
            "match_phase_ms": 0,
            "llm_phase_ms": 0,
            "llm_max_concurrency": settings.llm_max_concurrency,
            "llm_enabled": settings.openai_api_key is not None,
            "lag_focus_top_n": await _lag_focus_top_n(session),
            "threshold_profile_id": tctx.profile_id,
            "threshold_profile_label": tctx.profile_label,
        }

    focus_n = await _lag_focus_top_n(session)
    market_limit = settings.matcher_market_limit
    if focus_n > 0:
        top_ids = (
            await session.execute(
                select(MarketLagScore.market_id)
                .order_by(desc(MarketLagScore.combined_score))
                .limit(focus_n)
            )
        ).scalars().all()
        if top_ids:
            markets = (
                await session.execute(
                    select(Market).where(
                        and_(
                            Market.id.in_(list(top_ids)),
                            Market.active == True,  # noqa: E712
                            Market.closed == False,  # noqa: E712
                        )
                    )
                )
            ).scalars().all()
        else:
            markets = (
                await session.execute(
                    select(Market)
                    .where(and_(Market.active == True, Market.closed == False))  # noqa: E712
                    .order_by(desc(Market.liquidity))
                    .limit(market_limit)
                )
            ).scalars().all()
    else:
        markets = (
            await session.execute(
                select(Market)
                .where(and_(Market.active == True, Market.closed == False))  # noqa: E712
                .order_by(desc(Market.liquidity))
                .limit(market_limit)
            )
        ).scalars().all()

    signals_created = 0
    t_match = time.perf_counter()

    for article in articles:
        # ── Stage 1: keyword pre-filter (permissive) ──────────────────────────
        kw_candidates = match_article_to_markets(
            article,
            markets,
            min_relevance=settings.matcher_keyword_min_relevance,
        )
        # Cap before LLM to bound token spend.
        kw_candidates = kw_candidates[: settings.matcher_keyword_max_candidates]

        if not kw_candidates:
            continue

        # ── Stage 2: batch LLM relevance screen ──────────────────────────────
        candidate_markets = [c["market"] for c in kw_candidates]
        # Build a quick lookup so we can join LLM scores back to keyword scores.
        kw_by_market_id = {c["market"].id: c for c in kw_candidates}

        llm_scores = await batch_relevance_screen(article, candidate_markets)

        # Keep only markets that pass the LLM threshold.
        confirmed: list[dict] = []
        for result in llm_scores:
            mid = result["market_id"]
            score = result["score"]
            if score < settings.matcher_llm_min_relevance:
                continue
            kw = kw_by_market_id.get(mid)
            if kw is None:
                continue
            confirmed.append({**kw, "llm_relevance": score})

        # Sort confirmed by LLM score (highest first) and take top 5.
        confirmed.sort(key=lambda x: x["llm_relevance"], reverse=True)

        for cand in confirmed[:5]:
            market = cand["market"]
            relevance = float(cand["llm_relevance"])

            existing = (
                await session.execute(
                    select(NewsSignal).where(and_(NewsSignal.market_id == market.id, NewsSignal.article_id == article.id))
                )
            ).scalar_one_or_none()
            if existing is not None:
                continue

            sig = NewsSignal(
                id=new_id("sig"),
                market_id=market.id,
                article_id=article.id,
                relevance_score=relevance,
                action="CANDIDATE",
                created_at=now_utc(),
            )
            session.add(sig)
            signals_created += 1

    await session.commit()
    match_phase_ms = int((time.perf_counter() - t_match) * 1000)

    candidates = (
        await session.execute(select(NewsSignal).where(NewsSignal.action == "CANDIDATE").order_by(desc(NewsSignal.created_at)).limit(200))
    ).scalars().all()

    t_llm = time.perf_counter()
    sem = asyncio.Semaphore(max(1, settings.llm_max_concurrency))
    results = await asyncio.gather(*[_process_one_candidate_signal(s.id, sem) for s in candidates])
    llm_phase_ms = int((time.perf_counter() - t_llm) * 1000)

    signals_processed = sum(r["processed"] for r in results)
    trades_created = sum(r["trades"] for r in results)
    duration_ms = int((time.perf_counter() - t_run) * 1000)

    logger.info(
        "process_candidates done articles=%s created=%s processed=%s trades=%s duration_ms=%s match_ms=%s llm_ms=%s concurrency=%s",
        len(articles),
        signals_created,
        signals_processed,
        trades_created,
        duration_ms,
        match_phase_ms,
        llm_phase_ms,
        settings.llm_max_concurrency,
    )

    return {
        "articles": len(articles),
        "signals_created": signals_created,
        "signals_processed": signals_processed,
        "trades_created": trades_created,
        "duration_ms": duration_ms,
        "match_phase_ms": match_phase_ms,
        "llm_phase_ms": llm_phase_ms,
        "llm_max_concurrency": settings.llm_max_concurrency,
        "llm_enabled": settings.openai_api_key is not None,
        "lag_focus_top_n": focus_n,
        "threshold_profile_id": tctx.profile_id,
        "threshold_profile_label": tctx.profile_label,
    }
