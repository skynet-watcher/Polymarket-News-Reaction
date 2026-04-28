"""
Article → market keyword pre-filter.

Stage 1 of a two-stage matching pipeline:

  Stage 1 (this file): fast, deterministic, permissive.
      • Title words weighted 3× body words.
      • Common entity aliases normalised before tokenisation.
      • Hard stop-words removed so "will", "the", "said" don't inflate scores.
      • Returns candidates with relevance ≥ caller-supplied threshold (default 0.15).
      • Sets entity_hit=True when ≥1 significant (≥5-char) market keyword is
        found in the article title — a cheap proxy for named-entity overlap.

  Stage 2 (app/core/interpret.py → batch_relevance_screen):
      • A single batched LLM call semantically vets the keyword candidates
        before any NewsSignal rows are created.

Keep this module free of async/DB/LLM dependencies.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from app.models import Market, NewsArticle

# ── Entity alias normalisation ────────────────────────────────────────────────
# Expand abbreviations so "Fed" matches "federal reserve" and vice-versa.
ENTITY_ALIASES: dict[str, str] = {
    "fed": "federal reserve",
    "fomc": "federal reserve",
    "btc": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "xrp": "ripple",
    "doge": "dogecoin",
    "potus": "president",
    "flotus": "first lady",
    "gop": "republican",
    "dems": "democrat",
    "doj": "department justice",
    "dhs": "department homeland security",
    "fda": "food drug administration",
    "sec": "securities exchange commission",
    "cpi": "consumer price index",
    "gdp": "gross domestic product",
    "ai": "artificial intelligence",
    "ev": "electric vehicle",
    "ipo": "initial public offering",
    "dow": "dow jones",
    "s&p": "standard poors",
    "nfl": "national football league",
    "nba": "national basketball association",
    "mlb": "major league baseball",
    "nhl": "national hockey league",
    "ufc": "ultimate fighting championship",
}

# ── Stop words ────────────────────────────────────────────────────────────────
# Words that appear in both news text and market questions but carry no signal.
_STOP_WORDS: frozenset[str] = frozenset(
    {
        # Articles / prepositions / conjunctions
        "the", "and", "for", "from", "with", "that", "this", "these", "those",
        "which", "what", "when", "where", "who", "how", "why",
        "but", "not", "nor", "yet", "both", "either", "neither",
        "about", "above", "after", "before", "between", "into", "over",
        "than", "then", "there", "their", "they", "them",
        "also", "just", "more", "most", "much", "such", "even",
        # High-frequency prediction-market boilerplate
        "will", "would", "could", "should", "may", "might", "shall",
        "has", "have", "had", "was", "were", "been", "being",
        "are", "can", "did", "does", "said", "says",
        "yes", "any", "all", "its", "his", "her", "our", "your",
        "new", "one", "two", "out", "get", "got", "let",
        "first", "second", "third", "last",
    }
)

_word_re = re.compile(r"[a-z0-9]{2,}", re.IGNORECASE)


def _normalise(text: str) -> str:
    """Lower-case and expand entity aliases."""
    t = (text or "").lower()
    for abbr, expansion in ENTITY_ALIASES.items():
        t = re.sub(rf"\b{re.escape(abbr)}\b", expansion, t)
    return t


def _keywords(text: str) -> set[str]:
    """Tokenise normalised text and strip stop-words."""
    return {
        tok
        for m in _word_re.finditer(_normalise(text))
        if (tok := m.group(0).lower()) not in _STOP_WORDS
    }


def _score(
    title_kw: set[str],
    body_only_kw: set[str],
    market_kw: set[str],
) -> tuple[float, bool]:
    """
    Compute weighted relevance and entity_hit flag.

    Title tokens count 3×; body-only tokens count 1×.
    entity_hit is True when any significant market keyword (≥5 chars) appears
    in the article title — a cheap proxy for named-entity overlap.

    Returns (relevance ∈ [0, 1], entity_hit).
    """
    all_article_kw = title_kw | body_only_kw

    # weighted overlap
    weighted_overlap = sum(
        (3.0 if w in title_kw else 1.0)
        for w in market_kw
        if w in all_article_kw
    )
    if weighted_overlap == 0:
        return 0.0, False

    # denominator: effective article vocab (title words × 3), clamped
    effective_size = len(title_kw) * 3 + len(body_only_kw)
    denom = max(15, min(effective_size, len(market_kw) * 3))
    relevance = min(1.0, weighted_overlap / denom)

    # entity_hit: any market keyword ≥5 chars appears in the article title
    entity_hit = any(w in title_kw for w in market_kw if len(w) >= 5)

    return relevance, entity_hit


def match_article_to_markets(
    article: NewsArticle,
    markets: Iterable[Market],
    min_relevance: float,
) -> list[dict[str, Any]]:
    """
    Stage-1 keyword pre-filter.

    Returns candidates sorted by (entity_hit desc, relevance desc), each dict:
        market      – Market ORM object
        relevance   – float ∈ [0, 1]
        overlap     – raw weighted overlap score
        entity_hit  – bool
    """
    title_kw = _keywords(article.title or "")
    body_only_kw = _keywords(article.body or "") - title_kw
    all_article_kw = title_kw | body_only_kw

    if not all_article_kw:
        return []

    scored: list[dict[str, Any]] = []
    for m in markets:
        market_kw = _keywords(f"{m.question}\n{m.description or ''}")
        if not market_kw:
            continue

        # Hard gate: zero token overlap → skip without computing score.
        if not (all_article_kw & market_kw):
            continue

        relevance, entity_hit = _score(title_kw, body_only_kw, market_kw)
        if relevance < min_relevance:
            continue

        weighted_overlap = sum(
            (3.0 if w in title_kw else 1.0)
            for w in market_kw
            if w in all_article_kw
        )
        scored.append(
            {
                "market": m,
                "relevance": relevance,
                "overlap": weighted_overlap,
                "entity_hit": entity_hit,
            }
        )

    scored.sort(
        key=lambda x: (x["entity_hit"], x["relevance"], x["overlap"]),
        reverse=True,
    )
    return scored
