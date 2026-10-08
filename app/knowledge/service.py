"""Upload, approve, archive, and search approved static tenant knowledge."""

from hashlib import sha256
from io import BytesIO
from pathlib import PurePath
from uuid import UUID, uuid4
from zipfile import BadZipFile, ZipFile

from app.audit.events import AuditAction
from app.audit.service import AuditService
from app.auth.context import ActorType, RequestContext
from app.auth.policies import Permission, require_permission
from app.database.unit_of_work import UnitOfWork
from app.errors import ConflictError, NotFoundError
from app.knowledge.embeddings import Embedder
from app.knowledge.schemas import DocumentView, KnowledgeResult, SourceView, VersionView
from app.knowledge.storage import ObjectStorage

CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "md": "text/markdown",
}


def validate_upload(name: str, body: bytes, max_bytes: int) -> str:
    if not name or len(name) > 200 or any(char in name for char in "/\\\0"):
        raise ValueError("Invalid document name")
    suffix = PurePath(name).suffix.lower().lstrip(".")
    if suffix not in CONTENT_TYPES or not body or len(body) > max_bytes:
        raise ValueError("Unsupported document format or size")
    if suffix == "pdf" and not body.startswith(b"%PDF-"):
        raise ValueError("Invalid PDF")
    if suffix == "md":
        body.decode("utf-8")
    if suffix == "docx":
        try:
            with ZipFile(BytesIO(body)) as archive:
                if "word/document.xml" not in archive.namelist():
                    raise ValueError("Invalid DOCX")
                if sum(item.file_size for item in archive.infolist()) > max_bytes * 5:
                    raise ValueError("DOCX expansion limit exceeded")
        except BadZipFile:
            raise ValueError("Invalid DOCX") from None
    return suffix


class KnowledgeService:
    def __init__(
        self, uow: UnitOfWork, storage: ObjectStorage, embedder: Embedder | None = None
    ) -> None:
        self.uow, self.storage, self.embedder = uow, storage, embedder

    async def upload(
        self,
        context: RequestContext,
        name: str,
        body: bytes,
        max_bytes: int,
        document_id: UUID | None = None,
    ) -> DocumentView:
        require_permission(context, Permission.KNOWLEDGE_WRITE)
        source_type = validate_upload(name, body, max_bytes)
        key = f"{context.tenant_id.hex}/{uuid4().hex}/{uuid4().hex}"
        await self.storage.put(key, body, CONTENT_TYPES[source_type])
        try:
            async with self.uow.transaction(context) as tx:
                # The repository queues the ingestion job with the new version.
                document, _ = await tx.knowledge.create_version(
                    context.tenant_id,
                    name,
                    source_type,
                    sha256(body).hexdigest(),
                    key,
                    document_id,
                )
                AuditService(tx.audit).record(
                    context, AuditAction.DOCUMENT_UPLOADED, "document", document.id
                )
                return DocumentView.model_validate(document)
        except Exception:
            await self.storage.delete(key)
            raise

    async def list_documents(
        self,
        context: RequestContext,
        limit: int,
        offset: int,
        *,
        status: str | None = None,
        q: str | None = None,
    ) -> list[DocumentView]:
        require_permission(context, Permission.KNOWLEDGE_READ)
        async with self.uow.transaction(context) as tx:
            return [
                DocumentView.model_validate(item)
                for item in await tx.knowledge.list_documents(limit, offset, status=status, q=q)
            ]

    async def versions(self, context: RequestContext, document_id: UUID) -> list[VersionView]:
        require_permission(context, Permission.KNOWLEDGE_READ)
        async with self.uow.transaction(context) as tx:
            document = await tx.knowledge.get(document_id)
            if document is None:
                raise NotFoundError()
            return [
                VersionView.model_validate(item)
                for item in await tx.knowledge.versions(document_id)
            ]

    async def set_status(
        self, context: RequestContext, document_id: UUID, *, approve: bool
    ) -> DocumentView:
        require_permission(context, Permission.KNOWLEDGE_WRITE)
        async with self.uow.transaction(context) as tx:
            document = await tx.knowledge.get_for_update(document_id)
            if document is None:
                raise NotFoundError()
            if approve:
                versions = await tx.knowledge.versions(document_id)
                if (
                    not versions
                    or versions[0].version != document.active_version
                    or versions[0].status != "ready"
                ):
                    raise ConflictError()
                document.status = "approved"
                action = AuditAction.DOCUMENT_APPROVED
            else:
                document.status = "archived"
                action = AuditAction.DOCUMENT_ARCHIVED
            AuditService(tx.audit).record(context, action, "document", document.id)
            return DocumentView.model_validate(document)

    async def search(
        self, context: RequestContext, question: str, limit: int = 3
    ) -> KnowledgeResult:
        if context.actor_type in {ActorType.USER, ActorType.PLATFORM_SUPPORT}:
            require_permission(context, Permission.KNOWLEDGE_READ)
        elif context.actor_type != ActorType.CALLER:
            from app.errors import AuthorizationError

            raise AuthorizationError()
        if not 5 <= len(question.strip()) <= 300 or not 1 <= limit <= 5:
            raise ValueError("Invalid knowledge query")
        # Check caller binding before making a paid embedding request. The second
        # transaction rechecks it after the external call, before returning data.
        async with self.uow.transaction(context):
            pass
        if self.embedder is None:
            raise RuntimeError("Knowledge embedder is unavailable")
        vector = (await self.embedder.embed([question], kind="query"))[0]
        async with self.uow.transaction(context) as tx:
            rows = await tx.knowledge.retrieve(context.tenant_id, vector, limit)
            sources = [
                SourceView(
                    document_id=document.id,
                    version=document.active_version,
                    document_name=document.name,
                    section=chunk.section,
                    subsection=chunk.subsection,
                    page_number=chunk.page_number,
                    text=chunk.text[:2200],
                    distance=distance,
                )
                for chunk, document, distance in rows
                if distance <= 0.6
            ]
            return KnowledgeResult(status="found" if sources else "not_found", sources=sources)
