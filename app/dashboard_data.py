"""Shared dashboard snapshot for HTML + SSE (JSON-serializable)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Market, NewsArticle, NewsSignal, NewsSource, PaperTrade, PriceSnapshot
from app.threshold_context import resolve_trading_thresholds
from app.util import now_utc, to_utc_aware


def _sig_row(s: NewsSignal) -> dict[str, Any]:
    return {
        "id": s.id,
        "created_at": s.created_at.isoformat() if s.created_at else "",
        "market_id": s.market_id,
        "interpreted_outcome": s.interpreted_outcome,
        "evidence_type": s.evidence_type,
        "confidence": float(s.confidence or 0.0),
        "verifier_agrees": bool(s.verifier_agrees),
        "verifier_confidence": float(s.verifier_confidence or 0.0),
        "action": s.action,
    }


async def get_dashboard_snapshot(session: AsyncSession) -> dict[str, Any]:
    markets_count = (await session.execute(select(func.count()).select_from(Market))).scalar_one()
    sources_count = (await session.execute(select(func.count()).select_from(NewsSource))).scalar_one()
    articles_count = (await session.execute(select(func.count()).select_from(NewsArticle))).scalar_one()
    signals_count = (await session.execute(select(func.count()).select_from(NewsSignal))).scalar_one()
    trades_count = (await session.execute(select(func.count()).select_from(PaperTrade))).scalar_one()

    recent_signals = (
        await session.execute(select(NewsSignal).order_by(desc(NewsSignal.created_at)).limit(20))
    ).scalars().all()

    last_snap = (await session.execute(select(func.max(PriceSnapshot.timestamp)))).scalar_one_or_none()
    last_article = (await session.execute(select(func.max(NewsArticle.published_at)))).scalar_one_or_none()

    act_24h = (
        await session.execute(
            select(func.count())
            .select_from(NewsSignal)
            .where(NewsSignal.action == "ACT")
            .where(NewsSignal.created_at >= now_utc() - dt.timedelta(hours=24))
        )
    ).scalar_one()

    last_snap_age_min = (
        int((now_utc() - to_utc_aware(last_snap)).total_seconds() / 60) if last_snap else 9999
    )
    last_article_age_min = (
        int((now_utc() - to_utc_aware(last_article)).total_seconds() / 60) if last_article else 9999
    )

    tctx = await resolve_trading_thresholds(session)

    return {
        "last_snap_age_min": last_snap_age_min,
        "last_article_age_min": last_article_age_min,
        "act_24h": act_24h,
        "threshold_profile_label": tctx.profile_label,
        "threshold_profile_id": tctx.profile_id,
        "counts": {
            "markets": markets_count,
            "sources": sources_count,
            "articles": articles_count,
            "signals": signals_count,
            "trades": trades_count,
        },
        "recent_signals": [_sig_row(s) for s in recent_signals],
    }
