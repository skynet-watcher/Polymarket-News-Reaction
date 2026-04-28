"""Resolve active paper-trading thresholds from DB (ThresholdProfile + RuntimeSetting)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RuntimeSetting, ThresholdProfile
from app.settings import settings

RUNTIME_KEY_THRESHOLD_PROFILE = "threshold_profile_id"
DEFAULT_PROFILE_ID = "research"


@dataclass(frozen=True)
class TradingThresholdContext:
    min_liquidity: float
    max_spread: float
    min_relevance: float
    min_confidence: float
    min_verifier_confidence: float
    max_article_age_minutes: int
    allow_indirect_evidence: bool
    paper_size_multiplier: float
    profile_id: str
    profile_label: str


def _fallback_from_settings() -> TradingThresholdContext:
    return TradingThresholdContext(
        min_liquidity=settings.min_liquidity,
        max_spread=settings.max_spread,
        min_relevance=settings.min_relevance,
        min_confidence=settings.min_confidence,
        min_verifier_confidence=settings.min_verifier_confidence,
        max_article_age_minutes=settings.max_article_age_minutes,
        allow_indirect_evidence=False,
        paper_size_multiplier=1.0,
        profile_id="conservative",
        profile_label="Settings fallback",
    )


async def resolve_trading_thresholds(session: AsyncSession) -> TradingThresholdContext:
    row = await session.get(RuntimeSetting, RUNTIME_KEY_THRESHOLD_PROFILE)
    pid = (row.value or "").strip() if row else ""
    if not pid:
        pid = DEFAULT_PROFILE_ID

    prof = await session.get(ThresholdProfile, pid)
    if prof is None:
        prof = await session.get(ThresholdProfile, DEFAULT_PROFILE_ID)
    if prof is None:
        return _fallback_from_settings()

    return TradingThresholdContext(
        min_liquidity=float(prof.min_liquidity),
        max_spread=float(prof.max_spread),
        min_relevance=float(prof.min_relevance),
        min_confidence=float(prof.min_confidence),
        min_verifier_confidence=float(prof.min_verifier_confidence),
        max_article_age_minutes=int(prof.max_article_age_minutes),
        allow_indirect_evidence=bool(prof.allow_indirect_evidence),
        paper_size_multiplier=float(prof.paper_size_multiplier or 1.0),
        profile_id=prof.id,
        profile_label=prof.display_name,
    )
