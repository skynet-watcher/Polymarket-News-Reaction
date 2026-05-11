"""
Persistent Polymarket WebSocket worker scaffold.

Run this outside Vercel after the Vercel-first sports latency MVP is deployed.
The implementation is intentionally small: subscribe, timestamp, write to DB.
No paper-trade decisions belong in this worker.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def main() -> None:
    logger.warning(
        "sports_ws worker scaffold only. Next sprint: add DB session wiring, "
        "Polymarket sports WS client, market WS client, 5-minute subscription refresh, "
        "and reconnect/backoff event logging."
    )
    while True:
        await asyncio.sleep(300)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())

