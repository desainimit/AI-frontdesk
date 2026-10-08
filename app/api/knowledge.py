"""Tenant document API: raw request bodies with strict size and format checks."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request

from app.api.tenancy import Context, DatabaseDependency, Limit, Offset, Search
from app.config.settings import Settings
from app.database.unit_of_work import UnitOfWork
from app.knowledge.embeddings import open_embedder
from app.knowledge.schemas import DocumentView, KnowledgeQuery, KnowledgeResult, VersionView
from app.knowledge.service import KnowledgeService
from app.knowledge.storage import build_storage

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])
FileName = Annotated[str, Header(alias="X-File-Name", min_length=1, max_length=200)]


def service(request: Request, database: DatabaseDependency) -> KnowledgeService:
    settings: Settings = request.app.state.settings
    return KnowledgeService(UnitOfWork(database.session_factory), build_storage(settings))


async def body(request: Request) -> bytes:
    settings: Settings = request.app.state.settings
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > settings.knowledge_max_upload_bytes:
            raise HTTPException(413, "Document is too large")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/documents", status_code=202)
async def upload_document(
    request: Request, name: FileName, context: Context, database: DatabaseDependency
) -> DocumentView:
    settings: Settings = request.app.state.settings
    try:
        return await service(request, database).upload(
            context, name, await body(request), settings.knowledge_max_upload_bytes
        )
    except (ValueError, UnicodeError):
        raise HTTPException(422, "Invalid document") from None


@router.post("/documents/{document_id}/versions", status_code=202)
async def upload_version(
    document_id: UUID,
    request: Request,
    name: FileName,
    context: Context,
    database: DatabaseDependency,
) -> DocumentView:
    settings: Settings = request.app.state.settings
    try:
        return await service(request, database).upload(
            context,
            name,
            await body(request),
            settings.knowledge_max_upload_bytes,
            document_id,
        )
    except (ValueError, UnicodeError):
        raise HTTPException(422, "Invalid document") from None


@router.get("/documents")
async def list_documents(
    request: Request,
    context: Context,
    database: DatabaseDependency,
    limit: Limit = 50,
    offset: Offset = 0,
    status: Literal["draft", "approved", "archived"] | None = None,
    q: Search = None,
) -> list[DocumentView]:
    # Listing existing records needs no embedding provider or ingestion worker.
    return await service(request, database).list_documents(
        context, limit, offset, status=status, q=q
    )


@router.get("/documents/{document_id}/versions")
async def list_versions(
    document_id: UUID, request: Request, context: Context, database: DatabaseDependency
) -> list[VersionView]:
    return await service(request, database).versions(context, document_id)


@router.post("/documents/{document_id}/approve")
async def approve_document(
    document_id: UUID, request: Request, context: Context, database: DatabaseDependency
) -> DocumentView:
    return await service(request, database).set_status(context, document_id, approve=True)


@router.post("/documents/{document_id}/archive")
async def archive_document(
    document_id: UUID, request: Request, context: Context, database: DatabaseDependency
) -> DocumentView:
    return await service(request, database).set_status(context, document_id, approve=False)


@router.post("/search")
async def search_knowledge(
    query: KnowledgeQuery, request: Request, context: Context, database: DatabaseDependency
) -> KnowledgeResult:
    settings: Settings = request.app.state.settings
    provider = await open_embedder(
        settings, getattr(request.app.state, "integration_resolver", None), context.tenant_id
    )
    try:
        return await KnowledgeService(
            UnitOfWork(database.session_factory),
            build_storage(request.app.state.settings),
            provider,
        ).search(context, query.question, query.limit)
    finally:
        await provider.close()
