from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import time
from typing import Awaitable, Callable

from app.db import SessionLocal
from app.init_db import init_db
from app.db import engine
from app.jobs import sports_latency


logger = logging.getLogger(__name__)


async def _run_job(name: str, fn: Callable) -> dict:
    async with SessionLocal() as session:
        try:
            out = await fn(session)
            logger.info("%s ok: %s", name, out)
            return out
        except Exception:
            logger.exception("%s failed", name)
            return {"ok": False, "job": name}


async def run_loop(*, poll_seconds: int = 60, settle_seconds: int = 300, once: bool = False) -> None:
    """Run sports latency collection locally with the same job functions used by Vercel."""
    await init_db(engine)
    logger.info("sports latency loop starting poll_seconds=%s settle_seconds=%s", poll_seconds, settle_seconds)
    await _run_job("sports_build_watchlist", sports_latency.build_watchlist)
    last_settle = time.monotonic() - settle_seconds
    while True:
        await _run_job("sports_poll_sources", sports_latency.poll_sources)
        now = time.monotonic()
        if now - last_settle >= settle_seconds:
            await _run_job("sports_check_settlements", sports_latency.check_settlements)
            last_settle = now
        if once:
            return
        await asyncio.sleep(max(10, poll_seconds))


def main() -> None:
    parser = argparse.ArgumentParser(description="Local sports settlement latency collector")
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--settle-seconds", type=int, default=300)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(run_loop(poll_seconds=args.poll_seconds, settle_seconds=args.settle_seconds, once=args.once))


if __name__ == "__main__":
    main()
