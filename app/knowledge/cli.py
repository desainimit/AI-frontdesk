"""Dedicated knowledge ingestion process using the existing restricted worker login."""

import argparse
import asyncio
import logging
import signal
from types import FrameType

from app.config.logging import configure_logging
from app.config.settings import Settings
from app.database.session import Database
from app.events.store import verify_worker_role
from app.integrations.resolver import TenantIntegrationResolver
from app.knowledge.embeddings import GeminiEmbedder, open_embedder
from app.knowledge.storage import build_storage
from app.knowledge.worker import KnowledgeWorker

logger = logging.getLogger(__name__)


async def run(settings: Settings, *, once: bool = False, check: bool = False) -> None:
    if not settings.knowledge_enabled or settings.worker_database_url is None:
        raise ValueError("Knowledge worker requires enabled knowledge and worker credentials")
    config = settings.model_copy(update={"database_url": settings.worker_database_url})
    database = Database(config)
    # Mixed-tenant keys: each job embeds with its own tenant's connection.
    embedder = None if settings.tenant_integrations_enabled else GeminiEmbedder(settings)
    resolver = TenantIntegrationResolver(
        database.session_factory, settings, role_check=verify_worker_role
    )
    worker = KnowledgeWorker(
        database.session_factory,
        build_storage(settings),
        embedder,
        tenant_embedder=(
            (lambda tenant_id: open_embedder(settings, resolver, tenant_id))
            if settings.tenant_integrations_enabled
            else None
        ),
    )
    stop = asyncio.Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, request_stop)
    try:
        async with database.session_factory.begin() as session:
            await verify_worker_role(session)
        if check:
            return
        while not stop.is_set():
            processed = await worker.process_one()
            if once:
                return
            if not processed:
                try:
                    await asyncio.wait_for(stop.wait(), settings.knowledge_worker_poll_seconds)
                except TimeoutError:
                    continue
    finally:
        if embedder is not None:
            await embedder.close()
        await database.close()


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
        logger.error("knowledge_worker_failed", extra={"error_type": type(exc).__name__})
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
