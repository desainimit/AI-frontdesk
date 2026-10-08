import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database.rls import set_tenant_scope
from app.database.session import Database
from app.database.unit_of_work import UnitOfWork
from app.errors import ConflictError, NotFoundError
from app.knowledge.embeddings import EMBEDDING_DIMENSIONS
from app.knowledge.models import DocumentChunk
from app.knowledge.service import KnowledgeService
from app.knowledge.storage import LocalObjectStorage
from app.knowledge.worker import KnowledgeWorker
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


class FakeEmbedder:
    async def embed(self, texts: list[str], *, kind: str = "document") -> list[list[float]]:
        return [[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1) for _ in texts]


@pytest.mark.skipif(importlib.util.find_spec("docling") is None, reason="worker extra required")
async def test_tenant_document_lifecycle_and_retrieval(
    tenancy: TenancyFixture, tmp_path: Path
) -> None:
    assert tenancy.settings.worker_database_url is not None
    storage = LocalObjectStorage(str(tmp_path))
    embedder = FakeEmbedder()
    service = KnowledgeService(UnitOfWork(tenancy.database.session_factory), storage, embedder)
    worker_db = Database(
        tenancy.settings.model_copy(update={"database_url": tenancy.settings.worker_database_url})
    )
    worker = KnowledgeWorker(worker_db.session_factory, storage, embedder)
    a = tenancy.context(tenancy.a)
    b = tenancy.context(tenancy.b)
    try:
        doc_a = await service.upload(a, "hours.md", b"# Hours\n\nOpen Tuesday to Friday.", 1000000)
        doc_b = await service.upload(
            b, "secret.md", b"# Policy\n\nTenant B private policy.", 1000000
        )
        with pytest.raises(ConflictError):
            await service.set_status(a, doc_a.id, approve=True)
        assert await worker.process_one()
        assert await worker.process_one()
        await service.set_status(a, doc_a.id, approve=True)
        await service.set_status(b, doc_b.id, approve=True)
        answer_a = await service.search(a, "What are your opening hours?")
        answer_b = await service.search(b, "What is your policy?")
        assert answer_a.status == "found" and {s.document_name for s in answer_a.sources} == {
            "hours.md"
        }
        assert answer_b.status == "found" and {s.document_name for s in answer_b.sources} == {
            "secret.md"
        }
        with pytest.raises(NotFoundError):
            await service.versions(a, doc_b.id)
        async with tenancy.database.session_factory.begin() as session:
            assert (await session.scalars(select(DocumentChunk))).all() == []
            await set_tenant_scope(session, a.tenant_id)
            chunks = (await session.scalars(select(DocumentChunk))).all()
            assert chunks and all(chunk.tenant_id == a.tenant_id for chunk in chunks)
        await service.upload(a, "new-hours.md", b"# Hours\n\nOpen every day.", 1000000, doc_a.id)
        assert (await service.search(a, "What are your opening hours?")).status == "not_found"
        assert await worker.process_one()
        await service.set_status(a, doc_a.id, approve=True)
        assert {
            s.document_name for s in (await service.search(a, "What are your hours?")).sources
        } == {"new-hours.md"}
        await service.set_status(a, doc_a.id, approve=False)
        assert (await service.search(a, "What are your opening hours?")).status == "not_found"
    finally:
        await worker_db.close()
