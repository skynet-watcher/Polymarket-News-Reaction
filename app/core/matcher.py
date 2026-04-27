from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from app.models import Market, NewsArticle


_word_re = re.compile(r"[a-z0-9]{3,}", re.IGNORECASE)


def _keywords(text: str) -> set[str]:
    return {m.group(0).lower() for m in _word_re.finditer(text or "")}


def match_article_to_markets(article: NewsArticle, markets: Iterable[Market], min_relevance: float) -> list[dict[str, Any]]:
    """
    Deterministic matcher for MVP: keyword overlap between article and market question/description.
    Returns sorted candidates with relevance \in [0, 1].
    """
    a_kw = _keywords(f"{article.title}\n{article.body}")
    if not a_kw:
        return []

    scored: list[dict[str, Any]] = []
    for m in markets:
        m_kw = _keywords(f"{m.question}\n{m.description or ''}")
        if not m_kw:
            continue
        overlap = len(a_kw & m_kw)
        denom = max(10, min(len(a_kw), len(m_kw)))
        relevance = min(1.0, overlap / denom)
        if relevance >= min_relevance:
            scored.append({"market": m, "relevance": relevance, "overlap": overlap})

    scored.sort(key=lambda x: (x["relevance"], x["overlap"]), reverse=True)
    return scored

