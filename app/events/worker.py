"""Dedicated outbox worker. DATABASE_URL must be the worker login, never the schema owner."""

import argparse
import asyncio
import logging
import signal
from types import FrameType

from sqlalchemy.exc import SQLAlchemyError

from app.config.logging import configure_logging
from app.config.settings import Settings
from app.database.session import Database
from app.events.processor import EventProcessor
from app.events.providers import ConsoleNotificationProvider, RetryPolicy
from app.events.store import OutboxStore
from app.observability.tracing import Telemetry

logger = logging.getLogger("app.events.worker")


async def run(settings: Settings, *, once: bool = False, check: bool = False) -> None:
    database = Database(settings)
    telemetry = Telemetry(settings)
    stop = asyncio.Event()

    def stop_worker(signum: int, frame: FrameType | None) -> None:
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop_worker)
    try:
        store = OutboxStore(database.session_factory, settings.outbox_lease_seconds)
        await store.check()
        if check:
            return
        processor = EventProcessor(
            store,
            ConsoleNotificationProvider(),
            retry=RetryPolicy(max_attempts=settings.outbox_max_attempts),
            timeout_seconds=settings.outbox_provider_timeout_seconds,
            telemetry=telemetry,
        )
        logger.info("outbox_worker_started")
        while not stop.is_set():
            try:
                processed = await processor.process_one()
            except (SQLAlchemyError, OSError) as exc:
                # A failed acknowledgement leaves the lease recoverable; do not claim delivery.
                logger.error(
                    "outbox_database_unavailable", extra={"error_type": type(exc).__name__}
                )
                processed = False
            if once:
                return
            if not processed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=settings.outbox_poll_seconds)
                except TimeoutError:
                    continue
    finally:
        await database.close()
        await telemetry.close()
        logger.info("outbox_worker_stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_environment()
    configure_logging(settings.log_level)
    try:
        asyncio.run(run(settings, once=args.once, check=args.check))
    except Exception as exc:
        logger.error("outbox_worker_failed", extra={"error_type": type(exc).__name__})
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
