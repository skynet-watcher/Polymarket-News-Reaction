from __future__ import annotations

import datetime as dt
from typing import Optional, Tuple

from app.models import Market, NewsArticle, NewsSignal, PriceSnapshot
from app.util import now_utc


def decide_action(
    *,
    market: Market,
    article: NewsArticle,
    signal: NewsSignal,
    max_spread: float,
    min_liquidity: float,
    min_confidence: float,
    min_verifier_confidence: float,
    max_article_age_minutes: int,
    allow_indirect_evidence: bool = False,
    snapshot: Optional[PriceSnapshot] = None,
) -> Tuple[str, Optional[str]]:
    # Source is already whitelisted by ingestion guardrail.

    age = now_utc() - (
        article.published_at
        if article.published_at.tzinfo
        else article.published_at.replace(tzinfo=dt.timezone.utc)
    )
    max_age_seconds = max_article_age_minutes * 60
    if age.total_seconds() > max_age_seconds:
        return "ABSTAIN", "TOO_OLD"

    # Prefer the freshest snapshot (captured by periodic sync) over Market fields, which may be stale.
    bid = snapshot.best_bid_yes if snapshot else market.best_bid_yes
    ask = snapshot.best_ask_yes if snapshot else market.best_ask_yes
    liq = snapshot.liquidity if snapshot else market.liquidity

    if (liq or 0.0) < min_liquidity:
        return "NO_TRADE_LOW_LIQUIDITY", "LOW_LIQUIDITY"

    if bid is not None and ask is not None and (ask - bid) > max_spread:
        return "NO_TRADE_SPREAD_TOO_WIDE", "SPREAD_TOO_WIDE"

    et = (signal.evidence_type or "NONE").upper()
    if allow_indirect_evidence:
        if et in ("NONE", "SPECULATIVE"):
            return "ABSTAIN", "WEAK_EVIDENCE"
    else:
        if et != "DIRECT":
            return "ABSTAIN", "NOT_DIRECT_EVIDENCE"

    if (signal.confidence or 0.0) < min_confidence:
        return "ABSTAIN", "LOW_CONFIDENCE"

    if not signal.verifier_agrees:
        return "ABSTAIN", "VERIFIER_DISAGREES"

    if (signal.verifier_confidence or 0.0) < min_verifier_confidence:
        return "ABSTAIN", "LOW_VERIFIER_CONFIDENCE"

    # Still paper-only: ACT means "simulate trade"
    return "ACT", None

