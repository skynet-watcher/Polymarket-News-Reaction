"""
Resolution data polling job — STUB, not hooked up.

FUTURE STATE: Called by the scheduler (or a dedicated asyncio loop) when
settings.enable_resolution_worker is True.

For each enabled ResolutionSourceConfig row, instantiate the named adapter
and call fetch(). Persist results as... TBD (new ResolutionDataPoint table
or as OutboxEvent payloads for the bias worker to consume).

Do not wire this into main.py until:
1. The ResolutionDataPoint model / table is designed and migrated.
2. The bias worker is ready to consume resolution signals.
3. settings.enable_resolution_worker is flipped to True in .env.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


async def run(session: AsyncSession) -> dict[str, Any]:
    """
    Poll all enabled resolution adapters and persist their outputs.

    NOT IMPLEMENTED — returns immediately with a no-op status.
    """
    return {"status": "NOT_IMPLEMENTED", "adapters_run": 0}
