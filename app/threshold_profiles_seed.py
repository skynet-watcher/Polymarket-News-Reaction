"""Default threshold profile rows; seeded on startup if the table is empty."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ThresholdProfile

DEFAULT_PROFILES: list[dict] = [
    {
        "id": "conservative",
        "display_name": "Conservative",
        "description": "High bar for ACT: tight spreads/liquidity, direct evidence only, strict LLM confidence.",
        "min_liquidity": 1000.0,
        "max_spread": 0.08,
        "min_relevance": 0.75,
        "min_confidence": 0.90,
        "min_verifier_confidence": 0.85,
        "max_article_age_minutes": 30,
        "allow_indirect_evidence": False,
        "paper_size_multiplier": 1.0,
    },
    {
        "id": "balanced",
        "display_name": "Balanced",
        "description": "Moderate keyword overlap; allows indirect/preliminary evidence; longer article window.",
        "min_liquidity": 800.0,
        "max_spread": 0.10,
        "min_relevance": 0.55,
        "min_confidence": 0.82,
        "min_verifier_confidence": 0.78,
        "max_article_age_minutes": 90,
        "allow_indirect_evidence": True,
        "paper_size_multiplier": 1.0,
    },
    {
        "id": "aggressive",
        "display_name": "Aggressive",
        "description": "More ACTs for research: lower relevance/confidence, wider spreads, 4h articles, larger paper size.",
        "min_liquidity": 600.0,
        "max_spread": 0.12,
        "min_relevance": 0.40,
        "min_confidence": 0.72,
        "min_verifier_confidence": 0.68,
        "max_article_age_minutes": 240,
        "allow_indirect_evidence": True,
        "paper_size_multiplier": 1.25,
    },
]


async def ensure_default_threshold_profiles(session: AsyncSession) -> None:
    cnt = (await session.execute(select(func.count()).select_from(ThresholdProfile))).scalar_one()
    if int(cnt or 0) > 0:
        return
    for row in DEFAULT_PROFILES:
        session.add(ThresholdProfile(**row))
    await session.commit()
