import importlib.util
import io
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.agents.language import spoken
from app.auth.context import ActorType, RequestContext
from app.auth.policies import Permission, require_permission
from app.core.errors import AuthorizationError
from app.knowledge.models import DocumentChunk
from app.knowledge.parsing import parse_document
from app.knowledge.schemas import KnowledgeQuery
from app.knowledge.service import validate_upload
from app.knowledge.storage import LocalObjectStorage


def test_upload_format_validation_and_access_control() -> None:
    assert validate_upload("policy.pdf", b"%PDF-1.7\n", 100) == "pdf"
    assert validate_upload("policy.md", b"# Hours\nOpen daily.", 100) == "md"
    archive = io.BytesIO()
    with ZipFile(archive, "w") as output:
        output.writestr("word/document.xml", "<doc/>")
    assert validate_upload("policy.docx", archive.getvalue(), 1000) == "docx"
    for name, body in [
        ("..\\policy.md", b"# Hi"),
        ("policy.exe", b"MZ"),
        ("policy.pdf", b"not a pdf"),
        ("policy.md", b"\xff"),
    ]:
        with pytest.raises((ValueError, UnicodeError)):
            validate_upload(name, body, 100)
    member = RequestContext(
        uuid4(),
        uuid4(),
        uuid4(),
        ActorType.USER,
        role_id=uuid4(),
        capabilities=frozenset({Permission.KNOWLEDGE_READ.value}),
    )
    require_permission(member, Permission.KNOWLEDGE_READ)
    with pytest.raises(AuthorizationError):
        require_permission(member, Permission.KNOWLEDGE_WRITE)
    with pytest.raises(ValidationError):
        KnowledgeQuery(question="x", limit=20)


async def test_local_object_keys_cannot_escape_storage_root(tmp_path: Path) -> None:
    storage = LocalObjectStorage(str(tmp_path))
    key = f"{uuid4().hex}/{uuid4().hex}"
    await storage.put(key, b"document", "text/markdown")
    assert await storage.get(key) == b"document"
    await storage.delete(key)
    for key in ("../other", "/absolute", "abc\\..\\other"):
        with pytest.raises(ValueError):
            await storage.get(key)


@pytest.mark.skipif(importlib.util.find_spec("docling") is None, reason="worker extra required")
def test_docling_preserves_headings_and_tokens() -> None:
    chunks = parse_document(
        "guide.md", b"# Hours\n\nOpen Tuesday to Friday.\n\n# Parking\n\nParking is free."
    )
    assert [(item.section, item.text) for item in chunks] == [
        ("Hours", "Open Tuesday to Friday."),
        ("Parking", "Parking is free."),
    ]
    assert all(item.token_count > 0 and len(item.checksum) == 64 for item in chunks)


def test_no_result_message_exists_in_every_launch_language() -> None:
    for code in ("en", "es", "fr", "de", "pt", "hi", "hinglish"):
        assert spoken(code, "knowledge_unavailable")


def test_vector_column_compiles_to_pgvector() -> None:
    query = (
        select(DocumentChunk)
        .where(DocumentChunk.tenant_id == uuid4())
        .order_by(DocumentChunk.embedding.cosine_distance([1.0] + [0.0] * 1535))
    )
    sql = str(query.compile(dialect=postgresql.dialect()))
    assert "document_chunks.tenant_id =" in sql
    assert "document_chunks.embedding <=>" in sql
