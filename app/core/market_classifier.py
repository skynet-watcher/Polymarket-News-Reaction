"""
Market type classifier.

Assigns a market_type string to a Market row based on its question text,
category field, and resolution_source_text. This runs offline (not on the
critical signal path) and writes Market.market_type.

NOT wired into the live pipeline yet. Call classify_market() from a one-off
script or backfill job once the market_type column migration is applied.
"""

from __future__ import annotations

import re
from typing import Optional

# Ordered list of (market_type, compiled patterns).
# First match wins, so place more-specific rules above generic ones.
_RULES: list[tuple[str, list[re.Pattern[str]]]] = [
    (
        "CRYPTO_HOURLY",
        [
            re.compile(r"\b(bitcoin|btc|ethereum|eth|crypto|solana|sol|doge)\b", re.I),
            re.compile(r"\b(price.*above|above.*price)\b", re.I),
        ],
    ),
    (
        "WEATHER_DAILY",
        [
            re.compile(r"\b(temperature|rainfall|hurricane|tornado|snowfall|drought|flood)\b", re.I),
            re.compile(r"\bwunderground\b", re.I),
        ],
    ),
    (
        "TSA_DATA",
        [
            re.compile(r"\btsa\b", re.I),
            re.compile(r"\bairport.*(passenger|throughput|screening)\b", re.I),
        ],
    ),
    (
        "SPORTS",
        [
            re.compile(r"\b(nba|nfl|mlb|nhl|mls|fifa|ufc|nascar)\b", re.I),
            re.compile(r"\b(super bowl|world series|world cup|playoffs|championship)\b", re.I),
            re.compile(r"\b(win|beat|defeat).*(game|match|series)\b", re.I),
        ],
    ),
    (
        "TRUMP_APPROVAL",
        [
            re.compile(r"\btrump.*approval\b", re.I),
            re.compile(r"\bapproval.*(rating|poll).*trump\b", re.I),
            re.compile(r"\bsilver.?bulletin\b", re.I),
        ],
    ),
    (
        "TRUMP_SOCIAL",
        [
            re.compile(r"\btrump.*(post|tweet|truth.social|mar.a.lago)\b", re.I),
            re.compile(r"\btruth.social\b", re.I),
        ],
    ),
    (
        "GOVT_ACTION",
        [
            re.compile(r"\b(executive order|legislation|congress|senate|bill|veto|supreme court)\b", re.I),
            re.compile(r"\b(fda|fed|federal reserve|treasury|doj|dhs)\b", re.I),
        ],
    ),
    (
        "BOX_OFFICE",
        [
            re.compile(r"\bbox.office\b", re.I),
            re.compile(r"\b(opening weekend|domestic gross|worldwide gross)\b", re.I),
        ],
    ),
    (
        "BILLBOARD",
        [
            re.compile(r"\bbillboard\b", re.I),
            re.compile(r"\b(hot 100|chart|album sales|streaming chart)\b", re.I),
        ],
    ),
    (
        "POP_CULTURE",
        [
            re.compile(r"\b(oscar|grammy|emmy|golden globe|award|celebrity)\b", re.I),
            re.compile(r"\b(viral|trending|social media)\b", re.I),
        ],
    ),
]


def classify_market(
    question: str,
    category: Optional[str] = None,
    resolution_source_text: Optional[str] = None,
) -> str:
    """
    Return the market_type string for a market. Falls back to "OTHER".

    Args:
        question: Market.question
        category: Market.category (Polymarket-supplied, may be None)
        resolution_source_text: Market.resolution_source_text (free text from Gamma)
    """
    corpus = " ".join(filter(None, [question, category, resolution_source_text]))

    for market_type, patterns in _RULES:
        if any(pat.search(corpus) for pat in patterns):
            return market_type

    return "OTHER"
