"""
Truth Social post adapter — STUB.

FUTURE STATE: Detect when a specific Truth Social account posts (or posts
containing specific keywords).

WARNING: Truth Social does not provide a public API. Any implementation must
use an authenticated scraping approach, which may violate Truth Social's Terms
of Service. Before implementing, obtain legal sign-off and evaluate whether
scraping constitutes unauthorized access under the CFAA. A safer alternative
is a third-party monitoring service that provides a licensed feed.

Config keys:
    account_handle (str): e.g. "realDonaldTrump".
    keyword_pattern (str): Regex to match against post text (optional).
"""

from __future__ import annotations

from typing import Any, Optional

from app.resolution.base import ResolutionAdapter, ResolutionSignal


class TruthSocialAdapter(ResolutionAdapter):
    name = "truth_social"

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)

    async def fetch(self) -> Optional[ResolutionSignal]:
        raise NotImplementedError(
            "TruthSocialAdapter is not implemented. "
            "See module docstring for legal considerations before implementing."
        )
