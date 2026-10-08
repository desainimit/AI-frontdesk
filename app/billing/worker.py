"""Durable usage-lease expiry; explicit worker and financial logins, no API credentials."""

import argparse
import asyncio
import logging
import signal
from contextlib import suppress
from types import FrameType

from sqlalchemy.exc import SQLAlchemyError

from app.billing.database import FinancialDatabase
from app.billing.expiry import UsageExpiryBatchError, UsageExpirySweeper
from app.config.logging import configure_logging
from app.config.settings import Settings
from app.database.session import Database

logger = logging.getLogger(__name__)


async def run(settings: Settings, *, once: bool = False, check: bool = False) -> None:
    financial = FinancialDatabase(settings)
    discovery = Database(settings)
    stop = asyncio.Event()

    def stopping(signum: int, frame: FrameType | None) -> None:
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stopping)
    try:
        sweeper = UsageExpirySweeper(discovery.session_factory, financial.session_factory)
        await sweeper.check()
        if check:
            return
        while not stop.is_set():
            try:
                # A stalled database cannot prevent bounded shutdown. Cancellation
                # rolls back the current transaction; committed receipts survive.
                async with asyncio.timeout(10):
                    processed = await sweeper.process_batch()
                if processed:
                    logger.info("usage_leases_expired", extra={"count": processed})
            except (SQLAlchemyError, OSError, TimeoutError, UsageExpiryBatchError) as failure:
                if once:
                    raise SystemExit(
                        "Usage expiry failed; committed work remains recoverable"
                    ) from None
                logger.error(
                    "usage_expiry_unavailable", extra={"error_type": type(failure).__name__}
                )
            if once:
                return
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=1)
    finally:
        await discovery.close()
        await financial.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_environment(env_file=None)
    configure_logging(settings.log_level)
    asyncio.run(run(settings, once=args.once, check=args.check))


if __name__ == "__main__":
    main()
