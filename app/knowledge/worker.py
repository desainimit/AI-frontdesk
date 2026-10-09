"""Claim scheduling IDs globally; process every document under tenant RLS."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database.rls import set_tenant_scope
from app.events.store import verify_worker_role
from app.knowledge.embeddings import Embedder, OwnedEmbedder
from app.knowledge.models import DocumentChunk, DocumentVersion, KnowledgeJob
from app.knowledge.parsing import ParsedChunk, parse_document
from app.knowledge.storage import ObjectStorage

logger = logging.getLogger(__name__)
LEASE_SECONDS = 900


@dataclass(frozen=True)
class ClaimedJob:
    id: UUID
    tenant_id: UUID
    version_id: UUID
    lease_token: UUID
    attempts: int


class KnowledgeWorker:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        storage: ObjectStorage,
        embedder: Embedder | None,
        *,
        tenant_embedder: Callable[[UUID], Awaitable[OwnedEmbedder]] | None = None,
    ) -> None:
        if embedder is None and tenant_embedder is None:
            raise ValueError("Knowledge worker requires an embedding source")
        self.sessions, self.storage, self.embedder = sessions, storage, embedder
        self.tenant_embedder = tenant_embedder

    async def claim(
        self, version_id: UUID | None = None, tenant_id: UUID | None = None
    ) -> ClaimedJob | None:
        async with self.sessions.begin() as session:
            await verify_worker_role(session)
            query = select(KnowledgeJob).where(
                or_(
                    and_(
                        KnowledgeJob.status == "pending",
                        KnowledgeJob.available_at <= func.now(),
                    ),
                    and_(
                        KnowledgeJob.status == "processing",
                        KnowledgeJob.lease_until <= func.now(),
                    ),
                )
            )
            if version_id is not None:
                query = query.where(KnowledgeJob.document_version_id == version_id)
            if tenant_id is not None:
                query = query.where(KnowledgeJob.tenant_id == tenant_id)
            job = await session.scalar(
                query.order_by(KnowledgeJob.available_at, KnowledgeJob.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            token = uuid4()
            job.status = "processing"
            job.attempts += 1
            job.lease_token = token
            job.lease_until = await session.scalar(
                select(func.now() + timedelta(seconds=LEASE_SECONDS))
            )
            return ClaimedJob(job.id, job.tenant_id, job.document_version_id, token, job.attempts)

    async def _source(self, job: ClaimedJob) -> tuple[str, bytes]:
        async with self.sessions.begin() as session:
            await set_tenant_scope(session, job.tenant_id)
            version = await session.get(DocumentVersion, job.version_id)
            if version is None or version.tenant_id != job.tenant_id:
                raise ValueError("Version unavailable")
            key, name, checksum = version.storage_key, version.name, version.checksum
        body = await self.storage.get(key)
        if sha256(body).hexdigest() != checksum or len(body) > 25_000_000:
            raise ValueError("Stored document failed validation")
        return name, body

    async def _save(
        self, job: ClaimedJob, chunks: list[ParsedChunk], vectors: list[list[float]]
    ) -> None:
        async with self.sessions.begin() as session:
            await set_tenant_scope(session, job.tenant_id)
            lease = await session.scalar(
                select(KnowledgeJob)
                .where(
                    KnowledgeJob.id == job.id,
                    KnowledgeJob.lease_token == job.lease_token,
                    KnowledgeJob.status == "processing",
                    KnowledgeJob.lease_until > func.now(),
                )
                .with_for_update()
            )
            if lease is None:
                raise ValueError("Job lease expired")
            version = await session.get(DocumentVersion, job.version_id)
            if version is None or version.tenant_id != job.tenant_id:
                raise ValueError("Version unavailable")
            await session.execute(
                delete(DocumentChunk).where(DocumentChunk.document_version_id == version.id)
            )
            for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
                session.add(
                    DocumentChunk(
                        id=uuid4(),
                        tenant_id=job.tenant_id,
                        document_id=version.document_id,
                        document_version_id=version.id,
                        ordinal=index,
                        text=chunk.text,
                        section=chunk.section,
                        subsection=chunk.subsection,
                        page_number=chunk.page_number,
                        token_count=chunk.token_count,
                        embedding=vector,
                        checksum=chunk.checksum,
                        source_metadata=chunk.source_metadata,
                    )
                )
            version.status = "ready"
            version.error_code = None
            lease.status = "delivered"
            lease.lease_token = None
            lease.lease_until = None

    async def _fail(self, job: ClaimedJob, code: str) -> None:
        async with self.sessions.begin() as session:
            await set_tenant_scope(session, job.tenant_id)
            lease = await session.scalar(
                select(KnowledgeJob)
                .where(
                    KnowledgeJob.id == job.id,
                    KnowledgeJob.lease_token == job.lease_token,
                )
                .with_for_update()
            )
            if lease is None:
                return
            version = await session.get(DocumentVersion, job.version_id)
            if job.attempts >= 3:
                lease.status = "dead"
                if version is not None:
                    version.status = "failed"
                    version.error_code = code
            else:
                lease.status = "pending"
                lease.available_at = (
                    await session.scalar(select(func.now() + timedelta(seconds=5**job.attempts)))
                    or lease.available_at
                )
            lease.error_code = code
            lease.lease_token = None
            lease.lease_until = None

    async def process_one(
        self, version_id: UUID | None = None, tenant_id: UUID | None = None
    ) -> bool:
        job = await self.claim(version_id, tenant_id)
        if job is None:
            return False
        try:
            async with asyncio.timeout(LEASE_SECONDS - 30):
                name, body = await self._source(job)
                chunks = await asyncio.to_thread(parse_document, name, body)
                vectors: list[list[float]] = []
                # Resolved at execution, for this job's tenant only: a revoked or
                # disconnected connection fails the job instead of borrowing a key.
                opened = await self.tenant_embedder(job.tenant_id) if self.tenant_embedder else None
                try:
                    embedder = opened or self.embedder
                    if embedder is None:
                        raise RuntimeError("No embedding source")
                    for start in range(0, len(chunks), 16):
                        group = chunks[start : start + 16]
                        vectors.extend(
                            await embedder.embed(
                                [
                                    "\n".join(
                                        filter(None, (chunk.section, chunk.subsection, chunk.text))
                                    )
                                    for chunk in group
                                ]
                            )
                        )
                finally:
                    if opened is not None:
                        await opened.close()
                await self._save(job, chunks, vectors)
        except (ValueError, UnicodeError):
            await self._fail(job, "invalid_document")
        except Exception as exc:
            logger.warning("knowledge_ingestion_retry", extra={"error_type": type(exc).__name__})
            await self._fail(job, "processing_error")
        return True
