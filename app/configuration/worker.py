"""Dedicated publication event dispatcher; use WORKER_DATABASE_URL as DATABASE_URL."""

import argparse
import asyncio
import logging
import signal
from contextlib import suppress
from types import FrameType

from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from app.cache.redis import RedisClient
from app.config.logging import configure_logging
from app.config.settings import Settings
from app.configuration.dispatch import ConfigurationDispatcher, ValkeyRevisionPublisher
from app.database.session import Database

logger = logging.getLogger(__name__)


async def run(settings: Settings, *, once: bool = False, check: bool = False) -> None:
    database, redis = Database(settings), RedisClient(settings)
    stop = asyncio.Event()

    def stopping(signum: int, frame: FrameType | None) -> None:
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stopping)
    try:
        dispatcher = ConfigurationDispatcher(
            database.session_factory,
            ValkeyRevisionPublisher(redis),
            timeout_seconds=settings.dependency_timeout_seconds,
        )
        await dispatcher.check()
        await redis.ping()
        if check:
            return
        while not stop.is_set():
            try:
                processed = await dispatcher.process_one()
            except (SQLAlchemyError, RedisError, OSError, TimeoutError) as failure:
                logger.error(
                    "configuration_dispatch_unavailable",
                    extra={"error_type": type(failure).__name__},
                )
                processed = False
            if once:
                return
            if not processed:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=1)
    finally:
        await redis.close()
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    # Worker deployments receive only their explicit environment; do not import
    # API/provider/schema-owner credentials from a local application .env file.
    settings = Settings.from_environment(env_file=None)
    configure_logging(settings.log_level)
    asyncio.run(run(settings, once=args.once, check=args.check))


if __name__ == "__main__":
    main()
