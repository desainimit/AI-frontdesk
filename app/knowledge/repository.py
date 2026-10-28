"""Tenant-scoped knowledge writes and source-aware vector retrieval."""

from uuid import UUID, uuid4

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database.search import contains
from app.knowledge.models import Document, DocumentChunk, DocumentVersion, KnowledgeJob


class KnowledgeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_version(
        self,
        tenant_id: UUID,
        name: str,
        source_type: str,
        checksum: str,
        storage_key: str,
        document_id: UUID | None = None,
    ) -> tuple[Document, DocumentVersion]:
        if document_id is None:
            document = Document(
                id=uuid4(),
                tenant_id=tenant_id,
                name=name,
                source_type=source_type,
                status="draft",
                active_version=1,
            )
            self.session.add(document)
        else:
            document = await self.session.scalar(
                select(Document).where(Document.id == document_id).with_for_update()
            )
            if document is None or document.status == "archived":
                from app.core.errors import NotFoundError

                raise NotFoundError()
            document.active_version += 1
            document.name = name
            document.source_type = source_type
            document.status = "draft"
        version = DocumentVersion(
            id=uuid4(),
            tenant_id=tenant_id,
            document_id=document.id,
            version=document.active_version,
            checksum=checksum,
            name=name,
            source_type=source_type,
            storage_key=storage_key,
            status="pending",
            attempts=0,
        )
        self.session.add(version)
        self.session.add(
            KnowledgeJob(
                id=uuid4(),
                tenant_id=tenant_id,
                document_version_id=version.id,
                status="pending",
                attempts=0,
            )
        )
        await self.session.flush()
        return document, version

    async def list_documents(
        self, limit: int, offset: int, *, status: str | None = None, q: str | None = None
    ) -> list[Document]:
        query = select(Document)
        if status is not None:
            query = query.where(Document.status == status)
        search = contains(q, Document.name)
        if search is not None:
            query = query.where(search)
        return list(
            (
                await self.session.scalars(
                    query.order_by(Document.created_at.desc(), Document.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        )

    async def get(self, document_id: UUID) -> Document | None:
        return await self.session.get(Document, document_id)

    async def get_for_update(self, document_id: UUID) -> Document | None:
        return await self.session.scalar(
            select(Document).where(Document.id == document_id).with_for_update()
        )

    async def versions(self, document_id: UUID) -> list[DocumentVersion]:
        return list(
            (
                await self.session.scalars(
                    select(DocumentVersion)
                    .where(DocumentVersion.document_id == document_id)
                    .order_by(DocumentVersion.version.desc())
                    .limit(50)
                )
            ).all()
        )

    async def retrieve(
        self, tenant_id: UUID, vector: list[float], limit: int, model: str
    ) -> list[tuple[DocumentChunk, Document, float]]:
        distance = DocumentChunk.embedding.cosine_distance(vector)
        rows = await self.session.execute(
            select(DocumentChunk, Document, distance)
            .join(
                Document,
                (Document.tenant_id == DocumentChunk.tenant_id)
                & (Document.id == DocumentChunk.document_id),
            )
            .join(
                DocumentVersion,
                (DocumentVersion.tenant_id == DocumentChunk.tenant_id)
                & (DocumentVersion.id == DocumentChunk.document_version_id),
            )
            .where(
                DocumentChunk.tenant_id == tenant_id,
                Document.tenant_id == tenant_id,
                Document.status == "approved",
                DocumentVersion.status == "ready",
                Document.active_version == DocumentVersion.version,
                # Only vectors from the model that made the question's vector compare.
                # Chunks from before the model was recorded count as matching.
                DocumentChunk.embedding_model.in_((model, "")),
            )
            .order_by(distance)
            .limit(limit)
        )
        return [(chunk, document, float(score)) for chunk, document, score in rows.all()]

    async def reindex_other_models(self, tenant_id: UUID, model: str) -> int:
        """Queue every live document whose chunks another model made, once, so it is
        searchable again after the embedding model changed. Returns how many."""
        stale = (
            await self.session.scalars(
                select(DocumentVersion.id)
                .join(
                    Document,
                    (Document.tenant_id == DocumentVersion.tenant_id)
                    & (Document.id == DocumentVersion.document_id)
                    & (Document.active_version == DocumentVersion.version),
                )
                .where(
                    DocumentVersion.tenant_id == tenant_id,
                    DocumentVersion.status == "ready",
                    exists().where(
                        DocumentChunk.tenant_id == tenant_id,
                        DocumentChunk.document_version_id == DocumentVersion.id,
                        DocumentChunk.embedding_model.not_in((model, "")),
                    ),
                    ~exists().where(
                        KnowledgeJob.tenant_id == tenant_id,
                        KnowledgeJob.document_version_id == DocumentVersion.id,
                        KnowledgeJob.status.in_(("pending", "processing")),
                    ),
                )
                .limit(50)
            )
        ).all()
        for version_id in stale:
            self.session.add(
                KnowledgeJob(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    document_version_id=version_id,
                    status="pending",
                    attempts=0,
                )
            )
        await self.session.flush()
        return len(stale)
